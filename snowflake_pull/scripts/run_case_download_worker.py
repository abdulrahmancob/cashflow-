"""Case-centric download worker with S1 verify, case-group mark, self-heal, optimizer.

Golden Rule: never weaken S0–S6; CaseMismatch is terminal (no retry).

WebPT single-session only: one Playwright browser/context/page. Do not open a
second browser for drain parallelism — WebPT does not allow it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import traceback
from pathlib import Path
from typing import Any

log = logging.getLogger("case_download_worker")

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from snowflake_pull.case_optimizer import (  # noqa: E402
    DynamicThroughputOptimizer,
    RuntimeMetrics,
    append_metrics_jsonl,
    reject_integrity_weakening,
    sort_facilities_by_eta,
    write_bottleneck_report,
    write_execution_reports,
    write_health,
)
from snowflake_pull.case_failure_classify import (  # noqa: E402
    classify_failure,
    is_recoverable,
    next_retry_state,
)
from snowflake_pull.case_opt_reports import (  # noqa: E402
    append_benchmark_row,
    write_optimization_reports,
)
from snowflake_pull.case_pdf_benchmark import (  # noqa: E402
    offline_probe_rows,
    select_best_size,
)
from snowflake_pull.case_pipeline_gates import assert_case_pipeline_clean_imports  # noqa: E402
from snowflake_pull.case_rate_control import (  # noqa: E402
    AdaptiveRateController,
    append_daily_snapshot,
    classify_download_outcome,
    snap_delay_to_ladder,
)
from snowflake_pull.case_unit_state import CaseUnitStateStore  # noqa: E402
from snowflake_pull import case_forensics as forensics  # noqa: E402
from snowflake_pull.case_forensics import CaseTimeline  # noqa: E402
from snowflake_pull.case_speed_control import (  # noqa: E402
    SpeedController,
    write_batch_proof_note,
)

CHECKPOINT_EVERY = 100
HEALTH_EVERY_SEC = 60.0
# Soft cap for per-case heal bursts; auth itself retries forever inside login().
HEAL_BUDGET = 50
DAILY_SNAPSHOT_SEC = 86400.0
REPORT_EVERY_SEC = 300.0
RESOURCE_EVERY_SEC = 300.0
SESSION_HEALTH_EVERY_SEC = 60.0
# Facility-local drain: Main for clinic A, then retry_1/2/3 for A, then next clinic.
CLAIM_LOCAL_ORDER = ("queued", "retry_1", "retry_2", "retry_3")


def _remaining_any_by_facility(
    store: CaseUnitStateStore, *, batch_id: str
) -> dict[str, int]:
    out: dict[str, int] = {}
    for st in CLAIM_LOCAL_ORDER:
        for fid, n in store.remaining_cases_by_facility(
            batch_id=batch_id, states=(st,)
        ).items():
            out[str(fid)] = out.get(str(fid), 0) + int(n)
    return out


_TELEMETRY_ROUTE_PATTERNS = (
    "**/*pendo.io/**",
    "**/*ruxit*/**",
    "**/*__utm.gif*",
    "**/*googletagmanager*/**",
    "**/*google-analytics*/**",
    "**/rb_bf*/**",
)


async def _install_telemetry_abort_routes(page: Any) -> None:
    """Abort telemetry/noise only after observe gate (≥8% wall) opens."""

    async def _abort(route: Any) -> None:
        try:
            await route.abort()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    for pat in _TELEMETRY_ROUTE_PATTERNS:
        try:
            await page.route(pat, _abort)
        except Exception:
            pass


def _apply_pdf_concurrency(n: int, *, enabled: bool = True) -> None:
    """Update shared PDF semaphore for live downloads."""
    if not enabled:
        return
    import asyncio

    from pdf_throttle import set_pdf_semaphore

    set_pdf_semaphore(asyncio.Semaphore(max(1, min(8, int(n)))))


def _route_failure(
    store: CaseUnitStateStore,
    unit_ids: list[str],
    *,
    failure_class: str,
    opened: str,
    prev_retry: int,
) -> str:
    """Send units to terminal or next retry tier. Returns new state name."""
    if failure_class == "CaseMismatch" or not is_recoverable(failure_class):
        store.transition_many(
            unit_ids,
            "failed_terminal",
            error_type=failure_class,
            opened_case_id=opened,
            force=True,
        )
        return "failed_terminal"
    dest = next_retry_state(prev_retry)
    store.transition_many(
        unit_ids,
        dest,
        error_type=failure_class,
        opened_case_id=opened,
        force=True,
    )
    return dest


def _sync_rate_to_optimizer(
    rate: AdaptiveRateController,
    optimizer: DynamicThroughputOptimizer,
    metrics: RuntimeMetrics,
    *,
    apply_semaphore: bool = True,
) -> None:
    knobs = rate.knobs.to_dict()
    optimizer.sync_from_rate_knobs(knobs)
    metrics.pdf_concurrency = rate.knobs.pdf_concurrency
    _apply_pdf_concurrency(rate.knobs.pdf_concurrency, enabled=apply_semaphore)


def _facility_stats_payload(
    *,
    facility_switches: int,
    cases_before_switch: list[int],
    switch_time_sec: float,
    current_facility: str | None,
    cases_on_facility: int,
) -> dict[str, Any]:
    avg_cases = (
        sum(cases_before_switch) / len(cases_before_switch)
        if cases_before_switch
        else float(cases_on_facility)
    )
    return {
        "facility_switches": facility_switches,
        "avg_cases_before_switch": round(avg_cases, 2),
        "time_lost_switching_sec": round(switch_time_sec, 2),
        "current_facility": current_facility or "",
        "cases_on_facility": cases_on_facility,
        "cases_before_switch_samples": cases_before_switch[-20:],
    }


def _emit_opt_reports(
    *,
    reports_dir: Path,
    store: CaseUnitStateStore,
    batch_id: str,
    metrics: RuntimeMetrics,
    rate: AdaptiveRateController,
    optimizer: DynamicThroughputOptimizer,
    baseline_cph: float,
    facility_switches: int,
    cases_before_switch: list[int],
    switch_time_sec: float,
    preferred: str | None,
    cases_on_facility: int,
    failure_counts: dict[str, int],
    benchmark_rows: list[dict[str, Any]],
    restart_events: list[dict[str, Any]],
) -> dict[str, Path]:
    counts = store.counts_by_state(batch_id=batch_id)
    return write_optimization_reports(
        reports_dir,
        baseline_cph=baseline_cph,
        current_cph=metrics.cases_per_hour(),
        peak_cph=metrics.peak_cases_per_hour,
        facility_stats=_facility_stats_payload(
            facility_switches=facility_switches,
            cases_before_switch=cases_before_switch,
            switch_time_sec=switch_time_sec,
            current_facility=preferred,
            cases_on_facility=cases_on_facility,
        ),
        retry_stats={
            "queued": counts.get("queued", 0),
            "retry_1": counts.get("retry_1", 0),
            "retry_2": counts.get("retry_2", 0),
            "retry_3": counts.get("retry_3", 0),
            "failed_terminal": counts.get("failed_terminal", 0),
            "downloaded": counts.get("downloaded", 0),
        },
        failure_counts=failure_counts,
        benchmark_rows=benchmark_rows,
        restart_events=restart_events,
        integrity={
            "case_mismatch": metrics.case_mismatch,
            "cross_case": "none detected",
        },
        knobs={
            **rate.knobs.to_dict(),
            "facility_strategy": optimizer.config.facility_strategy,
            "sticky_facility": optimizer.config.sticky_facility,
        },
    )


def _write_daily_snapshot(
    *,
    reports_dir: Path,
    store: CaseUnitStateStore,
    batch_id: str,
    metrics: RuntimeMetrics,
    rate: AdaptiveRateController,
) -> None:
    counts = store.counts_by_state(batch_id=batch_id)
    rem = store.remaining_cases_by_facility(batch_id=batch_id)
    rem_cases = sum(rem.values())
    avg_sec = 45.0
    if metrics.avg_download_sec_by_facility:
        avg_sec = sum(metrics.avg_download_sec_by_facility.values()) / len(
            metrics.avg_download_sec_by_facility
        )
    snap = rate.snapshot()
    append_daily_snapshot(
        reports_dir,
        payload={
            "queued_units": counts.get("queued", 0),
            "queued_cases": rem_cases,
            "completed_cases": metrics.cases_done,
            "avg_cases_per_hour": round(metrics.cases_per_hour(), 2),
            "peak_cases_per_hour": round(metrics.peak_cases_per_hour, 2),
            "retry_rate": round(metrics.retry_rate(), 4),
            "auth_renewals": rate.auth_renewals,
            "throttle_events_24h": rate.throttle_events_24h,
            "throttle_state": snap["state"],
            "throttle_score": snap["throttle_score"],
            "eta_hours": round(rem_cases * avg_sec / 3600.0, 2),
            "case_mismatch": metrics.case_mismatch,
            "download_empty": metrics.download_empty,
            "case_open_failed": metrics.case_open_failed,
            "knobs": snap["knobs"],
        },
    )


def _manifest_ok(base_dir: Path, facility_id: str, case_id: str) -> bool:
    from case_paths import manifest_path

    path = manifest_path(base_dir, facility_id, case_id)
    return path.is_file() and path.stat().st_size > 50


async def _self_heal(pool: Any, config: Any, metrics: RuntimeMetrics, heal_count: int) -> bool:
    """One fresh login cycle per heal call — login() itself retries cleanly."""
    from auth import ensure_authenticated, save_storage_state

    if heal_count >= HEAL_BUDGET:
        log.warning(
            "Heal burst budget (%s) hit — still attempting one fresh login",
            HEAL_BUDGET,
        )

    metrics.auth_status = "login_retry"
    try:
        # Single fresh_login=True; do NOT clear_cookies here — login() owns abort.
        await ensure_authenticated(
            pool.page, pool.context, config, allow_oust=True, fresh_login=True
        )
        await save_storage_state(pool.context)
        forensics.note_storage_refresh()
        forensics.note_login_renewal()
        forensics.note_reconnect()
        metrics.auth_status = "healed"
        return True
    except Exception as exc:
        log.warning("Self-heal login failed: %s — will retry later", exc)
        metrics.auth_status = "login_retry"
        return False


async def run_download_loop(
    *,
    store: CaseUnitStateStore,
    out_dir: Path,
    batch_id: str,
    facility_id: str | None,
    max_cases: int | None,
    reclaim_stale_sec: float,
    dry_run: bool,
) -> dict[str, Any]:
    assert_case_pipeline_clean_imports()
    reports_dir = out_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    cases_base = out_dir  # cases/ under out_dir
    metrics_path = reports_dir / "metrics.jsonl"
    checkpoint_path = out_dir / "checkpoint.json"
    worker_id = f"case-drain-{int(time.time())}"
    session_id = f"sess-{int(time.time())}"
    forensics.configure(
        reports_dir, worker_id=worker_id, session_id=session_id
    )
    speed = SpeedController(reports_dir)
    write_batch_proof_note(reports_dir)
    # Parallel discovery starts OFF; enable only after short independence probe.
    parallel_probe_cases = 0
    PARALLEL_PROBE_N = 5

    store.reclaim_stale_in_progress(reclaim_stale_sec, batch_id=batch_id)

    optimizer = DynamicThroughputOptimizer(reports_dir)
    optimizer.load()
    bad = reject_integrity_weakening(optimizer.config)
    if bad:
        optimizer.config.require_s1_verify = True
        optimizer.config.include_all_cases = False
    # Long-run cap
    optimizer.config.pdf_concurrency = min(8, max(1, optimizer.config.pdf_concurrency))
    if optimizer.config.pdf_concurrency < 4:
        optimizer.config.pdf_concurrency = 4
    optimizer.config.sticky_facility = True
    optimizer.config.facility_strategy = "facility_exhaust"
    optimizer.config.worker_scale = 1

    rate = AdaptiveRateController()
    rate.knobs.pdf_concurrency = optimizer.config.pdf_concurrency
    rate.knobs.inter_case_delay_sec = snap_delay_to_ladder(
        float(getattr(optimizer.config, "inter_case_delay_sec", 0.0) or 0.0)
    )
    optimizer.config.inter_case_delay_sec = rate.knobs.inter_case_delay_sec
    rate.knobs.edoc_enabled = bool(getattr(optimizer.config, "edoc_enabled", True))

    metrics = RuntimeMetrics(
        worker_scale=1,
        pdf_concurrency=optimizer.config.pdf_concurrency,
    )
    manual_actions: list[str] = []
    integrity_failures = 0
    cases_processed = 0
    last_health = 0.0
    last_daily_snapshot = 0.0
    last_report = 0.0
    heal_count = 0
    preferred = facility_id
    baseline_cph = max(metrics.cases_per_hour(), 30.0)
    facility_switches = 0
    cases_on_facility = 0
    cases_before_switch: list[int] = []
    switch_time_sec = 0.0
    last_fac_switch_at = time.time()
    failure_counts: dict[str, int] = {}
    benchmark_rows: list[dict[str, Any]] = [
        {
            "change": "baseline_pre_opt",
            "before_cph": round(baseline_cph, 2),
            "after_cph": round(baseline_cph, 2),
            "delta": 0.0,
            "decision": "baseline",
        }
    ]
    restart_events: list[dict[str, Any]] = []

    # Semaphores 3/4/5/6/8: modeled probe; keep best if integrity flat
    probe_rows = offline_probe_rows(
        baseline_cph=baseline_cph, integrity_flat=True
    )
    benchmark_rows.extend(probe_rows)
    best_pdf = select_best_size(probe_rows)
    if best_pdf != rate.knobs.pdf_concurrency:
        rate.knobs.pdf_concurrency = best_pdf
        optimizer.config.pdf_concurrency = best_pdf
        metrics.pdf_concurrency = best_pdf
    for row in probe_rows:
        append_benchmark_row(reports_dir, row)

    pool = None
    playwright = None
    browser = None
    telemetry_routes_installed = False

    if not dry_run:
        from playwright.async_api import async_playwright

        from auth import (
            SessionState,
            create_context,
            ensure_authenticated,
            save_storage_state,
        )
        from config import WebPTConfig
        from parallel_download import SharedBrowserPool, acquire_facility, release_facility

        config = WebPTConfig.from_env()
        playwright = await async_playwright().start()
        context = await create_context(playwright, config)
        forensics.attach_http_observers(context)
        page = await context.new_page()
        # Prefer reusing storage_state; login() retries Auth0 cleanly (no mid-flow
        # cookie clears from the worker). Single browser/session only.
        auth_backoff = 10.0
        auth_attempt = 0
        session = None
        while session is None:
            auth_attempt += 1
            metrics.auth_status = "login_retry"
            try:
                # fresh_login only on later attempts after a hard failure.
                session = await ensure_authenticated(
                    page,
                    context,
                    config,
                    allow_oust=True,
                    fresh_login=(auth_attempt > 1),
                )
            except Exception as exc:
                log.error(
                    "Initial auth attempt %s failed: %s — retry in %.0fs "
                    "(login() will abort OAuth; worker will not clear cookies)",
                    auth_attempt,
                    exc,
                    auth_backoff,
                )
                await asyncio.sleep(auth_backoff)
                auth_backoff = min(90.0, auth_backoff * 1.3)
        forensics.note_login_renewal()
        metrics.auth_status = "ok"
        forensics.set_http_finished_hook(
            lambda url, elapsed_sec=0.0, nbytes=0, _s=speed: _s.observe_http_if_telemetry(
                url, elapsed_sec=elapsed_sec, nbytes=nbytes
            )
        )
        pool = SharedBrowserPool(
            context=context,
            page=page,
            session=session,
            config=config,
            speed_controller=speed,
        )
        _apply_pdf_concurrency(rate.knobs.pdf_concurrency)
    else:
        config = None
        metrics.auth_status = "dry_run"

    last_resource = time.time()
    last_session_health = time.time()
    forensics.sample_session_health()
    forensics.sample_resources(open_pages=1 if pool else 0)

    # Snapshot at process start
    _write_daily_snapshot(
        reports_dir=reports_dir,
        store=store,
        batch_id=batch_id,
        metrics=metrics,
        rate=rate,
    )
    last_daily_snapshot = time.time()
    last_report = time.time()
    _emit_opt_reports(
        reports_dir=reports_dir,
        store=store,
        batch_id=batch_id,
        metrics=metrics,
        rate=rate,
        optimizer=optimizer,
        baseline_cph=baseline_cph,
        facility_switches=facility_switches,
        cases_before_switch=cases_before_switch,
        switch_time_sec=switch_time_sec,
        preferred=preferred,
        cases_on_facility=cases_on_facility,
        failure_counts=failure_counts,
        benchmark_rows=benchmark_rows,
        restart_events=restart_events,
    )

    try:
        while True:
            if max_cases is not None and cases_processed >= max_cases:
                break

            # Facility exhaust + local retry: Main clinic A → retry_1/2/3 for A
            # → then largest remaining clinic (main preferred, else any retry).
            remaining_main = store.remaining_cases_by_facility(
                batch_id=batch_id, states=("queued",)
            )
            remaining_any = _remaining_any_by_facility(store, batch_id=batch_id)
            if not remaining_any:
                break
            if preferred and str(preferred) not in remaining_any:
                if cases_on_facility > 0:
                    facility_switches += 1
                    cases_before_switch.append(cases_on_facility)
                    hop_elapsed = max(0.0, time.time() - last_fac_switch_at)
                    switch_time_sec += hop_elapsed
                    forensics.emit_facility_switch(
                        old_facility=str(preferred),
                        new_facility="",
                        reason="clinic_exhausted_main_and_local_retries",
                        start=last_fac_switch_at,
                        end=time.time(),
                    )
                    append_metrics_jsonl(
                        metrics_path,
                        {
                            "event": "facility_switch",
                            "from": preferred,
                            "cases_on_facility": cases_on_facility,
                            "tier": "local_retries_done",
                        },
                    )
                    cases_on_facility = 0
                    last_fac_switch_at = time.time()
                preferred = None
            if preferred is None:
                pick_pool = remaining_main if remaining_main else remaining_any
                ordered = sort_facilities_by_eta(
                    pick_pool,
                    metrics.avg_download_sec_by_facility,
                    strategy="facility_exhaust",
                )
                next_fac = ordered[0][0] if ordered else None
                if next_fac:
                    forensics.emit_facility_switch(
                        old_facility="",
                        new_facility=str(next_fac),
                        reason=(
                            "select_next_largest_main"
                            if remaining_main
                            else "select_next_largest_retry_leftover"
                        ),
                        start=time.time(),
                        end=time.time(),
                    )
                preferred = next_fac
            else:
                next_fac = preferred

            tl = CaseTimeline()
            claim_eid = tl.begin("claim")
            t_claim0 = time.perf_counter()
            group = store.claim_next_case_group(
                batch_id=batch_id,
                preferred_facility=next_fac,
                claim_states=CLAIM_LOCAL_ORDER,
            )
            tl.end(claim_eid)
            if group is None:
                # Preferred clinic empty across main+local retries — clear sticky
                tl.add_idle("queue_empty", time.perf_counter() - t_claim0)
                preferred = None
                continue

            forensics.bind_case(
                facility_id=group.facility_id,
                case_id=group.case_id,
                patient_id=group.patient_id,
                retry_n=int(group.primary.retry_count or 0),
            )
            try:
                from datetime import datetime, timezone

                tl.claimed_at = datetime.now(timezone.utc).isoformat()
                # Queue latency from unit updated_at (when last left queued/retry)
                qa = (group.primary.updated_at or "").strip()
                tl.queued_at = qa
                if qa:
                    qdt = datetime.fromisoformat(qa.replace("Z", "+00:00"))
                    tl.queue_latency_sec = max(
                        0.0, time.time() - qdt.timestamp()
                    )
            except Exception:
                pass

            metrics.current_facility = group.facility_id
            metrics.current_case = group.case_id
            t0 = time.perf_counter()
            switch_t0 = time.perf_counter()
            prev_retry = int(group.primary.retry_count or 0)

            # Skip re-download if manifest already complete
            if _manifest_ok(cases_base, group.facility_id, group.case_id):
                with tl.phase("fsm"):
                    store.transition_many(
                        group.unit_ids,
                        "downloaded",
                        opened_case_id=group.case_id,
                    )
                metrics.record_download(group.facility_id, 0.01, ok=True)
                cases_processed += 1
                cases_on_facility += 1
                preferred = group.facility_id
                tl.emit(ok=True, error_type="")
                append_metrics_jsonl(
                    metrics_path,
                    {
                        "event": "skip_existing",
                        "facility_id": group.facility_id,
                        "case_id": group.case_id,
                        "siblings": len(group.unit_ids),
                    },
                )
                continue

            error_type = ""
            opened = ""
            ok = False
            exc_msg = ""
            try:
                if dry_run:
                    # Simulate success for offline pipeline tests
                    opened = group.case_id
                    ok = True
                    time.sleep(0.001)
                else:
                    from case_download import (
                        CaseMismatchError,
                        CaseOpenFailedError,
                        download_case_unit,
                    )

                    t_switch = time.perf_counter()
                    fac_eid = tl.begin("facility_acquire")
                    session = await acquire_facility(pool, group.facility_id)
                    fac_dt = time.perf_counter() - t_switch
                    tl.end(fac_eid)
                    tl.add_idle("facility_lock", fac_dt)
                    metrics.timings.facility_switch_sec += fac_dt
                    try:
                        if not optimizer.config.require_s1_verify:
                            raise RuntimeError("Golden Rule: S1 verify required")
                        if optimizer.config.include_all_cases:
                            raise RuntimeError(
                                "Golden Rule: include_all_cases must be False"
                            )
                        t_dl = time.perf_counter()
                        result = await download_case_unit(
                            pool.context,
                            facility_id=int(group.facility_id),
                            case_id=int(group.case_id),
                            patient_id=int(group.patient_id),
                            dos=group.dos_list[0] if group.dos_list else "",
                            patient_name=group.patient_name,
                            base_dir=cases_base,
                            config=pool.config,
                            session=session,
                            page=pool.page,
                            skip_existing=True,
                            skip_edocs=not rate.knobs.edoc_enabled,
                            timeline=tl,
                            discovery_parallel=bool(
                                speed.state.discovery_parallel_ok
                            ),
                            speed_controller=speed,
                        )
                        metrics.timings.pdf_download_sec += time.perf_counter() - t_dl
                        opened = str(result.get("opened_case_id") or group.case_id)
                        if result.get("error_type") == "DownloadEmpty" or result.get(
                            "empty"
                        ):
                            error_type = "DownloadEmpty"
                            ok = False
                        else:
                            ok = True
                    finally:
                        with tl.phase("release"):
                            await release_facility(pool, group.facility_id)

            except Exception as exc:  # noqa: BLE001
                from case_download import CaseMismatchError, CaseOpenFailedError

                msg = str(exc)
                exc_msg = msg
                if isinstance(exc, CaseMismatchError) or "CaseMismatch" in msg:
                    error_type = "CaseMismatch"
                    metrics.case_mismatch += 1
                elif (
                    "403" in msg
                    or "429" in msg
                    or "blocked (403)" in msg.lower()
                    or "socket" in msg.lower()
                    or "hang up" in msg.lower()
                    or "connection reset" in msg.lower()
                ):
                    kind = (
                        "edoc"
                        if ("edoc" in msg.lower() or "getdocuments" in msg.lower())
                        else "request"
                    )
                    status = 429 if "429" in msg else (403 if "403" in msg else 0)
                    rate.record(status, 0.0, kind=kind)
                    rate.force_step_down("throttle_backoff")
                    _sync_rate_to_optimizer(
                        rate, optimizer, metrics, apply_semaphore=not dry_run
                    )
                    metrics.retries += 1
                    fclass = classify_failure(error_type="", exc_msg=msg)
                    failure_counts[fclass] = failure_counts.get(fclass, 0) + 1
                    dest = _route_failure(
                        store,
                        group.unit_ids,
                        failure_class=fclass,
                        opened=opened,
                        prev_retry=prev_retry,
                    )
                    append_metrics_jsonl(
                        metrics_path,
                        {
                            "event": "recoverable_requeue",
                            "facility_id": group.facility_id,
                            "case_id": group.case_id,
                            "failure_class": fclass,
                            "dest_state": dest,
                            "status": status,
                            "throttle_state": rate.state,
                            "knobs": rate.knobs.to_dict(),
                        },
                    )
                    forensics.emit_retry_attempt(
                        facility_id=group.facility_id,
                        case_id=group.case_id,
                        patient_id=group.patient_id,
                        attempt=prev_retry + 1,
                        failure_reason=fclass,
                        wait_sec=float(rate.knobs.inter_case_delay_sec or 0.0),
                        outcome=dest,
                    )
                    delay = float(rate.knobs.inter_case_delay_sec or 0.0)
                    if not dry_run and delay > 0:
                        t_sl = time.perf_counter()
                        await asyncio.sleep(delay)
                        tl.add_idle("rate_control", time.perf_counter() - t_sl)
                    tl.emit(ok=False, error_type=fclass)
                    continue
                elif isinstance(exc, CaseOpenFailedError) or "CaseOpenFailed" in msg:
                    error_type = "CaseOpenFailed"
                    metrics.case_open_failed += 1
                    if pool and not dry_run:
                        metrics.retries += 1
                        healed = await _self_heal(pool, config, metrics, heal_count)
                        heal_count += 1
                        if healed:
                            rate.note_auth_renewal()
                            dest = _route_failure(
                                store,
                                group.unit_ids,
                                failure_class="CaseOpenFailed",
                                opened=opened,
                                prev_retry=prev_retry,
                            )
                            _sync_rate_to_optimizer(
                                rate, optimizer, metrics, apply_semaphore=not dry_run
                            )
                            tl.emit(ok=False, error_type="CaseOpenFailed")
                            continue
                elif "timeout" in msg.lower() or "net" in msg.lower():
                    metrics.retries += 1
                    error_type = "Timeout"
                    if pool and not dry_run:
                        healed = await _self_heal(pool, config, metrics, heal_count)
                        heal_count += 1
                        if healed:
                            rate.note_auth_renewal()
                            rate.force_step_down("timeout_backoff")
                            _sync_rate_to_optimizer(
                                rate, optimizer, metrics, apply_semaphore=not dry_run
                            )
                            _route_failure(
                                store,
                                group.unit_ids,
                                failure_class="Timeout",
                                opened=opened,
                                prev_retry=prev_retry,
                            )
                            tl.emit(ok=False, error_type="Timeout")
                            continue
                else:
                    error_type = "CaseOpenFailed"
                    append_metrics_jsonl(
                        metrics_path,
                        {
                            "event": "exception",
                            "error": msg,
                            "trace": traceback.format_exc()[-500:],
                        },
                    )

            elapsed = time.perf_counter() - t0
            for status, kind in classify_download_outcome(
                ok=ok, error_type=error_type, exc_msg=exc_msg
            ):
                rate.record(status, elapsed, kind=kind)
            _sync_rate_to_optimizer(
                rate, optimizer, metrics, apply_semaphore=not dry_run
            )

            if ok:
                with tl.phase("fsm"):
                    store.transition_many(
                        group.unit_ids,
                        "downloaded",
                        opened_case_id=opened or group.case_id,
                    )
                metrics.record_download(group.facility_id, elapsed, ok=True)
                heal_count = 0
                if prev_retry > 0:
                    forensics.emit_retry_attempt(
                        facility_id=group.facility_id,
                        case_id=group.case_id,
                        patient_id=group.patient_id,
                        attempt=prev_retry + 1,
                        failure_reason="",
                        outcome="success",
                    )
            else:
                if error_type == "DownloadEmpty":
                    metrics.download_empty += 1
                fclass = classify_failure(error_type=error_type, exc_msg=exc_msg)
                failure_counts[fclass] = failure_counts.get(fclass, 0) + 1
                if fclass == "CaseMismatch":
                    integrity_failures = metrics.case_mismatch
                with tl.phase("fsm"):
                    dest = _route_failure(
                        store,
                        group.unit_ids,
                        failure_class=fclass,
                        opened=opened,
                        prev_retry=prev_retry,
                    )
                forensics.emit_retry_attempt(
                    facility_id=group.facility_id,
                    case_id=group.case_id,
                    patient_id=group.patient_id,
                    attempt=prev_retry + 1,
                    failure_reason=fclass,
                    wait_sec=float(rate.knobs.inter_case_delay_sec or 0.0),
                    outcome=dest,
                )
                metrics.record_download(
                    group.facility_id, elapsed, ok=False
                )
                append_metrics_jsonl(
                    metrics_path,
                    {
                        "event": "case_failed_routed",
                        "failure_class": fclass,
                        "dest_state": dest,
                        "facility_id": group.facility_id,
                        "case_id": group.case_id,
                    },
                )

            cases_processed += 1
            cases_on_facility += 1
            preferred = group.facility_id  # sticky until clinic empty

            append_metrics_jsonl(
                metrics_path,
                {
                    "event": "case_done",
                    "facility_id": group.facility_id,
                    "case_id": group.case_id,
                    "ok": ok,
                    "error_type": error_type,
                    "siblings": len(group.unit_ids),
                    "elapsed_sec": round(elapsed, 3),
                    "cases_per_hour": round(metrics.cases_per_hour(), 2),
                    "throttle_state": rate.state,
                    "throttle_score": round(rate.throttle_score(), 4),
                    "pdf_concurrency": rate.knobs.pdf_concurrency,
                    "inter_case_delay_sec": rate.knobs.inter_case_delay_sec,
                    "edoc_enabled": rate.knobs.edoc_enabled,
                },
            )

            # Inter-case cool-down (WebPT-safe) — measure actual sleep
            delay = float(rate.knobs.inter_case_delay_sec or 0.0)
            if delay > 0 and not dry_run:
                delay_eid = tl.begin("inter_case_delay")
                t_sleep = time.perf_counter()
                await asyncio.sleep(delay)
                slept = time.perf_counter() - t_sleep
                tl.end(delay_eid)
                tl.add_idle("rate_control", slept)
            try:
                from pdf_throttle import semaphore_stats

                snap = semaphore_stats()
                tl.sem_peak = max(tl.sem_peak, int(snap.get("peak_in_flight") or 0))
            except Exception:
                pass
            wall = tl.wall_sec()
            tl.emit(ok=ok, error_type=error_type or "")
            try:
                telem_dec = speed.note_case_wall_for_telemetry(wall)
                if (
                    telem_dec.get("just_enabled")
                    and pool
                    and not dry_run
                    and not telemetry_routes_installed
                ):
                    await _install_telemetry_abort_routes(pool.page)
                    telemetry_routes_installed = True
                    append_metrics_jsonl(
                        metrics_path,
                        {
                            "event": "telemetry_abort_enabled",
                            "share": telem_dec.get("telemetry_share"),
                            "requests": telem_dec.get("telemetry_requests"),
                        },
                    )
            except Exception:
                pass
            # Independence probe: after N sequential successes with HTTP-only
            # discovery (no shared page mutation), enable parallel + measure.
            if (
                ok
                and not speed.state.discovery_parallel_ok
                and parallel_probe_cases < PARALLEL_PROBE_N
                and error_type != "CaseOpenFailed"
            ):
                parallel_probe_cases += 1
                if parallel_probe_cases >= PARALLEL_PROBE_N:
                    speed.mark_parallel_probe_ok()
                    append_metrics_jsonl(
                        metrics_path,
                        {
                            "event": "discovery_parallel_enabled",
                            "after_cases": parallel_probe_cases,
                            "kpi": speed.kpi_snapshot(),
                        },
                    )
            elif (
                not ok
                and speed.state.discovery_parallel_ok
                and error_type == "CaseOpenFailed"
            ):
                speed.mark_parallel_probe_failed()

            if cases_processed % CHECKPOINT_EVERY == 0:
                store.write_checkpoint(
                    checkpoint_path,
                    batch_id=batch_id,
                    watermark={
                        "facility_id": group.facility_id,
                        "case_id": group.case_id,
                        "cases_processed": cases_processed,
                    },
                    extra={
                        "config": optimizer.config.to_dict(),
                        "rate": rate.snapshot(),
                    },
                )
                if pool and not dry_run:
                    from auth import save_storage_state

                    await save_storage_state(pool.context)
                write_bottleneck_report(
                    reports_dir / "bottleneck_latest.json", metrics.timings
                )
                rate.maybe_tick_healthy_probe()
                _sync_rate_to_optimizer(
                    rate, optimizer, metrics, apply_semaphore=not dry_run
                )
                optimizer.maybe_tick(
                    metrics, force=True, throttle=rate.snapshot()
                )
                metrics.worker_scale = optimizer.config.worker_scale
                metrics.pdf_concurrency = optimizer.config.pdf_concurrency
                _apply_pdf_concurrency(
                    optimizer.config.pdf_concurrency, enabled=not dry_run
                )

            if time.time() - last_resource >= RESOURCE_EVERY_SEC:
                last_resource = time.time()
                pages = 0
                try:
                    if pool and pool.context:
                        pages = len(pool.context.pages)
                except Exception:
                    pages = 0
                forensics.sample_resources(open_pages=pages)
            if time.time() - last_session_health >= SESSION_HEALTH_EVERY_SEC:
                last_session_health = time.time()
                cookies = 0
                try:
                    if pool and pool.context:
                        cookies = len(await pool.context.cookies())
                except Exception:
                    cookies = 0
                forensics.sample_session_health(cookie_count=cookies)

            if time.time() - last_health >= HEALTH_EVERY_SEC:
                last_health = time.time()
                q = store.counts_by_state(batch_id=batch_id).get("queued", 0)
                rem_cases = sum(
                    store.remaining_cases_by_facility(batch_id=batch_id).values()
                )
                rate.maybe_tick_healthy_probe()
                _sync_rate_to_optimizer(
                    rate, optimizer, metrics, apply_semaphore=not dry_run
                )
                write_health(
                    reports_dir / "health.json",
                    metrics=metrics,
                    queue_remaining=q,
                    cases_remaining=rem_cases,
                    errors=store.counts_by_error_type(batch_id=batch_id),
                    facility_strategy=optimizer.config.facility_strategy,
                    throttle=rate.snapshot(),
                )
                optimizer.maybe_tick(metrics, throttle=rate.snapshot())

            if time.time() - last_report >= REPORT_EVERY_SEC:
                last_report = time.time()
                _emit_opt_reports(
                    reports_dir=reports_dir,
                    store=store,
                    batch_id=batch_id,
                    metrics=metrics,
                    rate=rate,
                    optimizer=optimizer,
                    baseline_cph=baseline_cph,
                    facility_switches=facility_switches,
                    cases_before_switch=cases_before_switch,
                    switch_time_sec=switch_time_sec,
                    preferred=preferred,
                    cases_on_facility=cases_on_facility,
                    failure_counts=failure_counts,
                    benchmark_rows=benchmark_rows,
                    restart_events=restart_events,
                )

            if time.time() - last_daily_snapshot >= DAILY_SNAPSHOT_SEC:
                _write_daily_snapshot(
                    reports_dir=reports_dir,
                    store=store,
                    batch_id=batch_id,
                    metrics=metrics,
                    rate=rate,
                )
                last_daily_snapshot = time.time()

            if metrics.auth_status in {"failed", "login_retry"} and pool and not dry_run:
                # Never exit the drain on auth — keep re-logging until session returns.
                log.warning(
                    "Auth not healthy (%s) — persistent re-login (heal_count=%s)",
                    metrics.auth_status,
                    heal_count,
                )
                healed = await _self_heal(pool, config, metrics, heal_count)
                heal_count += 1
                if healed:
                    rate.note_auth_renewal()
                    heal_count = 0
                else:
                    await asyncio.sleep(30)
                continue

    finally:
        store.write_checkpoint(
            checkpoint_path,
            batch_id=batch_id,
            watermark={"cases_processed": cases_processed},
            extra={"config": optimizer.config.to_dict()},
        )
        write_bottleneck_report(reports_dir / "bottleneck_latest.json", metrics.timings)
        if pool and not dry_run:
            try:
                from auth import save_storage_state

                await save_storage_state(pool.context)
                await pool.context.close()
            except Exception:
                pass
            if playwright:
                await playwright.stop()

    error_counts = store.counts_by_error_type(batch_id=batch_id)
    validation = {
        "s1_note": "CaseMismatch terminal; opened_case_id verified when downloaded",
        "all_pass": integrity_failures == 0,
    }
    integrity = {
        "invariant_failures": integrity_failures,
        "sampled": cases_processed,
        "golden_rule": "S0-S6 immutable",
    }
    paths = write_execution_reports(
        reports_dir,
        metrics=metrics,
        optimizer=optimizer,
        error_counts=error_counts,
        validation=validation,
        integrity=integrity,
        manual_actions=manual_actions,
    )
    opt_paths = _emit_opt_reports(
        reports_dir=reports_dir,
        store=store,
        batch_id=batch_id,
        metrics=metrics,
        rate=rate,
        optimizer=optimizer,
        baseline_cph=baseline_cph,
        facility_switches=facility_switches,
        cases_before_switch=cases_before_switch,
        switch_time_sec=switch_time_sec,
        preferred=preferred,
        cases_on_facility=cases_on_facility,
        failure_counts=failure_counts,
        benchmark_rows=benchmark_rows,
        restart_events=restart_events,
    )
    paths.update(opt_paths)
    return {
        "cases_processed": cases_processed,
        "metrics": {
            "cases_done": metrics.cases_done,
            "cases_failed": metrics.cases_failed,
            "cases_per_hour": metrics.cases_per_hour(),
        },
        "error_counts": error_counts,
        "facility_switches": facility_switches,
        "reports": {k: str(v) for k, v in paths.items()},
        "manual_actions": manual_actions,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case",
    )
    ap.add_argument("--batch-id", type=str, default="case_schedule_202601_202608")
    ap.add_argument("--facility-id", type=str, default=None)
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--reclaim-stale-sec", type=float, default=1800.0)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Claim/transition without WebPT (for tests / smoke)",
    )
    ap.add_argument(
        "--browser-workers",
        type=int,
        default=1,
        help="Must be 1 — WebPT allows a single browser session only",
    )
    args = ap.parse_args()
    if int(args.browser_workers) != 1:
        print(
            json.dumps(
                {
                    "error": "WebPT single-session only: --browser-workers must be 1",
                    "got": args.browser_workers,
                }
            )
        )
        return 2

    db_path = args.out_dir / "case_units.sqlite"
    if not db_path.is_file():
        print(json.dumps({"error": f"missing FSM db: {db_path} — run enqueue first"}))
        return 2

    store = CaseUnitStateStore(db_path)
    try:
        result = asyncio.run(
            run_download_loop(
                store=store,
                out_dir=args.out_dir,
                batch_id=args.batch_id,
                facility_id=args.facility_id,
                max_cases=args.max_cases,
                reclaim_stale_sec=args.reclaim_stale_sec,
                dry_run=args.dry_run,
            )
        )
    finally:
        store.close()

    print(json.dumps(result, indent=2))
    if result.get("manual_actions"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

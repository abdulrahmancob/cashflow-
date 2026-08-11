"""Stage 1 — Acquire: all scrapers / source pulls."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
from pathlib import Path
from typing import Any

from cashflow_ops.adapters import case_pipeline, revflow, snowflake, waystar, webpt
from cashflow_ops.config import CASE_PIPELINE_DIR, REVFLOW_OUTPUT, WEBPT_OUTPUT
from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult

log = logging.getLogger(__name__)


def _env_blank(*keys: str) -> bool:
    """True when any required env key is missing/blank."""
    for key in keys:
        if not (os.getenv(key) or "").strip():
            return True
    return False


def _env_truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _cases_remaining() -> int | None:
    """Read drain health.json; None when file missing/unreadable."""
    health = Path(CASE_PIPELINE_DIR) / "reports" / "health.json"
    if not health.is_file():
        return None
    try:
        data = json.loads(health.read_text(encoding="utf-8"))
        return int(data.get("cases_remaining") or 0)
    except Exception:  # noqa: BLE001
        return None


def _should_skip_case_download() -> tuple[bool, str]:
    if _env_truthy("CASHFLOW_OPS_SKIP_CASE_DOWNLOAD"):
        return True, "CASHFLOW_OPS_SKIP_CASE_DOWNLOAD=1"
    remaining = _cases_remaining()
    # Align with nightly_pipeline drain gate: when remaining is tiny/zero the
    # long-running case pack is effectively done; do not re-download nightly.
    if remaining is not None and remaining <= 500:
        return True, f"cases_remaining={remaining}"
    return False, ""


class AcquireStage:
    key = "acquire"
    requires: list[str] = []
    produces = [
        "schedule_export",
        "case_downloads",
        "revflow_exports",
        "waystar_exports",
        "patient_payments",
        "snowflake_kpi",
        "mail_checks",
        "audit_prep",
    ]
    on_failure = FailurePolicy.STOP
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        from cashflow_ops import events, maintenance

        start = ctx.window_start.isoformat()
        end = ctx.window_end.isoformat()
        dry = ctx.dry_run
        skip = ctx.skip_scrapers
        outputs: dict[str, Any] = {
            "window_start": start,
            "window_end": end,
        }
        artifacts: list[ArtifactSpec] = []
        alerts: list[dict[str, Any]] = []
        failures: list[str] = []

        skip_map = {} if dry else maintenance.resolve_skip_systems()
        outputs["maintenance"] = skip_map
        skip_webpt = skip or bool(skip_map.get("webpt", {}).get("skip"))
        skip_revflow = skip or bool(skip_map.get("revflow", {}).get("skip"))
        skip_waystar = skip or bool(skip_map.get("waystar", {}).get("skip"))
        skip_snow = skip or bool(skip_map.get("snowflake", {}).get("skip"))

        # Soft-skip scrapers when credentials are not wired into the container.
        # Missing secrets must not hard-fail Acquire (worker pass still loads warehouse).
        cred_skips: list[tuple[str, tuple[str, ...]]] = [
            ("webpt", ("WEBPT_USERNAME", "WEBPT_PASSWORD")),
            ("revflow", ("REVFLOW_USERNAME", "REVFLOW_PASSWORD")),
            ("waystar", ("WAYSTAR_USER", "WAYSTAR_PASS")),
            ("snowflake", ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD")),
        ]
        for sys_key, keys in cred_skips:
            if skip:
                break
            if _env_blank(*keys):
                skip_map[sys_key] = {
                    "skip": True,
                    "reason": "credentials_missing",
                    "keys": list(keys),
                }
                if sys_key == "webpt":
                    skip_webpt = True
                elif sys_key == "revflow":
                    skip_revflow = True
                elif sys_key == "waystar":
                    skip_waystar = True
                elif sys_key == "snowflake":
                    skip_snow = True

        for sys_key, info in skip_map.items():
            if info.get("skip"):
                alerts.append(
                    {
                        "severity": "warning",
                        "alert_key": f"{sys_key}_maintenance",
                        "message": f"{sys_key} skipped: {info.get('reason')}",
                        "payload": info,
                    }
                )
                events.emit_event(
                    ctx.run_id,
                    event_key=f"{sys_key}_maintenance",
                    stage_key="acquire",
                    message=f"{sys_key} skipped: {info.get('reason')}",
                    severity="warning",
                    entity_key=f"system={sys_key}",
                    payload=info,
                )

        # --- WebPT exclusive session path (serial) ---
        sched = webpt.export_schedule(
            start=start, end=end, dry_run=dry, skip=skip_webpt
        )
        outputs["webpt_schedule"] = sched.to_dict()
        if not sched.ok and not skip_webpt:
            failures.append(f"webpt schedule: {sched.stderr[-500:]}")

        checkouts = webpt.export_checkouts(dry_run=dry, skip=skip_webpt)
        outputs["webpt_checkouts"] = checkouts.to_dict()
        if not checkouts.ok:
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "webpt_checkouts_failed",
                    "message": checkouts.stderr[-500:] or "export-checkouts failed",
                }
            )

        payments = webpt.scrape_patient_payments(dry_run=dry, skip=skip_webpt)
        outputs["patient_payments"] = payments.to_dict()
        if not payments.ok:
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "patient_payments_failed",
                    "message": payments.stderr[-500:] or "patient payments failed",
                }
            )

        # Build + download cases (still WebPT session). Skip when drain is done
        # or explicitly disabled — nightly should not re-download the full case pack.
        sched_matches = sorted(WEBPT_OUTPUT.glob("schedule_visits_*.csv"))
        schedule_csv = sched_matches[-1] if sched_matches else WEBPT_OUTPUT / "schedule_visits.csv"
        skip_case_dl, skip_case_reason = _should_skip_case_download()
        skip_cases = skip_webpt or skip_case_dl
        if skip_case_dl and not skip_webpt:
            alerts.append(
                {
                    "severity": "info",
                    "alert_key": "case_download_skipped",
                    "message": f"case download skipped: {skip_case_reason}",
                }
            )
            log.info("case download skipped: %s", skip_case_reason)

        build = case_pipeline.build_case_schedule(
            schedule_export=schedule_csv,
            start=start,
            end=end,
            dry_run=dry,
            skip=skip_cases,
        )
        outputs["case_schedule_build"] = build.to_dict()
        if not build.ok and not skip_cases:
            failures.append(f"case schedule build: {build.stderr[-500:]}")

        download = case_pipeline.run_case_download(
            schedule_export=schedule_csv,
            start=start,
            end=end,
            dry_run=dry,
            skip=skip_cases,
            phase="download",
        )
        outputs["case_download"] = download.to_dict()
        outputs["case_download_skipped"] = skip_cases
        if not download.ok and not skip_cases:
            failures.append(f"case download: {download.stderr[-500:]}")

        # --- Parallel non-WebPT acquires ---
        parallel_results: dict[str, Any] = {}

        def _revflow() -> dict:
            return revflow.discover_and_export(
                from_date=start, to_date=end, dry_run=dry, skip=skip_revflow
            )

        def _waystar_rej():
            return waystar.scrape_rejected(
                trans_from=start, trans_to=end, dry_run=dry, skip=skip_waystar
            )

        def _waystar_den():
            return waystar.scrape_denials(dry_run=dry, skip=skip_waystar)

        def _snow():
            return snowflake.pull_kpi(start=start, end=end, dry_run=dry, skip=skip_snow)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futs = {
                "revflow": pool.submit(_revflow),
                "waystar_rejected": pool.submit(_waystar_rej),
                "waystar_denials": pool.submit(_waystar_den),
                "snowflake": pool.submit(_snow),
            }
            for name, fut in futs.items():
                try:
                    parallel_results[name] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    parallel_results[name] = {"error": str(exc)}
                    failures.append(f"{name}: {exc}")

        rf = parallel_results.get("revflow") or {}
        outputs["revflow"] = {
            k: (v.to_dict() if hasattr(v, "to_dict") else v) for k, v in rf.items()
        } if isinstance(rf, dict) else rf
        revflow_step_failures: list[str] = []
        for step_name, step_res in (rf.items() if isinstance(rf, dict) else []):
            if hasattr(step_res, "ok") and not step_res.ok and not step_res.skipped:
                revflow_step_failures.append(
                    f"revflow.{step_name}: {step_res.stderr[-300:]}"
                )
        # If RevFlow browser/IP fails but prior exports exist, keep Acquire alive so
        # warehouse/reconcile/forecast can still run from the latest files on disk.
        prior_rf = revflow.count_exports()
        if revflow_step_failures:
            if prior_rf > 0:
                alerts.append(
                    {
                        "severity": "warning",
                        "alert_key": "revflow_failed_using_prior_exports",
                        "message": (
                            f"RevFlow scrape failed; using {prior_rf} existing export files. "
                            + "; ".join(revflow_step_failures)[:400]
                        ),
                    }
                )
                log.warning(
                    "RevFlow failed with %d prior exports retained", prior_rf
                )
            else:
                failures.extend(revflow_step_failures)

        for key in ("waystar_rejected", "waystar_denials", "snowflake"):
            res = parallel_results.get(key)
            if hasattr(res, "to_dict"):
                outputs[key] = res.to_dict()
                if not res.ok and not res.skipped:
                    # Waystar/Snowflake are important but allow continue with alert
                    # only if RevFlow+WebPT succeeded — still collect as soft fail
                    alerts.append(
                        {
                            "severity": "warning",
                            "alert_key": f"{key}_failed",
                            "message": res.stderr[-500:] or f"{key} failed",
                        }
                    )
            else:
                outputs[key] = res

        outputs["revflow_skipped"] = skip_revflow
        outputs["waystar_skipped"] = skip_waystar
        outputs["webpt_skipped"] = skip_webpt
        outputs["snowflake_skipped"] = skip_snow
        outputs["mail_checks"] = {"status": "ingest_path_ready"}
        outputs["audit_prep"] = {"status": "ready"}

        # Only hard-fail WebPT/RevFlow when not in maintenance skip
        if skip_webpt or skip_cases:
            failures = [f for f in failures if not f.startswith(("webpt", "case "))]
        if skip_revflow:
            failures = [f for f in failures if not f.startswith("revflow")]

        rf_files = revflow.count_exports()
        artifacts.extend(
            [
                ArtifactSpec(
                    key="schedule_export",
                    uri=str(schedule_csv),
                    payload={"window": [start, end]},
                ),
                ArtifactSpec(
                    key="revflow_exports",
                    uri=str(REVFLOW_OUTPUT / "exports"),
                    row_count=rf_files,
                ),
                ArtifactSpec(
                    key="case_downloads",
                    uri=str(CASE_PIPELINE_DIR / "cases"),
                ),
                ArtifactSpec(key="patient_payments", uri=str(WEBPT_OUTPUT)),
                ArtifactSpec(key="snowflake_kpi", payload={"role": "kpi_only"}),
                ArtifactSpec(key="waystar_exports", payload=waystar.count_waystar_outputs()),
                ArtifactSpec(key="mail_checks", payload={"status": "path_ready"}),
                ArtifactSpec(key="audit_prep", payload={"status": "ready"}),
            ]
        )

        # Critical: WebPT schedule or RevFlow hard-fail stops Acquire
        if failures and any(f.startswith(("webpt schedule", "case ", "revflow")) for f in failures):
            return StageResult.failed("; ".join(failures), outputs=outputs)

        return StageResult.success(
            outputs=outputs,
            artifacts=artifacts,
            alerts=alerts,
            warnings=failures,
        )

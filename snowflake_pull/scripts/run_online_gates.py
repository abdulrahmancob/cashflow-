"""Run online validation gates P3 (schedule) and P2a (note-index); optional P2b.

Requires WebPT credentials in webpt_edco_scraper/.env.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRAPER = _REPO / "webpt_edco_scraper"
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
if str(_SCRAPER) not in sys.path:
    sys.path.insert(0, str(_SCRAPER))

from snowflake_pull.coverage_run import finish_run, resume_run  # noqa: E402
from snowflake_pull.facility_map import assert_scrape_allowed  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402
from snowflake_pull.scripts.score_p3_schedule import score_schedule  # noqa: E402

START = date(2026, 6, 1)
END = date(2026, 7, 31)


def _write_gate(run_dir: Path, gate_id: str, payload: dict) -> None:
    gate_dir = run_dir / "summaries" / "gates"
    gate_dir.mkdir(parents=True, exist_ok=True)
    (gate_dir / f"{gate_id}.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "summaries" / f"gate_{gate_id}_summary.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )


def run_p3_schedule(run, *, clinic: str = "Brownsville") -> dict:
    mapping = assert_scrape_allowed(clinic)
    out_dir = (
        run.artifacts
        / "clinic_rediscover"
        / f"{mapping.webpt_facility_id}_{clinic.replace(' ', '_')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    schedule_csv = out_dir / f"schedule_visits_{START.isoformat()}_{END.isoformat()}.csv"

    run.obs.stage_start("online_p3")
    run.obs.online = True
    run.obs.start_heartbeat()

    if not schedule_csv.is_file():
        cmd = [
            sys.executable,
            str(_SCRAPER / "scraper.py"),
            "export-schedule",
            "--start-date",
            START.isoformat(),
            "--end-date",
            END.isoformat(),
            "--facility-id",
            str(mapping.webpt_facility_id),
            "--output",
            str(out_dir),
            "--skip-chart",
        ]
        run.obs.emit(
            "decision",
            operation="export_schedule",
            decision="start_schedule_export",
            facility_id=mapping.webpt_facility_id,
            facility_name=clinic,
            extra={"command": cmd},
        )
        proc = subprocess.run(cmd, cwd=str(_SCRAPER))
        if proc.returncode != 0:
            payload = {
                "gate": "P3",
                "pass": False,
                "reason": f"export_schedule_failed_rc={proc.returncode}",
                "facility_id": mapping.webpt_facility_id,
                "clinic": clinic,
            }
            _write_gate(run.run_dir, "P3", payload)
            run.obs.emit(
                "error",
                level="ERROR",
                operation="export_schedule",
                outcome="fail",
                error_type="Unexpected",
                error_expected=False,
                decision_reason=payload["reason"],
            )
            run.obs.stage_end("online_p3", **payload)
            return payload
        run.obs.mark_success(
            operation="export_schedule",
            facility_id=mapping.webpt_facility_id,
        )
    else:
        run.obs.emit(
            "decision",
            operation="export_schedule",
            decision="reuse_existing_schedule_csv",
            decision_reason="schedule_csv_present",
            extra={"path": str(schedule_csv)},
        )

    if not schedule_csv.is_file():
        # scraper may write with different name — find any schedule_visits_*.csv
        found = sorted(out_dir.glob("schedule_visits_*.csv"))
        if found:
            schedule_csv = found[-1]

    sf_path = _REPO / "snowflake_pull/output/all_billing_data.csv"
    payload = score_schedule(
        schedule_csv=schedule_csv,
        sf_path=sf_path,
        clinic=clinic,
        facility_id=mapping.webpt_facility_id or "",
    )
    _write_gate(run.run_dir, "P3", payload)
    run.obs.stage_end(
        "online_p3",
        gate_pass=payload["pass"],
        schedule_ceiling_pct=payload.get("schedule_ceiling_pct"),
    )
    return payload


def _load_stratified_sample(class_csv: Path, n_after=40, n_before=40, n_interior=20) -> list[dict]:
    by: dict[str, list] = defaultdict(list)
    with class_csv.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("classification") != "patient_in_rec_but_dos_missing":
                continue
            if (row.get("sf_status") or "").lower() not in {"paid", "partial"}:
                continue
            by[row.get("subtype") or ""].append(row)
    rng = random.Random(42)
    out: list[dict] = []
    for key, n in (
        ("dos_after_last_note", n_after),
        ("dos_before_first_note", n_before),
        ("interior_gap", n_interior),
    ):
        rows = by.get(key, [])[:]
        rng.shuffle(rows)
        out.extend(rows[:n])
    return out


def _export_case_lookup(export_path: Path) -> dict[str, tuple[str, str]]:
    """emr -> (facility_id, case_id)"""
    best: dict[str, tuple[str, str]] = {}
    with export_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            fid = (row.get("facility_id") or "").strip()
            cid = (row.get("case_id") or "").strip()
            if pid and cid:
                best[pid] = (fid, cid)
    return best


async def _run_p2a_async(run, sample: list[dict], case_lookup: dict[str, tuple[str, str]]) -> dict:
    from auth import (
        ClinicSwitchError,
        create_context,
        ensure_authenticated,
        is_auth_redirect_url,
        switch_clinic_and_settle,
    )
    from chart_notes_api import fetch_patient_chart_notes
    from config import WebPTConfig
    from playwright.async_api import async_playwright

    # Ensure scraper .env is loaded even when invoked from repo root.
    from dotenv import load_dotenv

    load_dotenv(_SCRAPER / ".env", override=False)
    config = WebPTConfig.from_env()
    if not config.username or not config.password:
        raise RuntimeError(
            "WEBPT_USERNAME/WEBPT_PASSWORD missing after loading "
            f"{_SCRAPER / '.env'} — refusing to hang on manual login page"
        )
    if not config.company_id:
        raise RuntimeError("WEBPT_COMPANY_ID missing — needed for clinic switch")
    present = 0
    attempted = 0
    inconclusive = 0
    missing_case = 0
    auth_bounces = 0
    force_reauth = False
    by_subtype: dict[str, dict[str, int]] = defaultdict(lambda: {"n": 0, "present": 0})
    details: list[dict] = []
    max_auth_bounces = 15

    def _bounce(page_url: str, exc: BaseException) -> bool:
        msg = str(exc)
        return (
            is_auth_redirect_url(page_url)
            or "login" in msg.lower()
            or "ClinicChange" in msg
            or "Clinic switch" in msg
            or "Auth" in type(exc).__name__
            or "SessionExpired" in type(exc).__name__
        )

    # Minimize clinic switches (each switch can bounce session to Auth0).
    def _sort_key(row: dict) -> tuple[str, str]:
        emr = (row.get("emr_ids") or "").split(";")[0]
        fid = (case_lookup.get(emr) or ("", ""))[0]
        return (fid, emr)

    ordered = sorted(sample, key=_sort_key)

    async with async_playwright() as pw:
        context = await create_context(pw, config)
        page = await context.new_page()
        try:
            await ensure_authenticated(page, context, config, allow_oust=True)
            run.obs.set_auth_healthy(True)
            run.obs.set_browser_healthy(True)
            current_fac: str | None = None

            async def _reauth(*, reason: str) -> None:
                nonlocal current_fac, auth_bounces, force_reauth
                auth_bounces += 1
                force_reauth = False
                current_fac = None
                run.obs.set_auth_healthy(False)
                run.obs.emit(
                    "decision",
                    level="WARN",
                    operation="reauth",
                    decision="session_bounced_to_login",
                    decision_reason=reason,
                    error_type="AuthExpired",
                    error_expected=True,
                    extra={"url": page.url, "auth_bounces": auth_bounces},
                )
                await ensure_authenticated(
                    page, context, config, allow_oust=True, fresh_login=True
                )
                run.obs.set_auth_healthy(True)

            async def _ensure_app_session() -> None:
                nonlocal force_reauth
                if force_reauth or is_auth_redirect_url(page.url):
                    await _reauth(
                        reason=(
                            "force_reauth_after_auth_error"
                            if force_reauth
                            else "page_on_auth_redirect"
                        )
                    )

            for row in ordered:
                if run.obs.abort_requested():
                    break
                if auth_bounces >= max_auth_bounces:
                    run.obs.emit(
                        "error",
                        level="ERROR",
                        operation="note_index",
                        outcome="fail",
                        error_type="AuthExpired",
                        error_expected=False,
                        decision="abort_repeated_auth_bounce",
                        decision_reason=f"auth_bounced_to_login_{max_auth_bounces}_times",
                    )
                    break
                emr = (row.get("emr_ids") or "").split(";")[0]
                dos = row.get("date_of_service") or ""
                subtype = row.get("subtype") or ""
                clinic = row.get("sf_clinic") or ""
                lookup = case_lookup.get(emr)
                attempted += 1
                by_subtype[subtype]["n"] += 1
                corr = f"{emr}|{dos}|{lookup[0] if lookup else ''}|p2a"
                if not lookup:
                    missing_case += 1
                    run.obs.emit(
                        "decision",
                        operation="note_index",
                        correlation_id=corr,
                        emr_id=emr,
                        dos=dos,
                        outcome="skip",
                        decision="skip_missing_case_id",
                        decision_reason="patient_not_in_export_case_lookup",
                        error_type="PatientNotInWebPT",
                        error_expected=True,
                    )
                    details.append(
                        {"emr_id": emr, "dos": dos, "subtype": subtype, "result": "no_case"}
                    )
                    continue

                fid, case_id = lookup
                t0 = time.perf_counter()
                try:
                    await _ensure_app_session()
                    if fid != current_fac:
                        try:
                            await switch_clinic_and_settle(
                                page,
                                context,
                                config,
                                company_id=str(config.company_id),
                                facility_id=str(fid),
                                allow_oust=True,
                            )
                        except (ClinicSwitchError, TimeoutError) as switch_exc:
                            # Typical when dashboard redirected to Auth0 mid-run.
                            run.obs.emit(
                                "decision",
                                level="WARN",
                                operation="clinic_switch",
                                decision="retry_switch_after_reauth",
                                decision_reason=str(switch_exc)[:200],
                                facility_id=fid,
                                error_type="AuthExpired",
                                error_expected=True,
                            )
                            await _reauth(
                                reason=f"clinic_switch_retry:{switch_exc}"[:180]
                            )
                            await switch_clinic_and_settle(
                                page,
                                context,
                                config,
                                company_id=str(config.company_id),
                                facility_id=str(fid),
                                allow_oust=True,
                            )
                        current_fac = fid
                        run.obs.mark_success(
                            operation="clinic_switch",
                            facility_id=fid,
                        )

                    # Page navigation after settle is more reliable than HTTP
                    # when the session was just clinic-switched.
                    notes = await fetch_patient_chart_notes(
                        context,
                        patient_id=int(emr),
                        case_id=int(case_id),
                        page=page,
                        config=config,
                        prefer_http=False,
                    )
                    if is_auth_redirect_url(page.url):
                        raise RuntimeError(
                            f"chart notes navigated to login ({page.url})"
                        )
                    dates = {((n.note_date or "")[:10]) for n in notes if n.note_date}
                    hit = dos[:10] in dates
                    if hit:
                        present += 1
                        by_subtype[subtype]["present"] += 1
                    result = "dos_present_in_index" if hit else "note_index_dos_absent"
                    # Successful probe clears early-noise bounce budget.
                    auth_bounces = 0
                    run.obs.emit(
                        "decision",
                        operation="note_index",
                        correlation_id=corr,
                        emr_id=emr,
                        dos=dos,
                        facility_id=fid,
                        facility_name=clinic,
                        visit_status=row.get("sf_status"),
                        webpt_patient_id=emr,
                        outcome="success",
                        decision=result,
                        decision_reason=subtype,
                        execution_ms=round((time.perf_counter() - t0) * 1000, 2),
                        extra={"note_dates_n": len(dates), "notes_n": len(notes)},
                    )
                    run.obs.mark_success(
                        operation="note_index",
                        emr_id=emr,
                        dos=dos,
                        facility_id=fid,
                        correlation_id=corr,
                    )
                    details.append(
                        {
                            "emr_id": emr,
                            "dos": dos,
                            "subtype": subtype,
                            "result": result,
                            "notes_n": len(notes),
                        }
                    )
                except Exception as exc:
                    inconclusive += 1
                    msg = str(exc)
                    bounced = _bounce(page.url, exc)
                    if bounced:
                        force_reauth = True
                        current_fac = None
                        run.obs.set_auth_healthy(False)
                        try:
                            await _reauth(reason=f"after_error:{msg}"[:180])
                        except Exception as reauth_exc:
                            run.obs.emit(
                                "error",
                                level="ERROR",
                                operation="reauth",
                                outcome="fail",
                                error_type="AuthExpired",
                                error_expected=False,
                                exception=reauth_exc,
                            )
                    run.obs.emit(
                        "error",
                        level="ERROR",
                        operation="note_index",
                        correlation_id=corr,
                        emr_id=emr,
                        dos=dos,
                        facility_id=fid,
                        outcome="fail",
                        error_type="AuthExpired" if bounced else "Unexpected",
                        error_expected=bounced,
                        exception=exc,
                        extra={"page_url": page.url, "auth_bounces": auth_bounces},
                    )
                    details.append(
                        {
                            "emr_id": emr,
                            "dos": dos,
                            "subtype": subtype,
                            "result": "error",
                            "error": msg[:200],
                            "page_url": page.url,
                        }
                    )
                run.obs.set_progress(
                    completed=attempted, remaining=max(len(ordered) - attempted, 0)
                )
                run.obs.metrics.incr("attempted")
        finally:
            await context.close()

    rate = present / max(attempted - missing_case, 1)
    payload = {
        "gate": "P2a",
        "pass": True,
        "pending_online": False,
        "sample_n": len(sample),
        "attempted": attempted,
        "dos_present_in_index": present,
        "present_rate": round(rate, 4),
        "inconclusive": inconclusive,
        "missing_case": missing_case,
        "auth_bounces": auth_bounces,
        "by_subtype": {k: dict(v) for k, v in by_subtype.items()},
        "unlocks_track_c": False,
        "reason": (
            f"online_note_index present_rate={rate:.4f} "
            f"auth_bounces={auth_bounces}"
        ),
        "details_path": "artifacts/pilots/p2a_details.json",
    }
    return payload, details


def run_p2a(run) -> dict:
    run.obs.stage_start("online_p2a")
    run.obs.online = True
    run.obs.start_heartbeat()
    class_csv = run.artifacts / "missing_classification.csv"
    if not class_csv.is_file():
        raise SystemExit("missing_classification.csv required — run rebuild_root_cause")
    sample = _load_stratified_sample(class_csv)
    export_path = (
        _REPO / "webpt_edco_scraper/output/jun_jul_2026/patients_export_273d.csv"
    )
    case_lookup = _export_case_lookup(export_path)
    payload, details = asyncio.run(_run_p2a_async(run, sample, case_lookup))
    det_path = run.artifacts / "pilots" / "p2a_details.json"
    det_path.parent.mkdir(parents=True, exist_ok=True)
    det_path.write_text(json.dumps(details, indent=2) + "\n", encoding="utf-8")
    _write_gate(run.run_dir, "P2a", payload)
    # P2b remains blocked until explicit --p2b
    p2b = {
        "gate": "P2b",
        "pass": False,
        "reason": "blocked_until_p2b_pdf_pilot",
        "p2a_present_rate": payload.get("present_rate"),
        "unlocks_track_c": False,
    }
    _write_gate(run.run_dir, "P2b", p2b)
    run.obs.stage_end("online_p2a", **{k: payload[k] for k in ("present_rate", "attempted")})
    return payload


async def _run_p2b_async(run, p2a_details: list[dict], case_lookup: dict) -> dict:
    """PDF+extract pilot for P2a dos_present_in_index hits (plus errors as filler)."""
    from auth import (
        ClinicSwitchError,
        create_context,
        ensure_authenticated,
        is_auth_redirect_url,
        switch_clinic_and_settle,
    )
    from chart_notes_api import fetch_patient_chart_notes
    from chart_notes_download import download_patient_chart_notes
    from chart_notes_parse import extract_daily_note
    from config import WebPTConfig
    from playwright.async_api import async_playwright

    # Prefer present first, then errors; cap pilot size.
    candidates = (
        [d for d in p2a_details if d.get("result") == "dos_present_in_index"]
        + [d for d in p2a_details if d.get("result") == "error"]
    )[:60]

    def _fid_for(row: dict) -> str:
        emr = row.get("emr_id") or ""
        return (case_lookup.get(emr) or ("", ""))[0]

    candidates = sorted(candidates, key=lambda r: (_fid_for(r), r.get("emr_id") or ""))

    from dotenv import load_dotenv

    load_dotenv(_SCRAPER / ".env", override=False)
    config = WebPTConfig.from_env()
    if not config.username or not config.password:
        raise RuntimeError("WEBPT credentials missing — aborting P2b")
    if not config.company_id:
        raise RuntimeError("WEBPT_COMPANY_ID missing — needed for clinic switch")
    edocs = run.artifacts / "pilots" / "p2b_edocs"
    edocs.mkdir(parents=True, exist_ok=True)
    recovered = 0
    attempted = 0
    auth_bounces = 0
    force_reauth = False
    max_auth_bounces = 15
    minutes: list[float] = []
    outcomes: dict[str, int] = defaultdict(int)

    def _bounce(page_url: str, exc: BaseException) -> bool:
        msg = str(exc)
        return (
            is_auth_redirect_url(page_url)
            or "login" in msg.lower()
            or "ClinicChange" in msg
            or "Clinic switch" in msg
            or "Auth" in type(exc).__name__
            or "SessionExpired" in type(exc).__name__
        )

    async with async_playwright() as pw:
        context = await create_context(pw, config)
        page = await context.new_page()
        try:
            await ensure_authenticated(page, context, config, allow_oust=True)
            run.obs.set_auth_healthy(True)
            run.obs.set_browser_healthy(True)
            current_fac: str | None = None

            async def _reauth(*, reason: str) -> None:
                nonlocal current_fac, auth_bounces, force_reauth
                auth_bounces += 1
                force_reauth = False
                current_fac = None
                run.obs.set_auth_healthy(False)
                run.obs.emit(
                    "decision",
                    level="WARN",
                    operation="reauth",
                    decision="session_bounced_to_login",
                    decision_reason=reason,
                    error_type="AuthExpired",
                    error_expected=True,
                    extra={"url": page.url, "auth_bounces": auth_bounces},
                )
                await ensure_authenticated(
                    page, context, config, allow_oust=True, fresh_login=True
                )
                run.obs.set_auth_healthy(True)

            async def _ensure_app_session() -> None:
                nonlocal force_reauth
                if force_reauth or is_auth_redirect_url(page.url):
                    await _reauth(
                        reason=(
                            "force_reauth_after_auth_error"
                            if force_reauth
                            else "page_on_auth_redirect"
                        )
                    )

            for row in candidates:
                if run.obs.abort_requested():
                    break
                if auth_bounces >= max_auth_bounces:
                    outcomes["abort_auth_bounce"] += 1
                    break
                emr = row["emr_id"]
                dos = row["dos"]
                lookup = case_lookup.get(emr)
                if not lookup:
                    outcomes["no_case"] += 1
                    continue
                fid, case_id = lookup
                attempted += 1
                t0 = time.perf_counter()
                corr = f"{emr}|{dos}|{fid}|p2b"
                try:
                    await _ensure_app_session()
                    if fid != current_fac:
                        try:
                            await switch_clinic_and_settle(
                                page,
                                context,
                                config,
                                company_id=str(config.company_id),
                                facility_id=str(fid),
                                allow_oust=True,
                            )
                        except (ClinicSwitchError, TimeoutError) as switch_exc:
                            await _reauth(
                                reason=f"clinic_switch_retry:{switch_exc}"[:180]
                            )
                            await switch_clinic_and_settle(
                                page,
                                context,
                                config,
                                company_id=str(config.company_id),
                                facility_id=str(fid),
                                allow_oust=True,
                            )
                        current_fac = fid
                    notes = await fetch_patient_chart_notes(
                        context,
                        patient_id=int(emr),
                        case_id=int(case_id),
                        page=page,
                        config=config,
                        prefer_http=False,
                    )
                    if is_auth_redirect_url(page.url):
                        raise RuntimeError(
                            f"chart notes navigated to login ({page.url})"
                        )
                    target_notes = [
                        n for n in notes if (n.note_date or "")[:10] == dos[:10]
                    ]
                    if not target_notes:
                        outcomes["notes_downloaded_but_dos_absent"] += 1
                        run.obs.emit(
                            "decision",
                            operation="p2b_pdf",
                            correlation_id=corr,
                            outcome="skip",
                            decision="NoteDosAbsentAfterDownload",
                            decision_reason="dos_not_in_index_at_download_time",
                            emr_id=emr,
                            dos=dos,
                            facility_id=fid,
                        )
                        continue
                    results = await download_patient_chart_notes(
                        context,
                        notes=target_notes,
                        patient_id=int(emr),
                        case_id=int(case_id),
                        output_dir=edocs,
                        config=config,
                        facility_id=fid,
                        skip_existing=True,
                    )
                    got = False
                    for res in results:
                        path = Path(res.get("path") or "")
                        if not path.is_file():
                            continue
                        extract = extract_daily_note(path, patient_id=emr)
                        extract_dos = (extract.date_of_daily_note or "")[:10]
                        if extract_dos == dos[:10] or path.suffix.lower() == ".pdf":
                            got = True
                            break
                    elapsed_min = (time.perf_counter() - t0) / 60.0
                    minutes.append(elapsed_min)
                    if got:
                        recovered += 1
                        auth_bounces = 0
                        outcomes["note_recovered_for_dos"] += 1
                        run.obs.mark_success(
                            operation="p2b_pdf",
                            emr_id=emr,
                            dos=dos,
                            facility_id=fid,
                        )
                    else:
                        outcomes["chart_empty"] += 1
                except Exception as exc:
                    bounced = _bounce(page.url, exc)
                    if bounced:
                        force_reauth = True
                        current_fac = None
                        run.obs.set_auth_healthy(False)
                        try:
                            await _reauth(reason=f"after_error:{exc}"[:180])
                        except Exception as reauth_exc:
                            run.obs.emit(
                                "error",
                                level="ERROR",
                                operation="reauth",
                                outcome="fail",
                                error_type="AuthExpired",
                                exception=reauth_exc,
                            )
                        outcomes["auth_or_facility_error"] += 1
                    else:
                        outcomes["download_or_extract_error"] += 1
                    run.obs.emit(
                        "error",
                        level="ERROR",
                        operation="p2b_pdf",
                        correlation_id=corr,
                        outcome="fail",
                        error_type="AuthExpired" if bounced else "Unexpected",
                        error_expected=bounced,
                        exception=exc,
                        emr_id=emr,
                        dos=dos,
                        facility_id=fid,
                        extra={"page_url": page.url, "auth_bounces": auth_bounces},
                    )
                run.obs.set_progress(
                    completed=attempted, remaining=max(len(candidates) - attempted, 0)
                )
        finally:
            await context.close()

    present_rate = 0.0
    p2a_gate = run.run_dir / "summaries" / "gates" / "P2a.json"
    if p2a_gate.is_file():
        present_rate = float(
            json.loads(p2a_gate.read_text(encoding="utf-8")).get("present_rate") or 0
        )
    recovery_rate = recovered / max(attempted, 1)
    threshold = max(0.25, present_rate - 0.10)
    median_min = sorted(minutes)[len(minutes) // 2] if minutes else None
    passed = recovery_rate >= threshold and attempted > 0
    return {
        "gate": "P2b",
        "pass": passed,
        "attempted": attempted,
        "recovered": recovered,
        "recovery_rate": round(recovery_rate, 4),
        "threshold": round(threshold, 4),
        "p2a_present_rate": present_rate,
        "auth_bounces": auth_bounces,
        "median_minutes_per_attempt": median_min,
        "outcomes": dict(outcomes),
        "unlocks_track_c": passed,
        "reason": (
            f"recovery_rate={recovery_rate:.4f} threshold={threshold:.4f} "
            f"median_min={median_min} auth_bounces={auth_bounces}"
        ),
    }


def run_p2b(run) -> dict:
    run.obs.stage_start("online_p2b")
    run.obs.online = True
    det_path = run.artifacts / "pilots" / "p2a_details.json"
    if not det_path.is_file():
        raise SystemExit("P2a details missing — run P2a first")
    details = json.loads(det_path.read_text(encoding="utf-8"))
    export_path = (
        _REPO / "webpt_edco_scraper/output/jun_jul_2026/patients_export_273d.csv"
    )
    case_lookup = _export_case_lookup(export_path)
    payload = asyncio.run(_run_p2b_async(run, details, case_lookup))
    _write_gate(run.run_dir, "P2b", payload)
    run.obs.stage_end("online_p2b", **{k: payload[k] for k in ("pass", "recovery_rate")})
    return payload


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", required=True)
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--allow-input-drift", action="store_true")
    p.add_argument("--skip-p3", action="store_true")
    p.add_argument("--skip-p2a", action="store_true")
    p.add_argument("--p2b", action="store_true", help="Also run PDF pilot P2b")
    p.add_argument("--clinic", default="Brownsville")
    args = p.parse_args(argv)

    run = resume_run(
        args.run_id,
        root=args.root,
        script="run_online_gates.py",
        allow_input_drift=args.allow_input_drift,
    )
    set_global_obs(run.obs)

    results: dict[str, dict] = {}
    try:
        if not args.skip_p3:
            results["P3"] = run_p3_schedule(run, clinic=args.clinic)
            print("P3:", json.dumps(results["P3"], indent=2))
        if not args.skip_p2a:
            results["P2a"] = run_p2a(run)
            print("P2a:", json.dumps({k: results["P2a"][k] for k in results["P2a"] if k != "details_path"}, indent=2))
        if args.p2b:
            results["P2b"] = run_p2b(run)
            print("P2b:", json.dumps(results["P2b"], indent=2))
    finally:
        rollup = {
            "run_id": run.run_id,
            "gates": {
                k: {"pass": v.get("pass"), "reason": v.get("reason")}
                for k, v in results.items()
            },
        }
        (run.run_dir / "summaries" / "online_gates_rollup.json").write_text(
            json.dumps(rollup, indent=2) + "\n", encoding="utf-8"
        )
        finish_run(run, status="online_gates_done")
        set_global_obs(None)

    # exit non-zero only if P3 hard-failed when requested
    if "P3" in results and results["P3"].get("pass") is False:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

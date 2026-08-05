"""Case-scoped Raw Snapshot — light capture during S1; heavy APIs deferred.

Golden Rule: never open a second browser; never call getalldocuments.
Light snapshot uses already-open page HTML / in-flight XHR only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from case_artifact_contract import (
    save_raw_json_with_meta,
    save_raw_text_with_meta,
    update_audit,
    write_case_sources,
    write_json,
)
from case_paths import case_root, ensure_case_layout, write_case_meta
from logging_config import get_logger

log = get_logger("case_raw_capture")

RAW_SUBDIR = "raw"

# Light snapshot completeness (payments are a separate stage)
SNAPSHOT_REQUIRED = (
    "chart_notes.html",  # S1 opens patientChartNote
    "request_log.json",
    "case_sources.json",
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def raw_dir(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    return case_root(base_dir, facility_id, case_id) / RAW_SUBDIR


def ensure_raw_layout(base_dir: Path, facility_id: str | int, case_id: str | int) -> Path:
    ensure_case_layout(base_dir, facility_id, case_id)
    root = raw_dir(base_dir, facility_id, case_id)
    (root / "graphql").mkdir(parents=True, exist_ok=True)
    (root / "probe_extra").mkdir(parents=True, exist_ok=True)
    return root


def raw_coverage(raw_root: Path) -> dict[str, Any]:
    present = {name: (Path(raw_root) / name).is_file() for name in SNAPSHOT_REQUIRED}
    # Bonus artifacts
    for name in (
        "patientChart.html",
        "scheduler.json",
        "edoc_list.json",
        "patientChart.cleaned.html",
    ):
        present[name] = (Path(raw_root) / name).is_file()
    required = list(SNAPSHOT_REQUIRED)
    have = sum(1 for r in required if present.get(r))
    return {
        "present": present,
        "required": required,
        "required_present": have,
        "required_total": len(required),
        "coverage_pct": round(100.0 * have / len(required), 1) if required else 0.0,
    }


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8", errors="replace")


def save_json(path: Path, obj: Any) -> None:
    write_json(path, obj)


def extract_payments_json_from_html(html: str) -> list[dict[str, Any]] | None:
    m = re.search(r"var\s+transactions\s*=\s*(\[.*?\]);", html or "", re.DOTALL)
    if not m:
        return None
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if isinstance(arr, list):
        return [x for x in arr if isinstance(x, dict)]
    return None


def append_request_log(
    raw_root: Path,
    entries: list[dict[str, Any]],
) -> Path:
    path = Path(raw_root) / "request_log.json"
    existing: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                existing = data
        except json.JSONDecodeError:
            existing = []
    existing.extend(entries)
    write_json(path, existing)
    return path


def save_chart_html_if_present(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    html: str,
    filename: str = "chart_notes.html",
    endpoint: str = "/patientChartNote.php",
) -> Path | None:
    """Cheap S1 hook — persist already-fetched HTML without extra requests."""
    if not (html or "").strip():
        return None
    root = ensure_raw_layout(base_dir, facility_id, case_id)
    paths = save_raw_text_with_meta(
        root / filename,
        html,
        facility_id=facility_id,
        case_id=case_id,
        endpoint=endpoint,
        also_cleaned=True,
    )
    return paths.get("raw")


def light_raw_snapshot_from_page_html(
    base_dir: Path,
    *,
    facility_id: str | int,
    case_id: str | int,
    patient_id: str | int,
    html: str,
    request_log: list[dict[str, Any]] | None = None,
    edoc_list: list[dict[str, Any]] | None = None,
    scheduler_raw: Any | None = None,
    page_url: str = "",
) -> dict[str, Any]:
    """Persist light snapshot artifacts already available after S1 (no new WebPT I/O)."""
    case_dir = case_root(base_dir, facility_id, case_id)
    root = ensure_raw_layout(base_dir, facility_id, case_id)
    errors: list[str] = []

    try:
        save_chart_html_if_present(
            base_dir,
            facility_id=facility_id,
            case_id=case_id,
            html=html,
            filename="chart_notes.html",
            endpoint="/patientChartNote.php",
        )
        # If URL looks like patientChart.php, also store as patientChart.html
        if "patientchart.php" in (page_url or "").lower() and "patientchartnote" not in (
            page_url or ""
        ).lower():
            save_raw_text_with_meta(
                root / "patientChart.html",
                html,
                facility_id=facility_id,
                case_id=case_id,
                endpoint="/patientChart.php",
                also_cleaned=True,
            )
    except Exception as exc:
        errors.append(f"html: {exc}")

    if request_log:
        try:
            append_request_log(root, list(request_log))
        except Exception as exc:
            errors.append(f"request_log: {exc}")
    elif not (root / "request_log.json").is_file():
        write_json(
            root / "request_log.json",
            [
                {
                    "endpoint": "/patientChartNote.php",
                    "method": "GET",
                    "status": 200,
                    "elapsed_sec": "",
                    "bytes": len((html or "").encode("utf-8", errors="replace")),
                    "content_type": "text/html",
                    "phase": "s1_snapshot",
                    "timestamp": _utc(),
                }
            ],
        )

    if edoc_list is not None:
        try:
            save_raw_json_with_meta(
                root / "edoc_list.json",
                edoc_list,
                facility_id=facility_id,
                case_id=case_id,
                endpoint="/edoc/edoc/getdocumentspercase",
            )
        except Exception as exc:
            errors.append(f"edoc_list: {exc}")

    if scheduler_raw is not None:
        try:
            # Full POST response as-is — no parse
            save_raw_json_with_meta(
                root / "scheduler.json",
                scheduler_raw,
                facility_id=facility_id,
                case_id=case_id,
                endpoint="/scheduler/index/data/T/e",
            )
        except Exception as exc:
            errors.append(f"scheduler: {exc}")

    write_case_sources(case_dir)
    update_audit(
        case_dir,
        flag="raw_snapshot_complete",
        value=not errors,
        error="; ".join(errors) if errors else "",
    )
    cov = raw_coverage(root)
    write_case_meta(
        base_dir,
        facility_id=facility_id,
        case_id=case_id,
        meta={
            "patient_id": str(patient_id),
            "raw_snapshot_at": _utc(),
            "raw_coverage_pct": cov["coverage_pct"],
            "raw_snapshot_errors": errors,
        },
    )
    return {"raw_dir": str(root), "coverage": cov, "errors": errors}


# --- Deferred heavy capture (payments / missing APIs) — NOT for PDF wave ---

async def capture_case_raw(
    context: Any,
    *,
    base_dir: Path,
    facility_id: str | int,
    case_id: int,
    patient_id: int,
    config: Any,
    session: Any,
    scheduler_events: list[dict[str, Any]] | None = None,
    include_scheduler_fetch: bool = True,
    include_payments: bool = False,
) -> dict[str, Any]:
    """Heavy/deferred capture. Prefer light_raw_snapshot_from_page_html during drain."""
    from auth import ajax_headers
    from chart_notes_api import patient_chart_note_url
    from config import BASE_URL, SCHEDULER_DATA_URL
    from edoc_api import get_documents_per_case
    from patient_chart_api import patient_chart_url
    from patient_payments_api import patient_payments_url
    from playwright.async_api import BrowserContext

    if not isinstance(context, BrowserContext):
        raise TypeError("context must be BrowserContext")

    root = ensure_raw_layout(base_dir, facility_id, case_id)
    case_dir = case_root(base_dir, facility_id, case_id)
    errors: list[str] = []
    req_log: list[dict[str, Any]] = []

    async def _get(url: str, *, referer: str) -> str:
        t0 = datetime.now(timezone.utc)
        resp = await context.request.get(
            url, headers={"Referer": referer}, timeout=90_000
        )
        text = await resp.text()
        hdrs = await resp.all_headers()
        req_log.append(
            {
                "endpoint": url.split("?", 1)[0].replace(BASE_URL, ""),
                "method": "GET",
                "status": resp.status,
                "elapsed_sec": (
                    datetime.now(timezone.utc) - t0
                ).total_seconds(),
                "bytes": len(text.encode("utf-8", errors="replace")),
                "content_type": hdrs.get("content-type", ""),
                "phase": "deferred_raw",
                "timestamp": _utc(),
            }
        )
        return text

    try:
        html = await _get(
            patient_chart_url(patient_id, case_id),
            referer=f"{BASE_URL}/dashboard.php",
        )
        save_raw_text_with_meta(
            root / "patientChart.html",
            html,
            facility_id=facility_id,
            case_id=case_id,
            endpoint="/patientChart.php",
            also_cleaned=True,
        )
    except Exception as exc:
        errors.append(f"patientChart: {exc}")

    try:
        html = await _get(
            patient_chart_note_url(patient_id, case_id),
            referer=patient_chart_url(patient_id, case_id),
        )
        save_raw_text_with_meta(
            root / "chart_notes.html",
            html,
            facility_id=facility_id,
            case_id=case_id,
            endpoint="/patientChartNote.php",
            also_cleaned=True,
        )
    except Exception as exc:
        errors.append(f"chart_notes: {exc}")

    try:
        docs = await get_documents_per_case(
            context,
            case_id=case_id,
            patient_id=patient_id,
            config=config,
            session=session,
        )
        save_raw_json_with_meta(
            root / "edoc_list.json",
            docs,
            facility_id=facility_id,
            case_id=case_id,
            endpoint="/edoc/edoc/getdocumentspercase",
        )
        req_log.append(
            {
                "endpoint": "/edoc/edoc/getdocumentspercase",
                "method": "POST",
                "status": 200,
                "elapsed_sec": "",
                "bytes": len(json.dumps(docs).encode("utf-8")),
                "content_type": "application/json",
                "phase": "deferred_raw",
                "timestamp": _utc(),
            }
        )
    except Exception as exc:
        errors.append(f"edoc_list: {exc}")

    try:
        events: Any = list(scheduler_events or [])
        if include_scheduler_fetch and not events:
            form = {"from": "2020-01-01", "to": "2030-12-31"}
            resp = await context.request.post(
                SCHEDULER_DATA_URL,
                form=form,
                headers=ajax_headers(session, referer=f"{BASE_URL}/scheduler/index"),
                timeout=120_000,
            )
            try:
                events = await resp.json()
            except Exception:
                events = json.loads(await resp.text())
            req_log.append(
                {
                    "endpoint": "/scheduler/index/data/T/e",
                    "method": "POST",
                    "status": resp.status,
                    "elapsed_sec": "",
                    "bytes": "",
                    "content_type": "application/json",
                    "phase": "deferred_raw",
                    "timestamp": _utc(),
                }
            )
        # Store FULL response — no filtering/parse
        save_raw_json_with_meta(
            root / "scheduler.json",
            events,
            facility_id=facility_id,
            case_id=case_id,
            endpoint="/scheduler/index/data/T/e",
        )
    except Exception as exc:
        errors.append(f"scheduler: {exc}")

    if include_payments:
        try:
            from case_payments_stage import fetch_and_store_payments

            await fetch_and_store_payments(
                context,
                base_dir=base_dir,
                facility_id=facility_id,
                case_id=case_id,
                patient_id=patient_id,
            )
        except Exception as exc:
            errors.append(f"payments: {exc}")

    append_request_log(root, req_log)
    write_case_sources(case_dir)
    update_audit(
        case_dir,
        flag="raw_snapshot_complete",
        value=True,
        error="; ".join(errors) if errors else "",
    )
    cov = raw_coverage(root)
    write_case_meta(
        base_dir,
        facility_id=facility_id,
        case_id=case_id,
        meta={
            "patient_id": str(patient_id),
            "raw_captured_at": _utc(),
            "raw_coverage_pct": cov["coverage_pct"],
            "raw_errors": errors,
        },
    )
    return {"raw_dir": str(root), "coverage": cov, "errors": errors}

"""Exclusive-session WebPT surface probe — record ALL request/response metadata.

Golden Rule: one browser only. Do NOT run while case drain holds the session.
Saves samples under reports/webpt_probe_raw/ and refreshes webpt_inventory.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from auth import ensure_logged_in, load_storage_state  # noqa: E402
from config import (  # noqa: E402
    BASE_URL,
    SCHEDULER_INDEX_URL,
    STORAGE_STATE_PATH,
    WebPTConfig,
    load_config,
)
from patient_chart_api import patient_chart_url  # noqa: E402
from patient_payments_api import patient_payments_url  # noqa: E402
from chart_notes_api import patient_chart_note_url  # noqa: E402
from snowflake_pull.webpt_inventory import (  # noqa: E402
    build_inventory_from_http_log,
    merge_fields_into_inventory,
    normalize_endpoint,
    write_inventory,
)

DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"
DEFAULT_REPORTS = DEFAULT_ARTIFACTS / "reports"


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(url: str, method: str, idx: int) -> str:
    ep = normalize_endpoint(url).strip("/").replace("/", "_") or "root"
    return f"{idx:04d}_{method}_{ep}"[:120]


def _json_keys(obj: Any, *, prefix: str = "", depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    keys: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            keys.append(path)
            keys.extend(_json_keys(v, prefix=path, depth=depth + 1))
    elif isinstance(obj, list) and obj:
        keys.extend(_json_keys(obj[0], prefix=prefix + "[]", depth=depth + 1))
    return keys


def _html_labels(html: str) -> list[str]:
    from patient_chart_api import LABEL_PATTERN

    out: list[str] = []
    for m in LABEL_PATTERN.finditer(html or ""):
        lab = m.group("label").strip()
        if lab and lab not in out:
            out.append(lab)
    for lab in re.findall(
        r'class="x-form-item-label"[^>]*>([^<]+)</label>', html or ""
    ):
        clean = lab.strip().rstrip(":")
        if clean and clean not in out:
            out.append(clean)
    return out


async def run_probe(
    *,
    facility_id: int,
    patient_id: int,
    case_id: int,
    reports_dir: Path,
    headless: bool,
) -> Path:
    from playwright.async_api import async_playwright

    from auth import SessionState, switch_clinic

    reports_dir.mkdir(parents=True, exist_ok=True)
    probe_raw = reports_dir / "webpt_probe_raw"
    probe_raw.mkdir(parents=True, exist_ok=True)
    events_path = reports_dir / "webpt_probe_events.jsonl"

    config = load_config()
    config.headless = headless
    session = SessionState()
    captured: list[dict[str, Any]] = []
    idx = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            storage_state=str(STORAGE_STATE_PATH)
            if STORAGE_STATE_PATH.exists()
            else None
        )
        page = await context.new_page()

        async def on_response(response: Any) -> None:
            nonlocal idx
            try:
                req = response.request
                url = response.url
                method = req.method
                status = response.status
                headers = await response.all_headers()
                ct = headers.get("content-type") or headers.get("Content-Type") or ""
                body_bytes = b""
                try:
                    body_bytes = await response.body()
                except Exception:
                    body_bytes = b""
                idx += 1
                name = _safe_name(url, method, idx)
                kind = "other"
                fields: list[str] = []
                sample_rel = ""
                if "json" in ct.lower() or urlparse(url).path.endswith("graphql"):
                    kind = "json"
                    text = body_bytes.decode("utf-8", errors="replace")
                    sample_path = probe_raw / f"{name}.json"
                    sample_path.write_text(text[:2_000_000], encoding="utf-8")
                    sample_rel = str(sample_path.relative_to(reports_dir))
                    try:
                        fields = _json_keys(json.loads(text))[:200]
                    except Exception:
                        fields = []
                elif "html" in ct.lower() or url.endswith(".php"):
                    kind = "html"
                    text = body_bytes.decode("utf-8", errors="replace")
                    sample_path = probe_raw / f"{name}.html"
                    sample_path.write_text(text[:2_000_000], encoding="utf-8")
                    sample_rel = str(sample_path.relative_to(reports_dir))
                    fields = _html_labels(text)
                    if "var transactions" in text:
                        m = re.search(
                            r"var\s+transactions\s*=\s*(\[.*?\]);",
                            text,
                            re.DOTALL,
                        )
                        if m:
                            try:
                                arr = json.loads(m.group(1))
                                if arr and isinstance(arr[0], dict):
                                    fields.extend(sorted(arr[0].keys()))
                            except Exception:
                                pass
                elif body_bytes[:4] == b"%PDF" or "pdf" in ct.lower():
                    kind = "binary"
                else:
                    if body_bytes:
                        sample_path = probe_raw / f"{name}.bin"
                        sample_path.write_bytes(body_bytes[:500_000])
                        sample_rel = str(sample_path.relative_to(reports_dir))

                event = {
                    "timestamp": _utc(),
                    "method": method,
                    "url": url,
                    "endpoint": normalize_endpoint(url),
                    "status": status,
                    "content_type": ct,
                    "response_kind": kind,
                    "fields_discovered": fields,
                    "sample_path": sample_rel,
                    "bytes": len(body_bytes),
                }
                captured.append(event)
                with events_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event) + "\n")
            except Exception as exc:
                with events_path.open("a", encoding="utf-8") as f:
                    f.write(
                        json.dumps(
                            {"timestamp": _utc(), "error": str(exc), "url": getattr(response, "url", "")}
                        )
                        + "\n"
                    )

        context.on("response", on_response)

        await ensure_logged_in(page, config, session)
        await switch_clinic(page, facility_id, config, session)

        # Sample surfaces
        await page.goto(
            patient_chart_url(patient_id, case_id),
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        await page.wait_for_timeout(1500)

        await page.goto(
            patient_chart_note_url(patient_id, case_id),
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        await page.wait_for_timeout(1500)

        await page.goto(
            patient_payments_url(patient_id, case_id),
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        await page.wait_for_timeout(1500)

        await page.goto(
            f"{BASE_URL}/patientExtDoc.php?ID={patient_id}&CaseID={case_id}",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        await page.wait_for_timeout(1500)

        await page.goto(
            SCHEDULER_INDEX_URL, wait_until="domcontentloaded", timeout=90_000
        )
        await page.wait_for_timeout(2000)

        # Persist storage for reuse
        await context.storage_state(path=str(STORAGE_STATE_PATH))
        await browser.close()

    # Build / refresh inventory from probe events (+ existing http log if present)
    http_log = reports_dir / "http_requests.jsonl"
    if http_log.is_file():
        inventory = build_inventory_from_http_log(http_log)
    else:
        inventory = {
            "generated_at": _utc(),
            "source": "surface_probe",
            "endpoints": [],
            "endpoint_count": 0,
            "app_hits": 0,
            "http_lines_scanned": 0,
            "notes": [],
        }
    inventory["probe"] = {
        "facility_id": facility_id,
        "patient_id": patient_id,
        "case_id": case_id,
        "events": len(captured),
        "events_path": str(events_path),
    }
    for ev in captured:
        merge_fields_into_inventory(
            inventory,
            endpoint=ev.get("endpoint") or "",
            method=str(ev.get("method") or "GET"),
            fields=list(ev.get("fields_discovered") or []),
            sample_path=str(ev.get("sample_path") or ""),
            content_type=str(ev.get("content_type") or ""),
            response_kind=str(ev.get("response_kind") or ""),
            url_sample=str(ev.get("url") or ""),
        )
    inventory["notes"] = list(inventory.get("notes") or []) + [
        f"Surface probe at {_utc()} for case {case_id} patient {patient_id}."
    ]
    json_path, _ = write_inventory(inventory, reports_dir)
    meta = {
        "generated_at": _utc(),
        "facility_id": facility_id,
        "patient_id": patient_id,
        "case_id": case_id,
        "events": len(captured),
        "inventory": str(json_path),
    }
    (reports_dir / "webpt_probe_meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return json_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--facility-id", type=int, required=True)
    ap.add_argument("--patient-id", type=int, required=True)
    ap.add_argument("--case-id", type=int, required=True)
    ap.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument(
        "--i-confirm-exclusive-session",
        action="store_true",
        help="Required: confirms drain/babysit is stopped (no second browser).",
    )
    args = ap.parse_args()
    if not args.i_confirm_exclusive_session:
        print(
            "Refusing to open Playwright: pass --i-confirm-exclusive-session "
            "after stopping case drain (Golden Rule: one WebPT session).",
            file=sys.stderr,
        )
        return 2
    path = asyncio.run(
        run_probe(
            facility_id=args.facility_id,
            patient_id=args.patient_id,
            case_id=args.case_id,
            reports_dir=args.reports_dir,
            headless=args.headless,
        )
    )
    print(f"Inventory updated: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

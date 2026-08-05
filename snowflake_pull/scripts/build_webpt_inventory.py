"""Build webpt_inventory.json from live http_requests.jsonl (+ optional field samples)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from snowflake_pull.webpt_inventory import (  # noqa: E402
    build_inventory_from_http_log,
    merge_fields_into_inventory,
    write_inventory,
)


DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"
DEFAULT_REPORTS = DEFAULT_ARTIFACTS / "reports"


def _discover_html_labels(html: str) -> list[str]:
    from patient_chart_api import LABEL_PATTERN

    labels = []
    for m in LABEL_PATTERN.finditer(html or ""):
        lab = m.group("label").strip()
        if lab and lab not in labels:
            labels.append(lab)
    # ExtJS form labels
    for lab in re.findall(
        r'class="x-form-item-label"[^>]*>([^<]+)</label>', html or ""
    ):
        clean = lab.strip().rstrip(":")
        if clean and clean not in labels:
            labels.append(clean)
    return labels


def _discover_json_keys_from_payments(html: str) -> list[str]:
    m = re.search(r"var\s+transactions\s*=\s*(\[.*?\]);", html or "", re.DOTALL)
    if not m:
        return []
    try:
        arr = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    keys: list[str] = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        for k in item:
            if k not in keys:
                keys.append(str(k))
    return keys


def _attach_fixture_fields(inventory: dict, probe_raw: Path) -> None:
    """Seed fields_discovered from saved HTML fixtures / probe_raw samples."""
    candidates = [
        (
            "/patientchart.php",
            "GET",
            list((SCRAPER / "output").rglob("patient_chart_*.html"))[:5],
            "html",
        ),
        (
            "/patient/transaction/chart",
            "GET",
            list((SCRAPER / "output").rglob("payments*.html"))[:5]
            + list((SCRAPER / "output").rglob("patient_payments_*.html"))[:5],
            "html",
        ),
        (
            "/patientchartnote.php",
            "GET",
            list((SCRAPER / "output").rglob("chart_notes_*.html"))[:3],
            "html",
        ),
    ]
    if probe_raw.is_dir():
        for p in probe_raw.rglob("*.html"):
            name = p.name.lower()
            if "payment" in name or "transaction" in name:
                candidates[1][2].append(p)  # type: ignore[index]
            elif "chartnote" in name or "chart_note" in name:
                candidates[2][2].append(p)  # type: ignore[index]
            elif "chart" in name:
                candidates[0][2].append(p)  # type: ignore[index]

    for endpoint, method, paths, kind in candidates:
        fields: list[str] = []
        sample = ""
        for path in paths:
            try:
                text = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if endpoint == "/patient/transaction/chart":
                fields.extend(_discover_json_keys_from_payments(text))
            fields.extend(_discover_html_labels(text))
            if not sample:
                # Copy into probe_raw for stable sample_path
                probe_raw.mkdir(parents=True, exist_ok=True)
                dest = probe_raw / f"{endpoint.strip('/').replace('/', '_')}_sample{Path(path).suffix}"
                try:
                    dest.write_text(text, encoding="utf-8", errors="replace")
                    sample = str(dest.relative_to(DEFAULT_REPORTS))
                except OSError:
                    sample = str(path)
        # dedupe preserve order
        seen: set[str] = set()
        uniq = []
        for f in fields:
            if f not in seen:
                seen.add(f)
                uniq.append(f)
        if uniq or sample:
            merge_fields_into_inventory(
                inventory,
                endpoint=endpoint,
                method=method,
                fields=uniq,
                sample_path=sample,
                response_kind=kind,
            )

    # Known edoc list keys from WebPT responses (documented in edoc usage)
    merge_fields_into_inventory(
        inventory,
        endpoint="/edoc/edoc/getdocumentspercase",
        method="POST",
        fields=[
            "ExtDocID",
            "URI",
            "UserDefName",
            "DateFiled",
            "CaseID",
            "PatientID",
            "DocType",
            "Category",
            "Locked",
            "Signed",
            "UploadedBy",
            "FileSize",
            "MimeType",
        ],
        response_kind="json",
    )

    # Scheduler event keys commonly observed
    merge_fields_into_inventory(
        inventory,
        endpoint="/scheduler/index/data/T/e",
        method="POST",
        fields=[
            "id",
            "appointment_id",
            "p_id",
            "case_id",
            "title",
            "start_date",
            "end_date",
            "status",
            "checkin_time",
            "checkout_time",
            "ins_name",
            "copay",
            "auth_visits",
            "visit_num",
            "apt_type",
            "paid",
            "length",
            "provider",
            "room",
            "resource",
            "color",
            "reason",
        ],
        response_kind="json",
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--http-jsonl",
        type=Path,
        default=DEFAULT_REPORTS / "http_requests.jsonl",
    )
    ap.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    ap.add_argument(
        "--probe-raw",
        type=Path,
        default=DEFAULT_REPORTS / "webpt_probe_raw",
    )
    ap.add_argument("--max-lines", type=int, default=None)
    args = ap.parse_args()

    if not args.http_jsonl.is_file():
        print(f"ERROR: missing http log {args.http_jsonl}", file=sys.stderr)
        return 2

    inventory = build_inventory_from_http_log(
        args.http_jsonl, max_lines=args.max_lines
    )
    _attach_fixture_fields(inventory, args.probe_raw)
    json_path, md_path = write_inventory(inventory, args.reports_dir)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Endpoints: {inventory.get('endpoint_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

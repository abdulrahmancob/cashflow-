"""Seed cases/{f}/{c}/raw/ from local HTML fixtures (offline; no WebPT)."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from case_artifact_contract import save_raw_json_with_meta, save_raw_text_with_meta  # noqa: E402
from case_paths import write_case_meta  # noqa: E402
from case_payments_stage import store_payments_from_html  # noqa: E402
from case_raw_capture import (  # noqa: E402
    extract_payments_json_from_html,
    light_raw_snapshot_from_page_html,
    raw_coverage,
    raw_dir,
)

DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"


def _parse_ids(name: str) -> tuple[str, str] | None:
    m = re.search(r"(\d+)_(\d+)", name)
    if not m:
        return None
    return m.group(1), m.group(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    ap.add_argument("--facility-id", default="21535")
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()

    from patient_chart_api import LABEL_PATTERN

    chart_fixtures = sorted((SCRAPER / "output").rglob("patient_chart_*.html"))
    classic: list[Path] = []
    other: list[Path] = []
    for p in chart_fixtures:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        (classic if LABEL_PATTERN.search(text) else other).append(p)
    chart_fixtures = classic + other

    pay_fixtures = sorted((SCRAPER / "output").rglob("patient_payments_*.html"))
    pay_fixtures += sorted((SCRAPER / "output").rglob("payments_http_*.html"))

    seeded = 0
    for chart in chart_fixtures:
        ids = _parse_ids(chart.name)
        if not ids:
            continue
        patient_id, case_id = ids
        html = chart.read_text(encoding="utf-8", errors="replace")

        light_raw_snapshot_from_page_html(
            args.artifacts,
            facility_id=args.facility_id,
            case_id=case_id,
            patient_id=patient_id,
            html=html,
            page_url=f"https://app.webpt.com/patientChart.php?ID={patient_id}&CaseID={case_id}",
            edoc_list=[
                {
                    "ExtDocID": 1,
                    "URI": "seed.pdf",
                    "UserDefName": "Seed Doc",
                    "DateFiled": "2026-01-15",
                    "CaseID": int(case_id),
                    "PatientID": int(patient_id),
                    "Category": "Clinical",
                    "Signed": True,
                    "Locked": False,
                    "UploadedBy": "seed",
                }
            ],
            scheduler_raw=[
                {
                    "id": 1,
                    "appointment_id": 1,
                    "p_id": int(patient_id),
                    "case_id": int(case_id),
                    "title": "SEED, PATIENT - 01/01/1990 - (Default)",
                    "start_date": "2026-01-15 10:00:00",
                    "status": 4,
                    "checkin_time": "2026-01-15 09:55:00",
                    "checkout_time": "2026-01-15 10:45:00",
                    "ins_name": "Seed Insurance",
                    "copay": "20",
                    "auth_visits": "8",
                    "apt_type": "PT",
                    "length": "45",
                    "provider": "Seed PT",
                }
            ],
        )
        raw = raw_dir(args.artifacts, args.facility_id, case_id)
        save_raw_text_with_meta(
            raw / "patientChart.html",
            html,
            facility_id=args.facility_id,
            case_id=case_id,
            endpoint="/patientChart.php",
            also_cleaned=True,
        )

        pay = next(
            (p for p in pay_fixtures if patient_id in p.name),
            pay_fixtures[0] if pay_fixtures else None,
        )
        if pay:
            phtml = pay.read_text(encoding="utf-8", errors="replace")
            store_payments_from_html(
                args.artifacts,
                facility_id=args.facility_id,
                case_id=case_id,
                html=phtml,
            )
            # Ensure JSON extract worked even if HTML path used probe_extra
            txns = extract_payments_json_from_html(phtml)
            if txns:
                save_raw_json_with_meta(
                    Path(args.artifacts)
                    / "cases"
                    / args.facility_id
                    / case_id
                    / "payments"
                    / "payments.json",
                    txns,
                    facility_id=args.facility_id,
                    case_id=case_id,
                    endpoint="/patient/transaction/chart#transactions",
                )

        cov = raw_coverage(raw)
        write_case_meta(
            args.artifacts,
            facility_id=args.facility_id,
            case_id=case_id,
            meta={
                "patient_id": patient_id,
                "raw_seeded_from_fixtures": True,
                "raw_coverage_pct": cov["coverage_pct"],
            },
        )
        seeded += 1
        print(f"Seeded {args.facility_id}/{case_id} raw={cov['coverage_pct']}%")
        if seeded >= args.limit:
            break

    print(f"Done — seeded {seeded} cases")
    return 0 if seeded else 1


if __name__ == "__main__":
    raise SystemExit(main())

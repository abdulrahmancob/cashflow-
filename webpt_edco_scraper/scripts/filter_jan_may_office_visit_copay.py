#!/usr/bin/env python3
"""Filter unpaid Patient Payments to Copay / Office Visit Copay and enrich from patients.csv."""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

try:
    from openpyxl import Workbook
except ImportError:  # pragma: no cover
    Workbook = None  # type: ignore

OUT_COLS = [
    "facility_id",
    "facility_name",
    "patient_id",
    "patient_name",
    "case_id",
    "dos",
    "payment_type",
    "description",
    "amount_due",
    "amount_paid",
    "amount_owed",
    "reason",
    "mobile_phone",
    "home_phone",
    "work_phone",
    "email",
    "best_phone",
]

EXCLUDE_RE = re.compile(r"(no\s*show|noshow|\bns\b|cancel)", re.I)
OFFICE_RE = re.compile(r"office\s*visit\s*copay", re.I)


def _money(v: str) -> float:
    s = (v or "").strip().replace("$", "").replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def best_phone(mobile: str, home: str, work: str) -> str:
    for p in (mobile, home, work):
        if (p or "").strip():
            return p.strip()
    return ""


def load_patients(path: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        for row in r:
            pid = str(row.get("PATIENT_ID") or row.get("patient_id") or "").strip()
            if not pid:
                continue
            out[pid] = {
                "mobile_phone": str(row.get("MOBILE_PHONE") or row.get("mobile_phone") or "").strip(),
                "home_phone": str(row.get("HOME_PHONE") or row.get("home_phone") or "").strip(),
                "work_phone": str(row.get("WORK_PHONE") or row.get("work_phone") or "").strip(),
                "email": str(row.get("EMAIL_ADDRESS") or row.get("email") or "").strip(),
            }
    return out


def dos_in_jan_may(dos: str) -> bool:
    s = (dos or "").strip()
    # Accept YYYY-MM-DD or M/D/YYYY
    if re.match(r"2026-0[1-5]-\d{2}", s):
        return True
    m = re.match(r"(\d{1,2})/(\d{1,2})/(2026)", s)
    if m:
        month = int(m.group(1))
        return 1 <= month <= 5
    return False


def keep_row(row: dict[str, str]) -> bool:
    ptype = (row.get("payment_type") or "").strip().lower()
    if ptype != "copay":
        return False
    desc = row.get("description") or ""
    if not OFFICE_RE.search(desc):
        return False
    if EXCLUDE_RE.search(desc):
        return False
    if not dos_in_jan_may(row.get("dos") or ""):
        return False
    due = _money(row.get("amount_due") or "")
    paid = _money(row.get("amount_paid") or "")
    if paid >= due and due > 0:
        return False
    if due <= 0 and paid >= due:
        # still treat zero-due as not unpaid interest; keep only true underpaid
        owed = _money(row.get("amount_owed") or "")
        if owed <= 0 and paid >= due:
            return False
    return True


def write_xlsx(path: Path, rows: list[dict[str, str]]) -> None:
    if Workbook is None:
        raise SystemExit("openpyxl required for xlsx output")
    wb = Workbook()
    ws = wb.active
    ws.title = "unpaid"
    ws.append(OUT_COLS)
    for row in rows:
        ws.append([row.get(c, "") for c in OUT_COLS])
    wb.save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="unpaid or raw payments CSV")
    ap.add_argument("--patients", required=True, help="patients.csv")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument(
        "--basename",
        default="patient_payments_unpaid_jan_may_2026",
    )
    args = ap.parse_args()

    inp = Path(args.input)
    patients = load_patients(Path(args.patients))
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    kept: list[dict[str, str]] = []
    total = 0
    with inp.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            total += 1
            if not keep_row(row):
                continue
            pid = str(row.get("patient_id") or "").strip()
            contact = patients.get(pid, {})
            mobile = contact.get("mobile_phone", "") or str(row.get("mobile_phone") or "")
            home = contact.get("home_phone", "") or str(row.get("home_phone") or "")
            work = contact.get("work_phone", "") or str(row.get("work_phone") or "")
            email = contact.get("email", "") or str(row.get("email") or "")
            out = {c: str(row.get(c) or "") for c in OUT_COLS}
            out["mobile_phone"] = mobile
            out["home_phone"] = home
            out["work_phone"] = work
            out["email"] = email
            out["best_phone"] = best_phone(mobile, home, work)
            # recompute owed if blank
            if not out.get("amount_owed"):
                owed = _money(out.get("amount_due")) - _money(out.get("amount_paid"))
                out["amount_owed"] = f"{owed:.2f}"
            kept.append(out)

    csv_path = out_dir / f"{args.basename}.csv"
    xlsx_path = out_dir / f"{args.basename}.xlsx"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(kept)
    write_xlsx(xlsx_path, kept)

    with_contact = sum(1 for r in kept if r.get("best_phone") or r.get("email"))
    print(f"input_rows={total}")
    print(f"kept_rows={len(kept)}")
    print(f"with_contact={with_contact}")
    print(f"wrote={csv_path}")
    print(f"wrote={xlsx_path}")
    # sample descriptions
    for r in kept[:5]:
        print(f"sample dos={r['dos']} type={r['payment_type']} desc={r['description']!r}")


if __name__ == "__main__":
    main()

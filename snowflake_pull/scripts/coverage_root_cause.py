"""Classify SF visits missing from reconciliation and clinic coverage gaps.

Writes under reconciliation/sf_compare/:
  - missing_classification.csv
  - coverage_root_cause.md
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import name_key_from_webpt, parse_date  # noqa: E402
from snowflake_pull.compare_visits import name_key_from_snowflake_patient  # noqa: E402

SF_PATH = _REPO / "snowflake_pull/output/all_billing_data.csv"
BASE = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
REC_PATH = BASE / "reconciliation/reconciliation_visits.csv"
EXPORT_PATH = BASE / "patients_export_273d.csv"
NOTES_PATH = BASE / "extracted/daily_notes.csv"
OUT_DIR = BASE / "reconciliation/sf_compare"

START = date(2026, 6, 1)
END = date(2026, 7, 31)
FOCUS_CLINICS = ("Brownsville", "Inwood")


def _in_range(dos: str) -> bool:
    d = parse_date(dos)
    return d is not None and START <= d <= END


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    export_ids: set[str] = set()
    export_fac: dict[str, str] = {}
    export_by_fac: dict[str, set[str]] = defaultdict(set)
    with EXPORT_PATH.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            fac = (row.get("facility_name") or "").strip()
            if not pid:
                continue
            export_ids.add(pid)
            export_fac[pid] = fac
            export_by_fac[fac].add(pid)

    rec_pids: set[str] = set()
    rec_pid_dos: set[tuple[str, str]] = set()
    rec_nk_dos: set[tuple[str, str]] = set()
    rec_fac_counts: Counter[str] = Counter()
    with REC_PATH.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("date_of_service") or "").strip()
            if not _in_range(dos):
                continue
            pid = (row.get("webpt_patient_id") or "").strip()
            nk = name_key_from_webpt(row.get("patient_name") or "")
            fac = (row.get("facility_name") or "").strip()
            rec_fac_counts[fac] += 1
            if pid:
                rec_pids.add(pid)
                rec_pid_dos.add((pid, dos))
            if nk:
                rec_nk_dos.add((nk, dos))

    notes_pids: set[str] = set()
    notes_pid_dos: set[tuple[str, str]] = set()
    notes_fac_counts: Counter[str] = Counter()
    notes_fac_patients: dict[str, set[str]] = defaultdict(set)
    with NOTES_PATH.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            dos = (row.get("date_of_daily_note") or "").strip()[:10]
            fac = (row.get("facility_name") or "").strip()
            if pid:
                notes_pids.add(pid)
            if pid and dos:
                notes_pid_dos.add((pid, dos))
            if fac:
                notes_fac_counts[fac] += 1
                if pid:
                    notes_fac_patients[fac].add(pid)

    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with SF_PATH.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("DATE_OF_SERVICE") or "").strip()
            if not _in_range(dos):
                continue
            nk = name_key_from_snowflake_patient(row.get("PATIENT") or "")
            if not nk:
                continue
            buckets[(nk, dos)].append(row)

    missing = {k: v for k, v in buckets.items() if k not in rec_nk_dos}
    class_rows: list[dict[str, str]] = []
    cats: Counter[str] = Counter()
    clinic_missing: Counter[str] = Counter()

    for (nk, dos), rows in sorted(missing.items(), key=lambda x: (x[0][1], x[0][0])):
        emrs = sorted(
            {(r.get("EMR_ID") or "").strip() for r in rows if (r.get("EMR_ID") or "").strip()}
        )
        clinics = sorted(
            {(r.get("CLINIC") or "").strip() for r in rows if (r.get("CLINIC") or "").strip()}
        )
        for c in clinics:
            clinic_missing[c] += 1

        if any(e in rec_pids for e in emrs) and any((e, dos) in rec_pid_dos for e in emrs):
            cat = "name_key_mismatch_same_emr_dos"
        elif any(e in rec_pids for e in emrs):
            cat = "patient_in_rec_but_dos_missing"
        elif emrs:
            cat = "patient_emr_not_in_rec_at_all"
        else:
            cat = "no_emr_id"
        cats[cat] += 1

        in_export = [e for e in emrs if e in export_ids]
        in_notes = [e for e in emrs if e in notes_pids]
        class_rows.append(
            {
                "name_key": nk,
                "date_of_service": dos,
                "sf_patient": (rows[0].get("PATIENT") or "").strip(),
                "sf_clinic": ";".join(clinics),
                "sf_status": (rows[0].get("STATUS") or "").strip(),
                "sf_insurance": (rows[0].get("INSURANCE") or "").strip(),
                "emr_ids": ";".join(emrs),
                "classification": cat,
                "emr_in_patients_export": "yes" if in_export else "no",
                "emr_in_daily_notes": "yes" if in_notes else "no",
                "export_facility": ";".join(
                    sorted({export_fac[e] for e in in_export if export_fac.get(e)})
                ),
                "sf_row_count": str(len(rows)),
            }
        )

    class_path = OUT_DIR / "missing_classification.csv"
    fields = list(class_rows[0].keys()) if class_rows else [
        "name_key",
        "date_of_service",
        "sf_patient",
        "sf_clinic",
        "sf_status",
        "sf_insurance",
        "emr_ids",
        "classification",
        "emr_in_patients_export",
        "emr_in_daily_notes",
        "export_facility",
        "sf_row_count",
    ]
    with class_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(class_rows)

    # Clinic focus deep-dive
    focus_lines: list[str] = []
    for clinic in FOCUS_CLINICS:
        sf_emrs: set[str] = set()
        sf_emr_dos: set[tuple[str, str]] = set()
        sf_rows = 0
        with SF_PATH.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if (row.get("CLINIC") or "").strip() != clinic:
                    continue
                dos = (row.get("DATE_OF_SERVICE") or "").strip()
                if not _in_range(dos):
                    continue
                sf_rows += 1
                emr = (row.get("EMR_ID") or "").strip()
                if emr:
                    sf_emrs.add(emr)
                    sf_emr_dos.add((emr, dos))
        in_export = sf_emrs & export_ids
        in_notes = sf_emrs & notes_pids
        in_rec = sf_emrs & rec_pids
        visit_in_notes = sum(1 for k in sf_emr_dos if k in notes_pid_dos)
        export_n = len(export_by_fac.get(clinic, set()))
        rec_n = rec_fac_counts.get(clinic, 0)
        notes_n = notes_fac_counts.get(clinic, 0)
        focus_lines.extend(
            [
                f"### {clinic}",
                "",
                f"- SF Jun–Jul raw rows: **{sf_rows}**; unique EMR: **{len(sf_emrs)}**",
                f"- patients_export_273d labeled {clinic}: **{export_n}** patients",
                f"- daily_notes labeled {clinic}: **{notes_n}** notes / "
                f"**{len(notes_fac_patients.get(clinic, set()))}** patients",
                f"- reconciliation_visits labeled {clinic}: **{rec_n}** visits",
                f"- SF EMR ∩ patients_export (any facility): "
                f"**{len(in_export)}** ({100 * len(in_export) / max(len(sf_emrs), 1):.1f}%)",
                f"- SF EMR ∩ daily_notes: "
                f"**{len(in_notes)}** ({100 * len(in_notes) / max(len(sf_emrs), 1):.1f}%)",
                f"- SF EMR ∩ reconciliation patients: "
                f"**{len(in_rec)}** ({100 * len(in_rec) / max(len(sf_emrs), 1):.1f}%)",
                f"- SF EMR+DOS exact hit in daily_notes: "
                f"**{visit_in_notes}** / {len(sf_emr_dos)} "
                f"({100 * visit_in_notes / max(len(sf_emr_dos), 1):.1f}%)",
                "",
            ]
        )

    # Global EMR-in-export coverage by SF clinic
    all_export = export_ids
    sf_clinic_emrs: dict[str, set[str]] = defaultdict(set)
    sf_clinic_rows: Counter[str] = Counter()
    with SF_PATH.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("DATE_OF_SERVICE") or "").strip()
            if not _in_range(dos):
                continue
            clinic = (row.get("CLINIC") or "").strip() or "(blank)"
            emr = (row.get("EMR_ID") or "").strip()
            sf_clinic_rows[clinic] += 1
            if emr:
                sf_clinic_emrs[clinic].add(emr)

    coverage_table: list[str] = [
        "| SF clinic | SF EMR | in export | pct | SF rows |",
        "|---|---:|---:|---:|---:|",
    ]
    for clinic in sorted(sf_clinic_emrs, key=lambda c: -len(sf_clinic_emrs[c])):
        emrs = sf_clinic_emrs[clinic]
        ov = emrs & all_export
        pct = 100 * len(ov) / max(len(emrs), 1)
        coverage_table.append(
            f"| {clinic} | {len(emrs)} | {len(ov)} | {pct:.1f}% | {sf_clinic_rows[clinic]} |"
        )

    md = "\n".join(
        [
            "# SF vs reconciliation coverage root cause",
            "",
            f"Window: **{START.isoformat()} .. {END.isoformat()}**",
            "",
            "## Compare summary",
            "",
            f"- SF visit keys (name+DOS): **{len(buckets)}**",
            f"- REC visit keys: **{len(rec_nk_dos)}**",
            f"- Missing in REC (name+DOS): **{len(missing)}**",
            "",
            "## Missing classification (EMR-aware)",
            "",
            *(f"- `{k}`: **{v}**" for k, v in cats.most_common()),
            "",
            "Interpretation:",
            "",
            "1. `patient_in_rec_but_dos_missing` — patient known in our pipeline, "
            "but that DOS visit was never ingested (largest bucket).",
            "2. `patient_emr_not_in_rec_at_all` — EMR never entered patients_export / recon.",
            "3. `name_key_mismatch_same_emr_dos` — false positive from name-key join; "
            "visit exists under same EMR+DOS.",
            "",
            f"Full row dump: `{class_path.name}`",
            "",
            "## Top clinics among missing visits",
            "",
            "| clinic | missing visit keys |",
            "|---|---:|",
            *[f"| {c} | {n} |" for c, n in clinic_missing.most_common(15)],
            "",
            "## Focus: Brownsville / Inwood",
            "",
            "Root cause: **upstream WebPT patient discovery / chart download coverage**, "
            "not reconciliation matching logic.",
            "",
            f"`patients_export_273d.csv` only lists "
            f"**{len(export_by_fac.get('Brownsville', set()))}** Brownsville and "
            f"**{len(export_by_fac.get('Inwood', set()))}** Inwood patients, while Snowflake "
            "has hundreds of distinct EMRs with Jun–Jul DOS at those clinics. "
            "`daily_notes.csv` and `reconciliation_visits.csv` largely follow the same "
            "thin patient set (export → download → extract → reconcile).",
            "",
            *focus_lines,
            "## SF clinic EMR coverage vs patients_export_273d",
            "",
            "(Overlap ignores facility label — counts EMR present anywhere in export.)",
            "",
            *coverage_table,
            "",
            "## Recommended next actions",
            "",
            "1. Re-run WebPT discovery for under-covered facilities "
            "(Brownsville `28029`, Inwood `21535`) with a full Jun–Jul schedule window.",
            "2. Re-download charts/edocs for newly discovered patients, re-extract, re-reconcile.",
            "3. Optionally harden `compare_visits` to join on `EMR_ID`/`webpt_patient_id` + DOS "
            "to eliminate `name_key_mismatch_same_emr_dos` false positives.",
            "",
        ]
    )
    md_path = OUT_DIR / "coverage_root_cause.md"
    md_path.write_text(md, encoding="utf-8")
    print(f"Wrote {class_path} ({len(class_rows)} rows)")
    print(f"Wrote {md_path}")
    print("classification:", dict(cats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

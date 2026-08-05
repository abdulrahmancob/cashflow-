"""Rebuild subtype-aware root_cause_summary.json for SF vs REC gaps."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import name_key_from_webpt, parse_date  # noqa: E402
from snowflake_pull.compare_visits import name_key_from_snowflake_patient  # noqa: E402
from snowflake_pull.coverage_run import resume_run, finish_run, init_run  # noqa: E402
from snowflake_pull.facility_map import map_sf_clinic  # noqa: E402
from snowflake_pull.observability import set_global_obs  # noqa: E402

START = date(2026, 6, 1)
END = date(2026, 7, 31)


def _in_range(dos: str) -> bool:
    d = parse_date(dos)
    return d is not None and START <= d <= END


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-id", default="")
    p.add_argument("--root", type=Path, default=None)
    p.add_argument("--init-if-missing", action="store_true")
    args = p.parse_args(argv)

    if args.run_id:
        run = resume_run(args.run_id, root=args.root, script="rebuild_root_cause.py")
    elif args.init_if_missing:
        run = init_run(root=args.root, script="rebuild_root_cause.py")
    else:
        raise SystemExit("Pass --run-id or --init-if-missing")

    set_global_obs(run.obs)
    run.obs.stage_start("root_cause")
    run.obs.online = False

    base = _REPO / "webpt_edco_scraper/output/jun_jul_2026"
    sf_path = _REPO / "snowflake_pull/output/all_billing_data.csv"
    rec_path = base / "reconciliation/reconciliation_visits.csv"
    notes_path = base / "extracted/daily_notes.csv"
    cpt_path = base / "extracted/cpt_codes.csv"
    export_path = base / "patients_export_273d.csv"

    export_ids: set[str] = set()
    with export_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            if pid:
                export_ids.add(pid)

    notes_by_pid: dict[str, list[str]] = defaultdict(list)
    notes_pid_dos: set[tuple[str, str]] = set()
    with notes_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            dos = (row.get("date_of_daily_note") or "")[:10]
            if pid and dos:
                notes_by_pid[pid].append(dos)
                notes_pid_dos.add((pid, dos))

    cpt_pid_dos: set[tuple[str, str]] = set()
    with cpt_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            pid = (row.get("patient_id") or "").strip()
            dos = (row.get("date_of_daily_note") or "")[:10]
            if pid and dos:
                cpt_pid_dos.add((pid, dos))

    rec_nk: set[tuple[str, str]] = set()
    rec_pids: set[str] = set()
    rec_pid_dos: set[tuple[str, str]] = set()
    with rec_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("date_of_service") or "").strip()
            if not _in_range(dos):
                continue
            nk = name_key_from_webpt(row.get("patient_name") or "")
            pid = (row.get("webpt_patient_id") or "").strip()
            if nk:
                rec_nk.add((nk, dos))
            if pid:
                rec_pids.add(pid)
                rec_pid_dos.add((pid, dos))

    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with sf_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            dos = (row.get("DATE_OF_SERVICE") or "").strip()
            if not _in_range(dos):
                continue
            nk = name_key_from_snowflake_patient(row.get("PATIENT") or "")
            if not nk:
                continue
            buckets[(nk, dos)].append(row)

    missing = {k: v for k, v in buckets.items() if k not in rec_nk}
    class_rows: list[dict[str, str]] = []
    subtype_matrix: Counter[tuple[str, str, str, str]] = Counter()
    # key: classification, subtype, status, clinic

    for (nk, dos), rows in missing.items():
        emrs = sorted({(r.get("EMR_ID") or "").strip() for r in rows if (r.get("EMR_ID") or "").strip()})
        clinics = sorted({(r.get("CLINIC") or "").strip() for r in rows if (r.get("CLINIC") or "").strip()})
        status = (rows[0].get("STATUS") or "").strip() or "Blank"
        clinic = clinics[0] if clinics else ""
        fmap = map_sf_clinic(clinic)

        if any((e, dos) in rec_pid_dos for e in emrs):
            classification = "name_key_mismatch_same_emr_dos"
            subtype = "name_mismatch"
        elif any(e in rec_pids for e in emrs):
            classification = "patient_in_rec_but_dos_missing"
            notes: list[str] = []
            for e in emrs:
                notes.extend(notes_by_pid.get(e, []))
            if any((e, dos) in notes_pid_dos for e in emrs):
                if any((e, dos) in cpt_pid_dos for e in emrs):
                    subtype = "note_and_cpt_exist_recon_missed"
                else:
                    subtype = "note_exists_cpt_missing"
            elif not notes:
                subtype = "patient_known_no_notes"
            else:
                mn, mx = min(notes), max(notes)
                if dos < mn:
                    subtype = "dos_before_first_note"
                elif dos > mx:
                    subtype = "dos_after_last_note"
                else:
                    subtype = "interior_gap"
        elif emrs:
            classification = "patient_emr_not_in_rec_at_all"
            subtype = "emr_not_in_export" if not any(e in export_ids for e in emrs) else "emr_in_export_not_in_rec"
        else:
            classification = "no_emr_id"
            subtype = "no_emr_id"

        subtype_matrix[(classification, subtype, status, clinic)] += 1
        class_rows.append(
            {
                "name_key": nk,
                "date_of_service": dos,
                "sf_patient": (rows[0].get("PATIENT") or "").strip(),
                "sf_clinic": clinic,
                "facility_map_status": fmap.status,
                "webpt_facility_id": fmap.webpt_facility_id or "",
                "sf_status": status,
                "emr_ids": ";".join(emrs),
                "classification": classification,
                "subtype": subtype,
                "emr_in_patients_export": "yes" if any(e in export_ids for e in emrs) else "no",
                "emr_in_daily_notes": "yes" if any(e in notes_by_pid for e in emrs) else "no",
                "dos_in_daily_notes": "yes" if any((e, dos) in notes_pid_dos for e in emrs) else "no",
                "dos_in_cpt_codes": "yes" if any((e, dos) in cpt_pid_dos for e in emrs) else "no",
            }
        )
        run.obs.emit(
            "decision",
            operation="classify_missing",
            correlation_id=f"{emrs[0] if emrs else ''}|{dos}|{fmap.webpt_facility_id or ''}|",
            emr_id=emrs[0] if emrs else "",
            dos=dos,
            facility_id=fmap.webpt_facility_id or "",
            facility_name=clinic,
            visit_status=status,
            outcome="success",
            decision=classification,
            decision_reason=subtype,
        )

    out_csv = run.artifacts / "missing_classification.csv"
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        fields = list(class_rows[0].keys()) if class_rows else ["classification"]
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(class_rows)

    by_class = Counter(r["classification"] for r in class_rows)
    by_subtype = Counter(r["subtype"] for r in class_rows)
    matrix = [
        {
            "classification": a,
            "subtype": b,
            "sf_status": c,
            "sf_clinic": d,
            "count": n,
        }
        for (a, b, c, d), n in sorted(subtype_matrix.items(), key=lambda x: -x[1])
    ]
    summary = {
        "run_id": run.run_id,
        "window": {"from": START.isoformat(), "to": END.isoformat()},
        "sf_visit_keys": len(buckets),
        "rec_visit_keys_name": len(rec_nk),
        "missing_name_key": len(missing),
        "by_classification": dict(by_class),
        "by_subtype": dict(by_subtype),
        "matrix": matrix,
        "recon_missed_candidates": by_subtype.get("note_exists_cpt_missing", 0)
        + by_subtype.get("note_and_cpt_exist_recon_missed", 0),
        "artifacts": {"missing_classification_csv": str(out_csv)},
    }
    path = run.run_dir / "root_cause_summary.json"
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    summaries = run.run_dir / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    (summaries / "root_cause_summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    run.obs.stage_end("root_cause", **{k: summary[k] for k in ("missing_name_key", "by_subtype")})
    print(json.dumps({"run_id": run.run_id, "root_cause": str(path), "by_subtype": dict(by_subtype)}, indent=2))
    finish_run(run, status="root_cause_done")
    set_global_obs(None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Parallel offline OCR over downloaded cases (no WebPT network).

Discovers cases with PDFs that are not yet ocr_complete, runs stage_ocr +
stage_merge in a ProcessPool, and writes case_export_ocr_{batch}.csv.
Safe to run alongside the WebPT drain (enrich_loop should keep --skip-ocr).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DEFAULT_ARTIFACTS = ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case"

log = logging.getLogger("case_ocr_batch")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _setup_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    # Avoid duplicate handlers on re-entry
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _audit_ocr_done(case_dir: Path) -> bool:
    """True if OCR stage already ran (audit flag or ocr_fields.json present)."""
    audit_path = case_dir / "audit.json"
    if audit_path.is_file():
        try:
            data = json.loads(audit_path.read_text(encoding="utf-8"))
            # Treat explicit True as done; also accept processed marker via file.
            if data.get("ocr_complete") is True:
                return True
            # Ran but extracted nothing — still done if error recorded without crash
            if data.get("ocr_complete") is False and data.get("ocr_error"):
                # only skip hard "no PDFs" / completed empty runs that left ocr_fields
                pass
        except (OSError, json.JSONDecodeError):
            pass
    ocr_fields = case_dir / "parsed" / "ocr_fields.json"
    return ocr_fields.is_file()


def _has_pdfs(case_dir: Path) -> bool:
    try:
        next(case_dir.rglob("*.pdf"))
        return True
    except StopIteration:
        return False


def discover_ocr_targets(
    cases_dir: Path,
    *,
    limit: int | None = None,
    facility_id: str | None = None,
) -> list[tuple[str, str]]:
    """Return (facility_id, case_id) with PDFs and not yet ocr_complete."""
    out: list[tuple[str, str]] = []
    for case_dir in sorted(cases_dir.glob("*/*")):
        if not case_dir.is_dir():
            continue
        fac = case_dir.parent.name
        case = case_dir.name
        if facility_id and fac != facility_id:
            continue
        if not _has_pdfs(case_dir):
            continue
        if _audit_ocr_done(case_dir):
            continue
        out.append((fac, case))
        if limit is not None and len(out) >= limit:
            break
    return out


def _ocr_one_case(payload: dict[str, Any]) -> dict[str, Any]:
    """Worker entry (picklable). Runs OCR + merge for one case."""
    # Re-init paths inside child process
    root = Path(payload["root"])
    scraper = root / "webpt_edco_scraper"
    for p in (str(root), str(scraper)):
        if p not in sys.path:
            sys.path.insert(0, p)

    from case_enrich_parse import stage_merge, stage_ocr  # noqa: WPS433
    from case_paths import case_root  # noqa: WPS433

    artifacts = Path(payload["artifacts"])
    fac = str(payload["facility_id"])
    case = str(payload["case_id"])
    dpi = int(payload.get("dpi") or 200)
    patient_id = str(payload.get("patient_id") or "")
    patient_name = str(payload.get("patient_name") or "")

    t0 = time.perf_counter()
    try:
        ocr_res = stage_ocr(
            artifacts,
            facility_id=fac,
            case_id=case,
            patient_name=patient_name,
            patient_id=patient_id,
            ocr_dpi=dpi,
        )
        stage_merge(
            artifacts,
            facility_id=fac,
            case_id=case,
            patient_id=patient_id,
            patient_name=patient_name,
        )
        elapsed = round(time.perf_counter() - t0, 2)
        err = str(ocr_res.get("error") or "")
        return {
            "facility_id": fac,
            "case_id": case,
            "ok": True,
            "fields": int(ocr_res.get("fields") or 0),
            "error": err,
            "elapsed_sec": elapsed,
            "case_dir": str(case_root(artifacts, fac, case)),
        }
    except Exception as exc:  # pragma: no cover
        return {
            "facility_id": fac,
            "case_id": case,
            "ok": False,
            "fields": 0,
            "error": str(exc),
            "elapsed_sec": round(time.perf_counter() - t0, 2),
            "case_dir": "",
        }


def _meta_for(cases_dir: Path, fac: str, case: str) -> tuple[str, str]:
    meta_path = cases_dir / fac / case / "meta.json"
    if not meta_path.is_file():
        return "", ""
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    return str(meta.get("patient_id") or ""), str(meta.get("patient_name") or "")


def _rewrite_ocr_csv(artifacts: Path, batch_id: str) -> Path:
    from snowflake_pull.case_export_aggregate import (  # noqa: WPS433
        iter_ocr_export_rows,
        write_ocr_export_csv,
    )

    cases_dir = artifacts / "cases"
    rows = iter_ocr_export_rows(cases_dir)
    out = artifacts / "reports" / f"case_export_ocr_{batch_id}.csv"
    write_ocr_export_csv(rows, out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    ap.add_argument("--batch-id", default="case_schedule_202601_202608")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--facility-id", default=None)
    ap.add_argument(
        "--export-every",
        type=int,
        default=100,
        help="Rewrite OCR CSV every N completed cases (0=end only)",
    )
    args = ap.parse_args()

    artifacts = Path(args.artifacts)
    cases_dir = artifacts / "cases"
    reports = artifacts / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    _setup_logging(reports / "ocr_batch.log")

    workers = max(1, int(args.workers))
    targets = discover_ocr_targets(
        cases_dir, limit=args.limit, facility_id=args.facility_id
    )
    log.info(
        "OCR batch start targets=%s workers=%s limit=%s dpi=%s",
        len(targets),
        workers,
        args.limit,
        args.dpi,
    )
    if not targets:
        log.info("Nothing to OCR (all done or no PDFs).")
        csv_path = _rewrite_ocr_csv(artifacts, args.batch_id)
        log.info("Wrote %s", csv_path)
        return 0

    payloads: list[dict[str, Any]] = []
    for fac, case in targets:
        pid, pname = _meta_for(cases_dir, fac, case)
        payloads.append(
            {
                "root": str(ROOT),
                "artifacts": str(artifacts),
                "facility_id": fac,
                "case_id": case,
                "patient_id": pid,
                "patient_name": pname,
                "dpi": args.dpi,
            }
        )

    done = 0
    fail = 0
    fields_hit = 0
    t_start = time.perf_counter()

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_ocr_one_case, p): p for p in payloads}
        for fut in as_completed(futures):
            res = fut.result()
            if res.get("ok"):
                done += 1
                if int(res.get("fields") or 0) > 0:
                    fields_hit += 1
            else:
                fail += 1
            total = done + fail
            if total % 10 == 0 or total == len(payloads):
                elapsed = time.perf_counter() - t_start
                rate = total / elapsed * 3600 if elapsed > 0 else 0
                log.info(
                    "progress %s/%s done=%s fail=%s with_fields=%s rate=%.0f/h last=%s/%s err=%s",
                    total,
                    len(payloads),
                    done,
                    fail,
                    fields_hit,
                    rate,
                    res.get("facility_id"),
                    res.get("case_id"),
                    (res.get("error") or "")[:80],
                )
            if (
                args.export_every
                and total % int(args.export_every) == 0
                and total > 0
            ):
                try:
                    path = _rewrite_ocr_csv(artifacts, args.batch_id)
                    log.info("Checkpoint OCR CSV %s", path)
                except Exception as exc:
                    log.warning("OCR CSV checkpoint failed: %s", exc)

    csv_path = _rewrite_ocr_csv(artifacts, args.batch_id)
    elapsed = time.perf_counter() - t_start
    summary = {
        "generated_at": _utc(),
        "targets": len(payloads),
        "done": done,
        "fail": fail,
        "with_fields": fields_hit,
        "workers": workers,
        "elapsed_sec": round(elapsed, 1),
        "ocr_csv": str(csv_path),
    }
    (reports / "ocr_batch_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    log.info("OCR batch finished %s", summary)
    print(json.dumps(summary, indent=2))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    # Windows ProcessPool requires __main__ guard
    raise SystemExit(main())

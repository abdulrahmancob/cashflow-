"""Link denial lines to exported Waystar claims (rejections list)."""

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from denial_categories import link_confidence_score
from denials_normalize import normalize_claim_id
from logging_config import get_logger, setup_logging

log = get_logger("link_denials")


def _norm(value: str) -> str:
    return " ".join((value or "").upper().split())


def _norm_date(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    parts = text.split("/")
    if len(parts) == 3 and len(parts[2]) == 2:
        parts[2] = "20" + parts[2]
    return "/".join(parts)


def load_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_claim_indexes(claims_rows: list[dict]) -> tuple[dict, dict]:
    by_claim_id: dict[str, dict] = {}
    by_fallback: dict[tuple[str, str, str], dict] = {}
    for row in claims_rows:
        claim_id = row.get("claim_id", "")
        if claim_id:
            by_claim_id[claim_id] = row
        key = (
            _norm(row.get("claim_number", "")),
            _norm(row.get("patient_name", "")),
            _norm_date(row.get("service_date", "")),
        )
        if any(key):
            by_fallback[key] = row
    return by_claim_id, by_fallback


def link_denials_to_claims(denial_rows: list[dict], claims_rows: list[dict]) -> tuple[list[dict], dict]:
    by_claim_id, by_fallback = build_claim_indexes(claims_rows)
    linked: list[dict] = []
    match_methods: Counter = Counter()

    for row in denial_rows:
        out = dict(row)
        claim = None
        method = "unmatched"

        claim_id = normalize_claim_id(row.get("claim_id", ""))
        if claim_id and claim_id in by_claim_id:
            claim = by_claim_id[claim_id]
            method = "claim_id"
        else:
            key = (
                _norm(row.get("claim_number", "")),
                _norm(row.get("patient_name", "")),
                _norm_date(row.get("service_date", "")),
            )
            if key in by_fallback:
                claim = by_fallback[key]
                method = "claim_number+patient+service_date"

        if claim:
            out["matched_claim_id"] = claim.get("claim_id", "")
            out["matched_claim_status"] = claim.get("status", "")
            out["matched_claim_status_detail"] = claim.get("status_detail", "")
            out["matched_claim_rejection_messages"] = claim.get("rejection_messages", "")
            out["had_pre_submission_rejection"] = (
                "rejected" in (claim.get("status") or "").lower()
            )
        else:
            out["matched_claim_id"] = ""
            out["matched_claim_status"] = ""
            out["matched_claim_status_detail"] = ""
            out["matched_claim_rejection_messages"] = ""
            out["had_pre_submission_rejection"] = False

        link_confidence, match_quality_score = link_confidence_score(method)
        out["link_method"] = method
        out["link_confidence"] = link_confidence
        out["match_quality_score"] = match_quality_score
        match_methods[method] += 1
        linked.append(out)

    report = {
        "linked_at": datetime.now(timezone.utc).isoformat(),
        "denial_rows": len(denial_rows),
        "claims_rows": len(claims_rows),
        "match_methods": dict(match_methods),
        "matched": sum(1 for r in linked if r.get("link_method") != "unmatched"),
        "unmatched": sum(1 for r in linked if r.get("link_method") == "unmatched"),
        "both_rejection_and_denial": sum(
            1 for r in linked if r.get("had_pre_submission_rejection") is True
        ),
    }
    return linked, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Link denial lines to claims export")
    parser.add_argument("--denials", type=Path, required=True, help="Denials lines CSV")
    parser.add_argument("--claims", type=Path, required=True, help="claims_rejected_all_merged.csv")
    parser.add_argument("--output", type=Path, default=None, help="Output CSV path")
    parser.add_argument("--report", type=Path, default=None, help="JSON link report path")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()
    setup_logging(level=args.log_level)

    denial_rows = load_csv(args.denials.resolve())
    claims_rows = load_csv(args.claims.resolve())
    linked, report = link_denials_to_claims(denial_rows, claims_rows)

    output_path = args.output or args.denials.with_name("denials_with_claims.csv")
    report_path = args.report or output_path.with_suffix(".link_report.json")

    if linked:
        fieldnames = list(dict.fromkeys(list(linked[0].keys())))
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(linked)

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(
        "Linked %s/%s rows → %s (report: %s)",
        report["matched"],
        report["denial_rows"],
        output_path,
        report_path,
    )
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

"""Transform raw denial CSVs into production-ready clean analytics files."""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from denial_categories import (
    categorize_resolution,
    final_outcome,
    is_preventable,
    link_confidence_score,
    recommended_action,
    recovery_priority,
    requires_appeal,
    resolution_success,
    root_cause_category,
    categorize_carc,
    recoverable_amount_line,
)
from denials_normalize import (
    aging_bucket,
    build_line_key,
    data_quality_flags,
    days_between,
    denial_status_from_stage,
    extract_carc_codes,
    extract_rarc_codes,
    format_money,
    is_reminder_overdue,
    normalize_claim_id,
    normalize_payer_fields,
    normalize_provider_fields,
    parse_appeal_level,
    parse_boolean,
    parse_carc_parts,
    parse_date_iso,
    parse_last_note,
    parse_money,
    parse_snapshot_date,
    sla_status,
    split_pipe_codes,
)
from link_denials_to_claims import link_denials_to_claims, load_csv
from logging_config import get_logger, setup_logging

log = get_logger("transform_denials")

MERGED_CLEAN_FIELDS = [
    "denial_id",
    "snapshot_date",
    "claim_id",
    "claim_instance_id",
    "claim_number",
    "claim_type",
    "can_resubmit",
    "account",
    "patient_name",
    "pop_score",
    "service_date",
    "denial_date",
    "rendering_provider_name",
    "rendering_provider_npi",
    "billing_provider_name",
    "billing_provider_npi",
    "payer_name",
    "payer_id",
    "denied_amount",
    "allowed_amount",
    "billed_amount",
    "denial_category",
    "reminder_date",
    "stage",
    "denial_status",
    "days_in_phase",
    "days_since_denial",
    "aging_bucket",
    "is_reminder_overdue",
    "appeal_level_num",
    "level_of_appeal",
    "service_facility",
    "last_note_date",
    "last_note_text",
    "sla_status",
    "escalation_flag",
    "open_line_count",
    "recoverable_amount",
    "requires_appeal",
    "recommended_action",
    "recovery_priority",
    "data_quality_flags",
    "scraped_at",
]

LINE_FACT_FIELDS = [
    "line_key",
    "denial_id",
    "claim_id",
    "claim_number",
    "patient_name",
    "snapshot_date",
    "overview_active_remit_count",
    "remit_id",
    "remit_number",
    "remit_instance_id",
    "remit_active",
    "is_current_remit",
    "remit_received_date",
    "remit_payer_name",
    "remit_payer_id",
    "line_num",
    "proc_code",
    "modifiers",
    "rarc_codes_clean",
    "remark_descriptions",
    "billed_amount",
    "allowed_amount",
    "paid_amount",
    "carc_codes",
    "carc_group",
    "carc_code_num",
    "carc_descriptions",
    "adjustment_amount",
    "adjustment_status",
    "is_actionable_line",
    "recoverable_amount_line",
    "carc_category",
    "root_cause_category",
    "is_preventable",
    "requires_appeal_line",
    "recommended_action_line",
    "resolution_action",
    "resolution_date",
    "resolution_rule",
    "resolution_payer",
    "resolution_success",
    "final_outcome",
    "resolution_category",
    "days_to_resolution",
    "payer_name",
    "payer_id",
    "denial_category",
    "stage",
    "detail_fetch_error",
]

LINE_SLIM_FIELDS = [
    "line_key",
    "denial_id",
    "claim_id",
    "claim_number",
    "remit_instance_id",
    "line_num",
    "proc_code",
    "modifiers",
    "carc_codes",
    "carc_category",
    "adjustment_amount",
    "adjustment_status",
    "is_actionable_line",
    "recoverable_amount_line",
    "final_outcome",
    "recommended_action_line",
]

LINK_FIELDS = [
    "matched_claim_id",
    "matched_claim_status",
    "matched_claim_status_detail",
    "matched_claim_rejection_messages",
    "had_pre_submission_rejection",
    "link_method",
    "link_confidence",
    "match_quality_score",
]


def _is_actionable(remit_active: Any, adjustment_status: str) -> bool:
    return parse_boolean(remit_active) and (adjustment_status or "").strip() == "Active"


def _transform_line_row(row: dict[str, Any], snapshot_date: str) -> dict[str, Any]:
    payer = normalize_payer_fields(row)
    carc_codes_raw = " || ".join(extract_carc_codes(str(row.get("carc_codes", ""))))
    rarc_codes_clean = " || ".join(
        extract_rarc_codes(str(row.get("remark_codes", "")))
        or split_pipe_codes(str(row.get("remark_codes", "")))
    )
    carc_group, carc_code_num = parse_carc_parts(carc_codes_raw)
    remit_active = parse_boolean(row.get("remit_active"))
    adjustment_status = (row.get("adjustment_status") or "").strip()
    resolution_action = (row.get("resolution_action") or "").strip()
    adjustment_amount = parse_money(row.get("adjustment_amount"))
    actionable = _is_actionable(remit_active, adjustment_status)
    remit_received_iso = parse_date_iso(row.get("remit_received_date"))
    resolution_date_iso = parse_date_iso(row.get("resolution_date"))

    out = {
        "line_key": build_line_key(
            str(row.get("denial_id", "")),
            str(row.get("remit_instance_id", "")),
            row.get("line_num", ""),
        ),
        "denial_id": row.get("denial_id", ""),
        "claim_id": normalize_claim_id(row.get("claim_id")),
        "claim_number": row.get("claim_number", ""),
        "patient_name": row.get("patient_name", ""),
        "snapshot_date": snapshot_date,
        "overview_active_remit_count": row.get("overview_active_remit_count", ""),
        "remit_id": row.get("remit_id", ""),
        "remit_number": row.get("remit_number", ""),
        "remit_instance_id": row.get("remit_instance_id", ""),
        "remit_active": remit_active,
        "is_current_remit": False,
        "remit_received_date": remit_received_iso,
        "remit_payer_name": payer["remit_payer_name"],
        "remit_payer_id": payer["remit_payer_id"],
        "line_num": row.get("line_num", ""),
        "proc_code": row.get("proc_code", ""),
        "modifiers": row.get("modifiers", ""),
        "rarc_codes_clean": rarc_codes_clean,
        "remark_descriptions": row.get("remark_descriptions", ""),
        "billed_amount": format_money(row.get("billed_amount")),
        "allowed_amount": format_money(row.get("allowed_amount")),
        "paid_amount": format_money(row.get("paid_amount")),
        "carc_codes": carc_codes_raw,
        "carc_group": carc_group,
        "carc_code_num": carc_code_num,
        "carc_descriptions": row.get("carc_descriptions", ""),
        "adjustment_amount": format_money(adjustment_amount),
        "adjustment_status": adjustment_status,
        "is_actionable_line": actionable,
        "recoverable_amount_line": format_money(recoverable_amount_line(adjustment_amount, actionable)),
        "carc_category": categorize_carc(carc_codes_raw, rarc_codes_clean),
        "root_cause_category": root_cause_category(
            carc_codes_raw, rarc_codes_clean, str(row.get("denial_category", ""))
        ),
        "is_preventable": is_preventable(
            carc_codes_raw, rarc_codes_clean, str(row.get("denial_category", ""))
        ),
        "requires_appeal_line": requires_appeal(
            str(row.get("stage", "")), carc_codes_raw, adjustment_status
        ),
        "recommended_action_line": recommended_action(
            stage=str(row.get("stage", "")),
            carc_codes=carc_codes_raw,
            adjustment_status=adjustment_status,
            can_resubmit=parse_boolean(row.get("can_resubmit")),
            resolution_action=resolution_action,
        ),
        "resolution_action": resolution_action,
        "resolution_date": resolution_date_iso,
        "resolution_rule": row.get("resolution_rule", ""),
        "resolution_payer": row.get("resolution_payer", ""),
        "resolution_success": resolution_success(resolution_action, adjustment_status),
        "final_outcome": final_outcome(resolution_action, adjustment_status, remit_active),
        "resolution_category": categorize_resolution(resolution_action, adjustment_status),
        "days_to_resolution": days_between(remit_received_iso, resolution_date_iso) or "",
        "payer_name": payer["payer_name"],
        "payer_id": payer["payer_id"],
        "denial_category": row.get("denial_category", ""),
        "stage": row.get("stage", ""),
        "detail_fetch_error": row.get("detail_fetch_error", ""),
    }
    return out


def _mark_current_remits(lines: list[dict[str, Any]]) -> None:
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for row in lines:
        if not row.get("is_actionable_line"):
            continue
        key = (str(row.get("denial_id", "")), str(row.get("proc_code", "")))
        received = str(row.get("remit_received_date", ""))
        current = latest.get(key)
        if current is None or received > current[0]:
            latest[key] = (received, str(row.get("line_key", "")))

    current_keys = {entry[1] for entry in latest.values()}
    for row in lines:
        row["is_current_remit"] = row.get("line_key", "") in current_keys


def _rollup_lines(lines: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    rollups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"open_line_count": 0, "recoverable_amount": 0.0}
    )
    for row in lines:
        denial_id = str(row.get("denial_id", ""))
        if row.get("is_actionable_line"):
            rollups[denial_id]["open_line_count"] += 1
            rollups[denial_id]["recoverable_amount"] += parse_money(row.get("recoverable_amount_line"))
    return rollups


def _transform_merged_row(
    row: dict[str, Any],
    snapshot_date: str,
    rollup: dict[str, Any] | None,
) -> dict[str, Any]:
    payer = normalize_payer_fields(row)
    provider = normalize_provider_fields(row)
    note_date, note_text = parse_last_note(str(row.get("last_note", "")))
    denial_date_iso = parse_date_iso(row.get("denial_date"))
    reminder_date_iso = parse_date_iso(row.get("reminder_date"))
    days_since = days_between(denial_date_iso, snapshot_date)
    stage = str(row.get("stage", ""))
    recoverable = rollup["recoverable_amount"] if rollup else 0.0
    open_lines = rollup["open_line_count"] if rollup else 0
    appeal_needed = requires_appeal(stage)
    recommended = recommended_action(
        stage=stage,
        can_resubmit=parse_boolean(row.get("can_resubmit")),
    )
    overdue = is_reminder_overdue(reminder_date_iso, snapshot_date)
    aging = aging_bucket(days_since)
    sla = sla_status(stage, row.get("days_in_phase"))
    escalation = overdue or aging == "90+" or sla == "Breached"

    out = {
        "denial_id": row.get("denial_id", ""),
        "snapshot_date": snapshot_date,
        "claim_id": normalize_claim_id(row.get("claim_id")),
        "claim_instance_id": row.get("instance_id", ""),
        "claim_number": row.get("claim_number", ""),
        "claim_type": row.get("claim_type", ""),
        "can_resubmit": parse_boolean(row.get("can_resubmit")),
        "account": row.get("account", ""),
        "patient_name": row.get("patient_name", ""),
        "pop_score": row.get("pop_score", ""),
        "service_date": parse_date_iso(row.get("service_date")),
        "denial_date": denial_date_iso,
        "rendering_provider_name": provider["rendering_provider_name"],
        "rendering_provider_npi": provider["rendering_provider_npi"],
        "billing_provider_name": provider["billing_provider_name"],
        "billing_provider_npi": provider["billing_provider_npi"],
        "payer_name": payer["payer_name"],
        "payer_id": payer["payer_id"],
        "denied_amount": format_money(row.get("denied_amount")),
        "allowed_amount": format_money(row.get("allowed_amount")),
        "billed_amount": format_money(row.get("billed_amount")),
        "denial_category": row.get("denial_category", ""),
        "reminder_date": reminder_date_iso,
        "stage": stage,
        "denial_status": denial_status_from_stage(stage),
        "days_in_phase": row.get("days_in_phase", ""),
        "days_since_denial": days_since if days_since is not None else "",
        "aging_bucket": aging,
        "is_reminder_overdue": overdue,
        "appeal_level_num": parse_appeal_level(str(row.get("level_of_appeal", ""))) or "",
        "level_of_appeal": row.get("level_of_appeal", ""),
        "service_facility": row.get("service_facility", ""),
        "last_note_date": note_date,
        "last_note_text": note_text,
        "sla_status": sla,
        "escalation_flag": escalation,
        "open_line_count": open_lines,
        "recoverable_amount": format_money(recoverable),
        "requires_appeal": appeal_needed,
        "recommended_action": recommended,
        "recovery_priority": recovery_priority(recoverable, days_since, appeal_needed),
        "data_quality_flags": "",
        "scraped_at": row.get("scraped_at", ""),
    }
    out["data_quality_flags"] = data_quality_flags(out)
    return out


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %s rows → %s", len(rows), path)


def _line_row_rank(row: dict[str, Any]) -> tuple[int, int]:
    """Higher rank = preferred when deduplicating duplicate line_keys."""
    remit_active = 1 if parse_boolean(row.get("remit_active")) else 0
    actionable = 1 if (row.get("adjustment_status") or "").strip() == "Active" else 0
    return (remit_active, actionable)


def transform_denials(
    merged_path: Path,
    lines_path: Path | None = None,
    claims_path: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Path]:
    merged_rows = load_csv(merged_path)
    out_dir = output_dir or merged_path.parent
    snapshot_date = parse_snapshot_date(
        merged_rows[0].get("scraped_at") if merged_rows else ""
    )

    fact_rows: list[dict[str, Any]] = []
    if lines_path and lines_path.exists():
        raw_lines = load_csv(lines_path)
        seen_keys: dict[str, dict[str, Any]] = {}
        for row in raw_lines:
            transformed = _transform_line_row(row, snapshot_date)
            key = transformed["line_key"]
            existing = seen_keys.get(key)
            if existing is None or _line_row_rank(transformed) > _line_row_rank(existing):
                seen_keys[key] = transformed
        fact_rows = list(seen_keys.values())
        _mark_current_remits(fact_rows)
        rollups = _rollup_lines(fact_rows)
    else:
        rollups = {}

    merged_clean = [
        _transform_merged_row(row, snapshot_date, rollups.get(str(row.get("denial_id", ""))))
        for row in merged_rows
    ]

    outputs: dict[str, Path] = {}
    merged_clean_path = out_dir / "denials_merged_clean.csv"
    _write_csv(merged_clean_path, MERGED_CLEAN_FIELDS, merged_clean)
    outputs["merged_clean"] = merged_clean_path

    if fact_rows:
        fact_path = out_dir / "denials_lines_fact.csv"
        slim_path = out_dir / "denials_lines_slim.csv"
        _write_csv(fact_path, LINE_FACT_FIELDS, fact_rows)
        _write_csv(slim_path, LINE_SLIM_FIELDS, fact_rows)
        outputs["lines_fact"] = fact_path
        outputs["lines_slim"] = slim_path

        if claims_path and claims_path.exists():
            claims_rows = load_csv(claims_path)
            linked, report = link_denials_to_claims(fact_rows, claims_rows)
            linked_path = out_dir / "denials_with_claims_clean.csv"
            link_fieldnames = list(dict.fromkeys([*LINE_FACT_FIELDS, *LINK_FIELDS]))
            _write_csv(linked_path, link_fieldnames, linked)
            report_path = out_dir / "denials_with_claims_clean.link_report.json"
            import json

            report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            outputs["with_claims_clean"] = linked_path
            outputs["link_report"] = report_path

    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Transform raw denial CSVs into clean analytics files")
    parser.add_argument("--merged", type=Path, required=True, help="denials_merged.csv")
    parser.add_argument("--lines", type=Path, default=None, help="denials_lines.csv")
    parser.add_argument("--claims", type=Path, default=None, help="claims export for lifecycle linking")
    parser.add_argument("--output-dir", type=Path, default=None, help="Output directory")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    args = parser.parse_args()
    setup_logging(level=args.log_level)

    outputs = transform_denials(
        args.merged.resolve(),
        args.lines.resolve() if args.lines else None,
        args.claims.resolve() if args.claims else None,
        args.output_dir.resolve() if args.output_dir else None,
    )
    for name, path in outputs.items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()

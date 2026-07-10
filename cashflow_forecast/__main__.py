"""CLI: python -m cashflow_forecast sla|build."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from cashflow_forecast.aggregations import (
    denied_by_insurance,
    outcome_stage_counts,
    overdue_by_insurance,
    risk_by_insurance,
)
from cashflow_forecast.audit_linker import link_audit_to_waystar
from cashflow_forecast.config import DEFAULT_AS_OF, REPO_ROOT
from cashflow_forecast.export import export_all
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.forecast_engine import (
    actual_cash_buckets,
    kpi_summary,
    projected_cash_buckets,
)
from cashflow_forecast.loaders import (
    load_audit,
    load_denials,
    load_patients,
    load_payments_unified,
    load_reconciliation_lines,
    load_rejections,
)
from cashflow_forecast.outcome_stages import classify_outcomes
from cashflow_forecast.payer_sla import build_payer_sla, sla_lookup, write_payer_sla
from cashflow_forecast.risk_flags import build_risk_flags

log = logging.getLogger("cashflow_forecast")


def _resolve_path(path: str | Path) -> Path:
    """Resolve relative paths against repo root (works from any cwd)."""
    p = Path(path)
    if p.is_absolute():
        return p
    return REPO_ROOT / p


def _parse_as_of(text: str | None) -> date:
    if not text:
        return DEFAULT_AS_OF
    return datetime.strptime(text, "%Y-%m-%d").date()


def cmd_sla(args: argparse.Namespace) -> int:
    recon_dir = _resolve_path(args.reconciliation_dir)
    lines_path = recon_dir / "reconciliation_lines.csv"
    if not lines_path.exists():
        log.error("Missing %s", lines_path)
        return 1
    lines = load_reconciliation_lines(lines_path)
    sla = build_payer_sla(lines)
    out = _resolve_path(args.output)
    write_payer_sla(sla, out)
    log.info("Wrote %s (%d payers)", out, len(sla))
    print(sla.head(15).to_string(index=False))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    data_dir = _resolve_path(args.data_dir)
    output_dir = _resolve_path(args.output_dir)
    as_of = _parse_as_of(args.as_of)

    recon_dir = data_dir / "reconciliation"
    audit_dir = data_dir / "audit"
    lines_path = recon_dir / "reconciliation_lines.csv"
    payments_path = recon_dir / "payments_unified.csv"

    if not lines_path.exists():
        log.error("Missing %s", lines_path)
        return 1

    log.info("Loading reconciliation lines…")
    lines = load_reconciliation_lines(lines_path)
    log.info("  %d lines", len(lines))

    payments = (
        load_payments_unified(payments_path) if payments_path.exists() else lines.iloc[0:0].copy()
    )
    log.info("Loading payments_unified… %d rows", len(payments))

    # Optional Waystar / audit paths
    rejections_path = _resolve_path(
        args.rejections
        or REPO_ROOT / "waystar_scraper/output/claims_rejected_all/claims_rejected_all_merged.csv"
    )
    denials_path = _resolve_path(
        args.denials or REPO_ROOT / "waystar_scraper/output/denials_2026_all"
    )

    rejections = load_rejections(rejections_path) if rejections_path.exists() else None
    denials = load_denials(denials_path) if denials_path.exists() else None
    audit = load_audit(audit_dir) if audit_dir.exists() else None

    log.info(
        "Loaded rejections=%s denials=%s audit=%s",
        len(rejections) if rejections is not None else 0,
        len(denials) if denials is not None else 0,
        len(audit) if audit is not None else 0,
    )

    # Patients optional
    patients = load_patients(data_dir)
    log.info("Patients: %d", len(patients))

    # SLA
    sla = build_payer_sla(lines)
    lookup = sla_lookup(sla)
    write_payer_sla(sla, output_dir / "payer_sla.csv")

    # Fees
    fees = FeeEstimator.from_paid_lines(lines)

    # Outcomes (no risk mixed in)
    log.info("Classifying outcomes as_of=%s…", as_of)
    outcomes = classify_outcomes(
        lines,
        sla_lookup=lookup,
        fee_estimator=fees,
        rejections=rejections,
        denials=denials,
        as_of=as_of,
    )

    # Risk flags (parallel)
    risk = build_risk_flags(
        outcomes,
        audit if audit is not None else __import__("pandas").DataFrame(),
        fee_estimator=fees,
        as_of=as_of,
    )

    # Audit linker scoring
    matches = link_audit_to_waystar(
        audit if audit is not None else __import__("pandas").DataFrame(),
        denials if denials is not None else __import__("pandas").DataFrame(),
        rejections,
    )

    actual = actual_cash_buckets(payments)
    projected = projected_cash_buckets(outcomes)
    kpi = kpi_summary(outcomes, payments, risk, as_of=as_of)

    artifacts = {
        "payer_sla": sla,
        "outcome_stages": outcomes,
        "risk_flags": risk,
        "audit_denial_matches": matches,
        "actual_cash_daily": actual["daily"],
        "actual_cash_weekly": actual["weekly"],
        "actual_cash_monthly": actual["monthly"],
        "projected_cash_daily": projected["daily"],
        "projected_cash_weekly": projected["weekly"],
        "projected_cash_monthly": projected["monthly"],
        "overdue_by_insurance": overdue_by_insurance(outcomes),
        "denied_by_insurance": denied_by_insurance(outcomes),
        "risk_by_insurance": risk_by_insurance(risk),
        "outcome_stage_counts": outcome_stage_counts(outcomes),
        "kpi_summary": kpi,
    }
    export_all(output_dir, artifacts)

    log.info("KPI summary: %s", kpi)
    print(f"\nWrote forecast outputs -> {output_dir}")
    print(f"  Actual cash:    ${kpi['actual_cash_received']:,.2f}")
    print(f"  Projected:      ${kpi['projected_cash_in']:,.2f}")
    print(f"  On track:       ${kpi['on_track_amount']:,.2f} ({kpi['on_track_count']} lines)")
    print(f"  Overdue:        ${kpi['overdue_amount']:,.2f} ({kpi['overdue_count']} lines)")
    print(f"  Denied+Reject:  ${kpi['denied_amount']:,.2f} ({kpi['denied_count']} lines)")
    print(f"  Risk exposure:  ${kpi['risk_exposure_amount']:,.2f} ({kpi['risk_visit_count']} visits)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cashflow_forecast", description="Cash flow forecasting pilot")
    sub = p.add_subparsers(dest="command", required=True)

    sla = sub.add_parser("sla", help="Build payer SLA table from reconciliation")
    sla.add_argument(
        "--reconciliation-dir",
        default=str(REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026/reconciliation"),
    )
    sla.add_argument(
        "--output",
        default=str(REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026/forecast/payer_sla.csv"),
    )
    sla.set_defaults(func=cmd_sla)

    build = sub.add_parser("build", help="Run full forecast pipeline")
    build.add_argument(
        "--data-dir",
        default=str(REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026"),
    )
    build.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026/forecast"),
    )
    build.add_argument("--as-of", default=None, help="YYYY-MM-DD (default from config)")
    build.add_argument("--rejections", default=None)
    build.add_argument("--denials", default=None)
    build.set_defaults(func=cmd_build)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

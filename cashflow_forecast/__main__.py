"""CLI: python -m cashflow_forecast sla|build."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from cashflow_forecast.aggregations import (
    denied_by_insurance,
    outcome_stage_counts,
    overdue_by_insurance,
    risk_by_insurance,
)
from cashflow_forecast.audit_linker import link_audit_to_waystar
from cashflow_forecast.config import DEFAULT_AS_OF, REPO_ROOT
from cashflow_forecast.deposit_capacity import (
    build_deposit_events_from_actual,
    build_deposit_events_from_checks,
    pack_pastdue_ffd,
)
from cashflow_forecast.export import export_all
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.forecast_engine import (
    actual_cash_buckets,
    actual_cash_buckets_by_facility,
    actual_cash_buckets_by_insurance,
    actual_cash_buckets_from_deposits,
    filter_period_to_window,
    kpi_summary,
    projected_cash_buckets,
    projected_cash_buckets_by_facility,
    projected_cash_buckets_by_insurance,
    projected_cash_monthly_by_facility_insurance,
)
from cashflow_forecast.forward_volume import (
    attach_forward_expected_amounts,
    build_august_forward_lines,
)
from cashflow_forecast.insurance_behavior_sla import (
    cash_velocity_lookup_from_rows,
    deposit_schedule_lookup_from_rows,
    eob_to_deposit_lookup_from_rows,
    load_cash_velocity_lookup,
    load_deposit_schedule_lookup,
    load_eob_to_deposit_lookup,
    merge_velocity_into_lookup,
)
from cashflow_forecast.payer_payment_model import (
    apply_visit_expected_amounts,
    learn_payment_models,
    payment_models_to_frame,
    write_payment_models,
)
from cashflow_forecast.sf_visit_overrides import (
    apply_sf_visit_overrides,
    load_sf_override_keys,
    resolve_override_path,
)
from cashflow_forecast.land_accuracy import (
    build_land_accuracy_frame,
    summarize_land_accuracy,
)
from cashflow_forecast.loaders import (
    load_audit,
    load_denials,
    load_patients,
    load_payments_unified,
    load_reconciliation_lines,
    load_rejections,
)
from cashflow_forecast.loaders.load_extracted import (
    load_cpt_codes,
    load_daily_notes,
    load_may_ar_lines,
    load_plans_of_care,
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


def _ensure_recon_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Align optional columns so concat with recon lines works."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if "source" not in out.columns:
        out["source"] = "reconciliation"
    if "units" not in out.columns:
        out["units"] = 1.0
    return out


def cmd_sla(args: argparse.Namespace) -> int:
    if getattr(args, "from_db", False):
        from cashflow_forecast.db_source import load_reconciliation_lines_df, write_forecast_run

        lines = load_reconciliation_lines_df()
        if lines.empty:
            log.error("No reconciliation_line rows in DB — run reconcile --from-db first")
            return 1
        sla = build_payer_sla(lines)
        write_forecast_run(
            algorithm_version="sla-only",
            as_of_date=_parse_as_of(None),
            outcome_df=pd.DataFrame(),
            feature_tables={"payer_sla": sla},
            rules_version="sla",
        )
        if getattr(args, "emit_csv", False):
            out = _resolve_path(args.output)
            write_payer_sla(sla, out)
            log.info("Wrote diagnostic CSV %s (%d payers)", out, len(sla))
        print(sla.head(15).to_string(index=False))
        return 0

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
    from_db = getattr(args, "from_db", False)
    emit_csv = getattr(args, "emit_csv", False) or not from_db
    data_dir = _resolve_path(args.data_dir)
    output_dir = _resolve_path(args.output_dir)
    as_of = _parse_as_of(args.as_of)

    recon_dir = data_dir / "reconciliation"
    audit_dir = data_dir / "audit"
    extracted_dir = data_dir / "extracted"

    if from_db:
        from cashflow_forecast import db_source as dbs

        log.info("Loading reconciliation lines from DB…")
        recon_lines = _ensure_recon_columns(dbs.load_reconciliation_lines_df())
        if recon_lines.empty:
            log.error("No reconciliation_line rows — run: python -m cashflow_reconcile --from-db")
            return 1
        log.info("  %d recon lines", len(recon_lines))
        may_lines = dbs.load_clinical_ar_lines_df(
            service_from=date(2026, 1, 1),
            service_to=date(2026, 5, 31),
        )
        may_lines = _ensure_recon_columns(may_lines) if not may_lines.empty else may_lines
        patients = dbs.load_patients_df()
        log.info("Patients: %d", len(patients))
        sla = build_payer_sla(recon_lines)
        # Prefer feature_store payor velocity when snapshots exist for as_of
        try:
            from cashflow_db.repository import connection, features

            with connection() as conn:
                snaps = features.get_features(
                    conn,
                    as_of_date=as_of,
                    feature_keys=["payor.avg_cash_velocity_days"],
                )
            if snaps and not sla.empty and "payer" in sla.columns:
                vel = {
                    (s.get("entity_key") or "").replace("payer=", ""): s.get("value_num")
                    for s in snaps
                    if s.get("value_num") is not None
                }
                if vel and "cash_velocity_days" in sla.columns:
                    sla = sla.copy()
                    sla["cash_velocity_days"] = sla.apply(
                        lambda r: vel.get(str(r.get("payer")), r.get("cash_velocity_days")),
                        axis=1,
                    )
                    log.info("Merged %d feature_store payor velocity rows into SLA", len(vel))
        except Exception as exc:  # noqa: BLE001
            log.debug("feature_store SLA merge skipped: %s", exc)
        lookup = sla_lookup(sla)
        ib_df = dbs.load_payor_behavior_df()
        ib_rows = ib_df.to_dict(orient="records") if not ib_df.empty else []
        velocity = cash_velocity_lookup_from_rows(ib_rows)
        deposit_schedules = deposit_schedule_lookup_from_rows(ib_rows)
        eob_to_deposit = eob_to_deposit_lookup_from_rows(ib_rows)
        if velocity:
            lookup = merge_velocity_into_lookup(lookup, velocity)
            log.info("Merged insurance_behavior cash velocity for %d keys (DB)", len(velocity))
        fees = FeeEstimator.from_paid_lines(recon_lines)
        forward_lines = pd.DataFrame()
        forward_summary = pd.DataFrame()
        plans = dbs.load_plans_of_care_df()
        if not plans.empty:
            log.info("Building August forward volume from PoC (DB)…")
            notes = dbs.load_clinical_ar_lines_df()  # reuse grain; forward uses plans+patients
            forward_lines, forward_summary = build_august_forward_lines(
                plans,
                patients,
                notes.iloc[0:0],
                notes.iloc[0:0],
                fee_estimator=fees,
            )
        frames = [recon_lines]
        if not may_lines.empty:
            frames.append(may_lines)
        if not forward_lines.empty:
            frames.append(forward_lines)
        lines = _ensure_recon_columns(pd.concat(frames, ignore_index=True, sort=False))
        payments = dbs.load_payments_unified_df()
        visits_df = dbs.load_reconciliation_visits_df()
        if not visits_df.empty and "visit_paid_total" in visits_df.columns:
            visits_df["visit_paid_total"] = pd.to_numeric(
                visits_df["visit_paid_total"], errors="coerce"
            ).fillna(0)
        denials_all = dbs.load_denials_df()
        if denials_all.empty:
            denials = None
            rejections = None
        elif "source" in denials_all.columns:
            rejections = denials_all[denials_all["source"] == "rejection"]
            denials = denials_all[denials_all["source"] != "rejection"]
            if rejections.empty:
                rejections = None
            if denials.empty:
                denials = None
        else:
            denials = denials_all
            rejections = None
        audit = dbs.load_audit_df()
        if audit is not None and audit.empty:
            audit = None
        pay_catalog = learn_payment_models(
            recon_lines,
            payments_unified=payments if not payments.empty else None,
            visits=visits_df if not visits_df.empty else None,
            fee_estimator=fees,
        )
        lines = apply_visit_expected_amounts(lines, pay_catalog)
        actual = (
            actual_cash_buckets(payments)
            if not payments.empty
            else {"daily": pd.DataFrame(), "weekly": pd.DataFrame(), "monthly": pd.DataFrame()}
        )
        actual_ins = (
            actual_cash_buckets_by_insurance(payments)
            if not payments.empty
            else {"daily": pd.DataFrame(), "weekly": pd.DataFrame(), "monthly": pd.DataFrame()}
        )
        deposits = dbs.load_deposits_df()
        tracker_total: float | None = None
        if not deposits.empty:
            actual = actual_cash_buckets_from_deposits(deposits)
            tracker_total = float(deposits["amount"].sum())
            log.info("Actual cash from bank_deposit: %d rows / $%.2f", len(deposits), tracker_total)
        deposit_events = build_deposit_events_from_actual(actual_ins["daily"], as_of=as_of)
        if not deposit_events:
            checks_df = dbs.load_checks_timeline_df()
            if not checks_df.empty:
                for col in ("deposit_date", "eob_date"):
                    if col in checks_df.columns:
                        checks_df[col] = pd.to_datetime(checks_df[col], errors="coerce").dt.date
                amt_col = "paid_amount" if "paid_amount" in checks_df.columns else "paid_amount_sum"
                checks_df["paid_amount_sum"] = pd.to_numeric(
                    checks_df.get(amt_col), errors="coerce"
                ).fillna(0)
                deposit_events = build_deposit_events_from_checks(checks_df, as_of=as_of)
    else:
        lines_path = recon_dir / "reconciliation_lines.csv"
        payments_path = recon_dir / "payments_unified.csv"
        if not lines_path.exists():
            log.error("Missing %s", lines_path)
            return 1

        log.info("Loading reconciliation lines…")
        recon_lines = _ensure_recon_columns(load_reconciliation_lines(lines_path))
        log.info("  %d recon lines", len(recon_lines))

        # Propagate SF paid/denied visit overrides onto lines (before SLA/fees/classify)
        override_path = resolve_override_path(recon_dir)
        if override_path is not None:
            overrides = load_sf_override_keys(override_path)
            log.info("SF visit overrides loaded from %s (%d keys)", override_path.name, len(overrides))
            if overrides:
                recon_lines = apply_sf_visit_overrides(recon_lines, overrides)
                recon_lines = _ensure_recon_columns(recon_lines)

        # Jan–May AR from extracted CPT (no overlap with recon Jun–Jul DOS window)
        may_lines = pd.DataFrame()
        if extracted_dir.exists():
            log.info("Loading Jan–May AR from extracted…")
            may_lines = load_may_ar_lines(extracted_dir)
            log.info("  %d extracted AR lines", len(may_lines))

        # Patients for Aug enrichment
        patients = load_patients(data_dir)
        log.info("Patients: %d", len(patients))

        # Fees + SLA from paid recon first (needed for Aug forward $)
        sla = build_payer_sla(recon_lines)
        lookup = sla_lookup(sla)
        if emit_csv:
            write_payer_sla(sla, output_dir / "payer_sla.csv")

        # Prefer DOS→deposit cash velocity + deposit weekday schedule from insurance_behavior
        ib_summary = recon_dir / "insurance_behavior" / "payor_behavior_summary.csv"
        velocity = load_cash_velocity_lookup(ib_summary)
        deposit_schedules = load_deposit_schedule_lookup(ib_summary)
        eob_to_deposit = load_eob_to_deposit_lookup(ib_summary)
        if velocity:
            lookup = merge_velocity_into_lookup(lookup, velocity)
            output_dir.mkdir(parents=True, exist_ok=True)
            dest = output_dir / "payor_behavior_summary.csv"
            dest.write_bytes(ib_summary.read_bytes())
            log.info(
                "Merged insurance_behavior cash velocity for %d keys (from %s)",
                len(velocity),
                ib_summary.name,
            )
        else:
            log.info("No insurance_behavior cash velocity at %s", ib_summary)
        if deposit_schedules:
            log.info(
                "Loaded deposit weekday schedules for %d keys (snap weekly/multi cadence)",
                len(deposit_schedules),
            )
        else:
            log.info("No deposit weekday schedules at %s", ib_summary)
        if eob_to_deposit:
            log.info(
                "Loaded EOB→deposit lags for %d keys (land = eob + lag then cadence snap)",
                len(eob_to_deposit),
            )

        fees = FeeEstimator.from_paid_lines(recon_lines)

        # August forward volume from PoC
        forward_lines = pd.DataFrame()
        forward_summary = pd.DataFrame()
        if extracted_dir.exists():
            poc_path = extracted_dir / "plans_of_care.csv"
            notes_path = extracted_dir / "daily_notes.csv"
            cpt_path = extracted_dir / "cpt_codes.csv"
            if poc_path.exists():
                log.info("Building August forward volume from Plans of Care…")
                plans = load_plans_of_care(poc_path)
                notes = load_daily_notes(notes_path) if notes_path.exists() else pd.DataFrame()
                cpt = load_cpt_codes(cpt_path) if cpt_path.exists() else pd.DataFrame()
                forward_lines, forward_summary = build_august_forward_lines(
                    plans, patients, notes, cpt, fee_estimator=fees
                )
                log.info(
                    "  %d forward lines (%d patients)",
                    len(forward_lines),
                    forward_summary["webpt_patient_id"].nunique() if not forward_summary.empty else 0,
                )

        frames = [recon_lines]
        if not may_lines.empty:
            frames.append(may_lines)
        if not forward_lines.empty:
            frames.append(forward_lines)
        lines = pd.concat(frames, ignore_index=True, sort=False)
        lines = _ensure_recon_columns(lines)
        log.info("Combined lines: %d", len(lines))

        payments = (
            load_payments_unified(payments_path) if payments_path.exists() else lines.iloc[0:0].copy()
        )
        log.info("Loading payments_unified… %d rows", len(payments))

        visits_path = recon_dir / "reconciliation_visits.csv"
        visits_df = (
            pd.read_csv(visits_path, dtype=str, keep_default_na=False)
            if visits_path.exists()
            else pd.DataFrame()
        )
        if not visits_df.empty and "visit_paid_total" in visits_df.columns:
            visits_df["visit_paid_total"] = pd.to_numeric(
                visits_df["visit_paid_total"], errors="coerce"
            ).fillna(0)
            if "date_of_service" in visits_df.columns:
                visits_df["date_of_service"] = pd.to_datetime(
                    visits_df["date_of_service"], errors="coerce"
                ).dt.date

        log.info("Learning payer_plan payment models…")
        pay_catalog = learn_payment_models(
            recon_lines,
            payments_unified=payments if not payments.empty else None,
            visits=visits_df if not visits_df.empty else None,
            fee_estimator=fees,
        )
        log.info("  %d payment models", len(pay_catalog.models))
        before_pre = int(lines["precomputed_expected"].notna().sum()) if "precomputed_expected" in lines.columns else 0
        lines = apply_visit_expected_amounts(lines, pay_catalog)
        after_pre = int(pd.to_numeric(lines.get("precomputed_expected"), errors="coerce").notna().sum())
        log.info("  visit expected applied (%d → %d precomputed rows)", before_pre, after_pre)

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

        # Actual cash + deposit events before classify (weekday spill weights need history)
        actual = actual_cash_buckets(payments) if not payments.empty else {
            "daily": pd.DataFrame(),
            "weekly": pd.DataFrame(),
            "monthly": pd.DataFrame(),
        }
        actual_ins = (
            actual_cash_buckets_by_insurance(payments)
            if not payments.empty
            else {"daily": pd.DataFrame(), "weekly": pd.DataFrame(), "monthly": pd.DataFrame()}
        )

        tracker_override = getattr(args, "transaction_tracker", None)
        tracker_path = (
            _resolve_path(tracker_override) if tracker_override else None
        )
        tracker_total = None
        try:
            from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger

            ledger_rows = load_deposit_ledger(
                tracker_path if tracker_path and tracker_path.is_file() else None
            )
            deposits = pd.DataFrame(ledger_rows)
            if not deposits.empty:
                actual = actual_cash_buckets_from_deposits(deposits)
                tracker_total = float(deposits["amount"].sum())
                log.info(
                    "Actual cash from Transaction Tracker deposits: %d rows / $%.2f (%s)",
                    len(deposits),
                    tracker_total,
                    tracker_path.name if tracker_path else "postgres",
                )
            else:
                log.warning(
                    "Transaction Tracker empty — actual cash falls back to RevFlow eob_date"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Transaction Tracker load failed (%s) — actual cash falls back to RevFlow eob_date",
                exc,
            )

        deposit_events = build_deposit_events_from_actual(actual_ins["daily"], as_of=as_of)
        if not deposit_events:
            checks_path = recon_dir / "insurance_behavior" / "checks_timeline.csv"
            if checks_path.exists():
                try:
                    checks_df = pd.read_csv(checks_path)
                    for col in ("deposit_date", "eob_date"):
                        if col in checks_df.columns:
                            checks_df[col] = pd.to_datetime(checks_df[col], errors="coerce").dt.date
                    checks_df["paid_amount_sum"] = pd.to_numeric(
                        checks_df.get("paid_amount_sum"), errors="coerce"
                    ).fillna(0)
                    deposit_events = build_deposit_events_from_checks(checks_df, as_of=as_of)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Could not load checks_timeline for capacity: %s", exc)
    from cashflow_forecast.deposit_capacity import build_weekday_deposit_probs

    grain_wd_probs, global_wd_probs = build_weekday_deposit_probs(
        deposit_events, as_of=as_of
    )
    log.info(
        "Deposit capacity events: %d; spill grains with history: %d",
        len(deposit_events),
        len(grain_wd_probs),
    )

    # Outcomes (no risk mixed in)
    log.info("Classifying outcomes as_of=%s…", as_of)
    outcomes = classify_outcomes(
        lines,
        sla_lookup=lookup,
        fee_estimator=fees,
        rejections=rejections,
        denials=denials,
        as_of=as_of,
        deposit_schedule_lookup=deposit_schedules or None,
        weekday_probs_by_grain=grain_wd_probs,
        global_weekday_probs=global_wd_probs,
        eob_to_deposit_lookup=eob_to_deposit or None,
    )

    if not forward_summary.empty:
        forward_summary = attach_forward_expected_amounts(forward_summary, outcomes)

    # Risk flags (parallel)
    risk = build_risk_flags(
        outcomes,
        audit if audit is not None else pd.DataFrame(),
        fee_estimator=fees,
        as_of=as_of,
    )

    # Audit linker scoring
    matches = link_audit_to_waystar(
        audit if audit is not None else pd.DataFrame(),
        denials if denials is not None else pd.DataFrame(),
        rejections,
    )

    actual_fac = actual_cash_buckets_by_facility(payments, facility_lookup=outcomes)

    risk_keys: set[tuple[str, date]] = set()
    if risk is not None and not risk.empty and "webpt_patient_id" in risk.columns:
        dos_col = "date_of_service" if "date_of_service" in risk.columns else None
        for rrow in risk.itertuples(index=False):
            pid = str(getattr(rrow, "webpt_patient_id", "") or "")
            if not pid or not dos_col:
                continue
            dos_v = getattr(rrow, dos_col, None)
            if hasattr(dos_v, "date"):
                dos_v = dos_v.date() if not isinstance(dos_v, date) else dos_v
            if isinstance(dos_v, date):
                risk_keys.add((pid, dos_v))

    # Freeze scheduled land day before past-due packing moves forecast_date.
    outcomes["original_forecast_date"] = outcomes["forecast_date"]

    log.info("Packing past-due forecast_dates (FFD / Cap_eff)…")
    outcomes, reschedule_audit, slot_audit, capacity_df = pack_pastdue_ffd(
        outcomes,
        as_of=as_of,
        deposit_events=deposit_events,
        deposit_schedules=deposit_schedules or None,
        risk_patient_dos=risk_keys,
        actual_cash_daily=actual.get("daily"),
    )
    log.info("  rescheduled %d past-due rows", len(reschedule_audit))
    if slot_audit is not None and not slot_audit.empty:
        near = slot_audit.head(5)
        for row in near.itertuples(index=False):
            log.info(
                "  slot %s target=%.0f raw=%.0f cal=%.0f reserved=%.0f packed=%.0f final=%.0f",
                row.slot,
                row.weekday_target,
                row.raw_cap_sum,
                row.calibrated_cap_sum,
                row.reserved_future,
                row.packed_overdue,
                row.final_expected,
            )
    payment_models_df = payment_models_to_frame(pay_catalog)
    if emit_csv:
        write_payment_models(pay_catalog, output_dir / "payer_plan_payment_models.csv")

    projected = projected_cash_buckets(outcomes)
    projected_ins = projected_cash_buckets_by_insurance(outcomes)
    projected_fac = projected_cash_buckets_by_facility(outcomes)
    projected_cross = projected_cash_monthly_by_facility_insurance(outcomes)

    kpi = kpi_summary(
        outcomes,
        payments,
        risk,
        as_of=as_of,
        actual_cash_received=tracker_total,
    )

    # Walk-forward style land accuracy vs bank (Cash Expected Land = on+ov)
    focus_dates = ["2026-07-24", "2026-07-27"]
    land_acc = build_land_accuracy_frame(
        outcomes, actual["daily"], dates=focus_dates
    )
    # Also emit broader recent window for diagnostics
    land_acc_all = build_land_accuracy_frame(outcomes, actual["daily"])
    land_summary = summarize_land_accuracy(
        land_acc if not land_acc.empty else land_acc_all
    )
    log.info(
        "Land accuracy focus %s: MAPE=%s Bias=%s RMSE=%s Accuracy=%s (n=%s)",
        focus_dates,
        land_summary.get("mape"),
        land_summary.get("bias"),
        land_summary.get("rmse"),
        land_summary.get("accuracy"),
        land_summary.get("n_days"),
    )
    if not land_acc.empty:
        for row in land_acc.itertuples(index=False):
            log.info(
                "  land %s actual=%.0f pred=%.0f accuracy=%s",
                row.date,
                row.actual,
                row.pred,
                row.accuracy,
            )

    artifacts = {
        "payer_sla": sla,
        "payer_plan_payment_models": payment_models_df,
        "deposit_capacity": capacity_df,
        "slot_capacity_audit": slot_audit,
        "reschedule_audit": reschedule_audit,
        "land_accuracy": land_acc_all,
        "land_accuracy_focus": land_acc,
        "outcome_stages": outcomes,
        "risk_flags": risk,
        "audit_denial_matches": matches,
        "actual_cash_daily": actual["daily"],
        "actual_cash_weekly": actual["weekly"],
        "actual_cash_monthly": actual["monthly"],
        "projected_cash_daily": projected["daily"],
        "projected_cash_weekly": projected["weekly"],
        "projected_cash_monthly": projected["monthly"],
        "actual_cash_daily_by_insurance": actual_ins["daily"],
        "actual_cash_weekly_by_insurance": actual_ins["weekly"],
        "actual_cash_monthly_by_insurance": actual_ins["monthly"],
        "actual_cash_daily_by_facility": actual_fac["daily"],
        "actual_cash_weekly_by_facility": actual_fac["weekly"],
        "actual_cash_monthly_by_facility": actual_fac["monthly"],
        "projected_cash_daily_by_insurance": projected_ins["daily"],
        "projected_cash_weekly_by_insurance": projected_ins["weekly"],
        "projected_cash_monthly_by_insurance": projected_ins["monthly"],
        "projected_cash_daily_by_facility": projected_fac["daily"],
        "projected_cash_weekly_by_facility": projected_fac["weekly"],
        "projected_cash_monthly_by_facility": projected_fac["monthly"],
        "projected_cash_monthly_by_facility_insurance": projected_cross,
        # May–Aug filtered views
        "projected_cash_daily_may_aug": filter_period_to_window(projected["daily"]),
        "projected_cash_weekly_may_aug": filter_period_to_window(projected["weekly"]),
        "projected_cash_monthly_may_aug": filter_period_to_window(projected["monthly"]),
        "projected_cash_daily_by_insurance_may_aug": filter_period_to_window(projected_ins["daily"]),
        "projected_cash_weekly_by_insurance_may_aug": filter_period_to_window(
            projected_ins["weekly"]
        ),
        "projected_cash_monthly_by_insurance_may_aug": filter_period_to_window(
            projected_ins["monthly"]
        ),
        "projected_cash_daily_by_facility_may_aug": filter_period_to_window(projected_fac["daily"]),
        "projected_cash_weekly_by_facility_may_aug": filter_period_to_window(
            projected_fac["weekly"]
        ),
        "projected_cash_monthly_by_facility_may_aug": filter_period_to_window(
            projected_fac["monthly"]
        ),
        "projected_cash_monthly_by_facility_insurance_may_aug": filter_period_to_window(
            projected_cross
        ),
        "actual_cash_daily_may_aug": filter_period_to_window(actual["daily"]),
        "actual_cash_weekly_may_aug": filter_period_to_window(actual["weekly"]),
        "actual_cash_monthly_may_aug": filter_period_to_window(actual["monthly"]),
        "forward_visits_august": forward_summary,
        "overdue_by_insurance": overdue_by_insurance(outcomes),
        "denied_by_insurance": denied_by_insurance(outcomes),
        "risk_by_insurance": risk_by_insurance(risk),
        "outcome_stage_counts": outcome_stage_counts(outcomes),
        "kpi_summary": kpi,
    }
    if from_db:
        from cashflow_forecast.db_source import write_forecast_run

        feature_tables = {
            "payer_sla": sla,
            "risk_flags": risk if risk is not None else pd.DataFrame(),
            "actual_cash_daily": actual["daily"],
            "projected_cash_daily": projected["daily"],
            "deposit_capacity": capacity_df if capacity_df is not None else pd.DataFrame(),
            "payment_models": payment_models_df,
            "kpi_summary": pd.DataFrame([kpi]) if isinstance(kpi, dict) else pd.DataFrame(),
        }
        run_id = write_forecast_run(
            algorithm_version="forecast-build",
            as_of_date=as_of,
            outcome_df=outcomes,
            feature_tables=feature_tables,
            rules_version="business_rules",
            params={"as_of": as_of.isoformat()},
        )
        log.info("Wrote forecast_run %s to DB", run_id)
    if emit_csv:
        export_all(output_dir, artifacts)
        log.info("Wrote diagnostic CSV pack -> %s", output_dir)

    log.info("KPI summary: %s", kpi)
    print(f"\nWrote forecast outputs -> {'DB' if from_db else output_dir}")
    print(f"  Actual cash:    ${kpi['actual_cash_received']:,.2f}")
    print(f"  Projected:      ${kpi['projected_cash_in']:,.2f}")
    print(f"  Jan–Aug proj:   ${kpi.get('projected_cash_may_aug', 0):,.2f}")
    print(f"  On track:       ${kpi['on_track_amount']:,.2f} ({kpi['on_track_count']} lines)")
    print(f"  Overdue:        ${kpi['overdue_amount']:,.2f} ({kpi['overdue_count']} lines)")
    print(f"  Denied+Reject:  ${kpi['denied_amount']:,.2f} ({kpi['denied_count']} lines)")
    print(f"  Risk exposure:  ${kpi['risk_exposure_amount']:,.2f} ({kpi['risk_visit_count']} visits)")
    if land_summary.get("n_days"):
        print(
            f"  Land accuracy:  MAPE={land_summary['mape']:.1%}  "
            f"Bias=${land_summary['bias']:,.0f}  "
            f"RMSE=${land_summary['rmse']:,.0f}  "
            f"Acc={land_summary['accuracy']:.1%}  "
            f"(n={land_summary['n_days']} focus days)"
        )
    if not forward_summary.empty:
        print(
            f"  Aug forward:    {int(forward_summary['projected_visit_count'].sum())} visits / "
            f"${float(forward_summary.get('expected_amount', pd.Series(dtype=float)).sum()):,.2f}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cashflow_forecast", description="Cash flow forecasting pilot")
    sub = p.add_subparsers(dest="command", required=True)

    sla = sub.add_parser("sla", help="Build payer SLA table from reconciliation")
    sla.add_argument("--from-db", action="store_true", help="Read spine from cashflow_db")
    sla.add_argument("--emit-csv", action="store_true", help="Also write payer_sla.csv")
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
        "--from-db",
        action="store_true",
        help="Read all inputs from cashflow_db repository (product path)",
    )
    build.add_argument(
        "--emit-csv",
        action="store_true",
        help="Also write diagnostic CSV pack (default on for legacy file mode)",
    )
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
    build.add_argument(
        "--transaction-tracker",
        default=None,
        help="Transaction Tracker xlsx (bank deposits → Actual remits)",
    )
    build.set_defaults(func=cmd_build)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""CLI: python -m cashflow_forecast sla|build."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from cashflow_forecast.aggregations import (
    denied_by_insurance,
    outcome_stage_counts,
    overdue_by_insurance,
    risk_by_insurance,
    risk_totals_by_insurance,
)
from cashflow_forecast.audit_linker import link_audit_to_waystar
from cashflow_forecast.config import REPO_ROOT, default_as_of
from cashflow_forecast.deposit_capacity import (
    ORG_CAPACITY_PACKING,
    build_deposit_events_from_actual,
    build_deposit_events_from_checks,
    pack_pastdue_ffd,
    weekday_deposit_targets,
)
from cashflow_forecast.export import export_all
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.forecast_engine import (
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
    fill_schedule_anchors,
    load_cash_velocity_lookup,
    load_deposit_schedule_lookup,
    load_eob_to_deposit_lookup,
    merge_velocity_into_lookup,
    pit_deposit_schedules,
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
from cashflow_forecast.sheet_visit_overrides import (
    apply_sheet_visit_overrides,
    load_sheet_visit_overrides,
    sheet_override_counts,
    sheet_overrides_enabled,
    sheet_paid_visit_keys,
)
from cashflow_forecast.backtest_eval import (
    eval_dates,
    evaluate_backtest,
    load_holdout_csv,
    rescore_backtest_pack,
    rescore_variant_tree,
    write_backtest_outputs,
)
from cashflow_forecast.pack_experiment import (
    ISOLATED_SPECS,
    ORG_CAPACITY_SPEC,
    TUE_PIERCE_VARIANT,
    VARIANT_SPECS,
    layer2b_gates,
    run_pack_variants,
)
from cashflow_forecast.batch_cadence import batch_plan_by_grain, tuesday_pierce_plan
from cashflow_forecast.inflight_ledger import (
    cap_scheduled_by_slot,
    lag_lookup_from_leadtime_rows,
    overlay_scheduled,
    scheduled_amounts_by_slot,
    scheduled_from_eobs,
    scheduled_from_mail,
)
from cashflow_forecast.forecast_components import (
    assert_exclusive,
    exclude_stream_ar,
    offset_known_from_ar,
)
from cashflow_forecast.recurring_cash import (
    nynm_stream_frame,
    offset_stream_by_known,
    paper_check_frame,
    patient_weekday_frame,
    tag_residual,
)
from cashflow_forecast.accuracy_ops import TARGET_ACCURACY, daterange, write_weekly_report
from cashflow_forecast.day_ahead_coverage import day_ahead_coverage
from cashflow_forecast.week_envelope import apply_live_envelope_tilt
from cashflow_forecast.land_accuracy import (
    actual_by_day,
    build_land_accuracy_frame,
    summarize_error_metrics,
)
from cashflow_forecast.pit import (
    build_leakage_manifest,
    filter_frame_on_or_before,
    reopen_lines_after_as_of,
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
from cashflow_forecast.payer_plan import (
    PACKING_GRAIN_ORG,
    PACKING_GRAIN_PLAN,
    build_eligible_orgs,
    fidelis_is_eligible,
    fill_ins_from_eligibility,
    outcome_ins_pairs,
)
from cashflow_forecast.payer_sla import build_payer_sla, sla_lookup, write_payer_sla
from cashflow_forecast.risk_flags import build_risk_flags
from cashflow_forecast.spine_coverage import (
    normalize_recon_identity,
    recon_spine_conservation,
)

log = logging.getLogger("cashflow_forecast")


def _resolve_path(path: str | Path) -> Path:
    """Resolve relative paths against repo root (works from any cwd)."""
    p = Path(path)
    if p.is_absolute():
        return p
    return REPO_ROOT / p


def _cli_packing_grain(args: argparse.Namespace) -> str:
    raw = str(getattr(args, "packing_grain", PACKING_GRAIN_PLAN) or PACKING_GRAIN_PLAN)
    mode = raw.strip().lower()
    env = os.environ.get("FORECAST_PACKING_GRAIN", "").strip().lower()
    # Env is the nightly gate. CLI --packing-grain org still wins when set.
    if mode == PACKING_GRAIN_PLAN and env in (PACKING_GRAIN_PLAN, PACKING_GRAIN_ORG):
        mode = env
    if mode not in (PACKING_GRAIN_PLAN, PACKING_GRAIN_ORG):
        raise ValueError(f"invalid --packing-grain {raw!r} (want plan or org)")
    return mode


def _pierce_batch_guard(
    *,
    pierce_batch: bool,
    from_db: bool,
    backtest: bool,
    output_dir: Path,
) -> str | None:
    """Live one-shot only. None = allowed."""
    if not pierce_batch:
        return None
    if not from_db:
        return "--pierce-batch requires --from-db"
    if backtest:
        return "--pierce-batch is live-only; do not combine with --backtest"
    if output_dir.name.startswith("backtest_") and "pack_variants" not in output_dir.parts:
        return f"--pierce-batch must not overwrite {output_dir}"
    return None


def _tracker_payor_names() -> list[str]:
    from cashflow_forecast import db_source as dbs

    df = dbs.load_tracker_payer_daily_df()
    if df.empty or "payor" not in df.columns:
        return []
    names: list[str] = []
    for raw in df["payor"].dropna().unique():
        name = str(raw).strip()
        if name and name.lower() not in ("__unmatched__", "nan"):
            names.append(name)
    return names


def _nynm_tracker_occupancy(as_of: date) -> list:
    """Tracker Tuesday occupancy for NYNM — last in → next out."""
    try:
        from cashflow_forecast import db_source as dbs
        from cashflow_forecast.batch_cadence import nynm_events_from_payor_daily

        df = dbs.load_tracker_payer_daily_df()
        events = nynm_events_from_payor_daily(df, as_of)
        log.info("NYNM tracker occupancy events=%d as_of=%s", len(events), as_of)
        return events
    except Exception as exc:  # noqa: BLE001
        log.warning("NYNM tracker occupancy skipped: %s", exc)
        return []


def _enrich_deposit_events_with_sheet(
    events: list,
    as_of: date,
) -> list:
    """Cadence/FFD only: sheet lumps + separate MERCH/BOA streams. Scoring actuals unchanged."""
    try:
        from cashflow_forecast.db_source import enrich_deposit_events_with_sheet

        events = enrich_deposit_events_with_sheet(events, as_of=as_of)
    except Exception as exc:  # noqa: BLE001
        log.warning("Sheet lump attribution skipped: %s", exc)
    try:
        from cashflow_forecast.db_source import enrich_deposit_events_with_nonins

        events = enrich_deposit_events_with_nonins(events, as_of=as_of)
    except Exception as exc:  # noqa: BLE001
        log.warning("Nonins stream events skipped: %s", exc)
    return events


def _preflight_eligible_orgs(outcomes: pd.DataFrame) -> set[str]:
    pairs = outcome_ins_pairs(outcomes)
    eob = [ins or rev for ins, rev in pairs]
    eob.extend(rev for ins, rev in pairs if rev)
    tracker = _tracker_payor_names()
    eligible = build_eligible_orgs(
        tracker_payors=tracker,
        outcome_ins=pairs,
        eob_payors=eob,
    )
    log.info("Layer 2B eligible_orgs (%d): %s", len(eligible), sorted(eligible))
    print("Layer 2B eligible_orgs:", ", ".join(sorted(eligible)) or "(none)")
    if not fidelis_is_eligible(eligible):
        raise RuntimeError(
            "Layer 2B preflight: Fidelis not in eligible_orgs "
            f"{sorted(eligible)} — stop"
        )
    return eligible


def _parse_as_of(text: str | None) -> date:
    if not text:
        return default_as_of()
    return datetime.strptime(text, "%Y-%m-%d").date()


def _first_of_month_minus(d: date, months: int) -> date:
    """First calendar day of (d's month minus ``months``)."""
    months = max(int(months), 0)
    year, month = d.year, d.month - months
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _month_end(d: date) -> date:
    """Last calendar day of ``d``'s month."""
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def _forward_window(as_of: date) -> tuple[date, date]:
    """Forward-volume projection window: strictly after as_of, through month end."""
    start = as_of + timedelta(days=1)
    return start, _month_end(start)


def _old_ar_window(as_of: date) -> tuple[date, date]:
    """Jan 1 of as_of year through the day before the CPT-mix window."""
    mix_start = _first_of_month_minus(as_of, 2)
    return date(as_of.year, 1, 1), mix_start - timedelta(days=1)


def _display_window(as_of: date, forward_end: date | None = None) -> tuple[date, date]:
    """Jan 1 of as_of year through the month that contains the forward window."""
    end = _month_end(forward_end if forward_end is not None else as_of)
    return date(as_of.year, 1, 1), end


class _PhaseTimer:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.last = self.t0
        self.phases: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        dt = now - self.last
        self.phases[name] = round(dt, 2)
        log.info("phase %s %.1fs (elapsed %.1fs)", name, dt, now - self.t0)
        self.last = now


def _ensure_recon_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Align optional columns so concat with recon lines works."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if "source" not in out.columns:
        out["source"] = "reconciliation"
    if "units" not in out.columns:
        out["units"] = 1.0
    for col in ("ins_name", "insurance_revflow"):
        if col not in out.columns:
            continue
        s = out[col].fillna("").astype(str).str.strip()
        out[col] = s.replace({"nan": "", "None": "", "<NA>": "", "<NaN>": ""})
    return out


def _exclude_recon_covered_ar(
    ar: pd.DataFrame,
    recon_lines: pd.DataFrame,
    visit_aggs: pd.DataFrame,
    *,
    drop_paid_visits: bool = True,
    sheet_paid_visits: set[tuple[str, date]] | None = None,
) -> pd.DataFrame:
    """Drop clinical-AR rows already represented in reconciliation output.

    The reconciliation run emits one row per WebPT service line in its window
    (matched or pending), so re-adding the same line as synthetic pending AR
    double-counts it. Lines of visits reconciliation already marked paid are
    dropped as well.
    """
    if ar is None or ar.empty:
        return ar

    out = ar.copy()
    out["date_of_service"] = pd.to_datetime(out["date_of_service"], errors="coerce").dt.date
    n0 = len(out)

    def _norm(series: pd.Series) -> pd.Series:
        return series.astype(str).str.strip()

    line_cols = {"webpt_patient_id", "date_of_service", "cpt_code"}
    if recon_lines is not None and not recon_lines.empty and line_cols <= set(recon_lines.columns):
        rl = recon_lines
        rl_dos = pd.to_datetime(rl["date_of_service"], errors="coerce").dt.date
        covered = set(zip(_norm(rl["webpt_patient_id"]), rl_dos, _norm(rl["cpt_code"])))
        keys = list(zip(_norm(out["webpt_patient_id"]), out["date_of_service"], _norm(out["cpt_code"])))
        out = out[[k not in covered for k in keys]]

    agg_cols = {"webpt_patient_id", "date_of_service", "visit_status"}
    if (
        drop_paid_visits
        and not out.empty
        and visit_aggs is not None
        and not visit_aggs.empty
        and agg_cols <= set(visit_aggs.columns)
    ):
        closed = visit_aggs[visit_aggs["visit_status"].astype(str) == "paid"]
        if not closed.empty:
            closed_dos = pd.to_datetime(closed["date_of_service"], errors="coerce").dt.date
            closed_keys = set(zip(_norm(closed["webpt_patient_id"]), closed_dos))
            vkeys = list(zip(_norm(out["webpt_patient_id"]), out["date_of_service"]))
            out = out[[k not in closed_keys for k in vkeys]]

    if sheet_paid_visits and not out.empty and {"webpt_patient_id", "date_of_service"} <= set(out.columns):
        vkeys = list(zip(_norm(out["webpt_patient_id"]), out["date_of_service"]))
        out = out[[k not in sheet_paid_visits for k in vkeys]].reset_index(drop=True)

    if len(out) != n0:
        log.info("Clinical AR lines: %d -> %d after reconciliation dedupe", n0, len(out))
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


def _holiday_shifts_from_schedules(schedules: dict | None) -> dict[str, int]:
    if not schedules:
        return {}
    return {
        str(key).strip().lower(): int(sch.holiday_shift)
        for key, sch in schedules.items()
        if sch is not None
    }


def _persist_cash_matches(matches: list, events: list) -> None:
    """Store the auditable match. A missing table must not stop the forecast."""
    if not matches:
        return
    payers = {ev.event_id: (ev.payer, ev.ref) for ev in events}
    try:
        from cashflow_db.repository import connection

        with connection() as conn:
            for match in matches:
                payer, ref = payers.get(match.event_id, (None, None))
                conn.execute(
                    """
                    INSERT INTO billing.cash_event_match (
                        event_id, kind, payer, ref, amount, tier, reason,
                        source_date, expected_bank_date, actual_bank_date,
                        observed_at, bank_row_ids, component
                    ) VALUES (
                        %s, 'eob', %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (event_id) DO UPDATE SET
                        tier = EXCLUDED.tier,
                        reason = EXCLUDED.reason,
                        payer = EXCLUDED.payer,
                        ref = EXCLUDED.ref,
                        amount = EXCLUDED.amount,
                        source_date = EXCLUDED.source_date,
                        expected_bank_date = EXCLUDED.expected_bank_date,
                        actual_bank_date = EXCLUDED.actual_bank_date,
                        observed_at = EXCLUDED.observed_at,
                        bank_row_ids = EXCLUDED.bank_row_ids,
                        matched_at = now()
                    WHERE billing.cash_event_match.tier <> 'MATCHED_MANUAL'
                    """,
                    (
                        match.event_id,
                        payer,
                        ref,
                        match.amount,
                        match.tier,
                        match.reason,
                        match.source_date,
                        match.expected_bank_date,
                        match.actual_bank_date,
                        match.observed_at,
                        list(match.bank_row_ids),
                        match.component,
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("cash_event_match persist skipped: %s", exc)


def _eobs_still_inflight(
    eob_rows: list,
    bank_rows: list,
    *,
    as_of: date,
    lag_lookup: dict,
    persist: bool = True,
) -> list:
    """Keep NOT_YET_DUE and AMBIGUOUS only. Definitive matches leave the forecast."""
    from cashflow_forecast.cash_event_match import (
        BankTxn,
        CashEvent,
        inflight_events,
        match_cash_events,
    )
    from cashflow_forecast.payer_plan import resolve_payer_plan

    if not eob_rows or not bank_rows:
        return eob_rows
    events = []
    index_of: dict[str, int] = {}
    for i, row in enumerate(eob_rows):
        origin = row.get("check_date") or row.get("eob_date")
        if origin is None:
            continue
        if not isinstance(origin, date):
            continue
        payor = str(row.get("payor_raw") or "")
        key = resolve_payer_plan(payor)
        lag = lag_lookup.get(
            key.plan_key,
            lag_lookup.get(key.org_key, lag_lookup.get(payor.strip().lower(), 2)),
        )
        amount = float(row.get("paid_amount_sum") or 0)
        event_id = f"{row.get('check_eft_num') or ''}|{origin.isoformat()}|{round(amount, 2)}"
        events.append(
            CashEvent(
                event_id=event_id,
                payer=payor,
                ref=str(row.get("check_eft_num") or ""),
                amount=amount,
                source_date=origin,
                lead_days=int(lag or 2),
                observed_at=as_of,
            )
        )
        index_of[event_id] = i
    banks = []
    for row in bank_rows:
        txn = row.get("txn_date")
        if not isinstance(txn, date):
            continue
        banks.append(
            BankTxn(
                row_id=str(row.get("row_id") or ""),
                txn_date=txn,
                amount=float(row.get("amount") or 0),
                refs=tuple(
                    str(row.get(k) or "")
                    for k in ("eft_1", "eft_2", "check_reference")
                ),
                description=str(row.get("description") or ""),
                observed_at=row.get("observed_at") if isinstance(row.get("observed_at"), date) else None,
            )
        )
    matched = match_cash_events(events, banks, as_of=as_of)
    if persist:
        _persist_cash_matches(matched, events)
    keep = {m.event_id for m in inflight_events(matched, as_of=as_of)}
    return [eob_rows[index_of[event_id]] for event_id in sorted(keep) if event_id in index_of]


def _stream_by_slot(stream: pd.DataFrame) -> dict[date, float]:
    out: dict[date, float] = {}
    if stream is None or stream.empty:
        return out
    for row in stream.itertuples(index=False):
        day = getattr(row, "forecast_date", None)
        if hasattr(day, "date") and not isinstance(day, date):
            day = day.date()
        amt = float(getattr(row, "expected_amount", 0) or 0)
        if isinstance(day, date) and amt > 0:
            out[day] = out.get(day, 0.0) + amt
    return out


def _append_recurring_components(
    outcomes: pd.DataFrame,
    *,
    as_of: date,
    deposit_events: list,
    known: pd.DataFrame,
    from_db: bool,
    stream: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """NYNM stream, patient weekday stream, and future paper checks. One component each."""
    if stream is None:
        stream = offset_stream_by_known(nynm_stream_frame(deposit_events or [], as_of), known)
    frames = [outcomes]
    if stream is not None and not stream.empty:
        frames.append(stream)
    if from_db:
        try:
            from cashflow_db.repository import connection, payments as pay_repo

            with connection() as conn:
                sheet = pay_repo.list_checks_deposits_sheet(conn)
                history = pay_repo.patient_cash_daily(conn, as_of=as_of)
            paper = paper_check_frame(sheet, as_of)
            patient = patient_weekday_frame(history, as_of)
            if paper is not None and not paper.empty:
                frames.append(paper)
            if patient is not None and not patient.empty:
                frames.append(patient)
        except Exception as exc:  # noqa: BLE001
            log.warning("recurring cash streams skipped: %s", exc)
    return pd.concat(frames, ignore_index=True)


def _load_inflight_scheduled(
    as_of: date,
    *,
    eob_to_deposit: dict | None = None,
    pit: bool = False,
    holiday_shifts: dict[str, int] | None = None,
) -> pd.DataFrame:
    """EOBs + mail checks, measured lead times, no cap (caller caps per day_total_mult)."""
    from cashflow_db.repository import connection, payments as pay_repo

    with connection() as conn:
        eob_rows = pay_repo.get_unallocated_eob_checks(conn, as_of=as_of, pit=pit)
        mail_rows = pay_repo.get_mail_checks_in_hand(conn, as_of=as_of)
        lead_rows = pay_repo.get_eob_deposit_leadtime(conn, before=as_of if pit else None)
        try:
            bank_rows = pay_repo.list_tracker_for_match(conn, as_of=as_of)
        except Exception:  # noqa: BLE001
            bank_rows = []
    lags = {**(eob_to_deposit or {}), **lag_lookup_from_leadtime_rows(lead_rows)}
    eob_rows = _eobs_still_inflight(
        eob_rows, bank_rows, as_of=as_of, lag_lookup=lags, persist=not pit
    )
    return pd.concat(
        [
            scheduled_from_eobs(
                eob_rows, as_of=as_of, lag_lookup=lags, holiday_shifts=holiday_shifts
            ),
            scheduled_from_mail(mail_rows, as_of=as_of, holiday_shifts=holiday_shifts),
        ],
        ignore_index=True,
    )


def cmd_build(args: argparse.Namespace) -> int:
    from_db = getattr(args, "from_db", False)
    backtest = bool(getattr(args, "backtest", False))
    if backtest and not from_db:
        log.error("--backtest requires --from-db")
        return 2
    emit_csv = getattr(args, "emit_csv", False) or not from_db or backtest
    data_dir = _resolve_path(args.data_dir)
    as_of = _parse_as_of(args.as_of)
    fwd_start, fwd_end = _forward_window(as_of)
    ar_from, ar_to = _old_ar_window(as_of)
    disp_start, disp_end = _display_window(as_of, fwd_end)
    live_default = str(REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026/forecast")
    if backtest and str(getattr(args, "output_dir", live_default)) == live_default:
        output_dir = Path("/data/exports") / f"backtest_{as_of.isoformat()}"
    else:
        output_dir = _resolve_path(args.output_dir)
    try:
        packing_mode = _cli_packing_grain(args)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    if packing_mode == PACKING_GRAIN_ORG:
        if not from_db:
            log.error("--packing-grain org requires --from-db (tracker payors)")
            return 2
        if output_dir.name.startswith("backtest_") and "pack_variants" not in output_dir.parts:
            log.error(
                "Layer 2B must not overwrite %s. Re-pack with: "
                "python -m cashflow_forecast pack-variants --dir %s --from-db --packing-grain org",
                output_dir,
                output_dir,
            )
            return 2
    pierce_batch = bool(getattr(args, "pierce_batch", False))
    pierce_err = _pierce_batch_guard(
        pierce_batch=pierce_batch,
        from_db=from_db,
        backtest=backtest,
        output_dir=output_dir,
    )
    if pierce_err:
        log.error("%s", pierce_err)
        return 2
    horizon_days = int(getattr(args, "horizon_days", 14) or 14)
    holdout_path = getattr(args, "holdout_csv", None)
    deposits = pd.DataFrame()
    eval_actual: dict[str, pd.DataFrame] | None = None
    timer = _PhaseTimer()
    leakage_bits: dict[str, object] = {
        "recon_reopened": 0,
        "sf_n": 0,
        "sf_disabled": False,
        "ib_source": "payor_behavior_summary (latest snapshot)",
        "poc_caveat": "PoC snapshot is not versioned — current warehouse rows used",
        "recon_run_id": "",
        "spine_conservation": {},
        "sheet_n": 0,
        "sheet_paid": 0,
        "sheet_denied": 0,
        "sheet_disabled": False,
    }
    nynm_occ: list = []

    recon_dir = data_dir / "reconciliation"
    audit_dir = data_dir / "audit"
    extracted_dir = data_dir / "extracted"

    if from_db:
        from cashflow_forecast import db_source as dbs

        log.info("Loading reconciliation lines from DB…")
        recon_run_id = None
        try:
            from cashflow_db.repository import connection as db_conn
            from cashflow_db.repository import reconciliation as recon_repo

            with db_conn() as conn:
                recon_run_id = recon_repo.latest_reconciliation_run_id(conn)
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not stamp reconciliation_run_id: %s", exc)
        leakage_bits["recon_run_id"] = recon_run_id or ""
        recon_lines = _ensure_recon_columns(dbs.load_reconciliation_lines_df(as_of=as_of))
        if recon_lines.empty:
            log.error("No reconciliation_line rows — run: python -m cashflow_reconcile --from-db")
            return 1
        recon_lines, reopened = reopen_lines_after_as_of(recon_lines, as_of)
        leakage_bits["recon_reopened"] = reopened
        if reopened:
            log.info("  reopened %d lines with eob_date > %s", reopened, as_of)
        log.info("  %d recon lines", len(recon_lines))
        timer.mark("recon_lines")
        # Sheet paid/denied. Waystar paid lines stay as they are. Snowflake
        # copies already sit on the sheet, so the DB path does not apply a
        # second Snowflake override (that path could flip a Waystar paid visit).
        leakage_bits["sf_n"] = 0
        leakage_bits["sf_disabled"] = True
        sheet_overrides: dict = {}
        if not sheet_overrides_enabled():
            leakage_bits["sheet_disabled"] = True
            log.info(
                "Eligibility sheet overrides disabled (CASHFLOW_FORECAST_DISABLE_SHEET_OVERRIDES)"
            )
        else:
            sheet_overrides = load_sheet_visit_overrides(as_of=as_of, backtest=backtest)
            sheet_n, sheet_paid, sheet_denied = sheet_override_counts(sheet_overrides)
            leakage_bits["sheet_n"] = sheet_n
            leakage_bits["sheet_paid"] = sheet_paid
            leakage_bits["sheet_denied"] = sheet_denied
            if sheet_overrides:
                recon_lines = apply_sheet_visit_overrides(recon_lines, sheet_overrides)
                recon_lines = _ensure_recon_columns(recon_lines)
            else:
                log.info("No eligibility sheet paid/denied overrides")
        visits_df = dbs.load_reconciliation_visits_df()
        if not visits_df.empty and "visit_paid_total" in visits_df.columns:
            visits_df["visit_paid_total"] = pd.to_numeric(
                visits_df["visit_paid_total"], errors="coerce"
            ).fillna(0)
        if ar_to >= ar_from:
            may_lines = dbs.load_clinical_ar_lines_df(
                service_from=ar_from,
                service_to=ar_to,
            )
        else:
            may_lines = pd.DataFrame()
        log.info("Old AR window %s..%s rows=%d", ar_from, ar_to, len(may_lines))
        may_lines = _exclude_recon_covered_ar(
            may_lines,
            recon_lines,
            visits_df,
            drop_paid_visits=not backtest,
            sheet_paid_visits=sheet_paid_visit_keys(sheet_overrides),
        )
        may_lines = _ensure_recon_columns(may_lines) if not may_lines.empty else may_lines
        timer.mark("may_ar")
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
        if backtest:
            leakage_bits["ib_source"] = (
                "skipped latest snapshot (leakage); SLA from as_of-gated recon lines only"
            )
            velocity = {}
            deposit_schedules = {}
            eob_to_deposit = {}
            log.info("Backtest: skipped latest insurance_behavior snapshot")
        else:
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
            log.info("Building forward volume from PoC (DB)…")
            # CPT-mix + facility/insurance fallback: last 3 calendar months only
            # (unbounded scan exploded after pt_city charges ~1.1M rows).
            sl_from = _first_of_month_minus(as_of, 2)
            sl = dbs.load_clinical_ar_lines_df(service_from=sl_from, service_to=as_of)
            log.info("  clinical AR mix window %s..%s rows=%d", sl_from, as_of, len(sl))
            if not sl.empty:
                sl = sl.copy()
                sl["patient_id"] = sl["webpt_patient_id"].astype(str)
                sl["date_of_daily_note"] = pd.to_datetime(
                    sl["date_of_service"], errors="coerce"
                ).dt.date
                sl["units"] = pd.to_numeric(sl.get("units"), errors="coerce").fillna(1.0)
                notes_df = sl[["patient_id", "date_of_daily_note", "patient_name"]].copy()
                notes_df["facility_name"] = sl.get("facility_name", "")
                notes_df["insurance_name"] = sl.get("ins_name", "")
                cpt_df = sl[
                    ["patient_id", "cpt_code", "modifier", "units", "date_of_daily_note"]
                ].copy()
            else:
                notes_df = pd.DataFrame()
                cpt_df = pd.DataFrame()
            # auth_remaining = visits_authorized − visits already used (per patient)
            if not patients.empty and "auth_ins_visits" in patients.columns:
                patients = patients.copy()
                auth = pd.to_numeric(patients["auth_ins_visits"], errors="coerce")
                if not sl.empty:
                    used = sl.groupby("patient_id")["date_of_daily_note"].nunique()
                    used_by_pid = (
                        patients["patient_id"].astype(str).map(used).fillna(0)
                        if "patient_id" in patients.columns
                        else patients["webpt_patient_id"].astype(str).map(used).fillna(0)
                    )
                else:
                    used_by_pid = 0
                patients["auth_remaining"] = (auth - used_by_pid).clip(lower=0)
            # Only project visits after as_of — earlier days are already real
            # visits in the warehouse and would double-count.
            forward_lines, forward_summary = build_august_forward_lines(
                plans,
                patients,
                notes_df,
                cpt_df,
                fee_estimator=fees,
                window_start=fwd_start,
                window_end=fwd_end,
            )
            log.info(
                "  %d forward lines (%d patients, window %s..%s)",
                len(forward_lines),
                forward_summary["webpt_patient_id"].nunique() if not forward_summary.empty else 0,
                fwd_start,
                fwd_end,
            )
        frames = [recon_lines]
        if not may_lines.empty:
            frames.append(may_lines)
        if not forward_lines.empty:
            frames.append(forward_lines)
        lines = _ensure_recon_columns(pd.concat(frames, ignore_index=True, sort=False))
        timer.mark("spine_concat")
        payments = dbs.load_payments_unified_df(as_of=as_of)
        denials_all = dbs.load_denials_df()
        if backtest and not denials_all.empty:
            denials_all = filter_frame_on_or_before(
                denials_all, as_of, "denial_date", drop_undated=False
            )
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
        train_pay = recon_lines
        if "eob_date" in recon_lines.columns:
            eob = pd.to_datetime(recon_lines["eob_date"], errors="coerce").dt.date
            train_pay = recon_lines[eob.isna() | (eob <= as_of)]
        pay_catalog = learn_payment_models(
            train_pay,
            payments_unified=payments if not payments.empty else None,
            visits=visits_df if not visits_df.empty else None,
            fee_estimator=fees,
        )
        lines = apply_visit_expected_amounts(lines, pay_catalog)
        actual_ins = (
            actual_cash_buckets_by_insurance(payments)
            if not payments.empty
            else {"daily": pd.DataFrame(), "weekly": pd.DataFrame(), "monthly": pd.DataFrame()}
        )
        tracker_df = dbs.load_tracker_actuals_df(date_to=as_of if backtest else None)
        if not tracker_df.empty:
            actual = actual_cash_buckets_from_deposits(tracker_df)
            tracker_total = float(tracker_df["amount"].sum())
            log.info(
                "Actual cash from transaction tracker: %d days / $%.2f",
                len(tracker_df),
                tracker_total,
            )
        else:
            actual = {
                "daily": pd.DataFrame(),
                "weekly": pd.DataFrame(),
                "monthly": pd.DataFrame(),
            }
            tracker_total = 0.0
            log.warning(
                "Transaction Tracker empty — actual cash is $0 (no RevFlow fallback)"
            )
        deposits_eval = dbs.load_scoring_actuals_df() if backtest else tracker_df
        if backtest and deposits_eval is not None and not deposits_eval.empty:
            eval_actual = actual_cash_buckets_from_deposits(deposits_eval)
            log.info(
                "Eval actuals (tracker-preferred): %d days / $%.2f",
                len(deposits_eval),
                float(deposits_eval["amount"].sum()),
            )
        else:
            eval_actual = actual
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
        deposit_events = _enrich_deposit_events_with_sheet(deposit_events, as_of)
        nynm_occ = _nynm_tracker_occupancy(as_of)
        timer.mark("payments_models")
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
                log.info("Building forward volume from Plans of Care…")
                plans = load_plans_of_care(poc_path)
                notes = load_daily_notes(notes_path) if notes_path.exists() else pd.DataFrame()
                cpt = load_cpt_codes(cpt_path) if cpt_path.exists() else pd.DataFrame()
                forward_lines, forward_summary = build_august_forward_lines(
                    plans,
                    patients,
                    notes,
                    cpt,
                    fee_estimator=fees,
                    window_start=fwd_start,
                    window_end=fwd_end,
                )
                log.info(
                    "  %d forward lines (%d patients, window %s..%s)",
                    len(forward_lines),
                    forward_summary["webpt_patient_id"].nunique() if not forward_summary.empty else 0,
                    fwd_start,
                    fwd_end,
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

        # Actual cash from Transaction Tracker only (never RevFlow remits).
        actual = {
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
        tracker_total = 0.0
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
                    "Transaction Tracker empty — actual cash is $0 (no RevFlow fallback)"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "Transaction Tracker load failed (%s) — actual cash is $0 (no RevFlow fallback)",
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
    pit = pit_deposit_schedules(deposit_events, as_of)
    if backtest:
        deposit_schedules = pit
        n_phase = sum(
            1
            for s in deposit_schedules.values()
            if s is not None and s.period_days and s.anchor_date is not None
        )
        log.info(
            "Backtest PIT deposit schedules: %d keys (%d phase-confident)",
            len(deposit_schedules),
            n_phase,
        )
    else:
        merged = dict(pit)
        merged.update(deposit_schedules or {})
        deposit_schedules = merged
    if deposit_schedules:
        deposit_schedules = fill_schedule_anchors(
            deposit_schedules, deposit_events, as_of
        )
    log.info(
        "Deposit capacity events: %d; spill grains with history: %d",
        len(deposit_events),
        len(grain_wd_probs),
    )

    # Outcomes (no risk mixed in)
    log.info("Classifying outcomes as_of=%s…", as_of)
    lines = normalize_recon_identity(lines)
    recon_lines = normalize_recon_identity(recon_lines)
    if from_db:
        try:
            from cashflow_forecast import db_source as dbs

            elig_lookup = dbs.load_eligibility_ins_lookup()
            n_blank = 0
            if not lines.empty and "ins_name" in lines.columns:
                n_blank = int(
                    lines["ins_name"].fillna("").astype(str).str.strip().eq("").sum()
                )
            lines = fill_ins_from_eligibility(lines, elig_lookup)
            n_blank_after = 0
            if not lines.empty and "ins_name" in lines.columns:
                n_blank_after = int(
                    lines["ins_name"].fillna("").astype(str).str.strip().eq("").sum()
                )
            log.info(
                "Eligibility ins fill: lookup=%d blank_before=%d blank_after=%d",
                len(elig_lookup),
                n_blank,
                n_blank_after,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Eligibility ins fill skipped: %s", exc)
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
    spine_report = recon_spine_conservation(recon_lines, outcomes, as_of=as_of)
    leakage_bits["spine_conservation"] = spine_report
    log.info(
        "Spine conservation eligible=%s classified=%s excluded=%s unmatched=%s closed=%s",
        spine_report.get("eligible"),
        spine_report.get("classified"),
        spine_report.get("excluded"),
        spine_report.get("unmatched"),
        spine_report.get("closed"),
    )
    timer.mark("classify")

    if not forward_summary.empty:
        forward_summary = attach_forward_expected_amounts(
            forward_summary,
            outcomes,
            window_start=fwd_start,
            window_end=fwd_end,
        )

    # Risk flags (parallel)
    risk = build_risk_flags(
        outcomes,
        audit if audit is not None else pd.DataFrame(),
        fee_estimator=fees,
        as_of=as_of,
    )
    timer.mark("risk_flags")

    # Audit linker scoring (DB denials may lack name_key — linker fills or no-ops)
    try:
        matches = link_audit_to_waystar(
            audit if audit is not None else pd.DataFrame(),
            denials if denials is not None else pd.DataFrame(),
            rejections,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("audit linker skipped: %s", exc)
        matches = pd.DataFrame()
    timer.mark("audit_linker")

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
    prepack_outcomes = outcomes.copy()

    log.info("Packing past-due forecast_dates (FFD / Cap_eff, day_total_mult=1.00)…")
    # 2026-08-17 round 2/3: no concentrate_batch. Overlay cap is production.
    # 2026-08-18 round 4: payer-week envelope tilt failed the gate (day-1
    # 4/8, weekly worse 3/8). ENVELOPE_TILT_LIVE stays False.
    # 2026-08-19 round 5: cadence_floor + weekday-stat knobs failed
    # in-sample weekly ≤ B1 (cadence_floor 23 Jul 16.2% > B1 15.9%).
    # Company Mon/Tue mean overshot OOS. batch_v2 beat v0 but not
    # normalize_1.00. Keep median weekday targets; concentrate off.
    sched_meta = {
        "scheduled_sum": 0.0,
        "packed_sum": 0.0,
        "scheduled_share": 0.0,
        "week1_share": 0.0,
        "week1_scheduled_sum": 0.0,
        "week1_pred_sum": 0.0,
    }
    sched_df = pd.DataFrame()
    if from_db:
        try:
            sched_df = _load_inflight_scheduled(
                as_of,
                eob_to_deposit=eob_to_deposit or {},
                pit=backtest,
                holiday_shifts=_holiday_shifts_from_schedules(deposit_schedules),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Inflight overlay skipped: %s", exc)
            sched_df = pd.DataFrame()
    if not sched_df.empty:
        wd_targets = weekday_deposit_targets(
            as_of=as_of,
            deposit_events=deposit_events,
            actual_cash_daily=actual.get("daily") if isinstance(actual, dict) else actual,
        )
        sched_df = cap_scheduled_by_slot(
            sched_df, wd_targets, day_total_mult=1.00, as_of=as_of
        )
    if pierce_batch:
        batch_slots, batch_ewma = tuesday_pierce_plan(
            deposit_events, as_of, horizon_days=horizon_days
        )
        pierce_kw: dict[str, object] = {
            "pierce_batch": True,
            "batch_ewma": batch_ewma,
        }
        log.info(
            "tue_pierce live one-shot grains=%d ewma_sum=%.0f",
            len(batch_slots),
            sum(batch_ewma.values()),
        )
    else:
        batch_slots, batch_ewma = batch_plan_by_grain(
            deposit_events,
            as_of,
            horizon_days=horizon_days,
            nynm_occupancy_events=nynm_occ or None,
        )
        pierce_kw = {"batch_ewma": batch_ewma}
        log.info(
            "batch_plan grains=%d ewma_sum=%.0f (phase_pierce via schedules)",
            len(batch_slots),
            sum(float(v or 0) for v in (batch_ewma or {}).values()),
        )
    sched_by_slot = scheduled_amounts_by_slot(sched_df) if not sched_df.empty else None
    if ORG_CAPACITY_PACKING:
        log.warning("ORG_CAPACITY_PACKING is True in code but ignored; pass --packing-grain org")
    eligible_orgs: set[str] | None = None
    if packing_mode == PACKING_GRAIN_ORG:
        try:
            eligible_orgs = _preflight_eligible_orgs(outcomes)
        except RuntimeError as exc:
            log.error("%s", exc)
            return 2
    outcomes = exclude_stream_ar(outcomes)
    if sched_df is not None and not sched_df.empty:
        outcomes = offset_known_from_ar(outcomes, sched_df)
    nynm_stream = offset_stream_by_known(
        nynm_stream_frame(nynm_occ or deposit_events, as_of),
        sched_df,
    )
    outcomes, reschedule_audit, slot_audit, capacity_df = pack_pastdue_ffd(
        outcomes,
        as_of=as_of,
        deposit_events=deposit_events,
        deposit_schedules=deposit_schedules or None,
        risk_patient_dos=risk_keys,
        actual_cash_daily=actual.get("daily"),
        day_total_mult=1.00,
        horizon_days=horizon_days,
        batch_slots=batch_slots or None,
        scheduled_by_slot=sched_by_slot,
        packing_grain_mode=packing_mode,
        eligible_orgs=eligible_orgs,
        stream_by_slot=_stream_by_slot(nynm_stream),
        **pierce_kw,
    )
    log.info("  rescheduled %d past-due rows", len(reschedule_audit))
    timer.mark("pack")
    if not sched_df.empty:
        outcomes, sched_meta = overlay_scheduled(outcomes, sched_df, as_of=as_of)
        log.info(
            "  inflight overlay scheduled=$%.0f share=%.1f%% week1_share=%.1f%%",
            sched_meta["scheduled_sum"],
            100.0 * sched_meta["scheduled_share"],
            100.0 * sched_meta.get("week1_share", 0.0),
        )
    outcomes = apply_live_envelope_tilt(
        outcomes,
        as_of=as_of,
        deposit_events=deposit_events,
        actual_daily=actual.get("daily") if isinstance(actual, dict) else actual,
        output_dir=output_dir,
        backtest=backtest,
    )
    outcomes = _append_recurring_components(
        outcomes,
        as_of=as_of,
        deposit_events=nynm_occ or deposit_events,
        known=sched_df,
        from_db=from_db,
        stream=nynm_stream,
    )
    outcomes = tag_residual(outcomes)
    try:
        assert_exclusive(outcomes)
    except AssertionError as exc:
        log.error("forecast component exclusivity failed: %s", exc)
        return 2
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
        window_start=disp_start,
        window_end=disp_end,
    )
    cov = day_ahead_coverage(as_of=as_of, scheduled=sched_df, packed=outcomes)
    kpi["day_ahead_coverage"] = cov
    kpi["tomorrow_coverage_pct"] = cov["coverage_pct"]
    kpi["tomorrow_eligible_for_98"] = cov["eligible_for_98"]
    kpi["tomorrow_note"] = cov["note"]
    log.info(
        "Day-ahead coverage %s: scheduled=$%.0f packed=$%.0f coverage=%.1f%% eligible_98=%s",
        cov["slot"],
        cov["scheduled_sum"],
        cov["packed_sum"],
        cov["coverage_pct"],
        cov["eligible_for_98"],
    )

    # Diagnostic land frame (future days only in live runs — typically empty).
    future_days = eval_dates(as_of, horizon_days)
    land_acc = build_land_accuracy_frame(outcomes, actual["daily"], dates=future_days)
    land_acc_all = build_land_accuracy_frame(outcomes, actual["daily"])
    land_summary = summarize_error_metrics(land_acc if not land_acc.empty else land_acc_all)
    if land_acc is not None and not land_acc.empty and (land_acc["actual"] > 0).any():
        log.info(
            "Land accuracy diagnostic (days after %s): WAPE=%s MAPE=%s Bias=%s n=%s",
            as_of,
            land_summary.get("wape"),
            land_summary.get("mape"),
            land_summary.get("bias"),
            land_summary.get("n_days"),
        )
    else:
        log.info(
            "Land accuracy diagnostic skipped — no scored days after as_of=%s "
            "(use --backtest for walk-forward evaluation)",
            as_of,
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
        # Display-window filtered views (Jan 1 of as_of year → month of forward end)
        "projected_cash_daily_may_aug": filter_period_to_window(
            projected["daily"], start=disp_start, end=disp_end
        ),
        "projected_cash_weekly_may_aug": filter_period_to_window(
            projected["weekly"], start=disp_start, end=disp_end
        ),
        "projected_cash_monthly_may_aug": filter_period_to_window(
            projected["monthly"], start=disp_start, end=disp_end
        ),
        "projected_cash_daily_by_insurance_may_aug": filter_period_to_window(
            projected_ins["daily"], start=disp_start, end=disp_end
        ),
        "projected_cash_weekly_by_insurance_may_aug": filter_period_to_window(
            projected_ins["weekly"], start=disp_start, end=disp_end
        ),
        "projected_cash_monthly_by_insurance_may_aug": filter_period_to_window(
            projected_ins["monthly"], start=disp_start, end=disp_end
        ),
        "projected_cash_daily_by_facility_may_aug": filter_period_to_window(
            projected_fac["daily"], start=disp_start, end=disp_end
        ),
        "projected_cash_weekly_by_facility_may_aug": filter_period_to_window(
            projected_fac["weekly"], start=disp_start, end=disp_end
        ),
        "projected_cash_monthly_by_facility_may_aug": filter_period_to_window(
            projected_fac["monthly"], start=disp_start, end=disp_end
        ),
        "projected_cash_monthly_by_facility_insurance_may_aug": filter_period_to_window(
            projected_cross, start=disp_start, end=disp_end
        ),
        "actual_cash_daily_may_aug": filter_period_to_window(
            actual["daily"], start=disp_start, end=disp_end
        ),
        "actual_cash_weekly_may_aug": filter_period_to_window(
            actual["weekly"], start=disp_start, end=disp_end
        ),
        "actual_cash_monthly_may_aug": filter_period_to_window(
            actual["monthly"], start=disp_start, end=disp_end
        ),
        "forward_visits_august": forward_summary,
        "overdue_by_insurance": overdue_by_insurance(outcomes),
        "denied_by_insurance": denied_by_insurance(outcomes),
        "risk_by_insurance": risk_by_insurance(risk),
        "outcome_stage_counts": outcome_stage_counts(outcomes),
        "kpi_summary": kpi,
    }
    if from_db and not backtest:
        from cashflow_forecast.db_source import write_forecast_run

        feature_tables = {
            "payer_sla": sla,
            "risk_flags": risk if risk is not None else pd.DataFrame(),
            "actual_cash_daily": actual["daily"],
            "projected_cash_daily": projected["daily"],
            "projected_cash_monthly": projected["monthly"],
            "projected_cash_monthly_by_facility": projected_fac["monthly"],
            "projected_cash_monthly_by_insurance": projected_ins["monthly"],
            "deposit_capacity": capacity_df if capacity_df is not None else pd.DataFrame(),
            "payment_models": payment_models_df,
            "kpi_summary": pd.DataFrame([kpi]) if isinstance(kpi, dict) else pd.DataFrame(),
            "outcome_stage_counts": outcome_stage_counts(outcomes),
            "overdue_by_insurance": overdue_by_insurance(outcomes),
            "risk_totals_by_insurance": risk_totals_by_insurance(
                risk if risk is not None else pd.DataFrame()
            ),
        }
        run_id = write_forecast_run(
            algorithm_version="forecast-build-tue-pierce" if pierce_batch else "forecast-build",
            as_of_date=as_of,
            outcome_df=outcomes,
            feature_tables=feature_tables,
            rules_version="business_rules",
            params={
                "as_of": as_of.isoformat(),
                "pierce_batch": pierce_batch,
                "sheet_n": int(leakage_bits.get("sheet_n") or 0),
                "sheet_paid": int(leakage_bits.get("sheet_paid") or 0),
                "sheet_denied": int(leakage_bits.get("sheet_denied") or 0),
            },
        )
        log.info("Wrote forecast_run %s to DB", run_id)
        timer.mark("persist")
    elif backtest:
        log.info("Backtest mode: skipped write_forecast_run")

    if backtest:
        dates = eval_dates(as_of, horizon_days)
        holdout = None
        if holdout_path:
            holdout = load_holdout_csv(_resolve_path(holdout_path))
            log.info("Loaded holdout actuals from %s (%d days)", holdout_path, len(holdout))
        hist = actual_by_day(actual["daily"])
        score_actual = eval_actual["daily"] if eval_actual is not None else actual["daily"]
        result = evaluate_backtest(
            outcomes=outcomes,
            actual_daily=score_actual,
            history_actual=hist,
            as_of=as_of,
            dates=dates,
            holdout=holdout,
        )
        manifest = build_leakage_manifest(
            as_of=as_of,
            recon_lines=recon_lines,
            recon_reopened=int(leakage_bits["recon_reopened"]),
            payments=payments,
            deposits=deposits if "deposits" in locals() and deposits is not None else pd.DataFrame(),
            denials=denials if "denials" in locals() else None,
            rejections=rejections if "rejections" in locals() else None,
            sf_overrides_n=int(leakage_bits["sf_n"]),
            sf_overrides_disabled=bool(leakage_bits["sf_disabled"]),
            ib_source=str(leakage_bits["ib_source"]),
            poc_caveat=str(leakage_bits["poc_caveat"]),
            reconciliation_run_id=str(leakage_bits.get("recon_run_id") or "") or None,
            spine_conservation=leakage_bits.get("spine_conservation")
            if isinstance(leakage_bits.get("spine_conservation"), dict)
            else None,
        )
        manifest["packing_grain"] = packing_mode
        manifest["eligible_orgs"] = sorted(eligible_orgs or [])
        manifest["sheet_overrides"] = {
            "keys": int(leakage_bits.get("sheet_n") or 0),
            "paid": int(leakage_bits.get("sheet_paid") or 0),
            "denied": int(leakage_bits.get("sheet_denied") or 0),
            "disabled": bool(leakage_bits.get("sheet_disabled")),
        }
        write_backtest_outputs(output_dir, result, manifest)
        summary_path = output_dir / "summary.json"
        if summary_path.exists() and sched_meta.get("scheduled_sum"):
            import json as _json

            packed_summary = _json.loads(summary_path.read_text(encoding="utf-8"))
            packed_summary["scheduled"] = {
                "share": sched_meta.get("scheduled_share"),
                "sum": sched_meta.get("scheduled_sum"),
                "packed_sum": sched_meta.get("packed_sum"),
                "week1_share": sched_meta.get("week1_share"),
                "week1_scheduled_sum": sched_meta.get("week1_scheduled_sum"),
                "week1_pred_sum": sched_meta.get("week1_pred_sum"),
            }
            summary_path.write_text(_json.dumps(packed_summary, indent=2, default=str), encoding="utf-8")
        log.info("Wrote backtest pack -> %s", output_dir)
        dm = result["daily_model"]
        print(f"\nBacktest as_of={as_of} window={dates[0]}..{dates[-1]}")
        print(
            f"  Packed daily WAPE={dm.get('wape')}  B1={result['daily_b1'].get('wape')}  "
            f"B2={result['daily_b2'].get('wape')}  MAE={dm.get('mae')}  "
            f"bias={dm.get('bias')}  n={dm.get('n_days')}"
        )
        wm = result["weekly_model"]
        print(
            f"  Packed weekly WAPE={wm.get('wape')}  B1={result['weekly_b1'].get('wape')}  "
            f"B2={result['weekly_b2'].get('wape')}"
        )
        land_dm = result.get("land", {}).get("daily_model") or {}
        land_wm = result.get("land", {}).get("weekly_model") or {}
        if land_dm:
            print(
                f"  Land daily WAPE={land_dm.get('wape')}  weekly={land_wm.get('wape')}  "
                f"(diagnostic)"
            )
        if getattr(args, "pack_variants", False):
            pv_specs = [dict(ORG_CAPACITY_SPEC)] if packing_mode == PACKING_GRAIN_ORG else None
            run_pack_variants(
                prepack_outcomes=prepack_outcomes,
                as_of=as_of,
                dates=dates,
                deposit_events=deposit_events,
                deposit_schedules=deposit_schedules or None,
                actual_daily=score_actual,
                history_actual=hist,
                holdout=holdout,
                output_dir=output_dir,
                risk_patient_dos=risk_keys,
                horizon_days=horizon_days,
                batch_slots=batch_slots or None,
                batch_ewma=batch_ewma or None,
                scheduled=sched_df if not sched_df.empty else None,
                variants=pv_specs,
                packing_grain_mode=packing_mode,
                eligible_orgs=eligible_orgs,
                recon_run_id=str(leakage_bits.get("recon_run_id") or "") or None,
            )
    if emit_csv:
        export_all(output_dir, artifacts)
        log.info("Wrote diagnostic CSV pack -> %s", output_dir)

    log.info("KPI summary: %s", kpi)
    log.info("phase totals %s elapsed %.1fs", timer.phases, time.perf_counter() - timer.t0)
    print(f"\nWrote forecast outputs -> {'DB' if from_db else output_dir}")
    print(f"  Actual cash:    ${kpi['actual_cash_received']:,.2f}")
    print(f"  Projected:      ${kpi['projected_cash_in']:,.2f}")
    print(f"  Jan–Aug proj:   ${kpi.get('projected_cash_may_aug', 0):,.2f}")
    print(f"  On track:       ${kpi['on_track_amount']:,.2f} ({kpi['on_track_count']} lines)")
    print(f"  Overdue:        ${kpi['overdue_amount']:,.2f} ({kpi['overdue_count']} lines)")
    print(f"  Denied+Reject:  ${kpi['denied_amount']:,.2f} ({kpi['denied_count']} lines)")
    print(f"  Risk exposure:  ${kpi['risk_exposure_amount']:,.2f} ({kpi['risk_visit_count']} visits)")
    if not backtest and land_summary.get("n_days") and land_summary.get("wape") is not None:
        print(
            f"  Land diagnostic: WAPE={land_summary['wape']:.1%}  "
            f"MAPE={land_summary['mape']:.1%}  "
            f"Bias=${land_summary['bias']:,.0f}  "
            f"(n={land_summary['n_days']} days after as_of)"
        )
    if not forward_summary.empty:
        print(
            f"  Aug forward:    {int(forward_summary['projected_visit_count'].sum())} visits / "
            f"${float(forward_summary.get('expected_amount', pd.Series(dtype=float)).sum()):,.2f}"
        )
    return 0


def cmd_pack_variants(args: argparse.Namespace) -> int:
    import json

    output_dir = _resolve_path(args.dir)
    stages_path = output_dir / "outcome_stages.csv"
    summary_path = output_dir / "summary.json"
    if not stages_path.exists() or not summary_path.exists():
        log.error("Need outcome_stages.csv and summary.json in %s", output_dir)
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    as_of = _parse_as_of(str(summary["as_of"]))
    dates = [str(d)[:10] for d in summary.get("eval_dates") or []]
    outcomes = pd.read_csv(stages_path)
    for col in ("forecast_date", "original_forecast_date", "date_of_service"):
        if col in outcomes.columns:
            outcomes[col] = pd.to_datetime(outcomes[col], errors="coerce").dt.date
    hist_path = output_dir / "actual_cash_daily.csv"
    hist_df = pd.read_csv(hist_path) if hist_path.exists() else pd.DataFrame()
    hist = actual_by_day(hist_df) if not hist_df.empty else pd.Series(dtype=float)
    holdout = None
    holdout_path = getattr(args, "holdout_csv", None)
    if holdout_path:
        holdout = load_holdout_csv(_resolve_path(holdout_path))
    score_actual = hist_df
    deposit_events = []
    if getattr(args, "from_db", False):
        from cashflow_forecast import db_source as dbs
        from cashflow_forecast.forecast_engine import actual_cash_buckets_from_deposits

        deposits_eval = dbs.load_deposits_df()
        if not deposits_eval.empty:
            score_actual = actual_cash_buckets_from_deposits(deposits_eval)["daily"]
        score_df = dbs.load_scoring_actuals_df()
        if not score_df.empty:
            score_actual = actual_cash_buckets_from_deposits(score_df)["daily"]
        payments = dbs.load_payments_unified_df(as_of=as_of)
        actual_ins = (
            actual_cash_buckets_by_insurance(payments)
            if not payments.empty
            else {"daily": pd.DataFrame()}
        )
        deposit_events = build_deposit_events_from_actual(actual_ins["daily"], as_of=as_of)
        if not deposit_events:
            checks_df = dbs.load_checks_timeline_df()
            if not checks_df.empty:
                deposit_events = build_deposit_events_from_checks(checks_df, as_of=as_of)
        deposit_events = _enrich_deposit_events_with_sheet(deposit_events, as_of)
        nynm_occ_pv = _nynm_tracker_occupancy(as_of)
    else:
        ins_path = output_dir / "actual_cash_daily_by_insurance.csv"
        if ins_path.exists():
            ins_df = pd.read_csv(ins_path)
            deposit_events = build_deposit_events_from_actual(ins_df, as_of=as_of)
        nynm_occ_pv = []
    scheduled = pd.DataFrame()
    if getattr(args, "from_db", False):
        try:
            scheduled = _load_inflight_scheduled(as_of, pit=True)
            log.info(
                "Inflight scheduled rows=%d sum=$%.0f",
                len(scheduled),
                float(scheduled["expected_amount"].sum()) if not scheduled.empty else 0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Inflight overlay skipped: %s", exc)
    if holdout is None and score_actual is hist_df:
        log.warning("Scoring against pack actuals (may be PIT-gated); prefer --from-db or --holdout-csv")
    variants = None
    names = getattr(args, "variants", None)
    try:
        packing_mode = _cli_packing_grain(args)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    eligible_orgs: set[str] | None = None
    recon_run_id = ""
    man_path = output_dir / "leakage_manifest.json"
    if man_path.exists():
        try:
            man = json.loads(man_path.read_text(encoding="utf-8"))
            recon_run_id = str(man.get("reconciliation_run_id") or "")
            if not recon_run_id:
                recon_run_id = str(
                    ((man.get("inputs") or {}).get("reconciliation_lines") or {}).get(
                        "reconciliation_run_id"
                    )
                    or ""
                )
        except (OSError, json.JSONDecodeError):
            recon_run_id = ""
    if packing_mode == PACKING_GRAIN_ORG:
        if not getattr(args, "from_db", False):
            log.error("--packing-grain org requires --from-db (tracker payors)")
            return 2
        try:
            eligible_orgs = _preflight_eligible_orgs(outcomes)
        except RuntimeError as exc:
            log.error("%s", exc)
            return 2
        variants = [dict(ORG_CAPACITY_SPEC)]
        vdir_pre = output_dir / "pack_variants" / "org_capacity"
        vdir_pre.mkdir(parents=True, exist_ok=True)
        (vdir_pre / "eligible_orgs.json").write_text(
            json.dumps({"eligible_orgs": sorted(eligible_orgs)}, indent=2),
            encoding="utf-8",
        )
        if names:
            log.warning("Ignoring --variants %s; Layer 2B runs org_capacity only", names)
    elif names:
        want = {n.strip() for n in str(names).split(",") if n.strip()}
        variants = [s for s in VARIANT_SPECS if s["name"] in want]
        for n in sorted(want):
            if n in ISOLATED_SPECS and all(str(v.get("name")) != n for v in variants):
                variants.append(dict(ISOLATED_SPECS[n]))
        if not variants:
            log.error("No matching variants in %s", names)
            return 1
    pv_slots, pv_ewma = batch_plan_by_grain(
        deposit_events, as_of, horizon_days=14, nynm_occupancy_events=nynm_occ_pv or None
    )
    summary_df = run_pack_variants(
        prepack_outcomes=outcomes,
        as_of=as_of,
        dates=dates,
        deposit_events=deposit_events,
        deposit_schedules=None,
        actual_daily=score_actual if holdout is None else score_actual,
        history_actual=hist,
        holdout=holdout,
        output_dir=output_dir,
        variants=variants,
        horizon_days=14,
        batch_slots=pv_slots or None,
        batch_ewma=pv_ewma or None,
        scheduled=scheduled if not scheduled.empty else None,
        packing_grain_mode=packing_mode,
        eligible_orgs=eligible_orgs,
        recon_run_id=recon_run_id or None,
    )
    print(summary_df.to_string(index=False))
    gate_names: list[str] = []
    if packing_mode == PACKING_GRAIN_ORG:
        from cashflow_forecast.pack_experiment import ORG_CAPACITY_VARIANT

        gate_names.append(ORG_CAPACITY_VARIANT)
    if TUE_PIERCE_VARIANT in {str(v.get("name")) for v in (variants or [])}:
        gate_names.append(TUE_PIERCE_VARIANT)
    for vname in gate_names:
        vdir = output_dir / "pack_variants" / vname
        v_sum_path = vdir / "summary.json"
        b_daily_path = output_dir / "packed_accuracy_daily.csv"
        v_daily_path = vdir / "packed_accuracy_daily.csv"
        aetna_path = vdir / "aetna_grain_check.json"
        if not (v_sum_path.exists() and b_daily_path.exists() and v_daily_path.exists()):
            continue
        aetna = {}
        if aetna_path.exists():
            aetna = json.loads(aetna_path.read_text(encoding="utf-8"))
        gates = layer2b_gates(
            baseline_summary=summary,
            variant_summary=json.loads(v_sum_path.read_text(encoding="utf-8")),
            baseline_daily=pd.read_csv(b_daily_path),
            variant_daily=pd.read_csv(v_daily_path),
            as_of=as_of,
            aetna_check=aetna,
        )
        (vdir / "layer2b_gates.json").write_text(
            json.dumps(gates, indent=2, default=str), encoding="utf-8"
        )
        print(json.dumps(gates, indent=2, default=str))
        if not gates.get("passed"):
            log.warning("%s gates failed — leave production knobs unchanged, no adopt", vname)
        else:
            log.info("%s gates passed — OOS only on explicit follow-up, no auto-adopt", vname)
    return 0


def cmd_rescore(args: argparse.Namespace) -> int:
    if not getattr(args, "from_db", False):
        log.error("rescore requires --from-db")
        return 2
    from cashflow_forecast import db_source as dbs

    output_dir = _resolve_path(args.dir)
    score_df = dbs.load_scoring_actuals_df()
    if score_df.empty:
        score_df = dbs.load_deposits_df()
    if score_df.empty:
        log.error("No scoring actuals (tracker or bank_deposit)")
        return 1
    actual = actual_cash_buckets_from_deposits(score_df)
    result = rescore_backtest_pack(output_dir, actual["daily"])
    dm = result["daily_model"]
    print(f"\nRescored {output_dir}")
    print(
        f"  Daily WAPE model={dm.get('wape')}  B1={result['daily_b1'].get('wape')}  "
        f"B2={result['daily_b2'].get('wape')}  MAE={dm.get('mae')}  "
        f"bias={dm.get('bias')}  n={dm.get('n_days')}"
    )
    wm = result["weekly_model"]
    print(
        f"  Weekly WAPE model={wm.get('wape')}  B1={result['weekly_b1'].get('wape')}  "
        f"B2={result['weekly_b2'].get('wape')}"
    )
    packed = result.get("packed") or {}
    w1 = packed.get("week1_model") or {}
    w2 = packed.get("week2_model") or {}
    if w1:
        print(f"  Week-1 daily WAPE={w1.get('wape')}  acc={w1.get('accuracy')}  n={w1.get('n_days')}")
    if w2:
        print(f"  Week-2 daily WAPE={w2.get('wape')}  acc={w2.get('accuracy')}  n={w2.get('n_days')}")
    vsum = rescore_variant_tree(output_dir, actual["daily"])
    if vsum is not None and not vsum.empty:
        print("\nPack variants rescored:")
        print(vsum.to_string(index=False))
    return 0


DEFAULT_SWEEP_AS_OFS = (
    "2026-06-13",
    "2026-06-20",
    "2026-06-27",
    "2026-07-10",
    "2026-07-17",
    "2026-07-23",
    "2026-07-31",
    "2026-08-08",
)


def cmd_sweep(args: argparse.Namespace) -> int:
    """Walk-forward packs: classify missing as_ofs, re-pack existing, write sweep_summary."""
    as_ofs = [s.strip() for s in str(getattr(args, "as_ofs", "") or "").split(",") if s.strip()]
    if not as_ofs:
        as_ofs = list(DEFAULT_SWEEP_AS_OFS)
    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    existing = [a for a in as_ofs if (export_root / f"backtest_{a}" / "outcome_stages.csv").exists()]
    missing = [a for a in as_ofs if a not in existing]
    as_ofs = existing + missing
    rows: list[dict] = []
    for as_of_s in as_ofs:
        d = export_root / f"backtest_{as_of_s}"
        stages = d / "outcome_stages.csv"
        if not stages.exists():
            log.info("Sweep: classifying %s", as_of_s)
            ns = argparse.Namespace(
                from_db=True,
                backtest=True,
                emit_csv=True,
                data_dir=str(REPO_ROOT / "webpt_edco_scraper/output/jun_jul_2026"),
                output_dir=str(d),
                as_of=as_of_s,
                horizon_days=int(getattr(args, "horizon_days", 14) or 14),
                holdout_csv=None,
                pack_variants=True,
                rejections=None,
                denials=None,
                transaction_tracker=None,
            )
            rc = cmd_build(ns)
            if rc:
                log.error("Sweep build failed for %s rc=%s", as_of_s, rc)
                continue
        else:
            log.info("Sweep: pack-variants %s", as_of_s)
            ns = argparse.Namespace(
                dir=str(d),
                from_db=True,
                holdout_csv=None,
                variants=getattr(args, "variants", None),
            )
            rc = cmd_pack_variants(ns)
            if rc:
                log.error("Sweep pack-variants failed for %s rc=%s", as_of_s, rc)
        summary_path = d / "pack_variants" / "variants_summary.csv"
        if summary_path.exists():
            df = pd.read_csv(summary_path)
            df["as_of"] = as_of_s
            rows.append(df)
        else:
            sp = d / "summary.json"
            if sp.exists():
                import json

                s = json.loads(sp.read_text(encoding="utf-8"))
                packed = s.get("packed") or {}
                daily = (packed.get("daily") or {}).get("model") or s.get("daily") or {}
                weekly = (packed.get("weekly") or {}).get("model") or s.get("weekly") or {}
                w1 = (packed.get("week1") or {}).get("model") or {}
                rows.append(
                    pd.DataFrame(
                        [
                            {
                                "as_of": as_of_s,
                                "variant": "adopted",
                                "packed_daily_wape": (daily or {}).get("wape") if isinstance(daily, dict) else None,
                                "packed_weekly_wape": (weekly or {}).get("wape") if isinstance(weekly, dict) else None,
                                "week1_daily_wape": w1.get("wape"),
                                "week1_accuracy": w1.get("accuracy"),
                            }
                        ]
                    )
                )
    if rows:
        out = pd.concat(rows, ignore_index=True)
        dest = export_root / "sweep_summary.csv"
        if dest.exists():
            prev = pd.read_csv(dest)
            if "as_of" in prev.columns:
                replacing = set(as_ofs)
                prev = prev[~prev["as_of"].astype(str).str[:10].isin(replacing)]
                out = pd.concat([prev, out], ignore_index=True)
        out.to_csv(dest, index=False)
        print(out.to_string(index=False))
        print(f"Wrote {dest}")
        from cashflow_forecast.pack_experiment import apply_variant_gate

        gate = apply_variant_gate(out)
        gate_path = export_root / "variant_gate.csv"
        gate.to_csv(gate_path, index=False)
        print("\nVariant gate (OOS daily+weekly < v0, in-sample weekly ≤ B1):")
        print(gate.to_string(index=False))
        print(f"Wrote {gate_path}")
        winners = gate[gate["passed"]] if not gate.empty else gate
        if winners is not None and not winners.empty:
            print(f"GATE PASS: {', '.join(winners['variant'].astype(str))}")
        else:
            print("GATE: no new variant passed; keep adopted day_total_mult=1.00")
    return 0


def cmd_backfill_accuracy(args: argparse.Namespace) -> int:
    from cashflow_forecast import db_source as dbs
    from cashflow_forecast.land_accuracy import build_packed_accuracy_frame, summarize_land_accuracy
    from cashflow_ops.state import upsert_forecast_accuracy
    from cashflow_db.repository import connection, forecast as forecast_repo

    start = _parse_as_of(args.start)
    end = _parse_as_of(args.end)
    score_df = dbs.load_scoring_actuals_df()
    actual_daily = actual_cash_buckets_from_deposits(score_df)["daily"] if not score_df.empty else pd.DataFrame()
    n = 0
    with connection() as conn:
        for day in daterange(start, end):
            run_id = forecast_repo.get_prior_forecast_run_id(conn, before_as_of=day)
            if not run_id:
                log.warning("No prior forecast_run before %s", day)
                continue
            pred_rows = conn.execute(
                """
                SELECT outcome_stage, expected_amount,
                       payload->>'forecast_date' AS forecast_date,
                       payload->>'original_forecast_date' AS original_forecast_date
                FROM analytics.forecast_prediction
                WHERE forecast_run_id = %s::uuid
                """,
                (run_id,),
            ).fetchall()
            outcomes = pd.DataFrame([dict(r) for r in pred_rows])
            frame = build_packed_accuracy_frame(
                outcomes, actual_daily, dates=[day.isoformat()], as_of=date.today()
            )
            summary = summarize_land_accuracy(frame)
            upsert_forecast_accuracy(
                as_of_date=day,
                run_id=None,
                forecast_run_id=run_id,
                forecast_total=float(frame["pred"].sum()) if not frame.empty else None,
                actual_total=float(frame["actual"].sum()) if not frame.empty else None,
                mape=summary.get("mape"),
                bias=summary.get("bias"),
                rmse=summary.get("rmse"),
                accuracy=summary.get("accuracy"),
                per_insurance=[],
                details={
                    "official": "packed",
                    "backfill": True,
                    "focus_day": day.isoformat(),
                    "n_days": summary.get("n_days"),
                },
            )
            n += 1
            print(
                f"{day} actual={float(frame['actual'].sum()) if not frame.empty else 0} "
                f"pred={float(frame['pred'].sum()) if not frame.empty else 0} "
                f"acc={summary.get('accuracy')}"
            )
    print(f"Backfilled {n} accuracy days")
    return 0


def cmd_leadtime_audit(args: argparse.Namespace) -> int:
    from cashflow_db.repository import connection, payments as pay_repo

    dest = Path(getattr(args, "output", None) or "/data/exports/inflight_leadtime.csv")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        rows = pay_repo.get_eob_deposit_leadtime(conn)
    df = pd.DataFrame(rows)
    if df.empty:
        print("No matched EOB/tracker rows")
        df.to_csv(dest, index=False)
        return 0
    df.to_csv(dest, index=False)
    dollars = pd.to_numeric(df["matched_dollars"], errors="coerce").fillna(0)
    share = pd.to_numeric(df["share_lead_ge_1"], errors="coerce").fillna(0)
    knowable = float((dollars * share).sum())
    total = float(dollars.sum())
    frac = knowable / total if total > 1e-9 else 0.0
    print(df.head(20).to_string(index=False))
    print(f"\nMatched ${total:,.0f}. Knowable (lead>=1 day) ${knowable:,.0f} ({frac:.1%}).")
    print(f"Wrote {dest}")
    return 0


def cmd_weekly_report(args: argparse.Namespace) -> int:
    import json

    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    rows: list[dict] = []
    by_as_of: dict[str, dict] = {}
    for d in sorted(export_root.glob("backtest_*/summary.json")):
        s = json.loads(d.read_text(encoding="utf-8"))
        packed = s.get("packed") or {}
        daily = (packed.get("daily") or {}).get("model") or {}
        weekly = (packed.get("weekly") or {}).get("model") or {}
        w1 = (packed.get("week1") or {}).get("model") or {}
        as_of = str(s.get("as_of") or d.parent.name.replace("backtest_", ""))[:10]
        integrity = s.get("data_integrity") or {}
        by_as_of[as_of] = {
            "as_of": as_of,
            "week1_accuracy": w1.get("accuracy"),
            "week1_wape": w1.get("wape"),
            "week1_n_days": w1.get("n_days"),
            "packed_daily_wape": daily.get("wape"),
            "packed_weekly_wape": weekly.get("wape"),
            "scheduled_share": (s.get("scheduled") or {}).get("week1_share")
            or (s.get("scheduled") or {}).get("share"),
            "n_missing_days": integrity.get("n_missing_days", 0),
            "beats_b1": packed.get("beats_b1_wape_week1"),
        }
    sweep_path = export_root / "sweep_summary.csv"
    if sweep_path.exists():
        sdf = pd.read_csv(sweep_path)
        if "as_of" in sdf.columns and "variant" in sdf.columns:
            gate_path = export_root / "variant_gate.csv"
            winner = "normalize_1.00"
            if gate_path.exists():
                gdf = pd.read_csv(gate_path)
                passed = gdf[gdf["passed"].astype(str).str.lower().isin(["true", "1"])]
                if not passed.empty:
                    winner = str(passed.iloc[0]["variant"])
            adopted = sdf[sdf["variant"].astype(str) == winner]
            if adopted.empty:
                adopted = sdf[sdf["variant"].astype(str).isin(["headroom_0.75", "adopted", "normalize_1.00"])]
            for as_of, grp in adopted.groupby(adopted["as_of"].astype(str).str[:10]):
                row = grp.iloc[0].to_dict()
                cur = by_as_of.setdefault(as_of, {"as_of": as_of})
                if row.get("week1_accuracy") is not None:
                    cur["week1_accuracy"] = row.get("week1_accuracy")
                if row.get("week1_daily_wape") is not None:
                    cur["week1_wape"] = row.get("week1_daily_wape")
                if row.get("packed_daily_wape") is not None:
                    cur["packed_daily_wape"] = row.get("packed_daily_wape")
                if row.get("packed_weekly_wape") is not None:
                    cur["packed_weekly_wape"] = row.get("packed_weekly_wape")
                if row.get("week1_share") is not None and not pd.isna(row.get("week1_share")):
                    cur["scheduled_share"] = row.get("week1_share")
    rows = [by_as_of[k] for k in sorted(by_as_of)]
    dest = Path(getattr(args, "output_dir", None) or (export_root / "accuracy_weekly"))
    env_path = export_root / "envelope_eval.csv"
    if env_path.exists():
        edf = pd.read_csv(env_path)
        if "as_of" in edf.columns and "scenario" in edf.columns:
            primary = "mon_thu"
            dec_path = export_root / "envelope_eval_decision.json"
            if dec_path.exists():
                try:
                    primary = str(json.loads(dec_path.read_text(encoding="utf-8")).get("primary_scenario") or primary)
                except Exception:  # noqa: BLE001
                    pass
            sub = edf[edf["scenario"].astype(str) == primary]
            by_day1 = {
                str(r["as_of"])[:10]: r.get("payer_day1_acc")
                for r in sub.to_dict(orient="records")
            }
            for row in rows:
                d1 = by_day1.get(str(row.get("as_of"))[:10])
                if d1 is not None and not (isinstance(d1, float) and pd.isna(d1)):
                    row["day1_accuracy"] = d1
    path = write_weekly_report(rows, dest, target=TARGET_ACCURACY)
    print(path.read_text(encoding="utf-8"))
    gate_path = export_root / "variant_gate.csv"
    if gate_path.exists():
        print("\nVariant gate:")
        print(pd.read_csv(gate_path).to_string(index=False))
    return 0


def cmd_pit_audit(args: argparse.Namespace) -> int:
    import json

    from cashflow_db.repository import connection, payments as pay_repo
    from cashflow_forecast.week_envelope import interpret_pit_audit

    dest = Path(getattr(args, "output", None) or "/data/exports/pit_audit.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with connection() as conn:
        tracker = pay_repo.audit_tracker_created_at_degeneracy(conn)
        eob = pay_repo.audit_eob_etl_availability(conn)
        lags = pay_repo.audit_tracker_entry_lag(conn)
    audit = interpret_pit_audit(tracker, eob, lags)
    payload = {
        "real_pit_usable": audit.real_pit_usable,
        "primary_scenario": audit.primary_scenario,
        "reason": audit.reason,
        "tracker": {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in (audit.tracker or {}).items()},
        "eob": audit.eob,
        "n_lag_days": len(lags),
    }
    dest.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    if lags:
        pd.DataFrame(lags).to_csv(dest.with_suffix(".csv"), index=False)
    print(json.dumps(payload, indent=2, default=str))
    print(f"Wrote {dest}")
    return 0


def cmd_weekday_attribution(args: argparse.Namespace) -> int:
    """Who causes the Mon/Tue misses? Payer × weekday gap vs the frozen pack."""
    import json

    from cashflow_db.repository import connection, payments as pay_repo
    from cashflow_forecast.inflight_ledger import week1_bank_days
    from cashflow_forecast.payer_plan import resolve_payer_plan
    from cashflow_forecast.week_envelope import load_packed_outcomes_csv, packed_by_grain_day

    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    as_ofs = [s.strip() for s in str(getattr(args, "as_ofs", "") or "").split(",") if s.strip()]
    if not as_ofs:
        as_ofs = list(DEFAULT_SWEEP_AS_OFS)
    weekdays = {
        int(x) for x in str(getattr(args, "weekdays", "0,1") or "0,1").split(",") if x.strip() != ""
    }

    with connection() as conn:
        payer_rows = pay_repo.get_tracker_payer_daily(conn)
    actual_by_grain_day: dict[tuple[str, date], float] = {}
    for row in payer_rows:
        period = row.get("period")
        if period is None:
            continue
        payor = str(row.get("payor") or "")
        if payor == "__unmatched__" or not payor:
            grain = "__unmatched__"
        else:
            key = resolve_payer_plan(payor)
            grain = f"plan:{key.plan_key}" if key.plan_key else "__unmatched__"
        k = (grain, period)
        actual_by_grain_day[k] = actual_by_grain_day.get(k, 0.0) + float(row.get("amount") or 0)

    agg: dict[tuple[str, int], dict[str, float]] = {}
    n_windows_seen = 0
    for raw in as_ofs:
        as_of = _parse_as_of(raw)
        pack_dir = export_root / f"backtest_{as_of.isoformat()}"
        if not (pack_dir / "outcome_stages.csv").exists():
            log.warning("Skip %s — no outcome_stages.csv", as_of)
            continue
        n_windows_seen += 1
        days = [d for d in week1_bank_days(as_of) if d.weekday() in weekdays]
        outcomes = load_packed_outcomes_csv(pack_dir)
        preds = packed_by_grain_day(outcomes, days)
        grains = set(preds)
        for (grain, d) in actual_by_grain_day:
            if d in days:
                grains.add(grain)
        for grain in grains:
            for d in days:
                actual = actual_by_grain_day.get((grain, d), 0.0)
                pred = float((preds.get(grain) or {}).get(d, 0.0) or 0.0)
                if actual <= 0 and pred <= 0:
                    continue
                key = (grain, d.weekday())
                cur = agg.setdefault(
                    key,
                    {"actual": 0.0, "pred": 0.0, "n_days": 0, "n_under": 0},
                )
                cur["actual"] += actual
                cur["pred"] += pred
                cur["n_days"] += 1
                if actual > pred + 1e-9:
                    cur["n_under"] += 1

    rows_out = []
    for (grain, wd), cur in agg.items():
        gap = cur["actual"] - cur["pred"]
        rows_out.append(
            {
                "grain": grain,
                "weekday": wd,
                "actual_sum": round(cur["actual"], 2),
                "pred_sum": round(cur["pred"], 2),
                "gap_sum": round(gap, 2),
                "n_days": int(cur["n_days"]),
                "n_days_under": int(cur["n_under"]),
            }
        )
    df = pd.DataFrame(rows_out).sort_values("gap_sum", ascending=False)
    dest = export_root / "weekday_attribution.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dest, index=False)

    by_grain = (
        df[df["grain"] != "__unmatched__"]
        .groupby("grain", as_index=False)
        .agg(actual_sum=("actual_sum", "sum"), pred_sum=("pred_sum", "sum"), gap_sum=("gap_sum", "sum"))
        .sort_values("gap_sum", ascending=False)
    )
    under = by_grain[by_grain["gap_sum"] > 0]
    total_under = float(under["gap_sum"].sum()) if not under.empty else 0.0
    top3 = under.head(3)
    top3_share = float(top3["gap_sum"].sum()) / total_under if total_under > 1e-9 else 0.0
    unmatched = df[df["grain"] == "__unmatched__"]
    unmatched_actual = float(unmatched["actual_sum"].sum()) if not unmatched.empty else 0.0
    total_actual = float(df["actual_sum"].sum()) or 1.0
    summary = {
        "weekdays": sorted(weekdays),
        "n_windows": n_windows_seen,
        "total_under_prediction": round(total_under, 2),
        "top3_grains": top3["grain"].tolist(),
        "top3_gap_sums": [round(float(x), 2) for x in top3["gap_sum"]],
        "top3_share_of_under": round(top3_share, 6),
        "unmatched_actual_share": round(unmatched_actual / total_actual, 6),
        "concentrated": bool(top3_share >= 0.6),
    }
    (export_root / "weekday_attribution_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(df.head(25).to_string(index=False))
    print()
    print(json.dumps(summary, indent=2))
    print(f"Wrote {dest}")
    return 0


def cmd_conservation_audit(args: argparse.Namespace) -> int:
    """Decompose Mon/Tue payer gaps into missing / stage / fee / timing leaks."""
    import json as json_mod

    from cashflow_db.repository import connection, payments as pay_repo, tracker as tracker_repo
    from cashflow_forecast.conservation_audit import (
        AUDIT_GRAIN_TAGS,
        DEFAULT_AS_OFS,
        run_conservation_audit,
        write_audit_outputs,
    )

    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    as_ofs = [s.strip() for s in str(getattr(args, "as_ofs", "") or "").split(",") if s.strip()]
    if not as_ofs:
        as_ofs = list(DEFAULT_AS_OFS)
    weekdays = {
        int(x) for x in str(getattr(args, "weekdays", "0,1") or "0,1").split(",") if x.strip() != ""
    }
    date_from = date.fromisoformat(min(as_ofs)[:10])
    with connection() as conn:
        payer_daily = pay_repo.get_tracker_payer_daily(conn)
        eob_rows = pay_repo.get_eob_payments_slim(
            conn, date_from=date_from, payor_tags=AUDIT_GRAIN_TAGS
        )
        lead_rows = pay_repo.get_eob_deposit_leadtime(conn)
        tracker_rows = tracker_repo.list_active_for_export(conn, date_from=date_from)
    log.info(
        "Conservation inputs payer_daily=%d eob=%d tracker=%d",
        len(payer_daily),
        len(eob_rows),
        len(tracker_rows),
    )
    df, summary = run_conservation_audit(
        export_root=export_root,
        as_ofs=as_ofs,
        payer_daily=payer_daily,
        eob_rows=eob_rows,
        lead_rows=lead_rows,
        tracker_rows=tracker_rows,
        weekdays=frozenset(weekdays),
    )
    dest = write_audit_outputs(df, summary, export_root)
    if not df.empty:
        cols = [
            c
            for c in (
                "as_of",
                "grain",
                "pred_week1",
                "actual_week1",
                "gap",
                "missing_paid",
                "stage_leak_paid",
                "fee_expected",
                "fee_paid",
                "stock_parked",
                "dominant_leak",
            )
            if c in df.columns
        ]
        print(df[cols].to_string(index=False))
    print()
    print(json_mod.dumps(summary, indent=2, default=str))
    print(f"Wrote {dest}")
    return 0


def cmd_dollar_lineage(args: argparse.Namespace) -> int:
    """Trace actual insurance dollars through identity / EOB / recon / packed date."""
    import json as json_mod

    from cashflow_db.repository import connection, payments as pay_repo, tracker as tracker_repo
    from cashflow_db.repository import forecast as forecast_repo
    from cashflow_db.repository import reconciliation as recon_repo
    from cashflow_forecast.dollar_lineage import (
        PIT_KIND_CURRENT,
        PIT_KIND_PACK_INPUT,
        PIT_KIND_PIT,
        PIT_KIND_UNAVAILABLE,
        pack_recon_run_id,
        run_dollar_lineage,
        scoring_days,
        _as_date as lin_date,
    )
    from cashflow_forecast.payer_plan import PACKING_GRAIN_ORG, PACKING_GRAIN_PLAN

    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    as_ofs = [s.strip() for s in str(getattr(args, "as_ofs", "") or "").split(",") if s.strip()]
    if not as_ofs:
        as_ofs = list(DEFAULT_SWEEP_AS_OFS) + ["2026-08-15"]
    weekdays = {
        int(x) for x in str(getattr(args, "weekdays", "0,1") or "0,1").split(",") if x.strip() != ""
    }
    pit_kind_arg = str(getattr(args, "recon_pit_kind", PIT_KIND_PIT) or PIT_KIND_PIT).strip().lower()
    scoring_mode = (
        PACKING_GRAIN_ORG
        if str(getattr(args, "scoring_grain", "plan") or "plan").strip().lower() == "org"
        else PACKING_GRAIN_PLAN
    )
    date_from = date.fromisoformat(min(as_ofs)[:10])
    with connection() as conn:
        payer_daily = pay_repo.get_tracker_payer_daily(conn)
        company_by_day: dict[date, float] = {}
        for row in payer_daily:
            d = lin_date(row.get("period"))
            if d is None:
                continue
            company_by_day[d] = company_by_day.get(d, 0.0) + float(row.get("amount") or 0)
        score_days: set[date] = set()
        for raw in as_ofs:
            as_of = date.fromisoformat(str(raw)[:10])
            score_days.update(scoring_days(as_of, frozenset(weekdays), company_by_day))
        tracker_rows = tracker_repo.list_active_for_export(
            conn,
            date_from=min(score_days) if score_days else date_from,
            date_to=max(score_days) if score_days else None,
        )
        efts: list[str] = []
        for trow in tracker_rows:
            td = lin_date(trow.get("txn_date"))
            if td not in score_days:
                continue
            for k in ("eft_1", "eft_2", "check_reference"):
                raw_ref = str(trow.get(k) or "").strip()
                if raw_ref:
                    efts.append(raw_ref)
        efts = list(dict.fromkeys(efts))
        eob_rows = pay_repo.get_eob_payments_slim(conn, eft_nums=efts) if efts else []
        patients: list[str] = []
        for erow in eob_rows:
            for k in ("webpt_patient_id", "name_key", "revflow_patient_id"):
                pid = str(erow.get(k) or "").strip()
                if pid:
                    patients.append(pid)
                    break
        patient_ids = list(dict.fromkeys(patients))
        recon_by_as_of: dict[str, dict] = {}
        latest_id = recon_repo.latest_reconciliation_run_id(conn)
        for raw in as_ofs:
            as_of = date.fromisoformat(str(raw)[:10])
            pack_dir = export_root / f"backtest_{as_of.isoformat()}"
            rid = None
            kind = pit_kind_arg
            if pit_kind_arg == PIT_KIND_CURRENT:
                rid = latest_id
            elif pit_kind_arg == PIT_KIND_PACK_INPUT:
                rid = pack_recon_run_id(pack_dir)
                if not rid:
                    rid = forecast_repo.get_forecast_recon_run_id(conn, as_of=as_of)
                if not rid:
                    kind = PIT_KIND_UNAVAILABLE
            else:
                rid = recon_repo.reconciliation_run_as_of(conn, as_of)
                kind = PIT_KIND_PIT if rid else PIT_KIND_UNAVAILABLE
            keys: set[tuple[str, str, str]] = set()
            if rid and patient_ids:
                keys = recon_repo.get_recon_visit_keys_for_patients(
                    conn,
                    patient_ids,
                    as_of=as_of,
                    run_id=rid,
                )
            recon_by_as_of[as_of.isoformat()] = {
                "pit_kind": kind,
                "recon_run_id": rid or "",
                "keys": keys,
            }
            log.info(
                "Lineage recon as_of=%s kind=%s run=%s keys=%d",
                as_of,
                kind,
                rid or "(none)",
                len(keys),
            )
        log.info(
            "Lineage inputs payer_daily=%d tracker=%d efts=%d eob=%d scoring=%s pit=%s",
            len(payer_daily),
            len(tracker_rows),
            len(efts),
            len(eob_rows),
            scoring_mode,
            pit_kind_arg,
        )
        _df, summary = run_dollar_lineage(
            export_root=export_root,
            as_ofs=as_ofs,
            payer_daily=payer_daily,
            tracker_rows=tracker_rows,
            eob_rows=eob_rows,
            recon_by_as_of=recon_by_as_of,
            weekdays=frozenset(weekdays),
            pass2=True,
            scoring_mode=scoring_mode,
        )
    print(json_mod.dumps({"company": summary.get("company"), "root": summary.get("root")}, indent=2, default=str))
    print(f"Wrote {export_root}/universal_dollar_lineage.csv")
    return 0


def cmd_spine_conservation(args: argparse.Namespace) -> int:
    """Pack-input vs PIT vs current recon membership — Layer 0, no re-pack."""
    import json as json_mod

    from cashflow_db.repository import connection
    from cashflow_db.repository import forecast as forecast_repo
    from cashflow_db.repository import reconciliation as recon_repo
    from cashflow_forecast.dollar_lineage import (
        PIT_KIND_CURRENT,
        PIT_KIND_PACK_INPUT,
        PIT_KIND_PIT,
        PIT_KIND_UNAVAILABLE,
        load_outcomes_slim,
        pack_recon_run_id,
    )
    from cashflow_forecast.spine_coverage import recon_spine_conservation

    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    as_ofs = [s.strip() for s in str(getattr(args, "as_ofs", "") or "").split(",") if s.strip()]
    if not as_ofs:
        as_ofs = ["2026-08-15", "2026-08-08"]
    reports: list[dict] = []
    with connection() as conn:
        latest = recon_repo.latest_reconciliation_run_id(conn)
        for raw in as_ofs:
            as_of = date.fromisoformat(str(raw)[:10])
            pack = export_root / f"backtest_{as_of.isoformat()}"
            outcomes = load_outcomes_slim(pack)
            pack_rid = pack_recon_run_id(pack) or forecast_repo.get_forecast_recon_run_id(
                conn, as_of=as_of
            )
            pit_rid = recon_repo.reconciliation_run_as_of(conn, as_of)
            kinds = {
                PIT_KIND_PACK_INPUT: pack_rid,
                PIT_KIND_PIT: pit_rid,
                PIT_KIND_CURRENT: latest,
            }
            row: dict = {
                "as_of": as_of.isoformat(),
                "outcome_rows": int(len(outcomes)),
                "kinds": {},
            }
            for kind, rid in kinds.items():
                if not rid:
                    row["kinds"][kind] = {
                        "recon_run_id": "",
                        "pit_kind": PIT_KIND_UNAVAILABLE,
                    }
                    continue
                from cashflow_forecast import db_source as dbs

                recon_df = dbs.load_reconciliation_lines_df(run_id=rid, as_of=as_of)
                cons = recon_spine_conservation(recon_df, outcomes, as_of=as_of)
                cons["recon_run_id"] = rid
                cons["pit_kind"] = kind
                cons["recon_rows"] = int(len(recon_df))
                row["kinds"][kind] = cons
                log.info(
                    "spine %s %s run=%s eligible=%s classified=%s unmatched=%s",
                    as_of,
                    kind,
                    rid,
                    cons.get("eligible"),
                    cons.get("classified"),
                    cons.get("unmatched"),
                )
            reports.append(row)
    out_path = export_root / "spine_pit_report.json"
    out_path.write_text(json_mod.dumps(reports, indent=2, default=str), encoding="utf-8")
    print(json_mod.dumps(reports, indent=2, default=str))
    print(f"Wrote {out_path}")
    return 0


def cmd_envelope_eval(args: argparse.Namespace) -> int:
    import json

    from cashflow_db.repository import connection, payments as pay_repo
    from cashflow_forecast import db_source as dbs
    from cashflow_forecast.forecast_engine import actual_cash_buckets_from_deposits
    from cashflow_forecast.week_envelope import (
        decide_envelope_adoption,
        evaluate_as_of,
        interpret_pit_audit,
        load_packed_outcomes_csv,
        repack_production,
        write_envelope_report,
    )

    export_root = Path(getattr(args, "export_root", None) or "/data/exports")
    as_ofs = [s.strip() for s in str(getattr(args, "as_ofs", "") or "").split(",") if s.strip()]
    if not as_ofs:
        as_ofs = list(DEFAULT_SWEEP_AS_OFS)
    with connection() as conn:
        tracker = pay_repo.audit_tracker_created_at_degeneracy(conn)
        eob = pay_repo.audit_eob_etl_availability(conn)
        lags = pay_repo.audit_tracker_entry_lag(conn)
        tracker_pit = pay_repo.get_tracker_availability_daily(conn)
        eob_pit = pay_repo.get_eob_availability_daily(conn)
    audit = interpret_pit_audit(tracker, eob, lags)
    log.info("PIT audit: usable=%s primary=%s — %s", audit.real_pit_usable, audit.primary_scenario, audit.reason)

    score_df = dbs.load_scoring_actuals_df()
    if score_df.empty:
        score_df = dbs.load_deposits_df()
    actual_daily = actual_cash_buckets_from_deposits(score_df)["daily"] if not score_df.empty else pd.DataFrame()

    summaries: list[dict] = []
    days: list[dict] = []
    scenarios = ["same_day", "lag_1", "mon_thu"]
    if audit.real_pit_usable:
        scenarios.append("real_pit")
    for raw in as_ofs:
        as_of = _parse_as_of(raw)
        pack_dir = export_root / f"backtest_{as_of.isoformat()}"
        stages = pack_dir / "outcome_stages.csv"
        if not stages.exists():
            log.warning("Skip %s — missing %s", as_of, stages)
            continue
        outcomes = load_packed_outcomes_csv(pack_dir)
        deposit_events = []
        scheduled = pd.DataFrame()
        if getattr(args, "from_db", False):
            payments = dbs.load_payments_unified_df(as_of=as_of)
            actual_ins = (
                actual_cash_buckets_by_insurance(payments)
                if not payments.empty
                else {"daily": pd.DataFrame()}
            )
            deposit_events = build_deposit_events_from_actual(actual_ins["daily"], as_of=as_of)
            deposit_events = _enrich_deposit_events_with_sheet(deposit_events, as_of)
            try:
                scheduled = _load_inflight_scheduled(as_of, pit=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("Inflight overlay skipped for %s: %s", as_of, exc)
            outcomes = repack_production(
                outcomes,
                as_of=as_of,
                deposit_events=deposit_events,
                actual_daily=actual_daily,
                scheduled=scheduled if not scheduled.empty else None,
            )
        else:
            ins_path = pack_dir / "actual_cash_daily_by_insurance.csv"
            if ins_path.exists():
                deposit_events = build_deposit_events_from_actual(pd.read_csv(ins_path), as_of=as_of)
        win_sum, win_days = evaluate_as_of(
            as_of=as_of,
            outcomes=outcomes,
            deposit_events=deposit_events,
            actual_daily=actual_daily,
            scenarios=scenarios,
            tracker_pit_rows=tracker_pit,
            eob_pit_rows=eob_pit,
            real_pit_usable=audit.real_pit_usable,
        )
        summaries.extend(win_sum)
        days.extend(win_days)
        log.info("Evaluated %s scenarios=%s", as_of, [s["scenario"] for s in win_sum])
    decision = decide_envelope_adoption(summaries, primary_scenario=audit.primary_scenario)
    md = write_envelope_report(
        export_root,
        audit=audit,
        summaries=summaries,
        days=days,
        decision=decision,
    )
    print(md.read_text(encoding="utf-8"))
    print(json.dumps(decision, indent=2, default=str))
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
    build.add_argument(
        "--backtest",
        action="store_true",
        help="Walk-forward eval only: no forecast_run write, PIT gates, write /data/exports/backtest_<as_of>/",
    )
    build.add_argument(
        "--horizon-days",
        type=int,
        default=14,
        help="Backtest eval window length after as_of (default 14)",
    )
    rescore = sub.add_parser(
        "rescore",
        help="Re-score an existing backtest pack against ungated bank_deposit actuals",
    )
    rescore.add_argument("--dir", required=True, help="Existing /data/exports/backtest_YYYY-MM-DD")
    rescore.add_argument("--from-db", action="store_true", help="Load bank_deposit from cashflow_db")
    rescore.set_defaults(func=cmd_rescore)

    build.add_argument(
        "--holdout-csv",
        default=None,
        help="Optional date,amount CSV of out-of-sample insurance deposits (never loaded to DB)",
    )
    build.add_argument(
        "--pack-variants",
        action="store_true",
        help="After backtest, re-pack the pre-pack snapshot across the experiment grid",
    )
    build.add_argument(
        "--packing-grain",
        choices=["plan", "org"],
        default="plan",
        help="Capacity packing grain. org = evidence-gated Layer 2B (does not overwrite baseline)",
    )
    build.add_argument(
        "--pierce-batch",
        action="store_true",
        help="Live one-shot: lumpy Tuesday EWMA after day_norm. Default off. Refuses --backtest.",
    )
    build.set_defaults(func=cmd_build)

    packv = sub.add_parser(
        "pack-variants",
        help="Re-pack an existing backtest pack's outcome_stages across the experiment grid",
    )
    packv.add_argument("--dir", required=True, help="Existing /data/exports/backtest_YYYY-MM-DD")
    packv.add_argument("--from-db", action="store_true", help="Rebuild deposit events from DB")
    packv.add_argument("--holdout-csv", default=None)
    packv.add_argument(
        "--variants",
        default=None,
        help="Comma-separated variant names (default: full grid)",
    )
    packv.add_argument(
        "--packing-grain",
        choices=["plan", "org"],
        default="plan",
        help="org = Layer 2B evidence-gated capacity merge into pack_variants/org_capacity",
    )
    packv.set_defaults(func=cmd_pack_variants)

    sweep = sub.add_parser("sweep", help="6–8 weekly as_of backtests with pack variants + W1/W2")
    sweep.add_argument("--from-db", action="store_true", default=True)
    sweep.add_argument("--as-ofs", default=",".join(DEFAULT_SWEEP_AS_OFS))
    sweep.add_argument("--export-root", default="/data/exports")
    sweep.add_argument("--horizon-days", type=int, default=14)
    sweep.add_argument(
        "--variants",
        default=None,
        help="Comma-separated variant subset (default: full grid)",
    )
    sweep.set_defaults(func=cmd_sweep)

    backfill = sub.add_parser("backfill-accuracy", help="Rewrite ops.forecast_accuracy_day from tracker actuals")
    backfill.add_argument("--start", required=True)
    backfill.add_argument("--end", required=True)
    backfill.set_defaults(func=cmd_backfill_accuracy)

    lead = sub.add_parser("leadtime-audit", help="EOB to tracker deposit lag per payer")
    lead.add_argument("--output", default="/data/exports/inflight_leadtime.csv")
    lead.set_defaults(func=cmd_leadtime_audit)

    weekly = sub.add_parser("weekly-report", help="Write weekly packed accuracy vs 90% report")
    weekly.add_argument("--export-root", default="/data/exports")
    weekly.add_argument("--output-dir", default=None)
    weekly.set_defaults(func=cmd_weekly_report)

    pit = sub.add_parser("pit-audit", help="Tracker created_at / EOB etl_run availability audit")
    pit.add_argument("--output", default="/data/exports/pit_audit.json")
    pit.set_defaults(func=cmd_pit_audit)

    envelope = sub.add_parser(
        "envelope-eval",
        help="Payer-week envelope redistribution: day-1 accuracy vs frozen pack + company control",
    )
    envelope.add_argument("--from-db", action="store_true", help="Rebuild deposit events + re-pack from DB")
    envelope.add_argument("--as-ofs", default=",".join(DEFAULT_SWEEP_AS_OFS))
    envelope.add_argument("--export-root", default="/data/exports")
    envelope.set_defaults(func=cmd_envelope_eval)

    attribution = sub.add_parser(
        "weekday-attribution",
        help="Payer x weekday gap vs frozen pack (who causes Mon/Tue misses)",
    )
    attribution.add_argument("--as-ofs", default=",".join(DEFAULT_SWEEP_AS_OFS))
    attribution.add_argument("--export-root", default="/data/exports")
    attribution.add_argument("--weekdays", default="0,1", help="0=Mon .. 4=Fri")
    attribution.set_defaults(func=cmd_weekday_attribution)

    conserv = sub.add_parser(
        "conservation-audit",
        help="Decompose Mon/Tue payer gaps into missing/stage/fee/timing leaks",
    )
    conserv.add_argument("--as-ofs", default=",".join(DEFAULT_SWEEP_AS_OFS))
    conserv.add_argument("--export-root", default="/data/exports")
    conserv.add_argument("--weekdays", default="0,1", help="0=Mon .. 4=Fri")
    conserv.set_defaults(func=cmd_conservation_audit)

    lineage = sub.add_parser(
        "dollar-lineage",
        help="Trace actual insurance dollars through recon/outcomes (read-only)",
    )
    lineage.add_argument("--as-ofs", default=",".join(DEFAULT_SWEEP_AS_OFS + ("2026-08-15",)))
    lineage.add_argument("--export-root", default="/data/exports")
    lineage.add_argument("--weekdays", default="0,1", help="0=Mon .. 4=Fri")
    lineage.add_argument(
        "--recon-pit-kind",
        default="pit",
        choices=("pit", "pack_input", "current"),
        help="PIT truth is recon finished by as_of. current is diagnostic only.",
    )
    lineage.add_argument(
        "--scoring-grain",
        default="plan",
        choices=("plan", "org"),
        help="plan=production packing identity. org=Layer 2A attribution only.",
    )
    lineage.set_defaults(func=cmd_dollar_lineage)

    spine = sub.add_parser(
        "spine-conservation",
        help="Layer 0: pack-input vs PIT vs current recon membership (read-only)",
    )
    spine.add_argument("--as-ofs", default="2026-08-15,2026-08-08")
    spine.add_argument("--export-root", default="/data/exports")
    spine.set_defaults(func=cmd_spine_conservation)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

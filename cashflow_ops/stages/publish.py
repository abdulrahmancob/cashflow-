"""Stage 8 — Publish + Monitor (snapshot, accuracy, report, notify)."""

from __future__ import annotations

from typing import Any

from cashflow_ops import alerts, report, state
from cashflow_ops.config import OPS_ARTIFACTS_DIR
from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class PublishMonitorStage:
    key = "publish_monitor"
    requires = ["forecast"]
    produces = [
        "daily_snapshot",
        "forecast_accuracy",
        "daily_report",
        "notifications",
    ]
    on_failure = FailurePolicy.CONTINUE_WITH_ALERT
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        dry = ctx.dry_run
        outputs: dict[str, Any] = {}
        stage_map = state.stage_statuses(ctx.run_id)

        # Pull forecast / recon ids from prior stage outputs
        forecast_run_id = None
        forecast_total = None
        reconciliation_run_id = None
        volumes: dict[str, Any] = {}
        for sr in state.list_stage_runs(ctx.run_id):
            outs = sr.get("outputs") or {}
            if sr["stage_key"] == "forecast":
                forecast_run_id = outs.get("forecast_run_id")
                forecast_total = outs.get("forecast_total")
            if sr["stage_key"] == "reconciliation":
                facts = (outs.get("match") or {}).get("summary") or {}
                reconciliation_run_id = facts.get("reconciliation_run_id") or facts.get(
                    "run_id"
                )
            if sr["stage_key"] == "validate_sources":
                volumes = outs.get("metrics") or {}

        accuracy = _compute_forecast_accuracy(
            ctx, forecast_run_id=forecast_run_id, dry_run=dry
        )
        outputs["forecast_accuracy"] = accuracy

        if not dry:
            state.upsert_forecast_accuracy(
                as_of_date=ctx.as_of_date,
                run_id=ctx.run_id,
                forecast_run_id=forecast_run_id,
                forecast_total=accuracy.get("forecast_total"),
                actual_total=accuracy.get("actual_total"),
                mape=accuracy.get("mape"),
                bias=accuracy.get("bias"),
                rmse=accuracy.get("rmse"),
                accuracy=accuracy.get("accuracy"),
                per_insurance=accuracy.get("per_insurance") or [],
                details=accuracy.get("details") or {},
            )

        summary = {
            "forecast_total": accuracy.get("forecast_total", forecast_total),
            "actual_total": accuracy.get("actual_total"),
            "mape": accuracy.get("mape"),
            "bias": accuracy.get("bias"),
            "forecast_run_id": forecast_run_id,
            "reconciliation_run_id": reconciliation_run_id,
            "dataset_version": ctx.dataset_version,
        }

        # Stamp lineage tables with dataset_version
        if not dry and ctx.dataset_version:
            try:
                from cashflow_db.repository import connection

                with connection() as conn:
                    if forecast_run_id:
                        conn.execute(
                            """
                            UPDATE analytics.forecast_run
                            SET dataset_version = %s
                            WHERE forecast_run_id = %s::uuid
                            """,
                            (ctx.dataset_version, forecast_run_id),
                        )
                    if reconciliation_run_id:
                        conn.execute(
                            """
                            UPDATE billing.reconciliation_run
                            SET dataset_version = %s
                            WHERE reconciliation_run_id = %s::uuid
                            """,
                            (ctx.dataset_version, reconciliation_run_id),
                        )
            except Exception:  # noqa: BLE001
                pass

        quality_trend: list[dict[str, Any]] = []
        if not dry:
            from cashflow_ops import quality

            quality_trend = quality.quality_trend(ctx.as_of_date, days=30)
        outputs["quality_trend_rows"] = len(quality_trend)

        if not dry:
            snap_id = state.upsert_daily_snapshot(
                as_of_date=ctx.as_of_date,
                run_id=ctx.run_id,
                summary=summary,
                volumes=volumes,
                stage_statuses_map=stage_map,
                forecast_run_id=forecast_run_id,
                reconciliation_run_id=reconciliation_run_id,
                dataset_version=ctx.dataset_version,
            )
            outputs["snapshot_id"] = snap_id
        else:
            outputs["snapshot_id"] = "dry-run"

        # Daily report artifact on disk
        OPS_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = report.write_daily_report(
            run_id=ctx.run_id,
            as_of=ctx.as_of_date,
            summary=summary,
            stage_statuses=stage_map,
            volumes=volumes,
            accuracy=accuracy,
            out_dir=OPS_ARTIFACTS_DIR,
            quality_trend=quality_trend,
            dataset_version=ctx.dataset_version,
        )
        outputs["report_path"] = str(report_path)

        notify_result = {"notified": False, "dry_run": True} if dry else alerts.notify_run(
            ctx.run_id
        )
        outputs["notify"] = notify_result

        # Drain due retry items (record only — next run / resume picks them up)
        due = [] if dry else state.due_retry_items(limit=50)
        outputs["retry_due_count"] = len(due)

        return StageResult.success(
            outputs=outputs,
            artifacts=[
                ArtifactSpec(
                    key="daily_snapshot",
                    payload={"snapshot_id": outputs.get("snapshot_id"), **summary},
                ),
                ArtifactSpec(key="forecast_accuracy", payload=accuracy),
                ArtifactSpec(key="daily_report", uri=str(report_path)),
                ArtifactSpec(key="notifications", payload=notify_result),
            ],
        )


def _compute_forecast_accuracy(
    ctx: RunContext,
    *,
    forecast_run_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    if dry_run:
        return {
            "forecast_total": None,
            "actual_total": None,
            "mape": None,
            "bias": None,
            "rmse": None,
            "accuracy": None,
            "per_insurance": [],
            "details": {"dry_run": True},
        }

    try:
        import pandas as pd
        from cashflow_db.repository import connection, payments
        from cashflow_forecast.land_accuracy import (
            build_land_accuracy_frame,
            summarize_land_accuracy,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "forecast_total": None,
            "actual_total": None,
            "mape": None,
            "bias": None,
            "rmse": None,
            "accuracy": None,
            "per_insurance": [],
            "details": {"error": str(exc)},
        }

    try:
        with connection() as conn:
            deposits = payments.get_bank_deposits(conn)
            actual_rows = []
            for d in deposits:
                period = d.get("bank_posting_date") or d.get("check_date_recognized")
                if not period:
                    continue
                actual_rows.append(
                    {"period": str(period)[:10], "amount": float(d.get("amount") or 0)}
                )
            actual_daily = pd.DataFrame(actual_rows)
            if not actual_daily.empty:
                actual_daily = (
                    actual_daily.groupby("period", as_index=False)["amount"].sum()
                )

            outcomes = pd.DataFrame()
            if forecast_run_id:
                rows = conn.execute(
                    """
                    SELECT outcome_stage, expected_amount,
                           payload->>'forecast_date' AS forecast_date,
                           payload->>'original_forecast_date' AS original_forecast_date,
                           payload->>'insurance' AS insurance
                    FROM analytics.forecast_prediction
                    WHERE forecast_run_id = %s::uuid
                    """,
                    (forecast_run_id,),
                ).fetchall()
                outcomes = pd.DataFrame([dict(r) for r in rows])

            if outcomes.empty or actual_daily.empty:
                actual_total = (
                    float(actual_daily["amount"].sum()) if not actual_daily.empty else 0.0
                )
                return {
                    "forecast_total": None,
                    "actual_total": actual_total,
                    "mape": None,
                    "bias": None,
                    "rmse": None,
                    "accuracy": None,
                    "per_insurance": [],
                    "details": {"note": "insufficient outcomes or actuals"},
                }

            # Focus accuracy on as_of / window end
            day = ctx.window_end.isoformat()
            frame = build_land_accuracy_frame(outcomes, actual_daily, dates=[day])
            if frame.empty:
                frame = build_land_accuracy_frame(outcomes, actual_daily)
            summary = summarize_land_accuracy(frame)
            forecast_total = float(frame["pred"].sum()) if not frame.empty else None
            actual_total = float(frame["actual"].sum()) if not frame.empty else None

            per_ins: list[dict[str, Any]] = []
            if "insurance" in outcomes.columns:
                for ins, grp in outcomes.groupby(outcomes["insurance"].fillna("UNKNOWN")):
                    f = build_land_accuracy_frame(grp, actual_daily)
                    s = summarize_land_accuracy(f)
                    per_ins.append(
                        {
                            "insurance": str(ins),
                            "mape": s.get("mape"),
                            "bias": s.get("bias"),
                            "n_days": s.get("n_days"),
                        }
                    )

            return {
                "forecast_total": forecast_total,
                "actual_total": actual_total,
                "mape": summary.get("mape"),
                "bias": summary.get("bias"),
                "rmse": summary.get("rmse"),
                "accuracy": summary.get("accuracy"),
                "per_insurance": per_ins,
                "details": {"n_days": summary.get("n_days"), "focus_day": day},
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "forecast_total": None,
            "actual_total": None,
            "mape": None,
            "bias": None,
            "rmse": None,
            "accuracy": None,
            "per_insurance": [],
            "details": {"error": str(exc)},
        }

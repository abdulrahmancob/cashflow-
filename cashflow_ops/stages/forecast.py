"""Stage 7 — Forecast (repository SoT)."""

from __future__ import annotations

from cashflow_ops.adapters import forecast as forecast_adapter
from cashflow_ops.config import FORECAST_CASH_DROP_ALERT_PCT
from cashflow_ops import state
from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class ForecastStage:
    key = "forecast"
    requires = ["feature_store"]
    produces = ["forecast_run", "forecast_predictions"]
    on_failure = FailurePolicy.STOP
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        dry = ctx.dry_run
        # Prefer feature_snapshot when present (best-effort preload into meta)
        feature_count = 0
        if not dry:
            try:
                from cashflow_db.repository import connection, features

                with connection() as conn:
                    snaps = features.get_features(
                        conn,
                        as_of_date=ctx.as_of_date,
                        dataset_version=ctx.dataset_version,
                    )
                    feature_count = len(snaps)
            except Exception:  # noqa: BLE001
                feature_count = 0

        result = forecast_adapter.build_from_db(
            dry_run=dry, as_of=ctx.as_of_date.isoformat()
        )
        outputs = {
            "build": result.to_dict(),
            "feature_snapshot_rows": feature_count,
            "dataset_version": ctx.dataset_version,
        }
        alerts: list[dict] = []

        if not result.ok and not dry:
            return StageResult.failed(
                f"forecast build --from-db failed: {result.stderr[-800:]}",
                outputs=outputs,
            )

        forecast_run_id = None
        forecast_total = None
        if not dry:
            try:
                from cashflow_db.repository import connection, forecast as frepo

                with connection() as conn:
                    # latest successful run
                    row = conn.execute(
                        """
                        SELECT forecast_run_id, created_at
                        FROM analytics.forecast_run
                        WHERE status = 'success'
                        ORDER BY created_at DESC
                        LIMIT 1
                        """
                    ).fetchone()
                    if row:
                        forecast_run_id = str(row["forecast_run_id"])
                    # optional total from predictions
                    if forecast_run_id:
                        tot = conn.execute(
                            """
                            SELECT COALESCE(SUM(expected_amount), 0) AS total
                            FROM analytics.forecast_prediction
                            WHERE forecast_run_id = %s::uuid
                            """,
                            (forecast_run_id,),
                        ).fetchone()
                        forecast_total = float(tot["total"]) if tot else None
            except Exception as exc:  # noqa: BLE001
                alerts.append(
                    {
                        "severity": "warning",
                        "alert_key": "forecast_run_lookup_failed",
                        "message": str(exc),
                    }
                )

        # Alert if forecast cash dropped sharply vs prior snapshot
        prior = state.get_prior_snapshot(ctx.as_of_date)
        if prior and forecast_total is not None:
            prior_fc = (prior.get("summary") or {}).get("forecast_total")
            if prior_fc and float(prior_fc) > 0:
                drop = 1.0 - (forecast_total / float(prior_fc))
                if drop >= FORECAST_CASH_DROP_ALERT_PCT:
                    alerts.append(
                        {
                            "severity": "critical",
                            "alert_key": "forecast_cash_drop",
                            "message": (
                                f"Forecast cash dropped {drop:.1%} "
                                f"({prior_fc} → {forecast_total})"
                            ),
                            "payload": {
                                "prior": prior_fc,
                                "current": forecast_total,
                            },
                        }
                    )

        outputs["forecast_run_id"] = forecast_run_id
        outputs["forecast_total"] = forecast_total

        return StageResult.success(
            outputs=outputs,
            artifacts=[
                ArtifactSpec(
                    key="forecast_run",
                    payload={
                        "forecast_run_id": forecast_run_id,
                        "forecast_total": forecast_total,
                    },
                ),
                ArtifactSpec(
                    key="forecast_predictions",
                    payload={"forecast_run_id": forecast_run_id},
                ),
            ],
            alerts=alerts,
        )

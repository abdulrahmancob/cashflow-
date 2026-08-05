"""Stage — Feature Store materialization (after Analytics, before Forecast)."""

from __future__ import annotations

from typing import Any

from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class FeatureStoreStage:
    key = "feature_store"
    requires = ["analytics"]
    produces = ["feature_snapshot"]
    on_failure = FailurePolicy.CONTINUE_WITH_ALERT
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        if ctx.dry_run:
            return StageResult.success(
                outputs={"dry_run": True, "features_written": 0},
                artifacts=[ArtifactSpec(key="feature_snapshot", payload={"dry_run": True})],
            )

        try:
            from cashflow_db.repository import connection, features, insurance
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(f"feature store imports failed: {exc}")

        rows_out: list[dict[str, Any]] = []
        alerts: list[dict[str, Any]] = []

        try:
            with connection() as conn:
                # 1) Payor avg cash velocity from payor_behavior_summary
                try:
                    payors = insurance.get_payor_behavior_summary(conn)
                    for p in payors:
                        key = str(
                            p.get("payor_key") or p.get("payor_raw") or "UNKNOWN"
                        )
                        val = p.get("median_cash_velocity_days")
                        if val is None:
                            val = p.get("cash_velocity_days")
                        if val is None:
                            continue
                        rows_out.append(
                            {
                                "feature_key": "payor.avg_cash_velocity_days",
                                "entity_key": f"payer={key}",
                                "value_num": float(val),
                                "payload": {
                                    "payor_key": key,
                                    "check_count": p.get("check_count"),
                                },
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    alerts.append(
                        {
                            "severity": "warning",
                            "alert_key": "feature_payor_velocity_failed",
                            "message": str(exc),
                        }
                    )

                # 2) Facility cancellation rate from schedule_appointment statuses
                try:
                    fac_rows = conn.execute(
                        """
                        SELECT
                            COALESCE(f.webpt_facility_id::text, sa.facility_id::text) AS facility_key,
                            COUNT(*)::float AS total,
                            COUNT(*) FILTER (
                                WHERE sa.status = 'cancelled'
                            )::float AS cancelled
                        FROM core.schedule_appointment sa
                        LEFT JOIN ref.facility f ON f.facility_id = sa.facility_id
                        GROUP BY 1
                        HAVING COUNT(*) > 0
                        """
                    ).fetchall()
                    for fr in fac_rows:
                        total = float(fr["total"] or 0)
                        cancelled = float(fr["cancelled"] or 0)
                        rate = cancelled / total if total else 0.0
                        fk = fr["facility_key"] or "unknown"
                        rows_out.append(
                            {
                                "feature_key": "facility.cancellation_rate",
                                "entity_key": f"facility={fk}",
                                "value_num": rate,
                                "payload": {
                                    "total": total,
                                    "cancelled": cancelled,
                                },
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    alerts.append(
                        {
                            "severity": "warning",
                            "alert_key": "feature_facility_cancel_failed",
                            "message": str(exc),
                        }
                    )

                # 3) Authorization risk: remaining = authorized - used
                try:
                    auth_rows = conn.execute(
                        """
                        SELECT
                            pc.webpt_case_id AS case_id,
                            a.visits_authorized,
                            a.visits_used
                        FROM core.authorization a
                        JOIN core.patient_case pc ON pc.case_pk = a.case_pk
                        WHERE a.visits_authorized IS NOT NULL
                        """
                    ).fetchall()
                    for ar in auth_rows:
                        authorized = float(ar["visits_authorized"] or 0) or 1.0
                        used = float(ar["visits_used"] or 0)
                        remaining = max(authorized - used, 0.0)
                        risk = max(0.0, min(1.0, 1.0 - (remaining / authorized)))
                        cid = ar["case_id"] or "unknown"
                        rows_out.append(
                            {
                                "feature_key": "case.authorization_risk",
                                "entity_key": f"case={cid}",
                                "value_num": risk,
                                "payload": {
                                    "remaining_visits": remaining,
                                    "visits_authorized": authorized,
                                    "visits_used": used,
                                },
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    alerts.append(
                        {
                            "severity": "warning",
                            "alert_key": "feature_auth_risk_failed",
                            "message": str(exc),
                        }
                    )

                n = features.write_snapshots(
                    conn,
                    as_of_date=ctx.as_of_date,
                    dataset_version=ctx.dataset_version,
                    rows=rows_out,
                )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(f"feature_store failed: {exc}")

        return StageResult.success(
            outputs={
                "features_written": n,
                "dataset_version": ctx.dataset_version,
                "feature_keys": sorted({r["feature_key"] for r in rows_out}),
            },
            artifacts=[
                ArtifactSpec(
                    key="feature_snapshot",
                    payload={
                        "count": n,
                        "dataset_version": ctx.dataset_version,
                    },
                )
            ],
            alerts=alerts,
        )

"""Stage 6 — Analytics facts (payor / velocity / denials / KPIs) — not Forecast."""

from __future__ import annotations

from typing import Any

from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class AnalyticsStage:
    key = "analytics"
    requires = ["reconciliation"]
    produces = [
        "payor_behavior_facts",
        "cash_velocity_facts",
        "denial_trend_facts",
        "facility_kpi_facts",
        "aging_facts",
    ]
    on_failure = FailurePolicy.CONTINUE_WITH_ALERT
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        """Materialize analytics summaries from warehouse repository views."""
        outputs: dict[str, Any] = {"as_of": ctx.as_of_date.isoformat()}
        alerts: list[dict[str, Any]] = []

        if ctx.dry_run:
            return StageResult.success(
                outputs={**outputs, "dry_run": True},
                artifacts=[
                    ArtifactSpec(key=k, payload={"dry_run": True}) for k in self.produces
                ],
            )

        try:
            from cashflow_db.repository import connection, insurance, payments
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(f"repository import failed: {exc}")

        facts: dict[str, Any] = {}
        try:
            with connection() as conn:
                # Payor behavior already written in recon stage; read back summary
                try:
                    summary = insurance.get_payor_behavior_summary(conn)
                    facts["payor_behavior"] = {
                        "rows": len(summary) if summary is not None else 0
                    }
                except Exception as exc:  # noqa: BLE001
                    facts["payor_behavior"] = {"error": str(exc)}
                    alerts.append(
                        {
                            "severity": "warning",
                            "alert_key": "payor_behavior_read_failed",
                            "message": str(exc),
                        }
                    )

                try:
                    deposits = payments.get_bank_deposits(conn)
                    n = len(deposits)
                    total = sum(float(r.get("amount") or 0) for r in deposits)
                    facts["cash_velocity"] = {
                        "deposit_rows": n,
                        "deposit_total": total,
                    }
                except Exception as exc:  # noqa: BLE001
                    facts["cash_velocity"] = {"error": str(exc)}

                # Denial / aging / facility KPIs — best-effort SQL counts
                try:
                    row = conn.execute(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM billing.denial_record) AS denials,
                          (SELECT COUNT(*) FROM billing.eob_check) AS eob_checks,
                          (SELECT COUNT(*) FROM core.visit) AS visits,
                          (SELECT COUNT(DISTINCT facility_id)
                             FROM core.schedule_appointment) AS facilities
                        """
                    ).fetchone()
                    facts["denial_trends"] = {"denial_record_count": row["denials"]}
                    facts["facility_kpis"] = {
                        "facilities": row["facilities"],
                        "visits": row["visits"],
                        "eob_checks": row["eob_checks"],
                    }
                    # Outstanding proxy: unmatched-ish visit count if mart exists
                    try:
                        aging = conn.execute(
                            """
                            SELECT COUNT(*) AS n
                            FROM information_schema.views
                            WHERE table_schema = 'mart'
                              AND table_name LIKE '%unmatched%'
                            """
                        ).fetchone()
                        facts["aging"] = {"unmatched_views": aging["n"]}
                        if aging["n"]:
                            # Prefer a known mart if present
                            try:
                                u = conn.execute(
                                    "SELECT COUNT(*) AS n FROM mart.v_unmatched_visits"
                                ).fetchone()
                                facts["aging"]["unmatched_visits"] = u["n"]
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception as exc:  # noqa: BLE001
                        facts["aging"] = {"error": str(exc)}
                except Exception as exc:  # noqa: BLE001
                    alerts.append(
                        {
                            "severity": "warning",
                            "alert_key": "analytics_counts_failed",
                            "message": str(exc),
                        }
                    )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(f"analytics stage failed: {exc}", outputs=outputs)

        outputs["facts"] = facts
        return StageResult.success(
            outputs=outputs,
            artifacts=[
                ArtifactSpec(key="payor_behavior_facts", payload=facts.get("payor_behavior", {})),
                ArtifactSpec(key="cash_velocity_facts", payload=facts.get("cash_velocity", {})),
                ArtifactSpec(key="denial_trend_facts", payload=facts.get("denial_trends", {})),
                ArtifactSpec(key="facility_kpi_facts", payload=facts.get("facility_kpis", {})),
                ArtifactSpec(key="aging_facts", payload=facts.get("aging", {})),
            ],
            alerts=alerts,
        )

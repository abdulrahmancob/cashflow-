"""Stage — generate eligibility work items after reconciliation."""

from __future__ import annotations

from typing import Any

from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class EligibilityQueueStage:
    key = "eligibility_queue"
    requires = ["reconciliation"]
    produces = ["eligibility_work_items"]
    on_failure = FailurePolicy.CONTINUE_WITH_ALERT
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        if ctx.dry_run:
            return StageResult.success(
                outputs={"dry_run": True},
                artifacts=[
                    ArtifactSpec(key="eligibility_work_items", payload={"dry_run": True})
                ],
            )
        try:
            from cashflow_db.services.eligibility_generator import (
                generate_eligibility_work_items,
            )

            result = generate_eligibility_work_items(from_db=True, dry_run=False)
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(f"eligibility_queue failed: {exc}")

        alerts: list[dict[str, Any]] = []
        if not result.get("ok"):
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "eligibility_queue_partial",
                    "message": "; ".join(result.get("errors") or ["partial failure"])[:500],
                }
            )
        return StageResult.success(
            outputs=result,
            artifacts=[ArtifactSpec(key="eligibility_work_items", payload=result)],
            alerts=alerts,
        )

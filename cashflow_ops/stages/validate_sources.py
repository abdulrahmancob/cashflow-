"""Stage 2 — Validate Sources (hard gate)."""

from __future__ import annotations

from cashflow_ops import state
from cashflow_ops.checks.source_checks import run_all_checks
from cashflow_ops.contracts import (
    ArtifactSpec,
    FailurePolicy,
    RunContext,
    StageResult,
    StageStatus,
)


class ValidateSourcesStage:
    key = "validate_sources"
    requires = ["acquire"]
    produces = ["sources_validated"]
    on_failure = FailurePolicy.STOP
    max_attempts = 1

    def run(self, ctx: RunContext) -> StageResult:
        acquire_outputs: dict = {}
        for sr in state.list_stage_runs(ctx.run_id):
            if sr["stage_key"] == "acquire":
                acquire_outputs = sr.get("outputs") or {}
                break

        result = run_all_checks(as_of=ctx.as_of_date, acquire_outputs=acquire_outputs)
        # Persist quality history
        from cashflow_ops import quality

        quality_rows = quality.persist_from_metrics_dict(
            as_of_date=ctx.as_of_date,
            run_id=ctx.run_id,
            metrics=result.metrics,
        )
        outputs = {
            "ok": result.ok,
            "metrics": result.metrics,
            "critical_failures": result.critical_failures,
            "quality": quality_rows,
        }
        artifacts = [
            ArtifactSpec(
                key="sources_validated",
                payload={"ok": result.ok, "metrics": result.metrics},
            )
        ]
        if not result.ok:
            return StageResult(
                status=StageStatus.FAILED,
                outputs=outputs,
                artifacts=artifacts,
                error_message="; ".join(result.critical_failures),
                alerts=result.alerts,
            )

        return StageResult.success(
            outputs=outputs,
            artifacts=artifacts,
            alerts=result.alerts,
        )

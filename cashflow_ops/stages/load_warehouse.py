"""Stage 4 — Load Warehouse (SoT gate)."""

from __future__ import annotations

from cashflow_ops.adapters import warehouse
from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class LoadWarehouseStage:
    key = "load_warehouse"
    requires = ["enrich_clinical"]
    produces = ["warehouse_loaded", "warehouse_validated"]
    on_failure = FailurePolicy.STOP
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        dry = ctx.dry_run
        mig = warehouse.migrate(dry_run=dry)
        if not mig.ok:
            return StageResult.failed(
                f"migrate failed: {mig.stderr[-800:]}",
                outputs={"migrate": mig.to_dict()},
            )

        load = warehouse.load_all(dry_run=dry)
        if not load.ok:
            return StageResult.failed(
                f"load-all failed: {load.stderr[-800:]}",
                outputs={"migrate": mig.to_dict(), "load_all": load.to_dict()},
            )

        val = warehouse.validate(dry_run=dry)
        report = warehouse.validate_report(val)
        outputs = {
            "migrate": mig.to_dict(),
            "load_all": load.to_dict(),
            "validate": val.to_dict(),
            "validate_report": report,
        }
        alerts: list[dict] = []
        # load-all is the hard gate; source↔DB drift is warning-only so reconcile
        # / forecast still run after a successful warehouse write.
        if not val.ok and not dry:
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "warehouse_validate_drift",
                    "message": f"warehouse validate drift (continuing): {report}",
                }
            )

        return StageResult.success(
            outputs=outputs,
            alerts=alerts,
            artifacts=[
                ArtifactSpec(key="warehouse_loaded", payload={"ok": True}),
                ArtifactSpec(
                    key="warehouse_validated",
                    payload={"ok": bool(report.get("ok", True)), "report": report},
                ),
            ],
        )

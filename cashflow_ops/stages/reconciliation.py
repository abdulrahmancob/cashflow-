"""Stage 5 — Reconciliation composite (Match → Insurance Behavior → Audit → Facts)."""

from __future__ import annotations

from typing import Any

from cashflow_ops.adapters import reconcile
from cashflow_ops.contracts import ArtifactSpec, FailurePolicy, RunContext, StageResult


class ReconciliationStage:
    key = "reconciliation"
    requires = ["load_warehouse"]
    produces = [
        "reconciliation_match",
        "insurance_behavior",
        "audit_facts",
        "reconciliation_facts",
    ]
    on_failure = FailurePolicy.STOP
    max_attempts = 2

    def run(self, ctx: RunContext) -> StageResult:
        dry = ctx.dry_run
        outputs: dict[str, Any] = {}
        alerts: list[dict[str, Any]] = []

        # 1) Match — full history rebuild, NOT the scrape window.
        # Each reconciliation run replaces the previous one and downstream
        # consumers (forecast, portal) read only the latest run, so a windowed
        # match would silently drop everything outside the lookback window.
        try:
            match = reconcile.reconcile_from_db(
                service_from=None,
                service_to=ctx.window_end,
                dry_run=dry,
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(f"reconcile --from-db failed: {exc}")
        outputs["match"] = match
        if not match.get("ok", False):
            return StageResult.failed("reconcile match not ok", outputs=outputs)

        # 2) Insurance behavior (requires RevFlow loaded + match)
        ib = reconcile.insurance_behavior(dry_run=dry)
        outputs["insurance_behavior"] = ib.to_dict()
        if not ib.ok and not ib.skipped and not dry:
            return StageResult.failed(
                f"insurance_behavior failed: {ib.stderr[-800:]}",
                outputs=outputs,
            )

        # 3) Audit facts
        audit = reconcile.audit_billing(dry_run=dry)
        outputs["audit"] = audit.to_dict()
        if not audit.ok and not audit.skipped:
            alerts.append(
                {
                    "severity": "warning",
                    "alert_key": "audit_billing_failed",
                    "message": audit.stderr[-500:] or "audit_billing failed",
                }
            )

        # 4) Facts written (summary from match)
        summary = match.get("summary") or {}
        recon_run_id = summary.get("reconciliation_run_id") or summary.get("run_id")

        return StageResult.success(
            outputs=outputs,
            artifacts=[
                ArtifactSpec(
                    key="reconciliation_match",
                    payload={"summary": summary},
                ),
                ArtifactSpec(
                    key="insurance_behavior",
                    payload=ib.to_dict(),
                ),
                ArtifactSpec(
                    key="audit_facts",
                    payload=audit.to_dict(),
                ),
                ArtifactSpec(
                    key="reconciliation_facts",
                    payload={
                        "reconciliation_run_id": recon_run_id,
                        "summary": summary,
                    },
                ),
            ],
            alerts=alerts,
        )

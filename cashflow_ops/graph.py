"""Dependency graph for the daily RCM pipeline stages."""

from __future__ import annotations

from cashflow_ops.contracts import FailurePolicy, Stage
from cashflow_ops.stages.acquire import AcquireStage
from cashflow_ops.stages.analytics import AnalyticsStage
from cashflow_ops.stages.enrich_clinical import EnrichClinicalStage
from cashflow_ops.stages.feature_store import FeatureStoreStage
from cashflow_ops.stages.forecast import ForecastStage
from cashflow_ops.stages.load_warehouse import LoadWarehouseStage
from cashflow_ops.stages.publish import PublishMonitorStage
from cashflow_ops.stages.eligibility_queue import EligibilityQueueStage
from cashflow_ops.stages.reconciliation import ReconciliationStage
from cashflow_ops.stages.validate_sources import ValidateSourcesStage

# Canonical daily order. Feature store sits before Forecast.
STAGE_ORDER: list[str] = [
    "acquire",
    "validate_sources",
    "enrich_clinical",
    "load_warehouse",
    "reconciliation",
    "eligibility_queue",
    "analytics",
    "feature_store",
    "forecast",
    "publish_monitor",
]


def build_stages() -> dict[str, Stage]:
    stages: list[Stage] = [
        AcquireStage(),
        ValidateSourcesStage(),
        EnrichClinicalStage(),
        LoadWarehouseStage(),
        ReconciliationStage(),
        EligibilityQueueStage(),
        AnalyticsStage(),
        FeatureStoreStage(),
        ForecastStage(),
        PublishMonitorStage(),
    ]
    by_key = {s.key: s for s in stages}
    missing = [k for k in STAGE_ORDER if k not in by_key]
    if missing:
        raise RuntimeError(f"Missing stage implementations: {missing}")
    return by_key


def _dep_satisfied(status: str | None) -> bool:
    """success or skipped (continue_with_alert) unblocks dependents."""
    return status in {"success", "skipped"}


def ready_stages(
    stages: dict[str, Stage],
    statuses: dict[str, str],
) -> list[str]:
    """Return stage keys whose dependencies are satisfied and not yet done."""
    ready: list[str] = []
    for key in STAGE_ORDER:
        st = statuses.get(key, "pending")
        if st in {"success", "skipped", "running"}:
            continue
        stage = stages[key]
        deps_ok = all(_dep_satisfied(statuses.get(dep)) for dep in stage.requires)
        if not deps_ok:
            continue
        if st in {"pending", "failed", "blocked"}:
            ready.append(key)
    return ready


def blocked_by_deps(
    stages: dict[str, Stage],
    statuses: dict[str, str],
) -> list[str]:
    out: list[str] = []
    for key in STAGE_ORDER:
        st = statuses.get(key, "pending")
        if st in {"success", "skipped", "running", "failed"}:
            continue
        stage = stages[key]
        hard_fail = any(statuses.get(dep) == "failed" for dep in stage.requires)
        if hard_fail:
            out.append(key)
            continue
        if stage.requires and not all(
            _dep_satisfied(statuses.get(dep)) for dep in stage.requires
        ):
            if any(
                statuses.get(dep) in {"pending", "running", "blocked"}
                for dep in stage.requires
            ):
                out.append(key)
    return out


def failure_stops_pipeline(stage: Stage) -> bool:
    return stage.on_failure == FailurePolicy.STOP

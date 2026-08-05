"""Unit tests for workflow graph and stage contracts (no DB required)."""

from __future__ import annotations

from cashflow_ops.contracts import FailurePolicy, StageStatus
from cashflow_ops.graph import STAGE_ORDER, blocked_by_deps, build_stages, ready_stages


def test_stage_order_complete():
    stages = build_stages()
    assert STAGE_ORDER == [
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
    assert set(stages) == set(STAGE_ORDER)


def test_feature_store_before_forecast():
    stages = build_stages()
    assert stages["feature_store"].requires == ["analytics"]
    assert stages["forecast"].requires == ["feature_store"]
    assert STAGE_ORDER.index("feature_store") < STAGE_ORDER.index("forecast")


def test_ready_and_blocked():
    stages = build_stages()
    statuses = {k: "pending" for k in STAGE_ORDER}
    assert ready_stages(stages, statuses) == ["acquire"]

    statuses["acquire"] = "success"
    assert ready_stages(stages, statuses) == ["validate_sources"]

    statuses["validate_sources"] = "failed"
    blocked = blocked_by_deps(stages, statuses)
    assert "enrich_clinical" in blocked


def test_failure_policies():
    stages = build_stages()
    assert stages["validate_sources"].on_failure == FailurePolicy.STOP
    assert stages["enrich_clinical"].on_failure == FailurePolicy.CONTINUE_WITH_ALERT
    assert stages["feature_store"].on_failure == FailurePolicy.CONTINUE_WITH_ALERT
    assert stages["forecast"].on_failure == FailurePolicy.STOP


def test_stage_status_enum():
    assert StageStatus.SUCCESS.value == "success"

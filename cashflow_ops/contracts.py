"""Stage protocol and result types for the workflow engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class FailurePolicy(str, Enum):
    STOP = "stop"
    RETRY = "retry"
    CONTINUE_WITH_ALERT = "continue_with_alert"


class StageStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"


@dataclass
class ArtifactSpec:
    key: str
    uri: str | None = None
    row_count: int | None = None
    checksum: str | None = None
    etl_run_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class StageResult:
    status: StageStatus
    outputs: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactSpec] = field(default_factory=list)
    error_message: str | None = None
    alerts: list[dict[str, Any]] = field(default_factory=list)
    retry_items: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def success(
        cls,
        outputs: dict[str, Any] | None = None,
        artifacts: list[ArtifactSpec] | None = None,
        warnings: list[str] | None = None,
        alerts: list[dict[str, Any]] | None = None,
        retry_items: list[dict[str, Any]] | None = None,
    ) -> StageResult:
        return cls(
            status=StageStatus.SUCCESS,
            outputs=outputs or {},
            artifacts=artifacts or [],
            warnings=warnings or [],
            alerts=alerts or [],
            retry_items=retry_items or [],
        )

    @classmethod
    def failed(cls, message: str, outputs: dict[str, Any] | None = None) -> StageResult:
        return cls(
            status=StageStatus.FAILED,
            outputs=outputs or {},
            error_message=message,
        )


@dataclass
class RunContext:
    run_id: str
    as_of_date: date
    lookback_days: int
    trigger_source: str
    dry_run: bool = False
    skip_scrapers: bool = False
    dataset_version: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def window_start(self) -> date:
        from cashflow_ops.config import window_dates

        start, _ = window_dates(self.as_of_date, self.lookback_days)
        return start

    @property
    def window_end(self) -> date:
        from cashflow_ops.config import window_dates

        _, end = window_dates(self.as_of_date, self.lookback_days)
        return end


@runtime_checkable
class Stage(Protocol):
    key: str
    requires: list[str]
    produces: list[str]
    on_failure: FailurePolicy
    max_attempts: int

    def run(self, ctx: RunContext) -> StageResult: ...

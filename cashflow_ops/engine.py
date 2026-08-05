"""DAG executor with resume, metrics, events, and SLA enforcement."""

from __future__ import annotations

import logging
import traceback
from datetime import date
from typing import Any

from cashflow_ops import events, metrics, state
from cashflow_ops.config import DEFAULT_LOOKBACK_DAYS, DRY_RUN, SKIP_SCRAPERS
from cashflow_ops.contracts import FailurePolicy, RunContext, StageStatus
from cashflow_ops.graph import (
    STAGE_ORDER,
    blocked_by_deps,
    build_stages,
    ready_stages,
)

log = logging.getLogger(__name__)


def _stage_meta(stages: dict) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for key, stage in stages.items():
        out[key] = {
            "max_attempts": getattr(stage, "max_attempts", 1),
            "on_failure": (
                stage.on_failure.value
                if isinstance(stage.on_failure, FailurePolicy)
                else str(stage.on_failure)
            ),
            "inputs": {
                "requires": list(stage.requires),
                "produces": list(stage.produces),
            },
        }
    return out


def start_run(
    *,
    as_of_date: date,
    trigger_source: str = "manual",
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    dry_run: bool | None = None,
    skip_scrapers: bool | None = None,
    notes: str | None = None,
    meta_extra: dict[str, Any] | None = None,
) -> str:
    stages = build_stages()
    run_id = state.create_pipeline_run(
        as_of_date=as_of_date,
        trigger_source=trigger_source,
        lookback_days=lookback_days,
        stage_keys=STAGE_ORDER,
        stage_meta=_stage_meta(stages),
        notes=notes,
        meta_extra=meta_extra,
    )
    row = state.get_pipeline_run(run_id)
    ds = (row or {}).get("dataset_version")
    events.emit_event(
        run_id,
        event_key="pipeline_started",
        message=f"Pipeline started as_of={as_of_date} dataset={ds}",
        severity="info",
    )
    ctx = RunContext(
        run_id=run_id,
        as_of_date=as_of_date,
        lookback_days=lookback_days,
        trigger_source=trigger_source,
        dry_run=DRY_RUN if dry_run is None else dry_run,
        skip_scrapers=SKIP_SCRAPERS if skip_scrapers is None else skip_scrapers,
        dataset_version=ds,
        meta=(row or {}).get("meta") or {},
    )
    return execute_run(ctx)


def resume_run(
    run_id: str,
    *,
    dry_run: bool | None = None,
    skip_scrapers: bool | None = None,
) -> str:
    row = state.get_pipeline_run(run_id)
    if not row:
        raise ValueError(f"Unknown run_id: {run_id}")
    state.reclaim_stale_running(run_id)
    for sr in state.list_stage_runs(run_id):
        if sr["status"] == "failed" and sr["attempt"] < sr["max_attempts"]:
            state.reset_stage_pending(run_id, sr["stage_key"])
        elif sr["status"] == "blocked":
            state.reset_stage_pending(run_id, sr["stage_key"])
        elif sr["status"] == "failed" and sr["on_failure"] != "stop":
            state.reset_stage_pending(run_id, sr["stage_key"])
    state.finish_pipeline_run(run_id, "running", notes="resumed")
    events.emit_event(run_id, event_key="pipeline_resumed", message="Resume requested")
    ctx = RunContext(
        run_id=run_id,
        as_of_date=row["as_of_date"],
        lookback_days=int(row["lookback_days"]),
        trigger_source=str(row["trigger_source"]),
        dry_run=DRY_RUN if dry_run is None else dry_run,
        skip_scrapers=SKIP_SCRAPERS if skip_scrapers is None else skip_scrapers,
        dataset_version=row.get("dataset_version"),
        meta=row.get("meta") or {},
    )
    return execute_run(ctx)


def execute_run(ctx: RunContext) -> str:
    stages = build_stages()

    while True:
        statuses = state.stage_statuses(ctx.run_id)
        if all(statuses.get(k) in {"success", "skipped"} for k in STAGE_ORDER):
            final = (
                "partial"
                if any(statuses.get(k) == "skipped" for k in STAGE_ORDER)
                else "success"
            )
            state.finish_pipeline_run(ctx.run_id, final)
            events.emit_event(
                ctx.run_id,
                event_key="pipeline_finished",
                message=f"Pipeline {final}",
                severity="info" if final == "success" else "warning",
            )
            log.info("pipeline run %s %s", ctx.run_id, final.upper())
            return ctx.run_id

        ready = ready_stages(stages, statuses)
        if not ready:
            blocked = blocked_by_deps(stages, statuses)
            if blocked:
                state.mark_stages_blocked(
                    ctx.run_id, blocked, "blocked by upstream dependency"
                )
            statuses = state.stage_statuses(ctx.run_id)
            if any(statuses.get(k) == "failed" for k in STAGE_ORDER):
                state.finish_pipeline_run(ctx.run_id, "failed")
                events.emit_event(
                    ctx.run_id,
                    event_key="pipeline_failed",
                    message="Pipeline failed",
                    severity="critical",
                )
                log.error("pipeline run %s FAILED", ctx.run_id)
            elif all(
                statuses.get(k) in {"success", "skipped", "blocked"} for k in STAGE_ORDER
            ):
                has_fail = any(statuses.get(k) == "failed" for k in STAGE_ORDER)
                final = "failed" if has_fail else "partial"
                state.finish_pipeline_run(ctx.run_id, final)
            else:
                state.finish_pipeline_run(ctx.run_id, "failed", notes="deadlock")
            return ctx.run_id

        key = ready[0]
        stage = stages[key]
        log.info("stage START %s (run=%s)", key, ctx.run_id)
        state.mark_stage_running(ctx.run_id, key)
        events.emit_event(
            ctx.run_id,
            event_key=f"{key}_started",
            stage_key=key,
            message=f"{key} started",
            entity_key=f"stage={key}",
        )
        started_at = metrics.start_job_runtime(ctx.run_id, key)
        sla_sec = metrics.get_sla_seconds(key)

        try:
            result = stage.run(ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("stage CRASH %s", key)
            err = f"{exc}\n{traceback.format_exc()}"
            duration = metrics.finish_job_runtime(
                ctx.run_id,
                key,
                started_at=started_at,
                sla_sec=sla_sec,
                sla_breached=False,
            )
            crash_status = (
                StageStatus.SKIPPED
                if stage.on_failure == FailurePolicy.CONTINUE_WITH_ALERT
                else StageStatus.FAILED
            )
            state.mark_stage_finished(
                ctx.run_id, key, crash_status, error_message=err
            )
            events.emit_event(
                ctx.run_id,
                event_key=f"{key}_failed",
                stage_key=key,
                message=err[:500],
                severity="error",
                entity_key=f"stage={key}",
            )
            _handle_failure(ctx, stage, err)
            if stage.on_failure == FailurePolicy.STOP:
                _stop_run(ctx.run_id, key)
                return ctx.run_id
            continue

        for alert in result.alerts:
            state.record_alert(
                ctx.run_id,
                stage_key=key,
                severity=str(alert.get("severity", "warning")),
                alert_key=str(alert.get("alert_key", f"{key}_alert")),
                message=str(alert.get("message", "")),
                payload=alert.get("payload"),
            )
        if result.retry_items:
            state.enqueue_retry_items(ctx.run_id, key, result.retry_items)
            for item in result.retry_items:
                events.emit_event(
                    ctx.run_id,
                    event_key="retry_queued",
                    stage_key=key,
                    message=item.get("last_error") or "retry queued",
                    severity="warning",
                    entity_key=item.get("item_key"),
                    payload=item,
                )
        if result.artifacts:
            state.record_artifacts(ctx.run_id, key, result.artifacts)

        metrics.emit_stage_output_metrics(ctx.run_id, key, result.outputs or {})

        duration = metrics.finish_job_runtime(
            ctx.run_id,
            key,
            started_at=started_at,
            sla_sec=sla_sec,
            sla_breached=False,
        )

        sla_failed = False
        if sla_sec is not None and duration > sla_sec:
            sla_failed = True
            metrics.finish_job_runtime(
                ctx.run_id,
                key,
                started_at=started_at,
                sla_sec=sla_sec,
                sla_breached=True,
            )
            msg = (
                f"SLA breach: {key} took {duration:.1f}s > {sla_sec}s"
            )
            events.emit_event(
                ctx.run_id,
                event_key="sla_breached",
                stage_key=key,
                message=msg,
                severity="critical",
                entity_key=f"stage={key}",
                payload={"duration_sec": duration, "sla_sec": sla_sec},
            )
            state.record_alert(
                ctx.run_id,
                stage_key=key,
                severity="critical",
                alert_key="sla_breach",
                message=msg,
                payload={"duration_sec": duration, "sla_sec": sla_sec},
            )

        final_status = result.status
        error_message = result.error_message
        if sla_failed and result.status == StageStatus.SUCCESS:
            final_status = StageStatus.FAILED
            error_message = (
                f"SLA breach: {duration:.1f}s > {sla_sec}s"
            )
        if (
            final_status == StageStatus.FAILED
            and stage.on_failure == FailurePolicy.CONTINUE_WITH_ALERT
        ):
            final_status = StageStatus.SKIPPED

        state.mark_stage_finished(
            ctx.run_id,
            key,
            final_status,
            outputs=result.outputs,
            error_message=error_message,
        )
        events.emit_event(
            ctx.run_id,
            event_key=f"{key}_finished",
            stage_key=key,
            message=f"{key} -> {final_status.value} ({duration:.1f}s)",
            severity="info" if final_status != StageStatus.FAILED else "error",
            entity_key=f"stage={key}",
            payload={"status": final_status.value, "duration_sec": duration},
        )
        log.info("stage END %s status=%s duration=%.1fs", key, final_status.value, duration)

        failed_hard = (
            result.status == StageStatus.FAILED or sla_failed
        ) and stage.on_failure == FailurePolicy.STOP
        if result.status == StageStatus.FAILED or sla_failed:
            _handle_failure(
                ctx, stage, error_message or result.error_message or "stage failed"
            )
        if failed_hard:
            _stop_run(ctx.run_id, key)
            return ctx.run_id


def _stop_run(run_id: str, failed_stage: str) -> None:
    statuses = state.stage_statuses(run_id)
    blocked = [k for k in STAGE_ORDER if statuses.get(k) in {"pending", "blocked"}]
    state.mark_stages_blocked(run_id, blocked, f"stopped after {failed_stage} failure")
    state.finish_pipeline_run(run_id, "failed")
    events.emit_event(
        run_id,
        event_key="pipeline_failed",
        stage_key=failed_stage,
        message=f"Stopped after {failed_stage}",
        severity="critical",
    )


def _handle_failure(ctx: RunContext, stage: Any, message: str) -> None:
    policy = stage.on_failure
    if isinstance(policy, FailurePolicy):
        policy_val = policy
    else:
        policy_val = FailurePolicy(str(policy))
    if policy_val == FailurePolicy.CONTINUE_WITH_ALERT:
        state.record_alert(
            ctx.run_id,
            stage_key=stage.key,
            severity="critical",
            alert_key=f"{stage.key}_failed_continue",
            message=message,
        )
    elif policy_val == FailurePolicy.RETRY:
        state.enqueue_retry_items(
            ctx.run_id,
            stage.key,
            [
                {
                    "item_type": "stage",
                    "item_key": stage.key,
                    "last_error": message,
                    "delay_hours": 1,
                    "max_attempts": getattr(stage, "max_attempts", 3),
                    "payload": {"run_id": ctx.run_id},
                }
            ],
        )
    else:
        state.record_alert(
            ctx.run_id,
            stage_key=stage.key,
            severity="critical",
            alert_key=f"{stage.key}_failed_stop",
            message=message,
        )


def run_status(run_id: str) -> dict[str, Any]:
    row = state.get_pipeline_run(run_id)
    if not row:
        return {"error": "not_found", "run_id": run_id}
    stages = state.list_stage_runs(run_id)
    alerts = state.list_alerts(run_id)
    artifacts = state.get_artifacts(run_id)
    return {
        "run": {
            "run_id": str(row["run_id"]),
            "as_of_date": str(row["as_of_date"]),
            "status": row["status"],
            "trigger_source": row["trigger_source"],
            "lookback_days": row["lookback_days"],
            "dataset_version": row.get("dataset_version"),
            "started_at": row["started_at"].isoformat() if row["started_at"] else None,
            "finished_at": row["finished_at"].isoformat() if row["finished_at"] else None,
            "notes": row["notes"],
        },
        "stages": [
            {
                "stage_key": s["stage_key"],
                "status": s["status"],
                "attempt": s["attempt"],
                "on_failure": s["on_failure"],
                "error_message": s["error_message"],
                "started_at": s["started_at"].isoformat() if s["started_at"] else None,
                "finished_at": s["finished_at"].isoformat() if s["finished_at"] else None,
            }
            for s in stages
        ],
        "alerts": [
            {
                "severity": a["severity"],
                "alert_key": a["alert_key"],
                "message": a["message"],
                "stage_key": a["stage_key"],
            }
            for a in alerts
        ],
        "artifact_keys": list(artifacts.keys()),
    }

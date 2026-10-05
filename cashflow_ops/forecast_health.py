"""Nightly forecast presence check — no forecast math."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from cashflow_ops.config import cairo_today

log = logging.getLogger(__name__)

# 08:00 Africa/Cairo is 05:00 UTC while Egypt is on UTC+3.
_CAIRO_MORNING_UTC_HOUR = 5


def _published_after_cairo_morning(created_at: str) -> bool:
    text = created_at.strip()
    if len(text) < 13:
        return False
    try:
        hour = int(text[11:13])
    except ValueError:
        return False
    return hour >= _CAIRO_MORNING_UTC_HOUR


def latest_success_forecast(as_of: date) -> dict[str, Any] | None:
    """Return the latest successful analytics.forecast_run for ``as_of``, or None."""
    from cashflow_db.repository import connection

    with connection() as conn:
        row = conn.execute(
            """
            SELECT forecast_run_id, as_of_date, status, created_at
            FROM analytics.forecast_run
            WHERE as_of_date = %s
              AND status = 'success'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (as_of,),
        ).fetchone()
    if not row:
        return None
    return {
        "forecast_run_id": str(row["forecast_run_id"]),
        "as_of_date": str(row["as_of_date"]),
        "status": row["status"],
        "created_at": str(row["created_at"]),
    }


def _stop_after(latest: dict[str, Any] | None) -> str | None:
    """Scraper passes store stop_after in pipeline meta. Worker runs do not."""
    if not latest:
        return None
    meta = latest.get("meta") or {}
    if isinstance(meta, str):
        import json

        try:
            meta = json.loads(meta)
        except ValueError:
            return None
    if not isinstance(meta, dict):
        return None
    value = meta.get("stop_after")
    text = str(value or "").strip()
    return text or None


def ensure_nightly_forecast(
    as_of: date | None = None,
    *,
    resume: bool = True,
    max_age_hours: int = 26,
) -> dict[str, Any]:
    """Alert (and optionally resume) when today's success forecast is missing.

    Does not change forecast packing. Returns a JSON-serializable status dict.
    """
    from cashflow_ops import alerts, state

    day = as_of or cairo_today()
    found = latest_success_forecast(day)
    if found:
        created = found.get("created_at") or ""
        late = _published_after_cairo_morning(str(created))
        if late:
            alerts.emit(
                None,
                stage_key="forecast",
                severity="critical",
                alert_key="forecast_published_late",
                message=(
                    f"Forecast for {day.isoformat()} published at {created}, "
                    "after 08:00 Africa/Cairo"
                ),
                payload={"as_of": day.isoformat(), "created_at": created},
            )
        return {
            "ok": True,
            "as_of": day.isoformat(),
            "forecast": found,
            "created_at": created,
            "published_late": late,
            "max_age_hours": max_age_hours,
        }

    latest = state.latest_pipeline_run(day)
    run_id = str(latest["run_id"]) if latest else None
    status = str((latest or {}).get("status") or "")
    stop_after = _stop_after(latest)
    resumed = False
    started = False
    # The check container is the worker image: it cannot launch browsers, and a
    # scraper run is marked stop_after=acquire so resuming it never reaches forecast.
    if resume:
        try:
            if run_id and not stop_after and status in {"failed", "running", "partial"}:
                from cashflow_ops.engine import resume_run

                log.warning(
                    "missing nightly forecast for %s — resume %s skip_scrapers",
                    day,
                    run_id,
                )
                resume_run(run_id, skip_scrapers=True)
                resumed = True
            elif stop_after or not run_id:
                if status == "running":
                    log.warning(
                        "missing nightly forecast for %s — scraper run still going, not starting another",
                        day,
                    )
                else:
                    from cashflow_ops.engine import start_run

                    log.warning(
                        "missing nightly forecast for %s — start skip-scrapers run",
                        day,
                    )
                    run_id = start_run(
                        as_of_date=day,
                        trigger_source="task_scheduler",
                        skip_scrapers=True,
                        notes="check-forecast skip-scrapers",
                    )
                    started = True
        except Exception as exc:  # noqa: BLE001
            log.warning("recover after missing forecast failed: %s", exc)
        found = latest_success_forecast(day)
        if found:
            return {
                "ok": True,
                "as_of": day.isoformat(),
                "forecast": found,
                "resumed": resumed,
                "started": started,
                "run_id": run_id,
            }

    message = f"No successful analytics.forecast_run for as_of={day.isoformat()}"
    alerts.emit(
        run_id,
        stage_key="forecast",
        severity="critical",
        alert_key="missing_nightly_forecast",
        message=message,
        payload={
            "as_of": day.isoformat(),
            "pipeline_run_id": run_id,
            "resumed": resumed,
            "started": started,
        },
    )
    if run_id:
        alerts.notify_run(run_id)
    return {
        "ok": False,
        "as_of": day.isoformat(),
        "forecast": None,
        "run_id": run_id,
        "resumed": resumed,
        "started": started,
        "error": message,
    }

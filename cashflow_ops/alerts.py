"""Alert helpers and optional webhook notification."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from cashflow_ops import state
from cashflow_ops.config import NOTIFY_WEBHOOK_URL

log = logging.getLogger(__name__)


def emit(
    run_id: str | None,
    *,
    stage_key: str | None,
    severity: str,
    alert_key: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> str:
    alert_id = state.record_alert(
        run_id,
        stage_key=stage_key,
        severity=severity,
        alert_key=alert_key,
        message=message,
        payload=payload,
    )
    log.log(
        logging.ERROR if severity == "critical" else logging.WARNING,
        "[%s] %s: %s",
        severity,
        alert_key,
        message,
    )
    return alert_id


def notify_run(run_id: str) -> dict[str, Any]:
    """Push a summary webhook if CASHFLOW_OPS_NOTIFY_WEBHOOK is set."""
    status = None
    try:
        from cashflow_ops.engine import run_status

        status = run_status(run_id)
    except Exception as exc:  # noqa: BLE001
        return {"notified": False, "error": str(exc)}

    if not NOTIFY_WEBHOOK_URL:
        return {"notified": False, "reason": "no_webhook", "summary": status["run"]}

    body = json.dumps(
        {
            "text": (
                f"RCM pipeline {status['run']['status']} "
                f"as_of={status['run']['as_of_date']} run={run_id}"
            ),
            "run": status["run"],
            "stages": status["stages"],
            "alerts": status["alerts"],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        NOTIFY_WEBHOOK_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"notified": True, "http_status": resp.status}
    except urllib.error.URLError as exc:
        log.warning("webhook notify failed: %s", exc)
        return {"notified": False, "error": str(exc)}

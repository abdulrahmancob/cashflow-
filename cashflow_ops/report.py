"""Daily report generation for Publish stage."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


def write_daily_report(
    *,
    run_id: str,
    as_of: date,
    summary: dict[str, Any],
    stage_statuses: dict[str, str],
    volumes: dict[str, Any],
    accuracy: dict[str, Any],
    out_dir: Path,
    quality_trend: list[dict[str, Any]] | None = None,
    dataset_version: str | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"daily_report_{as_of.isoformat()}_{run_id[:8]}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"

    payload = {
        "run_id": run_id,
        "as_of_date": as_of.isoformat(),
        "dataset_version": dataset_version,
        "summary": summary,
        "stage_statuses": stage_statuses,
        "volumes": volumes,
        "forecast_accuracy": accuracy,
        "quality_trend_30d": quality_trend or [],
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        f"# RCM Daily Report — {as_of.isoformat()}",
        "",
        f"- **Run ID:** `{run_id}`",
        f"- **Dataset version:** `{dataset_version}`",
        f"- **Forecast total:** {summary.get('forecast_total')}",
        f"- **Actual total:** {summary.get('actual_total')}",
        f"- **MAPE:** {accuracy.get('mape')}",
        f"- **Bias:** {accuracy.get('bias')}",
        "",
        "## Stages",
        "",
    ]
    for key, st in stage_statuses.items():
        lines.append(f"- `{key}`: **{st}**")
    lines.extend(["", "## Volumes", ""])
    for k, v in volumes.items():
        lines.append(f"- `{k}`: {v}")

    lines.extend(["", "## Quality trend (30d)", ""])
    if quality_trend:
        # Compact: latest status per metric
        latest: dict[str, dict[str, Any]] = {}
        for row in quality_trend:
            latest[str(row.get("metric_key"))] = row
        for mk, row in sorted(latest.items()):
            lines.append(
                f"- `{mk}`: value={row.get('value_num')} "
                f"status=**{row.get('status')}** "
                f"(expected {row.get('expected_value')}, threshold {row.get('threshold')})"
            )
    else:
        lines.append("- _(no quality history)_")
    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path

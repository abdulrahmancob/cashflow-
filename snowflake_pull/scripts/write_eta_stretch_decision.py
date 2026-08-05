"""Write ETA / stretch-goal decision from measured case_phases + health.

Stretch (~500 cph / ~2 days) is only endorsed when measured wall supports it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snowflake_pull.case_forensics import summarize  # noqa: E402

STRETCH_CPH = 500.0
MIN_CASES_DECISION = 200


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_decision(reports_dir: Path, *, cases_remaining: int | None = None) -> Path:
    reports_dir = Path(reports_dir)
    phases = _read_jsonl(reports_dir / "case_phases.jsonl")
    ok = [r for r in phases if r.get("ok")]
    # Prefer cases that already have S1 sub-breakdown (post light-nav deploy)
    with_s1 = [r for r in ok if "s1_navigation" in (r.get("phases") or {})]
    sample = with_s1 if len(with_s1) >= 20 else ok
    walls = [float(r.get("wall_sec") or 0) for r in sample if float(r.get("wall_sec") or 0) > 0]
    open_s1 = [
        float((r.get("phases") or {}).get("open_s1") or 0)
        for r in sample
        if (r.get("phases") or {}).get("open_s1") is not None
    ]
    disc = [
        float((r.get("phases") or {}).get("discovery") or 0)
        for r in sample
        if (r.get("phases") or {}).get("discovery") is not None
    ]
    sw = summarize(walls)
    so = summarize(open_s1)
    sd = summarize(disc)
    wall_avg = sw["avg"] or 0.0
    cph_theo = (3600.0 / wall_avg) if wall_avg > 0 else 0.0

    health_path = reports_dir / "health.json"
    health: dict = {}
    if health_path.is_file():
        try:
            health = json.loads(health_path.read_text(encoding="utf-8"))
        except Exception:
            health = {}
    rem = cases_remaining
    if rem is None:
        rem = int(health.get("cases_remaining") or 0)
    cph_live = float(health.get("speed_cases_per_hour") or 0)
    eta_h_theo = (rem / cph_theo) if cph_theo > 0 else None
    eta_h_live = (rem / cph_live) if cph_live > 0 else None

    stretch_ok = cph_theo >= STRETCH_CPH and len(sample) >= MIN_CASES_DECISION
    sample_note = (
        f"sample={len(sample)} (s1_breakdown={len(with_s1)}, ok_total={len(ok)})"
    )
    if len(sample) < MIN_CASES_DECISION:
        verdict = (
            f"**قرار مبدئي** (عينة < {MIN_CASES_DECISION}): "
            f"stretch يومين غير مثبت. ETA من القياس الحالي."
        )
    elif stretch_ok:
        verdict = (
            f"**Stretch ممكن بالقياس:** cph_theo={cph_theo:.0f} ≥ {STRETCH_CPH:.0f}."
        )
    else:
        verdict = (
            f"**Stretch يومين مرفوض بالقياس:** cph_theo={cph_theo:.0f} "
            f"< {STRETCH_CPH:.0f}. Outcome = ETA من القياس."
        )

    lines = [
        "# ETA / Stretch Decision",
        "",
        f"Updated: {_utc()}",
        f"Source: `{sample_note}`",
        "",
        "## Verdict",
        "",
        verdict,
        "",
        "## Measured",
        "",
        f"- wall_avg: **{wall_avg:.2f}s** → cph_theo **{cph_theo:.1f}**",
        f"- open_s1_avg: {so['avg']:.2f}s",
        f"- discovery_avg: {sd['avg']:.2f}s",
        f"- cph_live (health): {cph_live:.1f}",
        f"- cases_remaining: {rem}",
        f"- ETA_theo_hours: {eta_h_theo:.1f}" if eta_h_theo is not None else "- ETA_theo_hours: n/a",
        f"- ETA_live_hours: {eta_h_live:.1f}" if eta_h_live is not None else "- ETA_live_hours: n/a",
        f"- ETA_theo_days: {eta_h_theo / 24:.1f}" if eta_h_theo is not None else "",
        "",
        "## Gate",
        "",
        f"- Stretch threshold: {STRETCH_CPH:.0f} cph",
        f"- Decision sample size: {MIN_CASES_DECISION} (have {len(sample)})",
        f"- Adaptive batch: OFF (not part of stretch path)",
        "",
        "## What blocks stretch",
        "",
    ]
    if so["avg"] > 5:
        lines.append(
            f"- Open+S1 ≈ {so['avg']:.1f}s/case — primary wall driver; "
            "even zero-PDF ≈ "
            f"{3600 / max(so['avg'] + sd['avg'], 0.001):.0f} cph theoretical."
        )
    else:
        lines.append("- Open+S1 no longer dominates; re-check PDF / other phases.")
    lines.append("")
    out = reports_dir / "eta_stretch_decision.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    # Machine-readable sidecar
    (reports_dir / "eta_stretch_decision.json").write_text(
        json.dumps(
            {
                "at": _utc(),
                "sample_n": len(sample),
                "s1_breakdown_n": len(with_s1),
                "wall_avg": wall_avg,
                "cph_theo": round(cph_theo, 2),
                "open_s1_avg": so["avg"],
                "discovery_avg": sd["avg"],
                "cph_live": cph_live,
                "cases_remaining": rem,
                "eta_theo_hours": round(eta_h_theo, 2) if eta_h_theo else None,
                "eta_live_hours": round(eta_h_live, 2) if eta_h_live else None,
                "stretch_ok": stretch_ok,
                "stretch_threshold_cph": STRETCH_CPH,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case",
    )
    args = ap.parse_args()
    path = write_decision(Path(args.out_dir) / "reports")
    print(json.dumps({"wrote": str(path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

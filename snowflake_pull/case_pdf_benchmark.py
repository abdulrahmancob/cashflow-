"""PDF semaphore size benchmark decisions (same-case parallel only)."""

from __future__ import annotations

from typing import Any

# Probe range from plan P3
PROBE_SIZES = (3, 4, 5, 6, 8)


def relative_throughput(concurrency: int, *, serial_frac: float = 0.35) -> float:
    """Amdahl-ish relative cph vs concurrency=1 (discovery serial + PDF parallel)."""
    n = max(1, int(concurrency))
    parallel = max(0.0, 1.0 - serial_frac)
    return 1.0 / (serial_frac + parallel / n)


def decide_semaphore(
    *,
    size: int,
    before_cph: float,
    after_cph: float,
    integrity_flat: bool,
    min_improve_pct: float = 3.0,
) -> dict[str, Any]:
    """Keep only if measured cph improves and integrity stays flat."""
    delta = float(after_cph) - float(before_cph)
    pct = (delta / before_cph * 100.0) if before_cph > 0 else 0.0
    if not integrity_flat:
        decision = "rollback"
        reason = "integrity_not_flat"
    elif pct >= min_improve_pct:
        decision = "keep"
        reason = "cph_improved"
    else:
        decision = "rollback"
        reason = "insufficient_gain"
    return {
        "change": f"pdf_semaphore={size}",
        "before_cph": round(before_cph, 2),
        "after_cph": round(after_cph, 2),
        "delta": round(delta, 2),
        "delta_pct": round(pct, 1),
        "decision": decision,
        "reason": reason,
    }


def offline_probe_rows(
    *,
    baseline_cph: float,
    integrity_flat: bool = True,
) -> list[dict[str, Any]]:
    """Document modeled before/after for each probe size; pick best keep."""
    base_rel = relative_throughput(1)
    rows: list[dict[str, Any]] = []
    best_size = 1
    best_cph = baseline_cph
    for size in PROBE_SIZES:
        after = baseline_cph * (relative_throughput(size) / base_rel)
        # Compare stepwise vs previous kept best
        row = decide_semaphore(
            size=size,
            before_cph=best_cph,
            after_cph=after,
            integrity_flat=integrity_flat,
        )
        rows.append(row)
        if row["decision"] == "keep":
            best_size = size
            best_cph = after
    rows.append(
        {
            "change": "pdf_semaphore_selected",
            "before_cph": round(baseline_cph, 2),
            "after_cph": round(best_cph, 2),
            "delta": round(best_cph - baseline_cph, 2),
            "decision": "keep",
            "reason": f"best_size={best_size}",
            "selected_size": best_size,
        }
    )
    return rows


def select_best_size(rows: list[dict[str, Any]]) -> int:
    for row in reversed(rows):
        if row.get("change") == "pdf_semaphore_selected":
            return int(row.get("selected_size") or 4)
        if row.get("decision") == "keep" and str(row.get("change", "")).startswith(
            "pdf_semaphore="
        ):
            try:
                return int(str(row["change"]).split("=", 1)[1])
            except ValueError:
                pass
    return 4

"""Suggest new payer_registry aliases from unmapped names + match evidence.

Does not modify payer_registry.yaml — review suggested_aliases.csv manually.

Examples:
  python -m cashflow_reconcile.scripts.suggest_payer_aliases \\
    --daily-notes webpt_edco_scraper/output/jun_jul_2026/extracted/daily_notes.csv \\
    --payer-sla webpt_edco_scraper/output/jun_jul_2026/forecast/payer_sla.csv \\
    --unmapped webpt_edco_scraper/output/jun_jul_2026/audit/unmapped_insurance.csv \\
    --revflow-dir revflow_scraper/output/jan_jul_2026/exports \\
    --out suggested_aliases.csv
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cashflow_reconcile.payer_registry import (  # noqa: E402
    extract_ach_payer_head,
    get_registry,
    normalize_raw,
    resolve,
)


def _read_col_counts(path: Path | None, column: str) -> Counter[str]:
    counts: Counter[str] = Counter()
    if path is None or not path.exists():
        return counts
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = (row.get(column) or "").strip()
            if value:
                counts[value] += 1
    return counts


def _revflow_payors_from_dir(path: Path | None) -> Counter[str]:
    counts: Counter[str] = Counter()
    if path is None or not path.exists():
        return counts
    for file in path.rglob("*.csv"):
        stem = file.stem
        # Typical: "PAYOR - CHECK - ..."
        payor = stem.split(" - ")[0].strip() if " - " in stem else stem.strip()
        if payor:
            counts[payor] += 1
    return counts


def _load_sla_pairs(path: Path | None) -> list[tuple[str, str, int]]:
    pairs: list[tuple[str, str, int]] = []
    if path is None or not path.exists():
        return pairs
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            webpt = (row.get("webpt_insurance") or "").strip()
            rev = (row.get("revflow_payor") or "").strip()
            try:
                n = int(float(row.get("sample_count") or 0))
            except ValueError:
                n = 0
            if webpt and rev:
                pairs.append((webpt, rev, n))
    return pairs


def _token_overlap(a: str, b: str) -> float:
    stop = {"the", "and", "of", "inc", "llc", "plan", "health", "care", "new", "york"}
    ta = {t for t in re.split(r"[^a-z0-9]+", normalize_raw(a).lower()) if len(t) > 2 and t not in stop}
    tb = {t for t in re.split(r"[^a-z0-9]+", normalize_raw(b).lower()) if len(t) > 2 and t not in stop}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def suggest_rows(
    *,
    daily_notes: Path | None,
    unmapped: Path | None,
    payer_sla: Path | None,
    revflow_dir: Path | None,
    tracker_descriptions: Path | None,
) -> list[dict[str, str | int | float]]:
    registry = get_registry()
    known_revflow = {
        payor.upper()
        for org in registry.orgs
        for payor in org.revflow_payors
    }
    for org in registry.orgs:
        for alias in org.aliases:
            if alias.source == "revflow" and alias.exact:
                known_revflow.add(alias.exact.upper())

    webpt_counts = _read_col_counts(daily_notes, "insurance_name")
    webpt_counts.update(_read_col_counts(unmapped, "insurance_name"))
    revflow_counts = _revflow_payors_from_dir(revflow_dir)
    sla_pairs = _load_sla_pairs(payer_sla)

    # Evidence: WebPT → RevFlow from SLA (strongest).
    evidence: dict[str, Counter[str]] = defaultdict(Counter)
    for webpt, rev, n in sla_pairs:
        evidence[normalize_raw(webpt).lower()][rev] += max(n, 1)

    suggestions: list[dict[str, str | int | float]] = []
    seen: set[tuple[str, str]] = set()

    def add_row(
        *,
        raw_name: str,
        source: str,
        candidate_code: str,
        candidate_name: str,
        evidence_count: int,
        confidence: str,
        reason: str,
    ) -> None:
        key = (source, normalize_raw(raw_name).lower())
        if key in seen:
            return
        if resolve(raw_name, source) or resolve(raw_name, "any"):
            return
        seen.add(key)
        suggestions.append(
            {
                "raw_name": raw_name,
                "source": source,
                "candidate_payer_org_code": candidate_code,
                "candidate_payer_org": candidate_name,
                "evidence_count": evidence_count,
                "confidence": confidence,
                "reason": reason,
            }
        )

    # 1) Unmapped / daily WebPT names with SLA evidence to a known org.
    for raw, count in webpt_counts.most_common():
        if resolve(raw, "webpt") or resolve(raw, "any"):
            continue
        key = normalize_raw(raw).lower()
        linked = evidence.get(key)
        if linked:
            top_rev, top_n = linked.most_common(1)[0]
            rev_hit = resolve(top_rev, "revflow") or resolve(top_rev, "any")
            if rev_hit is not None:
                add_row(
                    raw_name=raw,
                    source="webpt",
                    candidate_code=rev_hit.code,
                    candidate_name=rev_hit.name,
                    evidence_count=top_n,
                    confidence="high" if top_n >= 10 else "medium",
                    reason=f"sla_pair→{top_rev}",
                )
                continue

        # Soft token overlap against known org names / aliases.
        best_code = ""
        best_name = ""
        best_score = 0.0
        for org in registry.orgs:
            score = _token_overlap(raw, org.name)
            for alias in org.aliases:
                if alias.source != "webpt":
                    continue
                label = alias.exact or alias.pattern or ""
                score = max(score, _token_overlap(raw, label))
            if score > best_score:
                best_score = score
                best_code = org.code
                best_name = org.name
        if best_score >= 0.4 and best_code:
            add_row(
                raw_name=raw,
                source="webpt",
                candidate_code=best_code,
                candidate_name=best_name,
                evidence_count=count,
                confidence="low",
                reason=f"token_overlap={best_score:.2f}",
            )
        elif count >= 5:
            add_row(
                raw_name=raw,
                source="webpt",
                candidate_code="",
                candidate_name="",
                evidence_count=count,
                confidence="review",
                reason="unmapped_frequent",
            )

    # 2) RevFlow payors not in registry.
    for payor, count in revflow_counts.most_common():
        if payor.upper() in known_revflow:
            continue
        if resolve(payor, "revflow") or resolve(payor, "any"):
            continue
        best_code = ""
        best_name = ""
        best_score = 0.0
        for org in registry.orgs:
            score = _token_overlap(payor, org.name)
            for known in org.revflow_payors:
                score = max(score, _token_overlap(payor, known))
            if score > best_score:
                best_score = score
                best_code = org.code
                best_name = org.name
        add_row(
            raw_name=payor,
            source="revflow",
            candidate_code=best_code if best_score >= 0.35 else "",
            candidate_name=best_name if best_score >= 0.35 else "",
            evidence_count=count,
            confidence="medium" if best_score >= 0.5 else "low" if best_score >= 0.35 else "review",
            reason=(
                f"token_overlap={best_score:.2f}" if best_score >= 0.35 else "revflow_unmapped"
            ),
        )

    # 3) Tracker ACH heads (optional CSV with a Description column).
    if tracker_descriptions and tracker_descriptions.exists():
        desc_counts = _read_col_counts(tracker_descriptions, "Description")
        for desc, count in desc_counts.most_common():
            head = extract_ach_payer_head(desc)
            if not head:
                continue
            if resolve(head, "tracker") or resolve(head, "any"):
                continue
            best_code = ""
            best_name = ""
            best_score = 0.0
            for org in registry.orgs:
                score = _token_overlap(head, org.name)
                for alias in org.aliases:
                    if alias.source != "tracker":
                        continue
                    label = alias.exact or alias.pattern or ""
                    score = max(score, _token_overlap(head, label))
                if score > best_score:
                    best_score = score
                    best_code = org.code
                    best_name = org.name
            add_row(
                raw_name=head,
                source="tracker",
                candidate_code=best_code if best_score >= 0.35 else "",
                candidate_name=best_name if best_score >= 0.35 else "",
                evidence_count=count,
                confidence="medium" if best_score >= 0.5 else "low" if best_score >= 0.35 else "review",
                reason=(
                    f"token_overlap={best_score:.2f}"
                    if best_score >= 0.35
                    else "tracker_unmapped"
                ),
            )

    suggestions.sort(
        key=lambda r: (
            {"high": 0, "medium": 1, "low": 2, "review": 3}.get(str(r["confidence"]), 9),
            -int(r["evidence_count"]),
            str(r["source"]),
            str(r["raw_name"]).lower(),
        )
    )
    return suggestions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daily-notes", type=Path, default=None)
    parser.add_argument("--unmapped", type=Path, default=None)
    parser.add_argument("--payer-sla", type=Path, default=None)
    parser.add_argument("--revflow-dir", type=Path, default=None)
    parser.add_argument(
        "--tracker-descriptions",
        type=Path,
        default=None,
        help="Optional CSV with a Description column (e.g. exported tracker ledger)",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    rows = suggest_rows(
        daily_notes=args.daily_notes,
        unmapped=args.unmapped,
        payer_sla=args.payer_sla,
        revflow_dir=args.revflow_dir,
        tracker_descriptions=args.tracker_descriptions,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "raw_name",
        "source",
        "candidate_payer_org_code",
        "candidate_payer_org",
        "evidence_count",
        "confidence",
        "reason",
    ]
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} suggestions → {args.out}")


if __name__ == "__main__":
    main()

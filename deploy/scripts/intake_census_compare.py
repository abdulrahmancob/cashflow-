"""Shadow checks for a new intake census against the previous run (host only).

Compares the Oct 5 run (``--old``, rows.jsonl + patients.csv + summary.json) with the new one
(``--new``): the inventory totals must be identical, the distribution gates from the plan are
evaluated, the patient-level transitions old -> new are counted, and a spot-check map of files
(half changed, half unchanged) is written for ``intake_census_eval.py --boards all`` so the
crops can be looked at. Writes ``compare.json`` into the new directory and prints a summary.

  python intake_census_compare.py --old /data/exports/intake_census --new /data/exports/intake_census_v2
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from pathlib import Path

OLD_NAMES = {"unmarked_options": "unmarked", "social": "social_media", "blank": "unmarked", "friends_family": "friend_family"}
GATES = {"unmarked": 0.05, "unreadable": 0.02, "event": 0.03, "multiple": 0.10, "phone": 0.002, "website": 0.002}


def read_rows(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if not path.is_file():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = row.get("path")
            if key:
                rows[key] = row
    return rows


def read_patients(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.is_file():
        return out
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            pid = row.get("webpt_patient_id") or row.get("patient_id") or ""
            if pid:
                out[pid] = row
    return out


def norm(source: str) -> str:
    source = (source or "").strip()
    return OLD_NAMES.get(source, source) or "none"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old", default="/data/exports/intake_census")
    parser.add_argument("--new", default="/data/exports/intake_census_v2")
    parser.add_argument("--spot", type=int, default=100, help="files per group (changed / unchanged) in the spot-check map")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    old_dir, new_dir = Path(args.old), Path(args.new)

    old_summary = json.loads((old_dir / "summary.json").read_text(encoding="utf-8")) if (old_dir / "summary.json").is_file() else {}
    new_summary = json.loads((new_dir / "summary.json").read_text(encoding="utf-8")) if (new_dir / "summary.json").is_file() else {}
    totals = {}
    for key in ("patients_total", "with_intake", "intake_files"):
        totals[key] = {"old": old_summary.get(key), "new": new_summary.get(key), "same": old_summary.get(key) == new_summary.get(key)}

    old_pat = read_patients(old_dir / "patients.csv")
    new_pat = read_patients(new_dir / "patients.csv")
    with_intake = {pid: row for pid, row in new_pat.items() if (row.get("has_intake") or "yes") == "yes" and row.get("source")}
    n = max(1, len(with_intake))
    dist = Counter(norm(row.get("source")) for row in with_intake.values())
    gates = {}
    for code, limit in GATES.items():
        share = dist.get(code, 0) / n
        gates[code] = {"count": dist.get(code, 0), "share": round(share, 4), "limit": limit, "pass": share <= limit}
    review = sum(1 for row in with_intake.values() if row.get("needs_review"))
    reasons = Counter()
    for row in with_intake.values():
        for reason in (row.get("reason") or "").split(","):
            reason = reason.strip().split(":")[0]
            if reason:
                reasons[reason] += 1

    transitions = Counter()
    for pid, row in with_intake.items():
        old_row = old_pat.get(pid)
        if old_row is None:
            continue
        transitions[(norm(old_row.get("source")), norm(row.get("source")))] += 1
    old_dist = Counter(norm(row.get("source")) for pid, row in old_pat.items() if pid in with_intake)

    old_rows = read_rows(old_dir / "rows.jsonl")
    new_rows = read_rows(new_dir / "rows.jsonl")
    changed, kept = [], []
    for path, row in new_rows.items():
        old_row = old_rows.get(path)
        if old_row is None or not row.get("source"):
            continue
        (kept if norm(old_row.get("source")) == norm(row.get("source")) else changed).append((path, norm(old_row.get("source")), norm(row.get("source"))))
    rng = random.Random(args.seed)
    rng.shuffle(changed)
    rng.shuffle(kept)
    spot_map: dict[str, str] = {}
    label_lines = ["# spot check: the pseudo label is the Oct 5 reading with a question mark, so a different new reading is soft, never a miss"]
    for prefix, group in (("C", changed[: args.spot]), ("K", kept[: args.spot])):
        for i, (path, old_source, new_source) in enumerate(group):
            code = f"{prefix}{i:03d}"
            spot_map[code] = path
            label_lines.append(f"{code}\t{old_source}?\told={old_source} new={new_source}")
    (new_dir / "spot_map.json").write_text(json.dumps(spot_map, indent=1), encoding="utf-8")
    (new_dir / "spot_labels.tsv").write_text("\n".join(label_lines) + "\n", encoding="utf-8")

    report = {
        "totals": totals,
        "patients_with_source": len(with_intake),
        "distribution_new": dict(dist.most_common()),
        "distribution_old_same_patients": dict(old_dist.most_common()),
        "gates": gates,
        "needs_review": review,
        "needs_review_share": round(review / n, 4),
        "review_reasons": dict(reasons.most_common(30)),
        "transitions_top": [{"old": a, "new": b, "patients": c} for (a, b), c in transitions.most_common(40)],
        "files_compared": len(changed) + len(kept),
        "files_changed": len(changed),
        "files_unchanged": len(kept),
        "spot_map": len(spot_map),
    }
    (new_dir / "compare.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("COMPARE_TOTALS " + " ".join(f"{k}={v['new']}({'same' if v['same'] else 'WAS ' + str(v['old'])})" for k, v in totals.items()))
    for code, gate in gates.items():
        print(f"COMPARE_GATE {code} share={gate['share']:.3f} limit={gate['limit']} {'pass' if gate['pass'] else 'FAIL'}")
    print(f"COMPARE_REVIEW needs_review={review} share={review / n:.3f}")
    print("COMPARE_DIST_NEW " + " ".join(f"{k}={v}" for k, v in dist.most_common()))
    print("COMPARE_DIST_OLD " + " ".join(f"{k}={v}" for k, v in old_dist.most_common()))
    for item in report["transitions_top"][:25]:
        print(f"COMPARE_MOVE {item['old']} -> {item['new']} patients={item['patients']}")
    print(f"COMPARE_FILES compared={len(changed) + len(kept)} changed={len(changed)} unchanged={len(kept)} spot={len(spot_map)}")
    print("COMPARE_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

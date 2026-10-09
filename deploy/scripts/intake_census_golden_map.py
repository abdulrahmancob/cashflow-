"""Rebuild the golden-set map (code -> intake PDF path) on the host.

The labelled codes in `intake_reader/tests/golden_labels.tsv` were drawn from the Oct 5 2026 run:
  U*/D*  sample_holdout/map.json of that run,
  G F Z I E O S W P B K R  `grep source | shuf -n N --random-source=<(yes 42)` over rows.jsonl,
  T*     the first 40 patients with an intake in patients.csv (sorted by WebPT id), first intake file.
Everything is deterministic, so the map is rebuilt here instead of being committed (file names can
carry patient names). Writes golden_map.json and golden_map_check.json (code -> md5 of the path).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

OLD = Path(os.environ.get("INTAKE_CENSUS_OLD_DIR", "/data/exports/intake_census"))
CASES = Path(os.environ.get("CASE_PIPELINE_DIR", "/data/exports/side_by_side_case")) / "cases"
SAMPLES = (
    ("google", "G", 16),
    ("friend_family", "F", 16),
    ("zocdoc", "Z", 16),
    ("insurance", "I", 16),
    ("event", "E", 16),
    ("other", "O", 16),
    ("social_media", "S", 16),
    ("walk_in", "W", 8),
    ("phone", "P", 4),
    ("website", "B", 2),
    ("blank", "K", 1),
    ("unreadable", "R", 24),
)
# The audit drew walk_in/phone/website/blank with one `shuf -n 8` each and kept what came out,
# so the draw must ask for 8 even when the bucket needs fewer codes.
DRAW_N = {"W": 8, "P": 8, "B": 8, "K": 8}


def shuf_sample(rows: Path, source: str, count: int) -> list[str]:
    cmd = f'grep -F \'"source": "{source}"\' "{rows}" | shuf -n {count} --random-source=<(yes 42) | cut -c1-260'
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=False).stdout
    paths: list[str] = []
    for line in out.splitlines():
        match = re.search(r'"path": "([^"]+)"', line)
        if match:
            paths.append(match.group(1))
    return paths


def meta_ids(case_dir: Path) -> set[str]:
    try:
        meta = json.loads((case_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    ids = set()
    if isinstance(meta, dict):
        raw = meta.get("patient_ids")
        if isinstance(raw, list):
            ids |= {str(x).strip() for x in raw}
        if meta.get("patient_id"):
            ids.add(str(meta.get("patient_id")).strip())
    return ids


# The first 40 rows with an intake in the Oct 5 workbook (its Patients sheet order), i.e. the rows
# the user checked by hand. WebPT ids only; the files are resolved on the host.
FIRST_ROWS = (
    "45250965", "50823766", "51124090", "56206276", "56569736", "56720430", "56777174", "56783176",
    "56795058", "56810038", "56820299", "56842821", "56864812", "56867655", "56871200", "56872833",
    "56876890", "56881556", "56881766", "56882818", "56885623", "56887135", "56887530", "56895375",
    "56897264", "56898501", "56901731", "56902404", "56906866", "56912533", "56916208", "56917667",
    "56917772", "56920083", "56922487", "56923362", "56923823", "56926150", "56926200", "56926235",
)


def first_rows(patients_csv: Path, count: int) -> list[str]:
    return list(FIRST_ROWS[:count])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/data/exports/intake_census_v2/golden_map.json")
    args = parser.parse_args()
    rows = OLD / "rows.jsonl"
    mapping: dict[str, str] = {}
    holdout = json.loads((OLD / "sample_holdout" / "map.json").read_text(encoding="utf-8"))
    mapping.update({code: path for code, path in holdout.items() if code[0] in "UD"})
    for source, prefix, count in SAMPLES:
        for index, path in enumerate(shuf_sample(rows, source, DRAW_N.get(prefix, count))[:count]):
            mapping[f"{prefix}{index:02d}"] = path
    # first 40 sheet rows -> first intake file of that patient
    wanted = first_rows(OLD / "patients.csv", 40)
    by_pid: dict[str, list[str]] = {pid: [] for pid in wanted}
    row_paths: list[str] = []
    with rows.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = re.search(r'"path": "([^"]+)"', line)
            if match:
                row_paths.append(match.group(1))
    case_dirs: dict[str, list[Path]] = {pid: [] for pid in wanted}
    want = set(wanted)
    for facility in sorted(CASES.iterdir()):
        if not facility.is_dir():
            continue
        for case in sorted(facility.iterdir()):
            if not (case / "meta.json").is_file():
                continue
            hit = meta_ids(case) & want
            for pid in hit:
                case_dirs[pid].append(case)
    for index, pid in enumerate(wanted):
        for case in case_dirs[pid]:
            prefix = str(case) + "/"
            found = [p for p in row_paths if p.startswith(prefix)]
            if found:
                mapping[f"T{index:02d}"] = found[0]
                break
        # patients linked through /data/webpt/<pack>/edocs/<pid>/ instead of a case folder
        if f"T{index:02d}" not in mapping:
            found = [p for p in row_paths if f"/edocs/{pid}/" in p]
            if found:
                mapping[f"T{index:02d}"] = found[0]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(mapping, indent=0, ensure_ascii=False), encoding="utf-8")
    check = {code: hashlib.md5(path.encode("utf-8")).hexdigest()[:10] for code, path in sorted(mapping.items())}
    out.with_name("golden_map_check.json").write_text(json.dumps(check, indent=0), encoding="utf-8")
    missing = [f"T{i:02d}" for i in range(len(wanted)) if f"T{i:02d}" not in mapping]
    print(f"GOLDEN_MAP codes={len(mapping)} missing_first_rows={missing}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

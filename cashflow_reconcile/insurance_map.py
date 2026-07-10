"""Map WebPT insurance strings to RevFlow payor names."""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None  # type: ignore[assignment]


DEFAULT_MAP_PATH = Path(__file__).with_name("insurance_map.yaml")


@dataclass(frozen=True)
class InsuranceRule:
    patterns: tuple[str, ...]
    payors: tuple[str, ...]
    regexes: tuple[re.Pattern[str], ...]


def _compile_rules(raw_rules: list[dict]) -> list[InsuranceRule]:
    rules: list[InsuranceRule] = []
    for item in raw_rules:
        patterns = tuple(str(p).strip().lower() for p in item.get("patterns", []) if str(p).strip())
        payors = tuple(str(p).strip() for p in item.get("payors", []) if str(p).strip())
        if not patterns or not payors:
            continue
        regexes = tuple(re.compile(pat, re.IGNORECASE) for pat in patterns)
        rules.append(InsuranceRule(patterns=patterns, payors=payors, regexes=regexes))
    return rules


def load_insurance_rules(map_path: Path | None = None) -> list[InsuranceRule]:
    path = map_path or DEFAULT_MAP_PATH
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return _compile_rules(data.get("mappings", []))


def map_insurance_to_payors(
    insurance_value: str,
    rules: list[InsuranceRule],
) -> list[str]:
    text = (insurance_value or "").strip()
    if not text:
        return []
    lowered = text.lower()
    matches: list[str] = []
    for rule in rules:
        if any(regex.search(lowered) for regex in rule.regexes):
            matches.extend(rule.payors)
    deduped: list[str] = []
    for payor in matches:
        if payor not in deduped:
            deduped.append(payor)
    return deduped


def _normalize_payor(value: str) -> str:
    text = (value or "").upper()
    text = text.replace("/", "").replace("-", " ").replace(",", " ")
    return re.sub(r"\s+", " ", text).strip()


def payor_matches_insurance(
    revflow_payor: str,
    insurance_values: list[str],
    rules: list[InsuranceRule],
) -> bool:
    payor = _normalize_payor(revflow_payor)
    if not payor:
        return False
    expected: set[str] = set()
    for value in insurance_values:
        for mapped in map_insurance_to_payors(value, rules):
            expected.add(_normalize_payor(mapped))
    if not expected:
        return True
    return any(
        payor == candidate
        or payor in candidate
        or candidate in payor
        or payor.replace(" ", "") == candidate.replace(" ", "")
        for candidate in expected
    )


def suggest_mappings(
    webpt_insurances: Counter[str],
    revflow_payors: Counter[str],
    rules: list[InsuranceRule],
) -> list[dict[str, str | int]]:
    rows: list[dict[str, str | int]] = []
    revflow_names = list(revflow_payors.keys())

    for insurance, count in webpt_insurances.most_common():
        mapped = map_insurance_to_payors(insurance, rules)
        suggested = "; ".join(mapped)
        if not mapped:
            lowered = insurance.lower()
            guesses = [
                payor
                for payor in revflow_names
                if any(token in payor.lower() for token in lowered.split() if len(token) > 3)
            ][:3]
            suggested = "; ".join(guesses)
        rows.append(
            {
                "webpt_insurance": insurance,
                "suggested_revflow_payor": suggested,
                "mapped_payors": "; ".join(mapped),
                "match_count": count,
                "mapped": "yes" if mapped else "no",
            }
        )
    return rows


def write_insurance_mapping_report(
    rows: list[dict[str, str | int]],
    output_path: Path,
) -> None:
    fieldnames = [
        "webpt_insurance",
        "suggested_revflow_payor",
        "mapped_payors",
        "match_count",
        "mapped",
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def collect_webpt_insurance_counts(lines) -> Counter[str]:
    counts: Counter[str] = Counter()
    for line in lines:
        if line.ins_name:
            counts[line.ins_name] += 1
        if line.insurance_note and line.insurance_note != line.ins_name:
            counts[line.insurance_note] += 1
    return counts


def collect_revflow_payor_counts(payments) -> Counter[str]:
    return Counter(payment.payor for payment in payments if payment.payor)

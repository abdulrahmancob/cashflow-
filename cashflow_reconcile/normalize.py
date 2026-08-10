"""Name, date, and money normalization helpers."""

from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, datetime
from typing import Any

WEBPT_NAME_RE = re.compile(r"^\s*([^,]+),\s*(.+?)\s*$")
MONEY_RE = re.compile(r"^\(?\$?\s*([\d,]+(?:\.\d{2})?)\s*\)?$")


def name_match_levenshtein_enabled() -> bool:
    """Opt-in only — medical identity; default OFF (CASHFLOW_NAME_MATCH_LEVENSHTEIN=1)."""
    return (os.environ.get("CASHFLOW_NAME_MATCH_LEVENSHTEIN") or "").strip() in {
        "1",
        "true",
        "TRUE",
        "yes",
        "YES",
    }


def parse_webpt_name(name: str) -> tuple[str, str]:
    text = (name or "").strip()
    match = WEBPT_NAME_RE.match(text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    parts = text.split(None, 1)
    if len(parts) == 2:
        return parts[1], parts[0]
    return text, ""


def normalize_name_key(last: str, first: str) -> str:
    """Collapse to uppercase alnum for cross-system matching."""
    first_token = (first or "").split()[0] if first else ""
    combined = f"{last} {first_token}".upper()
    return re.sub(r"[^A-Z0-9]", "", combined)


def name_key_from_webpt(patient_name: str) -> str:
    last, first = parse_webpt_name(patient_name)
    return normalize_name_key(last, first)


def name_key_from_revflow(last: str, first: str) -> str:
    return normalize_name_key(last or "", first or "")


def _alnum_upper(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _unicode_fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _strip_hyphen_apostrophe(text: str) -> str:
    return (
        (text or "")
        .replace("-", " ")
        .replace("'", " ")
        .replace("\u2019", " ")
        .replace("`", " ")
    )


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(
                min(
                    cur[j - 1] + 1,
                    prev[j] + 1,
                    prev[j - 1] + (ca != cb),
                )
            )
        prev = cur
    return prev[-1]


def _common_suffix(a: str, b: str) -> str:
    n = 0
    limit = min(len(a), len(b))
    while n < limit and a[-(n + 1)] == b[-(n + 1)]:
        n += 1
    return a[-n:] if n else ""


def _compound_surname_compatible(a: str, b: str) -> bool:
    """Same first-name suffix; one last-name extends the other by a real token.

    Requires a surname-length delta ≥ 3 (e.g. ALMONTE+REYES) so single-letter
    typos (ROSADO vs ROSADOE / ELIZBETH vs ELIZABETH) do not look like compounds.
    """
    if a == b:
        return True
    suf = _common_suffix(a, b)
    if len(suf) < 3:
        return False
    last_a, last_b = a[: -len(suf)], b[: -len(suf)]
    if not last_a or not last_b or last_a == last_b:
        return False
    short, long = (last_a, last_b) if len(last_a) <= len(last_b) else (last_b, last_a)
    if len(long) - len(short) < 3:
        return False
    return long.startswith(short) or short in long


def name_keys_compatible(
    a: str,
    b: str,
    *,
    allow_levenshtein: bool | None = None,
) -> bool:
    """Data-driven soft identity: exact → compound → hyphen/apos → unicode → (opt) edit-1.

    Levenshtein is OFF unless allow_levenshtein=True or env flag is set.
    """
    ka = _alnum_upper(a)
    kb = _alnum_upper(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True

    # Compound surname (dominant class in name_key_mismatch report)
    if _compound_surname_compatible(ka, kb):
        return True

    # Hyphen / apostrophe / unicode folds (usually no-ops on already-alnum keys,
    # but equalize if either side still carries punct/marks).
    folded_a = _alnum_upper(_unicode_fold(_strip_hyphen_apostrophe(a)))
    folded_b = _alnum_upper(_unicode_fold(_strip_hyphen_apostrophe(b)))
    if folded_a and folded_a == folded_b:
        return True
    if _compound_surname_compatible(folded_a, folded_b):
        return True

    use_lev = (
        name_match_levenshtein_enabled()
        if allow_levenshtein is None
        else allow_levenshtein
    )
    if use_lev and min(len(ka), len(kb)) >= 6 and _levenshtein(ka, kb) == 1:
        return True
    return False


def parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def format_date(value: date | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat() if value else ""


def parse_money(value: str | None) -> float:
    text = (value or "").strip()
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.replace("$", "").replace(",", "").strip("() ")
    if not cleaned:
        return 0.0
    try:
        amount = float(cleaned)
    except ValueError:
        return 0.0
    return -amount if negative else amount


def format_money(value: float) -> str:
    return f"{value:.2f}"


def safe_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text or text.startswith("$") or text.startswith("("):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def join_carcs(carcs: list[str]) -> str:
    seen: list[str] = []
    for code in carcs:
        code = (code or "").strip()
        if code and code not in seen:
            seen.append(code)
    return "; ".join(seen)


def split_carcs(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def pick_first(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""

#!/usr/bin/env python3
"""Classify name_key_mismatch true-gap visits into structural buckets (before soft-match).

Buckets (first match wins):
  compound_surname, hyphen, apostrophe, middle_name, unicode_punct,
  typo_edit1 (measurement only), truly_different, no_close_eob_peer
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_reconcile.normalize import (  # noqa: E402
    name_key_from_webpt,
    parse_date,
    parse_webpt_name,
)


def _norm_check(value: str) -> str:
    text = (value or "").strip().upper()
    text = re.sub(r"[\s\-]", "", text)
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def _alnum(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    if abs(len(a) - len(b)) > 1 and min(len(a), len(b)) < 4:
        # cheap reject for far pairs
        pass
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


def load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _strip_hyphen_apos(text: str) -> str:
    return (text or "").replace("-", " ").replace("'", " ").replace("’", " ").replace("`", " ")


def _unicode_fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def classify_pair(
    *,
    webpt_name: str,
    webpt_nk: str,
    eob_nk: str,
) -> str:
    """Classify a single (webpt, eob) name_key pair."""
    if not webpt_nk or not eob_nk:
        return "truly_different"
    if webpt_nk == eob_nk:
        return "exact"  # should not appear in mismatch cohort

    last, first = parse_webpt_name(webpt_name)
    first_parts = [p for p in (first or "").split() if p]
    first0 = _alnum(first_parts[0]) if first_parts else ""
    last_alnum = _alnum(last)
    webpt_last_raw = last or ""

    # 1) compound surname: same first token suffix; last containment
    if first0 and webpt_nk.endswith(first0) and eob_nk.endswith(first0):
        w_last = webpt_nk[: -len(first0)] if first0 else webpt_nk
        e_last = eob_nk[: -len(first0)] if first0 else eob_nk
        if w_last and e_last and w_last != e_last:
            if w_last.startswith(e_last) or e_last.startswith(w_last) or e_last in w_last or w_last in e_last:
                return "compound_surname"
        # multi-token last in raw WebPT, eob shorter last
        if " " in webpt_last_raw.strip() or "-" in webpt_last_raw:
            if w_last and e_last and (e_last in w_last or w_last.startswith(e_last)):
                return "compound_surname"

    # 2) hyphen — re-key after hyphen→space on raw webpt; or keys equal after removing
    #    letter runs that differ only by hyphen join (already stripped in keys → rare)
    last_h, first_h = parse_webpt_name(_strip_hyphen_apos(webpt_name))
    nk_h = _alnum(last_h) + _alnum((first_h or "").split()[0] if first_h else "")
    eob_h = _alnum(_strip_hyphen_apos(eob_nk))  # eob is already key; hyphen already gone
    if "-" in (webpt_name or "") and nk_h == eob_nk:
        return "hyphen"
    if webpt_nk.replace(" ", "") == eob_nk and "-" in (webpt_name or ""):
        return "hyphen"

    # 3) apostrophe
    if ("'" in (webpt_name or "") or "’" in (webpt_name or "")) and nk_h == eob_nk:
        return "apostrophe"
    # O'Brien style: raw has apostrophe, keys differ from a form that includes O
    if "'" in (webpt_name or "") or "’" in (webpt_name or ""):
        # compare alnum of unicode-folded hyphen/apos stripped
        folded = _alnum(_unicode_fold(_strip_hyphen_apos(webpt_name.split(",")[0] if "," in webpt_name else last)))
        if first0 and eob_nk == folded + first0:
            return "apostrophe"

    # 4) middle name — eob matches last + alternate first token
    if len(first_parts) >= 2 and last_alnum:
        for tok in first_parts[1:]:
            alt = last_alnum + _alnum(tok)
            if alt == eob_nk:
                return "middle_name"
        # eob first token is webpt middle; webpt key uses first given name
        if first0 and eob_nk.endswith(_alnum(first_parts[1])):
            e_last = eob_nk[: -len(_alnum(first_parts[1]))]
            if e_last == last_alnum:
                return "middle_name"

    # 5) unicode / punct fold
    w_fold = _alnum(_unicode_fold(last)) + _alnum(_unicode_fold(first_parts[0] if first_parts else ""))
    if w_fold and w_fold == eob_nk and w_fold != webpt_nk:
        return "unicode_punct"
    if _alnum(_unicode_fold(webpt_nk)) == _alnum(_unicode_fold(eob_nk)) and webpt_nk != eob_nk:
        return "unicode_punct"

    # 6) typo measurement (edit distance 1) — not a match decision
    if min(len(webpt_nk), len(eob_nk)) >= 6 and _levenshtein(webpt_nk, eob_nk) == 1:
        return "typo_edit1"

    return "truly_different"


def _best_eob_key(webpt_nk: str, webpt_name: str, eob_nks: set[str]) -> tuple[str, int]:
    """Pick closest EOB name_key peer; return (key, score) lower score better."""
    if not eob_nks:
        return "", 999
    last, first = parse_webpt_name(webpt_name)
    first0 = _alnum((first or "").split()[0] if first else "")
    best = ""
    best_score = 10**9
    for ek in eob_nks:
        if ek == webpt_nk:
            return ek, 0
        score = _levenshtein(webpt_nk, ek)
        # prefer same first-token suffix
        if first0 and ek.endswith(first0) and webpt_nk.endswith(first0):
            score -= 50
            w_last = webpt_nk[: -len(first0)]
            e_last = ek[: -len(first0)]
            if w_last and e_last and (e_last in w_last or w_last in e_last):
                score -= 100
        if score < best_score:
            best_score = score
            best = ek
    return best, best_score


def collect_mismatch_rows(
    gap_rows: list[dict[str, str]],
    recon_lines: list[dict[str, str]],
    eob_rows: list[dict[str, str]],
) -> list[dict[str, Any]]:
    lines_by_emr_dos: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in recon_lines:
        emr = (row.get("webpt_patient_id") or "").strip()
        dos = (parse_date(row.get("date_of_service")) or date.min).isoformat()
        if emr and dos != date.min.isoformat():
            lines_by_emr_dos[(emr, dos)].append(row)

    eob_by_check: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in eob_rows:
        chk = _norm_check(row.get("check_eft_num") or "")
        if chk:
            eob_by_check[chk].append(row)

    out: list[dict[str, Any]] = []
    for gap in gap_rows:
        emr = (gap.get("emr_id") or "").strip()
        dos = (gap.get("dos") or "").strip()
        our_lines = lines_by_emr_dos.get((emr, dos), [])
        webpt_name = ""
        if our_lines:
            webpt_name = our_lines[0].get("patient_name") or ""
        if not webpt_name:
            webpt_name = gap.get("patient") or ""
        webpt_nk = name_key_from_webpt(webpt_name) if webpt_name else ""
        # Prefer "Last, First" from WebPT line for parse; SF patient is "First Last"
        if our_lines:
            webpt_nk = name_key_from_webpt(our_lines[0].get("patient_name") or "")

        checks = [
            _norm_check(c)
            for c in (gap.get("sf_checks") or gap.get("sf_check") or "").split(";")
            if _norm_check(c)
        ]
        eob_for: list[dict[str, str]] = []
        for c in checks:
            eob_for.extend(eob_by_check.get(c, []))
        eob_same = [
            e
            for e in eob_for
            if (parse_date(e.get("date_of_service")) or date.min).isoformat() == dos
        ]
        if not eob_same:
            eob_same = eob_for

        eob_nks = {
            (e.get("name_key") or "").strip().upper()
            for e in eob_same
            if (e.get("name_key") or "").strip()
        }
        if not webpt_nk or not eob_nks or webpt_nk in eob_nks:
            continue  # not a name_key_mismatch

        best_eob, score = _best_eob_key(webpt_nk, webpt_name if our_lines else "", eob_nks)
        # If we only have SF "First Last", synthesize WebPT-like for compound detection
        parse_name = our_lines[0].get("patient_name") if our_lines else ""
        if not parse_name:
            # SF style First Last → Last, First
            parts = (gap.get("patient") or "").strip().split()
            if len(parts) >= 2:
                parse_name = f"{parts[-1]}, {' '.join(parts[:-1])}"
            else:
                parse_name = gap.get("patient") or ""

        if score > 40 and not (
            best_eob
            and webpt_nk
            and any(
                webpt_nk.endswith(_alnum((parse_webpt_name(parse_name)[1] or "").split()[0] or "X"))
                and best_eob.endswith(_alnum((parse_webpt_name(parse_name)[1] or "").split()[0] or "X"))
                for _ in [0]
            )
        ):
            # far peer among large remit — no close identity candidate
            bucket = "no_close_eob_peer"
        else:
            bucket = classify_pair(webpt_name=parse_name, webpt_nk=webpt_nk, eob_nk=best_eob)
            if bucket == "truly_different" and score > 8:
                bucket = "no_close_eob_peer"

        out.append(
            {
                "emr_id": emr,
                "dos": dos,
                "patient": gap.get("patient") or "",
                "webpt_name": parse_name,
                "webpt_name_key": webpt_nk,
                "best_eob_name_key": best_eob,
                "best_score": score,
                "eob_peer_count": len(eob_nks),
                "sf_check": gap.get("sf_check") or "",
                "class": bucket,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gap-visits", type=Path, required=True)
    p.add_argument("--recon-lines", type=Path, required=True)
    p.add_argument("--eob-payments", type=Path, required=True)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("snowflake_pull/output/sf_eval/name_key_mismatch_by_class.json"),
    )
    args = p.parse_args(argv)

    rows = collect_mismatch_rows(
        load_csv(args.gap_visits),
        load_csv(args.recon_lines),
        load_csv(args.eob_payments),
    )
    counts = Counter(r["class"] for r in rows)
    total = sum(counts.values()) or 1
    report = {
        "total": len(rows),
        "by_class": dict(counts.most_common()),
        "by_class_pct": {k: round(100.0 * v / total, 1) for k, v in counts.most_common()},
        "note": (
            "typo_edit1 is measurement-only. no_close_eob_peer = check/DOS remit has "
            "no near name_key for this WebPT patient (possible analyzer over-label)."
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    sample_path = args.out.with_name("name_key_mismatch_class_samples.csv")
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if len(samples[r["class"]]) < 20:
            samples[r["class"]].append(r)
    flat: list[dict[str, Any]] = []
    for cls, items in samples.items():
        for r in items:
            flat.append({**r, "class": cls})
    if flat:
        with sample_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(flat[0].keys()))
            w.writeheader()
            w.writerows(flat)

    # full classified CSV for later rematch
    full_path = args.out.with_name("name_key_mismatch_classified.csv")
    if rows:
        with full_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    print(json.dumps(report, indent=2))
    print(f"Wrote {args.out}", file=sys.stderr)
    print(f"Wrote {sample_path}", file=sys.stderr)
    print(f"Wrote {full_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

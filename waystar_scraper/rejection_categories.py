"""Shared rejection categorization and analysis helpers."""

import re
from collections import Counter, defaultdict

X12_INFO = re.compile(r"\[X12 Info[:\s]*([^\]]+)\]", re.IGNORECASE)
A3_CODE = re.compile(r"\bA[0-9]:[0-9]{1,3}(?::[0-9]{1,3})?\b")

CATEGORY_RULES = [
    ("Duplicate claim", ["duplicate"]),
    ("Eligibility / Member ID", [
        "eligibility", "member id", "subscriber id", "contract/member",
        "patient id not found", "subscriber and/or subscriber member id",
        "eligibility span", "subscriber's contract",
    ]),
    ("Provider enrollment / NPI", [
        "rendering provider", "billing provider", "credential/enrollment",
        "npi", "provider data management", "resubmit to anthem",
    ]),
    ("Patient demographics (zip/address/name/gender)", [
        "zip code", "address is missing", "address is invalid", "city is invalid",
        "gender code", "name must be the same", "entity type", "last name is missing",
        "relationship to insured",
    ]),
    ("Resubmission / Original Reference Number", [
        "original reference number", "frequency code is 7", "payer claim control number",
    ]),
    ("Accident / P&C info", [
        "accident", "property and casualty",
    ]),
    ("Diagnosis codes", [
        "diagnosis code",
    ]),
    ("Procedure code / modifier / frequency (SmartEdits)", [
        "procedure code", "modifier", "smartedit", "daily frequency",
        "covered service", "anatomical",
    ]),
    ("Print on form / hold", [
        "print on form", "rejected for hold",
    ]),
    ("Payer ID / routing", [
        "payer id", "payer crosswalk",
    ]),
    ("Attachment / documentation required", [
        "documentation", "attachment",
    ]),
]

ENRICHED_COLUMNS = [
    "rejection_count",
    "rejection_messages",
    "rejection_fix_urls",
    "rejection_fix_slugs",
    "rejection_original_message",
    "rejection_fetch_error",
]


def parse_money(value: str) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def categorize(text: str) -> str:
    lowered = text.lower()
    for name, keywords in CATEGORY_RULES:
        if any(kw in lowered for kw in keywords):
            return name
    return "Other / payer-specific"


def analysis_text(row: dict) -> str:
    return (row.get("rejection_messages") or "") + " " + (row.get("status_detail") or "")


def month_key(date_str: str) -> str:
    parts = (date_str or "").split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[0].zfill(2)}"
    return "?"


def top_counter_items(
    counter: Counter,
    charges: dict[str, float] | None = None,
    limit: int = 15,
) -> list[dict]:
    items = []
    for name, count in counter.most_common(limit):
        entry: dict = {"name": name, "count": count}
        if charges is not None:
            entry["charges"] = round(charges.get(name, 0.0), 2)
        items.append(entry)
    return items


def analyze_rejection_reasons(rows: list[dict], top_n: int = 15) -> dict:
    status_detail = Counter()
    status_charges: dict[str, float] = defaultdict(float)
    categories = Counter()
    category_charges: dict[str, float] = defaultdict(float)
    x12_segments = Counter()
    payers = Counter()
    payer_charges: dict[str, float] = defaultdict(float)
    a3_codes = Counter()

    for row in rows:
        detail = (row.get("status_detail") or "").strip() or "(empty)"
        text = analysis_text(row)
        charges = parse_money(row.get("charges", ""))

        status_detail[detail] += 1
        status_charges[detail] += charges

        cat = categorize(text)
        categories[cat] += 1
        category_charges[cat] += charges

        payer = (row.get("payer") or "?").strip()
        payers[payer] += 1
        payer_charges[payer] += charges

        for match in X12_INFO.findall(text):
            x12_segments[" ".join(match.split()).strip(" .")] += 1

        for match in A3_CODE.findall(row.get("rejection_original_message", "") or ""):
            a3_codes[match] += 1

    total_charges = sum(parse_money(r.get("charges", "")) for r in rows)

    return {
        "total_charges": round(total_charges, 2),
        "top_status_detail": top_counter_items(status_detail, status_charges, top_n),
        "top_categories": top_counter_items(categories, category_charges, top_n),
        "top_x12_segments": top_counter_items(x12_segments, limit=top_n),
        "top_payers": top_counter_items(payers, payer_charges, top_n),
        "top_a3_codes": top_counter_items(a3_codes, limit=top_n),
    }

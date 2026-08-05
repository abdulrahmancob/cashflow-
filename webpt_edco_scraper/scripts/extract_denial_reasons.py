"""Extract denial dates, insurance names, and classified reasons from eDoc PDFs.

Usage:
  python scripts/extract_denial_reasons.py \\
    --edocs-dir output/jun_jul_2026/edocs \\
    --out output/jun_jul_2026/extracted

  python scripts/extract_denial_reasons.py \\
    --edocs-dir output/jun_jul_2026/edocs \\
    --out output/jun_jul_2026/extracted \\
    --refresh-unparseable
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from edoc_ocr import extract_pdf_text, resolve_tesseract_paths  # noqa: E402

CSV_FIELDS = [
    "patient_id",
    "filename",
    "path",
    "extraction_method",
    "denial_date",
    "insurance_name",
    "payer_guess",
    "reason_raw",
    "reason_class",
    "error",
]

REASON_CLASSES = (
    "already_authorized",
    "documentation_insufficient",
    "skilled_care_not_supported",
    "visit_limit_or_benefit",
    "partial_approval",
    "criteria_not_met",
    "medical_necessity",
    "other",
    "not_a_denial",
    "unparseable",
)

FILENAME_DENIAL = re.compile(
    r"denial|denail|deniel|deanil|denaied|denaill|denyed|"
    r"\bdeny\b|\bdenied\b|partially\s*deny|partial\s*deny|"
    r"adverse\s*determination|notice\s+of\s+denial",
    re.I,
)
FILENAME_APPROVAL_ONLY = re.compile(
    r"approv|approval|\bapp\b|\bapps?\b",
    re.I,
)
CONTENT_DENIAL_MARKERS = re.compile(
    r"INITIAL\s+ADVERSE\s+DETERMINATION|"
    r"DENIAL\s+NOTICE|"
    r"Notice\s+of\s+Denial|"
    r"Coverage\s+for\s+ongoing\s+physical\s+therapy\s+is\s+denied|"
    r"Why\s+did\s+we\s+decide\s+to\s+deny|"
    r"Non[-\s]*Authorized\s*/\s*Criteria\s*Not\s*Met|"
    r"claim\s+administrator\s+has\s+denied|"
    r"has\s+been\s+denied\s+based\s+on",
    re.I,
)

PAYER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("healthfirst", re.compile(r"healthfirst", re.I)),
    ("fidelis", re.compile(r"fidelis", re.I)),
    ("evolent", re.compile(r"\bevolent\b", re.I)),
    ("carelon", re.compile(r"\bcarelon\b", re.I)),
    ("molina", re.compile(r"\bmolina\b", re.I)),
    ("metroplus", re.compile(r"metro\s*plus|metroplus", re.I)),
    ("wellcare", re.compile(r"well\s*care|wellcare", re.I)),
    ("emblem", re.compile(r"emblem", re.I)),
    ("united", re.compile(r"united\s*healthcare|uhc\b|oxford|optum", re.I)),
    ("empire", re.compile(
        r"empire\s+plan|nys\s+empire|new\s+york\s+state\s+empire|"
        r"managed\s+physical\s+network|\bmpn\b",
        re.I,
    )),
    ("anthem", re.compile(r"\banthem\b|wellpoint", re.I)),
    ("pace", re.compile(r"\bpace\b|centerlight", re.I)),
    ("mltc", re.compile(r"\bmltc\b", re.I)),
    ("aetna", re.compile(r"\baetna\b", re.I)),
    ("cigna", re.compile(r"\bcigna\b", re.I)),
    ("humana", re.compile(r"\bhumana\b", re.I)),
    ("workers_comp", re.compile(r"workers['\s]*compensation|\bwcb\b", re.I)),
    ("medicaid", re.compile(r"\bmedicaid\b", re.I)),
    ("medicare", re.compile(r"\bmedicare\b", re.I)),
]

NAMED_PLAN = re.compile(
    r"(Healthfirst\s+Medicare\s+Advantage|"
    r"Healthfirst\s+Medicaid\s+Managed\s+Care\s+Plan|"
    r"Healthfirst\s+Personal\s+Wellness\s+Plan|"
    r"Healthfirst[^\n,]{0,50}|"
    r"UnitedHealthcare(?:\s+Insurance\s+Company\s+of\s+New\s+York)?|"
    r"UnitedHealthcare(?:\s+Medicare)?|"
    r"The\s+New\s+York\s+State\s+Empire\s+Plan|"
    r"OptumHealth\s+Care\s+Solutions|"
    r"Molina\s+Healthcare[^\n,]{0,40}|"
    r"Centerlight\s+Healthcare\s+PACE|"
    r"WellCare[^\n,]{0,40}|"
    r"Fidelis\s+Care[^\n,]{0,40}|"
    r"Carelon\s+Medical\s+Benefits\s+Management|"
    r"Carelon|"
    r"Anthem[^\n,]{0,40}|"
    r"MetroPlusHealth|"
    r"MLTC\s*:\s*[^\n,]{0,40}|"
    r"Evolent\s+Specialty\s+Services)",
    re.I,
)

INSURANCE_KEYWORD = re.compile(
    r"health|care|plan|insurance|medicaid|medicare|wellcare|fidelis|"
    r"united|empire|mltc|optum|anthem|molina|metro|emblem|aetna|"
    r"cigna|humana|pace|workers|wcb|advantage|managed",
    re.I,
)
JUNK_INSURANCE = re.compile(
    r"^(?:outpatient\s+)?physical\s+therapy(?:\s+of\s+the\s+city)?$|"
    r"^as\s+denied$|^denied$|^coverage$|"
    r"^support\s+clinician$",
    re.I,
)

MONTH_NAME = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DATE_SLASH = re.compile(r"\b(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2,4})\b")
DATE_NAMED = re.compile(
    r"\b(?P<mon>January|February|March|April|May|June|July|August|September|"
    r"October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"\.?\s+(?P<d>\d{1,2}),?\s*(?P<y>\d{4})\b",
    re.I,
)
FILENAME_DATE = re.compile(
    r"(?:^|[_\-])(?P<m>\d{2})-(?P<d>\d{2})-(?P<y>\d{4})(?:[_\-]|$)|"
    r"(?:^|[_\-])(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})(?:[_\-]|$)"
)

COVERAGE_TYPE = re.compile(
    r"Coverage\s*Type\s*:\s*(.+?)(?:\n|Service\s*:|Provider\s*:|Enrollee|Member)",
    re.I | re.S,
)
HEALTH_PLAN = re.compile(
    r"Health\s*Plan\s*:\s*(.+?)(?:\n|Spoken|Written|Provider|Member|Your\s+Indicated)",
    re.I | re.S,
)
WHY_DENY = re.compile(
    r"Why\s+did\s+we\s+decide\s+to\s+deny\s+the\s+(?:request|claim)\??\s*(.+?)(?="
    r"What\s+should\s+you\s+do|How\s+to\s+ask\s+for\s+a\s+Plan\s+Appeal|"
    r"You\s+have\s+the\s+right|Page\s+\d+\s+of\s+\d+|$)",
    re.I | re.S,
)
DENIED_BECAUSE = re.compile(
    r"(?:denied(?:\s+the\s+(?:medical\s+)?services?(?:/items)?)?"
    r"(?:\s+listed\s+(?:above|below))?\s+because|"
    r"decided\s+to\s+deny\s+this\s+(?:service|claim|Physical\s+Therapy)"
    r"[^.]*because)\s*(.+?)(?=\n\n|What\s+should|How\s+to\s+ask|Page\s+\d|$)",
    re.I | re.S,
)
UHC_DENIED = re.compile(
    r"(Coverage\s+for\s+ongoing\s+physical\s+therapy\s+is\s+denied\..{0,700})",
    re.I | re.S,
)
PACE_DENIED = re.compile(
    r"(?:has\s+been\s+denied\s+based\s+on\s*(?:the\s+fol+\w*\s+information)?\s*:?\s*"
    r"|We\s+denied\s+the\s+medical\s+se[rv]{0,3}ices?(?:/items)?\s+"
    r"(?:listed\s+below\s+)?because\s*)"
    r"(.+?)(?=You\s+may\s+file|Decision\s*:|Appeal|Share\s+a\s+copy|Page\s+\d|$)",
    re.I | re.S,
)
PORTAL_CASE_DENIED = re.compile(
    r"Overall\s+case\s+status\s*:\s*Denied\b(.{0,400})?",
    re.I | re.S,
)
PORTAL_REQUEST_DENIED = re.compile(
    r"(Your\s+request\s+has\s+been\s+denied[^\n.]{0,250})",
    re.I,
)
MEDICAL_NEC_NOT_MET = re.compile(
    r"((?:DECISION\s+STATUS\s+REASON\s+)?Medical\s+necessity\s+not\s+met[^\n]{0,120})",
    re.I,
)
REJECTION_NOTICE = re.compile(
    r"Service\s+Request\s+Rejection\s+Notification(.{0,500}?)(?="
    r"Page\s+\d|Member\s+Name|$)",
    re.I | re.S,
)
FUZZY_MED_NEC = re.compile(
    r"((?:not\s+medica(?:lly|l)?\s*necessar\w*|nat\s+medea\s+necessary|"
    r"nat\s+med(?:ica)?l?\s*necessar\w*)[^\n.]{0,120})",
    re.I,
)
WHY_AM_GETTING = re.compile(
    r"Why\s+am\.?\s*I?\s*getting\s+this\s+notice\??\s*(.+?)(?="
    r"Why\s+did\s+we|What\s+should|How\s+to\s+ask|Page\s+\d|$)",
    re.I | re.S,
)
DENIED_PAYMENT_SERVICE = re.compile(
    r"((?:denied\s+payment\s+for\s+(?:these\s+)?service|"
    r"denied\s+(?:this\s+)?(?:service|request|the\s+service))[^\n.]{0,200})",
    re.I,
)
MTG_DENIED = re.compile(
    r"(claim\s+administrator\s+has\s+denied.{0,400})",
    re.I | re.S,
)
DECIDED_ON = re.compile(
    r"On\s+(\d{1,2}/\d{1,2}/\d{2,4}|"
    r"[A-Za-z]+\s+\d{1,2},?\s*\d{4})"
    r".{0,100}?decided\s+to\s+(?:deny|partially\s+approv)",
    re.I | re.S,
)
NO_LATER_THAN = re.compile(
    r"no\s+later\s+than\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)
PARTIAL_APPROVE_DATE = re.compile(
    r"PARTIAL\s+APPROVE\s+(\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)
INITIAL_DET = re.compile(
    r"Initial\s+Determination\s*Date\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})",
    re.I,
)
CLINICAL_RATIONALE = re.compile(
    r"Clinical\s+Rationale\s*(.+?)(?=Outcome\s*/\s*Reason|Page\s+\d|$)",
    re.I | re.S,
)
PARTIAL_APPROVAL = re.compile(
    r"Partial(?:ly)?\s+Approv|Status\s*:\s*Partial",
    re.I,
)
NOT_A_DENIAL = re.compile(
    r"Referral\s+Authorized|Referral\s+Reason\s*:|"
    r"To\s+Whom\s+It\s+May\s+Concern|"
    r"Member\s+prior\s+authorizations|"
    r"\bAPPROVED\b(?!.*den(?:y|ied|ial))",
    re.I,
)


@dataclass
class DenialRow:
    patient_id: str = ""
    filename: str = ""
    path: str = ""
    extraction_method: str = ""
    denial_date: str = ""
    insurance_name: str = ""
    payer_guess: str = ""
    reason_raw: str = ""
    reason_class: str = "unparseable"
    error: str = ""


def _native_text_preview(path: Path, max_pages: int = 2) -> str:
    try:
        doc = fitz.open(path)
        try:
            chunks: list[str] = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    break
                chunks.append(page.get_text() or "")
            return "\n".join(chunks)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001
        return ""


def discover_denial_pdfs(
    edocs_dir: Path,
    *,
    content_scan: bool = True,
) -> list[Path]:
    found: dict[str, Path] = {}

    for path in sorted(edocs_dir.rglob("*.pdf")):
        if "chart_notes" in path.parts:
            continue
        name = path.name
        key = str(path.resolve()).lower()

        if FILENAME_DENIAL.search(name):
            found[key] = path
            continue

        # Skip obvious approval-only filenames unless content says denial
        approval_name = bool(FILENAME_APPROVAL_ONLY.search(name)) and not FILENAME_DENIAL.search(
            name
        )
        if not content_scan:
            continue

        preview = _native_text_preview(path)
        if not preview.strip():
            continue
        if CONTENT_DENIAL_MARKERS.search(preview):
            # Avoid pulling pure approval letters that mention LCD "Determination"
            if approval_name and not re.search(
                r"den(?:y|ied|ial)|not\s+approved|non[-\s]*authorized|"
                r"adverse\s+determination|denial\s+notice",
                preview,
                re.I,
            ):
                continue
            found[key] = path

    return sorted(found.values(), key=lambda p: (p.parent.name, p.name.lower()))


def patient_id_from_path(path: Path, edocs_dir: Path) -> str:
    try:
        rel = path.relative_to(edocs_dir)
        return rel.parts[0] if rel.parts else ""
    except ValueError:
        return path.parent.name


def _normalize_ws(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text or "").strip()


def clean_ocr_text(text: str) -> str:
    """Normalize noisy OCR before regex extraction."""
    if not text:
        return ""
    # Drop lone pipe / bullet junk lines common in scanned letters
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue
        if re.fullmatch(r"[|Iil.\-•·\s]{1,6}", stripped):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    cleaned = re.sub(r"[|]+", " ", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned


def _collapse(text: str, max_len: int = 800) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) > max_len:
        return cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def text_quality_score(text: str) -> float:
    """Higher is better; garbled/rotated OCR scores low."""
    if not text or not text.strip():
        return 0.0
    sample = text[:4000]
    letters = sum(1 for c in sample if c.isalpha())
    spaces = sum(1 for c in sample if c.isspace())
    total = len(sample)
    if total < 40:
        return 0.05
    alpha_ratio = letters / total
    # Common English denial words
    hits = sum(
        1
        for w in (
            "denial",
            "denied",
            "coverage",
            "therapy",
            "patient",
            "provider",
            "medical",
            "necessary",
            "request",
            "appeal",
        )
        if w in sample.lower()
    )
    return alpha_ratio * 2.0 + hits * 0.35 + min(spaces / total, 0.25)


def looks_garbled(text: str) -> bool:
    return text_quality_score(text) < 1.2


def _valid_denial_year(year: int) -> bool:
    if year < 100:
        year += 2000 if year < 70 else 1900
    return 2020 <= year <= datetime.now().year + 1


def _to_iso(month: int, day: int, year: int) -> str | None:
    if year < 100:
        year += 2000 if year < 70 else 1900
    if not _valid_denial_year(year):
        return None
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def parse_date_token(token: str) -> str | None:
    token = (token or "").strip()
    if not token:
        return None
    m = DATE_SLASH.search(token)
    if m:
        return _to_iso(int(m.group("m")), int(m.group("d")), int(m.group("y")))
    m = DATE_NAMED.search(token)
    if m:
        mon = MONTH_NAME.get(m.group("mon").lower())
        if mon:
            return _to_iso(mon, int(m.group("d")), int(m.group("y")))
    return None


def date_from_filename(filename: str) -> str:
    m = FILENAME_DATE.search(filename or "")
    if not m:
        return ""
    if m.group("y"):
        return _to_iso(int(m.group("m")), int(m.group("d")), int(m.group("y"))) or ""
    return _to_iso(int(m.group("m2")), int(m.group("d2")), int(m.group("y2"))) or ""


def extract_denial_date(text: str, filename: str = "") -> str:
    m = DECIDED_ON.search(text)
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    m = INITIAL_DET.search(text)
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    m = NO_LATER_THAN.search(text)
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    # WCB reviewer / statement date (avoid Date of Injury)
    m = re.search(
        r"(?:Reviewer[^\n]{0,80}Date\s*|Date\s+)(\d{1,2}/\d{1,2}/\d{4})"
        r"(?=\s*(?:STATEMENT|Reviewer\s+Title))",
        text,
        re.I,
    )
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    m = PARTIAL_APPROVE_DATE.search(text)
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    m = re.search(
        r"(?:Notice\s+Sent|Date\s*:\s*|Letter\s+Date\s*:?\s*|"
        r"Your\s+Indicated\s+Start\s+Date|"
        r"Submitted\s+Initial\s+Date\s*:?\s*)"
        r"(\d{1,2}/\d{1,2}/\d{2,4}|"
        r"(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December)\s+\d{1,2},?\s*\d{4})",
        text[:3500],
        re.I,
    )
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    # UHC table: date above "Not Approved"
    m = re.search(
        r"(\d{1,2}/\d{1,2}/\d{2,4})\s*\n?\s*Not\s+Approved",
        text,
        re.I,
    )
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    # After DENIAL NOTICE / ADVERSE DETERMINATION: named or slash date
    m = re.search(
        r"(?:INITIAL\s+ADVERSE\s+DETERMINATION|DENIAL\s+NOTICE).{0,250}?"
        r"("
        + DATE_NAMED.pattern
        + r"|"
        + r"\d{1,2}/\d{1,2}/\d{2,4}"
        + r")",
        text,
        re.I | re.S,
    )
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    # Standalone denial-notice date line often OCR'd near member name
    m = re.search(
        r"DENIAL\s+NOTICE[^\n]{0,40}\n[^\n]{0,20}"
        r"(\d{1,2}/\d{1,2}/\d{2,4})",
        text,
        re.I,
    )
    if m:
        iso = parse_date_token(m.group(1))
        if iso:
            return iso

    head_lines = text[:900].splitlines()
    for line in head_lines:
        if re.search(r"birth|dob|born", line, re.I):
            continue
        for pattern in (DATE_NAMED, DATE_SLASH):
            m = pattern.search(line)
            if m:
                iso = parse_date_token(m.group(0))
                if iso:
                    return iso

    # Filename fallback (e.g. DO-01-6732-520_03-24-2026-18-40.pdf)
    return date_from_filename(filename)


def _looks_like_person_name(name: str) -> bool:
    """True when value is likely a patient/person, not a payer plan."""
    if INSURANCE_KEYWORD.search(name or ""):
        return False
    parts = [p for p in re.split(r"[\s,]+", (name or "").strip()) if p]
    if not (1 <= len(parts) <= 4):
        return False
    # Mostly alphabetic tokens (allow apostrophe / hyphen)
    return all(re.fullmatch(r"[A-Za-z][A-Za-z'\-]{1,30}", p) for p in parts)


def _clean_insurance(name: str) -> str:
    name = _normalize_ws(name.replace("\n", " "))
    name = re.sub(r"\s{2,}", " ", name).strip(" .-:")
    name = re.sub(r"^\d{3,}\s+", "", name)
    name = re.split(
        r"\b(?:Spoken|Written|Provider|Member|Service|Your\s+Indicated|"
        r"Support\s+Clinician|Submitted|CPT)\b",
        name,
        maxsplit=1,
        flags=re.I,
    )[0].strip(" .-")
    low = name.lower()
    if low in {"as denied", "denied", "coverage"} or len(name) < 4:
        return ""
    if JUNK_INSURANCE.search(name):
        return ""
    if _looks_like_person_name(name):
        return ""
    if len(name) > 120:
        name = name[:120].rstrip()
    return name


def extract_insurance_name(text: str) -> str:
    for pattern in (COVERAGE_TYPE, HEALTH_PLAN):
        m = pattern.search(text)
        if m:
            cleaned = _clean_insurance(m.group(1))
            if cleaned:
                return cleaned

    m = NAMED_PLAN.search(text[:4000])
    if m:
        cleaned = _clean_insurance(m.group(1))
        if cleaned:
            return cleaned

    for label, pat in PAYER_PATTERNS:
        if label in ("medicaid", "medicare"):
            continue
        m = pat.search(text[:3000])
        if m:
            start = max(0, m.start())
            end = min(len(text), m.end() + 50)
            snippet = _clean_insurance(text[start:end].split("\n")[0])
            if snippet:
                return snippet[:80]
            # Pattern hit but line cleanup failed — use canonical label phrase
            if label == "wellcare":
                wm = re.search(r"WellCare[^\n,]{0,40}", text[:3000], re.I)
                if wm:
                    return _clean_insurance(wm.group(0)) or "WellCare"
            if label == "empire":
                return "The New York State Empire Plan"
            if label == "united":
                return "UnitedHealthcare"
            if label == "mltc":
                return "MLTC"
    return ""


def guess_payer(insurance_name: str, text: str) -> str:
    blob = f"{insurance_name}\n{text[:3000]}"
    ins = insurance_name or ""
    if re.search(r"well\s*care|wellcare", ins, re.I):
        return "wellcare"
    if re.search(r"\bmltc\b", ins, re.I):
        return "mltc"
    if re.search(r"empire", ins, re.I):
        return "empire"
    if re.search(r"fidelis", blob, re.I):
        return "fidelis"
    if re.search(r"optum|united\s*healthcare", blob, re.I):
        return "united"
    if re.search(r"empire\s+plan|managed\s+physical\s+network|\bmpn\b", blob, re.I):
        return "empire"
    for name, pat in PAYER_PATTERNS:
        if pat.search(blob):
            return name
    return "other" if (insurance_name or text.strip()) else ""


def is_not_a_denial(text: str, filename: str = "") -> bool:
    low = (text or "")[:3500].lower()
    if CONTENT_DENIAL_MARKERS.search(text or ""):
        return False
    if re.search(
        r"coverage for ongoing physical therapy is denied|"
        r"not approved|has been denied|services have been denied|"
        r"why did we decide to deny",
        low,
    ):
        return False
    # Referral / portal screenshots mislabeled as denial
    if re.search(r"referral authorized|referral reason\s*:", low):
        return True
    if re.search(r"member prior authorizations", low) and "denied" not in low:
        return True
    if FILENAME_APPROVAL_ONLY.search(filename or "") and "denied" not in low:
        if re.search(r"\bapproved\b|\bapproval\b", low) and "denial" not in low:
            return True
    return False


def extract_reason_raw(text: str) -> str:
    cleaned = clean_ocr_text(text)

    m = WHY_DENY.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = UHC_DENIED.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = PACE_DENIED.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = MTG_DENIED.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = re.search(
        r"Why\s+was\s+coverage\s+denied\?\s*(.+?)(?="
        r"What\s+should|How\s+to\s+ask|You\s+have\s+the\s+right|Page\s+\d|$)",
        cleaned,
        re.I | re.S,
    )
    if m:
        return _collapse(m.group(1))

    m = DENIED_BECAUSE.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = CLINICAL_RATIONALE.search(cleaned)
    if m:
        return _collapse(m.group(1))

    # Portal / UM screenshots
    m = MEDICAL_NEC_NOT_MET.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = PORTAL_REQUEST_DENIED.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = PORTAL_CASE_DENIED.search(cleaned)
    if m:
        extra = (m.group(1) or "").strip()
        base = "Overall case status: Denied"
        return _collapse(f"{base}. {extra}" if extra else base)

    m = REJECTION_NOTICE.search(cleaned)
    if m:
        return _collapse(
            "Service Request Rejection Notification: " + (m.group(1) or "")
        )

    # MLTC / IAD OCR: "Why am getting this notice" then maybe thin body
    m = WHY_AM_GETTING.search(cleaned)
    if m and len(_collapse(m.group(1))) > 40:
        return _collapse(m.group(1))

    m = DENIED_PAYMENT_SERVICE.search(cleaned)
    if m:
        return _collapse(m.group(1))

    if re.search(r"Non[-\s]*Authorized\s*/\s*Criteria\s*Not\s*Met", cleaned, re.I):
        return "Non-Authorized / Criteria Not Met"
    if re.search(r"Requested\s+services\s+have\s+been\s+denied", cleaned, re.I):
        return "Requested services have been denied for this Physical Therapy request."
    if re.search(r"Not\s+Approved", cleaned, re.I) and re.search(
        r"physical\s+therapy", cleaned, re.I
    ):
        m = re.search(
            r"(Coverage\s+for\s+ongoing\s+physical\s+therapy\s+is\s+denied[^\n]*)",
            cleaned,
            re.I,
        )
        if m:
            return _collapse(m.group(1))

    m = re.search(r"(not\s+medically\s+necessary[^.]*\.)", cleaned, re.I)
    if m:
        return _collapse(m.group(1))

    m = FUZZY_MED_NEC.search(cleaned)
    if m:
        return _collapse(m.group(1))

    m = re.search(r"(Criteria\s+Not\s+Met[^\n]{0,200})", cleaned, re.I)
    if m:
        return _collapse(m.group(1))

    return ""


def classify_reason(reason_raw: str, full_text: str = "", filename: str = "") -> str:
    if is_not_a_denial(full_text, filename) and not (reason_raw or "").strip():
        return "not_a_denial"

    reason = (reason_raw or "").lower()
    head = clean_ocr_text(full_text or "")[:5000].lower()
    has_partial = bool(PARTIAL_APPROVAL.search(full_text or "")) or (
        "partially approved" in head
    )
    if not reason.strip() and not has_partial:
        if re.search(r"non[-\s]*authorized", head) and re.search(
            r"criteria\s+not\s+met", head
        ):
            return "criteria_not_met"
        if "requested services have been denied" in head:
            return "criteria_not_met"
        if "medical necessity not met" in head:
            return "medical_necessity"
        if "overall case status" in head and "denied" in head:
            return "medical_necessity"
        if "your request has been denied" in head:
            return "medical_necessity"
        if "not approved" in head and "physical therapy" in head:
            return "skilled_care_not_supported"
        if FUZZY_MED_NEC.search(head):
            return "medical_necessity"
        if is_not_a_denial(full_text, filename):
            return "not_a_denial"
        return "unparseable"

    if has_partial and "denied" not in reason and "rejection" not in reason:
        # Don't override clear denials as partial just because MLTC says Partial Capitation
        if "partial capitation" not in head or "partially approv" in head:
            if "partially approv" in head or "partial approval" in head:
                return "partial_approval"

    blob = reason if reason.strip() else head

    if "rejection notification" in blob or "service request rejection" in reason:
        if any(
            k in head
            for k in (
                "continuity of care",
                "already obtained",
                "authorizations you have already",
            )
        ):
            return "already_authorized"

    rules: list[tuple[str, list[str]]] = [
        (
            "already_authorized",
            [
                "current approval",
                "already approved",
                "existing authorization",
                "already authorized",
                "active authorization",
                "authorizations you have already obtained",
                "continuity of care",
            ],
        ),
        (
            "skilled_care_not_supported",
            [
                "skilled plan of care",
                "do not support a skilled",
                "not support a skilled",
                "why a skilled therapist is needed",
                "goals do not support the skilled",
                "needs care from a skilled therapist",
                "non-skilled personnel",
                "do not support ongoing care",
            ],
        ),
        (
            "documentation_insufficient",
            [
                "documentation standards",
                "record keeping",
                "notes do not show",
                "notes do not",
                "medical information sent",
                "insufficient documentation",
                "clinical notes sent",
                "records must show",
                "records we reviewed do not support",
                "documentation submitted",
                "signed prescription",
                "upload the following documentation",
            ],
        ),
        (
            "visit_limit_or_benefit",
            [
                "benefit maximum",
                "benefit limit",
                "visit limit",
                "maximum visits",
                "exhausted",
                "benefit has been",
            ],
        ),
        (
            "partial_approval",
            ["partial approval", "partially approved", "partially approv", "granted in part"],
        ),
        (
            "criteria_not_met",
            [
                "criteria not met",
                "criteria are not met",
                "did not meet",
                "do not meet the criteria",
                "clinical criteria",
                "guideline",
                "non-authorized",
                "not covered",
                "non-covered",
                "lcd)",
                "local coverage determination",
                "rejection notification",
                "resubmit your request",
            ],
        ),
        (
            "medical_necessity",
            [
                "not medically necessary",
                "medically necessary",
                "medical necessity",
                "not medically needed",
                "medea necessary",
                "necessar",
                "case status: denied",
                "request has been denied",
            ],
        ),
    ]

    for class_name, keywords in rules:
        if any(k in blob for k in keywords):
            return class_name

    if reason.strip():
        if "have been denied" in reason or "services have been denied" in reason:
            if "carelon" in head or re.search(r"criteria\s+not\s+met", head):
                return "criteria_not_met"
        if "denied" in reason or "rejection" in reason:
            return "other"
        return "other"
    return "unparseable"


def cache_path_for(pdf: Path, cache_dir: Path) -> Path:
    digest = hashlib.sha1(str(pdf.resolve()).encode("utf-8")).hexdigest()[:16]
    safe = re.sub(r"[^\w.\-]+", "_", pdf.stem)[:60]
    return cache_dir / f"{pdf.parent.name}_{safe}_{digest}.txt"


def _ocr_page_rotations(pdf: Path, *, dpi: int = 300) -> tuple[str, str]:
    """OCR each page at 0/90/180/270 and keep the best-scoring rotation."""
    exe, tessdata = resolve_tesseract_paths()
    if tessdata:
        import os

        os.environ.setdefault("TESSDATA_PREFIX", tessdata)

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return extract_pdf_text(pdf, dpi=dpi, force_ocr=True)

    if exe:
        pytesseract.pytesseract.tesseract_cmd = exe

    doc = fitz.open(pdf)
    page_texts: list[str] = []
    try:
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        for page in doc:
            best_text = ""
            best_score = -1.0
            # Try native first for this page
            native = (page.get_text() or "").strip()
            if len(native) >= 50 and not looks_garbled(native):
                page_texts.append(native)
                continue

            pix = page.get_pixmap(matrix=matrix, alpha=False)
            base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            for angle in (0, 90, 180, 270):
                img = base if angle == 0 else base.rotate(angle, expand=True)
                try:
                    candidate = (pytesseract.image_to_string(img) or "").strip()
                except Exception:  # noqa: BLE001
                    candidate = ""
                score = text_quality_score(candidate)
                if score > best_score:
                    best_score = score
                    best_text = candidate
            if best_text:
                page_texts.append(best_text)
            elif native:
                page_texts.append(native)
    finally:
        doc.close()

    text = "\n".join(page_texts)
    return text, "ocr_rotated"


def load_or_extract_text(
    pdf: Path,
    *,
    cache_dir: Path,
    force_ocr: bool,
    dpi: int,
    refresh: bool = False,
) -> tuple[str, str]:
    cache_file = cache_path_for(pdf, cache_dir)
    meta_file = cache_file.with_suffix(".meta.json")

    if cache_file.exists() and not force_ocr and not refresh:
        text = cache_file.read_text(encoding="utf-8", errors="replace")
        method = "cached"
        if meta_file.exists():
            try:
                method = json.loads(meta_file.read_text(encoding="utf-8")).get(
                    "method", "cached"
                )
            except json.JSONDecodeError:
                pass
        if text.strip() and not looks_garbled(text):
            return text, method
        # Fall through to re-extract if cached text is garbled

    text, method = extract_pdf_text(pdf, dpi=dpi, force_ocr=force_ocr)

    if looks_garbled(text) or not text.strip():
        rotated, rot_method = _ocr_page_rotations(pdf, dpi=max(dpi, 300))
        if text_quality_score(rotated) > text_quality_score(text):
            text, method = rotated, rot_method

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(text, encoding="utf-8", errors="replace")
    meta_file.write_text(
        json.dumps({"method": method, "source": str(pdf)}, indent=2),
        encoding="utf-8",
    )
    return text, method


def parse_denial_text(
    text: str, filename: str = ""
) -> tuple[str, str, str, str, str]:
    """Return denial_date, insurance_name, payer_guess, reason_raw, reason_class."""
    if not (text or "").strip():
        return "", "", "", "", "unparseable"

    cleaned = clean_ocr_text(text)
    if is_not_a_denial(cleaned, filename):
        # Still try to extract if reason exists
        reason_raw = extract_reason_raw(cleaned)
        if not reason_raw:
            return (
                extract_denial_date(cleaned, filename),
                extract_insurance_name(cleaned),
                guess_payer("", cleaned),
                "",
                "not_a_denial",
            )

    denial_date = extract_denial_date(cleaned, filename)
    insurance_name = extract_insurance_name(cleaned)
    payer_guess = guess_payer(insurance_name, cleaned)
    reason_raw = extract_reason_raw(cleaned)
    reason_class = classify_reason(reason_raw, cleaned, filename)
    return denial_date, insurance_name, payer_guess, reason_raw, reason_class


def process_pdf(
    pdf: Path,
    *,
    edocs_dir: Path,
    cache_dir: Path,
    force_ocr: bool,
    dpi: int,
    refresh: bool = False,
) -> DenialRow:
    row = DenialRow(
        patient_id=patient_id_from_path(pdf, edocs_dir),
        filename=pdf.name,
        path=str(pdf),
    )
    try:
        text, method = load_or_extract_text(
            pdf,
            cache_dir=cache_dir,
            force_ocr=force_ocr,
            dpi=dpi,
            refresh=refresh,
        )
        row.extraction_method = method
        (
            row.denial_date,
            row.insurance_name,
            row.payer_guess,
            row.reason_raw,
            row.reason_class,
        ) = parse_denial_text(text, pdf.name)
        if not text.strip():
            row.error = "empty_text"
            row.reason_class = "unparseable"
        elif looks_garbled(text) and row.reason_class == "unparseable":
            row.error = "garbled_ocr"
    except Exception as exc:  # noqa: BLE001 — batch resilience
        row.error = str(exc)
        row.reason_class = "unparseable"
    return row


def write_outputs(rows: list[DenialRow], out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "denial_reasons.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))

    by_class = Counter(r.reason_class for r in rows)
    by_payer = Counter(r.payer_guess or "(blank)" for r in rows)
    by_month = Counter(
        (r.denial_date[:7] if r.denial_date else "(unknown)") for r in rows
    )
    summary = {
        "total": len(rows),
        "with_date": sum(1 for r in rows if r.denial_date),
        "with_insurance": sum(1 for r in rows if r.insurance_name),
        "with_reason": sum(1 for r in rows if r.reason_raw),
        "errors": sum(1 for r in rows if r.error),
        "unparseable": by_class.get("unparseable", 0),
        "not_a_denial": by_class.get("not_a_denial", 0),
        "by_reason_class": dict(by_class.most_common()),
        "by_payer_guess": dict(by_payer.most_common()),
        "by_month": dict(sorted(by_month.items())),
        "csv": str(csv_path),
    }
    summary_path = out_dir / "denial_reasons_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    summary["summary_json"] = str(summary_path)
    return summary


def _previous_unparseable_paths(out_dir: Path) -> set[str]:
    csv_path = out_dir / "denial_reasons.csv"
    if not csv_path.exists():
        return set()
    paths: set[str] = set()
    with csv_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("reason_class") == "unparseable":
                paths.add(row.get("path") or "")
    return paths


def reparse_from_csv(
    out_dir: Path,
    edocs_dir: Path | None = None,
    *,
    force_ocr: bool = False,
    dpi: int = 200,
) -> dict:
    """Re-parse existing CSV rows from OCR cache (no rediscovery)."""
    out_dir = out_dir.resolve()
    csv_path = out_dir / "denial_reasons.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")
    cache_dir = out_dir / "denial_ocr_cache"
    if edocs_dir is None:
        edocs_dir = out_dir.parent / "edocs"
    edocs_dir = edocs_dir.resolve()

    with csv_path.open(encoding="utf-8", newline="") as fh:
        old_rows = list(csv.DictReader(fh))

    rows: list[DenialRow] = []
    for i, old in enumerate(old_rows, 1):
        pdf = Path(old["path"])
        print(f"[{i}/{len(old_rows)}] reparse {pdf.parent.name}/{pdf.name}")
        if not pdf.exists():
            row = DenialRow(
                patient_id=old.get("patient_id", ""),
                filename=old.get("filename", pdf.name),
                path=str(pdf),
                extraction_method=old.get("extraction_method", ""),
                error="missing_file",
                reason_class="unparseable",
            )
            rows.append(row)
            continue
        rows.append(
            process_pdf(
                pdf,
                edocs_dir=edocs_dir,
                cache_dir=cache_dir,
                force_ocr=force_ocr,
                dpi=dpi,
                refresh=False,
            )
        )
    return write_outputs(rows, out_dir)


def run(
    edocs_dir: Path,
    out_dir: Path,
    *,
    limit: int | None = None,
    force_ocr: bool = False,
    dpi: int = 200,
    content_scan: bool = True,
    refresh_unparseable: bool = False,
) -> dict:
    edocs_dir = edocs_dir.resolve()
    out_dir = out_dir.resolve()
    cache_dir = out_dir / "denial_ocr_cache"

    prev_unparseable = (
        _previous_unparseable_paths(out_dir) if refresh_unparseable else set()
    )

    print("Discovering denial PDFs (filename + content markers)...")
    pdfs = discover_denial_pdfs(edocs_dir, content_scan=content_scan)
    print(f"Found {len(pdfs)} candidate denial PDFs")
    if limit is not None:
        pdfs = pdfs[:limit]

    rows: list[DenialRow] = []
    for i, pdf in enumerate(pdfs, 1):
        refresh = refresh_unparseable and str(pdf) in prev_unparseable
        print(f"[{i}/{len(pdfs)}] {pdf.parent.name}/{pdf.name}")
        rows.append(
            process_pdf(
                pdf,
                edocs_dir=edocs_dir,
                cache_dir=cache_dir,
                force_ocr=force_ocr,
                dpi=dpi,
                refresh=refresh or force_ocr,
            )
        )

    return write_outputs(rows, out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract and classify denial reasons from WebPT eDoc PDFs"
    )
    parser.add_argument(
        "--edocs-dir",
        type=Path,
        required=True,
        help="Path to edocs folder (…/edocs/{patient_id}/*.pdf)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for denial_reasons.csv and summary JSON",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N denial PDFs (smoke test)",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Force OCR even when native PDF text exists",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=200,
        help="OCR DPI (default 200)",
    )
    parser.add_argument(
        "--no-content-scan",
        action="store_true",
        help="Only use filename patterns (skip native-text content discovery)",
    )
    parser.add_argument(
        "--refresh-unparseable",
        action="store_true",
        help="Re-extract text for paths that were unparseable in the previous CSV",
    )
    parser.add_argument(
        "--reparse-csv",
        action="store_true",
        help="Re-parse existing denial_reasons.csv from OCR cache (skip discovery)",
    )
    args = parser.parse_args()

    if not args.edocs_dir.is_dir() and not args.reparse_csv:
        raise SystemExit(f"edocs dir not found: {args.edocs_dir}")

    if args.reparse_csv:
        summary = reparse_from_csv(
            args.out,
            edocs_dir=args.edocs_dir if args.edocs_dir.is_dir() else None,
            force_ocr=args.force_ocr,
            dpi=args.dpi,
        )
    else:
        summary = run(
            args.edocs_dir,
            args.out,
            limit=args.limit,
            force_ocr=args.force_ocr,
            dpi=args.dpi,
            content_scan=not args.no_content_scan,
            refresh_unparseable=args.refresh_unparseable,
        )
    print(
        f"Processed {summary['total']} denials | "
        f"date={summary['with_date']} insurance={summary['with_insurance']} "
        f"reason={summary['with_reason']} errors={summary['errors']} "
        f"unparseable={summary['unparseable']} not_a_denial={summary['not_a_denial']}"
    )
    print("by_reason_class:", summary["by_reason_class"])
    print("by_payer_guess:", summary["by_payer_guess"])
    print(f"CSV: {summary['csv']}")
    print(f"Summary: {summary['summary_json']}")


if __name__ == "__main__":
    main()

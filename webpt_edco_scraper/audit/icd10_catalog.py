"""ICD-10-CM catalog loader (CMS Code Descriptions / Order file)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

AUDIT_DIR = Path(__file__).resolve().parent
DEFAULT_CATALOG_PATH = AUDIT_DIR / "data" / "icd10cm_catalog.csv"


@dataclass(frozen=True)
class IcdCode:
    code: str
    description: str
    billable: bool
    chapter: str


def format_icd10_code(raw: str) -> str:
    """Insert the decimal after the 3rd character when CMS omits dots."""
    code = (raw or "").strip().upper().replace(".", "")
    if len(code) <= 3:
        return code
    return f"{code[:3]}.{code[3:]}"


def chapter_for(code: str) -> str:
    return (code or "")[:1].upper()


class Icd10Catalog:
    def __init__(self, by_code: dict[str, IcdCode]):
        self._by_code = by_code

    def __len__(self) -> int:
        return len(self._by_code)

    def get(self, code: str) -> IcdCode | None:
        key = (code or "").strip().upper()
        if not key:
            return None
        hit = self._by_code.get(key)
        if hit is not None:
            return hit
        # Tolerate missing dots.
        return self._by_code.get(format_icd10_code(key))

    def is_known(self, code: str) -> bool:
        return self.get(code) is not None

    def is_billable(self, code: str) -> bool:
        entry = self.get(code)
        return bool(entry and entry.billable)

    def invalid_or_nonbillable(self, codes: set[str]) -> list[str]:
        """Return codes that are missing from the catalog or non-billable headers."""
        bad: list[str] = []
        for code in sorted(codes):
            entry = self.get(code)
            if entry is None or not entry.billable:
                bad.append(code)
        return bad


def parse_order_line(line: str) -> IcdCode | None:
    """Parse one CMS icd10cm_order_YYYY.txt fixed-width line."""
    if not line or len(line) < 16:
        return None
    raw_code = line[6:13].strip()
    flag = line[14:15]
    if not raw_code or flag not in {"0", "1"}:
        return None
    # Short description starts at 16; long description follows after padding.
    rest = line[16:].rstrip()
    # CMS pads short desc to ~60 chars then appends long desc.
    if len(rest) > 60:
        short = rest[:60].rstrip()
        long_desc = rest[60:].strip() or short
    else:
        short = rest.strip()
        long_desc = short
    code = format_icd10_code(raw_code)
    return IcdCode(
        code=code,
        description=long_desc or short,
        billable=(flag == "1"),
        chapter=chapter_for(code),
    )


def parse_order_text(text: str) -> dict[str, IcdCode]:
    by_code: dict[str, IcdCode] = {}
    for line in text.splitlines():
        entry = parse_order_line(line)
        if entry is None:
            continue
        by_code[entry.code] = entry
    return by_code


def write_catalog_csv(by_code: dict[str, IcdCode], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["code", "description", "billable", "chapter"]
        )
        writer.writeheader()
        for code in sorted(by_code, key=lambda c: (c.replace(".", ""), c)):
            entry = by_code[code]
            writer.writerow(
                {
                    "code": entry.code,
                    "description": entry.description,
                    "billable": "1" if entry.billable else "0",
                    "chapter": entry.chapter,
                }
            )


def load_catalog(path: Path | None = None) -> Icd10Catalog:
    catalog_path = path or DEFAULT_CATALOG_PATH
    by_code: dict[str, IcdCode] = {}
    with catalog_path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("code") or "").strip().upper()
            if not code:
                continue
            by_code[code] = IcdCode(
                code=code,
                description=(row.get("description") or "").strip(),
                billable=(row.get("billable") or "").strip() in {"1", "true", "True", "yes"},
                chapter=(row.get("chapter") or chapter_for(code)).strip() or chapter_for(code),
            )
    return Icd10Catalog(by_code)


@lru_cache(maxsize=1)
def get_default_catalog() -> Icd10Catalog:
    return load_catalog(DEFAULT_CATALOG_PATH)

"""Download CMS ICD-10-CM order file and write audit/data/icd10cm_catalog.csv.

Usage:
  python scripts/build_icd10_catalog.py
  python scripts/build_icd10_catalog.py --url https://www.cms.gov/files/zip/2026-code-descriptions-tabular-order.zip
"""

from __future__ import annotations

import argparse
import io
import sys
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from audit.icd10_catalog import (  # noqa: E402
    DEFAULT_CATALOG_PATH,
    parse_order_text,
    write_catalog_csv,
)

# April 1, 2026 update — valid for encounters Apr–Sep 2026 (covers jun_jul_2026).
DEFAULT_URL = (
    "https://www.cms.gov/files/zip/april-1-2026-code-descriptions-tabular-order.zip"
)


def download_zip(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; cashflow-icd-catalog/1.0)"})
    with urlopen(req, timeout=180) as resp:
        return resp.read()


def extract_order_text(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        order_names = [
            name
            for name in zf.namelist()
            if name.lower().endswith(".txt") and "order" in name.lower() and "addenda" not in name.lower()
        ]
        if not order_names:
            raise FileNotFoundError(
                "No icd10cm_order_*.txt found in ZIP. Members: "
                + ", ".join(zf.namelist())
            )
        # Prefer the largest order file (full set vs tiny stubs).
        order_names.sort(key=lambda n: zf.getinfo(n).file_size, reverse=True)
        return zf.read(order_names[0]).decode("latin-1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build local ICD-10-CM catalog CSV from CMS")
    parser.add_argument("--url", default=DEFAULT_URL, help="CMS Code Descriptions ZIP URL")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help="Output catalog CSV path",
    )
    args = parser.parse_args()

    print(f"Downloading {args.url} ...")
    zip_bytes = download_zip(args.url)
    print(f"Downloaded {len(zip_bytes):,} bytes")
    text = extract_order_text(zip_bytes)
    by_code = parse_order_text(text)
    write_catalog_csv(by_code, args.out)
    billable = sum(1 for e in by_code.values() if e.billable)
    print(
        f"Wrote {len(by_code):,} codes ({billable:,} billable) -> {args.out}"
    )


if __name__ == "__main__":
    main()

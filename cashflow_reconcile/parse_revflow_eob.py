"""Parse RevFlow Electronic EOB Detail CSV exports."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import (
    format_date,
    join_carcs,
    name_key_from_revflow,
    parse_date,
    parse_money,
    safe_int,
)

DATE_RANGE_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})$"
)


@dataclass
class EobFileMeta:
    source_file: str
    payor: str
    check_eft_num: str
    eob_date: str
    report_from: str
    report_to: str
    eob_key: str = ""
    company_id: str = ""


@dataclass
class PaymentLine:
    revflow_patient_id: str
    first_name: str
    last_name: str
    name_key: str
    date_of_service: str
    cpt_code: str
    modifier: str
    units: int
    billed_amount: float
    allowed_amount: float
    paid_amount: float
    adjustment_amount: float
    deductible_amount: float
    carcs: str
    payor: str
    check_eft_num: str
    eob_date: str
    report_from: str
    report_to: str
    source_file: str
    eob_key: str = ""
    company_id: str = ""


@dataclass
class _RollupBucket:
    revflow_patient_id: str
    first_name: str
    last_name: str
    date_of_service: str
    cpt_code: str
    modifier: str
    units: int = 0
    billed_amount: float = 0.0
    allowed_amount: float = 0.0
    paid_amount: float = 0.0
    adjustment_amount: float = 0.0
    deductible_amount: float = 0.0
    carcs: list[str] = field(default_factory=list)


def parse_header(lines: list[str], source_file: str) -> EobFileMeta | None:
    payor = ""
    check_eft_num = ""
    eob_date = ""
    report_from = ""
    report_to = ""

    stem = Path(source_file).stem
    if " - " in stem:
        payor, check_eft_num = stem.rsplit(" - ", 1)

    for line in lines[:12]:
        text = line.strip()
        if text.startswith("Check Number"):
            check_eft_num = text.replace("Check Number", "", 1).strip() or check_eft_num
        elif text.startswith("EOB Date"):
            eob_date = text.replace("EOB Date", "", 1).strip()
        elif DATE_RANGE_RE.match(text):
            report_from, report_to = DATE_RANGE_RE.match(text).groups()  # type: ignore[union-attr]

    if not payor:
        return None

    return EobFileMeta(
        source_file=source_file,
        payor=payor,
        check_eft_num=check_eft_num,
        eob_date=eob_date,
        report_from=report_from,
        report_to=report_to,
    )


def _header_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if line.startswith("Patient ID,"):
            return index
    return None


def _rollup_key(
    patient_id: str,
    last: str,
    first: str,
    dos: str,
    cpt: str,
    modifier: str,
) -> tuple[str, str, str, str, str, str]:
    return (patient_id, last.upper(), first.upper(), dos, cpt, modifier)


def parse_revflow_csv(path: Path, manifest_meta: dict | None = None) -> list[PaymentLine]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = raw.splitlines()
    meta = parse_header(lines, path.name)
    if meta is None:
        return []

    if manifest_meta:
        meta.eob_key = str(manifest_meta.get("eob_key") or "")
        meta.company_id = str(manifest_meta.get("company_id") or "")
        if manifest_meta.get("payor"):
            meta.payor = str(manifest_meta["payor"])
        if manifest_meta.get("check_eft_num"):
            meta.check_eft_num = str(manifest_meta["check_eft_num"])
        if manifest_meta.get("eob_date"):
            meta.eob_date = str(manifest_meta["eob_date"])

    header_idx = _header_index(lines)
    if header_idx is None:
        return []

    buckets: dict[tuple[str, str, str, str, str, str], _RollupBucket] = {}

    for row in csv.DictReader(lines[header_idx:]):
        first = (row.get("First Name") or "").strip()
        last = (row.get("Last Name") or "").strip()
        if first.lower() == "total" or (not first and not last):
            continue

        patient_id = (row.get("Patient ID") or "").strip()
        dos = parse_date(row.get("Date of Service"))
        dos_str = format_date(dos)
        cpt = (row.get("CPT") or "").strip()
        modifier = (row.get("Mod1") or "").strip()
        units = safe_int(row.get("Units"))
        carc = (row.get("CARC") or "").strip()

        if not dos_str:
            continue

        if cpt and units is not None and units > 0:
            key = _rollup_key(patient_id, last, first, dos_str, cpt, modifier)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = _RollupBucket(
                    revflow_patient_id=patient_id,
                    first_name=first,
                    last_name=last,
                    date_of_service=dos_str,
                    cpt_code=cpt,
                    modifier=modifier,
                    units=units,
                )
                buckets[key] = bucket
            bucket.billed_amount += parse_money(row.get("Billed Amount"))
            bucket.allowed_amount += parse_money(row.get("Allowed Amount"))
            bucket.paid_amount += parse_money(row.get("Paid Amount"))
            bucket.adjustment_amount += parse_money(row.get("Adjustment Amount"))
            bucket.deductible_amount += parse_money(row.get("Deductible Amount"))
            if carc:
                bucket.carcs.append(carc)
            continue

        if carc and (units is None or units == 0):
            attached = False
            if cpt:
                key = _rollup_key(patient_id, last, first, dos_str, cpt, modifier)
                bucket = buckets.get(key)
                if bucket is not None:
                    bucket.adjustment_amount += parse_money(row.get("Adjustment Amount"))
                    bucket.deductible_amount += parse_money(row.get("Deductible Amount"))
                    bucket.carcs.append(carc)
                    attached = True
            if not attached:
                for bucket in buckets.values():
                    if (
                        bucket.revflow_patient_id == patient_id
                        and bucket.last_name.upper() == last.upper()
                        and bucket.first_name.upper() == first.upper()
                        and bucket.date_of_service == dos_str
                        and (not cpt or bucket.cpt_code == cpt)
                    ):
                        bucket.adjustment_amount += parse_money(row.get("Adjustment Amount"))
                        bucket.deductible_amount += parse_money(row.get("Deductible Amount"))
                        bucket.carcs.append(carc)

    results: list[PaymentLine] = []
    for bucket in buckets.values():
        results.append(
            PaymentLine(
                revflow_patient_id=bucket.revflow_patient_id,
                first_name=bucket.first_name,
                last_name=bucket.last_name,
                name_key=name_key_from_revflow(bucket.last_name, bucket.first_name),
                date_of_service=bucket.date_of_service,
                cpt_code=bucket.cpt_code,
                modifier=bucket.modifier,
                units=bucket.units,
                billed_amount=bucket.billed_amount,
                allowed_amount=bucket.allowed_amount,
                paid_amount=bucket.paid_amount,
                adjustment_amount=bucket.adjustment_amount,
                deductible_amount=bucket.deductible_amount,
                carcs=join_carcs(bucket.carcs),
                payor=meta.payor,
                check_eft_num=meta.check_eft_num,
                eob_date=meta.eob_date,
                report_from=meta.report_from,
                report_to=meta.report_to,
                source_file=meta.source_file,
                eob_key=meta.eob_key,
                company_id=meta.company_id,
            )
        )
    return results


def load_manifest(manifest_path: Path | None) -> dict[str, dict]:
    if manifest_path is None or not manifest_path.exists():
        return {}

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_file: dict[str, dict] = {}
    for entry in data.get("exports", []):
        selection = entry.get("selection") or {}
        path_value = str(entry.get("path") or "")
        filename = Path(path_value.replace("\\", "/")).name
        check = str(selection.get("check_eft_num") or "")
        payor = str(selection.get("payor") or "")
        stem = f"{payor} - {check}.csv" if payor and check else filename
        by_file[filename] = selection
        by_file[stem] = selection
    return by_file


def load_all_payments(
    exports_dir: Path,
    manifest_path: Path | None = None,
) -> list[PaymentLine]:
    manifest = load_manifest(manifest_path)
    all_lines: list[PaymentLine] = []
    for path in sorted(exports_dir.glob("*.csv")):
        meta = manifest.get(path.name)
        all_lines.extend(parse_revflow_csv(path, manifest_meta=meta))
    return all_lines


def index_payments(payments: list[PaymentLine]) -> dict[tuple, list[PaymentLine]]:
    grouped: dict[tuple, list[PaymentLine]] = defaultdict(list)
    for line in payments:
        grouped[
            (
                line.name_key,
                line.date_of_service,
                line.cpt_code,
                line.modifier,
            )
        ].append(line)
    return grouped

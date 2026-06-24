import os
import re
from pathlib import Path
from typing import Any

import fitz

from export_utils import empty_ocr_summary
from logging_config import get_logger

log = get_logger("edoc_ocr")

OCR_CACHE_FILENAME = ".ocr_cache.txt"
ICD10_PATTERN = re.compile(r"\b([A-Z]\d{2}(?:\.\d{1,4})?)\b", re.IGNORECASE)
NAME_LABEL_PATTERN = re.compile(
    r"(?:patient\s*name|name\s*of\s*patient|member\s*name|patient)\s*[:\-]\s*"
    r"([A-Za-z][A-Za-z\s,\.'\-]{1,80})",
    re.IGNORECASE,
)
DEFAULT_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)


def resolve_tesseract_paths(tesseract_cmd: str | None = None) -> tuple[str | None, str | None]:
    """Return (tesseract_exe, tessdata_dir) if discoverable."""
    candidates: list[str] = []
    if tesseract_cmd:
        candidates.append(tesseract_cmd)
    env_cmd = os.getenv("WEBPT_TESSERACT_CMD", "").strip()
    if env_cmd:
        candidates.append(env_cmd)
    candidates.extend(DEFAULT_TESSERACT_PATHS)

    exe: str | None = None
    for path in candidates:
        if path and Path(path).exists():
            exe = path
            break

    tessdata_candidates: list[str] = []
    env_prefix = os.getenv("TESSDATA_PREFIX", "").strip()
    if env_prefix:
        tessdata_candidates.append(env_prefix)
    if exe:
        tessdata_candidates.append(str(Path(exe).parent / "tessdata"))
    tessdata_candidates.append(r"C:\Program Files\Tesseract-OCR\tessdata")

    tessdata: str | None = None
    for path in tessdata_candidates:
        if path and Path(path).exists():
            tessdata = path
            break
    return exe, tessdata


def _letters_only(value: str) -> str:
    return re.sub(r"[^A-Za-z]", "", value or "").upper()


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def parse_patient_name_parts(name: str) -> tuple[str, str]:
    cleaned = (name or "").strip()
    if not cleaned:
        return "", ""
    if "," in cleaned:
        last, first = cleaned.split(",", 1)
        return last.strip(), first.strip()
    parts = cleaned.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def parse_icd_codes(diagnosis: str) -> list[str]:
    if not diagnosis:
        return []
    seen: set[str] = set()
    codes: list[str] = []
    for match in ICD10_PATTERN.finditer(diagnosis):
        code = match.group(1).upper()
        if code not in seen:
            seen.add(code)
            codes.append(code)
    return codes


def _configure_tesseract(tesseract_cmd: str | None, tessdata: str | None) -> None:
    if tessdata:
        os.environ.setdefault("TESSDATA_PREFIX", tessdata)


def pdf_to_text(
    path: Path,
    *,
    dpi: int = 200,
    tesseract_cmd: str | None = None,
    tessdata: str | None = None,
) -> str:
    _configure_tesseract(tesseract_cmd, tessdata)
    doc = fitz.open(path)
    chunks: list[str] = []
    try:
        scale = dpi / 72.0
        matrix = fitz.Matrix(scale, scale)
        for page in doc:
            text = (page.get_text() or "").strip()
            if len(text) >= 50:
                chunks.append(text)
                continue
            try:
                kwargs: dict[str, Any] = {"language": "eng"}
                if tessdata:
                    kwargs["tessdata"] = tessdata
                tp = page.get_textpage_ocr(**kwargs)
                ocr_text = (tp.extractText() or "").strip()
                if ocr_text:
                    chunks.append(ocr_text)
                    continue
            except Exception as exc:
                log.debug("PyMuPDF OCR failed on %s page %s: %s", path.name, page.number + 1, exc)

            pix = page.get_pixmap(matrix=matrix, alpha=False)
            try:
                import pytesseract
                from PIL import Image

                if tesseract_cmd:
                    pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                ocr_text = (pytesseract.image_to_string(img) or "").strip()
                if ocr_text:
                    chunks.append(ocr_text)
            except Exception as exc:
                log.warning("Fallback OCR failed on %s page %s: %s", path.name, page.number + 1, exc)
    finally:
        doc.close()
    return "\n".join(chunks)


def ocr_patient_edocs(
    pdf_paths: list[Path],
    *,
    dpi: int = 200,
    tesseract_cmd: str | None = None,
    tessdata: str | None = None,
) -> tuple[str, list[str], list[str]]:
    exe, data_dir = resolve_tesseract_paths(tesseract_cmd)
    if tessdata:
        data_dir = tessdata
    if not data_dir:
        raise RuntimeError(
            "Tesseract tessdata not found. Install Tesseract OCR or set TESSDATA_PREFIX."
        )

    merged: list[str] = []
    used_files: list[str] = []
    errors: list[str] = []
    for path in pdf_paths:
        if not path or not path.exists() or not path.is_file():
            continue
        if path.suffix.lower() != ".pdf":
            continue
        try:
            text = pdf_to_text(
                path,
                dpi=dpi,
                tesseract_cmd=exe,
                tessdata=data_dir,
            )
            used_files.append(path.name)
            if text:
                merged.append(f"--- {path.name} ---\n{text}")
            else:
                errors.append(f"{path.name}: no text extracted")
        except Exception as exc:
            errors.append(f"{path.name}: {exc}")
            log.warning("OCR failed for %s: %s", path, exc)
    return "\n\n".join(merged), used_files, errors


def _cache_path(patient_dir: Path) -> Path:
    return patient_dir / OCR_CACHE_FILENAME


def _cache_valid(cache_file: Path, pdf_paths: list[Path]) -> bool:
    if not cache_file.exists():
        return False
    cache_mtime = cache_file.stat().st_mtime
    for path in pdf_paths:
        if path.exists() and path.stat().st_mtime > cache_mtime:
            return False
    return True


def load_or_run_patient_ocr(
    pdf_paths: list[Path],
    *,
    patient_dir: Path | None = None,
    dpi: int = 200,
    tesseract_cmd: str | None = None,
    force: bool = False,
) -> tuple[str, list[str], list[str]]:
    existing = [p for p in pdf_paths if p.exists() and p.is_file()]
    cache_file = _cache_path(patient_dir) if patient_dir else None
    if cache_file and not force and _cache_valid(cache_file, existing):
        text = cache_file.read_text(encoding="utf-8")
        used = [p.name for p in existing]
        return text, used, []

    text, used_files, errors = ocr_patient_edocs(
        existing,
        dpi=dpi,
        tesseract_cmd=tesseract_cmd,
    )
    if cache_file and text:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(text, encoding="utf-8")
    return text, used_files, errors


def _code_in_ocr(code: str, ocr_text: str) -> bool:
    if code.upper() in {c.upper() for c in parse_icd_codes(ocr_text)}:
        return True
    letter = code[0].upper()
    rest = code[1:]
    if "." in rest:
        main, sub = rest.split(".", 1)
        pattern = rf"{letter}\s*{re.escape(main)}[\s.\-]?\s*{re.escape(sub)}"
    else:
        pattern = rf"{letter}\s*{re.escape(rest)}"
    return bool(re.search(pattern, ocr_text or "", re.IGNORECASE))


def extract_patient_fields(
    ocr_text: str,
    *,
    expected_name: str = "",
    expected_id: str = "",
) -> dict[str, str]:
    last, first = parse_patient_name_parts(expected_name)
    ocr_letters = _letters_only(ocr_text)

    extracted_name = ""
    if last and first and _letters_only(last) in ocr_letters and _letters_only(first) in ocr_letters:
        extracted_name = expected_name
    else:
        label_match = NAME_LABEL_PATTERN.search(ocr_text or "")
        if label_match:
            candidate = re.sub(r"\s+", " ", label_match.group(1)).strip(" ,.")
            if len(_letters_only(candidate)) >= 4:
                extracted_name = candidate
        elif last and _letters_only(last) in ocr_letters:
            extracted_name = last

    extracted_id = ""
    expected_digits = _digits_only(expected_id)
    if expected_digits and expected_digits in _digits_only(ocr_text):
        extracted_id = expected_digits

    extracted_codes = parse_icd_codes(ocr_text or "")
    return {
        "edoc_ocr_name": extracted_name,
        "edoc_ocr_patient_id": extracted_id,
        "edoc_ocr_diagnosis": "; ".join(extracted_codes),
    }


def validate_ocr_fields(
    extracted: dict[str, str],
    *,
    expected_name: str,
    expected_id: str,
    expected_diagnosis: str,
) -> dict[str, str]:
    ocr_text_for_match = extracted.get("_ocr_text", "")
    last, first = parse_patient_name_parts(expected_name)
    ocr_letters = _letters_only(ocr_text_for_match)
    name_match = ""
    if last and first:
        if _letters_only(last) in ocr_letters and _letters_only(first) in ocr_letters:
            name_match = "yes"
        else:
            name_match = "no"
    elif last:
        name_match = "yes" if _letters_only(last) in ocr_letters else "no"

    expected_digits = _digits_only(expected_id)
    id_match = ""
    if expected_digits:
        id_match = "yes" if expected_digits in _digits_only(ocr_text_for_match) else "no"

    expected_codes = parse_icd_codes(expected_diagnosis)
    diagnosis_match = ""
    if expected_codes:
        diagnosis_match = (
            "yes"
            if all(_code_in_ocr(code, ocr_text_for_match) for code in expected_codes)
            else "no"
        )

    return {
        "edoc_ocr_name_match": name_match,
        "edoc_ocr_id_match": id_match,
        "edoc_ocr_diagnosis_match": diagnosis_match,
    }


def analyze_file_contribution(
    pdf_path: Path,
    *,
    expected_name: str = "",
    expected_id: str = "",
    expected_diagnosis: str = "",
    dpi: int = 200,
    tesseract_cmd: str | None = None,
) -> dict[str, Any]:
    """OCR a single PDF and report which expected fields appear in it."""
    last, first = parse_patient_name_parts(expected_name)
    expected_digits = _digits_only(expected_id)
    expected_codes = parse_icd_codes(expected_diagnosis)
    result: dict[str, Any] = {
        "filename": pdf_path.name,
        "has_last_name": "",
        "has_first_name": "",
        "has_emr_id": "",
        "icd_codes": "",
        "has_expected_icd": "",
        "ocr_chars": 0,
        "error": "",
    }
    if not pdf_path.exists():
        result["error"] = "file missing"
        return result

    exe, tessdata = resolve_tesseract_paths(tesseract_cmd)
    if not tessdata:
        result["error"] = "tesseract not available"
        return result

    try:
        text = pdf_to_text(
            pdf_path,
            dpi=dpi,
            tesseract_cmd=exe,
            tessdata=tessdata,
        )
    except Exception as exc:
        result["error"] = str(exc)
        return result

    result["ocr_chars"] = len(text)
    if not text.strip():
        result["error"] = "no text extracted"
        return result

    ocr_letters = _letters_only(text)
    if last:
        result["has_last_name"] = "yes" if _letters_only(last) in ocr_letters else "no"
    if first:
        result["has_first_name"] = "yes" if _letters_only(first) in ocr_letters else "no"
    if expected_digits:
        result["has_emr_id"] = "yes" if expected_digits in _digits_only(text) else "no"

    found_codes = parse_icd_codes(text)
    result["icd_codes"] = "; ".join(found_codes)
    if expected_codes:
        matched = [code for code in expected_codes if _code_in_ocr(code, text)]
        if matched and len(matched) == len(expected_codes):
            result["has_expected_icd"] = "yes"
        elif matched:
            result["has_expected_icd"] = "partial"
        else:
            result["has_expected_icd"] = "no"
    elif found_codes:
        result["has_expected_icd"] = "found_other"

    return result


def analyze_patient_file_contributions(
    pdf_paths: list[Path],
    *,
    expected_name: str = "",
    expected_id: str = "",
    expected_diagnosis: str = "",
    dpi: int = 200,
    tesseract_cmd: str | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(pdf_paths):
        if not path.exists() or path.suffix.lower() != ".pdf":
            continue
        rows.append(
            analyze_file_contribution(
                path,
                expected_name=expected_name,
                expected_id=expected_id,
                expected_diagnosis=expected_diagnosis,
                dpi=dpi,
                tesseract_cmd=tesseract_cmd,
            )
        )
    return rows


def format_file_hints(contributions: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in contributions:
        if row.get("error"):
            parts.append(f"{row['filename']}:error")
            continue
        flags: list[str] = []
        if row.get("has_last_name") == "yes":
            flags.append("last")
        if row.get("has_first_name") == "yes":
            flags.append("first")
        if row.get("has_emr_id") == "yes":
            flags.append("id")
        if row.get("icd_codes"):
            flags.append("icd")
        if row.get("has_expected_icd") in ("yes", "partial"):
            flags.append(f"icd_match={row['has_expected_icd']}")
        label = "+".join(flags) if flags else "none"
        parts.append(f"{row['filename']}:{label}")
    return "; ".join(parts)


def classify_edoc_filename(filename: str) -> dict[str, bool]:
    lower = filename.lower()
    return {
        "has_intake": "intake" in lower,
        "has_referral": "referral" in lower or "refer" in lower or "refferal" in lower,
        "has_insurance_id": (
            "insurance" in lower
            or " id" in lower
            or lower.startswith("id ")
            or "id &" in lower
            or "id and" in lower
            or "ins card" in lower
            or "ins." in lower
        ),
        "has_mri": "mri" in lower,
        "has_chart_note": (
            "dailynote" in lower
            or "daily_note" in lower
            or "chart_" in lower
            or "initial_evaluation" in lower
            or "re_examination" in lower
            or "re-examination" in lower
            or lower.startswith("chart")
        ),
    }


def collect_patient_pdf_paths(patient_dir: Path) -> list[Path]:
    """Return eDoc PDFs plus chart_notes/*.pdf for a patient folder."""
    if not patient_dir.exists():
        return []
    pdfs = sorted(patient_dir.glob("*.pdf"))
    chart_dir = patient_dir / "chart_notes"
    if chart_dir.is_dir():
        pdfs.extend(sorted(chart_dir.glob("*.pdf")))
    return pdfs


def build_edoc_inventory_row(patient_id: str, pdf_paths: list[Path]) -> dict[str, Any]:
    names = [p.name for p in sorted(pdf_paths)]
    flags = {
        "has_intake": False,
        "has_referral": False,
        "has_insurance_id": False,
        "has_mri": False,
        "has_chart_note": False,
    }
    for name in names:
        for key, val in classify_edoc_filename(name).items():
            flags[key] = flags[key] or val
    return {
        "patient_id": patient_id,
        "file_count": len(names),
        "filenames": "; ".join(names),
        **{k: "yes" if v else "no" for k, v in flags.items()},
    }


def run_patient_ocr_validation(
    pdf_paths: list[Path],
    *,
    expected_name: str,
    expected_id: str,
    expected_diagnosis: str = "",
    patient_dir: Path | None = None,
    dpi: int = 200,
    tesseract_cmd: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    existing = [Path(p) for p in pdf_paths if p and Path(p).exists()]
    if not existing:
        return empty_ocr_summary(error="no PDF files available for OCR")

    _, tessdata = resolve_tesseract_paths(tesseract_cmd)
    if not tessdata:
        return empty_ocr_summary(
            error="Tesseract tessdata not found; install Tesseract OCR"
        )

    try:
        ocr_text, used_files, ocr_errors = load_or_run_patient_ocr(
            existing,
            patient_dir=patient_dir,
            dpi=dpi,
            tesseract_cmd=tesseract_cmd,
            force=force,
        )
    except Exception as exc:
        return empty_ocr_summary(error=str(exc))

    if not ocr_text.strip():
        err = " | ".join(ocr_errors) if ocr_errors else "OCR produced no text"
        return empty_ocr_summary(error=err)

    extracted = extract_patient_fields(
        ocr_text,
        expected_name=expected_name,
        expected_id=expected_id,
    )
    extracted["_ocr_text"] = ocr_text
    matches = validate_ocr_fields(
        extracted,
        expected_name=expected_name,
        expected_id=expected_id,
        expected_diagnosis=expected_diagnosis,
    )

    if not expected_diagnosis.strip():
        matches["edoc_ocr_diagnosis_match"] = ""
        if not ocr_errors:
            ocr_errors = []
        ocr_errors.append("chart diagnosis unavailable")

    contributions = analyze_patient_file_contributions(
        existing,
        expected_name=expected_name,
        expected_id=expected_id,
        expected_diagnosis=expected_diagnosis,
        dpi=dpi,
        tesseract_cmd=tesseract_cmd,
    )

    summary = {
        **{k: v for k, v in extracted.items() if not k.startswith("_")},
        **matches,
        "edoc_ocr_source_files": "; ".join(used_files),
        "edoc_ocr_file_hints": format_file_hints(contributions),
        "edoc_ocr_errors": " | ".join(ocr_errors[:3]),
        "_file_contributions": contributions,
    }
    return summary

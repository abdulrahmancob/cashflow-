"""Count who has a WebPT intake PDF, then classify language, form shape and referral source.

Aggregates go to stdout. Per-patient rows stay under INTAKE_CENSUS_DIR (default
/data/exports/intake_census_v2). The referral source is read by `intake_reader`
(rules R1–R16); this file keeps the inventory, the resume log and the reports.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

OUT_DIR = Path(os.environ.get("INTAKE_CENSUS_DIR", "/data/exports/intake_census_v2"))
TESSDATA_DIR = Path(os.environ.get("INTAKE_TESSDATA_DIR", "/data/ocr/tessdata"))
SPA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/spa.traineddata"
WORKERS = int(os.environ.get("INTAKE_CENSUS_WORKERS", "6"))
MAX_FILES = int(os.environ.get("INTAKE_CENSUS_MAX_FILES", "0") or 0)
MAX_PAGES = int(os.environ.get("INTAKE_CENSUS_MAX_PAGES", "20"))
READER_VERSION = "v2"

SYSTEM_TESSDATA = os.environ.get("TESSDATA_PREFIX", "").strip() or "/usr/share/tesseract-ocr/5/tessdata"

_STRONG_SPA = (
    "formulario",
    "paciente",
    "nacimiento",
    "redes sociales",
    "cómo se",
    "como se entero",
    "cómo nos conoció",
    "como nos conocio",
    "escuchado sobre nosotros",
    "referencia médica",
    "referencia medica",
    "sin cita previa",
    "marque lo que corresponda",
)
_STRONG_ENG = (
    "patient",
    "intake",
    "how did you hear",
    "date of birth",
    "please check what applies",
    "word of mouth",
)

_LABELISH_RE = re.compile(
    r"\b("
    r"patient|intake|name|dob|birth|address|phone|insurance|hear|referr|"
    r"emergency|signature|fecha|nombre|paciente|seguro|telefono|teléfono|"
    r"direccion|dirección|entero|enteró|formulario|historia|alerg|consent|"
    r"medical|physician|email|gender|occupation"
    r")\b",
    re.IGNORECASE,
)
_SHAPE_BITS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("en_hear", re.compile(r"how did you hear|please check what applies", re.IGNORECASE)),
    ("es_form", re.compile(r"formulario|redes sociales|c[oó]mo se enter|c[oó]mo nos conoci|escuchado sobre", re.IGNORECASE)),
    ("patient_info", re.compile(r"patient information|informaci[oó]n del paciente", re.IGNORECASE)),
    ("doctor_opt", re.compile(r"doctor.?s referral|recomend|referencia m[eé]dica", re.IGNORECASE)),
    ("zocdoc_opt", re.compile(r"zoc\s*doc", re.IGNORECASE)),
    ("social_opt", re.compile(r"social media|redes sociales", re.IGNORECASE)),
    ("google_opt", re.compile(r"\bgoogle\b", re.IGNORECASE)),
)

ROW_FIELDS = (
    "path",
    "method",
    "language",
    "shape_id",
    "shape_label",
    "source",
    "source_group",
    "marks",
    "other_text",
    "booking",
    "confidence",
    "needs_review",
    "family",
    "page",
    "reason",
    "chars",
    "error",
    "reader",
)
PATIENT_FIELDS = (
    "webpt_patient_id",
    "has_intake",
    "file_count",
    "language",
    "shape_id",
    "shape_label",
    "source",
    "source_group",
    "marks",
    "other_text",
    "booking",
    "confidence",
    "needs_review",
    "family",
    "reason",
    "text_method",
)


def emit(message: str) -> None:
    print(message, flush=True)


# ---------------------------------------------------------------- inventory


def webpt_roots() -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    named = [
        Path(os.environ.get("WEBPT_OUTPUT_DIR", "/data/webpt/jan_aug_2026")),
        Path(os.environ.get("WEBPT_LEGACY_OUTPUT_DIR", "/data/webpt/legacy")),
    ]
    webpt_parent = Path("/data/webpt")
    if webpt_parent.is_dir():
        for child in sorted(webpt_parent.iterdir()):
            if child.is_dir():
                named.append(child)
    for base in named:
        edocs = base / "edocs"
        key = str(edocs)
        if key in seen:
            continue
        seen.add(key)
        if edocs.is_dir():
            roots.append(edocs)
    return roots


def case_root() -> Path:
    return Path(os.environ.get("CASE_PIPELINE_DIR", "/data/exports/side_by_side_case"))


def is_intake_name(name: str) -> bool:
    return name.lower().endswith(".pdf") and "intake" in name.lower()


def iter_intake_pdfs(folder: Path) -> list[Path]:
    found: list[Path] = []
    if not folder.is_dir():
        return found
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if is_intake_name(name):
                found.append(Path(dirpath) / name)
    return found


def _clean_id(value: object) -> str:
    text = str(value or "").strip()
    if text.lower() in {"", "none", "null"}:
        return ""
    return text


def patient_ids_from_meta(case_dir: Path) -> list[str]:
    meta_path = case_dir / "meta.json"
    ids: list[str] = []
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if isinstance(meta, dict):
            raw = meta.get("patient_ids")
            if isinstance(raw, list):
                ids.extend(_clean_id(item) for item in raw)
            ids.append(_clean_id(meta.get("patient_id")))
    ids = [item for item in ids if item]
    if ids:
        return list(dict.fromkeys(ids))
    manifest = case_dir / "manifests" / "artifacts_manifest.csv"
    if not manifest.is_file():
        return []
    try:
        with manifest.open(newline="", encoding="utf-8", errors="replace") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                pid = _clean_id(row.get("patient_id"))
                if pid:
                    ids.append(pid)
    except OSError:
        return []
    return list(dict.fromkeys(ids))


def load_db_patient_ids() -> tuple[set[str], str]:
    url = os.environ.get("CASHFLOW_DATABASE_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return set(), "missing_database_url"
    try:
        import psycopg
    except ImportError:
        return set(), "psycopg_missing"
    try:
        with psycopg.connect(url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT webpt_patient_id
                    FROM core.patient
                    WHERE webpt_patient_id IS NOT NULL
                      AND btrim(webpt_patient_id) <> ''
                    """
                )
                ids = {_clean_id(row[0]) for row in cur.fetchall()}
        return {item for item in ids if item}, ""
    except Exception as exc:
        return set(), type(exc).__name__


def inventory() -> tuple[set[str], dict[str, list[Path]], list[Path], list[str]]:
    """Return (all patient ids, intake paths by patient, unmatched files, edoc roots)."""
    patients, db_error = load_db_patient_ids()
    db_count = len(patients)
    intake_by_patient: dict[str, list[Path]] = {}
    unmatched: list[Path] = []
    roots = webpt_roots()
    root_labels = [str(path) for path in roots]

    for edocs in roots:
        try:
            children = list(os.scandir(edocs))
        except OSError as exc:
            emit(f"edocs_unreadable path={edocs} error={type(exc).__name__}")
            continue
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            pid = _clean_id(child.name)
            if not pid:
                continue
            patients.add(pid)
            pdfs = iter_intake_pdfs(Path(child.path))
            if pdfs:
                intake_by_patient.setdefault(pid, []).extend(pdfs)

    cases = case_root() / "cases"
    case_dirs = 0
    if cases.is_dir():
        for facility in os.scandir(cases):
            if not facility.is_dir():
                continue
            for case in os.scandir(facility.path):
                if not case.is_dir():
                    continue
                case_dirs += 1
                case_dir = Path(case.path)
                pdfs = iter_intake_pdfs(case_dir / "edocs")
                pids = patient_ids_from_meta(case_dir)
                for pid in pids:
                    patients.add(pid)
                if not pdfs:
                    continue
                if not pids:
                    unmatched.extend(pdfs)
                    continue
                intake_by_patient.setdefault(pids[0], []).extend(pdfs)
    else:
        emit(f"case_tree_missing path={cases}")

    for pid, paths in intake_by_patient.items():
        intake_by_patient[pid] = list(dict.fromkeys(paths))

    file_count = len({path for paths in intake_by_patient.values() for path in paths}) + len(unmatched)
    emit("INTAKE_CENSUS_INVENTORY")
    emit(f"patients_db={db_count}")
    if db_error:
        emit(f"db_error={db_error}")
    emit(f"patients_total={len(patients)}")
    emit(f"with_intake={len(intake_by_patient)}")
    emit(f"without_intake={len(patients) - len(intake_by_patient)}")
    emit(f"intake_files={file_count}")
    emit(f"unmatched_intake_files={len(unmatched)}")
    emit(f"case_dirs={case_dirs}")
    emit("edoc_roots=" + ",".join(root_labels))
    emit("INTAKE_CENSUS_INVENTORY_DONE")
    return patients, intake_by_patient, unmatched, root_labels


# ---------------------------------------------------------------- tesseract data


def ensure_tessdata() -> tuple[str, bool]:
    TESSDATA_DIR.mkdir(parents=True, exist_ok=True)
    system = Path(SYSTEM_TESSDATA)
    for name in ("eng.traineddata", "osd.traineddata"):
        dest = TESSDATA_DIR / name
        src = system / name
        if dest.is_file() and dest.stat().st_size > 1000:
            continue
        if src.is_file():
            dest.write_bytes(src.read_bytes())
    spa = TESSDATA_DIR / "spa.traineddata"
    spa_ok = spa.is_file() and spa.stat().st_size > 100_000
    if not spa_ok:
        try:
            urllib.request.urlretrieve(SPA_URL, spa)
            spa_ok = spa.is_file() and spa.stat().st_size > 100_000
        except Exception as exc:
            emit(f"spa_tessdata_error={type(exc).__name__}")
            spa_ok = False
            if spa.exists() and spa.stat().st_size < 100_000:
                spa.unlink(missing_ok=True)
    if not (TESSDATA_DIR / "eng.traineddata").is_file():
        emit("eng_tessdata_missing=1")
    if not (TESSDATA_DIR / "osd.traineddata").is_file():
        emit("osd_tessdata_missing=1")
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
    return str(TESSDATA_DIR), spa_ok


# ---------------------------------------------------------------- language / shape


def detect_language(text: str) -> str:
    if not text or len(text.strip()) < 20:
        return "unknown"
    low = text.lower()
    spa = sum(1 for word in _STRONG_SPA if word in low)
    eng = sum(1 for word in _STRONG_ENG if word in low)
    if spa and eng:
        if abs(spa - eng) <= 1:
            return "bilingual"
        return "spanish" if spa > eng else "english"
    if spa:
        return "spanish"
    if eng:
        return "english"
    return "unknown"


def _normalize_line(raw: str) -> str:
    line = raw.lower()
    line = re.sub(r"https?://\S+", " ", line)
    line = re.sub(r"\b[\w.+-]+@[\w.-]+\b", " ", line)
    line = re.sub(r"\d", "", line)
    line = re.sub(r"[^a-záéíóúüñ\s]", " ", line)
    return re.sub(r"\s+", " ", line).strip()


def form_fingerprint(text: str) -> tuple[str, str]:
    hits = [name for name, pattern in _SHAPE_BITS if pattern.search(text)]
    if len(hits) >= 3:
        blob = "|".join(hits)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10], blob.replace("_", " ")
    label_lines: list[str] = []
    fallback: list[str] = []
    seen_labels: set[str] = set()
    for index, raw in enumerate(text.splitlines()):
        if index > 80 and len(label_lines) >= 4:
            break
        if index > 120:
            break
        norm = _normalize_line(raw)
        if len(norm) < 6:
            continue
        if len(fallback) < 12:
            fallback.append(norm)
        if _LABELISH_RE.search(norm) and norm not in seen_labels:
            seen_labels.add(norm)
            label_lines.append(norm)
        if len(label_lines) >= 15:
            break
    chosen = label_lines if len(label_lines) >= 4 else fallback
    if not chosen:
        return "no_text", "no_text"
    blob = " | ".join(chosen)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10], chosen[0][:80]


def merge_language(langs: list[str]) -> str:
    present = {lang for lang in langs if lang and lang != "unknown"}
    if not present:
        return "unknown"
    if "bilingual" in present or ({"english", "spanish"} <= present):
        return "bilingual"
    if "spanish" in present:
        return "spanish"
    if "english" in present:
        return "english"
    return "unknown"


# ---------------------------------------------------------------- classification


def _limit_ocr_threads() -> None:
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"


def classify_one(path_str: str, tessdata: str, spa_ok: bool) -> dict[str, str]:
    from intake_reader import read_intake

    path = Path(path_str)
    base = {name: "" for name in ROW_FIELDS}
    base.update({"path": path_str, "reader": READER_VERSION})
    try:
        result = read_intake(str(path), lang="eng+spa" if spa_ok else "eng", max_pages=MAX_PAGES)
        shape_id, shape_label = form_fingerprint(result.text)
        base.update(result.reading.as_row())
        base.update(
            {
                "method": result.method,
                "language": detect_language(result.text),
                "shape_id": shape_id,
                "shape_label": shape_label,
                "chars": str(len(result.text)),
            }
        )
        return base
    except Exception as exc:
        base.update(
            {
                "method": "error",
                "language": "unknown",
                "shape_id": "no_text",
                "shape_label": "no_text",
                "source": "unreadable",
                "source_group": "Unreadable",
                "reason": f"error:{type(exc).__name__}",
                "needs_review": "1",
                "confidence": "0.00",
                "chars": "0",
                "error": type(exc).__name__,
            }
        )
        return base


def _worker(payload: tuple[str, str, bool]) -> dict[str, str]:
    path_str, tessdata, spa_ok = payload
    _limit_ocr_threads()
    os.environ["TESSDATA_PREFIX"] = tessdata
    return classify_one(path_str, tessdata, spa_ok)


def load_saved_rows() -> dict[str, dict[str, str]]:
    path = OUT_DIR / "rows.jsonl"
    saved: dict[str, dict[str, str]] = {}
    if not path.is_file():
        return saved
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return saved
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = row.get("path")
        if not key or row.get("reader") != READER_VERSION:
            continue
        saved[str(key)] = {name: "" if value is None else str(value) for name, value in row.items()}
    return saved


def append_saved_row(row: dict[str, str]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with (OUT_DIR / "rows.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()


def classify_files(paths: list[Path], tessdata: str, spa_ok: bool) -> dict[str, dict[str, str]]:
    unique = list(dict.fromkeys(paths))
    if MAX_FILES:
        unique = unique[:MAX_FILES]
    if not unique:
        return {}
    known = {str(path) for path in unique}
    results = {key: row for key, row in load_saved_rows().items() if key in known}
    pending = [path for path in unique if str(path) not in results]
    total = len(unique)
    done = total - len(pending)
    started = time.monotonic()
    emit(f"resumed={done}")
    if done:
        emit(f"classified={done}/{total}")
    if not pending:
        return results
    resumed = done

    def note(row: dict[str, str]) -> None:
        nonlocal done
        results[row["path"]] = row
        append_saved_row(row)
        done += 1
        fresh = done - resumed
        if fresh == 1 or fresh % 100 == 0 or done == total:
            elapsed = max(time.monotonic() - started, 0.001)
            emit(f"classified={done}/{total} rate={fresh / elapsed * 60:.1f}/min")

    payloads = [(str(path), tessdata, spa_ok) for path in pending]
    workers = max(1, min(WORKERS, len(payloads)))
    if workers == 1:
        for item in payloads:
            note(_worker(item))
        return results
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, item) for item in payloads]
        for future in as_completed(futures):
            note(future.result())
    return results


# ---------------------------------------------------------------- reports


def row_to_reading(row: dict[str, str]):
    from intake_reader import Reading

    return Reading(
        source=row.get("source") or "unreadable",
        marks=[m for m in (row.get("marks") or "").split(",") if m],
        other_text=row.get("other_text") or "",
        booking=[b for b in (row.get("booking") or "").split(",") if b],
        family=row.get("family") or "",
        page=int(row.get("page") or -1),
        confidence=float(row.get("confidence") or 0.0),
        needs_review=bool(row.get("needs_review")),
        reasons=[r for r in (row.get("reason") or "").split(";") if r],
    )


def merge_patient(rows: list[dict[str, str]]) -> dict[str, str]:
    """Best-evidence merge across a patient's files (R15)."""
    from intake_reader import GROUPS, merge_readings

    readings = [row_to_reading(row) for row in rows if row]
    merged = merge_readings(readings) if readings else None
    primary_row = max(rows, key=lambda r: int(r.get("chars") or 0)) if rows else {}
    out = {
        "language": merge_language([row.get("language", "") for row in rows]),
        "shape_id": primary_row.get("shape_id") or "no_text",
        "shape_label": primary_row.get("shape_label") or "no_text",
        "source": merged.source if merged else "unreadable",
        "source_group": GROUPS.get(merged.source, merged.source) if merged else "Unreadable",
        "marks": ",".join(merged.marks) if merged else "",
        "other_text": merged.other_text if merged else "",
        "booking": ",".join(merged.booking) if merged else "",
        "confidence": f"{merged.confidence:.2f}" if merged else "0.00",
        "needs_review": "1" if (merged is None or merged.needs_review) else "",
        "family": merged.family if merged else "",
        "reason": ";".join(merged.reasons) if merged else "",
    }
    methods = sorted({row.get("method") or "error" for row in rows})
    out["text_method"] = methods[0] if len(methods) == 1 else "mixed"
    return out


def write_reports(
    patients: set[str],
    intake_by_patient: dict[str, list[Path]],
    unmatched: list[Path],
    file_rows: dict[str, dict[str, str]],
    roots: list[str],
    spa_ok: bool,
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    language_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    group_counts: Counter[str] = Counter()
    family_counts: Counter[str] = Counter()
    method_counts: Counter[str] = Counter()
    review_count = 0
    shape_patients: dict[str, set[str]] = {}
    shape_labels: dict[str, str] = {}
    detail_path = OUT_DIR / "patients.csv"
    with detail_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PATIENT_FIELDS))
        writer.writeheader()
        for pid in sorted(patients):
            paths = intake_by_patient.get(pid, [])
            rows = [file_rows.get(str(path)) for path in paths]
            rows = [row for row in rows if row]
            if not paths or not rows:
                writer.writerow({**{name: "" for name in PATIENT_FIELDS}, "webpt_patient_id": pid, "has_intake": "no" if not paths else "yes", "file_count": len(paths)})
                continue
            merged = merge_patient(rows)
            language_counts[merged["language"]] += 1
            source_counts[merged["source"]] += 1
            group_counts[merged["source_group"]] += 1
            family_counts[merged["family"] or "none"] += 1
            method_counts[merged["text_method"]] += 1
            if merged["needs_review"]:
                review_count += 1
            shape_patients.setdefault(merged["shape_id"], set()).add(pid)
            shape_labels.setdefault(merged["shape_id"], merged["shape_label"])
            writer.writerow({"webpt_patient_id": pid, "has_intake": "yes", "file_count": len(paths), **merged})

    shapes = sorted(shape_patients.items(), key=lambda item: (-len(item[1]), item[0]))
    shape_report = [{"shape_id": shape_id, "label": shape_labels.get(shape_id, ""), "patients": len(pids)} for shape_id, pids in shapes]
    summary = {
        "reader": READER_VERSION,
        "patients_total": len(patients),
        "with_intake": len(intake_by_patient),
        "without_intake": len(patients) - len(intake_by_patient),
        "intake_files": len(file_rows),
        "unmatched_intake_files": len(unmatched),
        "spa_tessdata": spa_ok,
        "edoc_roots": roots,
        "language": dict(language_counts),
        "source": dict(source_counts),
        "source_group": dict(group_counts),
        "family": dict(family_counts),
        "needs_review": review_count,
        "text_method": dict(method_counts),
        "shapes": shape_report,
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    emit("INTAKE_CENSUS_DONE")
    for key in ("patients_total", "with_intake", "without_intake", "intake_files", "unmatched_intake_files"):
        emit(f"{key}={summary[key]}")
    emit(f"spa_tessdata={int(spa_ok)}")
    for key in ("english", "spanish", "bilingual", "unknown"):
        emit(f"language_{key}={language_counts.get(key, 0)}")
    for key, count in sorted(source_counts.items(), key=lambda kv: -kv[1]):
        emit(f"source_{key}={count}")
    for key, count in sorted(group_counts.items(), key=lambda kv: -kv[1]):
        emit(f"group {key}={count}")
    for key, count in sorted(family_counts.items(), key=lambda kv: -kv[1]):
        emit(f"family_{key}={count}")
    emit(f"needs_review={review_count}")
    for key, count in method_counts.items():
        emit(f"text_method_{key}={count}")
    emit(f"shape_count={len(shape_report)}")
    for row in shape_report[:25]:
        emit(f"shape {row['shape_id']} patients={row['patients']} label={row['label'].replace(chr(10), ' ')}")


# ---------------------------------------------------------------- entry points


def _self_check() -> int:
    """Synthetic rule tests (no tesseract needed)."""
    import unittest

    here = Path(__file__).resolve().parent
    suite = unittest.defaultTestLoader.discover(str(here / "intake_reader" / "tests"), top_level_dir=str(here))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    ok = result.wasSuccessful()
    emit(f"self_check={'ok' if ok else 'failed'} tests={result.testsRun} failures={len(result.failures)} errors={len(result.errors)}")
    return 0 if ok else 1


def _check_pdfs(folder: str) -> int:
    """Read every PDF in a folder and print the readings (needs tesseract)."""
    from intake_reader import read_intake

    tessdata, spa_ok = ensure_tessdata()
    lang = "eng+spa" if spa_ok else "eng"
    bad = 0
    for path in sorted(Path(folder).glob("*.pdf")):
        started = time.monotonic()
        try:
            result = read_intake(str(path), lang=lang, max_pages=MAX_PAGES)
            reading = result.reading
            emit(
                f"PDF {path.name} source={reading.source} marks={','.join(reading.marks)} booking={','.join(reading.booking)} "
                f"family={reading.family} page={reading.page} conf={reading.confidence:.2f} review={int(reading.needs_review)} "
                f"reasons={';'.join(reading.reasons)} other={reading.other_text!r} method={result.method} "
                f"angles={[s.angle for s in result.scanned]} secs={time.monotonic() - started:.1f}"
            )
        except Exception as exc:
            bad += 1
            emit(f"PDF {path.name} error={type(exc).__name__}: {exc}")
    return 1 if bad else 0


def main() -> int:
    _limit_ocr_threads()
    try:
        level = int(os.environ.get("INTAKE_CENSUS_NICE", "0"))
        if level:
            os.nice(level)
    except (AttributeError, OSError, ValueError):
        pass
    patients, intake_by_patient, unmatched, roots = inventory()
    files = [path for paths in intake_by_patient.values() for path in paths]
    files.extend(unmatched)
    tessdata, spa_ok = ensure_tessdata()
    emit(f"spa_tessdata={int(spa_ok)}")
    file_rows = classify_files(files, tessdata, spa_ok)
    write_reports(patients, intake_by_patient, unmatched, file_rows, roots, spa_ok)
    return 0


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        sys.exit(_self_check())
    if "--check-pdfs" in sys.argv:
        sys.exit(_check_pdfs(sys.argv[sys.argv.index("--check-pdfs") + 1]))
    sys.exit(main())

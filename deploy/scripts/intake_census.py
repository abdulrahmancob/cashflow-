"""Count who has a WebPT intake PDF, then classify language, form shape, and referral source.

Aggregates go to stdout. Per-patient rows stay under /data/exports/intake_census/.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

OUT_DIR = Path(os.environ.get("INTAKE_CENSUS_DIR", "/data/exports/intake_census"))
TEXT_DIR = OUT_DIR / "text"
TESSDATA_DIR = Path(os.environ.get("INTAKE_TESSDATA_DIR", "/data/ocr/tessdata"))
SPA_URL = (
    "https://github.com/tesseract-ocr/tessdata_fast/raw/main/spa.traineddata"
)
NATIVE_MIN_CHARS = 50
MAX_PAGES = 12
MAX_OCR_PAGES = 8
OCR_DPI = 150
WORKERS = int(os.environ.get("INTAKE_CENSUS_WORKERS", "2"))

SYSTEM_TESSDATA = (
    os.environ.get("TESSDATA_PREFIX", "").strip()
    or "/usr/share/tesseract-ocr/5/tessdata"
)

_SPA_WORDS = (
    "formulario",
    "paciente",
    "nacimiento",
    "apellido",
    "direccion",
    "dirección",
    "telefono",
    "teléfono",
    "seguro",
    "alergia",
    "medicamento",
    "firma",
    "enteró",
    "entero",
    "historia",
    "masculino",
    "femenino",
    "dolor",
    "como se",
    "cómo se",
)
_ENG_WORDS = (
    "patient",
    "intake",
    "birth",
    "insurance",
    "address",
    "phone",
    "signature",
    "emergency",
    "hear",
    "medical",
    "history",
    "physician",
    "allergy",
    "medication",
    "pain",
    "date of",
)

_HEAR_RE = re.compile(
    r"("
    r"how\s+did\s+you\s+(?:hear|find|learn)(?:\s+about(?:\s+us)?)?"
    r"|referred\s+by"
    r"|referral\s+source"
    r"|who\s+referred"
    r"|c[oó]mo\s+(?:se\s+)?enter[oó](?:\s+de(?:\s+nosotros)?)?"
    r"|c[oó]mo\s+nos\s+conoci[oó]"
    r"|c[oó]mo\s+supo"
    r"|fuente\s+de\s+referencia"
    r")",
    re.IGNORECASE,
)

# Order is the primary-source priority when more than one box is marked.
_BUCKETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("zocdoc", re.compile(r"zoc\s*doc", re.IGNORECASE)),
    (
        "social_media",
        re.compile(
            r"facebook|instagram|tik\s*tok|twitter|youtube|snapchat|"
            r"social\s+media|redes\s+sociales|\b(?:fb|ig)\b",
            re.IGNORECASE,
        ),
    ),
    ("google", re.compile(r"\bgoogle\b", re.IGNORECASE)),
    (
        "website",
        re.compile(
            r"web\s*site|\bwebsite\b|\binternet\b|p[aá]gina\s+web|sitio\s+web",
            re.IGNORECASE,
        ),
    ),
    (
        "doctor",
        re.compile(
            r"\b(?:doctor|pcp|therapist|m[eé]dico|medico|physician referral)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "friend_family",
        re.compile(
            r"friend|family|spouse|relative|word of mouth|amigo|familia|familiar",
            re.IGNORECASE,
        ),
    ),
    (
        "insurance",
        re.compile(
            r"\binsurance\b|aetna|medicaid|medicare|blue\s*cross|seguro",
            re.IGNORECASE,
        ),
    ),
    (
        "walk_in",
        re.compile(r"walk[\s-]*in|drove by|saw (?:the |a )?sign|letrero", re.IGNORECASE),
    ),
    ("phone", re.compile(r"\bphone\b|tel[eé]fono", re.IGNORECASE)),
)

_MARK_RE = re.compile(
    r"(?:\[\s*[xX✓✔]\s*\]|\(\s*[xX✓✔]\s*\)|[☑☒✓✔]|(?:^|\s)[xX](?:\s|$))"
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
_ACCENT_RE = re.compile(r"[áéíóúñ¿¡]")


def emit(message: str) -> None:
    print(message, flush=True)


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
    url = (
        os.environ.get("CASHFLOW_DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )
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
        deduped = list(dict.fromkeys(paths))
        intake_by_patient[pid] = deduped

    file_count = len({path for paths in intake_by_patient.values() for path in paths})
    file_count += len(unmatched)
    with_intake = len(intake_by_patient)
    emit("INTAKE_CENSUS_INVENTORY")
    emit(f"patients_db={db_count}")
    if db_error:
        emit(f"db_error={db_error}")
    emit(f"patients_total={len(patients)}")
    emit(f"with_intake={with_intake}")
    emit(f"without_intake={len(patients) - with_intake}")
    emit(f"intake_files={file_count}")
    emit(f"unmatched_intake_files={len(unmatched)}")
    emit(f"case_dirs={case_dirs}")
    emit("edoc_roots=" + ",".join(root_labels))
    emit("INTAKE_CENSUS_INVENTORY_DONE")
    return patients, intake_by_patient, unmatched, root_labels


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
    os.environ["TESSDATA_PREFIX"] = str(TESSDATA_DIR)
    return str(TESSDATA_DIR), spa_ok


def cache_path_for(pdf: Path) -> Path:
    stat = pdf.stat()
    raw = f"{pdf.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return TEXT_DIR / f"{digest}.txt"


def read_cache(path: Path) -> tuple[str, str] | None:
    cache = cache_path_for(path)
    if not cache.is_file():
        return None
    try:
        payload = cache.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    method, sep, text = payload.partition("\n")
    if not sep or not method.startswith("method="):
        return None
    return method.removeprefix("method="), text


def write_cache(path: Path, method: str, text: str) -> None:
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    cache = cache_path_for(path)
    tmp = cache.with_suffix(".tmp")
    tmp.write_text(f"method={method}\n{text}", encoding="utf-8")
    tmp.replace(cache)


def ocr_page(page, language: str, tessdata: str) -> str:
    try:
        kwargs = {"language": language, "dpi": OCR_DPI}
        if tessdata:
            kwargs["tessdata"] = tessdata
        text_page = page.get_textpage_ocr(**kwargs)
        return (text_page.extractText() or "").strip()
    except Exception:
        pass
    try:
        import fitz
        import pytesseract
        from PIL import Image

        pix = page.get_pixmap(matrix=fitz.Matrix(OCR_DPI / 72, OCR_DPI / 72), alpha=False)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        return (pytesseract.image_to_string(image, lang=language) or "").strip()
    except Exception:
        return ""


def extract_pdf_text(path: Path, tessdata: str, spa_ok: bool) -> tuple[str, str]:
    cached = read_cache(path)
    if cached is not None:
        return cached[1], cached[0]
    import fitz

    language = "eng+spa" if spa_ok else "eng"
    doc = fitz.open(path)
    chunks: list[str] = []
    used_native = False
    used_ocr = False
    ocr_pages = 0
    try:
        for index, page in enumerate(doc):
            if index >= MAX_PAGES:
                break
            native = (page.get_text() or "").strip()
            if len(native) >= NATIVE_MIN_CHARS:
                chunks.append(native)
                used_native = True
                continue
            if ocr_pages >= MAX_OCR_PAGES:
                if native:
                    chunks.append(native)
                    used_native = True
                continue
            ocr_pages += 1
            page_text = ocr_page(page, language, tessdata)
            if page_text:
                chunks.append(page_text)
                used_ocr = True
            elif native:
                chunks.append(native)
                used_native = True
    finally:
        doc.close()
    if used_ocr and used_native:
        method = "mixed"
    elif used_ocr:
        method = "ocr"
    elif used_native:
        method = "native_text"
    else:
        method = "empty"
    text = "\n".join(chunks)
    write_cache(path, method, text)
    return text, method


_STRONG_SPA = (
    "formulario",
    "paciente",
    "nacimiento",
    "redes sociales",
    "cómo se",
    "como se entero",
)
_STRONG_ENG = (
    "patient",
    "intake",
    "how did you hear",
    "date of birth",
    "please check what applies",
)


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


_SHAPE_BITS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("en_hear", re.compile(r"how did you hear|please check what applies", re.IGNORECASE)),
    ("es_form", re.compile(r"formulario|redes sociales|c[oó]mo se enter", re.IGNORECASE)),
    ("patient_info", re.compile(r"patient information|informaci[oó]n del paciente", re.IGNORECASE)),
    ("doctor_opt", re.compile(r"doctor.?s referral|recomend", re.IGNORECASE)),
    ("zocdoc_opt", re.compile(r"zoc\s*doc", re.IGNORECASE)),
    ("social_opt", re.compile(r"social media|redes sociales", re.IGNORECASE)),
    ("google_opt", re.compile(r"\bgoogle\b", re.IGNORECASE)),
)


def form_fingerprint(text: str) -> tuple[str, str]:
    hits = [name for name, pattern in _SHAPE_BITS if pattern.search(text)]
    if len(hits) >= 3:
        blob = "|".join(hits)
        shape_id = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
        return shape_id, blob.replace("_", " ")
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
    shape_id = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
    return shape_id, chosen[0][:80]


def _keywords(window: str) -> list[str]:
    found: list[str] = []
    for name, pattern in _BUCKETS:
        if pattern.search(window):
            found.append(name)
    return found


def _marked_keywords(window: str) -> list[str]:
    hits: list[str] = []
    for raw_line in re.split(r"[\r\n]+", window):
        line = raw_line.strip()
        if not line:
            continue
        marked = bool(_MARK_RE.search(line)) or bool(re.match(r"^[xX✓✔☑]\s+\S", line))
        if not marked:
            continue
        for name in _keywords(line):
            if name not in hits:
                hits.append(name)
    return hits


_EMPTY_MARK_RE = re.compile(
    r"^(?:[Oo0○◦☐]\s+|[Oo0](?=[A-Za-z])|\[\s*\]|\(\s*\))"
)
_SELECTED_MARK_RE = re.compile(
    r"^(?:[@●•✓✔☑☒]\s*|[xX]\s+|\[\s*[xX✓✔]\s*\]|\(\s*[xX]\s*\))"
)
_INSTRUCTION_RE = re.compile(
    r"please check|what applies|check all",
    re.IGNORECASE,
)


def _option_state(line: str) -> str:
    stripped = line.strip()
    if _INSTRUCTION_RE.search(stripped):
        return "instruction"
    if _EMPTY_MARK_RE.search(stripped):
        return "empty"
    if _SELECTED_MARK_RE.search(stripped) or _MARK_RE.search(stripped):
        return "selected"
    return "plain"


def classify_source(text: str) -> str:
    if not text.strip():
        return "unreadable"
    match = _HEAR_RE.search(text)
    if match is None:
        if re.search(r"zoc\s*doc", text, re.IGNORECASE):
            return "zocdoc"
        return "unreadable"
    window = text[match.end() : match.end() + 700]
    remainder = window.splitlines()[0] if window.splitlines() else ""
    remainder_hits = _keywords(remainder)
    if (
        len(remainder_hits) == 1
        and not _INSTRUCTION_RE.search(remainder)
        and _option_state(remainder) == "plain"
    ):
        return remainder_hits[0]
    lines = [line.strip() for line in window.splitlines() if line.strip()][:12]
    options: list[tuple[str, str]] = []
    answer_lines: list[str] = []
    for line in lines:
        state = _option_state(line)
        if state == "instruction":
            continue
        if state == "plain" and re.match(
            r"^(primary|referring|emergency)\b", line, re.IGNORECASE
        ):
            break
        answer_lines.append(line)
        found = _keywords(line)
        if not found:
            continue
        if state == "plain" and len(line) > 48:
            if options:
                break
            continue
        if state == "plain" and re.search(
            r"\b(?:emergency|signature|contact|address)\b", line, re.IGNORECASE
        ):
            if options:
                break
            continue
        options.append((found[0], state))
        if len(options) >= 8:
            break
    selected = [name for name, state in options if state == "selected"]
    empty = [name for name, state in options if state == "empty"]
    plain = [name for name, state in options if state == "plain"]
    if len(selected) == 1:
        return selected[0]
    if len(selected) > 1:
        return min(selected, key=lambda name: _SOURCE_RANK.get(name, 99))
    if empty and len(plain) == 1:
        return plain[0]
    if len(options) == 1 and options[0][1] != "empty":
        return options[0][0]
    if len(options) >= 2:
        return "unmarked_options"
    letters = re.sub(r"[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ]", "", " ".join(answer_lines))
    if len(letters) < 2:
        return "blank"
    return "other"


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


_SOURCE_RANK = {
    "zocdoc": 0,
    "social_media": 1,
    "google": 2,
    "doctor": 3,
    "friend_family": 4,
    "website": 5,
    "insurance": 6,
    "walk_in": 7,
    "phone": 8,
    "other": 9,
    "multiple": 10,
    "unmarked_options": 11,
    "blank": 12,
    "unreadable": 13,
}


def merge_source(sources: list[str]) -> str:
    usable = [src for src in sources if src]
    if not usable:
        return "unreadable"
    return min(usable, key=lambda src: _SOURCE_RANK.get(src, 99))


def _visible_edit_distance(left: str, right: str) -> int:
    if abs(len(left) - len(right)) > 2:
        return 99
    prev = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for other_index, other in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    prev[other_index] + 1,
                    prev[other_index - 1] + (char != other),
                )
            )
        prev = current
    return prev[-1]


_VISIBLE_BUCKETS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("zocdoc", re.compile(r"zoc\s*doc", re.I)),
    ("social_media", re.compile(r"social\s+media|redes\s+sociales|facebook|instagram", re.I)),
    ("google", re.compile(r"\bgoogle\b", re.I)),
    ("website", re.compile(r"web\s*site|\bwebsite\b|sitio\s+web", re.I)),
    ("doctor", re.compile(r"\b(?:doctor|m[eé]dico|medico|remisi[oó]n)\b", re.I)),
    (
        "friend_family",
        re.compile(r"friend|family|word\s+of\s+mouth|boca\s+en\s+boca|amigo|familia", re.I),
    ),
    ("insurance", re.compile(r"\binsurance\b|seguro", re.I)),
    ("walk_in", re.compile(r"walk[\s-]*in", re.I)),
    ("phone", re.compile(r"\bphone\b|tel[eé]fon", re.I)),
    ("other", re.compile(r"\bothers?\b|\botros?\b|lives\s+nearby|\bnearby\b|especif", re.I)),
)
_VISIBLE_FUZZY = (
    ("google", "google"),
    ("zocdoc", "zocdoc"),
    ("doctor", "doctor"),
    ("social_media", "social"),
    ("friend_family", "friend"),
    ("friend_family", "family"),
    ("friend_family", "mouth"),
    ("walk_in", "walk"),
    ("other", "other"),
    ("other", "nearby"),
    ("phone", "phone"),
    ("website", "website"),
    ("insurance", "insurance"),
)
_VISIBLE_HINT_RE = re.compile(r"^\(?\s*(?:type|escriba|typedoctor)", re.I)
_VISIBLE_STOP_RE = re.compile(
    r"insurance information|workers|medicare coverage|primary insurance",
    re.I,
)


def _visible_fuzzy(token: str) -> str | None:
    folded = re.sub(r"[^a-z]", "", token.lower())
    if len(folded) < 4:
        return None
    best_name = None
    best_dist = 99
    for name, canon in _VISIBLE_FUZZY:
        dist = _visible_edit_distance(folded, canon)
        if dist < best_dist:
            best_name, best_dist = name, dist
    if best_dist <= 1:
        return best_name
    return None


def choose_visible_mark(scores: list[tuple[str, float]]) -> str:
    by_name: dict[str, float] = {}
    for name, score in scores:
        by_name[name] = max(score, by_name.get(name, 0.0))
    ordered = sorted(by_name.items(), key=lambda item: item[1], reverse=True)
    if not ordered or ordered[0][1] < 0.10:
        return "unmarked"
    top_name, top = ordered[0]
    second = ordered[1][1] if len(ordered) > 1 else 0.0
    if top >= 0.55 and top >= second + 0.20:
        return top_name
    if top >= second + 0.08:
        return top_name
    marked = [name for name, score in ordered if score >= 0.10 and top - score <= 0.04]
    if len(marked) == 1:
        return marked[0]
    if len(marked) > 1:
        return min(marked, key=lambda name: _SOURCE_RANK.get(name, 99))
    return "unmarked"


def _checkbox_fill(image, word: dict, text_h: int) -> float:
    h = max(12, text_h)
    x0 = max(0, word["x"] - int(h * 4.2))
    x1 = min(image.width, word["x"] - 1)
    y0 = max(0, word["y"] - int(h * 0.25))
    y1 = min(image.height, word["y"] + min(word["h"], h) + int(h * 0.35))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return 0.0
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    width, height = crop.size
    pixels = list(crop.getdata())
    dark = [pixel < 160 for pixel in pixels]
    seen = [False] * (width * height)
    best_gap = 10**9
    best_fill = 0.0
    min_side = max(8, int(h * 0.85))
    max_side = int(h * 2.3)
    for start, on in enumerate(dark):
        if not on or seen[start]:
            continue
        stack = [start]
        seen[start] = True
        cells: list[int] = []
        while stack:
            cur = stack.pop()
            cells.append(cur)
            x = cur % width
            for nxt in (cur - 1, cur + 1, cur - width, cur + width):
                if nxt < 0 or nxt >= width * height or seen[nxt] or not dark[nxt]:
                    continue
                if abs((nxt % width) - x) > 1:
                    continue
                seen[nxt] = True
                stack.append(nxt)
        if len(cells) < 12:
            continue
        xs = [cell % width for cell in cells]
        ys = [cell // width for cell in cells]
        left, right = min(xs), max(xs)
        top, bottom = min(ys), max(ys)
        side_w = right - left + 1
        side_h = bottom - top + 1
        if side_w < int(h * 0.40) or side_h < int(h * 0.40) or side_w > max_side or side_h > max_side:
            continue
        if max(side_w, side_h) > min(side_w, side_h) * 2.3:
            continue
        gap = word["x"] - (x0 + right)
        if gap < h * 0.08 or gap > h * 4.0:
            continue
        pad_x = max(1, int(side_w * 0.22))
        pad_y = max(1, int(side_h * 0.22))
        if right - pad_x <= left + pad_x or bottom - pad_y <= top + pad_y:
            continue
        inner = [
            pixels[y * width + x]
            for y in range(top + pad_y, bottom - pad_y + 1)
            for x in range(left + pad_x, right - pad_x + 1)
        ]
        if len(inner) < 4:
            continue
        fill = sum(1 for pixel in inner if pixel < 160) / len(inner)
        if (side_w < min_side or side_h < min_side) and fill < 0.70:
            continue
        if gap < best_gap:
            best_gap = gap
            best_fill = fill
    return best_fill


def _visible_lines(image) -> list[dict]:
    import pytesseract

    try:
        data = pytesseract.image_to_data(image, lang="eng+spa", output_type=pytesseract.Output.DICT)
    except Exception:
        data = pytesseract.image_to_data(image, lang="eng", output_type=pytesseract.Output.DICT)
    words = []
    for index, raw in enumerate(data["text"]):
        text = (raw or "").strip()
        if not text or int(data["conf"][index]) < 0:
            continue
        words.append(
            {
                "text": text,
                "x": int(data["left"][index]),
                "y": int(data["top"][index]),
                "w": int(data["width"][index]),
                "h": max(8, int(data["height"][index])),
            }
        )
    words.sort(key=lambda word: (word["y"], word["x"]))
    grouped: list[dict] = []
    for word in words:
        center = word["y"] + word["h"] / 2
        if grouped and abs(center - grouped[-1]["cy"]) <= 14:
            grouped[-1]["words"].append(word)
            grouped[-1]["cy"] = sum(item["y"] + item["h"] / 2 for item in grouped[-1]["words"]) / len(
                grouped[-1]["words"]
            )
        else:
            grouped.append({"cy": center, "words": [word]})
    for line in grouped:
        line["words"].sort(key=lambda word: word["x"])
        line["text"] = " ".join(word["text"] for word in line["words"])
    return grouped


def _visible_word_at(words: list[dict], text: str, start: int) -> dict:
    cursor = 0
    for word in words:
        end = cursor + len(word["text"])
        if start < end:
            return word
        cursor = end + 1
    return words[-1]


def _visible_options(words: list[dict]) -> list[tuple[str, dict]]:
    text = " ".join(word["text"] for word in words)
    if not text or _VISIBLE_HINT_RE.search(text):
        return []
    occupied = [False] * len(text)
    found: list[tuple[str, dict]] = []
    seen: set[int] = set()
    for name, pattern in _VISIBLE_BUCKETS:
        for match in pattern.finditer(text):
            if any(occupied[match.start() : match.end()]):
                continue
            for index in range(match.start(), match.end()):
                occupied[index] = True
            first = _visible_word_at(words, text, match.start())
            if id(first) in seen:
                continue
            seen.add(id(first))
            found.append((name, first))
    for word in words:
        if id(word) in seen:
            continue
        guessed = _visible_fuzzy(word["text"])
        if guessed:
            seen.add(id(word))
            found.append((guessed, word))
    return found


def _rescue_option_words(image, line: dict) -> list[dict]:
    import pytesseract

    words = line["words"]
    if any(pattern.search(line["text"]) for _name, pattern in _VISIBLE_BUCKETS):
        return words
    tops = [word["y"] for word in words]
    bottoms = [word["y"] + word["h"] for word in words]
    y0 = max(0, min(tops) - 4)
    y1 = min(image.height, max(bottoms) + 4)
    if y1 - y0 < 12 or y1 - y0 > 150:
        return words
    bands = [(y0, y1)]
    if y1 - y0 > 40:
        bands.append((y0 + (y1 - y0) // 2, y1))
    extra = list(words)
    for top, bottom in bands:
        crop = image.crop((0, top, image.width, bottom))
        try:
            data = pytesseract.image_to_data(
                crop, lang="eng", config="--psm 6", output_type=pytesseract.Output.DICT
            )
        except Exception:
            continue
        for index, raw in enumerate(data["text"]):
            text = (raw or "").strip()
            if not text or int(data["conf"][index]) < 0:
                continue
            if not any(pattern.search(text) for _name, pattern in _VISIBLE_BUCKETS) and _visible_fuzzy(text) is None:
                continue
            extra.append(
                {
                    "text": text,
                    "x": int(data["left"][index]),
                    "y": top + int(data["top"][index]),
                    "w": int(data["width"][index]),
                    "h": max(8, int(data["height"][index])),
                }
            )
    return extra


def score_visible_page(image) -> str:
    grouped = _visible_lines(image)
    hear_at = next((index for index, line in enumerate(grouped) if _HEAR_RE.search(line["text"])), None)
    if hear_at is None:
        return "no_question"
    found: list[tuple[str, dict]] = []
    for line in grouped[hear_at:]:
        stopped = line is not grouped[hear_at] and (
            _VISIBLE_STOP_RE.search(line["text"])
            or (
                re.match(r"insurance\b", line["text"], re.I)
                and not re.search(r"recommend|seguro", line["text"], re.I)
            )
        )
        if stopped:
            break
        words = line["words"]
        if line is grouped[hear_at]:
            match = _HEAR_RE.search(line["text"])
            cut = match.end() if match else 0
            cursor = 0
            kept = []
            for word in words:
                if cursor >= cut:
                    kept.append(word)
                cursor += len(word["text"]) + 1
            words = kept
            line = {**line, "words": words, "text": " ".join(word["text"] for word in words)}
        words = _rescue_option_words(image, line)
        found.extend(_visible_options(words))
    heights = sorted(min(word["h"], 36) for _name, word in found)
    text_h = heights[len(heights) // 2] if heights else 18
    scores = [(name, _checkbox_fill(image, word, text_h)) for name, word in found]
    return choose_visible_mark(scores)


def refine_unmarked_source(path: Path, source: str) -> str:
    if source != "unmarked_options":
        return source
    try:
        import fitz
        from PIL import Image
    except Exception:
        return source
    try:
        doc = fitz.open(path)
    except Exception:
        return source
    try:
        for index, page in enumerate(doc):
            if index >= 4:
                break
            native = page.get_text() or ""
            if len(native) >= NATIVE_MIN_CHARS and not _HEAR_RE.search(native):
                continue
            pix = page.get_pixmap(matrix=fitz.Matrix(1.6, 1.6), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            picked = score_visible_page(image)
            if picked == "no_question":
                continue
            if picked == "unmarked":
                return source
            return picked
    except Exception:
        return source
    finally:
        doc.close()
    return source


def classify_one(path_str: str, tessdata: str, spa_ok: bool) -> dict[str, str]:
    path = Path(path_str)
    try:
        text, method = extract_pdf_text(path, tessdata, spa_ok)
        shape_id, shape_label = form_fingerprint(text)
        source = classify_source(text)
        if source == "unmarked_options":
            source = refine_unmarked_source(path, source)
        return {
            "path": path_str,
            "method": method,
            "language": detect_language(text),
            "shape_id": shape_id,
            "shape_label": shape_label,
            "source": source,
            "chars": str(len(text)),
            "error": "",
        }
    except Exception as exc:
        return {
            "path": path_str,
            "method": "error",
            "language": "unknown",
            "shape_id": "no_text",
            "shape_label": "no_text",
            "source": "unreadable",
            "chars": "0",
            "error": type(exc).__name__,
        }


def _worker(payload: tuple[str, str, bool]) -> dict[str, str]:
    path_str, tessdata, spa_ok = payload
    os.environ["TESSDATA_PREFIX"] = tessdata
    return classify_one(path_str, tessdata, spa_ok)


def classify_files(paths: list[Path], tessdata: str, spa_ok: bool) -> dict[str, dict[str, str]]:
    unique = list(dict.fromkeys(paths))
    results: dict[str, dict[str, str]] = {}
    if not unique:
        return results
    payloads = [(str(path), tessdata, spa_ok) for path in unique]
    workers = max(1, min(WORKERS, len(payloads)))
    done = 0
    if workers == 1:
        iterator = (_worker(item) for item in payloads)
        for row in iterator:
            results[row["path"]] = row
            done += 1
            if done == 1 or done % 25 == 0 or done == len(payloads):
                emit(f"classified={done}/{len(payloads)}")
        return results
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, item) for item in payloads]
        for future in as_completed(futures):
            row = future.result()
            results[row["path"]] = row
            done += 1
            if done == 1 or done % 25 == 0 or done == len(payloads):
                emit(f"classified={done}/{len(payloads)}")
    return results


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
    shape_patients: dict[str, set[str]] = {}
    shape_labels: dict[str, str] = {}
    method_counts: Counter[str] = Counter()
    detail_path = OUT_DIR / "patients.csv"
    with detail_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "webpt_patient_id",
                "has_intake",
                "file_count",
                "language",
                "shape_id",
                "shape_label",
                "source",
                "text_method",
            ],
        )
        writer.writeheader()
        for pid in sorted(patients):
            paths = intake_by_patient.get(pid, [])
            if not paths:
                writer.writerow(
                    {
                        "webpt_patient_id": pid,
                        "has_intake": "no",
                        "file_count": 0,
                        "language": "",
                        "shape_id": "",
                        "shape_label": "",
                        "source": "",
                        "text_method": "",
                    }
                )
                continue
            rows = [file_rows.get(str(path)) or {} for path in paths]
            language = merge_language([row.get("language", "") for row in rows])
            source = merge_source([row.get("source", "") for row in rows])
            primary = max(rows, key=lambda row: int(row.get("chars") or 0))
            shape_id = primary.get("shape_id") or "no_text"
            shape_label = primary.get("shape_label") or "no_text"
            methods = sorted({row.get("method") or "error" for row in rows})
            method = methods[0] if len(methods) == 1 else "mixed"
            language_counts[language] += 1
            source_counts[source] += 1
            method_counts[method] += 1
            shape_patients.setdefault(shape_id, set()).add(pid)
            shape_labels.setdefault(shape_id, shape_label)
            writer.writerow(
                {
                    "webpt_patient_id": pid,
                    "has_intake": "yes",
                    "file_count": len(paths),
                    "language": language,
                    "shape_id": shape_id,
                    "shape_label": shape_label,
                    "source": source,
                    "text_method": method,
                }
            )

    shapes = sorted(shape_patients.items(), key=lambda item: (-len(item[1]), item[0]))
    shape_report = [
        {
            "shape_id": shape_id,
            "label": shape_labels.get(shape_id, ""),
            "patients": len(pids),
        }
        for shape_id, pids in shapes
    ]
    summary = {
        "patients_total": len(patients),
        "with_intake": len(intake_by_patient),
        "without_intake": len(patients) - len(intake_by_patient),
        "intake_files": len(file_rows),
        "unmatched_intake_files": len(unmatched),
        "spa_tessdata": spa_ok,
        "edoc_roots": roots,
        "language": dict(language_counts),
        "source": dict(source_counts),
        "text_method": dict(method_counts),
        "shapes": shape_report,
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    emit("INTAKE_CENSUS_DONE")
    emit(f"patients_total={summary['patients_total']}")
    emit(f"with_intake={summary['with_intake']}")
    emit(f"without_intake={summary['without_intake']}")
    emit(f"intake_files={summary['intake_files']}")
    emit(f"unmatched_intake_files={summary['unmatched_intake_files']}")
    emit(f"spa_tessdata={int(spa_ok)}")
    for key in ("english", "spanish", "bilingual", "unknown"):
        emit(f"language_{key}={language_counts.get(key, 0)}")
    for key in (
        "zocdoc",
        "social_media",
        "google",
        "website",
        "doctor",
        "friend_family",
        "insurance",
        "walk_in",
        "phone",
        "other",
        "unmarked_options",
        "blank",
        "unreadable",
    ):
        emit(f"source_{key}={source_counts.get(key, 0)}")
    for key, count in method_counts.items():
        emit(f"text_method_{key}={count}")
    emit(f"shape_count={len(shape_report)}")
    for row in shape_report[:25]:
        label = row["label"].replace("\n", " ")
        emit(f"shape {row['shape_id']} patients={row['patients']} label={label}")
    if len(shape_report) > 25:
        rest = sum(row["patients"] for row in shape_report[25:])
        emit(f"shape_remaining={len(shape_report) - 25} patients={rest}")


def _self_check() -> int:
    english = (
        "Patient Intake Form\n"
        "Patient name Date of birth Address Phone\n"
        "How did you hear about us? Zocdoc\n"
        "Insurance Emergency contact Signature\n"
    )
    spanish = (
        "Formulario de admisión del paciente\n"
        "Nombre Apellido Fecha de nacimiento Teléfono Dirección\n"
        "Cómo se enteró de nosotros? Instagram\n"
        "Seguro médico Firma del paciente Historia\n"
    )
    options = (
        "New Patient Intake\nPatient name Date of birth\n"
        "How did you hear about us?\n"
        "[ ] Zocdoc\n[x] Facebook\n[ ] Google\n"
    )
    assert detect_language(english) == "english", detect_language(english)
    assert detect_language(spanish) == "spanish", detect_language(spanish)
    assert classify_source(english) == "zocdoc"
    assert classify_source(spanish) == "social_media"
    assert classify_source(options) == "social_media"
    assert classify_source("no question here, but booked on Zocdoc") == "zocdoc"
    assert classify_source("How did you hear about us?\n\n") == "blank"
    assert classify_source("How did you hear about us?\nPrimary insurance: Aetna\n") == "blank"
    checklist = (
        "Patient Intake\nHow did you hear about us?\n"
        "(*) Please check what applies\n"
        "Doctor's referral/recommendations\n"
        "Google\nZocdoc\nSocial Media\n"
    )
    marked_google = (
        "How did you hear about us?\n"
        "O Doctor referral\n@ Google\nO Zocdoc\nO Social Media\n"
    )
    noisy = checklist.replace("recommendations", "recomi endations")
    assert classify_source(checklist) == "unmarked_options"
    assert classify_source(marked_google) == "google"
    assert form_fingerprint(checklist)[0] == form_fingerprint(noisy)[0]
    assert detect_language("jj jojdop wos " * 40) == "unknown"
    shape_a, _ = form_fingerprint(english)
    shape_b, _ = form_fingerprint(spanish)
    assert shape_a != shape_b
    assert shape_a == form_fingerprint(english)[0]
    assert choose_visible_mark([("google", 0.13), ("doctor", 0.0)]) == "google"
    assert choose_visible_mark([("other", 1.0), ("friend_family", 0.74)]) == "other"
    assert choose_visible_mark([("friend_family", 0.29)]) == "friend_family"
    assert choose_visible_mark([("other", 0.81), ("zocdoc", 0.0)]) == "other"
    assert choose_visible_mark([("doctor", 0.26)]) == "doctor"
    assert choose_visible_mark([("doctor", 0.0), ("google", 0.0)]) == "unmarked"
    emit("self_check=ok")
    return 0


def main() -> int:
    try:
        os.nice(10)
    except (AttributeError, OSError):
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
    sys.exit(main())

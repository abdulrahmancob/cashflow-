"""Case artifact contract: versioning, audit, sources index, cleaned HTML, SHA256."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1"
PARSER_VERSION = "1.0.0"

AUDIT_FLAGS = (
    "download_complete",
    "raw_snapshot_complete",
    "parse_html_complete",
    "parse_json_complete",
    "payments_complete",
    "ocr_complete",
    "merge_complete",
    "export_complete",
)

_SCRIPT_RE = re.compile(
    r"<script\b[^>]*>.*?</script>",
    re.IGNORECASE | re.DOTALL,
)
_STYLE_RE = re.compile(
    r"<style\b[^>]*>.*?</style>",
    re.IGNORECASE | re.DOTALL,
)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def scraper_version() -> str:
    """Best-effort git describe; falls back to package marker."""
    try:
        out = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=str(Path(__file__).resolve().parent.parent),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        return (out or "").strip() or "unknown"
    except Exception:
        return "unknown"


def file_sha256(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def clean_html(html: str) -> str:
    """Strip script/style noise for stable offline parsing."""
    text = _SCRIPT_RE.sub("", html or "")
    text = _STYLE_RE.sub("", text)
    return text


def write_json(path: Path, obj: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=str), encoding="utf-8")
    return path


def read_json(path: Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def artifact_meta(
    *,
    endpoint: str,
    facility_id: str | int,
    case_id: str | int,
    parser_version: str = PARSER_VERSION,
    endpoint_version: str = "observed",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "scraper_version": scraper_version(),
        "parser_version": parser_version,
        "captured_at": _utc(),
        "endpoint": endpoint,
        "endpoint_version": endpoint_version,
        "facility_id": str(facility_id),
        "case_id": str(case_id),
    }
    if extra:
        meta.update(extra)
    return meta


def save_raw_text_with_meta(
    path: Path,
    text: str,
    *,
    facility_id: str | int,
    case_id: str | int,
    endpoint: str,
    also_cleaned: bool = False,
) -> dict[str, Path]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text or "", encoding="utf-8", errors="replace")
    meta_path = path.with_suffix(path.suffix + ".meta.json")
    if path.name.endswith(".html"):
        meta_path = path.with_name(path.name + ".meta.json")
    write_json(
        meta_path,
        artifact_meta(
            endpoint=endpoint,
            facility_id=facility_id,
            case_id=case_id,
        ),
    )
    out = {"raw": path, "meta": meta_path}
    if also_cleaned and path.suffix.lower() == ".html":
        cleaned = path.with_name(path.stem + ".cleaned.html")
        cleaned.write_text(clean_html(text or ""), encoding="utf-8", errors="replace")
        write_json(
            cleaned.with_name(cleaned.name + ".meta.json"),
            artifact_meta(
                endpoint=endpoint + "#cleaned",
                facility_id=facility_id,
                case_id=case_id,
            ),
        )
        out["cleaned"] = cleaned
    return out


def save_raw_json_with_meta(
    path: Path,
    obj: Any,
    *,
    facility_id: str | int,
    case_id: str | int,
    endpoint: str,
) -> dict[str, Path]:
    path = Path(path)
    write_json(path, obj)
    meta_path = path.with_name(path.name + ".meta.json")
    write_json(
        meta_path,
        artifact_meta(
            endpoint=endpoint,
            facility_id=facility_id,
            case_id=case_id,
        ),
    )
    return {"raw": path, "meta": meta_path}


def audit_path(case_dir: Path) -> Path:
    return Path(case_dir) / "audit.json"


def load_audit(case_dir: Path) -> dict[str, Any]:
    data = read_json(audit_path(case_dir))
    if not data:
        data = {flag: False for flag in AUDIT_FLAGS}
        data["errors"] = {}
        data["timestamps"] = {}
    return data


def update_audit(
    case_dir: Path,
    *,
    flag: str,
    value: bool = True,
    error: str = "",
) -> dict[str, Any]:
    data = load_audit(case_dir)
    if flag in AUDIT_FLAGS:
        data[flag] = value
        ts = data.setdefault("timestamps", {})
        if isinstance(ts, dict):
            ts[flag] = _utc()
    if error:
        errs = data.setdefault("errors", {})
        if isinstance(errs, dict):
            errs[flag] = error
    elif isinstance(data.get("errors"), dict) and flag in data["errors"]:
        data["errors"].pop(flag, None)
    write_json(audit_path(case_dir), data)
    return data


def build_case_sources(case_dir: Path) -> dict[str, Any]:
    """Index which sources exist without walking blindly each parse."""
    case_dir = Path(case_dir)
    raw = case_dir / "raw"
    payments = case_dir / "payments"
    parsed = case_dir / "parsed"
    manifests = case_dir / "manifests"

    def _exists(*parts: str) -> bool:
        return (case_dir.joinpath(*parts)).is_file()

    sources = {
        "patientChart": _exists("raw", "patientChart.html")
        or _exists("raw", "patientChart.cleaned.html"),
        "chart_notes": _exists("raw", "chart_notes.html"),
        "scheduler": _exists("raw", "scheduler.json"),
        "edoc_list": _exists("raw", "edoc_list.json"),
        "request_log": _exists("raw", "request_log.json"),
        "payments": _exists("payments", "payments.json")
        or _exists("payments", "transactions.csv"),
        "payments_summary": _exists("payments", "summary.json"),
        "manifest": _exists("manifests", "artifacts_manifest.csv"),
        "ocr_cache": _exists(".ocr_cache.txt"),
        "parsed_chart": _exists("parsed", "chart_fields.json"),
        "parsed_scheduler": _exists("parsed", "scheduler_fields.json"),
        "parsed_edoc": _exists("parsed", "edoc_meta.json"),
        "parsed_ocr": _exists("parsed", "ocr_fields.json"),
        "case_enrich": _exists("manifests", "case_enrich.json"),
        "paths": {
            "raw": str(raw) if raw.is_dir() else "",
            "payments": str(payments) if payments.is_dir() else "",
            "parsed": str(parsed) if parsed.is_dir() else "",
            "manifests": str(manifests) if manifests.is_dir() else "",
        },
        "updated_at": _utc(),
    }
    return sources


def write_case_sources(case_dir: Path) -> Path:
    sources = build_case_sources(case_dir)
    path = Path(case_dir) / "raw" / "case_sources.json"
    if not (case_dir / "raw").is_dir():
        path = Path(case_dir) / "case_sources.json"
    write_json(path, sources)
    # Also mirror at case root for quick lookup
    write_json(Path(case_dir) / "case_sources.json", sources)
    return path


def provenance_field(
    value: Any,
    *,
    source: str,
    confidence: float,
) -> dict[str, Any]:
    return {
        "value": "" if value is None else value,
        "source": source,
        "confidence": float(confidence),
    }


def flatten_provenance(fields: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Expand {k: {value,source,confidence}} → k, k_source, k_confidence."""
    out: dict[str, Any] = {}
    for key, spec in fields.items():
        if isinstance(spec, dict) and "value" in spec:
            out[key] = spec.get("value", "")
            out[f"{key}_source"] = spec.get("source", "")
            out[f"{key}_confidence"] = spec.get("confidence", "")
        else:
            out[key] = spec
    return out

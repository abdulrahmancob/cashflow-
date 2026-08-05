"""WebPT surface inventory — endpoints, fields, parser coverage mapping.

Built from live http_requests.jsonl and/or a dedicated surface probe.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

# Hint tokens for classifying / naming endpoints (not an allow-list for capture).
HINT_TOKENS = (
    "patientchart",
    "scheduler",
    "payments",
    "transaction",
    "documents",
    "graphql",
    "ajax",
    "clinicactions",
    "patient",
    "case",
    "insurance",
    "appointment",
    "notes",
    "authorization",
    "flowsheet",
    "visit",
    "billing",
    "attachments",
    "forms",
    "fax",
    "referral",
    "letters",
    "edoc",
    "printpdf",
    "getdocument",
    "extdoc",
    "display",
    "benefits",
    "chart",
)

STATIC_SUFFIXES = (
    ".js",
    ".css",
    ".gif",
    ".png",
    ".jpg",
    ".jpeg",
    ".woff",
    ".woff2",
    ".map",
    ".ico",
    ".svg",
)

# Known parser modules (currently_parsed_by).
PARSER_MAP: dict[str, str | None] = {
    "/patientchart.php": "patient_chart_api",
    "/patientchartnote.php": "chart_notes_api",
    "/patient/transaction/chart": "patient_payments_api",
    "/scheduler/index/data/t/e": "scheduler_api",
    "/scheduler/index/data/t/d": "scheduler_api",
    "/edoc/edoc/getdocumentspercase": "edoc_api",
    "/edoc/edoc/getalldocuments": None,  # golden_rule_forbidden for case drain
    "/printpdf.php": "chart_notes_download",
    "/viewextdoc.php": "edoc_download",
    "/graphql": None,
    "/patient/chart/benefitsstatus": None,
    "/patient/outbounddocument/faxoutbound": None,
    "/menu/index/getclinicactions/": None,
    "/patientextdoc.php": "edoc_api",
    "/authorization/": None,
    "/patient/display/getnewpatients": None,
    "/scheduler/index": None,
}

GOLDEN_RULE_FORBIDDEN = frozenset(
    {
        "/edoc/edoc/getalldocuments",
    }
)


@dataclass
class InventoryEndpoint:
    endpoint: str
    method: str
    url_sample: str = ""
    content_type: str = ""
    status_sample: int | None = None
    response_kind: str = "other"
    fields_discovered: list[str] = field(default_factory=list)
    sample_path: str = ""
    requires_patient: bool = False
    requires_case: bool = False
    seen_during: list[str] = field(default_factory=list)
    currently_parsed_by: str | None = None
    hit_count: int = 0
    golden_rule_forbidden: bool = False


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_endpoint(url_or_path: str) -> str:
    raw = (url_or_path or "").strip()
    if not raw:
        return ""
    if raw.startswith("http"):
        path = urlparse(raw).path or ""
    else:
        path = raw.split("?", 1)[0]
    path = path.rstrip("/") or "/"
    # Collapse numeric ids in path segments for fingerprinting
    parts = []
    for seg in path.split("/"):
        if re.fullmatch(r"\d+", seg or ""):
            parts.append("{id}")
        else:
            parts.append(seg)
    return "/".join(parts).lower() or "/"


def _is_static(endpoint: str) -> bool:
    ep = endpoint.lower()
    return any(ep.endswith(s) for s in STATIC_SUFFIXES)


def _looks_app_api(url: str, endpoint: str) -> bool:
    blob = f"{url} {endpoint}".lower()
    if _is_static(endpoint):
        return False
    return any(tok in blob for tok in HINT_TOKENS)


def _infer_requires(url: str) -> tuple[bool, bool]:
    q = parse_qs(urlparse(url).query)
    body_hints = url.lower()
    requires_patient = any(
        k.lower() in {"id", "patient", "pid", "patient_id", "patientid"}
        for k in q
    ) or "patient=" in body_hints
    requires_case = any(
        k.lower() in {"caseid", "case_id", "case"} for k in q
    ) or "caseid=" in body_hints or "case=" in body_hints
    return requires_patient, requires_case


def _response_kind(content_type: str, endpoint: str) -> str:
    ct = (content_type or "").lower()
    ep = endpoint.lower()
    if "json" in ct or ep.endswith("graphql") or "/graphql" in ep:
        return "json"
    if "html" in ct or ep.endswith(".php"):
        return "html"
    if "pdf" in ct or "octet" in ct or "printpdf" in ep:
        return "binary"
    return "other"


def _parser_for(endpoint: str) -> str | None:
    return PARSER_MAP.get(endpoint.lower())


def build_inventory_from_http_log(
    http_jsonl: Path,
    *,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """Aggregate app endpoints from forensics http_requests.jsonl."""
    by_key: dict[tuple[str, str], InventoryEndpoint] = {}
    phase_hits: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    lines = 0
    app_hits = 0

    with Path(http_jsonl).open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if max_lines is not None and lines >= max_lines:
                break
            lines += 1
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            url = str(row.get("url") or "")
            endpoint_raw = str(row.get("endpoint") or urlparse(url).path or "")
            endpoint = normalize_endpoint(endpoint_raw or url)
            if not endpoint or not _looks_app_api(url, endpoint):
                continue
            method = str(row.get("method") or "GET").upper()
            key = (method, endpoint)
            app_hits += 1
            phase = str(row.get("phase_name") or "other") or "other"
            phase_hits[key][phase] += 1
            status = row.get("status")
            try:
                status_i = int(status) if status is not None else None
            except (TypeError, ValueError):
                status_i = None

            if key not in by_key:
                req_p, req_c = _infer_requires(url)
                by_key[key] = InventoryEndpoint(
                    endpoint=endpoint,
                    method=method,
                    url_sample=url[:500],
                    status_sample=status_i,
                    response_kind=_response_kind("", endpoint),
                    requires_patient=req_p,
                    requires_case=req_c,
                    currently_parsed_by=_parser_for(endpoint),
                    golden_rule_forbidden=endpoint in GOLDEN_RULE_FORBIDDEN,
                    hit_count=1,
                )
            else:
                ep = by_key[key]
                ep.hit_count += 1
                if status_i and not ep.status_sample:
                    ep.status_sample = status_i
                if url and not ep.url_sample:
                    ep.url_sample = url[:500]
                rp, rc = _infer_requires(url)
                ep.requires_patient = ep.requires_patient or rp
                ep.requires_case = ep.requires_case or rc

    endpoints: list[dict[str, Any]] = []
    for key, ep in sorted(by_key.items(), key=lambda kv: (-kv[1].hit_count, kv[0][1])):
        phases = [p for p, _ in phase_hits[key].most_common()]
        ep.seen_during = phases or ["other"]
        endpoints.append(asdict(ep))

    return {
        "generated_at": _utc(),
        "source": str(http_jsonl),
        "http_lines_scanned": lines,
        "app_hits": app_hits,
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
        "notes": [
            "Inventory from live drain http_requests.jsonl (observer forensics).",
            "getalldocuments is golden_rule_forbidden for case-scoped drain.",
            "fields_discovered filled by surface probe / unknown-fields analyzer.",
        ],
    }


def merge_fields_into_inventory(
    inventory: dict[str, Any],
    *,
    endpoint: str,
    method: str,
    fields: list[str],
    sample_path: str = "",
    content_type: str = "",
    response_kind: str = "",
    url_sample: str = "",
) -> dict[str, Any]:
    """Attach discovered fields to a matching endpoint entry (or create one)."""
    ep_norm = normalize_endpoint(endpoint)
    method_u = method.upper()
    endpoints: list[dict[str, Any]] = list(inventory.get("endpoints") or [])
    found = None
    for row in endpoints:
        if row.get("endpoint") == ep_norm and str(row.get("method", "")).upper() == method_u:
            found = row
            break
    if found is None:
        found = asdict(
            InventoryEndpoint(
                endpoint=ep_norm,
                method=method_u,
                url_sample=url_sample,
                content_type=content_type,
                response_kind=response_kind or _response_kind(content_type, ep_norm),
                currently_parsed_by=_parser_for(ep_norm),
                golden_rule_forbidden=ep_norm in GOLDEN_RULE_FORBIDDEN,
            )
        )
        endpoints.append(found)
    existing = list(found.get("fields_discovered") or [])
    for f in fields:
        if f and f not in existing:
            existing.append(f)
    found["fields_discovered"] = existing
    if sample_path:
        found["sample_path"] = sample_path
    if content_type:
        found["content_type"] = content_type
    if response_kind:
        found["response_kind"] = response_kind
    if url_sample and not found.get("url_sample"):
        found["url_sample"] = url_sample
    inventory["endpoints"] = endpoints
    inventory["endpoint_count"] = len(endpoints)
    inventory["generated_at"] = _utc()
    return inventory


def inventory_to_markdown(inventory: dict[str, Any]) -> str:
    lines = [
        "# WebPT Surface Inventory",
        "",
        f"Generated: `{inventory.get('generated_at', '')}`",
        f"Source: `{inventory.get('source', '')}`",
        f"Endpoints: **{inventory.get('endpoint_count', 0)}** "
        f"(app hits: {inventory.get('app_hits', 0)})",
        "",
        "| Method | Endpoint | Hits | Parsed by | Forbidden | Seen during | Fields |",
        "|--------|----------|------|-----------|-----------|-------------|--------|",
    ]
    for ep in inventory.get("endpoints") or []:
        fields = ep.get("fields_discovered") or []
        field_preview = ", ".join(fields[:8])
        if len(fields) > 8:
            field_preview += f" (+{len(fields) - 8})"
        lines.append(
            "| {method} | `{endpoint}` | {hits} | {parsed} | {forbid} | {seen} | {fields} |".format(
                method=ep.get("method", ""),
                endpoint=ep.get("endpoint", ""),
                hits=ep.get("hit_count", 0),
                parsed=ep.get("currently_parsed_by") or "—",
                forbid="yes" if ep.get("golden_rule_forbidden") else "",
                seen=", ".join((ep.get("seen_during") or [])[:3]),
                fields=field_preview or "—",
            )
        )
    lines.append("")
    for note in inventory.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def write_inventory(inventory: dict[str, Any], reports_dir: Path) -> tuple[Path, Path]:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    json_path = reports_dir / "webpt_inventory.json"
    md_path = reports_dir / "webpt_inventory.md"
    json_path.write_text(json.dumps(inventory, indent=2), encoding="utf-8")
    md_path.write_text(inventory_to_markdown(inventory), encoding="utf-8")
    return json_path, md_path


def load_inventory(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))

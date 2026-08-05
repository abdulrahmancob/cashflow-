"""Canonical payer registry: resolve raw names across WebPT / RevFlow / Tracker."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - optional dependency
    yaml = None  # type: ignore[assignment]


DEFAULT_REGISTRY_PATH = Path(__file__).with_name("payer_registry.yaml")

VALID_SOURCES = frozenset({"webpt", "revflow", "tracker", "any"})

_PROVIDER_NUMBER_RE = re.compile(
    r"\s*provider\s*number\s*:?\s*.*$",
    re.IGNORECASE,
)
_ACH_STOP_TOKENS = frozenset(
    {
        "ACH",
        "CCD",
        "PPD",
        "ORIG",
        "ORIGINATOR",
        "CREDIT",
        "DEBIT",
        "PAYMENT",
        "PAYMENTS",
        "EFT",
        "TRN",
        "WEB",
        "TEL",
        "DES",
        "INDN",
        "ID",
        "CO",
        "PMT",
        "INFO",
    }
)

# NACHA TRN segment: TRN*1*<reference>*<orig company id>
_TRN_EFT_RE = re.compile(r"TRN\*1\*([^*\s\\]+)", re.IGNORECASE)

ACH_PROCESSOR_CODE_PREFIX = "ACH_PROCESSOR_"


@dataclass(frozen=True)
class AliasRule:
    source: str
    pattern: str | None
    exact: str | None
    product_class: str | None
    regex: re.Pattern[str] | None
    exact_norm: str | None


@dataclass(frozen=True)
class PayerOrg:
    code: str
    name: str
    revflow_payors: tuple[str, ...]
    aliases: tuple[AliasRule, ...]


@dataclass(frozen=True)
class ResolvedPayer:
    code: str
    name: str
    product_class: str | None
    matched_alias: str
    source: str
    raw_name: str
    normalized: str


@dataclass(frozen=True)
class PayerRegistry:
    orgs: tuple[PayerOrg, ...]
    path: Path


def normalize_raw(value: str) -> str:
    """Strip WebPT provider-number noise and collapse punctuation/whitespace."""
    text = (value or "").strip()
    if not text:
        return ""
    text = _PROVIDER_NUMBER_RE.sub("", text).strip()
    text = text.replace("/", " ").replace("-", " ").replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_ach_payer_head(description: str) -> str:
    """Pull the ACH originator head from a Transaction Tracker Description."""
    text = (description or "").strip()
    if not text:
        return ""
    # Common ACH shape: "NYNM DES:NYNM PMT ID:…" → stop before DES:/INDN:
    des_split = re.split(r"\s+DES:", text, maxsplit=1, flags=re.IGNORECASE)
    if len(des_split) > 1 and des_split[0].strip():
        text = des_split[0].strip()
    # Drop leading date/check noise if present.
    tokens = re.split(r"\s+", text)
    head: list[str] = []
    for token in tokens:
        # Token may be "DES:NYNM" if split missed
        if re.match(r"(?i)DES:", token) or re.match(r"(?i)INDN:", token):
            break
        upper = re.sub(r"[^A-Za-z0-9]", "", token).upper()
        if not upper:
            continue
        if upper in _ACH_STOP_TOKENS:
            break
        if re.fullmatch(r"\d{5,}", upper):
            break
        if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", token):
            break
        head.append(token)
        if len(head) >= 4:
            break
    return " ".join(head).strip()


def extract_eft_refs_from_description(description: str) -> list[str]:
    """Extract check/EFT refs from ACH description TRN*1*{id} segments.

    Echo/PayPlus Tracker lines often leave EFT columns blank while the bank
    description carries the remit reference in a NACHA TRN segment.
    """
    text = description or ""
    refs: list[str] = []
    seen: set[str] = set()
    for match in _TRN_EFT_RE.finditer(text):
        raw = (match.group(1) or "").strip()
        if not raw:
            continue
        key = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        if key.isdigit():
            key = key.lstrip("0") or "0"
        if not key or key in seen:
            continue
        seen.add(key)
        refs.append(raw)
    return refs


def is_ach_processor(resolved: ResolvedPayer | PayerOrg | str | None) -> bool:
    """True when the registry entry is an ACH processor, not an insurer."""
    if resolved is None:
        return False
    if isinstance(resolved, str):
        code = resolved
        name = resolved
    else:
        code = resolved.code
        name = resolved.name
    code_u = (code or "").strip().upper()
    name_u = (name or "").strip().upper()
    return code_u.startswith(ACH_PROCESSOR_CODE_PREFIX) or name_u.startswith(
        "ACH PROCESSOR"
    )


def _compile_alias(raw: dict) -> AliasRule | None:
    source = str(raw.get("source") or "").strip().lower()
    if source not in {"webpt", "revflow", "tracker"}:
        return None
    exact = str(raw.get("exact") or "").strip() or None
    pattern = str(raw.get("pattern") or "").strip() or None
    if not exact and not pattern:
        return None
    product = raw.get("product_class")
    product_class = str(product).strip() if product not in (None, "") else None
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None
    exact_norm = normalize_raw(exact).upper() if exact else None
    return AliasRule(
        source=source,
        pattern=pattern,
        exact=exact,
        product_class=product_class,
        regex=regex,
        exact_norm=exact_norm,
    )


def _load_orgs(data: dict) -> tuple[PayerOrg, ...]:
    orgs: list[PayerOrg] = []
    for item in data.get("payer_orgs") or []:
        code = str(item.get("code") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if not code or not name:
            continue
        revflow = tuple(
            str(p).strip() for p in (item.get("revflow_payors") or []) if str(p).strip()
        )
        aliases = tuple(
            alias
            for raw in (item.get("aliases") or [])
            if (alias := _compile_alias(raw)) is not None
        )
        orgs.append(
            PayerOrg(code=code, name=name, revflow_payors=revflow, aliases=aliases)
        )
    return tuple(orgs)


def load_registry(path: Path | None = None) -> PayerRegistry:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    data = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    return PayerRegistry(orgs=_load_orgs(data), path=registry_path)


@lru_cache(maxsize=4)
def _cached_registry(path_str: str) -> PayerRegistry:
    return load_registry(Path(path_str))


def get_registry(path: Path | None = None) -> PayerRegistry:
    registry_path = Path(path) if path is not None else DEFAULT_REGISTRY_PATH
    return _cached_registry(str(registry_path.resolve()))


def clear_registry_cache() -> None:
    _cached_registry.cache_clear()


def _alias_matches(alias: AliasRule, normalized: str, lowered: str) -> bool:
    if alias.exact_norm and normalized.upper() == alias.exact_norm:
        return True
    if alias.regex is not None and alias.regex.search(lowered):
        return True
    return False


def resolve(
    raw_name: str,
    source: str = "any",
    *,
    registry: PayerRegistry | None = None,
) -> ResolvedPayer | None:
    """Resolve a raw payer string to a canonical payer_org.

    ``source`` may be webpt / revflow / tracker / any.
    First matching org wins (registry order); within an org, more specific
    aliases should be listed before broad ones.
    """
    source_key = (source or "any").strip().lower()
    if source_key not in VALID_SOURCES:
        raise ValueError(f"Unsupported source: {source!r}")

    normalized = normalize_raw(raw_name)
    if not normalized:
        return None
    lowered = normalized.lower()
    reg = registry or get_registry()

    for org in reg.orgs:
        for alias in org.aliases:
            if source_key != "any" and alias.source != source_key:
                continue
            if not _alias_matches(alias, normalized, lowered):
                continue
            matched = alias.exact or alias.pattern or ""
            return ResolvedPayer(
                code=org.code,
                name=org.name,
                product_class=alias.product_class,
                matched_alias=matched,
                source=alias.source,
                raw_name=(raw_name or "").strip(),
                normalized=normalized,
            )
    return None


def resolve_best(
    raw_name: str,
    sources: Iterable[str] = ("webpt", "revflow", "tracker"),
    *,
    registry: PayerRegistry | None = None,
) -> ResolvedPayer | None:
    """Try resolve with an explicit source order, then fall back to any."""
    for source in sources:
        hit = resolve(raw_name, source, registry=registry)
        if hit is not None:
            return hit
    return resolve(raw_name, "any", registry=registry)


def resolve_tracker_description(
    description: str,
    *,
    registry: PayerRegistry | None = None,
) -> ResolvedPayer | None:
    head = extract_ach_payer_head(description)
    if not head:
        return None
    return resolve(head, "tracker", registry=registry) or resolve(
        head, "any", registry=registry
    )


def insurance_rules_from_registry(
    registry: PayerRegistry | None = None,
) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Return (webpt_patterns, revflow_payors) pairs for insurance_map bridging."""
    reg = registry or get_registry()
    pairs: list[tuple[tuple[str, ...], tuple[str, ...]]] = []
    for org in reg.orgs:
        patterns = tuple(
            a.pattern or (f"^{re.escape(a.exact)}$" if a.exact else "")
            for a in org.aliases
            if a.source == "webpt" and (a.pattern or a.exact)
        )
        patterns = tuple(p for p in patterns if p)
        if not patterns:
            continue
        # Empty revflow_payors (self-pay) still useful as an explicit mapped rule.
        pairs.append((patterns, org.revflow_payors))
    return pairs


def org_by_code(
    code: str,
    *,
    registry: PayerRegistry | None = None,
) -> PayerOrg | None:
    key = (code or "").strip().upper()
    if not key:
        return None
    reg = registry or get_registry()
    for org in reg.orgs:
        if org.code == key:
            return org
    return None

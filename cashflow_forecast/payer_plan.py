"""Resolve payer_plan keys (ins_name + org + product_class) with hierarchy fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass

from cashflow_reconcile.payer_registry import ResolvedPayer, normalize_raw, resolve

_ZAYA_NOISE = re.compile(r"\s*\(?\s*zaya\s*\)?\s*", re.IGNORECASE)


@dataclass(frozen=True)
class PayerPlanKey:
    """Canonical plan grain for payment models, capacity, and packing."""

    plan_key: str  # normalized ins_name (primary)
    ins_name: str
    org_code: str
    product_class: str
    class_key: str  # "{org}|{product_class}" or ""
    org_key: str  # org_code lower, or ""

    @property
    def hierarchy(self) -> tuple[str, ...]:
        """Lookup order: plan → (org, class) → org."""
        keys: list[str] = []
        if self.plan_key:
            keys.append(f"plan:{self.plan_key}")
        if self.class_key:
            keys.append(f"class:{self.class_key}")
        if self.org_key:
            keys.append(f"org:{self.org_key}")
        return tuple(keys)


def normalize_ins_name(ins_name: str) -> str:
    text = normalize_raw(ins_name or "")
    text = _ZAYA_NOISE.sub(" ", text)
    # Drop replacement/mojibake chars from CSV encoding issues
    text = re.sub(r"[^\w\s.&+/'-]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def infer_product_class(ins_name: str, resolved: ResolvedPayer | None) -> str:
    if resolved and resolved.product_class:
        return str(resolved.product_class).strip().lower()
    lowered = (ins_name or "").lower()
    if re.search(r"workers?\s*comp|\bwc\b|worker's", lowered):
        return "wc"
    if "medicaid" in lowered or "mltc" in lowered or "harp" in lowered:
        return "medicaid"
    if "medicare" in lowered or "dsnp" in lowered or "mapd" in lowered:
        return "medicare"
    if re.search(r"\bppo\b", lowered):
        return "ppo"
    if "commercial" in lowered or "choice" in lowered:
        return "commercial"
    return ""


def resolve_payer_plan(
    ins_name: str,
    *,
    insurance_revflow: str = "",
    source: str = "webpt",
) -> PayerPlanKey:
    """Build a PayerPlanKey from WebPT ins_name (preferred) + optional RevFlow payor."""
    ins = (ins_name or "").strip()
    rev = (insurance_revflow or "").strip()
    if rev.lower() == "nan":
        rev = ""

    resolved = resolve(ins, source) if ins else None
    if resolved is None and rev:
        resolved = resolve(rev, "revflow") or resolve(rev, "any")

    plan_key = normalize_ins_name(ins) or normalize_ins_name(rev)
    org_code = (resolved.code if resolved else "").strip().upper()
    product_class = infer_product_class(ins or rev, resolved)
    class_key = (
        f"{org_code.lower()}|{product_class}" if org_code and product_class else ""
    )
    org_key = org_code.lower() if org_code else ""

    return PayerPlanKey(
        plan_key=plan_key,
        ins_name=ins or rev,
        org_code=org_code,
        product_class=product_class,
        class_key=class_key,
        org_key=org_key,
    )


def pick_hierarchy_value(
    key: PayerPlanKey,
    table: dict[str, object],
    *,
    min_n: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
) -> tuple[object | None, str]:
    """Return (value, grain) for the finest hierarchy level that exists (and passes min_n)."""
    mins = min_n or {}
    cnts = counts or {}
    mapping = (
        ("plan", f"plan:{key.plan_key}" if key.plan_key else ""),
        ("class", f"class:{key.class_key}" if key.class_key else ""),
        ("org", f"org:{key.org_key}" if key.org_key else ""),
    )
    for grain, lookup_key in mapping:
        if not lookup_key or lookup_key not in table:
            continue
        need = mins.get(grain, 0)
        if need and cnts.get(lookup_key, 0) < need:
            continue
        return table[lookup_key], grain
    return None, ""

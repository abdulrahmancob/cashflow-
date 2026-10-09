"""Learn and apply per-payer_plan reimbursement models from recon/RevFlow history."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pandas as pd

from cashflow_forecast.config import (
    FLAT_VISIT_MODE_SHARE,
    FLAT_VISIT_TOLERANCE,
    MIN_CLASS_SAMPLES,
    MIN_ORG_SAMPLES,
    MIN_PLAN_SAMPLES,
)
from cashflow_forecast.fee_estimator import FeeEstimator
from cashflow_forecast.payer_plan import (
    PayerPlanKey,
    pick_hierarchy_value,
    resolve_payer_plan,
)


MODEL_FLAT = "flat_per_visit"
MODEL_FLAT_ADDERS = "flat_per_visit_plus_adders"
MODEL_PCT_BILLED = "percent_of_billed"
MODEL_PCT_ALLOWED = "percent_of_allowed"
MODEL_CPT = "cpt_schedule"
MODEL_HYBRID = "hybrid"


@dataclass
class PaymentModel:
    grain_key: str
    grain: str  # plan | class | org | global
    model_type: str
    flat_amount: float = 0.0
    percent: float = 0.0
    adders: dict[str, float] = field(default_factory=dict)
    n_visits: int = 0
    notes: str = ""

    def estimate_visit(
        self,
        *,
        cpt_codes: list[str],
        billed_total: float = 0.0,
        allowed_total: float = 0.0,
        fee_estimator: FeeEstimator | None = None,
        ins_name: str = "",
        units_by_cpt: dict[str, float] | None = None,
    ) -> float:
        units_by_cpt = units_by_cpt or {}
        if self.model_type in (MODEL_FLAT, MODEL_FLAT_ADDERS):
            total = float(self.flat_amount)
            if self.model_type == MODEL_FLAT_ADDERS:
                seen = {str(c).strip() for c in cpt_codes if str(c).strip()}
                for cpt, adder in self.adders.items():
                    if cpt in seen:
                        total += float(adder)
            return round(max(total, 0.0), 2)
        if self.model_type == MODEL_PCT_BILLED and billed_total > 0 and self.percent > 0:
            return round(billed_total * self.percent, 2)
        if self.model_type == MODEL_PCT_ALLOWED and allowed_total > 0 and self.percent > 0:
            return round(allowed_total * self.percent, 2)
        # cpt_schedule / hybrid / fallback
        if fee_estimator is None:
            return round(float(self.flat_amount) or 50.0, 2)
        total = 0.0
        for cpt in cpt_codes:
            cpt_s = str(cpt).strip()
            if not cpt_s:
                continue
            units = float(units_by_cpt.get(cpt_s, 1.0) or 1.0)
            total += fee_estimator.estimate(cpt_s, ins_name) * units
        if total <= 0 and self.flat_amount > 0:
            return round(self.flat_amount, 2)
        return round(total, 2)


@dataclass
class PaymentModelCatalog:
    models: dict[str, PaymentModel]
    counts: dict[str, int]
    fee_estimator: FeeEstimator

    def resolve_model(self, key: PayerPlanKey) -> tuple[PaymentModel, str]:
        hit, grain = pick_hierarchy_value(
            key,
            self.models,  # type: ignore[arg-type]
            min_n={
                "plan": MIN_PLAN_SAMPLES,
                "class": MIN_CLASS_SAMPLES,
                "org": MIN_ORG_SAMPLES,
            },
            counts=self.counts,
        )
        if hit is not None:
            return hit, grain  # type: ignore[return-value]
        # Relaxed: ignore min_n, take finest available
        hit, grain = pick_hierarchy_value(key, self.models)  # type: ignore[arg-type]
        if hit is not None:
            return hit, grain  # type: ignore[return-value]
        return (
            PaymentModel(
                grain_key="global",
                grain="global",
                model_type=MODEL_CPT,
                flat_amount=self.fee_estimator._global,
                n_visits=0,
                notes="global_cpt_fallback",
            ),
            "global",
        )

    def estimate_for_lines(
        self,
        lines: list[dict],
        *,
        ins_name: str,
        insurance_revflow: str = "",
    ) -> float:
        key = resolve_payer_plan(ins_name, insurance_revflow=insurance_revflow)
        model, _ = self.resolve_model(key)
        cpts = [str(r.get("cpt_code") or "") for r in lines]
        units = {
            str(r.get("cpt_code") or "").strip(): float(r.get("units") or 1) or 1.0
            for r in lines
            if str(r.get("cpt_code") or "").strip()
        }
        billed = sum(float(r.get("billed_amount") or 0) for r in lines)
        allowed = sum(float(r.get("allowed_amount") or 0) for r in lines)
        return model.estimate_visit(
            cpt_codes=cpts,
            billed_total=billed,
            allowed_total=allowed,
            fee_estimator=self.fee_estimator,
            ins_name=ins_name,
            units_by_cpt=units,
        )


class _VisitAgg:
    """One paid visit, with the same totals the row-wise model used."""

    __slots__ = ("paid_total", "billed", "allowed", "paid_by_cpt")

    def __init__(
        self,
        paid_total: float,
        billed: float,
        allowed: float,
        paid_by_cpt: dict[str, float],
    ) -> None:
        self.paid_total = paid_total
        self.billed = billed
        self.allowed = allowed
        self.paid_by_cpt = paid_by_cpt


def _visit_paid_total(group: pd.DataFrame | _VisitAgg) -> float:
    if isinstance(group, _VisitAgg):
        return group.paid_total
    if "visit_paid_total" in group.columns:
        vals = pd.to_numeric(group["visit_paid_total"], errors="coerce").dropna()
        if not vals.empty:
            return float(vals.iloc[0])
    return float(pd.to_numeric(group.get("paid_amount"), errors="coerce").fillna(0).sum())


def _billed_allowed(group: pd.DataFrame | _VisitAgg) -> tuple[float, float]:
    if isinstance(group, _VisitAgg):
        return group.billed, group.allowed
    billed = float(pd.to_numeric(group.get("billed_amount"), errors="coerce").fillna(0).sum())
    allowed = float(pd.to_numeric(group.get("allowed_amount"), errors="coerce").fillna(0).sum())
    return billed, allowed


def _mode_amount(values: list[float], tol: float = FLAT_VISIT_TOLERANCE) -> tuple[float, float]:
    """Return (mode_center, share_within_tol)."""
    if not values:
        return 0.0, 0.0
    rounded = [round(v, 0) for v in values]  # dollar buckets
    mode_val, mode_n = Counter(rounded).most_common(1)[0]
    within = sum(1 for v in values if abs(v - mode_val) <= tol)
    return float(mode_val), within / len(values)


def _detect_adders(
    visit_groups: list[pd.DataFrame] | list[_VisitAgg],
    flat_amount: float,
) -> dict[str, float]:
    """Find CPT codes that systematically add ~constant amount above the flat visit fee."""
    extras: dict[str, list[float]] = defaultdict(list)
    for g in visit_groups:
        if isinstance(g, _VisitAgg):
            paid_map = g.paid_by_cpt
            visit_total = sum(paid_map.values())
            if visit_total <= flat_amount + FLAT_VISIT_TOLERANCE:
                continue
            for cpt, amt in paid_map.items():
                if abs(amt - flat_amount) <= FLAT_VISIT_TOLERANCE:
                    continue
                if amt >= 10:
                    extras[cpt].append(amt)
            continue
        paid_by_cpt = (
            g.groupby(g["cpt_code"].astype(str).str.strip(), dropna=False)["paid_amount"]
            .sum()
            if "cpt_code" in g.columns
            else {}
        )
        if isinstance(paid_by_cpt, pd.Series):
            paid_map = {
                str(k).strip(): float(v)
                for k, v in paid_by_cpt.items()
                if str(k).strip() and float(v) > 0
            }
        else:
            paid_map = {}
        visit_total = sum(paid_map.values())
        if visit_total <= flat_amount + FLAT_VISIT_TOLERANCE:
            continue
        # Candidate adders: positive paid CPTs that aren't the bulk of the flat
        for cpt, amt in paid_map.items():
            if abs(amt - flat_amount) <= FLAT_VISIT_TOLERANCE:
                continue
            if amt >= 10:  # ignore tiny noise
                extras[cpt].append(amt)
    adders: dict[str, float] = {}
    n_visits = max(len(visit_groups), 1)
    for cpt, amts in extras.items():
        if len(amts) / n_visits >= 0.05 and len(amts) >= 3:
            adders[cpt] = float(median(amts))
    return adders


def _infer_model_for_visits(
    grain_key: str,
    grain: str,
    visit_groups: list[pd.DataFrame],
) -> PaymentModel | None:
    if len(visit_groups) < 3:
        return None
    paid_totals = [_visit_paid_total(g) for g in visit_groups]
    paid_totals = [p for p in paid_totals if p > 0]
    if len(paid_totals) < 3:
        return None

    mode_amt, share = _mode_amount(paid_totals)
    if share >= FLAT_VISIT_MODE_SHARE and mode_amt > 0:
        adders = _detect_adders(visit_groups, mode_amt)
        if adders:
            return PaymentModel(
                grain_key=grain_key,
                grain=grain,
                model_type=MODEL_FLAT_ADDERS,
                flat_amount=mode_amt,
                adders=adders,
                n_visits=len(paid_totals),
                notes=f"flat={mode_amt};adders={sorted(adders)}",
            )
        return PaymentModel(
            grain_key=grain_key,
            grain=grain,
            model_type=MODEL_FLAT,
            flat_amount=mode_amt,
            n_visits=len(paid_totals),
            notes=f"flat_mode_share={share:.2f}",
        )

    # Bimodal flat + adder: e.g. 1199 at $50 or $100 (= $50 + $50 adder CPT)
    rounded = [round(v, 0) for v in paid_totals]
    top2 = Counter(rounded).most_common(2)
    if len(top2) == 2:
        (m1, n1), (m2, n2) = top2
        lo, hi = (m1, m2) if m1 <= m2 else (m2, m1)
        combined = (n1 + n2) / len(rounded)
        if (
            combined >= FLAT_VISIT_MODE_SHARE
            and lo > 0
            and abs(hi - 2 * lo) <= FLAT_VISIT_TOLERANCE + 1
        ):
            adders = _detect_adders(visit_groups, lo)
            if not adders:
                # Infer a generic adder of (hi-lo) on the most common extra paid CPT
                adders = _detect_adders(visit_groups, lo) or {}
            return PaymentModel(
                grain_key=grain_key,
                grain=grain,
                model_type=MODEL_FLAT_ADDERS if adders else MODEL_FLAT,
                flat_amount=float(lo),
                adders=adders,
                n_visits=len(paid_totals),
                notes=f"bimodal_flat={lo}/{hi};adders={sorted(adders)}",
            )

    # Percent of billed / allowed at visit level
    ratios_billed: list[float] = []
    ratios_allowed: list[float] = []
    for g in visit_groups:
        paid = _visit_paid_total(g)
        billed, allowed = _billed_allowed(g)
        if paid > 0 and billed > 0:
            ratios_billed.append(paid / billed)
        if paid > 0 and allowed > 0:
            ratios_allowed.append(paid / allowed)

    def _stable_ratio(ratios: list[float]) -> float | None:
        if len(ratios) < 5:
            return None
        med = float(median(ratios))
        if med <= 0 or med > 1.5:
            return None
        within = sum(1 for r in ratios if abs(r - med) <= 0.15) / len(ratios)
        return med if within >= 0.6 else None

    rb = _stable_ratio(ratios_billed)
    if rb is not None:
        return PaymentModel(
            grain_key=grain_key,
            grain=grain,
            model_type=MODEL_PCT_BILLED,
            percent=rb,
            flat_amount=float(median(paid_totals)),
            n_visits=len(paid_totals),
            notes=f"pct_billed={rb:.3f}",
        )
    ra = _stable_ratio(ratios_allowed)
    if ra is not None:
        return PaymentModel(
            grain_key=grain_key,
            grain=grain,
            model_type=MODEL_PCT_ALLOWED,
            percent=ra,
            flat_amount=float(median(paid_totals)),
            n_visits=len(paid_totals),
            notes=f"pct_allowed={ra:.3f}",
        )

    # CPT schedule: line-level variance by CPT
    return PaymentModel(
        grain_key=grain_key,
        grain=grain,
        model_type=MODEL_CPT,
        flat_amount=float(median(paid_totals)),
        n_visits=len(paid_totals),
        notes="cpt_schedule",
    )


def _group_paid_visits(lines: pd.DataFrame) -> dict[str, list[pd.DataFrame]]:
    """Map hierarchy keys → list of per-visit dataframes (paid only)."""
    if lines is None or lines.empty:
        return {}
    df = lines.copy()
    if "status" in df.columns:
        df = df[df["status"].astype(str).str.lower() == "paid"].copy()
    df["paid_amount"] = pd.to_numeric(df.get("paid_amount"), errors="coerce").fillna(0)
    df = df[df["paid_amount"] > 0]
    if df.empty:
        return {}

    if "webpt_patient_id" not in df.columns:
        df["webpt_patient_id"] = ""
    if "date_of_service" not in df.columns:
        return {}

    buckets: dict[str, list[pd.DataFrame]] = defaultdict(list)
    for (_, _, _), group in df.groupby(
        ["webpt_patient_id", "date_of_service", "ins_name"], dropna=False
    ):
        ins = str(group["ins_name"].iloc[0] if "ins_name" in group.columns else "")
        rev = ""
        if "insurance_revflow" in group.columns:
            rev = str(group["insurance_revflow"].iloc[0] or "")
        elif "payor" in group.columns:
            rev = str(group["payor"].iloc[0] or "")
        key = resolve_payer_plan(ins, insurance_revflow=rev)
        g = group.copy()
        for hkey in key.hierarchy:
            buckets[hkey].append(g)
    return buckets


def _group_paid_visit_aggs(lines: pd.DataFrame) -> dict[str, list[_VisitAgg]]:
    """Same buckets as ``_group_paid_visits``, without a DataFrame per visit."""
    if lines is None or lines.empty:
        return {}
    df = lines
    if "status" in df.columns:
        df = df.loc[df["status"].astype(str).str.lower() == "paid"]
    if df.empty or "date_of_service" not in df.columns:
        return {}
    paid = pd.to_numeric(
        df["paid_amount"] if "paid_amount" in df.columns else 0, errors="coerce"
    ).fillna(0)
    df = df.assign(_paid=paid)
    df = df.loc[df["_paid"] > 0]
    if df.empty:
        return {}
    if "webpt_patient_id" not in df.columns:
        df = df.assign(webpt_patient_id="")
    if "ins_name" not in df.columns:
        df = df.assign(ins_name="")
    df = df.copy()
    df["_row"] = range(len(df))
    df["_billed"] = (
        pd.to_numeric(df["billed_amount"], errors="coerce").fillna(0)
        if "billed_amount" in df.columns
        else 0.0
    )
    df["_allowed"] = (
        pd.to_numeric(df["allowed_amount"], errors="coerce").fillna(0)
        if "allowed_amount" in df.columns
        else 0.0
    )
    if "cpt_code" in df.columns:
        df["_cpt"] = df["cpt_code"].map(lambda value: "" if pd.isna(value) else str(value).strip())
    else:
        df["_cpt"] = ""
    keys = ["webpt_patient_id", "date_of_service", "ins_name"]
    df["_gid"] = df.groupby(keys, dropna=False, sort=True).ngroup()
    grouped = df.groupby("_gid", sort=True)
    line_sum = grouped["_paid"].sum()
    billed = grouped["_billed"].sum()
    allowed = grouped["_allowed"].sum()
    if "visit_paid_total" in df.columns:
        df["_vpt"] = pd.to_numeric(df["visit_paid_total"], errors="coerce")
        valid = df.dropna(subset=["_vpt"]).sort_values("_row")
        first_vpt = valid.groupby("_gid", sort=True)["_vpt"].first()
        paid_total = line_sum.copy()
        paid_total.update(first_vpt)
    else:
        paid_total = line_sum
    ordered = df.sort_values("_row")
    first = ordered.groupby("_gid", sort=False).head(1).set_index("_gid")
    rev_col = (
        "insurance_revflow"
        if "insurance_revflow" in df.columns
        else ("payor" if "payor" in df.columns else "")
    )
    cpt_rows = df.loc[df["_cpt"].astype(str) != ""]
    paid_maps: dict[int, dict[str, float]] = defaultdict(dict)
    if not cpt_rows.empty:
        cpt_sum = cpt_rows.groupby(["_gid", "_cpt"], sort=True)["_paid"].sum()
        for (gid, cpt_code), amt in cpt_sum.items():
            amount = float(amt)
            if amount > 0:
                paid_maps[int(gid)][str(cpt_code)] = amount

    plan_cache: dict[tuple[str, str], tuple[str, ...]] = {}
    buckets: dict[str, list[_VisitAgg]] = defaultdict(list)
    for gid, total in paid_total.items():
        gid_i = int(gid)
        ins = str(first.at[gid, "ins_name"])
        if rev_col:
            rev_val = first.at[gid, rev_col]
            rev = str(rev_val or "")
        else:
            rev = ""
        cache_key = (ins, rev)
        hierarchy = plan_cache.get(cache_key)
        if hierarchy is None:
            hierarchy = resolve_payer_plan(ins, insurance_revflow=rev).hierarchy
            plan_cache[cache_key] = hierarchy
        agg = _VisitAgg(
            paid_total=float(total),
            billed=float(billed.at[gid]),
            allowed=float(allowed.at[gid]),
            paid_by_cpt=paid_maps.get(gid_i, {}),
        )
        for hkey in hierarchy:
            buckets[hkey].append(agg)
    return buckets


def learn_payment_models(
    recon_lines: pd.DataFrame,
    *,
    payments_unified: pd.DataFrame | None = None,
    visits: pd.DataFrame | None = None,
    fee_estimator: FeeEstimator | None = None,
    grouper=None,
) -> PaymentModelCatalog:
    """Learn reimbursement models at plan/class/org grains.

    Primary training grain is reconciliation lines (has WebPT ``ins_name``).
    ``payments_unified`` optionally enriches billed_amount; full PU replace is
    avoided because RevFlow payor labels lack plan specificity and are huge.
    """
    fee = fee_estimator or FeeEstimator.from_paid_lines(recon_lines)

    train = recon_lines.copy() if recon_lines is not None else pd.DataFrame()
    if train.empty:
        return PaymentModelCatalog(models={}, counts={}, fee_estimator=fee)

    if "billed_amount" not in train.columns:
        train["billed_amount"] = 0.0

    # Enrich billed_amount from payments_unified when missing on recon
    if payments_unified is not None and not payments_unified.empty:
        pu = payments_unified
        need = {"webpt_patient_id", "date_of_service", "cpt_code", "billed_amount"}
        if need.issubset(set(pu.columns)):
            billed = (
                pu[list(need)]
                .assign(
                    billed_amount=pd.to_numeric(pu["billed_amount"], errors="coerce").fillna(0),
                    cpt_code=pu["cpt_code"].astype(str).str.strip(),
                    webpt_patient_id=pu["webpt_patient_id"].astype(str),
                )
                .groupby(["webpt_patient_id", "date_of_service", "cpt_code"], dropna=False)[
                    "billed_amount"
                ]
                .max()
                .reset_index()
            )
            train = train.copy()
            train["cpt_code"] = train["cpt_code"].astype(str).str.strip()
            train["webpt_patient_id"] = train["webpt_patient_id"].astype(str)
            before = pd.to_numeric(train["billed_amount"], errors="coerce").fillna(0)
            train = train.merge(
                billed,
                on=["webpt_patient_id", "date_of_service", "cpt_code"],
                how="left",
                suffixes=("", "_pu"),
            )
            if "billed_amount_pu" in train.columns:
                pu_b = pd.to_numeric(train["billed_amount_pu"], errors="coerce").fillna(0)
                train["billed_amount"] = before.where(before > 0, pu_b)
                train = train.drop(columns=["billed_amount_pu"])

    # Enrich visit_paid_total from visits file when available
    if visits is not None and not visits.empty and "visit_paid_total" in visits.columns:
        vcols = [
            c
            for c in ("webpt_patient_id", "date_of_service", "visit_paid_total")
            if c in visits.columns
        ]
        if len(vcols) == 3:
            v = visits[vcols].copy()
            v["webpt_patient_id"] = v["webpt_patient_id"].astype(str)
            train = train.merge(
                v.drop_duplicates(),
                on=["webpt_patient_id", "date_of_service"],
                how="left",
            )

    buckets = (grouper or _group_paid_visit_aggs)(train)
    models: dict[str, PaymentModel] = {}
    counts: dict[str, int] = {k: len(v) for k, v in buckets.items()}

    for grain_key, groups in buckets.items():
        if grain_key.startswith("plan:"):
            grain = "plan"
            min_n = MIN_PLAN_SAMPLES
        elif grain_key.startswith("class:"):
            grain = "class"
            min_n = MIN_CLASS_SAMPLES
        else:
            grain = "org"
            min_n = MIN_ORG_SAMPLES
        if len(groups) < min_n:
            # Still store weaker models for relaxed lookup
            if len(groups) < 5:
                continue
        model = _infer_model_for_visits(grain_key, grain, groups)
        if model is not None:
            models[grain_key] = model

    return PaymentModelCatalog(models=models, counts=counts, fee_estimator=fee)


def learn_payment_models_reference(
    recon_lines: pd.DataFrame,
    *,
    payments_unified: pd.DataFrame | None = None,
    visits: pd.DataFrame | None = None,
    fee_estimator: FeeEstimator | None = None,
) -> PaymentModelCatalog:
    """Row-wise learner. Tests compare the fast path against this."""
    return learn_payment_models(
        recon_lines,
        payments_unified=payments_unified,
        visits=visits,
        fee_estimator=fee_estimator,
        grouper=_group_paid_visits,
    )


def apply_visit_expected_amounts_reference(
    lines: pd.DataFrame,
    catalog: PaymentModelCatalog,
) -> pd.DataFrame:
    """Row-wise expected amounts. Tests compare the fast path against this."""
    if lines is None or lines.empty:
        return lines

    out = lines.copy()
    if "precomputed_expected" not in out.columns:
        out["precomputed_expected"] = None

    status = out["status"].astype(str).str.lower() if "status" in out.columns else pd.Series([""] * len(out))
    open_mask = ~status.isin(["paid", "zero_pay", "denied", "patient_responsibility"])
    pre = out["precomputed_expected"]
    missing_pre = pre.isna() | (pre.astype(str).str.strip() == "") | (pre.astype(str) == "None")
    work = out[open_mask & missing_pre].copy()
    if work.empty:
        return out

    group_cols = ["webpt_patient_id", "date_of_service", "ins_name"]
    for col in group_cols:
        if col not in work.columns:
            work[col] = ""

    expected_by_idx: dict[object, float] = {}
    for _, group in work.groupby(group_cols, dropna=False):
        ins = str(group["ins_name"].iloc[0] or "")
        rev = ""
        if "insurance_revflow" in group.columns:
            rev = str(group["insurance_revflow"].iloc[0] or "")
        records = group.to_dict("records")
        visit_amt = catalog.estimate_for_lines(records, ins_name=ins, insurance_revflow=rev)
        weights: list[float] = []
        for record in records:
            cpt = str(record.get("cpt_code") or "")
            units = float(record.get("units") or 1) or 1.0
            weights.append(max(catalog.fee_estimator.estimate(cpt, ins) * units, 0.01))
        wsum = sum(weights) or 1.0
        for idx, weight in zip(group.index.tolist(), weights):
            expected_by_idx[idx] = round(visit_amt * (weight / wsum), 2)

    for idx, amt in expected_by_idx.items():
        out.at[idx, "precomputed_expected"] = amt
    return out


def apply_visit_expected_amounts(
    lines: pd.DataFrame,
    catalog: PaymentModelCatalog,
) -> pd.DataFrame:
    """Set precomputed_expected on open lines using visit-level payment models.

    Paid/zero_pay/denied keep their classify path; we still set precomputed for
    pending-like rows so classify_outcomes uses visit totals instead of CPT sums.
    """
    if lines is None or lines.empty:
        return lines

    out = lines.copy()
    if "precomputed_expected" not in out.columns:
        out["precomputed_expected"] = None

    status = out["status"].astype(str).str.lower() if "status" in out.columns else pd.Series([""] * len(out))
    open_mask = ~status.isin(["paid", "zero_pay", "denied", "patient_responsibility"])
    # Keep existing precomputed_expected (e.g. August forward volume).
    pre = out["precomputed_expected"]
    missing_pre = pre.isna() | (pre.astype(str).str.strip() == "") | (pre.astype(str) == "None")
    work = out[open_mask & missing_pre].copy()
    if work.empty:
        return out

    group_cols = ["webpt_patient_id", "date_of_service", "ins_name"]
    for c in group_cols:
        if c not in work.columns:
            work[c] = ""

    from cashflow_forecast.payer_plan import resolve_payer_plan

    model_cache: dict[tuple[str, str], PaymentModel] = {}
    fee_cache: dict[tuple[str, str], float] = {}
    fee = catalog.fee_estimator

    def fee_of(cpt: str, ins_name: str) -> float:
        key = (cpt, ins_name)
        hit = fee_cache.get(key)
        if hit is None:
            hit = fee.estimate(cpt, ins_name)
            fee_cache[key] = hit
        return hit

    indexes: list[object] = []
    amounts: list[float] = []
    for _, group in work.groupby(group_cols, dropna=False):
        ins = str(group["ins_name"].iloc[0] or "")
        rev = ""
        if "insurance_revflow" in group.columns:
            rev = str(group["insurance_revflow"].iloc[0] or "")
        cache_key = (ins, rev)
        model = model_cache.get(cache_key)
        if model is None:
            model, _grain = catalog.resolve_model(resolve_payer_plan(ins, insurance_revflow=rev))
            model_cache[cache_key] = model
        if "cpt_code" in group.columns:
            cpts = [str(value or "") for value in group["cpt_code"].tolist()]
        else:
            cpts = [""] * len(group)
        if "units" in group.columns:
            raw_units = group["units"].tolist()
        else:
            raw_units = [1] * len(group)
        units: list[float] = []
        for value in raw_units:
            try:
                units.append(float(value or 1) or 1.0)
            except (TypeError, ValueError):
                units.append(1.0)
        units_by_cpt: dict[str, float] = {}
        for cpt, unit in zip(cpts, units):
            cpt_s = cpt.strip()
            if cpt_s:
                units_by_cpt[cpt_s] = unit
        billed = (
            float(pd.to_numeric(group["billed_amount"], errors="coerce").fillna(0).sum())
            if "billed_amount" in group.columns
            else 0.0
        )
        allowed = (
            float(pd.to_numeric(group["allowed_amount"], errors="coerce").fillna(0).sum())
            if "allowed_amount" in group.columns
            else 0.0
        )
        visit_amt = model.estimate_visit(
            cpt_codes=cpts,
            billed_total=billed,
            allowed_total=allowed,
            fee_estimator=fee,
            ins_name=ins,
            units_by_cpt=units_by_cpt,
        )
        weights = [max(fee_of(cpt, ins) * unit, 0.01) for cpt, unit in zip(cpts, units)]
        wsum = sum(weights) or 1.0
        for idx, weight in zip(group.index.tolist(), weights):
            indexes.append(idx)
            amounts.append(round(visit_amt * (weight / wsum), 2))

    if indexes:
        out.loc[indexes, "precomputed_expected"] = amounts
    return out


def payment_models_to_frame(catalog: PaymentModelCatalog) -> pd.DataFrame:
    rows = []
    for key, model in sorted(catalog.models.items()):
        rows.append(
            {
                "grain_key": key,
                "grain": model.grain,
                "model_type": model.model_type,
                "flat_amount": model.flat_amount,
                "percent": model.percent,
                "adders": ";".join(f"{c}:{a}" for c, a in sorted(model.adders.items())),
                "n_visits": model.n_visits,
                "sample_count": catalog.counts.get(key, model.n_visits),
                "notes": model.notes,
            }
        )
    return pd.DataFrame(rows)


def write_payment_models(catalog: PaymentModelCatalog, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df = payment_models_to_frame(catalog)
    df.to_csv(path, index=False, quoting=csv.QUOTE_MINIMAL)

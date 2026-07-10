"""Estimate expected payment amounts from historical allowed/paid amounts."""

from __future__ import annotations

from collections import defaultdict
from statistics import median

import pandas as pd


class FeeEstimator:
    """CPT + insurance → expected amount (median allowed, then paid, then visit avg)."""

    def __init__(self) -> None:
        self._cpt_ins: dict[tuple[str, str], float] = {}
        self._cpt: dict[str, float] = {}
        self._ins: dict[str, float] = {}
        self._global = 50.0

    @classmethod
    def from_paid_lines(cls, lines: pd.DataFrame) -> FeeEstimator:
        est = cls()
        paid = lines[
            (lines["status"] == "paid")
            & (lines["paid_amount"] > 0)
        ].copy()
        if paid.empty:
            return est

        cpt_ins_vals: dict[tuple[str, str], list[float]] = defaultdict(list)
        cpt_vals: dict[str, list[float]] = defaultdict(list)
        ins_vals: dict[str, list[float]] = defaultdict(list)
        all_vals: list[float] = []

        for _, row in paid.iterrows():
            cpt = str(row.get("cpt_code") or "").strip()
            ins = str(row.get("ins_name") or row.get("insurance_revflow") or "").strip().lower()
            amt = float(row.get("allowed_amount") or 0) or float(row.get("paid_amount") or 0)
            if amt <= 0 or not cpt:
                continue
            cpt_ins_vals[(cpt, ins)].append(amt)
            cpt_vals[cpt].append(amt)
            if ins:
                ins_vals[ins].append(amt)
            all_vals.append(amt)

        est._cpt_ins = {k: float(median(v)) for k, v in cpt_ins_vals.items()}
        est._cpt = {k: float(median(v)) for k, v in cpt_vals.items()}
        est._ins = {k: float(median(v)) for k, v in ins_vals.items()}
        if all_vals:
            est._global = float(median(all_vals))
        return est

    def estimate(self, cpt_code: str, insurance: str = "") -> float:
        cpt = (cpt_code or "").strip()
        ins = (insurance or "").strip().lower()
        if cpt and ins and (cpt, ins) in self._cpt_ins:
            return self._cpt_ins[(cpt, ins)]
        if cpt and cpt in self._cpt:
            return self._cpt[cpt]
        if ins and ins in self._ins:
            return self._ins[ins]
        return self._global

"""Per-payor payment behavior: DOS→EOB SLA, EOB→deposit lag, weekday & cadence.

`paid_amount_sum` is the sum of RevFlow payment *lines* per check (not tracker bank Amount).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable

from .load_transaction_tracker import load_deposit_dates, normalize_eft
from .normalize import format_money, parse_date, parse_money
from .payer_registry import resolve

log = logging.getLogger("cashflow_reconcile.insurance_behavior")

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_DOS_TO_EOB_DAYS = 180


def _weekday_name(value: date | None) -> str:
    if value is None:
        return ""
    return WEEKDAYS[value.weekday()]


def _percentile(sorted_values: list[int], pct: float) -> int | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (len(sorted_values) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return int(round(sorted_values[low] * (1 - weight) + sorted_values[high] * weight))


def _mode_string(values: Iterable[str]) -> str:
    counts = Counter(v for v in values if v)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def _weekday_profile(day_counts: Counter[str], *, top_n: int = 3) -> str:
    if not day_counts:
        return ""
    total = sum(day_counts.values()) or 1
    parts: list[str] = []
    for day, count in day_counts.most_common(top_n):
        parts.append(f"{day} {100.0 * count / total:.0f}%")
    return "/".join(parts)


def label_cadence(
    deposit_dates: list[date],
) -> tuple[str, str, str]:
    """Return (cadence_label, top_deposit_weekday, weekday_profile).

    Uses check-weighted deposit dates (pass one entry per check). Detects
    near-daily cash, multi-weekday patterns, and holiday-tolerant weekly/biweekly.
    """
    if not deposit_dates:
        return ("insufficient_history", "", "")

    check_weighted_days = Counter(_weekday_name(d) for d in deposit_dates)
    top_day, top_n = check_weighted_days.most_common(1)[0]
    top_share = top_n / len(deposit_dates)
    profile = _weekday_profile(check_weighted_days)

    unique = sorted(set(deposit_dates))
    if len(unique) < 2:
        return ("insufficient_history", top_day, profile)

    gaps = [(unique[i] - unique[i - 1]).days for i in range(1, len(unique))]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return ("irregular", top_day, profile)

    med_gap = median(gaps)

    # Single dominant weekday + ~weekly spacing (allow holiday slips 5–10).
    if top_share >= 0.45 and 5 <= med_gap <= 10:
        return (f"weekly_{top_day.lower()}", top_day, profile)

    # Two-day pattern (e.g. Tue+Thu): both days meaningful, dominate deposits.
    if len(check_weighted_days) >= 2:
        (d1, n1), (d2, n2) = check_weighted_days.most_common(2)
        share1 = n1 / len(deposit_dates)
        share2 = n2 / len(deposit_dates)
        if share1 + share2 >= 0.70 and share2 >= 0.25:
            return (f"multi_weekday_{d1.lower()}_{d2.lower()}", d1, profile)

    # Frequent business-day deposits (many consecutive calendar gaps of 1–2).
    if med_gap <= 2 and len(unique) >= 8:
        return ("near_daily", top_day, profile)

    if 12 <= med_gap <= 17:
        if top_share >= 0.45:
            return (f"biweekly_{top_day.lower()}", top_day, profile)
        return ("biweekly", top_day, profile)

    if 13 <= med_gap <= 18 and top_share < 0.45:
        return ("semi_monthly", top_day, profile)

    if 28 <= med_gap <= 35:
        if top_share >= 0.45:
            return (f"monthly_{top_day.lower()}", top_day, profile)
        return ("monthly", top_day, profile)

    # Weekly-ish with weaker weekday lock (holiday noise).
    if 5 <= med_gap <= 10 and top_share >= 0.35:
        return (f"weekly_{top_day.lower()}", top_day, profile)

    return ("irregular", top_day, profile)

def _parse_deposit_map(raw: dict[str, str]) -> dict[str, date]:
    out: dict[str, date] = {}
    for key, value in raw.items():
        parsed = parse_date(value)
        if parsed is None and value:
            # Tracker loader formats MM/DD/YYYY already; also accept datetime strings.
            try:
                parsed = datetime.strptime(value.strip(), "%m/%d/%Y").date()
            except ValueError:
                parsed = None
        if parsed is not None:
            out[key] = parsed
    return out


def _load_ins_name_by_check(lines_path: Path | None) -> dict[str, str]:
    """Dominant WebPT ins_name per check_eft_num from reconciliation lines."""
    if lines_path is None or not lines_path.exists():
        return {}
    by_check: dict[str, Counter[str]] = defaultdict(Counter)
    with lines_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            check = normalize_eft(row.get("check_eft_num"))
            ins = (row.get("ins_name") or "").strip()
            if not check or not ins:
                continue
            by_check[check][ins] += 1
    return {check: counts.most_common(1)[0][0] for check, counts in by_check.items()}


def _load_patient_ins_fallback(patients_path: Path | None) -> dict[str, str]:
    if patients_path is None or not patients_path.exists():
        return {}
    mapping: dict[str, str] = {}
    with patients_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            pid = str(row.get("patient_id") or "").strip()
            ins = (row.get("ins_name") or "").strip()
            if pid and ins and pid not in mapping:
                mapping[pid] = ins
    return mapping


def _dos_to_eob_samples_by_check(lines_path: Path | None) -> dict[str, list[int]]:
    if lines_path is None or not lines_path.exists():
        return {}
    samples: dict[str, list[int]] = defaultdict(list)
    with lines_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if (row.get("status") or "").strip().lower() != "paid":
                continue
            check = normalize_eft(row.get("check_eft_num"))
            if not check:
                continue
            dos = parse_date(row.get("date_of_service"))
            eob = parse_date(row.get("eob_date"))
            if dos is None or eob is None:
                continue
            lag = (eob - dos).days
            if 0 <= lag <= MAX_DOS_TO_EOB_DAYS:
                samples[check].append(lag)
    return samples


def build_checks_timeline(
    *,
    payments_path: Path,
    deposit_dates: dict[str, date],
    ins_by_check: dict[str, str],
    patient_ins: dict[str, str],
    dos_samples: dict[str, list[int]],
) -> list[dict[str, Any]]:
    aggregates: dict[tuple[str, str], dict[str, Any]] = {}

    with payments_path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            check = normalize_eft(row.get("check_eft_num"))
            if not check:
                continue
            payor = (row.get("payor") or "").strip() or "UNKNOWN"
            key = (payor, check)
            bucket = aggregates.get(key)
            eob = parse_date(row.get("eob_date"))
            dos = parse_date(row.get("date_of_service"))
            paid = parse_money(row.get("paid_amount"))
            webpt_pid = str(row.get("webpt_patient_id") or "").strip()

            if bucket is None:
                aggregates[key] = {
                    "payor": payor,
                    "check_eft_num": check,
                    "eob_dates": [] if eob is None else [eob],
                    "dos_dates": [] if dos is None else [dos],
                    "paid_amount_sum": paid,
                    "line_count": 1,
                    "ins_votes": Counter(),
                    "patient_ids": set(),
                }
                bucket = aggregates[key]
            else:
                bucket["paid_amount_sum"] += paid
                bucket["line_count"] += 1
                if eob is not None:
                    bucket["eob_dates"].append(eob)
                if dos is not None:
                    bucket["dos_dates"].append(dos)

            ins = ins_by_check.get(check) or patient_ins.get(webpt_pid) or ""
            if ins:
                bucket["ins_votes"][ins] += 1
            if webpt_pid:
                bucket["patient_ids"].add(webpt_pid)

    rows: list[dict[str, Any]] = []
    for (payor, check), bucket in sorted(aggregates.items(), key=lambda x: (x[0][0], x[0][1])):
        eob_dates: list[date] = bucket["eob_dates"]
        dos_dates: list[date] = bucket["dos_dates"]
        eob_date = min(eob_dates) if eob_dates else None
        deposit_date = deposit_dates.get(check)
        check_to_deposit = (
            (deposit_date - eob_date).days
            if deposit_date is not None and eob_date is not None
            else None
        )
        lag_samples = dos_samples.get(check) or []
        if not lag_samples and eob_date is not None and dos_dates:
            lag_samples = [
                (eob_date - d).days
                for d in dos_dates
                if 0 <= (eob_date - d).days <= MAX_DOS_TO_EOB_DAYS
            ]
        ins_name = (
            bucket["ins_votes"].most_common(1)[0][0] if bucket["ins_votes"] else ""
        )
        resolved = (
            resolve(payor, "revflow")
            or resolve(ins_name, "webpt")
            or resolve(payor, "any")
            or resolve(ins_name, "any")
        )
        rows.append(
            {
                "payor": payor,
                "ins_name": ins_name,
                "payer_org_code": resolved.code if resolved else "",
                "payer_org": resolved.name if resolved else "",
                "check_eft_num": check,
                "eob_date": eob_date.isoformat() if eob_date else "",
                "deposit_date": deposit_date.isoformat() if deposit_date else "",
                "eob_weekday": _weekday_name(eob_date),
                "deposit_weekday": _weekday_name(deposit_date),
                "check_to_deposit_days": (
                    "" if check_to_deposit is None else str(check_to_deposit)
                ),
                "paid_amount_sum": format_money(bucket["paid_amount_sum"]),
                "line_count": bucket["line_count"],
                "min_dos": min(dos_dates).isoformat() if dos_dates else "",
                "max_dos": max(dos_dates).isoformat() if dos_dates else "",
                "dos_to_eob_days_median": (
                    str(int(median(lag_samples))) if lag_samples else ""
                ),
                "dos_to_eob_sample_count": len(lag_samples),
            }
        )
    return rows


def _lag_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {
            "median": "",
            "p75": "",
            "p90": "",
            "n": 0,
        }
    sorted_vals = sorted(values)
    return {
        "median": int(median(sorted_vals)),
        "p75": _percentile(sorted_vals, 0.75),
        "p90": _percentile(sorted_vals, 0.90),
        "n": len(sorted_vals),
    }


def build_payor_summaries(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_payor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in checks:
        by_payor[row["payor"]].append(row)

    summaries: list[dict[str, Any]] = []
    for payor, rows in sorted(by_payor.items(), key=lambda x: -sum(parse_money(r["paid_amount_sum"]) for r in x[1])):
        deposit_lags: list[int] = []
        dos_lags: list[int] = []
        eob_weekdays: list[str] = []
        deposit_weekdays: list[str] = []
        deposit_dates: list[date] = []
        same = one = two = three_plus = 0
        with_deposit = 0
        total_paid = 0.0
        ins_votes: Counter[str] = Counter()

        for row in rows:
            total_paid += parse_money(row["paid_amount_sum"])
            if row.get("ins_name"):
                ins_votes[row["ins_name"]] += 1
            if row.get("eob_weekday"):
                eob_weekdays.append(row["eob_weekday"])
            dep = parse_date(row.get("deposit_date"))
            eob = parse_date(row.get("eob_date"))
            if dep is not None:
                with_deposit += 1
                deposit_dates.append(dep)
                if row.get("deposit_weekday"):
                    deposit_weekdays.append(row["deposit_weekday"])
            lag_text = (row.get("check_to_deposit_days") or "").strip()
            if lag_text != "" and eob is not None and dep is not None:
                lag = int(lag_text)
                # Ignore pathological negatives (data quirks) for stats.
                if lag >= 0:
                    deposit_lags.append(lag)
                    if lag == 0:
                        same += 1
                    elif lag == 1:
                        one += 1
                    elif lag == 2:
                        two += 1
                    else:
                        three_plus += 1
            dos_med = (row.get("dos_to_eob_days_median") or "").strip()
            if dos_med:
                dos_lags.append(int(dos_med))

        n_checks = len(rows)
        n_lag = len(deposit_lags) or 1
        dep_stats = _lag_stats(deposit_lags)
        dos_stats = _lag_stats(dos_lags)
        cadence, top_deposit_day, weekday_profile = label_cadence(deposit_dates)
        top_eob_day = _mode_string(eob_weekdays)
        eob_day_share = (
            eob_weekdays.count(top_eob_day) / len(eob_weekdays) if eob_weekdays and top_eob_day else 0.0
        )
        deposit_day_share = (
            deposit_weekdays.count(top_deposit_day) / len(deposit_weekdays)
            if deposit_weekdays and top_deposit_day
            else 0.0
        )
        dominant_ins = ins_votes.most_common(1)[0][0] if ins_votes else ""
        resolved = (
            resolve(payor, "revflow")
            or resolve(dominant_ins, "webpt")
            or resolve(payor, "any")
            or resolve(dominant_ins, "any")
        )
        coverage_pct = (100.0 * with_deposit / n_checks) if n_checks else 0.0
        avg_paid = (total_paid / n_checks) if n_checks else 0.0

        cash_velocity: str | int = ""
        if dos_stats["median"] != "" and dep_stats["median"] != "":
            cash_velocity = int(dos_stats["median"]) + int(dep_stats["median"])

        note_parts = []
        if cadence and cadence != "insufficient_history":
            note_parts.append(cadence.replace("_", " "))
        note_parts.append(f"coverage {coverage_pct:.0f}%")
        if cash_velocity != "":
            note_parts.append(f"cash velocity {cash_velocity}d")
        if dep_stats["median"] != "":
            note_parts.append(f"EOB→bank median {dep_stats['median']}d")
        if dos_stats["median"] != "":
            note_parts.append(f"DOS→EOB median {dos_stats['median']}d")
        behavior_note = "; ".join(note_parts)

        summaries.append(
            {
                "payor": payor,
                "dominant_ins_name": dominant_ins,
                "payer_org_code": resolved.code if resolved else "",
                "payer_org": resolved.name if resolved else "",
                "n_checks": n_checks,
                "n_with_deposit": with_deposit,
                "deposit_coverage_pct": f"{coverage_pct:.1f}",
                "paid_amount_sum": format_money(total_paid),
                "avg_paid_per_check": format_money(avg_paid),
                "dos_to_eob_median": dos_stats["median"],
                "dos_to_eob_p75": dos_stats["p75"] if dos_stats["p75"] is not None else "",
                "dos_to_eob_p90": dos_stats["p90"] if dos_stats["p90"] is not None else "",
                "dos_to_eob_n": dos_stats["n"],
                "eob_to_deposit_median": dep_stats["median"],
                "eob_to_deposit_p75": dep_stats["p75"] if dep_stats["p75"] is not None else "",
                "eob_to_deposit_p90": dep_stats["p90"] if dep_stats["p90"] is not None else "",
                "eob_to_deposit_n": dep_stats["n"],
                "cash_velocity_median": cash_velocity,
                "pct_deposit_same_day": f"{100.0 * same / n_lag:.1f}" if deposit_lags else "",
                "pct_deposit_plus_1": f"{100.0 * one / n_lag:.1f}" if deposit_lags else "",
                "pct_deposit_plus_2": f"{100.0 * two / n_lag:.1f}" if deposit_lags else "",
                "pct_deposit_plus_3_or_more": (
                    f"{100.0 * three_plus / n_lag:.1f}" if deposit_lags else ""
                ),
                "top_eob_weekday": top_eob_day,
                "top_eob_weekday_pct": f"{100.0 * eob_day_share:.1f}" if top_eob_day else "",
                "top_deposit_weekday": top_deposit_day,
                "top_deposit_weekday_pct": (
                    f"{100.0 * deposit_day_share:.1f}" if top_deposit_day else ""
                ),
                "weekday_profile": weekday_profile,
                "cadence": cadence,
                "behavior_note": behavior_note,
            }
        )
    return summaries


def build_weekday_heatmap(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"eob_count": 0, "deposit_count": 0}
    )
    for row in checks:
        payor = row["payor"]
        eob_day = row.get("eob_weekday") or ""
        dep_day = row.get("deposit_weekday") or ""
        if eob_day:
            counts[(payor, eob_day)]["eob_count"] += 1
        if dep_day:
            counts[(payor, dep_day)]["deposit_count"] += 1

    rows: list[dict[str, Any]] = []
    for (payor, weekday), vals in sorted(counts.items(), key=lambda x: (x[0][0], WEEKDAYS.index(x[0][1]) if x[0][1] in WEEKDAYS else 99)):
        rows.append(
            {
                "payor": payor,
                "weekday": weekday,
                "eob_count": vals["eob_count"],
                "deposit_count": vals["deposit_count"],
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build_insurance_behavior(
    *,
    payments_path: Path,
    lines_path: Path | None,
    transaction_tracker: Path | None,
    output_dir: Path,
    patients_path: Path | None = None,
) -> dict[str, Any]:
    if not payments_path.exists():
        raise FileNotFoundError(f"Missing payments file: {payments_path}")
    if transaction_tracker is not None and not transaction_tracker.exists():
        raise FileNotFoundError(f"Missing transaction tracker: {transaction_tracker}")

    deposit_raw = load_deposit_dates(transaction_tracker)
    deposit_dates = _parse_deposit_map(deposit_raw)
    ins_by_check = _load_ins_name_by_check(lines_path)
    patient_ins = _load_patient_ins_fallback(patients_path)
    dos_samples = _dos_to_eob_samples_by_check(lines_path)

    checks = build_checks_timeline(
        payments_path=payments_path,
        deposit_dates=deposit_dates,
        ins_by_check=ins_by_check,
        patient_ins=patient_ins,
        dos_samples=dos_samples,
    )
    check_keys = [(row["payor"], row["check_eft_num"]) for row in checks]
    if len(check_keys) != len(set(check_keys)):
        log.warning(
            "Duplicate (payor, check) keys in timeline (%s rows, %s unique)",
            len(check_keys),
            len(set(check_keys)),
        )
    else:
        log.info("Timeline check keys unique: %s", len(check_keys))

    summaries = build_payor_summaries(checks)
    heatmap = build_weekday_heatmap(checks)

    check_fields = [
        "payor",
        "ins_name",
        "payer_org_code",
        "payer_org",
        "check_eft_num",
        "eob_date",
        "deposit_date",
        "eob_weekday",
        "deposit_weekday",
        "check_to_deposit_days",
        "paid_amount_sum",
        "line_count",
        "min_dos",
        "max_dos",
        "dos_to_eob_days_median",
        "dos_to_eob_sample_count",
    ]
    summary_fields = [
        "payor",
        "dominant_ins_name",
        "payer_org_code",
        "payer_org",
        "n_checks",
        "n_with_deposit",
        "deposit_coverage_pct",
        "paid_amount_sum",
        "avg_paid_per_check",
        "dos_to_eob_median",
        "dos_to_eob_p75",
        "dos_to_eob_p90",
        "dos_to_eob_n",
        "eob_to_deposit_median",
        "eob_to_deposit_p75",
        "eob_to_deposit_p90",
        "eob_to_deposit_n",
        "cash_velocity_median",
        "pct_deposit_same_day",
        "pct_deposit_plus_1",
        "pct_deposit_plus_2",
        "pct_deposit_plus_3_or_more",
        "top_eob_weekday",
        "top_eob_weekday_pct",
        "top_deposit_weekday",
        "top_deposit_weekday_pct",
        "weekday_profile",
        "cadence",
        "behavior_note",
    ]
    heatmap_fields = ["payor", "weekday", "eob_count", "deposit_count"]

    _write_csv(output_dir / "checks_timeline.csv", checks, check_fields)
    _write_csv(output_dir / "payor_behavior_summary.csv", summaries, summary_fields)
    _write_csv(output_dir / "payor_weekday_heatmap.csv", heatmap, heatmap_fields)

    return {
        "n_checks": len(checks),
        "n_payors": len(summaries),
        "n_deposit_keys": len(deposit_dates),
        "output_dir": str(output_dir),
        "top_payors": summaries[:15],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze per-insurance check cadence and EOB→deposit SLA"
    )
    parser.add_argument(
        "--from-db",
        action="store_true",
        help="Read payments/deposits from cashflow_db and persist behavior facts",
    )
    parser.add_argument("--payments", type=Path, default=None)
    parser.add_argument("--lines", type=Path, default=None)
    parser.add_argument("--transaction-tracker", type=Path, default=None)
    parser.add_argument("--patients-export", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--emit-csv", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.from_db:
        import tempfile
        from pathlib import Path as P

        from cashflow_db.repository import connection
        from cashflow_db.repository import payments as pay_repo
        from cashflow_db.repository import reconciliation as recon_repo
        from cashflow_reconcile.db_io import persist_insurance_behavior_from_frames
        from cashflow_reconcile.normalize import format_money

        # Materialize temporary CSVs for existing builder, then persist to DB
        with connection() as conn:
            pay_rows = pay_repo.get_eob_payments_unified(conn)
            deposits = pay_repo.get_bank_deposits(conn)
            run_id = recon_repo.latest_reconciliation_run_id(conn)

        tmp = P(tempfile.mkdtemp(prefix="ib_db_"))
        pay_path = tmp / "payments_unified.csv"
        if pay_rows:
            keys = list(pay_rows[0].keys())
            import csv as _csv

            with pay_path.open("w", encoding="utf-8", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
                w.writeheader()
                for r in pay_rows:
                    w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})
        # Tracker still needed by builder — write a minimal xlsx-less path via deposits CSV hack:
        # Prefer real tracker if provided; else skip deposit join (coverage low).
        tracker = args.transaction_tracker
        # Deposit dates load from Postgres when tracker path is omitted.
        out = args.output_dir or (tmp / "out")
        summary = build_insurance_behavior(
            payments_path=pay_path,
            lines_path=None,
            transaction_tracker=P(tracker) if tracker else None,
            output_dir=P(out),
            patients_path=args.patients_export,
        )
        # Reload written CSVs into DB facts
        import csv as _csv

        summ_path = P(out) / "payor_behavior_summary.csv"
        tl_path = P(out) / "checks_timeline.csv"
        summ_rows = list(_csv.DictReader(summ_path.open(encoding="utf-8"))) if summ_path.exists() else []
        tl_rows = list(_csv.DictReader(tl_path.open(encoding="utf-8"))) if tl_path.exists() else []
        for r in summ_rows:
            r["payor_key"] = r.get("payor") or r.get("payer_org_code")
            r["median_cash_velocity_days"] = r.get("cash_velocity_median")
            r["median_eob_to_deposit_days"] = r.get("eob_to_deposit_median")
            r["check_count"] = r.get("n_checks")
        persisted = persist_insurance_behavior_from_frames(
            reconciliation_run_id=run_id,
            summary_rows=summ_rows,
            timeline_rows=tl_rows,
        )
        log.info("Persisted insurance behavior to DB: %s", persisted)
        if not args.emit_csv:
            # builder already wrote CSVs under out; acceptable as staging under tmp
            pass
        _ = format_money
        _ = deposits
    else:
        if not args.payments or not args.transaction_tracker or not args.output_dir:
            parser.error("Legacy mode requires --payments --transaction-tracker --output-dir")
        summary = build_insurance_behavior(
            payments_path=args.payments,
            lines_path=args.lines,
            transaction_tracker=args.transaction_tracker,
            output_dir=args.output_dir,
            patients_path=args.patients_export,
        )
    log.info(
        "Wrote insurance behavior for %s checks / %s payors -> %s",
        summary["n_checks"],
        summary["n_payors"],
        summary["output_dir"],
    )
    log.info("Top payors by paid amount:")
    for row in summary["top_payors"]:
        log.info(
            "  %s | checks=%s | %s | %s",
            row["payor"],
            row["n_checks"],
            row["paid_amount_sum"],
            row["behavior_note"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

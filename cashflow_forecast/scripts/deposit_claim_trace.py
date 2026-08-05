"""Claim-level deposit trace for a Tracker deposit day.

For each deposit row on the target date:
  Tracker EFT/check → checks_timeline / payments_unified → outcome_stages
Classify why Expected land did or did not cover the deposit.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from cashflow_forecast.utils import parse_money  # noqa: E402
from cashflow_reconcile.load_transaction_tracker import load_deposit_ledger  # noqa: E402
from cashflow_reconcile.payer_registry import (  # noqa: E402
    extract_ach_payer_head,
    extract_eft_refs_from_description,
    is_ach_processor,
    resolve,
    resolve_tracker_description,
)

PATIENT_HINTS = (
    "MERCH",
    "COUNTER",
    "BANK OF AMERICA",
    "DEPOSIT",
    "MERCH SERV",
)

BUCKETS = (
    "matched_expected_same_day",
    "matched_expected_other_day",
    "matched_already_paid_earlier",
    "matched_open_other_stage",
    "matched_missing_outcomes",
    "eft_in_checks_no_payments",
    "unmapped_processor",
    "no_eob_yet",
    "patient_non_ar",
    "unmatched_no_eft",
    "unmatched_eft_not_in_recon",
    "amount_mismatch_note",
)


def _norm_eft(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", str(value or "").strip().upper())
    # drop leading zeros for numeric-ish ids but keep if all zeros
    if text.isdigit():
        text = text.lstrip("0") or "0"
    return text


def _parse_day(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text[:10] if fmt.startswith("%Y") else text, fmt).date()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _is_patientish(description: str, payer_org: str, head: str) -> bool:
    blob = f"{description} {payer_org} {head}".upper()
    return any(h in blob for h in PATIENT_HINTS)


def _payer_org_label(description: str, payer_org: str) -> str:
    if payer_org:
        return payer_org
    hit = resolve_tracker_description(description)
    if hit:
        return hit.name
    head = extract_ach_payer_head(description)
    if head:
        hit2 = resolve(head, "tracker") or resolve(head, "any")
        if hit2:
            return hit2.name
        return head
    return "(blank)"


def load_checks_by_eft(path: Path) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = _norm_eft(row.get("check_eft_num"))
            if key:
                out[key].append(row)
    return out


def load_payments_by_eft(path: Path) -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not path.exists():
        return out
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            key = _norm_eft(row.get("check_eft_num"))
            if key:
                out[key].append(row)
    return out


def load_outcomes_index(
    path: Path,
) -> tuple[
    dict[tuple[str, str], list[dict[str, str]]],
    dict[str, list[dict[str, str]]],
]:
    """Indexes: (name_key, dos) -> rows; name_key -> rows."""
    by_key: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    by_name: dict[str, list[dict[str, str]]] = defaultdict(list)
    if not path.exists():
        return by_key, by_name
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            nk = (row.get("name_key") or "").strip()
            dos = (row.get("date_of_service") or "")[:10]
            if nk and dos:
                by_key[(nk, dos)].append(row)
            if nk:
                by_name[nk].append(row)
    return by_key, by_name


def _outcome_org(row: dict[str, str]) -> str:
    hit = (
        resolve(row.get("insurance_revflow") or "", "revflow")
        or resolve(row.get("ins_name") or "", "webpt")
        or resolve(row.get("ins_name") or "", "any")
    )
    return hit.name if hit else (row.get("ins_name") or "(blank)")


def summarize_outcomes(rows: list[dict[str, str]], deposit_day: date) -> dict[str, object]:
    if not rows:
        return {
            "outcome_line_count": 0,
            "open_expected_amt": 0.0,
            "paid_amt": 0.0,
            "denied_amt": 0.0,
            "open_on_deposit_day": 0.0,
            "open_other_day": 0.0,
            "min_original_fd": "",
            "max_original_fd": "",
            "stages": "",
        }
    open_amt = 0.0
    open_same = 0.0
    open_other = 0.0
    paid = 0.0
    denied = 0.0
    stages: Counter[str] = Counter()
    fds: list[date] = []
    day_s = deposit_day.isoformat()
    for row in rows:
        stage = (row.get("outcome_stage") or "").strip().lower()
        stages[stage] += 1
        exp = parse_money(row.get("expected_amount") or "0")
        paid_amt = parse_money(row.get("paid_amount") or "0")
        ofd = (row.get("original_forecast_date") or row.get("forecast_date") or "")[:10]
        od = _parse_day(ofd)
        if od:
            fds.append(od)
        if stage in ("on_track", "overdue") and exp > 0:
            open_amt += exp
            if ofd == day_s:
                open_same += exp
            else:
                open_other += exp
        elif stage == "paid":
            paid += paid_amt if paid_amt else exp
        elif stage == "denied":
            denied += exp
    return {
        "outcome_line_count": len(rows),
        "open_expected_amt": round(open_amt, 2),
        "paid_amt": round(paid, 2),
        "denied_amt": round(denied, 2),
        "open_on_deposit_day": round(open_same, 2),
        "open_other_day": round(open_other, 2),
        "min_original_fd": min(fds).isoformat() if fds else "",
        "max_original_fd": max(fds).isoformat() if fds else "",
        "stages": ";".join(f"{k}:{v}" for k, v in stages.most_common()),
    }


def classify_deposit(
    *,
    amount: float,
    description: str,
    payer_org: str,
    head: str,
    eft_keys: list[str],
    checks: dict[str, list[dict[str, str]]],
    payments: dict[str, list[dict[str, str]]],
    outcomes_by_key: dict[tuple[str, str], list[dict[str, str]]],
    deposit_day: date,
) -> tuple[str, dict[str, object]]:
    """Return (bucket, detail fields)."""
    mapped = bool(payer_org) and payer_org not in {"(blank)", ""}
    processor = is_ach_processor(payer_org) or any(
        x in f"{head} {description}".upper()
        for x in ("ECHO", "PAY PLUS", "PAYPLUS", "HNB ECHO", "PNC-ECHO")
    )
    patientish = _is_patientish(description, payer_org, head)
    if patientish and not any(
        x in description.upper() for x in ("HCCLAIMPMT", "ECHO", "PAY PLUS", "PAYPLUS", "NYNM")
    ):
        return "patient_non_ar", {
            "matched_eft": "",
            "check_payor": "",
            "check_eob_date": "",
            "check_deposit_date": "",
            "payment_lines": 0,
            "payment_paid_sum": 0.0,
            "note": "patient_or_merchant_ach",
        }

    # Find first matching eft in checks/payments
    matched_eft = ""
    check_rows: list[dict[str, str]] = []
    pay_rows: list[dict[str, str]] = []
    for eft in eft_keys:
        if eft in checks or eft in payments:
            matched_eft = eft
            check_rows = checks.get(eft, [])
            pay_rows = payments.get(eft, [])
            break

    if not eft_keys:
        if processor or not mapped:
            return "unmapped_processor", {
                "matched_eft": "",
                "check_payor": "",
                "check_eob_date": "",
                "check_deposit_date": "",
                "payment_lines": 0,
                "payment_paid_sum": 0.0,
                "note": "no_eft_ref",
            }
        return "unmatched_no_eft", {
            "matched_eft": "",
            "check_payor": "",
            "check_eob_date": "",
            "check_deposit_date": "",
            "payment_lines": 0,
            "payment_paid_sum": 0.0,
            "note": "mapped_payer_but_no_eft",
        }

    if not matched_eft:
        # Has EFT but not in RevFlow checks/payments yet.
        if processor or not mapped:
            bucket = "unmapped_processor"
            note = (
                "processor_eft_absent_from_revflow"
                if processor
                else "unmapped_eft_absent_from_revflow"
            )
        else:
            # Mapped insurer ACH (e.g. Anthem) but EFT not in reconcile → EOB not ingested
            bucket = "no_eob_yet"
            note = "mapped_payer_eft_absent_from_revflow"
        return bucket, {
            "matched_eft": eft_keys[0],
            "check_payor": "",
            "check_eob_date": "",
            "check_deposit_date": "",
            "payment_lines": 0,
            "payment_paid_sum": 0.0,
            "note": note,
        }

    check0 = check_rows[0] if check_rows else {}
    check_eob = check0.get("eob_date") or ""
    check_dep = check0.get("deposit_date") or ""
    check_payor = check0.get("payor") or check0.get("payer_org") or ""
    pay_sum = sum(parse_money(r.get("paid_amount") or "0") for r in pay_rows)

    detail = {
        "matched_eft": matched_eft,
        "check_payor": check_payor,
        "check_eob_date": check_eob,
        "check_deposit_date": check_dep,
        "payment_lines": len(pay_rows),
        "payment_paid_sum": round(pay_sum, 2),
        "note": "",
    }

    if check_rows and not pay_rows:
        detail["note"] = "in_checks_timeline_only"
        return "eft_in_checks_no_payments", detail

    if not pay_rows:
        # EFT matched somehow empty — treat as no_eob path
        eob_d = _parse_day(check_eob)
        if eob_d is None:
            return "no_eob_yet", detail
        detail["note"] = "checks_only_empty_payments"
        return "eft_in_checks_no_payments", detail

    # Link payments → outcomes via name_key + DOS
    outcome_rows: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for pr in pay_rows:
        nk = (pr.get("name_key") or "").strip()
        dos = str(pr.get("date_of_service") or "")[:10]
        # normalize dos
        dd = _parse_day(dos)
        dos_s = dd.isoformat() if dd else dos
        if not nk or not dos_s:
            continue
        for o in outcomes_by_key.get((nk, dos_s), []):
            sig = (nk, dos_s, o.get("cpt_code") or "", o.get("outcome_stage") or "")
            # allow multiple cpt lines
            key2 = (nk, dos_s, str(id(o)))
            if key2 in seen:
                continue
            seen.add(key2)
            outcome_rows.append(o)

    # Dedup outcome rows by object identity already; also unique by cpt+stage+exp
    uniq: list[dict[str, str]] = []
    ukeys: set[tuple] = set()
    for o in outcome_rows:
        uk = (
            o.get("name_key"),
            (o.get("date_of_service") or "")[:10],
            o.get("cpt_code"),
            o.get("outcome_stage"),
            o.get("expected_amount"),
            o.get("paid_amount"),
        )
        if uk in ukeys:
            continue
        ukeys.add(uk)
        uniq.append(o)

    summ = summarize_outcomes(uniq, deposit_day)
    detail.update(summ)

    if not uniq:
        eob_d = _parse_day(check_eob)
        if eob_d is None or eob_d > deposit_day:
            detail["note"] = "payments_present_but_no_outcome_rows"
            return "no_eob_yet" if eob_d is None else "matched_missing_outcomes", detail
        detail["note"] = "payments_not_in_outcome_stages"
        return "matched_missing_outcomes", detail

    open_same = float(summ["open_on_deposit_day"])
    open_other = float(summ["open_other_day"])
    paid_amt = float(summ["paid_amt"])

    # Primary classification by dollars on linked claims
    if open_same > 0 and open_same >= open_other and open_same >= paid_amt * 0.5:
        return "matched_expected_same_day", detail
    if open_other > 0 and open_other >= open_same:
        return "matched_expected_other_day", detail
    if paid_amt > 0 and open_same <= 0:
        # paid with eob earlier than deposit day is common
        eob_d = _parse_day(check_eob)
        if eob_d and eob_d < deposit_day:
            return "matched_already_paid_earlier", detail
        return "matched_already_paid_earlier", detail
    if float(summ["denied_amt"]) > 0 and open_same <= 0 and paid_amt <= 0:
        detail["note"] = "linked_claims_denied"
        return "matched_open_other_stage", detail

    # amount mismatch note if payment sum far from deposit
    if pay_sum > 0 and abs(pay_sum - amount) / max(amount, 1) > 0.25:
        detail["note"] = f"payment_sum={pay_sum:.2f}_vs_deposit={amount:.2f}"
    return "matched_open_other_stage", detail


def run_trace(
    *,
    tracker_path: Path,
    forecast_dir: Path,
    recon_dir: Path,
    target: date,
) -> Path:
    checks_path = recon_dir / "insurance_behavior" / "checks_timeline.csv"
    payments_path = recon_dir / "payments_unified.csv"
    outcomes_path = forecast_dir / "outcome_stages.csv"

    checks = load_checks_by_eft(checks_path)
    payments = load_payments_by_eft(payments_path)
    outcomes_by_key, _ = load_outcomes_index(outcomes_path)

    deposits = [
        r
        for r in load_deposit_ledger(tracker_path)
        if r.get("deposit_date") == target
    ]

    out_dir = forecast_dir / "gap_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / f"deposit_claim_trace_{target.isoformat()}.csv"
    summary_path = out_dir / f"deposit_claim_trace_{target.isoformat()}_summary.txt"

    rows_out: list[dict[str, object]] = []
    bucket_amt: dict[str, float] = defaultdict(float)
    bucket_n: dict[str, int] = defaultdict(int)

    for i, dep in enumerate(deposits):
        amount = float(dep.get("amount") or 0)
        desc = str(dep.get("description") or "")
        org = str(dep.get("payer_org") or "").strip()
        head = str(dep.get("ach_payer_head") or "").strip() or extract_ach_payer_head(desc)
        label = _payer_org_label(desc, org)
        eft_keys = []
        for k in ("eft_1", "eft_2", "check_reference"):
            n = _norm_eft(dep.get(k))
            if n and n not in eft_keys:
                eft_keys.append(n)
        for ref in extract_eft_refs_from_description(desc):
            n = _norm_eft(ref)
            if n and n not in eft_keys:
                eft_keys.append(n)

        resolved = resolve_tracker_description(desc)
        mapped_org = org or (resolved.name if resolved else "")
        bucket, detail = classify_deposit(
            amount=amount,
            description=desc,
            payer_org=mapped_org,
            head=head,
            eft_keys=eft_keys,
            checks=checks,
            payments=payments,
            outcomes_by_key=outcomes_by_key,
            deposit_day=target,
        )

        bucket_amt[bucket] += amount
        bucket_n[bucket] += 1

        rows_out.append(
            {
                "deposit_index": i,
                "deposit_date": target.isoformat(),
                "amount": round(amount, 2),
                "payer_org_resolved": label,
                "ach_payer_head": head,
                "description": desc[:180],
                "eft_1": dep.get("eft_1") or "",
                "eft_2": dep.get("eft_2") or "",
                "check_reference": dep.get("check_reference") or "",
                "bucket": bucket,
                **detail,
            }
        )

    fields = [
        "deposit_index",
        "deposit_date",
        "amount",
        "payer_org_resolved",
        "ach_payer_head",
        "description",
        "eft_1",
        "eft_2",
        "check_reference",
        "bucket",
        "matched_eft",
        "check_payor",
        "check_eob_date",
        "check_deposit_date",
        "payment_lines",
        "payment_paid_sum",
        "outcome_line_count",
        "open_expected_amt",
        "paid_amt",
        "denied_amt",
        "open_on_deposit_day",
        "open_other_day",
        "min_original_fd",
        "max_original_fd",
        "stages",
        "note",
    ]
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows_out:
            for f in fields:
                row.setdefault(f, "")
            w.writerow(row)

    total = sum(float(r["amount"]) for r in rows_out)
    # Linked open same day vs deposit for matched buckets
    with summary_path.open("w", encoding="utf-8") as fh:
        fh.write(f"Deposit claim trace — {target.isoformat()}\n")
        fh.write(f"Deposits: {len(rows_out)} | Total: ${total:,.2f}\n")
        fh.write(f"Detail CSV: {out_csv.name}\n\n")
        fh.write("Bucket rollup (by deposit $):\n")
        for b, amt in sorted(bucket_amt.items(), key=lambda kv: -kv[1]):
            fh.write(f"  {b}: n={bucket_n[b]} amt=${amt:,.2f} ({amt/total:.1%})\n")

        # Recall-style
        matched_buckets = {
            "matched_expected_same_day",
            "matched_expected_other_day",
            "matched_already_paid_earlier",
            "matched_open_other_stage",
            "matched_missing_outcomes",
            "eft_in_checks_no_payments",
        }
        matched_amt = sum(bucket_amt[b] for b in matched_buckets)
        fh.write("\nRecall (deposit $ with any EFT→recon link):\n")
        fh.write(f"  linked: ${matched_amt:,.2f} ({matched_amt/total:.1%})\n")
        fh.write(
            f"  unlinked: ${total-matched_amt:,.2f} ({(total-matched_amt)/total:.1%})\n"
        )

        # Echo / PayPlus processor join recall
        echo_rows = [
            r
            for r in rows_out
            if is_ach_processor(str(r["payer_org_resolved"]))
            or any(
                x in f"{r['ach_payer_head']} {r['description']}".upper()
                for x in ("ECHO", "PAY PLUS", "PAYPLUS")
            )
        ]
        echo_total = sum(float(r["amount"]) for r in echo_rows)
        echo_linked = sum(
            float(r["amount"]) for r in echo_rows if r["bucket"] in matched_buckets
        )
        fh.write("\nEcho Join Recall (Echo/PayPlus processor deposits):\n")
        fh.write(f"  deposits: n={len(echo_rows)} amt=${echo_total:,.2f}\n")
        if echo_total > 0:
            fh.write(
                f"  linked: ${echo_linked:,.2f} ({echo_linked/echo_total:.1%})\n"
            )
            fh.write(
                f"  unlinked: ${echo_total-echo_linked:,.2f} "
                f"({(echo_total-echo_linked)/echo_total:.1%})\n"
            )
        else:
            fh.write("  linked: $0.00 (n/a)\n")

        same = bucket_amt.get("matched_expected_same_day", 0)
        other = bucket_amt.get("matched_expected_other_day", 0)
        paid_earlier = bucket_amt.get("matched_already_paid_earlier", 0)
        fh.write("\nAmong linked (timing vs Expected land day):\n")
        fh.write(f"  expected_same_day: ${same:,.2f}\n")
        fh.write(f"  expected_other_day: ${other:,.2f}\n")
        fh.write(f"  already_paid_earlier: ${paid_earlier:,.2f}\n")

        # Top unlinked insurance-like by head
        fh.write("\nTop unlinked (no_eob_yet / unmapped / unmatched) by ach head:\n")
        by_head: dict[str, float] = defaultdict(float)
        for r in rows_out:
            if r["bucket"] in {
                "unmapped_processor",
                "no_eob_yet",
                "unmatched_eft_not_in_recon",
                "unmatched_no_eft",
            }:
                by_head[str(r["ach_payer_head"] or r["payer_org_resolved"])] += float(
                    r["amount"]
                )
        for h, amt in sorted(by_head.items(), key=lambda kv: -kv[1])[:12]:
            fh.write(f"  {h}: ${amt:,.2f}\n")

        # Anthem focus
        fh.write("\nAnthem / BCBS deposits:\n")
        ant = [r for r in rows_out if "Anthem" in str(r["payer_org_resolved"])]
        fh.write(f"  n={len(ant)} amt=${sum(float(r['amount']) for r in ant):,.2f}\n")
        ac: Counter[str] = Counter(str(r["bucket"]) for r in ant)
        for b, n in ac.most_common():
            a = sum(float(r["amount"]) for r in ant if r["bucket"] == b)
            fh.write(f"    {b}: n={n} ${a:,.2f}\n")

        # Residual narrative helpers
        patient = bucket_amt.get("patient_non_ar", 0)
        insurance_like = total - patient
        fh.write("\n--- Residual framing ---\n")
        fh.write(f"Patient-ish: ${patient:,.2f}\n")
        fh.write(f"Insurance-like deposits: ${insurance_like:,.2f}\n")
        fh.write(
            "Expected land (from outcomes ofd same day) is computed separately in RCA;\n"
            "use bucket rollup to attribute why deposits did not land in open same-day AR.\n"
        )

    print(f"Wrote {out_csv}")
    print(f"Wrote {summary_path}")
    return summary_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", default="2026-07-29")
    ap.add_argument(
        "--tracker",
        default="webpt_edco_scraper/Transaction Tracker 2026.xlsx",
    )
    ap.add_argument(
        "--forecast-dir",
        default="webpt_edco_scraper/output/jun_jul_2026/forecast",
    )
    ap.add_argument(
        "--recon-dir",
        default="webpt_edco_scraper/output/jun_jul_2026/reconciliation",
    )
    args = ap.parse_args()
    tracker = Path(args.tracker)
    forecast = Path(args.forecast_dir)
    recon = Path(args.recon_dir)
    if not tracker.is_absolute():
        tracker = _REPO / tracker
    if not forecast.is_absolute():
        forecast = _REPO / forecast
    if not recon.is_absolute():
        recon = _REPO / recon
    run_trace(
        tracker_path=tracker,
        forecast_dir=forecast,
        recon_dir=recon,
        target=date.fromisoformat(args.date),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

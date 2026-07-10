"""Denials 2026 insights: root-cause analysis + interactive Fix & Prevent dashboard.

Turns the scraped denial data into (a) a machine-readable insights JSON + a few
cross-tab CSVs and (b) a self-contained interactive HTML dashboard that answers:
what are the top denial reasons, how do we resolve them, and how do we prevent them.

Reuses the existing analytics core (no edits to those modules):
  - analyze_denials_list.load_rows / summarize      -> denial-level buckets & totals
  - denial_categories.is_preventable /
      PREVENTABLE_DENIAL_CATEGORIES / analyze_denial_lines / categorize_carc
  - denials_normalize.parse_money

Run from the waystar_scraper/ directory (same convention as analyze_denials_list.py):

    python denials_insights.py \
        --input output/denials_2026_all/denials_merged_clean.csv \
        --lines output/denials_2026_all/denials_lines_fact.csv   # optional; adds CARC depth

If --lines is omitted (or the file is absent), the dashboard is built at the
denial_category level and shows CARC coverage as "0 of N enriched". Re-run with
--lines once enrich_denials.py + transform_denials.py have produced the fact table.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from analyze_denials_list import load_rows, summarize
from denial_categories import (
    PREVENTABLE_DENIAL_CATEGORIES,
    analyze_denial_lines,
    is_preventable,
)
from denials_normalize import parse_money

TOP_PAYERS = 6
TOP_CATS = 6

# ---------------------------------------------------------------------------
# Fix & Prevent playbook — authored RCM guidance, keyed by denial_category.
# Grounded in denial_categories.CARC_CATEGORY_RULES / PREVENTABLE_DENIAL_CATEGORIES.
# ---------------------------------------------------------------------------
CATEGORY_PLAYBOOK: dict[str, dict[str, str]] = {
    "Coding": {
        "why": "Service coded in a way the payer won't accept — bundling / NCCI edits "
        "(CO-97), unbundled or mismatched CPT/HCPCS, or a missing/invalid modifier.",
        "resolve": "Pull the remit, read the CARC/RARC; add the correct modifier (e.g. 59/XU) "
        "or re-code and resubmit; if coding is defensible, appeal with op notes.",
        "prevent": "Run NCCI/CCI bundling + modifier edits in the scrubber before submission; "
        "coder feedback loop on the top offending CPTs; payer coding rules at charge entry.",
    },
    "Benefits": {
        "why": "Service is not a covered benefit, a benefit maximum is reached, or a plan "
        "limitation applies (CO-96 / CO-119). MetroPlus drives almost all of these.",
        "resolve": "Verify the benefit/limit on the remit; bill patient or secondary if truly "
        "non-covered; appeal if covered under plan terms.",
        "prevent": "Real-time benefit verification (270/271) at scheduling; maintain a "
        "MetroPlus benefit-limit matrix; flag visits that exceed plan caps before the visit.",
    },
    "Missing or Invalid Data": {
        "why": "A required claim field is missing or invalid — subscriber ID, DOB, provider "
        "NPI/taxonomy, referring provider (CO-16 + RARC). BCBS Empire drives most of these.",
        "resolve": "Correct the flagged field from the RARC remark and resubmit — usually a "
        "fast clean-claim fix, not an appeal.",
        "prevent": "Front-end required-field validation + scrubber edits; auto-populate "
        "NPI/taxonomy; demographic verification at check-in; BCBS-specific field rules.",
    },
    "Payer Guidelines": {
        "why": "Claim violates a payer-specific policy or edit (documentation, place of "
        "service, frequency). UMR is the main source.",
        "resolve": "Read the policy cited on the remit; supply the required documentation or "
        "correct POS/units and resubmit or appeal.",
        "prevent": "Maintain a payer-policy library (esp. UMR); build the top guideline checks "
        "into the scrubber; educate schedulers on POS rules.",
    },
    "Authorization": {
        "why": "No, expired, or invalid prior authorization / precert (CO-197 / CO-15). "
        "Highest dollars per denial. HealthFirst is the main source.",
        "resolve": "File a retro-authorization request; appeal with the auth number if one "
        "exists; attach medical-necessity documentation.",
        "prevent": "Auth check at scheduling against a payer auth-required matrix; hold/flag "
        "scheduling until auth is secured; put HealthFirst auth rules front and center.",
    },
    "Duplicate": {
        "why": "Payer sees the claim/line as a duplicate of one already adjudicated "
        "(CO-18 / CO-B7) — often an auto-rebill or a double charge entry.",
        "resolve": "Confirm the original's status; if truly duplicate, void; if distinct, "
        "resubmit with the appropriate modifier and documentation.",
        "prevent": "De-dupe check on claim submission; suppress automatic rebills until the "
        "first claim adjudicates; charge-entry duplicate guard.",
    },
    "Coordination of Benefits": {
        "why": "Wrong payer order or missing other-insurance info (CO-22 / CO-109) — primary "
        "vs secondary confusion.",
        "resolve": "Update the COB order, attach the primary EOB, and resubmit to the correct "
        "payer.",
        "prevent": "COB verification at registration; keep other-insurance on file current; "
        "eligibility check that returns COB order.",
    },
    "Eligibility": {
        "why": "Patient not eligible or coverage inactive on the date of service "
        "(CO-27 / CO-31).",
        "resolve": "Re-verify coverage for the DOS; bill the correct active plan or the "
        "patient; appeal with proof of eligibility if the payer erred.",
        "prevent": "Real-time 270/271 eligibility at check-in and again before billing; alert "
        "on termed coverage.",
    },
    "Medical Necessity": {
        "why": "Payer deems the service not medically necessary for the diagnosis "
        "(CO-50 / CO-4) — dx-to-procedure mismatch or missing documentation.",
        "resolve": "Appeal with clinical documentation and correct the dx linkage; add "
        "supporting ICD-10 codes if justified.",
        "prevent": "LCD/NCD and payer medical-policy checks at order/charge entry; dx-to-CPT "
        "validation; provider documentation prompts.",
    },
    "Property and Casualty": {
        "why": "Auto or workers-comp claim routed to the health plan, or missing P&C info.",
        "resolve": "Redirect to the P&C carrier / claim number and resubmit.",
        "prevent": "Capture accident / P&C info at registration; route P&C claims to the "
        "correct carrier upfront.",
    },
    "Timely Filing": {
        "why": "Claim submitted after the payer's filing deadline (CO-29).",
        "resolve": "Appeal with proof of timely original submission (clearinghouse acceptance "
        "report); write off only if unavoidable.",
        "prevent": "Filing-deadline alerts by payer; work the SLA-breach queue first; monitor "
        "unbilled / held claims daily.",
    },
    "Diagnosis": {
        "why": "Invalid, incomplete, or non-specific diagnosis code (CO-11 / CO-146).",
        "resolve": "Correct / specify the ICD-10 code to the highest specificity and resubmit.",
        "prevent": "ICD-10 specificity edits in the scrubber; coder review of flagged dx; "
        "provider documentation prompts.",
    },
}
DEFAULT_PLAY = {
    "why": "Payer-specific denial reason — inspect the remit CARC/RARC for the exact cause.",
    "resolve": "Read the remit code, correct the underlying issue, and resubmit or appeal.",
    "prevent": "Add the offending edit to the pre-submission scrubber once the pattern is clear.",
}


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def _bucket_to_rows(bucket: list[tuple[str, int, float]], total_count: int, total_amt: float) -> list[dict]:
    return [
        {
            "name": name,
            "count": count,
            "amount": round(amount, 2),
            "pct_count": round(100 * count / total_count, 1) if total_count else 0.0,
            "pct_amount": round(100 * amount / total_amt, 1) if total_amt else 0.0,
        }
        for name, count, amount in bucket
    ]


def build_denial_insights(rows: list[dict]) -> dict:
    stats = summarize(rows)
    n = stats["denial_count"]
    total_denied = stats["total_denied"]

    categories = _bucket_to_rows(stats["category"], n, total_denied)
    for cat in categories:
        cat["preventable"] = is_preventable("", "", cat["name"])

    # Preventable vs not (denial_category level via existing rule)
    prev_count = prev_amt = 0
    for cat in categories:
        if cat["preventable"]:
            prev_count += cat["count"]
            prev_amt += cat["amount"]
    preventable = {
        "preventable": {"count": prev_count, "amount": round(prev_amt, 2)},
        "not_preventable": {
            "count": n - prev_count,
            "amount": round(total_denied - prev_amt, 2),
        },
        "pct_count": round(100 * prev_count / n, 1) if n else 0.0,
        "pct_amount": round(100 * prev_amt / total_denied, 1) if total_denied else 0.0,
    }

    # Payer x category cross-tab (top payers x top categories, by count)
    top_payers = [p["name"] for p in _bucket_to_rows(stats["payer"], n, total_denied)[:TOP_PAYERS]]
    top_cats = [c["name"] for c in categories[:TOP_CATS]]
    cell = defaultdict(int)
    payer_totals = Counter()
    for r in rows:
        p = (r.get("payer_name") or "?").strip() or "?"
        c = (r.get("denial_category") or "?").strip() or "?"
        payer_totals[p] += 1
        if p in top_payers and c in top_cats:
            cell[(p, c)] += 1
    crosstab = {
        "payers": top_payers,
        "categories": top_cats,
        "payer_totals": {p: payer_totals[p] for p in top_payers},
        "matrix": [[cell[(p, c)] for c in top_cats] for p in top_payers],
    }

    # SLA / escalation dollar exposure
    sla = _bucket_to_rows(stats["sla"], n, total_denied)
    escalation = _bucket_to_rows(stats["escalation"], n, total_denied)

    return {
        "denial_count": n,
        "total_denied": round(total_denied, 2),
        "total_billed": round(stats["total_billed"], 2),
        "total_allowed": round(stats["total_allowed"], 2),
        "avg_denied": round(total_denied / n, 2) if n else 0.0,
        "categories": categories,
        "payers": _bucket_to_rows(stats["payer"], n, total_denied),
        "aging": _bucket_to_rows(stats["aging"], n, total_denied),
        "sla": sla,
        "escalation": escalation,
        "claim_type": _bucket_to_rows(stats["claim_type"], n, total_denied),
        "months": _bucket_to_rows(stats["months"], n, total_denied),
        "preventable": preventable,
        "crosstab": crosstab,
    }


def build_line_insights(line_rows: list[dict]) -> dict | None:
    if not line_rows:
        return None
    a = analyze_denial_lines(line_rows)
    enriched_ids = {r.get("denial_id", "") for r in line_rows if r.get("denial_id")}
    with_codes = sum(1 for r in line_rows if (r.get("carc_codes") or "").strip())

    def top(counter: Counter, k: int = 15) -> list[dict]:
        return [{"name": name, "count": count} for name, count in counter.most_common(k)]

    cat_amounts = a["category_amounts"]
    root = [
        {"name": name, "count": count, "amount": round(cat_amounts.get(name, 0.0), 2)}
        for name, count in a["category_counts"].most_common(15)
    ]
    return {
        "line_count": a["line_count"],
        "enriched_denials": len(enriched_ids),
        "lines_with_codes": with_codes,
        "top_carc": top(a["carc_counts"]),
        "top_rarc": top(a["remark_counts"], 10),
        "top_proc": top(a["proc_counts"], 10),
        "root_cause": root,
        "resolution": top(a["resolution_counts"], 10),
    }


# ---------------------------------------------------------------------------
# Rendering helpers (server-side; JS only adds tooltips + theme toggle)
# ---------------------------------------------------------------------------
def _fmt_money(v: float) -> str:
    v = float(v)
    if abs(v) >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:,.0f}"


def _esc(s) -> str:
    return html.escape(str(s))


def _hbars(items: list[dict], value_key: str, label_fn, *, color="var(--series-1)", max_val=None) -> str:
    if not items:
        return '<p class="empty">No data.</p>'
    mx = max_val or max((it[value_key] for it in items), default=1) or 1
    rows = []
    for it in items:
        w = max(1.5, 100 * it[value_key] / mx)
        rows.append(
            f'<div class="bar-row" data-tip="{_esc(label_fn(it, tip=True))}">'
            f'<span class="bar-label">{_esc(it["name"])}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{w:.1f}%;background:{color}"></span></span>'
            f'<span class="bar-value">{_esc(label_fn(it, tip=False))}</span>'
            f"</div>"
        )
    return '<div class="bars">' + "".join(rows) + "</div>"


def _heatmap(ct: dict) -> str:
    payers, cats, matrix = ct["payers"], ct["categories"], ct["matrix"]
    mx = max((max(r) for r in matrix), default=1) or 1
    head = "".join(f"<th>{_esc(c)}</th>" for c in cats)
    body = []
    for i, p in enumerate(payers):
        cells = []
        for j, c in enumerate(cats):
            v = matrix[i][j]
            t = v / mx
            alpha = 0.06 + 0.9 * t
            dark_cell = alpha > 0.55
            txt = "color:#fff;" if dark_cell else ""
            tip = f"{p} — {c}: {v:,} denials"
            label = f"{v:,}" if v else "·"
            cells.append(
                f'<td class="hm-cell" style="background:rgba(42,120,214,{alpha:.2f});{txt}" '
                f'data-tip="{_esc(tip)}">{label}</td>'
            )
        body.append(f"<tr><th class='hm-row'>{_esc(p)}</th>{''.join(cells)}</tr>")
    return (
        '<div class="table-scroll"><table class="heatmap">'
        f"<thead><tr><th></th>{head}</tr></thead>"
        f"<tbody>{''.join(body)}</tbody></table></div>"
        '<p class="cap">Cell = denial count. Darker = more denials. Each payer\'s dominant '
        "failure mode is its darkest cell.</p>"
    )


def _stacked_prevent(p: dict) -> str:
    pc, npc = p["preventable"], p["not_preventable"]
    tot = pc["amount"] + npc["amount"] or 1
    w1 = 100 * pc["amount"] / tot
    return (
        '<div class="stack">'
        f'<span class="seg" style="width:{w1:.1f}%;background:var(--series-1)" '
        f'data-tip="Preventable: {_fmt_money(pc["amount"])} · {pc["count"]:,} denials"></span>'
        f'<span class="seg" style="width:{100 - w1:.1f}%;background:var(--muted-fill)" '
        f'data-tip="Not preventable / needs appeal: {_fmt_money(npc["amount"])} · {npc["count"]:,} denials"></span>'
        "</div>"
        '<div class="stack-legend">'
        f'<span><i class="sw" style="background:var(--series-1)"></i>Preventable — '
        f'{_fmt_money(pc["amount"])} ({p["pct_amount"]}% of $, {p["pct_count"]}% of denials)</span>'
        f'<span><i class="sw" style="background:var(--muted-fill)"></i>Not preventable — '
        f'{_fmt_money(npc["amount"])}</span>'
        "</div>"
    )


STATUS_COLOR = {
    "On Track": "var(--good)",
    "At Risk": "var(--warning)",
    "Breached": "var(--critical)",
}


def _sla_bars(sla: list[dict], total: int) -> str:
    order = {"Breached": 0, "At Risk": 1, "On Track": 2}
    items = sorted(sla, key=lambda x: order.get(x["name"], 9))
    rows = []
    mx = max((s["count"] for s in items), default=1) or 1
    for s in items:
        w = max(1.5, 100 * s["count"] / mx)
        col = STATUS_COLOR.get(s["name"], "var(--series-1)")
        tip = f"{s['name']}: {s['count']:,} denials · {_fmt_money(s['amount'])}"
        rows.append(
            f'<div class="bar-row" data-tip="{_esc(tip)}">'
            f'<span class="bar-label">{_esc(s["name"])}</span>'
            f'<span class="bar-track"><span class="bar-fill" style="width:{w:.1f}%;background:{col}"></span></span>'
            f'<span class="bar-value">{s["count"]:,} · {_fmt_money(s["amount"])}</span></div>'
        )
    return '<div class="bars">' + "".join(rows) + "</div>"


def _kpi(label: str, value: str, sub: str = "") -> str:
    sub_html = f'<div class="kpi-sub">{_esc(sub)}</div>' if sub else ""
    return f'<div class="kpi"><div class="kpi-val">{_esc(value)}</div><div class="kpi-label">{_esc(label)}</div>{sub_html}</div>'


def _playbook(categories: list[dict]) -> str:
    rows = []
    for cat in categories:
        name = cat["name"]
        pb = CATEGORY_PLAYBOOK.get(name, DEFAULT_PLAY)
        badge = '<span class="badge prev">Preventable</span>' if cat["preventable"] else '<span class="badge appeal">Work/Appeal</span>'
        rows.append(
            "<tr>"
            f'<td class="pb-cat"><strong>{_esc(name)}</strong>{badge}'
            f'<div class="pb-stat">{cat["count"]:,} denials · {_fmt_money(cat["amount"])} · {cat["pct_count"]}%</div>'
            f'<div class="pb-why">{_esc(pb["why"])}</div></td>'
            f'<td>{_esc(pb["resolve"])}</td>'
            f'<td>{_esc(pb["prevent"])}</td>'
            "</tr>"
        )
    return (
        '<div class="table-scroll"><table class="playbook">'
        "<thead><tr><th>Denial reason</th><th>Resolve now</th><th>Prevent before it happens</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )


def _line_section(li: dict | None, total_denials: int) -> str:
    if not li:
        return (
            '<section class="card wide"><h2>CARC / RARC root cause</h2>'
            '<div class="coverage warn">CARC-level detail not loaded — '
            f"<strong>0 of {total_denials:,}</strong> denials enriched. "
            "Showing category-level analysis. Run <code>enrich_denials.py</code> then "
            "<code>transform_denials.py</code> and re-run with <code>--lines "
            "denials_lines_fact.csv</code> to resolve each category into specific CARC codes.</div>"
            "</section>"
        )
    carc = _hbars(li["top_carc"], "count", lambda it, tip: f"{it['name']}: {it['count']:,} lines")
    proc = _hbars(li["top_proc"], "count", lambda it, tip: f"{it['name']}: {it['count']:,} lines", color="var(--series-2)")
    return (
        '<section class="card wide"><h2>CARC / RARC root cause</h2>'
        f'<div class="coverage ok"><strong>{li["enriched_denials"]:,} of {total_denials:,}</strong> '
        f'denials enriched · {li["line_count"]:,} remit lines · {li["lines_with_codes"]:,} with codes</div>'
        '<div class="two-col">'
        f"<div><h3>Top CARC codes</h3>{carc}</div>"
        f"<div><h3>Top procedure codes</h3>{proc}</div>"
        "</div></section>"
    )


def render_dashboard(di: dict, li: dict | None, *, source: str) -> str:
    cats = di["categories"]
    n = di["denial_count"]
    breached = next((s for s in di["sla"] if s["name"] == "Breached"), {"count": 0, "amount": 0})
    esc_true = next((e for e in di["escalation"] if e["name"] in ("True", "true", "1")), {"count": 0})

    kpis = "".join(
        [
            _kpi("Open denials", f"{n:,}", f"2026 YTD · {source}"),
            _kpi("Denied $", _fmt_money(di["total_denied"]), f"on {_fmt_money(di['total_billed'])} billed"),
            _kpi("Avg / denial", _fmt_money(di["avg_denied"]), "denied amount"),
            _kpi("Preventable", f"{di['preventable']['pct_count']}%", f"{_fmt_money(di['preventable']['preventable']['amount'])} of denials"),
            _kpi("SLA breached", f"{breached['count']:,}", f"{_fmt_money(breached['amount'])} at risk"),
            _kpi("Escalation-flagged", f"{esc_true['count']:,}", f"{round(100 * esc_true['count'] / n) if n else 0}% of denials"),
        ]
    )

    reasons = _hbars(
        cats,
        "count",
        lambda it, tip: (
            f"{it['name']}: {it['count']:,} denials ({it['pct_count']}%) · {_fmt_money(it['amount'])}"
            if tip
            else f"{it['count']:,} · {_fmt_money(it['amount'])}"
        ),
    )
    payers = _hbars(
        di["payers"][:10],
        "amount",
        lambda it, tip: (
            f"{it['name']}: {_fmt_money(it['amount'])} · {it['count']:,} denials ({it['pct_count']}%)"
            if tip
            else _fmt_money(it["amount"])
        ),
        color="var(--series-2)",
    )
    aging = _hbars(
        di["aging"],
        "count",
        lambda it, tip: f"{it['name']} days: {it['count']:,} · {_fmt_money(it['amount'])}"
        if tip
        else f"{it['count']:,}",
        color="var(--series-1)",
    )
    months = _hbars(
        di["months"],
        "count",
        lambda it, tip: f"{it['name']}: {it['count']:,} denials · {_fmt_money(it['amount'])}"
        if tip
        else f"{it['count']:,}",
        color="var(--series-1)",
    )
    fin_items = [
        {"name": "Billed", "amount": di["total_billed"]},
        {"name": "Allowed", "amount": di["total_allowed"]},
        {"name": "Denied", "amount": di["total_denied"]},
    ]
    financial = _hbars(fin_items, "amount", lambda it, tip: f"{it['name']}: {_fmt_money(it['amount'])}" if tip else _fmt_money(it["amount"]), color="var(--series-1)")

    takeaways = [
        f"<strong>{di['preventable']['pct_count']}% of denials are preventable at the front end.</strong> "
        "Coding, Authorization, Duplicate and Timely-Filing denials are avoidable with scrubber edits, "
        "auth-at-scheduling and eligibility checks — not back-end appeals.",
        f"<strong>Top-5 payers carry the majority of the $.</strong> Each has a signature failure mode "
        "(see the heatmap): fix per-payer, not generically — e.g. MetroPlus→Benefits, "
        "BCBS Empire→Missing Data, HealthFirst→Coding + Auth.",
        f"<strong>{breached['count']:,} denials ({_fmt_money(breached['amount'])}) have already breached SLA.</strong> "
        "Work that queue first — aged denials convert to write-offs and timely-filing losses.",
        "<strong>Coding alone is the single biggest bucket.</strong> A modifier/bundling scrubber pass "
        "targeting the top CPTs is the highest-leverage single fix.",
    ]
    takeaway_html = "".join(f"<li>{t}</li>" for t in takeaways)

    return f"""<title>Denials 2026 — Root-Cause, Fix &amp; Prevent</title>
<style>{_CSS}</style>
<div class="viz-root" id="app">
  <header class="head">
    <div>
      <h1>Denials 2026 — Root-Cause, Fix &amp; Prevent</h1>
      <p class="sub">{n:,} open denials · {_fmt_money(di['total_denied'])} denied · analysis of <code>{_esc(source)}</code></p>
    </div>
    <button id="theme-btn" class="theme-btn" title="Toggle light/dark">◐</button>
  </header>

  <section class="kpis">{kpis}</section>

  <main class="grid">
  <section class="card"><h2>Top denial reasons</h2><p class="cap">By denial count; label shows count and denied $.</p>{reasons}</section>
  <section class="card"><h2>Top payers by denied $</h2><p class="cap">Top-5 carry ~73% of denied dollars.</p>{payers}</section>

  <section class="card wide"><h2>Payer × reason signature</h2>{_heatmap(di['crosstab'])}</section>

  <section class="card"><h2>Preventable vs. must-appeal</h2><p class="cap">Share of denied $ that is avoidable at the front end.</p>{_stacked_prevent(di['preventable'])}</section>
  <section class="card"><h2>SLA exposure</h2><p class="cap">Breached denials risk write-off &amp; timely-filing loss.</p>{_sla_bars(di['sla'], n)}</section>

  <section class="card"><h2>Aging</h2><p class="cap">Most denials are still workable (&le;60 days).</p>{aging}</section>
  <section class="card"><h2>Denial volume by month</h2>{months}</section>

  <section class="card"><h2>Billed → Allowed → Denied</h2><p class="cap">Denied $ in context of total billed.</p>{financial}</section>

  {_line_section(li, n)}

  <section class="card wide takeaways"><h2>Strategic takeaways</h2><ol>{takeaway_html}</ol></section>

  <section class="card wide"><h2>Fix &amp; Prevent playbook</h2>
    <p class="cap">Per reason: why it happens, how to resolve open denials now, and how to stop it upstream.</p>
    {_playbook(cats)}
  </section>
  </main>

  <footer class="foot">Generated by <code>denials_insights.py</code> · reuses <code>analyze_denials_list</code> + <code>denial_categories</code>. Playbook = RCM guidance; verify per payer contract.</footer>
</div>
<div id="tip" class="tip" role="tooltip"></div>
<script>{_JS}</script>
"""


_CSS = """
.viz-root{--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;--text-secondary:#52514e;
--muted:#898781;--grid:#e1e0d9;--baseline:#c3c2b7;--border:rgba(11,11,11,.10);
--series-1:#2a78d6;--series-2:#1baf7a;--muted-fill:#c3c2b7;
--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;
font-family:system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--text-primary);
background:var(--page);line-height:1.45;max-width:1180px;margin:0 auto;padding:20px 18px 60px;}
@media (prefers-color-scheme:dark){.viz-root{--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--baseline:#383835;--border:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#199e70;--muted-fill:#52514e;}}
.viz-root[data-theme="dark"]{--surface-1:#1a1a19;--page:#0d0d0d;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--baseline:#383835;--border:rgba(255,255,255,.10);
--series-1:#3987e5;--series-2:#199e70;--muted-fill:#52514e;}
.viz-root[data-theme="light"]{--surface-1:#fcfcfb;--page:#f9f9f7;--text-primary:#0b0b0b;
--text-secondary:#52514e;--grid:#e1e0d9;--baseline:#c3c2b7;--border:rgba(11,11,11,.10);
--series-1:#2a78d6;--series-2:#1baf7a;--muted-fill:#c3c2b7;--good:#0ca30c;--warning:#fab219;--critical:#d03b3b;}
.head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px;}
h1{font-size:1.5rem;margin:0 0 4px;letter-spacing:-.01em;}
.sub{color:var(--text-secondary);font-size:.9rem;margin:0;}
.sub code,.foot code,.coverage code{background:var(--grid);padding:1px 5px;border-radius:4px;font-size:.85em;}
.theme-btn{background:var(--surface-1);border:1px solid var(--border);color:var(--text-primary);
border-radius:8px;width:38px;height:38px;font-size:1.1rem;cursor:pointer;flex:none;}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:20px;}
.kpi{background:var(--surface-1);border:1px solid var(--border);border-radius:12px;padding:14px 16px;}
.kpi-val{font-size:1.7rem;font-weight:650;letter-spacing:-.02em;}
.kpi-label{font-size:.8rem;color:var(--text-secondary);margin-top:2px;font-weight:550;}
.kpi-sub{font-size:.72rem;color:var(--muted);margin-top:2px;}
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;align-items:start;}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:14px;padding:18px 20px;box-sizing:border-box;min-width:0;}
.card.wide{grid-column:1/-1;}
@media (max-width:760px){.grid{grid-template-columns:1fr;}}
h2{font-size:1.05rem;margin:0 0 2px;}
h3{font-size:.9rem;margin:0 0 8px;color:var(--text-secondary);}
.cap,.caption{font-size:.78rem;color:var(--muted);margin:0 0 12px;}
.bars{display:flex;flex-direction:column;gap:7px;}
.bar-row{display:grid;grid-template-columns:150px 1fr auto;align-items:center;gap:10px;font-size:.82rem;}
.bar-label{color:var(--text-secondary);text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-track{background:var(--grid);border-radius:5px;height:16px;overflow:hidden;}
.bar-fill{display:block;height:100%;border-radius:0 5px 5px 0;min-width:3px;}
.bar-value{font-variant-numeric:tabular-nums;color:var(--text-primary);font-weight:550;white-space:nowrap;}
.table-scroll{overflow-x:auto;}
table{border-collapse:separate;border-spacing:2px;width:100%;font-size:.8rem;}
.heatmap th{color:var(--text-secondary);font-weight:550;padding:5px 6px;text-align:center;font-size:.74rem;}
.heatmap th.hm-row{text-align:right;white-space:nowrap;}
.hm-cell{text-align:center;padding:9px 6px;border-radius:5px;font-variant-numeric:tabular-nums;min-width:56px;color:var(--text-primary);}
.stack{display:flex;height:26px;border-radius:6px;overflow:hidden;gap:2px;margin-bottom:10px;}
.seg{display:block;height:100%;}
.stack-legend{display:flex;flex-wrap:wrap;gap:14px;font-size:.8rem;color:var(--text-secondary);}
.sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:middle;}
.coverage{font-size:.85rem;padding:9px 12px;border-radius:8px;margin-bottom:12px;}
.coverage.warn{background:rgba(250,178,25,.12);border:1px solid rgba(250,178,25,.4);}
.coverage.ok{background:rgba(12,163,12,.10);border:1px solid rgba(12,163,12,.35);}
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:20px;}
@media (max-width:760px){.two-col{grid-template-columns:1fr;}}
.takeaways ol{margin:6px 0 0;padding-left:20px;}
.takeaways li{margin-bottom:9px;font-size:.9rem;}
.playbook th,.playbook td{text-align:left;padding:10px 12px;vertical-align:top;border-bottom:1px solid var(--border);}
.playbook thead th{color:var(--text-secondary);font-size:.78rem;text-transform:uppercase;letter-spacing:.03em;}
.playbook td{font-size:.83rem;color:var(--text-secondary);}
.pb-cat{min-width:230px;color:var(--text-primary)!important;}
.pb-stat{font-size:.75rem;color:var(--muted);margin:3px 0 6px;font-variant-numeric:tabular-nums;}
.pb-why{font-size:.8rem;color:var(--text-secondary);font-weight:400;}
.badge{font-size:.66rem;padding:1px 7px;border-radius:20px;margin-left:8px;vertical-align:middle;font-weight:600;}
.badge.prev{background:rgba(12,163,12,.15);color:var(--good);}
.badge.appeal{background:rgba(250,178,25,.15);color:var(--warning);}
.foot{font-size:.74rem;color:var(--muted);margin-top:20px;}
.empty{color:var(--muted);font-size:.85rem;}
.tip{position:fixed;pointer-events:none;background:var(--text-primary);color:var(--surface-1);
padding:6px 10px;border-radius:7px;font-size:.78rem;max-width:260px;opacity:0;transition:opacity .1s;
z-index:50;box-shadow:0 4px 14px rgba(0,0,0,.25);}
"""

_JS = """
(function(){
  var app=document.getElementById('app'),tip=document.getElementById('tip');
  app.addEventListener('mousemove',function(e){
    var t=e.target.closest('[data-tip]');
    if(!t){tip.style.opacity=0;return;}
    tip.textContent=t.getAttribute('data-tip');tip.style.opacity=1;
    var x=e.clientX+14,y=e.clientY+14;
    if(x+tip.offsetWidth>window.innerWidth)x=e.clientX-tip.offsetWidth-14;
    if(y+tip.offsetHeight>window.innerHeight)y=e.clientY-tip.offsetHeight-14;
    tip.style.left=x+'px';tip.style.top=y+'px';
  });
  app.addEventListener('mouseleave',function(){tip.style.opacity=0;});
  var btn=document.getElementById('theme-btn');
  btn&&btn.addEventListener('click',function(){
    var cur=app.getAttribute('data-theme');
    var dark=cur?cur==='dark':window.matchMedia('(prefers-color-scheme:dark)').matches;
    app.setAttribute('data-theme',dark?'light':'dark');
  });
})();
"""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Denials root-cause insights + dashboard")
    parser.add_argument("--input", type=Path, default=Path("output/denials_2026_all/denials_merged_clean.csv"))
    parser.add_argument("--lines", type=Path, default=None, help="Optional denials_lines_fact.csv for CARC depth")
    parser.add_argument("--outdir", type=Path, default=None, help="Defaults to the input file's folder")
    args = parser.parse_args()

    input_path = args.input.resolve()
    if not input_path.exists():
        raise SystemExit(f"Input not found: {input_path}")
    outdir = (args.outdir or input_path.parent).resolve()

    rows = load_rows(input_path)
    if not rows:
        raise SystemExit("No rows found.")
    di = build_denial_insights(rows)

    li = None
    if args.lines and args.lines.exists():
        li = build_line_insights(load_rows(args.lines.resolve()))

    # Data feed
    (outdir / "denials_insights.json").write_text(
        json.dumps({"denial_level": di, "line_level": li}, indent=2), encoding="utf-8"
    )
    _write_csv(outdir / "insight_by_category.csv", di["categories"],
               ["name", "count", "amount", "pct_count", "pct_amount", "preventable"])
    _write_csv(outdir / "insight_by_payer.csv", di["payers"],
               ["name", "count", "amount", "pct_count", "pct_amount"])
    ct = di["crosstab"]
    xrows = [dict({"payer": p}, **{c: ct["matrix"][i][j] for j, c in enumerate(ct["categories"])})
             for i, p in enumerate(ct["payers"])]
    _write_csv(outdir / "insight_payer_x_category.csv", xrows, ["payer"] + ct["categories"])

    # Dashboard (body-inner content: openable standalone AND publishable as an Artifact)
    inner = render_dashboard(di, li, source=input_path.name)
    (outdir / "denials_dashboard.html").write_text(inner, encoding="utf-8")

    print(f"Denials: {di['denial_count']:,} | Denied {_fmt_money(di['total_denied'])} | "
          f"Billed {_fmt_money(di['total_billed'])} | Preventable {di['preventable']['pct_count']}%")
    for c in di["categories"][:6]:
        print(f"  {c['name']:<26} {c['count']:>7,}  {c['pct_count']:>5}%  {_fmt_money(c['amount'])}")
    print(f"Wrote: {outdir/'denials_dashboard.html'}")
    print(f"       {outdir/'denials_insights.json'} (+ 3 CSVs)")


if __name__ == "__main__":
    main()

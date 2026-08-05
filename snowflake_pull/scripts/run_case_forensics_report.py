"""Offline Performance Engineering Platform analyzer (observer data only).

Reads case_phases.jsonl / http_requests.jsonl / etc. under --out-dir/reports
and writes all forensic deliverables. Never changes pipeline knobs.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from snowflake_pull.case_forensics import (  # noqa: E402
    MIN_CASES_FOR_RECS,
    MIN_HTTP_FOR_RECS,
    critical_path_sec,
    is_app_api_url,
    normalize_endpoint,
    percentile,
    sample_gate_ok,
    summarize,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _pct(part: float, whole: float) -> float:
    if whole <= 0:
        return 0.0
    return 100.0 * part / whole


def analyze(reports_dir: Path) -> dict[str, Any]:
    cases = _read_jsonl(reports_dir / "case_phases.jsonl")
    http = _read_jsonl(reports_dir / "http_requests.jsonl")
    io_ev = _read_jsonl(reports_dir / "io_events.jsonl")
    resources = _read_jsonl(reports_dir / "resource_usage.jsonl")
    session = _read_jsonl(reports_dir / "session_health.jsonl")
    pdfs = _read_csv(reports_dir / "pdf_downloads.csv")
    switches = _read_csv(reports_dir / "facility_switch_history.csv")
    retries = _read_csv(reports_dir / "retry_lifecycle.csv")
    queue = _read_csv(reports_dir / "queue_latency.csv")
    browser = _read_csv(reports_dir / "browser_timing.csv")

    n_cases = len(cases)
    n_http = len(http)
    gate = sample_gate_ok(n_cases, n_http)

    # --- Phase statistics ---
    phase_vals: dict[str, list[float]] = defaultdict(list)
    walls: list[float] = []
    for c in cases:
        walls.append(float(c.get("wall_sec") or 0))
        for name, dur in (c.get("phases") or {}).items():
            phase_vals[name].append(float(dur))
    phase_stats = {k: summarize(v) for k, v in sorted(phase_vals.items())}
    _write(
        reports_dir / "phase_statistics.json",
        json.dumps({"n_cases": n_cases, "phases": phase_stats, "wall": summarize(walls)}, indent=2),
    )
    phase_rows = []
    for name, st in phase_stats.items():
        phase_rows.append({"phase": name, **{k: round(v, 4) for k, v in st.items()}})
    _write_csv(
        reports_dir / "phase_timing.csv",
        ["phase", "count", "avg", "median", "p90", "p95", "p99", "max"],
        phase_rows,
    )

    # --- HTTP endpoint stats ---
    by_ep: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for h in http:
        by_ep[str(h.get("endpoint") or "unknown")].append(h)
    ep_rows = []
    for ep, rows in sorted(by_ep.items(), key=lambda kv: -sum(float(r.get("elapsed_sec") or 0) for r in kv[1])):
        el = [float(r.get("elapsed_sec") or 0) for r in rows]
        st = summarize(el)
        n403 = sum(1 for r in rows if int(r.get("status") or 0) == 403)
        n429 = sum(1 for r in rows if int(r.get("status") or 0) == 429)
        nto = sum(1 for r in rows if int(r.get("status") or 0) == 0 or "timeout" in str(r.get("exception") or "").lower())
        ok = sum(1 for r in rows if 200 <= int(r.get("status") or 0) < 400)
        bytes_sum = sum(int(r.get("bytes") or 0) for r in rows)
        ep_rows.append(
            {
                "Endpoint": ep,
                "Calls": len(rows),
                "Success %": round(_pct(ok, len(rows)), 2),
                "403": n403,
                "429": n429,
                "Timeout": nto,
                "Avg": round(st["avg"], 4),
                "Median": round(st["median"], 4),
                "P90": round(st["p90"], 4),
                "P95": round(st["p95"], 4),
                "P99": round(st["p99"], 4),
                "Max": round(st["max"], 4),
                "Downloaded Bytes": bytes_sum,
            }
        )
    _write_csv(
        reports_dir / "http_endpoint_statistics.csv",
        [
            "Endpoint",
            "Calls",
            "Success %",
            "403",
            "429",
            "Timeout",
            "Avg",
            "Median",
            "P90",
            "P95",
            "P99",
            "Max",
            "Downloaded Bytes",
        ],
        ep_rows,
    )

    # --- HTTP ↔ Phase correlation ---
    phase_http: dict[str, list[dict[str, Any]]] = defaultdict(list)
    phase_dur_total: dict[str, float] = defaultdict(float)
    for c in cases:
        for name, dur in (c.get("phases") or {}).items():
            phase_dur_total[name] += float(dur)
    for h in http:
        pname = str(h.get("phase_name") or h.get("phase_id") or "unknown")
        phase_http[pname].append(h)
    corr_rows = []
    for phase, rows in sorted(phase_http.items(), key=lambda kv: -sum(float(r.get("elapsed_sec") or 0) for r in kv[1])):
        el = [float(r.get("elapsed_sec") or 0) for r in rows]
        http_wall = sum(el)
        pdur = phase_dur_total.get(phase, 0.0) or http_wall
        # dominant endpoint
        ep_sum: dict[str, float] = defaultdict(float)
        for r in rows:
            ep_sum[str(r.get("endpoint") or "")] += float(r.get("elapsed_sec") or 0)
        dom = max(ep_sum.items(), key=lambda kv: kv[1]) if ep_sum else ("", 0.0)
        slow = max(rows, key=lambda r: float(r.get("elapsed_sec") or 0)) if rows else {}
        corr_rows.append(
            {
                "Phase": phase,
                "HTTP Calls": len(rows),
                "HTTP Wall Time": round(http_wall, 4),
                "Average Request": round(sum(el) / len(el), 4) if el else 0,
                "Slowest Request": round(float(slow.get("elapsed_sec") or 0), 4),
                "Slowest Endpoint": slow.get("endpoint", ""),
                "% Phase Waiting On HTTP": round(_pct(http_wall, pdur), 2),
                "Dominant Endpoint": dom[0],
                "Dominant Share Sec": round(dom[1], 4),
            }
        )
    _write_csv(
        reports_dir / "http_phase_correlation.csv",
        [
            "Phase",
            "HTTP Calls",
            "HTTP Wall Time",
            "Average Request",
            "Slowest Request",
            "Slowest Endpoint",
            "% Phase Waiting On HTTP",
            "Dominant Endpoint",
            "Dominant Share Sec",
        ],
        corr_rows,
    )

    # --- IO statistics ---
    io_by: dict[str, list[float]] = defaultdict(list)
    for e in io_ev:
        io_by[str(e.get("kind") or "unknown")].append(float(e.get("duration_sec") or 0))
    io_lines = ["# IO Statistics", "", f"Events: {len(io_ev)}", ""]
    total_io = sum(sum(v) for v in io_by.values())
    largest = max(io_by.items(), key=lambda kv: sum(kv[1]), default=("none", []))
    io_lines.append(f"- Total IO time: {total_io:.4f}s")
    io_lines.append(f"- Largest contributor: {largest[0]} ({sum(largest[1]):.4f}s)")
    write_lat = io_by.get("write") or io_by.get("manifest_write") or []
    io_lines.append(f"- Average file write latency: {(sum(write_lat)/len(write_lat) if write_lat else 0):.4f}s")
    io_lines.append("")
    for kind, vals in sorted(io_by.items()):
        st = summarize(vals)
        io_lines.append(
            f"## {kind}\n\n- count={st['count']} avg={st['avg']:.4f} p95={st['p95']:.4f} max={st['max']:.4f}\n"
        )
    _write(reports_dir / "io_statistics.md", "\n".join(io_lines) + "\n")

    # --- Queue starvation ---
    idle_tot: dict[str, float] = defaultdict(float)
    for c in cases:
        for reason, sec in (c.get("idle") or {}).items():
            idle_tot[reason] += float(sec)
    worker_wall = sum(walls) or 1.0
    starve_lines = [
        "# Queue Starvation Analysis",
        "",
        f"Worker wall (sum case walls): {worker_wall:.2f}s",
        "",
    ]
    for reason in (
        "queue_empty",
        "facility_lock",
        "semaphore",
        "rate_control",
        "browser",
    ):
        sec = idle_tot.get(reason, 0.0)
        starve_lines.append(
            f"- Idle because {reason}: {sec:.2f}s ({_pct(sec, worker_wall):.1f}%)"
        )
    _write(reports_dir / "queue_starvation.md", "\n".join(starve_lines) + "\n")

    # --- Download efficiency ---
    eff_rows = []
    for c in cases:
        pdf_n = int(c.get("pdf_count") or 0)
        nbytes = int(c.get("bytes_total") or 0)
        dl = float((c.get("phases") or {}).get("pdf_wave") or 0)
        if dl <= 0:
            dl = float(c.get("wall_sec") or 0)
        mb = nbytes / (1024 * 1024)
        eff_rows.append(
            {
                "Case ID": c.get("case_id"),
                "Facility": c.get("facility_id"),
                "PDF Count": pdf_n,
                "Bytes": nbytes,
                "Download Time": round(dl, 4),
                "MB/sec": round(mb / dl, 4) if dl > 0 else 0,
                "PDF/sec": round(pdf_n / dl, 4) if dl > 0 else 0,
                "Seconds/PDF": round(dl / pdf_n, 4) if pdf_n else 0,
            }
        )
    _write_csv(
        reports_dir / "download_efficiency.csv",
        [
            "Case ID",
            "Facility",
            "PDF Count",
            "Bytes",
            "Download Time",
            "MB/sec",
            "PDF/sec",
            "Seconds/PDF",
        ],
        eff_rows,
    )
    avg_mbps = summarize([float(r["MB/sec"]) for r in eff_rows]) if eff_rows else summarize([])
    _write(
        reports_dir / "download_efficiency.md",
        "\n".join(
            [
                "# Download Efficiency",
                "",
                f"Cases: {len(eff_rows)}",
                f"Avg MB/sec: {avg_mbps['avg']:.4f}",
                f"P95 Seconds/PDF: {summarize([float(r['Seconds/PDF']) for r in eff_rows])['p95']:.4f}",
                "",
                "See download_efficiency.csv for per-case rows.",
                "",
            ]
        ),
    )

    # --- Time budget ---
    budget_keys = {
        "Browser": ("open_s1", "open_nav", "s1_verify", "s1_navigation", "s1_dom_ready"),
        "Discovery": ("discovery",),
        "HTTP": (),  # filled from http sum / n_cases
        "Semaphore Wait": (),
        "Disk IO": (),
        "Manifest": ("manifest",),
        "Retry Wait": (),
        "Delay Ladder": ("inter_case_delay",),
        "Facility Switch": ("facility_acquire",),
        "FSM": ("fsm",),
        "PDF Wave": ("pdf_wave",),
        "Claim": ("claim",),
        "Release": ("release",),
        "Build Plan": ("build_plan",),
    }
    avg_wall = (sum(walls) / len(walls)) if walls else 0.0
    budget: dict[str, float] = {}
    for label, names in budget_keys.items():
        if not names:
            continue
        vals = []
        for c in cases:
            vals.append(sum(float((c.get("phases") or {}).get(n, 0)) for n in names))
        budget[label] = (sum(vals) / len(vals)) if vals else 0.0
    # HTTP avg per case
    http_by_case: dict[str, float] = defaultdict(float)
    for h in http:
        key = f"{h.get('facility_id')}:{h.get('case_id')}"
        http_by_case[key] += float(h.get("elapsed_sec") or 0)
    budget["HTTP"] = (sum(http_by_case.values()) / n_cases) if n_cases else 0.0
    # Semaphore from idle
    sem_vals = [float((c.get("idle") or {}).get("semaphore", 0)) for c in cases]
    budget["Semaphore Wait"] = (sum(sem_vals) / n_cases) if n_cases else 0.0
    io_case = [sum(float(v) for v in (c.get("io_sec") or {}).values()) for c in cases]
    budget["Disk IO"] = (sum(io_case) / n_cases) if n_cases else 0.0
    retry_idle = [
        float((c.get("idle") or {}).get("rate_control", 0))
        for c in cases
        if int(c.get("retry_n") or 0) > 0 or (c.get("error_type"))
    ]
    # Retry wait approx from retry lifecycle wait_sec
    retry_waits = [float(r.get("wait_sec") or 0) for r in retries if r.get("outcome") not in ("success",)]
    budget["Retry Wait"] = (sum(retry_waits) / n_cases) if n_cases and retry_waits else 0.0
    attributed = sum(budget.values())
    other = max(0.0, avg_wall - attributed)
    budget["Other / Unattributed"] = other
    # Normalize display to 100% of avg_wall
    base = avg_wall if avg_wall > 0 else attributed or 1.0
    budget_lines = [
        "# Time Budget",
        "",
        f"Average Case wall: {avg_wall:.3f}s",
        "",
        "```text",
        "Average Case",
        "100%",
    ]
    for k, v in sorted(budget.items(), key=lambda kv: -kv[1]):
        budget_lines.append(f"├── {k:20s} {_pct(v, base):5.1f}%")
    budget_lines.extend(["```", ""])
    _write(reports_dir / "time_budget.md", "\n".join(budget_lines))

    # --- Critical path ---
    cp_walls, cp_paths, cp_par, cp_idle = [], [], [], []
    for c in cases:
        ev = c.get("events") or []
        # Prefer events with start_rel; else synthesize from phases
        if not ev:
            ev = [
                {
                    "event_id": name,
                    "name": name,
                    "duration": dur,
                    "start_rel": 0,
                    "end_rel": dur,
                    "parent_id": "",
                    "depends_on": [],
                }
                for name, dur in (c.get("phases") or {}).items()
            ]
        cp = critical_path_sec(ev)
        wall = float(c.get("wall_sec") or cp["wall_sec"])
        cp_walls.append(wall)
        cp_paths.append(cp["critical_path_sec"])
        cp_par.append(cp["parallelizable_sec"])
        cp_idle.append(cp["idle_sec"])
    sw = summarize(cp_walls)
    sp = summarize(cp_paths)
    spar = summarize(cp_par)
    si = summarize(cp_idle)
    theo_gain = 0.0
    if sw["avg"] > 0:
        theo_gain = min(spar["avg"], max(0.0, sw["avg"] - sp["avg"])) / sw["avg"] * 100
    _write(
        reports_dir / "critical_path_report.md",
        "\n".join(
            [
                "# Critical Path Report",
                "",
                "## Average Case",
                "",
                f"- Wall Time: {sw['avg']:.3f} sec",
                f"- Critical Path: {sp['avg']:.3f} sec",
                f"- Parallelizable: {spar['avg']:.3f} sec",
                f"- Idle: {si['avg']:.3f} sec",
                f"- Theoretical gain from more PDF parallelism: ~{theo_gain:.1f}% of wall",
                "",
                f"P95 wall={sw['p95']:.3f} critical={sp['p95']:.3f}",
                "",
            ]
        ),
    )

    # --- Occupancy ---
    dl_occ = []
    br_occ = []
    sem_occ = []
    wr_occ = []
    ascii_lines = ["# Occupancy Report", "", "Sample ASCII (first case with pdf_wave):", "", "```text"]
    sample_drawn = False
    for c in cases:
        wall = float(c.get("wall_sec") or 0) or 1.0
        phases = c.get("phases") or {}
        dl = float(phases.get("pdf_wave") or 0)
        br = float(phases.get("open_s1") or 0)
        sem = float((c.get("idle") or {}).get("semaphore") or 0)
        sleep = float(phases.get("inter_case_delay") or 0) + float(
            (c.get("idle") or {}).get("rate_control") or 0
        )
        dl_occ.append(_pct(dl, wall))
        br_occ.append(_pct(br, wall))
        # semaphore occupancy approx peak/limit unknown → use wait share inverse
        peak = int(c.get("sem_peak") or 0)
        sem_occ.append(min(100.0, peak / 8.0 * 100) if peak else 0.0)
        wr_occ.append(max(0.0, 100.0 - _pct(sleep, wall)))
        if not sample_drawn and dl > 0:
            # crude bar: download vs idle chunks
            for label, frac in (("Download", dl / wall), ("Idle", max(0, 1 - dl / wall - br / wall)), ("Browser", br / wall)):
                n = max(1, int(frac * 40))
                ascii_lines.append(f"{'█' * n} {label}")
            sample_drawn = True
    ascii_lines.append("```")
    ascii_lines.extend(
        [
            "",
            f"- Download Occupancy % (avg): {summarize(dl_occ)['avg']:.1f}",
            f"- Browser Occupancy % (avg): {summarize(br_occ)['avg']:.1f}",
            f"- Semaphore Occupancy % (avg peak/8): {summarize(sem_occ)['avg']:.1f}",
            f"- Worker Occupancy % (avg): {summarize(wr_occ)['avg']:.1f}",
            "",
        ]
    )
    _write(reports_dir / "occupancy_report.md", "\n".join(ascii_lines))

    # --- Duplicates ---
    dup_lines = ["# Duplicate Work Report", ""]
    url_counts: dict[str, int] = defaultdict(int)
    fp_counts: dict[str, int] = defaultdict(int)
    app_by_case: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for h in http:
        url_counts[str(h.get("url") or "")] += 1
        if h.get("fingerprint"):
            fp_counts[f"{h.get('endpoint')}|{h.get('fingerprint')}"] += 1
        is_app = h.get("is_app_api")
        if is_app is None:
            is_app = is_app_api_url(str(h.get("url") or ""))
        if is_app:
            ck = f"{h.get('facility_id')}:{h.get('case_id')}"
            ep = str(h.get("endpoint") or normalize_endpoint(str(h.get("url") or "")))
            app_by_case[ck][ep] += 1
    dup_urls = [(u, n) for u, n in url_counts.items() if u and n > 1]
    dup_urls.sort(key=lambda x: -x[1])
    wasted = 0.0
    for h in http:
        u = str(h.get("url") or "")
        if url_counts[u] > 1:
            wasted += float(h.get("elapsed_sec") or 0) * (1 - 1 / url_counts[u])
    dup_lines.append(f"- Duplicate URL occurrences: {sum(n-1 for _, n in dup_urls)}")
    dup_lines.append(f"- Estimated wasted HTTP seconds: {wasted:.2f}")
    dup_lines.append(f"- % of HTTP time wasted (upper bound): {_pct(wasted, sum(float(h.get('elapsed_sec') or 0) for h in http) or 1):.1f}%")
    dup_lines.append("")
    for u, n in dup_urls[:20]:
        dup_lines.append(f"- x{n}: {u[:120]}")
    # App API duplicates within the same case (Zero Duplicate target)
    app_dup_eps: dict[str, int] = defaultdict(int)
    app_dup_cases = 0
    for _ck, eps in app_by_case.items():
        hit = False
        for ep, n in eps.items():
            if n > 1:
                app_dup_eps[ep] += n - 1
                hit = True
        if hit:
            app_dup_cases += 1
    dup_lines.append("")
    dup_lines.append("## Zero Duplicate — App APIs (same case)")
    dup_lines.append(f"- Cases with repeated app API endpoint: {app_dup_cases}")
    for ep, n in sorted(app_dup_eps.items(), key=lambda x: -x[1])[:15]:
        dup_lines.append(f"- x{n} extra calls: `{ep}`")
    # PDF duplicates by filename+case
    pdf_key: dict[str, int] = defaultdict(int)
    for p in pdfs:
        pdf_key[f"{p.get('facility_id')}:{p.get('case_id')}:{p.get('filename')}"] += 1
    pdf_dups = [(k, n) for k, n in pdf_key.items() if n > 1]
    dup_lines.append("")
    dup_lines.append(f"- Duplicate PDF downloads (same case+filename): {len(pdf_dups)}")
    _write(reports_dir / "duplicate_work_report.md", "\n".join(dup_lines) + "\n")

    # --- Discovery breakdown ---
    disc_sub = {
        "disc_chart_reuse_page": [],
        "disc_chart_notes": [],
        "disc_edocs_ajax": [],
        "discovery": [],
    }
    for c in cases:
        ph = c.get("phases") or {}
        for k in disc_sub:
            if k in ph:
                disc_sub[k].append(float(ph[k] or 0))
    disc_break_lines = [
        "# Discovery Breakdown",
        "",
        "| Sub-phase | Count | Avg s | Median | P90 |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, xs in disc_sub.items():
        st = summarize(xs)
        disc_break_lines.append(
            f"| `{k}` | {st['count']} | {st['avg']:.3f} | "
            f"{st['median']:.3f} | {st['p90']:.3f} |"
        )
    disc_break_lines.extend(
        [
            "",
            "What to fight: largest avg among `disc_*` children of Discovery.",
            "Zero Duplicate: prefer `disc_chart_reuse_page` over a second `patientChartNote` nav.",
            "",
        ]
    )
    _write(reports_dir / "discovery_breakdown.md", "\n".join(disc_break_lines) + "\n")

    # --- Open+S1 breakdown ---
    s1_sub = {
        "open_s1": [],
        "s1_navigation": [],
        "s1_verify": [],
        "s1_dom_ready": [],
    }
    for c in cases:
        ph = c.get("phases") or {}
        for k in s1_sub:
            if k in ph:
                s1_sub[k].append(float(ph[k] or 0))
    # HTTP during open_s1
    s1_http = [h for h in http if (h.get("phase_name") or "") == "open_s1"]
    s1_ep_time: dict[str, float] = defaultdict(float)
    s1_ep_n: dict[str, int] = defaultdict(int)
    for h in s1_http:
        ep = str(h.get("endpoint") or "")
        s1_ep_time[ep] += float(h.get("elapsed_sec") or 0)
        s1_ep_n[ep] += 1
    s1_break_lines = [
        "# Open+S1 Breakdown",
        "",
        "| Sub-phase | Count | Avg s | Median | P90 |",
        "|---|---:|---:|---:|---:|",
    ]
    for k, xs in s1_sub.items():
        st = summarize(xs)
        s1_break_lines.append(
            f"| `{k}` | {st['count']} | {st['avg']:.3f} | "
            f"{st['median']:.3f} | {st['p90']:.3f} |"
        )
    s1_break_lines.extend(
        [
            "",
            f"HTTP rows in phase open_s1: {len(s1_http)}",
            "",
            "## Top HTTP during open_s1 (by total time)",
            "",
        ]
    )
    for ep, tot in sorted(s1_ep_time.items(), key=lambda kv: -kv[1])[:15]:
        s1_break_lines.append(f"- {s1_ep_n[ep]} calls · {tot:.2f}s · `{ep}`")
    s1_break_lines.extend(
        [
            "",
            "What to fight: `s1_navigation` wall + static/script noise during goto.",
            "CaseID verify should stay near-zero after URL is ready.",
            "",
        ]
    )
    _write(reports_dir / "open_s1_breakdown.md", "\n".join(s1_break_lines) + "\n")

    # Useful Seconds KPI snapshot (if present)
    speed_path = reports_dir / "speed_control_state.json"
    useful_md = ["# Useful Seconds KPI", ""]
    if speed_path.is_file():
        try:
            speed_payload = json.loads(speed_path.read_text(encoding="utf-8"))
            st = speed_payload.get("state") or {}
            useful_md.extend(
                [
                    f"- discovery_parallel_ok: {st.get('discovery_parallel_ok')}",
                    f"- open_s1_light_nav_ok: {st.get('open_s1_light_nav_ok')}",
                    f"- useful_seconds_saved_discovery: {st.get('useful_seconds_saved_discovery')}",
                    f"- useful_seconds_saved_open_s1: {st.get('useful_seconds_saved_open_s1')}",
                    f"- telemetry_abort_enabled: {st.get('telemetry_abort_enabled')}",
                    f"- adaptive_batch_enabled: {st.get('adaptive_batch_enabled')}",
                    f"- rollbacks: {len(st.get('rollbacks') or [])}",
                    "",
                    speed_payload.get("adaptive_batch_note") or "",
                    "",
                ]
            )
        except Exception as exc:
            useful_md.append(f"Failed to read speed_control_state: {exc}")
    else:
        useful_md.append("No speed_control_state.json yet.")
    _write(reports_dir / "useful_seconds.md", "\n".join(useful_md) + "\n")

    # --- Cache candidates ---
    cache_lines = ["# Cache Candidates (suggestions only — not implemented)", ""]
    ep_fp: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for h in http:
        ep = str(h.get("endpoint") or "")
        fp = str(h.get("fingerprint") or "")
        if ep and fp:
            ep_fp[ep][fp] += 1
    cands = []
    for ep, fps in ep_fp.items():
        total = sum(fps.values())
        if total < 5:
            continue
        top_fp, top_n = max(fps.items(), key=lambda kv: kv[1])
        cands.append((ep, total, top_n, _pct(top_n, total)))
    cands.sort(key=lambda x: -x[3])
    for ep, total, top_n, hit in cands[:30]:
        cache_lines.append(
            f"- `{ep}`: called {total} times; identical fingerprint {top_n} times ({hit:.1f}% hit-rate upper bound)"
        )
    if not cands:
        cache_lines.append("- (insufficient fingerprint data)")
    _write(reports_dir / "cache_candidates.md", "\n".join(cache_lines) + "\n")

    # --- Phase root cause (PDF wave) ---
    pdf_phase_http = sum(float(r.get("HTTP Wall Time") or 0) for r in corr_rows if r["Phase"] in ("pdf_wave", "pdf_job"))
    pdf_wall = phase_stats.get("pdf_wave", {}).get("avg", 0) * n_cases
    sem_total = sum(sem_vals)
    io_total = sum(io_case)
    rca_parts = {
        "semaphore_wait": sem_total,
        "server_latency_http": pdf_phase_http,
        "disk_io": io_total,
        "other": max(0.0, pdf_wall - sem_total - pdf_phase_http - io_total),
    }
    rca_sum = sum(rca_parts.values()) or 1.0
    rca_lines = [
        "# Phase Root Cause Attribution",
        "",
        f"PDF wave total measured ≈ {pdf_wall:.2f}s across sample",
        "",
    ]
    share = _pct(pdf_wall, sum(walls) or 1)
    rca_lines.append(f"PDF Download ≈ {share:.1f}% of total wall because:")
    for k, v in sorted(rca_parts.items(), key=lambda kv: -kv[1]):
        rca_lines.append(f"- {k}: {_pct(v, rca_sum):.1f}% of PDF attribution pool")
    _write(reports_dir / "phase_root_cause.md", "\n".join(rca_lines) + "\n")

    # --- Session health ---
    sh_lines = ["# Session Health Timeline", "", f"Samples: {len(session)}", ""]
    if session:
        last = session[-1]
        first = session[0]
        sh_lines.append(f"- Session age (last): {last.get('session_age_sec')}s")
        sh_lines.append(f"- 403 cumulative: {last.get('http_403')}")
        sh_lines.append(f"- 429 cumulative: {last.get('http_429')}")
        sh_lines.append(f"- Timeouts cumulative: {last.get('http_timeout')}")
        sh_lines.append(f"- Login renewals: {last.get('login_renewals')}")
        sh_lines.append(f"- Storage refreshes: {last.get('storage_refreshes')}")
        # trend: 403 rate
        if len(session) >= 2:
            d403 = int(last.get("http_403") or 0) - int(first.get("http_403") or 0)
            dage = float(last.get("session_age_sec") or 1) - float(first.get("session_age_sec") or 0)
            sh_lines.append(f"- 403 growth over window: {d403} in {dage:.0f}s")
    else:
        sh_lines.append("- (no session_health.jsonl samples yet)")
    _write(reports_dir / "session_health.md", "\n".join(sh_lines) + "\n")

    # --- Facility performance ---
    fac: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for c in cases:
        fid = str(c.get("facility_id") or "")
        fac[fid]["wall"].append(float(c.get("wall_sec") or 0))
        fac[fid]["pdfs"].append(float(c.get("pdf_count") or 0))
        fac[fid]["discovery"].append(float((c.get("phases") or {}).get("discovery") or 0))
        fac[fid]["download"].append(float((c.get("phases") or {}).get("pdf_wave") or 0))
    fac_rows = []
    for fid, m in fac.items():
        fac_rows.append(
            {
                "facility_id": fid,
                "cases": len(m["wall"]),
                "avg_case_sec": round(summarize(m["wall"])["avg"], 3),
                "avg_pdfs": round(summarize(m["pdfs"])["avg"], 2),
                "avg_discovery_sec": round(summarize(m["discovery"])["avg"], 3),
                "avg_download_sec": round(summarize(m["download"])["avg"], 3),
            }
        )
    fac_rows.sort(key=lambda r: -r["avg_case_sec"])
    _write_csv(
        reports_dir / "facility_performance.csv",
        [
            "facility_id",
            "cases",
            "avg_case_sec",
            "avg_pdfs",
            "avg_discovery_sec",
            "avg_download_sec",
        ],
        fac_rows,
    )

    # --- Top 50 slowest ---
    slow = sorted(cases, key=lambda c: -float(c.get("wall_sec") or 0))[:50]
    slow_rows = []
    for c in slow:
        ph = c.get("phases") or {}
        slow_rows.append(
            {
                "facility_id": c.get("facility_id"),
                "case_id": c.get("case_id"),
                "wall_sec": round(float(c.get("wall_sec") or 0), 3),
                "discovery": round(float(ph.get("discovery") or 0), 3),
                "download": round(float(ph.get("pdf_wave") or 0), 3),
                "manifest": round(float(ph.get("manifest") or 0), 3),
                "pdf_count": c.get("pdf_count"),
                "error_type": c.get("error_type"),
                "retry_n": c.get("retry_n"),
            }
        )
    _write_csv(
        reports_dir / "top_slowest_cases.csv",
        [
            "facility_id",
            "case_id",
            "wall_sec",
            "discovery",
            "download",
            "manifest",
            "pdf_count",
            "error_type",
            "retry_n",
        ],
        slow_rows,
    )

    # --- Retry analysis ---
    by_reason: dict[str, list[float]] = defaultdict(list)
    for r in retries:
        by_reason[str(r.get("failure_reason") or "unknown")].append(
            float(r.get("wait_sec") or 0)
        )
    retry_rows = []
    for reason, waits in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        successes = sum(
            1
            for r in retries
            if r.get("failure_reason") == reason and r.get("outcome") == "success"
        )
        retry_rows.append(
            {
                "reason": reason,
                "count": len(waits),
                "avg_wait_sec": round(summarize(waits)["avg"], 3),
                "recovery_rate": round(_pct(successes, len(waits)), 2),
            }
        )
    _write_csv(
        reports_dir / "retry_analysis.csv",
        ["reason", "count", "avg_wait_sec", "recovery_rate"],
        retry_rows,
    )

    # --- PDF utilization ---
    pdf_util = {
        "n_pdf_rows": len(pdfs),
        "avg_elapsed": summarize([float(p.get("elapsed_sec") or 0) for p in pdfs]),
        "avg_size": summarize([float(p.get("size") or 0) for p in pdfs]),
        "semaphore_peak_avg": summarize([float(c.get("sem_peak") or 0) for c in cases]),
        "configured_limit": 8,
    }
    _write(reports_dir / "pdf_utilization.json", json.dumps(pdf_util, indent=2))

    # --- Worker utilization ---
    wu = {
        "download_occ_avg": summarize(dl_occ)["avg"],
        "browser_occ_avg": summarize(br_occ)["avg"],
        "worker_occ_avg": summarize(wr_occ)["avg"],
        "idle_breakdown": {k: round(v, 3) for k, v in idle_tot.items()},
        "resource_samples": len(resources),
    }
    _write(reports_dir / "worker_utilization.json", json.dumps(wu, indent=2))

    # --- Bottleneck ranking ---
    total_phase_time = sum(sum(v) for v in phase_vals.values()) or 1.0
    rank = sorted(
        ((name, sum(vals)) for name, vals in phase_vals.items()),
        key=lambda kv: -kv[1],
    )
    rank_lines = ["# Bottleneck Ranking", "", "| Phase | % Total | Seconds |", "|---|---:|---:|"]
    for name, sec in rank:
        rank_lines.append(f"| {name} | {_pct(sec, total_phase_time):.1f}% | {sec:.1f} |")
    # add delay / idle
    for reason, sec in sorted(idle_tot.items(), key=lambda kv: -kv[1]):
        rank_lines.append(
            f"| idle:{reason} | {_pct(sec, total_phase_time + sum(idle_tot.values())):.1f}% | {sec:.1f} |"
        )
    _write(reports_dir / "bottleneck_ranking.md", "\n".join(rank_lines) + "\n")

    # --- Queue latency stats ---
    qlat = [float(r.get("queue_latency_sec") or 0) for r in queue]
    ql = summarize(qlat)
    _write(
        reports_dir / "queue_latency_stats.md",
        f"# Queue Latency\n\navg={ql['avg']:.2f} median={ql['median']:.2f} p95={ql['p95']:.2f} max={ql['max']:.2f}\n",
    )

    # --- Resource statistics ---
    res_lines = ["# Resource Statistics", "", f"Samples: {len(resources)}", ""]
    if resources:
        ram = [float(r.get("ram_mb") or 0) for r in resources]
        cpu = [float(r.get("cpu_percent") or 0) for r in resources if float(r.get("cpu_percent") or -1) >= 0]
        res_lines.append(f"- RAM MB: avg={summarize(ram)['avg']:.1f} max={summarize(ram)['max']:.1f}")
        if cpu:
            res_lines.append(f"- CPU %: avg={summarize(cpu)['avg']:.1f} max={summarize(cpu)['max']:.1f}")
        if len(ram) >= 2 and ram[-1] > ram[0] * 1.5 + 100:
            res_lines.append("- Trend: possible memory growth (last >> first)")
        else:
            res_lines.append("- Trend: no strong leak signal in sample")
    _write(reports_dir / "resource_statistics.md", "\n".join(res_lines) + "\n")

    # --- request correlation schema ---
    _write(
        reports_dir / "request_correlation.json",
        json.dumps(
            {
                "fields": [
                    "facility_id",
                    "case_id",
                    "patient_id",
                    "worker_id",
                    "session_id",
                    "phase_id",
                    "phase_name",
                    "request_id",
                ],
                "sample": http[:3],
                "n_http": n_http,
                "n_cases": n_cases,
            },
            indent=2,
        ),
    )

    # --- Facility switch summary ---
    sw_elapsed = [float(s.get("elapsed_sec") or 0) for s in switches]
    switch_md = [
        "# Facility Switch History Summary",
        "",
        f"- Switches logged: {len(switches)}",
        f"- Total switch time: {sum(sw_elapsed):.2f}s",
        f"- Average: {summarize(sw_elapsed)['avg']:.3f}s",
        f"- Worst: {summarize(sw_elapsed)['max']:.3f}s",
        f"- % of worker wall: {_pct(sum(sw_elapsed), worker_wall):.2f}%",
        "",
        "See facility_switch_history.csv",
        "",
    ]
    _write(reports_dir / "facility_switch_summary.md", "\n".join(switch_md))

    # --- Simulator + recommendations (gated) ---
    insuff = "Insufficient sample size."
    scenarios = _simulate(cases, phase_stats, idle_tot, sw, sp, spar, worker_wall)
    if not gate:
        _write(reports_dir / "optimization_recommendations.md", insuff + "\n")
        _write(reports_dir / "optimization_matrix.md", insuff + "\n")
        _write(
            reports_dir / "optimization_simulator.md",
            insuff
            + f"\n\nNeed ≥{MIN_CASES_FOR_RECS} cases and ≥{MIN_HTTP_FOR_RECS} HTTP rows. "
            f"Have cases={n_cases} http={n_http}.\n\n"
            + "### Preview simulations (not actionable)\n\n"
            + scenarios["markdown"],
        )
    else:
        _write(reports_dir / "optimization_simulator.md", scenarios["markdown"])
        _write(reports_dir / "optimization_recommendations.md", scenarios["recommendations"])
        _write(reports_dir / "optimization_matrix.md", scenarios["matrix"])

    # --- Master forensics MD ---
    top_ep = ep_rows[0]["Endpoint"] if ep_rows else "n/a"
    top_phase = rank[0][0] if rank else "n/a"
    theo_cph = (3600 / sw["avg"]) if sw["avg"] > 0 else 0.0
    disc_avg = phase_stats.get("discovery", {}).get("avg", 0.0)
    fac_avg = phase_stats.get("facility_acquire", {}).get("avg", 0.0)
    pdf_avg = phase_stats.get("pdf_wave", {}).get("avg", 0.0)
    open_avg = phase_stats.get("open_s1", {}).get("avg", 0.0)
    delay_avg = phase_stats.get("inter_case_delay", {}).get("avg", 0.0)
    sem_share = _pct(idle_tot.get("semaphore", 0.0), worker_wall)
    fac_share = _pct(idle_tot.get("facility_lock", 0.0), worker_wall)
    disc_share = _pct(disc_avg, avg_wall) if avg_wall else 0.0
    perf = [
        "# Performance Forensics",
        "",
        f"- Cases sampled: {n_cases}",
        f"- HTTP requests: {n_http}",
        f"- Gate open: {gate}",
        f"- Avg wall: {sw['avg']:.3f}s → ~{theo_cph:.1f} cph theoretical",
        f"- Top phase: {top_phase}",
        f"- Top HTTP endpoint by time: {top_ep}",
        "",
        "See forensics_general_report.md for the single consolidated report.",
        "",
    ]
    _write(reports_dir / "performance_forensics.md", "\n".join(perf))

    # --- Single general report (Arabic + numbers) ---
    pdf_share = _pct(pdf_avg, avg_wall) if avg_wall else 0.0
    open_share = _pct(open_avg, avg_wall) if avg_wall else 0.0
    top_eps = ep_rows[:5]
    gen = [
        "# تقرير Forensics العام — Case Drain",
        "",
        f"**العينة:** {n_cases} Case · {n_http:,} طلب HTTP",
        f"**متوسط Case:** {sw['avg']:.2f}s (~{theo_cph:.0f} cph نظري من الـ wall)",
        f"**Critical path:** {sp['avg']:.2f}s · **Parallelizable PDF:** {spar['avg']:.2f}s",
        f"**بوابة التوصيات:** {'مفتوحة' if gate else f'مغلقة (يلزم ≥{MIN_CASES_FOR_RECS} Case؛ الحالي {n_cases})'}",
        "",
        "## الخلاصة في جملة",
        "",
    ]
    s1_nav_avg = summarize(s1_sub["s1_navigation"])["avg"]
    s1_ver_avg = summarize(s1_sub["s1_verify"])["avg"]
    s1_dom_avg = summarize(s1_sub["s1_dom_ready"])["avg"]
    # الخلاصة من آخر 40 Case ناجحة حتى لا تُخفي التحسينات عيّنة Discovery القديمة
    recent = [c for c in cases if c.get("ok")][-40:]
    if recent:
        r_walls = [float(c.get("wall_sec") or 0) for c in recent]
        r_wall = (sum(r_walls) / len(r_walls)) if r_walls else avg_wall
        def _ravg(name: str) -> float:
            xs = [
                float((c.get("phases") or {}).get(name) or 0)
                for c in recent
                if (c.get("phases") or {}).get(name) is not None
            ]
            return (sum(xs) / len(xs)) if xs else 0.0

        r_open, r_disc, r_pdf, r_fac = (
            _ravg("open_s1"),
            _ravg("discovery"),
            _ravg("pdf_wave"),
            _ravg("facility_acquire"),
        )
        ranked = [
            ("Open+S1", r_open),
            ("Discovery", r_disc),
            ("PDF", r_pdf),
            ("Facility", r_fac),
        ]
        ranked.sort(key=lambda kv: -kv[1])
        top_r, top_r_sec = ranked[0]
        gen.append(
            f"آخر {len(recent)} Case: الأكبر **{top_r}** "
            f"({top_r_sec:.1f}s / wall {r_wall:.1f}s = {_pct(top_r_sec, r_wall):.0f}%)."
        )
        if top_r == "Open+S1":
            gen.append(
                "البطء الأساسي الآن في **Open+S1** (تنقّل الصفحة) وليس Discovery ولا PDF."
            )
        elif top_r == "Discovery":
            gen.append(
                "البطء الأساسي في **Discovery** وليس تحميل PDF ولا الـ semaphore."
            )
        elif top_r == "Facility":
            gen.append(
                "جزء كبير من الوقت في **facility acquire / قفل العيادة**؛ PDF ليس المسيطر."
            )
        else:
            gen.append(
                f"أكبر مرحلة مقاسة: **{top_phase}** · أكبر endpoint: `{top_ep}`."
            )
    elif open_share >= disc_share and open_share >= pdf_share and open_share >= fac_share:
        gen.append(
            "البطء الأساسي في **Open+S1** (تنقّل الصفحة) وليس Discovery ولا PDF."
        )
    elif disc_share >= pdf_share and disc_share >= fac_share:
        gen.append(
            "البطء الأساسي في **Discovery** وليس تحميل PDF ولا الـ semaphore."
        )
    elif fac_share >= pdf_share:
        gen.append(
            "جزء كبير من الوقت في **facility acquire / قفل العيادة**؛ PDF ليس المسيطر."
        )
    else:
        gen.append(
            f"أكبر مرحلة مقاسة: **{top_phase}** · أكبر endpoint: `{top_ep}`."
        )
    if sem_share < 1.0:
        gen.append(
            "Semaphore wait ≈ 0% → زيادة PDF concurrency فوق الحد الحالي غالبًا لن تعطي مكسب wall كبير."
        )
    disc_reuse = summarize(disc_sub["disc_chart_reuse_page"])["avg"]
    disc_chart = summarize(disc_sub["disc_chart_notes"])["avg"]
    disc_edoc = summarize(disc_sub["disc_edocs_ajax"])["avg"]
    gen.extend(
        [
            "",
            "## أين يروح الوقت؟ (متوسط Case)",
            "",
            "| المكوّن | متوسط ثانية | % من Wall |",
            "|---|---:|---:|",
            f"| Browser / Open+S1 | {open_avg:.2f} | {open_share:.1f}% |",
            f"| ↳ s1_navigation | {s1_nav_avg:.2f} | {_pct(s1_nav_avg, avg_wall) if avg_wall else 0:.1f}% |",
            f"| ↳ s1_verify | {s1_ver_avg:.2f} | {_pct(s1_ver_avg, avg_wall) if avg_wall else 0:.1f}% |",
            f"| ↳ s1_dom_ready | {s1_dom_avg:.2f} | {_pct(s1_dom_avg, avg_wall) if avg_wall else 0:.1f}% |",
            f"| Discovery | {budget.get('Discovery', disc_avg):.2f} | {_pct(budget.get('Discovery', disc_avg), avg_wall) if avg_wall else 0:.1f}% |",
            f"| ↳ chart reuse S1 page | {disc_reuse:.2f} | {_pct(disc_reuse, avg_wall) if avg_wall else 0:.1f}% |",
            f"| ↳ chart notes HTTP | {disc_chart:.2f} | {_pct(disc_chart, avg_wall) if avg_wall else 0:.1f}% |",
            f"| ↳ edocs ajax | {disc_edoc:.2f} | {_pct(disc_edoc, avg_wall) if avg_wall else 0:.1f}% |",
            f"| Facility acquire / lock | {budget.get('Facility Switch', fac_avg):.2f} | {fac_share:.1f}% |",
            f"| PDF wave | {budget.get('PDF Wave', pdf_avg):.2f} | {_pct(budget.get('PDF Wave', pdf_avg), avg_wall) if avg_wall else 0:.1f}% |",
            f"| Delay ladder | {budget.get('Delay Ladder', 0):.2f} | {_pct(budget.get('Delay Ladder', 0), avg_wall) if avg_wall else 0:.1f}% |",
            f"| Semaphore idle | — | {sem_share:.1f}% |",
            "",
            f"App-API duplicate cases: **{app_dup_cases}** · see `open_s1_breakdown.md` · `discovery_breakdown.md` · `useful_seconds.md`.",
            "",
            "## أهم HTTP Endpoints",
            "",
            "| Endpoint | Calls | Avg s | Success % |",
            "|---|---:|---:|---:|",
        ]
    )
    for r in top_eps:
        gen.append(
            f"| `{r['Endpoint']}` | {r['Calls']} | {r['Avg']} | {r['Success %']} |"
        )
    gen.extend(
        [
            "",
            "## خمول الـ Worker",
            "",
            f"- queue_empty: {_pct(idle_tot.get('queue_empty', 0), worker_wall):.1f}%",
            f"- facility_lock: {fac_share:.1f}%",
            f"- browser: {_pct(idle_tot.get('browser', 0), worker_wall):.1f}%",
            f"- rate_control: {_pct(idle_tot.get('rate_control', 0), worker_wall):.1f}%",
            f"- semaphore: {sem_share:.1f}%",
            "",
            "## Time Budget (مرجع)",
            "",
            "```text",
            "Average Case",
        ]
    )
    for k, v in sorted(budget.items(), key=lambda kv: -kv[1])[:8]:
        gen.append(f"├── {k:20s} {_pct(v, base):5.1f}%")
    gen.extend(
        [
            "```",
            "",
            "> بند HTTP قد >100% لأنه مجموع طلبات متوازية وليس exclusive wall.",
            "",
            "## تفاصيل إضافية",
            "",
            "`time_budget.md` · `critical_path_report.md` · `bottleneck_ranking.md` · "
            "`http_endpoint_statistics.csv` · `http_phase_correlation.csv` · "
            "`queue_starvation.md` · `occupancy_report.md` · `duplicate_work_report.md` · "
            "`open_s1_breakdown.md` · `discovery_breakdown.md` · `useful_seconds.md` · "
            "`adaptive_batch_proof.md` · `eta_stretch_decision.md` · `optimization_simulator.md`",
            "",
        ]
    )
    if gate:
        gen.append(
            "التوصيات: `optimization_recommendations.md` + `optimization_matrix.md`."
        )
    else:
        gen.append(
            f"Insufficient sample size للتوصيات الرسمية "
            f"(cases={n_cases}/{MIN_CASES_FOR_RECS}, http={n_http}/{MIN_HTTP_FOR_RECS})."
        )
        gen.append("")
        gen.append("معاينة المحاكاة (غير ملزمة) في `optimization_simulator.md`.")
    gen.append("")
    _write(reports_dir / "forensics_general_report.md", "\n".join(gen))

    return {
        "n_cases": n_cases,
        "n_http": n_http,
        "gate": gate,
        "avg_wall": sw["avg"],
        "top_phase": top_phase,
        "top_endpoint": top_ep,
        "general_report": str(reports_dir / "forensics_general_report.md"),
    }


def _simulate(
    cases: list[dict],
    phase_stats: dict,
    idle_tot: dict[str, float],
    sw: dict,
    sp: dict,
    spar: dict,
    worker_wall: float,
) -> dict[str, str]:
    """What-if simulations from measured distributions only."""
    avg_wall = sw.get("avg") or 0.0
    cpath = sp.get("avg") or 0.0
    parallelizable = spar.get("avg") or 0.0
    delay_avg = phase_stats.get("inter_case_delay", {}).get("avg", 0.0)
    manifest_avg = phase_stats.get("manifest", {}).get("avg", 0.0)
    fac_avg = phase_stats.get("facility_acquire", {}).get("avg", 0.0)
    discovery_avg = phase_stats.get("discovery", {}).get("avg", 0.0)
    sem_avg = (idle_tot.get("semaphore", 0.0) / len(cases)) if cases else 0.0

    def gain_from_saving(sec: float) -> float:
        if avg_wall <= 0:
            return 0.0
        new_wall = max(cpath * 0.5, avg_wall - sec)  # cannot beat half critical path absurdly
        return max(0.0, (avg_wall - new_wall) / avg_wall * 100.0)

    # More concurrency: can only reclaim semaphore wait + part of parallelizable residual
    conc_save = min(sem_avg + parallelizable * 0.25, avg_wall * 0.3)
    # Removing manifest
    man_save = manifest_avg
    # Facility batching: save most of acquire if sticky already — residual hops only
    fac_save = fac_avg * 0.5
    # Parallel discovery: only if serial before pdf — save min(discovery, overlap opportunity)
    disc_save = min(discovery_avg * 0.3, discovery_avg)
    # Delay ladder to 0 if healthy — measured delay only
    delay_save = delay_avg * 0.5  # conservative: cannot assume always healthy

    scenarios = [
        {
            "name": "PDF concurrency 8→12",
            "gain": gain_from_saving(conc_save),
            "cost": "M",
            "risk": "Med (WAF)",
            "confidence": "Med" if sem_avg > 0.5 else "Low",
            "evidence": f"sem_wait_avg={sem_avg:.2f}s parallelizable={parallelizable:.2f}s",
            "location": "pdf_throttle / rate knobs",
        },
        {
            "name": "Remove/amortize manifest flush",
            "gain": gain_from_saving(man_save),
            "cost": "S",
            "risk": "Low",
            "confidence": "High",
            "evidence": f"manifest_avg={manifest_avg:.3f}s",
            "location": "case_download.append_manifest_rows",
        },
        {
            "name": "Facility batching / fewer switches",
            "gain": gain_from_saving(fac_save),
            "cost": "M",
            "risk": "Low",
            "confidence": "Med",
            "evidence": f"facility_acquire_avg={fac_avg:.3f}s",
            "location": "run_case_download_worker facility_exhaust",
        },
        {
            "name": "Parallel discovery",
            "gain": gain_from_saving(disc_save),
            "cost": "M",
            "risk": "Med",
            "confidence": "Low",
            "evidence": f"discovery_avg={discovery_avg:.3f}s",
            "location": "case_download discovery block",
        },
        {
            "name": "Reduce delay ladder when healthy",
            "gain": gain_from_saving(delay_save),
            "cost": "S",
            "risk": "Med (throttle)",
            "confidence": "Med",
            "evidence": f"inter_case_delay_avg={delay_avg:.3f}s",
            "location": "case_rate_control DELAY_LADDER",
        },
    ]
    scenarios.sort(key=lambda s: -s["gain"])

    md = [
        "# Optimization Simulator (advisory only)",
        "",
        f"Baseline avg wall={avg_wall:.3f}s critical_path={cpath:.3f}s",
        "",
        "| Scenario | Expected Δ throughput | Evidence |",
        "|---|---:|---|",
    ]
    for s in scenarios:
        md.append(f"| {s['name']} | +{s['gain']:.1f}% | {s['evidence']} |")
    md.append("")
    md.append("Gains are upper-bound estimates from measured timers; they do not change production.")
    md.append("")

    recs = ["# Optimization Recommendations", ""]
    for s in scenarios:
        if s["gain"] < 1.0:
            continue
        recs.extend(
            [
                f"## {s['name']}",
                "",
                f"- Problem: measured time in related phases",
                f"- Measured evidence: {s['evidence']}",
                f"- Affected %: ~{s['gain']:.1f}% of case wall (simulated)",
                f"- Estimated improvement: +{s['gain']:.1f}% throughput",
                f"- Confidence: {s['confidence']}",
                f"- Risk: {s['risk']}",
                f"- Required code location: {s['location']}",
                "",
            ]
        )
    negligible = [s for s in scenarios if s["gain"] < 1.0]
    if negligible:
        recs.append("## Do NOT attempt (negligible measured gain)")
        recs.append("")
        for s in negligible:
            recs.append(f"- {s['name']}: +{s['gain']:.1f}% ({s['evidence']})")
        recs.append("")

    matrix = [
        "# Optimization Matrix",
        "",
        "| Optimization | Expected Gain | Engineering Cost | Risk | Confidence | Priority |",
        "|---|---:|---|---|---|---|",
    ]
    for i, s in enumerate(scenarios, 1):
        matrix.append(
            f"| {s['name']} | +{s['gain']:.1f}% | {s['cost']} | {s['risk']} | {s['confidence']} | {i} |"
        )
    matrix.append("")

    return {
        "markdown": "\n".join(md),
        "recommendations": "\n".join(recs),
        "matrix": "\n".join(matrix),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case",
    )
    args = ap.parse_args()
    reports = Path(args.out_dir) / "reports"
    result = analyze(reports)
    print(json.dumps(result, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

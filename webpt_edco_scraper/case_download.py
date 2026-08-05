"""Case-scoped chart/eDoc download with open/verify CaseID (S1).

Case pipeline only — never uses include_all_cases=True or patient-first folders.
PDF downloads within a case are parallelized (bounded by pdf_throttle semaphore).
"""

from __future__ import annotations

import asyncio
import csv
import time
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from playwright.async_api import BrowserContext

T = TypeVar("T")

try:
    from snowflake_pull.case_forensics import CaseTimeline, io_span
except Exception:  # pragma: no cover
    CaseTimeline = None  # type: ignore
    io_span = None  # type: ignore

from case_paths import (
    MANIFEST_FIELDNAMES,
    ensure_case_layout,
    manifest_path,
    write_case_meta,
)
from case_artifact_contract import (
    file_sha256,
    save_raw_json_with_meta,
    update_audit,
    write_case_sources,
)
from case_raw_capture import ensure_raw_layout, light_raw_snapshot_from_page_html
from chart_notes_api import (
    assert_opened_case_id,
    extract_case_id_from_url,
    fetch_patient_chart_notes,
    parse_chart_notes_html,
    patient_chart_note_url,
)
from chart_notes_download import chart_note_filename, download_chart_note_pdf
from config import WebPTConfig
from edoc_api import list_patient_edocs
from edoc_download import download_edoc_pdf, sanitize_filename
from logging_config import get_logger

log = get_logger("case_download")


class CaseMismatchError(RuntimeError):
    """Opened CaseID does not match scheduled CaseID."""


class CaseOpenFailedError(RuntimeError):
    """Could not open or resolve CaseID on chart page."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


async def bounded_gather(
    factories: Sequence[Callable[[], Awaitable[T]]],
) -> list[T]:
    """Run awaitable factories concurrently; PDF slot limiting is inside downloaders."""
    if not factories:
        return []
    return list(await asyncio.gather(*(factory() for factory in factories)))


def note_subdir_for_type(note_type: str) -> str:
    t = (note_type or "").lower()
    if "daily" in t:
        return "daily_notes"
    if "eval" in t or "examination" in t or "re-exam" in t:
        return "evaluations"
    if "progress" in t:
        return "progress_notes"
    return "other"


def _enrich_manifest_row_hashes(base_dir: Path, row: dict[str, str]) -> dict[str, str]:
    """Add size + sha256 for on-disk PDF paths (best-effort)."""
    out = dict(row)
    rel = (out.get("path") or "").strip()
    if not rel:
        out.setdefault("size", out.get("size") or "")
        out.setdefault("sha256", out.get("sha256") or "")
        return out
    pth = Path(rel)
    if not pth.is_file():
        pth = Path(base_dir) / rel
    if pth.is_file():
        try:
            out["size"] = str(int(pth.stat().st_size))
            out["sha256"] = file_sha256(pth)
        except OSError:
            out.setdefault("size", "")
            out.setdefault("sha256", "")
    else:
        out.setdefault("size", out.get("size") or "")
        out.setdefault("sha256", out.get("sha256") or "")
    return out


def append_manifest_rows(
    base_dir: Path,
    facility_id: str | int,
    case_id: str | int,
    rows: list[dict[str, str]],
) -> Path:
    path = manifest_path(base_dir, facility_id, case_id)

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDNAMES, extrasaction="ignore")
            if write_header:
                w.writeheader()
            for row in rows:
                enriched = _enrich_manifest_row_hashes(base_dir, row)
                w.writerow({k: enriched.get(k, "") for k in MANIFEST_FIELDNAMES})

    if io_span is not None:
        with io_span("manifest_write"):
            _write()
    else:
        _write()
    return path


async def _s1_resolve_opened_case_id(
    page,
    *,
    patient_id: int,
    scheduled_case_id: int,
    requested_url: str,
) -> str:
    """Extract opened CaseID from URL / page params (no extra waits)."""
    opened = extract_case_id_from_url(page.url or "")
    if not opened:
        try:
            opened = await page.evaluate(
                """() => {
                    const params = new URLSearchParams(window.location.search);
                    return params.get('CaseID') || params.get('caseid') || '';
                }"""
            )
            opened = str(opened or "").strip()
        except Exception:
            opened = ""
    if not opened:
        opened = str(scheduled_case_id)
        requested = extract_case_id_from_url(requested_url)
        if requested and extract_case_id_from_url(page.url or "") == "":
            from urllib.parse import parse_qs, urlparse

            qs = parse_qs(urlparse(page.url or "").query)
            pid = (qs.get("ID") or qs.get("id") or [""])[0]
            if str(pid) != str(patient_id):
                raise CaseOpenFailedError(
                    f"CaseOpenFailed: chart URL patient mismatch page={page.url}"
                )
            opened = requested
    return opened


async def _install_s1_light_routes(page) -> Any:
    """Abort static/script noise during S1; keep chart/graphql/PDF APIs."""
    try:
        from snowflake_pull.case_speed_control import s1_should_abort_request
    except Exception:  # pragma: no cover
        return None

    async def _handler(route: Any) -> None:
        try:
            req = route.request
            url = req.url or ""
            rtype = getattr(req, "resource_type", "") or ""
            if s1_should_abort_request(url, rtype):
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            try:
                await route.continue_()
            except Exception:
                pass

    try:
        await page.route("**/*", _handler)
        return _handler
    except Exception:
        return None


async def _uninstall_s1_light_routes(page, handler: Any) -> None:
    if handler is None:
        return
    try:
        await page.unroute("**/*", handler)
    except Exception:
        try:
            await page.unroute("**/*")
        except Exception:
            pass


async def open_and_verify_case(
    page,
    *,
    patient_id: int,
    scheduled_case_id: int,
    timeout_ms: int = 90000,
    timeline: Any = None,
    speed_controller: Any = None,
    s1_light_nav: bool | None = None,
) -> str:
    """Navigate to chart URL with CaseID and assert opened CaseID matches (S1).

    When light nav is on (default via SpeedController): abort static/script noise
    during navigation, prefer early CaseID verify after commit, then ensure
    domcontentloaded for HTML reuse — never networkidle.
    """
    url = patient_chart_note_url(patient_id, scheduled_case_id)
    use_light = (
        bool(s1_light_nav)
        if s1_light_nav is not None
        else (
            bool(speed_controller.should_use_s1_light_nav())
            if speed_controller is not None
            and hasattr(speed_controller, "should_use_s1_light_nav")
            else True
        )
    )
    browser: dict[str, Any] = {
        "navigation_sec": 0.0,
        "dom_loaded_sec": 0.0,
        "network_idle_sec": "",  # not in path — do not add waits
        "page_ready_sec": 0.0,
        "click_latency_sec": "",
        "case_verification_sec": 0.0,
        "s1_light_nav": use_light,
        "s1_nav_mode": "",
    }
    tl = timeline
    parent_id = ""
    if tl is not None:
        for eid, ev in list(getattr(tl, "_open", {}).items()):
            if getattr(ev, "name", "") == "open_s1":
                parent_id = eid
                break

    route_handler = None
    t_open0 = time.perf_counter()
    nav_eid = ""
    if tl is not None:
        nav_eid = tl.begin(
            "s1_navigation",
            parent_id=parent_id,
            meta={"light": use_light},
        )
    try:
        if use_light:
            route_handler = await _install_s1_light_routes(page)
        t_nav = time.perf_counter()
        if use_light:
            # Commit first — CaseID usually present in URL without waiting on assets.
            await page.goto(url, wait_until="commit", timeout=timeout_ms)
            browser["s1_nav_mode"] = "commit"
            try:
                await page.wait_for_function(
                    """(expected) => {
                        const p = new URLSearchParams(window.location.search);
                        const cid = p.get('CaseID') || p.get('caseid') || '';
                        return String(cid) === String(expected);
                    }""",
                    arg=str(scheduled_case_id),
                    timeout=min(20000, timeout_ms),
                )
            except Exception:
                # Fall back to DOM ready (still with asset abort if light).
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=timeout_ms
                )
                browser["s1_nav_mode"] = "commit_then_domcontentloaded"
        else:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            browser["s1_nav_mode"] = "domcontentloaded"
        browser["navigation_sec"] = round(time.perf_counter() - t_nav, 6)
        browser["dom_loaded_sec"] = browser["navigation_sec"]
    finally:
        if tl is not None and nav_eid:
            tl.end(nav_eid, mode=browser.get("s1_nav_mode") or "")

    ver_eid = ""
    if tl is not None:
        ver_eid = tl.begin("s1_verify", parent_id=parent_id)
    t_ver = time.perf_counter()
    try:
        opened = await _s1_resolve_opened_case_id(
            page,
            patient_id=patient_id,
            scheduled_case_id=scheduled_case_id,
            requested_url=url,
        )
        try:
            assert_opened_case_id(opened, scheduled_case_id)
        except ValueError as exc:
            msg = str(exc)
            if msg.startswith("CaseMismatch"):
                raise CaseMismatchError(msg) from exc
            raise CaseOpenFailedError(msg) from exc
    finally:
        browser["case_verification_sec"] = round(time.perf_counter() - t_ver, 6)
        if tl is not None and ver_eid:
            tl.end(ver_eid)

    # Ensure HTML document is available for chart-note reuse (no networkidle).
    dom_eid = ""
    if tl is not None:
        dom_eid = tl.begin("s1_dom_ready", parent_id=parent_id)
    try:
        try:
            await page.wait_for_load_state(
                "domcontentloaded", timeout=min(15000, timeout_ms)
            )
        except Exception:
            pass
    finally:
        if tl is not None and dom_eid:
            tl.end(dom_eid)
        await _uninstall_s1_light_routes(page, route_handler)

    browser["page_ready_sec"] = round(
        browser["navigation_sec"] + browser["case_verification_sec"], 6
    )
    open_s1_sec = time.perf_counter() - t_open0
    if tl is not None:
        timeline.browser = browser
        timeline.add_idle("browser", float(browser["navigation_sec"] or 0))
    if speed_controller is not None:
        try:
            speed_controller.note_open_s1_sec(
                open_s1_sec,
                opt_name="s1_light_nav" if use_light else "s1_full_nav",
            )
        except Exception:
            pass
    return opened


async def _discover_chart_notes_reuse_page(
    page,
    *,
    case_id: int,
) -> list[Any]:
    """Parse chart notes from already-open S1 page (Zero Duplicate nav)."""
    try:
        html = await page.content()
    except Exception:
        return []
    return parse_chart_notes_html(html, case_id=case_id)


async def download_case_unit(
    context: BrowserContext,
    *,
    facility_id: str | int,
    case_id: int,
    patient_id: int,
    dos: str,
    patient_name: str = "",
    base_dir: Path,
    config: WebPTConfig,
    session,
    page=None,
    skip_existing: bool = True,
    skip_edocs: bool = False,
    skip_chart_notes: bool = False,
    timeline: Any = None,
    discovery_parallel: bool = False,
    speed_controller: Any = None,
) -> dict[str, Any]:
    """Download chart notes + eDocs for ONE case into cases/{facility}/{case}/.

    Hard rules:
    - include_all_cases=False
    - S1 open/verify before download when page is provided
    - manifest rows always carry facility_id + case_id
    Observer-only: optional timeline records phase/PDF timings.
    """
    tl = timeline
    if tl is None and CaseTimeline is not None:
        tl = CaseTimeline()

    ensure_case_layout(base_dir, facility_id, case_id)
    opened_case_id = str(case_id)
    open_eid = ""
    if page is not None:
        if tl is not None:
            open_eid = tl.begin("open_s1")
        try:
            opened_case_id = await open_and_verify_case(
                page,
                patient_id=patient_id,
                scheduled_case_id=case_id,
                timeout_ms=int(config.chart_timeout_sec * 1000),
                timeline=tl,
                speed_controller=speed_controller,
            )
            # Light Raw Snapshot during S1 (no extra WebPT requests).
            try:
                html = await page.content()
                light_raw_snapshot_from_page_html(
                    base_dir,
                    facility_id=facility_id,
                    case_id=case_id,
                    patient_id=patient_id,
                    html=html,
                    page_url=getattr(page, "url", "") or "",
                )
            except Exception:
                pass
        finally:
            if tl is not None and open_eid:
                tl.end(open_eid)

    write_case_meta(
        base_dir,
        facility_id=facility_id,
        case_id=case_id,
        meta={
            "patient_ids": [str(patient_id)],
            "patient_name": patient_name,
            "schedule_dos": [dos[:10]],
            "opened_case_id": opened_case_id,
            "updated_at": _utc(),
        },
    )

    # Download-only phase: discover ALL sources, then one parallel PDF wave.
    # No OCR / extract / merge / REC here.
    manifest_rows: list[dict[str, str]] = []
    chart_results: list[dict[str, Any]] = []
    edoc_results: list[dict[str, Any]] = []
    downloaded_at = _utc()
    pdf_jobs: list[Callable[[], Awaitable[tuple[str, Any, dict[str, Any], dict[str, str]]]]] = []

    scoped_notes: list[Any] = []
    docs: list[dict[str, Any]] = []
    disc_meta: dict[str, Any] = {
        "mode": "sequential",
        "chart_via": "",
        "parallel_ok_flag": bool(discovery_parallel),
    }
    disc_eid = tl.begin("discovery") if tl is not None else ""
    t_disc0 = time.perf_counter()
    try:

        async def _fetch_chart() -> list[Any]:
            eid = ""
            if tl is not None and disc_eid:
                eid = tl.begin(
                    "disc_chart_notes",
                    parent_id=disc_eid,
                    meta={"prefer_http": True},
                )
            try:
                notes = await fetch_patient_chart_notes(
                    context,
                    patient_id=patient_id,
                    case_id=case_id,
                    page=page,
                    config=config,
                    timeout_ms=int(config.chart_timeout_sec * 1000),
                    # HTTP first after S1 — avoid re-navigating the shared page.
                    prefer_http=True,
                )
                return [
                    n
                    for n in notes
                    if not n.case_id or str(n.case_id).strip() == str(case_id)
                ]
            finally:
                if tl is not None and eid:
                    tl.end(eid)

        async def _fetch_edocs() -> list[dict[str, Any]]:
            eid = ""
            if tl is not None and disc_eid:
                eid = tl.begin("disc_edocs_ajax", parent_id=disc_eid)
            try:
                return await list_patient_edocs(
                    context,
                    patient_id=patient_id,
                    case_id=case_id,
                    config=config,
                    session=session,
                    include_all_cases=False,
                )
            finally:
                if tl is not None and eid:
                    tl.end(eid)

        # Zero Duplicate: reuse S1 page HTML before any second chart fetch.
        if not skip_chart_notes and page is not None:
            reuse_eid = ""
            if tl is not None and disc_eid:
                reuse_eid = tl.begin(
                    "disc_chart_reuse_page", parent_id=disc_eid
                )
            try:
                reused = await _discover_chart_notes_reuse_page(
                    page, case_id=case_id
                )
                scoped_notes = [
                    n
                    for n in reused
                    if not n.case_id or str(n.case_id).strip() == str(case_id)
                ]
                if scoped_notes:
                    disc_meta["chart_via"] = "reuse_s1_page"
            finally:
                if tl is not None and reuse_eid:
                    tl.end(reuse_eid)

        need_chart_http = (not skip_chart_notes) and (not scoped_notes)
        need_edocs = not skip_edocs
        use_parallel = bool(discovery_parallel) and need_chart_http and need_edocs

        if use_parallel:
            disc_meta["mode"] = "parallel"
            try:
                chart_part, docs = await asyncio.gather(
                    _fetch_chart(), _fetch_edocs()
                )
                scoped_notes = chart_part
                disc_meta["chart_via"] = disc_meta.get("chart_via") or "http_parallel"
            except Exception as exc:
                disc_meta["mode"] = "sequential_fallback"
                disc_meta["parallel_error"] = str(exc)[:300]
                if speed_controller is not None:
                    try:
                        speed_controller.mark_parallel_probe_failed()
                    except Exception:
                        pass
                if need_chart_http:
                    scoped_notes = await _fetch_chart()
                    disc_meta["chart_via"] = "http_after_parallel_fail"
                if need_edocs:
                    docs = await _fetch_edocs()
        else:
            if need_chart_http:
                scoped_notes = await _fetch_chart()
                disc_meta["chart_via"] = disc_meta.get("chart_via") or "http"
            if need_edocs:
                docs = await _fetch_edocs()
    finally:
        disc_sec = time.perf_counter() - t_disc0
        if tl is not None and disc_eid:
            tl.end(disc_eid, **disc_meta)
        if speed_controller is not None:
            try:
                speed_controller.note_discovery_sec(
                    disc_sec,
                    opt_name=(
                        "parallel_discovery"
                        if disc_meta.get("mode") == "parallel"
                        else "discovery"
                    ),
                )
            except Exception:
                pass

    plan_eid = tl.begin("build_plan", depends_on=[disc_eid] if disc_eid else None) if tl is not None else ""
    # Mutable: set before gather so jobs see pdf_wave parent at runtime
    wave_parent_ref: list[str] = [""]
    try:
        # Build unified job list (chart notes + edocs)
        for note in scoped_notes:
            def _mk_chart(n: Any = note) -> Callable[[], Awaitable[Any]]:
                async def _run() -> tuple[str, Any, dict[str, Any], dict[str, str]]:
                    t0 = time.perf_counter()
                    job_eid = ""
                    if tl is not None and wave_parent_ref[0]:
                        job_eid = tl.begin(
                            "pdf_job",
                            parent_id=wave_parent_ref[0],
                            meta={"type": note_subdir_for_type(n.note_type)},
                        )
                    sub = note_subdir_for_type(n.note_type)
                    dest = ensure_case_layout(base_dir, facility_id, case_id) / sub
                    result = await download_chart_note_pdf(
                        context,
                        note=n,
                        patient_id=patient_id,
                        case_id=case_id,
                        dest_dir=dest,
                        config=config,
                        facility_id=str(facility_id),
                        skip_existing=skip_existing,
                    )
                    status = "ok"
                    if result.get("error"):
                        status = "error"
                    elif result.get("skipped"):
                        status = "skipped"
                    elif result.get("downloaded"):
                        status = "downloaded"
                    artifact_id = str(result.get("cnsid") or result.get("note_id") or "")
                    rel = ""
                    size = 0
                    if result.get("path"):
                        try:
                            pth = Path(result["path"])
                            rel = str(pth.relative_to(base_dir))
                            if pth.is_file():
                                size = int(pth.stat().st_size)
                        except ValueError:
                            rel = str(result["path"])
                    elapsed = time.perf_counter() - t0
                    if tl is not None:
                        tl.pdf_rows.append(
                            {
                                "filename": result.get("filename")
                                or chart_note_filename(n),
                                "pdf_type": note_subdir_for_type(n.note_type),
                                "size": size,
                                "elapsed_sec": round(elapsed, 6),
                                "retries": result.get("retries") or 0,
                                "status": status,
                                "http_status": result.get("http_status") or "",
                            }
                        )
                        tl.bytes_total += size
                        tl.pdf_count += 1
                        if job_eid:
                            tl.end(job_eid)
                    row = {
                        "facility_id": str(facility_id),
                        "case_id": str(case_id),
                        "patient_id": str(patient_id),
                        "dos": (n.note_date or dos)[:10],
                        "doc_source": "chart_note",
                        "artifact_id": artifact_id,
                        "original_filename": result.get("filename")
                        or chart_note_filename(n),
                        "path": rel,
                        "source_url": n.print_url or "",
                        "downloaded_at": downloaded_at,
                        "status": status,
                        "size": str(size) if size else "",
                        "sha256": "",
                    }
                    return "chart", n, result, row

                return _run

            pdf_jobs.append(_mk_chart())

        edocs_dir = ensure_case_layout(base_dir, facility_id, case_id) / "edocs"
        for doc in docs:
            def _mk_edoc(d: dict[str, Any] = doc) -> Callable[[], Awaitable[Any]]:
                async def _run() -> tuple[str, Any, dict[str, Any], dict[str, str]]:
                    t0 = time.perf_counter()
                    job_eid = ""
                    if tl is not None and wave_parent_ref[0]:
                        job_eid = tl.begin(
                            "pdf_job",
                            parent_id=wave_parent_ref[0],
                            meta={"type": "edoc"},
                        )
                    ext_id = d.get("ExtDocID") or d.get("extDocID") or ""
                    fname = sanitize_filename(
                        str(
                            d.get("UserDefName")
                            or d.get("FileName")
                            or d.get("filename")
                            or f"edoc_{ext_id}.pdf"
                        ),
                        f"edoc_{ext_id}.pdf",
                    )
                    result = await download_edoc_pdf(
                        context,
                        doc=d,
                        patient_id=patient_id,
                        dest_dir=edocs_dir,
                        config=config,
                        skip_existing=skip_existing,
                    )
                    status = "ok"
                    if result.get("error"):
                        status = "error"
                    elif result.get("skipped"):
                        status = "skipped"
                    elif result.get("downloaded"):
                        status = "downloaded"
                    rel = ""
                    size = 0
                    if result.get("path"):
                        try:
                            pth = Path(result["path"])
                            rel = str(pth.relative_to(base_dir))
                            if pth.is_file():
                                size = int(pth.stat().st_size)
                        except ValueError:
                            rel = str(result["path"])
                    elapsed = time.perf_counter() - t0
                    if tl is not None:
                        tl.pdf_rows.append(
                            {
                                "filename": result.get("filename") or fname,
                                "pdf_type": "edoc",
                                "size": size,
                                "elapsed_sec": round(elapsed, 6),
                                "retries": result.get("retries") or 0,
                                "status": status,
                                "http_status": result.get("http_status") or "",
                            }
                        )
                        tl.bytes_total += size
                        tl.pdf_count += 1
                        if job_eid:
                            tl.end(job_eid)
                    row = {
                        "facility_id": str(facility_id),
                        "case_id": str(case_id),
                        "patient_id": str(patient_id),
                        "dos": dos[:10],
                        "doc_source": "edoc",
                        "artifact_id": str(ext_id),
                        "original_filename": result.get("filename") or fname,
                        "path": rel,
                        "source_url": str(d.get("URI") or d.get("URL") or ""),
                        "downloaded_at": downloaded_at,
                        "status": status,
                        "size": str(size) if size else "",
                        "sha256": "",
                    }
                    return "edoc", d, result, row

                return _run

            pdf_jobs.append(_mk_edoc())
    finally:
        if tl is not None and plan_eid:
            tl.end(plan_eid)

    # Persist edoc list JSON if discovery already fetched it (no extra request).
    if docs:
        try:
            ensure_raw_layout(base_dir, facility_id, case_id)
            save_raw_json_with_meta(
                ensure_raw_layout(base_dir, facility_id, case_id) / "edoc_list.json",
                docs,
                facility_id=facility_id,
                case_id=case_id,
                endpoint="/edoc/edoc/getdocumentspercase",
            )
        except Exception:
            pass

    # One parallel PDF wave (bounded by pdf_throttle semaphore inside downloaders)
    wave_eid = ""
    if tl is not None:
        wave_eid = tl.begin(
            "pdf_wave", depends_on=[plan_eid] if plan_eid else None
        )
        wave_parent_ref[0] = wave_eid
    try:
        wave = await bounded_gather(pdf_jobs)
    finally:
        if tl is not None and wave_eid:
            tl.end(wave_eid)
    for kind, _src, result, row in wave:
        if kind == "chart":
            chart_results.append(result)
        else:
            edoc_results.append(result)
        manifest_rows.append(row)

    if manifest_rows:
        man_eid = tl.begin("manifest", depends_on=[wave_eid] if wave_eid else None) if tl is not None else ""
        try:
            append_manifest_rows(base_dir, facility_id, case_id, manifest_rows)
        finally:
            if tl is not None and man_eid:
                tl.end(man_eid)

    chart_ok = sum(
        1 for r in chart_results if r.get("downloaded") or r.get("skipped")
    )
    edoc_ok = sum(1 for r in edoc_results if r.get("downloaded") or r.get("skipped"))
    empty = chart_ok + edoc_ok == 0 and not skip_chart_notes
    case_dir = ensure_case_layout(base_dir, facility_id, case_id)
    try:
        update_audit(
            case_dir,
            flag="download_complete",
            value=not empty,
            error="DownloadEmpty" if empty else "",
        )
        write_case_sources(case_dir)
    except Exception:
        pass
    out: dict[str, Any] = {
        "facility_id": str(facility_id),
        "case_id": str(case_id),
        "patient_id": str(patient_id),
        "dos": dos[:10],
        "opened_case_id": opened_case_id,
        "chart_notes": len(chart_results),
        "chart_ok": chart_ok,
        "edocs": len(edoc_results),
        "edoc_ok": edoc_ok,
        "manifest_rows": len(manifest_rows),
        "empty": empty,
        "error_type": "DownloadEmpty" if empty else "",
        "timeline": tl,
    }
    return out


def validate_manifest_case_ids(
    base_dir: Path,
    facility_id: str | int,
    case_id: str | int,
) -> list[str]:
    """S2: every manifest row case_id must equal scheduled case_id."""
    path = manifest_path(base_dir, facility_id, case_id)
    if not path.is_file():
        return ["manifest_missing"]
    errors: list[str] = []
    expected = str(case_id)
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            got = (row.get("case_id") or "").strip()
            if got != expected:
                errors.append(f"line {i}: case_id={got!r} expected={expected!r}")
            fid = (row.get("facility_id") or "").strip()
            if fid and fid != str(facility_id):
                errors.append(f"line {i}: facility_id={fid!r}")
    return errors

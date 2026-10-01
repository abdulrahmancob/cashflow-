"""Eligibility work-queue HTTP API."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field

from cashflow_ops.security import (
    ROLE_COLLECTOR,
    ROLE_FINANCE,
    ROLE_OPS_ADMIN,
    ROLE_POSTING,
    ROLE_SECOND_SUBMISSION,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUB_ADMIN,
    ROLE_SUPER,
    AuthUser,
    get_current_user,
    parse_uuid_list,
    require_roles,
)

router = APIRouter(prefix="/eligibility", tags=["eligibility"])

EDIT_ROLES = (ROLE_POSTING, ROLE_SUPER, ROLE_OPS_ADMIN, ROLE_SUB_ADMIN)
VIEW_ROLES = (ROLE_POSTING, ROLE_SUPER, ROLE_FINANCE, ROLE_COLLECTOR, ROLE_OPS_ADMIN, ROLE_SUB_ADMIN)
PR_ROLES = (
    ROLE_SECOND_SUBMISSION,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUPER,
    ROLE_OPS_ADMIN,
    ROLE_SUB_ADMIN,
)
META_ROLES = tuple(dict.fromkeys((*VIEW_ROLES, *PR_ROLES)))
PR_PAGE_ROLES = tuple(role for role in META_ROLES if role != ROLE_COLLECTOR)
TFL_EDIT_ROLES = (
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_OPS_ADMIN,
    ROLE_SUPER,
    ROLE_SUB_ADMIN,
)


def _require_medicare_medicaid_lead(user: AuthUser, enabled: bool) -> None:
    """Medicare–Medicaid rows are visible only to the Second Submission Lead."""
    if enabled and not user.has_role(ROLE_SECOND_SUBMISSION_LEAD):
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def _require_queue_view(queue: str, user: AuthUser) -> None:
    from cashflow_db.repository.eligibility import _is_pr3_queue

    allowed = PR_PAGE_ROLES if _is_pr3_queue(queue) else VIEW_ROLES
    if not user.is_super_admin and not user.has_role(*allowed):
        raise HTTPException(status_code=403, detail="Insufficient permissions")


def _export_filename(queue: str) -> str:
    if queue == "collection":
        return "collection.xlsx"
    if queue in {"pr3", "patient_responsibility"}:
        return "patient_responsibility.xlsx"
    return "eligibility_sheet.xlsx"


# The API container mounts /tmp as a 256MB tmpfs. An all-months workbook
# overflows that and the worker dies, which nginx reports as 502.
_EXPORT_DISK = Path("/data/logs")
_export_temp_lock = threading.Lock()
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def export_workdir() -> Path:
    """Writable directory for sheet workbooks, outside the 256MB tmpfs."""
    if _EXPORT_DISK.is_dir() and os.access(_EXPORT_DISK, os.W_OK):
        return _EXPORT_DISK
    return Path(tempfile.gettempdir())


def _discard_export_file(path: Path | str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def _openpyxl_temp_on_export_disk() -> Iterator[Path]:
    """Point openpyxl's temp XML at the export disk for this build."""
    from openpyxl.worksheet._writer import ALL_TEMP_FILES

    work = export_workdir()
    work.mkdir(parents=True, exist_ok=True)
    with _export_temp_lock:
        previous = tempfile.tempdir
        tempfile.tempdir = str(work)
        marked = len(ALL_TEMP_FILES)
        try:
            yield work
        except Exception:
            for leftover in ALL_TEMP_FILES[marked:]:
                _discard_export_file(leftover)
            del ALL_TEMP_FILES[marked:]
            raise
        finally:
            tempfile.tempdir = previous


def save_export_workbook(wb: Any, directory: Path) -> Path:
    """Write the workbook to disk. Remove a partial file if save fails."""
    path = directory / f"eligibility-export-{uuid.uuid4().hex}.xlsx"
    try:
        wb.save(path)
    except Exception:
        _discard_export_file(path)
        raise
    return path


_generate_lock = threading.Lock()
_generate_job: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "actor_user_id": None,
}


def _generate_status_payload() -> dict[str, Any]:
    with _generate_lock:
        return {
            "running": bool(_generate_job["running"]),
            "started_at": _generate_job["started_at"],
            "finished_at": _generate_job["finished_at"],
            "last_result": _generate_job["last_result"],
        }


def _run_generate_job() -> None:
    from cashflow_db.services.eligibility_generator import generate_eligibility_work_items

    with _generate_lock:
        actor_user_id = _generate_job.get("actor_user_id")
    try:
        result = generate_eligibility_work_items(from_db=True)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "errors": [str(exc)[:500]], "visit_count": 0}
    if result.get("ok") and actor_user_id:
        try:
            from cashflow_db.repository import connection, portal_activity

            n = (
                result.get("created_approx")
                or result.get("enqueued")
                or result.get("visit_count")
                or 0
            )
            with connection() as conn:
                portal_activity.record_activity(
                    conn,
                    actor_user_id=actor_user_id,
                    action="created",
                    area="eligibility",
                    entity_type="sheet",
                    entity_id="eligibility-sheet",
                    entity_label="Eligibility sheet",
                    summary=f"Generated from recon ({n} visits)",
                    details={
                        "visit_count": result.get("visit_count"),
                        "created_approx": result.get("created_approx"),
                        "enqueued": result.get("enqueued"),
                        "closed": result.get("closed"),
                    },
                )
        except Exception:  # noqa: BLE001
            pass
    with _generate_lock:
        _generate_job["running"] = False
        _generate_job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _generate_job["last_result"] = result


def _activity(conn, user: AuthUser, **kwargs: Any) -> None:
    from cashflow_db.repository import portal_activity

    portal_activity.record_from_diff(conn, actor_user_id=user.user_id, **kwargs)


def _work_area(item: dict[str, Any] | None) -> str:
    status = str((item or {}).get("source_visit_status") or "").strip().lower()
    return "collection" if status == "denied" else "eligibility"


def _log_work_item(
    conn,
    user: AuthUser,
    *,
    action: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    keys: list[str] | None = None,
    fallback: str | None = None,
    extra: dict[str, Any] | None = None,
    force: bool = False,
) -> None:
    from cashflow_db.repository import portal_activity

    item = after or before or {}
    area = _work_area(item)
    label = portal_activity.work_item_label(item, area=area)
    _activity(
        conn,
        user,
        action=action,
        area=area,
        entity_type="work_item",
        entity_id=str(item.get("work_item_id") or ""),
        entity_label=label,
        before=before,
        after=after,
        keys=keys,
        extra=extra,
        fallback=fallback or label,
        force=force,
    )


def _ser(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ser(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "hex"):  # uuid
        return str(obj)
    return obj


class PatchBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    eligibility_status: str | None = None
    reference_number: str | None = None
    notes: str | None = None
    patient_name: str | None = None
    emr_patient_id: str | None = None
    dos: str | None = None
    insurance_name: str | None = None
    facility_name: str | None = None
    paid_amount: float | str | None = None
    check_number: str | None = None
    check_date: str | None = None
    tracker_date: str | None = None
    source_visit_status: str | None = None
    client_payment: float | str | None = None
    insurance_payment: float | str | None = None
    updated_payment: float | str | None = None
    coinsurance_payment: float | str | None = None
    reduction: float | str | None = None
    charged_amount: float | str | None = None
    adjusted: float | str | None = None
    details: str | None = None
    reason_key: str | None = None
    reason_text: str | None = None


class AdjustmentBody(BaseModel):
    column_name: str = Field(min_length=1, max_length=64)
    amount: float
    check_number: str | None = None
    check_date: str | None = None
    note: str | None = None


class AssignBody(BaseModel):
    assigned_to: str | None = None
    reason_key: str | None = None
    reason_text: str | None = None


class BulkAssignBody(BaseModel):
    work_item_ids: list[str] = Field(min_length=1, max_length=500)
    assigned_to: str | None = None


class FilterAssignBody(BaseModel):
    assigned_to: str | None = None
    q: str | None = None
    facility: list[str] = Field(default_factory=list)
    month: list[str] = Field(default_factory=list)
    insurance: list[str] = Field(default_factory=list)
    visit_status: list[str] = Field(default_factory=list)
    filter_assigned_to: list[str] = Field(default_factory=list)
    unassigned: bool = False
    bucket: str = "denied"
    collection_status: list[str] = Field(default_factory=list)
    root_cause: list[str] = Field(default_factory=list)


class TransitionBody(BaseModel):
    eligibility_status: str
    reason_key: str
    reason_text: str | None = None


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class PrFlagBody(BaseModel):
    revflow_patient_id: str = Field(min_length=1, max_length=64)
    dos: str
    kind: str
    second_submission: bool | None = None
    second_insurance: str | None = None
    submission_date: str | None = None
    workload_status: str | None = None
    payment: str | None = None
    provider: str | None = None
    submitter: str | None = None
    claim_number: str | None = None
    paid_date: str | None = None
    check_number: str | None = None
    workload_note: str | None = None


class TflRuleCreate(BaseModel):
    insurance_name: str = Field(min_length=1, max_length=200)
    tfl_days: int = Field(ge=1, le=3650)
    aliases: list[str] | None = None


class TflRulePatch(BaseModel):
    insurance_name: str | None = Field(default=None, min_length=1, max_length=200)
    tfl_days: int | None = Field(default=None, ge=1, le=3650)
    aliases: list[str] | None = None


@router.get("/meta")
def meta(_: AuthUser = Depends(require_roles(*META_ROLES))) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        return _ser(
            {
                "statuses": eligibility.list_statuses(conn),
                "reasons": eligibility.list_reasons(conn),
                "filters": eligibility.filter_options(conn),
            }
        )


@router.get("/kpis")
def kpis(
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        return _ser(eligibility.kpis(conn, facility=facility, month=month))


@router.get("/charts")
def charts(
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        return _ser(eligibility.chart_aggregates(conn, facility=facility, month=month))


@router.get("/items")
def list_items(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    insurance: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    visit_status: list[str] | None = Query(None),
    check_date: list[str] | None = Query(None),
    assigned_to: list[str] | None = Query(None),
    unassigned: bool = False,
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    queue: str = "sheet",
    bucket: str = "denied",
    collection_status: list[str] | None = Query(None),
    root_cause: list[str] | None = Query(None),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    _require_queue_view(queue, user)
    with connection() as conn:
        data = eligibility.list_work_items(
            conn,
            q=q,
            facility=facility,
            month=month,
            insurance=insurance,
            status=status,
            visit_status=visit_status,
            check_date=check_date,
            assigned_to=parse_uuid_list(assigned_to),
            unassigned=unassigned,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
            queue=queue,
            bucket=bucket,
            collection_status=collection_status,
            root_cause=root_cause,
        )
    return _ser(data)


@router.get("/items/export")
def export_items(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    insurance: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    visit_status: list[str] | None = Query(None),
    check_date: list[str] | None = Query(None),
    assigned_to: list[str] | None = Query(None),
    unassigned: bool = False,
    sort_by: str = "dos",
    sort_dir: str = "desc",
    queue: str = "sheet",
    bucket: str = "denied",
    collection_status: list[str] | None = Query(None),
    root_cause: list[str] | None = Query(None),
    user: AuthUser = Depends(get_current_user),
) -> FileResponse:
    from cashflow_db.repository import connection, eligibility

    _require_queue_view(queue, user)
    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    assigned = parse_uuid_list(assigned_to)
    # sort_by/sort_dir stay on the route so existing export URLs still
    # validate. The file is walked by work_item_id so an all-months export
    # does not recount or OFFSET through the whole sheet.
    _ = (sort_by, sort_dir)
    path: Path | None = None
    try:
        with _openpyxl_temp_on_export_disk() as directory:
            wb = Workbook(write_only=True)
            if queue == "collection":
                sheet_name = "Collection"
            elif queue in {"pr3", "patient_responsibility"}:
                sheet_name = "Patient Responsibility"
            else:
                sheet_name = "Eligibility Sheet"
            ws = wb.create_sheet(sheet_name)
            ws.append(eligibility.sheet_export_headers(queue))
            with connection() as conn:
                for row in eligibility.iter_export_work_items(
                    conn,
                    q=q,
                    facility=facility,
                    month=month,
                    insurance=insurance,
                    status=status,
                    visit_status=visit_status,
                    check_date=check_date,
                    assigned_to=assigned,
                    unassigned=unassigned,
                    queue=queue,
                    bucket=bucket,
                    collection_status=collection_status,
                    root_cause=root_cause,
                ):
                    ws.append(eligibility.sheet_export_row(row, queue))
            path = save_export_workbook(wb, directory)
    except HTTPException:
        raise
    except Exception as exc:
        if path is not None:
            _discard_export_file(path)
        raise HTTPException(status_code=500, detail="Eligibility export failed") from exc
    return FileResponse(
        path,
        media_type=_XLSX_MEDIA,
        filename=_export_filename(queue),
        background=BackgroundTask(_discard_export_file, path),
    )


@router.get("/secondary")
def list_secondary(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
    _: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    """Visits with CARC PR-2 on the primary EOB — secondary payment queue."""
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        data = eligibility.list_secondary_queue(
            conn,
            q=q,
            facility=facility,
            month=month,
            paid=paid,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
        )
    return _ser(data)


@router.get("/secondary/export")
def export_secondary(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    _: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, eligibility

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Secondary Payments")
    ws.append(
        [
            "Patient",
            "EMR ID",
            "Account # (PV4)",
            "DOS",
            "Insurance",
            "PR-2 Check #",
            "Second Insurance",
            "Second Submission",
            "Facility",
            "Secondary Paid",
            "Paid Amount",
            "Check #",
            "Check Date",
            "Secondary Payer",
        ]
    )
    page = 1
    page_size = 2000
    with connection() as conn:
        while True:
            data = eligibility.list_secondary_queue(
                conn,
                q=q,
                facility=facility,
                month=month,
                paid=paid,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                page_size=page_size,
            )
            items = data.get("items") or []
            for row in items:
                ws.append(
                    [
                        row.get("patient_name"),
                        row.get("emr_patient_id"),
                        row.get("account_number"),
                        str(row.get("dos") or ""),
                        row.get("primary_payer"),
                        row.get("primary_check_number"),
                        row.get("second_insurance"),
                        "Yes" if row.get("second_submission") else "No",
                        row.get("facility_name"),
                        "Paid" if row.get("secondary_paid") else "Not Paid",
                        row.get("secondary_amount"),
                        row.get("secondary_check_number"),
                        str(row.get("secondary_check_date") or ""),
                        row.get("secondary_payer"),
                    ]
                )
            pages = int(data.get("pages") or 0)
            if not items or page >= pages:
                break
            page += 1
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=secondary_payments.xlsx"
        },
    )


@router.get("/deductible")
def list_deductible(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
    _: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    """Visits with CARC PR-1 on the primary EOB — deductible queue."""
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        data = eligibility.list_deductible_queue(
            conn,
            q=q,
            facility=facility,
            month=month,
            paid=paid,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
        )
    return _ser(data)


@router.get("/deductible/export")
def export_deductible(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    paid: str = "all",
    sort_by: str = "dos",
    sort_dir: str = "desc",
    _: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, eligibility

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Deductible (PR-1)")
    ws.append(
        [
            "Patient",
            "EMR ID",
            "Account # (PV4)",
            "DOS",
            "Insurance",
            "PR-1 Check #",
            "Second Insurance",
            "Second Submission",
            "Facility",
        ]
    )
    page = 1
    page_size = 2000
    with connection() as conn:
        while True:
            data = eligibility.list_deductible_queue(
                conn,
                q=q,
                facility=facility,
                month=month,
                paid=paid,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                page_size=page_size,
            )
            items = data.get("items") or []
            for row in items:
                ws.append(
                    [
                        row.get("patient_name"),
                        row.get("emr_patient_id"),
                        row.get("account_number"),
                        str(row.get("dos") or ""),
                        row.get("primary_payer"),
                        row.get("primary_check_number"),
                        row.get("second_insurance"),
                        "Yes" if row.get("second_submission") else "No",
                        row.get("facility_name"),
                    ]
                )
            pages = int(data.get("pages") or 0)
            if not items or page >= pages:
                break
            page += 1
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=deductible_payments.xlsx"
        },
    )


@router.get("/pr100")
def list_pr100(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 100,
    _: AuthUser = Depends(require_roles(*PR_PAGE_ROLES)),
) -> dict[str, Any]:
    """Visits with CARC PR-100 on an EOB check."""
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        data = eligibility.list_pr100_queue(
            conn,
            q=q,
            facility=facility,
            month=month,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
        )
    return _ser(data)


@router.get("/pr100/export")
def export_pr100(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    sort_by: str = "dos",
    sort_dir: str = "desc",
    _: AuthUser = Depends(require_roles(*PR_PAGE_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, eligibility

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("PR-100")
    ws.append(
        [
            "Patient",
            "EMR ID",
            "Account # (PV4)",
            "DOS",
            "Insurance",
            "PR-100 Check #",
            "Facility",
        ]
    )
    page = 1
    page_size = 2000
    with connection() as conn:
        while True:
            data = eligibility.list_pr100_queue(
                conn,
                q=q,
                facility=facility,
                month=month,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                page_size=page_size,
            )
            items = data.get("items") or []
            for row in items:
                ws.append(
                    [
                        row.get("patient_name"),
                        row.get("emr_patient_id"),
                        row.get("account_number"),
                        str(row.get("dos") or ""),
                        row.get("primary_payer"),
                        row.get("primary_check_number"),
                        row.get("facility_name"),
                    ]
                )
            pages = int(data.get("pages") or 0)
            if not items or page >= pages:
                break
            page += 1
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=pr100.xlsx"},
    )


@router.patch("/pr-flags")
def patch_pr_flag(
    body: PrFlagBody,
    user: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import client, connection, eligibility

    try:
        with connection() as conn:
            before = client.fetchone(
                conn,
                """
                SELECT *
                FROM ops.pr_queue_flag
                WHERE revflow_patient_id = %s AND dos = %s::date AND carc_kind = %s
                """,
                (body.revflow_patient_id, body.dos, body.kind),
            ) or {}
            row = eligibility.upsert_pr_queue_flag(
                conn,
                revflow_patient_id=body.revflow_patient_id,
                dos=body.dos,
                carc_kind=body.kind,
                second_submission=body.second_submission,
                second_insurance=body.second_insurance,
                workload_status=body.workload_status,
                payment=body.payment,
                provider=body.provider,
                claim_number=body.claim_number,
                paid_date=body.paid_date,
                check_number=body.check_number,
                workload_note=body.workload_note,
                actor_id=user.user_id,
                actor_name=user.display_name,
            )
            from cashflow_db.repository import portal_activity

            label = (
                f"{body.kind.upper()} · {body.revflow_patient_id} · {body.dos}"
            )
            _activity(
                conn,
                user,
                action="updated" if before else "created",
                area="second_submission",
                entity_type="pr_flag",
                entity_id=portal_activity.pr_flag_id(
                    body.revflow_patient_id, body.dos, body.kind
                ),
                entity_label=label,
                before=before,
                after=row or {},
                keys=[
                    "second_submission",
                    "second_insurance",
                    "submission_date",
                    "workload_status",
                    "payment",
                    "provider",
                    "submitter",
                    "claim_number",
                    "paid_date",
                    "check_number",
                    "workload_note",
                ],
                fallback=label,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser(row)


@router.get("/pr-meta")
def pr_meta(_: AuthUser = Depends(require_roles(*PR_PAGE_ROLES))) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        return _ser(eligibility.pr_submission_meta(conn))


class WorkloadAddBody(BaseModel):
    visit_id: str | None = None
    visit_ids: list[str] | None = None
    kind: str
    second_insurance: str = Field(min_length=1, max_length=200)


class WorkloadVisitKey(BaseModel):
    revflow_patient_id: str = Field(min_length=1, max_length=64)
    dos: str
    kind: str


class WorkloadRemoveBody(BaseModel):
    items: list[WorkloadVisitKey] = Field(min_length=1)


class WorkloadBulkBody(BaseModel):
    items: list[WorkloadVisitKey] = Field(min_length=1)
    second_insurance: str | None = None
    submission_date: str | None = None
    workload_status: str | None = None
    payment: str | None = None
    provider: str | None = None
    submitter: str | None = None
    claim_number: str | None = None
    paid_date: str | None = None
    check_number: str | None = None
    workload_note: str | None = None


@router.get("/ss-today")
def ss_today(user: AuthUser = Depends(require_roles(*PR_ROLES))) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    with connection() as conn:
        return {
            "completed_today": work_analytics.ss_claims_today_for_user(
                conn, user.user_id
            )
        }


@router.get("/workload")
def list_workload(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    kind: str | None = None,
    status: list[str] | None = Query(None),
    second_insurance: list[str] | None = Query(None),
    tfl: str | None = None,
    has_status: bool | None = None,
    exclude_status: list[str] | None = Query(None),
    exclude_second_insurance: list[str] | None = Query(None),
    submitter: list[str] | None = Query(None),
    submission_date: list[str] | None = Query(None),
    require_moved: bool = True,
    medicare_medicaid: bool = False,
    sort_by: str = "tfl_days_left",
    sort_dir: str = "asc",
    page: int = 1,
    page_size: int = 100,
    user: AuthUser = Depends(require_roles(*PR_PAGE_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    _require_medicare_medicaid_lead(user, medicare_medicaid)
    with connection() as conn:
        return _ser(
            eligibility.list_workload_queue(
                conn,
                q=q,
                facility=facility,
                month=month,
                kind=kind,
                status=status,
                second_insurance=second_insurance,
                tfl=tfl,
                has_status=has_status,
                exclude_status=exclude_status,
                exclude_second_insurance=exclude_second_insurance,
                submitter=submitter,
                submission_date=submission_date,
                require_moved=require_moved,
                medicare_medicaid=medicare_medicaid,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                page_size=page_size,
            )
        )


@router.get("/workload/visits")
def search_workload_visits(
    q: str = "",
    _: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        return _ser({"items": eligibility.search_workload_visits(conn, q=q)})


@router.post("/workload/add")
def add_workload_visit(
    body: WorkloadAddBody,
    user: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    visit_ids = list(body.visit_ids or [])
    if body.visit_id:
        vid = body.visit_id.strip()
        if vid and vid not in visit_ids:
            visit_ids.insert(0, vid)
    try:
        with connection() as conn:
            result = eligibility.add_workload_visits(
                conn,
                visit_ids=visit_ids,
                carc_kind=body.kind,
                second_insurance=body.second_insurance,
                actor_id=user.user_id,
            )
            from cashflow_db.repository import portal_activity

            for item in result.get("items") or []:
                dos = item.get("dos")
                rf = str(item.get("revflow_patient_id") or "")
                kind = str(item.get("carc_kind") or body.kind)
                flag = item.get("flag") or {}
                name = str((flag or {}).get("patient_name") or rf or "Visit")
                label = f"{name} · {dos} · {kind.upper()}"
                _activity(
                    conn,
                    user,
                    action="created",
                    area="second_submission",
                    entity_type="pr_flag",
                    entity_id=portal_activity.pr_flag_id(rf, dos, kind),
                    entity_label=label,
                    after=flag,
                    fallback="Added visit to workload",
                )
            return _ser(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workload/remove")
def remove_workload_visits(
    body: WorkloadRemoveBody,
    user: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    try:
        with connection() as conn:
            result = eligibility.remove_workload_visits(
                conn,
                items=[item.model_dump() for item in body.items],
                actor_id=user.user_id,
            )
            from cashflow_db.repository import portal_activity

            for item in body.items:
                _activity(
                    conn,
                    user,
                    action="updated",
                    area="second_submission",
                    entity_type="pr_flag",
                    entity_id=portal_activity.pr_flag_id(
                        item.revflow_patient_id, item.dos, item.kind
                    ),
                    entity_label=f"{item.kind.upper()} · {item.revflow_patient_id} · {item.dos}",
                    fallback="Removed visit from workload",
                )
            return _ser(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workload/bulk")
def bulk_update_workload(
    body: WorkloadBulkBody,
    user: AuthUser = Depends(require_roles(*PR_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    patch = body.model_dump(exclude={"items"}, exclude_none=True)
    patch.pop("submitter", None)
    patch.pop("submission_date", None)
    try:
        with connection() as conn:
            result = eligibility.bulk_update_workload_visits(
                conn,
                items=[item.model_dump() for item in body.items],
                patch=patch,
                actor_id=user.user_id,
                actor_name=user.display_name,
            )
            from cashflow_db.repository import portal_activity

            _activity(
                conn,
                user,
                action="updated",
                area="second_submission",
                entity_type="pr_flag",
                entity_id="bulk",
                entity_label=f"{len(body.items)} workload visits",
                after=patch,
                fallback="Bulk updated workload visits",
            )
            return _ser(result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/workload/export")
def export_workload(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    kind: str | None = None,
    status: list[str] | None = Query(None),
    second_insurance: list[str] | None = Query(None),
    tfl: str | None = None,
    has_status: bool | None = None,
    exclude_status: list[str] | None = Query(None),
    exclude_second_insurance: list[str] | None = Query(None),
    submitter: list[str] | None = Query(None),
    submission_date: list[str] | None = Query(None),
    require_moved: bool = True,
    medicare_medicaid: bool = False,
    sort_by: str = "tfl_days_left",
    sort_dir: str = "asc",
    user: AuthUser = Depends(require_roles(*PR_PAGE_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, eligibility

    _require_medicare_medicaid_lead(user, medicare_medicaid)

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Workload")
    ws.append(
        [
            "Kind",
            "EMR",
            "Patient",
            "Primary Insurance",
            "Secondary Insurance",
            "Location",
            "DOS",
            "Submission Date",
            "Payment",
            "Note",
            "Provider",
            "Submitter",
            "Status",
            "Claim number",
            "Paid date",
            "Check number",
            "TFL due",
            "TFL days left",
        ]
    )
    page = 1
    page_size = 2000
    with connection() as conn:
        while True:
            data = eligibility.list_workload_queue(
                conn,
                q=q,
                facility=facility,
                month=month,
                kind=kind,
                status=status,
                second_insurance=second_insurance,
                tfl=tfl,
                has_status=has_status,
                exclude_status=exclude_status,
                exclude_second_insurance=exclude_second_insurance,
                submitter=submitter,
                submission_date=submission_date,
                require_moved=require_moved,
                medicare_medicaid=medicare_medicaid,
                sort_by=sort_by,
                sort_dir=sort_dir,
                page=page,
                page_size=page_size,
            )
            items = data.get("items") or []
            for row in items:
                kind_label = "PR-1" if row.get("carc_kind") == "pr1" else "PR-2"
                ws.append(
                    [
                        kind_label,
                        row.get("emr_patient_id"),
                        row.get("patient_name"),
                        row.get("primary_payer"),
                        row.get("second_insurance"),
                        row.get("facility_name"),
                        str(row.get("dos") or ""),
                        str(row.get("submission_date") or ""),
                        row.get("payment"),
                        row.get("workload_note"),
                        row.get("provider"),
                        row.get("submitter"),
                        row.get("workload_status"),
                        row.get("claim_number"),
                        str(row.get("paid_date") or ""),
                        row.get("check_number"),
                        str(row.get("tfl_due") or ""),
                        row.get("tfl_days_left"),
                    ]
                )
            pages = int(data.get("pages") or 0)
            if not items or page >= pages:
                break
            page += 1
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=second_submission_workload.xlsx"},
    )


@router.get("/tfl-rules")
def list_tfl_rules(_: AuthUser = Depends(require_roles(*PR_ROLES))) -> dict[str, Any]:
    from cashflow_db.repository import connection, pr_tfl

    with connection() as conn:
        items = pr_tfl.list_tfl_rules(conn)
    return _ser({"items": items, "count": len(items)})


@router.post("/tfl-rules")
def create_tfl_rule(
    body: TflRuleCreate,
    user: AuthUser = Depends(require_roles(*TFL_EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, pr_tfl

    try:
        with connection() as conn:
            row = pr_tfl.create_tfl_rule(
                conn,
                insurance_name=body.insurance_name,
                tfl_days=body.tfl_days,
                aliases=body.aliases,
                actor_id=user.user_id,
            )
            _activity(
                conn,
                user,
                action="created",
                area="tfl",
                entity_type="tfl_rule",
                entity_id=str(row.get("tfl_rule_id") or row.get("rule_id") or body.insurance_name),
                entity_label=f"TFL · {body.insurance_name}",
                after=row,
                fallback=f"Created TFL rule for {body.insurance_name}",
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"item": row})


@router.patch("/tfl-rules/{rule_id}")
def patch_tfl_rule(
    rule_id: str,
    body: TflRulePatch,
    user: AuthUser = Depends(require_roles(*TFL_EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, pr_tfl

    payload = body.model_dump(exclude_unset=True)
    try:
        with connection() as conn:
            before = pr_tfl.get_tfl_rule(conn, rule_id) or {}
            row = pr_tfl.update_tfl_rule(
                conn,
                rule_id,
                insurance_name=payload.get("insurance_name"),
                tfl_days=payload.get("tfl_days"),
                aliases=payload.get("aliases"),
                actor_id=user.user_id,
            )
            _activity(
                conn,
                user,
                action="updated",
                area="tfl",
                entity_type="tfl_rule",
                entity_id=rule_id,
                entity_label=f"TFL · {row.get('insurance_name') or rule_id}",
                before=before,
                after=row,
                keys=["insurance_name", "tfl_days", "aliases"],
                fallback="Updated TFL rule",
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"item": row})


@router.delete("/tfl-rules/{rule_id}")
def delete_tfl_rule(
    rule_id: str,
    user: AuthUser = Depends(require_roles(*TFL_EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, pr_tfl

    with connection() as conn:
        before = pr_tfl.get_tfl_rule(conn, rule_id)
        deleted = pr_tfl.delete_tfl_rule(conn, rule_id)
        if deleted and before:
            _activity(
                conn,
                user,
                action="deleted",
                area="tfl",
                entity_type="tfl_rule",
                entity_id=rule_id,
                entity_label=f"TFL · {before.get('insurance_name') or rule_id}",
                before=before,
                fallback=f"Deleted TFL rule for {before.get('insurance_name') or rule_id}",
            )
    if not deleted:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.get("/items/{work_item_id}")
def get_item(
    work_item_id: str,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        item = eligibility.get_work_item(conn, work_item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Not found")
        return _ser(
            {
                "item": item,
                "history": eligibility.get_history(conn, work_item_id),
                "comments": eligibility.get_comments(conn, work_item_id),
                "attachments": eligibility.get_attachments(conn, work_item_id),
                "ledger": eligibility.get_amount_ledger(conn, work_item_id),
            }
        )


@router.patch("/items/{work_item_id}")
def patch_item(
    work_item_id: str,
    body: PatchBody,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    updates = {
        k: v
        for k, v in body.model_dump(exclude_unset=True).items()
        if k in eligibility.EDITABLE_FIELDS
    }
    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields provided")
    try:
        with connection() as conn:
            before = eligibility.get_work_item(conn, work_item_id)
            item = eligibility.patch_work_item(
                conn,
                work_item_id,
                actor_id=user.user_id,
                updates=updates,
                reason_key=body.reason_key,
                reason_text=body.reason_text,
            )
            if item and not item.get("assigned_to"):
                item = eligibility.assign_work_item(
                    conn,
                    work_item_id,
                    actor_id=user.user_id,
                    assignee_id=user.user_id,
                )
            if item:
                keys = list(updates.keys())
                if before and item.get("assigned_to") != before.get("assigned_to"):
                    keys.extend(["assigned_to", "assigned_to_name"])
                _log_work_item(
                    conn,
                    user,
                    action="updated",
                    before=before,
                    after=item,
                    keys=keys,
                )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return _ser({"item": item})


@router.post("/items/{work_item_id}/adjustments")
def add_adjustment(
    work_item_id: str,
    body: AdjustmentBody,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    try:
        with connection() as conn:
            before = eligibility.get_work_item(conn, work_item_id)
            item = eligibility.add_amount_adjustment(
                conn,
                work_item_id,
                actor_id=user.user_id,
                column_name=body.column_name,
                amount=body.amount,
                check_number=body.check_number,
                check_date=body.check_date,
                note=body.note,
            )
            if not item:
                raise HTTPException(status_code=404, detail="Not found")
            _log_work_item(
                conn,
                user,
                action="updated",
                before=before,
                after=item,
                keys=[body.column_name, "check_number", "check_date"],
                fallback=f"Adjusted {body.column_name}",
            )
            return _ser(
                {
                    "item": item,
                    "ledger": eligibility.get_amount_ledger(conn, work_item_id),
                }
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/items/{work_item_id}/transition")
def transition(
    work_item_id: str,
    body: TransitionBody,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    try:
        with connection() as conn:
            before = eligibility.get_work_item(conn, work_item_id)
            item = eligibility.patch_work_item(
                conn,
                work_item_id,
                actor_id=user.user_id,
                updates={"eligibility_status": body.eligibility_status},
                reason_key=body.reason_key,
                reason_text=body.reason_text,
            )
            if item:
                _log_work_item(
                    conn,
                    user,
                    action="updated",
                    before=before,
                    after=item,
                    keys=["eligibility_status"],
                )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return _ser({"item": item})


def _parse_work_item_ids(raw: list[str]) -> list[str]:
    parsed = parse_uuid_list(raw) or []
    if len(parsed) != len(raw):
        raise HTTPException(status_code=400, detail="Invalid work item id")
    seen: list[str] = []
    for item_id in parsed:
        if item_id not in seen:
            seen.append(item_id)
    return seen


def _normalize_assignee(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    parsed = parse_uuid_list([str(raw)])
    if not parsed:
        raise HTTPException(status_code=400, detail="Invalid assignee")
    return parsed[0]


def _require_collector_assignee(conn, assignee_id: str | None) -> None:
    if not assignee_id:
        return
    from cashflow_db.repository import auth_users

    user = auth_users.get_user_by_id(conn, assignee_id)
    roles = auth_users.get_user_roles(conn, assignee_id) if user else []
    if not user or not user.get("is_active") or ROLE_COLLECTOR not in roles:
        raise HTTPException(status_code=400, detail="Assignee must be an active collector")


@router.post("/items/assign-bulk")
def assign_bulk(
    body: BulkAssignBody,
    user: AuthUser = Depends(require_roles(ROLE_OPS_ADMIN, ROLE_SUB_ADMIN)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    ids = _parse_work_item_ids(body.work_item_ids)
    assignee_id = _normalize_assignee(body.assigned_to)
    with connection() as conn:
        _require_collector_assignee(conn, assignee_id)
        members = eligibility.collection_member_ids(conn, ids)
        if any(item_id not in members for item_id in ids):
            raise HTTPException(status_code=400, detail="Not a collection visit")
        updated: list[dict[str, Any]] = []
        for work_item_id in ids:
            before = eligibility.get_work_item(conn, work_item_id)
            item = eligibility.assign_work_item(
                conn,
                work_item_id,
                actor_id=user.user_id,
                assignee_id=assignee_id,
            )
            if not item:
                raise HTTPException(status_code=404, detail="Not found")
            _log_work_item(
                conn,
                user,
                action="assigned",
                before=before,
                after=item,
                keys=["assigned_to", "assigned_to_name"],
            )
            updated.append(item)
    return _ser({"updated": len(updated), "items": updated})


@router.post("/items/assign-filter")
def assign_filter(
    body: FilterAssignBody,
    user: AuthUser = Depends(require_roles(ROLE_OPS_ADMIN, ROLE_SUB_ADMIN)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    assignee_id = _normalize_assignee(body.assigned_to)
    with connection() as conn:
        _require_collector_assignee(conn, assignee_id)
        updated = eligibility.assign_matching_work_items(
            conn,
            actor_id=user.user_id,
            assignee_id=assignee_id,
            q=body.q,
            facility=body.facility,
            month=body.month,
            insurance=body.insurance,
            visit_status=body.visit_status,
            assigned_to=parse_uuid_list(body.filter_assigned_to),
            unassigned=body.unassigned,
            bucket=body.bucket,
            collection_status=body.collection_status,
            root_cause=body.root_cause,
        )
        if updated:
            _activity(
                conn,
                user,
                action="assigned",
                area="collection",
                entity_type="collection_filter",
                entity_id=body.bucket or "collection",
                entity_label=f"Assigned {updated} collection visits",
                fallback=f"Assigned {updated} collection visits",
                force=True,
                extra={"updated": updated, "assigned_to": assignee_id, "bucket": body.bucket},
            )
    return _ser({"updated": updated})


@router.post("/items/{work_item_id}/assign")
def assign(
    work_item_id: str,
    body: AssignBody,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    parsed = parse_uuid_list([work_item_id])
    if not parsed:
        raise HTTPException(status_code=400, detail="Invalid work item id")
    work_item_id = parsed[0]
    assignee_id = _normalize_assignee(body.assigned_to)
    with connection() as conn:
        in_collection = work_item_id in eligibility.collection_member_ids(conn, [work_item_id])
        if in_collection and not (user.is_ops_admin or user.is_elevated_admin):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if in_collection:
            _require_collector_assignee(conn, assignee_id)
        before = eligibility.get_work_item(conn, work_item_id)
        item = eligibility.assign_work_item(
            conn,
            work_item_id,
            actor_id=user.user_id,
            assignee_id=assignee_id,
            reason_key=body.reason_key,
            reason_text=body.reason_text,
        )
        if item:
            _log_work_item(
                conn,
                user,
                action="assigned",
                before=before,
                after=item,
                keys=["assigned_to", "assigned_to_name"],
            )
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return _ser({"item": item})


@router.post("/items/{work_item_id}/lock")
def lock(
    work_item_id: str,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        result = eligibility.acquire_lock(
            conn,
            work_item_id,
            actor_id=user.user_id,
            force=user.is_elevated_admin,
        )
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Not found")
    if not result.get("ok"):
        raise HTTPException(
            status_code=409,
            detail={
                "message": f"Editing by {result.get('locked_by_name') or 'another user'}…",
                **{k: _ser(v) for k, v in result.items() if k != "ok"},
            },
        )
    return _ser(result)


@router.post("/items/{work_item_id}/heartbeat")
def heartbeat(
    work_item_id: str,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        result = eligibility.heartbeat_lock(
            conn, work_item_id, actor_id=user.user_id
        )
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return _ser(result)


@router.post("/items/{work_item_id}/unlock")
def unlock(
    work_item_id: str,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        result = eligibility.release_lock(
            conn,
            work_item_id,
            actor_id=user.user_id,
            force=user.is_elevated_admin,
        )
    if not result.get("ok") and result.get("error") == "not_found":
        raise HTTPException(status_code=404, detail="Not found")
    if not result.get("ok"):
        raise HTTPException(status_code=409, detail=result.get("error"))
    return _ser(result)


@router.post("/items/{work_item_id}/comments")
def add_comment(
    work_item_id: str,
    body: CommentBody,
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import client, connection, eligibility

    with connection() as conn:
        if not eligibility.get_work_item(conn, work_item_id):
            raise HTTPException(status_code=404, detail="Not found")
        client.execute(
            conn,
            """
            INSERT INTO ops.eligibility_comment (work_item_id, body, created_by)
            VALUES (%s::uuid, %s, %s::uuid)
            """,
            (work_item_id, body.body.strip(), user.user_id),
        )
        eligibility.add_history(
            conn,
            work_item_id=work_item_id,
            column_name="comment",
            old_value=None,
            new_value=body.body.strip()[:500],
            changed_by=user.user_id,
        )
        item = eligibility.get_work_item(conn, work_item_id)
        _log_work_item(
            conn,
            user,
            action="updated",
            before=item,
            after=item,
            extra={"comment": body.body.strip()[:500]},
            fallback="Added a comment",
            force=True,
        )
        return _ser({"comments": eligibility.get_comments(conn, work_item_id)})


@router.get("/attachments/{attachment_id}/file")
def open_attachment(
    attachment_id: str,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> FileResponse:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        att = eligibility.get_attachment(conn, attachment_id)
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")
    path_str = att.get("storage_path") or att.get("document_storage_path")
    if not path_str:
        raise HTTPException(status_code=404, detail="No file path")
    path = Path(path_str)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"File missing: {path.name}")
    filename = att.get("filename") or att.get("document_filename") or path.name
    return FileResponse(path, filename=filename, media_type="application/pdf")


@router.get("/generate/status")
def generate_status(
    _: AuthUser = Depends(require_roles(ROLE_SUPER, ROLE_SUB_ADMIN)),
) -> dict[str, Any]:
    return _ser(_generate_status_payload())


@router.post("/generate")
def generate(
    user: AuthUser = Depends(require_roles(ROLE_SUPER, ROLE_SUB_ADMIN)),
) -> dict[str, Any]:
    with _generate_lock:
        if _generate_job["running"]:
            raise HTTPException(status_code=409, detail="Generation already running")
        _generate_job["running"] = True
        _generate_job["started_at"] = datetime.now(timezone.utc).isoformat()
        _generate_job["finished_at"] = None
        _generate_job["actor_user_id"] = user.user_id
    thread = threading.Thread(target=_run_generate_job, name="elig-generate", daemon=True)
    thread.start()
    return {"started": True, **_generate_status_payload()}


@router.get("/posting-users")
def posting_users(
    role: str | None = None,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> list[dict[str, Any]]:
    """Users that can be assignees (anyone who can open the sheet)."""
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        users = auth_users.list_users(conn)
    out = []
    for u in users:
        roles = u.get("roles") or []
        if not u.get("is_active"):
            continue
        if role == "collector":
            allowed = "collector" in roles
        else:
            allowed = (
                "posting_team" in roles
                or "super_admin" in roles
                or "finance" in roles
                or "collector" in roles
                or "ops_admin" in roles
                or "sub_admin" in roles
            )
        if allowed:
            out.append(
                {
                    "user_id": str(u["user_id"]),
                    "display_name": u["display_name"],
                    "username": u["username"],
                    "collector_code": u.get("collector_code"),
                }
            )
    return out

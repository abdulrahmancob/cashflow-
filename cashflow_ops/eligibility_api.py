"""Eligibility work-queue HTTP API."""

from __future__ import annotations

import io
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_POSTING,
    ROLE_SUPER,
    AuthUser,
    get_current_user,
    parse_uuid_list,
    require_roles,
)

router = APIRouter(prefix="/eligibility", tags=["eligibility"])

EDIT_ROLES = (ROLE_POSTING, ROLE_SUPER)
VIEW_ROLES = (ROLE_POSTING, ROLE_SUPER, "finance")


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
    eligibility_status: str | None = None
    reference_number: str | None = None
    notes: str | None = None
    reason_key: str | None = None
    reason_text: str | None = None


class AssignBody(BaseModel):
    assigned_to: str | None = None
    reason_key: str | None = None
    reason_text: str | None = None


class TransitionBody(BaseModel):
    eligibility_status: str
    reason_key: str
    reason_text: str | None = None


class CommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.get("/meta")
def meta(_: AuthUser = Depends(require_roles(*VIEW_ROLES))) -> dict[str, Any]:
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
    assigned_to: list[str] | None = Query(None),
    unassigned: bool = False,
    sort_by: str = "dos",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        data = eligibility.list_work_items(
            conn,
            q=q,
            facility=facility,
            month=month,
            insurance=insurance,
            status=status,
            assigned_to=parse_uuid_list(assigned_to),
            unassigned=unassigned,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page=page,
            page_size=page_size,
        )
    return _ser(data)


@router.get("/items/export")
def export_items(
    q: str | None = None,
    facility: list[str] | None = Query(None),
    month: list[str] | None = Query(None),
    insurance: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    assigned_to: list[str] | None = Query(None),
    unassigned: bool = False,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, eligibility

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    with connection() as conn:
        data = eligibility.list_work_items(
            conn,
            q=q,
            facility=facility,
            month=month,
            insurance=insurance,
            status=status,
            assigned_to=parse_uuid_list(assigned_to),
            unassigned=unassigned,
            page=1,
            page_size=5000,
        )
    wb = Workbook()
    ws = wb.active
    ws.title = "Eligibility Queue"
    headers = [
        "Patient",
        "EMR ID",
        "DOS",
        "DOB",
        "Facility",
        "Insurance",
        "Eligibility Status",
        "Reference Number",
        "Notes",
        "Assigned To",
        "Updated By",
        "Updated At",
    ]
    ws.append(headers)
    for row in data["items"]:
        ws.append(
            [
                row.get("patient_name"),
                row.get("emr_patient_id"),
                str(row.get("dos") or ""),
                str(row.get("dob") or ""),
                row.get("facility_name"),
                row.get("insurance_name"),
                row.get("eligibility_status"),
                row.get("reference_number"),
                row.get("notes"),
                row.get("assigned_to_name"),
                row.get("updated_by_name"),
                str(row.get("updated_at") or ""),
            ]
        )
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=eligibility_queue.xlsx"
        },
    )


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
            }
        )


@router.patch("/items/{work_item_id}")
def patch_item(
    work_item_id: str,
    body: PatchBody,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    updates = {
        k: v
        for k, v in body.model_dump().items()
        if k in ("eligibility_status", "reference_number", "notes") and v is not None
    }
    if not updates:
        raise HTTPException(status_code=400, detail="No editable fields provided")
    try:
        with connection() as conn:
            item = eligibility.patch_work_item(
                conn,
                work_item_id,
                actor_id=user.user_id,
                updates=updates,
                reason_key=body.reason_key,
                reason_text=body.reason_text,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return _ser({"item": item})


@router.post("/items/{work_item_id}/transition")
def transition(
    work_item_id: str,
    body: TransitionBody,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    try:
        with connection() as conn:
            item = eligibility.patch_work_item(
                conn,
                work_item_id,
                actor_id=user.user_id,
                updates={"eligibility_status": body.eligibility_status},
                reason_key=body.reason_key,
                reason_text=body.reason_text,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return _ser({"item": item})


@router.post("/items/{work_item_id}/assign")
def assign(
    work_item_id: str,
    body: AssignBody,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    # Posting can self-assign; reassignment to others requires super_admin
    if (
        body.assigned_to
        and body.assigned_to != user.user_id
        and not user.is_super_admin
    ):
        raise HTTPException(status_code=403, detail="Only managers can reassign")
    with connection() as conn:
        item = eligibility.assign_work_item(
            conn,
            work_item_id,
            actor_id=user.user_id,
            assignee_id=body.assigned_to,
            reason_key=body.reason_key,
            reason_text=body.reason_text,
        )
    if not item:
        raise HTTPException(status_code=404, detail="Not found")
    return _ser({"item": item})


@router.post("/items/{work_item_id}/lock")
def lock(
    work_item_id: str,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        result = eligibility.acquire_lock(
            conn,
            work_item_id,
            actor_id=user.user_id,
            force=user.is_super_admin,
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
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
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
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, eligibility

    with connection() as conn:
        result = eligibility.release_lock(
            conn,
            work_item_id,
            actor_id=user.user_id,
            force=user.is_super_admin,
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
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
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


@router.post("/generate")
def generate(
    user: AuthUser = Depends(require_roles(ROLE_SUPER)),
) -> dict[str, Any]:
    from cashflow_db.services.eligibility_generator import generate_eligibility_work_items

    _ = user
    return generate_eligibility_work_items(from_db=True)


@router.get("/posting-users")
def posting_users(
    _: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> list[dict[str, Any]]:
    """Users that can be assignees (posting team + admins)."""
    from cashflow_db.repository import auth_users, connection

    with connection() as conn:
        users = auth_users.list_users(conn)
    out = []
    for u in users:
        roles = u.get("roles") or []
        if not u.get("is_active"):
            continue
        if "posting_team" in roles or "super_admin" in roles:
            out.append(
                {
                    "user_id": str(u["user_id"]),
                    "display_name": u["display_name"],
                    "username": u["username"],
                }
            )
    return out

"""Transaction Tracker portal HTTP API."""

from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cashflow_ops.security import AuthUser, get_tracker_perms, require_tracker_perm

router = APIRouter(prefix="/tracker", tags=["tracker"])


def _ser(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ser(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if hasattr(obj, "hex"):
        return str(obj)
    return obj


class RowCreate(BaseModel):
    payment_id: str = Field(min_length=1, max_length=120)
    month_date: date | None = None
    txn_date: date
    amount: Decimal
    eft_1: str | None = None
    eft_2: str | None = None
    transaction_type: str | None = None
    description: str | None = None
    check_reference: str | None = None
    bank_name: str | None = None
    billing_status: str | None = None
    collector: str | None = None
    posted: bool | None = None
    notes: str | None = None
    assigned_date: date | None = None
    claims: str | None = None


class RowPatch(BaseModel):
    version: int
    payment_id: str | None = None
    month_date: date | None = None
    txn_date: date | None = None
    amount: Decimal | None = None
    eft_1: str | None = None
    eft_2: str | None = None
    transaction_type: str | None = None
    description: str | None = None
    check_reference: str | None = None
    bank_name: str | None = None
    billing_status: str | None = None
    collector: str | None = None
    posted: bool | None = None
    notes: str | None = None
    assigned_date: date | None = None
    claims: str | None = None


class VersionBody(BaseModel):
    version: int


class GrantBody(BaseModel):
    can_view: bool = False
    can_edit: bool = False
    can_upload: bool = False
    can_admin: bool = False


class CommitBody(BaseModel):
    preview_id: str


from cashflow_ops.security import get_current_user  # noqa: E402


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    """Probe tracker permissions for nav (does not require view grant)."""
    perms = get_tracker_perms(user)
    return {
        "user_id": user.user_id,
        "resource_key": "transaction_tracker",
        **perms,
    }


@router.get("/months")
def months(user: AuthUser = Depends(require_tracker_perm("view"))) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        return {"months": tracker.available_months(conn)}


@router.get("/rows")
def list_rows(
    month: str | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    q: str | None = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 100,
    _: AuthUser = Depends(require_tracker_perm("view")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        data = tracker.list_rows(
            conn,
            month=month,
            date_from=date_from,
            date_to=date_to,
            q=q,
            include_deleted=include_deleted,
            page=page,
            page_size=page_size,
        )
    return _ser(data)


@router.post("/rows")
def create_row(
    body: RowCreate,
    user: AuthUser = Depends(require_tracker_perm("edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    data = body.model_dump()
    if data.get("month_date") is None and data.get("txn_date"):
        data["month_date"] = data["txn_date"].replace(day=1)
    try:
        with connection() as conn:
            row = tracker.create_row(conn, data, actor_user_id=user.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser(row)


def _handle_conflict(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        raise HTTPException(status_code=404, detail="Row not found")
    if result.get("__conflict__"):
        raise HTTPException(
            status_code=409,
            detail={"message": "Version conflict", "current": _ser(result.get("current"))},
        )
    return result


@router.patch("/rows/{row_id}")
def patch_row(
    row_id: str,
    body: RowPatch,
    user: AuthUser = Depends(require_tracker_perm("edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    data = body.model_dump(exclude_unset=True)
    version = int(data.pop("version"))
    with connection() as conn:
        result = tracker.update_row(
            conn, row_id, data, version=version, actor_user_id=user.user_id
        )
    return _ser(_handle_conflict(result))


@router.post("/rows/{row_id}/delete")
def delete_row(
    row_id: str,
    body: VersionBody,
    user: AuthUser = Depends(require_tracker_perm("edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        result = tracker.soft_delete_row(
            conn, row_id, version=body.version, actor_user_id=user.user_id
        )
    return _ser(_handle_conflict(result))


@router.post("/rows/{row_id}/restore")
def restore_row(
    row_id: str,
    body: VersionBody,
    user: AuthUser = Depends(require_tracker_perm("edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    try:
        with connection() as conn:
            result = tracker.restore_row(
                conn, row_id, version=body.version, actor_user_id=user.user_id
            )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ser(_handle_conflict(result))


@router.get("/rows/{row_id}/history")
def row_history(
    row_id: str,
    _: AuthUser = Depends(require_tracker_perm("view")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        items = tracker.row_history(conn, row_id)
    return _ser({"items": items})


@router.get("/export")
def export_xlsx(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    _: AuthUser = Depends(require_tracker_perm("view")),
) -> StreamingResponse:
    from cashflow_db.loaders.tracker_xlsx import export_tracker_workbook
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        rows = tracker.list_active_for_export(
            conn, date_from=date_from, date_to=date_to
        )
    payload = export_tracker_workbook(rows)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": "attachment; filename=Transaction_Tracker.xlsx"
        },
    )


@router.post("/upload/preview")
async def upload_preview(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_tracker_perm("upload")),
) -> dict[str, Any]:
    from cashflow_db.loaders.tracker_xlsx import parse_tracker_workbook
    from cashflow_db.repository import connection, tracker

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    parsed = parse_tracker_workbook(content)
    errors = [
        {"sheet": e.sheet, "row": e.row, "message": e.message} for e in parsed.errors
    ]
    row_dicts = [r.to_dict() for r in parsed.rows]
    with connection() as conn:
        diff = tracker.build_upload_diff(conn, row_dicts)
        summary = {
            "filename": file.filename,
            "parsed_rows": len(row_dicts),
            "error_count": len(errors),
            "errors_sample": errors[:50],
            "skipped_sheets": parsed.skipped_sheets,
            "counts": diff["counts"],
            "month_bounds": diff["month_bounds"],
            "sample_adds": diff["adds"][:10],
            "sample_updates": [
                {
                    "payment_id": u["after"].get("payment_id"),
                    "row_id": u["row_id"],
                }
                for u in diff["updates"][:10]
            ],
            "sample_soft_deletes": [
                {"payment_id": d["payment_id"], "row_id": d["row_id"]}
                for d in diff["soft_deletes"][:10]
            ],
        }
        saved = tracker.save_upload_preview(
            conn,
            actor_user_id=user.user_id,
            summary=summary,
            payload={
                "adds": diff["adds"],
                "updates": diff["updates"],
                "soft_deletes": diff["soft_deletes"],
            },
        )
    return _ser(
        {
            "preview_id": saved["preview_id"],
            "expires_at": saved["expires_at"],
            **summary,
        }
    )


@router.post("/upload/commit")
def upload_commit(
    body: CommitBody,
    user: AuthUser = Depends(require_tracker_perm("upload")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        preview = tracker.get_upload_preview(conn, body.preview_id)
        if not preview:
            raise HTTPException(status_code=404, detail="Preview not found")
        expires = preview["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            tracker.delete_upload_preview(conn, body.preview_id)
            raise HTTPException(status_code=410, detail="Preview expired")
        if str(preview["created_by"]) != user.user_id and not user.is_super_admin:
            raise HTTPException(status_code=403, detail="Preview belongs to another user")
        payload = preview["payload_json"]
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        counts = tracker.apply_upload_payload(
            conn, payload, actor_user_id=user.user_id
        )
        tracker.delete_upload_preview(conn, body.preview_id)
    return _ser({"ok": True, **counts})


@router.get("/grants")
def list_grants(
    user: AuthUser = Depends(require_tracker_perm("admin")),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, connection, tracker

    with connection() as conn:
        grants = tracker.list_grants(conn)
        users = [
            {
                "user_id": u["user_id"],
                "username": u["username"],
                "display_name": u["display_name"],
                "is_active": u["is_active"],
                "roles": u.get("roles") or [],
            }
            for u in auth_users.list_users(conn)
            if u.get("is_active")
        ]
    return _ser({"grants": grants, "users": users})


@router.put("/grants/{user_id}")
def put_grant(
    user_id: str,
    body: GrantBody,
    user: AuthUser = Depends(require_tracker_perm("admin")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        row = tracker.upsert_grant(
            conn,
            user_id=user_id,
            can_view=body.can_view,
            can_edit=body.can_edit,
            can_upload=body.can_upload,
            can_admin=body.can_admin,
            granted_by=user.user_id,
        )
    return _ser(row)


@router.delete("/grants/{user_id}")
def delete_grant(
    user_id: str,
    user: AuthUser = Depends(require_tracker_perm("admin")),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, tracker

    with connection() as conn:
        ok = tracker.delete_grant(
            conn, user_id=user_id, actor_user_id=user.user_id
        )
    if not ok:
        raise HTTPException(status_code=404, detail="Grant not found")
    return {"ok": True}

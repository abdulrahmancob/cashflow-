"""Checks & Deposits portal HTTP API."""

from __future__ import annotations

import io
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from cashflow_db.loaders.checks_deposits_xlsx import ALLOWED_YEARS
from cashflow_ops.security import (
    CHECKS_DEPOSITS_RESOURCE,
    AuthUser,
    get_current_user,
    require_resource_perm,
)

router = APIRouter(prefix="/checks-deposits", tags=["checks-deposits"])


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


def _check_deposit_year(value: date | None) -> date | None:
    if value is not None and value.year not in ALLOWED_YEARS:
        allowed = ", ".join(str(year) for year in sorted(ALLOWED_YEARS))
        raise ValueError(f"deposit_date must fall in {allowed}")
    return value


class RowCreate(BaseModel):
    sheet_key: str | None = Field(default=None, max_length=200)
    check_date: date | None = None
    payer: str | None = None
    amount: Decimal
    check_number: str | None = None
    deposit_date: date
    link: str | None = None
    notes: str | None = None

    @field_validator("deposit_date")
    @classmethod
    def _year(cls, value: date) -> date:
        checked = _check_deposit_year(value)
        assert checked is not None
        return checked


class RowPatch(BaseModel):
    version: int
    sheet_key: str | None = None
    check_date: date | None = None
    payer: str | None = None
    amount: Decimal | None = None
    check_number: str | None = None
    deposit_date: date | None = None
    link: str | None = None
    notes: str | None = None

    @field_validator("deposit_date")
    @classmethod
    def _year(cls, value: date | None) -> date | None:
        return _check_deposit_year(value)


class VersionBody(BaseModel):
    version: int


class GrantBody(BaseModel):
    can_view: bool = False
    can_edit: bool = False
    can_upload: bool = False
    can_admin: bool = False


class CommitBody(BaseModel):
    preview_id: str


def _handle_conflict(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        raise HTTPException(status_code=404, detail="Row not found")
    if result.get("__conflict__"):
        raise HTTPException(
            status_code=409,
            detail={"message": "Version conflict", "current": _ser(result.get("current"))},
        )
    return result


def _activity(conn, user, **kwargs):
    from cashflow_ops.activity_api import log_write

    log_write(conn, user, **kwargs)


def _row_label(row: dict[str, Any] | None) -> str:
    from cashflow_db.repository import portal_activity

    return portal_activity.checks_deposits_row_label(row)


@router.get("/me")
def me(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    """Probe Checks & Deposits permissions for nav (does not require view grant)."""
    from cashflow_ops.security import get_resource_perms

    perms = get_resource_perms(user, CHECKS_DEPOSITS_RESOURCE)
    return {
        "user_id": user.user_id,
        "resource_key": CHECKS_DEPOSITS_RESOURCE,
        **perms,
    }


@router.get("/months")
def months(user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "view"))) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    with connection() as conn:
        return {"months": checks_deposits.available_months(conn)}


@router.get("/rows")
def list_rows(
    month: str | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    q: str | None = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = 100,
    _: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "view")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    with connection() as conn:
        data = checks_deposits.list_rows(
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
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    data = body.model_dump()
    data["deposit_month"] = data["deposit_date"].replace(day=1)
    if not data.get("sheet_key"):
        data["sheet_key"] = f"manual:{uuid4()}"
    try:
        with connection() as conn:
            row = checks_deposits.create_row(conn, data, actor_user_id=user.user_id)
            _activity(
                conn,
                user,
                action="created",
                area="checks_deposits",
                entity_type="checks_deposits_row",
                entity_id=str(row.get("row_id") or ""),
                entity_label=_row_label(row),
                after=row,
                fallback=f"Created check {_row_label(row)}",
            )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser(row)


@router.patch("/rows/{row_id}")
def patch_row(
    row_id: str,
    body: RowPatch,
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    data = body.model_dump(exclude_unset=True)
    version = int(data.pop("version"))
    if data.get("deposit_date") is not None:
        data["deposit_month"] = data["deposit_date"].replace(day=1)
    with connection() as conn:
        before = checks_deposits.get_row(conn, row_id)
        result = checks_deposits.update_row(
            conn, row_id, data, version=version, actor_user_id=user.user_id
        )
        if result and not result.get("__conflict__"):
            _activity(
                conn,
                user,
                action="updated",
                area="checks_deposits",
                entity_type="checks_deposits_row",
                entity_id=row_id,
                entity_label=_row_label(result),
                before=before,
                after=result,
                keys=list(data.keys()),
            )
    return _ser(_handle_conflict(result))


@router.post("/rows/{row_id}/delete")
def delete_row(
    row_id: str,
    body: VersionBody,
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    with connection() as conn:
        before = checks_deposits.get_row(conn, row_id)
        result = checks_deposits.soft_delete_row(
            conn, row_id, version=body.version, actor_user_id=user.user_id
        )
        if result and not result.get("__conflict__"):
            _activity(
                conn,
                user,
                action="deleted",
                area="checks_deposits",
                entity_type="checks_deposits_row",
                entity_id=row_id,
                entity_label=_row_label(result or before),
                before=before,
                after=result,
                fallback="Deleted check",
            )
    return _ser(_handle_conflict(result))


@router.post("/rows/{row_id}/restore")
def restore_row(
    row_id: str,
    body: VersionBody,
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "edit")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    try:
        with connection() as conn:
            before = checks_deposits.get_row(conn, row_id)
            result = checks_deposits.restore_row(
                conn, row_id, version=body.version, actor_user_id=user.user_id
            )
            if result and not result.get("__conflict__"):
                _activity(
                    conn,
                    user,
                    action="restored",
                    area="checks_deposits",
                    entity_type="checks_deposits_row",
                    entity_id=row_id,
                    entity_label=_row_label(result or before),
                    before=before,
                    after=result,
                    fallback="Restored check",
                )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _ser(_handle_conflict(result))


@router.get("/rows/{row_id}/history")
def row_history(
    row_id: str,
    _: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "view")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    with connection() as conn:
        items = checks_deposits.row_history(conn, row_id)
    return _ser({"items": items})


@router.get("/export")
def export_xlsx(
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    _: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "view")),
) -> StreamingResponse:
    from cashflow_db.loaders.checks_deposits_xlsx import export_checks_deposits_workbook
    from cashflow_db.repository import checks_deposits, connection

    with connection() as conn:
        rows = checks_deposits.list_active_for_export(
            conn, date_from=date_from, date_to=date_to
        )
    payload = export_checks_deposits_workbook(rows)
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Checks_Deposits_2026.xlsx"},
    )


@router.post("/upload/preview")
async def upload_preview(
    file: UploadFile = File(...),
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "upload")),
) -> dict[str, Any]:
    from cashflow_db.loaders.checks_deposits_xlsx import parse_checks_deposits_workbook
    from cashflow_db.repository import checks_deposits, connection

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    parsed = parse_checks_deposits_workbook(content)
    errors = [
        {"sheet": err.sheet, "row": err.row, "message": err.message} for err in parsed.errors
    ]
    row_dicts = [row.to_dict() for row in parsed.rows]
    with connection() as conn:
        diff = checks_deposits.build_upload_diff(conn, row_dicts)
        summary = {
            "filename": file.filename,
            "parsed_rows": len(row_dicts),
            "error_count": len(errors),
            "errors_sample": errors[:50],
            "skipped_sheets": parsed.skipped_sheets,
            "skipped_out_of_year": parsed.skipped_out_of_year,
            "skipped_no_amount": parsed.skipped_no_amount,
            "skipped_no_deposit_date": parsed.skipped_no_deposit_date,
            "duplicate_sheet_keys": parsed.duplicate_sheet_keys,
            "counts": diff["counts"],
            "month_bounds": diff["month_bounds"],
            "sample_adds": diff["adds"][:10],
            "sample_updates": [
                {"sheet_key": item["after"].get("sheet_key"), "row_id": item["row_id"]}
                for item in diff["updates"][:10]
            ],
            "sample_soft_deletes": [
                {"sheet_key": item["sheet_key"], "row_id": item["row_id"]}
                for item in diff["soft_deletes"][:10]
            ],
        }
        saved = checks_deposits.save_upload_preview(
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
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "upload")),
) -> dict[str, Any]:
    from cashflow_db.repository import checks_deposits, connection

    with connection() as conn:
        preview = checks_deposits.get_upload_preview(conn, body.preview_id)
        if not preview:
            raise HTTPException(status_code=404, detail="Preview not found")
        expires = preview["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            checks_deposits.delete_upload_preview(conn, body.preview_id)
            raise HTTPException(status_code=410, detail="Preview expired")
        if str(preview["created_by"]) != user.user_id and not user.is_elevated_admin:
            raise HTTPException(status_code=403, detail="Preview belongs to another user")
        payload = preview["payload_json"]
        if isinstance(payload, str):
            import json

            payload = json.loads(payload)
        counts = checks_deposits.apply_upload_payload(
            conn, payload, actor_user_id=user.user_id
        )
        changed = (
            int(counts.get("adds") or 0)
            + int(counts.get("updates") or 0)
            + int(counts.get("soft_deletes") or 0)
        )
        from cashflow_ops.activity_api import log_event

        log_event(
            conn,
            user,
            action="uploaded",
            area="checks_deposits",
            entity_type="checks_deposits_upload",
            entity_id=str(body.preview_id),
            entity_label="Checks & Deposits upload",
            summary=f"Uploaded {changed} check rows",
            details={"counts": {k: v for k, v in counts.items() if k != "upload_batch_id"}},
        )
        checks_deposits.delete_upload_preview(conn, body.preview_id)
    return _ser({"ok": True, **counts})


@router.get("/grants")
def list_grants(
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "admin")),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, checks_deposits, connection

    with connection() as conn:
        grants = checks_deposits.list_grants(conn)
        users = [
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "display_name": row["display_name"],
                "is_active": row["is_active"],
                "roles": row.get("roles") or [],
            }
            for row in auth_users.list_users(conn)
            if row.get("is_active")
        ]
    return _ser({"grants": grants, "users": users})


@router.put("/grants/{user_id}")
def put_grant(
    user_id: str,
    body: GrantBody,
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "admin")),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, checks_deposits, connection

    with connection() as conn:
        before = checks_deposits.get_grant(conn, user_id)
        target = auth_users.get_user_by_id(conn, user_id) or {}
        label = (
            f"{target.get('display_name') or target.get('username') or user_id}"
            " · Checks & Deposits access"
        )
        row = checks_deposits.upsert_grant(
            conn,
            user_id=user_id,
            can_view=body.can_view,
            can_edit=body.can_edit,
            can_upload=body.can_upload,
            can_admin=body.can_admin,
            granted_by=user.user_id,
        )
        _activity(
            conn,
            user,
            action="updated" if before.get("grant_id") else "created",
            area="checks_deposits",
            entity_type="checks_deposits_grant",
            entity_id=user_id,
            entity_label=label,
            before=before,
            after=row,
            keys=["can_view", "can_edit", "can_upload", "can_admin"],
            fallback=f"Updated Checks & Deposits access for {label}",
        )
    return _ser(row)


@router.delete("/grants/{user_id}")
def delete_grant(
    user_id: str,
    user: AuthUser = Depends(require_resource_perm(CHECKS_DEPOSITS_RESOURCE, "admin")),
) -> dict[str, Any]:
    from cashflow_db.repository import auth_users, checks_deposits, connection

    with connection() as conn:
        before = checks_deposits.get_grant(conn, user_id)
        target = auth_users.get_user_by_id(conn, user_id) or {}
        label = (
            f"{target.get('display_name') or target.get('username') or user_id}"
            " · Checks & Deposits access"
        )
        ok = checks_deposits.delete_grant(conn, user_id=user_id, actor_user_id=user.user_id)
        if ok:
            _activity(
                conn,
                user,
                action="deleted",
                area="checks_deposits",
                entity_type="checks_deposits_grant",
                entity_id=user_id,
                entity_label=label,
                before=before,
                fallback=f"Removed Checks & Deposits access for {label}",
            )
    if not ok:
        raise HTTPException(status_code=404, detail="Grant not found")
    return {"ok": True}

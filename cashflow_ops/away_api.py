"""Away-status HTTP API: personal breaks and the admin board."""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_OPS_ADMIN,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUB_ADMIN,
    ROLE_SUPER,
    AuthUser,
    get_current_user,
    require_roles,
)

router = APIRouter(prefix="/away", tags=["away"])

BOARD_ROLES = (
    ROLE_SUPER,
    ROLE_SUB_ADMIN,
    ROLE_OPS_ADMIN,
    ROLE_SECOND_SUBMISSION_LEAD,
)


def _ser(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ser(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    if hasattr(obj, "hex"):
        return str(obj)
    return obj


class StartAwayBody(BaseModel):
    kind: str = Field(min_length=1, max_length=32)
    planned_minutes: int | None = None
    with_whom: str | None = Field(default=None, max_length=200)


def _away_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.get("/me")
def away_me(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    from cashflow_db.repository import connection, user_away

    with connection() as conn:
        return _ser(user_away.my_away(conn, user.user_id))


@router.post("/start")
def away_start(
    body: StartAwayBody,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, user_away

    try:
        with connection() as conn:
            return _ser(
                user_away.start_away(
                    conn,
                    user.user_id,
                    kind=body.kind,
                    planned_minutes=body.planned_minutes,
                    with_whom=body.with_whom,
                )
            )
    except ValueError as exc:
        raise _away_error(exc) from exc


@router.post("/end")
def away_end(user: AuthUser = Depends(get_current_user)) -> dict[str, Any]:
    from cashflow_db.repository import connection, user_away

    try:
        with connection() as conn:
            return _ser(user_away.end_away(conn, user.user_id))
    except ValueError as exc:
        raise _away_error(exc) from exc


@router.get("/board/export")
def away_board_export_view(
    user: AuthUser = Depends(require_roles(*BOARD_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, user_away

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    with connection() as conn:
        payload = user_away.away_board_export(
            conn,
            role_keys=user_away.board_scope_roles(user.roles),
        )
    workbook = Workbook(write_only=True)
    for title, headers, rows in user_away.export_tables(payload):
        sheet = workbook.create_sheet(title)
        sheet.append(headers)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=away_board.xlsx"},
    )


@router.get("/board")
def away_board_view(
    day: date | None = Query(None),
    user: AuthUser = Depends(require_roles(*BOARD_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, user_away

    with connection() as conn:
        return _ser(
            user_away.away_board(
                conn,
                day=day,
                role_keys=user_away.board_scope_roles(user.roles),
            )
        )

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

from cashflow_ops.heavy import heavy_guard
from cashflow_ops.security import (
    ROLE_OPS_ADMIN,
    ROLE_REDTEAM_LEADER,
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
    ROLE_REDTEAM_LEADER,
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
        mine = user_away.my_away(conn, user.user_id)
        return _ser({**mine, "presence": user_away.my_presence(conn, user.user_id)})


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


@router.get("/board/export", dependencies=[Depends(heavy_guard)])
def away_board_export_view(
    team: str | None = Query(None, max_length=32),
    user: AuthUser = Depends(require_roles(*BOARD_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, user_away

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc

    if team and team not in user_away.TEAM_LABELS:
        raise HTTPException(status_code=400, detail=f"unknown team: {team}")
    with connection() as conn:
        payload = user_away.away_board_export(
            conn,
            role_keys=user_away.board_scope_roles(user.roles),
            hidden_roles=user_away.board_hidden_roles(user.roles),
            team=team or None,
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
        headers={
            "Content-Disposition": (
                f"attachment; filename=away_board_{team}.xlsx"
                if team
                else "attachment; filename=away_board.xlsx"
            )
        },
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
                hidden_roles=user_away.board_hidden_roles(user.roles),
            )
        )


def _scoped_target(conn: Any, viewer: AuthUser, target_id: str) -> None:
    from cashflow_db.repository import user_away

    if not user_away.person_in_board_scope(conn, viewer.roles, target_id):
        raise HTTPException(status_code=404, detail="person not on your board")


@router.post("/people/{target_id}/end")
def away_end_for(
    target_id: UUID,
    user: AuthUser = Depends(require_roles(*BOARD_ROLES)),
) -> dict[str, Any]:
    """A lead or admin ends a session someone forgot to close."""
    from cashflow_db.repository import connection, user_away

    try:
        with connection() as conn:
            _scoped_target(conn, user, str(target_id))
            return _ser(user_away.end_away(conn, str(target_id)))
    except ValueError as exc:
        raise _away_error(exc) from exc


@router.get("/people/{target_id}/pings")
def away_pings_for(
    target_id: UUID,
    limit: int = Query(100, ge=1, le=500),
    user: AuthUser = Depends(require_roles(*BOARD_ROLES)),
) -> dict[str, Any]:
    """The last pings from one person, to see why the board shows them as it does."""
    from cashflow_db.repository import connection, presence

    with connection() as conn:
        _scoped_target(conn, user, str(target_id))
        return _ser({"pings": presence.ping_log(conn, str(target_id), limit=limit)})

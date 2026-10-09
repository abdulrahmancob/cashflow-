"""Team work-analytics HTTP API."""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from cashflow_ops.heavy import heavy_guard
from cashflow_ops.security import (
    ROLE_ANALYTICS_VIEWER,
    ROLE_OPS_ADMIN,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUB_ADMIN,
    ROLE_SUPER,
    AuthUser,
    get_current_user,
    renew_session_if_due,
    require_roles,
)

router = APIRouter(prefix="/analytics", tags=["analytics"])

VIEW_ROLES = (
    ROLE_SUPER,
    ROLE_OPS_ADMIN,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUB_ADMIN,
    ROLE_ANALYTICS_VIEWER,
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


class HeartbeatBody(BaseModel):
    page_path: str | None = Field(default=None, max_length=200)
    idle: bool = False
    presence: bool = False
    closed: bool = False
    desk_permission: str | None = Field(default=None, max_length=32)
    state: str | None = Field(default=None, max_length=16)
    visible: bool | None = None
    tab_id: str | None = Field(default=None, max_length=64)
    client_at: datetime | None = None


def _period_bounds(
    preset: str | None,
    date_from: date | None,
    date_to: date | None,
) -> tuple[datetime, datetime]:
    from cashflow_db.repository import work_analytics

    return work_analytics.resolve_period(preset, date_from, date_to)


@router.post("/heartbeat")
def heartbeat(
    body: HeartbeatBody,
    request: Request,
    response: Response,
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    if body.visible and body.state == "active" and not body.closed:
        renew_session_if_due(request, response, user)
    with connection() as conn:
        return _ser(
            work_analytics.record_heartbeat(
                conn,
                user.user_id,
                page_path=body.page_path,
                idle=body.idle,
                presence=body.presence,
                closed=body.closed,
                desk_permission=body.desk_permission,
                state=body.state,
                visible=body.visible,
                tab_id=body.tab_id,
                client_at=body.client_at,
            )
        )


def _analytics_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc) or "forbidden")
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@router.get("/my-today")
def my_today(
    area: str = Query(...),
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    try:
        with connection() as conn:
            count = work_analytics.completed_today_for_user(
                conn, user.user_id, area
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"area": area.strip().lower(), "completed_today": count}


@router.get("/my-assignments")
def my_assignments(
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    with connection() as conn:
        progress = work_analytics.my_assignment_progress(conn, user.user_id)
    return {"assigned": progress["assigned"], "finished": progress["finished"]}


@router.get("/team")
def team(
    preset: str | None = Query("month"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    team: str | None = Query(None),
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    start, end = _period_bounds(preset, date_from, date_to)
    try:
        with connection() as conn:
            return _ser(
                work_analytics.team_summary(
                    conn,
                    user.roles,
                    start=start,
                    end=end,
                    team=team,
                    viewer_id=user.user_id,
                )
            )
    except (PermissionError, ValueError) as exc:
        raise _analytics_error(exc) from exc


@router.get("/ss/breakdown")
def ss_breakdown(
    grain: str | None = Query("month"),
    year: int | None = Query(None),
    month: str | None = Query(None),
    user_id: str | None = Query(None),
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    if user_id:
        try:
            UUID(user_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid user_id") from exc
    try:
        with connection() as conn:
            return _ser(
                work_analytics.ss_breakdown(
                    conn,
                    user.roles,
                    grain=grain,
                    year=year,
                    month=month,
                    user_id=user_id,
                )
            )
    except (PermissionError, ValueError) as exc:
        raise _analytics_error(exc) from exc


@router.get("/collection/dead-root-causes")
def collection_dead_root_causes(
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    try:
        with connection() as conn:
            return _ser(work_analytics.collection_dead_root_causes(conn, user.roles))
    except (PermissionError, ValueError) as exc:
        raise _analytics_error(exc) from exc


@router.get("/collection/root-causes")
def collection_root_causes(
    year: int | None = Query(None),
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    try:
        with connection() as conn:
            return _ser(
                work_analytics.collection_root_cause_breakdown(
                    conn, user.roles, year=year
                )
            )
    except (PermissionError, ValueError) as exc:
        raise _analytics_error(exc) from exc


@router.get("/export", dependencies=[Depends(heavy_guard)])
def analytics_export(
    preset: str | None = Query("month"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    year: int | None = Query(None),
    grain: str | None = Query("month"),
    month: str | None = Query(None),
    user_id: str | None = Query(None),
    team: str | None = Query(None),
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> StreamingResponse:
    from cashflow_db.repository import connection, work_analytics
    from cashflow_ops.analytics_export import EXPORT_FILENAMES, export_tables

    try:
        from openpyxl import Workbook
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="openpyxl required") from exc
    if user_id:
        try:
            UUID(user_id)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="invalid user_id") from exc
    start, end = _period_bounds(preset, date_from, date_to)
    try:
        with connection() as conn:
            team_key = work_analytics.resolve_team(user.roles, team)
            summaries = {
                team_key: work_analytics.team_summary(
                    conn,
                    user.roles,
                    start=start,
                    end=end,
                    team=team_key,
                    viewer_id=user.user_id,
                )
            }
            breakdown = (
                work_analytics.ss_breakdown(
                    conn,
                    user.roles,
                    grain=grain,
                    year=year,
                    month=month,
                    user_id=user_id,
                )
                if team_key == work_analytics.TEAM_SS
                else None
            )
            causes = (
                work_analytics.collection_root_cause_breakdown(
                    conn, user.roles, year=year
                )
                if team_key == work_analytics.TEAM_COLLECTION
                else None
            )
            dead = (
                work_analytics.collection_dead_root_causes(conn, user.roles)
                if team_key == work_analytics.TEAM_COLLECTION
                else None
            )
    except (PermissionError, ValueError) as exc:
        raise _analytics_error(exc) from exc
    workbook = Workbook(write_only=True)
    for title, headers, rows in export_tables(
        summaries, breakdown=breakdown, causes=causes, dead=dead
    ):
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
            "Content-Disposition": f"attachment; filename={EXPORT_FILENAMES[team_key]}"
        },
    )


@router.get("/team/{user_id}")
def team_user(
    user_id: str,
    preset: str | None = Query("month"),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    team: str | None = Query(None),
    user: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

    try:
        UUID(user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="User not found") from exc
    start, end = _period_bounds(preset, date_from, date_to)
    try:
        with connection() as conn:
            detail = work_analytics.user_detail(
                conn, user.roles, user_id, start=start, end=end, team=team
            )
    except (PermissionError, ValueError) as exc:
        raise _analytics_error(exc) from exc
    if not detail:
        raise HTTPException(status_code=404, detail="User not found")
    return _ser(detail)

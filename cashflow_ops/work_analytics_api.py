"""Team work-analytics HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_ANALYTICS_VIEWER,
    ROLE_OPS_ADMIN,
    ROLE_SECOND_SUBMISSION_LEAD,
    ROLE_SUB_ADMIN,
    ROLE_SUPER,
    AuthUser,
    get_current_user,
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
    user: AuthUser = Depends(get_current_user),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, work_analytics

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

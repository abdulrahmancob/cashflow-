"""CPT payer guide HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_FINANCE,
    ROLE_OPS_ADMIN,
    ROLE_PIU,
    ROLE_POSTING,
    ROLE_SUBMISSION,
    ROLE_SUB_ADMIN,
    ROLE_SUPER,
    AuthUser,
    require_roles,
)

router = APIRouter(prefix="/cpt-guide", tags=["cpt-guide"])

VIEW_ROLES = (
    ROLE_POSTING,
    ROLE_SUPER,
    ROLE_FINANCE,
    ROLE_SUBMISSION,
    ROLE_OPS_ADMIN,
    ROLE_SUB_ADMIN,
    ROLE_PIU,
)
EDIT_ROLES = (ROLE_POSTING, ROLE_SUPER, ROLE_SUBMISSION, ROLE_OPS_ADMIN, ROLE_SUB_ADMIN)


def _ser(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _ser(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_ser(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, UUID):
        return str(obj)
    if hasattr(obj, "hex"):
        return str(obj)
    return obj


class GuidePatch(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    display_name: str | None = None
    match_patterns: list[str] | None = None
    timed_codes_policy: str | None = None
    estim_code: str | None = None
    strapping_policy: str | None = None
    reeval_policy: str | None = None
    preferred_codes: list[str] | None = None
    highly_preferred_codes: list[str] | None = None
    do_not_use: list[str] | None = None
    reimbursement_hint: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class GuideCreate(GuidePatch):
    display_name: str = Field(min_length=1)
    guide_key: str | None = None


@router.get("")
@router.get("/")
def list_guides(
    q: str | None = None,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, cpt_guide

    with connection() as conn:
        rows = cpt_guide.list_current(conn, q=q)
    return _ser({"items": rows, "count": len(rows)})


@router.get("/{guide_id}")
def get_guide(
    guide_id: str,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, cpt_guide

    with connection() as conn:
        row = cpt_guide.get_guide(conn, guide_id)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        history = cpt_guide.list_history(conn, guide_id)
    return _ser({"item": row, "history": history})


@router.post("")
def create_guide(
    body: GuideCreate,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, cpt_guide

    payload = body.model_dump(exclude_unset=True)
    reason = payload.pop("reason")
    try:
        with connection() as conn:
            row = cpt_guide.create_guide(
                conn, payload, actor_user_id=user.user_id, reason=reason
            )
            from cashflow_ops.activity_api import log_write

            log_write(
                conn,
                user,
                action="created",
                area="cpt_guide",
                entity_type="guide",
                entity_id=str(row.get("guide_id") or row.get("payer_guide_id") or ""),
                entity_label=f"{row.get('display_name') or 'Guide'} · CPT Guide",
                after=row,
                fallback=f"Created CPT guide {row.get('display_name') or ''}".strip(),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"item": row})


@router.patch("/{guide_id}")
def patch_guide(
    guide_id: str,
    body: GuidePatch,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import connection, cpt_guide

    payload = body.model_dump(exclude_unset=True)
    reason = payload.pop("reason")
    try:
        with connection() as conn:
            before = cpt_guide.get_guide(conn, guide_id) or {}
            row = cpt_guide.apply_guide_edit(
                conn,
                guide_id,
                payload,
                actor_user_id=user.user_id,
                reason=reason,
            )
            from cashflow_ops.activity_api import log_write

            log_write(
                conn,
                user,
                action="updated",
                area="cpt_guide",
                entity_type="guide",
                entity_id=guide_id,
                entity_label=f"{row.get('display_name') or before.get('display_name') or 'Guide'} · CPT Guide",
                before=before,
                after=row,
                keys=list(payload.keys()),
                fallback="Updated CPT guide",
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"item": row})

"""Collection lookup catalog HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cashflow_ops.security import (
    ROLE_COLLECTOR,
    ROLE_FINANCE,
    ROLE_OPS_ADMIN,
    ROLE_PIU,
    ROLE_POSTING,
    ROLE_SUB_ADMIN,
    ROLE_SUPER,
    AuthUser,
    require_roles,
)

router = APIRouter(prefix="/collection", tags=["collection"])

VIEW_ROLES = (
    ROLE_POSTING,
    ROLE_SUPER,
    ROLE_FINANCE,
    ROLE_COLLECTOR,
    ROLE_OPS_ADMIN,
    ROLE_SUB_ADMIN,
    ROLE_PIU,
)
EDIT_ROLES = (ROLE_OPS_ADMIN, ROLE_SUPER, ROLE_SUB_ADMIN)


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


class LookupCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=200)
    sort_order: int | None = None


class LookupPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    sort_order: int | None = None
    active: bool | None = None


def _activity(conn, user: AuthUser, **kwargs: Any) -> None:
    from cashflow_db.repository import portal_activity

    portal_activity.record_from_diff(conn, actor_user_id=user.user_id, **kwargs)


@router.get("/lookups")
def list_lookups(
    kind: str | None = None,
    include_inactive: bool = False,
    _: AuthUser = Depends(require_roles(*VIEW_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import collection, connection

    try:
        with connection() as conn:
            items = collection.list_lookups(conn, kind=kind, include_inactive=include_inactive)
            by_kind = collection.lookups_by_kind(conn, include_inactive=include_inactive)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"items": items, "by_kind": by_kind, "count": len(items)})


@router.post("/lookups")
def create_lookup(
    body: LookupCreate,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import collection, connection

    try:
        with connection() as conn:
            row = collection.create_lookup(
                conn,
                kind=body.kind,
                label=body.label,
                sort_order=body.sort_order,
                actor_id=user.user_id,
            )
            _activity(
                conn,
                user,
                action="created",
                area="collection",
                entity_type="collection_lookup",
                entity_id=str(row.get("lookup_id") or body.label),
                entity_label=f"Lookup · {body.kind} · {body.label}",
                after=row,
                fallback=f"Added {body.kind} value {body.label}",
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"item": row})


@router.patch("/lookups/{lookup_id}")
def patch_lookup(
    lookup_id: str,
    body: LookupPatch,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import collection, connection

    payload = body.model_dump(exclude_unset=True)
    try:
        with connection() as conn:
            before = collection.get_lookup(conn, lookup_id) or {}
            row = collection.update_lookup(
                conn,
                lookup_id,
                label=payload.get("label"),
                sort_order=payload.get("sort_order"),
                active=payload.get("active"),
                actor_id=user.user_id,
            )
            _activity(
                conn,
                user,
                action="updated",
                area="collection",
                entity_type="collection_lookup",
                entity_id=lookup_id,
                entity_label=f"Lookup · {row.get('kind')} · {row.get('label') or lookup_id}",
                before=before,
                after=row,
                keys=["label", "sort_order", "active"],
                fallback="Updated collection lookup",
            )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _ser({"item": row})


@router.delete("/lookups/{lookup_id}")
def delete_lookup(
    lookup_id: str,
    user: AuthUser = Depends(require_roles(*EDIT_ROLES)),
) -> dict[str, Any]:
    from cashflow_db.repository import collection, connection

    with connection() as conn:
        before = collection.get_lookup(conn, lookup_id)
        deleted = collection.delete_lookup(conn, lookup_id)
        if deleted and before:
            _activity(
                conn,
                user,
                action="deleted",
                area="collection",
                entity_type="collection_lookup",
                entity_id=lookup_id,
                entity_label=f"Lookup · {before.get('kind')} · {before.get('label') or lookup_id}",
                before=before,
                fallback=f"Deleted {before.get('kind')} value {before.get('label') or lookup_id}",
            )
    if not deleted:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}

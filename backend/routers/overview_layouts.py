"""Per-function default Overview layouts.

Stores one shared default layout bundle per `function_id`. New analysts (or
anyone who hasn't customized their local layout) load this default the first
time they open a function's Overview tab. Anyone with edit access can
overwrite the default by clicking "Save as Function Default" in the
Overview toolbar.

Persistence is in-memory (dict), consistent with the rest of the workbench
— a backend restart resets these defaults. Re-save once after restart and
they're restored.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from models.schemas import OverviewLayout, OverviewLayoutSave
from routers.auth import get_current_user

router = APIRouter()

# function_id -> bundle dict (the JSON shape exported by the frontend's
# Export button). We store as plain dicts so the schema can evolve client-
# side without requiring backend migrations.
# ── layout persistence ─────────────────────────────────────────────────────
# Keyed by function_id rather than a generated id: there is one default
# layout per function, so the function *is* the identity.
_LAYOUT_ENTITY = "layout"


def load_layout(function_id: str) -> dict[str, Any] | None:
    from services import entity_store

    return entity_store.get(_LAYOUT_ENTITY, function_id)


def store_layout(function_id: str, bundle: dict[str, Any]) -> None:
    from services import entity_store

    # `id` is what list_all reads back as the key, so it has to be present.
    entity_store.put(_LAYOUT_ENTITY, function_id, {**bundle, "id": function_id})


def remove_layout(function_id: str) -> None:
    from services import entity_store

    entity_store.delete(_LAYOUT_ENTITY, function_id)


@router.get("/{function_id}", response_model=OverviewLayout)
async def get_default_layout(function_id: str, _: str = Depends(get_current_user)):
    """Return the saved default layout for a function. 404 if none saved."""
    bundle = load_layout(function_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="No default layout saved for this function.")
    bundle.pop("id", None)
    return OverviewLayout(function_id=function_id, **bundle)


@router.put("/{function_id}", response_model=OverviewLayout)
async def save_default_layout(
    function_id: str,
    body: OverviewLayoutSave,
    user: str = Depends(get_current_user),
):
    """Save (overwrite) the default layout for a function."""
    bundle = {
        "layout": [item.model_dump() for item in body.layout],
        "hidden": body.hidden.model_dump(),
        "text_cards": [tc.model_dump() for tc in body.text_cards],
        "saved_by": user,
        "saved_at": body.saved_at,
    }
    store_layout(function_id, bundle)
    return OverviewLayout(function_id=function_id, **bundle)


@router.delete("/{function_id}")
async def delete_default_layout(function_id: str, _: str = Depends(get_current_user)):
    """Clear the default layout for a function."""
    remove_layout(function_id)
    return {"ok": True}

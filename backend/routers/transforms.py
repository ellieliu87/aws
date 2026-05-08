"""Transforms router — the ETL step between a Data Source and a Model.

A Transform represents a recipe (today: a read-only Python snippet) that
reads rows from one or more Data Sources, performs filtering / joining /
feature engineering, and materializes the result as a Dataset that
downstream model nodes consume.

For the demo the recipe is informational; pack-shipped Transforms point
at a pre-staged Dataset via `output_dataset_id` and "running" the
transform on the canvas reduces to feeding that dataset into the next
node. The recipe + parameters are surfaced to the analyst so they can
read what the transform does without inventing a separate UI for ETL.
"""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from models.schemas import Transform
from packs import is_pack_visible
from routers.auth import get_current_user, get_current_user_groups

router = APIRouter()
log = logging.getLogger("cma.transforms")

_TRANSFORMS: dict[str, Transform] = {}


# ── Pack-registered seed ingest ────────────────────────────────────────────
def _ingest_pack_transforms() -> None:
    """Pull transform attachments registered by domain packs into
    `_TRANSFORMS`. Idempotent."""
    from packs import transform_attachments

    now = datetime.utcnow().isoformat() + "Z"
    for s in transform_attachments():
        if s["transform_id"] in _TRANSFORMS:
            continue
        try:
            t = Transform(
                id=s["transform_id"],
                function_id=s["function_id"],
                name=s["name"],
                description=s.get("description"),
                input_data_source_ids=list(s.get("input_data_source_ids") or []),
                output_dataset_id=s.get("output_dataset_id"),
                recipe_python=s.get("recipe_python"),
                parameters=list(s.get("parameters") or []),
                source="pack",
                pack_id=s.get("pack_id"),
                created_at=now,
            )
            _TRANSFORMS[t.id] = t
        except Exception as e:
            log.error("[transforms] could not ingest pack transform %s: %s", s.get("transform_id"), e)


# ── Routes ──────────────────────────────────────────────────────────────────
@router.get("", response_model=list[Transform])
async def list_transforms(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
    groups: list[str] = Depends(get_current_user_groups),
):
    items = list(_TRANSFORMS.values())
    if function_id:
        items = [t for t in items if t.function_id is None or t.function_id == function_id]
    items = [t for t in items if is_pack_visible(t.pack_id, groups)]
    items.sort(key=lambda t: (t.source != "builtin", t.name))
    return items


@router.get("/{transform_id}", response_model=Transform)
async def get_transform(
    transform_id: str,
    _: str = Depends(get_current_user),
):
    t = _TRANSFORMS.get(transform_id)
    if not t:
        raise HTTPException(status_code=404, detail="Transform not found")
    return t

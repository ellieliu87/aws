"""Workspace router - returns the default analytical views for a business function."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent.skill_loader import list_skills
from agent.tools import reset_request_context, set_request_context
from cof.orchestrator import AsyncOrchestrator
from models.schemas import WorkspaceData
from routers.auth import get_current_user
from routers.plots import _PLOTS
from services.workspace_data import get_workspace

router = APIRouter()
log = logging.getLogger("cma.workspace")

# Reuse the chat router's orchestrator so skill lookup / reload behavior
# is shared. Importing it lazily inside the handler avoids a circular
# import (chat.py also imports things via this module's siblings).
_ORCH: AsyncOrchestrator | None = None


def _orch() -> AsyncOrchestrator:
    global _ORCH
    if _ORCH is None:
        _ORCH = AsyncOrchestrator()
    return _ORCH


# ── /{function_id} — workspace metadata + insights from a skill ────────────
@router.get("/{function_id}", response_model=WorkspaceData)
async def get_function_workspace(
    function_id: str,
    _: str = Depends(get_current_user),
):
    data = get_workspace(function_id)
    if not data:
        from models.schemas import WorkspaceData
        data = WorkspaceData(
            function_id=function_id,
            function_name=function_id,
            kpis=[], charts=[], tables=[], insights=[],
        )
    return data


# ── Agent-generated Overview insights ──────────────────────────────────────
class InsightsRequest(BaseModel):
    """Choose which skill to run; defaults to the built-in `overview-insights`.
    The skill must accept `[Context]` carrying a digest of pinned tiles."""
    skill_id: str = Field(default="overview-insights")


class InsightsResponse(BaseModel):
    skill_id: str
    skill_name: str
    markdown: str
    generated_at: str  # ISO-8601 UTC
    pinned_tile_count: int


def _format_pinned_digest(function_id: str) -> tuple[str, int]:
    """Build a single-string digest of every pinned tile (kpi/chart/table)
    for this function — id, name, type, and any value/preview hint we can
    surface cheaply. The skill's prompt reads this from [Context]."""
    pinned = [p for p in _PLOTS.values() if p.function_id == function_id and p.pinned_to_overview]
    pinned.sort(key=lambda p: (p.tile_type, p.name))
    if not pinned:
        return ("Pinned tiles: (none)", 0)

    lines = [f"Pinned tiles ({len(pinned)}):"]
    for p in pinned:
        if p.tile_type == "kpi":
            agg = p.kpi_aggregation or "sum"
            field = p.kpi_field or "?"
            unit = (p.kpi_prefix or "") + (p.kpi_suffix or "")
            sub = f", sublabel={p.kpi_sublabel}" if p.kpi_sublabel else ""
            lines.append(
                f"- KPI '{p.name}' (id={p.id}): {agg}({field}) "
                f"unit='{unit or '—'}', scale={p.kpi_scale}{sub}"
            )
        elif p.tile_type == "table":
            cols = ", ".join(p.table_columns or [])[:200] or "(default columns)"
            lines.append(
                f"- TABLE '{p.name}' (id={p.id}): columns=[{cols}]"
                f"{', sort=' + p.table_default_sort if p.table_default_sort else ''}"
            )
        else:  # plot
            ys = ", ".join(p.y_fields or [])
            lines.append(
                f"- {p.chart_type.upper()} '{p.name}' (id={p.id}): "
                f"x={p.x_field or '?'}, y=[{ys or '?'}], agg={p.aggregation}"
            )
    return ("\n".join(lines), len(pinned))


@router.get("/{function_id}/insights/skills")
async def list_insight_skills(function_id: str, _: str = Depends(get_current_user)):
    """Return every loaded skill so the Overview can populate a picker.
    `function_id` is unused today but kept in the path so we can later
    filter by function-pack visibility."""
    out = []
    for s in list_skills():
        out.append({
            "id": s.name,
            "name": s.name.replace("-", " ").title(),
            "description": s.description,
            "icon": s.icon or "sparkles",
            "color": s.color or "#004977",
            "source": s.source,
            "pack_id": s.pack_id,
        })
    return out


@router.post("/{function_id}/insights", response_model=InsightsResponse)
async def generate_insights(
    function_id: str,
    body: InsightsRequest,
    _: str = Depends(get_current_user),
):
    """Run the requested skill against this function's pinned-tile digest
    and return a markdown insight brief."""
    workspace = get_workspace(function_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found for this function")

    orch = _orch()
    if not orch.available:
        raise HTTPException(
            status_code=503,
            detail=orch.init_error or "LLM not configured. Set OPENAI_API_KEY in backend/.env and restart.",
        )

    skill_id = (body.skill_id or "overview-insights").strip()
    skill = orch.get_skill(skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_id}' is not loaded.")

    digest, count = _format_pinned_digest(function_id)
    extra_context = (
        f"function_id: {function_id}\n"
        f"function_name: {workspace.function_name}\n"
        f"{digest}"
    )
    user_message = (
        "Write 3–5 short insight bullets per your system prompt's rules, "
        "using only values from [Context] (call get_tile_preview if you "
        "need a closer read on a specific tile)."
    )

    # Push the function id into the request context so any tool the skill
    # calls (get_workspace, etc.) sees it without the model having to
    # echo it back.
    token = set_request_context({"function_id": function_id, "entity_kind": None, "entity_id": None})
    try:
        text, _trace = await orch.chat_specialist_with_trace(
            skill_id, user_message, extra_context=extra_context,
        )
    except Exception as e:
        log.error("Insights call failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Skill `{skill_id}` failed: {e}")
    finally:
        reset_request_context(token)

    return InsightsResponse(
        skill_id=skill_id,
        skill_name=skill.name.replace("-", " ").title(),
        markdown=text or "_(empty response)_",
        generated_at=datetime.now(timezone.utc).isoformat(),
        pinned_tile_count=count,
    )

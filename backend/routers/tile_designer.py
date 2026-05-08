"""Tile Designer router — LLM endpoint that interprets a natural-language
narrative and returns PlotConfig blueprints the analyst can accept and
add to their Reporting dashboard in one shot."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from agent.tools import reset_request_context, set_request_context
from cof.orchestrator import AsyncOrchestrator
from routers.auth import get_current_user

router = APIRouter()
log = logging.getLogger("cma.tile_designer")

_ORCH: AsyncOrchestrator | None = None


def _orch() -> AsyncOrchestrator:
    global _ORCH
    if _ORCH is None:
        _ORCH = AsyncOrchestrator()
    return _ORCH


class TileDesignerRequest(BaseModel):
    function_id: str
    narrative: str = Field(min_length=4)
    dataset_id: str | None = None


class TileBlueprint(BaseModel):
    tile_type: str = "plot"
    name: str = ""
    chart_type: str | None = "line"
    x_field: str | None = None
    y_fields: list[str] = Field(default_factory=list)
    aggregation: str = "none"
    filters: list[dict] = Field(default_factory=list)
    kpi_field: str | None = None
    kpi_aggregation: str | None = None
    kpi_prefix: str | None = None
    kpi_suffix: str | None = None
    description: str | None = None
    python_snippet: str | None = None


class TileDesignerResponse(BaseModel):
    tiles: list[TileBlueprint]
    narrative_summary: str
    dataset_id: str | None = None


_SCHEMA_HINT = (
    "Dataset schema (long format): scenario, snap_date, variable_name, variable_value, segment, origin\n"
    "Known variable_name values: FEDFUNDS, GDP, UNEMPLOYMENT, CPI, M2, CORP_PROFIT, UST10Y, HOUSING_STARTS\n"
    "Scenarios: Baseline_2026, BHCB_2026, BHCS_2026, FedSA_2026\n"
)


@router.post("/generate", response_model=TileDesignerResponse)
async def generate_tiles(
    body: TileDesignerRequest,
    _: str = Depends(get_current_user),
):
    """Run the reporting-tile-designer skill against the analyst's narrative
    and return a set of tile blueprints ready for the frontend to preview
    and create via POST /api/plots."""
    orch = _orch()
    if not orch.available:
        raise HTTPException(
            status_code=503,
            detail=orch.init_error or "LLM not configured. Set OPENAI_API_KEY in backend/.env and restart.",
        )

    skill = orch.get_skill("reporting-tile-designer")
    if not skill:
        raise HTTPException(status_code=404, detail="Skill 'reporting-tile-designer' not loaded.")

    extra_context = (
        f"function_id: {body.function_id}\n"
        f"dataset_id: {body.dataset_id or 'macro_reporting_sample'}\n"
        f"{_SCHEMA_HINT}"
    )

    user_message = (
        f"Design dashboard tiles for this analyst request:\n\n{body.narrative}\n\n"
        "Return ONLY a JSON object — no markdown fences — with keys:\n"
        '  "tiles": array of tile objects\n'
        '  "narrative_summary": one sentence describing what was designed\n'
        "Each tile must have tile_type (plot/table/kpi), name, and appropriate fields.\n"
        "For plots: chart_type, x_field, y_fields, aggregation, filters.\n"
        "For KPIs: tile_type='kpi', kpi_field='variable_value', kpi_aggregation, kpi_prefix/suffix, x_field, filters.\n"
        "Also add python_snippet (1–2 line pandas code) for each tile."
    )

    token = set_request_context({"function_id": body.function_id, "entity_kind": None, "entity_id": None})
    try:
        text, _trace = await orch.chat_specialist_with_trace(
            "reporting-tile-designer", user_message, extra_context=extra_context,
        )
    except Exception as e:
        log.error("Tile designer skill failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Skill failed: {e}")
    finally:
        reset_request_context(token)

    # Strip markdown code fences if the model wrapped in ```json ... ```
    clean = (text or "").strip()
    if clean.startswith("```"):
        parts = clean.split("```", 2)
        body_part = parts[1] if len(parts) > 1 else ""
        if body_part.startswith("json"):
            body_part = body_part[4:]
        clean = body_part.rsplit("```", 1)[0].strip()

    try:
        data = json.loads(clean)
        raw_tiles = data.get("tiles", [])
        tiles: list[TileBlueprint] = []
        for t in raw_tiles:
            try:
                tiles.append(TileBlueprint(**t))
            except Exception as te:
                log.warning("Skipped malformed tile from LLM: %s — %s", te, t)
        summary: str = data.get("narrative_summary", body.narrative[:120])
    except Exception as e:
        log.warning("Failed to parse tile designer JSON: %s\nRaw: %.500s", e, text)
        tiles = []
        summary = "Could not parse tile recommendations — please rephrase your request."

    return TileDesignerResponse(
        tiles=tiles,
        narrative_summary=summary,
        dataset_id=body.dataset_id,
    )

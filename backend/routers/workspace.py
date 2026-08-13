"""Workspace router - returns the default analytical views for a business function."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import pandas as pd

from agent.skill_loader import list_skills
from agent.tools import reset_request_context, set_request_context
from cof.orchestrator import AsyncOrchestrator
from models.schemas import WorkspaceData
from routers.auth import get_current_user
from routers.datasets import _read_dataframe, _resolve_path, load_dataset
from routers.plots import _PLOTS, _aggregate, _apply_filters, _compute_kpi
from routers.scenarios import load_run
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
class InsightsTextCard(BaseModel):
    id: str
    body: str


class InsightsRequest(BaseModel):
    """Choose which skill to run; defaults to the built-in `overview-insights`.
    The skill must accept `[Context]` carrying a digest of pinned tiles + the
    analyst's text cards (free-form markdown commentary they typed onto the
    dashboard). Frontend passes `text_cards` from its localStorage state so
    the agent can read everything currently rendered on the Overview."""
    skill_id: str = Field(default="overview-insights")
    text_cards: list[InsightsTextCard] = Field(default_factory=list)


class InsightsResponse(BaseModel):
    skill_id: str
    skill_name: str
    markdown: str
    generated_at: str  # ISO-8601 UTC
    pinned_tile_count: int


def _tile_dataframe(p) -> pd.DataFrame | None:
    """Resolve the dataframe a tile's preview would read — same path as
    plots.preview_plot. Returns the filtered df, or None if the tile has
    no live source. Tries dataset_id first, then run_id."""
    df = None
    if p.dataset_id:
        d = load_dataset(p.dataset_id)
        if d and d.source_kind == "upload" and d.file_path and d.file_format:
            try:
                df = _read_dataframe(_resolve_path(d), d.file_format)
            except Exception:
                df = None
    if df is None and p.run_id:
        run = load_run(p.run_id)
        if run and run.series:
            try:
                df = pd.DataFrame(run.series)
            except Exception:
                df = None
    if df is None:
        return None
    try:
        return _apply_filters(df, p.filters)
    except Exception:
        return df


def _kpi_line(p) -> str:
    """KPI digest line — includes the actual computed value, not just config."""
    agg = p.kpi_aggregation or "sum"
    field = p.kpi_field or "?"
    sub = f", sublabel='{p.kpi_sublabel}'" if p.kpi_sublabel else ""
    df = _tile_dataframe(p)
    if df is None or df.empty:
        return f"- KPI '{p.name}': {agg}({field}) — value: (no data){sub}"
    payload = _compute_kpi(df, p)
    if not payload or payload.get("display") in (None, "", f"{p.kpi_prefix}—{p.kpi_suffix}"):
        return f"- KPI '{p.name}': {agg}({field}) — value: (no data){sub}"
    return (
        f"- KPI '{p.name}': {payload['display']} "
        f"[{agg}({field}), n={len(df)}]{sub}"
    )


def _chart_line(p) -> str:
    """Plot digest line — includes the chart shape (first/last/min/max for time
    series, top categories for grouped charts) so the agent can describe trend."""
    ys = ", ".join(p.y_fields or [])
    head = (
        f"- {p.chart_type.upper()} '{p.name}': x={p.x_field or '?'}, "
        f"y=[{ys or '?'}], agg={p.aggregation}"
    )
    df = _tile_dataframe(p)
    if df is None or df.empty:
        return f"{head} — shape: (no data)"
    try:
        if p.aggregation and p.aggregation != "none":
            df = _aggregate(df, p.x_field, p.y_fields, p.aggregation)
    except Exception:
        pass

    x = p.x_field
    y = (p.y_fields or [None])[0]
    if not x or not y or x not in df.columns or y not in df.columns:
        return f"{head} — rows={len(df)}"

    s = pd.to_numeric(df[y], errors="coerce")
    valid = df.assign(__y=s).dropna(subset=["__y"])
    if valid.empty:
        return f"{head} — rows={len(df)}, no numeric y values"

    if p.chart_type in ("line", "area") and pd.api.types.is_string_dtype(df[x]):
        # Treat snap_date strings as ordered
        sorted_df = valid.sort_values(x)
        first = sorted_df.iloc[0]
        last = sorted_df.iloc[-1]
        ymax = sorted_df.loc[sorted_df["__y"].idxmax()]
        ymin = sorted_df.loc[sorted_df["__y"].idxmin()]
        return (
            f"{head} — n={len(sorted_df)}, "
            f"first({first[x]})={first['__y']:.4g}, "
            f"last({last[x]})={last['__y']:.4g}, "
            f"peak({ymax[x]})={ymax['__y']:.4g}, "
            f"trough({ymin[x]})={ymin['__y']:.4g}"
        )

    # Bar / categorical: rank by y
    sorted_df = valid.sort_values("__y", ascending=False).head(8)
    pairs = ", ".join(f"{r[x]}={r['__y']:.4g}" for _, r in sorted_df.iterrows())
    return f"{head} — top: {pairs}" + (f"  (and {len(valid) - len(sorted_df)} more)" if len(valid) > len(sorted_df) else "")


def _table_line(p) -> str:
    """Table digest line — head N rows so the agent can quote actual numbers."""
    cols = ", ".join(p.table_columns or [])[:200] or "(default columns)"
    sort = f", sort={p.table_default_sort}" if p.table_default_sort else ""
    head = f"- TABLE '{p.name}': columns=[{cols}]{sort}"
    df = _tile_dataframe(p)
    if df is None or df.empty:
        return f"{head} — rows: (no data)"
    sample = df.head(6)
    rows = []
    for _, r in sample.iterrows():
        kv = ", ".join(f"{c}={r[c]}" for c in sample.columns[:6])
        rows.append(f"    {kv}")
    suffix = f"  (+ {len(df) - len(sample)} more rows)" if len(df) > len(sample) else ""
    return f"{head} — rows={len(df)}, sample:\n" + "\n".join(rows) + suffix


def _format_pinned_digest(function_id: str) -> tuple[str, int]:
    """Build a digest of every pinned tile + the live values currently
    rendered on the dashboard. The agent reads this from [Context] and
    quotes specific numbers. Cheap to compute (each dataset is read once,
    pandas is fine for the dashboard scale)."""
    pinned = [p for p in _PLOTS.values() if p.function_id == function_id and p.pinned_to_overview]
    pinned.sort(key=lambda p: (p.tile_type, p.name))
    if not pinned:
        return ("Pinned tiles: (none — the analyst hasn't pinned anything yet)", 0)

    lines = [f"Pinned tiles ({len(pinned)}) — these are the cards currently visible on the Overview:"]
    for p in pinned:
        try:
            if p.tile_type == "kpi":
                lines.append(_kpi_line(p))
            elif p.tile_type == "table":
                lines.append(_table_line(p))
            else:
                lines.append(_chart_line(p))
        except Exception as e:
            log.warning("digest line failed for tile %s: %s", p.id, e)
            lines.append(f"- {p.tile_type.upper()} '{p.name}' (digest unavailable: {e})")
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
    and return a markdown insight brief.

    Note: `get_workspace()` only returns for the four built-in functions —
    user-created workspaces (Deposit CCAR Process, etc.) aren't in that
    registry. The insights endpoint doesn't actually need the legacy
    WorkspaceData snapshot; the digest comes from `_PLOTS` (pinned tiles)
    plus the analyst's text cards. Fall back to a synthetic name so a
    fresh workspace can still generate insights as soon as it has any
    pinned tile or note."""
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

    workspace = get_workspace(function_id)
    function_name = workspace.function_name if workspace else function_id.replace("_", " ").title()

    digest, count = _format_pinned_digest(function_id)
    parts = [
        f"function_id: {function_id}",
        f"function_name: {function_name}",
        digest,
    ]
    if body.text_cards:
        notes = ["", f"Analyst notes ({len(body.text_cards)}) — free-form commentary the analyst typed onto the dashboard:"]
        for i, tc in enumerate(body.text_cards, 1):
            trimmed = tc.body.strip()
            if not trimmed:
                continue
            # Cap each note so a long note can't dominate the prompt.
            if len(trimmed) > 600:
                trimmed = trimmed[:600] + "…"
            notes.append(f"[Note {i}]\n{trimmed}")
        if len(notes) > 2:
            parts.append("\n".join(notes))
    extra_context = "\n".join(parts)
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

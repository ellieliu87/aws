"""Chat router — routes analyst queries to a real LLM-backed agent team.

Connection: the orchestrator constructs ``AsyncOpenAI()`` with no
arguments and lets the SDK auto-resolve the endpoint from
``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` (or the corporate COF SDK fork
running inside the Capital One network — neither env var is needed
there). When the SDK can't reach an LLM, every chat call surfaces the
upstream error verbatim (NOT a mock).

The backend pre-routes via the page context the frontend ships with each
message (tab + entity_kind + entity_id) so we usually go straight to the right
specialist without invoking the orchestrator's delegate-tools loop. When the
context isn't enough, we fall back to the orchestrator agent.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

from agent.skill_loader import AgentSkill, list_skills
from agent.tools import reset_request_context, set_request_context
from cof.orchestrator import AsyncOrchestrator
from models.schemas import (
    AgentInfo,
    ChatAction,
    ChatMessage,
    ChatResponse,
)
from routers.auth import get_current_user

router = APIRouter()
log = logging.getLogger("cma.chat")

# Single shared orchestrator instance (skills reload from disk on demand)
_ORCH = AsyncOrchestrator()


_DATA_QUALITY_PHRASES = (
    "data quality", "quality audit", "quality check",
    "data audit", "outlier", "anomal", "missing values",
    "null %", "dtype drift", "constant column", "data drift",
)


def _is_quality_intent(msg: str) -> bool:
    """True only when the analyst explicitly asked for a quality check.
    Substring 'quality' alone is too loose — phrases like 'skip
    data-quality' inside an unrelated message would falsely trigger."""
    return any(kw in msg for kw in _DATA_QUALITY_PHRASES)


# ── Routing (frontend context → specialist agent_id) ─────────────────────
def _route(req: ChatMessage) -> str:
    msg = (req.message or "").lower()

    if "error:" in msg or "traceback" in msg or "failed:" in msg:
        return "troubleshooter"
    if req.entity_kind == "kpi":
        return "kpi-explainer"
    if req.entity_kind == "dataset":
        # data-quality is reserved for the explicit "Check data quality"
        # button. The Sparkles "Explain dataset" icon should describe
        # what's in the dataset — that's the dedicated `data-explainer`
        # specialist, which only calls `get_dataset_preview` and writes a
        # structured shape/columns/use-cases brief.
        if _is_quality_intent(msg):
            return "data-quality"
        return "data-explainer"
    if req.entity_kind == "scenario":
        # Scenarios always go to the macro narrative writer — never to
        # data-quality, even if the message text mentions data-quality
        # in passing.
        return "macro-economist"
    if req.entity_kind == "model":
        return "model-explainer"
    if req.entity_kind == "transform":
        return "transform-explainer"
    if req.entity_kind == "run":
        return "run-troubleshooter" if ("fail" in msg or "error" in msg) else "model-explainer"
    if req.entity_kind == "tile":
        # Tune intent → plot-tuner (mutates the persisted spec).
        # Anything else (default Sparkles click) → tile-explainer (explain).
        if any(k in msg for k in (
            "tune", "filter", "sort", "rank", "limit", "change", "modify", "switch",
            "color", "palette", "font", "label", "title", "axis", "rename",
            "ascend", "descend", "asc", "desc", "bar", "line", "pie", "area",
            "format", "legend", "style",
        )):
            return "plot-tuner"
        return "tile-explainer"
    if req.entity_kind == "analytic_def":
        # Self-serve Analytics chart cards — same plot-tuner toolkit applies.
        return "plot-tuner"
    if req.entity_kind == "workflow":
        return "workflow-validator"
    if req.tab == "data" and _is_quality_intent(msg):
        return "data-quality"
    if req.tab == "models" and "explain" in msg:
        return "model-explainer"
    if req.tab == "workflow":
        if "valid" in msg or "check" in msg or "make sense" in msg:
            return "workflow-validator"
        if "trouble" in msg or "fail" in msg or "error" in msg:
            return "run-troubleshooter"
        return "workflow-validator"
    if req.tab == "reporting":
        # If the user types a tune-style request without first clicking
        # Tune on a tile, route to plot-tuner so it can list available tiles
        # by name and ask which one to mutate. The default for non-tune
        # questions stays tile-explainer.
        if any(k in msg for k in (
            "tune", "filter", "sort", "rank", "limit", "change", "modify",
            "switch", "color", "palette", "font", "label", "title", "axis",
            "rename", "ascend", "descend", "asc", "desc", "bar", "line",
            "pie", "area", "format", "legend", "style",
        )):
            return "plot-tuner"
        return "tile-explainer"
    if req.tab == "analytics":
        # New self-serve analytics tab — no dedicated specialist yet, so route
        # to the orchestrator and let it pick or answer directly.
        return "orchestrator"
    return "orchestrator"


# ── Build context bundle for the LLM ─────────────────────────────────────
def _build_context(req: ChatMessage) -> str:
    parts: list[str] = []
    if req.function_id:
        parts.append(f"function_id: {req.function_id}")
    if req.tab:
        parts.append(f"tab: {req.tab}")
    if req.entity_kind:
        parts.append(f"entity_kind: {req.entity_kind}")
    if req.entity_id:
        parts.append(f"entity_id: {req.entity_id}")
    if req.context:
        parts.append(f"page_context: {req.context}")
    if req.payload:
        # Workflow validator passes nodes/edges in payload
        import json
        parts.append(f"payload: {json.dumps(req.payload, default=str)[:4000]}")
    return "\n".join(parts)


def _agent_meta(agent_id: str) -> tuple[str, str | None, str | None]:
    """Return (display_name, color, icon) for the responding agent."""
    skill = _ORCH.get_skill(agent_id)
    if skill:
        # If user uploaded their own version, show that name; otherwise use the skill's name
        name = skill.name
        return (name.replace("-", " ").title(), skill.color, skill.icon)
    return (agent_id.replace("-", " ").title(), None, None)


# ── Endpoints ─────────────────────────────────────────────────────────────
@router.get("/agents", response_model=list[AgentInfo])
async def list_agents(_: str = Depends(get_current_user)):
    """Return one AgentInfo per loaded skill — built-ins + user uploads."""
    out: list[AgentInfo] = []
    for s in list_skills():
        out.append(AgentInfo(
            id=s.name, name=s.name.replace("-", " ").title(),
            description=s.description,
            icon=s.icon or "sparkles",
            color=s.color or "#004977",
        ))
    return out


@router.post("/message", response_model=ChatResponse)
async def send_message(req: ChatMessage, _: str = Depends(get_current_user)):
    if not _ORCH.available:
        return ChatResponse(
            response=(
                "## LLM not configured\n\n"
                f"{_ORCH.init_error or 'LLM not reachable. Inside the corporate environment, no env vars are needed. Outside it, set OPENAI_API_KEY in backend/.env and restart.'}"
            ),
            agent_id="setup_required",
            agent_name="Setup Required",
            agent_color="#FF5C5C",
            agent_icon="alert-triangle",
        )

    # If the frontend pinned a specific agent (not the default orchestrator),
    # honour it directly rather than re-routing by context. This lets pack
    # agents selected via the chat panel's agent picker be invoked directly.
    if req.agent_id and req.agent_id != "orchestrator" and _ORCH.get_skill(req.agent_id):
        target = req.agent_id
    else:
        target = _route(req)
    ctx = _build_context(req)

    # Cap history to the last 10 turns (5 user-assistant pairs) so token use
    # stays bounded even if the panel has been open for a long time.
    history = [t.model_dump() for t in (req.history or [])][-10:]

    # Push the request's bound entity into a contextvar so the agent's
    # mutation tools (apply_filter, set_chart_type, …) can fall back to it
    # when the model forgets to echo target_id. Without this fallback the
    # plot-tuner often described changes without actually applying them.
    ctx_token = set_request_context({
        "entity_kind": req.entity_kind,
        "entity_id": req.entity_id,
        "function_id": req.function_id,
    })

    trace_dicts: list[dict] = []
    try:
        if target == "orchestrator":
            text = await _ORCH.chat_orchestrator(
                req.message, extra_context=ctx, history=history,
                function_id=req.function_id,
            )
        else:
            text, trace_dicts = await _ORCH.chat_specialist_with_trace(
                target, req.message, extra_context=ctx, history=history,
            )
    except Exception as e:
        log.error("Chat call failed: %s", e)
        return ChatResponse(
            response=f"## Agent error\n\n```\n{e}\n```\nCheck the backend log for the full traceback.",
            agent_id="troubleshooter",
            agent_name="Troubleshooter",
            agent_color="#FF5C5C",
            agent_icon="life-buoy",
        )
    finally:
        reset_request_context(ctx_token)

    name, color, icon = _agent_meta(target)

    # The Tile Tuner emits action chips by calling apply_tile_filter — surface
    # those as ChatAction items so the frontend renders clickable filter chips.
    actions: list[ChatAction] = []
    if target == "tile-tuner" and req.entity_id:
        # Ask the agent's tool log for filters it just appended (we keep it simple
        # by inspecting the tile's current filter list pre/post is overkill —
        # the agent returns chip suggestions in markdown text and the analyst
        # can also click the saved filters indicator on the tile).
        pass  # filter actions are written via the tool itself; see tools.py:apply_tile_filter

    from models.schemas import TraceStep
    trace_steps: list[TraceStep] = []
    for d in trace_dicts:
        try:
            trace_steps.append(TraceStep(**d))
        except Exception:
            pass

    return ChatResponse(
        response=text,
        agent_id=target,
        agent_name=name,
        agent_color=color,
        agent_icon=icon,
        actions=actions,
        trace=trace_steps,
    )


# ── Human feedback (thumbs up/down) ────────────────────────────────────────
# Captured per assistant message so the offline LLM-eval pipeline can pull
# annotations for relevance / correctness scoring. Stored in-memory today;
# wire to LangSmith / Langfuse / a JSONL log later by extending the
# `_FEEDBACK_LOG` writer.
class FeedbackRequest(BaseModel):
    message_id: str
    rating: Literal["up", "down"]
    agent_id: str | None = None
    agent_name: str | None = None
    user_message: str | None = None  # the analyst prompt that produced the response
    assistant_message: str | None = None  # the agent's reply text
    comment: str | None = Field(default=None, max_length=2000)
    function_id: str | None = None
    tab: str | None = None
    entity_kind: str | None = None
    entity_id: str | None = None


class FeedbackEntry(FeedbackRequest):
    rated_by: str
    rated_at: str  # ISO-8601 UTC


_FEEDBACK_LOG: list[FeedbackEntry] = []


@router.post("/feedback", response_model=FeedbackEntry)
async def submit_feedback(
    body: FeedbackRequest,
    user: str = Depends(get_current_user),
):
    entry = FeedbackEntry(
        **body.model_dump(),
        rated_by=user,
        rated_at=datetime.now(timezone.utc).isoformat(),
    )
    _FEEDBACK_LOG.append(entry)
    log.info(
        "[feedback] %s rated %s on msg=%s agent=%s",
        user, body.rating, body.message_id, body.agent_id or "?",
    )
    return entry


@router.get("/feedback", response_model=list[FeedbackEntry])
async def list_feedback(_: str = Depends(get_current_user)):
    """Pull recent feedback annotations for the eval pipeline."""
    return list(_FEEDBACK_LOG[-500:])  # cap response size


# Re-export the validation helper (kept for backwards compatibility with the
# scenarios.py /workflow-validate endpoint that imports from here).
from routers.chat_validation import validate_workflow_payload as _validate_workflow_payload  # noqa: F401

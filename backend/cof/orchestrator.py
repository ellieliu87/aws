"""COF AsyncOrchestrator — orchestrator agent + 7 specialist sub-agents.

Connection model — matches the oasia pattern: ``AsyncOpenAI()`` is always
constructed with **no arguments**. The openai SDK auto-resolves the endpoint
and credentials from the standard env vars (``OPENAI_BASE_URL``,
``OPENAI_API_KEY``) or from a Capital One COF SDK fork installed in the
corporate proxy environment. We never force ``base_url=`` ourselves, because
doing so can route around the corporate proxy in environments where it's
preconfigured.

If you're outside the corporate environment, set ``OPENAI_API_KEY`` (and
optionally ``OPENAI_BASE_URL``) in ``backend/.env`` — the SDK picks them up
automatically.

Each specialist is a CofBaseAgent loaded from its own SKILL.md file. The
orchestrator is itself a CofBaseAgent whose tools are `delegate_to_<name>`
function tools that forward queries to specialists.

Skills auto-reload from disk on every chat call so user-uploaded skills (saved
via Settings → Agent Skills) take effect without a server restart.
"""
from __future__ import annotations

import json
import logging
import os
import traceback
from typing import Any

from agent.skill_loader import (
    AgentSkill,
    BUILTIN_SKILLS_DIR,
    USER_SKILLS_DIR,
    load_all_skills,
)
from agent.tools import OPENAI_TOOLS, handle_tool_call
from cof.base_agent import CofBaseAgent, HAS_AGENTS_SDK

log = logging.getLogger("cma.cof.orchestrator")


# Fallback delegate list — only consulted when the orchestrator skill on
# disk doesn't declare a `sub_agents:` block. The canonical source is the
# orchestrator skill's frontmatter (so packs can extend the list by
# editing one markdown file rather than this code).
_DEFAULT_DELEGATE_AGENT_NAMES = [
    "kpi-explainer",
    "data-quality",
    "model-explainer",
    "workflow-validator",
    "run-troubleshooter",
    "tile-tuner",
    "troubleshooter",
]


def _norm(name: str) -> str:
    return name.replace("-", "_").lower()


def _build_delegate_tools(sub_agents: dict[str, CofBaseAgent], skills: dict[str, AgentSkill]):
    """Build delegate_to_<name> FunctionTools for the orchestrator."""
    from agents import FunctionTool

    tools = []
    for name, agent in sub_agents.items():
        skill = skills.get(name)
        description = (skill.description if skill else f"Delegate to the {name} specialist.")
        safe_name = name.replace("-", "_")

        def _make(captured: CofBaseAgent):
            async def _h(ctx, input: str) -> str:
                try:
                    args = json.loads(input) if input else {}
                except json.JSONDecodeError:
                    args = {}
                query = args.get("query", input or "")
                ctxt = args.get("context", "")
                try:
                    return await captured.chat(query, extra_context=ctxt)
                except Exception as e:
                    return json.dumps({"error": str(e), "traceback": traceback.format_exc()[-300:]})
            return _h

        tools.append(FunctionTool(
            name=f"delegate_to_{safe_name}",
            description=description,
            params_json_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Full query to forward to the specialist."},
                    "context": {"type": "string", "description": "Optional extra context (entity_id, function_id, payload summary)."},
                },
                "required": ["query"],
            },
            on_invoke_tool=_make(agent),
        ))
    return tools


class AsyncOrchestrator:
    """Single instance shared across the chat router."""

    def __init__(self):
        self._available = False
        self._error: str | None = None

        if not HAS_AGENTS_SDK:
            self._error = (
                "openai-agents SDK not installed. Run `uv sync` (or "
                "`pip install openai-agents`) and restart the backend."
            )
            log.warning(self._error)
            return

        # Note: we DO NOT gate on env vars here — matching the oasia pattern.
        # The corporate COF environment auto-resolves the endpoint + auth
        # without any explicit env vars, so refusing to initialize when
        # `COF_BASE_URL`/`OPENAI_API_KEY` are unset would defeat the proxy.
        # If the SDK can't actually reach an LLM, individual chat calls will
        # surface the failure with a clear message at request time.

        self._skills_mtime: float = 0.0
        self._reload_skills()

        if "orchestrator" not in self._sub_agents and not self._sub_agents:
            self._error = "No agent skills loaded — check backend/agent/skills/."
            log.error(self._error)
            return

        self._available = True
        log.info("AsyncOrchestrator ready with %d specialists", len(self._sub_agents))

    # ── Lifecycle ────────────────────────────────────────────────────────
    @staticmethod
    def _scan_skills_mtime() -> float:
        """Latest mtime across both skill directories (incl. dir mtime to catch deletes)."""
        latest = 0.0
        for d in (BUILTIN_SKILLS_DIR, USER_SKILLS_DIR):
            if not d.exists():
                continue
            try:
                latest = max(latest, d.stat().st_mtime)
            except OSError:
                pass
            for p in d.glob("*.md"):
                try:
                    latest = max(latest, p.stat().st_mtime)
                except OSError:
                    pass
        return latest

    def _maybe_reload(self) -> None:
        """Re-parse skills if any .md file changed since the last build."""
        try:
            current = self._scan_skills_mtime()
        except Exception as e:
            log.debug("mtime scan failed: %s", e)
            return
        if current > self._skills_mtime:
            log.info("Skill files changed — reloading agents")
            self._reload_skills()

    def _reload_skills(self) -> None:
        """Re-read every skill from disk; rebuild agents."""
        from agents import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI

        self._skills: dict[str, AgentSkill] = {
            _norm(s.name): s for s in load_all_skills().values()
        }
        # Per-function orchestrator Agent cache, invalidated on every reload.
        self._orch_agent_by_fn: dict[str, Any] = {}

        # Build a specialist for every loaded skill (except the orchestrator
        # itself). Playbook phases call these by skill_name via chat_specialist,
        # so any skill on disk — including user-uploaded ones — becomes runnable
        # without touching this file.
        self._sub_agents: dict[str, CofBaseAgent] = {}
        for key, skill in self._skills.items():
            if key == "orchestrator":
                continue
            short_name = skill.name  # canonical kebab-case name from the skill file
            self._sub_agents[short_name] = CofBaseAgent(skill, OPENAI_TOOLS, handle_tool_call)

        # Build orchestrator agent
        orch_skill = self._skills.get("orchestrator")
        if not orch_skill:
            self._orch_skill = None
            return

        # Canonical delegate pool comes from the orchestrator skill's
        # `sub_agents:` frontmatter (so adding a pack-scoped delegate is a
        # one-line edit to orchestrator.md, not a code change). Fall back to
        # the legacy list if the skill on disk has no sub_agents block.
        self._delegate_pool_names: list[str] = list(orch_skill.sub_agents) or list(
            _DEFAULT_DELEGATE_AGENT_NAMES
        )
        for short_name in self._delegate_pool_names:
            if short_name not in self._sub_agents:
                log.warning("Delegate skill not found: %s", short_name)

        # Match oasia exactly: AsyncOpenAI() with no arguments. The openai
        # SDK auto-discovers `OPENAI_BASE_URL` / `OPENAI_API_KEY` from the
        # environment, and the COF proxy resolves transparently when the
        # backend is running inside the corporate network.
        from agents import set_tracing_disabled
        set_tracing_disabled(True)
        self._client = AsyncOpenAI()
        self._orch_model = OpenAIChatCompletionsModel(
            model=orch_skill.model, openai_client=self._client,
        )
        self._orch_skill = orch_skill
        # Record the mtime snapshot so _maybe_reload skips work until something changes.
        self._skills_mtime = self._scan_skills_mtime()

    # ── Public ───────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self._available

    @property
    def init_error(self) -> str | None:
        return self._error

    def get_specialist(self, agent_id: str) -> CofBaseAgent | None:
        """Return a specialist agent by short id (e.g. `kpi-explainer`)."""
        self._maybe_reload()
        return self._sub_agents.get(agent_id)

    def get_skill(self, agent_id: str) -> AgentSkill | None:
        self._maybe_reload()
        return self._skills.get(_norm(agent_id))

    # ── Function-scoped delegate filtering ───────────────────────────────
    def _delegate_pool_for_function(
        self, function_id: str | None
    ) -> dict[str, CofBaseAgent]:
        """Return the subset of delegates visible from `function_id`.

        Visibility rules:
          - Built-in skills (source='builtin') and user uploads ('user')
            are always in scope — they don't belong to any pack.
          - Pack skills (source='pack') are in scope only if the pack
            declares `attach_to_functions=[]` (universal) or includes
            `function_id` in that list.
          - When `function_id` is None/empty (e.g. user is on the home
            page), every loaded delegate is in scope — there's no
            workspace context to scope against.
        """
        from packs import get_pack

        pool: dict[str, CofBaseAgent] = {}
        for name in self._delegate_pool_names:
            agent = self._sub_agents.get(name)
            if not agent:
                continue
            skill = self._skills.get(_norm(name))
            if not skill or skill.source != "pack" or not skill.pack_id:
                pool[name] = agent
                continue
            # Pack skill — gate on attach_to_functions (only when we have
            # a function_id to scope by).
            if not function_id:
                pool[name] = agent
                continue
            pack = get_pack(skill.pack_id)
            attach = list(pack.attach_to_functions) if pack else []
            if not attach or function_id in attach:
                pool[name] = agent
        return pool

    def _build_orchestrator_agent(self, function_id: str | None):
        """Build (or fetch from cache) the orchestrator Agent for a given
        function_id. Cache is keyed by function_id and reset whenever
        skills reload."""
        from agents import Agent

        cache_key = function_id or "__no_function__"
        if cache_key in self._orch_agent_by_fn:
            return self._orch_agent_by_fn[cache_key]
        pool = self._delegate_pool_for_function(function_id)
        agent = Agent(
            name="orchestrator",
            instructions=self._orch_skill.system_prompt,
            model=self._orch_model,
            tools=_build_delegate_tools(pool, self._skills),
        )
        self._orch_agent_by_fn[cache_key] = agent
        return agent

    async def chat_orchestrator(
        self,
        user_message: str,
        extra_context: str = "",
        history: list[dict] | None = None,
        function_id: str | None = None,
    ) -> str:
        """Run the orchestrator (router) agent — it picks specialists via delegate tools.

        Delegates are filtered by `function_id` so a user on the Capital
        Planning workspace doesn't get portfolio-pack agents in the pool.
        """
        if not self._available:
            raise RuntimeError(self._error or "Orchestrator unavailable")
        self._maybe_reload()
        if not getattr(self, "_orch_skill", None):
            raise RuntimeError("Orchestrator skill not loaded")
        agent = self._build_orchestrator_agent(function_id)
        from agents import Runner
        try:
            messages = []
            if extra_context:
                messages.append({"role": "user", "content": f"[Context]\n{extra_context}"})
            for turn in history or []:
                role = turn.get("role")
                content = turn.get("content")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})
            messages.append({"role": "user", "content": user_message})
            result = await Runner.run(
                starting_agent=agent,
                input=messages,
                # Match the specialist default; the orchestrator may
                # delegate-tool-call several specialists in one chat.
                max_turns=20,
            )
            return result.final_output or ""
        except Exception as e:
            log.error("Orchestrator error: %s", e)
            log.debug(traceback.format_exc())
            raise

    async def chat_specialist(self, agent_id: str, user_message: str, extra_context: str = "") -> str:
        """Bypass the orchestrator and call a specialist directly."""
        text, _ = await self.chat_specialist_with_trace(agent_id, user_message, extra_context)
        return text

    async def chat_specialist_with_trace(
        self,
        agent_id: str,
        user_message: str,
        extra_context: str = "",
        on_step=None,
        history: list[dict] | None = None,
    ) -> tuple[str, list[dict]]:
        """Run a specialist and return (final_text, trace_steps).

        If `on_step` is provided, it is invoked with each step dict as the
        agent emits it (tool call → tool output → message), enabling the
        playbook runner to surface live progress.
        """
        self._maybe_reload()
        agent = self._sub_agents.get(agent_id)
        if not agent:
            available = ", ".join(sorted(self._sub_agents.keys())) or "(none)"
            raise RuntimeError(
                f"Specialist '{agent_id}' not registered. Loaded specialists: {available}"
            )
        return await agent.chat_with_trace(
            user_message, extra_context=extra_context, on_step=on_step,
            history=history,
        )

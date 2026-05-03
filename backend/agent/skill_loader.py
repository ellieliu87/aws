"""Skill loader for the CMA Workbench agent team.

Skills are markdown files with YAML frontmatter. Two source directories:

  - `agent/skills/`        — built-in skills shipped with the app
  - `agent/skills_user/`   — skills uploaded by analysts via Settings

User skills with the same `name` override built-ins, so analysts can replace or
extend any of the seven specialists. Tooling (parsing, lookup, listing) reads
both directories transparently.

Skill frontmatter shape::

    ---
    name: kpi-explainer
    description: Explains where a KPI comes from and what's driving it.
    model: gpt-oss-120b
    tools:
      - get_workspace
      - get_kpi_drivers
    max_tokens: 1024
    color: "#0891B2"
    icon: line-chart
    ---

    # System prompt body lives here as plain markdown.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from cof.llm_config import resolve_model

_HERE = Path(__file__).parent
BUILTIN_SKILLS_DIR = _HERE / "skills"
USER_SKILLS_DIR = _HERE / "skills_user"


@dataclass
class AgentSkill:
    name: str
    description: str
    model: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    # MCP server short-ids (e.g. "github", "onelake"). Each id resolves
    # against the runtime MCP registry built from pack registrations; the
    # SDK then advertises that server's tool catalog to the model alongside
    # any in-process Python tools the skill also lists in `tools:`.
    mcp_servers: list[str] = field(default_factory=list)
    sub_agents: list[str] = field(default_factory=list)
    max_tokens: int = 2048
    # Optional cap on how many tool-call turns the agent may run. 0 means
    # "use CofBaseAgent.MAX_TURNS"; bump it for skills that genuinely
    # need many tool calls (e.g. methodology-researcher running multiple
    # rag_search queries per top mover).
    max_turns: int = 0
    quick_queries: list[str] = field(default_factory=list)
    color: str | None = None
    icon: str | None = None
    source: str = "builtin"  # 'builtin' | 'user' | 'pack'
    pack_id: str | None = None  # populated when source == 'pack'


# ── Skill source registry ──────────────────────────────────────────────────
# (path, source_tag, pack_id). Packs append to this list at startup via
# `register_skill_source(path, source='pack', pack_id=<id>)`. A later entry
# overrides an earlier one for skills sharing a `name`, so user uploads
# trump pack skills, which trump built-ins.
_SKILL_SOURCES: list[tuple[Path, str, str | None]] = [
    (BUILTIN_SKILLS_DIR, "builtin", None),
    (USER_SKILLS_DIR, "user", None),
]


def register_skill_source(path: Path, source: str = "pack", pack_id: str | None = None) -> None:
    """Add a directory of `.md` skill files to be loaded by `load_all_skills`.

    Pack-registered sources should use `source='pack'` with `pack_id` set so
    the API surface can tell users which pack a skill came from. User-upload
    overrides still win because they're the last entry checked at lookup."""
    # Insert pack sources BEFORE the user-uploads dir so that a user upload
    # can still override a pack skill of the same name.
    user_idx = next(
        (i for i, (_p, src, _pid) in enumerate(_SKILL_SOURCES) if src == "user"),
        len(_SKILL_SOURCES),
    )
    _SKILL_SOURCES.insert(user_idx, (path, source, pack_id))


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter_dict, body)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text

    fm_lines = lines[1:end_idx]
    body = "\n".join(lines[end_idx + 1:]).strip()

    meta: dict = {}
    current_key: str | None = None
    current_list: list | None = None
    for line in fm_lines:
        stripped = line.strip()
        if line.startswith("  - ") or line.startswith("- "):
            item = stripped.lstrip("- ").strip().strip('"').strip("'")
            if current_list is not None:
                current_list.append(item)
            continue
        m = re.match(r'^(\w[\w_-]*):\s*(.*)$', line)
        if not m:
            continue
        if current_key and current_list is not None:
            meta[current_key] = current_list
        current_key = m.group(1)
        raw = m.group(2).strip()
        if raw == "":
            current_list = []
            meta[current_key] = current_list
        else:
            current_list = None
            raw_unq = raw.strip().strip('"').strip("'")
            try:
                meta[current_key] = int(raw_unq)
            except ValueError:
                try:
                    meta[current_key] = float(raw_unq)
                except ValueError:
                    meta[current_key] = raw_unq
    return meta, body


def _load_one(path: Path, source: str, pack_id: str | None = None) -> AgentSkill:
    text = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    return AgentSkill(
        name=meta.get("name", path.stem.replace("_", "-")),
        description=meta.get("description", ""),
        model=resolve_model(meta.get("model")),
        system_prompt=body,
        tools=meta.get("tools", []) if isinstance(meta.get("tools", []), list) else [],
        mcp_servers=meta.get("mcp_servers", []) if isinstance(meta.get("mcp_servers", []), list) else [],
        sub_agents=meta.get("sub_agents", []) if isinstance(meta.get("sub_agents", []), list) else [],
        max_tokens=int(meta.get("max_tokens", 2048)),
        max_turns=int(meta.get("max_turns", 0) or 0),
        quick_queries=meta.get("quick_queries", []) if isinstance(meta.get("quick_queries", []), list) else [],
        color=meta.get("color"),
        icon=meta.get("icon"),
        source=source,
        pack_id=pack_id,
    )


def load_all_skills() -> dict[str, AgentSkill]:
    """Load every skill from every registered source directory.

    Order matters: later entries in `_SKILL_SOURCES` override earlier ones
    for skills sharing a `name`. The default order is:

        builtin → pack:<id> → … → user

    so an analyst upload always wins over a pack skill, which always wins
    over a built-in."""
    skills: dict[str, AgentSkill] = {}
    BUILTIN_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    import warnings
    for path, source, pack_id in _SKILL_SOURCES:
        if not path.exists():
            continue
        for f in sorted(path.glob("*.md")):
            try:
                skill = _load_one(f, source, pack_id=pack_id)
                skills[_normalize(skill.name)] = skill  # later wins
            except Exception as e:
                warnings.warn(f"Could not load skill {f.name} from {source}: {e}")
    return skills


def _normalize(name: str) -> str:
    return name.replace("-", "_").lower()


def load_skill(name: str) -> AgentSkill | None:
    return load_all_skills().get(_normalize(name))


def list_skills() -> list[AgentSkill]:
    return list(load_all_skills().values())

"""Playbooks router — analyst-defined agentic workflows.

A Playbook is an ordered list of Phases. Each Phase picks an agent skill (from
`agent/skills/` or user uploads in `agent/skills_user/`) and assembles its
inputs from datasets, scenarios, prior-phase outputs, or free-text prompts.
Optionally a Phase has a `gate` — the runner pauses for the analyst to approve,
modify, or reject the agent's output before continuing.

Run lifecycle (synchronous, request-driven):
- POST /playbooks/{id}/run  → executes phases until the first gate (or end).
- POST /runs/{run_id}/gate  → submits an approve/modify/reject decision and
                              resumes execution to the next gate (or end).
- POST /runs/{run_id}/publish → snapshots the final report into the function's
                                Published Reports registry.

If the orchestrator can't reach the LLM (the openai SDK couldn't resolve a
backend at request time), phases fail with the upstream error surfaced —
no mock fallback.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from agent.skill_loader import list_skills
from models.schemas import (
    AttributionsResult,
    CommentaryResult,
    GateDecisionRequest,
    PhaseExecution,
    Playbook,
    PlaybookCreate,
    PlaybookPhase,
    PlaybookRun,
    PlaybookUpdate,
    PublishedReport,
    PublishRequest,
    TraceStep,
    VarianceWalkResult,
)
from routers.auth import get_current_user, get_user_record
from routers.datasets import _DATASETS, _read_dataframe, _resolve_path, _synthesize_sample
from routers.scenarios import _SCENARIOS

router = APIRouter()


_PLAYBOOKS: dict[str, Playbook] = {}
_RUNS: dict[str, PlaybookRun] = {}
_PUBLISHED: dict[str, PublishedReport] = {}


# ── helpers ───────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _summarize_dataset(dataset_id: str) -> str | None:
    d = _DATASETS.get(dataset_id)
    if not d:
        return None
    parts = [f"`{d.name}` (id={d.id}) — {d.source_kind}, {len(d.columns)} columns"]
    cols = ", ".join(c.name for c in d.columns[:12])
    if cols:
        parts.append(f"columns: {cols}")
    if d.row_count:
        parts.append(f"rows: {d.row_count:,}")
    # Try to attach a small sample
    try:
        if d.source_kind == "upload" and d.file_path and d.file_format:
            df = _read_dataframe(_resolve_path(d), d.file_format).head(5)
        else:
            df = pd.DataFrame(_synthesize_sample(d.columns, 5))
        sample = df.to_string(index=False, max_cols=8, max_colwidth=18)
        parts.append("sample (first 5 rows):\n" + sample)
    except Exception:
        pass
    return "\n".join(parts)


def _summarize_scenario(scenario_id: str) -> str | None:
    s = _SCENARIOS.get(scenario_id)
    if not s:
        return None
    return (
        f"`{s.name}` (id={s.id}) — severity={s.severity}, "
        f"variables={s.variables}, horizon_months={s.horizon_months}"
    )


# ── Typed phase-result extraction ──────────────────────────────────────
# Maps each skill_name to the Pydantic schema its output should fit
# into. After the agent completes, we parse the raw text into the
# matching model and store it on PhaseExecution.structured_output.
# Downstream phases get the re-serialized JSON instead of the raw
# (possibly noisy) agent prose, eliminating a whole class of bugs
# where one agent misreads another's output.
_SKILL_RESULT_SCHEMAS: dict[str, type] = {
    "variance-analyst":        VarianceWalkResult,
    "methodology-researcher":  AttributionsResult,
    "commentary-drafter":      CommentaryResult,
}


def _try_parse_json(text: str) -> Any | None:
    """Pull a JSON object out of an agent's text output.

    Strategy (most-specific first, but every candidate is tried — we
    don't stop at the first balanced object found):
      1. Fenced ```json / ```JSON block (with or without trailing newline).
      2. Any fenced ``` block (no language tag or other language).
      3. ALL balanced top-level `{...}` segments via brace-counting.
         Some agents emit a small example object before the real one;
         picking only the first would lose the actual result.
      4. The whole text, stripped.

    Returns the parsed dict, or None if every strategy fails.
    """
    import json
    import re
    if not text:
        return None
    candidates: list[str] = []
    # 1) Explicitly tagged json fence. Don't require a newline before
    # the closing ``` — some models emit `}\n```` flush against the
    # brace, and the strict regex rejects that valid block.
    for m in re.finditer(r"```\s*json\s*\n?([\s\S]*?)```", text, flags=re.IGNORECASE):
        candidates.append(m.group(1).strip())
    # 2) Any fenced block (also tolerant of missing trailing newline).
    for m in re.finditer(r"```(?:\w+)?\s*\n?([\s\S]*?)```", text):
        candidates.append(m.group(1).strip())
    # 3) ALL balanced top-level `{...}` segments. Walk the text and
    # capture every balanced object so a preamble example doesn't
    # shadow the real result later in the message.
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            c = text[j]
            if esc:
                esc = False
                j += 1
                continue
            if c == "\\":
                esc = True
                j += 1
                continue
            if c == '"':
                in_str = not in_str
                j += 1
                continue
            if not in_str:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(text[i:j + 1])
                        i = j + 1
                        break
            j += 1
        else:
            # Reached EOF without closing — bail out of the outer loop.
            break
        if depth != 0:
            i += 1
    # 4) Whole text as-is — last-resort.
    candidates.append(text.strip())

    # Prefer the LARGEST candidate that parses, so we don't pick up a
    # tiny example object embedded in the prose.
    parsed_dicts: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for c in candidates:
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            v = json.loads(c)
        except Exception:
            continue
        if isinstance(v, dict):
            parsed_dicts.append((len(c), v))
    if not parsed_dicts:
        return None
    parsed_dicts.sort(key=lambda t: t[0], reverse=True)
    return parsed_dicts[0][1]


def _extract_structured_result(skill_name: str, raw_output: str) -> tuple[dict | None, str | None]:
    """Parse `raw_output` against the skill's registered result schema.

    Returns `(structured_dict, error_msg)`. `structured_dict` is the
    validated, JSON-serializable result; `error_msg` is non-None when
    parsing or validation fails."""
    schema = _SKILL_RESULT_SCHEMAS.get(skill_name)
    if schema is None:
        return None, None  # no schema registered — skip extraction quietly

    parsed = _try_parse_json(raw_output)
    if parsed is None:
        # Surface a preview of what the agent actually said so the
        # analyst can see WHY parsing failed (e.g. agent narrated
        # instead of emitting JSON).
        preview = (raw_output or "").strip()[:400]
        if len(raw_output or "") > 400:
            preview += "…"
        return None, (
            f"could not parse JSON from {skill_name} output. The agent "
            f"likely emitted prose instead of a JSON block. First 400 "
            f"chars of the output:\n---\n{preview}\n---"
        )

    # Agent-reported error envelope: `{"error": "...", "next_steps": "..."}`.
    # The agent ran a tool that returned an error and decided to surface
    # it (per the variance-analyst skill's "fail-loud" rule). Pass the
    # error straight through to pe.error — much more useful than a
    # schema-validation message about missing fields.
    if isinstance(parsed, dict) and "error" in parsed and not any(
        k in parsed for k in ("total_variance_mm", "top_movers", "slide_header")
    ):
        err = str(parsed.get("error") or "")
        nxt = parsed.get("next_steps") or parsed.get("hint") or ""
        msg = err
        if nxt:
            msg = f"{err}\nNext steps: {nxt}"
        return None, msg

    try:
        validated = schema.model_validate(parsed)
    except Exception as e:
        # Tell the analyst which fields were present and which the
        # schema expected — much more actionable than a bare validation
        # error.
        present = sorted(parsed.keys()) if isinstance(parsed, dict) else []
        expected = sorted(schema.model_fields.keys()) if hasattr(schema, "model_fields") else []
        return None, (
            f"{skill_name} output did not validate against "
            f"{schema.__name__}: {e}\n"
            f"  fields present:  {present}\n"
            f"  fields expected: {expected}"
        )

    return validated.model_dump(mode="json"), None


def _verify_commentary_claims(
    commentary: dict,
    variance: dict | None,
) -> tuple[bool, list[str]]:
    """Cross-reference each NumericClaim in the commentary against the
    matching field in variance-analyst's structured output. Exact match
    after rounding to 2 decimal places ($MM precision). Returns
    `(all_verified, failures)`."""
    if not variance:
        return False, ["no variance-analyst structured_output found upstream"]
    claims = commentary.get("numeric_claims") or []
    if not claims:
        # No claims to verify — treat as a fail loudly so the agent has to
        # at least name what it's claiming. (The schema allows empty but
        # commentary worth its salt always has at least the headline.)
        return False, ["commentary emitted no numeric_claims; can't verify any numbers"]

    by_product = {row.get("product"): row for row in (variance.get("by_product") or [])}

    failures: list[str] = []
    for c in claims:
        text = c.get("text", "<no text>")
        claimed = c.get("value_mm")
        source = c.get("source_field", "")
        if claimed is None or not source:
            failures.append(f"claim {text!r}: missing value_mm or source_field")
            continue

        # Resolve source_field. Two shapes supported:
        #   - top-level field, e.g. "total_variance_mm" / "rate_effect_mm"
        #   - by-product cell, e.g. "by_product[PSAV].rate_effect_mm"
        source_value = None
        m = __import__("re").match(r"by_product\[(.+?)\]\.(\w+)$", source)
        if m:
            prod = m.group(1)
            field = m.group(2)
            row = by_product.get(prod)
            if row is None:
                failures.append(
                    f"claim {text!r}: source_field references product {prod!r} which "
                    f"is not in by_product (have: {sorted(by_product.keys())})"
                )
                continue
            source_value = row.get(field)
        else:
            source_value = variance.get(source)

        if source_value is None:
            failures.append(
                f"claim {text!r}: source_field {source!r} not found in variance JSON"
            )
            continue

        try:
            expected = float(source_value)
            actual   = float(claimed)
        except (TypeError, ValueError):
            failures.append(f"claim {text!r}: non-numeric value or source ({claimed!r} vs {source_value!r})")
            continue

        # Tolerance: accept if the values match exactly after rounding to
        # $MM precision OR if the relative difference is ≤1% OR the
        # absolute difference is ≤$0.01M. Stops false failures from
        # rounding while still catching real misstatements (e.g. claim
        # says "$3B" when actual is "$30B").
        TOL_RELATIVE = 0.01
        TOL_ABSOLUTE = 0.01
        abs_diff = abs(expected - actual)
        rel_diff = abs_diff / max(abs(expected), 1e-9)
        rounded_match = round(expected, 2) == round(actual, 2)

        if not rounded_match and abs_diff > TOL_ABSOLUTE and rel_diff > TOL_RELATIVE:
            # Try to detect a unit-scale mistake. If multiplying the claimed
            # value by 1000 (treating it as $B by accident) lands within
            # 1% of the source, the LLM almost certainly put a $B-scale
            # number in value_mm. Surface this cleanly so the analyst
            # doesn't have to puzzle over a noisy 99.9% delta.
            unit_hint = ""
            try:
                if abs(expected) > 1e-6:
                    if abs(actual * 1000.0 - expected) / abs(expected) <= 0.01:
                        unit_hint = (
                            f" — looks like $B / $MM unit confusion "
                            f"(value_mm should be ~{round(actual * 1000.0, 2)}; the "
                            f"prose's $B figure was put in value_mm directly)"
                        )
                    elif abs(actual / 1000.0 - expected) / abs(expected) <= 0.01:
                        unit_hint = (
                            f" — looks like $MM / $K unit confusion "
                            f"(value_mm should be ~{round(actual / 1000.0, 2)}; "
                            f"the prose's $K figure was put in value_mm directly)"
                        )
            except (ZeroDivisionError, ValueError):
                pass

            failures.append(
                f"claim {text!r}: value_mm={round(actual, 2)} doesn't match "
                f"{source}={round(expected, 2)} (Δ={round(abs_diff, 2)}, "
                f"{rel_diff*100:.1f}% — exceeds 1% tolerance){unit_hint}"
            )

    return (len(failures) == 0), failures


def _build_phase_context(
    phase: PlaybookPhase,
    run: PlaybookRun,
    function_id: str,
    playbook: Playbook,
) -> tuple[str, str]:
    """Return (extra_context, user_message) for the agent call."""
    ctx_parts: list[str] = [
        f"function_id: {function_id}",
        f"playbook: {playbook.name}",
        f"playbook_id: {playbook.id}",
        f"phase_id: {phase.id}",
        f"phase_name: {phase.name}",
    ]
    if playbook.description:
        ctx_parts.append(f"playbook_description: {playbook.description}")

    # Problem statement — the analyst's framing of the question. Surfaced
    # to every phase as a top-level block so the agent answers in the
    # context of the playbook's overall purpose, not just its own phase.
    if playbook.problem_statement:
        ctx_parts.append(
            "[PROBLEM STATEMENT]\n" + playbook.problem_statement.strip()
        )

    # If this is a re-run after the analyst provided gate feedback,
    # carry the feedback into the agent's context so it knows what to
    # change. We stash the feedback on the previous PhaseExecution's
    # `gate_notes` when the gate decision is "rerun".
    pe_prev = next((p for p in run.phases if p.phase_id == phase.id), None)
    if pe_prev and pe_prev.gate_decision == "rerun" and pe_prev.gate_notes:
        ctx_parts.append(
            "[ANALYST FEEDBACK ON PRIOR ATTEMPT]\n"
            + pe_prev.gate_notes.strip()
            + "\n\nApply the feedback above and re-emit the phase output."
        )

    # If this phase was the gate-issuer that triggered a cascade rerun,
    # surface its prior output so it can mark previously-flagged items
    # as remediated when the upstream rerun has addressed them — instead
    # of re-flagging the same issues against the now-corrected input.
    # Generic across skills: any agent at a gate can use this to compare
    # its current findings to the prior attempt's.
    if pe_prev and pe_prev.prior_findings:
        ctx_parts.append(
            "[YOUR PRIOR ATTEMPT'S OUTPUT — re-evaluate against the now-rerun upstream input]\n"
            + pe_prev.prior_findings.strip()[:6000]
            + "\n\nFor every issue you previously raised, decide one of:\n"
            "  • The upstream rerun ADDRESSES it — move it to "
            "`remediated_findings` with a one-line note explaining what "
            "changed (e.g. \"variance-analyst now documents the formula-"
            "vs-data gap as a known overlay\").\n"
            "  • The upstream rerun does NOT address it — keep it in "
            "`findings` and note that it persists.\n"
            "Do NOT silently drop a prior finding; either it remediates or "
            "it persists. New issues found in this attempt go in `findings` "
            "as usual."
        )

    # Uploaded files — surface paths in a tool-friendly shape.
    #
    # Tools (preview_tabular_file, compute_variance_walk, rag_search)
    # accept paths relative to the docs root, so the **relative id** is
    # the recommended argument value — it's short, contains no
    # backslashes, and survives JSON encoding cleanly. We also emit a
    # forward-slash absolute path for the rare case the agent needs it
    # (e.g. some MCP servers want absolute filesystem paths).
    if playbook.uploaded_file_ids:
        from routers.documents import _docs_root  # late import — avoids cycles
        root = _docs_root()
        scope_dir = (root / "playbook" / playbook.id).as_posix()
        rendered = []
        for fid in playbook.uploaded_file_ids:
            # `fid` already uses forward slashes (DocumentInfo normalizes).
            abs_path = (root / fid).as_posix()
            rendered.append(f"- `{fid}`  (absolute: `{abs_path}`)")
        ctx_parts.append(
            "[UPLOADED FILES]\n"
            "These files were attached to this playbook by the analyst. "
            "Pass the relative id (e.g. `playbook/<id>/file.csv`) to any "
            "tool that takes a path — the tools resolve it against the "
            "docs root automatically.\n"
            f"- For `rag_search`, scope to this playbook with "
            f"`doc_dir=\"{scope_dir}\"`, or omit `doc_dir` to scan the "
            "whole corpus.\n"
            "Files:\n"
            + "\n".join(rendered)
        )

    # Track which prior phases have already been included so we don't
    # double-paste them when they're both an explicit input AND a
    # depends_on dep.
    surfaced_phase_ids: set[str] = set()

    def _surface_phase(prior: PhaseExecution, label_prefix: str = "") -> None:
        if prior.phase_id in surfaced_phase_ids:
            return
        surfaced_phase_ids.add(prior.phase_id)
        if prior.structured_output:
            import json as _json
            body = _json.dumps(prior.structured_output, indent=2, default=str)
            ctx_parts.append(
                f"--- {label_prefix}structured output of prior phase "
                f"`{prior.phase_id}` ({prior.phase_name}) — validated "
                f"against the skill's schema ---\n"
                + body[:6000]
            )
        elif prior.output:
            ctx_parts.append(
                f"--- {label_prefix}output of prior phase `{prior.phase_id}` "
                f"({prior.phase_name}) ---\n"
                + prior.output[:3000]
            )

    # Auto-flow: every phase listed in `depends_on` gets its
    # structured_output included automatically. The analyst doesn't have
    # to wire `phase_output` inputs explicitly — the depends_on edge IS
    # the data dependency.
    #
    # When `depends_on` is empty, default to ALL prior completed phases
    # whose structured_output is non-null. This handles the common case
    # where commentary-drafter follows BOTH variance-analyst AND
    # methodology-researcher: previously we only auto-included the one
    # immediately above by index, leaving commentary blind to variance.
    # Tradeoff: when many prior phases ran, the [Context] gets longer —
    # acceptable because (a) only structured outputs are flowed (not
    # raw markdown), and (b) phases without structured_output are
    # silently skipped.
    deps = list(phase.depends_on or [])
    if not deps:
        playbook_phase_ids = [p.id for p in playbook.phases]
        if phase.id in playbook_phase_ids:
            i = playbook_phase_ids.index(phase.id)
            for prior_id in playbook_phase_ids[:i]:
                prior_pe = next((p for p in run.phases if p.phase_id == prior_id), None)
                if prior_pe and prior_pe.structured_output is not None:
                    deps.append(prior_id)
    for dep_id in deps:
        prior = next((p for p in run.phases if p.phase_id == dep_id), None)
        if prior:
            _surface_phase(prior, label_prefix="(auto, via depends_on) ")

    for inp in phase.inputs:
        if inp.kind == "dataset" and inp.ref_id:
            summary = _summarize_dataset(inp.ref_id)
            if summary:
                ctx_parts.append(f"--- input dataset ---\n{summary}")
        elif inp.kind == "scenario" and inp.ref_id:
            summary = _summarize_scenario(inp.ref_id)
            if summary:
                ctx_parts.append(f"--- input scenario ---\n{summary}")
        elif inp.kind == "phase_output" and inp.ref_id:
            prior = next((p for p in run.phases if p.phase_id == inp.ref_id), None)
            if prior:
                _surface_phase(prior)
        elif inp.kind == "prompt" and inp.text:
            ctx_parts.append(f"--- prompt ---\n{inp.text}")

    extra_context = "\n\n".join(ctx_parts)
    # When the phase has no custom instructions, the default user message
    # MUST be format-agnostic — each skill defines its own output contract
    # (variance-analyst demands strict JSON; commentary-drafter demands
    # CommentaryResult JSON; the orchestrator-style skills want markdown).
    # The previous default hardcoded "Return a markdown report", which
    # contradicted JSON-only skills and made the agent emit an "Invalid
    # output format request" error envelope instead of doing the work.
    user_message = (
        phase.instructions
        or f"Execute phase '{phase.name}' using the inputs in the [Context]. "
           "Follow the output format defined in your skill prompt — do not "
           "add or remove fields based on this user message."
    )
    return extra_context, user_message


async def _execute_phase(
    phase: PlaybookPhase,
    run: PlaybookRun,
    playbook: Playbook,
    pe: PhaseExecution,
) -> None:
    """Execute a phase, mutating the supplied PhaseExecution row in place.

    The row is the same object that lives in `run.phases[idx]`, so every state
    change (status, trace.append, output) becomes visible to GET pollers
    without a second copy step.
    """
    started = datetime.utcnow()
    pe.status = "running"
    pe.started_at = started.isoformat() + "Z"
    pe.error = None
    pe.output = None
    pe.trace = []
    # Sync current_phase_idx with the phase actually about to run, so
    # the frontend's "Running phase X" indicator reflects reality
    # instead of whatever the gate handler last computed (which goes
    # stale the moment the wave runner advances to the next phase).
    try:
        run.current_phase_idx = next(
            i for i, p in enumerate(run.phases) if p.phase_id == phase.id
        )
    except StopIteration:
        pass
    extra_context, user_message = _build_phase_context(phase, run, playbook.function_id, playbook)

    # Late import to dodge any circular dependency
    from routers.chat import _ORCH

    if not _ORCH.available:
        pe.status = "failed"
        pe.error = (
            _ORCH.init_error
            or "LLM not reachable. Inside the corporate environment, no env vars are needed. Outside it, set OPENAI_API_KEY in backend/.env."
        )
        completed = datetime.utcnow()
        pe.completed_at = completed.isoformat() + "Z"
        pe.duration_ms = (completed - started).total_seconds() * 1000
        return

    def _on_step(step_dict: dict) -> None:
        """Live append: each tool call / output / message / handoff lands here
        as the agent emits it, so polling sees the trace grow during the run."""
        try:
            pe.trace.append(TraceStep(**step_dict))
        except Exception:
            pass  # never let a malformed step break the phase

    try:
        text, _final_trace = await _ORCH.chat_specialist_with_trace(
            phase.skill_name, user_message,
            extra_context=extra_context,
            on_step=_on_step,
        )
        pe.output = text
        pe.agent_id = phase.skill_name

        # Try to parse the agent's output into the typed schema for this
        # skill. On success, downstream phases see the validated +
        # re-serialized JSON instead of the agent's raw prose.
        structured, parse_err = _extract_structured_result(phase.skill_name, text)

        # One-shot JSON retry: if the agent emitted prose instead of JSON,
        # replay the conversation with the prose as the assistant turn and
        # ask it to re-emit only the JSON block.
        if parse_err and _SKILL_RESULT_SCHEMAS.get(phase.skill_name):
            retry_history = [
                {"role": "user",      "content": user_message},
                {"role": "assistant", "content": text},
            ]
            retry_msg = (
                "Your previous response was not parseable JSON. "
                "Re-emit your answer as a JSON object only — "
                "no preamble, no explanation, no trailing prose. "
                "You may use a ```json fence, but nothing else."
            )
            try:
                text2, _ = await _ORCH.chat_specialist_with_trace(
                    phase.skill_name, retry_msg,
                    extra_context=extra_context,
                    on_step=_on_step,
                    history=retry_history,
                )
                pe.output = text2
                structured, parse_err = _extract_structured_result(phase.skill_name, text2)
            except Exception as retry_exc:
                parse_err = f"{parse_err} | retry also failed: {retry_exc}"

        if structured is not None:
            # commentary-drafter gets a backend post-hoc verification of
            # every NumericClaim against variance-analyst's structured_output.
            if phase.skill_name == "commentary-drafter":
                variance_struct = _find_prior_structured(run, "variance-analyst")
                ok, failures = _verify_commentary_claims(structured, variance_struct)
                structured["numbers_verified"]      = ok
                structured["verification_failures"] = failures
            pe.structured_output = structured
        elif parse_err:
            # Couldn't validate the output — keep the raw text for the
            # analyst to see, but mark as failed so the chain doesn't
            # silently propagate garbage downstream.
            pe.status = "failed"
            pe.error = parse_err
            return

        # Auto-pass the gate when this is a rerun AND the agent confirms
        # every prior finding is remediated. Without this the analyst has
        # to click "Approve" a second time even though they already
        # green-lit the path via "Send to <upstream> & rerun chain" and
        # the agent has just told them "all prior issues are fixed". We
        # only auto-pass when ALL three conditions hold:
        #   • `pe.prior_findings` is set (this is a rerun cycle, not a
        #     fresh first-time gate),
        #   • the new `structured_output.verdict == "approved"`,
        #   • `findings` is empty AND `remediated_findings` is non-empty
        #     (the agent explicitly accounted for the prior issues).
        # If any condition fails, the gate fires normally so the analyst
        # can still review.
        if (
            phase.gate
            and structured
            and pe.prior_findings
            and str(structured.get("verdict", "")).lower() == "approved"
            and not (structured.get("findings") or [])
            and (structured.get("remediated_findings") or [])
        ):
            pe.status = "completed"
            pe.gate_decision = "approve"
            pe.gate_notes = (
                "Auto-approved: rerun confirmed all prior findings "
                "remediated and the agent emitted verdict='approved'."
            )
        else:
            pe.status = "awaiting_gate" if phase.gate else "completed"
    except Exception as e:
        pe.status = "failed"
        pe.error = str(e)
    finally:
        completed = datetime.utcnow()
        pe.completed_at = completed.isoformat() + "Z"
        pe.duration_ms = (completed - started).total_seconds() * 1000


def _find_prior_structured(run: PlaybookRun, skill_name: str) -> dict | None:
    """Look back through `run.phases` for the most recent completed phase
    whose `skill_name` matches and which has a structured_output. Used by
    verification to find variance-analyst's numbers from the
    commentary-drafter phase."""
    for p in run.phases:
        if p.skill_name == skill_name and p.structured_output:
            return p.structured_output
    return None


def _resolve_deps(playbook: Playbook) -> dict[str, list[str]]:
    """Return `{phase_id: [dep_phase_id, ...]}` for the playbook.

    A phase's `depends_on` is honored as-is when set. Otherwise we
    fall back to **linear** behavior: phase N depends on phase N-1.
    The first phase with no explicit deps is a DAG root.
    """
    valid_ids = {p.id for p in playbook.phases}
    deps: dict[str, list[str]] = {}
    for i, p in enumerate(playbook.phases):
        if p.depends_on:
            # Drop any unknown ids quietly.
            deps[p.id] = [d for d in p.depends_on if d in valid_ids]
        elif i > 0:
            deps[p.id] = [playbook.phases[i - 1].id]
        else:
            deps[p.id] = []
    return deps


async def _run_to_next_gate(run: PlaybookRun, playbook: Playbook) -> None:
    """Execute phases as a DAG until the next gate, end, or failure.

    Phases whose dependencies are all completed run **concurrently** via
    `asyncio.gather`. A phase that hits a gate pauses the run; phases
    parallel to it that have already started complete normally before
    the run pauses.

    Mutates `run.phases[*]` in place so GET pollers see partial progress
    as it happens (status flipping idle → running → completed, trace
    steps appended live).
    """
    deps_map = _resolve_deps(playbook)
    pe_by_id = {pe.phase_id: pe for pe in run.phases}
    phase_def_by_id = {p.id: p for p in playbook.phases}

    # Ensure every phase has a PhaseExecution row (legacy runs may have
    # been started before this field existed).
    for p in playbook.phases:
        if p.id not in pe_by_id:
            new_pe = PhaseExecution(
                phase_id=p.id, phase_name=p.name, skill_name=p.skill_name,
                status="idle", duration_ms=0.0,
            )
            run.phases.append(new_pe)
            pe_by_id[p.id] = new_pe

    def _status_of(pid: str) -> str:
        return pe_by_id[pid].status

    def _ready() -> list[str]:
        """Phase ids whose status is idle AND all deps are completed."""
        out = []
        for pid, deps in deps_map.items():
            if _status_of(pid) != "idle":
                continue
            if all(_status_of(d) == "completed" for d in deps):
                out.append(pid)
        return out

    def _terminate(status_lit: str) -> None:
        """Move the run to a terminal state. Build the final report FIRST
        and assign it BEFORE flipping run.status — the polling client uses
        run.status to decide when to stop polling, so if status flipped
        first there's a window where the client sees 'completed' with no
        report and gives up.

        If the report builder itself raises (a malformed structured_output,
        an unexpected None, etc.), we MUST NOT strand the run at "running".
        Catch the failure inline, surface it as the report body so the
        analyst can see what broke, and still flip the status."""
        run.completed_at = _now()
        try:
            report = _build_final_report(run, playbook)
        except Exception as e:  # noqa: BLE001 — last line of defence
            import traceback as _tb
            report = (
                "# Report build failed\n\n"
                "The wave runner finished, but the final-report builder "
                "raised an exception. Phase outputs above are still valid; "
                "this just means the synthesis step couldn't render.\n\n"
                f"```\n{type(e).__name__}: {e}\n\n"
                f"{_tb.format_exc()[-800:]}\n```\n"
            )
        # Guarantee a truthy string so the frontend's `done && run.final_report`
        # check trips. An empty string would silently hide the card.
        run.final_report = report or "# (empty report)"
        run.status = status_lit  # type: ignore[assignment]

    try:
        while run.status == "running":
            ready = _ready()
            if not ready:
                break  # nothing to run — either everything's done or blocked

            # Launch all ready phases in parallel.
            coros = []
            for pid in ready:
                phase = phase_def_by_id[pid]
                pe = pe_by_id[pid]
                coros.append(_execute_phase(phase, run, playbook, pe))
            await asyncio.gather(*coros)

            # Did any phase fail? Stop the run — but still build a final
            # report so the analyst sees what every prior phase produced
            # before the failure, instead of an empty Final Report card.
            if any(pe_by_id[pid].status == "failed" for pid in ready):
                _terminate("failed")
                return

            # Did any phase hit a gate? Pause the run — other parallel phases
            # in this same wave have already completed.
            if any(pe_by_id[pid].status == "awaiting_gate" for pid in ready):
                run.status = "awaiting_gate"
                return

        # Nothing more is ready — either everything completed cleanly, or
        # there's a deps cycle / unsatisfiable dependency. Tally the result.
        if all(pe_by_id[p.id].status == "completed" for p in playbook.phases):
            _terminate("completed")
        elif any(pe_by_id[p.id].status == "awaiting_gate" for p in playbook.phases):
            run.status = "awaiting_gate"
        else:
            # Mix of {idle, failed, rejected} — none ready, none at gate, not
            # all completed. Always land in a terminal state so the client's
            # `done` check trips and the Final Report card renders. The
            # earlier version of this branch only set `run.status = "failed"`
            # when there were stuck idle phases — leaving a stuck-at-running
            # bug for any other shape of partial failure.
            unrun = [p.id for p in playbook.phases if pe_by_id[p.id].status == "idle"]
            if unrun:
                stuck = pe_by_id[unrun[0]]
                stuck.status = "failed"
                stuck.error = (
                    f"depends_on never satisfied. Stuck phases: {unrun}. "
                    f"Check for cycles or upstream failures."
                )
            _terminate("failed")
    except Exception as e:
        # Defensive: any uncaught exception in the wave runner (a phase
        # builder throwing before _execute_phase's own try block, an
        # unexpected schema mismatch, etc.) would otherwise leave run.status
        # pinned at "running" forever, with no way for the analyst to
        # recover except deleting the run. Surface it as a failure with the
        # error captured on whichever phase was last "running".
        last_running = next(
            (p for p in run.phases if p.status == "running"),
            None,
        )
        if last_running is not None:
            last_running.status = "failed"
            last_running.error = f"Wave runner crashed: {e}"
        _terminate("failed")


def _build_final_report(run: PlaybookRun, playbook: Playbook) -> str:
    """Presentation-grade markdown report.

    Layout (top → bottom):
      1. Cover  — title, tagline, run metadata in a clean header
      2. Executive Summary — pulled from commentary-drafter when present
      3. Headline Numbers — pulled from variance-analyst's totals
      4. Drivers — pulled from methodology-researcher's top_movers
      5. Commentary Memo — the full slide-ready prose
      6. Audit Trail — phase-by-phase log (collapsed underneath)

    Each section is built only when the corresponding agent ran and
    produced structured_output. Sections are skipped silently when
    their data isn't available — keeps the report clean for partial /
    failed runs instead of showing empty headers."""
    from datetime import datetime as _dt

    def _fmt_mm(v) -> str:
        try:
            x = float(v)
        except Exception:
            return str(v)
        sign = "−" if x < 0 else ""
        a = abs(x)
        if a >= 1000:
            return f"{sign}${a:,.0f}M"
        if a >= 10:
            return f"{sign}${a:.0f}M"
        if a >= 0.1:
            return f"{sign}${a:.1f}M"
        return f"{sign}${a:.2f}M"

    # Pull the typed structured outputs by skill_name (more reliable
    # than phase ordering when the playbook author rearranges phases).
    by_skill: dict[str, dict] = {}
    for pe in run.phases:
        if pe.structured_output and isinstance(pe.structured_output, dict):
            by_skill[pe.skill_name] = pe.structured_output
    variance     = by_skill.get("variance-analyst") or {}
    attributions = by_skill.get("methodology-researcher") or {}
    commentary   = by_skill.get("commentary-drafter") or {}

    started_at = run.created_at
    completed_at = run.completed_at
    try:
        date_label = _dt.fromisoformat(str(started_at).replace("Z", "")).strftime("%B %d, %Y")
    except Exception:
        date_label = ""

    L: list[str] = []

    # ── 1. Cover ────────────────────────────────────────────────────
    L.append(f"# {playbook.name}")
    if playbook.description:
        L.append(f"_{playbook.description}_")
    L.append("")
    meta_bits: list[str] = []
    if variance.get("current_scenario") and variance.get("benchmark_scenario"):
        meta_bits.append(f"**{variance['current_scenario']}** vs **{variance['benchmark_scenario']}**")
    if variance.get("metric"):
        meta_bits.append(f"Metric: `{variance['metric']}`")
    if date_label:
        meta_bits.append(f"Run date: {date_label}")
    status_label = {"completed": "Completed", "failed": "Failed", "rejected": "Rejected", "awaiting_gate": "Awaiting gate"}.get(run.status, run.status)
    meta_bits.append(f"Status: **{status_label}**")
    if meta_bits:
        L.append("&middot; ".join(meta_bits))
    L.append("")
    L.append("---")
    L.append("")

    # ── 2. Executive Summary (commentary slide_header + primary) ────
    if commentary.get("slide_header") or commentary.get("primary_driver"):
        L.append("## Executive Summary")
        L.append("")
        if commentary.get("slide_header"):
            L.append(f"**{commentary['slide_header']}**")
            L.append("")
        if commentary.get("primary_driver"):
            L.append(commentary["primary_driver"])
            L.append("")

    # ── 3. Headline Numbers (variance totals + waterfall) ───────────
    if variance and variance.get("total_variance_mm") is not None:
        L.append("## Headline Numbers")
        L.append("")
        bench_ie = variance.get("benchmark_ie_mm")
        cur_ie   = variance.get("current_ie_mm")
        delta    = variance.get("total_variance_mm")
        if bench_ie is not None and cur_ie is not None:
            L.append(
                f"| Scenario | Total Interest Expense |"
            )
            L.append("|---|---:|")
            L.append(f"| {variance.get('benchmark_scenario','baseline')} | {_fmt_mm(bench_ie)} |")
            L.append(f"| {variance.get('current_scenario','stress')}   | {_fmt_mm(cur_ie)}  |")
            L.append(f"| **Δ** | **{_fmt_mm(delta)}** |")
            L.append("")
        else:
            L.append(f"**ΔIE**: {_fmt_mm(delta)}")
            L.append("")

        L.append("**Decomposition**")
        L.append("")
        L.append("| Effect | Contribution |")
        L.append("|---|---:|")
        L.append(f"| Volume | {_fmt_mm(variance.get('volume_effect_mm'))} |")
        L.append(f"| Mix    | {_fmt_mm(variance.get('mix_effect_mm'))} |")
        L.append(f"| Rate   | {_fmt_mm(variance.get('rate_effect_mm'))} |")
        L.append("")

    # ── 4. Drivers (methodology top_movers) ─────────────────────────
    movers = attributions.get("top_movers") or []
    if movers:
        L.append("## Material Drivers")
        L.append("")
        L.append("| Rank | Product | Δ | Contribution | Primary effect | Model |")
        L.append("|---|---|---:|---:|---|---|")
        for m in movers[:8]:
            rank = m.get("rank", "")
            prod = m.get("product", "")
            tv   = _fmt_mm(m.get("total_variance_mm"))
            cp   = m.get("contribution_pct")
            cp_s = f"{cp:.1f}%" if isinstance(cp, (int, float)) else ""
            pe   = m.get("primary_effect", "")
            mc   = m.get("model_component", "")
            L.append(f"| {rank} | {prod} | {tv} | {cp_s} | {pe} | `{mc}` |")
        L.append("")

    # ── 5. Commentary Memo ─────────────────────────────────────────
    if commentary.get("secondary_drivers") or commentary.get("overlay_impacts"):
        L.append("## Commentary")
        L.append("")
        if commentary.get("secondary_drivers"):
            L.append("**Secondary drivers**")
            L.append("")
            for d in commentary["secondary_drivers"]:
                L.append(f"- {d}")
            L.append("")
        if commentary.get("overlay_impacts"):
            L.append("**Overlay impacts** _(manual, separated)_")
            L.append("")
            for d in commentary["overlay_impacts"]:
                L.append(f"- {d}")
            L.append("")

    # ── 6. Audit Trail ─────────────────────────────────────────────
    L.append("---")
    L.append("")
    L.append("## Audit Trail")
    L.append("")
    L.append(f"_Run id: `{run.id}` &middot; Function: `{run.function_id}`_")
    L.append("")
    for i, pe in enumerate(run.phases, start=1):
        L.append(f"### Phase {i}: {pe.phase_name}")
        meta = [f"Skill: `{pe.skill_name}`",
                f"Duration: {pe.duration_ms:.0f} ms",
                f"Status: {pe.status}"]
        if pe.gate_decision:
            badge = {"approve": "✓ APPROVED", "modify": "✎ MODIFIED",
                     "reject": "✗ REJECTED", "rerun": "↻ RERUN"}.get(pe.gate_decision, pe.gate_decision)
            meta.append(f"Analyst gate: {badge}")
        L.append("_" + " &middot; ".join(meta) + "_")
        if pe.error:
            L.append("")
            L.append(f"> **Error**: {pe.error}")
        if pe.gate_notes:
            L.append("")
            L.append(f"> _Analyst note_: {pe.gate_notes}")
        L.append("")
    return "\n".join(L)


# ── route ordering: literal paths must come before /{playbook_id} ───────
# (FastAPI matches in registration order; otherwise /runs etc. get swallowed)

@router.get("/_skills")
async def list_available_skills(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    """List loadable skill names for the phase skill picker.

    When `function_id` is supplied, pack skills are filtered the same
    way the chat orchestrator filters its delegate pool: a pack skill
    is in scope only if its pack's `attach_to_functions` is empty
    (universal) or includes the supplied function_id. Built-in / user
    skills are always in scope. With no `function_id`, every skill is
    returned (callers without workspace context).
    """
    from packs import get_pack
    from routers.functions import BUSINESS_FUNCTIONS

    imported_pack_ids: set[str] | None = None
    if function_id:
        fn = next((f for f in BUSINESS_FUNCTIONS if f.id == function_id), None)
        if fn and fn.imported_packs:
            imported_pack_ids = {p.pack_id for p in fn.imported_packs}

    out = []
    for s in list_skills():
        # Hide the orchestrator from the phase picker — it's the chat
        # router, not a phase specialist.
        if s.name == "orchestrator":
            continue
        if function_id and s.source == "pack" and s.pack_id:
            if imported_pack_ids is not None:
                # Workspace has explicit imports — only show imported packs.
                # Explicit import overrides attach_to_functions.
                if s.pack_id not in imported_pack_ids:
                    continue
            else:
                # No explicit imports — fall back to pack's attach_to_functions.
                pack = get_pack(s.pack_id)
                attach = list(pack.attach_to_functions) if pack else []
                if attach and function_id not in attach:
                    continue
        out.append({
            "name": s.name,
            "description": s.description,
            "source": s.source,
            "pack_id": s.pack_id,
            "color": s.color,
            "icon": s.icon,
        })
    return out


@router.get("/runs", response_model=list[PlaybookRun])
async def list_runs(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = list(_RUNS.values())
    if function_id:
        items = [r for r in items if r.function_id == function_id]
    items.sort(key=lambda r: r.created_at, reverse=True)
    return items


def _reconcile_stuck_run(run: PlaybookRun) -> None:
    """Watchdog: detect runs that claim to be running but have no actual
    work in flight, and force them to a terminal state.

    The wave-runner already has its own try/except + _terminate helper,
    but that only fires on the paths that actually reach the post-loop
    tally. If the asyncio task crashes between waves in a way that
    skips the except clause (e.g. the task is GC'd, the event loop
    shuts down mid-flight, or a hot-reload nukes the closure), the run
    is left at status='running' with every phase already completed.
    The frontend's `done` check stays false, the final-report card
    never renders, and polling continues forever. This reconciliation
    runs on every poll — it costs nothing when the run is healthy, and
    rescues genuinely stuck runs without the analyst having to delete
    and restart."""
    if run.status != "running":
        return
    # If any phase is still actually doing work, the run is healthy.
    has_active = any(
        p.status in ("running", "awaiting_gate") for p in run.phases
    )
    if has_active:
        return
    # No phase is active but the run claims it's running — reconcile.
    pb = _PLAYBOOKS.get(run.playbook_id)
    if not pb:
        # Playbook was deleted out from under the run. Best-effort: mark
        # failed with a stub report so the analyst sees something.
        run.status = "failed"
        run.completed_at = _now()
        run.final_report = run.final_report or "# Run abandoned\n\nThe playbook was deleted while this run was active."
        return
    # Build the report first so polling clients always observe the
    # terminal status with the report already populated.
    run.completed_at = run.completed_at or _now()
    if all(p.status == "completed" for p in run.phases):
        new_status = "completed"
    elif any(p.status in ("failed", "rejected") for p in run.phases):
        new_status = "failed"
    else:
        # Phases stuck idle with no scheduler running them — treat as failed.
        new_status = "failed"
        for p in run.phases:
            if p.status == "idle":
                p.status = "failed"
                p.error = p.error or (
                    "Wave runner exited without running this phase. The "
                    "playbook tab's run state may have been interrupted "
                    "(server reload, network failure, or browser refresh "
                    "during a long-running phase)."
                )
                break
    try:
        report = _build_final_report(run, pb)
    except Exception as e:  # noqa: BLE001
        report = (
            "# Report build failed during reconciliation\n\n"
            f"```\n{type(e).__name__}: {e}\n```\n"
        )
    run.final_report = run.final_report or report or "# (empty report)"
    run.status = new_status  # type: ignore[assignment]


@router.get("/runs/{run_id}", response_model=PlaybookRun)
async def get_run(run_id: str, _: str = Depends(get_current_user)):
    r = _RUNS.get(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    _reconcile_stuck_run(r)
    return r


def _phase_descendants(pb: "Playbook", target_phase_id: str) -> set[str]:
    """All phase ids that transitively depend on `target_phase_id`. Considers
    explicit `depends_on` AND linear-inferred dependencies (a phase with empty
    depends_on is treated as depending on the immediately preceding phase by
    index — matching the executor's own scheduling rule). Used by the gate
    handler's cascade-rerun path: when the analyst reruns variance-analyst,
    every downstream phase that consumed its output also resets."""
    effective_deps: dict[str, list[str]] = {}
    for i, ph in enumerate(pb.phases):
        if ph.depends_on:
            effective_deps[ph.id] = list(ph.depends_on)
        elif i > 0:
            effective_deps[ph.id] = [pb.phases[i - 1].id]
        else:
            effective_deps[ph.id] = []

    descendants: set[str] = set()
    queue: list[str] = [target_phase_id]
    while queue:
        current = queue.pop(0)
        for ph_id, deps in effective_deps.items():
            if current in deps and ph_id not in descendants and ph_id != target_phase_id:
                descendants.add(ph_id)
                queue.append(ph_id)
    return descendants


@router.post("/runs/{run_id}/gate", response_model=PlaybookRun)
async def submit_gate(
    run_id: str,
    req: GateDecisionRequest,
    _: str = Depends(get_current_user),
):
    run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != "awaiting_gate":
        raise HTTPException(status_code=400, detail=f"Run is not awaiting a gate (status={run.status})")
    pb = _PLAYBOOKS.get(run.playbook_id)
    if not pb:
        raise HTTPException(status_code=404, detail="Underlying playbook is gone")

    # Resolve which phase the analyst is gating. With parallel phases,
    # multiple can be awaiting a gate simultaneously — `phase_id` is
    # required to disambiguate, but for backward-compat (linear) we
    # fall back to the first phase whose status is awaiting_gate.
    pe = None
    if req.phase_id:
        pe = next((p for p in run.phases if p.phase_id == req.phase_id), None)
        if not pe:
            raise HTTPException(status_code=404, detail=f"Phase {req.phase_id} not in run")
        if pe.status != "awaiting_gate":
            raise HTTPException(
                status_code=400,
                detail=f"Phase {req.phase_id} is not awaiting a gate (status={pe.status})",
            )
    else:
        pe = next((p for p in run.phases if p.status == "awaiting_gate"), None)
        if not pe:
            raise HTTPException(status_code=400, detail="No phase is awaiting a gate")

    pe.gate_decision = req.decision
    pe.gate_notes = req.notes

    if req.decision == "modify" and req.modified_output:
        pe.output = req.modified_output
        pe.status = "completed"

    elif req.decision == "approve":
        pe.status = "completed"

    elif req.decision == "reject":
        pe.status = "rejected"
        # Reject is terminal — abandons the whole run. Build the report
        # before flipping run.status so a poll that catches the new
        # status also sees the populated final_report (the client stops
        # polling as soon as status leaves "running"/"awaiting_gate").
        run.completed_at = _now()
        run.final_report = _build_final_report(run, pb)
        run.status = "rejected"
        return run

    elif req.decision == "rerun":
        # Two flavors of rerun:
        #   (a) Same phase  — analyst tweaks instructions for THIS agent.
        #       Reset just `pe`; the gate's feedback rides on its
        #       own gate_notes.
        #   (b) Upstream    — analyst found a root cause earlier in the
        #       chain (e.g. attribution-challenger surfaces a variance-
        #       analyst mistake). The named target phase + every phase
        #       that depends on it (transitively, including this gate
        #       phase) is reset to idle. Feedback rides on the upstream
        #       target's gate_notes so its [Context] sees it; the gate
        #       phase reruns clean once the chain catches up.
        feedback_text = (req.feedback or req.notes or "").strip() or None

        if req.rerun_from_phase_id and req.rerun_from_phase_id != pe.phase_id:
            target_pe = next(
                (p for p in run.phases if p.phase_id == req.rerun_from_phase_id),
                None,
            )
            if target_pe is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"rerun_from_phase_id `{req.rerun_from_phase_id}` is not a phase in this run",
                )

            descendants = _phase_descendants(pb, req.rerun_from_phase_id)
            cascade = descendants | {req.rerun_from_phase_id}

            for p in run.phases:
                if p.phase_id not in cascade:
                    continue
                # Snapshot the gate-issuing phase's prior output BEFORE we
                # wipe it. On rerun, the agent gets this as `[YOUR PRIOR
                # ATTEMPT'S OUTPUT]` so it can see what it previously
                # flagged and decide whether the upstream rerun has
                # remediated each item — rather than blindly re-emitting
                # the same findings against a fixed input.
                if p.phase_id == pe.phase_id:
                    if p.structured_output:
                        try:
                            import json as _json
                            p.prior_findings = _json.dumps(p.structured_output, indent=2, default=str)
                        except Exception:
                            p.prior_findings = p.output
                    elif p.output:
                        p.prior_findings = p.output
                # Reset everything the rerun is about to recompute.
                p.status = "idle"
                p.output = None
                p.structured_output = None
                p.error = None
                p.trace = []
                if p.phase_id == req.rerun_from_phase_id:
                    # Target phase carries the feedback so
                    # _build_phase_context splices it into [Context].
                    p.gate_decision = "rerun"
                    p.gate_notes = feedback_text
                else:
                    # Cleared so a stale "rerun" decision from a prior
                    # gate doesn't leak into the new attempt.
                    p.gate_decision = None
                    p.gate_notes = None
        else:
            # Same-phase rerun (the existing behavior).
            pe.gate_notes = feedback_text
            pe.status = "idle"
            pe.output = None
            pe.structured_output = None
            pe.error = None
            pe.trace = []

    # Are any other phases still awaiting a gate? If so, stay paused.
    if any(p.status == "awaiting_gate" for p in run.phases):
        run.status = "awaiting_gate"
        return run

    run.status = "running"
    # Bump the legacy current_phase_idx counter for any UI still using
    # it (it's no longer authoritative under the DAG scheduler — the
    # DAG runner walks the deps graph itself).
    run.current_phase_idx = max(
        (i for i, p in enumerate(run.phases) if p.status != "idle"),
        default=0,
    )
    asyncio.create_task(_run_to_next_gate(run, pb))
    return run


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, _: str = Depends(get_current_user)):
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail="Run not found")
    del _RUNS[run_id]


@router.post("/runs/{run_id}/publish", response_model=PublishedReport, status_code=201)
async def publish_run(
    run_id: str,
    req: PublishRequest,
    user: dict = Depends(get_user_record),
):
    run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in ("completed", "rejected"):
        raise HTTPException(status_code=400, detail="Can only publish completed or rejected runs")
    pb = _PLAYBOOKS.get(run.playbook_id)
    if not pb:
        raise HTTPException(status_code=404, detail="Playbook is gone")
    body = run.final_report or _build_final_report(run, pb)

    pid = f"pbpub-{uuid.uuid4().hex[:10]}"
    rep = PublishedReport(
        id=pid,
        function_id=run.function_id,
        playbook_id=run.playbook_id,
        playbook_name=run.playbook_name,
        run_id=run.id,
        title=req.title or f"{pb.name} — {run.created_at[:10]}",
        body_markdown=body,
        published_by=user["username"],
        published_at=_now(),
    )
    _PUBLISHED[pid] = rep
    return rep


@router.get("/published", response_model=list[PublishedReport])
async def list_published(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = list(_PUBLISHED.values())
    if function_id:
        items = [p for p in items if p.function_id == function_id]
    items.sort(key=lambda p: p.published_at, reverse=True)
    return items


@router.delete("/published/{report_id}", status_code=204)
async def delete_published(report_id: str, _: str = Depends(get_current_user)):
    if report_id not in _PUBLISHED:
        raise HTTPException(status_code=404, detail="Report not found")
    del _PUBLISHED[report_id]


# ── playbook CRUD (parameter routes registered last) ─────────────────────
@router.get("", response_model=list[Playbook])
async def list_playbooks(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = list(_PLAYBOOKS.values())
    if function_id:
        items = [p for p in items if p.function_id == function_id]
    items.sort(key=lambda p: p.updated_at or p.created_at, reverse=True)
    return items


@router.get("/{playbook_id}", response_model=Playbook)
async def get_playbook(playbook_id: str, _: str = Depends(get_current_user)):
    p = _PLAYBOOKS.get(playbook_id)
    if not p:
        raise HTTPException(status_code=404, detail="Playbook not found")
    return p


@router.post("", response_model=Playbook, status_code=201)
async def create_playbook(req: PlaybookCreate, _: str = Depends(get_current_user)):
    # Honor a client-supplied id if present and well-formed so the
    # frontend can pre-allocate the id for upload scoping. Reject ids
    # that already exist or that contain unsafe characters.
    pid = (req.id or "").strip()
    if pid:
        if not all(c.isalnum() or c in "-_" for c in pid):
            raise HTTPException(status_code=400, detail="invalid playbook id")
        if pid in _PLAYBOOKS:
            raise HTTPException(status_code=409, detail="playbook id already exists")
    else:
        pid = f"pbk-{uuid.uuid4().hex[:10]}"
    # Re-id phases sequentially so they're predictable
    fixed_phases = [
        PlaybookPhase(**{**ph.model_dump(), "id": f"phase-{i + 1}"})
        for i, ph in enumerate(req.phases)
    ]
    pb = Playbook(
        id=pid,
        function_id=req.function_id,
        name=req.name,
        description=req.description,
        problem_statement=req.problem_statement,
        uploaded_file_ids=list(req.uploaded_file_ids or []),
        phases=fixed_phases,
        created_at=_now(),
    )
    _PLAYBOOKS[pid] = pb
    return pb


@router.patch("/{playbook_id}", response_model=Playbook)
async def update_playbook(
    playbook_id: str,
    req: PlaybookUpdate,
    _: str = Depends(get_current_user),
):
    p = _PLAYBOOKS.get(playbook_id)
    if not p:
        raise HTTPException(status_code=404, detail="Playbook not found")
    if req.name is not None:
        p.name = req.name
    if req.description is not None:
        p.description = req.description
    if req.problem_statement is not None:
        p.problem_statement = req.problem_statement
    if req.uploaded_file_ids is not None:
        p.uploaded_file_ids = list(req.uploaded_file_ids)
    if req.phases is not None:
        p.phases = [
            PlaybookPhase(**{**ph.model_dump(), "id": f"phase-{i + 1}"})
            for i, ph in enumerate(req.phases)
        ]
    p.updated_at = _now()
    return p


@router.delete("/{playbook_id}", status_code=204)
async def delete_playbook(playbook_id: str, _: str = Depends(get_current_user)):
    if playbook_id not in _PLAYBOOKS:
        raise HTTPException(status_code=404, detail="Playbook not found")
    del _PLAYBOOKS[playbook_id]


# ── runs ─────────────────────────────────────────────────────────────────
@router.post("/{playbook_id}/run", response_model=PlaybookRun, status_code=201)
async def start_run(playbook_id: str, _: str = Depends(get_current_user)):
    pb = _PLAYBOOKS.get(playbook_id)
    if not pb:
        raise HTTPException(status_code=404, detail="Playbook not found")
    if not pb.phases:
        raise HTTPException(status_code=400, detail="Playbook has no phases")

    rid = f"pbr-{uuid.uuid4().hex[:10]}"
    # Pre-populate idle phase records so the frontend can render the timeline
    # immediately and show each one flip to "running" / "completed" as polling
    # picks up state.
    initial_phases = [
        PhaseExecution(
            phase_id=ph.id,
            phase_name=ph.name,
            skill_name=ph.skill_name,
            status="idle",
            duration_ms=0.0,
        )
        for ph in pb.phases
    ]
    run = PlaybookRun(
        id=rid,
        playbook_id=playbook_id,
        playbook_name=pb.name,
        function_id=pb.function_id,
        status="running",
        phases=initial_phases,
        current_phase_idx=0,
        created_at=_now(),
    )
    _RUNS[rid] = run
    # Fire-and-forget: the task mutates _RUNS[rid] as it goes, polling sees it.
    asyncio.create_task(_run_to_next_gate(run, pb))
    return run


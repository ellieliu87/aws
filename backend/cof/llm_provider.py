"""Provider-agnostic single-shot completion.

The workbench has four non-agent LLM call sites (tool drafter, analytics
drafter, run narrator, whitepaper extractor). All four share one shape: a
system prompt, one user message, and a string back. This module is the seam
those call sites go through so the provider is a config decision rather than
an import decision.

Providers, selected by `cof.llm_config.provider()`:

  cof / openai  — `AsyncOpenAI()` with no arguments, exactly as before. The
                  SDK auto-discovers OPENAI_BASE_URL / OPENAI_API_KEY, and the
                  corporate COF proxy resolves transparently in-network.
  bedrock       — Claude on Amazon Bedrock via the Anthropic SDK's Bedrock
                  client, authenticating off the standard AWS credential
                  chain (AWS_PROFILE, env vars, instance role).

The streaming multi-agent orchestrator does NOT go through here — it is built
on the `openai-agents` SDK, which is coupled to the OpenAI wire format.
Routing that to Bedrock needs a compatibility shim and is separate work.

Setup problems raise `LlmNotConfigured` so routers can answer 503
(setup-required) instead of 502 (upstream failed) — the distinction matters
when the fix is "run aws configure", not "retry".
"""
from __future__ import annotations

import json
import os
import re

from cof.llm_config import bedrock_region, provider, resolve_model

# Generous default: on current Claude models thinking is on by default and
# `max_tokens` caps thinking + visible text together, so a tight budget
# truncates the answer mid-sentence rather than erroring.
DEFAULT_MAX_TOKENS = 16000

_JSON_ONLY_INSTRUCTION = (
    "\n\nRespond with a single valid JSON object and nothing else. "
    "No preamble, no commentary, no markdown code fences."
)


class LlmNotConfigured(RuntimeError):
    """The provider is unreachable for a setup reason the operator can fix."""


async def complete(
    system: str,
    user: str,
    *,
    model: str | None = None,
    json_object: bool = False,
    temperature: float | None = 0.2,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Run one completion and return its text.

    `model` is passed through `resolve_model()`, so callers keep using the
    same `os.getenv("CMA_TOOL_DRAFT_MODEL")` convention regardless of provider.
    `json_object` asks for a bare JSON object; use `complete_json()` if you
    want it parsed. `temperature` is honored on OpenAI and dropped on Bedrock
    (current Claude models reject sampling parameters outright).
    """
    resolved = resolve_model(model)
    if provider() == "bedrock":
        return await _complete_bedrock(
            system, user, model=resolved, json_object=json_object,
            temperature=temperature, max_tokens=max_tokens,
        )
    return await _complete_openai(
        system, user, model=resolved, json_object=json_object,
        temperature=temperature,
    )


async def complete_json(system: str, user: str, **kwargs) -> dict:
    """`complete()` in JSON mode, parsed. Raises ValueError on non-JSON."""
    raw = await complete(system, user, json_object=True, **kwargs)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"model returned non-JSON: {e}") from e


# ── OpenAI / COF ──────────────────────────────────────────────────────────
async def _complete_openai(
    system: str,
    user: str,
    *,
    model: str,
    json_object: bool,
    temperature: float | None,
) -> str:
    try:
        from openai import AsyncOpenAI
    except ImportError as e:
        raise LlmNotConfigured("openai package not installed. Run `uv sync`.") from e

    client = AsyncOpenAI()

    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}
    if temperature is not None:
        kwargs["temperature"] = temperature

    completion = await client.chat.completions.create(**kwargs)
    return (completion.choices[0].message.content or "").strip()


# ── Bedrock ───────────────────────────────────────────────────────────────
async def _complete_bedrock(
    system: str,
    user: str,
    *,
    model: str,
    json_object: bool,
    temperature: float | None,
    max_tokens: int,
) -> str:
    """One completion via Bedrock's Converse API.

    Converse is deliberately model-agnostic — the same request shape reaches
    Nova, Claude, Llama and Mistral — so switching models is a `modelId`
    change and nothing else. That is why this path uses boto3 rather than a
    vendor-specific SDK client.
    """
    try:
        import boto3
    except ImportError as e:
        raise LlmNotConfigured(
            "CMA_LLM_PROVIDER=bedrock but boto3 is not installed. "
            "Run `uv sync` in backend/."
        ) from e

    if json_object:
        system = system + _JSON_ONLY_INSTRUCTION

    inference_config: dict = {"maxTokens": max_tokens}
    # Current Claude models reject sampling parameters outright; Nova and the
    # rest accept them. Send temperature only where it is legal.
    if temperature is not None and not model.startswith("anthropic."):
        inference_config["temperature"] = temperature

    request: dict = {
        "modelId": model,
        "messages": [{"role": "user", "content": [{"text": user}]}],
        "inferenceConfig": inference_config,
    }
    if system:
        request["system"] = [{"text": system}]

    # boto3 is sync; these endpoints are low-frequency (an analyst clicking
    # Draft or Extract), so a worker thread is the right trade against
    # maintaining a second async code path.
    import anyio

    def _call() -> tuple[str, str]:
        client = boto3.client("bedrock-runtime", region_name=bedrock_region())
        response = client.converse(**request)
        # Content can include non-text blocks (reasoning, tool use); keep text.
        blocks = response["output"]["message"]["content"]
        text = "".join(b["text"] for b in blocks if "text" in b).strip()
        return text, response.get("stopReason", "")

    try:
        text, stop_reason = await anyio.to_thread.run_sync(_call)
    except LlmNotConfigured:
        raise
    except Exception as e:
        # Output ceilings are per-model and change as models ship, so rather
        # than carry a table that rots, take the limit from the model's own
        # complaint and retry once.
        cap = _parse_max_token_limit(e)
        if cap is None or cap >= inference_config["maxTokens"]:
            raise _explain_bedrock_failure(e) from e
        inference_config["maxTokens"] = cap
        try:
            text, stop_reason = await anyio.to_thread.run_sync(_call)
        except Exception as retry_exc:
            raise _explain_bedrock_failure(retry_exc) from retry_exc

    if stop_reason == "max_tokens" and json_object:
        # Truncated JSON is unparseable downstream; fail with the real cause
        # rather than a confusing decode error two frames up.
        raise RuntimeError(
            f"model hit the {max_tokens}-token cap before closing the JSON "
            "object — raise max_tokens for this call site"
        )

    if json_object:
        text = _strip_code_fence(text)
    return text


def _explain_bedrock_failure(exc: Exception) -> Exception:
    """Turn the common first-run Bedrock failures into actionable messages.

    Matching on message text rather than exception class: the underlying
    errors come from botocore via the SDK and the class surface is not a
    stable contract, but these operator-facing strings are distinctive.
    """
    text = f"{type(exc).__name__}: {exc}"
    lowered = text.lower()

    if (
        "unable to locate credentials" in lowered
        or "could not find credentials" in lowered
        or "nocredentials" in lowered
    ):
        profile = os.getenv("AWS_PROFILE", "").strip()
        hint = (
            f"AWS_PROFILE={profile} is set — check that profile exists in "
            "~/.aws/credentials."
            if profile
            else "Run `aws configure --profile cma-lab` and set "
                 "AWS_PROFILE=cma-lab in backend/.env."
        )
        return LlmNotConfigured(f"No AWS credentials found for Bedrock. {hint}")

    # AccessDenied covers two very different causes: the IAM caller lacks the
    # action, or the account has no agreement for that model family. The
    # sales-support URL is Bedrock's tell for the latter.
    if "sales-support" in lowered or "not available for this account" in lowered:
        return LlmNotConfigured(
            "Bedrock has not granted this account access to this model. Check "
            "`aws bedrock get-foundation-model-availability --model-id <id>`: "
            "if agreementAvailability is NOT_AVAILABLE, the model family needs "
            "a use-case agreement (Anthropic models require one). Point "
            f"CMA_BEDROCK_MODEL at a granted model instead. ({text})"
        )

    if "accessdenied" in lowered or "not authorized" in lowered:
        return LlmNotConfigured(
            "AWS credentials work but lack Bedrock permissions. The caller "
            "needs bedrock:InvokeModel on this model in "
            f"{bedrock_region()}. ({text})"
        )

    if "permission_error" in lowered:
        return LlmNotConfigured(
            "Bedrock has not granted this account access to the model. Check "
            "`aws bedrock get-foundation-model-availability --model-id <id>`: "
            "if agreementAvailability is NOT_AVAILABLE, submit the Anthropic "
            "use-case form under Bedrock → Model access in the console for "
            f"region {bedrock_region()}. ({text})"
        )

    if "don't have access to the model" in lowered:
        return LlmNotConfigured(
            "Bedrock reports no access to this model in "
            f"{bedrock_region()}. Point CMA_BEDROCK_MODEL at a model whose "
            "agreementAvailability is AVAILABLE. ({text})"
        )

    if "validationexception" in lowered:
        # Could be a bad id, an unsupported parameter, or a limit — Bedrock's
        # own message is more specific than anything we could guess, so lead
        # with it rather than asserting a cause.
        return LlmNotConfigured(f"Bedrock rejected the request: {text}")

    if "resourcenotfound" in lowered or ("404" in text and "not_found" in lowered):
        return LlmNotConfigured(
            "Bedrock does not recognise that model id in "
            f"{bedrock_region()}. Confirm it with `aws bedrock "
            "list-foundation-models`. Some models are only reachable through a "
            "cross-region inference profile — the region-prefixed form, e.g. "
            f"us.amazon.nova-pro-v1:0. ({text})"
        )

    return exc


_MAX_TOKEN_LIMIT_RE = re.compile(r"model limit of (\d+)")


def _parse_max_token_limit(exc: Exception) -> int | None:
    """Extract the model's output ceiling from a Bedrock validation error.

    Bedrock answers an over-large maxTokens with the actual limit, e.g.
    "The maximum tokens you requested exceeds the model limit of 10000."
    """
    match = _MAX_TOKEN_LIMIT_RE.search(str(exc))
    return int(match.group(1)) if match else None


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _strip_code_fence(text: str) -> str:
    """Unwrap a ```json fenced block if the model produced one anyway."""
    match = _FENCE_RE.match(text.strip())
    return match.group(1).strip() if match else text

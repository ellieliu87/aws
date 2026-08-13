"""LLM model selection across COF, direct-OpenAI, and Bedrock environments.

Inside the Capital One COF proxy environment the corporate `openai` SDK fork
auto-resolves the endpoint and auth, and `gpt-oss-120b` is the default model.
Outside that environment we fall back to direct OpenAI, where `gpt-oss-120b`
does not exist on the model catalog — `gpt-4o` is the equivalent default.

A third mode routes to Amazon Bedrock. It is opt-in via
``CMA_LLM_PROVIDER=bedrock`` (never auto-detected — an AWS credential lying
around in the environment must not silently redirect the corporate proxy).
Bedrock reaches every model through one model-agnostic Converse API, so the
mode is not Claude-specific: Nova, Claude, Llama and Mistral are all just
different `modelId` values.

Bedrock ids are namespaced by provider (`amazon.`, `anthropic.`, …) or by an
inference-profile region prefix (`us.`, `eu.`, …), so no OpenAI-shaped name is
ever a valid Bedrock target; `resolve_model()` maps those onto the Bedrock
default instead of passing them through to a guaranteed failure.

Skill `.md` frontmatter declares `model: gpt-oss-120b` (the COF default).
`resolve_model()` swaps that to the mode's equivalent default, leaving any
explicitly-non-default model name (e.g. a skill that pins `gpt-4o-mini`, or
one that pins `us.amazon.nova-pro-v1:0`) alone.
"""
from __future__ import annotations

import os

COF_DEFAULT_MODEL = "gpt-oss-120b"
OPENAI_DEFAULT_MODEL = "gpt-4o"
# Aspirational default: correct once the account's Anthropic use-case
# agreement is granted. Until then set CMA_BEDROCK_MODEL to a granted model
# (e.g. us.amazon.nova-pro-v1:0) — removing that one line is the upgrade.
BEDROCK_DEFAULT_MODEL = "anthropic.claude-opus-5"
# Bedrock ids are either provider-namespaced or prefixed with an
# inference-profile region. Used to tell "the caller already gave me a Bedrock
# id" from "the caller gave me an OpenAI id".
BEDROCK_ID_PREFIXES = (
    # provider namespaces
    "amazon.", "anthropic.", "ai21.", "cohere.", "deepseek.", "meta.",
    "mistral.", "openai.", "qwen.", "stability.", "writer.",
    # cross-region inference profiles
    "us.", "us-gov.", "eu.", "apac.",
)


def looks_like_bedrock_id(name: str) -> bool:
    """True when `name` is shaped like a Bedrock modelId or inference profile."""
    return name.startswith(BEDROCK_ID_PREFIXES)


def is_bedrock_mode() -> bool:
    """True when the backend should talk to Claude on Amazon Bedrock.

    Explicit opt-in only. Set ``CMA_LLM_PROVIDER=bedrock`` in `backend/.env`.
    """
    return os.getenv("CMA_LLM_PROVIDER", "").strip().lower() == "bedrock"


def bedrock_default_model() -> str:
    return os.getenv("CMA_BEDROCK_MODEL", "").strip() or BEDROCK_DEFAULT_MODEL


def bedrock_region() -> str:
    """Region for the Bedrock endpoint.

    Falls back through the standard AWS env vars so an existing profile-based
    setup keeps working; us-east-1 has the widest Claude model availability.
    """
    for var in ("CMA_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return "us-east-1"


def provider() -> str:
    """Which of the three connection modes is active: bedrock|openai|cof."""
    if is_bedrock_mode():
        return "bedrock"
    return "openai" if is_openai_direct_mode() else "cof"


def is_openai_direct_mode() -> bool:
    """True when the OpenAI SDK will hit api.openai.com directly.

    Heuristic: an `sk-`-prefixed `OPENAI_API_KEY` with no `OPENAI_BASE_URL`
    or `COF_BASE_URL` override means we're outside the COF proxy. Inside
    COF the corporate SDK fork preconfigures the endpoint and these env
    vars are typically empty.
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key.startswith("sk-"):
        return False
    if os.getenv("OPENAI_BASE_URL", "").strip():
        return False
    if os.getenv("COF_BASE_URL", "").strip():
        return False
    return True


def default_model() -> str:
    if is_bedrock_mode():
        return bedrock_default_model()
    return OPENAI_DEFAULT_MODEL if is_openai_direct_mode() else COF_DEFAULT_MODEL


def resolve_model(declared: str | None) -> str:
    """Return the model to actually use, given a declaration from skill
    YAML or an env override. Swaps the COF default to the active mode's
    equivalent default; passes any other explicit choice through.
    """
    if not declared:
        return default_model()
    if is_bedrock_mode():
        # An OpenAI-shaped id on Bedrock is a guaranteed failure, so map
        # anything that isn't already a Bedrock id onto the default. A
        # caller that pins a real Bedrock id (a cheaper model, say) wins.
        return declared if looks_like_bedrock_id(declared) else bedrock_default_model()
    if declared == COF_DEFAULT_MODEL and is_openai_direct_mode():
        return OPENAI_DEFAULT_MODEL
    return declared

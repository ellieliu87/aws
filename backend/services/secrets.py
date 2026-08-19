"""Provider credentials from SSM Parameter Store, rather than from a file.

`backend/.env` is read before anything else in `main.py` so that
`OPENAI_API_KEY` is set by the time `cof.orchestrator` is imported. That works,
and it is also the one real exposure left in this design: a plaintext provider
key sitting in a file beside the code, copied onto every machine that runs the
backend, and one `git add -A` away from a repository.

This is the seam that removes it. Parameters live under a path in SSM
Parameter Store as **SecureString** — KMS-encrypted, free at any volume this
app will reach — and are pulled into the process environment at boot, before
the modules that read them are imported. Nothing downstream changes: the
`openai` SDK still auto-discovers `OPENAI_API_KEY` from the environment,
exactly as it does today.

Parameter Store rather than Secrets Manager on purpose. Secrets Manager's
$0.40/secret/month buys rotation machinery, which is worth paying for
credentials that *can* be rotated automatically. A provider API key is not one
of those: rotating it means logging into the provider's console.

**Naming.** The last segment of the parameter path becomes the environment
variable, so the mapping is visible in the console without reading any code:

    /cma/workbench/OPENAI_API_KEY   ->  OPENAI_API_KEY
    /cma/workbench/OPENAI_BASE_URL  ->  OPENAI_BASE_URL

**Precedence** is the part worth stating out loud, because it decides which
copy of a key wins when there are two. A variable already exported in the real
process environment is never overwritten — that is a developer or a container
runtime being explicit, and it must beat a remote value they may not know
about. Everything else loses to SSM, `backend/.env` included, because the whole
point of this module is that the file stops being the source of truth.

**Non-secrets stay where they are.** `CMA_STATE_TABLE`, the queue URL, the
ARNs — `infra/sync_env.py` already distributes those from stack outputs, and
moving them here would be a lateral move that buys nothing.

Opt-in like everything else: with `CMA_SECRETS_PREFIX` unset this is a no-op
and the app reads its key from `.env` as before, so a developer with no AWS
account is unaffected.

Note the limit of what this module fixes. Reading the parameter needs AWS
credentials of its own, so on a laptop it trades a plaintext key for an AWS
profile.

In the deployed system none of this code runs, which is the point rather than
an oversight. `CMA_SECRETS_PREFIX` is deliberately absent from the ECS task's
environment (see `web_env` in `infra/cdk/async_stack.py`), so `enabled()` is
False and the app never calls SSM. The *execution* role — held by the ECS
agent, not by the container — resolves the SecureString before the process
starts and injects it as an ordinary environment variable. The task role the
application actually runs under has no `ssm:GetParameter` at all: it can reach
DynamoDB, SQS, Step Functions, S3, Cognito and Bedrock, and cannot read the
credential it is using. That separation is the property worth keeping — the
secret is delivered by an identity the application cannot impersonate.
"""
from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable

log = logging.getLogger("cma.secrets")

# A parameter's name has to be usable as an environment variable, and this is
# cheap insurance against a typo'd path quietly creating something strange:
# `/cma/workbench/openai-api-key` is a mistake, not a variable, and silently
# ignoring it is worse than saying so.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def prefix() -> str:
    """The Parameter Store path to read, e.g. `/cma/workbench`."""
    return os.getenv("CMA_SECRETS_PREFIX", "").strip()


def enabled() -> bool:
    return bool(prefix())


def _path() -> str:
    """Normalise to the leading-slash, no-trailing-slash form SSM wants."""
    return "/" + prefix().strip("/")


def _client():
    # Region resolved the same way services/blob_store.py resolves it, so one
    # setting covers every AWS client in the app. `cof.llm_config` is safe to
    # import here even at this point in boot — it reads env vars and nothing
    # else, and `cof/__init__.py` is empty, so it does not drag the
    # orchestrator in ahead of the key this function exists to set.
    import boto3

    from cof.llm_config import bedrock_region

    region = os.getenv("CMA_JOBS_REGION", "").strip() or bedrock_region()
    return boto3.client("ssm", region_name=region)


def load_into_env(preset: Iterable[str] = ()) -> list[str]:
    """Pull every parameter under the prefix into `os.environ`.

    `preset` is the set of variable names that were already in the real
    environment before `backend/.env` was read — those are left alone. The
    caller has to snapshot it, because by the time this runs `load_dotenv` has
    already blurred the two sources together.

    Returns the names that were set, for logging. Never returns or logs a
    value.

    A failure here is deliberately not fatal. An unreachable Parameter Store,
    a missing permission, or a wrong region should leave the app starting with
    whatever `.env` provided and complaining loudly — the same shape as every
    other AWS seam in this codebase. The alternative is a backend that refuses
    to boot because of a service it is only optionally wired to.
    """
    if not enabled():
        return []

    protected = set(preset)
    loaded: list[str] = []
    try:
        paginator = _client().get_paginator("get_parameters_by_path")
        for page in paginator.paginate(
            Path=_path(), Recursive=True, WithDecryption=True
        ):
            for param in page.get("Parameters", []):
                name = param["Name"].rsplit("/", 1)[-1]
                if not _ENV_NAME.match(name):
                    log.warning(
                        "ignoring %s: the last path segment is not a usable "
                        "environment variable name", param["Name"],
                    )
                    continue
                if name in protected:
                    log.info(
                        "%s is already set in the environment — leaving it; "
                        "the parameter under %s was not applied", name, _path(),
                    )
                    continue
                os.environ[name] = param["Value"]
                loaded.append(name)
    except Exception as e:
        log.warning("could not read parameters under %s: %s", _path(), e)

    return loaded


def status() -> dict:
    info: dict = {"enabled": enabled(), "prefix": prefix() or "(unset, using .env)"}
    if enabled():
        try:
            paginator = _client().get_paginator("get_parameters_by_path")
            # Names only — WithDecryption is deliberately omitted, so this
            # cannot put a secret anywhere near a status response.
            names = [
                param["Name"].rsplit("/", 1)[-1]
                for page in paginator.paginate(Path=_path(), Recursive=True)
                for param in page.get("Parameters", [])
            ]
            info["reachable"] = True
            info["parameters"] = sorted(names)
        except Exception as e:
            info["reachable"] = False
            info["error"] = f"{type(e).__name__}: {e}"
    return info

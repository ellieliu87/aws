"""Versioned registry of compiled workflow plans.

Implements three of the design decisions around canvas compilation:

  Compile on save      the analyst learns immediately that their diagram is
                       broken, instead of discovering it at run time.

  The API creates no   saving does not deploy. It records a plan and marks it
  AWS resources        pending, and a separate privileged process
                       (infra/deploy_workflows.py) does the deploying. The web
                       tier needs s3:PutObject and nothing that can create
                       infrastructure.

  Keep every version   each distinct plan is written to its own key and never
                       overwritten, so "which calculation produced the number
                       we filed?" has an exact answer months later. This is the
                       same argument that put versioning on the corpus bucket,
                       applied to workflows.

Plans live in S3 under plans/<workflow_id>/, in the bucket that already has
versioning enabled. That is deliberate: an audit trail kept in a process's
memory is not an audit trail.

A save that does not change the compiled plan does not create a version. Version
numbers should mean "the executable plan changed", not "someone dragged a box".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from services.workflow_compiler import compile_workflow

log = logging.getLogger("cma.workflow_plans")

PREFIX = "plans/"
# A placeholder ARN keeps compilation independent of deployment: the plan is
# validated and versioned without needing to know where it will run. The
# deployer substitutes the real ARN, and that substitution is not part of the
# plan's identity — otherwise redeploying to a new account would look like a
# new version of the analyst's workflow, which it is not.
SOLVER_PLACEHOLDER = "${SOLVER_FUNCTION_ARN}"


class PlanRegistryUnavailable(RuntimeError):
    """No corpus bucket configured, so plans cannot be persisted."""


def bucket() -> str:
    return os.getenv("CMA_CORPUS_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


def _client():
    import boto3

    from cof.llm_config import bedrock_region

    region = os.getenv("CMA_CORPUS_REGION", "").strip() or bedrock_region()
    return boto3.client("s3", region_name=region)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _key(workflow_id: str, name: str) -> str:
    return f"{PREFIX}{workflow_id}/{name}"


def _read(key: str) -> dict | None:
    try:
        return json.loads(_client().get_object(Bucket=bucket(), Key=key)["Body"].read())
    except Exception:
        return None


def _write(key: str, payload: dict) -> None:
    _client().put_object(
        Bucket=bucket(), Key=key,
        Body=json.dumps(payload, indent=2, sort_keys=True).encode(),
        ContentType="application/json",
    )


def _definition_hash(definition: dict) -> str:
    """Fingerprint the *executable* content only.

    `Comment` carries the workflow's display name, so including it would make
    renaming a workflow look like a new plan. A version must mean "what this
    thing does changed", or the audit trail becomes noise nobody reads.
    """
    executable = {k: v for k, v in definition.items() if k != "Comment"}
    canonical = json.dumps(executable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── registering ───────────────────────────────────────────────────────────
def compile_and_register(workflow: dict[str, Any]) -> dict[str, Any]:
    """Compile a canvas and record the plan. Raises WorkflowCompileError.

    Returns the plan record. Deliberately does NOT deploy: see the module
    docstring. Callers in the API layer surface a compile error to the analyst
    as a 400 so they see it while the diagram is still in front of them.
    """
    if not enabled():
        raise PlanRegistryUnavailable(
            "CMA_CORPUS_BUCKET is not set, so compiled plans cannot be stored"
        )

    workflow_id = str(workflow.get("id") or "")
    if not workflow_id:
        raise ValueError("workflow has no id")

    from services.workflow_artifacts import resolve

    # Bind model nodes to real artifacts where they resolve. Part of the plan's
    # identity on purpose: pointing a node at a different model changes what
    # runs, so it has to change the version.
    artifacts = resolve(workflow.get("nodes") or [])
    definition = compile_workflow(
        workflow,
        solver_arn=SOLVER_PLACEHOLDER,
        require_approval=bool(workflow.get("require_approval", True)),
        artifacts=artifacts,
    )
    fingerprint = _definition_hash(definition)

    index = _read(_key(workflow_id, "index.json")) or {"versions": []}
    if index["versions"] and index["versions"][-1]["hash"] == fingerprint:
        # Cosmetic edits (a moved box, a renamed workflow) do not change the
        # executable plan, so they do not earn a version.
        latest = index["versions"][-1]
        log.info("workflow %s unchanged at v%s", workflow_id, latest["version"])
        return {**latest, "unchanged": True}

    version = len(index["versions"]) + 1
    plan = {
        "workflow_id": workflow_id,
        "workflow_name": workflow.get("name"),
        "version": version,
        "hash": fingerprint,
        "require_approval": bool(workflow.get("require_approval", True)),
        "compiled_at": _now(),
        "state_count": len(definition["States"]),
        # Recorded so the audit trail says which artifact each node ran, not
        # just that the graph had a node.
        "artifacts": artifacts,
        "definition": definition,
        "status": "pending_deploy",
    }
    _write(_key(workflow_id, f"v{version}.json"), plan)

    index["versions"].append({
        "version": version, "hash": fingerprint,
        "compiled_at": plan["compiled_at"], "status": "pending_deploy",
        "state_count": plan["state_count"],
    })
    index["workflow_id"] = workflow_id
    index["latest"] = version
    _write(_key(workflow_id, "index.json"), index)

    log.info("workflow %s compiled to v%s (%s states, pending deploy)",
             workflow_id, version, plan["state_count"])
    return {**plan, "unchanged": False}


def request_delete(workflow_id: str) -> dict:
    """Mark a workflow's machine for removal by the deployer.

    Same reasoning as deployment: the API asks, the privileged process acts.
    The plan history is left in place — deleting a workflow should not erase
    the record of what it used to do.
    """
    if not enabled():
        return {"skipped": "CMA_CORPUS_BUCKET not set"}
    index = _read(_key(workflow_id, "index.json")) or {"workflow_id": workflow_id,
                                                       "versions": []}
    index["delete_requested_at"] = _now()
    _write(_key(workflow_id, "index.json"), index)
    return {"workflow_id": workflow_id, "delete_requested": True}


# ── reading ───────────────────────────────────────────────────────────────
def get_index(workflow_id: str) -> dict | None:
    return _read(_key(workflow_id, "index.json"))


def get_plan(workflow_id: str, version: int | None = None) -> dict | None:
    """A specific plan version, or the latest."""
    if version is None:
        index = get_index(workflow_id)
        if not index or not index.get("latest"):
            return None
        version = index["latest"]
    return _read(_key(workflow_id, f"v{version}.json"))


def list_workflow_ids() -> list[str]:
    """Every workflow with a registered plan — used by the cleanup sweep."""
    if not enabled():
        return []
    found: set[str] = set()
    paginator = _client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=PREFIX, Delimiter="/"):
        for item in page.get("CommonPrefixes", []):
            found.add(item["Prefix"][len(PREFIX):].rstrip("/"))
    return sorted(found)


def pending() -> list[dict]:
    """Plans awaiting deployment, plus workflows awaiting deletion."""
    work: list[dict] = []
    for workflow_id in list_workflow_ids():
        index = get_index(workflow_id) or {}
        if index.get("delete_requested_at"):
            work.append({"workflow_id": workflow_id, "action": "delete"})
            continue
        for entry in index.get("versions", []):
            if entry.get("status") == "pending_deploy":
                work.append({"workflow_id": workflow_id, "action": "deploy",
                             "version": entry["version"]})
    return work


def mark_superseded(workflow_id: str, version: int) -> None:
    """An older pending version that a newer one overtook before deploying.

    Recorded rather than silently dropped: "compiled but never deployed" is a
    real state and pretending it deployed would put a false entry in the audit
    trail.
    """
    plan = get_plan(workflow_id, version)
    if plan:
        plan["status"] = "superseded"
        plan["superseded_at"] = _now()
        _write(_key(workflow_id, f"v{version}.json"), plan)

    index = get_index(workflow_id) or {"versions": []}
    for entry in index.get("versions", []):
        if entry["version"] == version:
            entry["status"] = "superseded"
    _write(_key(workflow_id, "index.json"), index)


def mark_deployed(workflow_id: str, version: int, state_machine_arn: str) -> None:
    plan = get_plan(workflow_id, version)
    if plan:
        plan["status"] = "deployed"
        plan["deployed_at"] = _now()
        plan["state_machine_arn"] = state_machine_arn
        _write(_key(workflow_id, f"v{version}.json"), plan)

    index = get_index(workflow_id) or {"versions": []}
    for entry in index.get("versions", []):
        if entry["version"] == version:
            entry["status"] = "deployed"
            entry["deployed_at"] = plan["deployed_at"] if plan else _now()
    index["deployed"] = version
    _write(_key(workflow_id, "index.json"), index)


def mark_deleted(workflow_id: str) -> None:
    index = get_index(workflow_id) or {"versions": []}
    index.pop("delete_requested_at", None)
    index["deleted_at"] = _now()
    index["deployed"] = None
    _write(_key(workflow_id, "index.json"), index)

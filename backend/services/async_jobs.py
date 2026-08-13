"""Client for the async solve path and the playbook state machine.

Two things live here, mirroring the two shapes of work the workbench does:

  enqueue / get_result       one heavy solve, fire and poll
  start_playbook / approve   a multi-phase run with a human gate in the middle

Both are opt-in. Without CMA_JOBS_QUEUE_URL and CMA_PLAYBOOK_STATE_MACHINE the
functions report themselves unavailable and callers keep using the in-process
path, so nothing here is load-bearing for a developer without AWS.

The API contract this enables is the important part: submit returns a job id
immediately instead of holding the connection open for the length of a solve.
`services/model_runner.py` stays as the synchronous path — small models called
interactively are better served in-process than through a queue round trip.
Which path a request takes is a routing decision, not a rewrite.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

log = logging.getLogger("cma.async_jobs")

RESULT_PREFIX = "jobs/"


class JobsNotConfigured(RuntimeError):
    """The async path isn't provisioned — run infra/provision_async.py apply."""


def queue_url() -> str:
    return os.getenv("CMA_JOBS_QUEUE_URL", "").strip()


def state_machine_arn() -> str:
    return os.getenv("CMA_PLAYBOOK_STATE_MACHINE", "").strip()


def bucket() -> str:
    return os.getenv("CMA_CORPUS_BUCKET", "").strip()


def enabled() -> bool:
    return bool(queue_url() and bucket())


def playbooks_enabled() -> bool:
    return bool(state_machine_arn() and bucket())


def _region() -> str:
    from cof.llm_config import bedrock_region

    return os.getenv("CMA_JOBS_REGION", "").strip() or bedrock_region()


def _client(service: str):
    import boto3

    return boto3.client(service, region_name=_region())


# ── one-shot solves ───────────────────────────────────────────────────────
def enqueue(kind: str, params: dict[str, Any], job_id: str | None = None) -> str:
    """Put a solve on the queue and return its job id immediately."""
    if not enabled():
        raise JobsNotConfigured(
            "CMA_JOBS_QUEUE_URL / CMA_CORPUS_BUCKET not set. Run "
            "`python -m infra.provision_async apply` from backend/."
        )
    job_id = job_id or f"job-{uuid.uuid4().hex[:12]}"
    body = {"job_id": job_id, "kind": kind, "params": params}
    _client("sqs").send_message(QueueUrl=queue_url(), MessageBody=json.dumps(body))
    log.info("enqueued %s (%s)", job_id, kind)
    return job_id


def get_result(job_id: str) -> dict | None:
    """Fetch a finished job's result, or None while it is still running.

    None is the normal in-progress answer, not an error — the caller polls.
    """
    if not enabled():
        raise JobsNotConfigured("async jobs not configured")
    try:
        response = _client("s3").get_object(
            Bucket=bucket(), Key=f"{RESULT_PREFIX}{job_id}.json"
        )
    except Exception as e:
        if getattr(e, "response", {}).get("Error", {}).get("Code") in (
            "NoSuchKey", "404", "AccessDenied",
        ):
            return None
        raise
    return json.loads(response["Body"].read())


def queue_depth() -> dict:
    if not enabled():
        return {"enabled": False}
    attrs = _client("sqs").get_queue_attributes(
        QueueUrl=queue_url(),
        AttributeNames=["ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return {
        "enabled": True,
        "waiting": int(attrs["ApproximateNumberOfMessages"]),
        "in_flight": int(attrs["ApproximateNumberOfMessagesNotVisible"]),
    }


# ── playbook runs with an approval gate ───────────────────────────────────
def start_playbook(run_id: str, params: dict[str, Any] | None = None) -> str:
    """Start a playbook execution. Returns the execution ARN."""
    if not playbooks_enabled():
        raise JobsNotConfigured(
            "CMA_PLAYBOOK_STATE_MACHINE not set. Run "
            "`python -m infra.provision_async apply` from backend/."
        )
    return _client("stepfunctions").start_execution(
        stateMachineArn=state_machine_arn(),
        name=f"{run_id}-{uuid.uuid4().hex[:8]}",
        input=json.dumps({"run_id": run_id, "params": params or {}}),
    )["executionArn"]


def describe_run(execution_arn: str) -> dict:
    execution = _client("stepfunctions").describe_execution(executionArn=execution_arn)
    return {
        "status": execution["status"],
        "started_at": execution.get("startDate"),
        "stopped_at": execution.get("stopDate"),
        "output": json.loads(execution["output"]) if execution.get("output") else None,
    }


def pending_approval(run_id: str) -> dict | None:
    """The approval request parked by the state machine, if there is one.

    Holds the Step Functions task token. An execution waiting here is durable
    — it survives API restarts and deploys, which a thread blocked in a web
    worker does not.
    """
    if not playbooks_enabled():
        raise JobsNotConfigured("playbook state machine not configured")
    try:
        response = _client("s3").get_object(
            Bucket=bucket(), Key=f"{RESULT_PREFIX}approvals/{run_id}.json"
        )
    except Exception:
        return None
    return json.loads(response["Body"].read())


def approve(run_id: str, approved_by: str) -> dict:
    """Release a parked execution by answering its task token."""
    request = pending_approval(run_id)
    if not request:
        raise JobsNotConfigured(f"no approval pending for {run_id}")
    _client("stepfunctions").send_task_success(
        taskToken=request["task_token"],
        output=json.dumps({"approved_by": approved_by, "run_id": run_id}),
    )
    log.info("approved %s by %s", run_id, approved_by)
    return {"run_id": run_id, "approved_by": approved_by}


def reject(run_id: str, rejected_by: str, reason: str = "") -> dict:
    """Fail a parked execution instead of releasing it."""
    request = pending_approval(run_id)
    if not request:
        raise JobsNotConfigured(f"no approval pending for {run_id}")
    _client("stepfunctions").send_task_failure(
        taskToken=request["task_token"],
        error="ApprovalRejected",
        cause=json.dumps({"rejected_by": rejected_by, "reason": reason}),
    )
    return {"run_id": run_id, "rejected_by": rejected_by, "reason": reason}


def status() -> dict:
    return {
        "solves_enabled": enabled(),
        "playbooks_enabled": playbooks_enabled(),
        "region": _region(),
        "queue": queue_depth(),
        "state_machine": state_machine_arn() or "(unset)",
    }

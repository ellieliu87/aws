"""Provision the async solve path: SQS -> Lambda -> S3, orchestrated by Step Functions.

Why this exists
---------------
`services/model_runner.py` forks a subprocess with a 30-second wall clock and
runs it inside the HTTP request. The instinct is right — isolate the untrusted
artifact — but the layer is wrong: a heavy solve still occupies a web worker,
and a spike in solves degrades the API for everyone. This moves the work off
the request path entirely.

Why a script and not CDK
------------------------
CDK or Terraform is the right answer for anything a team maintains, and it is
where this should end up. A single idempotent script gets the same two
properties that matter most here — the infrastructure is versioned in the repo,
and it can be torn down completely — without a bootstrap detour. `--destroy`
is not a convenience; on a free-tier account the resource you forget is the
one that bills.

Usage (from backend/):
    python -m infra.provision_async status
    python -m infra.provision_async apply
    python -m infra.provision_async destroy
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

PREFIX = "cma-workbench"
QUEUE = f"{PREFIX}-solves"
DLQ = f"{PREFIX}-solves-dlq"
FUNCTION = f"{PREFIX}-solver"
LAMBDA_ROLE = f"{PREFIX}-solver-role"
SFN_ROLE = f"{PREFIX}-playbook-role"
STATE_MACHINE = f"{PREFIX}-playbook"
RUNTIME = "python3.12"

# The worker. Kept inline so the deployed artifact is reviewable in the same
# file as the infrastructure that creates it — for a single small handler that
# beats a separate build step.
LAMBDA_SOURCE = '''
"""Async solve worker.

Three modes, so one function backs both the queue consumer and the two
Step Functions task types:

  solve            — run the job, write the result to S3
  request_approval — persist the Step Functions task token to S3 and return,
                     leaving the execution paused until someone approves
  publish          — mark the run published after approval
"""
import json
import os
import time

import boto3

BUCKET = os.environ["RESULT_BUCKET"]
PREFIX = os.environ.get("RESULT_PREFIX", "jobs/")
s3 = boto3.client("s3")


def _put(key, payload):
    s3.put_object(
        Bucket=BUCKET, Key=key,
        Body=json.dumps(payload, default=str).encode(),
        ContentType="application/json",
    )


def _goal_seek(target, rate, guess, iterations):
    """Stand-in for the real quant engine: bisection toward a target balance.

    Deliberately CPU-bound and iterative — the point is that it runs here
    instead of inside a web request, not what it computes.
    """
    low, high = 0.0, max(guess * 4, target * 4, 1.0)
    history = []
    for i in range(iterations):
        mid = (low + high) / 2
        value = mid * (1 - rate) ** 12
        history.append({"iteration": i, "input": round(mid, 4), "output": round(value, 4)})
        if abs(value - target) < 1e-6:
            break
        if value < target:
            low = mid
        else:
            high = mid
    return {"solved_input": round((low + high) / 2, 4), "iterations": len(history),
            "history": history[-5:]}


def _solve(job):
    started = time.time()
    params = job.get("params", {})
    result = _goal_seek(
        target=float(params.get("target", 1_000_000)),
        rate=float(params.get("rate", 0.021)),
        guess=float(params.get("guess", 1_000_000)),
        iterations=int(params.get("iterations", 60)),
    )
    payload = {
        "job_id": job["job_id"],
        "status": "succeeded",
        "kind": job.get("kind", "goal_seek"),
        "result": result,
        "duration_ms": int((time.time() - started) * 1000),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _put(f"{PREFIX}{job['job_id']}.json", payload)
    return payload


def handler(event, context):
    # SQS delivery: one or more records, each a job envelope.
    if "Records" in event:
        out = []
        for record in event["Records"]:
            job = json.loads(record["body"])
            try:
                out.append(_solve(job))
            except Exception as e:
                _put(f"{PREFIX}{job.get('job_id', 'unknown')}.json",
                     {"job_id": job.get("job_id"), "status": "failed", "error": str(e)})
                raise
        return {"processed": len(out)}

    # Step Functions delivery.
    mode = event.get("mode", "solve")
    if mode == "request_approval":
        run_id = event["run_id"]
        _put(f"{PREFIX}approvals/{run_id}.json", {
            "run_id": run_id,
            "task_token": event["task_token"],
            "phase": event.get("phase", "champion_challenger"),
            "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "awaiting_approval",
        })
        # Returning without calling SendTaskSuccess is the point: the
        # execution stays parked here until a human decides.
        return {"awaiting_approval": run_id}

    if mode == "publish":
        run_id = event["run_id"]
        _put(f"{PREFIX}published/{run_id}.json", {
            "run_id": run_id, "status": "published",
            "approved_by": event.get("approved_by", "unknown"),
            "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        return {"published": run_id}

    return _solve(event)
'''


def _zip_source() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("handler.py", LAMBDA_SOURCE)
    return buffer.getvalue()


# ── context ───────────────────────────────────────────────────────────────
def _context() -> dict:
    import os

    from cof.llm_config import bedrock_region

    region = os.getenv("CMA_JOBS_REGION", "").strip() or bedrock_region()
    bucket = os.getenv("CMA_CORPUS_BUCKET", "").strip()
    if not bucket:
        raise SystemExit("CMA_CORPUS_BUCKET must be set — results are written there.")
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    return {"region": region, "bucket": bucket, "account": account}


def _arn(ctx: dict, service: str, resource: str) -> str:
    return f"arn:aws:{service}:{ctx['region']}:{ctx['account']}:{resource}"


# ── apply ─────────────────────────────────────────────────────────────────
def _ensure_queues(sqs, ctx) -> tuple[str, str]:
    dlq_url = sqs.create_queue(QueueName=DLQ)["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(
        QueueUrl=dlq_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    queue_url = sqs.create_queue(
        QueueName=QUEUE,
        Attributes={
            # Must exceed the Lambda timeout or SQS redelivers a message that
            # is still being processed.
            "VisibilityTimeout": "180",
            "MessageRetentionPeriod": "86400",
            "RedrivePolicy": json.dumps(
                {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 3}
            ),
        },
    )["QueueUrl"]
    print(f"  queue      {QUEUE}  (DLQ after 3 failures)")
    return queue_url, dlq_arn


def _ensure_role(iam, name: str, principal: str, policy: dict) -> str:
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": principal},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        arn = iam.create_role(
            RoleName=name, AssumeRolePolicyDocument=json.dumps(trust),
            Description="CMA Workbench async solve path",
        )["Role"]["Arn"]
        print(f"  role       {name}  (created)")
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        print(f"  role       {name}  (exists)")
    iam.put_role_policy(
        RoleName=name, PolicyName=f"{name}-inline",
        PolicyDocument=json.dumps(policy),
    )
    return arn


def _ensure_function(lam, ctx, role_arn: str, queue_arn: str) -> str:
    code = _zip_source()
    environment = {"Variables": {"RESULT_BUCKET": ctx["bucket"], "RESULT_PREFIX": "jobs/"}}
    try:
        lam.get_function(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=code)
        _wait_updated(lam)
        lam.update_function_configuration(
            FunctionName=FUNCTION, Role=role_arn, Timeout=120,
            MemorySize=512, Environment=environment,
        )
        _wait_updated(lam)
        print(f"  function   {FUNCTION}  (updated)")
    except lam.exceptions.ResourceNotFoundException:
        # IAM role creation is eventually consistent; Lambda rejects a role it
        # cannot yet see, so retry rather than fail the whole apply.
        for attempt in range(12):
            try:
                lam.create_function(
                    FunctionName=FUNCTION, Runtime=RUNTIME, Role=role_arn,
                    Handler="handler.handler", Code={"ZipFile": code},
                    Timeout=120, MemorySize=512, Environment=environment,
                    Description="CMA Workbench async solver",
                )
                print(f"  function   {FUNCTION}  (created after {attempt + 1} attempt(s))")
                break
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidParameterValueException":
                    raise
                time.sleep(5)
        else:
            raise SystemExit("Lambda creation kept failing on role propagation.")

    arn = lam.get_function(FunctionName=FUNCTION)["Configuration"]["FunctionArn"]

    existing = [
        m for m in lam.list_event_source_mappings(FunctionName=FUNCTION)["EventSourceMappings"]
        if m["EventSourceArn"] == queue_arn
    ]
    if not existing:
        lam.create_event_source_mapping(
            EventSourceArn=queue_arn, FunctionName=FUNCTION,
            BatchSize=1, Enabled=True,
        )
        print("  mapping    SQS -> Lambda  (created)")
    else:
        print("  mapping    SQS -> Lambda  (exists)")
    return arn


def _wait_updated(lam) -> None:
    for _ in range(30):
        state = lam.get_function(FunctionName=FUNCTION)["Configuration"]
        if state.get("LastUpdateStatus") != "InProgress":
            return
        time.sleep(2)


def _definition(function_arn: str) -> dict:
    """Playbook phases as a state machine, with approval as a real state.

    The champion/challenger gate is the reason this is Step Functions and not
    a loop in the API: an execution parked at ApprovalGate is durable, visible,
    and auditable for as long as the review takes. A thread blocked in a web
    process is none of those.
    """
    return {
        "Comment": "CMA Workbench playbook: solve, then human approval, then publish",
        "StartAt": "Solve",
        "States": {
            "Solve": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {
                    "FunctionName": function_arn,
                    "Payload": {
                        "mode": "solve",
                        "job_id.$": "$.run_id",
                        "params.$": "$.params",
                    },
                },
                "ResultSelector": {"result.$": "$.Payload.result"},
                "ResultPath": "$.solve",
                "Retry": [{
                    "ErrorEquals": ["Lambda.ServiceException", "Lambda.TooManyRequestsException",
                                    "States.TaskFailed"],
                    "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0,
                }],
                "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Failed",
                           "ResultPath": "$.error"}],
                "Next": "ApprovalGate",
            },
            "ApprovalGate": {
                "Type": "Task",
                # waitForTaskToken is what makes the pause durable: the
                # execution holds here until SendTaskSuccess arrives.
                "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
                "Parameters": {
                    "FunctionName": function_arn,
                    "Payload": {
                        "mode": "request_approval",
                        "run_id.$": "$.run_id",
                        "phase": "champion_challenger",
                        "task_token.$": "$$.Task.Token",
                    },
                },
                "ResultPath": "$.approval",
                "TimeoutSeconds": 86400,
                "Catch": [{"ErrorEquals": ["States.Timeout"], "Next": "ApprovalExpired",
                           "ResultPath": "$.error"}],
                "Next": "Publish",
            },
            "Publish": {
                "Type": "Task",
                "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {
                    "FunctionName": function_arn,
                    "Payload": {
                        "mode": "publish",
                        "run_id.$": "$.run_id",
                        "approved_by.$": "$.approval.approved_by",
                    },
                },
                "ResultPath": "$.publish",
                "End": True,
            },
            "ApprovalExpired": {"Type": "Fail", "Error": "ApprovalTimeout",
                                "Cause": "No reviewer approved within 24 hours"},
            "Failed": {"Type": "Fail", "Error": "SolveFailed",
                       "Cause": "The solve step failed after retries"},
        },
    }


def _ensure_state_machine(sfn, ctx, role_arn: str, function_arn: str) -> str:
    definition = json.dumps(_definition(function_arn))
    arn = _arn(ctx, "states", f"stateMachine:{STATE_MACHINE}")
    try:
        sfn.update_state_machine(stateMachineArn=arn, definition=definition,
                                 roleArn=role_arn)
        print(f"  machine    {STATE_MACHINE}  (updated)")
    except sfn.exceptions.StateMachineDoesNotExist:
        for attempt in range(12):
            try:
                arn = sfn.create_state_machine(
                    name=STATE_MACHINE, definition=definition, roleArn=role_arn,
                    type="STANDARD",
                )["stateMachineArn"]
                print(f"  machine    {STATE_MACHINE}  (created after {attempt + 1} attempt(s))")
                break
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("AccessDeniedException",
                                                       "InvalidArn"):
                    raise
                time.sleep(5)
        else:
            raise SystemExit("State machine creation kept failing on role propagation.")
    return arn


def apply() -> dict:
    ctx = _context()
    sqs = boto3.client("sqs", region_name=ctx["region"])
    iam = boto3.client("iam")
    lam = boto3.client("lambda", region_name=ctx["region"])
    sfn = boto3.client("stepfunctions", region_name=ctx["region"])

    print("applying:")
    queue_url, dlq_arn = _ensure_queues(sqs, ctx)
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"]
    )["Attributes"]["QueueArn"]

    lambda_role = _ensure_role(iam, LAMBDA_ROLE, "lambda.amazonaws.com", {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                                           "logs:PutLogEvents"],
             "Resource": "arn:aws:logs:*:*:*"},
            {"Effect": "Allow", "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage",
                                           "sqs:GetQueueAttributes"],
             "Resource": [queue_arn, dlq_arn]},
            # Scoped to the results prefix rather than the whole bucket — the
            # worker has no business reading the corpus.
            {"Effect": "Allow", "Action": ["s3:PutObject", "s3:GetObject"],
             "Resource": f"arn:aws:s3:::{ctx['bucket']}/jobs/*"},
        ],
    })

    function_arn = _ensure_function(lam, ctx, lambda_role, queue_arn)

    sfn_role = _ensure_role(iam, SFN_ROLE, "states.amazonaws.com", {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "lambda:InvokeFunction",
             "Resource": [function_arn, f"{function_arn}:*"]},
        ],
    })
    machine_arn = _ensure_state_machine(sfn, ctx, sfn_role, function_arn)

    print("\nadd to backend/.env:")
    print(f"  CMA_JOBS_QUEUE_URL={queue_url}")
    print(f"  CMA_PLAYBOOK_STATE_MACHINE={machine_arn}")
    return {"queue_url": queue_url, "function_arn": function_arn,
            "state_machine_arn": machine_arn}


# ── status / destroy ──────────────────────────────────────────────────────
def status() -> dict:
    ctx = _context()
    out: dict = {"region": ctx["region"], "bucket": ctx["bucket"]}
    sqs = boto3.client("sqs", region_name=ctx["region"])
    lam = boto3.client("lambda", region_name=ctx["region"])
    sfn = boto3.client("stepfunctions", region_name=ctx["region"])

    try:
        url = sqs.get_queue_url(QueueName=QUEUE)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"],
        )["Attributes"]
        out["queue"] = {"url": url,
                        "visible": attrs["ApproximateNumberOfMessages"],
                        "in_flight": attrs["ApproximateNumberOfMessagesNotVisible"]}
    except ClientError:
        out["queue"] = None

    try:
        cfg = lam.get_function(FunctionName=FUNCTION)["Configuration"]
        out["function"] = {"arn": cfg["FunctionArn"], "runtime": cfg["Runtime"],
                           "state": cfg.get("State")}
    except ClientError:
        out["function"] = None

    try:
        arn = _arn(ctx, "states", f"stateMachine:{STATE_MACHINE}")
        out["state_machine"] = sfn.describe_state_machine(
            stateMachineArn=arn)["status"]
        out["state_machine_arn"] = arn
    except ClientError:
        out["state_machine"] = None
    return out


def destroy() -> dict:
    ctx = _context()
    sqs = boto3.client("sqs", region_name=ctx["region"])
    iam = boto3.client("iam")
    lam = boto3.client("lambda", region_name=ctx["region"])
    sfn = boto3.client("stepfunctions", region_name=ctx["region"])
    removed: list[str] = []

    try:
        arn = _arn(ctx, "states", f"stateMachine:{STATE_MACHINE}")
        sfn.delete_state_machine(stateMachineArn=arn)
        removed.append(STATE_MACHINE)
    except ClientError:
        pass

    try:
        for mapping in lam.list_event_source_mappings(
                FunctionName=FUNCTION)["EventSourceMappings"]:
            lam.delete_event_source_mapping(UUID=mapping["UUID"])
        lam.delete_function(FunctionName=FUNCTION)
        removed.append(FUNCTION)
    except ClientError:
        pass

    for queue_name in (QUEUE, DLQ):
        try:
            sqs.delete_queue(QueueUrl=sqs.get_queue_url(QueueName=queue_name)["QueueUrl"])
            removed.append(queue_name)
        except ClientError:
            pass

    for role in (LAMBDA_ROLE, SFN_ROLE):
        try:
            for policy in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=policy)
            iam.delete_role(RoleName=role)
            removed.append(role)
        except ClientError:
            pass

    return {"removed": removed}


if __name__ == "__main__":
    sys.path.insert(0, ".")
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(".env"))

    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    actions = {"apply": apply, "status": status, "destroy": destroy}
    if command not in actions:
        print(f"usage: python -m infra.provision_async [{'|'.join(actions)}]")
        raise SystemExit(2)
    result = actions[command]()
    if command != "apply":
        print(json.dumps(result, indent=2, default=str))

"""Async solve worker.

Three modes, so one function backs both the queue consumer and the two
Step Functions task types:

  solve            — run the job, write the result to S3
  request_approval — persist the Step Functions task token to S3 and return,
                     leaving the execution paused until someone approves
  publish          — mark the run published after approval

This is a real file now rather than a string inside the provisioning script,
which is a small win on its own: it is reviewable, lintable, and diffable.
CDK zips this directory and uploads it as the function's code.
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
        Bucket=BUCKET,
        Key=key,
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
        history.append(
            {"iteration": i, "input": round(mid, 4), "output": round(value, 4)}
        )
        if abs(value - target) < 1e-6:
            break
        if value < target:
            low = mid
        else:
            high = mid
    return {
        "solved_input": round((low + high) / 2, 4),
        "iterations": len(history),
        "history": history[-5:],
    }


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
                _put(
                    f"{PREFIX}{job.get('job_id', 'unknown')}.json",
                    {"job_id": job.get("job_id"), "status": "failed", "error": str(e)},
                )
                raise
        return {"processed": len(out)}

    # Step Functions delivery.
    mode = event.get("mode", "solve")
    if mode == "request_approval":
        run_id = event["run_id"]
        _put(
            f"{PREFIX}approvals/{run_id}.json",
            {
                "run_id": run_id,
                "task_token": event["task_token"],
                "phase": event.get("phase", "champion_challenger"),
                "requested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "awaiting_approval",
            },
        )
        # Returning without calling SendTaskSuccess is the point: the
        # execution stays parked here until a human decides.
        return {"awaiting_approval": run_id}

    if mode == "publish":
        run_id = event["run_id"]
        _put(
            f"{PREFIX}published/{run_id}.json",
            {
                "run_id": run_id,
                "status": "published",
                "approved_by": event.get("approved_by", "unknown"),
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        return {"published": run_id}

    return _solve(event)

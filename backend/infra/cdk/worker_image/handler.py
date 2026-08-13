"""Solve worker, container edition — runs real model artifacts.

Supersedes the zip worker's placeholder arithmetic. Four modes, so one image
backs the SQS consumer and every Step Functions task type:

  solve            goal-seek (the original stand-in, kept for compiled canvases
                   whose nodes carry no artifact)
  predict          load a real .pkl/.joblib artifact from S3 and run predict
  request_approval park the task token and wait for a human
  publish          record the approved run

Why the artifact's directory matters
------------------------------------
These pickles reference a sibling `_classes.py` for their custom model classes,
so `pickle.load` needs that module importable. `services/model_runner.py`
handles this locally by inserting the artifact's directory on `sys.path`; this
does the same after pulling the whole model directory out of S3. Downloading
only the .pkl would fail at unpickle time with a bare ModuleNotFoundError,
which is a confusing way to learn about a missing sidecar.

The subprocess isolation the local runner needs is not repeated here: the
Lambda execution environment already is the sandbox, and a per-invocation
process would buy nothing.
"""
import json
import os
import pickle
import sys
import time
from pathlib import Path

import boto3

BUCKET = os.environ["RESULT_BUCKET"]
PREFIX = os.environ.get("RESULT_PREFIX", "jobs/")
MODEL_PREFIX = os.environ.get("MODEL_PREFIX", "models/")
WORK_DIR = Path("/tmp/models")  # noqa: S108 - the only writable path in Lambda

s3 = boto3.client("s3")


def _put(key, payload):
    s3.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(payload, default=str).encode(),
        ContentType="application/json",
    )


# ── real model execution ──────────────────────────────────────────────────
def _fetch_model_dir(model_dir: str) -> Path:
    """Download every object under models/<model_dir>/ into /tmp.

    Pulls the whole directory, not just the artifact, because the pickle needs
    its `_classes.py` sibling to unpickle. Cached across warm invocations —
    Lambda reuses /tmp, so a second call on the same container skips the fetch.
    """
    local = WORK_DIR / model_dir
    if local.exists() and any(local.iterdir()):
        return local
    local.mkdir(parents=True, exist_ok=True)

    prefix = f"{MODEL_PREFIX}{model_dir.strip('/')}/"
    paginator = s3.get_paginator("list_objects_v2")
    found = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix):
        for item in page.get("Contents", []):
            relative = item["Key"][len(prefix):]
            if not relative or relative.endswith("/"):
                continue
            target = local / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(BUCKET, item["Key"], str(target))
            found += 1
    if not found:
        raise FileNotFoundError(
            f"no objects under s3://{BUCKET}/{prefix} — upload the model "
            "directory including its _classes.py"
        )
    return local


def _prepare_import_path(directory: str) -> None:
    """Make `directory` the authoritative source of the `_classes` sidecar.

    Every model directory ships its own `_classes.py`, all under the same
    module name. Python caches the first one it imports in `sys.modules`, and a
    warm Lambda container reuses that cache across invocations — so without
    this, the first model directory to load wins and every artifact from a
    different directory fails with a confusing "Can't get attribute X on
    <module '_classes' from '.../other-dir/_classes.py'>".

    Worse than an error would be a silent one: two directories with a
    same-named class of different behaviour would unpickle against the wrong
    implementation and return plausible, wrong numbers.

    So: drop any cached module loaded from a *different* model directory, and
    put this directory first on the path.
    """
    for name, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if origin.startswith(str(WORK_DIR)) and not origin.startswith(directory):
            del sys.modules[name]

    # First on the path, not merely present: another model directory may
    # already be there from an earlier invocation.
    while directory in sys.path:
        sys.path.remove(directory)
    sys.path.insert(0, directory)


def _load_artifact(path: Path):
    """Unpickle, with the artifact's directory importable for custom classes."""
    _prepare_import_path(str(path.parent))
    if path.suffix == ".joblib":
        import joblib

        return joblib.load(path)
    with path.open("rb") as handle:
        return pickle.load(handle)  # noqa: S301 - operator-supplied artifact


def _predict(job: dict) -> dict:
    started = time.time()
    model_dir = job["model_dir"]
    artifact_name = job["artifact"]
    rows = job.get("rows") or []
    feature_columns = job.get("feature_columns") or []

    local_dir = _fetch_model_dir(model_dir)
    artifact_path = local_dir / artifact_name
    if not artifact_path.exists():
        raise FileNotFoundError(f"{artifact_name} not found in {model_dir}")

    model = _load_artifact(artifact_path)

    import pandas as pd

    frame = pd.DataFrame(rows)

    # Column ORDER is load-bearing: several of these models do
    # `np.asarray(X)` and unpack positionally, so a frame in the wrong order
    # produces confidently wrong numbers rather than an error. Prefer the
    # caller's list; fall back to the model's own declared feature_names,
    # which these artifacts carry as a class attribute. Relying on the
    # incoming dict's key order would work by luck, not by contract.
    if not feature_columns:
        declared = getattr(model, "feature_names", None) or getattr(
            model, "feature_names_in_", None)
        if declared is not None:
            feature_columns = [str(c) for c in declared]

    if feature_columns:
        missing = [c for c in feature_columns if c not in frame.columns]
        if missing:
            raise ValueError(
                f"input is missing feature column(s) {missing}; "
                f"model expects {feature_columns}"
            )
        frame = frame[feature_columns]

    if hasattr(model, "predict"):
        raw = model.predict(frame)
    elif callable(model):
        raw = model(frame)
    else:
        raise TypeError(
            f"artifact {artifact_name} has no predict() and is not callable"
        )

    predictions = raw.tolist() if hasattr(raw, "tolist") else list(raw)
    return {
        "job_id": job["job_id"],
        "status": "succeeded",
        "kind": "predict",
        "model_dir": model_dir,
        "artifact": artifact_name,
        "input_rows": len(frame),
        "predictions": predictions[:500],
        "prediction_count": len(predictions),
        "model_type": type(model).__name__,
        "duration_ms": int((time.time() - started) * 1000),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


# ── goal seek (kept from the zip worker) ──────────────────────────────────
def _goal_seek(target, rate, guess, iterations):
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
    return {
        "job_id": job["job_id"],
        "status": "succeeded",
        "kind": job.get("kind", "goal_seek"),
        "result": result,
        "duration_ms": int((time.time() - started) * 1000),
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _run_job(job: dict) -> dict:
    """A compute job: predict when it names an artifact, goal-seek otherwise."""
    if job.get("model_dir") and job.get("artifact"):
        payload = _predict(job)
    else:
        payload = _solve(job)
    _put(f"{PREFIX}{payload['job_id']}.json", payload)
    return payload


def handler(event, context):
    # SQS delivery.
    if "Records" in event:
        processed = 0
        for record in event["Records"]:
            job = json.loads(record["body"])
            try:
                _run_job(job)
                processed += 1
            except Exception as e:
                _put(
                    f"{PREFIX}{job.get('job_id', 'unknown')}.json",
                    {"job_id": job.get("job_id"), "status": "failed",
                     "error": f"{type(e).__name__}: {e}"},
                )
                raise
        return {"processed": processed}

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
        return {"awaiting_approval": run_id}

    if mode == "publish":
        run_id = event["run_id"]
        _put(
            f"{PREFIX}published/{run_id}.json",
            {
                "run_id": run_id,
                "status": "published",
                "approved_by": event.get("approved_by", "unknown"),
                "destinations": event.get("destinations", []),
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        return {"published": run_id}

    if mode == "predict":
        payload = _predict(event)
        _put(f"{PREFIX}{payload['job_id']}.json", payload)
        return payload

    return _run_job(event)

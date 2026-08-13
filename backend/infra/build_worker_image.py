"""Build the worker container image in AWS, with no local Docker.

Zips infra/cdk/worker_image/, uploads it to the results bucket, starts the
CodeBuild project the CDK stack created, and streams the outcome. CodeBuild
runs the docker build on a privileged build container and pushes to ECR.

The whole point is that nothing here needs Docker locally. The trade is a
slower loop (a remote build is minutes, not seconds) for one less thing
installed on the machine — a good trade for an image that changes rarely.

Order of operations, because a Lambda cannot reference an image that does not
exist yet:

    npx cdk deploy                      # zip worker + ECR repo + build project
    python -m infra.build_worker_image  # build and push the image
    npx cdk deploy -c workerImageTag=<tag>   # switch the worker to it

Usage (from backend/):
    python -m infra.build_worker_image            # package, upload, build, wait
    python -m infra.build_worker_image --no-wait  # start it and return
    python -m infra.build_worker_image status     # last build's outcome
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import boto3

SOURCE_DIR = Path(__file__).resolve().parent / "cdk" / "worker_image"
SOURCE_KEY = "builds/worker-image-src.zip"
PROJECT = "CmaWorkbenchAsync-worker-image"
POLL_SECONDS = 10


def _region() -> str:
    import os

    from cof.llm_config import bedrock_region

    return os.getenv("CMA_JOBS_REGION", "").strip() or bedrock_region()


def _bucket() -> str:
    import os

    bucket = os.getenv("CMA_CORPUS_BUCKET", "").strip()
    if not bucket:
        raise SystemExit("CMA_CORPUS_BUCKET must be set — the build source is "
                         "staged there.")
    return bucket


def package() -> bytes:
    """Zip the image source. Deterministic, so an unchanged tree is a no-op."""
    files = sorted(p for p in SOURCE_DIR.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    if not files:
        raise SystemExit(f"no files in {SOURCE_DIR}")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            # Fixed timestamp so identical content produces an identical zip.
            info = zipfile.ZipInfo(str(path.relative_to(SOURCE_DIR)).replace("\\", "/"),
                                   date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    print(f"  packaged {len(files)} file(s) from {SOURCE_DIR.name}/")
    return buffer.getvalue()


def upload(payload: bytes) -> None:
    boto3.client("s3", region_name=_region()).put_object(
        Bucket=_bucket(), Key=SOURCE_KEY, Body=payload,
        ContentType="application/zip",
    )
    print(f"  uploaded s3://{_bucket()}/{SOURCE_KEY} ({len(payload) / 1024:.1f} KB)")


def source_tag(payload: bytes) -> str:
    """A tag derived from the source itself.

    Deterministic, so rebuilding unchanged source produces the same tag and
    you can tell at a glance whether a deployed image matches the tree. Passed
    in as an override rather than computed inside the buildspec, so the caller
    knows the tag before the build finishes.
    """
    import hashlib

    return hashlib.sha256(payload).hexdigest()[:12]


def start(tag: str) -> str:
    build = boto3.client("codebuild", region_name=_region()).start_build(
        projectName=PROJECT,
        environmentVariablesOverride=[
            {"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"},
        ],
    )["build"]
    print(f"  started {build['id']}")
    return build["id"]


def wait(build_id: str) -> dict:
    codebuild = boto3.client("codebuild", region_name=_region())
    last_phase = None
    while True:
        build = codebuild.batch_get_builds(ids=[build_id])["builds"][0]
        phase = build.get("currentPhase")
        if phase != last_phase:
            print(f"  {phase}")
            last_phase = phase
        if build["buildStatus"] != "IN_PROGRESS":
            return build
        time.sleep(POLL_SECONDS)


def tail_log(build: dict, lines: int = 25) -> None:
    logs_info = build.get("logs") or {}
    group, stream = logs_info.get("groupName"), logs_info.get("streamName")
    if not group or not stream:
        return
    logs = boto3.client("logs", region_name=_region())
    try:
        events = logs.get_log_events(logGroupName=group, logStreamName=stream,
                                     startFromHead=False, limit=lines)["events"]
    except Exception as e:
        print(f"  (could not read logs: {e})")
        return
    print("  --- last log lines ---")
    for event in events:
        print("  " + event["message"].rstrip())


def resolve_tag(build: dict) -> str | None:
    """The IMAGE_TAG this build was started with."""
    for variable in build.get("environment", {}).get("environmentVariables", []):
        if variable.get("name") == "IMAGE_TAG":
            return variable.get("value")
    return None


def status() -> dict:
    codebuild = boto3.client("codebuild", region_name=_region())
    ids = codebuild.list_builds_for_project(projectName=PROJECT,
                                            sortOrder="DESCENDING")["ids"]
    if not ids:
        return {"builds": 0}
    build = codebuild.batch_get_builds(ids=ids[:1])["builds"][0]
    return {
        "build_id": build["id"],
        "status": build["buildStatus"],
        "phase": build.get("currentPhase"),
        "started": build.get("startTime"),
        "tag": resolve_tag(build),
    }


def images() -> list[dict]:
    """What is currently in the repo, so you can pick a tag deliberately."""
    ecr = boto3.client("ecr", region_name=_region())
    repos = [r for r in ecr.describe_repositories()["repositories"]
             if "workerimage" in r["repositoryName"].lower()]
    if not repos:
        return []
    found = ecr.describe_images(repositoryName=repos[0]["repositoryName"])
    return sorted(
        [{"tags": d.get("imageTags", []),
          "pushed": d.get("imagePushedAt"),
          "mb": round(d.get("imageSizeInBytes", 0) / 1_048_576, 1)}
         for d in found["imageDetails"]],
        key=lambda d: str(d["pushed"]), reverse=True,
    )


def run(do_wait: bool = True) -> int:
    print("building the worker image in AWS:")
    payload = package()
    tag = source_tag(payload)
    print(f"  tag {tag} (sha256 of the source)")
    upload(payload)
    build_id = start(tag)
    if not do_wait:
        print("  not waiting (--no-wait); check with `status`")
        return 0

    build = wait(build_id)
    outcome = build["buildStatus"]
    print(f"\n  result: {outcome}")
    if outcome != "SUCCEEDED":
        tail_log(build)
        return 1

    tag = resolve_tag(build)
    for image in images():
        print(f"  in ECR: tags={image['tags']}  {image['mb']} MB  {image['pushed']}")
    print("\nswitch the worker to it:")
    print(f"  cd infra/cdk && npx cdk deploy -c workerImageTag={tag or 'latest'}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    sys.path.insert(0, ".")
    load_dotenv(Path(".env"))

    argument = sys.argv[1] if len(sys.argv) > 1 else ""
    if argument == "status":
        print(json.dumps(status(), indent=2, default=str))
    elif argument == "images":
        print(json.dumps(images(), indent=2, default=str))
    else:
        raise SystemExit(run(do_wait="--no-wait" not in sys.argv))

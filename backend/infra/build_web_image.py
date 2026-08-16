"""Build the web-tier container image in AWS, with no local Docker.

The companion to infra/build_worker_image.py, and the same trade: a remote
build is minutes rather than seconds, in exchange for one less thing installed
on the machine and a build that is reproducible in CI.

What differs is the source. The worker image is one directory of two files;
this one is the application — main.py plus agent/, cof/, config/, infra/,
models/, packs/, routers/ and services/. So the packaging step is an explicit
allowlist rather than "zip that folder", and it refuses to run if anything
resembling a credential ends up inside it. An image layer is immutable and gets
pushed to a registry: a secret baked into one is not a mistake you quietly
correct later.

Order of operations, because a service cannot reference an image that does not
exist yet:

    npx cdk deploy                   # web ECR repo + build project, no service
    python -m infra.build_web_image  # build and push the image
    npx cdk deploy -c webImageTag=<tag>   # bring up the VPC, ALB and service

Usage (from backend/):
    python -m infra.build_web_image            # package, upload, build, wait
    python -m infra.build_web_image --no-wait  # start it and return
    python -m infra.build_web_image status     # last build's outcome
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import boto3

from infra.build_worker_image import (
    POLL_SECONDS,  # noqa: F401  - re-exported for symmetry with the worker script
    resolve_tag,
    source_tag,
    tail_log,
    wait,
)

BACKEND = Path(__file__).resolve().parent.parent
SOURCE_KEY = "builds/web-image-src.zip"
PROJECT = "CmaWorkbenchAsync-web-image"

# Exactly what the Dockerfile COPYs, plus the build files themselves. An
# allowlist rather than a denylist because the failure modes are asymmetric:
# forgetting to include something breaks the build immediately and loudly,
# while forgetting to exclude something ships it to a registry.
INCLUDE_FILES = ("Dockerfile", ".dockerignore", "main.py")
INCLUDE_DIRS = ("agent", "cof", "config", "infra", "models", "packs",
                "routers", "services")

# Pruned inside those directories. `data/` and `skills_user/` are caches of S3
# now, so shipping them would bake one machine's working set into the image;
# `worker_image/` is built separately and would put a second Dockerfile in the
# build context.
EXCLUDE_PARTS = {"__pycache__", ".venv", "cdk.out", "node_modules",
                 ".pytest_cache", ".ruff_cache", "skills_user", "data",
                 # The worker's own image source. .dockerignore already keeps
                 # it out of the build context, but a second Dockerfile inside
                 # a zip called "web-image-src" is the kind of thing that
                 # confuses whoever opens it next.
                 "worker", "worker_image"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}

# Refused outright, wherever they appear. This is the check that matters, and
# it is deliberately wider than "a file called .env": `config/` is documented
# as the home for corporate integration credentials, so a real
# `data_services.env` sitting next to the committed `.example.env` is the
# realistic way a secret gets packaged here.
FORBIDDEN_NAMES = {"credentials", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}


def _looks_like_a_secret(path: Path) -> bool:
    name = path.name
    if name in FORBIDDEN_NAMES or path.suffix in FORBIDDEN_SUFFIXES:
        return True
    # Any dotenv file, except the committed templates that exist to be read.
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return ".example" not in name and ".sample" not in name
    return False


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


def _keep(path: Path) -> bool:
    if any(part in EXCLUDE_PARTS for part in path.parts):
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return True


def _collect() -> list[Path]:
    files = [BACKEND / name for name in INCLUDE_FILES]
    missing = [f for f in files if not f.exists()]
    if missing:
        raise SystemExit(f"missing build input(s): {[str(m) for m in missing]}")
    for directory in INCLUDE_DIRS:
        root = BACKEND / directory
        if not root.exists():
            continue
        files.extend(p for p in root.rglob("*") if p.is_file() and _keep(p))
    return sorted(files)


def package() -> bytes:
    """Zip the build context. Deterministic, so unchanged source is a no-op."""
    files = _collect()

    # The guard. A `.env` in this zip would be extracted into the Docker build
    # context, and while .dockerignore would then skip it, relying on that is
    # one edit away from shipping a provider key inside an immutable layer.
    # Refuse at the point where it is still cheap to notice.
    caught = [f for f in files if _looks_like_a_secret(f)]
    if caught:
        raise SystemExit(
            "refusing to package what looks like a credential:\n  "
            + "\n  ".join(str(c.relative_to(BACKEND)) for c in caught)
        )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            # Fixed timestamp so identical content produces an identical zip,
            # which is what makes the tag below a function of the source.
            info = zipfile.ZipInfo(
                str(path.relative_to(BACKEND)).replace("\\", "/"),
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())
    payload = buffer.getvalue()
    print(f"  packaged {len(files)} file(s), {len(payload) / 1024:.0f} KB zipped")
    return payload


def upload(payload: bytes) -> None:
    boto3.client("s3", region_name=_region()).put_object(
        Bucket=_bucket(), Key=SOURCE_KEY, Body=payload,
        ContentType="application/zip",
    )
    print(f"  uploaded s3://{_bucket()}/{SOURCE_KEY}")


def start(tag: str) -> str:
    build = boto3.client("codebuild", region_name=_region()).start_build(
        projectName=PROJECT,
        environmentVariablesOverride=[
            {"name": "IMAGE_TAG", "value": tag, "type": "PLAINTEXT"},
        ],
    )["build"]
    print(f"  started {build['id']}")
    return build["id"]


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
    ecr = boto3.client("ecr", region_name=_region())
    repos = [r for r in ecr.describe_repositories()["repositories"]
             if "webimage" in r["repositoryName"].lower()]
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
    print("building the web image in AWS:")
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
    print(
        f"\nNow bring the service up with this image:\n"
        f"    npx cdk deploy -c webImageTag={tag}\n"
        f"or persist it as CMA_WEB_IMAGE_TAG={tag} in backend/.env.\n"
        f"\nNote the first deploy with a tag also creates the VPC, load "
        f"balancer and service — that is where this stack stops being free."
    )
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env")
    if "status" in sys.argv:
        print(json.dumps(status(), indent=2, default=str))
        raise SystemExit(0)
    raise SystemExit(run(do_wait="--no-wait" not in sys.argv))

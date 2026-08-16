"""Publish the built frontend to the site bucket and invalidate the cache.

`npm run build` produces `frontend/dist/`. This uploads it and tells CloudFront
to forget what it was serving. Two steps, and skipping the second is the
classic way to deploy a frontend that nobody can see for the next day.

Not CDK's `BucketDeployment`, for the same reason the other scripts here are
scripts: it bundles the assets into the CDK asset bucket at synth time and
deploys a custom-resource Lambda to copy them out, which means every frontend
change becomes a CloudFormation deployment. Uploading files to a bucket does
not need a change set.

Cache headers are set per file, and the split matters:

    index.html      no-cache      always revalidated
    everything else immutable     cached for a year

Vite fingerprints asset filenames with a content hash, so `index-a1b2c3.js`
either exists or it does not — its content can never change under that name,
which is what makes a year-long cache safe. `index.html` is the one file with
a stable name, and it is the file that points at the fingerprinted ones. Cache
it and a deploy is invisible until the TTL expires; leave it revalidated and
the new build is live the moment the invalidation completes.

Usage (from backend/):
    python -m infra.deploy_frontend            # show what would change
    python -m infra.deploy_frontend --write    # upload and invalidate
"""
from __future__ import annotations

import mimetypes
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DIST = BACKEND.parent / "frontend" / "dist"
STACK = "CmaWorkbenchAsync"

# One year, and the `immutable` half is what stops a browser revalidating on
# every navigation even while the response is fresh.
IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def _region() -> str:
    import os

    for var in ("CMA_JOBS_REGION", "CMA_BEDROCK_REGION", "AWS_REGION"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return "us-east-1"


def _outputs() -> dict[str, str]:
    import boto3

    stacks = boto3.client("cloudformation", region_name=_region()).describe_stacks(
        StackName=STACK
    )["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def _cache_control(rel: str) -> str:
    return REVALIDATE if rel in ("index.html", "sw.js") else IMMUTABLE


def apply(write: bool) -> int:
    if not DIST.exists():
        raise SystemExit(
            f"{DIST} does not exist. Build the frontend first:\n"
            f"    cd frontend && npm run build"
        )

    outputs = _outputs()
    bucket = outputs.get("SiteBucket")
    distribution = outputs.get("DistributionId")
    if not bucket or not distribution:
        raise SystemExit(
            "The stack has no SiteBucket/DistributionId output. The web tier is "
            "gated on CMA_WEB_IMAGE_TAG — deploy it first."
        )

    files = sorted(p for p in DIST.rglob("*") if p.is_file())
    if not files:
        raise SystemExit(f"{DIST} is empty — did `npm run build` succeed?")

    total = sum(p.stat().st_size for p in files)
    print(f"site bucket  : {bucket}")
    print(f"distribution : {distribution}")
    print(f"source       : {DIST}")
    print(f"files        : {len(files)}  ({total / 1e6:.2f} MB)\n")
    for path in files[:12]:
        rel = str(path.relative_to(DIST)).replace("\\", "/")
        print(f"  {rel:52} {_cache_control(rel)}")
    if len(files) > 12:
        print(f"  … and {len(files) - 12} more")

    if not write:
        print("\n(dry run — re-run with --write to upload)")
        return 0

    import boto3

    s3 = boto3.client("s3", region_name=_region())
    for path in files:
        rel = str(path.relative_to(DIST)).replace("\\", "/")
        content_type, _ = mimetypes.guess_type(rel)
        s3.put_object(
            Bucket=bucket, Key=rel, Body=path.read_bytes(),
            ContentType=content_type or "application/octet-stream",
            CacheControl=_cache_control(rel),
        )
    print(f"\nuploaded {len(files)} file(s)")

    # Only index.html needs invalidating — everything else is fingerprinted and
    # a new build writes new names. Invalidating /* would work and would also
    # spend the monthly free allowance on paths whose content cannot change.
    cf = boto3.client("cloudfront", region_name="us-east-1")
    result = cf.create_invalidation(
        DistributionId=distribution,
        InvalidationBatch={
            "Paths": {"Quantity": 2, "Items": ["/index.html", "/"]},
            "CallerReference": f"deploy-{path.stat().st_mtime_ns}",
        },
    )
    print(f"invalidation {result['Invalidation']['Id']} created "
          f"({result['Invalidation']['Status']})")
    print(f"\n{outputs.get('SiteUrl', '')}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(BACKEND / ".env")
    raise SystemExit(apply("--write" in sys.argv))

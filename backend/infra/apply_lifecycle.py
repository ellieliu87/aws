"""Lifecycle rules for the results bucket: what is allowed to accumulate.

Versioning is on, which means a delete does not reclaim anything and an
overwrite keeps both copies. That is deliberate — it is the audit trail for
which version of a methodology was in force when a number was published — but
left alone it also means job results from last spring, every superseded upload,
and every build zip are still being paid for.

**Why this is a script and not CDK.** The bucket is not created by the stack.
`CMA_CORPUS_BUCKET` names a bucket that already existed, and
`infra/cdk/async_stack.py` imports it with `Bucket.from_bucket_name`, which
returns a reference, not a resource. CDK cannot attach a lifecycle
configuration to something it does not own, and the alternatives — a custom
resource, or adopting the bucket into the stack — mean either a Lambda and a
role to maintain for one API call, or putting the document corpus one
`RemovalPolicy` mistake away from deletion. Neither is worth it for a call
that is idempotent and runs about once.

Usage (from backend/):
    python -m infra.apply_lifecycle           # show current vs proposed
    python -m infra.apply_lifecycle --write   # apply

`PutBucketLifecycleConfiguration` **replaces the entire configuration** rather
than merging, so the dry run prints what is already there. Anything in that
listing which is not in the proposal below is about to stop existing.

── the posture ────────────────────────────────────────────────────────────

    jobs/       current versions expire      30 days
                noncurrent versions expire    1 day
    datasets/   noncurrent versions expire  180 days
    builds/     noncurrent versions expire   90 days
    models/     nothing at all
    (bucket)    incomplete multipart uploads aborted after 7 days
    (bucket)    delete markers with nothing under them removed

`models/` is the exception and the reason the rules are prefix-scoped rather
than bucket-wide: its noncurrent versions *are* the audit trail. Expiring them
would quietly convert "we can show which artifact produced this number" into
"we can show it for a year", and a claim that decays on a timer is worse than
one that was never made. If that ever changes, it should change here and in
docs/ARCHITECTURE.md in the same commit.

The corpus documents live at the bucket root under no prefix, which is why no
rule below carries an empty-prefix expiry. The two bucket-wide rules are both
garbage collection: an aborted upload and an orphaned delete marker are not
data anyone can read.
"""
from __future__ import annotations

import json
import os
import sys

JOBS_EXPIRE_DAYS = 30
JOBS_NONCURRENT_DAYS = 1
DATASETS_NONCURRENT_DAYS = 180
BUILDS_NONCURRENT_DAYS = 90
ABORT_MULTIPART_DAYS = 7

CONFIGURATION = {
    "Rules": [
        {
            # A job result is read once by the endpoint that polled for it.
            # Thirty days is long enough that a stale link in someone's tab
            # still resolves, and short enough that nothing accumulates.
            "ID": "jobs-expire",
            "Status": "Enabled",
            "Filter": {"Prefix": "jobs/"},
            "Expiration": {"Days": JOBS_EXPIRE_DAYS},
            # Without this the expiry above only writes a delete marker and
            # the bytes stay, billed, forever. The single most common way a
            # lifecycle rule on a versioned bucket does nothing.
            "NoncurrentVersionExpiration": {"NoncurrentDays": JOBS_NONCURRENT_DAYS},
        },
        {
            # A re-uploaded dataset supersedes the previous file. Six months is
            # a generous window to notice the upload was wrong and want the old
            # one back; past that it is dead weight.
            "ID": "datasets-noncurrent-expire",
            "Status": "Enabled",
            "Filter": {"Prefix": "datasets/"},
            "NoncurrentVersionExpiration": {"NoncurrentDays": DATASETS_NONCURRENT_DAYS},
        },
        {
            # One key, overwritten on every `python -m infra.build_worker_image`.
            # The tag is a hash of the source tree, so an old zip is not the
            # recovery path — rebuilding from the tree is.
            "ID": "builds-noncurrent-expire",
            "Status": "Enabled",
            "Filter": {"Prefix": "builds/"},
            "NoncurrentVersionExpiration": {"NoncurrentDays": BUILDS_NONCURRENT_DAYS},
        },
        {
            # A multipart upload that never completed is invisible in the
            # console and billed like storage. Model artifacts are large enough
            # for an interrupted upload to be a real amount of money.
            "ID": "abort-incomplete-multipart",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {
                "DaysAfterInitiation": ABORT_MULTIPART_DAYS
            },
        },
        {
            # Only fires once a key's last noncurrent version is gone, so it
            # can never touch models/ while those versions are being kept.
            "ID": "expired-delete-markers",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"ExpiredObjectDeleteMarker": True},
        },
    ]
}


def _region() -> str:
    for var in ("CMA_JOBS_REGION", "CMA_BEDROCK_REGION", "AWS_REGION",
                "AWS_DEFAULT_REGION"):
        value = os.getenv(var, "").strip()
        if value:
            return value
    return "us-east-1"


def _current(client, bucket: str) -> dict | None:
    try:
        response = client.get_bucket_lifecycle_configuration(Bucket=bucket)
        return {"Rules": response.get("Rules", [])}
    except Exception as e:
        # A bucket with no configuration raises NoSuchLifecycleConfiguration,
        # which is the expected state the first time this runs — not an error.
        if "NoSuchLifecycleConfiguration" in f"{type(e).__name__}: {e}":
            return None
        raise


def apply(write: bool) -> int:
    bucket = os.getenv("CMA_CORPUS_BUCKET", "").strip()
    if not bucket:
        raise SystemExit(
            "CMA_CORPUS_BUCKET must be set in backend/.env — it names the "
            "bucket these rules apply to."
        )

    import boto3

    client = boto3.client("s3", region_name=_region())

    existing = _current(client, bucket)
    print(f"bucket {bucket} ({_region()})\n")
    if existing is None:
        print("current: no lifecycle configuration\n")
    else:
        print("current:")
        print(json.dumps(existing, indent=2, default=str))
        print()

    if existing == CONFIGURATION:
        print("already matches — nothing to do.")
        return 0

    print("proposed:")
    print(json.dumps(CONFIGURATION, indent=2))

    if not write:
        print(
            "\n(dry run — re-run with --write to apply)\n"
            "Note this REPLACES the configuration above rather than merging "
            "into it."
        )
        return 0

    client.put_bucket_lifecycle_configuration(
        Bucket=bucket, LifecycleConfiguration=CONFIGURATION,
    )
    print(f"\napplied to {bucket}")
    print(
        "\nS3 evaluates rules once a day, asynchronously, and bills nothing "
        "for the deletes. Expect the first sweep within 24-48 hours."
    )
    return 0


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    raise SystemExit(apply("--write" in sys.argv))

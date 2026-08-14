"""S3 as the source of record for uploaded bytes.

Phase 5 moved the *records* off module dicts. It did not move the bytes: a
dataset file or model artifact still lands on the local disk of whichever node
served the upload. On one node that is invisible. On two it is worse than a
clean failure — the DynamoDB record resolves everywhere, so the dataset appears
in every listing, and then reading it fails on every replica except the one
that happened to take the upload.

This closes that gap the same way services/corpus_store.py closes it for the
document corpus: S3 is the durable copy, the local directory is a cache.

    write   local + S3, together
    read    local if present, otherwise pull from S3 and keep it

`ensure_local` is the whole interface as far as callers are concerned. Existing
code takes a `Path` and hands it to pandas or pickle, and that stays true —
which is why the seam is here rather than in every reader.

Layout mirrors the local tree, so the two are diffable by eye:

    datasets/<function_id>/<dataset_id>.<ext>
    models/<function_id>/<model_id>.<ext>

The models prefix is deliberately the one services/workflow_artifacts.py
already uses for the Lambda worker, so an uploaded artifact is reachable by the
worker without a second copy under a different name.

Entirely opt-in. With CMA_CORPUS_BUCKET unset every function is a no-op and the
app behaves exactly as before — local disk, single node, no AWS account needed.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("cma.blob_store")

DATASET_PREFIX = "datasets/"
MODEL_PREFIX = "models/"


def bucket() -> str:
    return os.getenv("CMA_CORPUS_BUCKET", "").strip()


def enabled() -> bool:
    return bool(bucket())


def _client():
    import boto3

    from cof.llm_config import bedrock_region

    region = os.getenv("CMA_JOBS_REGION", "").strip() or bedrock_region()
    return boto3.client("s3", region_name=region)


def _key(prefix: str, rel_path: str) -> str:
    """`capital_planning/ds-abc.csv` -> `datasets/capital_planning/ds-abc.csv`.

    Backslashes are normalised because the local paths are built with `/` but
    Windows hands them back either way, and an S3 key containing `\\` is a
    different object rather than an error — the kind of bug that only shows up
    on someone else's machine.
    """
    return f"{prefix}{str(rel_path).replace(os.sep, '/').lstrip('/')}"


# ── write ─────────────────────────────────────────────────────────────────
def put(prefix: str, rel_path: str, data: bytes) -> bool:
    """Upload bytes. Returns False when no bucket is configured."""
    if not enabled():
        return False
    try:
        _client().put_object(Bucket=bucket(), Key=_key(prefix, rel_path), Body=data)
        return True
    except Exception as e:
        # An upload failure must not lose the analyst's file: it is already on
        # local disk by the time this runs, so the request still succeeds and
        # this node still serves it. What is lost is the durability, which is
        # worth a loud log and not worth a 500.
        log.warning("could not upload %s: %s", _key(prefix, rel_path), e)
        return False


def put_file(prefix: str, rel_path: str, source: Path) -> bool:
    try:
        return put(prefix, rel_path, Path(source).read_bytes())
    except OSError as e:
        log.warning("could not read %s for upload: %s", source, e)
        return False


# ── read ──────────────────────────────────────────────────────────────────
def ensure_local(prefix: str, rel_path: str, dest: Path) -> Path:
    """Return `dest`, pulling it from S3 first if it is not already there.

    The local file wins when present — it is a cache, and re-downloading a file
    this node already has would put S3 in the path of every read.
    """
    dest = Path(dest)
    if dest.exists():
        return dest
    if not enabled():
        return dest

    key = _key(prefix, rel_path)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Download to a sibling temp name and rename, so a reader on another
        # thread never observes a half-written file at the real path.
        tmp = dest.with_suffix(dest.suffix + ".part")
        _client().download_file(bucket(), key, str(tmp))
        tmp.replace(dest)
        log.info("pulled %s from s3", key)
    except Exception as e:
        # Missing in S3 too. Callers already handle a non-existent path with a
        # 400/500 that names the dataset, which is a better error than one
        # about object storage.
        log.warning("could not pull %s: %s", key, e)
    return dest


def ensure_siblings(prefix: str, rel_dir: str, dest_dir: Path, match: str = "_") -> list[Path]:
    """Pull the support files sitting beside an artifact.

    A pickle that references custom classes needs its `_classes.py` sidecar
    importable, so pulling the `.pkl` alone would fail at `pickle.load` with a
    bare ModuleNotFoundError — a confusing way to learn a file is missing.
    Only called after a real pull, so the listing is not on the hot path.
    """
    if not enabled():
        return []
    pulled: list[Path] = []
    listing_prefix = _key(prefix, rel_dir).rstrip("/") + "/"
    try:
        response = _client().list_objects_v2(Bucket=bucket(), Prefix=listing_prefix)
        for obj in response.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name.startswith(match) or not name.endswith(".py"):
                continue
            dest = Path(dest_dir) / name
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".part")
            _client().download_file(bucket(), obj["Key"], str(tmp))
            tmp.replace(dest)
            pulled.append(dest)
    except Exception as e:
        log.warning("could not pull sidecars under %s: %s", listing_prefix, e)
    return pulled


# ── delete ────────────────────────────────────────────────────────────────
def delete(prefix: str, rel_path: str) -> None:
    if not enabled():
        return
    try:
        _client().delete_object(Bucket=bucket(), Key=_key(prefix, rel_path))
    except Exception as e:
        log.warning("could not delete %s: %s", _key(prefix, rel_path), e)


def status() -> dict:
    info: dict = {"enabled": enabled(), "bucket": bucket() or "(unset, local only)"}
    if enabled():
        try:
            response = _client().list_objects_v2(
                Bucket=bucket(), Prefix=DATASET_PREFIX, MaxKeys=1
            )
            info["reachable"] = True
            info["has_datasets"] = response.get("KeyCount", 0) > 0
        except Exception as e:
            info["reachable"] = False
            info["error"] = f"{type(e).__name__}: {e}"
    return info

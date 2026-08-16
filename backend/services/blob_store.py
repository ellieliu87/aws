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
# Skills an analyst uploaded, and the built RAG index. Both were the last two
# things tying a request to a particular machine — see `sync_down` for why
# these two need a different pattern from the two above.
SKILL_PREFIX = "skills/"
RAG_INDEX_PREFIX = "rag_index/"


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


# ── sync a whole prefix ───────────────────────────────────────────────────
def list_names(prefix: str, suffix: str | None = None) -> set[str]:
    """Object names directly under `prefix`, with the prefix stripped.

    Returns an empty set both when nothing is there and when the listing
    failed, so callers must not read "empty" as "S3 is definitely empty" for
    anything destructive — see the prune guard in `sync_down`.
    """
    if not enabled():
        return set()
    names: set[str] = set()
    try:
        paginator = _client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                if name and "/" not in name and (not suffix or name.endswith(suffix)):
                    names.add(name)
    except Exception as e:
        log.warning("could not list %s: %s", prefix, e)
    return names



def sync_down(
    prefix: str,
    dest_dir: Path,
    *,
    suffix: str | None = None,
    prune: bool = False,
) -> list[Path]:
    """Make `dest_dir` match what S3 holds under `prefix`.

    `ensure_local` above is the right shape for datasets and model artifacts,
    because those are read *by id*: a request names the thing it wants, so a
    local miss is detectable and a pull can be triggered exactly then.

    Skills and the RAG index are not read that way. A skill is found by
    globbing a directory, and a missing file does not raise — it simply is not
    in the list, which is precisely the "answers differently" failure this is
    meant to prevent. So these need the whole prefix reconciled rather than one
    key fetched.

    Re-downloads when S3 holds something newer than the local copy, so an edit
    made on one node reaches the others. A freshly downloaded file gets the
    current time as its mtime, which is necessarily newer than the object's
    LastModified, so a synced file is not fetched again on the next pass.

    `prune` deletes local files that no longer exist in S3, which is how a
    delete propagates. Only pass it where S3 is genuinely authoritative for the
    whole directory — and note it is applied *only* when the listing succeeded,
    because treating an unreachable bucket as "nothing exists" would wipe the
    directory it was supposed to be protecting.

    Returns the paths actually written. Never raises: a sync failure leaves the
    node serving whatever it already had, which is the same trade every other
    S3 seam here makes.
    """
    dest_dir = Path(dest_dir)
    if not enabled():
        return []

    pulled: list[Path] = []
    seen: set[str] = set()
    try:
        paginator = _client().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket(), Prefix=prefix):
            for obj in page.get("Contents", []):
                name = obj["Key"][len(prefix):]
                # A key with a slash left in it belongs to a nested prefix this
                # caller did not ask for; ignore rather than flatten it.
                if not name or "/" in name:
                    continue
                if suffix and not name.endswith(suffix):
                    continue
                seen.add(name)
                dest = dest_dir / name
                if dest.exists() and dest.stat().st_mtime >= obj["LastModified"].timestamp():
                    continue
                dest_dir.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(dest.suffix + ".part")
                _client().download_file(bucket(), obj["Key"], str(tmp))
                tmp.replace(dest)
                pulled.append(dest)
    except Exception as e:
        log.warning("could not sync %s: %s", prefix, e)
        return pulled

    if prune and dest_dir.exists():
        for local in dest_dir.iterdir():
            if not local.is_file() or local.name in seen:
                continue
            if suffix and not local.name.endswith(suffix):
                continue
            try:
                local.unlink()
                log.info("removed %s — no longer in s3://%s", local.name, prefix)
            except OSError as e:
                log.warning("could not remove %s: %s", local, e)

    if pulled:
        log.info("synced %d file(s) from %s", len(pulled), prefix)
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

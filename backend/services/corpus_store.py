"""S3 as the source of record for the document corpus.

`rag_search` reads documents off the local filesystem, which is fine for one
developer and wrong the moment there is more than one node: an analyst uploads
a whitepaper, it lands on whichever pod served the request, and no other pod
can see it. That is the same single-node problem as the in-memory dicts, just
wearing a filesystem costume.

This module makes S3 the durable copy and the local directory a cache:

  push  — local corpus -> S3 (after an analyst uploads, or to seed the bucket)
  pull  — S3 -> local corpus (what a fresh node would do on startup)

The bucket has versioning enabled, so every revision of a whitepaper is
retained rather than overwritten. That matters more than durability here: it
is the audit trail a regulator asks for — which version of the methodology was
in force when a given number was published.

Entirely opt-in. With CMA_CORPUS_BUCKET unset every function is a no-op and
the app behaves exactly as before, so a developer with no AWS account is
unaffected.

CLI:  python -m services.corpus_store status|push|pull      (run from backend/)
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

log = logging.getLogger("cma.corpus_store")

# Only these are treated as corpus content — mirrors the patterns rag_search
# globs for, so push/pull and search agree on what the corpus is.
CORPUS_SUFFIXES = {
    ".md", ".txt", ".py", ".json",
    ".csv", ".xlsx", ".xls",
    ".pdf", ".docx", ".pptx",
}
DEFAULT_PREFIX = "docs/"


def bucket() -> str:
    return os.getenv("CMA_CORPUS_BUCKET", "").strip()


def prefix() -> str:
    raw = os.getenv("CMA_CORPUS_PREFIX", "").strip() or DEFAULT_PREFIX
    return raw if raw.endswith("/") else raw + "/"


def enabled() -> bool:
    return bool(bucket())


def docs_root() -> Path:
    """The local corpus directory — same resolution rag_search uses."""
    env_root = os.getenv("CMA_DOCS_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    return Path(__file__).resolve().parent.parent.parent / "sample_docs"


def _client():
    import boto3

    from cof.llm_config import bedrock_region

    # Corpus and models don't have to share a region, but defaulting to one
    # keeps a single-region lab single-region.
    region = os.getenv("CMA_CORPUS_REGION", "").strip() or bedrock_region()
    return boto3.client("s3", region_name=region)


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - matching S3 ETag, not a security use
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _remote_objects(client) -> dict[str, dict]:
    """Map of key-relative-to-prefix -> {etag, size}."""
    objects: dict[str, dict] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket(), Prefix=prefix()):
        for item in page.get("Contents", []):
            relative = item["Key"][len(prefix()):]
            if not relative:
                continue
            objects[relative] = {
                # A multipart ETag has a -N suffix and is not an md5, so it
                # can't be compared against a local digest.
                "etag": item["ETag"].strip('"'),
                "size": item["Size"],
            }
    return objects


def _local_files(root: Path) -> dict[str, Path]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in CORPUS_SUFFIXES
    }


def _needs_upload(path: Path, remote: dict | None) -> bool:
    if remote is None:
        return True
    if remote["size"] != path.stat().st_size:
        return True
    if "-" in remote["etag"]:
        # Multipart upload: no comparable digest, so fall back to size, which
        # we already know matches.
        return False
    return _md5(path) != remote["etag"]


# ── Operations ────────────────────────────────────────────────────────────
def push(root: Path | None = None) -> dict:
    """Upload new or changed local corpus files. Returns a summary."""
    if not enabled():
        return {"skipped": "CMA_CORPUS_BUCKET not set"}
    root = root or docs_root()
    if not root.is_dir():
        return {"error": f"local corpus not found: {root}"}

    client = _client()
    remote = _remote_objects(client)
    local = _local_files(root)

    uploaded: list[str] = []
    for relative, path in sorted(local.items()):
        if _needs_upload(path, remote.get(relative)):
            client.upload_file(str(path), bucket(), prefix() + relative)
            uploaded.append(relative)

    return {
        "bucket": bucket(),
        "prefix": prefix(),
        "local_files": len(local),
        "uploaded": len(uploaded),
        "unchanged": len(local) - len(uploaded),
        "keys": uploaded[:10],
    }


def pull(root: Path | None = None) -> dict:
    """Download remote corpus objects missing or differing locally.

    This is what a fresh node runs at startup so every replica sees the same
    corpus regardless of which one received the upload.
    """
    if not enabled():
        return {"skipped": "CMA_CORPUS_BUCKET not set"}
    root = root or docs_root()
    root.mkdir(parents=True, exist_ok=True)

    client = _client()
    remote = _remote_objects(client)

    downloaded: list[str] = []
    for relative, meta in sorted(remote.items()):
        if Path(relative).suffix.lower() not in CORPUS_SUFFIXES:
            continue
        destination = root / relative
        if destination.exists() and not _needs_upload(destination, meta):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        client.download_file(bucket(), prefix() + relative, str(destination))
        downloaded.append(relative)

    return {
        "bucket": bucket(),
        "remote_objects": len(remote),
        "downloaded": len(downloaded),
        "keys": downloaded[:10],
    }


def upload_one(path: Path, root: Path | None = None) -> str | None:
    """Push a single file just written locally. Best-effort by design.

    Called from the upload endpoint: an S3 hiccup must not fail an analyst's
    upload that already succeeded on disk, so failures are logged, not raised.
    """
    if not enabled():
        return None
    root = root or docs_root()
    try:
        relative = str(path.resolve().relative_to(root.resolve())).replace("\\", "/")
    except ValueError:
        log.warning("refusing to upload %s — outside the corpus root", path)
        return None
    key = prefix() + relative
    try:
        _client().upload_file(str(path), bucket(), key)
    except Exception as e:
        log.warning("corpus upload to s3://%s/%s failed: %s", bucket(), key, e)
        return None
    return key


def status() -> dict:
    root = docs_root()
    info: dict = {
        "enabled": enabled(),
        "bucket": bucket() or "(unset)",
        "prefix": prefix(),
        "local_root": str(root),
        "local_files": len(_local_files(root)) if root.is_dir() else 0,
    }
    if not enabled():
        return info
    try:
        client = _client()
        remote = _remote_objects(client)
        info["remote_objects"] = len(remote)
        versioning = client.get_bucket_versioning(Bucket=bucket()).get("Status", "Disabled")
        info["versioning"] = versioning
    except Exception as e:
        info["error"] = f"{type(e).__name__}: {e}"
    return info


if __name__ == "__main__":
    import json as _json
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    command = _sys.argv[1] if len(_sys.argv) > 1 else "status"
    actions = {"status": status, "push": push, "pull": pull}
    if command not in actions:
        print(f"usage: python -m services.corpus_store [{'|'.join(actions)}]")
        raise SystemExit(2)
    print(_json.dumps(actions[command](), indent=2))

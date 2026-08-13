"""Vector retrieval for `rag_search`, backed by Bedrock embeddings.

The built-in `rag_search` tool originally ranked chunks by term overlap, which
misses any query that doesn't reuse the corpus's vocabulary — ask about
"customer churn" and a whitepaper that says "attrition" scores zero. This
module supplies semantic ranking instead, and is deliberately optional: when
embeddings are unavailable (no AWS credentials, provider not set to bedrock,
boto3 missing, model not granted) `score_chunks` returns None and the caller
falls back to keyword scoring. The workbench must keep working for someone
running against the COF proxy with no AWS account at all.

Embeddings are cached on disk, keyed by a fingerprint of the exact chunk text
plus the model and dimension count. Any edit to the corpus changes the
fingerprint and triggers a rebuild, so there is no staleness to manage — the
failure mode that bit the hand-rolled version (duplicate, out-of-date chunks
polluting results) can't happen by construction.

Cost is negligible: the bundled corpus is ~126 chunks after de-duplication,
a fraction of a cent to embed once.

WHY THIS IS OFF BY DEFAULT
--------------------------
Measured on the bundled corpus with a 5-query set (see the Phase 2 notes),
plain keyword overlap scored 5/5 top-1 and MRR 1.000; hybrid rank fusion of
embeddings + keywords scored 3/5 and 0.733. Embeddings lose here for a
structural reason, not a tuning one: all 17 whitepapers describe deposit
balance models, so every chunk is a near-neighbour of every other and cosine
similarity has little signal to work with, while analysts querying this corpus
tend to use its own vocabulary — precisely where term overlap is strongest.

Vector retrieval should start winning as the corpus grows and diversifies
(the regulatory-filing case: many agencies, decades, heterogeneous language).
The path is built, cached, measured, and one env var away; turning it on
without re-measuring on the new corpus would be the mistake.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

log = logging.getLogger("cma.retrieval")

# Titan v2 supports 256/512/1024 dims and can normalize server-side, which
# makes cosine similarity a plain dot product.
DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
EMBED_DIMENSIONS = 1024
# Titan accepts far more, but a paragraph-sized chunk is the useful unit. The
# cap matters for the CSVs in the corpus: they contain no blank lines, so each
# becomes a single enormous chunk. Truncating keeps the header and first rows,
# which is what a semantic query about a dataset would match anyway.
MAX_CHARS_PER_CHUNK = 2000
# Fresh AWS accounts get low Bedrock throughput, and a corpus build is the
# burstiest thing this app does. Keep concurrency modest and let botocore's
# adaptive retry mode do client-side rate limiting on top — it backs off in
# response to observed throttling instead of hammering a fixed number of times.
_EMBED_WORKERS = int(os.getenv("CMA_EMBED_WORKERS", "4"))
_EMBED_MAX_ATTEMPTS = 10

_INDEX_DIR = Path(__file__).resolve().parent.parent / "data" / "rag_index"


def embed_model() -> str:
    return os.getenv("CMA_EMBED_MODEL", "").strip() or DEFAULT_EMBED_MODEL


def enabled() -> bool:
    """True when vector retrieval should be attempted at all.

    Off by default, and deliberately so — see the note on corpus size in the
    module docstring. Set CMA_RAG_VECTOR=on to turn it on; it additionally
    requires Bedrock mode, so an AWS credential sitting in the environment can
    never silently change retrieval behavior.
    """
    if os.getenv("CMA_RAG_VECTOR", "").strip().lower() not in ("on", "1", "true"):
        return False
    try:
        from cof.llm_config import is_bedrock_mode
    except ImportError:
        return False
    return is_bedrock_mode()


# ── Embedding ─────────────────────────────────────────────────────────────
def _bedrock_client():
    import boto3
    from botocore.config import Config

    from cof.llm_config import bedrock_region

    return boto3.client(
        "bedrock-runtime",
        region_name=bedrock_region(),
        config=Config(
            retries={"max_attempts": _EMBED_MAX_ATTEMPTS, "mode": "adaptive"},
            read_timeout=60,
        ),
    )


def _embed_one(client, model: str, text: str) -> list[float]:
    body = json.dumps(
        {
            "inputText": text[:MAX_CHARS_PER_CHUNK],
            "dimensions": EMBED_DIMENSIONS,
            "normalize": True,
        }
    )
    response = client.invoke_model(modelId=model, body=body)
    return json.loads(response["body"].read())["embedding"]


def embed_texts(texts: list[str], *, progress_every: int = 50) -> np.ndarray:
    """Embed texts, returning an (n, EMBED_DIMENSIONS) float32 array.

    Titan embeds one input per call, so the corpus build is parallelised. A
    boto3 client is safe to share across threads for concurrent calls.
    """
    client = _bedrock_client()
    model = embed_model()
    total = len(texts)
    done = 0
    vectors: list[list[float]] = []
    with ThreadPoolExecutor(max_workers=_EMBED_WORKERS) as pool:
        for vector in pool.map(lambda t: _embed_one(client, model, t), texts):
            vectors.append(vector)
            done += 1
            if total > progress_every and done % progress_every == 0:
                log.info("embedded %d/%d chunks", done, total)
    return np.asarray(vectors, dtype=np.float32)


# ── Cache ─────────────────────────────────────────────────────────────────
def _fingerprint(texts: list[str]) -> str:
    """Content hash over the exact chunk text, model, and dimensions.

    Hashing content rather than file mtimes means a rebuild happens when, and
    only when, what we would embed actually differs.
    """
    digest = hashlib.sha256()
    digest.update(f"{embed_model()}|{EMBED_DIMENSIONS}|{MAX_CHARS_PER_CHUNK}\n".encode())
    for text in texts:
        digest.update(text[:MAX_CHARS_PER_CHUNK].encode("utf-8", "replace"))
        digest.update(b"\x00")
    return digest.hexdigest()[:24]


def _cache_path(fingerprint: str) -> Path:
    return _INDEX_DIR / f"{fingerprint}.npz"


def _load_cached(fingerprint: str, count: int) -> np.ndarray | None:
    path = _cache_path(fingerprint)
    if not path.exists():
        return None
    try:
        vectors = np.load(path)["vectors"]
    except Exception as e:
        log.warning("rag index %s unreadable (%s) — rebuilding", path.name, e)
        return None
    if vectors.shape != (count, EMBED_DIMENSIONS):
        log.warning("rag index %s has unexpected shape %s — rebuilding",
                    path.name, vectors.shape)
        return None
    return vectors


def _save_cached(fingerprint: str, vectors: np.ndarray) -> None:
    try:
        _INDEX_DIR.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(_cache_path(fingerprint), vectors=vectors)
    except Exception as e:
        # A cache we can't write is a performance problem, not a correctness
        # one — the search already has its vectors in memory.
        log.warning("could not persist rag index: %s", e)


# ── Public ────────────────────────────────────────────────────────────────
def score_chunks(query: str, chunk_texts: list[str]) -> list[float] | None:
    """Cosine similarity of `query` against each chunk, or None if unavailable.

    None is the signal to fall back to keyword scoring; it is never an error
    the caller has to handle.
    """
    if not enabled() or not chunk_texts:
        return None

    fingerprint = _fingerprint(chunk_texts)
    try:
        vectors = _load_cached(fingerprint, len(chunk_texts))
        built = False
        if vectors is None:
            log.info("building rag index: %d chunks via %s",
                     len(chunk_texts), embed_model())
            vectors = embed_texts(chunk_texts)
            built = True

        query_vector = embed_texts([query])[0]
    except Exception as e:
        log.warning("vector retrieval unavailable (%s) — falling back to keywords", e)
        return None

    if built:
        _save_cached(fingerprint, vectors)

    # Titan normalizes, so the dot product IS cosine similarity.
    return (vectors @ query_vector).astype(float).tolist()


def warm_index(doc_dir: str = "") -> dict:
    """Build and cache the index for a corpus ahead of first use.

    Goes through the real `rag_search` handler rather than re-implementing the
    chunker, so the fingerprint is guaranteed to match what a live search
    computes. Without this the first analyst query pays the whole build.
    """
    import json as _json

    from agent.tools import _HANDLERS

    raw = _HANDLERS["rag_search"]({"query": "warm the index", "doc_dir": doc_dir,
                                   "top_k": 1})
    result = _json.loads(raw)
    return {"retrieval": result.get("retrieval"), "matches": len(result.get("matches", []))}


def index_status(chunk_texts: list[str]) -> dict:
    """Describe cache state for a corpus without building anything."""
    fingerprint = _fingerprint(chunk_texts)
    path = _cache_path(fingerprint)
    return {
        "enabled": enabled(),
        "model": embed_model(),
        "chunks": len(chunk_texts),
        "fingerprint": fingerprint,
        "cached": path.exists(),
        "cache_path": str(path),
    }


if __name__ == "__main__":
    # python -m agent.retrieval [doc_dir]   — run from backend/ to warm the
    # cache so the first real search is instant.
    import sys as _sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    _dir = _sys.argv[1] if len(_sys.argv) > 1 else ""
    if not enabled():
        print("vector retrieval disabled — set CMA_LLM_PROVIDER=bedrock "
              "(and leave CMA_RAG_VECTOR unset)")
        raise SystemExit(1)
    print(warm_index(_dir))

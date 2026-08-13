"""Resolve canvas model nodes to real model artifacts in S3.

The compiler is a pure function and must not know about registries, so this is
where a node's `ref_id` becomes something the worker can actually load. Kept
separate for that reason alone.

Layout contract
---------------
The worker downloads `s3://<bucket>/models/<model_dir>/` in full — artifact plus
its `_classes.py` sidecar, which the pickles need to unpickle. A registered
model's `artifact_path` is already directory-qualified
(`capital_planning/mdl-deposits-rdmaas.pkl`), so the mapping is direct:

    artifact_path              ->  model_dir / artifact
    capital_planning/x.pkl         capital_planning   x.pkl

Which means the S3 layout has to mirror the local one. `push_model_dirs` does
that, and is the step that makes a canvas node runnable rather than just
compilable.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("cma.workflow_artifacts")

MODEL_PREFIX = "models/"
ARTIFACT_SUFFIXES = {".pkl", ".joblib", ".onnx"}
# Sidecars and anything else the unpickler may need alongside the artifact.
SUPPORT_SUFFIXES = {".py", ".json", ".txt"}


def bucket() -> str:
    return os.getenv("CMA_CORPUS_BUCKET", "").strip()


def _model_roots() -> list[Path]:
    """Directories that hold model artifacts, in resolution order."""
    here = Path(__file__).resolve().parent.parent      # backend/
    roots = [here / "data" / "models", here.parent / "sample_models"]
    extra = os.getenv("CMA_MODELS_ROOT", "").strip()
    if extra:
        roots.insert(0, Path(extra))
    return [r for r in roots if r.is_dir()]


def resolve(nodes: list[dict]) -> dict[str, dict]:
    """Map canvas ref_ids to artifact references for the compiler.

    Only model/transform nodes whose ref_id is a registered model with an
    artifact are included. Anything unresolved is simply absent, and the
    compiler falls back to goal-seek for it — a node pointing at a model that
    was deleted should degrade, not fail the whole compile.
    """
    refs = {
        str((n.get("data") or {}).get("ref_id") or "")
        for n in nodes
        if (n.get("data") or {}).get("kind") in ("model", "transform")
    }
    refs.discard("")
    if not refs:
        return {}

    try:
        from routers.models_registry import load_model
    except Exception as e:  # pragma: no cover - import-order safety
        log.warning("model registry unavailable: %s", e)
        return {}

    resolved: dict[str, dict] = {}
    for ref in sorted(refs):
        model = load_model(ref)
        if not model or not model.artifact_path:
            continue
        path = Path(str(model.artifact_path).replace("\\", "/"))
        if path.suffix.lower() not in ARTIFACT_SUFFIXES:
            continue
        if not path.parent.name:
            log.warning("artifact_path %s has no directory, so its _classes.py "
                        "sidecar cannot be located; skipping", path)
            continue
        resolved[ref] = {
            "model_dir": path.parent.name,
            "artifact": path.name,
            # May be empty — the worker then orders columns from the model's own
            # feature_names, which these artifacts declare as a class attribute.
            "feature_columns": list(model.feature_columns or []),
            "output_kind": getattr(model, "output_kind", "scalar"),
        }
    return resolved


def push_model_dirs(dirs: list[str] | None = None) -> dict:
    """Upload local model directories to s3://<bucket>/models/<dir>/.

    Uploads the sidecars too, not just the artifacts: a .pkl without its
    `_classes.py` fails at unpickle time with a bare ModuleNotFoundError, which
    is a poor way to discover a missing file.
    """
    if not bucket():
        return {"skipped": "CMA_CORPUS_BUCKET not set"}

    import boto3

    from cof.llm_config import bedrock_region

    client = boto3.client(
        "s3", region_name=os.getenv("CMA_CORPUS_REGION", "").strip() or bedrock_region()
    )

    uploaded: list[str] = []
    for root in _model_roots():
        for directory in sorted(p for p in root.iterdir() if p.is_dir()):
            if dirs and directory.name not in dirs:
                continue
            for path in sorted(directory.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts:
                    continue
                if path.suffix.lower() not in (ARTIFACT_SUFFIXES | SUPPORT_SUFFIXES):
                    continue
                key = f"{MODEL_PREFIX}{directory.name}/{path.relative_to(directory).as_posix()}"
                client.upload_file(str(path), bucket(), key)
                uploaded.append(key)

    return {"bucket": bucket(), "uploaded": len(uploaded),
            "keys": uploaded[:12]}


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path as _Path

    from dotenv import load_dotenv

    sys.path.insert(0, ".")
    load_dotenv(_Path(".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(json.dumps(push_model_dirs(sys.argv[1:] or None), indent=2))

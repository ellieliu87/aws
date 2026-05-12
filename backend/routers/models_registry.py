"""Models router - function-scoped model registry.

Three creation paths:
1. Upload: bytes from disk (.pkl / .joblib / .onnx). We store the artifact and
   record metadata; we do NOT load arbitrary pickles for safety.
2. Build regression: train an OLS or logistic model in-app via scikit-learn on
   a bound dataset. Coefficients, intercept, and standard train metrics are
   captured and stored alongside the model.
3. From URI: register a pointer to an artifact in the company's model
   artifactory. We only persist the URI and metadata; we don't fetch.

Monitoring metrics are seeded with synthetic-but-plausible time series so the
UI looks alive. In production these would be appended each time a run scores
fresh data against the model.
"""
import json
import os
import random
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from models.schemas import (
    FromArtifactoryRequest,
    FromUriRequest,
    ModelMetric,
    RegressionRequest,
    TrainedModel,
)
from packs import is_pack_visible
from routers.auth import get_current_user, get_current_user_groups
from routers.datasets import _DATASETS, _read_dataframe, _resolve_path

router = APIRouter()

ARTIFACT_ROOT = Path(__file__).resolve().parent.parent / "data" / "models"
ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SUPPORTED_MODEL_FORMATS = {"pkl", "pickle", "joblib", "onnx", "json"}

_MODELS: dict[str, TrainedModel] = {}


# ── pack-registered seed ingest ────────────────────────────────────────────
def _ingest_pack_models() -> None:
    """Pull model attachments registered by domain packs into `_MODELS`.

    Called once at startup after `packs.discover_and_register()`. Idempotent."""
    from packs import model_attachments

    try:
        from agent.model_introspect import introspect_artifact
    except Exception:
        introspect_artifact = None  # type: ignore[assignment]

    now = datetime.utcnow().isoformat() + "Z"
    for s in model_attachments():
        if s["model_id"] in _MODELS:
            continue
        try:
            src: Path = s["source_path"]
            if not src.exists():
                continue
            ext = src.suffix.lstrip(".")
            func_dir = ARTIFACT_ROOT / s["function_id"]
            func_dir.mkdir(parents=True, exist_ok=True)
            rel = f"{s['function_id']}/{s['model_id']}.{ext}"
            dest = ARTIFACT_ROOT / rel
            if not dest.exists():
                dest.write_bytes(src.read_bytes())
            # Bring along any sibling `_*.py` files (e.g. `_classes.py`)
            # so pickles that reference custom classes by module name can
            # resolve them inside the sandbox. The sandbox prepends the
            # artifact's directory to `sys.path` before unpickling.
            # `_*` convention skips top-level scripts like `generate.py`.
            for sibling in src.parent.glob("_*.py"):
                target = func_dir / sibling.name
                if not target.exists() or target.read_bytes() != sibling.read_bytes():
                    target.write_bytes(sibling.read_bytes())
            introspection = None
            if introspect_artifact is not None:
                try:
                    introspection = introspect_artifact(dest, ext)
                except Exception:
                    introspection = None
            m = TrainedModel(
                id=s["model_id"],
                function_id=s["function_id"],
                name=s["name"],
                description=s["description"],
                source_kind="upload",
                model_type="uploaded",
                artifact_path=rel,
                file_format=ext,
                size_bytes=dest.stat().st_size,
                introspection=introspection,
                train_metrics=s.get("train_metrics", {}) or {},
                monitoring_metrics=_seed_monitoring("uploaded", 0.92),
                created_at=now,
                pack_id=s.get("pack_id"),
                # Pre-baked workflow execution config — pack ships ready
                # for the canvas to wire up dataset → model → CSV.
                feature_mapping=s.get("feature_mapping") or {},
                pre_transform=s.get("pre_transform"),
                output_kind=s.get("output_kind") or "scalar",
                class_labels=s.get("class_labels") or [],
                target_names=s.get("target_names") or [],
                forecast_steps=s.get("forecast_steps"),
            )
            _MODELS[m.id] = m
        except Exception:
            continue


# ── helpers ─────────────────────────────────────────────────────────────────
def _seed_monitoring(model_type: str, train_value: float) -> list[ModelMetric]:
    """Generate a 12-week monitoring trace decaying around the train metric."""
    rng = random.Random(hash(model_type) & 0xFFFFFFFF)
    metrics: list[ModelMetric] = []
    base_metric = "auc" if model_type == "logistic" else "r2"
    for i in range(12):
        d = (datetime.utcnow() - timedelta(weeks=11 - i)).strftime("%Y-%m-%d")
        # Slight degradation + noise
        drift = 0.01 * (i / 11)
        val = max(0.0, train_value - drift + rng.uniform(-0.02, 0.02))
        metrics.append(ModelMetric(name=base_metric, value=round(val, 4), asof=d))
        psi = 0.02 + (i / 11) * 0.08 + rng.uniform(-0.015, 0.015)
        metrics.append(ModelMetric(name="psi", value=round(max(0.0, psi), 4), asof=d))
    return metrics


def _frame_for_dataset(dataset_id: str) -> pd.DataFrame:
    d = _DATASETS.get(dataset_id)
    if not d:
        raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")
    if d.source_kind == "upload" and d.file_path and d.file_format:
        try:
            return _read_dataframe(_resolve_path(d), d.file_format)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed reading dataset: {e}")
    # sql_table — synthesize a small training frame from the column dtypes
    n = 250
    rng = np.random.default_rng(42)
    cols: dict[str, np.ndarray] = {}
    for c in d.columns:
        if c.dtype.startswith("float"):
            cols[c.name] = rng.normal(0, 1, n)
        elif c.dtype.startswith("int"):
            cols[c.name] = rng.integers(0, 100, n).astype(float)
        elif c.dtype == "bool":
            cols[c.name] = rng.integers(0, 2, n).astype(int)
        elif c.dtype.startswith("datetime"):
            cols[c.name] = pd.date_range("2024-01-01", periods=n, freq="D")
        else:
            cols[c.name] = rng.choice(["A", "B", "C", "D"], size=n)
    df = pd.DataFrame(cols)
    # If we generated nothing numeric, still need numeric for regression demo
    return df


def _train_regression(
    df: pd.DataFrame,
    target: str,
    features: list[str],
    model_type: str,
) -> dict[str, Any]:
    if target not in df.columns:
        raise HTTPException(status_code=400, detail=f"Target column '{target}' not in dataset")
    missing = [f for f in features if f not in df.columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Feature columns not in dataset: {missing}")
    if not features:
        raise HTTPException(status_code=400, detail="At least one feature column is required")

    work = df[[target] + features].dropna()
    if len(work) < 5:
        raise HTTPException(status_code=400, detail="Need at least 5 rows after dropping NaNs")

    # Coerce features to numeric (one-hot for object cols)
    feat_df = pd.get_dummies(work[features], drop_first=True)
    feat_df = feat_df.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all").fillna(0)
    expanded_features = list(feat_df.columns)
    X = feat_df.to_numpy(dtype=float)
    y_raw = work[target]

    if model_type == "logistic":
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import accuracy_score, roc_auc_score

        # Coerce target to binary
        y_num = pd.to_numeric(y_raw, errors="coerce")
        if y_num.notna().sum() == len(y_raw):
            y_arr = (y_num > y_num.median()).astype(int).to_numpy()
        else:
            uniq = list(pd.Series(y_raw).astype(str).unique())[:2]
            y_arr = pd.Series(y_raw).astype(str).map(lambda v: 1 if v == uniq[0] else 0).to_numpy()
        if len(set(y_arr)) < 2:
            raise HTTPException(status_code=400, detail="Target must have at least 2 classes for logistic regression")

        model = LogisticRegression(max_iter=200, solver="lbfgs")
        model.fit(X, y_arr)
        pred = model.predict(X)
        proba = model.predict_proba(X)[:, 1]
        train_metrics = {
            "accuracy": round(float(accuracy_score(y_arr, pred)), 4),
            "auc": round(float(roc_auc_score(y_arr, proba)), 4),
            "n_train": int(len(work)),
        }
        coefs = {f: round(float(c), 6) for f, c in zip(expanded_features, model.coef_[0].tolist())}
        intercept = float(model.intercept_[0])
        seed_metric = train_metrics["auc"]
    else:  # ols
        from sklearn.linear_model import LinearRegression
        from sklearn.metrics import mean_absolute_error, r2_score

        y_num = pd.to_numeric(y_raw, errors="coerce")
        if y_num.isna().any():
            raise HTTPException(status_code=400, detail="OLS target must be numeric")
        y_arr = y_num.to_numpy()
        model = LinearRegression()
        model.fit(X, y_arr)
        pred = model.predict(X)
        train_metrics = {
            "r2": round(float(r2_score(y_arr, pred)), 4),
            "mae": round(float(mean_absolute_error(y_arr, pred)), 4),
            "n_train": int(len(work)),
        }
        coefs = {f: round(float(c), 6) for f, c in zip(expanded_features, model.coef_.tolist())}
        intercept = float(model.intercept_)
        seed_metric = max(0.0, train_metrics["r2"])

    return {
        "coefficients": coefs,
        "intercept": round(intercept, 6),
        "feature_columns": expanded_features,
        "train_metrics": train_metrics,
        "seed_metric": seed_metric,
    }


# Seed bundled sample models (no-op if registry already has entries).
# Pack-registered models are ingested by the startup hook in main.py.


# ── routes ─────────────────────────────────────────────────────────────────
@router.get("", response_model=list[TrainedModel])
async def list_models(
    function_id: str | None = Query(default=None),
    groups: list[str] = Depends(get_current_user_groups),
):
    items = [m for m in _MODELS.values() if is_pack_visible(m.pack_id, groups)]
    if function_id:
        items = [m for m in items if m.function_id == function_id]
    items.sort(key=lambda m: m.created_at, reverse=True)
    return items


@router.get("/{model_id}", response_model=TrainedModel)
async def get_model(model_id: str, _: str = Depends(get_current_user)):
    m = _MODELS.get(model_id)
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    return m


@router.delete("/{model_id}", status_code=204)
async def delete_model(model_id: str, _: str = Depends(get_current_user)):
    m = _MODELS.pop(model_id, None)
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    if m.artifact_path:
        try:
            (ARTIFACT_ROOT / m.artifact_path).unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/build-regression", response_model=TrainedModel, status_code=201)
async def build_regression(req: RegressionRequest, _: str = Depends(get_current_user)):
    df = _frame_for_dataset(req.dataset_id)
    fit = _train_regression(df, req.target_column, req.feature_columns, req.model_type)

    mid = f"mdl-{uuid.uuid4().hex[:10]}"
    now = datetime.utcnow().isoformat() + "Z"
    m = TrainedModel(
        id=mid,
        function_id=req.function_id,
        name=req.name,
        description=req.description,
        source_kind="regression",
        model_type=req.model_type,
        target_column=req.target_column,
        feature_columns=fit["feature_columns"],
        coefficients=fit["coefficients"],
        intercept=fit["intercept"],
        train_metrics=fit["train_metrics"],
        monitoring_metrics=_seed_monitoring(req.model_type, fit["seed_metric"]),
        dataset_id=req.dataset_id,
        created_at=now,
        last_run=now,
    )
    _MODELS[mid] = m
    return m


@router.post("/upload", response_model=TrainedModel, status_code=201)
async def upload_model(
    function_id: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    # ── Workflow-execution config (all optional). The frontend's upload
    #    modal serializes the structured fields as JSON strings since
    #    multipart can't carry nested objects natively.
    output_kind: Annotated[str, Form()] = "scalar",
    feature_mapping_json: Annotated[str | None, Form()] = None,
    pre_transform: Annotated[str | None, Form()] = None,
    class_labels_csv: Annotated[str | None, Form()] = None,
    target_names_csv: Annotated[str | None, Form()] = None,
    forecast_steps: Annotated[int | None, Form()] = None,
    _: str = Depends(get_current_user),
):
    if not file.filename:
        raise HTTPException(status_code=400, detail="Filename required")
    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in SUPPORTED_MODEL_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model format '.{ext}'. Supported: {sorted(SUPPORTED_MODEL_FORMATS)}",
        )
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Model too large ({len(contents):,} bytes)")

    mid = f"mdl-{uuid.uuid4().hex[:10]}"
    func_dir = ARTIFACT_ROOT / function_id
    func_dir.mkdir(parents=True, exist_ok=True)
    rel = f"{function_id}/{mid}.{ext}"
    abs_path = ARTIFACT_ROOT / rel
    abs_path.write_bytes(contents)

    # Best-effort introspection of the uploaded artifact
    from agent.model_introspect import introspect_artifact
    introspection = introspect_artifact(abs_path, ext)

    # Parse the structured execution-config fields the modal sent.
    try:
        feature_mapping = json.loads(feature_mapping_json) if feature_mapping_json else {}
    except json.JSONDecodeError:
        feature_mapping = {}
    if not isinstance(feature_mapping, dict):
        feature_mapping = {}
    class_labels = [c.strip() for c in (class_labels_csv or "").split(",") if c.strip()]
    target_names = [c.strip() for c in (target_names_csv or "").split(",") if c.strip()]
    if output_kind not in ("scalar", "probability_vector", "n_step_forecast", "multi_target"):
        output_kind = "scalar"

    now = datetime.utcnow().isoformat() + "Z"
    m = TrainedModel(
        id=mid,
        function_id=function_id,
        name=name or file.filename.rsplit(".", 1)[0],
        description=description,
        source_kind="upload",
        model_type="uploaded",
        artifact_path=rel,
        file_format=ext,
        size_bytes=len(contents),
        introspection=introspection,
        train_metrics={"uploaded": 1.0, "size_bytes": float(len(contents))},
        monitoring_metrics=_seed_monitoring("uploaded", 0.85),
        created_at=now,
        feature_mapping=feature_mapping,
        pre_transform=pre_transform or None,
        output_kind=output_kind,  # type: ignore[arg-type]
        class_labels=class_labels,
        target_names=target_names,
        forecast_steps=forecast_steps,
    )
    _MODELS[mid] = m
    return m


@router.post("/{model_id}/reintrospect", response_model=TrainedModel)
async def reintrospect_model(model_id: str, _: str = Depends(get_current_user)):
    """Re-run introspection on a previously uploaded artifact.

    Useful if you've added the model's class definitions to the Python path
    after upload, or if you've re-uploaded the file to disk manually.
    """
    m = _MODELS.get(model_id)
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    if m.source_kind != "upload" or not m.artifact_path or not m.file_format:
        raise HTTPException(status_code=400, detail="Only uploaded artifacts can be re-introspected")
    abs_path = ARTIFACT_ROOT / m.artifact_path
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file is missing on disk")
    from agent.model_introspect import introspect_artifact
    m.introspection = introspect_artifact(abs_path, m.file_format)
    return m


@router.post("/from-uri", response_model=TrainedModel, status_code=201)
async def register_from_uri(req: FromUriRequest, _: str = Depends(get_current_user)):
    mid = f"mdl-{uuid.uuid4().hex[:10]}"
    now = datetime.utcnow().isoformat() + "Z"

    # If the URI names a preinstalled package, attach its rich metadata
    # to `introspection` so the model-explainer agent can describe what
    # the package does. Falls through to None for any other URI scheme.
    from routers.preinstalled_packages import (
        get_package_metadata,
        package_id_from_uri,
    )
    pkg_id = package_id_from_uri(req.artifactory_uri)
    pkg_meta = get_package_metadata(pkg_id) if pkg_id else None

    # For preinstalled packages, the package registry IS the authoritative
    # documentation. Skip the synthetic placeholder train_metrics and
    # monitoring_metrics seeds — they would just give the model-explainer
    # agent fake numbers to mistakenly cite (e.g. R²=0.78 isn't a real
    # metric for an aggregator like NII Calculator). Empty defaults force
    # the agent to rely on `introspection` alone.
    if pkg_meta is not None:
        train_metrics: dict[str, float] = {}
        monitoring_metrics: list = []
        # Promote the package's `purpose` paragraph to `description` when
        # the caller didn't supply a richer one. That way the model card
        # in the UI shows the full purpose, not just the dropdown's
        # one-line label.
        description = req.description or pkg_meta.get("purpose") or req.name
    else:
        train_metrics = {"reference": 1.0}
        monitoring_metrics = _seed_monitoring("external", 0.78)
        description = req.description

    m = TrainedModel(
        id=mid,
        function_id=req.function_id,
        name=req.name,
        description=description,
        source_kind="uri",
        model_type=req.model_type,
        artifactory_uri=req.artifactory_uri,
        train_metrics=train_metrics,
        monitoring_metrics=monitoring_metrics,
        introspection=pkg_meta,  # None for non-preinstalled URIs
        created_at=now,
    )
    _MODELS[mid] = m
    return m


# ── Install from Artifactory (pip) ──────────────────────────────────────────
def _pip_install(package_name: str, timeout_sec: int = 180) -> tuple[bool, str]:
    """Run `pip install <package_name>` in the backend's interpreter.

    Forces the source distribution (`.tar.gz` sdist) for the named
    package — corporate Artifactory ships these as tar.gz, so we skip
    wheel resolution entirely and install straight from sdist.

    Returns (ok, combined_log). The combined log captures stdout+stderr
    and is surfaced to the analyst on failure so corporate Artifactory
    routing issues / 404s are visible in the UI.
    """
    import subprocess
    import sys
    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "pip", "install",
                "--upgrade",
                # `--no-binary <pkg>` tells pip to ignore any wheel for
                # this package and install from the .tar.gz sdist that
                # the corporate Artifactory publishes.
                "--no-binary", package_name,
                package_name,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        log = (proc.stdout or "") + (proc.stderr or "")
        return (proc.returncode == 0), log[-4000:]
    except subprocess.TimeoutExpired:
        return False, f"`pip install {package_name}` timed out after {timeout_sec}s."


# Method/function names a library might expose for prediction, ranked by
# preference (sklearn convention first, then time-series + generic names).
_PREDICT_NAMES = ("predict", "forecast", "infer", "score", "run", "simulate", "__call__")


def _entry_metadata(entry, mod) -> dict[str, Any]:
    """Pull `output_kind`, `target_names`, `class_labels`, `feature_names`
    off the entry (class or function), with module-level constants as
    fallback (e.g. `OUTPUT_KIND = "..."`).

    Library authors declare these once on whatever surface is natural for
    their library — class attrs for OO libs, function attrs for FP libs,
    or module constants when neither feels right.
    """
    def _read(name: str, fallback_const: str):
        v = getattr(entry, name, None)
        if v in (None, [], "", {}):
            v = getattr(mod, fallback_const, None)
        return v

    return {
        "output_kind":   _read("output_kind",   "OUTPUT_KIND"),
        "target_names":  list(_read("target_names",  "TARGET_NAMES")  or []),
        "class_labels":  list(_read("class_labels",  "CLASS_LABELS")  or []),
        "feature_names": list(_read("feature_names", "FEATURE_NAMES") or []),
    }


def _resolve_package_entry(module_name: str, entry_hint: str | None):
    """Pick the prediction entry-point out of a freshly-installed package.

    Returns a tuple `(callable_to_pickle, source_entry, metadata)` where:
      - `callable_to_pickle` is what we serialize to disk so the sandbox
        can invoke it. Always callable: an instance (with `.predict`),
        a bound method (`instance.<method>`), or a module-level function.
      - `source_entry` is the class or function we discovered (used for
        attribute introspection).
      - `metadata` is the extracted output_kind / target_names / etc.

    Discovery order:
      1. Honor `entry_hint` if set — look it up by name on the module.
      2. Walk the module's namespace, ranking each top-level name:
           class with `.predict`           → 100  (sklearn convention)
           class with `.forecast/.run/...` →  80
           function named `predict/...`    →  70
           class with `.__call__`          →  60
      3. Prefer candidates whose name matches the package, then take the
         highest score.
    """
    import importlib
    import inspect
    importlib.invalidate_caches()
    try:
        mod = importlib.import_module(module_name)
    except ImportError as e:
        raise RuntimeError(f"`import {module_name}` failed after pip install: {e}")
    # Best-effort reload so a re-install of the same package picks up the
    # new bytes. Some modules don't have a loader spec (synthetic / test
    # fixtures, namespace packages without an __init__) — skip cleanly.
    try:
        importlib.reload(mod)
    except (ModuleNotFoundError, ImportError):
        pass

    def _is_local(obj) -> bool:
        # Reject things re-exported from elsewhere — we want the package's
        # own entries, not transitively-imported ones.
        return (getattr(obj, "__module__", "") or "").startswith(module_name)

    def _build_callable(entry, name: str):
        """Turn a discovered entry into a (callable, method_used) pair.
        For classes with `.predict`, returns the instance (sandbox calls
        `.predict(X)`). For classes with another method, returns the bound
        method. For functions, returns the function itself."""
        if inspect.isclass(entry):
            for method_name in _PREDICT_NAMES:
                if callable(getattr(entry, method_name, None)):
                    try:
                        instance = entry()
                    except TypeError as e:
                        raise RuntimeError(
                            f"Could not instantiate `{name}` with no-arg constructor: {e}. "
                            "Provide a no-arg factory or pin an explicit `entry_name`."
                        )
                    if method_name == "predict":
                        return instance, "predict"
                    return getattr(instance, method_name), method_name
            raise RuntimeError(
                f"Class `{name}` has no callable predict / forecast / infer / score / run / __call__ method."
            )
        if callable(entry):
            return entry, "__call__"
        raise RuntimeError(f"Entry `{name}` is neither a class nor a callable.")

    # ── 1. explicit hint ──────────────────────────────────────────────────
    if entry_hint:
        entry = getattr(mod, entry_hint, None)
        if entry is None:
            raise RuntimeError(
                f"`{entry_hint}` not found in `{module_name}`. "
                f"Available top-level names: "
                f"{sorted(n for n in dir(mod) if not n.startswith('_'))}"
            )
        callable_obj, method = _build_callable(entry, entry_hint)
        return callable_obj, entry, _entry_metadata(entry, mod), method

    # ── 2. heuristic discovery ────────────────────────────────────────────
    scored: list[tuple[int, str, Any]] = []  # (score, name, entry)
    for attr_name in dir(mod):
        if attr_name.startswith("_"):
            continue
        obj = getattr(mod, attr_name)
        if not _is_local(obj):
            continue

        if inspect.isclass(obj):
            if callable(getattr(obj, "predict", None)):
                scored.append((100, attr_name, obj))
                continue
            for i, m in enumerate(_PREDICT_NAMES[1:], start=1):
                if callable(getattr(obj, m, None)):
                    scored.append((90 - i * 5, attr_name, obj))
                    break
            continue

        if inspect.isfunction(obj) or inspect.isbuiltin(obj):
            if attr_name in _PREDICT_NAMES:
                scored.append((70, attr_name, obj))
            continue

    if not scored:
        raise RuntimeError(
            f"`{module_name}` was installed but no obvious prediction entry-point "
            "was found. Looked for classes with predict/forecast/infer/score/run "
            "methods and module-level functions of the same names. "
            "Pass `class_name` to pin a specific entry."
        )

    # Prefer candidates whose name resembles the package
    norm = module_name.replace("_", "").lower()
    boosted = [
        (s + 5, n, e) if (n.replace("_", "").lower() in norm or norm in n.replace("_", "").lower())
        else (s, n, e)
        for (s, n, e) in scored
    ]
    boosted.sort(key=lambda x: -x[0])
    _, name, entry = boosted[0]
    callable_obj, method = _build_callable(entry, name)
    return callable_obj, entry, _entry_metadata(entry, mod), method


@router.post("/from-artifactory", response_model=TrainedModel, status_code=201)
async def register_from_artifactory(req: FromArtifactoryRequest, _: str = Depends(get_current_user)):
    """Install a pip-resolvable model package and register it as a model.

    The flow mirrors file-upload registration once the install lands, so
    the canvas + sandbox path is identical (introspection, feature-name
    discovery, multi_target post-processing) — the only difference is
    where the artifact came from."""
    import pickle

    # 1) pip install — capture stderr verbatim so corporate Artifactory
    #    routing failures (403, 404, proxy errors) surface in the UI.
    ok, log = _pip_install(req.package_name)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"`pip install {req.package_name}` failed.",
                "log": log,
                "hint": (
                    "If you're outside the corporate proxy, the package may not be "
                    "resolvable. Check the package name and your pip index configuration."
                ),
            },
        )

    # 2) Resolve the prediction entry. Could be a class instance (when
    #    the class exposes `.predict`), a bound method (when it exposes
    #    `.forecast/.run/...`), or a top-level function. All three are
    #    pickleable and callable, so the sandbox treats them uniformly.
    module_name = req.package_name.replace("-", "_")
    try:
        callable_obj, source_entry, meta, method_used = _resolve_package_entry(
            module_name, req.class_name,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 3) Persist the callable as a .pkl. Bound methods + module-level
    #    functions both pickle cleanly by qualname; the sandbox just
    #    `pickle.load`s and either calls `.predict(X)` (when the
    #    artifact is an instance) or `model(X)` (otherwise).
    mid = f"mdl-{uuid.uuid4().hex[:10]}"
    func_dir = ARTIFACT_ROOT / req.function_id
    func_dir.mkdir(parents=True, exist_ok=True)
    rel = f"{req.function_id}/{mid}.pkl"
    abs_path = ARTIFACT_ROOT / rel
    try:
        with abs_path.open("wb") as fh:
            pickle.dump(callable_obj, fh)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Could not pickle the installed entry: {e}. "
                "If this is a closure or lambda, expose it as a module-level "
                "function or class instead."
            ),
        )

    # 4) Best-effort introspection so feature names / metadata appear in
    #    the model card right away.
    from agent.model_introspect import introspect_artifact
    introspection = introspect_artifact(abs_path, "pkl")

    # 5) Resolve output shape: prefer what the library declares, then
    #    fall back to a heuristic based on the discovered method.
    declared_kind   = meta["output_kind"]
    declared_targets = meta["target_names"]
    declared_labels = meta["class_labels"]

    if declared_kind in ("scalar", "probability_vector", "n_step_forecast", "multi_target"):
        detected_output_kind = declared_kind
    elif declared_targets:
        detected_output_kind = "multi_target"
    elif declared_labels or callable(getattr(source_entry, "predict_proba", None)):
        detected_output_kind = "probability_vector"
    else:
        detected_output_kind = "scalar"

    # Pin the discovered method into the description so the analyst can
    # see at a glance how the package is being invoked.
    base_desc = req.description or f"Installed from Artifactory: `{req.package_name}`"
    method_tag = f" · invoked via `{method_used}`" if method_used and method_used != "predict" else ""

    now = datetime.utcnow().isoformat() + "Z"
    m = TrainedModel(
        id=mid,
        function_id=req.function_id,
        name=req.name,
        description=base_desc + method_tag,
        source_kind="upload",          # uses the same sandbox path as file-upload
        model_type="uploaded",
        artifact_path=rel,
        file_format="pkl",
        size_bytes=abs_path.stat().st_size,
        introspection=introspection,
        artifactory_uri=f"pip://{req.package_name}",
        feature_columns=list(meta["feature_names"] or []),
        train_metrics={
            "uploaded": 1.0,
            "size_bytes": float(abs_path.stat().st_size),
        },
        monitoring_metrics=_seed_monitoring("uploaded", 0.85),
        created_at=now,
        output_kind=detected_output_kind,  # type: ignore[arg-type]
        class_labels=declared_labels,
        target_names=declared_targets,
        # Per-run knobs like forecast_steps are configured at workflow-
        # build time, not install time.
        forecast_steps=None,
    )
    _MODELS[mid] = m
    return m


@router.get("/{model_id}/metrics")
async def get_metrics(model_id: str, _: str = Depends(get_current_user)):
    m = _MODELS.get(model_id)
    if not m:
        raise HTTPException(status_code=404, detail="Model not found")
    # Group by metric name into series
    series: dict[str, list[dict[str, Any]]] = {}
    for entry in m.monitoring_metrics:
        series.setdefault(entry.name, []).append({"asof": entry.asof, "value": entry.value})
    return {"model_id": model_id, "series": series, "train_metrics": m.train_metrics}

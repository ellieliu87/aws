"""Scenarios + Analytics Runs router.

A Scenario is a named macro path. It can come from three places:
1. **Built-ins** — bank-wide Base / Adverse / Severely Adverse / Outlook, seeded
   below with reference rate-and-macro paths.
2. **Upload** — analyst uploads a CSV / Parquet / XLSX / JSON. The plumbing
   reuses the Datasets pipeline (we register the file as a Dataset *and* a
   Scenario in one step).
3. **SQL table** — analyst points at a table in a connected data source
   (e.g. `CMA.PUBLIC.SCENARIOS`).

An Analytics Run takes (model, scenario, horizon) and produces a result series.
For built-in models (regression-trained in-app) we apply the linear / logistic
formula to the scenario's macro variables. For uploaded / external models we
synthesize a plausible response curve so the UI works end-to-end.
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from models.schemas import (
    AnalyticsRun,
    DestinationWrite,
    RunRequest,
    SavedWorkflow,
    SavedWorkflowCreate,
    SavedWorkflowSummary,
    Scenario,
    ScenarioCreateFromDataset,
    WorkflowEdge,
    WorkflowNode,
    WorkflowRequest,
    WorkflowResult,
    WorkflowValidationIssue,
    WorkflowValidationResult,
)
from routers.auth import get_current_user
from routers.datasets import _read_dataframe, _resolve_path, load_dataset
from routers.models_registry import load_model, store_model

router = APIRouter()


log = logging.getLogger("cma.scenarios")

# ── scenario persistence ───────────────────────────────────────────────────
# There used to be one `_SCENARIOS` dict here holding two different kinds of
# thing, and the mixture was the whole problem.
#
# Built-in scenarios are *derived*. `services/data_services.py` recomputes them
# from packs and the CCAR/Outlook cards on every startup, so every replica
# arrives at the same set without talking to anything. Those are a cache, and a
# cache is exactly the right home for them.
#
# Scenarios minted by `POST /scenarios/from-dataset` are not derived from
# anything. An analyst points at their own dataset and gets an `scn-…` record
# that exists nowhere else. In one dict with the built-ins those died with the
# process and were invisible to a second replica — the same data-loss shape the
# rest of this migration exists to fix, just smaller and noticed later.
#
# The codebase was already saying so out loud: `delete_scenario` refuses when
# `source_kind == "builtin"`, which is a special case that only exists because
# two kinds of thing shared one home. So they no longer do. Built-ins stay in
# the dict below; analyst-created ones go in the table, beside datasets, models
# and workflows.
#
# `load_scenario` / `all_scenarios` are what callers use, and they merge the
# two. Built-ins are checked first deliberately: they are the overwhelming
# majority of lookups and a dict hit costs nothing, so the network read only
# happens for ids that can only be analyst-created.
_SCENARIO_ENTITY = "scenario"

_BUILTIN_SCENARIOS: dict[str, Scenario] = {}


def load_scenario(scenario_id: str) -> Scenario | None:
    from services import entity_store

    builtin = _BUILTIN_SCENARIOS.get(scenario_id)
    if builtin is not None:
        return builtin
    record = entity_store.get(_SCENARIO_ENTITY, scenario_id)
    return Scenario(**record) if record else None


def store_scenario(scenario: Scenario) -> Scenario:
    from services import entity_store

    entity_store.put(_SCENARIO_ENTITY, scenario.id, scenario.model_dump())
    return scenario


def remove_scenario(scenario_id: str) -> None:
    from services import entity_store

    entity_store.delete(_SCENARIO_ENTITY, scenario_id)


def all_scenarios() -> list[Scenario]:
    from services import entity_store

    out: list[Scenario] = list(_BUILTIN_SCENARIOS.values())
    for record in entity_store.list_all(_SCENARIO_ENTITY):
        try:
            out.append(Scenario(**record))
        except Exception as e:
            # One stored item that no longer matches the schema must not take
            # the whole palette down with it.
            log.warning("skipping unreadable scenario %s: %s", record.get("id"), e)
    return out


# ── run persistence ────────────────────────────────────────────────────────
# Runs differ from the other registries in two ways that matter.
#
# They are append-only and unbounded — one item per execution, forever, where
# datasets and models are a fixed handful. And each one carries its whole
# result `series`, so a run item is orders of magnitude larger than a model.
# `_input_dataframe` caps rows at `horizon`, which keeps the normal case to a
# few KB, but `horizon_months` has no upper bound, so `store_run` cannot
# assume the item fits in DynamoDB's 400 KB limit.
_RUN_ENTITY = "run"

# 400 KB is the hard item limit; leave room for the key attributes and for
# Decimal encoding being wider than the JSON we measure.
_MAX_RUN_BYTES = 380 * 1024

# How long a run stays on the history page before DynamoDB sweeps it.
#
# gsi1 made reading the newest N cheap regardless of how many exist; it did
# nothing about the fact that the number only ever goes up. This is the
# decision that bounds it, and it is a judgement rather than a tuning knob: a
# run is scratch, not a record. Everything a number was derived from — the
# dataset, the model, the saved workflow, the compiled plan versions in S3 —
# is kept indefinitely, so a result stays reproducible long after the run row
# that first reported it has gone.
#
# Set `CMA_RUN_TTL_DAYS=0` to keep runs forever, which is the right setting
# the day someone has to answer "what exactly did we file, and when" from the
# run history itself rather than from its inputs.
_RUN_TTL_DAYS_DEFAULT = 90

# Page size for the run history. The cap exists because the index makes a
# large page genuinely expensive again — a page of runs carries their series.
RUNS_PAGE_DEFAULT = 200
RUNS_PAGE_MAX = 500


def _run_index_pk(function_id: str) -> str:
    return f"{_RUN_ENTITY}#{function_id}"


def _run_ttl() -> int | None:
    """Epoch seconds at which a run written now becomes eligible for deletion.

    Returns None when the TTL is disabled, which is also what an unreadable
    setting falls back to: getting this wrong in the permissive direction
    leaves runs lying around, and getting it wrong in the other direction
    deletes them. Only one of those is recoverable.
    """
    raw = os.getenv("CMA_RUN_TTL_DAYS", "").strip()
    try:
        days = int(raw) if raw else _RUN_TTL_DAYS_DEFAULT
    except ValueError:
        log.warning("CMA_RUN_TTL_DAYS=%r is not a number — keeping runs forever", raw)
        return None
    if days <= 0:
        return None
    # From now rather than from `run.created_at`: the two are the same value in
    # every current caller, and parsing the stored string to rediscover it
    # would add a failure mode for no gain.
    return int(time.time()) + days * 86400


def load_run(run_id: str) -> AnalyticsRun | None:
    from services import entity_store

    record = entity_store.get(_RUN_ENTITY, run_id)
    return AnalyticsRun(**record) if record else None


def store_run(run: AnalyticsRun) -> AnalyticsRun:
    """Persist a run, trimming the series if the record would not fit.

    The computation already succeeded by the time this is called, so failing
    the request because its *history record* is oversized would be the wrong
    trade. Instead the row-level detail is dropped and the run is kept, with a
    note saying so; the caller still gets the full series back in the response.
    """
    from services import entity_store

    payload = run.model_dump()
    if entity_store.enabled():
        size = len(json.dumps(payload, default=str).encode("utf-8"))
        if size > _MAX_RUN_BYTES:
            log.warning(
                "run %s is %d bytes, over the %d limit — persisting without "
                "its %d series rows",
                run.id, size, _MAX_RUN_BYTES, len(run.series),
            )
            dropped = len(run.series)
            payload["series"] = []
            note = f"[series omitted from history: {dropped} rows exceeded the record size limit]"
            payload["notes"] = f"{run.notes} {note}".strip() if run.notes else note
    # Index by (function, time) so the history page can read the newest N
    # without walking every run ever recorded. `created_at` is an ISO-8601
    # UTC string, which sorts lexicographically in timestamp order — the
    # property the whole index depends on.
    #
    # `ttl` is the other half of that: the index made reads cheap, this keeps
    # the collection finite. Note it applies to the index entry too — a GSI
    # item goes when its base item does, so the history page never shows a run
    # the table no longer holds.
    entity_store.put(
        _RUN_ENTITY, run.id, payload,
        index=(_run_index_pk(run.function_id), run.created_at),
        ttl=_run_ttl(),
    )
    return run


def all_runs() -> list[AnalyticsRun]:
    from services import entity_store

    out: list[AnalyticsRun] = []
    for record in entity_store.list_all(_RUN_ENTITY):
        try:
            out.append(AnalyticsRun(**record))
        except Exception as e:
            log.warning("skipping unreadable run %s: %s", record.get("id"), e)
    return out


def remove_run(run_id: str) -> None:
    from services import entity_store

    entity_store.delete(_RUN_ENTITY, run_id)


# ── Built-in scenarios ──────────────────────────────────────────────────────
# Populated at startup by `services.data_services.materialize_into_scenarios_registry`.
# Each entry mirrors a CCAR or Outlook card on the Data tab → Data Services
# section, so the Workflow tab's Scenarios palette stays in sync with what
# the user has visible under Data.
BUILTIN_DATA: dict[str, dict[str, Any]] = {}


# ── Helpers ────────────────────────────────────────────────────────────────
def _scenario_dataframe(scenario_id: str) -> pd.DataFrame:
    """Return a (horizon_months × variables) wide-format DataFrame for the scenario."""
    sc = load_scenario(scenario_id)
    if not sc:
        raise HTTPException(status_code=404, detail="Scenario not found")
    if sc.source_kind == "builtin":
        data = BUILTIN_DATA[sc.id]
        rows = []
        for i in range(data["horizon_months"]):
            row = {"month": i + 1}
            for v in data["variables"]:
                row[v] = data["paths"][v][i]
            rows.append(row)
        return pd.DataFrame(rows)
    # upload / sql_table — parse the bound dataset
    if not sc.dataset_id:
        raise HTTPException(status_code=400, detail="Scenario has no underlying dataset")
    d = load_dataset(sc.dataset_id)
    if not d:
        raise HTTPException(status_code=404, detail="Bound dataset is missing")
    if d.source_kind == "upload" and d.file_path and d.file_format:
        try:
            df = _read_dataframe(_resolve_path(d), d.file_format)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not read scenario file: {e}")
        return df
    # sql_table — synthesize a 12-step series for the column names
    n = sc.horizon_months or 12
    rng = np.random.default_rng(hash(scenario_id) & 0xFFFFFFFF)
    cols: dict[str, list[float]] = {"month": list(range(1, n + 1))}
    for c in d.columns:
        if c.dtype.startswith(("float", "int")):
            cols[c.name] = list(np.linspace(0, 5, n) + rng.normal(0, 0.5, n))
    return pd.DataFrame(cols)


def _apply_model(model_id: str, scenario_df: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    m = load_model(model_id)
    if not m:
        raise HTTPException(status_code=404, detail=f"Model {model_id} not found")

    if m.source_kind == "regression" and m.coefficients:
        # Apply linear (or logistic) formula to scenario rows.
        # Match feature names case-insensitively so e.g. model.gdp aligns with
        # scenario.GDP — analysts shouldn't have to babysit casing.
        row_results: list[dict[str, Any]] = []
        used_features = list(m.coefficients.keys())
        col_lower_to_actual = {str(c).lower(): c for c in scenario_df.columns}
        matched: set[str] = set()
        for _, row in scenario_df.iterrows():
            xb = float(m.intercept or 0.0)
            for f in used_features:
                actual = col_lower_to_actual.get(f.lower())
                if actual is not None:
                    try:
                        xb += float(m.coefficients[f]) * float(row[actual])
                        matched.add(f)
                    except (TypeError, ValueError):
                        pass
            if m.model_type == "logistic":
                pred = 1.0 / (1.0 + np.exp(-xb))
            else:
                pred = xb
            entry = {"month": int(row.get("month", len(row_results) + 1)), "prediction": float(round(pred, 6))}
            for v in scenario_df.columns:
                if v != "month" and v in row.index:
                    try:
                        entry[v] = float(row[v])
                    except (TypeError, ValueError):
                        pass
            row_results.append(entry)
        preds = [r["prediction"] for r in row_results]
        unmatched = [f for f in used_features if f not in matched]
        summary = {
            "min_prediction": round(min(preds), 4),
            "max_prediction": round(max(preds), 4),
            "mean_prediction": round(float(np.mean(preds)), 4),
            "horizon_steps": len(preds),
            "model_type": m.model_type,
            "features_matched": len(matched),
            "features_unmatched": unmatched,
        }
        return row_results, summary

    # Uploaded artifact — sandboxed prediction via subprocess, then
    # post-processed per the model's declared output_kind.
    if m.source_kind == "upload" and m.artifact_path and m.file_format:
        return _apply_uploaded_model(m, scenario_df)

    # Fallback for everything else (e.g. URI-referenced) — fabricate a
    # plausible response from the macro vars. Marked clearly in the
    # summary so the analyst knows the numbers aren't real.
    return _synthesize_response(m, scenario_df)


def _synthesize_response(m, scenario_df: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rng = np.random.default_rng(hash((m.id, len(scenario_df))) & 0xFFFFFFFF)
    base_signal = scenario_df.select_dtypes(include="number").drop(columns=["month"], errors="ignore")
    if base_signal.shape[1] == 0:
        signal = np.linspace(0, 1, len(scenario_df))
    else:
        signal = base_signal.mean(axis=1).to_numpy()
        signal = (signal - signal.min()) / (signal.max() - signal.min() + 1e-9)
    noise = rng.normal(0, 0.05, len(signal))
    series = []
    for i, (_, row) in enumerate(scenario_df.iterrows()):
        entry = {
            "month": int(row.get("month", i + 1)),
            "prediction": float(round(0.5 + signal[i] * 0.3 + noise[i], 4)),
        }
        for v in scenario_df.columns:
            if v != "month" and v in row.index:
                try:
                    entry[v] = float(row[v])
                except (TypeError, ValueError):
                    pass
        series.append(entry)
    preds = [r["prediction"] for r in series]
    return series, {
        "min_prediction": round(min(preds), 4),
        "max_prediction": round(max(preds), 4),
        "mean_prediction": round(float(np.mean(preds)), 4),
        "horizon_steps": len(preds),
        "model_type": m.model_type,
        "note": "Synthesized response for non-in-app model",
    }


def _apply_uploaded_model(m, scenario_df: pd.DataFrame) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Run an uploaded artifact in the sandboxed runner and shape the
    output to the orchestrator's row-per-step format."""
    from pathlib import Path
    from services.model_runner import predict as sandbox_predict
    from routers.models_registry import resolve_artifact

    # Build the absolute artifact path. Pack-shipped models use
    # `attach_model` and store an absolute path; uploaded ones store a
    # path relative to ARTIFACT_ROOT — and for those, `resolve_artifact`
    # pulls the bytes from S3 if this node never received the upload.
    artifact_str = str(m.artifact_path)
    abs_path = Path(artifact_str)
    if not abs_path.is_absolute():
        abs_path = resolve_artifact(artifact_str)
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail=f"Model artifact not found at {abs_path}")

    # Resolve declared input feature names. Priority:
    #   1. feature_mapping keys (model's expected names) — the canonical list.
    #   2. feature_columns (legacy / regression-shaped models).
    #   3. (empty) — sandbox falls back to all numeric columns.
    feature_columns = list(m.feature_mapping.keys()) if m.feature_mapping else list(m.feature_columns or [])

    # Pass the dataset to the sandbox UNCHANGED. The sandbox runs the
    # pre-transform first (against the user's original CSV column names,
    # which is what their expression references), THEN applies
    # feature_mapping renames, THEN extracts feature_columns. Renaming
    # here would break the user's pre-transform expressions.
    df_records = scenario_df.to_dict(orient="records")

    result = sandbox_predict(
        artifact_path=str(abs_path),
        file_format=m.file_format or "pkl",
        df_records=df_records,
        feature_columns=feature_columns,
        feature_mapping=dict(m.feature_mapping or {}),
        pre_transform=m.pre_transform,
        output_kind=m.output_kind or "scalar",
    )

    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Sandboxed model prediction failed.",
                "error": result.get("error"),
                "stderr": result.get("stderr"),
                "traceback": result.get("traceback"),
            },
        )

    predictions = result["predictions"]
    return _post_process(predictions, scenario_df, m)


def _post_process(predictions, scenario_df: pd.DataFrame, m) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Shape the sandbox's raw predictions into the orchestrator's
    `[{month, prediction, ...input_cols}]` series format, branching on
    `output_kind`."""
    output_kind = m.output_kind or "scalar"
    series: list[dict[str, Any]] = []

    def _row_passthrough(i: int, row) -> dict[str, Any]:
        entry: dict[str, Any] = {"month": int(row.get("month", i + 1))}
        for v in scenario_df.columns:
            if v == "month":
                continue
            try:
                entry[v] = float(row[v])
            except (TypeError, ValueError):
                pass
        return entry

    if output_kind == "scalar":
        # predictions: 1-D list, one value per input row.
        flat = [float(p[0]) if isinstance(p, list) else float(p) for p in predictions]
        for i, (_, row) in enumerate(scenario_df.iterrows()):
            entry = _row_passthrough(i, row)
            entry["prediction"] = float(round(flat[i] if i < len(flat) else 0.0, 6))
            series.append(entry)
        summary = _scalar_summary(flat, m)

    elif output_kind == "probability_vector":
        # predictions: 2-D, shape (rows, classes).
        labels = m.class_labels or [f"class_{j}" for j in range(len(predictions[0]) if predictions else 0)]
        for i, (_, row) in enumerate(scenario_df.iterrows()):
            entry = _row_passthrough(i, row)
            probs = predictions[i] if i < len(predictions) else [0.0] * len(labels)
            for j, lbl in enumerate(labels):
                entry[f"p_{lbl}"] = float(round(float(probs[j]), 6)) if j < len(probs) else 0.0
            # `prediction` = argmax label, for downstream tiles that want a
            # single column.
            try:
                argmax_idx = max(range(len(probs)), key=lambda k: probs[k])
                entry["prediction"] = labels[argmax_idx]
            except ValueError:
                entry["prediction"] = None
            series.append(entry)
        summary = {
            "model_type": m.model_type,
            "output_kind": "probability_vector",
            "classes": labels,
            "horizon_steps": len(series),
        }

    elif output_kind == "n_step_forecast":
        # predictions: a 1-D list of length = forecast_steps. Single input
        # row produces multiple output rows.
        flat = [float(p[0]) if isinstance(p, list) else float(p) for p in predictions]
        steps = m.forecast_steps or len(flat)
        # Anchor month: the first row's `month` if present, else 1.
        try:
            anchor = int(scenario_df.iloc[0].get("month", 0)) if len(scenario_df) else 0
        except Exception:
            anchor = 0
        for j in range(steps):
            series.append({"month": anchor + j + 1, "step": j + 1,
                           "prediction": float(round(flat[j] if j < len(flat) else 0.0, 6))})
        summary = _scalar_summary(flat[:steps], m, extra={"output_kind": "n_step_forecast",
                                                          "forecast_steps": steps})

    elif output_kind == "multi_target":
        # predictions: 2-D, shape (rows, targets).
        targets = m.target_names or [f"target_{j}" for j in range(len(predictions[0]) if predictions else 0)]
        for i, (_, row) in enumerate(scenario_df.iterrows()):
            entry = _row_passthrough(i, row)
            vals = predictions[i] if i < len(predictions) else [0.0] * len(targets)
            for j, tname in enumerate(targets):
                entry[tname] = float(round(float(vals[j]), 6)) if j < len(vals) else 0.0
            # Convention: `prediction` = first target so existing tile UIs
            # that look for that key still work.
            entry["prediction"] = entry.get(targets[0]) if targets else None
            series.append(entry)
        summary = {
            "model_type": m.model_type,
            "output_kind": "multi_target",
            "targets": targets,
            "horizon_steps": len(series),
        }

    else:
        raise HTTPException(status_code=400, detail=f"Unknown output_kind '{output_kind}'")

    return series, summary


def _scalar_summary(preds, m, extra: dict | None = None) -> dict[str, Any]:
    if not preds:
        out = {"model_type": m.model_type, "horizon_steps": 0, "note": "Empty prediction set"}
    else:
        out = {
            "min_prediction": round(min(preds), 4),
            "max_prediction": round(max(preds), 4),
            "mean_prediction": round(float(np.mean(preds)), 4),
            "horizon_steps": len(preds),
            "model_type": m.model_type,
        }
    if extra:
        out.update(extra)
    return out


# ── Scenario routes ─────────────────────────────────────────────────────────
@router.get("/scenarios", response_model=list[Scenario])
async def list_scenarios(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = all_scenarios()
    # Built-ins (function_id None) are always returned; plus function-scoped ones
    items = [
        s for s in items
        if s.function_id is None or s.function_id == function_id or function_id is None
    ]
    items.sort(key=lambda s: (s.source_kind != "builtin", s.created_at), reverse=False)
    return items


@router.get("/scenarios/{scenario_id}", response_model=Scenario)
async def get_scenario(scenario_id: str, _: str = Depends(get_current_user)):
    s = load_scenario(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    return s


@router.get("/scenarios/{scenario_id}/preview")
async def preview_scenario(
    scenario_id: str,
    _: str = Depends(get_current_user),
):
    s = load_scenario(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    df = _scenario_dataframe(scenario_id)
    # Convert datetimes to strings for JSON safety
    out = df.head(50).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].astype(str)
    return {
        "scenario_id": scenario_id,
        "columns": list(out.columns),
        "rows": out.to_dict(orient="records"),
    }


@router.post("/scenarios/from-dataset", response_model=Scenario, status_code=201)
async def scenario_from_dataset(req: ScenarioCreateFromDataset, _: str = Depends(get_current_user)):
    d = load_dataset(req.dataset_id)
    if not d:
        raise HTTPException(status_code=404, detail="Dataset not found")
    sid = f"scn-{uuid.uuid4().hex[:10]}"
    now = datetime.utcnow().isoformat() + "Z"
    sc = Scenario(
        id=sid,
        function_id=req.function_id,
        name=req.name,
        description=req.description,
        severity=req.severity,
        source_kind=d.source_kind,  # 'upload' or 'sql_table'
        dataset_id=req.dataset_id,
        variables=[c.name for c in d.columns if c.dtype.startswith(("float", "int"))],
        horizon_months=d.row_count,
        created_at=now,
    )
    # The record this endpoint mints is the analyst's own — derived from their
    # dataset and from nothing a restart can recompute. It goes in the table.
    store_scenario(sc)
    return sc


@router.delete("/scenarios/{scenario_id}", status_code=204)
async def delete_scenario(scenario_id: str, _: str = Depends(get_current_user)):
    s = load_scenario(scenario_id)
    if not s:
        raise HTTPException(status_code=404, detail="Scenario not found")
    # Still a 403 rather than a 404, and still checked on `source_kind` rather
    # than on which store answered: the response the frontend gets should not
    # change because of where the record happens to live.
    if s.source_kind == "builtin":
        raise HTTPException(status_code=403, detail="Built-in scenarios cannot be deleted")
    remove_scenario(scenario_id)


# ── Run routes ─────────────────────────────────────────────────────────────
@router.get("/runs", response_model=list[AnalyticsRun])
async def list_runs(
    response: Response,
    function_id: str | None = Query(default=None),
    limit: int = Query(default=RUNS_PAGE_DEFAULT, ge=1, le=RUNS_PAGE_MAX),
    cursor: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    """Newest runs first.

    With a `function_id` this reads the (function, time) index and stops after
    `limit` items, so the cost does not grow with the number of runs on record.
    Without one there is no index partition to query and it falls back to
    reading the whole run partition — the old behaviour, still capped.

    The body stays a plain array so existing callers are unaffected; the
    continuation token comes back in the `X-Next-Cursor` header, and its
    absence means this was the last page.
    """
    from services import entity_store

    if function_id:
        try:
            records, next_cursor = entity_store.query_index(
                _run_index_pk(function_id), limit=limit, cursor=cursor,
            )
        except entity_store.IndexUnavailable:
            # Expected between deploying this code and running `cdk deploy`.
            log.warning("run index not deployed yet — falling back to a full read")
            records, next_cursor = None, None
        if records is not None:
            if next_cursor:
                response.headers["X-Next-Cursor"] = next_cursor
            return _runs_from(records)

    items = all_runs()
    if function_id:
        items = [r for r in items if r.function_id == function_id]
    items.sort(key=lambda r: r.created_at, reverse=True)
    return items[:limit]


def _runs_from(records: list[dict]) -> list[AnalyticsRun]:
    out: list[AnalyticsRun] = []
    for record in records:
        try:
            out.append(AnalyticsRun(**record))
        except Exception as e:
            log.warning("skipping unreadable run %s: %s", record.get("id"), e)
    return out


@router.get("/runs/{run_id}", response_model=AnalyticsRun)
async def get_run(run_id: str, _: str = Depends(get_current_user)):
    r = load_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return r


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(run_id: str, _: str = Depends(get_current_user)):
    if not load_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    remove_run(run_id)


def _input_dataframe(scenario_id: str | None, dataset_id: str | None, horizon: int) -> tuple[pd.DataFrame, str, str]:
    """Resolve the input frame for a run. Returns (df, input_kind, input_label)."""
    if scenario_id and dataset_id:
        raise HTTPException(status_code=400, detail="Pick either a scenario or a dataset, not both")
    if scenario_id:
        df = _scenario_dataframe(scenario_id)
        if "month" in df.columns:
            df = df[df["month"] <= horizon]
        else:
            df = df.head(horizon)
        s = load_scenario(scenario_id)
        return df, "scenario", (s.name if s else scenario_id)
    if dataset_id:
        d = load_dataset(dataset_id)
        if not d:
            raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")
        if d.source_kind == "upload" and d.file_path and d.file_format:
            try:
                df = _read_dataframe(_resolve_path(d), d.file_format)
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Could not read dataset: {e}")
        else:
            # sql_table — synthesize from declared columns
            n = max(horizon, 12)
            rng = np.random.default_rng(hash(dataset_id) & 0xFFFFFFFF)
            cols: dict[str, list[float]] = {}
            for c in d.columns:
                if c.dtype.startswith(("float", "int")):
                    cols[c.name] = list(np.linspace(0, 5, n) + rng.normal(0, 0.5, n))
            if not cols:
                cols["value"] = list(np.linspace(0, 1, n))
            df = pd.DataFrame(cols)
        df = df.head(horizon)
        if "month" not in df.columns:
            df = df.assign(month=list(range(1, len(df) + 1)))
        return df, "dataset", d.name
    raise HTTPException(status_code=400, detail="Either scenario_id or dataset_id is required")


@router.post("/runs", response_model=AnalyticsRun, status_code=201)
async def create_run(req: RunRequest, _: str = Depends(get_current_user)):
    started = datetime.utcnow()
    rid = f"run-{uuid.uuid4().hex[:10]}"
    try:
        df, input_kind, input_label = _input_dataframe(req.scenario_id, req.dataset_id, req.horizon_months)
        series, summary = _apply_model(req.model_id, df)
        m = load_model(req.model_id)
        if not m:
            raise HTTPException(status_code=404, detail=f"Model {req.model_id} not found")
        m.last_run = started.isoformat() + "Z"
        store_model(m)
        run = AnalyticsRun(
            id=rid,
            function_id=req.function_id,
            name=req.name or f"{m.name} × {input_label}",
            model_id=req.model_id,
            scenario_id=req.scenario_id,
            dataset_id=req.dataset_id,
            input_kind=input_kind,
            horizon_months=req.horizon_months,
            status="completed",
            summary=summary,
            series=series,
            notes=req.notes,
            created_at=started.isoformat() + "Z",
            duration_ms=(datetime.utcnow() - started).total_seconds() * 1000,
        )
    except HTTPException:
        raise
    except Exception as e:
        run = AnalyticsRun(
            id=rid,
            function_id=req.function_id,
            name=req.name or "Run",
            model_id=req.model_id,
            scenario_id=req.scenario_id,
            dataset_id=req.dataset_id,
            input_kind="scenario" if req.scenario_id else "dataset",
            horizon_months=req.horizon_months,
            status="failed",
            summary={},
            series=[],
            notes=req.notes,
            error=str(e),
            created_at=started.isoformat() + "Z",
            duration_ms=(datetime.utcnow() - started).total_seconds() * 1000,
        )
    store_run(run)
    return run


# ── Workflow runs ──────────────────────────────────────────────────────
def _topological_sort(nodes: list[WorkflowNode], edges: list[WorkflowEdge]) -> list[WorkflowNode]:
    """Kahn's algorithm. Raises HTTPException on cycles or missing nodes."""
    by_id = {n.id: n for n in nodes}
    for e in edges:
        if e.source not in by_id or e.target not in by_id:
            raise HTTPException(status_code=400, detail=f"Edge {e.source}->{e.target} references missing node")

    in_degree: dict[str, int] = {n.id: 0 for n in nodes}
    children: dict[str, list[str]] = {n.id: [] for n in nodes}
    for e in edges:
        in_degree[e.target] += 1
        children[e.source].append(e.target)

    queue = [n for n in nodes if in_degree[n.id] == 0]
    ordered: list[WorkflowNode] = []
    while queue:
        n = queue.pop(0)
        ordered.append(n)
        for c_id in children[n.id]:
            in_degree[c_id] -= 1
            if in_degree[c_id] == 0:
                queue.append(by_id[c_id])
    if len(ordered) != len(nodes):
        raise HTTPException(status_code=400, detail="Workflow contains a cycle")
    return ordered


def _resolve_input_frame(
    node: WorkflowNode,
    upstream_outputs: dict[str, list[dict[str, Any]]],
    horizon: int,
    *,
    by_id: dict[str, WorkflowNode] | None = None,
    incoming: dict[str, list[str]] | None = None,
    scenario_name: str | None = None,
    start_date: str | None = None,
) -> pd.DataFrame:
    """Turn one upstream node into a DataFrame the consuming model can read.

    `scenario_name` and `start_date` come from the Workflow tab's run
    controls. They flow through to Transform nodes as a query context —
    e.g. when the user picks `Data Harness` and the run is tagged with
    `BHCS 2026 / 2026-04-01`, the harness output is shaped to that
    scenario's macro path and re-anchored to the chosen start month.

    `by_id` + `incoming` are passed so transform nodes with upstream
    edges can recurse on their inputs (e.g. `Data Harness → DQC →
    Models` — DQC reads the harness's output, runs quality checks,
    passes through).
    """
    if node.kind == "dataset":
        df, _, _ = _input_dataframe(scenario_id=None, dataset_id=node.ref_id, horizon=horizon)
        return df
    if node.kind == "scenario":
        df, _, _ = _input_dataframe(scenario_id=node.ref_id, dataset_id=None, horizon=horizon)
        return df
    if node.kind == "model":
        # Use the model node's run output as a frame (already shaped with 'month'+driver cols)
        rows = upstream_outputs.get(node.id, [])
        if not rows:
            raise HTTPException(status_code=400, detail=f"Model node {node.id} has no upstream output yet")
        return pd.DataFrame(rows)
    if node.kind == "transform":
        from routers.transforms import _TRANSFORMS
        t = _TRANSFORMS.get(node.ref_id)
        if not t:
            raise HTTPException(
                status_code=404,
                detail=f"Transform '{node.ref_id}' is not registered.",
            )

        sources = (incoming or {}).get(node.id, [])
        if sources and by_id:
            # The transform has upstream nodes wired in — take their
            # merged frames as the input. Used by DQC ("validate the
            # harness output before models consume it") and any future
            # filter/join transforms.
            upstream_frames = [
                _resolve_input_frame(
                    by_id[s], upstream_outputs, horizon,
                    by_id=by_id, incoming=incoming,
                    scenario_name=scenario_name, start_date=start_date,
                )
                for s in sources
            ]
            input_df = _merge_frames(upstream_frames)
        else:
            # Source transform — read from its declared output dataset,
            # honoring any per-node "Read data from" override the user
            # configured on the canvas inspector. Apply the run-context
            # overlay (scenario + start_date) so the harness behaves as
            # if it had queried with those parameters.
            override = _read_selected_datasets(
                (node.config or {}).get("read_dataset_ids") or [], horizon,
            )
            if override is not None:
                input_df = override
            else:
                if not t.output_dataset_id:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Transform '{t.name}' has no output dataset configured "
                            "and no upstream input wired. Connect a dataset to its "
                            "left handle, or set `output_dataset_id` on the pack."
                        ),
                    )
                input_df, _, _ = _input_dataframe(
                    scenario_id=None, dataset_id=t.output_dataset_id, horizon=horizon,
                )
            input_df = _apply_run_context(
                input_df, scenario_name=scenario_name, start_date=start_date,
            )

        # Run the transform's recipe — DQC executes its checks; other
        # transforms pass through (their materialization is the read above).
        params = {
            p.name: p.default for p in (t.parameters or [])
            if p.default is not None
        }
        params.update((node.config or {}).get("params") or {})
        result_df = _execute_transform(t, input_df, params)

        # Validate model feature requirements per the inspector's
        # "Match requirements of" multi-select. Raises a structured
        # FEATURE_MISMATCH error if the materialized columns can't
        # satisfy the selected models.
        return _apply_node_config(node, result_df)
    raise HTTPException(status_code=400, detail=f"Unknown node kind {node.kind}")


def _read_selected_datasets(
    dataset_ids: list[str],
    horizon: int,
) -> pd.DataFrame | None:
    """Materialize the merged frame for a list of user-selected datasets.

    Used when the analyst overrides a Data Harness's source from the
    inspector's "Read data from" multi-select. Returns None when the
    selection is empty — the caller falls back to the static
    `output_dataset_id`.
    """
    if not dataset_ids:
        return None
    frames: list[pd.DataFrame] = []
    for ds_id in dataset_ids:
        try:
            df, _, _ = _input_dataframe(
                scenario_id=None, dataset_id=ds_id, horizon=horizon,
            )
            frames.append(df)
        except HTTPException:
            # Dataset went away between selection and run — skip rather
            # than hard-fail; the requirement check below catches the
            # missing-feature case if the survivors aren't enough.
            continue
    if not frames:
        return None
    return _merge_frames(frames)


def _apply_node_config(node: WorkflowNode, df: pd.DataFrame) -> pd.DataFrame:
    """Honor the per-node `requirement_model_ids` config — fail the run
    if the transform's output doesn't carry every feature the selected
    models need. Surfaces a structured FEATURE_MISMATCH the run-error
    card already knows how to render.
    """
    cfg = node.config or {}
    required_models = cfg.get("requirement_model_ids") or []
    if not required_models:
        return df

    cols_lower = {str(c).lower() for c in df.columns}
    missing_per_model: dict[str, list[str]] = {}
    for mid in required_models:
        m = load_model(mid)
        if not m:
            continue
        features = list(m.feature_columns or []) or list((m.feature_mapping or {}).keys())
        missing = [f for f in features if f and f.lower() not in cols_lower]
        if missing:
            missing_per_model[m.name] = missing
    if missing_per_model:
        first_name = next(iter(missing_per_model))
        first_missing = missing_per_model[first_name]
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Transform output missing features required by selected models.",
                "error": (
                    f"Feature(s) not found in dataset: {first_missing} "
                    f"(required by `{first_name}`)"
                    + (f"; +{len(missing_per_model)-1} other model(s)" if len(missing_per_model) > 1 else "")
                ),
                "hint": (
                    "Either pick a different dataset on the harness card, or "
                    "remove these models from the harness's 'Match requirements "
                    "of' selection."
                ),
            },
        )
    return df


def _execute_transform(
    transform, df: pd.DataFrame, params: dict[str, Any],
) -> pd.DataFrame:
    """Dispatch a transform's runtime by identity.

    DQC runs row-count + null-rate (and any other declared) checks
    against the input frame. Other transforms (Data Harness etc.)
    materialize their output via `output_dataset_id` upstream — by the
    time we get here, the materialization already happened, so the
    default is passthrough.
    """
    is_dqc = (
        "dqc" in (transform.id or "").lower()
        or "DataQualityCheck" in (transform.recipe_python or "")
    )
    if is_dqc:
        return _run_dqc(df, params)
    return df


def _run_dqc(df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    """Run Data Quality Check on the input frame.

    Production path: the corporate `pa_common_tools.DataQualityCheck`
    class. Outside the proxy environment (or if the class signature
    doesn't match), a built-in stub runs the same row-count and
    null-rate checks declared on the transform's parameters. Either
    path is a passthrough on success and a structured run-error on
    failure (so the Workflow tab's run-error card renders the
    `code: DQC_FAILED` message + a hint).
    """
    min_rows = int((params or {}).get("min_rows", 12))
    max_null_rate = float((params or {}).get("max_null_rate", 0.05))

    # ── Production path: the corporate library ────────────────────────
    try:
        import importlib
        importlib.invalidate_caches()
        mod = importlib.import_module("pa_common_tools")
        cls = getattr(mod, "DataQualityCheck", None)
        if cls is not None:
            checker = cls(min_rows=min_rows, max_null_rate=max_null_rate)
            run = getattr(checker, "run", None)
            if callable(run):
                return run(df)
            if callable(checker):
                return checker(df)
    except ImportError:
        pass  # pa_common_tools not installed — use the stub
    except Exception as e:
        # The library was found but raised — surface that to the user
        # rather than silently falling back. Keep `DQC_FAILED` so the
        # frontend run-error card recognizes it.
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Data Quality Check failed.",
                "error": f"pa_common_tools.DataQualityCheck raised: {e}",
                "hint": (
                    "Inspect the failure inside the corporate library, or "
                    "tighten the harness's parameters to keep the data "
                    "inside acceptable bands."
                ),
            },
        )

    # ── Built-in stub (matches the recipe text on the inspector) ──────
    if len(df) < min_rows:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Data Quality Check failed: too few rows.",
                "error": f"DQC: only {len(df)} row(s); need at least {min_rows}.",
                "hint": (
                    f"Lower the `min_rows` threshold on the DQC card "
                    f"(currently {min_rows}), or extend the harness horizon "
                    "so it materializes more rows."
                ),
            },
        )

    null_rates = df.isna().mean()
    worst_null = float(null_rates.max()) if len(null_rates) else 0.0
    if worst_null > max_null_rate:
        worst_col = str(null_rates.idxmax())
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Data Quality Check failed: column null rate too high.",
                "error": (
                    f"DQC: column `{worst_col}` is {worst_null:.1%} null "
                    f"(max allowed {max_null_rate:.1%})."
                ),
                "hint": (
                    "Filter null rows out upstream, or relax `max_null_rate` "
                    f"on the DQC card (currently {max_null_rate:.1%})."
                ),
            },
        )

    return df


def _apply_run_context(
    df: pd.DataFrame,
    *,
    scenario_name: str | None,
    start_date: str | None,
) -> pd.DataFrame:
    """Overlay the selected scenario's macro path + re-anchor the
    as_of_date column on a transform's materialized output.

    In a real corporate deploy the Data Harness would use these to
    query OneLake with the right scenario filter and start month. For
    the demo we approximate by overwriting the rate / unemployment / GDP
    / HPI columns from the matched scenario's synthetic path, leaving
    every other column (deposit-pricing, accounts, segment-specific)
    intact. Models downstream see different rate paths per scenario.
    """
    if not scenario_name and not start_date:
        return df

    out = df.copy()

    # ── Overlay macro paths from the matched scenario ──────────────────
    if scenario_name:
        # Built-ins only. The overlay reads `BUILTIN_DATA`, so a match on
        # anything else was always a no-op — and searching the whole registry
        # meant an analyst-created scenario that happened to share a name could
        # be found first and silently suppress the overlay. Narrowing the
        # search fixes that and keeps a per-node transform off the network.
        match = next(
            (s for s in _BUILTIN_SCENARIOS.values() if s.name == scenario_name),
            None,
        )
        if match and match.id in BUILTIN_DATA:
            paths = BUILTIN_DATA[match.id].get("paths") or {}
            n_rows = len(out)
            for col, values in paths.items():
                if col in out.columns and len(values) >= n_rows:
                    out[col] = list(values[:n_rows])

    # ── Re-anchor as_of_date to the picked start_date ──────────────────
    if start_date and "as_of_date" in out.columns:
        try:
            anchor = pd.Timestamp(start_date).to_period("M").to_timestamp()
            new_dates = pd.date_range(anchor, periods=len(out), freq="MS")
            out["as_of_date"] = [d.strftime("%Y-%m-%d") for d in new_dates]
        except Exception:
            # Bad date string — leave the column alone rather than crashing the run.
            pass

    return out


def _merge_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Outer-join multiple DataFrames on 'month' (or row index if absent)."""
    if not frames:
        raise HTTPException(status_code=400, detail="Model node has no incoming inputs")
    if len(frames) == 1:
        return frames[0]
    # Ensure each frame has a 'month' key for the join
    norm: list[pd.DataFrame] = []
    for df in frames:
        if "month" not in df.columns:
            df = df.assign(month=list(range(1, len(df) + 1)))
        norm.append(df)
    base = norm[0]
    for other in norm[1:]:
        # Drop columns from the other frame that already exist in base (except 'month')
        dup_cols = [c for c in other.columns if c in base.columns and c != "month"]
        other_clean = other.drop(columns=dup_cols)
        base = base.merge(other_clean, on="month", how="outer")
    base = base.sort_values("month").reset_index(drop=True)
    base = base.ffill().bfill()
    return base


def _write_destination(
    dest_node: WorkflowNode,
    upstream_run: AnalyticsRun,
) -> DestinationWrite:
    """'Write' a model run's series to a destination.

    Snowflake / OneLake / S3 writes are mocked for the demo (we just record the
    target and row count). CSV destinations carry the series back so the
    browser can trigger a file download.
    """
    kind = dest_node.ref_id
    cfg = dest_node.config or {}
    rows = upstream_run.series
    n = len(rows)

    if kind == "snowflake_table":
        target = cfg.get("table") or cfg.get("ref") or "CMA.PUBLIC.OUTPUT"
        return DestinationWrite(
            node_id=dest_node.id, kind="snowflake_table", target=target,
            upstream_model_id=upstream_run.model_id, upstream_run_id=upstream_run.id,
            rows_written=n, status="written",
            note=f"Mock write to Snowflake — {cfg.get('mode', 'append')} mode",
        )
    if kind == "onelake_table":
        target = cfg.get("table") or cfg.get("ref") or "Finance.cma.output"
        return DestinationWrite(
            node_id=dest_node.id, kind="onelake_table", target=target,
            upstream_model_id=upstream_run.model_id, upstream_run_id=upstream_run.id,
            rows_written=n, status="written",
            note="Mock write to OneLake lakehouse table",
        )
    if kind == "s3":
        bucket = cfg.get("bucket") or "cma-outputs"
        key = cfg.get("key") or f"runs/{upstream_run.id}.parquet"
        target = f"s3://{bucket}/{key}"
        return DestinationWrite(
            node_id=dest_node.id, kind="s3", target=target,
            upstream_model_id=upstream_run.model_id, upstream_run_id=upstream_run.id,
            rows_written=n, status="written",
            note=f"Mock write to S3 — {cfg.get('format', 'parquet')} format",
        )
    if kind == "csv":
        filename = cfg.get("filename") or f"{upstream_run.id}.csv"
        return DestinationWrite(
            node_id=dest_node.id, kind="csv", target=filename,
            upstream_model_id=upstream_run.model_id, upstream_run_id=upstream_run.id,
            rows_written=n, status="written",
            csv_filename=filename, csv_data=rows,
            note="CSV bytes returned to browser for download",
        )
    return DestinationWrite(
        node_id=dest_node.id, kind=kind,  # type: ignore[arg-type]
        target=str(cfg), upstream_model_id=upstream_run.model_id,
        upstream_run_id=upstream_run.id, rows_written=0, status="failed",
        note=f"Unknown destination kind '{kind}'",
    )


def _write_csv_combined(
    dest_node: WorkflowNode,
    upstream_runs: list[AnalyticsRun],
) -> DestinationWrite:
    """Coalesce N model runs into ONE long-format CSV with a `segment` column.

    Used when a CSV destination has multiple incoming model edges — the
    classic "fan-in" shape. Each upstream model contributes its rows
    prepended with `segment=<model name>` so the analyst gets one tidy
    table for downstream BI."""
    cfg = dest_node.config or {}
    filename = cfg.get("filename") or f"{dest_node.id}-combined.csv"
    combined: list[dict[str, Any]] = []
    for run in upstream_runs:
        m = load_model(run.model_id)
        seg = m.name if m else run.model_id
        for row in run.series:
            combined.append({"segment": seg, **row})
    return DestinationWrite(
        node_id=dest_node.id, kind="csv", target=filename,
        # `upstream_model_id` / `upstream_run_id` are scalar fields on the
        # schema — pin to the first run; the merged note records the rest.
        upstream_model_id=upstream_runs[0].model_id,
        upstream_run_id=upstream_runs[0].id,
        rows_written=len(combined), status="written",
        csv_filename=filename, csv_data=combined,
        note=f"Combined CSV across {len(upstream_runs)} upstream models (segment column added)",
    )


# ── Run-time error classification ──────────────────────────────────────────
# Maps a raw failure message from the sandbox / model loader to a user-facing
# {code, what_happened, how_to_fix} triple. The Run-failed card in the UI
# renders these fields directly. New patterns slot in by adding another
# (substring, code, hint) row.
_RUN_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (
        "DQC:",
        "DQC_FAILED",
        "Inspect the failing column on the harness output, or relax the DQC card's "
        "thresholds (min_rows / max_null_rate).",
    ),
    (
        "Feature(s) not found in dataset",
        "FEATURE_MISMATCH",
        "Rename the dataset's columns to match the model's expected feature names, "
        "or pick a different dataset. The match is case-insensitive.",
    ),
    (
        "timed out after",
        "TIMEOUT",
        "The prediction subprocess hit its wall-clock limit. Try a smaller horizon "
        "or upload a faster model.",
    ),
    (
        "Model artifact not found",
        "ARTIFACT_MISSING",
        "The model's file is missing on disk. Re-upload it from the Models tab.",
    ),
    (
        "pre_transform failed",
        "PRE_TRANSFORM",
        "The model's pre-transform expression raised. Open the model in the Models "
        "tab and fix the expression — it runs in the sandbox before feature extraction.",
    ),
    (
        "Unsupported model format",
        "UNSUPPORTED_FORMAT",
        "Only .pkl, .joblib, and .onnx are supported. Re-export the model in one of those formats.",
    ),
    (
        "exited without producing output",
        "SUBPROCESS_DIED",
        "The prediction subprocess crashed (segfault, OOM, or import-time error). "
        "Check that the .pkl was exported from the same Python/sklearn version as the runtime.",
    ),
    (
        "Can't get attribute",
        "PICKLE_CLASS_MISSING",
        "The pickle references a class that wasn't found at load time. If the model uses a "
        "custom class, ship its .py module alongside the artifact (a sibling `_classname.py`).",
    ),
    (
        "No module named",
        "PICKLE_MODULE_MISSING",
        "A module the pickle imports isn't installed in the backend. Either bake it into "
        "the artifact or add the dependency.",
    ),
    (
        "Could not parse model output",
        "OUTPUT_PARSE",
        "The sandbox produced output the orchestrator can't read. Check the model returns "
        "a list/array of numbers, not a custom object.",
    ),
    (
        "is expecting",  # sklearn: "X has N features, but ... is expecting M features"
        "OUTPUT_SHAPE",
        "The model received a different number of features than it was trained on. "
        "Check the dataset has exactly the columns the model expects.",
    ),
]


def _classify_run_error(raw: Any) -> dict[str, Any]:
    """Turn the orchestrator's raw failure (str / dict / Exception) into a
    structured error_detail the UI can render directly."""
    if isinstance(raw, dict):
        msg = (
            raw.get("error")
            or raw.get("message")
            or raw.get("detail")
            or str(raw)
        )
    else:
        msg = str(raw) if raw is not None else "Unknown error"
    msg_str = str(msg)
    for needle, code, hint in _RUN_ERROR_PATTERNS:
        if needle in msg_str:
            return {
                "code": code,
                "what_happened": msg_str,
                "how_to_fix": hint,
            }
    return {
        "code": "GENERIC",
        "what_happened": msg_str,
        "how_to_fix": (
            "Inspect the failed step's run details below, or ask the troubleshooter "
            "agent for help."
        ),
    }


# ── Saved workflows (named graphs the analyst saves + restores) ───────────
def _summarize_saved(w: SavedWorkflow) -> SavedWorkflowSummary:
    return SavedWorkflowSummary(
        id=w.id,
        function_id=w.function_id,
        name=w.name,
        description=w.description,
        node_count=len(w.nodes),
        edge_count=len(w.edges),
        created_at=w.created_at,
        updated_at=w.updated_at,
    )


# ── workflow persistence ──────────────────────────────────────────────────
# Phase 5: saved workflows live in DynamoDB, not in `_SAVED_WORKFLOWS`. Without
# CMA_STATE_TABLE the store falls back to an in-process dict, so local
# development is unchanged — but with it, a restart no longer loses the
# analyst's work and a second replica sees the same data.
_WORKFLOW_ENTITY = "workflow"


def _load_workflow(workflow_id: str) -> SavedWorkflow | None:
    from services import entity_store

    record = entity_store.get(_WORKFLOW_ENTITY, workflow_id)
    return SavedWorkflow(**record) if record else None


def _store_workflow(workflow: SavedWorkflow) -> None:
    from services import entity_store

    entity_store.put(_WORKFLOW_ENTITY, workflow.id, workflow.model_dump())


def _all_workflows() -> list[SavedWorkflow]:
    from services import entity_store

    out: list[SavedWorkflow] = []
    for record in entity_store.list_all(_WORKFLOW_ENTITY):
        try:
            out.append(SavedWorkflow(**record))
        except Exception as e:
            # A stored item that no longer matches the schema should not break
            # the whole list — surface it and keep going.
            log.warning("skipping unreadable workflow %s: %s",
                        record.get("id"), e)
    return out


@router.get("/workflows", response_model=list[SavedWorkflowSummary])
async def list_saved_workflows(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = _all_workflows()
    if function_id:
        items = [w for w in items if w.function_id == function_id]
    items.sort(key=lambda w: w.updated_at or w.created_at, reverse=True)
    return [_summarize_saved(w) for w in items]


@router.get("/workflows/{workflow_id}", response_model=SavedWorkflow)
async def get_saved_workflow(workflow_id: str, _: str = Depends(get_current_user)):
    w = _load_workflow(workflow_id)
    if not w:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return w


# ── canvas -> state machine ───────────────────────────────────────────────
def _register_plan(workflow: SavedWorkflow) -> None:
    """Compile the canvas and record the plan, so the analyst finds out now.

    Deliberately does NOT deploy. This endpoint runs in the web tier, which
    holds no permission to create AWS resources; `infra/deploy_workflows.py`
    ships pending plans under a separate, privileged identity. See
    services/workflow_plans.py.

    A broken graph is the analyst's problem to fix while the diagram is still
    on screen, so a compile error is a 400. Anything else — no bucket
    configured, S3 unavailable — must not block saving their work.
    """
    from services.workflow_compiler import WorkflowCompileError
    from services.workflow_plans import compile_and_register

    try:
        compile_and_register(workflow.model_dump())
    except WorkflowCompileError as e:
        raise HTTPException(status_code=400, detail=f"Workflow cannot run: {e}")
    except Exception as e:
        log.warning("plan registration skipped for %s: %s", workflow.id, e)


@router.post("/workflows", response_model=SavedWorkflow, status_code=201)
async def save_workflow(req: SavedWorkflowCreate, _: str = Depends(get_current_user)):
    wid = f"sw-{uuid.uuid4().hex[:10]}"
    now = datetime.utcnow().isoformat() + "Z"
    w = SavedWorkflow(
        id=wid,
        function_id=req.function_id,
        name=req.name,
        description=req.description,
        nodes=req.nodes,
        edges=req.edges,
        horizon_months=req.horizon_months,
        scenario_name=req.scenario_name,
        start_date=req.start_date,
        view=req.view,
        require_approval=req.require_approval,
        created_at=now,
    )
    _register_plan(w)
    _store_workflow(w)
    return w


@router.put("/workflows/{workflow_id}", response_model=SavedWorkflow)
async def update_saved_workflow(
    workflow_id: str,
    req: SavedWorkflowCreate,
    _: str = Depends(get_current_user),
):
    existing = _load_workflow(workflow_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Workflow not found")
    now = datetime.utcnow().isoformat() + "Z"
    updated = SavedWorkflow(
        id=workflow_id,
        function_id=req.function_id,
        name=req.name,
        description=req.description,
        nodes=req.nodes,
        edges=req.edges,
        horizon_months=req.horizon_months,
        scenario_name=req.scenario_name,
        start_date=req.start_date,
        view=req.view,
        require_approval=req.require_approval,
        created_at=existing.created_at,
        updated_at=now,
    )
    _register_plan(updated)
    _store_workflow(updated)
    return updated


@router.get("/workflows/{workflow_id}/plans")
async def list_workflow_plans(workflow_id: str, _: str = Depends(get_current_user)):
    """Every compiled version of this workflow's executable plan.

    The audit answer to "which calculation produced the number we filed?" —
    each version is kept, never overwritten, with the hash and timestamp of
    the plan that actually ran.
    """
    from services.workflow_plans import get_index

    try:
        index = get_index(workflow_id)
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Plan registry unavailable: {e}")
    if not index:
        raise HTTPException(status_code=404, detail="No compiled plans for this workflow")
    return index


@router.delete("/workflows/{workflow_id}", status_code=204)
async def delete_saved_workflow(workflow_id: str, _: str = Depends(get_current_user)):
    # Ask the deployer to remove the machine; the plan history stays, because
    # deleting a workflow should not erase the record of what it used to do.
    try:
        from services.workflow_plans import request_delete

        request_delete(workflow_id)
    except Exception as e:
        log.warning("could not request machine deletion for %s: %s", workflow_id, e)
    from services import entity_store

    entity_store.delete(_WORKFLOW_ENTITY, workflow_id)
    return None


@router.post("/workflow-runs", response_model=WorkflowResult, status_code=201)
async def create_workflow_run(req: WorkflowRequest, _: str = Depends(get_current_user)):
    started = datetime.utcnow()
    workflow_id = f"wf-{uuid.uuid4().hex[:10]}"

    model_nodes = [n for n in req.nodes if n.kind == "model"]
    if not model_nodes:
        raise HTTPException(status_code=400, detail="Workflow must contain at least one model node")

    try:
        ordered = _topological_sort(req.nodes, req.edges)
    except HTTPException:
        raise

    by_id = {n.id: n for n in req.nodes}
    incoming: dict[str, list[str]] = {n.id: [] for n in req.nodes}
    outgoing: dict[str, list[str]] = {n.id: [] for n in req.nodes}
    for e in req.edges:
        incoming[e.target].append(e.source)
        outgoing[e.source].append(e.target)

    outputs: dict[str, list[dict[str, Any]]] = {}
    model_runs_by_node: dict[str, AnalyticsRun] = {}
    runs: list[AnalyticsRun] = []
    destinations: list[DestinationWrite] = []
    node_status: dict[str, str] = {n.id: "idle" for n in req.nodes}
    step_idx = 0

    for node in ordered:
        if node.kind in ("dataset", "scenario", "transform"):
            # Inputs and ETL transforms are passive in v1 — the orchestrator
            # resolves them on demand inside `_resolve_input_frame`. Mark
            # them green so the canvas reflects the run shape.
            node_status[node.id] = "completed"
            continue

        if node.kind == "destination":
            # Find the upstream model(s) feeding this destination.
            sources = incoming.get(node.id, [])
            if not sources:
                node_status[node.id] = "skipped"
                continue
            completed_runs = [
                model_runs_by_node[s]
                for s in sources
                if model_runs_by_node.get(s) and model_runs_by_node[s].status == "completed"
            ]
            if not completed_runs:
                node_status[node.id] = "skipped"
                continue
            # CSV with multiple inputs → fan-in into one long-format file
            # with a `segment` column. Other kinds (Snowflake/OneLake/S3)
            # keep their per-source semantics.
            if node.ref_id == "csv" and len(completed_runs) > 1:
                dw = _write_csv_combined(node, completed_runs)
                destinations.append(dw)
                node_status[node.id] = "completed" if dw.status == "written" else "skipped"
            else:
                wrote_any = False
                for src_run in completed_runs:
                    dw = _write_destination(node, src_run)
                    destinations.append(dw)
                    wrote_any = wrote_any or dw.status == "written"
                node_status[node.id] = "completed" if wrote_any else "skipped"
            continue

        # node.kind == 'model'
        sources = incoming.get(node.id, [])
        if not sources:
            raise HTTPException(
                status_code=400,
                detail=f"Model node '{node.id}' has no input — connect a dataset, scenario, or upstream model.",
            )

        m = load_model(node.ref_id)
        if not m:
            raise HTTPException(status_code=404, detail=f"Model {node.ref_id} not found")

        run_started = datetime.utcnow()
        failed_detail: dict[str, Any] | None = None
        run_status_lit: str
        run_error: str | None
        series: list[dict[str, Any]]
        summary: dict[str, Any]

        # Wrap input resolution AND the model call in the same try/except
        # so failures upstream (DQC tripping, transform config rejecting
        # the data, missing dataset) get classified into a structured
        # error_detail on the WorkflowResult, rather than escaping as a
        # raw HTTP error from the endpoint.
        try:
            frames = [
                _resolve_input_frame(
                    by_id[s], outputs, req.horizon_months,
                    by_id=by_id,
                    incoming=incoming,
                    scenario_name=req.scenario_name,
                    start_date=req.start_date,
                )
                for s in sources
            ]
            merged = _merge_frames(frames)
            if "month" in merged.columns:
                merged = merged[merged["month"] <= req.horizon_months]
            else:
                merged = merged.head(req.horizon_months)

            series, summary = _apply_model(node.ref_id, merged)
            run_status_lit = "completed"
            run_error = None
        except HTTPException as e:
            raw = e.detail if isinstance(e.detail, dict) else {"message": str(e.detail)}
            classified = _classify_run_error(raw)
            failed_detail = {
                **classified,
                "node_id": node.id,
                "node_label": m.name,
                "step_index": step_idx + 1,
                "raw": raw,
            }
            series, summary, run_status_lit = [], {}, "failed"
            run_error = classified["what_happened"]
        except Exception as e:
            classified = _classify_run_error(str(e))
            failed_detail = {
                **classified,
                "node_id": node.id,
                "node_label": m.name,
                "step_index": step_idx + 1,
                "raw": {"message": str(e)},
            }
            series, summary, run_status_lit = [], {}, "failed"
            run_error = classified["what_happened"]

        rid = f"run-{uuid.uuid4().hex[:10]}"
        m.last_run = run_started.isoformat() + "Z"
        store_model(m)
        # Stitch the run-context (scenario, start_date) into `notes` so
        # the run-history panel shows them inline. Storing them as
        # first-class fields would require schema + UI surface area we
        # don't need yet.
        ctx_bits = []
        if req.scenario_name:
            ctx_bits.append(f"scenario: {req.scenario_name}")
        if req.start_date:
            ctx_bits.append(f"start: {req.start_date}")
        ctx_prefix = " · ".join(ctx_bits)
        merged_notes = (
            f"[{ctx_prefix}] {req.notes}" if ctx_prefix and req.notes
            else (f"[{ctx_prefix}]" if ctx_prefix else req.notes)
        )
        run = AnalyticsRun(
            id=rid,
            function_id=req.function_id,
            name=f"{m.name} (step {step_idx + 1})",
            model_id=node.ref_id,
            input_kind="workflow",
            workflow_id=workflow_id,
            workflow_step_index=step_idx,
            input_node_ids=sources,
            horizon_months=req.horizon_months,
            status=run_status_lit,  # type: ignore[arg-type]
            summary=summary,
            series=series,
            notes=merged_notes,
            error=run_error,
            created_at=run_started.isoformat() + "Z",
            duration_ms=(datetime.utcnow() - run_started).total_seconds() * 1000,
        )
        store_run(run)
        runs.append(run)
        model_runs_by_node[node.id] = run
        outputs[node.id] = series
        node_status[node.id] = run_status_lit
        step_idx += 1

        if run_status_lit == "failed":
            return WorkflowResult(
                workflow_id=workflow_id,
                status="partial",
                runs=runs,
                destinations=destinations,
                node_status=node_status,  # type: ignore[arg-type]
                error=run_error,
                error_detail=failed_detail,
                duration_ms=(datetime.utcnow() - started).total_seconds() * 1000,
            )

    return WorkflowResult(
        workflow_id=workflow_id,
        status="completed" if runs else "failed",
        runs=runs,
        destinations=destinations,
        node_status=node_status,  # type: ignore[arg-type]
        duration_ms=(datetime.utcnow() - started).total_seconds() * 1000,
    )


@router.post("/workflow-validate", response_model=WorkflowValidationResult)
async def validate_workflow(req: WorkflowRequest, _: str = Depends(get_current_user)):
    """Reuses the chat router's validator for a single source of truth."""
    from routers.chat import _validate_workflow_payload  # local import to avoid circular
    issues_raw = _validate_workflow_payload(
        [n.model_dump() for n in req.nodes],
        [e.model_dump() for e in req.edges],
    )
    issues = [WorkflowValidationIssue(**i) for i in issues_raw]
    return WorkflowValidationResult(
        ok=not any(i.severity == "error" for i in issues),
        issues=issues,
    )

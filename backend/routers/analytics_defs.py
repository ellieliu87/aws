"""Self-serve Analytics router — user-defined analytic definitions.

Three primitives, each described by a small JSON spec:

* `aggregate`      — group-by + measures (sum/avg/weighted_avg/percentile/...)
* `compare`        — same metric across two slices (period A vs. B, dataset
                     A vs. B, etc.) → delta + % change
* `custom_python`  — escape hatch: user supplies a Python function that
                     receives input DataFrames and returns a structured
                     `{table, chart, kpis}` dict

Definitions are persisted in-memory keyed by id. Each run is recorded as an
`AnalyticDefinitionRun` and surfaced in the tab's history. The agent-assist
endpoints (`/draft`, `/runs/{id}/narrate`) use a direct `AsyncOpenAI` call
with JSON-mode response so the spec can be auto-populated from prose, and
results can carry a one-paragraph narrative the analyst can pin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query

from cof.llm_config import resolve_model
from models.schemas import (
    AggregateMeasure,
    AggregateSpec,
    AnalyticDefinition,
    AnalyticDefinitionCreate,
    AnalyticDefinitionRun,
    AnalyticDefinitionUpdate,
    AnalyticDraftRequest,
    AnalyticDraftResponse,
    AnalyticInputs,
    AnalyticNarrationResponse,
    AnalyticOutput,
    AnalyticResult,
    AnalyticResultChart,
    AnalyticResultKpi,
    AnalyticResultTable,
    CompareSpec,
    CustomPythonSpec,
)
from routers.auth import get_current_user
from routers.datasets import _DATASETS, _read_dataframe, _resolve_path

router = APIRouter()

_DEFS: dict[str, AnalyticDefinition] = {}
_RUNS: dict[str, AnalyticDefinitionRun] = {}


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ── DataFrame loading ──────────────────────────────────────────────────────
def _df_for_dataset(dataset_id: str) -> pd.DataFrame:
    d = _DATASETS.get(dataset_id)
    if not d:
        raise HTTPException(status_code=404, detail=f"Dataset {dataset_id} not found")
    try:
        path = _resolve_path(d)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not resolve dataset path: {e}")
    fmt = (d.file_format or "csv").lower()
    try:
        return _read_dataframe(Path(path), fmt)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read dataset {dataset_id}: {e}")


# ── Filter application (used by aggregate + compare) ───────────────────────
def _apply_filters(df: pd.DataFrame, filters: list[dict[str, Any]]) -> pd.DataFrame:
    if not filters:
        return df
    out = df
    for f in filters:
        col = f.get("column")
        op = (f.get("op") or "eq").lower()
        val = f.get("value")
        if not col or col not in out.columns:
            continue
        try:
            if op == "eq":
                out = out[out[col] == val]
            elif op == "ne":
                out = out[out[col] != val]
            elif op == "gt":
                out = out[out[col] > val]
            elif op == "gte":
                out = out[out[col] >= val]
            elif op == "lt":
                out = out[out[col] < val]
            elif op == "lte":
                out = out[out[col] <= val]
            elif op == "in":
                vs = val if isinstance(val, list) else [val]
                out = out[out[col].isin(vs)]
            elif op == "contains":
                out = out[out[col].astype(str).str.contains(str(val), case=False, na=False)]
        except Exception:
            # filter that doesn't apply cleanly is silently dropped — better
            # to render *something* than to fail the whole run.
            pass
    return out


# ── Aggregation ────────────────────────────────────────────────────────────
_AGG_FNS = {
    "sum":   lambda s: s.sum(),
    "avg":   lambda s: s.mean(),
    "count": lambda s: s.count(),
    "min":   lambda s: s.min(),
    "max":   lambda s: s.max(),
    "median": lambda s: s.median(),
    "p25":   lambda s: s.quantile(0.25),
    "p75":   lambda s: s.quantile(0.75),
    "p90":   lambda s: s.quantile(0.90),
    "p99":   lambda s: s.quantile(0.99),
    "stddev": lambda s: s.std(),
}


def _measure_alias(m: AggregateMeasure) -> str:
    return m.alias or f"{m.agg}_{m.column}"


def _apply_aggregate(df: pd.DataFrame, spec: AggregateSpec) -> pd.DataFrame:
    df = _apply_filters(df, spec.filters)
    if not spec.measures:
        # No measures = just count rows by group
        if spec.group_by:
            out = df.groupby(spec.group_by, dropna=False).size().reset_index(name="row_count")
        else:
            out = pd.DataFrame([{"row_count": len(df)}])
    elif spec.group_by:
        cols_needed = set(spec.group_by)
        for m in spec.measures:
            cols_needed.add(m.column)
            if m.weight_by:
                cols_needed.add(m.weight_by)
        missing = cols_needed - set(df.columns)
        if missing:
            raise HTTPException(status_code=400, detail=f"Columns not in dataset: {sorted(missing)}")

        groups = df.groupby(spec.group_by, dropna=False)
        result_records: list[dict[str, Any]] = []
        for keys, sub in groups:
            if not isinstance(keys, tuple):
                keys = (keys,)
            row: dict[str, Any] = {k: v for k, v in zip(spec.group_by, keys)}
            for m in spec.measures:
                row[_measure_alias(m)] = _eval_measure(sub, m)
            result_records.append(row)
        out = pd.DataFrame(result_records)
    else:
        # No group-by: a single row of measure values
        row: dict[str, Any] = {}
        for m in spec.measures:
            row[_measure_alias(m)] = _eval_measure(df, m)
        out = pd.DataFrame([row])

    if spec.sort_by and spec.sort_by in out.columns:
        out = out.sort_values(spec.sort_by, ascending=not spec.sort_desc, kind="mergesort")
    if spec.limit and spec.limit > 0:
        out = out.head(int(spec.limit))
    return out.reset_index(drop=True)


_NUMERIC_AGGS = {"sum", "avg", "min", "max", "median", "p25", "p75", "p90", "p99",
                 "weighted_avg", "stddev"}


def _is_numeric_series(s: pd.Series) -> bool:
    """Best-effort: is this column actually numeric? `pd.to_numeric(coerce)`
    can silently turn a string column into all-NaN, which then makes a sum
    or mean look like '0' or 'null' — so we explicitly reject string columns
    on numeric aggregators rather than papering over the bug."""
    if pd.api.types.is_numeric_dtype(s):
        return True
    # Mixed-object columns can still be valid numerics (CSVs read as object
    # with numeric content). Try a coerce and require >50% non-NaN.
    coerced = pd.to_numeric(s, errors="coerce")
    return float(coerced.notna().mean()) > 0.5


def _eval_measure(sub: pd.DataFrame, m: AggregateMeasure):
    if not m.column:
        raise HTTPException(
            status_code=400,
            detail=f"Measure has no column selected (agg={m.agg!r}). "
                   f"Pick a numeric column to {m.agg} on.",
        )
    if m.column not in sub.columns:
        raise HTTPException(
            status_code=400,
            detail=f"Measure column {m.column!r} not in dataset",
        )
    series = sub[m.column]

    # Reject numeric aggs on non-numeric columns up-front so the user gets
    # a clear "wrong column type" message instead of a silent NaN cell.
    if m.agg in _NUMERIC_AGGS and not _is_numeric_series(series):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot {m.agg} column {m.column!r} — it isn't numeric "
                   f"(dtype={series.dtype}). Use 'count' for non-numeric "
                   f"columns, or pick a different column.",
        )

    if m.agg == "weighted_avg":
        if not m.weight_by:
            raise HTTPException(status_code=400, detail=f"weighted_avg requires weight_by ({m.column})")
        if m.weight_by not in sub.columns:
            raise HTTPException(status_code=400, detail=f"weight_by column {m.weight_by!r} not in dataset")
        w = sub[m.weight_by]
        if not _is_numeric_series(w):
            raise HTTPException(status_code=400, detail=f"weight_by column {m.weight_by!r} isn't numeric")
        s = pd.to_numeric(series, errors="coerce")
        wn = pd.to_numeric(w, errors="coerce")
        mask = (~s.isna()) & (~wn.isna())
        if not mask.any() or wn[mask].sum() == 0:
            return None
        return float((s[mask] * wn[mask]).sum() / wn[mask].sum())
    fn = _AGG_FNS.get(m.agg)
    if not fn:
        raise HTTPException(status_code=400, detail=f"Unknown aggregator: {m.agg}")
    val = fn(pd.to_numeric(series, errors="coerce") if m.agg != "count" else series)
    if pd.isna(val):
        return None
    if isinstance(val, (np.integer,)):
        return int(val)
    if isinstance(val, (np.floating,)):
        return float(val)
    return val


# ── Primitive runners ──────────────────────────────────────────────────────
def _run_aggregate(d: AnalyticDefinition) -> AnalyticResult:
    if not d.aggregate_spec:
        raise HTTPException(status_code=400, detail="aggregate_spec missing")
    if not d.inputs.dataset_id:
        raise HTTPException(status_code=400, detail="dataset_id required")
    df = _df_for_dataset(d.inputs.dataset_id)
    out_df = _apply_aggregate(df, d.aggregate_spec)
    return _result_from_df(out_df, d.output)


def _run_compare(d: AnalyticDefinition) -> AnalyticResult:
    if not d.compare_spec:
        raise HTTPException(status_code=400, detail="compare_spec missing")
    if not d.inputs.dataset_id or not d.inputs.dataset_id_b:
        raise HTTPException(
            status_code=400,
            detail=(
                "Compare needs two datasets — pick both Dataset A and "
                "Dataset B in the editor. If you only want to compare "
                "two measures inside a single dataset, switch the kind "
                "to Aggregate and add multiple measures."
            ),
        )
    if d.inputs.dataset_id == d.inputs.dataset_id_b:
        raise HTTPException(
            status_code=400,
            detail=(
                "Compare requires two DIFFERENT datasets — Dataset A and "
                "Dataset B point at the same id. Pick a different B, or "
                "switch the kind to Aggregate."
            ),
        )
    df_a = _df_for_dataset(d.inputs.dataset_id)
    df_b = _df_for_dataset(d.inputs.dataset_id_b)
    spec = d.compare_spec

    # Pre-flight: every column referenced by the Compare spec must exist
    # in BOTH datasets, otherwise we'd surface a vague "column not in
    # dataset" error from the aggregate runner without saying which side
    # is missing it.
    cols_a = set(df_a.columns)
    cols_b = set(df_b.columns)
    referenced: list[tuple[str, str]] = []  # (column_name, role)
    for c in spec.group_by:
        referenced.append((c, "group_by"))
    if spec.measure.column:
        referenced.append((spec.measure.column, "measure"))
    if spec.measure.weight_by:
        referenced.append((spec.measure.weight_by, "weight_by"))

    missing_lines = []
    for col, role in referenced:
        miss_a = col not in cols_a
        miss_b = col not in cols_b
        if miss_a and miss_b:
            missing_lines.append(f"  - {role} {col!r}: not in either dataset")
        elif miss_a:
            missing_lines.append(f"  - {role} {col!r}: missing from Dataset A")
        elif miss_b:
            missing_lines.append(f"  - {role} {col!r}: missing from Dataset B")
    if missing_lines:
        common = sorted(cols_a & cols_b)
        raise HTTPException(
            status_code=400,
            detail=(
                "Compare spec references columns that aren't in both datasets:\n"
                + "\n".join(missing_lines)
                + f"\n\nColumns common to both datasets:\n  {', '.join(common) if common else '(none)'}"
            ),
        )

    inner = AggregateSpec(
        group_by=spec.group_by,
        measures=[spec.measure],
        filters=[],
        sort_by=None,
        limit=None,
    )
    a = _apply_aggregate(df_a, inner)
    b = _apply_aggregate(df_b, inner)
    measure_alias = _measure_alias(spec.measure)
    a = a.rename(columns={measure_alias: spec.label_a})
    b = b.rename(columns={measure_alias: spec.label_b})
    if spec.group_by:
        merged = a.merge(b, on=spec.group_by, how="outer")
    else:
        merged = pd.concat([a.reset_index(drop=True), b.reset_index(drop=True)], axis=1)

    merged[spec.label_a] = pd.to_numeric(merged[spec.label_a], errors="coerce").fillna(0)
    merged[spec.label_b] = pd.to_numeric(merged[spec.label_b], errors="coerce").fillna(0)
    merged["delta"] = merged[spec.label_b] - merged[spec.label_a]
    if spec.show_pct_change:
        denom = merged[spec.label_a].replace(0, np.nan)
        merged["pct_change"] = (merged["delta"] / denom) * 100.0
        merged["pct_change"] = merged["pct_change"].replace([np.inf, -np.inf], np.nan)

    if spec.group_by:
        merged = merged.sort_values("delta", ascending=False, kind="mergesort").reset_index(drop=True)

    out = d.output
    if not out.x_field and spec.group_by:
        out = AnalyticOutput(
            chart_type=out.chart_type or "bar",
            x_field=spec.group_by[0],
            y_fields=out.y_fields or ["delta"],
            description=out.description,
        )
    return _result_from_df(merged, out)


def _run_custom_python(d: AnalyticDefinition) -> AnalyticResult:
    if not d.custom_python_spec:
        raise HTTPException(status_code=400, detail="custom_python_spec missing")
    spec = d.custom_python_spec
    ds_ids = list(d.inputs.dataset_ids)
    if d.inputs.dataset_id and d.inputs.dataset_id not in ds_ids:
        ds_ids.insert(0, d.inputs.dataset_id)
    if not ds_ids:
        raise HTTPException(status_code=400, detail="At least one dataset must be bound for custom_python")

    # Stage every dataset into a temp dir as parquet so the subprocess can
    # read them deterministically without having to share the in-process
    # _DATASETS dict.
    workdir = Path(tempfile.mkdtemp(prefix="cma_anal_"))
    try:
        for did in ds_ids:
            df = _df_for_dataset(did)
            df.to_parquet(workdir / f"{did}.parquet")
        harness = _custom_python_harness(spec.python_source, spec.function_name, ds_ids, str(workdir))
        result = _exec_subprocess(harness)
    finally:
        for f in workdir.glob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Custom python failed")
    payload = result.get("result") or {}
    table = payload.get("table")
    chart = payload.get("chart")
    kpis = payload.get("kpis") or []
    return AnalyticResult(
        table=AnalyticResultTable(**table) if isinstance(table, dict) and "columns" in table else None,
        chart=AnalyticResultChart(**chart) if isinstance(chart, dict) and "type" in chart else None,
        kpis=[AnalyticResultKpi(**k) for k in kpis if isinstance(k, dict) and "label" in k],
    )


def _custom_python_harness(source: str, function_name: str, ds_ids: list[str], workdir: str) -> str:
    """Build the Python harness that loads each dataset as a DataFrame and
    invokes the user's function with `dfs` (dict id→DataFrame).

    The user's function may also accept a single positional arg if there's
    only one dataset — we try both calling conventions.
    """
    return (
        "import json, sys, traceback\n"
        "import pandas as pd\n"
        "from pathlib import Path\n"
        "\n"
        f"WORKDIR = Path(r'''{workdir}''')\n"
        f"DS_IDS = {ds_ids!r}\n"
        "\n"
        "# === user source begins ===\n"
        f"{source}\n"
        "# === user source ends ===\n"
        "\n"
        "try:\n"
        "    dfs = {did: pd.read_parquet(WORKDIR / f'{did}.parquet') for did in DS_IDS}\n"
        f"    fn = {function_name}\n"
        "    try:\n"
        "        out = fn(dfs)\n"
        "    except TypeError:\n"
        "        if len(DS_IDS) == 1:\n"
        "            out = fn(dfs[DS_IDS[0]])\n"
        "        else:\n"
        "            raise\n"
        "    if not isinstance(out, dict):\n"
        "        out = {'kpis': [{'label': 'result', 'value': str(out)}]}\n"
        "    sys.stdout.write('__CMA_RESULT__:' + json.dumps({'ok': True, 'result': out}, default=str))\n"
        "except Exception as e:\n"
        "    sys.stdout.write('__CMA_RESULT__:' + json.dumps({\n"
        "        'ok': False, 'error': str(e), 'traceback': traceback.format_exc()\n"
        "    }))\n"
    )


def _exec_subprocess(harness: str, timeout: float = 15.0) -> dict[str, Any]:
    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)
    try:
        completed = subprocess.run(
            [sys.executable, path],
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Custom python timed out after {timeout}s"}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    out = completed.stdout
    marker = "__CMA_RESULT__:"
    idx = out.rfind(marker)
    if idx == -1:
        return {"ok": False, "error": "No result envelope from subprocess",
                "traceback": (completed.stderr or out)[-2000:]}
    try:
        return json.loads(out[idx + len(marker):].strip())
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Could not parse result: {e}", "traceback": out[-2000:]}


# ── Result rendering ───────────────────────────────────────────────────────
def _result_from_df(df: pd.DataFrame, output: AnalyticOutput) -> AnalyticResult:
    """Convert a tabular result + output spec into a chart + table + kpis."""
    table = AnalyticResultTable(
        columns=list(df.columns),
        rows=df.where(pd.notna(df), None).values.tolist(),
    )
    chart: AnalyticResultChart | None = None
    kpis: list[AnalyticResultKpi] = []

    if output.chart_type == "kpi":
        # render up to 4 kpi cards from the first row's numeric columns
        if not df.empty:
            first = df.iloc[0]
            for col in df.columns:
                v = first[col]
                if isinstance(v, (int, float, np.integer, np.floating)):
                    kpis.append(AnalyticResultKpi(label=str(col), value=_fmt_num(v)))
                if len(kpis) >= 4:
                    break
    elif output.chart_type != "table":
        x = output.x_field or (df.columns[0] if len(df.columns) else None)
        y = output.y_fields or [c for c in df.columns if c != x][:1]
        if x and y:
            data = []
            for _, row in df.iterrows():
                rec: dict[str, Any] = {x: row[x]}
                for yf in y:
                    if yf in df.columns:
                        v = row[yf]
                        if isinstance(v, (np.integer,)):
                            v = int(v)
                        elif isinstance(v, (np.floating,)):
                            v = float(v) if not np.isnan(v) else None
                        rec[yf] = v
                data.append(rec)
            chart = AnalyticResultChart(
                type=output.chart_type, x_field=x, y_fields=y, data=data,
                style=output.style,  # carry the style overlay to the renderer
            )
    return AnalyticResult(table=table, chart=chart, kpis=kpis)


def _fmt_num(v) -> str:
    try:
        f = float(v)
    except Exception:
        return str(v)
    if abs(f) >= 1e9:
        return f"{f/1e9:.2f}B"
    if abs(f) >= 1e6:
        return f"{f/1e6:.2f}M"
    if abs(f) >= 1e3:
        return f"{f/1e3:.2f}K"
    if abs(f) < 1 and f != 0:
        return f"{f:.4f}"
    return f"{f:,.2f}"


# ── Run a definition ───────────────────────────────────────────────────────
def _execute_definition(d: AnalyticDefinition) -> AnalyticDefinitionRun:
    started = time.perf_counter()
    rid = f"adr-{uuid.uuid4().hex[:10]}"
    try:
        if d.kind == "aggregate":
            result = _run_aggregate(d)
        elif d.kind == "compare":
            result = _run_compare(d)
        elif d.kind == "custom_python":
            result = _run_custom_python(d)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown kind: {d.kind}")
        run = AnalyticDefinitionRun(
            id=rid,
            definition_id=d.id,
            function_id=d.function_id,
            name=d.name,
            kind=d.kind,
            status="completed",
            result=result,
            created_at=_now(),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except HTTPException as he:
        run = AnalyticDefinitionRun(
            id=rid, definition_id=d.id, function_id=d.function_id,
            name=d.name, kind=d.kind, status="failed",
            error=str(he.detail), created_at=_now(),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as e:
        run = AnalyticDefinitionRun(
            id=rid, definition_id=d.id, function_id=d.function_id,
            name=d.name, kind=d.kind, status="failed",
            error=str(e), created_at=_now(),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    _RUNS[run.id] = run
    return run


# ── CRUD endpoints ─────────────────────────────────────────────────────────
@router.get("", response_model=list[AnalyticDefinition])
async def list_definitions(
    function_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = list(_DEFS.values())
    if function_id:
        items = [d for d in items if d.function_id == function_id]
    items.sort(key=lambda d: d.updated_at or d.created_at, reverse=True)
    return items


@router.post("", response_model=AnalyticDefinition, status_code=201)
async def create_definition(req: AnalyticDefinitionCreate, _: str = Depends(get_current_user)):
    did = f"adef-{uuid.uuid4().hex[:8]}"
    d = AnalyticDefinition(
        id=did,
        created_at=_now(),
        **req.model_dump(),
    )
    _DEFS[did] = d
    return d


# Literal route comes before /{def_id} parameter route
@router.get("/runs", response_model=list[AnalyticDefinitionRun])
async def list_runs(
    function_id: str | None = Query(default=None),
    definition_id: str | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = list(_RUNS.values())
    if function_id:
        items = [r for r in items if r.function_id == function_id]
    if definition_id:
        items = [r for r in items if r.definition_id == definition_id]
    items.sort(key=lambda r: r.created_at, reverse=True)
    return items


@router.get("/runs/{run_id}", response_model=AnalyticDefinitionRun)
async def get_run(run_id: str, _: str = Depends(get_current_user)):
    r = _RUNS.get(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return r


@router.post("/runs/{run_id}/narrate", response_model=AnalyticNarrationResponse)
async def narrate_run(run_id: str, _: str = Depends(get_current_user)):
    r = _RUNS.get(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="Run not found")
    return AnalyticNarrationResponse(markdown=await _narrate(r))


@router.post("/draft", response_model=AnalyticDraftResponse)
async def draft_definition(req: AnalyticDraftRequest, _: str = Depends(get_current_user)):
    return await _draft(req)


@router.get("/{def_id}", response_model=AnalyticDefinition)
async def get_definition(def_id: str, _: str = Depends(get_current_user)):
    d = _DEFS.get(def_id)
    if not d:
        raise HTTPException(status_code=404, detail="Definition not found")
    return d


@router.patch("/{def_id}", response_model=AnalyticDefinition)
async def update_definition(def_id: str, req: AnalyticDefinitionUpdate, _: str = Depends(get_current_user)):
    d = _DEFS.get(def_id)
    if not d:
        raise HTTPException(status_code=404, detail="Definition not found")
    update = req.model_dump(exclude_unset=True)
    for k, v in update.items():
        setattr(d, k, v)
    d.updated_at = _now()
    return d


@router.delete("/{def_id}", status_code=204)
async def delete_definition(def_id: str, _: str = Depends(get_current_user)):
    if def_id not in _DEFS:
        raise HTTPException(status_code=404, detail="Definition not found")
    del _DEFS[def_id]


@router.post("/{def_id}/run", response_model=AnalyticDefinitionRun)
async def run_definition(def_id: str, _: str = Depends(get_current_user)):
    d = _DEFS.get(def_id)
    if not d:
        raise HTTPException(status_code=404, detail="Definition not found")
    return _execute_definition(d)


# ── Agent assist: draft + narrate ─────────────────────────────────────────
def _llm_client():
    """Match oasia: AsyncOpenAI() with no arguments. The SDK auto-resolves
    OPENAI_BASE_URL / OPENAI_API_KEY from env; corporate COF proxy
    environments preconfigure these transparently."""
    try:
        from openai import AsyncOpenAI
    except ImportError:
        raise HTTPException(status_code=503, detail="openai package not installed.")
    return AsyncOpenAI()


_DRAFT_SYSTEM = """You design self-serve analytics for a domain-agnostic
analytics workbench. Given a plain-English prompt and a list of available
datasets (with column names + dtypes), you pick ONE primitive and produce a
JSON spec the runner can execute.

Three primitive kinds, each with its own spec key:

1. "aggregate" — group-by + measures
   aggregate_spec: {
     group_by: [<column>...],          // 0+ categorical/date columns
     measures: [{
        column: <numeric column>,
        agg: "sum"|"avg"|"count"|"min"|"max"|"median"|"p25"|"p75"|"p90"|"p99"|"weighted_avg"|"stddev",
        alias: <optional output name>,
        weight_by: <numeric column>    // ONLY when agg == "weighted_avg"
     }],
     filters: [{column, op: "eq"|"ne"|"gt"|"gte"|"lt"|"lte"|"in"|"contains", value}],
     sort_by: <output column or null>,
     sort_desc: true,
     limit: 100
   }
   inputs: { dataset_id: <id of the chosen dataset> }

2. "compare" — same metric across two datasets/slices
   compare_spec: {
     group_by: [<column>...],
     measure: { column, agg, alias?, weight_by? },
     label_a: "<short>", label_b: "<short>",
     show_pct_change: true
   }
   inputs: { dataset_id: <A>, dataset_id_b: <B> }

3. "custom_python" — only when neither aggregate nor compare fits
   custom_python_spec: {
     function_name: "run",
     python_source: "def run(dfs):\\n    df = dfs['<id>']\\n    ...\\n    return {'kpis': [...], 'chart': {...}, 'table': {...}}"
   }
   inputs: { dataset_ids: [<id>, ...] }
   The function must return a dict with any combination of:
     - "kpis": [{label, value, sublabel?}]
     - "chart": {type: "bar"|"line"|"area"|"stacked_bar"|"scatter"|"pie", x_field, y_fields:[...], data:[{...}]}
     - "table": {columns:[...], rows:[[...],...]}
   Use ONLY pandas + numpy + python stdlib.

Always include `output`: {chart_type, x_field, y_fields, description}.

Pick column names ONLY from the supplied datasets. If the prompt refers to a
metric not present, pick the closest match and explain in `notes`.

Reply with STRICT JSON, NO prose, NO markdown:
{
  "name": "<concise label>",
  "description": "<one sentence>",
  "kind": "aggregate" | "compare" | "custom_python",
  "inputs": {...},
  "aggregate_spec": {...} | null,
  "compare_spec": {...} | null,
  "custom_python_spec": {...} | null,
  "output": {...},
  "notes": "<optional caveats>"
}
Set the two unused spec keys to null."""


def _maybe_draft_beta_justification(req: AnalyticDraftRequest) -> AnalyticDraftResponse | None:
    """Fast-path for prompts that ask to justify the projected deposit beta.

    Bypasses the LLM and returns a pre-built `custom_python` definition
    that — on Run — loads the commercial CCAR output + rate history
    datasets, computes projected vs historical betas, classifies each
    product against the P60 fixed-pricing-percentile assumption, and
    emits a scatter chart (historical on X, projected on Y) plus KPIs.

    Mirrors the 4-agent chat-panel chain (beta-quant → beta-benchmarker
    → beta-visualizer → beta-challenger), but condensed into one
    deterministic Python function so the analytics tab doesn't pay a
    multi-round agent latency cost.
    """
    p = (req.prompt or "").lower()
    triggers = ("beta",)
    actions = ("justif", "challeng", "defen", "reasonable", "support")
    if not any(t in p for t in triggers) or not any(a in p for a in actions):
        return None
    if "deposit" not in p and "ccar" not in p and "commercial" not in p:
        return None

    # Resolve dataset ids by registered name. Both projection + actuals
    # are required. Names matched case-insensitively (also tolerates
    # underscores, hyphens, spaces) so a user-renamed dataset still binds.
    def _norm(s):
        return str(s or "").lower().replace("_", "").replace("-", "").replace(" ", "")
    available = req.available_datasets or []
    by_norm = {_norm(d.get("name")): d.get("id") for d in available}

    ds_output_aliases = (
        "commercial_deposit_output_CCAR26",
        "commercial_deposit_output",
        "commercial_CCAR_output",   # legacy demo name
        "commercial_ccar_output",
    )
    ds_actuals_aliases = (
        "commercial_deposit_rate_actuals",
        "commercial_deposit_actuals",
        "commercial_rate_actuals",
        "commercial_rate_history",   # legacy demo name
    )

    def _pick(*aliases):
        for a in aliases:
            v = by_norm.get(_norm(a))
            if v:
                return v
        return None

    ds_proj = _pick(*ds_output_aliases)
    ds_hist = _pick(*ds_actuals_aliases)
    if not (ds_proj and ds_hist):
        return None

    dataset_ids = [ds_proj, ds_hist]

    # Detect lookback hints — when the analyst frames the comparison
    # against a specific historical period (the 2022/2023 tightening
    # cycle), narrow the historical regression to that window so the
    # comparison is "BHCS projection vs the same kind of cycle we just
    # lived through" rather than "BHCS projection vs a 6-year average
    # that includes ZIRP".
    lookback_iso = None
    lookback_label = None
    if "2023 rate hike" in p or "2023 tightening" in p or "2023 hike cycle" in p \
            or "tightening cycle" in p or "rate hike cycle" in p \
            or "hiking cycle" in p:
        lookback_iso = "2022-01-01"
        lookback_label = "2022-Q1 onwards (the 2022-23 Fed tightening cycle)"
    elif "since 2023" in p or "from 2023" in p:
        lookback_iso = "2023-01-01"
        lookback_label = "2023-Q1 onwards"
    elif "since 2022" in p or "from 2022" in p:
        lookback_iso = "2022-01-01"
        lookback_label = "2022-Q1 onwards"

    # Detect projection scenario — the supervisory cycle has four named
    # paths (BHCB/BHCS/FEDB/FEDSA). The matcher below is hierarchical:
    # explicit codes win over plain-language synonyms, and FEDSA wins
    # over plain "stress" because severely-adverse is more specific.
    scenario_code = None
    scenario_label = None
    if "fedsa" in p or "severely adverse" in p or "severely-adverse" in p or "fed severely" in p:
        scenario_code, scenario_label = "FEDSA", "Fed Severely Adverse (FEDSA)"
    elif "bhcs" in p or "bhc stress" in p or "bhc-stress" in p:
        scenario_code, scenario_label = "BHCS", "BHC Stress (BHCS)"
    elif "fedb" in p or "fed base" in p or "fed baseline" in p:
        scenario_code, scenario_label = "FEDB", "Fed Baseline (FEDB)"
    elif "bhcb" in p or "bhc base" in p or "bhc baseline" in p:
        scenario_code, scenario_label = "BHCB", "BHC Baseline (BHCB)"
    elif "stress" in p:
        # Generic "stress" — default to BHCS (the conservative case for
        # CCAR commentary) but record so the analyst can tell.
        scenario_code, scenario_label = "BHCS", "BHC Stress (BHCS) — inferred from 'stress'"
    elif "baseline" in p or " base " in p:
        scenario_code, scenario_label = "BHCB", "BHC Baseline (BHCB) — inferred from 'baseline'"

    PYTHON_SOURCE = '''def run(dfs):
    """Beta justification — compute projected vs historical effective
    deposit beta per segment and classify against the P60 fixed-pricing-
    percentile assumption. Schema: both projection and actuals frames are
    long-format with columns scenario, snap_date, variable_name,
    variable_value, segment, origin. Tolerates case + underscore
    variants in column / variable / origin values. Identifies the two
    frames by date range (most-recent-spanning = projection)."""
    import numpy as np
    import pandas as pd

    # Optional lookback for the historical regression — set by the draft
    # handler when the analyst's prompt names a specific cycle. When None,
    # the OLS uses every actuals row.
    HISTORICAL_LOOKBACK = __LOOKBACK_PLACEHOLDER__
    HISTORICAL_LOOKBACK_LABEL = __LOOKBACK_LABEL_PLACEHOLDER__
    # Optional projection scenario filter — set when the analyst names one
    # of BHCB / BHCS / FEDB / FEDSA. When None, the projection beta is
    # computed against whatever scenario shows up first in the data.
    PROJECTION_SCENARIO = __SCENARIO_PLACEHOLDER__
    PROJECTION_SCENARIO_LABEL = __SCENARIO_LABEL_PLACEHOLDER__

    def _norm(s):
        return str(s).lower().replace("_", "").replace("-", "").replace(" ", "")

    def _ci_pick(df, *candidates):
        by_n = {_norm(c): c for c in df.columns}
        for cand in candidates:
            f = by_n.get(_norm(cand))
            if f is not None:
                return f
        return None

    def _ci_match(series, *candidates):
        norm_cands = {_norm(c) for c in candidates}
        return [v for v in series.dropna().unique() if _norm(v) in norm_cands]

    INPUT_ALIASES  = ["model_input", "input", "macro_input", "macro", "predictor", "feature"]
    OUTPUT_ALIASES = ["model_output", "output", "predicted", "target", "modeled"]
    RATE_ALIASES   = ["rate_paid", "rate_paid_pct", "interest_apy", "interest_apr",
                       "interest_rate", "rate", "rate_paid_apr"]
    FF_ALIASES     = ["fed_funds_rate", "fed_funds", "fedfunds", "ff_rate", "ffr", "fed_funds_pct"]

    def _beta_for_frame(df, mode):
        """Compute per-segment beta for one frame.
        mode == 'projection' → Δrate / ΔFF using endpoint values.
        mode == 'history'    → OLS slope of rate on FF, plus R²."""
        var_col  = _ci_pick(df, "variable_name", "metric")
        val_col  = _ci_pick(df, "variable_value", "value")
        date_col = _ci_pick(df, "snap_date", "date", "period", "quarter_id", "as_of_date")
        seg_col  = _ci_pick(df, "segment", "product_name", "product", "product_l1")
        org_col  = _ci_pick(df, "origin", "source", "io")
        if not (var_col and val_col and date_col and seg_col):
            return None, f"frame missing required columns; have: {list(df.columns)}"

        rate_matches = _ci_match(df[var_col], *RATE_ALIASES)
        ff_matches   = _ci_match(df[var_col], *FF_ALIASES)
        if not rate_matches:
            return None, f"no rate-paid variable. seen: {sorted(map(str, df[var_col].dropna().unique()))[:10]}"
        if not ff_matches:
            return None, f"no fed_funds_rate variable. seen: {sorted(map(str, df[var_col].dropna().unique()))[:10]}"

        # Drop missing values before doing anything — sparse uploads
        # shouldn't poison the endpoints / regression.
        rate_df = df[df[var_col].isin(rate_matches)].copy()
        if org_col is not None:
            m = _ci_match(rate_df[org_col], *OUTPUT_ALIASES)
            if m:
                rate_df = rate_df[rate_df[org_col].isin(m)]
        rate_df = rate_df.dropna(subset=[seg_col])
        rate_df = rate_df[rate_df[seg_col].astype(str).str.strip() != ""]
        rate_df[val_col] = pd.to_numeric(rate_df[val_col], errors="coerce")
        rate_df = rate_df.dropna(subset=[val_col])

        ff_df = df[df[var_col].isin(ff_matches)].copy()
        if org_col is not None:
            m = _ci_match(ff_df[org_col], *INPUT_ALIASES)
            if m:
                ff_df = ff_df[ff_df[org_col].isin(m)]
        ff_df[val_col] = pd.to_numeric(ff_df[val_col], errors="coerce")
        ff_df = ff_df.dropna(subset=[val_col])
        ff_path = ff_df.groupby(date_col)[val_col].mean().dropna().sort_index()
        if len(ff_path) < 2:
            return None, "need at least 2 non-null fed_funds_rate observations"

        out = {}
        if mode == "projection":
            for segment, sub in rate_df.groupby(seg_col):
                rp = sub.groupby(date_col)[val_col].mean().dropna().sort_index()
                if len(rp) < 2:
                    continue
                # Match endpoints to the segment's own observation window
                # so missing rate_paid rows don't drag beta against a
                # global ΔFF the segment never spanned.
                seg_start_date, seg_end_date = rp.index[0], rp.index[-1]
                ff_start_seg = ff_path.get(seg_start_date)
                ff_end_seg   = ff_path.get(seg_end_date)
                if ff_start_seg is None or ff_end_seg is None or pd.isna(ff_start_seg) or pd.isna(ff_end_seg):
                    continue
                seg_ff_change = float(ff_end_seg - ff_start_seg)
                if abs(seg_ff_change) < 1e-6:
                    continue
                out[str(segment)] = {
                    "beta": float((rp.iloc[-1] - rp.iloc[0]) / seg_ff_change),
                    "r_squared": None,
                }
        else:  # history → OLS
            ff_series = ff_path.rename("_ff").to_frame().reset_index()
            for segment, sub in rate_df.groupby(seg_col):
                seg_path = (sub.groupby(date_col)[val_col].mean().dropna()
                               .rename("_y").to_frame().reset_index())
                merged = (seg_path.merge(ff_series, on=date_col, how="inner")
                                  .dropna(subset=["_y", "_ff"])
                                  .sort_values(date_col))
                if len(merged) < 3:
                    continue
                x = merged["_ff"].astype(float).to_numpy()
                y = merged["_y"].astype(float).to_numpy()
                if x.var() < 1e-9:
                    continue
                slope, intercept = np.polyfit(x, y, 1)
                y_hat = intercept + slope * x
                ss_res = float(((y - y_hat) ** 2).sum())
                ss_tot = float(((y - y.mean()) ** 2).sum())
                r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
                out[str(segment)] = {"beta": float(slope), "r_squared": float(r2)}
        return out, None

    # Identify projection vs actuals by latest snap_date — projection
    # always points into the future; actuals end at the recent past.
    frames = []
    for _k, df in dfs.items():
        date_col = _ci_pick(df, "snap_date", "date", "period", "quarter_id", "as_of_date")
        if date_col is None:
            continue
        try:
            max_d = pd.to_datetime(df[date_col], errors="coerce").max()
            frames.append((max_d, df))
        except Exception:
            continue
    if len(frames) < 2:
        return {"kpis": [{"label": "Error", "value": f"need 2 dated frames, got {len(frames)}"}]}
    frames.sort(key=lambda t: t[0])
    actuals_df, projection_df = frames[0][1], frames[-1][1]

    # Apply the optional historical lookback window (when the analyst's
    # prompt named a specific cycle).
    if HISTORICAL_LOOKBACK:
        date_col_h = _ci_pick(actuals_df, "snap_date", "date", "period", "as_of_date")
        if date_col_h:
            actuals_df = actuals_df.copy()
            actuals_df[date_col_h] = pd.to_datetime(actuals_df[date_col_h], errors="coerce")
            actuals_df = actuals_df[actuals_df[date_col_h] >= pd.to_datetime(HISTORICAL_LOOKBACK)]

    # Apply the optional projection-scenario filter. The match is
    # case-insensitive and tolerates the long-form scenario string used
    # in some submissions (e.g. CCAR_26_BHC_Stress matches BHCS via the
    # substring "bhc" + "stress" both being normalised forms of "bhcs").
    if PROJECTION_SCENARIO:
        scen_col = _ci_pick(projection_df, "scenario", "scenario_id", "scenarioName")
        if scen_col is not None:
            target = _norm(PROJECTION_SCENARIO)
            # Build an inclusive predicate: row's scenario contains the target
            # as a substring (after norm), OR its long form decomposes to the
            # same code (e.g. "bhc" + "stress" → "bhcs").
            def _matches_scen(v):
                nv = _norm(v)
                if target in nv or nv in target:
                    return True
                if target == "bhcs" and "bhc" in nv and "stress" in nv: return True
                if target == "bhcb" and "bhc" in nv and ("base" in nv or "baseline" in nv): return True
                if target == "fedsa" and "fed" in nv and ("severely" in nv or "sevadv" in nv): return True
                if target == "fedb" and "fed" in nv and ("base" in nv or "baseline" in nv): return True
                return False
            filtered = projection_df[projection_df[scen_col].apply(_matches_scen)]
            if not len(filtered):
                seen = sorted(map(str, projection_df[scen_col].dropna().unique()))[:8]
                return {"kpis": [{"label": "Error", "value": f"no projection rows for scenario {PROJECTION_SCENARIO}; seen: {seen}"}]}
            projection_df = filtered

    proj_betas, err = _beta_for_frame(projection_df, "projection")
    if err:
        return {"kpis": [{"label": "Error", "value": f"projection: {err}"}]}
    hist_betas, err = _beta_for_frame(actuals_df, "history")
    if err:
        return {"kpis": [{"label": "Error", "value": f"actuals: {err}"}]}

    # Join on segment name (case-insensitive)
    TOL = 0.10
    norm_hist = {_norm(k): k for k in hist_betas}
    chart_rows, table_rows = [], []
    aligned = overshoot = undershoot = 0
    for p_name, pr in proj_betas.items():
        h = norm_hist.get(_norm(p_name))
        if h is None:
            continue
        pb = round(float(pr["beta"]), 3)
        hb = round(float(hist_betas[h]["beta"]), 3)
        r2 = round(float(hist_betas[h]["r_squared"] or 0.0), 3)
        gap = round(pb - hb, 3)
        if abs(gap) <= TOL:
            status = "ALIGNED"; aligned += 1
        elif gap > 0:
            status = "OVERSHOOT"; overshoot += 1
        else:
            status = "UNDERSHOOT"; undershoot += 1
        chart_rows.append({
            "product":         p_name,
            "historical_beta": hb,
            "projected_beta":  pb,
            "status":          status,
        })
        table_rows.append([p_name, hb, pb, gap, status, r2])

    overall = "PASS" if (overshoot + undershoot) == 0 else "REVIEW"

    # Title carries the verdict so the analyst sees PASS / REVIEW + counts
    # without needing the four KPI cards above the scatter.
    counts = f"{aligned} aligned · {overshoot} overshoot · {undershoot} undershoot"
    title_lead = "Commercial deposit beta"
    if PROJECTION_SCENARIO_LABEL:
        title_lead += f" — {PROJECTION_SCENARIO_LABEL}"
    chart_title = f"{title_lead} — {overall} ({counts})"
    if HISTORICAL_LOOKBACK_LABEL:
        chart_title += f"  ·  hist window: {HISTORICAL_LOOKBACK_LABEL}"

    return {
        # Empty kpis → no card strip above the chart. Verdict + counts are
        # now folded into the chart title for a cleaner top-of-pane.
        "kpis": [],
        "chart": {
            "type":     "scatter",
            "x_field":  "historical_beta",
            "y_fields": ["projected_beta"],
            "data":     chart_rows,
            "style":    {
                "title":        chart_title,
                "x_axis_label": "Historical beta",
                "y_axis_label": "Projected beta",
            },
        },
        "table": {
            "columns": ["product", "historical_beta", "projected_beta", "gap", "status", "r_squared"],
            "rows":    sorted(table_rows, key=lambda r: -abs(r[3])),
        },
    }
'''
    # Substitute the placeholders the embedded function reads. Scenario
    # filter + lookback window are detected from the analyst's prompt
    # before this point and baked into the python_source so each
    # AnalyticDefinition records exactly which scenario it ran for.
    PYTHON_SOURCE = (
        PYTHON_SOURCE
        .replace("__LOOKBACK_PLACEHOLDER__",       repr(lookback_iso)    if lookback_iso    else "None")
        .replace("__LOOKBACK_LABEL_PLACEHOLDER__", repr(lookback_label)  if lookback_label  else "None")
        .replace("__SCENARIO_PLACEHOLDER__",       repr(scenario_code)   if scenario_code   else "None")
        .replace("__SCENARIO_LABEL_PLACEHOLDER__", repr(scenario_label)  if scenario_label  else "None")
    )

    return AnalyticDraftResponse(
        name="Commercial deposit beta — justification",
        description=(
            "Projected vs historical effective beta per commercial deposit "
            "product, classified against the P60 fixed-pricing-percentile "
            "assumption. Mirrors the 4-agent beta-justification chain "
            "(quant → benchmarker → visualizer → challenger) as a single "
            "deterministic analytic."
        ),
        kind="custom_python",
        inputs=AnalyticInputs(dataset_ids=dataset_ids),
        custom_python_spec=CustomPythonSpec(
            function_name="run",
            python_source=PYTHON_SOURCE,
        ),
        output=AnalyticOutput(
            chart_type="scatter",
            x_field="historical_beta",
            y_fields=["projected_beta"],
            description=(
                "One point per product. Above the 45° line = projection "
                "more aggressive than history; below = more conservative. "
                "Tolerance band ±0.10 = P60 peer-pricing assumption."
            ),
        ),
        notes=(
            "Recognised as a beta-justification request — pre-built from the "
            "commercial_deposit_output_CCAR26 + commercial_deposit_rate_actuals "
            "datasets without an LLM round-trip. Both files share the long-format "
            "schema (scenario, snap_date, variable_name, variable_value, segment, "
            "origin); the function is schema-tolerant about column-name case, "
            "underscores, and accepts alias values for variable_name and origin. "
            "Click Run to render the scatter."
        ),
    )


async def _draft(req: AnalyticDraftRequest) -> AnalyticDraftResponse:
    # Fast-path: beta-justification prompts skip the LLM and use the
    # 4-agent chain's deterministic equivalent.
    fast = _maybe_draft_beta_justification(req)
    if fast is not None:
        return fast

    client = _llm_client()
    user = req.prompt.strip()
    if req.available_datasets:
        user += "\n\n[Available datasets]\n" + json.dumps(req.available_datasets, default=str)[:6000]
    try:
        completion = await client.chat.completions.create(
            model=resolve_model(os.getenv("CMA_TOOL_DRAFT_MODEL")),
            messages=[
                {"role": "system", "content": _DRAFT_SYSTEM},
                {"role": "user", "content": user},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    raw = completion.choices[0].message.content or ""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=502, detail=f"LLM returned non-JSON: {e}")

    kind = data.get("kind") or "aggregate"
    if kind not in ("aggregate", "compare", "custom_python"):
        kind = "aggregate"

    inputs_raw = data.get("inputs") or {}
    output_raw = data.get("output") or {}
    notes = data.get("notes")

    # ── Post-validation: catch the common LLM mistakes before they reach
    #    the runner. Two specific failures we've seen on real prompts:
    #
    #    (1) `kind == "compare"` with no `dataset_id_b` (or A == B).
    #        That fails at run time with "dataset_id and dataset_id_b
    #        required". The user's intent was almost always "show two
    #        measures on one dataset" — i.e. an aggregate. Downgrade the
    #        spec and surface the assumption in `notes`.
    #
    #    (2) An aggregate spec with empty `column` on a measure. We
    #        let the runner reject this with a clear message — drafting
    #        rarely emits empty columns since the prompt asks for column
    #        names, but the runner now catches it explicitly.
    if kind == "compare":
        ds_a = inputs_raw.get("dataset_id")
        ds_b = inputs_raw.get("dataset_id_b")
        cspec = data.get("compare_spec") or {}
        if not ds_b or ds_b == ds_a:
            measure = cspec.get("measure") or {}
            kind = "aggregate"
            data["aggregate_spec"] = {
                "group_by": cspec.get("group_by", []),
                "measures": [measure] if measure.get("column") else [],
                "filters": [],
                "sort_by": None,
                "sort_desc": True,
                "limit": 200,
            }
            data["compare_spec"] = None
            warning = (
                "Auto-converted to an Aggregate — your prompt didn't supply "
                "two distinct datasets, which Compare requires. If you want a "
                "true A-vs-B comparison, pick a second dataset and switch "
                "the kind back to Compare."
            )
            notes = f"{notes}\n{warning}" if notes else warning

    try:
        return AnalyticDraftResponse(
            name=(data.get("name") or "Untitled analytic").strip(),
            description=(data.get("description") or "").strip(),
            kind=kind,
            inputs=AnalyticInputs(**{k: v for k, v in inputs_raw.items() if v is not None}),
            aggregate_spec=AggregateSpec(**data["aggregate_spec"])
                if kind == "aggregate" and data.get("aggregate_spec") else None,
            compare_spec=CompareSpec(**data["compare_spec"])
                if kind == "compare" and data.get("compare_spec") else None,
            custom_python_spec=CustomPythonSpec(**data["custom_python_spec"])
                if kind == "custom_python" and data.get("custom_python_spec") else None,
            output=AnalyticOutput(**output_raw) if output_raw else AnalyticOutput(),
            notes=notes,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM draft did not parse: {e}")


_NARRATE_SYSTEM = """You write a concise one-paragraph executive summary of an
analytics result. Read the kpis / chart data / table sample. Identify the 1-3
most notable facts (largest contributor, biggest delta, distribution shape,
anomaly). Keep it under 80 words. Plain markdown — no headings, no bullets
unless the finding genuinely benefits from them. Prefer numbers from the
result over generalities."""


async def _narrate(run: AnalyticDefinitionRun) -> str:
    client = _llm_client()
    if not run.result:
        return run.error or "(no result to narrate)"

    payload: dict[str, Any] = {
        "name": run.name,
        "kind": run.kind,
        "kpis": [k.model_dump() for k in run.result.kpis],
    }
    if run.result.chart:
        c = run.result.chart.model_dump()
        c["data"] = c.get("data", [])[:30]  # cap context
        payload["chart"] = c
    if run.result.table:
        t = run.result.table.model_dump()
        t["rows"] = t.get("rows", [])[:30]
        payload["table"] = t

    try:
        completion = await client.chat.completions.create(
            model=resolve_model(os.getenv("CMA_TOOL_DRAFT_MODEL")),
            messages=[
                {"role": "system", "content": _NARRATE_SYSTEM},
                {"role": "user", "content": json.dumps(payload, default=str)[:8000]},
            ],
            temperature=0.2,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")
    return (completion.choices[0].message.content or "").strip() or "(empty narrative)"

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

    # Resolve dataset ids by registered name. Output + history are required;
    # input (the macro Fed Funds path) is optional — when present, the
    # generated function reads the FF path from there. When absent, it
    # falls back to looking for fed_funds_rate inside the output file.
    # Names matched case-insensitively so a user-renamed dataset still
    # binds.
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
    ds_input_aliases = (
        "commercial_deposit_input_CCAR26",
        "commercial_deposit_input",
        "commercial_CCAR_input",
        "commercial_ccar_input",
    )
    ds_history_aliases = (
        "commercial_rate_history",
        "commercial_deposit_rate_history",
    )

    def _pick(*aliases):
        for a in aliases:
            v = by_norm.get(_norm(a))
            if v:
                return v
        return None

    ds_proj = _pick(*ds_output_aliases)
    ds_hist = _pick(*ds_history_aliases)
    ds_input = _pick(*ds_input_aliases)
    if not (ds_proj and ds_hist):
        return None

    dataset_ids = [ds_proj]
    if ds_input:
        dataset_ids.append(ds_input)
    dataset_ids.append(ds_hist)

    PYTHON_SOURCE = '''def run(dfs):
    """Beta justification — compute projected vs historical effective
    deposit beta per product and classify against the P60 fixed-pricing-
    percentile assumption. Schema-tolerant: identifies each input frame
    by its column shape (case + underscore insensitive), parses
    additional_dimensions JSON for product names, and falls back to
    output-embedded fed_funds_rate when no separate input frame is bound."""
    import json, ast
    import numpy as np
    import pandas as pd

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

    def _parse_dims(x):
        if isinstance(x, dict):
            return x
        try:
            if pd.isna(x):
                return {}
        except Exception:
            pass
        s = str(x)
        try:
            return json.loads(s)
        except Exception:
            try:
                return ast.literal_eval(s)
            except Exception:
                return {}

    # Identify the three frames from their column shapes:
    #   output  — has variable_name AND additional_dimensions
    #   input   — has variable_name AND no additional_dimensions
    #   history — has a fed-funds column AND no variable_name
    out_df = in_df = hist_df = None
    for _k, df in dfs.items():
        var = _ci_pick(df, "variable_name", "metric")
        dims = _ci_pick(df, "additional_dimensions", "additionalDimensions", "dims")
        ff_col = _ci_pick(df, "fedfunds", "fed_funds", "fed_funds_rate", "ff_rate", "ffr")
        if var is not None and dims is not None:
            out_df = df
        elif var is not None and ff_col is None:
            in_df = df
        elif ff_col is not None and var is None:
            hist_df = df
    if out_df is None or hist_df is None:
        cols = {k: list(v.columns)[:8] for k, v in dfs.items()}
        return {"kpis": [{"label": "Error", "value": f"Could not identify output + history frames from column shapes: {cols}"}]}

    # ── Projected beta ────────────────────────────────────────────────
    var_col = _ci_pick(out_df, "variable_name", "metric")
    val_col = _ci_pick(out_df, "variable_value", "value")
    date_col = _ci_pick(out_df, "snap_date", "date", "quarter_id", "period")
    dims_col = _ci_pick(out_df, "additional_dimensions", "additionalDimensions", "dims")
    if not (var_col and val_col and date_col and dims_col):
        return {"kpis": [{"label": "Error", "value": f"output frame missing required columns; have: {list(out_df.columns)}"}]}

    rate_aliases = ["rate_paid", "rate_paid_pct", "interest_apy", "interest_apr",
                    "interest_rate", "rate_paid_apr"]
    rate_matches = _ci_match(out_df[var_col], *rate_aliases)
    if not rate_matches:
        return {"kpis": [{"label": "Error", "value": f"no rate-paid variable in output. seen: {sorted(map(str, out_df[var_col].dropna().unique()))[:10]}"}]}

    rate_df = out_df[out_df[var_col].isin(rate_matches)].copy()
    rate_df["_product"] = rate_df[dims_col].apply(
        lambda d: (_parse_dims(d).get("product_name")
                   or _parse_dims(d).get("product")
                   or _parse_dims(d).get("product_l1")))
    rate_df = rate_df.dropna(subset=["_product"])

    ff_aliases = ["fed_funds_rate", "fed_funds", "fedfunds", "ff_rate", "ffr", "fed_funds_pct"]
    ff_path = None
    if in_df is not None:
        in_var = _ci_pick(in_df, "variable_name", "metric")
        in_val = _ci_pick(in_df, "variable_value", "value")
        in_date = _ci_pick(in_df, "snap_date", "date", "period")
        if in_var and in_val and in_date:
            ff_match = _ci_match(in_df[in_var], *ff_aliases)
            if ff_match:
                ff_path = (in_df[in_df[in_var].isin(ff_match)]
                              .groupby(in_date)[in_val].mean().sort_index())
    if ff_path is None or len(ff_path) < 2:
        ff_match = _ci_match(out_df[var_col], *ff_aliases)
        if ff_match:
            ff_path = (out_df[out_df[var_col].isin(ff_match)]
                          .groupby(date_col)[val_col].mean().sort_index())
    if ff_path is None or len(ff_path) < 2:
        return {"kpis": [{"label": "Error", "value": "fed_funds_rate not found in input or output frame"}]}

    ff_change = float(ff_path.iloc[-1] - ff_path.iloc[0])
    if abs(ff_change) < 1e-6:
        return {"kpis": [{"label": "Error", "value": "fed_funds_rate is flat across the horizon"}]}

    proj_betas = {}
    for product, sub in rate_df.groupby("_product"):
        rp = sub.groupby(date_col)[val_col].mean().sort_index()
        if len(rp) < 2:
            continue
        proj_betas[str(product)] = float((rp.iloc[-1] - rp.iloc[0]) / ff_change)

    # ── Historical beta ───────────────────────────────────────────────
    date_h = _ci_pick(hist_df, "date", "snap_date", "as_of_date", "observation_date")
    ff_h = _ci_pick(hist_df, "fedfunds", "fed_funds", "fed_funds_rate", "ff_rate", "ffr", "fed_funds_pct")
    if not (date_h and ff_h):
        return {"kpis": [{"label": "Error", "value": f"history frame missing date or FF column; have: {list(hist_df.columns)}"}]}

    EXCLUDED_MACROS = {"bbbyield", "rgt10y", "bbbspread", "bbb_spread",
                       "ust10y", "ust2y", "ust30y", "ust_2y", "ust_30y",
                       "treasury10y", "treasury2y", "vix", "spx", "djia",
                       "unemployment", "unemploymentpct", "gdp", "gdpyoypct",
                       "creprice", "hpi", "hpiyoypct", "oil", "m2", "m2gdp"}
    excl = EXCLUDED_MACROS | {_norm(date_h), _norm(ff_h)}
    product_cols = [c for c in hist_df.columns
                    if _norm(c) not in excl and pd.api.types.is_numeric_dtype(hist_df[c])]

    hist_betas, hist_r2 = {}, {}
    for c in product_cols:
        sub = hist_df.dropna(subset=[c, ff_h])
        if len(sub) < 3:
            continue
        x = sub[ff_h].astype(float).to_numpy()
        y = sub[c].astype(float).to_numpy()
        if x.var() < 1e-9:
            continue
        slope, intercept = np.polyfit(x, y, 1)
        y_hat = intercept + slope * x
        ss_res = float(((y - y_hat) ** 2).sum())
        ss_tot = float(((y - y.mean()) ** 2).sum())
        hist_betas[str(c)] = float(slope)
        hist_r2[str(c)]    = (1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else 0.0

    # ── Join + classify against ±0.10 (P60 peer pricing) ─────────────
    TOL = 0.10
    norm_hist = {_norm(k): k for k in hist_betas}
    chart_rows, table_rows = [], []
    aligned = overshoot = undershoot = 0
    for p_name, pb_raw in proj_betas.items():
        h = norm_hist.get(_norm(p_name))
        if h is None:
            continue
        pb = round(float(pb_raw), 3)
        hb = round(float(hist_betas[h]), 3)
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
        table_rows.append([p_name, hb, pb, gap, status, round(hist_r2.get(h, 0.0), 3)])

    overall = "PASS" if (overshoot + undershoot) == 0 else "REVIEW"

    return {
        "kpis": [
            {"label": "Overall",    "value": overall,            "sublabel": "vs P60 peer pricing"},
            {"label": "Aligned",    "value": str(aligned),       "sublabel": "within ±0.10"},
            {"label": "Overshoot",  "value": str(overshoot),     "sublabel": "projected > historical"},
            {"label": "Undershoot", "value": str(undershoot),    "sublabel": "projected < historical"},
        ],
        "chart": {
            "type":     "scatter",
            "x_field":  "historical_beta",
            "y_fields": ["projected_beta"],
            "data":     chart_rows,
            "style":    {
                "title":        "Commercial deposit beta — projected vs historical",
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
            "commercial_deposit_output_CCAR26 + commercial_deposit_input_CCAR26 + "
            "commercial_rate_history datasets without an LLM round-trip. "
            "The function is schema-tolerant: case/underscore-insensitive "
            "column names, alias variable-name values, and works whether "
            "fed_funds_rate lives in the input file or the output file. "
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

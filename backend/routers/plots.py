"""Plots router - per-function report plots that can read from datasets,
analytics runs, or fall back to sample data for the live designer.
"""
import logging
import uuid
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query

from models.schemas import PlotConfig, PlotConfigCreate
from routers.auth import get_current_user
from routers.datasets import _read_dataframe, _resolve_path, load_dataset
from routers.scenarios import load_run

router = APIRouter()
log = logging.getLogger("cma.plots")


_PLOTS: dict[str, PlotConfig] = {}


def _ingest_pack_plots() -> None:
    """Materialize every pack-attached plot into `_PLOTS`. Called at
    startup so pack-shipped demo dashboards land in the Reporting tab as
    if a user had built them. Existing entries with the same id are left
    alone so a user-edited tile isn't clobbered on a future re-ingest."""
    from packs import plot_attachments
    for att in plot_attachments():
        plot_id = att["plot_id"]
        if plot_id in _PLOTS:
            continue
        cfg = dict(att.get("config") or {})
        cfg["id"] = plot_id
        cfg.setdefault("function_id", att["function_id"])
        try:
            _PLOTS[plot_id] = PlotConfig(**cfg)
        except Exception as e:
            # Don't kill startup over a bad pack tile — log and skip.
            import logging
            logging.getLogger("cma.plots").warning(
                "[pack:%s] plot %s failed to ingest: %s",
                att.get("pack_id", "?"), plot_id, e,
            )


# Sample fields by data source (used by the designer when no dataset is bound)
SAMPLE_FIELDS = {
    "ds-snowflake-prod": [
        "month", "quarter", "as_of_date", "product_type", "sector",
        "nav", "market_value", "book_yield", "oas_bps", "oad_years",
        "eve_pct", "shock_bp", "var_99", "svar",
    ],
    "ds-onelake-finance": [
        "month", "segment", "revenue", "opex", "ppnr",
        "plan_revenue", "plan_opex", "variance",
    ],
}


# Reporting-tab tiles come exclusively from pack attachments now (see
# `_ingest_pack_plots()`). The previous import-time seed of a synthetic
# `plot-nav-trend` was removed so the Reporting tab opens with only the
# user's pack-bundled and user-created tiles.


# ── helpers ─────────────────────────────────────────────────────────────────
def _df_records(df: pd.DataFrame, n: int = 100) -> list[dict[str, Any]]:
    out = df.head(n).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].astype(str)
    return out.to_dict(orient="records")


def _aggregate(df: pd.DataFrame, x: str, ys: list[str], how: str) -> pd.DataFrame:
    if how == "none":
        return df
    if x not in df.columns:
        return df
    available_ys = [y for y in ys if y in df.columns]
    if not available_ys:
        return df
    agg_map = {y: how if how != "avg" else "mean" for y in available_ys}
    grouped = df.groupby(x, as_index=False).agg(agg_map)
    return grouped


def _compute_kpi(df: pd.DataFrame, p) -> dict[str, Any] | None:
    """Reduce a (filtered) dataframe to a single KPI value + formatted
    display string per the tile's KPI config. Returns None on bad config."""
    field = p.kpi_field
    if not field or field not in df.columns:
        return None
    agg = p.kpi_aggregation
    try:
        if agg == "weighted_avg":
            wf = p.kpi_weight_field
            if not wf or wf not in df.columns:
                return None
            joined = pd.DataFrame({
                "v": pd.to_numeric(df[field], errors="coerce"),
                "w": pd.to_numeric(df[wf], errors="coerce"),
            }).dropna()
            if joined.empty or joined["w"].sum() == 0:
                return None
            value = float((joined["v"] * joined["w"]).sum() / joined["w"].sum())
        elif agg == "latest":
            sort_col = p.kpi_latest_field or field
            if sort_col not in df.columns:
                return None
            sorted_df = df.sort_values(sort_col, ascending=False)
            non_null = pd.to_numeric(sorted_df[field], errors="coerce").dropna()
            if non_null.empty:
                return None
            value = float(non_null.iloc[0])
        else:
            series = pd.to_numeric(df[field], errors="coerce").dropna()
            if series.empty:
                return None
            if agg == "sum":     value = float(series.sum())
            elif agg == "avg":   value = float(series.mean())
            elif agg == "min":   value = float(series.min())
            elif agg == "max":   value = float(series.max())
            elif agg == "count": value = float(series.count())
            else: return None
    except Exception:
        return None

    scale = p.kpi_scale if p.kpi_scale else 1.0
    decimals = max(0, min(6, int(p.kpi_decimals or 0)))
    scaled = value * scale
    formatted = f"{scaled:,.{decimals}f}"
    display = f"{p.kpi_prefix}{formatted}{p.kpi_suffix}"
    return {
        "value": value,
        "scaled": scaled,
        "display": display,
        "label": p.name,
        "sublabel": p.kpi_sublabel,
    }


def _apply_filters(df: pd.DataFrame, filters: list[dict[str, Any]]) -> pd.DataFrame:
    """Apply structured filter dicts of shape {field, op, value}.

    Tolerates a few common LLM/UI shape variations so a tile with the
    intended filter still renders correctly:
      • Accept `column` or `name` as aliases for `field`.
      • Coerce the column to string for `eq`/`ne`/`in` so `"BHCB"` matches
        a column read as `object` even if the value side is non-string.
    Logs at WARNING level when a filter is skipped — visible in the
    backend log so we can see why a tile's data didn't change.
    """
    if not filters or df is None or df.empty:
        return df
    out = df
    for f in filters:
        field = f.get("field") or f.get("column") or f.get("name")
        op = f.get("op", "eq")
        value = f.get("value")
        if not field:
            log.warning("plot filter skipped — missing field key: %s", f)
            continue
        if field not in out.columns:
            log.warning(
                "plot filter skipped — field %r not in dataframe columns %s",
                field, list(out.columns),
            )
            continue
        col = out[field]
        before = len(out)
        try:
            if op == "eq":
                # Coerce both sides to string so numeric/object dtype mismatches
                # don't silently drop the whole filter.
                mask = col.astype(str) == str(value)
            elif op == "ne":
                mask = col.astype(str) != str(value)
            elif op == "in":
                vals = value if isinstance(value, list) else [value]
                mask = col.astype(str).isin([str(v) for v in vals])
            elif op == "gt":    mask = col > value
            elif op == "gte":   mask = col >= value
            elif op == "lt":    mask = col < value
            elif op == "lte":   mask = col <= value
            elif op == "contains":
                mask = col.astype(str).str.contains(str(value), case=False, na=False)
            else:
                log.warning("plot filter skipped — unknown op %r in %s", op, f)
                continue
            out = out[mask]
            after = len(out)
            log.info(
                "plot filter applied %s %s %r → %d → %d rows",
                field, op, value, before, after,
            )
        except Exception as e:
            log.warning("plot filter raised %s for %s — skipped", e, f)
            continue
    return out


# ── routes ──────────────────────────────────────────────────────────────────
@router.get("", response_model=list[PlotConfig])
async def list_plots(
    function_id: str | None = Query(default=None),
    pinned: bool | None = Query(default=None),
    _: str = Depends(get_current_user),
):
    items = list(_PLOTS.values())
    if function_id:
        items = [p for p in items if p.function_id == function_id]
    if pinned is not None:
        items = [p for p in items if p.pinned_to_overview == pinned]
    return items


@router.post("", response_model=PlotConfig, status_code=201)
async def create_plot(req: PlotConfigCreate, _: str = Depends(get_current_user)):
    pid = f"plot-{uuid.uuid4().hex[:10]}"
    p = PlotConfig(id=pid, **req.model_dump())
    _PLOTS[pid] = p
    return p


@router.delete("/{plot_id}", status_code=204)
async def delete_plot(plot_id: str, _: str = Depends(get_current_user)):
    if plot_id not in _PLOTS:
        raise HTTPException(status_code=404, detail="Plot not found")
    del _PLOTS[plot_id]


@router.post("/{plot_id}/pin", response_model=PlotConfig)
async def toggle_pin(plot_id: str, _: str = Depends(get_current_user)):
    p = _PLOTS.get(plot_id)
    if not p:
        raise HTTPException(status_code=404, detail="Plot not found")
    p.pinned_to_overview = not p.pinned_to_overview
    return p


@router.post("/{plot_id}/filters", response_model=PlotConfig)
async def update_filters(
    plot_id: str,
    payload: dict[str, Any],
    _: str = Depends(get_current_user),
):
    """Replace the tile's filters wholesale, or append a single filter.

    Body shape:
      { "filters": [...] }                    — replace
      { "append": {"field": "...", ...} }     — add one
      { "clear": true }                       — wipe all
    """
    p = _PLOTS.get(plot_id)
    if not p:
        raise HTTPException(status_code=404, detail="Plot not found")
    if payload.get("clear"):
        p.filters = []
    elif payload.get("append"):
        p.filters = list(p.filters) + [payload["append"]]
    elif "filters" in payload:
        p.filters = list(payload["filters"] or [])
    return p


@router.get("/fields")
async def get_fields(
    data_source_id: str | None = None,
    dataset_id: str | None = None,
    run_id: str | None = None,
    _: str = Depends(get_current_user),
):
    if dataset_id:
        d = load_dataset(dataset_id)
        if not d:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return {"fields": [c.name for c in d.columns]}
    if run_id:
        run = load_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.series:
            return {"fields": list(run.series[0].keys())}
        return {"fields": []}
    return {"fields": SAMPLE_FIELDS.get(data_source_id or "", SAMPLE_FIELDS["ds-snowflake-prod"])}


@router.get("/{plot_id}/preview")
async def preview_plot(plot_id: str, _: str = Depends(get_current_user)):
    p = _PLOTS.get(plot_id)
    if not p:
        raise HTTPException(status_code=404, detail="Plot not found")

    is_table = p.tile_type == "table"
    is_kpi = p.tile_type == "kpi"
    row_cap = 1000 if is_table else 200  # tables show more rows; plots aggregate

    # Pull data from the configured source
    rows: list[dict[str, Any]] | None = None
    columns_meta: list[dict[str, str]] | None = None
    kpi_payload: dict[str, Any] | None = None

    if p.dataset_id:
        d = load_dataset(p.dataset_id)
        if d and d.source_kind == "upload" and d.file_path and d.file_format:
            try:
                df = _read_dataframe(_resolve_path(d), d.file_format)
                df = _apply_filters(df, p.filters)
                if is_kpi:
                    kpi_payload = _compute_kpi(df, p)
                    rows = []  # KPI tiles don't render rows
                else:
                    if not is_table:
                        df = _aggregate(df, p.x_field, p.y_fields, p.aggregation)
                    rows = _df_records(df, row_cap)
                columns_meta = [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns]
            except Exception:
                rows = None
    if rows is None and p.run_id:
        run = load_run(p.run_id)
        if run and run.series:
            df = pd.DataFrame(run.series)
            df = _apply_filters(df, p.filters)
            if is_kpi:
                kpi_payload = _compute_kpi(df, p)
                rows = []
            else:
                if not is_table:
                    df = _aggregate(df, p.x_field, p.y_fields, p.aggregation)
                rows = _df_records(df, row_cap)
            columns_meta = [{"name": c, "dtype": str(df[c].dtype)} for c in df.columns]

    if rows is not None:
        result = {
            "plot": p, "preview_data": rows, "columns": columns_meta or [], "source": "live",
        }
        if is_kpi:
            result["kpi"] = kpi_payload or {
                "value": None, "scaled": None,
                "display": f"{p.kpi_prefix}—{p.kpi_suffix}",
                "label": p.name, "sublabel": p.kpi_sublabel,
            }
        return result

    if is_kpi:
        # No live data — return a placeholder KPI so the tile still renders.
        return {
            "plot": p, "preview_data": [], "columns": [], "source": "sample",
            "kpi": {
                "value": None, "scaled": None,
                "display": f"{p.kpi_prefix}—{p.kpi_suffix}",
                "label": p.name, "sublabel": p.kpi_sublabel,
            },
        }

    # Fall back to a synthetic sample
    if p.chart_type in ("line", "area", "bar", "stacked_bar"):
        sample = [
            {p.x_field: m, **{f: round(100 + i * 12 + j * 8, 2) for j, f in enumerate(p.y_fields)}}
            for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun"])
        ]
    elif p.chart_type == "pie":
        sample = [
            {p.x_field: c, p.y_fields[0]: v}
            for c, v in [("Cat A", 38), ("Cat B", 27), ("Cat C", 18), ("Cat D", 17)]
        ]
    elif p.chart_type == "scatter":
        sample = [
            {p.x_field: i, p.y_fields[0]: round((i % 5) * 1.4 + 0.6, 2)}
            for i in range(1, 21)
        ]
    else:
        sample = []
    sample_cols = list(sample[0].keys()) if sample else []
    return {
        "plot": p, "preview_data": sample,
        "columns": [{"name": c, "dtype": "object"} for c in sample_cols],
        "source": "sample",
    }

# Workflow Orchestrator + Proxy-Env Setup

> For the design rationale behind the workflow engine — why validation runs
> before execution, why models execute in isolation, why compiling and
> deploying are separated — see
> [SOLUTION-ARCHITECTURE.html](SOLUTION-ARCHITECTURE.html) §4 and §6.5. This
> document is the step-by-step mechanics and the proxy-environment runbook.

This document covers two things:

1. **How the workflow orchestrator works** end-to-end — the journey from
   "user clicks Run Workflow" to "rows download as a CSV", with code
   references at every step. Includes the canvas's draft persistence,
   the saved-workflow CRUD that backs Save / Load / Delete, and the
   model→model fan-in pattern (RDMaaS / CommMaaS / SBBMaaS → NII
   Calculator).

2. **What to wire up in the corporate proxy environment** so that the
   demo's static fallbacks are replaced with live data: the OneLake
   extractor for the macro-scenario dataset + CCAR/Outlook scenarios,
   `pa-common-tools` for the Data Harness ETL and Data Quality Check,
   and the CCAR data file shape your extractor should produce.

---

## Part 1 — How the orchestrator works

### Inputs

The workflow tab posts a single request to
[`POST /api/analytics/workflow-runs`](../backend/routers/scenarios.py)
([`create_workflow_run`](../backend/routers/scenarios.py)). The body
shape (defined as
[`WorkflowRequest`](../backend/models/schemas.py)):

```jsonc
{
  "function_id": "capital_planning",
  "horizon_months": 12,
  "scenario_name": "BHCS 2026",      // optional — picked from the run-controls dropdown
  "start_date":    "2026-04-01",     // optional — picked from the run-controls calendar
  "nodes": [
    {"id":"h", "kind":"transform",   "ref_id":"tr-deposits-data-harness", "config":{...}},
    {"id":"q", "kind":"transform",   "ref_id":"tr-deposits-dqc",          "config":{...}},
    {"id":"r", "kind":"model",       "ref_id":"mdl-deposits-rdmaas",      "config":{}},
    {"id":"o", "kind":"destination", "ref_id":"csv",                      "config":{"filename":"out.csv"}}
  ],
  "edges": [
    {"source":"h", "target":"q"},
    {"source":"q", "target":"r"},
    {"source":"r", "target":"o"}
  ]
}
```

Five node kinds are recognized: `dataset`, `scenario`, `transform`,
`model`, `destination`. Each carries an opaque `config` dict the
frontend uses for per-node settings (e.g. the Data Harness inspector's
multi-selects land in `config.read_dataset_ids` and
`config.requirement_model_ids`).

### Step 1 — Topological sort

Kahn's algorithm in
[`_topological_sort`](../backend/routers/scenarios.py) returns the
nodes in dependency order. Cycles or edges referencing missing nodes
raise `400`. The sort is kind-agnostic — every kind is treated as a
plain DAG node.

### Step 2 — Walk the order

`create_workflow_run` walks the sorted list:

| Node kind     | What the loop does                                                  |
|---------------|---------------------------------------------------------------------|
| `dataset`     | Marks `node_status[id] = "completed"`. Resolved on demand by `_resolve_input_frame`. |
| `scenario`    | Same — resolved on demand.                                          |
| `transform`   | Same — resolved on demand. Lazy resolution lets DQC see its upstream's frame. |
| `destination` | For each completed upstream model run, `_write_destination` records / serializes. CSV with multiple inputs uses `_write_csv_combined` to coalesce into one long-format file with a `segment` column. |
| `model`       | Resolves frames for each incoming node, merges, runs the model, records an `AnalyticsRun`. |

### Step 3 — Resolving input frames

The model branch is the only place that actually pulls data through the
graph.
[`_resolve_input_frame`](../backend/routers/scenarios.py) is the
recursive resolver. Per node kind:

- **`dataset`**: `_input_dataframe(dataset_id=ref_id)` — reads the
  staged file from `backend/data/datasets/<function_id>/<dataset_id>.<ext>`.
- **`scenario`**: `_input_dataframe(scenario_id=ref_id)` — reads from
  `BUILTIN_DATA[id]["paths"]` (a wide-format dict of variable → list).
  For materialized CCAR + Outlook scenarios this dict is populated at
  startup by `services.data_services.materialize_into_scenarios_registry`.
- **`model`**: returns `pd.DataFrame(upstream_outputs[node.id])` — the
  prior step's output cached by id. This is what powers **model →
  model fan-in** (e.g. `RDMaaS / CommMaaS / SBBMaaS → NII Calculator`).
  Each MaaS emits **segment-prefixed columns** (`rdmaas_rate_pct`,
  `rdmaas_balance_mm`, `commmaas_*`, `sbbmaas_*`) so when the three
  frames merge into the NII Calculator's input, the columns don't
  collide and the calculator's `feature_names` resolves all six rate
  + balance columns from one wide row per month.
- **`transform`**:
  1. If the transform has incoming edges (e.g. *Data Harness → DQC*),
     resolve each upstream recursively and merge them.
  2. Otherwise the transform is a *source* — read either:
     - The merged frame of any datasets the analyst picked in the
       inspector's "Read data from" multi-select
       (`config.read_dataset_ids`), or
     - The transform's static `output_dataset_id`.
  3. Apply [`_apply_run_context`](../backend/routers/scenarios.py) —
     overlay the chosen scenario's macro path onto matching columns and
     re-anchor `as_of_date` to `start_date`.
  4. Run the transform's recipe via
     [`_execute_transform`](../backend/routers/scenarios.py). The DQC
     transform is dispatched to
     [`_run_dqc`](../backend/routers/scenarios.py) (real or stub);
     other transforms pass through (their materialization happened at
     step 2).
  5. Validate via [`_apply_node_config`](../backend/routers/scenarios.py)
     — fail the run with `FEATURE_MISMATCH` if the inspector's "Match
     requirements of" models declare features the materialized output
     doesn't carry.

### Step 4 — Merge frames

[`_merge_frames`](../backend/routers/scenarios.py) outer-joins the
resolved frames on `month` (or row index when absent), drops duplicate
columns, and forward/backfills nulls. The merged frame is then
truncated to `req.horizon_months`.

### Step 5 — Apply the model

[`_apply_model`](../backend/routers/scenarios.py) routes:

- **Regression / logistic** (`coefficients` set on the `TrainedModel`)
  — applied directly in-process.
- **Pickled/joblib/ONNX** (`source_kind="upload"`) — handed off to the
  sandboxed model runner in
  [`services/model_runner.py`](../backend/services/model_runner.py).
  The runner spawns a fresh subprocess (the venv's Python), writes the
  feature DataFrame + artifact metadata to a temp JSON, runs the
  inline runner script, and reads the predictions back. Wall-clock
  timeout is 30 s by default.
- The runner is permissive about the loaded artifact: it tries
  `model.predict(X)` first, then `model.predict_proba(X)` for
  probability vectors, then a bare `model(X)` if the artifact is a
  callable / bound method. This lets pip-installed corporate libraries
  expose any of those shapes.

### Step 6 — Post-process predictions

[`_post_process`](../backend/routers/scenarios.py) reshapes the raw
model output into the per-row series the canvas + Reporting tab
consume. Branches on `output_kind`:

| `output_kind`         | Input shape       | Output rows                                              |
|-----------------------|-------------------|----------------------------------------------------------|
| `scalar`              | `(n,)`            | `{month, prediction, ...input_cols}`                     |
| `probability_vector`  | `(n, classes)`    | `{month, p_<class>... , prediction=argmax_label}`        |
| `n_step_forecast`     | `(steps,)`        | `{month, step, prediction}`                              |
| `multi_target`        | `(n, targets)`    | `{month, <target_1>, ..., <target_k>, prediction=<t1>}`  |

### Step 7 — Record the run

A `run-<uuid>` is written into the in-memory `_RUNS` dict with the
series, summary, status, and notes prefixed with the run-context (e.g.
`[scenario: BHCS 2026 · start: 2026-04-01]`). The model node's status
flips to `completed` (or `failed`).

### Step 8 — Destinations

After every model node, the loop revisits destination nodes. The
single CSV destination supports two modes:

- **One upstream model** → `_write_destination` returns one
  `DestinationWrite` carrying that model's series.
- **N upstream models (fan-in)** →
  [`_write_csv_combined`](../backend/routers/scenarios.py) walks every
  completed upstream run, prepends a `segment` column derived from the
  model's name, and emits a single long-format CSV the browser
  downloads.

### Step 9 — Error classification

Failures don't escape the orchestrator as raw 500s. The model branch
wraps both frame resolution and the model call in one try/except. On
HTTPException the structured `detail` dict is preserved; otherwise
the plain message is wrapped. Both paths feed
[`_classify_run_error`](../backend/routers/scenarios.py) which maps
known substrings to short codes (`FEATURE_MISMATCH`, `DQC_FAILED`,
`TIMEOUT`, `PICKLE_CLASS_MISSING`, `OUTPUT_PARSE`, …) and a
one-line user-facing hint. The result is attached to the
`WorkflowResult.error_detail` field; the frontend
`WorkflowRunErrorCard` renders it directly.

### Validation (separate endpoint)

`POST /api/analytics/workflow-validate` routes through
[`chat_validation.validate_workflow_payload`](../backend/routers/chat_validation.py)
— the same checks that catch a missing input or a model-feature
mismatch *before* the run, surfacing them in the canvas's Validate
panel. Used by the Validate button on the Workflow tab.

### Saved workflows + draft persistence

The Workflow tab has two independent persistence layers — one for the
in-progress canvas, one for named saves the analyst chooses to keep.

**1. In-progress draft (frontend, automatic).** AnalyticsTab serializes
the canvas state — nodes, edges, horizon, scenario name, start date,
view mode, plus the loaded saved-workflow id — to
`localStorage["cma:workflow:draft:<function_id>"]` on every change.
On mount it reads back from the same key, so a tab switch (which
unmounts the component) or a hard reload restores exactly what the
user was looking at. The Clear button explicitly removes the entry so
"start fresh" actually starts fresh.

**2. Named saves (backend CRUD).** A separate registry of analyst-named
workflows. Endpoints under `/api/analytics/workflows`:

| Verb     | Path                                | Purpose                          |
|----------|-------------------------------------|----------------------------------|
| `GET`    | `/api/analytics/workflows`          | List summaries (filter by `function_id`) |
| `GET`    | `/api/analytics/workflows/{id}`     | Full record (nodes + edges + run config) |
| `POST`   | `/api/analytics/workflows`          | Save a new record                |
| `PUT`    | `/api/analytics/workflows/{id}`     | Update an existing record        |
| `DELETE` | `/api/analytics/workflows/{id}`     | Delete                           |

Storage is in-memory (`_SAVED_WORKFLOWS` dict in
[`scenarios.py`](../backend/routers/scenarios.py)), consistent with
the rest of the demo — survives until backend restart. Schemas:
[`SavedWorkflow`](../backend/models/schemas.py),
[`SavedWorkflowCreate`](../backend/models/schemas.py),
[`SavedWorkflowSummary`](../backend/models/schemas.py).

The Save / Load / Delete UI in the Workflow run controls (a Bookmark
icon dropdown) drives all four endpoints. Loading replaces the canvas
state in one shot; the loaded record id is held in component state so
subsequent **Save** clicks `PUT`-update in place. **Save as…** forces a
new record. **Clear** detaches from the loaded record so the next save
creates a new one.

---

## Part 2 — Setting up the proxy environment

Three integrations to flip on. Each is independently configurable;
fallbacks let the demo continue to work if any are off.

### 2.1 OneLake extractor — the foundation

Both the *Macro scenario from OneLake* dataset and the CCAR + Outlook
scenarios route through one stub function in your codebase:
[`services.data_services._onelake_read_table`](../backend/services/data_services.py).
Outside the proxy environment it raises `NotImplementedError` and
callers fall back to static specs.

**Implementation recipe** — replace the stub body with your corporate
extractor. The signature:

```python
def _onelake_read_table(table: str, **filters: Any) -> list[dict[str, Any]]:
    from your_corp_lib import OneLakeExtractor
    client = OneLakeExtractor(
        workspace=ONELAKE_WORKSPACE,        # default "Finance" (env: CMA_ONELAKE_WORKSPACE)
        lakehouse=ONELAKE_LAKEHOUSE,        # default "cma"     (env: CMA_ONELAKE_LAKEHOUSE)
    )
    # Sync interface; if your client is async, wrap with asyncio.run(...).
    return client.read_table(table_name=table, **filters)
```

**Env knobs** (full list in
[`backend/config/data_services.example.env`](../backend/config/data_services.example.env)):

```bash
CMA_ONELAKE_SCENARIOS_ENABLED=1
CMA_ONELAKE_WORKSPACE=Finance
CMA_ONELAKE_LAKEHOUSE=cma
CMA_ONELAKE_CCAR_TABLE=ccar_scenarios
CMA_ONELAKE_OUTLOOK_TABLE=outlook_scenarios
```

**Expected row shapes** the loaders read from each table:

| Table                | Required columns                                                |
|----------------------|-----------------------------------------------------------------|
| `ccar_scenarios`     | `year`, `code`, `label`, `severity`, `source`, `description`    |
| `outlook_scenarios`  | `id`, `title`, `subtitle`, `description` (+ optional `color`, `icon`, `tag`, `agent_prompt`) |
| `macro_scenario`     | `as_of_date` + every column the Data Harness needs to feed the deposit MaaS classes (rates, deposit pricing, account info, treasury demand, merchant volume) |

The first two are consumed by
[`materialize_into_scenarios_registry`](../backend/services/data_services.py)
at startup → `_BUILTIN_SCENARIOS` + `BUILTIN_DATA` get populated → the
Workflow tab's Scenarios palette + the Run-controls scenario dropdown
+ the Data tab's Data Services CCAR / Outlook sections all light up
with live values. The integration-status badges flip to
`live · onelake` (green) when `_onelake_read_table` returns rows, or
`onelake · fallback` (amber) on any failure with a hover-detail
explaining why.

### 2.2 Wiring OneLake to the *Macro scenario* dataset

The deposits pack attaches `ds-onelake-macro-scenario` to Capital
Planning & CCAR (see
[`backend/packs/deposits/pack.py`](../backend/packs/deposits/pack.py)).
For the demo it's backed by a staged CSV at
`sample_data/deposits/data_harness.csv` so workflows run end-to-end
without proxy connectivity.

In the proxy environment, replace the file-backed read with a call to
your extractor. The lightest-touch path: a startup hook that runs
right after pack ingestion, calls the extractor, and overwrites the
staged file. Add to
[`backend/main.py`'s `_ingest_pack_assets`](../backend/main.py):

```python
@app.on_event("startup")
async def _refresh_macro_scenario_from_onelake():
    """In the proxy env, refresh the Macro scenario dataset from OneLake
    on every startup. Outside the proxy env this is a no-op."""
    if not data_services.ONELAKE_SCENARIOS_ENABLED:
        return
    try:
        rows = data_services._onelake_read_table("macro_scenario")
        if not rows:
            return
        import pandas as pd
        df = pd.DataFrame(rows)
        from routers.datasets import _resolve_path, load_dataset
        d = load_dataset("ds-onelake-macro-scenario")
        if d:
            df.to_csv(_resolve_path(d), index=False)
            print(f"[startup] refreshed Macro scenario from OneLake — {len(df)} rows")
    except Exception as e:
        print(f"[startup] OneLake macro-scenario refresh failed: {e}")
```

This keeps the same disk-read code path the orchestrator already uses
(`_input_dataframe → _read_dataframe → _resolve_path`), so no other
file needs to change. The Data Harness transform then reads
`ds-onelake-macro-scenario` and applies the run-context overlay
(scenario + start_date) on top.

A more elegant alternative: change the dataset's `source_kind` to a
new `"onelake"` literal and intercept `_input_dataframe` to call the
extractor directly. That's a bigger change; the file-staging hook
above is enough for v1.

### 2.3 Using `pa-common-tools` for Data Quality Check

Already wired. [`_run_dqc`](../backend/routers/scenarios.py) tries:

```python
from pa_common_tools import DataQualityCheck
checker = DataQualityCheck(min_rows=..., max_null_rate=...)
return checker.run(df)   # or checker(df) — both supported
```

If the import succeeds, the corporate class runs and any exception it
raises propagates back as a structured `DQC_FAILED` workflow error.
If the import fails, a built-in stub runs the same row-count + null-rate
checks declared on the DQC card so the demo still has a working gate.

**To enable**:

```bash
cd backend
uv add pa-common-tools                  # or `uv pip install pa-common-tools`
```

The transform recognizes itself as DQC by id pattern
(`tr-...-dqc`) or by `DataQualityCheck` appearing in `recipe_python`.
Multiple DQC cards on the canvas all route through the same executor.

**Optional contract** — to make `DataQualityCheck` carry richer error
context, expose:

```python
class DataQualityCheck:
    def __init__(self, *, min_rows=12, max_null_rate=0.05, **kwargs): ...
    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        # Raise on failure; return df on pass.
        ...
```

Whatever the class raises becomes the workflow's
`error_detail.what_happened`; the frontend run-error card shows it
verbatim with the `code: DQC_FAILED` tag.

### 2.4 Using `pa-common-tools` for the Data Harness

The Data Harness transform today is a **passthrough** — its `recipe_python`
text is informational, and the orchestrator just resolves it to the
materialized `output_dataset_id`. To make it actually call
`pa_common_tools.DataHarness` at run time, extend
[`_execute_transform`](../backend/routers/scenarios.py) with a Harness
dispatcher (mirror what DQC does):

```python
def _execute_transform(transform, df, params):
    if "dqc" in transform.id.lower() or "DataQualityCheck" in (transform.recipe_python or ""):
        return _run_dqc(df, params)
    if "harness" in transform.id.lower() or "DataHarness" in (transform.recipe_python or ""):
        return _run_harness(df, params)
    return df


def _run_harness(df, params):
    """Production path: pa_common_tools.DataHarness. Stub falls back to
    the input frame unchanged so the demo keeps working."""
    try:
        import importlib
        importlib.invalidate_caches()
        mod = importlib.import_module("pa_common_tools")
        cls = getattr(mod, "DataHarness", None)
        if cls is not None:
            harness = cls(**(params or {}))
            run = getattr(harness, "run", None)
            if callable(run):
                return run(df)
            if callable(harness):
                return harness(df)
    except ImportError:
        pass
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Data Harness failed.",
                "error": f"pa_common_tools.DataHarness raised: {e}",
                "hint": "Inspect the failing call inside the harness library.",
            },
        )
    return df  # demo passthrough
```

Then add a corresponding pattern entry in `_RUN_ERROR_PATTERNS`:

```python
("DataHarness", "HARNESS_FAILED",
 "Inspect the harness library logs, or relax its parameters."),
```

In production this means: the orchestrator hands the OneLake-backed
DataFrame to `pa_common_tools.DataHarness.run(df)`, the library's join
+ feature-engineering logic runs, and the returned DataFrame flows
into the next transform / model. Outside the proxy env the demo still
works because the pass-through still returns the input frame.

The Data Harness inspector's per-node config (`read_dataset_ids`,
`requirement_model_ids`) is read in this exact order:

1. **`read_dataset_ids`** chooses the input frame *before*
   `_apply_run_context` and `_run_harness` run.
2. **Run controls** (`scenario_name`, `start_date`) overlay onto that
   frame.
3. **`pa_common_tools.DataHarness`** runs.
4. **`requirement_model_ids`** validates the harness output has every
   feature each selected model needs — fails the run with
   `FEATURE_MISMATCH` if not.

### 2.5 Verification

After flipping each integration on:

```bash
# 1. CCAR + Outlook live status
TOKEN=$(curl -sS -X POST http://127.0.0.1:8001/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"pqr557","password":"capital1"}' \
  | jq -r .token)
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8001/api/data_services?function_id=capital_planning" \
  | jq '.predictive_status, .ccar_status, .outlook_status'

# 2. Macro scenario dataset row count
curl -sS -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8001/api/datasets/ds-onelake-macro-scenario/preview?n=5" \
  | jq '.total_rows, .columns | map(.name)'

# 3. Run a workflow with DQC inline; force pa_common_tools error to
#    confirm it propagates as DQC_FAILED with the expected message.
```

The Data tab's *Data Services* section badges (`static` / `live ·
pa_common_tools` / `live · onelake` / `<integration> · fallback`)
mirror this state at a glance.

### 2.6 The CCAR scenario data files

In addition to the *Macro scenario* dataset (which feeds the Data
Harness ETL), the deposits pack ships two CCAR-specific data files
under `sample_data/ccar/`. These power the Reporting tab's CCAR
comparison tiles and serve as the data shape your OneLake CCAR table
should produce.

**`ccar_scenarios.csv`** — wide format, 20 rows × 51 columns.

| Column                 | Type   | Notes                                       |
|------------------------|--------|---------------------------------------------|
| `cycle`                | string | `CCAR25` (start Q4-2024) or `CCAR26` (start Q4-2025) |
| `pq`                   | string | `PQ0`..`PQ9` — 10 quarters per cycle        |
| `period`               | string | calendar quarter label, e.g. `2024Q4`       |
| `<scenario>_<macro>`   | float  | for each (BHCB / BHCS / FedB / FedSA) × 12 macros |

The 12 macros: `unemployment_rate_pct`, `employment_growth_yoy_pct`,
`hpi_yoy_pct`, `cre_price_yoy_pct`, `gdp_qoq_annl_pct`, `m2_billions`,
`oil_price_usd_bbl`, `fed_funds_pct`, `ust_1y_pct`, `ust_10y_pct`,
`bbb_spread_bps`, plus `m2_to_gdp_ratio` (precomputed, since the plot
tile machinery has no run-time arithmetic).

The wide format is required by the chart renderer — multi-line plots
come from multiple `y_fields`, not from a `group_by`. Two-line BHCB-vs-BHCS
plots use `y_fields=["bhcb_<x>", "bhcs_<x>"]`; the `style.palette`
field on each tile pins the order: `["#1E3A8A", "#DC2626"]` — dark
blue for BHCB, red for BHCS.

**`ccar_peak_summary.csv`** — long format, 9 rows × 5 columns. One row
per macro variable; columns are `macro`, `bhcb_25`, `bhcb_26`,
`bhcs_25`, `bhcs_26`. Each cell is the **worst-case (peak) value** of
that macro over PQ0–PQ9 — `max` for unemployment + BBB spread, `min`
for everything else (rate troughs, GDP trough, HPI/CRE/oil declines,
employment contraction). M2 and 1-year UST are intentionally excluded
per the comparison-table spec.

Both files are regenerated by `python sample_data/ccar/generate.py`.
The trajectories are deterministic shape functions per scenario type
(BHCB/FedB = mild expansion glide, BHCS = recession with V-trough at
PQ3-5, FedSA = deep recession + ZLB rates), anchored to plausible
Q4-2024 / Q4-2025 starting values so the cycle anchor is visible at PQ0.

When OneLake is enabled, the CCAR table your extractor returns can
have either layout — long format with one row per (year, scenario,
pq, macro) is the natural OneLake shape, but the workbench currently
expects the wide CSV at the dataset path. The cleanest production
path: have the OneLake refresh hook (section 2.2) pivot the long-form
extractor result to wide format before writing the CSV.

### 2.7 Order of operations in the proxy env

The minimum viable rollout:

1. Implement `_onelake_read_table` (one function body).
2. Set `CMA_ONELAKE_SCENARIOS_ENABLED=1` and the four
   workspace/lakehouse/table env vars.
3. Drop in the `_refresh_macro_scenario_from_onelake` startup hook
   (15 lines).
4. `uv add pa-common-tools` to the backend venv.
5. Add the `_run_harness` dispatcher (30 lines) if you want the
   harness to actually compute features at run time rather than read
   pre-staged ones.
6. Restart the backend.

Steps 1-4 give you live data flowing through the canvas with the
pa-common-tools DQC enforcing quality. Step 5 makes the harness
itself dynamic. None of these steps changes any router, schema, or
loader — every integration point is a single function in
`scenarios.py` or `data_services.py` plus an env toggle.

---

## Code references — quick index

| Area                              | File                                                            |
|-----------------------------------|-----------------------------------------------------------------|
| Workflow run endpoint             | `backend/routers/scenarios.py: create_workflow_run`             |
| Topological sort                  | `backend/routers/scenarios.py: _topological_sort`               |
| Recursive frame resolver          | `backend/routers/scenarios.py: _resolve_input_frame`            |
| Run-context overlay               | `backend/routers/scenarios.py: _apply_run_context`              |
| Per-node config check             | `backend/routers/scenarios.py: _apply_node_config`              |
| Transform dispatcher              | `backend/routers/scenarios.py: _execute_transform`              |
| DQC executor                      | `backend/routers/scenarios.py: _run_dqc`                        |
| Run-error classifier              | `backend/routers/scenarios.py: _classify_run_error`             |
| Combined-CSV destination          | `backend/routers/scenarios.py: _write_csv_combined`             |
| Saved-workflow CRUD               | `backend/routers/scenarios.py: list_saved_workflows / get_saved_workflow / save_workflow / update_saved_workflow / delete_saved_workflow` |
| Saved-workflow schemas            | `backend/models/schemas.py: SavedWorkflow / SavedWorkflowCreate / SavedWorkflowSummary` |
| Sandboxed model runner            | `backend/services/model_runner.py`                              |
| OneLake extractor stub            | `backend/services/data_services.py: _onelake_read_table`        |
| Scenario materialization hook     | `backend/services/data_services.py: materialize_into_scenarios_registry` |
| Data Services REST                | `backend/routers/data_services.py`                              |
| Validator                         | `backend/routers/chat_validation.py`                            |
| CCAR scenario data + generator    | `sample_data/ccar/ccar_scenarios.csv`, `sample_data/ccar/ccar_peak_summary.csv`, `sample_data/ccar/generate.py` |
| Deposit-suite model classes       | `sample_models/deposits/_classes.py` (RDMaaS / CommMaaS / SBBMaaS / NIICalculator) |
| Pack registration                 | `backend/packs/deposits/pack.py`                                |
| Frontend Workflow tab             | `frontend/src/pages/Workspace/AnalyticsTab.tsx`                 |
| Frontend Saved Workflows menu     | `frontend/src/pages/Workspace/AnalyticsTab.tsx: SavedWorkflowsMenu` |
| Frontend draft localStorage key   | `cma:workflow:draft:<function_id>`                              |
| Frontend Data Services section    | `frontend/src/pages/Workspace/DataServicesSection.tsx`          |

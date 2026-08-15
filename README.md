# CMA Workbench

Self-serve analytics workbench for the **Capital Markets & Analytics**
department. The shape: an analyst lands on a function-selection home page,
opens a workspace for one of 6 business functions, and gets a 7-tab work
surface backed by a multi-agent system. Domain-specific data, models,
agents, and tools ship as **domain packs** so adding a new business area is
a directory drop, not a code-base rewrite.

## Tech stack

- **Frontend** — React 18 · Vite · TypeScript · Tailwind · Radix/shadcn ·
  Recharts · ReactFlow (xyflow) · Zustand · ReactMarkdown
- **Backend** — FastAPI · Uvicorn · Pydantic v2 · pandas · scikit-learn ·
  [openai-agents](https://github.com/openai/openai-agents-python) (the real
  orchestrator — there is no mock fallback)
- **LLM connection** — OpenAI direct (`OPENAI_API_KEY`) or Capital One COF
  endpoint (`COF_BASE_URL`). Chosen at startup; `chat_specialist` raises
  with a clear setup-required message if neither is configured.

## Documentation

| Document | What it covers |
|---|---|
| [docs/SOLUTION-ARCHITECTURE.html](docs/SOLUTION-ARCHITECTURE.html) | **Start here.** What the system is, why it was re-architected, and the reasoning behind each design decision — in plain language, with the architecture diagrams. Open it in a browser. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The AWS implementation reference: exact configuration, service limits that shape the code, deployment order. |
| [docs/ORCHESTRATOR.md](docs/ORCHESTRATOR.md) | Workflow engine mechanics step by step, and the corporate-proxy data integrations. |

## Architecture at a glance

```
cma/
├── backend/
│   ├── main.py                    ← .env → discover packs → mount routers
│   ├── agent/
│   │   ├── tools.py               ← 11 universal "introspection" tools
│   │   │                            (get_workspace, get_dataset_preview,
│   │   │                             validate_workflow, get_tile, …)
│   │   ├── skills/                ← 10 universal built-in skills (.md)
│   │   ├── skills_user/           ← analyst-uploaded skills
│   │   └── skill_loader.py        ← multi-source loader (builtin/user/pack)
│   │
│   ├── packs/                     ← DOMAIN PACKS — opt-in bundles
│   │   ├── __init__.py            ← Pack, PackContext, registry, group filter
│   │   ├── portfolio/             ← first domain pack
│   │   │   ├── pack.py            ← register(ctx) — entry point
│   │   │   ├── skills/            ← 8 portfolio-planning agents (.md)
│   │   │   └── tools.py           ← 20 register_python_tool(...) calls
│   │   └── deposits/              ← deposit-forecasting pack
│   │       └── pack.py            ← attaches Data Harness transform,
│   │                                3 MaaS models, demo dashboard
│   │
│   ├── cof/
│   │   ├── orchestrator.py        ← AsyncOrchestrator (streaming + traces)
│   │   └── base_agent.py          ← CofBaseAgent — wraps one skill via
│   │                                openai-agents SDK; emits per-step trace
│   │
│   ├── models/schemas.py          ← Pydantic v2 wire schemas
│   ├── services/
│   │   ├── model_runner.py        ← sandboxed pkl/joblib/onnx prediction
│   │   ├── data_services.py       ← Data Services aggregation
│   │   │                            (pa_common_tools + OneLake integrations)
│   │   ├── workspace_data.py      ← static Overview-tab fallback specs
│   │   ├── entity_store.py        ← DynamoDB-backed registries + per-request
│   │   │                            memo (falls back to dicts when unset)
│   │   ├── async_jobs.py          ← SQS enqueue for long solves
│   │   ├── corpus_store.py        ← S3 as source of record for documents
│   │   ├── blob_store.py          ← S3 for uploaded datasets + artifacts
│   │   │                            (local disk becomes a cache)
│   │   ├── cognito_auth.py        ← verify Cognito JWTs offline (no session
│   │   │                            store), falls back to mock auth
│   │   ├── workflow_compiler.py   ← canvas → Step Functions definition
│   │   ├── workflow_plans.py      ← compiled-plan registry (audit history)
│   │   ├── workflow_deploy.py     ← ships plans as state machines
│   │   └── workflow_artifacts.py  ← model dirs → S3 for the worker
│   │
│   ├── infra/                     ← AWS deployment — see docs/ARCHITECTURE.md
│   │   ├── cdk/async_stack.py     ← DynamoDB · SQS · Lambda · Step Functions
│   │   │                            · ECR · CodeBuild (one stack)
│   │   ├── cdk/worker_image/      ← container worker (real .pkl artifacts)
│   │   ├── build_worker_image.py  ← push source → CodeBuild → ECR
│   │   ├── deploy_workflows.py    ← privileged deployer for compiled plans
│   │   └── sync_env.py            ← stack outputs → backend/.env
│   │
│   ├── config/
│   │   └── data_services.example.env  ← env-var docs for proxy-env wiring
│   │
│   └── routers/
│       ├── auth.py                ← pqr557 / capital1, group-based access
│       ├── functions.py           ← 6 business functions catalog
│       ├── workspace.py           ← Overview tab default views
│       ├── chat.py                ← /api/chat/message (entity-aware routing)
│       ├── chat_validation.py     ← workflow validator (cycle / refs / features)
│       ├── datasources.py         ← Snowflake / OneLake / S3 / file uploads
│       ├── datasets.py            ← per-function dataset registry
│       ├── models_registry.py     ← per-function model registry
│       │                            (build / upload / from-artifactory)
│       ├── transforms.py          ← per-function ETL transform registry
│       ├── scenarios.py           ← scenarios + analytics + workflow runs
│       ├── data_services.py       ← /api/data_services aggregation endpoint
│       ├── playbooks.py           ← phased agent playbooks (gated runs)
│       ├── analytics_defs.py      ← self-serve analytic definitions
│       ├── plots.py               ← tile / chart builder
│       ├── tools.py               ← Python-tool registry (with /draft endpoint)
│       └── skills.py              ← agent-skill CRUD + tool catalog
│
├── frontend/src/
│   ├── pages/
│   │   ├── Login/                 ← single-account form
│   │   ├── Home/                  ← function picker
│   │   ├── Workspace/             ← 7-tab work surface (see below)
│   │   └── Settings/              ← Data Sources · Agent Skills · Python
│   │                                Tools · Plot Builder
│   ├── components/chat/           ← right-side chat panel
│   ├── store/                     ← Zustand: auth, chat
│   └── types/index.ts             ← API wire types
│
├── sample_data/
│   ├── portfolio/                 ← bundled CSVs the portfolio pack stages
│   └── deposits/                  ← Data Harness output + seeded results
└── sample_models/
    ├── portfolio/                 ← bundled .pkls (BGM, prepayment, etc.)
    └── deposits/                  ← RDMaaS / CommMaaS / SBBMaaS .pkls
```

### Workspace tabs (per function)

| Tab            | What it does                                                   |
|----------------|----------------------------------------------------------------|
| **Overview**   | KPI strip + default charts/tables/insights for the function    |
| **Data**       | Two views: *Datasets* (source bindings + uploads) and *Data Services* — Predictive Analytics built-ins (Data Harness, Data Quality Check), CCAR scenarios with year picker (BHCB/BHCS/FedB/FedSA), Outlook scenarios. Each section shows a `live · pa_common_tools` / `live · onelake` / `static` badge. |
| **Models**     | Build OLS / logistic in-app, upload `.pkl`/`.joblib`/`.onnx`, or **From Artifactory** — `pip install --no-binary <pkg> <pkg>` resolves the package's prediction entry (class with `.predict`/`.forecast`/`.run`/etc., or top-level function), pickles a callable, and registers it. Output shape is auto-detected from class/function metadata. |
| **Workflow**   | Three views over the same graph — *Steps*, *Canvas* (ReactFlow), *Spec* (YAML). Canvas palette has Datasets, Scenarios, Models, Destinations, plus **Transforms** (ETL recipes with a side-panel showing the read-only Python). Click **Validate** for structured issue list (BLOCKER/WARNING/NOTE with hints, click to highlight node). Run-time failures render a code-tagged "What happened / How to fix" card. |
| **Playbooks**  | Author phased agent workflows with gate-by-gate analyst review; trace streams live as the agent calls tools |
| **Analytics**  | Self-serve analytic definitions — *Aggregate*, *Compare*, *Custom Python* primitives + an agent that drafts specs from prose |
| **Reporting**  | Tile/chart catalog over your function's data; click a tile to have the tile-explainer agent narrate it |

### Agent system

Two layers, both real (no mock fallback):

- **Introspection tools** (`agent/tools.py`) — 11 functions the agent calls
  to read live workbench state: `get_workspace`, `get_dataset_preview`,
  `profile_dataset`, `get_model`, `get_model_metrics`, `validate_workflow`,
  `get_run`, `get_tile`, `get_tile_preview`, `apply_tile_filter`,
  `get_function_meta`. Always available, never user-modifiable.

- **Skills** — markdown files with YAML frontmatter (`name`, `description`,
  `model`, `tools`, `color`, `icon`). Loaded from three sources, in this
  override order: built-in → pack → user upload.

  Universal built-ins shipped today: `orchestrator`, `kpi-explainer`,
  `data-quality`, `model-explainer`, `workflow-validator`, `run-troubleshooter`,
  `tile-tuner`, `tile-explainer`, `macro-economist`, `troubleshooter`.

  Portfolio pack skills: `portfolio-gap-analyst`, `portfolio-risk-officer`,
  `new-volume-analyst`, `allocation-strategist`, `mbs-decomposition-specialist`,
  `universe-screener`, `pool-analytics-specialist`, `trade-advisor`.

The router (`routers/chat.py:_route`) maps the chat context (tab +
`entity_kind` + message intent) to a specialist; e.g. clicking *Explain* on
a scenario card routes to `macro-economist`, on a chart tile to
`tile-explainer`, on a model card to `model-explainer`.

The orchestrator (`cof/orchestrator.py:AsyncOrchestrator`) uses
`Runner.run_streamed()` and emits a step-by-step trace
(tool call → tool output → reasoning → final message) that the playbook UI
renders live during a run.

### Domain packs

A pack is a self-contained directory under `backend/packs/<id>/` with one
entry point: `pack.py:register(ctx)`. The registration writes into four
shared registries that the routers consume:

| Registry              | Populated by                                  | Consumer                                     |
|-----------------------|-----------------------------------------------|----------------------------------------------|
| Skill source dirs     | `ctx.register_skill_dir(path)`                | `agent/skill_loader.py`                      |
| Python tool seeds     | `ctx.register_python_tool(...)`               | `routers/tools.py`                           |
| Dataset attachments   | `ctx.attach_dataset(function_id=..., ...)`    | `routers/datasets.py` (startup hook)         |
| Model attachments     | `ctx.attach_model(function_id=..., ...)`      | `routers/models_registry.py` (startup hook)  |
| Transform attachments | `ctx.attach_transform(function_id=..., ...)`  | `routers/transforms.py` (startup hook)       |
| Plot attachments      | `ctx.attach_plot(function_id=..., ...)`       | `routers/plots.py` (startup hook)            |
| MCP server attachments| `ctx.register_mcp_server(...)`                | `cof/orchestrator.py`                        |

Each registered artifact is stamped with `source="pack"` and `pack_id=<id>`
on its wire schema, plus a colored `Pack: <id>` badge in the UI. Packs
declare a `user_groups: list[str]` — `is_pack_visible(pack_id, user_groups)`
filters every list endpoint, so a user in `["irr_team"]` won't see the
`portfolio` pack's artifacts unless its `user_groups` includes that string
(or `"*"`).

## Running locally

### Backend (uv-managed)

Dependencies live in `backend/pyproject.toml` and are pinned in
`backend/uv.lock`. Install [uv](https://docs.astral.sh/uv/) once
(`pip install uv` or `winget install astral-sh.uv`), then:

```bash
cd backend

# Create the .venv and install the locked dependency set (deterministic).
uv sync

# Configure the LLM connection (one of these). Do NOT paste keys in chat.
cp .env.example .env
# then edit backend/.env with your editor and set:
#   OPENAI_API_KEY=sk-...
# or, inside the company network:
#   COF_BASE_URL=https://...
#   COF_API_KEY=...

# Run the server in the uv-managed venv.
uv run uvicorn main:app --port 8001 --host 127.0.0.1
```

Backend serves on **http://127.0.0.1:8001** ; OpenAPI docs at `/docs`.

Common uv commands:

| Task                                | Command                              |
|-------------------------------------|--------------------------------------|
| Add a runtime dep                   | `uv add fastapi-something`           |
| Add a dev-only dep                  | `uv add --dev pytest-asyncio`        |
| Install optional ONNX support       | `uv sync --extra onnx`               |
| Update everything to latest allowed | `uv lock --upgrade && uv sync`       |
| Run any command in the venv        | `uv run <command>` (e.g. `uv run python -c "import agents; print(agents.__version__)"`) |
| Drop into a venv-aware shell        | `.venv\Scripts\activate` (Windows)   |

> **Windows + OneDrive notes:**
> 1. `pyproject.toml` already sets `[tool.uv].link-mode = "copy"` because
>    the OneDrive filesystem rejects uv's hardlink mode (Windows error 396).
> 2. `--reload` is unreliable when the backend directory lives inside
>    OneDrive — file-watcher events get dropped and orphaned
>    multiprocessing children can hold port 8001 after a parent kill.
>    Run without `--reload` and restart manually after edits, or move the
>    checkout outside OneDrive.

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite serves on **http://localhost:5173** if free, otherwise auto-bumps to
**5174**. The dev server proxies `/api/*` to `http://localhost:8001` (set
in `vite.config.ts`).

## Sign-in

| Field    | Value      |
|----------|------------|
| Username | `pqr557`   |
| Password | `capital1` |

Profile: **Quantitative Analyst** in the **Capital Markets & Analytics**
department, with `groups: ["*"]` (sees every pack). This is the only
account `routers/auth.py` accepts; any other combination returns 401.

## Business functions shipped out of the box

Markets & Risk:

1. Investment Portfolio Analytics
2. Market Risk & VaR
3. Interest Rate Risk Management

Treasury & Capital:

4. Liquidity & Funding
5. Capital Planning & CCAR
6. Financial Planning & Reporting

The **portfolio** pack attaches its 2 datasets (macro history + forecast)
and the BGM term-structure model to *Investment Portfolio*, plus a copy
of the same BGM model to *Interest Rate Risk*.

The **deposits** pack attaches a Data Harness transform (reads OneLake →
Finance), three MaaS pickle models (`output_kind="multi_target"` returning
end_balance + interest_income), and a starter Reporting catalog (unpinned
by default) to *Capital Planning & CCAR* — the deposit-suite forecast is
one workstream that feeds the overall capital plan. Other functions start
empty until you build models, upload datasets, or attach a pack.

## Adding a new domain pack

The whole delta for a new domain (e.g. `interest_rate_risk`) is a new
directory. Zero edits to any router, schema, or loader.

```
backend/packs/interest_rate_risk/
├── pack.py            ← register(ctx)
├── skills/            ← any *.md skill files
└── tools.py           ← register_python_tools(ctx)
```

**`pack.py`** — declare metadata, then call ctx methods:

```python
from packs import Pack, PackContext
from packs.interest_rate_risk.tools import register_python_tools

def register(ctx: PackContext) -> None:
    ctx.pack = Pack(
        id="interest_rate_risk",
        label="Interest Rate Risk",
        description="ALM, EVE shock, BGM scenario work.",
        attach_to_functions=["interest_rate_risk"],
        user_groups=["irr_team"],   # empty = visible to everyone
        color="#0F766E",
        icon="trending-up",
    )

    ctx.register_skill_dir()         # default: <pack_dir>/skills
    register_python_tools(ctx)       # the tools.py module's helper

    ctx.attach_dataset(
        function_id="interest_rate_risk",
        dataset_id="ds-rate-history",
        name="Rate History",
        description="...",
        source_path=Path(__file__).parent / "data" / "rate_history.csv",
    )
    ctx.attach_model(
        function_id="interest_rate_risk",
        model_id="mdl-bgm-irr",
        name="BGM Term-Structure",
        description="...",
        source_path=Path(__file__).parent / "models" / "bgm.pkl",
        train_metrics={"calibration_rmse_bps": 4.2},
    )

    # Pre-wire an ETL transform so the Workflow canvas has a Transforms
    # palette card. The recipe is read-only Python; running the canvas
    # node resolves to `output_dataset_id` for downstream models.
    ctx.attach_transform(
        function_id="interest_rate_risk",
        transform_id="tr-irr-rate-harness",
        name="Rate Harness",
        description="Joins OneLake rate tables and engineers IRR features.",
        input_data_source_ids=["ds-onelake-finance"],
        output_dataset_id="ds-rate-history",
        recipe_python="...",
        parameters=[{"name": "horizon_months", "label": "Horizon",
                     "type": "number", "default": 27}],
    )
```

**`tools.py`** — one `ctx.register_python_tool(...)` per tool:

```python
from packs import PackContext

def register_python_tools(ctx: PackContext) -> None:
    ctx.register_python_tool(
        name="compute_eve_shock",
        description="Apply a parallel rate shock and return EVE % change.",
        parameters=[
            {"name": "shock_bps", "type": "integer",
             "description": "Parallel shock in bps.", "required": True},
        ],
        python_source=(
            "def compute_eve_shock(shock_bps: int):\n"
            "    return {'shock_bps': shock_bps,\n"
            "            'eve_pct': round(-0.01 * shock_bps, 2)}\n"
        ),
    )
```

**Skill files** — drop any number of markdown skills into `skills/`. Use
the same frontmatter the universal built-ins use (`name`, `description`,
`model`, `max_tokens`, `tools`, `color`, `icon`). Tool names referenced in
`tools:` can come from `agent/tools.py` (introspection), the registered
Python tools (universal or any pack's), or be added later — the editor
flags any unknown ones inline.

Restart the backend; `packs.discover_and_register()` runs at startup and
the pack's artifacts appear automatically with their `Pack: <id>` badge.

## Data Services integrations (proxy-env)

The Data tab's **Data Services** section has three groups — *Scenario
Service from Predictive Analytics* (Data Harness, Data Quality Check),
*CCAR* (BHCB / BHCS / FedB / FedSA per year), and *Outlook* (internal IR
view). Each group has two backing modes:

- **static** (default) — hard-coded specs in
  `backend/services/data_services.py`. The app works end-to-end without
  any corporate connectivity.
- **live** — opt-in, env-driven. Predictive Analytics tools pulled from
  a `pa_common_tools` pip package; CCAR + Outlook pulled from OneLake
  tables. When live mode is on but the integration fails at runtime
  (package missing, OneLake unreachable, schema drift), the backend logs
  and falls back to static so the UI never breaks.

Each section header shows a status badge:

| Badge                       | Meaning                                                |
|-----------------------------|--------------------------------------------------------|
| `static` (grey)             | Integration is off                                     |
| `live · <integration>` (green) | Enabled and succeeded                               |
| `<integration> · fallback` (amber) | Enabled but failed; hover for the failure detail |

Single source of truth for env vars:
[`backend/config/data_services.example.env`](backend/config/data_services.example.env).
Copy it into the backend's runtime env (`.env`, systemd unit, K8s
Secret) when running inside the corporate proxy.

### Configuring `pa_common_tools` (Predictive Analytics tools)

The package ships **Data Harness** + **Data Quality Check** as importable
entries; the workbench discovers them by name convention so updating the
cards becomes a `pip install --upgrade` — no app deploy.

**1. Set env vars** (default values below — only `_ENABLED` strictly needs
flipping if your package name matches):

```bash
CMA_PA_COMMON_TOOLS_ENABLED=1
CMA_PA_COMMON_TOOLS_PACKAGE=pa_common_tools
```

**2. Confirm the package exposes one of these naming patterns** at the
top level of its module:

| Service             | Recognized names                                      |
|---------------------|-------------------------------------------------------|
| Data Harness        | `DataHarness`, `data_harness`, `DATA_HARNESS`         |
| Data Quality Check  | `DataQualityCheck`, `data_quality_check`, `DATA_QUALITY_CHECK` |

Either a class, a function, or a module-level constant pointing at one
works. Card text comes from (in order): an explicit `.description`
attribute → the entry's docstring → the static fallback.

**3. Install the package in the backend's venv**:

```bash
cd backend
uv add pa_common_tools          # or `uv pip install pa_common_tools`
```

**4. Restart the backend.** The Data Services section should now show a
`live · pa_common_tools` badge above Predictive Analytics, with the card
tag changed from `BUILT-IN` to e.g. `pa_common_tools · DataHarness`.

If discovery finds nothing, the backend logs a warning naming the
missing entries and falls back to static — so it's safe to flip on
before the package is fully built out.

### Configuring OneLake-backed CCAR + Outlook

CCAR and Outlook both come from OneLake tables when this integration is
on. The workbench fetches the years (distinct `year` column on the CCAR
table), then per-year scenarios, then the Outlook table; any failure
falls back to static.

**1. Set env vars** (default workspace + lakehouse names below; override
to point at your tables):

```bash
CMA_ONELAKE_SCENARIOS_ENABLED=1
CMA_ONELAKE_WORKSPACE=Finance
CMA_ONELAKE_LAKEHOUSE=cma
CMA_ONELAKE_CCAR_TABLE=ccar_scenarios
CMA_ONELAKE_OUTLOOK_TABLE=outlook_scenarios
```

**2. Plug in your corporate extractor.** Open
`backend/services/data_services.py` and find `_onelake_read_table(...)` —
it currently raises `NotImplementedError`. Replace its body with your
extractor:

```python
def _onelake_read_table(table: str, **filters: Any) -> list[dict[str, Any]]:
    from your_corp_lib import OneLakeExtractor
    client = OneLakeExtractor(
        workspace=ONELAKE_WORKSPACE,
        lakehouse=ONELAKE_LAKEHOUSE,
    )
    return client.read_table(table_name=table, **filters)
```

The function should return a list of plain dicts. Keep it synchronous —
if your client is async, wrap with `asyncio.run(...)` so the rest of the
loaders stay unchanged.

**3. Match the expected row shapes**:

| Table                | Required columns                                               |
|----------------------|----------------------------------------------------------------|
| `ccar_scenarios`     | `year`, `code`, `label`, `severity`, `source`, `description`   |
| `outlook_scenarios`  | `id`, `title`, `subtitle`, `description` (+ optional `color`, `icon`, `tag`, `agent_prompt`) |

`severity` ∈ `{base, adverse, severely_adverse}` (drives card color).
`source` ∈ `{BHC, Fed}` (drives card icon).

**4. Restart the backend.** The CCAR and Outlook section headers should
flip to `live · onelake` with the year dropdown now reflecting whatever
distinct years are in your `ccar_scenarios` table.

### Quick verification

After flipping either integration on, hit the endpoint directly to see
what the loaders found:

```bash
curl -H "Authorization: Bearer <token>" \
  "http://127.0.0.1:8001/api/data_services?function_id=capital_planning" \
  | jq '.predictive_status, .ccar_status, .outlook_status'
```

Each status object reports `enabled`, `live`, and a `detail` string —
the same string the UI shows on hover of the badge.

## Customization (Settings)

The Settings page (sidebar) has four tabs:

- **Data Sources** — Snowflake, OneLake, Postgres, REST, S3 connections
  plus direct file upload (CSV / Parquet / XLSX / JSON). Used by datasets
  bound on the workspace's Data tab.
- **Agent Skills** — three sections: *User-Customized* (uploads / your
  edits), one section per *Domain Pack* (read-only here), and *Built-in*.
  Edit a skill to fork or wire its tool list. The tool picker uses chips
  for what's selected and a search-to-add input for the rest, so it scales
  past 30+ tools without becoming a wall of checkboxes.
- **Python Tools** — same three-section layout. `+ New Tool` opens a
  drafter where you describe the tool in plain English, the agent fills
  in the source / parameters / description, and the test runner executes
  it in a subprocess sandbox (5-second wall clock) before save.
- **Plot Builder** — tile/chart designer. Pick source (dataset or run
  output), chart type, X axis, multiple Y series, aggregation. Live
  preview, then pin to Reporting or Overview.

## Notes & known limitations

- **Persistence is split.** Datasets, models, saved workflows, and
  analytics runs are durable when `CMA_STATE_TABLE` points at the
  DynamoDB table — see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
  Without it they fall back to in-process dicts and reset on restart,
  which is the default for local development.
  Also durable: charts, Python tools, dashboard layouts, warehouse
  connections, and analytic definitions. Still in-process either way:
  playbooks and their runs, plus scenarios (which are re-derived from
  packs at startup, so every node agrees anyway). The pack-bundled datasets and
  models re-stage on startup from `sample_data/` and `sample_models/`,
  so those reappear automatically.
- **Uploaded bytes follow the records** when `CMA_CORPUS_BUCKET` is set:
  dataset files and model artifacts write through to S3, and any node
  pulls on a local miss. Without a bucket they stay on local disk, which
  is fine for one node. Still local-only either way: uploaded agent
  skills and the RAG index.
- **Data Services live mode is opt-in.** Outside the corporate proxy,
  Data Services renders static fallback cards — the app stays usable.
  See [Data Services integrations](#data-services-integrations-proxy-env)
  for how to flip on `pa_common_tools` and OneLake when you're inside
  the proxy.
- **Auth is mock by default** — single account, in-memory bearer-token
  store, and no flow for adding a second user. Set
  `CMA_COGNITO_USER_POOL_ID` + `CMA_COGNITO_CLIENT_ID` and the token
  becomes a Cognito JWT verified offline instead, with `cognito:groups`
  feeding the same `is_pack_visible(...)` filter. See
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
- **Subprocess sandbox is process-isolation only.** Python tools run in
  a fresh `python` subprocess with a wall-clock timeout. There is no
  imports allowlist, no RLIMIT, no seccomp, no chroot. Don't deploy
  user-submitted Python tools without hardening this.
- **`--reload` flakiness on Windows + OneDrive** — see backend run note.
- **Frontend port** — Vite picks 5173 if free, 5174 otherwise. Either
  works; the proxy target is 8001 regardless.

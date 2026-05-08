"""CMA Workbench - Capital Markets & Analytics self-service platform - FastAPI backend."""
# Load backend/.env into the process environment BEFORE any other imports so
# that downstream modules (cof.orchestrator, etc.) see OPENAI_API_KEY at import time.
from pathlib import Path
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Domain-pack discovery ──────────────────────────────────────────────────
# Each `packs/<id>/pack.py` registers its skills, tools, datasets, and model
# attachments via the PackContext API. Run discovery BEFORE importing any
# router so the routers' module-level seeds can pull pack-registered tools
# from the registry.
import packs as _packs
_packs.discover_and_register()

# Bridge pack-registered Python tools into the agent runtime. Pack tools
# are added to the Settings UI registry by `register_python_tool(...)`,
# but agents dispatch through `agent.tools._HANDLERS` — this hop merges
# them so any skill that lists a pack tool in its frontmatter can
# actually call it. Idempotent; safe to call once on startup.
from agent.tools import register_pack_tools as _register_pack_tools
_register_pack_tools()

# Build the live MCP server registry from pack attachments BEFORE importing
# routers — `routers.chat` instantiates the orchestrator at import time,
# which constructs each specialist agent and calls
# `resolve_mcp_servers_for_skill(...)`. If the registry is empty at that
# point, every skill that lists `mcp_servers:` would log a spurious
# "unknown MCP server" warning per chat call.
try:
    from cof.mcp_registry import register_pack_mcp_servers
    register_pack_mcp_servers()
except Exception as e:
    print(f"[startup] MCP server registration failed: {e}")

from routers import (
    auth,
    functions,
    workspace,
    chat,
    datasources,
    datasets,
    documents,
    skills,
    plots,
    tools,
    models_registry,
    transforms,
    scenarios,
    playbooks,
    analytics_defs,
    overview_layouts,
    mcp_servers,
    data_services,
)

app = FastAPI(
    title="CMA Workbench API",
    description="Self-service analytics platform for Capital Markets & Finance analysts",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])
app.include_router(functions.router, prefix="/api/functions", tags=["Business Functions"])
app.include_router(workspace.router, prefix="/api/workspace", tags=["Workspace"])
app.include_router(chat.router, prefix="/api/chat", tags=["Chat"])
app.include_router(datasources.router, prefix="/api/datasources", tags=["Data Sources"])
app.include_router(documents.router, prefix="/api/documents", tags=["Knowledge Base"])
app.include_router(datasets.router, prefix="/api/datasets", tags=["Datasets"])
app.include_router(models_registry.router, prefix="/api/models", tags=["Models"])
app.include_router(transforms.router, prefix="/api/transforms", tags=["Transforms"])
app.include_router(scenarios.router, prefix="/api/analytics", tags=["Scenarios & Runs"])
app.include_router(playbooks.router, prefix="/api/playbooks", tags=["Playbooks"])
app.include_router(skills.router, prefix="/api/skills", tags=["Agent Skills"])
app.include_router(plots.router, prefix="/api/plots", tags=["Plot Builder"])
app.include_router(tools.router, prefix="/api/tools", tags=["Python Tools"])
app.include_router(analytics_defs.router, prefix="/api/analytics_defs", tags=["Analytics Definitions"])
app.include_router(overview_layouts.router, prefix="/api/overview_layouts", tags=["Overview Layouts"])
app.include_router(mcp_servers.router, prefix="/api/mcp_servers", tags=["MCP Servers"])
app.include_router(data_services.router, prefix="/api/data_services", tags=["Data Services"])


@app.on_event("startup")
async def _ingest_pack_assets():
    """After all routers and registries are wired, pull dataset, model,
    and plot attachments from each registered pack into the in-memory
    stores. Tools are ingested lazily inside `routers/tools.py:_seed()`."""
    try:
        datasets._ingest_pack_datasets()
    except Exception as e:
        print(f"[startup] dataset pack ingest failed: {e}")
    try:
        models_registry._ingest_pack_models()
    except Exception as e:
        print(f"[startup] model pack ingest failed: {e}")
    # Transforms depend on dataset ingest (they reference output_dataset_id).
    try:
        transforms._ingest_pack_transforms()
    except Exception as e:
        print(f"[startup] transform pack ingest failed: {e}")
    # Plots depend on dataset ingest — they reference dataset_ids by name.
    try:
        plots._ingest_pack_plots()
    except Exception as e:
        print(f"[startup] plot pack ingest failed: {e}")
    # Push CCAR + Outlook cards from the Data Services aggregator into
    # the legacy `_SCENARIOS` + `BUILTIN_DATA` so the Workflow tab's
    # Scenarios palette mirrors what's on the Data tab → Data Services
    # section. Deferred to here so the data_services config has been
    # read and any pack-attached datasets the loaders peek at exist.
    try:
        from services.data_services import materialize_into_scenarios_registry
        n = materialize_into_scenarios_registry(None)
        print(f"[startup] data_services: materialized {n} scenario(s) into _SCENARIOS")
    except Exception as e:
        print(f"[startup] data_services scenario materialization failed: {e}")
    # MCP server registration already runs at module import (see top of
    # this file) so the orchestrator sees the live registry when it
    # constructs specialists. No-op here.


@app.get("/health")
async def health():
    return {"status": "ok", "service": "CMA Workbench API"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)

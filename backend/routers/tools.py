"""Python tool registry - users can register Python functions as agent tools.

Each tool stores its source code, a JSON-schema-shaped parameter list, and the
name of the entry-point function. The /test endpoint runs the function in a
fresh subprocess with a timeout; this is good enough for a local workbench but
NOT a hardened sandbox: production deployments should add an imports allowlist,
seccomp / cgroup limits, and a non-privileged user. (See _run_tool below.)
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from models.schemas import (
    PythonTool,
    PythonToolCreate,
    PythonToolUpdate,
    ToolDraftRequest,
    ToolDraftResponse,
    ToolParameter,
    ToolTestRequest,
    ToolTestResponse,
)
from packs import is_pack_visible
from routers.auth import get_current_user, get_current_user_groups

router = APIRouter()


# ── tool persistence ───────────────────────────────────────────────────────
# Analyst-written Python tools. Three endpoints below mutate a tool in place
# (`update`, `toggle`, `test` writing back its last result), so each one now
# writes back explicitly — a loaded tool is a copy, not the registry's object.
_TOOL_ENTITY = "pytool"


def load_tool(tool_id: str) -> PythonTool | None:
    from services import entity_store

    record = entity_store.get(_TOOL_ENTITY, tool_id)
    return PythonTool(**record) if record else None


def store_tool(t: PythonTool) -> PythonTool:
    from services import entity_store

    entity_store.put(_TOOL_ENTITY, t.id, t.model_dump())
    return t


def all_tools() -> list[PythonTool]:
    from services import entity_store

    out: list[PythonTool] = []
    for record in entity_store.list_all(_TOOL_ENTITY):
        try:
            out.append(PythonTool(**record))
        except Exception:
            continue
    return out


def remove_tool(tool_id: str) -> None:
    from services import entity_store

    entity_store.delete(_TOOL_ENTITY, tool_id)

DEFAULT_SOURCE = '''def my_tool(symbol: str, lookback_days: int = 30):
    """Example tool — replace with your own logic.

    Returns a small dict the agent can summarize.
    """
    return {
        "symbol": symbol,
        "lookback_days": lookback_days,
        "note": "Replace this body with a real lookup against your data source.",
    }
'''


def _seed():
    if all_tools():
        return
    builtin_seeds = [
        PythonTool(
            id="tool-sql-query",
            name="sql_query",
            description="Run a read-only SQL query against the active data source and return the rows.",
            parameters=[
                {"name": "sql", "type": "string", "description": "The SQL query to execute (read-only).", "required": True},
                {"name": "limit", "type": "integer", "description": "Max rows to return.", "required": False},
            ],
            python_source=(
                'def sql_query(sql: str, limit: int = 100):\n'
                '    """Mock SQL runner. Replace with snowflake/onelake driver."""\n'
                '    return {"rows_returned": min(limit, 42), "preview": ["row_1", "row_2", "row_3"], "sql": sql}\n'
            ),
            function_name="sql_query",
            enabled=True,
            source="builtin",
        ),
        PythonTool(
            id="tool-metadata-lookup",
            name="metadata_lookup",
            description="Return data-lineage metadata for a metric: source table, formula, as-of.",
            parameters=[
                {"name": "metric", "type": "string", "description": "Name of the metric to look up.", "required": True},
            ],
            python_source=(
                'def metadata_lookup(metric: str):\n'
                '    """Mock metadata lookup."""\n'
                '    return {\n'
                '        "metric": metric,\n'
                '        "source": "cma_warehouse.fact_positions",\n'
                '        "formula": "weighted by market_value",\n'
                '        "as_of": "EOD prior business day",\n'
                '    }\n'
            ),
            function_name="metadata_lookup",
            enabled=True,
            source="builtin",
        ),
        PythonTool(
            id="tool-scenario-parallel",
            name="scenario_parallel",
            description="Apply a parallel rate shock and return the EVE % impact.",
            parameters=[
                {"name": "shock_bps", "type": "integer", "description": "Parallel shock in bps (e.g. 200, -100).", "required": True},
            ],
            python_source=(
                'def scenario_parallel(shock_bps: int):\n'
                '    """Mock EVE shock — illustrative quadratic in shock_bps."""\n'
                '    eve = -0.01 * shock_bps - 0.000005 * (shock_bps ** 2)\n'
                '    return {"shock_bps": shock_bps, "eve_pct": round(eve * 100, 2)}\n'
            ),
            function_name="scenario_parallel",
            enabled=True,
            source="builtin",
        ),
        PythonTool(
            id="tool-rag-search",
            name="rag_search",
            description=(
                "Retrieval-Augmented Generation search across a folder of documents "
                "(markdown, text, CSV, XLSX). Returns the top-k matching chunks "
                "ranked by keyword score, each with the source path and any "
                "frontmatter metadata (model_id, model_component, portfolio_scope) "
                "the chunk's parent doc carries. Use this to surface methodology / "
                "whitepaper context behind a forecast number. Defaults doc_dir to "
                "$CMA_DOCS_ROOT or <repo>/sample_docs/."
            ),
            parameters=[
                {"name": "query",   "type": "string",
                 "description": "Free-text query (multi-word).", "required": True},
                {"name": "doc_dir", "type": "string",
                 "description": "Folder of documents to search. Defaults to $CMA_DOCS_ROOT or sample_docs/.",
                 "required": False},
                {"name": "top_k",   "type": "integer",
                 "description": "Max chunks to return (default 5).", "required": False},
            ],
            python_source=(
                'def rag_search(query, doc_dir=None, top_k=5):\n'
                '    """Keyword RAG over the Knowledge Base. Reads .md / .txt /\n'
                '    .py / .json / .csv / .xlsx natively, plus .pdf / .docx /\n'
                '    .pptx via pypdf / python-docx / python-pptx (graceful skip\n'
                '    if a parser library is missing). Returns the top `top_k`\n'
                '    chunks ranked by raw token-frequency match, each with any\n'
                '    markdown frontmatter (model_id, portfolio_scope, ...)."""\n'
                '    import os, re, glob\n'
                '\n'
                '    if not doc_dir:\n'
                '        # Resolve doc_dir: $CMA_DOCS_ROOT, else walk up from cwd\n'
                '        # looking for a sibling `sample_docs/` folder.\n'
                '        doc_dir = os.environ.get("CMA_DOCS_ROOT") or ""\n'
                '        if not doc_dir:\n'
                '            here = os.path.abspath(os.getcwd())\n'
                '            for _ in range(5):\n'
                '                cand = os.path.join(here, "sample_docs")\n'
                '                if os.path.isdir(cand):\n'
                '                    doc_dir = cand; break\n'
                '                parent = os.path.dirname(here)\n'
                '                if parent == here: break\n'
                '                here = parent\n'
                '    if not doc_dir or not os.path.isdir(doc_dir):\n'
                '        return {"error": f"doc_dir does not exist: {doc_dir!r}. Pass an absolute path or set CMA_DOCS_ROOT."}\n'
                '\n'
                '    q_tokens = [t for t in re.findall(r"[a-z0-9_]+", query.lower()) if len(t) > 2]\n'
                '    if not q_tokens:\n'
                '        return {"matches": []}\n'
                '\n'
                '    def read_text(path):\n'
                '        ext = os.path.splitext(path)[1].lower()\n'
                '        try:\n'
                '            if ext in (".md", ".txt", ".py", ".json"):\n'
                '                with open(path, encoding="utf-8", errors="ignore") as fh: return fh.read()\n'
                '            if ext == ".csv":\n'
                '                import pandas as pd; return pd.read_csv(path).to_csv(index=False)\n'
                '            if ext in (".xlsx", ".xls"):\n'
                '                import pandas as pd; return pd.read_excel(path).to_csv(index=False)\n'
                '            if ext == ".pdf":\n'
                '                try:\n'
                '                    from pypdf import PdfReader\n'
                '                except ImportError: return ""\n'
                '                reader = PdfReader(path)\n'
                '                return "\\n\\n".join((p.extract_text() or "") for p in reader.pages)\n'
                '            if ext == ".docx":\n'
                '                try:\n'
                '                    from docx import Document\n'
                '                except ImportError: return ""\n'
                '                doc = Document(path)\n'
                '                paras = [p.text for p in doc.paragraphs if p.text and p.text.strip()]\n'
                '                for tbl in doc.tables:\n'
                '                    for row in tbl.rows:\n'
                '                        for cell in row.cells:\n'
                '                            if cell.text and cell.text.strip():\n'
                '                                paras.append(cell.text.strip())\n'
                '                return "\\n\\n".join(paras)\n'
                '            if ext == ".pptx":\n'
                '                try:\n'
                '                    from pptx import Presentation\n'
                '                except ImportError: return ""\n'
                '                prs = Presentation(path)\n'
                '                slides_out = []\n'
                '                for idx, slide in enumerate(prs.slides, start=1):\n'
                '                    parts = [f"[Slide {idx}]"]\n'
                '                    for shape in slide.shapes:\n'
                '                        if shape.has_text_frame:\n'
                '                            for para in shape.text_frame.paragraphs:\n'
                '                                txt = "".join(run.text for run in para.runs)\n'
                '                                if txt and txt.strip():\n'
                '                                    parts.append(txt.strip())\n'
                '                    if len(parts) > 1:\n'
                '                        slides_out.append("\\n".join(parts))\n'
                '                return "\\n\\n".join(slides_out)\n'
                '        except Exception: return ""\n'
                '        return ""\n'
                '\n'
                '    def parse_md(text):\n'
                '        meta = {}\n'
                '        m = re.match(r"^---\\n(.*?)\\n---\\n(.*)$", text, re.DOTALL)\n'
                '        body = m.group(2) if m else text\n'
                '        if m:\n'
                '            for line in m.group(1).splitlines():\n'
                '                if ":" in line:\n'
                '                    k, _, v = line.partition(":")\n'
                '                    meta[k.strip()] = v.strip().strip(\'"\')\n'
                '        chunks = [c.strip() for c in re.split(r"\\n\\s*\\n", body) if c.strip()]\n'
                '        return meta, chunks\n'
                '\n'
                '    scored = []\n'
                '    patterns = ("*.md", "*.txt", "*.py", "*.json", "*.csv",\n'
                '                "*.xlsx", "*.xls", "*.pdf", "*.docx", "*.pptx")\n'
                '    for pat in patterns:\n'
                '        for path in sorted(glob.glob(os.path.join(doc_dir, "**", pat), recursive=True)):\n'
                '            text = read_text(path)\n'
                '            meta, chunks = (parse_md(text) if path.endswith(".md") else ({}, [c.strip() for c in re.split(r"\\n\\s*\\n", text) if c.strip()]))\n'
                '            for i, chunk in enumerate(chunks):\n'
                '                tokens = re.findall(r"[a-z0-9_]+", chunk.lower())\n'
                '                score = sum(tokens.count(t) for t in q_tokens)\n'
                '                if score == 0: continue\n'
                '                preview = chunk[:480] + ("..." if len(chunk) > 480 else "")\n'
                '                scored.append({\n'
                '                    "score": score,\n'
                '                    "model_id":        meta.get("model_id", os.path.basename(path)),\n'
                '                    "model_component": meta.get("model_component", ""),\n'
                '                    "portfolio_scope": meta.get("portfolio_scope", ""),\n'
                '                    "doc_path": path, "chunk_index": i, "preview": preview,\n'
                '                })\n'
                '    scored.sort(key=lambda r: -r["score"])\n'
                '    return {"query": query, "matches": scored[:int(top_k or 5)]}\n'
            ),
            function_name="rag_search",
            enabled=True,
            source="builtin",
        ),
        PythonTool(
            id="tool-get-macro-schema",
            name="get_macro_dataset_schema",
            description=(
                "Return the schema and field catalog for the standard reporting dataset "
                "(long format: scenario, snap_date, variable_name, variable_value, segment, origin). "
                "Use this to understand available columns, known scenario labels, segment values, "
                "and example variable names before designing tiles. No dataset_id required."
            ),
            parameters=[],
            python_source=(
                'def get_macro_dataset_schema():\n'
                '    """Return schema + field catalog for the long-format reporting dataset."""\n'
                '    return {\n'
                '        "format": "long",\n'
                '        "description": "Standard reporting dataset in long format. Each row is one metric for one scenario/date/segment combination.",\n'
                '        "columns": [\n'
                '            {"name": "scenario",       "type": "string", "description": "Scenario label, e.g. BHCB, BHCS, FEDB, FEDSA"},\n'
                '            {"name": "snap_date",      "type": "date",   "description": "Snapshot date in yyyy-mm-dd format (monthly cadence)"},\n'
                '            {"name": "variable_name",  "type": "string", "description": "Name of the metric or driver, e.g. FEDFUNDS, interest_apy, interest_expense"},\n'
                '            {"name": "variable_value", "type": "float",  "description": "Numeric value for this metric"},\n'
                '            {"name": "segment",        "type": "string", "description": "Business or portfolio segment, e.g. GB, NON-GB, HYMM, Macro, Portfolio"},\n'
                '            {"name": "origin",         "type": "string", "description": "Row type: input (assumption/driver) or output (model result)"},\n'
                '        ],\n'
                '        "example_scenarios": ["BHCB", "BHCS", "FEDB", "FEDSA"],\n'
                '        "example_variable_names": [\n'
                '            "FEDFUNDS", "interest_apy", "interest_expense",\n'
                '            "ecr_driven_avg_balance", "rate_driven_balance",\n'
                '        ],\n'
                '        "example_segments": ["GB", "NON-GB", "HYMM", "Macro", "Portfolio"],\n'
                '        "origin_values": ["input", "output"],\n'
                '        "snap_date_format": "yyyy-mm-dd",\n'
                '        "filter_hints": {\n'
                '            "by_scenario": {"field": "scenario", "op": "eq", "value": "<scenario>"},\n'
                '            "by_variable": {"field": "variable_name", "op": "eq", "value": "<variable_name>"},\n'
                '            "by_segment":  {"field": "segment", "op": "eq", "value": "<segment>"},\n'
                '            "inputs_only":  {"field": "origin", "op": "eq", "value": "input"},\n'
                '            "outputs_only": {"field": "origin", "op": "eq", "value": "output"},\n'
                '        },\n'
                '    }\n'
            ),
            function_name="get_macro_dataset_schema",
            enabled=True,
            source="builtin",
        ),
        PythonTool(
            id="tool-design-reporting-tiles",
            name="design_reporting_tiles",
            description=(
                "Validate and echo back a proposed set of tile blueprints for the "
                "Reporting dashboard. Pass the JSON array of tile configs from the "
                "LLM; this tool validates required fields and returns the cleaned list "
                "ready for the frontend to create via POST /api/plots."
            ),
            parameters=[
                {"name": "tiles_json", "type": "string",
                 "description": "JSON array of tile blueprint objects.", "required": True},
                {"name": "dataset_id", "type": "string",
                 "description": "Dataset id to attach to all tiles.", "required": False},
            ],
            python_source=(
                'def design_reporting_tiles(tiles_json, dataset_id="macro_reporting_sample"):\n'
                '    """Validate tile blueprints and return cleaned list."""\n'
                '    import json\n'
                '    try:\n'
                '        tiles = json.loads(tiles_json) if isinstance(tiles_json, str) else tiles_json\n'
                '    except Exception as e:\n'
                '        return {"error": f"Invalid JSON: {e}"}\n'
                '    required = {"tile_type", "name"}\n'
                '    valid, skipped = [], []\n'
                '    for t in (tiles if isinstance(tiles, list) else []):\n'
                '        missing = required - set(t.keys())\n'
                '        if missing:\n'
                '            skipped.append({"tile": t.get("name","?"), "reason": f"missing {missing}"})\n'
                '            continue\n'
                '        t.setdefault("dataset_id", dataset_id)\n'
                '        t.setdefault("aggregation", "none")\n'
                '        t.setdefault("filters", [])\n'
                '        t.setdefault("y_fields", ["variable_value"])\n'
                '        valid.append(t)\n'
                '    return {"valid_tiles": valid, "skipped": skipped, "count": len(valid)}\n'
            ),
            function_name="design_reporting_tiles",
            enabled=True,
            source="builtin",
        ),
    ]
    for t in builtin_seeds:
        store_tool(t)

    # Pull in pack-registered tools (each domain pack registers its tools at
    # startup via PackContext.register_python_tool; the seeds end up in
    # `packs.python_tool_seeds()`).
    _ingest_pack_seeds()


def _ingest_pack_seeds() -> None:
    """Idempotent — pack-registered tools land in the store keyed by id.
    Safe to call again after `discover_and_register()` runs."""
    from packs import python_tool_seeds

    existing = {t.id for t in all_tools()}
    for s in python_tool_seeds():
        if s["id"] in existing:
            continue
        store_tool(PythonTool(**s))




_seed()


def _validate_source(source: str, function_name: str) -> tuple[bool, str | None]:
    """Parse the source with ast and check the named function is defined."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return False, f"SyntaxError: {e.msg} (line {e.lineno})"
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return True, None
    return False, f"Function '{function_name}' is not defined in the source."


def _run_tool(source: str, function_name: str, args: dict[str, Any], timeout: float = 5.0) -> ToolTestResponse:
    """Execute the tool in a subprocess with a timeout.

    NOT a real sandbox — this only buys process isolation and a wall-clock cap.
    For production, add: imports allowlist, resource limits (RLIMIT_*), seccomp,
    chroot or container, and a non-privileged user.
    """
    harness = (
        "import json, sys, traceback\n"
        "\n"
        "# === user source begins ===\n"
        f"{source}\n"
        "# === user source ends ===\n"
        "\n"
        "try:\n"
        "    args = json.loads(sys.stdin.read() or '{}')\n"
        f"    result = {function_name}(**args)\n"
        "    sys.stdout.write('__CMA_RESULT__:' + json.dumps({'ok': True, 'result': result}, default=str))\n"
        "except Exception as e:\n"
        "    sys.stdout.write('__CMA_RESULT__:' + json.dumps({\n"
        "        'ok': False, 'error': str(e), 'traceback': traceback.format_exc()\n"
        "    }))\n"
    )

    fd, path = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        f.write(harness)

    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, path],
            input=json.dumps(args),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolTestResponse(ok=False, error=f"Timed out after {timeout}s", duration_ms=timeout * 1000)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    duration_ms = (time.perf_counter() - started) * 1000

    if completed.returncode != 0:
        return ToolTestResponse(
            ok=False,
            error=f"Subprocess exited with code {completed.returncode}",
            traceback=(completed.stderr or completed.stdout)[-4000:],
            duration_ms=duration_ms,
        )

    out = completed.stdout
    marker = "__CMA_RESULT__:"
    idx = out.rfind(marker)
    if idx == -1:
        return ToolTestResponse(ok=False, error="Tool produced no result envelope", traceback=out[-4000:], duration_ms=duration_ms)
    try:
        payload = json.loads(out[idx + len(marker):].strip())
    except json.JSONDecodeError as e:
        return ToolTestResponse(ok=False, error=f"Could not parse result: {e}", traceback=out[-4000:], duration_ms=duration_ms)

    return ToolTestResponse(
        ok=bool(payload.get("ok")),
        result=payload.get("result"),
        error=payload.get("error"),
        traceback=payload.get("traceback"),
        duration_ms=duration_ms,
    )


@router.get("/template")
async def get_template(_: str = Depends(get_current_user)):
    return {"python_source": DEFAULT_SOURCE, "function_name": "my_tool"}


_DRAFT_SYSTEM_PROMPT = """You are a senior Python engineer helping an analyst draft a single
Python function that will be registered as an agent tool in the CMA Workbench.

You produce ONE pure Python function only. It must:
- Use only the standard library (no third-party imports).
- Have explicit type hints on every parameter and on the return type.
- Have a one-line docstring summarizing what it does.
- Return a JSON-serializable result (dict / list / str / number / bool).
- Be deterministic — no randomness, no time-dependent behavior unless required.
- Avoid network calls, file IO outside `tempfile`, and `os.system` / `subprocess` shell calls.
- Keep the body short (under ~40 lines). If the task needs more, summarize the
  approach in `notes` and keep the body to a clean skeleton.

You respond with STRICT JSON matching this schema (no markdown, no prose):

{
  "name":          string  // snake_case, the public tool name
  "description":   string  // ONE sentence describing what it does
  "function_name": string  // matches `name` exactly
  "parameters":    [ { "name": string, "type": "string"|"integer"|"number"|"boolean"|"object"|"array",
                       "description": string, "required": boolean } ]
  "python_source": string  // the full `def ...:` definition, ready to paste into an editor
  "notes":         string  // optional caveats, e.g. "stub data — replace with real query"
}

If the analyst's request is ambiguous, pick a sensible interpretation and mention
your assumptions in `notes`. Never refuse — produce a working starting point even
if the spec is fuzzy."""


@router.post("/draft", response_model=ToolDraftResponse)
async def draft_tool(req: ToolDraftRequest, _: str = Depends(get_current_user)):
    """Ask the LLM to draft a Python tool from a plain-English description.

    Returns a structured draft the analyst can review, edit, test, and save.
    Uses the same OpenAI/COF connection the orchestrator uses; if neither is
    configured, returns 503 with a setup-required message.
    """
    from cof.llm_provider import LlmNotConfigured, complete_json

    user_msg = req.prompt.strip()
    if req.context:
        user_msg += f"\n\n[Additional context]\n{req.context.strip()}"

    try:
        data = await complete_json(
            _DRAFT_SYSTEM_PROMPT,
            user_msg,
            model=os.getenv("CMA_TOOL_DRAFT_MODEL"),
        )
    except LlmNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"LLM returned non-JSON: {e}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    # Normalize and validate the agent's draft before returning. We are lenient
    # — accept missing optional fields, reject only on clearly-broken outputs.
    name = (data.get("name") or "").strip()
    fn_name = (data.get("function_name") or name).strip() or "my_tool"
    if not name:
        name = fn_name
    description = (data.get("description") or "").strip() or "Generated tool"
    raw_params = data.get("parameters") or []
    params: list[ToolParameter] = []
    for p in raw_params:
        if not isinstance(p, dict) or "name" not in p:
            continue
        ptype = p.get("type", "string")
        if ptype not in ("string", "integer", "number", "boolean", "object", "array"):
            ptype = "string"
        params.append(ToolParameter(
            name=str(p["name"]),
            type=ptype,
            description=str(p.get("description") or "") or None,
            required=bool(p.get("required", True)),
        ))
    source = data.get("python_source") or ""
    if not source.strip():
        raise HTTPException(status_code=502, detail="LLM returned empty python_source.")

    # Sanity-check the source parses and defines fn_name. If not, surface that
    # to the analyst as a `notes` warning rather than rejecting — they may
    # still want to see what the agent produced and fix it themselves.
    notes = data.get("notes")
    ok, err = _validate_source(source, fn_name)
    if not ok:
        warning = f"Draft validation: {err}"
        notes = f"{notes}\n{warning}" if notes else warning

    return ToolDraftResponse(
        name=name,
        description=description,
        function_name=fn_name,
        parameters=params,
        python_source=source,
        notes=notes,
    )


@router.get("", response_model=list[PythonTool])
async def list_tools(groups: list[str] = Depends(get_current_user_groups)):
    """Pack-scoped tools are filtered by the user's groups; universal
    (`builtin` / `user`) tools are always returned."""
    return [t for t in all_tools() if is_pack_visible(t.pack_id, groups)]


@router.get("/{tool_id}", response_model=PythonTool)
async def get_tool(tool_id: str, _: str = Depends(get_current_user)):
    t = load_tool(tool_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tool not found")
    return t


@router.post("", response_model=PythonTool, status_code=201)
async def create_tool(req: PythonToolCreate, _: str = Depends(get_current_user)):
    ok, err = _validate_source(req.python_source, req.function_name)
    if not ok:
        raise HTTPException(status_code=400, detail=err)
    tid = f"tool-{uuid.uuid4().hex[:8]}"
    t = PythonTool(id=tid, enabled=True, **req.model_dump())
    store_tool(t)
    return t


@router.patch("/{tool_id}", response_model=PythonTool)
async def update_tool(tool_id: str, req: PythonToolUpdate, _: str = Depends(get_current_user)):
    t = load_tool(tool_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tool not found")
    update = req.model_dump(exclude_unset=True)
    if "python_source" in update or "function_name" in update:
        new_source = update.get("python_source", t.python_source)
        new_fn = update.get("function_name", t.function_name)
        ok, err = _validate_source(new_source, new_fn)
        if not ok:
            raise HTTPException(status_code=400, detail=err)
    for k, v in update.items():
        setattr(t, k, v)
    store_tool(t)
    return t


@router.delete("/{tool_id}", status_code=204)
async def delete_tool(tool_id: str, _: str = Depends(get_current_user)):
    if not load_tool(tool_id):
        raise HTTPException(status_code=404, detail="Tool not found")
    remove_tool(tool_id)


@router.patch("/{tool_id}/toggle", response_model=PythonTool)
async def toggle_tool(tool_id: str, _: str = Depends(get_current_user)):
    t = load_tool(tool_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tool not found")
    t.enabled = not t.enabled
    store_tool(t)
    return t


@router.post("/{tool_id}/test", response_model=ToolTestResponse)
async def test_tool(tool_id: str, req: ToolTestRequest, _: str = Depends(get_current_user)):
    t = load_tool(tool_id)
    if not t:
        raise HTTPException(status_code=404, detail="Tool not found")
    res = _run_tool(t.python_source, t.function_name, req.args)
    t.last_test_result = res.model_dump()
    store_tool(t)
    return res

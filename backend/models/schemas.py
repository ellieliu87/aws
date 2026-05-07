"""Pydantic schemas for the CMA Workbench API."""
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


# ── Auth ────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    username: str
    role: str
    department: str
    groups: list[str] = Field(default_factory=list)


class UserInfo(BaseModel):
    username: str
    role: str
    department: str
    groups: list[str] = Field(default_factory=list)


# ── Business Functions ──────────────────────────────────────────────────────
class BusinessFunction(BaseModel):
    id: str
    name: str
    short_name: str
    description: str
    icon: str
    color: str
    category: str
    default_views: list[str]
    sample_metrics: list[str]


# ── Workspace / Views ───────────────────────────────────────────────────────
class KpiCard(BaseModel):
    label: str
    value: str
    delta: str | None = None
    delta_dir: Literal["up", "down", "flat"] | None = None
    sublabel: str | None = None


class TableSpec(BaseModel):
    title: str
    columns: list[str]
    rows: list[list[Any]]


class ChartPoint(BaseModel):
    x: Any
    y: float
    series: str | None = None


class ChartSpec(BaseModel):
    id: str
    title: str
    type: Literal["line", "bar", "area", "pie", "scatter", "stacked_bar"]
    data: list[dict[str, Any]]
    x_key: str
    y_keys: list[str]
    description: str | None = None


class WorkspaceData(BaseModel):
    function_id: str
    function_name: str
    kpis: list[KpiCard]
    charts: list[ChartSpec]
    tables: list[TableSpec]
    insights: list[str]


# ── Overview default-layout sharing ─────────────────────────────────────────
# Mirrors the JSON the frontend's Export button produces. The shape is
# intentionally permissive — the grid library evolves, and we want client
# changes to land without forcing schema/router migrations.
class OverviewLayoutItem(BaseModel):
    """One react-grid-layout entry — `i` is the card id, the rest is geometry."""
    i: str
    x: int
    y: int
    w: int
    h: int
    minW: int | None = None
    minH: int | None = None


class OverviewHiddenSet(BaseModel):
    ids: list[str] = Field(default_factory=list)


class OverviewTextCard(BaseModel):
    id: str
    body: str


class OverviewLayoutSave(BaseModel):
    """Request body when saving a function-default layout."""
    layout: list[OverviewLayoutItem] = Field(default_factory=list)
    hidden: OverviewHiddenSet = Field(default_factory=OverviewHiddenSet)
    text_cards: list[OverviewTextCard] = Field(default_factory=list)
    saved_at: str | None = None


class OverviewLayout(OverviewLayoutSave):
    """Response shape — same as save body plus the routing key + audit."""
    function_id: str
    saved_by: str | None = None


# ── Chat ────────────────────────────────────────────────────────────────────
class ChatTurn(BaseModel):
    """One prior turn the frontend ships so the agent can resolve references
    like 'the pie chart you just suggested'."""
    role: Literal["user", "assistant"]
    content: str


class ChatMessage(BaseModel):
    message: str
    function_id: str | None = None
    agent_id: str = "orchestrator"
    context: str | None = None
    # Specialist routing inputs
    tab: Literal[
        "overview", "data", "models", "workflow", "playbooks",
        "analytics", "reporting", "settings",
    ] | None = None
    entity_kind: Literal["kpi", "dataset", "scenario", "model", "run", "tile", "workflow", "analytic_def", "transform"] | None = None
    entity_id: str | None = None
    # Optional payload — for workflow validation we pass nodes/edges
    payload: dict[str, Any] | None = None
    # Prior turns from the chat panel, oldest-first. The current `message`
    # is NOT included here — the backend appends it after the history.
    history: list[ChatTurn] = Field(default_factory=list)


class ChatAction(BaseModel):
    """A clickable suggestion an agent surfaces — e.g. 'apply this filter'."""
    kind: Literal["apply_filter", "open_tab", "run_validation", "troubleshoot", "noop"]
    label: str
    target: str | None = None  # tile_id / run_id / model_id depending on kind
    payload: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    response: str
    agent_id: str
    agent_name: str
    agent_color: str | None = None
    agent_icon: str | None = None
    actions: list[ChatAction] = Field(default_factory=list)
    # Trace of tool calls / outputs / messages emitted during the run.
    # Surfaced for tune-style flows so the chat bubble can render a
    # checklist of what the agent did.
    trace: list["TraceStep"] = Field(default_factory=list)


class AgentInfo(BaseModel):
    id: str
    name: str
    description: str
    icon: str
    color: str


# ── Data Sources ────────────────────────────────────────────────────────────
class DataSource(BaseModel):
    id: str
    name: str
    type: Literal["snowflake", "onelake", "file_upload", "rest_api", "postgres", "s3"]
    status: Literal["connected", "disconnected", "pending", "error"]
    connection_string: str | None = None
    last_synced: str | None = None
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class DataSourceCreate(BaseModel):
    name: str
    type: Literal["snowflake", "onelake", "file_upload", "rest_api", "postgres", "s3"]
    description: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


# ── Agent Skills ────────────────────────────────────────────────────────────
class AgentSkill(BaseModel):
    id: str
    name: str
    description: str
    category: Literal["analytical", "reporting", "data", "risk", "custom"]
    enabled: bool
    instructions: str | None = None
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    source: Literal["builtin", "user", "pack"] = "builtin"
    pack_id: str | None = None  # populated when source == 'pack'


class AgentSkillCreate(BaseModel):
    name: str
    description: str
    category: Literal["analytical", "reporting", "data", "risk", "custom"]
    instructions: str | None = None
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)


class AgentSkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    category: Literal["analytical", "reporting", "data", "risk", "custom"] | None = None
    instructions: str | None = None
    tools: list[str] | None = None
    mcp_servers: list[str] | None = None
    enabled: bool | None = None


# ── Python Tools ────────────────────────────────────────────────────────────
class ToolParameter(BaseModel):
    name: str
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    description: str | None = None
    required: bool = True


class PythonTool(BaseModel):
    id: str
    name: str
    description: str
    parameters: list[ToolParameter] = Field(default_factory=list)
    python_source: str
    function_name: str
    enabled: bool = True
    last_test_result: dict[str, Any] | None = None
    source: Literal["builtin", "user", "pack"] = "user"
    pack_id: str | None = None  # populated when source == 'pack'


class PythonToolCreate(BaseModel):
    name: str
    description: str
    parameters: list[ToolParameter] = Field(default_factory=list)
    python_source: str
    function_name: str


class PythonToolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    parameters: list[ToolParameter] | None = None
    python_source: str | None = None
    function_name: str | None = None
    enabled: bool | None = None


class ToolTestRequest(BaseModel):
    args: dict[str, Any] = Field(default_factory=dict)


class ToolDraftRequest(BaseModel):
    prompt: str  # plain-English description of what the tool should do
    context: str | None = None  # optional extra context (existing tools, data sources)


class ToolDraftResponse(BaseModel):
    name: str
    description: str
    function_name: str
    parameters: list[ToolParameter] = Field(default_factory=list)
    python_source: str
    notes: str | None = None  # any caveats the agent wants to flag


# ── Self-serve Analytics ────────────────────────────────────────────────────
# A user-defined analytic is a small JSON spec the runner translates into
# pandas operations (Aggregate, Compare) or executes as user-supplied Python
# (CustomPython). The shape is intentionally generic so it works across any
# business function — the user binds their own datasets and dimensions.

class AnalyticInputs(BaseModel):
    """Inputs are references to existing artifacts in the workspace.
    Most primitives use a single dataset; Compare uses two; CustomPython
    can take an arbitrary list."""
    dataset_id: str | None = None
    dataset_id_b: str | None = None  # for Compare
    dataset_ids: list[str] = Field(default_factory=list)  # for CustomPython
    run_id: str | None = None
    scenario_id: str | None = None


class AggregateMeasure(BaseModel):
    column: str
    agg: Literal["sum", "avg", "count", "min", "max", "median",
                 "p25", "p75", "p90", "p99", "weighted_avg", "stddev"]
    alias: str | None = None
    weight_by: str | None = None  # required when agg == 'weighted_avg'


class AggregateSpec(BaseModel):
    group_by: list[str] = Field(default_factory=list)
    measures: list[AggregateMeasure] = Field(default_factory=list)
    filters: list[dict[str, Any]] = Field(default_factory=list)  # [{column, op, value}]
    sort_by: str | None = None
    sort_desc: bool = True
    limit: int | None = 100


class CompareSpec(BaseModel):
    group_by: list[str] = Field(default_factory=list)
    measure: AggregateMeasure
    label_a: str = "A"
    label_b: str = "B"
    show_pct_change: bool = True


class CustomPythonSpec(BaseModel):
    function_name: str = "run"
    python_source: str
    # The function receives a dict mapping each input dataset id → pandas
    # DataFrame and must return a dict shaped like AnalyticResult below.


class PlotStyle(BaseModel):
    """Visual customization overlay applied to plots and tables.

    Honored by both `PlotConfig` (Reporting tiles) and `AnalyticOutput`
    (Analytics chart cards). The agent's `plot-tuner` skill mutates fields
    on this block; the renderer reads them on every render."""
    palette: list[str] = Field(default_factory=list)  # hex colors, in series order
    font_size: int | None = None  # pixel size for axis labels + legend
    title: str | None = None  # chart title (overrides spec name)
    x_axis_label: str | None = None
    y_axis_label: str | None = None
    number_format: str | None = None  # e.g. "0.00", "$0,0.00", "0.0%", "0.0a"
    label_overrides: dict[str, str] = Field(default_factory=dict)  # {field_name → display label}
    legend_position: Literal["top", "bottom", "right", "left", "none"] | None = None
    sort_field: str | None = None  # frontend-side sort applied before render
    sort_desc: bool = False


class AnalyticOutput(BaseModel):
    chart_type: Literal["bar", "line", "area", "stacked_bar", "scatter", "pie", "table", "kpi"] = "bar"
    x_field: str | None = None
    y_fields: list[str] = Field(default_factory=list)
    description: str | None = None
    style: PlotStyle = Field(default_factory=PlotStyle)


class AnalyticDefinition(BaseModel):
    id: str
    function_id: str
    name: str
    description: str = ""
    kind: Literal["aggregate", "compare", "custom_python"]
    inputs: AnalyticInputs = Field(default_factory=AnalyticInputs)
    aggregate_spec: AggregateSpec | None = None
    compare_spec: CompareSpec | None = None
    custom_python_spec: CustomPythonSpec | None = None
    output: AnalyticOutput = Field(default_factory=AnalyticOutput)
    parameters: dict[str, Any] = Field(default_factory=dict)  # user-tunable knobs
    created_at: str
    updated_at: str | None = None
    created_by: str | None = None


class AnalyticDefinitionCreate(BaseModel):
    function_id: str
    name: str
    description: str = ""
    kind: Literal["aggregate", "compare", "custom_python"]
    inputs: AnalyticInputs = Field(default_factory=AnalyticInputs)
    aggregate_spec: AggregateSpec | None = None
    compare_spec: CompareSpec | None = None
    custom_python_spec: CustomPythonSpec | None = None
    output: AnalyticOutput = Field(default_factory=AnalyticOutput)
    parameters: dict[str, Any] = Field(default_factory=dict)


class AnalyticDefinitionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    kind: Literal["aggregate", "compare", "custom_python"] | None = None
    inputs: AnalyticInputs | None = None
    aggregate_spec: AggregateSpec | None = None
    compare_spec: CompareSpec | None = None
    custom_python_spec: CustomPythonSpec | None = None
    output: AnalyticOutput | None = None
    parameters: dict[str, Any] | None = None


class AnalyticResultTable(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


class AnalyticResultChart(BaseModel):
    type: str
    x_field: str | None = None
    y_fields: list[str] = Field(default_factory=list)
    data: list[dict[str, Any]] = Field(default_factory=list)
    style: PlotStyle = Field(default_factory=PlotStyle)


class AnalyticResultKpi(BaseModel):
    label: str
    value: str
    sublabel: str | None = None


class AnalyticResult(BaseModel):
    table: AnalyticResultTable | None = None
    chart: AnalyticResultChart | None = None
    kpis: list[AnalyticResultKpi] = Field(default_factory=list)


class AnalyticDefinitionRun(BaseModel):
    id: str
    definition_id: str
    function_id: str
    name: str  # snapshot of definition name
    kind: str
    status: Literal["completed", "failed"]
    result: AnalyticResult | None = None
    narrative: str | None = None
    error: str | None = None
    created_at: str
    duration_ms: float = 0.0


class AnalyticDraftRequest(BaseModel):
    function_id: str
    prompt: str
    available_datasets: list[dict[str, Any]] = Field(default_factory=list)


class AnalyticDraftResponse(BaseModel):
    name: str
    description: str
    kind: Literal["aggregate", "compare", "custom_python"]
    inputs: AnalyticInputs = Field(default_factory=AnalyticInputs)
    aggregate_spec: AggregateSpec | None = None
    compare_spec: CompareSpec | None = None
    custom_python_spec: CustomPythonSpec | None = None
    output: AnalyticOutput = Field(default_factory=AnalyticOutput)
    notes: str | None = None


class AnalyticNarrationResponse(BaseModel):
    markdown: str


class ToolTestResponse(BaseModel):
    ok: bool
    result: Any = None
    error: str | None = None
    traceback: str | None = None
    duration_ms: float = 0.0


# ── Datasets ────────────────────────────────────────────────────────────────
class DatasetColumn(BaseModel):
    name: str
    dtype: str  # pandas dtype string (e.g. 'int64', 'float64', 'object')
    nullable: bool = True


class Dataset(BaseModel):
    id: str
    function_id: str
    name: str
    description: str | None = None
    source_kind: Literal["upload", "sql_table"]
    # Where this dataset sits in the workflow lifecycle. `input` = a
    # source the canvas reads from; `output` = a destination the
    # workflow writes to. Defaults to `input` so existing datasets keep
    # their current behavior.
    dataset_role: Literal["input", "output"] = "input"
    data_source_id: str | None = None
    table_ref: str | None = None
    # Optional SQL filter applied at read time when the dataset is
    # backed by a SQL table — e.g. `SELECT * FROM <table_ref> WHERE
    # as_of_date >= '2026-04-01'`. None = read the whole table.
    sql_query: str | None = None
    file_path: str | None = None
    file_format: Literal["csv", "parquet", "xlsx", "xls", "json"] | None = None
    columns: list[DatasetColumn] = Field(default_factory=list)
    row_count: int | None = None
    size_bytes: int | None = None
    created_at: str
    last_synced: str | None = None
    pack_id: str | None = None  # set when seeded by a domain pack


class DatasetCreateFromTable(BaseModel):
    function_id: str
    name: str
    description: str | None = None
    data_source_id: str
    # For OneLake / Snowflake bindings the SQL query is the binding
    # contract — `table_ref` is just a friendly label (defaults to the
    # data-source name + "(custom SQL)" when omitted). For S3 bindings
    # `table_ref` carries the s3:// URL the workflow reads from.
    table_ref: str | None = None
    dataset_role: Literal["input", "output"] = "input"
    sql_query: str | None = None


class DraftSqlRequest(BaseModel):
    """Ask the agent to draft a SQL filter for a bound table from a
    natural-language description."""
    table_ref: str
    description: str
    columns: list[str] = Field(default_factory=list)


class DraftSqlResponse(BaseModel):
    sql_query: str
    note: str | None = None  # e.g. "drafted by agent" / "stub fallback"


class DatasetPreview(BaseModel):
    dataset_id: str
    columns: list[DatasetColumn]
    sample_rows: list[dict[str, Any]]
    total_rows: int | None = None
    truncated: bool = False


# ── Models ──────────────────────────────────────────────────────────────────
class ModelMetric(BaseModel):
    name: str
    value: float
    asof: str


class TrainedModel(BaseModel):
    id: str
    function_id: str
    name: str
    description: str | None = None
    source_kind: Literal["upload", "regression", "uri"]
    model_type: Literal["ols", "logistic", "uploaded", "external"]
    target_column: str | None = None
    feature_columns: list[str] = Field(default_factory=list)
    coefficients: dict[str, float] | None = None
    intercept: float | None = None
    train_metrics: dict[str, float] = Field(default_factory=dict)
    monitoring_metrics: list[ModelMetric] = Field(default_factory=list)
    dataset_id: str | None = None
    artifact_path: str | None = None
    artifactory_uri: str | None = None
    file_format: str | None = None
    size_bytes: int | None = None
    introspection: dict[str, Any] | None = None
    created_at: str
    last_run: str | None = None
    pack_id: str | None = None  # set when seeded by a domain pack
    # ── Workflow-execution config (for uploaded artifacts) ────────────────
    # `feature_mapping` resolves the model's expected input names against
    # the dataset columns at run time: {model_input_name: csv_column_name}.
    # Empty mapping → fall back to identity match by name (case-insensitive).
    feature_mapping: dict[str, str] = Field(default_factory=dict)
    # Optional Python expression executed inside the sandbox before feature
    # extraction. The expression receives a pandas DataFrame as `df` and is
    # expected to either mutate `df` in place or assign the result back to
    # `df`. Used for engineered features (lags, first differences, etc.).
    pre_transform: str | None = None
    # Output kind tells the post-processor what shape the model returns:
    #   - "scalar"               — `model.predict(X)` → 1-D vector, one row per row.
    #   - "probability_vector"   — `model.predict_proba(X)` → (rows, classes).
    #   - "n_step_forecast"      — single-row input, model returns `forecast_steps` values.
    #   - "multi_target"         — `model.predict(X)` → (rows, targets).
    output_kind: Literal["scalar", "probability_vector", "n_step_forecast", "multi_target"] = "scalar"
    class_labels: list[str] = Field(default_factory=list)   # for probability_vector
    target_names: list[str] = Field(default_factory=list)   # for multi_target
    forecast_steps: int | None = None                       # for n_step_forecast


# ── Transforms (ETL between Data Source and Model) ─────────────────────────
class TransformParameter(BaseModel):
    """A single user-tunable knob exposed by a Transform recipe."""
    name: str                                # machine name, e.g. "scenario_severity"
    label: str                               # display label, e.g. "Scenario Severity"
    type: Literal["string", "number", "select", "boolean"]
    default: Any | None = None
    options: list[str] = Field(default_factory=list)  # for type="select"
    description: str | None = None


class Transform(BaseModel):
    """A registered ETL step a workflow node can reference.

    A Transform pulls rows from one or more Data Sources, applies a
    recipe, and materializes the result as a Dataset that downstream
    models consume. For the canvas demo the recipe is read-only; the
    transform's `output_dataset_id` points at a pre-staged dataset and
    "execution" reduces to passing that dataset through to consumers."""
    id: str
    function_id: str
    name: str
    description: str | None = None
    # Upstream — which configured Data Sources this transform reads from.
    # IDs reference entries in `_DATA_SOURCES` (datasources router).
    input_data_source_ids: list[str] = Field(default_factory=list)
    # Downstream — the Dataset id this transform materializes to.
    output_dataset_id: str | None = None
    # Recipe: the Python source the transform runs (read-only in v1; the
    # UI shows it for transparency, the orchestrator doesn't execute it
    # because the result is pre-staged).
    recipe_python: str | None = None
    # Tunable parameters surfaced to the analyst.
    parameters: list[TransformParameter] = Field(default_factory=list)
    source: Literal["builtin", "user", "pack"] = "user"
    pack_id: str | None = None
    created_at: str


class RegressionRequest(BaseModel):
    function_id: str
    name: str
    description: str | None = None
    dataset_id: str
    target_column: str
    feature_columns: list[str]
    model_type: Literal["ols", "logistic"] = "ols"


class FromUriRequest(BaseModel):
    function_id: str
    name: str
    description: str | None = None
    artifactory_uri: str
    model_type: Literal["uploaded", "external"] = "external"


class FromArtifactoryRequest(BaseModel):
    """Install a model packaged as a pip-installable Python package.

    Backend runs `pip install --no-binary <pkg> <pkg>` (forces the
    `.tar.gz` sdist that the corporate Artifactory publishes), imports
    the package, picks a class with a `.predict()` method, instantiates
    it, pickles the instance, and registers a TrainedModel pointing at
    that artifact so the canvas + sandbox flow is identical to file
    uploads.

    **Output shape** (`output_kind`, `target_names`, `class_labels`) is
    auto-detected from the installed class — the library author declares
    these as class attributes; the analyst doesn't fill them in here.
    Per-run knobs like `forecast_steps` are set when the analyst wires
    the model into a workflow, not at install time.
    """
    function_id: str
    name: str
    package_name: str
    description: str | None = None
    # Optional class hint — if the package exports multiple classes with
    # `.predict()`, the analyst can pin which one to instantiate.
    class_name: str | None = None


# ── Scenarios & Analytics Runs ──────────────────────────────────────────────
class Scenario(BaseModel):
    id: str
    function_id: str | None = None  # None = global / org-wide
    name: str
    description: str | None = None
    severity: Literal["base", "adverse", "severely_adverse", "outlook", "custom"] = "custom"
    source_kind: Literal["upload", "sql_table", "builtin"]
    dataset_id: str | None = None  # for upload / sql_table
    variables: list[str] = Field(default_factory=list)  # macro variables present
    horizon_months: int | None = None
    created_at: str


class ScenarioCreateFromDataset(BaseModel):
    function_id: str | None = None
    name: str
    description: str | None = None
    severity: Literal["base", "adverse", "severely_adverse", "outlook", "custom"] = "custom"
    dataset_id: str


class AnalyticsRun(BaseModel):
    id: str
    function_id: str
    name: str
    model_id: str
    scenario_id: str | None = None
    dataset_id: str | None = None
    input_kind: Literal["scenario", "dataset", "workflow"] = "scenario"
    workflow_id: str | None = None
    workflow_step_index: int | None = None
    input_node_ids: list[str] = Field(default_factory=list)
    horizon_months: int = 12
    status: Literal["completed", "failed", "running"]
    summary: dict[str, Any] = Field(default_factory=dict)
    series: list[dict[str, Any]] = Field(default_factory=list)
    notes: str | None = None
    error: str | None = None
    created_at: str
    duration_ms: float = 0.0


class RunRequest(BaseModel):
    function_id: str
    name: str | None = None
    model_id: str
    scenario_id: str | None = None
    dataset_id: str | None = None
    horizon_months: int = 12
    notes: str | None = None


# ── Workflow runs (multi-node DAG) ─────────────────────────────────────────
class WorkflowNode(BaseModel):
    id: str  # client-side node id
    kind: Literal["dataset", "scenario", "model", "destination", "transform"]
    ref_id: str  # dataset/scenario/model/transform id, OR destination kind for destination nodes
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowEdge(BaseModel):
    source: str  # source node id
    target: str  # target node id


class WorkflowRequest(BaseModel):
    function_id: str
    name: str | None = None
    nodes: list[WorkflowNode]
    edges: list[WorkflowEdge]
    horizon_months: int = 12
    # Optional run-context — set from the Workflow tab's run controls.
    # `scenario_name` pins which CCAR / Outlook scenario the workflow is
    # being executed under (for run-history attribution); `start_date`
    # is the as-of starting month for the horizon. Both pass through to
    # the AnalyticsRun for visibility; runtime semantics are deferred.
    scenario_name: str | None = None
    start_date: str | None = None  # ISO date, e.g. "2026-04-01"
    notes: str | None = None


class DestinationWrite(BaseModel):
    node_id: str
    kind: Literal["snowflake_table", "onelake_table", "s3", "csv"]
    target: str
    upstream_model_id: str
    upstream_run_id: str
    rows_written: int
    status: Literal["written", "failed"]
    note: str | None = None
    # CSV writes return their data so the browser can trigger a download
    csv_filename: str | None = None
    csv_data: list[dict[str, Any]] | None = None


# ── Saved workflows (named graphs persisted across sessions) ───────────────
class SavedWorkflow(BaseModel):
    """A workflow the analyst saved by name. Stores everything the canvas
    needs to reconstruct the graph + run controls — nodes (with their
    canvas positions + per-node config), edges, the horizon, the chosen
    scenario, and the start date."""
    id: str
    function_id: str
    name: str
    description: str | None = None
    nodes: list[dict[str, Any]]   # opaque pass-through — frontend node shape
    edges: list[dict[str, Any]]   # opaque pass-through — frontend edge shape
    horizon_months: int = 12
    scenario_name: str | None = None
    start_date: str | None = None
    view: Literal["steps", "canvas", "spec"] = "canvas"
    created_at: str
    updated_at: str | None = None


class SavedWorkflowSummary(BaseModel):
    """Lightweight projection used by the Workflow tab's Saved-Workflows
    dropdown — no nodes/edges payload, just enough to render the row."""
    id: str
    function_id: str
    name: str
    description: str | None = None
    node_count: int
    edge_count: int
    created_at: str
    updated_at: str | None = None


class SavedWorkflowCreate(BaseModel):
    function_id: str
    name: str
    description: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    horizon_months: int = 12
    scenario_name: str | None = None
    start_date: str | None = None
    view: Literal["steps", "canvas", "spec"] = "canvas"


class WorkflowResult(BaseModel):
    workflow_id: str
    status: Literal["completed", "failed", "partial"]
    runs: list[AnalyticsRun] = Field(default_factory=list)
    destinations: list[DestinationWrite] = Field(default_factory=list)
    node_status: dict[str, Literal["idle", "running", "completed", "failed", "skipped"]] = Field(default_factory=dict)
    error: str | None = None
    # Structured error payload for the UI's "Run failed" card. Set when a
    # node fails — keys: code, node_id, node_label, step_index, what_happened,
    # how_to_fix, raw (the original detail dict, for the agent troubleshooter).
    error_detail: dict[str, Any] | None = None
    duration_ms: float = 0.0


class WorkflowValidationIssue(BaseModel):
    severity: Literal["error", "warning", "info"]
    message: str
    node_id: str | None = None
    # Machine-friendly tag the UI uses to render specific CTAs (e.g.
    # "FEATURE_MISMATCH" → button to open the dataset).
    code: str | None = None
    # One-line, user-actionable suggestion shown below the message.
    hint: str | None = None


class WorkflowValidationResult(BaseModel):
    ok: bool
    issues: list[WorkflowValidationIssue] = Field(default_factory=list)


# ── Data Services (Data tab) ───────────────────────────────────────────────
class DataServiceCard(BaseModel):
    """One card on the Data Services section. Field shape mirrors the
    frontend's `ServiceCardSpec` so the JSON serializes straight in."""
    id: str
    title: str
    subtitle: str
    description: str
    color: str          # hex, e.g. "#EA580C"
    icon: str           # lucide-react icon name (frontend resolves)
    tag: str
    agent_prompt: str = Field(alias="agent_prompt")
    # Optional deep links — populated for cards backed by a registry
    # entity. transform_id binds Data Harness / DQC cards to the
    # transform-explainer chat path; scenario_id drives the Eye / Preview
    # button on CCAR + Outlook cards.
    transform_id: str | None = None
    scenario_id: str | None = None

    model_config = {"populate_by_name": True}


class DataServicesIntegrationStatus(BaseModel):
    """Shown next to each section so the analyst can see whether the
    cards are backed by a live integration or by static fallbacks."""
    name: Literal["pa_common_tools", "onelake"]
    enabled: bool
    live: bool
    detail: str = ""


class DataServicesPayload(BaseModel):
    function_id: str
    predictive: list[DataServiceCard]
    predictive_status: DataServicesIntegrationStatus
    ccar_years: list[str]
    ccar_scenarios: dict[str, list[DataServiceCard]]
    ccar_status: DataServicesIntegrationStatus
    outlook: list[DataServiceCard]
    outlook_status: DataServicesIntegrationStatus


# ── Playbooks (analyst-defined agentic workflows) ──────────────────────────
class PlaybookPhaseInput(BaseModel):
    kind: Literal["dataset", "scenario", "phase_output", "prompt"]
    ref_id: str | None = None  # dataset_id / scenario_id / "phase-2"
    text: str | None = None    # for kind=='prompt'


class PlaybookPhase(BaseModel):
    id: str  # logical id within the playbook ("phase-1", "phase-2", ...)
    name: str
    skill_name: str            # references agent/skills/<name>.md
    instructions: str | None = None
    inputs: list[PlaybookPhaseInput] = Field(default_factory=list)
    gate: bool = False         # if True, wait for analyst approve/modify/reject
    # Phase ids this phase must wait for before running. Empty = run
    # at the start of the playbook (DAG root) OR run linearly after the
    # immediately preceding phase if no other phase declares it as a
    # dependency. The executor uses this to schedule independent
    # phases concurrently with `asyncio.gather`.
    depends_on: list[str] = Field(default_factory=list)


class Playbook(BaseModel):
    id: str
    function_id: str
    name: str
    description: str | None = None
    # Problem framing — analyst describes the analytical question the
    # playbook is meant to answer. Surfaced to every phase as
    # `[PROBLEM STATEMENT]` in the agent's context so the specialists
    # don't have to re-derive intent from the phase instructions alone.
    problem_statement: str | None = None
    # Document ids (relative paths under sample_docs/uploads/) the
    # analyst attached to the playbook. Files live in the universal
    # Knowledge Base store (router/documents.py) under
    # `playbook/<id>/`. Made discoverable to phase agents through
    # `[UPLOADED FILES]` context + the universal `rag_search` tool's
    # default scan.
    uploaded_file_ids: list[str] = Field(default_factory=list)
    phases: list[PlaybookPhase] = Field(default_factory=list)
    created_at: str
    updated_at: str | None = None


class PlaybookCreate(BaseModel):
    function_id: str
    name: str
    description: str | None = None
    problem_statement: str | None = None
    uploaded_file_ids: list[str] = Field(default_factory=list)
    phases: list[PlaybookPhase] = Field(default_factory=list)
    # Optional client-supplied id — lets the frontend pre-allocate an
    # id so it can scope file uploads under `playbook/<id>/` before the
    # playbook is persisted. Backend generates one when not provided.
    id: str | None = None


class PlaybookUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    problem_statement: str | None = None
    uploaded_file_ids: list[str] | None = None
    phases: list[PlaybookPhase] | None = None


class TraceStep(BaseModel):
    """One observable event from an agent's run — a tool call, its result, a
    sub-agent handoff, a reasoning block, or the final message. Rendered as a
    transparency timeline under each PhaseExecution."""
    kind: Literal["tool_call", "tool_output", "message", "reasoning", "handoff", "info"]
    label: str  # one-line summary, e.g. "called get_workspace" or "final message"
    detail: str | None = None  # JSON / text payload, possibly truncated
    tool_name: str | None = None
    agent_name: str | None = None  # which agent emitted this (handoff support)
    truncated: bool = False
    at: str | None = None  # ISO timestamp


class PhaseExecution(BaseModel):
    phase_id: str
    phase_name: str
    skill_name: str
    status: Literal["idle", "running", "awaiting_gate", "completed", "rejected", "failed"]
    output: str | None = None  # raw markdown / text response from the agent
    # Validated, parsed result of the agent run — populated when the
    # phase's skill_name has a registered result schema (see
    # routers/playbooks.py:_extract_structured_result). Downstream
    # phases receive this re-serialized as a clean JSON block in
    # `[Context]`, instead of the raw output text. Renderers prefer
    # this when present.
    structured_output: dict[str, Any] | None = None
    agent_id: str | None = None
    gate_decision: Literal["approve", "modify", "reject", "rerun"] | None = None
    gate_notes: str | None = None
    duration_ms: float = 0.0
    error: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    trace: list[TraceStep] = Field(default_factory=list)
    # Snapshot of this phase's previous attempt, captured by the gate
    # handler when a cascade-rerun resets the phase. Spliced into the
    # rerun's [Context] so the agent can see what it previously emitted
    # and mark items as remediated rather than re-flagging the same
    # issues. Only populated for the GATE-ISSUING phase (the one whose
    # findings triggered the analyst's rerun decision).
    prior_findings: str | None = None


# ── Typed phase results — used by the deposit pack pipeline ──────────────
# Each playbook skill that emits structured JSON has a corresponding
# Pydantic model here. The playbook executor parses the agent's raw
# text output into the matching model and stores it on
# PhaseExecution.structured_output. Downstream phases see a freshly
# re-serialized version, so they can never get tripped up by sloppy
# agent prose or JSON shape drift.

class ProductVariance(BaseModel):
    """One row of the variance walk's by_product table."""
    product: str
    total_variance_mm:  float
    rate_effect_mm:     float
    volume_effect_mm:   float
    mix_effect_mm:      float


class VarianceWalkAudit(BaseModel):
    """Pre-computed checks variance-analyst attaches so attribution-challenger
    doesn't have to re-derive them from the by_product table. Each field is
    a concrete signal the challenger's `audit_logic_rules` can act on
    without re-reading the source CSV."""
    # Unit-detection trace. The tool's outputs are always reported in $MM,
    # but the source CSV may carry monetary values in raw $1, $K, or $B.
    # `*_scale_to_mm` is the multiplier applied; `*_scale_note` explains
    # how it was inferred (suffix vs magnitude vs explicit override).
    balance_scale_to_mm:    float = 1.0
    metric_scale_to_mm:     float = 1.0
    # Rate-column divisor used to normalise to decimal (1/100 for %,
    # 1/10,000 for bps, 1.0 if the rate column is already in decimal).
    # Wrong choice shifts the formula by 100×; visible as a "computed
    # vs data" reconciliation gap of exactly 100×.
    rate_divisor_to_decimal: float = 100.0
    balance_scale_note:  str | None = None
    metric_scale_note:   str | None = None
    rate_scale_note:     str | None = None
    # Plain-English description of how data_ie_delta_mm + the V/M/R
    # effects were aggregated (so the analyst / challenger can confirm
    # we summed across all products and all snap_dates, not a slice).
    aggregation_method:  str | None = None
    # Materiality — products contributing > threshold% of |total_variance|.
    # The challenger flags any of these missing from methodology's top_movers.
    materiality_threshold_pct: float = 5.0
    material_products:    list[str] = Field(default_factory=list)
    immaterial_products:  list[str] = Field(default_factory=list)
    # Reconciliation diagnostics. By construction V+M+R should equal total
    # to the unrounded penny, but rounding individual fields can introduce
    # 1-cent residuals — both numbers exposed so the challenger can
    # distinguish "real bug" from "display rounding".
    reconciliation_v_plus_m_plus_r_diff_mm: float = 0.0
    reconciliation_by_product_sum_diff_mm:  float = 0.0
    formula_vs_data_gap_mm:                 float | None = None
    formula_vs_data_gap_pct:                float | None = None
    # Assumption flags worth a regulator's attention.
    period_factor_was_defaulted: bool = False
    fallback_scenarios_used:     bool = False
    # Data quality / coverage.
    products_count:   int = 0
    snap_date_count:  int = 0
    products_with_partial_data: list[str] = Field(default_factory=list)


class VarianceWalkResult(BaseModel):
    """Output of `variance-analyst`. Numbers come straight from
    `compute_variance_walk`."""
    current_scenario:   str
    benchmark_scenario: str
    metric:             str
    metric_requested:   str | None = None
    rate_var_name:      str | None = None
    balance_var_name:   str | None = None
    period_factor:      float | None = None
    csv_path_used:      str | None = None
    playbook_id:        str | None = None
    run_ids:            list[str] = Field(default_factory=list)
    snap_dates:         list[str] = Field(default_factory=list)
    # Computed via the sequential V/M/R framework — always reconciles
    # exactly: total_variance_mm = volume_effect_mm + mix_effect_mm + rate_effect_mm.
    total_variance_mm:  float
    rate_effect_mm:     float
    volume_effect_mm:   float
    mix_effect_mm:      float
    # When the source CSV carries its own interest_expense column, this
    # is the ΔIE read directly from that column. The decomposition
    # follows the computed value (balance × rate × period_factor); any
    # gap between this and `total_variance_mm` is surfaced in
    # `assumptions` so the analyst can see whether the model output
    # reconciles to the formula.
    data_ie_delta_mm:   float | None = None
    # Total IE for each scenario across all products + snap_dates,
    # in $MM. Used by the waterfall renderer to anchor the chart on
    # the baseline IE level (left bar) and end at the stress IE level
    # (right bar) — `current_ie_mm = benchmark_ie_mm + total_variance_mm`
    # by construction. Both formula-derived (sum of bal × rate × pf
    # per row) so the chart reconciles with the V/M/R decomposition.
    benchmark_ie_mm:    float | None = None
    current_ie_mm:      float | None = None
    by_product:         list[ProductVariance] = Field(default_factory=list)
    assumptions:        str | None = None
    # Structured audit signals attribution-challenger consumes. Nullable
    # for back-compat with older runs that predate this field.
    audit:              VarianceWalkAudit | None = None


class TopMover(BaseModel):
    """One ranked, material delta methodology-researcher hands to
    commentary-drafter. Single source of truth for "what's worth
    talking about" — replaces commentary-drafter independently
    re-deciding from `by_product`."""
    rank:                int
    product:             str
    total_variance_mm:   float
    contribution_pct:    float | None = None  # |this| / |total| as %
    primary_effect:      Literal["rate", "volume", "mix"] | None = None
    attribution_summary: str | None = None    # one-line "why" from methodology
    model_component:     str | None = None    # cited PRED_RETAIL... id


class Attribution(BaseModel):
    """Detailed per-driver attribution row."""
    driver:           str
    # Canonical values are 'methodology' / 'scenario' / 'portfolio'.
    # The validator below is tolerant of common variants the LLM
    # produces ('scenario input change', 'methodology change',
    # 'portfolio addition', etc.) and normalizes them to canonical.
    category:         str
    model_component:  str
    explanation:      str

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, v: str) -> str:
        s = str(v or "").strip().lower()
        if "method" in s:
            return "methodology"
        if "scen" in s or "input" in s:
            return "scenario"
        if "port" in s or "addition" in s:
            return "portfolio"
        raise ValueError(
            f"category must be 'methodology' / 'scenario' / 'portfolio' "
            f"(or a variant containing those keywords), got {v!r}"
        )


class AttributionsResult(BaseModel):
    """Output of `methodology-researcher`. `top_movers` is the curated
    short-list commentary-drafter narrates from."""
    current_scenario:   str
    benchmark_scenario: str
    top_movers:         list[TopMover] = Field(default_factory=list)
    attributions:       list[Attribution] = Field(default_factory=list)


class NumericClaim(BaseModel):
    """A specific number commentary-drafter is asserting in its prose,
    annotated with the source field in `VarianceWalkResult` so the
    backend can verify it exactly. The `text` field is how it appears
    in the prose; `value_mm` is the precise value being claimed."""
    text:         str   # e.g. "~$3.0B" — display form
    value_mm:     float # e.g. -3043.2 — exact value behind the display
    source_field: str   # e.g. "total_variance_mm" or "by_product[PSAV].rate_effect_mm"


class CommentaryResult(BaseModel):
    """Output of `commentary-drafter`. Includes structured numeric
    claims so the backend can verify each $ figure in the narrative
    against the variance JSON exactly (no tolerance bands)."""
    slide_header:        str
    primary_driver:      str
    secondary_drivers:   list[str] = Field(default_factory=list)
    overlay_impacts:     list[str] = Field(default_factory=list)
    numeric_claims:      list[NumericClaim] = Field(default_factory=list)
    # Populated by the backend, not the agent. True only when every
    # numeric_claim ties exactly to the variance JSON.
    numbers_verified:    bool = False
    verification_failures: list[str] = Field(default_factory=list)


class PlaybookRun(BaseModel):
    id: str
    playbook_id: str
    playbook_name: str
    function_id: str
    status: Literal["running", "awaiting_gate", "completed", "rejected", "failed"]
    phases: list[PhaseExecution] = Field(default_factory=list)
    current_phase_idx: int = 0
    final_report: str | None = None
    created_at: str
    completed_at: str | None = None


class GateDecisionRequest(BaseModel):
    decision: Literal["approve", "modify", "reject", "rerun"]
    notes: str | None = None
    modified_output: str | None = None
    # When `decision="rerun"`, this is the analyst's feedback for the
    # agent — appended to the phase's `[Context]` as
    # `[ANALYST FEEDBACK ON PRIOR ATTEMPT]\n<text>` before re-running.
    feedback: str | None = None
    # When `decision="rerun"` AND the analyst wants to rerun an UPSTREAM
    # phase (not the gate phase itself). The named phase + every phase
    # that transitively depends on it (including the gate phase) is
    # reset to idle and the DAG scheduler picks it up. The `feedback`
    # is attached to the upstream target's `gate_notes` so it gets
    # spliced into that phase's [Context]. Common pattern:
    # attribution-challenger gates, finds an attribution mistake whose
    # root cause is in variance-analyst's output → analyst sets
    # `rerun_from_phase_id` to the variance-analyst phase id.
    rerun_from_phase_id: str | None = None
    # Which phase the decision applies to. Required when multiple
    # phases can be awaiting a gate simultaneously (parallel siblings).
    # If omitted, the backend defaults to the first phase whose status
    # is `awaiting_gate` — preserves single-gate / linear behavior.
    phase_id: str | None = None


class PublishedReport(BaseModel):
    id: str
    function_id: str
    playbook_id: str
    playbook_name: str
    run_id: str
    title: str
    body_markdown: str
    published_by: str
    published_at: str


class PublishRequest(BaseModel):
    title: str | None = None


# ── Plot Builder ────────────────────────────────────────────────────────────
class PlotConfig(BaseModel):
    id: str
    function_id: str | None = None
    name: str
    tile_type: Literal["plot", "table", "kpi"] = "plot"
    chart_type: Literal["line", "bar", "area", "pie", "scatter", "stacked_bar"] = "line"
    data_source_id: str | None = None
    dataset_id: str | None = None
    run_id: str | None = None
    pinned_to_overview: bool = False
    # For table tiles
    table_columns: list[str] | None = None
    table_default_sort: str | None = None
    table_default_sort_desc: bool = False
    # For plot tiles
    x_field: str = ""
    y_fields: list[str] = Field(default_factory=list)
    aggregation: Literal["sum", "avg", "count", "min", "max", "none"] = "none"
    filters: list[dict[str, Any]] = Field(default_factory=list)
    description: str | None = None
    style: PlotStyle = Field(default_factory=PlotStyle)
    # For KPI tiles — read a single number from a column. `kpi_aggregation`
    # picks how to reduce many rows to one; `weighted_avg` requires
    # `kpi_weight_field`. The display value is `kpi_prefix + (value *
    # kpi_scale formatted to kpi_decimals) + kpi_suffix`.
    kpi_field: str = ""
    kpi_aggregation: Literal["sum", "avg", "weighted_avg", "latest", "min", "max", "count"] = "sum"
    kpi_weight_field: str | None = None
    kpi_latest_field: str | None = None  # for "latest", which column orders rows
    kpi_prefix: str = ""
    kpi_suffix: str = ""
    kpi_decimals: int = 2
    kpi_scale: float = 1.0
    kpi_sublabel: str | None = None


class PlotConfigCreate(BaseModel):
    function_id: str | None = None
    name: str
    tile_type: Literal["plot", "table", "kpi"] = "plot"
    chart_type: Literal["line", "bar", "area", "pie", "scatter", "stacked_bar"] = "line"
    data_source_id: str | None = None
    dataset_id: str | None = None
    run_id: str | None = None
    pinned_to_overview: bool = False
    table_columns: list[str] | None = None
    table_default_sort: str | None = None
    table_default_sort_desc: bool = False
    x_field: str = ""
    y_fields: list[str] = Field(default_factory=list)
    aggregation: Literal["sum", "avg", "count", "min", "max", "none"] = "none"
    filters: list[dict[str, Any]] = Field(default_factory=list)
    description: str | None = None
    kpi_field: str = ""
    kpi_aggregation: Literal["sum", "avg", "weighted_avg", "latest", "min", "max", "count"] = "sum"
    kpi_weight_field: str | None = None
    kpi_latest_field: str | None = None
    kpi_prefix: str = ""
    kpi_suffix: str = ""
    kpi_decimals: int = 2
    kpi_scale: float = 1.0
    kpi_sublabel: str | None = None

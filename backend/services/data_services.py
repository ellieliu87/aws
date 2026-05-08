"""Data Services aggregation layer.

Drives the **Data Services** section on the Data tab. Three groups of
cards: Scenario Service from Predictive Analytics (built-in tools),
CCAR (BHC + Fed scenarios per year), and Outlook (internal forward
view). Each group has two backing modes:

  - **static**: hard-coded specs in this file. Used by default so the
    app works end-to-end outside the corporate proxy environment.

  - **live**: pulled from a corporate integration —
      • Predictive Analytics tools ← `pa_common_tools` pip package
      • CCAR + Outlook scenarios   ← OneLake table extractor

Live mode is opt-in through env vars (see `backend/config/
data_services.example.env`). When a live integration fails at runtime
we log and fall back to the static spec so the UI never breaks.

Inside the proxy environment, flipping live mode on is purely a config
change — drop the env vars in place, replace the `_onelake_read_table`
stub with your corporate extractor, and the cards repopulate from the
real data.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("cma.data_services")


# ── Config (env-driven knobs) ───────────────────────────────────────────────
def _bool_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# pa_common_tools — corporate package shipping Data Harness + DQC.
PA_COMMON_TOOLS_ENABLED = _bool_env("CMA_PA_COMMON_TOOLS_ENABLED")
PA_COMMON_TOOLS_PACKAGE = os.getenv("CMA_PA_COMMON_TOOLS_PACKAGE", "pa_common_tools")

# OneLake — corporate lakehouse holding CCAR + Outlook scenario tables.
ONELAKE_SCENARIOS_ENABLED = _bool_env("CMA_ONELAKE_SCENARIOS_ENABLED")
ONELAKE_WORKSPACE         = os.getenv("CMA_ONELAKE_WORKSPACE", "Finance")
ONELAKE_LAKEHOUSE         = os.getenv("CMA_ONELAKE_LAKEHOUSE", "cma")
ONELAKE_CCAR_TABLE        = os.getenv("CMA_ONELAKE_CCAR_TABLE", "ccar_scenarios")
ONELAKE_OUTLOOK_TABLE     = os.getenv("CMA_ONELAKE_OUTLOOK_TABLE", "outlook_scenarios")


@dataclass
class ServiceCard:
    """One card on the Data Services section. Field shape matches the
    frontend's `ServiceCardSpec` so it serializes straight to JSON."""
    id: str
    title: str
    subtitle: str
    description: str
    color: str          # hex
    icon: str           # lucide-react icon name (frontend resolves)
    tag: str
    agent_prompt: str
    # Optional bindings the frontend uses to deep-link a card to the
    # right backend entity:
    #   transform_id  — Data Harness / DQC cards point at a Transform in
    #                   `_TRANSFORMS` so the chat panel can bind
    #                   entity_kind=transform and `get_transform_recipe`
    #                   reads the actual recipe.
    #   scenario_id   — CCAR / Outlook cards point at a Scenario in
    #                   `_SCENARIOS` so the Preview button can pull
    #                   `/api/analytics/scenarios/{id}/preview`.
    transform_id: str | None = None
    scenario_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── Static fallback specs (default behavior, used outside proxy env) ───────
_STATIC_PREDICTIVE_CARDS: list[ServiceCard] = [
    ServiceCard(
        id="data-harness",
        title="Data Harness",
        subtitle="ETL — preparation",
        description=(
            "Pulls raw drivers from OneLake / Snowflake / vendor feeds, joins them, "
            "engineers the features the model libraries expect, and materializes a "
            "clean dataset. Available as a Transform on the Workflow canvas."
        ),
        color="#EA580C",
        icon="WorkflowIcon",
        tag="BUILT-IN",
        agent_prompt=(
            "Explain how the Data Harness ETL works — what raw tables it reads from, "
            "what transforms it applies, and what shape it materializes."
        ),
        # Bound to the deposits-pack Data Harness transform so the chat
        # panel can route to transform-explainer with a real recipe to
        # read. If the deposits pack isn't installed for the function
        # the user's on, the agent will surface a "transform not found"
        # error and the analyst can pick a different one.
        transform_id="tr-deposits-data-harness",
    ),
    ServiceCard(
        id="data-quality-check",
        title="Data Quality Check",
        subtitle="Runtime — pre-model",
        description=(
            "Runs row-count, null-rate, range, and distribution checks on the inbound "
            "data right before models consume it. Fails the workflow fast when the "
            "data drifts outside acceptable bands."
        ),
        color="#0891B2",
        icon="ShieldCheck",
        tag="BUILT-IN",
        agent_prompt=(
            "Walk me through the Data Quality Check service — which checks run, "
            "where they fire in the workflow, and how to extend them."
        ),
        transform_id="tr-deposits-dqc",
    ),
]

_STATIC_CCAR_YEARS = ["2026", "2025"]

_SEVERITY_COLOR = {
    "base":             "#059669",
    "adverse":          "#D97706",
    "severely_adverse": "#DC2626",
}

_CCAR_TEMPLATE = [
    {
        "code": "BHCB",  "label": "BHC Base",   "severity": "base",
        "source": "BHC",
        "description": (
            "The bank's own baseline view — central case for rates, growth, "
            "unemployment, and credit. Used for budgeting and the consensus PPNR."
        ),
    },
    {
        "code": "BHCS",  "label": "BHC Stress", "severity": "adverse",
        "source": "BHC",
        "description": (
            "The bank's adverse story — moderate recession, rising unemployment, "
            "credit deterioration. Tighter than base, looser than the Fed severe path."
        ),
    },
    {
        "code": "FedB",  "label": "Fed Base",   "severity": "base",
        "source": "Fed",
        "description": (
            "Federal Reserve supervisory baseline — central case published with the "
            "CCAR scenarios. Required submission input for capital planning."
        ),
    },
    {
        "code": "FedSA", "label": "Fed Severely Adverse", "severity": "severely_adverse",
        "source": "Fed",
        "description": (
            "Federal Reserve severely adverse — deep recession, sharp asset-price "
            "declines, credit-spread blowout. The binding constraint for stress capital."
        ),
    },
]


def _ccar_card(year: str, row: dict[str, Any]) -> ServiceCard:
    """Adapt a CCAR row (static template OR OneLake row) into a ServiceCard."""
    code     = str(row["code"])
    label    = str(row["label"])
    severity = str(row.get("severity", "base"))
    source   = str(row.get("source", "BHC"))
    color    = _SEVERITY_COLOR.get(severity, "#059669")
    sid = f"ccar-{code.lower()}-{year}"
    return ServiceCard(
        id=sid,
        title=code,
        subtitle=f"{label} · {'Federal Reserve supervisory' if source == 'Fed' else 'BHC internal'}",
        description=str(row["description"]),
        color=color,
        icon="Landmark" if source == "Fed" else "Building2",
        tag=f"CCAR {year}",
        agent_prompt=(
            f"Explain the macro narrative in the \"CCAR {year} {label}\" scenario — "
            f"regime, rate path, credit/spreads, real economy, and key tail risks. "
            f"Severity: {severity}."
        ),
        scenario_id=sid,
    )


_STATIC_CCAR_BY_YEAR: dict[str, list[ServiceCard]] = {
    year: [_ccar_card(year, row) for row in _CCAR_TEMPLATE]
    for year in _STATIC_CCAR_YEARS
}

_STATIC_OUTLOOK_CARDS: list[ServiceCard] = [
    ServiceCard(
        id="outlook-ir",
        title="Internal Interest Rate Outlook",
        subtitle="Treasury · forward curve view",
        description=(
            "The bank's house view on the rate path — Fed funds, key UST tenors, "
            "spreads, and term-premium evolution. Used as the consensus input for "
            "budgeting and IR-risk planning when CCAR isn't the right lens."
        ),
        color="#0EA5E9",
        icon="TrendingUp",
        tag="OUTLOOK",
        agent_prompt=(
            "Explain the bank's internal Interest Rate Outlook — current view on "
            "Fed path, term premium, and the key swing factors for the next 12 months."
        ),
        scenario_id="outlook-ir",
    ),
]


# ── Live integration: pa_common_tools (Predictive Analytics) ───────────────
def _load_predictive_from_pa_common_tools() -> list[ServiceCard] | None:
    """Discover Data Harness + Data Quality Check in `pa_common_tools`.

    The corporate package is expected to expose entries by convention —
    classes or functions named `DataHarness` / `data_harness` and
    `DataQualityCheck` / `data_quality_check`. Returns a list of cards
    when discovery succeeds, or None to signal the caller should use the
    static fallback.
    """
    try:
        import importlib
        importlib.invalidate_caches()
        mod = importlib.import_module(PA_COMMON_TOOLS_PACKAGE)
    except ImportError as e:
        log.warning(
            "[data_services] CMA_PA_COMMON_TOOLS_ENABLED=1 but `import %s` failed: %s. "
            "Falling back to static cards.",
            PA_COMMON_TOOLS_PACKAGE, e,
        )
        return None

    def _resolve_entry(*candidate_names: str):
        for name in candidate_names:
            entry = getattr(mod, name, None)
            if entry is not None:
                return entry, name
        return None, None

    def _extract_description(entry, fallback: str) -> str:
        # Prefer an explicit `.description`, then the docstring, then fallback.
        desc = getattr(entry, "description", None)
        if isinstance(desc, str) and desc.strip():
            return desc.strip()
        doc = (getattr(entry, "__doc__", None) or "").strip()
        if doc:
            # First non-empty paragraph
            return doc.split("\n\n", 1)[0].strip()
        return fallback

    cards: list[ServiceCard] = []

    harness_entry, harness_name = _resolve_entry("DataHarness", "data_harness", "DATA_HARNESS")
    if harness_entry is not None:
        cards.append(ServiceCard(
            id="data-harness",
            title=getattr(harness_entry, "title", None) or "Data Harness",
            subtitle="ETL — preparation",
            description=_extract_description(
                harness_entry,
                _STATIC_PREDICTIVE_CARDS[0].description,
            ),
            color="#EA580C",
            icon="WorkflowIcon",
            tag=f"pa_common_tools · {harness_name}",
            agent_prompt=_STATIC_PREDICTIVE_CARDS[0].agent_prompt,
        ))

    dqc_entry, dqc_name = _resolve_entry("DataQualityCheck", "data_quality_check", "DATA_QUALITY_CHECK")
    if dqc_entry is not None:
        cards.append(ServiceCard(
            id="data-quality-check",
            title=getattr(dqc_entry, "title", None) or "Data Quality Check",
            subtitle="Runtime — pre-model",
            description=_extract_description(
                dqc_entry,
                _STATIC_PREDICTIVE_CARDS[1].description,
            ),
            color="#0891B2",
            icon="ShieldCheck",
            tag=f"pa_common_tools · {dqc_name}",
            agent_prompt=_STATIC_PREDICTIVE_CARDS[1].agent_prompt,
        ))

    if not cards:
        log.warning(
            "[data_services] `%s` imported but no Data Harness / DQC entries were "
            "found. Expected names: DataHarness, data_harness, DataQualityCheck, "
            "data_quality_check. Falling back to static cards.",
            PA_COMMON_TOOLS_PACKAGE,
        )
        return None
    return cards


# ── Live integration: OneLake extractor (CCAR + Outlook) ───────────────────
def _onelake_read_table(table: str, **filters: Any) -> list[dict[str, Any]]:
    """Pluggable OneLake table reader.

    ── PROXY-ENV INTEGRATION POINT ─────────────────────────────────────
    Outside the corporate proxy environment, this raises NotImplemented;
    the loaders catch it and fall back to static cards. Inside the proxy
    env, replace this body with your corporate OneLake extractor, e.g.:

        from your_corp_lib import OneLakeExtractor
        client = OneLakeExtractor(
            workspace=ONELAKE_WORKSPACE,
            lakehouse=ONELAKE_LAKEHOUSE,
        )
        return client.read_table(table_name=table, **filters)

    Or if the call is async, wrap it with `asyncio.run(...)` here so the
    sync interface is preserved.

    Expected row shapes:
      CCAR table:    {year, code, label, severity, source, description}
      Outlook table: {id, title, subtitle, description, color?, icon?, tag?}

    Filters supported by callers: `year` (CCAR only).
    """
    raise NotImplementedError(
        "OneLake extractor is not configured for this environment. "
        "Replace `_onelake_read_table` in backend/services/data_services.py "
        "with your corporate extractor, or set CMA_ONELAKE_SCENARIOS_ENABLED=0 "
        "to use the static fallback."
    )


def _load_ccar_years() -> list[str]:
    if not ONELAKE_SCENARIOS_ENABLED:
        return list(_STATIC_CCAR_YEARS)
    try:
        rows = _onelake_read_table(ONELAKE_CCAR_TABLE)
        years = sorted({str(r["year"]) for r in rows}, reverse=True)
        return years or list(_STATIC_CCAR_YEARS)
    except Exception:
        log.exception(
            "[data_services] OneLake year discovery failed (%s). Falling back to static years.",
            ONELAKE_CCAR_TABLE,
        )
        return list(_STATIC_CCAR_YEARS)


def _load_ccar_scenarios_for_year(year: str) -> list[ServiceCard]:
    if not ONELAKE_SCENARIOS_ENABLED:
        return list(_STATIC_CCAR_BY_YEAR.get(year, []))
    try:
        rows = _onelake_read_table(ONELAKE_CCAR_TABLE, year=year)
        return [_ccar_card(year, r) for r in rows] or list(_STATIC_CCAR_BY_YEAR.get(year, []))
    except Exception:
        log.exception(
            "[data_services] OneLake CCAR read failed (%s, year=%s). Falling back to static cards.",
            ONELAKE_CCAR_TABLE, year,
        )
        return list(_STATIC_CCAR_BY_YEAR.get(year, []))


def _load_outlook_cards() -> list[ServiceCard]:
    if not ONELAKE_SCENARIOS_ENABLED:
        return list(_STATIC_OUTLOOK_CARDS)
    try:
        rows = _onelake_read_table(ONELAKE_OUTLOOK_TABLE)
        cards = []
        for i, r in enumerate(rows):
            sid = str(r.get("id") or f"outlook-{i}")
            cards.append(ServiceCard(
                id=sid,
                title=str(r["title"]),
                subtitle=str(r.get("subtitle", "Treasury · outlook")),
                description=str(r["description"]),
                color=str(r.get("color") or "#0EA5E9"),
                icon=str(r.get("icon") or "TrendingUp"),
                tag=str(r.get("tag") or "OUTLOOK"),
                agent_prompt=str(r.get("agent_prompt") or (
                    f"Explain the \"{r['title']}\" outlook scenario — drivers, central "
                    "case, and key swing factors."
                )),
                scenario_id=sid,
            ))
        return cards or list(_STATIC_OUTLOOK_CARDS)
    except Exception:
        log.exception(
            "[data_services] OneLake Outlook read failed (%s). Falling back to static cards.",
            ONELAKE_OUTLOOK_TABLE,
        )
        return list(_STATIC_OUTLOOK_CARDS)


# ── Public API consumed by the router ──────────────────────────────────────
@dataclass
class IntegrationStatus:
    """Surfaced to the UI so the analyst can see whether a section is
    backed by the live integration or by the static fallback."""
    name: str
    enabled: bool
    live: bool          # enabled AND succeeded
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def list_predictive_cards() -> tuple[list[ServiceCard], IntegrationStatus]:
    if not PA_COMMON_TOOLS_ENABLED:
        return _STATIC_PREDICTIVE_CARDS, IntegrationStatus(
            name="pa_common_tools",
            enabled=False, live=False,
            detail="Static fallback (set CMA_PA_COMMON_TOOLS_ENABLED=1 to load live).",
        )
    cards = _load_predictive_from_pa_common_tools()
    if cards is None:
        return _STATIC_PREDICTIVE_CARDS, IntegrationStatus(
            name="pa_common_tools",
            enabled=True, live=False,
            detail=f"Enabled but `{PA_COMMON_TOOLS_PACKAGE}` discovery failed; using static fallback.",
        )
    return cards, IntegrationStatus(
        name="pa_common_tools",
        enabled=True, live=True,
        detail=f"Loaded from `{PA_COMMON_TOOLS_PACKAGE}` ({len(cards)} entries).",
    )


def list_ccar(year_filter: str | None = None) -> tuple[list[str], dict[str, list[ServiceCard]], IntegrationStatus]:
    """Returns (available years, scenarios keyed by year, status).
    `year_filter` lets the caller restrict the scenarios payload to a
    single year (cheaper when OneLake is large)."""
    years = _load_ccar_years()
    target_years = [year_filter] if year_filter and year_filter in years else years
    by_year = {y: _load_ccar_scenarios_for_year(y) for y in target_years}
    if ONELAKE_SCENARIOS_ENABLED:
        live = all(by_year[y] for y in target_years)
        status = IntegrationStatus(
            name="onelake",
            enabled=True, live=live,
            detail=(
                f"OneLake `{ONELAKE_WORKSPACE}/{ONELAKE_LAKEHOUSE}/{ONELAKE_CCAR_TABLE}` — "
                f"{'live' if live else 'failed; using static fallback'}."
            ),
        )
    else:
        status = IntegrationStatus(
            name="onelake",
            enabled=False, live=False,
            detail="Static fallback (set CMA_ONELAKE_SCENARIOS_ENABLED=1 to load live).",
        )
    return years, by_year, status


# ── Materialize into the legacy scenario registry ──────────────────────────
# The Workflow tab's Scenarios palette pulls from `routers/scenarios.py:_SCENARIOS`,
# and the orchestrator runs scenarios through `BUILTIN_DATA`'s wide-format
# paths. To keep the Workflow palette in sync with the Data tab's Data
# Services section, we push every CCAR (across all years) + Outlook card
# into both stores at startup with synthetic monthly paths shaped by
# severity. Variable names mirror the deposit pack's feature names so a
# CCAR scenario can flow straight into a MaaS model.

# Variable set the synthetic paths cover. Keep aligned with what the
# downstream models expect.
_SCENARIO_VARIABLES = [
    "fed_funds_pct", "ust_2y_pct", "ust_10y_pct",
    "unemployment_pct", "gdp_yoy_pct", "hpi_yoy_pct",
]


def _synthetic_paths(severity: str, n_months: int) -> dict[str, list[float]]:
    """Generate a deterministic monthly path keyed by severity. Realistic
    enough for a demo — base glides, adverse stresses, severely_adverse
    breaks, outlook is a soft positive."""
    import numpy as np  # local import — keeps top-level startup cheap
    t = np.arange(n_months)

    if severity == "base":
        ff = 4.50 - 0.04 * t + 0.02 * np.sin(t / 3.0)
        unemp = 3.80 + 0.02 * t
        gdp = 2.10 - 0.01 * t + 0.05 * np.cos(t / 4.0)
        hpi = 3.50 - 0.02 * t
    elif severity == "adverse":
        ff = 4.50 - 0.20 * np.minimum(t, 6) - 0.03 * np.maximum(t - 6, 0)
        unemp = 3.80 + 0.30 * np.minimum(t, 12) + 0.01 * np.maximum(t - 12, 0)
        gdp = 2.10 - 0.40 * np.minimum(t, 6) + 0.05 * np.maximum(t - 6, 0)
        hpi = 3.50 - 0.50 * np.minimum(t, 12) + 0.10 * np.maximum(t - 12, 0)
    elif severity == "severely_adverse":
        ff = np.clip(4.50 - 0.50 * np.minimum(t, 6), 0.10, None) - 0.05 * np.maximum(t - 6, 0)
        unemp = 3.80 + 0.60 * np.minimum(t, 12) + 0.02 * np.maximum(t - 12, 0)
        gdp = 2.10 - 0.80 * np.minimum(t, 4) + 0.10 * np.maximum(t - 4, 0)
        hpi = 3.50 - 1.20 * np.minimum(t, 12) + 0.20 * np.maximum(t - 12, 0)
    else:  # outlook
        ff = 4.50 - 0.04 * t
        unemp = 3.80 + 0.01 * t
        gdp = 2.10 - 0.005 * t
        hpi = 3.50 - 0.01 * t

    return {
        "fed_funds_pct":    [round(float(x), 3) for x in ff],
        "ust_2y_pct":       [round(float(x + 0.20), 3) for x in ff],
        "ust_10y_pct":      [round(float(x + 0.30), 3) for x in ff],
        "unemployment_pct": [round(float(x), 3) for x in unemp],
        "gdp_yoy_pct":      [round(float(x), 3) for x in gdp],
        "hpi_yoy_pct":      [round(float(x), 3) for x in hpi],
    }


def materialize_into_scenarios_registry(function_id: str | None = None) -> int:
    """Push CCAR (every year) + Outlook cards into `_SCENARIOS` + `BUILTIN_DATA`.

    Idempotent — calling twice for the same function is a no-op for ids
    already present. Returns the number of new scenarios added so the
    startup hook can log it.
    """
    from datetime import datetime
    from models.schemas import Scenario
    from routers.scenarios import _SCENARIOS, BUILTIN_DATA

    now = datetime.utcnow().isoformat() + "Z"
    horizon = 27
    n_added = 0

    # CCAR — one Scenario per (year, code). Severity comes from the
    # static template; live OneLake mode would override the rows.
    years = _load_ccar_years()
    for year in years:
        ccar_cards = _load_ccar_scenarios_for_year(year)
        # Re-walk the source rows so we keep the severity. The static
        # template has it; live rows must include `severity` in their dict.
        if not ONELAKE_SCENARIOS_ENABLED:
            rows = _CCAR_TEMPLATE
        else:
            try:
                rows = _onelake_read_table(ONELAKE_CCAR_TABLE, year=year)
            except Exception:
                rows = _CCAR_TEMPLATE
        sev_by_code = {r["code"]: r.get("severity", "base") for r in rows}

        for card in ccar_cards:
            sid = card.id  # e.g. "ccar-bhcb-2026"
            if sid in _SCENARIOS:
                continue
            severity = sev_by_code.get(card.title, "base")
            paths = _synthetic_paths(severity, horizon)
            BUILTIN_DATA[sid] = {
                "name": f"{card.title} {year}",
                "description": card.description,
                "severity": severity,
                "variables": list(paths.keys()),
                "horizon_months": horizon,
                "paths": paths,
            }
            _SCENARIOS[sid] = Scenario(
                id=sid,
                function_id=function_id,
                name=f"{card.title} {year}",
                description=card.description,
                severity=severity,  # type: ignore[arg-type]
                source_kind="builtin",
                variables=list(paths.keys()),
                horizon_months=horizon,
                created_at=now,
            )
            n_added += 1

    # Outlook
    outlook_cards, _ = list_outlook()
    for card in outlook_cards:
        sid = card.id  # e.g. "outlook-ir"
        if sid in _SCENARIOS:
            continue
        paths = _synthetic_paths("outlook", horizon)
        BUILTIN_DATA[sid] = {
            "name": card.title,
            "description": card.description,
            "severity": "outlook",
            "variables": list(paths.keys()),
            "horizon_months": horizon,
            "paths": paths,
        }
        _SCENARIOS[sid] = Scenario(
            id=sid,
            function_id=function_id,
            name=card.title,
            description=card.description,
            severity="outlook",
            source_kind="builtin",
            variables=list(paths.keys()),
            horizon_months=horizon,
            created_at=now,
        )
        n_added += 1

    return n_added


def list_outlook() -> tuple[list[ServiceCard], IntegrationStatus]:
    cards = _load_outlook_cards()
    if ONELAKE_SCENARIOS_ENABLED:
        # The CCAR status carries the same OneLake state — duplicate here so
        # each section can render its own badge independently.
        live = bool(cards) and cards != _STATIC_OUTLOOK_CARDS
        status = IntegrationStatus(
            name="onelake",
            enabled=True, live=live,
            detail=(
                f"OneLake `{ONELAKE_WORKSPACE}/{ONELAKE_LAKEHOUSE}/{ONELAKE_OUTLOOK_TABLE}` — "
                f"{'live' if live else 'failed; using static fallback'}."
            ),
        )
    else:
        status = IntegrationStatus(
            name="onelake",
            enabled=False, live=False,
            detail="Static fallback (set CMA_ONELAKE_SCENARIOS_ENABLED=1 to load live).",
        )
    return cards, status

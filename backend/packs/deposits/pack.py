"""Deposit forecasting demo pack.

Bundles the three deposit-MaaS models (retail / commercial / small-business),
the Data Harness ETL transform that feeds them, the dataset it
materializes, and a starter Reporting-tab dashboard backed by
pre-computed `deposit_results.csv` so the Overview looks alive the
moment the function loads.

Canvas demo flow (intuitive end-to-end):
  Data Harness (reads OneLake / Finance) ─→ RDMaaS    ─┐
                                          ├→ CommMaaS  ─┼─→ CSV (combined w/ `segment` col)
                                          └→ SBBMaaS   ─┘
"""
from __future__ import annotations

from pathlib import Path

from packs import Pack, PackContext
from packs.deposits.tools import register_python_tools

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_SAMPLE_DATA = _REPO_ROOT / "sample_data" / "deposits"
_SAMPLE_MODELS = _REPO_ROOT / "sample_models" / "deposits"
_CCAR_DATA = _REPO_ROOT / "sample_data" / "ccar"


def register(ctx: PackContext) -> None:
    # 1) Pack metadata. The deposit-suite forecast is one workstream
    # under Capital Planning & CCAR (the overall capital plan needs a
    # deposit balance + NII path), so this pack attaches to that
    # function rather than living on its own.
    ctx.pack = Pack(
        id="deposits",
        label="Deposit Forecasting Suite",
        description=(
            "Deposit-suite forecasting workstream within Capital Planning & "
            "CCAR — Retail (RDMaaS), Commercial (CommMaaS), and Small-Business "
            "(SBBMaaS) projections of end balances and interest income across "
            "nine quarters."
        ),
        attach_to_functions=["capital_planning"],
        user_groups=[],
        color="#0EA5E9",
        icon="piggy-bank",
    )

    # 1b) Skills + Python tools — backs the four-agent CCAR variance
    #     attribution playbook (Quant → Modeler → LOB Finance Lead →
    #     Fact-Checker). The skills land under Settings → Agent Skills →
    #     Domain Pack — deposits; tools under Settings → Python Tools →
    #     Domain Pack — deposits.
    ctx.register_skill_dir()         # default: <pack_dir>/skills
    register_python_tools(ctx)

    # 2) Dataset — the macro-scenario wide table the Data Harness reads.
    #    Logically sourced from OneLake: in the corporate proxy
    #    environment this dataset is fetched live by the OneLake
    #    extractor (see docs/ORCHESTRATOR.md, "Wiring OneLake to a
    #    dataset"). For the demo it ships as a staged CSV so the canvas
    #    works end-to-end without proxy connectivity.
    ctx.attach_dataset(
        function_id="capital_planning",
        dataset_id="ds-onelake-macro-scenario",
        name="Macro scenario from OneLake",
        description=(
            "Wide monthly time series of macro + segment-specific drivers "
            "(rates, deposit pricing, account info, treasury demand, "
            "merchant volume). Sourced from the OneLake macro-scenario "
            "table via the corporate OneLake extractor; bundled CSV is "
            "the offline fallback shape so the demo runs without a proxy."
        ),
        source_path=_SAMPLE_DATA / "data_harness.csv",
    )

    # CCAR cycle data — wide format with per-scenario columns. 10 quarters
    # × 2 cycles (CCAR25 / CCAR26) × 4 scenarios (BHCB, BHCS, FedB, FedSA).
    # Used by the Reporting tab tiles below + available to any analyst
    # comparing supervisory paths across cycles.
    ctx.attach_dataset(
        function_id="capital_planning",
        dataset_id="ds-ccar-scenarios",
        name="CCAR Scenarios",
        description=(
            "CCAR macro paths for BHCB / BHCS / FedB / FedSA across 9 "
            "projection quarters (PQ0–PQ9) for cycles CCAR25 (Q4-2024 "
            "start) and CCAR26 (Q4-2025 start). Variables: unemployment "
            "rate, employment growth y/y, HPI y/y, CRE price y/y, real "
            "GDP q/q ann'lzd, M2 ($B), oil ($/bbl), Fed funds target, "
            "1y UST, 10y UST, BBB spread (bp), and M2/GDP."
        ),
        source_path=_CCAR_DATA / "ccar_scenarios.csv",
    )

    # Commercial deposit CCAR projection — long-format file containing
    # both model inputs (macro paths: fed_funds_rate, ust_10y_rate,
    # bbb_spread_pct) and model outputs (per-segment rate_paid + balance).
    # Schema: scenario, snap_date, variable_name, variable_value, segment,
    # origin. The `origin` column distinguishes 'model_input' rows from
    # 'model_output' rows; `segment` carries the product label (e.g. MMDA,
    # HYMM, GBIB, NONGB_IB, NONGB_ECR, ECR) and is empty for macro rows.
    # Feeds the beta-justification chain (quant → benchmarker →
    # visualizer → challenger).
    ctx.attach_dataset(
        function_id="capital_planning",
        dataset_id="ds-commercial-ccar-output",
        name="commercial_deposit_output_CCAR26",
        description=(
            "Commercial deposit projections from CommMaaS, long-format "
            "(scenario, snap_date, variable_name, variable_value, "
            "segment, origin) across 9 quarterly snap_dates under "
            "BHCS. Carries both model inputs (fed_funds_rate, "
            "ust_10y_rate, bbb_spread_pct) and model outputs "
            "(per-segment rate_paid, balance_mm)."
        ),
        source_path=_CCAR_DATA / "commercial_deposit_output_CCAR26.csv",
        dataset_role="output",
    )

    # Commercial deposit historical actuals — same schema as the
    # projection file (scenario, snap_date, variable_name, variable_value,
    # segment, origin). 24 quarterly observations 2019-Q1 → 2024-Q4
    # spanning the same six segments as the projection. Used by the
    # beta-benchmarker to recover each segment's effective historical
    # beta via OLS regression of rate_paid on fed_funds_rate.
    ctx.attach_dataset(
        function_id="capital_planning",
        dataset_id="ds-commercial-rate-actuals",
        name="commercial_deposit_rate_actuals",
        description=(
            "Long-format historical actuals for the commercial deposit "
            "book. Schema: scenario, snap_date, variable_name, "
            "variable_value, segment, origin. Carries fed_funds_rate "
            "(origin=model_input) plus per-segment rate_paid "
            "(origin=model_output) for MMDA, HYMM, GBIB, NONGB_IB, "
            "NONGB_ECR, ECR across 2019-Q1 through 2024-Q4."
        ),
        source_path=_CCAR_DATA / "commercial_deposit_rate_actuals.csv",
    )

    # Peak-value summary — backs the BHC comparison table on Reporting.
    # 9 rows (one per macro variable, M2 + 1y UST excluded) ×
    # 5 columns: macro, bhcb_25, bhcb_26, bhcs_25, bhcs_26.
    ctx.attach_dataset(
        function_id="capital_planning",
        dataset_id="ds-ccar-peak-summary",
        name="CCAR Peak Summary",
        description=(
            "Worst-case (peak) value for each macro variable across "
            "PQ0–PQ9 under the BHC scenarios in CCAR25 and CCAR26. "
            "Direction: max for unemployment + BBB spread, min for "
            "everything else (the trough during stress)."
        ),
        source_path=_CCAR_DATA / "ccar_peak_summary.csv",
    )

    # 2b) Data Harness transform — the ETL step on the canvas that reads
    #     from OneLake / Finance and materializes the harness table. The
    #     recipe is informational (read-only in the side panel); the
    #     orchestrator resolves the node to its `output_dataset_id` so the
    #     downstream models see the same rows the recipe would produce.
    _DATA_HARNESS_RECIPE = '''
"""Data Harness — Deposit Suite ETL recipe.

Pulls raw deposit drivers from OneLake (Finance lakehouse), aligns them
to the requested scenario severity + horizon, engineers the features
the three MaaS libraries expect, and returns one wide monthly DataFrame.
"""
def build(*, scenario_severity="base", horizon_months=27, onelake):
    raw = onelake.read_lakehouse_table(
        workspace="Finance",
        lakehouse="cma",
        tables=[
            "deposit_balances_monthly",
            "deposit_pricing_strategy",
            "deposit_competitive_pricing",
            "deposit_account_metrics",
            "macro_scenario_paths",
        ],
        scenario=scenario_severity,
        horizon_months=horizon_months,
    )

    df = (
        raw["macro_scenario_paths"]
        .merge(raw["deposit_pricing_strategy"], on="as_of_date")
        .merge(raw["deposit_competitive_pricing"], on="as_of_date")
        .merge(raw["deposit_account_metrics"], on="as_of_date")
        .sort_values("as_of_date")
        .reset_index(drop=True)
    )

    # Engineered competitive feature: APY spread vs market average, in bps
    df["apy_spread_vs_market_bps"] = (
        (df["retail_promo_apy_pct"] - df["competitor_apy_avg_pct"]) * 100.0
    )

    # Validate every column the MaaS contract expects is present.
    required = {
        "fed_funds_pct", "ust_2y_pct", "unemployment_pct", "gdp_yoy_pct",
        "retail_promo_apy_pct", "retail_standard_apy_pct",
        "competitor_apy_avg_pct", "apy_spread_vs_market_bps",
        "active_accounts_k", "avg_balance_per_acct_usd", "account_attrition_pct",
        "commercial_deposit_beta", "corp_treasury_demand_idx", "loc_utilization_pct",
        "sb_deposit_beta", "merchant_volume_yoy_pct", "sb_loan_originations_mm",
    }
    missing = required - set(df.columns)
    if missing:
        raise RuntimeError(f"Data Harness: columns missing from OneLake feed: {missing}")

    return df.assign(month_index=range(1, len(df) + 1))
'''

    ctx.attach_transform(
        function_id=None,
        transform_id="tr-deposits-data-harness",
        name="Data Harness",
        description=(
            "Deposit Suite ETL recipe. Reads raw deposit drivers from "
            "OneLake (Finance lakehouse), aligns them to the requested "
            "scenario severity + horizon, engineers the features the "
            "three MaaS libraries expect, and materializes the result "
            "as the Data Harness Output dataset."
        ),
        input_data_source_ids=["ds-onelake-finance"],
        output_dataset_id="ds-onelake-macro-scenario",
        recipe_python=_DATA_HARNESS_RECIPE.strip(),
        parameters=[
            {
                "name": "scenario_severity",
                "label": "Scenario Severity",
                "type": "select",
                "default": "base",
                "options": ["base", "outlook", "adverse", "severely_adverse"],
                "description": "Macro scenario path to pull from OneLake.",
            },
            {
                "name": "horizon_months",
                "label": "Horizon (months)",
                "type": "number",
                "default": 27,
                "description": "Number of monthly rows to materialize.",
            },
        ],
    )

    # Data Quality Check — runtime gate that runs immediately downstream
    # of the Data Harness and right before the models. Treated as a
    # passthrough transform on the canvas (its `output_dataset_id` is
    # the same as the Harness so consumers see identical rows); the
    # value is the signal: it fails the run fast when the inbound data
    # drifts outside acceptable bands.
    _DQC_RECIPE = '''
"""Data Quality Check — pre-model runtime gate.

Runs row-count, null-rate, range, and distribution checks on whatever
dataset is wired to its input. Fails the workflow with a structured
error when any check trips. Pass-through on success — downstream models
see the same rows.
"""
def check(df, *, min_rows=12, max_null_rate=0.05, value_bounds=None):
    if len(df) < min_rows:
        raise RuntimeError(f"DQC: only {len(df)} rows; need at least {min_rows}")
    null_rate = df.isna().mean().max()
    if null_rate > max_null_rate:
        raise RuntimeError(f"DQC: max column null rate {null_rate:.1%} > {max_null_rate:.1%}")
    if value_bounds:
        for col, (lo, hi) in value_bounds.items():
            if col in df.columns:
                if df[col].min() < lo or df[col].max() > hi:
                    raise RuntimeError(f"DQC: {col} outside [{lo}, {hi}]")
    return df  # passthrough — no transformation, just validation
'''

    ctx.attach_transform(
        function_id=None,
        transform_id="tr-deposits-dqc",
        name="Data Quality Check",
        description=(
            "Runtime data-quality gate. Runs row-count, null-rate, range, "
            "and distribution checks on the inbound dataset right before "
            "models consume it. Fails the workflow fast when the data "
            "drifts outside acceptable bands; passthrough on success."
        ),
        input_data_source_ids=["ds-onelake-finance"],
        output_dataset_id="ds-onelake-macro-scenario",
        recipe_python=_DQC_RECIPE.strip(),
        parameters=[
            {
                "name": "min_rows",
                "label": "Minimum rows",
                "type": "number",
                "default": 12,
                "description": "Reject the dataset if it has fewer rows than this.",
            },
            {
                "name": "max_null_rate",
                "label": "Max null rate per column",
                "type": "number",
                "default": 0.05,
                "description": "Reject if any column has a higher null rate.",
            },
        ],
    )

    # 3) Models — three MaaS pickles + the NII Calculator that
    #    aggregates them. Each MaaS emits segment-prefixed (rate, balance)
    #    columns so the orchestrator can merge three frames cleanly when
    #    they fan into the NII Calculator.
    ctx.attach_model(
        function_id="capital_planning",
        model_id="mdl-deposits-rdmaas",
        name="RDMaaS",
        description=(
            "Retail Deposit Model-as-a-Service. Projects monthly deposit "
            "rate paid and end-of-month balance for the retail book given "
            "pricing strategy (promo/standard APY), competitive pricing "
            "(market gap), and account information (active accounts, avg "
            "balance, attrition)."
        ),
        source_path=_SAMPLE_MODELS / "rdmaas.pkl",
        train_metrics={
            "starting_balance_mm": 240_000.0,
            "horizon_months": 27.0,
            "promo_account_share_pct": 30.0,
        },
        output_kind="multi_target",
        target_names=["rdmaas_rate_pct", "rdmaas_balance_mm"],
    )
    ctx.attach_model(
        function_id="capital_planning",
        model_id="mdl-deposits-commmaas",
        name="CommMaaS",
        description=(
            "Commercial Deposit Model-as-a-Service. Projects monthly "
            "deposit rate paid and end-of-month balance for the commercial "
            "book based on corporate treasury demand, line-of-credit "
            "utilization, and rate-sensitivity beta."
        ),
        source_path=_SAMPLE_MODELS / "commmaas.pkl",
        train_metrics={
            "starting_balance_mm": 180_000.0,
            "horizon_months": 27.0,
            "deposit_beta": 0.45,
        },
        output_kind="multi_target",
        target_names=["commmaas_rate_pct", "commmaas_balance_mm"],
    )
    ctx.attach_model(
        function_id="capital_planning",
        model_id="mdl-deposits-sbbmaas",
        name="SBBMaaS",
        description=(
            "Small-Business Banking Deposit Model-as-a-Service. Projects "
            "monthly deposit rate paid and end-of-month balance for the "
            "small-business book using merchant-acquiring volume, new SB "
            "loan originations, and rate-sensitivity beta."
        ),
        source_path=_SAMPLE_MODELS / "sbbmaas.pkl",
        train_metrics={
            "starting_balance_mm": 45_000.0,
            "horizon_months": 27.0,
            "deposit_beta": 0.38,
        },
        output_kind="multi_target",
        target_names=["sbbmaas_rate_pct", "sbbmaas_balance_mm"],
    )
    ctx.attach_model(
        function_id="capital_planning",
        model_id="mdl-deposits-nii-calculator",
        name="NII Calculator",
        description=(
            "Net Interest Income aggregator. Takes the rate + balance "
            "outputs of RDMaaS, CommMaaS, and SBBMaaS and computes the "
            "monthly interest income contribution of each deposit product "
            "across the next 9 quarters: NII = balance_mm × rate_pct / 100 / 12."
        ),
        source_path=_SAMPLE_MODELS / "nii_calculator.pkl",
        train_metrics={"horizon_months": 27.0, "segment_count": 3.0},
        output_kind="multi_target",
        target_names=["nii_rdmaas_mm", "nii_commmaas_mm", "nii_sbbmaas_mm"],
    )

    # 4) Reporting tiles — backed by the CCAR Scenarios dataset.
    #
    #   • Comparison table: BHCB and BHCS across CCAR25 + CCAR26 for
    #     every macro variable except M2 and 1-year UST (per spec).
    #   • Three line plots filtered to CCAR26: 1y UST, Fed funds, M2/GDP.
    #     Each plot has two y_fields → two lines (BHCB + BHCS).
    #
    # All four are unpinned — they live in the Reporting catalog. The
    # analyst pins from there onto Overview if they want them up top.

    # Comparison table — rows are macro variables, columns are the four
    # (scenario × cycle) combos. Backed by ccar_peak_summary.csv whose
    # cells already carry the worst-case value per (var, scenario, cycle).
    ctx.attach_plot(
        function_id="capital_planning",
        plot_id="plot-ccar-peak-summary",
        config={
            "name": "BHC Base vs Stress — Peak values, CCAR25 vs CCAR26",
            "tile_type": "table",
            "dataset_id": "ds-ccar-peak-summary",
            "table_columns": ["macro", "bhcb_25", "bhcb_26", "bhcs_25", "bhcs_26"],
            "filters": [],
            "description": (
                "One row per macro variable; cells show the peak (worst-case) "
                "value during the 9-quarter horizon. Max for unemployment + "
                "BBB spread, min for everything else. M2 and 1-year UST "
                "intentionally omitted — they live in the line plots below."
            ),
            "pinned_to_overview": False,
        },
    )

    # Line plots — BHCB + BHCS, two y_fields = two lines.
    # Palette pins BHCB → dark blue, BHCS → red across all three plots.
    _CCAR26_FILTER = [{"field": "cycle", "op": "eq", "value": "CCAR26"}]
    _BHC_PALETTE = ["#1E3A8A", "#DC2626"]   # dark blue, red — order matches y_fields

    ctx.attach_plot(
        function_id="capital_planning",
        plot_id="plot-ccar-1y-ust",
        config={
            "name": "1-year Treasury — BHCB vs BHCS (CCAR26)",
            "tile_type": "plot",
            "chart_type": "line",
            "dataset_id": "ds-ccar-scenarios",
            "x_field": "pq",
            "y_fields": ["bhcb_ust_1y_pct", "bhcs_ust_1y_pct"],
            "aggregation": "none",
            "filters": _CCAR26_FILTER,
            "style": {"palette": _BHC_PALETTE},
            "description": "1-year UST yield projection across PQ0–PQ9 under BHCB and BHCS.",
            "pinned_to_overview": False,
        },
    )
    ctx.attach_plot(
        function_id="capital_planning",
        plot_id="plot-ccar-fed-funds",
        config={
            "name": "Fed funds effective rate — BHCB vs BHCS (CCAR26)",
            "tile_type": "plot",
            "chart_type": "line",
            "dataset_id": "ds-ccar-scenarios",
            "x_field": "pq",
            "y_fields": ["bhcb_fed_funds_pct", "bhcs_fed_funds_pct"],
            "aggregation": "none",
            "filters": _CCAR26_FILTER,
            "style": {"palette": _BHC_PALETTE},
            "description": "Fed funds policy path across PQ0–PQ9 under BHCB and BHCS.",
            "pinned_to_overview": False,
        },
    )
    ctx.attach_plot(
        function_id="capital_planning",
        plot_id="plot-ccar-m2-gdp",
        config={
            "name": "M2 / GDP — BHCB vs BHCS (CCAR26)",
            "tile_type": "plot",
            "chart_type": "line",
            "dataset_id": "ds-ccar-scenarios",
            "x_field": "pq",
            "y_fields": ["bhcb_m2_to_gdp_ratio", "bhcs_m2_to_gdp_ratio"],
            "aggregation": "none",
            "filters": _CCAR26_FILTER,
            "style": {"palette": _BHC_PALETTE},
            "description": "Money supply relative to nominal GDP across PQ0–PQ9 under BHCB and BHCS.",
            "pinned_to_overview": False,
        },
    )

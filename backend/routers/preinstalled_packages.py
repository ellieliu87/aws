"""Rich metadata registry for the four preinstalled model packages the
Models tab exposes via its "Preinstalled packages" dropdown.

Keyed by the lower-case package id the frontend writes into the URI
(`pip://<id>` — see `frontend/src/pages/Workspace/ModelsTab.tsx`).

When the user registers a preinstalled package via POST /api/models/from-uri,
the handler looks up the package here and injects this dict into the
TrainedModel's `introspection` field. The `model-explainer` skill is
taught to read `introspection.format == "preinstalled_package"` and
present this metadata as the model's documentation.

To add a new preinstalled package, do BOTH of these in lockstep:
  1. Add an entry to `frontend/.../ModelsTab.tsx#PREINSTALLED_PACKAGES`
     (id + short label + one-line description for the dropdown UI).
  2. Add an entry here with the rich metadata for the agent to read.
"""
from __future__ import annotations

from typing import Any


PACKAGE_METADATA: dict[str, dict[str, Any]] = {
    # ── Retail Deposit MaaS — the 8-component retail deposit suite ─────────
    "rdmaas": {
        "format": "preinstalled_package",
        "package_id": "rdmaas",
        "display_name": "RDMaaS — Retail Deposit MaaS",
        "family": "Deposit balance + pricing forecast (model suite)",
        "owner": "CMA — Deposits Modelling team",
        "version": "v3.2 (2026-Q1)",
        "purpose": (
            "Projects retail deposit balances, rates, and interest expense "
            "across the supervisory horizon for CCAR submission. Bundles the "
            "eight modelled components into one importable package so the "
            "workflow canvas can wire it as a single node."
        ),
        "components": [
            {"name": "New Originations",        "kind": "Volume",  "summary": "New-account balance inflow per product per snap_date."},
            {"name": "Backbook Balance",        "kind": "Volume",  "summary": "Existing-account balance attrition / retention."},
            {"name": "Frontbook Balance",       "kind": "Volume",  "summary": "Aggregated frontbook balance roll-forward."},
            {"name": "Branch Balance",          "kind": "Volume",  "summary": "Branch-channel balance projection."},
            {"name": "CD Attrition",            "kind": "Volume",  "summary": "Certificate of Deposit run-off by maturity bucket."},
            {"name": "Liquid Rate",             "kind": "Pricing", "summary": "Effective offered rate on liquid (PSAV, MMDA) products."},
            {"name": "CD Rate",                 "kind": "Pricing", "summary": "Offered rate on term CDs by tenor bucket."},
            {"name": "Liquid-CD Migration",     "kind": "Connector", "summary": "Internal transfer flow between liquid and CD products under stress."},
        ],
        "inputs": {
            "macro_drivers": [
                "FEDFUNDS", "UST_2Y", "UST_10Y", "UNEMPLOYMENT",
                "GDP_QOQ", "CPI_YOY", "HPI_YOY",
            ],
            "internal_drivers": [
                "branch_count", "promo_rate_basis_points",
                "competitor_apy_avg_pct", "marketing_spend_index",
            ],
            "data_cadence": "Monthly snap_dates; bound to a scenario node on the canvas.",
        },
        "outputs": {
            "long_format": True,
            "schema": ["scenario", "snap_date", "variable_name",
                       "variable_value", "segment", "origin"],
            "variable_names": [
                "balance_mm", "rate_paid", "interest_expense_mm",
                "new_originations_mm", "attrition_pct",
            ],
            "segments": ["PSAV", "MMDA", "DFS_CD", "HYMM", "Other"],
        },
        "methodology": {
            "summary": (
                "Volume components use first-difference panel regressions with "
                "macro + internal driver lags. Pricing components follow a "
                "fixed-pricing-percentile (P60) deposit beta framework against "
                "FEDFUNDS. Outputs aggregate to the variance-walk inputs used "
                "by the Retail IE Attribution playbook."
            ),
            "model_class": "Linear panel regressions with segment fixed effects",
            "training_window": "2014-Q1 through 2024-Q4 (44 quarters)",
            "validation_doc": "RDMaaS_v3_methodology.pdf in the Knowledge Base",
            "p60_fixed_pricing_percentile": (
                "Pricing components calibrate to the 60th percentile of "
                "peer-bank pass-through during the 2022-23 hiking cycle."
            ),
        },
        "downstream_consumers": [
            "Retail IE Attribution playbook (variance-analyst phase)",
            "NII Calculator package (this catalog) — aggregates RDMaaS output into NII path",
            "Capital planning workflow — feeds PPNR and stress-IE projections",
        ],
        "limitations": (
            "Volume models are not stable below 50 bps of FEDFUNDS movement "
            "(low signal). Pricing models assume monotonic FEDFUNDS path; "
            "non-monotonic paths require the rate-trajectory overlay."
        ),
        "regulatory_status": "SR 11-7 documented; ECB SS 1/23 mapped; CCAR-tier",
        "typical_use": (
            "Wire on the Workflow canvas between a Data-Harness transform "
            "(scenario + internal drivers) and a destination table. Run with "
            "horizon=27 (CCAR 9-quarter) for submission, horizon=12 for "
            "Outlook scenarios."
        ),
    },

    # ── Commercial Deposit MaaS ─────────────────────────────────────────────
    "commaas": {
        "format": "preinstalled_package",
        "package_id": "commaas",
        "display_name": "CommMaaS — Commercial Deposit MaaS",
        "family": "Deposit balance + pricing forecast (commercial book)",
        "owner": "CMA — Commercial Deposits Modelling team",
        "version": "v2.7 (2026-Q1)",
        "purpose": (
            "Forecasts commercial deposit balances and effective pricing "
            "across the supervisory horizon. Covers the GB (Global Banking), "
            "NON-GB, and HYMM commercial segments."
        ),
        "components": [
            {"name": "Commercial Balance Forecast", "kind": "Volume",  "summary": "Per-segment ECR-driven + rate-driven balance roll-forward."},
            {"name": "Commercial Pricing",          "kind": "Pricing", "summary": "Segment-level effective APY against FEDFUNDS."},
            {"name": "ECR Sensitivity Overlay",     "kind": "Overlay", "summary": "Treasury-earnings-credit sensitivity adjustments."},
        ],
        "inputs": {
            "macro_drivers": ["FEDFUNDS", "UST_2Y", "BBB_SPREAD_BPS"],
            "internal_drivers": [
                "commercial_deposit_beta", "corp_treasury_demand_idx",
                "loc_utilization_pct",
            ],
            "data_cadence": "Monthly snap_dates.",
        },
        "outputs": {
            "long_format": True,
            "schema": ["scenario", "snap_date", "variable_name",
                       "variable_value", "segment", "origin"],
            "variable_names": [
                "ecr_driven_avg_balance", "rate_driven_balance",
                "interest_apy", "interest_expense",
            ],
            "segments": ["GB", "NON-GB", "HYMM", "Macro", "Portfolio"],
        },
        "methodology": {
            "summary": (
                "OLS regressions per segment with explicit ECR vs rate-driven "
                "balance split. Pricing layer is a segment beta against "
                "FEDFUNDS with overlay adjustments for treasury earnings credit."
            ),
            "model_class": "OLS regression with segment fixed effects",
            "training_window": "2018-Q1 through 2024-Q4 (28 quarters)",
            "validation_doc": "CommMaaS_v2_methodology.pdf in the Knowledge Base",
        },
        "downstream_consumers": [
            "Commercial deposit IE attribution analyses",
            "NII Calculator (aggregates this segment's output)",
            "Beta-justification analytic in the Analytics tab",
        ],
        "limitations": (
            "Does not model intraday liquidity dynamics. ECR overlay "
            "calibration assumes Fed reserve regime; may need recalibration "
            "in a balance-sheet-shrink scenario."
        ),
        "regulatory_status": "SR 11-7 documented; CCAR-tier",
        "typical_use": (
            "Wire after a scenario node; bind to commercial input drivers. "
            "Output rows match the schema the variance-walk and beta-"
            "benchmarker tools expect."
        ),
    },

    # ── Small Business Banking MaaS ─────────────────────────────────────────
    "sbbmaas": {
        "format": "preinstalled_package",
        "package_id": "sbbmaas",
        "display_name": "SBBMaaS — Small Business Banking MaaS",
        "family": "Deposit balance + pricing forecast (SBB segment)",
        "owner": "CMA — Small Business Modelling team",
        "version": "v1.9 (2025-Q4)",
        "purpose": (
            "Forecasts small-business deposit balances and pricing. Bridges "
            "retail-style behavioural modelling with commercial-style "
            "treasury sensitivity for SBB customers."
        ),
        "components": [
            {"name": "SBB Balance",   "kind": "Volume",  "summary": "Small-business deposit balance forecast per product."},
            {"name": "SBB Pricing",   "kind": "Pricing", "summary": "Effective APY model for SBB products."},
            {"name": "Merchant Flow", "kind": "Driver",  "summary": "Merchant-payment-volume linkage to deposit balances."},
        ],
        "inputs": {
            "macro_drivers": ["FEDFUNDS", "GDP_QOQ", "UNEMPLOYMENT"],
            "internal_drivers": [
                "sb_deposit_beta", "merchant_volume_yoy_pct",
                "sb_loan_originations_mm",
            ],
            "data_cadence": "Monthly snap_dates.",
        },
        "outputs": {
            "long_format": True,
            "schema": ["scenario", "snap_date", "variable_name",
                       "variable_value", "segment", "origin"],
            "variable_names": [
                "balance_mm", "interest_apy", "interest_expense_mm",
                "merchant_volume_idx",
            ],
            "segments": ["SBB_Checking", "SBB_Savings", "SBB_CD"],
        },
        "methodology": {
            "summary": (
                "Panel regressions per SBB product with merchant-flow as a "
                "leading indicator for balance changes. Pricing is a hybrid "
                "of retail-style behavioural beta and commercial-style ECR "
                "sensitivity."
            ),
            "model_class": "Panel regression with leading-indicator augmentation",
            "training_window": "2017-Q1 through 2024-Q4",
            "validation_doc": "SBBMaaS_v1_methodology.pdf in the Knowledge Base",
        },
        "downstream_consumers": [
            "SBB IE attribution playbook (when used)",
            "NII Calculator (aggregates SBB segment)",
        ],
        "limitations": (
            "Merchant-flow signal is noisy below $5MM monthly volume per "
            "branch. Pricing model is calibrated for prime SBB customers; "
            "sub-prime overlay required separately."
        ),
        "regulatory_status": "SR 11-7 documented; CCAR-tier",
        "typical_use": (
            "Used in conjunction with RDMaaS + CommMaaS to produce a full "
            "deposit-book picture. Often wired as a parallel branch of the "
            "main retail/commercial workflow."
        ),
    },

    # ── NII Calculator ──────────────────────────────────────────────────────
    "nii-calculator": {
        "format": "preinstalled_package",
        "package_id": "nii-calculator",
        "display_name": "NII Calculator",
        "family": "Aggregation calculator (not a forecast model)",
        "owner": "CMA — Treasury Analytics team",
        "version": "v4.1 (2026-Q1)",
        "purpose": (
            "Aggregates outputs from RDMaaS, CommMaaS, SBBMaaS into the "
            "consolidated Net Interest Income (NII) projection used in CCAR "
            "submission. Performs the bal × rate × period_factor roll-up "
            "across products + segments + snap_dates."
        ),
        "components": [
            {"name": "Aggregation Roll-Up",     "kind": "Calculator", "summary": "Sums interest income / expense across the deposit suite outputs."},
            {"name": "Asset-Side Mirror",       "kind": "Calculator", "summary": "Pairs deposit expense with the asset-side income path from the asset MaaS."},
            {"name": "Rate Shock Overlay",      "kind": "Overlay",    "summary": "Applies ±200bp rate-shock variants to the NII path for sensitivity reporting."},
        ],
        "inputs": {
            "upstream_packages": ["rdmaas", "commaas", "sbbmaas"],
            "macro_drivers": ["FEDFUNDS"],  # for the rate-shock overlay
            "data_cadence": "Matches upstream cadence (monthly).",
        },
        "outputs": {
            "long_format": True,
            "schema": ["scenario", "snap_date", "variable_name",
                       "variable_value", "segment", "origin"],
            "variable_names": [
                "nii_mm", "interest_income_mm", "interest_expense_mm",
                "rate_shock_plus_200_nii_mm", "rate_shock_minus_200_nii_mm",
            ],
            "segments": ["Total", "Retail", "Commercial", "SBB"],
        },
        "methodology": {
            "summary": (
                "Pure arithmetic — no statistical model. NII = "
                "Σ(asset_balance × asset_rate × period_factor) − "
                "Σ(deposit_balance × deposit_rate × period_factor). The "
                "rate-shock overlay re-runs the calculation with FEDFUNDS "
                "+200bp / -200bp shifts applied to the pricing components."
            ),
            "model_class": "Deterministic aggregator (not a statistical model)",
            "training_window": "Not applicable — no parameters are fit",
            "validation_doc": "NIICalculator_v4_user_guide.pdf in the Knowledge Base",
        },
        "downstream_consumers": [
            "CCAR PPNR submission",
            "Treasury IRRBB report",
            "Outlook scenarios dashboard",
        ],
        "limitations": (
            "Only as good as the upstream MaaS outputs. The rate-shock "
            "overlay is a sensitivity exercise — not a fully-modelled stress "
            "(no portfolio rebalancing under shock)."
        ),
        "regulatory_status": "Audited as a calculator, not a model — SR 11-7 §III.1",
        "typical_use": (
            "Wire as the terminal node of the deposit forecast workflow "
            "(after RDMaaS/CommMaaS/SBBMaaS). Output is the NII path used "
            "for CCAR + IRRBB reporting."
        ),
    },
}


def get_package_metadata(package_id: str | None) -> dict[str, Any] | None:
    """Look up a preinstalled package by id (case-insensitive). Returns
    None if the id isn't a known preinstalled package — the caller
    should then fall back to the default "uri" model behaviour."""
    if not package_id:
        return None
    return PACKAGE_METADATA.get(package_id.strip().lower())


def package_id_from_uri(artifactory_uri: str | None) -> str | None:
    """Extract the package id from a pip:// URI. Returns None for any
    other URI scheme (corporate Artifactory paths, registry URLs, …)."""
    if not artifactory_uri:
        return None
    s = artifactory_uri.strip()
    if not s.lower().startswith("pip://"):
        return None
    return s[len("pip://"):].strip().lower() or None

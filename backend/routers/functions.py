"""Business functions router - lists the analytical domains analysts can choose from."""
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from models.schemas import BusinessFunction, PackImport
from routers.auth import get_current_user

router = APIRouter()


def _slugify(name: str) -> str:
    """Stable URL-friendly id from a function name."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "workspace"


# Categories are inspired by Capital Markets & Analytics' real org topics
# (Capital Markets & Risks, Capital Resilience Analysis, Treasury Strategy &
# Execution, Balance Sheet Strategy) but compressed to three buckets so each
# bucket carries multiple related functions.
BUSINESS_FUNCTIONS: list[BusinessFunction] = [
    # ── Markets & Risk ─────────────────────────────────────────────────────
    BusinessFunction(
        id="investment_portfolio",
        name="Investment Portfolio Analytics",
        short_name="Portfolio",
        description="Monitor NAV, OAS, OAD, sector mix and unrealized P&L across the bank's investment book.",
        icon="briefcase",
        color="#004977",
        category="Markets & Risk",
        default_views=["Portfolio Snapshot", "Sector Allocation", "Top Holdings", "Risk Limits"],
        sample_metrics=["NAV", "OAS", "OAD", "Book Yield"],
    ),
    BusinessFunction(
        id="market_risk",
        name="Market Risk & VaR",
        short_name="Market Risk",
        description="VaR / SVaR, IRC, FRTB sensitivities, P&L attribution and back-testing exceptions across desks.",
        icon="activity",
        color="#FF5C5C",
        category="Markets & Risk",
        default_views=["VaR by Desk", "SVaR Trend", "FRTB Sensitivities", "Back-test Exceptions"],
        sample_metrics=["1d VaR", "SVaR", "IRC", "BT Exceptions"],
    ),
    BusinessFunction(
        id="interest_rate_risk",
        name="Interest Rate Risk Management",
        short_name="IRR",
        description="EVE and NII sensitivities to parallel shocks, twists, and curve steepeners. ALCO-ready views.",
        icon="line-chart",
        color="#0891B2",
        category="Markets & Risk",
        default_views=["EVE Shock Profile", "NII Sensitivity", "Key Rate Durations", "Hedge Ladder"],
        sample_metrics=["EVE +200bp", "NII 12M", "DV01", "Hedge Notional"],
    ),

    # ── Treasury & Capital ─────────────────────────────────────────────────
    BusinessFunction(
        id="liquidity_management",
        name="Liquidity & Funding",
        short_name="Liquidity",
        description="HQLA stack, LCR/NSFR, deposit beta, and projected cash ladders for the bank's funding profile.",
        icon="droplet",
        color="#059669",
        category="Treasury & Capital",
        default_views=["HQLA Stack", "LCR Trend", "Deposit Mix", "Cash Ladder"],
        sample_metrics=["HQLA", "LCR", "NSFR", "Deposit Beta"],
    ),
    BusinessFunction(
        id="capital_planning",
        name="Capital Planning & CCAR",
        short_name="Capital",
        description="CET1, RWA, stress scenario PPNR, and capital actions tracking against CCAR submissions.",
        icon="building-2",
        color="#D97706",
        category="Treasury & Capital",
        default_views=["CET1 Trajectory", "RWA Composition", "Stress PPNR", "Capital Actions"],
        sample_metrics=["CET1", "RWA", "Tier 1", "SCB"],
    ),
    BusinessFunction(
        id="financial_reporting",
        name="Financial Planning & Reporting",
        short_name="FP&A",
        description="Revenue/expense forecasts, variance vs plan, segment P&L, and management reporting packages.",
        icon="file-spreadsheet",
        color="#00B8D9",
        category="Treasury & Capital",
        default_views=["P&L vs Plan", "Segment Walk", "Expense Drivers", "Forecast Path"],
        sample_metrics=["Revenue", "OpEx", "PPNR", "Variance"],
    ),
]


# Display order for the home page (matches the buckets above)
CATEGORY_ORDER = [
    "Markets & Risk",
    "Treasury & Capital",
]


@router.get("", response_model=list[BusinessFunction])
async def list_functions(_: str = Depends(get_current_user)):
    return BUSINESS_FUNCTIONS


@router.get("/categories")
async def list_categories(_: str = Depends(get_current_user)):
    return {"categories": CATEGORY_ORDER}


@router.get("/{function_id}", response_model=BusinessFunction)
async def get_function(function_id: str, _: str = Depends(get_current_user)):
    for f in BUSINESS_FUNCTIONS:
        if f.id == function_id:
            return f
    raise HTTPException(status_code=404, detail="Function not found")


# ── Create a new workspace (in-memory; resets on backend restart) ──────────
class BusinessFunctionCreate(BaseModel):
    """Body for the new-workspace modal. `id` is auto-derived from `name`
    when not provided. `default_views` and `sample_metrics` come up empty —
    a freshly-created workspace has no hardcoded content; analysts populate
    it via the Reporting / Data / Models tabs."""
    name: str = Field(min_length=2, max_length=80)
    short_name: str | None = Field(default=None, max_length=40)
    description: str = Field(default="", max_length=500)
    category: str = Field(min_length=2, max_length=60)
    icon: str = "briefcase"
    color: str = "#004977"
    id: str | None = None  # optional — defaults to slugify(name)
    imported_packs: list[PackImport] = Field(default_factory=list)


@router.delete("/{function_id}", status_code=204)
async def delete_function(function_id: str, _: str = Depends(get_current_user)):
    idx = next((i for i, f in enumerate(BUSINESS_FUNCTIONS) if f.id == function_id), None)
    if idx is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    BUSINESS_FUNCTIONS.pop(idx)
    remaining_cats = {f.category for f in BUSINESS_FUNCTIONS}
    for cat in list(CATEGORY_ORDER):
        if cat not in remaining_cats:
            CATEGORY_ORDER.remove(cat)


@router.post("", response_model=BusinessFunction, status_code=201)
async def create_function(
    body: BusinessFunctionCreate,
    _: str = Depends(get_current_user),
):
    fid = (body.id or _slugify(body.name)).strip()
    if not fid:
        raise HTTPException(status_code=400, detail="Could not derive a valid id from the name.")
    if any(f.id == fid for f in BUSINESS_FUNCTIONS):
        raise HTTPException(status_code=409, detail=f"A workspace with id '{fid}' already exists.")

    short = (body.short_name or body.name).strip() or body.name
    fn = BusinessFunction(
        id=fid,
        name=body.name.strip(),
        short_name=short[:40],
        description=body.description.strip(),
        icon=body.icon or "briefcase",
        color=body.color or "#004977",
        category=body.category.strip(),
        default_views=[],
        sample_metrics=[],
        imported_packs=body.imported_packs,
    )
    BUSINESS_FUNCTIONS.append(fn)
    if fn.category not in CATEGORY_ORDER:
        CATEGORY_ORDER.append(fn.category)

    # Ensure built-in CCAR + Outlook scenarios are in the scenario registry
    # so they appear immediately in the new workspace's workflow tab dropdown.
    # Idempotent — skips any scenario already present from the startup hook.
    try:
        from services.data_services import materialize_into_scenarios_registry
        materialize_into_scenarios_registry(None)
    except Exception:
        pass

    return fn

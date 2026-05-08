"""Data sources router - configure Snowflake / OneLake / file uploads / etc.

In a real deployment this would persist connection metadata (and secrets!) in a
secure store. Here we keep things in process memory and never accept real
credentials.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from models.schemas import DataSource, DataSourceCreate
from routers.auth import get_current_user

router = APIRouter()


# Sample tables exposed by each data source. In a real deployment this would
# come from an introspection call against the source's information_schema.
SAMPLE_TABLES: dict[str, dict[str, list[tuple[str, str]]]] = {
    "ds-snowflake-prod": {
        # SimCorp position book — what analysts see in their daily portfolio
        "SIMCORP.PORTFOLIO.POSITIONS": [
            ("position_id", "object"), ("portfolio", "object"),
            ("cusip", "object"), ("security_id", "object"),
            ("security_type", "object"), ("agency", "object"),
            ("coupon", "float64"), ("maturity_date", "datetime64[ns]"),
            ("par_amount", "float64"), ("market_value", "float64"),
            ("book_value", "float64"), ("unrealized_pnl", "float64"),
            ("oad_years", "float64"), ("oas_bps", "float64"),
            ("book_yield", "float64"), ("convexity", "float64"),
            ("sector", "object"), ("trader", "object"),
            ("as_of_date", "datetime64[ns]"),
        ],
        "SIMCORP.PORTFOLIO.TRANSACTIONS": [
            ("trade_id", "object"), ("trade_date", "datetime64[ns]"),
            ("settle_date", "datetime64[ns]"), ("cusip", "object"),
            ("side", "object"), ("par_amount", "float64"),
            ("price", "float64"), ("yield", "float64"),
            ("counterparty", "object"), ("trader", "object"),
        ],
        # eMBS deal info — pool-level MBS data feed
        "EMBS.DEAL_INFO.POOL_FACTORS": [
            ("cusip", "object"), ("pool_id", "object"),
            ("agency", "object"), ("coupon", "float64"),
            ("issue_date", "datetime64[ns]"), ("factor", "float64"),
            ("wac", "float64"), ("wam", "int64"),
            ("wala", "int64"), ("orig_balance", "float64"),
            ("current_balance", "float64"), ("loan_count", "int64"),
            ("avg_loan_size", "float64"), ("fico_avg", "int64"),
            ("ltv_avg", "float64"), ("dti_avg", "float64"),
            ("top_state", "object"), ("top_state_pct", "float64"),
            ("lien_position", "int64"), ("occupancy", "object"),
        ],
        "EMBS.DEAL_INFO.PREPAY_HISTORY": [
            ("cusip", "object"), ("month", "datetime64[ns]"),
            ("cpr_1m", "float64"), ("cpr_3m", "float64"),
            ("cpr_6m", "float64"), ("cpr_12m", "float64"),
            ("smm", "float64"), ("scheduled_paydown", "float64"),
            ("unscheduled_paydown", "float64"),
        ],
        "CMA.PUBLIC.LIMITS": [
            ("limit_name", "object"), ("threshold", "float64"),
            ("direction", "object"), ("active", "bool"),
        ],
    },
    "ds-onelake-finance": {
        "Finance.cma.gl_balances": [
            ("account", "object"), ("segment", "object"),
            ("balance", "float64"), ("period", "datetime64[ns]"),
        ],
        "Finance.cma.budget": [
            ("category", "object"), ("month", "datetime64[ns]"),
            ("plan", "float64"), ("actual", "float64"),
        ],
        "Finance.cma.deposits": [
            ("product", "object"), ("balance", "float64"),
            ("rate", "float64"), ("beta", "float64"),
            ("as_of_date", "datetime64[ns]"),
        ],
    },
    # IHS Markit — macroeconomic time series
    "ds-ihs-markit": {
        "IHS.MacroEcon.RATES_DAILY": [
            ("date", "datetime64[ns]"), ("ust_2y", "float64"),
            ("ust_5y", "float64"), ("ust_10y", "float64"),
            ("ust_30y", "float64"), ("sofr", "float64"),
            ("mortgage_30y_primary", "float64"),
            ("mortgage_30y_secondary", "float64"),
            ("move_index", "float64"), ("vix", "float64"),
        ],
        "IHS.MacroEcon.MACRO_MONTHLY": [
            ("date", "datetime64[ns]"), ("variable", "object"),
            ("value", "float64"), ("yoy_change", "float64"),
            ("region", "object"), ("frequency", "object"),
            ("source", "object"), ("revision_count", "int64"),
        ],
        "IHS.MacroEcon.HOUSING": [
            ("date", "datetime64[ns]"), ("hpi_national", "float64"),
            ("hpi_yoy", "float64"), ("housing_starts", "float64"),
            ("existing_home_sales", "float64"),
            ("median_price", "float64"), ("supply_months", "float64"),
        ],
        "IHS.MacroEcon.SCENARIO_FORECASTS": [
            ("scenario_name", "object"), ("variable", "object"),
            ("horizon_date", "datetime64[ns]"),
            ("forecast_value", "float64"), ("baseline_delta", "float64"),
            ("publication_date", "datetime64[ns]"),
        ],
    },
    "ds-postgres-staging": {
        "public.positions_stg": [
            ("position_id", "object"), ("market_value", "float64"),
            ("ingested_at", "datetime64[ns]"),
        ],
    },
}


_DATA_SOURCES: dict[str, DataSource] = {}

DATASOURCES_STORE = Path(__file__).resolve().parent.parent / "data" / "datasources.json"


def _save() -> None:
    """Persist all data sources to disk so custom registrations survive restarts."""
    try:
        DATASOURCES_STORE.parent.mkdir(parents=True, exist_ok=True)
        DATASOURCES_STORE.write_text(
            json.dumps([s.model_dump() for s in _DATA_SOURCES.values()], indent=2)
        )
    except Exception:
        pass


def _seed() -> None:
    # Restore any previously saved sources first (covers custom OneLake extractors
    # and any other sources registered at runtime).
    if DATASOURCES_STORE.exists():
        try:
            for r in json.loads(DATASOURCES_STORE.read_text()):
                src = DataSource(**r)
                _DATA_SOURCES[src.id] = src
        except Exception:
            pass

    # Add hardcoded defaults only for IDs not already loaded from disk.
    seeds = [
        DataSource(
            id="ds-snowflake-prod",
            name="Snowflake — CMA Warehouse",
            type="snowflake",
            status="connected",
            connection_string="snowflake://cma_warehouse.snowflakecomputing.com/CMA/PUBLIC",
            last_synced=datetime.utcnow().isoformat() + "Z",
            description="Primary analytical warehouse — SimCorp position book and eMBS pool feeds.",
            config={"warehouse": "ANALYTICS_WH", "database": "CMA", "schema": "PUBLIC"},
        ),
        DataSource(
            id="ds-onelake-finance",
            name="OneLake — Finance Datalake",
            type="onelake",
            status="connected",
            connection_string="abfss://finance@onelake.dfs.fabric.microsoft.com/cma",
            last_synced=datetime.utcnow().isoformat() + "Z",
            description="Finance datalake — GL balances, budget, deposits.",
            config={"workspace": "Finance", "lakehouse": "cma"},
        ),
        DataSource(
            id="ds-ihs-markit",
            name="IHS Markit — Macro Feeds",
            type="rest_api",
            status="connected",
            connection_string="https://api.ihsmarkit.com/macroeconomics/v2",
            last_synced=datetime.utcnow().isoformat() + "Z",
            description="IHS Markit macroeconomic feeds — rates, housing, scenario forecasts.",
            config={"vendor": "IHS Markit", "api_version": "v2", "frequency": "Daily / Monthly"},
        ),
        DataSource(
            id="ds-postgres-staging",
            name="Postgres — Staging",
            type="postgres",
            status="disconnected",
            connection_string="postgresql://staging.local:5432/cma_stg",
            last_synced=None,
            description="Pre-prod staging database. Re-enable to use.",
            config={"db": "cma_stg"},
        ),
    ]
    for s in seeds:
        if s.id not in _DATA_SOURCES:
            _DATA_SOURCES[s.id] = s


_seed()


@router.get("", response_model=list[DataSource])
async def list_sources(_: str = Depends(get_current_user)):
    return list(_DATA_SOURCES.values())


@router.post("", response_model=DataSource, status_code=201)
async def create_source(req: DataSourceCreate, _: str = Depends(get_current_user)):
    sid = f"ds-{req.type}-{uuid.uuid4().hex[:6]}"
    src = DataSource(
        id=sid,
        name=req.name,
        type=req.type,
        status="pending",
        description=req.description,
        config=req.config,
    )
    _DATA_SOURCES[sid] = src
    return src


@router.delete("/{source_id}", status_code=204)
async def delete_source(source_id: str, _: str = Depends(get_current_user)):
    if source_id not in _DATA_SOURCES:
        raise HTTPException(status_code=404, detail="Data source not found")
    del _DATA_SOURCES[source_id]


@router.post("/{source_id}/test")
async def test_connection(source_id: str, _: str = Depends(get_current_user)):
    src = _DATA_SOURCES.get(source_id)
    if not src:
        raise HTTPException(status_code=404, detail="Data source not found")
    # Mock test: flip to connected
    src.status = "connected"
    src.last_synced = datetime.utcnow().isoformat() + "Z"
    return {"ok": True, "status": src.status, "tested_at": src.last_synced}


@router.get("/{source_id}/tables")
async def list_tables(source_id: str, _: str = Depends(get_current_user)):
    if source_id not in _DATA_SOURCES:
        raise HTTPException(status_code=404, detail="Data source not found")
    tables = SAMPLE_TABLES.get(source_id, {})
    return {
        "source_id": source_id,
        "tables": [
            {
                "ref": ref,
                "columns": [{"name": n, "dtype": d} for n, d in cols],
            }
            for ref, cols in tables.items()
        ],
    }


@router.post("/upload", response_model=DataSource, status_code=201)
async def upload_file(
    file: Annotated[UploadFile, File()],
    _: str = Depends(get_current_user),
):
    # We don't actually persist the bytes anywhere — just record metadata.
    contents = await file.read()
    sid = f"ds-upload-{uuid.uuid4().hex[:6]}"
    src = DataSource(
        id=sid,
        name=file.filename or "uploaded.csv",
        type="file_upload",
        status="connected",
        description=f"Uploaded file ({len(contents):,} bytes, type {file.content_type or 'unknown'}).",
        last_synced=datetime.utcnow().isoformat() + "Z",
        config={"filename": file.filename, "size_bytes": len(contents), "content_type": file.content_type},
    )
    _DATA_SOURCES[sid] = src
    return src

"""Deposit pack — Python tools for the CCAR variance-attribution playbook
plus the chat-panel deposit-expert / attribution-challenger flow.

  - compute_variance_walk         — variance-analyst: Pandas
                                    Rate / Volume / Mix decomposition
                                    over the CCAR retail PPNR CSV.
  - verify_numbers_in_narrative   — accuracy-reviewer: extracts dollar
                                    figures from prose, confirms each
                                    ties to a row in the variance JSON.
  - audit_logic_rules             — attribution-challenger: runs a registered
                                    set of SR 11-7-style red-flag
                                    patterns against a claim/narrative.
  - get_model_assumptions         — methodology-researcher /
                                    attribution-challenger: returns the
                                    documented per-product assumption
                                    block (PSAV beta, CD attrition
                                    floor, recapture rate, marketing
                                    pullback path).
  - compute_sensitivity_walk      — variance-analyst (sensitivity
                                    branch): perturbs a parameter and
                                    recomputes Interest_Expense_mm.

The methodology-researcher uses the universal built-in `rag_search`
against `sample_docs/retail_deposit/`.

Each tool is a self-contained Python source string registered through
`PackContext.register_python_tool` so it lands in the universal tool
registry with `source='pack'` + `pack_id='deposits'`.
"""
from __future__ import annotations

from packs import PackContext


def register_python_tools(ctx: PackContext) -> None:
    # ── Agent 1 — variance walk ──────────────────────────────────────────
    ctx.register_python_tool(
        name="compute_variance_walk",
        description=(
            "Decompose the dollar variance between a **stress** scenario "
            "(BHCS or FedSA) and a **baseline** scenario (BHCB or FedB) "
            "into Rate / Volume / Mix components, attributed per "
            "product. Operates on long-format retail-model output rows: "
            "`scenario`, `Run_ID`, `variable_name`, `snap_date`, "
            "`variable_value`, `additional_dimensions{product_name, "
            "variable_type}`. The corresponding macro inputs (rate "
            "paths, etc.) are NOT consumed by this tool — they live in "
            "the input CSV the methodology-researcher reads via "
            "rag_search."
        ),
        parameters=[
            {"name": "current_scenario",   "type": "string",
             "description": (
                 "Stress scenario code. Defaults to BHCS when omitted "
                 "(falls back to FedSA if BHCS is absent from the file)."
             ),
             "required": False},
            {"name": "benchmark_scenario", "type": "string",
             "description": (
                 "Baseline scenario code. Defaults to BHCB when omitted "
                 "(falls back to FedB if BHCB is absent from the file)."
             ),
             "required": False},
            {"name": "metric",             "type": "string",
             "description": (
                 "`variable_name` value to attribute. Default "
                 "'interest_expense'. Matching is case- and "
                 "suffix-insensitive: 'interest_expense' will match "
                 "'Interest_Expense', 'interest_expense_mm', etc."
             ),
             "required": False},
            {"name": "rate_var_name",      "type": "string",
             "description": (
                 "`variable_name` value carrying the deposit rate. "
                 "Default 'interest_apy'. Common alternates that match: "
                 "'interest_apr', 'rate_paid', 'rate_paid_pct'."
             ),
             "required": False},
            {"name": "balance_var_name",   "type": "string",
             "description": (
                 "`variable_name` value carrying the average balance. "
                 "Default 'balance'. Common alternates that match: "
                 "'balance_mm', 'avg_balance'."
             ),
             "required": False},
            {"name": "period_factor",      "type": "number",
             "description": (
                 "Annualization factor applied to (rate × balance). "
                 "Default 0.0833 (= 1/12, monthly snap_dates). Set to "
                 "0.25 for quarterly data."
             ),
             "required": False},
            {"name": "playbook_id",        "type": "string",
             "description": (
                 "Playbook id (read from `[Context]`, line `playbook_id: …`). "
                 "When supplied, the tool auto-discovers the variance CSV by "
                 "scanning the analyst-uploaded files for this playbook and "
                 "picking the one whose schema + scenarios match. "
                 "**Preferred** way to point the tool at uploaded data — no "
                 "manual path construction needed."
             ),
             "required": False},
            {"name": "csv_path",           "type": "string",
             "description": (
                 "Explicit path to a CSV with the long-format retail-output "
                 "schema (scenario, Run_ID, variable_name, snap_date, "
                 "variable_value, additional_dimensions). Use this only when "
                 "the analyst named a file outside the playbook uploads."
             ),
             "required": False},
        ],
        python_source=(
            'def compute_variance_walk(current_scenario=None, benchmark_scenario=None,\n'
            '                           metric="interest_expense",\n'
            '                           rate_var_name="interest_apy",\n'
            '                           balance_var_name="balance",\n'
            '                           period_factor=None,\n'
            '                           balance_scale_to_mm=None,\n'
            '                           metric_scale_to_mm=None,\n'
            '                           playbook_id=None, csv_path=None):\n'
            '    """Rate/Volume/Mix decomposition of a retail-model metric between\n'
            '    a baseline and a stress scenario, on long-format output data.\n'
            '\n'
            '    Expected schema (one row per scenario × snap_date × product ×\n'
            '    variable):\n'
            '       scenario               BHCB | BHCS | FedB | FedSA\n'
            '       Run_ID                 (carried through, not aggregated on)\n'
            '       variable_name          rate_paid | balance_mm | interest_expense_mm | ...\n'
            '       snap_date              ISO date or YYYY-MM\n'
            '       variable_value         numeric\n'
            '       additional_dimensions  dict / JSON string with at least\n'
            '                              `product_name`. May also carry\n'
            '                              `variable_type`.\n'
            '\n'
            '    Defaults: stress = BHCS (fallback FedSA), baseline = BHCB\n'
            '    (fallback FedB). Period factor defaults to 1/12 (monthly)."""\n'
            '    import os, json, ast, glob\n'
            '    import pandas as pd\n'
            '\n'
            '    def _find_repo_root():\n'
            '        """Walk up from cwd looking for the `sample_data` folder. Avoids\n'
            '        relying on __file__ (this source is exec\'d in subprocesses where\n'
            '        __file__ may not be defined)."""\n'
            '        here = os.path.abspath(os.getcwd())\n'
            '        for _ in range(6):\n'
            '            if os.path.isdir(os.path.join(here, "sample_data")):\n'
            '                return here\n'
            '            parent = os.path.dirname(here)\n'
            '            if parent == here:\n'
            '                break\n'
            '            here = parent\n'
            '        return os.path.abspath(os.getcwd())\n'
            '\n'
            '    def _docs_root():\n'
            '        env_root = (os.environ.get("CMA_DOCS_ROOT") or "").strip()\n'
            '        if env_root:\n'
            '            return os.path.join(env_root, "uploads") if not env_root.rstrip("/\\\\").endswith("uploads") else env_root\n'
            '        return os.path.join(_find_repo_root(), "sample_docs", "uploads")\n'
            '\n'
            '    # Schema matching is case- and underscore-tolerant. We map each\n'
            '    # required canonical column to the actual file column once\n'
            '    # per file and reuse the map throughout. `product_name` may\n'
            '    # live at the top level OR nested inside `additional_dimensions`.\n'
            '    # Canonical names are lowercase + alphanumeric only, so\n'
            '    # `Run_ID`, `RunID`, `run id`, and `run-id` all collapse to\n'
            '    # `runid` and match.\n'
            '    REQUIRED_CANON = ["scenario", "runid", "variablename",\n'
            '                       "snapdate", "variablevalue"]\n'
            '\n'
            '    def _canon(c):\n'
            '        return "".join(ch for ch in str(c).lower() if ch.isalnum())\n'
            '\n'
            '    def _build_col_map(df_):\n'
            '        """Return (canonical_name -> actual_column) for every column\n'
            '        in `df_`. Multiple actual columns mapping to the same canonical\n'
            '        name keep the first one we see."""\n'
            '        out = {}\n'
            '        for c in df_.columns:\n'
            '            out.setdefault(_canon(c), c)\n'
            '        return out\n'
            '\n'
            '    def _missing_canon(col_map):\n'
            '        miss = [c for c in REQUIRED_CANON if c not in col_map]\n'
            '        # `product_name` is satisfied by EITHER a top-level column\n'
            '        # OR a nested `additional_dimensions{product_name}` field.\n'
            '        if "productname" not in col_map and "additionaldimensions" not in col_map:\n'
            '            miss.append("productname OR additionaldimensions")\n'
            '        return miss\n'
            '\n'
            '    # Auto-discover from the playbook upload folder when the agent\n'
            '    # passes `playbook_id` instead of a path. We scan every .csv /\n'
            '    # .xlsx / .parquet in the folder, score by schema + scenario\n'
            '    # match, and pick the best one. This is the recommended path\n'
            '    # — it spares the agent from constructing or escaping paths.\n'
            '    if not csv_path and playbook_id:\n'
            '        scope = os.path.join(_docs_root(), "playbook", playbook_id)\n'
            '        if not os.path.isdir(scope):\n'
            '            return {\n'
            '                "error":      "no upload folder for this playbook",\n'
            '                "scope":      scope,\n'
            '                "hint":       ("The analyst hasn\'t uploaded any files to this "\n'
            '                                "playbook yet. Ask them to attach a long-format "\n'
            '                                "retail-output CSV (scenario, Run_ID, "\n'
            '                                "variable_name, snap_date, variable_value, "\n'
            '                                "additional_dimensions) via the playbook editor\'s "\n'
            '                                "Reference files area."),\n'
            '            }\n'
            '        candidates = []\n'
            '        for ext in ("*.csv", "*.xlsx", "*.xls", "*.parquet"):\n'
            '            for p in sorted(glob.glob(os.path.join(scope, ext))):\n'
            '                try:\n'
            '                    e = os.path.splitext(p)[1].lower()\n'
            '                    if e == ".csv":\n'
            '                        peek = pd.read_csv(p, nrows=200)\n'
            '                    elif e in (".xlsx", ".xls"):\n'
            '                        peek = pd.read_excel(p, nrows=200)\n'
            '                    else:\n'
            '                        peek = pd.read_parquet(p)\n'
            '                except Exception as ex:\n'
            '                    candidates.append({"path": p, "score": -1,\n'
            '                                        "reason": f"read failed: {ex}"})\n'
            '                    continue\n'
            '                col_map = _build_col_map(peek)\n'
            '                missing = _missing_canon(col_map)\n'
            '                if missing:\n'
            '                    candidates.append({\n'
            '                        "path":            p,\n'
            '                        "score":           0,\n'
            '                        "reason":          f"missing cols (canonical): {missing}",\n'
            '                        "actual_columns":  list(peek.columns),\n'
            '                    })\n'
            '                    continue\n'
            '                scenarios = set(peek[col_map["scenario"]].astype(str).unique())\n'
            '                # Score schema-match alone at 1; +1 for each\n'
            '                # named scenario present (or default-pair present).\n'
            '                pair_for_scoring = (\n'
            '                    [s for s in [current_scenario, benchmark_scenario] if s]\n'
            '                    or ["BHCS", "BHCB"]\n'
            '                )\n'
            '                hits = sum(1 for s in pair_for_scoring if s in scenarios)\n'
            '                candidates.append({"path": p, "score": 1 + hits,\n'
            '                                    "scenarios_in_file": sorted(scenarios)[:8],\n'
            '                                    "reason": "ok"})\n'
            '        ok = [c for c in candidates if c["score"] >= 1]\n'
            '        if not ok:\n'
            '            return {\n'
            '                "error":          "no usable CSV in playbook uploads",\n'
            '                "scope":          scope,\n'
            '                "files_checked":  candidates,\n'
            '                "needed_columns": REQUIRED_CANON + ["product_name OR additional_dimensions"],\n'
            '                "match_rules":    ("column matching is case- and underscore-insensitive: "\n'
            '                                    "Scenario / scenario / SCENARIO all match. "\n'
            '                                    "`product_name` may live at the top level OR be "\n'
            '                                    "nested inside `additional_dimensions`."),\n'
            '                "hint":           ("None of the uploaded files match the variance "\n'
            '                                    "schema. Each file\'s actual columns are listed "\n'
            '                                    "in `files_checked[].actual_columns` so you can "\n'
            '                                    "tell the analyst which fields are missing."),\n'
            '            }\n'
            '        ok.sort(key=lambda c: -c["score"])\n'
            '        csv_path = ok[0]["path"]\n'
            '\n'
            '    if not csv_path:\n'
            '        # No file resolved. The bundled sample is in the OLD schema\n'
            '        # and would silently mismatch — surface a clear error so the\n'
            '        # caller knows to pass `playbook_id` or `csv_path`.\n'
            '        return {\n'
            '            "error": "no source file specified",\n'
            '            "next_steps": (\n'
            '                "Pass `playbook_id` (read from `[Context]` line "\n'
            '                "`playbook_id: pbk-…`) so the tool auto-discovers the "\n'
            '                "uploaded CSV. If the analyst hasn\'t uploaded data, "\n'
            '                "tell them the playbook needs a long-format retail-output "\n'
            '                "CSV (scenario, Run_ID, variable_name, snap_date, "\n'
            '                "variable_value, additional_dimensions)."\n'
            '            ),\n'
            '        }\n'
            '    else:\n'
            '        # Resolve relative paths against the Knowledge Base / playbook\n'
            '        # uploads root so the agent can pass `playbook/<id>/file.csv`\n'
            '        # without having to escape Windows backslashes in tool args.\n'
            '        if not os.path.isabs(csv_path):\n'
            '            cand = os.path.join(_docs_root(), csv_path)\n'
            '            if os.path.exists(cand):\n'
            '                csv_path = cand\n'
            '        if not os.path.exists(csv_path):\n'
            '            return {\n'
            '                "error":     "csv file not found",\n'
            '                "csv_path":  csv_path,\n'
            '                "hint":      ("Pass either an absolute path or a path relative to "\n'
            '                              "the docs root (e.g. `playbook/<id>/file.csv`)."),\n'
            '            }\n'
            '\n'
            '    df = pd.read_csv(csv_path)\n'
            '    col_map = _build_col_map(df)\n'
            '    missing = _missing_canon(col_map)\n'
            '    if missing:\n'
            '        return {\n'
            '            "error":           "csv missing required columns",\n'
            '            "missing_canonical": missing,\n'
            '            "actual_columns":  list(df.columns),\n'
            '            "csv_path":        csv_path,\n'
            '            "match_rules":     ("matching is case- and underscore-insensitive; "\n'
            '                                 "`product_name` may be top-level OR nested in "\n'
            '                                 "`additional_dimensions`."),\n'
            '            "hint":            ("Expected long-format retail-output CSV with columns "\n'
            '                                "(scenario, Run_ID, variable_name, snap_date, "\n'
            '                                "variable_value) plus product_name (top-level or "\n'
            '                                "nested in additional_dimensions)."),\n'
            '        }\n'
            '\n'
            '    # Resolve a top-level `product_name` column. Either copy from\n'
            '    # the file (if present) or extract from `additional_dimensions`.\n'
            '    def _get_product(v):\n'
            '        if isinstance(v, dict):\n'
            '            return v.get("product_name")\n'
            '        if isinstance(v, str) and v.strip():\n'
            '            try:\n'
            '                d = json.loads(v)\n'
            '            except Exception:\n'
            '                try:\n'
            '                    d = ast.literal_eval(v)\n'
            '                except Exception:\n'
            '                    return None\n'
            '            return d.get("product_name") if isinstance(d, dict) else None\n'
            '        return None\n'
            '\n'
            '    if "productname" in col_map:\n'
            '        df["product_name"] = df[col_map["productname"]]\n'
            '    else:\n'
            '        df["product_name"] = df[col_map["additionaldimensions"]].apply(_get_product)\n'
            '\n'
            '    # Promote canonical column names so the rest of the function can\n'
            '    # use the standard names regardless of file casing.\n'
            '    if col_map["scenario"]       != "scenario":       df["scenario"]       = df[col_map["scenario"]]\n'
            '    if col_map["runid"]          != "Run_ID":         df["Run_ID"]         = df[col_map["runid"]]\n'
            '    if col_map["variablename"]   != "variable_name":  df["variable_name"]  = df[col_map["variablename"]]\n'
            '    if col_map["snapdate"]       != "snap_date":      df["snap_date"]      = df[col_map["snapdate"]]\n'
            '    if col_map["variablevalue"]  != "variable_value": df["variable_value"] = df[col_map["variablevalue"]]\n'
            '\n'
            '    # Auto-default scenarios. Stress = BHCS, fallback FedSA;\n'
            '    # baseline = BHCB, fallback FedB. Surfaced in `assumptions`\n'
            '    # so the next agent can challenge.\n'
            '    scenarios_in_file = set(df["scenario"].astype(str).unique())\n'
            '    assumption_notes = []\n'
            '    if not current_scenario:\n'
            '        if "BHCS" in scenarios_in_file:\n'
            '            current_scenario = "BHCS"\n'
            '        elif "FedSA" in scenarios_in_file:\n'
            '            current_scenario = "FedSA"\n'
            '            assumption_notes.append("BHCS not in file; defaulted to FedSA.")\n'
            '        else:\n'
            '            return {\n'
            '                "error": "no stress scenario in csv",\n'
            '                "expected_one_of": ["BHCS", "FedSA"],\n'
            '                "available_scenarios": sorted(scenarios_in_file),\n'
            '                "csv_path": csv_path,\n'
            '            }\n'
            '    if not benchmark_scenario:\n'
            '        if "BHCB" in scenarios_in_file:\n'
            '            benchmark_scenario = "BHCB"\n'
            '        elif "FedB" in scenarios_in_file:\n'
            '            benchmark_scenario = "FedB"\n'
            '            assumption_notes.append("BHCB not in file; defaulted to FedB.")\n'
            '        else:\n'
            '            return {\n'
            '                "error": "no baseline scenario in csv",\n'
            '                "expected_one_of": ["BHCB", "FedB"],\n'
            '                "available_scenarios": sorted(scenarios_in_file),\n'
            '                "csv_path": csv_path,\n'
            '            }\n'
            '\n'
            '    if current_scenario not in scenarios_in_file or benchmark_scenario not in scenarios_in_file:\n'
            '        return {\n'
            '            "error":              "scenario(s) not found in csv",\n'
            '            "current_scenario":   current_scenario,\n'
            '            "benchmark_scenario": benchmark_scenario,\n'
            '            "current_found":      current_scenario in scenarios_in_file,\n'
            '            "benchmark_found":    benchmark_scenario in scenarios_in_file,\n'
            '            "available_scenarios": sorted(scenarios_in_file),\n'
            '            "csv_path":           csv_path,\n'
            '        }\n'
            '\n'
            '    variable_names = set(df["variable_name"].astype(str).unique())\n'
            '\n'
            '    # Tolerant matching for variable_name values: case-insensitive,\n'
            '    # underscore-insensitive, AND tolerant of common unit/format\n'
            '    # suffixes (`_mm`, `_pct`, `_apr`, `_apy`, `_b`). So\n'
            '    # `interest_expense` matches `interest_expense_mm`, and\n'
            '    # `balance` matches `balance_mm`.\n'
            '    _SUFFIXES = ("mm", "pct", "apr", "apy", "b", "billion", "millions",\n'
            '                  "rate", "amount", "amt")\n'
            '\n'
            '    def _var_canon(s):\n'
            '        return "".join(c for c in str(s).lower() if c.isalnum())\n'
            '\n'
            '    def _strip_suffix(s):\n'
            '        for sfx in _SUFFIXES:\n'
            '            if s.endswith(sfx) and len(s) > len(sfx):\n'
            '                return s[:-len(sfx)]\n'
            '        return s\n'
            '\n'
            '    var_index = {}\n'
            '    for v in variable_names:\n'
            '        c = _var_canon(v)\n'
            '        var_index.setdefault(c, v)\n'
            '        var_index.setdefault(_strip_suffix(c), v)\n'
            '\n'
            '    def _resolve_var(target):\n'
            '        c = _var_canon(target)\n'
            '        if c in var_index:\n'
            '            return var_index[c]\n'
            '        if _strip_suffix(c) in var_index:\n'
            '            return var_index[_strip_suffix(c)]\n'
            '        # Try the reverse — target is base, look for suffixed forms.\n'
            '        for sfx in _SUFFIXES:\n'
            '            if (c + sfx) in var_index:\n'
            '                return var_index[c + sfx]\n'
            '        return None\n'
            '\n'
            '    resolved = {}\n'
            '    for need_var, label in [(metric, "metric"),\n'
            '                              (rate_var_name, "rate_var_name"),\n'
            '                              (balance_var_name, "balance_var_name")]:\n'
            '        match = _resolve_var(need_var)\n'
            '        if match is None:\n'
            '            return {\n'
            '                "error":               f"`{label}={need_var!r}` not in csv `variable_name`",\n'
            '                "available_variables": sorted(variable_names),\n'
            '                "match_rules":         ("matching is case-, underscore-, and "\n'
            '                                         "suffix-insensitive (_mm, _pct, _apr, _apy)"),\n'
            '                "csv_path":            csv_path,\n'
            '            }\n'
            '        resolved[label] = match\n'
            '\n'
            '    actual_metric           = resolved["metric"]\n'
            '    actual_rate_var_name    = resolved["rate_var_name"]\n'
            '    actual_balance_var_name = resolved["balance_var_name"]\n'
            '\n'
            '    # Pivot to wide form: one row per (scenario, snap_date,\n'
            '    # product_name) with rate / balance / metric columns.\n'
            '    pivot_src = df[df["variable_name"].isin([\n'
            '        actual_rate_var_name, actual_balance_var_name, actual_metric,\n'
            '    ])]\n'
            '    wide = pivot_src.pivot_table(\n'
            '        index=["scenario", "snap_date", "product_name"],\n'
            '        columns="variable_name", values="variable_value",\n'
            '        aggfunc="sum",\n'
            '    ).reset_index()\n'
            '\n'
            '    cur = wide[wide["scenario"] == current_scenario]\n'
            '    ben = wide[wide["scenario"] == benchmark_scenario]\n'
            '\n'
            '    join_keys = ["snap_date", "product_name"]\n'
            '    m = cur.merge(ben, on=join_keys, how="outer", suffixes=("_cur", "_ben"))\n'
            '    m = m.fillna(0.0)\n'
            '\n'
            '    # Period factor — annualization scaler from rate × balance to\n'
            '    # interest expense. Default 1/12 (monthly snap_dates).\n'
            '    if period_factor is None:\n'
            '        period_factor = 1.0 / 12.0\n'
            '        assumption_notes.append("Period factor defaulted to 1/12 (monthly snap_dates).")\n'
            '\n'
            '    # ── Sequential V / M / R decomposition ─────────────────────────\n'
            '    # Per-period (snap_date) decomposition reconciling exactly to the\n'
            '    # computed ΔIE. Following the standard sequential framework:\n'
            '    #\n'
            '    #   IE       = B_tot × Σ_i (m_i × r_i)\n'
            '    #   Volume_t = (B_tot_S - B_tot_B) × Σ(m_B × r_B)         (locks mix+rate)\n'
            '    #   Mix_t    = B_tot_S × Σ((m_S - m_B) × r_B)             (locks rate, total at S)\n'
            '    #   Rate_t   = Σ(B_S × (r_S - r_B))                       (locks total+mix at S)\n'
            '    #   ΔIE_t    = Volume_t + Mix_t + Rate_t  (exact, no residual)\n'
            '    #\n'
            '    # Per-product splits sum back to the totals:\n'
            '    #   Volume_i = (B_tot_S - B_tot_B) × m_B,i × r_B,i\n'
            '    #   Mix_i    = B_tot_S × (m_S,i - m_B,i) × r_B,i\n'
            '    #   Rate_i   = B_S,i × (r_S,i - r_B,i)\n'
            '    # All multiplied by period_factor and (rate_in_pct / 100).\n'
            '\n'
            '    # ── Unit detection: convert balance + metric to $MM ─────────────\n'
            '    # The CSV may carry monetary values in different scales — raw $1\n'
            '    # is common for CCAR exports, $MM for retail-suite outputs, $B for\n'
            '    # board-deck snapshots. The tool reports everything in $MM (per\n'
            '    # field naming `_mm`), so we detect each money column\'s scale and\n'
            '    # convert before the math. Auto-detection: variable_name suffix\n'
            '    # first (`_mm`/`_b`/`_k`), then magnitude heuristic. Explicit\n'
            '    # override via balance_scale_to_mm / metric_scale_to_mm.\n'
            '    def _detect_scale_to_mm(var_name, series, override):\n'
            '        if override is not None:\n'
            '            return float(override), f"explicit override ({override})"\n'
            '        try:\n'
            '            max_abs = float(series.abs().max())\n'
            '        except Exception:\n'
            '            max_abs = 0.0\n'
            '        n = str(var_name).lower()\n'
            '        # MAGNITUDE WINS at the extremes — if a column is named\n'
            '        # `interest_expense_mm` but values are 5,000,000,000, the\n'
            '        # column header is lying (5B MM = $5 quadrillion is not\n'
            '        # a real CCAR figure). Detect raw-$ files even when the\n'
            '        # generator stamped `_mm` on the column.\n'
            '        if max_abs > 1e7:\n'
            '            return 1e-6, f"max |value| = {max_abs:,.0f} > 1e7 — too large for $MM, treating as raw $1 (×1e-6 → $MM)"\n'
            '        if 0 < max_abs < 1e-3:\n'
            '            return 1000.0, f"max |value| = {max_abs:.5f} < 1e-3 — too small for $MM, treating as $B (×1000 → $MM)"\n'
            '        # In the typical-MM range the suffix is the most reliable\n'
            '        # indicator (numbers like 0.5M and 500M are both plausible).\n'
            '        if n.endswith("_mm") or n.endswith("mm"):\n'
            '            return 1.0, "suffix `mm` and magnitude consistent — already in $MM"\n'
            '        if n.endswith("_b") or n.endswith("_bn") or n.endswith("bn"):\n'
            '            return 1000.0, "suffix `b/bn` → in $B, ×1000 → $MM"\n'
            '        if n.endswith("_k") or n.endswith("_thousand"):\n'
            '            return 0.001, "suffix `k/thousand` → in $K, ÷1000 → $MM"\n'
            '        return 1.0, f"max |value| = {max_abs:,.2f}, no suffix hint — assumed $MM"\n'
            '\n'
            '    bal_series_raw = pd.concat([\n'
            '        m[f"{actual_balance_var_name}_ben"], m[f"{actual_balance_var_name}_cur"]\n'
            '    ], ignore_index=True)\n'
            '    metric_series_raw = pd.concat([\n'
            '        m[f"{actual_metric}_ben"], m[f"{actual_metric}_cur"]\n'
            '    ], ignore_index=True) if (f"{actual_metric}_ben" in m.columns) else None\n'
            '\n'
            '    bal_scale, bal_scale_note = _detect_scale_to_mm(\n'
            '        actual_balance_var_name, bal_series_raw, balance_scale_to_mm,\n'
            '    )\n'
            '    if metric_series_raw is not None:\n'
            '        met_scale, met_scale_note = _detect_scale_to_mm(\n'
            '            actual_metric, metric_series_raw, metric_scale_to_mm,\n'
            '        )\n'
            '        # When balance was rescaled (raw $ / $K / $B) but the\n'
            '        # metric stayed at 1.0 from suffix matching, INHERIT the\n'
            '        # balance scale. CCAR files almost always use a consistent\n'
            '        # unit across columns, and the column suffix can lie\n'
            '        # (e.g. `interest_expense_mm` carrying raw-$ values). The\n'
            '        # only signal we can trust here is balance\'s magnitude;\n'
            '        # suffix consistency is unreliable. Explicit override\n'
            '        # still wins.\n'
            '        if metric_scale_to_mm is None and bal_scale != 1.0 and met_scale == 1.0:\n'
            '            met_scale = bal_scale\n'
            '            met_scale_note = (\n'
            '                f"inherited from balance ({bal_scale}); column suffix "\n'
            '                "wasn\'t reliable enough to override"\n'
            '            )\n'
            '    else:\n'
            '        met_scale, met_scale_note = 1.0, "metric column not present in pivot"\n'
            '\n'
            '    if bal_scale != 1.0:\n'
            '        assumption_notes.append(f"Balance unit detected: {bal_scale_note}")\n'
            '    if met_scale != 1.0:\n'
            '        assumption_notes.append(f"Metric unit detected: {met_scale_note}")\n'
            '\n'
            '    bal_b  = m[f"{actual_balance_var_name}_ben"] * bal_scale\n'
            '    bal_s  = m[f"{actual_balance_var_name}_cur"] * bal_scale\n'
            '    rate_b = m[f"{actual_rate_var_name}_ben"]    / 100.0\n'
            '    rate_s = m[f"{actual_rate_var_name}_cur"]    / 100.0\n'
            '\n'
            '    # Total balance per snap_date for each scenario (groupby trick).\n'
            '    m_aug = m.assign(_bal_b=bal_b, _bal_s=bal_s, _rate_b=rate_b, _rate_s=rate_s)\n'
            '    btot = m_aug.groupby("snap_date")[["_bal_b", "_bal_s"]].transform("sum")\n'
            '    btot_b = btot["_bal_b"].replace(0, float("nan"))\n'
            '    btot_s = btot["_bal_s"].replace(0, float("nan"))\n'
            '\n'
            '    mix_b = (bal_b / btot_b).fillna(0)\n'
            '    mix_s = (bal_s / btot_s).fillna(0)\n'
            '\n'
            '    bal_diff = bal_s - bal_b\n'
            '    btot_diff = btot_s.fillna(0) - btot_b.fillna(0)\n'
            '\n'
            '    # Per-product, per-period effects (in $MM, since balance is in MM\n'
            '    # already and rate is now decimal).\n'
            '    vol_eff  = btot_diff * mix_b * rate_b * period_factor\n'
            '    mix_eff  = btot_s.fillna(0) * (mix_s - mix_b) * rate_b * period_factor\n'
            '    rate_eff = bal_s * (rate_s - rate_b) * period_factor\n'
            '    total_eff_per_row = vol_eff + mix_eff + rate_eff  # equals ΔIE per row by construction\n'
            '\n'
            '    by_product = (\n'
            '        m.assign(_vol=vol_eff, _mix=mix_eff, _rate=rate_eff, _tot=total_eff_per_row)\n'
            '        .groupby(["product_name"], dropna=False)\n'
            '        .agg(total_var=("_tot", "sum"),\n'
            '             rate_effect_mm=("_rate", "sum"),\n'
            '             volume_effect_mm=("_vol", "sum"),\n'
            '             mix_effect_mm=("_mix", "sum"))\n'
            '        .reset_index()\n'
            '    )\n'
            '    by_product_rows = [\n'
            '        {"product": r["product_name"],\n'
            '         "total_variance_mm": round(float(r["total_var"]), 2),\n'
            '         "rate_effect_mm":    round(float(r["rate_effect_mm"]), 2),\n'
            '         "volume_effect_mm":  round(float(r["volume_effect_mm"]), 2),\n'
            '         "mix_effect_mm":     round(float(r["mix_effect_mm"]), 2)}\n'
            '        for _, r in by_product.iterrows()\n'
            '    ]\n'
            '    by_product_rows.sort(key=lambda r: -abs(r["total_variance_mm"]))\n'
            '\n'
            '    rate_total  = round(float(rate_eff.sum()), 2)\n'
            '    vol_total   = round(float(vol_eff.sum()),  2)\n'
            '    mix_total   = round(float(mix_eff.sum()),  2)\n'
            '    total_var   = round(rate_total + vol_total + mix_total, 2)\n'
            '\n'
            '    # Sanity: if the file has an `interest_expense` column too, surface\n'
            '    # the gap between the data ΔIE and the computed ΔIE so the analyst\n'
            '    # knows whether the model output reconciles to the formula.\n'
            '    data_ie_delta = None\n'
            '    if actual_metric in m.columns or f"{actual_metric}_cur" in m.columns:\n'
            '        try:\n'
            '            data_ie_delta = round(\n'
            '                float((m[f"{actual_metric}_cur"] - m[f"{actual_metric}_ben"]).sum()) * met_scale,\n'
            '                2,\n'
            '            )\n'
            '        except Exception:\n'
            '            data_ie_delta = None\n'
            '    if data_ie_delta is not None and abs(data_ie_delta - total_var) > 0.01:\n'
            '        gap_pct = abs(data_ie_delta - total_var) / max(abs(total_var), 1e-9) * 100\n'
            '        assumption_notes.append(\n'
            '            "Computed ΔIE (bal × rate × period_factor) is "\n'
            '            + str(total_var) + " MM; the file\'s `" + str(actual_metric)\n'
            '            + "` column shows " + str(data_ie_delta) + " MM (gap "\n'
            '            + format(gap_pct, ".1f") + "%). Decomposition follows the "\n'
            '            "computed value so V+M+R reconciles exactly."\n'
            '        )\n'
            '\n'
            '    run_ids = sorted(df["Run_ID"].astype(str).unique().tolist())\n'
            '\n'
            '    # ── Audit block for attribution-challenger ─────────────────────\n'
            '    # Pre-compute the signals the next agent will check, so it can act\n'
            '    # on structured data instead of re-deriving from by_product.\n'
            '    MATERIALITY_PCT = 5.0\n'
            '    abs_total = max(abs(total_var), 1e-9)\n'
            '    material_products = []\n'
            '    immaterial_products = []\n'
            '    for r in by_product_rows:\n'
            '        share_pct = abs(r["total_variance_mm"]) / abs_total * 100.0\n'
            '        (material_products if share_pct >= MATERIALITY_PCT else immaterial_products).append(r["product"])\n'
            '\n'
            '    # V+M+R reconciliation residual (should be 0 by construction;\n'
            '    # any non-zero is pure 2dp rounding of the individual effects).\n'
            '    vmr_diff = round(rate_total + vol_total + mix_total - total_var, 4)\n'
            '\n'
            '    # by_product sum vs top-level (each row rounded to 2dp; sum can\n'
            '    # drift by a cent or two — exposed so challenger can tell\n'
            '    # rounding noise from a real aggregation bug).\n'
            '    by_prod_total_sum = round(sum(r["total_variance_mm"] for r in by_product_rows), 4)\n'
            '    bp_diff = round(by_prod_total_sum - total_var, 4)\n'
            '\n'
            '    formula_vs_data_pct = None\n'
            '    if data_ie_delta is not None and abs(total_var) > 1e-9:\n'
            '        formula_vs_data_pct = round(\n'
            '            abs(data_ie_delta - total_var) / abs(total_var) * 100.0, 2,\n'
            '        )\n'
            '\n'
            '    # Per-product data-completeness check. A product missing rate or\n'
            '    # balance for some snap_dates would skew its per-product effects.\n'
            '    expected_periods = len(snap_dates_unique := sorted(df["snap_date"].astype(str).unique().tolist()))\n'
            '    products_with_partial = []\n'
            '    for prod in m["product_name"].dropna().unique():\n'
            '        sub = m[m["product_name"] == prod]\n'
            '        # Each row in `m` is a (snap_date × product) merge result with\n'
            '        # both _cur and _ben columns; missing rows show as NaN above.\n'
            '        present_periods = len(sub["snap_date"].dropna().unique())\n'
            '        if present_periods < expected_periods:\n'
            '            products_with_partial.append(str(prod))\n'
            '\n'
            '    period_factor_defaulted = any(\n'
            '        "Period factor defaulted" in n for n in assumption_notes\n'
            '    )\n'
            '    fallback_used = any(\n'
            '        "fall" in n.lower() or "fallback" in n.lower() for n in assumption_notes\n'
            '    )\n'
            '\n'
            '    audit = {\n'
            '        "balance_scale_to_mm":                   round(float(bal_scale), 9),\n'
            '        "metric_scale_to_mm":                    round(float(met_scale), 9),\n'
            '        "balance_scale_note":                    bal_scale_note,\n'
            '        "metric_scale_note":                     met_scale_note,\n'
            '        "aggregation_method":                    (\n'
            '            f"data_ie_delta_mm = sum over {len(by_product_rows)} products × "\n'
            '            f"{expected_periods} snap_dates of (IE_cur − IE_ben) × {met_scale}; "\n'
            '            f"V/M/R effects similarly aggregate per-product per-period contributions"\n'
            '        ),\n'
            '        "materiality_threshold_pct":             MATERIALITY_PCT,\n'
            '        "material_products":                     material_products,\n'
            '        "immaterial_products":                   immaterial_products,\n'
            '        "reconciliation_v_plus_m_plus_r_diff_mm": vmr_diff,\n'
            '        "reconciliation_by_product_sum_diff_mm":  bp_diff,\n'
            '        "formula_vs_data_gap_mm":                (None if data_ie_delta is None\n'
            '                                                  else round(data_ie_delta - total_var, 2)),\n'
            '        "formula_vs_data_gap_pct":               formula_vs_data_pct,\n'
            '        "period_factor_was_defaulted":           period_factor_defaulted,\n'
            '        "fallback_scenarios_used":               fallback_used,\n'
            '        "products_count":                        len(by_product_rows),\n'
            '        "snap_date_count":                       expected_periods,\n'
            '        "products_with_partial_data":            sorted(products_with_partial),\n'
            '    }\n'
            '\n'
            '    return {\n'
            '        "current_scenario":           current_scenario,\n'
            '        "benchmark_scenario":         benchmark_scenario,\n'
            '        "metric":                     actual_metric,\n'
            '        "metric_requested":           metric,\n'
            '        "rate_var_name":              actual_rate_var_name,\n'
            '        "balance_var_name":           actual_balance_var_name,\n'
            '        "period_factor":              round(float(period_factor), 4),\n'
            '        "csv_path_used":              csv_path,\n'
            '        "playbook_id":                playbook_id,\n'
            '        "run_ids":                    run_ids[:8],\n'
            '        "snap_dates":                 sorted(df["snap_date"].astype(str).unique().tolist())[:24],\n'
            '        "total_variance_mm":          total_var,\n'
            '        "rate_effect_mm":             rate_total,\n'
            '        "volume_effect_mm":           vol_total,\n'
            '        "mix_effect_mm":              mix_total,\n'
            '        "data_ie_delta_mm":           data_ie_delta,\n'
            '        "by_product":                 by_product_rows,\n'
            '        "assumptions":                "; ".join(assumption_notes) or None,\n'
            '        "audit":                      audit,\n'
            '    }\n'
        ),
    )

    # ── Agent 4 — fact-checker ────────────────────────────────────────────
    ctx.register_python_tool(
        name="verify_numbers_in_narrative",
        description=(
            "Extract dollar figures from Agent 3's narrative text and "
            "verify each against Agent 1's variance JSON. Returns one row "
            "per claimed number with whether it ties (within tolerance)."
        ),
        parameters=[
            {"name": "narrative",       "type": "string",
             "description": "Full narrative text from Agent 3 (slide_header + drivers + overlays concatenated).",
             "required": True},
            {"name": "variance_json",   "type": "object",
             "description": "Agent 1's variance walk output (dict).",
             "required": True},
            {"name": "tolerance_pct",   "type": "number",
             "description": "Tolerance for matching numbers (default 0.10 = 10%).",
             "required": False},
        ],
        python_source=(
            'def verify_numbers_in_narrative(narrative, variance_json, tolerance_pct=0.10):\n'
            '    """Heuristic fact-checker: pull every $X.XB / $X.XMM figure from prose,\n'
            '    flag if absent from the variance JSON values."""\n'
            '    import re\n'
            '\n'
            '    # Collect known values (in $MM) from the variance JSON.\n'
            '    known_mm = set()\n'
            '    def add(val):\n'
            '        try:\n'
            '            v = float(val)\n'
            '            if abs(v) > 0.001:\n'
            '                known_mm.add(round(v, 2))\n'
            '        except Exception:\n'
            '            pass\n'
            '    for k in ("total_variance_mm", "starting_point_variance_mm",\n'
            '             "scenario_change_mm", "rate_effect_mm", "volume_effect_mm",\n'
            '             "mix_effect_mm"):\n'
            '        add(variance_json.get(k))\n'
            '    for row in (variance_json.get("by_product") or []):\n'
            '        for k in ("total_variance_mm", "rate_effect_mm",\n'
            '                  "volume_effect_mm", "mix_effect_mm"):\n'
            '            add(row.get(k))\n'
            '\n'
            '    # Extract $X.XB or $X.XMM tokens from the narrative.\n'
            '    pat = re.compile(r"\\$\\s*([+-]?[0-9]+(?:\\.[0-9]+)?)\\s*(B|MM|M)\\b", re.IGNORECASE)\n'
            '    checks = []\n'
            '    for m in pat.finditer(narrative):\n'
            '        amount = float(m.group(1))\n'
            '        unit = m.group(2).upper()\n'
            '        as_mm = amount * 1000.0 if unit == "B" else amount\n'
            '        # Tolerance bands\n'
            '        tol = max(abs(as_mm) * float(tolerance_pct), 0.01)\n'
            '        match = next((kv for kv in known_mm if abs(kv - as_mm) <= tol\n'
            '                       or abs(kv + as_mm) <= tol), None)\n'
            '        checks.append({\n'
            '            "claim":            m.group(0),\n'
            '            "claimed_mm":       round(as_mm, 2),\n'
            '            "tolerance_passed": match is not None,\n'
            '            "matched_value_mm": match,\n'
            '        })\n'
            '\n'
            '    all_pass = all(c["tolerance_passed"] for c in checks) if checks else True\n'
            '    return {\n'
            '        "all_numbers_tie":  all_pass,\n'
            '        "checks":           checks,\n'
            '        "known_values_mm":  sorted(known_mm),\n'
            '    }\n'
        ),
    )

    # ── Challenger — SR 11-7 red-flag patterns ────────────────────────────
    ctx.register_python_tool(
        name="audit_logic_rules",
        description=(
            "Run the registered SR 11-7-style red-flag checklist against a "
            "claim or narrative. Returns each rule with a `tripped` flag, "
            "a `severity`, and the `regulator_question` a reviewer would "
            "ask. Used by the attribution-challenger skill to find logical gaps "
            "(marketing → 0 but flat NABs, rate ↑ but beta ≈ 0, overlay "
            "without re-cal plan, etc.)."
        ),
        parameters=[
            {"name": "narrative", "type": "string",
             "description": "The claim or drafted narrative to audit.",
             "required": True},
            {"name": "context",   "type": "object",
             "description": "Optional structured signals — e.g. variance JSON, model assumption block, scenario tag — that lets some rules check more precisely.",
             "required": False},
        ],
        python_source=(
            'def audit_logic_rules(narrative, context=None):\n'
            '    """Heuristic SR 11-7 red-flag scan over a claim / narrative."""\n'
            '    import re\n'
            '\n'
            '    text = (narrative or "").lower()\n'
            '    ctx_d = context or {}\n'
            '\n'
            '    rules = [\n'
            '        {\n'
            '            "id": "marketing_to_zero_but_flat_nabs",\n'
            '            "label": "Marketing → 0 but new accounts unchanged",\n'
            '            "severity": "high",\n'
            '            "trigger": (\n'
            '                ("marketing" in text and ("zero" in text or "$0" in text or "to 0" in text))\n'
            '                and ("flat" in text or "unchanged" in text or "consistent" in text\n'
            '                     or "interchange" in text)\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "If marketing drops to $0, what empirical elasticity links marketing "\n'
            '                "spend to new-account inflows? The narrative implies NABs are macro-driven only."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.4 — Implementation, Use, Validation",\n'
            '        },\n'
            '        {\n'
            '            "id": "rate_up_beta_zero",\n'
            '            "label": "Rate ↑ but deposit beta ≈ 0",\n'
            '            "severity": "high",\n'
            '            "trigger": (\n'
            '                ("rate" in text and ("higher" in text or "increase" in text or "rising" in text))\n'
            '                and ("beta" in text and ("zero" in text or "0.0" in text or "no pass-through" in text))\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "Deposit pricing should track at non-zero beta in a rising-rate regime. "\n'
            '                "Is there a documented beta floor? What did 2022-2023 imply empirically?"\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.3 — Model Development, Implementation",\n'
            '        },\n'
            '        {\n'
            '            "id": "non_macro_model_in_stress",\n'
            '            "label": "Non-macro-sensitive model used in stress scenario",\n'
            '            "severity": "medium",\n'
            '            "trigger": (\n'
            '                ("non-macro" in text or "no macro" in text or "macro-insensitive" in text\n'
            '                 or "checking aof" in text)\n'
            '                and ("stress" in text or "ccar" in text)\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "If the model has no macro features, on what basis is its stress-period "\n'
            '                "behavior validated? Backtesting against 2008/2020 should be required."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.5 — Outcomes Analysis (Backtesting)",\n'
            '        },\n'
            '        {\n'
            '            "id": "overlay_without_recal_plan",\n'
            '            "label": "Overlay applied without re-calibration plan",\n'
            '            "severity": "high",\n'
            '            "trigger": (\n'
            '                ("overlay" in text)\n'
            '                and not ("re-calibrat" in text or "recal" in text or "permanent" in text\n'
            '                         or "retire" in text)\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "What is the timeline to re-calibrate the underlying model so the overlay can be retired? "\n'
            '                "Permanent overlays without a re-cal plan are a recurring SR 11-7 finding."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.4 — Use Limitations",\n'
            '        },\n'
            '        {\n'
            '            "id": "methodology_change_as_scenario_impact",\n'
            '            "label": "Methodology change attributed as scenario impact",\n'
            '            "severity": "high",\n'
            '            "trigger": (\n'
            '                ("benchmark" in text or "big 6" in text or "big 8" in text\n'
            '                 or "reconstitut" in text or "re-cal" in text)\n'
            '                and ("scenario" in text or "stress" in text)\n'
            '                and not ("methodology" in text or "model update" in text)\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "Is this delta a model/methodology change or a scenario-input change? "\n'
            '                "Mixing the two understates the size of methodology updates between cycles."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.4 — Change Management",\n'
            '        },\n'
            '        {\n'
            '            "id": "dfs_modeled_with_capital_one_only_data",\n'
            '            "label": "DFS / Discover behavior modeled with Capital One-only data",\n'
            '            "severity": "high",\n'
            '            "trigger": (\n'
            '                ("dfs" in text or "discover" in text)\n'
            '                and not ("validat" in text or "back-test" in text or "backtest" in text\n'
            '                         or "demograph" in text or "tenure" in text or "cohort align" in text)\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "How is DFS behavior validated against Capital One PSAV cohorts? "\n'
            '                "Demographic / tenure / channel differences should be empirically tested."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.3 — Data Quality and Relevance",\n'
            '        },\n'
            '        {\n'
            '            "id": "judgment_floor_no_history",\n'
            '            "label": "Floor / cap calibrated to management judgment with no historical anchor",\n'
            '            "severity": "medium",\n'
            '            "trigger": (\n'
            '                ("floor" in text or "cap" in text or "limit" in text)\n'
            '                and ("judgment" in text or "qualitative" in text or "expert" in text)\n'
            '                and not ("2008" in text or "2020" in text or "historical" in text\n'
            '                         or "back-test" in text or "backtest" in text)\n'
            '            ),\n'
            '            "regulator_question": (\n'
            '                "What historical period (2008 / 2020 / 2023) anchors the floor? "\n'
            '                "Pure management-judgment floors are weak under SR 11-7."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 §III.3 — Calibration",\n'
            '        },\n'
            '    ]\n'
            '\n'
            '    flags = [\n'
            '        {"id": r["id"], "label": r["label"], "severity": r["severity"],\n'
            '          "regulator_question": r["regulator_question"],\n'
            '          "rule_citation": r["rule_citation"], "evidence": None}\n'
            '        for r in rules if r["trigger"]\n'
            '    ]\n'
            '    passed_ids = [r["id"] for r in rules if not r["trigger"]]\n'
            '\n'
            '    # ── Structured-input rules ─────────────────────────────────────────\n'
            '    # These read from `context` rather than text — they require the\n'
            '    # caller to pass typed phase outputs:\n'
            '    #   context = {\n'
            '    #     "variance":    VarianceWalkResult dict (has by_product),\n'
            '    #     "attributions": AttributionsResult dict (has top_movers + attributions),\n'
            '    #     "commentary":  CommentaryResult dict (has numeric_claims, verification_failures),\n'
            '    #   }\n'
            '    variance     = ctx_d.get("variance")     or {}\n'
            '    attributions = ctx_d.get("attributions") or {}\n'
            '    commentary   = ctx_d.get("commentary")   or {}\n'
            '\n'
            '    by_product   = variance.get("by_product")   or []\n'
            '    total_var    = variance.get("total_variance_mm")\n'
            '    top_movers   = attributions.get("top_movers") or []\n'
            '    attrs_list   = attributions.get("attributions") or []\n'
            '\n'
            '    # Rule M1 — materiality_omission: a product contributes >5% of\n'
            '    # total variance but is NOT in top_movers. The narrative will\n'
            '    # silently skip it — a regulator will ask why.\n'
            '    if by_product and total_var:\n'
            '        try:\n'
            '            tot = abs(float(total_var))\n'
            '            top_set = set()\n'
            '            for m in top_movers:\n'
            '                top_set.add(m.get("product"))\n'
            '            omitted = []\n'
            '            for row in by_product:\n'
            '                pname = row.get("product")\n'
            '                pvar  = abs(float(row.get("total_variance_mm") or 0))\n'
            '                if tot > 0 and pname not in top_set and pvar / tot > 0.05:\n'
            '                    pct = pvar / tot * 100\n'
            '                    omitted.append(str(pname) + " (" + format(pct, ".1f") + "% of total)")\n'
            '            if omitted:\n'
            '                evidence = ("These products contribute >5% of total variance "\n'
            '                             "but are not narrated downstream: " + ", ".join(omitted) + ".")\n'
            '                flags.append({\n'
            '                    "id":       "materiality_omission",\n'
            '                    "label":    "Material product missing from top_movers",\n'
            '                    "severity": "high",\n'
            '                    "evidence": evidence,\n'
            '                    "regulator_question": (\n'
            '                        "The commentary does not mention these material movers. "\n'
            '                        "Why were they excluded from the top-mover ranking?"\n'
            '                    ),\n'
            '                    "rule_citation": "SR 11-7 \\u00a7III.4 \\u2014 Use Limitations / Selective Disclosure",\n'
            '                })\n'
            '        except Exception:\n'
            '            pass  # malformed inputs - skip the rule rather than break the audit\n'
            '\n'
            '    # Rule M2 — effect_component_mismatch: a top_movers entry says\n'
            '    # primary_effect="rate" but the cited model_component is a Volume\n'
            '    # model (or vice versa). The "why" does not match the math.\n'
            '    PRICING_MODELS = set([\n'
            '        "PRED_RETAILDEPOSIT_LIQUIDRATE", "PRED_RETAILDEPOSIT_CDRATE",\n'
            '    ])\n'
            '    VOLUME_MODELS = set([\n'
            '        "PRED_RETAILDEPOSIT_NEWORIGINATIONS",\n'
            '        "PRED_RETAILDEPOSIT_BACKBOOKBALANCE",\n'
            '        "PRED_RETAILDEPOSIT_FRONTBOOKBALANCE",\n'
            '        "PRED_RETAILDEPOSIT_BRANCHBALANCE",\n'
            '        "PRED_RETAILDEPOSIT_CDATTRITION",\n'
            '    ])\n'
            '    mismatches = []\n'
            '    for m in top_movers:\n'
            '        eff  = (m.get("primary_effect") or "").lower()\n'
            '        comp = m.get("model_component") or ""\n'
            '        prod = m.get("product") or ""\n'
            '        if not eff or not comp:\n'
            '            continue\n'
            '        if eff == "rate" and comp in VOLUME_MODELS:\n'
            '            mismatches.append(str(prod) + ": rate-driven but cites volume model " + str(comp))\n'
            '        elif eff == "volume" and comp in PRICING_MODELS:\n'
            '            mismatches.append(str(prod) + ": volume-driven but cites pricing model " + str(comp))\n'
            '    if mismatches:\n'
            '        flags.append({\n'
            '            "id":       "effect_component_mismatch",\n'
            '            "label":    "Cited model_component does not match primary_effect",\n'
            '            "severity": "high",\n'
            '            "evidence": "; ".join(mismatches),\n'
            '            "regulator_question": (\n'
            '                "How can a rate-driven move be attributed to a Volume model "\n'
            '                "(or vice versa)? The attribution chain is internally inconsistent."\n'
            '            ),\n'
            '            "rule_citation": "SR 11-7 \\u00a7III.4 \\u2014 Implementation Logic",\n'
            '        })\n'
            '\n'
            '    # Rule M3 — unattributed_top_mover: a product is in top_movers\n'
            '    # but has no entry in `attributions`. The "what" is named without\n'
            '    # a "why".\n'
            '    if top_movers and attrs_list is not None:\n'
            '        attr_components = set()\n'
            '        for a in attrs_list:\n'
            '            attr_components.add(a.get("model_component"))\n'
            '        attr_drivers = []\n'
            '        for a in attrs_list:\n'
            '            attr_drivers.append((a.get("driver") or "").lower())\n'
            '        unattributed = []\n'
            '        for m in top_movers:\n'
            '            comp = m.get("model_component")\n'
            '            prod = (m.get("product") or "").lower()\n'
            '            if not comp:\n'
            '                continue\n'
            '            if comp in attr_components:\n'
            '                continue\n'
            '            linked = False\n'
            '            if prod:\n'
            '                for d in attr_drivers:\n'
            '                    if prod in d:\n'
            '                        linked = True\n'
            '                        break\n'
            '            if linked:\n'
            '                continue\n'
            '            unattributed.append(str(m.get("product")) + " (" + str(comp) + ")")\n'
            '        if unattributed:\n'
            '            flags.append({\n'
            '                "id":       "unattributed_top_mover",\n'
            '                "label":    "Top mover with no methodology attribution",\n'
            '                "severity": "medium",\n'
            '                "evidence": "; ".join(unattributed),\n'
            '                "regulator_question": (\n'
            '                    "These movers are flagged as material but no "\n'
            '                    "methodology row explains why they moved. What "\n'
            '                    "drove the change?"\n'
            '                ),\n'
            '                "rule_citation": "SR 11-7 \\u00a7III.4 \\u2014 Documentation",\n'
            '            })\n'
            '\n'
            '    # Append the structured rule ids to passed_ids when they didn\'t\n'
            '    # trip, so callers can tell us which checks ran.\n'
            '    structured_ids = ["materiality_omission", "effect_component_mismatch", "unattributed_top_mover"]\n'
            '    flagged_ids = {f["id"] for f in flags}\n'
            '    for sid in structured_ids:\n'
            '        if sid not in flagged_ids:\n'
            '            passed_ids.append(sid)\n'
            '\n'
            '    return {\n'
            '        "tripped":       flags,\n'
            '        "passed":        passed_ids,\n'
            '        "rule_count":    len(rules) + len(structured_ids),\n'
            '        "max_severity":  max((f["severity"] for f in flags),\n'
            '                              key=lambda s: ["low","medium","high","critical"].index(s),\n'
            '                              default="none"),\n'
            '    }\n'
        ),
    )

    # ── Methodology / Challenger — per-product assumption lookup ──────────
    ctx.register_python_tool(
        name="get_model_assumptions",
        description=(
            "Return the documented assumption block for a retail-deposit "
            "product or cohort — beta, attrition floor, recapture rate, "
            "marketing pullback path, overlay status. Demo data; in "
            "production, back this with a registered config table."
        ),
        parameters=[
            {"name": "product", "type": "string",
             "description": "Product / cohort key. One of: 'PSAV' (Performance Savings), 'DFS_CD', 'DFS_SAVINGS', '360_SAVINGS', 'BRANCH_CHECKING', 'COMM_TIME', 'SBB_LIQUID'.",
             "required": True},
        ],
        python_source=(
            'def get_model_assumptions(product):\n'
            '    """Return per-product assumption block. Demo-grade; values\n'
            '    are illustrative and aligned with the bundled whitepapers."""\n'
            '    catalog = {\n'
            '        "PSAV": {\n'
            '            "model_id": "PRED_RETAILDEPOSIT_LIQUIDRATE",\n'
            '            "beta_floor": 0.30,\n'
            '            "beta_ceiling": 0.65,\n'
            '            "calibration_window": "2018-2024 monthly",\n'
            '            "marketing_elasticity": 0.18,\n'
            '            "validation_note": "PSAV is the legacy CapitalOne high-yield savings cohort. Used as the proxy for DFS savings until Q3-2026.",\n'
            '            "overlays": [],\n'
            '        },\n'
            '        "DFS_CD": {\n'
            '            "model_id": "PRED_RETAILDEPOSIT_CDRATE",\n'
            '            "beta_floor": 0.40,\n'
            '            "beta_ceiling": 0.85,\n'
            '            "benchmark": "Big 6 (Big 8 retired Q4-2025 to remove DFS double-count)",\n'
            '            "recapture_rate_at_maturity": 0.62,\n'
            '            "early_withdrawal_floor_pct": 0.015,\n'
            '            "validation_note": "Recapture rate is calibrated against 2018-2024 Discover internal data — DFS-native, not PSAV proxy.",\n'
            '            "overlays": [\n'
            '                {"name": "DFS CD benchmark overlay", "size_bps": 10,\n'
            '                 "rationale": "Discover historically prices ~10 bps above the Big 6 anchor; overlay preserves that spread post-acquisition."}\n'
            '            ],\n'
            '        },\n'
            '        "DFS_SAVINGS": {\n'
            '            "model_id": "PRED_RETAILDEPOSIT_LIQUIDRATE",\n'
            '            "beta_floor": 0.35,\n'
            '            "beta_ceiling": 0.70,\n'
            '            "validation_note": "DFS savings cohort is 18 months post-acquisition; segment alignment to PSAV is documented but tenure differs (DFS book skews longer-tenure).",\n'
            '            "overlays": [],\n'
            '        },\n'
            '        "360_SAVINGS": {\n'
            '            "model_id": "PRED_RETAILDEPOSIT_LIQUIDRATE",\n'
            '            "beta_floor": 0.45,\n'
            '            "beta_ceiling": 0.80,\n'
            '            "validation_note": "360 Savings rate paid is post-hoc adjusted via the +25 bps overlay to match observed pricing; underlying model has not been re-fit since 2022.",\n'
            '            "overlays": [\n'
            '                {"name": "360 Savings Rate Paid Overlay", "size_bps": 25,\n'
            '                 "rationale": "Reflects competitive repricing not yet captured in the core Liquid Rate model. Re-cal targeted Q1-2026 per Model Risk Office."}\n'
            '            ],\n'
            '        },\n'
            '        "BRANCH_CHECKING": {\n'
            '            "model_id": "PRED_RETAILDEPOSIT_BRANCHBALANCE",\n'
            '            "macro_features": False,\n'
            '            "attrition_pct_annual": 0.04,\n'
            '            "validation_note": "Legacy COF branch-checking AOF model is non-macro-sensitive — known limitation; backtested only against 2018-2024 quiet period.",\n'
            '            "overlays": [],\n'
            '        },\n'
            '        "COMM_TIME": {\n'
            '            "model_id": "PRED_RETAILDEPOSIT_CDATTRITION",\n'
            '            "early_withdrawal_floor_pct": 0.020,\n'
            '            "renewal_recapture": 0.55,\n'
            '            "validation_note": "Floor calibrated to Q4-2008 / Q2-2020 idiosyncratic-withdrawal observations.",\n'
            '            "overlays": [],\n'
            '        },\n'
            '        "SBB_LIQUID": {\n'
            '            "model_id": "PRED_SBB_BALANCEMODEL",\n'
            '            "submodels": 5,\n'
            '            "status": "Temporary — promotion to Permanent targeted Q3-2026.",\n'
            '            "validation_note": "Suite split into 5 sub-models in CCAR-26 (merchant volume vs loan-linked sweep disentangled).",\n'
            '            "overlays": [],\n'
            '        },\n'
            '    }\n'
            '    key = (product or "").upper().replace(" ", "_").replace("-", "_")\n'
            '    if key not in catalog:\n'
            '        return {"error": f"Unknown product `{product}`. Available: {sorted(catalog.keys())}"}\n'
            '    return {"product": key, **catalog[key]}\n'
        ),
    )

    # ── Sensitivity walk — perturb a parameter, recompute IE ──────────────
    ctx.register_python_tool(
        name="compute_sensitivity_walk",
        description=(
            "Sensitivity branch of the variance walk: take a base scenario "
            "+ a parameter perturbation (recapture rate Δ, beta Δ, attrition "
            "floor Δ) and return the implied Interest_Expense_mm impact and "
            "the new total. Used to answer 'what if X were Y% different?' "
            "questions in the chat."
        ),
        parameters=[
            {"name": "scenario",            "type": "string",
             "description": "Scenario name to perturb (e.g. 'CCAR_26_BHC_Stress').",
             "required": True},
            {"name": "parameter",           "type": "string",
             "description": "Which parameter to shock: 'recapture_rate', 'beta', or 'attrition_floor'.",
             "required": True},
            {"name": "delta_pct",           "type": "number",
             "description": "Relative perturbation (e.g. -0.20 = 20% lower than base, +0.10 = 10% higher).",
             "required": True},
            {"name": "product",             "type": "string",
             "description": "Optional product scope (e.g. 'DFS_CD'). If omitted, applies to all retail products.",
             "required": False},
            {"name": "csv_path",            "type": "string",
             "description": "Path to CCAR_Retail_Outputs.csv. Defaults to bundled sample.",
             "required": False},
        ],
        python_source=(
            'def compute_sensitivity_walk(scenario, parameter, delta_pct,\n'
            '                              product=None, csv_path=None):\n'
            '    """Approximate sensitivity: scale rate/balance/attrition by delta_pct\n'
            '    and recompute Interest_Expense_mm against the base scenario row."""\n'
            '    import os\n'
            '    import pandas as pd\n'
            '\n'
            '    if not csv_path:\n'
            '        here = os.path.dirname(os.path.abspath(__file__))\n'
            '        repo = os.path.abspath(os.path.join(here, "..", "..", ".."))\n'
            '        csv_path = os.path.join(repo, "sample_data", "ccar", "CCAR_Retail_Outputs.csv")\n'
            '\n'
            '    df = pd.read_csv(csv_path)\n'
            '    needed = {"Scenario", "Quarter_ID", "Portfolio", "Product_L1", "Metric", "Value"}\n'
            '    missing = needed - set(df.columns)\n'
            '    if missing:\n'
            '        return {"error": f"CSV missing columns: {sorted(missing)}"}\n'
            '\n'
            '    valid_params = {"recapture_rate", "beta", "attrition_floor"}\n'
            '    if parameter not in valid_params:\n'
            '        return {"error": f"parameter must be one of {sorted(valid_params)}"}\n'
            '\n'
            '    sub = df[df["Scenario"] == scenario].copy()\n'
            '    if product:\n'
            '        prod_norm = product.upper().replace("_", " ").replace("-", " ")\n'
            '        sub = sub[sub["Product_L1"].str.upper().str.contains(prod_norm.split()[0])]\n'
            '    if sub.empty:\n'
            '        return {"error": f"No rows for scenario={scenario!r}, product={product!r}",\n'
            '                "available_scenarios": sorted(df["Scenario"].unique().tolist())}\n'
            '\n'
            '    wide = sub.pivot_table(\n'
            '        index=["Quarter_ID", "Portfolio", "Product_L1"],\n'
            '        columns="Metric", values="Value", aggfunc="first",\n'
            '    ).reset_index().fillna(0.0)\n'
            '\n'
            '    ie_series = wide.get("Interest_Expense_mm", pd.Series([0]))\n'
            '    # Same scale auto-detection compute_variance_walk uses — when\n'
            '    # the source CSV carries Interest_Expense in raw $1, the\n'
            '    # column header `Interest_Expense_mm` is a misnomer (the file\n'
            '    # generator stamped it MM-suffix even though the values are\n'
            '    # raw). Fall back to the magnitude heuristic so this tool and\n'
            '    # variance-analyst report sensitivity numbers on the same scale.\n'
            '    try:\n'
            '        max_abs = float(pd.Series(ie_series).abs().max())\n'
            '    except Exception:\n'
            '        max_abs = 0.0\n'
            '    if max_abs > 1e7:\n'
            '        ie_scale = 1e-6\n'
            '    elif 0 < max_abs < 1e-3:\n'
            '        ie_scale = 1000.0\n'
            '    else:\n'
            '        ie_scale = 1.0\n'
            '    base_ie_mm = float(ie_series.sum()) * ie_scale\n'
            '\n'
            '    # Approximate effect on Interest_Expense_mm:\n'
            '    #   beta            ≈ rate_paid scales by (1 + delta_pct)         → rate effect\n'
            '    #   attrition_floor ≈ balance scales by (1 - delta_pct/2)          → volume effect\n'
            '    #   recapture_rate  ≈ balance scales by (1 + delta_pct * 0.4)      → volume effect\n'
            '    rate_d, vol_d = 0.0, 0.0\n'
            '    if parameter == "beta":\n'
            '        rate_d = float(delta_pct)\n'
            '    elif parameter == "attrition_floor":\n'
            '        vol_d = -float(delta_pct) / 2.0\n'
            '    elif parameter == "recapture_rate":\n'
            '        vol_d = float(delta_pct) * 0.4\n'
            '\n'
            '    rate_eff = base_ie_mm * rate_d\n'
            '    vol_eff  = base_ie_mm * vol_d\n'
            '    mix_eff  = base_ie_mm * rate_d * vol_d\n'
            '    new_ie_mm = base_ie_mm + rate_eff + vol_eff + mix_eff\n'
            '\n'
            '    return {\n'
            '        "scenario":            scenario,\n'
            '        "parameter":           parameter,\n'
            '        "delta_pct":           float(delta_pct),\n'
            '        "product":             product,\n'
            '        "base_interest_expense_mm":   round(float(base_ie_mm), 2),\n'
            '        "perturbed_interest_expense_mm": round(float(new_ie_mm), 2),\n'
            '        "delta_mm":            round(float(new_ie_mm - base_ie_mm), 2),\n'
            '        "rate_effect_mm":      round(float(rate_eff), 2),\n'
            '        "volume_effect_mm":    round(float(vol_eff), 2),\n'
            '        "mix_effect_mm":       round(float(mix_eff), 2),\n'
            '        "ie_scale_to_mm":      ie_scale,\n'
            '        "method_note":         "Demo-grade linearization; production sensitivities should re-run the model.",\n'
            '    }\n'
        ),
    )

    # ── Beta justification chain ─────────────────────────────────────────
    # Three tools that back the four-agent beta-justification flow:
    #   beta-quant        → compute_projected_beta
    #   beta-benchmarker  → compute_historical_beta
    #   beta-challenger   → assess_beta_alignment
    # The visualizer agent doesn't need its own tool — it composes a
    # scatter spec from the quant + benchmarker outputs.
    #
    # Both compute_* tools are schema-tolerant: column names match
    # case-insensitively (and underscores / hyphens / spaces are
    # ignored), and variable_name values match against alias lists. This
    # lets the same tools work across "BHC long-format" outputs
    # (variable_name + additional_dimensions JSON), "metric-style" wide
    # outputs (Metric/Value pairs), and even when callers rename columns.
    # Both compute_* tools share a long-format schema:
    #   scenario, snap_date, variable_name, variable_value, segment, origin
    # `segment` carries the product label; `origin` distinguishes model
    # inputs (FF / macros) from model outputs (per-product rate paid).
    # Tools tolerate column-name case/underscore variants and alias values
    # for variable_name + origin.
    ctx.register_python_tool(
        name="compute_projected_beta",
        description=(
            "Compute each commercial-deposit segment's effective projected "
            "beta (Δrate_paid / Δfed_funds) over the CCAR horizon. Reads a "
            "single long-format projection CSV with columns scenario, "
            "snap_date, variable_name, variable_value, segment, origin. "
            "Tool reads model_output rows (variable_name='rate_paid') for "
            "the per-segment rate paths and model_input rows "
            "(variable_name='fed_funds_rate') for the macro Fed Funds path. "
            "Schema-tolerant: column names matched case-insensitively, "
            "alias values accepted for variable_name and origin."
        ),
        parameters=[
            {"name": "csv_path", "type": "string",
             "description": (
                 "Path to the long-format projection CSV. Defaults to "
                 "sample_data/ccar/commercial_deposit_output_CCAR26.csv."
             ),
             "required": False},
            {"name": "scenario", "type": "string",
             "description": (
                 "Scenario value to filter on (e.g. BHCS). Matched "
                 "case-insensitively. Defaults to the first scenario "
                 "found in the file."
             ),
             "required": False},
            {"name": "rate_var_aliases", "type": "array",
             "description": (
                 "Extra aliases for the rate-paid variable_name. "
                 "Defaults already cover rate_paid / rate_paid_pct / "
                 "interest_apy / interest_apr / interest_rate / rate."
             ),
             "required": False},
            {"name": "ff_var_aliases", "type": "array",
             "description": (
                 "Extra aliases for the fed-funds variable_name. "
                 "Defaults cover fed_funds_rate / fed_funds / "
                 "fed_funds_pct / FEDFUNDS / ff_rate / ffr."
             ),
             "required": False},
        ],
        python_source='''def compute_projected_beta(csv_path=None, scenario=None,
                            rate_var_aliases=None, ff_var_aliases=None):
    """Per-segment Δrate_paid / Δfed_funds over the projection horizon.
    Reads a single long-format CSV; uses `origin` to disambiguate model
    inputs (FF) from model outputs (rate_paid). Tolerates case +
    underscore variants in column + value names."""
    import os
    import pandas as pd

    def _find_repo_root():
        here = os.path.abspath(os.getcwd())
        for _ in range(6):
            if os.path.isdir(os.path.join(here, "sample_data")):
                return here
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
        return os.path.abspath(os.getcwd())

    def _norm(s):
        return str(s).lower().replace("_", "").replace("-", "").replace(" ", "")

    def _ci_pick(df, *candidates):
        by_norm = {_norm(c): c for c in df.columns}
        for cand in candidates:
            f = by_norm.get(_norm(cand))
            if f is not None:
                return f
        return None

    def _ci_match_values(series, *candidates):
        norm_cands = {_norm(c) for c in candidates}
        return [v for v in series.dropna().unique() if _norm(v) in norm_cands]

    repo = _find_repo_root()
    if not csv_path:
        csv_path = os.path.join(repo, "sample_data", "ccar", "commercial_deposit_output_CCAR26.csv")
    if not os.path.exists(csv_path):
        return {"error": f"csv not found: {csv_path}"}
    df = pd.read_csv(csv_path)

    var_col  = _ci_pick(df, "variable_name", "variableName", "metric")
    val_col  = _ci_pick(df, "variable_value", "value")
    date_col = _ci_pick(df, "snap_date", "snapDate", "date", "quarter_id", "quarterID", "period")
    seg_col  = _ci_pick(df, "segment", "product_name", "productName", "product", "product_l1")
    scen_col = _ci_pick(df, "scenario", "scenarioName", "scenario_id")
    org_col  = _ci_pick(df, "origin", "source", "source_kind", "io")

    if var_col is None or val_col is None or date_col is None or seg_col is None:
        return {"error": f"csv missing required columns; have: {list(df.columns)}; need variable_name + variable_value + snap_date + segment (any case)"}

    if scenario and scen_col is not None:
        df = df[df[scen_col].astype(str).str.lower() == str(scenario).lower()]
    elif scen_col is not None and len(df):
        scenario = str(df[scen_col].dropna().iloc[0])
        df = df[df[scen_col].astype(str) == scenario]
    if not len(df):
        return {"error": f"no rows for scenario {scenario}"}

    INPUT_ALIASES  = ["model_input", "input", "macro_input", "macro", "predictor", "feature"]
    OUTPUT_ALIASES = ["model_output", "output", "predicted", "target", "modeled"]
    rate_aliases = list(rate_var_aliases or []) + ["rate_paid", "rate_paid_pct", "interest_apy", "interest_apr", "interest_rate", "rate", "rate_paid_apr"]
    ff_aliases   = list(ff_var_aliases or [])   + ["fed_funds_rate", "fed_funds", "fedfunds", "ff_rate", "ffr", "fed_funds_pct"]

    rate_matches = _ci_match_values(df[var_col], *rate_aliases)
    ff_matches   = _ci_match_values(df[var_col], *ff_aliases)
    if not rate_matches:
        return {"error": f"no rate-paid variable in csv. variable_name values seen: {sorted(map(str, df[var_col].dropna().unique()))[:20]}; tried aliases: {rate_aliases}"}
    if not ff_matches:
        return {"error": f"no fed_funds_rate variable in csv. variable_name values seen: {sorted(map(str, df[var_col].dropna().unique()))[:20]}; tried aliases: {ff_aliases}"}

    # Per-segment rate paths (filter to model_output rows when origin
    # column is present; otherwise just take all rate_paid rows). Drop
    # missing values so partial / sparse data doesn't poison the endpoints.
    rate_df = df[df[var_col].isin(rate_matches)].copy()
    if org_col is not None:
        out_matches = _ci_match_values(rate_df[org_col], *OUTPUT_ALIASES)
        if out_matches:
            rate_df = rate_df[rate_df[org_col].isin(out_matches)]
    rate_df = rate_df.dropna(subset=[seg_col])
    rate_df = rate_df[rate_df[seg_col].astype(str).str.strip() != ""]
    rate_df[val_col] = pd.to_numeric(rate_df[val_col], errors="coerce")
    rate_df = rate_df.dropna(subset=[val_col])
    if not len(rate_df):
        return {"error": "no per-segment rate rows found after dropping NaN / applying origin filters"}

    # Fed Funds path — use origin=model_input rows when origin exists,
    # otherwise just match by variable_name. Coerce values numeric and
    # drop rows that didn't parse so a stray text value can't break the path.
    ff_df = df[df[var_col].isin(ff_matches)].copy()
    if org_col is not None:
        in_matches = _ci_match_values(ff_df[org_col], *INPUT_ALIASES)
        if in_matches:
            ff_df = ff_df[ff_df[org_col].isin(in_matches)]
    ff_df[val_col] = pd.to_numeric(ff_df[val_col], errors="coerce")
    ff_df = ff_df.dropna(subset=[val_col])
    ff_path = ff_df.groupby(date_col)[val_col].mean().dropna().sort_index()
    if len(ff_path) < 2:
        return {"error": "need at least two non-null fed_funds_rate observations across snap_date"}
    ff_change = float(ff_path.iloc[-1] - ff_path.iloc[0])
    if abs(ff_change) < 1e-6:
        return {"error": "fed_funds_rate is flat across the horizon — beta undefined"}

    skipped = []
    out_rows = []
    for segment, sub in rate_df.groupby(seg_col):
        rp = sub.groupby(date_col)[val_col].mean().dropna().sort_index()
        if len(rp) < 2:
            skipped.append({"product": str(segment), "reason": "fewer than 2 non-null rate observations", "obs_count": int(len(rp))})
            continue
        # Compute Δrate / ΔFF over the segment's OWN observation window —
        # if rate_paid is missing at the global PQ0/PQ8 the right comparison
        # is to ΔFF over the same dates the segment was observed, not the
        # global horizon (otherwise sparse data inflates / deflates beta).
        seg_start_date, seg_end_date = rp.index[0], rp.index[-1]
        ff_start_seg = ff_path.get(seg_start_date)
        ff_end_seg   = ff_path.get(seg_end_date)
        if ff_start_seg is None or ff_end_seg is None or pd.isna(ff_start_seg) or pd.isna(ff_end_seg):
            skipped.append({"product": str(segment), "reason": "fed_funds missing at segment endpoint dates", "obs_count": int(len(rp))})
            continue
        seg_ff_change = float(ff_end_seg - ff_start_seg)
        if abs(seg_ff_change) < 1e-6:
            skipped.append({"product": str(segment), "reason": "fed_funds is flat across segment window", "obs_count": int(len(rp))})
            continue
        rate_change = float(rp.iloc[-1] - rp.iloc[0])
        beta = rate_change / seg_ff_change
        out_rows.append({
            "product":         str(segment),
            "projected_beta":  round(beta, 3),
            "rate_change_pp": round(rate_change, 3),
            "ff_change_pp":   round(seg_ff_change, 3),
            "rate_start":     round(float(rp.iloc[0]), 3),
            "rate_end":       round(float(rp.iloc[-1]), 3),
            "window_start":   str(seg_start_date),
            "window_end":     str(seg_end_date),
            "obs_count":      int(len(rp)),
        })
    out_rows.sort(key=lambda r: -r["projected_beta"])

    return {
        "scenario":        str(scenario) if scenario is not None else None,
        "ff_start":        round(float(ff_path.iloc[0]), 3),
        "ff_end":          round(float(ff_path.iloc[-1]), 3),
        "ff_change_pp":    round(ff_change, 3),
        "horizon_periods": sorted(map(str, ff_path.index.tolist())),
        "by_product":      out_rows,
        "skipped":         skipped,
        "rate_var_matched":  rate_matches,
        "ff_var_matched":    ff_matches,
        "csv_path_used":   csv_path,
    }
''',
    )

    ctx.register_python_tool(
        name="compute_historical_beta",
        description=(
            "Compute each segment's effective historical beta (OLS slope "
            "of rate_paid vs Fed Funds) from a long-format actuals CSV. "
            "Schema mirrors the projection file: scenario, snap_date, "
            "variable_name, variable_value, segment, origin. The tool "
            "joins each segment's per-snap_date rate_paid against the "
            "macro fed_funds_rate path, then regresses. Schema-tolerant: "
            "column names matched case-insensitively, alias values for "
            "variable_name and origin."
        ),
        parameters=[
            {"name": "csv_path", "type": "string",
             "description": (
                 "Path to the long-format actuals CSV. Defaults to "
                 "sample_data/ccar/commercial_deposit_rate_actuals.csv."
             ),
             "required": False},
            {"name": "lookback_start", "type": "string",
             "description": (
                 "ISO date — earliest observation to include. Defaults "
                 "to the earliest row in the file. Use this to limit "
                 "the regression to the latest tightening cycle."
             ),
             "required": False},
            {"name": "rate_var_aliases", "type": "array",
             "description": (
                 "Extra aliases for the rate-paid variable_name. "
                 "Defaults already cover rate_paid / rate_paid_pct / "
                 "interest_apy / interest_apr / interest_rate / rate."
             ),
             "required": False},
            {"name": "ff_var_aliases", "type": "array",
             "description": (
                 "Extra aliases for the fed-funds variable_name. "
                 "Defaults cover fed_funds_rate / fed_funds / "
                 "fed_funds_pct / FEDFUNDS / ff_rate / ffr."
             ),
             "required": False},
            {"name": "products_only", "type": "array",
             "description": (
                 "If supplied, restricts the regression to ONLY these "
                 "segment values (case-insensitive). Useful when the "
                 "file mixes segments from multiple lines of business."
             ),
             "required": False},
        ],
        python_source='''def compute_historical_beta(csv_path=None, lookback_start=None,
                              rate_var_aliases=None, ff_var_aliases=None,
                              products_only=None):
    """OLS slope of rate_paid on fed_funds per segment, from the long-format
    actuals CSV. Joins per-segment rate paths against the macro fed_funds
    path on snap_date, then regresses Δy on Δx."""
    import os
    import pandas as pd

    def _find_repo_root():
        here = os.path.abspath(os.getcwd())
        for _ in range(6):
            if os.path.isdir(os.path.join(here, "sample_data")):
                return here
            parent = os.path.dirname(here)
            if parent == here:
                break
            here = parent
        return os.path.abspath(os.getcwd())

    def _norm(s):
        return str(s).lower().replace("_", "").replace("-", "").replace(" ", "")

    def _ci_pick(df, *candidates):
        by_norm = {_norm(c): c for c in df.columns}
        for cand in candidates:
            f = by_norm.get(_norm(cand))
            if f is not None:
                return f
        return None

    def _ci_match_values(series, *candidates):
        norm_cands = {_norm(c) for c in candidates}
        return [v for v in series.dropna().unique() if _norm(v) in norm_cands]

    repo = _find_repo_root()
    if not csv_path:
        # Try the new name first; fall back to the legacy "rate_history" name.
        for cand in ("commercial_deposit_rate_actuals.csv", "commercial_rate_history.csv"):
            p = os.path.join(repo, "sample_data", "ccar", cand)
            if os.path.exists(p):
                csv_path = p
                break
    if not csv_path or not os.path.exists(csv_path):
        return {"error": f"csv not found (tried sample_data/ccar/commercial_deposit_rate_actuals.csv)"}
    df = pd.read_csv(csv_path)

    var_col  = _ci_pick(df, "variable_name", "variableName", "metric")
    val_col  = _ci_pick(df, "variable_value", "value")
    date_col = _ci_pick(df, "snap_date", "snapDate", "date", "as_of_date", "observation_date")
    seg_col  = _ci_pick(df, "segment", "product_name", "productName", "product", "product_l1")
    org_col  = _ci_pick(df, "origin", "source", "source_kind", "io")

    if var_col is None or val_col is None or date_col is None or seg_col is None:
        return {"error": f"csv missing required columns; have: {list(df.columns)}; need variable_name + variable_value + snap_date + segment (any case)"}

    INPUT_ALIASES  = ["model_input", "input", "macro_input", "macro", "predictor", "feature"]
    OUTPUT_ALIASES = ["model_output", "output", "predicted", "target", "modeled"]
    rate_aliases = list(rate_var_aliases or []) + ["rate_paid", "rate_paid_pct", "interest_apy", "interest_apr", "interest_rate", "rate", "rate_paid_apr"]
    ff_aliases   = list(ff_var_aliases or [])   + ["fed_funds_rate", "fed_funds", "fedfunds", "ff_rate", "ffr", "fed_funds_pct"]

    rate_matches = _ci_match_values(df[var_col], *rate_aliases)
    ff_matches   = _ci_match_values(df[var_col], *ff_aliases)
    if not rate_matches:
        return {"error": f"no rate-paid variable. variable_name values seen: {sorted(map(str, df[var_col].dropna().unique()))[:20]}"}
    if not ff_matches:
        return {"error": f"no fed_funds_rate variable. variable_name values seen: {sorted(map(str, df[var_col].dropna().unique()))[:20]}"}

    df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
    df = df.dropna(subset=[date_col])
    if lookback_start:
        df = df[df[date_col] >= pd.to_datetime(lookback_start)]
    if not len(df):
        return {"error": "no rows after applying lookback filter"}

    # Per-segment rate path — coerce values numeric so any non-numeric text
    # becomes NaN, then drop NaN before grouping.
    rate_df = df[df[var_col].isin(rate_matches)].copy()
    if org_col is not None:
        m = _ci_match_values(rate_df[org_col], *OUTPUT_ALIASES)
        if m:
            rate_df = rate_df[rate_df[org_col].isin(m)]
    rate_df = rate_df.dropna(subset=[seg_col])
    rate_df = rate_df[rate_df[seg_col].astype(str).str.strip() != ""]
    rate_df[val_col] = pd.to_numeric(rate_df[val_col], errors="coerce")
    rate_df = rate_df.dropna(subset=[val_col])

    # Fed Funds path — same NaN treatment, then groupby+dropna so a missing
    # FF print at one snap_date doesn't drag down the regression.
    ff_df = df[df[var_col].isin(ff_matches)].copy()
    if org_col is not None:
        m = _ci_match_values(ff_df[org_col], *INPUT_ALIASES)
        if m:
            ff_df = ff_df[ff_df[org_col].isin(m)]
    ff_df[val_col] = pd.to_numeric(ff_df[val_col], errors="coerce")
    ff_df = ff_df.dropna(subset=[val_col])
    ff_series = (ff_df.groupby(date_col)[val_col].mean().dropna()
                       .rename("_ff").to_frame().reset_index())
    if len(ff_series) < 3:
        return {"error": "need at least 3 non-null fed_funds_rate observations to regress"}

    if products_only:
        wanted = {_norm(s) for s in products_only}
        rate_df = rate_df[rate_df[seg_col].apply(lambda s: _norm(s) in wanted)]

    out_rows = []
    skipped = []
    for segment, sub in rate_df.groupby(seg_col):
        seg_path = (sub.groupby(date_col)[val_col].mean().dropna()
                       .rename("_y").to_frame().reset_index())
        merged = (seg_path.merge(ff_series, on=date_col, how="inner")
                          .dropna(subset=["_y", "_ff"])
                          .sort_values(date_col))
        if len(merged) < 3:
            skipped.append({"product": str(segment), "reason": "fewer than 3 paired observations after NaN drop", "obs_count": int(len(merged))})
            continue
        x = merged["_ff"].astype(float).to_numpy()
        y = merged["_y"].astype(float).to_numpy()
        x_mean = float(x.mean()); y_mean = float(y.mean())
        x_var = float(((x - x_mean) ** 2).sum())
        if x_var < 1e-9:
            skipped.append({"product": str(segment), "reason": "fed_funds variance is zero over this window"})
            continue
        slope = float(((x - x_mean) * (y - y_mean)).sum() / x_var)
        intercept = float(y_mean - slope * x_mean)
        y_hat = intercept + slope * x
        ss_res = float(((y - y_hat) ** 2).sum())
        ss_tot = float(((y - y_mean) ** 2).sum())
        r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
        out_rows.append({
            "product":         str(segment),
            "historical_beta": round(slope, 3),
            "intercept":       round(intercept, 3),
            "r_squared":       round(r2, 3),
            "observations":    int(len(merged)),
            "date_start":      merged[date_col].min().strftime("%Y-%m-%d"),
            "date_end":        merged[date_col].max().strftime("%Y-%m-%d"),
        })
    out_rows.sort(key=lambda r: -r["historical_beta"])

    return {
        "by_product":       out_rows,
        "skipped":          skipped,
        "date_col_used":    date_col,
        "ff_var_matched":   ff_matches,
        "rate_var_matched": rate_matches,
        "lookback_start":   lookback_start,
        "csv_path_used":    csv_path,
    }
''',
    )

    ctx.register_python_tool(
        name="assess_beta_alignment",
        description=(
            "Compare per-product projected betas against historical betas "
            "and flag deviations against the documented 'fixed pricing "
            "percentile' assumption. Returns one row per product with "
            "alignment status and the gap in beta units."
        ),
        parameters=[
            {"name": "projected", "type": "object",
             "description": (
                 "Output of compute_projected_beta — must contain "
                 "by_product = [{product, projected_beta, ...}]."
             ),
             "required": True},
            {"name": "historical", "type": "object",
             "description": (
                 "Output of compute_historical_beta — must contain "
                 "by_product = [{product, historical_beta, r_squared, ...}]."
             ),
             "required": True},
            {"name": "tolerance", "type": "number",
             "description": (
                 "Absolute beta tolerance for ALIGNED status. Default "
                 "0.10 — projected within ±0.10 of historical is considered "
                 "consistent with the 60th-percentile peer pricing assumption."
             ),
             "required": False},
            {"name": "fixed_pricing_percentile", "type": "number",
             "description": (
                 "Documented peer-pricing percentile used when the "
                 "CommMaaS model was built (default 60). Carried through "
                 "for the challenger's narrative."
             ),
             "required": False},
        ],
        python_source=(
            'def assess_beta_alignment(projected, historical, tolerance=0.10, fixed_pricing_percentile=60):\n'
            '    """Per-product alignment check between projected and historical\n'
            '    betas. Status:\n'
            '        ALIGNED      — |proj - hist| <= tolerance\n'
            '        OVERSHOOT    — proj > hist + tolerance (model too aggressive)\n'
            '        UNDERSHOOT   — proj < hist - tolerance (model too conservative)\n'
            '    """\n'
            '    proj_rows = (projected or {}).get("by_product") or []\n'
            '    hist_rows = (historical or {}).get("by_product") or []\n'
            '    proj_idx = {r["product"]: r for r in proj_rows}\n'
            '    hist_idx = {r["product"]: r for r in hist_rows}\n'
            '    products = sorted(set(proj_idx) | set(hist_idx))\n'
            '\n'
            '    rows = []\n'
            '    for p in products:\n'
            '        pr = proj_idx.get(p, {})\n'
            '        hr = hist_idx.get(p, {})\n'
            '        proj_b = pr.get("projected_beta")\n'
            '        hist_b = hr.get("historical_beta")\n'
            '        if proj_b is None or hist_b is None:\n'
            '            rows.append({\n'
            '                "product": p,\n'
            '                "projected_beta":  proj_b,\n'
            '                "historical_beta": hist_b,\n'
            '                "gap":             None,\n'
            '                "status":          "MISSING",\n'
            '                "note":            "projected or historical beta unavailable",\n'
            '            })\n'
            '            continue\n'
            '        gap = round(float(proj_b) - float(hist_b), 3)\n'
            '        if abs(gap) <= float(tolerance):\n'
            '            status = "ALIGNED"\n'
            '            note = f"Within ±{tolerance:.2f} of historical — consistent with P{fixed_pricing_percentile} peer pricing assumption."\n'
            '        elif gap > 0:\n'
            '            status = "OVERSHOOT"\n'
            '            note = f"Projection {gap:+.2f} above historical — implies repricing more aggressive than P{fixed_pricing_percentile} peer percentile."\n'
            '        else:\n'
            '            status = "UNDERSHOOT"\n'
            '            note = f"Projection {gap:+.2f} below historical — implies repricing more conservative than P{fixed_pricing_percentile} peer percentile."\n'
            '        rows.append({\n'
            '            "product":          p,\n'
            '            "projected_beta":   round(float(proj_b), 3),\n'
            '            "historical_beta":  round(float(hist_b), 3),\n'
            '            "r_squared":        hr.get("r_squared"),\n'
            '            "gap":              gap,\n'
            '            "status":           status,\n'
            '            "note":             note,\n'
            '        })\n'
            '\n'
            '    aligned = sum(1 for r in rows if r["status"] == "ALIGNED")\n'
            '    over    = sum(1 for r in rows if r["status"] == "OVERSHOOT")\n'
            '    under   = sum(1 for r in rows if r["status"] == "UNDERSHOOT")\n'
            '    overall = "PASS" if (over + under) == 0 else "REVIEW"\n'
            '\n'
            '    return {\n'
            '        "fixed_pricing_percentile": fixed_pricing_percentile,\n'
            '        "tolerance":                float(tolerance),\n'
            '        "aligned_count":            aligned,\n'
            '        "overshoot_count":          over,\n'
            '        "undershoot_count":         under,\n'
            '        "overall_status":           overall,\n'
            '        "by_product":               rows,\n'
            '    }\n'
        ),
    )

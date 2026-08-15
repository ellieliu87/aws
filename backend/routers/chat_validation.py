"""Workflow validation logic — shared by chat tools, the validation endpoint,
and the chat router itself. Lives outside chat.py to avoid circular imports.

Each issue carries:
  severity: 'error' | 'warning' | 'info'
  message:  human-readable explanation
  hint:     one-line, actionable suggestion ("Connect …", "Set the
            target on …")
  code:     machine tag (FEATURE_MISMATCH, NO_INPUT, …) the UI uses to
            render specific CTAs
  node_id:  optional canvas node the issue points at
"""
from __future__ import annotations

from typing import Any

from routers.datasets import load_dataset
from routers.models_registry import load_model
from routers.scenarios import load_scenario
from routers.transforms import _TRANSFORMS


def _label_for_node(n: dict[str, Any]) -> str:
    """Best-effort human label for a node, used inside hint strings."""
    kind = n.get("kind")
    ref_id = n.get("ref_id") or ""
    if kind == "model":
        m = load_model(ref_id)
        return m.name if m else ref_id or "this model"
    if kind == "dataset":
        d = load_dataset(ref_id)
        return d.name if d else ref_id or "this dataset"
    if kind == "scenario":
        s = load_scenario(ref_id)
        return s.name if s else ref_id or "this scenario"
    if kind == "transform":
        t = _TRANSFORMS.get(ref_id)
        return t.name if t else ref_id or "this transform"
    if kind == "destination":
        return f"{ref_id} destination"
    return ref_id or "this node"


def _model_expected_features(m) -> list[str]:
    """Best-guess at the columns a model wants. Pulls from (in order):
    feature_mapping keys, feature_columns, coefficients keys."""
    if getattr(m, "feature_mapping", None):
        return list(m.feature_mapping.keys())
    if getattr(m, "feature_columns", None):
        return list(m.feature_columns)
    if getattr(m, "coefficients", None):
        return list(m.coefficients.keys())
    return []


def _dataset_columns(d) -> set[str]:
    """Lower-cased column names available on a dataset. Reads the on-disk
    file when present so we see post-load columns (the in-memory `Dataset`
    schema can lag if the file was overwritten)."""
    cols: set[str] = {c.name.lower() for c in (d.columns or [])}
    try:
        from pathlib import Path

        from routers.datasets import _read_dataframe, _resolve_path
        if d.file_path and d.file_format:
            p: Path = _resolve_path(d)
            if p.exists():
                df = _read_dataframe(p, d.file_format)
                cols |= {str(c).lower() for c in df.columns}
    except Exception:
        # Best-effort — fall back to the schema snapshot.
        pass
    return cols


def validate_workflow_payload(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a list of issue dicts {severity, message, hint, code, node_id?}.

    Opens its own registry memo. The HTTP middleware already provides one for
    the `/workflow-validate` endpoint, but this is also reached from the
    `validate_workflow` agent tool mid-stream, where the middleware's memo has
    already closed. `request_cache` joins an active memo rather than nesting,
    so the endpoint path is unaffected.
    """
    from services.entity_store import request_cache

    with request_cache():
        return _validate_workflow_payload(nodes, edges)


def _validate_workflow_payload(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    # ── 0. Empty workflow ──────────────────────────────────────────────────
    if not nodes:
        issues.append({
            "severity": "error",
            "code": "WORKFLOW_EMPTY",
            "message": "Workflow is empty.",
            "hint": "Drag a dataset and a model onto the canvas to get started.",
        })
        return issues

    by_id = {n["id"]: n for n in nodes}
    incoming: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    outgoing: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e.get("source") in by_id and e.get("target") in by_id:
            incoming[e["target"]].append(e["source"])
            outgoing[e["source"]].append(e["target"])

    # ── 1. Cycles ──────────────────────────────────────────────────────────
    def has_cycle() -> bool:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n["id"]: WHITE for n in nodes}

        def visit(v: str) -> bool:
            if color[v] == GRAY:
                return True
            if color[v] == BLACK:
                return False
            color[v] = GRAY
            for c in outgoing.get(v, []):
                if visit(c):
                    return True
            color[v] = BLACK
            return False
        return any(visit(n["id"]) for n in nodes if color[n["id"]] == WHITE)

    if has_cycle():
        issues.append({
            "severity": "error",
            "code": "CYCLE",
            "message": "Workflow contains a cycle.",
            "hint": "Models can't loop back into themselves. Remove the edge that closes the loop.",
        })

    # ── 2. Coverage — must have at least one model ─────────────────────────
    if not any(n.get("kind") == "model" for n in nodes):
        issues.append({
            "severity": "error",
            "code": "NO_MODEL",
            "message": "Workflow has no model.",
            "hint": "Drag a model from the palette onto the canvas. The workflow needs at least one model node to produce predictions.",
        })

    # ── 3. Per-node structural checks ──────────────────────────────────────
    for n in nodes:
        kind = n.get("kind")
        nid = n["id"]
        ref_id = n.get("ref_id") or ""

        if kind == "model":
            m = load_model(ref_id)
            if not m:
                issues.append({
                    "severity": "error",
                    "code": "MODEL_MISSING",
                    "message": "Model is no longer registered.",
                    "hint": "Re-pick a model from the palette — the one this node references was deleted or renamed.",
                    "node_id": nid,
                })
                continue
            if not incoming[nid]:
                issues.append({
                    "severity": "error",
                    "code": "MODEL_NO_INPUT",
                    "message": f"`{m.name}` has no input.",
                    "hint": "Connect a dataset, scenario, or upstream model to this node's left handle.",
                    "node_id": nid,
                })
            # output_kind sanity
            ok_kind = (m.output_kind or "scalar")
            if ok_kind == "n_step_forecast" and not (m.forecast_steps or 0):
                issues.append({
                    "severity": "warning",
                    "code": "OUTPUT_KIND_FORECAST_STEPS",
                    "message": f"`{m.name}` is configured as n-step forecast but no `forecast_steps` was set.",
                    "hint": "Open the Models tab, edit this model, and set the number of forecast steps.",
                    "node_id": nid,
                })
            if ok_kind == "multi_target" and not (m.target_names or []):
                issues.append({
                    "severity": "warning",
                    "code": "OUTPUT_KIND_TARGET_NAMES",
                    "message": f"`{m.name}` is multi-target but `target_names` is empty.",
                    "hint": "Open the Models tab and list the target column names so the workflow can label outputs.",
                    "node_id": nid,
                })
            if ok_kind == "probability_vector" and not (m.class_labels or []):
                issues.append({
                    "severity": "info",
                    "code": "OUTPUT_KIND_CLASS_LABELS",
                    "message": f"`{m.name}` returns probabilities but no class labels were declared.",
                    "hint": "Optional — labels make the output table easier to read. Set them in the Models tab.",
                    "node_id": nid,
                })

        elif kind == "dataset":
            d = load_dataset(ref_id)
            if not d:
                issues.append({
                    "severity": "error",
                    "code": "DATASET_MISSING",
                    "message": "Dataset is no longer registered.",
                    "hint": "Re-pick a dataset from the palette.",
                    "node_id": nid,
                })
            elif not outgoing[nid]:
                issues.append({
                    "severity": "info",
                    "code": "DATASET_DANGLING",
                    "message": f"`{d.name}` isn't wired to anything.",
                    "hint": "Drag an edge from this dataset to a model — otherwise it'll be ignored at run time.",
                    "node_id": nid,
                })

        elif kind == "transform":
            t = _TRANSFORMS.get(ref_id)
            if not t:
                issues.append({
                    "severity": "error",
                    "code": "TRANSFORM_MISSING",
                    "message": "Transform is no longer registered.",
                    "hint": "Re-pick a transform from the palette.",
                    "node_id": nid,
                })
            else:
                if not t.output_dataset_id:
                    issues.append({
                        "severity": "error",
                        "code": "TRANSFORM_NO_OUTPUT",
                        "message": f"`{t.name}` has no output dataset configured.",
                        "hint": "Open the Transform's side panel and set its output dataset.",
                        "node_id": nid,
                    })
                if not outgoing[nid]:
                    issues.append({
                        "severity": "info",
                        "code": "TRANSFORM_DANGLING",
                        "message": f"`{t.name}` isn't wired to anything.",
                        "hint": "Drag an edge from this transform to a model — otherwise it'll be ignored at run time.",
                        "node_id": nid,
                    })

        elif kind == "scenario":
            s = load_scenario(ref_id)
            if not s:
                issues.append({
                    "severity": "error",
                    "code": "SCENARIO_MISSING",
                    "message": "Scenario is no longer registered.",
                    "hint": "Re-pick a scenario from the palette.",
                    "node_id": nid,
                })
            elif not outgoing[nid]:
                issues.append({
                    "severity": "info",
                    "code": "SCENARIO_DANGLING",
                    "message": f"`{s.name}` isn't wired to anything.",
                    "hint": "Drag an edge from this scenario to a model.",
                    "node_id": nid,
                })

        elif kind == "destination":
            cfg = n.get("config", {}) or {}
            target = cfg.get("table") or cfg.get("bucket") or cfg.get("filename") or cfg.get("ref")
            if not incoming[nid]:
                issues.append({
                    "severity": "warning",
                    "code": "DESTINATION_NO_INPUT",
                    "message": f"`{ref_id or 'destination'}` has no upstream model.",
                    "hint": "Drag an edge from a model node to this destination — without one nothing gets written.",
                    "node_id": nid,
                })
            if not target:
                kind_word = {
                    "snowflake_table": "table name", "onelake_table": "table reference",
                    "s3": "bucket/key", "csv": "filename",
                }.get(ref_id, "target")
                issues.append({
                    "severity": "error",
                    "code": "DESTINATION_NO_TARGET",
                    "message": f"`{ref_id or 'destination'}` is missing a {kind_word}.",
                    "hint": f"Click the destination card on the canvas to set its {kind_word}.",
                    "node_id": nid,
                })

    # ── 4. Feature-presence check (model ↔ upstream dataset/scenario) ──────
    for n in nodes:
        if n.get("kind") != "model":
            continue
        m = load_model(n.get("ref_id", ""))
        if not m:
            continue
        expected = [str(f) for f in _model_expected_features(m)]
        if not expected:
            # Nothing declared and nothing introspectable from in-memory state;
            # the sandbox will try `feature_names_in_` at runtime — skip the
            # check rather than warning when we have no ground truth.
            continue
        expected_lower = {f.lower() for f in expected}
        for src_id in incoming[n["id"]]:
            src = by_id.get(src_id)
            if not src:
                continue
            available: set[str] = set()
            if src.get("kind") == "scenario":
                sc = load_scenario(src["ref_id"])
                if sc:
                    available |= {v.lower() for v in sc.variables}
            elif src.get("kind") == "dataset":
                d = load_dataset(src["ref_id"])
                if d:
                    available |= _dataset_columns(d)
            elif src.get("kind") == "transform":
                # Resolve through to the transform's output dataset so a
                # model fed by `Data Harness → Model` gets the same
                # column-presence check as `Dataset → Model`.
                t = _TRANSFORMS.get(src["ref_id"])
                if t and t.output_dataset_id:
                    d = load_dataset(t.output_dataset_id)
                    if d:
                        available |= _dataset_columns(d)
            elif src.get("kind") == "model":
                # Upstream model output rows include all input columns + the
                # prediction column — we don't statically know them, so skip.
                continue
            if not available:
                continue
            missing = sorted(expected_lower - available)
            if missing == sorted(expected_lower):
                issues.append({
                    "severity": "error",
                    "code": "FEATURE_MISMATCH",
                    "message": (
                        f"`{m.name}` expects {expected} — none are present in `"
                        f"{_label_for_node(src)}`."
                    ),
                    "hint": (
                        "Rename the dataset's columns to match the model's expected names, "
                        "or pick a different dataset. Tip: case doesn't matter for the match."
                    ),
                    "node_id": n["id"],
                })
            elif missing:
                issues.append({
                    "severity": "warning",
                    "code": "FEATURE_PARTIAL",
                    "message": (
                        f"`{m.name}` expects {expected} but `{_label_for_node(src)}` is "
                        f"missing {missing}."
                    ),
                    "hint": "Add the missing columns to the dataset, or rename existing ones to match.",
                    "node_id": n["id"],
                })

    return issues

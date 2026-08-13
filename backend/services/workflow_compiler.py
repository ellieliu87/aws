"""Compile a Workflow-tab canvas into an Amazon States Language definition.

The Workflow tab already builds a dependency graph: nodes (dataset, scenario,
model, transform, destination) and edges. `workflowSpec.ts:computeSteps`
topologically sorts it in the browser to render the Steps view, and the backend
stores nodes/edges as opaque JSON it never reads.

A Step Functions definition is the same shape — named steps plus what comes
next — so the graph does not need interpreting, it needs translating. This
module does that translation, which is what lets the picture an analyst drew
become the thing that actually executes.

Design notes
------------
One state machine per saved workflow, compiled when the analyst saves. The
alternative — a single generic machine that walks the DAG at runtime — would
just move the interpreter from Python into Amazon States Language, and you
would lose the thing that makes this worth doing: the execution graph in the
console IS the analyst's diagram, so a failed run points at the box that failed.

`compile_workflow` is deliberately pure: dicts in, dict out, no AWS calls, no
imports beyond the standard library. That makes the hard part (graph ordering,
name collisions, parallel levels) unit-testable offline, and leaves deployment
as a thin separate step.

Node kinds map as follows:
  dataset / scenario   inputs, not states — they are data the models read
  transform / model    Task states invoking the solver Lambda
  destination          collected into the final publish step

Models at the same dependency depth become a Parallel state, mirroring what
the canvas shows side by side.
"""
from __future__ import annotations

import re
from typing import Any

# ASL state names must be unique within a machine and at most 80 characters.
MAX_STATE_NAME = 80
INPUT_KINDS = {"dataset", "scenario"}
COMPUTE_KINDS = {"model", "transform"}


class WorkflowCompileError(ValueError):
    """The canvas graph cannot be expressed as a state machine."""


# ── graph helpers ─────────────────────────────────────────────────────────
def _node_id(node: dict) -> str:
    return str(node.get("id", ""))


def _node_data(node: dict) -> dict:
    # ReactFlow nests the interesting fields under `data`.
    return node.get("data") or {}


def _kind(node: dict) -> str:
    return str(_node_data(node).get("kind", ""))


def _title(node: dict) -> str:
    data = _node_data(node)
    return str(data.get("title") or data.get("ref_id") or _node_id(node))


def _levels(nodes: list[dict], edges: list[dict]) -> list[list[dict]]:
    """Group compute nodes into dependency levels (a topological layering).

    Mirrors workflowSpec.ts:computeSteps — only compute-to-compute edges add
    depth, because datasets and scenarios are level-zero data rather than work.
    Raises on a cycle instead of the frontend's graceful `return 0`, since a
    cyclic definition is not deployable and silence would be worse than an
    error the analyst can act on.
    """
    by_id = {_node_id(n): n for n in nodes if _node_id(n)}
    incoming: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for edge in edges:
        source, target = str(edge.get("source", "")), str(edge.get("target", ""))
        if source in by_id and target in by_id:
            incoming[target].append(source)

    depth: dict[str, int] = {}
    visiting: set[str] = set()

    def depth_of(node_id: str) -> int:
        if node_id in depth:
            return depth[node_id]
        if node_id in visiting:
            raise WorkflowCompileError(
                f"the graph has a cycle through {_title(by_id[node_id])!r}; "
                "a workflow must be acyclic to run as a state machine"
            )
        visiting.add(node_id)
        best = 0
        for source in incoming[node_id]:
            if _kind(by_id[source]) in COMPUTE_KINDS:
                best = max(best, depth_of(source) + 1)
            else:
                best = max(best, depth_of(source))
        visiting.discard(node_id)
        depth[node_id] = best
        return best

    compute = [n for n in nodes if _kind(n) in COMPUTE_KINDS]
    for node in compute:
        depth_of(_node_id(node))

    if not compute:
        raise WorkflowCompileError(
            "the canvas has no model or transform nodes, so there is nothing "
            "to execute"
        )

    grouped: dict[int, list[dict]] = {}
    for node in compute:
        grouped.setdefault(depth[_node_id(node)], []).append(node)
    # Stable ordering within a level keeps the compiled output deterministic,
    # which matters because a changed definition means a redeploy.
    return [
        sorted(grouped[level], key=lambda n: (_title(n), _node_id(n)))
        for level in sorted(grouped)
    ]


def _inputs_for(node: dict, nodes: list[dict], edges: list[dict]) -> list[str]:
    by_id = {_node_id(n): n for n in nodes}
    refs = []
    for edge in edges:
        if str(edge.get("target")) != _node_id(node):
            continue
        source = by_id.get(str(edge.get("source")))
        if source and _kind(source) in INPUT_KINDS:
            refs.append(str(_node_data(source).get("ref_id") or _node_id(source)))
    return refs


def _state_name(node: dict, used: set[str]) -> str:
    """A readable, unique, ASL-legal state name derived from the node title."""
    base = re.sub(r"[^A-Za-z0-9 _-]", "", _title(node)).strip() or "Step"
    base = re.sub(r"\s+", " ", base)[:MAX_STATE_NAME - 6]
    name = base
    suffix = 2
    while name in used:
        name = f"{base} {suffix}"
        suffix += 1
    used.add(name)
    return name


# ── compiler ──────────────────────────────────────────────────────────────
def compile_workflow(
    workflow: dict[str, Any],
    *,
    solver_arn: str,
    require_approval: bool = True,
    artifacts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Translate a SavedWorkflow into an Amazon States Language definition.

    `workflow` is the stored shape: nodes, edges, horizon_months, name.
    `solver_arn` is the Lambda that runs a compute node.
    `require_approval` inserts a waitForTaskToken gate before publishing,
    which is the champion/challenger control expressed as a state.

    `artifacts` maps a node's `ref_id` to a real model artifact:

        {"mdl-deposits-rdmaas": {"model_dir": "capital_planning",
                                 "artifact": "mdl-deposits-rdmaas.pkl",
                                 "feature_columns": [...]}}

    A node found in that map compiles to `mode: predict` and runs the actual
    pickle; a node absent from it falls back to `mode: solve` (goal-seek). Kept
    as a parameter rather than looked up here so the compiler stays pure —
    resolving ref_ids against the model registry is the caller's job, and the
    graph logic remains testable with no registry at all.
    """
    artifacts = artifacts or {}
    nodes = list(workflow.get("nodes") or [])
    edges = list(workflow.get("edges") or [])
    if not nodes:
        raise WorkflowCompileError("the canvas is empty")

    levels = _levels(nodes, edges)
    destinations = [
        str(_node_data(n).get("ref_id") or _node_id(n))
        for n in nodes if _kind(n) == "destination"
    ]

    states: dict[str, Any] = {}
    used_names: set[str] = set()
    failed_name = "WorkflowFailed"

    def compute_task(node: dict, result_path: str) -> dict:
        """One compute node as a Lambda-invoking Task state."""
        data = _node_data(node)
        ref_id = str(data.get("ref_id") or "")
        job_id = ("States.Format('{}-{}', $.run_id, "
                  f"'{_node_id(node)}')")

        artifact = artifacts.get(ref_id)
        if artifact:
            # A node bound to a real artifact runs the model itself. Input rows
            # come from the execution input rather than being baked into the
            # definition, so the same compiled plan can run against different
            # data — which is the whole point of a saved workflow.
            payload: dict[str, Any] = {
                "mode": "predict",
                "job_id.$": job_id,
                "node_id": _node_id(node),
                "model_ref": ref_id,
                "model_dir": artifact["model_dir"],
                "artifact": artifact["artifact"],
                "feature_columns": artifact.get("feature_columns") or [],
                "rows.$": "$.params.rows",
                "horizon_months": workflow.get("horizon_months", 12),
            }
        else:
            payload = {
                "mode": "solve",
                "job_id.$": job_id,
                "node_id": _node_id(node),
                "node_kind": _kind(node),
                "model_ref": ref_id,
                "inputs": _inputs_for(node, nodes, edges),
                "params": data.get("config") or {},
                "horizon_months": workflow.get("horizon_months", 12),
            }

        return {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {"FunctionName": solver_arn, "Payload": payload},
            # Keep the whole payload rather than reaching inside it. The two
            # modes return different shapes — goal-seek has `result`, predict
            # has `predictions` — and a selector naming an absent field is a
            # States.Runtime failure, so a working Lambda result gets thrown
            # away by the state machine. Selecting the envelope is agnostic to
            # what the worker chooses to return.
            "ResultSelector": {"output.$": "$.Payload"},
            "ResultPath": result_path,
            "Retry": [{
                "ErrorEquals": ["Lambda.ServiceException",
                                "Lambda.TooManyRequestsException",
                                "States.TaskFailed"],
                "IntervalSeconds": 2, "MaxAttempts": 3, "BackoffRate": 2.0,
            }],
            "Catch": [{
                "ErrorEquals": ["States.ALL"],
                "Next": failed_name,
                "ResultPath": "$.error",
            }],
        }

    # Each dependency level becomes one state: a Task when it holds a single
    # node, a Parallel when the canvas shows several side by side.
    level_names: list[str] = []
    for index, level in enumerate(levels):
        result_path = f"$.levels.level{index}"
        if len(level) == 1:
            name = _state_name(level[0], used_names)
            states[name] = compute_task(level[0], result_path)
        else:
            name = f"Level {index + 1} ({len(level)} in parallel)"
            used_names.add(name)
            branches = []
            for node in level:
                branch_name = _state_name(node, used_names)
                branches.append({
                    "StartAt": branch_name,
                    "States": {
                        # Inside a Parallel branch the Catch above would point
                        # at a state in the outer scope, which ASL forbids, so
                        # branch failures propagate to the Parallel's own Catch.
                        branch_name: {
                            **{k: v for k, v in compute_task(node, "$.result").items()
                               if k != "Catch"},
                            "End": True,
                        },
                    },
                })
            states[name] = {
                "Type": "Parallel",
                "Branches": branches,
                "ResultPath": result_path,
                "Catch": [{
                    "ErrorEquals": ["States.ALL"],
                    "Next": failed_name,
                    "ResultPath": "$.error",
                }],
            }
        level_names.append(name)

    # Chain the levels in dependency order.
    tail = level_names[-1]
    for current, following in zip(level_names, level_names[1:]):
        states[current]["Next"] = following

    if require_approval:
        gate = "ApprovalGate"
        states[gate] = {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
            "Parameters": {
                "FunctionName": solver_arn,
                "Payload": {
                    "mode": "request_approval",
                    "run_id.$": "$.run_id",
                    "phase": "champion_challenger",
                    "task_token.$": "$$.Task.Token",
                },
            },
            "ResultPath": "$.approval",
            "TimeoutSeconds": 86400,
            "Catch": [{
                "ErrorEquals": ["States.Timeout"],
                "Next": "ApprovalExpired",
                "ResultPath": "$.error",
            }],
        }
        states["ApprovalExpired"] = {
            "Type": "Fail", "Error": "ApprovalTimeout",
            "Cause": "No reviewer approved within 24 hours",
        }
        states[tail]["Next"] = gate
        tail = gate

    publish = "Publish"
    states[publish] = {
        "Type": "Task",
        "Resource": "arn:aws:states:::lambda:invoke",
        "Parameters": {
            "FunctionName": solver_arn,
            "Payload": {
                "mode": "publish",
                "run_id.$": "$.run_id",
                "approved_by.$": ("$.approval.approved_by" if require_approval
                                  else "States.Format('auto')"),
                "destinations": destinations,
            },
        },
        "ResultSelector": {"published.$": "$.Payload"},
        "ResultPath": "$.publish",
        "End": True,
    }
    states[tail]["Next"] = publish
    states[failed_name] = {
        "Type": "Fail", "Error": "WorkflowStepFailed",
        "Cause": "A compute step failed after retries",
    }

    return {
        "Comment": f"Compiled from Workflow canvas: {workflow.get('name', 'untitled')}",
        "StartAt": level_names[0],
        "States": states,
    }


def _modes(definition: dict[str, Any]) -> dict[str, int]:
    """Count how many compute states run a real model vs goal-seek."""
    counts: dict[str, int] = {}

    def walk(states: dict) -> None:
        for state in states.values():
            payload = (state.get("Parameters") or {}).get("Payload") or {}
            mode = payload.get("mode")
            if mode:
                counts[mode] = counts.get(mode, 0) + 1
            for branch in state.get("Branches") or []:
                walk(branch["States"])

    walk(definition["States"])
    return counts


def summarize(definition: dict[str, Any]) -> dict[str, Any]:
    """A human-readable description of what the compiler produced."""
    states = definition["States"]
    return {
        "start_at": definition["StartAt"],
        "states": len(states),
        "tasks": sum(1 for s in states.values() if s["Type"] == "Task"),
        "parallel": sum(1 for s in states.values() if s["Type"] == "Parallel"),
        "fail_states": sum(1 for s in states.values() if s["Type"] == "Fail"),
        "modes": _modes(definition),
        "order": _walk_order(definition),
    }


def _walk_order(definition: dict[str, Any]) -> list[str]:
    """Follow Next pointers from StartAt to the end, for display."""
    states, order = definition["States"], []
    current = definition["StartAt"]
    while current and current not in order:
        order.append(current)
        current = states.get(current, {}).get("Next")
    return order

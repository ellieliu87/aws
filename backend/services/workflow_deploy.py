"""Deploy a compiled canvas workflow as a Step Functions state machine.

Split from workflow_compiler.py on purpose: compiling is a pure function worth
unit-testing offline, deploying touches AWS and needs credentials. Keeping them
apart means the interesting logic is testable without a network.

The lifecycle mirrors the analyst's: saving a workflow compiles and deploys it,
so the Step Functions console shows the graph they drew. Because each workflow
gets its own machine, a failed execution highlights the specific box that
failed rather than a generic "phase 3 error".

Validation without deployment is available via `validate` — Step Functions will
check a definition and report problems without creating anything, which makes
it safe to compile-and-check in CI.
"""
from __future__ import annotations

import json
import logging
import os
import re

from services.workflow_compiler import compile_workflow

log = logging.getLogger("cma.workflow_deploy")

NAME_PREFIX = "cma-canvas-"


def region() -> str:
    from cof.llm_config import bedrock_region

    return os.getenv("CMA_JOBS_REGION", "").strip() or bedrock_region()


# Kept for internal callers written before `region()` was public.
_region = region


def _client(service: str = "stepfunctions"):
    import boto3

    return boto3.client(service, region_name=_region())


def solver_arn() -> str:
    """The compute Lambda the compiled Tasks invoke.

    Read from the environment so the compiler stays free of AWS lookups; set by
    infra/sync_env.py from the CDK stack outputs.
    """
    arn = os.getenv("CMA_SOLVER_FUNCTION_ARN", "").strip()
    if arn:
        return arn
    name = os.getenv("CMA_SOLVER_FUNCTION_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "set CMA_SOLVER_FUNCTION_ARN (or CMA_SOLVER_FUNCTION_NAME) — run "
            "`python -m infra.sync_env --write` after a cdk deploy"
        )
    return _client("lambda").get_function(FunctionName=name)["Configuration"]["FunctionArn"]


def machine_name(workflow: dict) -> str:
    """A stable, legal state-machine name derived from the workflow id."""
    slug = re.sub(r"[^A-Za-z0-9-_]", "-", str(workflow.get("id") or workflow.get("name", "")))
    return f"{NAME_PREFIX}{slug}"[:80]


def validate(workflow: dict, *, require_approval: bool = True) -> dict:
    """Compile and ask Step Functions to check it — creates nothing."""
    definition = compile_workflow(
        workflow, solver_arn=solver_arn(), require_approval=require_approval
    )
    response = _client().validate_state_machine_definition(
        definition=json.dumps(definition)
    )
    return {
        "result": response.get("result"),
        "diagnostics": response.get("diagnostics", []),
        "definition": definition,
    }


def deploy(workflow: dict, *, role_arn: str | None = None,
           require_approval: bool = True) -> dict:
    """Compile this workflow and create or update its state machine."""
    return deploy_definition(
        workflow,
        compile_workflow(workflow, solver_arn=solver_arn(),
                         require_approval=require_approval),
        role_arn=role_arn,
    )


def deploy_definition(workflow: dict, definition_dict: dict, *,
                      role_arn: str | None = None) -> dict:
    """Create or update a machine from an already-compiled definition.

    Separate from `deploy` because the registry stores compiled plans: the
    deployer should ship the exact definition that was recorded and versioned,
    not recompile from source and hope it matches.
    """
    role_arn = role_arn or os.getenv("CMA_PLAYBOOK_ROLE_ARN", "").strip()
    if not role_arn:
        raise RuntimeError(
            "set CMA_PLAYBOOK_ROLE_ARN — the role Step Functions assumes to "
            "invoke the solver Lambda (see the CDK stack's Playbook role)"
        )

    definition = json.dumps(definition_dict)
    sfn, name = _client(), machine_name(workflow)
    account = _client("sts").get_caller_identity()["Account"]
    arn = f"arn:aws:states:{_region()}:{account}:stateMachine:{name}"

    try:
        sfn.update_state_machine(stateMachineArn=arn, definition=definition,
                                 roleArn=role_arn)
        action = "updated"
    except sfn.exceptions.StateMachineDoesNotExist:
        arn = sfn.create_state_machine(
            name=name, definition=definition, roleArn=role_arn, type="STANDARD",
        )["stateMachineArn"]
        action = "created"

    log.info("%s state machine %s", action, name)
    return {"action": action, "name": name, "state_machine_arn": arn}


def delete(workflow: dict) -> dict:
    """Remove the machine when its workflow is deleted."""
    account = _client("sts").get_caller_identity()["Account"]
    arn = (f"arn:aws:states:{_region()}:{account}:stateMachine:"
           f"{machine_name(workflow)}")
    try:
        _client().delete_state_machine(stateMachineArn=arn)
        return {"deleted": arn}
    except Exception as e:
        return {"error": str(e)}


def start(workflow: dict, run_id: str, params: dict | None = None) -> str:
    """Start an execution of this workflow's machine."""
    import uuid

    account = _client("sts").get_caller_identity()["Account"]
    arn = (f"arn:aws:states:{_region()}:{account}:stateMachine:"
           f"{machine_name(workflow)}")
    return _client().start_execution(
        stateMachineArn=arn,
        name=f"{run_id}-{uuid.uuid4().hex[:8]}",
        input=json.dumps({"run_id": run_id, "params": params or {}}),
    )["executionArn"]

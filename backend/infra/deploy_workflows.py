"""Deploy pending workflow plans. The only thing allowed to create machines.

This exists so the web tier does not need permission to create AWS resources.
The API compiles a canvas and records a plan marked `pending_deploy`; this
process — run from CI or an operator's shell with a role that CAN create state
machines — picks those up and deploys them. A bug or an injection in the
request path therefore cannot bring infrastructure into existence.

It also owns cleanup, which is the other half of the same argument. Machines
created at run time are not in the CDK stack, so nothing else knows they exist:

    apply     deploy pending plans, delete requested workflows
    sweep     find cma-canvas-* machines with no registered workflow
    status    what is pending

`sweep` is the backstop for the case that actually happens: a workflow deleted
while this process was not running, leaving a machine nobody remembers. It
reports by default and only deletes with --delete, because a sweep that removes
things unprompted is how you lose something you needed.

Usage (from backend/):
    python -m infra.deploy_workflows status
    python -m infra.deploy_workflows apply
    python -m infra.deploy_workflows sweep [--delete]
"""
from __future__ import annotations

import json
import sys

from services import workflow_deploy, workflow_plans
from services.workflow_compiler import compile_workflow

MANAGED_PREFIX = workflow_deploy.NAME_PREFIX  # cma-canvas-


def _plan_to_workflow_stub(plan: dict) -> dict:
    """The minimum workflow_deploy.machine_name needs to name the machine."""
    return {"id": plan["workflow_id"], "name": plan.get("workflow_name") or ""}


def apply() -> dict:
    """Deploy every pending plan; delete every requested workflow."""
    work = workflow_plans.pending()
    if not work:
        return {"pending": 0, "deployed": [], "deleted": []}

    solver = workflow_deploy.solver_arn()
    deployed: list[dict] = []
    deleted: list[str] = []
    superseded: list[dict] = []

    # Several edits between two runs of this process leave several pending
    # versions for one workflow. Only the newest is worth shipping — replaying
    # the history would update the same machine repeatedly to reach the same
    # end state. The skipped ones are marked superseded rather than deployed,
    # because they genuinely never ran.
    latest_pending: dict[str, int] = {}
    for item in work:
        if item["action"] == "deploy":
            workflow_id = item["workflow_id"]
            latest_pending[workflow_id] = max(
                latest_pending.get(workflow_id, 0), item["version"]
            )

    for item in work:
        workflow_id = item["workflow_id"]

        if (item["action"] == "deploy"
                and item["version"] != latest_pending[workflow_id]):
            workflow_plans.mark_superseded(workflow_id, item["version"])
            superseded.append({"workflow_id": workflow_id,
                               "version": item["version"]})
            print(f"  superseded {workflow_id} v{item['version']}")
            continue

        if item["action"] == "delete":
            result = workflow_deploy.delete({"id": workflow_id, "name": ""})
            workflow_plans.mark_deleted(workflow_id)
            deleted.append(workflow_id)
            print(f"  deleted   {workflow_id}  {result}")
            continue

        version = item["version"]
        plan = workflow_plans.get_plan(workflow_id, version)
        if not plan:
            print(f"  MISSING   {workflow_id} v{version}")
            continue

        # The stored plan carries a placeholder where the solver ARN goes, so a
        # plan is portable across accounts and the ARN is not part of its
        # identity. Substituting here is the deployment step's job.
        definition = json.loads(
            json.dumps(plan["definition"]).replace(
                workflow_plans.SOLVER_PLACEHOLDER, solver
            )
        )
        outcome = workflow_deploy.deploy_definition(
            _plan_to_workflow_stub(plan), definition
        )
        workflow_plans.mark_deployed(workflow_id, version,
                                    outcome["state_machine_arn"])
        deployed.append({"workflow_id": workflow_id, "version": version,
                         **outcome})
        print(f"  {outcome['action']:9} {workflow_id} v{version} -> {outcome['name']}")

    return {"pending": len(work), "deployed": deployed, "deleted": deleted,
            "superseded": superseded}


def sweep(delete: bool = False) -> dict:
    """Find managed machines with no registered workflow behind them."""
    import boto3

    sfn = boto3.client("stepfunctions", region_name=workflow_deploy.region())

    # A machine is legitimate only if its workflow still exists. Plan history
    # is kept forever for audit, so "has plans" is not the test — a workflow
    # that was deleted should have no machine, and one that survived is exactly
    # the leftover this sweep is for.
    known = set()
    for workflow_id in workflow_plans.list_workflow_ids():
        index = workflow_plans.get_index(workflow_id) or {}
        if index.get("deleted_at"):
            continue
        known.add(workflow_deploy.machine_name({"id": workflow_id, "name": ""}))

    orphans: list[dict] = []
    deleting = 0
    paginator = sfn.get_paginator("list_state_machines")
    for page in paginator.paginate():
        for machine in page["stateMachines"]:
            name = machine["name"]
            if not name.startswith(MANAGED_PREFIX) or name in known:
                continue
            # delete_state_machine is asynchronous: a machine stays in
            # DELETING and keeps appearing in listings for up to a minute.
            # Without this check a sweep run just after a delete reports a
            # false orphan, and would try to delete it again.
            try:
                status_now = sfn.describe_state_machine(
                    stateMachineArn=machine["stateMachineArn"]
                ).get("status")
            except sfn.exceptions.StateMachineDoesNotExist:
                continue
            if status_now == "DELETING":
                deleting += 1
                continue
            orphans.append({"name": name, "arn": machine["stateMachineArn"]})

    for orphan in orphans:
        if delete:
            sfn.delete_state_machine(stateMachineArn=orphan["arn"])
            print(f"  deleted orphan  {orphan['name']}")
        else:
            print(f"  ORPHAN          {orphan['name']}  (re-run with --delete)")

    return {"managed_prefix": MANAGED_PREFIX, "known": sorted(known),
            "orphans": orphans, "already_deleting": deleting, "deleted": delete}


def status() -> dict:
    work = workflow_plans.pending()
    return {
        "registry_enabled": workflow_plans.enabled(),
        "workflows_with_plans": workflow_plans.list_workflow_ids(),
        "pending": work,
    }


def verify_compile(workflow: dict) -> dict:
    """Compile without registering — useful in CI on a checked-in canvas."""
    definition = compile_workflow(
        workflow, solver_arn=workflow_plans.SOLVER_PLACEHOLDER,
        require_approval=bool(workflow.get("require_approval", True)),
    )
    return {"states": len(definition["States"]), "start_at": definition["StartAt"]}


if __name__ == "__main__":
    from pathlib import Path

    from dotenv import load_dotenv

    sys.path.insert(0, ".")
    load_dotenv(Path(".env"))

    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "apply":
        print("applying:")
        print(json.dumps(apply(), indent=2, default=str))
    elif command == "sweep":
        print("sweeping:")
        print(json.dumps(sweep("--delete" in sys.argv), indent=2, default=str))
    elif command == "status":
        print(json.dumps(status(), indent=2, default=str))
    else:
        print("usage: python -m infra.deploy_workflows [status|apply|sweep [--delete]]")
        raise SystemExit(2)

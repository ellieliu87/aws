"""Copy CloudFormation stack outputs into backend/.env.

CDK names physical resources itself, so the queue URL and state machine ARN are
not knowable until after a deploy. Stack outputs are how you get them, and this
closes the loop: deploy, then sync, and the app picks up the new values.

That indirection is the point of not hardcoding names — you can deploy a second
copy of the stack (a staging environment) and each one reports its own.

Usage (from backend/):
    python -m infra.sync_env              # show what would change
    python -m infra.sync_env --write      # apply it
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3

STACK = "CmaWorkbenchAsync"
# stack output key -> env var the app reads
MAPPING = {
    "QueueUrl": "CMA_JOBS_QUEUE_URL",
    "StateMachineArn": "CMA_PLAYBOOK_STATE_MACHINE",
    "SolverFunctionArn": "CMA_SOLVER_FUNCTION_ARN",
    "PlaybookRoleArn": "CMA_PLAYBOOK_ROLE_ARN",
    "StateTableName": "CMA_STATE_TABLE",
    "UserPoolId": "CMA_COGNITO_USER_POOL_ID",
    "UserPoolClientId": "CMA_COGNITO_CLIENT_ID",
}
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def stack_outputs(stack: str = STACK) -> dict[str, str]:
    region = (os.getenv("CMA_JOBS_REGION") or os.getenv("CMA_BEDROCK_REGION")
              or "us-east-1").strip()
    stacks = boto3.client("cloudformation", region_name=region).describe_stacks(
        StackName=stack
    )["Stacks"]
    return {
        output["OutputKey"]: output["OutputValue"]
        for output in stacks[0].get("Outputs", [])
    }


def apply(write: bool) -> int:
    outputs = stack_outputs()
    missing = [key for key in MAPPING if key not in outputs]
    if missing:
        print(f"stack {STACK} has no output(s): {', '.join(missing)}")
        return 1

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    changes: list[str] = []

    for output_key, env_key in MAPPING.items():
        value = outputs[output_key]
        line = f"{env_key}={value}"
        for index, existing in enumerate(lines):
            if existing.strip().startswith(f"{env_key}="):
                if existing.strip() != line:
                    changes.append(f"  ~ {env_key}\n      from {existing.strip().split('=', 1)[1]}\n      to   {value}")
                    lines[index] = line
                break
        else:
            changes.append(f"  + {env_key}={value}")
            lines.append(line)

    if not changes:
        print("backend/.env already matches the stack outputs.")
        return 0

    print("\n".join(changes))
    if not write:
        print("\n(dry run — re-run with --write to apply)")
        return 0

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {ENV_PATH}")
    return 0


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(ENV_PATH)
    raise SystemExit(apply("--write" in sys.argv))

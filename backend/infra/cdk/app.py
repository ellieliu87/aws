#!/usr/bin/env python3
"""CDK entry point.

`cdk` runs this file; it builds the object graph and CDK turns it into a
CloudFormation template. Nothing here talks to AWS — that happens on deploy.

Run from this directory:
    npx cdk diff        what would change
    npx cdk deploy      make it so
    npx cdk destroy     remove the whole stack
"""
import os
from pathlib import Path

from aws_cdk import App, Environment
from async_stack import AsyncSolveStack
from dotenv import load_dotenv

# Reuse the backend's .env so the bucket and region are configured in exactly
# one place rather than duplicated into CDK context.
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

bucket = os.getenv("CMA_CORPUS_BUCKET", "").strip()
if not bucket:
    raise SystemExit("CMA_CORPUS_BUCKET must be set in backend/.env — job "
                     "results are written there.")

region = (os.getenv("CMA_JOBS_REGION") or os.getenv("CMA_BEDROCK_REGION")
          or os.getenv("AWS_REGION") or "us-east-1").strip()

app = App()

# Set once an image exists in ECR, to switch the worker from the zip (goal-seek
# only) to the container (real model artifacts):
#     npx cdk deploy -c workerImageTag=<tag>
# or persist it as CMA_WORKER_IMAGE_TAG in backend/.env.
worker_image_tag = (app.node.try_get_context("workerImageTag")
                    or os.getenv("CMA_WORKER_IMAGE_TAG", "").strip() or None)

# Where alarms and the cost budget report. Unset is a supported deploy: the
# alarms are still created, they just have no subscriber, and the budget is
# skipped. Set it and confirm the SNS subscription email that follows —
# an unconfirmed subscription silently drops every notification.
alarm_email = (app.node.try_get_context("alarmEmail")
               or os.getenv("CMA_ALARM_EMAIL", "").strip() or None)

# Defaulted rather than required: the whole stack is built to sit inside the
# free tier, so a $5 month means something changed. Raise it once real
# workloads land, or set it to 0 to opt out.
budget_raw = os.getenv("CMA_MONTHLY_BUDGET_USD", "5").strip()
try:
    monthly_budget_usd = float(budget_raw) or None
except ValueError:
    raise SystemExit(
        f"CMA_MONTHLY_BUDGET_USD must be a number (got {budget_raw!r})."
    ) from None

AsyncSolveStack(
    app, "CmaWorkbenchAsync",
    result_bucket=bucket,
    worker_image_tag=worker_image_tag,
    alarm_email=alarm_email,
    monthly_budget_usd=monthly_budget_usd,
    env=Environment(region=region),
    description="CMA Workbench: async solve queue, worker, and playbook orchestration",
)
app.synth()

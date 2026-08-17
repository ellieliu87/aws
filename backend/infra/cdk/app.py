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

# Set once an image exists, to bring up the web tier on Fargate:
#     npx cdk deploy -c webImageTag=<tag>
# or persist it as CMA_WEB_IMAGE_TAG in backend/.env. Left unset, the stack
# creates the registry and the build project but no VPC, load balancer or
# service — which is what makes the first deploy possible, and also what keeps
# the account at zero cost until you actually want the API running.
web_image_tag = (app.node.try_get_context("webImageTag")
                 or os.getenv("CMA_WEB_IMAGE_TAG", "").strip() or None)

# Two replicas is the point of the whole migration; one is cheaper and proves
# nothing new. Both are legitimate, so it is a knob rather than a constant.
try:
    web_desired_count = int(os.getenv("CMA_WEB_DESIRED_COUNT", "2").strip() or 2)
except ValueError:
    raise SystemExit("CMA_WEB_DESIRED_COUNT must be a whole number.") from None

# Where the provider key lives. Passing it here lets ECS inject the secret from
# Parameter Store at container start, which is the half of that work a laptop
# cannot do — see services/secrets.py.
secrets_prefix = os.getenv("CMA_SECRETS_PREFIX", "").strip() or None

# Shared between the distribution and the load balancer rule that only admits
# traffic carrying it. Derived from the account when unset — see the comment on
# the listener rule for what this token is and, more importantly, is not.
origin_secret = os.getenv("CMA_ORIGIN_SECRET", "").strip() or None

# Where the app is served from, so Cognito knows which redirect back to the
# browser it is allowed to make. Cannot be read off the distribution: the
# client feeds the task definition, which feeds the service, the load balancer
# and then the distribution — referencing it here would close that ring.
# Read it from the SiteUrl output after the first deploy and set it here.
site_url = os.getenv("CMA_SITE_URL", "").strip() or None

AsyncSolveStack(
    app, "CmaWorkbenchAsync",
    result_bucket=bucket,
    worker_image_tag=worker_image_tag,
    alarm_email=alarm_email,
    monthly_budget_usd=monthly_budget_usd,
    web_image_tag=web_image_tag,
    web_desired_count=web_desired_count,
    secrets_prefix=secrets_prefix,
    origin_secret=origin_secret,
    site_url=site_url,
    env=Environment(region=region),
    description="CMA Workbench: async solve queue, worker, playbook orchestration, "
                "and the web tier on Fargate",
)
app.synth()

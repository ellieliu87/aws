# The web tier: FastAPI, the routers, the agent runtime, and the domain packs.
#
# Distinct from infra/cdk/worker_image/Dockerfile on purpose. That image holds
# one file — the solver handler — plus the numerical stack it needs to unpickle
# artifacts, and it is already ~290 MB against ECR's 500 MB free allowance.
# Folding the web framework, the agent SDK and the packs into it would make
# every solver cold start pull a much larger image for the sake of code it
# never calls. Two images, each containing what it actually runs.
#
# Built in AWS by CodeBuild, same as the worker: no local Docker requirement
# and a reproducible build. See infra/build_web_image.py.

FROM python:3.12-slim

# Unbuffered so container logs reach CloudWatch as they happen rather than when
# a buffer fills; a stalled request should be visible while it is stalling.
# No .pyc files because the layer is written once and read once per task.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies as their own layer, before the source. Application code changes
# on every deploy and the dependency set changes rarely, so this ordering means
# a normal deploy re-uploads only the last layer instead of reinstalling
# pandas and scikit-learn each time.
#
# Pinned to the same floors as pyproject.toml. Deliberately NOT `uv sync` from
# the lockfile: that would need the project installed as a package, and
# `[tool.uv] package = false` says it is a leaf application, not a library.
RUN pip install \
      "fastapi>=0.110.0" \
      "uvicorn[standard]>=0.29.0" \
      "python-multipart>=0.0.9" \
      "pydantic>=2.6.0" \
      "python-dotenv>=1.0.0" \
      "pandas>=2.0.0" \
      "numpy>=1.26.0" \
      "pyarrow>=15.0.0" \
      "openpyxl>=3.1.0" \
      "scikit-learn>=1.4.0" \
      "joblib>=1.3.0" \
      "openai>=1.30.0" \
      "openai-agents>=0.0.1" \
      "boto3>=1.34.0" \
      "pyjwt[crypto]>=2.8.0" \
      "pypdf>=4.0.0" \
      "python-docx>=1.1.0" \
      "python-pptx>=0.6.23"

# The application. `infra/` comes along because services/workflow_plans.py and
# the deployer share the compiler — running `python -m infra.deploy_workflows`
# is the same image with a different command and a privileged task role, which
# is how a canvas becomes a state machine without the web tier ever holding
# permission to create one.
COPY main.py ./
COPY agent/ ./agent/
COPY cof/ ./cof/
COPY config/ ./config/
COPY infra/ ./infra/
COPY models/ ./models/
COPY packs/ ./packs/
COPY routers/ ./routers/
COPY services/ ./services/

# Writable scratch for the caches that used to be the record and are now only
# a cache: pulled datasets, pulled model artifacts, synced skills, the RAG
# index. All of it is re-fetchable from S3, which is what makes a container
# filesystem — thrown away when the task stops — an acceptable home for it.
RUN mkdir -p data/datasets data/models data/rag_index agent/skills_user

# Not root. The app writes only under /app and needs no privileged operation.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

# No --reload, and no shell form: exec form makes uvicorn PID 1 so it receives
# SIGTERM directly when ECS drains the task, instead of a shell swallowing it
# and forcing the 30-second kill.
#
# One worker per task on purpose. Scaling is the service's job — more tasks,
# which the load balancer already spreads across — and a second worker inside
# the container would double memory for the same request concurrency an async
# framework already provides.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1"]

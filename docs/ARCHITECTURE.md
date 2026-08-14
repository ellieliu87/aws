# AWS Architecture

What runs in AWS, why each piece is shaped the way it is, and what is still
missing. Everything here is declared in `backend/infra/cdk/async_stack.py` as
one CloudFormation stack, `CmaWorkbenchAsync`.

All of it is **opt-in**. With none of the `CMA_*` environment variables set the
app runs entirely in-process, exactly as it did before any of this existed.
That is a deliberate property, not an accident of staging: a developer with no
AWS account gets the same behaviour, and the fallback paths are exercised by
the same code the deployed system uses.

---

## The problem this solves

The workbench kept its application state in module-level dicts — `_DATASETS`,
`_MODELS`, `_RUNS`, `_SAVED_WORKFLOWS`, the auth token store. Two consequences
followed, and neither was fixable by deployment:

- a restart lost every dataset, model, and run
- a second replica served different data from the first

It was a data-design problem. No amount of load balancing fixes a process that
holds its own truth.

---

## Component map

```
   Analyst (browser)
        │
        ▼
   FastAPI  (backend/main.py)
        │
        ├───────────────► DynamoDB ── single table + gsi1
        │                 registries: dataset · model · workflow · run
        │
        ├── enqueue ────► SQS  Solves ──► Lambda worker ──► S3  jobs/
        │                      │ 3 tries        (zip │ container)
        │                      └──► SolvesDlq         │
        │                                             ▼
        └── start ──────► Step Functions          S3  models/
                          Playbook + compiled
                          canvas machines

   ECR ◄── CodeBuild ◄── S3 builds/worker-image-src.zip
     └── image consumed by the container worker
```

---

## DynamoDB — the state table

**One table, partitioned by entity type, sorted by id.**

```
pk = "workflow"   sk = "sw-abc123"
pk = "dataset"    sk = "ds-abc123"
pk = "model"      sk = "mdl-abc123"
pk = "run"        sk = "run-abc123"
```

Fetching one item and listing all of a type are each a single call, and neither
is a table scan. Access is wrapped by `backend/services/entity_store.py`, which
exposes `get` / `put` / `delete` / `list_all` / `query_index` over plain dicts.
Pydantic models go in as dicts and come back as dicts; converting to a model is
the caller's job, because the storage layer must not import schemas.

**Configuration.** On-demand billing — request volume is an analyst clicking,
which is the spiky low-average pattern on-demand exists for, and it stays inside
the 25 GB always-free tier. Physical names are left to CDK so a second (staging)
stack can coexist; the real name comes out as the `StateTableName` output and
`infra/sync_env.py` copies it into `backend/.env`.

### gsi1 — the time-ordered index

```
gsi1pk = "run#capital_planning"   gsi1sk = "2026-08-13T14:38:42Z"
```

Runs are the one entity that grows without bound — one item per execution,
forever. On the base table, "the newest 50 runs for this function" can only be
answered by reading every run ever recorded and sorting in Python, because the
sort key is a random id.

The index re-keys the same items by (function, time), so that question becomes a
range read that stops after 50 items. **The cost of a page no longer depends on
how many runs exist.** `created_at` is an ISO-8601 UTC string, which sorts
lexicographically in timestamp order — the property the whole index rests on.

The index is **sparse by construction**: a GSI contains only items carrying its
key attributes, so datasets, models, and workflows never enter it and cost
nothing. Entities opt in by passing `index=` to `entity_store.put`.

Projection is `ALL`, which keeps `list_runs` returning whole runs as it does
today. Switching to `INCLUDE` without `series` would shrink the index
enormously but would also empty the series out of list responses — a
frontend-visible change, so it is a separate decision.

### Two limits that shape the code

**400 KB per item.** An item is a row, and the cap counts every attribute name
as well as every value. A list of maps stores its keys once per element, so a
run's `series` is mostly column labels. `store_run` measures the payload and,
if it is over, persists the run *without* its series plus a note recording how
many rows were dropped — while still returning the full series to the caller.
The computation already succeeded; failing the request because its history
record is oversized would be the wrong trade.

**1 MB per Query page.** Unrelated to the item cap and enforced on reads, which
is why `list_all` loops on `LastEvaluatedKey` and why `query_index` returns an
opaque cursor.

### The per-request memo

Moving registries off dicts turned a dict lookup into a network call, and some
callers do that lookup per graph node — chat validation walks the canvas twice,
a workflow run resolves a model per step.

`entity_store.request_cache()` memoizes reads for the span of one request. It is
a `ContextVar`, not a module dict, because that is what makes it *per request*:
asyncio copies the context per task, so concurrent requests cannot see each
other's entries and nothing survives into the next request. Default off means
anything outside a request (workers, startup) is unaffected.

Opened by HTTP middleware in `main.py`, and separately by
`validate_workflow_payload`, which is also reached from an agent tool
mid-stream after the middleware's memo has closed.

Correctness rests on two things: writes go through the same module, so `put`
and `delete` refresh the entry rather than leaving it stale; and every hit is a
deep copy, so a caller that mutates what it got back cannot corrupt the next
reader.

Measured: validating an 11-node canvas went from 21 backend reads to 2.

**The deliberate limit:** within one request the registry is a snapshot. A long
workflow run will not observe a model another request updates midway. For a run
that is arguably the property you want. It is not a general-purpose cache and
must not grow into one.

---

## SQS + Lambda — the async solve path

**`Solves` queue → worker → `SolvesDlq` after 3 attempts.**

The queue's visibility timeout is *derived* as worker timeout + 60s rather than
hardcoded. SQS rejects an event source mapping whose visibility timeout is below
the function timeout, because it would redeliver a message still being
processed. Stating that rule only in a comment is what let a later timeout bump
break it.

Dead letters are retained 14 days; live messages 1 day.

### Two worker flavours, one parameter

`worker_image_tag` selects between them:

| | zip worker | container worker |
|---|---|---|
| Code | `infra/cdk/worker/` | ECR image |
| Memory | 512 MB | 2048 MB |
| Timeout | 2 min | 5 min |
| Capability | goal-seek arithmetic only | real `.pkl` / `.joblib` artifacts |

The zip worker exists so the **first deploy is possible at all**: a Lambda
cannot reference an ECR image that does not exist yet, and the image is built by
the CodeBuild project this same stack creates. So the order is deploy (zip) →
build image → deploy again with the tag. Encoding that as a parameter beats a
deploy that fails until someone reads a runbook.

Container memory is not padding. A cold start pulls the image and unpickling
scikit-learn wants headroom; 512 MB was fine for the zip worker and too tight
here.

### Four modes, one image

One image backs the SQS consumer and every Step Functions task type:

- `solve` — goal-seek, the original stand-in, kept for compiled canvases whose
  nodes carry no artifact
- `predict` — pull a model directory from S3 and run a real artifact
- `request_approval` — park the task token and wait for a human
- `publish` — record the approved run

**Why the whole model *directory* is fetched:** these pickles reference a
sibling `_classes.py` for their custom classes, so `pickle.load` needs that
module importable. Downloading only the `.pkl` fails at unpickle time with a
bare `ModuleNotFoundError`, which is a confusing way to learn about a missing
sidecar. `services/workflow_artifacts.py` maintains the S3 side of that layout.

### IAM, scoped narrowly

The SQS half of the worker's policy is inferred by CDK from the event-source
wiring — one line replaces both the mapping and the permission. The S3 half is
explicit because the intent is narrower than any helper: read/write under
`jobs/`, read-only under `models/`, and a `ListBucket` conditioned on those two
prefixes. Granting the whole bucket would hand the worker the document corpus
for no reason.

---

## Step Functions — durable human approval

The `Playbook` state machine:

```
Solve ──► ApprovalGate ──► Publish
  │           │
  │ retry 3×  │ WAIT_FOR_TASK_TOKEN, 24 h
  │ 2s ×2.0   │
  ▼           ▼
Failed    ApprovalExpired
```

`WAIT_FOR_TASK_TOKEN` is the point of the whole thing. The execution holds at
the gate until `SendTaskSuccess` arrives with the reviewer's answer — the pause
is **durable**, surviving restarts and deploys, which an in-process `await`
never could. Execution timeout is 2 days; the gate's own is 24 hours.

Retries cover `Lambda.ServiceException`, `TooManyRequestsException`, and
`States.TaskFailed` with exponential backoff, then fall to an explicit `Fail`
state rather than a silent stall.

**Compiled canvases.** `services/workflow_compiler.py` turns a Workflow-tab
canvas into its own state machine definition, and `services/workflow_deploy.py`
ships it. Those machines invoke the same worker Lambda, which is why
`SolverFunctionArn` and `PlaybookRoleArn` are stack outputs. The web tier
deliberately **cannot** deploy them: it holds no permission to create AWS
resources, so `/workflows` compiles and registers a plan while
`infra/deploy_workflows.py` ships pending plans under a separate, privileged
identity.

---

## ECR + CodeBuild — the image supply chain

The image is built **in AWS**, not on a developer's machine: no local Docker
requirement and a reproducible build.

Source arrives as a zip in the results bucket (`builds/worker-image-src.zip`),
uploaded by `infra/build_worker_image.py`, avoiding a git-provider integration
for a handful of files.

`IMAGE_TAG` is supplied by the caller rather than derived in the build, so the
tag is known before the build starts and is a deterministic function of the
source. Everything runs in a single `build` phase because CodeBuild runs each
phase in a separate shell — a variable set in `pre_build` is gone by `build`.

**ECR keeps one image.** A scikit-learn image is ~277 MB against a 500 MB free
allowance, so two tags already exceed it. The trade-off, stated plainly: there
is no previous image to roll back to. Rebuilding from a known-good source tree
is the recovery path, acceptable because the tag *is* a hash of that tree, but
slower than repointing at an existing tag. Raise to 2 and accept ~5¢/month if
you would rather have instant rollback.

---

## S3 — one bucket, four prefixes

`CMA_CORPUS_BUCKET`, versioned:

| Prefix | Contents | Written by |
|---|---|---|
| `jobs/` | async solve results | worker |
| `models/` | model artifacts + `_classes.py` sidecars | `workflow_artifacts.py`, `blob_store.py` |
| `datasets/` | uploaded dataset files | `blob_store.py` |
| `builds/` | worker image source zip | `build_worker_image.py` |
| corpus | knowledge-base documents | `corpus_store.py` |

`blob_store` writes model artifacts under the same `models/` layout the Lambda
worker already reads, so an uploaded artifact is reachable by the worker
without a second copy under a different name.

Versioning matters more than durability here: it is the audit trail for which
version of a methodology was in force when a number was published.

---

## On Fargate

**There is no Fargate or ECS in this system.** Compute is Lambda, in two
flavours, orchestrated by Step Functions.

That is the right call for the current workload. Solves are short, spiky, and
event-driven — exactly Lambda's shape — and Lambda's scale-to-zero is what keeps
an idle lab account at no cost. Fargate would mean paying for a task that spends
most of its life idle.

Fargate becomes the right answer when one of these becomes true, and none is
today:

- **a solve exceeds 15 minutes**, Lambda's hard ceiling
- **the image outgrows 10 GB**, Lambda's container limit
- **a model needs more than 10 GB of memory**, or needs a GPU
- **cold starts stop being acceptable** and provisioned concurrency is more
  expensive than a warm task

If that day comes, the migration is contained: Step Functions has a native
`ECS RunTask` integration with the same `WAIT_FOR_TASK_TOKEN` pattern, so the
state machine shape survives. The worker handler's four-mode dispatch would
port unchanged; what changes is the invocation task type and the IAM role it
assumes.

---

## Deployment

```bash
cd backend/infra/cdk
export AWS_PROFILE=cma-lab          # the CDK CLI does not read backend/.env
npx cdk diff                        # always; confirm no [-]/[+] replacement
npx cdk deploy
cd .. && python -m infra.sync_env --write   # stack outputs -> backend/.env
```

First-time sequence, because of the chicken-and-egg noted above:

1. `npx cdk deploy` — zip worker
2. `python -m infra.build_worker_image` — builds and pushes the image
3. `npx cdk deploy -c workerImageTag=<tag>` — switch to the container worker

`cdk diff` before every deploy is not ceremony. The table carries
`RemovalPolicy.DESTROY`, so a change CloudFormation treats as a *replacement*
would take all state with it. CDK builds a real change set for the diff, so
replacement is visible before you commit to it.

---

## Known gaps

**Local disk is now a cache, not the record.** Uploads write through to S3
(`services/blob_store.py`), and the two resolvers — `datasets._resolve_path`
and `models_registry.resolve_artifact` — pull on a local miss, so a node that
never received an upload can still read it. Model pulls bring the `_*.py`
sidecars along, because a pickle that cannot import its classes is no more
useful than a missing one. What remains local-only: skills uploaded to
`agent/skills_user/`, and the RAG index under `data/rag_index/`.

**No backup on the state table.** `point_in_time_recovery_enabled=False` and
`removal_policy=DESTROY`. Fine for a lab; must change before anything in that
table is treated as a record.

**Run storage is unbounded.** gsi1 fixed the read cost; nothing bounds growth.
Either a TTL attribute or an archive-to-S3 path is still needed, and which one
depends on whether a run is a record or scratch.

**Three registries remain in-process.** `_SCENARIOS` (mostly derived at startup
from packs, so arguably a cache), `playbooks._RUNS`, and
`analytics_defs._RUNS`. The playbook one is not a mechanical swap: a background
task mutates its run in place as it progresses and polling reads the mutation,
which needs a write-back-per-transition design rather than a find-and-replace.

**Auth is still in-process.** The bearer-token store is a module dict, so a
second replica rejects tokens the first one issued. It is the last piece of
genuinely user-facing state that has not moved.

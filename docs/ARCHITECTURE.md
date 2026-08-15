# AWS Architecture

What runs in AWS, why each piece is shaped the way it is, and what is still
missing. Everything here is declared in `backend/infra/cdk/async_stack.py` as
one CloudFormation stack, `CmaWorkbenchAsync`.

> **Start with [SOLUTION-ARCHITECTURE.html](SOLUTION-ARCHITECTURE.html)** if you
> want the design decisions and their rationale in plain language. This
> document is the implementation reference underneath it: exact configuration,
> the limits that shape the code, and the operational detail.

All of it is **opt-in**. With none of the `CMA_*` environment variables set the
app runs entirely in-process, exactly as it did before any of this existed.
That is a deliberate property, not an accident of staging: a developer with no
AWS account gets the same behaviour, the fallback paths are exercised by the
same code the deployed system uses, and it is what makes the migration
*incremental* — each service can be switched on by itself, and switched back
off if it turns out to be the wrong call. Every variable is listed in
`backend/.env.example`.

**For where the migration has got to and what comes next, jump to
[Migration status](#migration-status).**

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

   SolvesDlq depth ─────────┐
   worker errors/throttles ─┼─► CloudWatch alarms ─► SNS Alerts ─► email
   Playbook failed/expired ─┘
```

**Where the API itself runs is not in this stack.** There is no ALB, no
CloudFront, no App Runner, no EC2 — `backend/main.py` is started by hand, on a
developer's machine or a box someone provisioned. That is worth stating
plainly, because the premise of everything below is that a *second replica*
must see the same truth as the first, and nothing here creates that second
replica. What the stack does is remove every reason one couldn't exist. Choosing
the hosting is **step 5 of the plan below**.

---

## Migration status

The move to AWS is deliberately incremental — each step replaces one piece of
in-process behaviour with a managed service, and every one of them stays opt-in
behind an environment variable. This is the ledger of where that has got to.

### Landed

| # | Was | Now | Service | Where |
|---|---|---|---|---|
| ✅ | `_DATASETS` / `_MODELS` / `_SAVED_WORKFLOWS` / `_RUNS` dicts | one table, partitioned by entity type | **DynamoDB** | [the state table](#dynamodb--the-state-table) |
| ✅ | "newest 50 runs" = read every run, sort in Python | bounded range read | **DynamoDB gsi1** | [gsi1](#gsi1--the-time-ordered-index) |
| ✅ | UUID token in a module dict | self-describing JWT, verified offline | **Cognito** | [identity](#cognito--identity-without-a-session-store) |
| ✅ | solves computed inline in the request | queued, retried 3×, dead-lettered | **SQS + Lambda** | [the async solve path](#sqs--lambda--the-async-solve-path) |
| ✅ | approval as an in-process `await` | durable pause across restarts and deploys | **Step Functions** | [durable human approval](#step-functions--durable-human-approval) |
| ✅ | uploads and artifacts on local disk | write-through, pull on local miss | **S3** | [one bucket, four prefixes](#s3--one-bucket-four-prefixes) |
| ✅ | worker image built on a laptop, if at all | built in-account from an uploaded source zip | **CodeBuild + ECR** | [the image supply chain](#ecr--codebuild--the-image-supply-chain) |
| ✅ | `provision_async.py`, a boto3 script with retry loops | declared once; deleting a resource deletes it | **CDK** | the file header |
| ✅ | no backup, `DESTROY` on the state table | 35-day restore, table survives the stack | **DynamoDB PITR** | [retention](#dynamodb--the-state-table) |
| ✅ | a failed solve rots silently in the DLQ | five alarms to one topic, to an inbox | **CloudWatch + SNS** | [being told](#cloudwatch--being-told-rather-than-looking) |
| ✅ | Lambda logs kept forever, outside CloudFormation | 14-day retention, owned by the stack | **CloudWatch Logs** | [log retention](#log-retention) |
| ✅ | nothing watching spend on a free-tier design | 80% actual / 100% forecast to email | **AWS Budgets** | [the budget](#the-budget) |

### Planned, in the order worth doing them

Ordered by *what unblocks what*, not by size. Steps 1–3 are independent and can
be taken in any order or skipped; 4 onward build on each other.

| # | Step | Service | Why now, or why not yet |
|---|---|---|---|
| 1 | Get `OPENAI_API_KEY` out of `backend/.env` | **SSM Parameter Store** (SecureString) | The only live exposure left. Independent of everything else, and small. |
| 2 | Bound the growth of runs and job results | **DynamoDB TTL** + **S3 lifecycle** | Blocked on one decision, not on code: is a run a record or scratch? |
| 3 | Move `_SCENARIOS` off a module dict | **DynamoDB** | The last registry holding analyst-created records in memory: `POST /scenarios/from-dataset` writes there, so those are lost on restart. |
| 4 | Move `skills_user/` and `data/rag_index/` off local disk | **S3** (+ a managed vector store when the index outgrows a file) | These are what still tie a request to a particular machine. Must land *before* step 5, or a second replica answers differently from the first. |
| 5 | Actually run the API in AWS, more than once | **ECS Fargate + ALB**, or **App Runner** | The step the whole migration has been for. Also retires step 1's remaining half: a task role means secrets arrive as an identity, not a file. |
| 6 | Hosted UI authorization-code flow; enable MFA | **Cognito** | `USER_PASSWORD_AUTH` is the migration step, not the destination — it keeps the password flowing through this service. |
| 7 | Serve the built frontend from a CDN | **S3 + CloudFront** | `vite build` output has no home today. Wants step 5 first, so there is a stable API origin to point at. |
| 8 | Deepen observability once there is more than one replica | **X-Ray**, structured logs, **CloudTrail** data events | Correlating one request across replicas is a real problem; correlating it across one is not. Deliberately deferred — see [deliberately absent](#deliberately-absent). |

Steps 1–7 are written up in full, with the same numbers, in
[Known gaps](#known-gaps) at the end; step 8's reasoning is in
[deliberately absent](#deliberately-absent).

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

**Retention.** `RemovalPolicy.RETAIN`, and point-in-time recovery on. This table
now holds the datasets, models, saved workflows and run history — the evidence
trail behind numbers that get filed — so the cost of RETAIN is an orphaned table
to delete by hand after a teardown, and the cost of DESTROY is the trail. That
is the correct direction to fail. PITR gives 35 days of second-granularity
restore, bills on stored bytes (cents at this volume), and is the only defence
against a *bad write*: versioning protects the documents in S3, and until this
was turned on, nothing protected these records.

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

## Cognito — identity without a session store

A UUID token is a pointer into one process's memory, which is the single
reason a second replica rejects a token the first one issued. The fix is not a
shared token table — it is to stop needing one.

A Cognito **ID token** carries its claims and is verified offline against the
pool's public keys (`services/cognito_auth.py`). Nothing is looked up, so
nothing is shared. What stays in process memory is a cache of Cognito's
*public* keys, which is safe per-replica because it holds no session state.

**Claim contract**, matching what the workbench already displays and filters on:

| Claim | Used for |
|---|---|
| `cognito:username` | the username every router sees |
| `cognito:groups` | the `groups` list `is_pack_visible` filters packs by |
| `custom:role` | the user badge |
| `custom:department` | the user badge |

**Why the ID token, not the access token.** The API needs `custom:role` and
`custom:department`, which Cognito puts only on the ID token. The purist rule —
ID tokens for the client, access tokens for APIs — applies when the API is a
separate resource server; here the API *is* the application that authenticated
the user. `verify` asserts `token_use == "id"` rather than inferring it, so a
future switch to scoped access tokens fails loudly instead of silently
accepting the wrong shape.

**Revocation.** Offline verification never asks Cognito anything, so a
signed-out token keeps validating until it expires. `global_sign_out` revokes
the refresh token immediately, so no *new* tokens can be minted, but the one in
the user's hand lives out its TTL. Hence the 1-hour token validity: the TTL
*is* the revocation window. A denylist would fix it and would also reintroduce
the shared lookup this design exists to remove.

**Login** uses `USER_PASSWORD_AUTH` so the existing login form keeps working —
the frontend still posts a username and password and gets a token back. The
production path is the Hosted UI authorization-code flow, which keeps the
password out of this service entirely. This is the migration step, not the
destination.

Without `CMA_COGNITO_USER_POOL_ID` the mock login is untouched, so local
development needs no AWS account.

---

## SQS + Lambda — the async solve path

**`Solves` queue → worker → `SolvesDlq` after 3 attempts.**

The queue's visibility timeout is *derived* as worker timeout + 60s rather than
hardcoded. SQS rejects an event source mapping whose visibility timeout is below
the function timeout, because it would redeliver a message still being
processed. Stating that rule only in a comment is what let a later timeout bump
break it.

Dead letters are retained 14 days; live messages 1 day. Fourteen days of
retention is only useful if somebody knows to look inside the fourteen days —
see the `DlqNotEmpty` alarm below.

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
never could. Execution timeout is 2 days; the gate's own is 24 hours, and
`PlaybookApprovalsExpired` is what says so out loud when it lapses.

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
version of a methodology was in force when a number was published. Note the
limit of that claim — it records *what* the artifact was, never who read or
replaced it. Object-level access is a CloudTrail data-event question, and this
account has no trail; see the deliberately-absent list below.

---

## CloudWatch — being told, rather than looking

Two of this system's states are *designed* to be reached, and both used to be
silent when reached. A solve that fails three times lands in `SolvesDlq` and
waits 14 days. An approval nobody answers times out after 24 hours and the
execution sits red in a console. In both cases the analyst who asked for the
number is still waiting, and nothing anywhere says so.

Five alarms, all publishing to one SNS topic:

| Alarm | Metric | What it means |
|---|---|---|
| `DlqNotEmpty` | `ApproximateNumberOfMessagesVisible` (max) | A solve failed 3× and is parked |
| `WorkerErrors` | Lambda `Errors` (sum) | The worker raised — fires *before* the DLQ does |
| `WorkerThrottles` | Lambda `Throttles` (sum) | Concurrency limit, invisible in the error metric |
| `PlaybookFailures` | `ExecutionsFailed` (sum) | An execution reached `Failed` |
| `PlaybookApprovalsExpired` | `ExecutionsTimedOut` (sum) | Most likely the 24-hour gate expired |

Each fires on any occurrence at all — threshold `> 0` over five minutes, one
evaluation period. At this volume a *rate* threshold would only delay the
message.

**Failures and expiries are separate alarms on purpose.** A timed-out execution
is not a broken system; it is a reviewer who never answered. Different cause,
different fix, so it should arrive as a different message.

**`treatMissingData: NOT_BREACHING` matters more than it looks.** None of these
metrics is emitted when nothing goes wrong, so the healthy state is *no data at
all*. The CloudWatch default would leave all five parked in
`INSUFFICIENT_DATA`, which reads like a broken alarm and teaches people to
ignore the whole set.

**Alarms, not a dashboard.** A dashboard has to be visited and nobody visits a
lab dashboard. The point is to be told.

`CMA_ALARM_EMAIL` supplies the subscriber. Left unset the alarms are still
created and still fire — they just have nobody to tell, which is one
subscription away from fixed. Set it and **confirm the email SNS sends**: an
unconfirmed subscription drops every notification silently.

### Log retention

Left to itself, Lambda creates `/aws/lambda/<function>` on first invoke with
retention set to *never expire*, and does it outside CloudFormation — so
`cdk destroy` leaves the group behind and it accrues forever. The stack declares
the group instead, at 14 days: longer than any debugging session, short enough
that logs never become a line item.

It is an explicit `LogGroup` rather than the `logRetention` prop, which is
deprecated and works by deploying a custom-resource Lambda whose only job is to
call `PutRetentionPolicy` — a second function and role to maintain for a
property the function itself can carry.

### The budget

Every sizing decision in this document is a bet that usage stays small: one ECR
image, on-demand DynamoDB inside the 25 GB tier, scale-to-zero Lambda. A monthly
budget is what tells you a bet stopped paying, and it costs nothing.
`CMA_MONTHLY_BUDGET_USD` defaults to `5` — at this design's intended cost, five
dollars means something changed. Two notifications: actual over 80%, and
*forecast* over 100%, which is the useful one because it arrives while the month
can still be changed.

Budget notifications go straight to email rather than through the alerts topic.
Routing them via SNS needs a topic policy granting `budgets.amazonaws.com`
publish rights, and Budgets notifies from `us-east-1` while this stack follows
the app's region. Two lines of subscriber beat a cross-region topic.

### Deliberately absent

- **A CloudTrail trail.** Management events are already in CloudTrail *Event
  history* for 90 days, free, no trail required. A trail buys longer retention,
  data events, and Athena, and costs S3 storage for all of it. The one real
  argument for it is the audit claim in the S3 section above: versioning records *what*
  an artifact was, not *who read it*. If that claim ever has to hold up, data
  events scoped to `models/` is the piece that completes it — and only that
  piece.
- **X-Ray.** The `FastAPI → SQS → Lambda → Step Functions` chain is exactly a
  trace-shaped problem, but with four modes in one function at this volume, the
  log group answers the same questions.
- **Customer-managed KMS keys.** The table and bucket use AWS-managed
  encryption. A CMK is a compliance requirement, not a technical one, and brings
  key policies and rotation with it.
- **GuardDuty, Config, Security Hub, WAF.** Real cost and real noise against an
  account with one queue, one function, and no public endpoint.

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

`cdk diff` before every deploy is not ceremony. `RemovalPolicy.RETAIN` means a
change CloudFormation treats as a *replacement* no longer destroys the state
table — but it does orphan it and stand up an empty one in its place, and the
app follows the new name out of stack outputs. The data survives; the workbench
comes back blank, which is its own kind of bad morning. CDK builds a real change
set for the diff, so replacement is visible before you commit to it.

Optional, and worth setting once: `CMA_ALARM_EMAIL` (alarm and budget
subscriber) and `CMA_MONTHLY_BUDGET_USD` (defaults to `5`). After the first
deploy with an email set, confirm the SNS subscription — until you do, the
alarms fire into nothing.

---

## Known gaps

The detail behind [Planned](#planned-in-the-order-worth-doing-them); the
numbers match.

**1 · Secrets live in a file.** `main.py` loads `backend/.env` before any other
import specifically so `OPENAI_API_KEY` is set by the time `cof.orchestrator`
is imported, and `config/data_services.example.env` points at corporate
integrations that will want credentials of their own. A plaintext provider key
in a file beside the code is the one real exposure left in this design. The fix
is SSM Parameter Store **SecureString** — free, KMS-encrypted — rather than
Secrets Manager, whose rotation machinery is worth $0.40/secret/month only for
credentials that can actually be rotated automatically. Note the *non*-secrets
(`CMA_STATE_TABLE`, the queue URL, the ARNs) do not need this:
`infra/sync_env.py` already distributes stack outputs, and moving them into
Parameter Store is a lateral move until the API itself runs in AWS.

**2 · Run storage is unbounded.** gsi1 fixed the read cost; nothing bounds
growth. Either a TTL attribute or an archive-to-S3 path is still needed, and
which one depends on whether a run is a record or scratch. The same question is
open one layer down in S3: `jobs/` results and noncurrent object versions
accumulate with no lifecycle rule, and versioning means deletes do not actually
reclaim anything. Both are a few lines of CDK once the question is answered —
which is why this is a decision waiting on a person, not work waiting on a
sprint.

**3 · `_SCENARIOS` is still a module dict, and it is not a cache.**
`playbooks._RUNS` and `analytics_defs._RUNS` have both moved to the table; the
playbook one needed a design rather than a rename, because a background task
mutates its run in place as it progresses and polling reads the mutation — that
became a write-back per transition.

What is left (`routers/scenarios.py`) is a *mixed* registry, and the mix is the
problem. Built-in scenarios are re-derived from packs at startup, so every
replica computes the same ones — those really are a cache. But
`POST /scenarios/from-dataset` mints a `scn-…` record from an analyst's dataset
and puts it in the same dict, and that one is not derived from anything. It
dies with the process and is invisible to a second replica. The delete
endpoint already knows the difference — it refuses on
`source_kind == "builtin"` — which is the codebase saying out loud that these
are two kinds of thing sharing one home.

So this is the same data-loss shape the whole migration exists to fix, just
smaller and later-noticed. It ranks third only because steps 1 and 2 are
smaller still, not because it is optional.

**`_TRANSFORMS` (`routers/transforms.py`) genuinely is a cache** and is
deliberately not on the list: it is seeded from pack attachments at startup and
has no write endpoints at all, so there is nothing in it a restart could lose.

**4 · Local disk is now a cache, not the record — but not everywhere.** Uploads
write through to S3 (`services/blob_store.py`), and the two resolvers —
`datasets._resolve_path` and `models_registry.resolve_artifact` — pull on a
local miss, so a node that never received an upload can still read it. Model
pulls bring the `_*.py` sidecars along, because a pickle that cannot import its
classes is no more useful than a missing one.

What remains local-only: skills uploaded to `agent/skills_user/`, and the RAG
index under `data/rag_index/`. The skills are the same write-through pattern
again and should be easy. The index is not: it is rebuilt by embedding a corpus,
so the choice is between shipping the built index to S3 and having every replica
pull it, or moving to a store that is shared by construction. The first is
cheaper and probably right until the corpus grows.

These two are why step 5 waits on step 4. A second replica that cannot see the
first one's skills or index does not fail — it answers *differently*, which is
harder to notice and worse to debug.

**5 · Nothing runs the API in AWS.** Covered under the component map: no ALB,
no CloudFront, no App Runner, no EC2. Every reason a second replica couldn't
exist has now been removed — shared state, shared identity, shared files — and
none of that is worth anything until a second replica does exist. Fargate behind
an ALB is the conventional answer; App Runner is less to operate if the
single-container shape holds. Note this is a *different* Fargate question from
[On Fargate](#on-fargate) above, which is about the solver, not the web tier.

**6 · Auth is stateless when a pool is configured**, and a module dict
otherwise — see the Cognito section above. The mock path is still the default,
so the in-process token store is what runs locally. Two things remain even with
a pool: `USER_PASSWORD_AUTH` means the password still passes through this
service, and MFA is off. Both are deliberate for a lab and both are wrong for
anything else.

**7 · The frontend has no home.** `vite build` produces a bundle that nothing
deploys. S3 with CloudFront in front of it is the obvious shape, and it wants a
stable API origin — step 5 — to point at first.

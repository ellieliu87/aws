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
   Analyst (browser)  ──► https://d25dobitv0e2c3.cloudfront.net
                              │
                        CloudFront
                              ├── default ──► S3  site/ (vite build)
                              │
                              └── /api/*  ──► ALB :80
                                               │  403 unless X-Origin-Verify
                                               ▼
   FastAPI  × 2 Fargate tasks  (backend/main.py)
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

   SSM Parameter Store ──► FastAPI at boot (OPENAI_API_KEY, SecureString)

   SolvesDlq depth ─────────┐
   worker errors/throttles ─┼─► CloudWatch alarms ─► SNS Alerts ─► email
   Playbook failed/expired ─┘
```

**The whole thing runs in AWS, over HTTPS, with the API on two replicas.** That
is the sentence the rest of this document was written to earn: every store the
two replicas read is shared, so they agree by construction rather than by luck,
and the browser reaches both the app and the API through one origin.

Unsetting `CMA_WEB_IMAGE_TAG` removes the web tier, the distribution and the
site bucket, and returns the account to free — the gate is still there, it is
simply open now. What is deliberately still missing is listed under
[the web tier](#ecs-fargate--the-web-tier): autoscaling, and an origin that is
not internet-facing at all.

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
| ✅ | uploads and artifacts on local disk | write-through, pull on local miss | **S3** | [one bucket, six prefixes](#s3--one-bucket-six-prefixes) |
| ✅ | worker image built on a laptop, if at all | built in-account from an uploaded source zip | **CodeBuild + ECR** | [the image supply chain](#ecr--codebuild--the-image-supply-chain) |
| ✅ | `provision_async.py`, a boto3 script with retry loops | declared once; deleting a resource deletes it | **CDK** | the file header |
| ✅ | no backup, `DESTROY` on the state table | 35-day restore, table survives the stack | **DynamoDB PITR** | [retention](#dynamodb--the-state-table) |
| ✅ | a failed solve rots silently in the DLQ | five alarms to one topic, to an inbox | **CloudWatch + SNS** | [being told](#cloudwatch--being-told-rather-than-looking) |
| ✅ | Lambda logs kept forever, outside CloudFormation | 14-day retention, owned by the stack | **CloudWatch Logs** | [log retention](#log-retention) |
| ✅ | nothing watching spend on a free-tier design | 80% actual / 100% forecast to email | **AWS Budgets** | [the budget](#the-budget) |
| ✅ | `OPENAI_API_KEY` in plaintext in `backend/.env` | KMS-encrypted, pulled into the environment at boot | **SSM Parameter Store** | [the key that is not in the file](#ssm-parameter-store--the-key-that-is-not-in-the-file) |
| ✅ | runs and job results accumulated forever | 90-day run TTL, prefix-scoped object lifecycle | **DynamoDB TTL + S3 lifecycle** | [what expires](#what-expires) |
| ✅ | `_SCENARIOS`, one dict holding derived *and* analyst data | built-ins cached, `scn-…` records in the table | **DynamoDB** | [scenarios, and the mixed registry](#scenarios-and-the-mixed-registry) |
| ✅ | uploaded skills and the built RAG index on one node's disk | S3 is the record; each node syncs a cache of it | **S3** | [the last two local-only paths](#the-last-two-local-only-paths) |
| ✅ | nothing anywhere runs the API | **two replicas behind a load balancer**, sharing every store | **ECS Fargate + ALB** | [the web tier](#ecs-fargate--the-web-tier) |
| ✅ | plain HTTP, no home for the frontend, CORS by allowlist | **HTTPS**, the app and API on one origin, bucket private | **CloudFront + S3** | [one origin](#cloudfront--one-origin-and-the-tls-that-comes-with-it) |

### Planned, in the order worth doing them

Ordered by *what unblocks what*, not by size.

| # | Step | Service | Why now, or why not yet |
|---|---|---|---|
| 1 | Retire `USER_PASSWORD_AUTH`; enforce MFA | **Cognito** | The Hosted UI authorization-code flow with PKCE is deployed and a real login has now gone through it, so the password no longer passes through this service on the live path. Two things remain, both deliberate: the old password flow is still *enabled* at the pool as a rollback, and MFA is OPTIONAL rather than REQUIRED. The completed login is what unblocks removing the first. |
| 2 | Deepen observability now that there is more than one replica | **X-Ray**, structured logs, **CloudTrail** data events | Correlating one request across replicas is a real problem; correlating it across one is not. Deliberately deferred — see [deliberately absent](#deliberately-absent). |

Step 1 is written up in full, with the same number, in
[Known gaps](#known-gaps) at the end; step 2's reasoning is in
[deliberately absent](#deliberately-absent).

---

## DynamoDB — the state table

**One table, partitioned by entity type, sorted by id.**

```
pk = "workflow"   sk = "sw-abc123"
pk = "dataset"    sk = "ds-abc123"
pk = "model"      sk = "mdl-abc123"
pk = "run"        sk = "run-abc123"
pk = "scenario"   sk = "scn-abc123"
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

### Scenarios, and the mixed registry

`_SCENARIOS` was one dict holding two different kinds of thing, and the mixture
was the whole problem.

Built-in scenarios are **derived**: `services/data_services.py` recomputes the
CCAR and Outlook set from packs on every startup, so every replica arrives at
the same answer without asking anyone. Those really are a cache, and they stay
one — `routers/scenarios.py:_BUILTIN_SCENARIOS`, a module dict, deliberately.

Scenarios from `POST /scenarios/from-dataset` are **not derived from
anything**. An analyst points at their own dataset and gets an `scn-…` record
that exists nowhere else in the system. Sharing a dict with the built-ins, those
died with the process and were invisible to a second replica — the same
data-loss shape the rest of this migration exists to fix, just smaller and
noticed later. They are now table items like everything else.

The codebase had been saying this out loud for a while: `delete_scenario`
refuses when `source_kind == "builtin"`, a special case that only needed to
exist because two kinds of thing shared one home. The endpoint's behaviour is
unchanged — still a 403, still keyed on `source_kind` rather than on which store
answered, because which store answered is not the frontend's business.

`load_scenario` and `all_scenarios` merge the two. Built-ins are checked first
on purpose: they are the overwhelming majority of lookups and a dict hit costs
nothing, so a network read only happens for an id that could only be
analyst-created.

**`_TRANSFORMS` (`routers/transforms.py`) genuinely is a cache** and is
deliberately still a dict: seeded from pack attachments at startup, with no
write endpoints at all, so there is nothing in it a restart could lose.

### What expires

gsi1 below made *reading* the newest N runs cheap regardless of how many exist.
It did nothing about the fact that the number only ever goes up. Two mechanisms
bound that, and both rest on the same judgement:

> **A run is scratch, not a record.** Everything a published number was derived
> from — the dataset, the model, the saved workflow, the compiled plan versions
> in S3 — is kept indefinitely, so a result stays reproducible long after the
> run row that first reported it is gone. What expires is the transcript, not
> the evidence.

**DynamoDB TTL**, on the `expires_at` attribute. The window lives in
`entity_store.run_ttl()` — now + `CMA_RUN_TTL_DAYS`, default 90, `0` keeps runs
forever. It is shared rather than per-router because there are **three**
unbounded run collections, and a retention window that is 90 days in one and
something else in the others is not a policy:

| Collection | Written by | Expires |
|---|---|---|
| `run` — analytics runs | `scenarios.store_run` | ✅ |
| `adef_run` — analytic-definition runs | `analytics_defs.store_adef_run` | ✅ |
| `pbrun` — playbook runs | `playbooks.store_pbrun` | ✅ |
| `published` — published reports | `playbooks.store_published` | ❌ **never** |

A published report is the thing somebody filed. That is evidence, not
transcript, and it is the one item in this family that must outlive the run
that produced it.

Note `store_pbrun` writes on every progress transition, so an in-flight
playbook's expiry slides forward as it advances — the clock starts when the run
stops changing, which is the behaviour you want.

The attribute is written and stripped by `entity_store`, so it never reaches a
caller's model, and only items carrying it are ever touched — datasets, models
and workflows are exempt by construction, exactly as they are absent from the
sparse index. The GSI entry goes when its base item does, so a history page can
never show a run the table no longer holds. TTL deletes are not billed as
writes, which is the whole reason to prefer this to a scheduled sweeper.

Two properties worth stating plainly. Deletion is asynchronous and best-effort —
DynamoDB promises *within a few days of expiry*, not on the second — so this
bounds growth and is **not** an access control. And it applies only to runs
written from here on; items already in the table carry no expiry and are never
swept.

**S3 lifecycle**, applied by `infra/apply_lifecycle.py`:

| Prefix | Rule |
|---|---|
| `jobs/` | current versions expire at 30 days, noncurrent at 1 |
| `datasets/` | noncurrent versions expire at 180 days |
| `builds/` | noncurrent versions expire at 90 days |
| `models/` | **nothing** |
| bucket-wide | incomplete multipart uploads aborted at 7 days; orphaned delete markers removed |

`models/` is the exception and the reason every expiry rule is prefix-scoped:
its noncurrent versions *are* the audit trail described under
[S3](#s3--one-bucket-six-prefixes). Expiring them would quietly convert "we can
show which artifact produced this number" into "we can show it for a year", and
a claim that decays on a timer is worse than one never made.

Note the `jobs/` rule has two halves. On a versioned bucket an expiry alone only
writes a delete marker and the bytes stay, billed, forever — the single most
common way a lifecycle rule does nothing at all.

**Why a script and not CDK.** The bucket is not created by the stack;
`CMA_CORPUS_BUCKET` names one that already existed, and the stack imports it
with `Bucket.from_bucket_name`, which yields a reference rather than a resource.
CDK cannot attach a lifecycle configuration to something it does not own, and
the alternatives are a custom-resource Lambda and role for one API call, or
adopting the bucket into the stack and putting the document corpus one
`RemovalPolicy` mistake away from deletion. The script prints the existing
configuration before replacing it, because `PutBucketLifecycleConfiguration`
replaces wholesale rather than merging.

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

## SSM Parameter Store — the key that is not in the file

`main.py` loads `backend/.env` before any other import specifically so that
`OPENAI_API_KEY` is set by the time `cof.orchestrator` is imported. That works,
and it was also the one real exposure left in this design: a plaintext provider
key in a file beside the code, copied onto every machine that runs the backend,
one `git add -A` away from a repository.

Set `CMA_SECRETS_PREFIX` and `services/secrets.py` pulls the credentials from
Parameter Store instead, in the same pre-import window and for the same reason.
The last path segment becomes the variable name, so the mapping is legible in
the console without reading any code:

```
/cma/workbench/OPENAI_API_KEY   ->  OPENAI_API_KEY
/cma/workbench/OPENAI_BASE_URL  ->  OPENAI_BASE_URL
```

Nothing downstream changes: the `openai` SDK still auto-discovers the key from
the environment exactly as it does today.

**Parameter Store, not Secrets Manager.** SecureString is KMS-encrypted and
free at any volume this app will reach. Secrets Manager's $0.40/secret/month
buys rotation machinery, which is worth paying for credentials that *can* be
rotated automatically. A provider API key is not one of those — rotating it
means logging into the provider's console.

**Precedence** decides which copy wins when there are two. A variable already
exported in the real process environment is never overwritten: that is a
developer or a container runtime being explicit, and it must beat a remote value
they may not know about. Everything else loses to Parameter Store, `.env`
included, because the point is that the file stops being the source of truth.
`main.py` snapshots `os.environ` *before* `load_dotenv` runs to keep the two
distinguishable, which is impossible to reconstruct afterwards.

A lookup failure is logged, not fatal. An unreachable Parameter Store or a
missing permission leaves the app starting on whatever `.env` provided and
complaining loudly — the same shape as every other AWS seam here.

**The parameter is created out of band, and has to be.** CloudFormation cannot
create a SecureString: `AWS::SSM::Parameter` supports `String` and `StringList`
only, because a template is a plaintext document that ends up in the console, in
change sets, and in anyone's `cdk diff`. Declaring the secret in CDK would write
the key into precisely the places this exists to keep it out of. So
`infra/put_secret.py` does it, once, and deliberately does not edit
`backend/.env` afterwards — deleting the only copy of a key before anyone has
confirmed the parameter reads back is a bad afternoon. It prints the line to
remove instead.

**What this fixes, and where.** Reading the parameter needs AWS credentials of
its own, so on a laptop it trades a plaintext key for an AWS profile. That is
the limit of what this module can do.

**In the deployed system none of this code runs**, which is the point rather
than an oversight. `CMA_SECRETS_PREFIX` is deliberately absent from the ECS
task's environment — see `web_env` in `infra/cdk/async_stack.py` — so
`enabled()` is False and the app never calls SSM. The *execution* role, held by
the ECS agent rather than by the container, resolves the SecureString before the
process starts and injects it as an ordinary environment variable. The task role
the application actually runs under has no `ssm:GetParameter` at all: it reaches
DynamoDB, SQS, Step Functions, S3, Cognito and Bedrock, and cannot read the
credential it is using. The secret is delivered by an identity the application
cannot impersonate, which is a stronger property than encrypting it at rest.

Note the non-secrets — `CMA_STATE_TABLE`, the queue URL, the ARNs — do not
belong here. `infra/sync_env.py` already distributes those from stack outputs,
and moving them would be a lateral move that buys nothing.

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

**Login** is the Hosted UI authorization-code flow with PKCE. The browser goes
to Cognito's own page, comes back with a single-use code, and exchanges it for
tokens itself — nothing in the request path ever sees a password. PKCE rather
than a client secret because the frontend is a static bundle on a CDN and
therefore a public client by definition: it cannot keep a secret. The proof of
"I started this flow" is a per-login random verifier of which only the SHA-256
is sent up front, and `state` is round-tripped and checked, because without it an
attacker can feed a victim a code of their own and log them into the attacker's
account — a login that *succeeds*, which is what makes it easy to miss.

A real login has been completed through this flow on the deployed system.

`USER_PASSWORD_AUTH` remains enabled at the pool, and that is now the only part
of this that is still the migration step rather than the destination. The CDK
deploy, the backend image and the frontend bundle ship separately, so leaving it
on made a broken hosted flow a rollback rather than a lockout. With a login
proven, removing it is step 1 of [Planned](#planned-in-the-order-worth-doing-them).

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

## S3 — one bucket, six prefixes

`CMA_CORPUS_BUCKET`, versioned:

| Prefix | Contents | Written by |
|---|---|---|
| `jobs/` | async solve results | worker |
| `models/` | model artifacts + `_classes.py` sidecars | `workflow_artifacts.py`, `blob_store.py` |
| `datasets/` | uploaded dataset files | `blob_store.py` |
| `builds/` | worker image source zip | `build_worker_image.py` |
| `skills/` | analyst-uploaded agent skills | `routers/skills.py` |
| `rag_index/` | built vector indexes, named by content fingerprint | `agent/retrieval.py` |
| corpus | knowledge-base documents | `corpus_store.py` |

`blob_store` writes model artifacts under the same `models/` layout the Lambda
worker already reads, so an uploaded artifact is reachable by the worker
without a second copy under a different name.

Versioning matters more than durability here: it is the audit trail for which
version of a methodology was in force when a number was published. Note the
limit of that claim — it records *what* the artifact was, never who read or
replaced it. Object-level access is a CloudTrail data-event question, and this
account has no trail; see the deliberately-absent list below.

Versioning also means a delete reclaims nothing and an overwrite keeps both
copies, which is what [what expires](#what-expires) is about. `models/` is
exempt from every expiry rule, because its noncurrent versions are the trail
this paragraph is claiming exists.

### The last two local-only paths

`ensure_local` — write locally and to S3, pull on a local miss — is the right
shape for datasets and model artifacts, because those are read **by id**. A
request names the thing it wants, so a miss is detectable and the pull happens
exactly then.

Skills and the RAG index are not read that way, and that difference is the
whole design:

| | `agent/skills_user/` | `data/rag_index/` |
|---|---|---|
| Read by | globbing a directory | fingerprint lookup |
| A miss looks like | the skill is simply absent from the list | a cache miss |
| So it needs | the whole prefix reconciled | pull on miss, plus a startup sync |

**Skills.** A missing file does not raise — it just is not in the listing,
which is exactly the *answers differently* failure this is meant to prevent. So
`skill_loader.sync_user_skills` reconciles the whole prefix rather than fetching
one key: it pulls anything newer than the local copy, and **prunes** local files
that no longer exist in S3, which is how a delete on one node reaches the
others. Pruning applies only when the listing *succeeded* — treating an
unreachable bucket as "nothing exists" would empty the directory it was meant to
protect.

It is throttled to `CMA_SKILLS_SYNC_SECONDS` (default 30) because
`load_all_skills` is a hot path — the orchestrator resolves skills per chat turn,
and a LIST on each would put S3 in the middle of every message. That interval is
therefore the staleness window between replicas. The node that served the upload
writes through immediately, so it never sees its own edit late.

**The RAG index.** Not an upload but a *build*: embedding the whole corpus
through Bedrock, which is the burstiest thing this app does. Left on local disk
that cost is paid again per replica — and worse, a node still building falls
back to keyword scoring, so two replicas answer the same question differently
until they converge.

The file is named by a content fingerprint of the chunks, model and dimensions,
which is what makes sharing it safe: a name collision means the inputs were
identical, and a corpus change produces a different name rather than a stale
hit. So there is no coherence problem to solve, only a transfer.

`sync_indexes()` runs at startup and goes **both ways**. Pull, so a fresh
replica does not re-embed what another node already paid for. Push, because
`_save_cached` only uploads at the moment of a rebuild, and a rebuild only
happens when the corpus changes — without the push half, an index built before
this seam existed would stay on one machine forever.

Deliberately **not** a managed vector store. At this corpus size the index is
about 1 MB, a dot product over it is instant, and a vector database would be a
standing cost for a file that fits in memory. The day the corpus outgrows that,
`_pull_index` / `_push_index` are the seam to replace.

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

## CloudFront — one origin, and the TLS that comes with it

```
                    https://d25dobitv0e2c3.cloudfront.net
                                   │
                          CloudFront distribution
                          │                     │
              default ────┤                     ├──── /api/*
                          ▼                     ▼
                  S3  site bucket          ALB :80  ──► Fargate ×2
                  (private, OAC)           403 unless
                  + history fallback       X-Origin-Verify
```

One distribution, three problems, which is why they were one gap rather than
three.

**TLS, without owning a domain.** An ALB's own `*.elb.amazonaws.com` name
cannot carry a certificate — nobody controls that domain, so ACM will not issue
for it. CloudFront arrives with a hostname *and* a certificate already attached.
The viewer protocol policy is `REDIRECT_TO_HTTPS`, so plain HTTP answers `301`
rather than serving anything. A domain of your own works equally well and costs
~$12–15/year; the certificate is free either way.

**A home for the frontend.** `vite build` output goes to a private bucket
reached by Origin Access Control, so the bucket is not public and the
distribution is the only way in.

**The CORS question stops existing.** `src/lib/api.ts` sets `baseURL: ''` — the
app has always called same-origin `/api/...`, which is why this needed no
frontend change at all. Behind one distribution the browser app and the API
share an origin, so `main.py`'s localhost-only `allow_origins` list is once
again about local development and nothing else.

### Two behaviours, and why they must differ

| | default | `/api/*` |
|---|---|---|
| Origin | S3 (OAC) | ALB |
| Cache | `CACHING_OPTIMIZED` | **`CACHING_DISABLED`** |
| Methods | GET/HEAD | **ALL** |
| Forwards | — | `ALL_VIEWER_EXCEPT_HOST_HEADER` |

`CACHING_DISABLED` on the API is not a tuning choice. Caching an authenticated
response would serve one analyst's data to another, which is a data-leak with a
cache-hit ratio. `ALL_VIEWER_EXCEPT_HOST_HEADER` forwards `Authorization`,
cookies and query strings, and withholds `Host` because a custom origin should
see its own hostname.

### The history fallback, and the trap under it

`App.tsx` uses `BrowserRouter`, so `/workspace/capital_planning` is a route the
browser must be able to request directly. The obvious lever is CloudFront's
custom error responses — map 404 to `/index.html` — and it is a trap: **error
responses are distribution-wide**, so the same rule would rewrite a genuine API
404 into an HTML page with a `200`. A frontend that works and an API that lies.

A **CloudFront Function** attaches to one behaviour instead. Any path with no
dot in it is a route rather than a file, so it is rewritten to `/index.html`;
`/assets/index-a1b2c3.js` has a dot and passes through. The API behaviour never
sees the function.

### The load balancer answers 403

Adding HTTPS at the distribution does nothing if the plaintext origin still
serves the same API to anyone who finds it — that is not "the API is on HTTPS",
it is "HTTPS is also available". So the listener's default action is a fixed
`403`, and a rule at priority 10 forwards to the tasks only when the request
carries `X-Origin-Verify`.

**Be honest about that token.** CloudFront can only send a literal string as an
origin header, so it lives in the template and in the distribution config: it is
a bypass guard, not a credential. It stops direct access and casual scanning; it
does not stop anyone who can read the stack. The real fix is an origin that is
not internet-facing at all, which needs VPC origins or a private subnet and a
NAT gateway. Override the derived value with `CMA_ORIGIN_SECRET`.

### Publishing the frontend

```bash
cd frontend && npm run build
cd ../backend && python -m infra.deploy_frontend --write
```

Not CDK's `BucketDeployment`, which bundles assets at synth time and deploys a
custom-resource Lambda to copy them — that turns every frontend change into a
CloudFormation deployment. Uploading files to a bucket does not need a change
set.

Cache headers are split, and the split is the whole design:

| File | Header | Why |
|---|---|---|
| `assets/*` | `max-age=31536000, immutable` | Vite fingerprints the name, so the content can never change under it |
| `index.html` | `no-cache` | The one stable name, and the file that points at the fingerprinted ones |

Which is also why the invalidation names `/index.html` and `/` rather than
`/*`: everything else got a new name, so there is nothing stale to invalidate,
and `/*` would spend the free allowance on paths whose content cannot change.

---

## ECS Fargate — the web tier

**Built, not deployed.** The stack declares it; nothing is running. Deploy
without `CMA_WEB_IMAGE_TAG` and you get the image registry and the build
project and nothing else, which is what keeps the account at zero while the
rest of this document stays true.

This is the step everything else was for. Shared state, shared identity, shared
files, shared skills, a shared search index — each removed one reason a second
replica could not exist, and none of it was worth anything while `main.py` ran
once, by hand.

```
internet ──► ALB :80 ──► ECS service (Fargate)
                           ├─ task  cma web image   0.25 vCPU / 1 GB
                           └─ task  cma web image        ← the second replica
                                       │
                                       ├─ DynamoDB · S3 · SQS · Step Functions
                                       ├─ Cognito (verify offline)
                                       └─ OPENAI_API_KEY, injected by ECS
```

**Two tasks by default.** `CMA_WEB_DESIRED_COUNT` defaults to 2 because "run
the API in AWS" was never the goal — a *second replica* was, and one task
proves nothing a laptop did not already prove. Set it to 1 to halve the
compute once the point has been made.

**No NAT gateway, and the tasks are in public subnets.** A NAT is ~$32/month
before it passes a byte, which on a stack budgeted at $5 would be the largest
line by a wide margin — more than the compute it exists to serve. The trade,
stated plainly: the tasks are addressable from the internet at the network
level, and only their security group keeps them private. That group admits the
load balancer and nothing else. A production account puts the tasks in private
subnets and pays for the NAT, or uses VPC endpoints.

**The secret arrives as an identity.** `ecs.Secret.from_ssm_parameter` injects
`OPENAI_API_KEY` at container start using the execution role, so no secret
material exists on the host and no application code fetches one. This is the
half of [the key that is not in the file](#ssm-parameter-store--the-key-that-is-not-in-the-file)
that a laptop cannot do — there, `services/secrets.py` still needs AWS
credentials of its own, so a plaintext key was traded for a local profile.
`services/secrets.py` becomes dead code the day this is the only way the app
runs.

**Environment comes from constructs, not from a file.** Every `CMA_*` value in
the task definition is read off the table, queue, machine and user-pool objects
in the same stack. `infra/sync_env.py` exists because a human starts `main.py`
by hand; a task definition needs no such step.

**IAM, scoped to what the web tier does.** Read/write on the table, send on the
queue, start-execution and task-response on the state machine, object access
under the bucket, sign-in and sign-out on the pool, and model invocation on
Bedrock. Note what is absent: **nothing here can create AWS resources.** That is
the same argument as `services/workflow_plans.py` — the request path compiles
and records a plan, and a separate privileged identity deploys it — and it only
holds while the task role stays this narrow.

### What this does not include

- **A private origin.** The load balancer is internet-facing and refuses
  anything without `X-Origin-Verify`, which is a guard rather than a boundary —
  see [one origin](#cloudfront--one-origin-and-the-tls-that-comes-with-it).
  Making it genuinely unreachable needs VPC origins, or a private subnet and the
  NAT gateway this design spent effort avoiding.
- **Autoscaling.** Fixed task count. Adding a target-tracking policy is a few
  lines, and pointless before there is traffic to track.

TLS and CORS *were* on this list and are not any more; the distribution took
both.

### The first deploy, and why it is three commands

Same chicken-and-egg as the worker: a service cannot reference an image that
does not exist, and the image is built by a project this stack creates.

```bash
npx cdk deploy                        # registry + builder, still free
python -m infra.build_web_image       # builds and pushes; prints the tag
npx cdk deploy -c webImageTag=<tag>   # VPC, ALB, service — costs begin
```

`build_web_image.py` packages an explicit allowlist of the application rather
than zipping the tree, and refuses outright if anything resembling a credential
is in the set. An image layer is immutable and gets pushed to a registry; a
secret baked into one is not a mistake you quietly correct later.

**ECR keeps two web images**, against the worker's one. The asymmetry is
deliberate: a bad worker image fails a solve that can be retried, while a bad
web image takes the application down, and the fix at that moment should be
repointing at the previous tag rather than waiting out a rebuild.

---

## On Fargate — for the *solver*

**The solver does not run on Fargate, and should not** — even though the
section above puts the web tier there. Solver compute is Lambda, in two
flavours, orchestrated by Step Functions.

Two opposite conclusions about the same service, which is worth stating
plainly because reading one as the answer to the other is the easiest mistake
this document invites. The web tier is a long-lived process serving requests,
so it wants a container that stays up. Solves are short, spiky, and
event-driven — exactly Lambda's shape — and scale-to-zero is what keeps an idle
lab account at no cost. Fargate for the solver would mean paying for a task
that spends most of its life idle, which is precisely the cost the web tier
accepts and the solver has no reason to.

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
cd backend
python -m infra.deploy              # diff + dry runs; changes nothing
python -m infra.deploy --write      # deploy, sync .env, apply S3 rules
```

`infra/deploy.py` runs the four steps in dependency order and stops at the
first failure. It reads `backend/.env` itself, so `AWS_PROFILE` set there
reaches the CDK CLI — which does *not* read that file, and is the usual way a
deploy ends up pointed at the wrong account. The bare command is the review
gate: it prints the diff and applies nothing, and `--write` is a second,
deliberate invocation. It also refuses to deploy when the diff reports a
replacement, overridable with `--allow-replacement`.

`infra.put_secret` is deliberately not one of its steps — it carries a value
that has to come from a human, and `put_parameter` mints a new version on every
call, so running it per deploy would churn versions of an unchanged key.

By hand, equivalently:

```bash
cd backend/infra/cdk
export AWS_PROFILE=cma-lab          # the CDK CLI does not read backend/.env
npx cdk diff                        # always; confirm no [-]/[+] replacement
npx cdk deploy
cd .. && python -m infra.sync_env --write      # stack outputs -> backend/.env
python -m infra.apply_lifecycle --write        # S3 rules
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

Two things live outside the stack, for reasons given in their own sections.
`apply_lifecycle` is folded into `infra.deploy` above; `put_secret` is not, and
runs on key rotation rather than on deploy:

```bash
python -m infra.put_secret OPENAI_API_KEY   # dry run; --write to apply
```

It needs `CMA_SECRETS_PREFIX` set — CloudFormation cannot create a SecureString,
which is why this is a script at all.

---

## Known gaps

The detail behind [Planned](#planned-in-the-order-worth-doing-them); the
numbers match.

**1 · The password path is still enabled, and MFA is not enforced.** The
substance of this gap has closed: the Hosted UI authorization-code flow with
PKCE is deployed, and a real login has been completed through it, so on the live
path no password reaches this service. What is left is two switches, and it is
worth being exact about which is which.

`USER_PASSWORD_AUTH` is still *enabled* at the pool. Nothing uses it — the
frontend no longer has a password form — but while it is enabled the property
above is true of the frontend rather than enforced by the pool, and anyone
holding a username and password can still obtain a token directly. It stayed on
because the CDK deploy, the backend image and the frontend bundle ship as three
separate steps, so there is no instant at which all three change together, and
leaving it on made a broken hosted flow a rollback rather than a lockout. A
completed login is precisely the evidence that was missing, so this is now a
one-line change with nothing gating it.

MFA is OPTIONAL with authenticator apps enabled, not REQUIRED. REQUIRED locks
out every existing user until each enrols, and this pool's only member is the
person running the deploy. It becomes correct the moment there is a second user
to coordinate, and it is one word.

Verification was never the problem — a JWT checked offline against the pool's
public keys, which is what let two replicas exist at all. The mock login remains
the default only when no pool is configured, which now means local development
and nothing else.

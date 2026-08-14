# The CMA Workbench on AWS

**A solution architecture, and the reasoning behind it.**

This document explains what the CMA Workbench is, why it needed re-architecting,
and the decision behind each piece of the AWS design. It is written to be read
start to finish by someone who has not seen the code.

Two companion documents go deeper where this one summarises:
[ARCHITECTURE.md](ARCHITECTURE.md) for the AWS implementation detail, and
[ORCHESTRATOR.md](ORCHESTRATOR.md) for the workflow engine internals and the
corporate-proxy data integrations.

---

## 1. What the system does

The CMA Workbench is a self-service analytics platform for the analysts who
produce a bank's capital-planning numbers — the deposit balance forecasts and
net-interest-income projections that feed CCAR stress testing and the annual
outlook.

Before a tool like this, that work is spreadsheets and one-off scripts. The
workbench gives an analyst four things in one place:

- **Data** — connect to a warehouse table or upload a file, and see its schema
  and sample rows immediately.
- **Models** — register an existing model artifact, or train a regression in
  the browser against bound data.
- **Workflows** — draw a pipeline on a canvas (data → transform → model →
  output), run it, and download the result.
- **Agents** — ask questions in plain language, with AI agents that can read
  the datasets, models, and runs the analyst is looking at.

The important characteristic for what follows: **the numbers this produces get
filed with a regulator.** That single fact drives more of the architecture than
any performance requirement does.

---

## 2. Why it needed re-architecting

The original build kept everything it knew in the memory of one running
process — every dataset, every registered model, every completed run, every
logged-in user's session.

Three consequences followed, and the third is the one that matters:

1. **Restarting lost work.** Deploy a fix on Friday afternoon and every dataset
   an analyst uploaded that week was gone.
2. **It could not scale.** Adding a second server would not help, because the
   two servers would disagree about reality — a dataset uploaded to one was
   invisible to the other.
3. **There was no durable record of what happened.** If a regulator asked
   "which calculation produced this number?", the honest answer was that the
   evidence lived in a process that had since restarted.

**The diagnosis matters more than the symptoms.** This was not a deployment
problem. There is no amount of load balancing, auto-scaling, or containerisation
that fixes a process which holds its own truth. It was a *data design* problem,
and it had to be solved as one before any hosting change would mean anything.

That is the thesis of this whole architecture, and the reason the work happened
in the order it did.

---

## 3. Principles applied throughout

Five rules shaped every decision below.

**Fix the data design before the hosting.** Moving state out of the process is
what makes a second server meaningful. Doing it in the other order produces
servers that disagree with each other — worse than one honest server.

**Every AWS dependency is optional.** Every integration checks for its
configuration and falls back to the original local behaviour when absent. A
developer with no AWS account runs the full application. This is not developer
convenience for its own sake: it means the fallback path is exercised
constantly, so it still works when it is needed, and it keeps the system
testable without a cloud account.

**Prefer managed services with no idle cost.** This is a system used by a
handful of analysts in bursts. Anything billing a standing capacity floor is
the wrong shape, regardless of its technical merits.

**Make the audit trail a property of the design, not a feature.** Versioned
artifacts, retained execution history, and immutable compiled plans are
cheaper to build in from the start than to add under regulatory pressure.

**Say plainly what is built and what is designed.** Section 8 lists both. A
design document that blurs the two is not usable by anyone.

---

## 4. How the platform works

Two distinct paths through the system, with different requirements.

### The interactive path

An analyst clicks something; the answer comes back immediately. Browsing
datasets, previewing data, editing a chart, talking to an agent. These are
short, chatty, and latency-sensitive — including agent responses, which stream
back token by token over a held-open connection.

### The workflow engine

An analyst draws a pipeline on a canvas and runs it. This is the heart of the
product, and it works like this:

1. **Validate before running.** The canvas is checked for problems while the
   diagram is still on screen — circular dependencies, a model whose input is
   missing, a model expecting a column the upstream data does not have. Fixing
   a broken pipeline is far cheaper before it runs than after.
2. **Work out the order.** The pipeline is a dependency graph, so the engine
   sorts it into an execution order and rejects anything circular.
3. **Resolve the inputs.** Each step's input is assembled from what feeds it —
   a scenario, an uploaded dataset, a warehouse table, or the output of an
   earlier model.
4. **Run each model in isolation.** Model artifacts are third-party code. They
   execute in a separate sandboxed process with a time limit, so a model that
   hangs or crashes cannot take the platform down with it.
5. **Record every run.** Inputs, outputs, timing, status, and the scenario
   context are all retained.
6. **Write the outputs.** Results go to a CSV download or a warehouse table.
   Where several models feed one output — three deposit models feeding a
   net-interest-income calculator, for instance — their results are merged and
   labelled by source.
7. **Explain failures in the analyst's language.** Failures are classified into
   named causes with a plain-English hint (`FEATURE_MISMATCH`, `DQC_FAILED`,
   `TIMEOUT`) rather than surfacing a stack trace. An analyst who can read the
   error can usually fix it themselves.

Analysts can save named pipelines and reload them. The in-progress canvas also
survives a browser reload independently, so switching tabs never loses work.

---

## 5. The AWS architecture

```
   Analyst (browser)
        │
        ▼
   Web tier — the application API
        │
        ├──────────────► DynamoDB      registries: datasets, models,
        │                              workflows, runs
        │
        ├──────────────► Cognito       identity; tokens verified offline
        │
        ├──────────────► S3            uploaded files, model artifacts,
        │                              documents, results
        │
        ├── enqueue ───► SQS ─────────► Lambda      heavy computation,
        │                  │ 3 tries    worker      off the request path
        │                  └──► dead-letter queue
        │
        └── start ─────► Step Functions   multi-step runs with a durable
                                          human approval gate

        ECR ◄── CodeBuild        the worker's container image,
                                 built in AWS, not on a laptop
```

### What each service is doing, and why it was chosen

**DynamoDB — the system's records.** Datasets, models, saved workflows, and
completed runs. Chosen over a relational database because the access patterns
are simple and known in advance — fetch one item by id, or list all items of a
type — and because on-demand billing costs nothing when nobody is clicking. A
relational database would bill for an idle instance and buy flexibility this
workload does not need. *If* the requirement grew to ad-hoc analytical queries
across entities, that trade would flip and Aurora Serverless would be the
answer.

**Cognito — identity.** Covered in detail in section 6, because the decision
there is more interesting than the service choice.

**S3 — everything that is a file.** Uploaded datasets, model artifacts, the
document corpus, and computation results. Versioning is on, which matters less
for durability than for evidence: it answers which version of a methodology
document was in force when a number was published.

**SQS + Lambda — computation off the request path.** A model run inside a web
request is a resource risk with a user-visible failure mode: one heavy
calculation can consume the process that is also serving everyone else's page
loads. Publishing the work to a queue and running it on a worker that scales
independently removes that coupling. Failed messages retry three times, then
land in a dead-letter queue rather than disappearing.

**Step Functions — multi-step runs with human approval.** Explained in
section 6.

**ECR + CodeBuild — the worker's image.** Built inside AWS from source uploaded
to S3, so no developer needs Docker installed and the build is reproducible.
The image tag is a hash of the source, which makes "what exactly is running?"
answerable.

---

## 6. Design decisions and trade-offs

This is the section that matters. Each decision states the problem, the options,
the choice, and what was given up.

### 6.1 Externalising state before changing the hosting

**Problem.** The application could not run as more than one copy.

**Options.** Add sticky sessions at the load balancer so each user always
returns to the same server; or move the state out of the process.

**Chose.** Move the state out. Sticky sessions would have hidden the problem
rather than solved it — a server restart still loses that user's work, and one
overloaded server cannot shed load to another.

**Gave up.** Reads that used to be instant memory lookups became network calls.
That cost was real and had to be managed (see 6.6).

### 6.2 One DynamoDB table, not one per entity type

**Problem.** Four kinds of record needed durable storage.

**Chose.** A single table, where each record's key is its type plus its id.
Fetching one record and listing all records of a type are each one operation,
and neither scans the table.

**Why.** Fewer things to provision, monitor, and back up. The alternative — four
tables — buys separation the application does not need, since nothing queries
across types.

**Gave up.** It is less immediately obvious to someone browsing the console. A
comment in the code and this document are the mitigation.

### 6.3 Stateless tokens instead of a shared session store

**Problem.** Sessions lived in one server's memory, so a second server rejected
tokens the first one issued. This was the last thing preventing horizontal
scaling.

**Options.** Move sessions to a shared store (DynamoDB or Redis), or stop
needing a session lookup at all.

**Chose.** The second. A Cognito token is *signed* and carries the user's
identity, role, department, and group memberships inside it. Any server
verifies it mathematically against Cognito's public keys — no lookup, so
nothing to share.

**Why this is the better answer.** A shared session store is still a shared
dependency: another thing to scale, another thing whose outage is an outage.
Verification needs nothing but the token and a cached public key.

**Gave up.** A signed token cannot be un-signed. Signing a user out revokes
their ability to obtain *new* tokens, but the one already in their browser
remains valid until it expires. The mitigation is a short token lifetime — one
hour — which makes the expiry window the revocation window. A revocation
denylist would close that gap and would also reintroduce exactly the shared
lookup this design removed. **If** the requirement were immediate revocation —
plausible for a privileged-access system — a short-TTL denylist would be
correct, and the cost would be accepted deliberately rather than by default.

### 6.4 Human approval as a durable state, not application code

**Problem.** Model governance requires a person to review results before they
are published — the champion/challenger gate. Implemented in application code,
that means the process must stay alive while it waits, which could be hours or
days. A restart mid-wait strands the run, requiring a repair routine to detect
and clean up the wreckage afterwards.

**Chose.** Step Functions, where "wait for a human" is a first-class state. The
execution parks and holds until the reviewer's answer arrives, surviving
restarts and deployments.

**Why this matters here specifically.** The approval gate is a *control*. A
control implemented as a convention that a repair routine has to clean up after
is difficult to evidence to an examiner. A control implemented as a state
machine has an execution history showing who approved what and when.

**Gave up.** The orchestration logic now lives in two places conceptually — the
canvas the analyst draws and the state machine it compiles into. That is
managed by generating the second from the first.

### 6.5 Compile in the web tier, deploy from somewhere else

**Problem.** Saving a workflow needs to produce a runnable state machine. The
obvious implementation gives the web tier permission to create AWS resources.

**Chose.** Split it. Saving *compiles* the canvas and registers a versioned
plan; a separate privileged process deploys pending plans.

**Why.** The web tier is the internet-facing component. It should not hold
permission to create infrastructure, because that permission is what an
attacker who compromises it inherits. A broken diagram is still rejected
immediately, while the analyst is looking at it — the user experience does not
suffer for the security property.

**Bonus.** Every compiled version is retained rather than overwritten, with its
hash and timestamp. That is the audit answer to "which calculation produced the
number we filed?"

### 6.6 Accepting a performance cost, then paying it back

**Problem.** After 6.1, a page that previously did twenty instant memory
lookups now made twenty network calls. Validating a canvas of eleven steps cost
twenty-one round trips for three distinct records.

**Chose.** Remember records for the duration of a single request. Twenty-one
round trips became two.

**Why scoped to one request.** A longer-lived cache introduces staleness — one
server showing an old version of a model another server just updated. Scoped to
a request, the data is internally consistent for that operation and fresh on the
next one.

**Gave up.** Within one long-running operation, the data is a snapshot. For a
workflow run that is arguably the property you want: the run should not observe
a model changing underneath it halfway through.

### 6.7 Designing for the query, not the storage

**Problem.** Listing an analyst's recent runs meant reading *every run ever
recorded* and sorting them, because the records were organised by identifier —
which sorts meaninglessly. Cost grew forever.

**Chose.** A secondary index organising the same records by function and time,
so "the newest fifty" is a direct range read that stops after fifty.

**Result.** The cost of a page no longer depends on how many runs exist. Ten
runs or ten million, the same.

**What it did not fix, stated plainly.** Storage still grows without bound.
Read cost and retention are separate problems and only the first is solved. The
second is a policy question — see 7.4.

### 6.8 Keeping the record when the payload will not fit

**Problem.** DynamoDB caps a single record at 400 KB. A run carries its full
result set, so an unusually large run exceeds it, and the write fails *after*
the computation has already succeeded.

**Options.** Fail the request; silently truncate; or store the result elsewhere
and link to it.

**Chose.** Keep the run record, drop the row-level detail, and annotate it with
how many rows were omitted. The user still receives their complete results;
only the historical copy is trimmed.

**Why.** Failing the request would discard work that succeeded. Silently
truncating would produce a record that lies. Storing the payload in S3 is the
*right* long-term answer and is the next step if large runs become common —
this is a deliberate, documented interim.

### 6.9 Records are durable; so are the files

**Problem.** Moving the records to a database left the *files* — uploaded
datasets, model artifacts — on the local disk of whichever server received the
upload. This is worse than an obvious failure: the record appears everywhere,
so the dataset shows in every listing, and then reading it fails on every
server except one.

**Chose.** Uploads write to S3 as well as local disk, and any server missing a
file fetches it on demand and keeps a local copy.

**Why a cache and not direct-from-S3 every time.** Model files are read
repeatedly during a run. A local copy after the first read keeps S3 out of the
inner loop.

**Detail worth noting.** Model artifacts depend on companion files to load at
all. Fetching the model without them fails with an error naming a missing
*class*, not a missing *file* — a confusing way to discover the problem. The
fetch brings the companions along.

### 6.10 Measuring an upgrade, and shipping it turned off

**Problem.** The document search behind the AI agents used keyword matching.
The obvious upgrade is semantic search using embeddings.

**What happened.** It was built, measured against a test set — and it *lost*.
Keyword matching found the right document first every time; the semantic
version managed three out of five.

**Why.** Every document in the corpus describes deposit balance modelling, so
every passage resembles every other and semantic similarity has almost nothing
to separate. Meanwhile analysts search using the corpus's own vocabulary, which
is precisely where keyword matching is strongest.

**Chose.** Ship it switched off, one setting away, with the condition for
revisiting written down: a larger, more varied corpus. Which is exactly the
regulatory-filings case, where many agencies across decades describe the same
concepts in different words.

**The honest caveat.** Five queries, written by the same person who chose the
expected answers. A defensible benchmark needs analyst-written queries with
blind relevance labels. The measurement was enough to decline an upgrade, not
enough to call the question settled.

### 6.11 Lambda now; Fargate when the constraints change

**Chose.** Lambda for computation. Runs are short, bursty, and event-driven,
and Lambda costs nothing when idle.

**When Fargate becomes right**, and none of these is true yet:

- a single run exceeds fifteen minutes, Lambda's hard limit
- the container image exceeds ten gigabytes
- a model needs more memory than Lambda provides, or a GPU
- cold-start latency stops being acceptable

**Why the migration is contained.** Step Functions invokes container tasks with
the same pattern it invokes Lambda, so the state machine design survives the
change.

---

## 7. Meeting a regulator's expectations

The design choices above were made with these questions in mind.

### 7.1 Can you reproduce a filed number?

Every workflow version compiles to an immutable plan, retained with its hash
and timestamp — never overwritten, and not deleted when the workflow is. Every
run is recorded with its inputs, outputs, and the scenario context it ran under.
Model artifacts are versioned in S3.

### 7.2 Who approved this, and can you prove it?

The approval gate is a state machine state, not a convention. The execution
history is the evidence, produced automatically rather than assembled after the
fact. It also enforces a real timeout — an approval nobody grants within
twenty-four hours fails explicitly, rather than a run sitting in limbo.

### 7.3 What can each component do if compromised?

Permissions are scoped to intent, not convenience. The compute worker can write
results and read model files — not the document corpus, which lives in the same
bucket under a different prefix. The internet-facing web tier holds no
permission to create infrastructure at all. Access is by role, never by stored
credentials.

### 7.4 What is your retention policy?

**This is the open item, and it is a policy question rather than a technical
one.** Run history currently grows without bound. Two viable answers: automatic
expiry after a defined period, or archival to cheaper storage with nothing ever
deleted. The right choice depends on whether a run is *evidence* — in which case
archival is the only acceptable answer — or working scratch. Given these numbers
feed regulatory filings, archival is the likely answer, and the versioned
document bucket is the established pattern to follow.

Two related gaps, stated rather than hidden: the records database has no
point-in-time backup enabled, and its deletion policy would remove it with the
stack. Both are appropriate for a lab and both must change before anything in
it is treated as a record.

### 7.5 Where does the data live?

One region, deliberately. Nothing is replicated across regions, so there is no
question about where a given record resides. Data residency requirements are met
by choosing the region, not by configuring replication.

---

## 8. Built versus designed

| Capability | Status |
|---|---|
| Account guardrails — MFA, budgets, audit logging | **Built** |
| AI model access via managed service | **Built** |
| Document storage in S3, versioned | **Built** |
| Semantic document search | **Built, measured, deliberately off** |
| Computation off the request path — queue + worker | **Built** |
| Multi-step runs with a durable approval gate | **Built** |
| Canvas compiled to a runnable state machine | **Built** |
| Application records in DynamoDB | **Built and deployed** |
| Time-ordered index for run history | **Built and deployed** |
| Uploaded files in S3, servers fetch on demand | **Built** |
| Identity with stateless tokens | **Built and deployed** |
| **Web tier on containers behind a load balancer** | **Designed, not built** |
| **Static frontend on a CDN** | **Designed, not built** |
| Retention policy for run history | **Open decision** |

The line matters. Everything above it has been deployed and verified against
real AWS services. Everything below it is a drawing.

---

## 9. Why this order

The sequence was not arbitrary, and the reasoning is itself an answer to how to
approach a migration.

**Guardrails first.** Budgets, audit logging, and least-privilege access before
the first real API call — because retrofitting those is how cloud programmes
acquire findings.

**Then the things that make the application honest** — real model access, real
document storage — each independently demonstrable.

**Then computation off the request path**, because that removed a user-visible
failure mode without touching how the application stored anything.

**Then state**, in dependency order. Records first, then the performance cost
that created, then the files, then identity. Each step was verifiable before
the next began.

**Hosting last**, and deliberately so. Running the web tier on multiple servers
before its state left the process would have produced servers that disagree
with each other. The unglamorous half had to go first — and having finished it,
the remaining work is configuration rather than redesign.

---

## 10. Cost

The deployed footprint runs at effectively zero:

- **DynamoDB** — on-demand billing, within the always-free allowance
- **Lambda, SQS, Step Functions** — within permanently-free tiers at this volume
- **S3** — pennies for the corpus and artifacts
- **Cognito** — free below fifty thousand monthly active users
- **ECR** — one image retained, which is what keeps it inside the free allowance

The one deliberate trade: keeping a single container image means there is no
previous image to roll back to. Recovery is rebuilding from a known-good source
tree, which is acceptable because the image tag *is* a hash of that tree.
Retaining two images costs a few cents a month and buys instant rollback — a
reasonable thing to change, and a decision rather than an oversight.

The pattern worth naming: every service here bills on use, not on standing
capacity. That was a selection criterion, not a happy accident.

---

## 11. Questions I expect

**"Walk me through a system you designed."**

Lead with the diagnosis, not the diagram. This system could not run on more
than one server, and the reason was not how it was deployed — it was that the
application held its state in memory. That distinction determined everything
else: it meant the fix was a data-design change, and it meant hosting had to
come last rather than first.

**"How would you make this highly available?"**

Be specific about what was actually blocking it. Four record types moved to a
managed database, uploaded files moved to object storage, and sessions became
signed tokens that need no shared lookup. Then be honest about what that cost —
reads became network calls, which needed a per-request cache to pay back — and
what remains: the web tier still runs as a single process, and that step is now
configuration rather than redesign.

**"How do you handle a long-running human approval?"**

A durable state machine, not application code. Explain why: code that waits
in-process needs a repair routine to clean up after restarts, and a control that
needs cleaning up after is hard to evidence to an examiner.

**"How would you build search over thirty years of regulatory filings?"**

The interesting part is not similarity search — it is ingestion and trust:
splitting documents along meaningful boundaries, attaching metadata that can be
filtered, citing sources so an answer can be audited, and choosing a vector
store whose cost model matches an access pattern that is bursty and small.
Mention the measurement that said keyword search won on *this* corpus, and why
a heterogeneous filings corpus is the case where that flips.

**"When would you not use an AI model?"**

For the numerical optimisation at the core of this platform. It is a
deterministic calculation with a correct answer, it must be reproducible for a
regulator, and a language model is worse at it in every dimension that matters.
The agents explain results and help build pipelines; they do not compute the
numbers.

**"What would you do differently?"**

Two things. The retention policy should have been decided before run history
was stored rather than after — it is a policy question that constrains the data
model, and answering it late risks a migration. And the semantic search work
should have started with the evaluation set rather than the implementation;
building it first meant discovering it lost after the effort was spent, when a
day defining the benchmark would have found the same answer sooner.

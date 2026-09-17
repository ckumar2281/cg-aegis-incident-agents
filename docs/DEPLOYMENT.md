# Deploying Aegis to AWS

What changes between `python -m aegis.cli` on a laptop and a system that runs
unattended against a live warehouse.

This is a design note, not a claim: **none of it is deployed.** The POC runs locally
against real Bedrock and a simulated platform. What follows is the honest shape of the
production path, including the one part that is a genuine architectural change rather
than packaging.

---

## 1. What already survives the move

Most of the system does not care where it runs. The agent graph, the contracts, the
governance policy, the hash chain and the cost governor are all pure Python over
interfaces. Three seams were built for this — though only one of them currently has a
real implementation behind it (Bedrock). The others have the seam and the mock; the
production adapter is written for the integrations and **not yet written** for
Snowflake.

| Seam | Local today | Deployed |
|---|---|---|
| `PlatformClient` | `SimulatedPlatform` over a seeded world | `SnowflakePlatform` over `INFORMATION_SCHEMA` + `ACCOUNT_USAGE` — **written, unverified**; run `scripts/check_snowflake.py` |
| `ModelClient` | Bedrock via boto3, or the heuristic backend | Unchanged — Bedrock either way |
| `EmailSink` / `TicketSink` / `VcsClient` | Mock adapters | SES, Jira/ServiceNow, GitHub |
| `Responder` | `ScriptedResponder` / `ConsoleResponder` | `PendingResponder` + a callback endpoint |

The last row is the one that is not just configuration. See §4.

---

## 2. The container

AgentCore Runtime takes an OCI image and expects a specific contract. Following AWS's
own multi-agent SRE reference implementation:

- **ARM64** — `--platform=linux/arm64`, not optional
- **Python 3.12**
- An HTTP server on **port 8080**
- OpenTelemetry instrumentation in the start command, which is what populates
  AgentCore Observability in CloudWatch

```dockerfile
FROM --platform=linux/arm64 ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY aegis/ ./aegis/

EXPOSE 8080
CMD ["uv", "run", "opentelemetry-instrument", \
     "uvicorn", "aegis.runtime:app", "--host", "0.0.0.0", "--port", "8080"]
```

The only new code is `aegis/runtime.py` — a thin FastAPI wrapper that accepts an
incident payload, calls `run_incident`, and returns the `IncidentOutcome`. Perhaps
sixty lines. Nothing in the agent layer changes.

```python
response = client.create_agent_runtime(
    agentRuntimeName="aegis",
    agentRuntimeArtifact={"containerConfiguration": {"containerUri": ecr_uri}},
    networkConfiguration={"networkMode": "PUBLIC"},
    roleArn=role_arn,
    environmentVariables={"AEGIS_PLATFORM": "snowflake", "AEGIS_MODEL_BACKEND": "bedrock"},
)
```

Invocation carries a session id, which is what makes a suspended incident resumable:

```python
client.invoke_agent_runtime(
    agentRuntimeArn=runtime_arn,
    runtimeSessionId=incident_id,
    payload=json.dumps({"input": {"alerts": [...], "file": {...}}}),
)
```

---

## 3. What triggers it

Today a scenario is loaded from a fixture. In production the trigger is the platform
itself:

```
Snowpipe / dbt test failure
Snowflake alert  ──▶  SNS topic  ──▶  Lambda  ──▶  invoke_agent_runtime
DMF threshold breach                              (runtimeSessionId = incident id)
Freshness monitor
```

The Lambda does no thinking. It normalises the alert into the `Alert` contract and
invokes the runtime. Triage does the correlation, which is deliberate: alert
de-duplication needs lineage, and lineage lives in the agent layer.

**Containment stays outside the agents.** The S3 quarantine move is a bucket
notification plus a Lambda, and it must keep working whether or not the agent system is
healthy. An incident-response system that becomes a dependency of your data safety has
the relationship the wrong way round.

---

## 4. The real change: approvals become asynchronous

Everything above is packaging. This is architecture.

Today the `ApprovalCoordinator` asks a `Responder` and gets an answer in the same call
stack. That is honest for a demo and wrong for production, where a Product Owner might
answer in four hours or not at all.

The production shape:

```
plan → disclose → send approval emails → SUSPEND the graph
                                              │
                        (hours pass; the container is not running)
                                              │
   PO clicks a signed link → API Gateway → Lambda → verify token
                                              │
                                     RESUME the graph from the gate node
```

Three things already exist for this:

- **`PendingResponder`** returns `None` for everyone, which resolves every gate as
  `TIMEOUT`. That is the correct suspend semantics — a timeout escalates, it never
  silently approves.
- **Signed, single-use, expiring tokens.** `TokenMinter` already mints an HMAC over
  incident, gate, role and expiry, and burns the token on use. The callback Lambda
  validates with the same code.
- **The audit chain** is already append-only and serialisable, so incident state can be
  rebuilt.

Two things do not:

- **Graph checkpointing.** LangGraph supports persistent checkpointers; Aegis currently
  compiles without one because every run completes in-process. Production needs a
  checkpointer (DynamoDB or Postgres) keyed on `incident_id` so `business_gate` can be
  resumed rather than replayed.
- **The callback endpoint.** API Gateway + Lambda: validate the token, record the
  verdict, resume the graph. Small, but it is the piece that turns the demo into a
  system.

**Why this is not hand-waving:** the gate nodes already return a `GateOutcome` and route
on it. Suspending means persisting state at that node instead of calling a responder
inline. The graph topology does not change at all.

---

## 5. AgentCore components, and which are worth it

| Component | Use it? | Why |
|---|---|---|
| **Runtime** | Yes | Serverless, session-isolated, scales from zero. The natural home |
| **Observability** | Yes | OTel traces into CloudWatch. Aegis already emits a structured `TraceBus`; this is where it lands |
| **Identity** | Yes | Cognito JWT for the approval callback. Approval links must be authenticated, not merely unguessable |
| **Gateway** | Probably not | Turns OpenAPI specs into MCP tools. Aegis's tools are a typed Python layer over Snowflake and S3, not HTTP APIs. Gateway would add a hop and a JWT dance to reach a database it can already reach. Worth it only if the tools become services shared with other agents |
| **Memory** | Partially | See below |
| **Code Interpreter** | No | Nothing here needs sandboxed code execution |

### On Memory specifically

AgentCore Memory offers namespace-routed strategies — user preferences, domain
knowledge, session summaries. Aegis has two stores that look superficially similar and
are not:

- **`IncidentMemory`** — past incidents for recurrence detection. A reasonable fit for
  AgentCore Memory, and it would survive container restarts, which the in-process
  version does not.
- **`PrecedentStore`** — standing approvals. **This should not go in agent memory.** A
  precedent is a governance record: it authorises action, it is legally interesting, and
  it must be queryable, auditable and revocable by a human who will never look at an
  agent memory namespace. It belongs in Snowflake next to the audit chain, with a
  proper table and a review surface.

That distinction is worth stating plainly because the two stores are the same shape and
it would be easy to put both in the convenient place.

---

## 6. Where state lives, and why it is split

Aegis holds five kinds of state. The temptation is to put all of it in Snowflake,
since Snowflake is already there — and for two of the five that is exactly right. For
the other three it is wrong, for reasons worth setting out because "we already have a
database" is a good instinct that leads somewhere bad here.

| State | Deployed home | Why there |
|---|---|---|
| **Audit chain** | `AEGIS.OPS.INCIDENT_AUDIT` in Snowflake | Governance record, read by humans, joined against incident and asset data, retained for years. Analytical workload — what a warehouse is for |
| **Precedents** | `AEGIS.OPS.PRECEDENTS` in Snowflake | Standing approvals are governance records too: a PO must be able to list, review and revoke them |
| **Incident memory** | Snowflake, or AgentCore Memory | Recurrence detection reads across incident history — again analytical, and it should survive container restarts |
| **Graph checkpoints** | **Not Snowflake.** DynamoDB, or Postgres on RDS | See below |
| **Backlog** | Jira | A backlog already has a home; do not build a second one |

`AuditChain.to_rows()` already emits the Snowflake column shape. That was not an accident.

### Why graph checkpoints do not go in Snowflake

Three reasons, and only the third decides it.

**The access pattern is wrong.** A checkpointer does point lookups — *"give me the state
for incident X"* — several times per incident. Snowflake is a columnar analytical
warehouse: excellent at scanning millions of rows, mediocre at fetching one by key. That
is roughly 100–500ms and a warehouse spin-up for work a key-value store does in single
digits of milliseconds.

**The cost model is wrong.** Every Snowflake query needs a running warehouse. Frequent
small writes keep it awake, so `AUTO_SUSPEND = 60` never fires and you pay
compute-by-the-second to do key-value work. DynamoDB on-demand bills per request.

**The dependency is backwards — this is the real reason.** If checkpoints live in
Snowflake, then *Snowflake being unavailable means suspended incidents cannot be
resumed.* Snowflake being unavailable is precisely when incidents happen. An
incident-response system that depends on the platform it monitors will fail at exactly
the moment it is needed.

That is the same principle applied to quarantine in §3: containment sits outside the
agents because it must keep working whether or not the agent layer is healthy.

**Honest concession:** at tens of incidents a day, Snowflake checkpointing would work
*functionally*. The performance and cost arguments are real but not painful at that
scale. It is the dependency argument that decides it — and unlike the other two, that one
gets worse as the system matters more, not better.

DynamoDB is not special here. The requirement is "a fast key-value store that is not the
warehouse". Postgres on RDS satisfies it equally well, and LangGraph ships checkpointers
for both.

---

## 6b. AWS services required

**For the POC as it stands: only Bedrock.** It runs on a laptop against a simulated
warehouse, with no standing infrastructure and no idle cost. Worth stating plainly,
because "what does it cost to run" has a good answer: $0.13 per incident and nothing
between runs.

For a deployed version, roughly in order of necessity:

| Service | Role | Needed when |
|---|---|---|
| **Bedrock** | The reasoning layer | Already wired |
| **S3** | Raw landing zone and quarantine bucket | First real deployment |
| **Lambda** | Three small ones: normalise an alert and invoke the agent; move a failed file to quarantine; handle an approval-link click | First real deployment |
| **EventBridge** | Routes Snowflake alerts and S3 events to those Lambdas | First real deployment |
| **API Gateway** | The endpoint approval links hit | When approvals go asynchronous |
| **SES** | Sends the approval emails; adapter already written | When approvals go real |
| **DynamoDB** *(or RDS Postgres)* | LangGraph checkpointer — see §6 | When approvals go asynchronous |
| **ECR + AgentCore Runtime** | Image registry and serverless host | When it stops running locally |
| **Secrets Manager** | Snowflake, Jira and GitHub credentials | When integrations go real |
| **CloudWatch** | Where AgentCore Observability lands the traces | Arrives with Runtime |

**EventBridge rather than SNS**, because routing here is content-based: a DMF breach on a
tier-1 asset should be able to take a different path from a freshness warning on a tier-3
one. SNS fans out to everything and leaves the filtering to the consumer; EventBridge
rules express that intent where it can be read.

### Glue is not needed

Glue is an ETL service — catalog, crawlers, Spark jobs. **Snowflake is already the
warehouse and the transformation engine here.** Adding Glue would introduce a second
catalog competing with Snowflake's own and a second place lineage lives, which is
precisely the ambiguity an incident system must not have. Aegis reads lineage from
Snowflake Horizon.

If Glue already runs *upstream* of Snowflake in your estate, Aegis treats those jobs as
another change source to correlate against — the `ChangeEvent` contract handles that
without modification. But it is not a dependency.

### The trimmed minimum

**S3 + EventBridge + 3 Lambdas + API Gateway + DynamoDB + Bedrock.**

Everything else is either bundled (CloudWatch arrives with Runtime) or an integration
that can stay mocked until it is worth wiring.

### And three to avoid

All one click away in the Bedrock console, all billing while idle:

- **Knowledge Bases** — the default OpenSearch Serverless vector store has a 2-OCU
  minimum, roughly **$345/month at zero queries**. Aegis deliberately uses an in-process
  similarity function instead
- **Provisioned Throughput** — hourly commitment regardless of traffic
- **AgentCore Runtime sessions left open** — memory bills per second *including idle*.
  A specific hazard for a system that waits on human approval, and the reason the
  approval flow must suspend rather than wait (§4, §7)

---

## 7. Cost at production volume

Measured live: **$0.16 per incident** on the current model pair (22 calls across two
investigation rounds, ~119s wall clock).

| Volume | Bedrock | AgentCore Runtime |
|---|---|---|
| 10 incidents/day | ~$40/month | scales from zero; cost dominated by session duration |
| 50 incidents/day | ~$200/month | as above |

Two warnings that apply specifically to a deployed agent system:

**AgentCore Runtime bills session memory per second while a session is alive, including
idle.** The default idle timeout is 15 minutes. An incident that suspends waiting for a
Product Owner must **not** hold a live session for four hours — which is another reason
the approval flow has to be genuinely asynchronous rather than a long-running wait. Set
`idleRuntimeSessionTimeout` deliberately.

**The per-incident budget governor matters more, not less, in production.** Locally a
runaway loop costs cents. At scale it costs attention and money. The ceiling and the
honest `degraded_reasoning` marker should stay exactly as they are.

---

## 8. What I would not move

The deterministic core stays in the container and stays deterministic: severity scoring,
hypothesis arithmetic, the redaction firewall, the autonomy ladder, precedent matching.

These are the parts a reviewer or an auditor needs to be able to reproduce without
re-running a model, and moving them behind a managed service — or letting a model do
them because a managed service makes that convenient — would trade the system's main
property for marginal convenience.

---

## 9. Order of work

1. **Write `SnowflakePlatform`** and verify it against a trial account. The protocol
   exists and the agents call only through it, so nothing above this layer changes — but
   the implementation itself does not exist yet
2. `aegis/runtime.py` FastAPI wrapper + Dockerfile + push to ECR
3. Graph checkpointer, then the approval callback Lambda — the two together are what
   make the approval chain real
4. Audit chain and precedents persisted to Snowflake
5. SNS/Lambda trigger from Snowflake alerts
6. AgentCore Runtime deploy, Observability on, `idleRuntimeSessionTimeout` tuned

Steps 1–2 are a day. Step 3 is the interesting one and worth doing carefully, because
an approval system that loses a pending decision on restart is worse than no approval
system.

---

## Sources

- [Build multi-agent SRE assistants with Amazon Bedrock AgentCore](https://aws.amazon.com/blogs/machine-learning/build-multi-agent-site-reliability-engineering-assistants-with-amazon-bedrock-agentcore/) — the reference implementation this container contract follows
- [AgentCore Runtime: get started without the CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html)
- [AgentCore starter toolkit](https://aws.github.io/bedrock-agentcore-starter-toolkit/examples/agentcore-quickstart-example.html)
- [Inference profile prerequisites and IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)

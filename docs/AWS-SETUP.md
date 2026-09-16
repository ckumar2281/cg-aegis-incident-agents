# Aegis POC — AWS Bedrock Setup Record

**Project:** Agentic DataPOC — "Aegis", governed agentic incident management for data pipelines
**Owner:** Chaitanya
**Date:** 16 September 2026
**Review:** Friday 18 September 2026 · **Deliverable due:** end of day Thursday 17 September

---

## 1. The task

Build an **Agentic Incident Management solution**. Constraints set by the lead:

- **Architecture** must be multi-agent — specialised agents for triage, analysis and resolution — not a single monolithic call.
- **Evaluation** is on technical quality, effective agent hand-off logic, and innovation.
- **Tech stack** is our choice.

### Chosen problem statement

**Data pipeline / ETL incident management** — failed loads, schema drift, freshness SLA
breaches, and bad-data propagation across a lineage-connected warehouse.

### The governance model (Chaitanya's design)

The distinguishing idea is that **business approval gates technical disclosure**. The
pipeline is:

```
Source → Ingestion → Validation → Failure/Quarantine → Agent → Diagnosis
  → Recommended Action → Human Approval (tiered) → Remediation/Rerun
  → Audit & Notification
```

Approval runs as two sequential gates:

| Gate | Approvers | Sees | Outcomes |
|---|---|---|---|
| **1. Business** | Product Owner + Scrum Master | Business brief only — no code, no object names, no errors | Approve → unlocks Gate 2 · Reject → quarantine + ticket · Defer → future-issues backlog |
| **2. Technical** | Developer + Engineering Manager | Full technical fix packet, root cause, diffs, rollback | Approve → implement, open PR, reprocess data |

The developer never receives the fix detail until the business has signed off on the
problem in business terms. Three terminal states: **resolved**, **rejected and
quarantined** (ticket raised with pipeline owner notified), or **deferred to backlog**.

---

## 2. Stack decisions

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **LangGraph** supervisor + specialist agents | Explicit graph, conditional edges, confidence-gated loops, human-in-the-loop interrupts. Matches AWS's own multi-agent SRE reference architecture |
| Reasoning | **Amazon Bedrock** (Claude) via boto3 Converse API | Required by POC brief |
| Data platform | **Snowflake** | Real incident signals: `TASK_HISTORY`, `COPY_HISTORY`, Data Metric Functions, Horizon lineage |
| Storage | **S3** raw + quarantine zones | File landing and fail-safe quarantine |
| Contracts | **Pydantic v2** | Typed hand-off contracts between agents |
| Deployment | Runs **locally** against real Bedrock + Snowflake | AgentCore Runtime documented as the production path, not deployed for the POC |

---

## 3. AWS Bedrock setup — what was done

### 3.1 Region

**`us-east-2` (Ohio).** Model access in Bedrock is granted **per region**, so everything
must stay consistent with this choice. Cross-region inference profiles (the `us.`
prefixed model IDs) are supported here.

### 3.2 Model access

The standalone "Model access" console page **no longer exists** in the current Bedrock
console. The flow is now:

1. Bedrock console → **Discover → Model catalog**
2. Filter **Providers → Anthropic**
3. Open **Claude Haiku 4.5**
4. Yellow banner → **Submit use case details**
5. Complete the form — **access granted immediately**, no approval queue

**The use-case form is submitted once per AWS account**, not per model. Submitting it
for Haiku unlocked all Anthropic models on the account.

Form values used:

| Field | Value |
|---|---|
| Intended users | **Internal users** only |
| Use case | Internal proof-of-concept for automated data pipeline incident management. A multi-agent system triages data quality alerts, correlates them using warehouse lineage metadata, performs root-cause analysis, and drafts remediation plans that are reviewed and approved by humans before any action is taken. Internal engineering use only; no customer-facing deployment and no end-user access to the models. |

### 3.3 Authentication

**Bedrock API key (long-term, 30-day expiry).**

Created at Bedrock console → **Discover → API keys**.

Chosen over an IAM user because:

- AWS provisions the backing permissions automatically — no IAM policy to author
- Scoped to Bedrock only, so it cannot touch anything else in the account
- Self-expiring, so no permanent credential is left behind

**Short-term keys were rejected** — they expire in ~12 hours and would be dead before
the Friday review.

**"Permissions to access Amazon Bedrock Marketplace models" was left unchecked.**
Marketplace models run on dedicated hourly-billed endpoints; Claude is a serverless
foundation model and does not use that path.

### 3.4 Environment variables

```bash
export AWS_BEARER_TOKEN_BEDROCK='<the API key>'
export AWS_REGION=us-east-2
```

`AWS_REGION` is **required** with an API key. The key carries no AWS profile, so
without it the SDK defaults to `us-east-1` and reports "no models found" — models are
enabled in Ohio.

### 3.5 Local Python environment

```bash
cd ~/Downloads
python3 -m venv aegis-venv
source aegis-venv/bin/activate
pip install boto3
python check_bedrock.py
```

**Known issue encountered:** the first attempt used `pip install --user boto3`, which
upgraded `botocore` from 1.34.69 to 1.43.95 system-wide and broke a pin held by
`aiobotocore 2.12.3` (`botocore<1.34.70`).

- Impact: warning only — boto3 installed and works. Any other local project using
  `aiobotocore` may now fail.
- **Not remediated** (deliberately deferred).
- If it becomes a problem:
  ```bash
  deactivate
  python3 -m pip uninstall -y boto3 botocore s3transfer
  python3 -m pip install --user 'botocore<1.34.70'
  ```
  The venv keeps its own copies and is unaffected either way.

**Lesson:** use a venv from the start; `--user` installs mutate shared state.

---

## 4. Cost position

### What is billed

| Item | Cost |
|---|---|
| Enabling model access | **$0** — no subscription, no reservation, no idle charge |
| IAM users, policies, API keys, budget alarms | **$0** |
| Bedrock inference | **~$0.10 per incident run** on the mixed tier; ~$0.004 with Nova Lite throughout |
| Snowflake | **$0** — 30-day trial, $400 credits; POC needs ~2–5 credits on an XS warehouse |
| S3, SES, Lambda, API Gateway | **~$0** at demo volume |
| AgentCore Runtime | **$0** — running locally |

**Estimated total for the POC: under $15.** All development, testing and the eval
suite run on the deterministic heuristic backend at **$0** — Bedrock tokens are spent
only on real demo runs.

### Bedrock pricing used (verify before relying on it)

Per million tokens, on-demand, as of September 2026:

| Model | Input | Output |
|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 |
| Claude Sonnet 5 | $2.00 | $10.00 |
| Claude Sonnet 4.6 | $3.00 | $15.00 |
| Amazon Nova Lite | $0.06 | $0.24 |
| Amazon Nova Pro | $0.80 | $3.20 |

Cached input tokens bill at roughly **10%** of the base input rate. Batch inference is
**50%** off. Published guidance warns real bills run **1.5–2×** naive estimates once
retries and ancillary services are counted.

These figures live in `aegis/config.py:MODEL_PRICES` and drive real spend accounting,
not decoration.

### Idle-cost traps — deliberately avoided

| Trap | Idle cost | Status |
|---|---|---|
| **Knowledge Bases** (default OpenSearch Serverless vector store) | 2 OCU minimum at $0.24/OCU/hr ≈ **$345/month at zero queries** | ❌ Not used. Incident memory is an in-process similarity function instead |
| **Provisioned Throughput** | Hourly commitment regardless of use | ❌ Not purchased |
| **AgentCore Runtime sessions** | Session memory billed per second including idle, 15-min default timeout | ❌ Not deployed |
| **Bedrock Marketplace model deployments** | Hourly endpoint charges | ❌ Permission not granted |
| **Snowflake warehouse left running** | ~1 credit/hour | ⚠️ Mitigate with `AUTO_SUSPEND = 60`, `INITIALLY_SUSPENDED = TRUE` |

### Recommended guardrail

**Billing → Budgets → Create budget → Cost budget → $20/month**, filtered to
service = Bedrock, alert at 50%. First two budgets are free.

The per-incident budget governor in the code is the first line of defence; the AWS
budget alarm is the one that does not depend on application code being correct.

---

## 5. Cost controls built into the architecture

1. **Model tiering per agent role** — the four forensic specialists run on the cheap
   model; RCA, planning and disclosure get the strong model. One config map is the
   difference between a ~$0.10 incident and a ~$0.25 one.
2. **Per-incident budget governor** — hard USD and call ceilings. On breach the
   remaining agents degrade to the deterministic backend and the incident is stamped
   `degraded_reasoning` in the audit trail. Degraded but honest beats accurate but
   unbounded.
3. **Evidence compaction** — agents never see raw logs. Tools return pre-aggregated
   structures ("row count is 3.1× median"), which is cheaper *and* stops the model
   doing arithmetic it is bad at.
4. **Prompt caching** on static system prompts.
5. **Bounded re-investigation** — maximum 2 rounds, hard stop.
6. **Heuristic backend as the default** for development, CI and evals — $0.

---

## 6. Build status

### Complete

| Component | File |
|---|---|
| Typed hand-off contracts | `aegis/contracts.py` |
| Config + cost governor | `aegis/config.py` |
| Redaction firewall + autonomy ladder + severity scoring | `aegis/policy.py` |
| Hash-chained audit trail + trace bus | `aegis/audit.py` |
| Bedrock / heuristic reasoning layer | `aegis/reasoning.py` |
| Simulated Snowflake warehouse (33 assets, lineage) | `aegis/platform/world.py` |
| Five seeded incidents with ground truth | `aegis/platform/scenarios.py` |
| Platform client + action API | `aegis/platform/client.py` |
| Provenance-recording tool belt | `aegis/tools.py` |
| Incident memory / recurrence detection | `aegis/memory.py` |
| Triage agent | `aegis/agents/triage.py` |
| Four forensic specialists | `aegis/agents/forensics.py` |
| RCA synthesiser with confidence gate | `aegis/agents/rca.py` |
| Remediation planner with playbooks | `aegis/agents/planner.py` |
| Tiered disclosure officer | `aegis/agents/disclosure.py` |
| Bedrock preflight script | `scripts/check_bedrock.py` |

≈7,100 lines.

### Remaining

- Two-gate approval state machine with signed tokens and timeout escalation
- Executor / verifier with rollback
- Integrations: Jira / ServiceNow ticketing, GitHub PR creation, SES email
- LangGraph orchestration wiring
- CLI demo runner + HTML trace report
- Eval harness scoring against ground truth
- Architecture writeup and AgentCore deployment notes
- Git repo and GitHub push

---

## 7. Seeded incident scenarios

| Key | Tests | Terminal state |
|---|---|---|
| `schema_drift` | Full happy path — Stripe renames a column, both gates approve, PR raised, quarantined file replayed | Resolved |
| `join_fanout` | Silent corruption — nothing failed, a merged PR fanned rows out 3× | Resolved |
| `vendor_outage` | Alert-storm de-duplication — 10 alerts across 9 assets fold into 1 incident | Resolved |
| `null_explosion` | **Reject path** — business declines, file stays quarantined, ticket raised | Rejected / quarantined |
| `chronic_lateness` | **Defer path** — third occurrence in 30 days, recurrence detected, parked as backlog item | Deferred to backlog |

---

## 8. Open items

### Needs a decision from Chaitanya

1. **Repo name** — currently `aegis`.
2. **Approver identities for the Friday demo.** Suggested: Gmail plus-addressing
   (`chaitan.gk+po@`, `+sm@`, `+dev@`, `+em@`) so all four land in one inbox but act
   as four distinct approvers — makes the tiered-disclosure effect visible live.

### Still to set up

| Service | Needed for | Status |
|---|---|---|
| Bedrock | Agent reasoning | ✅ Done |
| Snowflake | Real platform data | ⬜ Trial not started |
| GitHub | PR creation demo | ⬜ Repo + fine-grained PAT (`pull_requests: write`) |
| SES | Approval emails | ⬜ Verify one sender address |
| Jira | Ticket on reject path | ⬜ Optional — mock adapter demos the same flow |

### Immediate next step

Run the preflight and capture its output:

```bash
source ~/Downloads/aegis-venv/bin/activate
export AWS_BEARER_TOKEN_BEDROCK='<key>'
export AWS_REGION=us-east-2
python check_bedrock.py
```

The output names the exact model IDs this account can invoke. Those get pinned into
`aegis/config.py` along with verified pricing.

---

## 9. Reference links

- [Bedrock console](https://console.aws.amazon.com/bedrock/)
- [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
- [Inference profile prerequisites and IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)
- [Multi-agent SRE reference architecture on AgentCore](https://aws.amazon.com/blogs/machine-learning/build-multi-agent-site-reliability-engineering-assistants-with-amazon-bedrock-agentcore/)
- [Snowflake trial signup](https://signup.snowflake.com/)

---

## Appendix — production IAM policy (not used in the POC)

The API key made this unnecessary, but this is how access would be scoped properly in
production, and it is worth including in the writeup. Note that cross-region inference
profiles require permission on **both** the inference-profile ARN and the underlying
foundation-model ARN — a policy naming only the profile fails with a misleading
`AccessDenied`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeAndDiscover",
      "Effect": "Allow",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream",
        "bedrock:ListInferenceProfiles",
        "bedrock:GetInferenceProfile",
        "bedrock:ListFoundationModels"
      ],
      "Resource": [
        "arn:aws:bedrock:*:*:inference-profile/*",
        "arn:aws:bedrock:*::foundation-model/*"
      ]
    },
    {
      "Sid": "MarketplaceSubscribe",
      "Effect": "Allow",
      "Action": ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"],
      "Resource": "*",
      "Condition": {
        "StringEquals": { "aws:CalledViaLast": "bedrock.amazonaws.com" }
      }
    }
  ]
}
```

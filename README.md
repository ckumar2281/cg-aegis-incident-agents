# Aegis

**Governed agentic incident management for data pipelines.**

Aegis detects a data pipeline incident, investigates it with a team of specialised
agents, works out what actually broke, proposes a fix — and then refuses to touch
anything until the right humans have approved, in a specific order, having seen
specific things.

That ordering is the point of the system. And once a human has ruled on a situation,
it stops asking.

---

## The idea: business approval gates technical disclosure

Most incident automation optimises for speed: detect, diagnose, remediate, notify.
Aegis optimises for **defensible decisions**, because in a data platform the expensive
mistake is not being slow — it is confidently writing the wrong thing into a table
that finance closes the books from.

So the approval chain runs in an unusual order:

```
                    ┌─────────────────────────────────────┐
  file lands  ───▶  │ ingest → validate → QUARANTINE      │  fail-safe:
                    │ (bad data never reaches the         │  nothing bad
                    │  warehouse, no approval needed)     │  gets in
                    └──────────────────┬──────────────────┘
                                       ▼
                    ┌─────────────────────────────────────┐
                    │ triage → 4 forensic specialists →   │
                    │ RCA synthesis → remediation plan    │
                    └──────────────────┬──────────────────┘
                                       ▼
                    ┌─────────────────────────────────────┐
                    │ DISCLOSURE: one verdict, two        │
                    │ renderings, redaction firewall      │
                    └──────────────────┬──────────────────┘
                                       ▼
              ┌────────────────────────────────────────────────┐
              │ GATE 1 — BUSINESS                              │
              │ Product Owner                                  │
              │ see: plain-language brief ONLY                 │
              │      no code, no table names, no errors        │
              └───┬──────────────┬─────────────────┬───────────┘
            approve           reject            defer
                  │              │                 │
                  ▼              ▼                 ▼
    ┌─────────────────────┐  ┌────────────┐  ┌──────────────┐
    │ GATE 2 — TECHNICAL  │  │ stays      │  │ future-issues│
    │ Developer + Eng Mgr │  │ quarantined│  │ backlog with │
    │ see: full fix packet│  │ + Jira/SNOW│  │ high-level   │
    │      root cause,    │  │   ticket   │  │ change notes │
    │      diffs, rollback│  │ + owner    │  └──────────────┘
    └──────────┬──────────┘  │   notified │
          both approve       └────────────┘
               ▼
    ┌─────────────────────────────────────┐
    │ remediate data · open PR · verify   │
    │ · roll back on failure              │
    └─────────────────────────────────────┘
                     ▼
         hash-chained audit trail
```

**The developer does not receive the fix until the business has approved the problem.**

And not every incident asks. The autonomy ladder decides per incident from four inputs:
how risky the actions are, how large the code change is, how severe the incident is, and
**whether a human has already ruled on this exact case**. A SEV4 retry runs unsupervised.
A recurrence of a problem the Product Owner approved three weeks ago runs too — citing
their decision by name and date.

This means the business decision gets made on business grounds rather than deferred to
whoever understands the diff; an engineer cannot be leaned on to ship a fix the
business has not sanctioned, because it has not been sent to them yet; and the audit
trail can prove exactly what each audience saw, and when.

The firewall between the two tiers is **machine-enforced**, not a polite request in a
prompt. A business brief containing SQL, a diff, a stack trace, an internal object name
or anything resembling a raw record is rejected and regenerated. If it fails twice the
incident escalates to a human rather than over-disclosing. The system fails closed.

---

## Why multi-agent, and not one big prompt

Because the work genuinely decomposes, and the decomposition buys something measurable:

| Agent | Owns | Why separate |
|---|---|---|
| **Triage** | Correlate a noisy alert stream into one scoped incident | A vendor outage fires 10 alerts across 9 assets. Structural lineage correlation folds them into 1 |
| **Lineage analyst** | Who is hurt — downstream closure, consumers, SLAs | Drives severity and the business brief |
| **Pipeline forensics** | What the orchestrator did — runs, loads, errors, retries | Distinguishes "broken pipeline" from "pipeline faithfully loading bad data" |
| **Quality forensics** | How the data deviates — counts, nulls, duplicates, schema | Separates "data missing" from "data present but wrong" |
| **Change correlator** | What changed and when — deploys, config, vendor notices | Most data incidents are caused by a change; find it or rule it out |
| **RCA synthesiser** | Score competing hypotheses, decide confidence | Agreement between independent specialists is evidence; one prompt cannot agree with itself |
| **Remediation planner** | Risk-tiered data steps + a code fix for the PR | Data and code are repaired differently, on different clocks |
| **Disclosure officer** | One verdict → two audiences, redaction enforced | The governance core |
| **Executor / verifier** | Act, verify recovery, roll back on failure | Separation from planning means the plan is reviewable before anything runs |

Behind them sit three governance components that are not agents and deliberately not
model-driven: the **redaction firewall** (13 machine-checked rules), the **autonomy
ladder** (when humans are needed), and the **precedent store** (what a human already
decided).

The four specialists run in parallel and **deliberately overlap only a little**. When
the quality analyst and the change correlator reach the same conclusion from different
data, that agreement carries weight. A single call cannot corroborate itself.

---

## What makes the hand-offs work

Nothing crosses an agent boundary as free-form text. Every message is a validated
Pydantic contract in `aegis/contracts.py`.

**Directives, not context dumps.** Triage does not hand specialists a blob and hope. It
issues each one a numbered question it is accountable for answering. A report that
does not address its directives is rejected.

**Admitting ignorance is first-class.** `unresolved_directives` and
`follow_up_requests` are part of the schema. An agent saying "I could not establish
this, and here is who should look" triggers a second investigation round — which is far
cheaper than a confident wrong fix.

**The confidence gate.** RCA scores hypotheses arithmetically: best prior, plus an
agreement bonus for independent corroboration, plus supporting evidence, minus
refuting evidence at *double weight*. Refutation outweighs support deliberately —
"every task succeeded" should kill the infrastructure hypothesis outright. Below
threshold the investigation loops; far below, it escalates to a human.

**The model can doubt more easily than it can assert.** It may lower computed
confidence by up to 0.15 but raise it by at most 0.05. The asymmetry means the failure
mode is an extra round, not a wrong remediation.

**Provenance everywhere.** Every claim carries the tool calls that produced it. Every
state transition, disclosure and approval is appended to a SHA-256 hash chain, so
tampering with incident history is detectable.

---

## Running it

Zero credentials required. The whole system runs offline on a simulated Snowflake
warehouse with a deterministic reasoner:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m aegis.cli list                    # the scenarios
python -m aegis.cli run schema_drift        # one incident, live trace
python -m aegis.cli run --all --report      # whole suite + HTML trace report
python -m aegis.cli run null_explosion -i   # you play the Product Owner
python -m evals.harness                     # 118 checks against ground truth
python -m pytest tests/                     # 94 governance tests
```

Current state:

```
8/8 scenarios matched ground truth
118/118 eval checks passed · 0 governance violations
94 tests passed
```

To use real Claude models:

```bash
python scripts/check_bedrock.py             # discovers your model IDs
cp .env.example .env                        # set AEGIS_MODEL_BACKEND=bedrock
```

---

## The heuristic backend is not a stub

Every agent computes its structured answer in Python first, then optionally lets the
model improve it. That fallback earns its place three times over:

- **Cost.** Development, tests and the eval suite run on it for $0. Bedrock tokens are
  spent only on real demo runs.
- **Determinism.** An agentic system whose behaviour changes every run is untestable.
  The fallback gives byte-identical output, so orchestration regressions are visible
  rather than lost in model variance.
- **Resilience.** It is also the failure path — throttling, malformed JSON, or the
  budget ceiling all degrade to it rather than failing the incident, and the
  degradation is stamped into the audit trail.

The tool layer supplies facts; the model supplies judgement and language. On the
heuristic path the facts are unchanged and the judgement is rule-based.

---

## Cost control is part of the architecture

A fan-out agent graph is an excellent way to spend money by accident. Aegis prices
every call as it makes it.

- **Model tiering per role** — specialists on the cheap model, synthesis and disclosure
  on the strong one. One config map: ~$0.10 per incident instead of ~$0.25.
- **Per-incident budget governor** — hard USD and call ceilings; on breach the
  remaining agents degrade and the incident is marked `degraded_reasoning`.
- **Evidence compaction** — agents never see raw logs, only pre-aggregated structures.
  Cheaper, and it stops the model doing arithmetic it is bad at.
- **Bounded rounds** — hard stop at 2, so a stubborn incident cannot spend without limit.

Prompt caching is wired but **does not currently fire**: Bedrock needs 4,096 cached
tokens on Haiku and these system prompts are ~600. CloudWatch says so — no cache
metrics exist for this account. Documented rather than quietly left as a claim; see
`ARCHITECTURE.md` §7 defect 12.

No vector database. Incident memory is a small explainable similarity function — no
infrastructure, no idle cost, and you can see *why* two incidents were judged similar.
It also sidesteps a trap: Bedrock Knowledge Bases default to an OpenSearch Serverless
vector store with a 2-OCU minimum, roughly **$345/month at zero queries**.

---

## The eight scenarios

| Key | What it tests | Ends as |
|---|---|---|
| `schema_drift` | Full approve path — vendor renames a column mid-close | Resolved |
| `join_fanout` | Silent corruption — nothing failed, the numbers are just wrong | Resolved |
| `vendor_outage` | Alert-storm de-duplication — 10 alerts, 1 incident | Resolved |
| `null_explosion` | **Reject path** — the technically correct fix is the wrong business call | Quarantined + ticketed |
| `chronic_lateness` | **Defer path** — third occurrence in 30 days, reframed as a backlog item | Deferred |
| `transient_timeout` | **Autonomous path** — reversible, no code, nobody woken | Resolved, no gates |
| `ad_spend_drift` | Ordinary approve path that records a standing decision | Resolved |
| `ad_spend_drift_repeat` | **Precedent path** — same problem 3 weeks on, nobody asked | Resolved, no gates |

Each declares a ground truth the eval harness scores against: root cause, severity,
blast radius, alert de-duplication, and whether forbidden actions were avoided.

Two of them prove a *negative*. `transient_timeout` and `ad_spend_drift_repeat` have
deliberately empty scripted approvals — if any gate opened, nobody would answer and the
incident would escalate. They pass only because no gate opened.

---

## Stack

Python 3.10+ · LangGraph · Pydantic v2 · Amazon Bedrock (Claude)

Bedrock is wired and runs for real with `--bedrock`. The data platform is **simulated**:
everything that scores a result runs against `SimulatedPlatform`.

`aegis/platform/snowflake.py` is a full `PlatformClient` over `ACCOUNT_USAGE`,
`INFORMATION_SCHEMA` and `DATA_QUALITY_MONITORING_RESULTS` — **written, and never executed
against a live account.** Nothing imports it, so it cannot affect a scored result;
`scripts/check_snowflake.py` exercises every method against a real account and prints what
worked, what came back empty and what raised. Until that output exists, "written" is the
whole claim.

The integrations (SES, Jira, ServiceNow, GitHub) have real adapters and run mocked until
credentials are supplied.

`docs/` covers the AWS setup, the architecture, and the AgentCore deployment path.

---

## Layout

```
aegis/
  contracts.py     typed hand-off contracts -- read this first
  config.py        settings + cost governor
  policy.py        redaction firewall, autonomy ladder, severity scoring
  precedent.py     approval as a standing decision
  audit.py         hash-chained audit trail + trace bus
  reasoning.py     Bedrock / heuristic reasoning layer
  memory.py        incident memory and recurrence detection
  tools.py         provenance-recording tool belt
  graph.py         LangGraph orchestration
  report.py        self-contained HTML trace report
  platform/        the simulated warehouse, its scenarios, and the unverified
                   Snowflake client
  agents/          the nine agents
evals/harness.py   ground-truth scoring, 118 checks
tests/             adversarial governance tests, 94 of them
scripts/           setup and preflight utilities
docs/              see below
```

## Documentation

| File | What it covers |
|---|---|
| [`ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The design, the hand-off contracts, the confidence-gate arithmetic, **§7: twelve defects found during the build**, and stated limitations |
| [`CODE-TOUR.md`](docs/CODE-TOUR.md) | **Every claim mapped to the file and function that makes it true** — the reading order, and where to point when asked |
| [`DEMO-GUIDE.md`](docs/DEMO-GUIDE.md) | How to run and present it, with honest answers to hard questions |
| [`DEPLOYMENT.md`](docs/DEPLOYMENT.md) | The AgentCore path, and the one part that is a real architectural change |
| [`AWS-SETUP.md`](docs/AWS-SETUP.md) | Reproducible Bedrock setup in ~10 minutes |
| [`PROJECT-LOG.md`](docs/PROJECT-LOG.md) | Build history, every decision and every defect |

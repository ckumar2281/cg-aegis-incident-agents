# Aegis

**Governed agentic incident management for data pipelines.**

Aegis detects a data pipeline incident, investigates it with a team of specialised
agents, works out what actually broke, proposes a fix — and then refuses to touch
anything until two separate groups of humans have approved, in a specific order.

That ordering is the point of the system.

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
              │ Product Owner + Scrum Master                   │
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

python -m aegis.cli run schema_drift        # one incident, end to end
python -m aegis.cli run --all               # all five scenarios
python -m evals.harness                     # score against ground truth
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
- **Prompt caching** on static system prompts; **bounded rounds**, hard stop at 2.

No vector database. Incident memory is a small explainable similarity function — no
infrastructure, no idle cost, and you can see *why* two incidents were judged similar.

---

## The five scenarios

| Key | What it tests | Ends as |
|---|---|---|
| `schema_drift` | Full happy path — vendor renames a column mid-close | Resolved |
| `join_fanout` | Silent corruption — nothing failed, the numbers are just wrong | Resolved |
| `vendor_outage` | Alert-storm de-duplication — 10 alerts, 1 incident | Resolved |
| `null_explosion` | **Reject path** — the technically correct fix is the wrong business call | Quarantined + ticketed |
| `chronic_lateness` | **Defer path** — third occurrence in 30 days, reframed as a backlog item | Deferred |

Each declares a ground truth the eval harness scores against: root cause, severity,
blast radius, alert de-duplication, and whether forbidden actions were avoided.

---

## Stack

Python 3.10+ · LangGraph · Pydantic v2 · Amazon Bedrock (Claude) · Snowflake · S3

Runs locally against real services. `docs/` covers the AWS setup and the AgentCore
Runtime deployment path.

---

## Layout

```
aegis/
  contracts.py     typed hand-off contracts -- read this first
  config.py        settings + cost governor
  policy.py        redaction firewall, autonomy ladder, severity scoring
  audit.py         hash-chained audit trail + trace bus
  reasoning.py     Bedrock / heuristic reasoning layer
  memory.py        incident memory and recurrence detection
  tools.py         provenance-recording tool belt
  platform/        simulated + real Snowflake backends, scenarios
  agents/          the nine agents
evals/             ground-truth scoring harness
scripts/           setup and preflight utilities
docs/              AWS setup, architecture, deployment
```

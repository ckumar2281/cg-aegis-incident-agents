# What we built — every file, and why it exists

Training material and reviewer's map. ~15,800 lines of Python across 39 files.
`CODE-TOUR.md` is the 20-minute reading order; this is the complete index.

---

## The shape, in five layers

```
  cli.py / evals.harness          entry points
        │
  graph.py                        orchestration — the LangGraph state machine
        │
  agents/                         nine agents, each owning one question
        │
  reasoning.py · tools.py         how an agent thinks, and what it is allowed to touch
        │
  platform/ · integrations.py     the world: warehouse, storage, email, tickets, VCS
```

Cutting across all five: **contracts.py** (the types everything passes),
**policy.py** (the two guards), **audit.py** (what gets recorded).

---

## The governance core — read these four first

These are the files the whole project is *about*. If someone has ten minutes, this is
where they go.

| File | Lines | What it does |
|---|---|---|
| **`contracts.py`** | 708 | Every message crossing an agent boundary, as a Pydantic model. Nothing passes as free-form text. `IncidentPacket`, `Directive`, `SpecialistReport`, `RCAVerdict`, `RemediationPlan`, `DisclosureBundle`, `ApprovalRequest`. The orchestrator validates before routing, so a malformed hand-off fails at the boundary rather than three agents later. |
| **`policy.py`** | 427 | The two guards. `RedactionPolicy` is the disclosure firewall — thirteen rules scanning for table names, columns, credentials, diffs, stack traces. `AutonomyLadder` decides autonomous / gated / always-gated from risk, reversibility and whether code changes. Also `score_severity`, which is arithmetic, not a model judgement. |
| **`approvals.py`** | 691 | Gates, tokens and verdicts. `TokenMinter` issues HMAC-signed, TTL-bounded, single-use approval tokens. `ApprovalCoordinator.run_gate` sends the right tier to the right roles and collects verdicts. Three responders: scripted (eval), console (live demo), pending (production — everything times out and waits for a callback). |
| **`audit.py`** | 189 | `AuditChain` — SHA-256 hash chain where each event carries the digest of the previous one; `verify()` names the record that broke. `TraceBus` is the live event stream the CLI renders. The compliance artifact and the demo output come from the same source. |

---

## Orchestration

| File | Lines | What it does |
|---|---|---|
| **`graph.py`** | 1050 | The LangGraph `StateGraph`: 20 nodes, the conditional routing functions, the parallel fan-out, and `build_runtime` which wires every dependency into one `Runtime` object. Read `route_after_rca` and `route_after_plan` — those two functions are where the system decides. |
| **`cli.py`** | 395 | `aegis list / graph / run`. Renders the trace, the outcome panel and the disclosure table. The run banner reports which backends are live — added after a run claimed to have emailed five people while silently on a mock. |

---

## The agents

| File | Lines | What it does |
|---|---|---|
| **`agents/base.py`** | 151 | Shared scaffolding. Every agent has the same three beats: gather facts through tools, compute an answer in Python, then ask the model to improve on it. The separation is what makes agents testable without a model. Also `handoff()` and `audit()`. |
| **`agents/triage.py`** | 565 | Correlate, classify, scope, dispatch. Folds N alerts into one incident, computes blast radius and severity, and **writes a numbered directive for each specialist**. Does not diagnose and does not fix. |
| **`agents/forensics.py`** | 888 | All four specialists: lineage impact, pipeline forensics, quality forensics, change correlator. They overlap deliberately little — independent agreement is what the confidence score is made of. |
| **`agents/rca.py`** | 400 | Scores hypotheses arithmetically: `share × mass`, agreement bonus for independent proposers, refutation at double weight. Gates to proceed / dig deeper / escalate. The model may lower confidence by 0.15 but raise it by only 0.05. |
| **`agents/planner.py`** | 668 | Two halves, because data and code are repaired on different clocks. Data steps run now with explicit risk tiers and rollback; code becomes a pull request a human reviews. |
| **`agents/disclosure.py`** | 391 | One verdict, two audiences. Builds the business brief and the technical packet, and runs the firewall over the brief field by field. The agent the rest of the system exists to protect. |
| **`agents/executor.py`** | 414 | Carries out an already-approved plan, **verifies independently that it worked**, rolls back if not, and opens the PR. Separated from planning so the plan is reviewable before anything runs. |

---

## Reasoning, tools and memory

| File | Lines | What it does |
|---|---|---|
| **`reasoning.py`** | 378 | `Reasoner.think(...)` — Bedrock Converse with a Pydantic response model and, crucially, a `fallback` callable. Every call has a deterministic answer computed in Python first; the model improves on it. `CostLedger` meters spend and degrades honestly at the ceiling. |
| **`tools.py`** | 202 | What agents are allowed to do, and the provenance they leave. **Facts come from tools; judgement comes from the model.** Every `Evidence` carries the `ToolCall`s that produced it. |
| **`memory.py`** | 217 | Recurrence detection without a vector database — a small, explainable similarity function over structured incident records. "This has happened before, here is what fixed it." |
| **`precedent.py`** | 320 | Approval as a standing decision. The answer to gate decay: by the fifth identical request people click approve without reading, and the control becomes theatre. A precedent has a scope, an author and an expiry. |
| **`config.py`** | 382 | All settings in one dataclass, every one env-overridable. Model tier per agent (the cheap/strong map that halves cost), price table, budget policy, backend selection, recipient addresses. |

---

## The world and the outside

| File | Lines | What it does |
|---|---|---|
| **`platform/client.py`** | 534 | `PlatformClient` — the seam between agents and reality. Fourteen methods returning *compacted* structures. Agents never touch a cursor or the `World` directly. |
| **`platform/world.py`** | 554 | A deterministic simulation of a Snowflake warehouse: assets, lineage, metrics, task runs, load history, changes, tags. Gives the eval harness ground truth to score against. |
| **`platform/scenarios.py`** | 1086 | The eight incidents, each building a world state plus alerts, plus the ground truth the evals check and the scripted approval verdicts. |
| **`platform/snowflake.py`** | 928 | The same interface against a real warehouse. Dry-run by default, fixed allow-list of parameterised statements, **no `drop_table` at all**. Verified against a live Enterprise account. |
| **`platform/storage.py`** | 381 | `StorageClient` — quarantine as a real object move. `S3Storage` is dry-run by default, refuses to overwrite an existing target, and exposes **no delete primitive**; delete exists only as a step inside `move`. |
| **`integrations.py`** | 615 | Email, ticketing, VCS. The firewall is enforced at the *delivery* boundary: a technical-tier send before business release raises rather than silently dropping. A transport failure is recorded on the message instead — refusal and failure are opposites. |
| **`report.py`** | 400 | One self-contained HTML file per run. No CDN, opens offline. For someone answering "why did it do that" months later without reading the code. |

---

## Entry points, tooling, tests

| File | Lines | What it does |
|---|---|---|
| **`evals/harness.py`** | 523 | Scores every scenario against ground truth. 118 checks, `--json` for CI, `--bedrock` to run the same suite against live models. |
| `scripts/check_bedrock.py` | 283 | Preflight: models, region, inference profiles, a real call. |
| `scripts/check_snowflake.py` | 164 | Probes all 14 client methods. Reports OK / **EMPTY** / FAIL separately — a true negative is not a gap. |
| `scripts/seed_snowflake.sql` | 233 | Builds `CG_AEGIS_DEMO`: lineage chain, tags, DMFs, a task, a COPY through a stage, read-only role — **and §8, the teardown**. |
| `scripts/seed_s3.py` | 258 | Real parquet into the landing zone. `--reset` restores both zones between demo runs. |
| `tests/test_governance.py` | 417 | The firewall, tokens, the hash chain, the ladder, severity scoring. |
| `tests/test_outbound_boundary.py` | 233 | Attacks the rendered email — the place nothing was looking. |
| `tests/test_delivery_failure.py` | 219 | A dead mailer must not kill an incident; a refusal must still be fatal. |
| `tests/test_storage.py` | 151 | The write locks, with an S3 client that raises if anything real is attempted. |
| `tests/test_snowflake_guards.py` | 106 | The allow-list and dry-run defaults. No connection needed. |
| `tests/test_precedent.py` | 262 | Scope, expiry, and what a precedent must *not* cover. |

**Every test constructs the leak or the failure it guards against.** A test that only
asserts the happy path would have passed on all fifteen defects.

---

## If you change X, look at Y

| Change | Also touch |
|---|---|
| A new agent | `contracts.py` (its report type), `graph.py` (node + edge), `config.py` (model tier) |
| A new redaction rule | `policy.py`, plus a test in `test_governance.py` that constructs the leak |
| A new remediation action | `planner.py`, `policy.py` (its risk tier), `platform/client.py` (how it executes) |
| A new scenario | `platform/scenarios.py` only — world, alerts, ground truth and verdicts all live together |
| Anything that sends | `integrations.py`, and check the firewall runs over the *rendered* output |
| A new setting | `config.py`, and make sure it is visible in the CLI banner if it can silently degrade |

---

## The five files to read if you read nothing else

1. **`contracts.py`** — the shape of everything.
2. **`policy.py`** — the two guards, and why severity is arithmetic.
3. **`graph.py`**, specifically `route_after_rca` and `route_after_plan` — where it decides.
4. **`agents/rca.py`** — how confidence is computed rather than felt.
5. **`agents/disclosure.py`** — the reason the rest exists.

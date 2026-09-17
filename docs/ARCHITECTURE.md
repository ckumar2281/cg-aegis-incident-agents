# Aegis — Architecture

**Governed agentic incident management for data pipelines.**

A team of specialised agents detects a data incident, investigates it, works out what
broke, proposes a fix — and then refuses to touch anything until the right humans have
approved, in a specific order, having seen specific things.

That ordering is the point of the system, and the rest of this document explains why.

---

## 1. Problem statement

**Data pipeline and ETL incidents**: failed loads, upstream schema drift, freshness SLA
breaches, silent data corruption, and bad data propagating through a lineage-connected
warehouse.

This domain was chosen over infrastructure or log analysis for one reason: **the
expensive failure mode is not slowness, it is confident wrongness.** A web service that
goes down announces itself. A data platform that quietly writes inflated revenue figures
into the table finance closes the books from does not — and by the time anyone notices,
three weeks of downstream work has been built on it.

That asymmetry is what makes the domain worth a governed agent system rather than a
faster one.

### Why this needs multiple agents

Not because "multi-agent" was a requirement, but because the work genuinely decomposes
and the decomposition buys something measurable:

| The work | Why it is a separate agent |
|---|---|
| Correlating a noisy alert stream into one incident | A vendor outage here fires 10 alerts across 9 assets. Structural lineage correlation folds them into 1, and that is a different job from diagnosis |
| Establishing who is affected | Drives severity and the business brief; needs lineage traversal, not log reading |
| Establishing what the orchestrator did | Distinguishes a broken pipeline from a pipeline faithfully loading bad data |
| Establishing how the data deviates | Separates "data is missing" from "data is present but wrong" — different incidents, different fixes |
| Establishing what changed | Most data incidents are caused by a change; find it or rule it out |
| Deciding which explanation is right | Requires weighing the four above *against each other* |
| Deciding what to do | Requires knowing which actions are reversible and in what order |
| Deciding who may see what | A governance question, not a technical one |
| Doing it and checking it worked | Verification must be independent of the actor |

The four forensic specialists run **in parallel and read different data**. That is what
makes their agreement informative: when the quality analyst and the change correlator
independently reach the same conclusion from different evidence, that convergence is
worth more than either alone. **A single prompt cannot corroborate itself.**

---

## 2. The governance model

This is the part that distinguishes Aegis from a faster RCA bot, so it goes first.

### Business approval gates technical disclosure

```
         ┌──────────────────────────────────────────┐
         │ one root-cause verdict                   │
         └────────────────┬─────────────────────────┘
                          │ rendered twice
           ┌──────────────┴──────────────┐
           ▼                             ▼
  ┌─────────────────┐          ┌──────────────────────┐
  │ BUSINESS BRIEF  │          │ TECHNICAL FIX PACKET │
  │ plain language  │          │ root cause, diffs,   │
  │ no code         │          │ rollback, evidence   │
  │ no object names │          │                      │
  │ no errors       │          │  ── WITHHELD ──      │
  └────────┬────────┘          └──────────┬───────────┘
           │                              │
           ▼                              │
     Product Owner                        │
     approves ─────────── releases ───────┘
                                          ▼
                              Developer + Engineering Manager
```

**The developer does not receive the fix until the business has approved the problem.**

Three consequences, and each is a design goal rather than a side effect:

1. **The business decision is made on business grounds.** A PO handed a diff defers to
   whoever understands it. A PO handed impact, cost, and risk-of-waiting makes an actual
   decision.
2. **An engineer cannot be leaned on to ship something unsanctioned**, because it has
   not been sent to them yet. The sequencing removes the pressure rather than asking
   people to resist it.
3. **The audit trail can prove what each audience saw, and when.**

### The firewall is machine-enforced

`policy.RedactionPolicy` checks every business-tier artifact against 13 rules — SQL,
diffs, code fences, database object names, URIs, filesystem paths, source filenames,
stack traces, credentials, email addresses, long digit sequences, raw JSON, internal
identifiers.

A violation rejects the artifact. The disclosure agent gets **one** repair attempt with
the specific findings fed back. If it fails twice the incident **escalates to a human
rather than over-disclosing**. The system fails closed.

This is not a prompt instruction. Models are cooperative, not reliable, and "please
don't include table names" is a request, not a control. The deterministic draft the
agent starts from is redaction-safe by construction, so the model can write something
better but cannot make the output unsafe by writing something worse.

> Two of these rules were **broken and looked fine** until adversarial tests were
> written. See §7 — it is the most instructive thing in this document.

### Approval as precedent

The strongest criticism of any approval gate is that it decays. By the fifth identical
request people click approve without reading, and the control provides the *appearance*
of oversight rather than oversight. Adding more gates makes this worse.

Aegis treats an approval as a **standing decision about a class of situation**. The first
occurrence asks a human; a recurrence with the same fix applies the decision already
made, and names whose it was. The Product Owner's attention is spent on novel judgements
rather than repeated ones.

Safe only because the boundaries are narrow:

| Boundary | Rule |
|---|---|
| Scope | Same root cause **and** same asset. Approving a drift on the marketing feed does not authorise one on payments |
| Severity | SEV1 is never covered, however familiar it looks |
| Actions | `rollback_deployment`, `restate_table`, `drop_table`, `force_merge_pr` are never pre-authorisable |
| Envelope | The new plan must fit inside what was approved — no extra actions, no higher risk tier, no larger code change |
| Lifetime | 90-day expiry, revocable at any time |

Every application is audited citing the precedent and the person who set it, so *"the
system did this on its own"* is never the whole answer — there is always a named human
and a date behind it. When a precedent exists but does **not** cover the plan, that is
traced too, because silence would be indistinguishable from there never having been one.

The boundaries are the entire value of the feature, so the tests attack them:
`tests/test_precedent.py` is 22 tests of which almost all assert a **refusal**.

### Autonomy proportional to risk

Gates are not the opposite of autonomy. Waking four people to approve re-running a
failed task is how an approval process gets ignored. The ladder decides per incident,
**from the plan the system just wrote**:

| Condition | Result |
|---|---|
| Plan contains a dangerous action (release quarantine, backfill, restate, rollback, drop, alter schema) | 🔒 both gates |
| Plan makes an **additive, modifying or destructive** code change | 🔒 both gates |
| Plan's only code changes are **cosmetic** | 🟢 autonomous |
| **SEV4** + all steps reversible + no code change | 🟢 autonomous |
| A live **precedent** covers this exact situation | 🟢 autonomous, attributed to the approver |
| Anything else | 🔒 both gates |

Four inputs: how risky the actions are, how large the change is, how severe the incident
is, and whether a human has already ruled on this exact case. The decision and its reasoning are written to the trace and the audit
chain, so the system explains *why* it did or did not ask.

### Four terminal states

| State | What happened |
|---|---|
| **Resolved** | Both gates approved. Data remediated and verified, PR opened |
| **Resolved autonomously** | The ladder granted autonomy, or a standing approval covered it. Executed, verified, owner notified afterwards |
| **Rejected and quarantined** | Business declined. Data stays held, ticket raised, pipeline owner notified |
| **Deferred to backlog** | Parked as a future fix with the high-level change captured |

Plus **escalated**, when confidence never reached the floor — the system hands over
rather than guessing.

---

## 3. Agent roster

| Agent | Owns | Model tier |
|---|---|---|
| **Triage** | Correlate, classify, scope, dispatch the work plan | cheap |
| **Lineage analyst** | Blast radius: downstream, consumers, SLAs, business processes | cheap |
| **Pipeline forensics** | Orchestrator history: runs, loads, errors, retries | cheap |
| **Quality forensics** | Data deviation: counts, nulls, duplicates, schema diff | cheap |
| **Change correlator** | Deploys, config, vendor notices, and temporal correlation | cheap |
| **RCA synthesiser** | Score hypotheses, decide confidence, gate the outcome | strong |
| **Remediation planner** | Risk-tiered data steps + the code fix | strong |
| **Disclosure officer** | One verdict → two audiences, redaction enforced | strong |
| **Executor / verifier** | Act, verify independently, roll back, open the PR | cheap |

The specialists are mostly structured retrieval plus a short judgement — cheap models do
that well. Synthesis, planning and disclosure are where reasoning quality changes the
outcome. That one config map is the difference between a **$0.13** incident and a
**$0.25** one.

---

## 4. Hand-off design

Nothing crosses an agent boundary as free-form text. Every message is a validated
Pydantic contract in `contracts.py`, and the orchestrator validates before routing.

### Directives, not context dumps

Triage does not hand specialists a blob of context and hope. It issues each one a
**numbered question it is accountable for answering**:

```
D1 → lineage    Map the blast radius of RAW.STRIPE_CHARGES: which downstream assets
                are affected, which are tier-1, which consumer surfaces and business
                processes are exposed, and which SLAs are already breached.

D4 → change     Identify every deploy, config change, vendor notice or schema registry
                update in the last 72 hours touching RAW.STRIPE_CHARGES or its
                upstreams, and assess how well each correlates in time with onset.
```

A `SpecialistReport` that does not address its assigned directives is incomplete, and
the RCA agent can **mint new directives** to re-open the investigation.

### Admitting ignorance is a first-class output

`unresolved_directives` and `follow_up_requests` are in the schema. An agent saying *"I
could not establish this, and here is who should look"* is the mechanism that triggers a
second round — which is far cheaper than a confident wrong fix applied to production
data.

### The confidence gate

`RCASynthesizer` scores every hypothesis arithmetically, not by asking a model how sure
it feels:

```
raw    = best_prior × (1 + 0.25 × (independent_proposers − 1))     agreement bonus
       + 0.15 × Σ supporting_evidence_strength
       − 0.30 × Σ refuting_evidence_strength                       double weight

share  = raw / Σ raw                       how good vs the alternatives
mass   = Σ raw / (Σ raw + 0.30)            is there enough evidence at all
posterior = share × mass
```

Two deliberate asymmetries:

- **Refutation outweighs support 2:1.** "Every task succeeded" should *kill* the
  infrastructure hypothesis, not merely rank it lower.
- **`mass` exists because `share` alone lies.** Eliminate every rival and the survivor
  scores 1.0 regardless of evidence. That bug shipped, and §7 records it.

Then the gate: `PROCEED` above threshold with margin, `DIG_DEEPER` (loop, bounded at 2
rounds), or `ESCALATE_HUMAN` below the floor.

### The model may doubt more easily than it may assert

The RCA agent can lower computed confidence by up to **0.15** and raise it by at most
**0.05**. The asymmetry is the safety property: the failure mode is an extra
investigation round, not a wrong remediation.

### Provenance

Every `Evidence` carries the `ToolCall`s that produced it. Every state transition,
disclosure and approval is appended to a **SHA-256 hash chain** where each event carries
the digest of the previous one. Tamper with history and `verify()` reports which record
broke. That is how you answer, months later: *who approved releasing production data,
and what were they shown at the time?*

---

## 5. Orchestration

LangGraph, doing real work rather than decorating a linear script:

```
START → quarantine → triage ─┬→ lineage    ─┐
                             ├→ pipeline   ─┤
                             ├→ quality    ─┼→ rca ─┬→ round 2 ↺
                             └→ change     ─┘       ├→ escalate → END
                                                    └→ plan
                                                        │
                                        ┌───────────────┴──────────────┐
                                   autonomous                      disclose
                                        │                              ↓
                                        │                       business_gate
                                        │              ┌────────────┼────────────┐
                                        │           reject        defer      approve
                                        │              ↓            ↓            ↓
                                        │           ticket      backlog   technical_gate
                                        │              ↓            ↓       ┌────┴────┐
                                        │             END          END   reject  approve
                                        └──────────────────────────────────┐        ↓
                                                                        ticket   execute
                                                                           ↓        ↓
                                                                          END    close → END
```

Three load-bearing features:

- **Genuine fan-out.** Four nodes in one superstep, merged through a list reducer.
- **A loop, not a pipeline.** The edge out of `rca` is conditional and routes *back* with
  fresh directives. Round two sees round one's evidence.
- **Gates as nodes with three exits.** The business gate does not return a boolean; each
  exit is a distinct terminal state with its own side effects.

### Containment happens before any agent runs

A file failing validation is quarantined **immediately and automatically**. No agent, no
approval. Every decision afterwards is therefore unhurried — the approval chain decides
whether the file is ever *released*, never whether to contain it.

---

## 6. Engineering decisions worth defending

### Deterministic where determinism matters

Severity, hypothesis scoring, blast radius, the autonomy decision and remediation
sequencing are **computed in Python**. The model names things, explains them, and writes
the prose a human reads.

Severity drives paging and approval SLAs — a reviewer must be able to reproduce why
something was a SEV1 without re-running an LLM. Remediation ordering encodes safety
properties (snapshot before restate, release before reprocess) and a model rediscovering
that ordering on every incident is a liability, not a feature.

### The heuristic backend is not a stub

Every agent computes its structured answer in Python first, then optionally lets the
model improve it. That fallback earns its place three times:

- **Cost.** Development, tests and the eval suite run on it for $0.
- **Determinism.** An agentic system whose score moves 10% between identical runs cannot
  be improved, because you can never distinguish a regression from variance.
- **Resilience.** It is also the failure path — throttling, malformed JSON, or the budget
  ceiling all degrade to it rather than failing the incident, and the degradation is
  stamped into the audit trail.

The tool layer supplies facts; the model supplies judgement and language. On the
heuristic path the facts are identical and the judgement is rule-based.

### Cost is an architectural concern

A fan-out agent graph is an excellent way to spend money by accident: every
investigation round multiplies calls.

- **Model tiering per agent role** — one config map, ~2× cost difference
- **Per-incident budget governor** — hard USD and call ceilings; on breach the remaining
  agents degrade and the incident is marked `degraded_reasoning`
- **Prices match on model family, not full profile ID** — an uncatalogued model bills at
  the *most expensive* known rate, so an unknown model makes the governor more cautious
  rather than blind
- **Evidence compaction** — agents never see raw logs, only pre-aggregated structures
  ("row count is 3.1× median"). Cheaper, and it stops the model doing arithmetic
- **Bounded rounds** — hard stop at 2

Measured live: **$0.16 per incident** (22 calls, two investigation rounds); ~$6 for
the whole POC. A single-round incident lands nearer $0.09 — the confidence loop is the
variable, not the pricing.

### No vector database

Incident memory is a small explainable similarity function over structured records. No
infrastructure, no idle cost, and you can see *why* two incidents were judged similar. At
the volume one data platform produces — hundreds of incidents a year — structural and
lexical overlap is competitive with embeddings.

It also avoided a trap: Bedrock Knowledge Bases default to an OpenSearch Serverless
vector store with a **2-OCU minimum, roughly $345/month at zero queries**.

---

## 7. What went wrong, and what it teaches

Fifteen defects found during the build. These are recorded because a reviewer will probe
exactly here, and because the fixes are more interesting than the features.

| # | Defect | Why it matters |
|---|---|---|
| 0 | Price table keyed on full profile IDs; the account's actual models matched nothing and priced at **$0** | A cost control that silently prices at zero is not a cost control |
| 1 | Confidence pinned at **100%** — refutation zeroed rivals, so the survivor took the whole share | The system reported certainty on thin evidence |
| 2 | Severity treated reachability as materiality — everything eventually reaches the exec dashboard, so **everything was SEV1** | The severity scale stopped carrying information |
| 3 | Change correlation ignored **causal direction** — a mart refactor competed to explain a fault three hops upstream of it | Data flows one way; the correlator didn't know that |
| 4 | Temporal correlation decayed to zero at **24 hours** — the flagship scenario's vendor notice (posted 5 days early) was invisible | Vendor notices arrive *before* the change lands |
| 5 | **The autonomy ladder was decorative** — it computed the decision, logged it, and the graph ignored it via an unconditional edge | The trace said "executing autonomously" and then asked four people anyway |
| 6 | Pipeline forensics emitted **no evidence** for an ordinary task failure — only load failures and vendor errors had branches | A warehouse timeout is the commonest real incident and produced nothing |
| 7–8 | **Two holes in the redaction firewall** | See below |
| 9 | Every notification defaulted to **business tier** while every call site sent to the pipeline owner, a *technical* recipient | Five of seven messages per incident were recorded at the wrong tier — including in the audit trail that claims to prove who saw what |
| 10 | **Neither an approval-request body nor any subject line was ever firewall-checked.** Only the brief's individual fields were | Anything the email renderer added *between* the fields left the building unchecked |
| 11 | Two firewall rules were **over-broad in ways only a rendered email could reveal** | A control that blocks the recipient's own approval button is a control that gets switched off |
| 12 | **Prompt caching never once fired.** The system prompts are far below Bedrock's minimum cacheable prefix, so every cache point was silently ignored | A documented cost optimisation that has never executed is a claim, not an optimisation |
| 13 | The Snowflake seed script left **a task on a 10-minute schedule and DMFs on a 5-minute one**, with no teardown | Tuned for fast verification, shipped as a standing cost. ~78 credits/month for a table nobody reads |
| 14 | **A transport failure killed the whole incident.** SES raising `NoCredentialsError` propagated out of the graph, discarding two minutes of reasoning, four specialists, two RCA rounds and the entire audit chain | The system could not tell "we must not send this" from "we could not send this" — and threw away the record that would have explained which |
| 15 | **`ZoneLayout` configured nothing.** `build_runtime` read `settings.raw_bucket` and passed a `ZoneLayout` into the runtime; the quarantine node moved `record.original_uri` → `record.quarantine_uri`, both hard-coded in `scenarios.py` | Configuration that cannot change behaviour is decoration — the same defect as #5, in a different costume. Invisible until the store became real, because S3 bucket names are globally unique and `aegis-raw` is not a name you can have |

### Defect 14: refusal and failure are opposites

The firewall raises `DisclosureViolation` when a message is about to reach the wrong
person, and that correctly stops the incident — the control fired, and continuing would
mean leaking. `SesEmailSink` then failed on a missing credential, raised, and stopped the
incident in exactly the same way. Same mechanism, opposite meanings: one says *we must
not do this*, the other says *we could not do this*. Only the first is a reason to
abandon the work.

The fix keeps the two paths deliberately asymmetric. `EmailSink._attempt` catches
transport errors onto `SentMessage.delivered` / `.delivery_error`; the firewall check
runs **before** it and is never caught. Writing it the obvious way — one `try/except
Exception` around the whole send — would have swallowed the firewall too, and a refused
message would have been logged as an undelivered one. That is the difference between a
control and a shrug, and `TestDisclosureRefusalIsStillFatal` exists to keep it.

The second half is a governance question, not a plumbing one. If the approval email
never arrived, was the human asked? `ApprovalCoordinator.run_gate` now asks the
responder: `PendingResponder` — production, where the verdict *is* a click in that email
— records `approval_timeout` and escalates, because counting a verdict there would be
the system manufacturing its own approval. `ScriptedResponder` and `ConsoleResponder`
receive the answer through another channel, so their verdicts stand and the failed copy
is recorded as what it was.

### The two firewall holes are the instructive ones

The rule meant to stop database object names reaching the Product Owner required
**three** dotted parts (`DB.SCHEMA.TABLE`). Every object in this warehouse is two
(`MART.DAILY_REVENUE`). **The rule had never fired on a single real table name.** It
looked like a working control in every trace, because it was never asked to block
anything.

The diff rule missed `--- a/models/stg_payments.sql` because it demanded a non-space
character immediately after the dashes.

Neither was findable by running realistic incidents and observing that nothing went
wrong. Both were found in minutes by tests that **construct the leak they are trying to
prevent** — `tests/test_governance.py` builds fourteen different leaks and asserts each
is caught.

> **A control that has never been attacked is untested, not working.** That generalises
> well beyond this project.

One deliberate restraint in the fix: the diff rule still does not match bare `+ ` / `- `
line prefixes, because those are markdown bullets. A control that rejects every bulleted
list gets switched off within a week, and a disabled control is worse than a narrow one.

### Defects 9–11: the firewall was guarding the wrong object

These three were found by the **first live Bedrock run**, not by the eval suite — which
is the point of running one. The run printed its disclosure table and a business-tier
row read:

```
notification | business | [resolved] RAW.STRIPE_CHARGES schema drift: CURRENCY_CODE...
```

A schema name, a table name and a column name, on a message stamped *business tier*.
Pulling that thread found three distinct defects stacked on each other.

**The tier label was wrong (9).** `send_notification` defaulted to `BUSINESS`, and all
five call sites passed no tier and sent to the pipeline owner — whose `HumanRole.tier`
is `TECHNICAL`. Nothing actually leaked: the content reached the right person. But the
eval check asserting *"no technical message before business approval"* filters on tier,
so mislabelled messages were **invisible to the check that exists to catch them**. The
fix removes the parameter entirely: tier is derived from `HumanRole.tier`, one source
of truth, and a node cannot relabel its own message.

Correcting the labels immediately turned two scenarios red — which was the right
outcome, and forced the invariant to be stated precisely rather than accidentally. What
the design forbids is *asking* a developer to approve a fix packet before the business
has ruled. Telling the pipeline owner "the data is contained, a ticket is open" on the
reject path is correct and must stay allowed. `SentMessage.kind` now distinguishes
`approval_request` from `notification`, and the check names which one it means.

**Nothing checked the rendered message (10).** The disclosure agent scans the
`BusinessBrief` field by field. The *email* — subject line, section headings, whatever
the renderer inserts between fields — was never scanned by anything. Both senders now
run the same `RedactionPolicy` over subject and body for every business-tier message,
and fail closed. The subject line had never been checked anywhere in the system.

**Two rules were over-broad, invisibly (11).** Turning the check on lit up six of the
eight scenarios — all false positives, and both causes were latent from the start:

- The diff rule used `\s` as its separator, so a bare `---` line followed by any text
  matched. That is a markdown horizontal rule, and the approval emails use them as
  section dividers. Now `[ \t]`: a real header carries its path on the same line.
- `qualified_object` is written in upper case because Snowflake objects are upper case
  — but the shared `IGNORECASE` flag turned it into *"any two dotted words"*. It
  matched `approvals.example.com`: **the approval link in the Product Owner's own
  email.** The firewall would have blocked the button the recipient is meant to press.
  That rule is now the one case-sensitive rule in the set.

The generalisable lesson is narrower than "test your controls", and worth stating on
its own:

> **Guard the artifact that actually leaves, not the one you happen to have a schema
> for.** The firewall had been validating a well-typed `BusinessBrief` for the whole
> build. The thing that reaches a human inbox is a rendered string, and it is assembled
> *after* the last check runs.

And a second: correcting a mislabel is not cosmetic work. Defect 9 leaked nothing —
it blinded a control. The eval score went **up** from 112 to 118 checks after these
fixes, because two of the new checks could not previously have failed.

### Defect 12: the optimisation that was never running

Found by opening the CloudWatch console and looking, which took about a minute and
should have happened on day one.

`reasoning.py` puts a `cachePoint` on the system block of every Converse call, and the
docs listed prompt caching among the cost controls. Bedrock requires a **minimum
cumulative prefix** before a checkpoint is honoured: **4,096 tokens on Haiku 4.5**,
1,024 on Sonnet 4.6. These system prompts are 408–638 tokens.

| Prompt | ~tokens | Tier | Minimum | Cached? |
|---|---|---|---|---|
| Triage | 408 | Sonnet | 1,024 | ✗ |
| RCA | 484 | Sonnet | 1,024 | ✗ |
| Planner | 471 | Sonnet | 1,024 | ✗ |
| Disclosure | 562 | Sonnet | 1,024 | ✗ |
| Four specialists | ~636 each | **Haiku** | **4,096** | ✗ |

Below the minimum the call **still succeeds and the prefix is simply not cached** —
no error, no warning, nothing in the response that distinguishes it from a cache miss.
The proof is an absence: in the `AWS/Bedrock` namespace for this account there is no
`CacheReadInputTokenCount` **and no `CacheWriteInputTokenCount`**. CloudWatch only
publishes metrics that have data, so not one checkpoint was ever written.

The instructive part is the decision *not* to fix it. Padding these prompts past 4,096
tokens to qualify would be optimising backwards: a cache write is billed above base
rate, so it needs several hits to pay for itself, and each agent's system prompt is
used once or twice per incident against a 5-minute default TTL. The design comment
claimed the prompts were "long, static and reused across every incident" — static is
the only one of the three that was true.

So the cache point stays (correct and free, and it starts working if the prompts ever
grow) and the *claim* goes. Every cost figure in this repo was measured with caching
inactive, so none of them move.

> **An optimisation you have not measured is a belief.** This one was in the code, in
> the architecture doc and in the README, and its contribution to date is exactly zero.

### Defect 13: a verification convenience that became a standing bill

`seed_snowflake.sql` set the demo task to run every 10 minutes and the data metric
functions every 5, so the metadata views would populate in minutes instead of hours. That
was the right call for verifying the client. Shipping it without a teardown was not.

The cost is larger than the schedule suggests, for a reason that is easy to miss:
Snowflake bills warehouse time per second **with a 60-second minimum on every resume**. A
two-second query every 10 minutes is billed as a full minute, 144 times a day — roughly
2.6 credits/day, ~78 credits a month, on a table nothing reads. The DMFs add 864
serverless evaluations a day on top.

The script now ends with a teardown section that suspends both, explains the arithmetic,
and prints what was actually consumed. Caught by the user asking "do those tasks charge
anything" — which is the question I should have answered in the script's own comments
before anyone had to ask it.

> **A setting chosen to make a test fast is not a default.** The same reasoning that says
> "don't tune a safety threshold to improve a demo" says "don't leave the demo's
> scaffolding running."


---

## 8. Results

```
$ python -m evals.harness
118/118 checks passed (100%)   8/8 scenarios fully clean
0 governance violations

$ python -m pytest tests/
68 passed
```

| Scenario | Tests | Outcome | Sev | Confidence | Path |
|---|---|---|---|---|---|
| `schema_drift` | Full approve path — vendor renames a column mid-close | resolved | SEV1 | 84% | gated |
| `join_fanout` | Silent corruption — nothing failed, numbers are wrong | resolved | SEV3 | 64% | gated |
| `vendor_outage` | Alert storm — 10 alerts, 9 assets, 1 incident | resolved | SEV1 | 81% | gated |
| `null_explosion` | **Reject path** — the technically correct fix is the wrong business call | quarantined + ticketed | SEV2 | 54% | gated |
| `chronic_lateness` | **Defer path** — third occurrence in 30 days | backlog | SEV4 | 74% | gated |
| `transient_timeout` | **Autonomous path** — reversible, no code, nobody woken | resolved | SEV4 | 58% | **no gates** |
| `ad_spend_drift` | Ordinary approve path that records a standing decision | resolved | SEV3 | 85% | gated |
| `ad_spend_drift_repeat` | **Precedent path** — same problem 3 weeks on, nobody asked | resolved | SEV3 | 85% | **no gates** |

The confidence gate does real work: three scenarios proceed on round one, three loop for
more evidence before deciding.

### How the evaluation is structured

Checks are grouped into three families, and the grouping is the argument:

- **Diagnosis** — did it work out what happened?
- **Governance** — did it respect the rules it claims to enforce? These assert
  *negatives*: the technical packet was **not** sent before approval; execution did
  **not** happen without the required gates; the autonomous path opened **no** gate.
- **Remediation** — did it do the right things *and avoid the wrong ones*? Forbidden
  actions are checked explicitly: it is not enough to do the right thing if the system
  would also have done the dangerous thing given the chance.

A governance failure is reported differently from a wrong answer, because it is a
different kind of problem. A wrong diagnosis is a bad answer. A governance violation is
a control that did not hold.

`transient_timeout` has **deliberately empty scripted responses**: if any gate opened,
nobody would answer and the incident would escalate. It passes only because no gate
opened — the test proves the absence of a thing.

---

## 9. Limitations

Stated plainly, because a POC that claims no weaknesses invites someone to find them for
you.

- **The platform is simulated; the Snowflake backend is written and now verified.**
  A 33-asset warehouse with deterministic lineage, task history and metrics stands in for
  the real thing, and **every scored result still comes from it** — that separation is
  deliberate, because a suite whose answers depend on a live account is a suite that goes
  red when someone else changes a table. `SnowflakePlatform`
  (`aegis/platform/snowflake.py`) implements the whole protocol against
  `INFORMATION_SCHEMA` table functions, `ACCOUNT_USAGE` and
  `DATA_QUALITY_MONITORING_RESULTS`, and was **verified against a live Snowflake account
  on 17 Sep**: 13 of 14 methods returned real rows, the fourteenth (`failed_runs`) a true
  negative. `scripts/check_snowflake.py` reproduces it and reports empties separately
  from passes. Three things it cannot get from Snowflake at all are named in the file's
  own header: asset tier and ownership (object tags), deploys and pull requests (the
  VCS), and per-asset credit attribution.
- **Approvals resolve synchronously in the demo.** Real asynchronous operation —
  emails out, incident suspended, resumed by a signed callback — is designed
  (`PendingResponder`, signed single-use tokens) but the callback endpoint is not built.
- **The PR body carries the proposed diff as an annotated file** rather than applying
  the patch, because Aegis has never seen the target repository. Wiring it to a real repo
  layout is straightforward and deliberately not faked.
- **Remediation playbooks are hand-written per root cause.** This is a feature at six
  scenarios and a scaling problem at sixty. The honest answer is that the playbook set
  grows with operational experience, not that the model should improvise them.
- **Six scenarios is a small eval.** Enough to catch the eight regressions above, not
  enough to claim generalisation.
- **Distance decay and scoring constants are calibrated, not derived.** They are tuned
  against this suite and documented as such. Real deployment would recalibrate against
  real incident history.

---

## 10. What I would build next

1. **Live Snowflake backend** — the interface exists; this is implementation, not design.
3. **Asynchronous approval callbacks** — API Gateway + Lambda validating the signed
   tokens that already exist.
4. **Blast-radius-aware comms** — notify affected *consumers*, not just owners. The
   lineage data is already there.
4. **Precedent review surface** — a page listing live standing approvals with their
   envelopes and expiry, so a Product Owner can see what they have authorised and revoke
   any of it. The store supports revocation; nothing surfaces it yet.

---

## Appendix — running it

```bash
python -m aegis.cli list                    # the scenarios
python -m aegis.cli run schema_drift        # one incident, live trace
python -m aegis.cli run --all --report      # whole suite + HTML trace report
python -m aegis.cli run null_explosion -i   # you play the Product Owner
python -m aegis.cli graph                   # the graph as Mermaid
python -m evals.harness                     # the scorecard
python -m pytest tests/                     # the governance tests
```

Default backend is deterministic and free. `--bedrock` uses real Claude models
(~$0.13/incident). AWS setup is in `AWS-SETUP.md`; the build history, decisions and
every defect found are in `PROJECT-LOG.md`.

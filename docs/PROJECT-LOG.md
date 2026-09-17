# Aegis — Project Record

**Project:** Agentic DataPOC — "Aegis", governed agentic incident management for data pipelines
**Owner:** Chaitanya
**Repo:** `~/projects/aegis`
**Last updated:** 17 September 2026
**Deliverable due:** end of day Thursday 17 September · **Review:** Friday 18 September

> Living document. Covers the brief, design decisions, AWS setup, build progress, and
> every non-obvious problem found along the way.

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

The distinguishing idea: **business approval gates technical disclosure**.

```
Source → Ingestion → Validation → Failure/Quarantine → Agent → Diagnosis
  → Recommended Action → Human Approval (tiered) → Remediation/Rerun
  → Audit & Notification
```

| Gate | Approvers | Sees | Outcomes |
|---|---|---|---|
| **1. Business** | Product Owner | Business brief only — no code, no object names, no errors | Approve → unlocks Gate 2 · Reject → quarantine + ticket · Defer → future-issues backlog |
| **2. Technical** | Developer + Engineering Manager | Full technical fix packet: root cause, diffs, rollback | Approve → implement, open PR, reprocess data |

The developer never receives the fix detail until the business signs off on the problem
in business terms.

**Revision, 16 Sep — the Scrum Master was removed from the business gate.** Two business
approvers added ceremony rather than judgement: both saw the same brief and the second
had no information the first lacked. One accountable decision-maker is cleaner to
explain and cleaner to demo. The technical gate keeps two roles because the developer
and the engineering manager genuinely assess different things — correctness, and
acceptable risk.

**Not every incident asks a human.** The autonomy ladder decides per incident from the
plan it just wrote, on three inputs: how risky the actions are, how large the code
change is, and how severe the incident is.

| Condition | Result |
|---|---|
| Plan contains a dangerous action (release quarantine, backfill, restate, rollback, drop) | 🔒 both gates, always |
| Plan makes an **additive, modifying or destructive** code change | 🔒 both gates |
| Plan's only code changes are **cosmetic** (formatting, comments) | 🟢 autonomous |
| **SEV4** + every step reversible + no code change | 🟢 autonomous |
| Anything else | 🔒 both gates |

Four terminal states: **resolved** (with approval), **resolved autonomously**,
**rejected and quarantined** (ticket raised, pipeline owner notified), or **deferred to
backlog**.

---

## 2. Stack decisions

| Layer | Choice | Why |
|---|---|---|
| Orchestration | **LangGraph** supervisor + specialists | Explicit graph, conditional edges, confidence-gated loops. Matches AWS's own multi-agent SRE reference architecture |
| Reasoning | **Amazon Bedrock** (Claude) via boto3 Converse | Required by the brief |
| Data platform | **Snowflake** | Real incident signals: `TASK_HISTORY`, `COPY_HISTORY`, Data Metric Functions, Horizon lineage |
| Storage | **S3** raw + quarantine zones | File landing, fail-safe quarantine |
| Contracts | **Pydantic v2** | Typed hand-off contracts between agents |
| Deployment | Runs **locally** against real services | AgentCore Runtime documented as the production path, not deployed for the POC |

---

## 3. AWS Bedrock setup — completed ✅

### 3.1 Region

**`us-east-1`.** Note: model access was originally enabled while the console was on
`us-east-2` (Ohio), but the preflight resolved `us-east-1` from the local AWS config
and **worked** — because the Anthropic use-case form is granted **per account**, not
per region. Everything is now standardised on `us-east-1`.

### 3.2 Model access

The standalone "Model access" console page **no longer exists**. Current flow:

1. Bedrock console → **Discover → Model catalog**
2. Filter **Providers → Anthropic** → open **Claude Haiku 4.5**
3. Yellow banner → **Submit use case details**
4. Complete the form — **access granted immediately**, no approval queue

Submitted once for the whole account; unlocked all Anthropic models.

Form values used — intended users: **Internal only**. Use case: *"Internal proof-of-concept
for automated data pipeline incident management. A multi-agent system triages data quality
alerts, correlates them using warehouse lineage metadata, performs root-cause analysis, and
drafts remediation plans that are reviewed and approved by humans before any action is taken.
Internal engineering use only; no customer-facing deployment and no end-user access to the models."*

### 3.3 Authentication

**Bedrock API key (long-term, 30-day expiry)**, created at **Discover → API keys**.

Chosen over an IAM user: AWS provisions the backing permissions automatically, the key
is scoped to Bedrock only, and it self-expires. Short-term keys were rejected — they
expire in ~12 hours and would be dead before Friday.

**"Permissions to access Amazon Bedrock Marketplace models" left unchecked** —
Marketplace models run on dedicated hourly-billed endpoints; Claude is serverless.

**The key lives in `~/.zshrc`, not in the project folder.** Deliberate: `~/projects` is
connected to the Claude session, so a key in `.env` would be within its read scope.
A real environment variable beats the `.env` file, so behaviour is identical.

```bash
export AWS_BEARER_TOKEN_BEDROCK='...'   # in ~/.zshrc
```

### 3.4 Models discovered by the preflight

`scripts/check_bedrock.py` reads the account rather than trusting hardcoded IDs:

```
AEGIS_CHEAP_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0
AEGIS_STRONG_MODEL=us.anthropic.claude-sonnet-4-6
```

**No Sonnet 5 in this account**, so the strong tier is Sonnet 4.6 at $3/$15 rather than
$2/$10. Note the `global.` prefix on Haiku — a different cross-region inference profile
than the `us.` one assumed. This broke the price table and is why pricing now matches
on model *family* rather than full profile ID (see §6, bug 0).

### 3.5 Local environment

```bash
cd ~/projects/aegis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/check_bedrock.py
```

**Known issue, not remediated:** an early `pip install --user boto3` upgraded system
`botocore` 1.34.69 → 1.43.95, breaking a pin held by `aiobotocore 2.12.3`
(`botocore<1.34.70`). Warning only; boto3 works. If it bites another project:

```bash
deactivate
python3 -m pip uninstall -y boto3 botocore s3transfer
python3 -m pip install --user 'botocore<1.34.70'
```

The venv keeps its own copies and is unaffected. **Lesson:** use a venv from the start.

---

## 4. Cost position

| Item | Cost |
|---|---|
| Enabling model access | **$0** — no subscription, no idle charge |
| IAM, API keys, budget alarms | **$0** |
| **Bedrock inference** | **$0.16 per incident run** (measured live — see below) |
| Snowflake | **$0** — 30-day trial, $400 credits |
| S3, SES, Lambda, API Gateway | **~$0** at demo volume |
| AgentCore Runtime | **$0** — running locally |

**Realistic total for the POC: ~$6.** All development, tests and the eval suite run on
the deterministic heuristic backend at **$0**; Bedrock is called only on real demo runs.

Claude Max is a **separate meter** — it covers claude.ai and this build conversation.
It does not offset Bedrock, which is billed by AWS separately.

**Correction, 16 Sep:** this log previously said Bedrock "bills the AWS account card
monthly in arrears". That is true of a paid account but not of this one — the account is
on AWS's **credit-based free plan**, so usage draws down a credit balance instead. The
per-token price is the same either way; what changes is the constraint. The limit that
will bite first is the free period's **expiry date**, not the balance: ~$6 of planned
usage against the credits shown on Console Home is not close. `AWS-SETUP.md` §6 has both
cases.

### First live Bedrock run — `schema_drift`, 16 Sep

The whole system had been built and scored on the heuristic backend. This was the first
real one.

| | Estimated | Measured |
|---|---|---|
| Model calls | ~12 | **22** |
| Cost | $0.132 | **$0.1606** |
| Wall clock | — | **119s** (~12–22s per agent) |
| JSON parse failures | — | **0 of 22** |

**Why 22 calls, not 12.** The RCA synthesiser returned `dig_deeper` at 62% confidence,
so the graph looped back through all four specialists for a second round, then settled
at 58% and proceeded. Eight specialist calls instead of four. *Per-call cost tracked
the estimate almost exactly* — the round count moved the total, not the pricing. This
is the confidence loop working as designed, and it is the honest answer to "what does
a run cost": it depends on whether the first round was conclusive.

**No `heuristic:repair-failed` anywhere in the trace**, so structured-output parsing
held across all 22 calls — the single biggest risk in a typed-contract agent graph, and
the main reason for doing a live run before the demo rather than during it.

**Two minutes is a long time to stand in front of people.** Demo plan: lead on the
heuristic backend (~0.3s, deterministic, identical every time), then run *one* live
Bedrock incident to show it is real. Do not run the full suite live.

The run also found three governance defects the entire eval suite could not see — §6,
bugs 9–11.

### Idle-cost traps — deliberately avoided

| Trap | Idle cost | Status |
|---|---|---|
| **Knowledge Bases** (OpenSearch Serverless default) | 2 OCU min @ $0.24/hr ≈ **$345/mo at zero queries** | ❌ Not used. Incident memory is an in-process similarity function |
| **Provisioned Throughput** | Hourly commitment regardless of use | ❌ Not purchased |
| **AgentCore Runtime sessions** | Memory billed per second incl. idle | ❌ Not deployed |
| **Marketplace model deployments** | Hourly endpoint charges | ❌ Permission not granted |
| **Snowflake warehouse left running** | ~1 credit/hour | ⚠️ Use `AUTO_SUSPEND = 60` |

### Guardrails

- **AWS budget alarm** — notifies, does **not** cap. AWS has no hard spend limit. ⬜ *still to set up*
- **Code budget governor** — $0.50 hard ceiling per incident, then degrades to the free backend. ✅
- **Key expiry** — 30 days; nothing runs after that. ✅

---

## 5. Architecture as built

### Agents (nine)

| Agent | Owns |
|---|---|
| **Triage** | Correlate a noisy alert stream into one scoped, classified incident + a work plan |
| **Lineage analyst** | Who is hurt — downstream closure, consumers, SLAs, business processes |
| **Pipeline forensics** | What the orchestrator did — runs, loads, errors, retries |
| **Quality forensics** | How the data deviates — counts, nulls, duplicates, schema diff |
| **Change correlator** | What changed and when — deploys, config, vendor notices |
| **RCA synthesiser** | Score competing hypotheses, decide confidence, gate the outcome |
| **Remediation planner** | Risk-tiered data steps + a code fix for the PR |
| **Disclosure officer** | One verdict → two audiences, redaction machine-enforced |
| **Executor / verifier** | Act, verify independently, roll back on failure, open the PR |

The four specialists run **in parallel** and deliberately overlap only a little. When
two of them reach the same conclusion from different data, that agreement carries
weight — a single call cannot corroborate itself.

### What makes the hand-offs work

- **Directives, not context dumps.** Triage issues each specialist a numbered question it is accountable for answering.
- **Admitting ignorance is first-class.** `unresolved_directives` and `follow_up_requests` are in the schema; an honest gap triggers another round.
- **The confidence gate.** Hypotheses score arithmetically: best prior + agreement bonus for independent corroboration + supporting evidence − refuting evidence **at double weight**.
- **The model can doubt more easily than assert.** It may lower computed confidence by 0.15 but raise it by at most 0.05.
- **Provenance everywhere.** Every claim carries its tool calls; every transition is appended to a SHA-256 hash chain.

### Graph topology

```
START → quarantine → triage ─┬→ lineage    ─┐
                             ├→ pipeline   ─┤
                             ├→ quality    ─┼→ rca ─┬→ (dig deeper, round 2) ↺
                             └→ change     ─┘       ├→ escalate → END
                                                    └→ plan → disclose
                                                                 ↓
                                                          business_gate
                                                 ┌────────────┼────────────┐
                                              reject        defer      approve
                                                 ↓            ↓            ↓
                                              ticket      backlog   technical_gate
                                                 ↓            ↓       ┌────┴────┐
                                                END          END   reject   approve
                                                                      ↓        ↓
                                                                   ticket   execute → close
```

---

## 6. Problems found and fixed

Recorded because they are exactly what a reviewer will probe, and the fixes are more
interesting than the features.

### Bug 0 — the price table missed the real models

Keyed on full inference-profile IDs (`us.anthropic.claude-sonnet-5-...`). The account
actually has `global.anthropic.claude-haiku-4-5` and `us.anthropic.claude-sonnet-4-6`,
which matched nothing and therefore priced at **$0** — meaning the budget governor
would never have fired.

**Fix:** match on model *family*, stripping region prefixes. Unknown models now price
at the **most expensive** rate in the table, so an uncatalogued model makes the
governor more cautious, never blind.

### Bug 1 — confidence was pinned at 100%

Posterior was each hypothesis's share of total weight. Refutation is aggressive enough
to zero out rivals, so the last one standing scored 1.0 regardless of how much evidence
actually backed it. An investigation with one weak lead reported certainty.

**Fix:** confidence is now two factors multiplied — `share` (dominance over rivals) ×
`mass` (is there enough evidence to be confident at all, saturating so it approaches
but never reaches 1). Confidence now spans 54–84% across the suite.

### Bug 2 — severity treated reachability as materiality

In a warehouse of any size almost everything eventually reaches the executive
dashboard, so unweighted blast radius made **every** incident a SEV1. A segment field
four joins away from a KPI tile scored the same as revenue being wrong.

**Fix:** blast radius is distance-weighted — `1/(1 + 0.45·(depth−1))`, so depth 1
counts 1.00 and depth 5 counts 0.36. Applied to tier-1 assets, consumer surfaces *and*
financial proximity. Leaving consumer surfaces unweighted was on its own enough to push
a mid-severity incident into SEV1, because four distant dashboards saturated the cap
three nearby ones were meant to.

### Bug 3 — change correlation ignored causal direction

Two routine deploys touching assets **downstream** of the fault were competing as
explanations. A change to a mart cannot cause a null spike in the staging table that
feeds it — data flows one way.

**Fix:** only the failing asset and its upstreams are in causal scope. Downstream
changes are demoted (×0.25) rather than dropped, since they can still be a coincidental
co-factor worth a human noticing.

### Bugs 7 & 8 — the redaction firewall had two holes it could never have found on its own

Both were found by writing tests that *construct* the leak rather than tests that watch
normal runs and observe nothing going wrong. The distinction matters: before these
tests, the firewall had never been asked to block anything, so "it passed" meant only
that nothing had attacked it.

**The object-name rule required three dotted parts** (`DB.SCHEMA.TABLE`). Every object
in this warehouse is two parts (`MART.DAILY_REVENUE`, `RAW.STRIPE_CHARGES`). The rule
meant to stop database object names reaching the Product Owner had therefore **never
fired on a single real table name**. It looked like a working control in every trace.

**The diff rule missed `--- a/models/stg_payments.sql`** because the pattern required a
non-space character immediately after the dashes.

**Fix:** both patterns corrected, with the object rule matching two-part as well as
three-part names. One deliberate restraint: the diff rule still does **not** match bare
`+ ` / `- ` line prefixes, because those are markdown bullets — a control that rejects
every bulleted list gets switched off within a week, and a disabled control is worse
than a narrow one.

### Bug 5 — the autonomy ladder was decorative

The rule deciding whether humans are needed was implemented, called, written to the
trace and appended to the audit chain — and then **ignored**. The graph had
`add_edge("plan", "disclose")`, unconditional, so every incident went to the gates
regardless of what the ladder concluded. Autonomous execution could never have
happened.

It looked correct in the trace, which is what made it easy to miss: the log line said
*"executing autonomously"* and then the incident went and asked four people anyway.

**Fix:** the decision is now carried in graph state and routes a conditional edge to
either `disclose` (gated) or `autonomous` (execute, verify, notify after the fact). A
sixth scenario, `transient_timeout`, exercises the autonomous path — and it passes only
if no gate opens, because its scripted responses are deliberately empty. If a gate were
opened, nobody would answer and the incident would escalate.

### Bug 6 — no evidence for an ordinary task failure

`PipelineForensics` had branches for load failures and vendor errors, but not for a
plain task failure with an ordinary error code. So a warehouse timeout — probably the
single commonest incident in any real platform — produced **zero** findings, and the
investigation stalled at 46% confidence with nothing to say.

**Fix:** a branch for generic task failures that distinguishes transient
execution-environment errors (timeout, cancelled, resource, queued, throttled) from
logic errors, and proposes `infrastructure_failure` with refutation of the data-related
causes. Also added the matching playbook, which had been falling through to the generic
"contain and hand over" default that never actually re-ran anything.

### Bug 4 — temporal correlation decayed to zero at 24 hours

Linear decay meant a change three days old was scored as impossible. The flagship
scenario's Stripe vendor notice (posted 5 days before the change landed) was missed
entirely, so the change correlator contributed **nothing** to the headline incident.

**Fix:** exponential decay with a 72-hour half-life, and a 7-day window. Vendor notices
arrive well ahead of the change taking effect; schema migrations run over weekends; a
deploy only bites when the nightly batch next runs.

### Bugs 9–11 — found by the first live Bedrock run, 16 Sep

The whole system had only ever run on the heuristic backend. The first real Bedrock
execution of `schema_drift` succeeded — SEV1 matched, root cause matched, 31 audit
events valid, 4/4 steps verified, PR opened — and its disclosure table contained this
row:

```
notification | business | [resolved] RAW.STRIPE_CHARGES schema drift: CURRENCY_CODE...
```

Schema, table and column names on a message stamped **business tier**. Three separate
defects, stacked.

**Bug 9 — the tier label was wrong.** `send_notification` defaulted to `BUSINESS`, and
all five call sites sent to the pipeline owner, whose `HumanRole.tier` is `TECHNICAL`.
Nothing leaked — the content went to the right person — but the eval check asserting
"no technical message before business approval" *filters on tier*, so five of the seven
messages in a typical incident were invisible to it. The audit trail, which the README
claims can prove what each audience saw, was recording the wrong answer.

**Fix:** the `tier` parameter is gone. Tier is derived from `HumanRole.tier`. Correcting
the labels turned `null_explosion` and `chronic_lateness` red, which was correct and
forced the invariant to be stated properly: what is forbidden is *asking* someone to
approve a fix packet before the business ruled, not *telling* the pipeline owner that
data is contained. `SentMessage.kind` now separates `approval_request` from
`notification` and the check names which it means.

**Bug 10 — nothing checked the rendered message.** The disclosure agent scans the
`BusinessBrief` field by field. The email that leaves the building — subject line,
headings, anything the renderer inserts between fields — was scanned by nothing at all.
Subjects had never been checked anywhere in the system.

**Fix:** both `send_notification` and `send_approval_request` now run the same
`RedactionPolicy` over subject and body for every business-tier message, and raise
`DisclosureViolation` rather than sending. The policy is built once per incident on the
`Runtime` so no node can construct a laxer one, and `ApprovalCoordinator` defaults to a
real policy rather than to `None` — forgetting to pass one cannot disable the check.

**Bug 11 — two rules were over-broad, invisibly.** Switching the check on lit up six of
eight scenarios, all false positives, both causes latent since the rules were written:

- The `diff` rule used `\s` as its separator, so a bare `---` line followed by any text
  matched — a markdown horizontal rule, which the approval emails use as a section
  divider. Now `[ \t]`. A genuine `--- a/etl/stripe_ingest.py` still matches.
- `qualified_object` is written in upper case because Snowflake objects are upper case,
  but the module-level `IGNORECASE` flag made it *"any two dotted words"*. It matched
  `approvals.example.com` — **the approval link in the Product Owner's own email.** The
  firewall would have blocked the button the recipient is meant to press. It is now the
  only case-sensitive rule in the set.

**What this is worth saying out loud on Friday:** the eval suite could not have found
any of these, because it was reading the same mislabelled data. A live run found all
three in one screenful. And the score went *up* — 112 → 118 checks — because two of the
new checks could not previously have failed.

New coverage: `tests/test_outbound_boundary.py`, 14 tests that construct each leak and
each false positive.

### Bug 12 — prompt caching had never fired, 16 Sep

Found by opening the CloudWatch console. Filtering the `AWS/Bedrock` metrics for
"cache" returned **no matches** — neither `CacheReadInputTokenCount` nor
`CacheWriteInputTokenCount` exists for this account. CloudWatch only publishes metrics
that have data, so the absence of the *write* metric means not one cache checkpoint was
ever created.

**Cause.** Bedrock honours a cache point only once the cumulative prefix before it
clears a minimum: **4,096 tokens on Haiku 4.5**, 1,024 on Sonnet 4.6. The system
prompts are 408–638 tokens. Below the minimum the call succeeds and the prefix is
silently not cached — no error, nothing in the response to distinguish it from a miss.
The four specialists are the worst case: smallest budget model, highest threshold.

**Decision: do not fix, correct the claim.** Padding prompts to 4,096 tokens to qualify
is optimising backwards — a cache write bills above base rate and needs several hits to
break even, while each agent's system prompt is used once or twice per incident against
a 5-minute TTL. The `cachePoint` stays in the code (free, and correct if prompts grow);
the claim comes out of `README.md`, `AWS-SETUP.md` §4 and `CODE-TOUR.md`, and
`reasoning.py`'s docstring now explains why it is inert.

**No cost figures change.** Every measured number in this log was recorded with caching
inactive, because caching has always been inactive. The $0.16 incident is the real one.

For Friday: this is the cleanest example in the project of measurement beating
assumption. The optimisation was in the code, in the architecture doc and in the README,
and its lifetime contribution was zero.

---

## 7. Current results

```
$ python -m evals.harness

scenario            diagnosis  governance  remediation   total    result
schema_drift           6/6        6/6          4/4       16/16     PASS
join_fanout            5/5        6/6          4/4       15/15     PASS
vendor_outage          6/6        6/6          4/4       16/16     PASS
null_explosion         5/5        5/5          3/3       13/13     PASS
chronic_lateness       6/6        5/5          3/3       14/14     PASS
transient_timeout      4/4        3/3          3/3       10/10     PASS

118/118 checks passed (100%)   8/8 scenarios fully clean
0 governance violations

$ python -m pytest tests/
68 passed

$ python -m aegis.cli run --all
8/8 scenarios matched ground truth
```

Per-scenario behaviour:

| scenario | outcome | sev | confidence | path | approvers asked |
|---|---|---|---|---|---|
| schema_drift | resolved | SEV1 | 84% | gated | PO, dev, eng mgr |
| join_fanout | resolved | SEV3 | 64% | gated | PO, dev, eng mgr |
| vendor_outage | resolved | SEV1 | 81% | gated | PO, dev, eng mgr |
| null_explosion | rejected + ticketed | SEV2 | 54% | gated | PO only |
| chronic_lateness | deferred to backlog | SEV4 | 74% | gated | PO only |
| transient_timeout | resolved | SEV4 | 58% | **autonomous** | **none** |
| ad_spend_drift | resolved | SEV3 | 85% | gated | PO, dev, eng mgr |
| ad_spend_drift_repeat | resolved | SEV3 | 85% | **precedent** | **none** |

### How the evaluation is structured

Checks are grouped into three families, and the grouping is the argument:

- **Diagnosis** — did it work out what happened? Root cause, severity, type, blast
  radius, alert de-duplication, recurrence.
- **Governance** — did it respect the rules it claims to enforce? These assert
  *negatives*: the technical packet was **not** sent before approval; execution did
  **not** happen without the gates the ladder demanded; the autonomous path opened
  **no** gate; the audit chain is unbroken; no business message contains code.
- **Remediation** — did it do the right things *and avoid the wrong ones*? Forbidden
  actions are checked explicitly. It is not enough to do the right thing if the system
  would also have done the dangerous thing given the chance.

A governance failure is reported differently from a wrong answer, because it is a
different kind of problem: a wrong diagnosis is a bad answer, a governance violation is
a control that did not hold.

All four paths exercised: resolved-with-approval, rejected-and-ticketed,
deferred-to-backlog, and resolved-autonomously.

Two details worth noticing in that table:

- **`transient_timeout` sent zero approval emails.** The autonomy ladder decided no
  human was needed and the graph honoured it. Its scripted responses are empty, so if
  any gate *had* opened the incident would have escalated instead of resolving — the
  test passes only because no gate opened.
- **`null_explosion` sent one, not two.** The Product Owner rejected, so the Scrum
  Master was never asked. Nobody is chased for a decision that cannot change the outcome.

The confidence gate does real work: three scenarios proceed on round one, three loop
for more evidence before deciding.

### The five scenarios

| Key | Tests | Ends as |
|---|---|---|
| `schema_drift` | Full happy path — Stripe renames a column mid-close, both gates approve | Resolved |
| `join_fanout` | Silent corruption — nothing failed, the numbers are just wrong | Resolved |
| `vendor_outage` | Alert-storm de-duplication — 10 alerts across 9 assets → 1 incident | Resolved |
| `null_explosion` | **Reject path** — the technically correct fix is the wrong business call | Quarantined + ticketed |
| `chronic_lateness` | **Defer path** — third occurrence in 30 days, reframed as backlog | Deferred |
| `transient_timeout` | **Autonomous path** — SEV4, reversible, no code: fixed without waking anyone | Resolved, no gates |
| `ad_spend_drift` | Ordinary approve path that **records a standing decision** | Resolved |
| `ad_spend_drift_repeat` | **Precedent path** — same problem three weeks later, nobody asked | Resolved, no gates |

`transient_timeout` has **deliberately empty scripted responses**. If any gate opened,
nobody would answer it and the incident would escalate — so it passes only because no
gate opened. The test proves the absence of a thing, which is the only way to test that
a system does not do something.

---

## 8. Running it

```bash
cd ~/projects/aegis
source .venv/bin/activate

python -m aegis.cli list                      # the scenarios
python -m aegis.cli run schema_drift          # one incident, live trace
python -m aegis.cli run --all                 # the whole suite
python -m aegis.cli run null_explosion -i     # you play the approvers  <-- Friday demo
python -m aegis.cli run --all --bedrock       # real Claude models (~$0.66)
python -m aegis.cli graph                     # the graph as Mermaid
```

Default is the heuristic backend: deterministic, free, no credentials. `--bedrock` or
`AEGIS_MODEL_BACKEND=bedrock` in `.env` switches to real models.

---

## 9. Build status

### Complete ✅

| Component | File |
|---|---|
| Typed hand-off contracts | `aegis/contracts.py` |
| Config, cost governor, `.env` loading | `aegis/config.py` |
| Redaction firewall, autonomy ladder, severity scoring | `aegis/policy.py` |
| Hash-chained audit trail + trace bus | `aegis/audit.py` |
| Bedrock / heuristic reasoning layer | `aegis/reasoning.py` |
| Simulated Snowflake warehouse (33 assets) | `aegis/platform/world.py` |
| Five scenarios with ground truth | `aegis/platform/scenarios.py` |
| Platform client + action API | `aegis/platform/client.py` |
| Provenance-recording tool belt | `aegis/tools.py` |
| Incident memory / recurrence detection | `aegis/memory.py` |
| Nine agents | `aegis/agents/*.py` |
| Two-gate approval chain, signed tokens | `aegis/approvals.py` |
| Jira / ServiceNow / GitHub / SES adapters + mocks | `aegis/integrations.py` |
| LangGraph orchestration | `aegis/graph.py` |
| CLI with live trace | `aegis/cli.py` |
| Bedrock preflight | `scripts/check_bedrock.py` |

~10,000 lines. Committed to git locally.

| Two-gate approval chain, signed tokens | `aegis/approvals.py` |
| Autonomy ladder (risk + change magnitude + severity) | `aegis/policy.py` |
| Precedent-based autonomy | `aegis/precedent.py` |
| HTML trace report | `aegis/report.py` |
| Eval harness — 118 checks across 3 families | `evals/harness.py` |
| Adversarial governance tests — 94 tests | `tests/test_governance.py`, `tests/test_precedent.py`, `tests/test_outbound_boundary.py` |
| Architecture writeup | `docs/ARCHITECTURE.md` |

~12,700 lines. Pushed to
[github.com/ckumar2281/cg-aegis-incident-agents](https://github.com/ckumar2281/cg-aegis-incident-agents).

### Remaining ⬜

- AgentCore Runtime deployment notes

---

## 9b. The Snowflake client — written 17 Sep, unverified

`aegis/platform/snowflake.py`, ~600 lines, implements the whole `PlatformClient`
protocol against a real warehouse. **It has never been executed.** Nothing imports it,
so it cannot move a single eval check; `SimulatedPlatform` remains what everything runs
against.

### Why write it unverified at all

Two days of this log are corrections to claims that outran evidence, so the bar for
adding another was high. It clears it for one reason: *"the platform is simulated"* is
the weakest sentence in the project, and there are two ways to improve it. Describing
the queries a real client would issue is talk. Writing them down makes the design
inspectable, testable in about a minute once credentials exist, and falsifiable — a
reviewer can read the SQL and tell me it is wrong. The honest phrasing is **"written,
not verified"**, and every document now says exactly that.

### The two decisions worth defending

**Freshness over depth.** `ACCOUNT_USAGE` is the obvious source — 365 days, everything
in one place — and it carries **up to ~2 hours of latency**. An incident-response agent
reading a two-hour-old view is diagnosing the recent past. So operational history comes
from the latency-free `INFORMATION_SCHEMA` **table functions** (`TASK_HISTORY`,
`COPY_HISTORY`, `QUERY_HISTORY`), whose 7–14 day retention comfortably covers the 24–72
hour windows these methods ask for. `ACCOUNT_USAGE` is used only where there is no
alternative — object dependencies, column history, access history, tags — and each use
is marked, because staleness means something different in each case.

**`execute()` refuses to write by default.** `execute_mode="dry_run"` renders the SQL
and runs nothing; `"live"` must be asked for by name. Every mutating action is a
parameterised template in a fixed allow-list, so an agent picks a key and supplies
parameters and never composes SQL. **`drop_table` is absent from the allow-list on
purpose** — the ladder already refuses to pre-authorise it, and not writing the code is
the second lock.

### What Snowflake genuinely cannot answer

Named rather than papered over, in the module header and in `ARCHITECTURE.md` §9:

| Needed | Snowflake's answer | What this does |
|---|---|---|
| Tier, owner, SLA, domain | No native concept | Object **tags**, names configurable |
| 30-day metric series | No native table | Aggregated from scheduled **DMF results** |
| Schema history | No native diff | `ACCOUNT_USAGE.COLUMNS` retains dropped columns with a `DELETED` timestamp |
| Deploys, PRs, vendor notices | Not a warehouse concern | DDL from `QUERY_HISTORY` + an injected VCS feed |
| Credits per asset | Only per warehouse/query | Cloud-services credits per query; understated, and said so |

`metric_summary` returns `available: False` when no DMFs are scheduled rather than
inventing a baseline. An agent told "no baseline exists" reasons better than one handed
a fabricated one.

### What is tested, and what cannot be

`tests/test_snowflake_guards.py` — 12 tests, no connection required. They attack the
three write locks: dry-run default, allow-list, and the deliberate absence of
`drop_table`. The read methods cannot be tested without an account, and a pile of mocks
would only test the mocks.

`scripts/check_snowflake.py` closes the gap: it calls every method against a live
account and prints OK / **EMPTY** / FAIL per method, reporting empty as distinct from
passing — several methods return nothing on a bare account because the optional setup
(tags, scheduled DMFs) is absent, and counting that as success would repeat exactly the
mistake defect 12 was. `--grants` prints the least-privilege role, which grants no
INSERT, UPDATE, DELETE, TRUNCATE or DROP.

**Next step:** a Snowflake trial, then paste that output here. Until then the claim is
"written".

---

## 10. Open items

### Needs a decision

1. **Repo name** — currently `aegis`.
2. **Approver identities for Friday.** Suggested: Gmail plus-addressing
   (`chaitan.gk+po@`, `+sm@`, `+dev@`, `+em@`) — four distinct approvers, one inbox,
   makes the tiered-disclosure effect visible live.

### Service setup

| Service | Needed for | Status |
|---|---|---|
| Bedrock | Agent reasoning | ✅ verified end to end |
| AWS budget alarm | Spend early-warning | ⬜ recommended, $10 threshold |
| Snowflake | Real platform data | ⬜ trial not started |
| GitHub | PR creation demo | ⬜ repo + fine-grained PAT (`pull_requests: write`) |
| SES | Approval emails | ⬜ verify one sender address |
| Jira | Ticket on reject path | ⬜ optional — mock demos the same flow |

---

## 12. Approval as precedent — BUILT ✅

Chaitanya's proposal, 16 Sep: **the first occurrence of a problem asks a human; a
recurrence of the same problem with the same fix applies the decision already made.**
Approval becomes a policy the PO sets once, not an interruption they get every time.

This is the strongest idea in the design and it answers the most serious attack on
approval gates: *they decay into rubber-stamping — by the fifth identical request people
click approve without reading.*

**Deferred until the deliverable spine was finished, then built Thursday morning.**
The sequencing mattered: half-built learned autonomy is worse than none, because if
precedent matching is too loose a reviewer asks *"so it auto-approved something a human
never actually agreed to"*, and that one question damages the governance story including
the parts that are solid.

Built as the narrow version only (`aegis/precedent.py`):

- **same root cause + same asset** — the PO approved a specific situation, not a category
- **SEV2 and below** — a repeat on revenue-critical data still gets a human glance
- **90-day expiry**, and revocable at any time — standing approvals go stale
- the new plan must fit **inside the envelope** that was approved: no extra actions, no
  higher risk tier, no larger code change
- `rollback_deployment`, `restate_table`, `drop_table` and `force_merge_pr` are in
  `NEVER_PRECEDENTED` — not pre-authorisable however many times they were approved before
- every application is audited **citing the precedent and the person who set it**, so
  "the system did this on its own" is never the whole answer

### How it reads in the trace

```
autonomy    supervisor   Plan contains always-gated action(s): release_quarantine.
precedent   supervisor   standing approval applies -- product owner approved this
                         exact situation on 2026-08-26 (incident INC-AD-SPEND-DRIFT);
                         that decision stands until 2026-11-24
autonomous  supervisor   proceeding without human approval
```

The ladder says gate it; the precedent overrides with attribution. When a precedent
exists but does **not** cover the plan, that is traced too — silence would be
indistinguishable from there never having been one.

### Demonstrated by a scenario pair

- `ad_spend_drift` — AdBridge renames a column, SEV3, PO approves, resolves cleanly →
  **a standing decision is recorded**
- `ad_spend_drift_repeat` — same vendor, same feed, three weeks later → **nobody is
  asked**. Its scripted responses are deliberately empty, so it passes only because no
  gate opened

### The boundaries are the feature, so that is what is tested

22 tests in `tests/test_precedent.py`, almost all asserting a refusal:

| Attack | Refused because |
|---|---|
| Same cause, different asset | The PO approved a situation, not a category |
| SEV1 recurrence | Never covered, however familiar |
| Plan adds `backfill_table` | Not in the approved envelope |
| Plan reaches a higher risk tier | Beyond what was sanctioned |
| Additive approval → destructive change | Not the same decision |
| 200 days old | Expired |
| Revoked | Immediate |
| Approved twice, newer is tighter | The later decision governs |

---

## 11. Reference

- [Bedrock console](https://console.aws.amazon.com/bedrock/) · [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/)
- [Inference profile prerequisites and IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)
- [Multi-agent SRE reference architecture on AgentCore](https://aws.amazon.com/blogs/machine-learning/build-multi-agent-site-reliability-engineering-assistants-with-amazon-bedrock-agentcore/)
- [Snowflake trial signup](https://signup.snowflake.com/)

### Appendix — production IAM policy (not used; the API key made it unnecessary)

Cross-region inference profiles need permission on **both** the inference-profile ARN
and the underlying foundation-model ARN. A policy naming only the profile fails with a
misleading `AccessDenied`.

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

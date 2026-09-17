# Agent narration — what to say, line by line

`RUN-SHEET.md` is the timing. This is the depth: every line the flagship run prints,
what it means, and the question it invites.

Run it with `-v` or the four hand-off lines do not appear.

---

## The situation, in twenty seconds

> "Stripe shipped an API change at 06:12. The charges export renames `currency_code` to
> `currency`. They announced it five days earlier and nobody read the notice. The file
> that lands is valid parquet — it just violates the contract our pipeline maps against.
> The load fails, the payments task fails, freshness monitors on the revenue marts start
> firing. Six alerts in twelve minutes from four systems. It's month-end close."

The trap, worth stating before the run:

> "The obvious fix is a rollback. It's wrong — Stripe isn't reverting, so tomorrow's file
> breaks the same way. The correct fix is a contract bump plus a mapping change, then
> reprocess the file we held."

---

## `quarantine  system  file FILE-…-STRIPE-0001 held before the warehouse`

> "Containment runs first, before any agent. Nothing has reasoned about this yet — we
> just don't let it into the warehouse while we think."

**Q: what if there's no file?** The node says so and moves on. Run `transient_timeout`
and you'll see `no file involved — nothing to hold back`. The graph never decides whether
to *try* to contain; a routing bug there would let a bad file through.

## `quarantine  storage  object moved to s3://cg-aegis-quarantine-…`

> "That's a real 6MB object, really moved between two real buckets. Copy, then delete the
> source. Containment is a move, not a flag."

**Q: what if the move fails?** You'd see `containment incomplete — …` and the incident
would carry on and record that the bytes did not move. Saying "quarantined" when nothing
moved is the class of untrue claim this project kept finding in itself.

---

## `triage  6 alerts -> 1 incident (SEV1)`

> "Six alerts fired from four systems. A human on call opens six tickets or mutes the
> channel. Triage returns **one** incident, with a severity and a blast radius —
> 83% deduplication."

Triage answers the four textbook questions and **stops**: what happened, how serious,
what's affected, where does this go next. It does not diagnose and it does not fix.

**Q: how does it know they're one incident?** Deduplication needs lineage — you can only
fold an alert on `MART.ARR_SUMMARY` into an incident rooted at `RAW.STRIPE_CHARGES` if
you know one reaches the other. That's why correlation lives in the agent layer and not
in the Lambda that receives the alert.

**Q: why SEV1?** Not because it's loud. Severity is scored from weighted tier-1
downstream assets, consumer surfaces, SLA breaches, rows affected, financial proximity
and whether the data is *wrong* versus merely *late*. An early version treated
reachability as materiality — everything eventually reaches the exec dashboard, so
everything scored SEV1 and the scale stopped carrying information. Defect 2.

## The four `handoff  triage -> …` lines

**This is the slide-worthy moment.** Point at them.

> "Triage doesn't hand over a blob of context and hope. It writes a **numbered question
> for each specialist**, and each one is accountable for answering it."

```
D1 → lineage   Map the blast radius of RAW.STRIPE_CHARGES: which downstream assets,
               which are tier-1, which consumer surfaces, which SLAs already breached.
D4 → change    Every deploy, config change, vendor notice or registry update in 72h
               touching this asset or its upstreams, and how well each correlates
               in time with onset.
```

> "These cross as validated Pydantic contracts, not strings. A report that doesn't
> address its directives is *incomplete*, and RCA can mint new directives to reopen the
> investigation."

---

## The four `specialist` lines

They run **concurrently in one superstep** — one LangGraph step, four agents, merged
through a list reducer.

| Line | Reads | Answers |
|---|---|---|
| `change_correlator  1 findings, 1 hypotheses` | Deploys, config, vendor notices, DDL history | **Finds the vendor notice from five days ago** |
| `pipeline_forensics  1 findings, 2 hypotheses` | Task history, load history | The load failed and the task failed, both naming `CURRENCY_CODE` |
| `lineage_impact_analyst  4 findings, 0 hypotheses` | Dependencies, tags, access history | 13 downstream, 9 tier-1, 5 consumer surfaces, 3 SLA breaches |
| `data_quality_forensics  2 findings, 1 hypotheses` | Metric baselines, quality monitor results | Row count, nulls and freshness against a 30-day baseline |

**Q: why does lineage return 0 hypotheses?** Because blast radius is not a cause. It
tells you how much this *matters*, not what broke. An agent that invented a hypothesis
to look useful would be adding noise to a score built on agreement.

> "Each reads different data and answers a different question. That's what makes them
> agents rather than functions — and it's what the confidence number is made of."

**Q: why not one prompt with tools?** Confidence here is agreement between *independent*
specialists. A single call cannot corroborate itself — the agreement term would be 1 for
everything, always.

---

## `rca  upstream_schema_drift @ 61% -> dig_deeper`

> "Confidence was short of the bar, so instead of guessing it sent **fresh directives**
> back to the specialists. Bounded at two rounds — it cannot loop forever."

> "And the number is arithmetic, not a model's feeling. Agreement between independent
> proposers raises the prior; refuting evidence counts **double**, because 'every task
> succeeded' should *kill* the infrastructure hypothesis, not just rank it lower."

**Q: can the model override it?** Only downward, really. It may lower computed confidence
by up to 0.15 and raise it by at most 0.05. The asymmetry is the safety property: the
failure mode is an extra investigation round, not a wrong fix on production data.

**Q: why is live confidence lower than the deterministic run?** Because the models
genuinely disagree more than the heuristic does. Below 40% it escalates to a human
instead of proceeding. **If that happens live, say so — it is the gate working.**

---

## `planner  4 data steps, 2 code changes (max tier: mutating)`

> "Pin the contract to v4, release the quarantine, reprocess the file, re-run the task —
> plus a mapping change and a contract bump in the dbt repo. Every step is risk-tiered."

Note what it did **not** propose: a rollback.

## `autonomy  supervisor  Plan contains always-gated action(s): release_quarantine.`

> "This is the autonomy ladder, and it's real control flow, not a log line. Some actions
> are always gated no matter how confident the system is — releasing quarantined data is
> one. No score unlocks it."

**Q: doesn't that defeat automation?** Run `transient_timeout` — a retry on a tier-3
asset is reversible and touches no code, so nobody is woken at all. Same graph, same
agents. The ladder decides, and if it asked permission for everything, people would
switch it off.

**Defect 5 is the story here:** the ladder computed the decision, traced it, audited it —
and an unconditional edge ignored it. The trace said "executing autonomously" and then
asked four people anyway. *A decision that nothing routes on is a log line.*

---

## `disclosure  business brief cleared the redaction firewall`

> "One verdict, two audiences. The business brief is scanned against thirteen rules
> before it can be sent — no table names, no columns, no code, no credentials. Machine
> enforced at the delivery boundary, so no future code path can leak by forgetting a flag."

**Defects 7–8, the best story in the deck:** the rule meant to stop database object names
required *three* dotted parts — `DB.SCHEMA.TABLE`. Every object in this warehouse is two.
**The rule had never fired on a single real table name**, and it looked like a working
control in every trace, because it was never asked to block anything.

## `gate_open  business_gate opened for product_owner (business tier)`

> "The Product Owner is asked first, and is told only what the business needs to decide:
> eight business-critical processes are on stale figures during month-end close."

## `disclosure_released  technical fix packet released to the dev team`

**The line to land:**

> "The technical packet **did not exist** for the dev team until the business approved.
> This is the ordering the whole system is built around."

Then show the two emails side by side.

## `gate_open  technical_gate … (developer, engineering_manager)`

> "Only now. Two approvers, technical tier, and they see the diff."

---

## `execute  S1 pin_schema_version  verified` ×4

> "Each step is **verified against the platform afterwards**, not assumed to have worked.
> The executor and the verifier are the same agent asking two different questions."

## `pull_request  opened PR #1901`

> "The data is recovered, but the cause isn't fixed until this is merged — which is why
> the residual risk line says the same incident can recur before then. The system does
> not claim the problem is closed."

## `audit chain  31 events  valid`

> "Every state transition, disclosure and approval, SHA-256 hash-chained — each event
> carries the digest of the one before. Tamper with history and `verify()` tells you
> which record broke. That's how you answer, months later: **who approved releasing
> production data, and what were they shown at the time?**"

---

## Three questions you will get

**"How much did you write versus the AI?"**
> The governance model, the approval rules, the scenarios and every judgement about what
> to trust are mine. The defect list is the evidence — catching that the autonomy ladder
> was decorative, or that my own test suite could delete the demo's data, isn't something
> that happens to someone who was handed code.

**"What happens when the LLM gets it wrong?"**
> Three things catch it. Confidence below the floor escalates instead of proceeding.
> Always-gated actions need a human regardless of confidence. And every remediation step
> is verified against the platform afterwards. The model is never the last word on
> whether something worked.

**"Is it production ready?"**
> No, and I can tell you exactly where the line is. The containment decision is scripted,
> the Snowflake load is a fixture, and the approval callback endpoint isn't built. The
> reasoning, the emails, the object movement and the platform client are real.

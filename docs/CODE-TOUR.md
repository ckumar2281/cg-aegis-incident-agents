# Code Tour

**For the person presenting this.** Every claim the other documents make, mapped to the
file and function that makes it true — so that when someone says *"show me where that
happens"*, you open one file and point at one function.

Read this with the repo open. It is organised the way a reviewer's questions arrive, not
the way the code is laid out.

---

## 0. The ninety-second version

If you remember one paragraph, make it this one:

> Nine agents investigate a data incident in parallel, hand typed Pydantic contracts to
> each other rather than free text, and score competing explanations arithmetically. The
> system then **refuses to send the fix to an engineer until the business has approved
> the problem** — and that refusal is enforced by a regex firewall at the send boundary,
> not by asking a model nicely. Once a human rules on a situation, a recurrence applies
> their decision by name instead of asking again.

Everything below is evidence for that paragraph.

---

## 1. Reading order — 20 minutes, in this sequence

| # | File | Why it comes here | What to notice |
|---|---|---|---|
| 1 | `aegis/contracts.py` | The vocabulary. Nothing else makes sense first | Every hand-off is a `BaseModel`. `SpecialistReport` has `unresolved_directives` — admitting ignorance is in the schema |
| 2 | `aegis/graph.py` L217 `build_graph` | The topology, in one screen | The fan-out, the conditional edge out of `rca`, the three-way gate exits |
| 3 | `aegis/policy.py` | The governance core, ~400 lines, no model calls | `BUSINESS_TIER_RULES` (L57), `AutonomyLadder.evaluate` (L265), `score_severity` (L333) |
| 4 | `aegis/agents/rca.py` L201 `_score` | The one piece of real arithmetic | `share × mass`, refutation at double weight |
| 5 | `aegis/precedent.py` L214 `find` | The innovation claim | Read the *rejection* branches, not the match branch |
| 6 | `tests/test_governance.py` | Proof the controls work | Every test constructs the leak it prevents |

Stop there. The four forensic specialists in `agents/forensics.py` are 888 lines of
domain playbook and nobody will ask you to defend them line by line.

---

## 2. Claim → code

Anything in the README or `ARCHITECTURE.md` that sounds impressive, and where it lives.

### The governance claims

| Claim | Where | The one-liner |
|---|---|---|
| "The developer does not receive the fix until the business has approved" | `graph.py` L555 `route_after_business` → L565 `technical_gate`; enforced in `integrations.py` L77 `send_approval_request` | The graph *routes* it; the sink *refuses* it. Two independent mechanisms, deliberately |
| "The firewall is machine-enforced, not a prompt instruction" | `policy.py` L57 `BUSINESS_TIER_RULES` — 13 compiled regexes | Point at the tuple. It is 100 lines of regex and zero model calls |
| "A business brief that trips a rule is rejected and regenerated" | `agents/disclosure.py` L91 `run` | One repair attempt with findings fed back, then escalate |
| "If it fails twice the incident escalates rather than over-disclosing" | `graph.py` L510 `route_after_disclosure` → L516 `disclosure_failed` | The fail-closed path is a node, so it is visible in the trace |
| "Every business-tier message is checked on the way out" | `integrations.py` L77 and L121 | Added after the live run found subjects were never checked — see §5 |
| "Tier is derived, never asserted" | `contracts.py` L93 `HumanRole.tier` | One source of truth; a node cannot relabel its own message |
| "The audit trail can prove what each audience saw" | `audit.py` L50 `AuditChain`, L90 `verify` | SHA-256, each event carries the previous digest |
| "The autonomy ladder decides per incident" | `policy.py` L265 `AutonomyLadder.evaluate` | Four inputs, early returns, reads top to bottom |
| "Approval as a standing decision" | `precedent.py` L214 `find` | The boundaries are the feature — see below |

### The multi-agent claims

| Claim | Where |
|---|---|
| Four specialists run in parallel | `graph.py` L217 — four `add_edge("triage", …)` calls, merged by `Annotated[list, operator.add]` on `GraphState.reports` (L108) |
| "Directives, not context dumps" | `agents/triage.py` L369 `_directives` — each specialist gets numbered questions it is accountable for |
| "A report that does not address its directives is rejected" | `contracts.py` L352 `SpecialistReport.unresolved_directives` + `agents/rca.py` L308 `_default_follow_ups` |
| "Agreement between independent specialists is evidence" | `agents/rca.py` L236 — `raw = best.prior * agreement`, where agreement rises with the number of distinct agents proposing the tag |
| "Refutation outweighs support, deliberately" | `agents/rca.py` L201 `_score` — support `+0.15`, refutation `−0.30` |
| "The model can doubt more easily than it can assert" | `agents/rca.py` L66 `RCAJudgement` — the confidence adjustment field is bounded `ge=-0.15, le=0.05` |
| "The loop is bounded at 2 rounds" | `graph.py` L294 `route_after_rca` + L302 `prepare_round_two` |

### The cost claims

| Claim | Where |
|---|---|
| Model tiering per role | `config.py` L197 `model_for` |
| Per-incident budget governor | `config.py` L220 `BudgetLedger`, L249 `would_breach` |
| Degrades honestly rather than failing | `reasoning.py` L198 `_should_use_model` → `config.py` L262 `mark_degraded` |
| Prompt caching | `reasoning.py` L145 `converse` — `cachePoint` on the static system block |
| "No vector database" | `memory.py` L82 `_score` — token overlap plus recency, ~20 lines |

---

## 3. Trace one incident through the code

This is the demo narrative. Run `python -m aegis.cli run schema_drift` and follow along.

```
graph.py:222   quarantine    Bad data is contained BEFORE any agent runs. No approval
                             needed to NOT load something — the fail-safe is free
graph.py:257   triage        10 alerts → 1 incident. triage.py:247 _correlate folds
                             the cascade; :289 _blast_radius walks lineage;
                             policy.py:333 score_severity uses distance_weight so
                             reachability ≠ materiality
graph.py:269   ×4 specialists  Same superstep, different data. forensics.py:278/357/
                             550/751 are the four investigate() methods
graph.py:284   rca           rca.py:201 _score ranks hypotheses; :269 _decide returns
                             proceed / dig_deeper / escalate
graph.py:354   plan          planner.py:75 run dispatches to a playbook by root-cause
                             tag. Risk tier per step is set here, not later
graph.py:445   route_after_plan   ← THE LADDER FIRES HERE. policy.py:265 decides
                             gates-or-autonomous. This edge was once unconditional;
                             see ARCHITECTURE §7 defect 5
graph.py:505   disclose      One verdict → two renderings. disclosure.py:150 renders
                             the brief, :327 the technical packet, and the firewall
                             runs between them
graph.py:545   business_gate       PO sees the brief. Nothing technical has been sent
graph.py:565   technical_gate      Released only now. approvals.py:417
                             release_technical_tier is the single place that flips it
graph.py:702   execute       executor.py:48 run — verify each step, roll back on
                             failure (:286), open the PR (:311)
graph.py:712   close         Notify both tiers, then record the precedent
```

**The two lines to point at during the demo** are `graph.py` L445 and L555. Those two
conditional edges are the entire governance model expressed as routing.

---

## 4. Where the interesting judgement calls live

Reviewers reward knowing *why*, not *what*. Five places where a decision was made that
could reasonably have gone the other way:

**`rca.py` L258 — why confidence is `share × mass`.** `share` answers "how much of the
total weight does this hypothesis hold". On its own the last surviving hypothesis scores
1.0 no matter how thin the evidence. `mass = total/(total + 0.30)` answers the question
`share` cannot: *is there enough evidence here to be confident at all?* This shipped
broken and is defect 1 in `ARCHITECTURE.md` §7.

**`policy.py` L417 `distance_weight` — why reachability is not materiality.** Everything
in a warehouse eventually reaches the exec dashboard. Without distance decay every
incident scored SEV1 and the severity scale carried no information. Defect 2.

**`forensics.py` L751 — why the change correlator only considers upstream changes.**
Data flows one way. A mart refactor cannot explain a fault three hops above it. Restricting
causal scope to `{primary} | upstreams(primary)` is one line and fixes a whole class of
wrong answers. Defect 3.

**`policy.py` diff rule — why it does *not* match bare `+ ` / `- ` prefixes.** Those are
markdown bullets. A control that rejects every bulleted list gets switched off within a
week, and a disabled control is worse than a narrow one. Deliberate restraint, documented
as such.

**`precedent.py` L58 `NEVER_PRECEDENTED`.** Four actions can never be pre-authorised
however familiar the incident looks. Approval-as-precedent is the feature most likely to
be attacked, and the answer is that its boundaries are hard-coded and tested.

---

## 5. The defect list is the strongest thing you have

`ARCHITECTURE.md` §7 lists **eleven defects found during the build**, with what each one
taught. Counter-intuitive, but lead with it if the conversation goes technical: a
candidate who found eleven bugs in their own work and wrote them down is more credible
than one whose demo simply worked.

Three of them are worth being able to tell as a story:

**Defect 5 — the autonomy ladder was decorative.** It computed the decision, traced it,
audited it, and then `add_edge("plan", "disclose")` ignored it. The trace said
*"executing autonomously"* and then asked four people anyway. **Lesson: a decision that
nothing routes on is a log line.**

**Defects 7–8 — the firewall had never fired.** The rule meant to stop database object
names required *three* dotted parts (`DB.SCHEMA.TABLE`). Every object in this warehouse
is two (`MART.DAILY_REVENUE`). It looked like a working control in every trace because it
was never asked to block anything. **Lesson: a control that has never been attacked is
untested, not working.**

**Defects 9–11 — found by the first live Bedrock run, not by the eval suite.** The run
succeeded on every ground-truth measure, and its disclosure table printed a *business
tier* row reading `[resolved] RAW.STRIPE_CHARGES schema drift: CURRENCY_CODE...`. Three
stacked problems: the tier was defaulted rather than derived (so five of seven messages
per incident were mislabelled, blinding the check that filters on tier); nothing had ever
firewall-checked a *rendered* email, only the brief's individual fields; and two rules
were over-broad in ways only a rendered email could reveal — one of them would have
blocked the approval link in the PO's own email. **Lesson: guard the artifact that
actually leaves, not the one you happen to have a schema for.**

The eval score went **up** afterwards, 112 → 118, because two of the new checks could not
previously have failed.

---

## 6. Questions you will be asked

**"Why not one prompt with tools?"**
> Because agreement between independent specialists is the evidence the confidence score
> is built from, and a single call cannot corroborate itself. `rca.py` L236 — the
> agreement multiplier is literally a function of how many distinct agents proposed a
> tag. Collapse the agents and that term goes to 1 for everything.

**"Is this actually multi-agent or just functions?"**
> Each agent owns a different question, reads different data, and hands over a typed
> contract rather than a string. Four run concurrently in one LangGraph superstep and
> merge through a list reducer. And the graph loops — `route_after_rca` sends fresh
> directives back when confidence is short, which is control flow a pipeline of functions
> does not have.

**"The heuristic backend — isn't that a stub that does the real work?"**
> Every agent computes a structured answer in Python first, then the model improves it.
> That is deliberate and it earns its place three times: development and the whole eval
> suite run at **$0**; output is byte-identical so orchestration regressions are visible
> rather than lost in model variance; and it is the failure path — throttling, bad JSON
> or the budget ceiling all degrade to it, stamped into the audit trail. The live Bedrock
> run is real and the numbers are in `PROJECT-LOG.md` §4.

**"So is Snowflake connected?"**
> **No, and say so plainly.** The data platform is simulated. `platform/client.py`
> defines a `PlatformClient` protocol and `SimulatedPlatform` is the only implementation;
> a Snowflake one would implement the same protocol against `ACCOUNT_USAGE` views and
> Horizon lineage, and **is not written**. Bedrock is real. The integrations — SES, Jira,
> ServiceNow, GitHub — have real adapters and run mocked until credentials are supplied.
> Do not soften this. It is the single easiest thing to get caught on, and the protocol
> boundary is the honest answer to why it would not be a rewrite.

**"How do you know it works?"**
> Two independent mechanisms. Eight scenarios with declared ground truth scored by a
> harness — 118 checks across diagnosis, governance and remediation, all passing. And 82
> adversarial tests that construct the leak they are trying to prevent. **Two scenarios
> prove a negative**: `transient_timeout` and `ad_spend_drift_repeat` have deliberately
> empty scripted approvals, so if any gate opened nobody would answer and the incident
> would escalate. They pass only because no gate opened.

**"What does a run cost?"**
> $0.16 for the live run, 22 calls. Worth being precise: RCA came back `dig_deeper` at
> 62%, so all four specialists ran a second round. Per-call cost matched the estimate —
> the round count moved the total. A single-round incident is nearer $0.09. Development
> and the eval suite cost nothing because they run on the heuristic backend.

**"What would you do next?"**
> Write the real `SnowflakePlatform` against the protocol. Then replace the scripted
> approvals with real SES email and signed links — `approvals.py` L80 `TokenMinter`
> already mints and verifies HMAC tokens with a TTL, so the mechanism exists and only
> delivery is mocked. Then AgentCore Runtime, where the one real architectural change is
> that the approval flow must suspend rather than block — `DEPLOYMENT.md` §6.

**"What is the weakest part?"**
> Pick one and answer honestly. Fair candidates: the root-cause playbooks in `planner.py`
> are per-tag and would need to generalise; the eight scenarios are ones I wrote, so
> ground truth and implementation share an author; and precedent matching is exact-match
> on root cause plus asset, which is safe but will miss recurrences a human would
> recognise.

---

## 7. If you are asked something you do not know

Say so, then say where you would look. That is a better answer than a guess, and this
repo is navigable enough to make it credible:

- *"The contracts are in `contracts.py` — let me check what that field actually is."*
- *"§7 of the architecture doc is the defect log; if it went wrong during the build it is
  written down there."*
- *"`PROJECT-LOG.md` has the build history with every decision and why."*

Nobody expects total recall of 13,000 lines. They are checking whether you built it.

---

## 8. Demo pacing

| | |
|---|---|
| Heuristic backend | ~0.3s per scenario, byte-identical every run |
| Live Bedrock | ~119s per incident |

**Lead on the heuristic backend, then run exactly one live Bedrock incident** to show it
is real. Do not run the suite live. Suggested order:

1. `python -m aegis.cli run schema_drift` — the full approve path, and the "who was told
   what" table at the end is the governance model in one screen
2. `python -m aegis.cli run null_explosion` — the reject path: the technically correct
   fix is the wrong business call
3. `python -m aegis.cli run ad_spend_drift_repeat` — precedent: nobody was asked, and the
   trace cites who decided and when
4. `python -m evals.harness` — 118/118, three seconds
5. `python -m aegis.cli run schema_drift --bedrock` — the live one, while you talk over it

Have `python -m aegis.cli run --all --report` run beforehand so the HTML trace report is
already on disk if anyone wants to scroll through it afterwards.

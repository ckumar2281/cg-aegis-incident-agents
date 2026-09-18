# Learning path — understanding Aegis from scratch

Thirteen sessions, roughly six hours end to end. Each one says what to **run**, what to
**read**, and a **checkpoint** — questions you should be able to answer before moving on.
If you can't answer the checkpoint, reread that session rather than pressing on; every
later session assumes the earlier ones.

**Do not start by reading code.** Start by watching the system run. The trace is the best
index into this codebase there is: every line you see printed corresponds to a place in
the code, and once you know what the lines mean the files stop being a wall.

> **In a hurry?** Sessions 1, 2, 3 and 5 (about 100 minutes) give you enough to discuss
> the system intelligently. Everything after that is depth.

---

## Session 0 · Make it run · 10 min

```bash
cd ~/projects/aegis
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                       # expect 124 passing
python -m aegis.cli list                  # the eight scenarios
```

No credentials are needed for any of this. `AEGIS_MODEL_BACKEND=heuristic` runs the whole
system deterministically at $0, which is not a toy mode — it is how the eval suite scores
118/118 without depending on a live account.

**Checkpoint.** Why does a deterministic backend exist at all? (Because a system whose
behaviour you cannot reproduce cannot be regression-tested, and because a demo that costs
money every time you rehearse is a demo you rehearse less.)

---

## Session 1 · Watch it, twice · 20 min

```bash
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run schema_drift -v
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run transient_timeout -v
```

Do not try to understand the code yet. Just notice that the second run **skips
two-thirds of the lines** the first one printed — no disclosure, no gates, no approvals.
Same graph, same agents, different path.

Then read **`docs/AGENT-NARRATION.md`** with that trace still on screen. It is written
against those exact lines.

**Checkpoint.** What is the difference between the two incidents that made one of them
skip the gates? Which line in the trace announced that decision?

---

## Session 2 · The contracts · 25 min

**Read `aegis/contracts.py`** — class definitions only, skip the methods.

This is the highest-leverage file in the project. Every message that crosses an agent
boundary is one of these models. Once you hold them in your head, every other file is a
function that takes one and returns another.

The ones that matter: `IncidentPacket`, `Directive`, `SpecialistReport`, `Hypothesis`,
`RCAVerdict`, `RemediationPlan`, `DisclosureBundle`, `ApprovalRequest`, `Evidence`,
`ToolCall`. Plus the enums that encode the governance: `HumanRole`, `DisclosureTier`,
`GateKind`, `GateVerdict`, `Severity`, `RiskTier`, `ChangeMagnitude`.

**Checkpoint.** Which contract does triage produce, and which one does each specialist
return? What does `unresolved_directives` on a `SpecialistReport` cause to happen? Why is
`HumanRole.tier` a property of the role rather than a parameter you pass to a send?

---

## Session 3 · The two guards · 25 min

**Read `aegis/policy.py`.** It is short and it is what the project is about.

- `RedactionPolicy` — thirteen rules. Read them as regexes and ask of each one: *what
  would get past this?* That question is what found defects 7, 8 and 11.
- `AutonomyLadder.evaluate` — the four checks, in order. Note the default posture is
  **closed**: gates always, autonomy granted only in a narrow case it has to earn.
- `score_severity` — arithmetic, not a model judgement, so a reviewer can ask "why SEV1"
  and get an answer that does not vary between runs.

**Checkpoint.** Name the seven always-gated actions. Why are cosmetic code changes *not*
gated — and what does that have in common with the precedent store? What does
`distance_weight` do, and why does correlation need it?

---

## Session 4 · Orchestration · 25 min

**Read `aegis/graph.py`**, but only these: the `Runtime` dataclass, `build_runtime`, and
the five routing functions — `route_after_rca` (line ~355), `route_after_plan` (~506),
`route_after_disclosure`, `route_after_business`, `route_after_technical`.

Skip the node bodies for now. **The routing functions are where the system decides**, and
they are the dashed edges on `docs/graph.html`. Open that page beside the code.

**Checkpoint.** Which routing function can return a *list* rather than a string, and why
does that matter? What stops the RCA loop running forever? Which node runs before every
incident regardless, and what is the safety argument for making it unconditional?

---

## Session 5 · One agent, end to end · 30 min

**Read `aegis/agents/base.py`** (151 lines) then **`aegis/agents/triage.py`**.

`base.py` is the shape every agent shares: gather facts through tools, compute an answer
in Python, then ask the model to improve on it. That separation is why every agent is
testable without a model, and why the heuristic backend exists.

In `triage.py`, follow `run()` top to bottom: `_correlate`, `_classify`,
`_blast_radius`, `_proximity`, `score_severity`, then the directives and the `handoff`
calls at the end.

**Checkpoint.** Triage answers four questions and stops. Which four? Why does alert
deduplication need lineage — and what does that imply about where correlation belongs in
a production deployment?

---

## Session 6 · How an agent thinks · 25 min

**Read `aegis/reasoning.py`** and **`aegis/tools.py`**.

Two rules to take away. *Facts come from tools, judgement comes from the model* — every
`Evidence` carries the `ToolCall`s that produced it. And every model call passes a
`fallback` callable, so there is always a deterministic answer computed first that the
model is merely improving on.

Also read the `CostLedger` and the module docstring's note about prompt caching being
**inert** — a documented optimisation that has never executed is a claim, not an
optimisation.

**Checkpoint.** What happens if Bedrock returns malformed JSON? What happens when the
budget ceiling is hit mid-incident? Why is `requestMetadata` set on every call?

---

## Session 7 · The specialists and the arithmetic · 35 min

**Read `aegis/agents/forensics.py`** (all four specialists — they share `_Specialist`)
then **`aegis/agents/rca.py`**, concentrating on the scoring block.

```
raw   = best_prior × (1 + 0.25 × (independent_proposers − 1))
      + 0.15 × Σ supporting − 0.30 × Σ refuting
share = raw / Σ raw            mass = Σ raw / (Σ raw + 0.30)
posterior = share × mass
```

**Checkpoint.** Why does refutation count double? Why does `mass` exist — what goes wrong
with `share` alone? Why may the model lower confidence by 0.15 but raise it by only 0.05?
Why does the lineage analyst return zero hypotheses?

---

## Session 8 · Disclosure and approvals · 35 min

**Read `aegis/agents/disclosure.py`**, then **`aegis/approvals.py`**, then the email half
of **`aegis/integrations.py`**.

This is the governance core. Follow one path: the disclosure officer builds two tiers →
the firewall scans the business brief → `ApprovalCoordinator.run_gate` mints a token and
sends → `EmailSink.send_approval_request` refuses a technical-tier send that has not been
released → a verdict comes back → the technical tier is released.

Note where the firewall is enforced: at the **delivery boundary**, not by convention. No
future code path can leak by forgetting a flag.

**Checkpoint.** What exactly makes a technical-tier send legal? Why is a
`DisclosureViolation` fatal while a transport failure is not? What is
`Responder.depends_on_delivery` for, and what would go wrong without it?

---

## Session 9 · The world, and precedent · 30 min

**Read `aegis/platform/client.py`** (the interface), skim **`world.py`**, then read
**`aegis/precedent.py`** and **`aegis/memory.py`**.

`PlatformClient` is the seam: fourteen methods, agents never touch a cursor. `precedent.py`
is the answer to gate decay — by the fifth identical request people click approve without
reading, and the control becomes theatre.

**Checkpoint.** What are the three things a precedent must carry to be safe? What must a
precedent *not* be allowed to cover? Why does `PrecedentStore` belong in the warehouse
rather than in agent memory?

---

## Session 10 · The tests are the argument · 25 min

**Read `tests/test_governance.py`** and **`tests/test_outbound_boundary.py`**.

Every test here constructs the leak or the failure it guards against. A test that only
asserted the happy path would have passed on all fifteen defects — which is exactly what
happened for weeks.

Then run the scorecard:

```bash
python -m evals.harness
```

**Checkpoint.** Why did the eval score go *up* after two firewall holes were fixed? What
is the difference between what `evals/harness.py` measures and what `tests/` measures?

---

## Session 11 · The real edges · 30 min

**Read `aegis/platform/storage.py`**, **`aegis/platform/snowflake.py`** (skim), and
`scripts/check_snowflake.py`.

One design language runs through all of them: **dry run by default, a fixed allow-list,
no destructive primitive exposed, and EMPTY reported separately from FAIL.** `S3Storage`
has no delete method at all — delete exists only as a step inside `move`.

```bash
python scripts/inspect_landing.py
```

**Checkpoint.** Why does `move` refuse to overwrite an existing target? Why is a
true negative reported differently from a failure? Why is `drop_table` absent from the
Snowflake allow-list rather than merely gated?

---

## Session 12 · What went wrong · 40 min

**Read `docs/PROJECT-LOG.md` §6 and `docs/ARCHITECTURE.md` §7** — all fifteen defects.

This is the most instructive reading in the repo, and it is where the design decisions
are actually explained. Four patterns recur:

1. **A control that has never fired looks identical to one that works.** (7, 8)
2. **A computed decision nothing routes on is a log line.** (5, 15)
3. **Anything that can quietly degrade must say so where someone is looking.** (9, 14, 15)
4. **A claim that outran its evidence is corrected, not quietly dropped.** (12, and the
   §9b heading)

**Checkpoint.** Pick any three defects and say, for each, what class of bug it was and
what would have caught it earlier.

---

## Session 13 · How it would really run · 30 min

**Read `docs/DEPLOYMENT.md`** — §3 and §3a especially.

Note what is *rejected* and why: Gateway, Code Interpreter, AgentCore Memory for
precedents, and a pre-load schema validator. Rejections with reasons are where the design
judgement is.

**Checkpoint.** Why is `COPY` a better schema gate than a validator Lambda? Why do
containment and detection have to be separate? What breaks if approvals become
asynchronous and there is no checkpointer?

---

## When you are done

You should be able to answer these without looking anything up:

- Where does the system decide? *(Five routing functions in `graph.py`.)*
- What stops it leaking? *(`RedactionPolicy`, enforced at the delivery boundary.)*
- What stops it acting? *(`AutonomyLadder`, default closed, seven always-gated actions.)*
- What stops it asking too often? *(`PrecedentStore`, plus not gating cosmetic changes.)*
- How does it know it is right? *(`share × mass` over independent agreement — and it
  doesn't fully know, which is why below 40% it escalates.)*
- How would you prove any of that to an auditor? *(The hash chain, and `verify()`.)*

---

## Reference order

| Document | Answers |
|---|---|
| `AGENT-NARRATION.md` | What each trace line means |
| `CODEBASE-MAP.md` | Which file does what |
| `ARCHITECTURE.md` | Why it is built this way |
| `AWS-SETUP.md` | Bedrock, models, cost, observability |
| `DEPLOYMENT.md` | How it would run in production |
| `PROJECT-LOG.md` | Everything that went wrong |
| `DEMO-GUIDE.md` / `RUN-SHEET.md` | Presenting it |

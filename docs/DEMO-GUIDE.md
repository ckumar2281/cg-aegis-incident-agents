# Demo guide — how to run it and how to talk about it

For Friday. Everything you need to present this confidently, including the questions
that are hard to answer and how to answer them honestly.

**Read this once before the review.** Not to memorise — to know where things are so you
can answer from understanding rather than recall.

---

## 1. The 30-second framing

Open with this. It sets up everything else.

> "I built a multi-agent system for data pipeline incidents. Nine specialised agents
> detect, investigate and fix problems — but the interesting part isn't the diagnosis,
> it's the governance. **Business approval gates technical disclosure.** The Product
> Owner decides whether a fix proceeds while seeing only what the incident means for the
> business. The developer doesn't even receive the fix until that decision is made.
> And the system learns which decisions it no longer needs to ask about."

If they only remember one sentence, make it the bolded one. It's the thing nobody else
will have built.

**Why this domain:** the expensive failure in a data platform isn't slowness, it's
confident wrongness. A service that goes down announces itself; a pipeline that quietly
writes inflated revenue into the table finance closes the books from does not.

---

## 2. The demo sequence — about 10 minutes

```bash
cd ~/projects/aegis
source .venv/bin/activate
```

### Run 1 — the full path (3 min)

```bash
python -m aegis.cli run schema_drift
```

**What to say while it scrolls:**

- *"Six alerts came in. Triage folded them into one incident — it uses lineage, so if
  asset B is downstream of A, B's alert is a cascade, not a separate problem."*
- *"Four specialists just ran in parallel. They read different data on purpose — when
  two of them agree from different evidence, that agreement means something."*
- *"84% confidence, so it proceeds. Below threshold it would loop and investigate again."*
- **Point at the `disclosure` line:** *"The business brief passed a redaction firewall —
  that's a machine check, not a prompt instruction. No SQL, no table names, no errors."*
- **Point at `gate_closed` then `disclosure_released`:** *"The PO approved, and only now
  is the technical packet released to the dev team."*

**Then point at the "who was told what" table at the bottom.** PO got `business` tier,
developer and eng manager got `technical`. That table is the whole thesis in four rows.

### Run 2 — the business says no (2 min)

```bash
python -m aegis.cli run null_explosion -i
```

You'll be prompted as the Product Owner. **Read the brief out loud** — it's plain
language, no object names. Then type `reject`.

**What to say:**

> "The technically correct fix here is to default the empty values. That's the wrong
> business call, because the source system is mid-migration and will backfill the real
> ones. The agent can't know that. The PO can. So the data stays quarantined, a ticket
> goes to the CRM team, and the pipeline owner is notified. **The agent doesn't get to
> overrule that.**"

Point out: only **one** approval email was sent. The PO rejected, so the gate closed and
nobody else was chased for a decision that couldn't change the outcome.

### Run 3 — no humans at all (1 min)

```bash
python -m aegis.cli run transient_timeout
```

> "SEV4, warehouse timeout, the whole fix is 'run it again' — reversible, no code
> change. Waking four people to approve a retry is how an approval process gets ignored.
> So it doesn't ask. It fixes it and tells the owner afterwards."

**Point at the `autonomy` line** — the system explains *why* it didn't ask.

### Run 4 — the punchline (2 min)

```bash
python -m aegis.cli run ad_spend_drift_repeat
```

> "Same vendor, same feed, same problem as three weeks ago. A human already decided this
> exact situation. Rather than asking again, the system applies that decision — and names
> who made it and when."

**Point at the `precedent` line.** Then:

> "This is the answer to the obvious criticism of approval gates: they decay into
> rubber-stamping. Here, approval is a **policy set once**, not an interruption received
> every time."

### Close — the evidence (2 min)

```bash
python -m evals.harness
python -m pytest tests/
```

> "118 checks across eight scenarios, 82 tests. The checks are grouped deliberately:
> diagnosis, governance, remediation. **The governance ones assert negatives** — that the
> technical packet was *not* sent before approval, that nothing executed without the
> gates it needed. Those are the ones that matter, because a wrong diagnosis is a bad
> answer, and a governance violation is a control that didn't hold."

Then open `runs/report.html` and scroll to a **tiered disclosure** section — the
side-by-side view. That picture makes the argument faster than any explanation.

---

## 3. If you have more time: the architecture in five minutes

Draw this or pull it up from `ARCHITECTURE.md` §5:

```
alerts → quarantine → triage ─┬→ lineage   ─┐
                              ├→ pipeline  ─┼→ RCA ─┬→ loop (more evidence)
                              ├→ quality   ─┤       ├→ escalate
                              └→ change    ─┘       └→ plan → disclose
                                                              ↓
                                                     business gate (PO)
                                              ┌───────────┼───────────┐
                                           reject       defer     approve
                                              ↓           ↓          ↓
                                           ticket     backlog   technical gate
                                                                (dev + eng mgr)
                                                                     ↓
                                                            execute + open PR
```

**Three things to emphasise:**

1. **It's a loop, not a pipeline.** Below a confidence threshold, RCA mints new targeted
   questions and sends the investigation back round. Bounded at two rounds.
2. **Gates have three exits, not two.** Reject and defer are distinct outcomes with
   different side effects.
3. **Quarantine happens before any agent runs.** Containment is not an agent decision —
   it's automatic, so every decision afterwards is unhurried.

---

## 4. Hard questions, honest answers

### "How much of this did you write versus the AI?"

**Answer it straight. Do not be defensive — it's a fair question and the honest answer
is a good one.**

> "I designed it and directed the build; Claude wrote the code. The problem statement,
> the domain choice, and the governance model — business approval gating technical
> disclosure, the two-tier chain, the reject-to-quarantine path, precedent-based
> autonomy — those are my design decisions, and I made several of them against what the
> AI initially proposed. It suggested two business approvers; I cut it to one. It was
> going to build everything at once; I sequenced it. The implementation is AI-written and
> I reviewed the shape of it as it went."

Then, if useful:

> "What I'd point at as evidence I understand it: §7 of the architecture doc lists eleven
> defects found during the build, including two holes in the redaction firewall that
> *looked* like working controls in every trace because nothing had ever attacked them.
> Knowing why those matter is the part that isn't automatable."

### "Why multi-agent? Couldn't one prompt do this?"

> "Two reasons. Practically, the four forensic specialists read different data and run in
> parallel — when the quality analyst and the change correlator independently reach the
> same conclusion, that agreement is evidence. A single prompt can't corroborate itself.
>
> Structurally, the work genuinely decomposes: correlating alerts, traversing lineage,
> reading orchestrator history and finding recent changes are different jobs with
> different tools. And separating them is what makes the hand-offs inspectable — you can
> read the trace and see exactly what each agent was asked and what it committed to."

### "What happens when the LLM gets it wrong?"

> "Several things, deliberately layered.
>
> Severity, hypothesis scoring, the autonomy decision and remediation sequencing are
> computed in Python, not by the model. The model names things and writes the prose a
> human reads — it doesn't decide whether something is a SEV1.
>
> The model can lower its own confidence by up to 0.15 but raise it by at most 0.05.
> That asymmetry means the failure mode is an extra investigation round, not a wrong fix.
>
> And below a confidence floor it escalates to a human and touches nothing."

### "Isn't requiring approval defeating the point of automation?"

> "It would be if it asked every time. It doesn't — the autonomy ladder decides per
> incident from three inputs: how risky the actions are, how big the code change is, how
> severe the incident is. A SEV4 retry runs unsupervised. Releasing quarantined data into
> the revenue mart needs two gates. And with precedent, the second occurrence of a known
> problem doesn't ask at all.
>
> It's not cautious or permissive. It's calibrated, and it tells you which it's being."

### "How do you know it actually works?"

> "Eight scenarios with declared ground truth, scored by a harness — 118 checks, all
> passing. It runs on a deterministic backend so the score doesn't move between runs;
> an agentic system whose score wobbles 10% can't be improved, because you can't tell a
> regression from variance.
>
> The governance tests are adversarial — they *construct* the leak they're trying to
> prevent. That's how the two firewall holes were found."

### "Is this production ready?"

**Say no. Confidently.**

> "No, and `ARCHITECTURE.md` §9 says so explicitly. The platform is simulated — the
> Snowflake interface exists but isn't verified against a live account. Approvals resolve
> synchronously; real async operation needs a checkpointer and a callback endpoint, which
> `DEPLOYMENT.md` §4 designs but doesn't build. And the PR carries the proposed diff as
> an annotated file rather than applying the patch, because the system has never seen the
> target repo — I'd rather that be visibly unfinished than faked."

A POC claiming no weaknesses invites someone to go find them for you.

### "What's genuinely new here?"

> "Tiered disclosure with a machine-enforced firewall, and precedent-based autonomy.
> Everyone builds triage → RCA → fix. I haven't seen a system where the business
> approval *gates what the engineer is allowed to see*, or where an approval becomes a
> standing decision with an explicit envelope, expiry and revocation."

### "What would you do differently?"

> "I'd have written the adversarial tests earlier. The redaction firewall had two rules
> that had never once fired on a real object name, and they looked fine in every trace.
> I only found them by writing tests that build the leak. That generalises: a control
> that's never been attacked is untested, not working."

### "Why LangGraph and not Bedrock Agents?"

> "Bedrock's managed multi-agent collaboration puts the hand-off logic in the
> supervisor's prompt. I wanted it in code — the confidence gate, the loop-back, the
> three-exit gates are things I want to be able to test and point at. AWS's own
> multi-agent SRE reference architecture uses LangGraph on AgentCore for the same reason."

---

## 5. Numbers worth having in your head

| | |
|---|---|
| Agents | 9 |
| Lines | ~12,700 |
| Scenarios | 8, with declared ground truth |
| Eval checks | 118, all passing |
| Tests | 68 |
| Cost per incident | **$0.16** measured live (22 calls, ~119s) |
| Whole POC cost | ~$6 |
| Run time | ~200ms per incident on the deterministic backend |
| Alert de-dup | 10 alerts → 1 incident on the vendor outage |
| Defects found and fixed | 8, documented |

---

## 6. If something breaks live

**Stay calm and use it.** A system that fails visibly and explains itself is a better
demo than one that works silently.

- **Anything odd** → `python -m aegis.cli run <scenario>` again; it's deterministic, so
  it will do exactly the same thing
- **Bedrock errors** → drop the `--bedrock` flag. The deterministic backend needs no
  credentials and produces the same outcomes. *"The reasoning layer has a deterministic
  fallback — that's also what the tests run on."*
- **Import errors** → `source .venv/bin/activate`
- **Total failure** → open `runs/report.html`. It's self-contained and shows a full run

Generate a fresh report before the meeting so you have a fallback:

```bash
python -m aegis.cli run --all --report runs/report.html
```

---

## 7. Where everything lives

| Question | File |
|---|---|
| What does each agent do? | `ARCHITECTURE.md` §3 |
| How do agents hand off? | `ARCHITECTURE.md` §4 |
| Why gates, and when does it skip them? | `ARCHITECTURE.md` §2 |
| What went wrong during the build? | `ARCHITECTURE.md` §7 |
| What doesn't work yet? | `ARCHITECTURE.md` §9 |
| How would it deploy? | `DEPLOYMENT.md` |
| How was AWS set up? | `AWS-SETUP.md` |
| Full build history and decisions | `PROJECT-LOG.md` |

**The single most useful section if they push on technical depth is
`ARCHITECTURE.md` §7** — eleven defects, what each one taught. Counter-intuitive, but
admitting what broke builds more credibility than a clean story.

---

## 8. One thing to do tonight

Run the four demo commands once, in order, out loud. Not to rehearse a script — to find
the two or three places where you think *"I'm not sure I could explain that."*

Then ask me about exactly those, and we'll go through them until you could defend them
without notes.

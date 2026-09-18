# Run sheet — 20 minutes

> ## This is the only document you need on the day.
>
> Follow it top to bottom. Everything else in `docs/` is reference you open **only if
> asked a question you want backup for** — you do not read them during the demo.
>
> | If they ask | Open |
> |---|---|
> | "what does that line mean?" | `AGENT-NARRATION.md` |
> | "which file does that?" | `CODEBASE-MAP.md` |
> | "why is it built this way?" | `ARCHITECTURE.md` |
> | "how would this run in production?" | `DEPLOYMENT.md` |
> | a hard or awkward question | `DEMO-GUIDE.md` §4 |
>
> A printable copy of this page: **`docs/DEMO-DAY.html`** — open it in a browser.

The shape: start the slow live run first and talk over it, rather than watching a
spinner in silence. Everything after it is instant.

---

## Before you start — 2 minutes, do not skip

```bash
cd ~/projects/aegis
source ~/.zshrc
python scripts/seed_s3.py --reset          # landing restored, quarantine emptied
python scripts/seed_s3.py --check          # 4 objects in landing, quarantine empty
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run --all --report runs/report.html
```

Open and leave open:

1. **Terminal A** — the project, big font
2. **Terminal B** — a second tab, same directory (for the live run)
3. `docs/graph.html` in a browser tab
4. `runs/report.html` in a browser tab
5. The **S3 console**, on the quarantine bucket
6. Your **inbox**, with the five messages from a previous run already there

Check the banner on a throwaway run reads: `email: ses    storage: s3 (live)`.
If it says `mock` or `simulated`, you did not `source ~/.zshrc`.

---

## 0:00 – 1:30 · Framing

> "Data incidents are not usually hard because the fix is hard. They are hard because
> nobody agrees who is allowed to decide. So I built the governance first and the
> automation second. Aegis triages, diagnoses and fixes data pipeline incidents — and
> the interesting part is where it stops and asks a human."

One sentence on the rule, because it is *yours*:

> "First time a class of problem appears, a human approves. After that, the same class
> is automated. Anything that adds, deletes, or changes scope still needs the Product
> Owner, every time."

---

## 1:30 – 2:00 · Start the live run (Terminal B)

```bash
python scripts/seed_s3.py --reset
python -m aegis.cli run schema_drift -v
```

> "That is running against Bedrock — real models, about two minutes. While it works,
> let me show you the architecture."

**Switch to the browser. Do not watch it.**

---

## 2:00 – 5:00 · Architecture, while it runs · `docs/graph.html`

- Generated from the compiled graph at runtime — **it cannot drift from the code**.
- `quarantine` runs first, on every incident, before any agent. Containment is
  fail-safe: the node decides there is nothing to contain, the graph never decides
  whether to *try*.
- `triage` fans out to **four specialists in one superstep** — different data,
  different questions, merged through a list reducer.
- **The dashed edges are the answer to "is this really multi-agent".** Those are
  computed at runtime by a routing function. `rca ⇢ round_two` is a loop: short
  confidence sends fresh directives back to the specialists, bounded at two rounds.
- `plan ⇢ autonomous` bypasses every gate for low-risk reversible work.
- `business_gate ⇢ technical_gate` is tiered disclosure. `⇢ rejected` is the human
  saying no.

If there is time: **why multi-agent** —

> "Confidence is a function of agreement between independent specialists. A single
> call cannot corroborate itself. Collapse the agents and that term goes to 1 for
> everything."

---

## 5:00 – 9:00 · Back to the live run · Terminal B

Walk the trace top to bottom:

| Point at | Say |
|---|---|
| `quarantine … storage  object moved to s3://…` | "That is a real 6MB object, really moved between buckets. Containment is a move, not a flag." |
| `triage  6 alerts -> 1 incident (SEV1)` | "Six alerts fired. A human on call opens six tickets or mutes the channel. Triage returns one incident with a severity and a blast radius." |
| the four `handoff  triage -> …` lines | "**Each specialist gets a different question, written by triage.** That is the hand-off contract — typed, not a string." |
| `rca … -> dig_deeper` then round two | "Confidence was short, so it went back for more evidence rather than guessing." |
| `autonomy  supervisor  always-gated action` | "`release_quarantine` is always gated. No confidence score unlocks it." |
| `disclosure  business brief cleared the redaction firewall` | "Machine-enforced. Thirteen rules. The business brief cannot contain a table name." |
| `disclosure_released` | "The technical packet did not exist for the dev team until the business approved." |
| the `who was told what` table | "Five real emails. Two tiers." |

Then **the inbox** — open the PO email and the developer email side by side.

> "Same incident. The PO gets ARR and churn campaigns. The developer gets
> `RAW.STRIPE_CHARGES` and the fix. The second one only exists because the first
> was approved."

Then **the S3 console** — the v4 file in quarantine, the valid v3 file still in landing.

Cost line at the bottom: **$0.15, 22 model calls.**

---

## 9:00 – 10:30 · The boring case · Terminal A

```bash
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run transient_timeout
```

> "A tier-3 asset missed its build, contention has cleared, the fix is a retry.
> Nobody is woken. Same graph, same agents — the ladder is a real decision, not
> decoration. If it asked permission for this, people would switch it off."

---

## 10:30 – 13:00 · The human says no

```bash
python scripts/seed_s3.py --reset
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run null_explosion
```

> "The Product Owner rejects. The source is mid-migration and will backfill — fixing
> it now would bake in data that is about to be replaced. The system obeys: file stays
> held, ticket raised, pipeline owner told, nothing executed."

**This is the most important run.** Most agent demos only ever show approval.

---

## 13:00 – 15:30 · The punchline

```bash
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run ad_spend_drift
AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli run ad_spend_drift_repeat
```

> "First time, a human approves. Three weeks later the same class of problem —
> **nobody is asked.** The approval became a standing decision. That was the design
> goal from day one: approve once, automate the shape."

---

## 15:30 – 18:00 · The evidence

**`runs/report.html`** — hypotheses that lost and why, the approval chain, both
disclosure tiers, the timeline.

Then the part to lead with if the conversation goes technical — **`ARCHITECTURE.md` §7,
fifteen defects**:

- **#5** — the autonomy ladder computed a decision and an unconditional edge ignored
  it. *A decision nothing routes on is a log line.*
- **#7–8** — the redaction rule required three dotted parts; every table here has two.
  **It had never fired.** *A control that has never been attacked is untested, not
  working.*
- **#14** — SES raised `NoCredentialsError` and killed a two-minute incident. *Fail
  closed on authority, open on plumbing.*
- **#15 + the test** — the suite read ambient config and was one variable from deleting
  the demo's own data. *A safe default is only safe if it holds where you forgot to look.*

> "I found fifteen bugs in my own work and wrote down what each one taught. That is
> more useful to you than a demo that simply worked."

---

## 18:00 – 20:00 · Limits, then questions

Say these before you are asked:

- **The containment decision is scripted.** The scenario marks the file bad; the system
  does not yet read the object and validate it. Everything downstream is real.
- **The Snowflake load is a fixture.** The client is written and verified against a real
  Enterprise account — `check_snowflake.py`, 13 of 14 methods returning real rows — but
  the demo runs on the simulator so scenarios stay deterministic.
- **Approval links do not resolve.** The token is real — signed, TTL-bounded, single-use.
  What is missing is API Gateway plus a checkpointer so a paused incident can resume when
  someone clicks hours later. That is the next build.

---

## If something breaks

| Problem | Do this |
|---|---|
| `containment incomplete — source does not exist` | You forgot `--reset`. Say so, run it, move on. |
| Live confidence lands near 40% and it **escalates** | *"Below 40% it escalates to a human instead of proceeding. That is the design working."* Then show the heuristic run. **Do not re-run hoping for a better number.** |
| Bedrock is slow or erroring | Drop `--bedrock`; every scenario runs on the heuristic backend in two seconds, $0. |
| Emails do not arrive | The delivery column already told you. The incident completes anyway — that is defect 14. |
| Anything else | `runs/report.html` has every incident already rendered. |

---

## The three sentences to land

1. **"The graph does not know in advance which way it will go."** — dashed edges.
2. **"The technical fix did not exist for the dev team until the business approved."** — tiered disclosure.
3. **"Approve once, automate the shape."** — precedent, and the rule I was given.

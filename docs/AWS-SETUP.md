# AWS Setup Runbook

Everything needed to point Aegis at real Bedrock models, start to finish.

> This is the **how-to**. For the full project record — brief, architecture, decisions,
> bugs found, results — see [`PROJECT-LOG.md`](./PROJECT-LOG.md).

**Status: complete and verified.** Time to reproduce from scratch: ~10 minutes.

---

## Before you start

You need an AWS account. You do **not** need: an IAM user, a written policy, Bedrock
Marketplace access, a Knowledge Base, or Provisioned Throughput. Three of those cost
money while idle (see [§5](#5-what-not-to-click)).

**Enabling model access costs nothing.** Bedrock on-demand is pure pay-per-token: no
subscription, no reservation, no idle charge. An account with every Claude model
enabled and zero API calls bills $0.

---

## 1. Region

Use **`us-east-1`**.

The Anthropic use-case form is granted **per account**, not per region, so models
enabled from any region's console work everywhere. But the SDK resolves a region per
call, and a mismatch shows up as a confusing "no models found" rather than a clear
error — so pick one and keep everything consistent.

> During this setup the console was on `us-east-2` (Ohio) while the local AWS config
> resolved `us-east-1`. It worked anyway, for the reason above. Standardised on
> `us-east-1`.

---

## 2. Enable model access

The standalone **Model access** page no longer exists in the current Bedrock console.
If a guide tells you to look for it, that guide is out of date.

1. [Bedrock console](https://console.aws.amazon.com/bedrock/) → **Discover → Model catalog**
2. Filter **Providers → Anthropic**
3. Open **Claude Haiku 4.5**
4. Yellow banner at the top → **Submit use case details**
5. Fill the form:
   - **Intended users:** Internal users only
   - **Use case:** see the text below
6. Submit — **access is granted immediately**. No approval queue.

The form is submitted **once per AWS account** and unlocks every Anthropic model. Check
a Sonnet model afterwards to confirm the banner is gone there too.

<details>
<summary>Use-case text used</summary>

```
Internal proof-of-concept for automated data pipeline incident management.
A multi-agent system triages data quality alerts, correlates them using
warehouse lineage metadata, performs root-cause analysis, and drafts
remediation plans that are reviewed and approved by humans before any
action is taken. Internal engineering use only; no customer-facing
deployment and no end-user access to the models.
```

Accurate, internal-only, and explicit that the system is human-gated.
</details>

---

## 3. Credentials

Two options. **Use the API key** unless you have a reason not to.

### Option A — Bedrock API key (recommended)

Bedrock console → **Discover → API keys** → **Generate API key**.

- Choose **long-term**, expiry **30 days**. Short-term keys last ~12 hours and will be
  dead before you next need them.
- Leave **"Permissions to access Amazon Bedrock Marketplace models" unchecked** —
  Marketplace models run on dedicated hourly-billed endpoints; Claude is serverless and
  doesn't use that path.
- Copy the key when shown. AWS displays it once.

AWS provisions the backing permissions automatically, so there is no IAM policy to
write, and the key is scoped to Bedrock alone.

**Store it outside the project folder:**

```bash
open -e ~/.zshrc          # or: nano ~/.zshrc
```

Add at the bottom, keeping the quotes:

```bash
export AWS_BEARER_TOKEN_BEDROCK='your-key-here'
```

Then `source ~/.zshrc` and check it took:

```bash
echo ${#AWS_BEARER_TOKEN_BEDROCK}    # prints length, not the key
```

> **Why not `.env`?** `.env` sits inside the project folder. If that folder is shared
> with an agent, a CI runner, or a screen-share, the key is in scope. `~/.zshrc` keeps
> it out. A real environment variable takes precedence over `.env`, so behaviour is
> identical either way.

### Option B — ordinary AWS credentials

```bash
brew install awscli
aws configure               # region: us-east-1, output: json
aws sts get-caller-identity
```

Works fine; just more moving parts. If you scope a dedicated IAM user, the policy is in
[`PROJECT-LOG.md` §11](./PROJECT-LOG.md#appendix--production-iam-policy-not-used-the-api-key-made-it-unnecessary) —
note that cross-region inference profiles need permission on **both** the
inference-profile ARN and the foundation-model ARN, or you get a misleading
`AccessDenied`.

---

## 4. Verify, and discover your model IDs

Model IDs move between releases and differ per account, so don't trust a hardcoded
list — read them from your own account:

```bash
cd ~/projects/aegis
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/check_bedrock.py
```

The preflight:

1. resolves credentials (handles both auth options above)
2. lists the Claude profiles your account can see — and if the credentials can't list
   (normal for an API key), probes candidates directly instead of dead-ending
3. makes one real Converse call per tier, a fraction of a cent
4. confirms prompt caching engages
5. prints a config block to paste into `.env`

**What it found for this account:**

```
AEGIS_MODEL_BACKEND=bedrock
AWS_REGION=us-east-1
AEGIS_CHEAP_MODEL=global.anthropic.claude-haiku-4-5-20251001-v1:0
AEGIS_STRONG_MODEL=us.anthropic.claude-sonnet-4-6
```

Two things worth noting: Haiku came back with a **`global.`** prefix rather than `us.`,
and there is **no Sonnet 5** — the strong tier is Sonnet 4.6 at $3/$15 rather than
$2/$10. Both assumptions would have been wrong if hardcoded, which is why the price
table in `aegis/config.py` matches on model *family* and falls back to the most
expensive known rate for anything it doesn't recognise.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AccessDeniedException` on invoke | Use-case form not submitted | Step 2 |
| "on-demand throughput isn't supported" | Bare model ID used instead of an inference profile | Use the `us.`/`global.` prefixed ID |
| "No Anthropic profiles visible" | Region mismatch | `AWS_REGION=us-east-1` |
| "cannot list profiles" | Normal for an API key | Not an error — it probes instead |
| `ProxyConnectionError` | Network/VPN/proxy | Connectivity, not credentials |

---

## 5. What *not* to click

Three things in the Bedrock console bill while idle. One is expensive enough to matter.

| Trap | Idle cost | Why it's tempting |
|---|---|---|
| **Knowledge Bases** | 2 OpenSearch Serverless OCUs minimum @ $0.24/hr ≈ **$345/month at zero queries** | Sounds exactly like something an agent system needs |
| **Provisioned Throughput** | Hourly commitment whether used or not | Sounds like a performance setting |
| **AgentCore Runtime** | Session memory billed per second *including idle*, 15-min default timeout | The obvious "deploy this" button |

Aegis uses none of them. Incident memory is a small in-process similarity function, not
a vector store — chosen for explainability, and this is the other reason.

---

## 6. Cost

| Item | Cost |
|---|---|
| Model access, IAM, API keys, budget alarms | **$0** |
| **One incident run** | **$0.16** (measured live: 22 calls, two rounds) |
| All eight scenarios once | ~$1.30 |
| Development, tests, eval suite (heuristic backend) | **$0** |
| **Realistic POC total** | **~$6** |

Billed to the AWS account's card, **monthly in arrears**. Watch it live at
**Billing → Bills**, where Bedrock appears as its own line item.

### Set a budget alarm

**Billing → Budgets → Create budget → Cost budget → $10/month**, filtered to
service = Bedrock, alert at 50%. First two budgets are free.

> **It notifies; it does not cap.** AWS has no hard spend limit. The actual circuit
> breaker is the budget governor in `aegis/config.py`: a $0.50 ceiling per incident,
> after which agents degrade to the free backend and the incident is stamped
> `degraded_reasoning`. Belt and braces — the alarm doesn't depend on application code
> being correct, and the governor doesn't depend on anyone reading email.

If a number ever looks wrong, **delete the API key** and everything stops immediately.

---

## 7. Local environment notes

```bash
cd ~/projects/aegis
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Use a venv.** An early `pip install --user boto3` here upgraded system `botocore`
1.34.69 → 1.43.95 and broke a pin held by `aiobotocore 2.12.3`. Warning only, boto3
worked, but it quietly broke an unrelated project. If you hit it:

```bash
deactivate
python3 -m pip uninstall -y boto3 botocore s3transfer
python3 -m pip install --user 'botocore<1.34.70'
```

The venv keeps its own copies and is unaffected either way.

Two other macOS details: dotfiles are hidden in Finder (**Cmd+Shift+.** toggles them),
and if `code` isn't found, open VS Code → **Cmd+Shift+P** → *Install 'code' command in
PATH*, or just use `open -e`.

---

## 8. Switching Aegis to Bedrock

```bash
cp .env.example .env
```

Set three values:

```
AEGIS_MODEL_BACKEND=bedrock
AWS_REGION=us-east-1
# AWS_BEARER_TOKEN_BEDROCK stays in ~/.zshrc
```

Then either:

```bash
python -m aegis.cli run --all              # uses .env
python -m aegis.cli run --all --bedrock    # forces it regardless of .env
```

`.env` is gitignored. A real environment variable always beats it — so if something
behaves oddly, `env | grep AEGIS` shows you what's overriding the file.

---

## 9. Seeing the runs in the AWS console

Useful for two different reasons: debugging a live run, and proving on Friday that the
Bedrock calls are real rather than mocked. There are two levels, and the first needs no
setup at all.

### Level 1 — CloudWatch metrics (already on, $0)

Bedrock publishes these automatically under the **`AWS/Bedrock`** namespace, dimensioned
by `ModelId`. Nothing to enable; you only need permission to read CloudWatch.

> CloudWatch console → **Metrics → All metrics → Bedrock → By Model ID**

| Metric | What it tells you |
|---|---|
| `Invocations` | Call count — should be ~22 for one two-round incident |
| `InputTokenCount` / `OutputTokenCount` | The volume behind the cost figure |
| `InvocationLatency` | Per-call latency; explains the ~119s wall clock |
| `CacheReadInputTokenCount` / `CacheWriteInputTokenCount` | **Whether prompt caching is actually engaging.** Worth checking — it is the one cost optimisation the local trace cannot confirm |
| `InvocationThrottles` | If a demo run stalls, look here first |

Set the period to 1 minute; at 5 minutes a single incident is one flat blip.

### Level 2 — model invocation logging (~3 minutes, then near-$0)

Off by default: Bedrock does **not** retain prompts or completions unless you turn this
on. Enabling it gives you the full request and response body for every call.

> Bedrock console → **Settings** (left nav) → **Model invocation logging** → toggle on
> → select the **Text** modality → choose **CloudWatch Logs only** → name a log group
> (e.g. `/aegis/bedrock`) → let the console create the service role for you

The console offers to create the IAM role that lets `bedrock.amazonaws.com` write to the
log group; accept it rather than hand-rolling one. Each entry carries the timestamp,
model ID, caller identity, token counts, and the input/output bodies inline up to 100 KB
— past that, or for binary output, S3 is required. Text-only agent traffic is far under
the limit.

**Enable it in the same region your client calls** — `us-east-1` here, per §1. Logging is
configured per region, and a cross-region inference profile does not move the
configuration to wherever the request was ultimately served.

**Cost:** CloudWatch Logs bills roughly $0.50/GB ingested and $0.03/GB-month stored. One
incident is a few hundred KB, so a whole demo week is cents. Set the log group's
retention to **1 day** anyway — it is one click and it means you cannot forget about it.

### Per-agent cost attribution (the part worth showing)

Every Converse call Aegis makes carries `requestMetadata` tags — `incident`, `agent` and
`attempt` (`reasoning.py` `_request_metadata`). Bedrock records these in the invocation
log, so once logging is on you can break a single incident's spend down **by agent**:

> CloudWatch console → **Logs → Logs Insights** → select `/aegis/bedrock`

```
fields requestMetadata.agent as agent, modelId,
       input.inputTokenCount as in_tok,
       output.outputTokenCount as out_tok
| filter requestMetadata.incident = "INC-SCHEMA_DRIFT"
| stats count() as calls, sum(in_tok) as input, sum(out_tok) as output by agent, modelId
| sort calls desc
```

That table is a good thing to have open in a second tab. It shows AWS's own records
agreeing with the local ledger, it makes the fan-out visible as four specialists billed
separately, and it demonstrates the model tiering — specialists on the cheap model,
synthesis and disclosure on the strong one — as a fact rather than a claim.

The tags are ignored when invocation logging is off, so the code is safe either way, and
values are sanitised before they are sent: a request rejected mid-demo over a stray
character in an incident ID would be a much worse outcome than a missing log tag.

One caveat to state honestly if asked: these tags are **not** AWS cost-allocation tags.
They do not appear in Cost Explorer or the billing report — they attribute *tokens* in
the logs, and the per-agent USD figure still comes from the local ledger in `config.py`.

---

## Reference

- [Bedrock console](https://console.aws.amazon.com/bedrock/) · [pricing](https://aws.amazon.com/bedrock/pricing/)
- [Inference profile prerequisites and IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)
- [Model invocation logging](https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html) · [runtime CloudWatch metrics](https://docs.aws.amazon.com/bedrock/latest/userguide/monitoring-runtime-metrics.html) · [per-request metadata tagging](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-request-metadata.html)
- [Multi-agent SRE reference architecture on AgentCore](https://aws.amazon.com/blogs/machine-learning/build-multi-agent-site-reliability-engineering-assistants-with-amazon-bedrock-agentcore/)

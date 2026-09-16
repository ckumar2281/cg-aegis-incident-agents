"""
Configuration and the cost governor.

Everything environment-specific lives here, read from env vars with safe defaults so
the system runs end-to-end with zero credentials (`AEGIS_MODEL_BACKEND=heuristic`,
`AEGIS_PLATFORM=simulated`), and switches to real Bedrock + Snowflake by setting env.

The cost governor is deliberately part of the core rather than an afterthought. A
fan-out multi-agent graph is an excellent way to spend money by accident: every
investigation round multiplies calls. Aegis prices every call as it makes it, and
when an incident exceeds its ceiling the remaining agents degrade to the deterministic
heuristic backend and the incident is stamped `degraded_reasoning` in the audit trail.
Degraded but honest beats accurate but unbounded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import AgentRole, HumanRole

# --------------------------------------------------------------------------- #
# .env loading
#
# Deliberately hand-rolled rather than pulling in python-dotenv. It is about
# twenty lines, it removes a dependency from the critical path, and it makes the
# precedence rule explicit: a real environment variable always wins over the file,
# so `AEGIS_MODEL_BACKEND=heuristic python -m aegis.cli ...` overrides .env without
# anyone having to remember a flag.
# --------------------------------------------------------------------------- #


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """
    Read KEY=VALUE pairs from a .env file into os.environ, without clobbering
    variables that are already set. Returns what it actually applied.
    """
    if path is None:
        # Walk up from this file to find the project root holding .env.
        for parent in [Path.cwd(), *Path(__file__).resolve().parents]:
            candidate = parent / ".env"
            if candidate.is_file():
                path = candidate
                break
        else:
            return {}

    env_path = Path(path)
    if not env_path.is_file():
        return {}

    applied: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")  # tolerate quoted values
        if not key or key in os.environ:  # real env wins
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


# Loaded at import time so every `_env(...)` default below sees the file.
load_dotenv()

# --------------------------------------------------------------------------- #
# Model pricing (USD per million tokens, Bedrock on-demand)
#
# Keep this table honest and dated. It is used for real spend accounting, not
# decoration. Update when AWS changes rates.
# --------------------------------------------------------------------------- #

PRICING_AS_OF = "2026-09"

#: Keyed on model *family*, not on full inference-profile IDs.
#:
#: The same model is reachable as `us.`, `eu.`, `apac.` or `global.` depending on
#: which cross-region profile your account has, and the date suffix changes between
#: releases. Keying on the full ID means a new profile prefix silently prices at zero
#: and the budget governor stops governing -- which is the one failure mode a cost
#: control must not have. Matching on family is looser but fails safe.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # family fragment: (usd_per_M_input, usd_per_M_output)
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-3-5-haiku": (0.80, 4.00),
    "nova-micro": (0.035, 0.14),
    "nova-lite": (0.06, 0.24),
    "nova-pro": (0.80, 3.20),
    "heuristic": (0.0, 0.0),
}

#: Charged when a model ID matches nothing above. Deliberately the most expensive
#: rate in the table: an unknown model should make the governor *more* cautious,
#: never less. Over-estimating costs you an early degrade; under-estimating costs
#: you a surprise bill.
UNKNOWN_MODEL_PRICE = (15.00, 75.00)

#: Cached input tokens bill at roughly 10% of the base input rate.
CACHE_READ_DISCOUNT = 0.10

#: Cross-region inference profile prefixes, stripped before family matching.
_PROFILE_PREFIXES = ("us-gov.", "global.", "apac.", "us.", "eu.", "jp.", "au.")


def resolve_price(model_id: str) -> tuple[tuple[float, float], bool]:
    """
    Return ((input_rate, output_rate), matched) for a model ID.

    `matched` is False when we fell back to the pessimistic default, which callers
    surface rather than swallow -- a silently mispriced model is how a cost control
    stops being one.
    """
    if not model_id:
        return UNKNOWN_MODEL_PRICE, False

    needle = model_id.lower()
    for prefix in _PROFILE_PREFIXES:
        if needle.startswith(prefix):
            needle = needle[len(prefix) :]
            break

    if needle in MODEL_PRICES:
        return MODEL_PRICES[needle], True

    # Longest family fragment wins, so claude-sonnet-4-5 is not matched by a
    # hypothetical shorter "claude-sonnet" entry.
    best: tuple[str, tuple[float, float]] | None = None
    for family, rates in MODEL_PRICES.items():
        if family in needle and (best is None or len(family) > len(best[0])):
            best = (family, rates)
    if best:
        return best[1], True
    return UNKNOWN_MODEL_PRICE, False


def price_call(model_id: str, input_tokens: int, output_tokens: int, cached_input: int = 0) -> float:
    """Return the USD cost of a single model call."""
    (in_rate, out_rate), _ = resolve_price(model_id)
    fresh_input = max(0, input_tokens - cached_input)
    cost = (fresh_input / 1_000_000) * in_rate
    cost += (cached_input / 1_000_000) * in_rate * CACHE_READ_DISCOUNT
    cost += (output_tokens / 1_000_000) * out_rate
    return cost


# --------------------------------------------------------------------------- #
# Model tiering
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModelTier:
    model_id: str
    max_tokens: int = 1400
    temperature: float = 0.0


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


CHEAP_MODEL = _env("AEGIS_CHEAP_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
STRONG_MODEL = _env("AEGIS_STRONG_MODEL", "us.anthropic.claude-sonnet-5-20260514-v1:0")

# Which agents deserve the expensive model.
#
# The four forensic specialists are mostly structured retrieval plus a short
# judgement -- cheap models do this well. Synthesis, planning and the disclosure
# rendering are where reasoning quality actually changes the outcome, so they get
# the strong model. This single map is the difference between a ~$0.10 incident
# and a ~$0.25 one.
AGENT_MODEL_TIER: dict[AgentRole, str] = {
    AgentRole.TRIAGE: "cheap",
    AgentRole.LINEAGE: "cheap",
    AgentRole.PIPELINE: "cheap",
    AgentRole.QUALITY: "cheap",
    AgentRole.CHANGE: "cheap",
    AgentRole.RCA: "strong",
    AgentRole.PLANNER: "strong",
    AgentRole.DISCLOSURE: "strong",
    AgentRole.EXECUTOR: "cheap",
    AgentRole.AUDIT: "cheap",
    AgentRole.SUPERVISOR: "cheap",
}


def model_for(role: AgentRole) -> ModelTier:
    tier = AGENT_MODEL_TIER.get(role, "cheap")
    model_id = STRONG_MODEL if tier == "strong" else CHEAP_MODEL
    max_tokens = 2000 if tier == "strong" else 1200
    return ModelTier(model_id=model_id, max_tokens=max_tokens)


# --------------------------------------------------------------------------- #
# Budget governor
# --------------------------------------------------------------------------- #


@dataclass
class BudgetPolicy:
    """Hard ceilings, enforced per incident."""

    max_usd_per_incident: float = float(_env("AEGIS_MAX_USD_PER_INCIDENT", "0.50"))
    max_llm_calls_per_incident: int = int(_env("AEGIS_MAX_LLM_CALLS", "24"))
    max_investigation_rounds: int = int(_env("AEGIS_MAX_ROUNDS", "2"))
    degrade_on_breach: bool = _env("AEGIS_DEGRADE_ON_BREACH", "true").lower() == "true"


@dataclass
class BudgetLedger:
    """Live spend for one incident. Written into the audit trail at close."""

    policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    degraded: bool = False
    degraded_reason: str = ""
    by_agent: dict[str, float] = field(default_factory=dict)

    def record(
        self,
        role: AgentRole,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cached_input: int = 0,
    ) -> None:
        cost = price_call(model_id, input_tokens, output_tokens, cached_input)
        self.calls += 1
        self.input_tokens += input_tokens
        self.cached_tokens += cached_input
        self.output_tokens += output_tokens
        self.usd += cost
        self.by_agent[role.value] = self.by_agent.get(role.value, 0.0) + cost

    def would_breach(self) -> tuple[bool, str]:
        if self.calls >= self.policy.max_llm_calls_per_incident:
            return True, (
                f"call ceiling reached ({self.calls}/"
                f"{self.policy.max_llm_calls_per_incident})"
            )
        if self.usd >= self.policy.max_usd_per_incident:
            return True, (
                f"spend ceiling reached (${self.usd:.4f}/"
                f"${self.policy.max_usd_per_incident:.2f})"
            )
        return False, ""

    def mark_degraded(self, reason: str) -> None:
        if not self.degraded:
            self.degraded = True
            self.degraded_reason = reason

    def summary(self) -> dict[str, object]:
        return {
            "llm_calls": self.calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_tokens,
            "output_tokens": self.output_tokens,
            "usd": round(self.usd, 6),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "by_agent_usd": {k: round(v, 6) for k, v in self.by_agent.items()},
        }


# --------------------------------------------------------------------------- #
# Approval routing
# --------------------------------------------------------------------------- #


@dataclass
class ApprovalPolicy:
    """
    The two-gate chain.

    Business gate first. Its approval is what *unlocks disclosure* of the technical
    packet -- the developer does not receive the fix detail until the Product Owner
    has signed off on the problem in business terms.
    """

    #: One accountable business decision-maker, not a committee. A second business
    #: approver added ceremony without adding judgement: both were shown the same
    #: brief and the second had no information the first lacked. The technical gate
    #: keeps two roles because the developer and the engineering manager genuinely
    #: assess different things -- correctness and acceptable risk.
    business_roles: tuple[HumanRole, ...] = (HumanRole.PRODUCT_OWNER,)
    technical_roles: tuple[HumanRole, ...] = (HumanRole.DEVELOPER, HumanRole.ENG_MANAGER)
    token_ttl_minutes: int = int(_env("AEGIS_TOKEN_TTL_MIN", "1440"))
    # A timeout never means yes. It escalates to the pipeline owner.
    timeout_behaviour: str = _env("AEGIS_TIMEOUT_BEHAVIOUR", "escalate")
    auto_approve_sev4_readonly: bool = (
        _env("AEGIS_AUTO_APPROVE_SEV4", "true").lower() == "true"
    )


@dataclass
class Recipients:
    product_owner: str = _env("AEGIS_PO_EMAIL", "product.owner@example.com")
    scrum_master: str = _env("AEGIS_SM_EMAIL", "scrum.master@example.com")
    developer: str = _env("AEGIS_DEV_EMAIL", "dev.team@example.com")
    engineering_manager: str = _env("AEGIS_EM_EMAIL", "eng.manager@example.com")
    pipeline_owner: str = _env("AEGIS_PIPELINE_OWNER_EMAIL", "pipeline.owner@example.com")

    def for_role(self, role: HumanRole) -> str:
        return {
            HumanRole.PRODUCT_OWNER: self.product_owner,
            HumanRole.SCRUM_MASTER: self.scrum_master,
            HumanRole.DEVELOPER: self.developer,
            HumanRole.ENG_MANAGER: self.engineering_manager,
            HumanRole.PIPELINE_OWNER: self.pipeline_owner,
        }[role]


# --------------------------------------------------------------------------- #
# Top-level settings
# --------------------------------------------------------------------------- #


@dataclass
class Settings:
    # backends
    model_backend: str = _env("AEGIS_MODEL_BACKEND", "heuristic")  # heuristic | bedrock
    platform_backend: str = _env("AEGIS_PLATFORM", "simulated")  # simulated | snowflake
    aws_region: str = _env("AWS_REGION", "us-east-1")

    # snowflake
    snowflake_account: str = _env("SNOWFLAKE_ACCOUNT", "")
    snowflake_user: str = _env("SNOWFLAKE_USER", "")
    snowflake_role: str = _env("SNOWFLAKE_ROLE", "AEGIS_AGENT")
    snowflake_warehouse: str = _env("SNOWFLAKE_WAREHOUSE", "AEGIS_WH_XS")
    snowflake_database: str = _env("SNOWFLAKE_DATABASE", "AEGIS")
    snowflake_schema: str = _env("SNOWFLAKE_SCHEMA", "OPS")

    # storage
    raw_bucket: str = _env("AEGIS_RAW_BUCKET", "aegis-raw")
    quarantine_bucket: str = _env("AEGIS_QUARANTINE_BUCKET", "aegis-quarantine")

    # integrations
    ticket_provider: str = _env("AEGIS_TICKET_PROVIDER", "mock")  # jira | servicenow | mock
    vcs_provider: str = _env("AEGIS_VCS_PROVIDER", "mock")  # github | mock
    email_provider: str = _env("AEGIS_EMAIL_PROVIDER", "mock")  # ses | mock
    github_repo: str = _env("AEGIS_GITHUB_REPO", "example-org/northwind-dbt")
    approval_base_url: str = _env("AEGIS_APPROVAL_BASE_URL", "https://approvals.example.com")

    # policies
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    approvals: ApprovalPolicy = field(default_factory=ApprovalPolicy)
    recipients: Recipients = field(default_factory=Recipients)

    # rca gating
    rca_confidence_threshold: float = float(_env("AEGIS_RCA_CONFIDENCE", "0.72"))
    rca_escalate_below: float = float(_env("AEGIS_RCA_ESCALATE_BELOW", "0.40"))

    signing_secret: str = _env("AEGIS_SIGNING_SECRET", "dev-only-not-a-real-secret")

    @property
    def offline(self) -> bool:
        return self.model_backend == "heuristic" and self.platform_backend == "simulated"


def load_settings() -> Settings:
    return Settings()

"""
Typed hand-off contracts for Aegis.

Every message that crosses an agent boundary is one of the models below. Nothing is
passed between agents as free-form text: an agent that wants to be understood by the
next one has to fill in a schema, and the orchestrator validates it before routing.
This is what makes the hand-off logic auditable -- you can read the trace and see
exactly what each agent was asked and what it committed to.

Flow:

    FileArrival + ValidationFailure[] + Alert[]      raw signals from the platform
      -> IncidentPacket          Triage: correlate, classify, scope, dispatch directives
      -> SpecialistReport[]      Forensics: answer directives with attributable Evidence
      -> RootCauseVerdict        RCA: score hypotheses, decide PROCEED / DIG / ESCALATE
      -> RemediationProposal     Planner: data remediation + code fix, risk-tiered
      -> DisclosureBundle        Disclosure: one verdict, two audiences, redaction enforced
      -> GateOutcome (business)  PO + Scrum Master. Approve unlocks the technical tier.
      -> GateOutcome (technical) Developer + Eng Manager. Approve unlocks execution.
      -> ExecutionResult         Executor: remediate data, open PR, verify, roll back
      -> AuditEvent[]            Hash-chained, append-only, for the whole lifecycle
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Shared vocabulary
# --------------------------------------------------------------------------- #


class Severity(str, Enum):
    """Drives autonomy, comms cadence, approval SLAs and paging."""

    SEV1 = "SEV1"  # Tier-1 asset wrong or unavailable; exec/external impact
    SEV2 = "SEV2"  # Material impact to important assets; same-day
    SEV3 = "SEV3"  # Contained; next business day
    SEV4 = "SEV4"  # Informational / self-healing

    @property
    def rank(self) -> int:
        return {"SEV1": 1, "SEV2": 2, "SEV3": 3, "SEV4": 4}[self.value]

    @property
    def approval_sla_minutes(self) -> int:
        return {"SEV1": 30, "SEV2": 120, "SEV3": 480, "SEV4": 1440}[self.value]


class IncidentType(str, Enum):
    SCHEMA_DRIFT = "schema_drift"
    INGESTION_FAILURE = "ingestion_failure"
    PIPELINE_FAILURE = "pipeline_failure"
    FRESHNESS_BREACH = "freshness_breach"
    VOLUME_ANOMALY = "volume_anomaly"
    QUALITY_DEGRADATION = "quality_degradation"
    REFERENTIAL_BREAK = "referential_break"
    COST_ANOMALY = "cost_anomaly"
    UNKNOWN = "unknown"


class AgentRole(str, Enum):
    TRIAGE = "triage"
    LINEAGE = "lineage_impact_analyst"
    PIPELINE = "pipeline_forensics"
    QUALITY = "data_quality_forensics"
    CHANGE = "change_correlator"
    RCA = "rca_synthesizer"
    PLANNER = "remediation_planner"
    DISCLOSURE = "disclosure_officer"
    APPROVALS = "approval_orchestrator"
    EXECUTOR = "executor_verifier"
    AUDIT = "audit_scribe"
    SUPERVISOR = "supervisor"


class HumanRole(str, Enum):
    """Who approves what. The business tier gates the technical tier."""

    PRODUCT_OWNER = "product_owner"
    SCRUM_MASTER = "scrum_master"
    DEVELOPER = "developer"
    ENG_MANAGER = "engineering_manager"
    PIPELINE_OWNER = "pipeline_owner"

    @property
    def tier(self) -> DisclosureTier:
        if self in (HumanRole.PRODUCT_OWNER, HumanRole.SCRUM_MASTER):
            return DisclosureTier.BUSINESS
        return DisclosureTier.TECHNICAL


class DisclosureTier(str, Enum):
    """
    How much of the truth an audience is allowed to see.

    BUSINESS is a strict subset of TECHNICAL. The redaction policy in policy.py is
    machine-enforced: a business artifact that contains code, credentials, internal
    paths, stack traces or raw records fails validation and the incident halts.
    """

    BUSINESS = "business"
    TECHNICAL = "technical"


class RiskTier(int, Enum):
    """How dangerous an action is. Gates the autonomy ladder."""

    T0_READ_ONLY = 0  # observation only
    T1_REVERSIBLE = 1  # rerun, pause, clone -- no data destroyed
    T2_MUTATING = 2  # backfill, reprocess, schema pin, quarantine release
    T3_DESTRUCTIVE = 3  # restate, drop, force-merge, prod rollback

    @property
    def label(self) -> str:
        return {0: "read-only", 1: "reversible", 2: "mutating", 3: "destructive"}[self.value]


class IncidentState(str, Enum):
    """The lifecycle. Three terminal states, reached only through the gates."""

    DETECTED = "detected"
    QUARANTINED = "quarantined"
    TRIAGED = "triaged"
    INVESTIGATING = "investigating"
    DIAGNOSED = "diagnosed"
    AWAITING_BUSINESS_APPROVAL = "awaiting_business_approval"
    AWAITING_TECHNICAL_APPROVAL = "awaiting_technical_approval"
    REMEDIATING = "remediating"
    # terminal
    RESOLVED = "resolved"  # fix merged, data reprocessed, verified
    REJECTED_QUARANTINED = "rejected_quarantined"  # business said no -> ticket raised
    DEFERRED_BACKLOG = "deferred_backlog"  # PO parked it as a future fix
    ESCALATED = "escalated"  # agents could not reach confidence; humans own it

    @property
    def is_terminal(self) -> bool:
        return self in (
            IncidentState.RESOLVED,
            IncidentState.REJECTED_QUARANTINED,
            IncidentState.DEFERRED_BACKLOG,
            IncidentState.ESCALATED,
        )


# --------------------------------------------------------------------------- #
# Raw platform signals
# --------------------------------------------------------------------------- #


class FileArrival(BaseModel):
    """A file landing in the S3 raw zone, about to be ingested into Snowflake."""

    model_config = ConfigDict(frozen=True)

    file_id: str
    bucket: str
    key: str
    source_system: str
    arrived_at: datetime
    size_bytes: int = 0
    row_count: int | None = None
    declared_schema_version: str | None = None
    target_table: str = ""


class ValidationFailure(BaseModel):
    """A validation rule that rejected the file or the rows inside it."""

    model_config = ConfigDict(frozen=True)

    failure_id: str
    file_id: str
    rule: str
    rule_kind: Literal["schema", "freshness", "volume", "nullability", "referential", "domain"]
    detected_at: datetime
    expected: str = ""
    actual: str = ""
    failed_rows: int = 0
    total_rows: int = 0
    sample_redacted: str = ""  # already-redacted excerpt, never raw PII

    @property
    def failure_rate(self) -> float:
        return (self.failed_rows / self.total_rows) if self.total_rows else 0.0


class Alert(BaseModel):
    """A signal from monitoring. Noisy by design -- triage dedups these."""

    model_config = ConfigDict(frozen=True)

    alert_id: str
    fired_at: datetime
    source: Literal[
        "snowpipe", "task_monitor", "dmf_monitor", "freshness_monitor", "cost_monitor", "user_report"
    ]
    asset: str
    rule: str
    message: str
    observed: dict[str, Any] = Field(default_factory=dict)
    raw_severity: str = "warning"
    file_id: str | None = None


class QuarantineRecord(BaseModel):
    """Fail-safe: the file is held out of the warehouse the moment validation fails."""

    file_id: str
    quarantined_at: datetime
    quarantine_uri: str
    reason: str
    original_uri: str
    released: bool = False
    released_at: datetime | None = None
    release_authorised_by: str | None = None


# --------------------------------------------------------------------------- #
# Triage -> Investigation
# --------------------------------------------------------------------------- #


class InvestigationDirective(BaseModel):
    """
    A specific question routed to a specific specialist.

    The heart of the hand-off design. Triage does not hand the next agent a blob of
    context and hope; it hands each specialist a numbered question it is accountable
    for answering. A SpecialistReport that does not address every directive assigned
    to it is rejected by the supervisor, and the RCA agent can mint *new* directives
    to re-open the investigation when its confidence is too low to act.
    """

    directive_id: str
    assigned_to: AgentRole
    question: str
    rationale: str = ""
    priority: Literal["critical", "high", "normal"] = "normal"
    context: dict[str, Any] = Field(default_factory=dict)
    round: int = 1


class BlastRadius(BaseModel):
    """Who is hurt, and how badly. Drives severity and the business brief."""

    downstream_assets: list[str] = Field(default_factory=list)
    tier1_assets: list[str] = Field(default_factory=list)
    consumer_surfaces: list[str] = Field(default_factory=list)
    affected_teams: list[str] = Field(default_factory=list)
    breached_slas: list[str] = Field(default_factory=list)
    estimated_rows_affected: int = 0
    business_processes: list[str] = Field(default_factory=list)
    score: float = Field(0.0, ge=0.0, le=1.0)

    def summary(self) -> str:
        return (
            f"{len(self.downstream_assets)} downstream assets "
            f"({len(self.tier1_assets)} tier-1), "
            f"{len(self.consumer_surfaces)} consumer surfaces, "
            f"{len(self.breached_slas)} SLA breaches"
        )


class IncidentPacket(BaseModel):
    """Triage's output: a de-duplicated, classified, scoped incident plus a work plan."""

    incident_id: str
    title: str
    opened_at: datetime
    incident_type: IncidentType
    severity: Severity
    severity_rationale: str

    primary_asset: str
    symptom_assets: list[str] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)
    correlated_alert_ids: list[str] = Field(default_factory=list)
    suppressed_alert_ids: list[str] = Field(default_factory=list)
    suppression_rationale: str = ""

    window_start: datetime
    window_end: datetime

    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    directives: list[InvestigationDirective] = Field(default_factory=list)
    similar_past_incidents: list[dict[str, Any]] = Field(default_factory=list)
    pipeline_owner: str = ""

    @property
    def dedup_ratio(self) -> float:
        total = len(self.correlated_alert_ids) + len(self.suppressed_alert_ids)
        return (len(self.suppressed_alert_ids) / total) if total else 0.0


# --------------------------------------------------------------------------- #
# Investigation -> RCA
# --------------------------------------------------------------------------- #


class ToolCall(BaseModel):
    """Provenance. Every claim an agent makes traces back to one of these."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    latency_ms: int = 0


class Evidence(BaseModel):
    """
    One atomic, attributable finding.

    `strength` is how strongly the finding discriminates between competing
    hypotheses, not how interesting it is. Evidence at strength 0 is recorded for
    the audit trail but carries no weight in scoring.
    """

    evidence_id: str
    author: AgentRole
    directive_id: str | None = None
    claim: str
    detail: str = ""
    observed_at: datetime | None = None
    strength: float = Field(0.5, ge=0.0, le=1.0)
    supports_tags: list[str] = Field(default_factory=list)
    refutes_tags: list[str] = Field(default_factory=list)
    provenance: list[ToolCall] = Field(default_factory=list)
    contains_sensitive: bool = False  # set when detail carries raw data or code


class Hypothesis(BaseModel):
    """A candidate causal story. Scored, ranked, confirmed or dropped."""

    hypothesis_id: str
    tag: str
    statement: str
    causal_chain: list[str] = Field(default_factory=list)
    proposed_by: AgentRole
    prior: float = Field(0.2, ge=0.0, le=1.0)
    posterior: float = Field(0.0, ge=0.0, le=1.0)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    refuting_evidence_ids: list[str] = Field(default_factory=list)


class SpecialistReport(BaseModel):
    """
    A specialist's answer to its directives.

    `unresolved_directives` and `follow_up_requests` are first-class: an agent is
    encouraged to say "I could not answer this, and here is what somebody else needs
    to look at". That admission is what drives a second investigation round instead
    of a confident wrong answer.
    """

    agent: AgentRole
    round: int = 1
    answered_directive_ids: list[str] = Field(default_factory=list)
    unresolved_directives: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    follow_up_requests: list[InvestigationDirective] = Field(default_factory=list)
    notes: str = ""
    duration_ms: int = 0


class RCADecision(str, Enum):
    PROCEED = "proceed_to_remediation"
    DIG_DEEPER = "dig_deeper"
    ESCALATE_HUMAN = "escalate_to_human"


class RootCauseVerdict(BaseModel):
    incident_id: str
    round: int
    ranked_hypotheses: list[Hypothesis] = Field(default_factory=list)
    top_hypothesis: Hypothesis | None = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    decision: RCADecision = RCADecision.DIG_DEEPER
    reasoning: str = ""
    contradictions: list[str] = Field(default_factory=list)
    follow_up_directives: list[InvestigationDirective] = Field(default_factory=list)
    evidence_gap: str = ""


# --------------------------------------------------------------------------- #
# Remediation proposal (pre-disclosure, pre-approval)
# --------------------------------------------------------------------------- #


class CodeChange(BaseModel):
    """A concrete patch the agent proposes to put in a pull request."""

    path: str
    change_kind: Literal[
        "transform_fix", "schema_contract", "validation_rule", "reprocess_manifest"
    ]
    rationale: str
    diff: str = ""
    tests_added: list[str] = Field(default_factory=list)


class RemediationStep(BaseModel):
    step_id: str
    action: str  # must resolve to a registered action in platform.actions
    params: dict[str, Any] = Field(default_factory=dict)
    intent: str
    risk_tier: RiskTier
    preconditions: list[str] = Field(default_factory=list)
    rollback: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None


class RemediationProposal(BaseModel):
    """
    What the agent wants to do. Two halves, because data and code are fixed
    differently: the data is repaired in place, the code is repaired via pull request.
    """

    incident_id: str
    objective: str
    strategy: str
    data_steps: list[RemediationStep] = Field(default_factory=list)
    code_changes: list[CodeChange] = Field(default_factory=list)
    containment_first: bool = True
    expected_recovery_minutes: int = 0
    do_not_do: list[str] = Field(default_factory=list)
    requires_quarantine_release: bool = False

    @property
    def max_risk_tier(self) -> RiskTier:
        tiers = [s.risk_tier for s in self.data_steps] or [RiskTier.T0_READ_ONLY]
        return max(tiers, key=lambda t: t.value)


# --------------------------------------------------------------------------- #
# Tiered disclosure -- the governance core
# --------------------------------------------------------------------------- #


class BusinessBrief(BaseModel):
    """
    What the Product Owner and Scrum Master see. Plain language only.

    Machine-checked against the redaction policy: no diffs, no SQL, no stack traces,
    no table paths, no credentials, no raw records. If the check fails the incident
    halts rather than over-disclosing.
    """

    incident_id: str
    headline: str
    what_happened: str
    who_is_affected: str
    business_impact: str
    data_at_risk: str
    proposed_fix_in_plain_terms: str
    time_to_fix: str
    risk_of_fixing: str
    risk_of_not_fixing: str
    decision_requested: str = "Approve the fix, reject it, or defer it to the backlog."


class TechnicalFixPacket(BaseModel):
    """
    What the developer and engineering manager see, and only after the business
    gate has passed. Full fidelity: root cause, evidence, diffs, rollback.
    """

    incident_id: str
    root_cause: str
    causal_chain: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    evidence_summary: list[str] = Field(default_factory=list)
    data_steps: list[RemediationStep] = Field(default_factory=list)
    code_changes: list[CodeChange] = Field(default_factory=list)
    rollback_plan: str = ""
    verification_plan: str = ""
    max_risk_tier: RiskTier = RiskTier.T0_READ_ONLY
    blast_radius: str = ""


class DisclosureBundle(BaseModel):
    """One verdict, two renderings, with the redaction verdict attached."""

    incident_id: str
    business: BusinessBrief
    technical: TechnicalFixPacket
    redaction_passed: bool = False
    redaction_findings: list[str] = Field(default_factory=list)
    technical_released: bool = False  # flips true only after the business gate passes


# --------------------------------------------------------------------------- #
# Approvals
# --------------------------------------------------------------------------- #


class GateKind(str, Enum):
    BUSINESS = "business_gate"
    TECHNICAL = "technical_gate"


class GateVerdict(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    DEFER = "defer"  # business only: park as a future fix
    TIMEOUT = "timeout"


class ApprovalRequest(BaseModel):
    request_id: str
    incident_id: str
    gate: GateKind
    role: HumanRole
    recipient: str
    tier: DisclosureTier
    subject: str
    body: str
    token: str  # signed, single-use, expiring
    sent_at: datetime
    expires_at: datetime


class ApprovalResponse(BaseModel):
    request_id: str
    incident_id: str
    gate: GateKind
    role: HumanRole
    verdict: GateVerdict
    responded_at: datetime
    comment: str = ""
    responder: str = ""


class GateOutcome(BaseModel):
    """
    The resolved verdict of one gate.

    A gate needs every required role to approve. Any single reject fails the gate;
    a defer at the business gate parks the incident. Timeouts escalate rather than
    silently approving -- the system never assumes consent.
    """

    gate: GateKind
    required_roles: list[HumanRole] = Field(default_factory=list)
    responses: list[ApprovalResponse] = Field(default_factory=list)
    verdict: GateVerdict = GateVerdict.TIMEOUT
    decided_at: datetime | None = None
    rationale: str = ""

    @property
    def approvals(self) -> list[HumanRole]:
        return [r.role for r in self.responses if r.verdict == GateVerdict.APPROVE]

    @property
    def is_unanimous_approval(self) -> bool:
        return set(self.approvals) >= set(self.required_roles)


# --------------------------------------------------------------------------- #
# Execution and outcomes
# --------------------------------------------------------------------------- #


class StepResult(BaseModel):
    step_id: str
    action: str
    status: Literal["succeeded", "failed", "skipped", "denied", "rolled_back"]
    detail: str = ""
    verification_passed: bool | None = None
    verification_detail: str = ""
    duration_ms: int = 0


class PullRequestRef(BaseModel):
    provider: str = "github"
    repo: str = ""
    number: int | None = None
    url: str = ""
    branch: str = ""
    title: str = ""
    reviewers: list[str] = Field(default_factory=list)


class TicketRef(BaseModel):
    provider: Literal["jira", "servicenow", "mock"] = "mock"
    key: str = ""
    url: str = ""
    summary: str = ""
    assignee: str = ""


class BacklogEntry(BaseModel):
    """Where a deferred incident goes: the future-issues list the PO asked for."""

    incident_id: str
    title: str
    business_summary: str
    high_level_change: str
    estimated_effort: str = "unknown"
    revisit_after: str = ""
    deferred_by: str = ""
    deferred_at: datetime | None = None


class ExecutionResult(BaseModel):
    incident_id: str
    step_results: list[StepResult] = Field(default_factory=list)
    pull_request: PullRequestRef | None = None
    quarantine_released: bool = False
    recovered: bool = False
    residual_risk: str = ""
    health_after: dict[str, Any] = Field(default_factory=dict)

    @property
    def executed(self) -> int:
        return sum(1 for s in self.step_results if s.status == "succeeded")


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class AuditEvent(BaseModel):
    """
    One link in a hash chain. Each event carries the digest of the previous event,
    so any tampering with incident history is detectable. Persisted to Snowflake.
    """

    seq: int
    incident_id: str
    at: datetime
    actor: str  # agent role or human identity
    actor_kind: Literal["agent", "human", "system"]
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    tier: DisclosureTier | None = None
    prev_hash: str = ""
    hash: str = ""

    def compute_hash(self) -> str:
        payload = json.dumps(
            {
                "seq": self.seq,
                "incident_id": self.incident_id,
                "at": self.at.isoformat(),
                "actor": self.actor,
                "actor_kind": self.actor_kind,
                "action": self.action,
                "detail": self.detail,
                "tier": self.tier.value if self.tier else None,
                "prev_hash": self.prev_hash,
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class IncidentOutcome(BaseModel):
    """The closing record for one incident. What the eval harness scores."""

    incident_id: str
    final_state: IncidentState
    # Optional because an incident can terminate before triage classifies it -- a
    # disclosure-policy halt, for instance. The eval harness treats a missing value
    # as a miss rather than crashing on it.
    severity: Severity | None = None
    incident_type: IncidentType | None = None
    root_cause_tag: str = ""
    confidence: float = 0.0
    business_gate: GateOutcome | None = None
    technical_gate: GateOutcome | None = None
    execution: ExecutionResult | None = None
    ticket: TicketRef | None = None
    backlog_entry: BacklogEntry | None = None
    audit_chain_valid: bool = False
    audit_events: int = 0
    elapsed_ms: int = 0
    notes: str = ""

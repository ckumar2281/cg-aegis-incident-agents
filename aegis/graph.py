"""
The orchestration graph.

LangGraph is doing real work here rather than decorating a linear script. Three
features of the topology are load-bearing:

**A genuine fan-out.** Triage branches to four specialist nodes that run in the same
superstep and merge their reports through a list reducer. They are independent because
they read different data, which is what makes their agreement meaningful when the RCA
node scores it.

**A loop, not a pipeline.** The edge out of `rca` is conditional. Below the confidence
threshold it routes *back* to the specialists with freshly-minted directives, and the
second round sees the first round's evidence. Bounded at two rounds so it terminates.

**Gates as first-class nodes with three exits each.** The business gate does not return
a boolean; it routes to the technical gate, the reject path, or the backlog. Each exit
is a distinct terminal state with its own side effects.

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
                                                                       ticket   execute
                                                                          ↓        ↓
                                                                         END     close → END
"""

from __future__ import annotations

import operator
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from .agents.disclosure import DisclosureOfficer
from .agents.executor import ExecutorVerifier
from .agents.forensics import ChangeCorrelator, LineageAnalyst, PipelineForensics, QualityForensics
from .agents.planner import RemediationPlanner
from .agents.rca import RCASynthesizer
from .agents.triage import TriageAgent
from .approvals import ApprovalCoordinator, Responder, ScriptedResponder
from .audit import AuditChain, TraceBus
from .config import BudgetLedger, Settings
from .contracts import (
    AgentRole,
    BacklogEntry,
    DisclosureBundle,
    ExecutionResult,
    GateKind,
    GateOutcome,
    GateVerdict,
    HumanRole,
    IncidentOutcome,
    IncidentPacket,
    IncidentState,
    InvestigationDirective,
    RCADecision,
    RemediationProposal,
    RootCauseVerdict,
    SpecialistReport,
    TicketRef,
)
from .integrations import BacklogStore, EmailSink, TicketSink, VcsClient, build_integrations
from .memory import IncidentMemory, PastIncident
from .platform.client import SimulatedPlatform
from .platform.scenarios import ScenarioSignals
from .platform.storage import S3Storage, SimulatedStorage, StorageClient, ZoneLayout
from .policy import AutonomyLadder, GateRequirement, RedactionPolicy
from .precedent import PrecedentMatch, PrecedentStore
from .reasoning import Reasoner
from .tools import ToolBelt


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class GraphState(TypedDict, total=False):
    """
    What flows through the graph.

    `reports` uses an append reducer so the four parallel specialists can write
    concurrently without clobbering each other, and so a second round adds to the
    evidence rather than replacing it.
    """

    incident_id: str
    signals: ScenarioSignals
    packet: IncidentPacket
    reports: Annotated[list[SpecialistReport], operator.add]
    active_directives: list[InvestigationDirective]
    round: int
    verdict: RootCauseVerdict
    proposal: RemediationProposal
    gate_requirement: dict[str, Any]
    precedent_applied: dict[str, Any]
    bundle: DisclosureBundle
    business_gate: GateOutcome
    technical_gate: GateOutcome
    execution: ExecutionResult
    ticket: TicketRef
    backlog: BacklogEntry
    lifecycle: str
    notes: list[str]


# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #


@dataclass
class Runtime:
    """Everything the nodes need. Built once per incident."""

    settings: Settings
    tools: ToolBelt
    reasoner: Reasoner
    trace: TraceBus
    chain: AuditChain
    ledger: BudgetLedger
    email: EmailSink
    tickets: TicketSink
    vcs: VcsClient
    backlog: BacklogStore
    approvals: ApprovalCoordinator
    ladder: AutonomyLadder
    precedents: PrecedentStore
    #: The same firewall the disclosure agent is checked against, reused on the way
    #: out. Held on the runtime so there is exactly one instance per incident and no
    #: node can quietly construct a laxer one.
    redaction: RedactionPolicy
    #: The object store. Containment is a move, so the fail-safe needs somewhere to
    #: move things to -- see platform/storage.py for why a flag is not containment.
    storage: StorageClient
    zones: ZoneLayout

    def agent_kwargs(self) -> dict[str, Any]:
        return {
            "tools": self.tools,
            "reasoner": self.reasoner,
            "settings": self.settings,
            "trace": self.trace,
            "chain": self.chain,
        }


def build_runtime(
    *,
    settings: Settings,
    platform: SimulatedPlatform,
    incident_id: str,
    responder: Responder,
    memory: IncidentMemory | None = None,
    trace: TraceBus | None = None,
    precedents: PrecedentStore | None = None,
) -> Runtime:
    trace = trace or TraceBus()
    chain = AuditChain(incident_id)
    ledger = BudgetLedger(policy=settings.budget)
    reasoner = Reasoner(settings, ledger, trace, incident_id=incident_id)
    tools = ToolBelt(platform, memory or IncidentMemory(now=platform.world.now))
    email, tickets, vcs, backlog = build_integrations(settings)
    zones = ZoneLayout(
        landing_bucket=settings.raw_bucket,
        quarantine_bucket=settings.quarantine_bucket,
    )
    if settings.storage_provider == "s3":
        storage: StorageClient = S3Storage(
            region=settings.aws_region, execute_mode=settings.storage_execute_mode
        )
    else:
        storage = SimulatedStorage(platform.world)
    redaction = RedactionPolicy()
    approvals = ApprovalCoordinator(
        settings=settings,
        email=email,
        trace=trace,
        chain=chain,
        responder=responder,
        redaction=redaction,
    )
    return Runtime(
        settings=settings,
        tools=tools,
        reasoner=reasoner,
        trace=trace,
        chain=chain,
        ledger=ledger,
        email=email,
        tickets=tickets,
        vcs=vcs,
        backlog=backlog,
        approvals=approvals,
        ladder=AutonomyLadder(),
        precedents=precedents if precedents is not None else PrecedentStore(),
        redaction=redaction,
        storage=storage,
        zones=zones,
    )


# --------------------------------------------------------------------------- #
# Graph construction
# --------------------------------------------------------------------------- #

_SPECIALIST_NODES = {
    "lineage": LineageAnalyst,
    "pipeline": PipelineForensics,
    "quality": QualityForensics,
    "change": ChangeCorrelator,
}


def build_graph(rt: Runtime):  # noqa: C901 -- the topology is the point
    """Compile the incident graph for a given runtime."""

    # -- fail-safe containment, before any agent runs ----------------------- #

    def quarantine(state: GraphState) -> GraphState:
        signals = state["signals"]
        if signals.quarantine is None:
            rt.trace.emit(
                "quarantine",
                "system",
                "no file involved -- nothing to hold back",
                {"quarantined": False},
            )
            return {"lifecycle": IncidentState.DETECTED.value, "notes": []}

        record = signals.quarantine
        # Containment is a move, not a flag. The record already names both URIs; this
        # is where the object actually leaves the landing zone. On the simulated store
        # that is an in-memory rename; on S3 it is copy-then-delete-the-source, and in
        # dry-run mode it reports what it would do and touches nothing.
        moved = rt.storage.move(record.original_uri, record.quarantine_uri)
        rt.trace.emit(
            "quarantine",
            "system",
            f"file {record.file_id} held before the warehouse -- {record.reason}",
            {
                "file_id": record.file_id,
                "uri": record.quarantine_uri,
                "object_moved": moved.ok,
                "dry_run": moved.dry_run,
            },
        )
        # Say what happened to the bytes, every time, in the visible message rather
        # than only in the event detail. The trace is what a reviewer reads; a run
        # that never touched S3 and a run that moved a 6MB object were printing the
        # identical line, which makes "quarantined" an unverifiable claim.
        if not moved.ok:
            # A failed move is not a failed incident, but it must not be silent: the
            # file is still where the loader can see it, and saying "quarantined" when
            # nothing moved is the exact class of untrue claim this project keeps
            # finding in itself.
            rt.trace.emit(
                "quarantine",
                "storage",
                f"containment incomplete -- {moved.detail}",
                {"file_id": record.file_id, "from": moved.source_uri},
            )
        elif moved.dry_run:
            rt.trace.emit(
                "quarantine",
                "storage",
                f"dry run -- would move to {moved.target_uri} (nothing touched)",
                {"file_id": record.file_id, "target": moved.target_uri, "dry_run": True},
            )
        else:
            rt.trace.emit(
                "quarantine",
                "storage",
                f"object moved to {moved.target_uri}",
                {"file_id": record.file_id, "target": moved.target_uri},
            )
        rt.chain.append(
            actor="ingestion",
            actor_kind="system",
            action="file_quarantined",
            detail={
                "file_id": record.file_id,
                "reason": record.reason,
                "quarantine_uri": record.quarantine_uri,
                # The audit trail records whether the bytes moved, separately from
                # whether the incident was marked contained.
                "object_moved": moved.ok,
                "storage_detail": moved.detail,
                "dry_run": moved.dry_run,
            },
        )
        return {
            "lifecycle": IncidentState.QUARANTINED.value,
            "notes": [f"File {record.file_id} quarantined automatically before triage."],
        }

    # -- triage -------------------------------------------------------------- #

    def triage(state: GraphState) -> GraphState:
        agent = TriageAgent(**rt.agent_kwargs())
        packet = agent.run(state["signals"], state["incident_id"])
        return {
            "packet": packet,
            "active_directives": packet.directives,
            "round": 1,
            "lifecycle": IncidentState.TRIAGED.value,
        }

    # -- the four specialists (parallel) ------------------------------------- #

    def make_specialist(key: str):
        cls = _SPECIALIST_NODES[key]

        def node(state: GraphState) -> GraphState:
            agent = cls(**rt.agent_kwargs())
            report = agent.run(
                state["packet"], state.get("active_directives", []), state.get("round", 1)
            )
            return {"reports": [report]}

        node.__name__ = f"{key}_specialist"
        return node

    # -- synthesis ----------------------------------------------------------- #

    def rca(state: GraphState) -> GraphState:
        agent = RCASynthesizer(**rt.agent_kwargs())
        verdict = agent.run(
            state["packet"],
            state.get("reports", []),
            state.get("round", 1),
            rt.settings.budget.max_investigation_rounds,
        )
        return {"verdict": verdict, "lifecycle": IncidentState.INVESTIGATING.value}

    def route_after_rca(state: GraphState) -> list[str] | str:
        verdict = state["verdict"]
        if verdict.decision is RCADecision.PROCEED:
            return "plan"
        if verdict.decision is RCADecision.ESCALATE_HUMAN:
            return "escalate"
        return list(_SPECIALIST_NODES.keys())  # fan back out for another round

    def prepare_round_two(state: GraphState) -> GraphState:
        """Carry the RCA's follow-up directives into the next round."""
        verdict = state["verdict"]
        return {
            "active_directives": verdict.follow_up_directives,
            "round": state.get("round", 1) + 1,
        }

    def escalate(state: GraphState) -> GraphState:
        packet, verdict = state["packet"], state["verdict"]
        rt.trace.emit(
            "escalate",
            AgentRole.SUPERVISOR.value,
            f"confidence {verdict.confidence:.0%} below the escalation floor -- handing to a human",
            {"confidence": verdict.confidence, "gap": verdict.evidence_gap},
        )
        rt.chain.append(
            actor=AgentRole.SUPERVISOR.value,
            actor_kind="agent",
            action="escalated_to_human",
            detail={
                "confidence": verdict.confidence,
                "evidence_gap": verdict.evidence_gap,
                "rounds_run": state.get("round", 1),
            },
        )
        ticket = rt.tickets.open_ticket(
            packet,
            summary=f"[{packet.severity.value}] {packet.title} -- automated diagnosis inconclusive",
            description=(
                f"Aegis investigated over {state.get('round', 1)} round(s) and reached only "
                f"{verdict.confidence:.0%} confidence, below the {rt.settings.rca_escalate_below:.0%} "
                f"floor required to act.\n\nLeading hypothesis: "
                f"{verdict.top_hypothesis.statement if verdict.top_hypothesis else 'none'}\n\n"
                f"Evidence gap: {verdict.evidence_gap or 'not characterised'}\n\n"
                "No remediation was attempted. Affected data remains contained."
            ),
            assignee=packet.pipeline_owner,
            labels=["escalated", "low-confidence"],
        )
        rt.email.send_notification(
            rt.settings.recipients.pipeline_owner,
            f"[{packet.severity.value}] Needs a human: {packet.title}",
            f"Automated diagnosis was inconclusive ({verdict.confidence:.0%} confidence). "
            f"Ticket {ticket.key} raised. Affected data is contained and no changes were made.",
            role=HumanRole.PIPELINE_OWNER,
            incident_id=packet.incident_id,
        )
        return {"ticket": ticket, "lifecycle": IncidentState.ESCALATED.value}

    # -- planning and disclosure -------------------------------------------- #

    def plan(state: GraphState) -> GraphState:
        agent = RemediationPlanner(**rt.agent_kwargs())
        verdict = state["verdict"]
        proposal = agent.run(state["packet"], verdict)
        requirement = rt.ladder.evaluate(state["packet"].severity, proposal)
        rt.trace.emit(
            "autonomy",
            AgentRole.SUPERVISOR.value,
            requirement.rationale,
            {
                "business_gate": requirement.business_gate,
                "technical_gate": requirement.technical_gate,
                "max_risk": proposal.max_risk_tier.label,
            },
        )
        rt.chain.append(
            actor=AgentRole.SUPERVISOR.value,
            actor_kind="agent",
            action="autonomy_evaluated",
            detail={
                "business_gate_required": requirement.business_gate,
                "technical_gate_required": requirement.technical_gate,
                "rationale": requirement.rationale,
            },
        )
        # The ladder is the default policy. A standing approval from a human can
        # override it -- but only inside boundaries the human actually set, and the
        # decision is attributed to them by name either way.
        precedent_applied: dict[str, Any] = {}
        if requirement.business_gate and verdict.top_hypothesis:
            found = rt.precedents.find(
                root_cause_tag=verdict.top_hypothesis.tag,
                asset=state["packet"].primary_asset,
                proposal=proposal,
                severity=state["packet"].severity,
                at=state["packet"].opened_at,
            )
            if isinstance(found, PrecedentMatch):
                requirement = GateRequirement(False, False, found.rationale)
                precedent_applied = {
                    "precedent_id": found.precedent.precedent_id,
                    "approved_by": found.precedent.approved_by,
                    "approved_at": found.precedent.approved_at.isoformat(),
                    "original_incident": found.precedent.incident_id,
                    "expires_at": found.precedent.expires_at.isoformat(),
                    "rationale": found.rationale,
                }
                rt.precedents.mark_applied(found.precedent.precedent_id)
                rt.trace.emit(
                    "precedent",
                    AgentRole.SUPERVISOR.value,
                    f"standing approval applies -- {found.precedent.describe()}",
                    precedent_applied,
                )
                rt.chain.append(
                    actor=AgentRole.SUPERVISOR.value,
                    actor_kind="agent",
                    action="precedent_applied",
                    detail=precedent_applied,
                )
            elif found.precedent is not None:
                # A precedent existed and did not cover this. Say so out loud: silence
                # would be indistinguishable from there never having been one.
                rt.trace.emit(
                    "precedent",
                    AgentRole.SUPERVISOR.value,
                    f"standing approval {found.precedent.precedent_id} does NOT cover "
                    f"this -- {found.reason}",
                    {"reason": found.reason},
                )
                rt.chain.append(
                    actor=AgentRole.SUPERVISOR.value,
                    actor_kind="agent",
                    action="precedent_rejected",
                    detail={
                        "precedent_id": found.precedent.precedent_id,
                        "reason": found.reason,
                    },
                )

        return {
            "proposal": proposal,
            "gate_requirement": {
                "business": requirement.business_gate,
                "technical": requirement.technical_gate,
                "rationale": requirement.rationale,
            },
            "precedent_applied": precedent_applied,
            "lifecycle": IncidentState.DIAGNOSED.value,
        }

    def route_after_plan(state: GraphState) -> str:
        """
        The autonomy decision, actually acted on.

        Everything that needs a human goes to disclosure and the gates. The narrow
        case that does not -- SEV4, fully reversible, no code change -- skips straight
        to execution and tells the pipeline owner afterwards. Waking four people to
        approve re-running a failed task is how an approval process gets ignored.
        """
        requirement = state.get("gate_requirement", {"business": True})
        return "disclose" if requirement.get("business", True) else "autonomous"

    def autonomous(state: GraphState) -> GraphState:
        """Execute without gates, then notify. Used only where the ladder allows it."""
        packet, proposal = state["packet"], state["proposal"]
        rationale = state.get("gate_requirement", {}).get("rationale", "")

        rt.trace.emit(
            "autonomous",
            AgentRole.SUPERVISOR.value,
            "proceeding without human approval -- " + rationale,
            {"severity": packet.severity.value, "max_risk": proposal.max_risk_tier.label},
        )
        agent = ExecutorVerifier(vcs=rt.vcs, **rt.agent_kwargs())
        result = agent.run(packet, proposal, approved=True)

        # Notified after the fact, not asked beforehand. The distinction matters: the
        # owner still learns what happened, they just were not made a bottleneck for it.
        rt.email.send_notification(
            rt.settings.recipients.pipeline_owner,
            f"[auto-resolved] {packet.title}",
            f"{rationale}\n\n"
            f"{result.executed} reversible step(s) completed and verified. "
            f"No human approval was required.\n\n"
            f"Residual risk: {result.residual_risk}\n\n"
            "If this was the wrong call, the autonomy rules are in policy.py and every "
            "step taken has a rollback recorded in the audit trail.",
            role=HumanRole.PIPELINE_OWNER,
            incident_id=packet.incident_id,
        )
        rt.chain.append(
            actor=AgentRole.SUPERVISOR.value,
            actor_kind="agent",
            action="remediated_autonomously",
            detail={
                "rationale": rationale,
                "steps": result.executed,
                "recovered": result.recovered,
                "notified": rt.settings.recipients.pipeline_owner,
            },
        )
        return {
            "execution": result,
            "lifecycle": (
                IncidentState.RESOLVED.value
                if result.recovered
                else IncidentState.REMEDIATING.value
            ),
        }

    def disclose(state: GraphState) -> GraphState:
        agent = DisclosureOfficer(**rt.agent_kwargs())
        bundle = agent.run(state["packet"], state["verdict"], state["proposal"])
        return {"bundle": bundle}

    def route_after_disclosure(state: GraphState) -> str:
        """A failed redaction check halts rather than over-discloses."""
        if not state["bundle"].redaction_passed:
            return "disclosure_failed"
        return "business_gate"

    def disclosure_failed(state: GraphState) -> GraphState:
        packet, bundle = state["packet"], state["bundle"]
        rt.trace.emit(
            "disclosure_blocked",
            AgentRole.DISCLOSURE.value,
            "business brief could not be made safe -- halting rather than over-disclosing",
            {"findings": bundle.redaction_findings},
        )
        rt.chain.append(
            actor=AgentRole.DISCLOSURE.value,
            actor_kind="agent",
            action="disclosure_blocked",
            detail={"findings": bundle.redaction_findings},
        )
        ticket = rt.tickets.open_ticket(
            packet,
            summary=f"[{packet.severity.value}] {packet.title} -- disclosure policy blocked automation",
            description=(
                "The business brief could not be generated without leaking technical "
                "detail, so the approval chain was not started.\n\nPolicy findings:\n"
                + "\n".join(f"- {f}" for f in bundle.redaction_findings)
            ),
            assignee=packet.pipeline_owner,
            labels=["disclosure-policy"],
        )
        return {"ticket": ticket, "lifecycle": IncidentState.ESCALATED.value}

    # -- gate 1: business ---------------------------------------------------- #

    def business_gate(state: GraphState) -> GraphState:
        outcome = rt.approvals.run_gate(GateKind.BUSINESS, state["packet"], state["bundle"])
        update: GraphState = {
            "business_gate": outcome,
            "lifecycle": IncidentState.AWAITING_BUSINESS_APPROVAL.value,
        }
        if outcome.verdict is GateVerdict.APPROVE:
            update["bundle"] = rt.approvals.release_technical_tier(state["bundle"], outcome)
        return update

    def route_after_business(state: GraphState) -> str:
        return {
            GateVerdict.APPROVE: "technical_gate",
            GateVerdict.REJECT: "rejected",
            GateVerdict.DEFER: "deferred",
            GateVerdict.TIMEOUT: "escalate",
        }[state["business_gate"].verdict]

    # -- gate 2: technical --------------------------------------------------- #

    def technical_gate(state: GraphState) -> GraphState:
        outcome = rt.approvals.run_gate(GateKind.TECHNICAL, state["packet"], state["bundle"])
        return {
            "technical_gate": outcome,
            "lifecycle": IncidentState.AWAITING_TECHNICAL_APPROVAL.value,
        }

    def route_after_technical(state: GraphState) -> str:
        verdict = state["technical_gate"].verdict
        if verdict is GateVerdict.APPROVE:
            return "execute"
        if verdict is GateVerdict.TIMEOUT:
            return "escalate"
        return "rejected"

    # -- terminal paths ------------------------------------------------------ #

    def rejected(state: GraphState) -> GraphState:
        packet, bundle = state["packet"], state["bundle"]
        gate = state.get("technical_gate") or state["business_gate"]
        signals = state["signals"]

        rt.trace.emit(
            "rejected",
            AgentRole.SUPERVISOR.value,
            f"{gate.gate.value} rejected -- data stays contained, raising a ticket",
            {"rationale": gate.rationale},
        )
        ticket = rt.tickets.open_ticket(
            packet,
            summary=f"[{packet.severity.value}] {packet.title} -- fix declined, data held",
            description=(
                f"{gate.rationale}\n\n"
                f"Business summary: {bundle.business.what_happened}\n\n"
                f"Impact: {bundle.business.business_impact}\n\n"
                f"Affected file(s): {', '.join(packet.file_ids) or 'n/a'} "
                f"(held at {signals.quarantine.quarantine_uri if signals.quarantine else 'n/a'})\n\n"
                f"Proposed fix (not applied): {bundle.business.proposed_fix_in_plain_terms}\n\n"
                "No data was modified. The affected file remains quarantined until this "
                "ticket is resolved."
            ),
            assignee=packet.pipeline_owner,
            labels=["business-declined", "quarantined"],
        )
        rt.email.send_notification(
            rt.settings.recipients.pipeline_owner,
            f"[{packet.severity.value}] Fix declined, data held: {packet.title}",
            f"{gate.rationale}\n\nTicket {ticket.key} raised and assigned to "
            f"{packet.pipeline_owner}. The affected data remains quarantined and has not "
            f"reached any report.\n\n{ticket.url}",
            role=HumanRole.PIPELINE_OWNER,
            incident_id=packet.incident_id,
        )
        # The business ruled here, so the business is told the outcome -- in the same
        # plain terms the brief was already cleared in, through the same firewall.
        rt.email.send_notification(
            rt.settings.recipients.product_owner,
            f"[not proceeding] {bundle.business.headline}",
            f"{bundle.business.what_happened}\n\n"
            f"Impact: {bundle.business.business_impact}\n\n"
            "The fix was declined, so nothing was changed. The affected data is being "
            "held back and has not reached any report. A ticket is with the owning team.",
            role=HumanRole.PRODUCT_OWNER,
            redaction=rt.redaction,
            incident_id=packet.incident_id,
        )
        rt.chain.append(
            actor=AgentRole.SUPERVISOR.value,
            actor_kind="agent",
            action="incident_rejected_and_ticketed",
            detail={
                "gate": gate.gate.value,
                "rationale": gate.rationale,
                "ticket": ticket.key,
                "pipeline_owner_notified": packet.pipeline_owner,
            },
        )
        return {"ticket": ticket, "lifecycle": IncidentState.REJECTED_QUARANTINED.value}

    def deferred(state: GraphState) -> GraphState:
        packet, bundle, proposal = state["packet"], state["bundle"], state["proposal"]
        gate = state["business_gate"]
        deferrer = next(
            (r for r in gate.responses if r.verdict is GateVerdict.DEFER), None
        )
        entry = BacklogEntry(
            incident_id=packet.incident_id,
            title=packet.title,
            business_summary=bundle.business.what_happened,
            high_level_change=bundle.business.proposed_fix_in_plain_terms,
            estimated_effort=(
                f"~{proposal.expected_recovery_minutes} min of operational work, plus "
                f"{len(proposal.code_changes)} code change(s)"
            ),
            revisit_after="next sprint planning",
            deferred_by=deferrer.role.value if deferrer else "product_owner",
            deferred_at=_now(),
        )
        rt.backlog.add(entry)
        rt.trace.emit(
            "deferred",
            AgentRole.SUPERVISOR.value,
            "parked on the future-issues backlog with the high-level change captured",
            {"title": entry.title, "effort": entry.estimated_effort},
        )
        rt.email.send_notification(
            rt.settings.recipients.pipeline_owner,
            f"[deferred] {packet.title}",
            f"{gate.rationale}\n\nAdded to the future-issues list:\n\n"
            f"  {entry.title}\n  {entry.high_level_change}\n  Effort: {entry.estimated_effort}\n\n"
            "No changes were made. Revisit at sprint planning.",
            role=HumanRole.PIPELINE_OWNER,
            incident_id=packet.incident_id,
        )
        rt.email.send_notification(
            rt.settings.recipients.product_owner,
            f"[deferred] {bundle.business.headline}",
            f"{bundle.business.what_happened}\n\n"
            f"Parked for later at your request. High-level change kept on the "
            f"future-issues list: {entry.high_level_change}\n\n"
            "Nothing was changed and the affected data is still being held back.",
            role=HumanRole.PRODUCT_OWNER,
            redaction=rt.redaction,
            incident_id=packet.incident_id,
        )
        rt.chain.append(
            actor=AgentRole.SUPERVISOR.value,
            actor_kind="agent",
            action="incident_deferred_to_backlog",
            detail={
                "deferred_by": entry.deferred_by,
                "rationale": gate.rationale,
                "high_level_change": entry.high_level_change,
            },
        )
        return {"backlog": entry, "lifecycle": IncidentState.DEFERRED_BACKLOG.value}

    def execute(state: GraphState) -> GraphState:
        agent = ExecutorVerifier(vcs=rt.vcs, **rt.agent_kwargs())
        result = agent.run(state["packet"], state["proposal"], approved=True)
        return {
            "execution": result,
            "lifecycle": (
                IncidentState.RESOLVED.value if result.recovered else IncidentState.REMEDIATING.value
            ),
        }

    def close(state: GraphState) -> GraphState:
        packet = state["packet"]
        execution = state.get("execution")
        # An autonomously-resolved incident never went through disclosure, so there is
        # no business brief to quote -- it was already notified by the autonomous node.
        bundle = state.get("bundle")
        if bundle is None:
            rt.chain.append(
                actor=AgentRole.AUDIT.value,
                actor_kind="agent",
                action="incident_closed",
                detail={
                    "path": "autonomous",
                    "recovered": execution.recovered if execution else False,
                    "steps_executed": execution.executed if execution else 0,
                    "budget": rt.ledger.summary(),
                },
            )
            return {"lifecycle": state.get("lifecycle", IncidentState.RESOLVED.value)}

        rt.email.send_notification(
            rt.settings.recipients.pipeline_owner,
            f"[resolved] {packet.title}",
            f"{bundle.business.headline}\n\n"
            f"Fix applied and verified. "
            f"{execution.executed if execution else 0} remediation step(s) completed."
            + (
                f"\n\nPull request for the underlying fix: {execution.pull_request.url}"
                if execution and execution.pull_request
                else ""
            )
            + f"\n\nResidual risk: {execution.residual_risk if execution else 'unknown'}",
            role=HumanRole.PIPELINE_OWNER,
            incident_id=packet.incident_id,
        )
        # And the business audience that approved it hears that it landed, without the
        # object names, the PR link or the step count that the owner needs.
        rt.email.send_notification(
            rt.settings.recipients.product_owner,
            f"[resolved] {bundle.business.headline}",
            f"{bundle.business.what_happened}\n\n"
            f"The fix you approved has been applied and checked. "
            f"{bundle.business.business_impact}\n\n"
            "Nothing further is needed from you.",
            role=HumanRole.PRODUCT_OWNER,
            redaction=rt.redaction,
            incident_id=packet.incident_id,
        )
        # A human approval that resolved cleanly becomes a standing decision, so the
        # same question is not asked again next month. Only on a clean resolution:
        # approving a fix that then failed verification is not a decision worth
        # repeating automatically.
        verdict = state.get("verdict")
        business = state.get("business_gate")
        recorded = None
        if (
            execution is not None
            and execution.recovered
            and verdict is not None
            and verdict.top_hypothesis is not None
            and business is not None
            and business.verdict is GateVerdict.APPROVE
        ):
            recorded = rt.precedents.record(
                incident_id=packet.incident_id,
                root_cause_tag=verdict.top_hypothesis.tag,
                asset=packet.primary_asset,
                proposal=state["proposal"],
                severity=packet.severity,
                approved_by=(
                    business.approvals[0].value if business.approvals else "product_owner"
                ),
                at=packet.opened_at,
            )
            if recorded:
                rt.trace.emit(
                    "precedent",
                    AgentRole.SUPERVISOR.value,
                    f"recorded as a standing decision until {recorded.expires_at.date()} "
                    f"-- a recurrence of this will not ask again",
                    {"precedent_id": recorded.precedent_id},
                )
                rt.chain.append(
                    actor=AgentRole.SUPERVISOR.value,
                    actor_kind="agent",
                    action="precedent_recorded",
                    detail=recorded.to_dict(),
                )

        rt.chain.append(
            actor=AgentRole.AUDIT.value,
            actor_kind="agent",
            action="incident_closed",
            detail={
                "recovered": execution.recovered if execution else False,
                "steps_executed": execution.executed if execution else 0,
                "precedent_recorded": recorded.precedent_id if recorded else None,
                "budget": rt.ledger.summary(),
            },
        )
        return {"lifecycle": state.get("lifecycle", IncidentState.RESOLVED.value)}

    # -- assemble ------------------------------------------------------------ #

    builder = StateGraph(GraphState)

    builder.add_node("quarantine", quarantine)
    builder.add_node("triage", triage)
    for key in _SPECIALIST_NODES:
        builder.add_node(key, make_specialist(key))
    builder.add_node("rca", rca)
    builder.add_node("round_two", prepare_round_two)
    builder.add_node("escalate", escalate)
    builder.add_node("plan", plan)
    builder.add_node("autonomous", autonomous)
    builder.add_node("disclose", disclose)
    builder.add_node("disclosure_failed", disclosure_failed)
    builder.add_node("business_gate", business_gate)
    builder.add_node("technical_gate", technical_gate)
    builder.add_node("rejected", rejected)
    builder.add_node("deferred", deferred)
    builder.add_node("execute", execute)
    builder.add_node("close", close)

    builder.add_edge(START, "quarantine")
    builder.add_edge("quarantine", "triage")

    # fan out
    for key in _SPECIALIST_NODES:
        builder.add_edge("triage", key)
        builder.add_edge("round_two", key)
        builder.add_edge(key, "rca")

    # the confidence gate: proceed, escalate, or loop
    builder.add_conditional_edges(
        "rca",
        lambda s: (
            "plan"
            if s["verdict"].decision is RCADecision.PROCEED
            else "escalate"
            if s["verdict"].decision is RCADecision.ESCALATE_HUMAN
            else "round_two"
        ),
        {"plan": "plan", "escalate": "escalate", "round_two": "round_two"},
    )

    # The autonomy ladder decides whether humans are involved at all.
    builder.add_conditional_edges(
        "plan", route_after_plan, {"disclose": "disclose", "autonomous": "autonomous"}
    )
    builder.add_edge("autonomous", "close")
    builder.add_conditional_edges(
        "disclose",
        route_after_disclosure,
        {"business_gate": "business_gate", "disclosure_failed": "disclosure_failed"},
    )
    builder.add_conditional_edges(
        "business_gate",
        route_after_business,
        {
            "technical_gate": "technical_gate",
            "rejected": "rejected",
            "deferred": "deferred",
            "escalate": "escalate",
        },
    )
    builder.add_conditional_edges(
        "technical_gate",
        route_after_technical,
        {"execute": "execute", "rejected": "rejected", "escalate": "escalate"},
    )
    builder.add_edge("execute", "close")

    for terminal in ("close", "rejected", "deferred", "escalate", "disclosure_failed"):
        builder.add_edge(terminal, END)

    return builder.compile()


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def run_incident(
    *,
    settings: Settings,
    platform: SimulatedPlatform,
    signals: ScenarioSignals,
    incident_id: str,
    responder: Responder | None = None,
    scripted: dict | None = None,
    memory: IncidentMemory | None = None,
    trace: TraceBus | None = None,
    precedents: PrecedentStore | None = None,
) -> tuple[IncidentOutcome, Runtime, GraphState]:
    """Run one incident end to end and return its closing record."""
    started = time.perf_counter()
    responder = responder or ScriptedResponder(scripted or {})
    rt = build_runtime(
        settings=settings,
        platform=platform,
        incident_id=incident_id,
        responder=responder,
        memory=memory,
        trace=trace,
        precedents=precedents,
    )
    graph = build_graph(rt)

    initial: GraphState = {
        "incident_id": incident_id,
        "signals": signals,
        "reports": [],
        "round": 1,
        "notes": [],
        "lifecycle": IncidentState.DETECTED.value,
    }
    # Two rounds x (4 specialists + rca) plus the linear spine, with headroom.
    final: GraphState = graph.invoke(initial, {"recursion_limit": 60})

    verification = rt.chain.verify()
    packet: IncidentPacket | None = final.get("packet")
    verdict: RootCauseVerdict | None = final.get("verdict")
    execution: ExecutionResult | None = final.get("execution")

    outcome = IncidentOutcome(
        incident_id=incident_id,
        final_state=IncidentState(final.get("lifecycle", IncidentState.ESCALATED.value)),
        severity=packet.severity if packet else None,  # type: ignore[arg-type]
        incident_type=packet.incident_type if packet else None,  # type: ignore[arg-type]
        root_cause_tag=(verdict.top_hypothesis.tag if verdict and verdict.top_hypothesis else ""),
        confidence=verdict.confidence if verdict else 0.0,
        business_gate=final.get("business_gate"),
        technical_gate=final.get("technical_gate"),
        execution=execution,
        ticket=final.get("ticket"),
        backlog_entry=final.get("backlog"),
        audit_chain_valid=verification.valid,
        audit_events=verification.events,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        notes=(
            f"rounds={final.get('round', 1)} "
            f"budget=${rt.ledger.usd:.4f} "
            f"calls={rt.ledger.calls}"
            + (" DEGRADED" if rt.ledger.degraded else "")
        ),
    )

    # Feed the outcome back into memory so the next incident can recognise it.
    if packet and verdict:
        rt.tools.memory.remember(
            PastIncident(
                incident_id=incident_id,
                occurred_at=packet.opened_at,
                asset=packet.primary_asset,
                incident_type=packet.incident_type.value,
                root_cause_tag=outcome.root_cause_tag,
                symptom=packet.title,
                resolution=(
                    state_summary(final) or "no remediation applied"
                ),
                final_state=outcome.final_state.value,
                time_to_resolve_minutes=max(1, outcome.elapsed_ms // 60000),
            )
        )
    return outcome, rt, final


def state_summary(state: GraphState) -> str:
    execution = state.get("execution")
    if execution and execution.recovered:
        return f"Remediated in {execution.executed} step(s) and verified healthy."
    if state.get("ticket"):
        return f"Declined; ticket {state['ticket'].key} raised and data held."
    if state.get("backlog"):
        return "Deferred to the future-issues backlog."
    return "No remediation applied."

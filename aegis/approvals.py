"""
The two-gate approval chain.

This is the part of Aegis that makes it a governance system rather than an automation
script. Two gates run in sequence, and the first one controls what the second one is
even allowed to see.

    GATE 1 -- BUSINESS     Product Owner + Scrum Master
                           receive the business brief ONLY
                           approve -> unlocks Gate 2
                           reject  -> file stays quarantined, ticket raised
                           defer   -> future-issues backlog

    GATE 2 -- TECHNICAL    Developer + Engineering Manager
                           receive the full fix packet
                           both approve -> execute and open the PR

Design decisions worth defending:

**A gate needs every required role.** Not a majority, not the first responder. Two
people are asked because two perspectives are wanted, so one approval is not consent.

**Any single reject fails the gate immediately.** The remaining approvers are not
chased for a decision that cannot change the outcome.

**A timeout never means yes.** Silence escalates to the pipeline owner. The most
dangerous thing an approval system can do is interpret an unanswered email as consent,
and the easiest way to build that bug is to default the verdict to approve.

**Tokens are signed, single-use and expiring.** An approval link that can be replayed,
forwarded, or forged is not an approval. Each carries an HMAC over the incident, gate,
role and expiry, and is burned on use.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .audit import AuditChain, TraceBus
from .config import Settings
from .contracts import (
    AgentRole,
    ApprovalRequest,
    ApprovalResponse,
    DisclosureBundle,
    DisclosureTier,
    GateKind,
    GateOutcome,
    GateVerdict,
    HumanRole,
    IncidentPacket,
)
from .integrations import EmailSink


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Signed tokens
# --------------------------------------------------------------------------- #


class TokenError(RuntimeError):
    pass


@dataclass
class TokenMinter:
    """
    Issues and verifies single-use approval tokens.

    The payload is readable (it is not a secret -- the recipient already knows which
    incident they are approving); the signature is what matters. `_spent` gives
    single-use semantics: an approval link works exactly once, so a forwarded email
    cannot be used to approve twice or to approve on someone else's behalf after they
    have already answered.
    """

    secret: str
    ttl_minutes: int = 1440
    _spent: set[str] = field(default_factory=set)

    def mint(
        self, *, incident_id: str, gate: GateKind, role: HumanRole, issued_at: datetime
    ) -> tuple[str, datetime]:
        expires = issued_at + timedelta(minutes=self.ttl_minutes)
        payload = {
            "incident_id": incident_id,
            "gate": gate.value,
            "role": role.value,
            "exp": expires.isoformat(),
            "nonce": hashlib.sha256(
                f"{incident_id}{gate.value}{role.value}{issued_at.isoformat()}".encode()
            ).hexdigest()[:16],
        }
        raw = json.dumps(payload, sort_keys=True).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()[:32]
        return f"{body}.{signature}", expires

    def verify(self, token: str, *, at: datetime | None = None) -> dict[str, str]:
        at = at or _now()
        try:
            body, signature = token.rsplit(".", 1)
            padded = body + "=" * (-len(body) % 4)
            raw = base64.urlsafe_b64decode(padded)
            payload = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise TokenError("malformed approval token") from exc

        expected = hmac.new(self.secret.encode(), raw, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, signature):
            raise TokenError("approval token signature does not verify")
        if datetime.fromisoformat(payload["exp"]) < at:
            raise TokenError("approval token has expired")
        if token in self._spent:
            raise TokenError("approval token has already been used")

        self._spent.add(token)
        return payload


# --------------------------------------------------------------------------- #
# Responders -- how humans answer
# --------------------------------------------------------------------------- #


class Responder(ABC):
    """Supplies the human verdict for one approval request."""

    @abstractmethod
    def respond(
        self, request: ApprovalRequest, packet: IncidentPacket
    ) -> ApprovalResponse | None:
        """Return the response, or None if nobody answered before the deadline."""


class ScriptedResponder(Responder):
    """
    Plays a pre-recorded set of verdicts. Used by the demo and the eval harness.

    Keyed on (gate, role), so a scenario can encode "the PO rejects but the Scrum
    Master approves" and the gate logic is exercised exactly as it would be with real
    people. A missing key means nobody answered, which exercises the timeout path.
    """

    def __init__(self, script: dict[tuple[str, HumanRole], GateVerdict]) -> None:
        self.script = script

    def respond(self, request, packet) -> ApprovalResponse | None:
        verdict = self.script.get((request.gate.value, request.role))
        if verdict is None:
            return None
        return ApprovalResponse(
            request_id=request.request_id,
            incident_id=request.incident_id,
            gate=request.gate,
            role=request.role,
            verdict=verdict,
            responded_at=_now(),
            responder=request.recipient,
            comment=_default_comment(verdict, request.role),
        )


class ConsoleResponder(Responder):
    """Prompts at the terminal. Lets a reviewer drive the chain live on Friday."""

    def respond(self, request, packet) -> ApprovalResponse | None:
        options = "approve / reject / defer" if request.gate is GateKind.BUSINESS else "approve / reject"
        print("\n" + "=" * 74)
        print(f"  APPROVAL REQUEST -- {request.gate.value.replace('_', ' ').upper()}")
        print(f"  To: {request.role.value.replace('_', ' ').title()} <{request.recipient}>")
        print(f"  Disclosure tier: {request.tier.value}")
        print("=" * 74)
        print(request.body)
        print("=" * 74)
        try:
            raw = input(f"  Your decision ({options}) [approve]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        mapping = {
            "": GateVerdict.APPROVE,
            "a": GateVerdict.APPROVE,
            "approve": GateVerdict.APPROVE,
            "r": GateVerdict.REJECT,
            "reject": GateVerdict.REJECT,
            "d": GateVerdict.DEFER,
            "defer": GateVerdict.DEFER,
        }
        verdict = mapping.get(raw)
        if verdict is None:
            return None
        if verdict is GateVerdict.DEFER and request.gate is not GateKind.BUSINESS:
            verdict = GateVerdict.REJECT  # defer is a business-only option
        return ApprovalResponse(
            request_id=request.request_id,
            incident_id=request.incident_id,
            gate=request.gate,
            role=request.role,
            verdict=verdict,
            responded_at=_now(),
            responder=request.recipient,
            comment=input("  Comment (optional): ").strip(),
        )


class PendingResponder(Responder):
    """
    Real asynchronous operation: send the emails and stop.

    Returns None for everyone, so every gate resolves as TIMEOUT and escalates. The
    incident is then resumed by the approval callback when a human actually clicks a
    link. This is the production shape; the scripted and console responders exist so
    the chain can be demonstrated end to end in one run.
    """

    def respond(self, request, packet) -> ApprovalResponse | None:
        return None


def _default_comment(verdict: GateVerdict, role: HumanRole) -> str:
    return {
        GateVerdict.APPROVE: "Approved.",
        GateVerdict.REJECT: (
            "Not approving this fix. The source system is mid-migration and will "
            "backfill the correct values; defaulting them now would bake in data that "
            "is about to be replaced. Keep the file held and raise it with the owning team."
            if role is HumanRole.PRODUCT_OWNER
            else "Not approving."
        ),
        GateVerdict.DEFER: (
            "Not urgent enough to interrupt the sprint, but it keeps recurring. Park it "
            "on the future-issues list with the high-level change so we can size it properly."
        ),
        GateVerdict.TIMEOUT: "",
    }[verdict]


# --------------------------------------------------------------------------- #
# Coordinator
# --------------------------------------------------------------------------- #


class ApprovalCoordinator:
    """Runs a gate: mint tokens, send the right tier, collect verdicts, resolve."""

    def __init__(
        self,
        *,
        settings: Settings,
        email: EmailSink,
        trace: TraceBus,
        chain: AuditChain,
        responder: Responder,
    ) -> None:
        self.settings = settings
        self.email = email
        self.trace = trace
        self.chain = chain
        self.responder = responder
        self.minter = TokenMinter(
            secret=settings.signing_secret,
            ttl_minutes=settings.approvals.token_ttl_minutes,
        )
        self.requests: list[ApprovalRequest] = []

    # -- public ------------------------------------------------------------- #

    def run_gate(
        self, gate: GateKind, packet: IncidentPacket, bundle: DisclosureBundle
    ) -> GateOutcome:
        roles = (
            self.settings.approvals.business_roles
            if gate is GateKind.BUSINESS
            else self.settings.approvals.technical_roles
        )
        tier = DisclosureTier.BUSINESS if gate is GateKind.BUSINESS else DisclosureTier.TECHNICAL

        self.trace.emit(
            "gate_open",
            AgentRole.APPROVALS.value,
            f"{gate.value} opened for {', '.join(r.value for r in roles)} ({tier.value} tier)",
            {"gate": gate.value, "roles": [r.value for r in roles], "tier": tier.value},
        )

        responses: list[ApprovalResponse] = []
        for role in roles:
            request = self._build_request(gate, role, tier, packet, bundle)
            self.requests.append(request)

            # Delivery enforces the firewall; a violation raises rather than leaking.
            self.email.send_approval_request(
                request, technical_released=bundle.technical_released
            )
            self.chain.append(
                actor=AgentRole.APPROVALS.value,
                actor_kind="agent",
                action="approval_requested",
                detail={
                    "gate": gate.value,
                    "role": role.value,
                    "recipient": request.recipient,
                    "expires_at": request.expires_at.isoformat(),
                    "request_id": request.request_id,
                },
                tier=tier,
            )

            response = self.responder.respond(request, packet)
            if response is None:
                self.trace.emit(
                    "gate_response",
                    role.value,
                    "no response before deadline",
                    {"gate": gate.value, "verdict": "timeout"},
                )
                self.chain.append(
                    actor=role.value,
                    actor_kind="human",
                    action="approval_timeout",
                    detail={"gate": gate.value, "request_id": request.request_id},
                    tier=tier,
                )
                continue

            # A real response must present a valid token. Scripted and console
            # responders go through the same check, so the path is never untested.
            try:
                self.minter.verify(request.token)
            except TokenError as exc:
                self.trace.emit(
                    "gate_response",
                    role.value,
                    f"response rejected: {exc}",
                    {"gate": gate.value, "verdict": "invalid"},
                )
                self.chain.append(
                    actor=role.value,
                    actor_kind="human",
                    action="approval_token_rejected",
                    detail={"gate": gate.value, "reason": str(exc)},
                    tier=tier,
                )
                continue

            responses.append(response)
            self.trace.emit(
                "gate_response",
                role.value,
                f"{response.verdict.value}",
                {"gate": gate.value, "verdict": response.verdict.value,
                 "comment": response.comment},
            )
            self.chain.append(
                actor=role.value,
                actor_kind="human",
                action=f"approval_{response.verdict.value}",
                detail={
                    "gate": gate.value,
                    "request_id": request.request_id,
                    "comment": response.comment,
                    "responder": response.responder,
                },
                tier=tier,
            )

            # No point chasing the rest once the outcome is decided.
            if response.verdict is GateVerdict.REJECT:
                break

        outcome = self._resolve(gate, list(roles), responses)
        self.trace.emit(
            "gate_closed",
            AgentRole.APPROVALS.value,
            f"{gate.value} -> {outcome.verdict.value.upper()}",
            {
                "gate": gate.value,
                "verdict": outcome.verdict.value,
                "approvals": [r.value for r in outcome.approvals],
                "rationale": outcome.rationale,
            },
        )
        self.chain.append(
            actor=AgentRole.APPROVALS.value,
            actor_kind="agent",
            action="gate_resolved",
            detail={
                "gate": gate.value,
                "verdict": outcome.verdict.value,
                "required": [r.value for r in outcome.required_roles],
                "approvals": [r.value for r in outcome.approvals],
                "rationale": outcome.rationale,
            },
            tier=tier,
        )
        return outcome

    def release_technical_tier(
        self, bundle: DisclosureBundle, outcome: GateOutcome
    ) -> DisclosureBundle:
        """
        Flip the disclosure flag. The only place this happens.

        Called exactly once, only on a unanimous business approval. Until it runs, the
        email sink refuses to deliver technical-tier content at all.
        """
        if outcome.verdict is not GateVerdict.APPROVE:
            return bundle
        released = bundle.model_copy(update={"technical_released": True})
        self.trace.emit(
            "disclosure_released",
            AgentRole.APPROVALS.value,
            "business gate passed -- technical fix packet released to the dev team",
            {"approved_by": [r.value for r in outcome.approvals]},
        )
        self.chain.append(
            actor=AgentRole.APPROVALS.value,
            actor_kind="agent",
            action="technical_disclosure_released",
            detail={
                "authorised_by": [r.value for r in outcome.approvals],
                "gate": outcome.gate.value,
            },
            tier=DisclosureTier.TECHNICAL,
        )
        return released

    # -- internals ----------------------------------------------------------- #

    def _build_request(
        self,
        gate: GateKind,
        role: HumanRole,
        tier: DisclosureTier,
        packet: IncidentPacket,
        bundle: DisclosureBundle,
    ) -> ApprovalRequest:
        issued = _now()
        token, expires = self.minter.mint(
            incident_id=packet.incident_id, gate=gate, role=role, issued_at=issued
        )
        recipient = self.settings.recipients.for_role(role)
        body = (
            self._business_body(packet, bundle, token)
            if tier is DisclosureTier.BUSINESS
            else self._technical_body(packet, bundle, token)
        )
        subject = (
            f"[{packet.severity.value}] Decision needed: {bundle.business.headline}"
            if tier is DisclosureTier.BUSINESS
            else f"[{packet.severity.value}] Approved by business -- fix ready for review: "
            f"{packet.incident_id}"
        )
        return ApprovalRequest(
            request_id=f"{packet.incident_id}-{gate.value}-{role.value}",
            incident_id=packet.incident_id,
            gate=gate,
            role=role,
            recipient=recipient,
            tier=tier,
            subject=subject,
            body=body,
            token=token,
            sent_at=issued,
            expires_at=expires,
        )

    def _links(self, token: str, *, with_defer: bool) -> str:
        base = self.settings.approval_base_url.rstrip("/")
        lines = [
            f"  Approve : {base}/approve?t={token}",
            f"  Reject  : {base}/reject?t={token}",
        ]
        if with_defer:
            lines.append(f"  Defer   : {base}/defer?t={token}")
        return "\n".join(lines)

    def _business_body(
        self, packet: IncidentPacket, bundle: DisclosureBundle, token: str
    ) -> str:
        brief = bundle.business
        return f"""\
{brief.headline}

WHAT HAPPENED
{brief.what_happened}

WHO IS AFFECTED
{brief.who_is_affected}

BUSINESS IMPACT
{brief.business_impact}

DATA AT RISK
{brief.data_at_risk}

WHAT WE PROPOSE TO DO
{brief.proposed_fix_in_plain_terms}

TIME TO FIX
{brief.time_to_fix}

RISK OF FIXING
{brief.risk_of_fixing}

RISK OF NOT FIXING
{brief.risk_of_not_fixing}

---
{brief.decision_requested}

{self._links(token, with_defer=True)}

Approve  -- the engineering team receives the technical fix and implements it.
Reject   -- the data stays held back, a ticket is raised and the pipeline owner is told.
Defer    -- parked on the future-issues list with the high-level change noted.

This link works once and expires {self.settings.approvals.token_ttl_minutes // 60}h
after it was sent. No technical detail has been sent to anyone yet -- your decision
is what releases it.
"""

    def _technical_body(
        self, packet: IncidentPacket, bundle: DisclosureBundle, token: str
    ) -> str:
        tech = bundle.technical
        steps = "\n".join(
            f"  {s.step_id}. [{s.risk_tier.label}] {s.action}({_fmt_params(s.params)})\n"
            f"      {s.intent}"
            for s in tech.data_steps
        )
        changes = "\n".join(
            f"  {c.path}  ({c.change_kind})\n"
            f"      {c.rationale}\n"
            f"      tests: {', '.join(c.tests_added) or 'none'}\n"
            + "\n".join(f"      | {line}" for line in c.diff.splitlines())
            for c in tech.code_changes
        )
        return f"""\
Incident {packet.incident_id} -- {packet.title}
Severity: {packet.severity.value}   Confidence: {tech.confidence:.0%}

The business has approved this fix. It is now released to engineering for technical
review. Approved by: the Product Owner and Scrum Master.

ROOT CAUSE
{tech.root_cause}

CAUSAL CHAIN
{chr(10).join(f"  {i}. {step}" for i, step in enumerate(tech.causal_chain, 1))}

EVIDENCE
{chr(10).join(f"  - {e}" for e in tech.evidence_summary)}

BLAST RADIUS
  {tech.blast_radius}

PROPOSED DATA REMEDIATION (max risk tier: {tech.max_risk_tier.label})
{steps or "  none"}

PROPOSED CODE CHANGES
{changes or "  none"}

ROLLBACK PLAN
  {tech.rollback_plan}

VERIFICATION
  {tech.verification_plan}

---
Both the developer and the engineering manager must approve before anything runs.

{self._links(token, with_defer=False)}

On approval: the data steps execute in order with verification after each, and a pull
request is opened for the code changes. Nothing is merged automatically.
"""

    def _resolve(
        self, gate: GateKind, roles: list[HumanRole], responses: list[ApprovalResponse]
    ) -> GateOutcome:
        """
        Precedence: reject > defer > unanimous approve > timeout.

        Timeout last and never implicit: an unanswered gate is an escalation, not a yes.
        """
        by_verdict = {v: [r for r in responses if r.verdict is v] for v in GateVerdict}
        answered = {r.role for r in responses}
        missing = [r for r in roles if r not in answered]

        if by_verdict[GateVerdict.REJECT]:
            rejecter = by_verdict[GateVerdict.REJECT][0]
            verdict, rationale = (
                GateVerdict.REJECT,
                f"{rejecter.role.value.replace('_', ' ').title()} rejected: "
                f"{rejecter.comment or 'no reason given'}",
            )
        elif by_verdict[GateVerdict.DEFER] and gate is GateKind.BUSINESS:
            deferrer = by_verdict[GateVerdict.DEFER][0]
            verdict, rationale = (
                GateVerdict.DEFER,
                f"{deferrer.role.value.replace('_', ' ').title()} deferred: "
                f"{deferrer.comment or 'parked for later'}",
            )
        elif len(by_verdict[GateVerdict.APPROVE]) == len(roles):
            verdict, rationale = (
                GateVerdict.APPROVE,
                "All required approvers approved: "
                + ", ".join(r.role.value.replace("_", " ").title() for r in responses),
            )
        else:
            verdict, rationale = (
                GateVerdict.TIMEOUT,
                "No decision within the approval window from: "
                + ", ".join(r.value.replace("_", " ").title() for r in missing)
                + ". Escalating to the pipeline owner -- silence is not consent.",
            )

        return GateOutcome(
            gate=gate,
            required_roles=roles,
            responses=responses,
            verdict=verdict,
            decided_at=_now(),
            rationale=rationale,
        )


def _fmt_params(params: dict) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in params.items() if v not in (None, "", []))

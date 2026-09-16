"""
Tiered disclosure: one verdict, two audiences, one firewall between them.

This is the agent the rest of the system exists to protect. Aegis's governance model
is that **business approval gates technical disclosure** -- the Product Owner and
Scrum Master decide whether a fix proceeds while seeing only what the incident means
for the business, and the developer does not receive the fix detail until that
decision has been made.

That ordering is unusual and it is the point. It means:

* the business decision is made on business grounds, not deferred to whoever
  understands the diff;
* an engineer cannot be pressured into shipping a fix the business has not sanctioned,
  because the fix has not been sent yet;
* the audit trail can prove exactly what each audience was shown, and when.

The firewall is machine-enforced (`policy.RedactionPolicy`), not a request in a prompt.
If the business brief contains SQL, a diff, a stack trace, an internal object name or
anything resembling a raw record, the artifact is rejected and regenerated once with
the findings fed back. If it fails twice the incident escalates to a human rather than
over-disclosing -- the system fails closed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..contracts import (
    AgentRole,
    BusinessBrief,
    DisclosureBundle,
    DisclosureTier,
    IncidentPacket,
    RemediationProposal,
    RootCauseVerdict,
    TechnicalFixPacket,
)
from ..policy import RedactionPolicy
from .base import COMMON_RULES, Agent

DISCLOSURE_SYSTEM = (
    COMMON_RULES
    + """
Your remit is to write the BUSINESS brief for a Product Owner and a Scrum Master.
They are deciding whether an engineering fix should proceed. They are not engineers.

Write for someone who owns the outcome but not the implementation. They need to know
what broke in terms of what the business does, who is affected, what it costs to act
and what it costs to wait. They do not need to know how the pipeline works.

ABSOLUTE CONSTRAINTS -- these are checked by an automated policy and a violation
rejects your entire answer:
- No SQL, no code, no diffs, no configuration snippets, no code fences.
- No database, schema, table, column or file names. Say "the payments data feed",
  not the object name. Say "a field the reports rely on", not the column.
- No file paths, storage locations, URLs, stack traces, error codes or error messages.
- No internal identifiers: no run IDs, query IDs, PR numbers, ticket keys, UUIDs.
- No email addresses, no raw data values, no long digit strings.
- Do not name individual engineers.

Write in plain sentences. Quantify impact in business terms -- how many reports, which
processes, how many people, how long. "The daily revenue report has been showing
yesterday's figures since 04:00 and three finance processes depend on it" is right.

Every field must be non-empty. `risk_of_not_fixing` must be a real assessment, not
"data stays broken" -- say what that actually costs this business.
"""
)


class BriefDraft(BaseModel):
    headline: str = Field(..., description="One plain-language line. No object names.")
    what_happened: str
    who_is_affected: str
    business_impact: str
    data_at_risk: str
    proposed_fix_in_plain_terms: str
    time_to_fix: str
    risk_of_fixing: str
    risk_of_not_fixing: str


class DisclosureOfficer(Agent):
    role = AgentRole.DISCLOSURE
    system_prompt = DISCLOSURE_SYSTEM

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.redaction = RedactionPolicy()

    def run(
        self,
        packet: IncidentPacket,
        verdict: RootCauseVerdict,
        proposal: RemediationProposal,
    ) -> DisclosureBundle:
        started = self.timed()

        technical = self._technical_packet(packet, verdict, proposal)
        draft = self._deterministic_brief(packet, verdict, proposal)

        brief, findings, attempts, source = self._render_business_brief(
            packet, verdict, proposal, draft
        )

        bundle = DisclosureBundle(
            incident_id=packet.incident_id,
            business=brief,
            technical=technical,
            redaction_passed=not findings,
            redaction_findings=[f.render() for f in findings],
            technical_released=False,  # stays false until the business gate approves
        )

        self.audit(
            "disclosure_prepared",
            {
                "redaction_passed": bundle.redaction_passed,
                "redaction_findings": bundle.redaction_findings,
                "attempts": attempts,
                "reasoning_source": source,
                "technical_released": False,
            },
            tier=DisclosureTier.BUSINESS,
        )
        self.trace.emit(
            "disclosure",
            self.role.value,
            (
                "business brief cleared the redaction firewall"
                if bundle.redaction_passed
                else f"redaction FAILED after {attempts} attempt(s): {len(findings)} findings"
            ),
            {
                "passed": bundle.redaction_passed,
                "findings": bundle.redaction_findings,
                "attempts": attempts,
            },
            duration_ms=self.ms_since(started),
        )
        self.handoff(
            AgentRole.APPROVALS,
            "business brief only; technical packet withheld pending business approval",
            {"redaction_passed": bundle.redaction_passed},
        )
        return bundle

    # -- business tier ------------------------------------------------------- #

    def _render_business_brief(
        self, packet, verdict, proposal, draft: BriefDraft
    ) -> tuple[BusinessBrief, list, int, str]:
        """
        Generate, check, and on failure regenerate once with the violations fed back.

        The deterministic draft is written to be redaction-safe by construction, so it
        is both the fallback and the floor: the model can write something better, but
        it cannot make the system unsafe by writing something worse.
        """
        user = self._brief_prompt(packet, verdict, proposal, draft)
        attempts = 0
        last_source = "heuristic"

        for attempt in (1, 2):
            attempts = attempt
            candidate, thought = self.think(
                user=user, response_model=BriefDraft, fallback_value=draft
            )
            last_source = thought.source
            brief = BusinessBrief(incident_id=packet.incident_id, **candidate.model_dump())
            findings = self.redaction.check_business_brief(brief)
            if not findings:
                return brief, [], attempt, last_source
            if attempt == 1:
                user = (
                    self._brief_prompt(packet, verdict, proposal, draft)
                    + "\n\n## YOUR PREVIOUS DRAFT WAS REJECTED\n"
                    + "The automated disclosure policy found these violations:\n"
                    + "\n".join(f"- {f.render()}" for f in findings)
                    + "\nRewrite every affected field in plain business language. "
                    "Replace any technical name with a description of what it is for."
                )

        # Failed twice. Fall back to the deterministic draft, which is safe by construction.
        safe = BusinessBrief(incident_id=packet.incident_id, **draft.model_dump())
        residual = self.redaction.check_business_brief(safe)
        return safe, residual, attempts, last_source

    def _deterministic_brief(self, packet, verdict, proposal) -> BriefDraft:
        """A redaction-safe brief built from structured facts, never from free text."""
        processes = packet.blast_radius.business_processes or ["reporting"]
        surfaces = len(packet.blast_radius.consumer_surfaces)
        teams = packet.blast_radius.affected_teams
        recurrence = next(
            (p["recurrence"] for p in packet.similar_past_incidents if "recurrence" in p), None
        )

        plain_cause = {
            "upstream_schema_drift": (
                "An external provider changed the format of the data they send us without "
                "us updating to match, so the file was rejected rather than loaded."
            ),
            "bad_deploy_join_fanout": (
                "A recent change to how we combine two data sets caused records to be "
                "counted more than once, so the reported figures are inflated."
            ),
            "upstream_vendor_outage": (
                "A third-party data provider was unavailable, so a scheduled data feed "
                "could not be collected and the reports that rely on it are out of date."
            ),
            "chronic_vendor_lateness": (
                "A supplier delivered their daily file late again. This is the third time "
                "in a month, so the underlying problem is the reliability of that feed "
                "rather than any one late delivery."
            ),
            "source_config_change": (
                "A change made in the source business system has left a field empty for a "
                "large share of records. The data is not corrupted, it is incomplete while "
                "that system is being migrated."
            ),
        }.get(
            verdict.top_hypothesis.tag if verdict.top_hypothesis else "",
            "The cause has not been established with enough confidence to act on automatically.",
        )

        plain_fix = {
            "upstream_schema_drift": (
                "Update our side to accept the provider's new format, then reload the held "
                "data so the reports catch up."
            ),
            "bad_deploy_join_fanout": (
                "Undo the recent change, rebuild the affected data from source, and add an "
                "automatic check so duplicated records are caught before they reach reports."
            ),
            "upstream_vendor_outage": (
                "Collect the data again now the provider has recovered, and add a fallback "
                "so a future outage leaves reports available with a clear 'last updated' "
                "marker instead of stopping them."
            ),
            "chronic_vendor_lateness": (
                "Collect the file when it does arrive, and change the schedule to expect "
                "this supplier to be late so it stops being raised as an incident each time."
            ),
            "source_config_change": (
                "Either accept the incomplete data temporarily while the source system "
                "finishes its migration, or continue holding it until they complete it. "
                "This is a business judgement about acceptable quality."
            ),
        }.get(
            verdict.top_hypothesis.tag if verdict.top_hypothesis else "",
            "Hold the affected data and hand over to the engineering team for diagnosis.",
        )

        impact_clause = (
            f"{surfaces} reporting surface{'s' if surfaces != 1 else ''} "
            f"and {len(processes)} business process{'es' if len(processes) != 1 else ''} "
            f"({', '.join(processes[:3])}) are affected."
        )
        if packet.blast_radius.estimated_rows_affected:
            impact_clause += (
                f" Around {packet.blast_radius.estimated_rows_affected:,} records are involved."
            )

        risk_not_fixing = {
            "SEV1": (
                "Business-critical reporting stays wrong or unavailable. Decisions and "
                "external reporting made on this data would be unreliable, and the longer "
                "it runs the more downstream work has to be redone."
            ),
            "SEV2": (
                "Affected reports continue to show incorrect figures. Teams relying on them "
                "may act on numbers that are materially wrong."
            ),
            "SEV3": (
                "The affected reports stay incomplete. Impact is contained but grows if the "
                "underlying cause repeats."
            ),
            "SEV4": (
                "Minor and contained, but the same issue is likely to recur and consume "
                "attention each time."
            ),
        }[packet.severity.value]

        headline = (
            f"{packet.severity.value}: {', '.join(processes[:2])} affected by a data feed problem"
        )
        if recurrence:
            headline += f" (recurring -- {recurrence['occurrences_in_window'] + 1} times this month)"

        return BriefDraft(
            headline=headline,
            what_happened=plain_cause,
            who_is_affected=(
                f"{len(teams)} team{'s' if len(teams) != 1 else ''} own affected data, and "
                f"{surfaces} reporting surface{'s' if surfaces != 1 else ''} are exposed to it."
                + (
                    " Financial reporting is among them."
                    if any(
                        self.tools.get_asset(a).get("is_financial")
                        for a in packet.blast_radius.downstream_assets
                    )
                    else ""
                )
            ),
            business_impact=impact_clause,
            data_at_risk=(
                "The affected data is being held and has not reached any report."
                if packet.file_ids
                else "Affected data has already reached reporting and is currently incorrect."
            ),
            proposed_fix_in_plain_terms=plain_fix,
            time_to_fix=(
                f"About {proposal.expected_recovery_minutes} minutes once approved."
                if proposal.expected_recovery_minutes
                else "No automatic fix is proposed; this needs a decision first."
            ),
            risk_of_fixing=(
                "The fix changes stored data and can be undone, but undoing it takes time."
                if proposal.max_risk_tier.value >= 2
                else "Low. The steps are reversible and no stored data is rewritten."
            ),
            risk_of_not_fixing=risk_not_fixing,
        )

    # -- technical tier ------------------------------------------------------ #

    def _technical_packet(self, packet, verdict, proposal) -> TechnicalFixPacket:
        """Full fidelity. Released only after the business gate approves."""
        rollback = "; ".join(
            f"{s.step_id}: {s.rollback['action']}" for s in proposal.data_steps if s.rollback
        ) or "No destructive step in this plan."
        verification = "; ".join(
            f"{s.step_id}: {s.verification['check']}"
            for s in proposal.data_steps
            if s.verification
        ) or "Health check on the affected assets."
        return TechnicalFixPacket(
            incident_id=packet.incident_id,
            root_cause=verdict.top_hypothesis.statement if verdict.top_hypothesis else "Undetermined",
            causal_chain=verdict.top_hypothesis.causal_chain if verdict.top_hypothesis else [],
            confidence=verdict.confidence,
            evidence_summary=[
                f"[{h.tag}] posterior {h.posterior:.2f}, "
                f"{len(h.supporting_evidence_ids)} supporting / "
                f"{len(h.refuting_evidence_ids)} refuting findings"
                for h in verdict.ranked_hypotheses[:4]
            ]
            + ([f"contradiction: {c}" for c in verdict.contradictions]),
            data_steps=proposal.data_steps,
            code_changes=proposal.code_changes,
            rollback_plan=rollback,
            verification_plan=verification,
            max_risk_tier=proposal.max_risk_tier,
            blast_radius=packet.blast_radius.summary(),
        )

    # -- prompt -------------------------------------------------------------- #

    def _brief_prompt(self, packet, verdict, proposal, draft: BriefDraft) -> str:
        processes = packet.blast_radius.business_processes or ["reporting"]
        recurrence = next(
            (p["recurrence"] for p in packet.similar_past_incidents if "recurrence" in p), None
        )
        lines = [
            "## Facts (already sanitised -- safe to paraphrase, but do not add detail)",
            f"severity: {packet.severity.value}",
            f"business_processes_affected: {', '.join(processes)}",
            f"reporting_surfaces_affected: {len(packet.blast_radius.consumer_surfaces)}",
            f"teams_affected: {len(packet.blast_radius.affected_teams)}",
            f"records_involved: {packet.blast_radius.estimated_rows_affected:,}",
            f"data_currently_held_back: {bool(packet.file_ids)}",
            f"slas_already_breached: {len(packet.blast_radius.breached_slas)}",
            f"root_cause_category: {verdict.top_hypothesis.tag if verdict.top_hypothesis else 'undetermined'}",
            f"confidence: {verdict.confidence:.0%}",
            f"estimated_fix_minutes: {proposal.expected_recovery_minutes}",
            f"fix_changes_stored_data: {proposal.max_risk_tier.value >= 2}",
            f"fix_is_reversible: {proposal.max_risk_tier.value < 3}",
        ]
        if recurrence:
            lines.append(f"recurrence: {recurrence['framing']}")
        lines.append("")
        lines.append("## A safe baseline draft (you may improve on it, but not make it more technical)")
        for key, value in draft.model_dump().items():
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append(
            "Return JSON with exactly these keys: headline, what_happened, who_is_affected, "
            "business_impact, data_at_risk, proposed_fix_in_plain_terms, time_to_fix, "
            "risk_of_fixing, risk_of_not_fixing."
        )
        return "\n".join(lines)

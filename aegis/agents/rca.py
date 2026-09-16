"""
Root-cause synthesis: turn four partial views into one ranked, scored verdict.

The scoring is explicit arithmetic rather than a model judgement, for the same reason
severity is: a reviewer needs to be able to ask "why did it believe that" and get an
answer that does not require re-running an LLM. Each hypothesis accumulates:

* its highest prior from any specialist that proposed it,
* an agreement bonus when independent specialists converge on it -- the quality
  analyst and the change correlator reaching the same tag from different data is
  worth more than either alone,
* the summed strength of evidence supporting it,
* minus, at double weight, the summed strength of evidence refuting it.

Refutation outweighs support deliberately. "Every task succeeded" should kill the
infrastructure hypothesis outright, not merely rank it lower.

The model then writes the narrative and may *lower* confidence by up to 0.15 or raise
it by at most 0.05. The asymmetry is the point: it is easy for the model to express
doubt and hard for it to manufacture certainty, so the failure mode is an extra
investigation round rather than a confident wrong fix.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import (
    AgentRole,
    Evidence,
    Hypothesis,
    IncidentPacket,
    InvestigationDirective,
    RCADecision,
    RootCauseVerdict,
    SpecialistReport,
)
from .base import COMMON_RULES, Agent
from .forensics import KNOWN_TAGS

#: Total accumulated hypothesis weight at which confidence reaches half its
#: share-based value. Tuned so a single well-corroborated hypothesis lands around
#: 0.75-0.85 rather than 1.0, leaving the confidence gate meaningful headroom.
_EVIDENCE_HALF_WEIGHT = 0.30

_ROLE_BY_NAME = {
    "lineage_impact_analyst": AgentRole.LINEAGE,
    "pipeline_forensics": AgentRole.PIPELINE,
    "data_quality_forensics": AgentRole.QUALITY,
    "change_correlator": AgentRole.CHANGE,
}


class FollowUp(BaseModel):
    assigned_to: Literal[
        "lineage_impact_analyst",
        "pipeline_forensics",
        "data_quality_forensics",
        "change_correlator",
    ]
    question: str


class RCAJudgement(BaseModel):
    reasoning: str = ""
    contradictions: list[str] = Field(default_factory=list, max_length=4)
    evidence_gap: str = ""
    follow_ups: list[FollowUp] = Field(default_factory=list, max_length=3)
    confidence_adjustment: float = Field(
        0.0,
        ge=-0.15,
        le=0.05,
        description="Lower confidence if the evidence is circumstantial. Raising it is capped.",
    )


RCA_SYSTEM = (
    COMMON_RULES
    + """
Your remit is synthesis. Four specialists have reported. Their evidence has already
been scored arithmetically and you are given the resulting ranking as fact.

You contribute:
1. `reasoning`: the causal story in 2-4 sentences. Name the mechanism, not just the
   correlation. "PR #1862 changed the join key to a non-unique column, so each order
   matched many customer rows" -- not "a deploy correlates with the anomaly".
2. `contradictions`: evidence that does not fit the leading hypothesis. Say so plainly.
   A contradiction you surface is a fix that does not get made wrongly.
3. `evidence_gap`: the single most useful thing nobody has established yet.
4. `follow_ups`: targeted questions if another round would change the answer.
5. `confidence_adjustment`: lower the computed confidence if the leading hypothesis
   rests on correlation rather than mechanism, or if a specialist reported an
   unresolved directive that matters. You may lower by up to 0.15 and raise by at
   most 0.05. Prefer lowering. Another round is far cheaper than a wrong remediation
   applied to production data.
"""
)


class RCASynthesizer(Agent):
    role = AgentRole.RCA
    system_prompt = RCA_SYSTEM

    def run(
        self,
        packet: IncidentPacket,
        reports: list[SpecialistReport],
        round_no: int,
        max_rounds: int,
    ) -> RootCauseVerdict:
        started = self.timed()
        evidence = [e for r in reports for e in r.evidence]
        hypotheses = [h for r in reports for h in r.hypotheses]

        ranked = self._score(hypotheses, evidence)
        top = ranked[0] if ranked else None
        computed_confidence = top.posterior if top else 0.0
        margin = (
            round(ranked[0].posterior - ranked[1].posterior, 3) if len(ranked) > 1 else computed_confidence
        )
        unresolved = [d for r in reports for d in r.unresolved_directives]

        fallback = RCAJudgement(
            reasoning=(
                f"{top.statement} Supported by {len(top.supporting_evidence_ids)} findings "
                f"across {len({h.proposed_by for h in hypotheses if h.tag == top.tag})} "
                "independent specialists."
                if top
                else "No hypothesis reached a usable confidence level."
            ),
            evidence_gap=(
                "No specialist established a mechanism linking the correlated change to the symptom."
                if margin < 0.15
                else ""
            ),
        )
        judgement, thought = self.think(
            user=self._brief(packet, ranked, evidence, reports, computed_confidence, margin),
            response_model=RCAJudgement,
            fallback_value=fallback,
        )

        confidence = max(0.0, min(1.0, computed_confidence + judgement.confidence_adjustment))
        if top:
            top.posterior = confidence
        decision, follow_ups = self._decide(
            confidence, margin, round_no, max_rounds, judgement, unresolved
        )

        verdict = RootCauseVerdict(
            incident_id=packet.incident_id,
            round=round_no,
            ranked_hypotheses=ranked,
            top_hypothesis=top,
            confidence=round(confidence, 3),
            decision=decision,
            reasoning=judgement.reasoning or fallback.reasoning,
            contradictions=judgement.contradictions,
            follow_up_directives=follow_ups,
            evidence_gap=judgement.evidence_gap,
        )

        self.audit(
            "root_cause_assessed",
            {
                "round": round_no,
                "top_tag": top.tag if top else None,
                "computed_confidence": round(computed_confidence, 3),
                "model_adjustment": judgement.confidence_adjustment,
                "final_confidence": verdict.confidence,
                "margin": margin,
                "decision": decision.value,
                "ranking": [(h.tag, round(h.posterior, 3)) for h in ranked[:4]],
                "contradictions": judgement.contradictions,
                "reasoning_source": thought.source,
            },
        )
        self.trace.emit(
            "rca",
            self.role.value,
            f"{top.tag if top else 'no hypothesis'} @ {verdict.confidence:.0%} -> {decision.value}",
            {
                "ranking": [(h.tag, round(h.posterior, 3)) for h in ranked[:4]],
                "margin": margin,
                "round": round_no,
                "adjustment": judgement.confidence_adjustment,
            },
            duration_ms=self.ms_since(started),
        )
        self.handoff(
            AgentRole.PLANNER if decision == RCADecision.PROCEED else AgentRole.SUPERVISOR,
            f"verdict: {decision.value}",
            {"confidence": verdict.confidence},
        )
        return verdict

    # -- scoring ------------------------------------------------------------- #

    def _score(self, hypotheses: list[Hypothesis], evidence: list[Evidence]) -> list[Hypothesis]:
        by_tag: dict[str, list[Hypothesis]] = {}
        for h in hypotheses:
            by_tag.setdefault(h.tag, []).append(h)

        support: dict[str, float] = {}
        refute: dict[str, float] = {}
        support_ids: dict[str, list[str]] = {}
        refute_ids: dict[str, list[str]] = {}
        for e in evidence:
            for tag in e.supports_tags:
                support[tag] = support.get(tag, 0.0) + e.strength
                support_ids.setdefault(tag, []).append(e.evidence_id)
            for tag in e.refutes_tags:
                refute[tag] = refute.get(tag, 0.0) + e.strength
                refute_ids.setdefault(tag, []).append(e.evidence_id)

        # Evidence can implicate a tag no specialist formally proposed.
        for tag in set(support) | set(refute):
            if tag not in by_tag and tag in KNOWN_TAGS:
                by_tag[tag] = [
                    Hypothesis(
                        hypothesis_id=f"H-INF-{abs(hash(tag)) % 997:03d}",
                        tag=tag,
                        statement=KNOWN_TAGS[tag],
                        proposed_by=AgentRole.RCA,
                        prior=0.15,
                    )
                ]

        scored: list[tuple[float, Hypothesis]] = []
        for tag, group in by_tag.items():
            best = max(group, key=lambda h: h.prior)
            agents = {h.proposed_by for h in group}
            agreement = 1.0 + 0.25 * (len(agents) - 1)
            raw = best.prior * agreement
            raw += 0.15 * support.get(tag, 0.0)
            raw -= 0.30 * refute.get(tag, 0.0)  # refutation outweighs support
            merged = best.model_copy(
                update={
                    "supporting_evidence_ids": support_ids.get(tag, []),
                    "refuting_evidence_ids": refute_ids.get(tag, []),
                }
            )
            scored.append((max(0.0, raw), merged))

        # Two factors, multiplied.
        #
        # `share` is how much of the total weight this hypothesis holds -- it answers
        # "compared to the alternatives, how good is this one?".
        #
        # `mass` answers the question share cannot: "is there enough evidence here to
        # be confident at all?". Without it, eliminating every rival hands the survivor
        # a posterior of 1.0 even when almost nothing supports it, which is how an
        # investigation with one weak lead reports certainty. The saturating form means
        # evidence has diminishing returns and confidence approaches but never reaches 1.
        total = sum(value for value, _ in scored) or 1.0
        mass = total / (total + _EVIDENCE_HALF_WEIGHT)

        ranked: list[Hypothesis] = []
        for value, hypothesis in sorted(scored, key=lambda pair: pair[0], reverse=True):
            share = value / total
            hypothesis.posterior = round(share * mass, 4)
            ranked.append(hypothesis)
        return ranked

    # -- gate ---------------------------------------------------------------- #

    def _decide(
        self,
        confidence: float,
        margin: float,
        round_no: int,
        max_rounds: int,
        judgement: RCAJudgement,
        unresolved: list[str],
    ) -> tuple[RCADecision, list[InvestigationDirective]]:
        threshold = self.settings.rca_confidence_threshold
        escalate_below = self.settings.rca_escalate_below

        confident = confidence >= threshold and margin >= 0.12
        if confident:
            return RCADecision.PROCEED, []

        if round_no < max_rounds:
            follow_ups = [
                InvestigationDirective(
                    directive_id=f"R{round_no + 1}-{idx}",
                    assigned_to=_ROLE_BY_NAME[item.assigned_to],
                    question=item.question,
                    rationale=(
                        f"Round {round_no} reached only {confidence:.0%} confidence "
                        f"(margin {margin:.2f}); this gap is what is blocking a decision."
                    ),
                    priority="critical",
                    round=round_no + 1,
                )
                for idx, item in enumerate(judgement.follow_ups, start=1)
            ]
            if not follow_ups:
                follow_ups = self._default_follow_ups(round_no, unresolved)
            return RCADecision.DIG_DEEPER, follow_ups

        if confidence < escalate_below:
            return RCADecision.ESCALATE_HUMAN, []
        return RCADecision.PROCEED, []

    def _default_follow_ups(
        self, round_no: int, unresolved: list[str]
    ) -> list[InvestigationDirective]:
        """If the model offered no follow-ups, ask the obvious discriminating questions."""
        return [
            InvestigationDirective(
                directive_id=f"R{round_no + 1}-A",
                assigned_to=AgentRole.CHANGE,
                question=(
                    "Widen the change window to 7 days and look specifically for changes "
                    "to upstream sources, not just to our own repository."
                ),
                rationale="The leading hypothesis lacks a correlated change.",
                priority="critical",
                round=round_no + 1,
            ),
            InvestigationDirective(
                directive_id=f"R{round_no + 1}-B",
                assigned_to=AgentRole.QUALITY,
                question=(
                    "Compare the affected asset against its nearest healthy sibling to "
                    "isolate which specific column or key is behaving differently."
                ),
                rationale=(
                    f"Unresolved directives from round {round_no}: {', '.join(unresolved) or 'none'}."
                ),
                priority="high",
                round=round_no + 1,
            ),
        ]

    # -- prompt -------------------------------------------------------------- #

    def _brief(
        self,
        packet: IncidentPacket,
        ranked: list[Hypothesis],
        evidence: list[Evidence],
        reports: list[SpecialistReport],
        confidence: float,
        margin: float,
    ) -> str:
        lines = [
            f"## Incident {packet.incident_id} ({packet.severity.value}) -- {packet.title}",
            f"primary_asset: {packet.primary_asset}  classified_as: {packet.incident_type.value}",
            f"blast_radius: {packet.blast_radius.summary()}",
            "",
            "## Computed hypothesis ranking (authoritative)",
        ]
        for idx, h in enumerate(ranked[:5], start=1):
            lines.append(
                f"{idx}. [{h.tag}] posterior={h.posterior:.3f} "
                f"support={len(h.supporting_evidence_ids)} refute={len(h.refuting_evidence_ids)}"
            )
            lines.append(f"   {h.statement}")
        lines.append("")
        lines.append(f"computed_confidence: {confidence:.3f}   margin_over_second: {margin:.3f}")
        lines.append(
            f"gate: PROCEED needs confidence >= {self.settings.rca_confidence_threshold:.2f} "
            "and margin >= 0.12"
        )
        lines.append("")
        lines.append("## Evidence")
        for e in evidence:
            tags = ""
            if e.supports_tags:
                tags += f" supports={','.join(e.supports_tags)}"
            if e.refutes_tags:
                tags += f" refutes={','.join(e.refutes_tags)}"
            lines.append(f"- [{e.evidence_id}] ({e.author.value}, strength {e.strength:.2f}){tags}")
            lines.append(f"  {e.claim}")
            if e.detail:
                lines.append(f"  detail: {e.detail}")
        unresolved = [d for r in reports for d in r.unresolved_directives]
        if unresolved:
            lines.append("")
            lines.append(f"## Unresolved directives: {', '.join(unresolved)}")
        if packet.similar_past_incidents:
            lines.append("")
            lines.append("## Prior incidents")
            for past in packet.similar_past_incidents:
                if "recurrence" in past:
                    lines.append(f"- {past['recurrence']['framing']}")
                else:
                    lines.append(
                        f"- {past['incident_id']}: {past['root_cause_tag']} -> {past['resolution']}"
                    )
        lines.append("")
        lines.append(
            'Return JSON: {"reasoning", "contradictions": [...], "evidence_gap", '
            '"follow_ups": [{"assigned_to", "question"}], "confidence_adjustment"}'
        )
        return "\n".join(lines)

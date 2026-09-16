"""
Triage: turn a noisy alert stream into one scoped, classified incident with a work plan.

Triage is the agent that most obviously earns its keep. A vendor outage in this
platform fires ten alerts across nine assets; a system without correlation opens ten
incidents and pages four teams for one root cause. The correlation here is structural
rather than statistical: alerts are folded together when lineage says one explains the
other, which is both cheaper and more defensible than clustering on text similarity.

Severity is computed in Python, not by the model, because it drives paging and
approval SLAs and a reviewer must be able to reproduce it. The model names the
incident and explains the score; it does not set it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import (
    AgentRole,
    Alert,
    BlastRadius,
    IncidentPacket,
    IncidentType,
    InvestigationDirective,
    Severity,
    ValidationFailure,
)
from ..platform.scenarios import ScenarioSignals
from ..policy import distance_weight, score_severity
from .base import COMMON_RULES, Agent

_ROLE_BY_NAME = {
    "lineage_impact_analyst": AgentRole.LINEAGE,
    "pipeline_forensics": AgentRole.PIPELINE,
    "data_quality_forensics": AgentRole.QUALITY,
    "change_correlator": AgentRole.CHANGE,
}


class ExtraDirective(BaseModel):
    assigned_to: Literal[
        "lineage_impact_analyst",
        "pipeline_forensics",
        "data_quality_forensics",
        "change_correlator",
    ]
    question: str
    rationale: str = ""


class TriageNarrative(BaseModel):
    """What the model contributes on top of the deterministic scoping."""

    title: str = Field(..., description="One line, specific, names the asset and the symptom")
    severity_rationale: str
    suppression_rationale: str
    additional_directives: list[ExtraDirective] = Field(default_factory=list, max_length=3)


TRIAGE_SYSTEM = (
    COMMON_RULES
    + """
Your remit is triage. The scoping work -- which alerts belong together, how bad this
is, which assets are downstream -- has already been computed deterministically and is
given to you as fact. Do not recompute or contradict it.

You contribute three things:
1. `title`: one specific line naming the asset and the symptom. Not "Data issue".
2. `severity_rationale`: explain the given severity in business terms, one or two sentences.
3. `suppression_rationale`: explain why the suppressed alerts are the same incident.

You may optionally add up to 3 `additional_directives` -- targeted questions for a
specialist that the standard directive set would miss for this particular incident.
Only add one if it is genuinely specific to what you were shown. An empty list is a
perfectly good answer.
"""
)


class TriageAgent(Agent):
    role = AgentRole.TRIAGE
    system_prompt = TRIAGE_SYSTEM

    def run(self, signals: ScenarioSignals, incident_id: str) -> IncidentPacket:
        started = self.timed()
        alerts = list(signals.alerts)
        failures = list(signals.validation_failures)

        primary = self._primary_asset(signals, alerts)
        correlated, suppressed, unrelated = self._correlate(primary, alerts)
        incident_type = self._classify(failures, alerts, primary)
        blast = self._blast_radius(primary)
        rows_affected = self._rows_affected(signals, failures)
        data_is_wrong = incident_type in (
            IncidentType.VOLUME_ANOMALY,
            IncidentType.QUALITY_DEGRADATION,
            IncidentType.REFERENTIAL_BREAK,
        )
        primary_meta = self.tools.get_asset(primary)

        weighted_tier1, financial_proximity, weighted_consumers = self._proximity(primary)
        severity, score, rationale = score_severity(
            tier1_downstream=weighted_tier1,
            total_downstream=len(blast.downstream_assets),
            consumer_surfaces=weighted_consumers,
            sla_breaches=len(blast.breached_slas),
            rows_affected=rows_affected,
            financial_proximity=financial_proximity,
            primary_tier=int(primary_meta.get("tier", 3) or 3),
            data_is_wrong=data_is_wrong,
        )
        blast.score = score
        blast.estimated_rows_affected = rows_affected

        symptom = "; ".join(a.message for a in alerts[:4])
        similar = self.tools.similar_incidents(
            asset=primary, incident_type=incident_type.value, symptom=symptom
        )
        recurrence = self.tools.memory.recurrence(
            asset=primary, incident_type=incident_type.value, symptom=symptom
        )
        if recurrence["is_recurrence"]:
            similar = [*similar, {"recurrence": recurrence}]

        window_end = max((a.fired_at for a in alerts), default=self.tools.platform.world.now)  # type: ignore[attr-defined]
        window_start = min(
            (a.fired_at for a in alerts), default=window_end - timedelta(hours=6)
        ) - timedelta(hours=6)

        directives = self._directives(primary, incident_type, blast, signals)

        fallback = IncidentPacket(
            incident_id=incident_id,
            title=f"{incident_type.value.replace('_', ' ').title()} on {primary}",
            opened_at=window_end,
            incident_type=incident_type,
            severity=severity,
            severity_rationale=rationale,
            primary_asset=primary,
            symptom_assets=sorted({a.asset for a in alerts if a.asset != primary}),
            file_ids=[signals.file.file_id] if signals.file else [],
            correlated_alert_ids=[a.alert_id for a in correlated],
            suppressed_alert_ids=[a.alert_id for a in suppressed],
            suppression_rationale=(
                f"{len(suppressed)} alert(s) are downstream of {primary} in lineage and are "
                f"explained by the same root cause. {len(unrelated)} alert(s) were excluded "
                "as not lineage-connected."
            ),
            window_start=window_start,
            window_end=window_end,
            blast_radius=blast,
            directives=directives,
            similar_past_incidents=similar,
            pipeline_owner=primary_meta.get("owner_team", "data-platform"),
        )

        narrative, thought = self.think(
            user=self._brief(fallback, alerts, correlated, suppressed, unrelated, failures),
            response_model=TriageNarrative,
            fallback_value=TriageNarrative(
                title=fallback.title,
                severity_rationale=fallback.severity_rationale,
                suppression_rationale=fallback.suppression_rationale,
            ),
        )

        packet = fallback.model_copy(
            update={
                "title": narrative.title or fallback.title,
                "severity_rationale": (
                    f"{rationale} {narrative.severity_rationale}".strip()
                ),
                "suppression_rationale": (
                    narrative.suppression_rationale or fallback.suppression_rationale
                ),
                "directives": [
                    *directives,
                    *self._extra_directives(narrative.additional_directives, len(directives)),
                ],
            }
        )

        self.audit(
            "incident_triaged",
            {
                "incident_type": packet.incident_type.value,
                "severity": packet.severity.value,
                "blast_score": round(score, 3),
                "alerts_in": len(alerts),
                "alerts_correlated": len(correlated),
                "alerts_suppressed": len(suppressed),
                "alerts_excluded": len(unrelated),
                "dedup_ratio": round(packet.dedup_ratio, 3),
                "reasoning_source": thought.source,
            },
        )
        self.trace.emit(
            "triage",
            self.role.value,
            f"{len(alerts)} alerts -> 1 incident ({packet.severity.value})",
            {
                "suppressed": len(suppressed),
                "excluded": len(unrelated),
                "primary": primary,
                "type": packet.incident_type.value,
            },
            duration_ms=self.ms_since(started),
        )
        for directive in packet.directives:
            self.handoff(
                directive.assigned_to,
                directive.question[:80],
                {"directive_id": directive.directive_id, "priority": directive.priority},
            )
        return packet

    # -- scoping ------------------------------------------------------------- #

    def _primary_asset(self, signals: ScenarioSignals, alerts: list[Alert]) -> str:
        """
        The asset the incident is rooted at.

        A quarantined file names its own target table -- that is the true entry point,
        even when the loudest alerts are three hops downstream. Otherwise walk the
        lineage: the root is the alerting asset with no other alerting asset above it.
        """
        if signals.file:
            return signals.file.target_table
        assets = {a.asset for a in alerts}
        if not assets:
            return "UNKNOWN"
        roots = [a for a in assets if not (set(self._upstream_names(a)) & assets)]
        if not roots:
            roots = sorted(assets)
        return max(roots, key=lambda a: len(set(self._downstream_names(a)) & assets))

    def _upstream_names(self, asset: str) -> list[str]:
        return [a["name"] for a in self.tools.upstream(asset)]

    def _downstream_names(self, asset: str) -> list[str]:
        return [a["name"] for a in self.tools.downstream(asset)]

    def _correlate(
        self, primary: str, alerts: list[Alert]
    ) -> tuple[list[Alert], list[Alert], list[Alert]]:
        """Split alerts into: the incident, its cascade, and everything unrelated."""
        downstream = set(self._downstream_names(primary))
        correlated = [a for a in alerts if a.asset == primary]
        suppressed = [a for a in alerts if a.asset in downstream]
        accounted = {a.alert_id for a in correlated} | {a.alert_id for a in suppressed}
        unrelated = [a for a in alerts if a.alert_id not in accounted]
        if not correlated and suppressed:
            correlated, suppressed = suppressed[:1], suppressed[1:]
        return correlated, suppressed, unrelated

    def _classify(
        self, failures: list[ValidationFailure], alerts: list[Alert], primary: str
    ) -> IncidentType:
        kinds = {f.rule_kind for f in failures}
        if "schema" in kinds:
            return IncidentType.SCHEMA_DRIFT
        if "nullability" in kinds or "domain" in kinds:
            return IncidentType.QUALITY_DEGRADATION
        if "referential" in kinds:
            return IncidentType.REFERENTIAL_BREAK
        if "volume" in kinds:
            return IncidentType.VOLUME_ANOMALY

        rules = {a.rule for a in alerts}
        if {"duplicate_count", "row_count_anomaly"} & rules:
            return IncidentType.VOLUME_ANOMALY
        if "null_rate" in rules:
            return IncidentType.QUALITY_DEGRADATION
        if "copy_failed" in rules:
            diff = self.tools.schema_diff(primary)
            return (
                IncidentType.SCHEMA_DRIFT if diff.get("changed") else IncidentType.INGESTION_FAILURE
            )
        if "freshness_sla" in rules or "task_failed" in rules:
            return IncidentType.FRESHNESS_BREACH
        if "credit_spike" in rules:
            return IncidentType.COST_ANOMALY
        return IncidentType.UNKNOWN

    def _blast_radius(self, primary: str) -> BlastRadius:
        downstream = self.tools.downstream(primary)
        consumers = self.tools.consumers(primary)
        breached: list[str] = []
        for asset in [self.tools.get_asset(primary), *downstream]:
            name, sla = asset.get("name"), asset.get("sla_minutes")
            if not name or not sla:
                continue
            summary = self.tools.metric_summary(name)
            if not summary.get("available"):
                continue
            if summary["freshness_lag_min"]["latest"] > sla:
                breached.append(name)
        return BlastRadius(
            downstream_assets=[a["name"] for a in downstream],
            tier1_assets=[a["name"] for a in downstream if a.get("tier") == 1],
            consumer_surfaces=sorted({c["asset"] for c in consumers}),
            affected_teams=sorted(
                {a.get("owner_team", "") for a in downstream if a.get("owner_team")}
            ),
            breached_slas=breached,
            business_processes=sorted(
                {a.get("business_process", "") for a in downstream if a.get("business_process")}
            ),
        )

    def _proximity(self, primary: str) -> tuple[float, float, float]:
        """
        Distance-weighted tier-1 exposure, financial proximity, and consumer exposure.

        Counting hops matters because lineage reachability overstates impact: in a
        warehouse of any size almost everything eventually reaches the executive
        dashboard, so an unweighted count makes every incident a SEV1 and the severity
        scale stops carrying information.

        All three factors are weighted, not just some. Leaving one unweighted was
        enough on its own to push a mid-severity incident into SEV1, because four
        distant dashboards saturated the cap that three nearby ones were meant to.
        """
        downstream = self.tools.downstream(primary)
        depth_of = {a["name"]: int(a.get("depth", 1)) for a in downstream}
        primary_meta = self.tools.get_asset(primary)

        weighted_tier1 = sum(
            distance_weight(depth_of.get(a["name"], 1))
            for a in downstream
            if a.get("tier") == 1
        )
        weighted_consumers = sum(
            distance_weight(depth_of.get(a["name"], 1))
            for a in downstream
            if a.get("consumer_kind")
        )

        if primary_meta.get("is_financial"):
            financial_proximity = 1.0
        else:
            financial_depths = [
                depth_of.get(a["name"], 1) for a in downstream if a.get("is_financial")
            ]
            financial_proximity = (
                distance_weight(min(financial_depths)) if financial_depths else 0.0
            )
        return weighted_tier1, financial_proximity, weighted_consumers

    def _rows_affected(self, signals: ScenarioSignals, failures: list[ValidationFailure]) -> int:
        if failures:
            return max(f.failed_rows for f in failures)
        if signals.file and signals.file.row_count:
            return signals.file.row_count
        best = 0
        for alert in signals.alerts:
            for key in ("duplicates", "observed", "rows"):
                value = alert.observed.get(key)
                if isinstance(value, (int, float)):
                    best = max(best, int(value))
        return best

    # -- work plan ----------------------------------------------------------- #

    def _directives(
        self,
        primary: str,
        incident_type: IncidentType,
        blast: BlastRadius,
        signals: ScenarioSignals,
    ) -> list[InvestigationDirective]:
        """
        The baseline work plan: one accountable question per specialist, plus
        type-specific follow-ups. Guaranteed coverage regardless of what the model does.
        """
        window = "the last 72 hours"
        out: list[InvestigationDirective] = [
            InvestigationDirective(
                directive_id="D1",
                assigned_to=AgentRole.LINEAGE,
                question=(
                    f"Map the blast radius of {primary}: which downstream assets are "
                    "affected, which are tier-1, which consumer surfaces and business "
                    "processes are exposed, and which SLAs are already breached."
                ),
                rationale="Severity and the business brief both depend on who is actually hurt.",
                priority="critical",
                context={"primary_asset": primary},
            ),
            InvestigationDirective(
                directive_id="D2",
                assigned_to=AgentRole.PIPELINE,
                question=(
                    f"Establish what the orchestrator did around {primary}: which task "
                    "runs and file loads failed, with error codes, retry counts and "
                    "timings, and whether failures are upstream or downstream of the fault."
                ),
                rationale="Distinguishes a broken pipeline from a pipeline faithfully loading bad data.",
                priority="critical",
                context={"primary_asset": primary},
            ),
            InvestigationDirective(
                directive_id="D3",
                assigned_to=AgentRole.QUALITY,
                question=(
                    f"Quantify how the data for {primary} deviates from its baseline: "
                    "row counts against the 30-day median, null rates, duplicate keys, "
                    "and any schema difference from the agreed contract."
                ),
                rationale="Separates 'data is missing' from 'data is present but wrong'.",
                priority="critical",
                context={"primary_asset": primary},
            ),
            InvestigationDirective(
                directive_id="D4",
                assigned_to=AgentRole.CHANGE,
                question=(
                    f"Identify every deploy, config change, vendor notice or schema "
                    f"registry update in {window} touching {primary} or its upstreams, "
                    "and assess how well each correlates in time with the symptom onset."
                ),
                rationale="Most data incidents are caused by a change; find it or rule it out.",
                priority="critical",
                context={"primary_asset": primary, "window": window},
            ),
        ]

        seq = 5
        if incident_type == IncidentType.SCHEMA_DRIFT:
            out.append(
                InvestigationDirective(
                    directive_id=f"D{seq}",
                    assigned_to=AgentRole.QUALITY,
                    question=(
                        f"For {primary}, determine precisely which columns were added, "
                        "removed or renamed versus the contract, and whether the change "
                        "is a rename that can be mapped or a genuine data loss."
                    ),
                    rationale="A mappable rename and a dropped field need different fixes.",
                    priority="high",
                    context={"primary_asset": primary},
                )
            )
            seq += 1
        if incident_type in (IncidentType.VOLUME_ANOMALY, IncidentType.REFERENTIAL_BREAK):
            out.append(
                InvestigationDirective(
                    directive_id=f"D{seq}",
                    assigned_to=AgentRole.CHANGE,
                    question=(
                        "Every task succeeded, so look for a logic change: identify any "
                        "merged pull request altering join, filter or grain logic for "
                        f"{primary}, and name the PR number and author."
                    ),
                    rationale="Silent corruption is nearly always a code change, not an outage.",
                    priority="critical",
                    context={"primary_asset": primary},
                )
            )
            seq += 1
        if signals.file is not None:
            out.append(
                InvestigationDirective(
                    directive_id=f"D{seq}",
                    assigned_to=AgentRole.QUALITY,
                    question=(
                        f"Assess the quarantined file {signals.file.file_id}: is its "
                        "content safe to reprocess once the pipeline is fixed, or is the "
                        "data itself wrong at source?"
                    ),
                    rationale=(
                        "Determines whether remediation is 'fix and replay' or "
                        "'hold and escalate to the source system owner'."
                    ),
                    priority="high",
                    context={"file_id": signals.file.file_id},
                )
            )
        return out

    def _extra_directives(
        self, extras: list[ExtraDirective], offset: int
    ) -> list[InvestigationDirective]:
        out: list[InvestigationDirective] = []
        for idx, extra in enumerate(extras, start=offset + 1):
            role = _ROLE_BY_NAME.get(extra.assigned_to)
            if role is None:
                continue
            out.append(
                InvestigationDirective(
                    directive_id=f"D{idx}",
                    assigned_to=role,
                    question=extra.question,
                    rationale=extra.rationale or "Added by triage for this specific incident.",
                    priority="normal",
                )
            )
        return out

    # -- prompt -------------------------------------------------------------- #

    def _brief(
        self,
        packet: IncidentPacket,
        alerts: list[Alert],
        correlated: list[Alert],
        suppressed: list[Alert],
        unrelated: list[Alert],
        failures: list[ValidationFailure],
    ) -> str:
        lines = [
            "## Computed scoping (authoritative -- do not contradict)",
            f"primary_asset: {packet.primary_asset}",
            f"incident_type: {packet.incident_type.value}",
            f"severity: {packet.severity.value} (blast score {packet.blast_radius.score:.2f})",
            f"severity_drivers: {packet.severity_rationale}",
            f"blast_radius: {packet.blast_radius.summary()}",
            f"tier1_downstream: {', '.join(packet.blast_radius.tier1_assets) or 'none'}",
            f"consumer_surfaces: {', '.join(packet.blast_radius.consumer_surfaces) or 'none'}",
            f"business_processes: {', '.join(packet.blast_radius.business_processes) or 'none'}",
            f"breached_slas: {', '.join(packet.blast_radius.breached_slas) or 'none'}",
            "",
            "## Alerts folded into this incident",
        ]
        for alert in correlated:
            lines.append(f"- [{alert.alert_id}] {alert.asset} / {alert.rule}: {alert.message}")
        lines.append("")
        lines.append("## Alerts suppressed as downstream cascade")
        for alert in suppressed:
            lines.append(f"- [{alert.alert_id}] {alert.asset} / {alert.rule}: {alert.message}")
        if unrelated:
            lines.append("")
            lines.append("## Alerts excluded (not lineage-connected)")
            for alert in unrelated:
                lines.append(f"- [{alert.alert_id}] {alert.asset}: {alert.message}")
        if failures:
            lines.append("")
            lines.append("## Validation failures")
            for failure in failures:
                lines.append(
                    f"- {failure.rule} ({failure.rule_kind}): expected {failure.expected}; "
                    f"actual {failure.actual}; {failure.failed_rows:,}/{failure.total_rows:,} rows"
                )
        if packet.similar_past_incidents:
            lines.append("")
            lines.append("## Similar past incidents")
            for past in packet.similar_past_incidents:
                if "recurrence" in past:
                    lines.append(f"- RECURRENCE: {past['recurrence']['framing']}")
                else:
                    lines.append(
                        f"- {past['incident_id']} ({past.get('days_ago')}d ago, "
                        f"similarity {past['similarity']}): {past['symptom']} "
                        f"-> {past['resolution']}"
                    )
        lines.append("")
        lines.append(
            "Return JSON with keys: title, severity_rationale, suppression_rationale, "
            "additional_directives (list, may be empty; each has assigned_to, question, rationale)."
        )
        return "\n".join(lines)

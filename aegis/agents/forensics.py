"""
The four forensic specialists.

They run in parallel and deliberately overlap only a little. Each owns one question
about the incident, and the separation is what makes the evidence worth scoring: when
the change correlator and the quality analyst independently point at the same cause,
that agreement means something, because they looked at different data to get there.

| agent              | owns                                                          |
|--------------------|---------------------------------------------------------------|
| LineageAnalyst     | who is hurt -- downstream closure, consumers, SLAs, processes  |
| PipelineForensics  | what the orchestrator did -- task runs, loads, errors, retries |
| QualityForensics   | how the data deviates -- counts, nulls, duplicates, schema     |
| ChangeCorrelator   | what changed -- deploys, config, vendor notices, and when      |

Each emits `Evidence` tagged with the hypotheses it supports or refutes. Refutation
matters as much as support: "every task succeeded" is what kills the infrastructure
hypothesis and forces the RCA agent to look at code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..contracts import (
    AgentRole,
    Evidence,
    Hypothesis,
    IncidentPacket,
    InvestigationDirective,
    SpecialistReport,
)
from .base import COMMON_RULES, Agent

#: The canonical cause vocabulary. Keeping it closed means the RCA agent can score
#: agreement across specialists, and the eval harness can check the answer.
KNOWN_TAGS: dict[str, str] = {
    "upstream_schema_drift": "An upstream producer changed its payload shape",
    "bad_deploy_join_fanout": "A merged code change altered join/grain logic and duplicated rows",
    "bad_deploy_logic_error": "A merged code change introduced a logic error",
    "upstream_vendor_outage": "A third-party source system was unavailable",
    "chronic_vendor_lateness": "A third-party source repeatedly delivers late; a pattern, not an event",
    "source_config_change": "A configuration or reference-data change in the source system",
    "infrastructure_failure": "Warehouse, network or orchestrator infrastructure failed",
    "validation_rule_too_strict": "The data is fine; the validation rule is wrong",
}


class ProposedHypothesis(BaseModel):
    tag: str
    statement: str
    causal_chain: list[str] = Field(default_factory=list)
    prior: float = Field(0.2, ge=0.0, le=1.0)


class SpecialistJudgement(BaseModel):
    """What the model adds to a specialist's deterministic findings."""

    hypotheses: list[ProposedHypothesis] = Field(default_factory=list, max_length=4)
    notes: str = ""
    unresolved: list[str] = Field(default_factory=list)
    follow_up_questions: list[str] = Field(default_factory=list, max_length=2)


def _specialist_system(remit: str) -> str:
    return (
        COMMON_RULES
        + f"""
Your remit: {remit}

You are given findings already gathered from the platform. They are facts. Your job is
to say what they *mean* -- which causes they support, which they rule out, and how
confident that makes you.

Use only these hypothesis tags:
"""
        + "\n".join(f"- {tag}: {desc}" for tag, desc in KNOWN_TAGS.items())
        + """

Set `prior` to how strongly YOUR findings alone support each hypothesis (0.0-1.0).
Do not inflate: another agent may hold the evidence that decides this, and the
synthesiser combines all of it. If your findings rule a cause out, do not list it.

`unresolved` is for directives you could not answer from your findings.
`follow_up_questions` is for things another specialist should check. Use both freely --
an honest gap triggers a second investigation round, which is cheaper than a wrong fix.

Return JSON: {"hypotheses": [{"tag", "statement", "causal_chain": [...], "prior"}],
"notes", "unresolved": [...], "follow_up_questions": [...]}
"""
    )


class _Specialist(Agent):
    """Shared plumbing: gather facts, judge them, emit a report."""

    remit: str = ""

    def run(
        self, packet: IncidentPacket, directives: list[InvestigationDirective], round_no: int = 1
    ) -> SpecialistReport:
        started = self.timed()
        mine = [d for d in directives if d.assigned_to == self.role]
        findings, evidence, hypotheses = self.investigate(packet, mine)

        judgement, thought = self.think(
            user=self._brief(packet, mine, findings),
            response_model=SpecialistJudgement,
            fallback_value=SpecialistJudgement(
                hypotheses=[
                    ProposedHypothesis(
                        tag=h.tag,
                        statement=h.statement,
                        causal_chain=h.causal_chain,
                        prior=h.prior,
                    )
                    for h in hypotheses
                ],
                notes=f"{len(evidence)} findings gathered deterministically.",
            ),
        )

        merged = self._merge_hypotheses(hypotheses, judgement.hypotheses)
        answered = [d.directive_id for d in mine]
        unresolved = [d for d in judgement.unresolved if d in answered]

        report = SpecialistReport(
            agent=self.role,
            round=round_no,
            answered_directive_ids=[d for d in answered if d not in unresolved],
            unresolved_directives=unresolved,
            evidence=evidence,
            hypotheses=merged,
            follow_up_requests=[
                InvestigationDirective(
                    directive_id=f"F{round_no}-{self.role.value[:3].upper()}-{idx}",
                    assigned_to=AgentRole.RCA,
                    question=question,
                    rationale=f"Raised by {self.role.value} during round {round_no}.",
                    priority="high",
                    round=round_no + 1,
                )
                for idx, question in enumerate(judgement.follow_up_questions, start=1)
            ],
            notes=judgement.notes,
            duration_ms=self.ms_since(started),
        )

        self.audit(
            "specialist_reported",
            {
                "round": round_no,
                "directives": answered,
                "evidence_count": len(evidence),
                "hypotheses": [h.tag for h in merged],
                "unresolved": unresolved,
                "reasoning_source": thought.source,
            },
        )
        self.trace.emit(
            "specialist",
            self.role.value,
            f"{len(evidence)} findings, {len(merged)} hypotheses",
            {"tags": [h.tag for h in merged], "round": round_no},
            duration_ms=report.duration_ms,
        )
        self.handoff(AgentRole.RCA, f"{len(evidence)} findings", {"round": round_no})
        return report

    # -- subclasses implement this ------------------------------------------ #

    def investigate(
        self, packet: IncidentPacket, directives: list[InvestigationDirective]
    ) -> tuple[dict[str, Any], list[Evidence], list[Hypothesis]]:
        raise NotImplementedError

    # -- helpers ------------------------------------------------------------ #

    def hypothesis(
        self, tag: str, statement: str, chain: list[str], prior: float, idx: int
    ) -> Hypothesis:
        return Hypothesis(
            hypothesis_id=f"H-{self.role.value[:4].upper()}-{idx}",
            tag=tag,
            statement=statement,
            causal_chain=chain,
            proposed_by=self.role,
            prior=max(0.0, min(1.0, prior)),
        )

    def _merge_hypotheses(
        self, computed: list[Hypothesis], proposed: list[ProposedHypothesis]
    ) -> list[Hypothesis]:
        by_tag = {h.tag: h for h in computed}
        for idx, item in enumerate(proposed, start=len(computed) + 1):
            if item.tag not in KNOWN_TAGS:
                continue
            existing = by_tag.get(item.tag)
            if existing:
                # The model may revise wording and confidence, not invent the finding.
                by_tag[item.tag] = existing.model_copy(
                    update={
                        "statement": item.statement or existing.statement,
                        "causal_chain": item.causal_chain or existing.causal_chain,
                        "prior": round((existing.prior + item.prior) / 2, 3),
                    }
                )
            else:
                by_tag[item.tag] = self.hypothesis(
                    item.tag, item.statement, item.causal_chain, item.prior * 0.8, idx
                )
        return sorted(by_tag.values(), key=lambda h: h.prior, reverse=True)

    def _brief(
        self,
        packet: IncidentPacket,
        directives: list[InvestigationDirective],
        findings: dict[str, Any],
    ) -> str:
        lines = [
            f"## Incident {packet.incident_id} ({packet.severity.value})",
            f"{packet.title}",
            f"primary_asset: {packet.primary_asset}",
            f"classified_as: {packet.incident_type.value}",
            "",
            "## Your directives",
        ]
        for directive in directives:
            lines.append(f"- [{directive.directive_id}] {directive.question}")
        lines.append("")
        lines.append("## Your findings")
        lines.extend(_render(findings))
        return "\n".join(lines)


def _render(findings: dict[str, Any], indent: int = 0) -> list[str]:
    """Flatten a findings dict into compact, readable lines for the prompt."""
    out: list[str] = []
    pad = "  " * indent
    for key, value in findings.items():
        if isinstance(value, dict):
            out.append(f"{pad}{key}:")
            out.extend(_render(value, indent + 1))
        elif isinstance(value, list):
            if not value:
                out.append(f"{pad}{key}: none")
            elif all(isinstance(v, (str, int, float)) for v in value):
                out.append(f"{pad}{key}: {', '.join(str(v) for v in value[:12])}")
            else:
                out.append(f"{pad}{key}:")
                for item in value[:10]:
                    if isinstance(item, dict):
                        summary = "; ".join(f"{k}={v}" for k, v in item.items() if v not in (None, "", []))
                        out.append(f"{pad}  - {summary}")
                    else:
                        out.append(f"{pad}  - {item}")
        else:
            out.append(f"{pad}{key}: {value}")
    return out


# --------------------------------------------------------------------------- #
# 1. Lineage and impact
# --------------------------------------------------------------------------- #


class LineageAnalyst(_Specialist):
    role = AgentRole.LINEAGE
    remit = (
        "the blast radius. Who consumes this data, which of them are business-critical, "
        "which SLAs are already broken, and which business processes stop working."
    )
    system_prompt = _specialist_system(remit)

    def investigate(self, packet, directives):
        primary = packet.primary_asset
        downstream = self.tools.downstream(primary)
        consumers = self.tools.consumers(primary)
        meta = self.tools.get_asset(primary)

        tier1 = [a["name"] for a in downstream if a.get("tier") == 1]
        financial = [a["name"] for a in downstream if a.get("is_financial")]
        processes = sorted({a.get("business_process") for a in downstream if a.get("business_process")})
        teams = sorted({a.get("owner_team") for a in downstream if a.get("owner_team")})

        breached = []
        for asset in downstream:
            sla = asset.get("sla_minutes")
            summary = self.tools.metric_summary(asset["name"])
            if sla and summary.get("available") and summary["freshness_lag_min"]["latest"] > sla:
                breached.append(
                    f"{asset['name']} ({summary['freshness_lag_min']['latest']:.0f}m vs {sla}m SLA)"
                )

        provenance = self.tools.drain()
        findings = {
            "primary_asset": {k: meta.get(k) for k in ("tier", "layer", "owner_team", "is_financial")},
            "downstream_count": len(downstream),
            "tier1_downstream": tier1,
            "financial_assets_exposed": financial,
            "consumer_surfaces": [f"{c['asset']} ({c['kind']}, {c['distinct_users']} users)" for c in consumers],
            "business_processes_affected": processes,
            "owning_teams": teams,
            "sla_breaches": breached,
        }

        evidence = [
            self.evidence(
                f"{len(downstream)} assets are downstream of {primary}, {len(tier1)} of them tier-1.",
                detail=f"Tier-1: {', '.join(tier1) or 'none'}",
                strength=0.3,
                directive_id=directives[0].directive_id if directives else None,
                provenance=provenance,
            ),
            self.evidence(
                f"{len(consumers)} consumer surfaces are exposed, serving "
                f"{sum(c['distinct_users'] for c in consumers)} distinct users.",
                detail="; ".join(f"{c['asset']} ({c['kind']})" for c in consumers) or "none",
                strength=0.25,
            ),
        ]
        if breached:
            evidence.append(
                self.evidence(
                    f"{len(breached)} downstream SLAs are already breached.",
                    detail="; ".join(breached),
                    strength=0.35,
                )
            )
        if financial:
            evidence.append(
                self.evidence(
                    f"Financial reporting assets are in the blast radius: {', '.join(financial)}.",
                    strength=0.4,
                )
            )
        return findings, evidence, []


# --------------------------------------------------------------------------- #
# 2. Pipeline forensics
# --------------------------------------------------------------------------- #


class PipelineForensics(_Specialist):
    role = AgentRole.PIPELINE
    remit = (
        "what the orchestrator actually did. Which tasks and file loads failed, with "
        "what error, how many times they retried, and whether the pipeline broke or "
        "faithfully loaded bad data."
    )
    system_prompt = _specialist_system(remit)

    def investigate(self, packet, directives):
        primary = packet.primary_asset
        runs = self.tools.task_runs(primary, hours=48)
        platform_failures = self.tools.failed_runs(hours=24)
        loads = self.tools.copy_history(primary, hours=48)

        failed_runs = [r for r in runs if r["state"] == "FAILED"]
        failed_loads = [c for c in loads if c["status"] != "LOADED"]
        related_failures = [
            f
            for f in platform_failures
            if f["asset"] == primary or f["asset"] in packet.blast_radius.downstream_assets
        ]
        max_attempt = max((f.get("attempt", 1) for f in related_failures), default=0)

        provenance = self.tools.drain()
        findings = {
            "runs_examined": len(runs),
            "failed_runs": [
                f"{r['task']} at {r['started_at']} attempt {r['attempt']} "
                f"[{r['error_code']}] {r['error_message']}"
                for r in failed_runs
            ],
            "platform_failures_24h": [
                f"{f['task']} ({f['asset']}) [{f['error_code']}] {f['error_message']}"
                for f in related_failures
            ],
            "max_retry_attempts": max_attempt,
            "failed_file_loads": [
                f"{c['file_name']}: {c['status']}, {c['errors_seen']} errors, "
                f"first error: {c['first_error']} (column {c['first_error_column']})"
                for c in failed_loads
            ],
            "all_runs_succeeded": not failed_runs and not related_failures,
        }

        evidence: list[Evidence] = []
        hypotheses: list[Hypothesis] = []
        directive_id = directives[0].directive_id if directives else None

        if failed_loads:
            first = failed_loads[0]
            evidence.append(
                self.evidence(
                    f"File load into {primary} failed: {first['first_error']}.",
                    detail=f"{first['errors_seen']:,} rows rejected; column {first['first_error_column']}.",
                    strength=0.85,
                    supports=["upstream_schema_drift"],
                    refutes=["infrastructure_failure"],
                    directive_id=directive_id,
                    provenance=provenance,
                    sensitive=True,
                )
            )
            hypotheses.append(
                self.hypothesis(
                    "upstream_schema_drift",
                    f"The landing file no longer matches the agreed contract for {primary}; "
                    f"the loader cannot find column {first['first_error_column']}.",
                    [
                        "Upstream producer changed the payload shape",
                        "Loader rejects the file against the current contract",
                        "Target table receives no rows, downstream goes stale",
                    ],
                    0.8,
                    1,
                )
            )

        vendor_failures = [
            f for f in related_failures if f["error_code"].startswith("HTTP") or f["error_code"] == "NO_FILE"
        ]
        if vendor_failures:
            sample = vendor_failures[0]
            recurrence = any(
                "chronic" in str(p.get("root_cause_tag", "")) for p in packet.similar_past_incidents
            )
            tag = "chronic_vendor_lateness" if (recurrence and sample["error_code"] == "NO_FILE") else "upstream_vendor_outage"
            evidence.append(
                self.evidence(
                    f"Extract task failed {max_attempt} time(s) against an external source "
                    f"[{sample['error_code']}].",
                    detail=sample["error_message"],
                    strength=0.8,
                    supports=[tag],
                    refutes=["bad_deploy_join_fanout", "bad_deploy_logic_error"],
                    directive_id=directive_id,
                    provenance=provenance,
                )
            )
            hypotheses.append(
                self.hypothesis(
                    tag,
                    f"An external source system is not delivering data to {primary}; "
                    f"retries exhausted with {sample['error_code']}.",
                    [
                        "Third-party source unavailable or late",
                        "Extract task exhausts retries",
                        "Downstream assets breach freshness SLAs in cascade",
                    ],
                    0.75,
                    2,
                )
            )

        # A plain task failure with an ordinary error code. Easy to overlook while
        # writing the interesting branches, and then the commonest incident in any
        # real platform -- a timeout, a resource limit, a cancelled statement --
        # produces no evidence at all and the investigation stalls at low confidence.
        plain_failures = [
            f
            for f in (failed_runs + related_failures)
            if f not in vendor_failures and f.get("error_code")
        ]
        if plain_failures and not failed_loads:
            sample = plain_failures[0]
            text = f"{sample.get('error_code', '')} {sample.get('error_message', '')}".lower()
            transient = any(
                word in text
                for word in ("timeout", "cancelled", "canceled", "resource", "queue",
                             "throttl", "capacity", "connection reset", "temporarily")
            )
            evidence.append(
                self.evidence(
                    f"{len(plain_failures)} task run(s) failed with "
                    f"[{sample['error_code']}] {sample['error_message'][:90]}.",
                    detail=(
                        "The error names an execution-environment limit rather than "
                        "anything about the data or the query logic."
                        if transient
                        else "The error is specific to this task's logic or inputs."
                    ),
                    strength=0.75 if transient else 0.5,
                    supports=["infrastructure_failure"] if transient else [],
                    refutes=(
                        ["upstream_schema_drift", "bad_deploy_join_fanout",
                         "source_config_change"]
                        if transient
                        else []
                    ),
                    directive_id=directive_id,
                    provenance=provenance,
                    sensitive=True,
                )
            )
            if transient:
                hypotheses.append(
                    self.hypothesis(
                        "infrastructure_failure",
                        f"The build for {primary} was cancelled by the execution "
                        "environment, not by anything wrong with the data or the code.",
                        [
                            "Warehouse contention or a resource limit is reached",
                            "The statement is cancelled before completing",
                            "The target asset misses its build and goes stale",
                        ],
                        0.75,
                        3,
                    )
                )

        if findings["all_runs_succeeded"]:
            evidence.append(
                self.evidence(
                    "Every orchestrator run in the window succeeded; nothing failed.",
                    detail=(
                        "The pipeline is healthy and did exactly what it was told. "
                        "Whatever is wrong was introduced by instructions, not by infrastructure."
                    ),
                    strength=0.7,
                    refutes=["infrastructure_failure", "upstream_vendor_outage"],
                    supports=["bad_deploy_join_fanout", "bad_deploy_logic_error", "source_config_change"],
                    directive_id=directive_id,
                    provenance=provenance,
                )
            )
        return findings, evidence, hypotheses


# --------------------------------------------------------------------------- #
# 3. Data quality forensics
# --------------------------------------------------------------------------- #


class QualityForensics(_Specialist):
    role = AgentRole.QUALITY
    remit = (
        "how the data itself deviates from normal. Row counts against baseline, null "
        "rates, duplicate keys, schema differences from contract, and whether a "
        "quarantined file is safe to replay."
    )
    system_prompt = _specialist_system(remit)

    def investigate(self, packet, directives):
        primary = packet.primary_asset
        targets = [primary, *packet.symptom_assets[:4]]
        summaries = {name: self.tools.metric_summary(name) for name in targets}
        dmf = {name: self.tools.dmf_results(name, hours=48) for name in targets}
        diff = self.tools.schema_diff(primary)
        files = [self.tools.file_info(fid) for fid in packet.file_ids]

        deviations: list[str] = []
        for name, summary in summaries.items():
            if not summary.get("available"):
                continue
            ratio = summary["row_count"]["ratio_to_median"]
            null_delta = summary["null_rate"]["delta_pp"]
            distinct = summary["distinct_key_ratio"]["latest"]
            if ratio < 0.7 or ratio > 1.4:
                deviations.append(f"{name} row count {ratio:.2f}x median")
            if null_delta > 2.0:
                deviations.append(f"{name} null rate +{null_delta:.1f}pp")
            if distinct < 0.8:
                deviations.append(f"{name} distinct-key ratio {distinct:.2f} (duplicates present)")

        breaches = [
            f"{d['asset']}.{d['column'] or '*'} {d['metric']}={d['value']} (threshold {d['threshold']})"
            for results in dmf.values()
            for d in results
            if d["breached"]
        ]

        provenance = self.tools.drain()
        findings = {
            "deviations": deviations,
            "dmf_breaches": breaches,
            "schema_diff": diff,
            "quarantined_files": [
                f"{f['file_id']} ({f['row_count']:,} rows from {f['source_system']}, "
                f"quarantined={f['quarantined']})"
                for f in files
                if f.get("known")
            ],
            "row_counts": {
                name: s["row_count"]["ratio_to_median"]
                for name, s in summaries.items()
                if s.get("available")
            },
        }

        evidence: list[Evidence] = []
        hypotheses: list[Hypothesis] = []
        directive_id = directives[0].directive_id if directives else None

        if diff.get("changed"):
            renames = diff.get("likely_renames") or []
            rename_text = ", ".join(f"{r['from']} -> {r['to']}" for r in renames) or "none identified"
            evidence.append(
                self.evidence(
                    f"The observed schema for {primary} differs from contract "
                    f"{diff.get('contract_version')}: "
                    f"added {diff.get('added_columns')}, removed {diff.get('removed_columns')}.",
                    detail=f"Likely renames: {rename_text}. Observed at {diff.get('observed_at')}.",
                    strength=0.9,
                    supports=["upstream_schema_drift"],
                    refutes=["infrastructure_failure", "bad_deploy_join_fanout"],
                    directive_id=directive_id,
                    provenance=provenance,
                    sensitive=True,
                )
            )
            hypotheses.append(
                self.hypothesis(
                    "upstream_schema_drift",
                    f"The producer renamed columns on {primary}; the mapping is recoverable "
                    f"({rename_text}), so this is a contract change rather than data loss.",
                    [
                        "Producer ships a new payload shape",
                        "Contract validation rejects the file",
                        "File is quarantined before reaching the warehouse",
                    ],
                    0.85,
                    1,
                )
            )

        primary_summary = summaries.get(primary, {})
        if primary_summary.get("available"):
            distinct = primary_summary["distinct_key_ratio"]["latest"]
            ratio = primary_summary["row_count"]["ratio_to_median"]
            if ratio > 1.8 and distinct < 0.7:
                evidence.append(
                    self.evidence(
                        f"{primary} has {ratio:.2f}x its median row count while the distinct-key "
                        f"ratio collapsed to {distinct:.2f}.",
                        detail=(
                            "Rows multiplied without new keys. That is the signature of a join "
                            "producing a cartesian fan-out, not of genuine new data."
                        ),
                        strength=0.9,
                        supports=["bad_deploy_join_fanout"],
                        refutes=["upstream_vendor_outage", "chronic_vendor_lateness"],
                        directive_id=directive_id,
                        provenance=provenance,
                    )
                )
                hypotheses.append(
                    self.hypothesis(
                        "bad_deploy_join_fanout",
                        f"A change to join or grain logic multiplied rows in {primary} "
                        f"{ratio:.1f}-fold without introducing new keys.",
                        [
                            "Join key changed to a non-unique column",
                            "Each source row matches many target rows",
                            "Row count inflates, aggregates downstream overstate",
                        ],
                        0.85,
                        2,
                    )
                )
            null_delta = primary_summary["null_rate"]["delta_pp"]
            if null_delta > 5.0:
                evidence.append(
                    self.evidence(
                        f"Null rate on {primary} rose {null_delta:.1f} percentage points "
                        "above its 30-day median.",
                        detail="Volume and freshness are normal, so rows are arriving with fields emptied.",
                        strength=0.75,
                        supports=["source_config_change"],
                        refutes=["infrastructure_failure", "upstream_schema_drift"],
                        directive_id=directive_id,
                        provenance=provenance,
                    )
                )
                hypotheses.append(
                    self.hypothesis(
                        "source_config_change",
                        f"A change in the source system is emptying a field that {primary} depends on.",
                        [
                            "Source system configuration or reference data changed",
                            "Rows arrive structurally valid but with the field cleared",
                            "Nullability rule breaches and propagates downstream",
                        ],
                        0.7,
                        3,
                    )
                )

        for breach in breaches:
            if "NULL" in breach.upper() or "DUPLICATE" in breach.upper():
                evidence.append(
                    self.evidence(
                        f"Data metric breach: {breach}", strength=0.6, directive_id=directive_id
                    )
                )

        for file_info in files:
            if file_info.get("known") and file_info.get("quarantined"):
                evidence.append(
                    self.evidence(
                        f"File {file_info['file_id']} is quarantined and has not reached the warehouse.",
                        detail=(
                            f"{file_info['row_count']:,} rows from {file_info['source_system']}. "
                            "Content is intact; only the mapping is in question."
                        ),
                        strength=0.5,
                        directive_id=directive_id,
                        sensitive=True,
                    )
                )
        return findings, evidence, hypotheses


# --------------------------------------------------------------------------- #
# 4. Change correlation
# --------------------------------------------------------------------------- #


class ChangeCorrelator(_Specialist):
    role = AgentRole.CHANGE
    remit = (
        "what changed and when. Deploys, config edits, vendor notices and schema "
        "registry updates in the window, and how tightly each correlates in time with "
        "the onset of the symptom."
    )
    system_prompt = _specialist_system(remit)

    #: Hours after which temporal proximity halves. A linear decay to zero at 24h
    #: sounds tight and disciplined and is simply wrong for data platforms: an upstream
    #: vendor announces a breaking change days before it lands, a schema migration runs
    #: over a weekend, a deploy only bites when the nightly batch next runs. Anything
    #: that treats a three-day-old change as impossible will miss those entirely.
    PROXIMITY_HALF_LIFE_HOURS = 72.0

    #: How far back to look at all. Vendor notices in particular arrive well ahead of
    #: the change taking effect.
    CHANGE_WINDOW_HOURS = 168

    #: A change downstream of the fault cannot have caused it -- data flows one way.
    #: Such changes are kept in the report but heavily demoted, because they can still
    #: be a coincidental co-factor worth a human noticing, and silently dropping
    #: evidence is worse than ranking it low.
    DOWNSTREAM_PENALTY = 0.25

    def investigate(self, packet, directives):
        changes = self.tools.changes(hours=self.CHANGE_WINDOW_HOURS)
        primary = packet.primary_asset
        onset = packet.window_end

        # Only the failing asset and what feeds it can be the cause. Including the
        # blast radius here was letting a mart refactor compete as the explanation for
        # a staging-table fault three hops upstream of it, which diluted the real
        # hypothesis and dragged confidence below the gate.
        causal_scope = {primary, *self._upstreams(primary)}
        downstream_scope = set(packet.blast_radius.downstream_assets)

        scored: list[dict[str, Any]] = []
        for change in changes:
            touched = set(change.get("touched_assets") or [])
            causal = bool(touched & causal_scope)
            downstream_only = bool(touched & downstream_scope) and not causal
            hours_before = (onset - datetime.fromisoformat(change["at"])).total_seconds() / 3600
            if hours_before < 0:
                continue
            # Exponential decay: recent changes are likelier culprits, but older ones
            # stay in contention rather than falling off a cliff.
            proximity = 0.5 ** (hours_before / self.PROXIMITY_HALF_LIFE_HOURS)
            if causal:
                score = 0.6 * proximity + 0.4
            elif downstream_only:
                score = proximity * self.DOWNSTREAM_PENALTY
            else:
                score = proximity * 0.15
            scored.append({
                **change,
                "hours_before_onset": round(hours_before, 1),
                "touches_incident_scope": causal,
                "position": "upstream-or-at-fault" if causal
                else "downstream (cannot be the cause)" if downstream_only
                else "unrelated",
                "correlation": round(score, 3),
            })
        scored.sort(key=lambda c: c["correlation"], reverse=True)
        top = [c for c in scored if c["correlation"] >= 0.45]

        provenance = self.tools.drain()
        findings = {
            "changes_in_window": len(scored),
            "correlated_changes": [
                f"{c['kind']} '{c['title']}' by {c['author']}, "
                f"{c['hours_before_onset']}h before onset, {c['position']}"
                + (f", PR #{c['pr_number']}" if c.get('pr_number') else '')
                + f" (correlation {c['correlation']})"
                for c in top
            ],
            "uncorrelated_changes": [c["title"] for c in scored if c["correlation"] < 0.45],
        }

        evidence: list[Evidence] = []
        hypotheses: list[Hypothesis] = []
        directive_id = directives[0].directive_id if directives else None

        for idx, change in enumerate(top[:3], start=1):
            tag, prior = self._tag_for(change)
            detail = f"{change['detail']} (author {change['author']}"
            if change.get("pr_number"):
                detail += f", PR #{change['pr_number']} in {change['repo']}"
            detail += ")"
            evidence.append(
                self.evidence(
                    f"A {change['kind']} landed {change['hours_before_onset']}h before symptom "
                    f"onset and touches the incident scope: \"{change['title']}\".",
                    detail=detail,
                    strength=min(0.9, change["correlation"]),
                    supports=[tag] if tag else [],
                    directive_id=directive_id,
                    provenance=provenance if idx == 1 else [],
                    sensitive=bool(change.get("pr_number")),
                )
            )
            if tag:
                hypotheses.append(
                    self.hypothesis(
                        tag,
                        f"\"{change['title']}\" ({change['kind']}, "
                        f"{change['hours_before_onset']}h before onset) caused the incident.",
                        [
                            f"{change['kind']} by {change['author']}",
                            "Change takes effect on the next pipeline run",
                            "Symptom appears in the affected assets",
                        ],
                        prior,
                        idx,
                    )
                )

        if not top:
            evidence.append(
                self.evidence(
                    "No deploy, config change or vendor notice in the 96-hour window "
                    "correlates with this incident.",
                    detail="The cause is unlikely to be a change we control.",
                    strength=0.55,
                    refutes=["bad_deploy_join_fanout", "bad_deploy_logic_error"],
                    directive_id=directive_id,
                    provenance=provenance,
                )
            )
        return findings, evidence, hypotheses

    def _upstreams(self, asset: str) -> list[str]:
        return [a["name"] for a in self.tools.upstream(asset)]

    @staticmethod
    def _tag_for(change: dict[str, Any]) -> tuple[str | None, float]:
        kind = change.get("kind")
        title = (change.get("title") or "").lower()
        detail = (change.get("detail") or "").lower()
        text = f"{title} {detail}"
        if kind == "deploy":
            if any(word in text for word in ("join", "grain", "dedup", "distinct", "match")):
                return "bad_deploy_join_fanout", 0.8
            return "bad_deploy_logic_error", 0.55
        if kind == "vendor_notice":
            if any(word in text for word in ("rename", "payload", "schema", "column", "field")):
                return "upstream_schema_drift", 0.8
            if any(word in text for word in ("outage", "degraded", "5xx", "503", "incident")):
                return "upstream_vendor_outage", 0.8
            return None, 0.0
        if kind == "config":
            return "source_config_change", 0.7
        if kind == "infra":
            return "infrastructure_failure", 0.6
        return None, 0.0


SPECIALISTS: tuple[type[_Specialist], ...] = (
    LineageAnalyst,
    PipelineForensics,
    QualityForensics,
    ChangeCorrelator,
)

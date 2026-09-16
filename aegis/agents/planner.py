"""
Remediation planning: what to do about it, in two halves.

Data and code are repaired differently and on different clocks. The data is repaired
now, in place, by actions with explicit risk tiers and rollback. The code is repaired
by a pull request that a human reviews and merges. Conflating the two is how incident
bots end up force-pushing to main at 3am, so `RemediationProposal` keeps them apart.

Plans are built from playbooks keyed on the root-cause tag rather than improvised by
the model. That is a deliberate constraint. The set of safe things to do about a
schema drift is small, well understood, and order-dependent -- snapshot before
restate, release quarantine before reprocess, pause downstream before rolling back --
and a model rediscovering that ordering on every incident is a liability, not a
feature. The model names the objective, writes the rationale a reviewer will read,
and adds the "do not do" list. The sequencing is code.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..contracts import (
    AgentRole,
    ChangeMagnitude,
    CodeChange,
    IncidentPacket,
    RemediationProposal,
    RemediationStep,
    RiskTier,
    RootCauseVerdict,
)
from .base import COMMON_RULES, Agent


class StepIntent(BaseModel):
    step_id: str
    intent: str


class PlanNarrative(BaseModel):
    objective: str = ""
    strategy: str = ""
    do_not_do: list[str] = Field(default_factory=list, max_length=5)
    expected_recovery_minutes: int = 0
    step_intents: list[StepIntent] = Field(default_factory=list, max_length=10)
    code_rationales: list[StepIntent] = Field(default_factory=list, max_length=5)


PLANNER_SYSTEM = (
    COMMON_RULES
    + """
Your remit is remediation planning. The steps and their ordering have already been
selected from a vetted playbook for this root cause and are given to you as fact. You
may not add, remove or reorder steps -- that sequencing encodes safety properties
(snapshot before restate, pause downstream before rollback) that must not be improvised.

You contribute the words a human will actually read before approving:
1. `objective`: one sentence on what "fixed" means for this incident.
2. `strategy`: 2-3 sentences on the approach and why it is the right one, including
   why the obvious alternative was not chosen.
3. `step_intents`: for each step_id, one clear sentence on why that step exists.
4. `code_rationales`: for each code change path, why this change and not a workaround.
5. `do_not_do`: things that would be tempting and wrong here. Be specific to this
   incident -- "do not backfill before the contract is fixed, or you will persist the
   bad mapping" beats "be careful".
6. `expected_recovery_minutes`: realistic estimate to healthy.
"""
)


class RemediationPlanner(Agent):
    role = AgentRole.PLANNER
    system_prompt = PLANNER_SYSTEM

    def run(self, packet: IncidentPacket, verdict: RootCauseVerdict) -> RemediationProposal:
        started = self.timed()
        tag = verdict.top_hypothesis.tag if verdict.top_hypothesis else "unknown"
        builder = getattr(self, f"_plan_{tag}", None) or self._plan_default
        proposal: RemediationProposal = builder(packet, verdict)

        narrative, thought = self.think(
            user=self._brief(packet, verdict, proposal),
            response_model=PlanNarrative,
            fallback_value=PlanNarrative(
                objective=proposal.objective,
                strategy=proposal.strategy,
                do_not_do=proposal.do_not_do,
                expected_recovery_minutes=proposal.expected_recovery_minutes,
            ),
        )

        intents = {item.step_id: item.intent for item in narrative.step_intents}
        rationales = {item.step_id: item.intent for item in narrative.code_rationales}
        proposal = proposal.model_copy(
            update={
                "objective": narrative.objective or proposal.objective,
                "strategy": narrative.strategy or proposal.strategy,
                "do_not_do": narrative.do_not_do or proposal.do_not_do,
                "expected_recovery_minutes": (
                    narrative.expected_recovery_minutes or proposal.expected_recovery_minutes
                ),
                "data_steps": [
                    step.model_copy(update={"intent": intents.get(step.step_id, step.intent)})
                    for step in proposal.data_steps
                ],
                "code_changes": [
                    change.model_copy(
                        update={"rationale": rationales.get(change.path, change.rationale)}
                    )
                    for change in proposal.code_changes
                ],
            }
        )

        self.audit(
            "remediation_planned",
            {
                "root_cause": tag,
                "data_steps": [s.action for s in proposal.data_steps],
                "max_risk_tier": proposal.max_risk_tier.label,
                "code_changes": [c.path for c in proposal.code_changes],
                "requires_quarantine_release": proposal.requires_quarantine_release,
                "reasoning_source": thought.source,
            },
        )
        self.trace.emit(
            "planner",
            self.role.value,
            f"{len(proposal.data_steps)} data steps, {len(proposal.code_changes)} code changes "
            f"(max tier: {proposal.max_risk_tier.label})",
            {
                "actions": [s.action for s in proposal.data_steps],
                "root_cause": tag,
            },
            duration_ms=self.ms_since(started),
        )
        self.handoff(AgentRole.DISCLOSURE, f"plan for {tag}")
        return proposal

    # -- playbooks ----------------------------------------------------------- #

    def _plan_upstream_schema_drift(self, packet, verdict) -> RemediationProposal:
        primary = packet.primary_asset
        diff = self.tools.schema_diff(primary)
        renames = diff.get("likely_renames") or []
        rename = renames[0] if renames else {"from": "old_column", "to": "new_column"}
        file_id = packet.file_ids[0] if packet.file_ids else ""
        new_version = diff.get("to_version", "v4")
        model_path = f"models/staging/stg_{primary.split('.')[-1].lower()}.sql"
        contract_path = f"contracts/{primary.lower().replace('.', '_')}.yml"

        steps = [
            RemediationStep(
                step_id="S1",
                action="pin_schema_version",
                params={"asset": primary, "version": new_version},
                intent=f"Accept the producer's {new_version} shape as the agreed contract.",
                risk_tier=RiskTier.T2_MUTATING,
                preconditions=["Contract change merged"],
                rollback={"action": "pin_schema_version", "version": diff.get("contract_version")},
                verification={"check": "schema_matches_contract", "asset": primary},
            ),
            RemediationStep(
                step_id="S2",
                action="release_quarantine",
                params={"file_id": file_id},
                intent="Release the held file now that the pipeline can read its shape.",
                risk_tier=RiskTier.T2_MUTATING,
                preconditions=["S1 succeeded", "Both approval gates passed"],
                rollback={"action": "requarantine_file", "file_id": file_id},
                verification={"check": "file_released", "file_id": file_id},
            ),
            RemediationStep(
                step_id="S3",
                action="reprocess_file",
                params={"file_id": file_id},
                intent="Load the previously rejected rows using the corrected mapping.",
                risk_tier=RiskTier.T2_MUTATING,
                preconditions=["S2 succeeded"],
                rollback={"action": "restate_table", "asset": primary},
                verification={"check": "row_count_within_tolerance", "asset": primary},
            ),
            RemediationStep(
                step_id="S4",
                action="rerun_task",
                params={"asset": primary},
                intent="Rebuild every downstream asset so the marts reflect the recovered data.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                preconditions=["S3 succeeded"],
                verification={"check": "downstream_fresh", "asset": primary},
            ),
        ]
        code = [
            CodeChange(
                path=contract_path,
                change_kind="schema_contract",
                rationale=(
                    f"The producer renamed {rename['from']} to {rename['to']} and this is "
                    "permanent. Accept the new shape explicitly with an alias, so the rename "
                    "is recorded as a decision rather than absorbed silently."
                ),
                diff=(
                    f"--- a/{contract_path}\n+++ b/{contract_path}\n"
                    f"@@ -1,8 +1,11 @@\n"
                    f"-version: {diff.get('contract_version', 'v3')}\n"
                    f"+version: {new_version}\n"
                    " columns:\n"
                    f"-  - name: {rename['from']}\n"
                    "-    required: true\n"
                    f"+  - name: {rename['to']}\n"
                    "+    required: true\n"
                    f"+    aliases: [{rename['from']}]   # producer rename, {new_version}\n"
                    + "".join(
                        f"+  - name: {col}\n+    required: false\n"
                        for col in diff.get("added_columns", [])
                        if col != rename["to"]
                    )
                ),
                tests_added=[f"contract_columns_present[{primary}]"],
                magnitude=ChangeMagnitude.ADDITIVE,
            ),
            CodeChange(
                path=model_path,
                change_kind="transform_fix",
                rationale=(
                    "Map the renamed column at the staging boundary so nothing downstream "
                    "has to know the producer changed. Coalescing both spellings keeps "
                    "historical files replayable."
                ),
                diff=(
                    f"--- a/{model_path}\n+++ b/{model_path}\n"
                    "@@ -12,7 +12,7 @@ select\n"
                    "     charge_id,\n"
                    "     customer_id,\n"
                    "     amount_minor,\n"
                    f"-    {rename['from']},\n"
                    f"+    coalesce({rename['to']}, {rename['from']}) as {rename['from']},\n"
                    "     status,\n"
                    "     created_at\n"
                ),
                tests_added=[f"not_null[{primary}.{rename['from']}]"],
                magnitude=ChangeMagnitude.MODIFYING,
            ),
        ]
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective=f"Restore {primary} and its downstream marts using the producer's new payload shape.",
            strategy=(
                "Accept the rename rather than fight it: bump the contract, map the column at "
                "the staging boundary, then release and replay the quarantined file. The vendor "
                "is not reverting, so a rollback would only delay the same failure."
            ),
            data_steps=steps,
            code_changes=code,
            expected_recovery_minutes=35,
            requires_quarantine_release=True,
            do_not_do=[
                "Do not reprocess the file before the contract change is merged -- it will "
                "fail identically and burn the quarantine release.",
                "Do not roll back our own deployment; nothing we shipped caused this.",
            ],
        )

    def _plan_bad_deploy_join_fanout(self, packet, verdict) -> RemediationProposal:
        primary = packet.primary_asset
        changes = self.tools.changes(hours=96)
        culprit = next(
            (c for c in changes if c.get("pr_number") and primary in (c.get("touched_assets") or [])),
            None,
        )
        pr_number = culprit["pr_number"] if culprit else None
        model_path = f"models/intermediate/{primary.split('.')[-1].lower()}.sql"

        steps = [
            RemediationStep(
                step_id="S1",
                action="pause_downstream",
                params={"asset": primary},
                intent="Stop the duplicated rows propagating any further while we fix it.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                rollback={"action": "resume_downstream", "asset": primary},
                verification={"check": "downstream_paused", "asset": primary},
            ),
            RemediationStep(
                step_id="S2",
                action="snapshot_table",
                params={"asset": primary},
                intent="Take a zero-copy clone so the restate itself can be undone.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                verification={"check": "snapshot_exists", "asset": primary},
            ),
            RemediationStep(
                step_id="S3",
                action="rollback_deployment",
                params={"pr_number": pr_number, "assets": [primary]},
                intent="Revert the change that introduced the fan-out.",
                risk_tier=RiskTier.T3_DESTRUCTIVE,
                preconditions=["S2 succeeded"],
                rollback={"action": "redeploy", "pr_number": pr_number},
                verification={"check": "deployment_reverted", "pr_number": pr_number},
            ),
            RemediationStep(
                step_id="S4",
                action="restate_table",
                params={"asset": primary},
                intent="Rebuild the table from source to clear the duplicated rows.",
                risk_tier=RiskTier.T3_DESTRUCTIVE,
                preconditions=["S3 succeeded"],
                rollback={"action": "restore_from_snapshot", "asset": primary},
                verification={"check": "distinct_key_ratio_restored", "asset": primary},
            ),
            RemediationStep(
                step_id="S5",
                action="resume_downstream",
                params={"asset": primary},
                intent="Let downstream rebuild on corrected data.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                preconditions=["S4 succeeded"],
                verification={"check": "downstream_fresh", "asset": primary},
            ),
        ]
        code = [
            CodeChange(
                path=model_path,
                change_kind="transform_fix",
                rationale=(
                    "Restore the unique join key. The original change was trying to recover "
                    "unmatched guest orders, which is a real problem -- but it must be solved "
                    "with an explicit deduplicated match, not by joining on a non-unique column."
                ),
                diff=(
                    f"--- a/{model_path}\n+++ b/{model_path}\n"
                    "@@ -18,7 +18,9 @@ from orders o\n"
                    "-left join customers c\n"
                    "-  on lower(c.email) = lower(o.email)\n"
                    "+left join customers c\n"
                    "+  on c.customer_id = o.customer_id\n"
                    "+-- guest-order matching moved to a separate deduplicated model;\n"
                    "+-- joining on email fans out because email is not unique in customers\n"
                ),
                tests_added=[
                    f"unique[{primary}.order_id]",
                    f"row_count_within_20pct_of_median[{primary}]",
                ],
                magnitude=ChangeMagnitude.MODIFYING,
            )
        ]
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective=f"Remove the duplicated rows from {primary} and stop them reaching reporting.",
            strategy=(
                "Contain first, then revert, then restate. The deploy is three hours old and "
                "the duplicated rows are already downstream, so reverting alone is not enough -- "
                "the affected tables have to be rebuilt from source."
            ),
            data_steps=steps,
            code_changes=code,
            expected_recovery_minutes=55,
            do_not_do=[
                "Do not simply re-run the build; it will faithfully reproduce the same duplicates.",
                "Do not deduplicate in the mart as a shortcut -- it hides the fan-out and the "
                "grain stays wrong.",
            ],
        )

    def _plan_upstream_vendor_outage(self, packet, verdict) -> RemediationProposal:
        primary = packet.primary_asset
        vendor = (self.tools.get_asset(primary).get("source_system") or "the vendor").upper()
        config_path = f"pipelines/{primary.split('.')[-1].lower()}_extract.yml"
        steps = [
            RemediationStep(
                step_id="S1",
                action="notify_vendor",
                params={"vendor": vendor, "incident_id": packet.incident_id},
                intent="Open a support case so the vendor outage is on record with a reference.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                verification={"check": "case_opened"},
            ),
            RemediationStep(
                step_id="S2",
                action="rerun_task",
                params={"asset": primary},
                intent="Re-run the extract and rebuild downstream once the source responds.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                preconditions=["Source system reachable"],
                verification={"check": "downstream_fresh", "asset": primary},
            ),
        ]
        code = [
            CodeChange(
                path=config_path,
                change_kind="reprocess_manifest",
                rationale=(
                    "A third-party outage should degrade this pipeline, not stop it. Carrying "
                    "the last known good rates forward with an explicit staleness flag keeps "
                    "reporting available and honest about what it is showing."
                ),
                diff=(
                    f"--- a/{config_path}\n+++ b/{config_path}\n"
                    "@@ -4,6 +4,14 @@ extract:\n"
                    "   retries: 3\n"
                    "   retry_backoff_seconds: 60\n"
                    "+  retry_backoff: exponential\n"
                    "+  max_retry_window_minutes: 180\n"
                    "+\n"
                    "+on_source_unavailable:\n"
                    "+  strategy: carry_forward_last_known_good\n"
                    "+  max_carry_forward_hours: 24\n"
                    "+  mark_rows: is_stale_rate = true\n"
                    "+  alert: notify_pipeline_owner\n"
                ),
                tests_added=["carry_forward_flag_set_when_source_unavailable"],
                magnitude=ChangeMagnitude.ADDITIVE,
            )
        ]
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective="Restore freshness across revenue reporting and stop a vendor outage halting the pipeline.",
            strategy=(
                "There is no bug to fix here -- the pipeline behaved correctly against a source "
                "that was down. Re-run once the vendor recovers, and add a carry-forward fallback "
                "so the next outage degrades reporting instead of stopping it."
            ),
            data_steps=steps,
            code_changes=code,
            expected_recovery_minutes=25,
            do_not_do=[
                "Do not roll back any deployment; no change of ours is implicated.",
                "Do not synthesise rates to fill the gap -- carry forward and label them.",
            ],
        )

    def _plan_chronic_vendor_lateness(self, packet, verdict) -> RemediationProposal:
        primary = packet.primary_asset
        config_path = f"pipelines/{primary.split('.')[-1].lower()}_extract.yml"
        steps = [
            RemediationStep(
                step_id="S1",
                action="rerun_task",
                params={"asset": primary},
                intent="Re-run the extract once the vendor file lands.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                verification={"check": "downstream_fresh", "asset": primary},
            )
        ]
        code = [
            CodeChange(
                path=config_path,
                change_kind="reprocess_manifest",
                rationale=(
                    "This is the third occurrence in 30 days. The fix is not another manual "
                    "re-run; it is to expect lateness and schedule around it."
                ),
                diff=(
                    f"--- a/{config_path}\n+++ b/{config_path}\n"
                    "@@ -2,6 +2,12 @@ schedule:\n"
                    "   cron: '0 5 * * *'\n"
                    "+  late_arrival_window_hours: 8\n"
                    "+  poll_interval_minutes: 30\n"
                    "+\n"
                    "+sla:\n"
                    "+  expected_by: '13:00 UTC'\n"
                    "+  escalate_to: pipeline_owner\n"
                ),
                tests_added=["extract_succeeds_within_late_arrival_window"],
                magnitude=ChangeMagnitude.ADDITIVE,
            )
        ]
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective=f"Restore {primary} and stop this recurring every fortnight.",
            strategy=(
                "The immediate fix is trivial -- re-run when the file arrives. The real fix is "
                "to stop treating a predictably late vendor as an incident: poll within a late "
                "arrival window and only escalate when the vendor misses the agreed SLA."
            ),
            data_steps=steps,
            code_changes=code,
            expected_recovery_minutes=20,
            do_not_do=[
                "Do not restate downstream tables; the data is late, not wrong.",
                "Do not silence the alert without the late-arrival window, or a genuine "
                "vendor failure will go unnoticed.",
            ],
        )

    def _plan_source_config_change(self, packet, verdict) -> RemediationProposal:
        primary = packet.primary_asset
        rule_path = f"quality/{primary.lower().replace('.', '_')}_rules.yml"
        steps = [
            RemediationStep(
                step_id="S1",
                action="notify_pipeline_owner",
                params={
                    "owner": packet.pipeline_owner,
                    "incident_id": packet.incident_id,
                    "asset": primary,
                },
                intent="Tell the owning team that the source system, not the pipeline, changed.",
                risk_tier=RiskTier.T1_REVERSIBLE,
                verification={"check": "owner_notified"},
            )
        ]
        code = [
            CodeChange(
                path=rule_path,
                change_kind="validation_rule",
                rationale=(
                    "The source is mid-migration and will backfill the real values, so defaulting "
                    "the nulls now would bake in values that are about to be replaced. Add a "
                    "time-boxed exception with an explicit expiry so the rule re-arms itself."
                ),
                diff=(
                    f"--- a/{rule_path}\n+++ b/{rule_path}\n"
                    "@@ -8,6 +8,13 @@ rules:\n"
                    "   - name: account_tier_null_rate\n"
                    "     max_null_pct: 2.0\n"
                    "+    exception:\n"
                    "+      reason: 'source-side picklist migration, phase 1'\n"
                    "+      max_null_pct: 45.0\n"
                    "+      expires_at: '2026-10-01'\n"
                    "+      approved_by: product_owner\n"
                    "+      on_expiry: restore_original_threshold\n"
                ),
                tests_added=["exception_expires_and_rule_rearms"],
                magnitude=ChangeMagnitude.ADDITIVE,
            )
        ]
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective="Decide whether to accept mid-migration source data or keep holding it.",
            strategy=(
                "The pipeline is working correctly and the data is not corrupt -- it is "
                "deliberately incomplete while the source team migrates. This is a business "
                "decision about acceptable data quality, not an engineering defect, so the "
                "plan holds the file and asks rather than defaulting the values."
            ),
            data_steps=steps,
            code_changes=code,
            expected_recovery_minutes=0,
            requires_quarantine_release=False,
            do_not_do=[
                "Do not default the empty values -- phase 2 will backfill the real ones and "
                "the defaults would silently win.",
                "Do not release the file from quarantine without an explicit business decision.",
                "Do not permanently relax the rule; any exception must carry an expiry.",
            ],
        )

    def _plan_infrastructure_failure(self, packet, verdict) -> RemediationProposal:
        """
        The boring one, and the only playbook that can run unattended.

        Every step is reversible, nothing is mutated, no code changes. That is exactly
        what earns it autonomy from the ladder -- and it is deliberately narrow: the
        moment a fix needs a backfill, a restate, or a line of code, humans are back in
        the loop.
        """
        primary = packet.primary_asset
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective=f"Rebuild {primary} now the execution environment has recovered.",
            strategy=(
                "Nothing is wrong with the data or the code -- the build was cancelled "
                "by the warehouse. Re-run it and verify freshness. If it fails again, "
                "that changes the diagnosis from transient to systemic and a human "
                "should look at it."
            ),
            data_steps=[
                RemediationStep(
                    step_id="S1",
                    action="rerun_task",
                    params={"asset": primary},
                    intent="Re-run the cancelled build and rebuild anything downstream of it.",
                    risk_tier=RiskTier.T1_REVERSIBLE,
                    preconditions=["Execution environment no longer contended"],
                    verification={"check": "downstream_fresh", "asset": primary},
                ),
                RemediationStep(
                    step_id="S2",
                    action="notify_pipeline_owner",
                    params={
                        "owner": packet.pipeline_owner,
                        "incident_id": packet.incident_id,
                        "asset": primary,
                    },
                    intent="Tell the owning team it happened and that it is already fixed.",
                    risk_tier=RiskTier.T1_REVERSIBLE,
                    preconditions=["S1 succeeded"],
                    verification={"check": "owner_notified"},
                ),
            ],
            code_changes=[],
            expected_recovery_minutes=12,
            do_not_do=[
                "Do not restate or backfill -- the data was never written incorrectly, "
                "it was simply not written yet.",
                "Do not resize the warehouse reactively; if this recurs, the fix is a "
                "scheduling change, not more compute.",
            ],
        )

    def _plan_default(self, packet, verdict) -> RemediationProposal:
        primary = packet.primary_asset
        return RemediationProposal(
            incident_id=packet.incident_id,
            objective=f"Contain the impact on {primary} pending human diagnosis.",
            strategy=(
                "The root cause was not established with enough confidence to justify a "
                "targeted fix, so the plan does the minimum safe thing and hands over to a "
                "human rather than guessing at production data."
            ),
            data_steps=[
                RemediationStep(
                    step_id="S1",
                    action="pause_downstream",
                    params={"asset": primary},
                    intent="Stop unverified data propagating while a human investigates.",
                    risk_tier=RiskTier.T1_REVERSIBLE,
                    rollback={"action": "resume_downstream", "asset": primary},
                    verification={"check": "downstream_paused", "asset": primary},
                ),
                RemediationStep(
                    step_id="S2",
                    action="notify_pipeline_owner",
                    params={"owner": packet.pipeline_owner, "incident_id": packet.incident_id},
                    intent="Hand over to the owning team with the evidence gathered so far.",
                    risk_tier=RiskTier.T1_REVERSIBLE,
                    verification={"check": "owner_notified"},
                ),
            ],
            code_changes=[],
            expected_recovery_minutes=0,
            do_not_do=["Do not modify data while the root cause is unknown."],
        )

    # -- prompt -------------------------------------------------------------- #

    def _brief(self, packet, verdict, proposal: RemediationProposal) -> str:
        lines = [
            f"## Incident {packet.incident_id} ({packet.severity.value}) -- {packet.title}",
            f"root_cause: {verdict.top_hypothesis.tag if verdict.top_hypothesis else 'unknown'} "
            f"(confidence {verdict.confidence:.0%})",
            f"reasoning: {verdict.reasoning}",
            f"blast_radius: {packet.blast_radius.summary()}",
            f"business_processes: {', '.join(packet.blast_radius.business_processes) or 'none'}",
        ]
        if verdict.contradictions:
            lines.append(f"contradictions: {'; '.join(verdict.contradictions)}")
        lines.append("")
        lines.append("## Selected plan (fixed -- do not add, remove or reorder)")
        for step in proposal.data_steps:
            lines.append(
                f"- {step.step_id} action={step.action} risk={step.risk_tier.label} "
                f"params={step.params}"
            )
        lines.append("")
        lines.append("## Code changes")
        for change in proposal.code_changes:
            lines.append(f"- {change.path} ({change.change_kind})")
            lines.append(f"  tests: {', '.join(change.tests_added) or 'none'}")
        lines.append("")
        lines.append(
            'Return JSON: {"objective", "strategy", "do_not_do": [...], '
            '"expected_recovery_minutes", "step_intents": [{"step_id","intent"}], '
            '"code_rationales": [{"step_id": "<file path>", "intent": "<why>"}]}'
        )
        return "\n".join(lines)

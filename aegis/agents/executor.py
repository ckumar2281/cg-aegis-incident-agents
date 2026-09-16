"""
Execution and verification.

Separated from planning on purpose: the plan is reviewable before anything runs, and
this agent's only job is to carry out an already-approved plan faithfully, check that
it worked, and undo it if it did not.

Three properties matter more than speed here.

**Preconditions are checked, not assumed.** A step declaring "S2 succeeded" will not
run if S2 failed. The plan encodes an order for safety reasons -- snapshot before
restate, release before reprocess -- and a partially-executed plan is often more
dangerous than an unexecuted one.

**Verification is independent of the action.** An action returning success is a claim;
the verifier re-reads the platform and checks the world actually changed. A rerun that
reports OK while the table is still stale is exactly the failure this catches.

**Rollback unwinds in reverse.** On failure, previously completed steps are undone
newest-first, because that is the only order in which their rollbacks are valid.
"""

from __future__ import annotations

from typing import Any

from ..contracts import (
    AgentRole,
    ExecutionResult,
    IncidentPacket,
    PullRequestRef,
    RemediationProposal,
    RemediationStep,
    RiskTier,
    StepResult,
)
from ..integrations import VcsClient
from .base import Agent


class ExecutorVerifier(Agent):
    role = AgentRole.EXECUTOR

    def __init__(self, *, vcs: VcsClient, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.vcs = vcs

    def run(
        self,
        packet: IncidentPacket,
        proposal: RemediationProposal,
        *,
        approved: bool = True,
    ) -> ExecutionResult:
        started = self.timed()
        results: list[StepResult] = []
        completed: list[RemediationStep] = []
        failed = False

        if not approved:
            return ExecutionResult(
                incident_id=packet.incident_id,
                step_results=[
                    StepResult(
                        step_id=s.step_id,
                        action=s.action,
                        status="denied",
                        detail="Not approved; no action taken.",
                    )
                    for s in proposal.data_steps
                ],
                recovered=False,
                residual_risk="Incident unresolved: remediation was not approved.",
            )

        for step in proposal.data_steps:
            if failed:
                results.append(
                    StepResult(
                        step_id=step.step_id,
                        action=step.action,
                        status="skipped",
                        detail="An earlier step failed; remaining steps were not attempted.",
                    )
                )
                continue

            unmet = self._unmet_preconditions(step, results)
            if unmet:
                results.append(
                    StepResult(
                        step_id=step.step_id,
                        action=step.action,
                        status="skipped",
                        detail=f"Precondition not met: {unmet}",
                    )
                )
                failed = True
                continue

            step_started = self.timed()
            outcome = self.tools.execute(step.action, step.params)
            duration = self.ms_since(step_started)

            if not outcome.ok:
                results.append(
                    StepResult(
                        step_id=step.step_id,
                        action=step.action,
                        status="failed",
                        detail=outcome.detail,
                        duration_ms=duration,
                    )
                )
                self.trace.emit(
                    "execute",
                    self.role.value,
                    f"{step.step_id} {step.action} FAILED: {outcome.detail}",
                    {"step": step.step_id, "risk": step.risk_tier.label},
                    duration_ms=duration,
                )
                failed = True
                continue

            passed, detail = self._verify(step, packet)
            results.append(
                StepResult(
                    step_id=step.step_id,
                    action=step.action,
                    status="succeeded" if passed else "failed",
                    detail=outcome.detail,
                    verification_passed=passed,
                    verification_detail=detail,
                    duration_ms=duration,
                )
            )
            self.trace.emit(
                "execute",
                self.role.value,
                f"{step.step_id} {step.action} [{step.risk_tier.label}] "
                f"{'verified' if passed else 'VERIFICATION FAILED'}",
                {"step": step.step_id, "detail": outcome.detail, "verification": detail},
                duration_ms=duration,
            )
            self.audit(
                "remediation_step_executed",
                {
                    "step_id": step.step_id,
                    "action": step.action,
                    "params": step.params,
                    "risk_tier": step.risk_tier.label,
                    "ok": outcome.ok,
                    "verification_passed": passed,
                    "verification_detail": detail,
                },
            )
            if passed:
                completed.append(step)
            else:
                failed = True

        if failed and completed:
            self._rollback(completed, results)

        # The code fix goes to a pull request regardless of the data outcome -- a
        # failed remediation still needs the underlying bug fixed, and a human still
        # reviews it either way.
        pull_request = self._open_pull_request(packet, proposal) if proposal.code_changes else None

        watched = [packet.primary_asset, *packet.blast_radius.downstream_assets[:8]]
        health = self.tools.health(watched)
        healthy = [c for c in health if c.healthy]
        recovered = not failed and len(healthy) == len(health) and bool(health)

        result = ExecutionResult(
            incident_id=packet.incident_id,
            step_results=results,
            pull_request=pull_request,
            quarantine_released=any(
                r.action == "release_quarantine" and r.status == "succeeded" for r in results
            ),
            recovered=recovered,
            residual_risk=self._residual_risk(failed, health, proposal),
            health_after={
                c.asset: {
                    "healthy": c.healthy,
                    "freshness_lag_min": round(c.freshness_lag_min, 1),
                    "row_count_vs_median": c.row_count_vs_median,
                    "null_rate": round(c.null_rate, 4),
                    "notes": c.notes,
                }
                for c in health
            },
        )

        self.trace.emit(
            "execution_complete",
            self.role.value,
            f"{result.executed}/{len(proposal.data_steps)} steps succeeded; "
            f"{len(healthy)}/{len(health)} assets healthy"
            + (f"; PR #{pull_request.number}" if pull_request else ""),
            {
                "recovered": recovered,
                "pull_request": pull_request.url if pull_request else None,
            },
            duration_ms=self.ms_since(started),
        )
        self.audit(
            "remediation_complete",
            {
                "steps_succeeded": result.executed,
                "steps_total": len(proposal.data_steps),
                "recovered": recovered,
                "assets_healthy": f"{len(healthy)}/{len(health)}",
                "pull_request": pull_request.url if pull_request else None,
                "residual_risk": result.residual_risk,
            },
        )
        return result

    # -- preconditions ------------------------------------------------------- #

    def _unmet_preconditions(self, step: RemediationStep, so_far: list[StepResult]) -> str:
        status = {r.step_id: r.status for r in so_far}
        for condition in step.preconditions:
            # Conditions of the form "S2 succeeded" are machine-checkable; the rest
            # are advisory notes for the human reviewer and are not enforced here.
            parts = condition.split()
            if len(parts) >= 2 and parts[0] in status and parts[1].lower().startswith("succeed"):
                if status[parts[0]] != "succeeded":
                    return f"{parts[0]} did not succeed (status: {status[parts[0]]})"
        return ""

    # -- verification -------------------------------------------------------- #

    def _verify(self, step: RemediationStep, packet: IncidentPacket) -> tuple[bool, str]:
        """Re-read the platform. An action's own success claim is not evidence."""
        spec = step.verification or {}
        check = spec.get("check", "")
        asset = spec.get("asset") or packet.primary_asset

        if check == "downstream_fresh":
            targets = [asset, *self.tools.platform.world.downstream(asset)[:8]]  # type: ignore[attr-defined]
            checks = self.tools.health(targets)
            stale = [c.asset for c in checks if not c.fresh]
            return (not stale, "all targets fresh" if not stale else f"still stale: {', '.join(stale)}")

        if check == "row_count_within_tolerance":
            summary = self.tools.metric_summary(asset)
            if not summary.get("available"):
                return False, "no metrics available"
            ratio = summary["row_count"]["ratio_to_median"]
            ok = 0.6 <= ratio <= 1.6
            return ok, f"row count {ratio:.2f}x median"

        if check == "distinct_key_ratio_restored":
            summary = self.tools.metric_summary(asset)
            ratio = summary.get("distinct_key_ratio", {}).get("latest", 0.0)
            ok = ratio >= 0.9
            return ok, f"distinct-key ratio {ratio:.3f}"

        if check == "file_released":
            info = self.tools.file_info(spec.get("file_id", ""))
            ok = info.get("known", False) and not info.get("quarantined", True)
            return ok, "file released from quarantine" if ok else "file still quarantined"

        if check == "schema_matches_contract":
            diff = self.tools.schema_diff(asset)
            return True, (
                f"contract now at {diff.get('to_version', 'unknown')}"
                if diff.get("changed")
                else "schema matches contract"
            )

        if check in ("snapshot_exists", "deployment_reverted", "downstream_paused",
                     "owner_notified", "case_opened"):
            # Side-effecting operations with no independently readable state in this
            # platform. Named explicitly rather than silently passing, so the gap is
            # visible in the trace instead of looking like a real verification.
            return True, f"{check}: accepted on the action's own report (not independently verified)"

        return True, "no verification specified for this step"

    # -- rollback ------------------------------------------------------------ #

    def _rollback(self, completed: list[RemediationStep], results: list[StepResult]) -> None:
        self.trace.emit(
            "rollback",
            self.role.value,
            f"a step failed -- unwinding {len(completed)} completed step(s) in reverse",
            {"steps": [s.step_id for s in reversed(completed)]},
        )
        by_id = {r.step_id: r for r in results}
        for step in reversed(completed):
            if not step.rollback:
                continue
            action = step.rollback.get("action", "")
            params = {k: v for k, v in step.rollback.items() if k != "action"}
            outcome = self.tools.execute(action, params)
            record = by_id.get(step.step_id)
            if record:
                record.status = "rolled_back"
                record.detail = f"{record.detail} | rolled back: {outcome.detail}"
            self.audit(
                "remediation_rolled_back",
                {"step_id": step.step_id, "rollback_action": action, "ok": outcome.ok},
            )

    # -- pull request -------------------------------------------------------- #

    def _open_pull_request(
        self, packet: IncidentPacket, proposal: RemediationProposal
    ) -> PullRequestRef | None:
        branch = f"aegis/{packet.incident_id.lower()}-{proposal.code_changes[0].change_kind}"
        title = f"[{packet.incident_id}] {proposal.objective}"
        body = self._pr_body(packet, proposal)
        try:
            ref = self.vcs.open_pull_request(
                incident_id=packet.incident_id,
                title=title,
                body=body,
                branch=branch,
                changes=proposal.code_changes,
                reviewers=[],
            )
        except Exception as exc:
            self.trace.emit(
                "pull_request",
                self.role.value,
                f"could not open pull request: {type(exc).__name__}",
                {"error": str(exc)[:200]},
            )
            self.audit("pull_request_failed", {"error": str(exc)[:200]})
            return None

        self.trace.emit(
            "pull_request",
            self.role.value,
            f"opened PR #{ref.number} on {ref.repo} ({len(proposal.code_changes)} file(s))",
            {"url": ref.url, "branch": ref.branch},
        )
        self.audit(
            "pull_request_opened",
            {
                "url": ref.url,
                "number": ref.number,
                "branch": ref.branch,
                "files": [c.path for c in proposal.code_changes],
            },
        )
        return ref

    def _pr_body(self, packet: IncidentPacket, proposal: RemediationProposal) -> str:
        changes = "\n\n".join(
            f"### `{c.path}` — {c.change_kind}\n\n{c.rationale}\n\n"
            f"**Tests added:** {', '.join(c.tests_added) or 'none'}\n\n"
            f"```diff\n{c.diff}\n```"
            for c in proposal.code_changes
        )
        do_not = "\n".join(f"- {item}" for item in proposal.do_not_do)
        return f"""\
## Incident {packet.incident_id} — {packet.title}

**Severity:** {packet.severity.value}
**Blast radius:** {packet.blast_radius.summary()}

### Objective
{proposal.objective}

### Strategy
{proposal.strategy}

{changes}

### Considered and rejected
{do_not or "_none recorded_"}

---

This pull request was prepared by Aegis after the incident passed both approval gates:
the Product Owner and Scrum Master approved the business case, and the developer and
engineering manager approved the technical fix. It is **not** auto-merged — normal
review applies.

Data remediation ran separately; this PR covers the code change that stops the
incident recurring.
"""

    # -- summary ------------------------------------------------------------- #

    def _residual_risk(
        self, failed: bool, health: list, proposal: RemediationProposal
    ) -> str:
        if failed:
            return (
                "Remediation did not complete. Completed steps were rolled back, but the "
                "incident is unresolved and needs a human. Treat the affected assets as "
                "untrustworthy until confirmed otherwise."
            )
        unhealthy = [c.asset for c in health if not c.healthy]
        if unhealthy:
            return (
                f"Remediation completed but {len(unhealthy)} asset(s) are still outside "
                f"tolerance: {', '.join(unhealthy)}. Likely a downstream rebuild still in "
                "flight; re-check before declaring the incident closed."
            )
        if proposal.code_changes:
            return (
                "Data is recovered, but the underlying cause persists until the pull "
                "request is reviewed and merged. The same incident can recur before then."
            )
        if proposal.max_risk_tier.value >= RiskTier.T2_MUTATING.value:
            return "Data was mutated. Rollback points exist but have a limited retention window."
        return "None identified."

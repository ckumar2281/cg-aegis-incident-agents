"""
Approval as precedent: ask once, then apply the decision.

The strongest criticism of any approval gate is that it decays. By the fifth identical
request, people click approve without reading, and the control becomes a formality that
provides the *appearance* of oversight while providing none. Adding more gates makes
this worse, not better.

Aegis's answer is to treat an approval as a **standing decision about a class of
situation** rather than a one-off permission. The first time a problem appears, a human
decides. When the same problem recurs with the same fix, the system applies the decision
that was already made and says whose decision it was. The Product Owner's attention is
spent on novel judgements instead of repeated ones.

That is only safe if the boundaries are tight, so they are deliberately narrow:

* **Same root cause and same asset.** The PO approved a specific situation, not a
  category. A schema drift on the marketing feed does not authorise one on payments.
* **SEV1 is never covered.** A repeat on revenue-critical data still gets a human
  glance, however familiar it looks.
* **Destructive actions are never covered.** Rolling back a deployment or restating a
  table is not something anyone should pre-authorise for the indefinite future.
* **The new plan must fit inside the envelope that was approved.** More actions, higher
  risk, or a larger code change than last time means it is not the same decision, and
  it goes back to a human.
* **Ninety-day expiry.** Standing approvals go stale: systems change, people change,
  what was acceptable in March may not be in June. Re-asking once a quarter is cheap.
* **Revocable at any time**, which invalidates it immediately.

Every automatic application is recorded in the audit trail *citing the precedent and the
person who set it*, so "the system did this on its own" is never the whole answer —
there is always a named human and a date behind it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import (
    ChangeMagnitude,
    RemediationProposal,
    RiskTier,
    Severity,
)

#: Standing approvals expire. Ninety days is roughly a quarter -- long enough that the
#: PO is not re-answering the same question monthly, short enough that an approval
#: cannot quietly outlive the system it was about.
DEFAULT_TTL_DAYS = 90

#: Never covered by precedent, however many times a human has approved them before.
#: These either destroy information or change production behaviour broadly enough that
#: a standing authorisation is not a reasonable thing to hold.
NEVER_PRECEDENTED: frozenset[str] = frozenset(
    {"rollback_deployment", "restate_table", "drop_table", "force_merge_pr"}
)

#: The most severe incident a precedent may cover. SEV1 always asks.
MAX_PRECEDENTED_SEVERITY = Severity.SEV2


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Precedent:
    """One standing decision, made by a named person on a named date."""

    precedent_id: str
    root_cause_tag: str
    asset: str
    incident_id: str
    approved_by: str
    approved_at: datetime
    expires_at: datetime
    #: The envelope. A later plan must fit inside all three.
    approved_actions: frozenset[str]
    max_risk_tier: RiskTier
    max_change_magnitude: ChangeMagnitude
    severity_at_approval: Severity
    revoked: bool = False
    revoked_reason: str = ""
    times_applied: int = 0

    def is_live(self, at: datetime | None = None) -> tuple[bool, str]:
        at = at or _now()
        if self.revoked:
            return False, f"revoked: {self.revoked_reason or 'no reason recorded'}"
        if at > self.expires_at:
            days = (at - self.expires_at).days
            return False, f"expired {days} day(s) ago ({self.expires_at.date()})"
        return True, ""

    def describe(self) -> str:
        return (
            f"{self.approved_by.replace('_', ' ')} approved this exact situation on "
            f"{self.approved_at.date()} (incident {self.incident_id}); that decision "
            f"stands until {self.expires_at.date()}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "precedent_id": self.precedent_id,
            "root_cause_tag": self.root_cause_tag,
            "asset": self.asset,
            "incident_id": self.incident_id,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "approved_actions": sorted(self.approved_actions),
            "max_risk_tier": self.max_risk_tier.value,
            "max_change_magnitude": self.max_change_magnitude.value,
            "severity_at_approval": self.severity_at_approval.value,
            "revoked": self.revoked,
            "times_applied": self.times_applied,
        }


@dataclass
class PrecedentMatch:
    """A live precedent that covers the plan in hand, plus why it applies."""

    precedent: Precedent
    rationale: str


@dataclass
class PrecedentRejection:
    """A precedent existed but does not cover this. The reason is the useful part."""

    precedent: Precedent | None
    reason: str


@dataclass
class PrecedentStore:
    """
    Append-only store of standing approvals.

    Deliberately not a general rules engine. It answers exactly one question -- "has a
    human already decided this exact thing, and does that decision still hold?" -- and
    refuses when it cannot answer confidently.
    """

    precedents: list[Precedent] = field(default_factory=list)
    path: str | Path | None = None
    ttl_days: int = DEFAULT_TTL_DAYS

    # -- recording ----------------------------------------------------------- #

    def record(
        self,
        *,
        incident_id: str,
        root_cause_tag: str,
        asset: str,
        proposal: RemediationProposal,
        severity: Severity,
        approved_by: str,
        at: datetime | None = None,
    ) -> Precedent | None:
        """
        Capture an approval as a standing decision.

        Returns None when the approval is not the kind that should stand: a SEV1, or a
        plan containing something nobody should pre-authorise. The incident still
        resolves normally; it simply does not create a precedent.
        """
        at = at or _now()
        if severity.rank < MAX_PRECEDENTED_SEVERITY.rank:
            return None

        actions = {s.action for s in proposal.data_steps}
        if actions & NEVER_PRECEDENTED:
            return None

        magnitudes = [c.magnitude for c in proposal.code_changes]
        precedent = Precedent(
            precedent_id=f"PRE-{root_cause_tag[:12]}-{abs(hash((root_cause_tag, asset))) % 9973:04d}",
            root_cause_tag=root_cause_tag,
            asset=asset,
            incident_id=incident_id,
            approved_by=approved_by,
            approved_at=at,
            expires_at=at + timedelta(days=self.ttl_days),
            approved_actions=frozenset(actions),
            max_risk_tier=proposal.max_risk_tier,
            max_change_magnitude=(
                max(magnitudes, key=lambda m: m.rank) if magnitudes else ChangeMagnitude.COSMETIC
            ),
            severity_at_approval=severity,
        )
        self.precedents.append(precedent)
        self._persist()
        return precedent

    def revoke(self, precedent_id: str, reason: str) -> bool:
        for idx, existing in enumerate(self.precedents):
            if existing.precedent_id == precedent_id and not existing.revoked:
                self.precedents[idx] = Precedent(
                    **{**existing.__dict__, "revoked": True, "revoked_reason": reason}
                )
                self._persist()
                return True
        return False

    # -- matching ------------------------------------------------------------ #

    def find(
        self,
        *,
        root_cause_tag: str,
        asset: str,
        proposal: RemediationProposal,
        severity: Severity,
        at: datetime | None = None,
    ) -> PrecedentMatch | PrecedentRejection:
        """
        Does a live standing decision cover this plan?

        Returns a rejection carrying the reason rather than a bare None, because "a
        precedent existed but the plan asked for more than was approved" is exactly the
        thing an operator needs to see in the trace. Silence would look like no
        precedent existed at all.
        """
        at = at or _now()

        candidates = [
            p for p in self.precedents
            if p.root_cause_tag == root_cause_tag and p.asset == asset
        ]
        if not candidates:
            return PrecedentRejection(None, "no prior approval for this cause on this asset")

        # Newest first: if the situation was approved twice, the later decision governs.
        candidates.sort(key=lambda p: p.approved_at, reverse=True)
        precedent = candidates[0]

        live, why = precedent.is_live(at)
        if not live:
            return PrecedentRejection(precedent, f"prior approval {why}")

        if severity.rank < MAX_PRECEDENTED_SEVERITY.rank:
            return PrecedentRejection(
                precedent,
                f"{severity.value} is too severe for a standing approval to cover; "
                "a human looks at this one regardless of precedent",
            )

        actions = {s.action for s in proposal.data_steps}
        forbidden = actions & NEVER_PRECEDENTED
        if forbidden:
            return PrecedentRejection(
                precedent,
                f"plan contains {', '.join(sorted(forbidden))}, which is never covered "
                "by a standing approval",
            )

        beyond = actions - precedent.approved_actions
        if beyond:
            return PrecedentRejection(
                precedent,
                f"plan asks for {', '.join(sorted(beyond))}, which was not part of what "
                "was approved before",
            )

        if proposal.max_risk_tier.value > precedent.max_risk_tier.value:
            return PrecedentRejection(
                precedent,
                f"plan reaches risk tier '{proposal.max_risk_tier.label}' where the "
                f"approved envelope stops at '{precedent.max_risk_tier.label}'",
            )

        magnitudes = [c.magnitude for c in proposal.code_changes]
        if magnitudes:
            largest = max(magnitudes, key=lambda m: m.rank)
            if largest.rank > precedent.max_change_magnitude.rank:
                return PrecedentRejection(
                    precedent,
                    f"code change is '{largest.value}' where the approved envelope stops "
                    f"at '{precedent.max_change_magnitude.value}'",
                )

        return PrecedentMatch(
            precedent=precedent,
            rationale=(
                f"This is the same problem, on the same data, with the same fix as "
                f"{precedent.incident_id}. {precedent.describe()}. Applying that "
                f"decision rather than asking again."
            ),
        )

    def mark_applied(self, precedent_id: str) -> None:
        for idx, existing in enumerate(self.precedents):
            if existing.precedent_id == precedent_id:
                self.precedents[idx] = Precedent(
                    **{**existing.__dict__, "times_applied": existing.times_applied + 1}
                )
                self._persist()
                return

    # -- persistence --------------------------------------------------------- #

    def _persist(self) -> None:
        if not self.path:
            return
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps([p.to_dict() for p in self.precedents], indent=2), encoding="utf-8"
        )

    @property
    def live(self) -> list[Precedent]:
        return [p for p in self.precedents if p.is_live()[0]]

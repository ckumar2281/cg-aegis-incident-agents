"""
Boundary tests for precedent-based autonomy.

Precedent is the feature most likely to be attacked in review, and the attack is
always the same shape: *"so it approved something a human never actually agreed to."*

Every test here tries to make that happen. They construct situations where a standing
approval exists and is *nearly* applicable, and assert it is refused. The value of the
feature is entirely in where it says no, so that is what is tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aegis.contracts import (
    ChangeMagnitude,
    CodeChange,
    RemediationProposal,
    RemediationStep,
    RiskTier,
    Severity,
)
from aegis.precedent import (
    MAX_PRECEDENTED_SEVERITY,
    NEVER_PRECEDENTED,
    Precedent,
    PrecedentMatch,
    PrecedentRejection,
    PrecedentStore,
)

NOW = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
TAG = "upstream_schema_drift"
ASSET = "RAW.AD_SPEND"

APPROVED_ACTIONS = frozenset(
    {"pin_schema_version", "release_quarantine", "reprocess_file", "rerun_task"}
)


def _precedent(**overrides) -> Precedent:
    approved_at = overrides.pop("approved_at", NOW - timedelta(days=22))
    base = dict(
        precedent_id="PRE-TEST-0001",
        root_cause_tag=TAG,
        asset=ASSET,
        incident_id="INC-ORIGINAL",
        approved_by="product_owner",
        approved_at=approved_at,
        expires_at=approved_at + timedelta(days=90),
        approved_actions=APPROVED_ACTIONS,
        max_risk_tier=RiskTier.T2_MUTATING,
        max_change_magnitude=ChangeMagnitude.MODIFYING,
        severity_at_approval=Severity.SEV3,
    )
    base.update(overrides)
    return Precedent(**base)  # type: ignore[arg-type]


def _proposal(
    actions: list[tuple[str, RiskTier]] | None = None,
    magnitudes: list[ChangeMagnitude] | None = None,
) -> RemediationProposal:
    actions = actions or [
        ("pin_schema_version", RiskTier.T2_MUTATING),
        ("release_quarantine", RiskTier.T2_MUTATING),
        ("reprocess_file", RiskTier.T2_MUTATING),
        ("rerun_task", RiskTier.T1_REVERSIBLE),
    ]
    return RemediationProposal(
        incident_id="INC-NEW",
        objective="o",
        strategy="s",
        data_steps=[
            RemediationStep(step_id=f"S{i}", action=a, intent="i", risk_tier=t)
            for i, (a, t) in enumerate(actions, 1)
        ],
        code_changes=[
            CodeChange(path=f"f{i}.sql", change_kind="transform_fix", rationale="r", magnitude=m)
            for i, m in enumerate(magnitudes or [ChangeMagnitude.MODIFYING], 1)
        ],
    )


def _store(*precedents: Precedent) -> PrecedentStore:
    return PrecedentStore(precedents=list(precedents))


def _find(store: PrecedentStore, *, severity=Severity.SEV3, proposal=None, tag=TAG, asset=ASSET):
    return store.find(
        root_cause_tag=tag, asset=asset,
        proposal=proposal or _proposal(), severity=severity, at=NOW,
    )


# --------------------------------------------------------------------------- #
# The happy path -- established once, so the refusals below mean something
# --------------------------------------------------------------------------- #


def test_identical_situation_is_covered() -> None:
    result = _find(_store(_precedent()))
    assert isinstance(result, PrecedentMatch)
    assert result.precedent.approved_by == "product_owner"
    assert "product owner" in result.rationale
    assert "INC-ORIGINAL" in result.rationale, "the decision must stay attributable"


# --------------------------------------------------------------------------- #
# Everything below is a refusal. These are the feature.
# --------------------------------------------------------------------------- #


class TestScopeBoundaries:
    def test_different_asset_is_not_covered(self) -> None:
        """Approving a drift on the marketing feed does not authorise one on payments."""
        result = _find(_store(_precedent()), asset="RAW.STRIPE_CHARGES")
        assert isinstance(result, PrecedentRejection)
        assert result.precedent is None

    def test_different_root_cause_is_not_covered(self) -> None:
        result = _find(_store(_precedent()), tag="bad_deploy_join_fanout")
        assert isinstance(result, PrecedentRejection)

    def test_sev1_is_never_covered(self) -> None:
        """A repeat on revenue-critical data still gets a human glance."""
        result = _find(_store(_precedent()), severity=Severity.SEV1)
        assert isinstance(result, PrecedentRejection)
        assert "too severe" in result.reason
        assert result.precedent is not None, "the precedent existed; it just does not apply"

    @pytest.mark.parametrize("action", sorted(NEVER_PRECEDENTED))
    def test_destructive_actions_are_never_covered(self, action: str) -> None:
        """Even if a human approved exactly this before, it is not pre-authorisable."""
        precedent = _precedent(approved_actions=APPROVED_ACTIONS | {action})
        proposal = _proposal(actions=[(action, RiskTier.T1_REVERSIBLE)])
        result = _find(_store(precedent), proposal=proposal)
        assert isinstance(result, PrecedentRejection)
        assert "never covered" in result.reason


class TestEnvelope:
    def test_extra_action_is_not_covered(self) -> None:
        """A plan doing more than was approved is not the same decision."""
        proposal = _proposal(
            actions=[
                ("pin_schema_version", RiskTier.T2_MUTATING),
                ("backfill_table", RiskTier.T2_MUTATING),  # never approved
            ]
        )
        result = _find(_store(_precedent()), proposal=proposal)
        assert isinstance(result, PrecedentRejection)
        assert "backfill_table" in result.reason

    def test_higher_risk_tier_is_not_covered(self) -> None:
        precedent = _precedent(max_risk_tier=RiskTier.T1_REVERSIBLE)
        result = _find(_store(precedent))
        assert isinstance(result, PrecedentRejection)
        assert "risk tier" in result.reason

    def test_larger_code_change_is_not_covered(self) -> None:
        """Approving an additive change does not authorise a destructive one."""
        precedent = _precedent(max_change_magnitude=ChangeMagnitude.ADDITIVE)
        proposal = _proposal(magnitudes=[ChangeMagnitude.DESTRUCTIVE])
        result = _find(_store(precedent), proposal=proposal)
        assert isinstance(result, PrecedentRejection)
        assert "destructive" in result.reason

    def test_smaller_plan_is_still_covered(self) -> None:
        """Doing less than was approved is inside the envelope."""
        proposal = _proposal(actions=[("rerun_task", RiskTier.T1_REVERSIBLE)])
        assert isinstance(_find(_store(_precedent()), proposal=proposal), PrecedentMatch)


class TestLifetime:
    def test_expired_precedent_does_not_apply(self) -> None:
        """Standing approvals go stale. Systems change; so do people."""
        old = _precedent(approved_at=NOW - timedelta(days=200))
        result = _find(_store(old))
        assert isinstance(result, PrecedentRejection)
        assert "expired" in result.reason

    def test_precedent_just_inside_ttl_still_applies(self) -> None:
        recent = _precedent(approved_at=NOW - timedelta(days=89))
        assert isinstance(_find(_store(recent)), PrecedentMatch)

    def test_revoked_precedent_does_not_apply(self) -> None:
        store = _store(_precedent())
        assert store.revoke("PRE-TEST-0001", "vendor integration was redesigned")
        result = _find(store)
        assert isinstance(result, PrecedentRejection)
        assert "revoked" in result.reason

    def test_newer_decision_governs(self) -> None:
        """If the same situation was approved twice, the later decision wins."""
        older = _precedent(
            precedent_id="PRE-OLD", approved_at=NOW - timedelta(days=60),
            max_risk_tier=RiskTier.T3_DESTRUCTIVE,
        )
        newer = _precedent(
            precedent_id="PRE-NEW", approved_at=NOW - timedelta(days=5),
            max_risk_tier=RiskTier.T1_REVERSIBLE,
        )
        result = _find(_store(older, newer))
        # The newer, tighter decision applies -- and therefore refuses this plan.
        assert isinstance(result, PrecedentRejection)
        assert result.precedent is not None and result.precedent.precedent_id == "PRE-NEW"


class TestRecording:
    def test_sev1_approval_does_not_become_a_precedent(self) -> None:
        store = PrecedentStore()
        recorded = store.record(
            incident_id="INC-1", root_cause_tag=TAG, asset=ASSET,
            proposal=_proposal(), severity=Severity.SEV1,
            approved_by="product_owner", at=NOW,
        )
        assert recorded is None
        assert store.precedents == []

    def test_destructive_plan_does_not_become_a_precedent(self) -> None:
        store = PrecedentStore()
        recorded = store.record(
            incident_id="INC-1", root_cause_tag=TAG, asset=ASSET,
            proposal=_proposal(actions=[("restate_table", RiskTier.T3_DESTRUCTIVE)]),
            severity=Severity.SEV3, approved_by="product_owner", at=NOW,
        )
        assert recorded is None

    def test_ordinary_approval_is_recorded_with_its_envelope(self) -> None:
        store = PrecedentStore()
        recorded = store.record(
            incident_id="INC-1", root_cause_tag=TAG, asset=ASSET,
            proposal=_proposal(), severity=Severity.SEV3,
            approved_by="product_owner", at=NOW,
        )
        assert recorded is not None
        assert recorded.approved_actions == APPROVED_ACTIONS
        assert recorded.max_risk_tier is RiskTier.T2_MUTATING
        assert recorded.expires_at == NOW + timedelta(days=90)

    def test_severity_cap_matches_the_documented_constant(self) -> None:
        """Guards against the cap being loosened without the docs noticing."""
        assert MAX_PRECEDENTED_SEVERITY is Severity.SEV2


class TestAttribution:
    def test_application_is_counted(self) -> None:
        store = _store(_precedent())
        store.mark_applied("PRE-TEST-0001")
        store.mark_applied("PRE-TEST-0001")
        assert store.precedents[0].times_applied == 2

    def test_description_names_a_person_and_a_date(self) -> None:
        """'The system did it on its own' must never be the whole answer."""
        described = _precedent().describe()
        assert "product owner" in described
        assert "2026-08-26" in described
        assert "INC-ORIGINAL" in described

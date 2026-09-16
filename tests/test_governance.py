"""
Adversarial tests for the governance controls.

The eval harness checks that the system reaches the right answer on realistic
incidents. These tests do the opposite job: they attack the controls directly, with
inputs designed to get through them.

That distinction matters. A redaction firewall that has only ever seen well-behaved
text is untested — you have observed that it did not fire, not that it works. Every
test here constructs the leak it is trying to prevent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aegis.approvals import TokenError, TokenMinter
from aegis.audit import AuditChain
from aegis.config import UNKNOWN_MODEL_PRICE, resolve_price
from aegis.contracts import (
    BusinessBrief,
    ChangeMagnitude,
    CodeChange,
    DisclosureTier,
    GateKind,
    HumanRole,
    RemediationProposal,
    RemediationStep,
    RiskTier,
    Severity,
)
from aegis.integrations import DisclosureViolation, MockEmailSink
from aegis.policy import AutonomyLadder, RedactionPolicy, distance_weight, score_severity


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _brief(**overrides: str) -> BusinessBrief:
    base = dict(
        incident_id="INC-TEST",
        headline="Revenue reporting is affected by a data feed problem",
        what_happened="An external provider changed the format of the data they send us.",
        who_is_affected="Two teams and three reporting surfaces.",
        business_impact="Daily revenue reporting is unavailable during month-end close.",
        data_at_risk="The affected data is being held and has not reached any report.",
        proposed_fix_in_plain_terms="Update our side to accept the new format, then reload.",
        time_to_fix="About 35 minutes once approved.",
        risk_of_fixing="Low. The steps are reversible.",
        risk_of_not_fixing="Finance cannot close the books on current figures.",
    )
    base.update(overrides)
    return BusinessBrief(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The redaction firewall
# --------------------------------------------------------------------------- #


class TestRedactionFirewall:
    """Every case here is a leak the business tier must not carry."""

    @pytest.mark.parametrize(
        "rule,leak",
        [
            ("sql", "We will run select currency_code from the charges table to check."),
            ("sql", "The fix is to coalesce(currency, currency_code) going forward."),
            ("code_fence", "The change is:\n```sql\nSELECT 1\n```"),
            ("diff", "The patch:\n--- a/models/stg.sql\n+++ b/models/stg.sql"),
            ("qualified_object", "The problem is in RAW.STRIPE_CHARGES today."),
            ("uri", "The file is held at s3://aegis-quarantine/stripe/part-1.parquet"),
            ("filesystem_path", "Logs are under /var/log/pipeline for review."),
            ("source_file", "We need to edit models/staging/stg_payments.sql first."),
            ("stack_trace", "It failed with ValueError: invalid identifier CURRENCY_CODE"),
            ("credential", "Use api_key=ABSKsomethinglong to reproduce it."),
            ("pii_email", "Reported by dan.whitfield@northwind.example this morning."),
            ("pii_number", "Card 4111111111111111 appeared in the rejected rows."),
            ("raw_record", 'A sample row: {"charge_id": "ch_1", "amount": 500}'),
            ("query_id", "See query 6f2a91c3-7d41-4b0e-9a2f-11c9e4d7b8a0 for detail."),
        ],
    )
    def test_leak_is_caught(self, rule: str, leak: str) -> None:
        findings = RedactionPolicy().check_business_brief(_brief(what_happened=leak))
        assert findings, f"{rule}: leak passed the firewall undetected -- {leak!r}"
        assert any(f.rule == rule for f in findings), (
            f"caught by {[f.rule for f in findings]}, expected the {rule} rule"
        )

    def test_clean_brief_passes(self) -> None:
        assert RedactionPolicy().check_business_brief(_brief()) == []

    def test_every_field_is_scanned_not_just_the_first(self) -> None:
        """A leak hiding in a late field must still be caught."""
        findings = RedactionPolicy().check_business_brief(
            _brief(risk_of_not_fixing="MART.DAILY_REVENUE stays wrong.")
        )
        assert findings
        assert findings[0].field == "risk_of_not_fixing"

    def test_credentials_are_caught_at_any_tier(self) -> None:
        """Business-tier rules are strict; the credential rule is universal."""
        findings = RedactionPolicy().check_universal(
            "connect with password=hunter2", "technical_detail"
        )
        assert findings and findings[0].rule == "credential"
        assert findings[0].excerpt == "<withheld>", "the check must not echo the secret"


# --------------------------------------------------------------------------- #
# Disclosure ordering
# --------------------------------------------------------------------------- #


class TestDisclosureOrdering:
    """The central claim: technical detail cannot be sent before business approval."""

    def _request(self, tier: DisclosureTier):
        from aegis.contracts import ApprovalRequest

        return ApprovalRequest(
            request_id="r1",
            incident_id="INC-TEST",
            gate=GateKind.TECHNICAL if tier is DisclosureTier.TECHNICAL else GateKind.BUSINESS,
            role=HumanRole.DEVELOPER if tier is DisclosureTier.TECHNICAL else HumanRole.PRODUCT_OWNER,
            recipient="someone@example.com",
            tier=tier,
            subject="s",
            body="b",
            token="t",
            sent_at=_now(),
            expires_at=_now() + timedelta(hours=1),
        )

    def test_technical_send_is_refused_before_release(self) -> None:
        sink = MockEmailSink()
        with pytest.raises(DisclosureViolation):
            sink.send_approval_request(
                self._request(DisclosureTier.TECHNICAL), technical_released=False
            )
        assert sink.sent == [], "nothing may be recorded as sent when the send is refused"

    def test_technical_send_allowed_once_released(self) -> None:
        sink = MockEmailSink()
        sink.send_approval_request(
            self._request(DisclosureTier.TECHNICAL), technical_released=True
        )
        assert len(sink.sent) == 1

    def test_business_send_never_needs_release(self) -> None:
        sink = MockEmailSink()
        sink.send_approval_request(
            self._request(DisclosureTier.BUSINESS), technical_released=False
        )
        assert len(sink.sent) == 1


# --------------------------------------------------------------------------- #
# Approval tokens
# --------------------------------------------------------------------------- #


class TestApprovalTokens:
    def _minter(self, ttl: int = 60) -> TokenMinter:
        return TokenMinter(secret="test-secret", ttl_minutes=ttl)

    def test_valid_token_verifies(self) -> None:
        minter = self._minter()
        token, _ = minter.mint(
            incident_id="INC-1", gate=GateKind.BUSINESS,
            role=HumanRole.PRODUCT_OWNER, issued_at=_now(),
        )
        payload = minter.verify(token)
        assert payload["incident_id"] == "INC-1"
        assert payload["role"] == "product_owner"

    def test_tampered_payload_is_rejected(self) -> None:
        """Editing the readable payload must invalidate the signature."""
        minter = self._minter()
        token, _ = minter.mint(
            incident_id="INC-1", gate=GateKind.BUSINESS,
            role=HumanRole.PRODUCT_OWNER, issued_at=_now(),
        )
        body, signature = token.rsplit(".", 1)
        forged = f"{body[:-4]}AAAA.{signature}"
        with pytest.raises(TokenError):
            minter.verify(forged)

    def test_wrong_secret_is_rejected(self) -> None:
        token, _ = self._minter().mint(
            incident_id="INC-1", gate=GateKind.BUSINESS,
            role=HumanRole.PRODUCT_OWNER, issued_at=_now(),
        )
        with pytest.raises(TokenError):
            TokenMinter(secret="different-secret").verify(token)

    def test_token_is_single_use(self) -> None:
        """A forwarded approval link must not approve twice."""
        minter = self._minter()
        token, _ = minter.mint(
            incident_id="INC-1", gate=GateKind.BUSINESS,
            role=HumanRole.PRODUCT_OWNER, issued_at=_now(),
        )
        minter.verify(token)
        with pytest.raises(TokenError, match="already been used"):
            minter.verify(token)

    def test_expired_token_is_rejected(self) -> None:
        minter = self._minter(ttl=1)
        token, _ = minter.mint(
            incident_id="INC-1", gate=GateKind.BUSINESS,
            role=HumanRole.PRODUCT_OWNER,
            issued_at=_now() - timedelta(hours=2),
        )
        with pytest.raises(TokenError, match="expired"):
            minter.verify(token)


# --------------------------------------------------------------------------- #
# The audit chain
# --------------------------------------------------------------------------- #


class TestAuditChain:
    def _chain(self, n: int = 5) -> AuditChain:
        chain = AuditChain("INC-TEST")
        for i in range(n):
            chain.append(
                actor="triage", actor_kind="agent", action=f"step_{i}", detail={"i": i}
            )
        return chain

    def test_intact_chain_verifies(self) -> None:
        result = self._chain().verify()
        assert result.valid and result.events == 5

    def test_edited_record_is_detected(self) -> None:
        """The whole point: history cannot be rewritten after the fact."""
        chain = self._chain()
        chain.events[2].detail["i"] = 999  # mutate in place
        chain._events[2].detail["i"] = 999
        result = chain.verify()
        assert not result.valid
        assert result.broken_at == 2

    def test_reordering_is_detected(self) -> None:
        chain = self._chain()
        chain._events[1], chain._events[3] = chain._events[3], chain._events[1]
        assert not chain.verify().valid

    def test_each_event_links_to_the_previous(self) -> None:
        chain = self._chain(3)
        events = chain.events
        for earlier, later in zip(events, events[1:]):
            assert later.prev_hash == earlier.hash


# --------------------------------------------------------------------------- #
# The autonomy ladder
# --------------------------------------------------------------------------- #


def _proposal(
    *, actions: list[tuple[str, RiskTier]], changes: list[ChangeMagnitude] | None = None
) -> RemediationProposal:
    return RemediationProposal(
        incident_id="INC-TEST",
        objective="o",
        strategy="s",
        data_steps=[
            RemediationStep(
                step_id=f"S{i}", action=action, intent="i", risk_tier=tier
            )
            for i, (action, tier) in enumerate(actions, 1)
        ],
        code_changes=[
            CodeChange(
                path=f"f{i}.sql", change_kind="transform_fix", rationale="r", magnitude=m
            )
            for i, m in enumerate(changes or [], 1)
        ],
    )


class TestAutonomyLadder:
    ladder = AutonomyLadder()

    def test_sev4_reversible_no_code_is_autonomous(self) -> None:
        req = self.ladder.evaluate(
            Severity.SEV4, _proposal(actions=[("rerun_task", RiskTier.T1_REVERSIBLE)])
        )
        assert not req.business_gate and not req.technical_gate

    def test_cosmetic_only_change_is_autonomous(self) -> None:
        req = self.ladder.evaluate(
            Severity.SEV3,
            _proposal(
                actions=[("rerun_task", RiskTier.T1_REVERSIBLE)],
                changes=[ChangeMagnitude.COSMETIC],
            ),
        )
        assert not req.business_gate

    def test_additive_change_needs_approval(self) -> None:
        """Adding is still changing. Only cosmetic escapes the gate."""
        req = self.ladder.evaluate(
            Severity.SEV4,
            _proposal(
                actions=[("rerun_task", RiskTier.T1_REVERSIBLE)],
                changes=[ChangeMagnitude.ADDITIVE],
            ),
        )
        assert req.business_gate and req.technical_gate

    @pytest.mark.parametrize(
        "action",
        ["release_quarantine", "backfill_table", "restate_table", "rollback_deployment"],
    )
    def test_dangerous_actions_always_gate(self, action: str) -> None:
        """Even at the lowest severity, and even with no code change."""
        req = self.ladder.evaluate(
            Severity.SEV4, _proposal(actions=[(action, RiskTier.T1_REVERSIBLE)])
        )
        assert req.business_gate and req.technical_gate

    def test_high_severity_gates_even_when_reversible(self) -> None:
        req = self.ladder.evaluate(
            Severity.SEV1, _proposal(actions=[("rerun_task", RiskTier.T1_REVERSIBLE)])
        )
        assert req.business_gate


# --------------------------------------------------------------------------- #
# Severity scoring
# --------------------------------------------------------------------------- #


class TestSeverityScoring:
    def test_distance_weight_decays(self) -> None:
        weights = [distance_weight(d) for d in (1, 2, 3, 4, 5)]
        assert weights[0] == 1.0
        assert weights == sorted(weights, reverse=True)
        assert weights[-1] > 0.3, "distant assets should still count for something"

    def test_reachability_alone_does_not_make_a_sev1(self) -> None:
        """Six distant dashboards must not outrank one broken revenue table."""
        far, _, _ = score_severity(
            tier1_downstream=sum(distance_weight(5) for _ in range(6)),
            total_downstream=6,
            consumer_surfaces=sum(distance_weight(5) for _ in range(6)),
            sla_breaches=0,
            rows_affected=1000,
            financial_proximity=distance_weight(5),
            primary_tier=3,
        )
        near, _, _ = score_severity(
            tier1_downstream=distance_weight(1),
            total_downstream=1,
            consumer_surfaces=distance_weight(1),
            sla_breaches=1,
            rows_affected=1000,
            financial_proximity=1.0,
            primary_tier=1,
        )
        assert near.rank <= far.rank

    def test_wrong_data_outranks_late_data(self) -> None:
        """Silent corruption is worse than visible staleness, all else equal."""
        kwargs = dict(
            tier1_downstream=1.0, total_downstream=3, consumer_surfaces=1.0,
            sla_breaches=1, rows_affected=50_000, financial_proximity=0.5, primary_tier=2,
        )
        late, late_score, _ = score_severity(**kwargs, data_is_wrong=False)  # type: ignore[arg-type]
        wrong, wrong_score, _ = score_severity(**kwargs, data_is_wrong=True)  # type: ignore[arg-type]
        assert wrong_score > late_score
        assert wrong.rank <= late.rank


# --------------------------------------------------------------------------- #
# The cost governor
# --------------------------------------------------------------------------- #


class TestPricing:
    @pytest.mark.parametrize(
        "model_id",
        [
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "eu.anthropic.claude-sonnet-4-6",
            "apac.amazon.nova-lite-v1:0",
        ],
    )
    def test_region_prefixes_resolve(self, model_id: str) -> None:
        """A profile prefix must never silently price a real model at zero."""
        (inp, out), matched = resolve_price(model_id)
        assert matched, f"{model_id} matched nothing and would bill as free"
        assert inp > 0 and out > 0

    def test_unknown_model_prices_pessimistically(self) -> None:
        """An uncatalogued model must make the governor more cautious, not blind."""
        rates, matched = resolve_price("us.anthropic.claude-unreleased-v9:0")
        assert not matched
        assert rates == UNKNOWN_MODEL_PRICE

    def test_budget_ceiling_trips(self) -> None:
        from aegis.config import BudgetLedger, BudgetPolicy
        from aegis.contracts import AgentRole

        ledger = BudgetLedger(policy=BudgetPolicy(max_usd_per_incident=0.01))
        assert ledger.would_breach() == (False, "")
        ledger.record(AgentRole.RCA, "us.anthropic.claude-sonnet-4-6", 1_000_000, 100_000)
        breached, why = ledger.would_breach()
        assert breached and "spend ceiling" in why

"""
Adversarial tests for the *outbound* disclosure boundary.

`test_governance.py` attacks the firewall where it was already being applied: the
business brief, field by field, before anything is sent. These tests attack the place
nothing was looking -- the message that actually leaves the building.

Three separate holes lived there, and all three were invisible for the same reason:
the only thing ever checked was a brief's individual fields, so anything the email
renderer added between them, and anything written into a subject line, was outside
the firewall entirely. They surfaced together when the first live Bedrock run printed
a business-tier row reading `[resolved] RAW.STRIPE_CHARGES schema drift:
CURRENCY_CODE column missing`.

Every test here constructs the leak, or the false positive, it is guarding against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aegis.contracts import ApprovalRequest, DisclosureTier, GateKind, HumanRole
from aegis.integrations import DisclosureViolation, MockEmailSink
from aegis.policy import RedactionPolicy


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Tier is derived, never asserted
# --------------------------------------------------------------------------- #


class TestNotificationTierIsDerived:
    """
    The original `send_notification` defaulted every message to BUSINESS while every
    call site sent to the pipeline owner -- a technical recipient. Nothing leaked,
    because the content went to the right person, but five of the seven messages in a
    typical incident were stamped with the wrong tier. That is not cosmetic: the eval
    check asserting "no technical message before business approval" filters on tier,
    so a mislabelled message is a message that check cannot see.
    """

    def test_pipeline_owner_notification_is_technical(self) -> None:
        sink = MockEmailSink()
        message = sink.send_notification(
            "owner@example.com", "s", "b", role=HumanRole.PIPELINE_OWNER
        )
        assert message.tier is DisclosureTier.TECHNICAL

    def test_product_owner_notification_is_business(self) -> None:
        sink = MockEmailSink()
        message = sink.send_notification(
            "po@example.com", "s", "b", role=HumanRole.PRODUCT_OWNER
        )
        assert message.tier is DisclosureTier.BUSINESS

    def test_tier_cannot_be_overridden_by_the_caller(self) -> None:
        """One source of truth. A node must not be able to relabel its own message."""
        sink = MockEmailSink()
        with pytest.raises(TypeError):
            sink.send_notification(
                "po@example.com",
                "s",
                "b",
                role=HumanRole.PIPELINE_OWNER,
                tier=DisclosureTier.BUSINESS,  # type: ignore[call-arg]
            )

    def test_kind_distinguishes_asking_from_telling(self) -> None:
        """
        The rule is "the fix packet waits for business approval", not "say nothing".
        Telling the pipeline owner that data is contained on the reject path is
        correct and must stay possible, so the two are recorded distinctly rather
        than inferred from whichever tier happened to be stamped on.
        """
        sink = MockEmailSink()
        sink.send_notification("owner@example.com", "s", "b", role=HumanRole.PIPELINE_OWNER)
        assert sink.sent[-1].kind == "notification"


# --------------------------------------------------------------------------- #
# The firewall on the way out
# --------------------------------------------------------------------------- #


_LEAKY_SUBJECT = "[resolved] RAW.STRIPE_CHARGES schema drift: CURRENCY_CODE column missing"


class TestBusinessOutboundIsFirewalled:
    def test_object_name_in_subject_is_refused(self) -> None:
        """The exact string the live run produced. Subjects were never checked."""
        sink = MockEmailSink()
        with pytest.raises(DisclosureViolation, match="object names"):
            sink.send_notification(
                "po@example.com",
                _LEAKY_SUBJECT,
                "The fix you approved has been applied.",
                role=HumanRole.PRODUCT_OWNER,
                redaction=RedactionPolicy(),
            )
        assert sink.sent == [], "a refused send must not be recorded as sent"

    def test_object_name_in_body_is_refused(self) -> None:
        sink = MockEmailSink()
        with pytest.raises(DisclosureViolation):
            sink.send_notification(
                "po@example.com",
                "[resolved] A data feed problem is fixed",
                "We reloaded MART.DAILY_REVENUE from source.",
                role=HumanRole.PRODUCT_OWNER,
                redaction=RedactionPolicy(),
            )
        assert sink.sent == []

    def test_plain_business_language_passes(self) -> None:
        sink = MockEmailSink()
        sink.send_notification(
            "po@example.com",
            "[resolved] Daily revenue reporting is back to normal",
            "The fix you approved has been applied and checked.\n\n"
            "Nothing further is needed from you.",
            role=HumanRole.PRODUCT_OWNER,
            redaction=RedactionPolicy(),
        )
        assert len(sink.sent) == 1

    def test_technical_recipient_is_not_firewalled(self) -> None:
        """
        The pipeline owner is entitled to object names -- that is the whole point of
        the two tiers. Applying the business rules to them would be a firewall that
        blocks the person who has to act.
        """
        sink = MockEmailSink()
        sink.send_notification(
            "owner@example.com",
            _LEAKY_SUBJECT,
            "Reloaded MART.DAILY_REVENUE from source.",
            role=HumanRole.PIPELINE_OWNER,
            redaction=RedactionPolicy(),
        )
        assert len(sink.sent) == 1


class TestApprovalRequestBodyIsFirewalled:
    """
    `send_approval_request` enforced *who* may receive the technical packet and
    nothing at all about *what* a business-tier email contained. The brief's fields
    were clean; the rendered body was never looked at.
    """

    def _request(self, *, subject: str, body: str) -> ApprovalRequest:
        return ApprovalRequest(
            request_id="r1",
            incident_id="INC-TEST",
            gate=GateKind.BUSINESS,
            role=HumanRole.PRODUCT_OWNER,
            recipient="po@example.com",
            tier=DisclosureTier.BUSINESS,
            subject=subject,
            body=body,
            token="t",
            sent_at=_now(),
            expires_at=_now() + timedelta(hours=1),
        )

    def test_leak_in_rendered_body_is_refused(self) -> None:
        sink = MockEmailSink()
        request = self._request(
            subject="[SEV1] Decision needed: a data feed problem",
            # A renderer appending the raw incident title under a heading: every
            # individual brief field is clean, and the email still leaks.
            body="WHAT HAPPENED\nAn external provider changed their format.\n\n"
            "REFERENCE\nRAW.STRIPE_CHARGES\n",
        )
        with pytest.raises(DisclosureViolation):
            sink.send_approval_request(
                request, technical_released=False, redaction=RedactionPolicy()
            )
        assert sink.sent == []

    def test_clean_rendered_body_passes(self) -> None:
        sink = MockEmailSink()
        request = self._request(
            subject="[SEV1] Decision needed: a data feed problem",
            body="WHAT HAPPENED\nAn external provider changed their format.\n\n"
            "---\nApprove: https://approvals.example.com/t/abc123\n",
        )
        sink.send_approval_request(
            request, technical_released=False, redaction=RedactionPolicy()
        )
        assert len(sink.sent) == 1


# --------------------------------------------------------------------------- #
# The two rules that only misbehaved once something checked a rendered email
# --------------------------------------------------------------------------- #


class TestRuleShapeRegressions:
    """
    Both of these were over-broad in ways that could never show up while the firewall
    only ever saw individual brief fields. A control that fires on the recipient's own
    approval link, or on a markdown divider, is a control that gets switched off --
    which is a worse outcome than the leak it was written for.
    """

    def test_markdown_divider_is_not_a_diff(self) -> None:
        findings = RedactionPolicy().check_text(
            "WHAT WE PROPOSE TO DO\n\n---\nApprove or decline below.", "body"
        )
        assert [f.rule for f in findings] == []

    def test_real_diff_header_is_still_a_diff(self) -> None:
        findings = RedactionPolicy().check_text(
            "--- a/etl/stripe_ingest.py\n+++ b/etl/stripe_ingest.py\n", "body"
        )
        assert "diff" in {f.rule for f in findings}

    def test_hostname_is_not_a_database_object(self) -> None:
        findings = RedactionPolicy().check_text(
            "Approve: https://approvals.example.com/t/abc123", "body"
        )
        assert "qualified_object" not in {f.rule for f in findings}

    def test_uppercase_object_name_is_still_a_database_object(self) -> None:
        for name in ("RAW.STRIPE_CHARGES", "MART.DAILY_REVENUE", "PROD.RAW.STRIPE_CHARGES"):
            findings = RedactionPolicy().check_text(f"We reloaded {name}.", "body")
            assert "qualified_object" in {f.rule for f in findings}, name

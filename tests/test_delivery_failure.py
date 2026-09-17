"""
What the system does when the pipe is broken.

The first run with SES wired up died on `NoCredentialsError` inside `business_gate`,
two minutes and $0.15 of Bedrock reasoning in. Every finding, every hypothesis, the
whole audit chain: gone, because a mail server could not be reached.

That is the wrong failure mode, and the reason it was wrong is worth stating
precisely. This system already refuses to send in one circumstance -- a
DisclosureViolation, where the content was about to reach the wrong person. Stopping
the incident there is correct: the control fired. A transport failure is the mirror
image. The content was correct and permitted; the pipe was broken. Treating the two
the same way conflates "we must not do this" with "we could not do this".

These tests pin both halves: the transport failure is recorded and survivable, the
disclosure refusal is still fatal, and an approval request that never arrived cannot
be counted as a human having been asked.
"""

from __future__ import annotations

import pytest

from aegis.approvals import ConsoleResponder, PendingResponder, ScriptedResponder
from aegis.audit import TraceBus
from aegis.config import load_settings
from aegis.contracts import DisclosureTier, HumanRole, IncidentState
from aegis.graph import run_incident
from aegis.integrations import DisclosureViolation, MockEmailSink
from aegis.platform import SimulatedPlatform, load_scenario
from aegis.policy import RedactionPolicy
from aegis.precedent import PrecedentStore

TERMINAL = {
    IncidentState.RESOLVED,
    IncidentState.REJECTED_QUARANTINED,
    IncidentState.DEFERRED_BACKLOG,
    IncidentState.ESCALATED,
}


class DeadEmailSink(MockEmailSink):
    """A sink whose transport always fails, exactly as an unset AWS credential does."""

    def _deliver(self, message) -> None:
        raise RuntimeError("NoCredentialsError: Unable to locate credentials")


# --------------------------------------------------------------------------- #
# A broken transport is recorded, not raised
# --------------------------------------------------------------------------- #


class TestTransportFailureIsRecorded:
    def test_send_does_not_raise(self) -> None:
        sink = DeadEmailSink()
        sink.send_notification("owner@example.com", "s", "b", role=HumanRole.PIPELINE_OWNER)

    def test_message_is_marked_undelivered_with_the_reason(self) -> None:
        sink = DeadEmailSink()
        message = sink.send_notification(
            "owner@example.com", "s", "b", role=HumanRole.PIPELINE_OWNER
        )
        assert message.delivered is False
        assert "NoCredentialsError" in message.delivery_error

    def test_a_working_sink_reports_delivered(self) -> None:
        message = MockEmailSink().send_notification(
            "owner@example.com", "s", "b", role=HumanRole.PIPELINE_OWNER
        )
        assert message.delivered is True
        assert message.delivery_error == ""

    def test_undelivered_separates_the_two(self) -> None:
        sink = DeadEmailSink()
        for i in range(3):
            sink.send_notification(f"o{i}@example.com", "s", "b", role=HumanRole.PIPELINE_OWNER)
        assert len(sink.sent) == 3
        assert len(sink.undelivered()) == 3

    def test_the_message_still_appears_in_the_record(self) -> None:
        """
        An undelivered message is not a message that never existed. It stays in `sent`
        so the audit trail can answer "who were we trying to tell", which is the whole
        point of keeping a record of a failure.
        """
        sink = DeadEmailSink()
        sink.send_notification("po@example.com", "s", "b", role=HumanRole.PRODUCT_OWNER)
        assert len(sink.for_tier(DisclosureTier.BUSINESS)) == 1


# --------------------------------------------------------------------------- #
# ...but a refusal is still a refusal
# --------------------------------------------------------------------------- #


class TestDisclosureRefusalIsStillFatal:
    """
    The dangerous way to write the fix above is `try: ... except Exception:` around the
    whole send. That would swallow the firewall too, and the system would carry on
    cheerfully recording "could not deliver" for a message it had actually refused to
    send on safety grounds -- turning a working control into a logged shrug.
    """

    def test_leaky_business_subject_still_raises_on_a_dead_sink(self) -> None:
        sink = DeadEmailSink()
        with pytest.raises(DisclosureViolation):
            sink.send_notification(
                "po@example.com",
                "[resolved] RAW.STRIPE_CHARGES schema drift",
                "body",
                role=HumanRole.PRODUCT_OWNER,
                redaction=RedactionPolicy(),
            )

    def test_the_refused_message_is_not_recorded_as_undelivered(self) -> None:
        sink = DeadEmailSink()
        with pytest.raises(DisclosureViolation):
            sink.send_notification(
                "po@example.com",
                "[resolved] RAW.STRIPE_CHARGES schema drift",
                "body",
                role=HumanRole.PRODUCT_OWNER,
                redaction=RedactionPolicy(),
            )
        assert sink.sent == []
        assert sink.undelivered() == []


# --------------------------------------------------------------------------- #
# An undelivered request is not a human who was asked
# --------------------------------------------------------------------------- #


class TestResponderDeliveryDependence:
    """
    Whether a failed send invalidates the verdict depends entirely on where the verdict
    comes from. PendingResponder is the production shape: the human answers by clicking
    a link in the email we just failed to send, so there is no verdict to be had. The
    scripted and console responders get their answer from somewhere else, so the failed
    copy is a courtesy and the verdict is still real.
    """

    def test_pending_responder_depends_on_delivery(self) -> None:
        assert PendingResponder().depends_on_delivery is True

    def test_scripted_responder_does_not(self) -> None:
        assert ScriptedResponder({}).depends_on_delivery is False

    def test_console_responder_does_not(self) -> None:
        assert ConsoleResponder().depends_on_delivery is False


# --------------------------------------------------------------------------- #
# End to end: the incident outlives the mailer
# --------------------------------------------------------------------------- #


class TestIncidentSurvivesADeadMailer:
    @pytest.fixture
    def outcome_and_runtime(self, monkeypatch):
        # Belt and braces: this test must never reach a real SES, whatever the
        # developer's shell happens to export.
        monkeypatch.delenv("AEGIS_SES_SENDER", raising=False)
        monkeypatch.setattr(MockEmailSink, "_deliver", DeadEmailSink._deliver)

        settings = load_settings()
        settings.model_backend = "heuristic"
        settings.email_provider = "mock"

        scenario, world, signals = load_scenario("schema_drift")
        outcome, rt, _final = run_incident(
            settings=settings,
            platform=SimulatedPlatform(world),
            signals=signals,
            incident_id="INC-DELIVERY-TEST",
            responder=ScriptedResponder(scenario.scripted_responses),
            trace=TraceBus(),
            precedents=PrecedentStore(precedents=[]),
        )
        return outcome, rt

    def test_incident_reaches_a_terminal_state(self, outcome_and_runtime) -> None:
        outcome, _rt = outcome_and_runtime
        assert outcome.final_state in TERMINAL

    def test_every_message_is_marked_undelivered(self, outcome_and_runtime) -> None:
        _outcome, rt = outcome_and_runtime
        assert rt.email.sent, "the run should still have attempted to tell people things"
        assert len(rt.email.undelivered()) == len(rt.email.sent)

    def test_the_audit_chain_still_verifies(self, outcome_and_runtime) -> None:
        """The record of a degraded run has to be as tamper-evident as a clean one."""
        _outcome, rt = outcome_and_runtime
        assert rt.chain.verify().valid

    def test_the_chain_names_who_was_not_reached(self, outcome_and_runtime) -> None:
        _outcome, rt = outcome_and_runtime
        actions = {event.action for event in rt.chain.events}
        assert "approval_request_undelivered" in actions

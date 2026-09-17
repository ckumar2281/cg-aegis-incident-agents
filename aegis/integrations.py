"""
Outbound integrations: email, ticketing, version control.

Each is an interface with a real adapter and a mock. The mocks are not throwaway --
they record everything they were asked to do, which is what lets the demo show the
full approval chain and the eval harness assert on it without a live Jira or a real
inbox. `MockEmailSink.sent` is, in effect, the transcript of who was told what and when.

One rule is enforced here rather than trusted to callers: **the technical tier cannot
be delivered until the business gate has released it.** `EmailSink.send` refuses a
DisclosureTier.TECHNICAL message whose bundle has not been released, and raises rather
than silently dropping it. Putting the check at the delivery boundary means no future
code path can leak the fix packet by forgetting to look at a flag.

The second rule is the mirror image: a *transport* failure is recorded, not raised.
A refused disclosure stops the incident; an unreachable mail server does not. See
`EmailSink._attempt` for why those two are treated as opposites.
"""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .contracts import (
    ApprovalRequest,
    BacklogEntry,
    CodeChange,
    DisclosureTier,
    HumanRole,
    IncidentPacket,
    PullRequestRef,
    TicketRef,
)

if TYPE_CHECKING:  # pragma: no cover - avoids a policy <-> integrations import cycle
    from .policy import RedactionPolicy


class DisclosureViolation(RuntimeError):
    """Raised when something tries to send technical detail before it is released."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #


@dataclass
class SentMessage:
    to: str
    subject: str
    body: str
    tier: DisclosureTier
    sent_at: datetime
    message_id: str
    incident_id: str = ""
    role: str = ""
    #: "approval_request" asks someone to rule on a fix and therefore carries the
    #: fix packet. "notification" tells someone what already happened. The
    #: disclosure rule that the technical packet must wait for business approval
    #: applies to the former; conflating the two hides which one was actually sent.
    kind: str = "notification"
    #: Whether the transport actually accepted the message. `sent` used to mean
    #: "we tried"; a run with a broken SES credential recorded seven delivered
    #: messages and nobody had received any of them. These two fields are the
    #: difference between an outbox and a delivery record.
    delivered: bool = True
    delivery_error: str = ""


class EmailSink(ABC):
    """Delivers approval requests and notifications."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []

    def send_approval_request(
        self,
        request: ApprovalRequest,
        *,
        technical_released: bool,
        redaction: "RedactionPolicy | None" = None,
    ) -> SentMessage:
        # The firewall, enforced at the boundary rather than by convention.
        if request.tier is DisclosureTier.TECHNICAL and not technical_released:
            raise DisclosureViolation(
                f"Refusing to send technical-tier detail for {request.incident_id} to "
                f"{request.role.value}: the business gate has not released it. "
                "This is the disclosure firewall working as designed."
            )
        # The tier check above governs *who* may be sent the technical packet. It says
        # nothing about *what* is in a business-tier email -- and until now nothing did.
        # The brief's fields were scanned individually by the disclosure agent, but the
        # rendered body that actually leaves the building was never checked, so anything
        # the renderer added between the fields was outside the firewall entirely.
        if request.tier is DisclosureTier.BUSINESS and redaction is not None:
            findings = redaction.check_text(
                request.subject, "subject"
            ) + redaction.check_text(request.body, "body")
            if findings:
                raise DisclosureViolation(
                    f"Refusing to send a business-tier approval request for "
                    f"{request.incident_id} to {request.role.value}: "
                    + "; ".join(f.render() for f in findings)
                )
        message = SentMessage(
            to=request.recipient,
            subject=request.subject,
            body=request.body,
            tier=request.tier,
            sent_at=_now(),
            message_id=f"msg-{uuid.uuid4().hex[:12]}",
            incident_id=request.incident_id,
            role=request.role.value,
            kind="approval_request",
        )
        self._attempt(message)
        self.sent.append(message)
        return message

    def send_notification(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        role: HumanRole,
        redaction: "RedactionPolicy | None" = None,
        incident_id: str = "",
    ) -> SentMessage:
        """Tell someone what happened.

        The tier is *derived from the recipient's role*, never passed in. An earlier
        version defaulted every notification to BUSINESS while sending all of them to
        the pipeline owner -- a technical recipient. Nothing leaked, because the
        content was going to the right person, but the audit trail recorded the wrong
        tier for five of the seven messages in a typical incident, and the eval check
        that asserts "no technical message before business approval" silently stopped
        seeing them. A control that mislabels what it is guarding cannot prove
        anything. One source of truth now: `HumanRole.tier`.

        Business-tier notifications go through the same firewall as business-tier
        approval requests, over subject *and* body. Subject was previously unchecked
        anywhere -- which is how `[resolved] RAW.STRIPE_CHARGES schema drift:
        CURRENCY_CODE column missing` would have reached a business inbox.
        """
        tier = role.tier
        if tier is DisclosureTier.BUSINESS and redaction is not None:
            findings = redaction.check_text(subject, "subject") + redaction.check_text(
                body, "body"
            )
            if findings:
                raise DisclosureViolation(
                    f"Refusing to send a business-tier notification for "
                    f"{incident_id or 'incident'} to {role.value}: "
                    + "; ".join(f.render() for f in findings)
                )
        message = SentMessage(
            to=to,
            subject=subject,
            body=body,
            tier=tier,
            sent_at=_now(),
            message_id=f"msg-{uuid.uuid4().hex[:12]}",
            incident_id=incident_id,
            role=role.value,
        )
        self._attempt(message)
        self.sent.append(message)
        return message

    def _attempt(self, message: SentMessage) -> None:
        """Deliver, recording transport failure rather than raising through the graph.

        Deliberately *not* symmetric with the firewall above. A DisclosureViolation is
        the control working -- it means we were about to tell the wrong person something,
        and the only safe response is to stop. It is raised before this method is
        reached and is never caught here.

        A transport failure is the opposite: the content was correct and permitted, the
        pipe was broken. Killing a two-minute, $0.15 incident run because SES could not
        find a credential throws away the analysis and, worse, throws away the audit
        trail that would have explained why. The honest behaviour is to carry on and
        record that this specific message did not arrive -- which is what lets the gate
        logic decide whether a human can still be said to have been asked.
        """
        try:
            self._deliver(message)
        except Exception as exc:  # noqa: BLE001 - transport failures must not be fatal
            message.delivered = False
            message.delivery_error = f"{type(exc).__name__}: {exc}"

    @abstractmethod
    def _deliver(self, message: SentMessage) -> None: ...

    def undelivered(self) -> list[SentMessage]:
        return [m for m in self.sent if not m.delivered]

    def for_tier(self, tier: DisclosureTier) -> list[SentMessage]:
        return [m for m in self.sent if m.tier is tier]


class MockEmailSink(EmailSink):
    """Records instead of sending. The demo's transcript of the approval chain."""

    def _deliver(self, message: SentMessage) -> None:  # noqa: D102
        return None


class SesEmailSink(EmailSink):
    """Real delivery via Amazon SES."""

    def __init__(self, sender: str, region: str) -> None:
        super().__init__()
        self.sender = sender
        try:
            import boto3  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required for the SES email sink") from exc
        self._client = boto3.client("sesv2", region_name=region)

    def _deliver(self, message: SentMessage) -> None:
        self._client.send_email(
            FromEmailAddress=self.sender,
            Destination={"ToAddresses": [message.to]},
            Content={
                "Simple": {
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}},
                }
            },
        )


# --------------------------------------------------------------------------- #
# Ticketing
# --------------------------------------------------------------------------- #


class TicketSink(ABC):
    """Raises a ticket when the business declines a fix."""

    def __init__(self) -> None:
        self.created: list[TicketRef] = []

    def open_ticket(
        self,
        packet: IncidentPacket,
        *,
        summary: str,
        description: str,
        assignee: str = "",
        labels: list[str] | None = None,
    ) -> TicketRef:
        ref = self._create(packet, summary, description, assignee, labels or [])
        self.created.append(ref)
        return ref

    @abstractmethod
    def _create(
        self,
        packet: IncidentPacket,
        summary: str,
        description: str,
        assignee: str,
        labels: list[str],
    ) -> TicketRef: ...


class MockTicketSink(TicketSink):
    def __init__(self) -> None:
        super().__init__()
        self._seq = 0

    def _create(self, packet, summary, description, assignee, labels) -> TicketRef:
        self._seq += 1
        key = f"DATA-{4100 + self._seq}"
        return TicketRef(
            provider="mock",
            key=key,
            url=f"https://jira.example.com/browse/{key}",
            summary=summary,
            assignee=assignee,
        )


class JiraTicketSink(TicketSink):
    """Jira Cloud REST API v3."""

    def __init__(self, base_url: str, project_key: str, email: str, api_token: str) -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.project_key = project_key
        self._auth = (email, api_token)

    def _create(self, packet, summary, description, assignee, labels) -> TicketRef:
        import requests  # noqa: PLC0415

        payload = {
            "fields": {
                "project": {"key": self.project_key},
                "summary": summary[:250],
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": description}],
                        }
                    ],
                },
                "issuetype": {"name": "Bug"},
                "labels": ["aegis", f"severity-{packet.severity.value.lower()}", *labels],
            }
        }
        response = requests.post(
            f"{self.base_url}/rest/api/3/issue",
            json=payload,
            auth=self._auth,
            timeout=30,
        )
        response.raise_for_status()
        key = response.json()["key"]
        return TicketRef(
            provider="jira",
            key=key,
            url=f"{self.base_url}/browse/{key}",
            summary=summary,
            assignee=assignee,
        )


class ServiceNowTicketSink(TicketSink):
    """ServiceNow Table API."""

    def __init__(self, instance_url: str, user: str, password: str) -> None:
        super().__init__()
        self.instance_url = instance_url.rstrip("/")
        self._auth = (user, password)

    def _create(self, packet, summary, description, assignee, labels) -> TicketRef:
        import requests  # noqa: PLC0415

        severity_map = {"SEV1": "1", "SEV2": "2", "SEV3": "3", "SEV4": "4"}
        response = requests.post(
            f"{self.instance_url}/api/now/table/incident",
            json={
                "short_description": summary[:160],
                "description": description,
                "urgency": severity_map.get(packet.severity.value, "3"),
                "category": "data_quality",
                "assigned_to": assignee,
            },
            auth=self._auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        result = response.json()["result"]
        number = result.get("number", "")
        return TicketRef(
            provider="servicenow",
            key=number,
            url=f"{self.instance_url}/nav_to.do?uri=incident.do?sys_id={result.get('sys_id', '')}",
            summary=summary,
            assignee=assignee,
        )


# --------------------------------------------------------------------------- #
# Version control
# --------------------------------------------------------------------------- #


class VcsClient(ABC):
    """Opens the pull request carrying the agent-authored fix."""

    def __init__(self) -> None:
        self.pull_requests: list[PullRequestRef] = []

    def open_pull_request(
        self,
        *,
        incident_id: str,
        title: str,
        body: str,
        branch: str,
        changes: list[CodeChange],
        reviewers: list[str] | None = None,
    ) -> PullRequestRef:
        ref = self._open(incident_id, title, body, branch, changes, reviewers or [])
        self.pull_requests.append(ref)
        return ref

    @abstractmethod
    def _open(
        self,
        incident_id: str,
        title: str,
        body: str,
        branch: str,
        changes: list[CodeChange],
        reviewers: list[str],
    ) -> PullRequestRef: ...


class MockVcsClient(VcsClient):
    def __init__(self, repo: str = "example-org/northwind-dbt") -> None:
        super().__init__()
        self.repo = repo
        self._seq = 1900

    def _open(self, incident_id, title, body, branch, changes, reviewers) -> PullRequestRef:
        self._seq += 1
        return PullRequestRef(
            provider="mock",
            repo=self.repo,
            number=self._seq,
            url=f"https://github.com/{self.repo}/pull/{self._seq}",
            branch=branch,
            title=title,
            reviewers=reviewers,
        )


class GitHubVcsClient(VcsClient):
    """
    Real GitHub PRs via the REST API.

    Creates a branch off the default branch, commits each change, opens the PR.
    Deliberately does not merge -- the whole point is that a human reviews it.
    """

    def __init__(self, repo: str, token: str) -> None:
        super().__init__()
        self.repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _api(self, method: str, path: str, **kwargs) -> Any:
        import requests  # noqa: PLC0415

        response = requests.request(
            method, f"https://api.github.com/repos/{self.repo}{path}",
            headers=self._headers, timeout=30, **kwargs
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _open(self, incident_id, title, body, branch, changes, reviewers) -> PullRequestRef:
        import base64  # noqa: PLC0415

        repo_info = self._api("GET", "")
        base = repo_info["default_branch"]
        base_sha = self._api("GET", f"/git/ref/heads/{base}")["object"]["sha"]
        self._api(
            "POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": base_sha}
        )

        for change in changes:
            try:
                existing = self._api("GET", f"/contents/{change.path}", params={"ref": branch})
                sha = existing.get("sha")
            except Exception:
                sha = None
            payload = {
                "message": f"{incident_id}: {change.change_kind} in {change.path}",
                "content": base64.b64encode(
                    _apply_patch_placeholder(change).encode()
                ).decode(),
                "branch": branch,
            }
            if sha:
                payload["sha"] = sha
            self._api("PUT", f"/contents/{change.path}", json=payload)

        pr = self._api(
            "POST", "/pulls",
            json={"title": title, "body": body, "head": branch, "base": base},
        )
        if reviewers:
            try:
                self._api(
                    "POST", f"/pulls/{pr['number']}/requested_reviewers",
                    json={"reviewers": reviewers},
                )
            except Exception:
                pass  # a bad reviewer handle must not sink the PR
        return PullRequestRef(
            provider="github",
            repo=self.repo,
            number=pr["number"],
            url=pr["html_url"],
            branch=branch,
            title=title,
            reviewers=reviewers,
        )


def _apply_patch_placeholder(change: CodeChange) -> str:
    """
    Render a change as file content for the commit.

    Aegis reasons in unified diffs because that is what a reviewer reads. Applying one
    to a real repository properly means fetching the file and patching it; against a
    repo Aegis has never seen, writing the diff itself as an annotated file is the
    honest fallback -- the PR still shows exactly what is proposed and why, and a human
    resolves it. Wire this to your own repo layout before using it for real.
    """
    return (
        f"# AEGIS PROPOSED CHANGE -- {change.change_kind}\n"
        f"# {change.rationale}\n"
        f"# Tests to add: {', '.join(change.tests_added) or 'none'}\n"
        f"#\n"
        f"# Proposed diff:\n"
        + "\n".join(f"# {line}" for line in change.diff.splitlines())
        + "\n"
    )


# --------------------------------------------------------------------------- #
# Backlog
# --------------------------------------------------------------------------- #


@dataclass
class BacklogStore:
    """
    The future-issues list.

    A deferred incident is not a dropped one. It keeps the business framing and the
    high-level change so that when somebody reviews the backlog in three weeks, the
    context is still there rather than reconstructed from a stale alert.
    """

    path: str | None = None
    entries: list[BacklogEntry] = field(default_factory=list)

    def add(self, entry: BacklogEntry) -> BacklogEntry:
        self.entries.append(entry)
        if self.path:
            existing: list[dict[str, Any]] = []
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as handle:
                        existing = json.load(handle)
                except (json.JSONDecodeError, OSError):
                    existing = []
            existing.append(json.loads(entry.model_dump_json()))
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(existing, handle, indent=2, default=str)
        return entry


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


def email_mode(settings) -> str:
    """What the email sink will actually be -- for the run banner, before anything runs.

    The fallback below is correct but silent, and silence cost an afternoon: with
    AEGIS_SES_SENDER unexported, a run printed five delivered messages and had emailed
    nobody. Anything that can quietly become a mock has to say so at the top.
    """
    if settings.email_provider == "ses" and os.environ.get("AEGIS_SES_SENDER"):
        return "ses"
    return "mock"


def build_integrations(settings) -> tuple[EmailSink, TicketSink, VcsClient, BacklogStore]:
    """Wire the configured adapters, falling back to mocks when credentials are absent."""
    if email_mode(settings) == "ses":
        email: EmailSink = SesEmailSink(os.environ["AEGIS_SES_SENDER"], settings.aws_region)
    else:
        email = MockEmailSink()

    if settings.ticket_provider == "jira" and os.environ.get("JIRA_API_TOKEN"):
        tickets: TicketSink = JiraTicketSink(
            os.environ.get("JIRA_BASE_URL", ""),
            os.environ.get("JIRA_PROJECT_KEY", "DATA"),
            os.environ.get("JIRA_EMAIL", ""),
            os.environ["JIRA_API_TOKEN"],
        )
    elif settings.ticket_provider == "servicenow" and os.environ.get("SERVICENOW_PASSWORD"):
        tickets = ServiceNowTicketSink(
            os.environ.get("SERVICENOW_INSTANCE", ""),
            os.environ.get("SERVICENOW_USER", ""),
            os.environ["SERVICENOW_PASSWORD"],
        )
    else:
        tickets = MockTicketSink()

    if settings.vcs_provider == "github" and os.environ.get("GITHUB_TOKEN"):
        vcs: VcsClient = GitHubVcsClient(settings.github_repo, os.environ["GITHUB_TOKEN"])
    else:
        vcs = MockVcsClient(settings.github_repo)

    return email, tickets, vcs, BacklogStore(path="runs/backlog.json")

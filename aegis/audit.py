"""
Hash-chained audit trail and the live trace bus.

Two related concerns:

* `AuditChain` is the compliance artifact. Every state transition, disclosure,
  approval and action is appended as an `AuditEvent` carrying the SHA-256 digest of
  the event before it. Any edit to incident history breaks the chain and
  `verify()` reports exactly where. This is what lets you answer "who approved the
  release of production data, and what were they shown at the time" months later.

* `TraceBus` is the developer-facing stream: the same events, plus agent-level
  timing and token spend, surfaced live to the console and to the HTML report.

They are separate because they have different retention and different audiences.
The audit chain is durable and legally interesting; the trace is ephemeral and
useful while debugging.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .contracts import AuditEvent, DisclosureTier

GENESIS = "0" * 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Audit chain
# --------------------------------------------------------------------------- #


@dataclass
class ChainVerification:
    valid: bool
    events: int
    broken_at: int | None = None
    reason: str = ""


class AuditChain:
    """Append-only, tamper-evident event log for one incident."""

    def __init__(self, incident_id: str) -> None:
        self.incident_id = incident_id
        self._events: list[AuditEvent] = []

    def append(
        self,
        *,
        actor: str,
        actor_kind: Literal["agent", "human", "system"],
        action: str,
        detail: dict[str, Any] | None = None,
        tier: DisclosureTier | None = None,
        at: datetime | None = None,
    ) -> AuditEvent:
        prev = self._events[-1].hash if self._events else GENESIS
        event = AuditEvent(
            seq=len(self._events),
            incident_id=self.incident_id,
            at=at or _now(),
            actor=actor,
            actor_kind=actor_kind,
            action=action,
            detail=detail or {},
            tier=tier,
            prev_hash=prev,
        )
        event.hash = event.compute_hash()
        self._events.append(event)
        return event

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def verify(self) -> ChainVerification:
        prev = GENESIS
        for event in self._events:
            if event.prev_hash != prev:
                return ChainVerification(
                    False,
                    len(self._events),
                    event.seq,
                    f"event {event.seq} expected prev_hash {prev[:12]}..., "
                    f"found {event.prev_hash[:12]}...",
                )
            recomputed = event.compute_hash()
            if recomputed != event.hash:
                return ChainVerification(
                    False,
                    len(self._events),
                    event.seq,
                    f"event {event.seq} content does not match its digest "
                    "(record was altered after being written)",
                )
            prev = event.hash
        return ChainVerification(True, len(self._events))

    def to_rows(self) -> list[dict[str, Any]]:
        """Shape for persistence into Snowflake AEGIS.OPS.INCIDENT_AUDIT."""
        return [
            {
                "INCIDENT_ID": e.incident_id,
                "SEQ": e.seq,
                "EVENT_AT": e.at.isoformat(),
                "ACTOR": e.actor,
                "ACTOR_KIND": e.actor_kind,
                "ACTION": e.action,
                "DISCLOSURE_TIER": e.tier.value if e.tier else None,
                "DETAIL": json.dumps(e.detail, default=str),
                "PREV_HASH": e.prev_hash,
                "HASH": e.hash,
            }
            for e in self._events
        ]


# --------------------------------------------------------------------------- #
# Trace bus
# --------------------------------------------------------------------------- #


@dataclass
class TraceEvent:
    at: datetime
    kind: str
    actor: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    usd: float = 0.0


class TraceBus:
    """Collects trace events and fans them out to subscribers (console, HTML report)."""

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []
        self._subscribers: list[Callable[[TraceEvent], None]] = []

    def subscribe(self, fn: Callable[[TraceEvent], None]) -> None:
        self._subscribers.append(fn)

    def emit(
        self,
        kind: str,
        actor: str,
        message: str,
        data: dict[str, Any] | None = None,
        duration_ms: int = 0,
        usd: float = 0.0,
    ) -> TraceEvent:
        event = TraceEvent(
            at=_now(),
            kind=kind,
            actor=actor,
            message=message,
            data=data or {},
            duration_ms=duration_ms,
            usd=usd,
        )
        self._events.append(event)
        for sub in self._subscribers:
            try:
                sub(event)
            except Exception:  # a broken renderer must never break the incident
                pass
        return event

    @property
    def events(self) -> list[TraceEvent]:
        return list(self._events)

    def handoffs(self) -> list[TraceEvent]:
        return [e for e in self._events if e.kind == "handoff"]

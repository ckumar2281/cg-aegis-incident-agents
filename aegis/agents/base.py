"""
Shared agent scaffolding.

Every Aegis agent follows the same three-beat shape, and the separation between the
beats is the reason the system is testable:

1. **Gather** -- call tools. Pure, deterministic, no model involved.
2. **Decide** -- compute the structured answer in Python (`fallback`), then optionally
   let the model revise or enrich it.
3. **Attribute** -- attach the tool calls that produced the facts to the evidence
   emitted, so the audit trail can reconstruct the reasoning.

Because step 2 always has a working Python answer before the model is consulted, an
agent can never fail to produce a valid contract. The model improves the output; it
is not load-bearing for the system continuing to function.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

from pydantic import BaseModel

from ..audit import AuditChain, TraceBus
from ..config import Settings
from ..contracts import AgentRole, DisclosureTier, Evidence, ToolCall
from ..reasoning import Reasoner, ThoughtResult
from ..tools import ToolBelt

T = TypeVar("T", bound=BaseModel)

#: Prepended to every agent system prompt. Keeps the model inside the contract and
#: stops it inventing facts the tool layer did not supply.
COMMON_RULES = """\
You are one specialist agent inside Aegis, a governed incident-management system for
data pipelines. You are not the whole system: other agents handle the work outside
your remit, and a human approval chain decides what actually happens.

Hard rules:
- Reply with a single JSON object matching the requested schema. No prose, no markdown.
- Use ONLY the facts supplied in the brief. If a fact is not there, you do not know it.
  Never invent table names, row counts, timestamps, people, or error messages.
- If the evidence does not support a conclusion, say so and lower your confidence.
  An honest "I could not determine this" is more useful than a confident guess,
  because it triggers another investigation round rather than a wrong fix.
- Be specific and concrete. "Row count is 3.1x the 30-day median" beats "volume looks off".
"""


class Agent:
    """Base class: wires an agent to its tools, reasoner, trace and audit chain."""

    role: AgentRole = AgentRole.SUPERVISOR
    system_prompt: str = COMMON_RULES

    def __init__(
        self,
        *,
        tools: ToolBelt,
        reasoner: Reasoner,
        settings: Settings,
        trace: TraceBus,
        chain: AuditChain,
    ) -> None:
        self.tools = tools
        self.reasoner = reasoner
        self.settings = settings
        self.trace = trace
        self.chain = chain
        self._evidence_seq = 0

    # -- helpers ------------------------------------------------------------ #

    def next_evidence_id(self) -> str:
        self._evidence_seq += 1
        return f"EV-{self.role.value[:4].upper()}-{self._evidence_seq:03d}"

    def evidence(
        self,
        claim: str,
        *,
        detail: str = "",
        strength: float = 0.5,
        supports: list[str] | None = None,
        refutes: list[str] | None = None,
        directive_id: str | None = None,
        provenance: list[ToolCall] | None = None,
        sensitive: bool = False,
    ) -> Evidence:
        return Evidence(
            evidence_id=self.next_evidence_id(),
            author=self.role,
            directive_id=directive_id,
            claim=claim,
            detail=detail,
            strength=strength,
            supports_tags=supports or [],
            refutes_tags=refutes or [],
            provenance=provenance or [],
            contains_sensitive=sensitive,
        )

    def think(
        self,
        *,
        user: str,
        response_model: type[T],
        fallback_value: T,
        system: str | None = None,
    ) -> tuple[T, ThoughtResult]:
        """Ask the model to improve on a Python-computed answer."""
        result = self.reasoner.think(
            role=self.role,
            system=system or self.system_prompt,
            user=user,
            response_model=response_model,
            fallback=lambda: fallback_value,
        )
        return result.value, result  # type: ignore[return-value]

    def handoff(self, to: AgentRole, payload: str, data: dict[str, Any] | None = None) -> None:
        """Record a contract crossing an agent boundary."""
        self.trace.emit(
            kind="handoff",
            actor=self.role.value,
            message=f"{self.role.value} -> {to.value}: {payload}",
            data={"from": self.role.value, "to": to.value, **(data or {})},
        )

    def audit(
        self,
        action: str,
        detail: dict[str, Any] | None = None,
        tier: DisclosureTier | None = None,
    ) -> None:
        """Append to the incident's hash chain. `tier` records who was allowed to see it."""
        self.chain.append(
            actor=self.role.value,
            actor_kind="agent",
            action=action,
            detail=detail or {},
            tier=tier,
        )

    def timed(self) -> float:
        return time.perf_counter()

    @staticmethod
    def ms_since(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

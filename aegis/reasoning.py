"""
The reasoning layer: Bedrock-backed inference with a deterministic fallback.

Every agent in Aegis calls `Reasoner.think(...)` with a system prompt, a user
message, the Pydantic model it expects back, and -- crucially -- a `fallback`
callable that computes the same structured answer deterministically in Python.

That fallback is not a stub. It is a real rule-based reasoner over the same evidence,
and it earns its place three times over:

* **Cost.** Development, the test suite and the eval harness all run on it, so
  iterating on the graph costs nothing. Bedrock tokens are spent only on real demo runs.
* **Determinism.** An agentic system whose behaviour changes every run is untestable.
  The fallback path gives byte-identical output, so regressions in the orchestration
  logic are visible rather than lost in model variance.
* **Resilience.** It is also the failure path. Throttling, a malformed JSON response,
  or the per-incident budget ceiling all degrade to it rather than failing the
  incident, and the degradation is stamped into the audit trail.

The distinction matters when reading results: the model contributes judgement,
prioritisation and natural-language rendering. The tool layer contributes facts. On
the heuristic path the facts are unchanged and the judgement is rule-based.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .audit import TraceBus
from .config import BudgetLedger, Settings, model_for
from .contracts import AgentRole

T = TypeVar("T", bound=BaseModel)


class ReasoningError(RuntimeError):
    pass


@dataclass
class ThoughtResult:
    """What came back, and how it was produced. `source` ends up in the audit trail."""

    value: BaseModel
    source: str  # "bedrock" | "heuristic" | "heuristic:degraded" | "heuristic:repair-failed"
    model_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    usd: float = 0.0
    latency_ms: int = 0
    attempts: int = 0


# --------------------------------------------------------------------------- #
# JSON extraction
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> dict[str, Any]:
    """
    Pull a JSON object out of a model response.

    Models wrap JSON in prose or fences often enough that a bare `json.loads` is a
    reliability bug. Tries the fenced block, then the outermost balanced braces.
    """
    if not text:
        raise ReasoningError("empty model response")

    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : idx + 1])
                    break

    candidates.append(text.strip())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ReasoningError("no JSON object found in model response")


# --------------------------------------------------------------------------- #
# Bedrock backend
# --------------------------------------------------------------------------- #


class BedrockBackend:
    """
    Thin wrapper over the Bedrock Converse API.

    System prompts are marked with a cache point: agent system prompts are long,
    static and reused across every incident, so caching them cuts input cost to
    roughly a tenth of base rate on repeat calls.
    """

    def __init__(self, region: str) -> None:
        try:
            import boto3  # noqa: PLC0415 -- optional dependency, only needed for real runs
        except ImportError as exc:  # pragma: no cover
            raise ReasoningError(
                "boto3 is required for the bedrock backend. "
                "Install it, or run with AEGIS_MODEL_BACKEND=heuristic."
            ) from exc
        self._client = boto3.client("bedrock-runtime", region_name=region)

    def converse(
        self,
        *,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        metadata: dict[str, str] | None = None,
    ) -> tuple[str, int, int, int]:
        kwargs: dict[str, Any] = {
            "modelId": model_id,
            "system": [{"text": system}, {"cachePoint": {"type": "default"}}],
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if metadata:
            # Recorded in Bedrock's model invocation logs, so a CloudWatch Logs Insights
            # query can break an incident's spend down *per agent* rather than showing
            # one undifferentiated pile of calls. Ignored by Bedrock when invocation
            # logging is off, so this is safe whether or not it has been enabled.
            kwargs["requestMetadata"] = metadata
        response = self._client.converse(**kwargs)
        blocks = response.get("output", {}).get("message", {}).get("content", [])
        text = "".join(b.get("text", "") for b in blocks)
        usage = response.get("usage", {}) or {}
        return (
            text,
            int(usage.get("inputTokens", 0)),
            int(usage.get("outputTokens", 0)),
            int(usage.get("cacheReadInputTokens", 0)),
        )


# --------------------------------------------------------------------------- #
# Reasoner
# --------------------------------------------------------------------------- #

#: Bedrock rejects a request whose metadata falls outside its allowed character set,
#: and a rejected call mid-demo is a far worse outcome than a missing log tag. So the
#: values are sanitised rather than trusted: anything unexpected is stripped, not sent.
_METADATA_SAFE = re.compile(r"[^A-Za-z0-9 _.:/=+@-]")


def _request_metadata(*, incident: str, agent: str, attempt: int) -> dict[str, str]:
    """Tags carried into Bedrock's invocation logs. At most 16 pairs, 256 chars each."""
    pairs = {"incident": incident, "agent": agent, "attempt": str(attempt)}
    return {
        key: _METADATA_SAFE.sub("", value)[:256]
        for key, value in pairs.items()
        if value and _METADATA_SAFE.sub("", value)
    }


_REPAIR_HINT = (
    "\n\nYour previous reply could not be parsed into the required schema.\n"
    "Error:\n{error}\n\n"
    "Reply again with ONLY a single valid JSON object matching the schema. "
    "No prose, no markdown fences."
)


class Reasoner:
    """Routes agent thinking to Bedrock or the deterministic fallback, under budget."""

    def __init__(
        self,
        settings: Settings,
        ledger: BudgetLedger,
        trace: TraceBus | None = None,
        incident_id: str = "",
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.trace = trace
        self.incident_id = incident_id
        self._bedrock: BedrockBackend | None = None

    # -- backend selection -------------------------------------------------- #

    def _bedrock_backend(self) -> BedrockBackend:
        if self._bedrock is None:
            self._bedrock = BedrockBackend(self.settings.aws_region)
        return self._bedrock

    def _should_use_model(self) -> tuple[bool, str]:
        if self.settings.model_backend != "bedrock":
            return False, "heuristic"
        breached, why = self.ledger.would_breach()
        if breached and self.settings.budget.degrade_on_breach:
            self.ledger.mark_degraded(why)
            return False, "heuristic:degraded"
        return True, "bedrock"

    # -- main entry point --------------------------------------------------- #

    def think(
        self,
        *,
        role: AgentRole,
        system: str,
        user: str,
        response_model: type[T],
        fallback: Callable[[], T],
    ) -> ThoughtResult:
        """
        Produce a validated `response_model`.

        Order of preference: Bedrock (one parse-repair retry), then the deterministic
        fallback. The fallback never raises -- if it did there would be no way to
        close the incident, so it is written to always return something structurally
        valid, even if that something is "I could not determine this".
        """
        use_model, reason = self._should_use_model()

        if not use_model:
            started = time.perf_counter()
            value = fallback()
            latency = int((time.perf_counter() - started) * 1000)
            self._trace(role, reason, latency, 0.0)
            return ThoughtResult(
                value=value, source=reason, model_id="heuristic", latency_ms=latency
            )

        tier = model_for(role)
        prompt = user
        last_error = ""

        for attempt in (1, 2):
            started = time.perf_counter()
            try:
                text, in_tok, out_tok, cached = self._bedrock_backend().converse(
                    model_id=tier.model_id,
                    system=system,
                    user=prompt,
                    max_tokens=tier.max_tokens,
                    temperature=tier.temperature,
                    metadata=_request_metadata(
                        incident=self.incident_id, agent=role.value, attempt=attempt
                    ),
                )
            except Exception as exc:  # throttling, access denied, network
                latency = int((time.perf_counter() - started) * 1000)
                self.ledger.mark_degraded(f"bedrock call failed: {type(exc).__name__}")
                value = fallback()
                self._trace(role, "heuristic:error", latency, 0.0, detail=str(exc)[:200])
                return ThoughtResult(
                    value=value,
                    source="heuristic:error",
                    model_id="heuristic",
                    latency_ms=latency,
                    attempts=attempt,
                )

            latency = int((time.perf_counter() - started) * 1000)
            self.ledger.record(role, tier.model_id, in_tok, out_tok, cached)
            usd_before = self.ledger.usd

            try:
                payload = extract_json(text)
                value = response_model.model_validate(payload)
            except (ReasoningError, ValidationError) as exc:
                last_error = str(exc)[:600]
                if attempt == 1:
                    prompt = user + _REPAIR_HINT.format(error=last_error)
                    continue
                value = fallback()
                self._trace(role, "heuristic:repair-failed", latency, 0.0, detail=last_error)
                return ThoughtResult(
                    value=value,
                    source="heuristic:repair-failed",
                    model_id=tier.model_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cached_tokens=cached,
                    latency_ms=latency,
                    attempts=attempt,
                )

            cost = self.ledger.by_agent.get(role.value, 0.0)
            self._trace(role, "bedrock", latency, usd_before, model_id=tier.model_id)
            return ThoughtResult(
                value=value,
                source="bedrock",
                model_id=tier.model_id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cached_tokens=cached,
                usd=cost,
                latency_ms=latency,
                attempts=attempt,
            )

        # Unreachable, but keeps the type checker honest.
        return ThoughtResult(value=fallback(), source="heuristic", model_id="heuristic")

    # -- tracing ------------------------------------------------------------ #

    def _trace(
        self,
        role: AgentRole,
        source: str,
        latency_ms: int,
        usd: float,
        detail: str = "",
        model_id: str = "",
    ) -> None:
        if self.trace is None:
            return
        self.trace.emit(
            kind="reasoning",
            actor=role.value,
            message=f"reasoned via {source}",
            data={"source": source, "model_id": model_id, "detail": detail},
            duration_ms=latency_ms,
            usd=usd,
        )

"""
The tool layer: what agents are allowed to do, and the provenance they leave behind.

Two rules hold here, and they are what keep the system trustworthy.

**Facts come from tools, judgement comes from the model.** No agent is asked to
recall a row count or infer whether a deploy happened. It calls a tool, the tool
returns a compacted structure, and the model reasons over that. When the reasoning
layer degrades to the heuristic backend, the facts are identical -- only the
interpretation changes.

**Every call is recorded.** `ToolBelt.drain()` hands back the `ToolCall` records
accumulated since the last drain, and agents attach them to the `Evidence` they emit.
The result is that every claim in the final postmortem can be traced to the specific
query that produced it, which is what makes the audit trail worth keeping.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from .contracts import ToolCall
from .memory import IncidentMemory
from .platform.client import ActionResult, HealthCheck, PlatformClient


class ToolBelt:
    """A provenance-recording facade over the platform and incident memory."""

    def __init__(self, platform: PlatformClient, memory: IncidentMemory | None = None) -> None:
        self.platform = platform
        self.memory = memory or IncidentMemory()
        self._calls: list[ToolCall] = []

    # -- provenance --------------------------------------------------------- #

    def _run(
        self,
        tool: str,
        args: dict[str, Any],
        fn: Callable[[], Any],
        summarise: Callable[[Any], str],
    ) -> Any:
        started = time.perf_counter()
        result = fn()
        latency = int((time.perf_counter() - started) * 1000)
        self._calls.append(
            ToolCall(tool=tool, args=args, summary=summarise(result), latency_ms=latency)
        )
        return result

    def drain(self) -> list[ToolCall]:
        """Return and clear the calls made since the last drain."""
        calls, self._calls = self._calls, []
        return calls

    @property
    def call_count(self) -> int:
        return len(self._calls)

    # -- catalogue and lineage ---------------------------------------------- #

    def get_asset(self, name: str) -> dict[str, Any]:
        return self._run(
            "get_asset",
            {"name": name},
            lambda: self.platform.asset(name),
            lambda r: f"tier {r.get('tier', '?')} {r.get('layer', '?')} asset"
            if r.get("known")
            else "unknown asset",
        )

    def downstream(self, name: str) -> list[dict[str, Any]]:
        return self._run(
            "lineage_downstream",
            {"name": name},
            lambda: self.platform.lineage_downstream(name),
            lambda r: f"{len(r)} downstream assets",
        )

    def upstream(self, name: str) -> list[dict[str, Any]]:
        return self._run(
            "lineage_upstream",
            {"name": name},
            lambda: self.platform.lineage_upstream(name),
            lambda r: f"{len(r)} upstream assets",
        )

    def consumers(self, name: str) -> list[dict[str, Any]]:
        return self._run(
            "consumers",
            {"name": name},
            lambda: self.platform.consumers(name),
            lambda r: f"{len(r)} consumer surfaces",
        )

    # -- operational history ------------------------------------------------ #

    def task_runs(self, asset: str, hours: int = 48) -> list[dict[str, Any]]:
        return self._run(
            "task_runs",
            {"asset": asset, "hours": hours},
            lambda: self.platform.task_runs(asset, hours),
            lambda r: f"{len(r)} runs, {sum(1 for x in r if x['state'] == 'FAILED')} failed",
        )

    def failed_runs(self, hours: int = 24) -> list[dict[str, Any]]:
        return self._run(
            "failed_runs",
            {"hours": hours},
            lambda: self.platform.failed_runs(hours),
            lambda r: f"{len(r)} failed runs platform-wide",
        )

    def copy_history(self, table: str, hours: int = 48) -> list[dict[str, Any]]:
        return self._run(
            "copy_history",
            {"table": table, "hours": hours},
            lambda: self.platform.copy_history(table, hours),
            lambda r: f"{len(r)} load events, "
            f"{sum(1 for x in r if x['status'] != 'LOADED')} not fully loaded",
        )

    def dmf_results(self, asset: str, hours: int = 48) -> list[dict[str, Any]]:
        return self._run(
            "dmf_results",
            {"asset": asset, "hours": hours},
            lambda: self.platform.dmf_results(asset, hours),
            lambda r: f"{sum(1 for x in r if x['breached'])} of {len(r)} metrics breached",
        )

    def metric_summary(self, asset: str, days: int = 30) -> dict[str, Any]:
        return self._run(
            "metric_summary",
            {"asset": asset, "days": days},
            lambda: self.platform.metric_summary(asset, days),
            lambda r: (
                f"rows {r['row_count']['ratio_to_median']}x median, "
                f"lag {r['freshness_lag_min']['latest']}m"
            )
            if r.get("available")
            else "no metrics",
        )

    def schema_diff(self, asset: str) -> dict[str, Any]:
        return self._run(
            "schema_diff",
            {"asset": asset},
            lambda: self.platform.schema_diff(asset),
            lambda r: (
                f"+{len(r.get('added_columns', []))} "
                f"-{len(r.get('removed_columns', []))} columns"
            )
            if r.get("changed")
            else "no schema change",
        )

    def changes(self, hours: int = 72) -> list[dict[str, Any]]:
        return self._run(
            "change_log",
            {"hours": hours},
            lambda: self.platform.changes(hours),
            lambda r: f"{len(r)} changes in window",
        )

    def file_info(self, file_id: str) -> dict[str, Any]:
        return self._run(
            "file_info",
            {"file_id": file_id},
            lambda: self.platform.file_info(file_id),
            lambda r: "quarantined" if r.get("quarantined") else "in raw zone",
        )

    # -- memory -------------------------------------------------------------- #

    def similar_incidents(self, *, asset: str, incident_type: str, symptom: str) -> list[dict]:
        return self._run(
            "similar_incidents",
            {"asset": asset, "incident_type": incident_type},
            lambda: self.memory.search(asset=asset, incident_type=incident_type, symptom=symptom),
            lambda r: f"{len(r)} similar past incidents",
        )

    # -- actions and verification ------------------------------------------- #

    def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        return self._run(
            f"action:{action}",
            params,
            lambda: self.platform.execute(action, params),
            lambda r: r.detail,
        )

    def health(self, assets: list[str]) -> list[HealthCheck]:
        return self._run(
            "health_check",
            {"assets": assets},
            lambda: self.platform.health(assets),
            lambda r: f"{sum(1 for c in r if c.healthy)}/{len(r)} assets healthy",
        )

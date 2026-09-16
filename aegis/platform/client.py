"""
The platform interface the agents see, plus the in-memory implementation.

`PlatformClient` is the seam between the agent layer and reality. Agents never touch
`World` or a Snowflake cursor directly -- they call these methods, which return
*compacted* structures: aggregates, deltas and diffs rather than raw rows.

That compaction is deliberate and does double duty. It keeps prompts small (the main
cost lever in a fan-out agent graph), and it stops the model from doing arithmetic it
is bad at. `metric_summary` computes the 30-day median and the deviation in Python;
the model is asked to interpret "row count is 3.1x median", not to derive it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .world import World


@dataclass
class ActionResult:
    action: str
    ok: bool
    detail: str
    data: dict[str, Any]


@dataclass
class HealthCheck:
    asset: str
    fresh: bool
    freshness_lag_min: float
    row_count: int
    row_count_vs_median: float
    null_rate: float
    healthy: bool
    notes: str = ""


class PlatformClient(Protocol):
    """
    The seam between the agent layer and reality.

    `SimulatedPlatform` below is the only implementation today. A Snowflake-backed one
    would implement this same protocol -- the agents call nothing else -- but it is not
    written yet. Documented here rather than implied, so the gap is visible in the code
    and not only in the limitations section of a document.
    """

    def asset(self, name: str) -> dict[str, Any]: ...
    def lineage_downstream(self, name: str) -> list[dict[str, Any]]: ...
    def lineage_upstream(self, name: str) -> list[dict[str, Any]]: ...
    def task_runs(self, asset: str, hours: int = 48) -> list[dict[str, Any]]: ...
    def failed_runs(self, hours: int = 24) -> list[dict[str, Any]]: ...
    def copy_history(self, table: str, hours: int = 48) -> list[dict[str, Any]]: ...
    def dmf_results(self, asset: str, hours: int = 48) -> list[dict[str, Any]]: ...
    def metric_summary(self, asset: str, days: int = 30) -> dict[str, Any]: ...
    def schema_diff(self, asset: str) -> dict[str, Any]: ...
    def changes(self, hours: int = 72) -> list[dict[str, Any]]: ...
    def consumers(self, asset: str) -> list[dict[str, Any]]: ...
    def file_info(self, file_id: str) -> dict[str, Any]: ...
    def execute(self, action: str, params: dict[str, Any]) -> ActionResult: ...
    def health(self, assets: list[str]) -> list[HealthCheck]: ...


# --------------------------------------------------------------------------- #


class SimulatedPlatform:
    """PlatformClient backed by the deterministic `World`."""

    def __init__(self, world: World) -> None:
        self.world = world

    # -- catalogue ---------------------------------------------------------- #

    def asset(self, name: str) -> dict[str, Any]:
        a = self.world.assets.get(name)
        if not a:
            return {"name": name, "known": False}
        return {
            "name": a.name,
            "known": True,
            "layer": a.layer,
            "tier": a.tier,
            "owner_team": a.owner_team,
            "domain": a.domain,
            "sla_minutes": a.sla_minutes,
            "is_financial": a.is_financial,
            "consumer_kind": a.consumer_kind,
            "business_process": a.business_process,
            "source_system": a.source_system,
            "upstreams": list(a.upstreams),
        }

    def lineage_downstream(self, name: str) -> list[dict[str, Any]]:
        depths = self.world.downstream_depths(name)
        return [
            {**self.asset(n), "depth": depths[n]}
            for n in sorted(depths, key=lambda k: (depths[k], k))
        ]

    def lineage_upstream(self, name: str) -> list[dict[str, Any]]:
        return [self.asset(n) for n in self.world.upstream(name)]

    # -- operational history ------------------------------------------------ #

    def task_runs(self, asset: str, hours: int = 48) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.run_id,
                "task": r.task_name,
                "asset": r.target_asset,
                "started_at": r.started_at.isoformat(),
                "duration_s": int((r.ended_at - r.started_at).total_seconds()),
                "state": r.state,
                "attempt": r.attempt,
                "error_code": r.error_code,
                "error_message": r.error_message,
                "rows_written": r.rows_written,
                "credits_used": r.credits_used,
            }
            for r in self.world.runs_for(asset, hours)
        ]

    def failed_runs(self, hours: int = 24) -> list[dict[str, Any]]:
        return [
            {
                "run_id": r.run_id,
                "task": r.task_name,
                "asset": r.target_asset,
                "started_at": r.started_at.isoformat(),
                "state": r.state,
                "attempt": r.attempt,
                "error_code": r.error_code,
                "error_message": r.error_message,
            }
            for r in self.world.failed_runs(hours)
        ]

    def copy_history(self, table: str, hours: int = 48) -> list[dict[str, Any]]:
        return [
            {
                "file_id": c.file_id,
                "file_name": c.file_name,
                "target_table": c.target_table,
                "loaded_at": c.loaded_at.isoformat(),
                "status": c.status,
                "rows_loaded": c.row_count,
                "rows_parsed": c.row_parsed,
                "errors_seen": c.errors_seen,
                "first_error": c.first_error,
                "first_error_column": c.first_error_column,
            }
            for c in self.world.copies_for(table, hours)
        ]

    def dmf_results(self, asset: str, hours: int = 48) -> list[dict[str, Any]]:
        return [
            {
                "asset": d.asset,
                "metric": d.metric_name,
                "column": d.column_name,
                "measured_at": d.measured_at.isoformat(),
                "value": d.value,
                "threshold": d.threshold,
                "breached": d.breached,
            }
            for d in self.world.dmf_for(asset, hours)
        ]

    # -- statistics --------------------------------------------------------- #

    def metric_summary(self, asset: str, days: int = 30) -> dict[str, Any]:
        """
        Compacted time-series: baseline vs latest, with the comparison already done.

        Returning 30 raw points per asset per metric would blow the context budget for
        no benefit -- what the agent needs is "3.1x median, outside the historical
        range", which is arithmetic, not judgement.
        """
        series = self.world.metric_series(asset, days)
        if not series:
            return {"asset": asset, "available": False}
        latest = series[-1]
        history = series[:-1] or series

        def _stat(values: list[float]) -> dict[str, float]:
            return {
                "median": round(statistics.median(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
            }

        rows = [float(p.row_count) for p in history]
        nulls = [p.null_rate for p in history]
        lags = [p.freshness_lag_min for p in history]
        distincts = [p.distinct_key_ratio for p in history]
        credits = [p.credits for p in history]

        row_median = statistics.median(rows) or 1.0
        return {
            "asset": asset,
            "available": True,
            "window_days": days,
            "latest_at": latest.at.isoformat(),
            "row_count": {
                "latest": latest.row_count,
                **_stat(rows),
                "ratio_to_median": round(latest.row_count / row_median, 3),
            },
            "null_rate": {
                "latest": round(latest.null_rate, 4),
                **_stat(nulls),
                "delta_pp": round((latest.null_rate - statistics.median(nulls)) * 100, 2),
            },
            "freshness_lag_min": {"latest": round(latest.freshness_lag_min, 1), **_stat(lags)},
            "distinct_key_ratio": {
                "latest": round(latest.distinct_key_ratio, 4),
                **_stat(distincts),
            },
            "credits": {
                "latest": latest.credits,
                **_stat(credits),
                "ratio_to_median": round(latest.credits / (statistics.median(credits) or 1.0), 2),
            },
        }

    def schema_diff(self, asset: str) -> dict[str, Any]:
        history = self.world.schema_history(asset)
        if len(history) < 2:
            return {"asset": asset, "changed": False, "versions": len(history)}
        previous, current = history[-2], history[-1]
        prev_cols = dict(previous.columns)
        curr_cols = dict(current.columns)
        added = sorted(set(curr_cols) - set(prev_cols))
        removed = sorted(set(prev_cols) - set(curr_cols))
        retyped = sorted(
            c for c in set(prev_cols) & set(curr_cols) if prev_cols[c] != curr_cols[c]
        )
        # A same-typed add+remove pair is the classic rename.
        likely_renames = [
            {"from": r, "to": a}
            for r in removed
            for a in added
            if prev_cols[r] == curr_cols[a]
        ]
        return {
            "asset": asset,
            "changed": bool(added or removed or retyped),
            "from_version": previous.version,
            "to_version": current.version,
            "observed_at": current.effective_from.isoformat(),
            "added_columns": added,
            "removed_columns": removed,
            "retyped_columns": retyped,
            "likely_renames": likely_renames,
            "contract_version": previous.version,
        }

    def changes(self, hours: int = 72) -> list[dict[str, Any]]:
        return [
            {
                "change_id": c.change_id,
                "at": c.at.isoformat(),
                "kind": c.kind,
                "title": c.title,
                "detail": c.detail,
                "author": c.author,
                "repo": c.repo,
                "pr_number": c.pr_number,
                "touched_assets": list(c.touched_assets),
            }
            for c in self.world.changes_in_window(hours)
        ]

    def consumers(self, asset: str) -> list[dict[str, Any]]:
        return [
            {
                "asset": c.asset,
                "consumer": c.consumer,
                "kind": c.consumer_kind,
                "reads_last_7d": c.reads_last_7d,
                "distinct_users": c.distinct_users,
            }
            for c in self.world.consumers_of(asset)
        ]

    def file_info(self, file_id: str) -> dict[str, Any]:
        f = self.world.files.get(file_id)
        if not f:
            return {"file_id": file_id, "known": False}
        return {
            "file_id": f.file_id,
            "known": True,
            "uri": f"s3://{f.bucket}/{f.key}",
            "source_system": f.source_system,
            "arrived_at": f.arrived_at.isoformat(),
            "size_bytes": f.size_bytes,
            "row_count": f.row_count,
            "target_table": f.target_table,
            "declared_schema_version": f.declared_schema_version,
            "quarantined": f.quarantined,
            "quarantine_uri": f.quarantine_uri,
        }

    # -- actions ------------------------------------------------------------ #

    def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return ActionResult(action, False, f"unknown action {action!r}", {})
        result: ActionResult = handler(params)
        self.world.action_log.append(
            {
                "action": action,
                "params": params,
                "ok": result.ok,
                "detail": result.detail,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return result

    def _heal(self, assets: list[str]) -> None:
        for name in assets:
            point = self.world.latest_metric(name)
            if point is None:
                continue
            series = self.world.metric_series(name, 30)
            history = series[:-1] or series
            point.row_count = int(statistics.median([p.row_count for p in history]))
            point.null_rate = statistics.median([p.null_rate for p in history])
            point.freshness_lag_min = 8.0
            point.distinct_key_ratio = statistics.median(
                [p.distinct_key_ratio for p in history]
            )
            point.credits = statistics.median([p.credits for p in history])

    def _act_rerun_task(self, params: dict[str, Any]) -> ActionResult:
        asset = params.get("asset", "")
        affected = [asset, *self.world.downstream(asset)]
        self._heal(affected)
        started = self.world.now
        return ActionResult(
            "rerun_task",
            True,
            f"Re-ran build for {asset} and {len(affected) - 1} downstream asset(s).",
            {"assets_rebuilt": affected, "started_at": started.isoformat()},
        )

    def _act_reprocess_file(self, params: dict[str, Any]) -> ActionResult:
        file_id = params.get("file_id", "")
        f = self.world.files.get(file_id)
        if not f:
            return ActionResult("reprocess_file", False, f"unknown file {file_id!r}", {})
        if f.quarantined:
            return ActionResult(
                "reprocess_file",
                False,
                "File is still quarantined; release_quarantine must run first.",
                {"file_id": file_id},
            )
        affected = [f.target_table, *self.world.downstream(f.target_table)]
        self._heal(affected)
        return ActionResult(
            "reprocess_file",
            True,
            f"Reprocessed {f.row_count:,} rows from {file_id} into {f.target_table}.",
            {"rows": f.row_count, "assets_rebuilt": affected},
        )

    def _act_release_quarantine(self, params: dict[str, Any]) -> ActionResult:
        file_id = params.get("file_id", "")
        f = self.world.files.get(file_id)
        if not f:
            return ActionResult("release_quarantine", False, f"unknown file {file_id!r}", {})
        f.quarantined = False
        return ActionResult(
            "release_quarantine",
            True,
            f"Released {file_id} from quarantine back to the raw zone.",
            {"file_id": file_id, "authorised": params.get("authorised_by", "")},
        )

    def _act_snapshot_table(self, params: dict[str, Any]) -> ActionResult:
        asset = params.get("asset", "")
        return ActionResult(
            "snapshot_table",
            True,
            f"Zero-copy clone {asset}_AEGIS_SNAPSHOT created as a rollback point.",
            {"clone": f"{asset}_AEGIS_SNAPSHOT"},
        )

    def _act_restate_table(self, params: dict[str, Any]) -> ActionResult:
        asset = params.get("asset", "")
        affected = [asset, *self.world.downstream(asset)]
        self._heal(affected)
        return ActionResult(
            "restate_table",
            True,
            f"Restated {asset} from source and rebuilt {len(affected) - 1} downstream asset(s).",
            {"assets_rebuilt": affected},
        )

    def _act_backfill_table(self, params: dict[str, Any]) -> ActionResult:
        asset = params.get("asset", "")
        self._heal([asset, *self.world.downstream(asset)])
        return ActionResult(
            "backfill_table",
            True,
            f"Backfilled {asset} for window {params.get('from', '?')}..{params.get('to', '?')}.",
            {"asset": asset},
        )

    def _act_rollback_deployment(self, params: dict[str, Any]) -> ActionResult:
        pr = params.get("pr_number")
        assets = params.get("assets") or []
        self._heal([*assets, *(d for a in assets for d in self.world.downstream(a))])
        return ActionResult(
            "rollback_deployment",
            True,
            f"Reverted PR #{pr} and redeployed the previous model version.",
            {"pr_number": pr, "assets": assets},
        )

    def _act_pause_downstream(self, params: dict[str, Any]) -> ActionResult:
        asset = params.get("asset", "")
        paused = self.world.downstream(asset)
        return ActionResult(
            "pause_downstream",
            True,
            f"Suspended {len(paused)} downstream task(s) to stop bad data propagating.",
            {"paused": paused},
        )

    def _act_resume_downstream(self, params: dict[str, Any]) -> ActionResult:
        asset = params.get("asset", "")
        resumed = self.world.downstream(asset)
        return ActionResult(
            "resume_downstream", True, f"Resumed {len(resumed)} downstream task(s).",
            {"resumed": resumed},
        )

    def _act_pin_schema_version(self, params: dict[str, Any]) -> ActionResult:
        return ActionResult(
            "pin_schema_version",
            True,
            f"Pinned {params.get('asset', '')} to contract {params.get('version', '')}.",
            dict(params),
        )

    def _act_notify_vendor(self, params: dict[str, Any]) -> ActionResult:
        return ActionResult(
            "notify_vendor",
            True,
            f"Raised a support case with {params.get('vendor', 'the vendor')}.",
            dict(params),
        )

    def _act_notify_pipeline_owner(self, params: dict[str, Any]) -> ActionResult:
        return ActionResult(
            "notify_pipeline_owner",
            True,
            f"Notified pipeline owner {params.get('owner', '')}.",
            dict(params),
        )

    # -- verification ------------------------------------------------------- #

    def health(self, assets: list[str]) -> list[HealthCheck]:
        checks: list[HealthCheck] = []
        for name in assets:
            summary = self.metric_summary(name)
            meta = self.asset(name)
            if not summary.get("available"):
                checks.append(HealthCheck(name, False, 0.0, 0, 0.0, 0.0, False, "no metrics"))
                continue
            sla = meta.get("sla_minutes") or 10_000
            lag = summary["freshness_lag_min"]["latest"]
            ratio = summary["row_count"]["ratio_to_median"]
            null_rate = summary["null_rate"]["latest"]
            fresh = lag <= sla
            volume_ok = 0.6 <= ratio <= 1.6
            nulls_ok = null_rate <= 0.05
            notes = []
            if not fresh:
                notes.append(f"stale by {lag - sla:.0f} min")
            if not volume_ok:
                notes.append(f"row count {ratio:.2f}x median")
            if not nulls_ok:
                notes.append(f"null rate {null_rate:.1%}")
            checks.append(
                HealthCheck(
                    asset=name,
                    fresh=fresh,
                    freshness_lag_min=lag,
                    row_count=summary["row_count"]["latest"],
                    row_count_vs_median=ratio,
                    null_rate=null_rate,
                    healthy=fresh and volume_ok and nulls_ok,
                    notes="; ".join(notes),
                )
            )
        return checks


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def within(a: datetime, b: datetime, minutes: int) -> bool:
    return abs((a - b).total_seconds()) <= minutes * 60


__all__ = [
    "ActionResult",
    "HealthCheck",
    "PlatformClient",
    "SimulatedPlatform",
    "timedelta",
    "utcnow",
    "within",
]

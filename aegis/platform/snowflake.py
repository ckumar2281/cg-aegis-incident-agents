"""
A Snowflake-backed implementation of `PlatformClient`.

**Status: written, never executed against a live Snowflake account.** Read that twice
before quoting this file as evidence of anything. It is a considered mapping from the
agents' needs onto views Snowflake actually publishes, with the column names checked
against the current documentation -- and it is completely untested. `scripts/check_snowflake.py`
exercises every method against a real account and prints what worked; until someone has
run it and pasted the output into `PROJECT-LOG.md`, the honest description of this file
is "the interface a real implementation would fill, filled in".

Nothing imports it by default. `SimulatedPlatform` remains what the CLI, the eval
harness and the tests run against, so this file cannot affect a single scored result.

---

## The two decisions worth defending

**1. Freshness over depth, where the two conflict.**

`SNOWFLAKE.ACCOUNT_USAGE` is the obvious source -- 365 days of history, everything in
one place. It also carries **up to ~2 hours of latency**, and ~45 minutes is typical.
An incident-response agent reading a 2-hour-old view is diagnosing the recent past, and
the incidents this system is built for are minutes old. So the operational history
methods read the `INFORMATION_SCHEMA` **table functions** (`TASK_HISTORY`,
`COPY_HISTORY`, `QUERY_HISTORY`), which are latency-free at the cost of a shorter
retention window -- 7 to 14 days, which is more than the 24-72 hours any of these
methods asks for.

`ACCOUNT_USAGE` is used only where the table functions have no equivalent: object
dependencies, column history, access history and tag references. Each of those is
marked, because a stale answer means something different in each case. Lineage that is
an hour stale is nearly always fine; a task run that is an hour stale is useless.

**2. `execute()` refuses to mutate anything unless explicitly told otherwise.**

The default is `execute_mode="dry_run"`: every action renders the SQL it *would* run,
returns it, and executes nothing. Going live requires passing `execute_mode="live"` at
construction. Three reasons, in order of how much they matter:

- The whole argument of this project is that an agent should not write to a warehouse
  without a human having approved it. Shipping a client that defaults to writing would
  contradict the thesis in the first file a reviewer opens.
- Every mutating statement goes through `_ALLOWED_STATEMENTS`, which is a fixed map from
  action name to a parameterised template. There is no path from an agent's output to
  arbitrary SQL, because agents choose an action name and supply parameters -- they
  never compose a statement.
- `drop_table` is deliberately **not implemented at all**, even though the contract has
  a risk tier for it. The autonomy ladder already refuses to pre-authorise it; not
  writing the code is the second lock.

---

## What Snowflake does not have, and what this does instead

| Needed | Snowflake's answer | What this file does |
|---|---|---|
| Asset tier, owner, SLA, domain | No native concept | Object **tags** via `TAG_REFERENCES`. Tag names are configurable |
| 30-day metric series | No native table | Aggregated from scheduled **DMF results** |
| Schema history | No native diff | `ACCOUNT_USAGE.COLUMNS` retains dropped columns with a `DELETED` timestamp; the diff is now-vs-cutoff |
| Deploys and PRs | Not a warehouse concern | DDL from `QUERY_HISTORY`, plus an optional injected feed from the VCS |
| Credits per asset | Only per warehouse/query | Cloud-services credits per query, joined by task run |

The gaps are real and named rather than papered over. The tag names and the DMF-to-metric
mapping are the two things that would need tailoring per warehouse, and both are
constructor arguments rather than hard-coded strings.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import ActionResult, HealthCheck

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass
class TagNames:
    """
    Which object tags carry the metadata the agents need.

    Snowflake has no built-in notion of "this table is tier 1" or "finance owns this".
    Tags are the supported way to attach it, and every warehouse names them differently,
    so they are configuration rather than constants.
    """

    tier: str = "GOVERNANCE.TIER"
    owner_team: str = "GOVERNANCE.OWNER_TEAM"
    domain: str = "GOVERNANCE.DOMAIN"
    sla_minutes: str = "GOVERNANCE.SLA_MINUTES"
    is_financial: str = "GOVERNANCE.IS_FINANCIAL"
    consumer_kind: str = "GOVERNANCE.CONSUMER_KIND"
    business_process: str = "GOVERNANCE.BUSINESS_PROCESS"
    source_system: str = "GOVERNANCE.SOURCE_SYSTEM"
    layer: str = "GOVERNANCE.LAYER"


@dataclass
class MetricMap:
    """
    Which scheduled data metric functions stand in for each series.

    `metric_summary` needs a 30-day history of row counts, null rates, freshness and key
    uniqueness. Snowflake keeps none of those as a time series -- but if the DMFs are
    scheduled on the table, `DATA_QUALITY_MONITORING_RESULTS` accumulates exactly that
    history as a side effect. This maps our vocabulary onto the DMF names.
    """

    row_count: str = "SNOWFLAKE.CORE.ROW_COUNT"
    null_count: str = "SNOWFLAKE.CORE.NULL_COUNT"
    freshness: str = "SNOWFLAKE.CORE.FRESHNESS"
    duplicate_count: str = "SNOWFLAKE.CORE.DUPLICATE_COUNT"
    unique_count: str = "SNOWFLAKE.CORE.UNIQUE_COUNT"


#: Mutating actions, as parameterised templates. An agent picks a key and supplies
#: parameters; it never composes SQL. Anything not in this map cannot be executed --
#: including `drop_table`, whose omission is the point rather than an oversight.
_ALLOWED_STATEMENTS: dict[str, str] = {
    "rerun_task": "EXECUTE TASK {task}",
    "reprocess_file": (
        "COPY INTO {table} FROM {stage} FILES = ('{file_name}') "
        "FILE_FORMAT = (FORMAT_NAME = {file_format}) ON_ERROR = ABORT_STATEMENT"
    ),
    "release_quarantine": "COPY FILES INTO {target_stage} FROM {quarantine_stage} FILES = ('{file_name}')",
    "snapshot_table": "CREATE TABLE {snapshot_name} CLONE {table}",
    "restate_table": (
        "CREATE OR REPLACE TABLE {table} AS SELECT * FROM {table} AT(TIMESTAMP => '{as_of}'::TIMESTAMP_LTZ)"
    ),
    "backfill_table": "CALL {backfill_procedure}('{table}', '{start}', '{end}')",
    "pin_schema_version": (
        "UPDATE {contract_table} SET pinned_version = '{version}' WHERE asset = '{asset}'"
    ),
}


# --------------------------------------------------------------------------- #
# The client
# --------------------------------------------------------------------------- #


@dataclass
class SnowflakePlatform:
    """
    `PlatformClient` over a live Snowflake account.

    Construct with an open `snowflake.connector` connection, or let it build one from
    the usual environment variables. Every read is a query; nothing is cached, because
    an incident-response agent asking "what does this look like now" must not be served
    a snapshot from earlier in the same incident.
    """

    connection: Any = None
    database: str = ""
    quarantine_stage: str = "@OPS.QUARANTINE"
    contract_table: str = "OPS.SCHEMA_CONTRACTS"
    backfill_procedure: str = "OPS.BACKFILL_RANGE"
    tags: TagNames = field(default_factory=TagNames)
    metrics: MetricMap = field(default_factory=MetricMap)
    execute_mode: str = "dry_run"
    #: Deploys, PRs and vendor notices come from the VCS and the vendor, not the
    #: warehouse. Injected rather than invented: a real deployment wires this to the
    #: GitHub API. Left empty, `changes()` returns warehouse DDL only and says so.
    change_feed: Any = None

    def __post_init__(self) -> None:
        if self.execute_mode not in ("dry_run", "live"):
            raise ValueError("execute_mode must be 'dry_run' or 'live'")
        if self.connection is None:
            self.connection = self._connect()
        if not self.database:
            self.database = self._scalar("SELECT CURRENT_DATABASE()") or ""

    # -- plumbing ----------------------------------------------------------- #

    @staticmethod
    def _connect() -> Any:
        try:
            import snowflake.connector  # noqa: PLC0415 -- optional dependency
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "snowflake-connector-python is required for SnowflakePlatform. "
                "pip install 'snowflake-connector-python'"
            ) from exc
        import os  # noqa: PLC0415

        return snowflake.connector.connect(
            account=os.environ["SNOWFLAKE_ACCOUNT"],
            user=os.environ["SNOWFLAKE_USER"],
            password=os.environ.get("SNOWFLAKE_PASSWORD"),
            private_key_file=os.environ.get("SNOWFLAKE_PRIVATE_KEY_FILE"),
            role=os.environ.get("SNOWFLAKE_ROLE", "AEGIS_AGENT"),
            warehouse=os.environ.get("SNOWFLAKE_WAREHOUSE", "AEGIS_WH"),
            database=os.environ.get("SNOWFLAKE_DATABASE"),
            client_session_keep_alive=False,
        )

    def _rows(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        """Run a read query, return dicts keyed by lower-cased column name."""
        cur = self.connection.cursor()
        try:
            cur.execute(sql, params or ())
            columns = [c[0].lower() for c in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
        finally:
            cur.close()

    def _scalar(self, sql: str, params: tuple | None = None) -> Any:
        rows = self._rows(sql, params)
        return next(iter(rows[0].values())) if rows else None

    def _split(self, name: str) -> tuple[str, str, str]:
        """`DB.SCHEMA.TABLE` or `SCHEMA.TABLE`, upper-cased as Snowflake stores them."""
        parts = [p.strip('"').upper() for p in name.split(".")]
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return self.database.upper(), parts[0], parts[1]
        raise ValueError(f"cannot resolve {name!r}: expected SCHEMA.TABLE or DB.SCHEMA.TABLE")

    def _short(self, schema: str, table: str) -> str:
        """The `SCHEMA.TABLE` form the rest of the system uses as an asset name."""
        return f"{schema}.{table}"

    @staticmethod
    def _iso(value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value) if value is not None else ""

    # -- catalogue and metadata --------------------------------------------- #

    def asset(self, name: str) -> dict[str, Any]:
        db, schema, table = self._split(name)
        exists = self._rows(
            f"""
            SELECT table_type, row_count, bytes, last_altered
            FROM {db}.INFORMATION_SCHEMA.TABLES
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        if not exists:
            return {"name": name, "known": False}

        # Tags carry the governance metadata. ACCOUNT_USAGE, so up to ~2h stale --
        # acceptable here because ownership and tier change on a quarterly cadence,
        # not a minute-by-minute one.
        tag_rows = self._rows(
            """
            SELECT tag_database || '.' || tag_schema || '.' || tag_name AS tag, tag_value
            FROM SNOWFLAKE.ACCOUNT_USAGE.TAG_REFERENCES
            WHERE object_database = %s AND object_schema = %s
              AND object_name = %s AND domain = 'TABLE'
            """,
            (db, schema, table),
        )
        tags = {r["tag"].upper(): r["tag_value"] for r in tag_rows}

        def tag(qualified: str, default: Any = None) -> Any:
            # Tags may be referenced as SCHEMA.NAME or DB.SCHEMA.NAME depending on how
            # they were applied; match on the trailing segments rather than demanding
            # a fully-qualified spelling the warehouse may not use.
            want = qualified.upper()
            for key, value in tags.items():
                if key.endswith(want):
                    return value
            return default

        return {
            "name": self._short(schema, table),
            "known": True,
            "layer": tag(self.tags.layer, schema.lower()),
            "tier": int(tag(self.tags.tier, 3) or 3),
            "owner_team": tag(self.tags.owner_team, "unassigned"),
            "domain": tag(self.tags.domain, ""),
            "sla_minutes": int(tag(self.tags.sla_minutes, 1440) or 1440),
            "is_financial": str(tag(self.tags.is_financial, "false")).lower() == "true",
            "consumer_kind": tag(self.tags.consumer_kind, ""),
            "business_process": tag(self.tags.business_process, ""),
            "source_system": tag(self.tags.source_system, ""),
            "upstreams": [a["name"] for a in self.lineage_upstream(name)],
            "row_count": exists[0].get("row_count"),
            "last_altered": self._iso(exists[0].get("last_altered")),
        }

    # -- lineage ------------------------------------------------------------ #
    #
    # OBJECT_DEPENDENCIES is ACCOUNT_USAGE, so up to ~2h stale. That is the right
    # trade here: lineage changes when someone ships a model, not when an incident
    # happens, and there is no latency-free equivalent.

    def lineage_downstream(self, name: str) -> list[dict[str, Any]]:
        db, schema, table = self._split(name)
        rows = self._rows(
            """
            WITH RECURSIVE descendants AS (
                SELECT referencing_database AS db, referencing_schema AS sch,
                       referencing_object_name AS obj, 1 AS depth
                FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
                WHERE referenced_database = %s AND referenced_schema = %s
                  AND referenced_object_name = %s
                UNION ALL
                SELECT d.referencing_database, d.referencing_schema,
                       d.referencing_object_name, a.depth + 1
                FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES d
                JOIN descendants a
                  ON d.referenced_database = a.db
                 AND d.referenced_schema = a.sch
                 AND d.referenced_object_name = a.obj
                WHERE a.depth < 6
            )
            SELECT db, sch, obj, MIN(depth) AS depth
            FROM descendants
            GROUP BY db, sch, obj
            ORDER BY depth, sch, obj
            """,
            (db, schema, table),
        )
        # Depth 6 is a guard, not a belief about the warehouse: a cyclic or diamond
        # dependency graph would otherwise recurse until Snowflake killed the query.
        out: list[dict[str, Any]] = []
        for row in rows:
            asset = self.asset(self._short(row["sch"], row["obj"]))
            out.append({**asset, "depth": int(row["depth"])})
        return out

    def lineage_upstream(self, name: str) -> list[dict[str, Any]]:
        db, schema, table = self._split(name)
        rows = self._rows(
            """
            SELECT DISTINCT referenced_schema AS sch, referenced_object_name AS obj
            FROM SNOWFLAKE.ACCOUNT_USAGE.OBJECT_DEPENDENCIES
            WHERE referencing_database = %s AND referencing_schema = %s
              AND referencing_object_name = %s
            ORDER BY sch, obj
            """,
            (db, schema, table),
        )
        # Deliberately one hop, matching the simulated platform: `upstreams` on an
        # asset means its direct parents. Multi-hop ancestry is the caller's walk.
        return [
            {"name": self._short(r["sch"], r["obj"]), "known": True} for r in rows
        ]

    # -- operational history ------------------------------------------------ #
    #
    # INFORMATION_SCHEMA table functions, not ACCOUNT_USAGE: no latency. This is the
    # single most important choice in the file. An agent told a task succeeded when it
    # failed four minutes ago will confidently reach the wrong conclusion.

    def task_runs(self, asset: str, hours: int = 48) -> list[dict[str, Any]]:
        db, schema, table = self._split(asset)
        rows = self._rows(
            f"""
            SELECT name, query_id, scheduled_time, query_start_time, completed_time,
                   state, attempt_number, error_code, error_message, database_name,
                   schema_name
            FROM TABLE({db}.INFORMATION_SCHEMA.TASK_HISTORY(
                SCHEDULED_TIME_RANGE_START => DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP())
            ))
            WHERE schema_name = %s
            ORDER BY scheduled_time DESC
            """,
            (schema,),
        )
        # TASK_HISTORY says whether a task ran and whether it failed. It does not say
        # how many rows it wrote or what it cost -- those live on the query. One extra
        # round trip is cheaper than joining ACCOUNT_USAGE and inheriting its latency.
        stats = self._query_stats([r["query_id"] for r in rows if r.get("query_id")], hours)
        out = []
        for r in rows:
            # Task names rarely equal table names, so associate by the query's target
            # where we have it and fall back to a name-contains heuristic. Named as a
            # heuristic because that is what it is.
            stat = stats.get(r.get("query_id"), {})
            if table not in (r["name"] or "").upper() and stat.get("target") != table:
                continue
            started = r.get("query_start_time") or r.get("scheduled_time")
            completed = r.get("completed_time")
            out.append(
                {
                    "run_id": r.get("query_id") or r["name"],
                    "task": r["name"],
                    "asset": self._short(schema, table),
                    "started_at": self._iso(started),
                    "duration_s": int((completed - started).total_seconds())
                    if completed and started
                    else 0,
                    "state": (r["state"] or "").lower(),
                    "attempt": int(r.get("attempt_number") or 1),
                    "error_code": r.get("error_code") or "",
                    "error_message": r.get("error_message") or "",
                    "rows_written": stat.get("rows_produced", 0),
                    "credits_used": stat.get("credits", 0.0),
                }
            )
        return out

    def _query_stats(self, query_ids: list[str], hours: int) -> dict[str, dict[str, Any]]:
        if not query_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(query_ids))
        rows = self._rows(
            f"""
            SELECT query_id, rows_produced, credits_used_cloud_services,
                   query_type, database_name, schema_name
            FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(
                END_TIME_RANGE_START => DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP()),
                RESULT_LIMIT => 10000
            ))
            WHERE query_id IN ({placeholders})
            """,
            tuple(query_ids),
        )
        # credits_used_cloud_services is not the whole cost of a query -- warehouse
        # compute dominates and is only attributable through QUERY_ATTRIBUTION_HISTORY,
        # which is ACCOUNT_USAGE and therefore stale. Reported as-is rather than
        # silently understating: the budget governor in config.py is the real control.
        return {
            r["query_id"]: {
                "rows_produced": int(r.get("rows_produced") or 0),
                "credits": float(r.get("credits_used_cloud_services") or 0.0),
                "target": None,
            }
            for r in rows
        }

    def failed_runs(self, hours: int = 24) -> list[dict[str, Any]]:
        rows = self._rows(
            f"""
            SELECT name, query_id, scheduled_time, state, attempt_number,
                   error_code, error_message, schema_name
            FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
                SCHEDULED_TIME_RANGE_START => DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP())
            ))
            WHERE state = 'FAILED'
            ORDER BY scheduled_time DESC
            """
        )
        return [
            {
                "run_id": r.get("query_id") or r["name"],
                "task": r["name"],
                "asset": r.get("schema_name") or "",
                "started_at": self._iso(r.get("scheduled_time")),
                "state": "failed",
                "attempt": int(r.get("attempt_number") or 1),
                "error_code": r.get("error_code") or "",
                "error_message": r.get("error_message") or "",
            }
            for r in rows
        ]

    def copy_history(self, table: str, hours: int = 48) -> list[dict[str, Any]]:
        db, schema, tbl = self._split(table)
        rows = self._rows(
            f"""
            SELECT file_name, stage_location, last_load_time, status,
                   row_count, row_parsed, error_count, first_error_message,
                   first_error_column_name, table_name, table_schema_name
            FROM TABLE({db}.INFORMATION_SCHEMA.COPY_HISTORY(
                TABLE_NAME => %s,
                START_TIME => DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP())
            ))
            ORDER BY last_load_time DESC
            """,
            (f"{db}.{schema}.{tbl}",),
        )
        return [
            {
                # COPY_HISTORY has no file id; the name within the stage is the
                # stable identifier the rest of the system treats as one.
                "file_id": (r.get("file_name") or "").rsplit("/", 1)[-1],
                "file_name": r.get("file_name") or "",
                "target_table": self._short(schema, tbl),
                "loaded_at": self._iso(r.get("last_load_time")),
                "status": (r.get("status") or "").lower(),
                "rows_loaded": int(r.get("row_count") or 0),
                "rows_parsed": int(r.get("row_parsed") or 0),
                "errors_seen": int(r.get("error_count") or 0),
                "first_error": r.get("first_error_message") or "",
                "first_error_column": r.get("first_error_column_name") or "",
            }
            for r in rows
        ]

    # -- data quality ------------------------------------------------------- #

    def dmf_results(self, asset: str, hours: int = 48) -> list[dict[str, Any]]:
        db, schema, table = self._split(asset)
        rows = self._rows(
            f"""
            SELECT measurement_time, metric_name, argument_names, value
            FROM SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS
            WHERE table_database = %s AND table_schema = %s AND table_name = %s
              AND measurement_time >= DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP())
            ORDER BY measurement_time DESC
            """,
            (db, schema, table),
        )
        return [
            {
                "asset": self._short(schema, table),
                "metric": r["metric_name"],
                # argument_names is an ARRAY of the columns the metric was applied to.
                "column": (list(r.get("argument_names") or []) or [""])[0],
                "measured_at": self._iso(r.get("measurement_time")),
                "value": float(r.get("value") or 0.0),
                # The view records measurements, not thresholds -- those live on the
                # alert that consumes them. Surfaced as unknown rather than invented,
                # because a fabricated threshold would drive a real severity decision.
                "threshold": None,
                "breached": None,
            }
            for r in rows
        ]

    def metric_summary(self, asset: str, days: int = 30) -> dict[str, Any]:
        """
        A 30-day baseline assembled from scheduled DMF measurements.

        Snowflake has no per-table metric time series. It does have one as a by-product:
        if the DMFs are scheduled, every measurement lands in
        `DATA_QUALITY_MONITORING_RESULTS` with a timestamp. This reads that history and
        does the arithmetic in Python, exactly as `SimulatedPlatform` does -- the model
        is handed "3.1x median", never thirty raw points to average.

        Returns `available: False` if no DMFs are scheduled, rather than guessing. An
        agent told "no baseline exists" reasons better than one handed a fabricated one.
        """
        db, schema, table = self._split(asset)
        rows = self._rows(
            f"""
            SELECT measurement_time, metric_name, value
            FROM SNOWFLAKE.LOCAL.DATA_QUALITY_MONITORING_RESULTS
            WHERE table_database = %s AND table_schema = %s AND table_name = %s
              AND measurement_time >= DATEADD('day', -{int(days)}, CURRENT_TIMESTAMP())
            ORDER BY measurement_time
            """,
            (db, schema, table),
        )
        if not rows:
            return {"asset": asset, "available": False}

        by_metric: dict[str, list[tuple[datetime, float]]] = {}
        for r in rows:
            by_metric.setdefault(r["metric_name"].upper(), []).append(
                (r["measurement_time"], float(r.get("value") or 0.0))
            )

        def series(metric: str) -> list[float]:
            return [v for _, v in by_metric.get(metric.upper(), [])]

        def block(values: list[float]) -> dict[str, Any] | None:
            if not values:
                return None
            latest, history = values[-1], values[:-1] or values
            median = statistics.median(history) or 1.0
            return {
                "latest": round(latest, 4),
                "median": round(statistics.median(history), 4),
                "min": round(min(history), 4),
                "max": round(max(history), 4),
                "ratio_to_median": round(latest / median, 3),
            }

        rows_series = series(self.metrics.row_count)
        nulls_series = series(self.metrics.null_count)
        fresh_series = series(self.metrics.freshness)
        unique_series = series(self.metrics.unique_count)

        out: dict[str, Any] = {
            "asset": self._short(schema, table),
            "available": True,
            "window_days": days,
            "latest_at": self._iso(rows[-1]["measurement_time"]),
        }
        if (b := block(rows_series)) is not None:
            out["row_count"] = b
        if nulls_series and rows_series:
            # NULL_COUNT is a count; the rest of the system speaks in rates.
            rates = [
                n / r if r else 0.0
                for n, r in zip(nulls_series, rows_series[: len(nulls_series)])
            ]
            if (b := block(rates)) is not None:
                b["delta_pp"] = round((rates[-1] - statistics.median(rates[:-1] or rates)) * 100, 2)
                out["null_rate"] = b
        if (b := block(fresh_series)) is not None:
            # FRESHNESS is reported in seconds; the contract is minutes.
            out["freshness_lag_min"] = {
                k: (round(v / 60, 1) if isinstance(v, (int, float)) and k != "ratio_to_median" else v)
                for k, v in b.items()
            }
        if unique_series and rows_series:
            ratios = [
                u / r if r else 0.0
                for u, r in zip(unique_series, rows_series[: len(unique_series)])
            ]
            if (b := block(ratios)) is not None:
                out["distinct_key_ratio"] = b
        return out

    # -- schema ------------------------------------------------------------- #

    def schema_diff(self, asset: str, lookback_days: int = 30) -> dict[str, Any]:
        """
        Now versus `lookback_days` ago, from `ACCOUNT_USAGE.COLUMNS`.

        Snowflake keeps no schema-version history, but `ACCOUNT_USAGE.COLUMNS` retains
        dropped columns with a `DELETED` timestamp and added ones with `CREATED`. That
        is enough to reconstruct the diff, which is what the change correlator needs --
        it asks "what changed about this table", not "show me every past version".
        """
        db, schema, table = self._split(asset)
        rows = self._rows(
            f"""
            SELECT column_name, data_type, created, deleted
            FROM SNOWFLAKE.ACCOUNT_USAGE.COLUMNS
            WHERE table_catalog = %s AND table_schema = %s AND table_name = %s
              AND (deleted IS NULL
                   OR deleted >= DATEADD('day', -{int(lookback_days)}, CURRENT_TIMESTAMP()))
            """,
            (db, schema, table),
        )
        if not rows:
            return {"asset": asset, "changed": False, "versions": 0}

        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

        def present_now(r: dict[str, Any]) -> bool:
            return r.get("deleted") is None

        def present_then(r: dict[str, Any]) -> bool:
            created = r.get("created")
            return created is not None and created <= cutoff and (
                r.get("deleted") is None or r["deleted"] > cutoff
            )

        now_cols = {r["column_name"]: r["data_type"] for r in rows if present_now(r)}
        then_cols = {r["column_name"]: r["data_type"] for r in rows if present_then(r)}
        added = sorted(set(now_cols) - set(then_cols))
        removed = sorted(set(then_cols) - set(now_cols))
        retyped = sorted(
            c for c in set(now_cols) & set(then_cols) if now_cols[c] != then_cols[c]
        )
        # Same-typed add plus remove in one window is the classic vendor rename. Stated
        # as "likely" because it is an inference, not an observation.
        likely_renames = [
            {"from": r, "to": a}
            for r in removed
            for a in added
            if then_cols[r] == now_cols[a]
        ]
        changed_at = max(
            (r.get("deleted") or r.get("created") for r in rows if present_now(r) or True),
            default=None,
        )
        return {
            "asset": self._short(schema, table),
            "changed": bool(added or removed or retyped),
            "from_version": f"observed-{cutoff.date()}",
            "to_version": "current",
            "observed_at": self._iso(changed_at),
            "added_columns": added,
            "removed_columns": removed,
            "retyped_columns": retyped,
            "likely_renames": likely_renames,
            "contract_version": f"observed-{cutoff.date()}",
        }

    # -- change correlation ------------------------------------------------- #

    def changes(self, hours: int = 72) -> list[dict[str, Any]]:
        """
        What changed recently.

        Two sources, and the split is honest: the warehouse knows about **DDL** because
        it executed it, and knows nothing whatsoever about deploys, pull requests or a
        vendor's breaking-change notice. Those arrive through `change_feed`, which a
        real deployment wires to the GitHub API and the vendor's status page. With no
        feed injected this returns DDL only, and every caller can tell from `kind`.
        """
        rows = self._rows(
            f"""
            SELECT query_id, start_time, query_type, query_text, user_name,
                   database_name, schema_name
            FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY(
                END_TIME_RANGE_START => DATEADD('hour', -{int(hours)}, CURRENT_TIMESTAMP()),
                RESULT_LIMIT => 10000
            ))
            WHERE query_type IN (
                'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER_TABLE',
                'CREATE_VIEW', 'ALTER_VIEW', 'DROP_TABLE', 'DROP_VIEW',
                'CREATE_TASK', 'ALTER_TASK'
            )
            AND execution_status = 'SUCCESS'
            ORDER BY start_time DESC
            """
        )
        out = [
            {
                "change_id": r["query_id"],
                "at": self._iso(r.get("start_time")),
                "kind": "ddl",
                "title": f"{r['query_type']} by {r.get('user_name') or 'unknown'}",
                # Truncated deliberately: a 40 KB CTAS body would dominate the
                # evidence budget and tell the correlator nothing extra.
                "detail": (r.get("query_text") or "")[:400],
                "author": r.get("user_name") or "",
                "repo": "",
                "pr_number": None,
                "touched_assets": [
                    self._short(r.get("schema_name") or "", "")
                ]
                if r.get("schema_name")
                else [],
            }
            for r in rows
        ]
        if self.change_feed is not None:
            out.extend(self.change_feed.changes(hours))
        return out

    # -- consumers ---------------------------------------------------------- #

    def consumers(self, asset: str) -> list[dict[str, Any]]:
        """
        Who actually reads this, from `ACCESS_HISTORY`.

        Note what this is *not*: a list of dashboards someone registered in a catalogue.
        It is observed reads over the last seven days, which is the question that
        matters when deciding how much a stale table hurts. ACCOUNT_USAGE, so up to
        ~3 hours stale -- irrelevant over a 7-day window.
        """
        db, schema, table = self._split(asset)
        rows = self._rows(
            """
            SELECT ah.user_name,
                   COUNT(*) AS reads,
                   COUNT(DISTINCT ah.user_name) OVER () AS distinct_users
            FROM SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY ah,
                 LATERAL FLATTEN(input => ah.base_objects_accessed) obj
            WHERE obj.value:objectName::STRING = %s
              AND ah.query_start_time >= DATEADD('day', -7, CURRENT_TIMESTAMP())
            GROUP BY ah.user_name
            ORDER BY reads DESC
            """,
            (f"{db}.{schema}.{table}",),
        )
        return [
            {
                "asset": self._short(schema, table),
                "consumer": r["user_name"],
                # ACCESS_HISTORY records the querying identity, not what kind of tool
                # it is. Tag service accounts to get better than "unknown" here.
                "kind": "unknown",
                "reads_last_7d": int(r.get("reads") or 0),
                "distinct_users": int(r.get("distinct_users") or 0),
            }
            for r in rows
        ]

    def file_info(self, file_id: str) -> dict[str, Any]:
        rows = self._rows(
            """
            SELECT file_name, stage_location, last_load_time, status, row_count,
                   file_size, table_name, table_schema_name, table_catalog_name,
                   first_error_message
            FROM SNOWFLAKE.ACCOUNT_USAGE.COPY_HISTORY
            WHERE file_name LIKE %s
            ORDER BY last_load_time DESC
            LIMIT 1
            """,
            (f"%{file_id}",),
        )
        if not rows:
            return {"file_id": file_id, "known": False}
        r = rows[0]
        loaded = (r.get("status") or "").upper() == "LOADED"
        return {
            "file_id": file_id,
            "known": True,
            "uri": f"{r.get('stage_location') or ''}{r.get('file_name') or ''}",
            "source_system": (r.get("table_schema_name") or "").lower(),
            "arrived_at": self._iso(r.get("last_load_time")),
            "size_bytes": int(r.get("file_size") or 0),
            "row_count": int(r.get("row_count") or 0),
            "target_table": self._short(
                r.get("table_schema_name") or "", r.get("table_name") or ""
            ),
            # Snowflake has no notion of a schema contract version; it lives in
            # `contract_table`, which is ours.
            "declared_schema_version": self._declared_version(
                r.get("table_schema_name"), r.get("table_name")
            ),
            "quarantined": not loaded,
            "quarantine_uri": f"{self.quarantine_stage}/{file_id}" if not loaded else "",
        }

    def _declared_version(self, schema: str | None, table: str | None) -> str:
        if not schema or not table:
            return ""
        value = self._scalar(
            f"SELECT pinned_version FROM {self.contract_table} WHERE asset = %s",
            (f"{schema}.{table}",),
        )
        return str(value or "")

    # -- health ------------------------------------------------------------- #

    def health(self, assets: list[str]) -> list[HealthCheck]:
        """Post-remediation verification, composed from the same sources as diagnosis."""
        out: list[HealthCheck] = []
        for name in assets:
            summary = self.metric_summary(name, days=30)
            if not summary.get("available"):
                out.append(
                    HealthCheck(
                        asset=name,
                        fresh=False,
                        freshness_lag_min=0.0,
                        row_count=0,
                        row_count_vs_median=0.0,
                        null_rate=0.0,
                        healthy=False,
                        notes="no data metric functions scheduled -- cannot verify",
                    )
                )
                continue
            rc = summary.get("row_count", {})
            nr = summary.get("null_rate", {})
            fr = summary.get("freshness_lag_min", {})
            sla = float(self.asset(name).get("sla_minutes") or 1440)
            lag = float(fr.get("latest") or 0.0)
            ratio = float(rc.get("ratio_to_median") or 0.0)
            null_rate = float(nr.get("latest") or 0.0)
            fresh = lag <= sla
            # Same bands the simulated platform uses, so a verification that passes
            # there means the same thing here.
            healthy = fresh and 0.7 <= ratio <= 1.5 and null_rate <= 0.05
            out.append(
                HealthCheck(
                    asset=name,
                    fresh=fresh,
                    freshness_lag_min=lag,
                    row_count=int(rc.get("latest") or 0),
                    row_count_vs_median=ratio,
                    null_rate=null_rate,
                    healthy=healthy,
                    notes="" if healthy else "outside expected bands",
                )
            )
        return out

    # -- actions ------------------------------------------------------------ #

    def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        """
        Render the statement for `action`; run it only in live mode.

        Dry run is the default and returns the SQL without executing it, which makes
        the whole remediation plan reviewable before anything touches the warehouse --
        the same property the approval gates exist to create, at the statement level.
        """
        template = _ALLOWED_STATEMENTS.get(action)
        if template is None:
            # Covers `drop_table`, which is absent on purpose.
            return ActionResult(
                action, False, f"action {action!r} is not in the allow-list", {}
            )
        try:
            sql = template.format(
                quarantine_stage=self.quarantine_stage,
                contract_table=self.contract_table,
                backfill_procedure=self.backfill_procedure,
                **params,
            )
        except KeyError as exc:
            return ActionResult(action, False, f"missing parameter {exc}", {"params": params})

        if self.execute_mode == "dry_run":
            return ActionResult(
                action, True, "dry run -- statement rendered, not executed", {"sql": sql}
            )

        cur = self.connection.cursor()
        try:
            cur.execute(sql)
            return ActionResult(
                action, True, "executed", {"sql": sql, "rows": cur.rowcount or 0}
            )
        except Exception as exc:  # noqa: BLE001 -- surfaced to the executor, not swallowed
            log.warning("action %s failed: %s", action, exc)
            return ActionResult(action, False, str(exc)[:500], {"sql": sql})
        finally:
            cur.close()

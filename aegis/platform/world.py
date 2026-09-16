"""
The Northwind data platform: a deterministic simulation of a Snowflake warehouse.

This exists so the whole system can be demonstrated and regression-tested without a
live account, and so the eval harness has ground truth to score against. It models
the objects the agents actually query in production:

* a lineage graph (Snowflake Horizon / OBJECT_DEPENDENCIES)
* task run history (TASK_HISTORY, SERVERLESS_TASK_HISTORY)
* file ingestion history (COPY_HISTORY, Snowpipe)
* data-quality metric results (Data Metric Functions, DATA_QUALITY_MONITORING_RESULTS)
* per-asset metric time series (row counts, null rates, freshness lag, credits)
* a schema registry with versions
* a change log (dbt deploys, config edits, vendor notices)
* consumer access (ACCESS_HISTORY -- who actually reads each asset)

The agents reach this only through the `PlatformClient` protocol in client.py, never
directly. A Snowflake-backed implementation of that same protocol would drop in without
the agent layer changing -- but it is not written yet, so the simulation is currently
the only implementation. It is a test fixture standing in for a real backend, not a
shortcut around one.
"""

from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Object model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Asset:
    name: str
    layer: str  # raw | staging | intermediate | mart | consumer
    tier: int  # 1 = business critical, 3 = best effort
    owner_team: str
    domain: str
    upstreams: tuple[str, ...] = ()
    sla_minutes: int | None = None
    is_financial: bool = False
    consumer_kind: str | None = None  # dashboard | ml_model | api | export
    business_process: str = ""
    source_system: str | None = None


@dataclass
class TaskRun:
    run_id: str
    task_name: str
    target_asset: str
    started_at: datetime
    ended_at: datetime
    state: str  # SUCCEEDED | FAILED | SKIPPED | RUNNING
    error_code: str = ""
    error_message: str = ""
    attempt: int = 1
    rows_written: int = 0
    credits_used: float = 0.0
    query_id: str = ""


@dataclass
class CopyEvent:
    file_id: str
    file_name: str
    target_table: str
    loaded_at: datetime
    status: str  # LOADED | LOAD_FAILED | PARTIALLY_LOADED
    row_count: int = 0
    row_parsed: int = 0
    errors_seen: int = 0
    first_error: str = ""
    first_error_column: str = ""
    pipe_name: str = ""


@dataclass
class DMFResult:
    asset: str
    metric_name: str  # NULL_COUNT, ROW_COUNT, FRESHNESS, DUPLICATE_COUNT, ...
    column_name: str | None
    measured_at: datetime
    value: float
    threshold: float | None = None
    breached: bool = False


@dataclass
class MetricPoint:
    asset: str
    at: datetime
    row_count: int
    null_rate: float
    freshness_lag_min: float
    distinct_key_ratio: float
    credits: float


@dataclass
class SchemaVersion:
    asset: str
    version: str
    effective_from: datetime
    columns: tuple[tuple[str, str], ...]  # (name, type)
    source: str = "contract"


@dataclass
class ChangeEvent:
    change_id: str
    at: datetime
    kind: str  # deploy | config | vendor_notice | infra | schema_registry
    title: str
    detail: str
    author: str
    repo: str = ""
    pr_number: int | None = None
    touched_assets: tuple[str, ...] = ()


@dataclass
class ConsumerAccess:
    asset: str
    consumer: str
    consumer_kind: str
    reads_last_7d: int
    distinct_users: int


@dataclass
class StoredFile:
    file_id: str
    bucket: str
    key: str
    source_system: str
    arrived_at: datetime
    size_bytes: int
    row_count: int
    target_table: str
    declared_schema_version: str | None = None
    quarantined: bool = False
    quarantine_uri: str = ""


# --------------------------------------------------------------------------- #
# Lineage definition
# --------------------------------------------------------------------------- #

_T = "data-platform"
_F = "finance-data"
_G = "growth-data"
_S = "support-data"
_M = "ml-platform"

ASSETS: tuple[Asset, ...] = (
    # ---- raw (file landing zones) ---------------------------------------- #
    Asset("RAW.STRIPE_CHARGES", "raw", 1, _T, "finance", (), 60, True,
          source_system="stripe", business_process="Revenue recognition"),
    Asset("RAW.STRIPE_SUBSCRIPTIONS", "raw", 1, _T, "finance", (), 60, True,
          source_system="stripe", business_process="Revenue recognition"),
    Asset("RAW.SHOPIFY_ORDERS", "raw", 1, _T, "commerce", (), 60, False,
          source_system="shopify", business_process="Order fulfilment"),
    Asset("RAW.SHOPIFY_CUSTOMERS", "raw", 2, _T, "commerce", (), 180, False,
          source_system="shopify"),
    Asset("RAW.SALESFORCE_ACCOUNTS", "raw", 2, _T, "crm", (), 240, False,
          source_system="salesforce"),
    Asset("RAW.ZENDESK_TICKETS", "raw", 3, _S, "support", (), 240, False,
          source_system="zendesk"),
    Asset("RAW.APP_EVENTS", "raw", 2, _T, "product", (), 30, False,
          source_system="segment"),
    Asset("RAW.FX_RATES", "raw", 1, _F, "finance", (), 120, True,
          source_system="openfx", business_process="Multi-currency revenue"),
    Asset("RAW.AD_SPEND", "raw", 2, _G, "marketing", (), 360, False,
          source_system="adbridge"),
    # ---- staging ---------------------------------------------------------- #
    Asset("STG.PAYMENTS", "staging", 1, _T, "finance", ("RAW.STRIPE_CHARGES",), 90, True),
    Asset("STG.SUBSCRIPTIONS", "staging", 1, _T, "finance",
          ("RAW.STRIPE_SUBSCRIPTIONS",), 90, True),
    Asset("STG.ORDERS", "staging", 1, _T, "commerce", ("RAW.SHOPIFY_ORDERS",), 90),
    Asset("STG.CUSTOMERS", "staging", 2, _T, "commerce", ("RAW.SHOPIFY_CUSTOMERS",), 240),
    Asset("STG.ACCOUNTS", "staging", 2, _T, "crm", ("RAW.SALESFORCE_ACCOUNTS",), 300),
    Asset("STG.TICKETS", "staging", 3, _S, "support", ("RAW.ZENDESK_TICKETS",), 300),
    Asset("STG.EVENTS", "staging", 2, _T, "product", ("RAW.APP_EVENTS",), 60),
    Asset("STG.FX_RATES", "staging", 1, _F, "finance", ("RAW.FX_RATES",), 150, True),
    Asset("STG.AD_SPEND", "staging", 2, _G, "marketing", ("RAW.AD_SPEND",), 420),
    # ---- intermediate ----------------------------------------------------- #
    Asset("INT.CUSTOMER_360", "intermediate", 2, _T, "commerce",
          ("STG.CUSTOMERS", "STG.ACCOUNTS", "STG.TICKETS"), 360),
    Asset("INT.ORDER_ENRICHED", "intermediate", 1, _T, "commerce",
          ("STG.ORDERS", "STG.CUSTOMERS", "STG.PAYMENTS"), 150),
    Asset("INT.REVENUE_EVENTS", "intermediate", 1, _F, "finance",
          ("STG.PAYMENTS", "STG.SUBSCRIPTIONS", "STG.FX_RATES"), 150, True,
          business_process="Revenue recognition"),
    # ---- marts ------------------------------------------------------------ #
    Asset("MART.DAILY_REVENUE", "mart", 1, _F, "finance",
          ("INT.REVENUE_EVENTS", "STG.FX_RATES"), 180, True,
          business_process="Daily revenue reporting"),
    Asset("MART.ARR_SNAPSHOT", "mart", 1, _F, "finance",
          ("INT.REVENUE_EVENTS", "STG.SUBSCRIPTIONS"), 240, True,
          business_process="ARR and board reporting"),
    Asset("MART.CHURN_FEATURES", "mart", 2, _M, "product",
          ("INT.CUSTOMER_360", "INT.REVENUE_EVENTS", "STG.EVENTS"), 480),
    Asset("MART.MARKETING_ROI", "mart", 2, _G, "marketing",
          ("STG.AD_SPEND", "INT.ORDER_ENRICHED"), 480),
    Asset("MART.SUPPORT_HEALTH", "mart", 3, _S, "support",
          ("STG.TICKETS", "INT.CUSTOMER_360"), 720),
    Asset("MART.EXEC_KPIS", "mart", 1, _F, "finance",
          ("MART.DAILY_REVENUE", "MART.ARR_SNAPSHOT", "MART.CHURN_FEATURES"), 300, True,
          business_process="Executive reporting"),
    # ---- consumer surfaces ------------------------------------------------ #
    Asset("DASH.EXEC_DAILY", "consumer", 1, _F, "finance", ("MART.EXEC_KPIS",), 360, True,
          consumer_kind="dashboard", business_process="Executive reporting"),
    Asset("DASH.FINANCE_CLOSE", "consumer", 1, _F, "finance",
          ("MART.DAILY_REVENUE", "MART.ARR_SNAPSHOT"), 360, True,
          consumer_kind="dashboard", business_process="Month-end close"),
    Asset("DASH.GROWTH", "consumer", 2, _G, "marketing",
          ("MART.MARKETING_ROI", "MART.CHURN_FEATURES"), 720,
          consumer_kind="dashboard", business_process="Growth reporting"),
    Asset("ML.CHURN_MODEL", "consumer", 2, _M, "product", ("MART.CHURN_FEATURES",), 1440,
          consumer_kind="ml_model", business_process="Churn prevention campaigns"),
    Asset("API.CUSTOMER_HEALTH", "consumer", 2, _S, "support",
          ("INT.CUSTOMER_360", "MART.SUPPORT_HEALTH"), 720,
          consumer_kind="api", business_process="Customer success workflows"),
    Asset("EXPORT.NETSUITE_REVENUE", "consumer", 1, _F, "finance",
          ("MART.DAILY_REVENUE",), 420, True,
          consumer_kind="export", business_process="Statutory financial reporting"),
)

SCHEMA_CONTRACTS: dict[str, tuple[tuple[str, str], ...]] = {
    "RAW.STRIPE_CHARGES": (
        ("charge_id", "VARCHAR"),
        ("customer_id", "VARCHAR"),
        ("amount_minor", "NUMBER"),
        ("currency_code", "VARCHAR"),
        ("status", "VARCHAR"),
        ("created_at", "TIMESTAMP_NTZ"),
    ),
    "RAW.FX_RATES": (
        ("rate_date", "DATE"),
        ("base_currency", "VARCHAR"),
        ("quote_currency", "VARCHAR"),
        ("rate", "NUMBER"),
    ),
    "RAW.SHOPIFY_ORDERS": (
        ("order_id", "VARCHAR"),
        ("customer_id", "VARCHAR"),
        ("order_total", "NUMBER"),
        ("currency", "VARCHAR"),
        ("placed_at", "TIMESTAMP_NTZ"),
    ),
    "RAW.APP_EVENTS": (
        ("event_id", "VARCHAR"),
        ("user_id", "VARCHAR"),
        ("event_name", "VARCHAR"),
        ("occurred_at", "TIMESTAMP_NTZ"),
    ),
    "RAW.AD_SPEND": (
        ("spend_date", "DATE"),
        ("channel", "VARCHAR"),
        ("campaign_id", "VARCHAR"),
        ("spend_usd", "NUMBER"),
    ),
}

TEAM_CONTACTS: dict[str, str] = {
    _T: "data-platform@northwind.example",
    _F: "finance-data@northwind.example",
    _G: "growth-data@northwind.example",
    _S: "support-data@northwind.example",
    _M: "ml-platform@northwind.example",
}


# --------------------------------------------------------------------------- #
# The world
# --------------------------------------------------------------------------- #


@dataclass
class World:
    """A deterministic snapshot of the platform at `now`, seeded for reproducibility."""

    now: datetime
    seed: int = 20260917

    assets: dict[str, Asset] = field(default_factory=dict)
    task_runs: list[TaskRun] = field(default_factory=list)
    copy_events: list[CopyEvent] = field(default_factory=list)
    dmf_results: list[DMFResult] = field(default_factory=list)
    metrics: list[MetricPoint] = field(default_factory=list)
    schema_versions: list[SchemaVersion] = field(default_factory=list)
    changes: list[ChangeEvent] = field(default_factory=list)
    consumer_access: list[ConsumerAccess] = field(default_factory=list)
    files: dict[str, StoredFile] = field(default_factory=dict)
    action_log: list[dict[str, object]] = field(default_factory=list)

    # -- construction ------------------------------------------------------- #

    @classmethod
    def build(cls, now: datetime | None = None, seed: int = 20260917) -> World:
        now = now or datetime(2026, 9, 17, 6, 40, tzinfo=timezone.utc)
        world = cls(now=now, seed=seed)
        world.assets = {a.name: a for a in ASSETS}
        rng = random.Random(seed)
        world._seed_schema_versions(now)
        world._seed_metrics(rng, now)
        world._seed_task_runs(rng, now)
        world._seed_copy_events(rng, now)
        world._seed_changes(now)
        world._seed_consumer_access(rng)
        return world

    # -- seeding ------------------------------------------------------------ #

    def _seed_schema_versions(self, now: datetime) -> None:
        for asset, cols in SCHEMA_CONTRACTS.items():
            self.schema_versions.append(
                SchemaVersion(asset, "v3", now - timedelta(days=95), cols)
            )

    def _baseline(self, asset: Asset) -> tuple[int, float, float]:
        base_rows = {
            "raw": 180_000,
            "staging": 176_000,
            "intermediate": 210_000,
            "mart": 42_000,
            "consumer": 8_000,
        }[asset.layer]
        scale = {1: 1.0, 2: 0.6, 3: 0.3}[asset.tier]
        return int(base_rows * scale), 0.004, 0.9985

    def _seed_metrics(self, rng: random.Random, now: datetime) -> None:
        for asset in self.assets.values():
            rows, null_rate, distinct = self._baseline(asset)
            for day_offset in range(30, 0, -1):
                at = now - timedelta(days=day_offset)
                drift = 1.0 + rng.uniform(-0.03, 0.035)
                weekend = 0.72 if at.weekday() >= 5 else 1.0
                self.metrics.append(
                    MetricPoint(
                        asset=asset.name,
                        at=at,
                        row_count=int(rows * drift * weekend),
                        null_rate=max(0.0, null_rate + rng.uniform(-0.002, 0.003)),
                        freshness_lag_min=rng.uniform(4, 22),
                        distinct_key_ratio=min(1.0, distinct + rng.uniform(-0.002, 0.001)),
                        credits=round(rng.uniform(0.4, 2.6) * (4 - asset.tier), 3),
                    )
                )

    def _seed_task_runs(self, rng: random.Random, now: datetime) -> None:
        for asset in self.assets.values():
            if asset.layer == "raw":
                continue
            for day_offset in range(7, 0, -1):
                started = now - timedelta(days=day_offset, minutes=rng.randint(5, 50))
                duration = timedelta(seconds=rng.randint(45, 900))
                self.task_runs.append(
                    TaskRun(
                        run_id=f"run_{asset.name.lower().replace('.', '_')}_{day_offset}",
                        task_name=f"TASK_BUILD_{asset.name.split('.')[-1]}",
                        target_asset=asset.name,
                        started_at=started,
                        ended_at=started + duration,
                        state="SUCCEEDED",
                        rows_written=self._baseline(asset)[0],
                        credits_used=round(rng.uniform(0.2, 1.9), 3),
                        query_id=f"{rng.getrandbits(32):08x}-task-{day_offset:04d}",
                    )
                )

    def _seed_copy_events(self, rng: random.Random, now: datetime) -> None:
        for asset in self.assets.values():
            if asset.layer != "raw":
                continue
            for day_offset in range(7, 0, -1):
                loaded = now - timedelta(days=day_offset, minutes=rng.randint(10, 60))
                rows = self._baseline(asset)[0]
                self.copy_events.append(
                    CopyEvent(
                        file_id=f"hist_{asset.name.lower()}_{day_offset}",
                        file_name=(
                            f"{asset.source_system}/"
                            f"{loaded.date().isoformat()}/part-0000.parquet"
                        ),
                        target_table=asset.name,
                        loaded_at=loaded,
                        status="LOADED",
                        row_count=rows,
                        row_parsed=rows,
                        pipe_name=f"PIPE_{asset.name.split('.')[-1]}",
                    )
                )

    def _seed_changes(self, now: datetime) -> None:
        self.changes.extend(
            [
                ChangeEvent(
                    "chg_routine_1",
                    now - timedelta(days=6),
                    "deploy",
                    "Add support ticket SLA fields",
                    "Extends STG.TICKETS with sla_breached flag.",
                    "priya.raman",
                    repo="northwind-dbt",
                    pr_number=1841,
                    touched_assets=("STG.TICKETS", "MART.SUPPORT_HEALTH"),
                ),
                ChangeEvent(
                    "chg_routine_2",
                    now - timedelta(days=4),
                    "config",
                    "Increase warehouse size for nightly batch",
                    "COMPUTE_WH resized from S to M between 01:00-04:00 UTC.",
                    "ops.automation",
                ),
                ChangeEvent(
                    "chg_routine_3",
                    now - timedelta(days=2),
                    "deploy",
                    "Refactor churn feature window to 90d",
                    "Feature window widened for the churn model.",
                    "sam.oyelaran",
                    repo="northwind-dbt",
                    pr_number=1856,
                    touched_assets=("MART.CHURN_FEATURES",),
                ),
            ]
        )

    def _seed_consumer_access(self, rng: random.Random) -> None:
        for asset in self.assets.values():
            if asset.layer != "consumer":
                continue
            self.consumer_access.append(
                ConsumerAccess(
                    asset=asset.name,
                    consumer=asset.name.split(".")[-1].lower(),
                    consumer_kind=asset.consumer_kind or "unknown",
                    reads_last_7d=rng.randint(40, 2400),
                    distinct_users=rng.randint(3, 180),
                )
            )

    # -- lineage ------------------------------------------------------------ #

    def downstream_depths(self, asset: str, max_depth: int = 10) -> dict[str, int]:
        """
        Downstream closure with hop distance from the failing asset.

        Distance matters for severity. A tier-1 dashboard one hop away is reading the
        broken data almost directly; the same dashboard five hops away is reading
        something aggregated, joined and filtered several times over, and the fault may
        barely register in it. Reachability is not materiality, and a blast radius that
        counts them equally makes every incident look like a SEV1.
        """
        children: dict[str, list[str]] = {}
        for a in self.assets.values():
            for up in a.upstreams:
                children.setdefault(up, []).append(a.name)
        depths: dict[str, int] = {}
        queue = deque((c, 1) for c in children.get(asset, []))
        while queue:
            node, depth = queue.popleft()
            if depth > max_depth or (node in depths and depths[node] <= depth):
                continue
            depths[node] = depth
            for child in children.get(node, []):
                queue.append((child, depth + 1))
        return depths

    def downstream(self, asset: str, max_depth: int = 10) -> list[str]:
        """Breadth-first downstream closure. The blast-radius primitive."""
        depths = self.downstream_depths(asset, max_depth)
        return sorted(depths, key=lambda name: (depths[name], name))

    def upstream(self, asset: str, max_depth: int = 10) -> list[str]:
        seen: set[str] = set()
        order: list[str] = []
        start = self.assets.get(asset)
        if not start:
            return []
        queue = deque((u, 1) for u in start.upstreams)
        while queue:
            node, depth = queue.popleft()
            if node in seen or depth > max_depth:
                continue
            seen.add(node)
            order.append(node)
            parent = self.assets.get(node)
            if parent:
                for up in parent.upstreams:
                    queue.append((up, depth + 1))
        return order

    # -- accessors used by the tool layer ----------------------------------- #

    def metric_series(self, asset: str, days: int = 30) -> list[MetricPoint]:
        cutoff = self.now - timedelta(days=days)
        return sorted(
            (m for m in self.metrics if m.asset == asset and m.at >= cutoff),
            key=lambda m: m.at,
        )

    def latest_metric(self, asset: str) -> MetricPoint | None:
        series = self.metric_series(asset, days=40)
        return series[-1] if series else None

    def runs_for(self, asset: str, hours: int = 48) -> list[TaskRun]:
        cutoff = self.now - timedelta(hours=hours)
        return sorted(
            (r for r in self.task_runs if r.target_asset == asset and r.started_at >= cutoff),
            key=lambda r: r.started_at,
        )

    def failed_runs(self, hours: int = 24) -> list[TaskRun]:
        cutoff = self.now - timedelta(hours=hours)
        return sorted(
            (r for r in self.task_runs if r.state == "FAILED" and r.started_at >= cutoff),
            key=lambda r: r.started_at,
        )

    def copies_for(self, table: str, hours: int = 48) -> list[CopyEvent]:
        cutoff = self.now - timedelta(hours=hours)
        return sorted(
            (c for c in self.copy_events if c.target_table == table and c.loaded_at >= cutoff),
            key=lambda c: c.loaded_at,
        )

    def dmf_for(self, asset: str, hours: int = 48) -> list[DMFResult]:
        cutoff = self.now - timedelta(hours=hours)
        return sorted(
            (d for d in self.dmf_results if d.asset == asset and d.measured_at >= cutoff),
            key=lambda d: d.measured_at,
        )

    def changes_in_window(self, hours: int = 72) -> list[ChangeEvent]:
        cutoff = self.now - timedelta(hours=hours)
        return sorted((c for c in self.changes if c.at >= cutoff), key=lambda c: c.at)

    def schema_history(self, asset: str) -> list[SchemaVersion]:
        return sorted(
            (s for s in self.schema_versions if s.asset == asset),
            key=lambda s: s.effective_from,
        )

    def consumers_of(self, asset: str) -> list[ConsumerAccess]:
        reachable = set(self.downstream(asset)) | {asset}
        return [c for c in self.consumer_access if c.asset in reachable]

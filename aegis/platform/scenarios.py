"""
Seeded incident scenarios with ground truth.

Five incidents chosen to exercise different parts of the system rather than five
flavours of the same thing:

| key                  | tests                                                        |
|----------------------|--------------------------------------------------------------|
| schema_drift         | the full happy path: both gates approve, PR raised, data fixed|
| join_fanout          | a silent bug -- nothing failed, the numbers are just wrong    |
| vendor_outage        | alert-storm de-duplication (12 alerts, 1 incident) + no code  |
| null_explosion       | the reject path: business says no -> quarantine + ticket      |
| chronic_lateness     | the defer path: recurrence detected -> future-issues backlog  |

Each scenario mutates the `World` to make the incident true, emits the raw signals
the agents will see, and declares a `GroundTruth` the eval harness scores against.
The agents never see `GroundTruth` or `narrative`.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..contracts import (
    Alert,
    FileArrival,
    GateVerdict,
    HumanRole,
    IncidentState,
    IncidentType,
    QuarantineRecord,
    Severity,
    ValidationFailure,
)
from .world import (
    SCHEMA_CONTRACTS,
    ChangeEvent,
    CopyEvent,
    DMFResult,
    SchemaVersion,
    StoredFile,
    TaskRun,
    World,
)


@dataclass
class GroundTruth:
    """What a perfect run would conclude. Scored by evals/harness.py."""

    root_cause_tag: str
    incident_type: IncidentType
    expected_severity: Severity
    severity_tolerance: int = 1  # allow +/- this many ranks
    primary_asset: str = ""
    must_identify_assets: tuple[str, ...] = ()
    expected_code_change_kinds: tuple[str, ...] = ()
    expected_actions: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = ()
    min_alerts_suppressed: int = 0
    expect_recurrence_detected: bool = False
    expected_final_state: IncidentState = IncidentState.RESOLVED


@dataclass
class ScenarioSignals:
    """What the platform hands the agents at t=0."""

    file: FileArrival | None
    validation_failures: list[ValidationFailure] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)
    quarantine: QuarantineRecord | None = None


@dataclass
class Scenario:
    key: str
    title: str
    narrative: str  # reviewer-facing; never shown to the agents
    ground_truth: GroundTruth
    build: Callable[[World], ScenarioSignals]
    #: How the humans respond at each gate when running unattended.
    scripted_responses: dict[tuple[str, HumanRole], GateVerdict] = field(default_factory=dict)
    #: Standing approvals already in place when this incident arrives. Lets a scenario
    #: be self-contained and deterministic rather than depending on another having run
    #: first -- the eval harness scores scenarios independently.
    seed_precedents: Callable[[datetime], list[Any]] | None = None


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _alert(
    world: World,
    idx: int,
    source: str,
    asset: str,
    rule: str,
    message: str,
    minutes_ago: int,
    observed: dict | None = None,
    severity: str = "warning",
    file_id: str | None = None,
) -> Alert:
    return Alert(
        alert_id=f"ALRT-{idx:04d}",
        fired_at=world.now - timedelta(minutes=minutes_ago),
        source=source,  # type: ignore[arg-type]
        asset=asset,
        rule=rule,
        message=message,
        observed=observed or {},
        raw_severity=severity,
        file_id=file_id,
    )


def _perturb_latest(
    world: World,
    asset: str,
    *,
    row_factor: float | None = None,
    null_rate: float | None = None,
    freshness_lag_min: float | None = None,
    distinct_key_ratio: float | None = None,
    credits_factor: float | None = None,
) -> None:
    """Bend today's metric point for an asset so the anomaly is real in the data."""
    point = world.latest_metric(asset)
    if point is None:
        return
    if row_factor is not None:
        point.row_count = int(point.row_count * row_factor)
    if null_rate is not None:
        point.null_rate = null_rate
    if freshness_lag_min is not None:
        point.freshness_lag_min = freshness_lag_min
    if distinct_key_ratio is not None:
        point.distinct_key_ratio = distinct_key_ratio
    if credits_factor is not None:
        point.credits = round(point.credits * credits_factor, 3)


def _raw_bucket() -> str:
    """The landing bucket, from the environment.

    These names were hard-coded here while the store was in-memory, where a name is
    just a label. The moment `S3Storage` went live they became the actual target of a
    copy-then-delete, and `settings.raw_bucket` -- which `build_runtime` was already
    reading and passing around as `ZoneLayout` -- turned out to configure nothing.
    S3 bucket names are globally unique, so "aegis-raw" is not a name anyone can
    reliably have; it has to be settable. Same defaults as `config.py`.
    """
    return os.environ.get("AEGIS_RAW_BUCKET", "aegis-raw")


def _quarantine_bucket() -> str:
    return os.environ.get("AEGIS_QUARANTINE_BUCKET", "aegis-quarantine")


def _quarantine(world: World, file: StoredFile, reason: str) -> QuarantineRecord:
    file.quarantined = True
    file.quarantine_uri = f"s3://{_quarantine_bucket()}/{file.key}"
    return QuarantineRecord(
        file_id=file.file_id,
        quarantined_at=world.now - timedelta(minutes=8),
        quarantine_uri=file.quarantine_uri,
        reason=reason,
        original_uri=f"s3://{file.bucket}/{file.key}",
    )


# --------------------------------------------------------------------------- #
# S1 -- upstream schema drift (full approve path)
# --------------------------------------------------------------------------- #


def _build_schema_drift(world: World) -> ScenarioSignals:
    arrival = world.now - timedelta(minutes=12)
    stored = StoredFile(
        file_id="FILE-20260917-STRIPE-0001",
        bucket=_raw_bucket(),
        key="stripe/2026-09-17/charges-part-0001.parquet",
        source_system="stripe",
        arrived_at=arrival,
        size_bytes=48_211_904,
        row_count=182_447,
        target_table="RAW.STRIPE_CHARGES",
        declared_schema_version="v4",
    )
    world.files[stored.file_id] = stored

    # The vendor notice nobody read, five days before the change landed.
    world.changes.append(
        ChangeEvent(
            "chg_stripe_v4",
            world.now - timedelta(days=5),
            "vendor_notice",
            "Stripe API v4 payload change",
            (
                "Stripe announced that from 2026-09-17 the charges export renames "
                "`currency_code` to `currency` and adds `presentment_currency`. "
                "Consumers on v3 contracts must update their mappings."
            ),
            author="stripe-notifications",
            touched_assets=("RAW.STRIPE_CHARGES",),
        )
    )
    world.schema_versions.append(
        SchemaVersion(
            "RAW.STRIPE_CHARGES",
            "v4",
            world.now - timedelta(minutes=12),
            (
                ("charge_id", "VARCHAR"),
                ("customer_id", "VARCHAR"),
                ("amount_minor", "NUMBER"),
                ("currency", "VARCHAR"),
                ("presentment_currency", "VARCHAR"),
                ("status", "VARCHAR"),
                ("created_at", "TIMESTAMP_NTZ"),
            ),
            source="observed",
        )
    )
    world.copy_events.append(
        CopyEvent(
            file_id=stored.file_id,
            file_name=stored.key,
            target_table="RAW.STRIPE_CHARGES",
            loaded_at=arrival + timedelta(minutes=2),
            status="LOAD_FAILED",
            row_count=0,
            row_parsed=182_447,
            errors_seen=182_447,
            first_error="Column 'CURRENCY_CODE' not found in file schema",
            first_error_column="CURRENCY_CODE",
            pipe_name="PIPE_STRIPE_CHARGES",
        )
    )
    failed_start = world.now - timedelta(minutes=9)
    world.task_runs.append(
        TaskRun(
            run_id="run_stg_payments_now",
            task_name="TASK_BUILD_PAYMENTS",
            target_asset="STG.PAYMENTS",
            started_at=failed_start,
            ended_at=failed_start + timedelta(seconds=38),
            state="FAILED",
            error_code="000904",
            error_message="invalid identifier 'CURRENCY_CODE'",
            rows_written=0,
            query_id="6f2a91c3-7d41-4b0e-9a2f-11c9e4d7b8a0",
        )
    )
    for asset in ("STG.PAYMENTS", "INT.REVENUE_EVENTS", "MART.DAILY_REVENUE"):
        _perturb_latest(world, asset, freshness_lag_min=200.0, row_factor=0.0)
    world.dmf_results.append(
        DMFResult(
            "STG.PAYMENTS",
            "FRESHNESS",
            None,
            world.now - timedelta(minutes=5),
            200.0,
            threshold=90.0,
            breached=True,
        )
    )

    failures = [
        ValidationFailure(
            failure_id="VF-0001",
            file_id=stored.file_id,
            rule="schema_contract.RAW.STRIPE_CHARGES.v3",
            rule_kind="schema",
            detected_at=arrival + timedelta(minutes=2),
            expected="column `currency_code` present (contract v3)",
            actual="column absent; unexpected columns `currency`, `presentment_currency`",
            failed_rows=182_447,
            total_rows=182_447,
            sample_redacted="<182447 rows rejected at parse: missing required column>",
        )
    ]
    alerts = [
        _alert(world, 1, "snowpipe", "RAW.STRIPE_CHARGES", "copy_failed",
               "Snowpipe load failed: column CURRENCY_CODE not found", 10,
               {"errors_seen": 182447}, "critical", stored.file_id),
        _alert(world, 2, "task_monitor", "STG.PAYMENTS", "task_failed",
               "TASK_BUILD_PAYMENTS failed: invalid identifier 'CURRENCY_CODE'", 9,
               {"error_code": "000904"}, "critical"),
        _alert(world, 3, "freshness_monitor", "STG.PAYMENTS", "freshness_sla",
               "STG.PAYMENTS is 200 minutes stale (SLA 90)", 5,
               {"lag_min": 200, "sla_min": 90}, "critical"),
        _alert(world, 4, "freshness_monitor", "INT.REVENUE_EVENTS", "freshness_sla",
               "INT.REVENUE_EVENTS is 195 minutes stale (SLA 150)", 4,
               {"lag_min": 195, "sla_min": 150}, "warning"),
        _alert(world, 5, "freshness_monitor", "MART.DAILY_REVENUE", "freshness_sla",
               "MART.DAILY_REVENUE is 190 minutes stale (SLA 180)", 3,
               {"lag_min": 190, "sla_min": 180}, "critical"),
        _alert(world, 6, "user_report", "DASH.FINANCE_CLOSE", "user_reported",
               "Finance reports revenue dashboard showing yesterday's figures", 2,
               {"reporter": "finance-close-team"}, "warning"),
    ]
    return ScenarioSignals(
        file=FileArrival(
            file_id=stored.file_id,
            bucket=stored.bucket,
            key=stored.key,
            source_system=stored.source_system,
            arrived_at=stored.arrived_at,
            size_bytes=stored.size_bytes,
            row_count=stored.row_count,
            declared_schema_version="v4",
            target_table=stored.target_table,
        ),
        validation_failures=failures,
        alerts=alerts,
        quarantine=_quarantine(
            world, stored, "Schema contract v3 violation: required column missing"
        ),
    )


SCENARIO_SCHEMA_DRIFT = Scenario(
    key="schema_drift",
    title="Stripe v4 payload renames currency_code, revenue pipeline halts",
    narrative=(
        "Stripe shipped an announced-but-unread breaking change. The landing file is "
        "structurally valid but violates the v3 contract, so it is quarantined before "
        "anything reaches the warehouse. Revenue marts go stale during month-end close. "
        "The correct fix is a contract bump plus a mapping change, then reprocess the "
        "quarantined file -- not a rollback, because the vendor is not reverting."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="upstream_schema_drift",
        incident_type=IncidentType.SCHEMA_DRIFT,
        expected_severity=Severity.SEV1,
        primary_asset="RAW.STRIPE_CHARGES",
        must_identify_assets=("MART.DAILY_REVENUE", "EXPORT.NETSUITE_REVENUE"),
        expected_code_change_kinds=("schema_contract", "transform_fix"),
        expected_actions=("release_quarantine", "reprocess_file", "rerun_task"),
        forbidden_actions=("rollback_deployment", "restate_table"),
        min_alerts_suppressed=2,
        expected_final_state=IncidentState.RESOLVED,
    ),
    build=_build_schema_drift,
    scripted_responses={
        ("business_gate", HumanRole.PRODUCT_OWNER): GateVerdict.APPROVE,
        ("business_gate", HumanRole.SCRUM_MASTER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.DEVELOPER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.ENG_MANAGER): GateVerdict.APPROVE,
    },
)


# --------------------------------------------------------------------------- #
# S2 -- bad deploy causing join fan-out (silent wrongness)
# --------------------------------------------------------------------------- #


def _build_join_fanout(world: World) -> ScenarioSignals:
    world.changes.append(
        ChangeEvent(
            "chg_pr_1862",
            world.now - timedelta(hours=3),
            "deploy",
            "Join orders to customers on email for better match rate",
            (
                "PR #1862 changed the INT.ORDER_ENRICHED join key from customer_id to "
                "lower(email) to recover unmatched guest orders."
            ),
            author="dan.whitfield",
            repo="northwind-dbt",
            pr_number=1862,
            touched_assets=("INT.ORDER_ENRICHED",),
        )
    )
    _perturb_latest(world, "INT.ORDER_ENRICHED", row_factor=3.12,
                    distinct_key_ratio=0.317, credits_factor=4.4)
    _perturb_latest(world, "MART.MARKETING_ROI", row_factor=2.86, credits_factor=3.1)
    world.dmf_results.extend(
        [
            DMFResult("INT.ORDER_ENRICHED", "DUPLICATE_COUNT", "order_id",
                      world.now - timedelta(minutes=22), 441_207.0, 0.0, True),
            DMFResult("INT.ORDER_ENRICHED", "ROW_COUNT", None,
                      world.now - timedelta(minutes=22), 655_920.0, 230_000.0, True),
            DMFResult("MART.MARKETING_ROI", "ROW_COUNT", None,
                      world.now - timedelta(minutes=18), 120_120.0, 48_000.0, True),
        ]
    )
    started = world.now - timedelta(hours=2, minutes=40)
    world.task_runs.append(
        TaskRun(
            run_id="run_int_order_enriched_now",
            task_name="TASK_BUILD_ORDER_ENRICHED",
            target_asset="INT.ORDER_ENRICHED",
            started_at=started,
            ended_at=started + timedelta(minutes=41),
            state="SUCCEEDED",
            rows_written=655_920,
            credits_used=8.44,
            query_id="a1d38e77-2c05-49be-8f3a-77bd2c0e9911",
        )
    )
    alerts = [
        _alert(world, 1, "dmf_monitor", "INT.ORDER_ENRICHED", "duplicate_count",
               "order_id duplicates: 441,207 (expected 0)", 22,
               {"duplicates": 441207}, "critical"),
        _alert(world, 2, "dmf_monitor", "INT.ORDER_ENRICHED", "row_count_anomaly",
               "Row count 655,920 is 3.1x the 30-day median", 22,
               {"observed": 655920, "median": 210000}, "critical"),
        _alert(world, 3, "dmf_monitor", "MART.MARKETING_ROI", "row_count_anomaly",
               "Row count 120,120 is 2.9x the 30-day median", 18,
               {"observed": 120120}, "warning"),
        _alert(world, 4, "cost_monitor", "INT.ORDER_ENRICHED", "credit_spike",
               "Task consumed 8.44 credits vs 1.9 baseline", 15,
               {"credits": 8.44, "baseline": 1.9}, "warning"),
    ]
    return ScenarioSignals(file=None, validation_failures=[], alerts=alerts, quarantine=None)


SCENARIO_JOIN_FANOUT = Scenario(
    key="join_fanout",
    title="PR #1862 join key change fans out orders 3x",
    narrative=(
        "Nothing failed. Every task succeeded, no file was rejected, and the only "
        "symptom is that the numbers are wrong and the warehouse bill went up. The "
        "change correlator has to connect a three-hour-old merge to a duplicate-count "
        "breach, and the plan must revert the join and restate, not just rerun."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="bad_deploy_join_fanout",
        incident_type=IncidentType.VOLUME_ANOMALY,
        expected_severity=Severity.SEV2,
        primary_asset="INT.ORDER_ENRICHED",
        must_identify_assets=("MART.MARKETING_ROI",),
        expected_code_change_kinds=("transform_fix",),
        expected_actions=("rollback_deployment", "restate_table"),
        forbidden_actions=("release_quarantine",),
        expected_final_state=IncidentState.RESOLVED,
    ),
    build=_build_join_fanout,
    scripted_responses={
        ("business_gate", HumanRole.PRODUCT_OWNER): GateVerdict.APPROVE,
        ("business_gate", HumanRole.SCRUM_MASTER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.DEVELOPER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.ENG_MANAGER): GateVerdict.APPROVE,
    },
)


# --------------------------------------------------------------------------- #
# S3 -- vendor outage freshness cascade (alert storm)
# --------------------------------------------------------------------------- #


def _build_vendor_outage(world: World) -> ScenarioSignals:
    world.changes.append(
        ChangeEvent(
            "chg_openfx_incident",
            world.now - timedelta(hours=5),
            "vendor_notice",
            "OpenFX status: degraded performance on rates API",
            "OpenFX reports elevated 5xx rates on /v2/rates. No ETA published.",
            author="openfx-status",
            touched_assets=("RAW.FX_RATES",),
        )
    )
    base = world.now - timedelta(hours=4, minutes=30)
    for attempt in range(1, 5):
        started = base + timedelta(minutes=35 * (attempt - 1))
        world.task_runs.append(
            TaskRun(
                run_id=f"run_fx_extract_{attempt}",
                task_name="TASK_EXTRACT_FX_RATES",
                target_asset="STG.FX_RATES",
                started_at=started,
                ended_at=started + timedelta(minutes=9),
                state="FAILED",
                error_code="HTTP_503",
                error_message="OpenFX /v2/rates returned 503 after 3 retries",
                attempt=attempt,
                rows_written=0,
                query_id=f"fx-{attempt:04d}-503e-4a11-9c02-{attempt:012d}",
            )
        )
    cascade = [
        "STG.FX_RATES", "INT.REVENUE_EVENTS", "MART.DAILY_REVENUE", "MART.ARR_SNAPSHOT",
        "MART.EXEC_KPIS", "DASH.EXEC_DAILY", "DASH.FINANCE_CLOSE", "EXPORT.NETSUITE_REVENUE",
    ]
    for asset in cascade:
        _perturb_latest(world, asset, freshness_lag_min=280.0)

    alerts = [
        _alert(world, 1, "task_monitor", "STG.FX_RATES", "task_failed",
               "TASK_EXTRACT_FX_RATES failed after 4 attempts (HTTP 503)", 268,
               {"attempts": 4, "http_status": 503}, "critical"),
    ]
    for idx, asset in enumerate(cascade, start=2):
        alerts.append(
            _alert(world, idx, "freshness_monitor", asset, "freshness_sla",
                   f"{asset} exceeded freshness SLA (280 min)", 240 - idx * 6,
                   {"lag_min": 280}, "critical" if asset.startswith(("MART", "EXPORT")) else "warning")
        )
    # a couple of unrelated background alerts triage should NOT fold in
    alerts.append(
        _alert(world, 20, "cost_monitor", "MART.SUPPORT_HEALTH", "credit_spike",
               "Support health build used 2.1x baseline credits", 190,
               {"credits": 3.2}, "info")
    )
    return ScenarioSignals(file=None, validation_failures=[], alerts=alerts, quarantine=None)


SCENARIO_VENDOR_OUTAGE = Scenario(
    key="vendor_outage",
    title="OpenFX outage cascades 9 freshness breaches through revenue reporting",
    narrative=(
        "One upstream vendor is down and the monitoring stack fires ten alerts across "
        "nine assets. A naive system opens ten incidents and pages four teams. Triage "
        "must fold the cascade into one incident rooted at the FX extract, keep the "
        "unrelated support-health cost alert out of it, and recognise that there is no "
        "code to fix -- the remediation is a stale-rate fallback plus a replay."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="upstream_vendor_outage",
        incident_type=IncidentType.FRESHNESS_BREACH,
        expected_severity=Severity.SEV1,
        primary_asset="STG.FX_RATES",
        must_identify_assets=("MART.DAILY_REVENUE", "MART.ARR_SNAPSHOT"),
        expected_code_change_kinds=("reprocess_manifest",),
        expected_actions=("rerun_task", "notify_vendor"),
        forbidden_actions=("rollback_deployment",),
        min_alerts_suppressed=6,
        expected_final_state=IncidentState.RESOLVED,
    ),
    build=_build_vendor_outage,
    scripted_responses={
        ("business_gate", HumanRole.PRODUCT_OWNER): GateVerdict.APPROVE,
        ("business_gate", HumanRole.SCRUM_MASTER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.DEVELOPER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.ENG_MANAGER): GateVerdict.APPROVE,
    },
)


# --------------------------------------------------------------------------- #
# S4 -- silent null explosion (business rejects -> quarantine + ticket)
# --------------------------------------------------------------------------- #


def _build_null_explosion(world: World) -> ScenarioSignals:
    arrival = world.now - timedelta(minutes=35)
    stored = StoredFile(
        file_id="FILE-20260917-SFDC-0007",
        bucket=_raw_bucket(),
        key="salesforce/2026-09-17/accounts-delta.parquet",
        source_system="salesforce",
        arrived_at=arrival,
        size_bytes=9_884_112,
        row_count=64_209,
        target_table="RAW.SALESFORCE_ACCOUNTS",
        declared_schema_version="v3",
    )
    world.files[stored.file_id] = stored
    world.changes.append(
        ChangeEvent(
            "chg_sfdc_picklist",
            world.now - timedelta(hours=9),
            "config",
            "Salesforce: account_tier picklist migration phase 1",
            (
                "CRM team began migrating account_tier values to the new segmentation "
                "model. Legacy values are being cleared ahead of phase 2 backfill."
            ),
            author="crm.admin",
            touched_assets=("RAW.SALESFORCE_ACCOUNTS",),
        )
    )
    _perturb_latest(world, "STG.ACCOUNTS", null_rate=0.381)
    _perturb_latest(world, "INT.CUSTOMER_360", null_rate=0.243)
    _perturb_latest(world, "MART.CHURN_FEATURES", null_rate=0.198)
    world.dmf_results.extend(
        [
            DMFResult("STG.ACCOUNTS", "NULL_PERCENT", "account_tier",
                      world.now - timedelta(minutes=30), 38.1, 2.0, True),
            DMFResult("INT.CUSTOMER_360", "NULL_PERCENT", "account_tier",
                      world.now - timedelta(minutes=26), 24.3, 2.0, True),
            DMFResult("MART.CHURN_FEATURES", "NULL_PERCENT", "tier_segment",
                      world.now - timedelta(minutes=20), 19.8, 5.0, True),
        ]
    )
    failures = [
        ValidationFailure(
            failure_id="VF-0007",
            file_id=stored.file_id,
            rule="dq.STG.ACCOUNTS.account_tier.null_rate_max_2pct",
            rule_kind="nullability",
            detected_at=world.now - timedelta(minutes=30),
            expected="null rate <= 2.0%",
            actual="null rate 38.1%",
            failed_rows=24_463,
            total_rows=64_209,
            sample_redacted="<24463 rows with empty account_tier>",
        )
    ]
    alerts = [
        _alert(world, 1, "dmf_monitor", "STG.ACCOUNTS", "null_rate",
               "account_tier null rate 38.1% (threshold 2%)", 30,
               {"null_pct": 38.1}, "critical", stored.file_id),
        _alert(world, 2, "dmf_monitor", "INT.CUSTOMER_360", "null_rate",
               "account_tier null rate 24.3% (threshold 2%)", 26,
               {"null_pct": 24.3}, "warning"),
        _alert(world, 3, "dmf_monitor", "MART.CHURN_FEATURES", "null_rate",
               "tier_segment null rate 19.8% (threshold 5%)", 20,
               {"null_pct": 19.8}, "warning"),
    ]
    return ScenarioSignals(
        file=FileArrival(
            file_id=stored.file_id,
            bucket=stored.bucket,
            key=stored.key,
            source_system=stored.source_system,
            arrived_at=stored.arrived_at,
            size_bytes=stored.size_bytes,
            row_count=stored.row_count,
            declared_schema_version="v3",
            target_table=stored.target_table,
        ),
        validation_failures=failures,
        alerts=alerts,
        quarantine=_quarantine(
            world, stored, "Nullability rule breach on account_tier (38.1% > 2%)"
        ),
    )


SCENARIO_NULL_EXPLOSION = Scenario(
    key="null_explosion",
    title="Salesforce picklist migration empties account_tier for 38% of accounts",
    narrative=(
        "The data is not corrupt -- it is deliberately mid-migration on the source "
        "side. The agent's technically-correct fix (default the nulls and carry on) is "
        "the wrong business call, because phase 2 will backfill the real values. This "
        "is the case the business gate exists for: the Product Owner rejects, the file "
        "stays quarantined, a ticket is raised against the CRM team and the pipeline "
        "owner is notified. The agent does not get to overrule that."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="source_config_change",
        incident_type=IncidentType.QUALITY_DEGRADATION,
        expected_severity=Severity.SEV3,
        severity_tolerance=1,
        primary_asset="RAW.SALESFORCE_ACCOUNTS",
        must_identify_assets=("MART.CHURN_FEATURES", "ML.CHURN_MODEL"),
        expected_code_change_kinds=("validation_rule",),
        expected_actions=("open_ticket", "notify_pipeline_owner"),
        forbidden_actions=("release_quarantine", "backfill_table"),
        expected_final_state=IncidentState.REJECTED_QUARANTINED,
    ),
    build=_build_null_explosion,
    scripted_responses={
        ("business_gate", HumanRole.PRODUCT_OWNER): GateVerdict.REJECT,
        ("business_gate", HumanRole.SCRUM_MASTER): GateVerdict.APPROVE,
    },
)


# --------------------------------------------------------------------------- #
# S5 -- chronic vendor lateness (defer -> future-issues backlog)
# --------------------------------------------------------------------------- #


def _build_chronic_lateness(world: World) -> ScenarioSignals:
    for days_ago, run_idx in ((21, 1), (12, 2)):
        started = world.now - timedelta(days=days_ago)
        world.task_runs.append(
            TaskRun(
                run_id=f"run_adspend_late_{run_idx}",
                task_name="TASK_EXTRACT_AD_SPEND",
                target_asset="STG.AD_SPEND",
                started_at=started,
                ended_at=started + timedelta(minutes=12),
                state="FAILED",
                error_code="NO_FILE",
                error_message="AdBridge daily export not present at expected prefix",
                rows_written=0,
            )
        )
    started = world.now - timedelta(hours=7)
    world.task_runs.append(
        TaskRun(
            run_id="run_adspend_late_3",
            task_name="TASK_EXTRACT_AD_SPEND",
            target_asset="STG.AD_SPEND",
            started_at=started,
            ended_at=started + timedelta(minutes=12),
            state="FAILED",
            error_code="NO_FILE",
            error_message="AdBridge daily export not present at expected prefix",
            rows_written=0,
        )
    )
    for asset in ("STG.AD_SPEND", "MART.MARKETING_ROI", "DASH.GROWTH"):
        _perturb_latest(world, asset, freshness_lag_min=430.0)
    alerts = [
        _alert(world, 1, "task_monitor", "STG.AD_SPEND", "task_failed",
               "TASK_EXTRACT_AD_SPEND failed: AdBridge export missing", 420,
               {"error_code": "NO_FILE"}, "warning"),
        _alert(world, 2, "freshness_monitor", "MART.MARKETING_ROI", "freshness_sla",
               "MART.MARKETING_ROI is 430 minutes stale (SLA 480)", 60,
               {"lag_min": 430}, "info"),
        _alert(world, 3, "freshness_monitor", "DASH.GROWTH", "freshness_sla",
               "DASH.GROWTH is 430 minutes stale (SLA 720)", 55,
               {"lag_min": 430}, "info"),
    ]
    return ScenarioSignals(file=None, validation_failures=[], alerts=alerts, quarantine=None)


SCENARIO_CHRONIC_LATENESS = Scenario(
    key="chronic_lateness",
    title="AdBridge export late for the third time in 30 days",
    narrative=(
        "Individually trivial; collectively a pattern. Incident memory should surface "
        "the two prior occurrences and reframe this from 'a late file' to 'an unreliable "
        "vendor integration needing a retry-with-backoff redesign'. That is a backlog "
        "item, not a 3am fix, so the Product Owner defers it to the future-issues list "
        "with the high-level change captured."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="chronic_vendor_lateness",
        incident_type=IncidentType.FRESHNESS_BREACH,
        expected_severity=Severity.SEV4,
        severity_tolerance=1,
        primary_asset="STG.AD_SPEND",
        must_identify_assets=("MART.MARKETING_ROI",),
        expected_code_change_kinds=("reprocess_manifest",),
        expected_actions=("rerun_task",),
        forbidden_actions=("restate_table", "rollback_deployment"),
        expect_recurrence_detected=True,
        expected_final_state=IncidentState.DEFERRED_BACKLOG,
    ),
    build=_build_chronic_lateness,
    scripted_responses={
        ("business_gate", HumanRole.PRODUCT_OWNER): GateVerdict.DEFER,
        ("business_gate", HumanRole.SCRUM_MASTER): GateVerdict.APPROVE,
    },
)


# --------------------------------------------------------------------------- #



# --------------------------------------------------------------------------- #
# S6 -- transient infrastructure failure (autonomous, no humans involved)
# --------------------------------------------------------------------------- #


def _build_transient_timeout(world: World) -> ScenarioSignals:
    started = world.now - timedelta(minutes=48)
    world.task_runs.append(
        TaskRun(
            run_id="run_support_health_timeout",
            task_name="TASK_BUILD_SUPPORT_HEALTH",
            target_asset="MART.SUPPORT_HEALTH",
            started_at=started,
            ended_at=started + timedelta(minutes=15),
            state="FAILED",
            error_code="000630",
            error_message="Statement reached its statement or warehouse timeout and was cancelled",
            attempt=1,
            rows_written=0,
            query_id="c4e18b02-9a37-41d6-8f55-2b7c09ae4411",
        )
    )
    world.changes.append(
        ChangeEvent(
            "chg_wh_contention",
            world.now - timedelta(minutes=55),
            "infra",
            "COMPUTE_WH queue depth spike",
            (
                "Warehouse queuing exceeded 90s for a 7-minute window during the "
                "nightly batch overlap. Self-cleared."
            ),
            author="platform.monitoring",
            touched_assets=("MART.SUPPORT_HEALTH",),
        )
    )
    _perturb_latest(world, "MART.SUPPORT_HEALTH", freshness_lag_min=760.0, row_factor=0.0)

    alerts = [
        _alert(world, 1, "task_monitor", "MART.SUPPORT_HEALTH", "task_failed",
               "TASK_BUILD_SUPPORT_HEALTH failed: statement timeout (000630)", 33,
               {"error_code": "000630", "attempts": 1}, "warning"),
        _alert(world, 2, "freshness_monitor", "MART.SUPPORT_HEALTH", "freshness_sla",
               "MART.SUPPORT_HEALTH is 760 minutes stale (SLA 720)", 20,
               {"lag_min": 760, "sla_min": 720}, "info"),
    ]
    return ScenarioSignals(file=None, validation_failures=[], alerts=alerts, quarantine=None)


SCENARIO_TRANSIENT_TIMEOUT = Scenario(
    key="transient_timeout",
    title="Warehouse timeout on a tier-3 support mart, self-cleared",
    narrative=(
        "The boring case, and the one that proves the autonomy ladder is a real "
        "decision rather than decoration. A tier-3 asset missed its build because the "
        "warehouse was briefly queued; the contention has already cleared. Nothing is "
        "wrong with the data, no code is implicated, and the entire fix is to run it "
        "again -- an action that is trivially reversible. Waking a Product Owner, a "
        "Scrum Master, a developer and an engineering manager to approve a retry is "
        "how an approval process gets ignored. Aegis executes it, verifies recovery, "
        "and tells the pipeline owner afterwards."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="infrastructure_failure",
        incident_type=IncidentType.FRESHNESS_BREACH,
        expected_severity=Severity.SEV4,
        primary_asset="MART.SUPPORT_HEALTH",
        expected_code_change_kinds=(),
        expected_actions=("rerun_task",),
        forbidden_actions=("rollback_deployment", "restate_table", "release_quarantine",
                           "backfill_table"),
        expected_final_state=IncidentState.RESOLVED,
    ),
    build=_build_transient_timeout,
    # Deliberately empty: if any gate were opened, nobody would answer it and the
    # incident would escalate. Passing this scenario proves no gate was opened.
    scripted_responses={},
)


# --------------------------------------------------------------------------- #
# S7 / S8 -- the same problem twice: approval becomes precedent
# --------------------------------------------------------------------------- #


def _ad_spend_drift(world: World, *, rename: tuple[str, str], file_seq: str) -> ScenarioSignals:
    """Shared builder: AdBridge renames a column on the marketing spend feed."""
    old_col, new_col = rename
    arrival = world.now - timedelta(minutes=18)
    stored = StoredFile(
        file_id=f"FILE-ADBRIDGE-{file_seq}",
        bucket=_raw_bucket(),
        key=f"adbridge/{world.now.date().isoformat()}/spend-daily.parquet",
        source_system="adbridge",
        arrived_at=arrival,
        size_bytes=6_418_220,
        row_count=84_106,
        target_table="RAW.AD_SPEND",
        declared_schema_version="v4",
    )
    world.files[stored.file_id] = stored

    world.changes.append(
        ChangeEvent(
            f"chg_adbridge_{file_seq}",
            world.now - timedelta(days=2),
            "vendor_notice",
            f"AdBridge export field rename: {old_col} -> {new_col}",
            (
                f"AdBridge is renaming `{old_col}` to `{new_col}` in the daily spend "
                "export. Consumers must update their column mappings."
            ),
            author="adbridge-notifications",
            touched_assets=("RAW.AD_SPEND",),
        )
    )
    world.schema_versions.append(
        SchemaVersion(
            "RAW.AD_SPEND",
            "v5",
            arrival,
            tuple(
                (new_col if name == old_col else name, dtype)
                for name, dtype in SCHEMA_CONTRACTS["RAW.AD_SPEND"]
            ),
            source="observed",
        )
    )
    world.copy_events.append(
        CopyEvent(
            file_id=stored.file_id,
            file_name=stored.key,
            target_table="RAW.AD_SPEND",
            loaded_at=arrival + timedelta(minutes=2),
            status="LOAD_FAILED",
            row_count=0,
            row_parsed=84_106,
            errors_seen=84_106,
            first_error=f"Column '{old_col.upper()}' not found in file schema",
            first_error_column=old_col.upper(),
            pipe_name="PIPE_AD_SPEND",
        )
    )
    failed = world.now - timedelta(minutes=14)
    world.task_runs.append(
        TaskRun(
            run_id=f"run_stg_ad_spend_{file_seq}",
            task_name="TASK_BUILD_AD_SPEND",
            target_asset="STG.AD_SPEND",
            started_at=failed,
            ended_at=failed + timedelta(seconds=41),
            state="FAILED",
            error_code="000904",
            error_message=f"invalid identifier '{old_col.upper()}'",
            rows_written=0,
        )
    )
    for asset in ("STG.AD_SPEND", "MART.MARKETING_ROI"):
        _perturb_latest(world, asset, freshness_lag_min=520.0, row_factor=0.0)

    failures = [
        ValidationFailure(
            failure_id=f"VF-AD-{file_seq}",
            file_id=stored.file_id,
            rule="schema_contract.RAW.AD_SPEND.v4",
            rule_kind="schema",
            detected_at=arrival + timedelta(minutes=2),
            expected=f"column `{old_col}` present (contract v4)",
            actual=f"column absent; unexpected column `{new_col}`",
            failed_rows=84_106,
            total_rows=84_106,
            sample_redacted="<84106 rows rejected at parse: missing required column>",
        )
    ]
    alerts = [
        _alert(world, 1, "snowpipe", "RAW.AD_SPEND", "copy_failed",
               f"Snowpipe load failed: column {old_col.upper()} not found", 16,
               {"errors_seen": 84106}, "warning", stored.file_id),
        _alert(world, 2, "task_monitor", "STG.AD_SPEND", "task_failed",
               f"TASK_BUILD_AD_SPEND failed: invalid identifier '{old_col.upper()}'", 14,
               {"error_code": "000904"}, "warning"),
        _alert(world, 3, "freshness_monitor", "MART.MARKETING_ROI", "freshness_sla",
               "MART.MARKETING_ROI is 520 minutes stale (SLA 480)", 8,
               {"lag_min": 520}, "warning"),
    ]
    return ScenarioSignals(
        file=FileArrival(
            file_id=stored.file_id,
            bucket=stored.bucket,
            key=stored.key,
            source_system=stored.source_system,
            arrived_at=stored.arrived_at,
            size_bytes=stored.size_bytes,
            row_count=stored.row_count,
            declared_schema_version="v5",
            target_table=stored.target_table,
        ),
        validation_failures=failures,
        alerts=alerts,
        quarantine=_quarantine(
            world, stored, "Schema contract v4 violation: required column missing"
        ),
    )


def _build_ad_spend_first(world: World) -> ScenarioSignals:
    return _ad_spend_drift(world, rename=("spend_usd", "spend_amount_usd"), file_seq="0041")


def _build_ad_spend_repeat(world: World) -> ScenarioSignals:
    return _ad_spend_drift(world, rename=("channel", "channel_name"), file_seq="0092")


def _seed_ad_spend_precedent(now: datetime) -> list[Any]:
    """
    The standing decision left behind by the first occurrence.

    Seeded rather than carried over from another scenario run, so this scenario is
    self-contained and the eval harness can score it independently. The live CLI demo
    of the pair works either way, because a real precedent is recorded at runtime too.
    """
    from ..contracts import ChangeMagnitude, RiskTier
    from ..precedent import Precedent

    approved_at = now - timedelta(days=22)
    return [
        Precedent(
            precedent_id="PRE-upstream_sch-0417",
            root_cause_tag="upstream_schema_drift",
            asset="RAW.AD_SPEND",
            incident_id="INC-AD-SPEND-DRIFT",
            approved_by="product_owner",
            approved_at=approved_at,
            expires_at=approved_at + timedelta(days=90),
            approved_actions=frozenset(
                {"pin_schema_version", "release_quarantine", "reprocess_file", "rerun_task"}
            ),
            max_risk_tier=RiskTier.T2_MUTATING,
            max_change_magnitude=ChangeMagnitude.MODIFYING,
            severity_at_approval=Severity.SEV3,
            times_applied=0,
        )
    ]


SCENARIO_AD_SPEND_DRIFT = Scenario(
    key="ad_spend_drift",
    title="AdBridge renames a spend column, marketing reporting stalls",
    narrative=(
        "A mid-severity schema drift on the marketing feed. Nothing financial is "
        "downstream and the fix is the familiar one: bump the contract, map the column, "
        "release and replay. The Product Owner approves. Because it resolved cleanly and "
        "is not SEV1, that approval is recorded as a standing decision -- so the next "
        "time AdBridge does this, nobody is asked again."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="upstream_schema_drift",
        incident_type=IncidentType.SCHEMA_DRIFT,
        expected_severity=Severity.SEV3,
        primary_asset="RAW.AD_SPEND",
        must_identify_assets=("MART.MARKETING_ROI",),
        expected_code_change_kinds=("schema_contract", "transform_fix"),
        expected_actions=("release_quarantine", "reprocess_file", "rerun_task"),
        forbidden_actions=("rollback_deployment", "restate_table"),
        expected_final_state=IncidentState.RESOLVED,
    ),
    build=_build_ad_spend_first,
    scripted_responses={
        ("business_gate", HumanRole.PRODUCT_OWNER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.DEVELOPER): GateVerdict.APPROVE,
        ("technical_gate", HumanRole.ENG_MANAGER): GateVerdict.APPROVE,
    },
)


SCENARIO_AD_SPEND_REPEAT = Scenario(
    key="ad_spend_drift_repeat",
    title="AdBridge does it again three weeks later — nobody is asked",
    narrative=(
        "The same vendor, the same feed, the same class of change, three weeks on. A "
        "human already decided this exact situation and that decision has not expired. "
        "Aegis applies it, names the person who made it, and fixes the pipeline without "
        "interrupting anyone -- then records the application in the audit trail so the "
        "decision remains attributable. This is what stops an approval gate decaying "
        "into a rubber stamp: the Product Owner's attention is spent on novel "
        "judgements, not repeated ones."
    ),
    ground_truth=GroundTruth(
        root_cause_tag="upstream_schema_drift",
        incident_type=IncidentType.SCHEMA_DRIFT,
        expected_severity=Severity.SEV3,
        primary_asset="RAW.AD_SPEND",
        expected_code_change_kinds=("schema_contract", "transform_fix"),
        expected_actions=("release_quarantine", "reprocess_file", "rerun_task"),
        forbidden_actions=("rollback_deployment", "restate_table"),
        expected_final_state=IncidentState.RESOLVED,
    ),
    build=_build_ad_spend_repeat,
    # Empty on purpose: if any gate opened, nobody would answer and this would escalate.
    # It passes only because the standing approval meant no gate was opened at all.
    scripted_responses={},
    seed_precedents=_seed_ad_spend_precedent,
)


ALL_SCENARIOS: tuple[Scenario, ...] = (
    SCENARIO_SCHEMA_DRIFT,
    SCENARIO_JOIN_FANOUT,
    SCENARIO_VENDOR_OUTAGE,
    SCENARIO_NULL_EXPLOSION,
    SCENARIO_CHRONIC_LATENESS,
    SCENARIO_TRANSIENT_TIMEOUT,
    SCENARIO_AD_SPEND_DRIFT,
    SCENARIO_AD_SPEND_REPEAT,
)

SCENARIOS_BY_KEY: dict[str, Scenario] = {s.key: s for s in ALL_SCENARIOS}


def load_scenario(key: str, now: datetime | None = None) -> tuple[Scenario, World, ScenarioSignals]:
    """Build a fresh world, apply the scenario, and return everything the runner needs."""
    if key not in SCENARIOS_BY_KEY:
        raise KeyError(f"unknown scenario {key!r}; choose from {sorted(SCENARIOS_BY_KEY)}")
    scenario = SCENARIOS_BY_KEY[key]
    world = World.build(now=now)
    signals = scenario.build(world)
    return scenario, world, signals

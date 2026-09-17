#!/usr/bin/env python3
"""
Preflight for `SnowflakePlatform`. Read-only; mutates nothing.

    python scripts/check_snowflake.py --asset RAW.STRIPE_CHARGES

`aegis/platform/snowflake.py` was written against Snowflake's documentation and has
never been run. This script is how that changes. It calls every method of the protocol
against a real account, one at a time, and prints what worked, what came back empty and
what raised -- so the gap between "written" and "verified" closes with evidence rather
than assertion.

An empty result is reported as EMPTY, not as a pass. Several methods depend on optional
warehouse setup: object tags for governance metadata, scheduled data metric functions
for the 30-day baseline. Those return nothing at all on a bare account, and calling that
a success would be the same self-deception the project has spent its defect log
unlearning.

Exit code is 0 only if every method ran without raising.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Any, Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aegis.platform.snowflake import SnowflakePlatform  # noqa: E402

GREEN, YELLOW, RED, DIM, RESET = "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"

#: ACCOUNT_USAGE is the usual blocker: it needs an explicit grant that most roles
#: do not have, and the failure reads as "object does not exist" rather than
#: "permission denied", which sends people looking in the wrong place for an hour.
GRANTS = """
-- Run as ACCOUNTADMIN. The agent role needs to read metadata, and nothing else.
CREATE ROLE IF NOT EXISTS AEGIS_AGENT;
GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE TO ROLE AEGIS_AGENT;   -- ACCOUNT_USAGE + LOCAL
GRANT MONITOR ON ACCOUNT TO ROLE AEGIS_AGENT;                          -- task/query history
GRANT USAGE ON WAREHOUSE <WH> TO ROLE AEGIS_AGENT;
GRANT USAGE ON DATABASE <DB> TO ROLE AEGIS_AGENT;
GRANT USAGE ON ALL SCHEMAS IN DATABASE <DB> TO ROLE AEGIS_AGENT;
GRANT SELECT ON ALL TABLES IN DATABASE <DB> TO ROLE AEGIS_AGENT;
-- Deliberately NOT granted: INSERT, UPDATE, DELETE, TRUNCATE, DROP.
-- Remediation runs in dry-run mode until someone decides otherwise.
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", help="an asset to probe, e.g. RAW.STRIPE_CHARGES")
    parser.add_argument("--file-id", default="", help="a file name fragment from COPY_HISTORY")
    parser.add_argument("--grants", action="store_true", help="print the grants and exit")
    args = parser.parse_args()

    if args.grants:
        print(GRANTS)
        return 0
    if not args.asset:
        parser.error("--asset is required (or use --grants)")

    missing = [v for v in ("SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER") if not os.environ.get(v)]
    if missing:
        print(f"{RED}missing environment: {', '.join(missing)}{RESET}")
        print(f"{DIM}Set them in ~/.zshrc, not in the project folder.{RESET}")
        return 2

    print(f"{DIM}connecting...{RESET}")
    try:
        # dry_run is the default, and this script never overrides it.
        platform = SnowflakePlatform()
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}connection failed: {exc}{RESET}")
        return 2

    ident = platform._rows(
        "SELECT CURRENT_ACCOUNT() a, CURRENT_ROLE() r, CURRENT_WAREHOUSE() w, CURRENT_DATABASE() d"
    )[0]
    print(
        f"  account {ident['a']}  role {ident['r']}  warehouse {ident['w']}  database {ident['d']}\n"
    )

    asset = args.asset
    probes: list[tuple[str, Callable[[], Any], str]] = [
        ("asset", lambda: platform.asset(asset), "INFORMATION_SCHEMA.TABLES + TAG_REFERENCES"),
        ("lineage_upstream", lambda: platform.lineage_upstream(asset), "OBJECT_DEPENDENCIES"),
        ("lineage_downstream", lambda: platform.lineage_downstream(asset), "OBJECT_DEPENDENCIES"),
        ("task_runs", lambda: platform.task_runs(asset, 48), "INFORMATION_SCHEMA.TASK_HISTORY"),
        ("failed_runs", lambda: platform.failed_runs(24), "INFORMATION_SCHEMA.TASK_HISTORY"),
        ("copy_history", lambda: platform.copy_history(asset, 48), "INFORMATION_SCHEMA.COPY_HISTORY"),
        ("dmf_results", lambda: platform.dmf_results(asset, 48), "DATA_QUALITY_MONITORING_RESULTS"),
        ("metric_summary", lambda: platform.metric_summary(asset, 30), "DMF history, aggregated"),
        ("schema_diff", lambda: platform.schema_diff(asset), "ACCOUNT_USAGE.COLUMNS"),
        ("changes", lambda: platform.changes(72), "QUERY_HISTORY, DDL only"),
        ("consumers", lambda: platform.consumers(asset), "ACCOUNT_USAGE.ACCESS_HISTORY"),
        ("health", lambda: platform.health([asset]), "composed from metric_summary"),
    ]
    if args.file_id:
        probes.append(("file_info", lambda: platform.file_info(args.file_id), "COPY_HISTORY"))

    failures, empties = 0, 0
    print(f"  {'method':<20} {'result':<10} {'source':<38} detail")
    print(f"  {'-' * 20} {'-' * 10} {'-' * 38} {'-' * 20}")

    for name, call, source in probes:
        try:
            value = call()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            detail = str(exc).splitlines()[0][:60]
            print(f"  {name:<20} {RED}{'FAIL':<10}{RESET} {source:<38} {detail}")
            if os.environ.get("AEGIS_TRACE"):
                traceback.print_exc()
            continue

        empty = (
            value in ([], {}, None)
            or (isinstance(value, dict) and value.get("available") is False)
            or (isinstance(value, dict) and value.get("known") is False)
        )
        if empty:
            empties += 1
            print(f"  {name:<20} {YELLOW}{'EMPTY':<10}{RESET} {source:<38} no rows returned")
        else:
            size = len(value) if isinstance(value, list) else len(str(value))
            unit = "rows" if isinstance(value, list) else "chars"
            print(f"  {name:<20} {GREEN}{'OK':<10}{RESET} {source:<38} {size} {unit}")

    # The one write path, exercised without writing.
    result = platform.execute("rerun_task", {"task": "OPS.LOAD_STRIPE"})
    ok = result.ok and "dry run" in result.detail
    print(
        f"  {'execute (dry run)':<20} {(GREEN + 'OK' if ok else RED + 'FAIL'):<19}{RESET} "
        f"{'allow-list, renders SQL only':<38} {result.data.get('sql', '')[:40]}"
    )
    refused = platform.execute("drop_table", {"table": "X"})
    print(
        f"  {'drop_table refused':<20} {(GREEN + 'OK' if not refused.ok else RED + 'FAIL'):<19}{RESET} "
        f"{'not in the allow-list, by design':<38} {refused.detail[:40]}"
    )

    print()
    if failures:
        print(f"{RED}{failures} method(s) raised.{RESET} Most often this is ACCOUNT_USAGE access:")
        print(f"{DIM}  python scripts/check_snowflake.py --grants{RESET}")
    if empties:
        print(
            f"{YELLOW}{empties} method(s) returned nothing.{RESET} Usually means the optional "
            "setup is absent —\n  object tags for governance metadata, scheduled DMFs for the "
            "30-day baseline.\n  Not a failure, but those methods are unproven until they "
            "return something."
        )
    if not failures and not empties:
        print(f"{GREEN}Every method returned data.{RESET} Record the output in PROJECT-LOG.md —")
        print("  that is what turns 'written' into 'verified'.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

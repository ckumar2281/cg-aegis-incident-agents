#!/usr/bin/env python3
"""
Read what is actually in the zones, and check it against the v3 contract.

Why this exists: the incident graph is an incident *responder*. It is only ever invoked
because something already failed, so nothing in a run ever looks at a file that is fine.
That makes the most concrete claim in the demo -- "this file violates the contract" --
the one thing a reviewer cannot see for themselves.

This is not the production gate, and it is deliberately not wired into the graph. In
production `COPY` is the schema gate, because it compares against the *real table* rather
than a registry that can drift from it (DEPLOYMENT.md 3a). This reads the same objects
`COPY` would and shows what it would find.

    python scripts/inspect_landing.py
"""

from __future__ import annotations

import io
import os
import sys

from rich.console import Console
from rich.table import Table

console = Console()

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
RAW = os.environ.get("AEGIS_RAW_BUCKET", "")
QUARANTINE = os.environ.get("AEGIS_QUARANTINE_BUCKET", "")

#: Mirrors the v3 SchemaVersion the pipeline's mapping is written against. The one
#: column that matters is currency_code: v4 renamed it and added presentment_currency.
CONTRACTS = {
    "stripe": {
        "version": "v3",
        "required": ["charge_id", "customer_id", "amount_minor", "currency_code",
                     "status", "created_at"],
    },
    "salesforce": {
        "version": "v3",
        "required": ["account_id", "account_name", "account_tier", "region", "updated_at"],
    },
    "adbridge": {
        "version": "v2",
        "required": ["campaign_id", "campaign_name", "spend_micros", "impressions", "clicks"],
    },
}


def main() -> int:
    if not RAW:
        console.print("[red]set AEGIS_RAW_BUCKET first[/red]")
        return 1
    try:
        import boto3  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    s3 = boto3.client("s3", region_name=REGION)

    def inspect(bucket: str, zone: str) -> list[tuple]:
        rows = []
        page = s3.list_objects_v2(Bucket=bucket, MaxKeys=100)
        for obj in page.get("Contents", []):
            key = obj["Key"]
            source = key.split("/")[1] if zone == "quarantine" else key.split("/")[0]
            contract = CONTRACTS.get(source)
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            columns = list(pq.read_schema(io.BytesIO(body)).names)
            if contract is None:
                rows.append((zone, key, columns, "?", "no contract on file"))
                continue
            missing = [c for c in contract["required"] if c not in columns]
            extra = [c for c in columns if c not in contract["required"]]
            if missing:
                verdict = "VIOLATES"
                detail = "missing " + ", ".join(missing)
                if extra:
                    detail += "  ·  found " + ", ".join(extra)
            else:
                verdict = "PASSES"
                detail = f"all {len(contract['required'])} required columns present"
            rows.append((zone, key, columns, verdict, detail))
        return rows

    rows = inspect(RAW, "landing")
    if QUARANTINE:
        rows += inspect(QUARANTINE, "quarantine")

    table = Table(box=None, padding=(0, 2), show_header=True, header_style="dim")
    table.add_column("zone", style="dim")
    table.add_column("object")
    table.add_column("contract")
    table.add_column("what the loader would find")

    for zone, key, _columns, verdict, detail in rows:
        style = {"PASSES": "green", "VIOLATES": "red"}.get(verdict, "yellow")
        zone_style = "red" if zone == "quarantine" else "dim"
        table.add_row(
            f"[{zone_style}]{zone}[/{zone_style}]",
            key,
            f"[{style}]{verdict}[/{style}]",
            detail,
        )

    console.print()
    console.print(table)
    console.print()
    passes = sum(1 for r in rows if r[3] == "PASSES")
    violates = sum(1 for r in rows if r[3] == "VIOLATES")
    console.print(
        f"  [green]{passes} file(s) the pipeline can load[/green]   "
        f"[red]{violates} that it cannot[/red]   "
        "[dim]— the difference is one renamed column[/dim]"
    )
    console.print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

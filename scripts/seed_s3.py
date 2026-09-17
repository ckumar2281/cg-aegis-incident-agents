#!/usr/bin/env python3
"""
Seed the landing zone with the objects the scenarios reference.

Until now "quarantine" was a flag on an in-memory record. With `AEGIS_STORAGE_PROVIDER=s3`
it becomes a copy-then-delete against a real bucket, which means the objects have to
actually exist -- otherwise `S3Storage.move` correctly refuses with "source does not
exist" and the trace says containment was incomplete.

Two design notes worth defending:

**The files are real parquet with the real schemas.** The Stripe object genuinely lacks
`currency_code` and genuinely has `currency` and `presentment_currency`. A reviewer can
download it and check. A placeholder here would make the most literal claim in the demo
-- "this file violates the v3 contract" -- the one thing that was faked.

**A good file is seeded too.** `stripe/.../charges-part-0000.parquet` is v3-shaped and
valid. Nothing in the run touches it today, because the quarantine decision is currently
made by the scenario rather than by reading the object. It is here so the contrast can
be shown, and so the validator that *should* make that decision has something to pass.

Usage:
    export AEGIS_RAW_BUCKET=cg-aegis-raw-<account-id>
    export AEGIS_QUARANTINE_BUCKET=cg-aegis-quarantine-<account-id>
    python scripts/seed_s3.py --create-buckets
    python scripts/seed_s3.py --check
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import random
import sys

REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
RAW = os.environ.get("AEGIS_RAW_BUCKET", "")
QUARANTINE = os.environ.get("AEGIS_QUARANTINE_BUCKET", "")

STRIPE_ROWS = 182_447
SFDC_ROWS = 64_209
ADBRIDGE_ROWS = 4_812


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _pa():
    try:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError:
        _fail("pyarrow is required to write parquet:  pip install pyarrow")
    return pa, pq


def _parquet(columns: dict[str, list]) -> bytes:
    pa, pq = _pa()
    table = pa.table(columns)
    sink = io.BytesIO()
    pq.write_table(table, sink, compression="snappy")
    return sink.getvalue()


# --------------------------------------------------------------------------- #
# The payloads
# --------------------------------------------------------------------------- #


def stripe_charges(*, contract: str, rows: int) -> bytes:
    """
    v3 is what the pipeline's mapping expects. v4 is what Stripe actually shipped:
    `currency_code` renamed to `currency`, with `presentment_currency` added.

    The load failure in the scenario is `Column 'CURRENCY_CODE' not found in file
    schema`. That string is only true of the v4 file, and it is true because of this.
    """
    rnd = random.Random(20260917)
    base = dt.datetime(2026, 9, 17, 4, 0, 0)
    common = {
        "charge_id": [f"ch_{rnd.getrandbits(48):012x}" for _ in range(rows)],
        "customer_id": [f"cus_{rnd.getrandbits(40):010x}" for _ in range(rows)],
        "amount_minor": [rnd.randrange(150, 480_00) for _ in range(rows)],
    }
    currencies = ["USD", "EUR", "GBP", "CAD", "AUD"]
    tail = {
        "status": [rnd.choice(["succeeded", "succeeded", "succeeded", "pending", "failed"])
                   for _ in range(rows)],
        "created_at": [base + dt.timedelta(seconds=rnd.randrange(0, 86_400)) for _ in range(rows)],
    }
    if contract == "v3":
        return _parquet({**common,
                         "currency_code": [rnd.choice(currencies) for _ in range(rows)],
                         **tail})
    return _parquet({**common,
                     "currency": [rnd.choice(currencies) for _ in range(rows)],
                     "presentment_currency": [rnd.choice(currencies) for _ in range(rows)],
                     **tail})


def salesforce_accounts(rows: int) -> bytes:
    """Phase 1 of the picklist migration: account_tier cleared for ~38% of rows."""
    rnd = random.Random(7)
    tiers = ["enterprise", "mid_market", "smb", "strategic"]
    return _parquet({
        "account_id": [f"001{rnd.getrandbits(36):09x}" for _ in range(rows)],
        "account_name": [f"Account {i:06d}" for i in range(rows)],
        "account_tier": [None if rnd.random() < 0.381 else rnd.choice(tiers) for _ in range(rows)],
        "region": [rnd.choice(["NA", "EMEA", "APAC", "LATAM"]) for _ in range(rows)],
        "updated_at": [dt.datetime(2026, 9, 17, 2, 0, 0)
                       + dt.timedelta(seconds=rnd.randrange(0, 43_200)) for _ in range(rows)],
    })


def adbridge_spend(rows: int) -> bytes:
    """AdBridge renamed `spend_micros` to `cost_micros`. Same shape of break as Stripe."""
    rnd = random.Random(31)
    return _parquet({
        "campaign_id": [f"cmp_{rnd.getrandbits(32):08x}" for _ in range(rows)],
        "campaign_name": [f"Campaign {i:05d}" for i in range(rows)],
        "cost_micros": [rnd.randrange(1_000, 9_000_000) for _ in range(rows)],
        "impressions": [rnd.randrange(100, 400_000) for _ in range(rows)],
        "clicks": [rnd.randrange(0, 9_000) for _ in range(rows)],
    })


def objects() -> list[tuple[str, str, callable]]:
    """(key, description, factory). The AdBridge key follows the current date."""
    today = dt.date.today().isoformat()
    return [
        ("stripe/2026-09-17/charges-part-0000.parquet",
         "Stripe v3 — valid, nothing wrong with it",
         lambda: stripe_charges(contract="v3", rows=STRIPE_ROWS)),
        ("stripe/2026-09-17/charges-part-0001.parquet",
         "Stripe v4 — the breaking change, no currency_code",
         lambda: stripe_charges(contract="v4", rows=STRIPE_ROWS)),
        ("salesforce/2026-09-17/accounts-delta.parquet",
         "Salesforce — account_tier null for 38% of rows",
         lambda: salesforce_accounts(SFDC_ROWS)),
        (f"adbridge/{today}/spend-daily.parquet",
         "AdBridge — spend_micros renamed to cost_micros",
         lambda: adbridge_spend(ADBRIDGE_ROWS)),
    ]


# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--create-buckets", action="store_true",
                    help="create the landing and quarantine buckets if absent")
    ap.add_argument("--check", action="store_true",
                    help="list what is in both zones and exit")
    ap.add_argument("--force", action="store_true",
                    help="re-upload objects that already exist")
    ap.add_argument("--reset", action="store_true",
                    help="empty the quarantine zone and restore the landing zone, "
                         "so the demo can be run again")
    args = ap.parse_args()

    if not RAW or not QUARANTINE:
        _fail("set AEGIS_RAW_BUCKET and AEGIS_QUARANTINE_BUCKET first")

    import boto3  # noqa: PLC0415
    from botocore.exceptions import ClientError  # noqa: PLC0415

    s3 = boto3.client("s3", region_name=REGION)

    def exists(bucket: str, key: str) -> bool:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False

    if args.check:
        for bucket in (RAW, QUARANTINE):
            print(f"\n{bucket}")
            try:
                page = s3.list_objects_v2(Bucket=bucket, MaxKeys=100)
            except ClientError as exc:
                print(f"  unreachable: {exc.response['Error']['Code']}")
                continue
            contents = page.get("Contents", [])
            if not contents:
                print("  (empty)")
            for obj in contents:
                print(f"  {obj['Size']:>12,}  {obj['Key']}")
        return 0

    if args.reset:
        # A live run is destructive by design: `move` copies then deletes the source,
        # and refuses to overwrite an existing target so one incident cannot erase
        # another's evidence. Both of those are correct, and both mean the demo is
        # not repeatable without putting the world back. Rehearsing a destructive
        # demo without a reset is how you discover this in front of an audience.
        removed = 0
        token = None
        while True:
            kwargs = {"Bucket": QUARANTINE, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = s3.list_objects_v2(**kwargs)
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket=QUARANTINE, Key=obj["Key"])
                removed += 1
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        print(f"cleared  {removed} object(s) from s3://{QUARANTINE}/")
        args.force = True  # re-upload whatever the move took out of the landing zone

    if args.create_buckets:
        for bucket in (RAW, QUARANTINE):
            try:
                # us-east-1 is the one region that must NOT be given a
                # LocationConstraint -- passing it is an InvalidLocationConstraint.
                if REGION == "us-east-1":
                    s3.create_bucket(Bucket=bucket)
                else:
                    s3.create_bucket(
                        Bucket=bucket,
                        CreateBucketConfiguration={"LocationConstraint": REGION},
                    )
                print(f"created  {bucket}")
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                    print(f"exists   {bucket}  ({code})")
                else:
                    _fail(f"could not create {bucket}: {code}")
            # Quarantined data is evidence. Versioning means an accidental overwrite
            # is recoverable, and it costs nothing on objects this size.
            s3.put_bucket_versioning(
                Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
            )

    for key, description, factory in objects():
        if exists(RAW, key) and not args.force:
            print(f"skip     s3://{RAW}/{key}  (already there)")
            continue
        body = factory()
        s3.put_object(Bucket=RAW, Key=key, Body=body,
                      ContentType="application/vnd.apache.parquet")
        print(f"upload   s3://{RAW}/{key}  ({len(body):,} bytes)  — {description}")

    print(f"\nlanding zone:    s3://{RAW}/")
    print(f"quarantine zone: s3://{QUARANTINE}/   (empty until an incident runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

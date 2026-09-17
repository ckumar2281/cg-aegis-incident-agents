"""
The object-store seam: landing zone and quarantine.

Until now S3 existed in this system only as a string. `StoredFile.quarantine_uri` was
set to `s3://aegis-quarantine/...` and `quarantined` flipped to True, and that was the
entire containment mechanism -- a boolean in a dataclass. Fine for scoring the
orchestration, and not the same thing as bad data being somewhere it cannot be read.

`StorageClient` is the seam that makes the difference real. Same shape as
`PlatformClient`: a protocol the agents call, a simulated implementation that keeps the
eval suite deterministic, and an S3 implementation for a real bucket. Nothing in the
graph changes -- quarantine already calls an action, it just now has somewhere for the
bytes to go.

---

## Why quarantine is a move and not a flag

The fail-safe claim in the README is that bad data never reaches the warehouse. A flag
does not enforce that: anything with the landing-zone prefix can still read the file.
A move does, because the object is no longer at the path the loader watches.

That distinction is also why this module exposes **no delete**. `move` is implemented as
copy-then-delete-the-source on S3, because that is the only way S3 does it -- but the
delete is an internal step of a move, never an operation an agent can request. There is
no code path from an agent's output to `delete_object` on an arbitrary key. The same
reasoning as `drop_table`'s absence from the Snowflake allow-list: the ladder refuses to
pre-authorise destruction, and not exposing the primitive is the second lock.

## Dry run, for the same reason as the Snowflake client

`S3Storage` defaults to `execute_mode="dry_run"`: it reports what it *would* move and
moves nothing. A remediation plan is reviewable before anything happens, at the object
level as well as the statement level. Going live is `execute_mode="live"`, by name.

## `read_head` exists because validation needs it and agents must not slurp files

Detecting schema drift means reading a CSV header. It does not mean pulling a 2 GB file
into an agent's context, which is both expensive and a good way to leak raw records past
the disclosure firewall. `read_head` takes a byte budget and uses an HTTP Range request,
so the cost is bounded at the source rather than by trusting the caller to truncate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .world import World

log = logging.getLogger(__name__)

#: Bytes of an object `read_head` will fetch by default. A CSV header and a couple of
#: rows is a few hundred bytes; 8 KB is generous and still nothing.
DEFAULT_HEAD_BYTES = 8192


@dataclass
class FileStat:
    uri: str
    exists: bool
    size_bytes: int = 0
    last_modified: datetime | None = None
    etag: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "exists": self.exists,
            "size_bytes": self.size_bytes,
            "last_modified": self.last_modified.isoformat() if self.last_modified else "",
            "etag": self.etag,
        }


@dataclass
class StorageResult:
    action: str
    ok: bool
    detail: str
    source_uri: str = ""
    target_uri: str = ""
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "ok": self.ok,
            "detail": self.detail,
            "from": self.source_uri,
            "to": self.target_uri,
            "dry_run": self.dry_run,
        }


class StorageClient(Protocol):
    """
    What the agents may do to objects. Deliberately five verbs, and none of them delete.

    `move` is the containment primitive and `move` back is the release. Everything else
    is read-only inspection.
    """

    def stat(self, uri: str) -> FileStat: ...
    def exists(self, uri: str) -> bool: ...
    def list_prefix(self, prefix_uri: str, limit: int = 100) -> list[FileStat]: ...
    def read_head(self, uri: str, max_bytes: int = DEFAULT_HEAD_BYTES) -> bytes: ...
    def move(self, source_uri: str, target_uri: str) -> StorageResult: ...


def parse_uri(uri: str) -> tuple[str, str]:
    """`s3://bucket/some/key` -> `("bucket", "some/key")`."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    if not bucket:
        raise ValueError(f"no bucket in uri: {uri!r}")
    return bucket, key


def build_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key.lstrip('/')}"


# --------------------------------------------------------------------------- #
# Simulated
# --------------------------------------------------------------------------- #


class SimulatedStorage:
    """
    An in-memory object store over the seeded `World`.

    Keeps the eval suite deterministic and offline, exactly as `SimulatedPlatform` does.
    Objects are synthesised from the world's `StoredFile` records on first access rather
    than pre-materialised, so nothing here depends on fixture files on disk.
    """

    def __init__(self, world: World) -> None:
        self.world = world
        self._objects: dict[str, bytes] = {}
        self._moves: list[StorageResult] = []
        for f in world.files.values():
            self._objects[build_uri(f.bucket, f.key)] = self._synthesise(f)

    @staticmethod
    def _synthesise(f: Any) -> bytes:
        """A plausible CSV header plus one row. Enough for `read_head` to be meaningful."""
        header = "charge_id,customer_id,amount_cents,currency_code,created_at\n"
        row = f"ch_000001,cus_00042,129900,USD,{f.arrived_at.isoformat()}\n"
        return (header + row).encode()

    def stat(self, uri: str) -> FileStat:
        data = self._objects.get(uri)
        if data is None:
            return FileStat(uri=uri, exists=False)
        return FileStat(
            uri=uri,
            exists=True,
            size_bytes=len(data),
            last_modified=datetime.now(timezone.utc),
            etag=f"sim-{abs(hash(uri)) % 10**12:012d}",
        )

    def exists(self, uri: str) -> bool:
        return uri in self._objects

    def list_prefix(self, prefix_uri: str, limit: int = 100) -> list[FileStat]:
        return [
            self.stat(u) for u in sorted(self._objects) if u.startswith(prefix_uri)
        ][:limit]

    def read_head(self, uri: str, max_bytes: int = DEFAULT_HEAD_BYTES) -> bytes:
        return self._objects.get(uri, b"")[:max_bytes]

    def move(self, source_uri: str, target_uri: str) -> StorageResult:
        if source_uri not in self._objects:
            return StorageResult(
                "move", False, f"source does not exist: {source_uri}", source_uri, target_uri
            )
        if target_uri in self._objects:
            # Refusing rather than clobbering. A quarantine that silently overwrites an
            # earlier quarantined file destroys the evidence of the first incident.
            return StorageResult(
                "move", False, f"target already exists: {target_uri}", source_uri, target_uri
            )
        self._objects[target_uri] = self._objects.pop(source_uri)
        result = StorageResult("move", True, "moved", source_uri, target_uri)
        self._moves.append(result)
        return result

    @property
    def moves(self) -> list[StorageResult]:
        """What was moved, for the trace and for tests to assert against."""
        return list(self._moves)


# --------------------------------------------------------------------------- #
# Real S3
# --------------------------------------------------------------------------- #


class S3Storage:
    """
    `StorageClient` over a real bucket.

    Dry run by default, no delete primitive, and `move` refuses to overwrite an existing
    target. The last one matters more than it looks: quarantine paths are derived from a
    file id, and a re-run that quarantines the same id twice would otherwise erase the
    first incident's evidence.
    """

    def __init__(
        self,
        *,
        client: Any = None,
        region: str = "us-east-1",
        execute_mode: str = "dry_run",
    ) -> None:
        if execute_mode not in ("dry_run", "live"):
            raise ValueError("execute_mode must be 'dry_run' or 'live'")
        self.execute_mode = execute_mode
        self._client = client if client is not None else self._connect(region)

    @staticmethod
    def _connect(region: str) -> Any:
        try:
            import boto3  # noqa: PLC0415 -- optional dependency
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("boto3 is required for S3Storage") from exc
        return boto3.client("s3", region_name=region)

    # -- reads -------------------------------------------------------------- #

    def stat(self, uri: str) -> FileStat:
        bucket, key = parse_uri(uri)
        try:
            head = self._client.head_object(Bucket=bucket, Key=key)
        except Exception:  # noqa: BLE001 -- 404 and 403 both mean "not usable"
            return FileStat(uri=uri, exists=False)
        return FileStat(
            uri=uri,
            exists=True,
            size_bytes=int(head.get("ContentLength") or 0),
            last_modified=head.get("LastModified"),
            etag=str(head.get("ETag") or "").strip('"'),
        )

    def exists(self, uri: str) -> bool:
        return self.stat(uri).exists

    def list_prefix(self, prefix_uri: str, limit: int = 100) -> list[FileStat]:
        bucket, prefix = parse_uri(prefix_uri)
        response = self._client.list_objects_v2(
            Bucket=bucket, Prefix=prefix, MaxKeys=min(limit, 1000)
        )
        return [
            FileStat(
                uri=build_uri(bucket, obj["Key"]),
                exists=True,
                size_bytes=int(obj.get("Size") or 0),
                last_modified=obj.get("LastModified"),
                etag=str(obj.get("ETag") or "").strip('"'),
            )
            for obj in response.get("Contents", [])
        ]

    def read_head(self, uri: str, max_bytes: int = DEFAULT_HEAD_BYTES) -> bytes:
        """
        First `max_bytes` of the object, fetched with a Range request.

        Bounded at S3 rather than after download: a landing-zone file can be gigabytes,
        and the caller only ever wants the header. This is also a containment measure --
        an agent cannot accidentally pull a whole file of customer records into a prompt.
        """
        bucket, key = parse_uri(uri)
        response = self._client.get_object(
            Bucket=bucket, Key=key, Range=f"bytes=0-{max(0, max_bytes - 1)}"
        )
        return response["Body"].read()

    # -- the one write ------------------------------------------------------ #

    def move(self, source_uri: str, target_uri: str) -> StorageResult:
        """
        Copy then delete the source. The delete is a step of the move, never exposed.

        Refuses if the target exists. Refuses to execute at all in dry-run mode, which
        is the default, so a remediation plan can be reviewed at the object level before
        anything is touched.
        """
        source_bucket, source_key = parse_uri(source_uri)
        target_bucket, target_key = parse_uri(target_uri)

        if source_bucket == target_bucket and source_key == target_key:
            return StorageResult(
                "move", False, "source and target are the same object", source_uri, target_uri
            )
        if self.execute_mode == "dry_run":
            return StorageResult(
                "move",
                True,
                "dry run -- would copy then delete the source",
                source_uri,
                target_uri,
                dry_run=True,
            )
        if not self.exists(source_uri):
            return StorageResult(
                "move", False, f"source does not exist: {source_uri}", source_uri, target_uri
            )
        if self.exists(target_uri):
            return StorageResult(
                "move",
                False,
                f"target already exists, refusing to overwrite: {target_uri}",
                source_uri,
                target_uri,
            )
        try:
            self._client.copy_object(
                Bucket=target_bucket,
                Key=target_key,
                CopySource={"Bucket": source_bucket, "Key": source_key},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("copy failed %s -> %s: %s", source_uri, target_uri, exc)
            return StorageResult("move", False, f"copy failed: {exc}"[:400], source_uri, target_uri)

        # Only now, and only for this key. If the delete fails the object exists in both
        # places, which is safe -- the loader no longer reads the landing path because
        # the target is where containment is asserted, and a duplicate is recoverable.
        try:
            self._client.delete_object(Bucket=source_bucket, Key=source_key)
        except Exception as exc:  # noqa: BLE001
            log.warning("copied but source delete failed %s: %s", source_uri, exc)
            return StorageResult(
                "move",
                True,
                f"copied to target; source delete failed and the original remains: {exc}"[:400],
                source_uri,
                target_uri,
            )
        return StorageResult("move", True, "moved", source_uri, target_uri)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


@dataclass
class ZoneLayout:
    """
    Where things live. Configuration, because every platform team names these
    differently and a hard-coded prefix is the first thing that breaks on adoption.
    """

    landing_bucket: str = "aegis-landing"
    quarantine_bucket: str = "aegis-quarantine"
    landing_prefix: str = "raw"
    quarantine_prefix: str = "quarantine"

    def landing_uri(self, source_system: str, file_name: str) -> str:
        return build_uri(self.landing_bucket, f"{self.landing_prefix}/{source_system}/{file_name}")

    def quarantine_uri(self, incident_id: str, source_system: str, file_name: str) -> str:
        """
        Quarantined objects are keyed by **incident**, not just by file.

        The same file quarantined by two incidents lands in two places, so neither
        erases the other's evidence, and an auditor can list one incident's held data
        with a single prefix query.
        """
        return build_uri(
            self.quarantine_bucket,
            f"{self.quarantine_prefix}/{incident_id}/{source_system}/{file_name}",
        )

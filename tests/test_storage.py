"""
Adversarial tests for the object-store boundary.

The claim this layer makes is that containment is real: bad data is *moved* out of the
landing zone rather than flagged. That claim has three ways to be false, and each has a
test that tries to make it false.

1. The client offers a delete an agent could call. It does not -- `move` is the only
   write verb, and its internal delete is scoped to the key it just copied.
2. Writing is on by default, so a careless construction mutates a real bucket.
3. A second quarantine of the same file silently overwrites the first, destroying the
   evidence of the earlier incident.
"""

from __future__ import annotations

import pytest

from aegis.platform.storage import (
    S3Storage,
    SimulatedStorage,
    StorageClient,
    ZoneLayout,
    build_uri,
    parse_uri,
)
from aegis.platform.scenarios import load_scenario


class _ExplodingS3:
    """Any API call fails the test. Used to prove dry run touches nothing."""

    def __getattr__(self, name):  # noqa: ANN001, ANN204
        raise AssertionError(f"dry run must not call S3 ({name})")


def _sim() -> SimulatedStorage:
    _, world, _ = load_scenario("schema_drift")
    return SimulatedStorage(world)


class TestNoDeletePrimitive:
    def test_protocol_exposes_no_delete(self) -> None:
        """
        The agents' vocabulary is the protocol. If there is no delete in it, there is no
        way for an agent to ask for one -- checked structurally rather than by reading.
        """
        verbs = {n for n in dir(StorageClient) if not n.startswith("_")}
        assert verbs == {"stat", "exists", "list_prefix", "read_head", "move"}
        assert not any("delete" in v or "remove" in v or "purge" in v for v in verbs)

    def test_implementations_expose_no_delete(self) -> None:
        for impl in (SimulatedStorage, S3Storage):
            public = {n for n in dir(impl) if not n.startswith("_")}
            assert not any("delete" in n or "remove" in n for n in public), impl


class TestDryRunIsTheDefault:
    def test_default_mode_is_dry_run(self) -> None:
        assert S3Storage(client=_ExplodingS3()).execute_mode == "dry_run"

    def test_dry_run_calls_nothing(self) -> None:
        store = S3Storage(client=_ExplodingS3())
        result = store.move("s3://landing/raw/a.csv", "s3://quarantine/inc-1/a.csv")
        assert result.ok and result.dry_run
        assert "would copy" in result.detail

    def test_live_must_be_named_exactly(self) -> None:
        for bad in ("LIVE", "yes", "true", "", "live "):
            with pytest.raises(ValueError):
                S3Storage(client=_ExplodingS3(), execute_mode=bad)


class TestMoveRefusesToDestroy:
    def test_move_refuses_an_existing_target(self) -> None:
        """
        Two incidents quarantining the same file must not erase each other. This is the
        test that would have caught it if quarantine keys were file-scoped.
        """
        store = _sim()
        source = next(iter(store._objects))
        first = "s3://aegis-quarantine/inc-1/a.csv"
        assert store.move(source, first).ok

        other = next(iter(store._objects))
        clash = store.move(other, first)
        assert not clash.ok
        assert "already exists" in clash.detail

    def test_move_refuses_a_missing_source(self) -> None:
        result = _sim().move("s3://aegis-landing/raw/nope.csv", "s3://aegis-quarantine/x.csv")
        assert not result.ok
        assert "does not exist" in result.detail

    def test_move_is_a_move_not_a_copy(self) -> None:
        store = _sim()
        source = next(iter(store._objects))
        target = "s3://aegis-quarantine/inc-1/held.csv"
        assert store.move(source, target).ok
        assert not store.exists(source), "the landing-zone copy must be gone"
        assert store.exists(target)

    def test_same_object_move_is_refused(self) -> None:
        store = S3Storage(client=_ExplodingS3(), execute_mode="dry_run")
        uri = "s3://landing/raw/a.csv"
        result = store.move(uri, uri)
        assert not result.ok


class TestReadHeadIsBounded:
    def test_read_head_truncates(self) -> None:
        """
        A landing file can be gigabytes. An agent must not be able to pull one into a
        prompt, both for cost and because raw records are exactly what the disclosure
        firewall exists to keep out of a brief.
        """
        store = _sim()
        uri = next(iter(store._objects))
        assert len(store.read_head(uri, max_bytes=16)) == 16

    def test_read_head_of_a_missing_object_is_empty_not_an_error(self) -> None:
        assert _sim().read_head("s3://aegis-landing/raw/nope.csv") == b""


class TestZoneLayout:
    def test_quarantine_is_keyed_by_incident(self) -> None:
        """
        Per-incident prefixes are what make the refusal above survivable: the same file
        held twice lands in two places instead of colliding.
        """
        zones = ZoneLayout()
        first = zones.quarantine_uri("INC-1", "stripe", "part-1.parquet")
        second = zones.quarantine_uri("INC-2", "stripe", "part-1.parquet")
        assert first != second
        assert "INC-1" in first and "INC-2" in second

    def test_landing_and_quarantine_are_different_buckets(self) -> None:
        zones = ZoneLayout()
        landing = parse_uri(zones.landing_uri("stripe", "a.csv"))[0]
        quarantine = parse_uri(zones.quarantine_uri("INC-1", "stripe", "a.csv"))[0]
        assert landing != quarantine, "containment across buckets, not just prefixes"


class TestUriParsing:
    def test_round_trip(self) -> None:
        assert parse_uri(build_uri("b", "a/b/c.csv")) == ("b", "a/b/c.csv")

    def test_non_s3_scheme_is_refused(self) -> None:
        for bad in ("https://example.com/a", "/tmp/a.csv", "gs://b/k", "s3://"):
            with pytest.raises(ValueError):
                parse_uri(bad)

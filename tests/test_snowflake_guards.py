"""
Adversarial tests for the Snowflake client's write guards.

The read methods cannot be tested without an account, and pretending otherwise with a
pile of mocks would test the mocks. What *can* be tested here is the part that matters
most and needs no connection at all: the guarantee that an agent cannot make this client
write something nobody sanctioned.

Three separate locks, each tested by trying to pick it:

1. `execute_mode` defaults to dry run, so a client built carelessly cannot mutate.
2. Actions come from a fixed allow-list of parameterised templates, so an agent chooses
   a key and supplies parameters -- it never composes SQL.
3. `drop_table` is absent from that list on purpose, matching the autonomy ladder's
   refusal to ever pre-authorise it.
"""

from __future__ import annotations

import pytest

from aegis.platform.snowflake import _ALLOWED_STATEMENTS, SnowflakePlatform
from aegis.precedent import NEVER_PRECEDENTED


class _ExplodingConnection:
    """Any attempt to execute against this fails the test loudly."""

    def cursor(self):  # noqa: D102, ANN201
        raise AssertionError("dry run must not open a cursor")


def _client(**kwargs) -> SnowflakePlatform:
    # database is passed so __post_init__ does not query for it.
    return SnowflakePlatform(
        connection=_ExplodingConnection(), database="ANALYTICS", **kwargs
    )


class TestDryRunIsTheDefault:
    def test_default_mode_is_dry_run(self) -> None:
        assert _client().execute_mode == "dry_run"

    def test_dry_run_renders_without_executing(self) -> None:
        result = _client().execute("rerun_task", {"task": "OPS.LOAD_STRIPE"})
        assert result.ok
        assert "dry run" in result.detail
        assert result.data["sql"] == "EXECUTE TASK OPS.LOAD_STRIPE"

    def test_live_mode_must_be_asked_for_by_name(self) -> None:
        """No truthy-string accident: only the exact literal turns writing on."""
        for bad in ("live-ish", "yes", "TRUE", "", "LIVE"):
            with pytest.raises(ValueError):
                _client(execute_mode=bad)


class TestAllowList:
    def test_unknown_action_is_refused(self) -> None:
        result = _client().execute("exfiltrate_everything", {})
        assert not result.ok
        assert "allow-list" in result.detail

    def test_drop_table_is_not_implemented(self) -> None:
        """
        The contract has a risk tier for it and the ladder never pre-authorises it.
        Not writing the code is the second lock, so assert the absence directly --
        otherwise someone adds it for symmetry one day and nothing complains.
        """
        assert "drop_table" not in _ALLOWED_STATEMENTS
        result = _client().execute("drop_table", {"table": "MART.DAILY_REVENUE"})
        assert not result.ok

    def test_never_precedented_actions_are_absent_or_gated(self) -> None:
        """
        Cross-check against the precedent store's own list rather than restating it.
        `restate_table` and `backfill_table` are implemented because a human can
        approve them in the moment; they are simply never *pre*-approved.
        """
        assert "drop_table" in NEVER_PRECEDENTED
        assert "force_merge_pr" not in _ALLOWED_STATEMENTS, "VCS actions are not the warehouse's"

    def test_missing_parameter_fails_closed(self) -> None:
        result = _client().execute("reprocess_file", {"table": "RAW.STRIPE_CHARGES"})
        assert not result.ok
        assert "missing parameter" in result.detail

    def test_every_template_is_parameterised(self) -> None:
        """A template with no placeholders would be a hard-coded statement."""
        for action, template in _ALLOWED_STATEMENTS.items():
            assert "{" in template, f"{action} takes no parameters -- is it really an action?"


class TestNameResolution:
    def test_two_part_names_take_the_session_database(self) -> None:
        assert _client()._split("RAW.STRIPE_CHARGES") == ("ANALYTICS", "RAW", "STRIPE_CHARGES")

    def test_three_part_names_are_respected(self) -> None:
        assert _client()._split("PROD.RAW.STRIPE_CHARGES") == ("PROD", "RAW", "STRIPE_CHARGES")

    def test_lower_case_is_upper_cased(self) -> None:
        """Snowflake stores unquoted identifiers upper-cased; metadata views match that."""
        assert _client()._split("raw.stripe_charges") == ("ANALYTICS", "RAW", "STRIPE_CHARGES")

    def test_unqualified_name_is_refused(self) -> None:
        with pytest.raises(ValueError):
            _client()._split("STRIPE_CHARGES")

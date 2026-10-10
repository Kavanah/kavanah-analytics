"""Tests for load_tests.step_profile.

Pure functions — no locust needed. The locustfile's StagesShape and the
run-locust endpoint both delegate here, so this is where the shape math and
step validation are pinned.
"""

import pytest

from load_tests import step_profile


class TestParseStepUsers:
    """Validation of the comma-separated step list (flows into subprocess env)."""

    def test_should_parse_default_profile(self):
        assert step_profile.parse_step_users("1,5,10,25,50") == [1, 5, 10, 25, 50]

    def test_should_parse_single_baseline_step(self):
        assert step_profile.parse_step_users("1") == [1]

    def test_should_reject_first_step_not_one(self):
        """The first step is the single-user baseline window; without it the
        degradation report has no denominator."""
        with pytest.raises(ValueError, match="first step must be 1"):
            step_profile.parse_step_users("5,10,25")

    def test_should_reject_step_over_max(self):
        with pytest.raises(ValueError, match="between 1 and 100"):
            step_profile.parse_step_users("1,5,101")

    def test_should_reject_zero_step(self):
        with pytest.raises(ValueError, match="between 1 and 100"):
            step_profile.parse_step_users("1,0,5")

    @pytest.mark.parametrize(
        "raw",
        ["", "1,5;rm -rf /", "1, 5", "1,,5", "abc", "1,5.5", "-1,5", "1,5,"],
    )
    def test_should_reject_malformed_input(self, raw):
        """Same defense-in-depth posture as the duration guard —
        nothing that isn't a plain integer list may leave the endpoint."""
        with pytest.raises(ValueError):
            step_profile.parse_step_users(raw)

    def test_should_reject_too_many_steps(self):
        raw = ",".join(["1"] * (step_profile.MAX_STEPS + 1))
        with pytest.raises(ValueError, match="at most"):
            step_profile.parse_step_users(raw)


class TestParseStepSeconds:
    def test_should_parse_valid_seconds(self):
        assert step_profile.parse_step_seconds(120) == 120
        assert step_profile.parse_step_seconds("60") == 60

    @pytest.mark.parametrize("raw", [9, 3601, "abc", None, ""])
    def test_should_reject_out_of_range_or_malformed(self, raw):
        with pytest.raises(ValueError):
            step_profile.parse_step_seconds(raw)


class TestTotalRunSeconds:
    def test_should_multiply_steps_by_dwell(self):
        assert step_profile.total_run_seconds([1, 5, 10], 120) == 360


class TestTick:
    """LoadTestShape.tick() math: which user count applies at a given run time."""

    def test_should_return_baseline_during_first_step(self):
        assert step_profile.tick(0.0, [1, 5, 10], 120) == (1, step_profile.SPAWN_RATE)
        assert step_profile.tick(119.9, [1, 5, 10], 120) == (1, step_profile.SPAWN_RATE)

    def test_should_step_up_at_each_boundary(self):
        assert step_profile.tick(120.0, [1, 5, 10], 120) == (5, step_profile.SPAWN_RATE)
        assert step_profile.tick(239.9, [1, 5, 10], 120) == (5, step_profile.SPAWN_RATE)
        assert step_profile.tick(240.0, [1, 5, 10], 120) == (10, step_profile.SPAWN_RATE)

    def test_should_return_none_when_all_steps_elapsed(self):
        """None tells locust to end the run."""
        assert step_profile.tick(360.0, [1, 5, 10], 120) is None
        assert step_profile.tick(9999.0, [1, 5, 10], 120) is None

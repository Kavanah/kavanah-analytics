"""Tests for load_report_service.

Golden-CSV tests: the whole point of this service is that "degradation vs
baseline" is computed server-side where it can be pinned down here, instead
of in template JS where it can't.
"""

from asset_management.web.services.load_report_service import (
    aggregate_series,
    build_report,
    compute_baseline,
    degradation_series,
    derive_steps,
    parse_resources,
    parse_stats_history,
    probe_series,
)

_HEADER = (
    "Timestamp,User Count,Type,Name,Requests/s,Failures/s,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%,"
    "Total Request Count,Total Failure Count,Total Median Response Time,Total Average Response Time,"
    "Total Min Response Time,Total Max Response Time,Total Average Content Size\n"
)


def _row(t: int, users: int, name: str, p50: str, p95: str) -> str:
    return f"{t},{users},GET,{name},1.0,0,{p50},0,0,0,0,{p95},0,0,0,0,0,10,0,0,0,0,0,0\n"


# A stepped run: two baseline ticks (users=1), then two ticks at 5 users.
# Probe p50 goes 100 -> 100 -> 200 -> 300; p95 goes 110 -> 130 -> 330 -> 440.
_GOLDEN_HISTORY = (
    _HEADER
    + _row(1000, 0, "Aggregated", "N/A", "N/A")
    + _row(1001, 1, "probe:/dashboard", "100", "110")
    + _row(1001, 1, "probe:query [Main:bar]", "100", "130")
    + _row(1001, 1, "Aggregated", "100", "120")
    + _row(1002, 1, "probe:/dashboard", "100", "130")
    + _row(1002, 1, "Aggregated", "100", "130")
    + _row(1062, 5, "probe:/dashboard", "200", "330")
    + _row(1062, 5, "bg-request", "180", "300")
    + _row(1062, 5, "Aggregated", "190", "310")
    + _row(1063, 5, "probe:/dashboard", "300", "440")
    + _row(1063, 5, "Aggregated", "280", "400")
)


class TestParseStatsHistory:
    def test_should_parse_rows_with_na_as_none(self):
        rows = parse_stats_history(_GOLDEN_HISTORY)
        assert rows[0] == {"t": 1000, "users": 0, "name": "Aggregated", "p50": None, "p95": None}
        assert rows[1]["p50"] == 100.0

    def test_should_skip_malformed_rows(self):
        assert parse_stats_history(_HEADER + "garbage,row\n") == []


class TestProbeSeries:
    def test_should_median_across_probe_names_per_tick(self):
        points = probe_series(parse_stats_history(_GOLDEN_HISTORY))
        # tick 1001 has two probe rows (p95 110 and 130) -> median 120
        first = points[0]
        assert first["t"] == 1001
        assert first["users"] == 1
        assert first["p50"] == 100.0
        assert first["p95"] == 120.0

    def test_should_exclude_background_and_aggregated_rows(self):
        points = probe_series(parse_stats_history(_GOLDEN_HISTORY))
        # tick 1062: probe p50 is 200; the bg-request's 180 must not bleed in
        at_1062 = next(p for p in points if p["t"] == 1062)
        assert at_1062["p50"] == 200.0

    def test_should_exclude_probe_login_rows(self):
        """The probe logs in once, inside the baseline window — its slow auth
        rows must not inflate the baseline (first staging run read later
        steps as 0.4x baseline because of this)."""
        history = (
            _HEADER
            + _row(1001, 1, "probe:/login [POST]", "150", "150")
            + _row(1001, 1, "probe:/dashboard", "100", "110")
        )
        points = probe_series(parse_stats_history(history))
        assert points == [{"t": 1001, "users": 1, "p50": 100.0, "p95": 110.0}]


class TestComputeBaseline:
    def test_should_median_each_name_over_the_single_user_window(self):
        baseline = compute_baseline(parse_stats_history(_GOLDEN_HISTORY))
        # /dashboard: p50 median(100,100)=100, p95 median(110,130)=120;
        # query [Main:bar]: single sample 100/130.
        assert baseline["per_name"] == {
            "probe:/dashboard": {"p50_ms": 100.0, "p95_ms": 120.0},
            "probe:query [Main:bar]": {"p50_ms": 100.0, "p95_ms": 130.0},
        }
        # Display summary = median of per-name medians; samples = p50 count.
        assert baseline["p50_ms"] == 100.0
        assert baseline["p95_ms"] == 125.0
        assert baseline["samples"] == 3

    def test_should_return_none_without_single_user_samples(self):
        """Classic runs (no forced 1-user window) get no baseline — raw
        series only, never a made-up denominator."""
        history = _HEADER + _row(1000, 5, "probe:/dashboard", "100", "110")
        assert compute_baseline(parse_stats_history(history)) is None

    def test_should_exclude_login_rows_from_baseline(self):
        history = (
            _HEADER
            + _row(1001, 1, "probe:/login [POST]", "150", "150")
            + _row(1001, 1, "probe:/dashboard", "100", "110")
        )
        baseline = compute_baseline(parse_stats_history(history))
        assert set(baseline["per_name"]) == {"probe:/dashboard"}


class TestDegradationSeries:
    def test_should_normalize_each_name_to_its_own_baseline(self):
        rows = parse_stats_history(_GOLDEN_HISTORY)
        degradation = degradation_series(rows, compute_baseline(rows))
        last = degradation[-1]
        assert last["t"] == 1063
        assert last["users"] == 5
        assert last["x50"] == 3.0  # /dashboard 300 / its own baseline 100
        assert last["x95"] == 3.67  # 440 / 120

    def test_baseline_window_itself_reads_about_one(self):
        rows = parse_stats_history(_GOLDEN_HISTORY)
        degradation = degradation_series(rows, compute_baseline(rows))
        assert degradation[0]["x50"] == 1.0

    def test_mix_shift_alone_does_not_move_degradation(self):
        """The bug the first staging run exposed: cheap names dominating
        later ticks must NOT read as a speed-up. Here the expensive name
        (200ms) disappears after baseline and only the cheap one (50ms)
        remains, both at exactly their own baseline speeds — degradation
        must stay 1.0x, not drop to 0.25x."""
        history = (
            _HEADER
            + _row(1001, 1, "probe:slow", "200", "200")
            + _row(1001, 1, "probe:fast", "50", "50")
            + _row(1062, 5, "probe:fast", "50", "50")
        )
        rows = parse_stats_history(history)
        degradation = degradation_series(rows, compute_baseline(rows))
        at_load = next(p for p in degradation if p["users"] == 5)
        assert at_load["x50"] == 1.0

    def test_names_without_baseline_are_skipped(self):
        """A request name first seen under load has no denominator — skip it
        rather than inventing one."""
        history = (
            _HEADER
            + _row(1001, 1, "probe:/dashboard", "100", "110")
            + _row(1062, 5, "probe:new-thing", "500", "600")
        )
        rows = parse_stats_history(history)
        degradation = degradation_series(rows, compute_baseline(rows))
        assert [p["t"] for p in degradation] == [1001]

    def test_should_be_empty_without_baseline(self):
        rows = parse_stats_history(_GOLDEN_HISTORY)
        assert degradation_series(rows, None) == []


class TestDeriveSteps:
    def test_should_band_user_counts_and_fold_prespawn_zero(self):
        steps = derive_steps(aggregate_series(parse_stats_history(_GOLDEN_HISTORY)))
        assert steps == [
            {"users": 1, "from_t": 1001, "to_t": 1002},
            {"users": 5, "from_t": 1062, "to_t": 1063},
        ]


class TestParseResources:
    def test_should_parse_iso_timestamps_and_blank_cells(self):
        csv_text = (
            "timestamp,host_cpu_pct,es_process_cpu_pct\n"
            "1970-01-01T00:16:40+00:00,25.5,\n"
            "not-a-timestamp,1,2\n"
        )
        points = parse_resources(csv_text)
        assert len(points) == 1
        assert points[0]["t"] == 1000
        assert points[0]["host_cpu_pct"] == 25.5
        assert points[0]["es_process_cpu_pct"] is None


class TestBuildReport:
    def test_should_assemble_all_sections(self):
        report = build_report(_GOLDEN_HISTORY, "timestamp,host_cpu_pct\n1970-01-01T00:16:41+00:00,20\n", {"profile": "steps"})
        assert report["meta"] == {"profile": "steps"}
        assert report["baseline"]["p50_ms"] == 100.0
        assert report["degradation"]
        assert report["steps"][0]["users"] == 1
        assert report["resources"][0]["host_cpu_pct"] == 20.0
        assert report["imports"] == []

    def test_should_degrade_gracefully_on_empty_inputs(self):
        report = build_report("", "", None)
        assert report["baseline"] is None
        assert report["probe"] == []
        assert report["degradation"] == []
        assert report["resources"] == []

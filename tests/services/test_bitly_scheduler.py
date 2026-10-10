"""Phase 2 — Bitly in the Report Scheduler.

Covers the two integration surfaces this phase adds:

* the ``report:bitly_clicks`` EMAIL report type — registration, the
  ``build_report_data`` dispatch, and ``_build_bitly_clicks_report``'s table
  shape / applied date window / index scoping / deny-by-default empty scope;
* the ``bitly`` IMPORT dispatch — the ``_CONNECTOR_IMPORT_DISPATCH`` entry
  (config-scoped: no scope flag, trailing ``--days`` window) through
  ``_trigger_scoped_import``, and the Import Data page's
  ``ImportService._build_import_command`` branch.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from asset_management.web.services import report_schedule_service as rss
from asset_management.web.services import report_types_service as rts
from asset_management.web.services.import_service import ImportService


class _FakeES:
    """Minimal es.search stub recording each call, replying with one canned
    response (the bitly builder issues a single aggregation query)."""

    def __init__(self, response=None):
        self.response = response or {"hits": {"hits": []}, "aggregations": {}}
        self.searches = []

    def search(self, *, index, body, **_kw):
        self.searches.append({"index": index, "body": body})
        return self.response


def _bitly_agg_response():
    return {
        "aggregations": {
            "total_clicks": {"value": 42.0},
            "by_link": {
                "buckets": [
                    {
                        "key": "bit.ly/abc123",
                        "total_clicks": {"value": 30.0},
                        "latest": {
                            "hits": {"hits": [{"_source": {"link_title": "Campaign A", "tags": ["flk-north", "spring"]}}]}
                        },
                        "by_day": {
                            "buckets": [
                                {"key_as_string": "2026-07-01", "clicks": {"value": 10.0}},
                                {"key_as_string": "2026-07-05", "clicks": {"value": 20.0}},
                            ]
                        },
                    },
                    {
                        "key": "bit.ly/def456",
                        "total_clicks": {"value": 12.0},
                        "latest": {"hits": {"hits": [{"_source": {"link_title": "Campaign B", "tags": []}}]}},
                        "by_day": {
                            "buckets": [
                                {"key_as_string": "2026-07-02", "clicks": {"value": 12.0}},
                            ]
                        },
                    },
                ]
            },
            "by_day": {
                "buckets": [
                    {"key_as_string": "2026-07-01", "clicks": {"value": 10.0}},
                    {"key_as_string": "2026-07-02", "clicks": {"value": 32.0}},
                ]
            },
        }
    }


class TestBitlyReportType:

    def test_should_register_bitly_clicks_as_a_report_type(self):
        assert rts.REPORT_TYPE_BITLY_CLICKS == "report:bitly_clicks"
        assert rts.REPORT_TYPES[rts.REPORT_TYPE_BITLY_CLICKS] == "Bitly Clicks per Link"
        assert rts.is_report_type("report:bitly_clicks")

    def test_should_dispatch_bitly_report_type_from_build_report_data(self, monkeypatch):
        seen = {}

        def fake_builder(es, scope_titles, date_range):
            seen["scope_titles"] = scope_titles
            seen["date_range"] = date_range
            return {"tables": [], "summary_metrics": {}}

        monkeypatch.setattr(rts, "_build_bitly_clicks_report", fake_builder)
        out = rts.build_report_data(
            object(),
            {"dashboard_id": "report:bitly_clicks"},
            ["Team Alpha"],
            {"from": "2026-07-01", "to": "2026-07-07"},
        )
        assert out == {"tables": [], "summary_metrics": {}}
        assert seen["scope_titles"] == ["Team Alpha"]
        assert seen["date_range"] == {"from": "2026-07-01", "to": "2026-07-07"}

    def test_should_build_per_link_and_per_day_tables_with_the_window_applied(self, monkeypatch):
        monkeypatch.setattr(rts, "_scoped_indices", lambda pattern, _titles: "esid_a__bitly_clicks")

        fake = _FakeES(_bitly_agg_response())
        out = rts._build_bitly_clicks_report(fake, ["Team Alpha"], {"from": "2026-07-01", "to": "2026-07-07"})

        links, days = out["tables"]
        assert links["chart_type"] == "table" and links["result"]["chart_type"] == "table"
        # One column per day of the selected 7-day window (feedback:
        # pivot days across, keep the window total as the last column).
        day_cols = [f"2026-07-{d:02d}" for d in range(1, 8)]
        assert links["result"]["columns"] == ["Link", "Title", "Tags", *day_cols, "Total clicks"]
        assert links["result"]["rows"] == [
            ["bit.ly/abc123", "Campaign A", "flk-north, spring", 10, 0, 0, 0, 20, 0, 0, 30],
            ["bit.ly/def456", "Campaign B", "", 0, 12, 0, 0, 0, 0, 0, 12],
        ]
        assert days["chart_id"] == "report:bitly_clicks:by_day"
        assert days["result"]["columns"] == ["Date", "Clicks"]
        assert days["result"]["rows"] == [["2026-07-01", 10], ["2026-07-02", 32]]
        assert out["summary_metrics"] == {
            "Total clicks": 42,
            "Links": 2,
            "Date range": "2026-07-01 to 2026-07-07",
        }

        # One scoped query, with the schedule window applied to `date` (unlike
        # the backfill report) plus the team_title + record_type filters.
        assert len(fake.searches) == 1
        search = fake.searches[0]
        assert search["index"] == "esid_a__bitly_clicks"
        filters = search["body"]["query"]["bool"]["filter"]
        assert {"range": {"date": {"gte": "2026-07-01", "lte": "2026-07-07"}}} in filters
        assert {"terms": {"team_title": ["Team Alpha"]}} in filters
        assert {"term": {"record_type": "link_clicks"}} in filters

    def test_should_default_to_a_trailing_30_day_window_when_none_resolves(self, monkeypatch):
        monkeypatch.setattr(rts, "_scoped_indices", lambda pattern, _titles: "esid_a__bitly_clicks")
        fake = _FakeES()

        rts._build_bitly_clicks_report(fake, ["Team Alpha"], {})

        rng = fake.searches[0]["body"]["query"]["bool"]["filter"][2]["range"]["date"]
        today = date.today()
        assert rng["lte"] == today.isoformat()
        assert rng["gte"] == (today - timedelta(days=29)).isoformat()

    def test_should_pivot_to_iso_week_columns_when_window_exceeds_a_month(self):
        # 60 days > _BITLY_MAX_DAY_COLUMNS -> week columns, keyed by Monday.
        keys, weekly = rts._bitly_period_keys("2026-06-01", "2026-07-30")
        assert weekly is True
        assert keys[0] == "wk 2026-06-01"  # 2026-06-01 is a Monday
        assert len(keys) == 9  # 60 days spanning 9 ISO weeks
        day_keys, daily = rts._bitly_period_keys("2026-07-01", "2026-07-07")
        assert daily is False
        assert day_keys == [f"2026-07-{d:02d}" for d in range(1, 8)]

    def test_should_compute_empty_and_query_nothing_when_scope_is_empty(self):
        fake = _FakeES()
        out = rts._build_bitly_clicks_report(fake, [], {"from": "2026-07-01", "to": "2026-07-07"})
        assert fake.searches == []
        assert [t["result"]["rows"] for t in out["tables"]] == [[], []]
        assert out["summary_metrics"]["Total clicks"] == 0
        assert out["summary_metrics"]["Links"] == 0

    def test_should_skip_query_when_no_in_scope_bitly_indices_exist(self, monkeypatch):
        monkeypatch.setattr(rts, "_scoped_indices", lambda pattern, _titles: "")
        fake = _FakeES()
        out = rts._build_bitly_clicks_report(fake, ["Team Alpha"], {"from": "2026-07-01", "to": "2026-07-07"})
        assert fake.searches == []
        assert [t["result"]["rows"] for t in out["tables"]] == [[], []]

    def test_should_degrade_to_empty_tables_when_es_search_fails(self, monkeypatch):
        monkeypatch.setattr(rts, "_scoped_indices", lambda pattern, _titles: "esid_a__bitly_clicks")

        class _Boom:
            def search(self, **_kw):
                raise RuntimeError("es down")

        out = rts._build_bitly_clicks_report(_Boom(), ["Team Alpha"], {"from": "2026-07-01", "to": "2026-07-07"})
        assert [t["result"]["rows"] for t in out["tables"]] == [[], []]

    def test_should_mirror_system_reports_in_the_scheduler_template(self):
        import os

        tpl_path = os.path.join(os.path.dirname(rts.__file__), "..", "templates", "report_scheduler.html")
        with open(tpl_path, encoding="utf-8") as fh:
            html = fh.read()
        assert '{ id: "report:bitly_clicks", title: "Bitly Clicks per Link" }' in html


class _FakeProc:
    pid = 4242


class TestBitlyImportDispatch:

    def _capture_argv(self, monkeypatch, date_range):
        captured = {}

        def _fake_popen(argv, **_kw):
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(rss.subprocess, "Popen", _fake_popen)
        result = rss.ReportScheduleService._trigger_scoped_import(
            object(), "bitly", ["team_esid_1", "team_esid_2"], date_range
        )
        return result, captured.get("argv")

    def test_should_dispatch_bitly_without_a_scope_flag_and_with_trailing_days(self, monkeypatch):
        frm = (datetime.now(timezone.utc).date() - timedelta(days=6)).isoformat()
        to = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        result, argv = self._capture_argv(monkeypatch, {"from": frm, "to": to})

        assert result["dispatched"] is True
        assert result["source"] == "bitly"
        assert result["lock_scope"] == ""  # connector-GLOBAL lock (bitly is not per-scope)
        assert argv[:4] == ["python3", "/app/python/import.py", "bitly", "run_daily_imports"]
        # Config-scoped: NO per-team scope flag, and no start/end dates.
        assert not any(a.startswith(("--team_ids", "--limit_teams", "--start_date", "--end_date")) for a in argv)
        # Trailing window reaches back to the window's from-date (7 days ago
        # inclusive of today), regardless of the `to` anchor.
        assert "--days=7" in argv

    def test_should_omit_days_flag_when_no_window_resolves(self, monkeypatch):
        result, argv = self._capture_argv(monkeypatch, {})
        assert result["dispatched"] is True
        assert not any(a.startswith("--days") for a in argv)

    def test_should_compute_trailing_days_from_the_window_from_date(self):
        now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
        days = rss.ReportScheduleService._trailing_days_from({"from": "2026-07-30", "to": "2026-08-04"}, now=now)
        assert days == 7
        assert rss.ReportScheduleService._trailing_days_from({}, now=now) == 0
        assert rss.ReportScheduleService._trailing_days_from({"from": "bogus"}, now=now) == 0
        # A from-date of today still pulls at least one day.
        assert rss.ReportScheduleService._trailing_days_from({"from": "2026-08-05"}, now=now) == 1

    def test_should_register_bitly_lock_and_progress_membership(self):
        # Phase 3 — the importer now takes the connector-global
        # import lock and writes 3-step phase progress (fetch/index/archive),
        # so the Run-now poll can track both signals. It stays OUT of the
        # per-scope set: its dispatch is a single unscoped run (scope_id "").
        assert "bitly" in rss._LOCKED_IMPORT_SOURCES
        assert "bitly" in rss._PROGRESS_IMPORT_SOURCES
        assert "bitly" not in rss._PER_SCOPE_LOCK_SOURCES

    def test_should_use_three_step_progress_denominator_for_bitly(self):
        # Keep in sync with import_scripts/bitly/run_daily_imports._BITLY_STEPS_TOTAL.
        assert rss.ReportScheduleService._progress_total("bitly", 30) == 3


class TestBitlyImportDataCommand:

    def test_should_build_bitly_command_with_days(self):
        cmd = ImportService._build_import_command(source="bitly", days=5)
        assert cmd == ["python3", "/app/python/import.py", "bitly", "run_daily_imports", "--days=5"]

    def test_should_convert_start_date_to_a_trailing_window(self):
        start = (datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d")
        end = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        cmd = ImportService._build_import_command(source="bitly", start_date=start, end_date=end)
        assert "--days=10" in cmd

    def test_should_never_append_team_ids_for_bitly(self):
        cmd = ImportService._build_import_command(source="bitly", days=3, team_ids="team_a,team_b")
        assert not any(a.startswith("--team_ids") for a in cmd)

    def test_should_default_window_when_no_range_given(self):
        cmd = ImportService._build_import_command(source="bitly")
        assert cmd == ["python3", "/app/python/import.py", "bitly", "run_daily_imports"]

    def test_should_register_bitly_data_paths(self):
        paths = ImportService.DATA_PATHS["bitly"]
        assert paths["pending"] == "/app/data/bitly/link_clicks"
        assert paths["imported"] == ["/app/data/bitly/link_clicks_imported"]

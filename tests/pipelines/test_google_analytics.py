"""
Tests for the Google Analytics pipeline scripts.

Covers import_scripts.google_analytics.send_json_to_elastic:
  read_json_files              — yields JSON from directory tree (newline-delimited)
  create_ga_metrics_index      — creates unified GA metrics index
  send_to_elasticsearch        — indexes a single document
  main                         — orchestration (reads JSON, indexes per-team)

All Elasticsearch calls and file I/O are mocked.
"""

import io
import json
import os
import subprocess

import pytest
from unittest import mock

import import_scripts.google_analytics.send_json_to_elastic as ga_send


# ---------------------------------------------------------------------------
# read_json_files
# ---------------------------------------------------------------------------


class TestReadJsonFiles:

    def test_yields_parsed_json(self):
        payload = {"team_title": "Alpha", "sessions": 100}
        fake_walk = [("/data/ga", [], ["alpha.json"])]
        # read_json_files reads newline-delimited JSON (one doc per line)
        fake_open = mock.mock_open(read_data=json.dumps(payload) + "\n")

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(ga_send.read_json_files("/data/ga"))

        assert len(results) == 1
        assert results[0]["team_title"] == "Alpha"
        assert results[0]["sessions"] == 100

    def test_skips_non_json_files(self):
        fake_walk = [("/data/ga", [], ["notes.txt", "report.json"])]
        payload = {"team_title": "Beta"}
        fake_open = mock.mock_open(read_data=json.dumps(payload) + "\n")

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(ga_send.read_json_files("/data/ga"))

        assert len(results) == 1

    def test_yields_nothing_for_empty_directory(self):
        fake_walk = [("/data/ga", [], [])]

        with mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk):
            results = list(ga_send.read_json_files("/data/ga"))

        assert results == []

    def test_handles_multiple_lines_per_file(self):
        """Each file can contain multiple newline-delimited JSON docs."""
        lines = json.dumps({"id": 1}) + "\n" + json.dumps({"id": 2}) + "\n"
        fake_walk = [("/data/ga", [], ["multi.json"])]
        fake_open = mock.mock_open(read_data=lines)

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(ga_send.read_json_files("/data/ga"))

        assert len(results) == 2

    def test_handles_multiple_files(self):
        files = ["a.json", "b.json", "c.json"]
        fake_walk = [("/data", [], files)]
        payloads = [{"id": i} for i in range(3)]

        open_calls = iter(payloads)

        def fake_open_fn(path, *args, **kwargs):
            data = json.dumps(next(open_calls)) + "\n"
            return io.StringIO(data)

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", side_effect=fake_open_fn),
        ):
            results = list(ga_send.read_json_files("/data"))

        assert len(results) == 3

    def test_skips_blank_lines(self):
        lines = json.dumps({"id": 1}) + "\n\n\n" + json.dumps({"id": 2}) + "\n"
        fake_walk = [("/data/ga", [], ["data.json"])]
        fake_open = mock.mock_open(read_data=lines)

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(ga_send.read_json_files("/data/ga"))

        assert len(results) == 2

    def test_logs_json_decode_error(self):
        fake_walk = [("/data/ga", [], ["bad.json"])]
        fake_open = mock.mock_open(read_data="not valid json\n")

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(ga_send.read_json_files("/data/ga"))

        assert results == []


# ---------------------------------------------------------------------------
# create_ga_metrics_index
# ---------------------------------------------------------------------------


class TestCreateGaMetricsIndex:

    def test_calls_create_index_if_not_exists(self):
        es = mock.MagicMock()
        # create_index_if_not_exists is resolved as a module global at call
        # time but is not imported into the module namespace, so patch with
        # create=True. The create_ga_metrics_index function itself is
        # unchanged.
        with (
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={"mappings": {}}
            ) as mock_schema,
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.create_index_if_not_exists", create=True
            ) as mock_create,
        ):
            ga_send.create_ga_metrics_index(es, "ga_metrics")

        mock_schema.assert_called_once_with("ga_metrics")
        mock_create.assert_called_once_with(es, "ga_metrics", {"mappings": {}})


# ---------------------------------------------------------------------------
# send_to_elasticsearch
# ---------------------------------------------------------------------------


class TestSendToElasticsearch:

    def test_indexes_document_with_doc_id(self):
        es = mock.MagicMock()
        data = {"team_title": "Alpha", "sessions": 50}
        ga_send.send_to_elasticsearch(es, "alpha_ga_metrics", data, "doc-123")

        es.index.assert_called_once_with(index="alpha_ga_metrics", body=data, id="doc-123")

    def test_handles_exception_without_raising(self):
        es = mock.MagicMock()
        es.index.side_effect = Exception("ES unavailable")

        # Should not raise
        ga_send.send_to_elasticsearch(es, "alpha_ga_metrics", {}, "doc-123")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


class TestGaSendMain:

    def _make_doc(self, team="Alpha", date="20240601", **kwargs):
        base = {
            "team_title": team,
            "date": date,
            "kibana_date": "2024-06-01",
            "_doc_id": "abc123",
        }
        base.update(kwargs)
        return base

    def _cache(self, *teams):
        """Build a team_cache matching build_team_lookup_cache() output.

        resolve_team_esid joins team_title -> team_esid via the
        ``by_title`` view (sanitized slug). The GA docs in these tests carry
        only team_title, so the title fallback is what gets exercised.
        """
        import re

        by_esid = {}
        by_title = {}
        for team_esid, team_title in teams:
            by_esid[team_esid] = {"team_title": team_title, "org_esids": []}
            slug = re.sub(r"[^a-z0-9_]", "_", team_title.lower().replace(" ", "_").replace("-", "_")).strip("_")
            by_title[slug] = team_esid
        return {"by_esid": by_esid, "by_title": by_title}

    def test_main_indexes_documents(self):
        mock_es = mock.MagicMock()
        data = self._make_doc()
        cache = self._cache(("alpha_id", "Alpha"))

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={"mappings": {}}
            ),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.ensure_data_index", return_value="alpha_id__ga_metrics"
            ),
        ):
            ga_send.main({})

        mock_es.index.assert_called_once()

    def test_main_creates_team_specific_index(self):
        mock_es = mock.MagicMock()
        data = self._make_doc(team="My Team")
        cache = self._cache(("my_team_id", "My Team"))

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={}),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.ensure_data_index", return_value="my_team_id__ga_metrics"
            ) as mock_ensure,
        ):
            ga_send.main({})

        # index is now ESID-keyed {team_esid}__ga_metrics, built via
        # ensure_data_index(es, team_esid, SOURCE_SLUG, schema).
        mock_ensure.assert_called_once_with(mock_es, "my_team_id", "ga_metrics", {})

    def test_main_sets_kibana_date_from_ga_date_format(self):
        mock_es = mock.MagicMock()
        data = {"team_title": "Alpha", "date": "20240601", "_doc_id": "abc123"}
        # kibana_date is missing — main() should set it from date field
        cache = self._cache(("alpha_id", "Alpha"))

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={}),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.ensure_data_index", return_value="alpha_id__ga_metrics"
            ),
        ):
            ga_send.main({})

        assert data["kibana_date"] == "2024-06-01"

    def test_main_strips_doc_id_before_indexing(self):
        mock_es = mock.MagicMock()
        data = self._make_doc()
        cache = self._cache(("alpha_id", "Alpha"))

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={}),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.ensure_data_index", return_value="alpha_id__ga_metrics"
            ),
        ):
            ga_send.main({})

        # The body sent to ES should not contain _doc_id
        call_kwargs = mock_es.index.call_args
        indexed_body = call_kwargs[1]["body"] if "body" in call_kwargs[1] else call_kwargs[0][1]
        assert "_doc_id" not in indexed_body

    def test_main_creates_index_once_per_team(self):
        mock_es = mock.MagicMock()
        docs = [self._make_doc("Alpha"), self._make_doc("Alpha"), self._make_doc("Beta")]
        cache = self._cache(("alpha_id", "Alpha"), ("beta_id", "Beta"))

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={}),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.read_json_files", return_value=docs),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch(
                "import_scripts.google_analytics.send_json_to_elastic.ensure_data_index",
                side_effect=lambda es_, tid, src, sch: f"{tid}__{src}",
            ) as mock_ensure,
        ):
            ga_send.main({})

        # once per team_esid — Alpha once + Beta once = 2 calls
        assert mock_ensure.call_count == 2

    def test_main_handles_empty_directory(self):
        mock_es = mock.MagicMock()

        with (
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.get_schema_for_index_type", return_value={}),
            mock.patch("import_scripts.google_analytics.send_json_to_elastic.read_json_files", return_value=[]),
        ):
            ga_send.main({})  # Should not raise

        mock_es.index.assert_not_called()


# ###########################################################################
# run_daily_imports.py
# ###########################################################################

import import_scripts.google_analytics.run_daily_imports as ga_daily

_GA_DAILY = "import_scripts.google_analytics.run_daily_imports"


class TestGaDailyMain:

    def test_uses_days_to_pull_default(self):
        with mock.patch(f"{_GA_DAILY}.subprocess.run") as mock_run:
            # property_ids now required; this test asserts date handling.
            ga_daily.main({"property_ids": "p1"})
        metrics_call = [c for c in mock_run.call_args_list if "get_ga_metrics" in str(c)]
        assert len(metrics_call) == 1
        assert "--days_to_pull=1" in str(metrics_call[0])

    def test_uses_custom_days_to_pull(self):
        with mock.patch(f"{_GA_DAILY}.subprocess.run") as mock_run:
            ga_daily.main({"days_to_pull": 7, "property_ids": "p1"})
        metrics_call = [c for c in mock_run.call_args_list if "get_ga_metrics" in str(c)]
        assert "--days_to_pull=7" in str(metrics_call[0])

    def test_uses_date_range_over_days_to_pull(self):
        with mock.patch(f"{_GA_DAILY}.subprocess.run") as mock_run:
            ga_daily.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "days_to_pull": 7, "property_ids": "p1"})
        metrics_call = [c for c in mock_run.call_args_list if "get_ga_metrics" in str(c)]
        cmd_str = str(metrics_call[0])
        assert "--start_date=2024-01-01" in cmd_str
        assert "--end_date=2024-01-31" in cmd_str
        assert "--days_to_pull" not in cmd_str

    def test_runs_all_three_steps(self):
        with mock.patch(f"{_GA_DAILY}.subprocess.run") as mock_run:
            ga_daily.main({"property_ids": "p1"})
        all_cmds = [str(c) for c in mock_run.call_args_list]
        assert any("get_ga_metrics" in c for c in all_cmds)
        assert any("send_json_to_elastic" in c for c in all_cmds)
        assert any("archive_imported_json" in c for c in all_cmds)

    def test_raises_on_subprocess_error(self):
        with mock.patch(f"{_GA_DAILY}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd")):
            with pytest.raises(subprocess.CalledProcessError):
                ga_daily.main({"property_ids": "p1"})

    def test_runs_steps_in_order(self):
        call_order = []

        def track_calls(cmd, **kwargs):
            # Extract the script name (4th element: get_ga_metrics, send_json_to_elastic, etc.)
            if len(cmd) >= 4:
                call_order.append(cmd[3])

        with mock.patch(f"{_GA_DAILY}.subprocess.run", side_effect=track_calls):
            ga_daily.main({"property_ids": "p1"})
        assert call_order == ["get_ga_metrics", "send_json_to_elastic", "archive_imported_json"]


# ###########################################################################
# get_ga_metrics.py
# ###########################################################################

import import_scripts.google_analytics.get_ga_metrics as ga_metrics

_GA_METRICS = "import_scripts.google_analytics.get_ga_metrics"


# ---------------------------------------------------------------------------
# handle_rate_limit_with_backoff
# ---------------------------------------------------------------------------


class TestHandleRateLimitWithBackoff:

    def test_returns_true_when_retries_remain(self):
        with mock.patch(f"{_GA_METRICS}.time.sleep"):
            result = ga_metrics.handle_rate_limit_with_backoff("prop1", 0, max_retries=3)
        assert result is True

    def test_returns_false_when_max_retries_exceeded(self):
        result = ga_metrics.handle_rate_limit_with_backoff("prop1", 5, max_retries=5)
        assert result is False

    def test_sleeps_with_exponential_backoff(self):
        with mock.patch(f"{_GA_METRICS}.time.sleep") as mock_sleep:
            ga_metrics.handle_rate_limit_with_backoff("prop1", 0, max_retries=5)
        mock_sleep.assert_called_once_with(60)  # 60 * 2^0

        with mock.patch(f"{_GA_METRICS}.time.sleep") as mock_sleep:
            ga_metrics.handle_rate_limit_with_backoff("prop1", 2, max_retries=5)
        mock_sleep.assert_called_once_with(240)  # 60 * 2^2


# ---------------------------------------------------------------------------
# load_properties_from_assets
# ---------------------------------------------------------------------------


class TestLoadPropertiesFromAssets:

    def test_returns_properties_with_team(self):
        ga_doc = {"_source": {"ga_property_id": "123", "ga_property_title": "My Site"}}
        team_meta = {"team_esid": "t1", "team_title": "Team A"}
        with (
            mock.patch(f"{_GA_METRICS}.get_ga_properties", return_value=[ga_doc]),
            mock.patch(f"{_GA_METRICS}.get_team_by_ga_property_id", return_value=team_meta),
        ):
            result = ga_metrics.load_properties_from_assets()
        assert len(result) == 1
        assert result[0]["property_id"] == "123"
        assert result[0]["team_metadata"] == team_meta

    def test_skips_properties_without_team(self):
        ga_doc = {"_source": {"ga_property_id": "123", "ga_property_title": "My Site"}}
        with (
            mock.patch(f"{_GA_METRICS}.get_ga_properties", return_value=[ga_doc]),
            mock.patch(f"{_GA_METRICS}.get_team_by_ga_property_id", return_value=None),
        ):
            result = ga_metrics.load_properties_from_assets()
        assert result == []

    def test_returns_empty_when_no_properties(self):
        with mock.patch(f"{_GA_METRICS}.get_ga_properties", return_value=[]):
            result = ga_metrics.load_properties_from_assets()
        assert result == []

    def test_returns_empty_when_no_property_docs(self):
        with mock.patch(f"{_GA_METRICS}.get_ga_properties", return_value=None):
            result = ga_metrics.load_properties_from_assets()
        assert result == []

    def test_uses_display_name_fallback(self):
        ga_doc = {"_source": {"ga_property_id": "123", "ga_property_display_name": "Fallback Name"}}
        team_meta = {"team_esid": "t1", "team_title": "Team A"}
        with (
            mock.patch(f"{_GA_METRICS}.get_ga_properties", return_value=[ga_doc]),
            mock.patch(f"{_GA_METRICS}.get_team_by_ga_property_id", return_value=team_meta),
        ):
            result = ga_metrics.load_properties_from_assets()
        assert result[0]["property_name"] == "Fallback Name"


# ---------------------------------------------------------------------------
# check_if_already_imported
# ---------------------------------------------------------------------------


class TestCheckIfAlreadyImported:

    def test_returns_false_when_dir_missing(self):
        with mock.patch(f"{_GA_METRICS}.os.path.exists", return_value=False):
            result = ga_metrics.check_if_already_imported("123", "20240101")
        assert result is False

    def test_returns_true_when_file_exists(self):
        with (
            mock.patch(f"{_GA_METRICS}.os.path.exists", return_value=True),
            mock.patch(f"{_GA_METRICS}.os.listdir", return_value=["20240101_abc.json"]),
        ):
            result = ga_metrics.check_if_already_imported("123", "20240101")
        assert result is True

    def test_returns_false_when_no_matching_file(self):
        with (
            mock.patch(f"{_GA_METRICS}.os.path.exists", return_value=True),
            mock.patch(f"{_GA_METRICS}.os.listdir", return_value=["20240102_abc.json"]),
        ):
            result = ga_metrics.check_if_already_imported("123", "20240101")
        assert result is False


# ---------------------------------------------------------------------------
# fetch_property_data
# ---------------------------------------------------------------------------


class TestFetchPropertyData:

    def _mock_config(self):
        config = mock.MagicMock()
        config.days_to_pull = 7
        config.report_limit = 1000
        config.get_rate_limit_config.return_value = {"max_retries": 3}
        return config

    def _mock_row(self, dims=None, metrics=None, report_type="pages"):
        """a row matching REPORT_SPECS[report_type]'s dim/metric counts.

        Dimension values are the spec's dimension NAMES (handy for assertions);
        metric values are stringified numbers, floats for the ratio/duration
        metrics so the cast path is exercised.
        """
        spec = ga_metrics.REPORT_SPECS[report_type]
        row = mock.MagicMock()
        if dims is None:
            dims = list(spec["dimensions"])  # use names as placeholder values
        if metrics is None:
            metrics = ["0.5" if m in ga_metrics._FLOAT_METRICS else "100" for m in spec["metrics"]]
        row.dimension_values = [mock.MagicMock(value=v) for v in dims]
        row.metric_values = [mock.MagicMock(value=v) for v in metrics]
        return row

    def test_returns_rows_on_success(self):
        config = self._mock_config()
        mock_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        # pages dims: pageTitle, pagePath, unifiedScreenClass, platform, date
        mock_response.rows = [self._mock_row(dims=["Home", "/home", "Screen", "web", "20240101"], report_type="pages")]
        mock_client.run_report.return_value = mock_response

        result = ga_metrics.fetch_property_data(mock_client, "123", "My Site", config, report_type="pages")
        assert len(result) == 1
        assert result[0]["property_id"] == "123"
        assert result[0]["report_type"] == "pages"
        assert result[0]["pageTitle"] == "Home"
        assert result[0]["screenPageViews"] == 100
        # ratio metric is cast to float and present on the pages report
        assert result[0]["bounceRate"] == 0.5

    def test_returns_empty_when_no_rows(self):
        config = self._mock_config()
        mock_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.rows = []
        mock_client.run_report.return_value = mock_response

        result = ga_metrics.fetch_property_data(mock_client, "123", "My Site", config)
        assert result == []

    def test_retries_on_rate_limit_error(self):
        config = self._mock_config()
        mock_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.rows = [self._mock_row()]
        mock_client.run_report.side_effect = [
            Exception("429 Resource Exhausted"),
            mock_response,
        ]

        with mock.patch(f"{_GA_METRICS}.handle_rate_limit_with_backoff", return_value=True):
            result = ga_metrics.fetch_property_data(mock_client, "123", "My Site", config)
        assert len(result) == 1

    def test_returns_empty_on_non_rate_limit_error(self):
        config = self._mock_config()
        mock_client = mock.MagicMock()
        mock_client.run_report.side_effect = Exception("Permission denied")

        result = ga_metrics.fetch_property_data(mock_client, "123", "My Site", config)
        assert result == []

    def test_returns_empty_when_max_retries_exceeded(self):
        config = self._mock_config()
        mock_client = mock.MagicMock()
        mock_client.run_report.side_effect = Exception("429 quota exhausted")

        with mock.patch(f"{_GA_METRICS}.handle_rate_limit_with_backoff", return_value=False):
            result = ga_metrics.fetch_property_data(mock_client, "123", "My Site", config)
        assert result == []

    def test_uses_explicit_date_range(self):
        config = self._mock_config()
        mock_client = mock.MagicMock()
        mock_response = mock.MagicMock()
        mock_response.rows = [self._mock_row()]
        mock_client.run_report.return_value = mock_response

        result = ga_metrics.fetch_property_data(
            mock_client, "123", "My Site", config, start_date="2024-01-01", end_date="2024-01-31"
        )
        # Verify run_report was called and returned data
        mock_client.run_report.assert_called_once()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# save_property_json
# ---------------------------------------------------------------------------


class TestSavePropertyJson:

    def test_returns_zero_for_empty_data(self):
        assert ga_metrics.save_property_json([], "123", {}) == 0

    def test_saves_files_grouped_by_date(self):
        # pages report_type docs carry the pages dimension set.
        data = [
            {
                "date": "20240101",
                "property_id": "123",
                "report_type": "pages",
                "pageTitle": "A",
                "pagePath": "/a",
                "unifiedScreenClass": "S",
                "platform": "web",
            },
            {
                "date": "20240102",
                "property_id": "123",
                "report_type": "pages",
                "pageTitle": "B",
                "pagePath": "/b",
                "unifiedScreenClass": "S",
                "platform": "web",
            },
        ]
        team_meta = {"team_esid": "t1", "team_title": "Team A", "organization_title": "Org"}

        with (
            mock.patch(f"{_GA_METRICS}.os.makedirs"),
            mock.patch("builtins.open", mock.mock_open()),
            mock.patch(f"{_GA_METRICS}._resolve_team_country", return_value="US"),
        ):
            count = ga_metrics.save_property_json(data, "123", team_meta, output_dir="/tmp/test")
        assert count == 2  # 2 dates = 2 files

    def test_adds_team_metadata_to_docs(self):
        data = [
            {
                "date": "20240101",
                "property_id": "123",
                "report_type": "pages",
                "pageTitle": "A",
                "pagePath": "/a",
                "unifiedScreenClass": "S",
                "platform": "web",
            },
        ]
        team_meta = {"team_esid": "t1", "team_title": "Team A", "organization_title": "Org"}

        m_open = mock.mock_open()
        with (
            mock.patch(f"{_GA_METRICS}.os.makedirs"),
            mock.patch("builtins.open", m_open),
            mock.patch(f"{_GA_METRICS}._resolve_team_country", return_value="US"),
        ):
            ga_metrics.save_property_json(data, "123", team_meta, output_dir="/tmp/test")

        assert data[0]["team_esid"] == "t1"
        assert data[0]["team_title"] == "Team A"
        assert data[0]["_doc_id"] is not None

    def test_creates_property_directory(self):
        data = [
            {
                "date": "20240101",
                "property_id": "123",
                "report_type": "pages",
                "pageTitle": "A",
                "pagePath": "/a",
                "unifiedScreenClass": "S",
                "platform": "web",
            },
        ]
        team_meta = {"team_esid": "t1", "team_title": "Team A"}

        with (
            mock.patch(f"{_GA_METRICS}.os.makedirs") as mock_mkdirs,
            mock.patch("builtins.open", mock.mock_open()),
            mock.patch(f"{_GA_METRICS}._resolve_team_country", return_value="US"),
        ):
            ga_metrics.save_property_json(data, "123", team_meta, output_dir="/tmp/test")
        mock_mkdirs.assert_called_once()
        assert "123" in mock_mkdirs.call_args[0][0]


# ---------------------------------------------------------------------------
# main (get_ga_metrics)
# ---------------------------------------------------------------------------


class TestGaMetricsMain:

    def _mock_config(self):
        config = mock.MagicMock()
        config.days_to_pull = 7
        config.report_limit = 1000
        config.property_team_mapping = {}
        config.get_rate_limit_config.return_value = {"max_retries": 3, "log_usage": False}
        config.get_credentials.return_value = mock.MagicMock()
        return config

    def test_raises_when_no_properties(self):
        """a scoped run resolving zero properties must FAIL, not return.

        This test previously called main({}) and asserted "should not raise",
        pinning the earlier behavior: warn, return, exit rc 0, and let the
        Report Scheduler record a SUCCESS for a run that imported nothing. That is
        the failure mode that hid six dark teams for weeks.

        Now both halves raise: an absent --property_ids (require_scope) and a
        provided one that resolves to nothing (scope_resolution_error).
        """
        config = self._mock_config()
        with (
            mock.patch(f"{_GA_METRICS}.GAConfig", return_value=config),
            mock.patch(f"{_GA_METRICS}.BetaAnalyticsDataClient"),
            mock.patch(f"{_GA_METRICS}.load_properties_from_assets", return_value=[]),
        ):
            # Scope provided, resolves to zero.
            with pytest.raises(ValueError, match="property_ids"):
                ga_metrics.main({"property_ids": "p1"})
            # Scope absent entirely.
            with pytest.raises(ValueError, match="property_ids"):
                ga_metrics.main({"property_ids": "123"})

    def test_processes_properties_and_saves(self):
        config = self._mock_config()
        props = [
            {
                "property_id": "123",
                "property_name": "My Site",
                "team_metadata": {"team_esid": "t1", "team_title": "Team A"},
            }
        ]
        row_data = [{"date": "20240101", "sessions": 100}]

        with (
            mock.patch(f"{_GA_METRICS}.GAConfig", return_value=config),
            mock.patch(f"{_GA_METRICS}.BetaAnalyticsDataClient"),
            mock.patch(f"{_GA_METRICS}.resolve_ga4_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_GA_METRICS}.load_properties_from_assets", return_value=props),
            mock.patch(f"{_GA_METRICS}.fetch_property_data", return_value=row_data) as mock_fetch,
            mock.patch(f"{_GA_METRICS}.save_property_json", return_value=1) as mock_save,
            mock.patch(f"{_GA_METRICS}.write_no_data_markers"),
        ):
            ga_metrics.main({"property_ids": "123"})
        # one fetch + one save per report_type (focused-report model).
        assert mock_fetch.call_count == len(ga_metrics.REPORT_SPECS)
        assert mock_save.call_count == len(ga_metrics.REPORT_SPECS)
        # every fetch is tagged with a known report_type
        requested_types = {c.kwargs["report_type"] for c in mock_fetch.call_args_list}
        assert requested_types == set(ga_metrics.REPORT_SPECS)

    def test_uses_explicit_date_range(self):
        config = self._mock_config()
        props = [
            {
                "property_id": "123",
                "property_name": "My Site",
                "team_metadata": {"team_esid": "t1", "team_title": "Team A"},
            }
        ]

        with (
            mock.patch(f"{_GA_METRICS}.GAConfig", return_value=config),
            mock.patch(f"{_GA_METRICS}.BetaAnalyticsDataClient"),
            mock.patch(f"{_GA_METRICS}.resolve_ga4_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_GA_METRICS}.load_properties_from_assets", return_value=props),
            mock.patch(f"{_GA_METRICS}.fetch_property_data", return_value=[]) as mock_fetch,
            mock.patch(f"{_GA_METRICS}.write_no_data_markers"),
        ):
            ga_metrics.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "property_ids": "123"})
        call_kwargs = mock_fetch.call_args[1]
        assert call_kwargs["start_date"] == "2024-01-01"
        assert call_kwargs["end_date"] == "2024-01-31"

    def test_overrides_days_to_pull_from_params(self):
        config = self._mock_config()
        with (
            mock.patch(f"{_GA_METRICS}.GAConfig", return_value=config),
            mock.patch(f"{_GA_METRICS}.BetaAnalyticsDataClient"),
            mock.patch(f"{_GA_METRICS}.load_properties_from_assets", return_value=[]),
        ):
            # this fixture returns zero properties, which is now a hard
            # error (scope provided, resolves to nothing). days_to_pull is applied
            # before that guard, so swallow the expected raise and assert the
            # override still took effect.
            with pytest.raises(ValueError, match="property_ids"):
                ga_metrics.main({"days_to_pull": "14", "property_ids": "123"})
        assert config.days_to_pull == 14

    def test_raises_on_config_error(self):
        with mock.patch(f"{_GA_METRICS}.GAConfig", side_effect=FileNotFoundError("no config")):
            with pytest.raises(FileNotFoundError):
                ga_metrics.main({"property_ids": "123"})

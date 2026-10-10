"""
Tests for the Facebook pipeline scripts.

Covers import_scripts.facebook.send_json_to_elastic:
  read_json_files              — yields JSON from directory tree
  generate_bulk_actions        — bulk index action generator
  main                         — orchestration (mocked ES + bulk)

Covers import_scripts.facebook.get_insights:
  get_known_ad_accounts_for_teams — ES-based ad account pre-filter

All Elasticsearch calls and file I/O are mocked.
SDK packages (facebook_business) are shimmed in conftest.py.
"""

import io
import json
import os
import pytest
from unittest import mock

# Import the modules under test (SDK shims applied by conftest.py)
import import_scripts.facebook.send_json_to_elastic as fb_send
import import_scripts.facebook.get_insights as fb_insights


# ---------------------------------------------------------------------------
# read_json_files
# ---------------------------------------------------------------------------


class TestReadJsonFiles:

    def test_yields_parsed_json_from_directory(self):
        payload = {"team_title": "Alpha", "impressions": 100}
        fake_walk = [("/data/facebook", [], ["alpha.json"])]
        fake_open = mock.mock_open(read_data=json.dumps(payload))

        with (
            mock.patch("import_scripts.facebook.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(fb_send.read_json_files("/data/facebook"))

        assert len(results) == 1
        assert results[0]["team_title"] == "Alpha"

    def test_skips_non_json_files(self):
        fake_walk = [("/data/facebook", [], ["notes.txt", "report.json"])]
        payload = {"team_title": "Beta"}
        fake_open = mock.mock_open(read_data=json.dumps(payload))

        with (
            mock.patch("import_scripts.facebook.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", fake_open),
        ):
            results = list(fb_send.read_json_files("/data/facebook"))

        assert len(results) == 1

    def test_yields_nothing_for_empty_directory(self):
        fake_walk = [("/data/facebook", [], [])]

        with mock.patch("import_scripts.facebook.send_json_to_elastic.os.walk", return_value=fake_walk):
            results = list(fb_send.read_json_files("/data/facebook"))

        assert results == []

    def test_handles_multiple_files(self, *args):
        files = ["a.json", "b.json", "c.json"]
        fake_walk = [("/data", [], files)]
        payloads = [{"id": i} for i in range(3)]

        # Each open() call returns the next payload
        open_calls = iter(payloads)

        def fake_open_fn(path, *args, **kwargs):
            return io.StringIO(json.dumps(next(open_calls)))

        with (
            mock.patch("import_scripts.facebook.send_json_to_elastic.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", side_effect=fake_open_fn),
        ):
            results = list(fb_send.read_json_files("/data"))

        assert len(results) == 3


# ---------------------------------------------------------------------------
# generate_bulk_actions (facebook send_json_to_elastic)
# ---------------------------------------------------------------------------


class TestFbGenerateBulkActions:
    """Updated for ESID-keyed indices + team_cache lookup.

    The importer now takes a team_cache built from de_asset_teams and
    routes data to `{team_esid}__fb_insights` (not `{slug}_fb_insights`).
    Orphan docs (no team_esid AND no matching team_title) are dropped.
    """

    def _make_doc(self, team="Alpha", team_esid=None, **kwargs):
        base = {"team_title": team, "insight_date_start": "2024-01-01", "kibana_date": "2024-01-01"}
        if team_esid is not None:
            base["team_esid"] = team_esid
        base.update(kwargs)
        return base

    def _cache(self, *teams):
        """Build a team_cache shape matching build_team_lookup_cache output."""
        by_esid = {}
        by_title = {}
        for team_esid, team_title in teams:
            by_esid[team_esid] = {"team_title": team_title, "org_esids": []}
            # sanitized title key
            import re

            slug = re.sub(r"[^a-z0-9_]", "_", team_title.lower().replace(" ", "_").replace("-", "_")).strip("_")
            by_title[slug] = team_esid
        return {"by_esid": by_esid, "by_title": by_title}

    def test_yields_index_action(self):
        es = mock.MagicMock()
        doc = self._make_doc(team_esid="alpha_id")
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(
            "import_scripts.facebook.send_json_to_elastic.ensure_data_index", return_value="alpha_id__fb_insights"
        ):
            actions = list(fb_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert len(actions) == 1
        assert actions[0]["_op_type"] == "index"
        assert actions[0]["_source"] is doc

    def test_uses_esid_keyed_index_name(self):
        es = mock.MagicMock()
        doc = self._make_doc(team="My Team", team_esid="my_team_id")
        cache = self._cache(("my_team_id", "My Team"))
        with mock.patch(
            "import_scripts.facebook.send_json_to_elastic.ensure_data_index", return_value="my_team_id__fb_insights"
        ):
            actions = list(fb_send.generate_bulk_actions([doc], {}, es, {}, cache))
        # index name is now {team_esid}__fb_insights
        assert actions[0]["_index"] == "my_team_id__fb_insights"

    def test_sets_kibana_date_when_missing(self):
        es = mock.MagicMock()
        doc = {"team_title": "Alpha", "team_esid": "alpha_id", "insight_date_start": "2024-06-01"}
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(
            "import_scripts.facebook.send_json_to_elastic.ensure_data_index", return_value="alpha_id__fb_insights"
        ):
            list(fb_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert doc["kibana_date"] == "2024-06-01"

    def test_creates_index_once_per_team(self):
        es = mock.MagicMock()
        docs = [
            self._make_doc("Alpha", team_esid="alpha_id"),
            self._make_doc("Alpha", team_esid="alpha_id"),
            self._make_doc("Beta", team_esid="beta_id"),
        ]
        cache = self._cache(("alpha_id", "Alpha"), ("beta_id", "Beta"))
        with mock.patch(
            "import_scripts.facebook.send_json_to_elastic.ensure_data_index",
            side_effect=lambda es_, tid, src, sch: f"{tid}__{src}",
        ) as mock_ensure:
            list(fb_send.generate_bulk_actions(docs, {}, es, {}, cache))
        assert mock_ensure.call_count == 2

    def test_drops_orphan_docs(self):
        """AC #7: docs whose team can't be resolved are dropped, not routed to unknown_*."""
        es = mock.MagicMock()
        docs = [
            self._make_doc("Stale Team"),  # no team_esid, no matching by_title
            self._make_doc("Alpha", team_esid="alpha_id"),
        ]
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(
            "import_scripts.facebook.send_json_to_elastic.ensure_data_index", return_value="alpha_id__fb_insights"
        ):
            actions = list(fb_send.generate_bulk_actions(docs, {}, es, {}, cache))
        # Only the Alpha doc made it through; orphan was dropped
        assert len(actions) == 1
        assert actions[0]["_source"]["team_title"] == "Alpha"


# ---------------------------------------------------------------------------
# main (facebook send_json_to_elastic)
# ---------------------------------------------------------------------------


class TestFacebookSendMain:

    def test_main_calls_bulk(self):
        mock_es = mock.MagicMock()
        data = {
            "team_title": "Alpha",
            "team_esid": "alpha_id",
            "insight_date_start": "2024-01-01",
            "kibana_date": "2024-01-01",
        }
        cache = {
            "by_esid": {"alpha_id": {"team_title": "Alpha", "org_esids": []}},
            "by_title": {"alpha": "alpha_id"},
        }

        with (
            mock.patch("import_scripts.facebook.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch(
                "import_scripts.facebook.send_json_to_elastic.get_schema_for_index_type", return_value={"mappings": {}}
            ),
            mock.patch("import_scripts.facebook.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.facebook.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch("import_scripts.facebook.send_json_to_elastic.ensure_data_index", return_value="alpha_id__fb_insights"),
            mock.patch("import_scripts.facebook.send_json_to_elastic.bulk", return_value=(1, [])) as mock_bulk,
        ):
            fb_send.main({})

        mock_bulk.assert_called_once()

    def test_main_logs_bulk_errors(self):
        mock_es = mock.MagicMock()
        data = {"team_title": "Beta", "team_esid": "beta_id", "insight_date_start": "2024-06-01", "kibana_date": "2024-06-01"}
        cache = {
            "by_esid": {"beta_id": {"team_title": "Beta", "org_esids": []}},
            "by_title": {"beta": "beta_id"},
        }

        with (
            mock.patch("import_scripts.facebook.send_json_to_elastic.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.facebook.send_json_to_elastic.get_schema_for_index_type", return_value={}),
            mock.patch("import_scripts.facebook.send_json_to_elastic.read_json_files", return_value=[data]),
            mock.patch("import_scripts.facebook.send_json_to_elastic.build_team_lookup_cache", return_value=cache),
            mock.patch("import_scripts.facebook.send_json_to_elastic.ensure_data_index", return_value="beta_id__fb_insights"),
            mock.patch("import_scripts.facebook.send_json_to_elastic.bulk", return_value=(0, [{"error": "mapping error"}])),
        ):
            fb_send.main({})  # Should not raise


# ---------------------------------------------------------------------------
# get_known_ad_accounts_for_teams
# ---------------------------------------------------------------------------


class TestGetKnownAdAccountsForTeams:

    def _make_page_id_to_team(self, teams):
        """Build page_id_to_team dict from list of (page_id, team_title) tuples."""
        mapping = {}
        for page_id, title in teams:
            mapping[page_id] = {"team_title": title, "_id": f"esid_{title}"}
        return mapping

    def _make_import_params(self, entries):
        """Build import_params list from list of (ad_account_id, fb_app_esid) tuples."""
        return [{"fb_ad_account": {"fb_ad_account_id": acct_id, "fb_app_esid": app_esid}} for acct_id, app_esid in entries]

    def _mapping_keyword(self, indices):
        """Build a get_mapping response where each index has ad_account_id as
        a top-level keyword (the modern shape used by every team index except
        the legacy flkvj_fb_insights)."""
        return {idx: {"mappings": {"properties": {"ad_account_id": {"type": "keyword"}}}} for idx in indices}

    def _mapping_legacy_text_keyword(self, indices):
        """Build a get_mapping response where each index has ad_account_id as
        text + .keyword subfield (the legacy flkvj_fb_insights shape)."""
        return {
            idx: {
                "mappings": {
                    "properties": {
                        "ad_account_id": {
                            "type": "text",
                            "fields": {"keyword": {"type": "keyword"}},
                        }
                    }
                }
            }
            for idx in indices
        }

    def test_should_return_known_accounts_when_es_has_data(self):
        mapping = self._make_page_id_to_team([("111", "FLK North")])
        import_params = self._make_import_params([("12345", "app_A"), ("67890", "app_A")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_keyword(["flk_north_fb_insights"])
        mock_es.search.return_value = {
            "aggregations": {
                "ad_accounts": {
                    "buckets": [
                        {"key": "12345", "doc_count": 50},
                        {"key": "67890", "doc_count": 10},
                    ]
                }
            }
        }

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, import_params)

        assert result == {"12345", "67890"}
        # The agg uses the top-level keyword field path on modern indices.
        agg = mock_es.search.call_args[1]["body"]["aggs"]["ad_accounts"]["terms"]
        assert agg["field"] == "ad_account_id"

    def test_should_use_keyword_subfield_on_legacy_text_index(self):
        """KAN: flkvj_fb_insights pre-dates the keyword schema bump and maps
        ad_account_id as text+keyword. The agg must target the .keyword
        subfield for that index, not the raw text path."""
        mapping = self._make_page_id_to_team([("111", "FLKVJ")])
        import_params = self._make_import_params([("12345", "app_A")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_legacy_text_keyword(["flkvj_fb_insights"])
        mock_es.search.return_value = {"aggregations": {"ad_accounts": {"buckets": [{"key": "12345", "doc_count": 50}]}}}

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, import_params)

        assert result == {"12345"}
        agg = mock_es.search.call_args[1]["body"]["aggs"]["ad_accounts"]["terms"]
        assert agg["field"] == "ad_account_id.keyword"

    def test_should_include_sibling_ad_account_under_same_fb_app(self):
        """When a team's ad account is swapped for a new one under the same FB
        app, the new account must not be filtered out just because it has no
        historical data yet."""
        mapping = self._make_page_id_to_team([("111", "FLKVT")])
        # Old account 12345 has history; new account 99999 is under the same app.
        # Account 77777 is under a different app and must NOT be included.
        import_params = self._make_import_params(
            [
                ("12345", "app_A"),
                ("99999", "app_A"),
                ("77777", "app_B"),
            ]
        )
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_keyword(["flkvt_fb_insights"])
        mock_es.search.return_value = {"aggregations": {"ad_accounts": {"buckets": [{"key": "12345", "doc_count": 50}]}}}

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, import_params)

        assert result == {"12345", "99999"}

    def test_should_return_none_when_no_buckets(self):
        mapping = self._make_page_id_to_team([("111", "TeamX")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_keyword(["teamx_fb_insights"])
        mock_es.search.return_value = {"aggregations": {"ad_accounts": {"buckets": []}}}

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, [])

        assert result is None

    def test_should_return_none_when_es_unavailable(self):
        mapping = self._make_page_id_to_team([("111", "TeamX")])

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=None):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, [])

        assert result is None

    def test_should_return_none_when_es_throws(self):
        mapping = self._make_page_id_to_team([("111", "TeamX")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_keyword(["teamx_fb_insights"])
        mock_es.search.side_effect = Exception("connection timeout")

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, [])

        assert result is None

    def test_should_return_none_when_no_usable_mapping(self):
        """If every index lacks an ad_account_id field (e.g. only no_data
        markers, no real ad docs ever written), the pre-filter should
        cleanly skip rather than running an unfilterable agg."""
        mapping = self._make_page_id_to_team([("111", "TeamX")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = {"teamx_fb_insights": {"mappings": {"properties": {}}}}

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, [])

        assert result is None
        mock_es.search.assert_not_called()

    def test_should_return_none_when_empty_mapping(self):
        result = fb_insights.get_known_ad_accounts_for_teams({}, [])
        assert result is None

    def test_should_query_multiple_indices_for_multiple_teams(self):
        mapping = self._make_page_id_to_team(
            [
                ("111", "FLK North"),
                ("222", "FLK West"),
            ]
        )
        import_params = self._make_import_params([("99999", "app_A")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_keyword(
            [
                "flk_north_fb_insights",
                "flk_west_fb_insights",
            ]
        )
        mock_es.search.return_value = {"aggregations": {"ad_accounts": {"buckets": [{"key": "99999", "doc_count": 5}]}}}

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, import_params)

        # Both indices use the same keyword shape, so they share one agg call.
        assert mock_es.search.call_count == 1
        index_str = mock_es.search.call_args[1]["index"]
        assert "flk_north_fb_insights" in index_str
        assert "flk_west_fb_insights" in index_str
        assert result == {"99999"}

    def test_should_run_separate_aggs_for_mixed_mapping_shapes(self):
        """A mixed pattern (one modern keyword index + one legacy text index)
        must produce two terms aggs — one per field path — and merge the
        bucket keys. Single-agg approach errors out with 'Fielddata is
        disabled' on the text index."""
        mapping = self._make_page_id_to_team(
            [
                ("111", "FLKVJ"),  # legacy text+keyword
                ("222", "FLK West"),  # modern keyword
            ]
        )
        import_params = self._make_import_params([("aaa", "app_A"), ("bbb", "app_B")])
        mock_es = mock.MagicMock()
        # get_mapping returns BOTH shapes.
        modern = self._mapping_keyword(["flk_west_fb_insights"])
        legacy = self._mapping_legacy_text_keyword(["flkvj_fb_insights"])
        mock_es.indices.get_mapping.return_value = {**modern, **legacy}
        # Two search calls — one per group. Side effects in stable order.
        mock_es.search.side_effect = [
            {"aggregations": {"ad_accounts": {"buckets": [{"key": "aaa"}]}}},
            {"aggregations": {"ad_accounts": {"buckets": [{"key": "bbb"}]}}},
        ]

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            result = fb_insights.get_known_ad_accounts_for_teams(mapping, import_params)

        assert result == {"aaa", "bbb"}
        assert mock_es.search.call_count == 2
        # Field paths used across the two calls.
        field_paths = {call.kwargs["body"]["aggs"]["ad_accounts"]["terms"]["field"] for call in mock_es.search.call_args_list}
        assert field_paths == {"ad_account_id", "ad_account_id.keyword"}

    def test_should_use_ignore_unavailable(self):
        mapping = self._make_page_id_to_team([("111", "TeamX")])
        mock_es = mock.MagicMock()
        mock_es.indices.get_mapping.return_value = self._mapping_keyword(["teamx_fb_insights"])
        mock_es.search.return_value = {"aggregations": {"ad_accounts": {"buckets": []}}}

        with mock.patch("import_scripts.facebook.get_insights.get_elasticsearch_client", return_value=mock_es):
            fb_insights.get_known_ad_accounts_for_teams(mapping, [])

        call_kwargs = mock_es.search.call_args[1]
        assert call_kwargs.get("ignore_unavailable") is True
        assert call_kwargs.get("allow_no_indices") is True


# ###########################################################################
# run_daily_imports.py
# ###########################################################################

import import_scripts.facebook.run_daily_imports as fb_daily

_DAILY = "import_scripts.facebook.run_daily_imports"


class TestDailyGenerateDateRange:

    def test_single_day(self):
        result = fb_daily.generate_date_range("2024-01-15", "2024-01-15")
        assert result == ["2024-01-15"]

    def test_multi_day_range(self):
        result = fb_daily.generate_date_range("2024-01-01", "2024-01-03")
        assert result == ["2024-01-01", "2024-01-02", "2024-01-03"]

    def test_end_before_start_returns_empty(self):
        result = fb_daily.generate_date_range("2024-01-05", "2024-01-03")
        assert result == []


class TestDailyRunImportForRange:
    """the per-date subprocess fan-out collapsed into ONE ranged
    get_insights invocation carrying the whole (post-skip) date set."""

    def test_runs_one_subprocess_with_all_dates(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.run_import_for_range(["2024-01-15", "2024-01-17", "2024-01-16"], False)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        # Sorted, comma-joined — non-contiguous sets pass through verbatim.
        assert "--dates=2024-01-15,2024-01-16,2024-01-17" in cmd
        assert "get_insights" in cmd

    def test_appends_log_usage_flag(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.run_import_for_range(["2024-01-15"], True)
        cmd = mock_run.call_args[0][0]
        assert "--log_usage" in cmd

    def test_appends_team_ids(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.run_import_for_range(["2024-01-15"], False, team_ids="t1,t2")
        cmd = mock_run.call_args[0][0]
        assert "--team_ids=t1,t2" in cmd

    def test_no_team_ids_when_none(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.run_import_for_range(["2024-01-15"], False, team_ids=None)
        cmd = mock_run.call_args[0][0]
        assert not any("--team_ids" in arg for arg in cmd)

    def test_force_dates_forwarded_only_for_dates_in_range(self):
        # at day grain: only tail days INSIDE the fetch set are
        # forwarded; force_refetch supersedes and forwards no per-day list.
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.run_import_for_range(["2024-01-15", "2024-01-16"], False, force_dates={"2024-01-16", "2024-02-01"})
        cmd = mock_run.call_args[0][0]
        assert "--force_dates=2024-01-16" in cmd
        assert not any("--force_refetch" in a for a in cmd)

    def test_force_refetch_supersedes_force_dates(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.run_import_for_range(["2024-01-15"], False, force_refetch=True, force_dates={"2024-01-15"})
        cmd = mock_run.call_args[0][0]
        assert "--force_refetch=true" in cmd
        assert not any("--force_dates" in a for a in cmd)

    def test_on_demand_run_uses_cancellable_subprocess(self):
        # with a progress_run_id the ranged pull must be interruptible.
        with (
            mock.patch(f"{_DAILY}._run_subprocess_cancellable") as mock_cancellable,
            mock.patch(f"{_DAILY}.subprocess.run") as mock_run,
        ):
            fb_daily.run_import_for_range(["2024-01-15"], False, progress_run_id="run-1", days_base=2)
        mock_run.assert_not_called()
        cmd = mock_cancellable.call_args[0][0]
        assert "--progress_run_id=run-1" in cmd
        assert "--progress_days_base=2" in cmd


class TestDailyMain:

    # changed ad-stage date dispatch: a SCHEDULED pull (no
    # connection_esid / progress_run_id, which is exactly what these bare
    # main({...}) calls are) now extends the AD stage to also cover the 8-day
    # attribution-maturation tail ending yesterday. So the count of
    # ``get_insights`` calls is no longer ``len(requested dates)`` — it's the
    # union of the requested dates and that moving tail. These tests verify
    # date DISPATCH, so they assert each requested date is among the dispatched
    # ``--date=`` args (stable, intent-preserving) rather than an exact total
    # that drifts with "today". The tail itself is covered by
    # the release-plan tests

    @staticmethod
    def _insight_dates(mock_run):
        """The set of YYYY-MM-DD values passed to get_insights. one
        ranged invocation carries them all via --dates= (comma list)."""
        days = set()
        for c in mock_run.call_args_list:
            cmd = c.args[0] if c.args else c.kwargs.get("args", [])
            if "get_insights" not in cmd:
                continue
            for tok in cmd:
                if isinstance(tok, str) and tok.startswith("--dates="):
                    days.update(d for d in tok.split("=", 1)[1].split(",") if d)
        return days

    def test_uses_specific_dates(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            # team_ids now required; this test asserts date handling.
            fb_daily.main({"dates": "2024-01-01, 2024-01-02", "team_ids": "t1"})
        dispatched = self._insight_dates(mock_run)
        assert {"2024-01-01", "2024-01-02"} <= dispatched

    def test_uses_date_range(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.main({"start_date": "2024-01-01", "end_date": "2024-01-03", "team_ids": "t1"})
        dispatched = self._insight_dates(mock_run)
        assert {"2024-01-01", "2024-01-02", "2024-01-03"} <= dispatched

    def test_uses_single_date(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.main({"date": "2024-06-15", "team_ids": "t1"})
        dispatched = self._insight_dates(mock_run)
        assert "2024-06-15" in dispatched

    def test_defaults_to_yesterday(self):
        import datetime as _dt

        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            # team_ids now required; this test asserts the no-date default.
            fb_daily.main({"team_ids": "t1"})
        dispatched = self._insight_dates(mock_run)
        yesterday = (_dt.date.today() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        assert yesterday in dispatched

    def test_ads_only_skips_page_and_instagram(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.main({"date": "2024-01-15", "ads_only": True, "team_ids": "t1"})
        all_cmds = [str(c) for c in mock_run.call_args_list]
        assert not any("run_page_daily_imports" in c for c in all_cmds)
        assert not any("run_instagram_daily_imports" in c for c in all_cmds)

    def test_non_ads_only_runs_page_and_instagram(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.main({"date": "2024-01-15", "team_ids": "t1"})
        all_cmds = [str(c) for c in mock_run.call_args_list]
        assert any("run_page_daily_imports" in c for c in all_cmds)
        assert any("run_instagram_daily_imports" in c for c in all_cmds)

    def test_passes_team_ids_to_page_imports(self):
        with mock.patch(f"{_DAILY}.subprocess.run") as mock_run:
            fb_daily.main({"date": "2024-01-15", "team_ids": "t1"})
        page_calls = [c for c in mock_run.call_args_list if "run_page_daily_imports" in str(c)]
        assert len(page_calls) == 1
        assert "--team_ids=t1" in str(page_calls[0])

    def test_handles_subprocess_error(self):
        import subprocess

        with mock.patch(f"{_DAILY}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd")):
            fb_daily.main({"date": "2024-01-15", "team_ids": "t1"})  # should not raise

    def test_runs_check_fb_token_first(self):
        call_order = []

        def track_calls(cmd, **kwargs):
            # The script name is the 4th arg (python3, import.py, facebook, <script>).
            # assert on that, not cmd[-1] — scoped runs now append
            # -team_ids to check_fb_token so the last arg is the flag.
            call_order.append(cmd[3] if len(cmd) >= 4 else "")

        with mock.patch(f"{_DAILY}.subprocess.run", side_effect=track_calls):
            fb_daily.main({"date": "2024-01-15", "team_ids": "t1"})
        assert call_order[0] == "check_fb_token"


# ###########################################################################
# run_page_daily_imports.py
# ###########################################################################

import import_scripts.facebook.run_page_daily_imports as fb_page_daily

_PAGE_DAILY = "import_scripts.facebook.run_page_daily_imports"


class TestPageDailyGenerateDateRange:

    def test_single_day(self):
        result = fb_page_daily.generate_date_range("2024-03-01", "2024-03-01")
        assert result == ["2024-03-01"]

    def test_multi_day(self):
        result = fb_page_daily.generate_date_range("2024-03-01", "2024-03-03")
        assert len(result) == 3


class TestPageDailyRunImportForDate:

    def test_runs_get_page_insights(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.run_import_for_date("2024-01-15")
        cmd = mock_run.call_args[0][0]
        assert "get_page_insights" in cmd
        assert "--date=2024-01-15" in cmd

    def test_appends_extra_params(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.run_import_for_date("2024-01-15", {"team_ids": "t1", "fb_page_id": "p1"})
        cmd = mock_run.call_args[0][0]
        assert "--team_ids=t1" in cmd
        assert "--fb_page_id=p1" in cmd

    def test_no_extra_params_when_none(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.run_import_for_date("2024-01-15", None)
        cmd = mock_run.call_args[0][0]
        assert len(cmd) == 5  # python3 /app/... facebook get_page_insights --date=...


class TestPageDailyMain:

    def test_uses_specific_dates(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"dates": "2024-01-01,2024-01-02", "team_ids": "t1"})
        insight_calls = [c for c in mock_run.call_args_list if "get_page_insights" in str(c)]
        assert len(insight_calls) == 2

    def test_uses_date_range(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"start_date": "2024-01-01", "end_date": "2024-01-02", "team_ids": "t1"})
        insight_calls = [c for c in mock_run.call_args_list if "get_page_insights" in str(c)]
        assert len(insight_calls) == 2

    def test_uses_single_date(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"date": "2024-06-15", "team_ids": "t1"})
        insight_calls = [c for c in mock_run.call_args_list if "get_page_insights" in str(c)]
        assert len(insight_calls) == 1

    def test_defaults_to_yesterday(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"team_ids": "t1"})  # scope now mandatory
        insight_calls = [c for c in mock_run.call_args_list if "get_page_insights" in str(c)]
        assert len(insight_calls) == 1

    def test_runs_send_page_json_to_elastic(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"date": "2024-01-15", "team_ids": "t1"})
        all_cmds = [str(c) for c in mock_run.call_args_list]
        assert any("send_page_json_to_elastic" in c for c in all_cmds)

    def test_runs_archive_page_imported_json(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"date": "2024-01-15", "team_ids": "t1"})
        all_cmds = [str(c) for c in mock_run.call_args_list]
        assert any("archive_page_imported_json" in c for c in all_cmds)

    def test_passes_team_ids_to_send_elastic(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"date": "2024-01-15", "team_ids": "t1"})
        send_calls = [c for c in mock_run.call_args_list if "send_page_json_to_elastic" in str(c)]
        assert "--team_ids=t1" in str(send_calls[0])

    def test_passthrough_keys_forwarded(self):
        with mock.patch(f"{_PAGE_DAILY}.subprocess.run") as mock_run:
            fb_page_daily.main({"date": "2024-01-15", "team_ids": "t1", "fb_app_ids": "app1", "fb_page_id": "pg1"})
        insight_calls = [c for c in mock_run.call_args_list if "get_page_insights" in str(c)]
        cmd_str = str(insight_calls[0])
        assert "--fb_app_ids=app1" in cmd_str
        assert "--fb_page_id=pg1" in cmd_str

    def test_handles_subprocess_error(self):
        import subprocess

        with mock.patch(f"{_PAGE_DAILY}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "cmd")):
            fb_page_daily.main({"date": "2024-01-15", "team_ids": "t1"})  # should not raise


# ###########################################################################
# send_page_json_to_elastic.py
# ###########################################################################

import import_scripts.facebook.send_page_json_to_elastic as fb_page_send

_PAGE_SEND = "import_scripts.facebook.send_page_json_to_elastic"


class TestPageReadJsonFiles:

    def test_yields_parsed_json(self):
        payload = {"team_title": "Alpha", "impressions": 100}
        fake_walk = [("/data/page", [], ["a.json"])]
        fake_open = mock.mock_open(read_data=json.dumps(payload))
        with mock.patch(f"{_PAGE_SEND}.os.walk", return_value=fake_walk), mock.patch("builtins.open", fake_open):
            results = list(fb_page_send.read_json_files("/data/page"))
        assert len(results) == 1
        assert results[0]["team_title"] == "Alpha"

    def test_skips_non_json(self):
        fake_walk = [("/data", [], ["notes.txt", "data.json"])]
        fake_open = mock.mock_open(read_data=json.dumps({"id": 1}))
        with mock.patch(f"{_PAGE_SEND}.os.walk", return_value=fake_walk), mock.patch("builtins.open", fake_open):
            results = list(fb_page_send.read_json_files("/data"))
        assert len(results) == 1

    def test_empty_directory(self):
        fake_walk = [("/data", [], [])]
        with mock.patch(f"{_PAGE_SEND}.os.walk", return_value=fake_walk):
            results = list(fb_page_send.read_json_files("/data"))
        assert results == []


class TestPageGenerateBulkActions:
    """mirrors ``TestFbGenerateBulkActions``. Sender now routes
    via team_cache → ``ensure_data_index`` (ESID-keyed).
    """

    def _make_doc(self, team="Alpha", team_esid=None, **kwargs):
        base = {"team_title": team, "date": "2024-01-01"}
        if team_esid is not None:
            base["team_esid"] = team_esid
        base.update(kwargs)
        return base

    def _cache(self, *teams):
        by_esid = {}
        by_title = {}
        for team_esid, team_title in teams:
            by_esid[team_esid] = {"team_title": team_title, "org_esids": []}
            slug = team_title.lower().replace(" ", "_").replace("-", "_")
            slug = "".join(c if c.isalnum() or c == "_" else "_" for c in slug)
            while "__" in slug:
                slug = slug.replace("__", "_")
            by_title[slug] = team_esid
        return {"by_esid": by_esid, "by_title": by_title}

    def test_yields_index_action(self):
        es = mock.MagicMock()
        doc = self._make_doc(team_esid="alpha_id")
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(f"{_PAGE_SEND}.ensure_data_index", return_value="alpha_id__fb_page_insights"):
            actions = list(fb_page_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert len(actions) == 1
        assert actions[0]["_op_type"] == "index"
        assert actions[0]["_index"] == "alpha_id__fb_page_insights"

    def test_uses_esid_keyed_index_name(self):
        es = mock.MagicMock()
        doc = self._make_doc(team="My Team", team_esid="my_team_id")
        cache = self._cache(("my_team_id", "My Team"))
        with mock.patch(f"{_PAGE_SEND}.ensure_data_index", return_value="my_team_id__fb_page_insights"):
            actions = list(fb_page_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert actions[0]["_index"] == "my_team_id__fb_page_insights"

    def test_creates_index_once_per_team(self):
        es = mock.MagicMock()
        docs = [
            self._make_doc("Alpha", team_esid="alpha_id"),
            self._make_doc("Alpha", team_esid="alpha_id"),
            self._make_doc("Beta", team_esid="beta_id"),
        ]
        cache = self._cache(("alpha_id", "Alpha"), ("beta_id", "Beta"))
        with mock.patch(
            f"{_PAGE_SEND}.ensure_data_index", side_effect=lambda es_, tid, src, sch: f"{tid}__{src}"
        ) as mock_ensure:
            list(fb_page_send.generate_bulk_actions(docs, {}, es, {}, cache))
        assert mock_ensure.call_count == 2

    def test_drops_orphan_docs(self):
        """docs whose team can't be resolved are dropped, not routed to unknown_fb_page_insights."""
        es = mock.MagicMock()
        docs = [
            self._make_doc("Stale Team"),  # no team_esid, no matching by_title
            self._make_doc("Alpha", team_esid="alpha_id"),
        ]
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(f"{_PAGE_SEND}.ensure_data_index", return_value="alpha_id__fb_page_insights"):
            actions = list(fb_page_send.generate_bulk_actions(docs, {}, es, {}, cache))
        assert len(actions) == 1
        assert actions[0]["_source"]["team_title"] == "Alpha"


class TestPageSendMain:

    def test_main_calls_bulk(self):
        mock_es = mock.MagicMock()
        data = {"team_title": "Alpha", "team_esid": "alpha_id", "date": "2024-01-01"}
        cache = {
            "by_esid": {"alpha_id": {"team_title": "Alpha", "org_esids": []}},
            "by_title": {"alpha": "alpha_id"},
        }
        with (
            mock.patch(f"{_PAGE_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_PAGE_SEND}.get_schema_for_index_type", return_value={}),
            mock.patch(f"{_PAGE_SEND}.read_json_files", return_value=[data]),
            mock.patch(f"{_PAGE_SEND}.build_team_lookup_cache", return_value=cache),
            mock.patch(f"{_PAGE_SEND}.ensure_data_index", return_value="alpha_id__fb_page_insights"),
            mock.patch(f"{_PAGE_SEND}.bulk", return_value=(1, [])) as mock_bulk,
        ):
            fb_page_send.main({})
        mock_bulk.assert_called_once()

    def test_main_logs_bulk_errors(self):
        mock_es = mock.MagicMock()
        data = {"team_title": "Beta", "team_esid": "beta_id", "date": "2024-01-01"}
        cache = {
            "by_esid": {"beta_id": {"team_title": "Beta", "org_esids": []}},
            "by_title": {"beta": "beta_id"},
        }
        with (
            mock.patch(f"{_PAGE_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_PAGE_SEND}.get_schema_for_index_type", return_value={}),
            mock.patch(f"{_PAGE_SEND}.read_json_files", return_value=[data]),
            mock.patch(f"{_PAGE_SEND}.build_team_lookup_cache", return_value=cache),
            mock.patch(f"{_PAGE_SEND}.ensure_data_index", return_value="beta_id__fb_page_insights"),
            mock.patch(f"{_PAGE_SEND}.bulk", return_value=(0, [{"error": "mapping"}])),
        ):
            fb_page_send.main({})  # should not raise

    def test_main_handles_team_filter(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_PAGE_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_PAGE_SEND}.get_schema_for_index_type", return_value={}),
            mock.patch(f"{_PAGE_SEND}.read_json_files", return_value=[]),
            mock.patch(f"{_PAGE_SEND}.build_team_lookup_cache", return_value={"by_esid": {}, "by_title": {}}),
            mock.patch(f"{_PAGE_SEND}.bulk", return_value=(0, [])),
        ):
            fb_page_send.main({"team_ids": "t1,t2"})  # should not raise

    def test_main_handles_empty_directory(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_PAGE_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_PAGE_SEND}.get_schema_for_index_type", return_value={}),
            mock.patch(f"{_PAGE_SEND}.read_json_files", return_value=[]),
            mock.patch(f"{_PAGE_SEND}.build_team_lookup_cache", return_value={"by_esid": {}, "by_title": {}}),
            mock.patch(f"{_PAGE_SEND}.bulk", return_value=(0, [])),
        ):
            fb_page_send.main({})  # should not raise


# ---------------------------------------------------------------------------
# team-filter pipeline scoping
# ---------------------------------------------------------------------------

import import_scripts.facebook.lib.assets as fb_assets
import import_scripts.facebook.check_fb_token as fb_check_token
import import_scripts.facebook.prewarm_city_geocache as fb_prewarm

_ASSETS = "import_scripts.facebook.lib.assets"


class TestResolveTeamFilterScope:
    """walk team -> pages -> fb_accounts -> fb_apps -> fb_ad_accounts."""

    def test_returns_none_when_team_ids_is_falsy(self):
        assert fb_assets.resolve_team_filter_scope(False) is None
        assert fb_assets.resolve_team_filter_scope("") is None
        assert fb_assets.resolve_team_filter_scope(None) is None

    def test_resolves_full_chain_for_matching_team(self):
        team_doc = {
            "_id": "team-flkvj",
            "_source": {"team_title": "FLKVJ", "team_facebook_page_esids": ["page-1", "page-2"]},
        }
        page_docs = [
            {"_id": "page-1", "_source": {"fb_account_esid": "acct-A"}},
            {"_id": "page-2", "_source": {"fb_account_esid": "acct-A"}},
        ]
        app_docs = [{"_id": "app-1", "_source": {}}, {"_id": "app-2", "_source": {}}]
        ad_account_docs = [{"_id": "adacct-1", "_source": {}}]

        with (
            mock.patch(f"{_ASSETS}.get_fb_teams", return_value=[team_doc]) as m_teams,
            mock.patch(f"{_ASSETS}.get_fb_pages", return_value=page_docs) as m_pages,
            mock.patch(f"{_ASSETS}.get_fb_apps", return_value=app_docs) as m_apps,
            mock.patch(f"{_ASSETS}.get_fb_ad_accounts", return_value=ad_account_docs) as m_adaccts,
        ):
            # match by team ESID (the title-substring fallback was removed).
            scope = fb_assets.resolve_team_filter_scope("team-flkvj")

        assert scope["team_esids"] == {"team-flkvj"}
        assert scope["fb_account_esids"] == {"acct-A"}
        assert scope["fb_app_esids"] == {"app-1", "app-2"}
        assert scope["fb_ad_account_esids"] == {"adacct-1"}
        assert m_teams.call_args.kwargs["filters"] == {"ds_facebook": "true"}
        assert set(m_pages.call_args.kwargs["filters"]["_id"]) == {"page-1", "page-2"}
        assert set(m_apps.call_args.kwargs["filters"]["fb_account_esid"]) == {"acct-A"}
        assert set(m_adaccts.call_args.kwargs["filters"]["fb_app_esid"]) == {"app-1", "app-2"}

    def test_returns_empty_sets_when_no_team_matches(self):
        team_doc = {"_id": "team-other", "_source": {"team_title": "Other Team", "team_facebook_page_esids": []}}
        with mock.patch(f"{_ASSETS}.get_fb_teams", return_value=[team_doc]):
            scope = fb_assets.resolve_team_filter_scope("FLKVJ")
        assert scope["team_esids"] == set()
        assert scope["fb_ad_account_esids"] == set()

    def test_title_match_is_exact_and_escape_hatch_gated(self):
        # the title-substring fallback was removed (it collided on
        # same-title teams across workspaces). Title matching is opt-in via
        # FB_IMPORT_ALLOW_TITLE_MATCH and EXACT-only.
        team_doc = {
            "_id": "team-1",
            "_source": {"team_title": "FLKVJ-North", "team_facebook_page_esids": []},
        }
        base = {k: v for k, v in os.environ.items() if k != "FB_IMPORT_ALLOW_TITLE_MATCH"}
        # Hatch OFF (default): a substring matches nothing.
        with mock.patch.dict(os.environ, base, clear=True), mock.patch(f"{_ASSETS}.get_fb_teams", return_value=[team_doc]):
            scope_off = fb_assets.resolve_team_filter_scope("flkvj")
        assert scope_off["team_esids"] == set()
        # Hatch ON: an EXACT title matches; a substring still does not.
        on_env = dict(base, FB_IMPORT_ALLOW_TITLE_MATCH="1")
        with mock.patch.dict(os.environ, on_env, clear=True), mock.patch(f"{_ASSETS}.get_fb_teams", return_value=[team_doc]):
            scope_exact = fb_assets.resolve_team_filter_scope("FLKVJ-North")
            scope_substr = fb_assets.resolve_team_filter_scope("flkvj")
        assert scope_exact["team_esids"] == {"team-1"}
        assert scope_substr["team_esids"] == set()


class TestSetImportParamsScoping:
    """set_import_params should pass scope-derived _id filters."""

    def test_scope_account_esids_become_id_filter(self):
        scope = {
            "fb_account_esids": {"acct-A"},
            "fb_app_esids": {"app-1"},
            "fb_ad_account_esids": {"adacct-1"},
        }
        with (
            mock.patch("import_scripts.facebook.get_insights.get_fb_accounts", return_value=[]) as m_accts,
            mock.patch("import_scripts.facebook.get_insights.get_fb_apps", return_value=[]),
            mock.patch("import_scripts.facebook.get_insights.get_fb_ad_accounts", return_value=[]),
        ):
            fb_insights.set_import_params(scope=scope)
        assert m_accts.call_args.kwargs["filters"]["_id"] == ["acct-A"]

    def test_no_scope_means_no_id_filter_on_accounts(self):
        with mock.patch("import_scripts.facebook.get_insights.get_fb_accounts", return_value=[]) as m_accts:
            fb_insights.set_import_params()
        assert "_id" not in m_accts.call_args.kwargs["filters"]


class TestCheckFbTokenScoping:
    """check_fb_token only debugs apps reachable from team_ids."""

    def test_no_team_ids_loads_all_apps(self):
        with mock.patch(
            "import_scripts.facebook.check_fb_token.get_all_documents",
            return_value=[],
        ) as m_all:
            fb_check_token.main({})
        m_all.assert_called_once_with("de_asset_fb_apps")

    def test_team_ids_scopes_to_resolved_app_esids(self):
        # follow-up: the scope dict shape now requires team_esids
        # and all_account_wide. A legacy team with apps still narrows the
        # search to its app esids.
        scope = {
            "team_esids": {"FLKVJ"},
            "fb_app_esids": {"app-1", "app-2"},
            "all_account_wide": False,
        }
        with (
            mock.patch("import_scripts.facebook.check_fb_token.refresh_fb_app_user_token", return_value=True),
            mock.patch(f"{_ASSETS}.resolve_team_filter_scope", return_value=scope),
            mock.patch("lib.es_helpers.search_index", return_value={"hits": {"hits": []}}) as m_search,
            mock.patch("import_scripts.facebook.check_fb_token.get_all_documents") as m_all,
        ):
            fb_check_token.main({"team_ids": "FLKVJ"})
        m_search.assert_called_once()
        m_all.assert_not_called()
        called_query = m_search.call_args.kwargs["query"]
        assert sorted(called_query["ids"]["values"]) == sorted(scope["fb_app_esids"])

    def test_empty_scope_strict_mode_aborts(self):
        # when --team_ids is passed but the resolver returns
        # zero teams, REFUSE to fall back to fleet-wide. Pre-fix this
        # was "fall back to checking ALL apps" — the same cross-tenant
        # leak path get_insights.py also closed.
        with (
            mock.patch(
                f"{_ASSETS}.resolve_team_filter_scope",
                return_value={
                    "team_esids": set(),
                    "fb_app_esids": set(),
                    "all_account_wide": False,
                },
            ),
            mock.patch(
                "import_scripts.facebook.check_fb_token.get_all_documents",
                return_value=[],
            ) as m_all,
            mock.patch("import_scripts.facebook.check_fb_token.refresh_fb_app_user_token", return_value=True) as m_refresh,
        ):
            fb_check_token.main({"team_ids": "Nobody"})
        # NEITHER fetching all apps NOR refreshing any token should happen.
        m_all.assert_not_called()
        m_refresh.assert_not_called()


class TestGetAllTeamsByPageIdScoping:
    """ES query carries an _id filter when team_esids is provided."""

    def test_no_team_esids_keeps_only_ds_facebook_filter(self):
        with mock.patch("import_scripts.facebook.get_insights.get_fb_teams", return_value=[]) as m:
            fb_insights.get_all_teams_by_page_id()
        assert m.call_args.kwargs["filters"] == {"ds_facebook": "true"}

    def test_team_esids_become_id_filter_alongside_ds_facebook(self):
        with mock.patch("import_scripts.facebook.get_insights.get_fb_teams", return_value=[]) as m:
            fb_insights.get_all_teams_by_page_id(team_esids={"team-1", "team-2"})
        filters = m.call_args.kwargs["filters"]
        assert filters["ds_facebook"] == "true"
        assert set(filters["_id"]) == {"team-1", "team-2"}


class TestPrewarmCityGeocacheScoping:
    """index pattern derived from team_ids."""

    def test_no_team_ids_uses_global_pattern(self):
        assert fb_prewarm._index_pattern_for_team_ids(None) == "*fb_insights"
        assert fb_prewarm._index_pattern_for_team_ids("") == "*fb_insights"

    def test_team_ids_become_per_team_index_pattern(self):
        assert fb_prewarm._index_pattern_for_team_ids("FLKVJ") == "flkvj*fb_insights"

    def test_multiple_team_ids_become_comma_separated(self):
        result = fb_prewarm._index_pattern_for_team_ids("FLKVJ, Other Team")
        assert result == "flkvj*fb_insights,other_team*fb_insights"

    def test_collect_uses_provided_index_pattern(self):
        es = mock.MagicMock()
        es.search.return_value = {"aggregations": {"by_city": {"buckets": []}}}
        fb_prewarm._collect_fb_targeting_cities(es, index_pattern="flkvj*fb_insights")
        assert es.search.call_args.kwargs["index"] == "flkvj*fb_insights"


# ---------------------------------------------------------------------------
# page-grain deduplicated reach (get_insights_window)
# ---------------------------------------------------------------------------


class TestPageGrainReach:
    """one grain="page" window doc per (team, page), exact per
    account, labeled sum across accounts — mirrors the team grain."""

    _TEAM_FIELDS = {"team_esid": "t1", "team_title": "Team One", "organization_title": "Org"}

    def _gw(self):
        import import_scripts.facebook.get_insights_window as gw

        return gw

    def test_should_group_ads_by_page_and_drop_unmapped(self):
        gw = self._gw()
        page_by_ad = {"a1": "P1", "a2": "P1", "a3": "P2", "a4": None}

        assert gw._pages_of_ads(page_by_ad) == {"P1": ["a1", "a2"], "P2": ["a3"]}
        # Restriction keeps only the listed ads; a4's None page never groups.
        assert gw._pages_of_ads(page_by_ad, ad_ids=["a2", "a4"]) == {"P1": ["a2"]}

    def test_should_reuse_team_reach_when_single_page_covers_team_ads(self):
        gw = self._gw()
        acc = {}
        team_reach = {"insight_reach": 100.0, "insight_frequency": 2.5}

        with mock.patch.object(gw, "_fetch_team_account_reach") as fetch:
            gw._pull_page_reach(
                "act_1",
                "2026-07-01",
                "2026-07-07",
                {"P1": ["a1", "a2"]},
                "t1",
                self._TEAM_FIELDS,
                acc,
                team_account_reach=team_reach,
                team_ad_ids=["a2", "a1"],
            )

        fetch.assert_not_called()
        assert acc[("t1", "P1")]["insight_reach"] == 100.0

    def test_should_pull_per_page_when_team_spans_multiple_pages(self):
        gw = self._gw()
        acc = {}

        with mock.patch.object(
            gw,
            "_fetch_team_account_reach",
            side_effect=[{"insight_reach": 60.0}, {"insight_reach": 40.0}],
        ) as fetch:
            gw._pull_page_reach(
                "act_1",
                "2026-07-01",
                "2026-07-07",
                {"P1": ["a1"], "P2": ["a2"]},
                "t1",
                self._TEAM_FIELDS,
                acc,
                team_account_reach={"insight_reach": 90.0},
                team_ad_ids=["a1", "a2"],
            )

        assert [c.kwargs["ad_ids"] for c in fetch.call_args_list] == [["a1"], ["a2"]]
        assert acc[("t1", "P1")]["insight_reach"] == 60.0
        assert acc[("t1", "P2")]["insight_reach"] == 40.0

    def test_should_flag_cross_account_and_drop_ratios_when_summed(self):
        gw = self._gw()
        acc = {}
        gw._accumulate_page_reach(acc, "t1", self._TEAM_FIELDS, "P1", {"insight_reach": 100.0, "insight_frequency": 2.0})
        gw._accumulate_page_reach(acc, "t1", self._TEAM_FIELDS, "P1", {"insight_reach": 50.0})

        written = {}
        with mock.patch.object(gw, "save_data_to_file", side_effect=lambda doc, path: written.update({path: doc})):
            count = gw._flush_page_grain_docs(acc, "2026-07-01", "2026-07-07", "2026-07-01..2026-07-07", {})

        assert count == 1
        ((path, doc),) = written.items()
        assert doc["insight_reach"] == 150
        assert doc["grain"] == "page"
        assert doc["page_id"] == "P1"
        assert doc["page_reach_accounts"] == 2
        assert doc["page_reach_cross_account"] is True
        assert "insight_frequency" not in doc  # ratios don't sum across accounts
        assert path == gw._page_output_path("t1", "P1", "2026-07-01", "2026-07-07")

    def test_should_write_exact_single_account_doc_with_ratios_and_name(self):
        gw = self._gw()
        acc = {}
        gw._accumulate_page_reach(acc, "t1", self._TEAM_FIELDS, "P1", {"insight_reach": 100.0, "insight_frequency": 2.5})

        written = {}
        with mock.patch.object(gw, "save_data_to_file", side_effect=lambda doc, path: written.update({path: doc})):
            gw._flush_page_grain_docs(acc, "2026-07-01", "2026-07-07", "2026-07-01..2026-07-07", {"P1": "My Page"})

        (doc,) = written.values()
        assert doc["page_reach_cross_account"] is False
        assert doc["insight_frequency"] == 2.5  # single-account exact ratios survive
        assert doc["page_name"] == "My Page"
        assert doc["team_esid"] == "t1"

    def test_should_skip_zero_reach_pairs_on_flush(self):
        gw = self._gw()
        acc = {}
        gw._accumulate_page_reach(acc, "t1", self._TEAM_FIELDS, "P1", {"insight_reach": 0})

        with mock.patch.object(gw, "save_data_to_file") as save:
            count = gw._flush_page_grain_docs(acc, "2026-07-01", "2026-07-07", "2026-07-01..2026-07-07", {})

        assert count == 0
        save.assert_not_called()


class TestAccountWideOwnerGuard:
    """an account-wide team may only claim an ad account it is the
    registered owner of. Anchor case: FLKOO's team doc back-links FLKXP's TC_MO
    ad-account doc; the guard must drop that mapping so FLKOO's index is not
    stamped with FLKXP's ads, while legitimate owners are unaffected."""

    _FLKXP = "0HsphpMBrlUVAhtejKhG"
    _FLKOO = "zU_525sBIm2AdJKu3Wpa"

    # ── pure guard ────────────────────────────────────────────────────────

    def test_should_pass_when_asset_doc_owner_matches_claiming_team(self):
        asset_doc = {"team_esid": self._FLKXP, "fb_ad_account_id": "700000000000007"}
        assert fb_insights._account_wide_owner_ok(asset_doc, self._FLKXP) is True

    def test_should_reject_when_asset_doc_owner_is_a_different_team(self):
        """TC_MO registers to FLKXP; FLKOO claiming it must be rejected."""
        asset_doc = {"team_esid": self._FLKXP, "fb_ad_account_id": "700000000000007"}
        assert fb_insights._account_wide_owner_ok(asset_doc, self._FLKOO) is False

    def test_should_pass_permissively_when_asset_doc_has_no_registered_owner(self):
        """A legacy/unowned asset doc is not evidence of a WRONG owner — blocking
        it would regress account-wide teams whose doc predates the team_esid stamp."""
        assert fb_insights._account_wide_owner_ok({"fb_ad_account_id": "1"}, self._FLKOO) is True
        assert fb_insights._account_wide_owner_ok({"team_esid": ""}, self._FLKOO) is True
        assert fb_insights._account_wide_owner_ok(None, self._FLKOO) is True

    # ── builder integration (ES collaborators mocked) ─────────────────────

    def _team_doc(self, team_id, title, ad_account_esid):
        return {"_id": team_id, "_source": {"team_title": title, "fb_ad_account_esid": ad_account_esid}}

    def test_builder_drops_the_mismatched_team_in_a_solo_run(self):
        # FLKOO's own daily cron run — only FLKOO in scope. It back-links TC_MO's
        # doc, which is registered to FLKXP. Because FLKXP is NOT in this run to
        # overwrite the shared key, a broken guard would leave TC_MO mapped to
        # FLKOO (the real contamination); the working guard drops it entirely,
        # so TC_MO's foreign ads fall through to the "no team match" skip.
        teams = [self._team_doc(self._FLKOO, "FLKOO", "ia8nYZ8BTJnYq0K4D8ap")]
        tc_mo_asset = {"fb_ad_account_id": "700000000000007", "team_esid": self._FLKXP}
        _P = "import_scripts.facebook.get_insights."
        with (
            mock.patch(_P + "get_fb_teams", return_value=teams),
            mock.patch(_P + "_filter_entitled_teams", side_effect=lambda t: t),
            mock.patch(_P + "get_document_by_id", return_value=tc_mo_asset),
        ):
            m = fb_insights.get_account_wide_teams_by_ad_account_id(team_esids=[self._FLKOO])

        # TC_MO is dropped — FLKOO does not own it, so nothing maps to it.
        assert m == {}

    def test_builder_keeps_a_correctly_owned_account_wide_team(self):
        teams = [self._team_doc(self._FLKXP, "FLKXP", "ia8nYZ8BTJnYq0K4D8ap")]
        tc_mo_asset = {"fb_ad_account_id": "700000000000007", "team_esid": self._FLKXP}
        _P = "import_scripts.facebook.get_insights."
        with (
            mock.patch(_P + "get_fb_teams", return_value=teams),
            mock.patch(_P + "_filter_entitled_teams", side_effect=lambda t: t),
            mock.patch(_P + "get_document_by_id", return_value=tc_mo_asset),
        ):
            m = fb_insights.get_account_wide_teams_by_ad_account_id()

        assert m == {"700000000000007": mock.ANY}
        assert m["700000000000007"]["_id"] == self._FLKXP

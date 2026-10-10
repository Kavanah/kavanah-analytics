"""
Tests for the Echo pipeline scripts.

Covers:
  import_scripts.echo.get_conversations:
    get_unix_timestamp      — pure date-to-unix conversion
    sanitize_directory_name — pure string sanitizer
    batch_date_ranges       — pure date batching logic
    get_tenants             — ES lookup (mocked)
    get_active_teams        — ES lookup (mocked)

  import_scripts.echo.send_json_to_elastic:
    generate_bulk_actions   — bulk upsert generator (mocked ES, ESID-keyed)
    main                    — orchestration (mocked ES + bulk)

SDK packages (facebook_business) are shimmed in conftest.py so that
get_conversations.py's transitive import of get_page_insights.py resolves.
"""

import json

import pytest
import requests
from unittest import mock
from datetime import datetime

import import_scripts.echo.get_conversations as echo_get
import import_scripts.echo.send_json_to_elastic as echo_send


# ---------------------------------------------------------------------------
# get_unix_timestamp
# ---------------------------------------------------------------------------

class TestGetUnixTimestamp:

    def test_start_of_day(self):
        ts = echo_get.get_unix_timestamp("2024-01-15")
        dt = datetime(2024, 1, 15, 0, 0, 0)
        assert ts == int(dt.timestamp())

    def test_end_of_day(self):
        ts = echo_get.get_unix_timestamp("2024-01-15", end_of_day=True)
        dt = datetime(2024, 1, 15, 23, 59, 59)
        assert ts == int(dt.timestamp())

    def test_start_lt_end_of_same_day(self):
        ts_start = echo_get.get_unix_timestamp("2024-06-01", end_of_day=False)
        ts_end = echo_get.get_unix_timestamp("2024-06-01", end_of_day=True)
        assert ts_start < ts_end


# ---------------------------------------------------------------------------
# sanitize_directory_name
# ---------------------------------------------------------------------------

class TestSanitizeDirectoryName:

    def test_allows_alphanumeric_and_safe_chars(self):
        result = echo_get.sanitize_directory_name("Team Alpha-1")
        assert result == "Team Alpha-1"

    def test_strips_invalid_chars(self):
        result = echo_get.sanitize_directory_name("Team<>Alpha")
        # < and > are not in the allowed chars
        assert "<" not in result
        assert ">" not in result

    def test_empty_string_returns_empty(self):
        assert echo_get.sanitize_directory_name("") == ""


# ---------------------------------------------------------------------------
# batch_date_ranges
# ---------------------------------------------------------------------------

class TestBatchDateRanges:

    def test_single_chunk_for_range_le_2_days(self):
        ranges = echo_get.batch_date_ranges("2024-01-01", "2024-01-02")
        assert ranges == [("2024-01-01", "2024-01-02")]

    def test_single_day_is_one_chunk(self):
        ranges = echo_get.batch_date_ranges("2024-01-01", "2024-01-01")
        assert ranges == [("2024-01-01", "2024-01-01")]

    def test_longer_range_is_batched(self):
        ranges = echo_get.batch_date_ranges("2024-01-01", "2024-01-10")
        # Each chunk is at most 3 days (start + 2)
        assert len(ranges) > 1
        # First range starts on the requested start date
        assert ranges[0][0] == "2024-01-01"
        # Last range ends on the requested end date
        assert ranges[-1][1] == "2024-01-10"

    def test_no_gaps_between_chunks(self):
        from datetime import datetime, timedelta
        ranges = echo_get.batch_date_ranges("2024-01-01", "2024-01-15")
        for i in range(len(ranges) - 1):
            end_of_chunk = datetime.strptime(ranges[i][1], "%Y-%m-%d")
            start_of_next = datetime.strptime(ranges[i + 1][0], "%Y-%m-%d")
            assert start_of_next == end_of_chunk + timedelta(days=1)


# ---------------------------------------------------------------------------
# get_tenants (ES-dependent)
# ---------------------------------------------------------------------------

class TestGetTenants:

    def test_returns_tenant_list_on_success(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "t1", "_source": {"echo_tenant_title": "Tenant A"}},
                ]
            }
        }
        with mock.patch("import_scripts.echo.get_conversations.get_elasticsearch_client", return_value=mock_es):
            result = echo_get.get_tenants()

        assert len(result) == 1
        assert result[0]["_source"]["echo_tenant_title"] == "Tenant A"

    def test_returns_empty_list_when_es_unavailable(self):
        with mock.patch("import_scripts.echo.get_conversations.get_elasticsearch_client", return_value=None):
            result = echo_get.get_tenants()
        assert result == []

    def test_returns_empty_list_on_es_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        with mock.patch("import_scripts.echo.get_conversations.get_elasticsearch_client", return_value=mock_es):
            result = echo_get.get_tenants()
        assert result == []


# ---------------------------------------------------------------------------
# get_active_teams (ES-dependent)
# ---------------------------------------------------------------------------

class TestGetActiveTeams:

    def _mock_es_with_teams(self, teams):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {"hits": teams}
        }
        return mock_es

    def test_returns_teams_for_tenant(self):
        teams = [{"_id": "team1", "_source": {"team_title": "Alpha"}}]
        mock_es = self._mock_es_with_teams(teams)
        result = echo_get.get_active_teams(mock_es, "tenant-esid-1", "mysubdomain")
        assert len(result) == 1
        assert result[0]["_source"]["team_title"] == "Alpha"

    def test_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("connection failed")
        result = echo_get.get_active_teams(mock_es, "t1", "subdomain")
        assert result == []

    def test_exclude_teams_passed_to_query(self):
        mock_es = self._mock_es_with_teams([])
        echo_get.get_active_teams(mock_es, "t1", "sub", exclude_teams=["teamA"])
        call_body = mock_es.search.call_args[1].get("body") or mock_es.search.call_args[0][1]
        # must_not clause should be present
        assert "must_not" in str(call_body)


# ---------------------------------------------------------------------------
# generate_bulk_actions (echo send_json_to_elastic) — bulk upsert generator
# ---------------------------------------------------------------------------
#
# indices are now ESID-keyed ({team_esid}__echo_conversations) built
# via ensure_data_index, and the team is resolved from team_title against a
# team_cache via resolve_team_esid. Docs whose team_title doesn't resolve are
# DROPPED (no more unknown_echo_conversations fallback).

# sanitize_team_title("Alpha") -> "alpha"; ("My Team") -> "my_team".
_ALPHA_ESID = "team-esid-alpha"
_MYTEAM_ESID = "team-esid-myteam"


def _team_cache():
    """Fake team_cache of the shape resolve_team_esid expects, resolving the
    test team_titles ("Alpha", "My Team") to known ESIDs."""
    return {
        "by_esid": {
            _ALPHA_ESID: {"team_title": "Alpha", "org_esids": []},
            _MYTEAM_ESID: {"team_title": "My Team", "org_esids": []},
        },
        "by_title": {
            "alpha": _ALPHA_ESID,
            "my_team": _MYTEAM_ESID,
        },
    }


class TestEchoGenerateBulkActions:

    def _make_doc(self, doc_id="conv-001", team="Alpha", **kwargs):
        base = {"id": doc_id, "team_title": team, "kibana_date": "2024-01-01", "expiration": "Jan 1 2024"}
        base.update(kwargs)
        return base

    def test_yields_update_action_with_upsert(self):
        es = mock.MagicMock()
        schema = {}
        indices_created = {}
        docs = [self._make_doc()]

        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            actions = list(
                echo_send.generate_bulk_actions(
                    docs, indices_created, es, schema, "Jan 01, 2024", _team_cache()
                )
            )

        assert len(actions) == 1
        assert actions[0]["_op_type"] == "update"
        assert actions[0]["doc_as_upsert"] is True
        assert actions[0]["_id"] == "conv-001"

    def test_sets_expiration_when_missing(self):
        es = mock.MagicMock()
        doc = {"id": "c1", "team_title": "Alpha", "kibana_date": "2024-01-01"}
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            list(echo_send.generate_bulk_actions([doc], {}, es, {}, "Jan 01, 2024 UTC", _team_cache()))
        assert doc["expiration"] == "Jan 01, 2024 UTC"

    def test_sets_kibana_date_from_created_at_when_missing(self):
        # Echo created_at is free-text "MMMM dd, yyyy HH:mm:ss UTC";
        # writer must convert to ISO yyyy-MM-dd so kibana_date matches every
        # other platform.
        es = mock.MagicMock()
        doc = {
            "id": "c1",
            "team_title": "Alpha",
            "created_at": "April 27, 2026 05:23:46 UTC",
            "expiration": "x",
        }
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert doc["kibana_date"] == "2026-04-27"

    def test_normalises_existing_freetext_kibana_date_to_iso(self):
        # existing kibana_date in the legacy free-text format gets
        # rewritten to ISO before the doc is shipped to ES.
        es = mock.MagicMock()
        doc = {
            "id": "c1",
            "team_title": "Alpha",
            "kibana_date": "April 27, 2026 05:23:46 UTC",
            "expiration": "x",
        }
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert doc["kibana_date"] == "2026-04-27"

    def test_passes_through_iso_kibana_date_unchanged(self):
        # Idempotent: a doc that already has an ISO kibana_date is left alone.
        es = mock.MagicMock()
        doc = {"id": "c1", "team_title": "Alpha", "kibana_date": "2026-04-27", "expiration": "x"}
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            actions = list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert doc["kibana_date"] == "2026-04-27"
        assert len(actions) == 1

    def test_drops_doc_when_no_parseable_date(self):
        # tolerant parsing — log and skip docs where neither
        # kibana_date nor created_at/createdAt is parseable.
        es = mock.MagicMock()
        doc = {"id": "c1", "team_title": "Alpha", "created_at": "not-a-date", "expiration": "x"}
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            actions = list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert actions == []
        assert "kibana_date" not in doc or doc["kibana_date"] != "not-a-date"

    def test_falls_back_to_createdAt_iso_when_created_at_absent(self):
        # GraphQL path delivers createdAt (camelCase) as ISO 8601; we should
        # accept it as a fallback when created_at hasn't been built yet.
        es = mock.MagicMock()
        doc = {
            "id": "c1",
            "team_title": "Alpha",
            "createdAt": "2026-04-27T05:23:46Z",
            "expiration": "x",
        }
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert doc["kibana_date"] == "2026-04-27"

    def test_handles_list_doc_id(self):
        es = mock.MagicMock()
        doc = {
            "id": ["conv-001", "conv-002"],
            "team_title": "Alpha",
            "expiration": "x",
            "kibana_date": "2024-06-01",
        }
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ):
            actions = list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert actions[0]["_id"] == "conv-001"

    def test_creates_index_on_first_encounter(self):
        # index is created once per team_esid; the second doc for the
        # same team reuses the cached index name in indices_created.
        es = mock.MagicMock()
        docs = [self._make_doc("c1", "Alpha"), self._make_doc("c2", "Alpha")]
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_ALPHA_ESID}__echo_conversations",
        ) as mock_ensure:
            list(echo_send.generate_bulk_actions(docs, {}, es, {}, "now", _team_cache()))
        assert mock_ensure.call_count == 1

    def test_uses_esid_keyed_index_name(self):
        # _index is {team_esid}__echo_conversations, not the old
        # sanitized-title {my_team}_echo_conversations.
        es = mock.MagicMock()
        doc = self._make_doc(team="My Team")
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index",
            return_value=f"{_MYTEAM_ESID}__echo_conversations",
        ):
            actions = list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert actions[0]["_index"] == f"{_MYTEAM_ESID}__echo_conversations"

    def test_drops_doc_when_team_unresolved(self):
        # a team_title that doesn't resolve to a known ESID is dropped
        # (no more unknown_echo_conversations fallback index).
        es = mock.MagicMock()
        doc = self._make_doc(team="Ghost Team")
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index"
        ) as mock_ensure:
            actions = list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert actions == []
        mock_ensure.assert_not_called()

    def test_drops_doc_when_team_title_missing(self):
        # an empty/missing team_title resolves to None -> doc dropped.
        es = mock.MagicMock()
        doc = {"id": "c1", "team_title": "", "kibana_date": "2024-06-01", "expiration": "x"}
        with mock.patch(
            "import_scripts.echo.send_json_to_elastic.ensure_data_index"
        ) as mock_ensure:
            actions = list(echo_send.generate_bulk_actions([doc], {}, es, {}, "now", _team_cache()))
        assert actions == []
        mock_ensure.assert_not_called()


# ---------------------------------------------------------------------------
# read_json_files_recursively (echo send_json_to_elastic)
# ---------------------------------------------------------------------------

class TestReadJsonFilesRecursively:

    def test_yields_json_from_files(self):
        import io, json
        payload = {"id": "conv-1", "team_title": "Alpha"}
        fake_walk = [("/data/echo", [], ["conv-1.json"])]

        with mock.patch("import_scripts.echo.send_json_to_elastic.os.walk", return_value=fake_walk), \
             mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(payload))):
            results = list(echo_send.read_json_files_recursively("/data/echo"))

        assert len(results) == 1
        assert results[0]["id"] == "conv-1"

    def test_skips_non_json_files(self):
        fake_walk = [("/data/echo", [], ["notes.txt", "data.json"])]
        payload = {"id": "conv-2"}

        with mock.patch("import_scripts.echo.send_json_to_elastic.os.walk", return_value=fake_walk), \
             mock.patch("builtins.open", mock.mock_open(read_data='{"id":"conv-2"}')):
            results = list(echo_send.read_json_files_recursively("/data/echo"))

        assert len(results) == 1

    def test_skips_malformed_json_without_raising(self):
        fake_walk = [("/data/echo", [], ["bad.json"])]
        with mock.patch("import_scripts.echo.send_json_to_elastic.os.walk", return_value=fake_walk), \
             mock.patch("builtins.open", mock.mock_open(read_data="not-json")):
            results = list(echo_send.read_json_files_recursively("/data/echo"))
        assert results == []

    def test_yields_nothing_for_empty_directory(self):
        fake_walk = [("/data/echo", [], [])]
        with mock.patch("import_scripts.echo.send_json_to_elastic.os.walk", return_value=fake_walk):
            results = list(echo_send.read_json_files_recursively("/data/echo"))
        assert results == []


# ---------------------------------------------------------------------------
# main (echo send_json_to_elastic)
# ---------------------------------------------------------------------------

class TestEchoSendMain:

    def test_main_returns_early_when_es_unavailable(self):
        with mock.patch("import_scripts.echo.send_json_to_elastic.get_elasticsearch_client",
                        return_value=None):
            echo_send.main({})  # Should not raise

    def test_main_calls_bulk_with_actions(self):
        # main builds a team_cache via build_team_lookup_cache and
        # passes it into generate_bulk_actions; indices are ESID-keyed.
        mock_es = mock.MagicMock()

        with mock.patch("import_scripts.echo.send_json_to_elastic.get_elasticsearch_client",
                        return_value=mock_es), \
             mock.patch("import_scripts.echo.send_json_to_elastic.get_schema_for_index_type",
                        return_value={"properties": {}}), \
             mock.patch("import_scripts.echo.send_json_to_elastic.build_team_lookup_cache",
                        return_value=_team_cache()), \
             mock.patch("import_scripts.echo.send_json_to_elastic.read_json_files_recursively",
                        return_value=[{"id": "c1", "team_title": "Alpha", "kibana_date": "2024-01-01", "expiration": "x"}]), \
             mock.patch("import_scripts.echo.send_json_to_elastic.ensure_data_index",
                        return_value=f"{_ALPHA_ESID}__echo_conversations"), \
             mock.patch("import_scripts.echo.send_json_to_elastic.bulk",
                        return_value=(1, [])) as mock_bulk:
            echo_send.main({})

        mock_bulk.assert_called_once()

    def test_main_logs_errors_when_bulk_returns_errors(self):
        mock_es = mock.MagicMock()
        error = {"index": {"error": "mapping error"}}

        with mock.patch("import_scripts.echo.send_json_to_elastic.get_elasticsearch_client",
                        return_value=mock_es), \
             mock.patch("import_scripts.echo.send_json_to_elastic.get_schema_for_index_type",
                        return_value={}), \
             mock.patch("import_scripts.echo.send_json_to_elastic.build_team_lookup_cache",
                        return_value=_team_cache()), \
             mock.patch("import_scripts.echo.send_json_to_elastic.read_json_files_recursively",
                        return_value=[{"id": "c1", "team_title": "Alpha", "kibana_date": "2024-01-01", "expiration": "x"}]), \
             mock.patch("import_scripts.echo.send_json_to_elastic.ensure_data_index",
                        return_value=f"{_ALPHA_ESID}__echo_conversations"), \
             mock.patch("import_scripts.echo.send_json_to_elastic.bulk",
                        return_value=(0, [error])):
            echo_send.main({})  # Should not raise even with errors


# ###########################################################################
# get_conversations.py — additional coverage for uncovered functions
# ###########################################################################

_ECHO = "import_scripts.echo.get_conversations"


# ---------------------------------------------------------------------------
# fetch_all_facebook_pages / build_fb_pages_maps
# ---------------------------------------------------------------------------

_FB_PAGE_HITS = [
    {"_id": "pg-1", "_source": {"fb_page_id": "111", "fb_page_slug": "mypage", "fb_page_name": "My Page"}},
    {"_id": "pg-2", "_source": {"fb_page_id": "222", "fb_page_slug": "otherpage", "fb_page_name": "Other Tenant Page"}},
]


class TestBuildFbPagesMaps:

    def test_returns_map_and_esid_mapping(self):
        fb_map, esid_map = echo_get.build_fb_pages_maps(_FB_PAGE_HITS, {"111"})
        assert fb_map["111"] == "pg-1"
        assert fb_map["mypage"] == "pg-1"
        assert fb_map["My Page"] == "pg-1"
        assert esid_map["pg-1"] == "111"

    def test_excludes_pages_outside_allowed_set(self):
        # a page not referenced by the tenant's teams must not
        # enter the referrer-match pool under ANY of its keys.
        fb_map, esid_map = echo_get.build_fb_pages_maps(_FB_PAGE_HITS, {"111"})
        assert "222" not in fb_map
        assert "otherpage" not in fb_map
        assert "Other Tenant Page" not in fb_map
        assert "pg-2" not in esid_map

    def test_empty_allowed_set_yields_empty_maps(self):
        fb_map, esid_map = echo_get.build_fb_pages_maps(_FB_PAGE_HITS, set())
        assert fb_map == {}
        assert esid_map == {}

    def test_fetch_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        assert echo_get.fetch_all_facebook_pages(mock_es) == []


# ---------------------------------------------------------------------------
# fetch_all_youtube_channels / build_yt_channels_map
# ---------------------------------------------------------------------------

_YT_CHANNEL_HITS = [
    {"_id": "ch-1", "_source": {"youtube_channel_title": "My Channel", "youtube_channel_handle": "@mychannel"}},
    {"_id": "ch-2", "_source": {"youtube_channel_title": "Foreign Channel", "youtube_channel_handle": "@foreign"}},
]


class TestBuildYtChannelsMap:

    def test_returns_channel_map(self):
        yt_map = echo_get.build_yt_channels_map(_YT_CHANNEL_HITS, {"ch-1"})
        assert yt_map["My Channel"] == "ch-1"
        assert yt_map["@mychannel"] == "ch-1"
        assert yt_map["mychannel"] == "ch-1"  # without @

    def test_excludes_channels_outside_allowed_set(self):
        # another tenant's channel never enters the pool.
        yt_map = echo_get.build_yt_channels_map(_YT_CHANNEL_HITS, {"ch-1"})
        assert "Foreign Channel" not in yt_map
        assert "@foreign" not in yt_map

    def test_fetch_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("fail")
        assert echo_get.fetch_all_youtube_channels(mock_es) == []


# ---------------------------------------------------------------------------
# get_tenant_team_maps
# ---------------------------------------------------------------------------

class TestGetTenantTeamMaps:

    def _team_hits(self):
        return [
            {"_id": "t2", "_source": {"team_title": "Beta", "team_facebook_page_id": ["111"],
                                      "team_youtube_channel_handle_esids": ["ch-1"]}},
            {"_id": "t1", "_source": {"team_title": "Alpha", "team_facebook_page_id": ["111", "333"],
                                      "team_youtube_channel_handle_esids": []}},
        ]

    def test_builds_maps_scoped_to_tenant_query(self):
        with mock.patch(f"{_ECHO}.get_all_documents_paginated", return_value=self._team_hits()) as fetch:
            fb_map, yt_map = echo_get.get_tenant_team_maps(mock.MagicMock(), "tenant-1")
        # The de_asset_teams query must carry the tenant clause.
        index_arg, query_arg = fetch.call_args[0][0], fetch.call_args[0][1]
        assert index_arg == "de_asset_teams"
        assert {"match": {"echo_tenant_esid": "tenant-1"}} in query_arg["bool"]["must"]
        assert fb_map["333"][1] == "t1"
        assert yt_map["ch-1"][1] == "t2"

    def test_collision_resolves_deterministically(self):
        # Two teams in the tenant share page 111 — first by sorted ESID wins
        # (t1), replacing the old resolver's arbitrary size=1 winner.
        with mock.patch(f"{_ECHO}.get_all_documents_paginated", return_value=self._team_hits()):
            fb_map, _ = echo_get.get_tenant_team_maps(mock.MagicMock(), "tenant-1")
        assert fb_map["111"][1] == "t1"

    def test_returns_empty_on_exception(self):
        with mock.patch(f"{_ECHO}.get_all_documents_paginated", side_effect=Exception("fail")):
            fb_map, yt_map = echo_get.get_tenant_team_maps(mock.MagicMock(), "tenant-1")
        assert fb_map == {}
        assert yt_map == {}


# ---------------------------------------------------------------------------
# normalize_referrer
# ---------------------------------------------------------------------------

class TestNormalizeReferrer:

    def test_returns_none_for_empty(self):
        assert echo_get.normalize_referrer("") == (None, [])
        assert echo_get.normalize_referrer(None) == (None, [])

    def test_lowercases_and_strips(self):
        norm, variants = echo_get.normalize_referrer("  My Page  ")
        assert norm == "my page"

    def test_extracts_facebook_page_from_url(self):
        norm, variants = echo_get.normalize_referrer("https://www.facebook.com/MyPage/")
        assert "mypage" in variants

    def test_extracts_from_mobile_url(self):
        norm, variants = echo_get.normalize_referrer("m.facebook.com/SomePage")
        assert "somepage" in variants

    def test_no_variants_for_plain_text(self):
        norm, variants = echo_get.normalize_referrer("My Page Name")
        assert variants == []


# ---------------------------------------------------------------------------
# _normalize_for_matching / _clean_referrer
# ---------------------------------------------------------------------------

class TestNormalizeForMatching:

    def test_strips_zero_width_chars(self):
        result = echo_get._normalize_for_matching("hello\u200bworld")
        assert result == "helloworld"

    def test_handles_none(self):
        assert echo_get._normalize_for_matching(None) is None

    def test_normalizes_arabic_chars(self):
        # Arabic yeh -> Farsi yeh
        result = echo_get._normalize_for_matching("\u064a")
        assert result == "\u06cc"


class TestCleanReferrer:

    def test_collapses_doubled_apostrophes(self):
        assert echo_get._clean_referrer("İsa''yı Keşfet") == "İsa'yı Keşfet"

    def test_strips_wrapping_quotes(self):
        assert echo_get._clean_referrer("'My Page'") == "My Page"

    def test_handles_none(self):
        assert echo_get._clean_referrer(None) is None

    def test_handles_empty(self):
        assert echo_get._clean_referrer("") == ""


# ---------------------------------------------------------------------------
# find_fb_page_with_fuzzy_matching
# ---------------------------------------------------------------------------

class TestFindFbPageWithFuzzyMatching:

    def test_exact_match(self):
        asset_map = {"My Page": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching("My Page", asset_map)
        assert esid == "pg-1"
        assert match_type == "exact"

    def test_case_insensitive_match(self):
        asset_map = {"My Page": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching("my page", asset_map)
        assert esid == "pg-1"
        assert match_type == "case-insensitive"

    def test_url_extracted_match(self):
        asset_map = {"mypage": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching(
            "https://www.facebook.com/mypage", asset_map
        )
        assert esid == "pg-1"
        assert match_type == "url-extracted"

    def test_no_space_match(self):
        asset_map = {"Way Finder": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching("WayFinder", asset_map)
        assert esid == "pg-1"
        assert match_type == "no-space"

    def test_partial_match_asset_in_referrer(self):
        asset_map = {"Way Finder": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching(
            "Way Finder في العالم", asset_map
        )
        assert esid == "pg-1"
        assert match_type == "partial"

    def test_returns_none_for_no_match(self):
        asset_map = {"Something Else": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching("xyz", asset_map)
        assert esid is None

    def test_returns_none_for_empty_referrer(self):
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching("", {})
        assert esid is None

    def test_handles_sql_escaped_referrer(self):
        asset_map = {"İsa'yı Keşfet": "pg-1"}
        esid, match_type, key = echo_get.find_fb_page_with_fuzzy_matching(
            "'İsa''yı Keşfet'", asset_map
        )
        assert esid == "pg-1"


# ---------------------------------------------------------------------------
# discover_echo_schema
# ---------------------------------------------------------------------------

class TestDiscoverEchoSchema:

    def test_runs_introspection_queries(self):
        resp = mock.MagicMock()
        resp.json.return_value = {"data": {"__type": {"fields": []}}}
        resp.raise_for_status = mock.MagicMock()
        with mock.patch(f"{_ECHO}.requests.post", return_value=resp) as mock_post:
            echo_get.discover_echo_schema("http://echo/graphql", {})
        assert mock_post.call_count == 3

    def test_handles_request_failure(self):
        with mock.patch(f"{_ECHO}.requests.post", side_effect=Exception("fail")):
            echo_get.discover_echo_schema("http://echo/graphql", {})  # should not raise


# ---------------------------------------------------------------------------
# fetch_conversations_graphql
# ---------------------------------------------------------------------------

class TestFetchConversationsGraphql:

    def _mock_response(self, body):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_returns_conversations(self):
        body = {"data": {"conversation": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"id": "c1", "status": "open"}],
        }}}
        with mock.patch(f"{_ECHO}.requests.post", return_value=self._mock_response(body)):
            result = echo_get.fetch_conversations_graphql(
                "http://echo", {}, "2024-01-01", "2024-01-01", {"calls": 0}
            )
        assert len(result) == 1
        assert result[0]["id"] == "c1"

    def test_paginates_with_cursor(self):
        page1 = {"data": {"conversation": {
            "pageInfo": {"hasNextPage": True, "endCursor": "cur1"},
            "nodes": [{"id": "c1"}],
        }}}
        page2 = {"data": {"conversation": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"id": "c2"}],
        }}}
        with mock.patch(f"{_ECHO}.requests.post",
                        side_effect=[self._mock_response(page1), self._mock_response(page2)]):
            result = echo_get.fetch_conversations_graphql(
                "http://echo", {}, "2024-01-01", "2024-01-01", {"calls": 0}
            )
        assert len(result) == 2

    def test_returns_none_on_all_retries_failed(self):
        with mock.patch(f"{_ECHO}.requests.post",
                        side_effect=requests.exceptions.RequestException("fail")), \
             mock.patch(f"{_ECHO}.time.sleep"):
            result = echo_get.fetch_conversations_graphql(
                "http://echo", {}, "2024-01-01", "2024-01-01", {"calls": 0}
            )
        assert result is None

    def test_returns_none_on_graphql_errors(self):
        body = {"errors": [{"message": "bad"}]}
        with mock.patch(f"{_ECHO}.requests.post", return_value=self._mock_response(body)):
            result = echo_get.fetch_conversations_graphql(
                "http://echo", {}, "2024-01-01", "2024-01-01", {"calls": 0}
            )
        assert result is None

    def test_returns_none_on_invalid_json(self):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.raise_for_status = mock.MagicMock()
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        resp.text = "not json"
        with mock.patch(f"{_ECHO}.requests.post", return_value=resp):
            result = echo_get.fetch_conversations_graphql(
                "http://echo", {}, "2024-01-01", "2024-01-01", {"calls": 0}
            )
        assert result is None

    def test_rate_limit_sleep(self):
        body = {"data": {"conversation": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"id": "c1"}],
        }}}
        with mock.patch(f"{_ECHO}.requests.post", return_value=self._mock_response(body)), \
             mock.patch(f"{_ECHO}.time.sleep") as mock_sleep:
            result = echo_get.fetch_conversations_graphql(
                "http://echo", {}, "2024-01-01", "2024-01-01", {"calls": 89}
            )
        mock_sleep.assert_called_once()  # rate limit triggered at 90 calls


# ---------------------------------------------------------------------------
# main (get_conversations) — integration-level tests
# ---------------------------------------------------------------------------

class TestEchoGetConversationsMain:

    def _mock_tenant(self, has_subdomain=True, has_key=True):
        return {
            "_id": "tenant-1",
            "_source": {
                "echo_tenant_subdomain": "acme" if has_subdomain else None,
                "echo_tenant_api_key": "enc-key" if has_key else None,
            },
        }

    def test_requires_scope(self):
        # a scope-less direct invocation must refuse BEFORE any ES
        # connection or Echo API call (mirrors run_daily_imports' gate).
        with mock.patch(f"{_ECHO}.get_elasticsearch_client") as mock_client:
            with pytest.raises(ValueError, match="team-scoped"):
                echo_get.main({"start_date": "2024-01-01", "end_date": "2024-01-01"})
        mock_client.assert_not_called()

    def test_scoped_run_with_no_tenants_raises(self):
        # a scoped run that resolves to no tenant docs is a
        # dangling link, not a silent success.
        mock_es = mock.MagicMock()
        with mock.patch(f"{_ECHO}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_ECHO}._tenant_esids_for_teams", return_value={"tenant-1"}), \
             mock.patch(f"{_ECHO}.get_tenants", return_value=[]):
            with pytest.raises(ValueError):
                echo_get.main({"start_date": "2024-01-01", "end_date": "2024-01-01",
                               "limit_teams": "t-1"})

    def test_returns_early_when_es_unavailable(self):
        with mock.patch(f"{_ECHO}.get_elasticsearch_client", return_value=None):
            with pytest.raises(SystemExit):
                echo_get.main({"start_date": "2024-01-01", "end_date": "2024-01-01",
                               "limit_teams": "t-1"})

    def test_skips_tenant_missing_subdomain(self):
        mock_es = mock.MagicMock()
        with mock.patch(f"{_ECHO}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_ECHO}._tenant_esids_for_teams", return_value={"tenant-1"}), \
             mock.patch(f"{_ECHO}.get_tenants", return_value=[self._mock_tenant(has_subdomain=False)]), \
             mock.patch(f"{_ECHO}.fetch_all_facebook_pages", return_value=[]), \
             mock.patch(f"{_ECHO}.fetch_all_youtube_channels", return_value=[]), \
             mock.patch(f"{_ECHO}.decrypt") as mock_decrypt:
            echo_get.main({"start_date": "2024-01-01", "end_date": "2024-01-01",
                           "limit_teams": "t-1"})
        mock_decrypt.assert_not_called()

    def test_processes_conversation_with_team_match(self):
        mock_es = mock.MagicMock()
        conv = {
            "id": "c1", "status": "open", "referrer": {"name": "My Page"},
            "outcomeTitle": "No Response", "createdAt": "2024-01-15T10:00:00Z",
            "closedAt": None, "user": {"id": "u1", "name": "Agent"},
            "tags": [{"id": "t1", "name": "vip"}],
            "disposition": {"completed": True, "notes": "test", "outcome": {"id": "o1", "title": "No Response"}},
            "client": {"id": "cl1", "name": "Jane", "country": "US", "language": "en",
                       "religion": None, "conversationsCount": 3},
            "webContact": {"utmCampaign": "camp1", "utmSource": "fb", "utmMedium": "social",
                           "utmTerm": None, "url": "http://example.com"},
        }
        team = {"team_title": "Alpha", "team_url": "alpha.com", "team_country": "US",
                "team_city": "NYC", "organization_title": "Org1"}

        fb_page_hits = [{"_id": "pg-1", "_source": {"fb_page_id": "111", "fb_page_name": "My Page"}}]
        with mock.patch(f"{_ECHO}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_ECHO}._tenant_esids_for_teams", return_value={"tenant-1"}), \
             mock.patch(f"{_ECHO}.get_tenants", return_value=[self._mock_tenant()]), \
             mock.patch(f"{_ECHO}.fetch_all_facebook_pages", return_value=fb_page_hits), \
             mock.patch(f"{_ECHO}.fetch_all_youtube_channels", return_value=[]), \
             mock.patch(f"{_ECHO}.get_tenant_team_maps", return_value=({"111": (team, "t-1")}, {})), \
             mock.patch(f"{_ECHO}.decrypt", return_value="key"), \
             mock.patch(f"{_ECHO}.get_active_teams", return_value=[{"_id": "t-1", "_source": team}]), \
             mock.patch(f"{_ECHO}.fetch_conversations_graphql", return_value=[conv]), \
             mock.patch(f"{_ECHO}.resolve_country_to_iso2", return_value="US"), \
             mock.patch(f"{_ECHO}.os.path.exists", return_value=False), \
             mock.patch(f"{_ECHO}.create_directory"), \
             mock.patch(f"{_ECHO}.save_to_file") as mock_save, \
             mock.patch(f"{_ECHO}._resolve_team_country", return_value="US"):
            echo_get.main({"start_date": "2024-01-01", "end_date": "2024-01-01",
                           "limit_teams": "t-1"})
        mock_save.assert_called_once()
        saved_entry = mock_save.call_args[0][2]
        assert saved_entry["team_title"] == "Alpha"
        assert saved_entry["no_response"] == 1

    def test_defaults_to_yesterday_dates(self):
        mock_es = mock.MagicMock()
        with mock.patch(f"{_ECHO}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_ECHO}._tenant_esids_for_teams", return_value={"tenant-1"}), \
             mock.patch(f"{_ECHO}.get_tenants", return_value=[self._mock_tenant(has_subdomain=False)]), \
             mock.patch(f"{_ECHO}.fetch_all_facebook_pages", return_value=[]), \
             mock.patch(f"{_ECHO}.fetch_all_youtube_channels", return_value=[]):
            echo_get.main({"limit_teams": "t-1"})  # should not raise

    def test_errors_when_both_exclude_and_limit_teams(self):
        mock_es = mock.MagicMock()
        with mock.patch(f"{_ECHO}.get_elasticsearch_client", return_value=mock_es):
            with pytest.raises(SystemExit):
                echo_get.main({"start_date": "2024-01-01", "end_date": "2024-01-01",
                               "exclude_teams": "t1", "limit_teams": "t2"})


# ###########################################################################
# get_user_stats.py
# ###########################################################################

import import_scripts.echo.get_user_stats as user_stats

_US = "import_scripts.echo.get_user_stats"


# ---------------------------------------------------------------------------
# parse_hours_online_to_decimal
# ---------------------------------------------------------------------------

class TestParseHoursOnlineToDecimal:

    def test_hours_and_minutes(self):
        assert user_stats.parse_hours_online_to_decimal("3h 27m") == 3.45

    def test_zero_hours(self):
        assert user_stats.parse_hours_online_to_decimal("0h 50m") == 0.83

    def test_hours_only(self):
        assert user_stats.parse_hours_online_to_decimal("2h") == 2.0

    def test_minutes_only(self):
        assert user_stats.parse_hours_online_to_decimal("45m") == 0.75

    def test_ten_hours_zero_minutes(self):
        assert user_stats.parse_hours_online_to_decimal("10h 0m") == 10.0

    def test_empty_string(self):
        assert user_stats.parse_hours_online_to_decimal("") == 0.0

    def test_none(self):
        assert user_stats.parse_hours_online_to_decimal(None) == 0.0

    def test_unparseable(self):
        assert user_stats.parse_hours_online_to_decimal("??") == 0.0


# ---------------------------------------------------------------------------
# get_tenants
# ---------------------------------------------------------------------------

class TestUserStatsGetTenants:

    def test_returns_tenants(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {"hits": [{"_id": "t1", "_source": {"echo_tenant_subdomain": "acme"}}]}
        }
        with mock.patch(f"{_US}.get_elasticsearch_client", return_value=mock_es):
            result = user_stats.get_tenants()
        assert len(result) == 1

    def test_returns_empty_when_es_unavailable(self):
        with mock.patch(f"{_US}.get_elasticsearch_client", return_value=None):
            result = user_stats.get_tenants()
        assert result == []

    def test_returns_empty_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        with mock.patch(f"{_US}.get_elasticsearch_client", return_value=mock_es):
            result = user_stats.get_tenants()
        assert result == []


# ---------------------------------------------------------------------------
# fetch_user_stats_graphql
# ---------------------------------------------------------------------------

class TestFetchUserStatsGraphql:

    def _mock_response(self, body, status=200):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.raise_for_status = mock.MagicMock()
        resp.text = json.dumps(body)
        return resp

    def test_returns_users_on_success(self):
        body = {"data": {"users": {"nodes": [{"id": "1", "name": "Alice"}]}}}
        with mock.patch(f"{_US}.requests.post", return_value=self._mock_response(body)):
            result = user_stats.fetch_user_stats_graphql("http://echo", {}, "2024-01-01", "2024-01-01")
        assert len(result) == 1
        assert result[0]["name"] == "Alice"

    def test_returns_none_on_all_retries_failed(self):
        with mock.patch(f"{_US}.requests.post",
                        side_effect=requests.exceptions.RequestException("fail")), \
             mock.patch(f"{_US}.time.sleep"):
            result = user_stats.fetch_user_stats_graphql("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result is None

    def test_falls_back_on_500(self):
        resp_500 = mock.MagicMock()
        resp_500.status_code = 500
        fallback_body = {"data": {"users": {"nodes": [{"id": "2", "name": "Bob"}]}}}
        with mock.patch(f"{_US}.requests.post", return_value=resp_500), \
             mock.patch(f"{_US}.fetch_user_stats_graphql_fallback",
                        return_value=[{"id": "2", "name": "Bob"}]) as mock_fb:
            result = user_stats.fetch_user_stats_graphql("http://echo", {}, "2024-01-01", "2024-01-01")
        mock_fb.assert_called_once()
        assert result[0]["name"] == "Bob"

    def test_falls_back_on_graphql_errors(self):
        body = {"errors": [{"message": "server bug"}]}
        with mock.patch(f"{_US}.requests.post", return_value=self._mock_response(body)), \
             mock.patch(f"{_US}.fetch_user_stats_graphql_fallback", return_value=[]) as mock_fb:
            user_stats.fetch_user_stats_graphql("http://echo", {}, "2024-01-01", "2024-01-01")
        mock_fb.assert_called_once()

    def test_returns_none_on_invalid_json(self):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.raise_for_status = mock.MagicMock()
        resp.json.side_effect = json.JSONDecodeError("bad", "", 0)
        resp.text = "not json"
        with mock.patch(f"{_US}.requests.post", return_value=resp):
            result = user_stats.fetch_user_stats_graphql("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_user_stats_graphql_fallback
# ---------------------------------------------------------------------------

class TestFetchUserStatsGraphqlFallback:

    def _mock_response(self, body):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_returns_users_on_success(self):
        body = {"data": {"users": {"nodes": [{"id": "1", "name": "Alice"}]}}}
        with mock.patch(f"{_US}.requests.post", return_value=self._mock_response(body)):
            result = user_stats.fetch_user_stats_graphql_fallback("http://echo", {}, "2024-01-01", "2024-01-01")
        assert len(result) == 1

    def test_returns_none_on_exception(self):
        with mock.patch(f"{_US}.requests.post", side_effect=Exception("fail")):
            result = user_stats.fetch_user_stats_graphql_fallback("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result is None

    def test_returns_none_on_graphql_errors(self):
        body = {"errors": [{"message": "still broken"}]}
        with mock.patch(f"{_US}.requests.post", return_value=self._mock_response(body)):
            result = user_stats.fetch_user_stats_graphql_fallback("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_hourly_online_users
# ---------------------------------------------------------------------------

class TestFetchHourlyOnlineUsers:

    def _mock_response(self, body):
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.json.return_value = body
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_aggregates_minutes_per_user(self):
        body = {"data": {"hourlyOnlineUsers": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [
                {"userId": "1", "hour": "2024-01-01T10:00:00Z", "minutes": 30},
                {"userId": "1", "hour": "2024-01-01T11:00:00Z", "minutes": 45},
                {"userId": "2", "hour": "2024-01-01T10:00:00Z", "minutes": 20},
            ],
        }}}
        with mock.patch(f"{_US}.requests.post", return_value=self._mock_response(body)):
            result = user_stats.fetch_hourly_online_users("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result["1"] == 75
        assert result["2"] == 20

    def test_paginates_with_cursor(self):
        page1 = {"data": {"hourlyOnlineUsers": {
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor1"},
            "nodes": [{"userId": "1", "hour": "h1", "minutes": 10}],
        }}}
        page2 = {"data": {"hourlyOnlineUsers": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"userId": "1", "hour": "h2", "minutes": 20}],
        }}}
        with mock.patch(f"{_US}.requests.post",
                        side_effect=[self._mock_response(page1), self._mock_response(page2)]):
            result = user_stats.fetch_hourly_online_users("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result["1"] == 30

    def test_returns_empty_on_request_failure(self):
        with mock.patch(f"{_US}.requests.post", side_effect=Exception("fail")), \
             mock.patch(f"{_US}.time.sleep"):
            result = user_stats.fetch_hourly_online_users("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result == {}

    def test_stops_on_graphql_errors(self):
        body = {"data": {"hourlyOnlineUsers": {}}, "errors": [{"message": "bad"}]}
        with mock.patch(f"{_US}.requests.post", return_value=self._mock_response(body)):
            result = user_stats.fetch_hourly_online_users("http://echo", {}, "2024-01-01", "2024-01-01")
        assert result == {}


# ---------------------------------------------------------------------------
# save_to_file (get_user_stats version)
# ---------------------------------------------------------------------------

class TestUserStatsSaveToFile:

    def test_creates_dir_and_writes_json(self):
        m_open = mock.mock_open()
        with mock.patch(f"{_US}.os.makedirs") as mock_mkdirs, \
             mock.patch("builtins.open", m_open):
            user_stats.save_to_file("/data/stats", "user1.json", {"id": 1})
        mock_mkdirs.assert_called_once()
        m_open.assert_called_once()


# ---------------------------------------------------------------------------
# main (get_user_stats)
# ---------------------------------------------------------------------------

class TestUserStatsMain:

    def _mock_tenant(self, has_subdomain=True, has_key=True):
        return {
            "_id": "tenant-1",
            "_source": {
                "echo_tenant_subdomain": "acme" if has_subdomain else None,
                "echo_tenant_api_key": "enc-key" if has_key else None,
            },
        }

    def test_returns_early_when_no_tenants(self):
        with mock.patch(f"{_US}.get_tenants", return_value=[]):
            user_stats.main({})

    def test_skips_tenant_missing_subdomain(self):
        with mock.patch(f"{_US}.get_tenants", return_value=[self._mock_tenant(has_subdomain=False)]), \
             mock.patch(f"{_US}.decrypt") as mock_decrypt:
            user_stats.main({})
        mock_decrypt.assert_not_called()

    def test_skips_tenant_missing_api_key(self):
        with mock.patch(f"{_US}.get_tenants", return_value=[self._mock_tenant(has_key=False)]), \
             mock.patch(f"{_US}.decrypt") as mock_decrypt:
            user_stats.main({})
        mock_decrypt.assert_not_called()

    def test_skips_tenant_on_graphql_error(self):
        with mock.patch(f"{_US}.get_tenants", return_value=[self._mock_tenant()]), \
             mock.patch(f"{_US}.decrypt", return_value="key"), \
             mock.patch(f"{_US}.fetch_user_stats_graphql", return_value=None), \
             mock.patch(f"{_US}.fetch_hourly_online_users") as mock_hourly:
            user_stats.main({"start_date": "2024-01-01", "end_date": "2024-01-01"})
        mock_hourly.assert_not_called()

    def test_processes_users_and_saves(self):
        users = [{"id": "u1", "name": "Alice", "sentMessagesCount": 5,
                  "hoursOnline": "2h 30m", "claimedConversationsCount": 3,
                  "isScheduledNow": True, "outcomeTotals": [{"value": "resolved", "count": 2}],
                  "lastLoginAt": "2024-01-15T10:00:00Z"}]

        with mock.patch(f"{_US}.get_tenants", return_value=[self._mock_tenant()]), \
             mock.patch(f"{_US}.decrypt", return_value="key"), \
             mock.patch(f"{_US}.fetch_user_stats_graphql", return_value=users), \
             mock.patch(f"{_US}.fetch_hourly_online_users", return_value={"u1": 150}), \
             mock.patch(f"{_US}.save_to_file") as mock_save:
            user_stats.main({"start_date": "2024-01-01", "end_date": "2024-01-01"})

        mock_save.assert_called_once()
        saved_doc = mock_save.call_args[0][2]
        assert saved_doc["user_name"] == "Alice"
        assert saved_doc["hours_online_decimal"] == 2.5
        assert saved_doc["total_minutes_online"] == 150
        assert saved_doc["hours_online_from_hourly"] == 2.5
        assert saved_doc["outcome_totals"] == {"resolved": 2}
        assert "January" in saved_doc["last_login_at"]

    def test_skips_user_with_no_id(self):
        users = [{"name": "NoId"}]
        with mock.patch(f"{_US}.get_tenants", return_value=[self._mock_tenant()]), \
             mock.patch(f"{_US}.decrypt", return_value="key"), \
             mock.patch(f"{_US}.fetch_user_stats_graphql", return_value=users), \
             mock.patch(f"{_US}.fetch_hourly_online_users", return_value={}), \
             mock.patch(f"{_US}.save_to_file") as mock_save:
            user_stats.main({"start_date": "2024-01-01", "end_date": "2024-01-01"})
        mock_save.assert_not_called()

    def test_defaults_to_yesterday_dates(self):
        with mock.patch(f"{_US}.get_tenants", return_value=[]):
            user_stats.main({})  # should not raise

    def test_handles_null_outcome_totals(self):
        users = [{"id": "u1", "name": "Bob", "outcomeTotals": None,
                  "hoursOnline": "", "lastLoginAt": None}]
        with mock.patch(f"{_US}.get_tenants", return_value=[self._mock_tenant()]), \
             mock.patch(f"{_US}.decrypt", return_value="key"), \
             mock.patch(f"{_US}.fetch_user_stats_graphql", return_value=users), \
             mock.patch(f"{_US}.fetch_hourly_online_users", return_value={}), \
             mock.patch(f"{_US}.save_to_file") as mock_save:
            user_stats.main({"start_date": "2024-01-01", "end_date": "2024-01-01"})
        saved_doc = mock_save.call_args[0][2]
        assert saved_doc["outcome_totals"] == {}
        assert saved_doc["last_login_at"] is None
        assert saved_doc["hours_online_decimal"] == 0.0

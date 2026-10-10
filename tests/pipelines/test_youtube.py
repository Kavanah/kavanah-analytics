"""
Tests for the YouTube pipeline scripts.

Covers import_scripts.youtube.get_youtube_analytics:
  get_youtube_accounts    — ES lookup (mocked)
  get_youtube_channels    — ES lookup (mocked)
  load_account_credentials — raises ValueError when token absent

SDK packages (google.oauth2, googleapiclient) are shimmed in conftest.py.
"""

import pytest
from unittest import mock

import import_scripts.youtube.get_youtube_analytics as yt_get


# ---------------------------------------------------------------------------
# get_youtube_accounts
# ---------------------------------------------------------------------------


class TestGetYoutubeAccounts:

    def test_returns_account_list(self):
        mock_hits = [
            {"_id": "acc1", "_source": {"youtube_account_title": "Channel A"}},
        ]
        with mock.patch(
            "import_scripts.youtube.get_youtube_analytics.get_all_documents_paginated",
            return_value=mock_hits,
        ):
            result = yt_get.get_youtube_accounts()

        assert len(result) == 1
        assert result[0]["_source"]["youtube_account_title"] == "Channel A"

    def test_returns_empty_list_on_exception(self):
        with mock.patch(
            "import_scripts.youtube.get_youtube_analytics.get_all_documents_paginated",
            side_effect=Exception("ES down"),
        ):
            result = yt_get.get_youtube_accounts()

        assert result == []

    def test_returns_empty_list_when_no_accounts(self):
        with mock.patch(
            "import_scripts.youtube.get_youtube_analytics.get_all_documents_paginated",
            return_value=[],
        ):
            result = yt_get.get_youtube_accounts()

        assert result == []


# ---------------------------------------------------------------------------
# get_youtube_channels
# ---------------------------------------------------------------------------


class TestGetYoutubeChannels:

    def test_returns_channel_list(self):
        mock_hits = [
            {"_id": "ch1", "_source": {"youtube_channel_handle": "@alphachannel"}},
            {"_id": "ch2", "_source": {"youtube_channel_handle": "@betachannel"}},
        ]
        with mock.patch(
            "import_scripts.youtube.get_youtube_analytics.get_all_documents_paginated",
            return_value=mock_hits,
        ):
            result = yt_get.get_youtube_channels()

        assert len(result) == 2

    def test_returns_empty_list_on_exception(self):
        with mock.patch(
            "import_scripts.youtube.get_youtube_analytics.get_all_documents_paginated",
            side_effect=Exception("timeout"),
        ):
            result = yt_get.get_youtube_channels()

        assert result == []


# ---------------------------------------------------------------------------
# load_account_credentials
# ---------------------------------------------------------------------------


class TestLoadAccountCredentials:

    def test_raises_value_error_when_no_token(self):
        account_source = {"youtube_account_title": "Channel A"}  # no token field
        with pytest.raises(ValueError, match="no OAuth token"):
            yt_get.load_account_credentials(account_source)

    def test_raises_value_error_when_token_is_empty_string(self):
        account_source = {"youtube_account_title": "Channel A", "youtube_account_token": ""}
        with pytest.raises(ValueError, match="no OAuth token"):
            yt_get.load_account_credentials(account_source)

    def test_raises_when_client_creds_have_bad_format(self):
        """A malformed client file must still surface as an error.

         moved this validation down into ``load_google_oauth_client``,
        so the guarantee is asserted at the loader (where the file is now
        parsed) rather than at this call site. The caller's contract is
        unchanged: a bad file raises rather than yielding partial credentials.
        """
        from lib.google_oauth_client import GoogleOAuthClientError, load_google_oauth_client

        with mock.patch(
            "lib.encryption.decrypt_json_file",
            return_value={"bad_key": {}},  # neither 'web' nor 'installed'
        ):
            with pytest.raises(GoogleOAuthClientError, match="Invalid OAuth client credentials"):
                load_google_oauth_client(legacy_path="/nonexistent/for-test.enc")

    def test_propagates_loader_failure_to_caller(self):
        """A loader failure must not be swallowed into partial credentials."""
        account_source = {
            "youtube_account_title": "Channel A",
            "youtube_account_token": "encrypted-token",
        }
        with (
            mock.patch("import_scripts.youtube.get_youtube_analytics.decrypt", return_value="refresh-token"),
            mock.patch(
                "import_scripts.youtube.get_youtube_analytics.load_google_oauth_client",
                side_effect=ValueError("Invalid OAuth client credentials"),
            ),
        ):
            with pytest.raises(ValueError, match="Invalid OAuth client credentials"):
                yt_get.load_account_credentials(account_source)

    def test_returns_credentials_on_success(self):
        account_source = {
            "youtube_account_title": "Channel A",
            "youtube_account_token": "encrypted-token",
        }
        fake_client_creds = {"client_id": "client-id", "client_secret": "client-secret"}
        with (
            mock.patch("import_scripts.youtube.get_youtube_analytics.decrypt", return_value="refresh-token"),
            mock.patch(
                "import_scripts.youtube.get_youtube_analytics.load_google_oauth_client", return_value=fake_client_creds
            ),
        ):
            # The Credentials class is shimmed via sys.modules (google.oauth2.credentials)
            # so this call should not raise
            result = yt_get.load_account_credentials(account_source)

        assert result is not None


# ###########################################################################
# Additional coverage for get_youtube_analytics.py
# ###########################################################################

import json

_YT = "import_scripts.youtube.get_youtube_analytics"


# ---------------------------------------------------------------------------
# get_teams_by_youtube_channel_esid
# ---------------------------------------------------------------------------


class TestGetTeamsByYoutubeChannelEsid:

    def test_returns_teams_from_multi_select(self):
        team_doc = {
            "_id": "t1",
            "_source": {
                "team_title": "Alpha",
                "team_url": "alpha.com",
                "team_city": "NYC",
                "organization_title": "Org1",
            },
        }
        with (
            mock.patch(
                f"{_YT}.get_all_documents_paginated",
                side_effect=[
                    [team_doc],  # multi-select query
                    [],  # legacy query
                ],
            ),
            mock.patch(f"{_YT}._resolve_team_country", return_value="US"),
        ):
            result = yt_get.get_teams_by_youtube_channel_esid("ch-1")
        assert len(result) == 1
        assert result[0]["team_esid"] == "t1"

    def test_returns_empty_on_exception(self):
        with mock.patch(f"{_YT}.get_all_documents_paginated", side_effect=Exception("fail")):
            result = yt_get.get_teams_by_youtube_channel_esid("ch-1")
        assert result == []

    def test_deduplicates_across_queries(self):
        team_doc = {
            "_id": "t1",
            "_source": {
                "team_title": "Alpha",
                "team_url": "a.com",
                "team_city": "NYC",
                "organization_title": "Org",
            },
        }
        with (
            mock.patch(
                f"{_YT}.get_all_documents_paginated",
                side_effect=[
                    [team_doc],  # multi-select
                    [team_doc],  # legacy (same team)
                ],
            ),
            mock.patch(f"{_YT}._resolve_team_country", return_value="US"),
        ):
            result = yt_get.get_teams_by_youtube_channel_esid("ch-1")
        assert len(result) == 1  # no duplicates


# ---------------------------------------------------------------------------
# resolve_team_ids_to_esids
# ---------------------------------------------------------------------------


class TestResolveTeamIdsToEsids:

    def test_returns_none_when_team_ids_falsy(self):
        # No filter → caller must import all teams (cron behavior).
        assert yt_get.resolve_team_ids_to_esids(None) is None
        assert yt_get.resolve_team_ids_to_esids("") is None
        assert yt_get.resolve_team_ids_to_esids("  ,  ") is None

    def test_matches_by_esid_exact(self):
        hits = [
            {"_id": "esidA", "_source": {"team_title": "Alpha"}},
            {"_id": "esidB", "_source": {"team_title": "Beta"}},
        ]
        with mock.patch(f"{_YT}.get_all_documents_paginated", return_value=hits):
            result = yt_get.resolve_team_ids_to_esids("esidA")
        assert result == {"esidA"}

    def test_does_not_collide_on_same_title_across_workspaces(self):
        # Two workspaces, same team_title, different ESIDs. Selecting one ESID
        # must resolve to exactly that ESID — the whole point of.
        hits = [
            {"_id": "ws1_kavanah", "_source": {"team_title": "Kavanah Team"}},
            {"_id": "ws2_kavanah", "_source": {"team_title": "Kavanah Team"}},
        ]
        with mock.patch(f"{_YT}.get_all_documents_paginated", return_value=hits):
            result = yt_get.resolve_team_ids_to_esids("ws1_kavanah")
        assert result == {"ws1_kavanah"}
        assert "ws2_kavanah" not in result

    def test_title_fallback_is_exact_not_substring(self):
        # A title term must match the full title, not a substring — substring
        # matching is the FB collision bug we are NOT copying.
        hits = [
            {"_id": "e1", "_source": {"team_title": "Kavanah"}},
            {"_id": "e2", "_source": {"team_title": "Kavanah Team"}},
        ]
        with mock.patch(f"{_YT}.get_all_documents_paginated", return_value=hits):
            result = yt_get.resolve_team_ids_to_esids("Kavanah")
        assert result == {"e1"}  # exact "Kavanah" only, not "Kavanah Team"

    def test_returns_empty_set_when_nothing_matches(self):
        with mock.patch(f"{_YT}.get_all_documents_paginated", return_value=[]):
            result = yt_get.resolve_team_ids_to_esids("nope")
        assert result == set()

    def test_raises_on_lookup_error(self):
        """an ES failure propagates instead of becoming an empty set.

        Returning set() made an outage indistinguishable from "these teams don't
        exist", so the importer's strict-mode guard would blame the user's
        configuration for an infrastructure fault. See release-plan test
        for the no-widening guarantee this preserves.
        """
        with mock.patch(f"{_YT}.get_all_documents_paginated", side_effect=Exception("ES down")):
            with pytest.raises(Exception, match="ES down"):
                yt_get.resolve_team_ids_to_esids("esidA")


# ---------------------------------------------------------------------------
# get_youtube_service / get_youtube_data_service
# ---------------------------------------------------------------------------


class TestGetYoutubeService:

    def test_returns_service(self):
        with mock.patch(f"{_YT}.build", return_value=mock.MagicMock()) as mock_build:
            result = yt_get.get_youtube_service(mock.MagicMock())
        mock_build.assert_called_once()
        assert result is not None

    def test_raises_on_error(self):
        with mock.patch(f"{_YT}.build", side_effect=Exception("fail")):
            with pytest.raises(Exception):
                yt_get.get_youtube_service(mock.MagicMock())


class TestGetYoutubeDataService:

    def test_returns_service(self):
        with mock.patch(f"{_YT}.build", return_value=mock.MagicMock()):
            result = yt_get.get_youtube_data_service(mock.MagicMock())
        assert result is not None


# ---------------------------------------------------------------------------
# _parse_iso8601_duration
# ---------------------------------------------------------------------------


class TestParseIso8601Duration:

    def test_hours_minutes_seconds(self):
        assert yt_get._parse_iso8601_duration("PT1H2M30S") == 3750

    def test_minutes_only(self):
        assert yt_get._parse_iso8601_duration("PT5M") == 300

    def test_seconds_only(self):
        assert yt_get._parse_iso8601_duration("PT45S") == 45

    def test_unparseable(self):
        assert yt_get._parse_iso8601_duration("INVALID") == 0


# ---------------------------------------------------------------------------
# fetch_channel_analytics
# ---------------------------------------------------------------------------


class TestFetchChannelAnalytics:

    def test_returns_daily_data(self):
        yt_analytics = mock.MagicMock()
        response = {
            "columnHeaders": [
                {"name": "day"},
                {"name": "views"},
                {"name": "engagedViews"},
                {"name": "estimatedMinutesWatched"},
                {"name": "subscribersGained"},
                {"name": "subscribersLost"},
                {"name": "likes"},
                {"name": "comments"},
                {"name": "shares"},
                {"name": "videosAddedToPlaylists"},
                {"name": "videosRemovedFromPlaylists"},
                {"name": "averageViewDuration"},
            ],
            "rows": [["2024-01-15", 100, 80, 500, 10, 2, 30, 5, 3, 7, 1, 120.5]],
        }
        yt_analytics.reports().query().execute.return_value = response

        result = yt_get.fetch_channel_analytics(yt_analytics, "UC123", "2024-01-01", "2024-01-31")
        assert len(result) == 1
        assert result[0]["views"] == 100
        assert result[0]["average_view_duration"] == 120.5

    def test_returns_empty_on_general_exception(self):
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.side_effect = RuntimeError("fail")

        result = yt_get.fetch_channel_analytics(yt_analytics, "UC123", "2024-01-01", "2024-01-31")
        assert result == []

    def test_returns_empty_on_no_rows(self):
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.return_value = {"columnHeaders": [], "rows": []}

        result = yt_get.fetch_channel_analytics(yt_analytics, "UC123", "2024-01-01", "2024-01-31")
        assert result == []


# ---------------------------------------------------------------------------
# fetch_channel_analytics_by_country
# ---------------------------------------------------------------------------


class TestFetchChannelAnalyticsByCountry:

    def test_returns_country_data(self):
        # The "User Activity by Country" report is country-only — the API rejects a
        # "day" dimension — so the response has no "day" column and one row per country
        # aggregated over the window. Each row is stamped with end_date.
        yt_analytics = mock.MagicMock()
        response = {
            "columnHeaders": [
                {"name": "country"},
                {"name": "views"},
                {"name": "estimatedMinutesWatched"},
                {"name": "subscribersGained"},
                {"name": "subscribersLost"},
                {"name": "likes"},
                {"name": "comments"},
                {"name": "shares"},
            ],
            "rows": [["US", 50, 200, 5, 1, 10, 2, 1]],
        }
        yt_analytics.reports().query().execute.return_value = response

        result = yt_get.fetch_channel_analytics_by_country(yt_analytics, "UC123", "2024-01-01", "2024-01-31")
        assert len(result) == 1
        assert result[0]["country"] == "US"
        assert result[0]["record_type"] == "country_breakdown"
        # Window-aggregated rows are stamped with end_date so they fall inside the
        # kibana_date range the dashboard filters on.
        assert result[0]["date"] == "2024-01-31"

    def test_country_query_omits_day_dimension(self):
        # Regression for the "query is not supported" error: the country report must NOT
        # request the "day" dimension (only "country" is allowed). KAN — YouTube country
        # breakdown.
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.return_value = {"columnHeaders": [], "rows": []}
        yt_get.fetch_channel_analytics_by_country(yt_analytics, "UC123", "2024-01-01", "2024-01-31")

        # The second .query() call is the real one (the first is MagicMock attribute access).
        _, kwargs = yt_analytics.reports().query.call_args
        assert kwargs["dimensions"] == "country"
        assert "day" not in kwargs["dimensions"]

    def test_returns_empty_on_exception(self):
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.side_effect = RuntimeError("fail")
        result = yt_get.fetch_channel_analytics_by_country(yt_analytics, "UC123", "2024-01-01", "2024-01-31")
        assert result == []


# ---------------------------------------------------------------------------
# fetch_video_retention
# ---------------------------------------------------------------------------


class TestFetchVideoRetention:

    def test_returns_retention_data(self):
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.return_value = {
            "rows": [[0.0, 1.0], [0.25, 0.8], [0.50, 0.6], [0.75, 0.4], [0.95, 0.2], [1.0, 0.1]],
        }
        result = yt_get.fetch_video_retention(yt_analytics, "UC123", "vid1", "2024-01-01", "2024-01-31")
        assert result is not None
        assert result["retention_at_25_pct"] == 0.8
        assert result["retention_at_50_pct"] == 0.6

    def test_returns_none_on_no_rows(self):
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.return_value = {"rows": []}
        result = yt_get.fetch_video_retention(yt_analytics, "UC123", "vid1", "2024-01-01", "2024-01-31")
        assert result is None

    def test_returns_none_on_exception(self):
        yt_analytics = mock.MagicMock()
        yt_analytics.reports().query().execute.side_effect = RuntimeError("fail")
        result = yt_get.fetch_video_retention(yt_analytics, "UC123", "vid1", "2024-01-01", "2024-01-31")
        assert result is None


# ---------------------------------------------------------------------------
# save_analytics_data
# ---------------------------------------------------------------------------


class TestSaveAnalyticsData:

    def test_saves_json_file(self):
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_YT}.os.makedirs"),
            mock.patch(f"{_YT}.os.path.exists", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            yt_get.save_analytics_data({"date": "2024-01-15"}, "ch-1", "t-1", "2024-01-15")
        m_open.assert_called_once()

    def test_skips_already_imported(self):
        # archived AND indexed -> true skip (no write).
        with (
            mock.patch(f"{_YT}.os.makedirs"),
            mock.patch(f"{_YT}.os.path.exists", return_value=True),
            mock.patch(f"{_YT}.indexed_doc_present", return_value=True),
            mock.patch("builtins.open") as m_open,
        ):
            yt_get.save_analytics_data({"date": "2024-01-15"}, "ch-1", "t-1", "2024-01-15")
        m_open.assert_not_called()

    def test_rewrites_when_archived_but_not_indexed(self):
        # archived but ES doc MISSING -> re-write for re-send
        # (the old archive-alone skip caused silent permanent loss).
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_YT}.os.makedirs"),
            mock.patch(f"{_YT}.os.path.exists", return_value=True),
            mock.patch(f"{_YT}.indexed_doc_present", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            yt_get.save_analytics_data({"date": "2024-01-15"}, "ch-1", "t-1", "2024-01-15")
        m_open.assert_called_once()

    def test_includes_country_in_filename(self):
        m_open = mock.mock_open()
        with (
            mock.patch(f"{_YT}.os.makedirs"),
            mock.patch(f"{_YT}.os.path.exists", return_value=False),
            mock.patch("builtins.open", m_open),
        ):
            yt_get.save_analytics_data({"date": "2024-01-15", "country": "US"}, "ch-1", "t-1", "2024-01-15")
        filepath = m_open.call_args[0][0]
        assert "US" in filepath


# ---------------------------------------------------------------------------
# main (get_youtube_analytics)
# ---------------------------------------------------------------------------


class TestYoutubeAnalyticsMain:

    # main now requires --team_ids and resolves it to teams, then to
    # channel ESIDs, before loading channel docs. These tests predate that and
    # mock only get_youtube_channels, so they stub the two resolution steps to a
    # fixed in-scope team/channel. Each test's own subject (per-channel skips,
    # date defaults, channel_ids filtering) is downstream and unaffected.
    _SCOPE_TEAM = "t1"
    _SCOPE_CHANNEL = "ch-1"

    def _scoped(self):
        """Context managers that satisfy the scope guards."""
        return (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
        )

    def _params(self, **kw):
        base = {"start_date": "2024-01-01", "end_date": "2024-01-31", "team_ids": self._SCOPE_TEAM}
        base.update(kw)
        return base

    def test_raises_when_no_channels(self):
        """zero channel docs is a dangling link, not a silent no-op.

        Previously test_returns_early_when_no_channels, asserting the rc-0 return
        that let the Report Scheduler record a SUCCESS for a run importing nothing.
        """
        scope_teams, scope_channels = self._scoped()
        with scope_teams, scope_channels, mock.patch(f"{_YT}.get_youtube_channels", return_value=[]):
            with pytest.raises(ValueError, match="team_ids"):
                yt_get.main(self._params())

    def test_skips_account_not_in_es(self):
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[]),
        ):
            yt_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "team_ids": self._SCOPE_TEAM})

    def test_skips_channel_without_team(self):
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {
            "_id": "acc-1",
            "_source": {
                "youtube_account_title": "Account",
                "youtube_account_token": "enc",
            },
        }
        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[]),
            mock.patch(f"{_YT}.load_account_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_data_service", return_value=mock.MagicMock()),
        ):
            yt_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "team_ids": self._SCOPE_TEAM})

    def test_processes_channel_with_team(self):
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_title": "Test",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {
            "_id": "acc-1",
            "_source": {
                "youtube_account_title": "Account",
                "youtube_account_token": "enc",
            },
        }
        team = {
            "team_esid": "t1",
            "team_title": "Alpha",
            "team_url": "a.com",
            "team_country": "US",
            "team_city": "NYC",
            "organization_title": "Org",
        }
        analytics = [{"date": "2024-01-15", "views": 100}]

        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[team]),
            mock.patch(f"{_YT}.load_account_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_data_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.fetch_channel_analytics", return_value=analytics),
            mock.patch(f"{_YT}.fetch_channel_analytics_by_country", return_value=[]),
            mock.patch(f"{_YT}.fetch_channel_video_retention", return_value=[]),
            mock.patch(f"{_YT}.save_analytics_data") as mock_save,
            mock.patch(f"{_YT}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            yt_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "team_ids": self._SCOPE_TEAM})
        mock_save.assert_called()

    def test_defaults_dates(self):
        """Omitted dates default without error before the scope guards are reached.

        the empty-channel fixture now raises (dangling scope), so assert
        the channel fetch was reached, which proves the default start/end dates were
        computed without a date-validation error.
        """
        scope_teams, scope_channels = self._scoped()
        with (
            scope_teams,
            scope_channels,
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[]) as mock_fetch,
        ):
            with pytest.raises(ValueError, match="team_ids"):
                yt_get.main({"team_ids": self._SCOPE_TEAM})
        mock_fetch.assert_called_once()

    def test_skips_credential_error(self):
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {"_id": "acc-1", "_source": {"youtube_account_title": "Acc"}}
        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[]),
            mock.patch(f"{_YT}.load_account_credentials", side_effect=ValueError("no token")),
        ):
            yt_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "team_ids": self._SCOPE_TEAM})

    def test_filters_by_channel_ids(self):
        ch1 = {
            "_id": "ch-1",
            "_source": {"youtube_channel_id": "UC111", "youtube_channel_handle": "@a", "youtube_account_esid": "acc-1"},
        }
        ch2 = {
            "_id": "ch-2",
            "_source": {"youtube_channel_id": "UC222", "youtube_channel_handle": "@b", "youtube_account_esid": "acc-1"},
        }
        account = {"_id": "acc-1", "_source": {"youtube_account_title": "Acc", "youtube_account_token": "enc"}}
        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[ch1, ch2]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[]),
            mock.patch(f"{_YT}.load_account_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_data_service", return_value=mock.MagicMock()),
        ):
            yt_get.main(
                {"start_date": "2024-01-01", "end_date": "2024-01-31", "channel_ids": "UC111", "team_ids": self._SCOPE_TEAM}
            )

    def test_warns_unassigned_channel_with_team(self):
        # Channel with no account_esid but a team references it
        ch = {"_id": "ch-1", "_source": {"youtube_channel_id": "UC123", "youtube_channel_handle": "@test"}}
        team = {"team_esid": "t1", "team_title": "Alpha"}
        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[ch]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[team]),
        ):
            yt_get.main({"start_date": "2024-01-01", "end_date": "2024-01-31", "team_ids": self._SCOPE_TEAM})

    def test_processes_country_and_retention_data(self):
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_title": "Test",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {
            "_id": "acc-1",
            "_source": {
                "youtube_account_title": "Account",
                "youtube_account_token": "enc",
            },
        }
        team = {
            "team_esid": "t1",
            "team_title": "Alpha",
            "team_url": "a.com",
            "team_country": "US",
            "team_city": "NYC",
            "organization_title": "Org",
        }
        analytics = [{"date": "2024-01-15", "views": 100}]
        country_data = [{"date": "2024-01-15", "country": "US", "views": 50, "record_type": "country_breakdown"}]
        retention = [
            {
                "video_id": "v1",
                "video_title": "Vid",
                "video_duration_seconds": 300,
                "record_type": "video_retention",
                "retention_at_25_pct": 0.8,
            }
        ]

        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[team]),
            mock.patch(f"{_YT}.load_account_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_data_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.fetch_channel_analytics", return_value=analytics),
            mock.patch(f"{_YT}.fetch_channel_analytics_by_country", return_value=country_data),
            mock.patch(f"{_YT}.fetch_channel_video_retention", return_value=retention),
            mock.patch(f"{_YT}.save_analytics_data") as mock_save,
            mock.patch(f"{_YT}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            yt_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "team_ids": self._SCOPE_TEAM})
        # analytics (1) + country (1) + retention (1) = 3 saves
        assert mock_save.call_count == 3

    def test_team_filter_scopes_to_selected_team(self):
        # a channel back-links to two teams; passing team_ids for only
        # one ESID must write data for that team only.
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_title": "Test",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {
            "_id": "acc-1",
            "_source": {
                "youtube_account_title": "Account",
                "youtube_account_token": "enc",
            },
        }
        team_keep = {
            "team_esid": "t1",
            "team_title": "Alpha",
            "team_url": "a.com",
            "team_country": "US",
            "team_city": "NYC",
            "organization_title": "Org",
        }
        team_drop = {
            "team_esid": "t2",
            "team_title": "Beta",
            "team_url": "b.com",
            "team_country": "US",
            "team_city": "LA",
            "organization_title": "Org2",
        }
        analytics = [{"date": "2024-01-15", "views": 100}]

        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={"t1"}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={"ch-1"}),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[team_keep, team_drop]),
            mock.patch(f"{_YT}.load_account_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_data_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.fetch_channel_analytics", return_value=analytics),
            mock.patch(f"{_YT}.fetch_channel_analytics_by_country", return_value=[]),
            mock.patch(f"{_YT}.fetch_channel_video_retention", return_value=[]),
            mock.patch(f"{_YT}.save_analytics_data") as mock_save,
            mock.patch(f"{_YT}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            yt_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "team_ids": "t1"})

        # Only the kept team's esid should appear as a save target.
        saved_team_esids = {call.args[2] for call in mock_save.call_args_list}
        assert saved_team_esids == {"t1"}
        assert "t2" not in saved_team_esids

    def test_channel_shared_by_two_in_scope_teams_writes_both(self):
        """A channel linked to two IN-SCOPE teams is written for both.

        this was test_no_team_filter_imports_all_teams, which drove the
        unscoped path (no --team_ids → resolver returns None → every team written).
        That path no longer exists: main() now requires an explicit scope, so the
        fleet-wide fallback it exercised is unreachable by design.

        The surviving, still-valuable property is the multi-team fan-out: one
        channel shared by several teams in scope must produce a save per team, not
        just the first. Both teams are put in scope here to test that directly.
        """
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_title": "Test",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {
            "_id": "acc-1",
            "_source": {
                "youtube_account_title": "Account",
                "youtube_account_token": "enc",
            },
        }
        team_a = {
            "team_esid": "t1",
            "team_title": "Alpha",
            "team_url": "a.com",
            "team_country": "US",
            "team_city": "NYC",
            "organization_title": "Org",
        }
        team_b = {
            "team_esid": "t2",
            "team_title": "Beta",
            "team_url": "b.com",
            "team_country": "US",
            "team_city": "LA",
            "organization_title": "Org2",
        }
        analytics = [{"date": "2024-01-15", "views": 100}]

        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={"t1", "t2"}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(f"{_YT}.get_teams_by_youtube_channel_esid", return_value=[team_a, team_b]),
            mock.patch(f"{_YT}.load_account_credentials", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.get_youtube_data_service", return_value=mock.MagicMock()),
            mock.patch(f"{_YT}.fetch_channel_analytics", return_value=analytics),
            mock.patch(f"{_YT}.fetch_channel_analytics_by_country", return_value=[]),
            mock.patch(f"{_YT}.fetch_channel_video_retention", return_value=[]),
            mock.patch(f"{_YT}.save_analytics_data") as mock_save,
            mock.patch(f"{_YT}.write_no_data_markers"),
            mock.patch("import_scripts.facebook.run_daily_imports.generate_date_range", return_value=["2024-01-15"]),
        ):
            yt_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "team_ids": "t1,t2"})

        saved_team_esids = {call.args[2] for call in mock_save.call_args_list}
        assert saved_team_esids == {"t1", "t2"}

    def test_skips_revoked_connection_account(self):
        # an account whose de_connection is revoked must be skipped
        # entirely — no credentials loaded, no data saved.
        channel = {
            "_id": "ch-1",
            "_source": {
                "youtube_channel_id": "UC123",
                "youtube_channel_handle": "@test",
                "youtube_account_esid": "acc-1",
            },
        }
        account = {
            "_id": "acc-1",
            "_source": {
                "youtube_account_title": "Revoked Account",
                "youtube_account_token": "enc",
            },
        }
        with (
            mock.patch(f"{_YT}.resolve_team_ids_to_esids", return_value={self._SCOPE_TEAM}),
            mock.patch(f"{_YT}._channel_esids_for_teams", return_value={self._SCOPE_CHANNEL}),
            mock.patch(f"{_YT}.get_youtube_channels", return_value=[channel]),
            mock.patch(f"{_YT}.get_youtube_accounts", return_value=[account]),
            mock.patch(
                "asset_management.web.services.connection_service.ConnectionService."
                "revoked_connection_exists_for_legacy_asset",
                return_value=True,
            ),
            mock.patch(f"{_YT}.load_account_credentials") as mock_creds,
            mock.patch(f"{_YT}.save_analytics_data") as mock_save,
        ):
            yt_get.main({"start_date": "2024-01-15", "end_date": "2024-01-15", "team_ids": self._SCOPE_TEAM})

        mock_creds.assert_not_called()
        mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# get_channel_videos
# ---------------------------------------------------------------------------


class TestGetChannelVideos:

    def test_returns_video_list(self):
        yt_data = mock.MagicMock()
        # Channel response
        yt_data.channels().list().execute.return_value = {
            "items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}],
        }
        # Playlist items response
        yt_data.playlistItems().list().execute.return_value = {
            "items": [{"contentDetails": {"videoId": "v1"}}],
        }
        # Video details response
        yt_data.videos().list().execute.return_value = {
            "items": [
                {
                    "id": "v1",
                    "snippet": {"title": "My Video"},
                    "contentDetails": {"duration": "PT5M30S"},
                }
            ],
        }

        result = yt_get.get_channel_videos(yt_data, "UC123")
        assert len(result) == 1
        assert result[0]["video_id"] == "v1"
        assert result[0]["video_duration_seconds"] == 330

    def test_returns_empty_when_no_channel(self):
        yt_data = mock.MagicMock()
        yt_data.channels().list().execute.return_value = {"items": []}
        result = yt_get.get_channel_videos(yt_data, "UC123")
        assert result == []

    def test_returns_empty_on_exception(self):
        yt_data = mock.MagicMock()
        yt_data.channels().list().execute.side_effect = RuntimeError("fail")
        result = yt_get.get_channel_videos(yt_data, "UC123")
        assert result == []


# ---------------------------------------------------------------------------
# fetch_channel_video_retention
# ---------------------------------------------------------------------------


class TestFetchChannelVideoRetention:

    def test_returns_retention_records(self):
        yt_analytics = mock.MagicMock()
        yt_data = mock.MagicMock()
        videos = [{"video_id": "v1", "video_title": "Vid", "video_duration_seconds": 300}]
        retention = {
            "retention_at_25_pct": 0.8,
            "retention_at_50_pct": 0.6,
            "retention_at_75_pct": 0.4,
            "retention_at_95_pct": 0.2,
            "retention_at_100_pct": 0.1,
        }

        with (
            mock.patch(f"{_YT}.get_channel_videos", return_value=videos),
            mock.patch(f"{_YT}.fetch_video_retention", return_value=retention),
        ):
            result = yt_get.fetch_channel_video_retention(yt_analytics, yt_data, "UC123", "2024-01-01", "2024-01-31")
        assert len(result) == 1
        assert result[0]["record_type"] == "video_retention"

    def test_returns_empty_when_no_videos(self):
        with mock.patch(f"{_YT}.get_channel_videos", return_value=[]):
            result = yt_get.fetch_channel_video_retention(
                mock.MagicMock(), mock.MagicMock(), "UC123", "2024-01-01", "2024-01-31"
            )
        assert result == []

    def test_skips_videos_with_no_retention(self):
        videos = [{"video_id": "v1", "video_title": "Vid", "video_duration_seconds": 300}]
        with (
            mock.patch(f"{_YT}.get_channel_videos", return_value=videos),
            mock.patch(f"{_YT}.fetch_video_retention", return_value=None),
        ):
            result = yt_get.fetch_channel_video_retention(
                mock.MagicMock(), mock.MagicMock(), "UC123", "2024-01-01", "2024-01-31"
            )
        assert result == []

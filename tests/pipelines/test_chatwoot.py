"""
Tests for the Chatwoot pipeline scripts.

Covers import_scripts.chatwoot.get_account_reports:
  date_to_unix          — pure timestamp conversion
  fetch_metric          — HTTP request to Chatwoot API (mocked)
  get_chatwoot_accounts — ES lookup (mocked)
  get_teams_for_account — ES lookup (mocked), in attribution.py
  save_report           — file I/O (mocked)
"""

import json
import pytest
from datetime import datetime
from unittest import mock

import import_scripts.chatwoot.attribution as attribution
from import_scripts.chatwoot.api import ChatwootApiError
import import_scripts.chatwoot.get_account_reports as chatwoot

_CLOUD = "https://app.chatwoot.com"


# ---------------------------------------------------------------------------
# date_to_unix
# ---------------------------------------------------------------------------


class TestDateToUnix:

    def test_start_of_day(self):
        ts = chatwoot.date_to_unix("2024-03-15")
        expected = int(datetime(2024, 3, 15).timestamp())
        assert ts == expected

    def test_end_of_day(self):
        ts = chatwoot.date_to_unix("2024-03-15", end_of_day=True)
        expected = int(datetime(2024, 3, 15, 23, 59, 59).timestamp())
        assert ts == expected

    def test_start_lt_end_same_day(self):
        ts_start = chatwoot.date_to_unix("2024-06-01")
        ts_end = chatwoot.date_to_unix("2024-06-01", end_of_day=True)
        assert ts_start < ts_end


# ---------------------------------------------------------------------------
# fetch_metric
# ---------------------------------------------------------------------------


class TestFetchMetric:

    def _mock_response(self, data, status=200):
        mock_resp = mock.MagicMock()
        mock_resp.status_code = status
        mock_resp.json.return_value = data
        mock_resp.raise_for_status = mock.MagicMock()
        return mock_resp

    def test_sums_count_metric(self):
        api_data = [{"value": 10}, {"value": 20}, {"value": 30}]
        with mock.patch(
            "import_scripts.chatwoot.get_account_reports.requests.get", return_value=self._mock_response(api_data)
        ):
            result = chatwoot.fetch_metric("123", "token", "conversations_count", 0, 86400, base_url=_CLOUD)
        assert result == 60

    def test_averages_avg_metric(self):
        api_data = [{"value": 100}, {"value": 200}]
        with mock.patch(
            "import_scripts.chatwoot.get_account_reports.requests.get", return_value=self._mock_response(api_data)
        ):
            result = chatwoot.fetch_metric("123", "token", "avg_first_response_time", 0, 86400, base_url=_CLOUD)
        assert result == 150.0

    def test_returns_zero_for_empty_data(self):
        with mock.patch("import_scripts.chatwoot.get_account_reports.requests.get", return_value=self._mock_response([])):
            result = chatwoot.fetch_metric("123", "token", "conversations_count", 0, 86400, base_url=_CLOUD)
        assert result == 0

    # a metric that cannot be read raises instead of returning None,
    # which used to be saved as a null metric on a "successful" run.
    def test_raises_when_response_not_list(self):
        with mock.patch(
            "import_scripts.chatwoot.get_account_reports.requests.get", return_value=self._mock_response({"error": "bad"})
        ):
            with pytest.raises(ChatwootApiError):
                chatwoot.fetch_metric("123", "token", "conversations_count", 0, 86400, base_url=_CLOUD)

    def test_raises_on_http_error(self):
        import requests as req_lib

        mock_resp = mock.MagicMock()
        mock_resp.status_code = 404
        mock_resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("404")
        with mock.patch("import_scripts.chatwoot.get_account_reports.requests.get", return_value=mock_resp):
            with pytest.raises(ChatwootApiError):
                chatwoot.fetch_metric("123", "token", "conversations_count", 0, 86400, base_url=_CLOUD)

    def test_raises_after_retrying_a_timeout(self):
        import requests as req_lib

        with (
            mock.patch("import_scripts.chatwoot.api.requests.get", side_effect=req_lib.Timeout("slow")),
            mock.patch("import_scripts.chatwoot.api.time.sleep"),
        ):
            with pytest.raises(ChatwootApiError):
                chatwoot.fetch_metric("123", "token", "conversations_count", 0, 86400, base_url=_CLOUD)

    def test_skips_points_with_none_value(self):
        api_data = [{"value": 10}, {"value": None}, {"value": 20}]
        with mock.patch(
            "import_scripts.chatwoot.get_account_reports.requests.get", return_value=self._mock_response(api_data)
        ):
            result = chatwoot.fetch_metric("123", "token", "conversations_count", 0, 86400, base_url=_CLOUD)
        # Only non-None values are summed: 10 + 20 = 30
        assert result == 30


# ---------------------------------------------------------------------------
# get_chatwoot_accounts
# ---------------------------------------------------------------------------


class TestGetChatwootAccounts:

    def test_returns_account_list(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "acc1", "_source": {"chatwoot_account_title": "Acme"}},
                ]
            }
        }
        result = chatwoot.get_chatwoot_accounts(mock_es)
        assert len(result) == 1
        assert result[0]["_source"]["chatwoot_account_title"] == "Acme"

    def test_returns_empty_list_on_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("connection error")
        result = chatwoot.get_chatwoot_accounts(mock_es)
        assert result == []

    def test_queries_correct_index(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        chatwoot.get_chatwoot_accounts(mock_es)
        call_kwargs = mock_es.search.call_args
        assert "de_asset_chatwoot_accounts" in str(call_kwargs)


# ---------------------------------------------------------------------------
# get_teams_for_account
# ---------------------------------------------------------------------------


class TestGetTeamsForAccount:

    def test_returns_every_linked_team_sorted_by_esid(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [{"_id": "team-b", "_source": {"team_title": "B"}}, {"_id": "team-a", "_source": {"team_title": "A"}}]
            }
        }
        teams = attribution.get_teams_for_account(mock_es, "acc-esid-1")
        assert teams == [("team-a", {"team_title": "A"}), ("team-b", {"team_title": "B"})]

    def test_returns_empty_list_when_no_team(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        assert attribution.get_teams_for_account(mock_es, "acc-esid-1") == []

    def test_raises_on_es_error_so_an_outage_is_not_read_as_unlinked(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("ES error")
        with pytest.raises(Exception, match="ES error"):
            attribution.get_teams_for_account(mock_es, "acc-esid-1")


# ---------------------------------------------------------------------------
# save_report
# ---------------------------------------------------------------------------


class TestSaveReport:

    def test_saves_json_file_to_correct_path(self):
        report = {"conversations_count": 42, "date": "2024-01-15"}
        m_open = mock.mock_open()

        with mock.patch("import_scripts.chatwoot.get_account_reports.os.makedirs"), mock.patch("builtins.open", m_open):
            chatwoot.save_report(report, "acc-esid-1", "2024-01-15")

        # Verify something was written
        m_open.return_value.__enter__().write.assert_called()

    def test_creates_directory_for_account(self):
        report = {"conversations_count": 5}
        with (
            mock.patch("import_scripts.chatwoot.get_account_reports.os.makedirs") as mock_makedirs,
            mock.patch("builtins.open", mock.mock_open()),
        ):
            chatwoot.save_report(report, "acc-esid-99", "2024-01-15")

        mock_makedirs.assert_called_once()
        # Directory path should include the account ESID
        call_args = mock_makedirs.call_args[0][0]
        assert "acc-esid-99" in call_args


# ---------------------------------------------------------------------------
# main — orchestration
# ---------------------------------------------------------------------------


class TestMain:

    def _mock_account(self, has_id=True, has_token=True):
        source = {
            "chatwoot_account_title": "TestAccount",
            "chatwoot_account_id": "123" if has_id else None,
            "chatwoot_api_token": "enc-token" if has_token else None,
        }
        return {"_id": "acc-esid-1", "_source": source}

    def test_main_raises_when_es_unavailable(self):
        # was test_main_returns_early_when_es_unavailable, asserting the
        # rc-0 return. An ES outage is a hard failure: returning rc 0 made the
        # Report Scheduler record a SUCCESS for a run that imported nothing.
        with mock.patch("import_scripts.chatwoot.get_account_reports.get_elasticsearch_client", return_value=None):
            with pytest.raises(RuntimeError, match="Elasticsearch"):
                chatwoot.main({})

    def test_main_returns_early_when_no_accounts(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch("import_scripts.chatwoot.get_account_reports.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.chatwoot.get_account_reports.get_chatwoot_accounts", return_value=[]),
        ):
            chatwoot.main({})

    def test_main_skips_account_with_missing_id(self):
        mock_es = mock.MagicMock()
        account = self._mock_account(has_id=False)
        with (
            mock.patch("import_scripts.chatwoot.get_account_reports.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.chatwoot.get_account_reports.get_chatwoot_accounts", return_value=[account]),
            mock.patch("import_scripts.chatwoot.get_account_reports.decrypt") as mock_decrypt,
        ):
            chatwoot.main({})
        # decrypt should not be called — account was skipped
        mock_decrypt.assert_not_called()

    def test_main_processes_valid_account(self):
        mock_es = mock.MagicMock()
        account = self._mock_account()
        metric_values = {m: 0 for m in chatwoot.METRICS}

        with (
            mock.patch("import_scripts.chatwoot.get_account_reports.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.chatwoot.get_account_reports.get_chatwoot_accounts", return_value=[account]),
            mock.patch("import_scripts.chatwoot.get_account_reports.decrypt", return_value="plaintext-token"),
            mock.patch(
                "import_scripts.chatwoot.get_account_reports.get_teams_for_account",
                return_value=[("team-esid-1", {"team_title": "TestTeam", "organization_title": "TestOrg"})],
            ),
            mock.patch("import_scripts.chatwoot.get_account_reports.fetch_metric", return_value=42),
            # these made live HTTP calls before and a failure read as None.
            mock.patch(
                "import_scripts.chatwoot.get_account_reports.fetch_inboxes",
                return_value={"facebook_page_id": None, "instagram_id": None, "inbox_names": [], "inboxes": []},
            ),
            mock.patch("import_scripts.chatwoot.get_account_reports.fetch_contact_checkbox_keys", return_value=[]),
            mock.patch(
                "import_scripts.chatwoot.get_account_reports.fetch_contact_activity",
                return_value={"contacts_started": 0, "flags": {}},
            ),
            mock.patch("import_scripts.chatwoot.get_account_reports.delete_stale_markers_for_date"),
            mock.patch("import_scripts.chatwoot.get_account_reports.write_no_data_markers"),
            mock.patch("import_scripts.chatwoot.get_account_reports.save_report") as mock_save,
            mock.patch("import_scripts.chatwoot.get_account_reports.time.sleep"),
        ):
            chatwoot.main({"date": "2024-01-15"})

        mock_save.assert_called_once()
        saved_report = mock_save.call_args[0][0]
        assert saved_report["team_title"] == "TestTeam"
        assert saved_report["date"] == "2024-01-15"

    def test_main_uses_yesterday_when_no_date_param(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch("import_scripts.chatwoot.get_account_reports.get_elasticsearch_client", return_value=mock_es),
            mock.patch("import_scripts.chatwoot.get_account_reports.get_chatwoot_accounts", return_value=[]),
        ):
            # Should not raise — date defaults to yesterday
            chatwoot.main({})


# ###########################################################################
# get_conversations.py
# ###########################################################################

import import_scripts.chatwoot.get_conversations as conv

_CONV = "import_scripts.chatwoot.get_conversations"


# ---------------------------------------------------------------------------
# get_chatwoot_accounts (get_conversations version)
# ---------------------------------------------------------------------------


class TestConvGetChatwootAccounts:

    def test_returns_hits(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": [{"_id": "a1", "_source": {"chatwoot_account_title": "Acme"}}]}}
        result = conv.get_chatwoot_accounts(mock_es)
        assert len(result) == 1
        assert result[0]["_id"] == "a1"

    def test_returns_empty_on_no_hits(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        result = conv.get_chatwoot_accounts(mock_es)
        assert result == []

    def test_returns_empty_on_missing_hits_key(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {}
        result = conv.get_chatwoot_accounts(mock_es)
        assert result == []


# ---------------------------------------------------------------------------
# fetch_conversations_page
# ---------------------------------------------------------------------------


class TestFetchConversationsPage:

    def _mock_response(self, body, status=200):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_returns_payload_and_meta(self):
        body = {
            "data": {
                "payload": [{"id": 1}, {"id": 2}],
                "meta": {"all_count": 50},
            }
        }
        with mock.patch("import_scripts.chatwoot.api.requests.get", return_value=self._mock_response(body)):
            payload, meta = conv.fetch_conversations_page("123", "tok", "open", 1, base_url=_CLOUD)
        assert len(payload) == 2
        assert meta["all_count"] == 50

    # an unreadable page raises; an empty page used to end the
    # pagination early and look like "no more conversations".
    def test_raises_on_http_error(self):
        import requests as req_lib

        resp = mock.MagicMock()
        resp.status_code = 404
        resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("404")
        with mock.patch("import_scripts.chatwoot.api.requests.get", return_value=resp):
            with pytest.raises(ChatwootApiError):
                conv.fetch_conversations_page("123", "tok", "open", 1, base_url=_CLOUD)

    def test_raises_after_retrying_a_connection_error(self):
        import requests as req_lib

        with (
            mock.patch("import_scripts.chatwoot.api.requests.get", side_effect=req_lib.ConnectionError("down")),
            mock.patch("import_scripts.chatwoot.api.time.sleep"),
        ):
            with pytest.raises(ChatwootApiError):
                conv.fetch_conversations_page("123", "tok", "open", 1, base_url=_CLOUD)

    def test_returns_empty_payload_when_data_missing(self):
        body = {"other_key": "value"}
        with mock.patch("import_scripts.chatwoot.api.requests.get", return_value=self._mock_response(body)):
            payload, meta = conv.fetch_conversations_page("123", "tok", "open", 1, base_url=_CLOUD)
        assert payload == []
        assert meta == {}


# ---------------------------------------------------------------------------
# flatten_conversation
# ---------------------------------------------------------------------------


class TestFlattenConversation:

    def _sample_conv(self, **overrides):
        base = {
            "id": 42,
            "uuid": "abc-123",
            "status": "open",
            "inbox_id": 5,
            "created_at": 1700000000,
            "updated_at": 1700001000,
            "last_activity_at": 1700001000,
            "first_reply_created_at": 1700000500,
            "waiting_since": None,
            "unread_count": 2,
            "priority": None,
            "sla_policy_id": None,
            "muted": False,
            "can_reply": True,
            "labels": ["vip"],
            "assignee_type": "user",
            "meta": {
                "sender": {
                    "id": 10,
                    "name": "Jane",
                    "email": "jane@test.com",
                    "phone_number": "+1234",
                    "additional_attributes": {
                        "social_instagram_user_name": "jane_ig",
                        "social_instagram_follower_count": 500,
                        "social_instagram_is_verified_user": False,
                    },
                },
                "assignee": {"id": 7, "name": "Agent Bob", "email": "bob@test.com"},
            },
            "custom_attributes": {"dt_conversation_url": "https://dt.example.com/42"},
            "additional_attributes": {"type": "instagram_direct_message"},
        }
        base.update(overrides)
        return base

    def test_maps_core_fields(self):
        doc = conv.flatten_conversation(self._sample_conv(), "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert doc["conversation_id"] == 42
        assert doc["conversation_uuid"] == "abc-123"
        assert doc["status"] == "open"
        assert doc["chatwoot_account_title"] == "Acme"
        assert doc["team_title"] == "TeamA"
        assert doc["team_esid"] == "t-1"
        assert doc["organization_title"] == "OrgX"

    def test_maps_sender_fields(self):
        doc = conv.flatten_conversation(self._sample_conv(), "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        # the seeker's name / email / phone / IG handle are never stored.
        for dropped in ("sender_name", "sender_email", "sender_phone", "sender_instagram_username", "dt_conversation_url"):
            assert dropped not in doc, dropped

    def test_maps_assignee_fields(self):
        doc = conv.flatten_conversation(self._sample_conv(), "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert doc["assignee_id"] == 7
        assert doc["assignee_name"] == "Agent Bob"

    def test_computes_kibana_date(self):
        doc = conv.flatten_conversation(self._sample_conv(), "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert doc["kibana_date"] == "2023-11-14"

    def test_kibana_date_none_when_no_activity(self):
        doc = conv.flatten_conversation(
            self._sample_conv(last_activity_at=None), "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX"
        )
        assert doc["kibana_date"] is None

    def test_handles_missing_meta(self):
        raw = self._sample_conv(meta={})
        doc = conv.flatten_conversation(raw, "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert "sender_name" not in doc
        assert doc["assignee_id"] is None

    def test_handles_null_custom_attributes(self):
        raw = self._sample_conv(custom_attributes=None)
        doc = conv.flatten_conversation(raw, "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert "dt_conversation_url" not in doc  # never stored

    def test_handles_null_additional_attributes(self):
        raw = self._sample_conv(additional_attributes=None)
        doc = conv.flatten_conversation(raw, "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert doc["channel_type"] is None

    def test_team_esid_defaults_to_unknown(self):
        doc = conv.flatten_conversation(self._sample_conv(), "Acme", "123", "acc-1", "TeamA", None, "OrgX")
        assert doc["team_esid"] == "unknown"

    def test_empty_labels_default(self):
        raw = self._sample_conv(labels=None)
        doc = conv.flatten_conversation(raw, "Acme", "123", "acc-1", "TeamA", "t-1", "OrgX")
        assert doc["labels"] == []


# ---------------------------------------------------------------------------
# save_conversation
# ---------------------------------------------------------------------------


class TestSaveConversation:

    def test_creates_directory_and_writes_json(self):
        doc = {"conversation_id": 99, "status": "open"}
        m_open = mock.mock_open()
        with mock.patch(f"{_CONV}.os.makedirs") as mock_mkdirs, mock.patch("builtins.open", m_open):
            conv.save_conversation(doc, "acc-esid-1")

        mock_mkdirs.assert_called_once()
        assert "acc-esid-1" in mock_mkdirs.call_args[0][0]
        m_open.assert_called_once()
        assert "99.json" in m_open.call_args[0][0]

    def test_writes_valid_json(self):
        doc = {"conversation_id": 7, "status": "pending"}
        m_open = mock.mock_open()
        with mock.patch(f"{_CONV}.os.makedirs"), mock.patch("builtins.open", m_open):
            conv.save_conversation(doc, "acc-esid-2")
        handle = m_open()
        written = "".join(c.args[0] for c in handle.write.call_args_list)
        assert json.loads(written)["conversation_id"] == 7


# ---------------------------------------------------------------------------
# main (get_conversations)
# ---------------------------------------------------------------------------


class TestConvMain:

    def _mock_account(self, has_id=True, has_token=True):
        return {
            "_id": "acc-esid-1",
            "_source": {
                "chatwoot_account_title": "TestAccount",
                "chatwoot_account_id": "123" if has_id else None,
                "chatwoot_api_token": "enc-token" if has_token else None,
            },
        }

    def test_raises_when_es_unavailable(self):
        # see the get_account_reports sibling: an ES outage must not
        # exit rc 0 as a successful no-op.
        with mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=None):
            with pytest.raises(RuntimeError, match="Elasticsearch"):
                conv.main({})

    def test_returns_early_when_no_accounts(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[]),
        ):
            conv.main({})

    def test_skips_account_missing_id(self):
        mock_es = mock.MagicMock()
        account = self._mock_account(has_id=False)
        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt") as mock_decrypt,
        ):
            conv.main({})
        mock_decrypt.assert_not_called()

    def test_skips_account_missing_token(self):
        mock_es = mock.MagicMock()
        account = self._mock_account(has_token=False)
        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt") as mock_decrypt,
        ):
            conv.main({})
        mock_decrypt.assert_not_called()

    def test_processes_account_and_saves_conversations(self):
        mock_es = mock.MagicMock()
        account = self._mock_account()
        sample_conv = {
            "id": 1,
            "uuid": "u1",
            "status": "open",
            "meta": {},
            "custom_attributes": {},
            "additional_attributes": {},
            "last_activity_at": 1700000000,
            "labels": [],
        }

        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt", return_value="plain-token"),
            mock.patch(
                f"{_CONV}.get_teams_for_account", return_value=[("t-esid", {"team_title": "T1", "organization_title": "O1"})]
            ),
            mock.patch(f"{_CONV}.fetch_conversations_page", return_value=([sample_conv], {"all_count": 1})),
            mock.patch(f"{_CONV}.save_conversation") as mock_save,
            mock.patch(f"{_CONV}.time.sleep"),
        ):
            conv.main({})

        assert mock_save.call_count >= 1
        saved_doc = mock_save.call_args[0][0]
        assert saved_doc["conversation_id"] == 1

    def test_pagination_stops_when_fewer_than_page_size(self):
        mock_es = mock.MagicMock()
        account = self._mock_account()
        small_page = [
            {
                "id": i,
                "uuid": f"u{i}",
                "status": "open",
                "meta": {},
                "custom_attributes": {},
                "additional_attributes": {},
                "last_activity_at": 1700000000,
                "labels": [],
            }
            for i in range(5)
        ]

        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt", return_value="tok"),
            mock.patch(
                f"{_CONV}.get_teams_for_account", return_value=[("t1", {"team_title": "T", "organization_title": "O"})]
            ),
            mock.patch(f"{_CONV}.fetch_conversations_page", return_value=(small_page, {})) as mock_fetch,
            mock.patch(f"{_CONV}.save_conversation"),
            mock.patch(f"{_CONV}.time.sleep"),
        ):
            conv.main({})

        # 5 < PAGE_SIZE(25), so each status fetches only 1 page = 2 calls total
        assert mock_fetch.call_count == 2

    def test_pagination_stops_on_empty_payload(self):
        mock_es = mock.MagicMock()
        account = self._mock_account()

        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt", return_value="tok"),
            mock.patch(
                f"{_CONV}.get_teams_for_account", return_value=[("t1", {"team_title": "T", "organization_title": "O"})]
            ),
            mock.patch(f"{_CONV}.fetch_conversations_page", return_value=([], {})) as mock_fetch,
            mock.patch(f"{_CONV}.time.sleep"),
        ):
            conv.main({})

        # Empty immediately for both statuses
        assert mock_fetch.call_count == 2

    def test_iterates_both_statuses(self):
        mock_es = mock.MagicMock()
        account = self._mock_account()
        statuses_seen = []

        def capture_status(account_id, token, status, page, *, base_url, inbox_id=None):
            statuses_seen.append(status)
            return [], {}

        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt", return_value="tok"),
            mock.patch(
                f"{_CONV}.get_teams_for_account", return_value=[("t1", {"team_title": "T", "organization_title": "O"})]
            ),
            mock.patch(f"{_CONV}.fetch_conversations_page", side_effect=capture_status),
            mock.patch(f"{_CONV}.time.sleep"),
        ):
            conv.main({})

        assert "open" in statuses_seen
        assert "pending" in statuses_seen

    def test_team_defaults_to_unknown_when_none(self):
        mock_es = mock.MagicMock()
        account = self._mock_account()
        sample_conv = {
            "id": 1,
            "uuid": "u1",
            "status": "open",
            "meta": {},
            "custom_attributes": {},
            "additional_attributes": {},
            "last_activity_at": 1700000000,
            "labels": [],
        }

        with (
            mock.patch(f"{_CONV}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_CONV}.get_chatwoot_accounts", return_value=[account]),
            mock.patch(f"{_CONV}.decrypt", return_value="tok"),
            mock.patch(f"{_CONV}.get_teams_for_account", return_value=[]),
            mock.patch(f"{_CONV}.fetch_conversations_page", return_value=([sample_conv], {})),
            mock.patch(f"{_CONV}.save_conversation") as mock_save,
            mock.patch(f"{_CONV}.time.sleep"),
        ):
            conv.main({})

        saved_doc = mock_save.call_args[0][0]
        assert saved_doc["team_title"] == "unknown"
        assert saved_doc["organization_title"] == "unknown"


# ###########################################################################
# explore_fields.py
# ###########################################################################

import import_scripts.chatwoot.explore_fields as explore

_EXP = "import_scripts.chatwoot.explore_fields"


# ---------------------------------------------------------------------------
# _get
# ---------------------------------------------------------------------------


class TestExploreGet:

    def _mock_response(self, body, status=200):
        resp = mock.MagicMock()
        resp.status_code = status
        resp.json.return_value = body
        resp.raise_for_status = mock.MagicMock()
        return resp

    def test_returns_parsed_json(self):
        with mock.patch(f"{_EXP}.requests.get", return_value=self._mock_response({"key": "val"})):
            result = explore._get("http://x", {})
        assert result == {"key": "val"}

    def test_returns_none_on_http_error(self):
        import requests as req_lib

        resp = mock.MagicMock()
        resp.raise_for_status.side_effect = req_lib.exceptions.HTTPError("500")
        with mock.patch(f"{_EXP}.requests.get", return_value=resp):
            result = explore._get("http://x", {})
        assert result is None

    def test_returns_none_on_exception(self):
        with mock.patch(f"{_EXP}.requests.get", side_effect=Exception("timeout")):
            result = explore._get("http://x", {})
        assert result is None

    def test_passes_params(self):
        with mock.patch(f"{_EXP}.requests.get", return_value=self._mock_response({})) as mock_get:
            explore._get("http://x", {"h": "v"}, params={"page": 1})
        mock_get.assert_called_once_with("http://x", headers={"h": "v"}, params={"page": 1}, timeout=30)


# ---------------------------------------------------------------------------
# _load_account
# ---------------------------------------------------------------------------


class TestLoadAccount:

    def _make_hits(self, accounts):
        return {"hits": {"hits": [{"_id": f"id-{a['chatwoot_account_id']}", "_source": a} for a in accounts]}}

    def test_returns_first_account_when_no_filter(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_hits(
            [
                {"chatwoot_account_id": "100", "chatwoot_account_title": "A"},
                {"chatwoot_account_id": "200", "chatwoot_account_title": "B"},
            ]
        )
        acc, esid = explore._load_account(mock_es)
        assert acc["chatwoot_account_id"] == "100"
        assert esid == "id-100"

    def test_returns_matching_account_with_filter(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_hits(
            [
                {"chatwoot_account_id": "100", "chatwoot_account_title": "A"},
                {"chatwoot_account_id": "200", "chatwoot_account_title": "B"},
            ]
        )
        acc, esid = explore._load_account(mock_es, account_id_filter="200")
        assert acc["chatwoot_account_id"] == "200"

    def test_falls_back_to_first_when_filter_not_found(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._make_hits(
            [
                {"chatwoot_account_id": "100", "chatwoot_account_title": "A"},
            ]
        )
        acc, esid = explore._load_account(mock_es, account_id_filter="999")
        assert acc["chatwoot_account_id"] == "100"

    def test_returns_none_when_no_accounts(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        acc, esid = explore._load_account(mock_es)
        assert acc is None
        assert esid is None


# ---------------------------------------------------------------------------
# _flatten_keys
# ---------------------------------------------------------------------------


class TestFlattenKeys:

    def test_flat_dict(self):
        keys = list(explore._flatten_keys({"a": 1, "b": 2}))
        assert "a" in keys
        assert "b" in keys

    def test_nested_dict(self):
        keys = list(explore._flatten_keys({"parent": {"child": 1}}))
        assert "parent.child" in keys

    def test_list_of_dicts(self):
        keys = list(explore._flatten_keys({"items": [{"id": 1}]}))
        assert "items[].id" in keys

    def test_empty_list(self):
        keys = list(explore._flatten_keys({"items": []}))
        assert "items[] (empty)" in keys

    def test_scalar_value(self):
        keys = list(explore._flatten_keys(42))
        assert keys == [""]

    def test_deeply_nested(self):
        keys = list(explore._flatten_keys({"a": {"b": {"c": 99}}}))
        assert "a.b.c" in keys


# ---------------------------------------------------------------------------
# _is_populated
# ---------------------------------------------------------------------------


class TestIsPopulated:

    def test_none_is_not_populated(self):
        assert explore._is_populated(None) is False

    def test_empty_string_is_not_populated(self):
        assert explore._is_populated("") is False

    def test_empty_list_is_not_populated(self):
        assert explore._is_populated([]) is False

    def test_empty_dict_is_not_populated(self):
        assert explore._is_populated({}) is False

    def test_non_empty_string_is_populated(self):
        assert explore._is_populated("hello") is True

    def test_zero_is_populated(self):
        assert explore._is_populated(0) is True

    def test_non_empty_list_is_populated(self):
        assert explore._is_populated([1]) is True


# ---------------------------------------------------------------------------
# _summarise
# ---------------------------------------------------------------------------


class TestSummarise:

    def test_none_data(self, capsys):
        explore._summarise("Test", None)
        out = capsys.readouterr().out
        assert "no data" in out

    def test_empty_list(self, capsys):
        explore._summarise("Test", [])
        out = capsys.readouterr().out
        assert "empty list" in out

    def test_dict_with_data_key_wrapping_list(self, capsys):
        explore._summarise("Test", {"data": [{"id": 1, "name": "A"}]})
        out = capsys.readouterr().out
        assert "id" in out

    def test_dict_with_data_key_wrapping_empty_list(self, capsys):
        explore._summarise("Test", {"data": []})
        out = capsys.readouterr().out
        assert "empty list" in out

    def test_dict_with_data_key_wrapping_dict(self, capsys):
        explore._summarise("Test", {"data": {"count": 5}})
        out = capsys.readouterr().out
        assert "count" in out

    def test_non_dict_sample(self, capsys):
        explore._summarise("Test", 42)
        out = capsys.readouterr().out
        assert "42" in out

    def test_list_of_dicts_shows_first(self, capsys):
        explore._summarise("Test", [{"x": 1}, {"x": 2}])
        out = capsys.readouterr().out
        assert "first of 2" in out


# ---------------------------------------------------------------------------
# _print_fields
# ---------------------------------------------------------------------------


class TestPrintFields:

    def test_nested_dict_fields(self, capsys):
        explore._print_fields({"parent": {"child": "val"}})
        out = capsys.readouterr().out
        assert "parent" in out
        assert "child" in out

    def test_list_of_objects(self, capsys):
        explore._print_fields({"items": [{"id": 1}]})
        out = capsys.readouterr().out
        assert "items" in out
        assert "array of objects" in out

    def test_empty_list(self, capsys):
        explore._print_fields({"items": []})
        out = capsys.readouterr().out
        assert "empty" in out

    def test_long_value_truncated(self, capsys):
        explore._print_fields({"key": "x" * 100})
        out = capsys.readouterr().out
        assert "..." in out

    def test_list_of_scalars(self, capsys):
        explore._print_fields({"tags": ["a", "b", "c", "d"]})
        out = capsys.readouterr().out
        assert "tags" in out


# ---------------------------------------------------------------------------
# Probe functions (representative subset)
# ---------------------------------------------------------------------------


class TestProbes:

    def test_probe_summary_calls_correct_url(self):
        with (
            mock.patch(f"{_EXP}._get", return_value={"conversations_count": 10}) as mock_get,
            mock.patch(f"{_EXP}._summarise"),
        ):
            explore.probe_summary("123", {"api_access_token": "tok"})
        url = mock_get.call_args[0][0]
        assert "123" in url
        assert "summary" in url

    def test_probe_conversations_handles_payload(self, capsys):
        data = {"data": {"meta": {"all_count": 5}, "payload": [{"id": 1, "status": "open"}]}}
        with mock.patch(f"{_EXP}._get", return_value=data):
            explore.probe_conversations("123", {})
        out = capsys.readouterr().out
        assert "Conversations" in out

    def test_probe_conversations_handles_empty_payload(self, capsys):
        data = {"data": {"meta": {}, "payload": []}}
        with mock.patch(f"{_EXP}._get", return_value=data):
            explore.probe_conversations("123", {})
        out = capsys.readouterr().out
        assert "no conversations" in out

    def test_probe_conversations_handles_none(self, capsys):
        with mock.patch(f"{_EXP}._get", return_value=None):
            explore.probe_conversations("123", {})

    def test_probe_inboxes_unwraps_payload(self):
        with (
            mock.patch(f"{_EXP}._get", return_value={"payload": [{"id": 1}]}) as mock_get,
            mock.patch(f"{_EXP}._summarise") as mock_sum,
        ):
            explore.probe_inboxes("123", {})
        # Should pass the unwrapped payload list, not the full response
        mock_sum.assert_called_once()
        assert mock_sum.call_args[0][1] == [{"id": 1}]

    def test_probe_inboxes_no_payload_key(self):
        with (
            mock.patch(f"{_EXP}._get", return_value={"other": "data"}) as mock_get,
            mock.patch(f"{_EXP}._summarise") as mock_sum,
        ):
            explore.probe_inboxes("123", {})
        mock_sum.assert_called_once()
        assert mock_sum.call_args[0][1] == {"other": "data"}

    def test_probe_labels_unwraps_payload(self):
        with (
            mock.patch(f"{_EXP}._get", return_value={"payload": ["label1"]}) as mock_get,
            mock.patch(f"{_EXP}._summarise") as mock_sum,
        ):
            explore.probe_labels("123", {})
        mock_sum.assert_called_once()
        assert mock_sum.call_args[0][1] == ["label1"]

    def test_probe_individual_metrics_iterates_all(self, capsys):
        with mock.patch(f"{_EXP}._get", return_value=[{"value": 10}]):
            explore.probe_individual_metrics("123", {})
        out = capsys.readouterr().out
        assert "conversations_count" in out
        assert "avg_first_response_time" in out
        assert "avg_reply_time" in out

    def test_probe_individual_metrics_handles_empty(self, capsys):
        with mock.patch(f"{_EXP}._get", return_value=[]):
            explore.probe_individual_metrics("123", {})
        out = capsys.readouterr().out
        assert "empty list" in out

    def test_probe_individual_metrics_handles_non_list(self, capsys):
        with mock.patch(f"{_EXP}._get", return_value=None):
            explore.probe_individual_metrics("123", {})
        out = capsys.readouterr().out
        assert "None" in out

    def test_probe_agents_calls_summarise(self):
        with mock.patch(f"{_EXP}._get", return_value=[{"id": 1}]), mock.patch(f"{_EXP}._summarise") as mock_sum:
            explore.probe_agents("123", {})
        mock_sum.assert_called_once()

    def test_probe_teams_calls_summarise(self):
        with mock.patch(f"{_EXP}._get", return_value=[{"id": 1}]), mock.patch(f"{_EXP}._summarise") as mock_sum:
            explore.probe_teams("123", {})
        mock_sum.assert_called_once()

    def test_probe_csat_calls_summarise(self):
        with mock.patch(f"{_EXP}._get", return_value=[]), mock.patch(f"{_EXP}._summarise") as mock_sum:
            explore.probe_csat("123", {})
        mock_sum.assert_called_once()

    def test_probe_overview_calls_summarise(self):
        with mock.patch(f"{_EXP}._get", return_value={}), mock.patch(f"{_EXP}._summarise") as mock_sum:
            explore.probe_overview("123", {})
        mock_sum.assert_called_once()

    def test_probe_contacts_calls_summarise(self):
        with mock.patch(f"{_EXP}._get", return_value=[{"id": 1}]), mock.patch(f"{_EXP}._summarise") as mock_sum:
            explore.probe_contacts("123", {})
        mock_sum.assert_called_once()

    def test_probe_custom_attributes_calls_twice(self):
        with mock.patch(f"{_EXP}._get", return_value=[]) as mock_get, mock.patch(f"{_EXP}._summarise") as mock_sum:
            explore.probe_custom_attributes("123", {})
        assert mock_get.call_count == 2
        assert mock_sum.call_count == 2


# ---------------------------------------------------------------------------
# main (explore_fields)
# ---------------------------------------------------------------------------


class TestExploreMain:

    def _mock_account_source(self):
        return {
            "chatwoot_account_id": "123",
            "chatwoot_account_title": "TestAccount",
            "chatwoot_api_token": "enc-token",
        }

    def test_returns_early_when_es_unavailable(self):
        with mock.patch(f"{_EXP}.get_elasticsearch_client", return_value=None):
            explore.main({})

    def test_returns_early_when_no_accounts(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_EXP}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_EXP}._load_account", return_value=(None, None)),
        ):
            explore.main({})

    def test_returns_early_when_missing_id(self):
        mock_es = mock.MagicMock()
        bad_account = {"chatwoot_account_id": None, "chatwoot_account_title": "X", "chatwoot_api_token": "tok"}
        with (
            mock.patch(f"{_EXP}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_EXP}._load_account", return_value=(bad_account, "id-1")),
            mock.patch(f"{_EXP}.decrypt") as mock_decrypt,
        ):
            explore.main({})
        mock_decrypt.assert_not_called()

    def test_returns_early_when_missing_token(self):
        mock_es = mock.MagicMock()
        bad_account = {"chatwoot_account_id": "123", "chatwoot_account_title": "X", "chatwoot_api_token": None}
        with (
            mock.patch(f"{_EXP}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_EXP}._load_account", return_value=(bad_account, "id-1")),
            mock.patch(f"{_EXP}.decrypt") as mock_decrypt,
        ):
            explore.main({})
        mock_decrypt.assert_not_called()

    def test_passes_account_id_filter(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_EXP}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_EXP}._load_account", return_value=(None, None)) as mock_load,
        ):
            explore.main({"account_id": "456"})
        mock_load.assert_called_once_with(mock_es, "456")

    def test_happy_path_calls_all_probes(self):
        mock_es = mock.MagicMock()
        account = self._mock_account_source()
        with (
            mock.patch(f"{_EXP}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_EXP}._load_account", return_value=(account, "id-123")),
            mock.patch(f"{_EXP}.decrypt", return_value="plain-token"),
            mock.patch(f"{_EXP}.probe_summary") as m_sum,
            mock.patch(f"{_EXP}.probe_individual_metrics"),
            mock.patch(f"{_EXP}.probe_conversations"),
            mock.patch(f"{_EXP}.probe_contacts"),
            mock.patch(f"{_EXP}.probe_agents"),
            mock.patch(f"{_EXP}.probe_inboxes"),
            mock.patch(f"{_EXP}.probe_teams"),
            mock.patch(f"{_EXP}.probe_labels"),
            mock.patch(f"{_EXP}.probe_csat"),
            mock.patch(f"{_EXP}.probe_overview"),
            mock.patch(f"{_EXP}.probe_custom_attributes"),
        ):
            explore.main({})
        m_sum.assert_called_once_with("123", {"api_access_token": "plain-token"})

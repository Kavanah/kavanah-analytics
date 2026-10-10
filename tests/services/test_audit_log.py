"""
Tests for the Audit Log Service.

Covers:
  ensure_index_exists     — creates index/template, put_mapping, ES unavailable
  delete_older_than       — success, ES unavailable, exception
  _is_scanner             — injection username, scanner UA, unknown user
  _get_known_usernames    — success, ES unavailable, exception
  log_event               — normal, suspicious login, ES unavailable, optional fields
  query_events            — filters, pagination, exclude_scanners, ES unavailable
  _send_security_alert    — success, no recipient, SMTP error
  backfill_scanner_tags   — tags updated, no changes, ES unavailable
"""

import json
from unittest import mock

import pytest

from asset_management.web.services.audit_log_service import AuditLogService, _HUMAN_USERNAME_RE


# ---------------------------------------------------------------------------
# _is_scanner
# ---------------------------------------------------------------------------

class TestIsScanner:

    def test_injection_username_detected(self):
        assert AuditLogService._is_scanner("admin' OR 1=1--") is True

    def test_normal_username_not_scanner(self):
        assert AuditLogService._is_scanner("john.doe_123") is False

    def test_empty_username_not_scanner(self):
        assert AuditLogService._is_scanner("") is False

    def test_scanner_user_agent(self):
        assert AuditLogService._is_scanner("admin", user_agent="Mozilla/5.0 ZAP") is True

    def test_nikto_user_agent(self):
        assert AuditLogService._is_scanner("admin", user_agent="Nikto/2.1.5") is True

    def test_normal_user_agent(self):
        assert AuditLogService._is_scanner("admin", user_agent="Mozilla/5.0 Chrome") is False

    def test_unknown_user_failed_login(self):
        known = {"admin", "john"}
        assert AuditLogService._is_scanner("hacker", known_users=known, success=False) is True

    def test_known_user_not_scanner(self):
        known = {"admin", "john"}
        assert AuditLogService._is_scanner("admin", known_users=known, success=False) is False

    def test_unknown_user_successful_login_not_scanner(self):
        known = {"admin"}
        assert AuditLogService._is_scanner("unknown", known_users=known, success=True) is False

    def test_no_known_users_no_scanner_tag(self):
        assert AuditLogService._is_scanner("unknown", known_users=None, success=False) is False


# ---------------------------------------------------------------------------
# _get_known_usernames
# ---------------------------------------------------------------------------

class TestGetKnownUsernames:

    def test_returns_set_of_usernames(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.return_value = {"admin": {}, "john": {}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService._get_known_usernames()
        assert result == {"admin", "john"}

    def test_es_unavailable_returns_empty_set(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = AuditLogService._get_known_usernames()
        assert result == set()

    def test_exception_returns_empty_set(self):
        mock_es = mock.MagicMock()
        mock_es.security.get_user.side_effect = Exception("error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService._get_known_usernames()
        assert result == set()


# ---------------------------------------------------------------------------
# ensure_index_exists
# ---------------------------------------------------------------------------

class TestEnsureIndexExists:

    def test_creates_index_and_template(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("lib.es_helpers.put_regular_index_template") as mock_template, \
             mock.patch("lib.es_helpers.create_index_if_not_exists") as mock_create:
            AuditLogService.ensure_index_exists()
        mock_template.assert_called_once()
        mock_create.assert_called_once()
        mock_es.indices.put_mapping.assert_called_once()

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            AuditLogService.ensure_index_exists()  # should not raise

    def test_put_mapping_error_logged(self):
        mock_es = mock.MagicMock()
        mock_es.indices.put_mapping.side_effect = Exception("mapping error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch("lib.es_helpers.put_regular_index_template"), \
             mock.patch("lib.es_helpers.create_index_if_not_exists"):
            AuditLogService.ensure_index_exists()  # should not raise


# ---------------------------------------------------------------------------
# delete_older_than
# ---------------------------------------------------------------------------

class TestDeleteOlderThan:

    def test_success(self):
        mock_es = mock.MagicMock()
        mock_es.delete_by_query.return_value = {"deleted": 42}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.delete_older_than(90)
        assert result == 42

    def test_nothing_to_delete(self):
        mock_es = mock.MagicMock()
        mock_es.delete_by_query.return_value = {"deleted": 0}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.delete_older_than(90)
        assert result == 0

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = AuditLogService.delete_older_than(90)
        assert result == -1

    def test_exception(self):
        mock_es = mock.MagicMock()
        mock_es.delete_by_query.side_effect = Exception("error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.delete_older_than(90)
        assert result == -1


# ---------------------------------------------------------------------------
# log_event
# ---------------------------------------------------------------------------

class TestLogEvent:

    def test_normal_event(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.log_event("admin", "login", True, auth_method="form")
        mock_es.index.assert_called_once()
        doc = mock_es.index.call_args[1]["document"]
        assert doc["username"] == "admin"
        assert doc["action"] == "login"
        assert doc["success"] is True
        assert doc["auth_method"] == "form"
        assert doc["suspicious"] is False

    def test_suspicious_login_sends_alert(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch.object(AuditLogService, "_get_known_usernames", return_value={"admin"}), \
             mock.patch.object(AuditLogService, "_send_security_alert") as mock_alert:
            AuditLogService.log_event("unknown_user", "login", True, ip_address="1.2.3.4", auth_method="sso")
        doc = mock_es.index.call_args[1]["document"]
        assert doc["suspicious"] is True
        mock_alert.assert_called_once_with("unknown_user", "1.2.3.4", "sso")

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            AuditLogService.log_event("admin", "login", True)  # should not raise

    def test_optional_fields_omitted_when_empty(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.log_event("admin", "login", True)
        doc = mock_es.index.call_args[1]["document"]
        assert "auth_method" not in doc
        assert "ip_address" not in doc
        assert "user_agent" not in doc

    def test_all_optional_fields_included(self):
        mock_es = mock.MagicMock()
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.log_event(
                "admin", "delete_user", True,
                auth_method="form", mfa_method="totp", ip_address="1.2.3.4",
                user_agent="Chrome", failure_reason="", target_user="victim", detail="removed",
            )
        doc = mock_es.index.call_args[1]["document"]
        assert doc["target_user"] == "victim"
        assert doc["detail"] == "removed"
        assert doc["mfa_method"] == "totp"

    def test_index_error_does_not_raise(self):
        mock_es = mock.MagicMock()
        mock_es.index.side_effect = Exception("write error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.log_event("admin", "login", True)  # should not raise


# ---------------------------------------------------------------------------
# query_events
# ---------------------------------------------------------------------------

class TestQueryEvents:

    def _mock_search_response(self, events, total=None):
        if total is None:
            total = len(events)
        return {
            "hits": {
                "total": {"value": total},
                "hits": [
                    {"_id": f"id_{i}", "_source": evt}
                    for i, evt in enumerate(events)
                ],
            }
        }

    def test_no_filters(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([
            {"username": "admin", "action": "login"},
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.query_events()
        assert result["total"] == 1
        assert len(result["events"]) == 1
        assert result["events"][0]["_id"] == "id_0"

    def test_filter_by_username(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(username="admin")
        query = mock_es.search.call_args[1]["body"]["query"]
        assert {"term": {"username": "admin"}} in query["bool"]["filter"]

    def test_filter_by_action(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(action="login")
        query = mock_es.search.call_args[1]["body"]["query"]
        assert {"term": {"action": "login"}} in query["bool"]["filter"]

    def test_filter_by_success(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(success=False)
        query = mock_es.search.call_args[1]["body"]["query"]
        assert {"term": {"success": False}} in query["bool"]["filter"]

    def test_exclude_scanners(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(exclude_scanners=True)
        query = mock_es.search.call_args[1]["body"]["query"]
        scanner_filter = query["bool"]["filter"][0]
        assert "bool" in scanner_filter
        assert scanner_filter["bool"]["minimum_should_match"] == 1

    def test_pagination(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([], total=50)
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(size=10, from_offset=20)
        body = mock_es.search.call_args[1]["body"]
        assert body["size"] == 10
        assert body["from"] == 20

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = AuditLogService.query_events()
        assert result == {"events": [], "total": 0}

    def test_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("query error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.query_events()
        assert result == {"events": [], "total": 0}

    def test_username_in_filter_applied(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(username_in=["alice", "bob"])
        query = mock_es.search.call_args[1]["body"]["query"]
        assert {"terms": {"username": ["alice", "bob"]}} in query["bool"]["filter"]

    def test_username_in_empty_list_matches_nothing(self):
        """Empty whitelist must produce a terms filter with [] — not be skipped."""
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._mock_search_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.query_events(username_in=[])
        query = mock_es.search.call_args[1]["body"]["query"]
        assert {"terms": {"username": []}} in query["bool"]["filter"]


# ---------------------------------------------------------------------------
# daily_unique_logins
# ---------------------------------------------------------------------------

class TestDailyUniqueLogins:

    def _agg_response(self, buckets):
        return {"aggregations": {"per_day": {"buckets": buckets}}}

    def test_returns_date_count_pairs(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._agg_response([
            {"key_as_string": "2026-04-15T00:00:00.000Z", "unique_users": {"value": 3}},
            {"key_as_string": "2026-04-16T00:00:00.000Z", "unique_users": {"value": 5}},
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.daily_unique_logins(days=30)
        assert result == [
            {"date": "2026-04-15", "count": 3},
            {"date": "2026-04-16", "count": 5},
        ]

    def test_filters_login_success_and_excludes_scanners(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._agg_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.daily_unique_logins(days=7)
        body = mock_es.search.call_args[1]["body"]
        filters = body["query"]["bool"]["filter"]
        assert {"term": {"action": "login"}} in filters
        assert {"term": {"success": True}} in filters
        assert {"range": {"@timestamp": {"gte": "now-7d/d"}}} in filters

    def test_username_whitelist_applied(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._agg_response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.daily_unique_logins(days=30, username_whitelist={"alice"})
        filters = mock_es.search.call_args[1]["body"]["query"]["bool"]["filter"]
        assert any(f.get("terms", {}).get("username") == ["alice"] for f in filters)

    def test_es_unavailable_returns_empty_list(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            assert AuditLogService.daily_unique_logins() == []

    def test_exception_returns_empty_list(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("agg error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            assert AuditLogService.daily_unique_logins() == []


# ---------------------------------------------------------------------------
# logins_for_day
# ---------------------------------------------------------------------------

class TestLoginsForDay:

    def _response(self, buckets):
        return {"aggregations": {"per_user": {"buckets": buckets}}}

    def test_returns_per_user_summary(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([
            {
                "key": "alice",
                "doc_count": 3,
                "first_login": {"value_as_string": "2026-04-15T08:00:00Z"},
                "last_login": {"value_as_string": "2026-04-15T17:30:00Z"},
            },
            {
                "key": "bob",
                "doc_count": 1,
                "first_login": {"value_as_string": "2026-04-15T11:15:00Z"},
                "last_login": {"value_as_string": "2026-04-15T11:15:00Z"},
            },
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.logins_for_day("2026-04-15")
        assert result == [
            {
                "username": "alice", "login_count": 3,
                "first_login": "2026-04-15T08:00:00Z",
                "last_login": "2026-04-15T17:30:00Z",
            },
            {
                "username": "bob", "login_count": 1,
                "first_login": "2026-04-15T11:15:00Z",
                "last_login": "2026-04-15T11:15:00Z",
            },
        ]

    def test_query_uses_utc_day_boundaries(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.logins_for_day("2026-04-15")
        filters = mock_es.search.call_args[1]["body"]["query"]["bool"]["filter"]
        assert {
            "range": {
                "@timestamp": {
                    "gte": "2026-04-15T00:00:00Z",
                    "lt": "2026-04-15T00:00:00Z||+1d",
                }
            }
        } in filters

    def test_filters_to_successful_logins_only(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.logins_for_day("2026-04-15")
        filters = mock_es.search.call_args[1]["body"]["query"]["bool"]["filter"]
        assert {"term": {"action": "login"}} in filters
        assert {"term": {"success": True}} in filters

    def test_username_whitelist_applied(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            AuditLogService.logins_for_day("2026-04-15", username_whitelist={"alice"})
        filters = mock_es.search.call_args[1]["body"]["query"]["bool"]["filter"]
        assert any(f.get("terms", {}).get("username") == ["alice"] for f in filters)

    def test_es_unavailable_returns_empty_list(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            assert AuditLogService.logins_for_day("2026-04-15") == []

    def test_exception_returns_empty_list(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("agg error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            assert AuditLogService.logins_for_day("2026-04-15") == []


# ---------------------------------------------------------------------------
# active_usernames
# ---------------------------------------------------------------------------

class TestActiveUsernames:

    def _now_ms(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).timestamp() * 1000

    def _bucket(self, username, last_login_ms, last_logout_ms=None):
        return {
            "key": username,
            "last_login": {"latest": {"value": last_login_ms}},
            "last_logout": {"latest": {"value": last_logout_ms}},
        }

    def _response(self, buckets):
        return {"aggregations": {"per_user": {"buckets": buckets}}}

    def test_includes_user_with_login_and_no_logout(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([
            self._bucket("alice", self._now_ms() - 60_000, None),
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.active_usernames()
        assert result == {"alice"}

    def test_includes_user_when_login_after_logout(self):
        now = self._now_ms()
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([
            self._bucket("alice", now - 60_000, now - 120_000),
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            assert AuditLogService.active_usernames() == {"alice"}

    def test_excludes_user_when_logout_after_login(self):
        now = self._now_ms()
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([
            self._bucket("alice", now - 120_000, now - 60_000),
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            assert AuditLogService.active_usernames() == set()

    def test_excludes_user_with_no_login_in_window(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = self._response([
            self._bucket("alice", None, self._now_ms() - 120_000),
        ])
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            assert AuditLogService.active_usernames() == set()

    def test_es_unavailable_returns_empty_set(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            assert AuditLogService.active_usernames() == set()


# ---------------------------------------------------------------------------
# _send_security_alert
# ---------------------------------------------------------------------------

class TestSendSecurityAlert:

    _SMTP_SETTINGS = json.dumps({
        "smtp_host": "smtp.example.com", "smtp_port": "587",
        "smtp_username": "user", "smtp_password": "pass",
        "from_email": "noreply@example.com", "smtp_use_tls": True,
    })

    def test_sends_email(self):
        mock_server = mock.MagicMock()
        with mock.patch.dict("os.environ", {"SECURITY_ALERT_EMAIL": "admin@example.com"}), \
             mock.patch("builtins.open", mock.mock_open(read_data=self._SMTP_SETTINGS)), \
             mock.patch("asset_management.web.services.audit_log_service.smtplib.SMTP", return_value=mock_server):
            AuditLogService._send_security_alert("unknown", "1.2.3.4", "sso")
        mock_server.sendmail.assert_called_once()

    def test_no_recipient_skips(self):
        with mock.patch.dict("os.environ", {"SECURITY_ALERT_EMAIL": ""}):
            AuditLogService._send_security_alert("unknown", "1.2.3.4", "sso")  # no error

    def test_smtp_config_missing(self):
        with mock.patch.dict("os.environ", {"SECURITY_ALERT_EMAIL": "admin@example.com"}), \
             mock.patch("builtins.open", side_effect=Exception("no file")):
            AuditLogService._send_security_alert("unknown", "1.2.3.4", "sso")  # no error

    def test_smtp_incomplete_config(self):
        incomplete = json.dumps({"smtp_host": "mail.example.com"})
        with mock.patch.dict("os.environ", {"SECURITY_ALERT_EMAIL": "admin@example.com"}), \
             mock.patch("builtins.open", mock.mock_open(read_data=incomplete)):
            AuditLogService._send_security_alert("unknown", "1.2.3.4", "sso")  # no error

    def test_smtp_ssl_path(self):
        settings = json.dumps({
            "smtp_host": "smtp.example.com", "smtp_port": "465",
            "smtp_username": "user", "smtp_password": "pass",
            "from_email": "noreply@example.com", "smtp_use_tls": False,
        })
        mock_server = mock.MagicMock()
        with mock.patch.dict("os.environ", {"SECURITY_ALERT_EMAIL": "admin@example.com"}), \
             mock.patch("builtins.open", mock.mock_open(read_data=settings)), \
             mock.patch("asset_management.web.services.audit_log_service.smtplib.SMTP_SSL", return_value=mock_server):
            AuditLogService._send_security_alert("unknown", "1.2.3.4", "sso")
        mock_server.login.assert_called_once()

    def test_smtp_send_error(self):
        mock_server = mock.MagicMock()
        mock_server.sendmail.side_effect = Exception("SMTP failure")
        with mock.patch.dict("os.environ", {"SECURITY_ALERT_EMAIL": "admin@example.com"}), \
             mock.patch("builtins.open", mock.mock_open(read_data=self._SMTP_SETTINGS)), \
             mock.patch("asset_management.web.services.audit_log_service.smtplib.SMTP", return_value=mock_server):
            AuditLogService._send_security_alert("unknown", "1.2.3.4", "sso")  # no error


# ---------------------------------------------------------------------------
# backfill_scanner_tags
# ---------------------------------------------------------------------------

class TestBackfillScannerTags:

    def test_tags_updated(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "_scroll_id": "abc",
            "hits": {"hits": [
                {"_id": "1", "_source": {"username": "admin' OR 1=1", "user_agent": "", "success": False}},
            ]},
        }
        mock_es.scroll.return_value = {"hits": {"hits": []}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch.object(AuditLogService, "_get_known_usernames", return_value={"admin"}):
            result = AuditLogService.backfill_scanner_tags()
        assert result == 1
        mock_es.bulk.assert_called_once()

    def test_no_changes_needed(self):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "_scroll_id": "abc",
            "hits": {"hits": [
                {"_id": "1", "_source": {"username": "admin", "user_agent": "Chrome", "success": True, "is_scanner": False}},
            ]},
        }
        mock_es.scroll.return_value = {"hits": {"hits": []}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es), \
             mock.patch.object(AuditLogService, "_get_known_usernames", return_value={"admin"}):
            result = AuditLogService.backfill_scanner_tags()
        assert result == 0
        mock_es.bulk.assert_not_called()

    def test_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            result = AuditLogService.backfill_scanner_tags()
        assert result == -1

    def test_exception(self):
        mock_es = mock.MagicMock()
        mock_es.search.side_effect = Exception("scroll error")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=mock_es):
            result = AuditLogService.backfill_scanner_tags()
        assert result == -1

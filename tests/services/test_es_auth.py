"""Tests for lib.es_auth."""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

import lib.es_auth as es_auth
from lib.es_auth import authenticate_user, has_role, get_elasticsearch_client_for_auth


# ---------------------------------------------------------------------------
# get_elasticsearch_client_for_auth
# ---------------------------------------------------------------------------

class TestGetElasticsearchClientForAuth:
    def test_returns_none_when_cert_missing_ssl(self):
        """With SSL enabled and no cert file → immediate None, no connection."""
        with mock.patch.dict("os.environ", {"ES_USE_SSL": "true", "ES01_INTERNAL_IP": "127.0.0.1"}):
            # cert file won't exist at /app/data/certs/es01_http_ca.crt
            result = get_elasticsearch_client_for_auth("user", "pass")
        assert result is None

    def test_returns_client_no_ssl(self):
        mock_es = mock.MagicMock()
        mock_es.ping.return_value = True
        with mock.patch.dict("os.environ", {"ES_USE_SSL": "false", "ES01_INTERNAL_IP": "127.0.0.1"}):
            with mock.patch("lib.es_auth.Elasticsearch", return_value=mock_es):
                result = get_elasticsearch_client_for_auth("user", "pass")
        assert result is mock_es

    def test_returns_none_on_exception_no_ssl(self):
        with mock.patch.dict("os.environ", {"ES_USE_SSL": "false", "ES01_INTERNAL_IP": "127.0.0.1"}):
            with mock.patch("lib.es_auth.Elasticsearch", side_effect=Exception("conn error")):
                result = get_elasticsearch_client_for_auth("user", "pass")
        assert result is None

    def test_returns_none_when_cert_load_fails(self):
        mock_ctx = mock.MagicMock()
        mock_ctx.load_verify_locations.side_effect = Exception("cert error")
        with mock.patch.dict("os.environ", {"ES_USE_SSL": "true", "ES01_INTERNAL_IP": "127.0.0.1"}):
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("ssl.create_default_context", return_value=mock_ctx):
                    result = get_elasticsearch_client_for_auth("user", "pass")
        assert result is None

    def test_returns_client_when_ssl_succeeds(self):
        mock_ctx = mock.MagicMock()
        mock_es = mock.MagicMock()
        mock_es.ping.return_value = True
        with mock.patch.dict("os.environ", {"ES_USE_SSL": "true", "ES01_INTERNAL_IP": "127.0.0.1"}):
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("ssl.create_default_context", return_value=mock_ctx):
                    with mock.patch("lib.es_auth.Elasticsearch", return_value=mock_es):
                        result = get_elasticsearch_client_for_auth("user", "pass")
        assert result is mock_es

    def test_returns_none_when_es_connect_raises_ssl(self):
        mock_ctx = mock.MagicMock()
        with mock.patch.dict("os.environ", {"ES_USE_SSL": "true", "ES01_INTERNAL_IP": "127.0.0.1"}):
            with mock.patch("os.path.exists", return_value=True):
                with mock.patch("ssl.create_default_context", return_value=mock_ctx):
                    with mock.patch("lib.es_auth.Elasticsearch", side_effect=Exception("tls error")):
                        result = get_elasticsearch_client_for_auth("user", "pass")
        assert result is None


# ---------------------------------------------------------------------------
# authenticate_user
# ---------------------------------------------------------------------------

class TestAuthenticateUser:
    def test_returns_failure_when_client_none(self):
        with mock.patch(
            "lib.es_auth.get_elasticsearch_client_for_auth", return_value=None
        ):
            result = authenticate_user("alice", "wrong")

        assert result["success"] is False
        assert "Elasticsearch" in result["error"]

    def test_returns_success_on_valid_auth(self):
        mock_es = mock.MagicMock()
        mock_es.security.authenticate.return_value = {
            "username": "alice",
            "roles": ["org_test", "asset_admin"],
            "full_name": "Alice Test",
            "email": "alice@example.com",
        }
        with mock.patch(
            "lib.es_auth.get_elasticsearch_client_for_auth", return_value=mock_es
        ):
            result = authenticate_user("alice", "secret")

        assert result["success"] is True
        assert result["username"] == "alice"
        assert "org_test" in result["roles"]
        assert result["full_name"] == "Alice Test"
        assert result["email"] == "alice@example.com"

    def test_returns_failure_on_auth_exception(self):
        mock_es = mock.MagicMock()
        mock_es.security.authenticate.side_effect = Exception("bad credentials")
        with mock.patch(
            "lib.es_auth.get_elasticsearch_client_for_auth", return_value=mock_es
        ):
            result = authenticate_user("alice", "wrong")

        assert result["success"] is False
        assert "Invalid credentials" in result["error"]

    def test_uses_username_from_es_response(self):
        mock_es = mock.MagicMock()
        mock_es.security.authenticate.return_value = {
            "username": "alice_normalized",
            "roles": [],
            "full_name": "",
            "email": "",
        }
        with mock.patch(
            "lib.es_auth.get_elasticsearch_client_for_auth", return_value=mock_es
        ):
            result = authenticate_user("Alice", "secret")

        assert result["username"] == "alice_normalized"

    def test_returns_failure_on_outer_exception(self):
        with mock.patch(
            "lib.es_auth.get_elasticsearch_client_for_auth",
            side_effect=Exception("unexpected"),
        ):
            result = authenticate_user("alice", "pass")

        assert result["success"] is False

    def test_defaults_empty_strings_for_missing_fields(self):
        mock_es = mock.MagicMock()
        mock_es.security.authenticate.return_value = {
            "username": "bob",
            "roles": [],
            # no full_name or email
        }
        with mock.patch(
            "lib.es_auth.get_elasticsearch_client_for_auth", return_value=mock_es
        ):
            result = authenticate_user("bob", "pass")

        assert result["full_name"] == ""
        assert result["email"] == ""


# ---------------------------------------------------------------------------
# has_role
# ---------------------------------------------------------------------------

class TestHasRole:
    def test_returns_false_when_auth_fails(self):
        with mock.patch(
            "lib.es_auth.authenticate_user",
            return_value={"success": False, "error": "bad creds"},
        ):
            result = has_role("alice", "wrong", "asset_admin")

        assert result is False

    def test_returns_true_when_user_has_role(self):
        with mock.patch(
            "lib.es_auth.authenticate_user",
            return_value={
                "success": True,
                "roles": ["asset_admin", "org_test"],
                "username": "alice",
                "full_name": "",
                "email": "",
            },
        ):
            result = has_role("alice", "pass", "asset_admin")

        assert result is True

    def test_returns_false_when_user_lacks_role(self):
        with mock.patch(
            "lib.es_auth.authenticate_user",
            return_value={
                "success": True,
                "roles": ["org_test"],
                "username": "alice",
                "full_name": "",
                "email": "",
            },
        ):
            result = has_role("alice", "pass", "asset_admin")

        assert result is False

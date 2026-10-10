"""
Tests for fb_oauth_server.py — OAuth callback and initiation routes.

Covers:
  /                      — home page
  /status                — health check
  /youtube/auth_callback — YouTube OAuth token exchange
  /youtube/<auth_url>    — YouTube OAuth initiation
  /<app>/<user>          — Facebook OAuth initiation
  /auth_callback — Facebook OAuth callback (canonical)
  get_fboauth_hostname   — config file reader
  stop_gunicorn          — process management
  start_gunicorn         — process management

All ES, HTTP, encryption, and file I/O calls are mocked.
"""

import json
import pytest
from unittest import mock

import fb_oauth_server as oauth_app

_MOD = "fb_oauth_server"


@pytest.fixture
def client():
    oauth_app.app.config["TESTING"] = True
    with oauth_app.app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Basic routes
# ---------------------------------------------------------------------------


class TestHome:

    def test_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"Page not found" in resp.data


class TestStatus:

    def test_returns_running(self, client):
        resp = client.get("/status")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"


# ---------------------------------------------------------------------------
# get_fboauth_hostname
# ---------------------------------------------------------------------------


class TestGetFboauthHostname:

    def test_returns_hostname_from_config(self):
        yaml_data = {"fboauth": {"hostname": "oauth.example.com"}}
        with mock.patch("builtins.open", mock.mock_open()), mock.patch(f"{_MOD}.yaml.safe_load", return_value=yaml_data):
            result = oauth_app.get_fboauth_hostname()
        assert result == "oauth.example.com"

    def test_returns_empty_when_no_hostname(self):
        yaml_data = {"fboauth": {}}
        with mock.patch("builtins.open", mock.mock_open()), mock.patch(f"{_MOD}.yaml.safe_load", return_value=yaml_data):
            result = oauth_app.get_fboauth_hostname()
        assert result == ""

    def test_returns_empty_on_exception(self):
        with mock.patch("builtins.open", side_effect=FileNotFoundError("no file")):
            result = oauth_app.get_fboauth_hostname()
        assert result == ""


# ---------------------------------------------------------------------------
# YouTube OAuth callback
# ---------------------------------------------------------------------------


class TestYoutubeAuthCallback:

    def test_missing_code_returns_400(self, client):
        with mock.patch(f"{_MOD}.get_elasticsearch_client"):
            resp = client.get("/youtube/auth_callback?state=abc")
        assert resp.status_code == 400

    def test_missing_state_returns_400(self, client):
        with mock.patch(f"{_MOD}.get_elasticsearch_client"):
            resp = client.get("/youtube/auth_callback?code=xyz")
        assert resp.status_code == 400

    def test_es_unavailable_returns_500(self, client):
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=None):
            resp = client.get("/youtube/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 500

    def test_bad_credentials_returns_500(self, client):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", side_effect=Exception("decrypt fail")),
        ):
            resp = client.get("/youtube/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 500

    def test_no_refresh_token_returns_400(self, client):
        mock_es = mock.MagicMock()
        creds = {"client_id": "cid", "client_secret": "sec"}
        token_resp = mock.MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}  # no refresh_token

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", return_value=creds),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
            mock.patch(f"{_MOD}.requests.post", return_value=token_resp),
        ):
            resp = client.get("/youtube/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 400

    def test_no_account_found_returns_404(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        creds = {"client_id": "cid", "client_secret": "sec"}
        token_resp = mock.MagicMock()
        token_resp.json.return_value = {"refresh_token": "rt", "access_token": "at"}

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", return_value=creds),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
            mock.patch(f"{_MOD}.requests.post", return_value=token_resp),
        ):
            resp = client.get("/youtube/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 404

    def test_success_saves_token(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "acc-1", "_source": {"youtube_account_title": "My Channel"}},
                ]
            }
        }
        creds = {"client_id": "cid", "client_secret": "sec"}
        token_resp = mock.MagicMock()
        token_resp.json.return_value = {"refresh_token": "rt", "access_token": "at"}
        # the callback now probes the authorized identity's owned
        # channels via requests.get — mock it so the test stays offline. The
        # guard/dedup behaviors are covered in
        # the release-plan tests
        probe_resp = mock.MagicMock(status_code=200)
        probe_resp.json.return_value = {"items": [{"id": "UC1", "snippet": {"title": "My Channel"}}]}

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", return_value=creds),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
            mock.patch(f"{_MOD}.requests.post", return_value=token_resp),
            mock.patch(f"{_MOD}.requests.get", return_value=probe_resp),
            mock.patch(f"{_MOD}.encrypt", return_value="encrypted_rt"),
        ):
            resp = client.get("/youtube/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 200
        mock_es.index.assert_called_once()

    def test_hostname_missing_returns_500(self, client):
        mock_es = mock.MagicMock()
        creds = {"client_id": "cid", "client_secret": "sec"}
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", return_value=creds),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value=""),
        ):
            resp = client.get("/youtube/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# YouTube OAuth initiation
# ---------------------------------------------------------------------------


class TestYoutubeAuthInitiate:

    def test_wrong_url_length_returns_404(self, client):
        resp = client.get("/youtube/short")
        assert resp.status_code == 404

    def test_es_unavailable_returns_500(self, client):
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=None):
            resp = client.get("/youtube/abcdefghijklmno")  # 15 chars
        assert resp.status_code == 500

    def test_no_account_returns_404(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/youtube/abcdefghijklmno")
        assert resp.status_code == 404

    def test_missing_state_is_minted_for_this_attempt(self, client):
        # the initiate leg no longer depends on a state stamped at
        # asset creation — it mints a fresh, time-boxed one per click and
        # persists it on the doc, so a stateless doc is authorizable.
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "acc-1", "_source": {"youtube_account_auth_state": None}},
                ]
            }
        }
        creds = {"client_id": "cid", "client_secret": "sec"}
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", return_value=creds),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
        ):
            resp = client.get("/youtube/abcdefghijklmno")
        assert resp.status_code == 200
        mock_es.update.assert_called_once()
        minted = mock_es.update.call_args.kwargs["doc"]
        assert minted["youtube_account_auth_state"] and minted["youtube_account_auth_state_expires_at"]

    def test_success_returns_html_with_oauth_url(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "acc-1",
                        "_source": {
                            "youtube_account_auth_state": "state123",
                            "youtube_account_email": "test@example.com",
                            "youtube_account_title": "My Channel",
                        },
                    },
                ]
            }
        }
        creds = {"client_id": "cid", "client_secret": "sec"}

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.load_google_oauth_client", return_value=creds),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
        ):
            resp = client.get("/youtube/abcdefghijklmno")
        assert resp.status_code == 200
        # codeql[py/incomplete-url-substring-sanitization]: assertion,
        # not a sanitization check. Confirms the response redirects to Google's
        # OAuth endpoint.
        assert b"accounts.google.com" in resp.data


# ---------------------------------------------------------------------------
# Facebook OAuth initiation
# ---------------------------------------------------------------------------


class TestFacebookAuthInitiate:

    def test_wrong_url_length_returns_404(self, client):
        resp = client.get("/app123/short")
        assert resp.status_code == 404

    def test_es_unavailable_returns_500(self, client):
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=None):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 500

    def test_app_not_found_returns_404(self, client):
        mock_es = mock.MagicMock()
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 404

    def test_missing_client_id_returns_400(self, client):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"fb_app_id": None}}
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 400

    def test_user_not_found_returns_500(self, client):
        # abort(404) inside the try block is caught by the outer except Exception
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_title": "App"}}
        mock_es.search.return_value = {"hits": {"hits": []}}
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 500

    def test_success_ads_role_returns_oauth_url(self, client):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_title": "App"}}
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "u1",
                        "_source": {
                            "fb_app_user_title": "User1",
                            "fb_app_user_role": "Ads",
                            "fb_app_user_auth_state": "state123",
                            "fb_app_esid": "app-esid-1",
                        },
                    },
                ]
            }
        }
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
        ):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 200
        assert b"ads_read" in resp.data

    def test_success_pages_role_includes_instagram_scope(self, client):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_title": "App"}}
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "u1",
                        "_source": {
                            "fb_app_user_title": "User1",
                            "fb_app_user_role": "Pages",
                            "fb_app_user_auth_state": "state123",
                            "fb_app_esid": "app-esid-1",
                        },
                    },
                ]
            }
        }
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
        ):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 200
        assert b"instagram_manage_insights" in resp.data

    def test_missing_state_is_minted_for_this_attempt(self, client):
        # a fresh state is minted per click and persisted on the user
        # doc; the stored value (None here) no longer gates the initiate leg.
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"fb_app_id": "111"}}
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "u1",
                        "_source": {
                            "fb_app_user_auth_state": None,
                            "fb_app_esid": "app-esid-1",
                        },
                    },
                ]
            }
        }
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
        ):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 200
        mock_es.update.assert_called_once()
        minted = mock_es.update.call_args.kwargs["doc"]
        assert minted["fb_app_user_auth_state"] and minted["fb_app_user_auth_state_expires_at"]
        assert minted["fb_app_user_auth_state"].encode() in resp.data

    def test_missing_fb_app_esid_returns_400(self, client):
        mock_es = mock.MagicMock()
        mock_es.get.return_value = {"_source": {"fb_app_id": "111"}}
        mock_es.search.return_value = {"hits": {"hits": [{"_id": "u1", "_source": {"fb_app_user_auth_state": "s"}}]}}
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/app123/abcdefghijklmno")
        assert resp.status_code == 400
        mock_es.update.assert_not_called()


# ---------------------------------------------------------------------------
# Facebook OAuth callback
# ---------------------------------------------------------------------------


class TestFacebookCallback:
    # callback route changed from /<auth_url_app>/auth_callback to a
    # canonical /auth_callback. The user is resolved by `state` first, then
    # the FB App is looked up by reading fb_app_esid off the user doc — so
    # the user search now precedes the app get (reverse of the old order).
    # Mocks below feed fb_app_esid via the user-search result.

    def test_missing_code_returns_400(self, client):
        resp = client.get("/auth_callback?state=abc")
        assert resp.status_code == 400

    def test_missing_state_returns_400(self, client):
        resp = client.get("/auth_callback?code=xyz")
        assert resp.status_code == 400

    def test_es_unavailable_returns_500(self, client):
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=None):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 500

    def test_user_not_found_returns_404(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 404

    def test_user_missing_fb_app_esid_returns_500(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1"}},  # no fb_app_esid
                ]
            }
        }
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 500

    def test_app_not_found_returns_404(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1", "fb_app_esid": "app123"}},
                ]
            }
        }
        mock_es.get.side_effect = Exception("not found")
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 404

    def test_missing_secret_returns_400(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1", "fb_app_esid": "app123"}},
                ]
            }
        }
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_secret": None}}
        with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 400

    def test_missing_client_id_returns_400(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1", "fb_app_esid": "app123"}},
                ]
            }
        }
        mock_es.get.return_value = {"_source": {"fb_app_id": None, "fb_app_secret": "enc"}}
        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.decrypt", return_value="secret"),
        ):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 400

    def test_no_access_token_returns_400(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1", "fb_app_esid": "app123"}},
                ]
            }
        }
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_secret": "enc"}}
        token_resp = mock.MagicMock()
        token_resp.json.return_value = {"error": "invalid code"}

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.decrypt", return_value="secret"),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
            mock.patch(f"{_MOD}.requests.get", return_value=token_resp),
        ):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 400

    def test_success_returns_self_closing_popup(self, client):
        # on success the OAuth callback returns an HTML page that
        # closes the popup. The wizard tab polls the user-status endpoint
        # to know when the token has landed; it does not depend on the
        # popup redirecting anywhere.
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1", "fb_app_esid": "app123"}},
                ]
            }
        }
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_secret": "enc"}}
        token_resp = mock.MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}
        debug_resp = mock.MagicMock()
        debug_resp.json.return_value = {"data": {"expires_at": 9999, "user_id": "fbuser1"}}

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.decrypt", return_value="secret"),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
            mock.patch(f"{_MOD}.encrypt", return_value="encrypted_tok"),
            mock.patch(f"{_MOD}.requests.get", side_effect=[token_resp, debug_resp]),
        ):
            resp = client.get("/auth_callback?code=xyz&state=abc", follow_redirects=False)
        assert resp.status_code == 200
        assert resp.headers.get("Content-Type", "").startswith("text/html")
        assert b"window.close()" in resp.data
        assert b"Authorized" in resp.data
        # ES calls: 1 search (user) + 1 get (app) + 2 index (token save + debug-info save)
        assert mock_es.index.call_count == 2

    def test_debug_token_error_returns_400(self, client):
        mock_es = mock.MagicMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "u1", "_source": {"fb_app_user_title": "User1", "fb_app_esid": "app123"}},
                ]
            }
        }
        mock_es.get.return_value = {"_source": {"fb_app_id": "111", "fb_app_secret": "enc"}}
        token_resp = mock.MagicMock()
        token_resp.json.return_value = {"access_token": "tok"}
        debug_resp = mock.MagicMock()
        debug_resp.json.return_value = {"error": {"message": "bad token"}}

        with (
            mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_MOD}.decrypt", return_value="secret"),
            mock.patch(f"{_MOD}.get_fboauth_hostname", return_value="oauth.example.com"),
            mock.patch(f"{_MOD}.encrypt", return_value="enc"),
            mock.patch(f"{_MOD}.requests.get", side_effect=[token_resp, debug_resp]),
        ):
            resp = client.get("/auth_callback?code=xyz&state=abc")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestStopGunicorn:

    def test_calls_pkill(self):
        with mock.patch(f"{_MOD}.subprocess.run") as mock_run, mock.patch(f"{_MOD}.time.sleep"):
            oauth_app.stop_gunicorn()
        mock_run.assert_called_once()

    def test_handles_no_process(self):
        import subprocess

        with mock.patch(f"{_MOD}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "pkill")):
            oauth_app.stop_gunicorn()  # should not raise


class TestStartGunicorn:

    def test_calls_gunicorn(self):
        with mock.patch(f"{_MOD}.os.chdir"), mock.patch(f"{_MOD}.subprocess.run") as mock_run:
            oauth_app.start_gunicorn()
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "gunicorn" in cmd

    def test_handles_error(self):
        import subprocess

        with (
            mock.patch(f"{_MOD}.os.chdir"),
            mock.patch(f"{_MOD}.subprocess.run", side_effect=subprocess.CalledProcessError(1, "gunicorn")),
        ):
            oauth_app.start_gunicorn()  # should not raise

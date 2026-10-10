"""Tests for lib.errors.safe_error_response."""

import logging

import pytest

from lib.errors import safe_error_response


@pytest.fixture
def app_ctx(app):
    """jsonify needs a Flask app context to build a Response."""
    with app.test_request_context():
        yield app


class TestSafeErrorResponse:
    """Helper must surface a hard-coded message to the client and route the
    exception detail to the server log only — that is the entire point of
     Phase 2."""

    def test_returns_500_with_default_body_shape(self, app_ctx):
        resp, status = safe_error_response("Generic failure")
        assert status == 500
        assert resp.get_json() == {"success": False, "error": "Generic failure"}

    def test_status_can_be_overridden(self, app_ctx):
        _, status = safe_error_response("Bad request", status=400)
        assert status == 400

    def test_body_shape_overrides_default_success_false(self, app_ctx):
        """Routes that return ``{"ok": False}`` or ``{"exists": False}``
        instead of ``{"success": False}`` must be able to preserve their
        original response shape — the helper does not force a ``success``
        key on them."""
        resp, _ = safe_error_response("Bad", body={"ok": False})
        assert resp.get_json() == {"ok": False, "error": "Bad"}
        assert "success" not in resp.get_json()

    def test_empty_body_yields_clean_payload(self, app_ctx):
        """Routes whose original response shape was just ``{"error": ...}``
        (no other keys) pass ``body={}`` to keep that shape — the helper
        must not silently inject ``success: False``."""
        resp, _ = safe_error_response("Just error", body={})
        assert resp.get_json() == {"error": "Just error"}
        assert "success" not in resp.get_json()

    def test_body_can_carry_extra_keys(self, app_ctx):
        resp, _ = safe_error_response(
            "Read failed",
            body={"exists": False, "content": ""},
        )
        assert resp.get_json() == {
            "exists": False,
            "content": "",
            "error": "Read failed",
        }

    def test_error_key_in_body_is_overwritten_by_client_msg(self, app_ctx):
        """A caller can't accidentally smuggle exception text in via
        ``body["error"]`` — the helper always overwrites that key with the
        hard-coded ``client_msg``."""
        resp, _ = safe_error_response(
            "Generic msg",
            body={"success": False, "error": "leaked exception text"},
        )
        assert resp.get_json()["error"] == "Generic msg"

    def test_response_body_never_contains_exception_text(self, app_ctx):
        """Core regression test: exception detail must not flow into the
        response body even when ``exc=`` is supplied."""
        try:
            raise RuntimeError("secret internal detail")
        except RuntimeError as e:
            resp, _ = safe_error_response("Public message", exc=e)
        assert "secret internal detail" not in resp.get_data(as_text=True)
        assert resp.get_json()["error"] == "Public message"


class TestSafeErrorResponseLogging:
    """Exception detail still has to land in the server log — that's how
    operators debug. Verify the logging side."""

    def test_exception_logged_with_traceback(self, app_ctx, caplog):
        try:
            raise ValueError("internal db failure")
        except ValueError as e:
            with caplog.at_level(logging.ERROR):
                safe_error_response(
                    "User-facing msg",
                    log_detail="route_xyz failed",
                    exc=e,
                )
        assert any("route_xyz failed" in r.message for r in caplog.records)
        assert any(r.exc_info is not None for r in caplog.records)

    def test_log_detail_logged_when_no_exception(self, app_ctx, caplog):
        with caplog.at_level(logging.ERROR):
            safe_error_response("Public msg", log_detail="route_abc failed")
        assert any("route_abc failed" in r.message for r in caplog.records)

    def test_falls_back_to_client_msg_when_log_detail_missing(self, app_ctx, caplog):
        try:
            raise RuntimeError("boom")
        except RuntimeError as e:
            with caplog.at_level(logging.ERROR):
                safe_error_response("Failed to do thing", exc=e)
        assert any("Failed to do thing" in r.message for r in caplog.records)

    def test_no_logging_when_no_exception_and_no_detail(self, app_ctx, caplog):
        with caplog.at_level(logging.ERROR):
            safe_error_response("Public only")
        assert not caplog.records

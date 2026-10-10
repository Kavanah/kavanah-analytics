"""Centralized helper for Flask JSON error responses.

 Phase 2: every route that does
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
echoes the full exception text back to the client. CodeQL flags those as
``py/stack-trace-exposure`` (~127 findings as of 2026-04-28). The exception
detail is genuinely useful for the operator, so it must keep flowing into
the server log — but it should not flow into the response body.

Usage from a route:

    try:
        ...
    except Exception as e:
        from lib.errors import safe_error_response
        return safe_error_response(
            "Failed to delete log file",
            log_detail=f"delete_log_file failed for {filename}",
            exc=e,
        )

Or, when the route returns extra flags alongside ``error``:

    return safe_error_response(
        "Could not check log existence",
        log_detail="check_todays_log failed",
        exc=e,
        extras={"exists": False},
    )

The full traceback always lands in the server log via ``logging.exception``;
the response body never contains exception text.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import jsonify

from lib.user_errors import InvalidRequestError, RequestedItemNotFoundError, UserFacingError  # noqa: F401 — re-exported


def safe_error_response(
    client_msg: str,
    *,
    log_detail: str | None = None,
    exc: BaseException | None = None,
    status: int = 500,
    body: dict[str, Any] | None = None,
):
    """Return a Flask JSON error response that does not leak exception text.

    Args:
        client_msg: Generic, hard-coded message returned in the JSON body
            under the ``error`` key. Must NOT include user input or
            exception text — that is the whole point of this helper.
        log_detail: Operator-facing context written to the server log.
            Concatenated with the exception string by ``logging.exception``
            when ``exc`` is supplied. May include local context like file
            names or asset IDs.
        exc: The exception that triggered the failure. Used so
            ``logging.exception`` records the traceback. If ``None``,
            ``logging.error`` is used with just ``log_detail``.
        status: HTTP status code to return. Defaults to 500.
        body: Full JSON body shape for the response. The route owns the
            top-level keys (``success``, ``ok``, ``found``, ``exists``,
            etc.) so the new response matches the original. ``error`` is
            always overwritten by ``client_msg`` regardless of what's
            passed here. Defaults to ``{"success": False}``.

    Returns:
        A ``(Response, status)`` tuple as produced by ``jsonify``.
    """
    if exc is not None:
        logging.exception(log_detail or client_msg)
    elif log_detail:
        logging.error(log_detail)

    # ``body=None`` -> default {"success": False}; ``body={}`` -> a clean
    # payload with only ``error``; ``body={"foo": ...}`` -> caller-owned shape.
    payload: dict[str, Any] = {"success": False} if body is None else dict(body)
    payload["error"] = client_msg
    return jsonify(payload), status

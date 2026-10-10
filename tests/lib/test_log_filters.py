"""Tests for lib.log_filters.CrlfScrubFilter."""

import logging

import pytest

from lib.log_filters import (
    CrlfScrubFilter,
    install_filter_on_all_handlers,
)


def _make_record(msg: str, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class TestCrlfScrubFilter:
    """Filter must replace CR/LF/NUL with printable escape sequences in
    formatted log messages so an attacker cannot inject fake log lines via
    a logged user-provided value."""

    def test_clean_message_passes_through_unchanged(self):
        f = CrlfScrubFilter()
        record = _make_record("audit: user=alice action=login")
        assert f.filter(record) is True
        assert record.msg == "audit: user=alice action=login"

    def test_lf_is_escaped(self):
        f = CrlfScrubFilter()
        record = _make_record("audit: user=alice\n[FAKE LINE]")
        f.filter(record)
        assert record.msg == "audit: user=alice\\n[FAKE LINE]"
        assert "\n" not in record.msg

    def test_cr_is_escaped(self):
        f = CrlfScrubFilter()
        record = _make_record("audit: user=alice\r[FAKE LINE]")
        f.filter(record)
        assert record.msg == "audit: user=alice\\r[FAKE LINE]"
        assert "\r" not in record.msg

    def test_nul_is_escaped(self):
        f = CrlfScrubFilter()
        record = _make_record("audit: user=alice\x00[FAKE LINE]")
        f.filter(record)
        assert record.msg == "audit: user=alice\\x00[FAKE LINE]"
        assert "\x00" not in record.msg

    def test_args_are_applied_then_cleared(self):
        """If the record uses %-formatting with args, the filter resolves
        them via record.getMessage() then clears record.args so the
        formatter doesn't double-apply the placeholder."""
        f = CrlfScrubFilter()
        record = _make_record("user=%s action=%s", args=("alice\n[FAKE]", "login"))
        f.filter(record)
        assert record.args is None
        assert record.msg == "user=alice\\n[FAKE] action=login"

    def test_filter_never_drops_records(self):
        f = CrlfScrubFilter()
        assert f.filter(_make_record("clean")) is True
        assert f.filter(_make_record("with\nnewline")) is True

    def test_handles_format_failure_gracefully(self):
        """If record.getMessage() itself throws (bad %-format), the filter
        leaves the record alone so the default error path can surface the
        real issue rather than masking it."""
        f = CrlfScrubFilter()
        # %-formatting with mismatched args raises TypeError inside
        # getMessage() — filter must not propagate that.
        record = _make_record("user=%s action=%s", args=("alice",))  # only 1 arg, 2 placeholders
        assert f.filter(record) is True
        # record.msg/args left untouched on getMessage failure
        assert record.msg == "user=%s action=%s"


class TestInstallFilterOnAllHandlers:
    """install_filter_on_all_handlers must attach the scrubber to every
    handler in the process so no log record reaches a handler unfiltered."""

    def test_attaches_to_root_handler(self):
        root = logging.getLogger()
        # Save and restore handlers so the test doesn't leak filters into
        # other tests (pytest fixtures share a root logger).
        original_handlers = list(root.handlers)
        original_filters_per_handler = [list(h.filters) for h in original_handlers]
        try:
            handler = logging.NullHandler()
            root.addHandler(handler)
            install_filter_on_all_handlers()
            assert any(isinstance(f, CrlfScrubFilter) for f in handler.filters)
        finally:
            root.removeHandler(handler)
            for h, filters in zip(original_handlers, original_filters_per_handler):
                h.filters = list(filters)

    def test_attaches_to_named_logger_handler(self):
        """Named loggers with their own handlers (e.g. password_reset's
        audit logger) must be covered too, not just root's handlers."""
        named = logging.getLogger("phase3_test_named_logger")
        handler = logging.NullHandler()
        named.addHandler(handler)
        try:
            install_filter_on_all_handlers()
            assert any(isinstance(f, CrlfScrubFilter) for f in handler.filters)
        finally:
            named.removeHandler(handler)
            handler.filters = []

    def test_idempotent(self):
        """Repeated calls must not stack multiple filters on the same handler."""
        named = logging.getLogger("phase3_test_idempotent")
        handler = logging.NullHandler()
        named.addHandler(handler)
        try:
            install_filter_on_all_handlers()
            install_filter_on_all_handlers()
            install_filter_on_all_handlers()
            crlf_count = sum(1 for f in handler.filters if isinstance(f, CrlfScrubFilter))
            assert crlf_count == 1
        finally:
            named.removeHandler(handler)
            handler.filters = []

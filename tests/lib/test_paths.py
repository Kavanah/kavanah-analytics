"""Tests for the ``lib.paths.safe_path_join`` helper."""

import pytest

from lib.paths import UnsafePathError, safe_path_join


class TestSafePathJoin:
    def test_joins_simple_filename(self):
        assert safe_path_join("/app/data", "file.log") == "/app/data/file.log"

    def test_joins_nested_subdir(self):
        assert safe_path_join("/app/data", "sub", "file.log") == "/app/data/sub/file.log"

    def test_rejects_parent_traversal(self):
        with pytest.raises(UnsafePathError):
            safe_path_join("/app/data", "../etc/passwd")

    def test_rejects_absolute_path_component(self):
        with pytest.raises(UnsafePathError):
            safe_path_join("/app/data", "/etc/passwd")

    def test_unsafepatherror_is_valueerror(self):
        # Callers can catch ValueError and still pick this up — keeps the
        # exception hierarchy compatible with existing handlers.
        with pytest.raises(ValueError):
            safe_path_join("/app/data", "../escape")

"""Unit tests for the release-plan catalog scanner."""

from __future__ import annotations

import textwrap
from pathlib import Path

from asset_management.web.services.release_plan_service import (
    ReleasePlanCatalog,
    scan_release_plans,
)


def _write(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(textwrap.dedent(body))
    return p


def test_should_collect_release_plan_markers_from_python_tests(tmp_path):
    _write(
        tmp_path,
        "test_a.py",
        """
        import pytest

        @pytest.mark.test_id("SEC-CSP-001")
        @pytest.mark.release_plan("plan-25")
        def test_one():
            pass
        """,
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {"plan-25": ["SEC-CSP-001"]}


def test_should_collect_markreleaseplan_calls_from_typescript_specs(tmp_path):
    _write(
        tmp_path,
        "demo.spec.ts",
        """
        test('SEC-CSP-005 — example', async ({ page }, testInfo) => {
            markReleasePlan(testInfo, 'SEC-CSP-005', 'plan-25');
        });
        """,
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {"plan-25": ["SEC-CSP-005"]}


def test_should_collect_multiple_test_ids_under_one_plan(tmp_path):
    _write(
        tmp_path,
        "test_b.py",
        """
        import pytest

        @pytest.mark.test_id("SEC-A")
        @pytest.mark.release_plan("plan-25")
        def test_a(): pass

        @pytest.mark.test_id("SEC-B")
        @pytest.mark.release_plan("plan-25")
        def test_b(): pass
        """,
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {"plan-25": ["SEC-A", "SEC-B"]}


def test_should_collect_test_id_under_multiple_plans(tmp_path):
    _write(
        tmp_path,
        "test_c.py",
        """
        import pytest

        @pytest.mark.test_id("SEC-MULTI")
        @pytest.mark.release_plan("plan-25")
        @pytest.mark.release_plan("plan-26")
        def test_multi(): pass
        """,
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {"plan-25": ["SEC-MULTI"], "plan-26": ["SEC-MULTI"]}


def test_should_skip_pycache_and_node_modules(tmp_path):
    pkg = tmp_path / "node_modules" / "lib"
    pkg.mkdir(parents=True)
    (pkg / "evil.ts").write_text(
        'markReleasePlan(testInfo, "SHOULD-NOT", "plan-25");\n'
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {}


def test_should_treat_unmarked_tests_as_absent(tmp_path):
    _write(
        tmp_path,
        "test_unmarked.py",
        """
        def test_no_markers():
            pass
        """,
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {}


def test_catalog_caches_until_invalidated(tmp_path):
    _write(
        tmp_path,
        "test_cache.py",
        """
        import pytest

        @pytest.mark.test_id("CACHE-1")
        @pytest.mark.release_plan("plan-25")
        def test_one(): pass
        """,
    )
    cat = ReleasePlanCatalog(roots=[str(tmp_path)])
    first = cat.get()
    assert "plan-25" in first

    # Add a new test file; without invalidate(), the catalog should NOT
    # see it (because it's cached).
    _write(
        tmp_path,
        "test_cache2.py",
        """
        import pytest

        @pytest.mark.test_id("CACHE-2")
        @pytest.mark.release_plan("plan-26")
        def test_two(): pass
        """,
    )
    second = cat.get()
    assert second == first  # still cached
    assert "plan-26" not in second

    cat.invalidate()
    third = cat.get()
    assert "plan-26" in third


def test_should_handle_unreadable_files_silently(tmp_path):
    """A binary or otherwise undecodeable file shouldn't blow up the scanner."""
    (tmp_path / "binary.py").write_bytes(b"\x00\x01\x02\xff" * 100)
    _write(
        tmp_path,
        "test_ok.py",
        """
        import pytest

        @pytest.mark.test_id("OK-1")
        @pytest.mark.release_plan("plan-25")
        def test_one(): pass
        """,
    )
    plans = scan_release_plans([str(tmp_path)])
    assert plans == {"plan-25": ["OK-1"]}

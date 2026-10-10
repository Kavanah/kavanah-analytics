"""Unit tests for the fb_legacy_team_health classifier (pure logic only).

The ES sweep is exercised on staging/prod; these tests pin the verdict
rules so a refactor can't silently reclassify a failing team as healthy.
"""

from datetime import date

import pytest

from tools.fb_legacy_team_health import classify_team_health

_TODAY = date(2026, 7, 13)


def _classify(**overrides):
    """classify_team_health with healthy-legacy defaults; override per test."""
    kwargs = {
        "path": "legacy",
        "ads_latest": date(2026, 7, 12),
        "ads_doc_count": 100,
        "pages_latest": date(2026, 7, 12),
        "pages_doc_count": 100,
        "pages_real_recent": 5,
        "today": _TODAY,
        "stale_days": 3,
    }
    kwargs.update(overrides)
    return classify_team_health(**kwargs)


def test_should_return_ok_when_legacy_team_is_fresh_and_real() -> None:
    status, reasons = _classify()
    assert status == "OK"
    assert reasons == []


def test_should_return_migrated_when_wizard_team_is_healthy() -> None:
    status, reasons = _classify(path="wizard")
    assert status == "MIGRATED"
    assert reasons == []


def test_should_flag_page_metrics_dead_when_docs_land_but_all_null() -> None:
    # The FLKVT shape: docs written daily, zero real metrics in the window.
    status, reasons = _classify(pages_real_recent=0)
    assert status == "FAILING"
    assert reasons == ["page_metrics_dead"]


def test_should_flag_ads_stale_when_newest_ad_doc_is_older_than_threshold() -> None:
    status, reasons = _classify(ads_latest=date(2026, 7, 9))
    assert status == "FAILING"
    assert reasons == ["ads_stale"]


def test_should_not_flag_ads_stale_when_exactly_at_threshold() -> None:
    status, reasons = _classify(ads_latest=date(2026, 7, 10))
    assert status == "OK"
    assert reasons == []


def test_should_flag_ads_missing_when_no_ad_docs_exist() -> None:
    status, reasons = _classify(ads_latest=None, ads_doc_count=0)
    assert status == "FAILING"
    assert reasons == ["ads_missing"]


def test_should_flag_pages_stale_and_dead_metrics_together() -> None:
    status, reasons = _classify(pages_latest=date(2026, 5, 25), pages_real_recent=0)
    assert status == "FAILING"
    assert reasons == ["pages_stale", "page_metrics_dead"]


def test_should_flag_pages_missing_when_no_page_docs_exist() -> None:
    status, reasons = _classify(pages_latest=None, pages_doc_count=0, pages_real_recent=0)
    assert status == "FAILING"
    assert reasons == ["pages_missing"]


def test_should_fail_wizard_team_when_its_imports_break() -> None:
    # MIGRATED is reserved for healthy wizard teams — a broken one must
    # surface as FAILING, not hide behind its migration status.
    status, reasons = _classify(path="wizard", ads_latest=date(2026, 6, 1))
    assert status == "FAILING"
    assert reasons == ["ads_stale"]


@pytest.mark.parametrize("missing_latest", [None])
def test_should_treat_docs_with_unparseable_latest_as_stale(missing_latest) -> None:
    # doc_count > 0 but no parseable latest date → stale, not OK.
    status, reasons = _classify(ads_latest=missing_latest)
    assert status == "FAILING"
    assert reasons == ["ads_stale"]


def test_should_ignore_dead_page_metrics_when_ads_only() -> None:
    # Meta v23 killed page metrics platform-wide; ads-only mode must not
    # let that universal failure drown the ads signal.
    status, reasons = _classify(pages_real_recent=0, pages_latest=None, pages_doc_count=0, ads_only=True)
    assert status == "OK"
    assert reasons == []


def test_should_still_flag_stale_ads_when_ads_only() -> None:
    status, reasons = _classify(ads_latest=date(2026, 5, 21), ads_only=True)
    assert status == "FAILING"
    assert reasons == ["ads_stale"]

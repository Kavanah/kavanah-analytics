"""per-tenant opt-in for the age/gender demographics breakdown.

The demographics pull is gated on the workspace's field-catalog
overrides: default (no override / store failure) keeps today's behavior,
an explicit disable skips the extra Meta call for that team's accounts.
"""

from __future__ import annotations

from unittest import mock

import pytest

import import_scripts.facebook.get_insights as gi
import import_scripts.facebook.get_insights_window as giw

_SVC = "asset_management.web.services.field_catalog_service.FieldCatalogService"
_FIELD = "fb_ads.breakdown_demographics"


@pytest.fixture(autouse=True)
def _fresh_caches():
    gi._TEAM_OVERRIDES_CACHE.clear()
    yield
    gi._TEAM_OVERRIDES_CACHE.clear()


def test_should_pull_demographics_when_no_override():
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value={}):
        assert giw._demographics_enabled_for_team("team1") is True


def test_should_skip_demographics_when_disabled():
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value={_FIELD: {"enabled": False}}):
        assert giw._demographics_enabled_for_team("team1") is False


def test_should_pull_demographics_when_reenabled():
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value={_FIELD: {"enabled": True}}):
        assert giw._demographics_enabled_for_team("team1") is True


def test_should_pull_demographics_when_store_fails():
    # Fail-soft: any picker failure keeps today's behavior (pull).
    with mock.patch(f"{_SVC}.get_overrides_for_team", side_effect=Exception("es down")):
        assert giw._demographics_enabled_for_team("team1") is True
    assert giw._demographics_enabled_for_team("unknown") is True
    assert giw._demographics_enabled_for_team(None) is True


def test_should_not_affect_request_lists():
    # The breakdown entry has no api_field — the fidelity contract holds.
    from migration.catalog_loader import clear_cache, request_fields, request_fields_for_tenant

    clear_cache()
    overrides = {_FIELD: {"enabled": False}}
    for dataset in ("daily", "window"):
        assert request_fields_for_tenant("fb_ads", dataset, overrides) == request_fields("fb_ads", dataset)


def test_should_expose_breakdown_toggle_in_picker():
    from asset_management.web.services.field_catalog_service import FieldCatalogService

    with mock.patch(
        "asset_management.web.services.field_catalog_service.FieldCatalogService.get_overrides",
        return_value={},
    ):
        rows = FieldCatalogService.picker_fields("ws1")
    row = next(r for r in rows if r["field_id"] == _FIELD)
    assert row["kind"] == "breakdown"
    assert row["enabled"] is True  # default on — behavior-preserving
    assert row["core"] is False


def test_should_persist_breakdown_toggle_via_set_field():
    from asset_management.web.services.field_catalog_service import FieldCatalogService

    es = mock.MagicMock()
    with mock.patch(
        "asset_management.web.services.field_catalog_service.get_elasticsearch_client",
        return_value=es,
    ):
        override = FieldCatalogService.set_field("ws1", "fb_ads", _FIELD, False, "john")
    assert override["enabled"] is False
    override_enable = None
    with mock.patch(
        "asset_management.web.services.field_catalog_service.get_elasticsearch_client",
        return_value=es,
    ):
        override_enable = FieldCatalogService.set_field("ws1", "fb_ads", _FIELD, True, "john")
    assert override_enable["enabled"] is True

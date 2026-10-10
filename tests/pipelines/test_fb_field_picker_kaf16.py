"""FB importers honor the per-tenant field picker.

The daily importer must request exactly the union of (always-on base fields +
enabled optional fields) per tenant, fall back to today's global list on any
picker failure, and drop a tenant's disabled fields from the docs it stores
(action metrics ride the always-requested ``actions`` parents, so the doc
filter is the only place they can be excluded).
"""

from __future__ import annotations

from unittest import mock

import pytest

import import_scripts.facebook.get_insights as gi
import import_scripts.facebook.get_insights_window as giw

_SVC = "asset_management.web.services.field_catalog_service.FieldCatalogService"


@pytest.fixture(autouse=True)
def _fresh_caches():
    gi._TEAM_OVERRIDES_CACHE.clear()
    gi._TEAM_DISABLED_CACHE.clear()
    yield
    gi._TEAM_OVERRIDES_CACHE.clear()
    gi._TEAM_DISABLED_CACHE.clear()


# ---- request-list union ----


def test_should_request_default_list_when_team_has_no_overrides():
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value={}):
        assert gi._daily_fields_for_teams(["team1"]) == gi._DAILY_INSIGHT_FIELDS


def test_should_request_default_list_when_no_teams_resolvable():
    assert gi._daily_fields_for_teams([]) == gi._DAILY_INSIGHT_FIELDS
    assert gi._daily_fields_for_teams(["unknown", None]) == gi._DAILY_INSIGHT_FIELDS


def test_should_drop_disabled_field_from_tenant_request():
    overrides = {"fb_ads.cost_per_inline_link_click": {"enabled": False}}
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value=overrides):
        fields = gi._daily_fields_for_teams(["team1"])
    assert "cost_per_inline_link_click" not in fields
    assert set(gi._DAILY_INSIGHT_FIELDS) - set(fields) == {"cost_per_inline_link_click"}


def test_should_request_union_across_teams_sharing_an_account():
    # team1 disables cpc, team2 disables cpm — the shared-account request keeps
    # BOTH (each tenant's disable only binds where the other doesn't enable it).
    per_team = {
        "team1": {"fb_ads.cpc": {"enabled": False}},
        "team2": {"fb_ads.cpm": {"enabled": False}},
    }
    with mock.patch(f"{_SVC}.get_overrides_for_team", side_effect=lambda t, source="fb_ads": per_team[t]):
        fields = gi._daily_fields_for_teams(["team1", "team2"])
    assert "cpc" in fields and "cpm" in fields
    # ...but a field BOTH disable leaves the union. (Fresh per-run cache — the
    # overrides cache is per importer run.)
    gi._TEAM_OVERRIDES_CACHE.clear()
    both = {
        "team1": {"fb_ads.cpc": {"enabled": False}},
        "team2": {"fb_ads.cpc": {"enabled": False}},
    }
    with mock.patch(f"{_SVC}.get_overrides_for_team", side_effect=lambda t, source="fb_ads": both[t]):
        fields = gi._daily_fields_for_teams(["team1", "team2"])
    assert "cpc" not in fields


def test_should_fall_back_to_default_list_when_picker_lookup_raises():
    with mock.patch(f"{_SVC}.get_overrides_for_team", side_effect=Exception("es down")):
        assert gi._daily_fields_for_teams(["team1"]) == gi._DAILY_INSIGHT_FIELDS


def test_should_pass_tenant_fields_to_meta_request():
    captured = {}

    class _FakeAccount:
        def __init__(self, _id):
            pass

        def get_insights(self, fields, params):
            captured["fields"] = fields
            return []

    with mock.patch.object(gi, "AdAccount", _FakeAccount):
        gi._fetch_account_ad_insights_chunk("act_1", "2026-07-01", "2026-07-01", fields=["spend", "reach"])
    assert captured["fields"] == ["ad_id", "spend", "reach"]


def test_should_default_meta_request_to_global_list_when_fields_omitted():
    captured = {}

    class _FakeAccount:
        def __init__(self, _id):
            pass

        def get_insights(self, fields, params):
            captured["fields"] = fields
            return []

    with mock.patch.object(gi, "AdAccount", _FakeAccount):
        gi._fetch_account_ad_insights_chunk("act_1", "2026-07-01", "2026-07-01")
    assert captured["fields"] == ["ad_id", *gi._DAILY_INSIGHT_FIELDS]


# ---- doc-level filter ----


def test_should_drop_disabled_action_metric_from_stored_doc():
    overrides = {"fb_ads.actions_link_click": {"enabled": False}}
    doc = {"insight_actions_link_click": 5.0, "insight_spend": 1.2, "team_esid": "team1"}
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value=overrides):
        for dropped in gi._disabled_fields_for_team("team1"):
            doc.pop(dropped, None)
    assert "insight_actions_link_click" not in doc
    assert doc["insight_spend"] == 1.2


def test_should_keep_doc_intact_when_team_unknown():
    assert gi._disabled_fields_for_team("unknown") == set()
    assert gi._disabled_fields_for_team(None) == set()


# ---- window importer ----


def test_should_use_tenant_window_list_when_team_has_overrides():
    overrides = {"fb_ads.estimated_ad_recallers": {"enabled": False}}
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value=overrides):
        fields = giw._window_fields_for_team("team1")
    assert "estimated_ad_recallers" not in fields
    assert set(giw._INSIGHT_FIELDS) - set(fields) == {"estimated_ad_recallers"}


def test_should_use_global_window_list_when_no_overrides():
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value={}):
        assert giw._window_fields_for_team("team1") is giw._INSIGHT_FIELDS
    assert giw._window_fields_for_team(None) is giw._INSIGHT_FIELDS


def test_should_drop_disabled_fields_from_window_doc():
    overrides = {"fb_ads.estimated_ad_recallers": {"enabled": False}}
    doc = {"insight_estimated_ad_recallers": 100, "insight_reach": 5, "team_esid": "team1"}
    with mock.patch(f"{_SVC}.get_overrides_for_team", return_value=overrides):
        giw._drop_disabled_fields(doc, "team1")
    assert "insight_estimated_ad_recallers" not in doc
    assert doc["insight_reach"] == 5

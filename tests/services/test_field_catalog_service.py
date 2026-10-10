"""FieldCatalogService: per-workspace field-picker selection store.

Fail-soft reads (any store failure => {} => catalog default set — imports must
never break on the picker), owner-write validation (core / deprecated /
unmapped / non-picker rejections), and the team -> workspace resolution the
importers use.
"""

from __future__ import annotations

from unittest import mock

import pytest

from asset_management.web.services.field_catalog_service import (
    FieldCatalogError,
    FieldCatalogService,
    FieldNotToggleableError,
)

_MOD = "asset_management.web.services.field_catalog_service"


class _FakeES:
    def __init__(self, docs=None, get_error=None):
        # docs: {(index, id): _source}
        self.docs = docs or {}
        self.get_error = get_error
        self.updates = []

    def get(self, index, id):
        if self.get_error:
            raise self.get_error
        key = (index, id)
        if key not in self.docs:
            raise Exception("404 not_found")
        return {"_source": self.docs[key]}

    def update(self, index, id, doc, doc_as_upsert, refresh):
        self.updates.append({"index": index, "id": id, "doc": doc})


def _with_es(es):
    return mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=es)


# ---- get_overrides: fail-soft ----


def test_should_return_empty_overrides_when_doc_missing():
    with _with_es(_FakeES()):
        assert FieldCatalogService.get_overrides("ws1") == {}


def test_should_return_empty_overrides_when_es_unavailable():
    with mock.patch(f"{_MOD}.get_elasticsearch_client", return_value=None):
        assert FieldCatalogService.get_overrides("ws1") == {}


def test_should_return_empty_overrides_when_read_raises():
    with _with_es(_FakeES(get_error=Exception("boom connection reset"))):
        assert FieldCatalogService.get_overrides("ws1") == {}


def test_should_return_fields_map_when_doc_exists():
    fields = {"fb_ads.cpc": {"enabled": False, "changed_at": "x", "changed_by": "u"}}
    es = _FakeES(docs={("de_asset_field_catalog", "ws1__fb_ads"): {"fields": fields}})
    with _with_es(es):
        assert FieldCatalogService.get_overrides("ws1") == fields


# ---- team -> workspace resolution ----


def test_should_resolve_overrides_via_team_workspace_stamp():
    fields = {"fb_ads.cpc": {"enabled": False}}
    es = _FakeES(
        docs={
            ("de_asset_teams", "team1"): {"workspace_esid": "ws1"},
            ("de_asset_field_catalog", "ws1__fb_ads"): {"fields": fields},
        }
    )
    with _with_es(es):
        assert FieldCatalogService.get_overrides_for_team("team1") == fields


def test_should_resolve_workspace_via_org_when_team_has_no_stamp():
    fields = {"fb_ads.cpm": {"enabled": False}}
    es = _FakeES(
        docs={
            ("de_asset_teams", "team1"): {"organization_esid": "org1"},
            ("de_asset_organization", "org1"): {"workspace_esid": "ws2"},
            ("de_asset_field_catalog", "ws2__fb_ads"): {"fields": fields},
        }
    )
    with _with_es(es):
        assert FieldCatalogService.get_overrides_for_team("team1") == fields


def test_should_return_empty_when_team_unresolvable():
    with _with_es(_FakeES()):
        assert FieldCatalogService.get_overrides_for_team("ghost-team") == {}


# ---- set_field validation ----


def test_should_persist_disable_when_field_is_toggleable():
    es = _FakeES()
    with _with_es(es):
        override = FieldCatalogService.set_field("ws1", "fb_ads", "fb_ads.cost_per_inline_link_click", False, "john")
    assert override["enabled"] is False
    assert override["changed_by"] == "john"
    assert len(es.updates) == 1
    update = es.updates[0]
    assert update["id"] == "ws1__fb_ads"
    assert update["doc"]["fields"]["fb_ads.cost_per_inline_link_click"]["enabled"] is False


def test_should_reject_disable_when_field_is_core():
    with _with_es(_FakeES()):
        with pytest.raises(FieldNotToggleableError):
            FieldCatalogService.set_field("ws1", "fb_ads", "fb_ads.spend", False, "john")


def test_should_reject_toggle_when_field_outside_picker_families():
    with _with_es(_FakeES()):
        with pytest.raises(FieldNotToggleableError):
            FieldCatalogService.set_field("ws1", "fb_ads", "fb_ads.team_title", False, "john")


def test_should_reject_toggle_when_field_unknown():
    with _with_es(_FakeES()):
        with pytest.raises(FieldNotToggleableError):
            FieldCatalogService.set_field("ws1", "fb_ads", "fb_ads.nope", True, "john")


def test_should_reject_enable_when_field_unmapped():
    # conversions is a list-valued response field with DYNAMIC action types —
    # still unmapped after enabling must be refused server-side.
    with _with_es(_FakeES()):
        with pytest.raises(FieldNotToggleableError):
            FieldCatalogService.set_field("ws1", "fb_ads", "fb_ads.conversions", True, "john")


def test_should_allow_disable_when_field_unmapped():
    es = _FakeES()
    with _with_es(es):
        override = FieldCatalogService.set_field("ws1", "fb_ads", "fb_ads.conversions", False, "john")
    assert override["enabled"] is False


# ---- picker listing ----


def test_should_list_only_picker_families_with_state():
    es = _FakeES(
        docs={
            ("de_asset_field_catalog", "ws1__fb_ads"): {
                "fields": {"fb_ads.cost_per_inline_link_click": {"enabled": False, "changed_at": "t"}}
            }
        }
    )
    with _with_es(es):
        rows = FieldCatalogService.picker_fields("ws1")
    by_id = {r["field_id"]: r for r in rows}
    # metric + action_metric + breakdown
    assert set(r["kind"] for r in rows) == {"metric", "action_metric", "breakdown"}
    assert by_id["fb_ads.spend"]["core"] is True
    assert by_id["fb_ads.spend"]["enabled"] is True
    assert by_id["fb_ads.cost_per_inline_link_click"]["enabled"] is False
    assert by_id["fb_ads.cost_per_inline_link_click"]["changed_at"] == "t"
    assert by_id["fb_ads.outbound_clicks"]["mapped"] is True
    assert by_id["fb_ads.conversions"]["mapped"] is False
    # action_metric children carry requested: false in the catalog but ARE
    # collected via the actions parent — the picker shows them enabled.
    assert by_id["fb_ads.actions_link_click"]["kind"] == "action_metric"
    assert by_id["fb_ads.actions_link_click"]["enabled"] is True


# --- source toggles — per-team only ----


_FC = "de_asset_field_catalog"


def test_should_enable_source_when_no_toggle_doc_exists():
    with _with_es(_FakeES()):
        assert FieldCatalogService.source_enabled_for_team("team1", "fb_pages") is True


def test_should_enable_source_when_store_read_raises():
    with _with_es(_FakeES(get_error=Exception("boom"))):
        assert FieldCatalogService.source_enabled_for_team("team1", "instagram") is True


def test_should_honor_explicit_team_disable():
    docs = {(_FC, "team__team1__fb_pages"): {"source_enabled": {"enabled": False}}}
    with _with_es(_FakeES(docs=docs)):
        assert FieldCatalogService.source_enabled_for_team("team1", "fb_pages") is False


def test_should_honor_explicit_team_enable():
    docs = {(_FC, "team__team1__instagram"): {"source_enabled": {"enabled": True}}}
    with _with_es(_FakeES(docs=docs)):
        assert FieldCatalogService.source_enabled_for_team("team1", "instagram") is True


def test_should_enable_source_when_team_missing():
    with _with_es(_FakeES()):
        assert FieldCatalogService.source_enabled_for_team("", "fb_pages") is True
        assert FieldCatalogService.source_enabled_for_team("team1", "not_a_source") is True


def test_should_persist_team_toggle():
    es = _FakeES(docs={("de_asset_teams", "team1"): {"workspace_esid": "ws1"}})
    with _with_es(es):
        toggle = FieldCatalogService.set_source_enabled("team1", "instagram", False, "john")
    assert toggle["enabled"] is False
    assert es.updates[0]["id"] == "team__team1__instagram"
    assert es.updates[0]["doc"]["scope"] == "team"
    assert es.updates[0]["doc"]["workspace_esid"] == "ws1"
    assert es.updates[0]["doc"]["source_enabled"]["enabled"] is False


def test_should_reject_team_toggle_outside_allowed_workspaces():
    es = _FakeES(docs={("de_asset_teams", "teamX"): {"workspace_esid": "ws-other"}})
    with _with_es(es):
        with pytest.raises(FieldCatalogError):
            FieldCatalogService.set_source_enabled("teamX", "fb_pages", False, "john", allowed_workspaces=["ws1"])
    assert es.updates == []


def test_should_allow_team_toggle_for_superuser_see_all():
    # allowed_workspaces=None (see-all) skips the workspace bound.
    es = _FakeES(docs={("de_asset_teams", "teamX"): {"workspace_esid": "ws-other"}})
    with _with_es(es):
        toggle = FieldCatalogService.set_source_enabled("teamX", "fb_pages", False, "root", allowed_workspaces=None)
    assert toggle["enabled"] is False
    assert es.updates[0]["id"] == "team__teamX__fb_pages"


def test_should_reject_toggle_when_source_unknown():
    with _with_es(_FakeES()):
        with pytest.raises(FieldCatalogError):
            FieldCatalogService.set_source_enabled("team1", "echo", False, "john")


def test_should_report_team_states_for_ui():
    docs = {(_FC, "team__team1__instagram"): {"source_enabled": {"enabled": False}}}
    with _with_es(_FakeES(docs=docs)):
        states = FieldCatalogService.get_team_source_states("team1")
    assert states == {"fb_ads": True, "fb_pages": True, "instagram": False}

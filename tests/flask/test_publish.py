"""
Publish Impact Reports (backend).

Covers the token lifecycle (publish / regenerate / unpublish / get), save-time
preservation of the publish state, the public viewer route, and the forced
org/team scoping on the public query endpoint.
"""

from unittest import mock

import pytest

from asset_management.web.blueprints.dashboard_builder.routes import (
    _allowed_data_views_for_publish,
    _forced_publish_filters,
    _is_publish_eligible,
    _strip_org_team_filters,
)

_R = "asset_management.web.blueprints.dashboard_builder.routes"
_PR = "asset_management.web.blueprints.public_report.routes"


def _eligible_source(**overrides):
    """Return a dashboard source dict that is eligible for publishing."""
    src = {
        "dashboard_id": "dash-1",
        "dashboard_type": "echarts",
        "title": "Impact",
        "menu": "Reports",
        "allowed_org_esids": ["org-1"],
        "allowed_team_esids": [],
        "echarts_config": {
            "charts": [
                {
                    "chart_id": "c1",
                    "chart_type": "report",
                    "data_view": "*fb_insights",
                    "org_name": "Acme",
                }
            ]
        },
    }
    src.update(overrides)
    return src


# ===========================================================================
# Unit tests — helpers
# ===========================================================================


class TestIsPublishEligible:
    def test_eligible_impact_report(self):
        ok, reason = _is_publish_eligible(_eligible_source())
        assert ok is True
        assert reason == ""

    def test_accepts_non_reports_menu(self):
        # the prior ``menu == "Reports"`` requirement was
        # dropped — workspace admins configure their own menu list per
        # workspace, and any menu may contain publishable Impact Report
        # dashboards. Visibility-mode and allowed_*_esids continue to
        # gate publish.
        ok, reason = _is_publish_eligible(_eligible_source(menu="My Dashboards"))
        assert ok is True
        assert reason == ""

    def test_rejects_non_impact_chart_type(self):
        src = _eligible_source()
        src["echarts_config"]["charts"][0]["chart_type"] = "bar"
        ok, reason = _is_publish_eligible(src)
        assert ok is False
        assert "Impact Report" in reason

    def test_rejects_mixed_chart_types(self):
        src = _eligible_source()
        src["echarts_config"]["charts"].append({"chart_id": "c2", "chart_type": "pie"})
        ok, _ = _is_publish_eligible(src)
        assert ok is False

    def test_rejects_empty_charts(self):
        src = _eligible_source()
        src["echarts_config"]["charts"] = []
        ok, _ = _is_publish_eligible(src)
        assert ok is False

    def test_rejects_when_no_scope_set(self):
        src = _eligible_source(allowed_org_esids=[], allowed_team_esids=[])
        ok, reason = _is_publish_eligible(src)
        assert ok is False
        assert "organization or team" in reason

    def test_rejects_leaflet_map(self):
        src = _eligible_source(dashboard_type="leaflet_map")
        ok, _ = _is_publish_eligible(src)
        assert ok is False

    def test_accepts_google_report(self):
        src = _eligible_source()
        src["echarts_config"]["charts"][0]["chart_type"] = "google_report"
        ok, _ = _is_publish_eligible(src)
        assert ok is True

    def test_accepts_combined_report(self):
        src = _eligible_source()
        src["echarts_config"]["charts"][0]["chart_type"] = "combined_report"
        ok, _ = _is_publish_eligible(src)
        assert ok is True

    def test_accepts_charts_as_dict(self):
        src = _eligible_source()
        src["echarts_config"]["charts"] = {"c1": src["echarts_config"]["charts"][0]}
        ok, _ = _is_publish_eligible(src)
        assert ok is True


class TestAllowedDataViews:
    def test_facebook_report_whitelist(self):
        src = _eligible_source()
        allowed = _allowed_data_views_for_publish(src)
        assert "*fb_insights" in allowed
        assert "*echo_conversations" in allowed

    def test_google_report_whitelist(self):
        src = _eligible_source()
        src["echarts_config"]["charts"][0]["chart_type"] = "google_report"
        src["echarts_config"]["charts"][0]["data_view"] = "*google_ads"
        allowed = _allowed_data_views_for_publish(src)
        assert "*google_ads" in allowed

    def test_combined_report_whitelist(self):
        src = _eligible_source()
        src["echarts_config"]["charts"][0]["chart_type"] = "combined_report"
        allowed = _allowed_data_views_for_publish(src)
        assert "*fb_insights" in allowed
        assert "*echo_conversations" in allowed
        assert "*google_ads" in allowed


class TestForcedPublishFilters:
    def test_single_team_forces_match_phrase(self):
        with mock.patch(f"{_R}.get_document_by_id") as mock_doc:
            mock_doc.return_value = {"team_title": "Team Alpha"}
            src = _eligible_source(allowed_team_esids=["team-1"], allowed_org_esids=[])
            filters = _forced_publish_filters(src)
        assert filters == [
            {"field": "team_title", "op": "match_phrase", "value": "Team Alpha"}
        ]

    def test_multi_team_uses_match_phrase_any(self):
        with mock.patch(f"{_R}.get_document_by_id") as mock_doc:
            mock_doc.side_effect = lambda idx, esid: {"team_title": f"Team {esid}"}
            src = _eligible_source(allowed_team_esids=["a", "b"], allowed_org_esids=[])
            filters = _forced_publish_filters(src)
        assert filters[0]["op"] == "match_phrase_any"
        assert filters[0]["field"] == "team_title"
        assert set(filters[0]["value"]) == {"Team a", "Team b"}

    def test_org_used_when_no_teams(self):
        with mock.patch(f"{_R}.get_document_by_id") as mock_doc:
            mock_doc.return_value = {"organization_title": "Acme"}
            src = _eligible_source(allowed_org_esids=["org-1"], allowed_team_esids=[])
            filters = _forced_publish_filters(src)
        assert filters == [
            {"field": "organization_title", "op": "match_phrase", "value": "Acme"}
        ]

    def test_teams_preferred_over_org(self):
        def _lookup(idx, esid):
            if idx == "de_asset_teams":
                return {"team_title": "Alpha"}
            return {"organization_title": "Acme"}

        with mock.patch(f"{_R}.get_document_by_id", side_effect=_lookup):
            src = _eligible_source(allowed_org_esids=["org-1"], allowed_team_esids=["team-1"])
            filters = _forced_publish_filters(src)
        # Team filter is the tighter scope — org filter should not appear.
        assert len(filters) == 1
        assert filters[0]["field"] == "team_title"


class TestStripOrgTeamFilters:
    def test_strips_org_filter(self):
        cfg = {"filters": [{"field": "organization_title", "op": "match_phrase", "value": "Evil"}]}
        out = _strip_org_team_filters(cfg)
        assert out["filters"] == []

    def test_strips_team_filter(self):
        cfg = {"filters": [{"field": "team_title", "op": "match_phrase", "value": "Evil"}]}
        out = _strip_org_team_filters(cfg)
        assert out["filters"] == []

    def test_preserves_other_filters(self):
        cfg = {
            "filters": [
                {"field": "kibana_date", "op": "gt", "value": "2025-01-01"},
                {"field": "team_title", "op": "match_phrase", "value": "Evil"},
            ]
        }
        out = _strip_org_team_filters(cfg)
        assert out["filters"] == [{"field": "kibana_date", "op": "gt", "value": "2025-01-01"}]


# ===========================================================================
# Publish API — authenticated endpoints
# ===========================================================================


class TestPublishEndpoint:
    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_publish_mints_token(self, mock_get, mock_es, auth_client):
        mock_get.return_value = _eligible_source()
        mock_es.return_value = mock.MagicMock()

        resp = auth_client.post("/dashboard-builder/api/dashboards/dash-1/publish")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["published"] is True
        assert data["token"]
        assert len(data["token"]) >= 40  # 32 bytes base64-url ≈ 43 chars
        assert data["path"] == f"/public/report/{data['token']}"

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_publish_is_idempotent(self, mock_get, mock_es, auth_client):
        mock_get.return_value = _eligible_source(
            publish_token="existing-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            publish_enabled=True,
            published_at="2026-04-01T00:00:00Z",
        )
        mock_es.return_value = mock.MagicMock()

        resp = auth_client.post("/dashboard-builder/api/dashboards/dash-1/publish")
        assert resp.status_code == 200
        assert resp.get_json()["token"] == "existing-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        # Should NOT have called es.update — the record is already published.
        mock_es.return_value.update.assert_not_called()

    @mock.patch(f"{_R}.get_document_by_id")
    def test_publish_rejects_ineligible(self, mock_get, auth_client):
        # dropped the menu-must-be-Reports rule. Use a still-live
        # ineligibility — a non-Impact-Report chart type — so this test
        # still proves the publish endpoint rejects ineligible dashboards.
        src = _eligible_source()
        src["echarts_config"]["charts"][0]["chart_type"] = "bar"
        mock_get.return_value = src
        resp = auth_client.post("/dashboard-builder/api/dashboards/dash-1/publish")
        assert resp.status_code == 400

    @mock.patch(f"{_R}.get_document_by_id")
    def test_publish_404_for_missing(self, mock_get, auth_client):
        mock_get.return_value = None
        resp = auth_client.post("/dashboard-builder/api/dashboards/missing/publish")
        assert resp.status_code == 404

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_regenerate_changes_token(self, mock_get, mock_es, auth_client):
        mock_get.return_value = _eligible_source(
            publish_token="old-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            publish_enabled=True,
        )
        mock_es.return_value = mock.MagicMock()

        resp = auth_client.post("/dashboard-builder/api/dashboards/dash-1/publish/regenerate")
        assert resp.status_code == 200
        new_token = resp.get_json()["token"]
        assert new_token
        assert new_token != "old-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    @mock.patch(f"{_R}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    def test_unpublish_clears_token(self, mock_get, mock_es, auth_client):
        mock_get.return_value = _eligible_source(
            publish_token="some-token-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            publish_enabled=True,
        )
        es_mock = mock.MagicMock()
        mock_es.return_value = es_mock

        resp = auth_client.delete("/dashboard-builder/api/dashboards/dash-1/publish")
        assert resp.status_code == 200
        assert resp.get_json()["published"] is False
        call_kwargs = es_mock.update.call_args.kwargs
        # the legacy unscoped entry now lives inside publishes[]
        # for new docs; for a legacy doc with the flat fields, DELETE
        # writes publishes=[] and nulls the legacy fields. The post-DELETE
        # invariant is "no active token for any scope", which the legacy
        # publish_enabled assertion (False) and the new publish_enabled
        # value (None) both satisfy.
        doc = call_kwargs["doc"]
        assert doc.get("publishes") == []
        assert doc["publish_token"] is None
        assert not doc["publish_enabled"]

    @mock.patch(f"{_R}.get_document_by_id")
    def test_get_state_unpublished(self, mock_get, auth_client):
        mock_get.return_value = _eligible_source()
        resp = auth_client.get("/dashboard-builder/api/dashboards/dash-1/publish")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["published"] is False
        assert data["token"] is None

    @mock.patch(f"{_R}.get_document_by_id")
    def test_get_state_published(self, mock_get, auth_client):
        mock_get.return_value = _eligible_source(
            publish_token="tok-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            publish_enabled=True,
            published_at="2026-04-01T00:00:00Z",
        )
        resp = auth_client.get("/dashboard-builder/api/dashboards/dash-1/publish")
        data = resp.get_json()
        assert data["published"] is True
        assert data["token"] == "tok-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        assert data["path"] == "/public/report/tok-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

    @mock.patch(f"{_R}.get_document_by_id")
    def test_get_state_includes_eligibility_for_ui(self, mock_get, auth_client):
        """UI gates the Publish button on ``eligible`` — endpoint must return it."""
        mock_get.return_value = _eligible_source()
        resp = auth_client.get("/dashboard-builder/api/dashboards/dash-1/publish")
        data = resp.get_json()
        assert data["eligible"] is True
        assert data["eligibility_reason"] is None

    @mock.patch(f"{_R}.get_document_by_id")
    def test_get_state_ineligible_includes_reason(self, mock_get, auth_client):
        # dropped the menu-must-be-Reports rule. Source-pin the
        # eligibility-reason surface on a still-live ineligibility — a
        # missing org/team scope.
        src = _eligible_source(allowed_org_esids=[], allowed_team_esids=[])
        mock_get.return_value = src
        resp = auth_client.get("/dashboard-builder/api/dashboards/dash-1/publish")
        data = resp.get_json()
        assert data["eligible"] is False
        assert "organization or team" in data["eligibility_reason"]


class TestSavePreservesPublish:
    """Regression: POST /api/dashboards must not wipe publish_* on re-save."""

    # overwrite now passes through the per-dashboard mutate-scope gate.
    # This test exercises publish-field preservation, not authz — treat the test
    # admin as a workspace admin so the gate passes.
    # the admin bypass is per-workspace; these fixtures carry no
    # workspace_esid, so treat the admin as admin of the dashboard's workspace.
    @mock.patch(f"{_R}._current_user_admins_dashboard_workspace", new=lambda _source: True)
    @mock.patch(f"{_R}._current_user_is_workspace_admin", return_value=True)
    @mock.patch(f"{_R}.get_elasticsearch_client")
    def test_save_preserves_publish_fields(self, mock_es, _mock_ws_admin, auth_client):
        es_mock = mock.MagicMock()
        es_mock.get.return_value = {
            "_source": {
                "created_at": "2026-01-01T00:00:00Z",
                "menu": "Reports",
                "publish_token": "keep-me-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                "publish_enabled": True,
                "published_at": "2026-04-01T00:00:00Z",
            }
        }
        mock_es.return_value = es_mock

        resp = auth_client.post(
            "/dashboard-builder/api/dashboards",
            json={"id": "dash-1", "title": "Updated"},
        )
        assert resp.status_code == 200
        doc = es_mock.index.call_args.kwargs["document"]
        assert doc["publish_token"] == "keep-me-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        assert doc["publish_enabled"] is True
        assert doc["published_at"] == "2026-04-01T00:00:00Z"


# ===========================================================================
# Public endpoints — no auth required
# ===========================================================================


class TestPublicReportView:
    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_valid_token_renders_viewer(self, mock_lookup, client):
        mock_lookup.return_value = ("dash-1", _eligible_source(title="My Impact"))
        resp = client.get("/public/report/validtoken123")
        assert resp.status_code == 200
        assert b"My Impact" in resp.data

    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_unknown_token_404s(self, mock_lookup, client):
        mock_lookup.return_value = (None, None)
        resp = client.get("/public/report/not-a-real-token")
        assert resp.status_code == 404

    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_viewer_accessible_without_session(self, mock_lookup, client):
        """Public viewer must NOT require login — test with fresh unauthenticated client."""
        mock_lookup.return_value = ("dash-1", _eligible_source())
        resp = client.get("/public/report/sometoken")
        assert resp.status_code == 200
        assert b"login" not in resp.data.lower()[:200]  # not a login redirect


class TestPublicDashboardFetch:
    """GET /public/api/dashboards/<token> — viewer fetches its dashboard config via this."""

    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_valid_token_returns_dashboard_state(self, mock_lookup, client):
        mock_lookup.return_value = ("dash-1", _eligible_source(title="My Report"))
        resp = client.get("/public/api/dashboards/validtoken")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "dash-1"
        assert data["title"] == "My Report"
        assert "charts" in data

    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_unknown_token_returns_404(self, mock_lookup, client):
        mock_lookup.return_value = (None, None)
        resp = client.get("/public/api/dashboards/bad")
        assert resp.status_code == 404

    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_strips_internal_fields(self, mock_lookup, client):
        """Public viewer should not see visibility_mode or ESID lists."""
        mock_lookup.return_value = ("dash-1", _eligible_source())
        data = client.get("/public/api/dashboards/tok").get_json()
        assert "visibility_mode" not in data
        assert "allowed_org_esids" not in data
        assert "allowed_team_esids" not in data


class TestPublicReportQuery:
    @mock.patch(f"{_PR}.get_elasticsearch_client")
    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_unknown_token_returns_404(self, mock_lookup, mock_es, client):
        mock_lookup.return_value = (None, None)
        resp = client.post(
            "/public/api/query",
            json={"token": "bad", "data_view": "*fb_insights"},
        )
        assert resp.status_code == 404

    def test_missing_token_returns_400(self, client):
        resp = client.post("/public/api/query", json={"data_view": "*fb_insights"})
        assert resp.status_code == 400

    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_rejects_unwhitelisted_data_view(self, mock_lookup, client):
        mock_lookup.return_value = ("dash-1", _eligible_source())
        resp = client.post(
            "/public/api/query",
            json={"token": "tok", "data_view": "de_asset_users"},
        )
        assert resp.status_code == 403

    @mock.patch(f"{_PR}.transform")
    @mock.patch(f"{_PR}.build_query")
    @mock.patch(f"{_PR}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_forces_scoping_and_drops_client_org(
        self,
        mock_lookup,
        mock_get_doc,
        mock_es_fn,
        mock_build,
        mock_transform,
        client,
    ):
        """Client-supplied organization_title filter must be dropped; dashboard's
        stored team title must be injected instead."""
        mock_lookup.return_value = ("dash-1", _eligible_source(
            allowed_team_esids=["team-1"], allowed_org_esids=[]
        ))
        # _forced_publish_filters uses get_document_by_id to resolve team_title
        mock_get_doc.return_value = {"team_title": "Alpha"}

        es_mock = mock.MagicMock()
        es_mock.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
        mock_es_fn.return_value = es_mock
        mock_build.return_value = {"query": {"match_all": {}}}
        mock_transform.return_value = {"chart_type": "metric", "value": 0}

        resp = client.post(
            "/public/api/query",
            json={
                "token": "tok",
                "data_view": "*fb_insights",
                "filters": [
                    {"field": "organization_title", "op": "match_phrase", "value": "Evil Corp"},
                    {"field": "kibana_date", "op": "gt", "value": "2025-01-01"},
                ],
            },
        )
        assert resp.status_code == 200

        # Inspect what build_query received
        sent_config = mock_build.call_args.args[0]
        field_ops = [(f["field"], f["op"], f["value"]) for f in sent_config["filters"]]
        # Attacker's org filter must be gone.
        assert ("organization_title", "match_phrase", "Evil Corp") not in field_ops
        # Legitimate date filter must survive.
        assert ("kibana_date", "gt", "2025-01-01") in field_ops
        # Forced team filter must be injected from stored scope.
        assert ("team_title", "match_phrase", "Alpha") in field_ops

    @mock.patch(f"{_PR}.transform")
    @mock.patch(f"{_PR}.build_query")
    @mock.patch(f"{_PR}.get_elasticsearch_client")
    @mock.patch(f"{_R}.get_document_by_id")
    @mock.patch(f"{_PR}._lookup_dashboard_by_token")
    def test_allowed_data_view_runs_query(
        self,
        mock_lookup,
        mock_get_doc,
        mock_es_fn,
        mock_build,
        mock_transform,
        client,
    ):
        mock_lookup.return_value = ("dash-1", _eligible_source())
        mock_get_doc.return_value = {"organization_title": "Acme"}

        es_mock = mock.MagicMock()
        es_mock.search.return_value = {}
        mock_es_fn.return_value = es_mock
        mock_build.return_value = {"query": {"match_all": {}}}
        mock_transform.return_value = {"chart_type": "metric", "value": 123}

        resp = client.post(
            "/public/api/query",
            json={
                "token": "tok",
                "data_view": "*echo_conversations",  # in platform whitelist for 'report'
                "y_field": "client_id",
                "aggregation": "cardinality",
            },
        )
        assert resp.status_code == 200
        assert resp.get_json()["value"] == 123

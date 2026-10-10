"""
Tests for asset CRUD routes.

Routes covered:
  GET  /asset/<asset_type>                 list_assets
  GET  /asset/<asset_type>/add             add_asset  (form)
  POST /asset/<asset_type>/add             add_asset  (create)
  GET  /asset/<asset_type>/edit/<id>       edit_asset (form)
  POST /asset/<asset_type>/edit/<id>       edit_asset (update)
  POST /asset/<asset_type>/delete/<id>     delete_asset
  POST /asset/<asset_type>/bulk-delete     bulk_delete_assets

All routes require an authenticated session — unauthenticated requests
are redirected to /login.

All Elasticsearch calls are mocked; no real ES instance is required.
"""

import pytest
from unittest import mock

# this module's edit/delete/bulk-delete tests target the company-tier
# (teams) routes that now enforce a per-object scope check. Neutralize that gate
# (and the /api/options scope filter) here; the gate is covered by
# the release-plan tests
pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_TYPE = "teams"          # must exist in ASSET_CONFIGS
INVALID_TYPE = "nonexistent"  # must NOT exist in ASSET_CONFIGS
ITEM_ID = "abc123"

# Minimal ES hit structure returned by AssetService.get_all
MOCK_HIT = {
    "_id": ITEM_ID,
    "_source": {"team_title": "Test Team"},
}

# Minimal document returned by AssetService.get_by_id
MOCK_DOC = {"team_title": "Test Team"}

# Prepared item returned by FormService helpers
MOCK_DISPLAY_ITEM = {"team_title": "Test Team", "_id": ITEM_ID}


# ---------------------------------------------------------------------------
# Auth: unauthenticated access redirects to /login
# ---------------------------------------------------------------------------

class TestAssetAuthRequired:
    """Every asset route must require authentication."""

    @pytest.mark.parametrize("url,method", [
        (f"/asset/{VALID_TYPE}", "GET"),
        (f"/asset/{VALID_TYPE}/add", "GET"),
        (f"/asset/{VALID_TYPE}/edit/{ITEM_ID}", "GET"),
        (f"/asset/{VALID_TYPE}/delete/{ITEM_ID}", "POST"),
        (f"/asset/{VALID_TYPE}/bulk-delete", "POST"),
    ])
    def test_unauthenticated_is_bounced_to_login(self, client, url, method):
        # a full-page GET navigation still gets the 302 -> /login; a
        # state-changing POST carries the X-CSRF-Token (CsrfTestClient injects
        # it) so it's classified as an AJAX/API caller and gets 401 + JSON
        # {login_url} instead, so a fetch redirects the tab cleanly.
        if method == "GET":
            resp = client.get(url)
            assert resp.status_code == 302
            assert "login" in (resp.headers.get("Location") or "").lower()
        else:
            resp = client.post(url, json={"item_ids": ["id1"]})
            assert resp.status_code == 401
            assert "login" in (resp.get_json() or {}).get("login_url", "").lower()


# ---------------------------------------------------------------------------
# list_assets  GET /asset/<asset_type>
# ---------------------------------------------------------------------------

class TestListAssets:

    def test_returns_200_for_valid_type(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all", return_value=[MOCK_HIT]), \
             mock.patch("asset_management.web.app.FormService.prepare_for_display", return_value=MOCK_DISPLAY_ITEM):
            resp = auth_client.get(f"/asset/{VALID_TYPE}")
        assert resp.status_code == 200

    def test_returns_404_for_invalid_type(self, auth_client):
        resp = auth_client.get(f"/asset/{INVALID_TYPE}")
        assert resp.status_code == 404

    def test_empty_index_renders_empty_list(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_all", return_value=[]):
            resp = auth_client.get(f"/asset/{VALID_TYPE}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# add_asset  GET /asset/<asset_type>/add
# ---------------------------------------------------------------------------

class TestAddAssetGet:

    def test_renders_form_for_valid_type(self, auth_client):
        with mock.patch("asset_management.web.app.FormService.get_select_options", return_value={}):
            resp = auth_client.get(f"/asset/{VALID_TYPE}/add")
        assert resp.status_code == 200

    def test_returns_404_for_invalid_type(self, auth_client):
        resp = auth_client.get(f"/asset/{INVALID_TYPE}/add")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# add_asset  POST /asset/<asset_type>/add
# ---------------------------------------------------------------------------

class TestAddAssetPost:

    def _post(self, client, asset_type=VALID_TYPE, data=None):
        return client.post(f"/asset/{asset_type}/add", data=data or {"team_title": "New Team"})

    def test_successful_create_redirects_to_list(self, auth_client):
        # dropped the patch on RoleManagementService.create_team_role
        # (the auto-create call site was removed).
        with mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "New Team"}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new_id")):
            resp = self._post(auth_client, data={"team_title": "New Team"})
        assert resp.status_code == 302
        assert f"/asset/{VALID_TYPE}" in resp.headers["Location"]

    def test_validation_failure_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(False, "Title is required")), \
             mock.patch("asset_management.web.app.FormService.get_select_options", return_value={}):
            resp = self._post(auth_client, data={})
        assert resp.status_code == 400

    def test_es_create_failure_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "New Team"}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(False, "ES unavailable", None)):
            resp = self._post(auth_client, data={"team_title": "New Team"})
        assert resp.status_code == 500

    def test_invalid_asset_type_returns_404(self, auth_client):
        resp = self._post(auth_client, asset_type=INVALID_TYPE, data={"foo": "bar"})
        assert resp.status_code == 404

    def test_from_tree_redirect_on_success(self, auth_client):
        # dropped the patch on RoleManagementService.create_team_role
        # (the auto-create call site was removed when the legacy
        # org_*/team_* ES Security role system was retired).
        # from_tree=true now redirects to the table view at
        # /asset/<asset_type>. Asset Explorer no longer expands asset-type
        # rows into an item list, so there's nothing for the tree to
        # "select" after add — the table view is where the new row shows.
        with mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "New Team"}), \
             mock.patch("asset_management.web.app.AssetService.create_with_error", return_value=(True, None, "new_id")):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/add?from_tree=true", data={"team_title": "New Team"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == f"/asset/{VALID_TYPE}"


# ---------------------------------------------------------------------------
# Delete button markup (list.html) — regression guard
# ---------------------------------------------------------------------------

class TestDeleteButtonMarkup:
    """The per-row delete button must survive titles containing quotes.

    Regression: the button used an inline
    ``onclick="deleteItem('id', '{{ title|e }}')"`` string. For any title
    with an apostrophe (e.g. "O'Brien"), Jinja's HTML-escape rendered
    ``&#39;`` which the browser decoded back into a bare ``'`` *inside* the
    JS string literal — a syntax error that made the click a silent no-op,
    so no delete request was ever sent. The fix carries the id/title on
    ``data-*`` attributes read by a delegated click handler instead.
    """

    def _render_list(self, app, items):
        cfg = {
            "title": "Teams",
            "title_field": "team_title",
            "fields": [{"name": "team_title", "label": "Title", "type": "text"}],
        }
        with app.test_request_context("/asset/teams"):
            return app.jinja_env.get_template("list.html").render(
                config=cfg, asset_type="teams", items=items,
                current_user={}, session_config={}, csrf_token="t",
            )

    def test_delete_button_uses_data_attributes_not_inline_onclick(self, app):
        html = self._render_list(app, [{"_id": "abc123", "team_title": "Acme Team"}])
        # The button no longer carries an inline deleteItem(...) call.
        assert 'onclick="deleteItem(' not in html
        # It carries the id/title on data-* attributes instead.
        assert 'data-item-id="abc123"' in html
        assert "js-delete-asset" in html

    def test_delete_confirmation_uses_modal_not_native_confirm(self, app):
        html = self._render_list(app, [{"_id": "abc123", "team_title": "Acme Team"}])
        # The confirmation is an in-page modal, not the browser confirm() dialog.
        assert 'id="delete-modal-overlay"' in html
        assert 'id="delete-modal-confirm"' in html
        assert "confirm('" not in html and 'confirm("' not in html

    def test_apostrophe_title_renders_without_breaking_the_attribute(self, app):
        html = self._render_list(app, [{"_id": "id1", "team_title": "O'Brien Co"}])
        # The apostrophe is HTML-entity-escaped inside the double-quoted
        # attribute; the browser's dataset API decodes it back losslessly.
        # Crucially there is NO bare unescaped apostrophe that could break a
        # JS string, and no inline deleteItem call at all.
        assert 'onclick="deleteItem(' not in html
        assert 'data-item-title="O&#39;Brien Co"' in html


# ---------------------------------------------------------------------------
# edit_asset  GET /asset/<asset_type>/edit/<id>
# ---------------------------------------------------------------------------

class TestEditAssetGet:

    def test_renders_form_for_existing_item(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=MOCK_DOC), \
             mock.patch("asset_management.web.app.FormService.prepare_for_edit", return_value=MOCK_DOC), \
             mock.patch("asset_management.web.app.FormService.get_select_options", return_value={}):
            resp = auth_client.get(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}")
        assert resp.status_code == 200

    def test_returns_404_when_item_not_found(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None):
            resp = auth_client.get(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}")
        assert resp.status_code == 404

    def test_returns_404_for_invalid_asset_type(self, auth_client):
        resp = auth_client.get(f"/asset/{INVALID_TYPE}/edit/{ITEM_ID}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# edit_asset  POST /asset/<asset_type>/edit/<id>
# ---------------------------------------------------------------------------

class TestEditAssetPost:

    def test_successful_update_redirects_to_list(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=dict(MOCK_DOC)), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "Updated"}), \
             mock.patch("asset_management.web.app.AssetService.update", return_value=True):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}", data={"team_title": "Updated"})
        assert resp.status_code == 302
        assert f"/asset/{VALID_TYPE}" in resp.headers["Location"]

    def test_validation_failure_returns_400(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=dict(MOCK_DOC)), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(False, "Title required")), \
             mock.patch("asset_management.web.app.FormService.prepare_for_edit", return_value=MOCK_DOC), \
             mock.patch("asset_management.web.app.FormService.get_select_options", return_value={}):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}", data={})
        assert resp.status_code == 400

    def test_update_failure_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=dict(MOCK_DOC)), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "Updated"}), \
             mock.patch("asset_management.web.app.AssetService.update", return_value=False):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}", data={"team_title": "Updated"})
        assert resp.status_code == 500

    def test_item_not_found_returns_404(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=None):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}", data={"team_title": "Updated"})
        assert resp.status_code == 404

    def test_invalid_asset_type_returns_404(self, auth_client):
        resp = auth_client.post(f"/asset/{INVALID_TYPE}/edit/{ITEM_ID}", data={"team_title": "x"})
        assert resp.status_code == 404

    def test_from_tree_redirect_on_success(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.get_by_id", return_value=dict(MOCK_DOC)), \
             mock.patch("asset_management.web.app.FormService.validate_required_fields", return_value=(True, None)), \
             mock.patch("asset_management.web.app.FormService.process_form_data", return_value={"team_title": "Updated"}), \
             mock.patch("asset_management.web.app.AssetService.update", return_value=True):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/edit/{ITEM_ID}?from_tree=true", data={"team_title": "Updated"})
        assert resp.status_code == 302
        assert "refresh=true" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# delete_asset  POST /asset/<asset_type>/delete/<id>
# ---------------------------------------------------------------------------

class TestDeleteAsset:

    def test_successful_delete_returns_json_success(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.delete", return_value=True):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/delete/{ITEM_ID}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_delete_failure_returns_500(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.delete", return_value=False):
            resp = auth_client.post(f"/asset/{VALID_TYPE}/delete/{ITEM_ID}")
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    def test_invalid_type_returns_404_json(self, auth_client):
        resp = auth_client.post(f"/asset/{INVALID_TYPE}/delete/{ITEM_ID}")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False


# ---------------------------------------------------------------------------
# bulk_delete_assets  POST /asset/<asset_type>/bulk-delete
# ---------------------------------------------------------------------------

class TestBulkDeleteAssets:

    def test_successful_bulk_delete(self, auth_client):
        with mock.patch("asset_management.web.app.AssetService.delete_only", return_value=True):
            resp = auth_client.post(
                f"/asset/{VALID_TYPE}/bulk-delete",
                json={"item_ids": ["id1", "id2"]},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["deleted_count"] == 2
        assert data["failed_count"] == 0

    def test_partial_failure_reflected_in_counts(self, auth_client):
        def _delete(index, item_id):
            return item_id == "id1"

        with mock.patch("asset_management.web.app.AssetService.delete_only", side_effect=_delete):
            resp = auth_client.post(
                f"/asset/{VALID_TYPE}/bulk-delete",
                json={"item_ids": ["id1", "id2"]},
            )
        data = resp.get_json()
        assert data["deleted_count"] == 1
        assert data["failed_count"] == 1

    def test_empty_item_ids_returns_400(self, auth_client):
        resp = auth_client.post(
            f"/asset/{VALID_TYPE}/bulk-delete",
            json={"item_ids": []},
        )
        assert resp.status_code == 400

    def test_invalid_type_returns_404(self, auth_client):
        resp = auth_client.post(
            f"/asset/{INVALID_TYPE}/bulk-delete",
            json={"item_ids": ["id1"]},
        )
        assert resp.status_code == 404



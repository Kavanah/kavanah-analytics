"""Tests for connector_wizard_service."""
import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services import connector_wizard_service as svc


# ---------------------------------------------------------------------------
# list_assets_for_picker
# ---------------------------------------------------------------------------

class TestListAssetsForPicker:
    def test_returns_sorted_options_with_titles(self):
        with mock.patch.object(svc.AssetService, "get_all") as mock_get_all:
            mock_get_all.return_value = [
                {"_id": "esid_2", "_source": {"echo_tenant_title": "Bravo"}},
                {"_id": "esid_1", "_source": {"echo_tenant_title": "Alpha"}},
            ]
            options = svc.list_assets_for_picker("echo_tenants")

        assert [o["title"] for o in options] == ["Alpha", "Bravo"]
        assert options[0]["esid"] == "esid_1"

    def test_unknown_asset_type_raises(self):
        with pytest.raises(svc.WizardError):
            svc.list_assets_for_picker("not_a_type")

    def test_falls_back_to_esid_when_title_missing(self):
        with mock.patch.object(svc.AssetService, "get_all") as mock_get_all:
            mock_get_all.return_value = [
                {"_id": "esid_x", "_source": {}},
            ]
            options = svc.list_assets_for_picker("echo_tenants")
        assert options[0]["title"] == "esid_x"

    def test_handles_list_shaped_title(self):
        """Some title fields may be lists (multi_select). Coerce to string."""
        with mock.patch.object(svc.AssetService, "get_all") as mock_get_all:
            mock_get_all.return_value = [
                {"_id": "esid_1", "_source": {"echo_tenant_title": ["Foo", "Bar"]}},
            ]
            options = svc.list_assets_for_picker("echo_tenants")
        assert options[0]["title"] == "Foo, Bar"


# ---------------------------------------------------------------------------
# configure_echo
# ---------------------------------------------------------------------------


def _patch_existing_team():
    """Patch AssetService.get_by_id to return a minimal team doc."""
    return mock.patch.object(
        svc.AssetService,
        "get_by_id",
        return_value={"team_title": "Team One"},
    )


class TestConfigureEcho:
    @pytest.fixture(autouse=True)
    def _no_pick_workspace_check(self):
        """the pick-existing workspace check is pinned in
        test_route_authz_medium_connectors.py; these pin the link."""
        with mock.patch.object(svc, "_require_pick_in_team_workspace", return_value=None):
            yield

    def test_pick_existing_tenant_updates_team(self):
        with _patch_existing_team(), mock.patch.object(
            svc.AssetService, "update", return_value=True
        ) as mock_update:
            result = svc.configure_echo(
                team_esid="t1",
                tenant_esid="tenant_existing",
                new_tenant=None,
            )

        assert result == {"tenant_esid": "tenant_existing", "created": False}
        # Team was updated with ds_echo flipped on and the tenant ESID linked
        team_doc_arg = mock_update.call_args[0][2]
        assert team_doc_arg["ds_echo"] == "true"
        assert team_doc_arg["echo_tenant_esid"] == "tenant_existing"
        # Existing fields preserved
        assert team_doc_arg["team_title"] == "Team One"

    def test_create_new_tenant_then_link(self):
        new_tenant = {
            "echo_tenant_title": "New Tenant",
            "echo_tenant_api_key": "secret123",
            "echo_tenant_subdomain": "new-tenant",
        }
        with _patch_existing_team(), mock.patch(
            "asset_management.web.services.form_service.encrypt",
            side_effect=lambda v: f"enc:{v}",
        ), mock.patch.object(
            svc.AssetService, "create_with_error",
            return_value=(True, None, "tenant_new"),
        ) as mock_create, mock.patch.object(
            svc.AssetService, "update", return_value=True
        ) as mock_update:
            result = svc.configure_echo(
                team_esid="t1",
                tenant_esid=None,
                new_tenant=new_tenant,
            )

        assert result == {"tenant_esid": "tenant_new", "created": True}
        # Created via AssetService into the right index
        index_arg, doc_arg = mock_create.call_args[0]
        assert index_arg == "de_asset_echo_tenants"
        # API key was encrypted before being stored
        assert doc_arg["echo_tenant_api_key"] == "enc:secret123"
        # Team linked to the new tenant
        team_doc_arg = mock_update.call_args[0][2]
        assert team_doc_arg["echo_tenant_esid"] == "tenant_new"
        # the tenant's display title is stamped on the team so the
        # team-form Echo chip (echo_tenant_title) isn't blank. From new_tenant.
        assert team_doc_arg["echo_tenant_title"] == "New Tenant"

    def test_pick_existing_writes_tenant_title_from_doc(self):
        """pick-existing resolves the tenant's title from the linked
        de_asset_echo_tenants doc and stamps it on the team."""
        def fake_get_by_id(index, esid):
            if index == "de_asset_echo_tenants":
                return {"echo_tenant_title": "Acme Echo"}
            return {"team_title": "Team One"}

        with mock.patch.object(svc.AssetService, "get_by_id", side_effect=fake_get_by_id), \
             mock.patch.object(svc.AssetService, "update", return_value=True) as mock_update:
            svc.configure_echo(team_esid="t1", tenant_esid="tenant_existing", new_tenant=None)

        team_doc_arg = mock_update.call_args[0][2]
        assert team_doc_arg["echo_tenant_esid"] == "tenant_existing"
        assert team_doc_arg["echo_tenant_title"] == "Acme Echo"

    def test_rejects_both_or_neither_options(self):
        with pytest.raises(svc.WizardError):
            svc.configure_echo(team_esid="t1", tenant_esid=None, new_tenant=None)

        with pytest.raises(svc.WizardError):
            svc.configure_echo(
                team_esid="t1",
                tenant_esid="x",
                new_tenant={"echo_tenant_title": "Y"},
            )

    def test_rejects_missing_team_esid(self):
        with pytest.raises(svc.WizardError):
            svc.configure_echo(team_esid="", tenant_esid="x", new_tenant=None)

    def test_propagates_create_failure(self):
        with mock.patch(
            "asset_management.web.services.form_service.encrypt",
            side_effect=lambda v: f"enc:{v}",
        ), mock.patch.object(
            svc.AssetService, "create_with_error",
            return_value=(False, "ES is down", None),
        ):
            with pytest.raises(svc.WizardError, match="ES is down"):
                svc.configure_echo(
                    team_esid="t1",
                    tenant_esid=None,
                    new_tenant={
                        "echo_tenant_title": "T",
                        "echo_tenant_api_key": "k",
                        "echo_tenant_subdomain": "s",
                    },
                )

    def test_rejects_missing_required_fields_on_new_tenant(self):
        """FormService.validate_required_fields runs and surfaces errors."""
        with pytest.raises(svc.WizardError):
            svc.configure_echo(
                team_esid="t1",
                tenant_esid=None,
                new_tenant={"echo_tenant_title": "Only Title"},  # missing api key + subdomain
            )

    def test_team_not_found(self):
        with mock.patch.object(svc.AssetService, "get_by_id", return_value=None):
            with pytest.raises(svc.WizardError, match="not found"):
                svc.configure_echo(team_esid="bad", tenant_esid="x", new_tenant=None)


# ---------------------------------------------------------------------------
# configure_chatwoot
# ---------------------------------------------------------------------------

class TestConfigureChatwoot:
    @pytest.fixture(autouse=True)
    def _no_pick_workspace_check(self):
        """the pick-existing workspace check is pinned in
        test_route_authz_medium_connectors.py; these pin the link."""
        with mock.patch.object(svc, "_require_pick_in_team_workspace", return_value=None):
            yield

    def test_pick_existing_account_updates_team(self):
        with _patch_existing_team(), mock.patch.object(
            svc.AssetService, "update", return_value=True
        ) as mock_update:
            result = svc.configure_chatwoot(
                team_esid="t1",
                account_esid="cw_existing",
                new_account=None,
            )

        assert result == {"account_esid": "cw_existing", "created": False}
        team_doc_arg = mock_update.call_args[0][2]
        assert team_doc_arg["ds_chatwoot"] == "true"
        assert team_doc_arg["chatwoot_account_esid"] == "cw_existing"

    def test_pick_existing_writes_account_title_from_doc(self):
        """pick-existing stamps chatwoot_account_title on the team
        (resolved from the linked account doc) so the chip isn't blank."""
        def fake_get_by_id(index, esid):
            if index == "de_asset_chatwoot_accounts":
                return {"chatwoot_account_title": "Acme CW"}
            return {"team_title": "Team One"}

        with mock.patch.object(svc.AssetService, "get_by_id", side_effect=fake_get_by_id), \
             mock.patch.object(svc.AssetService, "update", return_value=True) as mock_update:
            svc.configure_chatwoot(team_esid="t1", account_esid="cw_existing", new_account=None)

        team_doc_arg = mock_update.call_args[0][2]
        assert team_doc_arg["chatwoot_account_esid"] == "cw_existing"
        assert team_doc_arg["chatwoot_account_title"] == "Acme CW"

    def test_rejects_both_or_neither(self):
        with pytest.raises(svc.WizardError):
            svc.configure_chatwoot(team_esid="t1", account_esid=None, new_account=None)


# ---------------------------------------------------------------------------
# Facebook wizard helpers
# ---------------------------------------------------------------------------


class TestFbAccountStep:
    def test_pick_existing_returns_passed_esid(self):
        assert svc.fb_pick_or_create_account(account_esid="acct1", new_account=None) == "acct1"

    def test_create_new(self):
        with mock.patch.object(
            svc.AssetService, "create_with_error",
            return_value=(True, None, "acct_new"),
        ) as mock_create:
            esid = svc.fb_pick_or_create_account(
                account_esid=None,
                new_account={"fb_account_title": "T", "fb_account_id": "100"},
            )
        assert esid == "acct_new"
        index_arg, _doc_arg = mock_create.call_args[0]
        assert index_arg == "de_asset_fb_accounts"

    def test_rejects_both_or_neither(self):
        with pytest.raises(svc.WizardError):
            svc.fb_pick_or_create_account(account_esid=None, new_account=None)
        with pytest.raises(svc.WizardError):
            svc.fb_pick_or_create_account(account_esid="x", new_account={"fb_account_title": "T"})


class TestFbAppSelectionMode:
    """pick vs skip the App step based on workspace fb_apps."""

    def _meta_app(self, esid="unified_esid", client_id="1858"):
        from asset_management.web.services.wizard_oauth_service import MetaAppCredentials
        return MetaAppCredentials(esid=esid, client_id=client_id, client_secret="x")

    def test_skip_when_workspace_has_no_apps_and_unified_is_configured(self):
        with mock.patch.object(svc, "list_assets_for_picker", return_value=[]), \
             mock.patch(
                "asset_management.web.services.wizard_oauth_service.WizardOAuthService.get_meta_app",
                return_value=self._meta_app(),
             ):
            result = svc.fb_app_selection_mode(allowed_workspaces=["new-workspace"])
        assert result == {"mode": "skip", "fb_app_esid": "unified_esid"}

    def test_pick_when_workspace_already_owns_an_app(self):
        # Existing fb_app(s) in this workspace = legacy posture; keep the picker.
        with mock.patch.object(
            svc, "list_assets_for_picker",
            return_value=[{"esid": "their_app", "title": "Their App"}],
        ), mock.patch(
            "asset_management.web.services.wizard_oauth_service.WizardOAuthService.get_meta_app",
            return_value=self._meta_app(),
        ):
            result = svc.fb_app_selection_mode(allowed_workspaces=["kavanah-media"])
        assert result == {"mode": "pick"}

    def test_pick_when_unified_app_is_not_configured(self):
        # No workspace apps AND no WIZARD_META_APP_ID resolution = degrade
        # safely to legacy picker rather than break the wizard.
        with mock.patch.object(svc, "list_assets_for_picker", return_value=[]), \
             mock.patch(
                "asset_management.web.services.wizard_oauth_service.WizardOAuthService.get_meta_app",
                return_value=None,
             ):
            result = svc.fb_app_selection_mode(allowed_workspaces=["new-workspace"])
        assert result == {"mode": "pick"}

    def test_passes_allowed_workspaces_through_to_picker(self):
        # The selection-mode decision MUST be made against the same
        # workspace-scoped picker view the user would otherwise see — no
        # chance of UX drift between "the picker says 'No existing FB apps'"
        # and "the selection mode".
        with mock.patch.object(svc, "list_assets_for_picker", return_value=[]) as mock_picker, \
             mock.patch(
                "asset_management.web.services.wizard_oauth_service.WizardOAuthService.get_meta_app",
                return_value=self._meta_app(),
             ):
            svc.fb_app_selection_mode(allowed_workspaces=["ws-a", "ws-b"])
        mock_picker.assert_called_once_with(
            "facebook_apps", allowed_workspaces=["ws-a", "ws-b"]
        )


class TestFbListPages:
    def _patch_decrypt(self):
        return mock.patch("lib.encryption.decrypt", side_effect=lambda v: v.replace("enc:", ""))

    def test_lists_pages_from_graph(self):
        with mock.patch.object(
            svc.AssetService, "get_by_id",
            return_value={"fb_app_user_token": "enc:tok123"},
        ), self._patch_decrypt(), mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "data": [
                    {"id": "111", "name": "Bravo", "username": "bravo"},
                    {"id": "222", "name": "alpha", "username": "alpha"},
                ]
            }
            pages = svc.fb_list_pages_via_graph("user1")

        # Sorted alphabetically by name (case-insensitive)
        assert [p["name"] for p in pages] == ["alpha", "Bravo"]
        # Token was passed to FB Graph
        params = mock_get.call_args.kwargs["params"]
        assert params["access_token"] == "tok123"

    def test_raises_when_user_has_no_token(self):
        with mock.patch.object(
            svc.AssetService, "get_by_id",
            return_value={"fb_app_user_title": "X"},
        ):
            with pytest.raises(svc.WizardError, match="no access token"):
                svc.fb_list_pages_via_graph("user1")

    def test_raises_on_graph_http_error(self):
        with mock.patch.object(
            svc.AssetService, "get_by_id",
            return_value={"fb_app_user_token": "enc:tok"},
        ), self._patch_decrypt(), mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 400
            mock_get.return_value.json.return_value = {"error": {"message": "bad token", "code": 190}}
            # Meta's raw message stays in the log; code 190 maps to
            # a fixed reconnect message.
            with pytest.raises(svc.WizardError, match="Meta access token expired") as excinfo:
                svc.fb_list_pages_via_graph("user1")
        assert "bad token" not in str(excinfo.value)

    def test_raises_fixed_message_on_graph_error_without_known_code(self):
        with mock.patch.object(
            svc.AssetService, "get_by_id",
            return_value={"fb_app_user_token": "enc:tok"},
        ), self._patch_decrypt(), mock.patch("requests.get") as mock_get:
            mock_get.return_value.status_code = 500
            mock_get.return_value.json.return_value = {"error": {"message": "internal detail"}}
            with pytest.raises(svc.WizardError, match=r"HTTP 500") as excinfo:
                svc.fb_list_pages_via_graph("user1")
        assert "internal detail" not in str(excinfo.value)

    def test_raises_on_graph_network_error(self):
        import requests as _requests
        with mock.patch.object(
            svc.AssetService, "get_by_id",
            return_value={"fb_app_user_token": "enc:tok"},
        ), self._patch_decrypt(), mock.patch(
            "requests.get",
            side_effect=_requests.RequestException("conn refused"),
        ):
            # a requests error's text carries the URL (and the
            # access_token query param); it must not reach the browser.
            with pytest.raises(svc.WizardError, match="Could not reach Meta") as excinfo:
                svc.fb_list_pages_via_graph("user1")
        assert "conn refused" not in str(excinfo.value)


class TestFbLinkPagesToTeam:
    # (2026-05-26) — fb_link_pages_to_team now also runs the
    # Instagram bridge after the FB chain lands. The IG bridge resolves
    # an access token via the fb_account → fb_apps → fb_app_users chain;
    # without an ES client patched in, that resolution returns None and
    # the bridge short-circuits cleanly (no IG asset writes, no team
    # back-link update). The return shape gains ``ig_account_esids`` /
    # ``ig_discovered_count`` — these tests pin the FB-side return
    # fields and ignore the IG-side counts (covered tests).

    @pytest.fixture(autouse=True)
    def _no_cross_workspace_lookup(self):
        """the cross-workspace Page guard queries ES; it is pinned
        in test_route_authz_connectors.py, not here."""
        with mock.patch.object(svc, "_refuse_pages_linked_in_other_workspaces", return_value=None):
            yield

    def _patch_ig_bridge_noop(self):
        """Stub the IG bridge so these FB-only tests don't
        depend on the IG resolver's behaviour."""
        return mock.patch(
            "asset_management.web.services.meta_ig_bridge_service.bridge_ig_chain_for_pages",
            return_value={"ig_account_esids": [], "discovered_count": 0},
        )

    def test_links_existing_pages_only(self):
        with _patch_existing_team(), mock.patch.object(
            svc.AssetService, "update", return_value=True
        ) as mock_update, self._patch_ig_bridge_noop():
            result = svc.fb_link_pages_to_team(
                team_esid="t1",
                fb_account_esid="acct1",
                pages=[{"esid": "p1"}, {"esid": "p2"}],
            )

        assert result["page_esids"] == ["p1", "p2"]
        assert result["created_count"] == 0
        team_doc = mock_update.call_args[0][2]
        assert team_doc["ds_facebook"] == "true"
        assert team_doc["team_facebook_page_esids"] == ["p1", "p2"]

    def test_creates_new_pages_then_links(self):
        with _patch_existing_team(), mock.patch.object(
            svc.AssetService, "create_with_error",
            side_effect=[(True, None, "p_new1"), (True, None, "p_new2")],
        ), mock.patch.object(svc.AssetService, "update", return_value=True), self._patch_ig_bridge_noop():
            result = svc.fb_link_pages_to_team(
                team_esid="t1",
                fb_account_esid="acct1",
                pages=[
                    {"fb_page_id": "111", "fb_page_name": "Alpha"},
                    {"fb_page_id": "222", "fb_page_name": "Bravo", "fb_page_slug": "bravo"},
                ],
            )

        assert result["page_esids"] == ["p_new1", "p_new2"]
        assert result["created_count"] == 2

    def test_mixed_existing_and_new(self):
        with _patch_existing_team(), mock.patch.object(
            svc.AssetService, "create_with_error",
            return_value=(True, None, "p_new1"),
        ), mock.patch.object(svc.AssetService, "update", return_value=True), self._patch_ig_bridge_noop():
            result = svc.fb_link_pages_to_team(
                team_esid="t1",
                fb_account_esid="acct1",
                pages=[
                    {"esid": "p_existing"},
                    {"fb_page_id": "111", "fb_page_name": "Alpha"},
                ],
            )

        assert result["page_esids"] == ["p_existing", "p_new1"]
        assert result["created_count"] == 1

    def test_rejects_empty_page_list(self):
        with pytest.raises(svc.WizardError, match="At least one page"):
            svc.fb_link_pages_to_team(team_esid="t1", fb_account_esid="acct1", pages=[])

    def test_rejects_new_page_without_required_fields(self):
        with _patch_existing_team():
            with pytest.raises(svc.WizardError, match="fb_page_id"):
                svc.fb_link_pages_to_team(
                    team_esid="t1",
                    fb_account_esid="acct1",
                    pages=[{"fb_page_name": "Just A Name"}],
                )


# ---------------------------------------------------------------------------
# _prune_dangling_esids (GA4 / YouTube re-connect cleanup)
# ---------------------------------------------------------------------------


class TestKan992PruneDanglingEsids:
    def test_drops_esids_that_no_longer_resolve(self):
        """an ESID whose asset doc is gone is dropped; the live one
        is kept. This is what lets a re-connect clear a dangling reference
        instead of accumulating it forever (the old append-only behaviour)."""
        def get_by_id(index, esid):
            return {"x": 1} if esid == "live" else None  # "dead" resolves to nothing

        with mock.patch("asset_management.web.services.asset_service.AssetService.get_by_id",
                        side_effect=get_by_id):
            kept, dropped = svc._prune_dangling_esids(["live", "dead"], "de_asset_ga_properties")
        assert kept == ["live"]
        # the dropped esid is reported so the caller can audit it.
        assert dropped == ["dead"]

    def test_preserves_order(self):
        def get_by_id(index, esid):
            return {"x": 1}  # all resolve

        with mock.patch("asset_management.web.services.asset_service.AssetService.get_by_id",
                        side_effect=get_by_id):
            kept, dropped = svc._prune_dangling_esids(["a", "b", "c"], "de_asset_youtube_channels")
        assert kept == ["a", "b", "c"]
        assert dropped == []

    def test_fails_open_on_lookup_error(self):
        """A transient ES error must NOT silently unlink a live asset — the
        ESID is kept when the lookup raises."""
        def get_by_id(index, esid):
            raise RuntimeError("ES down")

        with mock.patch("asset_management.web.services.asset_service.AssetService.get_by_id",
                        side_effect=get_by_id):
            kept, dropped = svc._prune_dangling_esids(["a", "b"], "de_asset_ga_properties")
        assert kept == ["a", "b"]
        # Lookup errors are fail-open: nothing is reported as dropped either.
        assert dropped == []

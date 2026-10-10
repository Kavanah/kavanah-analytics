"""Phase 3 — Bitly connector productization (DEAM surfaces).

Covers the web-side integration this phase adds, mirroring the Disciple.Tools
 exemplar's coverage:

* ConnectionService — provider registration, ``create_bitly_connection``
  encryption round-trip (no plaintext token in the stored doc) and
  natural-key reuse (same team → upsert, never a duplicate);
* configure_bitly wizard — token validation (ok / 403 / unreachable),
  tag normalization, keep-if-unchanged saved token, team stamping;
* connector catalog — tile registration (onboarding_eligible=False policy);
* connector_status_service — the bitly descriptor's four states + wizard
  prefill payload (has_saved_token / tags), and get_team_connectors
  surfacing bitly for the import-schedule wizard;
* connections blueprint — provider label / disconnect / team-pointer sets;
* reference_cleanup_service — the team↔bitly_connection_esid edge.
"""

from unittest import mock

import pytest
from cryptography.fernet import Fernet

from asset_management.web.services import connection_service as cs
from asset_management.web.services import connector_status_service as css
from asset_management.web.services import connector_wizard_service as cws
from asset_management.web.services.connection_service import ConnectionService
from asset_management.web.services.connector_catalog_service import ConnectorCatalogService
from asset_management.web.services.connector_wizard_service import WizardError, configure_bitly

_CS = "asset_management.web.services.connection_service"
_CWS = "asset_management.web.services.connector_wizard_service"
_CSS = "asset_management.web.services.connector_status_service"


@pytest.fixture()
def enc_key(monkeypatch):
    """A real Fernet key so encrypt/decrypt round-trips without master-config."""
    monkeypatch.setenv("ENC_KEY", Fernet.generate_key().decode())


# ── ConnectionService ─────────────────────────────────────────────────────────


class TestBitlyProviderRegistration:

    def test_should_register_bitly_as_a_valid_provider(self):
        assert cs.PROVIDER_BITLY == "bitly"
        assert cs.PROVIDER_BITLY in cs._VALID_PROVIDERS

    def test_should_not_require_oauth_for_bitly(self):
        # Non-OAuth token connector: a bitly row must never be forced to
        # pending_token by the tokenless-provider guard.
        assert cs.PROVIDER_BITLY not in cs._OAUTH_REQUIRED_PROVIDERS

    def test_should_register_bitly_account_asset_type(self):
        assert cs.ASSET_TYPE_BITLY_ACCOUNT == "bitly_account"
        assert cs.ASSET_TYPE_BITLY_ACCOUNT in cs._VALID_ASSET_TYPES


class _RecordingES:
    """Fake ES: no existing natural-key rows; records every index() body."""

    def __init__(self, existing_hits=None):
        self.indexed = []
        self.existing_hits = existing_hits or []

    def search(self, index=None, body=None, **_kw):
        return {"hits": {"hits": list(self.existing_hits)}}

    def index(self, index=None, id=None, body=None, **_kw):
        self.indexed.append({"index": index, "id": id, "body": body})
        return {"_id": id or f"gen-{len(self.indexed)}"}

    def get(self, index=None, id=None, **_kw):
        return {"_source": {"principal": "john"}}


class TestCreateBitlyConnection:

    def test_should_store_the_token_encrypted_never_plaintext(self, enc_key):
        es = _RecordingES()
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es):
            result = ConnectionService.create_bitly_connection(
                principal="john",
                team_esid="team-1",
                api_token="secret-token-123",
                tags=["FLKVT", " spring ", "FLKVT"],
            )
        assert result["success"] is True
        conn_docs = [d for d in es.indexed if d["index"] == cs.INDEX_CONNECTION]
        assert len(conn_docs) == 1
        doc = conn_docs[0]["body"]
        assert doc["provider"] == "bitly"
        assert doc["provider_account_id"] == "team-1"
        assert doc["status"] == cs.STATUS_ACTIVE
        # Encryption round-trip: ciphertext stored, plaintext recoverable,
        # and the plaintext never appears anywhere in the stored doc.
        from lib.encryption import decrypt

        assert doc["oauth_token_enc"] != "secret-token-123"
        assert decrypt(doc["oauth_token_enc"]) == "secret-token-123"
        assert "secret-token-123" not in str({k: v for k, v in doc.items()})

    def test_should_write_the_tag_filter_on_the_asset_row(self, enc_key):
        es = _RecordingES()
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es):
            result = ConnectionService.create_bitly_connection(
                principal="john",
                team_esid="team-1",
                api_token="tok",
                tags=["b-tag", "a-tag", "", "  "],
            )
        assert result["success"] is True
        asset_docs = [d for d in es.indexed if d["index"] == cs.INDEX_CONNECTION_ASSET]
        assert len(asset_docs) == 1
        asset = asset_docs[0]["body"]
        assert asset["asset_type"] == cs.ASSET_TYPE_BITLY_ACCOUNT
        assert asset["provider_asset_id"] == "team-1"
        assert asset["metadata"]["tags"] == ["a-tag", "b-tag"]  # sorted, deduped, blanks dropped

    def test_should_reuse_the_existing_connection_for_the_same_team(self, enc_key):
        # Natural-key reuse: same
        # (principal, bitly, team) upserts in place, never duplicates.
        existing = [{"_id": "conn-existing", "_source": {"created_at": "2026-01-01T00:00:00+00:00"}}]
        es = _RecordingES(existing_hits=existing)
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es):
            result = ConnectionService.create_bitly_connection(
                principal="john", team_esid="team-1", api_token="tok2", tags=["t"]
            )
        assert result["success"] is True
        assert result["connection_esid"] == "conn-existing"
        conn_writes = [d for d in es.indexed if d["index"] == cs.INDEX_CONNECTION]
        assert conn_writes[0]["id"] == "conn-existing"

    def test_should_require_team_token_and_tags(self):
        assert (
            ConnectionService.create_bitly_connection(principal="p", team_esid="", api_token="t", tags=["x"])["success"]
            is False
        )
        assert (
            ConnectionService.create_bitly_connection(principal="p", team_esid="t", api_token="", tags=["x"])["success"]
            is False
        )
        assert (
            ConnectionService.create_bitly_connection(principal="p", team_esid="t", api_token="t", tags=[])["success"] is False
        )


# ── configure_bitly wizard ────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class TestConfigureBitly:

    def _configure(self, *, verify_status=200, team_doc=None, create_result=None, **kwargs):
        args = {"team_esid": "team-1", "api_token": "tok", "tags": "FLKVT, spring", "username": "john"}
        args.update(kwargs)
        create_result = create_result or {"success": True, "connection_esid": "conn-1", "asset_esid": "asset-1"}
        with (
            mock.patch(f"{_CWS}.AssetService.get_by_id", return_value=team_doc or {"team_title": "FLK North"}),
            mock.patch("requests.get", return_value=_Resp(verify_status)) as mock_get,
            mock.patch.object(ConnectionService, "create_bitly_connection", return_value=create_result) as mock_create,
            mock.patch(f"{_CWS}._update_team_data_source") as mock_stamp,
        ):
            result = configure_bitly(**args)
        return result, mock_get, mock_create, mock_stamp

    def test_should_configure_and_stamp_the_team_when_token_validates(self):
        result, mock_get, mock_create, mock_stamp = self._configure()
        # Token validated server-side against GET /v4/user.
        assert mock_get.call_args[0][0] == "https://api-ssl.bitly.com/v4/user"
        assert mock_get.call_args[1]["headers"]["Authorization"] == "Bearer tok"
        # Connection written with normalized tags, principal = session user.
        assert mock_create.call_args[1]["principal"] == "john"
        assert mock_create.call_args[1]["tags"] == ["FLKVT", "spring"]
        # Team stamped: ds_bitly + bitly_connection_esid.
        assert mock_stamp.call_args[1]["flag_field"] == "ds_bitly"
        assert mock_stamp.call_args[1]["esid_field"] == "bitly_connection_esid"
        assert mock_stamp.call_args[1]["esid_value"] == "conn-1"
        assert result["connection_esid"] == "conn-1"
        assert result["tags"] == ["FLKVT", "spring"]

    def test_should_surface_a_clear_error_when_bitly_returns_403(self):
        with pytest.raises(WizardError, match="Bitly rejected the API token"):
            self._configure(verify_status=403)

    def test_should_surface_a_clear_error_when_bitly_is_unreachable(self):
        with (
            mock.patch(f"{_CWS}.AssetService.get_by_id", return_value={"team_title": "T"}),
            mock.patch("requests.get", side_effect=OSError("boom")),
        ):
            with pytest.raises(WizardError, match="Could not reach the Bitly API"):
                configure_bitly(team_esid="team-1", api_token="tok", tags="a", username="john")

    def test_should_write_nothing_when_validation_fails(self):
        with (
            mock.patch(f"{_CWS}.AssetService.get_by_id", return_value={"team_title": "T"}),
            mock.patch("requests.get", return_value=_Resp(401)),
            mock.patch.object(ConnectionService, "create_bitly_connection") as mock_create,
            mock.patch(f"{_CWS}._update_team_data_source") as mock_stamp,
        ):
            with pytest.raises(WizardError):
                configure_bitly(team_esid="team-1", api_token="tok", tags="a", username="john")
        mock_create.assert_not_called()
        mock_stamp.assert_not_called()

    def test_should_require_at_least_one_tag(self):
        with pytest.raises(WizardError, match="tag"):
            configure_bitly(team_esid="team-1", api_token="tok", tags=" , ,", username="john")

    def test_should_require_authenticated_username(self):
        with pytest.raises(WizardError, match="Not authenticated"):
            configure_bitly(team_esid="team-1", api_token="tok", tags="a", username="")

    def test_should_reuse_the_saved_token_when_left_unchanged(self):
        # Keep-if-unchanged (DT pattern): empty api_token resolves the stored
        # plaintext and re-verifies with it.
        with (
            mock.patch(f"{_CWS}._existing_bitly_connection", return_value=("saved-tok", "conn-1", ["old"])),
            mock.patch(f"{_CWS}.AssetService.get_by_id", return_value={"team_title": "T"}),
            mock.patch("requests.get", return_value=_Resp(200)) as mock_get,
            mock.patch.object(
                ConnectionService,
                "create_bitly_connection",
                return_value={"success": True, "connection_esid": "conn-1", "asset_esid": "a"},
            ) as mock_create,
            mock.patch(f"{_CWS}._update_team_data_source"),
        ):
            configure_bitly(team_esid="team-1", api_token="", tags="new-tag", username="john")
        assert mock_get.call_args[1]["headers"]["Authorization"] == "Bearer saved-tok"
        assert mock_create.call_args[1]["api_token"] == "saved-tok"
        assert mock_create.call_args[1]["tags"] == ["new-tag"]

    def test_should_require_a_token_when_none_is_saved(self):
        with mock.patch(f"{_CWS}._existing_bitly_connection", return_value=None):
            with pytest.raises(WizardError, match="token is required"):
                configure_bitly(team_esid="team-1", api_token="", tags="a", username="john")

    def test_should_normalize_list_and_string_tags_identically(self):
        assert cws._normalize_bitly_tags("b, a ,a,") == ["a", "b"]
        assert cws._normalize_bitly_tags(["b", " a ", "a"]) == ["a", "b"]
        assert cws._normalize_bitly_tags("") == []


# ── Connector catalog ─────────────────────────────────────────────────────────


class TestBitlyCatalogTile:

    def test_should_register_the_bitly_tile(self):
        tile = ConnectorCatalogService.get("bitly")
        assert tile is not None
        assert tile.name == "Bitly"
        assert tile.icon_file == "bitly.svg"
        assert tile.coming_soon is False
        assert tile.hidden is False

    def test_should_never_appear_in_the_onboarding_wizard(self):
        # Hard policy: new connectors are onboarding_eligible=False.
        tile = ConnectorCatalogService.get("bitly")
        assert tile.onboarding_eligible is False
        assert "bitly" not in ConnectorCatalogService.selectable_ids()
        onboarding_tiles = ConnectorCatalogService.popular_tiles() + ConnectorCatalogService.all_section_tiles()
        assert "bitly" not in {t.connector_id for t in onboarding_tiles}

    def test_should_appear_on_the_data_connectors_page(self):
        assert "bitly" in {t.connector_id for t in ConnectorCatalogService.all_tiles()}


# ── connector_status_service ──────────────────────────────────────────────────


def _bitly_descriptor():
    return css.CONNECTORS_BY_KEY["bitly"]


class TestBitlyConnectorStatus:

    def test_should_report_not_configured_when_flag_off(self):
        payload = css._connector_status({}, _bitly_descriptor(), team_esid="t1")
        assert payload["state"] == "not_configured"
        assert payload["configured"] is False
        assert payload["auth"] == "api_key"
        assert payload["connection_esid"] is None

    def test_should_report_needs_setup_when_flag_on_but_no_pointer(self):
        team = {"ds_bitly": "true"}
        payload = css._connector_status(team, _bitly_descriptor(), team_esid="t1")
        assert payload["state"] == "needs_setup"

    def test_should_report_configured_with_prefill_when_connection_live(self):
        team = {"ds_bitly": "true", "bitly_connection_esid": "conn-1"}
        with (
            mock.patch.object(ConnectionService, "is_connection_live", return_value=True),
            mock.patch(f"{_CSS}._bitly_connection_info", return_value=(True, ["FLKVT"])),
        ):
            payload = css._connector_status(team, _bitly_descriptor(), team_esid="t1")
        assert payload["state"] == "configured"
        assert payload["configured"] is True
        assert payload["connection_esid"] == "conn-1"
        assert payload["has_saved_token"] is True
        assert payload["tags"] == ["FLKVT"]

    def test_should_report_disconnected_when_connection_revoked_or_deleted(self):
        team = {"ds_bitly": "true", "bitly_connection_esid": "conn-1"}
        with (
            mock.patch.object(ConnectionService, "is_connection_live", return_value=False),
            mock.patch(f"{_CSS}._bitly_connection_info", return_value=(False, [])),
        ):
            payload = css._connector_status(team, _bitly_descriptor(), team_esid="t1")
        assert payload["state"] == "disconnected"
        assert payload["connection_esid"] is None

    def test_should_surface_bitly_in_get_team_connectors(self):
        # Import-schedule wizard tile: a team with a live Bitly connection
        # gets a configured bitly entry from get_team_connectors.
        team = {"team_title": "FLK North", "ds_bitly": "true", "bitly_connection_esid": "conn-1"}
        with (
            mock.patch(f"{_CSS}.get_document_by_id", return_value=team),
            mock.patch.object(ConnectionService, "is_connection_live", return_value=True),
            mock.patch(f"{_CSS}._bitly_connection_info", return_value=(True, ["FLKVT"])),
            mock.patch(f"{_CSS}._resolve_org_title", return_value=""),
        ):
            payload = css.get_team_connectors("team-1")
        entries = {c["key"]: c for c in payload["connectors"]}
        assert "bitly" in entries
        assert entries["bitly"]["configured"] is True

    def test_should_support_generic_disconnect(self):
        # disconnect_connector's generic patch must clear the flag and the
        # connection pointer (the descriptor declares both fields).
        descriptor = _bitly_descriptor()
        assert descriptor["team_flag"] == "ds_bitly"
        assert descriptor["team_esid_field"] == "bitly_connection_esid"
        assert "bitly" not in css._MULTI_ACCOUNT_CONNECTORS


# ── connections blueprint sets ────────────────────────────────────────────────


class TestConnectionsBlueprintSets:

    def _routes(self):
        from asset_management.web.blueprints.connections import routes

        return routes

    def test_should_label_bitly_connections(self):
        assert self._routes()._PROVIDER_LABELS["bitly"] == "Bitly"

    def test_should_support_disconnect_but_not_reauth(self):
        routes = self._routes()
        assert "bitly" in routes._DISCONNECT_SUPPORTED_PROVIDERS
        assert "bitly" not in routes._REAUTH_SUPPORTED_PROVIDERS
        assert "bitly" not in routes._GOOGLE_REAUTH_PROVIDERS

    def test_should_resolve_team_links_via_bitly_connection_esid(self):
        assert "bitly_connection_esid" in self._routes()._TEAM_CONNECTION_FIELDS


# ── reference cleanup edge ────────────────────────────────────────────────────


class TestBitlyCleanupEdge:

    def test_should_register_the_team_bitly_connection_edge(self):
        from asset_management.web.services.reference_cleanup_service import REVERSE_MAP

        entries = REVERSE_MAP.get("de_connection", [])
        edge = next((e for e in entries if e["esid_field"] == "bitly_connection_esid"), None)
        assert edge is not None
        assert edge["child_index"] == "de_asset_teams"
        assert edge["is_array"] is False

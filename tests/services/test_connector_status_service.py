"""Tests for connector_status_service."""

import os
import sys
from unittest import mock

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services import connector_status_service as svc


# ---------------------------------------------------------------------------
# list_teams
# ---------------------------------------------------------------------------


class TestListTeams:
    def test_returns_sorted_team_dicts(self):
        # Sort order: strict alphabetical by team title (case-insensitive),
        # regardless of org membership.
        with mock.patch.object(svc, "get_all_documents") as mock_get_all:
            mock_get_all.return_value = [
                {"_id": "esid_d", "_source": {"team_title": "delta", "organization_title": "Org A"}},
                {"_id": "esid_b", "_source": {"team_title": "Beta", "organization_title": "Org B"}},
                {"_id": "esid_a", "_source": {"team_title": "Alpha", "organization_title": "Org B"}},
                {"_id": "esid_c", "_source": {"team_title": "Charlie", "organization_title": "Org A"}},
            ]
            teams = svc.list_teams()

        assert [t["title"] for t in teams] == ["Alpha", "Beta", "Charlie", "delta"]
        assert teams[0]["esid"] == "esid_a"
        assert teams[0]["org_title"] == "Org B"

    def test_returns_empty_on_es_error(self):
        with mock.patch.object(svc, "get_all_documents", side_effect=Exception("boom")):
            assert svc.list_teams() == []

    def test_falls_back_to_esid_when_title_missing(self):
        with mock.patch.object(svc, "get_all_documents") as mock_get_all:
            mock_get_all.return_value = [
                {"_id": "esid_x", "_source": {}},
            ]
            teams = svc.list_teams()
        assert teams[0]["title"] == "esid_x"

    def test_handles_organization_title_stored_as_list(self):
        """Regression: organization_title is a multi_select on teams, so it's
        stored as a list in ES. Must coerce to string for sorting / display."""
        with mock.patch.object(svc, "get_all_documents") as mock_get_all:
            mock_get_all.return_value = [
                {
                    "_id": "t1",
                    "_source": {
                        "team_title": "Alpha",
                        "organization_title": ["Org A", "Org B"],
                    },
                },
                {
                    "_id": "t2",
                    "_source": {
                        "team_title": "Beta",
                        "organization_title": [],
                    },
                },
            ]
            teams = svc.list_teams()

        # Did not raise. Both teams returned with string org_title values.
        assert len(teams) == 2
        titles_by_team = {t["title"]: t["org_title"] for t in teams}
        assert titles_by_team["Alpha"] == "Org A, Org B"
        assert titles_by_team["Beta"] == ""


class TestOrgTitleResolution:
    """wizard-provisioned teams store organization_esid but NOT
    organization_title, so the org name rendered as "(Unassigned)" in the Report
    Scheduler tree and blank in the Data Connectors header. The org title must be
    resolved from organization_esid for those teams while explicitly-stored
    titles still win."""

    def test_list_teams_resolves_org_title_from_esid_for_wizard_team(self):
        # Wizard team: organization_esid present, organization_title absent.
        team_docs = [
            {"_id": "t1", "_source": {"team_title": "Wizard Team", "organization_esid": ["org-7"]}},
        ]
        org_docs = [
            {"_id": "org-7", "_source": {"organization_title": "Real Org"}},
        ]

        def _get_all(index, query=None, *a, **k):
            return org_docs if index == svc.ORGS_INDEX else team_docs

        with mock.patch.object(svc, "get_all_documents", side_effect=_get_all):
            teams = svc.list_teams()

        assert len(teams) == 1
        # The org name is resolved from organization_esid -> org doc, NOT blank.
        assert teams[0]["org_title"] == "Real Org"

    def test_list_teams_prefers_stored_title_over_lookup(self):
        # A team that DID store organization_title keeps it (no override from the
        # esid lookup), so legacy/explicit teams are unaffected.
        team_docs = [
            {
                "_id": "t1",
                "_source": {
                    "team_title": "Legacy Team",
                    "organization_title": "Stored Org",
                    "organization_esid": ["org-7"],
                },
            },
        ]
        org_docs = [{"_id": "org-7", "_source": {"organization_title": "Different Org"}}]

        def _get_all(index, query=None, *a, **k):
            return org_docs if index == svc.ORGS_INDEX else team_docs

        with mock.patch.object(svc, "get_all_documents", side_effect=_get_all):
            teams = svc.list_teams()

        assert teams[0]["org_title"] == "Stored Org"

    def test_get_team_connectors_resolves_org_title_for_wizard_team(self):
        # The Data Connectors header reads team.org_title from get_team_connectors;
        # a wizard team (esid only) must show its real org, not a blank.
        team_source = {"team_title": "Wizard Team", "organization_esid": ["org-7"]}
        with (
            mock.patch.object(svc, "get_document_by_id") as mock_get,
            mock.patch.object(svc, "_connector_status", return_value={"key": "x", "configured": False}),
        ):
            def _by_id(index, doc_id):
                if index == svc.ORGS_INDEX and doc_id == "org-7":
                    return {"organization_title": "Real Org"}
                if index == svc.TEAMS_INDEX:
                    return team_source
                return None

            mock_get.side_effect = _by_id
            payload = svc.get_team_connectors("t1")

        assert payload["team"]["org_title"] == "Real Org"


# ---------------------------------------------------------------------------
# get_team_connectors
# ---------------------------------------------------------------------------


class TestGetTeamConnectors:
    def test_returns_none_for_unknown_team(self):
        with mock.patch.object(svc, "get_document_by_id", return_value=None):
            assert svc.get_team_connectors("nope") is None

    def test_returns_none_for_empty_esid(self):
        assert svc.get_team_connectors("") is None
        assert svc.get_team_connectors(None) is None

    def test_configured_facebook_with_linked_pages(self):
        # Meta Ads is the merged FB+IG card. earlier,
        # Instagram is additive: FB-only is "configured" (see the dedicated
        # test below). This case has BOTH sides linked.
        team_source = {
            "team_title": "Team One",
            "organization_title": "Org A",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["page_a", "page_b"],
            "ds_instagram": "true",
            "team_instagram_profile_esids": ["ig_a"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_pages" and doc_id == "page_a":
                return {"fb_page_name": "Page A"}
            if index == "de_asset_fb_pages" and doc_id == "page_b":
                return {"fb_page_name": "Page B"}
            if index == "de_asset_ig_accounts" and doc_id == "ig_a":
                return {"ig_account_title": "IG A"}
            return None

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_1")

        assert payload["team"]["title"] == "Team One"
        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "configured"
        assert fb["configured"] is True
        assert [a["title"] for a in fb["linked_assets"]] == ["Page A", "Page B"]
        assert [a["title"] for a in fb["ig_linked_assets"]] == ["IG A"]
        assert fb["ig_configured"] is True

    def test_needs_setup_when_flag_on_but_esid_unresolvable(self):
        """ds_facebook=true but the linked page ESID doesn't resolve to a doc.
        the dangling-link case is now ``needs_setup`` (was the generic
        ``misconfigured``), so the card can say "finish linking an asset"."""
        team_source = {
            "team_title": "Team One",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["dangling_esid"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            return None  # FB page lookup returns nothing

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_1")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "needs_setup"
        assert fb["configured"] is False
        assert fb["linked_assets"] == []

    def test_not_configured_when_flag_off(self):
        team_source = {"team_title": "Team One", "ds_facebook": "false"}

        def get_by_id(index, doc_id):
            return team_source if index == "de_asset_teams" else None

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_1")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "not_configured"
        assert fb["configured"] is False

    def test_disciple_tools_driven_by_url_field(self):
        team_source = {
            "team_title": "Team One",
            "team_disciple_tools_instance": "https://dt.example.org",
        }
        # has_saved_token does a connection lookup; stub it True here.
        with (
            mock.patch.object(svc, "get_document_by_id", return_value=team_source),
            mock.patch.object(svc, "_dt_site_has_token", return_value=True),
        ):
            payload = svc.get_team_connectors("team_1")

        dt = next(c for c in payload["connectors"] if c["key"] == "disciple_tools")
        assert dt["configured"] is True
        assert dt["state"] == "configured"
        assert dt["instance_url"] == "https://dt.example.org"
        # the saved-token flag drives the wizard's dots display; the
        # ciphertext itself is NOT shipped to the client.
        assert dt["has_saved_token"] is True
        assert "token_enc" not in dt

    def test_disciple_tools_blank_url_is_not_configured(self):
        team_source = {"team_title": "Team One", "team_disciple_tools_instance": "   "}
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            payload = svc.get_team_connectors("team_1")

        dt = next(c for c in payload["connectors"] if c["key"] == "disciple_tools")
        assert dt["configured"] is False
        assert dt["state"] == "not_configured"

    def test_every_connector_has_state_field(self):
        """Regression: every connector dict in the payload must have `state` so
        the UI filter can rely on it uniformly (DT used to omit it)."""
        team_source = {"team_title": "T"}
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            payload = svc.get_team_connectors("t")
        for c in payload["connectors"]:
            assert "state" in c, f"connector {c['key']} missing 'state' field"
            # vocabulary expanded: misconfigured split into
            # needs_setup + disconnected.
            assert c["state"] in {"configured", "needs_setup", "disconnected", "not_configured"}

    def test_facebook_payload_includes_token_health_when_configured(self):
        """Configured Meta payload includes a token_health field; not_configured
        teams skip the resolve step. token_health gate is now on
        the merged "configured" state (FB AND IG)."""
        team_source = {
            "team_title": "T",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["page_a"],
            "ds_instagram": "true",
            "team_instagram_profile_esids": ["ig_a"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_pages" and doc_id == "page_a":
                return {"fb_page_name": "Alpha", "fb_account_esid": "acct1"}
            if index == "de_asset_ig_accounts" and doc_id == "ig_a":
                return {"ig_account_title": "IG A"}
            return None

        with (
            mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id),
            mock.patch.object(
                svc, "_resolve_fb_token_health", return_value={"status": "healthy", "expires_in_days": 60}
            ) as mock_th,
        ):
            payload = svc.get_team_connectors("t")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "configured"
        assert fb["token_health"] == {"status": "healthy", "expires_in_days": 60}
        # Resolution called once with the linked-asset list.
        assert mock_th.called

    def test_facebook_payload_skips_token_health_when_not_configured(self):
        team_source = {"team_title": "T", "ds_facebook": "false"}

        with (
            mock.patch.object(svc, "get_document_by_id", return_value=team_source),
            mock.patch.object(svc, "_resolve_fb_token_health") as mock_th,
        ):
            payload = svc.get_team_connectors("t")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert "token_health" not in fb
        assert not mock_th.called

    # follow-up: wizard-cohort teams run in account_wide mode and
    # carry fb_ad_account_esid (not team_facebook_page_esids). The card
    # must fall back to the ad-account shape so the wizard's wired asset
    # is visible instead of misleading "needs attention".

    def test_facebook_fallback_resolves_via_fb_ad_account(self):
        """Account_wide wizard team — no team_facebook_page_esids, but
        fb_ad_account_esid is populated. Card resolves via the fallback.
        also IG-configured so the merged card reports configured."""
        team_source = {
            "team_title": "Wizard Team",
            "ds_facebook": "true",
            "fb_ad_account_esid": "ad_acct_1",
            # NOTE: no team_facebook_page_esids — the wizard doesn't set it.
            "ds_instagram": "true",
            "team_instagram_profile_esids": ["ig_a"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_ad_accounts" and doc_id == "ad_acct_1":
                return {"fb_ad_account_title": "Wizard Ad Account"}
            if index == "de_asset_ig_accounts" and doc_id == "ig_a":
                return {"ig_account_title": "IG A"}
            return None

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_w")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "configured"
        assert fb["configured"] is True
        assert [a["title"] for a in fb["linked_assets"]] == ["Wizard Ad Account"]

    def test_facebook_primary_wins_over_fallback(self):
        """When BOTH team_facebook_page_esids AND fb_ad_account_esid are
        populated (legacy page_filtered team that later got a wizard
        re-link, hypothetical), the primary page resolution wins. The
        fallback only fires when the primary returned nothing."""
        team_source = {
            "team_title": "Mixed Team",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["page_a"],
            "fb_ad_account_esid": "ad_acct_1",
            "ds_instagram": "true",
            "team_instagram_profile_esids": ["ig_a"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_pages" and doc_id == "page_a":
                return {"fb_page_name": "Page A"}
            if index == "de_asset_fb_ad_accounts" and doc_id == "ad_acct_1":
                return {"fb_ad_account_title": "Ad Account"}
            if index == "de_asset_ig_accounts" and doc_id == "ig_a":
                return {"ig_account_title": "IG A"}
            return None

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_m")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "configured"
        assert [a["title"] for a in fb["linked_assets"]] == ["Page A"], (
            "Primary page resolution must win — the fallback fires only " "when the primary returned nothing"
        )

    def test_facebook_needs_setup_when_both_primary_and_fallback_fail(self):
        """ds_facebook=true, no team_facebook_page_esids, no fb_ad_account_esid
        — the team has nothing linked. this is ``needs_setup``."""
        team_source = {
            "team_title": "Broken Team",
            "ds_facebook": "true",
            # neither field populated
        }

        def get_by_id(index, doc_id):
            return team_source if index == "de_asset_teams" else None

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_b")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "needs_setup"
        assert fb["linked_assets"] == []

    def test_payload_contains_all_connector_types(self):
        team_source = {"team_title": "T"}
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            payload = svc.get_team_connectors("t")
        keys = {c["key"] for c in payload["connectors"]}
        # Every connector definition should appear in the output.
        assert keys == {c["key"] for c in svc.CONNECTORS}


# ---------------------------------------------------------------------------
# FB-only is configured; states split into needs_setup / disconnected
# ---------------------------------------------------------------------------


class TestKan992ConnectorStates:
    def test_facebook_only_is_configured_not_needs_attention(self):
        """a team with Facebook Pages linked and NO Instagram is a
        valid, fully-configured Meta connector. earlier it was wrongly
        downgraded to the amber "Needs attention" pill."""
        team_source = {
            "team_title": "FB Only Team",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["page_a"],
            "ds_instagram": "false",  # explicitly FB-only
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_pages" and doc_id == "page_a":
                return {"fb_page_name": "Page A"}
            return None

        with (
            mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id),
            mock.patch.object(svc, "_resolve_fb_token_health", return_value={"status": "healthy"}),
        ):
            payload = svc.get_team_connectors("team_fb")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "configured", "FB-only must be configured, not needs_setup"
        assert fb["configured"] is True
        assert fb["ig_configured"] is False
        assert [a["title"] for a in fb["linked_assets"]] == ["Page A"]

    def test_instagram_enabled_but_dangling_is_needs_setup(self):
        """IG explicitly ON but its ESIDs don't resolve is a real
        dangling link → needs_setup (not configured, not the old misconfigured)."""
        team_source = {
            "team_title": "IG Dangling Team",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["page_a"],
            "ds_instagram": "true",
            "team_instagram_profile_esids": ["dangling_ig"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_pages" and doc_id == "page_a":
                return {"fb_page_name": "Page A"}
            return None  # IG lookup fails

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_ig")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "needs_setup"

    def test_account_wide_fb_with_ds_instagram_true_no_ig_esids_is_configured(self):
        """follow-up (regression, found 2026-06-15 on the real
        'Data Connector Test' team) — the account_wide FB wizard cohort writes
        ``ds_instagram=true`` with NO ``team_instagram_profile_esids`` and links
        a ``fb_ad_account_esid`` (no pages). The IG flag is on but there are no
        IG ESIDs to resolve — that is 'IG never wired', NOT a dangling link, so
        the card must stay ``configured`` (resolved via the FB ad-account
        fallback). The first cut of the IG branch keyed on ``not
        ig_linked_assets`` alone and wrongly downgraded this to needs_setup."""
        team_source = {
            "team_title": "Account Wide Team",
            "ds_facebook": "true",
            "fb_import_mode": "account_wide",
            "fb_ad_account_esid": "ad_acct_1",
            # NO team_facebook_page_esids, NO team_instagram_profile_esids
            "ds_instagram": "true",
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_fb_ad_accounts" and doc_id == "ad_acct_1":
                return {"fb_ad_account_title": "Acct One"}
            return None

        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_aw")

        fb = next(c for c in payload["connectors"] if c["key"] == "meta_ads")
        assert fb["state"] == "configured", (
            "account_wide FB with ds_instagram=true but no IG ESIDs must stay " "configured (IG never wired != dangling link)"
        )
        assert fb["configured"] is True
        assert [a["title"] for a in fb["linked_assets"]] == ["Acct One"]
        assert fb["ig_configured"] is False

    def test_revoked_connection_is_disconnected(self):
        """a YouTube/GA4/Google-Ads team whose asset resolves but whose
        de_connection is gone gets the distinct ``disconnected`` state (was the
        generic misconfigured), so the card can offer reconnect/remove."""
        team_source = {
            "team_title": "Revoked YT Team",
            "ds_youtube": "true",
            "team_youtube_channel_handle_esids": ["chan_a"],
        }

        def get_by_id(index, doc_id):
            if index == "de_asset_teams":
                return team_source
            if index == "de_asset_youtube_channels" and doc_id == "chan_a":
                return {"youtube_channel_handle": "@chan"}
            return None

        # No live connection resolves → connection_esid stays None → disconnected.
        with mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id):
            payload = svc.get_team_connectors("team_yt")

        yt = next(c for c in payload["connectors"] if c["key"] == "youtube")
        assert yt["state"] == "disconnected"
        assert yt["configured"] is False


# ---------------------------------------------------------------------------
# disconnect_connector
# ---------------------------------------------------------------------------


class TestKan992Disconnect:
    def test_disconnect_clears_flag_and_esid_returns_not_configured(self):
        """disconnecting clears ds_* + the esid field; the recomputed
        card reads not_configured."""
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_youtube": "true",
            "team_youtube_channel_handle_esids": ["chan_a"],
        }
        captured = {}

        def get_by_id(index, doc_id):
            return dict(team_source) if index == "de_asset_teams" else None

        def fake_update(index, doc_id, document):
            captured["doc"] = document
            # Mutate the source so the post-update recompute sees the cleared fields.
            team_source.update(document)
            return True

        with (
            mock.patch.object(svc, "get_document_by_id", side_effect=get_by_id),
            mock.patch("asset_management.web.services.asset_service.AssetService.update", side_effect=fake_update),
        ):
            payload = svc.disconnect_connector("t", "youtube", allowed_workspaces=None)

        assert captured["doc"]["ds_youtube"] == "false"
        assert captured["doc"]["team_youtube_channel_handle_esids"] == []
        yt = next(c for c in payload["connectors"] if c["key"] == "youtube")
        assert yt["state"] == "not_configured"

    def test_disconnect_unknown_connector_raises(self):
        with mock.patch.object(svc, "get_document_by_id", return_value={"team_title": "T"}):
            with pytest.raises(svc.ConnectorDisconnectError):
                svc.disconnect_connector("t", "no_such_connector", allowed_workspaces=None)

    def test_disconnect_cross_tenant_denied(self):
        """A scoped caller disconnecting a team outside their workspaces is
        collapsed to 'Team not found' — same shape as get_team_connectors deny."""
        team_source = {"team_title": "T", "workspace_esid": "ws_other", "ds_youtube": "true"}
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            with pytest.raises(svc.ConnectorDisconnectError, match="Team not found"):
                svc.disconnect_connector("t", "youtube", allowed_workspaces=["ws_mine"])

    def test_disconnect_meta_clears_all_meta_fields(self):
        """Meta disconnect clears the FB flag+field, the ad-account
        fallback field, AND the Instagram flag+field in one shot."""
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_facebook": "true",
            "team_facebook_page_esids": ["page_a"],
            "fb_ad_account_esid": "acct_a",
            "ds_instagram": "true",
            "team_instagram_profile_esids": ["ig_a"],
        }
        captured = {}

        def fake_update(index, doc_id, document):
            captured["doc"] = document
            team_source.update(document)
            return True

        with (
            mock.patch.object(svc, "get_document_by_id", return_value=dict(team_source)),
            mock.patch("asset_management.web.services.asset_service.AssetService.update", side_effect=fake_update),
        ):
            svc.disconnect_connector("t", "meta_ads", allowed_workspaces=["ws_1"])

        d = captured["doc"]
        assert d["ds_facebook"] == "false"
        assert d["ds_instagram"] == "false"
        assert d["team_facebook_page_esids"] == []
        assert d["fb_ad_account_esid"] == ""
        assert d["team_instagram_profile_esids"] == []


# ---------------------------------------------------------------------------
# remove_connector_account (per-account removal, Google Ads)
# ---------------------------------------------------------------------------


class TestKan1106RemoveAccount:
    def _run(self, team_source, account_esid, allowed_workspaces=None):
        """Call remove_connector_account against a mocked team doc; return the
        captured patch written to ES (or None if no write happened)."""
        captured = {}

        def fake_update(index, doc_id, document):
            captured["doc"] = document
            team_source.update(document)
            return True

        with (
            mock.patch.object(svc, "get_document_by_id", return_value=dict(team_source)),
            mock.patch("asset_management.web.services.asset_service.AssetService.update", side_effect=fake_update),
        ):
            svc.remove_connector_account("t", "google_ads", account_esid, allowed_workspaces=allowed_workspaces)
        return captured.get("doc")

    def test_removes_one_keeps_others_flag_stays_on(self):
        """Removing one of several customers shrinks the list; the connector
        stays enabled because accounts remain."""
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_google_ads": "true",
            "team_google_ads_customer_esids": ["cust_a", "cust_b", "cust_c"],
        }
        d = self._run(team_source, "cust_b")
        assert d["team_google_ads_customer_esids"] == ["cust_a", "cust_c"]
        assert d["ds_google_ads"] == "true"

    def test_removing_last_account_turns_flag_off(self):
        """Removing the final customer flips ds_google_ads off (the card
        returns to not-configured)."""
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_google_ads": "true",
            "team_google_ads_customer_esids": ["cust_only"],
        }
        d = self._run(team_source, "cust_only")
        assert d["team_google_ads_customer_esids"] == []
        assert d["ds_google_ads"] == "false"

    def test_removes_legacy_scalar_when_it_matches(self):
        """A earlier team may carry only the legacy scalar; removing that
        account clears the scalar and leaves an empty list + flag off."""
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_google_ads": "true",
            "team_google_ads_customer_esids": [],
            "google_ads_customer_esid": "cust_legacy",
        }
        d = self._run(team_source, "cust_legacy")
        assert d["team_google_ads_customer_esids"] == []
        assert d["google_ads_customer_esid"] == ""
        assert d["ds_google_ads"] == "false"

    def test_account_not_linked_raises(self):
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_google_ads": "true",
            "team_google_ads_customer_esids": ["cust_a"],
        }
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            with pytest.raises(svc.ConnectorDisconnectError, match="not linked"):
                svc.remove_connector_account("t", "google_ads", "cust_missing", allowed_workspaces=None)

    def test_cross_tenant_denied(self):
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_other",
            "ds_google_ads": "true",
            "team_google_ads_customer_esids": ["cust_a"],
        }
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            with pytest.raises(svc.ConnectorDisconnectError, match="Team not found"):
                svc.remove_connector_account("t", "google_ads", "cust_a", allowed_workspaces=["ws_mine"])

    def test_single_value_connector_rejected(self):
        """Chatwoot has no multi-value account list — per-account removal is
        meaningless there (use disconnect). It must raise rather than silently
        clear the scalar."""
        team_source = {
            "team_title": "T",
            "workspace_esid": "ws_1",
            "ds_chatwoot": "true",
            "chatwoot_account_esid": "acct_a",
        }
        with mock.patch.object(svc, "get_document_by_id", return_value=team_source):
            with pytest.raises(svc.ConnectorDisconnectError, match="per-account removal"):
                svc.remove_connector_account("t", "chatwoot", "acct_a", allowed_workspaces=None)


# ---------------------------------------------------------------------------
# _resolve_fb_token_health
# ---------------------------------------------------------------------------


class TestResolveFbTokenHealth:
    def test_returns_none_when_no_pages(self):
        assert svc._resolve_fb_token_health([]) is None

    def test_returns_none_when_page_has_no_account(self):
        with mock.patch.object(svc, "get_document_by_id", return_value={"fb_page_name": "X"}):  # no fb_account_esid
            result = svc._resolve_fb_token_health([{"esid": "page_a"}])
        assert result is None

    def _run(self, expires_at):
        """Helper: run the function with a single app + user, returning the
        token-health payload. ``expires_at`` is the unix-timestamp value."""
        es = mock.MagicMock()
        es.search.side_effect = [
            {"hits": {"hits": [{"_id": "app_x"}]}},
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "user_x",
                            "_source": {
                                "fb_app_user_title": "Bot",
                                "fb_app_user_token": "encrypted",
                                "fb_app_user_token_exp": expires_at,
                                "fb_app_esid": "app_x",
                                "fb_app_user_auth_url": "abc123",
                            },
                        }
                    ]
                }
            },
        ]
        with (
            mock.patch.object(svc, "get_document_by_id", return_value={"fb_account_esid": "acct1"}),
            mock.patch.object(svc, "get_elasticsearch_client", return_value=es),
            mock.patch.object(svc, "_get_fboauth_hostname", return_value="fboauth.example.org"),
        ):
            return svc._resolve_fb_token_health([{"esid": "page_a"}])

    def test_status_healthy_when_far_from_expiry(self):
        from datetime import datetime, timezone

        future = int(datetime.now(timezone.utc).timestamp()) + 60 * 86400
        result = self._run(future)
        assert result["status"] == "healthy"
        assert result["expires_in_days"] >= 59
        assert result["app_user_title"] == "Bot"
        assert result["oauth_refresh_url"] == "https://fboauth.example.org/app_x/abc123"

    def test_status_expiring_soon_within_two_weeks(self):
        from datetime import datetime, timezone

        soon = int(datetime.now(timezone.utc).timestamp()) + 5 * 86400
        result = self._run(soon)
        assert result["status"] == "expiring_soon"

    def test_status_expired_when_past(self):
        from datetime import datetime, timezone

        past = int(datetime.now(timezone.utc).timestamp()) - 5 * 86400
        result = self._run(past)
        assert result["status"] == "expired"

    def test_status_unknown_when_no_expiry(self):
        result = self._run(None)
        assert result["status"] == "unknown"
        assert result["expires_in_days"] is None

    def test_status_missing_when_no_user_has_token(self):
        es = mock.MagicMock()
        es.search.side_effect = [
            {"hits": {"hits": [{"_id": "app_x"}]}},
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "user_x",
                            "_source": {"fb_app_user_title": "Bot"},  # no token
                        }
                    ]
                }
            },
        ]
        with (
            mock.patch.object(svc, "get_document_by_id", return_value={"fb_account_esid": "acct1"}),
            mock.patch.object(svc, "get_elasticsearch_client", return_value=es),
        ):
            result = svc._resolve_fb_token_health([{"esid": "page_a"}])
        assert result == {"status": "missing"}

    def test_picks_user_with_latest_expiry_when_multiple(self):
        from datetime import datetime, timezone

        later = int(datetime.now(timezone.utc).timestamp()) + 100 * 86400
        earlier = int(datetime.now(timezone.utc).timestamp()) + 10 * 86400
        es = mock.MagicMock()
        es.search.side_effect = [
            {"hits": {"hits": [{"_id": "app_x"}]}},
            {
                "hits": {
                    "hits": [
                        {
                            "_id": "user_old",
                            "_source": {
                                "fb_app_user_title": "Old",
                                "fb_app_user_token": "x",
                                "fb_app_user_token_exp": earlier,
                                "fb_app_esid": "app_x",
                                "fb_app_user_auth_url": "old",
                            },
                        },
                        {
                            "_id": "user_new",
                            "_source": {
                                "fb_app_user_title": "New",
                                "fb_app_user_token": "y",
                                "fb_app_user_token_exp": later,
                                "fb_app_esid": "app_x",
                                "fb_app_user_auth_url": "new",
                            },
                        },
                    ]
                }
            },
        ]
        with (
            mock.patch.object(svc, "get_document_by_id", return_value={"fb_account_esid": "acct1"}),
            mock.patch.object(svc, "get_elasticsearch_client", return_value=es),
            mock.patch.object(svc, "_get_fboauth_hostname", return_value="fboauth.example.org"),
        ):
            result = svc._resolve_fb_token_health([{"esid": "page_a"}])
        assert result["app_user_title"] == "New"

    def test_returns_none_when_no_apps_for_account(self):
        es = mock.MagicMock()
        es.search.side_effect = [{"hits": {"hits": []}}]
        with (
            mock.patch.object(svc, "get_document_by_id", return_value={"fb_account_esid": "acct1"}),
            mock.patch.object(svc, "get_elasticsearch_client", return_value=es),
        ):
            result = svc._resolve_fb_token_health([{"esid": "page_a"}])
        assert result is None

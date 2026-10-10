"""
Tests for ConnectionService.revoked_connection_exists_for_legacy_asset.

This method is the revoke short-circuit signal for importers that read a legacy
token directly (YouTube). It must distinguish three states:
  * no de_connection_asset row          → never migrated → False (fail-open)
  * row(s) exist, all connections dead  → revoked        → True  (skip import)
  * row(s) exist, at least one is live  → still authorized → False
"""

from unittest import mock

from asset_management.web.services.connection_service import ConnectionService

_CS = "asset_management.web.services.connection_service"


def _es_returning(hits):
    es = mock.MagicMock()
    es.search.return_value = {"hits": {"hits": hits}}
    return es


class TestRevokedConnectionExistsForLegacyAsset:

    def test_false_when_no_connection_asset_row(self):
        # Pure-legacy account, never migrated → fail-open so it keeps importing.
        es = _es_returning([])
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es):
            result = ConnectionService.revoked_connection_exists_for_legacy_asset(
                "de_asset_youtube_accounts", "acc-1"
            )
        assert result is False

    def test_true_when_row_exists_and_connection_revoked(self):
        es = _es_returning([{"_source": {"connection_esid": "conn-1"}}])
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es), \
             mock.patch.object(ConnectionService, "is_connection_live", return_value=False):
            result = ConnectionService.revoked_connection_exists_for_legacy_asset(
                "de_asset_youtube_accounts", "acc-1"
            )
        assert result is True

    def test_false_when_row_exists_and_connection_live(self):
        es = _es_returning([{"_source": {"connection_esid": "conn-1"}}])
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es), \
             mock.patch.object(ConnectionService, "is_connection_live", return_value=True):
            result = ConnectionService.revoked_connection_exists_for_legacy_asset(
                "de_asset_youtube_accounts", "acc-1"
            )
        assert result is False

    def test_false_when_one_of_several_is_live(self):
        # Multiple linked rows; a single live connection means still authorized.
        es = _es_returning([
            {"_source": {"connection_esid": "dead"}},
            {"_source": {"connection_esid": "live"}},
        ])

        def _live(esid):
            return esid == "live"

        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es), \
             mock.patch.object(ConnectionService, "is_connection_live", side_effect=_live):
            result = ConnectionService.revoked_connection_exists_for_legacy_asset(
                "de_asset_youtube_accounts", "acc-1"
            )
        assert result is False

    def test_false_on_missing_args(self):
        assert ConnectionService.revoked_connection_exists_for_legacy_asset("", "acc-1") is False
        assert ConnectionService.revoked_connection_exists_for_legacy_asset("idx", "") is False

    def test_false_when_es_unavailable(self):
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=None):
            result = ConnectionService.revoked_connection_exists_for_legacy_asset(
                "de_asset_youtube_accounts", "acc-1"
            )
        assert result is False

    def test_false_when_search_raises(self):
        es = mock.MagicMock()
        es.search.side_effect = Exception("ES down")
        with mock.patch(f"{_CS}.get_elasticsearch_client", return_value=es):
            result = ConnectionService.revoked_connection_exists_for_legacy_asset(
                "de_asset_youtube_accounts", "acc-1"
            )
        assert result is False  # fail-open

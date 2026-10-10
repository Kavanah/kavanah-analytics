"""GA4 is OAuth-only; the service account is retired.

 made GA4 auth OAuth-first with a service-account fallback, but the SA
client was still built unconditionally at startup — so a missing SA file
(``ga_service_account.json``) aborted the whole import with
``FileNotFoundError`` even when every property used OAuth. removes the
SA entirely: GA4 resolves an OAuth client per property and SKIPS any property
whose team has no GA4 OAuth connection (mirrors the YouTube importer's
skip-if-no-token behaviour).

These are behaviour pins (no live Google/ES):
  1. ``GAConfig.validate_config`` no longer raises on a missing SA file.
  2. ``_ga4_data_client_for_property`` returns None (→ skip) when there is no
     OAuth connection, and never references a service-account client.
  3. ``get_ga_metrics.main`` skips an unconnected property instead of saving /
     raising.

Mutation-verified locally:
  - Make validate_config raise FileNotFoundError again → test_validate fails.
  - Make _ga4_data_client_for_property return a client on the no-OAuth path →
    test_skips_unconnected fails (save called / no skip).
"""

from __future__ import annotations

import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bin", "python"))

_GA_METRICS = "import_scripts.google_analytics.get_ga_metrics"


def test_validate_config_does_not_require_service_account(tmp_path):
    """A config referencing a nonexistent service-account file must NOT block
    loading — the SA is retired, auth is per-property OAuth.

     Part B strengthened this pin: ``validate_config`` and the
    ``credentials_file`` field are GONE (they were dead code from the retired
    SA path), so the guarantee is now structural — GAConfig loads whether or
    not the YAML carries the stale key, and no attribute exposes it.
    """
    import yaml

    from import_scripts.google_analytics.lib.ga_config import GAConfig

    cfg = tmp_path / "google-analytics.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "credentials_file": "/app/data/google_analytics/ga_service_account.json",  # does NOT exist
                "days_to_pull": 7,
            }
        )
    )

    config = GAConfig(config_path=str(cfg))
    assert config.days_to_pull == 7
    # the SA fields no longer exist at all; nothing can read the
    # stale key back into a code path.
    assert not hasattr(config, "credentials_file")
    assert not hasattr(config, "validate_config")


def test_no_oauth_connection_returns_none_not_sa():
    """_ga4_data_client_for_property returns None (caller skips) when no OAuth
    connection resolves — there is no service-account client to fall back to."""
    from import_scripts.google_analytics import get_ga_metrics as gm

    cache: dict = {}
    with mock.patch.object(gm, "resolve_ga4_credentials", return_value=None):
        client = gm._ga4_data_client_for_property("999", cache)

    assert client is None, "an unconnected property must resolve no client (skip), not an SA client"
    assert cache == {}


def test_main_skips_unconnected_property_without_raising():
    """get_ga_metrics.main skips a property with no OAuth connection: no save,
    no raise, clean completion."""
    from import_scripts.google_analytics import get_ga_metrics as gm

    config = mock.MagicMock()
    config.days_to_pull = 7
    config.report_limit = 1000
    config.property_team_mapping = {}
    config.get_rate_limit_config.return_value = {"max_retries": 3, "log_usage": False}

    props = [{"property_id": "123", "property_name": "Unconnected Site", "team_metadata": {"team_esid": "t1", "team_title": "Team A"}}]

    with mock.patch.object(gm, "GAConfig", return_value=config), mock.patch.object(
        gm, "resolve_ga4_credentials", return_value=None
    ), mock.patch.object(gm, "load_properties_from_assets", return_value=props), mock.patch.object(
        gm, "fetch_property_data"
    ) as mock_fetch, mock.patch.object(
        gm, "save_property_json"
    ) as mock_save, mock.patch.object(
        gm, "write_no_data_markers"
    ):
        gm.main({"property_ids": "123"})  # must not raise

    mock_fetch.assert_not_called()  # skipped before fetch
    mock_save.assert_not_called()


# ── GA4 connector-validation probe is OAuth-only too ──────────────


def _make_creds():
    return mock.MagicMock()


# The probe imports the heavy google.analytics / google.api_core libs lazily;
# they ship in the container but may be absent in a bare local env. Skip the
# live-path probe tests there (the import-guard fail-open is exercised by
# test_probe_fail_open_when_no_connected_accounts via the libs being
# present in CI; locally without the libs the guard returns fail-open, which is
# itself correct but not what these two assert).
import pytest  # noqa: E402

try:
    import google.api_core  # noqa: F401
    import google.analytics.data_v1beta  # noqa: F401

    _GA_LIBS = True
except Exception:  # noqa: BLE001
    _GA_LIBS = False


@pytest.mark.skipif(not _GA_LIBS, reason="google.analytics/api_core not installed in this env")
def test_probe_ok_when_a_connected_account_can_read():
    """_ga4_probe returns ok=True when any connected GA4 OAuth account can read
    the property's metadata — no service account involved."""
    from asset_management.web.services import connector_validation_service as cvs

    client = mock.MagicMock()  # get_metadata succeeds (no raise)
    with mock.patch(
        "import_scripts.google_analytics.lib.ga_config.iter_connected_ga4_accounts",
        return_value=iter([({"ga4_account_title": "A"}, _make_creds())]),
    ), mock.patch("google.analytics.data_v1beta.BetaAnalyticsDataClient", return_value=client):
        result = cvs._ga4_probe("123456789")

    assert result.ok is True


@pytest.mark.skipif(not _GA_LIBS, reason="google.analytics/api_core not installed in this env")
def test_probe_fail_open_when_no_connected_accounts():
    """With no connected GA4 OAuth accounts, the probe can't prove anything and
    fails OPEN (definitive=False) so setup isn't blocked — and it never touches
    a service account."""
    from asset_management.web.services import connector_validation_service as cvs

    with mock.patch(
        "import_scripts.google_analytics.lib.ga_config.iter_connected_ga4_accounts",
        return_value=iter([]),
    ):
        result = cvs._ga4_probe("123456789")

    assert result.ok is False
    assert result.definitive is False  # fail-open, not a rejection

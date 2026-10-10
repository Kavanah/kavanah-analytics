"""
Pipeline test configuration.

Pre-patches external SDK sys.modules entries before any pipeline test module
is imported. This makes pipeline tests runnable without having the Facebook,
Google Ads, or Google API client packages installed in the test environment.
"""

import sys
from unittest import mock

# ---------------------------------------------------------------------------
# SDK shims — applied once before any pipeline test module is collected
# ---------------------------------------------------------------------------
# GoogleAdsException must inherit from Exception so `except GoogleAdsException`
# works in production code — a plain MagicMock cannot be caught.
class _GoogleAdsException(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.failure = mock.MagicMock()

_google_ads_errors_mod = mock.MagicMock()
_google_ads_errors_mod.GoogleAdsException = _GoogleAdsException

# HttpError must inherit from Exception so `except HttpError` works.
class _HttpError(Exception):
    def __init__(self, *args, content=b'{"error":{}}', **kwargs):
        super().__init__(*args)
        self.content = content

_googleapiclient_errors_mod = mock.MagicMock()
_googleapiclient_errors_mod.HttpError = _HttpError

# RefreshError must inherit from Exception so `except RefreshError` works in
# get_ga_metrics' OAuth-token revocation-detection path.
class _RefreshError(Exception):
    pass

_google_auth_exceptions_mod = mock.MagicMock()
_google_auth_exceptions_mod.RefreshError = _RefreshError

# FacebookRequestError must inherit from Exception too: importer code does
# ``except FacebookRequestError`` (get_insights_window.py, get_page_insights.py,
# ...). This shim and the one in the release-plan tests both
# sys.modules.setdefault the same key, so whichever directory pytest collects
# FIRST wins for the whole session — pipelines sorts before release_plans, so a
# bare MagicMock here used to poison the release-plan window tests
# ("catching classes that do not inherit from BaseException")
# in full-suite runs only. Keep this class shape in sync with
# the release-plan tests's _FakeFacebookRequestError.
class _FakeFacebookRequestError(Exception):
    def __init__(self, *args, api_error_code=None, api_error_message=None, **kwargs):
        super().__init__(*args)
        self._api_error_code = api_error_code
        # the code-100 page-detail handler reports
        # api_error_message() instead of dumping the exception. Defaults to
        # str(args[0]) so callers that only pass a message still work.
        # Added here as well as in release_plans/conftest.py: in a full-suite
        # run pipelines collects FIRST and wins the setdefault, so a method
        # present in only one shim is invisible until CI (which is exactly how
        # PAGE-GRANT-MSG-001 first went red).
        self._api_error_message = api_error_message if api_error_message is not None else (str(args[0]) if args else "")

    def api_error_code(self):
        return self._api_error_code

    def api_error_message(self):
        return self._api_error_message

_fb_exceptions_mod = mock.MagicMock()
_fb_exceptions_mod.FacebookRequestError = _FakeFacebookRequestError

_SHIMS = {
    # Facebook Business SDK
    "facebook_business": mock.MagicMock(),
    "facebook_business.api": mock.MagicMock(),
    "facebook_business.exceptions": _fb_exceptions_mod,
    "facebook_business.adobjects": mock.MagicMock(),
    "facebook_business.adobjects.adaccount": mock.MagicMock(),
    "facebook_business.adobjects.ad": mock.MagicMock(),
    "facebook_business.adobjects.adcreative": mock.MagicMock(),
    "facebook_business.adobjects.page": mock.MagicMock(),
    "facebook_business.adobjects.adset": mock.MagicMock(),
    "facebook_business.adobjects.campaign": mock.MagicMock(),
    "facebook_business.adobjects.iguser": mock.MagicMock(),
    "facebook_business.adobjects.igmedia": mock.MagicMock(),
    # Google Ads SDK
    "google": mock.MagicMock(),
    "google.ads": mock.MagicMock(),
    "google.ads.googleads": mock.MagicMock(),
    "google.ads.googleads.client": mock.MagicMock(),
    "google.ads.googleads.errors": _google_ads_errors_mod,
    # Google OAuth / API client
    "google.oauth2": mock.MagicMock(),
    "google.oauth2.credentials": mock.MagicMock(),
    "google.oauth2.service_account": mock.MagicMock(),
    "google.auth": mock.MagicMock(),
    "google.auth.exceptions": _google_auth_exceptions_mod,
    "googleapiclient": mock.MagicMock(),
    "googleapiclient.discovery": mock.MagicMock(),
    "googleapiclient.errors": _googleapiclient_errors_mod,
    # Google Analytics Data API (GA4)
    "google.analytics": mock.MagicMock(),
    "google.analytics.data_v1beta": mock.MagicMock(),
    "google.analytics.data_v1beta.types": mock.MagicMock(),
}

for _name, _mod in _SHIMS.items():
    sys.modules.setdefault(_name, _mod)


# ---------------------------------------------------------------------------
# Importer-lock autouse stub
# ---------------------------------------------------------------------------
# added a cluster-level lock at the top of every importer's
# main(): ImportLockService.acquire(connector) is called before any subprocess.
# In CI there is no Elasticsearch, so the real acquire() lands in its broad
# except, returns None, and main() takes the "another importer holds the lock;
# exiting cleanly" branch BEFORE the patched subprocess.run is reached — the
# subprocess assertions then see an empty call_args_list.
#
# The release-plan stubs in the release-plan tests already patch
# lib.import_lock_service.ImportLockService.acquire / .release per-test; this
# autouse fixture extends the same stub to every test under tests/pipelines/
# so the older TestDailyMain / TestGaDailyMain cases (written before the lock
# landed) work without per-test boilerplate.
import pytest


@pytest.fixture(autouse=True)
def _stub_importer_lock():
    with mock.patch(
        "lib.import_lock_service.ImportLockService.acquire",
        return_value="test-lock-id",
    ), mock.patch("lib.import_lock_service.ImportLockService.release"):
        yield

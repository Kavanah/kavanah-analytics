"""Shared fixtures for the Flask route tests.

 introduced two new multi-tenant authz gates in ``app.py``:

  * ``_company_tier_asset_in_user_scope`` — a per-object scope check on the
    company-tier (teams / organizations) direct-object routes (edit / delete /
    check-references / bulk-delete / details).
  * ``_scope_filter_asset_hits`` — scopes the ``/api/options/<index>`` results
    to the caller.

A large body of pre-existing CRUD / options tests assert orthogonal behavior
(response shape, Kibana side-effects, per-item error handling) and were written
before these gates existed; under a unit-test session with no ES backend the
gates resolve to deny-all and those tests now 404. The
``allow_company_tier_and_options_scope`` fixture below lets such a test opt back
into the success path by neutralizing ONLY the two new gates — it does not touch
any other authz. The gates themselves are covered by
``the release-plan tests``.

Opt in per-module with::

    pytestmark = pytest.mark.usefixtures("allow_company_tier_and_options_scope")
"""

from unittest import mock

import pytest


@pytest.fixture
def allow_company_tier_and_options_scope():
    """Neutralize the company-tier + /api/options scope gates so tests
    that exercise orthogonal CRUD/options behavior reach their target path.

    Also provides a default ``AssetService.get_by_id`` that returns a minimal
    doc: the company-tier delete/bulk-delete/check-references gates now LOAD the
    doc (``if not existing_item: 404``) before the scope check, so a test that
    didn't previously mock the load would 404 on a None. A test that patches
    ``get_by_id`` itself overrides this default within its own ``with`` block."""
    with (
        mock.patch("asset_management.web.app._company_tier_asset_in_user_scope", return_value=True),
        mock.patch("asset_management.web.app._scope_filter_asset_hits", side_effect=lambda asset_type, hits: hits),
        mock.patch("asset_management.web.app.AssetService.get_by_id", return_value={"team_title": "T"}),
    ):
        yield


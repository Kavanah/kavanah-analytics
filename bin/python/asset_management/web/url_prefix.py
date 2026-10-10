"""Prefix-safe URL building for /assets-mounted routes.

The DEAM app is mounted behind nginx at ``/assets`` (PrefixMiddleware sets
``SCRIPT_NAME=/assets``), but several surfaces are deliberately reachable at
the BARE path — ``/billing``, ``/profile``, ``/dashboard`` are proxied
directly and whitelisted in ``PrefixMiddleware.allowed_paths``. On those
pages ``SCRIPT_NAME`` is empty, so a plain ``url_for`` to a route mounted
only under ``/assets`` emits a bare path that 404s.

Before this module the fix was hand-hardcoding ``/assets/...`` (in
the dashboard checklist in the Billing section) — correct in prod,
wrong in local dev (``FLASK_USE_PREFIX=false``, where the app serves bare),
and a fresh latent 404 every time a new link is added to a bare-path surface.

Two entry points:

* ``assets_url(endpoint, **values)`` — Jinja global (registered in app.py):
  ``url_for`` plus a guaranteed prefix. Use it in templates.
* ``assets_path(path)`` — plain-string variant for module-import-time
  constants (e.g. the dashboard checklist's ``action_url`` rows), where no
  request context exists for ``url_for``.
"""

from __future__ import annotations

import os

ASSETS_PREFIX = "/assets"


def use_prefix() -> bool:
    """Whether this process serves behind the /assets prefix.

    Reads the same env switch app.py keys PrefixMiddleware on, so the two can
    never disagree.
    """
    return os.environ.get("FLASK_USE_PREFIX", "true").lower() == "true"


def assets_path(path: str) -> str:
    """Absolute browser path for an /assets-mounted route, prefix-aware.

    ``assets_path("/data-connectors/")`` → ``/assets/data-connectors/`` in
    prod, ``/data-connectors/`` in bare local dev. Idempotent: an already
    prefixed path is returned unchanged.
    """
    if not path.startswith("/"):
        path = "/" + path
    if not use_prefix():
        return path
    if path == ASSETS_PREFIX or path.startswith(ASSETS_PREFIX + "/"):
        return path
    return ASSETS_PREFIX + path


def assets_url(endpoint: str, **values) -> str:
    """``url_for`` that always carries the /assets prefix when configured.

    On a prefixed request ``url_for`` already includes ``SCRIPT_NAME`` and the
    result passes through untouched; on a bare-path surface the prefix is
    added. Registered as a Jinja global in app.py.
    """
    from flask import url_for

    return assets_path(url_for(endpoint, **values))

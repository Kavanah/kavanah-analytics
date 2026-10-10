"""Production-hostname guard shared by every tool that must refuse prod.

Four seed/capacity tools and the live security suite each carried their own copy
of the prod hostname tuple. One list, one place, and the deployment-specific
part (legacy aliases, a second prod) comes from the environment instead of the
tree, so the public snapshot carries only the product's public domain.

    PROD_HOSTNAMES   comma-separated extra hostnames treated as production
                     (blank/unset = the built-in default only)

Matching is a case-insensitive substring test, exactly as the guards always did:
``is_prod_host("https://kavanahanalytics.com/assets/")`` is True.
"""

from __future__ import annotations

import os

DEFAULT_PROD_HOSTNAMES: tuple[str, ...] = ("kavanahanalytics.com",)
_ENV = "PROD_HOSTNAMES"


def prod_hostnames() -> tuple[str, ...]:
    """Built-in prod hostnames plus any from ``PROD_HOSTNAMES`` (read at call time)."""
    extra = tuple(h.strip().lower() for h in os.environ.get(_ENV, "").split(",") if h.strip())
    return DEFAULT_PROD_HOSTNAMES + extra


def is_prod_host(value: str) -> bool:
    """True when ``value`` (a hostname or URL) names a production host."""
    lower = (value or "").lower()
    return any(h in lower for h in prod_hostnames())

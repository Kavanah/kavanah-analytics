"""Header-auth test persona identities.

The ``X-Test-Persona`` header-auth path (web/auth.py) and the seed tools
(tools/seed_attack_surface_personas.py, tools/seed_load_test_identities.py)
must agree on the persona USERNAMES: they are real ES Security users on the
deployment that runs the attack-surface and load suites. The local part is
fixed (``test-<slug>`` / ``loadtest-NN``); the domain is deployment-specific
and comes from the environment:

    TEST_PERSONA_DOMAIN   domain of the persona usernames
                          (blank/unset = the neutral default ``example.com``)

A deployment whose personas were seeded under another domain sets the variable
on imports_container (deployment/.env -> compose) and exports it before running
the seed tools; the tree itself carries no organisation hostname.
"""

from __future__ import annotations

import os

DEFAULT_PERSONA_DOMAIN = "example.com"
_ENV = "TEST_PERSONA_DOMAIN"


def persona_domain() -> str:
    """Domain for persona usernames; env wins, blank/unset falls back to the neutral default."""
    return (os.environ.get(_ENV) or "").strip() or DEFAULT_PERSONA_DOMAIN


def persona_username(slug: str) -> str:
    """``test-<slug-with-hyphens>@<domain>`` — the convention PERSONAS.md documents."""
    return f"test-{slug.replace('_', '-')}@{persona_domain()}"

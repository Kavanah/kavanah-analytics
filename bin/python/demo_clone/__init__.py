"""Anonymized demo-team clone.

Copies one real team's platform-metric indices (``{esid}__{source}``) into the
demo team's esid-form indices, rewriting every identity-bearing field on the
way so the demo tenant carries realistic numbers and no trace of the source
team, its assets, or the people behind its conversations.

Modules:

* :mod:`demo_clone.policy` — the allowlist: what happens to each field.
* :mod:`demo_clone.transform` — pure document rewriting (no Elasticsearch).
* :mod:`demo_clone.runner` — team resolution, index creation, scan/bulk.

The CLI lives in ``scripts/clone_demo_team_data.py``.
"""


class DemoCloneError(Exception):
    """Base exception for the demo clone package."""


class TeamResolutionError(DemoCloneError):
    """A team title did not resolve to exactly one ``de_asset_teams`` document."""


class CloneRefused(DemoCloneError):
    """A deliberate refusal (unsafe target, missing restore point) — not a crash."""


class SaltMissing(CloneRefused):
    """``DEMO_CLONE_SALT`` is unset, too short, or a retired public value."""

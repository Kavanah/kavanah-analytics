"""Disable a user's MFA from the command line.

The Tier 3 fallback for the case the web UI can't cover: the **sole superuser
is locked out** of the web UI because they lost their own second factor and so
can't reach the admin "Reset MFA" action. This management command resets a
named user's MFA directly against Elasticsearch, following Matomo's
``twofactorauth:disable-2fa-for-user`` precedent.

It removes the target's MFA record (clearing the TOTP secret / YubiKey
credential and any recovery codes / recovery-email state) so the user is forced
to re-enrol on their next login — exactly what ``MFAService.admin_disable_mfa``
does behind the admin UI button. Recovery events stay audit-logged.

``--user`` accepts an email address or a username; an email is resolved to the
ES Security username (ES Security has no email index, so we enumerate users and
match case-insensitively — same approach as the SSO/signup services).

Dry-run by default. Pass ``--apply`` to actually reset.

Usage::

    # Dry-run — report what would be reset, no writes.
    docker exec -w /app/python imports_container python3 \\
        -m tools.disable_mfa --user=alice@example.com

    # Apply (the real reset).
    docker exec -w /app/python imports_container python3 \\
        -m tools.disable_mfa --user=alice@example.com --apply

    # By username instead of email.
    docker exec -w /app/python imports_container python3 \\
        -m tools.disable_mfa --user=alice --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as the other tools/ scripts.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from asset_management.web.services.mfa_service import MFAService  # noqa: E402


def resolve_username(es, identifier: str) -> str | None:
    """Resolve ``identifier`` (an email or a username) to an ES Security
    username. Returns None if no user matches.

    A bare username is accepted as-is when it exists; an ``@``-form is matched
    case-insensitively against stored emails (ES Security has no email index).
    """
    identifier = (identifier or "").strip()
    if not identifier:
        return None

    try:
        users = dict(es.security.get_user())
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logging.error("disable_mfa: es.security.get_user() failed: %s", e)
        return None

    # Exact username match wins (covers the username-with-@ edge too).
    if identifier in users:
        return identifier

    if "@" in identifier:
        wanted = identifier.lower()
        for username, record in users.items():
            stored = (record.get("email") or "").strip().lower()
            if stored and stored == wanted:
                return username
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", required=True, help="Target user's email address or username.")
    parser.add_argument("--apply", action="store_true", help="Actually reset MFA (default: dry-run).")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch unavailable — exiting.")
        return 1

    username = resolve_username(es, args.user)
    if not username:
        logging.error("No user found matching %r — nothing to do.", args.user)
        return 1

    record = MFAService.get_mfa_record(username)
    has_mfa = bool(record and record.get("mfa_enabled"))

    if not args.apply:
        logging.info(
            "DRY-RUN: would reset MFA for %r (mfa_enabled=%s). Re-run with --apply to write.",
            username,
            has_mfa,
        )
        return 0

    result = MFAService.admin_disable_mfa(username)
    if not result.get("success"):
        logging.error("Failed to reset MFA for %r: %s", username, result.get("error"))
        return 1

    logging.info(
        "AUDIT: action=cli_disable_mfa target=%s previously_enabled=%s — MFA reset; user must re-enrol on next login.",
        username,
        has_mfa,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

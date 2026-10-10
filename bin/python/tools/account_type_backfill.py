"""One-shot backfill: stamp ``account_type`` on existing ES Security users.

The entry-point cohort field stores ``account_type`` on each
user's ES Security metadata dict. Users who pre-exist the rollout have
no value set; this script scans every ES user, defaults missing values
to ``org_owner`` (the most common entry point for early users), and
preserves anything already set.

The script is idempotent — every write goes through
``UserProfileService.set_account_type``, which is a metadata-merge over
the existing record. Re-running is safe and produces zero writes
once every user has a stored value.

Usage::

    # Dry-run (default) — log what would be written, write nothing.
    docker exec -w /app/python imports_container python3 -m tools.account_type_backfill

    # Apply the writes.
    docker exec -w /app/python imports_container python3 -m tools.account_type_backfill --apply

    # Use a non-default fallback value (still skips users already set).
    docker exec -w /app/python imports_container python3 \\
        -m tools.account_type_backfill --apply --default individual

    # Skip specific users (e.g. the built-in elastic account).
    docker exec -w /app/python imports_container python3 \\
        -m tools.account_type_backfill --apply --skip elastic,kibana_system
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/connection_backfill.py.
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

from asset_management.web.services.user_profile_service import (  # noqa: E402
    DEFAULT_ACCOUNT_TYPE,
    METADATA_KEY,
    VALID_ACCOUNT_TYPES,
    UserProfileService,
)


def _list_users(es) -> dict[str, dict]:
    """Return every ES Security user as ``{username: user_record}``."""
    response = es.security.get_user()
    return dict(response)


def backfill(
    *,
    apply: bool,
    default_account_type: str,
    skip_usernames: set[str],
) -> dict:
    """Scan every ES Security user, fill in ``account_type`` when absent.

    Returns a dict suitable for printing as the run summary.
    """
    summary: dict[str, int] = {
        "scanned": 0,
        "skipped_reserved": 0,
        "skipped_explicit": 0,
        "already_set": 0,
        "would_update": 0,
        "updated": 0,
        "errors": 0,
    }

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch client unavailable — aborting")
        summary["errors"] = 1
        return summary

    users = _list_users(es)
    summary["scanned"] = len(users)

    for username, user_record in users.items():
        metadata = user_record.get("metadata") or {}

        # Skip ES built-ins (elastic, kibana_system, etc.) — these are
        # platform service accounts, not human users, so a cohort value
        # would only ever be noise in instrumentation.
        if metadata.get("_reserved"):
            summary["skipped_reserved"] += 1
            continue

        if username in skip_usernames:
            summary["skipped_explicit"] += 1
            continue

        stored = metadata.get(METADATA_KEY)
        if stored in VALID_ACCOUNT_TYPES:
            summary["already_set"] += 1
            continue

        if not apply:
            summary["would_update"] += 1
            logging.info(
                "[dry-run] would set account_type=%s for username=%s",
                default_account_type,
                username,
            )
            continue

        result = UserProfileService.set_account_type(username, default_account_type)
        if result.get("success"):
            summary["updated"] += 1
            logging.info(
                "set account_type=%s for username=%s (prev=%s)",
                default_account_type,
                username,
                result.get("old_value") or "<none>",
            )
        else:
            summary["errors"] += 1
            logging.error(
                "failed to set account_type for username=%s: %s",
                username,
                result.get("error"),
            )

    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="account_type backfill")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply writes. Without this flag, the script is a no-op dry-run.",
    )
    parser.add_argument(
        "--default",
        default=DEFAULT_ACCOUNT_TYPE,
        choices=VALID_ACCOUNT_TYPES,
        help=f"Account-type value to stamp on users that have none set (default: {DEFAULT_ACCOUNT_TYPE})",
    )
    parser.add_argument(
        "--skip",
        default="",
        help="Comma-separated usernames to skip in addition to ES built-ins",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    skip_usernames = {u.strip() for u in args.skip.split(",") if u.strip()}

    mode = "APPLY" if args.apply else "DRY-RUN"
    logging.info(
        "account_type backfill (%s) — default=%s skip=%s",
        mode,
        args.default,
        sorted(skip_usernames) or "<none>",
    )

    summary = backfill(
        apply=args.apply,
        default_account_type=args.default,
        skip_usernames=skip_usernames,
    )

    logging.info("Summary: %s", summary)
    return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

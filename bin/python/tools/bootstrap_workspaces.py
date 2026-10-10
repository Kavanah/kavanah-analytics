"""One-shot bootstrap: create the Kavanah singleton workspace.

The workspace tenant-boundary entity (``de_asset_workspace``) lands in F4
as part of the spike's migration plan. This script writes the
*first* doc to that index — the Kavanah singleton — so that the F5
backfill has a workspace_esid to point every existing org at.

The script is **dry-run by default**. Use ``--apply`` to write.

The singleton:

* Title: ``Kavanah Media`` (configurable via ``--title``).
* Slug: deterministic from title via ``WorkspaceSlugService.slugify`` —
  e.g. ``kavanah-media``. The slug is ALSO used as the deterministic doc
  id so the write is idempotent via ``op_type=create`` + 409-as-success
  (same pattern as DataGrantService).
* ``is_agency=True`` — Kavanah does manage multiple client orgs in
  practice (DT, Echo customers, etc.). The new model surfaces this as a
  first-class flag on the workspace rather than the deprecated
  ``is_multi_workspace`` on org.
* ``is_multi_workspace_user_supported=False`` — Phase 1 default. The
  multi-workspace switcher UI stays hidden until a future ticket flips
  this.
* Settings (timezone, currency, fiscal_year_start_month) default to the
  defaults in ``WorkspaceSettingsService``. Operators who want different
  defaults can override via the CLI flags below before apply.
* ``created_by="system_migration"`` so the bootstrap-era write is
  greppable in audit-log-style queries.

Usage::

    # Dry-run (default) — prints what would be written, no writes.
    docker exec -w /app/python imports_container python3 \\
        -m tools.bootstrap_workspaces

    # Apply with defaults.
    docker exec -w /app/python imports_container python3 \\
        -m tools.bootstrap_workspaces --apply

    # Apply with a custom title and timezone.
    docker exec -w /app/python imports_container python3 \\
        -m tools.bootstrap_workspaces --apply \\
        --title "Kavanah Media" --timezone "America/New_York"
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bootstrap_data_grants.py.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.permissions_index_service import WORKSPACE_INDEX  # noqa: E402
from asset_management.web.services.workspace_settings_service import (  # noqa: E402
    DEFAULT_CURRENCY,
    DEFAULT_FISCAL_YEAR_START_MONTH,
    DEFAULT_TIMEZONE,
    WorkspaceSettingsService,
)
from asset_management.web.services.workspace_slug_service import slugify  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

CREATED_BY_ACTOR = "system_migration"
DEFAULT_TITLE = "Kavanah Media"


def build_workspace_doc(
    *,
    title: str,
    is_agency: bool,
    timezone_value: str,
    currency: str,
    fiscal_year_start_month: int,
) -> tuple[str, dict]:
    """Build ``(doc_id, doc_body)`` for the singleton write.

    Slug is the deterministic doc id — same value the future
    WorkspaceSlugService probe will look up. Sanitises the three
    settings values through WorkspaceSettingsService so a typo in the
    CLI args lands as the documented fallback rather than silently
    storing garbage.
    """
    slug = slugify(title)
    if not slug:
        raise ValueError(
            f"Title {title!r} slugifies to empty string. Pick a title with " f"at least one ASCII-alphanumeric character."
        )
    sanitised = WorkspaceSettingsService.sanitize(
        timezone=timezone_value,
        currency=currency,
        fiscal_year_start_month=fiscal_year_start_month,
    )
    # follow-up (2026-05-21): _coerce_timezone now returns ""
    # on failure (was DEFAULT_TIMEZONE) so the wizard's state.workspace_timezone
    # stays clean and re-prefills via Intl on next render. The bootstrap
    # path doesn't have a follow-up render to re-prefill from, so coalesce
    # an empty result to DEFAULT_TIMEZONE here so the bootstrapped doc
    # carries a real zone.
    from asset_management.web.services.workspace_settings_service import (
        DEFAULT_TIMEZONE as _DEFAULT_TZ,
    )

    bootstrap_tz = sanitised.timezone or _DEFAULT_TZ
    now_iso = datetime.now(timezone.utc).isoformat()
    doc = {
        "workspace_title": title,
        "workspace_slug": slug,
        "is_agency": is_agency,
        # Phase-1 default: switcher UI stays hidden until a future ticket
        # flips this on a per-workspace basis.
        "is_multi_workspace_user_supported": False,
        "timezone": bootstrap_tz,
        "currency": sanitised.currency,
        "fiscal_year_start_month": sanitised.fiscal_year_start_month,
        "created_at": now_iso,
        "created_by": CREATED_BY_ACTOR,
    }
    return slug, doc


def run(
    *,
    apply: bool,
    title: str,
    is_agency: bool,
    timezone_value: str,
    currency: str,
    fiscal_year_start_month: int,
) -> int:
    """Plan and (optionally) apply the bootstrap. Returns the exit code."""
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    try:
        doc_id, doc = build_workspace_doc(
            title=title,
            is_agency=is_agency,
            timezone_value=timezone_value,
            currency=currency,
            fiscal_year_start_month=fiscal_year_start_month,
        )
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    logger.info("Bootstrap plan:")
    logger.info("  index:        %s", WORKSPACE_INDEX)
    logger.info("  doc_id:       %s", doc_id)
    logger.info("  title:        %s", doc["workspace_title"])
    logger.info("  slug:         %s", doc["workspace_slug"])
    logger.info("  is_agency:    %s", doc["is_agency"])
    logger.info("  timezone:     %s", doc["timezone"])
    logger.info("  currency:     %s", doc["currency"])
    logger.info("  fiscal_year_start_month: %s", doc["fiscal_year_start_month"])
    logger.info("  created_by:   %s", doc["created_by"])

    if not apply:
        logger.info("")
        logger.info("DRY-RUN: no writes. Pass --apply to bootstrap for real.")
        return 0

    logger.info("")
    logger.info("APPLY: writing singleton workspace...")
    try:
        es.index(
            index=WORKSPACE_INDEX,
            id=doc_id,
            document=doc,
            op_type="create",
            refresh="wait_for",
        )
    except Exception as e:
        # version_conflict / 409 = workspace already exists — happy idempotent path.
        # Re-raise everything else so a real ES failure surfaces.
        if "version_conflict" in str(e).lower() or "conflict" in str(e).lower():
            logger.info("  SKIPPED %s — already exists (idempotent no-op)", doc_id)
            return 0
        logger.error("  FAILED %s: %s", doc_id, e)
        return 1

    logger.info("  CREATED %s (slug=%s, title=%r)", doc_id, doc["workspace_slug"], doc["workspace_title"])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the singleton workspace. Default is dry-run.",
    )
    parser.add_argument(
        "--title",
        default=DEFAULT_TITLE,
        help=f"Workspace title. Default: {DEFAULT_TITLE!r}.",
    )
    parser.add_argument(
        "--not-agency",
        dest="is_agency",
        action="store_false",
        help="Mark the singleton as NOT an agency (default: is_agency=true).",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Workspace timezone. Default: {DEFAULT_TIMEZONE}.",
    )
    parser.add_argument(
        "--currency",
        default=DEFAULT_CURRENCY,
        help=f"Workspace currency (ISO-4217). Default: {DEFAULT_CURRENCY}.",
    )
    parser.add_argument(
        "--fiscal-year-start-month",
        type=int,
        default=DEFAULT_FISCAL_YEAR_START_MONTH,
        help=f"Fiscal year start month (1-12). Default: {DEFAULT_FISCAL_YEAR_START_MONTH}.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(
        apply=args.apply,
        title=args.title,
        is_agency=args.is_agency,
        timezone_value=args.timezone,
        currency=args.currency,
        fiscal_year_start_month=args.fiscal_year_start_month,
    )


if __name__ == "__main__":
    raise SystemExit(main())

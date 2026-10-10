"""Seed the per-workspace ``hide_empty_dashboards`` flag.

Sets ``de_asset_workspace.hide_empty_dashboards = true`` on a single
workspace so the user-facing dashboards sidebar keeps the legacy
data-driven menu hiding (``updatePlatformVisibility`` in
``dashboards.html``): a dashboard whose underlying indices hold zero
documents is hidden from the menu.

Every other workspace inherits the safe default (``false``, resolved by
``WorkspaceMembershipService.hide_empty_dashboards_for_workspace``):
flag-visible dashboards stay in the menu regardless of data so the
in-dashboard "pull data" control is always reachable.

Only the legacy Kavanah Media workspace (slug ``kavanah-media``) wants
the hiding behavior, so that is the default target. The flag is a plain
boolean on the workspace doc — no mapping migration is needed (ES
dynamic mapping adds it on first write).

The script is **dry-run by default**. Use ``--apply`` to write.

Idempotency: a partial update that sets a single boolean field. Re-running
after a successful pass is a no-op write (``result=noop`` when the value
is already ``true``) and reports ``changed=0``.

Usage::

    # Dry-run (default) — shows the current value and what would change.
    docker exec -w /app/python imports_container python3 \\
        -m tools.seed_hide_empty_dashboards

    # Apply to the default workspace (kavanah-media).
    docker exec -w /app/python imports_container python3 \\
        -m tools.seed_hide_empty_dashboards --apply

    # Target a different workspace, or clear the flag.
    docker exec -w /app/python imports_container python3 \\
        -m tools.seed_hide_empty_dashboards --apply \\
        --workspace-slug my-workspace --value false
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from asset_management.web.services.permissions_index_service import (  # noqa: E402
    WORKSPACE_INDEX,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE_SLUG = "kavanah-media"


def run(*, apply: bool, workspace_slug: str, value: bool) -> int:
    """Read the workspace doc, report the current flag, and (if --apply) set it.

    Returns 0 on success (including no-op), 1 if the workspace doc is
    missing or the write fails.
    """
    es = get_elasticsearch_client()
    if es is None:
        logger.error("No Elasticsearch client available.")
        return 1

    try:
        resp = es.get(index=WORKSPACE_INDEX, id=workspace_slug)
    except Exception as e:
        if "not_found" in str(e).lower() or "404" in str(e):
            logger.error(
                "Workspace doc not found: index=%s id=%r. " "Pass --workspace-slug to target a different workspace.",
                WORKSPACE_INDEX,
                workspace_slug,
            )
            return 1
        logger.error("Failed to read workspace %r: %s", workspace_slug, e)
        return 1

    source = resp.get("_source") or {}
    title = source.get("workspace_title", "(no title)")
    current = bool(source.get("hide_empty_dashboards"))

    logger.info(
        "Workspace %r (%s): hide_empty_dashboards is currently %s; target %s.",
        workspace_slug,
        title,
        current,
        value,
    )

    if current == value:
        logger.info("No change needed — flag already %s.", value)
        return 0

    if not apply:
        logger.info("")
        logger.info("DRY-RUN: no writes. Pass --apply to set the flag for real.")
        return 0

    try:
        result = es.update(
            index=WORKSPACE_INDEX,
            id=workspace_slug,
            body={"doc": {"hide_empty_dashboards": value}},
            refresh="wait_for",
        )
    except Exception as e:
        logger.error("Failed to update workspace %r: %s", workspace_slug, e)
        return 1

    logger.info(
        "APPLY: set hide_empty_dashboards=%s on %r (es result=%s). changed=1",
        value,
        workspace_slug,
        result.get("result"),
    )
    return 0


def _parse_bool(raw: str) -> bool:
    """Coerce a --value CLI string into a bool. Rejects ambiguous input."""
    candidate = raw.strip().lower()
    if candidate in ("true", "1", "yes", "on"):
        return True
    if candidate in ("false", "0", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected a boolean, got {raw!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the flag. Default is dry-run.",
    )
    parser.add_argument(
        "--workspace-slug",
        default=DEFAULT_WORKSPACE_SLUG,
        help=f"Target workspace slug (the doc _id). Default: {DEFAULT_WORKSPACE_SLUG!r}.",
    )
    parser.add_argument(
        "--value",
        type=_parse_bool,
        default=True,
        help="Boolean value to set (true/false). Default: true.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run(apply=args.apply, workspace_slug=args.workspace_slug, value=args.value)


if __name__ == "__main__":
    raise SystemExit(main())

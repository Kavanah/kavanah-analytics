"""One-shot cleanup: drop the reverse aliases.

The ESID migration left each old ``{slug}_{source}`` tenant index name
behind as a REVERSE ALIAS onto the new ``{team_esid}__{source}`` index, kept
"indefinitely" (Decision B) for dashboard back-compat until this cleanup.
Consumer audit (2026-09-01, prod): zero exact alias-name references in
de_asset_echarts_dashboards / de_dashboard_templates; every data view uses
``*_{source}`` wildcards, which match the ESID indices directly; the two
ad-hoc diagnostics that hardcoded ``kavanah_team_*`` names were widened to
wildcards in the same change; data_quality_service's legacy-name candidate is
a redundant fallback beside the always-present ESID candidate.

Selection rule (matches the audit): an alias WITHOUT ``__`` on an index WITH
``__``, not dot-prefixed. Intentional aliases with ESID-style names are
untouched. Every removal is logged as ``alias -> index`` so the set is
trivially re-creatable if anything surfaces.

Dry-run by default; ``--apply`` removes them in one ``update_aliases``
actions block (idempotent — a second run finds none).

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.remove_reverse_aliases [--apply]
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

from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)


def reverse_aliases(alias_map: dict) -> list[tuple[str, str]]:
    """(alias, index) pairs that fit the reverse-alias shape."""
    out = []
    for index, meta in (alias_map or {}).items():
        if "__" not in index or index.startswith("."):
            continue
        for alias in meta.get("aliases") or {}:
            if "__" not in alias and not alias.startswith("."):
                out.append((alias, index))
    return sorted(out)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description="Drop  reverse aliases.")
    ap.add_argument("--apply", action="store_true", help="remove them (default: dry-run)")
    args = ap.parse_args()

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Elasticsearch unavailable")
        return 1

    pairs = reverse_aliases(es.indices.get_alias(index="*", expand_wildcards="open"))
    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("%s: %d reverse alias(es)", mode, len(pairs))
    for alias, index in pairs:
        logger.info("  %s -> %s", alias, index)

    if not pairs:
        logger.info("Nothing to remove.")
        return 0
    if not args.apply:
        logger.info("DRY-RUN only. Re-run with --apply to remove.")
        return 0

    actions = [{"remove": {"index": index, "alias": alias}} for alias, index in pairs]
    es.indices.update_aliases(body={"actions": actions})
    logger.info("Removed %d alias(es) in one actions block.", len(actions))
    remaining = reverse_aliases(es.indices.get_alias(index="*", expand_wildcards="open"))
    logger.info("Remaining after removal: %d", len(remaining))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

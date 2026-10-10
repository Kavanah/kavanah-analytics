"""Repair dangling references found by ``audit_dangling_references``.

Dry-run by default; pass ``--apply`` to write. Read-only until ``--apply``.

## What it does

 stopped NEW dangling references accruing; this tool strips the ones
that already accumulated while the old cleanup silently no-op'd. It reuses the
exact painless removal semantics of ``ReferenceCleanupService`` (the on-delete
cascade), but targets the *specific* dead esid values the audit found rather
than "all references to a just-deleted doc":

* **array** esid field (``is_array``): remove ONLY the dead elements from the
  esid array (and the paired title-array element at the same index), leaving
  every live reference in place. Reuses ``_ARRAY_BULK_CLEANUP_SCRIPT``.
* **scalar** esid field: clear the esid + its title field, but ONLY when the
  stored value is one of the dead esids (a value guard the on-delete scalar
  script omits because there the whole field is known-dead — here we must not
  touch a field that happens to hold a LIVE esid).

The child docs are matched with the ``dual_terms`` helper so the
``update_by_query`` hits on either mapping shape.

## Ordering

Run this only AFTER is deployed (it reuses the fixed cleanup and the
dual-term helper). Re-run ``audit_dangling_references`` afterward — it must
report 0.

Usage::

    # dry-run: show what WOULD be stripped (read-only)
    docker exec -w /app/python imports_container python3 \\
        -m tools.repair_dangling_references

    # scope to one child index while validating
    docker exec -w /app/python imports_container python3 \\
        -m tools.repair_dangling_references --child-index de_asset_teams

    # apply (writes; per-edge update_by_query)
    docker exec -w /app/python imports_container python3 \\
        -m tools.repair_dangling_references --apply
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

from lib.es_helpers import get_elasticsearch_client, dual_terms  # noqa: E402
from asset_management.web.services.reference_cleanup_service import (  # noqa: E402
    _ARRAY_BULK_CLEANUP_SCRIPT,
    _INDEX_TITLE_FIELDS,
    REVERSE_MAP,
)
from tools.audit_dangling_references import audit  # noqa: E402

logger = logging.getLogger(__name__)

# Scalar removal guarded on value: only clear the esid (and its title) when the
# stored esid is one of the dead ones. The on-delete _SCALAR_CLEANUP_SCRIPT
# removes unconditionally because there the field is known-dead; a targeted
# repair must not clobber a scalar that holds a LIVE esid.
_SCALAR_GUARDED_CLEANUP_SCRIPT = """
    if (ctx._source.containsKey(params.esid_field)
            && params.dead.contains(ctx._source[params.esid_field])) {
        ctx._source.remove(params.esid_field);
        if (ctx._source.containsKey(params.title_field)) {
            ctx._source.remove(params.title_field);
        }
    }
"""


def _title_field_for(entry) -> str:
    """The paired title field to strip alongside the esid. REVERSE_MAP entries
    carry ``title_field``; fall back to the per-index title map, else ''."""
    return entry.get("title_field") or _INDEX_TITLE_FIELDS.get(entry["child_index"], "")


def _entry_for(child_index: str, esid_field: str):
    """Find the REVERSE_MAP entry matching a (child_index, esid_field) edge so
    the repair uses the same is_array / title_field the cleanup service would."""
    for entries in REVERSE_MAP.values():
        for entry in entries:
            if entry["child_index"] == child_index and entry["esid_field"] == esid_field:
                return entry
    return None


def repair(es, child_filter: set[str] | None = None, apply: bool = False) -> dict:
    """Audit, then strip each dangling reference. Dry-run unless ``apply``.

    Returns ``{"planned": [...], "stripped": {edge_key: updated_count}}``.
    """
    result = audit(es, child_filter)
    planned = []
    stripped: dict[str, int] = {}

    for edge in result["edges"]:
        if edge["dangling"] == 0:
            continue
        child_index = edge["child_index"]
        esid_field = edge["esid_field"]
        dead = edge["dangling_esids"]
        entry = _entry_for(child_index, esid_field)
        if entry is None:
            logger.warning(
                "no REVERSE_MAP entry for %s.%s — skipping (should not happen)",
                child_index,
                esid_field,
            )
            continue
        title_field = _title_field_for(entry)
        edge_key = f"{child_index}.{esid_field}"

        planned.append(
            {
                "edge": edge_key,
                "is_array": entry["is_array"],
                "dead_esids": dead,
                "affected_children": [a["child_id"] for a in edge["affected_children"]],
            }
        )

        # Audit-trail: name every child + the dead esids it loses.
        for aff in edge["affected_children"]:
            logger.info(
                "%s %s.%s child %s -> strip %s",
                "APPLY" if apply else "DRY  ",
                child_index,
                esid_field,
                aff["child_id"],
                aff["missing"],
            )

        if not apply:
            continue

        match = dual_terms(esid_field, dead)
        if entry["is_array"]:
            body = {
                "query": match,
                "script": {
                    "source": _ARRAY_BULK_CLEANUP_SCRIPT,
                    "lang": "painless",
                    "params": {
                        "esid_field": esid_field,
                        "title_field": title_field,
                        "doc_ids": dead,
                    },
                },
            }
        else:
            body = {
                "query": match,
                "script": {
                    "source": _SCALAR_GUARDED_CLEANUP_SCRIPT,
                    "lang": "painless",
                    "params": {
                        "esid_field": esid_field,
                        "title_field": title_field,
                        "dead": dead,
                    },
                },
            }
        resp = es.update_by_query(
            index=child_index,
            body=body,
            conflicts="proceed",
            refresh=True,
        )
        stripped[edge_key] = int(resp.get("updated", 0))
        logger.info("  -> updated %d doc(s) in %s", stripped[edge_key], child_index)

    return {"planned": planned, "stripped": stripped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child-index", action="append", default=None, help="Restrict to this child index (repeatable).")
    parser.add_argument("--apply", action="store_true", help="Write the strips (default: dry-run, read-only).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    mode = "APPLY (writing)" if args.apply else "DRY-RUN (read-only)"
    logger.info("===  dangling-reference repair — %s ===\n", mode)

    child_filter = set(args.child_index) if args.child_index else None
    out = repair(es, child_filter, apply=args.apply)

    logger.info("")
    if not out["planned"]:
        logger.info("Nothing to repair — audit found no dangling references.")
        return 0

    total_edges = len(out["planned"])
    total_children = sum(len(p["affected_children"]) for p in out["planned"])
    logger.info("Summary:")
    logger.info("  edges with danglers   %d", total_edges)
    logger.info("  affected child docs   %d", total_children)
    if args.apply:
        logger.info("  docs updated          %d", sum(out["stripped"].values()))
        logger.info("\n  Re-run audit_dangling_references to confirm it reports 0.")
    else:
        logger.info("\n  DRY-RUN — no writes. Re-run with --apply to strip these.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

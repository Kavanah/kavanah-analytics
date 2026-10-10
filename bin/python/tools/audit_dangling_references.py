"""Read-only audit: dangling references across all REVERSE_MAP edges.

Makes NO writes — only ``es.search`` / bulk id lookups. Safe on staging or prod.

## Why this exists

 fixed ``ReferenceCleanupService`` — its cleanup queries targeted
``<esid_field>.keyword`` while the ``de_asset_*`` esid fields are bare
``keyword`` (per the schema's ``strings_as_keyword`` dynamic template), so on
prod the strip silently matched nothing. Because that dynamic template has been
in place since the schema shipped, EVERY asset delete since then ran a cleanup
that no-op'd. The consequence: child docs may still carry esid references to
parent docs that were deleted long ago (dangling foreign keys).

This tool quantifies that damage. For each parent->child edge in
``ReferenceCleanupService.REVERSE_MAP`` (the same map the cleanup service walks),
it collects every esid value the child docs reference and checks whether the
referenced parent doc still exists. A referenced esid with no live parent doc is
a dangling reference.

It does NOT repair anything — run it first to size the problem, then the
repair step (a separate ``--apply`` tool or the fixed cleanup service) removes
what this finds. Run this BEFORE and AFTER any repair to confirm it reached zero.

## Scope / limitations

* Covers the scalar and array esid fields in REVERSE_MAP. A parent id that is
  the ES ``_id`` of a live doc in the parent index counts as "resolved."
* The parent index for an edge is inferred from the source_index key of
  REVERSE_MAP (the index whose docs the esid points at).
* Every parent index resolves the same way: the referenced esid value IS the
  parent doc's ES ``_id``. ``de_asset_workspace`` is no exception — its doc
  ``_id`` IS the workspace slug (per permissions_index_service, "doc id ==
  workspace_esid"), so a workspace_esid reference resolves by ``_id`` like the
  rest.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_dangling_references

    # limit to specific child indices (repeatable):
    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_dangling_references --child-index de_asset_teams

    # emit the full dangling list as JSON (for the repair step to consume):
    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_dangling_references --json > /app/data/dangling_refs.json
"""

from __future__ import annotations

import argparse
import json
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
from asset_management.web.services.reference_cleanup_service import REVERSE_MAP  # noqa: E402

logger = logging.getLogger(__name__)


def _normalise_esids(raw) -> list[str]:
    """A store_esid field is either a scalar string or a list of strings.
    Return the non-empty values as a list either way."""
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [v for v in raw if isinstance(v, str) and v]
    return []


def _live_parent_ids_by_id(es, parent_index: str, esids: set[str]) -> set[str]:
    """Return the subset of ``esids`` that are the ES _id of a live doc in
    ``parent_index``. Batched ``ids`` query; a missing id simply isn't in the
    response. Read-only."""
    live: set[str] = set()
    ids = list(esids)
    for i in range(0, len(ids), 1000):
        chunk = ids[i : i + 1000]
        resp = es.search(
            index=parent_index,
            body={"query": {"ids": {"values": chunk}}, "_source": False, "size": len(chunk)},
        )
        for hit in resp.get("hits", {}).get("hits", []):
            live.add(hit["_id"])
    return live


def _edges(child_filter: set[str] | None):
    """Yield (source_index, entry) for every REVERSE_MAP edge, optionally
    filtered to specific child indices."""
    for source_index, entries in REVERSE_MAP.items():
        for entry in entries:
            if child_filter and entry["child_index"] not in child_filter:
                continue
            yield source_index, entry


def audit(es, child_filter: set[str] | None = None) -> dict:
    """Walk every REVERSE_MAP edge; for each, find child docs referencing a
    parent esid that no longer resolves. Read-only.

    Returns::

        {
          "edges": [
            {
              "parent_index", "child_index", "esid_field", "is_array",
              "referenced": int,          # distinct esids child docs point at
              "dangling": int,            # of those, how many resolve to nothing
              "dangling_esids": [str],    # the missing parent esids
              "affected_children": [ {child_id, missing: [esid, ...]}, ... ],
            }, ...
          ],
          "totals": {"edges": int, "dangling_refs": int, "affected_children": int},
        }
    """
    edges_out = []
    total_dangling = 0
    total_children = 0

    for parent_index, entry in _edges(child_filter):
        child_index = entry["child_index"]
        esid_field = entry["esid_field"]

        # Pull every child doc that HAS the esid field, with just that field.
        # size 10000 single scan (no _id sort — the cluster 400s on it; asset
        # indices are well under 10k docs each).
        resp = es.search(
            index=child_index,
            body={
                "query": {"exists": {"field": esid_field}},
                "_source": [esid_field],
                "size": 10000,
            },
        )
        hits = resp.get("hits", {}).get("hits", [])

        # child_id -> set of referenced esids on this field
        child_refs: dict[str, set[str]] = {}
        all_referenced: set[str] = set()
        for hit in hits:
            vals = _normalise_esids((hit.get("_source") or {}).get(esid_field))
            if not vals:
                continue
            child_refs[hit["_id"]] = set(vals)
            all_referenced.update(vals)

        if not all_referenced:
            edges_out.append(
                {
                    "parent_index": parent_index,
                    "child_index": child_index,
                    "esid_field": esid_field,
                    "is_array": entry["is_array"],
                    "referenced": 0,
                    "dangling": 0,
                    "dangling_esids": [],
                    "affected_children": [],
                }
            )
            continue

        # Resolve which referenced esids point at a LIVE parent doc. Every
        # parent index (including de_asset_workspace, which is _id-keyed by the
        # workspace slug — see permissions_index_service "doc id == workspace_esid")
        # resolves the same way: the referenced value IS the parent doc's _id.
        live = _live_parent_ids_by_id(es, parent_index, all_referenced)

        dangling_esids = all_referenced - live

        affected = []
        if dangling_esids:
            for child_id, refs in child_refs.items():
                missing = sorted(refs & dangling_esids)
                if missing:
                    affected.append({"child_id": child_id, "missing": missing})

        total_dangling += len(dangling_esids)
        total_children += len(affected)

        edges_out.append(
            {
                "parent_index": parent_index,
                "child_index": child_index,
                "esid_field": esid_field,
                "is_array": entry["is_array"],
                "referenced": len(all_referenced),
                "dangling": len(dangling_esids),
                "dangling_esids": sorted(dangling_esids),
                "affected_children": affected,
            }
        )

    return {
        "edges": edges_out,
        "totals": {
            "edges": len(edges_out),
            "dangling_refs": total_dangling,
            "affected_children": total_children,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--child-index",
        action="append",
        default=None,
        help="Restrict to this child index (repeatable). Default: all REVERSE_MAP edges.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the full result as JSON on stdout (for the repair step).",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    child_filter = set(args.child_index) if args.child_index else None
    result = audit(es, child_filter)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    logger.info("===  dangling-reference audit (read-only) ===\n")
    for edge in sorted(result["edges"], key=lambda e: (-e["dangling"], e["child_index"], e["esid_field"])):
        marker = "  " if edge["dangling"] == 0 else "!!"
        logger.info(
            "%s %-32s .%-34s -> %-28s  referenced=%-4d dangling=%d",
            marker,
            edge["child_index"],
            edge["esid_field"],
            edge["parent_index"],
            edge["referenced"],
            edge["dangling"],
        )
        for aff in edge["affected_children"][:20]:
            logger.info("       child %s misses %s", aff["child_id"], aff["missing"])
        if len(edge["affected_children"]) > 20:
            logger.info("       ... and %d more affected children", len(edge["affected_children"]) - 20)

    t = result["totals"]
    logger.info("")
    logger.info("Summary:")
    logger.info("  edges audited            %d", t["edges"])
    logger.info("  dangling references      %d", t["dangling_refs"])
    logger.info("  affected child docs      %d", t["affected_children"])
    if t["dangling_refs"] == 0:
        logger.info("\n  CLEAN — no dangling references found.")
    else:
        logger.info("\n  Run the  repair step to strip these, then re-run this audit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

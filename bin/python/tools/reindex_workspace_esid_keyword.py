"""One-shot reindex: re-map ``workspace_esid`` to ``keyword`` on the two
text-mapped asset indices.

``de_asset_teams`` and ``de_asset_echarts_dashboards`` had their
``workspace_esid`` inferred as ``text`` (analyzed, with a ``.keyword``
sub-field) at first write, because no code path declared the field type. A
``text`` field tokenizes a hyphenated value, so a bare ``term:{workspace_esid:
...}`` query silently misses any hyphenated slug — the BugB
cascade-purge failure. worked around it with ``.keyword`` everywhere;
 fixes the mapping and reverts the workaround.

Elasticsearch cannot change an existing field's mapping in place, so each
index must be rebuilt. This tool, per index:

1. Skips if the index is missing, or if ``workspace_esid`` is already keyword.
2. Creates ``{index}_v2`` carrying the index's CURRENT mapping with
   ``properties.workspace_esid`` overridden to ``{"type": "keyword"}`` (the
   ``text`` block + its ``fields`` sub-block dropped). Other dynamically-mapped
   fields are preserved verbatim.
3. Reindexes ``{index}`` -> ``{index}_v2`` and verifies the doc counts match
   (aborts that index, leaving the original intact, if they differ).
4. Deletes ``{index}`` and recreates it with the corrected mapping.
5. Reindexes ``{index}_v2`` -> ``{index}`` and re-verifies counts.
6. Deletes ``{index}_v2``.

**Dry-run by default.** ``--apply`` writes. Step 4 has a brief window where the
original index is absent — run during low traffic.

A ``put_regular_index_template`` guard is applied before each create so a
lower-priority data-stream template can't intercept and create a data stream.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.reindex_workspace_esid_keyword

    docker exec -w /app/python imports_container python3 \\
        -m tools.reindex_workspace_esid_keyword --apply
"""

from __future__ import annotations

import argparse
import copy
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
    WORKSPACE_ESID_KEYWORD_INDICES,
)
from lib.es_helpers import (  # noqa: E402
    get_elasticsearch_client,
    put_regular_index_template,
)

logger = logging.getLogger(__name__)


def _current_workspace_esid_type(es, index_name: str) -> str | None:
    """Return the mapped type of ``index_name.workspace_esid`` or None.

    None means the index has no ``workspace_esid`` property mapped yet (an
    empty index that has never had a doc with the field). That is treated the
    same as "needs the keyword mapping" — recreating with the explicit mapping
    is harmless.
    """
    resp = es.indices.get_mapping(index=index_name)
    # resp is {index_name: {"mappings": {"properties": {...}}}}
    mappings = (resp.get(index_name) or {}).get("mappings") or {}
    props = mappings.get("properties") or {}
    field = props.get("workspace_esid")
    if not field:
        return None
    return field.get("type")


def _corrected_mapping(es, index_name: str) -> dict:
    """Deep-copy the index's current mapping, force ``workspace_esid`` to
    keyword, and drop any ``text`` ``fields`` sub-block on it."""
    resp = es.indices.get_mapping(index=index_name)
    mappings = (resp.get(index_name) or {}).get("mappings") or {}
    mappings = copy.deepcopy(mappings)
    props = mappings.setdefault("properties", {})
    props["workspace_esid"] = {"type": "keyword"}
    return mappings


def _count(es, index_name: str) -> int:
    return int(es.count(index=index_name).get("count", 0))


def _reindex_one(es, index_name: str, *, apply: bool) -> str:
    """Reindex a single index to a keyword ``workspace_esid``. Returns a
    short outcome string for the summary."""
    if not es.indices.exists(index=index_name):
        logger.info("SKIP %s — index does not exist on this cluster.", index_name)
        return "missing"

    current_type = _current_workspace_esid_type(es, index_name)
    if current_type == "keyword":
        logger.info("SKIP %s — workspace_esid is already keyword.", index_name)
        return "already-keyword"

    v2 = f"{index_name}_v2"
    corrected = _corrected_mapping(es, index_name)
    src_count = _count(es, index_name)
    logger.info(
        "%s %s — workspace_esid is %r; %d docs to reindex via %s.",
        "APPLY" if apply else "WOULD REINDEX",
        index_name,
        current_type,
        src_count,
        v2,
    )

    if not apply:
        logger.info("  target mapping for workspace_esid: {'type': 'keyword'}")
        return "planned"

    if es.indices.exists(index=v2):
        logger.error(
            "  temp index %s already exists — a previous run did not clean up. " "Inspect and delete it before re-running.",
            v2,
        )
        return "aborted: temp exists"

    # Step 2: temp index with the corrected mapping.
    put_regular_index_template(es, v2, corrected, priority=500)
    es.indices.create(index=v2, body={"mappings": corrected})

    # Step 3: source -> temp, verify counts.
    es.reindex(body={"source": {"index": index_name}, "dest": {"index": v2}}, wait_for_completion=True, refresh=True)
    v2_count = _count(es, v2)
    if v2_count != src_count:
        logger.error(
            "  count mismatch after %s -> %s (%d vs %d). Leaving %s intact and " "%s for inspection; NOT swapping.",
            index_name,
            v2,
            src_count,
            v2_count,
            index_name,
            v2,
        )
        return "aborted: count mismatch"

    # (data-loss guard) — NEVER delete the original unless the temp copy
    # is provably populated when the source had data. The 2026-06-15 incident
    # emptied de_asset_teams + de_asset_echarts_dashboards on staging: the
    # original was deleted in step 4 but the data did not survive the round-trip.
    # The count-equality check above catches a 34!=0 mismatch, but this explicit
    # gate is defense-in-depth against a count-check regression / misread.
    if src_count > 0 and v2_count == 0:
        logger.error(
            "  REFUSING to delete %s: source had %d docs but %s is empty after "
            "reindex. Leaving original intact. Investigate the reindex.",
            index_name,
            src_count,
            v2,
        )
        return "aborted: temp empty despite non-empty source"

    # snapshot-before-delete safety net. Even with the count + empty-v2
    # guards above, step 4 deletes the only remaining live copy of the data (the
    # temp _v2 is itself deleted at step 6). Take a point-in-time snapshot of the
    # original FIRST so the delete is always recoverable. Requires a registered
    # repo named ``reindex_safety_repo``; if it's absent we ABORT rather than
    # proceed unprotected (the whole point is to never delete without a backstop).
    if src_count > 0:
        snap = f"reindex_safety_{index_name}"
        try:
            es.snapshot.create(
                repository="reindex_safety_repo",
                snapshot=snap,
                body={"indices": index_name, "include_global_state": False},
                wait_for_completion=True,
            )
            logger.info("  safety snapshot taken: reindex_safety_repo/%s", snap)
        except Exception as e:  # noqa: BLE001 — refuse to proceed unprotected
            logger.error(
                "  REFUSING to delete %s: could not take safety snapshot (%s). "
                "Register an fs repo 'reindex_safety_repo' first, then re-run.",
                index_name,
                e,
            )
            return "aborted: no safety snapshot"

    # Step 4: delete original, recreate with corrected mapping.
    es.indices.delete(index=index_name)
    put_regular_index_template(es, index_name, corrected, priority=500)
    es.indices.create(index=index_name, body={"mappings": corrected})

    # Step 5: temp -> original, re-verify.
    es.reindex(body={"source": {"index": v2}, "dest": {"index": index_name}}, wait_for_completion=True, refresh=True)
    final_count = _count(es, index_name)
    if final_count != src_count:
        logger.error(
            "  count mismatch after %s -> %s (%d vs %d). %s STILL HOLDS the data " "— do NOT delete it; investigate.",
            v2,
            index_name,
            src_count,
            final_count,
            v2,
        )
        return "aborted: final count mismatch (temp retained)"

    # Step 6: drop the temp index AND its throwaway template (created in
    # step 2 only to guard the temp create against a data-stream template).
    es.indices.delete(index=v2)
    try:
        es.indices.delete_index_template(name=f"template_{v2}")
    except Exception:  # noqa: BLE001 — best-effort cleanup; cruft is harmless
        pass
    logger.info("  DONE %s — %d docs, workspace_esid now keyword.", index_name, final_count)
    return "reindexed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the reindex. Default is dry-run (plan only).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    if args.apply:
        logger.warning(
            "APPLY mode: each index is briefly DELETED and recreated between " "reindex passes. Run during low traffic.\n"
        )

    outcomes: dict[str, str] = {}
    for index_name in WORKSPACE_ESID_KEYWORD_INDICES:
        try:
            outcomes[index_name] = _reindex_one(es, index_name, apply=args.apply)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logger.error("FAILED %s: %s", index_name, e)
            outcomes[index_name] = f"error: {e}"

    logger.info("")
    logger.info("Reindex summary:")
    for index_name, outcome in outcomes.items():
        logger.info("  %-32s %s", index_name, outcome)
    if not args.apply:
        logger.info("")
        logger.info("(dry-run — re-run with --apply to reindex)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

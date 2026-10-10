"""rebuild ``de_asset_*`` indices with explicit keyword schemas.

Every index registered in ``migration.schema_loader.ASSET_SCHEMA_MAP`` is
rebuilt so its mapping matches ``schemas/asset_schema_*.json`` — explicit
``keyword`` for ESIDs / ids / titles / status fields, plus a
``dynamic_templates`` strings_as_keyword rule so future unenumerated string
fields can never drift back to ``text +.keyword`` (the failure
class). Elasticsearch cannot change an existing field's mapping in place, so
each drifted index is round-tripped through a temp copy, following the
 pattern (``tools.reindex_workspace_esid_keyword``) with its
data-loss guards intact.

Target-mapping construction (the safety core)
=============================================
The recreate does NOT use the schema file alone. Per index the target is:

- every schema-enumerated field, verbatim from the schema file;
- every OTHER field present in the live mapping:
  * live ``text`` (with or without a ``.keyword`` sub-field) -> bare
    ``keyword`` — that's the drift being fixed;
  * any other live type (``date``, ``long``, ``boolean``, ``object``...)
    preserved verbatim — so unenumerated dates/numerics keep their types and
    dynamic date-detection can't hijack a string field during the reindex.

Per index
=========
1. Skip if missing on this cluster, or already clean (audited via the same
   rules as ``migration.audit_asset_mappings``).
2. Create ``{index}_tmp`` with the target mapping.
3. Reindex ``{index}`` -> tmp; verify doc counts match (abort, original
   intact, if not).
4. refuse to delete when tmp is empty despite a non-empty
   source; take a safety snapshot of the original into ``--repo`` first and
   ABORT if the snapshot fails.
5. Delete ``{index}``; recreate with the target mapping; reindex tmp back;
   re-verify counts (tmp retained on mismatch).
6. Delete tmp + its throwaway template.

**Dry-run by default.** ``--apply`` writes. Step 5 has a brief window where
the original index is absent — run during low traffic.

Usage::

    # dry-run survey + plan
    docker exec -w /app/python imports_container python3 \\
        -m tools.reindex_asset_mappings

    # apply to every registered index (staging/prod)
    docker exec -w /app/python imports_container python3 \\
        -m tools.reindex_asset_mappings --apply

    # apply to a subset
    docker exec -w /app/python imports_container python3 \\
        -m tools.reindex_asset_mappings --apply \\
        --indices de_asset_fb_apps,de_asset_fb_pages

    # non-default snapshot repo
    ... --apply --repo daily_repo
"""

from __future__ import annotations

import argparse
import copy
import logging
import sys
from datetime import datetime, timezone
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

from import_scripts.migration.audit_asset_mappings import audit_asset_index  # noqa: E402
from lib.es_helpers import (  # noqa: E402
    get_elasticsearch_client,
    put_regular_index_template,
)
from migration.schema_loader import (  # noqa: E402
    ASSET_SCHEMA_MAP,
    get_schema_for_asset_index,
)

logger = logging.getLogger(__name__)

TMP_SUFFIX = "_tmp"
DEFAULT_REPO = "reindex_safety_repo"


def build_target_mapping(schema: dict, live_mappings: dict) -> dict:
    """The ``mappings`` body for the rebuilt index: schema fields verbatim,
    live-only ``text`` fields collapsed to keyword, every other live-only
    field's type preserved verbatim. Pure function — unit-tested in
    the release-plan tests"""
    target: dict = {"properties": copy.deepcopy(schema.get("properties", {}))}
    if schema.get("dynamic_templates"):
        target["dynamic_templates"] = copy.deepcopy(schema["dynamic_templates"])

    live_props = live_mappings.get("properties", {}) or {}
    for field_name, live_def in live_props.items():
        if field_name in target["properties"]:
            continue
        if live_def.get("type") == "text":
            # The drift being fixed: auto-mapped text (+ .keyword sub-field)
            # becomes bare keyword. ignore_above mirrors the dynamic template
            # so a pathological long value skips indexing instead of erroring.
            target["properties"][field_name] = {"type": "keyword", "ignore_above": 8191}
        else:
            # date / long / boolean / object / already-keyword — keep as-is so
            # the rebuilt index can't flip a type the schema didn't claim.
            target["properties"][field_name] = copy.deepcopy(live_def)
    return target


def _live_mappings(es, index_name: str) -> dict:
    resp = es.indices.get_mapping(index=index_name)
    return (resp.get(index_name) or {}).get("mappings") or {}


def _count(es, index_name: str) -> int:
    return int(es.count(index=index_name).get("count", 0))


def _reindex_one(es, index_name: str, *, apply: bool, repo: str) -> str:
    """Rebuild a single index. Returns a short outcome string for the summary."""
    schema = get_schema_for_asset_index(index_name)

    mismatches = audit_asset_index(es, index_name, schema)
    if mismatches is None:
        logger.info("SKIP %s — index does not exist on this cluster.", index_name)
        return "missing"
    if not mismatches:
        logger.info("SKIP %s — mapping already matches the asset schema.", index_name)
        return "already-clean"

    tmp = f"{index_name}{TMP_SUFFIX}"
    target = build_target_mapping(schema, _live_mappings(es, index_name))
    src_count = _count(es, index_name)
    logger.info(
        "%s %s — %d drifted field(s); %d docs to reindex via %s.",
        "APPLY" if apply else "WOULD REINDEX",
        index_name,
        len(mismatches),
        src_count,
        tmp,
    )
    for line in mismatches:
        logger.info(line)

    if not apply:
        return "planned"

    if es.indices.exists(index=tmp):
        logger.error(
            "  temp index %s already exists — a previous run did not clean up. Inspect and delete it before re-running.",
            tmp,
        )
        return "aborted: temp exists"

    # Temp index with the target mapping. The template guard
    # stops a lower-priority data-stream template from intercepting the create.
    put_regular_index_template(es, tmp, target, priority=500)
    es.indices.create(index=tmp, body={"mappings": target})

    # Source -> temp, verify counts.
    es.reindex(body={"source": {"index": index_name}, "dest": {"index": tmp}}, wait_for_completion=True, refresh=True)
    tmp_count = _count(es, tmp)
    if tmp_count != src_count:
        logger.error(
            "  count mismatch after %s -> %s (%d vs %d). Leaving %s intact and %s for inspection; NOT swapping.",
            index_name,
            tmp,
            src_count,
            tmp_count,
            index_name,
            tmp,
        )
        return "aborted: count mismatch"

    # (data-loss guard) — never delete the original unless the temp
    # copy is provably populated when the source had data.
    if src_count > 0 and tmp_count == 0:
        logger.error(
            "  REFUSING to delete %s: source had %d docs but %s is empty after reindex. Leaving original intact.",
            index_name,
            src_count,
            tmp,
        )
        return "aborted: temp empty despite non-empty source"

    # snapshot-before-delete safety net. The delete below removes
    # the only durable copy (the temp is itself deleted at the end), so take a
    # point-in-time snapshot FIRST and abort if it fails. Timestamp suffix so
    # a re-run after a partial failure doesn't collide with its own snapshot.
    if src_count > 0:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        snap = f"note_2{index_name}_{ts}"
        try:
            es.snapshot.create(
                repository=repo,
                snapshot=snap,
                body={"indices": index_name, "include_global_state": False},
                wait_for_completion=True,
            )
            logger.info("  safety snapshot taken: %s/%s", repo, snap)
        except Exception as e:  # noqa: BLE001 — refuse to proceed unprotected
            logger.error(
                "  REFUSING to delete %s: could not take safety snapshot (%s). Register repo %r first, then re-run.",
                index_name,
                e,
                repo,
            )
            return "aborted: no safety snapshot"

    # Delete original, recreate with the target mapping, reindex back.
    es.indices.delete(index=index_name)
    put_regular_index_template(es, index_name, target, priority=500)
    es.indices.create(index=index_name, body={"mappings": target})
    es.reindex(body={"source": {"index": tmp}, "dest": {"index": index_name}}, wait_for_completion=True, refresh=True)
    final_count = _count(es, index_name)
    if final_count != src_count:
        logger.error(
            "  count mismatch after %s -> %s (%d vs %d). %s STILL HOLDS the data — do NOT delete it; investigate.",
            tmp,
            index_name,
            src_count,
            final_count,
            tmp,
        )
        return "aborted: final count mismatch (temp retained)"

    # Drop the temp index and its throwaway template.
    es.indices.delete(index=tmp)
    try:
        es.indices.delete_index_template(name=f"template_{tmp}")
    except Exception:  # noqa: BLE001 — best-effort cleanup; cruft is harmless
        pass
    logger.info("  DONE %s — %d docs, mapping now explicit.", index_name, final_count)
    return "reindexed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the reindex. Default is dry-run (plan only).",
    )
    parser.add_argument(
        "--indices",
        default="",
        help="Comma-separated subset of registered indices to process. Default: all registered.",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Snapshot repository for the  safety snapshot (default: {DEFAULT_REPO}).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.indices:
        requested = [name.strip() for name in args.indices.split(",") if name.strip()]
        unknown = [name for name in requested if name not in ASSET_SCHEMA_MAP]
        if unknown:
            logger.error("Not registered in ASSET_SCHEMA_MAP: %s", ", ".join(unknown))
            return 2
        targets = requested
    else:
        targets = sorted(ASSET_SCHEMA_MAP.keys())

    es = get_elasticsearch_client()
    if es is None:
        logger.error("ES unavailable")
        return 2

    if args.apply:
        logger.warning(
            "APPLY mode: each drifted index is briefly DELETED and recreated between reindex passes. Run during low traffic.\n"
        )

    outcomes: dict[str, str] = {}
    for index_name in targets:
        try:
            outcomes[index_name] = _reindex_one(es, index_name, apply=args.apply, repo=args.repo)
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logger.error("FAILED %s: %s", index_name, e)
            outcomes[index_name] = f"error: {e}"

    logger.info("")
    logger.info("Reindex summary:")
    for index_name, outcome in outcomes.items():
        logger.info("  %-36s %s", index_name, outcome)
    if not args.apply:
        logger.info("")
        logger.info("(dry-run — re-run with --apply to reindex)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

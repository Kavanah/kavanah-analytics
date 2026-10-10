"""One-shot migration: enforce ``_id == workspace_slug`` for every workspace doc.

``permissions_index_service.py`` documents the natural-key convention
``de_asset_workspace id = <workspace_slug>`` (the slug is deterministic
from the title via WorkspaceSlugService.slugify). The bootstrap script,
the onboarding wizard, and the earlier Asset Explorer add path all
honor this. But the earlier Asset Explorer add path called
``es.index`` without an explicit id and let ES auto-generate one — so
workspaces created through the UI ended up with ``_id ==
<random-20-char-string>`` and ``workspace_slug`` as a separate field.

That inconsistency broke every downstream consumer that does either:

* ``es.get(index=de_asset_workspace, id=workspace_slug)`` — returns 404
  even though the workspace exists. The most visible symptom was the
   Edit Roles modal's Add-Workspace path 400'ing with "Workspace
  B not found".
* ``workspace_esid == workspace_slug`` equality assumptions in helpers
  that round-trip slug → esid via ``_provision_new_authz_assignments``-
  style search-and-resolve. Those happened to work for bootstrap
  workspaces and broke silently for UI-created ones.

This script reindexes every ``de_asset_workspace`` doc whose ``_id``
does not equal its ``workspace_slug``, then rewrites the
``workspace_esid`` field in three referencing indices:

* ``de_authz_workspace_membership`` — user→workspace membership
* ``de_authz_user_workspace_role`` — workspace-tier role assignment
* ``de_authz_data_grant`` (scope_type=workspace) — workspace-scoped grants

It also rewrites ``workspace_esid`` in the asset hierarchy where it
appears as a reference:

* ``de_asset_organization`` — / F5 field
* any other index in ``ASSET_INDICES_WITH_WORKSPACE_ESID``

Dry-run by default. Apply with ``--apply``.

The script is **idempotent**: a run after a successful apply produces
``rewritten=0`` because every workspace doc already has ``_id == slug``.

Usage::

    # Dry-run (default).
    docker exec -w /app/python imports_container python3 \\
        -m tools.standardize_workspace_ids

    # Apply.
    docker exec -w /app/python imports_container python3 \\
        -m tools.standardize_workspace_ids --apply
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
    DATA_GRANT_INDEX,
    USER_WORKSPACE_ROLE_INDEX,
    WORKSPACE_INDEX,
    WORKSPACE_MEMBERSHIP_INDEX,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)


# Asset indices that carry a ``workspace_esid`` reference field.
# de_asset_organization is the F5 / backfilled link. Other asset
# indices may also carry the field if a future backfill adds it; the
# script discovers references dynamically rather than enumerating each
# index, but keeps this list documented so reviewers know the expected
# scope of rewrites.
ASSET_INDICES_WITH_WORKSPACE_ESID: tuple[str, ...] = ("de_asset_organization",)


def fetch_workspaces_to_rewrite(es) -> list[tuple[str, str, dict]]:
    """Return ``[(current_id, target_slug, source_doc), ...]`` for every
    workspace whose ``_id`` does not match its ``workspace_slug``.

    Workspaces with no ``workspace_slug`` field are skipped with a warning —
    they predate F4 / and need a manual review before they can be
    keyed under the natural-key convention.
    """
    out: list[tuple[str, str, dict]] = []
    try:
        resp = es.search(
            index=WORKSPACE_INDEX,
            body={"query": {"match_all": {}}, "size": 1000},
        )
    except Exception as e:
        logger.error("Could not search %s: %s", WORKSPACE_INDEX, e)
        return out
    for hit in resp.get("hits", {}).get("hits", []):
        current_id = hit.get("_id")
        source = hit.get("_source") or {}
        slug = source.get("workspace_slug")
        if not slug:
            logger.warning(
                "Workspace %r has no workspace_slug; skipping (manual review needed)",
                current_id,
            )
            continue
        if current_id == slug:
            continue
        out.append((current_id, slug, source))
    return out


def rewrite_workspace_doc(es, *, current_id: str, target_slug: str, source: dict, apply: bool) -> bool:
    """Reindex one workspace doc from ``current_id`` to ``target_slug``.

    Writes the new doc with ``op_type=create`` so a stray duplicate at
    the slug-keyed id is surfaced as a 409 (the script aborts the
    rewrite for that doc and logs). Deletes the old doc only after the
    create succeeds.

    Returns True on success, False if the create failed (so the caller
    skips reference-field rewrites for this workspace).
    """
    if not apply:
        logger.info("[dry-run] would reindex workspace %r -> %r", current_id, target_slug)
        return True
    try:
        es.index(
            index=WORKSPACE_INDEX,
            id=target_slug,
            document=source,
            op_type="create",
            refresh="wait_for",
        )
    except Exception as e:
        err = str(e).lower()
        if "version_conflict" in err or "conflict" in err:
            logger.error(
                "Workspace %r: a doc already exists at id %r; manual reconciliation needed",
                current_id,
                target_slug,
            )
            return False
        logger.error("Workspace %r: create at %r failed: %s", current_id, target_slug, e)
        return False
    try:
        es.delete(index=WORKSPACE_INDEX, id=current_id, refresh="wait_for")
    except Exception as e:
        logger.warning(
            "Workspace %r: rewrite succeeded at %r but old-id delete failed: %s",
            current_id,
            target_slug,
            e,
        )
    return True


def rewrite_workspace_esid_references(es, *, current_id: str, target_slug: str, apply: bool) -> dict[str, int]:
    """Update every doc that references ``current_id`` via ``workspace_esid``.

    Walks each referencing index, finds docs whose ``workspace_esid``
    equals ``current_id``, and rewrites the field to ``target_slug``.
    Membership / role-assignment docs additionally need their ``_id``
    rewritten because the id encodes the workspace esid (e.g.
    ``<username>:<workspace_esid>``); for those indices the function
    reindexes (write new + delete old).

    Data-grant docs use the doc-id format ``<username>:<scope_type>:<scope_esid>``;
    same reindex treatment when ``scope_type=workspace``.

    Asset-index references (de_asset_organization etc.) only need a field
    update — the doc id is unrelated to the workspace esid.

    Returns ``{index_name: count_of_rewritten_docs}``.
    """
    counts: dict[str, int] = {}

    # --- 1. Membership index: doc id = "<username>:<workspace_esid>"
    counts[WORKSPACE_MEMBERSHIP_INDEX] = _reindex_by_esid(
        es,
        index=WORKSPACE_MEMBERSHIP_INDEX,
        old_esid=current_id,
        new_esid=target_slug,
        id_template=lambda src, _hit: f"{src.get('username')}:{target_slug}",
        esid_field="workspace_esid",
        apply=apply,
    )

    # --- 2. User-workspace-role index: doc id = "<username>:<workspace_esid>:<role_esid>"
    counts[USER_WORKSPACE_ROLE_INDEX] = _reindex_by_esid(
        es,
        index=USER_WORKSPACE_ROLE_INDEX,
        old_esid=current_id,
        new_esid=target_slug,
        id_template=lambda src, _hit: (f"{src.get('username')}:{target_slug}:{src.get('role_esid')}"),
        esid_field="workspace_esid",
        apply=apply,
    )

    # --- 3. Data-grant index: doc id = "<username>:workspace:<scope_esid>"
    counts[DATA_GRANT_INDEX] = _reindex_data_grants(es, old_esid=current_id, new_esid=target_slug, apply=apply)

    # --- 4. Asset indices: field rewrite only; doc id unchanged.
    for asset_idx in ASSET_INDICES_WITH_WORKSPACE_ESID:
        counts[asset_idx] = _field_update_by_esid(
            es,
            index=asset_idx,
            old_esid=current_id,
            new_esid=target_slug,
            esid_field="workspace_esid",
            apply=apply,
        )

    return counts


def _reindex_by_esid(
    es,
    *,
    index: str,
    old_esid: str,
    new_esid: str,
    id_template,
    esid_field: str,
    apply: bool,
) -> int:
    """Find docs in ``index`` where ``esid_field == old_esid`` and reindex
    them under a new id computed by ``id_template(source, hit)`` with the
    field rewritten to ``new_esid``.
    """
    try:
        resp = es.search(
            index=index,
            body={"query": {"term": {esid_field: old_esid}}, "size": 10000},
        )
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            return 0
        logger.error("Could not search %s: %s", index, e)
        return 0
    rewritten = 0
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        old_id = hit.get("_id")
        new_doc = {**source, esid_field: new_esid}
        new_id = id_template(new_doc, hit)
        if not apply:
            logger.info(
                "[dry-run] would reindex %s %r -> %r (esid %r -> %r)",
                index,
                old_id,
                new_id,
                old_esid,
                new_esid,
            )
            rewritten += 1
            continue
        try:
            es.index(
                index=index,
                id=new_id,
                document=new_doc,
                op_type="create",
                refresh="wait_for",
            )
        except Exception as e:
            err = str(e).lower()
            if "version_conflict" in err or "conflict" in err:
                # The new id already exists — likely a previous partial
                # run. Treat as success for this row and clean up the
                # old id below.
                logger.info(
                    "Doc already exists at new id %s/%s; cleaning up old id %r",
                    index,
                    new_id,
                    old_id,
                )
            else:
                logger.error("Reindex failed for %s/%s -> %s: %s", index, old_id, new_id, e)
                continue
        try:
            es.delete(index=index, id=old_id, refresh="wait_for")
        except Exception as e:
            logger.warning("Old-id delete failed for %s/%s: %s", index, old_id, e)
        rewritten += 1
    return rewritten


def _reindex_data_grants(es, *, old_esid: str, new_esid: str, apply: bool) -> int:
    """Data grants store the workspace esid in two places: ``scope_esid``
    field (when ``scope_type == 'workspace'``) AND the doc id.
    """
    try:
        resp = es.search(
            index=DATA_GRANT_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"scope_type": "workspace"}},
                            {"term": {"scope_esid": old_esid}},
                        ]
                    }
                },
                "size": 10000,
            },
        )
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            return 0
        logger.error("Could not search %s: %s", DATA_GRANT_INDEX, e)
        return 0
    rewritten = 0
    for hit in resp.get("hits", {}).get("hits", []):
        source = hit.get("_source") or {}
        old_id = hit.get("_id")
        new_doc = {**source, "scope_esid": new_esid}
        new_id = f"{source.get('username')}:workspace:{new_esid}"
        if not apply:
            logger.info(
                "[dry-run] would reindex data_grant %r -> %r (esid %r -> %r)",
                old_id,
                new_id,
                old_esid,
                new_esid,
            )
            rewritten += 1
            continue
        try:
            es.index(
                index=DATA_GRANT_INDEX,
                id=new_id,
                document=new_doc,
                op_type="create",
                refresh="wait_for",
            )
        except Exception as e:
            err = str(e).lower()
            if "version_conflict" not in err and "conflict" not in err:
                logger.error("Reindex failed for data_grant %r -> %r: %s", old_id, new_id, e)
                continue
        try:
            es.delete(index=DATA_GRANT_INDEX, id=old_id, refresh="wait_for")
        except Exception as e:
            logger.warning("Old-id delete failed for data_grant %r: %s", old_id, e)
        rewritten += 1
    return rewritten


def _field_update_by_esid(es, *, index: str, old_esid: str, new_esid: str, esid_field: str, apply: bool) -> int:
    """Asset-index references — doc id stays, only the field changes."""
    try:
        resp = es.search(
            index=index,
            body={"query": {"term": {esid_field: old_esid}}, "size": 10000},
        )
    except Exception as e:
        err = str(e).lower()
        if "index_not_found" in err or "404" in err:
            return 0
        logger.error("Could not search %s: %s", index, e)
        return 0
    rewritten = 0
    for hit in resp.get("hits", {}).get("hits", []):
        doc_id = hit.get("_id")
        if not apply:
            logger.info(
                "[dry-run] would update %s/%s field %s %r -> %r",
                index,
                doc_id,
                esid_field,
                old_esid,
                new_esid,
            )
            rewritten += 1
            continue
        try:
            es.update(
                index=index,
                id=doc_id,
                body={"doc": {esid_field: new_esid}},
                refresh="wait_for",
            )
        except Exception as e:
            logger.error("Field update failed for %s/%s: %s", index, doc_id, e)
            continue
        rewritten += 1
    return rewritten


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run).")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable INFO-level logging.")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Force info on the module logger so [dry-run] lines are visible.
    logger.setLevel(logging.INFO)

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch; aborting.")
        return 2

    workspaces = fetch_workspaces_to_rewrite(es)
    if not workspaces:
        print("All workspaces already have _id == workspace_slug. Nothing to do.")
        return 0

    print(f"Found {len(workspaces)} workspace(s) whose _id does not match workspace_slug:")
    for current_id, target_slug, _src in workspaces:
        print(f"  - {current_id!r} -> {target_slug!r}")

    grand_totals: dict[str, int] = {}
    workspaces_rewritten = 0

    for current_id, target_slug, source in workspaces:
        ok = rewrite_workspace_doc(es, current_id=current_id, target_slug=target_slug, source=source, apply=args.apply)
        if not ok:
            print(f"Skipping reference rewrites for {current_id!r} (workspace reindex failed).")
            continue
        workspaces_rewritten += 1
        ref_counts = rewrite_workspace_esid_references(es, current_id=current_id, target_slug=target_slug, apply=args.apply)
        for k, v in ref_counts.items():
            grand_totals[k] = grand_totals.get(k, 0) + v

    print()
    print(f"{'[dry-run] ' if not args.apply else ''}workspaces reindexed: {workspaces_rewritten}")
    for index, count in sorted(grand_totals.items()):
        print(f"  {index}: {count}")
    if not args.apply:
        print()
        print("Dry-run only. Re-run with --apply to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

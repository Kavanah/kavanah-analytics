"""
Reference Cleanup Service — cleans up orphaned ESID references when assets are deleted.

When a parent asset is deleted, child assets that hold its ESID in their fields
are updated to remove the stale reference. This prevents orphaned references
throughout the asset management system.

The reverse relationship map is built automatically from ASSET_CONFIGS at import
time, so any new store_esid fields are picked up without manual changes here.
"""

import logging
import sys
import os

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
)

from lib.es_helpers import get_elasticsearch_client, dual_term, dual_terms
from asset_management.web.asset_configs import ASSET_CONFIGS


def _build_reverse_map():
    """Build a reverse relationship map from ASSET_CONFIGS.

    Scans every asset type's fields for store_esid: True entries and inverts
    the relationship.  The result maps a *source* index (the parent being
    deleted) to a list of child entries that need cleanup.

    Returns
    -------
    dict[str, list[dict]]
        Keyed by source index name.  Each entry:
        {
            "child_index": str,     # ES index of the child asset
            "esid_field": str,      # field on the child that stores the parent ESID
            "title_field": str,     # paired display-name field to also clear
            "is_array": bool,       # True when field type is multi_select
        }
    """
    reverse_map = {}
    for _asset_type, config in ASSET_CONFIGS.items():
        child_index = config["index"]
        for field in config.get("fields", []):
            if not field.get("store_esid"):
                continue
            source_index = field["source"]
            # follow-up: a store_esid field may omit ``esid_field``
            # when the field's own ``name`` IS the esid (e.g. the
            # superuser-only ``workspace_esid`` selector). Default to
            # ``name`` in that case so a legitimately-self-keyed field
            # doesn't blow up the cleanup-map build at import time.
            entry = {
                "child_index": child_index,
                "esid_field": field.get("esid_field") or field["name"],
                "title_field": field["name"],
                "is_array": field.get("type") == "multi_select",
            }
            reverse_map.setdefault(source_index, []).append(entry)

    # removed echarts_dashboards from ASSET_CONFIGS (the Asset
    # Explorer table-view was retired), but the ``de_asset_echarts_dashboards``
    # index still carries ``allowed_org_esids`` / ``allowed_team_esids``
    # written by the Dashboard Builder's visibility editor. Those references
    # must still be cascaded-cleared when an org or team is deleted.
    for source_index, esid_field, title_field in (
        ("de_asset_organization", "allowed_org_esids", "allowed_org_titles"),
        ("de_asset_teams", "allowed_team_esids", "allowed_team_titles"),
    ):
        reverse_map.setdefault(source_index, []).append(
            {
                "child_index": "de_asset_echarts_dashboards",
                "esid_field": esid_field,
                "title_field": title_field,
                "is_array": True,
            }
        )

    # the LEGACY SCALAR google_ads_customer_esid on de_asset_teams.
    # migrated Google Ads customers from a single scalar to the
    # multi_select array team_google_ads_customer_esids — only the array gets a
    # store_esid config, so only the array is in the reverse map above. But
    # configure_google_ads_customer still WRITES the scalar (= the primary)
    # during the transition for legacy readers, and nothing scrubs it on delete.
    # So every deleted customer leaves the scalar dangling, and the shared
    # importer billing pre-check then fetches the missing doc by id
    # and logs a 404. Cleared as a scalar (esid_field == title_field — the
    # painless _SCALAR_CLEANUP_SCRIPT removes both keys idempotently; the
    # duplicate is a harmless no-op, same as the workspace-menu entry below).
    reverse_map.setdefault("de_asset_google_ads_customers", []).append(
        {
            "child_index": "de_asset_teams",
            "esid_field": "google_ads_customer_esid",
            "title_field": "google_ads_customer_esid",
            "is_array": False,
        }
    )

    # Phase 3: the team↔bitly connection edge. The team's
    # ``bitly_connection_esid`` points at a ``de_connection`` doc (NOT a
    # de_asset_* index), so it can't be declared via ASSET_CONFIGS store_esid.
    # Registered manually so the dangling-reference audit/repair tools
    # (tools/audit_dangling_references.py) cover the edge, and so a
    # hypothetical de_connection delete cascade would clear it. Scalar:
    # esid_field == title_field (the painless _SCALAR_CLEANUP_SCRIPT removes
    # both keys idempotently — same shape as the workspace-menu entry below).
    reverse_map.setdefault("de_connection", []).append(
        {
            "child_index": "de_asset_teams",
            "esid_field": "bitly_connection_esid",
            "title_field": "bitly_connection_esid",
            "is_array": False,
        }
    )

    # the team↔TikTok connections edge. A LIST of de_connection
    # esids (one TikTok account each), so deleting a connection removes just
    # its element; the dangling-reference audit covers it too. There is no
    # paired title list, and title_field must NOT repeat esid_field: the array
    # script removes index i from both, so naming the same list twice would
    # delete a second, unrelated account. A never-written name makes the
    # script's containsKey check skip the title half.
    reverse_map.setdefault("de_connection", []).append(
        {
            "child_index": "de_asset_teams",
            "esid_field": "team_tiktok_connection_esids",
            "title_field": "team_tiktok_connection_titles",
            "is_array": True,
        }
    )

    # defensive cascade for the new workspace-menu index. When
    # a workspace is deleted (no UI path today, but the data model
    # permits it), clear the ``workspace_esid`` field from every menu
    # doc that referenced it. The orphan menu docs are then
    # inaccessible via WorkspaceMenuService.list_menus (which filters
    # by workspace_esid term), and the seed path will re-bootstrap if
    # a fresh workspace with the same esid is ever created. We pass
    # ``workspace_esid`` as both esid_field and title_field — the
    # painless SCALAR_CLEANUP_SCRIPT removes both keys idempotently
    # and the duplicate is a harmless no-op.
    reverse_map.setdefault("de_asset_workspace", []).append(
        {
            "child_index": "de_asset_workspace_menu",
            "esid_field": "workspace_esid",
            "title_field": "workspace_esid",
            "is_array": False,
        }
    )
    return reverse_map


# Built once at import time — stays in sync with ASSET_CONFIGS automatically.
REVERSE_MAP = _build_reverse_map()

# child index → its own display-title field, so the audit trail can
# name the doc that lost a reference ("team 'NORTH'") rather than just its _id.
# Indices without an ASSET_CONFIGS entry (e.g. echarts dashboards) fall back to
# an empty title — the event still carries the child id.
_INDEX_TITLE_FIELDS: dict[str, str] = {cfg["index"]: cfg.get("title_field", "") for cfg in ASSET_CONFIGS.values()}

# Painless script for removing a scalar ESID + its title field.
_SCALAR_CLEANUP_SCRIPT = """
    if (ctx._source.containsKey(params.esid_field)) {
        ctx._source.remove(params.esid_field);
    }
    if (ctx._source.containsKey(params.title_field)) {
        ctx._source.remove(params.title_field);
    }
"""

# Painless script for removing one element from paired ESID + title arrays.
_ARRAY_CLEANUP_SCRIPT = """
    if (ctx._source.containsKey(params.esid_field) && ctx._source[params.esid_field] instanceof List) {
        int idx = ctx._source[params.esid_field].indexOf(params.doc_id);
        if (idx >= 0) {
            ctx._source[params.esid_field].remove(idx);
            if (ctx._source.containsKey(params.title_field)
                    && ctx._source[params.title_field] instanceof List
                    && idx < ctx._source[params.title_field].size()) {
                ctx._source[params.title_field].remove(idx);
            }
        }
    }
"""

# Painless script for removing *multiple* elements from paired arrays (bulk).
_ARRAY_BULK_CLEANUP_SCRIPT = """
    if (ctx._source.containsKey(params.esid_field) && ctx._source[params.esid_field] instanceof List) {
        for (int i = ctx._source[params.esid_field].size() - 1; i >= 0; i--) {
            if (params.doc_ids.contains(ctx._source[params.esid_field].get(i))) {
                ctx._source[params.esid_field].remove(i);
                if (ctx._source.containsKey(params.title_field)
                        && ctx._source[params.title_field] instanceof List
                        && i < ctx._source[params.title_field].size()) {
                    ctx._source[params.title_field].remove(i);
                }
            }
        }
    }
"""


class ReferenceCleanupService:
    """Cleans orphaned ESID references after asset deletion."""

    @staticmethod
    def get_reverse_map():
        """Return the reverse relationship map (useful for debugging/tests)."""
        return REVERSE_MAP

    @staticmethod
    def get_dependents(source_index, doc_id):
        """Count child documents that reference *doc_id* in *source_index*.

        Returns a dict of ``{child_index: count}`` for non-zero counts.
        Read-only — does not modify anything.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.error("ReferenceCleanupService.get_dependents: ES unavailable")
            return {}

        entries = REVERSE_MAP.get(source_index, [])
        dependents = {}
        for entry in entries:
            try:
                # dual-term so the count is correct whether the esid
                # field is bare keyword or text+.keyword. A .keyword-only term
                # silently returned zero on bare-keyword envs (prod), making
                # every "is it safe to delete?" dependent count read as zero.
                result = es.count(
                    index=entry["child_index"],
                    body={"query": dual_term(entry["esid_field"], doc_id)},
                )
                count = result["count"]
                if count > 0:
                    dependents[entry["child_index"]] = count
            except Exception as e:
                logging.error(f"get_dependents: error counting {entry['child_index']}.{entry['esid_field']}: {e}")
        return dependents

    @staticmethod
    def _collect_affected_docs(es, entry, doc_ids):
        """fetch the child docs that reference any of *doc_ids*
        BEFORE the update-by-query strips them, so the audit trail (and the
        delete-time warning) can name what is about to lose a reference.

        Returns a list of
        ``{"child_id", "child_title", "removed": [(esid, title), ...]}``
        where each ``removed`` tuple pairs a matched esid with its display
        title from the child's paired title array (when recoverable).
        Read-only; returns ``[]`` on any error — the cleanup itself must
        never be blocked by audit bookkeeping.
        """
        child_title_field = _INDEX_TITLE_FIELDS.get(entry["child_index"], "")
        source_fields = [entry["esid_field"], entry["title_field"]]
        if child_title_field:
            source_fields.append(child_title_field)
        # One try around search AND parse: any failure here must degrade to
        # "no names in the audit event", never abort the cleanup itself.
        try:
            result = es.search(
                index=entry["child_index"],
                body={
                    # dual-terms — must match the same docs the
                    # update-by-query strip does, on both mapping shapes.
                    "query": dual_terms(entry["esid_field"], doc_ids),
                    "_source": source_fields,
                    "size": 1000,
                },
            )
            affected = []
            for hit in result.get("hits", {}).get("hits", []):
                src = hit.get("_source", {})
                esids = src.get(entry["esid_field"])
                titles = src.get(entry["title_field"])
                removed = []
                if isinstance(esids, list):
                    titles = titles if isinstance(titles, list) else []
                    for idx, esid in enumerate(esids):
                        if esid in doc_ids:
                            title = titles[idx] if idx < len(titles) else ""
                            removed.append((esid, str(title or "")))
                elif esids in doc_ids:
                    # Scalar entry: title_field may equal esid_field (self-keyed).
                    title = titles if entry["title_field"] != entry["esid_field"] else ""
                    removed.append((esids, str(title or "")))
                if removed:
                    affected.append(
                        {
                            "child_id": hit["_id"],
                            "child_title": str(src.get(child_title_field) or "") if child_title_field else "",
                            "removed": removed,
                        }
                    )
            return affected
        except Exception as e:
            logging.warning(f"_collect_affected_docs: collection failed on {entry['child_index']}: {e}")
            return []

    @staticmethod
    def _audit_removed_references(source_index, entry, affected, updated_count, doc_ids):
        """emit one ``asset_reference.removed`` event per
        (child doc, removed esid) that the delete-cascade stripped.

        When the pre-collection failed but the update-by-query still
        touched docs, a single fallback event preserves the trail (child
        ids unknown, deleted ids named). Fail-soft: audit emission never
        raises into the cleanup path.
        """
        try:
            from asset_management.web.services.audit_log_service import AuditLogService

            if affected:
                for doc in affected:
                    for esid, title in doc["removed"]:
                        AuditLogService.log_reference_removed(
                            path="delete_cascade",
                            child_index=entry["child_index"],
                            child_id=doc["child_id"],
                            child_title=doc["child_title"],
                            field=entry["esid_field"],
                            removed_esid=esid,
                            removed_title=title,
                            source_index=source_index,
                        )
            elif updated_count:
                AuditLogService.log_reference_removed(
                    path="delete_cascade",
                    child_index=entry["child_index"],
                    child_id=f"(unknown: {updated_count} doc(s))",
                    field=entry["esid_field"],
                    removed_esid=",".join(doc_ids),
                    source_index=source_index,
                )
        except Exception as e:
            logging.warning(f"_audit_removed_references: audit emission failed: {e}")

    @staticmethod
    def get_dependent_docs(source_index, doc_id):
        """like :meth:`get_dependents` but returns the actual
        referencing docs — ``[{"index", "id", "title", "field"}, ...]`` —
        so the delete confirmation can say *which* teams will lose a data
        source, not just how many docs match. Deduped by (index, id).
        Read-only.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.error("ReferenceCleanupService.get_dependent_docs: ES unavailable")
            return []

        seen = set()
        docs = []
        for entry in REVERSE_MAP.get(source_index, []):
            for doc in ReferenceCleanupService._collect_affected_docs(es, entry, [doc_id]):
                key = (entry["child_index"], doc["child_id"])
                if key in seen:
                    continue
                seen.add(key)
                docs.append(
                    {
                        "index": entry["child_index"],
                        "id": doc["child_id"],
                        "title": doc["child_title"],
                        "field": entry["esid_field"],
                    }
                )
        return docs

    @staticmethod
    def list_child_teams_of_org(org_esid):
        """return the teams still referencing *org_esid*, as
        ``[{"esid", "title"}, ...]`` sorted by title.

        Feeds the org-delete refuse gate: an org with live child teams
        must not be deleted (the old cascade merely emptied each team's
        ``organization_esid``, leaving parentless-but-active teams that
        keep importing — the orphaned-team case). Fail CLOSED: on ES error this
        raises so the caller refuses the delete rather than treating the
        org as childless (same contract as the listing sweep).
        """
        es = get_elasticsearch_client()
        if es is None:
            raise RuntimeError("ES unavailable — cannot verify the organization has no teams")
        result = es.search(
            index="de_asset_teams",
            body={
                # dual-term — organization_esid's mapping shape
                # drifts between bare keyword and text+.keyword across envs.
                "query": dual_term("organization_esid", org_esid),
                "_source": ["team_title"],
                "size": 1000,
            },
        )
        teams = [
            {"esid": hit["_id"], "title": str((hit.get("_source") or {}).get("team_title") or "")}
            for hit in result.get("hits", {}).get("hits", [])
        ]
        return sorted(teams, key=lambda t: t["title"].lower())

    @staticmethod
    def cleanup_references(source_index, doc_id):
        """Remove all references to *doc_id* from child assets.

        For scalar ESID fields the field is removed entirely.
        For array ESID fields the specific element is removed from both
        the ESID array and its paired title array.

        Returns a dict of ``{child_index: updated_count}``.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.error("ReferenceCleanupService.cleanup_references: ES unavailable")
            return {}

        entries = REVERSE_MAP.get(source_index, [])
        if not entries:
            return {}

        results = {}
        for entry in entries:
            try:
                # dual-term so the strip matches on either mapping
                # shape. A .keyword-only term silently matched nothing on
                # bare-keyword envs (prod) — dangling references were never
                # stripped. The painless script mutates by the BARE esid_field
                # name (below), independent of the query field, so widening the
                # match is safe.
                match = dual_term(entry["esid_field"], doc_id)

                # capture the affected docs BEFORE the strip so the
                # audit trail can name them (update_by_query only reports counts).
                affected = ReferenceCleanupService._collect_affected_docs(es, entry, [doc_id])

                if entry["is_array"]:
                    body = {
                        "query": match,
                        "script": {
                            "source": _ARRAY_CLEANUP_SCRIPT,
                            "lang": "painless",
                            "params": {
                                "esid_field": entry["esid_field"],
                                "title_field": entry["title_field"],
                                "doc_id": doc_id,
                            },
                        },
                    }
                else:
                    body = {
                        "query": match,
                        "script": {
                            "source": _SCALAR_CLEANUP_SCRIPT,
                            "lang": "painless",
                            "params": {
                                "esid_field": entry["esid_field"],
                                "title_field": entry["title_field"],
                            },
                        },
                    }

                response = es.update_by_query(
                    index=entry["child_index"],
                    body=body,
                    conflicts="proceed",
                    refresh=True,
                )
                updated = response.get("updated", 0)
                if updated > 0:
                    results[entry["child_index"]] = updated
                    logging.info(
                        f"Cleaned {updated} reference(s) in {entry['child_index']}.{entry['esid_field']} "
                        f"for deleted {source_index}/{doc_id}"
                    )
                    ReferenceCleanupService._audit_removed_references(source_index, entry, affected, updated, [doc_id])
            except Exception as e:
                logging.error(f"cleanup_references: error updating {entry['child_index']}.{entry['esid_field']}: {e}")
        return results

    @staticmethod
    def cleanup_references_bulk(source_index, doc_ids):
        """Remove references to *multiple* doc_ids in a single pass per child index.

        More efficient than calling cleanup_references() in a loop — uses a
        ``terms`` query so each child index is hit once regardless of how many
        documents are being deleted.

        Returns a dict of ``{child_index: updated_count}``.
        """
        if not doc_ids:
            return {}

        es = get_elasticsearch_client()
        if es is None:
            logging.error("ReferenceCleanupService.cleanup_references_bulk: ES unavailable")
            return {}

        entries = REVERSE_MAP.get(source_index, [])
        if not entries:
            return {}

        results = {}
        for entry in entries:
            try:
                # dual-terms so the bulk strip matches on either
                # mapping shape (see cleanup_references above).
                match = dual_terms(entry["esid_field"], doc_ids)

                # capture the affected docs BEFORE the strip so the
                # audit trail can name them (update_by_query only reports counts).
                affected = ReferenceCleanupService._collect_affected_docs(es, entry, doc_ids)

                if entry["is_array"]:
                    body = {
                        "query": match,
                        "script": {
                            "source": _ARRAY_BULK_CLEANUP_SCRIPT,
                            "lang": "painless",
                            "params": {
                                "esid_field": entry["esid_field"],
                                "title_field": entry["title_field"],
                                "doc_ids": doc_ids,
                            },
                        },
                    }
                else:
                    body = {
                        "query": match,
                        "script": {
                            "source": _SCALAR_CLEANUP_SCRIPT,
                            "lang": "painless",
                            "params": {
                                "esid_field": entry["esid_field"],
                                "title_field": entry["title_field"],
                            },
                        },
                    }

                response = es.update_by_query(
                    index=entry["child_index"],
                    body=body,
                    conflicts="proceed",
                    refresh=True,
                )
                updated = response.get("updated", 0)
                if updated > 0:
                    results[entry["child_index"]] = updated
                    logging.info(
                        f"Bulk-cleaned {updated} reference(s) in {entry['child_index']}.{entry['esid_field']} "
                        f"for {len(doc_ids)} deleted doc(s) from {source_index}"
                    )
                    ReferenceCleanupService._audit_removed_references(source_index, entry, affected, updated, doc_ids)
            except Exception as e:
                logging.error(f"cleanup_references_bulk: error updating {entry['child_index']}.{entry['esid_field']}: {e}")
        return results

"""Orphan-row sweep for workspace-tier cascade indices.

 defense-in-depth. The workspace-delete cascade
(``permissions_index_service._delete_workspace_cascade``) reliably purges
child rows for any workspace deleted via the supported delete paths
earlier (2026-05-22). Historic deletes that happened BEFORE that
cascade landed left orphan rows behind in the six workspace-keyed
indices — confirmed on staging 2026-05-25 (2 membership rows + 5 team
rows + 46 workspace_menu rows pointing at two workspaces deleted on
2026-05-18/19, several days before the cascade existed).

This module sweeps those orphans at process startup. The sweep is:

* **Workspace-tier only.** The six indices match the cascade's workspace-
  keyed branch in ``_delete_workspace_cascade`` (steps 2 + extended in
  ). Org / team / wizard-asset / connection cascades are out of
  scope — their parents are workspaces, so the transitive purge would
  fire on any historic-orphan workspace AFTER this sweep removed it,
  if/when we extend coverage.

* **Idempotent.** Runs once on startup; a second invocation finds zero
  orphans and exits with all-zero counts.

* **Fail-soft.** A bad ES connection / missing index / ES exception
  during one index's sweep is logged and skipped — startup must not
  block on this defense layer.

* **Safety-gated.** If ``de_asset_workspace`` is empty or missing, the
  sweep returns immediately. Without that guard, an empty workspace
  index would interpret EVERY row in every cascade index as an orphan
  and wipe them all on cold boot.

The startup call site (``app.py``) treats the sweep the same way it
treats ``RolePermissionsService.seed_defaults()``: try-block with
warn-on-failure, no exception propagation.
"""

from __future__ import annotations

import logging
import sys

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client


# ── Index cascade contract ──────────────────────────────────────────────────
# Mirrors the workspace-keyed branch of permissions_index_service.
# _delete_workspace_cascade (steps 2 + extension). reindexed
# de_asset_teams and de_asset_echarts_dashboards to a keyword workspace_esid,
# so every index here uses the bare field (the per-index.keyword
# selection has been reverted).
_WORKSPACE_SCOPE_INDICES: tuple[tuple[str, str], ...] = (
    ("de_authz_workspace_membership", "workspace_esid"),
    ("de_authz_user_workspace_role", "workspace_esid"),
    # the org index is ``de_asset_organization`` (singular)
    # everywhere in the app (ASSET_CONFIGS, ORGANIZATION_INDEX). This entry
    # said ``de_asset_organizations`` (plural), so the
    # workspace-tier sweep targeted a non-existent index and silently
    # 404-skipped it — orphan org docs were never swept. Corrected to the
    # real index name.
    ("de_asset_organization", "workspace_esid"),
    ("de_asset_teams", "workspace_esid"),
    ("de_asset_workspace_menu", "workspace_esid"),
    ("de_asset_echarts_dashboards", "workspace_esid"),
)

# index + field for the org→team orphan-ref sweep. A team's
# ``organization_esid`` (multi-org array) can reference orgs that
# no longer exist when an org was deleted via a path that did not cascade the
# reference cleanup (or before that cleanup covered orgs). A team whose orgs
# are ALL dangling dead-ends the team→org→workspace graph walk, hiding its
# workspace from Asset Explorer / Data Inventory (the root cause).
# This sweep scrubs dead org ESIDs from teams at startup, defense-in-depth
# alongside the heal-on-read direct workspace_esid leg.
_TEAMS_INDEX = "de_asset_teams"
_ORGANIZATION_INDEX = "de_asset_organization"

_WORKSPACE_INDEX = "de_asset_workspace"

# ── Connection-orphan sweep ────────────────────────────────────────
# de_connection holds a live OAuth token (+ refresh token) keyed ONLY by
# ``principal`` (a username). It has no workspace_esid field and the
# workspace↔connection link table (de_workspace_connection_asset) has no
# writers yet, so a connection cannot be matched to its
# originating workspace. The workspace-delete cascade
# (permissions_index_service.cascade_purge_empty_workspace, step 3.6) purges
# connections by principal-in-zero-workspaces, but ONLY for principals it can
# enumerate from surviving membership rows at delete time. When those rows are
# already gone (the test-account case found on prod 2026-05-26:
# de_authz_workspace_membership=0), the principal is unknowable and the
# connection survives with a live token and no owning workspace.
#
# This sweep is the defense-in-depth layer for that case: a connection whose
# ``principal`` has ZERO workspace memberships owns a token no workspace can
# reach, so it is swept (token + child asset rows).
_CONNECTION_INDEX = "de_connection"
_CONNECTION_ASSET_INDEX = "de_connection_asset"
# (P3a): the orphan-connection sweep decides "principal belongs to no
# workspace" by counting workspace docs for that principal. The source of truth
# moved from de_authz_workspace_membership to the workspace ROLE index
# (membership == has-a-workspace-role now), so we count there. Same safety
# contract: an empty/missing index means we CANNOT tell who is orphaned, so the
# sweep skips entirely rather than risk wiping every live OAuth token.
_WORKSPACE_PRINCIPAL_INDEX = "de_authz_user_workspace_role"

# (P3b): the membership-without-role report that briefly lived here
# (Phase 1) is retired along with the membership indices themselves — there is
# no membership doc to audit any more. Scoped roles are the single source of
# truth; see tools/retire_workspace_membership.py for the one-shot migration.


class OrphanSweepService:
    """Startup sweep for orphan rows in workspace-tier cascade indices."""

    @staticmethod
    def _live_workspace_esids(es) -> set[str] | None:
        """Return the set of currently-live workspace doc ids.

        ``None`` signals "do not run the sweep" — either ES is
        unreachable or the workspace index is missing/empty. The caller
        MUST treat ``None`` and ``set()`` differently: ``None`` means
        "skip", ``set()`` would mean "delete everything" which is never
        the right answer for a defense-in-depth pass.
        """
        try:
            result = es.search(
                index=_WORKSPACE_INDEX,
                body={"query": {"match_all": {}}, "_source": False, "size": 10000},
            )
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                logging.info(
                    "OrphanSweepService: %s missing — skipping sweep " "(cold-boot before workspace seed is normal).",
                    _WORKSPACE_INDEX,
                )
            else:
                logging.warning(
                    "OrphanSweepService: could not enumerate live workspaces: %s",
                    e,
                )
            return None

        live = {h["_id"] for h in result.get("hits", {}).get("hits", [])}
        if not live:
            logging.info(
                "OrphanSweepService: %s is empty — skipping sweep "
                "(refuses to interpret 'no workspaces' as 'delete all rows').",
                _WORKSPACE_INDEX,
            )
            return None
        return live

    @staticmethod
    def sweep_workspace_tier() -> dict[str, int]:
        """Delete rows whose workspace_esid is not in de_asset_workspace.

        Iterates the six workspace-keyed cascade indices and runs one
        ``delete_by_query`` per index with a ``must_not terms`` filter
        against the live-workspace set. Returns ``{index: deleted_count}``
        for caller-side audit / logging.

        Per-index failures are logged and skipped — the sweep is best-
        effort defense, not a tripwire that should block startup.
        """
        counts: dict[str, int] = {}
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("OrphanSweepService: no ES client — skipping sweep.")
            return counts

        live_ws = OrphanSweepService._live_workspace_esids(es)
        if live_ws is None:
            return counts

        live_list = list(live_ws)
        for index, field in _WORKSPACE_SCOPE_INDICES:
            try:
                resp = es.delete_by_query(
                    index=index,
                    body={"query": {"bool": {"must_not": {"terms": {field: live_list}}}}},
                    refresh=True,
                    conflicts="proceed",
                )
                deleted = int(resp.get("deleted", 0))
                counts[index] = deleted
                if deleted:
                    logging.info(
                        "OrphanSweepService: deleted %d orphan row(s) from %s",
                        deleted,
                        index,
                    )
            except Exception as e:
                err = str(e).lower()
                if "index_not_found" in err or "404" in err:
                    counts[index] = 0
                    continue
                logging.warning(
                    "OrphanSweepService: sweep failed for %s: %s",
                    index,
                    e,
                )
                counts[index] = 0
        return counts

    @staticmethod
    def sweep_orphan_connections() -> dict[str, int]:
        """Delete de_connection + de_connection_asset rows whose principal
        holds no workspace membership.

        A ``de_connection`` is keyed only by ``principal``. A principal with
        ZERO ``de_authz_workspace_membership`` rows has no workspace from
        which to reach (or manage) the token — it is an orphan holding a live
        OAuth credential. This sweep removes those connections and their
        child asset rows.

        Returns ``{index: deleted_count}`` for ``de_connection`` and
        ``de_connection_asset`` (audit / logging).

        Safety-gated, mirroring ``sweep_workspace_tier``:

        * If the membership index is **missing or empty**, the sweep returns
          ``{}`` and deletes nothing. Without this guard a cold boot before
          the membership index is populated would read EVERY connection's
          principal as "in zero workspaces" and wipe all tokens. "No
          memberships" is never "delete all tokens".
        * Fail-soft: ES errors are logged and treated as no-op, never raised
          — this is a startup defense layer, not a tripwire.
        """
        counts: dict[str, int] = {}
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("OrphanSweepService: no ES client — skipping connection sweep.")
            return counts

        # Safety gate: a missing/empty membership index means we cannot tell
        # who is orphaned. Skip rather than risk wiping every token.
        try:
            membership_total = es.count(
                index=_WORKSPACE_PRINCIPAL_INDEX,
                body={"query": {"match_all": {}}},
            )
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                logging.info(
                    "OrphanSweepService: %s missing — skipping connection "
                    "sweep (cold boot before membership seed is normal).",
                    _WORKSPACE_PRINCIPAL_INDEX,
                )
            else:
                logging.warning(
                    "OrphanSweepService: could not count memberships — " "skipping connection sweep: %s",
                    e,
                )
            return counts

        if int(membership_total.get("count", 0)) == 0:
            logging.info(
                "OrphanSweepService: %s is empty — skipping connection sweep "
                "(refuses to interpret 'no memberships' as 'delete all "
                "tokens').",
                _WORKSPACE_PRINCIPAL_INDEX,
            )
            return counts

        # Enumerate every distinct principal that currently owns a connection.
        # composite agg paginates over all principals without a cardinality
        # cap (terms-agg size limits would silently miss principals).
        connection_principals: set[str] = set()
        after: dict | None = None
        try:
            while True:
                composite: dict = {
                    "size": 1000,
                    "sources": [{"p": {"terms": {"field": "principal"}}}],
                }
                if after is not None:
                    composite["after"] = after
                resp = es.search(
                    index=_CONNECTION_INDEX,
                    body={"size": 0, "aggs": {"principals": {"composite": composite}}},
                )
                agg = resp.get("aggregations", {}).get("principals", {})
                buckets = agg.get("buckets", [])
                for b in buckets:
                    val = b.get("key", {}).get("p")
                    if val:
                        connection_principals.add(val)
                after = agg.get("after_key")
                if not after or not buckets:
                    break
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                logging.info(
                    "OrphanSweepService: %s missing — no connections to sweep.",
                    _CONNECTION_INDEX,
                )
                return counts
            logging.warning(
                "OrphanSweepService: could not enumerate connection " "principals — skipping connection sweep: %s",
                e,
            )
            return counts

        # For each principal, an orphan iff it has zero workspace memberships.
        orphan_principals: list[str] = []
        for principal in connection_principals:
            try:
                resp = es.count(
                    index=_WORKSPACE_PRINCIPAL_INDEX,
                    body={"query": {"term": {"username": principal}}},
                )
                remaining = int(resp.get("count", 0))
            except Exception as e:
                # Conservative: a lookup failure means we cannot prove the
                # principal is orphaned, so we leave its token alone.
                logging.warning(
                    "OrphanSweepService: membership count failed for %s — " "leaving its connection in place: %s",
                    principal,
                    e,
                )
                continue
            if remaining == 0:
                orphan_principals.append(principal)

        if not orphan_principals:
            counts[_CONNECTION_INDEX] = 0
            counts[_CONNECTION_ASSET_INDEX] = 0
            return counts

        # Purge children first, then parents — same ordering as a clean
        # manual delete, so a mid-sweep failure never leaves a parent with
        # dangling children (the reverse can leave childless asset rows, but
        # those are harmless and caught next boot).
        for index in (_CONNECTION_ASSET_INDEX, _CONNECTION_INDEX):
            try:
                resp = es.delete_by_query(
                    index=index,
                    body={"query": {"terms": {"principal": orphan_principals}}},
                    refresh=True,
                    conflicts="proceed",
                )
                deleted = int(resp.get("deleted", 0))
                counts[index] = deleted
                if deleted:
                    logging.info(
                        "OrphanSweepService: deleted %d orphan-connection " "row(s) from %s (principals=%s)",
                        deleted,
                        index,
                        orphan_principals,
                    )
            except Exception as e:
                err = str(e).lower()
                if "index_not_found" in err or "404" in err:
                    counts[index] = 0
                    continue
                logging.warning(
                    "OrphanSweepService: connection sweep failed for %s: %s",
                    index,
                    e,
                )
                counts[index] = 0
        return counts

    @staticmethod
    def _live_organization_esids(es) -> set[str] | None:
        """Return the set of currently-live org doc ids, or ``None`` to skip.

        ``None`` signals "do not run the org→team sweep": ES unreachable or
        the org index missing/empty. As with ``_live_workspace_esids`` the
        caller MUST treat ``None`` and ``set()`` identically — an empty org
        index would make EVERY team's ``organization_esid`` look dangling and
        scrub them all, which is never the right answer for a defense pass.
        """
        try:
            result = es.search(
                index=_ORGANIZATION_INDEX,
                body={"query": {"match_all": {}}, "_source": False, "size": 10000},
            )
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                logging.info(
                    "OrphanSweepService: %s missing — skipping org→team sweep " "(cold-boot before org seed is normal).",
                    _ORGANIZATION_INDEX,
                )
            else:
                logging.warning(
                    "OrphanSweepService: could not enumerate live orgs — " "skipping org→team sweep: %s",
                    e,
                )
            return None

        live = {h["_id"] for h in result.get("hits", {}).get("hits", [])}
        if not live:
            logging.info(
                "OrphanSweepService: %s is empty — skipping org→team sweep "
                "(refuses to interpret 'no orgs' as 'scrub every team's org "
                "refs').",
                _ORGANIZATION_INDEX,
            )
            return None
        return live

    @staticmethod
    def sweep_orphan_team_org_refs() -> dict[str, int]:
        """Scrub dead ``organization_esid`` refs from team docs.

        A ``de_asset_teams`` doc holds ``organization_esid`` (a list since
        , occasionally a scalar string) linking it to its parent
        org(s). When an org is deleted by a path that did not cascade the
        reference cleanup (``ReferenceCleanupService``) — or before that
        cleanup covered orgs — the dead ESID lingers on the team. A team whose
        org refs ALL dead-end breaks the team→org→workspace graph walk
        (``_workspace_esids_from_scope_esids``), hiding its workspace from the
        graph-walking surfaces (Asset Explorer, Data Inventory). That is the
         root cause.

        This sweep removes any ``organization_esid`` element that does not
        resolve to a live org doc, leaving live refs intact. A team whose only
        org was deleted ends up with an empty ``organization_esid`` — still
        graph-orphaned, but the heal-on-read direct ``workspace_esid``
        leg surfaces its workspace, and the stale ref no longer pollutes any
        org-keyed walk.

        Returns ``{de_asset_teams: updated_count}``.

        Safety-gated exactly like ``sweep_workspace_tier``: a missing/empty org
        index means we cannot tell which refs are dead, so the sweep skips
        rather than scrub every team. Fail-soft: ES errors are logged, never
        raised — this is a startup defense layer.
        """
        counts: dict[str, int] = {}
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("OrphanSweepService: no ES client — skipping org→team sweep.")
            return counts

        live_orgs = OrphanSweepService._live_organization_esids(es)
        if live_orgs is None:
            return counts

        live_list = list(live_orgs)
        # Painless: keep only org ESIDs that are in the live set. Handles both
        # the list shape (the norm) and a legacy scalar string. ``ctx``
        # is only written when something actually changes, so update_by_query's
        # ``updated`` count reflects real scrubs (no-op docs are not rewritten).
        script = """
            if (ctx._source.containsKey('organization_esid')) {
                def v = ctx._source.organization_esid;
                if (v instanceof List) {
                    def kept = new ArrayList();
                    for (e in v) { if (params.live.contains(e)) { kept.add(e); } }
                    if (kept.size() != v.size()) {
                        ctx._source.organization_esid = kept;
                    } else {
                        ctx.op = 'noop';
                    }
                } else if (v != null) {
                    if (!params.live.contains(v)) {
                        ctx._source.organization_esid = new ArrayList();
                    } else {
                        ctx.op = 'noop';
                    }
                } else {
                    ctx.op = 'noop';
                }
            } else {
                ctx.op = 'noop';
            }
        """
        try:
            resp = es.update_by_query(
                index=_TEAMS_INDEX,
                body={
                    # Only visit teams that actually reference SOME org not in
                    # the live set — cheap pre-filter so a healthy cluster's
                    # sweep touches nothing. ``exists`` keeps it to teams that
                    # have the field at all; the script does the precise check.
                    "query": {"bool": {"must": [{"exists": {"field": "organization_esid"}}]}},
                    "script": {
                        "source": script,
                        "lang": "painless",
                        "params": {"live": live_list},
                    },
                },
                conflicts="proceed",
                refresh=True,
            )
            updated = int(resp.get("updated", 0))
            counts[_TEAMS_INDEX] = updated
            if updated:
                logging.info(
                    "OrphanSweepService: scrubbed dead org refs from %d team(s)",
                    updated,
                )
        except Exception as e:
            err = str(e).lower()
            if "index_not_found" in err or "404" in err:
                counts[_TEAMS_INDEX] = 0
            else:
                logging.warning(
                    "OrphanSweepService: org→team sweep failed for %s: %s",
                    _TEAMS_INDEX,
                    e,
                )
                counts[_TEAMS_INDEX] = 0
        return counts

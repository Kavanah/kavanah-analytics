# Import the es_helpers module
# asset reads go through the paginated fetch — the single-shot
# get_all_documents silently truncates at its size cap (1000) once an asset
# index outgrows it, dropping assets from the import in arbitrary ES order.
from lib.es_helpers import get_all_documents_paginated
import logging
import os


# the FB team-filter resolvers match a `--team_ids` token to a team
# by its ESID (the doc _id) — exact, collision-free. A team *title* is NOT a
# safe key: two teams in different workspaces can share a title (e.g. both
# "Kavanah Team"), so any title match (substring OR exact) scopes the import to
# BOTH workspaces' teams — leaking one workspace's data into the other's run
# (the same-title class, in the import-filter path). The import-data UI
# and the report scheduler both send the team ESID, so title matching
# is needed only by legacy cron/CLI callers that still pass a title. It is OFF
# by default and gated behind this opt-in env flag; when enabled it does
# case-insensitive EXACT title equality (never substring) and logs a loud
# WARNING on every use, because it can still collide on identical cross-workspace
# titles. There is no in-product path that sets this flag.
_TITLE_MATCH_ENV = "FB_IMPORT_ALLOW_TITLE_MATCH"


def _title_match_allowed():
    """Is the legacy title-match escape hatch enabled? (env-gated, off by default)."""
    return os.environ.get(_TITLE_MATCH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def team_matches_team_ids(team_esid, team_title, team_ids_filter):
    """the ONE per-team ``--team_ids`` predicate, shared by every
    FB team-filter resolver (``resolve_team_filter_scope`` here and
    ``_team_id_filter_matches`` in get_page_insights.py).

    A token matches when it equals the team's ESID (case-insensitive exact —
    the collision-safe doc ``_id``). A team *title* matches ONLY under the
    ``FB_IMPORT_ALLOW_TITLE_MATCH`` legacy escape hatch — case-insensitive
    EXACT (never substring), logged loudly per match, because identical
    cross-workspace titles can still collide.

    Before this helper the two resolvers carried duplicate copies of these
    rules, which is exactly how they drifted apart once already (the
    title-only vs ESID mismatch). Keep ALL matching-rule changes here.
    """
    tokens = [t.strip().lower() for t in str(team_ids_filter or "").split(",") if t.strip()]
    if not tokens:
        return False
    esid = (team_esid or "").lower()
    if esid and esid in tokens:
        return True
    if _title_match_allowed():
        title = (team_title or "").strip().lower()
        if title and title in tokens:
            logging.warning(
                "team_matches_team_ids: LEGACY title match for team_ids=%r matched team %r (%s) "
                "via %s — title is not a collision-safe key across workspaces; pass team ESIDs",
                team_ids_filter,
                team_title,
                team_esid,
                _TITLE_MATCH_ENV,
            )
            return True
    return False


# Function to build Elasticsearch query
def build_es_query(filters):
    # every de_asset_* index is now explicit keyword (both envs
    # reindexed 2026-07-06/07; audit reported 0 broken). A bare ``term`` /
    # ``terms`` against a foreign-key field like ``fb_account_esid`` matches
    # the keyword literal directly — no ``.keyword`` subfield exists anymore.
    # The ``_term_or_keyword_clause`` dual-shape ``should`` wrapper
    # (which tolerated the old ``text + .keyword`` drift) is removed: if the
    # mapping ever re-drifts, a bare term now finds nothing and the import
    # fails loudly (surfaced via the -class error reporting) instead
    # of being silently absorbed. Guarded -FBIMPORT-003.
    if not filters:
        # If no filters, return match_all query
        return {"match_all": {}}

    must_clauses = []
    for key, value in filters.items():
        if isinstance(value, list):
            # Use 'terms' for multiple values
            if key == "_id":
                must_clauses.append({"ids": {"values": value}})
            else:
                must_clauses.append({"terms": {key: value}})
        else:
            # Use 'term' for single value
            if key == "_id":
                must_clauses.append({"ids": {"values": [value]}})
            else:
                must_clauses.append({"term": {key: value}})

    return {"bool": {"must": must_clauses}}


# summarize a build_es_query body into the meaningful identifiers
# (ids + field=value filters) for a readable log line, instead of dumping the raw
# bool/must DSL. Best-effort: an unrecognised shape falls back to a compact
# repr so we never crash on a future query form.
def _summarize_query(query):
    try:
        must = (query or {}).get("bool", {}).get("must")
        if not must:
            return "match_all" if (query or {}).get("match_all") is not None else str(query)
        parts = []
        for clause in must:
            if "ids" in clause:
                vals = clause["ids"].get("values") or []
                parts.append("id=" + (",".join(map(str, vals)) if vals else "<none>"))
                continue
            # clauses are bare term/terms again (the should-
            # wrapper is gone). Still tolerate a legacy .keyword field name if
            # a caller passes one explicitly — collapse it to the bare field.
            body = clause.get("term") or clause.get("terms")
            if not body:
                continue
            field, value = next(iter(body.items()))
            if field.endswith(".keyword"):
                field = field[: -len(".keyword")]
            parts.append(f"{field}={value}")
        return ", ".join(parts) if parts else str(query)
    except Exception:  # noqa: BLE001 — logging helper must never raise
        return str(query)


# Empty-result helper. a "no results" lookup is an EXPECTED negative for
# these asset resolvers (e.g. a page-filtered team has no account_wide row), not an
# operator-actionable problem, so log at INFO with a short human summary rather than
# a WARNING dumping the raw query DSL.
def handle_no_results(index, query):
    logging.info("No match in %s for %s", index, _summarize_query(query))
    return []


# Function to get Facebook accounts
def get_fb_accounts(filters=None):
    index = "de_asset_fb_accounts"

    # Build the Elasticsearch query
    query = build_es_query(filters)

    try:
        # Call es_helpers to get all documents from the index
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Facebook accounts: {e}")
        return handle_no_results(index, query)


# Function to get Facebook ad accounts
def get_fb_ad_accounts(filters=None):
    index = "de_asset_fb_ad_accounts"

    # Build the Elasticsearch query
    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Facebook ad accounts: {e}")
        return handle_no_results(index, query)


# Function to get Facebook apps
def get_fb_apps(filters=None):
    index = "de_asset_fb_apps"

    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Facebook apps: {e}")
        return handle_no_results(index, query)


# Function to get Facebook pages
def get_fb_pages(filters=None):
    index = "de_asset_fb_pages"

    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Facebook pages: {e}")
        return handle_no_results(index, query)


# Function to get teams
def get_fb_teams(filters=None):
    index = "de_asset_teams"

    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving teams: {e}")
        return handle_no_results(index, query)


# Function to get team_url by facebook page slug
import logging


# Function to get the full team document by Facebook page slug
def get_team_by_fb_page_slug(fb_page_slug):
    # Step 1: Query the Facebook page using the slug
    fb_pages_filters = {"fb_page_slug": fb_page_slug}
    fb_pages_docs = get_fb_pages(filters=fb_pages_filters)

    # If no Facebook page is found, return False
    if not fb_pages_docs:
        logging.warning(f"No Facebook page found for slug: {fb_page_slug}")
        return False

    # Assuming we only care about the first matching document, get its _id
    fb_page_esid = fb_pages_docs[0]["_id"]

    # Step 2: Query the teams index using the fb_page_esid
    teams_filters = {"fb_page_esid": fb_page_esid}
    teams_docs = get_fb_teams(filters=teams_filters)

    # If no team is found, return False
    if not teams_docs:
        logging.warning(f"No team found for Facebook page _id: {fb_page_esid}")
        return False

    # Return the first matching team document (full document)
    return teams_docs[0]["_source"]


# Function to get Instagram accounts
def get_ig_accounts(filters=None):
    """
    Get Instagram accounts from de_asset_ig_accounts index.

    Args:
        filters: Optional dict of field:value filters

    Returns:
        List of Instagram account documents
    """
    index = "de_asset_ig_accounts"

    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving Instagram accounts: {e}")
        return handle_no_results(index, query)


# Function to get Facebook app users
def get_fb_app_users(filters=None):
    """
    Get Facebook app users from de_asset_fb_app_users index.

    Args:
        filters: Optional dict of field:value filters

    Returns:
        List of FB app user documents
    """
    index = "de_asset_fb_app_users"

    query = build_es_query(filters)

    try:
        results = get_all_documents_paginated(index, query)
        if not results:
            return handle_no_results(index, query)
        return results
    except Exception as e:
        logging.error(f"Error retrieving FB app users: {e}")
        return handle_no_results(index, query)


def get_fb_app_user_for_instagram(fb_app_esid):
    """
    Get the appropriate FB app user for Instagram API access.

    Args:
        fb_app_esid: The ESID of the Facebook app

    Returns:
        FB app user document (with _id and _source) or None
    """
    # Get all users for this app
    app_users = get_fb_app_users(filters={"fb_app_esid": fb_app_esid})

    if not app_users:
        logging.warning(f"No FB app users found for app ESID: {fb_app_esid}")
        return None

    # Prefer users with role="Pages" since Instagram is linked through Facebook Pages
    pages_users = [u for u in app_users if u["_source"].get("fb_app_user_role") == "Pages"]

    if pages_users:
        logging.debug(f"Found {len(pages_users)} Pages user(s) for app ESID {fb_app_esid}")
        return pages_users[0]

    # Fall back to first available user
    logging.debug(f"No Pages users found, using first available user for app ESID {fb_app_esid}")
    return app_users[0]


def get_fb_app_user_for_pages(fb_app_esid):
    """
    Get FB app user with Pages role for page insights API access.
    Similar to get_fb_app_user_for_instagram().

    Args:
        fb_app_esid: The ESID of the Facebook app

    Returns:
        FB app user document (with _id and _source) or None
    """
    # Get all users for this app
    app_users = get_fb_app_users(filters={"fb_app_esid": fb_app_esid})

    if not app_users:
        logging.warning(f"No FB app users found for app ESID: {fb_app_esid}")
        return None

    # Require users with role="Pages" for page insights access
    pages_users = [u for u in app_users if u["_source"].get("fb_app_user_role") == "Pages"]

    if pages_users:
        logging.debug(f"Found {len(pages_users)} Pages user(s) for app ESID {fb_app_esid}")
        return pages_users[0]

    # No fallback - Pages role is required
    logging.warning(f"No Pages users found for app ESID {fb_app_esid}. Pages role is required for page insights.")
    return None


def resolve_team_filter_scope(team_ids):
    """Resolve a `--team_ids` filter to the upstream asset ESIDs it covers.

    Without this resolver, the FB import pipeline loaded every fb_account →
    fb_app → fb_ad_account before applying the team filter. With
    it, every layer of the asset chain can be scoped at query time.

    Args:
        team_ids: Comma-separated team ESIDs (the collision-safe key sent by
            the UI, wizard, and report scheduler), or any falsy value
            (False / None / "") to indicate "no team filter — preserve
            unfiltered behavior". A team title is matched only when the legacy
            escape hatch ``FB_IMPORT_ALLOW_TITLE_MATCH`` is enabled (exact,
            logged);.

    Returns:
        None if `team_ids` is falsy → caller should NOT scope.

        dict otherwise:
            {
                "team_ids":           comma-separated normalized team titles,
                "team_esids":         set[str] of matched team _ids,
                "fb_account_esids":   set[str] of fb_account _ids reachable
                                      from the matched teams via fb_pages,
                "fb_app_esids":       set[str] of fb_app _ids whose
                                      fb_account_esid is in the above set,
                "fb_ad_account_esids":set[str] of fb_ad_account _ids whose
                                      fb_app_esid is in the above set,
            }

        Empty sets are possible at any layer if the configured assets
        don't link cleanly. Callers must defensively skip scoping (and
        fall back to unfiltered behavior) when an inner set is empty,
        since narrowing the chain to {} would skip the import entirely.
    """
    if not team_ids:
        return None

    raw_terms = [t.strip() for t in team_ids.split(",") if t.strip()]
    if not raw_terms:
        return None

    # team_ids may be EITHER team_esids (the wizard kickoff
    # path _resolve_connection_to_team_filter, the import-data
    # UI, and the report scheduler — all send the resolved ESID)
    # OR a team title (legacy cron/CLI only).
    #
    # the ESID/title matching rules live in the shared
    # team_matches_team_ids predicate (also used by the page importer's
    # _team_id_filter_matches) so the two resolvers can't drift again.
    all_fb_teams = get_fb_teams(filters={"ds_facebook": "true"})
    matched_teams = [t for t in all_fb_teams if team_matches_team_ids(t["_id"], t["_source"].get("team_title"), team_ids)]
    if not matched_teams:
        logging.warning(f"resolve_team_filter_scope: no teams matched team_ids={team_ids!r}")
        return {
            "team_ids": team_ids,
            "team_esids": set(),
            "fb_account_esids": set(),
            "fb_app_esids": set(),
            "fb_ad_account_esids": set(),
            "all_account_wide": False,
        }

    team_esids = {t["_id"] for t in matched_teams}

    # every matched team has fb_import_mode="account_wide" =>
    # the page-filtered asset chain (fb_accounts → fb_apps → fb_ad_accounts)
    # has nothing to do for this scope. Pre-fix, the caller's check at
    # get_insights.py:908 saw the empty fb_ad_account_esids that resulted
    # from wizard teams not having team_facebook_page_esids, and DROPPED
    # the scope entirely — causing the importer to fan out to every
    # legacy fb_app's page-filtered chain. The all_account_wide flag
    # lets the caller distinguish "team is misconfigured / legacy with
    # broken page chain" (fall back to unfiltered) from "team is
    # wizard-cohort, all its data flows through account_wide" (skip the
    # page-filtered walk entirely).
    all_account_wide = all((t["_source"].get("fb_import_mode") == "account_wide") for t in matched_teams)
    page_esids = set()
    page_ids_for_recovery = set()
    for t in matched_teams:
        for page_esid in t["_source"].get("team_facebook_page_esids", []) or []:
            if page_esid:
                page_esids.add(page_esid)
        # collect raw page IDs from the source-of-truth field for
        # by-value recovery below. team_facebook_page_id is what the importer
        # reads (get_insights.py:221); team_facebook_page_esids is a companion
        # that can legitimately diverge for teams configured before the dual-
        # write was wired up, or when an fb_pages doc is deleted/recreated.
        # Without this recovery, teams shaped like this resolve to 0 fb_accounts and
        # the importer falls back to scanning every ad account in the system,
        # burning rate limit on accounts unrelated to the request.
        for page_id in t["_source"].get("team_facebook_page_id", []) or []:
            if page_id:
                page_ids_for_recovery.add(str(page_id))

    # Layer 2: pages → fb_accounts. Pages may live under multiple FB accounts
    # for the same team (e.g. a team that owns pages across two business
    # portfolios). Collect every parent fb_account_esid we find.
    fb_account_esids = set()
    resolved_page_docs = []
    if page_esids:
        resolved_page_docs.extend(get_fb_pages(filters={"_id": list(page_esids)}))

    # by-value recovery. If any raw page IDs weren't represented by
    # a resolved ESID, look them up directly against fb_page_id. This mirrors
    # the renderer-side recovery.
    recovered_count = 0
    if page_ids_for_recovery:
        already_resolved_page_ids = {
            str(p["_source"].get("fb_page_id")) for p in resolved_page_docs if p["_source"].get("fb_page_id") is not None
        }
        unresolved_page_ids = page_ids_for_recovery - already_resolved_page_ids
        if unresolved_page_ids:
            recovery_docs = get_fb_pages(filters={"fb_page_id": list(unresolved_page_ids)})
            resolved_page_docs.extend(recovery_docs)
            recovered_count = len(recovery_docs)
            if recovered_count:
                logging.info(
                    f"resolve_team_filter_scope: recovered {recovered_count} page(s) "
                    f"by team_facebook_page_id for team_ids={team_ids!r} "
                    f"(team_facebook_page_esids was empty or stale; )"
                )

    for p in resolved_page_docs:
        account_esid = p["_source"].get("fb_account_esid")
        if account_esid:
            fb_account_esids.add(account_esid)

    # Layer 3: fb_accounts → fb_apps.
    fb_app_esids = set()
    if fb_account_esids:
        app_docs = get_fb_apps(filters={"fb_account_esid": list(fb_account_esids)})
        fb_app_esids = {a["_id"] for a in app_docs}

    # Layer 4: fb_apps → fb_ad_accounts.
    fb_ad_account_esids = set()
    if fb_app_esids:
        ad_account_docs = get_fb_ad_accounts(filters={"fb_app_esid": list(fb_app_esids)})
        fb_ad_account_esids = {a["_id"] for a in ad_account_docs}

    logging.info(
        f"resolve_team_filter_scope({team_ids!r}): "
        f"{len(team_esids)} team(s) → {len(fb_account_esids)} fb_account(s) "
        f"→ {len(fb_app_esids)} fb_app(s) → {len(fb_ad_account_esids)} fb_ad_account(s)"
    )

    return {
        "team_ids": team_ids,
        "team_esids": team_esids,
        "fb_account_esids": fb_account_esids,
        "fb_app_esids": fb_app_esids,
        "fb_ad_account_esids": fb_ad_account_esids,
        "all_account_wide": all_account_wide,
    }

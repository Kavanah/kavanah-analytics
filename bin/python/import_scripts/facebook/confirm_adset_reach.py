"""
 confirmation harness — does windowed reach at the Ad Sets table's
filter grain (ad account + campaign + publisher_platform) match Data Studio?

The Ad Sets table (``meta_ad_sets`` template) lets the user filter by platform,
ad account, and campaign. Reach is non-additive across ads, so we must learn HOW
Data Studio computes the reach the table is supposed to match before we build the
consuming side. This tool prints, for ONE slice, the candidate numbers
so they can be compared against a Data Studio reference the operator pulls by hand.

For a given ad_account + campaign + platform + window it prints:

  1. FB campaign-grain windowed reach  — ONE FB call at level=campaign, filtered
     to the campaign, broken down by publisher_platform. This is FB's own
     deduplicated reach for the slice; if Data Studio matches THIS, the table
     must query FB at the slice grain (or store a campaign-grain window doc),
     NOT sum per-ad docs.

  2. FB per-ad windowed reach, SUMMED across the campaign's ads — what summing
     our stored per-ad window docs reproduces. If Data Studio matches THIS (and
     #1 is lower), Data Studio is itself summing per-row reach and our stored
     per-ad window docs are the correct source.

  3. ES stored *_fb_insights_window reach for the slice (sum) — what
     docs would currently serve if the table read them.

  4. ES stored *_fb_insights (per-day) reach for the slice (sum) — TODAY's
     (over-counting) Ad Sets table value, for reference.

The Ad Sets table shows ONE ROW PER AD SET; the platform/account/campaign filters
just narrow which rows show. So the decisive question is: does FB's ad-set-grain
deduplicated reach ([1b], printed per ad set) equal each Data Studio row? If yes,
 should store window docs at AD-SET x publisher_platform grain and each
table row reads its own doc (no summing, exact DS match under any filter combo).

Interpretation:
  - Each [1b] per-ad-set row == its DS row -> store at ad-set x platform grain. ✓
  - within-ad-set over-count ([2]/[1b]) ~1.00x -> ad sets are ~single-ad.
  - within-ad-set over-count >1.00x -> ad sets have multiple ads; per-ad storage
    summed to a row would over-count -> ad-set grain is REQUIRED.

Usage::

    # Account-wide (every ad set, matches the no-campaign Data Studio view):
    docker exec -w /app/python imports_container python3 \\
        -m import_scripts.facebook.confirm_adset_reach \\
        --ad_account_id 456001023325657 \\
        --platform facebook \\
        --since 2026-05-25 --until 2026-06-01 \\
        --team_title <TEAM_TITLE>

    # Single campaign: add --campaign_name "..." or --campaign_id 120...

``--platform`` is optional; omit to aggregate across all platforms. Omit BOTH
campaign args to run account-wide.
"""

import argparse
import logging
import sys

import lib.logging
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.ad import Ad
from import_scripts.facebook.get_insights import (
    batch_fb_metadata,
    get_fb_app_user_token,
    init_facebook_api,
    set_import_params,
)
from import_scripts.facebook.lib.assets import get_fb_teams, resolve_team_filter_scope
from import_scripts.facebook.lib.fb_helpers import normalize_ad_account_id, strip_ad_account_prefix
from lib.es_helpers import get_elasticsearch_client


def _f(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def resolve_campaign_id(campaign_name: str, since: str, until: str) -> str | None:
    """Resolve a campaign NAME to its id by listing the account's campaigns
    that delivered in the window. Returns the id of the first name match
    (case-insensitive, exact), or None.

    Lets the operator pass the campaign name from the dashboard filter rather
    than digging up the numeric id.
    """
    rows = _ACCOUNT.get_insights(
        fields=["campaign_id", "campaign_name"],
        params={"time_range": {"since": since, "until": until}, "level": "campaign", "limit": 1000},
    )
    target = campaign_name.strip().lower()
    matches = []
    for r in rows:
        name = (r.get("campaign_name") or "").strip()
        if name.lower() == target:
            return r.get("campaign_id")
        if target in name.lower():
            matches.append((name, r.get("campaign_id")))
    if len(matches) == 1:
        logging.info("Resolved campaign name to %r (id=%s) by substring match", matches[0][0], matches[0][1])
        return matches[0][1]
    if matches:
        logging.warning("Ambiguous campaign name %r matched %d campaigns: %s", campaign_name, len(matches), matches)
    return None


def fb_campaign_grain_reach(campaign_id: str, since: str, until: str, platform: str | None) -> dict:
    """FB's own deduplicated reach for the campaign over the window.

    ONE call at level=campaign, filtered to the campaign. When a platform is
    given, break down by publisher_platform and return that platform's row.
    """
    params = {
        "time_range": {"since": since, "until": until},
        "level": "campaign",
        "limit": 1000,
    }
    if campaign_id:
        params["filtering"] = [{"field": "campaign.id", "operator": "EQUAL", "value": campaign_id}]
    if platform:
        params["breakdowns"] = ["publisher_platform"]

    # The insights edge hangs off the account; filtering pins the campaign when given.
    insights = _ACCOUNT.get_insights(fields=["campaign_id", "reach", "impressions", "spend", "frequency"], params=params)

    total = {"reach": 0, "impressions": 0, "spend": 0.0, "rows": 0}
    for row in insights:
        if platform and row.get("publisher_platform") != platform:
            continue
        total["reach"] += _f(row.get("reach"))
        total["impressions"] += _f(row.get("impressions"))
        total["spend"] += float(row.get("spend") or 0)
        total["rows"] += 1
    return total


def fb_adset_grain_reach(campaign_id: str, since: str, until: str, platform: str | None) -> dict:
    """FB's deduplicated reach at AD-SET grain, summed across the campaign's ad sets.

    This is the grain the Ad Sets table displays (one row per ad set). FB dedups
    reach WITHIN each ad set; we then sum ad-set reach to a campaign total. For a
    single-ad-set campaign this equals [1]; for a multi-ad-set campaign it sits
    between [1] (full campaign dedup) and [2] (per-ad sum) — and it is what each
    table ROW should show. Compare a single ad set's row to Data Studio.
    """
    params = {
        "time_range": {"since": since, "until": until},
        "level": "adset",
        "limit": 1000,
    }
    if campaign_id:
        params["filtering"] = [{"field": "campaign.id", "operator": "EQUAL", "value": campaign_id}]
    if platform:
        params["breakdowns"] = ["publisher_platform"]
    rows = _ACCOUNT.get_insights(
        fields=["adset_id", "adset_name", "campaign_name", "reach", "impressions", "spend"], params=params
    )
    total = {"reach": 0, "impressions": 0, "spend": 0.0, "adsets": 0}
    # key per_adset by adset_id (NOT name) so same-named ad sets across
    # campaigns don't collapse/sum together. When a publisher_platform breakdown
    # is active, FB returns one row per (adset, platform) — accumulate reach per
    # adset_id so the "per ad set" figure is the platform-summed value; the
    # caller compares it knowingly. Each entry: adset_id -> [name, campaign, reach, n_rows].
    by_id: dict = {}
    for row in rows:
        if platform and row.get("publisher_platform") != platform:
            continue
        r = _f(row.get("reach"))
        aid = row.get("adset_id") or "?"
        total["reach"] += r
        total["impressions"] += _f(row.get("impressions"))
        total["spend"] += float(row.get("spend") or 0)
        total["adsets"] += 1
        if aid not in by_id:
            by_id[aid] = [row.get("adset_name") or aid, row.get("campaign_name") or "?", 0, 0]
        by_id[aid][2] += r
        by_id[aid][3] += 1
    # per_adset: (adset_id, name, campaign, reach, n_rows) — n_rows>1 reveals a
    # platform breakdown (or a name collision if keyed by name elsewhere).
    total["per_adset"] = [(aid, v[0], v[1], v[2], v[3]) for aid, v in by_id.items()]
    total["adsets"] = len(by_id)
    return total


def fb_per_ad_reach_summed(campaign_id: str, since: str, until: str, platform: str | None) -> dict:
    """Sum of FB per-ad windowed reach across the campaign's ads.

    Lists the campaign's ads (one account-level call), then one windowed
    per-ad insights call each, summing reach. Reproduces what summing our
    stored per-ad window docs gives.
    """
    list_params = {
        "time_range": {"since": since, "until": until},
        "level": "ad",
        "limit": 1000,
    }
    if campaign_id:
        list_params["filtering"] = [{"field": "campaign.id", "operator": "EQUAL", "value": campaign_id}]
    ad_rows = _ACCOUNT.get_insights(fields=["ad_id", "spend"], params=list_params)
    ad_ids = [r["ad_id"] for r in ad_rows if float(r.get("spend", 0)) > 0]

    total = {"reach": 0, "impressions": 0, "spend": 0.0, "ads": 0}
    for ad_id in ad_ids:
        params = {
            "time_range": {"since": since, "until": until},
            "fields": ["reach", "impressions", "spend"],
        }
        if platform:
            params["breakdowns"] = ["publisher_platform"]
        ad_insights = Ad(ad_id).get_insights(
            fields=["reach", "impressions", "spend"],
            params={k: v for k, v in params.items() if k != "fields"},
        )
        for row in ad_insights:
            if platform and row.get("publisher_platform") != platform:
                continue
            total["reach"] += _f(row.get("reach"))
            total["impressions"] += _f(row.get("impressions"))
            total["spend"] += float(row.get("spend") or 0)
        total["ads"] += 1
    return total


def es_stored_reach(
    index: str, ad_account_id: str, campaign_id: str, since: str, until: str, platform: str | None, window: bool
) -> dict:
    """Sum stored reach from an ES index for the slice.

    ``window=True`` filters per-window docs by insight_window_start; otherwise
    per-day docs by insight_date_start range.
    """
    es = get_elasticsearch_client()
    if es is None:
        return {"error": "ES unavailable"}

    must = [
        {"term": {"ad_account_id": strip_ad_account_prefix(ad_account_id)}},
    ]
    if campaign_id:
        must.append({"term": {"ad_campaign_id": campaign_id}})
    if window:
        must.append({"term": {"insight_window_start": since}})
        must.append({"term": {"insight_window_end": until}})
    else:
        must.append({"range": {"insight_date_start": {"gte": since, "lte": until}}})
    if platform:
        must.append({"term": {"publisher_platform": platform}})

    # Window docs are ad-set grain (adset_id, no ad_id); per-day docs are per-ad
    # (ad_id). Count on the field that actually exists for the index, else docs=0
    # even when the index is full.
    count_field = "adset_id" if window else "ad_id"
    body = {
        "size": 0,
        "query": {"bool": {"filter": must}},
        "aggs": {
            "doc_count": {"value_count": {"field": count_field}},
            "reach": {"sum": {"field": "insight_reach"}},
            "impressions": {"sum": {"field": "insight_impressions"}},
            "spend": {"sum": {"field": "insight_spend"}},
        },
    }
    try:
        result = es.search(index=index, body=body, ignore_unavailable=True, allow_no_indices=True)
    except Exception as e:  # noqa: BLE001 — diagnostic tool boundary
        return {"error": f"{index}: {e}"}
    aggs = result.get("aggregations", {})
    return {
        "doc_count": _f(aggs.get("doc_count", {}).get("value")),
        "reach": _f(aggs.get("reach", {}).get("value")),
        "impressions": _f(aggs.get("impressions", {}).get("value")),
        "spend": float(aggs.get("spend", {}).get("value") or 0),
    }


# ---------------------------------------------------------------------------
# team-grain page-filtered reach (Phase 0 verification).
#
# The window importer skips SHARED ad accounts because an ad-set-grain
# row carries no page_id, so ad-set reach can't be attributed to a page-subset.
# insight: the team↔page link is derivable (creative.actor_id → page
# → team). A TEAM-grain reach — Meta's deduplicated reach for the team's PAGES
# over the window — sidesteps ad-set grain and works for shared accounts too,
# because it is page-filtered by construction.
#
# Meta has no direct page_id insights filter. The load-bearing unknown (this
# phase proves it): filtering a LEVEL=ACCOUNT windowed get_insights by the
# team's AD IDS returns ONE server-side-deduplicated reach across only those
# ads — the team's true windowed reach. This harness resolves the team's pages,
# finds the account's ads whose creative.actor_id is one of those pages, then
# runs the candidate filtered account-level call and prints it against the
# summed value the dashboard shows today (~91K for one team) and the ad-set sum.
# Compare the result to a teammate's Data Studio ~54K to decide if the field dedups.
# ---------------------------------------------------------------------------


def resolve_team(team_title: str) -> dict | None:
    """Return a ds_facebook team's ``{page_ids, import_mode, esid}`` by title.

    Reads ``team_facebook_page_id`` (the page list the importer attributes on)
    and ``fb_import_mode`` off de_asset_teams. The import mode is decisive for
    reading a "0 matches" result: an ``account_wide`` team claims EVERY ad on its
    ad account regardless of page, so its reach is the whole account and
    a page filter naturally matches nothing — that is NOT a bug. A page-filtered
    team MUST resolve via the creative page id, so a "0 matches" there points at
    the field (or a wrong account). Matched case-insensitively by exact title
    (manual tool; importers key on ESID). ``None`` if no team matches.
    """
    target = (team_title or "").strip().lower()
    for t in get_fb_teams(filters={"ds_facebook": "true"}):
        src = t["_source"]
        if (src.get("team_title") or "").strip().lower() == target:
            return {
                "page_ids": {str(pid) for pid in (src.get("team_facebook_page_id") or []) if pid is not None},
                "import_mode": src.get("fb_import_mode") or "(unset — page-filtered)",
                "esid": t["_id"],
            }
    return None


# Per-ad creative fields fetched in the batch round — exactly the page-bearing
# fields the importer's Round-1 batch pulls (get_insights.py ad_fields).
_AD_METADATA_FIELDS = "id,adset_id,creative{id,actor_id,object_story_spec{page_id}}"


def _creative_page_id(ad_body: dict) -> tuple[str | None, str]:
    """Extract an ad's page id from its batched metadata body, mirroring the
    importer's source.

    The importer attributes a page from ``creative.actor_id`` (get_insights.py:1520
    reads ``creative_details.actor_id``). We read actor_id as primary AND
    object_story_spec.page_id as a diagnostic fallback so a run reveals when a
    team's creatives carry the page in object_story_spec instead.

    ``ad_body`` is a batch response body: ``{id, adset_id, creative:{...}}``.
    Returns ``(page_id, source)``; source ∈ {'actor_id',
    'object_story_spec.page_id', 'none'}.
    """
    creative = ad_body.get("creative") or {}
    actor_id = creative.get("actor_id")
    if actor_id is not None:
        return str(actor_id), "actor_id"
    oss = creative.get("object_story_spec") or {}
    page_id = oss.get("page_id")
    if page_id is not None:
        return str(page_id), "object_story_spec.page_id"
    return None, "none"


def _delivering_ad_ids(since: str, until: str) -> list[str]:
    """Ad ids that SPENT in the window — the same set the importer attributes.

    Mirrors get_insights.py: ``AdAccount.get_insights(level="ad", time_range)``
    then keep spend>0. This is the correct superset for windowed reach (only ads
    that delivered contribute), and it comes off the INSIGHTS edge (which accepts
    time_range) rather than the ads edge (which 500s on nested creative)."""
    rows = _ACCOUNT.get_insights(
        fields=["ad_id", "spend"],
        params={"time_range": {"since": since, "until": until}, "level": "ad", "limit": 1000},
    )
    return [r["ad_id"] for r in rows if r.get("ad_id") and float(r.get("spend") or 0) > 0]


def ads_for_pages(page_ids: set[str], since: str, until: str, token: str) -> dict:
    """Find the delivering ads whose creative page id is one of the team's pages.

    Uses the PROVEN production path (get_insights.py), NOT ``AdAccount.get_ads``:
      1. ``get_insights(level="ad", time_range)`` → ad ids that spent in the window.
      2. ``batch_fb_metadata`` per-ad ``creative{actor_id,object_story_spec{page_id}}``
         in chunks of 50.
    The whole-account ads edge 500s on nested creative ("reduce the amount of
    data"); the per-ad node handles it, which is why the importer batches. Bonus:
    step 1 already scopes to ads that DELIVERED in the window — exactly the ads
    that contribute reach.

    Returns ``{ad_ids, adset_ids, by_page, scanned, no_page, source_counts}``.
    ``source_counts`` / ``no_page`` are diagnostics: they reveal HOW each ad's page
    resolved (actor_id vs object_story_spec vs none) so a "0 matches" result tells
    us whether it's a field problem or a genuinely wrong account.
    """
    delivering = _delivering_ad_ids(since, until)
    meta = batch_fb_metadata([(aid, _AD_METADATA_FIELDS) for aid in delivering], token)

    ad_ids: list[str] = []
    adset_ids: set[str] = set()
    by_page: dict[str, int] = {}
    all_pages_seen: set[str] = set()
    scanned = 0
    no_page = 0
    source_counts: dict[str, int] = {}
    for aid in delivering:
        body = meta.get(str(aid))
        if not body:
            continue  # batch dropped it (non-200); not counted as scanned
        scanned += 1
        page_id, source = _creative_page_id(body)
        source_counts[source] = source_counts.get(source, 0) + 1
        if page_id is None:
            no_page += 1
            continue
        all_pages_seen.add(page_id)
        if page_id in page_ids:
            ad_ids.append(str(body.get("id") or aid))
            if body.get("adset_id"):
                adset_ids.add(str(body.get("adset_id")))
            by_page[page_id] = by_page.get(page_id, 0) + 1
    return {
        "ad_ids": ad_ids,
        "adset_ids": sorted(adset_ids),
        "by_page": by_page,
        # Every distinct page id on the account's DELIVERING ads — lets a "0
        # matches" run show whether the team's pages are even present here (wrong
        # account) vs the field not resolving (empty / all 'none').
        "all_pages_seen": sorted(all_pages_seen),
        "delivering_count": len(delivering),
        "scanned": scanned,
        "no_page": no_page,
        "source_counts": source_counts,
    }


def fb_filtered_account_reach(filter_field: str, ids: list[str], since: str, until: str, platform: str | None) -> dict:
    """Candidate team-grain reach: ONE level=account windowed call, filtered to
    the given IDs (``ad.id`` or ``adset.id``) belonging to the team's pages.

    This is the load-bearing Phase-0 call. If Meta dedups reach server-side over
    only the filtered ads, the single returned row IS the team's windowed
    deduplicated reach — the ~54K target. ``IN`` operator so one call covers all
    the team's ads. Broken down by publisher_platform only when a platform is
    requested (then summed across the requested platform's rows).
    """
    if not ids:
        return {"reach": 0, "impressions": 0, "spend": 0.0, "rows": 0, "n_ids": 0}
    params = {
        "time_range": {"since": since, "until": until},
        "level": "account",
        "filtering": [{"field": filter_field, "operator": "IN", "value": ids}],
        "limit": 1000,
    }
    if platform:
        params["breakdowns"] = ["publisher_platform"]
    rows = _ACCOUNT.get_insights(fields=["reach", "impressions", "spend", "frequency"], params=params)
    total = {"reach": 0, "impressions": 0, "spend": 0.0, "rows": 0, "n_ids": len(ids)}
    for row in rows:
        if platform and row.get("publisher_platform") != platform:
            continue
        total["reach"] += _f(row.get("reach"))
        total["impressions"] += _f(row.get("impressions"))
        total["spend"] += float(row.get("spend") or 0)
        total["rows"] += 1
    return total


def fb_team_adset_rows(ad_ids: list[str], since: str, until: str, platform: str | None) -> dict:
    """Phase-1 verification — PER-AD-SET reach scoped to the team's ads.

    ``level=adset`` + ``filtering: ad.id IN <team's ads>``. The load-bearing
    question for the per-ad-set fix: does Meta return one row PER AD SET whose
    reach is deduped over ONLY the team's ads in that ad set (not the whole
    account's ads in it)? If yes, the importer can write these rows for a
    page-filtered team on a SHARED account — the ad-set grain is honest because
    each row counts only this team's ads, sidestepping the "page→ad-set not 1:1"
    trap (an ad set with other teams' ads still reports only ours).

    Returns ``{rows: [(adset_id, adset_name, reach, impr, spend)], reach_sum,
    n_adsets}``. reach_sum SUMS the per-ad-set reach (the Ad Sets table's Total —
    an upper bound across ad sets, expected; each ROW is the honest deduped value).
    """
    if not ad_ids:
        return {"rows": [], "reach_sum": 0, "n_adsets": 0}
    params = {
        "time_range": {"since": since, "until": until},
        "level": "adset",
        "filtering": [{"field": "ad.id", "operator": "IN", "value": ad_ids}],
        "limit": 1000,
    }
    if platform:
        params["breakdowns"] = ["publisher_platform"]
    rows = _ACCOUNT.get_insights(fields=["adset_id", "adset_name", "reach", "impressions", "spend"], params=params)
    by_id: dict = {}
    for row in rows:
        if platform and row.get("publisher_platform") != platform:
            continue
        aid = row.get("adset_id") or "?"
        if aid not in by_id:
            by_id[aid] = [row.get("adset_name") or aid, 0, 0, 0.0]
        by_id[aid][1] += _f(row.get("reach"))
        by_id[aid][2] += _f(row.get("impressions"))
        by_id[aid][3] += float(row.get("spend") or 0)
    out = [(aid, v[0], v[1], v[2], v[3]) for aid, v in by_id.items()]
    return {"rows": out, "reach_sum": sum(r[2] for r in out), "n_adsets": len(out)}


def run_team_grain(team_title: str, since: str, until: str, platform: str | None, token: str) -> int:
    """Phase 0: print the candidate team-grain reach numbers for a team.

    Resolves the team's pages, finds the delivering ads on those pages (batched
    per-ad metadata — the proven importer path; ``token`` is needed for the batch
    endpoint), then runs two candidate filtered account-level calls (ad.id and
    adset.id) so we can see which grain Meta dedups by.
    """
    team = resolve_team(team_title)
    if team is None:
        logging.error(
            "no ds_facebook team matched title %r. Cannot run team-grain verification.",
            team_title,
        )
        return 1
    page_ids = team["page_ids"]
    import_mode = team["import_mode"]
    if not page_ids:
        logging.error(
            "team %r (mode=%s) has no configured pages (team_facebook_page_id).",
            team_title,
            import_mode,
        )
        return 1

    logging.info("team %r mode=%s pages=%s", team_title, import_mode, sorted(page_ids))
    ads = ads_for_pages(page_ids, since, until, token)
    if not ads["ad_ids"]:
        logging.error(
            "0 of %d fetched ads (of %d delivering) matched team %r (mode=%s) pages %s over %s..%s.\n"
            "  page-id source counts: %s (no_page=%d)\n"
            "  DISTINCT page ids on this account's DELIVERING ads: %s\n"
            "  Read this: mode=account_wide -> the team's reach is the WHOLE account "
            "(attribution bypasses pages); a page filter matching nothing is EXPECTED, "
            "not a bug. mode=page-filtered + non-empty distinct list excluding the team's pages "
            "-> wrong account. All-'none' -> the creative field isn't resolving.",
            ads["scanned"],
            ads["delivering_count"],
            team_title,
            import_mode,
            sorted(page_ids),
            since,
            until,
            ads["source_counts"],
            ads["no_page"],
            ads["all_pages_seen"] or "[]",
        )
        return 1

    by_ad = fb_filtered_account_reach("ad.id", ads["ad_ids"], since, until, platform)
    by_adset = fb_filtered_account_reach("adset.id", ads["adset_ids"], since, until, platform)

    plat = platform or "(all platforms)"
    print()
    print(f"TEAM-GRAIN reach | team {team_title!r} (mode={import_mode}) | platform {plat} | {since}..{until}")
    print(f"  team pages: {sorted(page_ids)}")
    print(f"  ads on those pages: {len(ads['ad_ids'])} (scanned {ads['scanned']}); ad sets: {len(ads['adset_ids'])}")
    print(f"  ads per page: {ads['by_page']}")
    print(f"  page-id source: {ads['source_counts']} (no_page={ads['no_page']})")
    print(f"{'Candidate (server-side dedup over team ads)':<48} {'Reach':>12} {'Impr':>14} {'Spend':>12}")
    print("-" * 90)
    print(
        f"{'[T1] level=account, filtering ad.id IN team ads':<48} "
        f"{by_ad['reach']:>12,} {by_ad['impressions']:>14,} {by_ad['spend']:>12.2f}  (n_ids={by_ad['n_ids']})"
    )
    print(
        f"{'[T2] level=account, filtering adset.id IN team':<48} "
        f"{by_adset['reach']:>12,} {by_adset['impressions']:>14,} {by_adset['spend']:>12.2f}  (n_ids={by_adset['n_ids']})"
    )
    print()

    # Phase 1 (per-ad-set) verification — the grain the importer will
    # WRITE for a page-filtered team on a shared account: level=adset filtered by
    # THIS team's ad.ids, so each ad-set row counts only the team's ads (honest
    # even when the ad set also holds other teams' ads — the page->adset-not-1:1
    # trap). Each ROW is deduped-within-ad-set; the SUM is the table Total (upper
    # bound across ad sets, expected).
    adset_rows = fb_team_adset_rows(ads["ad_ids"], since, until, platform)
    print(f"[P1] PER-AD-SET rows (level=adset, filtering ad.id IN team ads) — {adset_rows['n_adsets']} ad set(s):")
    print(f"      {'adset_id':<20} {'ad set':<34} {'reach':>10} {'impr':>12} {'spend':>10}")
    for aid, name, reach, impr, spend in sorted(adset_rows["rows"], key=lambda r: -r[2]):
        print(f"      {str(aid)[:20]:<20} {str(name)[:34]:<34} {reach:>10,} {impr:>12,} {spend:>10.2f}")
    print(f"      {'— SUM (table Total; upper bound across ad sets)':<54} {adset_rows['reach_sum']:>10,}")
    print()
    print("Decision (record in the phase file):")
    print("  * [P1] per-ad-set rows deduped over ONLY the team's ads -> the importer writes")
    print("    THESE rows for a shared-account page-filtered team (stop skipping the account).")
    print("    Each row is honest per-ad-set dedup; the Total is the labeled upper bound.")
    print("  * If [T1] (ad.id) ~= a teammate's Data Studio reach (~54K for one team) and < the")
    print("    per-day sum the dashboard shows today -> ad.id filtering dedups. Build Phase 1")
    print("    on a level=account, filtering=ad.id IN <team ads> call.")
    print("  * [T2] (adset.id) is a coarser candidate — an ad set can carry ads for OTHER")
    print("    pages, so it can OVER-scope for a shared account. Compare, but prefer ad.id.")
    print("  * If NEITHER dedups (both ~= the summed value), no server-side page-dedup exists;")
    print("    downgrade Phase 1 to the LABELED upper bound.")
    return 0


def run_team_grain_all_accounts(team_title: str, since: str, until: str, platform: str | None) -> int:
    """Phase 0 — team-grain reach across ALL of a team's ad accounts.

    A team's pages can span >1 ad account (a team whose pages span 3 ad accounts). Meta only dedups reach
    WITHIN one account's call, so a cross-account team has NO single true dedup
    number — the ticket's decision is to show the per-account sum LABELED "approx
    upper bound — not deduplicated across ad accounts".

    This resolves the team's ad accounts from assets (page → fb_account → fb_app →
    fb_ad_account, via resolve_team_filter_scope + set_import_params), auths each,
    runs the single-account team-grain reach, and prints:
      * one row per ad account (its own deduped team-grain reach), and
      * the SUM across accounts = the labeled cross-account upper bound.

    Single-account teams degenerate to one row (== run_team_grain). This is the
    concrete evidence for Phase 1's single-vs-cross-account branch.
    """
    global _ACCOUNT

    team = resolve_team(team_title)
    if team is None:
        logging.error("no ds_facebook team matched title %r.", team_title)
        return 1
    page_ids = team["page_ids"]
    if not page_ids:
        logging.error("team %r has no configured pages.", team_title)
        return 1

    scope = resolve_team_filter_scope(team["esid"])
    entries = set_import_params(team_ids=team["esid"], scope=scope)
    # One entry per (asset-chain path); the reach call is account-level, so run
    # each distinct ad account once.
    seen: set[str] = set()
    per_account = []  # (bare_account, ads_dict, by_ad_reach_dict)
    for entry in entries:
        fb_app = entry["fb_app"]
        fb_ad_account = entry["fb_ad_account"]
        bare = strip_ad_account_prefix(str(fb_ad_account.get("fb_ad_account_id")))
        if not bare or bare in seen:
            continue
        seen.add(bare)

        token = get_fb_app_user_token(fb_ad_account.get("fb_app_user_esid"))
        if not token:
            logging.warning("no token for account %s; skipping.", bare)
            continue
        init_facebook_api(fb_app.get("fb_app_id"), fb_app.get("fb_app_secret"), token)
        _ACCOUNT = AdAccount(normalize_ad_account_id(bare))

        ads = ads_for_pages(page_ids, since, until, token)
        if not ads["ad_ids"]:
            logging.info(
                "account %s — 0 of %d delivering ads on the team's pages "
                "(source=%s, pages seen=%s). No team reach here in this window.",
                bare,
                ads["delivering_count"],
                ads["source_counts"],
                ads["all_pages_seen"] or "[]",
            )
            per_account.append((bare, ads, {"reach": 0, "impressions": 0, "spend": 0.0, "n_ids": 0}))
            continue
        by_ad = fb_filtered_account_reach("ad.id", ads["ad_ids"], since, until, platform)
        per_account.append((bare, ads, by_ad))

    if not per_account:
        logging.error("team %r resolved to no ad accounts.", team_title)
        return 1

    accounts_with_reach = [p for p in per_account if p[2]["reach"] > 0]
    sum_reach = sum(p[2]["reach"] for p in per_account)
    sum_impr = sum(p[2]["impressions"] for p in per_account)
    sum_spend = sum(p[2]["spend"] for p in per_account)
    is_cross_account = len(accounts_with_reach) > 1

    plat = platform or "(all platforms)"
    print()
    print(f"TEAM-GRAIN reach (ALL ACCOUNTS) | team {team_title!r} | platform {plat} | {since}..{until}")
    print(f"  team pages: {sorted(page_ids)}")
    print(f"  ad accounts resolved: {len(per_account)}; with reach in window: {len(accounts_with_reach)}")
    print(f"{'Ad account (level=account, ad.id IN its team ads)':<50} {'Reach':>12} {'Impr':>14} {'Spend':>12}")
    print("-" * 92)
    for bare, ads, by_ad in per_account:
        print(
            f"  {bare:<48} {by_ad['reach']:>12,} {by_ad['impressions']:>14,} {by_ad['spend']:>12.2f}  "
            f"(ads={len(ads['ad_ids'])})"
        )
    print("-" * 92)
    label = (
        "SUM across accounts — APPROX UPPER BOUND (not deduped across accounts)"
        if is_cross_account
        else "SINGLE account — TRUE deduplicated team reach"
    )
    print(f"  {label:<48} {sum_reach:>12,} {sum_impr:>14,} {sum_spend:>12.2f}")
    print()
    if is_cross_account:
        print("CROSS-ACCOUNT team: Meta can't dedup reach across ad accounts, so the sum")
        print("above RE-COUNTS anyone reached via >1 account -> it is an UPPER BOUND. Phase 1")
        print("stamps cross_account_upper_bound=true and the routing labels it.")
    else:
        print("SINGLE-account team: the one account's reach IS the true deduplicated team reach.")
        print("Phase 1 writes a clean team-grain window doc (no upper-bound label).")
    return 0


def _find_ad_account_entry(ad_account_id: str) -> dict | None:
    target = strip_ad_account_prefix(ad_account_id)
    for entry in set_import_params():
        stored = strip_ad_account_prefix(entry["fb_ad_account"].get("fb_ad_account_id", ""))
        if stored == target:
            return entry
    return None


_ACCOUNT = None  # set in main() after auth


def main() -> int:
    global _ACCOUNT
    lib.logging.define("fb-confirm-adset-reach")

    p = argparse.ArgumentParser(description="Ad Sets reach confirmation")
    p.add_argument("--ad_account_id", default=None, help="Required except with --team_grain --all_accounts")
    p.add_argument("--campaign_id", default=None, help="Numeric campaign id (or use --campaign_name)")
    p.add_argument(
        "--campaign_name",
        default=None,
        help="Campaign name from the dashboard filter (resolved to id). Omit BOTH to run account-wide "
        "(no campaign filter) — every ad set's row, matching the no-campaign Data Studio view.",
    )
    p.add_argument("--platform", default=None, help="publisher_platform (e.g. facebook, instagram); omit for all")
    p.add_argument("--since", required=True, help="Window start YYYY-MM-DD")
    p.add_argument("--until", required=True, help="Window end YYYY-MM-DD")
    p.add_argument("--team_title", required=True, help="Team title for ES index lookup")
    p.add_argument(
        "--team_grain",
        action="store_true",
        help="Phase 0: print the candidate TEAM-grain page-filtered reach "
        "(ad.id / adset.id IN the team's ads) instead of the  ad-set comparison. "
        "Ignores --campaign_* (team-grain spans the whole team, not one campaign).",
    )
    p.add_argument(
        "--all_accounts",
        action="store_true",
        help="Phase 0: with --team_grain, run team-grain reach across ALL of the "
        "team's ad accounts (resolved from assets) and print per-account dedup + the labeled "
        "cross-account upper-bound sum. --ad_account_id is not required in this mode.",
    )
    args = p.parse_args()

    # all-accounts mode resolves ad accounts from the team itself, so it
    # does not need (or use) --ad_account_id.
    if args.team_grain and args.all_accounts:
        return run_team_grain_all_accounts(args.team_title, args.since, args.until, args.platform)

    if not args.ad_account_id:
        logging.error("--ad_account_id is required (except with --team_grain --all_accounts).")
        return 1
    entry = _find_ad_account_entry(args.ad_account_id)
    if entry is None:
        logging.error("No ad account entry found for %s", args.ad_account_id)
        return 1

    fb_app = entry["fb_app"]
    fb_ad_account = entry["fb_ad_account"]
    token = get_fb_app_user_token(fb_ad_account.get("fb_app_user_esid"))
    if not token:
        logging.error("Could not resolve fb_app_user token for this ad account")
        return 1
    init_facebook_api(fb_app.get("fb_app_id"), fb_app.get("fb_app_secret"), token)
    _ACCOUNT = AdAccount(normalize_ad_account_id(args.ad_account_id))

    # Phase 0 — team-grain page-filtered reach. Separate output from the
    # ad-set comparison; returns early (campaign filters don't apply to a
    # whole-team reach).
    if args.team_grain:
        return run_team_grain(args.team_title, args.since, args.until, args.platform, token)

    campaign_id = args.campaign_id
    if not campaign_id and args.campaign_name:
        campaign_id = resolve_campaign_id(args.campaign_name, args.since, args.until)
        if not campaign_id:
            logging.error("Could not resolve campaign name %r to an id in the window", args.campaign_name)
            return 1
        logging.info("Using campaign_id=%s", campaign_id)
    if not campaign_id:
        logging.info("No campaign given — running ACCOUNT-WIDE (every ad set, no campaign filter).")

    # data indices are ESID-keyed ({team_esid}__source), NOT the legacy
    # {title-slug}_source. Querying the title slug finds nothing. Use the wildcard
    # source pattern — the ad_account_id term filter in es_stored_reach scopes it
    # to the right team's docs regardless of which ESID-keyed index they live in.
    daily_index = "*_fb_insights"
    window_index = "*_fb_insights_window"

    fb_campaign = fb_campaign_grain_reach(campaign_id, args.since, args.until, args.platform)
    fb_adset = fb_adset_grain_reach(campaign_id, args.since, args.until, args.platform)
    fb_per_ad = fb_per_ad_reach_summed(campaign_id, args.since, args.until, args.platform)
    es_window = es_stored_reach(
        window_index, args.ad_account_id, campaign_id, args.since, args.until, args.platform, window=True
    )
    es_daily = es_stored_reach(
        daily_index, args.ad_account_id, campaign_id, args.since, args.until, args.platform, window=False
    )

    def _freq(d):
        return (d["impressions"] / d["reach"]) if d.get("reach") else 0.0

    plat = args.platform or "(all platforms)"
    camp_label = campaign_id or "(no campaign — account-wide)"
    print()
    print(f"Campaign {camp_label} | platform {plat} | {args.since}..{args.until}")
    print(f"{'Source':<46} {'Reach':>12} {'Impr':>14} {'Freq':>6} {'Spend':>12}")
    print("-" * 94)
    print(
        f"{'[1] FB campaign-grain windowed (dedup)':<46} {fb_campaign['reach']:>12,} {fb_campaign['impressions']:>14,} {_freq(fb_campaign):>6.2f} {fb_campaign['spend']:>12.2f}"
    )
    print(
        f"{'[1b] FB ad-set-grain windowed, summed':<46} {fb_adset['reach']:>12,} {fb_adset['impressions']:>14,} {_freq(fb_adset):>6.2f} {fb_adset['spend']:>12.2f}  (adsets={fb_adset['adsets']})"
    )
    print(
        f"{'[2] FB per-ad windowed, SUMMED across ads':<46} {fb_per_ad['reach']:>12,} {fb_per_ad['impressions']:>14,} {_freq(fb_per_ad):>6.2f} {fb_per_ad['spend']:>12.2f}"
    )
    if fb_adset.get("per_adset"):
        print("    per-ad-set reach by adset_id (n>1 = platform breakdown summed; watch campaign):")
        print(f"      {'adset_id':<18} {'campaign':<28} {'ad set':<32} {'reach':>10} {'n':>3}")
        for aid, name, camp, r, n in sorted(fb_adset["per_adset"], key=lambda t: -t[3]):
            print(f"      {str(aid)[:18]:<18} {str(camp)[:28]:<28} {str(name)[:32]:<32} {r:>10,} {n:>3}")
    if "error" in es_window:
        print(f"[3] ES *_fb_insights_window               (skipped: {es_window['error']})")
    else:
        print(
            f"{'[3] ES *_fb_insights_window (sum stored)':<46} {es_window['reach']:>12,} {es_window['impressions']:>14,} {es_window['spend']:>12.2f}  (docs={es_window['doc_count']})"
        )
    if "error" in es_daily:
        print(f"[4] ES *_fb_insights per-day              (skipped: {es_daily['error']})")
    else:
        print(
            f"{'[4] ES *_fb_insights per-day (sum, TODAY)':<46} {es_daily['reach']:>12,} {es_daily['impressions']:>14,} {es_daily['spend']:>12.2f}  (docs={es_daily['doc_count']})"
        )
    print()
    if fb_adset["reach"] and fb_per_ad["reach"]:
        within = fb_per_ad["reach"] / fb_adset["reach"]
        print(f"Within-ad-set over-count (per-ad sum / ad-set dedup): {within:.2f}x")
        print("  ~1.00x -> ad sets are effectively single-ad; per-ad storage is safe.")
        print("  >1.00x -> ad sets have multiple ads reaching overlapping users;")
        print("            MUST store at ad-set grain (level=adset), not sum per-ad docs.")
    print()
    print("The decisive check: does each per-ad-set row above equal its Data Studio row?")
    print("  If yes -> store window docs at ad-set x platform grain; each table row reads its own doc.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

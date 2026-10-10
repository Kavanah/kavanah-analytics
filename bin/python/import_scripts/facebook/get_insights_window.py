"""
Custom-range (windowed) FB ads insights importer — AD-SET grain.

Sister to ``get_insights.py`` (per-day, per-ad). This one asks Facebook for ONE
arbitrary multi-day ``time_range`` at **ad-set × publisher_platform** grain and
writes the result to ``{team_esid}__fb_insights_window``.

WHY ad-set grain:
  FB **reach** is a count of *unique* users. Summing per-day reach over a range
  over-counts anyone reached on more than one day; summing per-AD reach over-counts
  anyone reached by more than one ad. FB only deduplicates reach when asked for the
  window at a given grain in ONE call. The Ad Sets dashboard table (``meta_ad_sets``)
  shows ONE ROW PER AD SET — platform / ad-account / campaign are just filters that
  narrow which rows show; the table never sums ad sets together. So the correct grain
  is the ad set: ``AdAccount.get_insights(level="adset", breakdowns=["publisher_platform"],
  time_range={since,until})`` returns FB's deduplicated reach per (ad set, platform),
  which reproduced all five Data Studio rows to the digit (see DECISIONS.md 2026-06-03
  and confirm_adset_reach.py). Each table row reads its own ad-set window doc —
  no summing, exact Data Studio match under any filter combination.

ONE account-level call per ad account per window returns every ad-set row, so this is
far cheaper than a per-ad pull (~1 call/account vs N).

SCOPE — three cases, all now covered:
  An account-level ad-set pull carries no page_id, so a whole-account pull can only
  be attributed when the WHOLE account belongs to one team:
    1. account_wide teams — resolved by ad account
       (``get_account_wide_teams_by_ad_account_id``); unambiguous by design.
    2. page-filtered teams that SOLE-OWN their ad account — i.e. no OTHER ds_facebook
       team's pages reach the account (``_teams_reaching_ad_account`` reverse walk).
  Both above → one whole-account ``level=adset`` pull, every ad set for the team.
    3. SHARED page-filtered account — multiple teams split it by Facebook
       Page. Per scoped team reaching the account, resolve THAT team's ads (page->ad
       by ``creative.actor_id``) and pull ``level=adset filtering ad.id IN <team ads>``
       so each ad-set row is deduplicated over ONLY that team's ads — honest even when
       an ad set also holds another team's ads. Before this account was
       skipped and its teams' Ad Sets reach stayed on the over-counting per-day index.
       (``_import_shared_account_per_team``.)

Usage::

    python import.py facebook get_insights_window \\
        --since 2026-05-01 --until 2026-05-31 --team_ids <team_id>

``--since`` and ``--until`` are required. A single-day window is allowed but the
caller (run_daily_imports) skips it — the per-day doc already carries that day's reach.
"""

import logging
import time
from datetime import datetime, timezone

import lib.logging
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.exceptions import FacebookRequestError
from import_scripts.facebook import get_insights as _daily
from import_scripts.facebook.get_insights import (
    batch_fb_metadata,
    check_and_pause_for_account,
    compute_optimization_result,
    get_account_wide_teams_by_ad_account_id,
    get_fb_app_user_token,
    init_facebook_api,
    reformat_insights,
    save_data_to_file,
    set_import_params,
    setup_usage_logging,
)
from import_scripts.facebook.lib.assets import (
    get_fb_ad_accounts,
    get_fb_apps,
    get_fb_pages,
    get_fb_teams,
    resolve_team_filter_scope,
)
from import_scripts.facebook.lib.fb_helpers import normalize_ad_account_id, strip_ad_account_prefix
from import_scripts.facebook.lib.team_helpers import _resolve_team_country
from lib.es_helpers import build_data_index_name, get_elasticsearch_client
from migration.catalog_loader import request_fields, request_fields_for_tenant

# Ad-set-grain insight fields. Reach/frequency are the non-additive metrics this
# index exists for; the additive ones (spend/impressions/clicks) ride along so a
# single ad-set row can show them all without a second index hit. cpp / cpm / ctr
# and the cost_per_* family are FB-computed ratios for the window.
#
# (PHASE-106): sourced from the field catalog (catalog/fb_ads.json) — the
# distinct `api_field`s of every requested `insight` entry whose `requested_in`
# includes "window" — instead of a hardcoded literal. Same single source of truth
# that drives the daily importer and the ES mapping generation.
_INSIGHT_FIELDS = request_fields("fb_ads", "window")


def _window_fields_for_team(team_esid):
    """the team's per-tenant window request list.

    Overrides come from the daily module's per-run cache (same fail-soft
    contract: any miss/failure → the global default list)."""
    if not team_esid or team_esid == "unknown":
        return _INSIGHT_FIELDS
    overrides = _daily._overrides_for_team(team_esid)
    if not overrides:
        return _INSIGHT_FIELDS
    return request_fields_for_tenant("fb_ads", "window", overrides)


def _drop_disabled_fields(doc, team_esid):
    """per-tenant doc filter (action metrics ride the always-requested
    parents; a disabled field can only be dropped from the stored doc)."""
    for dropped in _daily._disabled_fields_for_team(team_esid):
        doc.pop(dropped, None)
    return doc


_DEMOGRAPHICS_FIELD_ID = "fb_ads.breakdown_demographics"


def _demographics_enabled_for_team(team_esid):
    """whether this team's workspace wants the age/gender demographics
    pull. Default (no override / unresolvable team / store failure) is True —
    behavior-preserving: every tenant gets demographics unless they opt out."""
    override = (_daily._overrides_for_team(team_esid) or {}).get(_DEMOGRAPHICS_FIELD_ID)
    return override is None or bool(override.get("enabled"))


def _output_path(
    fb_ad_account_esid: str,
    team_esid: str,
    since: str,
    until: str,
    adset_id: str,
    publisher_platform: str | None = None,
) -> str:
    """File path for one ad-set window doc.

    Keyed by the window bounds + ad set + platform, so a re-pull of the same window
    overwrites the same file rather than accumulating duplicates (idempotent, like
    the weekly importer).
    """
    suffix = f"_{publisher_platform}" if publisher_platform else ""
    return (
        f"/app/data/facebook/ad_insights_window/" f"{fb_ad_account_esid}/{team_esid}/{since}_{until}_{adset_id}{suffix}.json"
    )


def _teams_reaching_ad_account(fb_ad_account_esid: str) -> set[str]:
    """Return the set of ds_facebook team_esids whose pages reach this ad account.

    Reverse of the scope walk (page → fb_account → fb_app → fb_ad_account):
      fb_ad_account_esid → its fb_app_esid → that app's fb_account_esid →
      every fb_page under that fb_account → every ds_facebook team whose
      ``team_facebook_page_id`` includes one of those pages' fb_page_id.

    A page-filtered team SOLE-OWNS the account iff this set is exactly that one
    team. When >1 team is returned, the account is shared (multiple teams split
    it by Facebook Page) and an ad-set-grain pull cannot be attributed to the
    right team — the caller skips it. Deterministic from asset data; no FB call.

    Returns an empty set if the chain can't be resolved (caller treats that as
    "can't prove sole ownership" → skip, fail-closed).
    """
    ad_acct_docs = get_fb_ad_accounts(filters={"_id": [fb_ad_account_esid]})
    if not ad_acct_docs:
        return set()
    fb_app_esid = ad_acct_docs[0]["_source"].get("fb_app_esid")
    if not fb_app_esid:
        return set()

    app_docs = get_fb_apps(filters={"_id": [fb_app_esid]})
    if not app_docs:
        return set()
    fb_account_esid = app_docs[0]["_source"].get("fb_account_esid")
    if not fb_account_esid:
        return set()

    page_docs = get_fb_pages(filters={"fb_account_esid": fb_account_esid})
    page_ids = {str(p["_source"].get("fb_page_id")) for p in page_docs if p["_source"].get("fb_page_id") is not None}
    if not page_ids:
        return set()

    # Every ds_facebook team whose configured pages intersect this account's pages.
    team_docs = get_fb_teams(filters={"ds_facebook": "true"})
    owners: set[str] = set()
    for t in team_docs:
        src = t["_source"]
        team_pages = {str(pid) for pid in (src.get("team_facebook_page_id") or []) if pid is not None}
        if team_pages & page_ids:
            owners.add(t["_id"])
    return owners


def _resolve_owning_team(bare_account, fb_ad_account_esid, ad_account_id_to_team, scoped_team_by_esid):
    """Decide which team (if any) a window pull for this ad account belongs to.

    Two safe cases return a team; everything else returns None (skip, logged):
      1. account_wide: the account maps to exactly one account_wide team
         (``get_account_wide_teams_by_ad_account_id``) — unambiguous by design.
      2. page-filtered SOLE owner: exactly one ds_facebook team's pages reach the
         account (``_teams_reaching_ad_account``), and that team is in scope.

    A shared account (>1 reaching team) returns None — an ad-set-grain row can't
    be attributed to a page-subset of a shared account in one FB call.
    """
    # Case 1 — account_wide map (built by the caller).
    aw_team = ad_account_id_to_team.get(bare_account)
    if aw_team:
        return aw_team

    # Case 2 — page-filtered sole owner.
    if not fb_ad_account_esid:
        return None
    owners = _teams_reaching_ad_account(fb_ad_account_esid)
    if len(owners) == 1:
        sole = next(iter(owners))
        team = scoped_team_by_esid.get(sole)
        if team:
            return team
        logging.info(
            "window: ad account %s is sole-owned by team %s, but that team is not in " "this run's scope; skipping.",
            bare_account,
            sole,
        )
        return None
    # a shared account no longer means "skip" — the caller
    # (_import_shared_account_per_team) writes per-ad-set docs for each scoped
    # team's OWN ads (page->ad->ad.id filter). This is a routing note, not a skip.
    logging.info(
        "window: ad account %s is shared by %d teams %s (no single owner); "
        "the caller writes per-team per-ad-set docs (ad.id-filtered) for scoped teams.",
        bare_account,
        len(owners),
        sorted(owners) if owners else "[]",
    )
    return None


# per-ad creative fields for the page->ad resolution. Mirrors the
# daily importer's Round-1 batch fields (get_insights.py) but only the
# page-bearing bits. object_story_spec{page_id} is a diagnostic fallback for
# creatives that leave actor_id empty.
_AD_PAGE_FIELDS = "id,creative{actor_id,object_story_spec{page_id}}"


def _creative_page_id(creative: dict) -> str | None:
    """Page id from a creative body — actor_id (the importer's source) then
    object_story_spec.page_id fallback. None when neither is present."""
    actor_id = creative.get("actor_id")
    if actor_id is not None:
        return str(actor_id)
    page_id = (creative.get("object_story_spec") or {}).get("page_id")
    return str(page_id) if page_id is not None else None


def _account_ad_page_map(ad_account_id, since, until, token) -> dict[str, str | None] | None:
    """``{ad_id: promoted page_id or None}`` for every ad that DELIVERED
    (spend > 0) on this account in the window.

    The proven fetch shape (staging-verified): insights(level=ad) for
    the delivering set, then batch per-ad ``creative{actor_id}`` (50/req) — NOT
    AdAccount.get_ads, which 500s on nested creative. Page id resolution is
    ``_creative_page_id`` (actor_id, then object_story_spec.page_id fallback).

    Returns ``None`` when the level=ad call fails — the complement
    caller must fail CLOSED on that (skip the owner's docs), never fall back to
    an unfiltered whole-account pull. ``{}`` means nothing delivered.
    """
    check_and_pause_for_account(ad_account_id)
    try:
        rows = AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
            fields=["ad_id", "spend"],
            params={"time_range": {"since": since, "until": until}, "level": "ad", "limit": 1000},
        )
        delivering = [r["ad_id"] for r in rows if r.get("ad_id") and float(r.get("spend") or 0) > 0]
    except Exception as exc:  # noqa: BLE001 — per-account boundary
        logging.warning("level=ad insights failed for account %s: %s — no team ad ids.", ad_account_id, exc)
        return None
    if not delivering:
        return {}

    meta = batch_fb_metadata([(aid, _AD_PAGE_FIELDS) for aid in delivering], token)
    page_by_ad: dict[str, str | None] = {}
    for aid in delivering:
        body = meta.get(str(aid))
        if not body:
            continue
        page_by_ad[str(body.get("id") or aid)] = _creative_page_id(body.get("creative") or {})
    return page_by_ad


def _team_ad_ids_on_account(ad_account_id, since, until, page_ids, token) -> list[str]:
    """Ad ids on this account that (a) delivered in the window AND (b) belong to
    one of the team's pages (creative.actor_id ∈ page_ids).

    Thin filter over ``_account_ad_page_map`` (one fetch shape for the shared
    path and the complement). These ids scope the per-ad-set reach pull
    so a shared account's rows count only this team's ads.
    """
    if not page_ids:
        return []
    page_by_ad = _account_ad_page_map(ad_account_id, since, until, token)
    if not page_by_ad:
        return []
    return [aid for aid, pid in page_by_ad.items() if pid is not None and pid in page_ids]


# ── observed-delivery probe-set union ─────────────────────────────
#
# The asset chain cannot express "team A's ads are paid for by business B's ad
# account" (Meta advertiser-access grants have no asset-model edge — one team's
# ads on a sibling org's shared ad account, prod 2026-07-10). The daily importer's
# per-ad attribution (creative.actor_id page-match first, account_wide claim
# second) is ground truth AND its docs carry both the paying ad_account_id and
# the team — so the daily indices are the ONE place cross-business delivery is
# visible. These helpers surface that evidence to the window pull; window reach
# then follows the same attribution rule as daily impressions (Option B,
# a teammate + a teammate 2026-07-10 — see the private docs).


def _observed_teams_by_account(team_esids, since, until) -> dict[str, set[str]]:
    """``{bare ad_account_id: {team_esid}}`` observed DELIVERING in [since..until],
    aggregated from the teams' daily ``{esid}__fb_insights`` indices.

    Callers pass ALL ds_facebook teams, not just this run's scope: interloper
    DETECTION (sole-owner demotion, account_wide complement) must see delivery
    from out-of-scope teams; WRITES stay scope-gated in ``_import_window``.

    FAIL-OPEN: any ES problem returns {} — the earlier asset-chain-only
    probe set — because the union is additive, never load-bearing. The
    keyword-vs-text ``ad_account_id`` mapping drift across old daily indices is
    handled by grouping indices per queryable field path
    (``_resolve_ad_account_id_field_paths``).
    """
    if not team_esids:
        return {}
    observed: dict[str, set[str]] = {}
    try:
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("no ES client — observed-delivery union skipped (asset-chain probe set only).")
            return {}
        esid_by_index = {}
        for esid in team_esids:
            try:
                esid_by_index[build_data_index_name(esid, "fb_insights")] = esid
            except ValueError as exc:  # illegal leading char — skip that team
                logging.warning("cannot derive daily index for team %s: %s", esid, exc)
        if not esid_by_index:
            return {}
        field_paths = _daily._resolve_ad_account_id_field_paths(es, list(esid_by_index))
        for field_path, indices in field_paths.items():
            result = es.search(
                index=",".join(indices),
                body={
                    "size": 0,
                    "query": {"range": {"insight_date_start": {"gte": since, "lte": until}}},
                    "aggs": {
                        "per_index": {
                            "terms": {"field": "_index", "size": max(len(indices), 1)},
                            "aggs": {"accts": {"terms": {"field": field_path, "size": 200}}},
                        }
                    },
                },
                ignore_unavailable=True,
                allow_no_indices=True,
            )
            for idx_bucket in result.get("aggregations", {}).get("per_index", {}).get("buckets", []):
                esid = esid_by_index.get(idx_bucket.get("key"))
                if not esid:
                    continue
                for acct_bucket in idx_bucket.get("accts", {}).get("buckets", []):
                    bare = strip_ad_account_prefix(str(acct_bucket.get("key") or "")).strip()
                    if bare:
                        observed.setdefault(bare, set()).add(esid)
    except Exception as exc:  # noqa: BLE001 — fail-open by design (see docstring)
        logging.warning("observed-delivery aggregation failed: %s — asset-chain probe set only.", exc)
        return {}
    if observed:
        logging.info(
            "observed delivery on %d ad account(s) across the daily indices for %s..%s.",
            len(observed),
            since,
            until,
        )
    return observed


def _entries_for_observed_accounts(observed_by_account, scope_esids, import_params) -> list[dict]:
    """Synthetic import_params entries for accounts a SCOPED team was observed
    delivering on that the asset-chain walk didn't produce (cross-business
    delivery — the account belongs to another business entirely).

    The token comes from the account's OWN ``de_asset_fb_ad_accounts`` doc.
    For a wizard/legacy TWIN pair the two concerns split:

    * **Attribution doc** — always the LEGACY doc: its ``fb_app_esid`` chain is
      what ``_teams_reaching_ad_account`` walks; the wizard twin carries no
      chain, so choosing it would silently demote a sole-owned account to the
      shared path.
    * **Token** — the WIZARD connection is preferred (auto-refreshing
      ``de_connection`` token with the wizard's superset scopes, vs the legacy
      app-user token that dies at its 60-day expiry), with the
      legacy app-user token kept on the entry as a FALLBACK for when the
      connection can't resolve (``_resolve_entry_credentials``). earlier
      the legacy token was preferred outright.

    An observed account with no asset doc at all is skipped with a warning —
    there is no token to pull it with.
    """
    if not scope_esids:
        return []  # unscoped runs never write page-filtered teams; nothing to add
    existing = {strip_ad_account_prefix(str(e["fb_ad_account"].get("fb_ad_account_id") or "")) for e in import_params}
    entries: list[dict] = []
    for bare in sorted(set(observed_by_account) - existing):
        if not (observed_by_account[bare] & scope_esids):
            continue  # only add probe work for teams this run may write
        docs = get_fb_ad_accounts(filters={"fb_ad_account_id": bare}) or []
        if not docs:
            logging.warning(
                "team(s) %s observed delivering on ad account %s, but no "
                "de_asset_fb_ad_accounts doc exists for it — no token; skipping the account.",
                sorted(observed_by_account[bare]),
                bare,
            )
            continue
        if len(docs) > 1:
            logging.warning(
                "ad account %s has %d asset docs (wizard/legacy twins — ); "
                "keeping the legacy doc for attribution, preferring the wizard token.",
                bare,
                len(docs),
            )
        legacy_doc = next((d for d in docs if d["_source"].get("fb_app_user_esid")), None)
        wizard_doc = next((d for d in docs if d["_source"].get("wizard_connection_esid")), None)
        if not legacy_doc and not wizard_doc:
            logging.warning(
                "ad account %s asset doc carries neither fb_app_user_esid nor "
                "wizard_connection_esid — no token path; skipping the account.",
                bare,
            )
            continue
        chosen = legacy_doc or wizard_doc
        src = dict(chosen["_source"])
        src["_id"] = chosen["_id"]
        if legacy_doc:
            app_src = {}
            if src.get("fb_app_esid"):
                apps = get_fb_apps(filters={"_id": [src["fb_app_esid"]]}) or []
                if apps:
                    app_src = dict(apps[0]["_source"])
                    app_src["_id"] = apps[0]["_id"]
                    app_src["fb_app_secret"] = _daily.decrypt(app_src.get("fb_app_secret", ""))
            if wizard_doc:
                # hybrid twin entry: legacy doc (attribution chain) +
                # legacy app creds (token FALLBACK) + the wizard connection as
                # the PREFERRED token source (auto-refreshing, superset scopes).
                entries.append(
                    {
                        "fb_account": {},
                        "fb_app": app_src,
                        "fb_ad_account": src,
                        "import_mode": "account_wide",
                        "connection_esid": wizard_doc["_source"]["wizard_connection_esid"],
                    }
                )
                token_via = "wizard connection (legacy fallback + attribution chain)"
            else:
                entries.append({"fb_account": {}, "fb_app": app_src, "fb_ad_account": src, "import_mode": "page_filtered"})
                token_via = "legacy app-user"
        else:
            entries.append(
                {
                    "fb_account": {},
                    "fb_app": {},
                    "fb_ad_account": src,
                    "import_mode": "account_wide",
                    "connection_esid": src["wizard_connection_esid"],
                }
            )
            token_via = "wizard connection"
        logging.info(
            "added observed-delivery ad account %s to the window probe set " "(teams observed: %s; token via %s).",
            bare,
            sorted(observed_by_account[bare]),
            token_via,
        )
    return entries


def _resolve_entry_credentials(ad_account_entry):
    """``(fb_app_id, fb_app_secret, token)`` for one import_params entry.

    Extracted from the run loop so the twin-account fallback is
    testable. account_wide entries resolve via the wizard connection
    (``_resolve_account_wide_credentials``); when that fails AND the
    entry also carries a legacy chain (``fb_app_user_esid`` + app creds — the
     hybrid twin entry), fall back to the legacy app-user token rather
    than dropping the account: a dying wizard connection must not lose data a
    legacy credential can still pull. Plain legacy entries resolve exactly as
    before. Any path may return a falsy token — the caller warns and skips.
    """
    fb_app = ad_account_entry["fb_app"]
    fb_ad_account = ad_account_entry["fb_ad_account"]
    fb_app_id = fb_app.get("fb_app_id")
    fb_app_secret = fb_app.get("fb_app_secret")
    if ad_account_entry.get("import_mode") == "account_wide":
        w_app_id, w_secret, token = _daily._resolve_account_wide_credentials(
            ad_account_entry.get("connection_esid"),
        )
        if token:
            return w_app_id, w_secret, token
        if fb_ad_account.get("fb_app_user_esid") and fb_app_id:
            logging.warning(
                "wizard connection for ad account %s did not resolve — "
                "falling back to the legacy app-user token.",
                fb_ad_account.get("fb_ad_account_id"),
            )
            return fb_app_id, fb_app_secret, get_fb_app_user_token(fb_ad_account.get("fb_app_user_esid"))
        return w_app_id, w_secret, None
    return fb_app_id, fb_app_secret, get_fb_app_user_token(fb_ad_account.get("fb_app_user_esid"))


def _shared_page_winner_esid(claimants):
    """pick ONE owner for a page claimed by several teams on a
    shared account. Rule 2 of the daily precedence (the account-wide owner of
    the ad's account) is handled by ``exclude_pages`` before this runs; here
    the page-filtered claimant beats account-wide claimants (an account-wide
    team's page list exists for Echo referrer matching — its ads arrive via
    its account claim), and ties resolve to the lowest team ``_id`` so the
    winner is stable across runs. ``claimants`` is ``[(team_esid, team_src)]``.
    """
    page_filtered = [c for c in claimants if c[1].get("fb_import_mode") != "account_wide"]
    pool = page_filtered or claimants
    return min(pool, key=lambda c: str(c[0]))[0]


def _import_shared_account_per_team(
    ad_account_id,
    bare_account,
    fb_ad_account_esid,
    since,
    until,
    window_label,
    token,
    teams_on_account,
    team_reach_acc=None,
    page_reach_acc=None,
    demo_reach_acc=None,
    asset_marg_acc=None,
    exclude_pages=None,
) -> int:
    """write per-ad-set window docs for EACH page-filtered team that
    reaches a SHARED ad account, scoped to that team's own ads.

    The a shared account (>1 team splits it by page) was skipped
    entirely, so its teams' Ad Sets reach stayed on the over-counting per-day
    index. Here, per team: resolve the team's ads on this account (page->ad), then
    pull ``level=adset filtering ad.id IN <team ads>`` (breakdown + combined,
    reusing _fetch_and_write_account) so each ad-set row is deduped over ONLY the
    team's ads — honest even when an ad set also carries other teams' ads.

    ``teams_on_account`` is ``{team_esid: team_src}`` for the scoped teams whose
    pages reach this account. Returns docs written across all of them.

    ``team_reach_acc`` — the cross-account team-reach accumulator. When
    given, each team's ``ad.id IN <team ads>`` level=account deduped reach on THIS
    shared account is added to it; the caller flushes one grain="team" doc per team
    after the whole account loop.

    ``page_reach_acc`` — the cross-account (team, page) accumulator.
    When given, each team's ads are grouped by promoted page (one lazy
    ``_account_ad_page_map`` fetch per account, same laziness as the
    effective_status cache) and each page gets its exact deduped figure.

    ``exclude_pages`` — pages the account's OWNING team claims;
    dropped from every team's match set here because the owner's complement
    already kept those ads. Shared pages AMONG the teams on this account
    additionally resolve to one winner each (:func:`_shared_page_winner_esid`)
    so the same ad is never written for two teams.
    """
    written = 0
    exclude_pages = {str(p) for p in (exclude_pages or set())}
    # per-page claimant lists among the teams on THIS account.
    claimants_by_page = {}
    for esid, t in teams_on_account.items():
        for p in t.get("team_facebook_page_id") or []:
            if p is not None:
                claimants_by_page.setdefault(str(p), []).append((esid, t))
    # ONE effective_status fetch per ACCOUNT, not per team. This
    # function runs once per shared account (the caller's seen_accounts dedup),
    # but the fetch sat inside the team loop: an account shared by 9 teams
    # re-requested the identical get_ad_sets payload up to 9 times, and Meta
    # throttles from the 2nd call on (code 17 "Ad Account Has Too Many API
    # Calls") — each team then burned the 60s+120s retry ladder. Lazy so an
    # account where no scoped team has delivering ads still makes zero calls.
    effective_status_by_adset = None
    # lazy delivery_estimate map, ONE batched fetch per account (rides
    # the effective_status ad-set list, same laziness rationale).
    delivery_estimate_by_adset = None
    # lazy ad→page map, ONE fetch per account (only when a scoped team
    # has delivering ads AND the caller wants page docs), for the per-page grouping.
    page_by_ad = None
    for team_esid, team in teams_on_account.items():
        # per-team Ad Insights switch: an opted-out team writes no
        # window docs on this shared account (mirrors the daily importer's
        # claiming-map filter).
        if not _daily._source_enabled_for_team(team_esid, "fb_ads"):
            logging.info("fb_ads disabled for team %s — skipping its window pull on this account.", team_esid)
            continue
        page_ids = {str(p) for p in (team.get("team_facebook_page_id") or []) if p is not None}
        # drop the owner's pages and shared pages this team loses.
        contested = {p for p in page_ids if p in exclude_pages or len(claimants_by_page.get(p, [])) > 1}
        page_ids = {
            p
            for p in page_ids
            if p not in exclude_pages
            and (len(claimants_by_page.get(p, [])) < 2 or _shared_page_winner_esid(claimants_by_page[p]) == team_esid)
        }
        if contested - page_ids:
            logging.info(
                "team %s on shared account %s — page(s) %s attributed to another claimant.",
                team_esid,
                bare_account,
                sorted(contested - page_ids),
            )
        ad_ids = _team_ad_ids_on_account(ad_account_id, since, until, page_ids, token) if page_ids else []
        if not ad_ids:
            logging.info(
                "shared-account: team %s has 0 delivering ads on its pages in account %s "
                "over %s..%s — no window docs (team ran no ads on its pages this window).",
                team_esid,
                bare_account,
                since,
                until,
            )
            continue
        team_fields = {
            "team_site_url": team.get("team_url", "unknown"),
            "team_title": team.get("team_title", "unknown"),
            "team_country": _resolve_team_country(team) or "unknown",
            "team_city": team.get("team_city", "unknown"),
            "team_esid": team_esid,
            "organization_title": team.get("organization_title", "Unknown"),
        }
        if effective_status_by_adset is None:
            effective_status_by_adset = _fetch_adset_effective_status(ad_account_id)
        if delivery_estimate_by_adset is None:
            delivery_estimate_by_adset = _fetch_adset_delivery_estimates(list(effective_status_by_adset), token)
        logging.info(
            "shared-account: team %s — %d ad(s) on its pages in account %s; writing per-ad-set docs.",
            team_esid,
            len(ad_ids),
            bare_account,
        )
        for breakdown in (True, False):
            written += _fetch_and_write_account(
                ad_account_id,
                since,
                until,
                window_label,
                team_fields,
                fb_ad_account_esid,
                breakdown=breakdown,
                effective_status_by_adset=effective_status_by_adset,
                delivery_estimate_by_adset=delivery_estimate_by_adset,
                ad_id_filter=ad_ids,
            )
            # AD-grain twin of the pull above, same ad.id scoping.
            written += _fetch_and_write_account_ads(
                ad_account_id,
                since,
                until,
                window_label,
                team_fields,
                fb_ad_account_esid,
                breakdown=breakdown,
                ad_id_filter=ad_ids,
            )
        # team-grain deduped reach for this team's ads on this SHARED
        # account (ad.id IN <team ads>, level=account). Added to the cross-account
        # accumulator; one grain="team" doc flushed per team after the account loop.
        team_account_reach = None
        if team_reach_acc is not None:
            team_account_reach = _fetch_team_account_reach(ad_account_id, since, until, ad_ids=ad_ids)
            _accumulate_team_reach(team_reach_acc, team_esid, team_fields, team_account_reach)
        # page-grain reach for this team's pages on this account. A
        # single-page team reuses team_account_reach (no extra Meta call).
        if page_reach_acc is not None:
            if page_by_ad is None:
                page_by_ad = _account_ad_page_map(ad_account_id, since, until, token) or {}
            _pull_page_reach(
                ad_account_id,
                since,
                until,
                _pages_of_ads(page_by_ad, ad_ids=ad_ids),
                team_esid,
                team_fields,
                page_reach_acc,
                team_account_reach=team_account_reach,
                team_ad_ids=ad_ids,
            )
        # demographics grain for this team's ads on this account:
        # one extra level=account + breakdowns=[age,gender] call, cells
        # accumulated across the team's accounts like the team grain.
        # skipped entirely when the team's workspace opted out.
        if demo_reach_acc is not None:
            if _demographics_enabled_for_team(team_esid):
                cells = _fetch_team_demographics(ad_account_id, since, until, ad_ids=ad_ids)
                _accumulate_demo_reach(demo_reach_acc, team_esid, team_fields, cells)
            else:
                logging.info("demographics pull disabled for team %s — skipping.", team_esid)
        # asset grain for this team's ads on this shared account.
        if _video_assets_enabled_for_team(team_esid):
            written += _fetch_and_write_account_assets(
                ad_account_id,
                since,
                until,
                window_label,
                team_fields,
                fb_ad_account_esid,
                asset_marg_acc,
                ad_id_filter=ad_ids,
            )
        else:
            logging.info("video-asset pull disabled for team %s — skipping.", team_esid)
    return written


def _import_window(
    since: str,
    until: str,
    import_params,
    ad_account_id_to_team,
    scoped_team_by_esid,
    progress_run_id=None,
    observed_by_account=None,
    pages_by_team_esid=None,
) -> int:
    """Run one window's ad-set-grain pull per ad account, write JSON, return docs written.

    ONE ``AdAccount.get_insights(level="adset", breakdowns=["publisher_platform"])``
    call per account returns every (ad set, platform) row with FB-deduplicated reach
    for the window. Each row → one window doc, team resolved by ad account.

    ``progress_run_id`` — when set (dedup-only refresh), the loop posts
    per-account progress to the run's ``de_import_progress`` doc: the doc's units
    are rebased to (accounts done / accounts total) so the dashboard pill advances
    through a multi-account pull instead of sitting at 0%. Soft-fail by design.

    ``observed_by_account`` — ``{bare account: {team_esid}}`` observed
    delivering in the window (daily-index ground truth, ALL teams). Drives the
    three Option-B behaviors: the shared-probe union, page-filtered sole-owner
    demotion, and the account_wide owner's complement filter. ``None``/{} =
    earlier behavior exactly.

    ``pages_by_team_esid`` — ``{team_esid: {page_id}}`` for ALL ds_facebook teams;
    the complement needs every OTHER team's pages (daily's page-match consults all
    teams, so the window must too — a scoped-only set would silently re-absorb an
    out-of-scope team's ads into the owner).
    """
    observed_by_account = observed_by_account or {}
    pages_by_team_esid = pages_by_team_esid or {}
    window_label = f"{since}..{until}"
    logging.info("--- Window import (ad-set grain): %s ---", window_label)

    docs_written = 0
    seen_accounts: set[str] = set()
    # accumulate each team's per-account deduped reach across the whole
    # account loop (a team can reach 2+ accounts), then flush ONE grain="team" doc
    # per team after the loop. Both the owning-team and shared-account paths feed it.
    team_reach_acc: dict[str, dict] = {}
    # same shape for (team, page) pairs; ONE grain="page" doc per pair
    # flushed after the loop.
    page_reach_acc: dict[tuple, dict] = {}
    # same shape for (team, age, gender) cells; ONE grain="demographics"
    # doc per cell (plus computed marginals) flushed after the loop.
    demo_reach_acc: dict[tuple, dict] = {}
    # per-asset MARGINAL accumulator keyed (team, video_asset_id);
    # cell docs write directly per account (an ad set lives in one account).
    asset_marg_acc: dict[tuple, dict] = {}

    # the workload is known up front: one pass per DISTINCT ad account
    # (import_params can carry the same account under several asset-chain paths;
    # the seen_accounts dedup below skips repeats, so they are not work units).
    accounts_total = len({strip_ad_account_prefix(str(e["fb_ad_account"].get("fb_ad_account_id"))) for e in import_params})
    accounts_done = 0

    def _bump_progress():
        """Post accounts_done/accounts_total to the progress doc. Never raises."""
        if not progress_run_id:
            return
        try:
            from lib.import_progress_service import ImportProgressService

            ImportProgressService.update_progress(
                run_id=progress_run_id, days_fetched=accounts_done, days_total=accounts_total
            )
        except Exception as exc:  # noqa: BLE001 — a progress write must never abort the pull
            logging.warning("window progress write failed (run_id=%r): %s", progress_run_id, exc)

    _bump_progress()  # 0/N — flips the doc to running and shows the account total

    for ad_account_entry in import_params:
        fb_ad_account = ad_account_entry["fb_ad_account"]

        ad_account_id = fb_ad_account.get("fb_ad_account_id")
        bare_account = strip_ad_account_prefix(str(ad_account_id))

        # An ad account can appear under multiple import_params entries (one per
        # asset-chain path); the ad-set call is account-level, so run it once.
        if bare_account in seen_accounts:
            continue
        seen_accounts.add(bare_account)

        fb_ad_account_esid = fb_ad_account.get("_id")

        # Resolve the owning team: account_wide map OR page-filtered sole owner.
        # A SHARED account (>1 scoped team splits it by page) returns None here.
        team = _resolve_owning_team(bare_account, fb_ad_account_esid, ad_account_id_to_team, scoped_team_by_esid)

        # teams observed delivering on this account in the window
        # (daily-index ground truth; sees the cross-business arrangements the
        # asset walk can't). A page-filtered SOLE owner with observed company is
        # demoted to the shared path here: the account is factually shared, and
        # leaving the owner on the whole-account pull would absorb the other
        # teams' ads into its reach. The demoted owner keeps its PAGE-MATCHED
        # ads via the shared helper — daily gives page-filtered teams no
        # second-chance claim, so neither does the window.
        observed_teams = observed_by_account.get(bare_account, set())
        demoted_sole_esid = None
        if team and bare_account not in ad_account_id_to_team:
            observed_others = observed_teams - {team.get("_id")}
            if observed_others:
                logging.info(
                    "sole-owner account %s also carries observed delivery from team(s) %s — "
                    "demoting to the shared (ad.id-filtered) path.",
                    bare_account,
                    sorted(observed_others),
                )
                demoted_sole_esid = team.get("_id")
                team = None

        # token resolution branches on the entry's import mode, exactly
        # like the daily importer. A wizard-cohort (account_wide) entry
        # carries NO fb_app_user_esid — its token lives in
        # de_connection.oauth_token_enc and the Meta app creds come from the shared
        # wizard app. The legacy-only lookup below could never resolve it, so every
        # connection-flow team's account was silently dropped from the window pull:
        # daily data current, window/team-grain reach permanently "—" (one team,
        # prod 2026-07-10). _resolve_account_wide_credentials already handles the
        # revoked / dangling-pointer / wrong-provider connections and records the
        # specific user error for each. a hybrid twin entry (legacy
        # chain + wizard connection) falls back to its legacy app-user token when
        # the connection can't resolve; see _resolve_entry_credentials.
        # bind the (post-demotion) resolved team so the token
        # record site below carries per-team attribution; None for shared /
        # demoted accounts, which is honest — their error belongs to no single
        # team — and never inherits the previous account's team.
        from lib.import_run_context import set_current_team

        set_current_team(team.get("_id") if team else None)

        fb_app_id, fb_app_secret, token = _resolve_entry_credentials(ad_account_entry)
        if not token:
            account_title = fb_ad_account.get("fb_ad_account_title", "Unknown")
            logging.warning("Could not get user token for ad account %s. Skipping.", account_title)
            # a token-less account drops ALL of its window/team-grain
            # reach; that must reach the user, not just the container log. The
            # "no oauth token" wording classifies to missing_credentials
            # ("reconnect it under Data Connectors"); dedup by category+detail
            # keeps the account_wide resolver's more specific entry (revoked /
            # wrong-provider) alongside rather than duplicated. No-op without a
            # bound run id (cron / full-import tail).
            try:
                from lib.import_run_context import record_user_error

                record_user_error(
                    f"No OAuth token for ad account {account_title} — window reach skipped.",
                    connector_id="meta_ads",
                    detail=account_title,
                )
            except Exception as exc:  # noqa: BLE001 — surfacing must never break the pull
                logging.warning("failed to record token-skip error: %s", exc)
            accounts_done += 1  # skipped-for-token still consumes its work unit
            _bump_progress()
            continue

        init_facebook_api(fb_app_id, fb_app_secret, token)

        # shared account: no single owning team, but the scoped teams
        # whose pages reach it EACH get per-ad-set docs scoped to their own ads
        # (page->ad filter), instead of the earlier whole-account skip. Only
        # fires for teams in THIS run's scope (we never write an out-of-scope team).
        # the probe list is the asset walk UNIONED with observed
        # delivery (plus a demoted sole owner), so a team whose ads run on an
        # account outside its derived chain (cross-business) is still probed.
        if not team:
            reaching = _teams_reaching_ad_account(fb_ad_account_esid) if fb_ad_account_esid else set()
            candidates = set(reaching) | observed_teams
            if demoted_sole_esid:
                candidates.add(demoted_sole_esid)
            teams_on_account = {esid: scoped_team_by_esid[esid] for esid in candidates if esid in scoped_team_by_esid}
            if teams_on_account:
                docs_written += _import_shared_account_per_team(
                    ad_account_id,
                    bare_account,
                    fb_ad_account_esid,
                    since,
                    until,
                    window_label,
                    token,
                    teams_on_account,
                    team_reach_acc=team_reach_acc,
                    page_reach_acc=page_reach_acc,
                    demo_reach_acc=demo_reach_acc,
                    asset_marg_acc=asset_marg_acc,
                )
            accounts_done += 1  # shared (or unattributable) account done
            _bump_progress()
            continue

        team_fields = {
            "team_site_url": team.get("team_url", "unknown"),
            "team_title": team.get("team_title", "unknown"),
            "team_country": _resolve_team_country(team) or "unknown",
            "team_city": team.get("team_city", "unknown"),
            "team_esid": team.get("_id", "unknown"),
            "organization_title": team.get("organization_title", "Unknown"),
        }

        # Option B — an account_wide owner does NOT absorb other teams'
        # ads. When the daily index shows OTHER teams delivering on this account,
        # the owner's window ad set is the daily rule's complement: its own
        # page-matched ads + ads matching NO team's page (daily's second-chance
        # claim). No interloper actually delivering → the complement IS the whole
        # account (unchanged, and the cheap unfiltered call is kept); every
        # delivering ad another team's → the owner writes nothing (reach "—",
        # matching its daily zeros — team A/team B, prod 2026-07-10). Fails
        # CLOSED on a fetch error: skipping a run beats shipping the overcount.
        owner_esid = team_fields["team_esid"]
        observed_others = observed_teams - {owner_esid}
        owner_ad_id_filter = None
        skip_owner_docs = False
        if observed_others:
            page_by_ad = _account_ad_page_map(ad_account_id, since, until, token)
            if page_by_ad is None:
                skip_owner_docs = True
                logging.warning(
                    "ad page map fetch failed for overlap account %s — skipping owner %s's "
                    "window docs this run (fail-closed; no whole-account overcount).",
                    bare_account,
                    owner_esid,
                )
            else:
                other_pages: set[str] = set()
                for other_esid, pages in pages_by_team_esid.items():
                    if other_esid != owner_esid:
                        other_pages |= pages
                # a page the OWNER itself claims is not "another
                # team's". Two teams both claim the same page; the
                # blanket union put the shared page in other_pages and emptied
                # the owner's complement — the account-wide owner wrote ZERO
                # window docs for its own account (the window twin of the
                # daily one-team-per-page steal). In its own account the owner
                # keeps its shared pages' ads; the interlopers' per-team pull
                # below excludes them symmetrically via exclude_pages.
                other_pages -= pages_by_team_esid.get(owner_esid, set())
                owner_ads = [aid for aid, pid in page_by_ad.items() if pid is None or pid not in other_pages]
                if not owner_ads:
                    skip_owner_docs = True
                    logging.info(
                        "every delivering ad on account %s belongs to other teams' pages — "
                        'owner %s gets no window docs (reach stays "—", matching its daily zeros).',
                        bare_account,
                        owner_esid,
                    )
                elif len(owner_ads) < len(page_by_ad):
                    owner_ad_id_filter = owner_ads
                    logging.info(
                        "overlap account %s — owner %s keeps %d of %d delivering ad(s) "
                        "(complement of other teams' page-matched ads).",
                        bare_account,
                        owner_esid,
                        len(owner_ads),
                        len(page_by_ad),
                    )
                # else: every delivering ad is the owner's own / unmatched —
                # the whole account is the honest set; keep the None filter.

        # per-team Ad Insights switch: an opted-out OWNER writes no
        # window docs on its own account. Scoped teams observed on this account
        # are gated individually inside _import_shared_account_per_team.
        if not skip_owner_docs and not _daily._source_enabled_for_team(team_fields["team_esid"], "fb_ads"):
            skip_owner_docs = True
            logging.info(
                "fb_ads disabled for team %s — skipping its window pull on account %s.",
                team_fields["team_esid"],
                bare_account,
            )

        if not skip_owner_docs:
            # Ad-set effective_status map (one get_ad_sets call per account). The
            # insights call doesn't return it, so we look it up once and stamp every
            # doc for this account — the table's "Effective Delivery" column..
            effective_status_by_adset = _fetch_adset_effective_status(ad_account_id)

            # estimated audience size per ad set (delivery_estimate
            # edge), one batched fetch per account riding the effective_status
            # ad-set list.
            delivery_estimate_by_adset = _fetch_adset_delivery_estimates(list(effective_status_by_adset), token)

            # Two ad-set-grain pulls per account:
            #   1. WITH publisher_platform breakdown → one doc per (ad set, platform).
            #      Serves the table when a platform filter is active.
            #   2. WITHOUT the breakdown → one doc per ad set carrying FB's
            #      CROSS-PLATFORM deduplicated reach, stamped publisher_platform="all".
            #      Serves the all-platforms view. Confirmed 2026-06-03 to equal Data
            #      Studio's all-platforms reach to the digit. Combined reach ≥ any
            #      single platform, so the table's `max` agg naturally picks "all"
            #      when unfiltered and the platform term-filter excludes "all" when set
            #      — no read-path change needed.
            docs_written += _fetch_and_write_account(
                ad_account_id,
                since,
                until,
                window_label,
                team_fields,
                fb_ad_account_esid,
                breakdown=True,
                effective_status_by_adset=effective_status_by_adset,
                delivery_estimate_by_adset=delivery_estimate_by_adset,
                ad_id_filter=owner_ad_id_filter,
            )
            docs_written += _fetch_and_write_account(
                ad_account_id,
                since,
                until,
                window_label,
                team_fields,
                fb_ad_account_esid,
                breakdown=False,
                effective_status_by_adset=effective_status_by_adset,
                delivery_estimate_by_adset=delivery_estimate_by_adset,
                ad_id_filter=owner_ad_id_filter,
            )

            # AD-grain twin of the two pulls above (breakdown +
            # combined), same complement scoping.
            for _ad_breakdown in (True, False):
                docs_written += _fetch_and_write_account_ads(
                    ad_account_id,
                    since,
                    until,
                    window_label,
                    team_fields,
                    fb_ad_account_esid,
                    breakdown=_ad_breakdown,
                    ad_id_filter=owner_ad_id_filter,
                )

            # team-grain deduped reach for THIS account. With no
            # complement in play the owning team (account_wide OR
            # page-filtered sole owner) has the WHOLE account to itself, so the
            # account-level reach with NO ad.id filter IS the team's cross-ad-set
            # deduped reach; on an overlap account the same call is scoped to the
            # owner's complement ads. Accumulate across the team's accounts; one
            # doc flushed after the loop.
            team_account_reach = _fetch_team_account_reach(ad_account_id, since, until, ad_ids=owner_ad_id_filter)
            _accumulate_team_reach(team_reach_acc, team_fields["team_esid"], team_fields, team_account_reach)

            # page-grain reach for the owner's pages on this account.
            # The overlap path already fetched the ad→page map; a
            # non-overlap account pays the +1 level=ad call here. Fail-open: no
            # map → no page docs (the read path falls back to the
            # amber daily-sum), never a wrong figure. A single-page account
            # reuses team_account_reach (no extra level=account call).
            owner_page_map = page_by_ad if observed_others else _account_ad_page_map(ad_account_id, since, until, token)
            if owner_page_map:
                _pull_page_reach(
                    ad_account_id,
                    since,
                    until,
                    _pages_of_ads(owner_page_map, ad_ids=owner_ad_id_filter),
                    team_fields["team_esid"],
                    team_fields,
                    page_reach_acc,
                    team_account_reach=team_account_reach,
                    team_ad_ids=owner_ad_id_filter if owner_ad_id_filter is not None else list(owner_page_map),
                )

            # demographics grain for the owner's ads on this account
            # (whole account, or the complement when one applies).
            # skipped entirely when the owner's workspace opted out.
            if _demographics_enabled_for_team(team_fields["team_esid"]):
                cells = _fetch_team_demographics(ad_account_id, since, until, ad_ids=owner_ad_id_filter)
                _accumulate_demo_reach(demo_reach_acc, team_fields["team_esid"], team_fields, cells)
            else:
                logging.info("demographics pull disabled for team %s — skipping.", team_fields["team_esid"])

            # asset grain for the owner's ads on this account (whole
            # account, or the complement when one applies).
            if _video_assets_enabled_for_team(team_fields["team_esid"]):
                docs_written += _fetch_and_write_account_assets(
                    ad_account_id,
                    since,
                    until,
                    window_label,
                    team_fields,
                    fb_ad_account_esid,
                    asset_marg_acc,
                    ad_id_filter=owner_ad_id_filter,
                )
            else:
                logging.info("video-asset pull disabled for team %s — skipping.", team_fields["team_esid"])

        # scoped teams observed delivering on the OWNER's account get
        # their own page-matched (ad.id-IN) docs, exactly like a shared account.
        others_scoped = {esid: scoped_team_by_esid[esid] for esid in observed_others if esid in scoped_team_by_esid}
        if others_scoped:
            docs_written += _import_shared_account_per_team(
                ad_account_id,
                bare_account,
                fb_ad_account_esid,
                since,
                until,
                window_label,
                token,
                others_scoped,
                team_reach_acc=team_reach_acc,
                page_reach_acc=page_reach_acc,
                demo_reach_acc=demo_reach_acc,
                asset_marg_acc=asset_marg_acc,
                # pages the OWNER claims stay the owner's in its own
                # account; an interloper sharing one of them must not re-pull
                # those ads (the owner's complement above kept them).
                exclude_pages=pages_by_team_esid.get(owner_esid, set()),
            )

        accounts_done += 1  # owning-team account done
        _bump_progress()

    # flush ONE grain="team" deduped-reach doc per accumulated team
    # (cross-account summed; single-account = exact). Additive to the ad-set docs.
    team_docs = _flush_team_grain_docs(team_reach_acc, since, until, window_label)
    docs_written += team_docs

    # flush ONE grain="page" doc per accumulated (team, page). The
    # inventory name lookup only runs when there is something to flush.
    page_docs = _flush_page_grain_docs(page_reach_acc, since, until, window_label, _page_name_map() if page_reach_acc else {})
    docs_written += page_docs

    # flush ONE grain="demographics" doc per accumulated
    # (team, age, gender) cell plus the computed marginals.
    demo_docs = _flush_demo_grain_docs(demo_reach_acc, since, until, window_label)
    docs_written += demo_docs

    # flush ONE grain="asset" marginal doc per accumulated
    # (team, video asset). Cell docs were written inline per account.
    asset_marginal_docs = _flush_asset_marginal_docs(asset_marg_acc, since, until, window_label)
    docs_written += asset_marginal_docs

    logging.info(
        "Window %s: wrote %d ad-set/ad/asset doc(s) + %d team-grain doc(s) + %d page-grain doc(s) "
        "+ %d demographics doc(s) + %d asset-marginal doc(s)",
        window_label,
        docs_written - team_docs - page_docs - demo_docs - asset_marginal_docs,
        team_docs,
        page_docs,
        demo_docs,
        asset_marginal_docs,
    )
    return docs_written


_COMBINED_PLATFORM = "all"  # publisher_platform sentinel for the no-breakdown (cross-platform dedup) doc

# Retries for the per-account effective_status fetch when FB throttles it (code 17).
# 3 attempts → backoffs of 60s, 120s, then degrade to a blank column.
_EFFSTATUS_MAX_RETRIES = 3


def _fetch_adset_effective_status(ad_account_id) -> dict[str, str]:
    """Return ``{adset_id: effective_status}`` for every ad set in an account.

    the window pull is ``level=adset`` *insights*, which carry no
    ``effective_status`` — that field lives on the ad-set object, not on an
    insights row. The per-day importer (``get_insights.py``) gets it from
    per-AD metadata; at ad-set grain the natural source is the ad set's OWN
    computed delivery state (FB factors in the parent campaign/account), which
    is exactly what Data Studio's "Effective Delivery" column reads.

    ONE ``AdAccount.get_ad_sets`` call per account returns every ad set's
    ``effective_status`` — far cheaper than per-ad-set ``api_get``. Returns an
    empty map on failure (per-account boundary); the caller then leaves the
    column unstamped rather than aborting the window pull.
    """
    # follow-up: this get_ad_sets call shares the per-account rate budget
    # with the two get_insights calls that bracket it. Under bursty multi-account
    # pulls FB throttles it (code 17 / subcode 2446079, "Ad Account Has Too Many
    # API Calls"), which silently blanked the Effective Delivery column for whole
    # accounts. Retry the transient throttle with exponential backoff (matching the
    # per-ad insights backoff in get_insights.py) before degrading to a blank column.
    # App Review will raise the rate tier and make this rare, but the retry is the
    # correct behavior regardless. Non-transient errors still fail soft immediately.
    for attempt in range(_EFFSTATUS_MAX_RETRIES):
        try:
            ad_sets = AdAccount(normalize_ad_account_id(ad_account_id)).get_ad_sets(
                fields=["id", "effective_status"],
                params={"limit": 1000},
            )
            status_by_adset = {
                str(a.get("id")): a.get("effective_status") for a in ad_sets if a.get("id") and a.get("effective_status")
            }
            logging.info(
                "window: fetched effective_status for %d ad set(s) in account %s.",
                len(status_by_adset),
                ad_account_id,
            )
            return status_by_adset
        except FacebookRequestError as exc:
            # Code 17 ("User request limit reached" / subcode 2446079 "Ad Account
            # Has Too Many API Calls") is FB's app-level per-account throttle. Code
            # is the canonical signal; subcode is a refinement we don't depend on.
            is_throttle = exc.api_error_code() == 17
            if is_throttle and attempt < _EFFSTATUS_MAX_RETRIES - 1:
                backoff = 60 * (2**attempt)  # 60s, 120s
                logging.warning(
                    "window: get_ad_sets(effective_status) throttled for account %s "
                    "(attempt %d/%d); sleeping %ds before retry.",
                    ad_account_id,
                    attempt + 1,
                    _EFFSTATUS_MAX_RETRIES,
                    backoff,
                )
                time.sleep(backoff)
                check_and_pause_for_account(ad_account_id)
                continue
            logging.warning(
                "window: get_ad_sets(effective_status) failed for account %s: %s — "
                "Effective Delivery column will be blank for this account's ad sets.",
                ad_account_id,
                exc,
            )
            return {}
        except Exception as exc:  # noqa: BLE001 — per-account boundary; missing column beats a failed pull
            logging.warning(
                "window: get_ad_sets(effective_status) failed for account %s: %s — "
                "Effective Delivery column will be blank for this account's ad sets.",
                ad_account_id,
                exc,
            )
            return {}
    return {}


# ── ad-set estimated audience size (delivery_estimate) ────────────
#
# Meta replaced exact "potential reach" with an estimated-audience-size RANGE
# in 2022: the ad set's ``delivery_estimate`` edge returns
# estimate_mau_lower_bound / estimate_mau_upper_bound for the targeting spec —
# a fetch-time snapshot of TARGETABLE users, not people reached. It lives on an
# EDGE (not an ad-set field), so it can't ride the single get_ad_sets call the
# effective_status column uses; instead one Graph Batch POST per 50 ad sets
# (the batch_fb_metadata shape, but with an edge relative_url). Stamped onto
# the window ad-set docs next to effective_status — the same "current state"
# semantics — and rendered as a range ("1.2M–1.4M") by the dashboard.
_DELIVERY_ESTIMATE_KEYS = ("estimate_mau_lower_bound", "estimate_mau_upper_bound")


def _fetch_adset_delivery_estimates(adset_ids, token) -> dict[str, dict]:
    """``{adset_id: {estimate_mau_lower_bound, estimate_mau_upper_bound}}`` for
    the given ad sets, via batched ``{id}/delivery_estimate`` edge requests.

    Fail-open everywhere (per-account boundary AND per-item): an ad set whose
    estimate is unavailable (permission gap, estimate not ready, deleted
    targeting) simply gets no fields — a blank column beats a failed pull.
    """
    if not adset_ids:
        return {}
    import json as _json

    import requests as req_lib

    estimates: dict[str, dict] = {}
    ids = [str(a) for a in adset_ids]
    for chunk_start in range(0, len(ids), _daily.FB_BATCH_SIZE):
        chunk = ids[chunk_start : chunk_start + _daily.FB_BATCH_SIZE]
        batch_items = [{"method": "GET", "relative_url": f"{aid}/delivery_estimate"} for aid in chunk]
        try:
            resp = req_lib.post(
                _daily.FB_GRAPH_URL,
                data={"access_token": token, "batch": _json.dumps(batch_items)},
                timeout=_daily.FB_GRAPH_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            batch_response = resp.json()
        except Exception as exc:  # noqa: BLE001 — per-account boundary; blank column beats a failed pull
            logging.warning(
                "delivery_estimate batch failed (chunk +%d of %d ad sets): %s",
                chunk_start,
                len(ids),
                exc,
            )
            continue
        for i, item in enumerate(batch_response or []):
            if not isinstance(item, dict) or item.get("code") != 200:
                continue
            try:
                body = _json.loads(item.get("body", "{}"))
            except (ValueError, TypeError):
                continue
            rows = body.get("data") or []
            row = rows[0] if rows and isinstance(rows[0], dict) else {}
            est = {k: row[k] for k in _DELIVERY_ESTIMATE_KEYS if row.get(k) is not None}
            if est:
                estimates[chunk[i]] = est
    logging.info(
        "delivery_estimate resolved for %d of %d ad set(s).",
        len(estimates),
        len(ids),
    )
    return estimates


# the combined ("all") doc is the source of truth ONLY for non-additive
# metrics (FB's cross-platform dedup). Its ADDITIVE metrics (impressions, spend,
# clicks, actions, cost_per_*, ...) are IDENTICAL to the sum of the per-platform
# docs — so if the combined doc also carried them, an all-platforms table that
# SUMS additive metrics would double-count (per-platform docs + the 'all' doc).
# Strip every insight_* field from the combined doc EXCEPT this keep-set; the
# additive total then comes solely from summing the per-platform docs, and the
# 'all' doc contributes only its (max-read) non-additive values. Non-insight_
# fields (identity / window / team) are always kept.
_COMBINED_KEEP_INSIGHT_FIELDS = frozenset(
    {
        # Non-additive metrics — the whole reason the combined doc exists.
        "insight_reach",
        "insight_frequency",
        "insight_cpp",
        "insight_estimated_ad_recallers",
        "insight_estimated_ad_recall_rate",
        "insight_full_view_reach",
        "insight_account_currency",  # harmless label, keep for completeness
        # Identity / window fields that happen to share the insight_ prefix —
        # NOT metrics; must survive the strip or the doc loses its window keys.
        "insight_window_start",
        "insight_window_end",
        "insight_window_label",
    }
)


def _fetch_and_write_account(
    ad_account_id,
    since,
    until,
    window_label,
    team_fields,
    fb_ad_account_esid,
    *,
    breakdown,
    effective_status_by_adset=None,
    delivery_estimate_by_adset=None,
    ad_id_filter=None,
):
    """One ad-set-grain insights pull for an account + write its docs. Returns docs written.

    ``breakdown=True``  → ``breakdowns=[publisher_platform]``, one doc per (ad set, platform).
    ``breakdown=False`` → no breakdown, one doc per ad set stamped publisher_platform="all"
    (FB's cross-platform deduplicated reach for the window).

    ``effective_status_by_adset`` (``{adset_id: effective_status}``) is stamped onto each
    doc so the table's "Effective Delivery" column matches Data Studio..

    ``ad_id_filter`` — when given, the pull is ``filtering: ad.id IN
    <ids>`` so the ad-set rows are deduplicated over ONLY those ads. This is how a
    page-filtered team on a SHARED account gets honest per-ad-set reach: the ids
    are the team's ads (resolved page→ad by creative.actor_id), so an ad set that
    also holds OTHER teams' ads still reports only this team's reach. None → the
    account-wide/sole-owner behavior (every ad set, unchanged). Proven on staging
    2026-07-09 (one team's rows matched Data Studio to the digit).
    """
    effective_status_by_adset = effective_status_by_adset or {}
    delivery_estimate_by_adset = delivery_estimate_by_adset or {}
    check_and_pause_for_account(ad_account_id)
    params = {
        "time_range": {"since": since, "until": until},
        "level": "adset",
        # the old "attribution_setting" request param was invalid
        # (a response FIELD name; Meta silently ignored it as a param) and is
        # dropped. NOTE: unlike the daily importer, this one deliberately does
        # NOT set use_unified_attribution_setting — its outputs are the
        # validated window docs; changing their value semantics is out
        # of scope.
        "action_attribution_windows": ["7d_click", "1d_view"],
        "limit": 1000,
    }
    if breakdown:
        params["breakdowns"] = ["publisher_platform"]
    if ad_id_filter:
        params["filtering"] = [{"field": "ad.id", "operator": "IN", "value": list(ad_id_filter)}]

    # the ad-set docs carry the full field surface, so this call
    # requests the doc's team's enabled set (the callers pass single-team
    # team_fields on every path, including the shared-account per-team pulls).
    tenant_fields = _window_fields_for_team((team_fields or {}).get("team_esid"))
    try:
        rows = AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
            fields=["adset_id", "adset_name", "campaign_id", "campaign_name", *tenant_fields],
            params=params,
        )
        rows = list(rows)  # materialize so we can count + so the cursor isn't re-consumed
    except Exception as exc:  # noqa: BLE001 — per-account boundary
        logging.warning(
            "window: %s ad-set insights call failed for account %s: %s — skipping.",
            "breakdown" if breakdown else "combined",
            ad_account_id,
            exc,
        )
        return 0

    written = 0
    skipped_nospend = 0
    skipped_other = 0
    for row in rows:
        try:
            doc = reformat_insights(row)
            if not doc:
                skipped_other += 1
                continue
            adset_id = row.get("adset_id")
            if not adset_id:
                skipped_other += 1
                continue
            # reformat_insights prefixes scalars with "insight_"; drop the prefixed
            # identity dups, keep bare fields.
            for k in (
                "insight_publisher_platform",
                "insight_adset_id",
                "insight_adset_name",
                "insight_campaign_id",
                "insight_campaign_name",
            ):
                doc.pop(k, None)

            platform = row.get("publisher_platform") if breakdown else _COMBINED_PLATFORM

            doc["adset_id"] = adset_id
            doc["adset_name"] = row.get("adset_name")
            doc["ad_campaign_id"] = row.get("campaign_id")
            doc["ad_campaign_name"] = row.get("campaign_name")
            doc["ad_account_id"] = strip_ad_account_prefix(str(ad_account_id))
            doc["publisher_platform"] = platform or _COMBINED_PLATFORM
            # grain discriminator. Every ad-set doc is grain="adset"; the
            # team-grain doc (_fetch_and_write_team_account) is grain="team". The read
            # path (dedup_routing grain-map) filters on this so a team-grouped reach
            # table reads the team doc, never the sum of these ad-set docs. Absent
            # grain defaults to "adset" server-side (back-compat for pre-1254 docs).
            doc["grain"] = "adset"

            # Effective Delivery column — the ad set's FB-computed delivery state.
            # Looked up from get_ad_sets (the insights row doesn't carry it).
            # Only set when present so a missing status leaves the field absent
            # (blank cell) rather than writing a null..
            eff_status = effective_status_by_adset.get(str(adset_id))
            if eff_status:
                doc["effective_status"] = eff_status

            # estimated audience size range for the ad set's
            # targeting spec (delivery_estimate edge, fetched once per account).
            # Same only-when-present rule as effective_status: no estimate →
            # fields absent, never 0. Bare (non-insight_) names survive the
            # combined-doc strip below, matching effective_status.
            doc.update(delivery_estimate_by_adset.get(str(adset_id)) or {})

            doc["insight_window_start"] = since
            doc["insight_window_end"] = until
            doc["insight_window_label"] = window_label
            doc["kibana_date"] = since
            doc.update(team_fields)
            doc["@timestamp"] = datetime.now(timezone.utc).isoformat()

            doc = compute_optimization_result(doc)
            _drop_disabled_fields(doc, doc.get("team_esid"))

            # Skip rows that didn't spend (no delivery → no meaningful reach).
            # Evaluated BEFORE the combined-doc strip below, which removes
            # insight_spend from the 'all' doc.
            if float(doc.get("insight_spend") or 0) <= 0:
                skipped_nospend += 1
                continue

            # the combined ('all') doc keeps ONLY non-additive metrics —
            # strip every other insight_* field so the all-platforms table sums
            # additive metrics from the per-platform docs alone (no double-count).
            if not breakdown:
                for key in [k for k in doc if k.startswith("insight_") and k not in _COMBINED_KEEP_INSIGHT_FIELDS]:
                    del doc[key]
                # Optimization-result fields are derived from additive metrics;
                # drop them too so the combined doc carries no additive surface.
                for k in (
                    "result_type",
                    "result_count",
                    "result_label",
                    "cost_per_result",
                    "cost_result_conversations",
                    "cost_result_link_clicks",
                ):
                    doc.pop(k, None)

            file_name = _output_path(
                fb_ad_account_esid,
                team_fields["team_esid"],
                since,
                until,
                adset_id,
                publisher_platform=doc["publisher_platform"],
            )
            save_data_to_file(doc, file_name)
            written += 1
        except Exception as exc:  # noqa: BLE001 — per-row boundary
            skipped_other += 1
            logging.warning(
                "window: dropped %s row adset_id=%s account=%s window=%s error=%r",
                "breakdown" if breakdown else "combined",
                row.get("adset_id"),
                ad_account_id,
                window_label,
                exc,
                exc_info=True,
            )
            continue
    logging.info(
        "window: %s call for account %s — wrote %d, skipped %d no-spend, %d other.",
        "breakdown" if breakdown else "combined",
        ad_account_id,
        written,
        skipped_nospend,
        skipped_other,
    )
    return written


# ── AD-grain deduplicated reach ───────────────────────────────────
#
# An ad-grouped table (the Ad Report — group-by ad_name, or ad sub-rows under an
# ad-set group-by) is FINER than the ad-set grain, so its reach fell back to the
# amber daily-sum upper bound and frequency read low. Meta returns the exact
# per-ad deduped figure the same way it does per ad set: ONE
# ``level=ad`` insights call per account per window. Same breakdown/combined
# pair as the ad-set grain (per-platform docs + a publisher_platform="all"
# cross-platform doc), same ad.id-IN filtering on shared/complement accounts —
# every row is individually attributable (ad -> creative page -> team), so the
# per-team ad_id filter gives exact attribution with no extra resolution step.
#
# Delivery gate: unlike the ad-set grain's legacy spend>0 gate (parity
# frozen), ad docs use the delivery rule (spend OR impressions) — Ads
# Manager reports reach for zero-spend delivering ads and this grain has no
# legacy-doc parity to preserve.


def _ad_output_path(
    fb_ad_account_esid: str,
    team_esid: str,
    since: str,
    until: str,
    ad_id: str,
    publisher_platform: str | None = None,
) -> str:
    """File path for one AD-grain window doc.

    ``__ad_`` marker so it never collides with an ad-set doc (same dir, keyed by
    bare adset_id) or the team/page/demo docs. Idempotent per (ad, window,
    platform)."""
    suffix = f"_{publisher_platform}" if publisher_platform else ""
    return (
        f"/app/data/facebook/ad_insights_window/" f"{fb_ad_account_esid}/{team_esid}/{since}_{until}__ad_{ad_id}{suffix}.json"
    )


def _fetch_and_write_account_ads(
    ad_account_id,
    since,
    until,
    window_label,
    team_fields,
    fb_ad_account_esid,
    *,
    breakdown,
    ad_id_filter=None,
):
    """One AD-grain insights pull for an account + write its docs. Returns docs written.

    Mirrors ``_fetch_and_write_account`` at ``level=ad``:
      ``breakdown=True``  → one doc per (ad, platform);
      ``breakdown=False`` → one doc per ad stamped publisher_platform="all"
      (Meta's cross-platform deduplicated per-ad reach for the window).

    ``ad_id_filter`` — same semantics as the ad-set pull: the shared-account /
    -complement callers pass the team's own ad ids so a team never
    writes another team's ads. At ad grain the filter is exact by construction
    (each row IS one of the filtered ads).

    No effective_status / delivery_estimate stamping: those are ad-set-object
    attributes; the Ad Report's status columns read the daily docs.
    """
    check_and_pause_for_account(ad_account_id)
    params = {
        "time_range": {"since": since, "until": until},
        "level": "ad",
        # note applies here too: window docs keep default value
        # semantics; only the explicit attribution windows are requested.
        "action_attribution_windows": ["7d_click", "1d_view"],
        "limit": 1000,
    }
    if breakdown:
        params["breakdowns"] = ["publisher_platform"]
    if ad_id_filter:
        params["filtering"] = [{"field": "ad.id", "operator": "IN", "value": list(ad_id_filter)}]

    tenant_fields = _window_fields_for_team((team_fields or {}).get("team_esid"))
    try:
        rows = AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
            fields=["ad_id", "ad_name", "adset_id", "adset_name", "campaign_id", "campaign_name", *tenant_fields],
            params=params,
        )
        rows = list(rows)
    except Exception as exc:  # noqa: BLE001 — per-account boundary
        logging.warning(
            "window: %s ad-grain insights call failed for account %s: %s — skipping.",
            "breakdown" if breakdown else "combined",
            ad_account_id,
            exc,
        )
        return 0

    written = 0
    skipped_nodelivery = 0
    skipped_other = 0
    for row in rows:
        try:
            doc = reformat_insights(row)
            if not doc:
                skipped_other += 1
                continue
            ad_id = row.get("ad_id")
            if not ad_id:
                skipped_other += 1
                continue
            for k in (
                "insight_publisher_platform",
                "insight_ad_id",
                "insight_ad_name",
                "insight_adset_id",
                "insight_adset_name",
                "insight_campaign_id",
                "insight_campaign_name",
            ):
                doc.pop(k, None)

            platform = row.get("publisher_platform") if breakdown else _COMBINED_PLATFORM

            doc["ad_id"] = ad_id
            doc["ad_name"] = row.get("ad_name")
            doc["adset_id"] = row.get("adset_id")
            doc["adset_name"] = row.get("adset_name")
            doc["ad_campaign_id"] = row.get("campaign_id")
            doc["ad_campaign_name"] = row.get("campaign_name")
            doc["ad_account_id"] = strip_ad_account_prefix(str(ad_account_id))
            doc["publisher_platform"] = platform or _COMBINED_PLATFORM
            # grain discriminator; the read path's grain-map routes an
            # ad_id/ad_name group-by (or an ad sub-row under an ad-set group-by)
            # to these docs. Always stamped, so the router's eq filter is safe.
            doc["grain"] = "ad"

            doc["insight_window_start"] = since
            doc["insight_window_end"] = until
            doc["insight_window_label"] = window_label
            doc["kibana_date"] = since
            doc.update(team_fields)
            doc["@timestamp"] = datetime.now(timezone.utc).isoformat()

            doc = compute_optimization_result(doc)
            _drop_disabled_fields(doc, doc.get("team_esid"))

            # delivery gate (spend OR impressions) — evaluated BEFORE
            # the combined-doc strip below removes the additive fields.
            if not _daily._ad_day_had_delivery(doc.get("insight_spend"), doc.get("insight_impressions")):
                skipped_nodelivery += 1
                continue

            # Same combined-doc rule as the ad-set grain: the 'all' doc keeps
            # ONLY non-additive metrics so an all-platforms sum reads the
            # per-platform docs alone (no double-count).
            if not breakdown:
                for key in [k for k in doc if k.startswith("insight_") and k not in _COMBINED_KEEP_INSIGHT_FIELDS]:
                    del doc[key]
                for k in (
                    "result_type",
                    "result_count",
                    "result_label",
                    "cost_per_result",
                    "cost_result_conversations",
                    "cost_result_link_clicks",
                ):
                    doc.pop(k, None)

            file_name = _ad_output_path(
                fb_ad_account_esid,
                team_fields["team_esid"],
                since,
                until,
                ad_id,
                publisher_platform=doc["publisher_platform"],
            )
            save_data_to_file(doc, file_name)
            written += 1
        except Exception as exc:  # noqa: BLE001 — per-row boundary
            skipped_other += 1
            logging.warning(
                "window: dropped %s ad row ad_id=%s account=%s window=%s error=%r",
                "breakdown" if breakdown else "combined",
                row.get("ad_id"),
                ad_account_id,
                window_label,
                exc,
                exc_info=True,
            )
            continue
    logging.info(
        "window: %s ad-grain call for account %s — wrote %d, skipped %d no-delivery, %d other.",
        "breakdown" if breakdown else "combined",
        ad_account_id,
        written,
        skipped_nodelivery,
        skipped_other,
    )
    return written


# ── TEAM-grain deduplicated reach ─────────────────────────────────
#
# A team-grouped reach table summing the per-ad-set docs above double-counts a
# person reached by 2+ of the team's ad sets (the "upper bound" case).
# Meta CAN return the exact cross-ad-set deduped total via ONE ``level=account``
# call filtered to the team's ads (verified: one team = 68,946, Data Studio match).
# We pull that per account the team reaches, SUM across the team's accounts (Meta
# can't dedup across accounts — that residual is labeled, not hidden), and write
# ONE ``grain="team"`` doc per team + window. It coexists with the ad-set docs;
# the read-path grain-map (Phase B) routes a team group-by to it.

# Team-grain doc keeps ONLY the non-additive reach metrics (level=account has no
# ad set / platform breakdown) plus the identity/window/team fields. Mirrors the
# combined doc's keep-set intent: a team reach doc is a reach doc, nothing else.
_TEAM_GRAIN_KEEP_INSIGHT_FIELDS = frozenset(
    {
        "insight_reach",
        "insight_frequency",
        "insight_cpp",
        "insight_estimated_ad_recallers",
        "insight_estimated_ad_recall_rate",
        "insight_full_view_reach",
        "insight_account_currency",
    }
)


def _team_output_path(team_esid: str, since: str, until: str) -> str:
    """File path for a team's ONE team-grain window doc.

    Distinct from ``_output_path`` (no adset_id, a ``__team`` marker) so it never
    collides with an ad-set doc. Keyed by team + window only — a team spans N
    accounts but produces ONE cross-account-summed doc. Idempotent per window."""
    return f"/app/data/facebook/ad_insights_window/{team_esid}/{since}_{until}__team.json"


def _fetch_team_account_reach(ad_account_id, since, until, ad_ids) -> dict | None:
    """ONE ``level=account`` + ``ad.id IN ad_ids`` insights pull → the account-level
    deduplicated reach for THIS team's ads on THIS account.

    Cross-platform combined (no publisher_platform breakdown) so the reach is
    Meta's single deduplicated figure for the window — the same call that matched
    Data Studio for one team. Returns the reformatted insight dict (reach/frequency/
    recall + spend) or ``None`` when the account returns no delivering row.

    ``ad_ids`` semantics:
      * ``None`` — no filter: the account-level reach over ALL its ads (the
        owning-team path, where the team owns the whole account).
      * a NON-EMPTY list — ``ad.id IN ad_ids`` scopes the reach to the team's ads
        (the shared-account path, page->ad resolved).
      * an EMPTY list — the team has no ads on this account this window; skip.
    """
    if ad_ids is not None and not ad_ids:
        return None
    check_and_pause_for_account(ad_account_id)
    params = {
        "time_range": {"since": since, "until": until},
        "level": "account",
        # invalid "attribution_setting" param dropped; see the
        # _fetch_and_write_account note (window docs keep default semantics).
        "action_attribution_windows": ["7d_click", "1d_view"],
        "limit": 1000,
    }
    if ad_ids is not None:
        params["filtering"] = [{"field": "ad.id", "operator": "IN", "value": list(ad_ids)}]
    try:
        rows = list(
            AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
                fields=["account_id", *_INSIGHT_FIELDS], params=params
            )
        )
    except Exception as exc:  # noqa: BLE001 — per-account boundary
        logging.warning(
            "team-grain level=account reach call failed for account %s: %s — skipping.",
            ad_account_id,
            exc,
        )
        return None
    if not rows:
        return None
    doc = reformat_insights(rows[0])
    return doc or None


def _accumulate_team_reach(accumulator, team_esid, team_fields, account_reach):
    """Add ONE account's deduped reach to a team's running cross-account total.

    ``accumulator`` is ``{team_esid: {...}}``. Meta can't dedup reach across
    accounts, so the honest cross-account figure is the SUM of the per-account
    deduped totals — an upper bound the read path labels when accounts > 1. Reach /
    full_view_reach / estimated_ad_recallers are additive-across-accounts counts;
    we sum them. Frequency/cpp/recall_rate are per-account ratios that don't sum
    meaningfully across accounts, so we DON'T carry them for a multi-account team
    (a single-account team keeps them — the exact per-account values)."""
    if not account_reach:
        return
    entry = accumulator.get(team_esid)
    if entry is None:
        entry = {
            "team_fields": team_fields,
            "accounts": 0,
            "insight_reach": 0.0,
            "insight_full_view_reach": 0.0,
            "insight_estimated_ad_recallers": 0.0,
            # First-account passthrough for per-account ratios (kept only if the
            # team stays single-account; dropped in the flush when accounts > 1).
            "single_account_extras": {},
        }
        accumulator[team_esid] = entry
    entry["accounts"] += 1
    for k in ("insight_reach", "insight_full_view_reach", "insight_estimated_ad_recallers"):
        entry[k] += float(account_reach.get(k) or 0)
    if entry["accounts"] == 1:
        entry["single_account_extras"] = {
            k: account_reach.get(k)
            for k in ("insight_frequency", "insight_cpp", "insight_estimated_ad_recall_rate", "insight_account_currency")
            if account_reach.get(k) is not None
        }


def _flush_team_grain_docs(accumulator, since, until, window_label) -> int:
    """Write ONE grain="team" doc per accumulated team. Returns docs written.

    single-account team → exact deduped reach + the per-account ratios, no
    cross-account flag. multi-account team → summed reach across accounts,
    team_reach_cross_account=true, ratios dropped (they don't sum). The read path
    reads this ONE doc with ``max`` (== its own value) and labels the multi-account
    case."""
    written = 0
    for team_esid, entry in accumulator.items():
        n_accounts = entry["accounts"]
        if n_accounts == 0 or float(entry.get("insight_reach") or 0) <= 0:
            continue
        doc = {
            "insight_reach": int(round(entry["insight_reach"])),
            "grain": "team",
            "team_reach_accounts": n_accounts,
            "team_reach_cross_account": n_accounts > 1,
            "publisher_platform": _COMBINED_PLATFORM,
            "insight_window_start": since,
            "insight_window_end": until,
            "insight_window_label": window_label,
            "kibana_date": since,
            "@timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if entry["insight_full_view_reach"] > 0:
            doc["insight_full_view_reach"] = int(round(entry["insight_full_view_reach"]))
        if entry["insight_estimated_ad_recallers"] > 0:
            doc["insight_estimated_ad_recallers"] = entry["insight_estimated_ad_recallers"]
        # Per-account ratios survive ONLY for a single-account team (exact); a
        # multi-account team can't carry a meaningful blended ratio.
        if n_accounts == 1:
            for k, v in (entry.get("single_account_extras") or {}).items():
                if k in _TEAM_GRAIN_KEEP_INSIGHT_FIELDS:
                    doc[k] = v
        doc.update(entry["team_fields"])
        _drop_disabled_fields(doc, team_esid)
        save_data_to_file(doc, _team_output_path(team_esid, since, until))
        written += 1
        logging.info(
            "team-grain reach doc for team %s — %d (%s across %d account(s)).",
            team_esid,
            doc["insight_reach"],
            "exact deduped" if n_accounts == 1 else "SUMMED (Meta can't dedup across accounts)",
            n_accounts,
        )
    return written


# ── PAGE-grain deduplicated reach ─────────────────────────────────
#
# A Team × Page table (page_id / page_name sub-row) renders a (team, page) slice
# finer than the ad-set grain — so falls back to the amber daily-sum
# upper bound. Meta CAN return the exact deduped figure for a page: the same
# ``level=account`` + ``ad.id IN <ads>`` call the team grain uses,
# with the page's ads (creative actor_id) instead of the team's. ONE
# ``grain="page"`` doc per (team, page) per window, accumulated across accounts
# exactly like the team grain (Meta can't dedup cross-account; the read path
# labels accounts > 1). Decision + alternatives: the private docs 2026-07-12.


def _page_output_path(team_esid: str, page_id: str, since: str, until: str) -> str:
    """File path for one (team, page) page-grain window doc.

    Mirrors ``_team_output_path`` — a ``__page_`` marker so it never collides
    with an ad-set or team doc. Idempotent per (team, page, window)."""
    return f"/app/data/facebook/ad_insights_window/{team_esid}/{since}_{until}__page_{page_id}.json"


def _pages_of_ads(page_by_ad: dict, ad_ids: list | None = None) -> dict[str, list[str]]:
    """Group ad ids by their promoted page: ``{page_id: [ad_id, ...]}``.

    ``ad_ids=None`` groups every ad in the map; a list restricts to those ads.
    Ads with no resolvable page (pid None) are dropped — they contribute to team
    reach but belong to no page row (the table's "(none)" bucket keeps its
    daily-sum treatment)."""
    scope = page_by_ad if ad_ids is None else {a: page_by_ad[a] for a in ad_ids if a in page_by_ad}
    pages: dict[str, list[str]] = {}
    for aid, pid in scope.items():
        if pid is not None:
            pages.setdefault(str(pid), []).append(aid)
    return pages


def _accumulate_page_reach(
    accumulator: dict, team_esid: str, team_fields: dict, page_id: str, account_reach: dict | None
) -> None:
    """Add ONE account's deduped page reach to the (team, page) running total.

    Same additive-across-accounts rules as ``_accumulate_team_reach``: reach
    counts sum; per-account ratios survive only while the pair stays
    single-account."""
    if not account_reach:
        return
    key = (team_esid, str(page_id))
    entry = accumulator.get(key)
    if entry is None:
        entry = {
            "team_fields": team_fields,
            "accounts": 0,
            "insight_reach": 0.0,
            "insight_full_view_reach": 0.0,
            "insight_estimated_ad_recallers": 0.0,
            "single_account_extras": {},
        }
        accumulator[key] = entry
    entry["accounts"] += 1
    for k in ("insight_reach", "insight_full_view_reach", "insight_estimated_ad_recallers"):
        entry[k] += float(account_reach.get(k) or 0)
    if entry["accounts"] == 1:
        entry["single_account_extras"] = {
            k: account_reach.get(k)
            for k in ("insight_frequency", "insight_cpp", "insight_estimated_ad_recall_rate", "insight_account_currency")
            if account_reach.get(k) is not None
        }


def _pull_page_reach(
    ad_account_id,
    since: str,
    until: str,
    pages: dict[str, list[str]],
    team_esid: str,
    team_fields: dict,
    page_reach_acc: dict | None,
    team_account_reach: dict | None = None,
    team_ad_ids: list | None = None,
) -> None:
    """Per page with delivery on THIS account: accumulate one exact deduped
    reach figure into the (team, page) accumulator.

    Reuse before refetch: when the team's ads on this account all belong to ONE
    page, the team's own level=account figure (already fetched for the
    team grain) IS that page's reach — no second Meta call. Otherwise one
    ``level=account`` + ``ad.id IN <page ads>`` call per page. A failed call
    returns None and simply contributes nothing (fail-open: a missing page doc
    falls back to the amber daily-sum, never a wrong figure)."""
    if not pages or page_reach_acc is None:
        return
    if (
        team_account_reach is not None
        and team_ad_ids is not None
        and len(pages) == 1
        and set(next(iter(pages.values()))) == set(team_ad_ids)
    ):
        pid = next(iter(pages))
        _accumulate_page_reach(page_reach_acc, team_esid, team_fields, pid, team_account_reach)
        return
    for pid, ads in pages.items():
        reach = _fetch_team_account_reach(ad_account_id, since, until, ad_ids=ads)
        _accumulate_page_reach(page_reach_acc, team_esid, team_fields, pid, reach)


def _page_name_map() -> dict[str, str]:
    """``{fb_page_id: fb_page_name}`` from the de_asset_fb_pages inventory.

    Pages outside the inventory (wizard cohort) get no page_name
    on their doc; page_id stays the reliable grouping key."""
    try:
        return {
            str(p["_source"].get("fb_page_id")): p["_source"].get("fb_page_name")
            for p in (get_fb_pages() or [])
            if p["_source"].get("fb_page_id") is not None and p["_source"].get("fb_page_name")
        }
    except Exception as exc:  # noqa: BLE001 — names are cosmetic; ids always land
        logging.warning("fb_pages name lookup failed: %s — page docs carry page_id only.", exc)
        return {}


def _flush_page_grain_docs(accumulator: dict, since: str, until: str, window_label: str, page_name_by_id: dict) -> int:
    """Write ONE grain="page" doc per accumulated (team, page). Returns docs written.

    Mirrors ``_flush_team_grain_docs``: single-account pair → exact deduped reach
    + per-account ratios; multi-account pair → summed reach with
    ``page_reach_cross_account=true`` (ratios dropped — they don't sum)."""
    written = 0
    for (team_esid, page_id), entry in accumulator.items():
        n_accounts = entry["accounts"]
        if n_accounts == 0 or float(entry.get("insight_reach") or 0) <= 0:
            continue
        doc = {
            "insight_reach": int(round(entry["insight_reach"])),
            "grain": "page",
            "page_id": page_id,
            "page_reach_accounts": n_accounts,
            "page_reach_cross_account": n_accounts > 1,
            "publisher_platform": _COMBINED_PLATFORM,
            "insight_window_start": since,
            "insight_window_end": until,
            "insight_window_label": window_label,
            "kibana_date": since,
            "@timestamp": datetime.now(timezone.utc).isoformat(),
        }
        page_name = page_name_by_id.get(page_id)
        if page_name:
            doc["page_name"] = page_name
        if entry["insight_full_view_reach"] > 0:
            doc["insight_full_view_reach"] = int(round(entry["insight_full_view_reach"]))
        if entry["insight_estimated_ad_recallers"] > 0:
            doc["insight_estimated_ad_recallers"] = entry["insight_estimated_ad_recallers"]
        if n_accounts == 1:
            for k, v in (entry.get("single_account_extras") or {}).items():
                if k in _TEAM_GRAIN_KEEP_INSIGHT_FIELDS:
                    doc[k] = v
        doc.update(entry["team_fields"])
        _drop_disabled_fields(doc, team_esid)
        save_data_to_file(doc, _page_output_path(team_esid, page_id, since, until))
        written += 1
    if written:
        logging.info("wrote %d page-grain reach doc(s) for window %s.", written, window_label)
    return written


# ── DEMOGRAPHICS grain (age × gender deduplicated reach) ──────────
#
# The Data Studio Performance Overview report's Demographics tile shows reach /
# impressions per (age bucket × gender). Meta returns that via the SAME
# ``level=account`` + ``ad.id IN <team ads>`` call the team grain uses, with
# ``breakdowns=["age","gender"]`` — one extra call per (team, account), ~21
# cells per team per window (7 age buckets × 3 genders). Deliberately NOT a
# per-ad/day breakdown (the ticket's feared multiplicative row blowup): the
# tile is a team-level window slice, so the team-grain shape is the honest and
# cheap one. ONE ``grain="demographics"`` doc per (team, age, gender) per
# window, accumulated across accounts exactly like the team/page grains.
#
# Cells are DISJOINT (a person has one age bucket and one gender), so summing
# cells within an account is exact — the flush also writes the two marginals
# (per-age with gender="all", per-gender with age="all") by summing cells, no
# extra Meta call. The "all" sentinel follows the publisher_platform="all"
# convention: an age-only group-by's ``max`` agg naturally picks the marginal,
# and an age × gender table term-filters "all" away.

# Metrics carried on a demographics doc. Reach family is non-additive across
# accounts (summed, labeled via demo_reach_cross_account); impressions/spend/
# clicks are additive everywhere and ride along so the tile can show them
# without a second index hit. Safe to carry additive metrics here (unlike the
# combined 'all' platform doc) because the read path only reads
# grain="demographics" docs under a demographics group-by — they never mix
# into an ad-set or team sum.
_DEMO_SUM_FIELDS = (
    "insight_reach",
    "insight_full_view_reach",
    "insight_estimated_ad_recallers",
    "insight_impressions",
    "insight_spend",
    "insight_clicks",
)

_DEMO_ALL = "all"  # age/gender sentinel for a marginal doc (mirrors publisher_platform="all")


def _demo_output_path(team_esid: str, age: str, gender: str, since: str, until: str) -> str:
    """File path for one (team, age, gender) demographics window doc.

    Mirrors ``_team_output_path`` — a ``__demo_`` marker so it never collides
    with an ad-set/team/page doc. Idempotent per (team, age, gender, window)."""
    return f"/app/data/facebook/ad_insights_window/{team_esid}/{since}_{until}__demo_{age}_{gender}.json"


def _fetch_team_demographics(ad_account_id, since, until, ad_ids) -> list[dict]:
    """ONE ``level=account`` + ``breakdowns=["age","gender"]`` insights pull →
    the account-level deduplicated reach per (age, gender) cell for THIS team's
    ads on THIS account.

    ``ad_ids`` semantics are ``_fetch_team_account_reach``'s: None = whole
    account (owning team), non-empty list = ``ad.id IN`` scope (shared account),
    empty list = nothing to pull. Returns [] on failure (fail-open: a missing
    demographics doc just leaves the tile empty, never a wrong figure).
    """
    if ad_ids is not None and not ad_ids:
        return []
    check_and_pause_for_account(ad_account_id)
    params = {
        "time_range": {"since": since, "until": until},
        "level": "account",
        "breakdowns": ["age", "gender"],
        # invalid "attribution_setting" param dropped; see the
        # _fetch_and_write_account note (window docs keep default semantics).
        "action_attribution_windows": ["7d_click", "1d_view"],
        "limit": 1000,
    }
    if ad_ids is not None:
        params["filtering"] = [{"field": "ad.id", "operator": "IN", "value": list(ad_ids)}]
    try:
        rows = list(
            AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
                fields=["account_id", *_INSIGHT_FIELDS], params=params
            )
        )
    except Exception as exc:  # noqa: BLE001 — per-account boundary
        logging.warning(
            "demographics level=account call failed for account %s: %s — skipping.",
            ad_account_id,
            exc,
        )
        return []
    cells = []
    for row in rows:
        age = row.get("age")
        gender = row.get("gender")
        doc = reformat_insights(row)
        if not doc or age is None or gender is None:
            continue
        # reformat_insights prefixes scalars — drop the prefixed breakdown dups,
        # keep the bare dimension values.
        doc.pop("insight_age", None)
        doc.pop("insight_gender", None)
        doc["age"] = str(age)
        doc["gender"] = str(gender)
        cells.append(doc)
    return cells


def _accumulate_demo_reach(accumulator: dict, team_esid: str, team_fields: dict, cells: list[dict]) -> None:
    """Add ONE account's (age, gender) cells to the team's running totals.

    ``accumulator`` is ``{(team_esid, age, gender): entry}``. Reach sums across
    accounts as an upper bound (Meta can't dedup cross-account — same rule as
    the team/page grains); impressions/spend/clicks sum exactly."""
    for cell in cells:
        key = (team_esid, cell["age"], cell["gender"])
        entry = accumulator.get(key)
        if entry is None:
            entry = {"team_fields": team_fields, "accounts": 0}
            entry.update({k: 0.0 for k in _DEMO_SUM_FIELDS})
            accumulator[key] = entry
        entry["accounts"] += 1
        for k in _DEMO_SUM_FIELDS:
            entry[k] += float(cell.get(k) or 0)


def _flush_demo_grain_docs(accumulator: dict, since: str, until: str, window_label: str) -> int:
    """Write ONE grain="demographics" doc per accumulated (team, age, gender)
    cell, plus the two computed marginals per team (per-age gender="all" and
    per-gender age="all", exact sums of the disjoint cells). Returns docs written."""
    # Marginals: cells are disjoint person-sets, so summing them is exact within
    # an account and inherits the cells' cross-account upper-bound semantics.
    marginals: dict[tuple, dict] = {}
    for (team_esid, age, gender), entry in accumulator.items():
        for mkey in ((team_esid, age, _DEMO_ALL), (team_esid, _DEMO_ALL, gender)):
            m = marginals.get(mkey)
            if m is None:
                m = {"team_fields": entry["team_fields"], "accounts": 0}
                m.update({k: 0.0 for k in _DEMO_SUM_FIELDS})
                marginals[mkey] = m
            m["accounts"] = max(m["accounts"], entry["accounts"])
            for k in _DEMO_SUM_FIELDS:
                m[k] += entry[k]

    written = 0
    for (team_esid, age, gender), entry in {**accumulator, **marginals}.items():
        if float(entry.get("insight_reach") or 0) <= 0 and float(entry.get("insight_impressions") or 0) <= 0:
            continue
        n_accounts = entry["accounts"]
        doc = {
            "grain": "demographics",
            "age": age,
            "gender": gender,
            "demo_reach_accounts": n_accounts,
            "demo_reach_cross_account": n_accounts > 1,
            "publisher_platform": _COMBINED_PLATFORM,
            "insight_window_start": since,
            "insight_window_end": until,
            "insight_window_label": window_label,
            "kibana_date": since,
            "@timestamp": datetime.now(timezone.utc).isoformat(),
        }
        for k in _DEMO_SUM_FIELDS:
            if entry[k] > 0:
                doc[k] = round(entry[k], 2) if k == "insight_spend" else int(round(entry[k]))
        doc.update(entry["team_fields"])
        _drop_disabled_fields(doc, team_esid)
        save_data_to_file(doc, _demo_output_path(team_esid, age, gender, since, until))
        written += 1
    if written:
        logging.info("wrote %d demographics-grain doc(s) for window %s.", written, window_label)
    return written


# ── ASSET grain (per-video-asset deduplicated reach) ──────────────
#
# Ads Manager's media-asset view splits a dynamic-creative ad's delivery per
# VIDEO ASSET — the layout of a teammate's 2026-08-20 screenshot, whose per-asset
# reach figures a staging probe reproduced 11/12 to the exact digit via
# ``level=adset + breakdowns=["video_asset"]``. A WINDOW-ONLY grain (the
# demographics pattern): asset identity exists on no daily
# doc, so an asset-dimension table routes wholly to the window index.
#
# Two pulls per (team, account) per window:
#   * CELLS  — ``level=adset + breakdowns=["video_asset"]``: one doc per
#     (ad set, video asset), written directly (an ad set lives in exactly one
#     account, so cells never need cross-account accumulation). Cells carry the
#     full metric surface + compute_optimization_result, so the Ads-Manager
#     columns (results / cost per result / spend) render per asset.
#   * MARGINALS — ``level=campaign + breakdowns=["video_asset"]``: Meta's
#     exact per-asset reach deduped across each CAMPAIGN's ad sets (the
#     breakdown silently returns zero rows at level=account — staging probe
#     2026-08-20), summed across campaigns/accounts with the slice count in
#     asset_reach_accounts (>1 = labeled upper bound). Stamped with the
#     ``adset_id="all"`` sentinel so an asset-only group-by's ``max`` reads the
# marginal while an (ad set × asset) table filters it away.
#
# ``video_asset_id`` stores the VIDEO id (the object's ``video_id`` — what Ads
# Manager displays and the same value space as the daily docs'
# ``creative_video_id``), not the ephemeral asset-container id. thumbnail_url /
# url are NOT stored (signed, expiring query params).

_ASSET_ALL = "all"  # adset sentinel for a per-asset marginal doc

_ASSET_MARGINAL_SUM_FIELDS = _DEMO_SUM_FIELDS  # same additive/upper-bound rules


def _video_assets_enabled_for_team(team_esid):
    """pattern — whether this team's workspace wants the video-asset
    pull. Default (no override / unresolvable team / store failure) is True."""
    override = (_daily._overrides_for_team(team_esid) or {}).get("fb_ads.breakdown_video_assets")
    return override is None or bool(override.get("enabled"))


def _asset_output_path(fb_ad_account_esid: str, team_esid: str, since: str, until: str, adset_id: str, video_id: str) -> str:
    """File path for one (ad set, video asset) CELL doc. ``__asset_`` marker so
    it never collides with the ad-set / ad / team / page / demo docs."""
    return (
        f"/app/data/facebook/ad_insights_window/"
        f"{fb_ad_account_esid}/{team_esid}/{since}_{until}__asset_{adset_id}_{video_id}.json"
    )


def _asset_marginal_output_path(team_esid: str, video_id: str, since: str, until: str) -> str:
    """File path for one per-asset MARGINAL doc (team-level dir, like the team/
    demo docs — a marginal spans the team's accounts)."""
    return f"/app/data/facebook/ad_insights_window/{team_esid}/{since}_{until}__assetm_{video_id}.json"


def _asset_identity(row) -> tuple[str, str] | None:
    """(video_id, video_name) from a breakdown row's ``video_asset`` object, or
    None when the row carries no resolvable video identity."""
    asset = row.get("video_asset") if hasattr(row, "get") else None
    if hasattr(asset, "export_all_data"):
        asset = asset.export_all_data()
    if not isinstance(asset, dict):
        return None
    video_id = asset.get("video_id") or asset.get("id")
    if video_id is None:
        return None
    return str(video_id), str(asset.get("video_name") or "")


def _fetch_and_write_account_assets(
    ad_account_id,
    since,
    until,
    window_label,
    team_fields,
    fb_ad_account_esid,
    asset_marg_acc,
    *,
    ad_id_filter=None,
):
    """Both asset pulls for one (team, account): write the (ad set × asset)
    CELL docs, accumulate the per-asset MARGINALS. Returns cell docs written.

    ``ad_id_filter`` semantics match every other grain (None = whole account;
    non-empty = the team's own ads on a shared/complement account; empty = skip).
    """
    if ad_id_filter is not None and not ad_id_filter:
        return 0
    tenant_fields = _window_fields_for_team((team_fields or {}).get("team_esid"))
    base_params = {
        "time_range": {"since": since, "until": until},
        "breakdowns": ["video_asset"],
        # note applies: window docs keep default value semantics.
        "action_attribution_windows": ["7d_click", "1d_view"],
        "limit": 1000,
    }
    if ad_id_filter:
        base_params["filtering"] = [{"field": "ad.id", "operator": "IN", "value": list(ad_id_filter)}]

    # CELLS — level=adset × video_asset. optimization_goal is requested
    # explicitly: compute_optimization_result keys the Results / cost-per
    # columns on it, and unlike the Ad Sets table (whose additive columns come
    # from the daily index) an asset table is window-only — the cell doc is the
    # ONLY place its results can come from.
    check_and_pause_for_account(ad_account_id)
    try:
        cell_rows = list(
            AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
                fields=["adset_id", "adset_name", "campaign_id", "campaign_name", "optimization_goal", *tenant_fields],
                params={**base_params, "level": "adset"},
            )
        )
    except Exception as exc:  # noqa: BLE001 — per-account boundary
        logging.warning("asset CELL call failed for account %s: %s — skipping.", ad_account_id, exc)
        cell_rows = []

    written = 0
    skipped = 0
    for row in cell_rows:
        try:
            identity = _asset_identity(row)
            adset_id = row.get("adset_id")
            doc = reformat_insights(row)
            if not doc or identity is None or not adset_id:
                skipped += 1
                continue
            video_id, video_name = identity
            for k in ("insight_adset_id", "insight_adset_name", "insight_campaign_id", "insight_campaign_name"):
                doc.pop(k, None)
            doc["adset_id"] = adset_id
            doc["adset_name"] = row.get("adset_name")
            doc["ad_campaign_id"] = row.get("campaign_id")
            doc["ad_campaign_name"] = row.get("campaign_name")
            doc["ad_account_id"] = strip_ad_account_prefix(str(ad_account_id))
            doc["video_asset_id"] = video_id
            if video_name:
                doc["video_asset_name"] = video_name
            doc["grain"] = "asset"
            doc["publisher_platform"] = _COMBINED_PLATFORM
            doc["insight_window_start"] = since
            doc["insight_window_end"] = until
            doc["insight_window_label"] = window_label
            doc["kibana_date"] = since
            doc.update(team_fields)
            doc["@timestamp"] = datetime.now(timezone.utc).isoformat()
            doc = compute_optimization_result(doc)
            _drop_disabled_fields(doc, doc.get("team_esid"))
            # delivery gate (spend OR impressions), like the ad grain.
            if not _daily._ad_day_had_delivery(doc.get("insight_spend"), doc.get("insight_impressions")):
                skipped += 1
                continue
            save_data_to_file(
                doc,
                _asset_output_path(fb_ad_account_esid, team_fields["team_esid"], since, until, adset_id, video_id),
            )
            written += 1
        except Exception as exc:  # noqa: BLE001 — per-row boundary
            skipped += 1
            logging.warning(
                "dropped asset cell row adset=%s account=%s window=%s error=%r",
                row.get("adset_id"),
                ad_account_id,
                window_label,
                exc,
                exc_info=True,
            )
    logging.info("asset CELL call for account %s — wrote %d, skipped %d.", ad_account_id, written, skipped)

    # MARGINALS — level=campaign × video_asset. Staging probe (2026-08-20):
    # the video_asset breakdown returns ZERO rows at level=account (silently
    # unsupported) but works at level=campaign, so the finest whole-slice dedup
    # Meta offers per asset is the (campaign, asset) figure. Each row is one
    # summed SLICE in the accumulator; a (team, asset) pair spanning 2+ slices
    # (multiple campaigns and/or accounts) flushes as the labeled upper bound —
    # the same rule as every cross-account sum in this importer.
    if asset_marg_acc is None:
        return written
    check_and_pause_for_account(ad_account_id)
    try:
        marg_rows = list(
            AdAccount(normalize_ad_account_id(ad_account_id)).get_insights(
                fields=["campaign_id", *tenant_fields],
                params={**base_params, "level": "campaign"},
            )
        )
    except Exception as exc:  # noqa: BLE001 — per-account boundary; missing marginals fail open
        logging.warning("asset MARGINAL call failed for account %s: %s — skipping.", ad_account_id, exc)
        return written
    team_esid = (team_fields or {}).get("team_esid")
    for row in marg_rows:
        identity = _asset_identity(row)
        doc = reformat_insights(row)
        if not doc or identity is None:
            continue
        if not _daily._ad_day_had_delivery(doc.get("insight_spend"), doc.get("insight_impressions")):
            continue
        video_id, video_name = identity
        doc = compute_optimization_result(doc)
        key = (team_esid, video_id)
        entry = asset_marg_acc.get(key)
        if entry is None:
            entry = {"team_fields": team_fields, "video_asset_name": video_name, "accounts": 0, "result_count": 0.0}
            entry.update({k: 0.0 for k in _ASSET_MARGINAL_SUM_FIELDS})
            entry["result_labels"] = set()
            asset_marg_acc[key] = entry
        entry["accounts"] += 1
        if video_name and not entry.get("video_asset_name"):
            entry["video_asset_name"] = video_name
        for k in _ASSET_MARGINAL_SUM_FIELDS:
            entry[k] += float(doc.get(k) or 0)
        if doc.get("result_count") is not None:
            entry["result_count"] += float(doc.get("result_count") or 0)
            if doc.get("result_label"):
                entry["result_labels"].add(doc["result_label"])
    return written


def _flush_asset_marginal_docs(accumulator: dict, since: str, until: str, window_label: str) -> int:
    """Write ONE grain="asset" MARGINAL doc per accumulated (team, video asset),
    ``adset_id="all"`` sentinel. A pair summed from 2+ slices (campaigns and/or
    accounts — Meta dedups the asset breakdown per campaign at finest) is the
    labeled upper bound (``asset_reach_cross_account=true``), like the
    demo/team grains.
    Results survive only when every contributing account agreed on the result
    label (mixed optimization goals make a summed count meaningless)."""
    written = 0
    for (team_esid, video_id), entry in accumulator.items():
        if float(entry.get("insight_reach") or 0) <= 0 and float(entry.get("insight_impressions") or 0) <= 0:
            continue
        n_accounts = entry["accounts"]
        doc = {
            "grain": "asset",
            "video_asset_id": video_id,
            "adset_id": _ASSET_ALL,
            "adset_name": _ASSET_ALL,
            "asset_reach_accounts": n_accounts,
            "asset_reach_cross_account": n_accounts > 1,
            "publisher_platform": _COMBINED_PLATFORM,
            "insight_window_start": since,
            "insight_window_end": until,
            "insight_window_label": window_label,
            "kibana_date": since,
            "@timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if entry.get("video_asset_name"):
            doc["video_asset_name"] = entry["video_asset_name"]
        for k in _ASSET_MARGINAL_SUM_FIELDS:
            if entry[k] > 0:
                doc[k] = round(entry[k], 2) if k == "insight_spend" else int(round(entry[k]))
        labels = entry.get("result_labels") or set()
        if entry.get("result_count") and len(labels) == 1:
            doc["result_count"] = entry["result_count"]
            doc["result_label"] = next(iter(labels))
            if doc.get("insight_spend"):
                doc["cost_per_result"] = round(float(doc["insight_spend"]) / entry["result_count"], 6)
        doc.update(entry["team_fields"])
        _drop_disabled_fields(doc, team_esid)
        save_data_to_file(doc, _asset_marginal_output_path(team_esid, video_id, since, until))
        written += 1
    if written:
        logging.info("wrote %d asset-grain marginal doc(s) for window %s.", written, window_label)
    return written


def main(params):
    lib.logging.define("fb-get-insights-window")

    # bind this run's progress doc so user-actionable skips (a
    # wizard-cohort connection that is revoked/dangling/wrong-provider, or an
    # account with no resolvable token at all) surface in the Report Scheduler's
    # "Errors" button. Without this, every record_user_error below — including
    # the ones inside _resolve_account_wide_credentials — was a silent no-op on
    # the window path. No-op when the caller passes no --progress_run_id (the
    # full-import tail step; its doc counts days).
    from lib.import_run_context import set_run_id

    set_run_id(params.get("progress_run_id"))

    # Mirror the daily importer's --log_usage hook.
    if params.get("log_usage"):
        _daily._log_usage = True
        setup_usage_logging()
        logging.info("Usage logging enabled — writing to /app/data/logs/fb_usage_YYYYMMDD.log")

    since = params.get("since")
    until = params.get("until")
    if not since or not until:
        raise ValueError("get_insights_window requires both --since and --until (the window bounds).")
    for label, value in (("since", since), ("until", until)):
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError(f"Invalid --{label} format: {value!r}. Expected YYYY-MM-DD.")
    if since > until:
        raise ValueError(f"--since ({since}) must be <= --until ({until}).")

    fb_account_ids = params.get("fb_account_ids", False)
    fb_ad_account_ids = params.get("fb_ad_account_ids", False)
    fb_app_ids = params.get("fb_app_ids", False)
    team_ids = params.get("team_ids", False)

    # Resolve team_ids to upstream ESIDs upfront. strict mode:
    # when --team_ids is passed and the asset chain doesn't resolve traversably,
    # abort rather than fall back to fleet-wide (cross-tenant leak risk). Mirrors
    # the daily importer.
    scope = resolve_team_filter_scope(team_ids) if team_ids else None
    if scope:
        if not scope.get("team_esids"):
            raise ValueError(
                f"strict mode: team_ids={team_ids!r} resolved to ZERO teams. "
                f"Aborting window importer to prevent cross-tenant data leak from a "
                f"fleet-wide fallback. If this is a legitimate call, omit --team_ids."
            )
        # NB: window pull supports BOTH account_wide teams (resolved by ad
        # account) AND page-filtered teams that SOLE-OWN their ad account
        # (resolved by reverse page-ownership walk in _resolve_owning_team).
        # A shared page-filtered account is skipped per-account, not aborted.

    import_params = set_import_params(
        fb_account_ids=fb_account_ids,
        fb_ad_account_ids=fb_ad_account_ids,
        fb_app_ids=fb_app_ids,
        team_ids=team_ids,
        scope=scope,
        # window-aware link selection (ended links overlapping the
        # requested window are pulled too; see the daily importer).
        window_start=since,
        window_end=until,
    )

    scope_team_esids = scope.get("team_esids") if scope else None
    # account_wide teams keyed by ad account (unambiguous-by-design owners).
    ad_account_id_to_team = get_account_wide_teams_by_ad_account_id(
        team_esids=scope_team_esids,
        window_start=since,
        window_end=until,
    )

    # Scoped teams keyed by esid — the candidate owners for the page-filtered
    # sole-owner path. A sole-owning team must be in THIS run's scope to be
    # written (we never write a team the caller didn't ask for).
    scoped_team_by_esid = {}
    if scope_team_esids:
        for t in get_fb_teams(filters={"ds_facebook": "true", "_id": list(scope_team_esids)}):
            src = dict(t["_source"])
            src["_id"] = t["_id"]
            scoped_team_by_esid[t["_id"]] = src

    if not ad_account_id_to_team and not scoped_team_by_esid:
        logging.info(
            "window: no teams in scope (team_ids=%r); nothing to import.",
            team_ids,
        )
        return

    # dedup-only refresh passes the run's progress doc id so the
    # per-account loop can advance the pill (accounts done / accounts total)
    # instead of leaving it at 0% for the whole pull. Absent on the cron /
    # full-import path (whose doc counts days) — no progress writes there.
    progress_run_id = params.get("progress_run_id") or None

    # observed-delivery union. Ground truth comes from ALL FB teams'
    # daily indices (interloper DETECTION must see out-of-scope teams: a scoped
    # owner's overlap is usually caused by a team outside this run's scope);
    # every WRITE below stays gated by scoped_team_by_esid / the aw map. Also
    # synthesizes probe entries for observed accounts the asset walk missed
    # (cross-business delivery — one team on a sibling org's account).
    all_fb_teams = get_fb_teams(filters={"ds_facebook": "true"}) or []
    pages_by_team_esid = {
        t["_id"]: {str(p) for p in (t["_source"].get("team_facebook_page_id") or []) if p is not None} for t in all_fb_teams
    }
    observed_by_account = _observed_teams_by_account(set(pages_by_team_esid), since, until)
    observed_entries = _entries_for_observed_accounts(
        observed_by_account,
        set(scope_team_esids) if scope_team_esids else None,
        import_params,
    )
    if observed_entries:
        import_params = list(import_params) + observed_entries

    total_docs = _import_window(
        since,
        until,
        import_params,
        ad_account_id_to_team,
        scoped_team_by_esid,
        progress_run_id=progress_run_id,
        observed_by_account=observed_by_account,
        pages_by_team_esid=pages_by_team_esid,
    )
    logging.info("Window import complete: %d doc(s) (ad-set + team grain) for %s..%s", total_docs, since, until)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fetch Facebook ad insights — one arbitrary window, ad-set grain")
    parser.add_argument("--since", required=True, help="Window start (YYYY-MM-DD)")
    parser.add_argument("--until", required=True, help="Window end (YYYY-MM-DD)")
    parser.add_argument("--fb_account_ids", default=False)
    parser.add_argument("--fb_ad_account_ids", default=False)
    parser.add_argument("--fb_app_ids", default=False)
    parser.add_argument("--team_ids", default=False)
    parser.add_argument("--log_usage", action="store_true", help="Log FB rate-limit headers per call")
    args = parser.parse_args()
    main(vars(args))

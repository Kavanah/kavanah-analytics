"""
Facebook API helper functions for ID normalization and utilities.
"""


def normalize_ad_account_id(account_id):
    """
    Normalize Facebook ad account ID by ensuring 'act_' prefix for API calls.

    Args:
        account_id (str): The ad account ID (with or without 'act_' prefix)

    Returns:
        str: Account ID with 'act_' prefix for use with Facebook API

    Examples:
        >>> normalize_ad_account_id("123456789")
        'act_123456789'
        >>> normalize_ad_account_id("act_123456789")
        'act_123456789'
    """
    if not account_id:
        return account_id

    account_id = str(account_id).strip()

    if not account_id.startswith("act_"):
        return f"act_{account_id}"

    return account_id


def strip_ad_account_prefix(account_id):
    """
    Remove 'act_' prefix from Facebook ad account ID for storage.

    Args:
        account_id (str): The ad account ID (with or without 'act_' prefix)

    Returns:
        str: Account ID without 'act_' prefix for storage in Elasticsearch

    Examples:
        >>> strip_ad_account_prefix("act_123456789")
        '123456789'
        >>> strip_ad_account_prefix("123456789")
        '123456789'
    """
    if not account_id:
        return account_id

    account_id = str(account_id).strip()

    if account_id.startswith("act_"):
        return account_id[4:]  # Remove 'act_' prefix

    return account_id


def select_team_ad_account_links(team_source, window_start=None, window_end=None):
    """Phase 2 — return the team's ``fb_ad_account_links`` relevant
    to an import window.

    With a window (both bounds, ``YYYY-MM-DD`` strings): every link whose
    effective range OVERLAPS ``[window_start, window_end]``. A missing
    ``start_date`` means "since forever" (seeded links — adoption date
    unknown); a missing ``end_date`` means the link is still open. ISO date
    strings compare correctly as strings, so no parsing is needed.

    Without a window: only OPEN links (no ``end_date``) — the daily-run
    "current accounts" view.

    A backfill over a switched team's old window therefore selects the
    ENDED link that covered it, which is what lets historical ads
    attribute to the team (one team's legacy ad-platform era). Dependency-free
    (like the rest of this module) so both importers and tests share it.
    """
    links = (team_source or {}).get("fb_ad_account_links") or []
    selected = []
    for link in links:
        start = link.get("start_date")
        end = link.get("end_date")
        if window_start and window_end:
            if start and str(start)[:10] > window_end:
                continue
            if end and str(end)[:10] < window_start:
                continue
        elif end:
            continue
        selected.append(link)
    return selected


def account_wide_owner_ok(asset_doc, team_id):
    """True iff ``team_id`` may claim this ad account account-wide.

    The invariant: an account-wide team must be the REGISTERED owner of the ad
    account it imports, i.e. the ``de_asset_fb_ad_accounts`` doc's ``team_esid``
    equals the claiming team's id. Returns True (permissive) when the asset doc
    carries no ``team_esid`` at all — a legacy/unowned doc is not evidence of a
    WRONG owner, and blocking it would regress account-wide teams whose asset
    doc predates the team_esid stamp. Only a positively-mismatched owner is
    rejected. ``asset_doc`` is the ``_source`` dict of the resolved asset doc.

    Defined once here (dependency-free) so the importer read side
    (:func:`import_scripts.facebook.get_insights._account_wide_owner_ok`) and
    the wizard bridge write side share a single definition.
    """
    registered = (asset_doc or {}).get("team_esid")
    if not registered:
        return True
    return registered == team_id

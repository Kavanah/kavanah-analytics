"""One-shot backfill: legacy asset docs → de_connection + de_connection_asset.

Reads existing scattered token storage and creates `connection` +
`connection_asset` rows so the importer cutover (PR-C) has a populated
new representation to read from.

Sources today:

* ``de_asset_fb_app_users`` — encrypted Facebook access token in
  ``fb_app_user_token``. Becomes one ``de_connection`` per app_user
  with ``provider=facebook``.
* ``de_asset_fb_ad_accounts`` — child of ``fb_app_user_esid``. Becomes
  one ``de_connection_asset`` per ad account, ``asset_type=fb_ad_account``.
* ``de_asset_fb_pages`` — child of ``fb_account_esid`` (not app_user).
  A page is reachable via every app_user that lives under the same
  ``fb_account``. The backfill mirrors the importer's lookup chain and
  attaches each page to **every** app_user under its account, so the
  importer cutover doesn't need to resurrect the chain.
* ``de_asset_youtube_accounts`` — encrypted refresh token in
  ``youtube_account_token``. Becomes one ``de_connection`` per account
  with ``provider=youtube``.
* ``de_asset_youtube_channels`` — child of ``youtube_account_esid`` (when
  populated). Becomes one ``de_connection_asset`` per channel,
  ``asset_type=youtube_channel``.

The script is **idempotent**: every write goes through
``ConnectionService.upsert_connection`` / ``upsert_connection_asset``,
which deduplicates on the natural key. Re-running re-encrypts the token
under the same key but never duplicates rows or resets ``created_at``.

The ``principal`` field uses a synthetic ``legacy:<asset_esid>`` value
during PR-B — the real user-account_type wiring lands, at
which point a follow-up will rewrite the principals to point at real
user ids. Carrying the legacy esid lets the eventual rewrite be a
deterministic lookup.

Usage::

    # Dry-run (default) — log what would be written, write nothing.
    python -m bin.python.tools.connection_backfill

    # Apply the writes.
    python -m bin.python.tools.connection_backfill --apply

    # Restrict to one provider.
    python -m bin.python.tools.connection_backfill --apply --provider facebook
    python -m bin.python.tools.connection_backfill --apply --provider youtube
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/feature_flags.py.
_HERE = Path(__file__).resolve().parent
for _candidate in (
    _HERE.parents[1],
    _HERE.parents[2] / "bin" / "python",
    Path("/app/python"),
):
    if (_candidate / "asset_management" / "__init__.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

# lib.* imports come from /app/python (or the repo-local bin/python via
# the same resolver — ConnectionService already inserts /app/python into
# sys.path on import, which works inside imports_container).
from lib.encryption import decrypt  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

from asset_management.web.services.connection_service import (  # noqa: E402
    ASSET_TYPE_FB_AD_ACCOUNT,
    ASSET_TYPE_FB_PAGE,
    ASSET_TYPE_YOUTUBE_CHANNEL,
    PROVIDER_FACEBOOK,
    PROVIDER_YOUTUBE,
    ConnectionService,
)

LEGACY_FB_APP_USERS = "de_asset_fb_app_users"
LEGACY_FB_AD_ACCOUNTS = "de_asset_fb_ad_accounts"
LEGACY_FB_PAGES = "de_asset_fb_pages"
LEGACY_FB_APPS = "de_asset_fb_apps"
LEGACY_YT_ACCOUNTS = "de_asset_youtube_accounts"
LEGACY_YT_CHANNELS = "de_asset_youtube_channels"


def _scan_all(es, index: str) -> list[dict]:
    """Return every doc in an index as ``[{"_id":..., "_source":...}]``.

    The legacy asset indices are small (FB pages is the largest at ~50
    rows on staging), so a single 1000-size match_all is fine — no need
    for the scroll API.
    """
    if not es.indices.exists(index=index):
        return []
    result = es.search(index=index, body={"query": {"match_all": {}}, "size": 1000})
    return list(result.get("hits", {}).get("hits", []))


# ─────────────────────────────────────────────────────────────────────────────
# Facebook backfill
# ─────────────────────────────────────────────────────────────────────────────


def backfill_facebook(es, *, apply: bool) -> dict:
    """Migrate Facebook app_users + ad_accounts + pages."""
    summary: dict[str, int] = defaultdict(int)

    app_users = _scan_all(es, LEGACY_FB_APP_USERS)
    summary["fb_app_users_scanned"] = len(app_users)

    # esid → app_user source, for later FK resolution
    app_user_by_esid: dict[str, dict] = {hit["_id"]: hit["_source"] for hit in app_users}

    # esid → new connection esid (filled as we create connections)
    connection_esid_by_app_user: dict[str, str] = {}

    for hit in app_users:
        app_user_esid = hit["_id"]
        src = hit["_source"]
        encrypted_token = src.get("fb_app_user_token")
        if not encrypted_token:
            summary["fb_app_users_skipped_no_token"] += 1
            logging.warning(
                "FB app_user %s (%s) has no token — skipping",
                app_user_esid,
                src.get("fb_app_user_title"),
            )
            continue
        # Decrypt then re-encrypt through ConnectionService so the new
        # row carries the same key + freshly-stamped ciphertext.
        try:
            plaintext_token = decrypt(encrypted_token)
        except Exception as e:
            summary["fb_app_users_skipped_decrypt_fail"] += 1
            logging.error("FB app_user %s decrypt failed: %s", app_user_esid, e)
            continue

        # provider_account_id: prefer the real FB user id, fall back to
        # the app_user esid so the natural key is always populated.
        provider_account_id = str(src.get("fb_app_user_fbid") or app_user_esid)

        # FB tokens carry an expiry epoch; 0 means "no expiry / unknown".
        expiry_epoch = src.get("fb_app_user_token_exp")
        token_expires_at: str | None = None
        if isinstance(expiry_epoch, (int, float)) and expiry_epoch > 0:
            from datetime import datetime, timezone

            token_expires_at = datetime.fromtimestamp(int(expiry_epoch), tz=timezone.utc).isoformat()

        principal = f"legacy:{app_user_esid}"
        label = src.get("fb_app_user_title") or ""

        if not apply:
            summary["fb_connections_would_create"] += 1
            logging.info(
                "[dry-run] would create FB connection: principal=%s label=%s",
                principal,
                label,
            )
            # Even in dry-run, simulate the FK so the asset-stage log makes sense.
            connection_esid_by_app_user[app_user_esid] = f"<would-create:{app_user_esid}>"
            continue

        result = ConnectionService.upsert_connection(
            principal=principal,
            provider=PROVIDER_FACEBOOK,
            provider_account_id=provider_account_id,
            oauth_token=plaintext_token,
            label=label,
            token_expires_at=token_expires_at,
            legacy_asset_index=LEGACY_FB_APP_USERS,
            legacy_asset_esid=app_user_esid,
        )
        if not result.get("success"):
            summary["fb_connections_errored"] += 1
            logging.error(
                "upsert_connection failed for FB app_user %s: %s",
                app_user_esid,
                result.get("error"),
            )
            continue
        connection_esid_by_app_user[app_user_esid] = result["esid"]
        if result.get("created"):
            summary["fb_connections_created"] += 1
        else:
            summary["fb_connections_refreshed"] += 1

    # ── FB ad accounts: 1:1 child of an app_user ───────────────────────
    ad_accounts = _scan_all(es, LEGACY_FB_AD_ACCOUNTS)
    summary["fb_ad_accounts_scanned"] = len(ad_accounts)

    for hit in ad_accounts:
        src = hit["_source"]
        ad_account_esid = hit["_id"]
        app_user_esid = src.get("fb_app_user_esid")
        if not app_user_esid:
            summary["fb_ad_accounts_skipped_no_app_user"] += 1
            logging.warning(
                "FB ad_account %s (%s) has no fb_app_user_esid — skipping",
                ad_account_esid,
                src.get("fb_ad_account_title"),
            )
            continue
        conn_esid = connection_esid_by_app_user.get(app_user_esid)
        if not conn_esid:
            summary["fb_ad_accounts_skipped_orphan"] += 1
            logging.warning(
                "FB ad_account %s points at app_user %s which has no connection — skipping",
                ad_account_esid,
                app_user_esid,
            )
            continue
        provider_asset_id = src.get("fb_ad_account_id") or ad_account_esid
        label = src.get("fb_ad_account_title") or ""

        if not apply:
            summary["fb_ad_account_assets_would_create"] += 1
            logging.info(
                "[dry-run] would create FB ad_account asset: %s under %s",
                provider_asset_id,
                conn_esid,
            )
            continue
        result = ConnectionService.upsert_connection_asset(
            connection_esid=conn_esid,
            asset_type=ASSET_TYPE_FB_AD_ACCOUNT,
            provider_asset_id=str(provider_asset_id),
            label=label,
            legacy_asset_index=LEGACY_FB_AD_ACCOUNTS,
            legacy_asset_esid=ad_account_esid,
        )
        if not result.get("success"):
            summary["fb_ad_account_assets_errored"] += 1
            logging.error(
                "upsert_connection_asset failed for FB ad_account %s: %s",
                ad_account_esid,
                result.get("error"),
            )
            continue
        if result.get("created"):
            summary["fb_ad_account_assets_created"] += 1
        else:
            summary["fb_ad_account_assets_refreshed"] += 1

    # ── FB pages: child of fb_account, must be attached to every
    #     app_user under that account (mirrors importer chain).
    apps = _scan_all(es, LEGACY_FB_APPS)
    # fb_account_esid → [app_user_esid, ...]
    app_users_by_account: dict[str, list[str]] = defaultdict(list)
    for app_hit in apps:
        app_esid = app_hit["_id"]
        fb_account_esid = app_hit["_source"].get("fb_account_esid")
        if not fb_account_esid:
            continue
        # Find app_users under this app.
        for user_esid, user_src in app_user_by_esid.items():
            if user_src.get("fb_app_esid") == app_esid:
                app_users_by_account[fb_account_esid].append(user_esid)

    pages = _scan_all(es, LEGACY_FB_PAGES)
    summary["fb_pages_scanned"] = len(pages)

    for hit in pages:
        src = hit["_source"]
        page_esid = hit["_id"]
        fb_account_esid = src.get("fb_account_esid")
        if not fb_account_esid:
            summary["fb_pages_skipped_no_account"] += 1
            logging.warning(
                "FB page %s (%s) has no fb_account_esid — skipping",
                page_esid,
                src.get("fb_page_name"),
            )
            continue
        candidate_app_users = app_users_by_account.get(fb_account_esid, [])
        if not candidate_app_users:
            summary["fb_pages_skipped_no_app_users"] += 1
            logging.warning(
                "FB page %s under fb_account %s has no app_users — skipping",
                page_esid,
                fb_account_esid,
            )
            continue
        provider_asset_id = src.get("fb_page_id") or page_esid
        label = src.get("fb_page_name") or src.get("fb_page_slug") or ""

        for app_user_esid in candidate_app_users:
            conn_esid = connection_esid_by_app_user.get(app_user_esid)
            if not conn_esid:
                continue
            if not apply:
                summary["fb_page_assets_would_create"] += 1
                logging.info(
                    "[dry-run] would create FB page asset: %s under %s",
                    provider_asset_id,
                    conn_esid,
                )
                continue
            result = ConnectionService.upsert_connection_asset(
                connection_esid=conn_esid,
                asset_type=ASSET_TYPE_FB_PAGE,
                provider_asset_id=str(provider_asset_id),
                label=label,
                legacy_asset_index=LEGACY_FB_PAGES,
                legacy_asset_esid=page_esid,
            )
            if not result.get("success"):
                summary["fb_page_assets_errored"] += 1
                logging.error(
                    "upsert_connection_asset failed for FB page %s under %s: %s",
                    page_esid,
                    conn_esid,
                    result.get("error"),
                )
                continue
            if result.get("created"):
                summary["fb_page_assets_created"] += 1
            else:
                summary["fb_page_assets_refreshed"] += 1

    return dict(summary)


# ─────────────────────────────────────────────────────────────────────────────
# YouTube backfill
# ─────────────────────────────────────────────────────────────────────────────


def backfill_youtube(es, *, apply: bool) -> dict:
    """Migrate YouTube accounts + channels."""
    summary: dict[str, int] = defaultdict(int)

    accounts = _scan_all(es, LEGACY_YT_ACCOUNTS)
    summary["yt_accounts_scanned"] = len(accounts)

    connection_esid_by_account: dict[str, str] = {}

    for hit in accounts:
        account_esid = hit["_id"]
        src = hit["_source"]
        encrypted_token = src.get("youtube_account_token")
        if not encrypted_token:
            summary["yt_accounts_skipped_no_token"] += 1
            logging.warning(
                "YT account %s (%s) has no token — skipping",
                account_esid,
                src.get("youtube_account_title"),
            )
            continue
        try:
            plaintext_token = decrypt(encrypted_token)
        except Exception as e:
            summary["yt_accounts_skipped_decrypt_fail"] += 1
            logging.error("YT account %s decrypt failed: %s", account_esid, e)
            continue

        provider_account_id = src.get("youtube_account_email") or src.get("youtube_account_title") or account_esid
        principal = f"legacy:{account_esid}"
        label = src.get("youtube_account_title") or ""

        if not apply:
            summary["yt_connections_would_create"] += 1
            logging.info(
                "[dry-run] would create YT connection: principal=%s label=%s",
                principal,
                label,
            )
            connection_esid_by_account[account_esid] = f"<would-create:{account_esid}>"
            continue

        result = ConnectionService.upsert_connection(
            principal=principal,
            provider=PROVIDER_YOUTUBE,
            provider_account_id=str(provider_account_id),
            oauth_token=plaintext_token,
            label=label,
            legacy_asset_index=LEGACY_YT_ACCOUNTS,
            legacy_asset_esid=account_esid,
        )
        if not result.get("success"):
            summary["yt_connections_errored"] += 1
            logging.error(
                "upsert_connection failed for YT account %s: %s",
                account_esid,
                result.get("error"),
            )
            continue
        connection_esid_by_account[account_esid] = result["esid"]
        if result.get("created"):
            summary["yt_connections_created"] += 1
        else:
            summary["yt_connections_refreshed"] += 1

    channels = _scan_all(es, LEGACY_YT_CHANNELS)
    summary["yt_channels_scanned"] = len(channels)

    for hit in channels:
        src = hit["_source"]
        channel_esid = hit["_id"]
        account_esid = src.get("youtube_account_esid")
        if not account_esid:
            summary["yt_channels_skipped_no_account"] += 1
            logging.warning(
                "YT channel %s (%s) has no youtube_account_esid — skipping",
                channel_esid,
                src.get("youtube_channel_title"),
            )
            continue
        conn_esid = connection_esid_by_account.get(account_esid)
        if not conn_esid:
            summary["yt_channels_skipped_orphan"] += 1
            logging.warning(
                "YT channel %s points at account %s which has no connection — skipping",
                channel_esid,
                account_esid,
            )
            continue
        provider_asset_id = src.get("youtube_channel_id") or channel_esid
        label = src.get("youtube_channel_title") or src.get("youtube_channel_handle") or ""

        if not apply:
            summary["yt_channel_assets_would_create"] += 1
            logging.info(
                "[dry-run] would create YT channel asset: %s under %s",
                provider_asset_id,
                conn_esid,
            )
            continue
        result = ConnectionService.upsert_connection_asset(
            connection_esid=conn_esid,
            asset_type=ASSET_TYPE_YOUTUBE_CHANNEL,
            provider_asset_id=str(provider_asset_id),
            label=label,
            legacy_asset_index=LEGACY_YT_CHANNELS,
            legacy_asset_esid=channel_esid,
        )
        if not result.get("success"):
            summary["yt_channel_assets_errored"] += 1
            logging.error(
                "upsert_connection_asset failed for YT channel %s: %s",
                channel_esid,
                result.get("error"),
            )
            continue
        if result.get("created"):
            summary["yt_channel_assets_created"] += 1
        else:
            summary["yt_channel_assets_refreshed"] += 1

    return dict(summary)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="connection_backfill",
        description=("PR-B — backfill legacy FB/YouTube token storage into " "de_connection + de_connection_asset."),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to ES. Without this, runs in dry-run mode and logs what would be done.",
    )
    parser.add_argument(
        "--provider",
        choices=("facebook", "youtube", "all"),
        default="all",
        help="Restrict the backfill to one provider.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Could not connect to Elasticsearch")
        return 1

    # The connection indices must exist before the backfill writes
    # against them. In production the app.py startup already runs this,
    # but the CLI is allowed to bootstrap idempotently so it works on a
    # fresh deploy that hasn't booted the web app yet.
    ConnectionService.ensure_indices_exist()

    mode = "APPLY" if args.apply else "DRY-RUN"
    logging.info("connection_backfill: mode=%s provider=%s", mode, args.provider)

    summary: dict[str, int] = {}
    if args.provider in ("facebook", "all"):
        summary.update(backfill_facebook(es, apply=args.apply))
    if args.provider in ("youtube", "all"):
        summary.update(backfill_youtube(es, apply=args.apply))

    logging.info("=== Backfill summary (%s) ===", mode)
    for k in sorted(summary):
        logging.info("  %s = %d", k, summary[k])

    # Non-zero exit on any errored row so a CI smoke run notices.
    if any(k.endswith("_errored") and v > 0 for k, v in summary.items()):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

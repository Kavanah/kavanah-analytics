"""backfill identity stamps onto legacy YouTube account docs.

The post-callback identity dedup (``_find_youtube_account_by_identity``
in ``fb_oauth_server.py``) matches candidate docs on ``youtube_account_identity``.
Account docs created that were never re-authorized since carry
no identity stamp, so the dedup cannot see them: a same-identity "Add another
account" / re-auth retry mints a twin doc instead of landing the token on the
existing one.

For every tokened ``de_asset_youtube_accounts`` doc missing
``youtube_account_identity``, this tool refreshes the stored token and probes
the same two facts the OAuth callback probes at authorization time:

* ``sub`` — the authorized Google identity id, from the refresh response's
  ``id_token`` when present, else from the tokeninfo endpoint. Legacy grants
  that predate the ``openid`` scope may yield NO sub either way — the tool then
  stamps only the owned-channel set and reports the doc as needing a supervised
  re-auth (an in-place re-auth on a tokened doc stamps the identity via the
  callback, provided the identity passes the ping-pong guard).
* ``owned`` — the channel set the token's identity OWNS via
  ``channels.list(mine=true)``.

Stamped fields mirror ``fb_oauth_server._stamp_youtube_identity`` exactly:
``youtube_account_identity`` (only when sub was recovered),
``youtube_account_owned_channel_ids``, ``youtube_account_owned_channel_titles``,
``youtube_account_identity_checked``. Tokens, team links, and de_connection
rows are never touched.

Usage (inside imports_container)::

    # Dry-run (default) — print the stamp plan, write nothing.
    python -m bin.python.tools.backfill_youtube_identity

    # Limit to one doc.
    python -m bin.python.tools.backfill_youtube_identity --account <account_esid>

    # Apply the writes.
    python -m bin.python.tools.backfill_youtube_identity --apply
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/split_youtube_account.py.
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

from lib.encryption import decrypt  # noqa: E402
from lib.google_oauth_client import load_google_oauth_client  # noqa: E402
from lib.es_helpers import get_elasticsearch_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("backfill_youtube_identity")

ACCOUNTS_INDEX = "de_asset_youtube_accounts"
OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
TOKENINFO_URL = "https://www.googleapis.com/oauth2/v3/tokeninfo"


def needs_backfill(source: dict[str, Any]) -> bool:
    """True for a tokened doc with no identity stamp that isn't a dead
     redirect placeholder (those deliberately stay unstamped)."""
    if not source.get("youtube_account_token"):
        return False
    if str(source.get("youtube_account_redirect_esid") or "").strip():
        return False
    return not str(source.get("youtube_account_identity") or "").strip()


def extract_sub_from_id_token(id_token: str) -> str:
    """``sub`` from an id_token payload, or "" when absent/undecodable.

    Read without signature verification — the token came straight from
    Google's token endpoint over TLS (same posture as
    ``fb_oauth_server._probe_youtube_token_identity``).
    """
    try:
        payload_b64 = id_token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return str(json.loads(base64.urlsafe_b64decode(payload_b64)).get("sub") or "")
    except Exception:  # noqa: BLE001 — malformed/absent token: sub is simply unknown
        return ""


def stamp_identity(source: dict[str, Any], sub: str, owned: list[dict[str, str]], checked_at: str) -> dict[str, Any]:
    """Return a stamped copy of ``source`` — field-for-field the same write as
    ``fb_oauth_server._stamp_youtube_identity`` (identity only when sub was
    recovered; owned ids/titles + checked timestamp always)."""
    stamped = dict(source)
    if sub:
        stamped["youtube_account_identity"] = sub
    stamped["youtube_account_owned_channel_ids"] = [c["id"] for c in owned]
    stamped["youtube_account_owned_channel_titles"] = [c["title"] for c in owned]
    stamped["youtube_account_identity_checked"] = checked_at
    return stamped


def _probe_identity(enc_token: str, client: dict[str, Any]) -> tuple[str, str, list[dict[str, str]] | None, str]:
    """Refresh the stored token and probe (sub, owned) like the callback does.

    Returns ``(sub, sub_source, owned, error)``. ``owned`` is ``None`` when the
    probe could not determine ownership (the doc is then skipped — a partial
    stamp with unknown ownership would be worse than no stamp). ``sub_source``
    is ``"id_token"`` / ``"tokeninfo"`` / ``""`` for reporting.
    """
    try:
        token_resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": decrypt(enc_token),
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
            },
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001 — CLI boundary; per-doc failure is reported, not fatal
        return "", "", None, f"token refresh request failed: {e}"
    data = token_resp.json() if token_resp.content else {}
    access_token = str((data or {}).get("access_token") or "")
    if not access_token:
        return "", "", None, f"token refresh failed (HTTP {token_resp.status_code}): {(token_resp.text or '')[:200]}"

    sub = extract_sub_from_id_token(str((data or {}).get("id_token") or ""))
    sub_source = "id_token" if sub else ""
    if not sub:
        # Legacy grants predate the openid scope, so the refresh response has
        # no id_token; tokeninfo returns sub for some scope sets — worth a try.
        try:
            ti = requests.get(TOKENINFO_URL, params={"access_token": access_token}, timeout=20)
            if ti.status_code == 200:
                sub = str((ti.json() or {}).get("sub") or "")
                sub_source = "tokeninfo" if sub else ""
        except Exception as e:  # noqa: BLE001 — sub stays unknown; owned probe still proceeds
            log.warning("tokeninfo probe failed: %s", e)

    try:
        chan_resp = requests.get(
            CHANNELS_URL,
            params={"part": "id,snippet", "mine": "true", "maxResults": 50},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except Exception as e:  # noqa: BLE001 — ownership unknown -> skip doc
        return sub, sub_source, None, f"owned-channel probe failed: {e}"
    if chan_resp.status_code == 200:
        items = (chan_resp.json() or {}).get("items") or []
        owned = [{"id": str(i.get("id")), "title": (i.get("snippet") or {}).get("title") or ""} for i in items if i.get("id")]
        return sub, sub_source, owned, ""
    if chan_resp.status_code == 401 and "youtubeSignupRequired" in (chan_resp.text or ""):
        # The identity owns no channel at all (the manager-not-owner
        # case) — a valid, stampable outcome.
        return sub, sub_source, [], ""
    return sub, sub_source, None, f"channels.list HTTP {chan_resp.status_code}: {(chan_resp.text or '')[:200]}"


def backfill(account_esid: str | None, apply: bool) -> int:
    es = get_elasticsearch_client()
    if es is None:
        log.error("no Elasticsearch client")
        return 1

    if account_esid:
        hits = [es.get(index=ACCOUNTS_INDEX, id=account_esid)]
    else:
        result = es.search(index=ACCOUNTS_INDEX, body={"query": {"match_all": {}}, "size": 500})
        hits = result.get("hits", {}).get("hits", [])

    try:
        client = load_google_oauth_client()  # env-var first, on-disk fallback
    except Exception as e:  # noqa: BLE001 — CLI boundary; nothing works without the client
        log.error("could not load OAuth client config: %s", e)
        return 1

    checked_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stamped = skipped = failed = need_reauth = 0
    for hit in hits:
        doc_id = hit.get("_id") or ""
        source = hit.get("_source") or {}
        title = source.get("youtube_account_title") or doc_id
        if not needs_backfill(source):
            log.info("SKIP   %-40s (%s) — no token, already stamped, or redirect placeholder", title, doc_id)
            skipped += 1
            continue

        sub, sub_source, owned, error = _probe_identity(str(source.get("youtube_account_token")), client)
        if owned is None:
            log.warning("FAIL   %-40s (%s) — %s", title, doc_id, error)
            failed += 1
            continue

        if not owned:
            log.warning(
                "NOTE   %-40s (%s) — identity owns NO channels; its token cannot read any "
                "channel's analytics (manager-not-owner grant?)",
                title,
                doc_id,
            )
        if not sub:
            # Owned channels are still worth stamping (ping-pong evidence),
            # but the doc stays invisible to the sub-keyed dedup until the
            # owner re-authorizes it in place (the callback stamps sub then).
            need_reauth += 1

        owned_desc = ", ".join(f"{c['title']} ({c['id']})" for c in owned) or "-"
        log.info(
            "STAMP  %-40s (%s) sub=%s%s owned=[%s]%s",
            title,
            doc_id,
            sub or "UNRECOVERABLE",
            f" via {sub_source}" if sub_source else "",
            owned_desc,
            "" if apply else " [dry-run]",
        )
        if apply:
            es.index(index=ACCOUNTS_INDEX, id=doc_id, document=stamp_identity(source, sub, owned, checked_at))
        stamped += 1

    log.info(
        "%s: %d stamped, %d skipped, %d failed%s",
        "APPLIED" if apply else "DRY-RUN",
        stamped,
        skipped,
        failed,
        "" if apply else " — re-run with --apply to write",
    )
    if need_reauth:
        log.info(
            "%d doc(s) stamped WITHOUT an identity sub (legacy grant, no openid scope). "
            "They stay invisible to the add-account dedup until each owner re-authorizes "
            "the doc in place — the  callback stamps sub on the next successful auth.",
            need_reauth,
        )
    return 0 if not failed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", help="limit to one de_asset_youtube_accounts ESID (default: scan all)")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()
    return backfill(args.account, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())

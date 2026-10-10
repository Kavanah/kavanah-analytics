"""split a multi-identity YouTube account into per-identity docs.

One ``de_asset_youtube_accounts`` doc holds ONE refresh token, but a token can
only read analytics for channels its authorizing Google identity OWNS. When one
account doc carries channels owned by several Brand Account identities (the
shared-bucket case: ~12 channels, one token slot), every re-authorization fixes
one channel and 403-breaks the rest — the token ping-pong.

This tool splits such an account: every linked channel that the account's
CURRENT token does not own is moved onto a fresh account doc of its own
(email copied, new OAuth url/state, no token), and the tool prints each new
doc's OAuth URL. The channel's owner then opens that URL and authorizes,
selecting the channel's own Brand Account tile at Google's chooser — the
 callback guards verify the selected identity actually owns the
channel before saving.

Channels the current token DOES own stay on the original doc, so whichever
identity authorized last keeps working through the split.

The tool never touches tokens, team links (``team_youtube_channel_handle_esids``
holds channel esids, which don't change), or ``de_connection`` rows. The
importer reads team-linked channels + each channel's account token, so imports
recover channel-by-channel as owners authorize.

Usage (inside imports_container)::

    # Dry-run (default) — print the split plan, write nothing.
    python -m bin.python.tools.split_youtube_account --account <account_esid>

    # Apply the writes.
    python -m bin.python.tools.split_youtube_account --account <account_esid> --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import requests
import yaml

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/connection_backfill.py.
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
log = logging.getLogger("split_youtube_account")

ACCOUNTS_INDEX = "de_asset_youtube_accounts"
CHANNELS_INDEX = "de_asset_youtube_channels"
OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"
MASTER_CONFIG_PATH = "/app/reporting_configs/master-config.yaml"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


def _dual_term(field: str, value: str) -> dict[str, Any]:
    """Match on both the bare field and .keyword — the de_asset_* mappings
    differ per environment."""
    return {
        "bool": {
            "should": [
                {"term": {field: value}},
                {"term": {f"{field}.keyword": value}},
            ],
            "minimum_should_match": 1,
        }
    }


def _fboauth_hostname() -> str:
    try:
        with open(MASTER_CONFIG_PATH, "r") as fh:
            config = yaml.safe_load(fh) or {}
        return str((config.get("fboauth") or {}).get("hostname") or "")
    except Exception as e:  # noqa: BLE001 — hostname is display-only here
        log.warning("could not read fboauth hostname: %s", e)
        return ""


def _random_string(length: int) -> str:
    # same CSPRNG generator as FormService.generate_random_string.
    from lib.oauth_state import random_alnum

    return random_alnum(length)


def _unique_auth_url(es: Any) -> str:
    """15-char auth_url unique across the accounts index (mirrors the
    ``auto_generate`` unique flag on youtube_account_auth_url)."""
    while True:
        value = _random_string(15)
        result = es.search(
            index=ACCOUNTS_INDEX,
            body={"query": _dual_term("youtube_account_auth_url", value), "size": 1},
        )
        if not result.get("hits", {}).get("hits", []):
            return value


def _owned_channel_ids(account_source: dict[str, Any]) -> set[str] | None:
    """Channel ids the account's CURRENT token owns, or None when unknowable
    (no token / refresh failed / probe failed)."""
    enc_token = account_source.get("youtube_account_token")
    if not enc_token:
        log.info("account has no token — every linked channel needs a per-identity doc")
        return None
    try:
        client = load_google_oauth_client()  # env-var first, on-disk fallback
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
        access_token = (token_resp.json() or {}).get("access_token")
        if not access_token:
            log.warning("token refresh failed (%s): %s", token_resp.status_code, token_resp.text[:200])
            return None
        chan_resp = requests.get(
            CHANNELS_URL,
            params={"part": "id", "mine": "true", "maxResults": 50},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if chan_resp.status_code == 401 and "youtubeSignupRequired" in (chan_resp.text or ""):
            return set()  # current identity owns nothing
        if chan_resp.status_code != 200:
            log.warning("channels.list probe HTTP %s: %s", chan_resp.status_code, chan_resp.text[:200])
            return None
        return {str(i.get("id")) for i in (chan_resp.json() or {}).get("items") or [] if i.get("id")}
    except Exception as e:  # noqa: BLE001 — CLI boundary; unknown ownership is a valid outcome
        log.warning("ownership probe failed: %s", e)
        return None


def split_account(account_esid: str, apply: bool) -> int:
    es = get_elasticsearch_client()
    if es is None:
        log.error("no Elasticsearch client")
        return 1

    account = es.get(index=ACCOUNTS_INDEX, id=account_esid)
    account_source = account.get("_source") or {}
    title = account_source.get("youtube_account_title") or account_esid
    email = account_source.get("youtube_account_email") or ""
    log.info("account %s (%r, email=%r)", account_esid, title, email)

    result = es.search(
        index=CHANNELS_INDEX,
        body={"query": _dual_term("youtube_account_esid", account_esid), "size": 500},
    )
    channel_hits = result.get("hits", {}).get("hits", [])
    if not channel_hits:
        log.info("no channels linked to this account — nothing to split")
        return 0

    owned = _owned_channel_ids(account_source)
    if owned is None:
        log.info("current token ownership UNKNOWN — splitting ALL %d channel(s)", len(channel_hits))
        owned = set()

    hostname = _fboauth_hostname()
    moved = 0
    for hit in channel_hits:
        ch_id = hit["_id"]
        ch = hit.get("_source") or {}
        cid = str(ch.get("youtube_channel_id") or "")
        ch_title = ch.get("youtube_channel_title") or cid
        if cid in owned:
            log.info("KEEP   %-30s (%s) — current token owns it", ch_title, cid)
            continue

        new_title = f"{ch_title} (owner)"
        moved += 1
        if not apply:
            log.info("SPLIT  %-30s (%s) -> new account %r [dry-run]", ch_title, cid, new_title)
            continue

        new_account = {
            "youtube_account_title": new_title,
            "youtube_account_email": email,
            "youtube_account_auth_url": _unique_auth_url(es),
            "youtube_account_auth_state": _random_string(25),
        }
        # Preserve visibility scoping from the original doc (a
        # doc with no workspace_esid is invisible to workspace-scoped users).
        for carry in ("workspace_esid", "wizard_created_by"):
            if account_source.get(carry):
                new_account[carry] = account_source[carry]
        created = es.index(index=ACCOUNTS_INDEX, document=new_account)
        new_esid = created.get("_id")

        # Re-point the channel at its per-identity account (both the esid and
        # the display title, matching the store_esid convention).
        ch["youtube_account_esid"] = new_esid
        ch["youtube_account_title"] = new_title
        es.index(index=CHANNELS_INDEX, id=ch_id, document=ch)

        auth_url = (
            f"https://{hostname}/youtube/{new_account['youtube_account_auth_url']}"
            if hostname
            else (f"/youtube/{new_account['youtube_account_auth_url']}")
        )
        log.info("SPLIT  %-30s (%s) -> account %s — OWNER MUST AUTHORIZE: %s", ch_title, cid, new_esid, auth_url)

    log.info(
        "%s: %d channel(s) split off, %d kept on the original doc%s",
        "APPLIED" if apply else "DRY-RUN",
        moved,
        len(channel_hits) - moved,
        "" if apply else " — re-run with --apply to write",
    )
    if apply and moved:
        log.info(
            "Next: each printed OAuth URL must be opened by the channel's owner, who signs in "
            "with %s and selects THAT channel's Brand Account tile at Google's chooser. The "
            "callback refuses identities that don't own the channel.",
            email or "the account's Google email",
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--account", required=True, help="ESID of the de_asset_youtube_accounts doc to split")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = parser.parse_args()
    return split_account(args.account, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())

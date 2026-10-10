"""TikTok connector constants.

The app is a **TikTok for Developers** app (developers.tiktok.com — Login Kit +
Display API), not a TikTok API for Business app: its credentials are a
``client_key`` / ``client_secret`` pair, verified against
``open.tiktokapis.com`` on 2026-09-26. Ads and the richer Business Account
insights need the Business API app; this app reads the
authorizing account's profile counts and its videos' lifetime counts.

The credentials live in AWS SSM and reach ``imports_container`` through the same
chain as the Google OAuth client:

    SSM /km/<env>/tiktok/client-key|client-secret
      --(PARAM_TO_ENV, deployment/start-secrets-container.sh)--> deployment/.env
      --(environment:, deployment/docker-compose.yml)-->         the container

Only ``/km/dev/`` carries values today: the sandbox app, which TikTok grants
without review. ``/km/prod/`` stays empty until TikTok reviews the app, so prod
gets empty variables and anything that reads them there fails as not configured.
"""

import os

ENV_CLIENT_KEY = "TIKTOK_CLIENT_KEY"
ENV_CLIENT_SECRET = "TIKTOK_CLIENT_SECRET"

# the connector's on/off switch. OFF BY ABSENCE, like
# DEDUP_ROUTING_ENABLED: a host whose deployment/.env never sets it (prod, until
# a teammate turns TikTok on) gets no tile, no wizard or OAuth routes, no TikTok data
# view in the pickers, no nightly cron line and no job-health expectation.
# Staging sets it true. Turning it on is `TIKTOK_ENABLED=true` in .env followed
# by start-imports-container.sh, which recreates the container AND re-provisions
# cron (deployment/03_provision_cron_jobs.sh reads the same line from .env).
ENV_ENABLED = "TIKTOK_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def tiktok_enabled() -> bool:
    """Whether the TikTok connector is switched on for this deployment.

    Read on every call (not cached at import) so a test can flip it with
    ``monkeypatch.setenv`` and a caller never sees a stale value.
    """
    return os.environ.get(ENV_ENABLED, "").strip().lower() in _TRUTHY


# Login Kit for Web. The redirect URI registered in the TikTok portal must match
# the one built from fboauth.hostname byte for byte (no trailing slash, no query).
AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
CALLBACK_PATH = "/tiktok/auth_callback"

USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"

# Every scope the importer needs. user.info.profile carries ``username`` (used
# for the connection label); user.info.stats the account counts; video.list the
# per-video counts. The portal must have all four enabled on the app.
SCOPES = ("user.info.basic", "user.info.profile", "user.info.stats", "video.list")

# Team linkage on de_asset_teams, read by the importer and written by the wizard
#. The list holds de_connection esids: one TikTok account per
# connection, any number of accounts per team. Never the generic
# ``connection_esid`` — that field belongs to Meta.
TEAM_FLAG_FIELD = "ds_tiktok"
TEAM_CONNECTIONS_FIELD = "team_tiktok_connection_esids"

# {team_esid}__tiktok_organic
SOURCE_SLUG = "tiktok_organic"
RECORD_ACCOUNT_DAILY = "account_daily"
RECORD_VIDEO_DAILY = "video_daily"

# A grant missing any of these cannot import (the consent screen lets the user
# untick scopes, and TikTok then returns a narrower ``scope`` string).
REQUIRED_SCOPES = frozenset({"user.info.basic", "user.info.stats", "video.list"})

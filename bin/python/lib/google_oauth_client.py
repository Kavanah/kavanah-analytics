"""Shared Google OAuth client + Google Ads developer-token resolution.

the last two on-disk Google secrets move to AWS SSM. The host
fetches them at container start, writes a mode-600 env file and injects it via
compose ``env_file:`` (the ``app-backups`` pattern), so the application
reads them from the environment and never from disk.

Precedence is **env-var first, on-disk fallback** — byte-for-byte the shape
``lib.encryption.load_encryption_key`` already uses for ``ENC_KEY`` under
. The fallback is what lets this ship before km-infra's SSM
provisioning lands: with no env vars set, every call resolves
exactly as it did before.

Once SSM is verified on staging then prod, the legacy files
(``youtube-oauth-client.json.enc`` and the ``developer_token`` line in
``google-ads.yaml``) are deleted and the fallback becomes dead code. Note that
``google-ads.yaml`` itself SURVIVES: it also carries ``login_customer_id``
(9455684499 on both staging and prod), which is not a secret and stays on disk.

One client serves all three Google connectors — Google Ads, GA4 and YouTube —
because verified a single OAuth client for every sensitive scope
(``youtube.readonly``, ``yt-analytics.readonly``, ``analytics.readonly``,
``adwords``). The file is named for YouTube purely for historical reasons.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Legacy on-disk location of the shared OAuth client (Fernet-encrypted JSON).
# Retained only as the pre-SSM fallback; see the module docstring.
LEGACY_OAUTH_CLIENT_PATH = "/app/reporting_configs/youtube-oauth-client.json.enc"

# Legacy on-disk location of the Google Ads developer token.
LEGACY_GOOGLE_ADS_CONFIG_PATH = "/app/reporting_configs/google-ads.yaml"

ENV_CLIENT_ID = "GOOGLE_OAUTH_CLIENT_ID"
ENV_CLIENT_SECRET = "GOOGLE_OAUTH_CLIENT_SECRET"
ENV_ADS_DEVELOPER_TOKEN = "GOOGLE_ADS_DEVELOPER_TOKEN"


class GoogleOAuthClientError(Exception):
    """Raised when the shared Google OAuth client cannot be resolved."""


def load_google_oauth_client(legacy_path=None):
    """Resolve the shared Google OAuth client credentials.

    Precedence:
        1. ``GOOGLE_OAUTH_CLIENT_ID`` + ``GOOGLE_OAUTH_CLIENT_SECRET`` env vars
           (injected from SSM). BOTH must be present to use this source — a
           half-configured environment is an operator error worth surfacing
           loudly rather than silently falling back to a stale on-disk value.
        2. The legacy encrypted JSON file on disk.

    Args:
        legacy_path: Override the on-disk fallback path (tests).

    Returns:
        dict: ``{"client_id": str, "client_secret": str}``.

    Raises:
        GoogleOAuthClientError: If exactly one of the two env vars is set, or if
            neither source yields a usable client.
    """
    env_client_id = os.environ.get(ENV_CLIENT_ID)
    env_client_secret = os.environ.get(ENV_CLIENT_SECRET)

    if env_client_id and env_client_secret:
        logger.info("Google OAuth client resolved from environment (SSM-injected)")
        return {"client_id": env_client_id, "client_secret": env_client_secret}

    # Partial configuration: fail loudly. Falling back to disk here would mask a
    # broken SSM injection and could pair a NEW client_id with an OLD secret.
    if bool(env_client_id) != bool(env_client_secret):
        missing = ENV_CLIENT_SECRET if env_client_id else ENV_CLIENT_ID
        raise GoogleOAuthClientError(
            f"Google OAuth client half-configured: {missing} is unset while the other is set. "
            f"Set both (from SSM) or neither (to use the on-disk fallback)."
        )

    path = legacy_path or LEGACY_OAUTH_CLIENT_PATH
    # load_oauth_client_file does the lib.encryption import locally — that module
    # pulls in cryptography and reads master-config, which the env-var path above
    # has no need of, so it must not be imported at module scope.
    try:
        client_creds = load_oauth_client_file(path)
    except FileNotFoundError as e:
        raise GoogleOAuthClientError(_no_client_message(path, reason="no fallback file at")) from e

    client = (client_creds or {}).get("web") or (client_creds or {}).get("installed")
    if not client or not client.get("client_id"):
        raise GoogleOAuthClientError(f"Invalid OAuth client credentials format in {path}")

    # the client_secret is no longer stored on disk. Once it was
    # stripped from the legacy file, a secretless file can still supply
    # client_id and redirect_uris (neither is a secret), but it can NO LONGER
    # produce a usable client on its own. Say so precisely: the old message
    # ("Invalid OAuth client credentials format") would send a reader hunting a
    # malformed file when the real answer is "the env vars are not set".
    if not client.get("client_secret"):
        raise GoogleOAuthClientError(_no_client_message(path, reason="no client_secret in"))

    logger.info("Google OAuth client resolved from legacy on-disk file %s", path)
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def _no_client_message(path, *, reason):
    """The one actionable message for 'no usable OAuth client available'.

    after the secret was stripped from disk, the env vars are the
    only source of a client_secret, so every failure here has the same fix. Name
    it once rather than leaving three differently-worded errors that all mean
    "run the secrets wrapper".
    """
    return (
        f"Google OAuth client unavailable: {ENV_CLIENT_ID}/{ENV_CLIENT_SECRET} are unset "
        f"and {reason} {path}. The client_secret is no longer stored on disk — "
        f"it comes from AWS SSM via deployment/start-secrets-container.sh. Run that wrapper "
        f"(SSM_ENV=prod SSM_PROFILE='' SSM_SUDO=1 on prod), or export both variables for a "
        f"local run."
    )


def load_oauth_client_file(path=None):
    """Read the on-disk OAuth client file, plaintext OR encrypted.

    the file used to be encrypted solely because it held the
    client_secret. With the secret in SSM the remaining contents (client_id,
    redirect_uris) are not secrets, so the file becomes plaintext JSON and the
    ``.enc`` is retired.

    Accepts both shapes during and after that migration, keyed off the file
    extension rather than a content sniff: ``.enc`` is decrypted, anything else
    is read as JSON. That means a host mid-migration, a host already converted,
    and a dev box that never had the file all behave sensibly, and no ordering
    constraint is imposed between the code deploy and the file rewrite.

    Raises FileNotFoundError when neither the plaintext nor the ``.enc`` form
    exists, so callers can distinguish "no file" from "unusable file".
    """
    import json

    from lib.encryption import decrypt_json_file

    candidate = path or LEGACY_OAUTH_CLIENT_PATH
    plain = candidate[: -len(".enc")] if candidate.endswith(".enc") else candidate
    encrypted = plain + ".enc"

    # Prefer plaintext: after the migration it is the live file, and preferring
    # it means a leftover .enc holding the DEAD pre-rotation secret cannot win.
    # Checked with os.path.exists because a plaintext file is only present on a
    # migrated host — its ABSENCE is the normal case, not an error.
    if os.path.exists(plain):
        logger.debug("Reading plaintext OAuth client file %s", plain)
        with open(plain, "r", encoding="utf-8") as fh:
            return json.load(fh)

    # The .enc read is NOT guarded by os.path.exists on purpose. decrypt_json_file
    # raises FileNotFoundError itself, and long-standing tests across three suites
    # (test_youtube) mock
    # ``lib.encryption.decrypt_json_file`` and expect it to be reached — an
    # existence pre-check here makes that seam unreachable and turns a
    # malformed-file assertion into a spurious "no fallback file". Let the reader
    # own the FileNotFoundError.
    logger.debug("Reading encrypted OAuth client file %s", encrypted)
    return decrypt_json_file(encrypted)


def load_google_ads_developer_token(yaml_config=None):
    """Resolve the Google Ads developer token.

     INVERTS the previous precedence. ``get_campaign_data`` used to read
    the YAML first and consult ``GOOGLE_ADS_DEVELOPER_TOKEN`` only as a fallback;
    the env var now wins so an SSM-injected value takes effect without editing
    the file. No env var is set in prod today, so the cutover is a no-op there
     lands.

    Args:
        yaml_config: Already-parsed ``google-ads.yaml`` contents, or None. The
            caller owns reading the file because it also needs
            ``login_customer_id`` from it, which is NOT a secret and stays on
            disk after the SSM migration.

    Returns:
        str | None: The developer token, or None if neither source has one.
    """
    env_token = os.environ.get(ENV_ADS_DEVELOPER_TOKEN)
    if env_token:
        logger.info("Google Ads developer token resolved from environment (SSM-injected)")
        return env_token

    yaml_token = (yaml_config or {}).get("developer_token")
    if yaml_token:
        logger.info("Google Ads developer token resolved from legacy on-disk YAML")
        return yaml_token

    return None

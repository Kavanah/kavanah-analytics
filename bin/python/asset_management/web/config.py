"""
Configuration management for the Web Asset Manager.
Loads settings from master-config.yaml and environment variables.
"""

import os
import yaml

# neutral Kibana fallback. Inside the compose stack Kibana is reachable
# at its service name (start_web_apps.sh exports the same value); a deployment whose
# Kibana lives elsewhere sets KIBANA_URL explicitly. Never an org hostname.
DEFAULT_KIBANA_URL = "http://kib01:5601/kibana"


class MasterConfigError(RuntimeError):
    """master-config.yaml exists but could not be used.

    Raised instead of warn-and-continue: a partial/empty config is more
    dangerous than a hard stop. The 2026-06-06 incident ran prod on
    defaults for EVERY value (FBOAUTH_HOSTNAME included) behind a single
    stdout warning, because one cp1252 em-dash byte in a comment made the
    whole file undecodable; (SECRET_KEY=null) was the same
    silent-default class.
    """


def _read_master_config(config_path):
    """Read and parse master-config.yaml, failing LOUDLY on a broken file.

    Returns the parsed mapping, or ``None`` when the file does not exist —
    dev/test environments without a mounted config keep running on env vars
    and defaults, unchanged.

    a file that EXISTS but cannot be read, decoded, or parsed
    raises MasterConfigError naming the exact byte/line/column (the em-dash
    incident took manual byte-dumping to locate offset 0x97) instead of
    silently defaulting the entire config.
    """
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, "rb") as f:
            raw = f.read()
    except OSError as e:
        raise MasterConfigError(
            f"{config_path} exists but cannot be read: {e}. Refusing to boot on defaults."
        ) from e
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        line = raw.count(b"\n", 0, e.start) + 1
        col = e.start - raw.rfind(b"\n", 0, e.start)
        raise MasterConfigError(
            f"{config_path} is not valid UTF-8: byte 0x{raw[e.start]:02x} at line {line}, "
            f"column {col} (file offset {e.start}). Common culprit: cp1252 punctuation pasted "
            "into a comment (em/en dash, smart quotes) - replace it with ASCII. "
            "Refusing to boot on defaults."
        ) from e
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise MasterConfigError(f"{config_path} is not valid YAML: {e}. Refusing to boot on defaults.") from e
    if not isinstance(parsed, dict) or not parsed:
        shape = "nothing" if not parsed else type(parsed).__name__
        raise MasterConfigError(
            f"{config_path} parsed to {shape} instead of a mapping of config sections - "
            "an empty or malformed config where one is mounted means every value silently "
            "defaults. Refusing to boot."
        )
    return parsed


def load_config():
    """Load configuration from master-config.yaml and environment variables."""
    config = {
        "SECRET_KEY": os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production"),
        "APP_HOST": os.environ.get("APP_HOST", "0.0.0.0"),
        "APP_PORT": int(os.environ.get("APP_PORT", 8084)),
        "FBOAUTH_HOSTNAME": None,
        # explicit Meta App ID for the onboarding wizard's
        # OAuth flow. Env var overrides master-config; either path
        # accepts the numeric FB App ID as a string (e.g.
        # "1858829444798788"). Empty / unset disables the wizard's
        # Meta OAuth (get_meta_app returns None, step-5 falls into its
        # fail-soft server_error banner).
        "WIZARD_META_APP_ID": os.environ.get("WIZARD_META_APP_ID", ""),
        # override the wizard's on-demand backfill window
        # (_CONNECTOR_IMPORTER_REGISTRY[*]["days_total"]) without
        # editing source. Empty / unset => use the registry's per-
        # connector default. Numeric string clamps to >= 1 day at
        # the consumer side.
        "WIZARD_KICKOFF_DAYS": os.environ.get("WIZARD_KICKOFF_DAYS", ""),
        "YUBICO_CLIENT_ID": os.environ.get("YUBICO_CLIENT_ID", ""),
        "YUBICO_SECRET_KEY": os.environ.get("YUBICO_SECRET_KEY", ""),
    }

    # master-config.yaml (mounted at container start). Missing file =>
    # env vars/defaults (dev and test boxes). Present-but-broken file =>
    # MasterConfigError - retired the warn-and-continue fallback,
    # and consumption below is deliberately UNwrapped so a malformed
    # section also refuses to boot instead of silently defaulting.
    yaml_config = _read_master_config("/app/reporting_configs/master-config.yaml")
    if yaml_config is not None:
        # Web app settings
        web_config = yaml_config.get("asset_web", {})
        if web_config:
            config["APP_PORT"] = web_config.get("port", config["APP_PORT"])
            # Use flask_secret_key from config if no env var is set
            yaml_secret = web_config.get("flask_secret_key")
            if yaml_secret and config["SECRET_KEY"] == "dev-secret-key-change-in-production":
                config["SECRET_KEY"] = yaml_secret

        # FB OAuth hostname for generating OAuth URLs.
        # wizard_meta_app_id pins the onboarding
        # wizard's Meta app selection per-environment so dev,
        # staging, and prod can each route through the right
        # FB App ID without sharing de_asset_fb_apps rows.
        # Env var (set above) wins when present; otherwise
        # the YAML value applies.
        fboauth_config = yaml_config.get("fboauth", {})
        if fboauth_config:
            config["FBOAUTH_HOSTNAME"] = fboauth_config.get("hostname")
            if not config.get("WIZARD_META_APP_ID"):
                config["WIZARD_META_APP_ID"] = str(fboauth_config.get("wizard_meta_app_id") or "").strip()
            # env var (set above) wins when present;
            # otherwise master-config's fboauth.wizard_kickoff_days
            # applies. Empty string means "use registry default".
            if not config.get("WIZARD_KICKOFF_DAYS"):
                config["WIZARD_KICKOFF_DAYS"] = str(fboauth_config.get("wizard_kickoff_days") or "").strip()

        # Yubico OTP validation API credentials (optional, for YubiKey 2FA)
        yubico_config = yaml_config.get("yubico", {})
        if yubico_config:
            if not config.get("YUBICO_CLIENT_ID"):
                config["YUBICO_CLIENT_ID"] = yubico_config.get("client_id", "")
            if not config.get("YUBICO_SECRET_KEY"):
                config["YUBICO_SECRET_KEY"] = yubico_config.get("secret_key", "")

        # Registration settings (optional, for self-service registration)
        reg_config = yaml_config.get("registration", {})
        if reg_config:
            if not os.environ.get("HCAPTCHA_SITE_KEY"):
                config["HCAPTCHA_SITE_KEY"] = reg_config.get("hcaptcha_site_key", "")
            if not os.environ.get("HCAPTCHA_SECRET_KEY"):
                config["HCAPTCHA_SECRET_KEY"] = reg_config.get("hcaptcha_secret_key", "")
            if not os.environ.get("REGISTRATION_ENABLED"):
                config["REGISTRATION_ENABLED"] = reg_config.get("enabled", False)

        # NLQ (Natural Language Query) settings
        nlq_config = yaml_config.get("nlq", {})
        if nlq_config:
            config["NLQ_ANTHROPIC_API_KEY"] = nlq_config.get("anthropic_api_key", "")
            # Haiku 5.5 on BOTH direct-API routes (a teammate, 2026-10-22:
            # the cheapest model for all AI usage). Bare ids, no date suffix.
            config["NLQ_MODEL"] = nlq_config.get("model", "claude-haiku-5-5")
            config["NLQ_ENABLED"] = nlq_config.get("enabled", False)
            config["NLQ_MAX_TOKENS"] = nlq_config.get("max_tokens", 8192)
            # every user has Ask Kavanah, so each one gets a daily
            # spend cap (a teammate, 2026-10-27: $2, matching the canvas AI cap).
            config["NLQ_USER_DAILY_COST_CAP_USD"] = nlq_config.get("user_daily_cost_cap_usd", 2.0)
            config["NLQ_PROVIDER"] = nlq_config.get("provider", "anthropic")
            config["NLQ_AWS_REGION"] = nlq_config.get("aws_region", "us-west-2")
            config["NLQ_AWS_PROFILE"] = nlq_config.get("aws_profile", "")
            config["NLQ_BEDROCK_MODEL_HAIKU"] = nlq_config.get(
                "bedrock_model_haiku", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
            )
            # Sonnet 4 became Legacy-gated on our Bedrock
            # account (30-day inactivity) and fails every invocation.
            # global. profile requires the per-env bedrock IAM users
            # (2026-07-14 cutover, both envs done).
            config["NLQ_BEDROCK_MODEL_SONNET"] = nlq_config.get("bedrock_model_sonnet", "global.anthropic.claude-sonnet-4-6")
            # the old default "claude-sonnet-4-5-20250514"
            # never existed (Sonnet 4.5 name, Sonnet 4 date) and
            # would 404 at the Anthropic API. Bare alias, no date.
            config["NLQ_MODEL_SONNET"] = nlq_config.get("model_sonnet", "claude-haiku-5-5")

        # Canvas AI chat settings. Provider/credentials are
        # shared with the NLQ section (same per-env bedrock IAM user);
        # this section only carries the canvas-specific switches: the
        # feature flag, the auto-routed tier->model map, and the daily
        # cost caps. Tier defaults follow the post-IAM-grant
        # re-probe + the private docs 2026-07-14 (legacy
        # AnthropicBedrock client; Opus 4.8 stays on the us. profile —
        # the only probe-verified one).
        # all tiers ship us. profiles — the running prod
        # container's Bedrock IAM only matches us.*, and global. ids
        # 400 ("model identifier is invalid") → 502 on the chat
        # endpoint. Switch back to global. (10% cheaper) only after
        # the post-cutover creds are verified live on prod.
        # when NLQ_PROVIDER is 'anthropic' (direct API) the
        # tiers resolve to the anthropic_model_* keys instead — Haiku 5.5
        # on every tier (a teammate, 2026-10-22: the cheapest model for all AI
        # usage); the Bedrock profiles above are never sent to the direct API.
        canvas_ai_config = yaml_config.get("canvas_ai", {})
        if canvas_ai_config:
            config["CANVAS_AI_ENABLED"] = canvas_ai_config.get("enabled", False)
            config["CANVAS_AI_MAX_TOKENS"] = canvas_ai_config.get("max_tokens", 4096)
            # alter-dashboard turns emit the FULL modified
            # objects list, so they need far more output headroom than
            # a chat reply.
            config["CANVAS_AI_EDIT_MAX_TOKENS"] = canvas_ai_config.get("edit_max_tokens", 16384)
            config["CANVAS_AI_MODEL_FAST"] = canvas_ai_config.get("model_fast", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
            config["CANVAS_AI_MODEL_DEFAULT"] = canvas_ai_config.get("model_default", "us.anthropic.claude-sonnet-5")
            config["CANVAS_AI_MODEL_COMPLEX"] = canvas_ai_config.get("model_complex", "us.anthropic.claude-opus-4-8")
            config["CANVAS_AI_ANTHROPIC_MODEL_FAST"] = canvas_ai_config.get("anthropic_model_fast", "claude-haiku-5-5")
            config["CANVAS_AI_ANTHROPIC_MODEL_DEFAULT"] = canvas_ai_config.get("anthropic_model_default", "claude-haiku-5-5")
            config["CANVAS_AI_ANTHROPIC_MODEL_COMPLEX"] = canvas_ai_config.get("anthropic_model_complex", "claude-haiku-5-5")
            config["CANVAS_AI_USER_DAILY_COST_CAP_USD"] = canvas_ai_config.get("user_daily_cost_cap_usd", 2.0)
            # a whole-feature daily ceiling, sized to the monthly API
            # credit (it was 25.0).
            config["CANVAS_AI_FEATURE_DAILY_COST_CAP_USD"] = canvas_ai_config.get("feature_daily_cost_cap_usd", 6.0)

        # Security settings (MFA enforcement, etc.) —
        security_config = yaml_config.get("security", {})
        if security_config:
            if not os.environ.get("ENFORCE_MFA"):
                config["ENFORCE_MFA"] = security_config.get("enforce_mfa", True)

        # Staff notifications — who gets the new-signup email.
        notifications_config = yaml_config.get("notifications", {})
        if notifications_config:
            config["SIGNUP_NOTIFY_EMAIL"] = notifications_config.get("signup_email", "")

        # S3 backup settings (Restore from Backup page).
        # Bucket + prefix vary per environment; dev points at
        # elastic-stack-dev/, prod at elastic-stack-prod/ (the
        # km-infra writer's prefix; app/ was never written to).
        s3_backup_config = yaml_config.get("s3_backup", {})
        if s3_backup_config:
            config["S3_BACKUP_BUCKET"] = s3_backup_config.get("bucket", "km-wp-backups")
            config["S3_BACKUP_PREFIX"] = s3_backup_config.get("prefix", "elastic-stack-dev")

        # Google SSO — "Continue with Google" entry point for
        # the new-user onboarding wizard. Empty client_id disables the
        # SSO button at the template layer.
        sso_config = yaml_config.get("google_sso", {})
        if sso_config:
            if not os.environ.get("GOOGLE_SSO_CLIENT_ID"):
                config["GOOGLE_SSO_CLIENT_ID"] = sso_config.get("client_id", "")
            if not os.environ.get("GOOGLE_SSO_CLIENT_SECRET"):
                config["GOOGLE_SSO_CLIENT_SECRET"] = sso_config.get("client_secret", "")
            if not os.environ.get("GOOGLE_SSO_REDIRECT_URI"):
                config["GOOGLE_SSO_REDIRECT_URI"] = sso_config.get("redirect_uri", "")

        # Microsoft SSO — "Continue with Microsoft" entry
        # point for the new-user onboarding wizard, alongside Google
        # SSO. Empty client_id disables the button at the
        # template layer. ``tenant`` defaults to ``common`` (accepts
        # work/school AND personal Microsoft accounts); set to an
        # Entra tenant GUID or verified domain for single-tenant
        # deployments.
        ms_sso_config = yaml_config.get("microsoft_sso", {})
        if ms_sso_config:
            if not os.environ.get("MICROSOFT_SSO_CLIENT_ID"):
                config["MICROSOFT_SSO_CLIENT_ID"] = ms_sso_config.get("client_id", "")
            if not os.environ.get("MICROSOFT_SSO_CLIENT_SECRET"):
                config["MICROSOFT_SSO_CLIENT_SECRET"] = ms_sso_config.get("client_secret", "")
            if not os.environ.get("MICROSOFT_SSO_REDIRECT_URI"):
                config["MICROSOFT_SSO_REDIRECT_URI"] = ms_sso_config.get("redirect_uri", "")
            if not os.environ.get("MICROSOFT_SSO_TENANT"):
                config["MICROSOFT_SSO_TENANT"] = ms_sso_config.get("tenant", "common")

        # Jira integration settings (for feedback → creation)
        jira_config = yaml_config.get("jira", {})
        if jira_config:
            config["JIRA_ENABLED"] = jira_config.get("enabled", False)
            config["JIRA_BASE_URL"] = jira_config.get("base_url", "")
            config["JIRA_PROJECT_KEY"] = jira_config.get("project_key", "")
            config["JIRA_USER_EMAIL"] = jira_config.get("user_email", "")
            config["JIRA_API_TOKEN"] = jira_config.get("api_token", "")
            config["JIRA_BUG_ISSUE_TYPE_ID"] = jira_config.get("bug_issue_type_id", "10089")
            config["JIRA_STORY_ISSUE_TYPE_ID"] = jira_config.get("story_issue_type_id", "10012")

        # Stripe billing settings. The
        # publishable key is client-safe and reaches the template;
        # the secret key + webhook secret stay server-side.
        # ``live_payments`` gates whether create-subscription/webhook
        # make REAL Stripe calls vs. stub (default false → stubbed,
        # no plan change). NOTE it is NOT a test-vs-production switch:
        # test-vs-live is the KEY PREFIX (sk_test_ → test mode / fake
        # money, sk_live_ → real money). To validate end-to-end on
        # staging, set live_payments=true WITH sk_test_/whsec_/price_
        # keys — the 4242 test card then succeeds (no 503). Empty keys
        # + live_payments=true is the only 503. Env vars override.
        stripe_config = yaml_config.get("stripe", {})
        if stripe_config:
            if not os.environ.get("STRIPE_PUBLISHABLE_KEY"):
                config["STRIPE_PUBLISHABLE_KEY"] = stripe_config.get("publishable_key", "")
            if not os.environ.get("STRIPE_SECRET_KEY"):
                config["STRIPE_SECRET_KEY"] = stripe_config.get("secret_key", "")
            if not os.environ.get("STRIPE_WEBHOOK_SECRET"):
                config["STRIPE_WEBHOOK_SECRET"] = stripe_config.get("webhook_secret", "")
            # ``price_business`` (Stripe "Business" product, renamed
            # from price_forprofit 2026-06-05). All envs use the new
            # key now, so the old-key fallback was dropped.
            if not os.environ.get("STRIPE_PRICE_BUSINESS"):
                config["STRIPE_PRICE_BUSINESS"] = stripe_config.get("price_business", "")
            if not os.environ.get("STRIPE_PRICE_NONPROFIT"):
                config["STRIPE_PRICE_NONPROFIT"] = stripe_config.get("price_nonprofit", "")
            # item 5b — self-serve add-on connection price.
            # Test-mode id created 2026-06-09; the live id is an
            # ops/master-config step before prod.
            # NOTE: the mission 50%-off coupon is NOT configured here —
            # mission $5 stays sales-assisted (no verification flag yet),
            # so the system never auto-applies a coupon. Customer-entered
            # coupons (like the existing two) resolve live by code via
            # validate_coupon and need no config. A coupon id only needs
            # config if the system applies it automatically — add
            # ``coupon_addon_mission`` here only when mission self-serve
            # ships and the route actually sets mission_coupon.
            if not os.environ.get("STRIPE_PRICE_ADDON_CONNECTION"):
                config["STRIPE_PRICE_ADDON_CONNECTION"] = stripe_config.get("price_addon_connection", "")
            if not os.environ.get("STRIPE_LIVE_PAYMENTS"):
                config["STRIPE_LIVE_PAYMENTS"] = stripe_config.get("live_payments", False)

    return config


class Config:
    """Flask configuration class."""

    def __init__(self):
        self._config = load_config()

    @property
    def SECRET_KEY(self):
        return self._config["SECRET_KEY"]

    @property
    def APP_HOST(self):
        return self._config["APP_HOST"]

    @property
    def APP_PORT(self):
        return self._config["APP_PORT"]

    @property
    def FBOAUTH_HOSTNAME(self):
        return self._config["FBOAUTH_HOSTNAME"]

    @property
    def WIZARD_META_APP_ID(self):
        """explicit Meta App ID for the onboarding wizard.

        Read by ``WizardOAuthService.get_meta_app()`` to pick the exact
        ``de_asset_fb_apps`` row to use, instead of the non-deterministic
        ``match_all + _doc:asc`` query that returned whichever doc Lucene
        sorted first in insertion order. Set per-environment in
        ``master-config.yaml`` under ``fboauth.wizard_meta_app_id`` or via
        the ``WIZARD_META_APP_ID`` env var. Empty string means unset —
        ``get_meta_app`` returns ``None`` and the wizard falls into its
        fail-soft server_error banner.
        """
        return self._config.get("WIZARD_META_APP_ID", "")

    @property
    def WIZARD_KICKOFF_DAYS(self):
        """override the wizard's on-demand backfill window.

        Read by ``_kickoff_on_demand_backfill`` in
        ``blueprints/analytics/routes.py``. When set to a positive
        integer string, overrides the per-connector ``days_total`` in
        ``_CONNECTOR_IMPORTER_REGISTRY`` so a wizard signup pulls only
        the last N days of backfill instead of the registry default
        (7 for meta_ads, 30 for echo / google_ads / GA4).

        Use case: staging needs a 1-day cycle so a fresh wizard finish
        completes in seconds during QA. Prod stays unset → registry
        defaults apply.

        Set per-environment via:
        * ``WIZARD_KICKOFF_DAYS`` env var, or
        * ``fboauth.wizard_kickoff_days`` in master-config.yaml.

        Returns ``None`` when unset, an int >= 1 otherwise. Unparseable
        values (non-numeric, <= 0) also return ``None`` so the registry
        default kicks in rather than a misconfigured zero-day window.
        """
        raw = (self._config.get("WIZARD_KICKOFF_DAYS") or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 1 else None

    @property
    def ES_AUTH_REQUIRED(self):
        """Whether ES authentication is required (can disable for dev)."""
        return os.environ.get("ES_AUTH_REQUIRED", "true").lower() == "true"

    @property
    def REQUIRED_ROLE(self):
        """ES role required for access."""
        return os.environ.get("ASSET_ADMIN_ROLE", "asset_admin")

    @property
    def SESSION_LIFETIME(self):
        """Idle timeout in seconds — how long a session can sit inactive
        before the next request forces re-login. Refreshed on each
        authenticated request. Default 14400s (4h).

        Historically named SESSION_LIFETIME when the session was an
        absolute timer; the env var name is preserved for compatibility.
        """
        return int(os.environ.get("SESSION_LIFETIME", "14400"))

    @property
    def SESSION_MAX_LIFETIME(self):
        """Absolute maximum session lifetime in seconds, regardless of
        activity. Mirrors Kibana's xpack.security.session.lifespan.
        After this many seconds since login the user is forced to
        re-authenticate even if they have been continuously active.
        Default 86400s (24h).
        """
        return int(os.environ.get("SESSION_MAX_LIFETIME", "86400"))

    @property
    def ELEVATION_LIFETIME(self):
        """how long a Kibana elevation lasts before requiring
        re-elevation. Set on the session at /admin/elevate; the gate at
        /api/internal/kibana-gate denies once now > elevated_until.
        Default 900s (15m), matching the fpc_pending / mfa_pending
        partial-session timeouts.
        """
        return int(os.environ.get("ELEVATION_LIFETIME", "900"))

    @property
    def TRUST_NGINX_HEADERS(self):
        """Whether to trust X-Remote-User from nginx (enable for SSO).

        Defaults to false. Set TRUST_NGINX_HEADERS=true in the
        Docker/deployment environment where nginx is the only path to
        the Flask app.
        """
        return os.environ.get("TRUST_NGINX_HEADERS", "false").lower() == "true"

    @property
    def TRUSTED_PROXY_IPS(self):
        """IPs allowed to assert identity via ``X-Remote-User``.

        Comma-separated allowlist compared against ``request.remote_addr``
        — the TCP peer, which unlike a header cannot be forged. Empty by
        default **everywhere** (same off-by-absence construction as
        ``TEST_AUTH_SECRET`` /): with no allowlist configured, the
        nginx header-SSO branch in ``verify_auth`` accepts nothing, even
        when ``TRUST_NGINX_HEADERS`` is true.

        ``TRUST_NGINX_HEADERS`` alone proved insufficient
        — the deploy exported it unconditionally while docker-compose
        published gunicorn's port to the host, so "nginx is the only path"
        did not hold and any client could forge a superuser session. If the
        header-SSO path is ever revived, set this to the reverse proxy's
        network address (e.g. ``172.18.0.5``) alongside PROXY_AUTH_SECRET.
        """
        raw = os.environ.get("TRUSTED_PROXY_IPS", "")
        return frozenset(ip.strip() for ip in raw.split(",") if ip.strip())

    @property
    def PROXY_AUTH_SECRET(self):
        """Shared secret the reverse proxy must present in ``X-Proxy-Auth``
        before ``X-Remote-User`` is honored.

        Empty by default **everywhere**, which keeps the header-SSO branch
        inert (off-by-absence pattern). Compared constant-time in
        ``verify_auth``. Second, independent proof of the proxy hop next to
        TRUSTED_PROXY_IPS: the IP check pins the TCP peer, the secret
        survives IP reuse/renumbering on the container network.
        """
        return os.environ.get("PROXY_AUTH_SECRET", "")

    @property
    def TEST_AUTH_SECRET(self):
        """Shared secret enabling the ``X-Test-Persona`` header-auth path.

        Empty by default **everywhere** — the header-auth branch in
        ``verify_auth`` is inert unless this is set to a non-empty value, and
        prod's deploy never sets it. This follows the ``DEDUP_ROUTING_ENABLED``
        pattern (a capability that's off-by-absence) rather than branching on an
        environment *name*, which deliberately forbids (an env-name
        branch that's misconfigured on prod re-enables dev behavior there).

        Set ``TEST_AUTH_SECRET=<random>`` in **staging's** ``.env`` only, so a
        verification agent can authenticate as a documented attack-surface
        persona (see the private docs)
        without a login/MFA/cookie dance. Because the path requires this secret,
        a leaked or misconfigured prod (which has no secret set) accepts nothing
        — it is safe by construction, not by an environment guess.

         adds the enforcement half: ``app.py`` refuses to START when
        this is non-empty and ``DEPLOY_ENV`` or ``APP_PUBLIC_HOSTNAME`` names
        production (``secret_validation.validate_test_auth_secret``). That gate
        is refuse-only, so it stays inside the a marker can
        remove the capability but never grant it.
        """
        return os.environ.get("TEST_AUTH_SECRET", "")

    # Phase 4 cutover flag ``USE_NEW_PERMISSIONS`` and Phase 3
    # shadow-compare flag ``USE_NEW_PERMISSIONS_SHADOW`` were retired in
    # (2026-05-28). The new-authz read path is unconditional.

    @property
    def ENFORCE_MFA(self):
        """Whether MFA is required for all web UI logins.

        When true: users with no MFA record or `mfa_enabled=false` are
        forced through enrollment on next login. When false: MFA remains
        per-user opt-in.

        Env var ENFORCE_MFA overrides the master-config.yaml setting.
        Default is true — secure posture is the default for new environments.
        """
        val = self._config.get("ENFORCE_MFA", os.environ.get("ENFORCE_MFA", "true"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def SIGNUP_NOTIFY_EMAIL(self) -> str:
        """Comma-separated addresses emailed when a new account signs up.

        Env var SIGNUP_NOTIFY_EMAIL overrides master-config.yaml
        ``notifications.signup_email``. Empty (the default) turns the notice off.
        """
        return os.environ.get("SIGNUP_NOTIFY_EMAIL") or self._config.get("SIGNUP_NOTIFY_EMAIL", "") or ""

    @property
    def YUBICO_CLIENT_ID(self):
        """Yubico OTP validation API client ID (free, register at upgrade.yubico.com/getapikey/)."""
        return self._config.get("YUBICO_CLIENT_ID", "")

    @property
    def YUBICO_SECRET_KEY(self):
        """Yubico OTP validation API secret key (base64-encoded HMAC key)."""
        return self._config.get("YUBICO_SECRET_KEY", "")

    @property
    def MFA_ALLOW_INSECURE_DEV(self):
        """(SVC-005): allow the YubiKey dev-mode bypass that skips
        Yubico API validation when YUBICO_CLIENT_ID is unset.

        Default false — secure posture is the default. In production a missing
        client ID must fail closed, not silently downgrade the YubiKey factor
        to a 12-char public-prefix check. Set MFA_ALLOW_INSECURE_DEV=true only
        in local/dev environments that intentionally run without Yubico creds.

        Env var MFA_ALLOW_INSECURE_DEV overrides the master-config.yaml setting.
        """
        val = self._config.get("MFA_ALLOW_INSECURE_DEV", os.environ.get("MFA_ALLOW_INSECURE_DEV", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def HCAPTCHA_SITE_KEY(self):
        """hCaptcha site key for registration form. Empty = captcha disabled."""
        return self._config.get("HCAPTCHA_SITE_KEY", os.environ.get("HCAPTCHA_SITE_KEY", ""))

    @property
    def HCAPTCHA_SECRET_KEY(self):
        """hCaptcha secret key for server-side verification."""
        return self._config.get("HCAPTCHA_SECRET_KEY", os.environ.get("HCAPTCHA_SECRET_KEY", ""))

    @property
    def REGISTRATION_ENABLED(self):
        """Whether self-service registration is enabled."""
        val = self._config.get("REGISTRATION_ENABLED", os.environ.get("REGISTRATION_ENABLED", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def GOOGLE_SSO_CLIENT_ID(self):
        """Google OAuth2 client ID for the "Continue with Google"
        wizard entry point. Empty string disables the SSO button at the
        template layer. Env var GOOGLE_SSO_CLIENT_ID overrides
        master-config.yaml."""
        return os.environ.get("GOOGLE_SSO_CLIENT_ID", self._config.get("GOOGLE_SSO_CLIENT_ID", ""))

    @property
    def GOOGLE_SSO_CLIENT_SECRET(self):
        """Google OAuth2 client secret. Server-only — never
        exposed to the template layer. Env var overrides master-config."""
        return os.environ.get("GOOGLE_SSO_CLIENT_SECRET", self._config.get("GOOGLE_SSO_CLIENT_SECRET", ""))

    @property
    def GOOGLE_SSO_REDIRECT_URI(self):
        """OAuth2 callback URL registered with Google Cloud
        Console. Must match exactly; deployment-dependent — dev points
        at https://<staging-host>/onboarding/google/callback,
        prod at https://kavanahanalytics.com/onboarding/google/callback."""
        return os.environ.get(
            "GOOGLE_SSO_REDIRECT_URI",
            self._config.get("GOOGLE_SSO_REDIRECT_URI", ""),
        )

    @property
    def GOOGLE_SSO_ENABLED(self):
        """Convenience: True when client_id, client_secret, and
        redirect_uri are all configured. The route handlers fail
        closed (404) when this returns False."""
        return bool(self.GOOGLE_SSO_CLIENT_ID and self.GOOGLE_SSO_CLIENT_SECRET and self.GOOGLE_SSO_REDIRECT_URI)

    @property
    def MICROSOFT_SSO_CLIENT_ID(self):
        """Microsoft Identity Platform application (client)
        ID for the "Continue with Microsoft" wizard entry point.
        Empty string disables the SSO button at the template layer.
        Env var MICROSOFT_SSO_CLIENT_ID overrides master-config.yaml."""
        return os.environ.get("MICROSOFT_SSO_CLIENT_ID", self._config.get("MICROSOFT_SSO_CLIENT_ID", ""))

    @property
    def MICROSOFT_SSO_CLIENT_SECRET(self):
        """Microsoft Identity Platform client secret.
        Server-only — never exposed to the template layer. Env var
        overrides master-config."""
        return os.environ.get("MICROSOFT_SSO_CLIENT_SECRET", self._config.get("MICROSOFT_SSO_CLIENT_SECRET", ""))

    @property
    def MICROSOFT_SSO_REDIRECT_URI(self):
        """OAuth2 callback URL registered in the Microsoft
        Entra app registration's Redirect URIs. Must match exactly;
        deployment-dependent — dev points at
        https://<staging-host>/onboarding/microsoft/callback,
        prod at https://kavanahanalytics.com/onboarding/microsoft/callback."""
        return os.environ.get(
            "MICROSOFT_SSO_REDIRECT_URI",
            self._config.get("MICROSOFT_SSO_REDIRECT_URI", ""),
        )

    @property
    def MICROSOFT_SSO_TENANT(self):
        """Microsoft Entra tenant. Defaults to ``common``
        (accepts both work/school + personal Microsoft accounts).
        Single-tenant deployments set this to the Entra tenant GUID
        or a verified domain to restrict sign-in to a specific
        organization.

         (nOAuth): under ``common`` the email claim of an
        Entra work account is only trusted for account matching /
        provisioning when the id_token asserts ``xms_edov`` (or the
        account is a personal MSA one); already-linked users always
        match on metadata.microsoft_sub. Pinning a tenant restores
        full email trust for that tenant's users."""
        return os.environ.get("MICROSOFT_SSO_TENANT", self._config.get("MICROSOFT_SSO_TENANT", "common"))

    @property
    def MICROSOFT_SSO_ENABLED(self):
        """Convenience: True when client_id, client_secret, and
        redirect_uri are all configured. The route handlers fail
        closed (404) when this returns False. Tenant defaults to
        ``common`` so it does NOT need to be set to enable the
        feature."""
        return bool(self.MICROSOFT_SSO_CLIENT_ID and self.MICROSOFT_SSO_CLIENT_SECRET and self.MICROSOFT_SSO_REDIRECT_URI)

    @property
    def NLQ_ANTHROPIC_API_KEY(self):
        """Anthropic API key for NLQ queries. Env var takes precedence.

        ``or`` (not ``get(key, default)``) so the empty
        ``ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}`` that compose injects on
        dev/staging falls through to master-config instead of shadowing it
        (the Phase-2 fallback the compose comment promises)."""
        return os.environ.get("ANTHROPIC_API_KEY") or self._config.get("NLQ_ANTHROPIC_API_KEY", "")

    @property
    def SELF_SIGNUP_ENABLED(self):
        """Whether UNINVITED visitors may create accounts (2026-08-06
        incident): the email+password wizard POST and the SSO callbacks'
        provision branch. When False (the default), existing users still
        sign in normally via password or SSO, and invite-carrying visitors
        (?invite=<jwt> / session pending_invite) still complete signup —
        only cold self-registration is refused.

        Default False: production discovered live with open registration
        (any Google/Microsoft account or email could self-provision, and a
        completed wizard grants ai_assistant = Anthropic API spend). Flip
        to true only when the productized signup flow (billing gate) ships.
        Source of truth is the SELF_SIGNUP_ENABLED environment variable,
        passed through by deployment/docker-compose.yml from deployment/.env
        . load_config never copies this key from
        master-config.yaml, so the ``self._config`` lookup below only fires
        when a test injects it; there is no yaml precedence.
        """
        val = self._config.get("SELF_SIGNUP_ENABLED", os.environ.get("SELF_SIGNUP_ENABLED", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def NLQ_MODEL(self):
        """Direct-API model ID for the NLQ "haiku" route."""
        return self._config.get("NLQ_MODEL", "claude-haiku-5-5")

    @property
    def NLQ_ENABLED(self):
        """Whether the NLQ Data Explorer feature is enabled."""
        val = self._config.get("NLQ_ENABLED", os.environ.get("NLQ_ENABLED", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def NLQ_MAX_TOKENS(self):
        """Max tokens for NLQ API responses."""
        return int(self._config.get("NLQ_MAX_TOKENS", 8192))

    @property
    def NLQ_USER_DAILY_COST_CAP_USD(self):
        """Per-user daily estimated-cost cap for Ask Kavanah (USD); 0 disables it."""
        return float(self._config.get("NLQ_USER_DAILY_COST_CAP_USD", 2.0))

    @property
    def NLQ_PROVIDER(self):
        """AI provider: 'anthropic' (direct API) or 'bedrock' (AWS)."""
        return os.environ.get("NLQ_PROVIDER", self._config.get("NLQ_PROVIDER", "anthropic"))

    @property
    def NLQ_AWS_REGION(self):
        """AWS region for Bedrock."""
        return os.environ.get("AWS_DEFAULT_REGION", self._config.get("NLQ_AWS_REGION", "us-west-2"))

    @property
    def NLQ_AWS_PROFILE(self):
        """AWS profile name for Bedrock credentials."""
        return os.environ.get("AWS_PROFILE", self._config.get("NLQ_AWS_PROFILE", ""))

    @property
    def NLQ_AWS_ACCESS_KEY_ID(self):
        """Explicit AWS access key for Bedrock — env-only, no YAML fallback.

        Injected from SSM (/km/<env>/bedrock-nlq/access-key-id) into
        deployment/.env.aws by start-imports-container.sh. When set together
        with NLQ_AWS_SECRET_ACCESS_KEY, it wins over NLQ_AWS_PROFILE.
        Deliberately NOT readable from master-config.yaml (load_config never
        copies it): these keys live in SSM, not in a file on disk — the
         Phase-3 direction. `or ""` guards the empty-env
        shadow case should compose ever inject an empty default."""
        return os.environ.get("NLQ_AWS_ACCESS_KEY_ID") or ""

    @property
    def NLQ_AWS_SECRET_ACCESS_KEY(self):
        """Explicit AWS secret key for Bedrock — env-only, no YAML fallback.

        Injected from SSM (/km/<env>/bedrock-nlq/secret-access-key) into
        deployment/.env.aws by start-imports-container.sh. See
        NLQ_AWS_ACCESS_KEY_ID for why there is no master-config fallback."""
        return os.environ.get("NLQ_AWS_SECRET_ACCESS_KEY") or ""

    @property
    def NLQ_BEDROCK_MODEL_HAIKU(self):
        """Bedrock model ID for Haiku."""
        return self._config.get("NLQ_BEDROCK_MODEL_HAIKU", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    @property
    def NLQ_BEDROCK_MODEL_SONNET(self):
        """Bedrock model ID for Sonnet."""
        return self._config.get("NLQ_BEDROCK_MODEL_SONNET", "global.anthropic.claude-sonnet-4-6")

    @property
    def NLQ_MODEL_SONNET(self):
        """Direct API model ID for Sonnet (old default was a
        nonexistent ID — Sonnet 4.5 name with Sonnet 4's date)."""
        return self._config.get("NLQ_MODEL_SONNET", "claude-haiku-5-5")

    # ── Canvas AI chat ────────────────────────────────────────────
    # Provider + AWS credentials are shared with the NLQ_* settings above
    # (same per-env Bedrock IAM user); these switches are canvas-chat-
    # specific. Tier defaults per the post-IAM-grant re-probe +
    # the private docs 2026-07-14: legacy AnthropicBedrock client,
    # global. inference profiles (Opus 4.8 on us. — the only probe-verified
    # Opus 4.8 profile).

    # (a teammate, 2026-10-22): the canvas-editor AI chat is switched OFF
    # in code for now — AI spend is reserved for the Data Explorer ("using AI
    # to understand the data is the most important usage"). The service,
    # routes, template, tests and config keys all stay in place, parked;
    # this constant is the only thing to flip (plus canvas_ai.enabled in
    # master-config) to bring the feature back. With it True, neither the
    # yaml flag nor the CANVAS_AI_ENABLED env var can turn the feature on:
    # the editor renders no AI button or panel and /api/ai/chat answers 503.
    CANVAS_AI_HARD_OFF = True

    @property
    def CANVAS_AI_ENABLED(self):
        """Whether the canvas-editor AI chat is enabled."""
        if self.CANVAS_AI_HARD_OFF:
            return False
        val = self._config.get("CANVAS_AI_ENABLED", os.environ.get("CANVAS_AI_ENABLED", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def CANVAS_AI_MAX_TOKENS(self):
        """Max output tokens per canvas AI chat turn."""
        return int(self._config.get("CANVAS_AI_MAX_TOKENS", 4096))

    @property
    def CANVAS_AI_EDIT_MAX_TOKENS(self):
        """Max output tokens for alter-dashboard turns — the model
        returns the FULL modified objects list, so these turns need far more
        output headroom than a chat reply."""
        return int(self._config.get("CANVAS_AI_EDIT_MAX_TOKENS", 16384))

    @property
    def CANVAS_AI_MODEL_FAST(self):
        """Fast-tier model (text-only small-edit turns)."""
        return self._config.get("CANVAS_AI_MODEL_FAST", "us.anthropic.claude-haiku-4-5-20251001-v1:0")

    @property
    def CANVAS_AI_MODEL_DEFAULT(self):
        """Default-tier model (attachment or create/alter-intent turns)."""
        return self._config.get("CANVAS_AI_MODEL_DEFAULT", "us.anthropic.claude-sonnet-5")

    @property
    def CANVAS_AI_MODEL_COMPLEX(self):
        """Complex-tier model (opt-in "complex mode" checkbox)."""
        return self._config.get("CANVAS_AI_MODEL_COMPLEX", "us.anthropic.claude-opus-4-8")

    # direct-API (NLQ_PROVIDER == 'anthropic') tier map. The
    # CANVAS_AI_MODEL_* ids above are Bedrock inference profiles, which the
    # direct API rejects, so each provider carries its own defaults and
    # canvas_ai_service.resolve_model picks the set by provider.
    @property
    def CANVAS_AI_ANTHROPIC_MODEL_FAST(self):
        """Fast-tier model on the direct Anthropic API (Haiku 5.5)."""
        return self._config.get("CANVAS_AI_ANTHROPIC_MODEL_FAST", "claude-haiku-5-5")

    @property
    def CANVAS_AI_ANTHROPIC_MODEL_DEFAULT(self):
        """Default-tier model on the direct Anthropic API (Haiku 5.5 — a teammate, 2026-10-22)."""
        return self._config.get("CANVAS_AI_ANTHROPIC_MODEL_DEFAULT", "claude-haiku-5-5")

    @property
    def CANVAS_AI_ANTHROPIC_MODEL_COMPLEX(self):
        """Complex-tier model on the direct Anthropic API (Haiku 5.5 — a teammate, 2026-10-22)."""
        return self._config.get("CANVAS_AI_ANTHROPIC_MODEL_COMPLEX", "claude-haiku-5-5")

    @property
    def CANVAS_AI_USER_DAILY_COST_CAP_USD(self):
        """Per-user daily estimated-cost cap for canvas AI chat (USD)."""
        return float(self._config.get("CANVAS_AI_USER_DAILY_COST_CAP_USD", 2.0))

    @property
    def CANVAS_AI_FEATURE_DAILY_COST_CAP_USD(self):
        """Whole-feature daily estimated-cost cap for canvas AI chat (USD)."""
        return float(self._config.get("CANVAS_AI_FEATURE_DAILY_COST_CAP_USD", 6.0))

    @property
    def JIRA_ENABLED(self):
        """Whether Jira integration is enabled for feedback submissions."""
        val = self._config.get("JIRA_ENABLED", os.environ.get("JIRA_ENABLED", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def JIRA_BASE_URL(self):
        """Jira Cloud instance URL."""
        return os.environ.get("JIRA_BASE_URL", self._config.get("JIRA_BASE_URL", ""))

    @property
    def JIRA_PROJECT_KEY(self):
        """Jira project key for feedback issues."""
        return os.environ.get("JIRA_PROJECT_KEY", self._config.get("JIRA_PROJECT_KEY", ""))

    @property
    def JIRA_USER_EMAIL(self):
        """Email for Jira API authentication."""
        return os.environ.get("JIRA_USER_EMAIL", self._config.get("JIRA_USER_EMAIL", ""))

    @property
    def JIRA_API_TOKEN(self):
        """API token for Jira API authentication.

        ``or`` (not ``get(key, default)``) so the empty
        ``JIRA_API_TOKEN=${JIRA_API_TOKEN:-}`` that compose injects on
        dev/staging falls through to master-config instead of shadowing it."""
        return os.environ.get("JIRA_API_TOKEN") or self._config.get("JIRA_API_TOKEN", "")

    @property
    def ANALYTICS_PLAUSIBLE_DOMAIN(self):
        """Plausible Analytics data-domain. Empty = disabled."""
        return os.environ.get("ANALYTICS_PLAUSIBLE_DOMAIN", "")

    @property
    def ANALYTICS_FATHOM_SITE_ID(self):
        """Fathom Analytics site ID. Empty = disabled."""
        return os.environ.get("ANALYTICS_FATHOM_SITE_ID", "")

    @property
    def ANALYTICS_GA4_ID(self):
        """Google Analytics 4 measurement ID (G-XXXXXXXXXX). Empty = disabled."""
        return os.environ.get("ANALYTICS_GA4_ID", "")

    @property
    def S3_BACKUP_BUCKET(self):
        """S3 bucket holding ES data + ELK config backups.

        Defaults to ``km-wp-backups`` to match all environments today;
        override in master-config.yaml only if a different bucket is
        provisioned. Env var S3_BACKUP_BUCKET takes precedence.
        """
        return os.environ.get("S3_BACKUP_BUCKET", self._config.get("S3_BACKUP_BUCKET", "km-wp-backups"))

    @property
    def S3_BACKUP_PREFIX(self):
        """S3 key prefix under which this environment's backups live.

        Dev: ``elastic-stack-dev``. Prod: ``elastic-stack-prod`` — the same
        prefix km-infra's backup-elk-*.sh write to (; the earlier
        ``…/app`` sub-prefix was never written to, so the Restore page listed
        nothing on prod).
        Must NOT end with a trailing slash — sub-prefixes (``data/``,
        ``config/``) are appended by the listing code. Env var
        S3_BACKUP_PREFIX takes precedence.
        """
        return os.environ.get("S3_BACKUP_PREFIX", self._config.get("S3_BACKUP_PREFIX", "elastic-stack-dev"))

    @property
    def FREE_TIER_SOURCE_LIMIT(self):
        """how many connector tiles a free-tier user can pick
        during the trial. Tiles beyond this position render disabled with
        the "Upgrade to add more sources" tooltip. Default 1 (try one
        live source). Env var ``FREE_TIER_SOURCE_LIMIT`` overrides.
        """
        return int(os.environ.get("FREE_TIER_SOURCE_LIMIT", "1"))

    @property
    def FREE_TIER_TRIAL_DAYS(self):
        """trial window length for the free tier. After this
        many days from the user's first wizard hit the source limit
        drops to 0 and every tile is locked behind the upgrade tooltip.
        Default 14, matching the marketing site's "14-day free trial"
        copy on /pricing. Env var ``FREE_TIER_TRIAL_DAYS`` overrides.
        """
        return int(os.environ.get("FREE_TIER_TRIAL_DAYS", "14"))

    @property
    def JIRA_BUG_ISSUE_TYPE_ID(self):
        """ type ID for Bug Reports."""
        return self._config.get("JIRA_BUG_ISSUE_TYPE_ID", "10089")

    @property
    def JIRA_STORY_ISSUE_TYPE_ID(self):
        """ type ID for Feature Requests (Story)."""
        return self._config.get("JIRA_STORY_ISSUE_TYPE_ID", "10012")

    @property
    def STRIPE_PUBLISHABLE_KEY(self):
        """Stripe publishable (client-safe) key for the Billing
        Payment page's Stripe Elements card capture. Safe to expose to
        the browser. Empty string disables card capture at the template
        layer (the page renders a "payments not configured" notice,
        mirroring the Google/Microsoft SSO empty-key degrade). Env var
        STRIPE_PUBLISHABLE_KEY overrides master-config's
        ``stripe.publishable_key``.

        use ``or`` (not ``get(key, default)``) so an env var that is
        present-but-EMPTY (e.g. a compose ``environment:`` line with no value)
        falls through to master-config instead of shadowing it with ""."""
        return os.environ.get("STRIPE_PUBLISHABLE_KEY") or self._config.get("STRIPE_PUBLISHABLE_KEY", "")

    @property
    def STRIPE_SECRET_KEY(self):
        """Stripe secret key. Server-only — never exposed to the
        template layer. Unused while ``STRIPE_LIVE_PAYMENTS`` is false
        (the create-subscription endpoint stubs out); wired in the
        follow-up that adds real Customer/Subscription creation + the
        webhook handler. Env var overrides master-config's
        ``stripe.secret_key``.

        an empty env var falls through to master-config (see
        STRIPE_PUBLISHABLE_KEY). An empty ``STRIPE_SECRET_KEY=`` injected into
        the container shadowed the populated master-config key here, leaving the
        coupon-apply endpoint stuck on its "unavailable" branch."""
        return os.environ.get("STRIPE_SECRET_KEY") or self._config.get("STRIPE_SECRET_KEY", "")

    @property
    def STRIPE_WEBHOOK_SECRET(self):
        """follow-up: Stripe webhook signing secret (``whsec_...``).
        Server-only. Used by the /billing/stripe/webhook handler to verify
        the ``Stripe-Signature`` header before trusting an event. Empty
        string → the webhook rejects every call (fail-closed: an unverifiable
        webhook must never mutate plan state). Env var overrides
        master-config's ``stripe.webhook_secret``.

        an empty env var falls through to master-config (see
        STRIPE_PUBLISHABLE_KEY)."""
        return os.environ.get("STRIPE_WEBHOOK_SECRET") or self._config.get("STRIPE_WEBHOOK_SECRET", "")

    @property
    def STRIPE_PRICE_BUSINESS(self):
        """Stripe Price ID for the Business plan ($49.95/mo) — the
        subscription the live create-subscription path uses.

        Renamed from STRIPE_PRICE_FORPROFIT / ``stripe.price_forprofit``
        (2026-06-05) to match the Stripe "Business" product. Env var
        STRIPE_PRICE_BUSINESS overrides master-config's
        ``stripe.price_business``. an empty env var falls through to
        master-config (see STRIPE_PUBLISHABLE_KEY)."""
        return os.environ.get("STRIPE_PRICE_BUSINESS") or self._config.get("STRIPE_PRICE_BUSINESS", "")

    @property
    def STRIPE_PRICE_NONPROFIT(self):
        """Stripe Price ID for the nonprofit plan ($20/mo).
        Optional this phase (the summary price is read from
        ``BILLING_NONPROFIT_PRICE_USD``); consumed by the real Stripe
        subscription path in the follow-up. Env var overrides
        master-config's ``stripe.price_nonprofit``. an empty env var
        falls through to master-config (see STRIPE_PUBLISHABLE_KEY)."""
        return os.environ.get("STRIPE_PRICE_NONPROFIT") or self._config.get("STRIPE_PRICE_NONPROFIT", "")

    @property
    def STRIPE_PRICE_ADDON_CONNECTION(self):
        """item 5b: Stripe Price ID for a self-serve add-on data
        connection ($10/mo, per-unit/quantity-able). Added as a second
        subscription item with ``quantity`` = connections beyond the included
        cap; mid-cycle purchases are prorated. Test-mode id created 2026-06-09;
        live id is set in master-config (``stripe.price_addon_connection``)
        before prod. Env var overrides. an empty env var falls through
        to master-config (see STRIPE_PUBLISHABLE_KEY)."""
        return os.environ.get("STRIPE_PRICE_ADDON_CONNECTION") or self._config.get("STRIPE_PRICE_ADDON_CONNECTION", "")

    @property
    def STRIPE_LIVE_PAYMENTS(self):
        """feature flag — "actually call Stripe" vs "stub".

        NAME CAVEAT (read this): ``live_payments`` does NOT mean
        "production/live-mode Stripe". It means: **make real Stripe API
        calls instead of stubbing them.** Whether those calls hit Stripe's
        *test* environment or *live* (real-money) environment is decided
        ENTIRELY by which keys are configured — a ``sk_test_...`` secret key
        → test mode (fake money, Stripe's test cards like 4242…), a
        ``sk_live_...`` key → real money. The two concepts are independent.

        Truth table::

            live_payments=false, any keys   → STUB: no Stripe call, returns
                                              {"status":"stub"} 200, no plan
                                              change (front-end/Elements flow
                                              only).
            live_payments=true,  empty keys → 503 "not fully configured".
            live_payments=true,  sk_test_…  → REAL test-mode call; the 4242
                                              test card SUCCEEDS, the webhook
                                              fires, the plan activates. This
                                              is the correct way to validate
                                              end-to-end — it does NOT 503.
            live_payments=true,  sk_live_…  → real money.

        So to TEST the live path on staging: paste sk_test_/whsec_/price_
        AND set live_payments=true (keys first, flag last — flipping the
        flag with empty keys is the only thing that 503s). Env var
        STRIPE_LIVE_PAYMENTS overrides master-config's
        ``stripe.live_payments``."""
        val = self._config.get("STRIPE_LIVE_PAYMENTS", os.environ.get("STRIPE_LIVE_PAYMENTS", "false"))
        if isinstance(val, bool):
            return val
        return str(val).lower() == "true"

    @property
    def STRIPE_ENABLED(self):
        """convenience — True when a publishable key is set, i.e.
        the Payment page can mount Stripe Elements. The page falls back to
        a "payments not configured" notice when False. Note this is
        independent of ``STRIPE_LIVE_PAYMENTS``: a test publishable key
        makes the page interactive while the backend stays stubbed."""
        return bool(self.STRIPE_PUBLISHABLE_KEY)

    @property
    def BILLING_BUSINESS_PRICE_USD(self):
        """Business plan price in USD/month shown in the Payment page
        subscription summary. Default 49.95 per epic.

        Renamed from BILLING_FORPROFIT_PRICE_USD (2026-06-05). Env var
        BILLING_BUSINESS_PRICE_USD overrides master-config's
        ``BILLING_BUSINESS_PRICE_USD``."""
        raw = os.environ.get("BILLING_BUSINESS_PRICE_USD", str(self._config.get("BILLING_BUSINESS_PRICE_USD", "49.95")))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 49.95

    @property
    def BILLING_NONPROFIT_PRICE_USD(self):
        """nonprofit plan price in USD/month shown in the Plans
        comparison and the Payment page summary. Default 20.00 per epic
        . Env var BILLING_NONPROFIT_PRICE_USD overrides. Kept here
        (not hardcoded in the template/route) so the comparison card, the
        payment summary, and later the real Stripe amount share one source
        of truth."""
        raw = os.environ.get("BILLING_NONPROFIT_PRICE_USD", str(self._config.get("BILLING_NONPROFIT_PRICE_USD", "20.00")))
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 20.00


# Singleton instance
config = Config()

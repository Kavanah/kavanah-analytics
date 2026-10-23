"""
Kavanah Analytics Password Reset App
Allows users to reset their Elasticsearch passwords via email link.

Security features:
- Rate limiting (5 requests per hour per username)
- Audit logging
- bcrypt password hashing for admin
"""

import os
import json
import hashlib
import secrets
import smtplib
import logging
from contextlib import contextmanager
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone
from functools import wraps

# fcntl gives us POSIX advisory file locking (prod is Linux; macOS dev has it
# too). It is absent on Windows — guard the import so the module still loads
# there. Without a real lock the read-modify-write below is still consolidated
# into a single function (closing the in-process TOCTOU); only the
# cross-gunicorn-worker race relies on flock, which only exists on prod (Linux).
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows dev only
    fcntl = None  # type: ignore[assignment]
from flask import Flask, render_template, request, redirect, url_for, flash, session, g
import requests
from requests.auth import HTTPBasicAuth
import urllib3

# bcrypt is required — listed in requirements.txt. The previous SHA256
# fallback was insecure (no salt, fast hash) and is removed (,
# CodeQL py/weak-sensitive-data-hashing).
import bcrypt

# Disable SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)


def _stable_secret_key() -> str:
    """a signing key every gunicorn worker agrees on.

    The old ``secrets.token_hex(32)`` fallback minted a DIFFERENT random key
    per worker (this app runs with --workers 2 and APP_SECRET_KEY is unset in
    the container), so a session cookie signed by worker A failed verification
    when the next request round-robined to worker B: admin login silently
    bounced back to /admin/login until retries happened to stick to one
    worker. Resolution order:

    1. ``APP_SECRET_KEY`` env (explicit deployment choice);
    2. the shared master-config ``asset_web.flask_secret_key`` — the same
       secret the DEAM app signs with — domain-separated through SHA-256 so
       the two apps never share a literal signing key;
    3. last resort: a per-process random key, with a loud warning (dev-only
       shape; sessions will drop across workers/restarts).
    """
    env_key = os.environ.get("APP_SECRET_KEY")
    if env_key:
        return env_key
    # Prod reality (verified 2026-09-01): DEAM's signing key arrives via the
    # SECRET_KEY env var and master-config's flask_secret_key is EMPTY, so the
    # env var must be a derivation source too — same domain separation.
    deam_env = (os.environ.get("SECRET_KEY") or "").strip()
    if deam_env:
        return hashlib.sha256(f"password-reset:{deam_env}".encode()).hexdigest()
    try:
        import yaml

        with open("/app/reporting_configs/master-config.yaml", "r") as fh:
            master = yaml.safe_load(fh) or {}
        yaml_secret = ((master.get("asset_web") or {}).get("flask_secret_key") or "").strip()
        if yaml_secret:
            return hashlib.sha256(f"password-reset:{yaml_secret}".encode()).hexdigest()
    except Exception as e:  # noqa: BLE001 — config read is best-effort; fall through
        logging.warning("could not derive secret key from master-config: %s", e)
    logging.warning(
        "no APP_SECRET_KEY and no master-config flask_secret_key — using a "
        "per-process random key; admin sessions will NOT survive worker round-robin."
    )
    return secrets.token_hex(32)


app.secret_key = _stable_secret_key()

# follow-up: scope this app's session cookie to /reset-password and
# give it a unique name. The default Flask cookie is named "session" at path
# "/", which collides with the DEAM app's cookie of the same name on the
# same host. When a user touched both apps in the same browser the second
# app to set the cookie clobbered the first, producing intermittent CSRF
# 403s on the password-reset form: GET rendered csrf_token from session A,
# POST arrived with session B, secrets.compare_digest fails. The bfcache
# fix (Cache-Control: no-store) masked this most of the time by forcing
# fresh GETs, but didn't eliminate it. Two scoping attributes prevent
# collision permanently.
app.config["SESSION_COOKIE_NAME"] = "pw_reset_session"
app.config["SESSION_COOKIE_PATH"] = "/reset-password"


# Handle reverse proxy prefix (/reset-password/)
class PrefixMiddleware:
    def __init__(self, wsgi_app, prefix=""):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        if environ["PATH_INFO"].startswith(self.prefix):
            environ["PATH_INFO"] = environ["PATH_INFO"][len(self.prefix) :]
            environ["SCRIPT_NAME"] = self.prefix
        return self.wsgi_app(environ, start_response)


# Apply prefix middleware
app.wsgi_app = PrefixMiddleware(app.wsgi_app, prefix="/reset-password")

# Paths
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
TOKENS_FILE = os.path.join(CONFIG_DIR, "tokens.json")
RATE_LIMIT_FILE = os.path.join(CONFIG_DIR, "rate_limits.json")
# PW_RESET_AUDIT_LOG lets the pytest fixture redirect the audit log to a
# tmp file so module import does not hard-fail when the on-disk audit.log
# is owned by root (which happens when the prod container has written to
# the volume-mounted path).
AUDIT_LOG_FILE = os.environ.get("PW_RESET_AUDIT_LOG", os.path.join(CONFIG_DIR, "audit.log"))

# Ensure config directory exists
os.makedirs(CONFIG_DIR, exist_ok=True)

# =============================================================================
# Audit Logging
# =============================================================================

# Setup audit logger
audit_logger = logging.getLogger("audit")
audit_logger.setLevel(logging.INFO)

# File handler for audit log
audit_handler = logging.FileHandler(AUDIT_LOG_FILE)
audit_handler.setLevel(logging.INFO)
audit_format = logging.Formatter("%(asctime)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
audit_handler.setFormatter(audit_format)
audit_logger.addHandler(audit_handler)

# Phase 3: scrub CR/LF/NUL from every log record. Run after the
# audit handler is attached so the filter applies to it.
try:
    from lib.log_filters import install_filter_on_all_handlers
except ImportError:
    # password_reset can run standalone without the lib path on sys.path;
    # in that case fall back to a no-op rather than crashing the import.
    def install_filter_on_all_handlers() -> None:  # type: ignore[no-redef]
        return


install_filter_on_all_handlers()

# the SMTP hop must VERIFY the relay (CERT_REQUIRED + hostname) and
# time out. Same standalone fallback as log_filters above — and the fallback is
# the same secure policy, never a downgrade.
try:
    from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
except ImportError:
    import ssl as _ssl

    SMTP_TIMEOUT_SECONDS = 30

    def smtp_ssl_context() -> "_ssl.SSLContext":  # type: ignore[no-redef]
        return _ssl.create_default_context()


def _write_to_es_audit_log(event, username=None, ip=None, details=None):
    """Write an audit event to the de_audit_log Elasticsearch index.

    This mirrors the event into the same index that the User Management
    Login Activity tab reads from.  Fire-and-forget — failures are logged
    but never raised.
    """
    es = get_es_config()
    if not es["password"]:
        return

    # Map password-reset events to action names the audit UI understands
    action_map = {
        "RESET_REQUEST": "password_reset_request",
        "RESET_EMAIL_SENT": "password_reset_email_sent",
        "RESET_EMAIL_FAILED": "password_reset_email_failed",
        "PASSWORD_RESET_SUCCESS": "password_reset_success",
        "PASSWORD_RESET_FAILED": "password_reset_failed",
        "RATE_LIMIT_EXCEEDED": "password_reset_rate_limited",
        "INVALID_TOKEN": "password_reset_invalid_token",
    }
    action = action_map.get(event)
    if not action:
        return  # Only mirror user-facing events, not admin actions

    success = event in ("RESET_EMAIL_SENT", "PASSWORD_RESET_SUCCESS")

    doc = {
        "@timestamp": datetime.utcnow().isoformat() + "Z",
        "username": username or "",
        "action": action,
        "success": success,
        "ip_address": ip or "",
        "is_scanner": False,
        "suspicious": False,
    }
    if details:
        doc["detail"] = details

    try:
        _es_session().post(
            f"{es['host']}/de_audit_log/_doc",
            auth=HTTPBasicAuth(es["user"], es["password"]),
            json=doc,
            timeout=5,
        )
    except Exception as e:
        logging.error(  # nosemgrep: python-logger-credential-disclosure
            "password_reset: failed to write to de_audit_log: %s", e
        )


def audit_log(event, username=None, ip=None, details=None):
    """Log an audit event."""
    ip = ip or request.remote_addr if request else "unknown"
    msg_parts = [f"EVENT={event}"]
    if username:
        msg_parts.append(f"USER={username}")
    msg_parts.append(f"IP={ip}")
    if details:
        msg_parts.append(f"DETAILS={details}")
    audit_logger.info(" | ".join(msg_parts))

    # Also write to ES audit index so it shows in User Management Login Activity
    _write_to_es_audit_log(event, username=username, ip=ip, details=details)


# =============================================================================
# Rate Limiting
# =============================================================================

RATE_LIMIT_MAX_REQUESTS = 5  # Max requests per window
RATE_LIMIT_WINDOW_HOURS = 1  # Time window in hours


def rate_limit_key(user_input):
    """Canonical rate-limit bucket for a username-or-email input.

    The limiter previously keyed on the raw form value, so " Alice " and
    "alice" (and assorted casings) fell into different buckets and each got a
    fresh 5/hour allowance. Strip + lowercase collapses those into one key.
    Email vs. username for the *same* account still differ (we can't resolve
    one to the other without an ES lookup that would itself leak existence and
    run before the rate-limit gate), but each individual identifier is now
    counted consistently rather than per-casing.
    """
    return (user_input or "").strip().lower()


@contextmanager
def _locked_rate_limits():
    """Open rate_limits.json under an exclusive advisory lock and yield the
    parsed dict; whatever the caller mutates is written back before the lock
    releases.

    This makes the prune -> check -> record sequence a single atomic
    critical section. The previous code did load/check/save and a *separate*
    load/append/save, so two concurrent gunicorn workers could each read the
    same count, both decide a request was allowed, and both append — losing
    increments and letting callers exceed the 5/hour cap (TOCTOU).
    """
    # "a+" creates the file if missing and lets us both read and rewrite it
    # without truncating before we hold the lock.
    with open(RATE_LIMIT_FILE, "a+") as f:
        if fcntl is not None:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                data = {}

            yield data

            f.seek(0)
            f.truncate()
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def check_and_record_rate_limit(user_input):
    """Atomically prune the window, decide if a request is allowed, and record
    it if so. Returns (allowed, remaining, reset_time).

    A rejected request is NOT recorded (it does not extend the window). This
    replaces the old check_rate_limit + record_rate_limit_request pair, whose
    split read-modify-write was the TOCTOU race.
    """
    now = datetime.now()
    window_start = now - timedelta(hours=RATE_LIMIT_WINDOW_HOURS)
    user_key = rate_limit_key(user_input)

    with _locked_rate_limits() as data:
        valid_requests = [ts for ts in data.get(user_key, []) if datetime.fromisoformat(ts) > window_start]

        if len(valid_requests) >= RATE_LIMIT_MAX_REQUESTS:
            data[user_key] = valid_requests  # persist the prune
            oldest = min(datetime.fromisoformat(ts) for ts in valid_requests)
            reset_time = oldest + timedelta(hours=RATE_LIMIT_WINDOW_HOURS)
            return False, 0, reset_time

        valid_requests.append(now.isoformat())
        data[user_key] = valid_requests
        remaining = RATE_LIMIT_MAX_REQUESTS - len(valid_requests)
        return True, remaining, None


def cleanup_rate_limits():
    """Remove expired rate limit entries (runs under the same lock)."""
    now = datetime.now()
    window_start = now - timedelta(hours=RATE_LIMIT_WINDOW_HOURS)

    with _locked_rate_limits() as data:
        for user_key in list(data.keys()):
            valid = [ts for ts in data[user_key] if datetime.fromisoformat(ts) > window_start]
            if valid:
                data[user_key] = valid
            else:
                del data[user_key]


# =============================================================================
# Configuration Management
# =============================================================================


# SMTP credentials are environment-managed. The env vars are
# injected from SSM (/km/<env>/deam/pw-reset-smtp/*) into deployment/.env.aws
# by start-imports-container.sh; compose's env_file: delivers them to the
# container. When an env var is set it wins over settings.json, the admin UI
# renders the field read-only, and save_settings() never writes it to disk —
# so the plaintext value ages out of settings.json on the first save.
_SMTP_ENV_OVERRIDES = {
    "smtp_host": "PW_RESET_SMTP_HOST",
    "smtp_port": "PW_RESET_SMTP_PORT",
    "smtp_username": "PW_RESET_SMTP_USERNAME",
    "smtp_password": "PW_RESET_SMTP_PASSWORD",
}


def env_managed_smtp_fields():
    """Setting names whose values come from the environment."""
    return {field for field, var in _SMTP_ENV_OVERRIDES.items() if os.environ.get(var)}


def get_settings():
    """Load settings from file (with env overrides) or return defaults."""
    defaults = {
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_username": "",
        "smtp_password": "",
        "smtp_use_tls": True,
        "from_email": "",
        "admin_contact": "",
        "token_expiry_minutes": 60,
        "app_url": "",
        "admin_password_hash": "",
    }

    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
                defaults.update(saved)
        except Exception as e:
            print(f"Error loading settings: {e}")

    # environment-managed SMTP credentials win over the file.
    for field, var in _SMTP_ENV_OVERRIDES.items():
        val = os.environ.get(var)
        if not val:
            continue
        if field == "smtp_port":
            try:
                defaults[field] = int(val)
            except ValueError:
                print(f"Ignoring non-numeric {var}")
                continue
        else:
            defaults[field] = val

    return defaults


def save_settings(settings):
    """Save settings to file.

    environment-managed SMTP fields are stripped before writing, so
    credentials never land in settings.json while the env vars are set. The
    fields that remain in the file (from_email, app_url, admin_password_hash
    — a hash, not a secret — etc.) are non-credentials.
    """
    on_disk = {k: v for k, v in settings.items() if k not in env_managed_smtp_fields()}
    with open(SETTINGS_FILE, "w") as f:
        json.dump(on_disk, f, indent=2)


def is_configured():
    """Check if SMTP settings are configured."""
    settings = get_settings()
    return all(
        [
            settings.get("smtp_host"),
            settings.get("smtp_username"),
            settings.get("smtp_password"),
            settings.get("from_email"),
        ]
    )


# =============================================================================
# Token Management
# =============================================================================


def load_tokens():
    """Load tokens from file."""
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_tokens(tokens):
    """Save tokens to file."""
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f, indent=2)


def _hash_token(token):
    """Return the sha256 hex digest used as the at-rest key for a token.

    tokens.json lives on the config volume; storing the raw
    secrets.token_urlsafe value meant anyone with read access to that file
    could replay an unexpired link. We persist only sha256(token) and look up
    by hash, so the file never contains a usable token. The raw token exists
    only in the emailed reset link. (Plain sha256 — no salt/stretching — is
    appropriate here: a 256-bit url-safe token has no guessable structure to
    brute-force, unlike a human password.)
    """
    return hashlib.sha256(token.encode()).hexdigest()


def create_token(username, email):
    """Create a password reset token. Returns the raw token (for the email link);
    only its hash is persisted."""
    token = secrets.token_urlsafe(32)
    settings = get_settings()
    expiry = datetime.now() + timedelta(minutes=settings.get("token_expiry_minutes", 60))

    tokens = load_tokens()
    tokens[_hash_token(token)] = {
        "username": username,
        "email": email,
        "expiry": expiry.isoformat(),
        "used": False,
    }
    save_tokens(tokens)

    return token


def validate_token(token):
    """Validate a token and return username if valid."""
    tokens = load_tokens()
    token_hash = _hash_token(token)

    if token_hash not in tokens:
        return None, "Invalid or expired reset link."

    token_data = tokens[token_hash]

    if token_data.get("used"):
        return None, "This reset link has already been used."

    expiry = datetime.fromisoformat(token_data["expiry"])
    if datetime.now() > expiry:
        return None, "This reset link has expired. Please request a new one."

    return token_data["username"], None


def mark_token_used(token):
    """Mark a token as used."""
    tokens = load_tokens()
    token_hash = _hash_token(token)
    if token_hash in tokens:
        tokens[token_hash]["used"] = True
        save_tokens(tokens)


def cleanup_expired_tokens():
    """Remove expired tokens."""
    tokens = load_tokens()
    now = datetime.now()

    valid_tokens = {k: v for k, v in tokens.items() if datetime.fromisoformat(v["expiry"]) > now and not v.get("used")}

    if len(valid_tokens) != len(tokens):
        save_tokens(valid_tokens)


# =============================================================================
# Elasticsearch Integration
# =============================================================================


def get_es_config():
    """Get Elasticsearch connection config from environment."""
    host = os.environ.get("ES_HOST")
    if not host:
        ip = os.environ.get("ES01_INTERNAL_IP", "172.18.0.2")
        host = f"https://{ip}:9200"
    return {
        "host": host,
        "user": os.environ.get("ES_USER", "elastic"),
        "password": os.environ.get("ES01_PASSWORD", os.environ.get("ES_PASS", "")),
    }


# (SVC-004 / PWRESET-001): the ES calls in this module previously passed
# verify=False, disabling TLS verification on requests that carry password changes
# and audit writes — a MITM on the app<->ES hop could intercept/alter them. They
# talk to ES DIRECTLY over its self-signed CA (es01_http_ca.crt), which trips
# Python 3.13's strict X.509 validation, so a plain verify=<ca_path> would fail.
# Mirror lib.es_helpers.get_elasticsearch_client: verify against the pinned
# internal CA with strict X.509 relaxed (CA + hostname checks still enforced),
# carried on a Session whose HTTPS adapter holds a custom SSL context.
#
# (analytics.routes._kibana_tls_verify can use a plain verify=<path> because the
# Kibana login goes through a public CA-issued cert; this direct-to-ES path can't.)
_ES_CA_CERT_PATH = os.environ.get("ES_CA_CERT", "/app/data/certs/es01_http_ca.crt")

_es_session_cache = None


def _build_es_session():
    """Build a requests.Session that verifies ES TLS against the pinned internal
    CA with strict X.509 relaxed (matching lib.es_helpers).

    Escape hatch: ES_TLS_INSECURE=1 restores the old verify=False behavior for an
    environment that genuinely needs it (internal IP, self-signed cert, no mounted
    CA). It is logged loudly so the insecure posture is never silent.
    """
    import ssl
    from requests.adapters import HTTPAdapter

    session = requests.Session()

    if os.environ.get("ES_TLS_INSECURE") == "1":
        logging.warning("ES_TLS_INSECURE=1 — password-reset ES TLS verification is DISABLED")
        session.verify = False
        return session

    ctx = ssl.create_default_context()
    # The ES-generated cert trips Python 3.13's strict RFC-5280 checks; relax only
    # that extra validation. CA trust and hostname verification stay enforced.
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    if os.path.exists(_ES_CA_CERT_PATH):
        ctx.load_verify_locations(cafile=_ES_CA_CERT_PATH)
    else:
        logging.warning(
            "ES CA cert not found at %s — verifying against the system trust store",
            _ES_CA_CERT_PATH,
        )

    class _SSLContextAdapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["ssl_context"] = ctx
            return super().init_poolmanager(*args, **kwargs)

        def proxy_manager_for(self, *args, **kwargs):
            kwargs["ssl_context"] = ctx
            return super().proxy_manager_for(*args, **kwargs)

    session.mount("https://", _SSLContextAdapter())
    return session


def _es_session():
    """Lazily-built, cached CA-pinned ES requests session (see _build_es_session)."""
    global _es_session_cache
    if _es_session_cache is None:
        _es_session_cache = _build_es_session()
    return _es_session_cache


# ES usernames may include letters, digits, '@', '.', '_', '-', '+',
# but not URL path separators. Reject anything else before interpolating into
# an ES Security API path so a value like "../config/_cluster" can't redirect
# the request (CodeQL py/partial-ssrf).
import re as _re

_ES_USERNAME_RE = _re.compile(r"[A-Za-z0-9._@+\-]{1,256}")


def _is_safe_es_username(username):
    # fullmatch, not match + "$" ("$" also matches before a trailing
    # newline), and no "." / ".." path segments — "..", or a name made only of
    # dots, would still walk the /_security/user/<name> path.
    return (
        isinstance(username, str)
        and _ES_USERNAME_RE.fullmatch(username) is not None
        and ".." not in username
        and username.strip(".") != ""
    )


def get_user_email(username):
    """Get a user's email from Elasticsearch."""
    if not _is_safe_es_username(username):
        return None
    es = get_es_config()

    try:
        response = _es_session().get(
            f"{es['host']}/_security/user/{username}",
            auth=HTTPBasicAuth(es["user"], es["password"]),
            timeout=10,
        )

        if response.status_code == 200:
            data = response.json()
            if username in data:
                return data[username].get("email", "")

        return None
    except Exception as e:
        print(f"Error getting user email: {e}")
        return None


def user_exists(username):
    """Check if a user exists in Elasticsearch."""
    if not _is_safe_es_username(username):
        return False
    es = get_es_config()

    try:
        response = _es_session().get(
            f"{es['host']}/_security/user/{username}",
            auth=HTTPBasicAuth(es["user"], es["password"]),
            timeout=10,
        )
        return response.status_code == 200
    except Exception:
        return False


def find_user_by_email(email):
    """Find a username by email address. Returns (username, email) or (None, None)."""
    es = get_es_config()

    try:
        # Get all users
        response = _es_session().get(
            f"{es['host']}/_security/user",
            auth=HTTPBasicAuth(es["user"], es["password"]),
            timeout=10,
        )

        if response.status_code == 200:
            users = response.json()
            email_lower = email.lower()

            for username, user_data in users.items():
                user_email = user_data.get("email", "")
                if user_email and user_email.lower() == email_lower:
                    return username, user_email

        return None, None
    except Exception as e:
        print(f"Error finding user by email: {e}")
        return None, None


def change_password(username, new_password):
    """Change a user's password in Elasticsearch."""
    if not _is_safe_es_username(username):
        return False
    es = get_es_config()

    try:
        response = _es_session().post(
            f"{es['host']}/_security/user/{username}/_password",
            auth=HTTPBasicAuth(es["user"], es["password"]),
            json={"password": new_password},
            timeout=10,
        )
        return response.status_code == 200
    except Exception as e:
        print(f"Error changing password: {e}")
        return False


def clear_force_password_change(username):
    """Clear the force_password_change flag from a user's ES metadata.

    This ensures that users who reset their password via the forgot-password
    flow are not prompted to change a temporary password on next login.

    Uses the same ES Python client as the rest of the system to avoid
    discrepancies with raw requests.

    this is also where this standalone app stamps
    ``metadata.credential_changed_at`` — unconditionally, not only when
    ``force_password_change`` was set, since a real password change (this
    function's one caller, right after ``change_password`` succeeds) always
    needs the stamp regardless of that flag's prior state. The stamp lets
    the main DEAM app's ``web.auth.session_denial_reason`` / ``verify_auth``
    deny any live session that predates this reset, the same as the other
     writers. Unlike those, THIS process cannot flush DEAM's
    per-worker account-state cache (separate process, separate deployment)
    or revoke the user's Kibana gate key (that lives in DEAM's own ES index,
    not reachable from here) — so the deny rides the full 30s account-state
    cache TTL, and any live Kibana gate key rides its own
    ``KIBANA_GATE_KEY_TTL`` expiry, same as before this ticket.
    """
    try:
        import sys

        if "/app/python" not in sys.path:
            sys.path.insert(0, "/app/python")
        from lib.es_helpers import get_elasticsearch_client, get_es_admin_client

        es = get_elasticsearch_client()
        if es is None:
            logging.error("clear_force_password_change(%s): Elasticsearch unavailable", username)
            return False
        # the WRITE goes through the admin client — the shared
        # client (Role B) loses manage_security option 2a. With no
        # ES_ADMIN_API_KEY this is the same shared client as before.
        admin = get_es_admin_client(es)
        if admin is None:
            logging.error("clear_force_password_change(%s): ES admin client unavailable", username)
            return False

        response = es.security.get_user(username=username)
        user_data = dict(response).get(username, {})
        metadata = dict(user_data.get("metadata", {}))

        # ES 9.x merges metadata on PUT — keys can't be removed, only overwritten.
        # Set to False instead of popping the key.
        metadata["force_password_change"] = False
        metadata["credential_changed_at"] = datetime.now(timezone.utc).isoformat()

        # ES writes enabled=true when a put_user omits it, so carry the stored
        # value. Without this a suspended user (enabled=false) who
        # resets their own password from the public page re-enables the account.
        admin.security.put_user(
            username=username,
            roles=user_data.get("roles", []),
            full_name=user_data.get("full_name", ""),
            email=user_data.get("email", ""),
            enabled=user_data.get("enabled", True),
            metadata=metadata,
        )
        audit_log("CLEAR_FORCE_PASSWORD_CHANGE", username=username)
        return True
    except Exception as e:  # noqa: BLE001 — the caller audits the failure
        logging.error("clear_force_password_change(%s) failed: %s", username, e)
        return False


# =============================================================================
# Email Functions
# =============================================================================


def mask_email(email):
    """Mask an email for display (e.g., m****s@example.com)."""
    if not email or "@" not in email:
        return email

    local, domain = email.split("@")
    if len(local) <= 2:
        masked_local = local[0] + "*" * (len(local) - 1)
    else:
        masked_local = local[0] + "*" * (len(local) - 2) + local[-1]

    return f"{masked_local}@{domain}"


def send_reset_email(email, username, token):
    """Send password reset email via SMTP."""
    settings = get_settings()

    app_url = settings.get("app_url", "").rstrip("/")
    reset_link = f"{app_url}/reset/{token}"
    logo_url = f"{app_url}/static/kavanah-analytics-logo.svg"
    expiry_minutes = settings.get("token_expiry_minutes", 60)

    subject = "Password Reset Request"

    html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>{subject}</title>
</head>
<body style="margin:0;padding:0;background-color:#F1F3F3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#000716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F3F3;">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;border:1px solid #e9ebed;">
      <tr><td style="background-color:#232F3E;padding:16px 24px;border-radius:8px 8px 0 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
          <td style="vertical-align:middle;width:32px;"><img src="{logo_url}" width="28" height="28" alt="" style="display:block;border:0;"></td>
          <td style="vertical-align:middle;padding-left:10px;color:#ffffff;font-size:17px;font-weight:700;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Kavanah Analytics</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:32px 28px;background-color:#ffffff;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">Password Reset Request</h2>
        <p style="margin:0 0 12px;">Hello,</p>
        <p style="margin:0 0 12px;">We received a request to reset the password for your account: <strong>{username}</strong></p>
        <p style="margin:0 0 8px;">Click the button below to reset your password:</p>
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr>
          <td style="border-radius:6px;background-color:#0972D3;">
            <a href="{reset_link}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">Reset Password</a>
          </td>
        </tr></table>
        <p style="margin:16px 0 4px;font-size:13px;color:#5F6B7A;">Or copy and paste this link into your browser:</p>
        <p style="margin:0 0 16px;"><a href="{reset_link}" style="color:#0972D3;word-break:break-all;">{reset_link}</a></p>
        <p style="margin:0 0 8px;font-size:13px;color:#5F6B7A;">This link will expire in {expiry_minutes} minutes.</p>
        <p style="margin:0;font-size:13px;color:#5F6B7A;">If you didn't request this password reset, please ignore this email.</p>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        This is an automated message — please do not reply.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

    text_body = f"""
    Password Reset Request

    Hello,

    We received a request to reset the password for your account: {username}

    Click the link below to reset your password:
    {reset_link}

    This link will expire in {settings.get('token_expiry_minutes', 60)} minutes.

    If you didn't request this password reset, please ignore this email.
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings["from_email"]
    msg["To"] = email

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if settings.get("smtp_use_tls", True):
            server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
            server.starttls(context=smtp_ssl_context())
        else:
            server = smtplib.SMTP_SSL(
                settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
            )

        server.login(settings["smtp_username"], settings["smtp_password"])
        server.sendmail(settings["from_email"], email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False


# =============================================================================
# Admin Authentication
# =============================================================================


def hash_password(password):
    """Hash a password for storage using bcrypt."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password, stored_hash):
    """Verify a password against a bcrypt-stored hash.

    earlier hashes were SHA256 (no salt). Anyone still holding such a
    hash should re-set their password; verify_password rejects non-bcrypt
    hashes outright so the SHA256 path is closed.
    """
    if not stored_hash or not stored_hash.startswith("$2"):
        return False
    try:
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    except Exception:
        return False


# =============================================================================
# Password Policy
# =============================================================================

# Raised from the old 8-char floor. NIST 800-63B leans on length + a
# blocklist rather than rigid composition rules, so we require a 12-char
# minimum, a light two-character-class check (rejects "aaaaaaaaaaaa" /
# "111111111111"), reject the username as a substring, and screen against a
# small embedded list of common/breached passwords that survive the length
# floor. This is an offline stand-in for an HIBP k-anonymity call — chosen to
# keep the reset hot path free of an outbound network dependency. See
# DECISIONS.md.
MIN_PASSWORD_LENGTH = 12

# Common/weak passwords that are >=12 chars and would otherwise pass the floor.
# Compared case-insensitively. Short classics ("password", "qwerty") are
# already excluded by the length minimum.
_COMMON_PASSWORDS = frozenset(
    {
        "123456789012",
        "1234567890123",
        "12345678901234",
        "123456789012345",
        "1234567890123456",
        "passwordpassword",
        "password123456",
        "password1234",
        "qwertyuiop12",
        "qwertyui12345",
        "iloveyou12345",
        "letmein123456",
        "adminadmin123",
        "welcome123456",
        "abcdefghijkl",
        "aaaaaaaaaaaa",
        "111111111111",
        "qwertyuiopas",
        "1q2w3e4r5t6y",
        "zaq12wsxcde3",
        "changeme1234",
        "trustno1trustno1",
        "monkeymonkey12",
        "football12345",
        "baseball12345",
        "superman12345",
    }
)


def validate_password_strength(password, username=None):
    """Return None if the password is acceptable, else an error message.

    Enforces a 12-char minimum, at least two character classes, no username
    substring, and rejects an embedded list of common/breached passwords.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."

    classes = sum(
        [
            any(c.islower() for c in password),
            any(c.isupper() for c in password),
            any(c.isdigit() for c in password),
            any(not c.isalnum() for c in password),
        ]
    )
    if classes < 2:
        return "Password must include at least two of: lowercase, uppercase, numbers, symbols."

    if password.lower() in _COMMON_PASSWORDS:
        return "This password is too common. Please choose a less predictable password."

    if username and len(username) >= 3 and username.lower() in password.lower():
        return "Password must not contain your username."

    return None


def admin_required(f):
    """Decorator to require admin authentication."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return decorated_function


# =============================================================================
# User Routes
# =============================================================================


@app.route("/")
def index():
    """Redirect to request reset page."""
    return redirect(url_for("request_reset"))


@app.route("/request", methods=["GET", "POST"])
def request_reset():
    """Handle password reset requests."""
    settings = get_settings()

    if request.method == "POST":
        user_input = request.form.get("username", "").strip()

        if not user_input:
            flash("Please enter your username or email.", "error")
            return render_template("request_reset.html", settings=settings)

        # Determine if input is email or username
        is_email_input = "@" in user_input

        # Check rate limit FIRST (before revealing if user exists). The check
        # and the record now happen in one atomic, file-locked step so
        # concurrent requests can't both slip past the cap. A
        # request is counted even if the user doesn't exist (no enumeration via
        # differential rate-limiting); an over-limit request is not recorded.
        allowed, remaining, reset_time = check_and_record_rate_limit(user_input)
        if not allowed:
            audit_log("RATE_LIMIT_EXCEEDED", username=user_input)
            reset_str = reset_time.strftime("%H:%M") if reset_time else "soon"
            flash(f"Too many reset requests. Please try again after {reset_str}.", "error")
            return render_template("request_reset.html", settings=settings)

        # Check if app is configured
        if not is_configured():
            flash(
                "Password reset is not configured. Please contact the administrator.",
                "error",
            )
            return render_template("request_reset.html", settings=settings)

        # Look up user by email or username
        if is_email_input:
            # Search for user by email
            username, email = find_user_by_email(user_input)
            if not username:
                # Don't reveal if user exists or not (security)
                audit_log("RESET_REQUEST", username=user_input, details="email_not_found")
                flash(
                    "If this account exists and has an email on file, a reset link has been sent.",
                    "success",
                )
                return render_template("request_reset.html", settings=settings)
        else:
            # Look up by username
            username = user_input
            if not user_exists(username):
                # Don't reveal if user exists or not (security)
                audit_log("RESET_REQUEST", username=username, details="user_not_found")
                flash(
                    "If this account exists and has an email on file, a reset link has been sent.",
                    "success",
                )
                return render_template("request_reset.html", settings=settings)

            # Get user's email
            email = get_user_email(username)

        if not email:
            audit_log("RESET_REQUEST", username=username, details="no_email_on_file")
            flash(
                f'No email address on file for this account. Please contact the administrator at {settings.get("admin_contact", "your system administrator")}.',
                "warning",
            )
            return render_template("request_reset.html", settings=settings)

        # Create token and send email
        token = create_token(username, email)

        if send_reset_email(email, username, token):
            masked = mask_email(email)
            audit_log("RESET_EMAIL_SENT", username=username, details=f"sent_to={masked}")
            flash(
                f"Password reset link sent to {masked}. Please check your email.",
                "success",
            )
        else:
            audit_log("RESET_EMAIL_FAILED", username=username, details="smtp_error")
            flash(
                "Error sending email. Please try again later or contact the administrator.",
                "error",
            )

        return render_template("request_reset.html", settings=settings)

    return render_template("request_reset.html", settings=settings)


@app.route("/reset/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Handle password reset form."""
    settings = get_settings()

    # Validate token
    username, error = validate_token(token)

    if error:
        audit_log("INVALID_TOKEN", details=error)
        flash(error, "error")
        return redirect(url_for("request_reset"))

    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not password:
            flash("Please enter a new password.", "error")
            return render_template("reset_password.html", token=token, username=username, settings=settings)

        pw_error = validate_password_strength(password, username=username)
        if pw_error:
            flash(pw_error, "error")
            return render_template("reset_password.html", token=token, username=username, settings=settings)

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token, username=username, settings=settings)

        # Change password in Elasticsearch
        if change_password(username, password):
            mark_token_used(token)
            if not clear_force_password_change(username):
                # the password DID change, so the user still sees
                # success — but without the credential_changed_at stamp, sessions
                # from before the reset stay valid. Record it where an admin looks.
                audit_log(
                    "PASSWORD_RESET_STAMP_FAILED",
                    username=username,
                    details="credential_changed_at not written; earlier sessions stay valid",
                )
            audit_log("PASSWORD_RESET_SUCCESS", username=username)
            flash(
                "Your password has been reset successfully. You can now log in.",
                "success",
            )
            return render_template("success.html", settings=settings)
        else:
            audit_log("PASSWORD_RESET_FAILED", username=username, details="es_api_error")
            flash(
                "Error resetting password. Please try again or contact the administrator.",
                "error",
            )
            return render_template("reset_password.html", token=token, username=username, settings=settings)

    return render_template("reset_password.html", token=token, username=username, settings=settings)


# =============================================================================
# Admin Routes
# =============================================================================


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Admin login page."""
    settings = get_settings()

    if request.method == "POST":
        password = request.form.get("password", "")

        # Check if admin password is set
        stored_hash = settings.get("admin_password_hash", "")

        if not stored_hash:
            # First time setup - set the password
            env_password = os.environ.get("ADMIN_PASSWORD", "")
            # constant-time compare, like every other credential
            # check in this file (a plain == leaks match length via timing).
            if env_password and secrets.compare_digest(password, env_password):
                settings["admin_password_hash"] = hash_password(password)
                save_settings(settings)
                session["admin_logged_in"] = True
                audit_log("ADMIN_PASSWORD_SET", details="initial_setup")
                flash("Admin password set. Welcome!", "success")
                return redirect(url_for("admin_settings"))
            elif not env_password:
                flash(
                    "ADMIN_PASSWORD environment variable not set. Please configure it first.",
                    "error",
                )
            else:
                audit_log("ADMIN_LOGIN_FAILED", details="wrong_initial_password")
                flash("Invalid password.", "error")
        else:
            # Verify password
            if verify_password(password, stored_hash):
                session["admin_logged_in"] = True
                audit_log("ADMIN_LOGIN_SUCCESS")
                return redirect(url_for("admin_settings"))
            else:
                audit_log("ADMIN_LOGIN_FAILED", details="wrong_password")
                flash("Invalid password.", "error")

    return render_template("admin/login.html", settings=settings)


@app.route("/admin/logout")
def admin_logout():
    """Admin logout."""
    session.pop("admin_logged_in", None)
    audit_log("ADMIN_LOGOUT")
    flash("You have been logged out.", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    """Admin settings page."""
    settings = get_settings()

    env_managed = env_managed_smtp_fields()

    if request.method == "POST":
        # Update settings. env-managed SMTP fields are not editable
        # here — their form inputs are disabled and any submitted value is
        # ignored (save_settings also strips them as a second line of defense).
        if "smtp_host" not in env_managed:
            settings["smtp_host"] = request.form.get("smtp_host", "").strip()
        if "smtp_port" not in env_managed:
            settings["smtp_port"] = int(request.form.get("smtp_port", 587))
        if "smtp_username" not in env_managed:
            settings["smtp_username"] = request.form.get("smtp_username", "").strip()

        # Only update password if provided
        if "smtp_password" not in env_managed:
            new_smtp_password = request.form.get("smtp_password", "")
            if new_smtp_password:
                settings["smtp_password"] = new_smtp_password

        settings["smtp_use_tls"] = request.form.get("smtp_use_tls") == "on"
        settings["from_email"] = request.form.get("from_email", "").strip()
        settings["admin_contact"] = request.form.get("admin_contact", "").strip()
        settings["token_expiry_minutes"] = int(request.form.get("token_expiry_minutes", 60))
        settings["app_url"] = request.form.get("app_url", "").strip().rstrip("/")

        save_settings(settings)
        audit_log("SETTINGS_UPDATED")
        flash("Settings saved successfully.", "success")

        return redirect(url_for("admin_settings"))

    return render_template("admin/settings.html", settings=settings, is_configured=is_configured(), env_managed=env_managed)


@app.route("/admin/test-email", methods=["POST"])
@admin_required
def test_email():
    """Send a test email."""
    settings = get_settings()
    test_address = request.form.get("test_email", "").strip()

    if not test_address:
        flash("Please enter a test email address.", "error")
        return redirect(url_for("admin_settings"))

    if not is_configured():
        flash("Please configure SMTP settings first.", "error")
        return redirect(url_for("admin_settings"))

    # Send test email
    subject = "Test Email - Password Reset App"
    body = "This is a test email from the Kavanah Analytics Password Reset App. If you received this, your SMTP settings are configured correctly!"

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = settings["from_email"]
    msg["To"] = test_address

    try:
        if settings.get("smtp_use_tls", True):
            server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
            server.starttls(context=smtp_ssl_context())
        else:
            server = smtplib.SMTP_SSL(
                settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
            )

        server.login(settings["smtp_username"], settings["smtp_password"])
        server.sendmail(settings["from_email"], test_address, msg.as_string())
        server.quit()

        audit_log("TEST_EMAIL_SENT", details=f"to={test_address}")
        flash(f"Test email sent to {test_address}!", "success")
    except Exception as e:
        audit_log("TEST_EMAIL_FAILED", details=str(e))
        flash(f"Error sending test email: {str(e)}", "error")

    return redirect(url_for("admin_settings"))


@app.route("/admin/change-password", methods=["POST"])
@admin_required
def admin_change_password():
    """Change admin password."""
    settings = get_settings()

    current = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm = request.form.get("confirm_password", "")

    # Verify current password
    if not verify_password(current, settings.get("admin_password_hash", "")):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("admin_settings"))

    pw_error = validate_password_strength(new_password)
    if pw_error:
        flash(pw_error, "error")
        return redirect(url_for("admin_settings"))

    if new_password != confirm:
        flash("New passwords do not match.", "error")
        return redirect(url_for("admin_settings"))

    settings["admin_password_hash"] = hash_password(new_password)
    save_settings(settings)

    audit_log("ADMIN_PASSWORD_CHANGED")
    flash("Admin password changed successfully.", "success")
    return redirect(url_for("admin_settings"))


@app.route("/admin/audit-log")
@admin_required
def view_audit_log():
    """View recent audit log entries."""
    entries = []
    if os.path.exists(AUDIT_LOG_FILE):
        try:
            with open(AUDIT_LOG_FILE, "r") as f:
                lines = f.readlines()
                entries = lines[-100:]  # Last 100 entries
                entries.reverse()  # Most recent first
        except Exception as e:
            flash(f"Error reading audit log: {e}", "error")

    return render_template("admin/audit_log.html", entries=entries, settings=get_settings())


# =============================================================================
# Cleanup Task
# =============================================================================


@app.before_request
def before_request():
    """Run cleanup tasks before each request."""
    # Cleanup expired tokens occasionally (1 in 100 requests)
    if secrets.randbelow(100) == 0:
        cleanup_expired_tokens()
        cleanup_rate_limits()


# =============================================================================
# CSRF protection
# =============================================================================
# All POST forms in this app must include {{ csrf_token() }} as a hidden input.
#
# The token lives in a dedicated cookie (``pw_reset_csrf``), NOT in the Flask
# session. Storing it in the session was the source of the recurring CSRF 403
# on the second consecutive submit: ``flash`` sets
# ``session.modified=True``, Flask re-signs the session cookie, and any
# combination of bfcache restoration / multi-tab cookie races / mid-flow
# session rotation could leave the form's hidden input out of sync with what
# the server sees on the next POST.
#
# A dedicated cookie is immune to all of that. It is set once on first GET
# (or refreshed when missing), never rotated within its TTL, and validated by
# straight equality with the form value (double-submit cookie pattern). Real
# CSRF protection comes from ``SameSite=Strict`` on the cookie — an attacker
# cannot read or set it from a third-party origin.

CSRF_COOKIE_NAME = "pw_reset_csrf"
CSRF_COOKIE_MAX_AGE_SECONDS = 60 * 60  # 1 hour
# env-gated diagnostic logging. Enable on dev-reporting / prod with
# PW_RESET_DEBUG_CSRF=1 to see exactly when the helper runs, what cookies the
# request carries, and whether the after_request hook actually emits
# Set-Cookie. Logs to the gunicorn error log (/tmp/pw_reset_error.log).
_CSRF_DEBUG = os.environ.get("PW_RESET_DEBUG_CSRF") == "1"


def _get_or_create_csrf_token():
    """Return the active CSRF token, minting one if the client has no cookie.
    The token (existing or new) is stashed on ``g`` unconditionally so the
    after_request hook re-emits Set-Cookie on every form-page response —
     surfaced cases where the browser's pw_reset_csrf cookie was
    silently absent and the original "only emit when minting" path left the
    user stuck (no cookie + form has token => POST 403 forever)."""
    existing = request.cookies.get(CSRF_COOKIE_NAME)
    if existing:
        token = existing
    else:
        # Reuse a token already minted earlier in this same request (template
        # may call csrf_token() more than once).
        token = getattr(g, "_pw_reset_csrf_pending", None) or secrets.token_hex(32)
    g._pw_reset_csrf_pending = token
    if _CSRF_DEBUG:
        app.logger.info(
            "csrf_helper: path=%s existing_cookie=%r minted=%s token=%s",
            request.path,
            bool(existing),
            not existing,
            token[:8] + "...",
        )
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": _get_or_create_csrf_token}


@app.before_request
def generate_csp_nonce():
    """Phase 1: mint a per-request CSP nonce on flask.g, mirroring the
    DEAM app. Read by both inject_csp_nonce (templates) and set_security_headers
    (the script-src directive) so they share one value per request."""
    g.csp_nonce = secrets.token_urlsafe(16)


@app.context_processor
def inject_csp_nonce():
    return {"csp_nonce": getattr(g, "csp_nonce", "")}


@app.before_request
def validate_csrf():
    if request.method != "POST":
        return None
    expected = request.cookies.get(CSRF_COOKIE_NAME, "")
    submitted = request.form.get("csrf_token", "")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        if _CSRF_DEBUG:
            app.logger.info(
                "csrf_validate REJECT: path=%s expected_present=%s submitted_present=%s cookies=%s",
                request.path,
                bool(expected),
                bool(submitted),
                list(request.cookies.keys()),
            )
        audit_log("CSRF_REJECTED", details=f"path={request.path}")
        return ("CSRF token missing or invalid", 403)
    return None


@app.after_request
def set_csrf_cookie(response):
    """Always emit Set-Cookie on any response that touched the CSRF helper.
    previous version short-circuited when ``request.cookies.get(...)
    == pending``, but real-browser traffic on prod showed Set-Cookie never
    arriving — so the browser never stored the cookie and every POST 403'd.
    Re-emitting on every request is idempotent (same token, same attributes)
    and self-heals any case where the cookie went missing client-side."""
    pending = getattr(g, "_pw_reset_csrf_pending", None)
    if pending:
        response.set_cookie(
            CSRF_COOKIE_NAME,
            pending,
            max_age=CSRF_COOKIE_MAX_AGE_SECONDS,
            # Match the rest of the app's cookie security posture — prod sets
            # SESSION_COOKIE_SECURE via env so https-only is enforced; tests
            # don't, so the cookie can flow through the test client.
            secure=app.config.get("SESSION_COOKIE_SECURE", False),
            httponly=False,  # form mirrors the value; cookie just needs to match
            samesite="Strict",
            path="/reset-password",
        )
        if _CSRF_DEBUG:
            app.logger.info(
                "csrf_after_request EMIT: path=%s status=%s token=%s",
                request.path,
                response.status_code,
                pending[:8] + "...",
            )
    elif _CSRF_DEBUG:
        app.logger.info(
            "csrf_after_request SKIP (no pending): path=%s status=%s method=%s",
            request.path,
            response.status_code,
            request.method,
        )
    return response


@app.after_request
def set_security_headers(response):
    """Match the main Flask app's hardening so ZAP doesn't flag this app's
    pages as missing CSP / X-Frame-Options."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Phase 1: nonce the script-src (keep 'unsafe-inline' as the CSP2
    # fallback; CSP3 ignores it once a nonce is present). style-src stays on
    # 'unsafe-inline' only — inline style="..." attrs can't carry a nonce.
    nonce = getattr(g, "csp_nonce", "")
    script_src_nonce = f"'nonce-{nonce}' " if nonce else ""
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' " + script_src_nonce + "'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'self'; "
        "frame-src 'none'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    # Defeat browser bfcache on form pages so a back-navigation
    # never reuses a CSRF token that no longer matches the current session
    # (same fix class as the DEAM login bfcache bug). Static assets are the
    # one exception — let them cache normally.
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    app.run(  # nosemgrep: avoid_app_run_with_bad_host - containerized, behind nginx, dev runner only (prod uses gunicorn)
        host="0.0.0.0", port=8085, debug=False
    )

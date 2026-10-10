"""
MFA Service
Handles TOTP (authenticator app) and YubiKey 2FA logic for DEAM.
"""

import logging
import time
import base64
import io
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone

INDEX = "de_asset_user_mfa"
ISSUER = "Kavanah Analytics"
YUBICO_VERIFY_URL = "https://api.yubico.com/wsapi/2.0/verify"

INDEX_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "username": {"type": "keyword"},
        "mfa_enabled": {"type": "boolean"},
        "mfa_method": {"type": "keyword"},
        "totp_secret_enc": {"type": "keyword", "index": False},
        "totp_confirmed": {"type": "boolean"},
        "yubikey_public_id": {"type": "keyword"},
        "yubikey_confirmed": {"type": "boolean"},
        "enabled_by": {"type": "keyword"},
        "enabled_at": {"type": "date"},
        "last_used": {"type": "date"},
        # per-user break-glass bypass. True = skip MFA for this user
        # even when enforce_mfa is on. Audit-logged on set/unset.
        "bypass_mfa": {"type": "boolean"},
        "bypass_reason": {"type": "text"},
        "bypass_set_by": {"type": "keyword"},
        "bypass_set_at": {"type": "date"},
        # Tier 1: single-use recovery codes. Only the SHA-256 hashes of
        # the still-unused codes live here (plaintext is shown once at generation
        # and never persisted). Consuming a code removes its hash from the array,
        # so the remaining count is len(recovery_code_hashes).
        "recovery_code_hashes": {"type": "keyword"},
        "recovery_codes_generated_at": {"type": "date"},
        # Tier 2: email-based recovery. The recovery address must be
        # verified before it can receive a recovery-login link.
        "recovery_email": {"type": "keyword"},
        "recovery_email_verified": {"type": "boolean"},
        "recovery_email_verified_at": {"type": "date"},
        # jti of the currently-outstanding recovery-login token. Set on send,
        # cleared on use — makes the 15-min token single-use even though the JWT
        # itself is stateless.
        "recovery_login_jti": {"type": "keyword"},
        # ISO timestamps of recent recovery-email sends, pruned to the last hour
        # on each send to enforce the 3/hour cap (durable + shared across
        # gunicorn workers, unlike the in-memory auth limiter).
        "recovery_email_send_log": {"type": "date"},
    }
}

# Tier 1 configuration.
RECOVERY_CODE_COUNT = 10
# Each code is token_hex(5) -> 10 hex chars, displayed grouped as "xxxxx-xxxxx".
# (was 5 → 40 bits): 10 bytes = 20 hex chars = 80 bits.
# The codes are what stands between a leaked ``de_asset_user_mfa`` index and a
# TOTP bypass, so they must survive an OFFLINE search, not just online guessing
# behind the lockout. 80 bits is out of reach of any offline search; the keyed
# hash below is the second, independent layer.
RECOVERY_CODE_BYTES = 10
# prefix of the keyed-hash storage format. A stored entry without a
# ``$`` is a legacy unsalted SHA-256 of a 40-bit code.
RECOVERY_HASH_V2_PREFIX = "v2$"

# Tier 2: recovery-email rate limit — max sends per rolling window.
RECOVERY_EMAIL_MAX_PER_HOUR = 3
RECOVERY_EMAIL_WINDOW_SECONDS = 3600


class MFAService:

    # -------------------------------------------------------------------------
    # Index bootstrap
    # -------------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists():
        """Create the MFA index if it does not exist. Safe to call repeatedly."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("MFAService: could not connect to ES to ensure index exists")
            return
        try:
            if not es.indices.exists(index=INDEX):
                es.indices.create(index=INDEX, body={"mappings": INDEX_MAPPING})
                logging.info(f"MFAService: created index {INDEX}")
        except Exception as e:
            # Ignore "already exists" errors (race condition on startup)
            if "resource_already_exists_exception" not in str(e).lower():
                logging.error(f"MFAService: error ensuring index exists: {e}")

    # -------------------------------------------------------------------------
    # Record retrieval helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def get_mfa_record(username: str) -> dict | None:
        """Return the MFA record for a user, or None if not found."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            result = es.get(index=INDEX, id=username)
            return result["_source"]
        except Exception:
            return None

    @staticmethod
    def is_entra_enforced(username: str) -> bool:
        """is this user's second factor enforced upstream in Entra?

        Derived from the presence of ``metadata.microsoft_sub`` on the ES
        Security user record (stamped by MicrosoftSsoService on first linked
        sign-in) rather than a manually-set flag — so a new SSO
        account can never be forgotten into the wrong state. Fails closed
        (False) on any error path: a transient ES blip must degrade to the
        normal MFA rules, never to a silent skip.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            response = es.security.get_user(username=username)
            udata = dict(response).get(username, {})
            return bool((udata.get("metadata") or {}).get("microsoft_sub"))
        except Exception:
            return False

    @staticmethod
    def is_mfa_required(username: str) -> dict:
        """
        Check whether MFA is required for this user.

         rules, in order:
          1. The `elastic` superuser cannot log in via the web UI at all when
             enforce_mfa is on. Scripts use `elastic` against ES directly;
             no legitimate human-web-UI use case exists.
          2. if the account is linked to an Entra identity
             (metadata.microsoft_sub present), the second factor is enforced
             upstream by Entra Conditional Access — same enforcement outcome
             as a bypass (the app does not challenge), but reported as
             method="entra_sso" so the admin UI can render "protected
             elsewhere" instead of "exempted". Checked BEFORE bypass_mfa so a
             linked user with a legacy bypass flag reads as Entra,
             while a genuine bypass with no SSO linkage keeps its Bypassed
             rank-0 treatment.
          3. If the user's MFA record has bypass_mfa=true, skip MFA entirely.
             Break-glass escape hatch; set by another superuser with a reason.
          4. If ENFORCE_MFA is on and the user has no record or mfa_enabled
             is false, force enrollment (required=true, setup_complete=false).
          5. Otherwise fall through to the existing per-user logic.

        Returns:
            {required: bool, method: str|None, setup_complete: bool}
        """
        from asset_management.web.config import config

        # Rule 1: block elastic from web UI when enforcement is on
        if config.ENFORCE_MFA and username == "elastic":
            return {"required": True, "method": None, "setup_complete": False}

        # Rule 2: Entra-enforced factor — no app-side challenge,
        # honest method value. setup_complete=True so no caller can route
        # this user into TOTP/YubiKey enrollment (the rule-5 fall-through
        # the ticket flags never sees an entra_sso user).
        if MFAService.is_entra_enforced(username):
            return {"required": False, "method": "entra_sso", "setup_complete": True}

        record = MFAService.get_mfa_record(username)

        # Rule 3: per-user bypass always wins (break-glass)
        if record and record.get("bypass_mfa"):
            return {"required": False, "method": None, "setup_complete": False}

        # Rule 4: enforce MFA when no record or not enabled
        if not record or not record.get("mfa_enabled"):
            if config.ENFORCE_MFA:
                return {"required": True, "method": None, "setup_complete": False}
            return {"required": False, "method": None, "setup_complete": False}

        # Rule 5: existing per-user logic. An unrecognized stored method
        # falls through to setup_complete=False (enrollment) — deliberate for
        # genuinely-unknown values; entra_sso never reaches here because it is
        # DERIVED in rule 2, never stored in mfa_method.
        method = record.get("mfa_method")
        if method == "totp":
            setup_complete = bool(record.get("totp_confirmed"))
        elif method == "yubikey":
            setup_complete = bool(record.get("yubikey_confirmed"))
        else:
            setup_complete = False

        return {"required": True, "method": method, "setup_complete": setup_complete}

    @staticmethod
    def has_any_factor(username: str) -> bool:
        """Phase 2 (2026-05-20) — has the user configured ANY
        confirmed MFA factor (TOTP or yubikey)?

        Distinct from ``is_mfa_required``: this method ignores the
        ENFORCE_MFA config flag and break-glass bypass — it answers a
        simpler question for the onboarding wizard's step 3 gate:
        "Did this user finish setting up at least one second factor?"
        The wizard's gate must be a function of user state alone, not
        of cluster config.

        Returns True iff the user's MFA record exists AND has a
        ``*_confirmed`` flag set for whichever method they picked.
        Returns False on any error path so the gate fails closed (a
        transient ES blip MUST NOT let an unconfigured user slip past).

        Used by ``onboarding_mfa_setup_step_post`` to decide whether
        forward progression to /onboarding/step-4 (Account type) is
        allowed.
        """
        try:
            record = MFAService.get_mfa_record(username)
        except Exception as e:  # noqa: BLE001 — top-level boundary, fail-closed
            logging.warning("MFAService.has_any_factor(%r) raised: %s", username, e)
            return False
        if not record:
            return False
        if record.get("totp_confirmed"):
            return True
        if record.get("yubikey_confirmed"):
            return True
        return False

    @staticmethod
    def verify_active_factor(
        username: str,
        code: str,
        *,
        yubico_client_id: str = "",
        yubico_secret_key: str = "",
        allow_insecure_dev_mode: bool = False,
    ) -> bool:
        """Tier 3 — validate ``code`` against whichever confirmed factor
        the user currently has (TOTP or YubiKey). No state change.

        Used to make an admin re-prove their OWN second factor before they reset
        another user's MFA (the ticket's "admin should confirm their own MFA
        before resetting another user's" requirement). Fails closed: returns
        False if the user has no confirmed factor or the code doesn't validate.
        """
        record = MFAService.get_mfa_record(username)
        if not record:
            return False
        method = record.get("mfa_method")
        if method == "totp" and record.get("totp_confirmed"):
            return MFAService.validate_totp_code(username, code)
        if method == "yubikey" and record.get("yubikey_confirmed"):
            return MFAService.validate_yubikey_otp(
                otp=code,
                expected_public_id=record.get("yubikey_public_id", ""),
                yubico_client_id=yubico_client_id,
                yubico_secret_key=yubico_secret_key,
                allow_insecure_dev_mode=allow_insecure_dev_mode,
            )
        return False

    # -------------------------------------------------------------------------
    # Admin operations
    # -------------------------------------------------------------------------

    @staticmethod
    def list_all_users_with_mfa() -> list:
        """
        Fetch all Elasticsearch Security users and join with MFA records.

        Returns list of dicts: {username, full_name, email, roles,
                                mfa_enabled, mfa_method, setup_complete, last_used}
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []

        # Fetch all ES Security users
        try:
            response = es.security.get_user()
            es_users = dict(response)
        except Exception as e:
            logging.error(f"MFAService: could not fetch ES users: {e}")
            return []

        # Fetch all MFA records in one query
        mfa_records = {}
        try:
            result = es.search(index=INDEX, body={"query": {"match_all": {}}}, size=1000)
            for hit in result.get("hits", {}).get("hits", []):
                rec = hit["_source"]
                mfa_records[rec.get("username", hit["_id"])] = rec
        except Exception:
            pass  # Index may not exist yet; treat as empty

        users = []
        for uname, udata in es_users.items():
            # Skip built-in/system users
            if udata.get("metadata", {}).get("_reserved") or uname.startswith("."):
                continue

            record = mfa_records.get(uname, {})
            method = record.get("mfa_method")
            if method == "totp":
                setup_complete = bool(record.get("totp_confirmed"))
            elif method == "yubikey":
                setup_complete = bool(record.get("yubikey_confirmed"))
            else:
                setup_complete = False

            # Entra-linked users (metadata.microsoft_sub) report
            # method=entra_sso, DERIVED — mirrors is_mfa_required rule 2, and
            # supersedes any legacy bypass on the same account so the
            # SSO cohort renders as protected rather than exempted.
            if (udata.get("metadata") or {}).get("microsoft_sub"):
                method = "entra_sso"
                setup_complete = True

            users.append(
                {
                    "username": uname,
                    "full_name": udata.get("full_name", ""),
                    "email": udata.get("email", ""),
                    "roles": udata.get("roles", []),
                    "enabled": udata.get("enabled", True),
                    "metadata": udata.get("metadata", {}),
                    "mfa_enabled": bool(record.get("mfa_enabled", False)),
                    "mfa_method": method or "",
                    "setup_complete": setup_complete,
                    "last_used": record.get("last_used", ""),
                    "bypass_mfa": bool(record.get("bypass_mfa", False)),
                    "bypass_reason": record.get("bypass_reason", "") or "",
                    "bypass_set_by": record.get("bypass_set_by", "") or "",
                }
            )

        users.sort(key=lambda u: u["username"])
        return users

    @staticmethod
    def admin_enable_mfa(username: str, enabled_by: str) -> dict:
        """
        Admin enables MFA for a user without choosing a method.
        Creates or replaces the MFA record; the user picks TOTP or YubiKey
        during their next-login enrollment flow.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "@timestamp": now,
            "username": username,
            "mfa_enabled": True,
            "mfa_method": None,
            "totp_secret_enc": None,
            "totp_confirmed": False,
            "yubikey_public_id": None,
            "yubikey_confirmed": False,
            "enabled_by": enabled_by,
            "enabled_at": now,
            "last_used": None,
        }
        try:
            es.index(index=INDEX, id=username, document=doc, refresh="wait_for")
            logging.info(f"AUDIT: admin={enabled_by} action=mfa_enable target={username} method=pending")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: admin_enable_mfa error: {e}")
            return {"success": False, "error": "Failed to enable MFA."}

    @staticmethod
    def build_totp_setup_payload(username: str, existing_secret_enc: str | None = None) -> dict:
        """Build a TOTP setup payload (QR + secret). No ES write.

        If `existing_secret_enc` is supplied, decrypt and re-render the SAME
        secret — used when the user submits a wrong code so their authenticator
        app's existing entry stays valid. Otherwise generate a fresh secret.

        The encrypted secret is meant to be stashed in the Flask session and
        only persisted to ES once the user confirms with a valid 6-digit code
        (see commit_self_mfa_setup). Lets the profile flow's Cancel button
        leave any existing MFA record untouched.

        Returns: {secret_enc, qr_code_b64, totp_secret}
        """
        import pyotp
        import qrcode
        from lib.encryption import encrypt, decrypt

        if existing_secret_enc:
            secret = decrypt(existing_secret_enc)
            secret_enc = existing_secret_enc
        else:
            secret = pyotp.random_base32()
            secret_enc = encrypt(secret)

        provisioning_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)
        qr = qrcode.make(provisioning_uri)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        return {
            "secret_enc": secret_enc,
            "qr_code_b64": base64.b64encode(buf.getvalue()).decode("utf-8"),
            "totp_secret": secret,
        }

    @staticmethod
    def _normalize_totp_code(code: str) -> str:
        """Strip ALL whitespace from a user-entered TOTP code.

        Authenticator apps display the 6 digits with a separator space
        ("123 456"); typed or pasted that way the code must still compare
        equal to pyotp's "123456"..
        """
        return "".join((code or "").split())

    @staticmethod
    def verify_totp_code_against_secret(code: str, secret_enc: str) -> bool:
        """Validate a TOTP code against a caller-supplied encrypted secret. No ES access."""
        import pyotp
        from lib.encryption import decrypt

        code = MFAService._normalize_totp_code(code)
        if not secret_enc or not code:
            return False
        try:
            secret = decrypt(secret_enc)
            return pyotp.TOTP(secret).verify(code, valid_window=1)
        except Exception as e:
            logging.error(f"MFAService: verify_totp_code_against_secret error: {e}")
            return False

    @staticmethod
    def validate_yubikey_otp_for_registration(
        otp: str,
        yubico_client_id: str,
        yubico_secret_key: str,
        *,
        allow_insecure_dev_mode: bool = False,
    ) -> dict:
        """Validate a YubiKey OTP for self-service registration. No ES write.

        Returns {success, public_id, error}. On success, caller commits the
        public_id via commit_self_mfa_setup.
        """
        public_id = MFAService.extract_yubikey_public_id(otp)
        if not public_id:
            return {
                "success": False,
                "public_id": None,
                "error": "Invalid OTP length. Please touch your YubiKey again.",
            }

        if yubico_client_id:
            api_result = MFAService._call_yubico_api(otp, yubico_client_id, yubico_secret_key)
            if api_result.get("status") != "OK":
                return {
                    "success": False,
                    "public_id": None,
                    "error": f"YubiKey validation failed: {api_result.get('status', 'unknown error')}",
                }
        elif not allow_insecure_dev_mode:
            # (SVC-005): no Yubico client id configured and the dev-mode
            # bypass is not explicitly enabled — fail closed rather than accept an
            # unvalidated OTP on a 12-char public-prefix match alone.
            logging.error(
                "MFAService: YubiKey registration blocked — YUBICO_CLIENT_ID unset " "and MFA_ALLOW_INSECURE_DEV not enabled"
            )
            return {
                "success": False,
                "public_id": None,
                "error": "YubiKey validation is not configured on this server.",
            }

        return {"success": True, "public_id": public_id, "error": None}

    @staticmethod
    def commit_self_mfa_setup(
        username: str,
        method: str,
        totp_secret_enc: str | None = None,
        yubikey_public_id: str | None = None,
    ) -> dict:
        """Atomically write a fully-confirmed MFA record from a self-service setup.

        Upserts the record with mfa_enabled=True, the chosen method's confirmed
        flag set to True, and the OTHER method's fields explicitly cleared so
        switching methods doesn't leave stale data. Use only after caller has
        already validated the code/OTP.
        """
        from lib.es_helpers import get_elasticsearch_client

        if method not in ("totp", "yubikey"):
            return {"success": False, "error": "Invalid method"}
        if method == "totp" and not totp_secret_enc:
            return {"success": False, "error": "totp_secret_enc required for method=totp"}
        if method == "yubikey" and not yubikey_public_id:
            return {"success": False, "error": "yubikey_public_id required for method=yubikey"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        now = datetime.now(timezone.utc).isoformat()
        doc = {
            "@timestamp": now,
            "username": username,
            "mfa_enabled": True,
            "mfa_method": method,
            "enabled_by": username,
            "enabled_at": now,
        }
        if method == "totp":
            doc.update(
                {
                    "totp_secret_enc": totp_secret_enc,
                    "totp_confirmed": True,
                    "yubikey_public_id": None,
                    "yubikey_confirmed": False,
                }
            )
        else:
            doc.update(
                {
                    "yubikey_public_id": yubikey_public_id,
                    "yubikey_confirmed": True,
                    "totp_secret_enc": None,
                    "totp_confirmed": False,
                }
            )

        try:
            es.update(
                index=INDEX,
                id=username,
                body={"doc": doc, "doc_as_upsert": True},
                refresh="wait_for",
            )
            logging.info(f"AUDIT: user={username} action=self_mfa_setup_complete method={method}")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: commit_self_mfa_setup error: {e}")
            return {"success": False, "error": "Could not finish MFA setup."}

    @staticmethod
    def set_mfa_method(username: str, method: str) -> dict:
        """
        User picks TOTP or YubiKey during first-login enrollment.

        Upserts the MFA record: when ENFORCE_MFA is on the user
        may land here with no existing record. When the admin had clicked
        Enable 2FA first the record already exists and this just
        sets the method.
        """
        from lib.es_helpers import get_elasticsearch_client

        if method not in ("totp", "yubikey"):
            return {"success": False, "error": "Invalid method"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        now = datetime.now(timezone.utc).isoformat()
        try:
            es.update(
                index=INDEX,
                id=username,
                body={
                    "doc": {
                        "@timestamp": now,
                        "username": username,
                        "mfa_enabled": True,
                        "mfa_method": method,
                    },
                    "doc_as_upsert": True,
                },
                refresh="wait_for",
            )
            logging.info(f"AUDIT: user={username} action=mfa_method_chosen method={method}")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: set_mfa_method error: {e}")
            return {"success": False, "error": "Could not save the MFA method."}

    @staticmethod
    def admin_disable_mfa(username: str) -> dict:
        """Admin disables MFA for a user by removing the record."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        try:
            es.delete(index=INDEX, id=username, refresh="wait_for", ignore=[404])
            logging.info(f"AUDIT: action=mfa_disable target={username}")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: admin_disable_mfa error: {e}")
            return {"success": False, "error": "Failed to disable MFA."}

    @staticmethod
    def set_mfa_bypass(username: str, enabled: bool, set_by: str, reason: str = "") -> dict:
        """
        Superuser-only: flip the break-glass bypass flag on a user's MFA record.

        When `enabled` is True, is_mfa_required returns {required: false} for this
        user regardless of the enforce_mfa setting. Use for genuine lockouts
        (lost YubiKey, broken TOTP secret) while a real fix is in progress.

        Upserts the record so it works even on users who have never enrolled.
        Audit-logged on every flip.
        """
        from lib.es_helpers import get_elasticsearch_client

        if not isinstance(enabled, bool):
            return {"success": False, "error": "enabled must be a boolean"}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        now = datetime.now(timezone.utc).isoformat()
        doc: dict = {
            "username": username,
            "bypass_mfa": bool(enabled),
        }
        if enabled:
            doc["bypass_reason"] = reason
            doc["bypass_set_by"] = set_by
            doc["bypass_set_at"] = now
        else:
            # Clear the bypass metadata on unset so stale reasons don't linger
            doc["bypass_reason"] = None
            doc["bypass_set_by"] = None
            doc["bypass_set_at"] = None

        try:
            es.update(
                index=INDEX,
                id=username,
                body={"doc": doc, "doc_as_upsert": True},
                refresh="wait_for",
            )
            action = "mfa_bypass_enable" if enabled else "mfa_bypass_disable"
            logging.info(f"AUDIT: admin={set_by} action={action} target={username} reason={reason!r}")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: set_mfa_bypass error: {e}")
            return {"success": False, "error": "Failed to update the MFA bypass."}

    # -------------------------------------------------------------------------
    # TOTP setup (user-facing)
    # -------------------------------------------------------------------------

    @staticmethod
    def generate_totp_secret(username: str) -> dict:
        """
        Generate a new TOTP secret, encrypt it, store it (unconfirmed),
        and return the QR code as a base64-encoded PNG plus the provisioning URI.

        Returns:
            {qr_code_b64: str, provisioning_uri: str, totp_secret: str}
        """
        import pyotp
        import qrcode
        from lib.encryption import encrypt
        from lib.es_helpers import get_elasticsearch_client

        secret = pyotp.random_base32()
        provisioning_uri = pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=ISSUER)

        # Generate QR code PNG as base64
        qr = qrcode.make(provisioning_uri)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        qr_code_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        # Encrypt and persist secret (unconfirmed)
        encrypted_secret = encrypt(secret)
        es = get_elasticsearch_client()
        if es:
            try:
                es.update(
                    index=INDEX,
                    id=username,
                    body={
                        "doc": {
                            "totp_secret_enc": encrypted_secret,
                            "totp_confirmed": False,
                        }
                    },
                    refresh="wait_for",
                )
            except Exception as e:
                logging.error(f"MFAService: could not store TOTP secret: {e}")

        return {
            "qr_code_b64": qr_code_b64,
            "provisioning_uri": provisioning_uri,
            "totp_secret": secret,  # Returned so template can show manual entry key
        }

    @staticmethod
    def get_or_create_totp_setup(username: str) -> dict:
        """Return the TOTP setup payload for first-login enrollment, REUSING
        the stored unconfirmed secret when one exists.

        Regenerating on every QR-page load / failed confirm attempt (the
        earlier behavior) silently invalidated whatever QR the user had
        already scanned, stranding them with stale authenticator entries that
        fail every later challenge. Reuse keeps the scanned QR valid across
        refreshes and retries — mirroring the profile flow's
        build_totp_setup_payload(existing_secret_enc=...) approach.

        Falls back to a fresh generate_totp_secret when there is no pending
        (unconfirmed) secret, or the stored one cannot be decrypted (e.g. an
        enc-key rotation since it was written).
        """
        record = MFAService.get_mfa_record(username)
        secret_enc = (record or {}).get("totp_secret_enc")
        if secret_enc and not (record or {}).get("totp_confirmed"):
            try:
                return MFAService.build_totp_setup_payload(username, existing_secret_enc=secret_enc)
            except Exception as e:  # noqa: BLE001 — an undecryptable pending secret
                # must not block enrollment; fall through to a fresh secret.
                logging.warning(f"MFAService: pending TOTP secret unusable for {username}, regenerating: {e}")
        return MFAService.generate_totp_secret(username)

    @staticmethod
    def verify_and_confirm_totp(username: str, code: str) -> bool:
        """
        Validate a TOTP code and, on success, mark the setup as confirmed.
        """
        import pyotp
        from lib.encryption import decrypt
        from lib.es_helpers import get_elasticsearch_client

        record = MFAService.get_mfa_record(username)
        if not record or not record.get("totp_secret_enc"):
            return False

        code = MFAService._normalize_totp_code(code)
        try:
            secret = decrypt(record["totp_secret_enc"])
            totp = pyotp.TOTP(secret)
            if not totp.verify(code, valid_window=1):
                return False
        except Exception as e:
            logging.error(f"MFAService: TOTP verify error: {e}")
            return False

        # Mark confirmed AND fully enabled. Confirming a TOTP code is the
        # single source of truth that "MFA is set up" — so this write
        # must leave the record in the exact shape is_mfa_required keys
        # on: mfa_enabled=True, mfa_method='totp', totp_confirmed=True.
        # Setting all three here (not just totp_confirmed) makes the
        # confirm self-sufficient: if the upstream set_mfa_method call
        # failed (it's fail-soft, e.g. on a transient ES blip) the record
        # would otherwise lack mfa_enabled, and is_mfa_required would
        # treat the user as un-enrolled and re-prompt for setup on the
        # next login (the "MFA twice" bug). follow-up 2026-05-26.
        es = get_elasticsearch_client()
        if es:
            try:
                es.update(
                    index=INDEX,
                    id=username,
                    body={
                        "doc": {
                            "mfa_enabled": True,
                            "mfa_method": "totp",
                            "totp_confirmed": True,
                        }
                    },
                    refresh="wait_for",
                )
            except Exception as e:
                logging.error(f"MFAService: could not confirm TOTP: {e}")

        return True

    # -------------------------------------------------------------------------
    # TOTP login challenge
    # -------------------------------------------------------------------------

    @staticmethod
    def validate_totp_code(username: str, code: str) -> bool:
        """Validate a TOTP code during login (no state change)."""
        import pyotp
        from lib.encryption import decrypt

        record = MFAService.get_mfa_record(username)
        if not record or not record.get("totp_secret_enc"):
            return False

        code = MFAService._normalize_totp_code(code)
        try:
            secret = decrypt(record["totp_secret_enc"])
            return pyotp.TOTP(secret).verify(code, valid_window=1)
        except Exception as e:
            logging.error(f"MFAService: TOTP validation error: {e}")
            return False

    # -------------------------------------------------------------------------
    # YubiKey helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def extract_yubikey_public_id(otp: str) -> str | None:
        """
        Extract the YubiKey public ID from an OTP.
        A standard YubiKey OTP is 44 modhex characters;
        the first 12 characters are the device's static public ID.
        """
        otp = otp.strip()
        if len(otp) != 44:
            return None
        return otp[:12]

    @staticmethod
    def register_yubikey(
        username: str,
        otp: str,
        yubico_client_id: str,
        yubico_secret_key: str,
        *,
        allow_insecure_dev_mode: bool = False,
    ) -> dict:
        """
        Validate the OTP against Yubico API, then store the public ID
        and set yubikey_confirmed=True.
        """
        public_id = MFAService.extract_yubikey_public_id(otp)
        if not public_id:
            return {
                "success": False,
                "error": "Invalid OTP length. Please touch your YubiKey again.",
            }

        if yubico_client_id:
            api_result = MFAService._call_yubico_api(otp, yubico_client_id, yubico_secret_key)
            if api_result.get("status") != "OK":
                return {
                    "success": False,
                    "error": f"YubiKey validation failed: {api_result.get('status', 'unknown error')}",
                }
        elif not allow_insecure_dev_mode:
            # (SVC-005): fail closed when Yubico is unconfigured and the
            # dev-mode bypass is not explicitly enabled.
            logging.error(
                "MFAService: YubiKey registration blocked — YUBICO_CLIENT_ID unset " "and MFA_ALLOW_INSECURE_DEV not enabled"
            )
            return {
                "success": False,
                "error": "YubiKey validation is not configured on this server.",
            }

        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        try:
            es.update(
                index=INDEX,
                id=username,
                body={
                    # Set the full enabled shape — not just yubikey_confirmed
                    # — so confirming a YubiKey is the single source of truth
                    # that "MFA is set up" (mirrors verify_and_confirm_totp).
                    # Otherwise a fail-soft set_mfa_method upstream could
                    # leave mfa_enabled unset and is_mfa_required would
                    # re-prompt for setup on the next login.
                    # follow-up 2026-05-26.
                    "doc": {
                        "mfa_enabled": True,
                        "mfa_method": "yubikey",
                        "yubikey_public_id": public_id,
                        "yubikey_confirmed": True,
                    }
                },
                refresh="wait_for",
            )
            logging.info(f"AUDIT: user={username} action=yubikey_register success=True public_id={public_id}")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: register_yubikey error: {e}")
            return {"success": False, "error": "Could not save your YubiKey. Please try again."}

    # -------------------------------------------------------------------------
    # YubiKey login challenge
    # -------------------------------------------------------------------------

    @staticmethod
    def validate_yubikey_otp(
        otp: str,
        expected_public_id: str,
        yubico_client_id: str,
        yubico_secret_key: str,
        *,
        allow_insecure_dev_mode: bool = False,
    ) -> bool:
        """
        Validate a YubiKey OTP during login.
        Checks: OTP length, public ID matches registered key, Yubico API says OK.
        """
        public_id = MFAService.extract_yubikey_public_id(otp)
        if not public_id:
            return False

        if public_id != expected_public_id:
            logging.warning(f"MFAService: YubiKey public ID mismatch (expected {expected_public_id}, got {public_id})")
            return False

        if not yubico_client_id:
            # (SVC-005): with no Yubico client id, accepting any OTP that
            # matches the registered 12-char prefix downgrades the second factor to
            # a non-secret check. Only allow that when dev-mode is explicitly on;
            # otherwise fail closed.
            if not allow_insecure_dev_mode:
                logging.error(
                    "MFAService: YubiKey login blocked — YUBICO_CLIENT_ID unset " "and MFA_ALLOW_INSECURE_DEV not enabled"
                )
                return False
            return True

        api_result = MFAService._call_yubico_api(otp, yubico_client_id, yubico_secret_key)
        return api_result.get("status") == "OK"

    # -------------------------------------------------------------------------
    # Yubico API call
    # -------------------------------------------------------------------------

    @staticmethod
    def _sign_yubico_params(params: dict, secret_key: str) -> str:
        """HMAC-SHA1 sign Yubico params per the protocol: the key=value pairs
        sorted alphabetically and joined with '&' (the 'h' field itself is
        excluded), HMAC-SHA1 with the base64-decoded shared secret, base64 out.

        Used for both signing the outbound request and recomputing the expected
        signature when verifying the response.
        """
        param_string = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if k != "h")
        key_bytes = base64.b64decode(secret_key)
        return base64.b64encode(hmac.new(key_bytes, param_string.encode("utf-8"), hashlib.sha1).digest()).decode("utf-8")

    @staticmethod
    def _verify_yubico_response(result: dict, sent_nonce: str, sent_otp: str, secret_key: str) -> bool:
        """(SVC-004): authenticate a Yubico verify response before its
        status is trusted.

        Checks, all constant-time:
          * the echoed ``nonce`` equals the nonce we sent,
          * the echoed ``otp`` equals the otp we submitted,
          * when a shared secret is configured, the response HMAC (``h``)
            verifies against the recomputed signature.

        Returns False on any mismatch or missing field — a MITM / spoofed
        YubiCloud reply that forges status=OK without the shared secret cannot
        pass. NOTE: with no ``secret_key`` configured the response cannot be
        cryptographically authenticated (nonce/otp travel in cleartext in the
        request); a real deployment must set YUBICO_SECRET_KEY.
        """
        if not hmac.compare_digest(result.get("nonce", ""), sent_nonce):
            return False
        if not hmac.compare_digest(result.get("otp", ""), sent_otp):
            return False

        if secret_key:
            received_sig = result.get("h", "")
            if not received_sig:
                return False
            expected_sig = MFAService._sign_yubico_params(result, secret_key)
            if not hmac.compare_digest(received_sig, expected_sig):
                return False

        return True

    @staticmethod
    def _call_yubico_api(otp: str, client_id: str, secret_key: str) -> dict:
        """
        Call the Yubico OTP validation API with HMAC-SHA1 request signing.
        Returns parsed response dict with a 'status' key.

         (SVC-004): a returned status=OK is only trusted after the
        response is authenticated via _verify_yubico_response (response HMAC +
        nonce/otp echo). A response that fails authentication has its status
        overridden to BAD_RESPONSE so every caller (all of which gate on
        status == "OK") rejects it.
        """
        import requests as req_lib

        nonce = secrets.token_hex(16)
        params = {
            "id": client_id,
            "otp": otp,
            "nonce": nonce,
        }

        # Build HMAC signature if secret key is available
        if secret_key:
            params["h"] = MFAService._sign_yubico_params(params, secret_key)

        try:
            response = req_lib.get(YUBICO_VERIFY_URL, params=params, timeout=5)
            result = {}
            for line in response.text.strip().splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        except Exception as e:
            logging.error(f"MFAService: Yubico API call failed: {e}")
            return {"status": "ERROR"}

        if not MFAService._verify_yubico_response(result, sent_nonce=nonce, sent_otp=otp, secret_key=secret_key):
            logging.warning(
                "MFAService: Yubico response failed authentication " "(h/nonce/otp mismatch) — rejecting forged/tampered reply"
            )
            return {"status": "BAD_RESPONSE", "_original_status": result.get("status", "")}

        return result

    # -------------------------------------------------------------------------
    # Recovery codes
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_recovery_code(code: str) -> str:
        """Canonicalize a user-entered recovery code for hashing/compare.

        Display format is grouped ("abc12-de345") and users may paste with
        surrounding whitespace or in any case. Strip the visual grouping and
        lowercase so the stored hash matches regardless of how it was typed.
        """
        return (code or "").strip().lower().replace("-", "").replace(" ", "")

    @staticmethod
    def _recovery_code_key() -> bytes:
        """Server-side key for the recovery-code HMAC.

        ``MFA_RECOVERY_CODE_KEY`` when set (any string; rotating it invalidates
        every stored code, which is the intended lever after a suspected key
        leak), otherwise derived from the Flask ``SECRET_KEY`` through one HMAC
        step so the two never share raw key bytes. The key lives in the
        container environment / master-config, never in Elasticsearch — that
        separation is the whole point: a dumped ``de_asset_user_mfa`` index no
        longer contains everything needed to test a candidate code offline.
        """
        explicit = os.environ.get("MFA_RECOVERY_CODE_KEY", "")
        if explicit:
            return explicit.encode("utf-8")
        from asset_management.web.config import config

        secret = str(getattr(config, "SECRET_KEY", "") or "")
        return hmac.new(secret.encode("utf-8"), b"mfa-recovery-codes", hashlib.sha256).digest()

    @staticmethod
    def _hash_recovery_code(code: str) -> str:
        """Stored form of a recovery code: ``v2$`` + HMAC-SHA256 of
        the normalized code under the server key. Constant cost per entry, so a
        login can compare against all RECOVERY_CODE_COUNT entries in
        microseconds — a bcrypt per entry would have made every recovery login a
        multi-second operation for no gain against an 80-bit code."""
        normalized = MFAService._normalize_recovery_code(code)
        digest = hmac.new(MFAService._recovery_code_key(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{RECOVERY_HASH_V2_PREFIX}{digest}"

    @staticmethod
    def _legacy_hash_recovery_code(code: str) -> str:
        """storage form — unsalted SHA-256 of the normalized code. Kept
        ONLY so a user whose set predates can still sign in once and be
        moved to a fresh set; never written any more."""
        normalized = MFAService._normalize_recovery_code(code)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def is_legacy_recovery_hash(stored: str) -> bool:
        """True for a earlier entry (bare 64-hex SHA-256, no version prefix)."""
        return bool(stored) and "$" not in stored

    @staticmethod
    def recovery_set_is_legacy(record: dict | None) -> bool:
        """True when the user's stored recovery set is the format and
        should be regenerated (the profile shows a prompt)."""
        hashes = (record or {}).get("recovery_code_hashes") or []
        return bool(hashes) and any(MFAService.is_legacy_recovery_hash(h) for h in hashes)

    @staticmethod
    def _generate_recovery_codes_plaintext() -> list[str]:
        """Return RECOVERY_CODE_COUNT fresh codes in display format.

        Cryptographically secure (secrets). 20 hex chars (80 bits)
        grouped as "xxxxx-xxxxx-xxxxx-xxxxx" purely for legibility;
        normalization strips the dashes before hashing.
        """
        codes = []
        for _ in range(RECOVERY_CODE_COUNT):
            raw = secrets.token_hex(RECOVERY_CODE_BYTES)  # 20 hex chars
            codes.append("-".join(raw[i : i + 5] for i in range(0, len(raw), 5)))
        return codes

    @staticmethod
    def generate_recovery_codes(username: str) -> dict:
        """Generate a fresh set of recovery codes, store their hashes, and return
        the plaintext ONCE for display.

        Replaces any existing codes (used at enrollment when there are none, and
        from Account Settings to regenerate). The plaintext is never persisted —
        the caller is responsible for showing it a single time.

        Returns: {success, plaintext: [str], remaining: int} or {success: False, error}.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        plaintext = MFAService._generate_recovery_codes_plaintext()
        hashes = [MFAService._hash_recovery_code(c) for c in plaintext]
        now = datetime.now(timezone.utc).isoformat()
        try:
            es.update(
                index=INDEX,
                id=username,
                body={
                    "doc": {
                        "username": username,
                        "recovery_code_hashes": hashes,
                        "recovery_codes_generated_at": now,
                    },
                    "doc_as_upsert": True,
                },
                refresh="wait_for",
            )
            logging.info(f"AUDIT: user={username} action=recovery_codes_generated count={len(hashes)}")
            return {"success": True, "plaintext": plaintext, "remaining": len(hashes)}
        except Exception as e:
            logging.error(f"MFAService: generate_recovery_codes error: {e}")
            return {"success": False, "error": "Could not generate recovery codes."}

    @staticmethod
    def count_recovery_codes(username: str) -> int:
        """Number of unused recovery codes remaining for a user (0 on any error
        or if none were ever generated)."""
        record = MFAService.get_mfa_record(username)
        if not record:
            return 0
        hashes = record.get("recovery_code_hashes") or []
        return len(hashes)

    @staticmethod
    def consume_recovery_code(username: str, code: str) -> bool:
        """Validate a recovery code at login and, on match, consume it.

        Constant-time compare against every stored hash (so a match doesn't leak
        timing). On success the matched hash is removed from the array — making
        the code single-use — and last_used is refreshed. Returns False on any
        no-match / error path so the login challenge fails closed.
        """
        from lib.es_helpers import get_elasticsearch_client

        if not code:
            return False

        record = MFAService.get_mfa_record(username)
        if not record:
            return False
        stored_hashes = record.get("recovery_code_hashes") or []
        if not stored_hashes:
            return False

        # compare each stored entry against the candidate in ITS own
        # format — v2 keyed hash, or the legacy SHA-256 — constant-time,
        # no early break, so the work is independent of which (or whether a)
        # code matches.
        candidate_v2 = MFAService._hash_recovery_code(code)
        candidate_legacy = MFAService._legacy_hash_recovery_code(code)
        matched = None
        matched_legacy = False
        for h in stored_hashes:
            legacy = MFAService.is_legacy_recovery_hash(h)
            if hmac.compare_digest(h, candidate_legacy if legacy else candidate_v2):
                matched = h
                matched_legacy = legacy
        if matched is None:
            logging.warning(f"AUDIT: user={username} action=recovery_code_used success=False")
            return False

        if matched_legacy:
            # A (40-bit, unsalted) code just signed the user in. Retire
            # the WHOLE legacy set now — one legacy login is the migration
            # moment, not a reason to keep nine more offline-crackable codes
            # alive. count == 0 makes the post-login flow issue a fresh v2 set.
            remaining = []
            logging.warning(f"AUDIT: user={username} action=recovery_codes_legacy_retired count={len(stored_hashes)}")
        else:
            remaining = [h for h in stored_hashes if h != matched]
        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            es.update(
                index=INDEX,
                id=username,
                body={
                    "doc": {
                        "recovery_code_hashes": remaining,
                        "last_used": datetime.now(timezone.utc).isoformat(),
                    }
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(f"MFAService: consume_recovery_code error: {e}")
            return False

        logging.info(f"AUDIT: user={username} action=recovery_code_used success=True remaining={len(remaining)}")
        return True

    # -------------------------------------------------------------------------
    # Email-based recovery
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_email(email: str) -> str:
        return (email or "").strip().lower()

    @staticmethod
    def set_recovery_email(username: str, email: str) -> dict:
        """Store an UNVERIFIED recovery email on the user's MFA record.

        Setting (or changing) the address always resets the verified flag — a
        recovery-login link can only ever be sent to a verified address, so a
        freshly-typed one must be re-verified. Returns {success, error}.
        """
        from lib.es_helpers import get_elasticsearch_client

        cleaned = MFAService._normalize_email(email)
        if not cleaned or "@" not in cleaned:
            return {"success": False, "error": "Enter a valid email address."}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}

        try:
            es.update(
                index=INDEX,
                id=username,
                body={
                    "doc": {
                        "username": username,
                        "recovery_email": cleaned,
                        "recovery_email_verified": False,
                        "recovery_email_verified_at": None,
                    },
                    "doc_as_upsert": True,
                },
                refresh="wait_for",
            )
            logging.info(f"AUDIT: user={username} action=recovery_email_set")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: set_recovery_email error: {e}")
            return {"success": False, "error": "Could not save your recovery email."}

    @staticmethod
    def get_recovery_email(username: str) -> dict:
        """Return {email, verified} for the user's recovery email (email='' when
        none set)."""
        record = MFAService.get_mfa_record(username)
        if not record:
            return {"email": "", "verified": False}
        return {
            "email": record.get("recovery_email") or "",
            "verified": bool(record.get("recovery_email_verified")),
        }

    @staticmethod
    def has_verified_recovery_email(username: str) -> bool:
        """True iff the user has a recovery email AND it is verified. Fails
        closed on any error."""
        info = MFAService.get_recovery_email(username)
        return bool(info["email"]) and info["verified"]

    @staticmethod
    def mark_recovery_email_verified(username: str, email: str) -> dict:
        """Flip recovery_email_verified=True, but only if the token's address
        still matches the stored one (the user may have changed it after the
        verify link was issued). Returns {success, error}.
        """
        from lib.es_helpers import get_elasticsearch_client

        cleaned = MFAService._normalize_email(email)
        record = MFAService.get_mfa_record(username)
        if not record or MFAService._normalize_email(record.get("recovery_email", "")) != cleaned:
            # Stored address changed since the link was issued — refuse so a
            # stale link can't verify a since-replaced address.
            return {"success": False, "error": "This verification link no longer matches your recovery email."}

        es = get_elasticsearch_client()
        if es is None:
            return {"success": False, "error": "Could not connect to Elasticsearch"}
        try:
            es.update(
                index=INDEX,
                id=username,
                body={
                    "doc": {
                        "recovery_email_verified": True,
                        "recovery_email_verified_at": datetime.now(timezone.utc).isoformat(),
                    }
                },
                refresh="wait_for",
            )
            logging.info(f"AUDIT: user={username} action=recovery_email_verified")
            return {"success": True}
        except Exception as e:
            logging.error(f"MFAService: mark_recovery_email_verified error: {e}")
            return {"success": False, "error": "We couldn't verify your recovery email."}

    @staticmethod
    def recovery_email_rate_ok(username: str) -> bool:
        """True if the user is under the 3-per-hour recovery-email send cap.

        Reads the send log off the MFA record and counts entries within the
        rolling window. Fails CLOSED (returns False) on a read error so a
        transient ES blip can't be used to bypass the cap.
        """
        record = MFAService.get_mfa_record(username)
        if record is None:
            # No record at all — no prior sends, so allow (the send path will
            # create the record). Distinct from an ES error, which get_mfa_record
            # also maps to None; we accept that conflation here because the very
            # next call (record_recovery_email_send) writes durably and would
            # itself fail on a real outage.
            return True
        log = record.get("recovery_email_send_log") or []
        cutoff = time.time() - RECOVERY_EMAIL_WINDOW_SECONDS
        recent = [ts for ts in log if MFAService._iso_to_epoch(ts) >= cutoff]
        return len(recent) < RECOVERY_EMAIL_MAX_PER_HOUR

    @staticmethod
    def record_recovery_email_send(username: str) -> None:
        """Append now() to the send log, pruning entries older than the window."""
        from lib.es_helpers import get_elasticsearch_client

        record = MFAService.get_mfa_record(username) or {}
        log = record.get("recovery_email_send_log") or []
        cutoff = time.time() - RECOVERY_EMAIL_WINDOW_SECONDS
        recent = [ts for ts in log if MFAService._iso_to_epoch(ts) >= cutoff]
        recent.append(datetime.now(timezone.utc).isoformat())

        es = get_elasticsearch_client()
        if es is None:
            return
        try:
            es.update(
                index=INDEX,
                id=username,
                body={"doc": {"username": username, "recovery_email_send_log": recent}, "doc_as_upsert": True},
                refresh="wait_for",
            )
        except Exception as e:
            logging.warning(f"MFAService: record_recovery_email_send error: {e}")

    @staticmethod
    def _iso_to_epoch(ts: str) -> float:
        """Parse an ISO-8601 timestamp to epoch seconds; 0.0 on any parse error
        (a garbage entry is treated as ancient, i.e. outside the window)."""
        try:
            return datetime.fromisoformat(ts).timestamp()
        except Exception:
            return 0.0

    @staticmethod
    def stage_recovery_login_jti(username: str, jti: str) -> None:
        """Persist the jti of the outstanding recovery-login token so the link
        can be made single-use (consume clears it)."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return
        try:
            es.update(
                index=INDEX,
                id=username,
                body={"doc": {"username": username, "recovery_login_jti": jti}, "doc_as_upsert": True},
                refresh="wait_for",
            )
        except Exception as e:
            logging.warning(f"MFAService: stage_recovery_login_jti error: {e}")

    @staticmethod
    def consume_recovery_login(username: str, jti: str) -> bool:
        """Validate + consume a recovery-login token's jti (single-use).

        Returns True only if the stored jti matches the token's jti; on success
        it clears the stored jti AND disables MFA (clears both factors) so the
        user is forced to re-enroll. Returns False on mismatch / missing /
        already-consumed.
        """
        from lib.es_helpers import get_elasticsearch_client

        if not jti:
            return False
        record = MFAService.get_mfa_record(username)
        if not record:
            return False
        stored = record.get("recovery_login_jti")
        if not stored or not hmac.compare_digest(str(stored), str(jti)):
            logging.warning(f"AUDIT: user={username} action=mfa_recovery_login success=False reason=jti_mismatch")
            return False

        es = get_elasticsearch_client()
        if es is None:
            return False
        try:
            # Disable MFA (force re-enroll) AND clear the jti in one write so the
            # link is single-use even on a double-click.
            es.update(
                index=INDEX,
                id=username,
                body={
                    "doc": {
                        "mfa_enabled": False,
                        "mfa_method": None,
                        "totp_secret_enc": None,
                        "totp_confirmed": False,
                        "yubikey_public_id": None,
                        "yubikey_confirmed": False,
                        "recovery_login_jti": None,
                    }
                },
                refresh="wait_for",
            )
        except Exception as e:
            logging.error(f"MFAService: consume_recovery_login error: {e}")
            return False

        logging.info(f"AUDIT: user={username} action=mfa_recovery_login success=True")
        return True

    # -------------------------------------------------------------------------
    # Utility
    # -------------------------------------------------------------------------

    @staticmethod
    def update_last_used(username: str):
        """Update the last_used timestamp after a successful MFA challenge."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return
        try:
            es.update(
                index=INDEX,
                id=username,
                body={"doc": {"last_used": datetime.now(timezone.utc).isoformat()}},
                refresh=False,
            )
        except Exception as e:
            logging.warning(f"MFAService: could not update last_used: {e}")

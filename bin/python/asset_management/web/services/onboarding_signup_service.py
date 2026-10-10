"""
Onboarding Signup Service

Step 1 of the New User Onboarding wizard. Validates a self-
service signup payload and provisions the Elasticsearch Security user.

Distinct from the legacy ``RegistrationService`` which gates
signup behind email verification + admin approval. The wizard flow is
intentionally one-shot: the user picks a password, the account is
created, the session is started, and the user is redirected to step 2
in the same screen. The ``UserProvisioningService.create_user`` helper
is reused for the ES write, with the force-password-change flag cleared
immediately afterward because the user just chose their own password.

During the rollout the routes were gated by the new-onboarding feature
flag; the flag was retired so the wizard routes are
now unconditional. This service was always unconditional so the unit
tests can exercise the validation + provisioning contract directly.

Password rules mirror the existing ``UserProvisioningService.validate_admin_password_complexity``
contract (length >= 12, upper + lower + digit + special) so that the
 admin path and the wizard path use the same character
classes. The wizard adds a confirm-password match check; admin flows
don't because the admin types the password once into the modal.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Iterable

# Top-level imports so tests can patch ``UserProvisioningService`` and
# ``get_elasticsearch_client`` on this module. The
# ``sys.path`` insert mirrors the importer used by sibling services so
# ``lib.es_helpers`` resolves whether the module is loaded from a test
# harness (where conftest extends sys.path) or from the imports_container
# (where ``/app/python`` is the resolution root).
sys.path.insert(0, "/app/python")

from lib.es_helpers import get_elasticsearch_client, get_es_admin_client  # noqa: E402
from asset_management.web.services.user_provisioning_service import (  # noqa: E402
    UserProvisioningService,
)

# Username rules: 3–30 chars, alphanumeric + underscore + dot + hyphen,
# all-lowercased before validation so collisions are deterministic.
_USERNAME_RE = re.compile(r"^[a-z0-9_.\-]{3,30}$")

# Same special set the admin path accepts. Keeping the two
# paths aligned means an admin-created account can re-enter the wizard
# flow without surprise rejections on a previously-valid password.
_SPECIAL_CHARS = "!@#$%^&*()-_=+[]{};:,.<>?/\\|~"

# Minimum password length matches the + admin-set rules.
_MIN_PASSWORD_LEN = 12

# ES Security roles assigned to a self-service signup account.
# Empty list — KA users never log in to Kibana directly (the Flask app
# makes Kibana calls on their behalf), so a Kibana role on the user
# record is a no-op. ``es.security.authenticate()`` does not require
# a role for the auth call to succeed, so a roleless account can still
# authenticate; the login gate then bounces the unverified / wizard-
# incomplete user to the holding page. Workspace + tier-role assignment
# happens later in the wizard via ``de_authz_user_org_role`` etc.
DEFAULT_SIGNUP_ROLES: list[str] = []

# follow-up — reserved usernames an anonymous signup MUST NOT
# claim. Covers (a) ES built-in / superuser-like names that collide
# with platform roles (elastic, kibana, admin, root, superuser, system),
# (b) role-tier names a malicious actor could use for social-
# engineering (administrator, owner, support, help, security, billing),
# (c) brand-impersonation candidates (kavanah), and (d) URL-path
# collisions that could confuse users (api, www, mail, info, login,
# signup, logout, register, dashboard). ES Security's put_user already
# rejects a duplicate of `elastic` etc. on a real cluster — this list
# is the defence for a clean install or staging where those built-ins
# may not yet exist. Names are compared after lowercase normalization.
_RESERVED_USERNAMES = frozenset(
    {
        "admin",
        "administrator",
        "root",
        "elastic",
        "kibana",
        "superuser",
        "system",
        "support",
        "owner",
        "kavanah",
        "api",
        "www",
        "mail",
        "info",
        "security",
        "help",
        "billing",
        "login",
        "signup",
        "logout",
        "register",
        "dashboard",
    }
)

# follow-up — throwaway / disposable email domains. Curated short
# list of the most common services; not exhaustive (the throwaway-email
# space evolves constantly). Anti-abuse hardening rather than security:
# a determined attacker can stand up their own disposable domain in
# minutes, so this is a friction layer not a wall. Names are compared
# against the lowercased domain (post-@) of the submitted email.
_THROWAWAY_EMAIL_DOMAINS = frozenset(
    {
        "mailinator.com",
        "10minutemail.com",
        "10minutemail.net",
        "guerrillamail.com",
        "guerrillamail.net",
        "guerrillamail.org",
        "guerrillamail.biz",
        "sharklasers.com",
        "tempmail.com",
        "temp-mail.org",
        "yopmail.com",
        "yopmail.fr",
        "trashmail.com",
        "trashmail.net",
        "dispostable.com",
        "throwawaymail.com",
        "fakeinbox.com",
        "maildrop.cc",
        "mintemail.com",
        "mytemp.email",
        "tempinbox.com",
        "tempr.email",
        "spambog.com",
        "spambox.us",
        "getairmail.com",
        "mail-temp.com",
        "discard.email",
        "discardmail.com",
        "emailondeck.com",
        "moakt.com",
    }
)


class OnboardingSignupService:
    """Step-1 signup contract for the New User Onboarding wizard."""

    @staticmethod
    def find_user_by_email(email: str) -> dict | None:
        """Look up an ES Security user whose stored email matches ``email``.

        Mirrors ``GoogleSsoService.find_user_by_email`` / ``MicrosoftSsoService.find_user_by_email``
        — ES Security has no email index, so we enumerate ``get_user()`` and
        compare case-insensitively. The duplicate-detection path
        needs the existing user's username to pre-fill the login form, so
        the match record includes the username key alongside the raw ES
        fields. Returns ``None`` on no match or any ES failure (fail-open
        on the lookup so a transient ES blip never blocks legitimate
        signups — the downstream ``create_user`` still guards the
        username-collision case).
        """
        if not email:
            return None
        es = get_elasticsearch_client()
        if es is None:
            return None
        try:
            response = dict(es.security.get_user())
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error(
                "OnboardingSignupService.find_user_by_email: get_user failed: %s",
                e,
            )
            return None

        wanted = email.strip().lower()
        for username, record in response.items():
            stored_email = (record.get("email") or "").strip().lower()
            if stored_email and stored_email == wanted:
                return {"username": username, **record}
        return None

    @staticmethod
    def validate_signup(
        username: str,
        email: str,
        password: str,
        confirm_password: str,
        full_name: str = "",
    ) -> dict:
        """Validate a wizard signup payload.

        Returns ``{"valid": bool, "errors": [<message>, ...]}``. All
        errors are collected so the form can surface them at once
        rather than ping-ponging the user through a one-at-a-time fix.
        """
        errors: list[str] = []

        username = (username or "").strip().lower()
        if not _USERNAME_RE.match(username):
            errors.append(
                "Username must be 3–30 characters and may only contain letters, " "numbers, underscores, dots, and hyphens."
            )
        # follow-up — reserved-username denylist. Defence against
        # impersonation of platform / role / brand identities. Only run
        # if the regex check above passed (no double-error on a malformed
        # username that happens to match a reserved string).
        elif username in _RESERVED_USERNAMES:
            errors.append("That username is reserved. Please choose another.")

        email = (email or "").strip().lower()
        # Lightweight email shape check — the source-of-truth validation
        # is the HTML5 type=email in the template plus the captcha; we
        # only need to keep obvious garbage out of the ES record.
        if "@" not in email or "." not in email.split("@")[-1]:
            errors.append("Email address is not valid.")
        # follow-up — the shape check has no length bound; cap
        # separately (RFC-5321 ceiling; mirrors the template's maxlength).
        elif len(email) > 254:
            errors.append("Email address must be 254 characters or fewer.")
        else:
            # follow-up — throwaway-email denylist. Same posture
            # as the username reserved check: only run if the basic shape
            # passed, so a malformed email surfaces one error not two.
            domain = email.split("@", 1)[1].strip().lower()
            if domain in _THROWAWAY_EMAIL_DOMAINS:
                errors.append("Please use a permanent email address. Disposable / throwaway email addresses are not allowed.")

        if not isinstance(password, str) or len(password) < _MIN_PASSWORD_LEN:
            errors.append(f"Password must be at least {_MIN_PASSWORD_LEN} characters.")
        else:
            if not any(c.isupper() for c in password):
                errors.append("Password must contain at least one uppercase letter.")
            if not any(c.islower() for c in password):
                errors.append("Password must contain at least one lowercase letter.")
            if not any(c.isdigit() for c in password):
                errors.append("Password must contain at least one number.")
            if not any(c in _SPECIAL_CHARS for c in password):
                errors.append("Password must contain at least one special character.")

        if password != confirm_password:
            errors.append("Passwords do not match.")

        # follow-up — full name is optional but bounded (mirrors
        # the template's maxlength="100"; a crafted POST bypasses the client).
        if full_name and len(full_name) > 100:
            errors.append("Full name must be 100 characters or fewer.")

        return {"valid": not errors, "errors": errors}

    @staticmethod
    def signup_step1(
        username: str,
        email: str,
        password: str,
        confirm_password: str,
        *,
        full_name: str = "",
        roles: Iterable[str] | None = None,
        detected_time_zone: str = "",
    ) -> dict:
        """Provision a wizard-flow user.

        On success returns ``{"success": True, "username": <str>,
        "roles": [...], "email": <str>, "full_name": <str>}``. On
        validation or provisioning failure returns
        ``{"success": False, "errors": [<message>, ...]}``.

        The flow:
          1. validate the payload
          2. create the ES user via ``UserProvisioningService.create_user``
             (which generates a temp password and sets force_password_change=True)
          3. immediately overwrite the temp password with the user-chosen
             one via ES ``change_password`` and clear force_password_change
             (the user just picked their own password — no first-login
             change should be forced)

        Step 2/3 are split because ``create_user`` is the existing
        guard-railed write path (collision check, audit log, suggestion
        on conflict). Re-using it keeps the username-collision UX
        consistent with the admin path; the temp password it produces
        is discarded.
        """
        validation = OnboardingSignupService.validate_signup(username, email, password, confirm_password, full_name=full_name)
        if not validation["valid"]:
            return {"success": False, "errors": validation["errors"]}

        username = username.strip().lower()
        email = email.strip().lower()
        roles_list = list(roles) if roles is not None else list(DEFAULT_SIGNUP_ROLES)

        # pre-check ES Security for an existing user with the
        # same email. The design brief explicitly chose the informative
        # pattern over the security-vague one (this is not a security-
        # sensitive product). Surfacing the existing username on the
        # error result lets the wizard layer pre-fill the login form so
        # the user lands one click away from signing in.
        existing = OnboardingSignupService.find_user_by_email(email)
        if existing is not None:
            return {
                "success": False,
                "errors": ["You already have an account — sign in instead."],
                "code": "email_taken",
                "existing_username": existing.get("username", ""),
            }

        create_result = UserProvisioningService.create_user(
            username=username,
            full_name=full_name,
            email=email,
            roles=roles_list,
        )
        if not create_result.get("success"):
            err = create_result.get("error") or "Could not create account."
            # Username collision surfaces a suggested alternative through
            # the existing UserProvisioningService contract; keep that
            # affordance visible to the wizard's caller.
            if create_result.get("code") == "username_taken":
                return {
                    "success": False,
                    "errors": [err],
                    "code": "username_taken",
                    "suggested_username": create_result.get("suggested_username"),
                }
            return {"success": False, "errors": [err]}

        # Replace the temporary password with the user-chosen one and
        # clear the force_password_change flag. If either step fails we
        # roll the user's password back into a forced-change state via
        # the existing reset flow rather than try to delete the partial
        # account — half-provisioned is recoverable; deletion at this
        # point would silently lose audit context.
        es = get_elasticsearch_client()
        if es is None:
            logging.error(
                "OnboardingSignupService.signup_step1: ES became unreachable "
                "after create_user succeeded for '%s' — account exists with "
                "force_password_change=True and the temp password is unknown.",
                username,
            )
            return {
                "success": False,
                "errors": ["Account was created but could not be activated. " "Please contact support."],
            }

        try:
            get_es_admin_client(es).security.change_password(username=username, body={"password": password})
        except Exception as e:  # noqa: BLE001 — top-level boundary
            # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
            logging.error(
                "OnboardingSignupService.signup_step1: change_password failed for '%s': %s",
                username,
                e,
            )
            return {
                "success": False,
                "errors": [
                    "Account was created but the password could not be set. " "Please use the password reset flow to recover."
                ],
            }

        # follow-up (2026-05-20): stamp ``onboarding_required``
        # so the LoginGateService can identify this user as a wizard
        # signup. The login gate redirects unverified/incomplete users
        # to the wizard until both gates pass; existing users (admin-
        # provisioned, pre-wizard signups, or any user that doesn't
        # have this key) are grandfathered through. Best-effort: if the
        # stamp fails the user is still in a working state (they just
        # won't be hard-blocked at login — which is the safer regression).
        #
        # Issue 2 (2026-05-21): also stamp ``wizard_signup`` —
        # the PERMANENT mirror of "this user came in through the wizard,
        # ever". ``onboarding_required`` is a transient gate flag the
        # login gate clears once both gates pass (so a future login is
        # fast); ``wizard_signup`` is never cleared and is what the
        # dashboard checklist (and any future post-wizard discovery
        # surface) reads to decide whether the user is in the wizard
        # cohort. Two separate concepts that were collapsed into one
        # flag v2; this split fixes the regression where a
        # wizard-completed user landed on /dashboard with the checklist
        # already suppressed by the lazy-clear.
        try:
            current = dict(es.security.get_user(username=username)).get(username) or {}
            metadata = dict(current.get("metadata") or {})
            metadata["onboarding_required"] = True
            metadata["wizard_signup"] = True
            # stamp so web.auth denies any session pre-dating this
            # signup's password write (belt-and-suspenders here — there is no
            # earlier session to deny in the normal flow, but a retried/
            # duplicate signup call must not leave a stale session valid).
            metadata["credential_changed_at"] = UserProvisioningService.credential_changed_stamp()
            # stamp the browser-detected time zone captured on the
            # signup form so the per-user ``time_zone`` is set at account
            # creation (rather than lazily on first profile visit). Reuse
            # UserProfileService's validator so a forged/invalid value is
            # silently dropped (falls back to the UTC default downstream).
            from asset_management.web.services.user_profile_service import (
                UserProfileService,
            )

            validated_tz = UserProfileService._coerce_time_zone(detected_time_zone)
            if validated_tz:
                metadata["time_zone"] = validated_tz
            get_es_admin_client(es).security.put_user(
                username=username,
                body={
                    "roles": current.get("roles", roles_list),
                    "full_name": current.get("full_name", full_name),
                    "email": current.get("email", email),
                    # ES writes enabled=true when it is omitted.
                    "enabled": current.get("enabled", True),
                    "metadata": metadata,
                },
            )
            # flush this process's account-state cache entry now
            # that credential_changed_at has landed (fail-soft, never raises).
            UserProvisioningService._flush_credential_change_cache(username)
        except Exception as e:  # noqa: BLE001 — best-effort, fail-soft
            logging.warning(
                "OnboardingSignupService.signup_step1: onboarding_required stamp failed for '%s': %s",
                username,
                e,
            )

        if not UserProvisioningService.clear_force_password_change(username):
            # Password was changed successfully; the user can still log
            # in with their chosen password, they'll just hit the
            # force-password-change prompt once. Surface the partial
            # success so the route knows to redirect to /change-password
            # rather than /onboarding/step-4.
            logging.error(
                "OnboardingSignupService.signup_step1: clear_force_password_change "
                "failed for '%s' — user can log in but will be re-prompted.",
                username,
            )
            return {
                "success": True,
                "username": username,
                "roles": roles_list,
                "email": email,
                "full_name": full_name,
                "force_password_change": True,
            }

        logging.info(
            "AUDIT: action=onboarding_signup username=%s roles=%s email=%s",
            username,
            roles_list,
            email,
        )
        return {
            "success": True,
            "username": username,
            "roles": roles_list,
            "email": email,
            "full_name": full_name,
            "force_password_change": False,
        }

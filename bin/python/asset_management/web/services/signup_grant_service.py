"""Personal signup links: let one named person create their own workspace
while public self-signup is closed.

Every attendee of the 2026-10-20 webinar gets their own workspace as org or
team owner, but ``SELF_SIGNUP_ENABLED`` stays off until after it (a teammate,
2026-09-29), and an invite can only JOIN an existing workspace. A signup grant
is the missing third door: a superuser mints one per attendee email, the
attendee opens the link, and the existing new-owner wizard runs for that one
email.

A grant is a server-side record in ``de_signup_grant`` (one doc per grant, id =
the token's ``jti``). The record, not the token, is the authority:

* the token is an HS256 JWT with its own ``purpose`` (``owner_signup``), so an
  invite or verification token can never pass as a grant and vice versa. It
  carries only the email (``sub``), the ``jti`` and ``exp``;
* a grant is usable only while its record is ``pending``, unexpired, and the
  signing-up email equals the grant's email;
* :meth:`claim` flips ``pending`` → ``used`` with an optimistic-concurrency
  write (``if_seq_no`` / ``if_primary_term``), so of two concurrent attempts
  exactly one wins; :meth:`release` returns a claim this attempt made when
  account creation then fails (a password-rule error must not burn the link);
* minting again for an email revokes that email's pending grant, so a re-send
  always leaves exactly one live link.

``sponsored`` (a teammate, 2026-09-29) marks the workspace the attendee creates as
comped, applied when the wizard provisions it (:meth:`apply_sponsorship`),
through the same workspace + org comp calls the superuser billing control makes.

The raw token is never stored or logged; only the ``jti`` is.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import jwt

sys.path.insert(0, "/app/python")

from lib.es_helpers import (  # noqa: E402
    create_index_if_not_exists,
    get_elasticsearch_client,
    put_regular_index_template,
)

INDEX = "de_signup_grant"
TOKEN_PURPOSE = "owner_signup"
# 14 days (a teammate, 2026-09-29): attendees register in the week after the webinar
# with a buffer; a re-send revokes the old link and mints a fresh 14 days.
SIGNUP_GRANT_EXPIRY_SECONDS = 14 * 24 * 60 * 60

STATUS_PENDING = "pending"
STATUS_USED = "used"
STATUS_REVOKED = "revoked"
STATUS_EXPIRED = "expired"  # derived at read time from expires_at; never stored

INDEX_MAPPING = {
    "properties": {
        "email": {"type": "keyword"},
        "jti": {"type": "keyword"},
        "status": {"type": "keyword"},
        "sponsored": {"type": "boolean"},
        "created_by": {"type": "keyword"},
        "created_at": {"type": "date"},
        "expires_at": {"type": "date"},
        "last_sent_at": {"type": "date"},
        "used_by": {"type": "keyword"},
        "used_at": {"type": "date"},
        "revoked_by": {"type": "keyword"},
        "revoked_at": {"type": "date"},
    }
}

INVALID_LINK = "This signup link is not valid. Ask the person who sent it for a new one."
EXPIRED_LINK = "This signup link has expired. Ask the person who sent it for a new one."
USED_LINK = "This signup link has already been used. Sign in instead."


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _norm(email: str | None) -> str:
    return str(email or "").strip().lower()


def _body(resp: Any) -> Any:
    return getattr(resp, "body", resp)


class SignupGrantService:
    """Mint, check, claim and revoke personal signup links."""

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the grant index if it does not exist. Idempotent."""
        es = get_elasticsearch_client()
        if es is None:
            logging.warning("SignupGrantService: could not connect to ES")
            return
        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)

    # ── token ────────────────────────────────────────────────────────────────

    @staticmethod
    def _encode(email: str, jti: str, exp: int, secret_key: str) -> str:
        return jwt.encode(
            {"sub": email, "purpose": TOKEN_PURPOSE, "jti": jti, "iat": int(time.time()), "exp": exp},
            secret_key,
            algorithm="HS256",
        )

    @staticmethod
    def _decode(token: str, secret_key: str) -> dict:
        """``{"valid": True, "email", "jti"}`` or ``{"valid": False, "error", "expired"}``."""
        if not token:
            return {"valid": False, "error": INVALID_LINK, "expired": False}
        try:
            claims = jwt.decode(token, secret_key, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {"valid": False, "error": EXPIRED_LINK, "expired": True}
        except jwt.InvalidTokenError:
            return {"valid": False, "error": INVALID_LINK, "expired": False}
        email, jti = _norm(claims.get("sub")), str(claims.get("jti") or "")
        if claims.get("purpose") != TOKEN_PURPOSE or "@" not in email or not jti:
            return {"valid": False, "error": INVALID_LINK, "expired": False}
        return {"valid": True, "email": email, "jti": jti}

    # ── record ───────────────────────────────────────────────────────────────

    @staticmethod
    def _get(es: Any, jti: str) -> dict | None:
        """``{"source", "seq_no", "primary_term"}`` for the grant, or None."""
        try:
            resp = _body(es.get(index=INDEX, id=jti))
        except Exception:  # noqa: BLE001 — missing doc / index both mean "no such grant"
            return None
        if not resp or not resp.get("found", True):
            return None
        return {"source": resp.get("_source") or {}, "seq_no": resp.get("_seq_no"), "primary_term": resp.get("_primary_term")}

    @staticmethod
    def effective_status(source: dict, now: datetime | None = None) -> str:
        """The grant's status, with a lapsed pending grant read as expired."""
        status = source.get("status") or ""
        if status == STATUS_PENDING:
            try:
                expires = datetime.fromisoformat(str(source.get("expires_at")))
            except ValueError:
                return STATUS_EXPIRED
            if expires <= (now or _now()):
                return STATUS_EXPIRED
        return status

    @staticmethod
    def mint(
        email: str,
        *,
        created_by: str,
        secret_key: str,
        sponsored: bool = False,
        expires_in_seconds: int = SIGNUP_GRANT_EXPIRY_SECONDS,
    ) -> dict:
        """Create a grant for ``email`` and return ``{"ok", "token", "jti", "revoked"}``.

        Any pending grant for the same email is revoked first, so a re-send
        leaves exactly one live link.
        """
        email = _norm(email)
        if "@" not in email or len(email) > 254:
            return {"ok": False, "error": "A valid email address is required."}
        if not created_by:
            return {"ok": False, "error": "created_by is required"}
        es = get_elasticsearch_client()
        if es is None:
            return {"ok": False, "error": "Elasticsearch unavailable"}
        SignupGrantService.ensure_index_exists()
        revoked = SignupGrantService._revoke_pending_for_email(es, email, by=created_by)
        jti = uuid.uuid4().hex
        now = time.time()
        exp = int(now + expires_in_seconds)
        doc = {
            "email": email,
            "jti": jti,
            "status": STATUS_PENDING,
            "sponsored": bool(sponsored),
            "created_by": created_by,
            "created_at": _iso(now),
            "expires_at": _iso(exp),
            "last_sent_at": None,
            "used_by": None,
            "used_at": None,
            "revoked_by": None,
            "revoked_at": None,
        }
        es.index(index=INDEX, id=jti, document=doc, op_type="create", refresh="wait_for")
        return {"ok": True, "jti": jti, "token": SignupGrantService._encode(email, jti, exp, secret_key), "revoked": revoked}

    @staticmethod
    def _revoke_pending_for_email(es: Any, email: str, *, by: str) -> int:
        try:
            resp = _body(
                es.search(
                    index=INDEX,
                    body={
                        "size": 100,
                        "query": {"bool": {"filter": [{"term": {"email": email}}, {"term": {"status": STATUS_PENDING}}]}},
                    },
                )
            )
        except Exception:  # noqa: BLE001 — no index yet means nothing to revoke
            return 0
        count = 0
        for hit in (resp.get("hits") or {}).get("hits") or []:
            if SignupGrantService.revoke(hit["_id"], by=by):
                count += 1
        return count

    @staticmethod
    def revoke(jti: str, *, by: str) -> bool:
        """Revoke a pending grant. False when it is not pending (or missing)."""
        es = get_elasticsearch_client()
        if es is None or not jti:
            return False
        got = SignupGrantService._get(es, jti)
        if not got or got["source"].get("status") != STATUS_PENDING:
            return False
        return SignupGrantService._cas(
            es, jti, got, {"status": STATUS_REVOKED, "revoked_by": by, "revoked_at": _now().isoformat()}
        )

    @staticmethod
    def _cas(es: Any, jti: str, got: dict, fields: dict) -> bool:
        """Write ``fields`` only if the doc is unchanged since ``got`` was read."""
        try:
            es.index(
                index=INDEX,
                id=jti,
                document={**got["source"], **fields},
                if_seq_no=got["seq_no"],
                if_primary_term=got["primary_term"],
                refresh="wait_for",
            )
            return True
        except Exception as exc:  # noqa: BLE001 — a version conflict means another writer won
            logging.info("SignupGrantService: write for %s lost the race: %s", jti, type(exc).__name__)
            return False

    # ── use ──────────────────────────────────────────────────────────────────

    @staticmethod
    def check(token: str, secret_key: str) -> dict:
        """Validate a presented link. ``{"valid": True, "grant": {...}}`` for a
        usable grant, else ``{"valid": False, "error"}`` with a user-safe message."""
        decoded = SignupGrantService._decode(token, secret_key)
        if not decoded["valid"]:
            return {"valid": False, "error": decoded["error"]}
        es = get_elasticsearch_client()
        if es is None:
            return {"valid": False, "error": INVALID_LINK}
        got = SignupGrantService._get(es, decoded["jti"])
        if not got or _norm(got["source"].get("email")) != decoded["email"]:
            return {"valid": False, "error": INVALID_LINK}
        status = SignupGrantService.effective_status(got["source"])
        if status == STATUS_USED:
            return {"valid": False, "error": USED_LINK}
        if status == STATUS_EXPIRED:
            return {"valid": False, "error": EXPIRED_LINK}
        if status != STATUS_PENDING:
            return {"valid": False, "error": INVALID_LINK}
        return {"valid": True, "grant": SignupGrantService.session_payload(got["source"])}

    @staticmethod
    def session_payload(source: dict) -> dict:
        """What the browser session carries between landing and signup."""
        return {
            "jti": source.get("jti") or "",
            "email": _norm(source.get("email")),
            "sponsored": bool(source.get("sponsored")),
            "created_by": source.get("created_by") or "",
        }

    @staticmethod
    def is_usable_for(grant: dict | None, email: str) -> bool:
        """True when the session's grant is still pending and bound to ``email``."""
        if not grant or not grant.get("jti") or _norm(email) != _norm(grant.get("email")):
            return False
        es = get_elasticsearch_client()
        if es is None:
            return False
        got = SignupGrantService._get(es, grant["jti"])
        return bool(
            got
            and _norm(got["source"].get("email")) == _norm(email)
            and SignupGrantService.effective_status(got["source"]) == STATUS_PENDING
        )

    @staticmethod
    def claim(jti: str, *, email: str, username: str) -> bool:
        """Atomically mark a pending grant for ``email`` used by ``username``.
        Exactly one of any number of concurrent claims returns True."""
        es = get_elasticsearch_client()
        if es is None or not jti:
            return False
        got = SignupGrantService._get(es, jti)
        if (
            not got
            or _norm(got["source"].get("email")) != _norm(email)
            or SignupGrantService.effective_status(got["source"]) != STATUS_PENDING
        ):
            return False
        return SignupGrantService._cas(
            es, jti, got, {"status": STATUS_USED, "used_by": username, "used_at": _now().isoformat()}
        )

    @staticmethod
    def release(jti: str, *, username: str) -> bool:
        """Undo a claim this attempt made (account creation then failed)."""
        es = get_elasticsearch_client()
        if es is None or not jti:
            return False
        got = SignupGrantService._get(es, jti)
        if not got or got["source"].get("status") != STATUS_USED or got["source"].get("used_by") != username:
            return False
        return SignupGrantService._cas(es, jti, got, {"status": STATUS_PENDING, "used_by": None, "used_at": None})

    # ── listing + sponsorship ────────────────────────────────────────────────

    @staticmethod
    def list_grants(limit: int = 500) -> list[dict]:
        """Newest first, each with its effective status."""
        es = get_elasticsearch_client()
        if es is None:
            return []
        try:
            resp = _body(
                es.search(index=INDEX, body={"size": limit, "sort": [{"created_at": "desc"}], "query": {"match_all": {}}})
            )
        except Exception:  # noqa: BLE001 — no index yet: no grants
            return []
        now = _now()
        out = []
        for hit in (resp.get("hits") or {}).get("hits") or []:
            src = dict(hit.get("_source") or {})
            src["status"] = SignupGrantService.effective_status(src, now)
            out.append(src)
        return out

    @staticmethod
    def mark_sent(jti: str) -> None:
        es = get_elasticsearch_client()
        if es is None:
            return
        try:
            es.update(index=INDEX, id=jti, doc={"last_sent_at": _now().isoformat()}, refresh="wait_for")
        except Exception as exc:  # noqa: BLE001 — bookkeeping only
            logging.warning("SignupGrantService: could not stamp last_sent_at on %s: %s", jti, type(exc).__name__)

    @staticmethod
    def apply_sponsorship(workspace_esid: str, *, actor: str) -> dict:
        """Comp the workspace a sponsored grant's owner just created, and every
        org under it — the same calls the superuser billing control makes for
        a workspace comp."""
        from asset_management.web.services.org_billing_service import OrgBillingService
        from asset_management.web.services.workspace_billing_service import WorkspaceBillingService

        ws = WorkspaceBillingService.set_comp(workspace_esid, actor)
        orgs_ok, orgs_failed = [], []
        for org in OrgBillingService.list_orgs_for_workspace(workspace_esid):
            o_esid = (org.get("org_esid") or "").strip()
            if not o_esid:
                continue
            r = OrgBillingService.set_org_comp(o_esid, actor, workspace_esid=workspace_esid)
            (orgs_ok if r.get("ok") else orgs_failed).append(o_esid)
        return {"ok": bool(ws.get("ok")) and not orgs_failed, "orgs_ok": orgs_ok, "orgs_failed": orgs_failed}


def signup_link_url(token: str, base_url: str) -> str:
    """The link an attendee opens. Always absolute: an emailed relative link
    is useless."""
    if not base_url:
        raise ValueError("an absolute base URL is required for a signup link")
    return f"{base_url.rstrip('/')}/onboarding/start?signup={token}"


def send_signup_link_email(email: str, link: str, *, sender_display: str) -> dict:
    """Email a personal signup link. ``{"success": bool, "error"?: str}``.

    Same shared SMTP settings, TLS context and timeout as the invite email.
    The link is the only secret in the message; it is never logged.
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from html import escape

    from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context

    try:
        from web.services.smtp_settings import load_smtp_settings
    except ImportError:
        from asset_management.web.services.smtp_settings import load_smtp_settings

    settings = load_smtp_settings("SignupGrantService")
    if not settings.get("smtp_host") or not settings.get("from_email"):
        return {"success": False, "error": "SMTP is not configured"}

    days = SIGNUP_GRANT_EXPIRY_SECONDS // 86400
    subject = "Set up your Kavanah Analytics workspace"
    who = escape(sender_display or "Kavanah Analytics")
    safe_link = escape(link, quote=True)
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{subject}</title></head>
<body style="margin:0;padding:0;background-color:#F1F3F3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#000716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F3F3;"><tr><td align="center" style="padding:24px 12px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;border:1px solid #e9ebed;">
<tr><td style="background-color:#232F3E;padding:16px 24px;border-radius:8px 8px 0 0;color:#ffffff;font-size:17px;font-weight:700;">Kavanah Analytics</td></tr>
<tr><td style="padding:32px 28px;font-size:15px;line-height:1.6;">
<h2 style="margin:0 0 16px;font-size:20px;">Your workspace is ready to set up</h2>
<p style="margin:0 0 12px;">Hello,</p>
<p style="margin:0 0 12px;"><strong>{who}</strong> has invited you to set up your own Kavanah Analytics workspace. This link is for <strong>{escape(email)}</strong>: create your account with that address, or continue with Google or Microsoft using it.</p>
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:20px 0;"><tr><td style="border-radius:6px;background-color:#0972D3;">
<a href="{safe_link}" style="display:inline-block;padding:14px 32px;color:#ffffff;text-decoration:none;font-weight:600;font-size:15px;border-radius:6px;">Set up my workspace</a>
</td></tr></table>
<p style="margin:16px 0 4px;font-size:13px;color:#5F6B7A;">This link works once and expires in {days} days. If the button doesn't work, copy and paste this URL into your browser:</p>
<p style="margin:0 0 16px;"><a href="{safe_link}" style="color:#0972D3;word-break:break-all;">{safe_link}</a></p>
<p style="margin:0;font-size:13px;color:#5F6B7A;">If you didn't expect this email, you can safely ignore it.</p>
</td></tr>
<tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;color:#5F6B7A;font-size:12px;">This is an automated message — please do not reply.</td></tr>
</table></td></tr></table></body></html>"""
    text_body = (
        f"Set up your Kavanah Analytics workspace\n\nHello,\n\n"
        f"{sender_display or 'Kavanah Analytics'} has invited you to set up your own Kavanah Analytics workspace. "
        f"This link is for {email}: create your account with that address, or continue with Google or Microsoft using it.\n\n"
        f"{link}\n\nThis link works once and expires in {days} days.\n\n"
        f"If you didn't expect this email, you can safely ignore it."
    )
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
        logging.info("SignupGrantService: signup link emailed to %s", email)
        return {"success": True}
    except Exception as e:  # noqa: BLE001 — mail boundary; the caller shows the failure
        logging.error("SignupGrantService: could not email %s: %s", email, type(e).__name__)
        return {"success": False, "error": type(e).__name__}

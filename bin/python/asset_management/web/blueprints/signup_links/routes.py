"""Personal signup links — the superuser surface.

While public self-signup is closed (until after the 2026-10-20 webinar), a
superuser creates one link per attendee here. Each attendee opens their link and
sets up their own workspace as owner. The page creates and emails links (one
email per line), lists every link with its status, re-sends (which revokes the
old link and mints a new one), and revokes.

Superuser-only: everyone else gets 404, so the page does not advertise itself.
Every POST carries the form CSRF token. Raw links are shown once, on the page
that created them, and never stored or logged.
"""

from __future__ import annotations

import logging
import re

from flask import abort, current_app, render_template, request, session

from . import signup_links_bp
from lib.email_format import is_plausible_email

try:
    from web import auth as auth_module
    from web.public_url import public_base_url
    from web.services.audit_log_service import AuditLogService
    from web.services.signup_grant_service import (
        SignupGrantService,
        send_signup_link_email,
        signup_link_url,
    )
except ImportError:
    from ... import auth as auth_module
    from ...public_url import public_base_url
    from ...services.audit_log_service import AuditLogService
    from ...services.signup_grant_service import (
        SignupGrantService,
        send_signup_link_email,
        signup_link_url,
    )

MAX_EMAILS_PER_SUBMIT = 200


def _require_superuser() -> str:
    if "superuser" not in (session.get("roles") or []):
        abort(404)
    return session.get("username") or ""


def _require_csrf() -> None:
    if not auth_module.validate_csrf_token(request.form.get("csrf_token", "")):
        abort(400, description="The form expired. Reload the page and try again.")


def _audit(actor: str, action: str, ok: bool, detail: str) -> None:
    AuditLogService.log_event(
        actor,
        action,
        ok,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=detail,
    )


def parse_emails(raw: str) -> tuple[list[str], list[str]]:
    """``(valid, rejected)`` from a pasted list: one per line, commas and
    semicolons also split; lowercased, de-duplicated, order kept."""
    valid: list[str] = []
    rejected: list[str] = []
    for part in re.split(r"[\s,;]+", raw or ""):
        email = part.strip().lower()
        if not email:
            continue
        # Length first: the loose regex is quadratic on long
        # '@'/'.'-heavy tokens, so never hand it more than an address can be.
        if len(email) > 254 or not is_plausible_email(email):
            rejected.append(part.strip())
        elif email not in valid:
            valid.append(email)
    return valid, rejected


def _render(actor: str, results: list[dict] | None = None, errors: list[str] | None = None, status: int = 200):
    return (
        render_template(
            "signup_links.html",
            grants=SignupGrantService.list_grants(),
            results=results or [],
            errors=errors or [],
            csrf_token=auth_module.refresh_csrf_token(),
            max_emails=MAX_EMAILS_PER_SUBMIT,
        ),
        status,
    )


def _mint_and_send(email: str, *, actor: str, sponsored: bool, send: bool, base_url: str) -> dict:
    minted = SignupGrantService.mint(email, created_by=actor, secret_key=current_app.secret_key, sponsored=sponsored)
    if not minted.get("ok"):
        return {"email": email, "ok": False, "error": minted.get("error") or "Could not create the link."}
    link = signup_link_url(minted["token"], base_url)
    row = {
        "email": email,
        "ok": True,
        "link": link,
        "sponsored": sponsored,
        "revoked": minted.get("revoked", 0),
        "sent": False,
    }
    if send:
        mail = send_signup_link_email(email, link, sender_display=session.get("full_name") or actor)
        row["sent"] = bool(mail.get("success"))
        if row["sent"]:
            SignupGrantService.mark_sent(minted["jti"])
        else:
            row["error"] = (
                f"Link created, but the email did not send ({mail.get('error') or 'unknown error'}). Copy the link below."
            )
    _audit(
        actor,
        "signup_grant_created",
        True,
        f"jti={minted['jti']} email={email} sponsored={sponsored} sent={row['sent']} revoked={row['revoked']}",
    )
    return row


@signup_links_bp.route("", methods=["GET"])
def signup_links_page():
    actor = _require_superuser()
    return _render(actor)


@signup_links_bp.route("/create", methods=["POST"])
def signup_links_create():
    actor = _require_superuser()
    _require_csrf()
    emails, rejected = parse_emails(request.form.get("emails", ""))
    errors = [f"Not an email address: {r}" for r in rejected]
    if not emails:
        return _render(actor, errors=errors or ["Enter at least one email address."], status=400)
    if len(emails) > MAX_EMAILS_PER_SUBMIT:
        return _render(actor, errors=[f"At most {MAX_EMAILS_PER_SUBMIT} addresses per submit."], status=400)
    base_url = public_base_url()
    if not base_url:
        return _render(
            actor,
            errors=["The public site address is not configured, so a link would not work. Nothing was created."],
            status=500,
        )
    sponsored = request.form.get("sponsored") == "on"
    send = request.form.get("send") == "on"
    results = [_mint_and_send(e, actor=actor, sponsored=sponsored, send=send, base_url=base_url) for e in emails]
    return _render(actor, results=results, errors=errors)


@signup_links_bp.route("/<jti>/resend", methods=["POST"])
def signup_links_resend(jti: str):
    actor = _require_superuser()
    _require_csrf()
    grant = next((g for g in SignupGrantService.list_grants() if g.get("jti") == jti), None)
    if grant is None:
        return _render(actor, errors=["That link no longer exists."], status=404)
    if grant.get("status") == "used":
        return _render(actor, errors=[f"{grant['email']} has already used their link."], status=400)
    base_url = public_base_url()
    if not base_url:
        return _render(actor, errors=["The public site address is not configured. Nothing was sent."], status=500)
    row = _mint_and_send(grant["email"], actor=actor, sponsored=bool(grant.get("sponsored")), send=True, base_url=base_url)
    return _render(actor, results=[row])


@signup_links_bp.route("/<jti>/revoke", methods=["POST"])
def signup_links_revoke(jti: str):
    actor = _require_superuser()
    _require_csrf()
    ok = SignupGrantService.revoke(jti, by=actor)
    _audit(actor, "signup_grant_revoked", ok, f"jti={jti}")
    if not ok:
        logging.info("signup_links: revoke of %s did nothing (not pending)", jti)
        return _render(actor, errors=["That link is not pending, so there was nothing to revoke."], status=400)
    return _render(actor)

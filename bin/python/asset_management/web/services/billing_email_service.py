"""Billing Email Service.

Transactional billing emails, sent on a background daemon thread so the
webhook / comp POST never blocks on SMTP. Mirrors the established pattern in
``OnboardingEmailVerificationService`` / ``InviteService``:

  * SMTP settings come from the shared ``/app/python/password_reset/config/
    settings.json`` file (host/port/user/pass/from_email/app_url).
  * Each public ``dispatch_*`` returns the started ``Thread`` so tests can
    ``.join()``; production ignores it.
  * A ``_send_fn`` seam lets tests record sends without touching SMTP.
  * Every send is best-effort: a failure logs and returns
    ``{"success": False, ...}`` — a billing email never breaks the webhook
    or the comp flow (the entitlement change has already been applied).

Emails (decisions confirmed 2026-06-05):
  * payment_succeeded  — a subscription started / renewed (Stripe invoice.paid).
  * payment_failed     — a charge failed / past_due (invoice.payment_failed).
  * subscription_canceled — a sub actually ended (period-end / lapse).
  * cancel_scheduled   — the user SCHEDULED a cancel at period end (still
    active until then; can still restore). Sent from the /billing/cancel route.
  * trial_ending       — the free trial ends in N days (cron-driven).
  * comp_granted       — a superuser comped the user AND that canceled a live
    PAID subscription (mode=canceled only). NOT sent for a comp on a user who
    had no paid sub — there is no billing change to explain.

The bodies are deliberately plain + branded with the same table-based shell
the verification email uses (email-client-safe, light-only).
"""

from __future__ import annotations

import json
import logging
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import sys
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

sys.path.insert(0, "/app/python")

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"


def _get_smtp_settings() -> dict:
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("BillingEmailService")


def _get_app_base_url(settings: dict | None = None) -> str:
    """Derive ``scheme://host`` from the SMTP settings' ``app_url`` (which
    stores the password-reset app base, e.g. ``https://host/reset-password``);
    strip the path so billing links build cleanly off scheme+host."""
    settings = settings if settings is not None else _get_smtp_settings()
    app_url = (settings.get("app_url") or "").rstrip("/")
    if not app_url:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(app_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _shell(subject: str, heading: str, body_html: str, settings: dict) -> str:
    """Wrap the per-email body in the shared branded, email-client-safe
    table shell (matches the onboarding verification email)."""
    logo_url = f"{_get_app_base_url(settings)}/static/kavanah-analytics-logo.svg"
    return f"""<!DOCTYPE html>
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
          <td style="vertical-align:middle;padding-left:10px;color:#ffffff;font-size:17px;font-weight:700;">Kavanah Analytics</td>
        </tr></table>
      </td></tr>
      <tr><td style="padding:32px 28px;background-color:#ffffff;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;color:#000716;font-size:20px;font-weight:700;">{heading}</h2>
        {body_html}
      </td></tr>
      <tr><td style="padding:16px 28px;background-color:#FAFAFA;border-top:1px solid #e9ebed;border-radius:0 0 8px 8px;font-size:12px;color:#5f6b7a;">
        Kavanah Analytics · This is an automated billing message.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""


# ── Email definitions ─────────────────────────────────────────────────────
# Each returns (subject, heading, html_body_inner, text_body). ``ctx`` carries
# the per-email substitutions (full_name, plan_name, price, date, etc.).


def _greeting(full_name: str) -> str:
    return f" {full_name}" if full_name else ""


def _billing_link(settings: dict) -> str:
    base = _get_app_base_url(settings)
    return f"{base}/billing/current" if base else "/billing/current"


class BillingEmailService:
    """Best-effort transactional billing emails."""

    # --- subject + body builders (pure; easy to unit-test) --------------

    @staticmethod
    def render(kind: str, ctx: dict, settings: dict) -> dict:
        """Return ``{"subject", "html", "text"}`` for an email ``kind``.

        ``kind`` ∈ {payment_succeeded, payment_receipt, payment_failed,
        subscription_canceled, cancel_scheduled, addons_removal_scheduled,
        trial_ending, comp_granted, comp_revoked}. Unknown kinds raise
        ValueError (a programming error, not a runtime input).
        """
        name = ctx.get("full_name", "")
        plan = ctx.get("plan_name", "your plan")
        price = ctx.get("price_display", "")
        date = ctx.get("date", "")
        days = ctx.get("days_remaining", "")
        link = _billing_link(settings)
        hello = f"Hello{_greeting(name)},"

        if kind == "payment_succeeded":
            subject = "Your Kavanah Analytics subscription is active"
            heading = "Payment received — you're all set"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Thanks — your payment for the <strong>{plan}</strong> plan "
                f"({price}/month) went through and your subscription is active.</p>"
                + (f"<p style='margin:0 0 12px;'>Your next billing date is <strong>{date}</strong>.</p>" if date else "")
                + f"<p style='margin:0 0 12px;'>You can review your plan any time on your "
                f"<a href='{link}'>Current Plan</a> page.</p>"
            )
            text = (
                f"{hello}\n\nThanks — your payment for the {plan} plan ({price}/month) went through "
                f"and your subscription is active."
                + (f"\nYour next billing date is {date}." if date else "")
                + f"\n\nReview your plan: {link}\n"
            )
        elif kind == "payment_receipt":
            # Monthly receipt for a successful recurring charge (
            # 2026-06-09). ``price`` is the ACTUAL amount paid (Stripe
            # invoice.amount_paid — already net of any promo/coupon). The
            # caller only sends this for amount > $0 (a $0 charge sends no
            # receipt). ``date`` is the next billing date when known.
            #
            # when the charge is a MID-CYCLE add-on purchase
            # (proration), ``ctx["is_proration"]`` is True: the amount is a
            # partial charge for the rest of the current period, NOT a full
            # month, so the copy says so explicitly (otherwise an odd partial
            # figure on a renewal-shaped receipt reads as a billing error).
            is_proration = bool(ctx.get("is_proration"))
            # on a prorated add-on receipt, name the coupon so the odd
            # partial figure is legible. Present only when a discount applied;
            # absent → no breakdown clause (a no-coupon receipt is unchanged).
            discount = ctx.get("discount_display", "")
            subject = "Your Kavanah Analytics payment receipt"
            heading = "Payment receipt"
            if is_proration:
                disc_html = f" (after a {discount} discount)" if discount else ""
                disc_text = f" (after a {discount} discount)" if discount else ""
                lead_html = (
                    f"<p style='margin:0 0 12px;'>This confirms your payment of <strong>{price}</strong>{disc_html} — "
                    f"the prorated charge for adding data connections to your <strong>{plan}</strong> subscription "
                    f"partway through your current billing period. It covers only the days remaining in this "
                    f"period; your regular monthly rate applies from your next bill.</p>"
                )
                lead_text = (
                    f"This confirms your payment of {price}{disc_text} — the prorated charge for adding data "
                    f"connections to your {plan} subscription partway through your current billing period. It covers "
                    f"only the days remaining in this period; your regular monthly rate applies from your next bill."
                )
                date_label_html = (
                    f"<p style='margin:0 0 12px;'>Your next billing date is <strong>{date}</strong>.</p>" if date else ""
                )
                date_label_text = f"\nYour next billing date is {date}." if date else ""
            else:
                lead_html = (
                    f"<p style='margin:0 0 12px;'>This confirms your payment of <strong>{price}</strong> for your "
                    f"<strong>{plan}</strong> subscription this month.</p>"
                )
                lead_text = f"This confirms your payment of {price} for your {plan} subscription this month."
                date_label_html = (
                    f"<p style='margin:0 0 12px;'>Your next billing date is <strong>{date}</strong>.</p>" if date else ""
                )
                date_label_text = f"\nYour next billing date is {date}." if date else ""
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                + lead_html
                + date_label_html
                + f"<p style='margin:0 0 12px;'>You can review your plan and payment history any time on your "
                f"<a href='{link}'>Current Plan</a> page.</p>"
            )
            text = f"{hello}\n\n{lead_text}" + date_label_text + f"\n\nReview your plan: {link}\n"
        elif kind == "payment_failed":
            subject = "Action needed: your Kavanah Analytics payment failed"
            heading = "We couldn't process your payment"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Your most recent payment for the <strong>{plan}</strong> plan "
                f"didn't go through. Your data and imports are still active for now, but to avoid an "
                f"interruption please update your payment method.</p>"
                f"<p style='margin:0 0 12px;'><a href='{link}'>Update your payment method</a></p>"
            )
            text = (
                f"{hello}\n\nYour most recent payment for the {plan} plan didn't go through. Your data and "
                f"imports are still active for now, but to avoid an interruption please update your payment "
                f"method:\n{link}\n"
            )
        elif kind == "subscription_canceled":
            subject = "Your Kavanah Analytics subscription was canceled"
            heading = "Your subscription has ended"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Your <strong>{plan}</strong> subscription has been canceled. "
                f"Your existing data is still visible, but new data imports are paused.</p>"
                f"<p style='margin:0 0 12px;'>You can re-subscribe any time from your "
                f"<a href='{link}'>Current Plan</a> page to resume importing.</p>"
            )
            text = (
                f"{hello}\n\nYour {plan} subscription has been canceled. Your existing data is still visible, "
                f"but new data imports are paused.\n\nRe-subscribe to resume importing: {link}\n"
            )
        elif kind == "cancel_scheduled":
            # Confirmation when a user SCHEDULES a cancel (at period end) — the
            # sub is still active until ``date``; they can still restore it.
            until = f" on <strong>{date}</strong>" if date else " at the end of your current billing period"
            until_txt = f" on {date}" if date else " at the end of your current billing period"
            subject = "Your Kavanah Analytics subscription is set to cancel"
            heading = "Your subscription will cancel at period end"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Your <strong>{plan}</strong> subscription is set to cancel"
                f"{until}. You'll keep full access until then — your card won't be charged again.</p>"
                f"<p style='margin:0 0 12px;'>Changed your mind? You can restore your subscription any time "
                f"before then from your <a href='{link}'>Current Plan</a> page.</p>"
            )
            text = (
                f"{hello}\n\nYour {plan} subscription is set to cancel{until_txt}. You'll keep full access "
                f"until then — your card won't be charged again.\n\nChanged your mind? Restore it before then "
                f"from your Current Plan page: {link}\n"
            )
        elif kind == "addons_removal_scheduled":
            # (2026-06-12) — the Remove flow generates NO invoice
            # (item deleted, proration none), so the receipt email never
            # announces it; without this the org's next receipt is just
            # silently smaller a month later. ``count`` = extra connections
            # being removed; ``date`` = the paid period end they last until.
            count = ctx.get("count", "")
            one = str(count) == "1"
            plural = "" if one else "s"
            until = f" on <strong>{date}</strong>" if date else " at the end of your current billing period"
            until_txt = f" on {date}" if date else " at the end of your current billing period"
            subject = "Your extra data connections are scheduled for removal"
            heading = "Extra connections scheduled for removal"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Your <strong>{count} extra data connection{plural}</strong> "
                f"{'is' if one else 'are'} scheduled for removal{until}. "
                f"{'It stays' if one else 'They stay'} usable until then, and your next bill won't include "
                f"{'it' if one else 'them'}. Your connections and data are not deleted.</p>"
                f"<p style='margin:0 0 12px;'>Changed your mind? You can purchase add-ons again any time from "
                f"your <a href='{link}'>Current Plan</a> page.</p>"
            )
            text = (
                f"{hello}\n\nYour {count} extra data connection{plural} "
                f"{'is' if one else 'are'} scheduled for removal{until_txt}. "
                f"{'It stays' if one else 'They stay'} usable until then, and your next bill won't include "
                f"{'it' if one else 'them'}. Your connections and data are not deleted.\n\n"
                f"Changed your mind? Purchase add-ons again any time: {link}\n"
            )
        elif kind == "trial_ending":
            day_word = "day" if str(days) == "1" else "days"
            subject = f"Your free trial ends in {days} {day_word}"
            heading = "Your free trial is ending soon"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Your Kavanah Analytics free trial ends in <strong>{days} "
                f"{day_word}</strong>. Choose a plan now to keep your data imports running without "
                f"interruption.</p>"
                f"<p style='margin:0 0 12px;'><a href='{link}'>Choose a plan</a></p>"
            )
            text = (
                f"{hello}\n\nYour Kavanah Analytics free trial ends in {days} {day_word}. Choose a plan now "
                f"to keep your data imports running:\n{link}\n"
            )
        elif kind == "comp_granted":
            subject = "You've been granted complimentary Kavanah Analytics access"
            heading = "Complimentary access granted"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>An administrator has granted you <strong>complimentary "
                f"access</strong> to Kavanah Analytics. Your previous paid subscription has been "
                f"canceled and <strong>you won't be charged going forward</strong>.</p>"
                f"<p style='margin:0 0 12px;'>Your account stays fully active — nothing else changes. "
                f"See your <a href='{link}'>Current Plan</a>.</p>"
            )
            text = (
                f"{hello}\n\nAn administrator has granted you complimentary access to Kavanah Analytics. "
                f"Your previous paid subscription has been canceled and you won't be charged going forward. "
                f"Your account stays fully active.\n\nCurrent Plan: {link}\n"
            )
        elif kind == "comp_revoked":
            subject = "Your complimentary Kavanah Analytics access has ended"
            heading = "Your complimentary access has ended"
            html = (
                f"<p style='margin:0 0 12px;'>{hello}</p>"
                f"<p style='margin:0 0 12px;'>Your complimentary access to Kavanah Analytics has ended. "
                f"Your existing data is still visible, but new data imports are paused.</p>"
                f"<p style='margin:0 0 12px;'>To continue, ask an administrator for access again, or "
                f"<a href='{link}'>choose a plan</a>.</p>"
            )
            text = (
                f"{hello}\n\nYour complimentary access to Kavanah Analytics has ended. Your existing data is "
                f"still visible, but new data imports are paused.\n\nTo continue, ask an administrator for "
                f"access again, or choose a plan: {link}\n"
            )
        else:
            raise ValueError(f"unknown billing email kind: {kind!r}")

        return {"subject": subject, "html": _shell(subject, heading, html, settings), "text": text}

    # --- send (synchronous; SMTP) ---------------------------------------

    @staticmethod
    def send(kind: str, to_email: str, ctx: dict) -> dict:
        """Render + send one billing email synchronously. Best-effort:
        returns ``{"success": bool, "error": str}`` and never raises."""
        if not to_email:
            return {"success": False, "error": "no recipient"}
        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {"success": False, "error": f"SMTP not configured (missing: {', '.join(missing)})"}

        try:
            rendered = BillingEmailService.render(kind, ctx, settings)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        msg = MIMEMultipart("alternative")
        msg["Subject"] = rendered["subject"]
        msg["From"] = settings["from_email"]
        msg["To"] = to_email
        msg.attach(MIMEText(rendered["text"], "plain"))
        msg.attach(MIMEText(rendered["html"], "html"))

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], to_email, msg.as_string())
            server.quit()
            logging.info("BillingEmailService: %s email sent to %s", kind, to_email)
            return {"success": True}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("BillingEmailService: failed to send %s to %s: %s", kind, to_email, e)
            return {"success": False, "error": str(e)}

    # --- async dispatch --------------------------------------------------

    @staticmethod
    def dispatch(kind: str, to_email: str, ctx: dict, *, _send_fn: Any = None) -> threading.Thread | None:
        """Send a billing email on a daemon thread so the caller (webhook /
        comp POST / cron) never blocks on SMTP. Returns the started Thread
        (tests ``.join()``); production ignores it. No recipient → no thread
        (returns None). ``_send_fn`` is a test seam."""
        if not to_email:
            return None
        sender = _send_fn or BillingEmailService.send

        def _runner() -> None:
            try:
                sender(kind, to_email, ctx)
            except Exception as e:  # noqa: BLE001 — daemon thread last-line guard
                logging.error("BillingEmailService.dispatch: send failed (%s -> %s): %s", kind, to_email, e)

        thread = threading.Thread(target=_runner, name=f"billing-email-{kind}", daemon=True)
        thread.start()
        return thread

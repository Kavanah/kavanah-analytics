"""Admin CLI for the connector notify-list.

The notify-list captures user interest in roadmap connectors during
onboarding step 4. Two paths feed it: ``placeholder`` tiles (Google
Ads / GA4 — partial OAuth) and ``coming_soon`` tiles (Snapchat /
Pinterest / Reddit — roadmap-only). When a connector ships, an admin
runs this CLI to email everyone who tapped Notify-me for it.

Usage::

    # Print per-connector demand counts.
    docker exec -w /app/python imports_container python3 \\
        -m tools.connector_notify_list counts

    # List unsent subscribers for one connector.
    docker exec -w /app/python imports_container python3 \\
        -m tools.connector_notify_list list snapchat_ads

    # Dry-run a send — print what would be emailed, write nothing.
    docker exec -w /app/python imports_container python3 \\
        -m tools.connector_notify_list send snapchat_ads

    # Actually send + stamp ``notified_at`` on each successful row.
    docker exec -w /app/python imports_container python3 \\
        -m tools.connector_notify_list send snapchat_ads --apply

    # Override the subject line (default uses the connector display name).
    docker exec -w /app/python imports_container python3 \\
        -m tools.connector_notify_list send snapchat_ads --apply \\
        --subject "Snapchat Ads is live in Kavanah Analytics"

Sends are resumable — ``notified_at`` is stamped per-recipient on
success, so a SMTP error halfway through leaves the remaining rows
marked-unsent and the next run picks them up.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Resolve a directory containing the ``asset_management`` package — same
# three-candidate resolver as bin/python/tools/account_type_backfill.py
# so this works from a repo-local pytest run, a `python -m` invocation
# from bin/python, and inside the imports_container at /app/python.
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

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context  # noqa: E402
from asset_management.web.services.connector_catalog_service import (  # noqa: E402
    ConnectorCatalogService,
)
from asset_management.web.services.notify_list_service import (  # noqa: E402
    NotifyListService,
)

# Reuse the same shared SMTP settings file that the email-verification
# and password-reset paths key off, so the admin doesn't need to
# configure a third SMTP account just for launch comms.
_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"


# SMTP credentials are env-managed (PW_RESET_SMTP_*, injected from
# SSM via deployment/.env.aws) and the password-reset app strips them from
# settings.json. This overlay mirrors
# asset_management/web/services/smtp_settings.py — kept inline because this
# tool runs standalone, outside the web package. Keep the two in sync.
_SMTP_ENV_OVERRIDES = {
    "smtp_host": "PW_RESET_SMTP_HOST",
    "smtp_port": "PW_RESET_SMTP_PORT",
    "smtp_username": "PW_RESET_SMTP_USERNAME",
    "smtp_password": "PW_RESET_SMTP_PASSWORD",
}


def _load_smtp_settings() -> dict:
    try:
        with open(_PASSWORD_RESET_SETTINGS, "r") as f:
            settings = json.load(f)
    except FileNotFoundError:
        logging.error("SMTP config not found at %s", _PASSWORD_RESET_SETTINGS)
        settings = {}
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logging.error("could not load SMTP settings: %s", e)
        settings = {}
    for _field, _var in _SMTP_ENV_OVERRIDES.items():
        _val = os.environ.get(_var)
        if not _val:
            continue
        if _field == "smtp_port":
            try:
                settings[_field] = int(_val)
            except ValueError:
                logging.warning("ignoring non-numeric %s", _var)
                continue
        else:
            settings[_field] = _val
    return settings


def _app_base_url(settings: dict) -> str:
    """Strip the password-reset path off ``app_url`` so links point at
    the dashboard host. Falls back to an empty string — callers render
    a link-less plain-text body when this is missing."""
    raw = (settings.get("app_url") or "").rstrip("/")
    if not raw:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(raw)
    return f"{parsed.scheme}://{parsed.netloc}"


def _render_launch_email(
    *,
    connector_name: str,
    recipient_email: str,
    app_base_url: str,
    subject: str,
) -> tuple[str, str]:
    """Build (text_body, html_body) for one launch notification.

    Distinct from the verification / password-reset templates — this
    is a marketing-ish "the thing you asked about is ready" email, not
    a security one, so the chrome is lighter and the CTA points at the
    onboarding step-4 connector grid (where the user will now see the
    tile as selectable).
    """
    dashboard_link = f"{app_base_url}/onboarding/step-4" if app_base_url else ""
    text_body = (
        f"{subject}\n\n"
        f"Good news — {connector_name} is now available in Kavanah Analytics.\n\n"
        f"You asked us to let you know when it shipped, so here we are.\n"
        f"{('Connect it from the onboarding flow: ' + dashboard_link) if dashboard_link else ''}\n\n"
        f"If you've already finished onboarding, you can add {connector_name}\n"
        f"from the Data Connectors page in the asset manager.\n\n"
        f"— The Kavanah Analytics team\n"
    )
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>{subject}</title></head>
<body style="margin:0;padding:0;background-color:#F1F3F3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#000716;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#F1F3F3;">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;border:1px solid #e9ebed;">
      <tr><td style="background-color:#232F3E;padding:16px 24px;border-radius:8px 8px 0 0;color:#ffffff;font-size:17px;font-weight:700;">Kavanah Analytics</td></tr>
      <tr><td style="padding:28px;color:#000716;font-size:15px;line-height:1.6;">
        <h2 style="margin:0 0 16px;font-size:20px;font-weight:700;">{connector_name} is now available</h2>
        <p style="margin:0 0 12px;">You asked us to email you when {connector_name} shipped — it's live.</p>
        {(f'<p style="margin:0 0 12px;"><a href="{dashboard_link}" style="color:#0972D3;">Connect it now in your onboarding flow.</a></p>') if dashboard_link else ''}
        <p style="margin:0;font-size:13px;color:#5F6B7A;">Already past onboarding? Add it from the Data Connectors page in the asset manager.</p>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;color:#5F6B7A;font-size:12px;line-height:1.5;">
        You're receiving this email because you tapped <em>Notify me</em> for {connector_name}
        on the Kavanah Analytics onboarding flow ({recipient_email}). If you didn't, you can safely ignore this.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
    return text_body, html_body


def _send_one(
    *,
    settings: dict,
    recipient_email: str,
    subject: str,
    text_body: str,
    html_body: str,
) -> tuple[bool, str]:
    """Send a single message. Returns ``(success, error_message)``."""
    required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
    missing = [k for k in required if not settings.get(k)]
    if missing:
        return False, f"SMTP not configured (missing: {', '.join(missing)})"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings["from_email"]
    msg["To"] = recipient_email
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
        server.sendmail(settings["from_email"], recipient_email, msg.as_string())
        server.quit()
        return True, ""
    except Exception as e:  # noqa: BLE001 — top-level boundary
        return False, str(e)


def _resolve_connector(connector_id: str) -> tuple[str, str | None]:
    """Look up a connector_id against the catalog; return ``(name, error)``.

    Unknown ids return an error string so the CLI can fail loudly
    before touching ES.
    """
    tile = ConnectorCatalogService.get(connector_id)
    if tile is None:
        valid = sorted(t.connector_id for t in ConnectorCatalogService.all_tiles())
        return "", (f"unknown connector_id {connector_id!r} " f"(valid ids: {valid})")
    return tile.name, None


# ----------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------


def _cmd_counts(_args: argparse.Namespace) -> int:
    rows = NotifyListService.counts_by_connector()
    print(json.dumps(rows, indent=2))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    name, err = _resolve_connector(args.connector_id)
    if err:
        print(err, file=sys.stderr)
        return 2
    rows = NotifyListService.list_subscribers(args.connector_id, unsent_only=not args.all)
    print(json.dumps({"connector_id": args.connector_id, "connector_name": name, "subscribers": rows}, indent=2))
    return 0


def _cmd_send(args: argparse.Namespace) -> int:
    name, err = _resolve_connector(args.connector_id)
    if err:
        print(err, file=sys.stderr)
        return 2

    subscribers = NotifyListService.list_subscribers(args.connector_id, unsent_only=True)
    if not subscribers:
        print(f"no unsent subscribers for {args.connector_id} — nothing to do")
        return 0

    settings = _load_smtp_settings()
    if not settings and args.apply:
        print("SMTP settings missing; aborting --apply run", file=sys.stderr)
        return 3
    app_base = _app_base_url(settings) if settings else ""
    subject = args.subject or f"{name} is now live in Kavanah Analytics"

    summary = {
        "connector_id": args.connector_id,
        "connector_name": name,
        "subject": subject,
        "total_unsent": len(subscribers),
        "sent": 0,
        "marked_notified": 0,
        "errors": [],
        "apply": args.apply,
    }

    now = datetime.now(timezone.utc).isoformat()
    for row in subscribers:
        recipient = row.get("email") or row.get("username") or ""
        doc_id = row.get("_id") or ""
        if not recipient or not doc_id:
            summary["errors"].append({"doc_id": doc_id, "error": "no recipient or doc id"})
            continue
        if not args.apply:
            # Dry-run — just count the recipient.
            summary["sent"] += 1
            continue
        text_body, html_body = _render_launch_email(
            connector_name=name,
            recipient_email=recipient,
            app_base_url=app_base,
            subject=subject,
        )
        ok, err_msg = _send_one(
            settings=settings,
            recipient_email=recipient,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
        if not ok:
            summary["errors"].append({"doc_id": doc_id, "email": recipient, "error": err_msg})
            continue
        summary["sent"] += 1
        if NotifyListService.mark_notified(doc_id, notified_at=now):
            summary["marked_notified"] += 1
        else:
            summary["errors"].append(
                {"doc_id": doc_id, "email": recipient, "error": "send succeeded but mark_notified failed"}
            )

    print(json.dumps(summary, indent=2))
    return 0 if not summary["errors"] else 1


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="connector_notify_list",
        description="Admin CLI for the  connector notify-list.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("counts", help="Per-connector total + unsent counts.").set_defaults(func=_cmd_counts)

    list_p = sub.add_parser("list", help="List subscribers for one connector.")
    list_p.add_argument("connector_id")
    list_p.add_argument("--all", action="store_true", help="Include already-notified rows.")
    list_p.set_defaults(func=_cmd_list)

    send_p = sub.add_parser("send", help="Email all unsent subscribers for one connector.")
    send_p.add_argument("connector_id")
    send_p.add_argument("--apply", action="store_true", help="Actually send. Dry-run is the default.")
    send_p.add_argument("--subject", help="Override the email subject line.")
    send_p.set_defaults(func=_cmd_send)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

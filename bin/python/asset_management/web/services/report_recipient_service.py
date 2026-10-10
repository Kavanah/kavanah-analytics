"""Who may receive a scheduled report.

Before the report scheduler emailed whatever addresses the author
typed, from the platform's trusted sender, with a body built from report
content the author controls. A compromised low-privilege account could
therefore phish every other missionary with SPF/DKIM-passing mail, or ship
report attachments to an outside address, one schedule at a time.

Policy, enforced at DELIVERY time (``ReportEmailService.deliver``), not only
at save — so a schedule edited by hand, or saved before this change, is held
to the same rule:

* **members** — an address that belongs to an Elasticsearch security user who
  shares a workspace with the schedule's creator (or is the creator). Always
  allowed: these people can already see the dashboards.
* **verified externals** — any other address, once its owner has clicked a
  signed verification link we sent them. Verification is per address and
  lives in ``de_report_recipient_verify``; it is requested automatically when
  a schedule is saved with the address.
* **unverified externals** — dropped from the send, named in the delivery
  result and the log, and reported back to the author on save so they know
  delivery will skip them until the owner clicks.

Every external address added to a schedule is written to ``de_audit_log``
(``report_recipient_added``), and a verification click writes
``report_recipient_verified``.

Fails **closed**: if the user directory or the verification index cannot be
read, no address is allowed — a broken dependency must not widen the
recipient list.
"""

from __future__ import annotations

import html as _html
import logging
import time
from datetime import datetime, timezone
from typing import Any

import jwt

from lib.es_helpers import create_index_if_not_exists, get_elasticsearch_client

INDEX = "de_report_recipient_verify"
MAPPING: dict[str, Any] = {
    "properties": {
        "email": {"type": "keyword"},
        "requested_by": {"type": "keyword"},
        "requested_at": {"type": "date"},
        "last_sent_at": {"type": "date"},
        "verified": {"type": "boolean"},
        "verified_at": {"type": "date"},
    }
}

TOKEN_PURPOSE = "report_recipient"
TOKEN_TTL_SECONDS = 7 * 24 * 3600
# Don't re-mail the same external address more often than this when the author
# keeps re-saving the schedule.
RESEND_COOLDOWN_SECONDS = 24 * 3600
VERIFY_PATH = "/report-scheduler/report-recipient/verify/{token}"  # blueprint url_prefix + route

_index_ready = False


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _secret_key() -> str:
    try:
        from asset_management.web.config import config
    except ImportError:  # pragma: no cover - import boundary
        from config import config  # type: ignore[no-redef]
    return str(getattr(config, "SECRET_KEY", "") or "")


def _app_base_url() -> str:
    try:
        try:
            from web.services.billing_email_service import _get_app_base_url
        except ImportError:
            from .billing_email_service import _get_app_base_url
        return (_get_app_base_url(None) or "").rstrip("/")
    except Exception:  # noqa: BLE001 — a missing base URL degrades to a relative link, never blocks
        return ""


class ReportRecipientService:
    """Recipient policy for scheduled report email."""

    # ── infrastructure ────────────────────────────────────────────────────────

    @staticmethod
    def ensure_index(es=None) -> bool:
        global _index_ready
        es = es if es is not None else get_elasticsearch_client()
        if es is None:
            return False
        if _index_ready:
            return True
        try:
            _index_ready = bool(create_index_if_not_exists(es, INDEX, MAPPING))
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.warning("ReportRecipientService: could not ensure %s: %s", INDEX, exc)
            _index_ready = False
        return _index_ready

    @staticmethod
    def _users_by_email(es) -> dict[str, list[str]]:
        """email (lower) -> [usernames] from the ES security realm."""
        out: dict[str, list[str]] = {}
        users = dict(es.security.get_user())
        for uname, u in users.items():
            email = _norm((u or {}).get("email") or "")
            if email:
                out.setdefault(email, []).append(uname)
        return out

    @staticmethod
    def _shares_workspace(username: str, created_by: str, creator_workspaces: list[str]) -> bool:
        if username == created_by:
            return True
        try:
            from web.services.workspace_membership_service import WorkspaceMembershipService
        except ImportError:
            from .workspace_membership_service import WorkspaceMembershipService
        return any(WorkspaceMembershipService.is_member(username, ws) for ws in creator_workspaces)

    @staticmethod
    def _verified_set(es, emails: list[str]) -> set[str]:
        if not emails:
            return set()
        resp = es.search(
            index=INDEX,
            body={
                "size": len(emails),
                "query": {"bool": {"filter": [{"terms": {"email": emails}}, {"term": {"verified": True}}]}},
                "_source": ["email"],
            },
            ignore_unavailable=True,
        )
        return {_norm(h["_source"].get("email")) for h in (resp.get("hits") or {}).get("hits") or []}

    # ── classification ────────────────────────────────────────────────────────

    @staticmethod
    def classify(recipients: list[str], created_by: str) -> dict[str, list[str]]:
        """Split ``recipients`` into members / verified / unverified for the
        schedule created by ``created_by``. Fails closed: on any lookup error
        every address is ``unverified``."""
        emails = []
        seen = set()
        for r in recipients or []:
            e = _norm(r)
            if e and e not in seen:
                seen.add(e)
                emails.append(e)
        result: dict[str, list[str]] = {"members": [], "verified": [], "unverified": []}
        if not emails:
            return result
        creator = (created_by or "").strip()
        es = get_elasticsearch_client()
        if es is None:
            result["unverified"] = list(emails)
            return result
        try:
            try:
                from web.services.workspace_membership_service import WorkspaceMembershipService
            except ImportError:
                from .workspace_membership_service import WorkspaceMembershipService

            creator_ws = list(WorkspaceMembershipService.get_workspaces_for_user(creator) or []) if creator else []
            by_email = ReportRecipientService._users_by_email(es)
            ReportRecipientService.ensure_index(es)
            verified = ReportRecipientService._verified_set(es, emails)
        except Exception as exc:  # noqa: BLE001 — fail closed
            logging.warning("ReportRecipientService.classify: lookup failed (%s) — treating every address as unverified", exc)
            result["unverified"] = list(emails)
            return result

        for e in emails:
            usernames = by_email.get(e, [])
            if creator and any(ReportRecipientService._shares_workspace(u, creator, creator_ws) for u in usernames):
                result["members"].append(e)
            elif e in verified:
                result["verified"].append(e)
            else:
                result["unverified"].append(e)
        return result

    @staticmethod
    def allowed_recipients(schedule: dict) -> tuple[list[str], list[str]]:
        """``(allowed, skipped)`` for a schedule about to be delivered."""
        delivery = (schedule.get("delivery") or {}).get("email") or {}
        recipients = [r for r in (delivery.get("recipients") or []) if r]
        status = ReportRecipientService.classify(recipients, schedule.get("created_by") or "")
        allowed = status["members"] + status["verified"]
        return allowed, status["unverified"]

    # ── save-time hook ────────────────────────────────────────────────────────

    @staticmethod
    def on_schedule_saved(recipients: list[str], principal: str, *, _send_fn: Any = None) -> dict[str, Any]:
        """Classify, request verification for unverified externals (with a
        resend cooldown), audit every external, and return the status plus a
        human ``warning`` for the UI ("" when nothing is pending)."""
        status = ReportRecipientService.classify(recipients, principal)
        for e in status["verified"]:
            ReportRecipientService._audit(principal, "report_recipient_added", True, f"external={e} verified=True")
        for e in status["unverified"]:
            sent = ReportRecipientService.request_verification(e, principal, _send_fn=_send_fn)
            ReportRecipientService._audit(
                principal, "report_recipient_added", True, f"external={e} verified=False verification_sent={sent}"
            )
        warning = ""
        if status["unverified"]:
            listed = ", ".join(status["unverified"])
            warning = (
                f"{len(status['unverified'])} recipient(s) are outside your workspace and have not confirmed their address: "
                f"{listed}. We have emailed them a confirmation link; the report will not be sent to them until they click it."
            )
        return {**status, "warning": warning}

    # ── verification ──────────────────────────────────────────────────────────

    @staticmethod
    def request_verification(email: str, requested_by: str, *, _send_fn: Any = None) -> bool:
        """Email ``email`` a signed confirmation link (unless one went out within
        the cooldown). Returns True when a mail was dispatched."""
        e = _norm(email)
        if not e:
            return False
        es = get_elasticsearch_client()
        if es is None or not ReportRecipientService.ensure_index(es):
            return False
        now = time.time()
        try:
            existing = es.get(index=INDEX, id=e).get("_source") or {}
        except Exception:  # noqa: BLE001 — no prior request
            existing = {}
        if existing.get("verified"):
            return False
        last = existing.get("last_sent_at") or ""
        if last:
            try:
                last_ts = datetime.fromisoformat(last).timestamp()
                if now - last_ts < RESEND_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass

        token = jwt.encode(
            {
                "purpose": TOKEN_PURPOSE,
                "email": e,
                "requested_by": requested_by,
                "iat": int(now),
                "exp": int(now) + TOKEN_TTL_SECONDS,
            },
            _secret_key(),
            algorithm="HS256",
        )
        link = f"{_app_base_url()}{VERIFY_PATH.format(token=token)}"
        who = _html.escape(requested_by or "a Kavanah Analytics user")
        subject = "Confirm your address to receive scheduled reports"
        text = (
            f"{requested_by or 'A Kavanah Analytics user'} wants to send you scheduled reports from Kavanah Analytics.\n\n"
            f"Confirm this address by opening the link below. If you did not expect this, ignore this email — "
            f"nothing will be sent to you.\n\n{link}\n\nThe link expires in 7 days."
        )
        html_body = (
            f"<p>{who} wants to send you scheduled reports from Kavanah Analytics.</p>"
            f"<p>Confirm this address by clicking the link below. If you did not expect this, ignore this email "
            f"&mdash; nothing will be sent to you.</p>"
            f"<p><a href='{_html.escape(link)}'>Confirm this address</a></p>"
            f"<p style='color:#5F6B7A'>The link expires in 7 days.</p>"
        )
        try:
            try:
                from web.services.report_email_service import ReportEmailService
            except ImportError:
                from .report_email_service import ReportEmailService
            res = ReportEmailService.send_report(to_email=e, subject=subject, html=html_body, text=text, _send_fn=_send_fn)
            sent = bool((res or {}).get("success"))
        except Exception as exc:  # noqa: BLE001 — the save must not fail because a courtesy mail did
            logging.warning("ReportRecipientService: verification mail to %s failed: %s", e, exc)
            sent = False
        doc = {
            "email": e,
            "requested_by": requested_by,
            "requested_at": existing.get("requested_at") or _now_iso(),
            "verified": False,
        }
        if sent:
            doc["last_sent_at"] = _now_iso()
        try:
            es.update(index=INDEX, id=e, body={"doc": doc, "doc_as_upsert": True}, refresh="wait_for")
        except Exception as exc:  # noqa: BLE001 — record is best-effort; the send already happened
            logging.warning("ReportRecipientService: could not record request for %s: %s", e, exc)
        return sent

    @staticmethod
    def verify(token: str) -> dict[str, Any]:
        """Consume a confirmation link. ``{"ok": True, "email": ...}`` or
        ``{"ok": False, "error": ...}``."""
        try:
            payload = jwt.decode(token or "", _secret_key(), algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return {
                "ok": False,
                "error": "This confirmation link has expired. Ask the report's author to save the schedule again.",
            }
        except jwt.InvalidTokenError:
            return {"ok": False, "error": "This confirmation link is invalid."}
        if payload.get("purpose") != TOKEN_PURPOSE:
            return {"ok": False, "error": "This confirmation link is invalid."}
        e = _norm(payload.get("email") or "")
        if not e:
            return {"ok": False, "error": "This confirmation link is invalid."}
        es = get_elasticsearch_client()
        if es is None or not ReportRecipientService.ensure_index(es):
            return {"ok": False, "error": "The service is temporarily unavailable. Please try the link again later."}
        try:
            es.update(
                index=INDEX,
                id=e,
                body={
                    "doc": {
                        "email": e,
                        "verified": True,
                        "verified_at": _now_iso(),
                        "requested_by": payload.get("requested_by") or "",
                    },
                    "doc_as_upsert": True,
                },
                refresh="wait_for",
            )
        except Exception as exc:  # noqa: BLE001 — boundary
            logging.error("ReportRecipientService.verify: write failed for %s: %s", e, exc)
            return {"ok": False, "error": "The service is temporarily unavailable. Please try the link again later."}
        ReportRecipientService._audit(payload.get("requested_by") or "-", "report_recipient_verified", True, f"external={e}")
        return {"ok": True, "email": e}

    # ── audit ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _audit(username: str, action: str, success: bool, detail: str) -> None:
        logging.info("AUDIT: user=%s action=%s success=%s detail=%s", username or "-", action, success, detail)
        try:
            try:
                from web.services.audit_log_service import AuditLogService
            except ImportError:
                from .audit_log_service import AuditLogService
            AuditLogService.log_event(username or "-", action, success, detail=detail)
        except Exception as exc:  # noqa: BLE001 — audit is best-effort
            logging.warning("ReportRecipientService: audit write failed for %s: %s", action, exc)

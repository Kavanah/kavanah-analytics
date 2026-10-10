"""
Audit Log Service
Writes and queries user activity events in Elasticsearch.
Used by the Login Activity tab on the User Management page.
"""

import html
import json
import logging
import os
import re
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

INDEX = "de_audit_log"

INDEX_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "username": {"type": "keyword"},
        "action": {"type": "keyword"},
        "success": {"type": "boolean"},
        "auth_method": {"type": "keyword"},
        "mfa_method": {"type": "keyword"},
        "ip_address": {"type": "ip"},
        "user_agent": {
            "type": "text",
            "fields": {"keyword": {"type": "keyword", "ignore_above": 512}},
        },
        "failure_reason": {"type": "keyword"},
        "target_user": {"type": "keyword"},
        "detail": {"type": "text"},
        "is_scanner": {"type": "boolean"},
        "suspicious": {"type": "boolean"},
        # entry-point cohort field. Stamped on action="login"
        # events from the user's ES Security metadata. Unset on other
        # actions so cohort aggregations only see meaningful values.
        "account_type": {"type": "keyword"},
        # org ESID of the auto-join match for the two
        # onboarding_domain_join* actions. Unset on other actions, so
        # a term aggregation on this field naturally produces a
        # per-org breakdown of the funnel without needing to
        # parse the free-text ``detail`` string at query time.
        "matched_org_esid": {"type": "keyword"},
        # structured context for the ``permission.denied`` action.
        # Stamped only on denial events emitted by ``_require_permission``
        # so a misconfigured grant (or a probing scoped admin) surfaces as
        # a queryable {permission, scope_type, scope_esid, route} tuple
        # rather than a free-text ``detail`` string. Unset on every other
        # action — term aggregations on these fields see only denial rows.
        "permission": {"type": "keyword"},
        "scope_type": {"type": "keyword"},
        "scope_esid": {"type": "keyword"},
        "route": {"type": "keyword"},
    }
}

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"

# Usernames that only contain word characters (letters, digits, underscores,
# hyphens, dots) are considered human.  Anything else is scanner/injection.
_HUMAN_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]+$")

# Substrings in the User-Agent header that identify automated scanners.
_SCANNER_UA_SUBSTRINGS = ["zap", "owasp", "nikto", "burp", "sqlmap", "nessus"]


class AuditLogService:

    # -------------------------------------------------------------------------
    # Index bootstrap
    # -------------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the audit log index (with a high-priority template to avoid
        the single-node data-stream template) if it does not already exist."""
        from lib.es_helpers import (
            get_elasticsearch_client,
            put_regular_index_template,
            create_index_if_not_exists,
        )

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("AuditLogService: could not connect to ES")
            return

        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)

        # Ensure new fields (e.g. is_scanner) are added to an existing index
        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("AuditLogService: put_mapping update: %s", e)

    # -------------------------------------------------------------------------
    # Retention cleanup
    # -------------------------------------------------------------------------

    @staticmethod
    def delete_older_than(days: int = 90) -> int:
        """Delete audit events older than the given number of days.

        Returns the number of deleted documents, or -1 on error.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return -1

        try:
            # proceed past docs the other gunicorn worker already deleted at startup (avoid 409).
            result = es.delete_by_query(
                index=INDEX,
                body={"query": {"range": {"@timestamp": {"lt": f"now-{days}d"}}}},
                conflicts="proceed",
            )
            deleted = result.get("deleted", 0)
            if deleted:
                logging.info(
                    "AuditLogService: purged %d events older than %d days",
                    deleted,
                    days,
                )
            return deleted
        except Exception as e:
            logging.error("AuditLogService: retention cleanup failed: %s", e)
            return -1

    # -------------------------------------------------------------------------
    # Scanner tagging
    # -------------------------------------------------------------------------

    @staticmethod
    def _is_scanner(
        username: str,
        user_agent: str = "",
        known_users: set | None = None,
        success: bool = False,
    ) -> bool:
        """Return True if the request looks like automated scanner traffic.

        Checks three signals:
        1. Username contains non-human characters (injection payloads)
        2. User-Agent contains a known scanner identifier
        3. Username is not a known ES user AND login failed

        Successful logins are never tagged as scanner — if an unknown user
        manages to authenticate, that's a security event worth seeing.
        """
        if username and not _HUMAN_USERNAME_RE.match(username):
            return True
        ua_lower = user_agent.lower()
        if any(s in ua_lower for s in _SCANNER_UA_SUBSTRINGS):
            return True
        if not success and known_users is not None and username and username not in known_users:
            return True
        return False

    @staticmethod
    def _get_known_usernames() -> set:
        """Fetch all ES security usernames for scanner detection."""
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return set()
        try:
            users = es.security.get_user()
            return set(dict(users).keys())
        except Exception:
            return set()

    @staticmethod
    def backfill_scanner_tags() -> int:
        """Re-tag all docs with is_scanner based on username + user_agent.

        Scans every document in the index, applies the Python-side
        _is_scanner() check, and bulk-updates any doc whose tag is
        missing or wrong.  Safe to call on every startup.

        Returns the number of updated documents, or -1 on error.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return -1

        try:
            # Scroll through all docs
            result = es.search(
                index=INDEX,
                body={"query": {"match_all": {}}, "size": 1000},
                scroll="2m",
            )
            scroll_id = result.get("_scroll_id")
            all_hits = result["hits"]["hits"]

            while True:
                page = es.scroll(scroll_id=scroll_id, scroll="2m")
                hits = page["hits"]["hits"]
                if not hits:
                    break
                all_hits.extend(hits)

            # Build bulk update for docs that need tagging
            known_users = AuditLogService._get_known_usernames()
            bulk_body: list[dict] = []
            for hit in all_hits:
                src = hit["_source"]
                expected = AuditLogService._is_scanner(
                    src.get("username", ""),
                    src.get("user_agent", ""),
                    known_users,
                    success=src.get("success", False),
                )
                if src.get("is_scanner") != expected:
                    bulk_body.append({"update": {"_index": INDEX, "_id": hit["_id"]}})
                    bulk_body.append({"doc": {"is_scanner": expected}})

            if not bulk_body:
                return 0

            es.bulk(body=bulk_body, refresh="wait_for")
            updated = len(bulk_body) // 2
            logging.info("AuditLogService: backfilled is_scanner on %d docs", updated)
            return updated
        except Exception as e:
            logging.error("AuditLogService: backfill_scanner_tags failed: %s", e)
            return -1

    # -------------------------------------------------------------------------
    # Security alerts
    # -------------------------------------------------------------------------

    @staticmethod
    def _send_security_alert(username: str, ip_address: str, auth_method: str) -> None:
        """Send an email alert when an unknown user logs in successfully.

        Reads SMTP config from the password-reset settings file and sends
        to the address in SECURITY_ALERT_EMAIL env var.  Fire-and-forget.
        """
        recipient = os.environ.get("SECURITY_ALERT_EMAIL", "")
        if not recipient:
            logging.warning("AuditLogService: SECURITY_ALERT_EMAIL not set, skipping alert")
            return

        settings = load_smtp_settings("AuditLogService")

        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        if any(not settings.get(k) for k in required):
            logging.error("AuditLogService: SMTP not configured, cannot send alert")
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        subject = f"SECURITY ALERT: Unknown user '{username}' logged in successfully"

        app_url = settings.get("app_url", "").rstrip("/")
        if app_url:
            from urllib.parse import urlparse as _urlparse

            _parsed = _urlparse(app_url)
            logo_url = f"{_parsed.scheme}://{_parsed.netloc}/static/kavanah-analytics-logo.svg"
        else:
            logo_url = ""

        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>{html.escape(subject)}</title>
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
      <tr><td style="padding:28px 28px 8px;background-color:#ffffff;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FDF3F1;border:2px solid #D91515;border-radius:8px;">
          <tr><td style="padding:20px;">
            <h2 style="margin:0 0 8px;color:#D91515;font-size:18px;font-weight:700;">&#9888; Unknown User Login Detected</h2>
            <p style="margin:0;color:#D91515;font-size:14px;line-height:1.5;">A user that does not exist in Elasticsearch Security successfully authenticated. This should not happen under normal circumstances.</p>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:8px 28px;background-color:#ffffff;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FAFAFA;border:1px solid #D5DBDB;border-radius:6px;">
          <tr><td style="padding:14px 18px;font-family:'Courier New',Consolas,monospace;font-size:14px;color:#000716;">
            <p style="margin:4px 0;"><strong>Username:</strong> {html.escape(username or "")}</p>
            <p style="margin:4px 0;"><strong>IP Address:</strong> {html.escape(ip_address or 'unknown')}</p>
            <p style="margin:4px 0;"><strong>Auth Method:</strong> {html.escape(auth_method or 'unknown')}</p>
            <p style="margin:4px 0;"><strong>Time:</strong> {timestamp}</p>
          </td></tr>
        </table>
      </td></tr>
      <tr><td style="padding:8px 28px 20px;background-color:#ffffff;color:#000716;font-size:14px;line-height:1.6;">
        <p style="margin:12px 0 8px;"><strong>Recommended actions:</strong></p>
        <ul style="margin:0;padding-left:22px;">
          <li style="margin-bottom:4px;">Check the Login Activity tab in User Management for details</li>
          <li style="margin-bottom:4px;">Verify this user was not provisioned outside the normal flow</li>
          <li style="margin-bottom:4px;">If unauthorized, rotate credentials and investigate the auth method</li>
        </ul>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        This alert was sent by Kavanah Analytics (DEAM).
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"SECURITY ALERT: Unknown user login detected\n\n"
            f"Username: {username}\n"
            f"IP Address: {ip_address or 'unknown'}\n"
            f"Auth Method: {auth_method or 'unknown'}\n"
            f"Time: {timestamp}\n\n"
            f"This user does not exist in Elasticsearch Security.\n"
            f"Check the Login Activity tab in User Management for details."
        )

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings["from_email"]
        msg["To"] = recipient
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
            server.sendmail(settings["from_email"], recipient, msg.as_string())
            server.quit()
            logging.info(
                "AuditLogService: security alert sent to %s for user '%s'",
                recipient,
                username,
            )
        except Exception as e:
            logging.error("AuditLogService: failed to send security alert: %s", e)

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    @staticmethod
    def log_event(
        username: str,
        action: str,
        success: bool,
        *,
        auth_method: str = "",
        mfa_method: str = "",
        ip_address: str = "",
        user_agent: str = "",
        failure_reason: str = "",
        target_user: str = "",
        detail: str = "",
        matched_org_esid: str = "",
        permission: str = "",
        scope_type: str = "",
        scope_esid: str = "",
        route: str = "",
    ) -> None:
        """Write a single audit event to Elasticsearch.

        This is fire-and-forget — failures are logged but never raised so that
        the caller's request is not interrupted.

        ``permission`` / ``scope_type`` / ``scope_esid`` / ``route``
        carry the structured context for ``permission.denied`` events (see
        :meth:`log_permission_denied`). They are unset on every other action.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            # a dropped audit row used to vanish without a trace. The
            # write stays fire-and-forget (never raise into the caller), but the
            # loss is now an ERROR line an operator can find in the container
            # log, with enough of the event to reconstruct it by hand.
            logging.error(
                "AuditLogService: Elasticsearch unavailable — audit event DROPPED: "
                "user=%s action=%s success=%s target=%s detail=%s",
                username,
                action,
                success,
                target_user,
                (detail or "")[:200],
            )
            return

        # Check if this is a suspicious login (unknown user succeeded)
        suspicious = False
        if success and action == "login" and username:
            known_users = AuditLogService._get_known_usernames()
            if known_users and username not in known_users:
                suspicious = True
                logging.warning(
                    "AUDIT: SUSPICIOUS LOGIN — unknown user '%s' authenticated " "from %s via %s",
                    username,
                    ip_address,
                    auth_method,
                )
                AuditLogService._send_security_alert(username, ip_address, auth_method)

        # (part 2): distinguish a failed login against an
        # unknown username from a genuine wrong-password. The route
        # cannot tell these apart (authenticate_user returns the same
        # failure for both), so a mobile-mangled username (part
        # 1) currently looks identical to a real wrong-password event in
        # the Login Activity tab. When the caller reports a generic
        # ``invalid_credentials`` failure for a ``login`` action and the
        # submitted username is not a known ES Security user, downgrade
        # the reason to ``username_unknown``. This is a server-side audit
        # refinement only — the HTTP response is unchanged, so login
        # stays a non-oracle for account existence (no enumeration leak).
        # Fail-soft: an empty/failed known-user lookup keeps the original
        # reason (never crash the audit write over a diagnostic tag).
        if not success and action == "login" and username and failure_reason == "invalid_credentials":
            known_users = AuditLogService._get_known_usernames()
            if known_users and username not in known_users:
                failure_reason = "username_unknown"

        # stamp entry-point cohort on login events so we can
        # answer "how many invited-users logged in this week?" without
        # joining audit_log against the user record at query time.
        # Failure is non-fatal — the audit write must always proceed.
        account_type = None
        if action == "login" and username:
            try:
                from asset_management.web.services.user_profile_service import (
                    UserProfileService,
                )

                detailed = UserProfileService.get_account_type_detailed(username)
                if detailed.get("success") and not detailed.get("is_default"):
                    account_type = detailed.get("account_type")
            except Exception as e:
                logging.warning("AuditLogService: account_type lookup failed: %s", e)

        doc: dict = {
            "@timestamp": datetime.now(timezone.utc).isoformat(),
            "username": username,
            "action": action,
            "success": success,
            "is_scanner": AuditLogService._is_scanner(username, user_agent, success=success),
            "suspicious": suspicious,
        }
        if account_type:
            doc["account_type"] = account_type
        if auth_method:
            doc["auth_method"] = auth_method
        if mfa_method:
            doc["mfa_method"] = mfa_method
        if ip_address:
            doc["ip_address"] = ip_address
        if user_agent:
            doc["user_agent"] = user_agent
        if failure_reason:
            doc["failure_reason"] = failure_reason
        if target_user:
            doc["target_user"] = target_user
        if detail:
            doc["detail"] = detail
        if matched_org_esid:
            doc["matched_org_esid"] = matched_org_esid
        if permission:
            doc["permission"] = permission
        if scope_type:
            doc["scope_type"] = scope_type
        if scope_esid:
            doc["scope_esid"] = scope_esid
        if route:
            doc["route"] = route

        try:
            es.index(index=INDEX, document=doc)
        except Exception as e:
            logging.error("AuditLogService: failed to write event: %s", e)

    @staticmethod
    def _request_username() -> str:
        """Best-effort actor resolution for service-layer emitters.

        Returns the session username when called inside a Flask request,
        else "". Services that strip asset references run both
        under web requests and from CLI/migration contexts, so the actor
        cannot always be threaded down explicitly. Attribution metadata
        only — never used for authorization (which must stay explicit,
        per the allowed_workspaces threading convention).
        """
        try:
            from flask import has_request_context, session

            if has_request_context():
                return str(session.get("username") or "")
        except Exception:  # noqa: BLE001 — attribution is best-effort by contract
            return ""
        return ""

    @staticmethod
    def log_reference_removed(
        *,
        path: str,
        child_index: str,
        child_id: str,
        removed_esid: str,
        field: str = "",
        child_title: str = "",
        removed_title: str = "",
        source_index: str = "",
        connector: str = "",
        username: str = "",
    ) -> None:
        """record an ``asset_reference.removed`` audit event.

        Emitted whenever a document's link to another asset is stripped —
        the delete-cascade (``ReferenceCleanupService``), the wizard's
        configure-time dangling prune, and the Data Connectors
        page's explicit disconnect / per-account remove (/
        ). One event per (child doc, removed esid), so "why did
        my channel disappear from team X" is answerable with a
        ``match_phrase`` on ``detail``.

        ``path`` discriminates the writer: ``delete_cascade`` |
        ``configure_prune`` | ``account_remove`` | ``disconnect``.
        ``child_*`` name the doc that LOST the reference (usually a team);
        ``removed_esid``/``removed_title`` name the reference that was
        stripped; ``source_index`` is the index the esid pointed into.

        Thin wrapper over :meth:`log_event` — same fire-and-forget
        contract; never raises.
        """
        if not username:
            username = AuditLogService._request_username() or "system"
        parts = [f"path={path}", f"child={child_index}/{child_id}"]
        if child_title:
            parts.append(f"child_title={child_title!r}")
        if field:
            parts.append(f"field={field}")
        parts.append(f"removed={removed_esid}")
        if removed_title:
            parts.append(f"removed_title={removed_title!r}")
        if source_index:
            parts.append(f"source={source_index}")
        if connector:
            parts.append(f"connector={connector}")
        AuditLogService.log_event(
            username,
            "asset_reference.removed",
            True,
            detail=" ".join(parts),
        )

    @staticmethod
    def log_permission_denied(
        username: str,
        *,
        permission: str,
        scope_type: str,
        scope_esid: str,
        route: str,
        ip_address: str = "",
        user_agent: str = "",
    ) -> None:
        """record a ``permission.denied`` audit event.

        Emitted by ``_require_permission`` whenever an authenticated,
        non-cluster-admin user fails the per-scope capability check on a
        destructive ``/api/user-management/*`` route. The structured
        ``{permission, scope_type, scope_esid, route}`` tuple lets a
        coverage query distinguish a misconfigured grant (a legitimate
        operator unexpectedly denied) from a scoped admin probing a route
        outside their scope — the signal the shadow WARNING was a
        stand-in for.

        Thin wrapper over :meth:`log_event` (``success=False``) — same
        fire-and-forget contract; never raises.
        """
        AuditLogService.log_event(
            username,
            "permission.denied",
            False,
            ip_address=ip_address,
            user_agent=user_agent,
            permission=permission,
            scope_type=scope_type,
            scope_esid=scope_esid,
            route=route,
        )

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    @staticmethod
    def query_events(
        *,
        username: str = "",
        action: str = "",
        success: bool | None = None,
        exclude_scanners: bool = False,
        username_in: list[str] | None = None,
        size: int = 100,
        from_offset: int = 0,
    ) -> dict:
        """Query audit events with optional filters.

        Args:
            exclude_scanners: When True, filters out events tagged as
                automated scanner traffic (is_scanner=true).

        Returns:
            dict with 'events' list and 'total' count.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return {"events": [], "total": 0}

        filters: list[dict] = []
        if username:
            filters.append({"term": {"username": username}})
        if action:
            filters.append({"term": {"action": action}})
        if success is not None:
            filters.append({"term": {"success": success}})
        if exclude_scanners:
            # Exclude docs tagged as scanner. Also include docs where
            # is_scanner hasn't been set yet (backfill may not have run).
            filters.append(
                {
                    "bool": {
                        "should": [
                            {"term": {"is_scanner": False}},
                            {"bool": {"must_not": [{"exists": {"field": "is_scanner"}}]}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            )
        if username_in is not None:
            # Empty list deliberately matches zero docs.
            filters.append({"terms": {"username": username_in}})

        query = {"bool": {"filter": filters}} if filters else {"match_all": {}}

        try:
            result = es.search(
                index=INDEX,
                body={
                    "query": query,
                    "sort": [{"@timestamp": {"order": "desc"}}],
                    "size": size,
                    "from": from_offset,
                },
            )
            hits = result.get("hits", {})
            total = hits.get("total", {})
            total_value = total.get("value", 0) if isinstance(total, dict) else total

            events = []
            for hit in hits.get("hits", []):
                evt = hit["_source"]
                evt["_id"] = hit["_id"]
                events.append(evt)

            return {"events": events, "total": total_value}
        except Exception as e:
            logging.error("AuditLogService: query failed: %s", e)
            return {"events": [], "total": 0}

    @staticmethod
    def daily_unique_logins(
        days: int = 30,
        username_whitelist: set | None = None,
    ) -> list[dict]:
        """Return daily unique-user login counts for the last N days.

        Counts distinct usernames per UTC calendar day where action=login,
        success=true, and is_scanner is not true. Days with zero logins
        are included so the chart has continuous x-axis points.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []

        filters: list[dict] = [
            {"term": {"action": "login"}},
            {"term": {"success": True}},
            {"range": {"@timestamp": {"gte": f"now-{days}d/d"}}},
            {
                "bool": {
                    "should": [
                        {"term": {"is_scanner": False}},
                        {"bool": {"must_not": [{"exists": {"field": "is_scanner"}}]}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        ]
        if username_whitelist is not None:
            filters.append({"terms": {"username": list(username_whitelist)}})

        try:
            result = es.search(
                index=INDEX,
                body={
                    "size": 0,
                    "query": {"bool": {"filter": filters}},
                    "aggs": {
                        "per_day": {
                            "date_histogram": {
                                "field": "@timestamp",
                                "calendar_interval": "1d",
                                "min_doc_count": 0,
                                "extended_bounds": {
                                    "min": f"now-{days - 1}d/d",
                                    "max": "now/d",
                                },
                            },
                            "aggs": {"unique_users": {"cardinality": {"field": "username"}}},
                        }
                    },
                },
            )
            buckets = result.get("aggregations", {}).get("per_day", {}).get("buckets", [])
            return [
                {
                    "date": b.get("key_as_string", "")[:10],
                    "count": b.get("unique_users", {}).get("value", 0),
                }
                for b in buckets
            ]
        except Exception as e:
            logging.error("AuditLogService: daily_unique_logins failed: %s", e)
            return []

    @staticmethod
    def logins_for_day(
        date: str,
        username_whitelist: set | None = None,
    ) -> list[dict]:
        """Return per-user login summary for a given UTC date (YYYY-MM-DD).

        One entry per distinct username with: login_count, first_login,
        last_login. Only counts successful, non-scanner login events.
        """
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return []

        filters: list[dict] = [
            {"term": {"action": "login"}},
            {"term": {"success": True}},
            {
                "range": {
                    "@timestamp": {
                        "gte": f"{date}T00:00:00Z",
                        "lt": f"{date}T00:00:00Z||+1d",
                    }
                }
            },
            {
                "bool": {
                    "should": [
                        {"term": {"is_scanner": False}},
                        {"bool": {"must_not": [{"exists": {"field": "is_scanner"}}]}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        ]
        if username_whitelist is not None:
            filters.append({"terms": {"username": list(username_whitelist)}})

        try:
            result = es.search(
                index=INDEX,
                body={
                    "size": 0,
                    "query": {"bool": {"filter": filters}},
                    "aggs": {
                        "per_user": {
                            "terms": {
                                "field": "username",
                                "size": 1000,
                                "order": {"_count": "desc"},
                            },
                            "aggs": {
                                "first_login": {"min": {"field": "@timestamp"}},
                                "last_login": {"max": {"field": "@timestamp"}},
                            },
                        }
                    },
                },
            )
            buckets = result.get("aggregations", {}).get("per_user", {}).get("buckets", [])
            return [
                {
                    "username": b.get("key", ""),
                    "login_count": b.get("doc_count", 0),
                    "first_login": b.get("first_login", {}).get("value_as_string"),
                    "last_login": b.get("last_login", {}).get("value_as_string"),
                }
                for b in buckets
            ]
        except Exception as e:
            logging.error("AuditLogService: logins_for_day failed: %s", e)
            return []

    @staticmethod
    def active_usernames(
        minutes: int | None = None,
        username_whitelist: set | None = None,
    ) -> set[str]:
        """Return usernames whose session could plausibly still be valid.

        A login counts as active when:
        - it is the most recent login for that user,
        - there is no later logout event,
        - its age is less than ``SESSION_MAX_LIFETIME`` (absolute cap), and
        - its age is less than ``SESSION_LIFETIME`` (idle timeout — proxy
          for activity, since per-request activity is not logged).

        ``minutes`` is an optional debug override of the ES search window.
        When omitted, the window is derived from ``SESSION_MAX_LIFETIME``
        so the count stays aligned with session model. The
        in-Python idle/max-lifetime checks always run regardless.
        """
        from lib.es_helpers import get_elasticsearch_client
        from asset_management.web.config import config

        es = get_elasticsearch_client()
        if es is None:
            return set()

        idle_seconds = config.SESSION_LIFETIME
        max_seconds = config.SESSION_MAX_LIFETIME
        if minutes is None:
            minutes = max(1, max_seconds // 60)

        filters: list[dict] = [
            {"terms": {"action": ["login", "logout"]}},
            {"range": {"@timestamp": {"gte": f"now-{minutes}m"}}},
            {
                "bool": {
                    "should": [
                        {"term": {"is_scanner": False}},
                        {"bool": {"must_not": [{"exists": {"field": "is_scanner"}}]}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        ]
        if username_whitelist is not None:
            filters.append({"terms": {"username": list(username_whitelist)}})

        try:
            result = es.search(
                index=INDEX,
                body={
                    "size": 0,
                    "query": {"bool": {"filter": filters}},
                    "aggs": {
                        "per_user": {
                            "terms": {"field": "username", "size": 1000},
                            "aggs": {
                                "last_login": {
                                    "filter": {
                                        "bool": {
                                            "filter": [
                                                {"term": {"action": "login"}},
                                                {"term": {"success": True}},
                                            ]
                                        }
                                    },
                                    "aggs": {"latest": {"max": {"field": "@timestamp"}}},
                                },
                                "last_logout": {
                                    "filter": {"term": {"action": "logout"}},
                                    "aggs": {"latest": {"max": {"field": "@timestamp"}}},
                                },
                            },
                        }
                    },
                },
            )
            buckets = result.get("aggregations", {}).get("per_user", {}).get("buckets", [])
            now_ms = datetime.now(timezone.utc).timestamp() * 1000
            active: set[str] = set()
            for b in buckets:
                username = b.get("key", "")
                last_login = b.get("last_login", {}).get("latest", {}).get("value")
                if last_login is None:
                    continue
                age_ms = now_ms - last_login
                if age_ms >= max_seconds * 1000:
                    continue
                if age_ms >= idle_seconds * 1000:
                    continue
                last_logout = b.get("last_logout", {}).get("latest", {}).get("value")
                if last_logout is None or last_login > last_logout:
                    active.add(username)
            return active
        except Exception as e:
            logging.error("AuditLogService: active_usernames failed: %s", e)
            return set()

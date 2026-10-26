"""
Feedback Service
Manages system settings (feedback_email) in Elasticsearch and sends
user-submitted feedback (bug reports / feature requests) via SMTP.
"""

import io
import json
import logging
import re
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

INDEX = "de_system_settings"
DOC_ID = "global_settings"

INDEX_MAPPING = {
    "properties": {
        "@timestamp": {"type": "date"},
        "feedback_email": {"type": "keyword"},
        "updated_by": {"type": "keyword"},
    }
}

try:
    from web.services.smtp_settings import load_smtp_settings
except ImportError:
    from ..services.smtp_settings import load_smtp_settings

_PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"


def _get_smtp_settings():
    """Env-aware shared SMTP settings: PW_RESET_SMTP_* env
    credentials win over settings.json, which no longer stores them."""
    return load_smtp_settings("FeedbackService")


def _get_app_base_url() -> str:
    """Return scheme://host of the main app, derived from SMTP settings."""
    app_url = _get_smtp_settings().get("app_url", "").rstrip("/")
    if not app_url:
        return ""
    from urllib.parse import urlparse

    parsed = urlparse(app_url)
    return f"{parsed.scheme}://{parsed.netloc}"


class FeedbackService:

    # ------------------------------------------------------------------
    # Index bootstrap
    # ------------------------------------------------------------------

    @staticmethod
    def ensure_index_exists() -> None:
        """Create the system settings index if it does not exist."""
        from lib.es_helpers import (
            get_elasticsearch_client,
            put_regular_index_template,
            create_index_if_not_exists,
        )

        es = get_elasticsearch_client()
        if es is None:
            logging.warning("FeedbackService: could not connect to ES")
            return

        put_regular_index_template(es, INDEX, INDEX_MAPPING, priority=500)
        create_index_if_not_exists(es, INDEX, INDEX_MAPPING)

        try:
            es.indices.put_mapping(index=INDEX, body=INDEX_MAPPING)
        except Exception as e:
            logging.warning("FeedbackService: put_mapping update: %s", e)

    # ------------------------------------------------------------------
    # Settings CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def get_feedback_email(es) -> str | None:
        """Return the configured feedback email, or None if not set."""
        try:
            doc = es.get(index=INDEX, id=DOC_ID)
            return doc["_source"].get("feedback_email") or None
        except Exception:
            return None

    @staticmethod
    def save_feedback_email(es, email: str, updated_by: str) -> dict:
        """Save (upsert) the feedback email address."""
        try:
            es.index(
                index=INDEX,
                id=DOC_ID,
                body={
                    "@timestamp": datetime.now(timezone.utc).isoformat(),
                    "feedback_email": email.strip(),
                    "updated_by": updated_by,
                },
            )
            return {"success": True}
        except Exception as e:
            logging.error("FeedbackService: save_feedback_email failed: %s", e)
            return {"success": False, "error": "Could not save the feedback email."}

    # ------------------------------------------------------------------
    # Send feedback email
    # ------------------------------------------------------------------

    @staticmethod
    def send_feedback_email(es, feedback_data: dict) -> dict:
        """
        Send a feedback email to the configured feedback_email address.

        feedback_data keys:
            type        - "bug" or "feature"
            subject     - short summary
            description - detailed description
            steps       - steps to reproduce (bugs only, may be empty)
            priority    - "low", "medium", or "high"
            user_name   - submitter's display name
            user_email  - submitter's email address
        """
        feedback_email_raw = FeedbackService.get_feedback_email(es)
        if not feedback_email_raw:
            return {"success": False, "error": "Feedback email not configured"}

        # Support comma-separated recipients
        recipients = [e.strip() for e in feedback_email_raw.split(",") if e.strip()]
        if not recipients:
            return {"success": False, "error": "Feedback email not configured"}

        settings = _get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing:
            return {
                "success": False,
                "error": f"SMTP not configured (missing: {', '.join(missing)})",
            }

        fb_type = feedback_data.get("type", "bug")
        type_label = "Bug Report" if fb_type == "bug" else "Feature Request"
        subject_text = feedback_data.get("subject", "(no subject)")
        description = feedback_data.get("description", "")
        steps = feedback_data.get("steps", "")
        priority = feedback_data.get("priority", "medium").capitalize()
        user_name = feedback_data.get("user_name", "Unknown")
        user_email = feedback_data.get("user_email", "")

        email_subject = f"[Kavanah Analytics] {type_label}: {subject_text}"

        # Build steps section for bugs
        steps_html = ""
        steps_text = ""
        if fb_type == "bug" and steps:
            steps_html = f"""
            <tr>
                <td style="padding:10px 14px;font-weight:600;color:#5F6B7A;vertical-align:top;white-space:nowrap">Steps to Reproduce</td>
                <td style="padding:10px 14px;color:#000716;white-space:pre-wrap">{_esc(steps)}</td>
            </tr>"""
            steps_text = f"\nSteps to Reproduce:\n{steps}\n"

        # Priority badge palette — Cloudscape status + notice tokens.
        priority_color = {"Low": "#037F0C", "Medium": "#F59E0B", "High": "#D91515"}.get(priority, "#F59E0B")

        # Optional Jira link row
        jira_url = feedback_data.get("jira_issue_url")
        jira_error = feedback_data.get("jira_error")
        jira_html = ""
        jira_text = ""
        if jira_url:
            jira_html = (
                f"\n            <tr>"
                f'<td style="padding:10px 14px;font-weight:600;color:#5F6B7A;white-space:nowrap">Jira Issue</td>'
                f'<td style="padding:10px 14px"><a href="{_esc(jira_url)}" style="color:#0972D3">{_esc(jira_url)}</a></td>'
                f"</tr>"
            )
            jira_text = f"\nJira Issue: {jira_url}\n"
        elif jira_error:
            # a failed Jira filing used to drop this row silently, so a
            # deactivated feedback Jira account went unnoticed for weeks. Say so
            # in the email the team actually reads.
            jira_note = f"NOT created ({jira_error}). File this report in Jira by hand and check the feedback Jira account."
            jira_html = (
                f"\n            <tr>"
                f'<td style="padding:10px 14px;font-weight:600;color:#5F6B7A;white-space:nowrap">Jira Issue</td>'
                f'<td style="padding:10px 14px;color:#D91515;font-weight:600">{_esc(jira_note)}</td>'
                f"</tr>"
            )
            jira_text = f"\nJira Issue: {jira_note}\n"

        logo_url = f"{_get_app_base_url()}/static/kavanah-analytics-logo.svg"

        html_body = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="color-scheme" content="light only">
<meta name="supported-color-schemes" content="light only">
<title>{type_label}</title>
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
      <tr><td style="padding:28px 28px 16px;background-color:#ffffff;color:#000716;">
        <h2 style="margin:0 0 4px;color:#000716;font-size:20px;font-weight:700;">{type_label}</h2>
        <p style="margin:0;color:#5F6B7A;font-size:14px;">Submitted through Kavanah Analytics</p>
      </td></tr>
      <tr><td style="padding:0 28px 8px;background-color:#ffffff;">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;border:1px solid #D5DBDB;border-radius:6px;overflow:hidden;">
          <tr style="background-color:#FAFAFA">
            <td style="padding:10px 14px;font-weight:600;color:#5F6B7A;white-space:nowrap;font-size:14px;">Type</td>
            <td style="padding:10px 14px;color:#000716;font-size:14px;">{type_label}</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;font-weight:600;color:#5F6B7A;white-space:nowrap;font-size:14px;">Priority</td>
            <td style="padding:10px 14px;font-size:14px;"><span style="display:inline-block;padding:2px 10px;border-radius:12px;font-size:13px;font-weight:600;color:#ffffff;background-color:{priority_color}">{priority}</span></td>
          </tr>
          <tr style="background-color:#FAFAFA">
            <td style="padding:10px 14px;font-weight:600;color:#5F6B7A;white-space:nowrap;font-size:14px;">Subject</td>
            <td style="padding:10px 14px;color:#000716;font-size:14px;">{_esc(subject_text)}</td>
          </tr>
          <tr>
            <td style="padding:10px 14px;font-weight:600;color:#5F6B7A;vertical-align:top;white-space:nowrap;font-size:14px;">Description</td>
            <td style="padding:10px 14px;color:#000716;white-space:pre-wrap;font-size:14px;">{_esc(description)}</td>
          </tr>{steps_html}{jira_html}
          <tr style="background-color:#FAFAFA">
            <td style="padding:10px 14px;font-weight:600;color:#5F6B7A;white-space:nowrap;font-size:14px;">Submitted By</td>
            <td style="padding:10px 14px;color:#000716;font-size:14px;">{_esc(user_name)} ({_esc(user_email)})</td>
          </tr>
        </table>
      </td></tr>
      <tr><td style="padding:16px 28px 24px;border-top:1px solid #e9ebed;background-color:#ffffff;color:#5F6B7A;font-size:12px;line-height:1.5;border-radius:0 0 8px 8px;">
        This is an automated message — please do not reply.
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""

        text_body = (
            f"{type_label}\n"
            f"{'=' * len(type_label)}\n\n"
            f"Priority: {priority}\n"
            f"Subject: {subject_text}\n\n"
            f"Description:\n{description}\n"
            f"{steps_text}"
            f"{jira_text}\n"
            f"Submitted by: {user_name} ({user_email})\n"
        )

        # Build MIME message — use mixed wrapper when attachment is present
        attachment_data = feedback_data.get("attachment_data")
        attachment_filename = feedback_data.get("attachment_filename")

        alt_part = MIMEMultipart("alternative")
        alt_part.attach(MIMEText(text_body, "plain"))
        alt_part.attach(MIMEText(html_body, "html"))

        if attachment_data and attachment_filename:
            msg = MIMEMultipart("mixed")
            msg.attach(alt_part)
            file_part = MIMEBase("application", "octet-stream")
            file_part.set_payload(attachment_data)
            encoders.encode_base64(file_part)
            file_part.add_header("Content-Disposition", "attachment", filename=attachment_filename)
            msg.attach(file_part)
        else:
            msg = alt_part

        msg["Subject"] = email_subject
        msg["From"] = settings["from_email"]
        msg["To"] = ", ".join(recipients)
        if user_email:
            msg["Reply-To"] = user_email

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], recipients, msg.as_string())
            server.quit()
            logging.info(
                "FeedbackService: feedback email sent to %s (type=%s, subject='%s')",
                recipients,
                fb_type,
                subject_text,
            )
            return {"success": True}
        except Exception as e:
            logging.error("FeedbackService: failed to send feedback email: %s", e)
            return {"success": False, "error": "Could not send the feedback."}

    # ------------------------------------------------------------------
    # Jira integration
    # ------------------------------------------------------------------

    @staticmethod
    def create_jira_issue(feedback_data: dict, config) -> dict:
        """
        Create a from feedback data.

        Returns {"success": True, "issue_key": "VAF-123", "issue_url": "..."} or
        {"success": False, "error": "..."}.
        """
        import requests

        base_url = config.JIRA_BASE_URL.rstrip("/")
        project_key = config.JIRA_PROJECT_KEY
        user_email = config.JIRA_USER_EMAIL
        api_token = config.JIRA_API_TOKEN

        if not all([base_url, project_key, user_email, api_token]):
            return {"success": False, "error": "Jira credentials not fully configured"}

        fb_type = feedback_data.get("type", "bug")
        issue_type_id = config.JIRA_BUG_ISSUE_TYPE_ID if fb_type == "bug" else config.JIRA_STORY_ISSUE_TYPE_ID

        priority_map = {"low": "Low", "medium": "Medium", "high": "High"}
        jira_priority = priority_map.get(feedback_data.get("priority", "medium"), "Medium")

        # Build description in Atlassian Document Format (ADF)
        description_content = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": feedback_data.get("description", "")}],
            }
        ]

        steps = feedback_data.get("steps", "")
        if fb_type == "bug" and steps:
            description_content.append(
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": "Steps to Reproduce"}],
                }
            )
            description_content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": steps}],
                }
            )

        submitter = feedback_data.get("user_name", "Unknown")
        submitter_email = feedback_data.get("user_email", "")
        description_content.append(
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": f"Submitted by: {submitter} ({submitter_email})",
                        "marks": [{"type": "em"}],
                    }
                ],
            }
        )

        payload = {
            "fields": {
                "project": {"key": project_key},
                "summary": feedback_data.get("subject", "(no subject)"),
                "issuetype": {"id": issue_type_id},
                "priority": {"name": jira_priority},
                "description": {
                    "version": 1,
                    "type": "doc",
                    "content": description_content,
                },
            }
        }

        auth = (user_email, api_token)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}

        try:
            resp = requests.post(
                f"{base_url}/rest/api/3/issue",
                json=payload,
                auth=auth,
                headers=headers,
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                # Jira's own errorMessages / errors travel with the
                # failure (into the email and the audit log), not only into a
                # container log that does not survive a recreate.
                error = jira_error_text(resp.status_code, resp.text)
                logging.error("FeedbackService: creation failed: %s", error)
                return {"success": False, "error": error, "status": resp.status_code}

            issue_data = resp.json()
            issue_key = issue_data.get("key", "")
            issue_url = f"{base_url}/browse/{issue_key}"
            logging.info("FeedbackService: created: %s", issue_key)

            # Attach file if present
            attachment_data = feedback_data.get("attachment_data")
            attachment_filename = feedback_data.get("attachment_filename")
            if attachment_data and attachment_filename:
                FeedbackService._attach_file_to_jira(base_url, auth, issue_key, attachment_data, attachment_filename)

            return {"success": True, "issue_key": issue_key, "issue_url": issue_url}

        except Exception as e:
            logging.error("FeedbackService: Jira request failed: %s", e)
            return {"success": False, "error": f"Could not reach Jira ({type(e).__name__})."}

    @staticmethod
    def probe_jira(config, timeout: int = 8) -> dict:
        """can the configured identity file into the configured project?

        Two read-only calls with the credentials the form uses: ``/myself``
        (does the token authenticate, and as whom) and ``/project/<key>``
        (can that identity see the project). Surfaced on Status Reports so a
        swapped token or a project-permission change is noticed before a user
        hits it. Never raises; never includes the token.
        """
        import requests

        out: dict = {
            "configured": False,
            "ok": False,
            "identity": None,
            "project_key": getattr(config, "JIRA_PROJECT_KEY", "") or "",
            "myself_status": None,
            "project_status": None,
            "error": "",
            "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        if not getattr(config, "JIRA_ENABLED", False):
            out["error"] = "Jira integration is disabled in master-config.yaml."
            return out
        base_url = (getattr(config, "JIRA_BASE_URL", "") or "").rstrip("/")
        user_email = getattr(config, "JIRA_USER_EMAIL", "") or ""
        api_token = getattr(config, "JIRA_API_TOKEN", "") or ""
        if not all([base_url, out["project_key"], user_email, api_token]):
            out["error"] = "Jira credentials not fully configured (base URL, project key, user email, API token)."
            return out
        out["configured"] = True
        auth = (user_email, api_token)
        headers = {"Accept": "application/json"}
        try:
            me = requests.get(f"{base_url}/rest/api/3/myself", auth=auth, headers=headers, timeout=timeout)
            out["myself_status"] = me.status_code
            if me.status_code != 200:
                out["error"] = f"The feedback Jira token no longer authenticates as {user_email}: " + jira_error_text(
                    me.status_code, me.text
                )
                return out
            try:
                out["identity"] = (me.json() or {}).get("emailAddress") or user_email
            except ValueError:
                out["identity"] = user_email
            proj = requests.get(
                f"{base_url}/rest/api/3/project/{out['project_key']}", auth=auth, headers=headers, timeout=timeout
            )
            out["project_status"] = proj.status_code
            if proj.status_code != 200:
                out["error"] = f"{out['identity']} cannot see project {out['project_key']}: " + jira_error_text(
                    proj.status_code, proj.text
                )
                return out
        except Exception as e:  # noqa: BLE001 — a probe must never take the page down
            out["error"] = f"Could not reach Jira ({type(e).__name__})."
            return out
        out["ok"] = True
        return out

    @staticmethod
    def _attach_file_to_jira(base_url: str, auth: tuple, issue_key: str, file_data: bytes, filename: str) -> None:
        """Attach a file to an existing."""
        import requests

        try:
            resp = requests.post(
                f"{base_url}/rest/api/3/issue/{issue_key}/attachments",
                auth=auth,
                headers={"X-Atlassian-Token": "no-check"},
                files={"file": (filename, io.BytesIO(file_data))},
                timeout=30,
            )
            if resp.status_code not in (200, 201):
                logging.warning(
                    "FeedbackService: Jira attachment failed for %s: %s %s",
                    issue_key,
                    resp.status_code,
                    resp.text[:300],
                )
            else:
                logging.info("FeedbackService: attached file to %s", issue_key)
        except Exception as e:
            logging.warning("FeedbackService: Jira attachment error for %s: %s", issue_key, e)


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


JIRA_ERROR_TEXT_MAX = 500
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def jira_error_text(status: int, body: str | None) -> str:
    """``Jira API returned <status>: <messages>`` from Jira's error JSON.

    Jira answers a refused create with ``{"errorMessages": [...], "errors":
    {"field": "why"}}``; both are joined, bounded to ``JIRA_ERROR_TEXT_MAX``
    characters and stripped of control characters. A non-JSON body
    contributes its first line. The status alone is what the email used to
    carry, and a bare 400 told nobody anything.
    """
    detail = ""
    text = (body or "").strip()
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            parts = [str(m) for m in (parsed.get("errorMessages") or []) if str(m).strip()]
            errors = parsed.get("errors") or {}
            if isinstance(errors, dict):
                parts += [f"{k}: {v}" for k, v in errors.items()]
            detail = "; ".join(parts)
        else:
            detail = text.splitlines()[0]
    detail = _CONTROL_RE.sub(" ", detail).strip()[:JIRA_ERROR_TEXT_MAX]
    return f"Jira API returned {status}" + (f": {detail}" if detail else "")

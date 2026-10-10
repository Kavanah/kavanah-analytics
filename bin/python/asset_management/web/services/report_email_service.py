"""
Report Email Service

Delivers a scheduled report to its configured recipients after the snapshot is
written. Separate from BillingEmailService by domain (billing vs reporting); it
reuses billing's SMTP-settings source + app-base-url helper + the daemon-thread
dispatch pattern + the ``_send_fn`` test seam, and adds the one capability the
billing layer lacks: **file attachments** (the xlsx / PDF report formats).

Delivery is best-effort, mirroring the billing contract: a render or SMTP
failure logs and is counted, but never fails the snapshot or aborts the runner
batch — the snapshot has already been written and is viewable in-app regardless.

Email formats (schedule.delivery.email.format):
  * ``inline_html``  — KPI summary + each table rendered as an HTML table in the
                       body. No attachment.
  * ``excel``        — summary in body + an ``.xlsx`` attachment built from the
                       snapshot's tables/summary via report_export_service.
  * ``pdf``          — same, a ``.pdf`` attachment.
                       both attachment formats downgrade at send time
                       when the snapshot stored no tables (graphical-only
                       dashboard) — the attachment is skipped and the body
                       states the limitation, instead of silently delivering a
                       valid-but-empty file.
  * ``summary_link`` — KPI summary + a link to the snapshot's in-app View at
                       ``/assets/report-scheduler/snapshots/<id>``; suppressed
                       when the snapshot was never persisted.
"""

from __future__ import annotations

import html as _html
import importlib
import logging
import re as _re
import smtplib

from lib.smtp_transport import SMTP_TIMEOUT_SECONDS, smtp_ssl_context
import threading
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any


def _import_first(*candidates: str):
    """Import the first resolvable module from a list of candidate dotted names.

    Same three-root resolver the rest of the report-scheduler uses
    (container ``asset_management.web.services.*`` / app ``web.services.*`` /
    pytest ``services.*``). See report_schedule_service._import_first.
    """
    last_err: Exception | None = None
    for name in candidates:
        try:
            return importlib.import_module(name)
        except ImportError as e:
            last_err = e
    raise last_err  # type: ignore[misc]


def _billing_email_mod():
    return _import_first(
        "asset_management.web.services.billing_email_service",
        "web.services.billing_email_service",
        "services.billing_email_service",
    )


def _export_mod():
    return _import_first(
        "asset_management.web.services.report_export_service",
        "web.services.report_export_service",
        "services.report_export_service",
    )


_VALID_FORMATS = ("inline_html", "excel", "pdf", "summary_link")
_DEFAULT_FORMAT = "summary_link"
# In-app snapshot-View route (report_scheduler blueprint). the
# app is public only under nginx's /assets/ mount (PrefixMiddleware strips
# it), so the emailed link must carry the /assets prefix.
_SNAPSHOT_VIEW_PATH = "/assets/report-scheduler/snapshots/{snapshot_id}"
# Subject used when a schedule stores no template. follow-up: the
# email dialog now PRE-FILLS its subject field with this exact string (see
# report_scheduler.html ESM_DEFAULT_SUBJECT) instead of showing it as a gray
# placeholder, so the user edits the real default rather than retyping it.
# Clearing the field still lands here — keep the two literals identical.
_DEFAULT_SUBJECT = "{{report_name}} — {{date_range}}"


class ReportEmailService:
    """Render + deliver a scheduled report email. Best-effort throughout."""

    # ─────────────────────────────────────────────────────────────────────────
    # Subject template
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _resolve_subject(template: str, *, report_name: str, date_range: dict) -> str:
        """Resolve {{report_name}} and {{date_range}} in the subject template."""
        dr = ""
        if isinstance(date_range, dict) and date_range.get("from") and date_range.get("to"):
            dr = f"{date_range['from']} – {date_range['to']}"
        subject = template or _DEFAULT_SUBJECT
        subject = subject.replace("{{report_name}}", report_name or "Report")
        subject = subject.replace("{{date_range}}", dr)
        # the Subject is a mail HEADER built from author-controlled
        # text. CR/LF (or any control char) in report_name / the template would
        # inject further headers (Bcc:, extra recipients). Collapse them to a
        # space and cap the length; header folding is the mail library's job.
        subject = _re.sub(r"[\x00-\x1f\x7f]+", " ", subject)
        return subject[:300]

    # ─────────────────────────────────────────────────────────────────────────
    # snapshot → export sections adapter
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _snapshot_to_sections(snapshot: dict) -> list[dict]:
        """Map a snapshot's data into the {query, narrative, table} section
        shape report_export_service.export_excel/export_pdf expect.

        One section for the KPI summary (as a 2-col table) + one per stored
        table result (columns → headers, rows → rows).
        """
        data = snapshot.get("data") or {}
        sections: list[dict] = []

        summary = data.get("summary_metrics") or {}
        if summary:
            sections.append(
                {
                    "query": "Summary",
                    "narrative": "",
                    "table": {
                        "headers": ["Metric", "Value"],
                        "rows": [[k, v] for k, v in summary.items()],
                    },
                }
            )

        for tbl in data.get("tables") or []:
            result = tbl.get("result") or {}
            sections.append(
                {
                    "query": tbl.get("title") or tbl.get("chart_id") or "Table",
                    "narrative": "",
                    "table": {
                        "headers": result.get("columns") or [],
                        "rows": result.get("rows") or [],
                    },
                }
            )
        return sections

    # ─────────────────────────────────────────────────────────────────────────
    # HTML body
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _summary_html(summary: dict) -> str:
        if not summary:
            return ""
        rows = "".join(
            f"<tr><td style='padding:4px 12px 4px 0'>{_html.escape(str(k))}</td>"
            f"<td style='padding:4px 0'><b>{_html.escape(str(v))}</b></td></tr>"
            for k, v in summary.items()
        )
        return f"<table style='border-collapse:collapse'>{rows}</table>"

    @staticmethod
    def _table_html(tbl: dict) -> str:
        result = tbl.get("result") or {}
        cols = result.get("columns") or []
        rows = result.get("rows") or []
        if not cols:
            return ""
        title = _html.escape(str(tbl.get("title") or tbl.get("chart_id") or "Table"))
        head = "".join(f"<th style='text-align:left;padding:4px 12px 4px 0'>{_html.escape(str(c))}</th>" for c in cols)
        body = "".join(
            "<tr>" + "".join(f"<td style='padding:2px 12px 2px 0'>{_html.escape(str(v))}</td>" for v in row) + "</tr>"
            for row in rows
        )
        return f"<h3>{title}</h3><table style='border-collapse:collapse'><tr>{head}</tr>{body}</table>"

    @staticmethod
    def _report_links_html(data: dict, *, settings: dict | None = None) -> str:
        """HTML for any Impact Report charts in the snapshot.

        Impact Report charts aren't precomputed (they render client-side only);
        the precompute stamps each with a ``report_link`` (app-relative path) +
        ``report_link_kind``. Surface each as a link to the live report — public
        token viewer or login-gated dashboard — with the absolute base prepended.
        Returns "" when there are no report charts.
        """
        charts = data.get("charts") or []
        links = [c for c in charts if isinstance(c, dict) and c.get("report_link")]
        if not links:
            return ""
        try:
            be = _billing_email_mod()
            base = be._get_app_base_url(settings)
        except Exception:  # noqa: BLE001 — link is non-critical
            base = ""
        out = []
        for c in links:
            path = c.get("report_link") or ""
            url = f"{base}{path}" if base else path
            title = _html.escape(str(c.get("title") or "Impact Report"))
            gated = c.get("report_link_kind") != "public"
            suffix = " (sign-in required)" if gated else ""
            out.append(f"<p><a href='{_html.escape(url)}'>{title}: View the full report &rarr;</a>{suffix}</p>")
        return "".join(out)

    @staticmethod
    def _dashboard_links_html(data: dict, *, settings: dict | None = None) -> str:
        """follow-up — one link per dashboard for a MULTI-dashboard
        schedule. The precompute stamps ``data.dashboard_links`` (only when the
        schedule carries more than one dashboard; a single-dashboard schedule
        keeps the existing lone "View the full report" link). Returns "" when
        absent, so single-dashboard emails are byte-identical to before.
        """
        links = [d for d in (data.get("dashboard_links") or []) if isinstance(d, dict) and d.get("link")]
        if not links:
            return ""
        try:
            be = _billing_email_mod()
            base = be._get_app_base_url(settings)
        except Exception:  # noqa: BLE001 — link is non-critical
            base = ""
        out = ["<h3>Dashboards</h3><ul>"]
        for d in links:
            url = f"{base}{d.get('link') or ''}" if base else (d.get("link") or "")
            title = _html.escape(str(d.get("title") or "Dashboard"))
            suffix = "" if d.get("kind") == "public" else " (sign-in required)"
            out.append(f"<li><a href='{_html.escape(url)}'>{title}</a>{suffix}</li>")
        out.append("</ul>")
        return "".join(out)

    @staticmethod
    def render_report_email(schedule: dict, snapshot: dict, *, settings: dict | None = None) -> dict:
        """Build ``{subject, html, text, attachments}`` for one schedule+snapshot.

        ``attachments`` is a list of ``{filename, content (bytes), mimetype}``.
        """
        delivery = (schedule.get("delivery") or {}).get("email") or {}
        fmt = delivery.get("format") or _DEFAULT_FORMAT
        if fmt not in _VALID_FORMATS:
            fmt = _DEFAULT_FORMAT
        report_name = schedule.get("name") or "Report"
        data = snapshot.get("data") or {}
        date_range = snapshot.get("date_range") or data.get("date_range") or {}
        summary = data.get("summary_metrics") or {}
        tables = data.get("tables") or []

        subject = ReportEmailService._resolve_subject(
            delivery.get("subject_template", ""), report_name=report_name, date_range=date_range
        )

        dr_text = ""
        if date_range.get("from") and date_range.get("to"):
            dr_text = f"{date_range['from']} – {date_range['to']}"

        parts = [f"<h2>{_html.escape(report_name)}</h2>"]
        if dr_text:
            parts.append(f"<p style='color:#5F6B7A'>{_html.escape(dr_text)}</p>")
        summary_html = ReportEmailService._summary_html(summary)
        if summary_html:
            parts.append("<h3>Summary</h3>")
            parts.append(summary_html)

        # Impact Report charts can't be embedded (they render
        # client-side only); link out to the live report instead.
        report_links_html = ReportEmailService._report_links_html(data, settings=settings)
        if report_links_html:
            parts.append(report_links_html)

        # follow-up — a link per dashboard when the schedule emails
        # several. Empty for single-dashboard schedules (unchanged shape).
        dashboard_links_html = ReportEmailService._dashboard_links_html(data, settings=settings)
        if dashboard_links_html:
            parts.append(dashboard_links_html)

        attachments: list[dict] = []

        if fmt == "inline_html":
            for tbl in tables:
                th = ReportEmailService._table_html(tbl)
                if th:
                    parts.append(th)
        elif fmt in ("excel", "pdf") and not any((t.get("result") or {}).get("columns") for t in tables):
            # the attachment is built from the snapshot's tables;
            # with none (graphical-only dashboard, e.g. Impact Report charts —
            # they precompute to link-outs, not tables) the export raises
            # nothing and the recipient silently got a valid-but-EMPTY
            # .xlsx/.pdf. Downgrade at send time: skip the attachment, say
            # why, and keep the summary + report/dashboard links above (plus
            # the in-app snapshot link when one exists). Send-time is the
            # right decision point — a schedule's target dashboard can change
            # shape long after the format was picked.
            attachment_name = "an Excel file" if fmt == "excel" else "a PDF"
            note = f"(This report contains only graphical charts, which cannot be attached as {attachment_name}"
            if report_links_html or dashboard_links_html:
                note += " — use the link above to view it"
            parts.append(f"<p>{note}.)</p>")
            link = ReportEmailService._snapshot_link(snapshot, settings=settings)
            if link:
                parts.append(f"<p><a href='{_html.escape(link)}'>View the full report</a></p>")
        elif fmt in ("excel", "pdf"):
            sections = ReportEmailService._snapshot_to_sections(snapshot)
            exp = _export_mod()
            try:
                if fmt == "excel":
                    buf = exp.export_excel(report_name, sections)
                    attachments.append(
                        {
                            "filename": f"{_safe_filename(report_name)}.xlsx",
                            "content": buf.getvalue(),
                            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        }
                    )
                else:
                    buf = exp.export_pdf(report_name, sections)
                    attachments.append(
                        {
                            "filename": f"{_safe_filename(report_name)}.pdf",
                            "content": buf.getvalue(),
                            "mimetype": "application/pdf",
                        }
                    )
                parts.append(f"<p>Your report is attached as {fmt.upper()}.</p>")
            except Exception as e:  # noqa: BLE001 — export is best-effort
                logging.error("ReportEmailService: %s export failed: %s", fmt, e)
                parts.append("<p>(The attachment could not be generated; the summary is above.)</p>")
        else:  # summary_link
            # when the report links out on its own (Impact Report
            # charts) AND the snapshot has no tables to view in-app, the legacy
            # snapshot-View link is redundant with the report link above and only
            # points at a near-empty snapshot page. Suppress it in that case so
            # the email carries a single, meaningful link. If the snapshot DOES
            # carry tables, keep the snapshot link (it's where those are viewed).
            # follow-up — the per-dashboard list counts as linking out
            # on its own, same as an Impact Report link.
            if (report_links_html or dashboard_links_html) and not tables:
                pass
            else:
                link = ReportEmailService._snapshot_link(snapshot, settings=settings)
                if link:
                    parts.append(f"<p><a href='{_html.escape(link)}'>View the full report</a></p>")

        html = "<div style='font-family:Arial,sans-serif'>" + "".join(parts) + "</div>"
        text = ReportEmailService._text_body(report_name, dr_text, summary)
        return {"subject": subject, "html": html, "text": text, "attachments": attachments}

    @staticmethod
    def _text_body(report_name: str, dr_text: str, summary: dict) -> str:
        lines = [report_name]
        if dr_text:
            lines.append(dr_text)
        if summary:
            lines.append("")
            for k, v in summary.items():
                lines.append(f"{k}: {v}")
        return "\n".join(lines)

    @staticmethod
    def _snapshot_link(snapshot: dict, *, settings: dict | None = None) -> str:
        """Build the in-app View URL for a snapshot.

        email-only schedules render their snapshot in memory and
        never persist it — there is no snapshot_id and nothing to view, so
        return "" (the caller suppresses the link) instead of a guaranteed
        404 at ``/snapshots/``.
        """
        snap_id = snapshot.get("snapshot_id") or ""
        if not snap_id:
            return ""
        try:
            be = _billing_email_mod()
            base = be._get_app_base_url(settings)
        except Exception:  # noqa: BLE001 — link is non-critical
            base = ""
        path = _SNAPSHOT_VIEW_PATH.format(snapshot_id=snap_id)
        return f"{base}{path}" if base else path

    # ─────────────────────────────────────────────────────────────────────────
    # send + dispatch
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def send_report(
        *,
        to_email: str,
        subject: str,
        html: str,
        text: str,
        attachments: list[dict] | None = None,
        _send_fn: Any = None,
    ) -> dict:
        """Send one report email synchronously, with optional attachments.

        Best-effort: returns ``{"success": bool, "error": str}``, never raises.
        ``_send_fn(message, to_email, settings)`` is a test seam that bypasses
        SMTP.
        """
        if not to_email:
            return {"success": False, "error": "no recipient"}

        be = _billing_email_mod()
        settings = be._get_smtp_settings()
        required = ["smtp_host", "smtp_port", "smtp_username", "smtp_password", "from_email"]
        missing = [k for k in required if not settings.get(k)]
        if missing and _send_fn is None:
            return {"success": False, "error": f"SMTP not configured (missing: {', '.join(missing)})"}

        # Attachments require a "mixed" outer container; the text/html alternative
        # is nested inside it.
        outer = MIMEMultipart("mixed")
        outer["Subject"] = subject
        outer["From"] = settings.get("from_email", "")
        outer["To"] = to_email

        body = MIMEMultipart("alternative")
        body.attach(MIMEText(text or "", "plain"))
        body.attach(MIMEText(html or "", "html"))
        outer.attach(body)

        for att in attachments or []:
            content = att.get("content")
            if not content:
                continue
            maintype, _, subtype = (att.get("mimetype") or "application/octet-stream").partition("/")
            part = MIMEApplication(content, _subtype=subtype or "octet-stream")
            part.add_header("Content-Disposition", "attachment", filename=att.get("filename", "report"))
            outer.attach(part)

        if _send_fn is not None:
            try:
                _send_fn(outer, to_email, settings)
                return {"success": True}
            except Exception as e:  # noqa: BLE001 — seam boundary
                return {"success": False, "error": str(e)}

        try:
            if settings.get("smtp_use_tls", True):
                server = smtplib.SMTP(settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS)
                server.starttls(context=smtp_ssl_context())
            else:
                server = smtplib.SMTP_SSL(
                    settings["smtp_host"], int(settings["smtp_port"]), timeout=SMTP_TIMEOUT_SECONDS, context=smtp_ssl_context()
                )
            server.login(settings["smtp_username"], settings["smtp_password"])
            server.sendmail(settings["from_email"], to_email, outer.as_string())
            server.quit()
            logging.info("ReportEmailService: report email sent to %s", to_email)
            return {"success": True}
        except Exception as e:  # noqa: BLE001 — top-level boundary
            logging.error("ReportEmailService: failed to send to %s: %s", to_email, e)
            return {"success": False, "error": str(e)}

    @staticmethod
    def dispatch_report(
        *,
        to_email: str,
        subject: str,
        html: str,
        text: str,
        attachments: list[dict] | None = None,
        _send_fn: Any = None,
    ) -> threading.Thread | None:
        """Send a report email on a daemon thread so the runner never blocks on
        SMTP. Returns the started Thread (tests ``.join()``); None if no
        recipient. Mirrors BillingEmailService.dispatch."""
        if not to_email:
            return None

        def _runner() -> None:
            try:
                ReportEmailService.send_report(
                    to_email=to_email,
                    subject=subject,
                    html=html,
                    text=text,
                    attachments=attachments,
                    _send_fn=_send_fn,
                )
            except Exception as e:  # noqa: BLE001 — daemon last-line guard
                logging.error("ReportEmailService.dispatch_report: send failed (-> %s): %s", to_email, e)

        thread = threading.Thread(target=_runner, name="report-email", daemon=True)
        thread.start()
        return thread

    @staticmethod
    def deliver(schedule: dict, snapshot: dict, *, _send_fn: Any = None) -> dict:
        """Render + dispatch a schedule's report to each recipient when email is
        enabled. Best-effort: returns ``{"emailed": N, "recipients": M}``; never
        raises so the runner's snapshot/advance is unaffected.
        """
        delivery = (schedule.get("delivery") or {}).get("email") or {}
        if not delivery.get("enabled"):
            return {"emailed": 0, "recipients": 0}
        recipients = [r for r in (delivery.get("recipients") or []) if r]
        if not recipients:
            return {"emailed": 0, "recipients": 0}

        # recipient policy at delivery time: workspace members and
        # verified external addresses only. Unverified externals are dropped
        # here (named in the result + log), never silently added to the send.
        try:
            try:
                from web.services.report_recipient_service import ReportRecipientService
            except ImportError:
                from .report_recipient_service import ReportRecipientService
            allowed, skipped = ReportRecipientService.allowed_recipients(schedule)
        except Exception as e:  # noqa: BLE001 — fail CLOSED: a policy error must not widen the list
            logging.error("ReportEmailService.deliver: recipient policy failed for %s: %s", schedule.get("schedule_id"), e)
            return {
                "emailed": 0,
                "recipients": len(recipients),
                "skipped_unverified": list(recipients),
                "error": "recipient policy unavailable",
            }
        if skipped:
            logging.warning(
                "ReportEmailService.deliver: %s — %d recipient(s) skipped (not workspace members, address not verified): %s",
                schedule.get("schedule_id"),
                len(skipped),
                ", ".join(skipped),
            )
        if not allowed:
            return {"emailed": 0, "recipients": len(recipients), "skipped_unverified": skipped}
        recipients = allowed

        try:
            rendered = ReportEmailService.render_report_email(schedule, snapshot)
        except Exception as e:  # noqa: BLE001 — render is best-effort
            logging.error("ReportEmailService.deliver: render failed for %s: %s", schedule.get("schedule_id"), e)
            return {"emailed": 0, "recipients": len(recipients), "error": str(e)}

        emailed = 0
        for to_email in recipients:
            thread = ReportEmailService.dispatch_report(
                to_email=to_email,
                subject=rendered["subject"],
                html=rendered["html"],
                text=rendered["text"],
                attachments=rendered["attachments"],
                _send_fn=_send_fn,
            )
            if thread is not None:
                emailed += 1
        return {"emailed": emailed, "recipients": len(recipients), "skipped_unverified": skipped}


def _safe_filename(name: str) -> str:
    keep = "".join(c if (c.isalnum() or c in " -_") else "_" for c in (name or "report"))
    return keep.strip().replace(" ", "_") or "report"

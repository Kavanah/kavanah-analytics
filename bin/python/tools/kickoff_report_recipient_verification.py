"""One-shot: classify every existing scheduled-report recipient and mail
verification links to the external ones.

 makes delivery send only to workspace members and verified external
addresses. Schedules that already exist were saved before that rule, so their
external recipients have never been asked to confirm — without this step they
would simply stop receiving reports at the first run after the cutover, and
nobody would be told until the author next saved the schedule.

For every ``de_report_schedules`` row with ``delivery.email.enabled`` and
recipients, this runs the same classification the save path runs
(``ReportRecipientService.on_schedule_saved``): members pass, verified
externals pass, unverified externals get ONE confirmation mail (24 h resend
cooldown) and an ``report_recipient_added`` audit row. Dry-run by default.

Usage::

    # Dry-run (default) — prints members / verified / unverified per schedule, sends nothing.
    docker exec -w /app/python imports_container python3 \\
        -m tools.kickoff_report_recipient_verification

    # Send the confirmation mails.
    docker exec -w /app/python imports_container python3 \\
        -m tools.kickoff_report_recipient_verification --apply
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def run(*, apply: bool) -> dict[str, int]:
    from lib.es_helpers import get_elasticsearch_client
    from asset_management.web.services.report_recipient_service import ReportRecipientService
    from asset_management.web.services.report_schedule_service import INDEX_REPORT_SCHEDULES

    counts = {"schedules": 0, "with_recipients": 0, "members": 0, "verified": 0, "unverified": 0, "mails_sent": 0}
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch")
        return counts
    try:
        resp = es.search(
            index=INDEX_REPORT_SCHEDULES,
            body={"size": 1000, "query": {"match_all": {}}, "_source": ["name", "created_by", "status", "delivery"]},
            ignore_unavailable=True,
        )
    except Exception as exc:  # noqa: BLE001 — boundary
        logger.error("search failed: %s", exc)
        return counts

    for hit in (resp.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        counts["schedules"] += 1
        email_cfg = (src.get("delivery") or {}).get("email") or {}
        recipients = [r for r in (email_cfg.get("recipients") or []) if r]
        if not email_cfg.get("enabled") or not recipients:
            continue
        counts["with_recipients"] += 1
        created_by = src.get("created_by") or ""
        label = f"{src.get('name') or hit.get('_id')!r} [created_by={created_by or '?'}, status={src.get('status') or '?'}]"
        if apply:
            status = ReportRecipientService.on_schedule_saved(recipients, created_by)
        else:
            status = ReportRecipientService.classify(recipients, created_by)
        counts["members"] += len(status["members"])
        counts["verified"] += len(status["verified"])
        counts["unverified"] += len(status["unverified"])
        logger.info(
            "%-12s %s — members=%s verified=%s unverified=%s",
            "notified" if (apply and status["unverified"]) else ("would_notify" if status["unverified"] else "ok"),
            label,
            status["members"],
            status["verified"],
            status["unverified"],
        )
    logger.info(
        "done%s — schedules=%d with_recipients=%d members=%d verified=%d unverified=%d",
        "" if apply else " (dry-run, nothing sent)",
        counts["schedules"],
        counts["with_recipients"],
        counts["members"],
        counts["verified"],
        counts["unverified"],
    )
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="send the confirmation mails (default: dry-run)")
    args = parser.parse_args(argv)
    run(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())

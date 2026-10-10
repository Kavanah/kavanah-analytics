"""One-shot backfill: stamp ``data_access_expires_at`` onto existing wizard Meta
connections from Meta's ``/debug_token``.

Before the OAuth callback read ``expires_at`` from debug_token but not
``data_access_expires_at`` — the 90-day DATA ACCESS clock that actually governs
whether a long-lived token can still read anything. On prod 2026-09-07 every
one of the 15 ``provider=facebook`` ``de_connection`` rows carried
``data_access_expires_at = null``, so the credential-health probe had
nothing to count down from and the November batch (~2026-11-02, up to 36 teams)
would have been discovered the morning imports stopped.

Going forward the callback / re-auth paths write the field. This heals the rows
that already exist: for every ``de_connection`` row whose provider is
``facebook`` (the wizard cohort — the same set the probe walks), decrypt the
stored token, ``/debug_token`` it with the wizard Meta app, and write the
``data_access_expires_at`` Meta reports (ISO-8601, the schema's convention).

Deliberately NOT touched:
* rows whose token is missing, will not decrypt, or Meta reports invalid — a
  dead token is the probe's job to flag (``token_dead``), not this script's;
* rows where Meta reports no window (``0`` / absent — system-user tokens have
  none): logged as ``no_window`` so the operator can see the answer, unwritten;
* anything but ``data_access_expires_at`` + ``updated_at`` (partial update, so
  the encrypted token and every other field are left exactly as they are).

The script is **dry-run by default**. Use ``--apply`` to write. Idempotent: a
second ``--apply`` finds every row ``unchanged``. It never logs a token — only
label / principal / esid / token type / days, the fields an operator needs.

Usage::

    # Dry-run (default) — shows what Meta reports per row, writes nothing.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_data_access_expiry

    # Apply.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_data_access_expiry --apply

    # One row only.
    docker exec -w /app/python imports_container python3 \\
        -m tools.backfill_data_access_expiry --esid <de_connection _id> --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

INDEX = "de_connection"
PROVIDER = "facebook"

# Outcome keys, in the order the summary prints them.
OUTCOMES = (
    "updated",
    "would_update",
    "unchanged",
    "no_window",
    "invalid",
    "unreachable",
    "no_token",
)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _epoch_to_iso(raw) -> str | None:
    """debug_token epochs -> the schema's ISO-8601; 0 / absent / junk -> None."""
    if isinstance(raw, (int, float)) and raw > 0:
        try:
            return datetime.fromtimestamp(int(raw), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _describe(esid: str, source: dict) -> str:
    """Operator-readable row identity — never the token."""
    return f"{source.get('label') or ''!r} [principal={source.get('principal') or '?'}, esid={esid}]"


def _safe_decrypt(value: str) -> str:
    if not value:
        return ""
    try:
        from lib.encryption import decrypt

        return decrypt(value) or ""
    except Exception:  # noqa: BLE001 — a row under a rotated key must not abort the run
        return ""


def run(*, apply: bool, only_esid: str = "") -> dict[str, int]:
    """Backfill ``data_access_expires_at`` on the wizard Meta cohort.

    Returns a count per outcome (see ``OUTCOMES``). Read-only unless ``apply``.
    """
    from lib.credential_prober import probe_token
    from lib.es_helpers import get_elasticsearch_client
    from asset_management.web.services.wizard_oauth_service import WizardOAuthService

    counts = {k: 0 for k in OUTCOMES}

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch")
        return counts

    try:
        meta_app = WizardOAuthService.get_meta_app()
        app_id, app_secret = meta_app.client_id, meta_app.client_secret
    except Exception as e:  # noqa: BLE001 — config gap; refusing beats guessing
        logger.error("wizard meta-app unavailable (%s) — nothing probed", type(e).__name__)
        return counts
    if not app_id or not app_secret:
        logger.error("wizard meta-app has no client_id/client_secret — nothing probed")
        return counts

    filters: list[dict] = [{"term": {"provider": PROVIDER}}]
    if only_esid:
        filters.append({"ids": {"values": [only_esid]}})
    body = {"size": 500, "query": {"bool": {"filter": filters}}}
    try:
        resp = es.search(index=INDEX, body=body, ignore_unavailable=True, allow_no_indices=True)
    except Exception as e:  # noqa: BLE001 — boundary
        logger.error("search failed: %s", e)
        return counts

    hits = (resp.get("hits") or {}).get("hits") or []
    logger.info("%d %s row(s) with provider=%s%s", len(hits), INDEX, PROVIDER, " (dry-run)" if not apply else "")

    for hit in hits:
        esid = str(hit.get("_id") or "")
        source = hit.get("_source") or {}
        desc = _describe(esid, source)
        status = source.get("status") or ""
        if status in ("superseded", "revoked"):
            # Tombstones / soft-deleted rows carry no live token to probe.
            counts["no_token"] += 1
            logger.info("no_token      %s — status=%s", desc, status)
            continue

        token = _safe_decrypt(source.get("oauth_token_enc") or "")
        if not token:
            counts["no_token"] += 1
            logger.info("no_token      %s — empty or undecryptable oauth_token_enc", desc)
            continue

        tp = probe_token(token, app_id, app_secret)
        if not tp.reachable:
            counts["unreachable"] += 1
            logger.warning("unreachable   %s — %s", desc, tp.error)
            continue
        if not tp.is_valid:
            counts["invalid"] += 1
            logger.warning("invalid       %s — %s (the health probe flags this as token_dead)", desc, tp.error)
            continue

        new_iso = _epoch_to_iso(tp.data_access_expires_at)
        if new_iso is None:
            counts["no_window"] += 1
            logger.info(
                "no_window     %s — Meta reports no data_access_expires_at (type=%s, token_kind=%s)",
                desc,
                tp.token_type or "?",
                source.get("token_kind") or "user",
            )
            continue

        days = tp.days_until_data_access_expiry
        prior = source.get("data_access_expires_at")
        if prior == new_iso:
            counts["unchanged"] += 1
            logger.info("unchanged     %s — %s (%s day(s))", desc, new_iso, days)
            continue

        if not apply:
            counts["would_update"] += 1
            logger.info("would_update  %s — %r -> %s (%s day(s))", desc, prior, new_iso, days)
            continue

        try:
            es.update(
                index=INDEX,
                id=esid,
                doc={"data_access_expires_at": new_iso, "updated_at": _now_iso()},
                refresh="wait_for",
            )
        except Exception as e:  # noqa: BLE001 — one bad write must not stop the rest
            logger.error("update failed %s — %s", desc, e)
            continue
        counts["updated"] += 1
        logger.info("updated       %s — %r -> %s (%s day(s))", desc, prior, new_iso, days)

    summary = ", ".join(f"{k}={counts[k]}" for k in OUTCOMES)
    logger.info("done%s — %s", "" if apply else " (dry-run, nothing written)", summary)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the field (default: dry-run)")
    parser.add_argument("--esid", default="", help="restrict to one de_connection _id")
    args = parser.parse_args(argv)
    run(apply=args.apply, only_esid=args.esid)
    return 0


if __name__ == "__main__":
    sys.exit(main())

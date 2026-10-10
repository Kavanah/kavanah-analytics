"""Read-only diagnostic: full per-doc state for every YouTube account doc.

 follow-up. The two existing audits each answer one narrow
question and then stop:

* ``audit_youtube_account_ownership`` — does THIS doc's token own the channels
  linked to it? (Costs a token refresh + a Google API call per doc.)
* ``audit_youtube_duplicate_accounts`` — do two docs duplicate EACH OTHER?

Neither answers the questions a human actually asks before touching data:
*is this doc safe to delete*, *why is it "unknown"*, and *which docs are the
same Google login*. This tool dumps the stored state that decides those, with
NO Google calls at all, so it is free to run and cannot be rate-limited.

Specifically it reports, per doc:

* whether a token exists at all — the difference between "never authorized"
  (benign, imports nothing) and "token present but failing" (needs re-auth).
  The ownership audit collapses both into ``unknown``.
* the stored identity (``sub``), with the ``UNRECOVERABLE`` sentinel
  shown as-is rather than mistaken for an identity.
* linked-channel count vs stamped owned-channel count. A doc whose linked count
  far exceeds its owned count is being used as a shared bucket — the
  shared-bucket shape.
* **dangling links** — channels still pointing at this doc. This is the field
  that makes a "safe" deletion unsafe: deleting a doc with linked channels
  orphans them silently. Every removal candidate must show 0 linked first.
* email-group membership, so docs sharing one Google login are visible together
  WITHOUT asserting they are duplicates (one login can front several Brand
  Accounts, each legitimately needing its own doc + token).

Read-only: never writes ES, never touches tokens, never deletes.

Exit code is always 0 — this is a report, not a gate. Use the sibling audits
for pass/fail signals.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.diagnose_youtube_accounts

    # narrow to specific docs
    docker exec -w /app/python imports_container python3 \\
        -m tools.diagnose_youtube_accounts --account jVj4LpwBIm2AdJKusRc6 --account J6-bQp8BTJnYq0K4FntI
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

# Same three-candidate resolver as the sibling YouTube tools.
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

from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from tools.audit_youtube_account_ownership import _fetch_all_accounts, _linked_channels  # noqa: E402
from tools.audit_youtube_duplicate_accounts import UNRECOVERABLE, _identity_of, _owned_set_of  # noqa: E402
from tools.split_youtube_account import ACCOUNTS_INDEX  # noqa: E402

logger = logging.getLogger(__name__)


def classify_token_state(source: dict[str, Any]) -> str:
    """Why a doc might be un-diagnosable, from STORED state only.

    Distinguishes the two cases the ownership audit collapses into ``unknown``:
    ``no_token`` (created, never authorized — benign) vs ``tokened`` (a token
    exists, so an ``unknown`` verdict there means the refresh or probe FAILED
    and the owner must re-authorize).
    """
    if not source.get("youtube_account_token"):
        return "no_token"
    return "tokened"


def build_report(
    accounts: list[dict[str, Any]],
    linked_of: Callable[[str], dict[str, str]],
) -> dict[str, Any]:
    """Pure report builder — ES injected via ``linked_of`` for unit-testing.

    Returns ``{"docs": [...], "email_groups": {email: [esid, ...]}}``, docs
    sorted so the most suspicious (largest linked-minus-owned gap) lead.
    """
    docs: list[dict[str, Any]] = []
    email_groups: dict[str, list[str]] = defaultdict(list)

    for acc in accounts:
        esid = acc["_id"]
        linked = linked_of(esid)
        owned = _owned_set_of(acc)
        raw_identity = str(acc.get("youtube_account_identity") or "").strip()
        email = str(acc.get("youtube_account_email") or "").strip().casefold()
        if email:
            email_groups[email].append(esid)

        # Channels linked here that the stamped owned-set does not cover. Uses
        # STORED stamps only (no API call), so it is a hint, not proof — the
        # ownership audit is authoritative. A doc with an empty owned set gets
        # no unowned count, since absence of a stamp is not evidence.
        unowned = sorted(cid for cid in linked if cid not in owned) if owned else []

        docs.append(
            {
                "_id": esid,
                "title": acc.get("youtube_account_title") or esid,
                "email": acc.get("youtube_account_email") or "",
                "identity": raw_identity,
                "identity_usable": _identity_of(acc) is not None,
                "token_state": classify_token_state(acc),
                "linked_count": len(linked),
                "linked": linked,
                "owned_count": len(owned),
                "unowned_linked": unowned,
                "workspace_esid": acc.get("workspace_esid") or "",
                # Safe to delete only when nothing still points at it.
                "safe_to_delete": len(linked) == 0,
            }
        )

    docs.sort(key=lambda d: (-len(d["unowned_linked"]), -d["linked_count"], d["_id"]))
    return {"docs": docs, "email_groups": {e: v for e, v in email_groups.items() if len(v) > 1}}


def _log_doc(doc: dict[str, Any]) -> None:
    if doc["identity_usable"]:
        ident = doc["identity"]
    elif doc["identity"] == UNRECOVERABLE:
        ident = "UNRECOVERABLE (legacy grant — no sub recoverable)"
    elif doc["identity"]:
        ident = f"{doc['identity']} (unusable)"
    else:
        ident = "(none — never stamped)"

    logger.info("")
    logger.info("esid=%s  %r", doc["_id"], doc["title"])
    logger.info("    email      : %s", doc["email"] or "(none)")
    logger.info("    identity   : %s", ident)
    logger.info(
        "    token      : %s",
        "present" if doc["token_state"] == "tokened" else "NONE — never authorized (imports nothing)",
    )
    logger.info("    channels   : %d linked, %d owned (stamped)", doc["linked_count"], doc["owned_count"])
    logger.info("    workspace  : %s", doc["workspace_esid"] or "(none — invisible to workspace-scoped users)")

    if doc["unowned_linked"]:
        names = ", ".join(f"{doc['linked'][c]} ({c})" for c in doc["unowned_linked"])
        logger.warning(
            "    SHARED BUCKET: %d linked channel(s) NOT in this doc's stamped owned set: %s",
            len(doc["unowned_linked"]),
            names,
        )
        logger.warning("        -> per-owner re-home needed; see tools.split_youtube_account --account %s", doc["_id"])

    if doc["safe_to_delete"]:
        logger.info("    DELETE-SAFE: no channels point at this doc")
    else:
        logger.info(
            "    DELETE-UNSAFE: %d channel(s) still point here — re-home them first or they dangle",
            doc["linked_count"],
        )


def run(only: list[str] | None = None) -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    accounts = _fetch_all_accounts(es)
    if only:
        wanted = set(only)
        missing = wanted - {a["_id"] for a in accounts}
        for esid in sorted(missing):
            logger.warning("requested account %s not found in %s", esid, ACCOUNTS_INDEX)
        accounts = [a for a in accounts if a["_id"] in wanted]

    report = build_report(accounts, linked_of=lambda esid: _linked_channels(es, esid))
    docs = report["docs"]

    tokenless = sum(1 for d in docs if d["token_state"] == "no_token")
    buckets = sum(1 for d in docs if d["unowned_linked"])
    deletable = sum(1 for d in docs if d["safe_to_delete"])
    logger.info(
        "%d %s doc(s) — %d without a token, %d shared-bucket, %d with no linked channels",
        len(docs),
        ACCOUNTS_INDEX,
        tokenless,
        buckets,
        deletable,
    )

    for doc in docs:
        _log_doc(doc)

    if report["email_groups"]:
        logger.info("")
        logger.info("── Docs sharing one Google email ──────────────────────────────")
        logger.info("Same email = same Google LOGIN, but one login can front several Brand")
        logger.info("Accounts, each needing its own doc + token. Compare the")
        logger.info("owned-channel sets above before concluding anything is a duplicate.")
        for email, esids in sorted(report["email_groups"].items()):
            logger.info("    %s -> %s", email, ", ".join(sorted(esids)))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only YouTube account-doc diagnostic (no Google API calls).")
    parser.add_argument(
        "--account",
        action="append",
        default=[],
        metavar="ESID",
        help="Limit to this account esid (repeatable). Default: every doc.",
    )
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO, stream=sys.stderr)
    return run(only=args.account or None)


if __name__ == "__main__":
    raise SystemExit(main())

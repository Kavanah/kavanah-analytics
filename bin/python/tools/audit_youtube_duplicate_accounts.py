"""Read-only audit: YouTube account docs that duplicate EACH OTHER.

. Sibling to ``tools/audit_youtube_account_ownership``, which
classifies each doc *in isolation* (are this doc's linked channels owned by its
own token?). That check is blind to the failure this tool finds: two docs can
each be perfectly ``healthy`` on their own and still be a duplicate pair — same
Google identity, split across two docs, competing for the same channels.

Two docs sharing a *display title* are NOT necessarily duplicates. The
prod-verified same-title account pair (2026-07-13) is two DISTINCT
Brand Account identities that legitimately need one doc each: one doc = one
token = one identity. Merging those recreates the token ping-pong.
So this tool never judges by title; it judges by identity and owned-channel set,
and reports title collisions only as a cosmetic note.

Verdicts, strongest evidence first:

* **duplicate_identity** — two docs carry the SAME non-empty
  ``youtube_account_identity`` (Google ``sub``). Structurally one identity, so
  one token, so these SHOULD be one doc. The only verdict that is safe to
  merge, and the only one that is a true "duplicate account".
* **duplicate_owned_set** — two identity-less docs whose stamped
  ``youtube_account_owned_channel_ids`` OVERLAP. The sub-miss shape:
  legacy pre-openid grants yield no ``sub``, so identity comparison is blind and
  overlap is the best available signal. Probable duplicate — needs a human to
  confirm the owning identity before any merge.
* **distinct_identity** — same title, DIFFERENT identities. Explicitly NOT a
  duplicate (the same-title pair above). Reported so a human sees the tool
  considered and cleared it, rather than staying silent.
* **title_collision_unknown** — same title, but at least one side has neither an
  identity nor a stamped owned set. Unknowable from stored state; resolve by
  running ``backfill_youtube_identity`` (or having the owner authorize) first.

Read-only: never writes ES, never merges, never touches tokens or team links.
Purely local — reads stored stamps only, makes NO Google API calls, so unlike
the ownership audit it costs no quota and is safe to run freely.

Exit codes: 0 = no duplicates (distinct/unknown are not failures);
1 = at least one duplicate_identity or duplicate_owned_set pair; 2 = ES
unavailable.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_youtube_duplicate_accounts
"""

from __future__ import annotations

import logging
import sys
from itertools import combinations
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

from tools.audit_youtube_account_ownership import (  # noqa: E402
    _fetch_all_accounts,
    _linked_channels,
)
from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from tools.split_youtube_account import ACCOUNTS_INDEX  # noqa: E402

logger = logging.getLogger(__name__)

# Sentinel written by backfill_youtube_identity when a legacy grant yields no
# recoverable sub. Present in the identity field but NOT an identity —
# treating it as one would collapse every unrecoverable doc into one "duplicate".
UNRECOVERABLE = "UNRECOVERABLE"


def _identity_of(source: dict[str, Any]) -> str | None:
    """The doc's real Google ``sub``, or None when absent/unrecoverable."""
    raw = str(source.get("youtube_account_identity") or "").strip()
    if not raw or raw == UNRECOVERABLE:
        return None
    return raw


def _owned_set_of(source: dict[str, Any]) -> set[str]:
    """The doc's STAMPED owned-channel ids (no API call). May be empty."""
    return {str(c).strip() for c in (source.get("youtube_account_owned_channel_ids") or []) if str(c).strip()}


def _norm_title(source: dict[str, Any]) -> str:
    return " ".join(str(source.get("youtube_account_title") or "").split()).casefold()


def classify_pair(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    """Verdict for one pair of account docs, or None if they are unrelated.

    Order matters: identity is the strongest signal and is checked first, so a
    same-identity pair is never downgraded to an overlap or title verdict.
    """
    l_id, r_id = _identity_of(left), _identity_of(right)

    # Strongest: same real identity => genuinely one account in two docs.
    if l_id and r_id:
        if l_id == r_id:
            return "duplicate_identity"
        # Different identities are definitively NOT duplicates, regardless of
        # owned-set overlap or title. Only surface if the title misleads.
        return "distinct_identity" if _norm_title(left) == _norm_title(right) else None

    # Sub-miss shape: identity-less on BOTH sides, overlapping owned
    # sets. Empty sets never overlap, so they can't produce a false positive.
    if l_id is None and r_id is None:
        if _owned_set_of(left) & _owned_set_of(right):
            return "duplicate_owned_set"

    # No identity evidence either way — flag only if the title invites confusion.
    if _norm_title(left) == _norm_title(right) and _norm_title(left):
        if l_id and r_id:
            return None  # already handled above
        return "title_collision_unknown"

    return None


def build_report(
    accounts: list[dict[str, Any]],
    linked_of: Callable[[str], dict[str, str]],
) -> dict[str, Any]:
    """Pure report builder — ES injected via ``linked_of`` so it is unit-testable.

    Returns ``{"pairs": [...], "counts": {verdict: n}}``. Linked-channel counts
    ride along on each side because "which doc do we keep?" is decided by where
    the channels currently point.
    """
    linked_cache = {acc["_id"]: linked_of(acc["_id"]) for acc in accounts}
    pairs: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "duplicate_identity": 0,
        "duplicate_owned_set": 0,
        "distinct_identity": 0,
        "title_collision_unknown": 0,
    }

    for left, right in combinations(accounts, 2):
        verdict = classify_pair(left, right)
        if verdict is None:
            continue
        counts[verdict] = counts.get(verdict, 0) + 1
        pairs.append(
            {
                "verdict": verdict,
                "shared_channel_ids": sorted(_owned_set_of(left) & _owned_set_of(right)),
                "sides": [
                    {
                        "_id": doc["_id"],
                        "title": doc.get("youtube_account_title") or doc["_id"],
                        "email": doc.get("youtube_account_email") or "",
                        "identity": _identity_of(doc) or "",
                        "owned_count": len(_owned_set_of(doc)),
                        "linked_count": len(linked_cache.get(doc["_id"], {})),
                        "has_token": bool(doc.get("youtube_account_token")),
                    }
                    for doc in (left, right)
                ],
            }
        )

    # Strongest verdicts first so the actionable pairs lead the output.
    order = {
        "duplicate_identity": 0,
        "duplicate_owned_set": 1,
        "title_collision_unknown": 2,
        "distinct_identity": 3,
    }
    pairs.sort(key=lambda p: order.get(p["verdict"], 9))
    return {"pairs": pairs, "counts": counts}


def _log_pair(pair: dict[str, Any]) -> None:
    verdict = pair["verdict"]
    left, right = pair["sides"]
    emit = logger.warning if verdict.startswith("duplicate") else logger.info

    logger.info("")
    emit("%s", verdict.upper())
    for side in (left, right):
        emit(
            "    esid=%s title=%r email=%r identity=%s owned=%d linked=%d token=%s",
            side["_id"],
            side["title"],
            side["email"],
            side["identity"] or "(none)",
            side["owned_count"],
            side["linked_count"],
            "yes" if side["has_token"] else "NO",
        )
    if pair["shared_channel_ids"]:
        emit("    shared owned channel id(s): %s", ", ".join(pair["shared_channel_ids"]))

    if verdict == "duplicate_identity":
        keep, drop = sorted((left, right), key=lambda s: (-s["linked_count"], s["_id"]))
        emit(
            "    -> ONE identity in two docs. Suggested keep=%s (most linked channels), "
            "re-home %s's channels onto it, then retire it. NOT done by this tool.",
            keep["_id"],
            drop["_id"],
        )
    elif verdict == "duplicate_owned_set":
        emit(
            "    -> PROBABLE duplicate (sub-less, overlapping owned sets). Confirm the owning "
            "identity first: python -m tools.backfill_youtube_identity --account %s",
            left["_id"],
        )
    elif verdict == "distinct_identity":
        emit("    -> NOT a duplicate: same title, different Google identities. Correct as-is.")
    else:
        emit(
            "    -> UNKNOWABLE from stored state (missing identity and/or owned set). Run "
            "backfill_youtube_identity, or have the owner authorize, then re-run."
        )


def run() -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    accounts = _fetch_all_accounts(es)
    report = build_report(accounts, linked_of=lambda esid: _linked_channels(es, esid))
    counts = report["counts"]
    dupes = counts.get("duplicate_identity", 0) + counts.get("duplicate_owned_set", 0)

    logger.info(
        "%d %s docs, %d pair(s) flagged — duplicate_identity=%d duplicate_owned_set=%d "
        "distinct_identity=%d title_collision_unknown=%d",
        len(accounts),
        ACCOUNTS_INDEX,
        len(report["pairs"]),
        counts.get("duplicate_identity", 0),
        counts.get("duplicate_owned_set", 0),
        counts.get("distinct_identity", 0),
        counts.get("title_collision_unknown", 0),
    )

    for pair in report["pairs"]:
        _log_pair(pair)

    if not report["pairs"]:
        logger.info("No duplicate or ambiguous account-doc pairs found.")

    return 1 if dupes else 0


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

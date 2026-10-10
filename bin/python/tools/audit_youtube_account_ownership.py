"""Read-only audit: YouTube account docs whose token does not own every
channel linked to them.

 (follow-up to). One ``de_asset_youtube_accounts``
doc holds ONE refresh token, and that token's authorizing Google identity can
only read analytics for the channels it OWNS. When a doc is linked to channels
its current token does not own, those channels 403 daily and go red in the
Report Scheduler (the ``youtube_channel_access_denied`` classifier) —
but only AFTER an import breaks. This tool front-runs that: it compares, for
every account doc, the channels the token owns against the channels linked to
it, and flags any doc with orphaned channels BEFORE imports break.

Detection today is reactive (Scheduler goes red) or manual+targeted (run
``tools/split_youtube_account --account <esid>`` against a doc you already
suspect — it can't tell you WHICH docs to suspect). This tool is the index-wide
proactive check that names the suspects.

For each account doc, classify by ``linked - owned``:

* **healthy** — the token owns every linked channel (empty diff). A
  correctly-scoped doc, including a post-split single-channel doc (one team) and a
   legacy sub-less doc whose owned set covers its linked set.
* **orphaned** — the token owns none / some but not all linked channels, and no
  other account doc's token owns them either; the missing ones need re-homing
  onto per-identity docs (``split_youtube_account``).

A linked channel this token does not own but ANOTHER account doc's token does is
not orphaned: it is a stale copy left by a reconnect, the channel
imports fine through the other doc, and the fix is
``dedupe_youtube_channels``, not a split. Such channels are listed as
``owned_elsewhere`` on the row and never set the exit code.
* **unknown** — ownership is UNKNOWABLE (no token, refresh failed, or the
  channels.list probe failed). NOT a false orphan flag; a distinct bucket a
  human resolves (usually: the doc needs its owner to (re)authorize).
* **unlinked** — a tokened doc with no linked channels (informational; nothing
  to import, nothing to fix).

Read-only: never writes ES, never touches tokens or team links.

Cost: one token refresh + one Google API call per account doc per run. Run as a
nightly/weekly cron — NOT per-import (Google quota).

Exit codes: 0 = every linked doc healthy (unknown/unlinked are not failures);
1 = at least one orphaned doc; 2 = ES unavailable.

Usage (inside imports_container)::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_youtube_account_ownership
"""

from __future__ import annotations

import logging
import sys
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
from tools.split_youtube_account import (  # noqa: E402
    ACCOUNTS_INDEX,
    CHANNELS_INDEX,
    _dual_term,
    _owned_channel_ids,
)

logger = logging.getLogger(__name__)


def _fetch_all_accounts(es: Any) -> list[dict[str, Any]]:
    """Every de_asset_youtube_accounts doc as ``{"_id", ...source}``. Index
    sizes are tiny (double digits per env), so one 10000-cap search covers it —
    same posture as the sibling audit/dedupe tools."""
    resp = es.search(
        index=ACCOUNTS_INDEX,
        body={"query": {"match_all": {}}, "size": 10000},
        ignore_unavailable=True,
    )
    out = []
    for hit in resp.get("hits", {}).get("hits", []):
        src = dict(hit.get("_source") or {})
        src["_id"] = hit["_id"]
        out.append(src)
    return out


def _linked_channels(es: Any, account_esid: str) -> dict[str, str]:
    """``{channel_id: title}`` for de_asset_youtube_channels rows linked to this
    account (dual-term on youtube_account_esid — the per-env mapping trap)."""
    resp = es.search(
        index=CHANNELS_INDEX,
        body={"query": _dual_term("youtube_account_esid", account_esid), "size": 500},
    )
    linked: dict[str, str] = {}
    for hit in resp.get("hits", {}).get("hits", []):
        src = hit.get("_source") or {}
        cid = str(src.get("youtube_channel_id") or "").strip()
        if cid:
            linked[cid] = src.get("youtube_channel_title") or cid
    return linked


def classify_account(
    linked: dict[str, str],
    owned: set[str] | None,
    owned_elsewhere: set[str] | frozenset[str] = frozenset(),
) -> tuple[str, dict[str, str]]:
    """Classify one account doc from its linked vs owned channel sets.

    ``owned`` is ``None`` when ownership is unknowable (no token / refresh or
    probe failed — the ``_owned_channel_ids`` contract). ``owned_elsewhere`` is
    the set of channel ids some OTHER account doc's token owns; those are never
    orphaned here. Returns ``(kind, orphaned)`` where ``orphaned`` is
    the ``{id: title}`` subset of linked channels no token owns (empty unless
    kind == "orphaned").
    """
    if owned is None:
        return "unknown", {}
    if not linked:
        return "unlinked", {}
    orphaned = {cid: title for cid, title in linked.items() if cid not in owned and cid not in owned_elsewhere}
    if orphaned:
        return "orphaned", orphaned
    return "healthy", {}


def build_report(
    accounts: list[dict[str, Any]],
    linked_of: Callable[[str], dict[str, str]],
    owned_of: Callable[[dict[str, Any]], set[str] | None],
) -> dict[str, Any]:
    """Pure report builder — I/O injected via ``linked_of`` / ``owned_of`` so it
    is unit-testable without ES or Google. Returns::

        {"accounts": [{"_id", "title", "kind", "linked_count",
                       "orphaned": {id: title},
                       "owned_elsewhere": {id: [account_esid, ...]}}],
         "counts": {kind: n}}

    Every account's owned set is read before any is classified, so a channel
    owned by a later account in the list is still recognised (one ``owned_of``
    call per account, as before).
    """
    probed = [(acc, linked_of(acc["_id"]), owned_of(acc)) for acc in accounts]
    owners: dict[str, set[str]] = {}
    for acc, _linked, owned in probed:
        for cid in owned or ():
            owners.setdefault(cid, set()).add(acc["_id"])

    rows = []
    counts: dict[str, int] = {"healthy": 0, "orphaned": 0, "unknown": 0, "unlinked": 0}
    for acc, linked, owned in probed:
        esid = acc["_id"]
        not_owned_here = [cid for cid in linked if cid not in (owned or set())]
        elsewhere = {cid: sorted(owners[cid] - {esid}) for cid in not_owned_here if owners.get(cid, set()) - {esid}}
        kind, orphaned = classify_account(linked, owned, set(elsewhere))
        counts[kind] = counts.get(kind, 0) + 1
        rows.append(
            {
                "_id": esid,
                "title": acc.get("youtube_account_title") or esid,
                "kind": kind,
                "linked_count": len(linked),
                "orphaned": orphaned,
                "owned_elsewhere": elsewhere if owned is not None else {},
            }
        )
    return {"accounts": rows, "counts": counts}


def run() -> int:
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to ES; aborting.")
        return 2

    accounts = _fetch_all_accounts(es)
    report = build_report(
        accounts,
        linked_of=lambda esid: _linked_channels(es, esid),
        owned_of=_owned_channel_ids,
    )
    counts = report["counts"]

    logger.info(
        "%d %s docs — healthy=%d orphaned=%d unknown=%d unlinked=%d",
        len(accounts),
        ACCOUNTS_INDEX,
        counts.get("healthy", 0),
        counts.get("orphaned", 0),
        counts.get("unknown", 0),
        counts.get("unlinked", 0),
    )

    for row in report["accounts"]:
        if row["kind"] == "orphaned":
            names = ", ".join(f"{t} ({c})" for c, t in sorted(row["orphaned"].items(), key=lambda kv: kv[1]))
            logger.info("")
            logger.warning(
                "ORPHANED esid=%s title=%r — token does not own %d of %d linked channel(s): %s",
                row["_id"],
                row["title"],
                len(row["orphaned"]),
                row["linked_count"],
                names,
            )
            logger.warning(
                "    fix: python -m tools.split_youtube_account --account %s   (then owners authorize)",
                row["_id"],
            )
        if row["owned_elsewhere"]:
            logger.info(
                "STALE COPY esid=%s title=%r — %d linked channel(s) are owned by another account doc: %s",
                row["_id"],
                row["title"],
                len(row["owned_elsewhere"]),
                ", ".join(f"{cid} -> {'/'.join(accts)}" for cid, accts in sorted(row["owned_elsewhere"].items())),
            )
            logger.info("    fix: python -m tools.dedupe_youtube_channels")
        if row["kind"] == "unknown":
            logger.warning(
                "UNKNOWN  esid=%s title=%r — ownership unknowable (no token / refresh or probe failed); "
                "owner must (re)authorize",
                row["_id"],
                row["title"],
            )

    return 1 if counts.get("orphaned", 0) else 0


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        level=logging.INFO,
        stream=sys.stderr,
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())

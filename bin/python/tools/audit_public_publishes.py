"""Read-only audit of every public dashboard link (publish entry) per team.

Threat: missionaries forward dashboard links in chat apps
that hostile states monitor. A 256-bit token is unguessable, but a link minted
with ``never_expires`` lives forever and needs no second factor. Before adding
passcodes and a default expiry, a teammate needs to see what is already out there.

For every doc in ``de_asset_echarts_dashboards`` the tool walks the same entry
set the public viewer honours: the ``publishes[]`` list **plus** the
legacy pre-664 flat-field slot (``publish_token`` / ``publish_expires_at`` at
the top of the doc) when no list entry covers the same (team, org) scope. That
mirrors ``_iter_publish_entries`` in ``dashboard_builder/routes.py`` — a doc
that was never re-published is still reachable via its flat slot,
so an audit that read only ``publishes[]`` would under-count.

Each entry is classified:

* ``never_expires`` — enabled token, ``publish_expires_at`` null/missing. These
  are the rows the ticket asks a teammate to review.
* ``active`` — enabled token, expiry in the future (days remaining shown).
* ``expired`` — expiry in the past; the viewer already refuses it.
* ``disabled`` — token present but ``publish_enabled`` false (revoked).

Grouping is by the entry's ``published_team_title`` scope snapshot (falling back
to ``published_org_title``, then "(no scope)"), because that is the team whose
data the link exposes. Tokens are never printed — only the last 4 characters,
enough to match a row against the publish dialog.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_public_publishes # human table
    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_public_publishes --json # machine-readable
    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_public_publishes --only never_expires

This tool never writes. It has no ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# When piped via ``python3 -`` (audit-before-deploy) ``__file__`` is ``<stdin>``
# and has too few parents; fall back to the container path so the sys.path
# probe below still runs.
_HERE = Path(__file__).resolve().parent if globals().get("__file__", "<stdin>") != "<stdin>" else Path("/app/python/tools")
_parents = list(_HERE.parents)
for _candidate in (
    *([_parents[1]] if len(_parents) > 1 else []),
    *([_parents[2] / "bin" / "python"] if len(_parents) > 2 else []),
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_all_documents_paginated, get_elasticsearch_client  # noqa: E402

logger = logging.getLogger(__name__)

DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

# Keep in sync with ``_PUBLISH_ENTRY_FIELDS`` in dashboard_builder/routes.py.
PUBLISH_ENTRY_FIELDS = (
    "publish_token",
    "publish_enabled",
    "published_at",
    "publish_expires_at",
    "published_org_title",
    "published_team_title",
    "display_time_selector",
)

STATES = ("never_expires", "active", "expired", "disabled")


def _scope_key(entry: dict) -> tuple[str, str]:
    team = entry.get("published_team_title")
    org = entry.get("published_org_title")
    t = team.strip() if isinstance(team, str) else ""
    o = org.strip() if isinstance(org, str) else ""
    return (t, o)


def iter_publish_entries(source: dict) -> list[dict]:
    """Return every publish entry on a dashboard doc, list entries first.

    Mirrors ``routes._iter_publish_entries``: the legacy flat-field slot is
    included only when it carries a token AND no ``publishes[]`` entry shares
    its (team, org) scope.
    """
    entries = source.get("publishes") or []
    out: list[dict] = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    if source.get("publish_token"):
        legacy = {key: source.get(key) for key in PUBLISH_ENTRY_FIELDS}
        legacy["_legacy_flat"] = True
        if not any(_scope_key(e) == _scope_key(legacy) for e in out):
            out.append(legacy)
    return out


def parse_expiry(raw) -> datetime | None:
    """ISO-8601 (naive or aware, ``Z`` accepted) -> aware UTC datetime, else None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def classify_entry(entry: dict, *, now: datetime) -> str:
    """One of ``STATES`` for a publish entry, judged at ``now``."""
    token = entry.get("publish_token")
    if not token or not entry.get("publish_enabled"):
        return "disabled"
    expires_at = parse_expiry(entry.get("publish_expires_at"))
    if expires_at is None:
        return "never_expires"
    return "expired" if now >= expires_at else "active"


def build_rows(docs: list[dict], *, now: datetime) -> list[dict]:
    """Flatten dashboard docs into one audit row per publish entry."""
    rows: list[dict] = []
    for doc in docs:
        source = doc.get("_source") or {}
        for entry in iter_publish_entries(source):
            state = classify_entry(entry, now=now)
            expires_at = parse_expiry(entry.get("publish_expires_at"))
            team, org = _scope_key(entry)
            token = entry.get("publish_token") or ""
            rows.append(
                {
                    "team": team or org or "(no scope)",
                    "org": org,
                    "dashboard_id": doc.get("_id"),
                    "dashboard_title": source.get("title") or "Untitled",
                    "workspace_esid": source.get("workspace_esid") or "",
                    "state": state,
                    "published_at": entry.get("published_at"),
                    "expires_at": entry.get("publish_expires_at"),
                    "days_left": ((expires_at - now).days if expires_at is not None and state == "active" else None),
                    "token_tail": token[-4:] if token else "",
                    "legacy_flat": bool(entry.get("_legacy_flat")),
                    "display_time_selector": bool(entry.get("display_time_selector")),
                }
            )
    rows.sort(key=lambda r: (r["team"].lower(), r["state"], r["dashboard_title"].lower()))
    return rows


def summarize(rows: list[dict]) -> dict[str, dict[str, int]]:
    """Per-team counts by state, plus a ``_total`` team."""
    summary: dict[str, dict[str, int]] = defaultdict(lambda: {s: 0 for s in STATES})
    for row in rows:
        summary[row["team"]][row["state"]] += 1
        summary["_total"][row["state"]] += 1
    return dict(summary)


def _fmt_ts(raw) -> str:
    return raw[:10] if isinstance(raw, str) and raw else "-"


def render_table(rows: list[dict], summary: dict[str, dict[str, int]], *, now: datetime) -> str:
    lines = [f"Public publish audit — {DASHBOARDS_INDEX} — as of {now.date().isoformat()} UTC", ""]
    if not rows:
        lines.append("No publish entries found.")
        return "\n".join(lines)

    lines.append(
        f"{'team':<28} {'state':<14} {'published':<11} {'expires':<11} {'days':>5}  {'legacy':<6} {'tok':<4} dashboard"
    )
    lines.append("-" * 110)
    for row in rows:
        days = "" if row["days_left"] is None else str(row["days_left"])
        lines.append(
            f"{row['team'][:28]:<28} {row['state']:<14} {_fmt_ts(row['published_at']):<11} "
            f"{_fmt_ts(row['expires_at']):<11} {days:>5}  {('yes' if row['legacy_flat'] else ''):<6} "
            f"{row['token_tail']:<4} {row['dashboard_title']} [{row['dashboard_id']}]"
        )

    lines += ["", "Per-team summary", f"{'team':<28} " + " ".join(f"{s:>14}" for s in STATES), "-" * 90]
    for team in sorted((t for t in summary if t != "_total"), key=str.lower):
        counts = summary[team]
        lines.append(f"{team[:28]:<28} " + " ".join(f"{counts[s]:>14}" for s in STATES))
    total = summary.get("_total", {s: 0 for s in STATES})
    lines.append("-" * 90)
    lines.append(f"{'TOTAL':<28} " + " ".join(f"{total[s]:>14}" for s in STATES))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    parser.add_argument("--only", choices=STATES, help="restrict rows to one state")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    now = datetime.now(tz=timezone.utc)

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch; refusing to report an empty audit.")
        return 2
    # get_all_documents_paginated returns [] on ANY error, which would read as
    # "nothing is published". Count first so an unreachable index fails loudly.
    expected = int((es.count(index=DASHBOARDS_INDEX) or {}).get("count", 0))
    docs = list(get_all_documents_paginated(DASHBOARDS_INDEX, es=es))
    if len(docs) != expected:
        logger.error(
            "Fetched %d dashboard docs but the index holds %d; aborting rather than under-report.",
            len(docs),
            expected,
        )
        return 2
    rows = build_rows(docs, now=now)
    if args.only:
        rows = [r for r in rows if r["state"] == args.only]
    summary = summarize(rows)

    if args.json:
        print(json.dumps({"as_of": now.isoformat(), "rows": rows, "summary": summary}, indent=2, default=str))
    else:
        print(render_table(rows, summary, now=now))
    return 0


if __name__ == "__main__":
    sys.exit(main())

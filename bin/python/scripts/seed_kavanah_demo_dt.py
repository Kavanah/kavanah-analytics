#!/usr/bin/env python3
"""seed the Kavanah demo Disciple Tools index.

Writes DT-shaped documents to the team-keyed esid-form index
``{team_esid}__disciple_tools`` (; was the slug-form
``kavanah_team_disciple_tools`` reverse-alias name retired those)
so the dashboard_builder Summary Report
"Jesus Journey" section renders these exact 90-day totals:

    Baptisms        350   (contact docs, meta.milestones = milestone_baptized)
    New Groups      110   (group docs, post_type = groups)
    New Contacts   2515   (contact docs, post_type = contacts)
    First Meetings 1223   (contact docs, meta.seeker_path = met)
    New Churches     72   (group docs, meta.church_start_date exists)

Realistic overlap model: baptisms and
first-meetings are SUBSETS of contacts (a baptized person is a contact),
and churches are a SUBSET of groups. So the index holds 2515 contact docs
+ 110 group docs (not disjoint per-metric populations). The exact counts
are guaranteed by construction, not random sampling — so the report shows
the target numbers precisely.

Distinct from the cross-tenant ``disciple-tools`` alias (that's,
deferred). This index is team-keyed; the Kavanah-Team-scoped user reads it
through the normal Layer-1 allowlist (``disciple_tools`` was added
to KNOWN_SOURCES Phase 46).

Idempotent + reversible: ``--reseed`` deletes the index first; ``--delete``
removes it entirely. Docs are dated across the trailing 90 days so the
report's time-range selector moves the numbers.

this script IS the daily demo DT pipeline. The push→pull
cutover orphaned the old path (generate_demo_data.py → disciple-tools-staging
→ promote), so the daily demo cron now runs ``--reseed`` here instead. Each
run rebuilds the trailing-90-day window from today, which keeps the curated
totals exact AND inside the report's rolling date range forever.

Usage (inside imports_container):
    python3 /app/python/scripts/seed_kavanah_demo_dt.py            # seed (refuses if index exists)
    python3 /app/python/scripts/seed_kavanah_demo_dt.py --reseed   # delete + reseed (safe re-run; daily cron)
    python3 /app/python/scripts/seed_kavanah_demo_dt.py --dry-run  # report planned counts, write nothing
    python3 /app/python/scripts/seed_kavanah_demo_dt.py --delete   # remove the index
    python3 /app/python/scripts/seed_kavanah_demo_dt.py --verify   # query the 5 totals and print them
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, "/app/python")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from lib.es_helpers import (  # noqa: E402
    build_data_index_name,
    create_index_if_not_exists,
    get_alias_backing_index,
    get_elasticsearch_client,
    is_alias,
    put_regular_index_template,
)
from migration.schema_loader import get_schema_for_index_type  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TEAM_TITLE = "Kavanah Team"
ORG_TITLE = "Kavanah"
SOURCE = "disciple_tools"


def resolve_team_esid(es) -> str | None:
    """The ``de_asset_teams`` ``_id`` of the demo team, or ``None`` (logged) when it is not exactly one.

    the index name is derived from the team esid like every real
    importer. ``team_title`` is keyword-mapped so the ``term`` query
    is exact; a foreign "Kavanah Team" has existed in another workspace
    , so hits are narrowed to ORG_TITLE first. Zero or several
    candidates is a refusal — seeding under a guessed esid would write demo
    data onto another team's index.
    """
    resp = es.search(index="de_asset_teams", body={"size": 5, "query": {"term": {"team_title": TEAM_TITLE}}})
    hits = [h for h in resp["hits"]["hits"] if h["_source"].get("team_title") == TEAM_TITLE]

    def _in_org(src: dict) -> bool:
        org = src.get("organization_title")
        return ORG_TITLE in (org if isinstance(org, list) else [org])

    candidates = [h for h in hits if _in_org(h["_source"])] or hits
    if len(candidates) != 1:
        logger.error(
            "team_title %r matched %d team(s) in de_asset_teams (org %r) — need exactly one; refusing to seed.",
            TEAM_TITLE,
            len(candidates),
            ORG_TITLE,
        )
        return None
    return candidates[0]["_id"]


def demo_index_name(team_esid: str) -> str:
    """``{team_esid}__disciple_tools`` — the esid-form name the dashboards' ``*_disciple_tools`` wildcard reads."""
    return build_data_index_name(team_esid, SOURCE)


WINDOW_DAYS = 90
SEED = 834  # deterministic builds → reproducible totals across re-runs

# Target 90-day totals (the report's Jesus Journey metrics).
N_CONTACTS = 2515
N_BAPTISMS = 350  # subset of contacts
N_FIRST_MEETINGS = 1223  # subset of contacts (meta.seeker_path = met)
N_GROUPS = 110
N_CHURCHES = 72  # subset of groups (meta.church_start_date exists)

# Non-target field vocab (cosmetic variety; not asserted by the report).
_NAMES = [
    "Alex Johnson",
    "Maria Garcia",
    "James Lee",
    "Sarah Williams",
    "Ahmed Hassan",
    "Priya Sharma",
    "Carlos Rivera",
    "Aisha Okonkwo",
    "Lena Müller",
    "David Chen",
    "Fatima Al-Rashid",
    "Samuel Osei",
]
_GROUP_NAMES = [
    "Discovery Bible Study",
    "Youth Fellowship",
    "Prayer Group",
    "New Believers",
    "Community Outreach",
    "Women's Ministry",
    "Men's Fellowship",
    "Young Adults",
    "Family Group",
]
# seeker_path values OTHER than "met" — used for the contacts that must
# NOT count toward First Meetings, so the 1223 total is exact.
_NON_MET_PATHS = ["established", "bible", "gospel", "baptized"]
# milestones OTHER than baptized — for contacts that must NOT count toward
# Baptisms, so the 350 total is exact.
_NON_BAPTIZED_MILESTONES = [
    "milestone_belief",
    "milestone_can_share",
    "milestone_sharing",
    "milestone_reading_bible",
]
_FAITH = ["believer", "seeker", "curious", "disciple"]
_SOURCES = ["personal", "facebook", "instagram", "website", "event"]
# contact_place feeds the DT map charts' address-geocode fallback
# (the old daily generator provided it; the seeder didn't, so demo maps went
# blank when this script became the only demo DT source).
_CITIES = [
    "New York",
    "London",
    "Tokyo",
    "Sydney",
    "Berlin",
    "Sao Paulo",
    "Lagos",
    "Mumbai",
    "Dubai",
    "Toronto",
    "Nairobi",
    "Seoul",
    "Mexico City",
    "Jakarta",
    "Cairo",
]
_GROUP_STATUSES = ["active", "inactive", "paused"]
_GROUP_TYPES = ["pre-group", "group", "church"]
_HEALTH = [
    "church_bible",
    "church_praise",
    "church_prayer",
    "church_fellowship",
    "church_giving",
    "church_communion",
]


def _spread_date(i: int, total: int, rng: random.Random) -> datetime:
    """Map doc index i∈[0,total) to a datetime within the trailing
    WINDOW_DAYS. Even spread + a little jitter so per-month buckets vary
    and the report's range selector moves the totals."""
    today = date.today()
    # Even distribution across the window, oldest first.
    day_offset = int((i / max(total, 1)) * (WINDOW_DAYS - 1))
    d = today - timedelta(days=(WINDOW_DAYS - 1 - day_offset))
    return datetime(d.year, d.month, d.day, rng.randint(6, 22), rng.randint(0, 59), rng.randint(0, 59))


def _contact_doc(idx: int, baptized: bool, met: bool, rng: random.Random) -> dict:
    created_dt = _spread_date(idx, N_CONTACTS, rng)
    # follow-up: ISO-8601 + tz so the report's `date_created.keyword`
    # range filter (now-90d/d date-math) compares correctly. The legacy
    # "%Y-%m-%d %H:%M:%S" string lexicographically failed the range and zeroed
    # every date-filtered DT metric (confirmed on prod + staging 2026-05-30).
    created = created_dt.isoformat() + "+00:00"
    created_ms = int(created_dt.timestamp() * 1000)
    milestone = "milestone_baptized" if baptized else rng.choice(_NON_BAPTIZED_MILESTONES)
    seeker_path = "met" if met else rng.choice(_NON_MET_PATHS)
    baptism_date = ""
    baptism_date_ms = 0
    if baptized:
        bap_dt = created_dt + timedelta(days=rng.randint(7, 60))
        baptism_date = bap_dt.isoformat() + "+00:00"
        baptism_date_ms = int(bap_dt.timestamp() * 1000)
    meta = {
        "type": rng.choice(["access", "user"]),
        "overall_status": rng.choice(["active", "paused", "closed"]),
        "faith_status": rng.choice(_FAITH),
        "seeker_path": seeker_path,
        "sources": rng.choice(_SOURCES),
        "assigned_to": f"user-{rng.randint(1, 20)}",
        "milestones": milestone,
        "milestone": {"formatted": milestone},
        "gender": rng.choice(["male", "female"]),
        "age": rng.choice(["18-25", "26-35", "36-45", "46-55"]),
        "first_contact_date": created,
        "first_contact_date_ms": created_ms,
        "baptism_date": baptism_date,
        "baptism_date_ms": baptism_date_ms,
        # fields the retired daily generator provided that demo
        # dashboards still read — maps (contact_place) and the quick-button
        # map stats (multiplier_contacts / spiritual_conversations). Ints, not
        # strings, matching the pull-schema type (long) so sum
        # aggregations behave the same on demo and real indices.
        "contact_place": rng.choice(_CITIES),
        "quick_button_contact_established": rng.randint(1, 5),
        "quick_button_meeting_scheduled": rng.randint(1, 4),
        "quick_button_meeting_complete": rng.randint(1, 8),
        "quick_button_no_answer": rng.randint(1, 3),
    }
    # Like church_start_date on groups: ES `exists` matches an empty string,
    # so non-baptized contacts must OMIT baptism_generation, not blank it.
    if baptized:
        meta["baptism_generation"] = rng.randint(1, 4)
    return {
        "@timestamp": created_dt.isoformat() + "+00:00",
        "ID": 100000 + idx,
        "post_type": "contacts",
        "title": rng.choice(_NAMES),
        "content": "",
        "author": str(rng.randint(1, 10)),
        "date_created": created,
        "date_created_ms": created_ms,
        "date_modified": created,
        "date_modified_ms": created_ms,
        "team_title": TEAM_TITLE,
        "organization_title": ORG_TITLE,
        "meta": meta,
    }


def _group_doc(idx: int, is_church: bool, rng: random.Random) -> dict:
    created_dt = _spread_date(idx, N_GROUPS, rng)
    # follow-up: ISO-8601 + tz — see _contact_doc note.
    created = created_dt.isoformat() + "+00:00"
    created_ms = int(created_dt.timestamp() * 1000)
    start_dt = created_dt + timedelta(days=rng.randint(0, 14))
    start_date = start_dt.isoformat() + "+00:00"
    start_date_ms = int(start_dt.timestamp() * 1000)
    meta = {
        "type": "access",
        "group_status": "active" if is_church else rng.choice(_GROUP_STATUSES),
        "group_type": "church" if is_church else rng.choice([t for t in _GROUP_TYPES if t != "church"]),
        "health_metrics": rng.choice(_HEALTH),
        "member_count": str(rng.randint(3, 25)),
        "start_date": start_date,
        "start_date_ms": start_date_ms,
        "assigned_to": f"user-{rng.randint(1, 20)}",
    }
    # New Churches counts `exists: meta.church_start_date`. ES `exists` matches
    # an empty string "" (the field is PRESENT), so non-church groups must OMIT
    # the field entirely — not set it to "". Otherwise all 110 groups count as
    # churches. (This is the churches=110 bug from the first staging --reseed.)
    if is_church:
        church_dt = start_dt + timedelta(days=rng.randint(0, 30))
        meta["church_start_date"] = church_dt.isoformat() + "+00:00"
        meta["church_start_date_ms"] = int(church_dt.timestamp() * 1000)
    return {
        "@timestamp": created_dt.isoformat() + "+00:00",
        "ID": 200000 + idx,
        "post_type": "groups",
        "title": rng.choice(_GROUP_NAMES),
        "content": "",
        "author": str(rng.randint(1, 10)),
        "date_created": created,
        "date_created_ms": created_ms,
        "date_modified": created,
        "date_modified_ms": created_ms,
        "team_title": TEAM_TITLE,
        "organization_title": ORG_TITLE,
        "meta": meta,
    }


def _spread_flags(total: int, n: int) -> set[int]:
    """Exactly ``n`` indices spread evenly across ``[0, total)``.

    ``k * total // n`` is strictly increasing for ``total >= n``, so the set
    has exactly ``n`` members with near-uniform spacing. Doc index maps to
    the doc's date (``_spread_date``, oldest first), so even INDEX spacing
    is even DATE spacing.
    """
    return {k * total // n for k in range(n)}


def build_docs() -> list[dict]:
    """Build the full doc set with exact subset counts.

    Contacts: 2515 total; N_BAPTISMS carry milestone_baptized and
    N_FIRST_MEETINGS carry seeker_path=met. The two subsets are independent
    (a contact can be both baptized and met — realistic), and each
    aggregation counts its own field, so both totals are exact.
    Groups: 110 total, N_CHURCHES carry church_start_date.

    flagged docs are INTERLEAVED across the index range, not
    front-loaded. Index order maps to date order (oldest first), so the old
    ``i < N`` rule stacked every baptism/meeting/church in the oldest slice
    of the 90-day window — a 30-day report range showed 0 for all three.
    Even interleave keeps the 90-day totals exact while any sub-window
    shows proportional values.
    """
    rng = random.Random(SEED)
    baptized_idx = _spread_flags(N_CONTACTS, N_BAPTISMS)
    met_idx = _spread_flags(N_CONTACTS, N_FIRST_MEETINGS)
    church_idx = _spread_flags(N_GROUPS, N_CHURCHES)
    docs: list[dict] = []
    for i in range(N_CONTACTS):
        docs.append(_contact_doc(i, baptized=(i in baptized_idx), met=(i in met_idx), rng=rng))
    for j in range(N_GROUPS):
        docs.append(_group_doc(j, is_church=(j in church_idx), rng=rng))
    return docs


def _planned_counts(docs: list[dict]) -> dict[str, int]:
    return {
        "contacts": sum(1 for d in docs if d["post_type"] == "contacts"),
        "baptisms": sum(1 for d in docs if d["meta"].get("milestones") == "milestone_baptized"),
        "first_meetings": sum(1 for d in docs if d["meta"].get("seeker_path") == "met"),
        "groups": sum(1 for d in docs if d["post_type"] == "groups"),
        # Mirror ES `exists`: count field PRESENCE, not truthiness. The builder
        # omits church_start_date on non-church groups so the two agree at 72.
        "churches": sum(1 for d in docs if "church_start_date" in d["meta"]),
    }


def _verify(es, index_name: str) -> dict[str, int]:
    """Query the live index the same way the report does and return the 5 totals."""

    def _count(query):
        return es.count(index=index_name, body={"query": query})["count"]

    def _term_either(field: str, value: str) -> dict:
        # Match both mapping spellings: bare keyword (unified pull schema —
        # what the 2026-07-13 index repair applied) and text+.keyword (the
        # legacy dynamic-mapped index this verify was written against).
        # A .keyword-only term counted 0 on the schema-mapped index and
        # failed every daily --reseed cron despite a good seed.
        return {
            "bool": {
                "should": [
                    {"term": {field: value}},
                    {"term": {f"{field}.keyword": value}},
                ],
                "minimum_should_match": 1,
            }
        }

    return {
        "contacts": _count(_term_either("post_type", "contacts")),
        "baptisms": _count(_term_either("meta.milestones", "milestone_baptized")),
        "first_meetings": _count(_term_either("meta.seeker_path", "met")),
        "groups": _count(_term_either("post_type", "groups")),
        "churches": _count({"exists": {"field": "meta.church_start_date"}}),
    }


_TARGETS = {
    "contacts": N_CONTACTS,
    "baptisms": N_BAPTISMS,
    "first_meetings": N_FIRST_MEETINGS,
    "groups": N_GROUPS,
    "churches": N_CHURCHES,
}


def _drop(es, name):
    """Clear `name` whether it's an ALIAS, a regular index, or a data stream.

    on prod ``kavanah_team_disciple_tools`` is an ALIAS (→ the
     ESID-keyed concrete index ``{esid}__disciple_tools``), not a
    regular index. ``indices.delete`` on an alias is rejected by ES with a
    400 ``illegal_argument_exception`` ("matches an alias, specify the
    corresponding concrete indices"); the old code swallowed that, so the
    delete silently never happened and the subsequent bulk APPENDED a second
    copy → the index doubled (2625 → 5250). On dev/staging the name is a
    regular index the seeder created, so delete succeeded and the bug never
    surfaced — the classic env-divergence trap.

    For the alias case we ``delete_by_query`` match_all on the alias: this
    empties the backing index while PRESERVING the index, its mapping, and
    the alias binding — which the create_index_if_not_exists in main() relies
    on (deleting the concrete index would orphan the alias and let the seeder
    recreate a plain regular index by that name, breaking the ESID scheme).
    Regular-index and data-stream remnants keep the original delete paths.
    """
    removed = False
    # ALIAS (prod): purge docs via delete_by_query; keep index + alias + mapping.
    if is_alias(es, name):
        backing = get_alias_backing_index(es, name) or name
        es.delete_by_query(
            index=name,
            body={"query": {"match_all": {}}},
            refresh=True,
            conflicts="proceed",
        )
        logger.info("Cleared alias '%s' (concrete index '%s') via delete_by_query.", name, backing)
        return True
    # Regular index (dev/staging): delete it outright.
    try:
        if es.indices.exists(index=name):
            es.indices.delete(index=name)
            removed = True
    except Exception as e:  # noqa: BLE001 — best-effort cleanup; logged
        logger.warning("indices.delete('%s') failed (may be a data stream): %s", name, e)
    try:
        # Idempotent: no-op / 404 when there's no data stream by this name.
        es.indices.delete_data_stream(name=name)
        removed = True
    except Exception:
        pass
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the Kavanah demo Disciple Tools index.")
    parser.add_argument("--reseed", action="store_true", help="Delete the index first, then seed (safe re-run).")
    parser.add_argument("--delete", action="store_true", help="Delete the index and exit (no seeding).")
    parser.add_argument("--dry-run", action="store_true", help="Report planned counts; write nothing.")
    parser.add_argument("--verify", action="store_true", help="Query the live index's 5 totals and exit.")
    args = parser.parse_args()

    docs = build_docs()
    planned = _planned_counts(docs)

    # Self-check: the builder must produce exactly the targets, independent of ES.
    mismatches = {k: (planned[k], _TARGETS[k]) for k in _TARGETS if planned[k] != _TARGETS[k]}
    if mismatches:
        logger.error("Builder produced wrong counts (got, want): %s", mismatches)
        return 1
    logger.info("Planned counts (exact): %s — %d docs total", planned, len(docs))

    if args.dry_run:
        logger.info("[DRY RUN] No changes made.")
        return 0

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect to Elasticsearch.")
        return 1

    team_esid = resolve_team_esid(es)
    if team_esid is None:
        return 1
    index_name = demo_index_name(team_esid)
    logger.info("Demo DT index: %s (team esid %s)", index_name, team_esid)
    # stamp the team esid the way the real DT importer does, so
    # esid-scoped queries (tenant scoping filters on team_esid) see the demo.
    for d in docs:
        d["team_esid"] = team_esid

    if args.verify:
        live = _verify(es, index_name)
        ok = all(live[k] == _TARGETS[k] for k in _TARGETS)
        logger.info("Live index totals: %s", live)
        logger.info("Targets:           %s", _TARGETS)
        logger.info("MATCH" if ok else "MISMATCH")
        return 0 if ok else 1

    if args.delete:
        if _drop(es, index_name):
            logger.info("Deleted '%s' (index and/or data stream).", index_name)
        else:
            logger.info("'%s' did not exist — nothing to delete.", index_name)
        return 0

    # Guard against accidentally clobbering a real seeded index: only error
    # when a populated index already exists and the caller didn't ask to
    # reseed. A data-stream REMNANT from a prior failed run has no docs, so
    # it's safe to clear without --reseed (count() returns 0 / errors → 0).
    try:
        existing_docs = es.count(index=index_name)["count"] if es.indices.exists(index=index_name) else 0
    except Exception:  # noqa: BLE001 — a bare data stream can error on count; treat as empty remnant
        existing_docs = 0

    if existing_docs > 0 and not args.reseed:
        logger.error(
            "Index '%s' already has %d docs. Re-run with --reseed to delete + reseed, " "or --delete to remove it.",
            index_name,
            existing_docs,
        )
        return 1

    # Clear any prior alias / regular-index / data-stream remnant before
    # (re)seeding, so a previously-failed run (which can leave an empty data
    # stream) can't block the regular-index create below. On prod this empties
    # the alias-backed index in place; on dev/staging it deletes.
    _drop(es, index_name)

    # Create the index EXPLICITLY as a regular index before writing. The name
    # `kavanah_team_disciple_tools` matches a data-stream template on the
    # cluster, so letting bulk auto-create it produces a data stream — which
    # rejects plain `index` ops with "only write ops with an op_type of create
    # are allowed in data streams". A high-priority regular index template
    # (put_regular_index_template, priority 500) overrides the data-stream
    # template; create_index_if_not_exists then makes a regular index. This is
    # the same defense promote_staging.py uses for the disciple-tools-* indices.
    #
    # the template body IS the unified disciple_tools schema — the
    # same mapping the `project_schema_disciple_tools` template (260)
    # and the real DT importer use. Priority 500 still wins, so this template
    # decides the mapping, and a one-field body here let dynamic mapping
    # re-create the index drifted (team_title text+keyword, no team_esid) on
    # every 05:40 reseed — the one index the sweep could never keep
    # clean. The Summary Report / map / trends queries use the bare keyword
    # spellings (post_type, meta.*.keyword stays valid: the schema maps meta.*
    # as text + keyword), and _verify accepts both.
    _MAPPING = get_schema_for_index_type("disciple_tools")
    put_regular_index_template(es, index_name, _MAPPING, priority=500)
    if not create_index_if_not_exists(es, index_name, _MAPPING):
        logger.error("Failed to create regular index '%s'.", index_name)
        return 1
    logger.info("Created regular index '%s'.", index_name)

    # Bulk index. refresh at the end so --verify sees the docs immediately.
    from elasticsearch.helpers import bulk

    actions = [{"_index": index_name, "_source": d} for d in docs]
    success, errors = bulk(es, actions, refresh="wait_for", raise_on_error=False)
    logger.info("Indexed %d docs into '%s' (%d errors).", success, index_name, len(errors) if errors else 0)
    if errors:
        logger.error("First few bulk errors: %s", errors[:3])
        return 1

    live = _verify(es, index_name)
    ok = all(live[k] == _TARGETS[k] for k in _TARGETS)
    logger.info("Verified live totals: %s", live)
    if not ok:
        logger.error("Live totals do not match targets %s", _TARGETS)
        return 1
    logger.info("✓ Seed complete — all 5 totals match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

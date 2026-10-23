"""Migrate legacy team-title-slug data indices to ESID-keyed names.

Tenant data indices used to be named ``{sanitize_team_title(team)}_{source}``
(e.g. ``kavanah_media_fb_insights``). keys them by the team's ES
document id instead: ``{team_esid}__{source}`` (e.g. ``qLm7B__fb_insights``).
ESIDs are stable, unique cluster-wide, and survive title renames.

This script cuts an existing cluster over. For each legacy ``{slug}_{source}``
concrete index it:

  1. Reverse-resolves the team by matching ``sanitize_team_title(team_title)``
     against ``de_asset_teams`` → ``team_esid``. A slug shared by >1 team is
     **ambiguous** — it is logged, added to the report, and skipped (NOT
     migrated), because routing its docs to one of the colliding teams would
     mis-attribute data.
  2. ``_reindex`` into ``{team_esid}__{source}`` and verifies the doc count.
  3. In a single ``_aliases`` actions block, deletes the old concrete index
     AND adds a **reverse alias** ``{slug}_{source}`` → new ESID index. ES
     forbids an alias whose name equals a live concrete index, so the delete
     and the add must be in the same atomic block. The reverse alias keeps
     existing dashboard wildcards (``*_fb_insights``) matching post-migration.

No forward aliases are created. Reverse-alias cleanup is
tracked.

**Idempotent.** A re-run skips any source name that is already an alias
(already swapped) or whose target ESID index already exists with a matching
doc count. Re-running is a no-op.

**Concurrency.** The ticket said "watchdog paused during reindex windows," but
there is no import watchdog — the real primitive is ``ImportLockService``
. This script does a HARD pre-flight: if any affected connector lock
is currently held it aborts before touching any index, then acquires those
locks for the duration and releases them in a finally block. Sources with no
lock connector (youtube, chatwoot) are migrated without a lock.

**Safety.** DRY-RUN BY DEFAULT — a bare invocation reports what it
would do and mutates nothing; pass ``--apply`` to cut over. ``--apply`` must
be an explicit true value (``--apply`` / ``--apply=true``); anything else is
ignored with a warning, and combining ``--apply`` with an explicit truthy
``--dry-run`` refuses outright. Apply refuses to run unless the earlier
pre-snapshot is present OR ``--snapshot-repo`` is given and a fresh
timestamped snapshot of every legacy index is taken (and verified SUCCESS)
first. The apply-path index sweep fails CLOSED: a listing error refuses
rather than reading as "nothing to delete". Every refusal returns 1 so
import.py exits non-zero. ``_swap`` permanently deletes
each legacy concrete index, so no mutation without a restore point — the
same contract as the sibling remaps.

**Dedup floor.** For the three id-rewrite sources the
legacy index is measured BEFORE the reindex: a cardinality agg over the same
key fields the painless script concatenates (``_ID_KEY_FIELDS``) plus the
docs missing a component gives the survivors the reindex will leave;
``expected / old_count`` below ``--allow-dedup-ratio`` (default 0.5) refuses
without reindexing, and dry-run prints the ratio. After the reindex two exact
invariants (``target == pre + created``, ``created + updated == old``) must
hold before the swap.

Execution:
    # Dry-run (default) — report only.
    python3 /app/python/import.py migration migrate_to_esid_indices
    # Apply (requires the pre-snapshot to exist).
    python3 /app/python/import.py migration migrate_to_esid_indices --apply
    # Apply, taking a fresh snapshot into an explicit repo first.
    python3 /app/python/import.py migration migrate_to_esid_indices --apply --snapshot-repo=daily_repo
"""

import logging

import lib.logging
from lib.es_helpers import (
    KNOWN_SOURCES,
    build_team_lookup_cache,
    create_index_if_not_exists,
    get_elasticsearch_client,
    is_alias,
    put_regular_index_template,
    sanitize_team_title,
)
from lib.import_lock_service import ImportLockService
from migration.schema_loader import get_schema_for_index_type
from migration.snapshot_gate import (  # the shared gate replaces the per-script copy
    MigrationRefused,
    resolve_apply_mode,
    snapshot_covers,
    take_snapshot,
)

# Map each tenant source slug to the ImportLockService connector that guards
# its writer, so the migration can serialise against a concurrent cron import.
# Sources absent from this map (youtube_analytics, chatwoot_*, echo_user_stats)
# have no lock connector and are migrated without one.
_SOURCE_LOCK_CONNECTOR = {
    "fb_insights": "facebook",
    "fb_page_insights": "facebook",
    "fb_page_posts": "facebook",
    "ga_metrics": "google_analytics",
    "ig_insights": "facebook",  # IG shares the FB token surface
    "google_ads": "google_ads",
    "echo_conversations": "echo",
}

# Sources excluded from this migration:
# * echo_user_stats — subdomain-keyed, not team-keyed,
#    so it has no {slug}_{source} → {team_esid}__{source} migration path.
_SKIP_SOURCES = {"echo_user_stats"}

# dedup plausibility floor: the deduped survivors from a legacy
# index must be at least this fraction of its doc count before the swap may
# delete it. Overridable per run with --allow-dedup-ratio=<0..1> after
# reviewing the logged collapse ratio.
DEFAULT_MIN_DEDUP_RATIO = 0.5

# snapshot-or-refuse gate (the sibling contract).
# The cutover snapshot covers the legacy index family; a fresh
# --snapshot-repo snapshot is the alternative restore point.
PRESNAPSHOT_REPO = "daily_repo"
PRESNAPSHOT_NAME = "pre-esid-cleanup"


def _snapshot_exists(es, indices=None) -> bool:
    """coverage-checked pre-snapshot acceptance via the shared
    gate. Kept as a module-level seam because the / suites
    patch it by name. Existence alone no longer passes: the snapshot must
    be state SUCCESS and (when ``indices`` is given) contain every index
    about to be deleted."""
    try:
        resp = es.snapshot.get(repository=PRESNAPSHOT_REPO, snapshot=PRESNAPSHOT_NAME)
        body = resp.body if hasattr(resp, "body") else resp
        snaps = body.get("snapshots") or []
    except Exception:  # noqa: BLE001 — missing repo/snapshot
        return False
    if not snaps:
        return False
    if indices is None:
        return True
    return snapshot_covers(snaps[0], indices)


def _take_snapshot(es, repo: str, indices: list[str]) -> str:
    """delegates to the shared gate: timestamped name (a fixed
    name wedges every retry), real request_timeout, state ==
    SUCCESS check, recent-reuse on retry. Kept as a module-level seam
    because the suite patches it by name."""
    return take_snapshot(es, repo, indices, name_prefix="pre-esid-migrate")


# sources whose ESID target name matches the cluster's catch-all
# data-stream template (`default_replicas_0_for_single_node`, which has
# `data_stream: {}`). If we let _reindex auto-create the target, ES makes a DATA
# STREAM, which rejects the overwrite-style write with "only write ops with an
# op_type of create are allowed in data streams" (hit on the 2026-06-01 staging
# run for disciple_tools). The fix mirrors seed_kavanah_demo_dt.py /
# promote_staging.py: PRE-CREATE the target as a REGULAR index (a priority-500
# regular-index template overrides the data-stream template) before reindexing.
# The legacy source (e.g. kavanah_team_disciple_tools) is itself a regular index.
_DATA_STREAM_TEMPLATE_SOURCES = {"disciple_tools"}

# FB daily/page/IG historically indexed with NO explicit _id (ES
# auto-generated one), so their legacy indices contain duplicate rows and share
# no _id with the now-deterministic ESID indices. For these sources the reindex
# must RECOMPUTE the deterministic _id from the doc's own fields (matching the
# senders' _build_doc_id) so (a) legacy duplicates collapse and (b) legacy rows
# overlapping the already-populated ESID target overwrite rather than double.
#
# Each script guards on the id-component fields being present — no-data marker
# docs (which carry their own {esid}_{date}_no_data id and lack ad_id/fb_page_id/
# ig_user_id) keep their existing _id untouched.
#
# The other 7 sources already wrote deterministic _ids, so a plain reindex (which
# preserves the source doc's _id) dedups them correctly — no script needed.
#
# the id-key fields per dedup source, in concatenation order, are the
# ONE table both the painless _id-rewrite script and the pre-reindex
# expected-distinct measure are generated from, so the two cannot drift. A test
# pins the generated scripts byte-for-byte against the originals.
_ID_KEY_FIELDS = {
    "fb_insights": ("ad_id", "insight_date_start", "publisher_platform"),
    "fb_page_insights": ("fb_page_id", "kibana_date"),
    "ig_insights": ("ig_user_id", "kibana_date"),
}


def _build_id_script(fields):
    """Painless for the reindex: recompute ``_id`` from ``fields`` when every
    component is present; otherwise leave the doc's ``_id`` alone."""
    guard = " && ".join(f"ctx._source.{f} != null" for f in fields)
    concat = " + '_' + ".join(f"ctx._source.{f}" for f in fields)
    return f"if ({guard}) {{ ctx._id = {concat}; }}"


def _build_dedup_key_script(fields):
    """Painless for a keyword runtime field mirroring ``_build_id_script`` over
    ``params._source``: emits the would-be ``_id`` when every component is
    present and nothing otherwise, so a ``missing`` agg on the field counts
    exactly the docs the id-rewrite leaves untouched. Reads ``_source`` rather
    than doc values so a legacy mapping with a key as ``text`` still works —
    slower, but it is the same raw value the reindex script concatenates."""
    guard = " && ".join(f"params._source['{f}'] != null" for f in fields)
    concat = " + '_' + ".join(f"params._source['{f}']" for f in fields)
    return f"if ({guard}) {{ emit({concat}); }}"


_ID_REWRITE_SCRIPTS = {source: _build_id_script(fields) for source, fields in _ID_KEY_FIELDS.items()}

# ES caps cardinality precision_threshold at 40000: near-exact below it and a
# few percent off above — irrelevant against a 0.5 floor.
_CARDINALITY_PRECISION = 40000
_DEDUP_RUNTIME_FIELD = "dedup_id"


def _doc_count(es, index):
    """Return the doc count for an index/alias, or None on error."""
    try:
        resp = es.count(index=index)
        body = resp.body if hasattr(resp, "body") else resp
        return body.get("count")
    except Exception as e:
        logging.warning("count(%s) failed: %s", index, e)
        return None


def _expected_distinct(es, index, source):
    """Pre-reindex expected survivors of a dedup reindex of ``index``.

    Returns ``(distinct, missing)``, or ``(None, None)`` on any error so the
    caller can refuse (fail closed). ``distinct`` is the cardinality of the
    would-be deterministic ``_id`` — a keyword runtime field over the same key
    fields, in the same order, that the reindex script concatenates — and
    ``missing`` the count of docs lacking a component, which keep their unique
    legacy ``_id``. Their sum is what the reindex will leave of ``index``:
    target-independent, retry-invariant, and available in dry-run.
    """
    fields = _ID_KEY_FIELDS[source]
    body = {
        "size": 0,
        "runtime_mappings": {
            _DEDUP_RUNTIME_FIELD: {"type": "keyword", "script": {"source": _build_dedup_key_script(fields)}},
        },
        "aggs": {
            "distinct": {"cardinality": {"field": _DEDUP_RUNTIME_FIELD, "precision_threshold": _CARDINALITY_PRECISION}},
            "missing": {"missing": {"field": _DEDUP_RUNTIME_FIELD}},
        },
    }
    try:
        # The runtime field reads _source for every doc; budget it like a
        # (short) reindex rather than a count.
        resp = es.options(request_timeout=600).search(index=index, body=body)
        rd = resp.body if hasattr(resp, "body") else resp
        aggs = rd["aggregations"]
        return int(aggs["distinct"]["value"]), int(aggs["missing"]["doc_count"])
    except Exception as e:  # noqa: BLE001 — unavailable reads as None and the caller refuses (fail closed)
        logging.warning("expected-distinct agg on %s failed: %s", index, e)
        return None, None


def build_reverse_slug_map(team_cache):
    """Build ``{sanitized_slug: [team_esid, ...]}`` from the team lookup cache.

    A slug that maps to more than one team_esid is ambiguous — the caller
    skips it rather than guess which team owns the legacy index.
    """
    slug_map = {}
    for esid, info in team_cache.get("by_esid", {}).items():
        title = info.get("team_title", "")
        if not title:
            continue
        slug = sanitize_team_title(title)
        if not slug or slug == "unknown":
            continue
        slug_map.setdefault(slug, [])
        if esid not in slug_map[slug]:
            slug_map[slug].append(esid)
    return slug_map


def list_legacy_indices_for_source(es, source, strict: bool = False):
    """Return concrete (non-alias) legacy ``{slug}_{source}`` index names.

    Excludes:
      * names already in the new ESID form (``{esid}__{source}`` — they
        contain ``__`` immediately before the source suffix),
      * names that are aliases (already swapped reverse aliases),
      * names whose slug is empty after stripping the suffix.

    ``strict=True`` (the apply path) re-raises listing errors instead of
    returning ``[]``: an empty result reads as "nothing to delete" and would
    skip the snapshot gate entirely, so apply must fail CLOSED.
    """
    suffix = f"_{source}"
    try:
        resp = es.indices.get(index=f"*{suffix}", expand_wildcards="open", allow_no_indices=True)
        meta = resp.body if hasattr(resp, "body") else resp
    except Exception as e:
        if strict:
            raise
        logging.error("list indices for %s failed: %s", source, e)
        return []

    out = []
    for name in meta.keys():
        if not name.endswith(suffix):
            continue
        slug = name[: -len(suffix)]
        # New ESID form is ``{esid}__{source}`` → slug would end with ``_``
        # (the second underscore of ``__``). Skip those — they're migrated.
        if not slug or slug.endswith("_"):
            continue
        if is_alias(es, name):
            continue
        out.append(name)
    return sorted(out)


def _schema_for_source(source):
    """Return the explicit index mapping for ``source``, or None if the source
    has no registered schema (then ES dynamic mapping is the intended behavior).

    the importer creates indices via get_schema_for_index_type(source)
    -> ensure_data_index, so the live index carries an explicit mapping (e.g.
    ga_metrics: date as `date`, dims as `keyword`). A bare _reindex with no
    pre-created target lets ES auto-create with DYNAMIC mapping, degrading
    `date`->`text` and `keyword`->`text` and breaking dashboard range/terms
    queries. We mirror the importer by pre-creating the target with the schema.
    """
    try:
        return get_schema_for_index_type(source)
    except ValueError:
        # No registered schema (youtube_analytics, chatwoot, google_ads, ...);
        # these legitimately use dynamic mapping, same as the importer.
        return None


def _precreate_regular_index(es, target_index, mapping=None):
    """Force ``target_index`` to exist as a REGULAR index before reindex.

    For sources whose name matches the cluster's catch-all data-stream template
    (``_DATA_STREAM_TEMPLATE_SOURCES``), letting _reindex auto-create the dest
    would produce a data stream that rejects the overwrite write. A priority-500
    regular-index template overrides the data-stream template; then
    create_index_if_not_exists makes a regular index. Mirrors
    seed_kavanah_demo_dt.py / promote_staging.py. No-op if it already exists.

    ``mapping`` carries the source's explicit schema when one is
    registered so the target is created with the CORRECT field types instead of
    ES dynamic mapping. ``None`` -> ``{"properties": {}}`` (dynamic) preserves
    the prior behavior for schema-less sources.
    """
    if es.indices.exists(index=target_index):
        return  # already a (regular) index from a prior partial run / merge
    mapping = mapping or {"properties": {}}
    put_regular_index_template(es, target_index, mapping, priority=500)
    # a False here used to be ignored and the following _reindex
    # auto-created the target under dynamic mapping (the drift).
    if not create_index_if_not_exists(es, target_index, mapping):
        raise RuntimeError(
            f"failed to pre-create {target_index} with its schema mapping — refusing to reindex into it"
        )
    logging.info(
        "Pre-created regular index %s (mapping=%s)", target_index, "schema" if mapping.get("properties") else "dynamic"
    )


def _reindex(es, old_index, target_index, source):
    """Run the _reindex, applying the per-source _id-rewrite script if any.

    Returns (ok, detail, stats). ``ok`` is False on transport error or doc
    failures. ``stats`` carries the response ``created``/``updated`` counts —
    the retry-invariant survivor signal the floor needs.
    """
    # if the source has an explicit schema, pre-create the target with
    # it so the ESID index gets the CORRECT field types (date/keyword), not ES
    # dynamic mapping (which degrades date->text + keyword->text and breaks
    # dashboard queries). data-stream-template sources ALSO need a
    # regular-index pre-create (else _reindex auto-creates a data stream). Both
    # conditions route through _precreate_regular_index; the schema (when present)
    # is applied for either.
    schema = _schema_for_source(source)
    if schema is not None or source in _DATA_STREAM_TEMPLATE_SOURCES:
        try:
            _precreate_regular_index(es, target_index, mapping=schema)
        except Exception as e:
            logging.error("pre-create regular index %s failed: %s", target_index, e)
            return False, f"error_precreate({e})", {}
    body = {
        "source": {"index": old_index},
        # No op_type:create — we want overwrite-on-same-_id so duplicates and
        # legacy↔ESID overlaps collapse onto the deterministic key.
        "dest": {"index": target_index},
    }
    script = _ID_REWRITE_SCRIPTS.get(source)
    if script:
        body["script"] = {"lang": "painless", "source": script}
    try:
        resp = es.options(request_timeout=1800).reindex(body=body, refresh=True, wait_for_completion=True, timeout="30m")
        rd = resp.body if hasattr(resp, "body") else dict(resp)
        failures = rd.get("failures", [])
        if failures:
            logging.error(
                "%s: %d doc(s) failed to reindex; NOT swapping. samples=%s",
                old_index,
                len(failures),
                failures[:2],
            )
            return False, f"error_reindex_failures({len(failures)})", {}
        return True, "ok", {"created": rd.get("created") or 0, "updated": rd.get("updated") or 0}
    except Exception as e:
        logging.error("reindex %s → %s failed: %s", old_index, target_index, e)
        return False, f"error_reindex({e})", {}


def migrate_one(es, old_index, source, target_index, *, dry_run, min_dedup_ratio=DEFAULT_MIN_DEDUP_RATIO):
    """Reindex ``old_index`` → ``target_index`` and atomically swap in the
    reverse alias. Returns a status string for the report.

    Two modes by source:
      * Dedup sources (``_ID_REWRITE_SCRIPTS`` — FB daily/page/IG): the reindex
        recomputes a deterministic ``_id`` so legacy duplicates and any rows
        already in a populated ESID target collapse. Post-reindex count is
        EXPECTED to be ≤ legacy count, so we verify ``0 < target ≤ old`` rather
        than equality, and we merge into an existing target instead of skipping.
        a pre-reindex expected-distinct floor on the legacy index
        refuses BEFORE the reindex when it would collapse below
        ``min_dedup_ratio`` (and previews the ratio in dry-run); two exact
        post-reindex invariants then check the reindex response itself.
      * Deterministic sources (the other 7): legacy docs already carry stable
        ``_id``s; a plain reindex preserves them. We require ``target == old``
        and skip an already-populated, count-matching target (idempotent).
    """
    is_dedup = source in _ID_REWRITE_SCRIPTS

    # Idempotency: already an alias means a prior run swapped it.
    if is_alias(es, old_index):
        return "skip_already_alias"

    target_exists = False
    exists_check_failed = False
    try:
        target_exists = es.indices.exists(index=target_index)
    except Exception as e:
        # Fail CLOSED downstream: an unknown target state must not read as
        # "target absent" (pre_count=0), which would inflate dedup survivors.
        logging.warning("exists(%s) failed: %s", target_index, e)
        exists_check_failed = True

    old_count = _doc_count(es, old_index)

    # Capture the target's pre-reindex count: for a dedup MERGE the target may
    # already hold recent ESID-only docs (the importer ran ahead of migration)
    # whose deterministic _ids aren't in legacy, so the post-merge count can
    # legitimately exceed old_count by up to pre_count.
    pre_count = None if exists_check_failed else (_doc_count(es, target_index) if target_exists else 0)

    # unknown counts refuse HERE: before either branch, the
    # dry-run return, and the up-to-30-minute reindex. Both verifications below
    # need these numbers; reindexing a known-unverifiable run would waste the
    # wall-clock AND merge into the target, contaminating pre_count for the
    # next attempt. This also covers the deterministic branch, whose old
    # None-tolerant guard silently skipped verification on a transient count
    # failure and swapped unverified.
    if old_count is None or pre_count is None:
        logging.error(
            "%s → %s doc counts unavailable (old=%s pre=%s); cannot verify a swap — refusing before reindex",
            old_index,
            target_index,
            old_count,
            pre_count,
        )
        return f"error_count_unavailable(old={old_count},pre={pre_count},target=unreindexed)"

    if target_exists and not is_dedup:
        # Deterministic source: an existing target either already matches (done,
        # fall through to swap) or is an unexpected partial — skip to be safe.
        if old_count == pre_count:
            logging.info(
                "%s → %s already reindexed (%s docs); proceeding to alias swap",
                old_index,
                target_index,
                pre_count,
            )
            if dry_run:
                return f"DRY-RUN would swap reverse alias {old_index} → {target_index} (already reindexed)"
            return _swap(es, old_index, target_index)
        return f"skip_target_exists(old={old_count},target={pre_count})"

    expected_note = ""
    if is_dedup and old_count > 0:
        # pre-reindex plausibility floor on the LEGACY index. The
        # post-reindex survivors measure below (created + min(updated, pre))
        # is an upper bound: once the target already holds >= floor*old docs,
        # every legacy doc landing on one of those ids counts as a survivor,
        # so 1000 legacy docs collapsing onto 10 ids that are all already in
        # a 900-doc target read as ratio 0.90 → swap, deleting 990 docs
        # recoverable only from snapshot. expected/old_count is what the
        # reindex will actually leave, independent of the target and of
        # retries, and it is computable here — which also gives dry-run the
        # ratio preview deferred. Unknown → refuse before the
        # reindex, exactly like the counts above.
        distinct, missing = _expected_distinct(es, old_index, source)
        if distinct is None:
            logging.error(
                "%s → %s expected-distinct count unavailable (old=%s pre=%s); cannot verify a swap — refusing before reindex",
                old_index,
                target_index,
                old_count,
                pre_count,
            )
            return f"error_count_unavailable(old={old_count},pre={pre_count},expected=None,target=unreindexed)"
        expected = distinct + missing
        ratio = expected / old_count
        logging.info(
            "%s → %s expected dedup ratio %.4f (distinct=%s missing=%s old=%s pre=%s floor=%.2f)%s",
            old_index,
            target_index,
            ratio,
            distinct,
            missing,
            old_count,
            pre_count,
            min_dedup_ratio,
            " [dry-run]" if dry_run else "",
        )
        if ratio < min_dedup_ratio:
            logging.error(
                "%s → %s expected dedup ratio %.4f is below the plausibility floor %.2f; NOT reindexing. "
                "If the dedup is genuinely this aggressive, review the ratio above and re-run with "
                "--allow-dedup-ratio=<0..1>.",
                old_index,
                target_index,
                ratio,
                min_dedup_ratio,
            )
            return (
                f"error_dedup_floor(old={old_count},pre={pre_count},expected={expected},"
                f"ratio={ratio:.4f},floor={min_dedup_ratio},target=unreindexed)"
            )
        expected_note = f" [expected_distinct={distinct} missing={missing} dedup ratio {ratio:.4f} >= floor {min_dedup_ratio}]"

    if dry_run:
        merge = f" (MERGE+dedup into existing target, {pre_count} docs)" if (target_exists and is_dedup) else ""
        return f"DRY-RUN would reindex {old_count} docs {old_index} → {target_index}{merge} + reverse alias{expected_note}"

    # Reindex — with id-rewrite for dedup sources, plain otherwise.
    ok, detail, reindex_stats = _reindex(es, old_index, target_index, source)
    if not ok:
        return detail

    # Verify before swapping.
    target_count = _doc_count(es, target_index)
    if target_count is None:
        # Unavailable, not implausible — an implausible label steers the
        # operator toward the ratio override; this state needs a plain retry.
        logging.error(
            "%s → %s post-reindex count unavailable (old=%s pre=%s); cannot verify — NOT swapping",
            old_index,
            target_index,
            old_count,
            pre_count,
        )
        return f"error_count_unavailable(old={old_count},pre={pre_count},target=None)"
    if old_count == 0:
        # Empty legacy index: nothing to verify, nothing to lose — the swap
        # deletes an empty index. Explicit so the checks below can assume
        # old_count > 0 (previously this corner skipped both checks blind).
        logging.info("%s is empty; swapping without count verification", old_index)
        return _swap(es, old_index, target_index)
    if is_dedup:
        # Dedup collapses legacy duplicates and any legacy↔ESID overlap onto the
        # deterministic _id, so target_count is EXPECTED to be ≤ old_count PLUS
        # whatever ESID-only docs the target already held (pre_count). A target
        # of 0 (when legacy had docs) means the id-rewrite script silently no-op'd
        # for every doc — treat that as a failure. The upper bound (old + pre)
        # catches an all-fields-null script that failed to dedup and ballooned
        # the target.
        upper = old_count + pre_count
        if not target_count or target_count > upper:
            logging.error(
                "%s → %s dedup count implausible (old=%s pre=%s target=%s upper=%s); NOT swapping",
                old_index,
                target_index,
                old_count,
                pre_count,
                target_count,
                upper,
            )
            return f"error_dedup_count(old={old_count},pre={pre_count},target={target_count})"

        # exact invariants on the reindex response. The connector
        # locks exclude concurrent writers and the reindex ran with
        # refresh=True, so every legacy doc is exactly one created or updated
        # op, and the target grew by exactly the created ids. Anything else
        # means these numbers do not describe this run (a writer slipped past
        # the lock, a partial response) and no ratio derived from them can be
        # trusted — refuse, don't swap.
        created = reindex_stats.get("created") or 0
        updated = reindex_stats.get("updated") or 0
        if target_count != pre_count + created or created + updated != old_count:
            logging.error(
                "%s → %s reindex accounting broken (old=%s pre=%s target=%s created=%s updated=%s): "
                "expected target == pre + created and created + updated == old; NOT swapping",
                old_index,
                target_index,
                old_count,
                pre_count,
                target_count,
                created,
                updated,
            )
            return (
                f"error_dedup_invariant(old={old_count},pre={pre_count},target={target_count},"
                f"created={created},updated={updated})"
            )

        # (scan MIG-008) — plausibility FLOOR on the response. The
        # zero check above alone let a deterministic-_id collision that
        # collapsed 100,000 legacy docs to 1 survivor green-lit the
        # irreversible swap. Survivors are measured from the REINDEX
        # RESPONSE, not target-count deltas: `created` counts net-new
        # deterministic ids, and `updated` ops (capped at pre_count) count
        # legacy docs landing on ids the target already held — which is what
        # a retry after an interrupted apply and an importer-ahead merge look
        # like; a target-delta measure read both as survivors=0 and wedged
        # the --allow-dedup-ratio recovery (review). this
        # is an UPPER bound once the target is populated, so the pre-reindex
        # expected-distinct floor above is the load-bearing measure; given
        # the invariants just checked, this ratio can never refuse a run that
        # floor passed, and it stays as a second look at the response itself.
        survivors = created + min(updated, pre_count)
        ratio = survivors / old_count
        logging.info(
            "%s → %s dedup collapse ratio %.4f (created=%s updated=%s pre=%s target=%s old=%s)",
            old_index,
            target_index,
            ratio,
            created,
            updated,
            pre_count,
            target_count,
            old_count,
        )
        if ratio < min_dedup_ratio:
            logging.error(
                "%s → %s dedup collapse ratio %.4f is below the plausibility floor %.2f; NOT swapping. "
                "If the dedup is genuinely this aggressive, review the ratio above and re-run with "
                "--allow-dedup-ratio=<0..1>.",
                old_index,
                target_index,
                ratio,
                min_dedup_ratio,
            )
            return (
                f"error_dedup_floor(old={old_count},pre={pre_count},target={target_count},"
                f"ratio={ratio:.4f},floor={min_dedup_ratio})"
            )
    else:
        if target_count != old_count:
            logging.error(
                "%s → %s count mismatch (old=%s target=%s); NOT swapping",
                old_index,
                target_index,
                old_count,
                target_count,
            )
            return f"error_count_mismatch(old={old_count},target={target_count})"

    return _swap(es, old_index, target_index)


def _swap(es, old_index, target_index):
    """Atomic swap: delete the old concrete index AND add the reverse alias of
    the same name pointing at the new ESID index. ES forbids an alias whose
    name equals a live concrete index, so both actions go in one block."""
    try:
        es.indices.update_aliases(
            body={
                "actions": [
                    {"remove_index": {"index": old_index}},
                    {"add": {"index": target_index, "alias": old_index}},
                ]
            }
        )
        logging.info("✓ swapped: reverse alias %s → %s", old_index, target_index)
        return "migrated"
    except Exception as e:
        logging.error("alias swap failed on %s: %s", old_index, e)
        return f"error_swap({e})"


def _affected_connectors(legacy_by_source):
    """Return the set of lock connectors that guard any legacy index present
    on this cluster, so we only lock what we'll actually touch. Takes the
    single main() listing sweep rather than re-listing, so the lock set is
    derived from the same cluster view as the snapshot and delete sets."""
    connectors = set()
    for source, indices in legacy_by_source.items():
        connector = _SOURCE_LOCK_CONNECTOR.get(source)
        if connector and indices:
            connectors.add(connector)
    return connectors


def main(params):
    lib.logging.define("migrate-to-esid-indices")

    # DRY-RUN BY DEFAULT; --apply (or an explicit --dry_run=false)
    # mutates. The old default was write mode: a bare invocation performed the
    # full cluster-wide cutover — reindex + PERMANENT delete of every legacy
    # concrete index — with no restore point. Matches the
    # sibling contract.
    # shared parse: --apply must be an explicit true value
    # (import.py delivers `--apply=false` as the truthy string "false"), and
    # -apply + explicit truthy --dry-run refuses (the review contract).
    try:
        dry_run, snapshot_repo = resolve_apply_mode(params)
        # operator override for the dedup plausibility floor.
        # Refusals raise MigrationRefused so they exit through the same
        # boundary as the apply-mode contract (exit 1; import.py maps None to
        # exit 0). The isinstance(bool) check comes first because
        # import.py delivers a value-less flag as True and float(True)==1.0
        # would silently turn a loosening intent into the strictest floor.
        raw_ratio = params.get("allow-dedup-ratio", params.get("allow_dedup_ratio", DEFAULT_MIN_DEDUP_RATIO))
        if isinstance(raw_ratio, bool):
            raise MigrationRefused(f"--allow-dedup-ratio needs an explicit value in [0, 1], got a bare flag ({raw_ratio})")
        try:
            min_dedup_ratio = float(raw_ratio)
        except (TypeError, ValueError):
            raise MigrationRefused(f"invalid --allow-dedup-ratio {raw_ratio!r} — must be a number in [0, 1]")
        if not 0 <= min_dedup_ratio <= 1:
            raise MigrationRefused(f"invalid --allow-dedup-ratio {min_dedup_ratio} — must be within [0, 1]")
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1
    if dry_run:
        logging.info("DRY-RUN: no indices will be reindexed, deleted, or aliased. Pass --apply to mutate.")

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return 1

    # One listing sweep feeds the lock pre-flight, the snapshot gate, AND the
    # migration loop, so the set we snapshot is provably the set we delete.
    # Apply runs list strictly: a transient listing error must refuse (fail
    # CLOSED) rather than read as "zero legacy indices" and skip the gate.
    try:
        legacy_by_source = {
            source: list_legacy_indices_for_source(es, source, strict=not dry_run)
            for source in KNOWN_SOURCES
            if source not in _SKIP_SOURCES
        }
    except Exception as e:  # noqa: BLE001 — refusal boundary: see comment above
        logging.error("Refusing to apply: could not enumerate legacy indices (%s). Re-run when ES is healthy.", e)
        return 1

    # Hard lock pre-flight: abort if any affected connector lock is held.
    connectors = _affected_connectors(legacy_by_source)
    held = [c for c in sorted(connectors) if ImportLockService.is_held(c)]
    if held:
        logging.error(
            "Aborting: import lock(s) currently held for %s. Wait for the "
            "running import(s) to finish (or clear the lock) and re-run.",
            ", ".join(held),
        )
        return 1

    # refuse to mutate without a restore point: _swap permanently
    # deletes each legacy concrete index. Gate sits BEFORE lock acquisition so
    # a refused run leaves nothing to clean up. An apply run with zero legacy
    # indices left (idempotent re-run) proceeds without a snapshot — there is
    # nothing to delete.
    if not dry_run:
        legacy_all = [i for indices in legacy_by_source.values() for i in indices]
        if legacy_all:
            if snapshot_repo:
                try:
                    _take_snapshot(es, snapshot_repo, legacy_all)
                # Refusal boundary: any snapshot failure (timeout, name clash,
                # PARTIAL state) must refuse, never escape the gate.
                except Exception as e:  # noqa: BLE001
                    logging.error("Refusing to apply: fresh snapshot into %s failed (%s).", snapshot_repo, e)
                    return 1
            elif _snapshot_exists(es, legacy_all):
                logging.info("Pre-snapshot %s/%s present — proceeding.", PRESNAPSHOT_REPO, PRESNAPSHOT_NAME)
            else:
                logging.error(
                    "Refusing to apply: no pre-snapshot %s/%s and no --snapshot-repo given. "
                    "Take a snapshot first — _swap permanently deletes each legacy index.",
                    PRESNAPSHOT_REPO,
                    PRESNAPSHOT_NAME,
                )
                # Return 1, not None: import.py maps None to exit 0,
                # which would record a refused destructive migration as green.
                return 1

    acquired = {}
    if not dry_run:
        for connector in sorted(connectors):
            # no progress doc here, so a global-slot wait (if the cap is
            # on) orders by a local queue ticket. One slot covers every connector
            # lock this process takes.
            lock_id = ImportLockService.acquire(connector)
            if lock_id is None:
                # Someone grabbed it between the pre-flight and now. Release
                # what we hold and abort rather than race.
                logging.error("Aborting: could not acquire lock for %s; releasing held locks.", connector)
                for c, lid in acquired.items():
                    ImportLockService.release(c, lid)
                return 1
            acquired[connector] = lock_id
        if acquired:
            logging.info("Acquired import locks: %s", ", ".join(sorted(acquired)))

    report = {"migrated": [], "skipped": [], "ambiguous": [], "errors": []}
    try:
        team_cache = build_team_lookup_cache(es)
        slug_map = build_reverse_slug_map(team_cache)

        for source in KNOWN_SOURCES:
            if source in _SKIP_SOURCES:
                continue
            # Reuse the single pre-lock sweep: the delete set is exactly the
            # set the snapshot gate covered. migrate_one re-checks is_alias
            # per index, so a stale entry degrades to a skip, never a delete.
            for old_index in legacy_by_source[source]:
                slug = old_index[: -len(f"_{source}")]
                esids = slug_map.get(slug, [])
                if len(esids) == 0:
                    logging.warning(
                        "%s: slug %r matches no team in de_asset_teams; skipping (orphan).",
                        old_index,
                        slug,
                    )
                    report["skipped"].append((old_index, "no_team_match"))
                    continue
                if len(esids) > 1:
                    logging.warning(
                        "%s: slug %r is AMBIGUOUS (teams %s); skipping to avoid mis-attribution.",
                        old_index,
                        slug,
                        esids,
                    )
                    report["ambiguous"].append((old_index, esids))
                    continue

                team_esid = esids[0]
                target_index = f"{team_esid}__{source}".lower()
                status = migrate_one(es, old_index, source, target_index, dry_run=dry_run, min_dedup_ratio=min_dedup_ratio)

                if status == "migrated" or status.startswith("DRY-RUN"):
                    report["migrated"].append((old_index, target_index, status))
                elif status.startswith("skip"):
                    report["skipped"].append((old_index, status))
                else:
                    report["errors"].append((old_index, status))
    finally:
        for connector, lock_id in acquired.items():
            ImportLockService.release(connector, lock_id)
        if acquired:
            logging.info("Released import locks: %s", ", ".join(sorted(acquired)))

    # Re-set prefix since es_helpers calls may have changed it.
    lib.logging.define("migrate-to-esid-indices")
    logging.info(
        "Migration complete%s: %d migrated, %d skipped, %d ambiguous, %d errors.",
        " (dry-run)" if dry_run else "",
        len(report["migrated"]),
        len(report["skipped"]),
        len(report["ambiguous"]),
        len(report["errors"]),
    )
    for old_index, target_index, status in report["migrated"]:
        logging.info("  migrated: %s → %s [%s]", old_index, target_index, status)
    for old_index, detail in report["skipped"]:
        logging.info("  skipped:  %s [%s]", old_index, detail)
    for old_index, esids in report["ambiguous"]:
        logging.warning("  ambiguous: %s → teams %s (NOT migrated)", old_index, esids)
    for old_index, detail in report["errors"]:
        logging.error("  error:    %s [%s]", old_index, detail)

    # Return 1, not the report, when an apply run hit errors: import.py maps
    # anything but explicit False / non-zero int to exit 0, so a
    # refused destructive migration would otherwise be recorded green.
    if report["errors"] and not dry_run:
        return 1
    return report


if __name__ == "__main__":
    # Direct execution parses no flags — it is always a report-only dry-run.
    # Apply is only reachable via import.py (see module docstring).
    main({})

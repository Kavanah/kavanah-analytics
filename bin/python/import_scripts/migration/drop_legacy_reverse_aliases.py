"""drop the legacy reverse aliases left by the ESID migration.

The migration (``migrate_to_esid_indices.py``) reindexes each legacy
``{slug}_{source}`` tenant index into a ``{team_esid}__{source}`` ESID index,
then — in the same ``_aliases`` block — deletes the old concrete index and adds
the old name back as a **reverse alias** pointing at the new ESID index. That
reverse alias keeps existing dashboard wildcards (``*_fb_insights``) and any
saved references matching post-migration (Decision B: the reverse
aliases live indefinitely until *this* ticket runs).

This script removes those reverse aliases once they are no longer needed.

⚠️ PRECONDITIONS (both MUST be true before ``--apply``):
  1. Every dashboard / data view has been audited to confirm none depend on the
     old ``{slug}_{source}`` reverse-alias NAME — they should all be on
     ``*_source`` wildcards, which match the ESID index directly (so removing
     the alias is invisible to them).
  2. The migration is fully deployed to prod and verified stable for a
     reasonable bake period.

This script enforces only the *mechanically checkable* half of precondition 2:
it refuses to drop an alias whose backing ESID index is missing (i.e. the
migration was never run on this cluster). The dashboard audit (precondition 1)
and the prod bake (precondition 2) are human gates — see the ticket.

What counts as a reverse alias to drop
======================================
A name is a reverse alias iff ALL of:
  * it is an ES alias (not a concrete index, not nonexistent);
  * it ends in ``_{source}`` for some ``source`` in ``KNOWN_SOURCES`` and the
    part before the suffix is a non-empty slug that is NOT itself ESID-form
    (i.e. the name is ``{slug}_{source}``, not ``{esid}__{source}``);
  * its backing concrete index IS ESID-form ``{team_esid}__{source}`` for the
    SAME source.

The third clause is what separates a reverse alias from every other
alias in the cluster:
  * the DT alias ``disciple-tools`` (``convert_dt_index_to_alias.py``) has no
    ``_{source}`` suffix and its backing index is a timestamped
    ``disciple-tools-<ts>`` — both clauses 2 and 3 fail, so it is left alone;
  * a hypothetical hand-made alias pointing at a non-ESID index fails clause 3.

Idempotent
==========
A re-run finds no qualifying aliases (they were already removed, and the ESID
indices behind them are concrete, not aliases) and exits a no-op.

Concurrency
===========
Removing an alias is metadata-only and near-instant — it does not move data, so
the heavy ``ImportLockService`` pre-flight the migration needs is overkill here.
The ticket asks the watchdog be "paused if the operation touches enough indices
to matter"; we surface the count and, in ``--apply`` mode, snapshot the affected
ESID indices' alias state implicitly via the cluster snapshot before touching
anything, so a mistaken drop is recoverable.

Usage
=====
::

    # Dry-run: enumerate the reverse aliases that WOULD be dropped (default).
    python3 /app/python/import.py migration drop_legacy_reverse_aliases

    # Apply on dev/staging (snapshot first):
    python3 /app/python/import.py migration drop_legacy_reverse_aliases \\
        --apply --repo migration_repo

    # Apply on prod:
    python3 /app/python/import.py migration drop_legacy_reverse_aliases \\
        --apply --repo daily_repo
"""

from __future__ import annotations

import logging

import lib.logging
from migration.snapshot_gate import (  # the shared gate replaces the per-script copy
    finalize_report,
    MigrationRefused,
    resolve_apply_mode,
    take_snapshot,
)
from lib.es_helpers import (
    KNOWN_SOURCES,
    get_alias_backing_index,
    get_elasticsearch_client,
    is_alias,
)


class AliasCleanupError(Exception):
    """Raised when the cleanup cannot proceed safely."""


def _is_esid_form(name: str, source: str) -> bool:
    """True if ``name`` is ``{team_esid}__{source}`` for this source.

    ESID-form names carry the ``__`` separator immediately before the source
    suffix; legacy slug names use a single ``_``. We test the separator rather
    than the esid charset because ESIDs are opaque ES doc ids.
    """
    suffix = f"__{source}"
    if not name.endswith(suffix):
        return False
    esid = name[: -len(suffix)]
    return bool(esid) and "__" not in esid  # non-empty esid, single separator


def _legacy_slug(name: str, source: str) -> str | None:
    """Return the ``{slug}`` of a legacy ``{slug}_{source}`` name, else None.

    Rejects ESID-form names (``{esid}__{source}`` — the slug would end in the
    second ``_`` of ``__``) and empty slugs.
    """
    suffix = f"_{source}"
    if not name.endswith(suffix):
        return None
    slug = name[: -len(suffix)]
    # ESID-form `{esid}__{source}` → slug would end in `_` (the first of `__`).
    if not slug or slug.endswith("_"):
        return None
    return slug


def find_reverse_aliases(es) -> list[tuple[str, str, str]]:
    """Return ``[(alias_name, backing_esid_index, source), ...]`` for every
     reverse alias on the cluster.

    Sorted and de-duplicated by alias name. A name qualifies only if it is an
    alias, parses as a legacy ``{slug}_{source}`` name, and its backing index is
    ESID-form for that same source (see module docstring for why all three).
    """
    found: dict[str, tuple[str, str, str]] = {}
    for source in KNOWN_SOURCES:
        suffix = f"_{source}"
        try:
            # `*{suffix}` matches both legacy aliases and ESID concrete indices;
            # expand_wildcards=all so aliases are included (open|closed concrete
            # indices alone would miss alias names).
            resp = es.indices.get(
                index=f"*{suffix}",
                expand_wildcards="all",
                allow_no_indices=True,
            )
            meta = resp.body if hasattr(resp, "body") else resp
            names = set(meta.keys())
        except Exception as e:
            logging.warning("listing names for *%s failed: %s", suffix, e)
            names = set()
        # es.indices.get only returns concrete indices, not aliases — so also
        # pull alias names explicitly for this suffix.
        try:
            alias_resp = es.indices.get_alias(index=f"*{suffix}")
            alias_meta = alias_resp.body if hasattr(alias_resp, "body") else alias_resp
            for backing, info in alias_meta.items():
                names.update(info.get("aliases", {}).keys())
        except Exception as e:
            logging.debug("get_alias(*%s) returned nothing/failed: %s", suffix, e)

        for name in names:
            slug = _legacy_slug(name, source)
            if slug is None:
                continue  # ESID-form or non-matching suffix
            if not is_alias(es, name):
                continue  # concrete index (e.g. an unmigrated legacy index) — not ours to drop
            backing = get_alias_backing_index(es, name)
            if not backing or not _is_esid_form(backing, source):
                # Alias points at a non-ESID index — NOT a reverse
                # alias (e.g. the DT alias). Leave it alone.
                logging.info(
                    "Skipping alias %r → %r: backing index is not ESID-form for %s; " "not a  reverse alias.",
                    name,
                    backing,
                    source,
                )
                continue
            found[name] = (name, backing, source)
    return [found[n] for n in sorted(found)]


def _take_snapshot(es, repo: str, indices: list[str]) -> str:
    """delegates to the shared gate: timestamped name, real
    request_timeout (the old copy ran a wait_for_completion snapshot on
    the ~10s default client timeout), state == SUCCESS check (a PARTIAL
    snapshot is not a restore point), recent-reuse on retry."""
    return take_snapshot(es, repo, sorted(set(indices)), name_prefix="pre-alias-cleanup")


def _drop_one(es, alias_name: str, backing_index: str) -> bool:
    """Remove a single reverse alias in one ``_aliases`` actions block.

    Returns True on success. The backing ESID index is untouched (it is live).
    """
    try:
        es.indices.update_aliases(body={"actions": [{"remove": {"index": backing_index, "alias": alias_name}}]})
        logging.info("✓ dropped reverse alias %s (was → %s)", alias_name, backing_index)
        return True
    except Exception as e:
        logging.error("failed to drop reverse alias %s → %s: %s", alias_name, backing_index, e)
        return False


def main(params: dict) -> dict | int:
    lib.logging.define("drop-legacy-reverse-aliases")

    # shared parse: --apply must be an explicit true value
    # (import.py delivers `--apply=false` as the truthy string "false",
    # which the old bool check read as write mode — the review
    # finding); --apply + explicit --dry-run=true refuses.
    try:
        _dry_run, _ = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1
    apply_changes = not _dry_run
    repo = params.get("repo") or "daily_repo"

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Elasticsearch client unavailable — exiting 1.")
        return 1

    aliases = find_reverse_aliases(es)
    report = {"dropped": [], "skipped_missing_target": [], "errors": []}

    if not aliases:
        logging.info("No  reverse aliases found. Nothing to do.")
        print("drop_legacy_reverse_aliases: 0 reverse aliases. Cluster is clean (no-op).")
        return report

    # Precondition guard (mechanical half of #2): an alias whose backing ESID
    # index doesn't actually exist means the migration wasn't run/finished here
    # — dropping it would orphan the old name AND there's no ESID index for
    # dashboards to fall through to. Skip + flag rather than drop.
    droppable: list[tuple[str, str, str]] = []
    for alias_name, backing, source in aliases:
        if not es.indices.exists(index=backing):
            logging.error(
                "Reverse alias %s points at %s which does NOT exist as a concrete "
                "index — migration incomplete on this cluster. NOT dropping.",
                alias_name,
                backing,
            )
            report["skipped_missing_target"].append((alias_name, backing))
            continue
        droppable.append((alias_name, backing, source))

    logging.info(
        "Found %d  reverse alias(es); %d droppable, %d skipped (missing target).",
        len(aliases),
        len(droppable),
        len(report["skipped_missing_target"]),
    )
    for alias_name, backing, source in droppable:
        logging.info("  reverse alias: %s → %s [%s]", alias_name, backing, source)

    if not apply_changes:
        print(f"drop_legacy_reverse_aliases: {len(droppable)} reverse alias(es) would be " f"dropped (dry run):")
        for alias_name, backing, _ in droppable:
            print(f"  - {alias_name} (→ {backing})")
        if report["skipped_missing_target"]:
            print(
                f"Plus {len(report['skipped_missing_target'])} alias(es) skipped — backing "
                "ESID index missing (migration incomplete here). See log."
            )
        print(
            "PRECONDITIONS before --apply: (1) dashboards audited to be on "
            "*_source wildcards, not the old names; (2)  migration deployed to "
            "prod and baked. Re-run with --apply --repo <repo> to snapshot + drop."
        )
        return report

    if not droppable:
        logging.warning("Nothing droppable (all skipped). Not snapshotting.")
        print("drop_legacy_reverse_aliases: nothing to drop (targets missing). See log.")
        return report

    # ── Apply: snapshot the affected ESID indices, then drop the aliases. ──
    affected = [backing for _, backing, _ in droppable]
    try:
        snapshot_name = _take_snapshot(es, repo, affected)
    except Exception as e:
        raise AliasCleanupError(
            f"Snapshot to {repo} failed ({e}); refusing to drop aliases without a "
            "backup. Check the --repo exists, or pick another."
        ) from e

    for alias_name, backing, _ in droppable:
        if _drop_one(es, alias_name, backing):
            report["dropped"].append((alias_name, backing))
        else:
            report["errors"].append((alias_name, backing))

    logging.info(
        "Cleanup complete: %d dropped, %d skipped (missing target), %d errors. Snapshot: %s/%s.",
        len(report["dropped"]),
        len(report["skipped_missing_target"]),
        len(report["errors"]),
        repo,
        snapshot_name,
    )
    print(
        f"drop_legacy_reverse_aliases: dropped {len(report['dropped'])} reverse alias(es). "
        f"Snapshot: {repo}/{snapshot_name}."
    )
    # Only reachable in apply mode (the dry-run path returned above).
    return finalize_report(report, dry_run=False)


if __name__ == "__main__":
    main({})

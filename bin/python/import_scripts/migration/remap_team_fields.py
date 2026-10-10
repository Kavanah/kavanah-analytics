"""rebuild per-team data indices whose ``team_esid`` / ``team_title``
drifted to ``text`` (+ ``.keyword`` subfield) under the current unified schema.

Background
==========
Every unified schema maps ``team_esid`` and ``team_title`` as bare ``keyword``
(the schema lint pins it). Prod nevertheless holds indices where both
are ``text`` + ``.keyword``: an index the importer failed to create explicitly
was auto-created by the following bulk write under ES dynamic mapping. The
bleed had three sources, all closed before this sweep:

  * ``ensure_data_index`` ignored a failed create and the importer wrote on
    (now raises);
  * the ``project_schema_<source>`` templates (priority 260) that
    type an auto-created ``*_<source>`` index correctly — except that the
    ``ig_insights_window`` template never installed because its schema file
    carried a root-level ``_comment`` ES rejects (the
    loader now strips it), which is why 22 of the 47 drifted indices are IG;
  * bare ``create_index_if_not_exists`` calls in the migration tooling that
    let a following reindex/bulk auto-create the target (gated in this PR).

Aggregations on the drifted indices fail ("Fielddata is disabled on
text fields") or, worse, a cross-team wildcard ``terms`` agg cannot pick one
field handle that works for every index (the class). ES
forbids changing a field's type in place, so each affected index is rebuilt.
a teammate's read-only prod scoping (2026-09-04): 47 indices across 26 teams —
ig_insights + _window 22, disciple_tools 5, google_ads 5, fb_page_insights 4,
chatwoot 4, youtube 3. This sweep discovers them by MAPPING, not by list, so
it is idempotent and catches any born since.

the second drift rule
================================
Two prod indices born under the same class of bug carry NO marker fields:
``hope_echo_user_stats`` and ``kavanahmedia_echo_user_stats`` are per-TENANT,
not per-team (``send_user_stats_to_elastic.py`` passed ``schema.get("mappings")``
to a schema body that IS the mappings, so both were created with an empty
explicit mapping and dynamic-mapped from the first write). They were already in
this sweep's scope and were dismissed as ``skip_no_team_fields`` — the reason
 needed a standalone audit tool to see them at all.

For the sources in ``FULL_SCHEMA_SOURCES`` drift is therefore judged against
EVERY schema field, not the two markers. The machinery below is unchanged:
same two-pass reindex, same count-equality, same snapshot-or-refuse gate. Only
the question "is this index drifted?" has a second answer.

The ``last_login_at`` rebuild is the one with a live failure mode worth naming:
``text`` -> ``date`` with format ``MMMM dd, yyyy HH:mm:ss 'UTC'`` means any
stored value ES cannot parse under that format lands in the reindex
``failures`` list. That is caught — step 2's failure check refuses, keeps the
temp and leaves live untouched — but it refuses AFTER the copy, so read the
dry-run's drift report before committing to an apply run.

What it does
============
Per drifted concrete ``*_<source>`` index (every SCHEMA_MAP source), mirroring
``remap_google_ads_geo.py`` step for step:

  1. Record ``creation_date`` and the drifted field types (the report is the
     ticket's narrative pre-check — see "Pre-check" below).
  2. Create a temp ``{index}__remap_3`` with the source's current unified
     schema (``load_schema`` output — guarantees an ES-valid body).
  3. Reindex live -> temp; refuse to continue unless ``count(temp) ==
     count(live)``.
  4. Delete live (drops its aliases), recreate it with the schema.
  5. Reindex temp -> live; refuse unless ``count(live) == old_count``.
  6. Re-add the aliases atomically, verify the marker fields are now bare
     keyword, drop temp.

Counts are verified with EQUALITY, not a floor: these are deterministic-``_id``
copies of a single index (the count-equality branch — the dedup
plausibility floor is for legacy→ESID merges, not for this). Unknown counts
refuse BEFORE the reindex (the review rule). Any failure keeps the
temp index and is recorded in ``report["errors"]`` so the apply run exits 1
.

Safety contract
======================================
  * DRY-RUN BY DEFAULT — prints what would be rebuilt, touches nothing.
  * ``--apply`` must be an explicit true value (``--apply=false`` stays dry).
  * An apply run must secure a restore point: pass ``--snapshot-repo=<repo>``
    to take (or reuse) a fresh timestamped snapshot of every index about to be
    deleted. There is deliberately NO pre-snapshot fallback — the drifted
    indices were born as late as 2026-08-28, after every named pre-snapshot.
  * An apply run refuses when a per-source mapping template for an in-scope
    source cannot be installed: rebuilding an index while its source can still
    be born drifted is churn, not a fix.

Pre-check (from the review)
===================================
If the templates have protected auto-creates landed, every
drifted index should predate that, and the sweep is one-shot. The dry-run
prints each drifted index's ``creation_date`` and, per source, whether
``project_schema_<source>`` is installed — any drifted index newer than the
template's install date means a template is missing or shadowed, which is a
second bug to file.

Operational notes
=================
  * Run OUTSIDE the import window (or with the connector's import lock held):
    a document written to the live index between step 3 and step 4 is not in
    the temp copy. Step 5's count check would catch it and keep the temp, but
    the live index would then hold the partial copy — recovery is a manual
    ``_reindex`` from the kept temp.
  * ``--source=<name>`` limits the sweep to one SCHEMA_MAP source;
    ``--indices=a,b,c`` limits it to named indices (both filters compose).
  * Temp indices ``*__remap_3`` are covered by the default
    template's pattern list so they are created as regular indices, not
    hijacked as data streams (the staging failure).

Usage
=====
    docker exec imports_container python3 import.py migration remap_team_fields
    ... migration remap_team_fields --source=ig_insights_window
    ... migration remap_team_fields --apply --snapshot-repo=daily_repo
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import lib.logging
from lib.es_helpers import (
    create_index_if_not_exists,
    get_elasticsearch_client,
)
from migration.schema_loader import SCHEMA_MAP, get_schema_for_index_type
from migration.snapshot_gate import (  # the shared gate
    MigrationRefused,
    ensure_restore_point,
    finalize_report,
    resolve_apply_mode,
)

TEMP_SUFFIX = "__remap_3"
SNAPSHOT_PREFIX = "pre-team-field-remap"

# The fields whose drift defines scope. Both are bare ``keyword`` in every
# unified schema; the schema lint keeps them that way.
MARKER_FIELDS = ("team_esid", "team_title")

# sources whose drift CANNOT be defined by the marker fields,
# because their schema has neither. ``echo_user_stats`` is per-tenant
# (``{subdomain}_echo_user_stats``), not per-team. Those indices already land
# in scope — ``echo_user_stats`` is in SCHEMA_MAP — and were then dismissed as
# ``skip_no_team_fields``, which is exactly why had to ship a separate
# audit tool to see their drift at all. For these sources EVERY schema field
# defines drift.
#
# An explicit allowlist, deliberately not "any source whose schema lacks the
# markers": the latter would silently widen a destructive sweep across every
# such source the day someone adds one. Adding a source here is a review.
FULL_SCHEMA_SOURCES = {"echo_user_stats"}

# Sources in longest-suffix-first order so ``x__ig_insights_window`` resolves
# to ``ig_insights_window`` and not to ``ig_insights``.
_SOURCES_BY_SUFFIX_LENGTH = sorted(SCHEMA_MAP, key=len, reverse=True)


def _source_of(index: str) -> str | None:
    """SCHEMA_MAP source an index name belongs to, or ``None``.

    Matches both the ESID form ``{esid}__{source}`` and the legacy slug form
    ``{slug}_{source}`` (``kavanah_team_disciple_tools`` is on the drift list).
    """
    for source in _SOURCES_BY_SUFFIX_LENGTH:
        if index.endswith(f"_{source}"):
            return source
    return None


def _concrete_indices(es, *, source_filter: str | None = None) -> dict[str, str]:
    """``{index: source}`` for every concrete data index of every SCHEMA_MAP
    source (or just ``source_filter``), excluding temp and system indices."""
    sources = [source_filter] if source_filter else list(SCHEMA_MAP)
    out: dict[str, str] = {}
    for source in sources:
        idx = es.indices.get(index=f"*_{source}", ignore_unavailable=True)
        for name in idx.keys():
            if name.startswith(".") or TEMP_SUFFIX in name:
                continue
            resolved = _source_of(name)
            if resolved != source:
                continue  # e.g. ``*_ig_insights`` never matches ``_window`` names, but be explicit
            out[name] = source
    return dict(sorted(out.items()))


def _doc_count(es, index: str) -> int | None:
    """Doc count, or ``None`` when ES cannot answer — callers refuse on None."""
    try:
        return int(es.count(index=index).get("count", 0))
    except Exception as e:  # noqa: BLE001 — boundary; the caller refuses on None
        logging.error("count(%s) failed: %s", index, e)
        return None


def _props(es, index: str) -> dict:
    m = es.indices.get_mapping(index=index)
    return list(m.values())[0]["mappings"].get("properties", {}) or {}


def _field_is_bare_keyword(props: dict, field: str) -> bool:
    spec = props.get(field)
    if not spec:
        return False
    return spec.get("type") == "keyword" and "fields" not in spec


def _drifted_fields(props: dict) -> dict[str, str]:
    """``{field: live_type}`` for every marker field PRESENT and not bare keyword.

    A marker absent from the mapping is not drift — the index has never seen
    the field (no docs yet). An index with neither marker is left alone and
    reported, not rebuilt: there is nothing to fix and rebuilding an empty
    index adds nothing the per-source template does not already do on the
    next auto-create.
    """
    out: dict[str, str] = {}
    for field in MARKER_FIELDS:
        spec = props.get(field)
        if spec and not _field_is_bare_keyword(props, field):
            shape = spec.get("type", "?")
            if "fields" in spec:
                shape += "+" + "/".join(sorted(spec["fields"]))
            out[field] = shape
    return out


def _shape_of(spec: dict) -> str:
    """Live field shape as one readable token, e.g. ``text+keyword``."""
    shape = str(spec.get("type", "?"))
    if "fields" in spec:
        shape += "+" + "/".join(sorted(spec["fields"]))
    if spec.get("format"):
        shape += f" format={spec['format']}"
    return shape


def _schema_drifted_fields(props: dict, schema_props: dict) -> dict[str, str]:
    """``{field: live_shape}`` for every SCHEMA field PRESENT and mistyped.

    The full-schema counterpart of :func:`_drifted_fields`, used for
    ``FULL_SCHEMA_SOURCES``. Same rule as the marker path: a field ABSENT from
    the live mapping is not drift (no doc has carried it yet), so a merely-BARE
    index is reported and left alone — the per-source template types it on the
    next auto-create.

    ``format`` counts only when the schema pins one: ``last_login_at`` is
    ``date`` with ``MMMM dd, yyyy HH:mm:ss 'UTC'``, and a live ``date`` that
    lost the format is drift even though the type matches.

    Deliberately NOT shared with ``tools.audit_echo_user_stats_mapping``
    although the comparison is the same. That tool is the gate this rebuild is
    verified by, and a gate importing its comparison
    from the thing it gates cannot catch a bug in that comparison.
    """
    out: dict[str, str] = {}
    for field, spec in schema_props.items():
        live = props.get(field)
        if not live:
            continue
        want_type = spec.get("type")
        # An ``object`` field ES stores as an implicit object carries no
        # ``type`` in the live mapping — the same thing, not drift.
        if want_type == "object" and live.get("type") in (None, "object"):
            continue
        if live.get("type") != want_type or ("format" in spec and live.get("format") != spec["format"]):
            out[field] = _shape_of(live)
    return out


def _drift_of(source: str, props: dict, schema_props: dict) -> dict[str, str]:
    """Drifted fields for one index, by whichever rule its source uses."""
    if source in FULL_SCHEMA_SOURCES:
        return _schema_drifted_fields(props, schema_props)
    return _drifted_fields(props)


def _skip_status(source: str, props: dict, schema_props: dict) -> str:
    """Why an undrifted index was skipped — clean, or nothing to judge it by."""
    if source in FULL_SCHEMA_SOURCES:
        return "skip_already_clean" if any(f in props for f in schema_props) else "skip_no_schema_fields"
    return "skip_already_clean" if any(f in props for f in MARKER_FIELDS) else "skip_no_team_fields"


def _is_clean(es, index: str, source: str = "", schema_props: dict | None = None) -> bool:
    """True iff the index carries no drift under its source's rule."""
    return not _drift_of(source, _props(es, index), schema_props or {})


def _creation_date(es, index: str) -> str | None:
    """``index.creation_date`` as an ISO-8601 UTC date string, or ``None``."""
    try:
        settings = es.indices.get_settings(index=index, name="index.creation_date")
        ms = int(list(settings.values())[0]["settings"]["index"]["creation_date"])
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception as e:  # noqa: BLE001 — informational; never blocks the sweep
        logging.warning("creation_date(%s) unavailable: %s", index, e)
        return None


def _aliases_of(es, index: str) -> list[str]:
    try:
        meta = es.indices.get_alias(index=index)
        return sorted((meta.get(index, {}).get("aliases", {}) or {}).keys())
    except Exception:  # noqa: BLE001 — boundary; absence = no aliases
        return []


def _reindex(es, src: str, dest: str) -> dict:
    resp = es.options(request_timeout=1800).reindex(
        body={"source": {"index": src}, "dest": {"index": dest}},
        wait_for_completion=True,
        refresh=True,
        timeout="30m",
    )
    return dict(resp) if resp is not None else {}


def _describe_es_error(exc: Exception) -> str:
    """One line naming WHY ES refused, for the per-index status string.

    ES 9 answers a ``_reindex`` whose documents were rejected with
    the reindex body as a **400**, so the client raises ``BadRequestError``
    instead of returning ``{"failures": [...]}``. The body carries the first
    rejected document's cause (``document_parsing_exception: failed to parse
    field [created_at] ...``); surface that, not a 600 KB traceback.
    """
    body = getattr(exc, "body", None)
    reason = ""
    if isinstance(body, dict):
        failures = body.get("failures") or []
        if failures and isinstance(failures[0], dict):
            cause = failures[0].get("cause") or {}
            inner = cause.get("caused_by") or {}
            reason = f"{cause.get('type', '?')}: {cause.get('reason', '')}"
            if inner.get("reason"):
                reason += f" <- {inner['reason']}"
            reason += f" (+{len(failures) - 1} more)" if len(failures) > 1 else ""
    if not reason:
        reason = str(exc)
    return f"{type(exc).__name__}: {reason}"[:300]


def _try_reindex(es, src: str, dest: str) -> tuple[dict, str | None]:
    """``_reindex`` that never raises: ``(response, None)`` or ``({}, cause)``.

    on prod (2026-09-05) an uncaught ``BadRequestError`` from the
    live→temp pass propagated out of :func:`_remap_one` and out of ``main``,
    aborting the run: the indices after it were never processed, nothing was
    recorded, ``finalize_report`` never ran. The module contract ("any failure
    keeps the temp index and is recorded in ``report['errors']``") requires
    the boundary to be HERE, per index. Live data was safe by construction —
    the contract about reporting was not.
    """
    try:
        return _reindex(es, src, dest), None
    except Exception as exc:  # noqa: BLE001 — per-index boundary; the cause is recorded in the status, never dropped
        logging.error("reindex %s -> %s raised: %s", src, dest, _describe_es_error(exc))
        return {}, _describe_es_error(exc)


def _remap_one(es, index: str, source: str, schema: dict, *, dry_run: bool) -> dict:
    """Rebuild one index under ``schema`` (two-pass, count-equality verified).

    ``status`` values: ``skip_already_clean`` / ``skip_no_team_fields`` (no
    marker present) / ``skip_no_schema_fields`` (full-schema source
    with none of its schema fields live yet), ``DRY-RUN would remap ...``,
    ``remapped``, or an ``error_*`` string. Every error path that has already
    created the temp index KEEPS it — the data is there for manual recovery.
    """
    result: dict = {
        "index": index,
        "source": source,
        "status": "",
        "old_count": None,
        "new_count": None,
        "creation_date": None,
        "drifted": {},
    }

    schema_props = schema.get("properties", {}) or {}
    props = _props(es, index)
    drifted = _drift_of(source, props, schema_props)
    if not drifted:
        result["status"] = _skip_status(source, props, schema_props)
        return result
    result["drifted"] = drifted
    result["creation_date"] = _creation_date(es, index)

    # an unknown count refuses HERE, before the dry-run
    # return and before the up-to-30-minute reindex: both verifications below
    # need it, and reindexing an unverifiable run wastes the wall-clock.
    old_count = _doc_count(es, index)
    if old_count is None:
        result["status"] = "error_count_unavailable(old=None) — nothing touched"
        return result
    aliases = _aliases_of(es, index)
    result["old_count"] = old_count
    result["aliases"] = aliases

    temp = f"{index}{TEMP_SUFFIX}"

    if dry_run:
        result["status"] = (
            f"DRY-RUN would remap {index} ({old_count} docs, created {result['creation_date']}, "
            f"drifted={drifted}, aliases={aliases}) via {temp}"
        )
        return result

    # 1) temp with the correct schema
    es.indices.delete(index=temp, ignore_unavailable=True)
    if not create_index_if_not_exists(es, temp, schema):
        result["status"] = "error_create_temp — nothing touched"
        return result

    # 2) live -> temp, verify count equality (deterministic-id copy)
    resp, cause = _try_reindex(es, index, temp)
    if cause:
        result["status"] = f"error_reindex_live_to_temp({cause}) — TEMP KEPT, LIVE UNTOUCHED"
        return result
    temp_count = _doc_count(es, temp)
    if resp.get("failures") or temp_count != old_count:
        result["status"] = (
            f"error_count_live_to_temp(old={old_count},temp={temp_count},"
            f"failures={len(resp.get('failures') or [])}) — TEMP KEPT, LIVE UNTOUCHED"
        )
        return result

    # 3) delete live (drops aliases), recreate with the correct schema
    es.indices.delete(index=index)
    if not create_index_if_not_exists(es, index, schema):
        result["status"] = "error_recreate_live — DATA IN TEMP"
        return result

    # 4) temp -> live, verify count equality
    resp, cause = _try_reindex(es, temp, index)
    if cause:
        result["new_count"] = _doc_count(es, index)
        result["status"] = f"error_reindex_temp_to_live({cause}) — TEMP KEPT (full copy), live partial"
        return result
    new_count = _doc_count(es, index)
    result["new_count"] = new_count
    if resp.get("failures") or new_count != old_count:
        result["status"] = (
            f"error_count_temp_to_live(old={old_count},new={new_count},"
            f"failures={len(resp.get('failures') or [])}) — TEMP KEPT"
        )
        return result

    # 5) restore aliases atomically
    if aliases:
        es.indices.update_aliases(body={"actions": [{"add": {"index": index, "alias": a}} for a in aliases]})

    # 6) verify the rebuilt mapping is clean under the SAME rule that put this
    # index in scope, then drop temp. Passing source/schema here is load-
    # bearing: with the marker rule a FULL_SCHEMA_SOURCES index has no marker
    # fields to judge, so the check would pass vacuously and the temp — the
    # only remaining copy of a correct rebuild — would be dropped unverified.
    if not _is_clean(es, index, source, schema_props):
        result["status"] = "error_mapping_still_drifted — TEMP KEPT"
        return result
    es.indices.delete(index=temp, ignore_unavailable=True)
    result["status"] = "remapped"
    return result


def _parse_indices_filter(params: dict) -> set[str] | None:
    raw = params.get("indices")
    if not raw or raw is True:
        return None
    return {s.strip() for s in str(raw).split(",") if s.strip()}


def _template_status(sources: list[str]) -> dict[str, bool]:
    """Install/refresh the per-source mapping templates and report per source.

    The sweep fixes the symptom; the templates are what stops the next
    auto-create from recreating it. Runs the installer (idempotent
    upsert) and returns ``{source: installed}`` for the in-scope sources.
    """
    from lib.es_index_template_service import ensure_source_mapping_templates

    results = ensure_source_mapping_templates()
    return {s: bool(results.get(s)) for s in sources}


def main(params):
    lib.logging.define("remap-team-fields")
    try:
        dry_run, snapshot_repo = resolve_apply_mode(params)
    except MigrationRefused as e:
        logging.error("Refusing: %s", e)
        return 1

    source_filter = params.get("source") if isinstance(params.get("source"), str) else None
    if source_filter and source_filter not in SCHEMA_MAP:
        logging.error("Refusing: --source=%s is not a SCHEMA_MAP source (%s)", source_filter, ", ".join(sorted(SCHEMA_MAP)))
        return 1
    indices_filter = _parse_indices_filter(params)

    es = get_elasticsearch_client()
    if es is None:
        logging.error("No ES client.")
        return 1

    # Temp names ``{index}__remap_3`` do not end in ``_<source>``; the
    # default template lists ``*__remap_3`` so they are created as
    # regular indices. The migration runs in imports_container, which does not
    # refresh that template at startup — refresh here (idempotent upsert).
    from lib.es_index_template_service import ensure_project_default_template

    if not ensure_project_default_template():
        logging.error(
            "Refusing to run:  default template install failed — temp index creates would be hijacked as data streams."
        )
        return 1

    scope = _concrete_indices(es, source_filter=source_filter)
    if indices_filter is not None:
        missing = sorted(indices_filter - set(scope))
        if missing:
            logging.warning("--indices names not found in scope (ignored): %s", ", ".join(missing))
        scope = {k: v for k, v in scope.items() if k in indices_filter}
    logging.info("%s: %d concrete data index(es) in scope", "DRY-RUN" if dry_run else "APPLY", len(scope))

    # Pre-check: which in-scope sources have a working per-source template?
    in_scope_sources = sorted(set(scope.values()))
    templates = _template_status(in_scope_sources)
    for source, ok in templates.items():
        (logging.info if ok else logging.error)(
            "template project_schema_%s: %s",
            source,
            "installed" if ok else "NOT INSTALLED — auto-creates of this source still drift",
        )

    # Classify first (read-only) so the snapshot covers exactly what is deleted.
    schemas = {s: get_schema_for_index_type(s) for s in in_scope_sources}
    to_rebuild: list[tuple[str, str]] = []
    report = {"remapped": [], "skipped": [], "errors": [], "templates": templates}
    for index, source in scope.items():
        schema_props = schemas[source].get("properties", {}) or {}
        props = _props(es, index)
        if not _drift_of(source, props, schema_props):
            report["skipped"].append({"index": index, "source": source, "status": _skip_status(source, props, schema_props)})
            continue
        to_rebuild.append((index, source))
    logging.info("%d drifted index(es) to rebuild, %d clean", len(to_rebuild), len(report["skipped"]))

    if not dry_run and to_rebuild:
        bad_sources = sorted(s for s in {src for _, src in to_rebuild} if not templates.get(s))
        if bad_sources:
            logging.error(
                "Refusing --apply: per-source template missing for %s — rebuilt indices of those sources "
                "would be born drifted again on the next auto-create. Fix the template first.",
                ", ".join(bad_sources),
            )
            return 1
        # snapshot-or-refuse: timestamped, state-checked, covering
        # every index this run deletes. No pre-snapshot fallback (see module doc).
        try:
            restore_point = ensure_restore_point(
                es,
                [i for i, _ in to_rebuild],
                snapshot_repo=snapshot_repo,
                name_prefix=SNAPSHOT_PREFIX,
            )
        except MigrationRefused as e:
            logging.error("Refusing --apply: %s", e)
            return 1
        logging.info("Restore point: %s", restore_point)

    for index, source in to_rebuild:
        try:
            r = _remap_one(es, index, source, schemas[source], dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — last-resort boundary: one index never aborts the sweep
            r = {
                "index": index,
                "source": source,
                "status": f"error_unexpected({_describe_es_error(exc)}) — check for a kept temp index",
                "old_count": None,
                "new_count": None,
                "creation_date": None,
                "drifted": {},
            }
        status = r["status"]
        if status.startswith("DRY-RUN") or status == "remapped":
            report["remapped"].append(r)
            logging.info("  %s", status if status.startswith("DRY-RUN") else f"remapped {index} ({r['new_count']} docs)")
        elif status.startswith("skip"):
            report["skipped"].append(r)
            logging.info("  skip %s [%s]", index, status)
        else:
            report["errors"].append(r)
            logging.error("  ERROR %s [%s]", index, status)

    # Narrative pre-check summary: the newest drifted creation_date per source.
    newest: dict[str, str] = {}
    for r in report["remapped"] + report["errors"]:
        cd = r.get("creation_date")
        if cd and cd > newest.get(r["source"], ""):
            newest[r["source"]] = cd
    for source in sorted(newest):
        logging.info(
            "newest drifted %s index created %s (template installed: %s)", source, newest[source], templates.get(source)
        )
    report["newest_drifted_creation_date"] = newest

    logging.info(
        "team-field remap  %s: %d remapped, %d skipped, %d errors.",
        "(dry-run)" if dry_run else "",
        len(report["remapped"]),
        len(report["skipped"]),
        len(report["errors"]),
    )
    return finalize_report(report, dry_run=dry_run)


if __name__ == "__main__":
    main({})

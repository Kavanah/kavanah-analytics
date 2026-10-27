"""
ES Index Template Service — project-wide default template
that prevents the data-stream-hijack bug.

## Background

The ES 9.x cluster ships with a default template called
``default_replicas_0_for_single_node`` that has ``data_stream: {}`` in
its body. That template matches ``*`` and turns every new index into a
data stream. Data streams reject writes with explicit ``_id`` values
(they require ``op_type=create`` and ES-generated ids), which breaks
this project's bulk-upsert pattern.

earlier the project worked around this with per-pattern override
templates (``*fb_page_insights``, ``*fb_insights_weekly``,
``*ig_insights``, ``*_google_ads_*``, ``*_youtube_*``, etc.) — one
script per pattern, manually wired into each importer's startup. Every
new feature that wrote to a new index pattern paid the same toll.

 replaces all of that with a single project-wide override:
``index_patterns: ["*"]``, ``priority: 250``, no ``data_stream`` block.
The wide override outranks ES's default ``data_stream: {}`` template
for any index this project creates, so first-time writes land in a
regular index without per-pattern intervention.

Anything that legitimately needs a data stream can opt in with a
higher-priority template (rare — this codebase doesn't use data
streams as a general pattern).

## Self-healing

``ensure_project_default_template()`` is called by ``app.py`` at
startup so a fresh deploy (or a template that gets manually deleted /
overwritten) is restored without operator action. Idempotent — calling
twice is a no-op at the ES side because ``put_index_template`` is
upsert.

A standalone ``deployment/install-default-template.sh`` script invokes
the same function for one-time provisioning against existing
environments where ``app.py`` hasn't restarted yet.

## What this does NOT do

- **Does not delete existing data streams.** An index that was created
  before the override was installed is still a data stream and must
  be deleted explicitly (one-time cleanup per environment). The
  override only governs FUTURE index creation.
- **Does not touch the underlying default template.** km-infra
  (a teammate) owns the ES platform config; the wide override sidesteps
  the coordination question by outranking the default rather than
  replacing it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Template name. Stable so a re-run upserts rather than creating
# duplicates. Prefix ``project_`` to distinguish from ES-built-in
# templates and from per-pattern overrides that may still exist in a
# cluster until cleanup.
TEMPLATE_NAME = "project_default_no_data_stream"

# Project-prefix patterns. Narrow enough that ES doesn't refuse the
# install (the wider ``*`` pattern was rejected because it would
# orphan unrelated data streams owned by Kibana/Fleet/Firebase/DT),
# but broad enough to cover every index pattern this project writes
# to today and going forward.
#
# Categories:
#  - ``de_*``       — every doc-id-style index this codebase creates
#                     (de_connection, de_audit_log, de_import_progress,
#                     de_asset_*, de_authz_*, de_wizard_session,
#                     de_dashboard_checklist_*, etc.).
#  - ``*_fb_insights``, ``*_fb_insights_window``, ``*_fb_page_insights``,
#    ``*_fb_page_posts``, ``*_ig_insights``, ``*_ga_metrics``, ``*_echo_conversations``,
#    ``*_echo_user_stats``, ``*_chatwoot_insights``,
#    ``*_chatwoot_conversations``, ``*_google_ads``,
#    ``*_youtube_analytics``
#                  — per-team prefix patterns: the importer writes to
#                    e.g. ``kavanah_team_fb_insights``, ``controlteam_fb_insights``.
#  - ``*chatwoot_conversations``, ``*chatwoot_insights`` (no underscore
#    prefix) — historical names from pre-tenant-prefix era still in
#    the codebase's reverse-lookup tables.
#
# What this deliberately does NOT match:
#  - Kibana / Fleet / ML / APM system indices (``.kibana_*``,
#    ``.fleet-*``, ``.ml-*``, ``logs-*``, ``metrics-*``).
#  - Disciple Tools (``dt_data_new``, ``disciple-tools-*``).
#  - Firebase / Google Analytics export streams
#    (``firebase-events-*``).
#  - Elastic security (``.items-default``, ``.lists-default``,
#    ``.edr-workflow-insights-*``).
# Those are owned by other apps; their data-stream behaviour stays
# untouched.
PROJECT_INDEX_PATTERNS: list[str] = [
    "de_*",
    "*_fb_insights",
    "*_fb_insights_window",
    # per-country delivery breakdown ({esid}__fb_insights_country).
    "*_fb_insights_country",
    # windowed deduplicated IG reach ({esid}__ig_insights_window).
    "*_ig_insights_window",
    "*_fb_page_insights",
    "*_fb_page_posts",
    "*_ig_insights",
    "*_ga_metrics",
    # Trailing-``*`` variant so transient remap/rebuild indices like
    # ``{esid}__ga_metrics__remap`` are ALSO covered — they don't end in
    # ``_ga_metrics`` so the bare ``*_ga_metrics`` pattern above misses them, and
    # they fall through to the priority-5 ``data_stream`` hijacker (this is what
    # blocked the GA remap on the single-node staging cluster). Kept
    # ALONGSIDE the exact ``*_ga_metrics`` so the schema-source guard
    # test (which asserts the literal ``*_<source>`` string) still passes.
    "*_ga_metrics*",
    # ga_metrics_window is now its own SCHEMA_MAP source, and the
    # guard asserts the LITERAL "*_<source>" string per source. The
    # "*_ga_metrics*" glob above already matches *_ga_metrics_window indices, but
    # the guard checks the exact string, so list it explicitly too.
    "*_ga_metrics_window",
    "*_echo_conversations",
    "*_echo_user_stats",
    "*_chatwoot_insights",
    "*_chatwoot_conversations",
    "*_google_ads",
    # Trailing-``*`` variant so transient remap/rebuild indices like
    # ``{esid}__google_ads__remap`` are ALSO covered — they don't end in
    # ``_google_ads`` so the bare ``*_google_ads`` pattern above misses them, and
    # they fall through to the priority-5 ``data_stream`` hijacker (this blocked
    # the google_ads geo remap on the single-node staging cluster, the
    # same failure mode hit for ga_metrics). Kept ALONGSIDE the exact
    # ``*_google_ads`` so the schema-source guard test still passes.
    "*_google_ads*",
    "*_youtube_analytics",
    # transient rebuild indices ``{index}__remap_3`` of the
    # team-field drift sweep (every SCHEMA_MAP source). One pattern covers them
    # all, so they are created as regular indices instead of falling through to
    # the priority-5 data-stream hijacker (the failure mode).
    "*__remap_3",
    # Bitly link-clicks importer ({team_esid}__bitly_clicks); the
    # guard requires the literal "*_<source>" per SCHEMA_MAP source.
    "*_bitly_clicks",
    # TikTok organic snapshots ({team_esid}__tiktok_organic).
    "*_tiktok_organic",
    # monthly conversation rollups ({team_esid}__conversation_rollups).
    "*_conversation_rollups",
    # Disciple.Tools pull importer added a ``disciple_tools`` schema
    # source; without this pattern its index falls through to the priority-5
    # ``*`` data-stream hijacker and bulk writes fail (the guard).
    "*_disciple_tools",
]

# Priority. Narrow patterns mean we only need to outrank the
# priority-5 hijacker (``default_replicas_0_for_single_node``, pattern
# ``*``). Priority 100 collides with 6 ES system templates installed
# by default (.kibana-reporting, agentless, behavioral_analytics-events,
# logs, metrics, synthetics) — even though none of their patterns
# overlap with ours, ES rejects the install with "multiple index
# templates may not match during index creation, please use a different
# priority" because composable templates are validated globally by
# priority + pattern overlap. 99 is the natural choice — empty band
# (cluster audit 2026-05-21 found priorities 6–99 completely vacant),
# one below the dense priority-100 ES system band.
PRIORITY = 99


def build_template_body() -> dict[str, Any]:
    """Return the template body as a dict.

    Extracted so the release-plan test can pin the exact shape without
    a live ES instance. The shape is the load-bearing fact —
    specifically the *absence* of a ``data_stream`` block (which is
    what prevents the hijack) plus the project-prefix patterns +
    priority that let it outrank the default for our indices only.
    """
    return {
        "index_patterns": PROJECT_INDEX_PATTERNS,
        "priority": PRIORITY,
        "template": {
            "settings": {
                # Single-node cluster — replicas would be unallocated.
                # Matches the default ES template's only useful setting,
                # which is why we preserve it here.
                "number_of_replicas": 0,
            },
        },
        # NOTE: deliberately NO ``data_stream`` block. Its absence is
        # the entire point of this template — without it, ES creates
        # regular indices for any pattern this project writes to.
        "_meta": {
            "managed_by": "es_index_template_service",
            "ticket": "",
            "description": (
                "Project-wide default — prevents the ES 9.x default "
                "template from hijacking new indices as data streams. "
                "See lib/es_index_template_service.py."
            ),
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# per-source MAPPING templates.
#
# The template above stops the data-stream hijack but carries NO
# ``mappings``. So when a doc write reaches ES *before*
# ``create_index_if_not_exists`` runs (the race), ES auto-creates the index as
# a regular index (good, thanks to) but with DYNAMIC mapping — every
# string field becomes ``text``, every metric ``text``. Dashboard ``terms``/
# metric aggs on the bare field then throw ``Fielddata is disabled … Text
# fields are not optimised for aggregations`` (HTTP 400) → empty panels, while
# imports look fine (writing ``text`` works). Several teams lost this race
# on prod.
#
# Fix: install one composable template per SCHEMA_MAP source carrying that
# source's unified-schema ``mappings``, at a priority that OUT-RANKS the legacy
# ``*_<source>_no_datastream`` templates (priority 250) and the template
# (99). Then an auto-created ``{esid}__{source}`` index is born with the correct
# field types — the race is closed at the ES layer, self-healing, idempotent.
#
# Mappings are sourced from ``schema_loader.load_schema`` — the SAME files
# ``create_index_if_not_exists`` applies — so the template mapping and the
# explicit-create mapping can never disagree. ``dynamic: true`` object
# sub-fields in the schema (adset_targeting, adset_promoted_object,
# insight_targeting) are preserved verbatim; we do NOT blanket ``dynamic:
# strict`` (that would reject the legitimately-dynamic ``insight_actions_*``
# children the importer relies on).

# Priority for per-source mapping templates. Must beat:
# 99 (project_default_no_data_stream, *_<source> patterns)
#   - 250 (legacy <source>_no_datastream, exact *_<source> patterns, NO mappings)
#   - 251 (fb_insights_weekly_indices_override, *fb_insights_weekly)
# and stay below the 500 band (exact-name de_*/template_* — no *_<source>
# overlap). 260 sits in an empty band (staging audit 2026-06-17) and is the
# unambiguous winner for every *_<source> auto-create. Different-priority
# pattern overlap is allowed by ES (precedence is by priority); only
# SAME-priority overlap is rejected — so at 99 is left untouched.
SOURCE_MAPPING_PRIORITY = 260


def build_source_mapping_template_body(source: str) -> dict[str, Any]:
    """Return the composable-template body for one SCHEMA_MAP ``source``.

    Carries the source's unified-schema ``mappings`` so an auto-created
    ``*_<source>`` index is born correctly typed. No ``data_stream`` block
    (so it also keeps the index a regular index, like). Pattern is the
    exact ``*_<source>`` — matches both ``{esid}__{source}`` and ``{slug}_{source}``
    index names.

    Raises whatever ``load_schema`` raises if the schema file is missing/invalid
    — a source in SCHEMA_MAP with no loadable schema is a bug, surfaced loudly.
    """
    from migration.schema_loader import get_schema_for_index_type

    schema = get_schema_for_index_type(source)  # {"properties": {...}}
    return {
        "index_patterns": [f"*_{source}"],
        "priority": SOURCE_MAPPING_PRIORITY,
        "template": {
            "settings": {
                "number_of_replicas": 0,
            },
            "mappings": schema,
        },
        # NOTE: deliberately NO ``data_stream`` block — keeps auto-created
        # indices regular, same as the template.
        "_meta": {
            "managed_by": "es_index_template_service",
            "ticket": "",
            "source": source,
            "description": (
                "Per-source mapping template — births auto-created "
                f"*_{source} indices with correct field types, closing the "
                "dynamic-mapping race. See lib/es_index_template_service.py."
            ),
        },
    }


def source_mapping_template_name(source: str) -> str:
    """Stable template name for a source (upsert, not duplicate, on re-run)."""
    return f"project_schema_{source}"


def ensure_source_mapping_templates() -> dict[str, bool]:
    """Install/refresh a mapping-carrying template for every SCHEMA_MAP source.

    Returns a ``{source: ok}`` dict. Never raises — like the installer,
    a failure here means "future auto-creates of that source may be dynamically
    mapped," not "the app is broken," so startup must continue. Each source is
    independent: one bad schema doesn't block the others.

    Idempotent (``put_index_template`` is upsert). Called by app.py startup
    alongside ``ensure_project_default_template`` and by the install script.
    """
    try:
        from lib.es_helpers import get_elasticsearch_client
        from migration.schema_loader import SCHEMA_MAP
    except Exception as e:  # noqa: BLE001 — import boundary
        logger.warning("import failed: %s", e)
        return {}

    es = get_elasticsearch_client()
    if es is None:
        logger.warning("ES unreachable; skipping source-mapping-template install")
        return {}

    results: dict[str, bool] = {}
    for source in SCHEMA_MAP:
        name = source_mapping_template_name(source)
        try:
            body = build_source_mapping_template_body(source)
            es.indices.put_index_template(name=name, body=body)
            results[source] = True
        except Exception as e:  # noqa: BLE001 — per-source boundary
            logger.error("put_index_template(%s) failed: %s", name, e)
            results[source] = False

    ok = sum(1 for v in results.values() if v)
    failed = sorted(source for source, installed in results.items() if not installed)
    if failed:
        # a partial install is a real problem, not a success with a
        # footnote: every auto-created index of a failed source is born under
        # dynamic mapping (the drift). "15/16 ensured" at INFO read as
        # green for ten weeks on staging and prod while ig_insights_window's
        # template never installed. Name the sources, at ERROR.
        logger.error(
            "ensured %d/%d per-source mapping templates at priority %d — "
            "install FAILED for %s; auto-created indices of those sources will be born "
            "dynamically mapped until this is fixed",
            ok,
            len(results),
            SOURCE_MAPPING_PRIORITY,
            ", ".join(failed),
        )
    else:
        logger.info(
            "ensured %d/%d per-source mapping templates at priority %d",
            ok,
            len(results),
            SOURCE_MAPPING_PRIORITY,
        )
    return results


def ensure_project_default_template() -> bool:
    """Install (or refresh) the project-wide default index template.

    Returns True on success, False on any ES failure. Failures are
    logged but never raised — the caller (app.py startup) should
    continue booting even if template installation fails, because the
    failure mode is "future index creates may be hijacked," not "the
    app is broken right now."

    Idempotent: calling repeatedly is a no-op at the ES layer because
    ``put_index_template`` is upsert.
    """
    try:
        from lib.es_helpers import get_elasticsearch_client
    except Exception as e:  # noqa: BLE001 — import boundary
        logger.warning("es_helpers import failed: %s", e)
        return False

    es = get_elasticsearch_client()
    if es is None:
        logger.warning("ES unreachable; skipping default-template install")
        return False

    body = build_template_body()
    try:
        es.indices.put_index_template(name=TEMPLATE_NAME, body=body)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logger.warning("put_index_template(%s) failed: %s", TEMPLATE_NAME, e)
        return False

    logger.info(
        "ensured project default index template %r at priority %d",
        TEMPLATE_NAME,
        PRIORITY,
    )
    return True

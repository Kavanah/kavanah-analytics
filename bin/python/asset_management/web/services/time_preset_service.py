"""
per-team time-range preset catalog + viewer union resolution.

The dashboards page (``dashboards.html``) shows a single, page-level time-range
dropdown. Which presets it offers is configurable per team on the team asset
record (``de_asset_teams.team_time_presets`` — a keyword list of the canonical
VALUES below). A team with no configuration falls back to the full catalog.

Dashboards are shared across many teams, so a team has no distinct set of
dashboards to scope by. The per-team control that DOES exist on the dashboards
page is the Team filter (``#team-filter``) — the viewer picks which team to
view. The dropdown follows that: the client rebuilds it from the selected
team's config via ``presets_by_team_title`` (team title → its configured
values, keyed by the filter's option values). Selecting a configured team shows
its presets; "All Teams" or an unconfigured team shows the full default catalog.

Two of the catalog entries the ticket adds — This/Last Month and This/Last
Quarter — cannot be expressed as the single ``now-Nd/d`` tokens the server's
``_resolve_date_math`` understands (it approximates months as 30 days and has no
calendar anchoring). Their VALUES are therefore sentinels (``cal:*``); the
client (dashboards.html) converts each sentinel into an explicit ISO from/to
window and takes the render path, exactly as the existing ``custom`` preset
already does. The server date pipeline is untouched.

``custom`` is intentionally NOT in this catalog: Custom range is always offered
and is never team-configurable.
"""

from __future__ import annotations

import logging

# Canonical order + display labels for the CONFIGURABLE presets. The dropdown
# renders these in this order (with "Custom range…" always appended by the
# template). ``value`` is what gets stored in ``team_time_presets`` and, for the
# non-``cal:`` entries, what the server date pipeline already understands.
#
# Keep this list in sync with the client-side sentinel handling in
# dashboards.html and the ``_KNOWN_PRESETS`` allowlist there.
TIME_PRESETS: list[dict[str, str]] = [
    {"value": "", "label": "All time"},
    {"value": "now-7d/d", "label": "Last 7 days"},
    {"value": "now-30d/d", "label": "Last 30 days"},
    {"value": "now-90d/d", "label": "Last 90 days"},
    {"value": "now-6M/d", "label": "Last 6 months"},
    {"value": "now-1y/d", "label": "Last 12 months"},
    # new calendar presets — client-computed from/to (see module docstring).
    {"value": "cal:this_month", "label": "This Month"},
    {"value": "cal:last_month", "label": "Last Month"},
    {"value": "cal:this_quarter", "label": "This Quarter"},
    {"value": "cal:last_quarter", "label": "Last Quarter"},
]

# Fast membership + ordering lookups derived from the catalog.
_PRESET_ORDER = {p["value"]: i for i, p in enumerate(TIME_PRESETS)}
_VALID_VALUES = frozenset(_PRESET_ORDER)

# The team-doc field that stores a team's configured preset VALUES.
TEAM_FIELD = "team_time_presets"


def form_options() -> list[dict[str, str]]:
    """Static ``options`` list for the ``checkbox_group`` team-form field.

    "All time" is intentionally omitted from the editable checkboxes — it is the
    implicit widest window and is always offered to viewers; making it a
    toggle would let an admin hide "All time" from a team, which the product
    treats as always-available. Every other catalog entry is editable.
    """
    return [dict(p) for p in TIME_PRESETS if p["value"] != ""]


def resolve_time_presets(team_configs: list) -> list[dict[str, str]] | None:
    """Return the ordered preset list a viewer's dropdown should offer.

    ``team_configs`` is an iterable of the ``team_time_presets`` values read off
    each team doc the viewer can see (each element is a list of preset VALUES,
    or ``None``/absent/empty when a team has no configuration).

    Rules:
      * If NO team supplies a non-empty configuration, return ``None`` — the
        caller renders the full default catalog (the fallback the ticket
        specifies).
      * Otherwise return the UNION of every configured team's presets, filtered
        to known catalog values, in canonical catalog order. "All time" is
        always included (it is not team-configurable — see :func:`form_options`).

    This is a pure function so the release-plan test can exercise it directly.
    """
    union: set[str] = set()
    any_config = False
    for cfg in team_configs or []:
        if not cfg:
            continue
        if isinstance(cfg, str):
            cfg = [cfg]
        for value in cfg:
            if value in _VALID_VALUES:
                union.add(value)
                any_config = True

    if not any_config:
        return None

    # "All time" is always offered regardless of a team's ticked set.
    union.add("")

    return [{"value": p["value"], "label": p["label"]} for p in TIME_PRESETS if p["value"] in union]


def default_presets() -> list[dict[str, str]]:
    """The full default dropdown list (every catalog entry, canonical order).

    Rendered when the selected team has no custom configuration (or "All Teams"
    is selected) — the fallback the ticket specifies.
    """
    return [{"value": p["value"], "label": p["label"]} for p in TIME_PRESETS]


def all_configured_team_titles() -> list:
    """Titles of every team that has a non-empty ``team_time_presets``.

    Used for an unrestricted viewer (superuser) whose Team filter can list any
    team — we can't enumerate their scope up front, so we surface every
    configured team and let :func:`presets_by_team_title` build the map. Returns
    ``[]`` on any ES error (client then renders the default list everywhere).
    """
    try:
        from lib.es_helpers import get_all_documents

        hits = get_all_documents(
            "de_asset_teams",
            query={"exists": {"field": TEAM_FIELD}},
        )
        titles = []
        for hit in hits or []:
            src = hit.get("_source") or {}
            title = src.get("team_title")
            values = src.get(TEAM_FIELD)
            if isinstance(values, str):
                values = [values]
            if title and any(v in _VALID_VALUES for v in (values or [])):
                titles.append(title)
        return titles
    except Exception as exc:  # ES outage / bootstrap race — safe default.
        logging.warning("configured-team lookup failed: %s", exc)
        return []


def presets_by_team_title(team_titles) -> dict:
    """Map each configured team TITLE → its stored preset VALUES.

    Keyed by team_title because that is the value the dashboards page's Team
    filter dropdown carries (``#team-filter`` options are the team titles). The
    client rebuilds the time-range dropdown from the team currently selected in
    that filter — so a team's presets follow "which team am I viewing", which is
    the only per-team selection the shared-dashboard model actually has.

    Returns ``{title: [values, ...]}`` for ONLY teams that set a non-empty,
    valid ``team_time_presets``; a team with no config is absent (client falls
    back to the default list). Unknown tokens are dropped.

    Fail-soft: any ES error returns ``{}`` → the client renders the default
    list everywhere, never a stripped-down dropdown.
    """
    titles = [t for t in (team_titles or []) if t]
    if not titles:
        return {}

    try:
        # Local import keeps the pure functions above importable in tooling /
        # test contexts without the ES client on the path.
        from lib.es_helpers import (
            _sortable_title_field,
            get_all_documents,
            get_elasticsearch_client,
        )

        # Pick the EXACT-match field for team_title via a cached mapping probe:
        # prod's title columns are text+keyword so an exact
        # ``terms`` must hit ``team_title.keyword``; staging's is pure keyword so
        # the bare field is exact. A hardcoded ``.keyword`` guess silently
        # 0-matches on a pure-keyword env (no error) — the trap.
        es = get_elasticsearch_client()
        match_field = _sortable_title_field(es, "de_asset_teams", "team_title")
        query = {"terms": {match_field: titles}}
        hits = get_all_documents("de_asset_teams", query=query)

        by_title = {}
        for hit in hits or []:
            src = hit.get("_source") or {}
            title = src.get("team_title")
            values = src.get(TEAM_FIELD)
            if isinstance(values, str):
                values = [values]
            kept = [v for v in (values or []) if v in _VALID_VALUES]
            if title and kept:
                by_title[title] = kept
        return by_title
    except Exception as exc:  # ES outage / bootstrap race — safe default.
        logging.warning("preset map lookup failed, using default list: %s", exc)
        return {}

"""seed an editable canvas page from an existing report dashboard.

Pure conversion logic (no Flask): take a ``de_asset_echarts_dashboards``
``_source`` dict and produce a ``canvas_config`` whose
objects carry LIVE data bindings — data_view + field + aggregation — never
baked-in values. The routes in ``routes.py`` (``/api/canvas-seed/<id>``,
``/api/canvas-seed-sources``, ``/api/canvas-icons``) are thin wrappers around
this module so the conversion is unit-testable without the web stack.

Relationship model per the private docs (2026-07-09):
COPY-on-seed — the converted config is returned unsaved; persisting it writes
a new ``dashboard_type: "canvas"`` doc stamped ``seeded_from_dashboard_id``
(provenance only). Sections are tag-groups: objects sharing a ``section_id``,
headed by a text object with ``role: "section_header"``.

Supported seed sources:
  - ``canva``          — the styled Summary Report card layout (the
    user-facing "Summary Report"; see renderCanvaChart in charts.js)
  - ``grouped_table``  — the plain summary table: groups → columns, each
    column bound to a data_view + field + aggregation
    (see create_summary_report.py)
  - ``report``         — Facebook Digital Impact Report
  - ``google_report``  — Google Digital Impact Report

Anything else (``combined_report`` included) degrades to a visible placeholder
object plus a ``skipped`` entry and a log line — never a silent drop.
"""

from __future__ import annotations

import copy

import logging
from pathlib import Path

from . import canvas_theme

logger = logging.getLogger(__name__)

# theme-representable colors resolve through the shared
# token source (canvas_theme.py) instead of carrying literals. The report
# seed converters emit token KEYS (resolved per-theme client-side against
# page.theme); only values with no key form — chart series, funnel bars —
# are resolved to concrete colors at seed time from the chosen theme.
# _SERIES stays the CLASSIC palette — the AI path's no-theme
# fallback (a themed build uses the per-builder palette, exactly
# like seeding).
_SERIES = canvas_theme.series_colors()

# Chart types this converter can turn into live canvas objects.
# "canva" is the styled Summary Report card layout (the user-facing
# "Summary Report"); "grouped_table" is the plain summary table;
# "table" is the aggregation table, copied verbatim into a
# first-class canvas table object.
SEEDABLE_CHART_TYPES = frozenset({"canva", "grouped_table", "table", "report", "google_report"})

# ── Icon library (the existing SVG set — the only icon store, per ticket) ──
ICON_LIBRARY_DIR = Path(__file__).resolve().parents[2] / "static" / "dashboard_builder" / "icons"


def list_icon_library() -> list[str]:
    """Sorted SVG filenames available to icon objects (``icon.name``)."""
    try:
        return sorted(p.name for p in ICON_LIBRARY_DIR.glob("*.svg") if p.is_file())
    except OSError:
        return []


def icon_library_has(name: str) -> bool:
    """True iff *name* is a plain filename of an existing library SVG.

    Rejects path separators / traversal outright — ``icon.name`` is joined
    into a static URL client-side, so only bare basenames are ever valid.
    """
    if not isinstance(name, str) or not name:
        return False
    if name != Path(name).name or name.startswith("."):
        return False
    return (ICON_LIBRARY_DIR / name).is_file()


# ── Impact Report platform defaults ──
# Keep in sync with _RPT_PLATFORM in static/dashboard_builder/charts.js (same
# convention as _IMPACT_REPORT_DATA_VIEWS in routes.py). Only the DATA shape
# is mirrored (labels + bindings + section wording); the visual template
# stays client-side.
_RPT_SEED_PLATFORM: dict[str, dict] = {
    "report": {
        "title": "Facebook Ads Impact Report",
        "subtitle": "How your faithful giving is reaching people through social media",
        "icon": "facebook.svg",
        "kpi_section": ("Reach & Stewardship", "How far your dollars go"),
        "kpis": [
            {"id": "kpi-reach", "label": "People Reached", "dv": "*fb_insights", "field": "insight_reach", "agg": "sum"},
            {
                "id": "kpi-spend",
                "label": "Total Investment",
                "dv": "*fb_insights",
                "field": "insight_spend",
                "agg": "sum",
                "currency": True,
            },
            {"id": "kpi-cpc", "label": "Cost Per Conversation", "computed": True},
            {
                "id": "kpi-conversations",
                "label": "Unique Conversations",
                "dv": "*echo_conversations",
                "field": "client_id",
                "agg": "cardinality",
            },
        ],
        "computed_kpi": {"id": "kpi-cpc", "numerator": "kpi-spend", "denominator": "kpi-conversations"},
        "funnel_section": ("The Engagement Journey", "From first impression to real conversation"),
        "funnel": [
            {
                "id": "funnel-impressions",
                "label": "Impressions",
                "dv": "*fb_insights",
                "field": "insight_impressions",
                "agg": "sum",
            },
            {"id": "funnel-reach", "label": "People Reached", "dv": "*fb_insights", "field": "insight_reach", "agg": "sum"},
            {
                "id": "funnel-clicks",
                "label": "Link Clicks",
                "dv": "*fb_insights",
                "field": "insight_inline_link_clicks",
                "agg": "sum",
            },
            {
                "id": "funnel-conversations",
                "label": "Unique Conversations",
                "dv": "*echo_conversations",
                "field": "client_id",
                "agg": "cardinality",
            },
        ],
        "geo_section": ("Geographic Reach", "Where your message is landing"),
        "trends_section": ("Monthly Trends", "Growth and cost efficiency over time"),
        "trend_ids": ["trend-reach", "trend-clicks", "trend-conversations"],
        # geo/trend chart defaults (mirror the `geo` / `trends`
        # entries in charts.js _RPT_PLATFORM, same convention as the rest of
        # this table).
        "geo": {"dv": "*fb_insights", "field": "insight_reach", "agg": "sum", "x_field": "team_country"},
        "trends": [
            {
                "id": "trend-reach",
                "label": "People Reached",
                "series_index": 0,
                "dv": "*fb_insights",
                "field": "insight_reach",
                "agg": "sum",
            },
            {
                "id": "trend-clicks",
                "label": "Link Clicks",
                "series_index": 1,
                "dv": "*fb_insights",
                "field": "insight_inline_link_clicks",
                "agg": "sum",
            },
            {
                "id": "trend-conversations",
                "label": "Unique Conversations",
                "series_index": 2,
                "dv": "*echo_conversations",
                "field": "client_id",
                "agg": "cardinality",
            },
        ],
    },
    "google_report": {
        "title": "Google Ads Impact Report",
        "subtitle": "How your faithful giving is reaching people through Google advertising",
        "icon": "google-ads.svg",
        "kpi_section": ("Reach & Performance", "How far your dollars go"),
        "kpis": [
            {"id": "kpi-reach", "label": "Impressions", "dv": "*google_ads", "field": "impressions", "agg": "sum"},
            {
                "id": "kpi-spend",
                "label": "Total Investment",
                "dv": "*google_ads",
                "field": "cost",
                "agg": "sum",
                "currency": True,
            },
            {"id": "kpi-cpc", "label": "Cost Per Install", "computed": True},
            {"id": "kpi-conversations", "label": "App Installs", "dv": "*google_ads", "field": "app_installs", "agg": "sum"},
        ],
        "computed_kpi": {"id": "kpi-cpc", "numerator": "kpi-spend", "denominator": "kpi-conversations"},
        "funnel_section": ("The Engagement Journey", "From first impression to real conversation"),
        "funnel": [
            {"id": "funnel-impressions", "label": "Impressions", "dv": "*google_ads", "field": "impressions", "agg": "sum"},
            {"id": "funnel-clicks", "label": "Clicks", "dv": "*google_ads", "field": "clicks", "agg": "sum"},
            {"id": "funnel-conversions", "label": "Conversions", "dv": "*google_ads", "field": "conversions", "agg": "sum"},
            {"id": "funnel-installs", "label": "App Installs", "dv": "*google_ads", "field": "app_installs", "agg": "sum"},
        ],
        "geo_section": ("Geographic Reach", "Where your message is landing"),
        "trends_section": ("Monthly Trends", "Growth and cost efficiency over time"),
        "trend_ids": ["trend-reach", "trend-clicks", "trend-installs"],
        # geo/trend chart defaults (mirror charts.js _RPT_PLATFORM).
        "geo": {
            "dv": "*google_ads",
            "field": "app_installs",
            "agg": "sum",
            "x_field": "geo_country_code",
            "filters": [{"field": "record_type", "op": "match_phrase", "value": "geo"}],
        },
        "trends": [
            {
                "id": "trend-reach",
                "label": "Impressions",
                "series_index": 0,
                "dv": "*google_ads",
                "field": "impressions",
                "agg": "sum",
            },
            {
                "id": "trend-clicks",
                "label": "Clicks",
                "series_index": 1,
                "dv": "*google_ads",
                "field": "clicks",
                "agg": "sum",
            },
            {
                "id": "trend-installs",
                "label": "App Installs",
                "series_index": 2,
                "dv": "*google_ads",
                "field": "app_installs",
                "agg": "sum",
            },
        ],
    },
}

# ── canva Summary Report defaults ──
# Keep in sync with the `M` metric list + section markup in renderCanvaChart
# (static/dashboard_builder/charts.js) — same convention as _RPT_SEED_PLATFORM.
# Each metric: (id, label, data_view, field, aggregation, extra-binding-keys).
_CANVA_SEED_METRICS: dict[str, dict] = {
    # dv values must be REAL data-view patterns: the old "*fb_page" /
    # "*youtube" / "*ga" shortcuts matched no index (Layer-1 rewrote them to the
    # empty-scope sentinel, so the tiles read 0). fb-shares also swapped
    # page_post_engagements (all engagements) for page_posts_shares_total (the
    # actual shares count every other surface reports). Keep in sync with
    # charts.js M[] + builder.js _CANVA_SECTIONS.
    # Section 1: Reach
    "fb-reach": {
        "label": "Facebook Reach (sum of daily, not unique)",
        "dv": "*fb_insights",
        "field": "insight_reach",
        "agg": "sum",
    },
    "ig-reach": {"label": "Instagram Reach", "dv": "*ig_insights", "field": "reach", "agg": "sum"},
    "web-users": {"label": "Web Users", "dv": "*_ga_metrics", "field": "totalUsers", "agg": "sum"},
    "meta-reach": {
        # Page viewers, summed per day (renamed "Media Views").
        "label": "Facebook Page Viewers (sum of daily, not unique)",
        "dv": "*_fb_page_insights",
        "field": "page_total_media_view_unique",
        "agg": "sum",
    },
    # the same account-level video-view gains as the engage card's tt-views.
    "tt-video-views": {"label": "TikTok Video Views", "dv": "*_tiktok_organic", "field": "video_views_gained", "agg": "sum"},
    # Section 2: Engage
    # lifetime snapshot over recent videos: max, never sum.
    "vid-15s": {
        "label": "15s Views (lifetime)",
        "dv": "*_fb_page_insights",
        "field": "page_video_views_15s_total",
        "agg": "max",
    },
    "vid-time": {
        "label": "View Time (Minutes)",
        "dv": "*_youtube_analytics",
        "field": "estimated_minutes_watched",
        "agg": "sum",
    },
    "app-installs": {"label": "App Installs", "dv": "*google_ads", "field": "app_installs", "agg": "sum"},
    "fb-reactions": {
        "label": "Reactions",
        "dv": "*_fb_page_insights",
        "field": "page_actions_post_reactions_total",
        "agg": "sum",
    },
    "fb-shares": {"label": "Shares", "dv": "*_fb_page_insights", "field": "page_posts_shares_total", "agg": "sum"},
    # paid twins (Meta ads); action_type 'post' is FB's shares action.
    "fbads-thruplays": {
        "label": "ThruPlays (ads)",
        "dv": "*fb_insights",
        "field": "insight_video_thruplay_watched_actions_video_view_total",
        "agg": "sum",
    },
    "fbads-reactions": {
        "label": "Reactions (ads)",
        "dv": "*fb_insights",
        "field": "insight_actions_post_reaction",
        "agg": "sum",
    },
    "fbads-shares": {"label": "Shares (ads)", "dv": "*fb_insights", "field": "insight_actions_post", "agg": "sum"},
    "ig-follows": {"label": "Follows", "dv": "*ig_insights", "field": "follows", "agg": "sum"},
    "ig-reactions": {"label": "Reactions", "dv": "*ig_insights", "field": "likes", "agg": "sum"},
    "ig-shares": {"label": "Shares", "dv": "*ig_insights", "field": "shares", "agg": "sum"},
    "yt-subs": {"label": "Subscriptions", "dv": "*_youtube_analytics", "field": "subscribers_gained", "agg": "sum"},
    "yt-reactions": {"label": "Reactions", "dv": "*_youtube_analytics", "field": "likes", "agg": "sum"},
    "yt-shares": {"label": "Shares", "dv": "*_youtube_analytics", "field": "shares", "agg": "sum"},
    # TikTok: a lifetime follower snapshot (max) + daily gains (sum).
    "tt-followers": {"label": "Followers (current)", "dv": "*_tiktok_organic", "field": "follower_count", "agg": "max"},
    "tt-follows": {"label": "Follows", "dv": "*_tiktok_organic", "field": "followers_gained", "agg": "sum"},
    "tt-views": {"label": "Views", "dv": "*_tiktok_organic", "field": "video_views_gained", "agg": "sum"},
    "tt-reactions": {"label": "Reactions", "dv": "*_tiktok_organic", "field": "likes_gained", "agg": "sum"},
    "tt-shares": {"label": "Shares", "dv": "*_tiktok_organic", "field": "video_shares_gained", "agg": "sum"},
    # Section 3: Interact
    # "also": extra sources the live report (charts.js M[]) sums into
    # the tile — Chatwoot beside Echo. Seed-time section visibility counts them;
    # seeded canvas tiles stay bound to the primary source (canvas field bindings
    # are single-source).
    "msg-contacts": {
        "label": "Messaging Contacts",
        "dv": "*echo_conversations",
        "field": "client_id",
        "agg": "cardinality",
        "also": [{"dv": "*_chatwoot_insights", "field": "contacts_started", "agg": "sum"}],
    },
    "msg-received": {
        "label": "Messages Received",
        "dv": "*echo_conversations",
        "field": "incomingMessagesCount",
        "agg": "sum",
        # archived months (retention rollups) count too.
        "also": [
            {"dv": "*_chatwoot_insights", "field": "incoming_messages_count", "agg": "sum"},
            {"dv": "*_conversation_rollups", "field": "incomingMessagesCount", "agg": "sum"},
        ],
    },
    "msg-sent": {
        "label": "Messages Sent",
        "dv": "*echo_conversations",
        "field": "outboundMessagesCount",
        "agg": "sum",
        "also": [
            {"dv": "*_chatwoot_insights", "field": "outgoing_messages_count", "agg": "sum"},
            {"dv": "*_conversation_rollups", "field": "outboundMessagesCount", "agg": "sum"},
        ],
    },
    # page_positive_feedback_by_type_comment is a Page Insights metric
    # Meta retired — 0 for every team. Comments on ADS are what Meta still
    # returns (insight_actions_comment), so the tile counts those and says so,
    # like the band's Reactions (ads) / Shares (ads) twins.
    "comments": {
        "label": "Comments (ads)",
        "dv": "*fb_insights",
        "field": "insight_actions_comment",
        "agg": "sum",
    },
    "spiritual": {
        "label": "Spiritual Conversations",
        "dv": "*echo_conversations",
        "field": "spiritual_conversation",
        "agg": "equals",
        # the Echo importer writes this flag as an INTEGER 0/1 and the
        # schema maps it integer; a term on "true" made ES reject the whole
        # query ('For input string: "true"') so the tile read 0 for every team.
        "equals_value": "1",
        "also": [
            {"dv": "*_chatwoot_insights", "field": "contact_attr_spiritual_conversation", "agg": "sum"},
            {"dv": "*_conversation_rollups", "field": "spiritual_conversation", "agg": "sum"},
        ],
    },
    # Section 4: Journey
    "baptisms": {
        "label": "Baptisms",
        "dv": "*_disciple_tools",
        "field": "meta.milestones.keyword",
        "agg": "equals",
        "equals_value": "milestone_baptized",
    },
    "new-groups": {
        "label": "New Groups",
        "dv": "*_disciple_tools",
        "field": "post_type",
        "agg": "equals",
        "equals_value": "groups",
    },
    "new-contacts": {
        "label": "New Contacts",
        "dv": "*_disciple_tools",
        "field": "post_type",
        "agg": "equals",
        "equals_value": "contacts",
    },
    "first-meetings": {
        "label": "First Meetings",
        "dv": "*_disciple_tools",
        "field": "meta.seeker_path.keyword",
        "agg": "equals",
        "equals_value": "met",
    },
    "new-churches": {
        "label": "New Churches",
        "dv": "*_disciple_tools",
        "field": "meta.church_start_date",
        "agg": "exists",
        "dt_date_unix": True,
    },
}

# Section palette — mirrors the .cv-reach/.cv-engage/.cv-interact/.cv-journey
# CSS in renderCanvaChart.
_CANVA_PALETTE = {
    "reach": {"card": "#8db4d2", "badge": "#6a9bbf", "title": "#1a3a5c", "label": "#1a3a5c", "value": "#1a3a5c"},
    "engage": {
        "card": "#c5d8e8",
        "badge": "#7ba7c9",
        "title": "#1a3a5c",
        "label": "#555555",
        "value": "#1a3a5c",
        "ecard": "#dce8f0",
    },
    "interact": {"card": "#2c3e50", "badge": "#3d566e", "title": "#ffffff", "label": "#a0b4c8", "value": "#ffffff"},
    "journey": {"card": "#d4a535", "badge": "#c49520", "title": "#3a2a00", "label": "#5a4000", "value": "#3a2a00"},
}

# Section badge + platform icons, all from the icon library. The live chart
# inlines bespoke SVGs; on canvas the nearest library icon stands in.
# follow-up: badge icons mirror _SEC_ICON in charts.js exactly —
# reach/journey use the report's inline SVGs exported to the icon library
# (report-play.svg / report-pin.svg); all badges get the white-ify style.
_CANVA_BADGES = {"reach": "report-play.svg", "engage": "like.svg", "interact": "messages.svg", "journey": "report-pin.svg"}
_CANVA_PLATFORM_ICONS = {
    "fb": "facebook.svg",
    "ig": "instagram.svg",
    "ga": "google-analytics-3-logo.svg",
    "meta": "meta.svg",
    "yt": "youtube.svg",
    "tt": "tiktok-icon-white-1-logo.svg",
}


# ── Layout constants (page coordinates at 100% zoom) ──
# follow-up: 880 (was 1280, then 960). The responsive report
# blocks the seeds mirror reflow to their pane at 100% text size; a fixed
# sheet can't, so its design width must sit at or below the panes it
# renders in. The narrowest common pane is the Builder preview (~860px
# with both side panels open); at 880 the viewer's downscale-only fit
# lands ≈1:1 there and exactly 1:1 everywhere wider — text matches the
# live report on every surface. User iterations 2026-07-10.
_PAGE_W = 880
_MARGIN = 48
_GAP = 16
_TILES_PER_ROW = 4
_CONTENT_W = _PAGE_W - 2 * _MARGIN
_TILE_W = (_CONTENT_W - (_TILES_PER_ROW - 1) * _GAP) // _TILES_PER_ROW
_TILE_H = 96
_HEADER_H = 40
_TITLE_H = 56
_PLACEHOLDER_H = 220
_MIN_PAGE_H = 720
_MAX_PAGE_H = 10000

# Impact-report visuals mirror .rpt-header / .rpt-funnel in renderReportChart
# (charts.js). they are no longer module constants — the header
# emits the "accent" token KEY and the funnel bars resolve accent + the
# chart-series palette from the builder's chosen theme at convert time.

# grouped_table cell geometry — mirrors the .gt-table report look (flush
# bordered cells: colored column-header strip over a white value cell; see
# renderGroupedTable in charts.js + .gt-table in builder.css).
_CELLS_PER_ROW = 6
_CELL_W = _CONTENT_W // _CELLS_PER_ROW
_CELL_H = 66
_GROUP_BAR_H = 34
_TITLE_BAR_H = 44
_GROUP_GAP = 8


class _Builder:
    """Accumulates objects down the page with deterministic ids.

    carries the chosen theme. Converters emit token KEYS in style
    fields (client-resolved against ``page.theme``) and pull concrete values
    from ``self.theme`` / ``self.series`` only where no key form exists
    (chart series, funnel bars, the page background).
    """

    def __init__(self, theme_ref: str | None = None) -> None:
        self.objects: list[dict] = []
        self.skipped: list[dict] = []
        self.y = _MARGIN
        self._seq = 0
        self._sec = 0
        self.theme = canvas_theme.get_theme(theme_ref)
        self.theme_key: str = self.theme["key"]
        self.series = canvas_theme.series_colors(self.theme)
        # Page backdrop follows the seeded content: the theme's page color
        # for table seeds, its paper (.rpt parchment) for impact reports;
        # canva keeps its bespoke builder-gray. Concrete value, not a key —
        # the page-settings color input can't display a token key.
        self.page_bg = canvas_theme.resolve_color("bg", self.theme)

    def next_id(self) -> str:
        self._seq += 1
        return f"obj-{self._seq}"

    def next_section(self) -> str:
        self._sec += 1
        return f"sec-{self._sec}"

    def add(self, obj: dict) -> dict:
        self.objects.append(obj)
        return obj

    def text(self, content, *, x=_MARGIN, w=_CONTENT_W, h=_HEADER_H, style=None, role=None, section_id=None, advance=True):
        obj = {
            "object_id": self.next_id(),
            "object_type": "text",
            "frame": {"x": x, "y": self.y, "w": w, "h": h, "rotation": 0},
            "locked": False,
            "group_id": None,
            "style": style or {"font_size": 15},
            "text": {"content": content},
        }
        if role:
            obj["role"] = role
        if section_id:
            obj["section_id"] = section_id
        self.add(obj)
        if advance:
            self.y += h + _GAP // 2
        return obj

    def tiles(self, items: list[dict], section_id: str) -> None:
        """Lay *items* (pre-built object bodies minus frame/ids) in a grid."""
        for i, body in enumerate(items):
            col = i % _TILES_PER_ROW
            row = i // _TILES_PER_ROW
            obj = {
                "object_id": self.next_id(),
                "object_type": body.pop("object_type", "data_field"),
                "frame": {
                    "x": _MARGIN + col * (_TILE_W + _GAP),
                    "y": self.y + row * (_TILE_H + _GAP),
                    "w": _TILE_W,
                    "h": _TILE_H,
                    "rotation": 0,
                },
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                **body,
            }
            self.add(obj)
        rows = (len(items) + _TILES_PER_ROW - 1) // _TILES_PER_ROW
        self.y += rows * (_TILE_H + _GAP) + _GAP

    def placeholder(self, label: str, reason: str, source_chart_type: str, *, section_id=None, h=_PLACEHOLDER_H):
        obj = {
            "object_id": self.next_id(),
            "object_type": "placeholder",
            "frame": {"x": _MARGIN, "y": self.y, "w": _CONTENT_W, "h": h, "rotation": 0},
            "locked": False,
            "group_id": None,
            "placeholder": {"label": label, "reason": reason, "source_chart_type": source_chart_type},
        }
        if section_id:
            obj["section_id"] = section_id
        self.add(obj)
        self.y += h + _GAP
        return obj

    def skip(self, chart_id: str, chart_type: str, element: str, reason: str) -> None:
        self.skipped.append({"chart_id": chart_id, "chart_type": chart_type, "element": element, "reason": reason})
        logger.warning(
            "canvas seed: could not convert %s (%s) of chart %s: %s",
            element,
            chart_type,
            chart_id,
            reason,
        )


def _section_header(b: _Builder, title: str, sub: str | None, section_id: str, *, bg_color=None, color=None) -> None:
    # "ink" == the render-time CSS default (#16191f) in classic, so this is
    # byte-identical there — but on a dark theme the title must resolve to
    # that theme's ink instead of vanishing into the page.
    style = {"font_size": 20, "bold": True, "color": "ink"}
    if bg_color:
        style["bg_color"] = bg_color
    if color:
        style["color"] = color
    b.text(title, h=_HEADER_H, style=style, role="section_header", section_id=section_id)
    if sub:
        # Token KEY — resolves against page.theme client-side.
        b.text(sub, h=24, style={"font_size": 12, "color": "muted"}, section_id=section_id)


def _column_binding(col: dict) -> dict | None:
    """Live binding for a grouped_table column — data_view + field +
    aggregation (+ equals_value), taken from the column's first data source.
    """
    sources = col.get("data_sources") or []
    src = sources[0] if sources and isinstance(sources[0], dict) else {}
    data_view = src.get("data_view")
    field = src.get("field")
    if not data_view or not field:
        return None
    binding = {
        "data_view": data_view,
        "field": field,
        "aggregation": col.get("aggregation") or "sum",
    }
    if col.get("equals_value") is not None:
        binding["equals_value"] = col.get("equals_value")
    return binding


def _int_or(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _convert_table(b: _Builder, chart: dict) -> None:
    """copy a legacy ``table`` chart onto the canvas as a live
    table object.

    The chart config rides VERBATIM under ``table`` (minus the grid
    ``position``/``width``/``height``, which the canvas frame replaces), so
    the same config produces identical output in the legacy dashboard and on
    the canvas — both surfaces query api/query and render through charts.js
    renderTable (the acceptance criterion). A title row is emitted
    when the legacy slot showed one (hide_title explicitly false).
    """
    title = str(chart.get("title") or "").strip()
    if title and chart.get("hide_title") is False:
        b.text(title, h=_HEADER_H, style={"font_size": 18, "bold": True, "color": "ink"})
    config = {k: v for k, v in chart.items() if k not in ("position", "width", "height")}
    try:
        num_rows = int(chart.get("num_values") or 10)
    except (TypeError, ValueError):
        num_rows = 10
    # Header (~40px) + ~26px per row, bounded so a 1000-row table doesn't
    # mint a frame taller than the page; overflow scrolls inside the frame.
    h = max(120, min(520, 40 + num_rows * 26))
    b.add(
        {
            "object_id": b.next_id(),
            "object_type": "table",
            "frame": {"x": _MARGIN, "y": b.y, "w": _CONTENT_W, "h": h, "rotation": 0},
            "locked": False,
            "group_id": None,
            "table": config,
        }
    )
    b.y += h + _GAP


def _convert_grouped_table(b: _Builder, chart: dict) -> None:
    """Mirror the live grouped_table (.gt-table) look: full-width title bar,
    a colored group-header bar per group, then that group's columns as flush
    bordered cells — colored column-header strip on top, white value cell
    below. Colors and text styles come from the same chart-config keys
    renderGroupedTable uses in charts.js; where the config is silent, the
    defaults are theme token KEYS whose classic values are the
    exact charts.js literals ("#2c6da0" / "#5a9cc5" / "#ffffff"), so a
    default-theme seed renders byte-identically while a themed seed
    restyles through its own palette.
    """
    chart_id = chart.get("chart_id", "?")
    title_bg = chart.get("title_bg_color") or "header_bg"
    group_bg = chart.get("group_header_bg_color") or "subheader_bg"
    col_bg = chart.get("col_header_bg_color") or "header_bg"
    text_color = chart.get("header_text_color") or "header_text"
    group_color = chart.get("group_text_color") or text_color
    col_color = chart.get("col_text_color") or text_color

    title = chart.get("table_title") or chart.get("title") or "Summary Report"
    b.text(
        title,
        h=_TITLE_BAR_H,
        style={"font_size": 18, "bold": True, "align": "center", "color": text_color, "bg_color": title_bg},
        role="title",
        advance=False,
    )
    b.y += _TITLE_BAR_H

    cell_style = {
        "variant": "table_cell",
        "format": "number",
        "align": "center",
        "bg_color": "surface",
        "border_color": "border",
        "label_position": "top",
        "label_bg_color": col_bg,
        "label_color": col_color,
        "label_font_size": _int_or(chart.get("col_font_size"), 12),
        "label_bold": chart.get("col_bold") is not False,
        "value_color": "value_ink",
        "value_font_size": 16,
    }

    for group in chart.get("groups") or []:
        section_id = b.next_section()
        b.text(
            group.get("label") or "Section",
            h=_GROUP_BAR_H,
            style={
                "font_size": _int_or(chart.get("group_font_size"), 13),
                "bold": chart.get("group_bold") is not False,
                "align": "center",
                "color": group_color,
                "bg_color": group_bg,
            },
            role="section_header",
            section_id=section_id,
            advance=False,
        )
        b.y += _GROUP_BAR_H

        cols = group.get("columns") or []
        for i, col in enumerate(cols):
            frame = {
                "x": _MARGIN + (i % _CELLS_PER_ROW) * _CELL_W,
                "y": b.y + (i // _CELLS_PER_ROW) * _CELL_H,
                "w": _CELL_W,
                "h": _CELL_H,
                "rotation": 0,
            }
            label = col.get("label") or "Metric"
            binding = _column_binding(col)
            if binding is None:
                b.skip(chart_id, "grouped_table", f"column '{label}'", "column has no data source")
                b.add(
                    {
                        "object_id": b.next_id(),
                        "object_type": "placeholder",
                        "frame": frame,
                        "locked": False,
                        "group_id": None,
                        "section_id": section_id,
                        "placeholder": {
                            "label": label,
                            "reason": "This column has no data source to bind.",
                            "source_chart_type": "grouped_table",
                        },
                    }
                )
                continue
            b.add(
                {
                    "object_id": b.next_id(),
                    "object_type": "data_field",
                    "frame": frame,
                    "locked": False,
                    "group_id": None,
                    "section_id": section_id,
                    "label": label,
                    "style": dict(cell_style),
                    "binding": binding,
                }
            )
        rows = (len(cols) + _CELLS_PER_ROW - 1) // _CELLS_PER_ROW
        b.y += rows * _CELL_H + _GROUP_GAP


def _canva_binding(ov: dict, metric_id: str) -> dict:
    """Live binding for a canva metric, with the chart's ``metric_overrides``
    entry applied the same way renderCanvaChart's ``_m`` helper does
    (dv/field/agg override individually; a non-empty ``equals_value``
    override wins; ``dt_date_unix`` passes through time
    wiring)."""
    m = _CANVA_SEED_METRICS[metric_id]
    o = ov.get(metric_id) or {}
    binding = {
        "data_view": o.get("dv") or m["dv"],
        "field": o.get("field") or m["field"],
        "aggregation": o.get("agg") or m["agg"],
    }
    equals_value = o.get("equals_value") or m.get("equals_value")
    if equals_value is not None:
        binding["equals_value"] = equals_value
    if o.get("dt_date_unix") is not None:
        binding["dt_date_unix"] = o["dt_date_unix"]
    elif m.get("dt_date_unix") is not None:
        binding["dt_date_unix"] = m["dt_date_unix"]
    return binding


def _canva_label(ov: dict, metric_id: str) -> str:
    o = ov.get(metric_id) or {}
    return o.get("label") or _CANVA_SEED_METRICS[metric_id]["label"]


# seed-time — the Summary sections and the metric ids each one
# renders. Used to roll per-pattern visibility up to whole sections so a
# team-scoped seed is born without the sections that team does not use.
# Keep in sync with the section blocks inside _convert_canva below (the
# lockstep test pins every id against _CANVA_SEED_METRICS).
_CANVA_SECTION_METRICS = {
    "sec-reach": ["fb-reach", "ig-reach", "web-users", "meta-reach", "tt-video-views"],
    "sec-engage": [
        "vid-15s",
        "vid-time",
        "app-installs",
        "fb-reactions",
        "fb-shares",
        "fbads-thruplays",
        "fbads-reactions",
        "fbads-shares",
        "ig-follows",
        "ig-reactions",
        "ig-shares",
        "yt-subs",
        "yt-reactions",
        "yt-shares",
        "tt-followers",
        "tt-follows",
        "tt-views",
        "tt-reactions",
        "tt-shares",
    ],
    "sec-interact": ["msg-contacts", "msg-received", "msg-sent", "comments", "spiritual"],
    "sec-journey": ["baptisms", "new-groups", "new-contacts", "first-meetings", "new-churches"],
}


def visible_canva_sections(es, team_titles, chart: dict):
    """seed-time — which Summary sections to seed for a team scope.

    Whole-section granularity (a teammate's option-2 call, 2026-08-27): a section
    seeds when ANY of its metrics' override-resolved data views is visible
    for the scope (existence AND ds_* toggle — same semantics as the live
    report's auto-hide), or when a per-team override force-shows any unit in
    it; a section-level 'hide' override drops it. Returns None for "seed
    everything": no team scope, or every lookup failing (fail open — a
    degraded seed must not silently produce an empty page).
    """
    titles = [t for t in (team_titles or []) if t]
    if not titles:
        return None
    try:
        from web.services import summary_visibility_service as svs
    except ImportError:
        from ...services import summary_visibility_service as svs

    ov = chart.get("metric_overrides") or {}
    # a tile's extra ("also") sources count too: a Chatwoot-only team
    # still gets the interact section.
    section_dvs = {
        sec: {_canva_binding(ov, m)["data_view"] for m in mids}
        | {src["dv"] for m in mids for src in _CANVA_SEED_METRICS[m].get("also", [])}
        for sec, mids in _CANVA_SECTION_METRICS.items()
    }
    patterns = sorted({dv for dvs in section_dvs.values() for dv in dvs})
    vis = svs.visible_patterns_for_titles(es, titles, patterns)
    if not vis:
        return None  # lookup failed / empty — fail open, seed everything
    overrides = svs.merged_overrides_for_titles(es, titles)
    visible: set = set()
    for sec, dvs in section_dvs.items():
        if overrides.get(sec) == "hide":
            continue
        auto = any(vis.get(dv, True) for dv in dvs)  # unknown dv -> visible
        rescued = any(state == "show" and svs.UNIT_SECTION.get(unit) == sec for unit, state in overrides.items())
        if auto or rescued:
            visible.add(sec)
    return visible


def _convert_canva(b: _Builder, chart: dict, visible_sections=None) -> None:
    """Mirror the canva Summary Report card layout (renderCanvaChart): a
    reach card and an engage card side by side, then full-width interact and
    journey bands — colored rounded cards with a circular badge icon on the
    top edge, platform icons, and label-over-value metrics.

    Cards are text objects with a fill + radius (a dedicated shape type is
    ); each card and its contents share a ``section_id``.

     scope note: the canva palette is the live report's bespoke
    art direction (four section-specific card colors), not a theme token
    set — it stays literal under every theme so the seed keeps mirroring
    renderCanvaChart exactly.
    """
    b.page_bg = "#f0f2f5"
    ov = chart.get("metric_overrides") or {}
    x0 = _MARGIN
    pal = _CANVA_PALETTE
    # follow-up: taller sections — the live report's cards breathe
    # (row pitch ~80px, tall ecards); the first cut's compressed heights
    # read visibly squatter than the report next to it.
    top_h = 430
    band_h = 156
    badge_r = 22

    def rect(x, y, w, h, bg, radius, section_id):
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "text",
                "frame": {"x": x, "y": y, "w": w, "h": h, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "role": "card",
                "style": {"bg_color": bg, "radius": radius},
                "text": {"content": ""},
            }
        )

    def icon(name, x, y, size, section_id, style=None):
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "icon",
                "frame": {"x": x, "y": y, "w": size, "h": size, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "icon": {"name": name},
                **({"style": style} if style else {}),
            }
        )

    def badge(kind, card_x, card_w, card_y, section_id):
        icon(
            _CANVA_BADGES[kind],
            card_x + card_w // 2 - badge_r,
            card_y - badge_r,
            badge_r * 2,
            section_id,
            # "white" mirrors _SEC_ICON's brightness(0) invert(1) filter in
            # charts.js — badge glyphs render white on the colored circle.
            style={"bg_color": pal[kind]["badge"], "radius": badge_r, "padding": 10, "white": True},
        )

    def title(content, kind, x, y, w, section_id, *, h=30, font_size=16):
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "text",
                "frame": {"x": x, "y": y, "w": w, "h": h, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "role": "section_header",
                "style": {"font_size": font_size, "bold": True, "align": "center", "color": pal[kind]["title"]},
                "text": {"content": content},
            }
        )

    def field(metric_id, kind, x, y, w, h, section_id, *, value_size=22, label_hidden=False):
        style = {
            "variant": "plain",
            "format": "number",
            "align": "center",
            "label_position": "top",
            "label_color": pal[kind]["label"],
            "label_font_size": 11,
            "value_color": pal[kind]["value"],
            "value_font_size": value_size,
        }
        if label_hidden:
            style["label_hidden"] = True
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "data_field",
                "frame": {"x": x, "y": y, "w": w, "h": h, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "label": _canva_label(ov, metric_id),
                "style": style,
                "binding": _canva_binding(ov, metric_id),
            }
        )

    # follow-up: mirror the report's.cv padding (16px). The
    # generic 48px seed margin doubled the report's gap between the filter
    # strip and the first cards; only applies when this canva section opens
    # the page (a multi-chart seed keeps its running y).
    if b.y == _MARGIN:
        b.y = 16
    y0 = b.y + badge_r  # room for the badge overhang above the first cards

    # seed-time (option 2, whole-section skip): a dark section is not
    # seeded and the layout reflows — a lone top card takes the full width,
    # bands stack up into freed space. None = seed all four (no team scope).
    show_reach = visible_sections is None or "sec-reach" in visible_sections
    show_engage = visible_sections is None or "sec-engage" in visible_sections

    # ── Section 1: Reach (left card) ──
    # Proportional (was a hardcoded 368 at the old 1280 page width) so the
    # reach/engage split survives page-width changes. Full width when Engage
    # is dark.
    reach_w = round(_CONTENT_W * 0.31) if show_engage else _CONTENT_W
    if show_reach:
        sec = b.next_section()
        rect(x0, y0, reach_w, top_h, pal["reach"]["card"], 16, sec)
        badge("reach", x0, reach_w, y0, sec)
        title("How Many People Saw Our Content?", "reach", x0 + 16, y0 + 26, reach_w - 32, sec, h=44)
        row_icons = [
            ("fb", "fb-reach"),
            ("ig", "ig-reach"),
            ("ga", "web-users"),
            ("fb", "meta-reach"),  # a Facebook Page metric, not a cross-platform Meta total
            ("tt", "tt-video-views"),
        ]
        row_y = y0 + 82
        # the row pitch compresses past four rows so every row stays
        # inside top_h; four rows keep the original 84px rhythm.
        row_pitch = min(84, (top_h - 90) // len(row_icons))
        row_h = min(72, row_pitch - 4)
        for platform, metric_id in row_icons:
            # 36px icons (ig rounded) — the exact _ICON sizes the report renders.
            icon(
                _CANVA_PLATFORM_ICONS[platform],
                x0 + 28,
                row_y + (row_h - 36) // 2,
                36,
                sec,
                style={"radius": 8} if platform == "ig" else None,
            )
            field(metric_id, "reach", x0 + 72, row_y, reach_w - 96, row_h, sec, label_hidden=True)
            row_y += row_pitch

    # ── Section 2: Engage (right card with four sub-cards) ──
    if show_engage:
        sec = b.next_section()
        eng_x = x0 + (reach_w + _GAP if show_reach else 0)
        eng_w = _CONTENT_W - (reach_w + _GAP if show_reach else 0)
        rect(eng_x, y0, eng_w, top_h, pal["engage"]["card"], 16, sec)
        badge("engage", eng_x, eng_w, y0, sec)
        title("How did they engage with our content?", "engage", eng_x + 16, y0 + 22, eng_w - 32, sec, h=28)
    ecard_gap = 10
    ecard_y = y0 + 62
    ecard_h = top_h - 78
    ecards = (
        []
        if not show_engage
        else [
            {"icon": None, "title": "Videos Watched", "metrics": ["vid-15s", "vid-time", "app-installs"]},
            # the FB card carries the paid twins after the organic trio
            # (labels carry "(ads)"); the viewer's markup twin is the cv-esep block.
            {
                "icon": "fb",
                "title": None,
                "metrics": ["fb-reactions", "fb-shares", "fbads-thruplays", "fbads-reactions", "fbads-shares"],
            },
            {"icon": "ig", "title": None, "metrics": ["ig-follows", "ig-reactions", "ig-shares"]},
            {"icon": "yt", "title": None, "metrics": ["yt-subs", "yt-reactions", "yt-shares"]},
            {"icon": "tt", "title": None, "metrics": ["tt-followers", "tt-follows", "tt-views", "tt-reactions", "tt-shares"]},
        ]
    )
    # the card width divides the band by however many cards it holds
    # (was a fixed 4; TikTok made it 5).
    ecard_count = max(len(ecards), 1)
    ecard_w = ((eng_w if show_engage else _CONTENT_W) - 32 - (ecard_count - 1) * ecard_gap) // ecard_count
    for i, ecard in enumerate(ecards):
        ex = eng_x + 16 + i * (ecard_w + ecard_gap)
        rect(ex, ecard_y, ecard_w, ecard_h, pal["engage"]["ecard"], 12, sec)
        fy = ecard_y + 56
        if ecard["icon"]:
            icon(
                _CANVA_PLATFORM_ICONS[ecard["icon"]],
                ex + ecard_w // 2 - 18,
                ecard_y + 10,
                36,
                sec,
                style={"radius": 8} if ecard["icon"] == "ig" else None,
            )
        else:
            title(ecard["title"], "engage", ex + 4, ecard_y + 12, ecard_w - 8, sec, h=24, font_size=13)
        # pitch compresses when a card holds more than 3 metrics
        # (the FB card holds 6) so every field stays inside ecard_h; 3-metric
        # cards keep the original 96px rhythm byte-identically.
        pitch = min(96, (ecard_h - 56) // len(ecard["metrics"]))
        for metric_id in ecard["metrics"]:
            field(metric_id, "engage", ex + 8, fy, ecard_w - 16, min(80, pitch - 16), sec)
            fy += pitch

    # ── Sections 3 + 4: full-width interact / journey bands ──
    # seed-time: bands stack straight after whatever rendered above —
    # no top row means they start at y0.
    band_y = y0 + ((top_h + badge_r + _GAP) if (show_reach or show_engage) else 0)
    bands = [
        ("interact", "How did they interact with us?", ["msg-contacts", "msg-received", "msg-sent", "comments", "spiritual"]),
        (
            "journey",
            "Where are they on their Jesus Journey?",
            ["baptisms", "new-groups", "new-contacts", "first-meetings", "new-churches"],
        ),
    ]
    for kind, band_title, metric_ids in bands:
        if visible_sections is not None and f"sec-{kind}" not in visible_sections:
            continue  # seed-time: dark band is not seeded
        sec = b.next_section()
        rect(x0, band_y, _CONTENT_W, band_h, pal[kind]["card"], 16, sec)
        badge(kind, x0, _CONTENT_W, band_y, sec)
        title(band_title, kind, x0 + 16, band_y + 22, _CONTENT_W - 32, sec, h=26)
        cell_w = (_CONTENT_W - 32) // len(metric_ids)
        for i, metric_id in enumerate(metric_ids):
            field(metric_id, kind, x0 + 16 + i * cell_w, band_y + 60, cell_w, 80, sec, value_size=24)
        band_y += band_h + badge_r + _GAP

    b.y = band_y


def _override(ov: dict, metric: dict) -> dict:
    """Apply a chart's ``metric_overrides`` entry onto a platform metric."""
    o = ov.get(metric["id"]) or {}
    return {
        **metric,
        "label": o.get("label") or metric.get("label"),
        "dv": o.get("dv") or metric.get("dv"),
        "field": o.get("field") or metric.get("field"),
        "agg": o.get("agg") or metric.get("agg"),
    }


def _metric_binding(m: dict) -> dict:
    return {"data_view": m["dv"], "field": m["field"], "aggregation": m.get("agg") or "sum"}


def _convert_impact_report(b: _Builder, chart: dict) -> None:
    chart_type = chart.get("chart_type")
    chart_id = chart.get("chart_id", "?")
    b.page_bg = canvas_theme.resolve_color("paper", b.theme)
    platform = _RPT_SEED_PLATFORM[chart_type]
    ov = chart.get("metric_overrides") or {}
    hidden = set(chart.get("hidden_metrics") or [])

    # Header — mirrors .rpt-header: navy band, gold org eyebrow, white
    # serif title, muted subtitle, date-range pill, platform icon inside
    # the band's top-right corner (it previously floated at the page edge).
    org_name = chart.get("org_name") or "Your Organization"
    header_sec = b.next_section()
    hy = b.y
    header_h = 152
    b.add(
        {
            "object_id": b.next_id(),
            "object_type": "text",
            "frame": {"x": _MARGIN, "y": hy, "w": _CONTENT_W, "h": header_h, "rotation": 0},
            "locked": False,
            "group_id": None,
            "section_id": header_sec,
            "role": "card",
            "style": {"bg_color": "accent", "radius": 16},
            "text": {"content": ""},
        }
    )
    b.add(
        {
            "object_id": b.next_id(),
            "object_type": "icon",
            "frame": {"x": _MARGIN + _CONTENT_W - 72, "y": hy + 20, "w": 48, "h": 48, "rotation": 0},
            "locked": False,
            "group_id": None,
            "section_id": header_sec,
            "icon": {"name": platform["icon"]},
        }
    )

    def _header_text(content, y, h, style, role=None):
        obj = {
            "object_id": b.next_id(),
            "object_type": "text",
            "frame": {"x": _MARGIN + 28, "y": y, "w": _CONTENT_W - 120, "h": h, "rotation": 0},
            "locked": False,
            "group_id": None,
            "section_id": header_sec,
            "style": style,
            "text": {"content": content},
        }
        if role:
            obj["role"] = role
        b.add(obj)
        return obj

    # On-accent decorations — token KEYS, classic values are the
    # exact .rpt-header literals (gold eyebrow, white title, muted subtitle).
    _header_text(org_name.upper(), hy + 14, 22, {"font_size": 12, "bold": True, "color": "eyebrow"})
    _header_text(platform["title"], hy + 38, 42, {"font_size": 28, "bold": True, "color": "accent_ink"}, role="title")
    _header_text(platform["subtitle"], hy + 82, 22, {"font_size": 13, "color": "accent_soft"})
    # Date pill — a live time pill: every canvas surface renders
    # role "time_pill" text from the page's current time range; the stored
    # content is only the no-range fallback wording.
    pill = _header_text(
        "All Time",
        hy + 112,
        26,
        {"font_size": 12, "align": "center", "color": "pill_ink", "bg_color": "pill_bg", "radius": 100},
        role="time_pill",
    )
    pill["frame"]["w"] = 170
    b.y = hy + header_h + _GAP

    # KPI section.
    kpis = [k for k in platform["kpis"] if k["id"] not in hidden]
    comp = platform.get("computed_kpi") or {}
    if comp and (comp.get("numerator") in hidden or comp.get("denominator") in hidden):
        kpis = [k for k in kpis if k["id"] != comp.get("id")]
    if kpis:
        section_id = b.next_section()
        _section_header(b, platform["kpi_section"][0], platform["kpi_section"][1], section_id)
        by_id = {k["id"]: _override(ov, k) for k in platform["kpis"]}
        tiles = []
        for k in kpis:
            m = by_id[k["id"]]
            if k.get("computed"):
                num = by_id.get(comp.get("numerator"))
                den = by_id.get(comp.get("denominator"))
                tiles.append(
                    {
                        "label": m["label"],
                        "style": {"format": "currency"},
                        "binding": {
                            "computed": True,
                            "numerator": _metric_binding(num),
                            "denominator": _metric_binding(den),
                        },
                    }
                )
            else:
                tiles.append(
                    {
                        "label": m["label"],
                        "style": {"format": "currency" if k.get("currency") else "number"},
                        "binding": _metric_binding(m),
                    }
                )
        b.tiles(tiles, section_id)

    # Funnel section — mirrors .rpt-funnel: a white card with a proportional
    # bar over each step's value. Bars use the template's initial heights
    # (the live chart later rescales them to real values); they are ordinary
    # movable/deletable rect objects.
    funnel = [f for f in platform["funnel"] if f["id"] not in hidden]
    if funnel:
        section_id = b.next_section()
        _section_header(b, platform["funnel_section"][0], platform["funnel_section"][1], section_id)
        n = len(funnel)
        bar_zone_h = 110
        card_h = 20 + bar_zone_h + 66 + 16
        fy = b.y
        # Bars are data-viz colors with no token-key form (series entries are
        # positional) — resolved from the chosen theme at seed time.
        funnel_colors = (canvas_theme.resolve_color("accent", b.theme),) + tuple(b.series[:4])
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "text",
                "frame": {"x": _MARGIN, "y": fy, "w": _CONTENT_W, "h": card_h, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "role": "card",
                "style": {"bg_color": "surface", "radius": 14},
                "text": {"content": ""},
            }
        )
        step_w = (_CONTENT_W - 56) // n
        for i, f in enumerate(funnel):
            m = _override(ov, f)
            pct = max(100 - i * (90 / (n - 1)), 6) if n > 1 else 100
            bar_h = max(int(bar_zone_h * pct / 100), 10)
            bar_w = int(step_w * 0.6)
            step_x = _MARGIN + 28 + i * step_w
            b.add(
                {
                    "object_id": b.next_id(),
                    "object_type": "text",
                    "frame": {
                        "x": step_x + (step_w - bar_w) // 2,
                        "y": fy + 20 + (bar_zone_h - bar_h),
                        "w": bar_w,
                        "h": bar_h,
                        "rotation": 0,
                    },
                    "locked": False,
                    "group_id": None,
                    "section_id": section_id,
                    "role": "funnel_bar",
                    "style": {"bg_color": funnel_colors[i % len(funnel_colors)], "radius": 8},
                    "text": {"content": ""},
                }
            )
            b.add(
                {
                    "object_id": b.next_id(),
                    "object_type": "data_field",
                    "frame": {"x": step_x, "y": fy + 20 + bar_zone_h + 4, "w": step_w, "h": 62, "rotation": 0},
                    "locked": False,
                    "group_id": None,
                    "section_id": section_id,
                    "label": m["label"],
                    "style": {
                        "variant": "plain",
                        "format": "number",
                        "align": "center",
                        # accent_text, not accent: the value sits ON the
                        # surface card — a dark theme's accent is itself a
                        # card color and would vanish there (classic values
                        # are identical, accent == accent_text == navy).
                        "value_color": "accent_text",
                        "value_font_size": 22,
                        "label_color": "muted",
                        "label_font_size": 12,
                    },
                    "binding": _metric_binding(m),
                }
            )
        b.y = fy + card_h + _GAP

    # Geo + trends — live chart objects: the shared
    # canvas_charts.js renderer ports the report's world map and
    # small-multiples trend charts; bindings stay LIVE, nothing is baked.
    if "geo" not in hidden:
        section_id = b.next_section()
        _section_header(b, platform["geo_section"][0], platform["geo_section"][1], section_id)
        geo = _override(dict(ov), {"id": "geo", "label": "", **platform["geo"]})
        geo_h = 380
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "chart",
                "frame": {"x": _MARGIN, "y": b.y, "w": _CONTENT_W, "h": geo_h, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "chart": {
                    "kind": "geo",
                    "statistic": "reach",
                    "show_list": chart.get("show_geo_list") is not False,
                    "geo": {
                        "data_view": geo["dv"],
                        "field": geo["field"],
                        "aggregation": geo.get("agg") or "sum",
                        "x_field": platform["geo"].get("x_field", "team_country"),
                        "filters": list(platform["geo"].get("filters") or []),
                    },
                },
                # Literal white, NOT "surface": canvas_charts.js draws the
                # chart internals (axis labels, country list) in fixed dark
                # inks — a dark theme's surface would make the chart
                # unreadable, so chart cards stay white under every theme.
                # (Chart-internals theming fell out of scope —
                # it's a future-epic item; switch to "surface" then.)
                "style": {"bg_color": "#ffffff", "radius": 14},
            }
        )
        b.y += geo_h + _GAP
    trends = [t for t in platform["trends"] if t["id"] not in hidden]
    if trends:
        section_id = b.next_section()
        _section_header(b, platform["trends_section"][0], platform["trends_section"][1], section_id)
        series = []
        for i, t in enumerate(trends):
            m = _override(ov, t)
            # Series colors come from the chosen theme's chart palette by the
            # metric's STABLE series_index — hiding one trend must
            # not shift its neighbours' colors (classic values unchanged).
            idx = t.get("series_index", i)
            palette_color = b.series[idx % len(b.series)] if b.series else "#2a8a7a"
            series.append(
                {
                    "id": t["id"],
                    "label": m["label"],
                    "color": t.get("color") or palette_color,
                    "data_view": m["dv"],
                    "field": m["field"],
                    "aggregation": m.get("agg") or "sum",
                }
            )
        # The renderer draws an 800-wide viewBox with 120px rows scaled to
        # the frame width — size the frame to that aspect (plus card padding)
        # so nothing clips.
        trend_h = int((_CONTENT_W - 32) * (120 * len(series)) / 800) + 32
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "chart",
                "frame": {"x": _MARGIN, "y": b.y, "w": _CONTENT_W, "h": trend_h, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "chart": {"kind": "trend", "series": series},
                # Literal white under every theme — see the geo card note.
                "style": {"bg_color": "#ffffff", "radius": 14},
            }
        )
        b.y += trend_h + _GAP


def convert_dashboard_to_canvas(source: dict, theme_ref: str | None = None, visible_sections=None) -> tuple[dict, list[dict]]:
    """Convert an echarts dashboard ``_source`` into ``(canvas_config, skipped)``.

    Every convertible section/column becomes a LIVE canvas object (binding
    preserved, no baked values); anything unconvertible becomes a visible
    placeholder object plus a ``skipped`` entry — never a silent drop.

    ``theme_ref`` names the built-in theme to seed in: the page
    carries the theme key, style fields carry token KEYS resolved against it
    client-side, and key-less colors (chart series, funnel bars, the page
    background) are resolved from it here. Unknown/missing refs fall back to
    classic — the pre-theme look, verbatim.
    """
    b = _Builder(theme_ref)
    charts = (source.get("echarts_config") or {}).get("charts") or []
    if isinstance(charts, dict):
        charts = list(charts.values())

    for chart in charts:
        if not isinstance(chart, dict):
            continue
        chart_type = chart.get("chart_type")
        if chart_type == "canva":
            _convert_canva(b, chart, visible_sections=visible_sections)
        elif chart_type == "grouped_table":
            _convert_grouped_table(b, chart)
        elif chart_type == "table":
            # a legacy aggregation table becomes a first-class
            # table object: config copied verbatim, rendered live by the
            # REAL charts.js renderTable on every canvas surface.
            _convert_table(b, chart)
        elif chart_type in _RPT_SEED_PLATFORM:
            _convert_impact_report(b, chart)
        else:
            label = chart.get("title") or chart.get("table_title") or chart_type or "chart"
            reason = f"chart_type '{chart_type}' can't be converted to canvas objects yet"
            b.skip(chart.get("chart_id", "?"), chart_type or "?", f"chart '{label}'", reason)
            b.placeholder(str(label), reason + ".", chart_type or "?")
        b.y += _GAP

    page_h = min(max(b.y + _MARGIN, _MIN_PAGE_H), _MAX_PAGE_H)
    canvas_config = {
        "schema_version": 1,
        "page": {
            "width": _PAGE_W,
            "height": page_h,
            "orientation": "landscape" if _PAGE_W >= page_h else "portrait",
            "background": {"type": "color", "value": b.page_bg},
            "grid": {"size": 20, "show": False, "snap": False},
            "margins": {"top": _MARGIN, "right": _MARGIN, "bottom": _MARGIN, "left": _MARGIN, "show": False},
            "rulers": {"show": True},
            "guides": {"h": [], "v": [], "show": True},
            "theme": b.theme_key,  # ref picker choice
        },
        "objects": b.objects,
        # follow-up: COPY-on-seed includes the source dashboard's
        # dashboard-level filters (canvas uses the identical
        # shape) so the seeded page filters exactly like the report
        # it mirrors, including the author's saved selections. Deep-copied so
        # later edits on the canvas page never mutate the source dashboard.
        "filters": copy.deepcopy((source.get("echarts_config") or {}).get("filters") or []),
    }
    return canvas_config, b.skipped


# ── build a canvas page from a VALIDATED AI proposal ─────────────
# Input is the clean tile structure canvas_ai_service.validate_proposal
# returns — never the raw tool input. The model speaks in tiles; this module
# owns the geometry (same _Builder the report seeds use), so AI output can't
# place, size, or style anything directly.

# Stat-tile look — the proven grouped_table cell styling (colored label strip
# over a white value cell). the theme's stat_tile component variant,
# resolved to concrete values (identical dict to the pre-theme literal).
# the NO-THEME fallback only — a themed build emits the variant's
# token KEYS instead (client-resolved against page.theme, like the seeds).
_AI_STAT_STYLE = canvas_theme.stat_tile_style()
# Trend-series fallback palette (the classic chart-series colors), used when
# the proposal doesn't carry a validated per-series color. themed
# builds pull the chosen theme's palette via b.series (identical here).
_AI_TREND_COLORS = _SERIES
_AI_GEO_H = 380
_AI_BAR_H = 340  # full-width bar chart tile height
_AI_TEXT_TILE_H = 64

# table-tile title bar: the grouped_table title look with its
# default colors (_convert_grouped_table's title_bg/header_text defaults).
# the colors are the theme's header component variant. :
# the no-theme fallback — themed builds emit the variant's token keys.
_AI_TABLE_TITLE_STYLE = {
    "font_size": 18,
    "bold": True,
    "align": "center",
    "color": canvas_theme.resolve_color("header_text"),
    "bg_color": canvas_theme.resolve_color("header_bg"),
}


def _proposal_stat_body(tile: dict, stat_style: dict) -> dict:
    style = dict(stat_style)
    style["format"] = tile.get("format") or "number"
    return {"label": tile.get("label") or "Metric", "style": style, "binding": dict(tile["binding"])}


def _proposal_table(b: _Builder, tile: dict, section_id: str, stat_style: dict, title_style: dict) -> None:
    """render a validated table tile in the grouped_table look:
    a full-width colored title bar, then the columns as flush bordered
    table_cell data fields (colored label strip over a white value cell),
    _CELLS_PER_ROW per row — same geometry as _convert_grouped_table."""
    b.text(
        tile.get("label") or "Summary Table",
        h=_TITLE_BAR_H,
        style=dict(title_style),
        role="title",
        section_id=section_id,
        advance=False,
    )
    b.y += _TITLE_BAR_H
    cols = tile.get("columns") or []
    for i, col in enumerate(cols):
        style = dict(stat_style)
        # The compact 66px table cell takes the grouped_table value size, not
        # the 96px stat tile's.
        style["value_font_size"] = 16
        style["format"] = col.get("format") or "number"
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "data_field",
                "frame": {
                    "x": _MARGIN + (i % _CELLS_PER_ROW) * _CELL_W,
                    "y": b.y + (i // _CELLS_PER_ROW) * _CELL_H,
                    "w": _CELL_W,
                    "h": _CELL_H,
                    "rotation": 0,
                },
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "label": col.get("label") or "Metric",
                "style": style,
                "binding": dict(col["binding"]),
            }
        )
    rows = (len(cols) + _CELLS_PER_ROW - 1) // _CELLS_PER_ROW
    b.y += rows * _CELL_H + _GAP


def _proposal_chart(b: _Builder, tile: dict, section_id: str) -> None:
    if tile["type"] == "geo_chart":
        binding = tile["binding"]
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "chart",
                "frame": {"x": _MARGIN, "y": b.y, "w": _CONTENT_W, "h": _AI_GEO_H, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "chart": {
                    "kind": "geo",
                    "statistic": "reach",
                    "show_list": True,
                    "geo": {
                        "data_view": binding["data_view"],
                        "field": binding["field"],
                        "aggregation": binding["aggregation"],
                        "x_field": tile["x_field"],
                        # geo.filters is passed RAW into the query
                        # config by the renderer, so validated {field, value}
                        # binding filters map to eq clauses here.
                        "filters": [
                            {"field": f["field"], "op": "eq", "value": f["value"]} for f in binding.get("filters") or []
                        ],
                    },
                },
                # Chart cards stay literal white under every theme
                # — see the geo card note in _convert_impact_report.
                "style": {"bg_color": "#ffffff", "radius": 14},
            }
        )
        b.y += _AI_GEO_H + _GAP
        return
    if tile["type"] == "bar_chart":
        # grouped/split column chart. Series carry the same
        # stat-grade binding shape as trend; x_field is the category axis,
        # split_field (single-series) becomes the renderer's breakdown, and
        # axis "right" puts a series on a second scale.
        bar_series = []
        for i, s in enumerate(tile["series"]):
            entry = {
                "id": f"bar-{i + 1}",
                "label": s["label"],
                # fallback colors come from the build theme's chart
                # palette (b.series == _AI_TREND_COLORS when no theme rides
                # in); a validated model-proposed color still wins, so a
                # user-requested "make it red" survives theming.
                "color": s.get("color") or b.series[i % len(b.series)],
                "data_view": s["data_view"],
                "field": s["field"],
                "aggregation": s["aggregation"],
            }
            if s.get("equals_value") is not None:
                entry["equals_value"] = s["equals_value"]
            if s.get("filters"):
                entry["filters"] = [{"field": f["field"], "value": f["value"]} for f in s["filters"]]
            if s.get("axis") == "right":
                entry["axis"] = "right"
            bar_series.append(entry)
        chart = {"kind": "bar", "x_field": tile["x_field"], "series": bar_series}
        if tile.get("split_field"):
            chart["split_field"] = tile["split_field"]
        b.add(
            {
                "object_id": b.next_id(),
                "object_type": "chart",
                "frame": {"x": _MARGIN, "y": b.y, "w": _CONTENT_W, "h": _AI_BAR_H, "rotation": 0},
                "locked": False,
                "group_id": None,
                "section_id": section_id,
                "chart": chart,
                "style": {"bg_color": "#ffffff", "radius": 14},
            }
        )
        b.y += _AI_BAR_H + _GAP
        return
    series = []
    for i, s in enumerate(tile["series"]):
        entry = {
            "id": f"trend-{i + 1}",
            "label": s["label"],
            # theme palette fallback; see the bar-series note.
            "color": s.get("color") or b.series[i % len(b.series)],
            "data_view": s["data_view"],
            "field": s["field"],
            "aggregation": s["aggregation"],
        }
        if s.get("equals_value") is not None:
            entry["equals_value"] = s["equals_value"]
        series.append(entry)
    # Same frame-height formula as the impact-report trends: the renderer
    # draws an 800-wide viewBox with 120px rows scaled to the frame width.
    trend_h = int((_CONTENT_W - 32) * (120 * len(series)) / 800) + 32
    b.add(
        {
            "object_id": b.next_id(),
            "object_type": "chart",
            "frame": {"x": _MARGIN, "y": b.y, "w": _CONTENT_W, "h": trend_h, "rotation": 0},
            "locked": False,
            "group_id": None,
            "section_id": section_id,
            "chart": {"kind": "trend", "series": series},
            "style": {"bg_color": "#ffffff", "radius": 14},
        }
    )
    b.y += trend_h + _GAP


def build_canvas_from_proposal(proposal: dict, theme_ref: str | None = None) -> dict:
    """Convert a validated AI dashboard proposal into a ``canvas_config``.

    Layout mirrors the report seeds: a page title, then per section a header
    (+ optional subheading) followed by that section's tiles — stat tiles in
    the standard 4-per-row grid, text tiles full-width, charts full-width,
    table tiles as a title bar over a flush table_cell grid.
    The result is UNSAVED (preview-before-apply, the private docs
    2026-07-14): persistence only ever happens through the normal
    user-confirmed save route, which re-validates the whole payload.

    ``theme_ref`` is the open page's selected theme, threaded in
    from ``/api/ai/chat`` — theming stays server-side; the model never sees
    or invents colors. A NON-classic theme swaps the fixed _AI_* look for
    the theme's component variants emitted as token KEYS (client-resolved
    against ``page.theme``, the report-seed contract) plus its chart palette
    and paper backdrop. None / ``classic`` / unknown refs reproduce the
    earlier output byte-for-byte (back-compat AC).
    """
    b = _Builder(theme_ref)
    themed = b.theme_key != canvas_theme.DEFAULT_THEME_KEY
    if themed:
        components = b.theme.get("components") or {}
        # Token-KEY dicts straight off the theme's component variants —
        # the same keys the report seeds emit (_convert_grouped_table).
        stat_style = copy.deepcopy(components.get("stat_tile") or {})
        table_title_style = {"font_size": 18, "bold": True, "align": "center", **(components.get("header") or {})}
        # Free text carries the ink KEY so it resolves readable on a dark
        # page (the legacy path leaves it to the CSS default, kept below).
        title_style = {"font_size": 26, "bold": True, "color": "ink"}
        text_style = {"font_size": 14, "color": "ink"}
        # Page backdrop behind the cards — the theme's paper, resolved
        # concrete (the page-settings color input can't display a key).
        b.page_bg = canvas_theme.resolve_color("paper", b.theme)
    else:
        stat_style = dict(_AI_STAT_STYLE)
        table_title_style = dict(_AI_TABLE_TITLE_STYLE)
        title_style = {"font_size": 26, "bold": True}
        text_style = {"font_size": 14}
        b.page_bg = "#f0f2f5"

    b.text(
        proposal.get("title") or "AI Dashboard",
        h=_TITLE_H,
        style=dict(title_style),
        role="title",
    )

    for section in proposal.get("sections", []):
        section_id = b.next_section()
        _section_header(b, section.get("heading") or "Section", section.get("subheading") or None, section_id)
        stat_buffer: list[dict] = []

        def _flush_stats(buffer: list[dict] = stat_buffer, sec: str = section_id) -> None:
            if buffer:
                b.tiles([_proposal_stat_body(t, stat_style) for t in buffer], sec)
                buffer.clear()

        for tile in section.get("tiles", []):
            if tile["type"] == "stat":
                stat_buffer.append(tile)
                continue
            _flush_stats()
            if tile["type"] == "text":
                b.text(tile.get("text") or "", h=_AI_TEXT_TILE_H, style=dict(text_style), section_id=section_id)
            elif tile["type"] in ("trend_chart", "geo_chart", "bar_chart"):
                _proposal_chart(b, tile, section_id)
            elif tile["type"] == "table":
                _proposal_table(b, tile, section_id, stat_style, table_title_style)
        _flush_stats()
        b.y += _GAP

    page_h = min(max(b.y + _MARGIN, _MIN_PAGE_H), _MAX_PAGE_H)
    return {
        "schema_version": 1,
        "page": {
            "width": _PAGE_W,
            "height": page_h,
            "orientation": "landscape" if _PAGE_W >= page_h else "portrait",
            "background": {"type": "color", "value": b.page_bg},
            "grid": {"size": 20, "show": False, "snap": False},
            "margins": {"top": _MARGIN, "right": _MARGIN, "bottom": _MARGIN, "left": _MARGIN, "show": False},
            "rulers": {"show": True},
            "guides": {"h": [], "v": [], "show": True},
            "theme": b.theme_key,  # ref; the AI honors it
        },
        "objects": b.objects,
        "filters": [],
    }

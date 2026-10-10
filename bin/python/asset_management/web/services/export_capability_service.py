"""
Export-capability resolver.

Single source of truth for "which export formats can this dashboard or
report type actually produce?". Before this module the only type knowledge
was an inline ``not in ("echarts",)`` check in the export endpoint plus an
``IMPACT_REPORT_CHART_TYPES`` frozenset duplicated in two modules — and the
UI offered Excel/CSV/PDF unconditionally, so graphical-only dashboards
surfaced a generic 400 only after the user picked a format.

Capability model:

* ``canvas`` / ``leaflet_map`` (and any unknown ``dashboard_type``) —
  PDF-only. PDF is browser-print of whatever the viewer iframe renders, so
  it works for free-form/graphical content; there is no tabular data to
  put in an .xlsx or .csv.
* ``echarts`` — DERIVED from chart membership: all three formats when at
  least one member chart can produce a table in the export writer
  (``routes.export_dashboard``), else PDF-only. "Summary" is an echarts
  template, and Impact Reports are chart types WITHIN an echarts
  dashboard — neither is a dashboard type, so the echarts entry can't be
  static.
* ``report:*`` synthetic ids (report scheduler) — always tabular; their
  builders emit ``data.tables`` directly (report_types_service).

The chart-membership check is a DENY-list (``NON_TABULAR_CHART_TYPES``)
rather than an allow-list: every chart type not known to be render-only
flows through the generic ``build_query`` → ``transform`` →
``_chart_data_to_table`` export path, which handles any series-shaped
result. A future series chart type therefore stays exportable without
editing this map, while the known client-render-only types are pinned
here. Keep the deny-list in sync with the export writer: a chart type
belongs here exactly when the writer can never produce columns for it.

Pure stdlib on purpose — imported by the Flask app (dashboard_builder
routes, dashboard_service), the report-scheduler blueprint, AND the
headless cron runner (report_schedule_service / report_email_service),
which must not drag Flask or ES helpers in.
"""

from __future__ import annotations

FORMAT_XLSX = "xlsx"
FORMAT_CSV = "csv"
FORMAT_PDF = "pdf"

ALL_EXPORT_FORMATS: tuple[str, ...] = (FORMAT_XLSX, FORMAT_CSV, FORMAT_PDF)
PDF_ONLY_FORMATS: tuple[str, ...] = (FORMAT_PDF,)

# Impact Report chart types (report / google_report /
# combined_report) render entirely client-side in charts.js
# (renderReportChart) and carry none of the fields build_query expects;
# the snapshot precompute skips them and links out instead. Canonical
# definition — dashboard_builder.routes and report_schedule_service
# import it from here (they carried drifting copies).
IMPACT_REPORT_CHART_TYPES = frozenset({"report", "google_report", "combined_report"})

# Chart types the export writer can NEVER turn into a table:
# * Impact Report types — client-render-only (above).
# * canva — an embedded Canva design; no query at all.
# * profile — a static profile card; no query at all.
# * posts_table — raw FB post hits fetched by a dedicated endpoint
#   (_run_posts_table_query); the export loop's generic transform path
#   yields no series/columns for its config shape.
NON_TABULAR_CHART_TYPES = IMPACT_REPORT_CHART_TYPES | frozenset({"canva", "profile", "posts_table"})


def chart_is_tabular(chart_type: str | None) -> bool:
    """True when a chart of this type can contribute a sheet to an export."""
    return (chart_type or "") not in NON_TABULAR_CHART_TYPES


def _iter_chart_types(source: dict):
    cfg = (source or {}).get("echarts_config") or {}
    charts = cfg.get("charts") or []
    if isinstance(charts, dict):
        charts = list(charts.values())
    for chart in charts:
        if isinstance(chart, dict):
            yield chart.get("chart_type", "")


def dashboard_export_formats(source: dict) -> tuple[str, ...]:
    """Supported export formats for a dashboard doc's ``_source``.

    ``echarts`` derives from chart membership; every other dashboard_type
    (canvas, leaflet_map, and anything future/unknown) is PDF-only.
    """
    dashboard_type = (source or {}).get("dashboard_type") or "echarts"
    if dashboard_type != "echarts":
        return PDF_ONLY_FORMATS
    if any(chart_is_tabular(t) for t in _iter_chart_types(source)):
        return ALL_EXPORT_FORMATS
    return PDF_ONLY_FORMATS


def report_type_export_formats(report_id: str) -> tuple[str, ...]:
    """Supported formats for a synthetic ``report:*`` id — always tabular
    (their builders emit ``data.tables`` directly)."""
    del report_id  # every registered report type is tabular today
    return ALL_EXPORT_FORMATS


def tabular_unsupported_reason(source: dict) -> str:
    """Human-readable reason a dashboard can't export Excel/CSV — used by
    the server-side guard so a direct API call gets a message naming the
    type-specific cause instead of a generic string."""
    dashboard_type = (source or {}).get("dashboard_type") or "echarts"
    if dashboard_type == "canvas":
        return "Canvas dashboards export to PDF only — they have no tabular chart data for Excel/CSV."
    if dashboard_type == "leaflet_map":
        return "Map dashboards export to PDF only — they have no tabular chart data for Excel/CSV."
    if dashboard_type != "echarts":
        return f"'{dashboard_type}' dashboards export to PDF only."
    return (
        "This dashboard contains only graphical charts (Impact Reports, Canva embeds, profiles), "
        "which have no tabular data — export it as PDF instead."
    )

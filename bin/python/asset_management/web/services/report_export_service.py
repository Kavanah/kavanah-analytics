"""
Report Export Service — PDF and Excel generation for Ask Kavanah reports.
Receives a list of saved query result sections and generates downloadable files.
"""

import csv
import io
import json
import logging
from typing import Any

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

try:
    from fpdf import FPDF
except ImportError:
    FPDF = None  # type: ignore[assignment, misc]


# Unicode → ASCII replacements for PDF (Helvetica doesn't support these)
_UNICODE_REPLACEMENTS = {
    "\u2191": "^",  # ↑
    "\u2193": "v",  # ↓
    "\u2192": "->",  # →
    "\u2190": "<-",  # ←
    "\u2014": "--",  # —
    "\u2013": "-",  # –
    "\u2018": "'",  # '
    "\u2019": "'",  # '
    "\u201c": '"',  # "
    "\u201d": '"',  # "
    "\u2026": "...",  # …
    "\u00d7": "x",  # ×
    "\u2022": "*",  # •
    "\u2713": "[x]",  # ✓
    "\u2717": "[ ]",  # ✗
    "\u00b7": ".",  # ·
}


def _sanitize_for_pdf(text: str) -> str:
    """Replace unicode characters that Helvetica can't render with ASCII equivalents."""
    for uni, ascii_rep in _UNICODE_REPLACEMENTS.items():
        text = text.replace(uni, ascii_rep)
    # Strip any remaining non-latin1 characters
    return text.encode("latin-1", errors="replace").decode("latin-1")


# ── full-row hydration ────────────────────────────────────────────────
#
# Ask Kavanah table rows are transcribed into the structured response by the
# LLM, which only ever sees the first 10 hits of a large ES response — the NLQ
# service trims raw hits to 10 to stay within token limits
# (nlq_service._execute_tool). A report section therefore stores at most ~10
# rows, and the export faithfully writes those 10. Aggregation-backed tables are
# unaffected (aggregations are never trimmed), so this only bites raw-hits
# tables ("list the posts/contacts/...").
#
# Fix: at export time, for sections backed by a raw-hits query, re-run the
# stored query with no truncation and rebuild every row deterministically from
# `_source`. Anything we can't confidently rebuild (aggregation queries, missing
# query/index, column/field shape mismatch, ES error, empty result) falls
# through to the LLM-provided rows unchanged — the export never regresses.
#
# SECURITY: the client supplies `es_index` / `es_query` in the export payload,
# so the re-query MUST NOT be trusted to carry scope. We re-apply the same two
# Layer-1/Layer-2 controls every /api/query read uses before executing: the
# index pattern is rewritten through scoped_index_pattern() (a forged "*" or
# foreign index collapses to the user's explicit allowed set, or the no-match
# sentinel), and an allow-only team_title filter is appended to the body. Both
# read scope from the Flask session, so this is only sound while the export
# runs in-request (it does — export_excel/pdf are called synchronously from the
# route). The empty-scope sentinel returns zero hits and falls through to the
# LLM rows.

# ES default max_result_window. Staying at/under it avoids needing
# search_after/scroll for a feature that exports report tables, not bulk dumps.
_EXPORT_ROW_CAP = 10000


def _hits_source_fields(es_query: Any) -> list[str] | None:
    """Return the `_source` field list iff es_query is a raw-hits query.

    Returns None for aggregation queries (size:0 or carrying aggs — their LLM
    rows are already complete) and for anything without a usable `_source`
    projection list.
    """
    if not isinstance(es_query, dict):
        return None
    if es_query.get("aggs") or es_query.get("aggregations"):
        return None
    if es_query.get("size") == 0:
        return None
    src = es_query.get("_source")
    if isinstance(src, list) and src and all(isinstance(f, str) for f in src):
        return src
    return None


def _scope_filtered_query(es_query: dict, allowed_teams: list[str] | None) -> dict:
    """Return a copy of ``es_query`` whose ``query`` is constrained to rows
    whose ``team_title`` is in ``allowed_teams`` (Layer-2 defense-in-depth).

    ``es_query`` is RAW Elasticsearch DSL (the body the NLQ service executed),
    NOT the app's intermediate ``{filters: [...]}`` shape — so the scope clause
    is emitted as real DSL: the existing query is wrapped in a ``bool`` whose
    ``filter`` pins ``team_title`` via a ``should`` of ``match_phrase`` over the
    allowed titles (mirroring es_query_builder's ``match_phrase_any`` on the
    bare text field).

    ``allowed_teams is None`` is the admin / unrestricted path — return the
    query unchanged. An empty list pins to a clause that matches nothing, so a
    scoped user with no resolvable teams sees no rows.
    """
    if allowed_teams is None:
        return es_query

    inner = es_query.get("query") or {"match_all": {}}
    scope_clause = {
        "bool": {
            "should": [{"match_phrase": {"team_title": t}} for t in allowed_teams],
            "minimum_should_match": 1,
        }
    }
    scoped = dict(es_query)
    scoped["query"] = {"bool": {"must": [inner], "filter": [scope_clause]}}
    return scoped


def _source_value(src: dict, field: str) -> Any:
    """Read a possibly-dotted field path from an ES `_source` dict.

    Tries the flat key first (mappings sometimes store dotted keys verbatim),
    then walks the nested structure. Missing paths return ""."""
    if field in src:
        return src[field]
    cur: Any = src
    for part in field.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return cur


def _stringify_cell(value: Any) -> Any:
    """Coerce an ES value into something the writers can render cleanly."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return value


def hydrate_full_rows(sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand raw-hits report tables to their full row set before export.

    See the module note above. Returns a new section list; sections
    that don't qualify are passed through untouched.
    """
    try:
        from lib.es_helpers import get_elasticsearch_client

        try:
            from web.services.role_filter_service import RoleFilterService
        except ImportError:
            from asset_management.web.services.role_filter_service import RoleFilterService
    except ImportError:  # pragma: no cover - defensive: export still works on LLM rows
        return sections

    es = None
    out: list[dict[str, Any]] = []
    for section in sections:
        es_query = section.get("es_query")
        es_index = section.get("es_index")
        source_fields = _hits_source_fields(es_query)
        headers = (section.get("table") or {}).get("headers") or []

        # Only override when we can map _source fields 1:1 onto the displayed
        # columns; otherwise the rebuilt rows might not line up with headers.
        if not (es_index and source_fields and len(source_fields) == len(headers)):
            if source_fields and len(source_fields) != len(headers):
                logging.info(
                    "skipping export re-query — %d _source fields vs %d headers",
                    len(source_fields),
                    len(headers),
                )
            out.append(section)
            continue

        if es is None:
            es = get_elasticsearch_client()
        if es is None:
            out.append(section)
            continue

        # SECURITY (see module note): never trust the client-supplied index or
        # query to be scoped. Layer 1 — rewrite the index to the caller's
        # allowed set (a forged "*"/foreign index collapses to the explicit
        # allowed indices, or the no-match sentinel). Layer 2 (defense-in-depth
        # for any multi-team index) — constrain the query body to the caller's
        # allowed team_titles. Both read scope from the Flask session and apply
        # exactly as /api/query does.
        scoped_index = RoleFilterService.scoped_index_pattern(es_index)
        scoped_query = _scope_filtered_query(es_query, RoleFilterService.get_allowed_teams_for_org_users())
        body = {**scoped_query, "size": _EXPORT_ROW_CAP}
        try:
            resp = es.search(index=scoped_index, body=body, ignore_unavailable=True)
            resp = resp.body if hasattr(resp, "body") else dict(resp)
        except Exception:
            logging.exception("export re-query failed for index %s", es_index)
            out.append(section)
            continue

        hits = (resp.get("hits") or {}).get("hits") or []
        rows = [[_stringify_cell(_source_value(hit.get("_source") or {}, f)) for f in source_fields] for hit in hits]
        # An empty re-query result (scope drift, transient issue) shouldn't wipe
        # the rows the user actually saw — keep the LLM rows in that case.
        if not rows:
            out.append(section)
            continue

        table = dict(section.get("table") or {})
        table["rows"] = rows
        out.append({**section, "table": table})

    return out


def export_excel(report_name: str, sections: list[dict[str, Any]]) -> io.BytesIO:
    """Generate an Excel workbook from saved report sections.

    Each section becomes a worksheet with:
    - Row 1: query text (bold)
    - Row 2: narrative summary
    - Row 4+: table data (headers + rows)

    Args:
        report_name: Title for the workbook.
        sections: List of dicts with keys: query, narrative, table {headers, rows}.

    Returns:
        BytesIO buffer containing the .xlsx file.
    """
    sections = hydrate_full_rows(sections)  # full rows, not the LLM's 10
    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # type: ignore[arg-type]

    header_font = Font(bold=True, size=12)
    narrative_font = Font(italic=True, size=10, color="5F6B7A")
    table_header_font = Font(bold=True, size=10)
    table_header_fill = PatternFill(start_color="E8F4FD", end_color="E8F4FD", fill_type="solid")
    wrap_align = Alignment(wrap_text=True, vertical="top")

    for i, section in enumerate(sections):
        # Sheet name: truncated query or "Section N"
        query = section.get("query", f"Section {i + 1}")
        sheet_name = query[:28] + "..." if len(query) > 31 else query
        # Clean sheet name of invalid chars
        for ch in "[]:*?/\\":
            sheet_name = sheet_name.replace(ch, "_")
        ws = wb.create_sheet(title=sheet_name)

        # Row 1: Query
        # (SVC-104): every cell that can carry a third-party-influenced
        # string routes through _csv_safe — openpyxl treats a string starting
        # with '=' as a live FORMULA (data_type='f'), so this is baked-in
        # execution, not just open-time interpretation.
        ws.cell(row=1, column=1, value=_csv_safe(query)).font = header_font

        # Row 2: Narrative
        narrative = section.get("narrative", "")
        # Strip markdown bold markers
        narrative = narrative.replace("**", "")
        ws.cell(row=2, column=1, value=_csv_safe(narrative)).font = narrative_font
        ws.cell(row=2, column=1).alignment = wrap_align

        # Row 4+: Table
        table = section.get("table", {})
        headers = table.get("headers", [])
        rows = table.get("rows", [])

        if headers:
            for col_idx, header in enumerate(headers, start=1):
                cell = ws.cell(row=4, column=col_idx, value=_csv_safe(header))
                cell.font = table_header_font
                cell.fill = table_header_fill

            for row_idx, row in enumerate(rows, start=5):
                for col_idx, value in enumerate(row, start=1):
                    ws.cell(row=row_idx, column=col_idx, value=_csv_safe(str(value)))

        # Auto-width columns
        for col in ws.columns:
            max_length = 0
            for cell in col:
                if cell.value:
                    max_length = max(max_length, len(str(cell.value)))
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[col[0].column_letter].width = adjusted_width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# (SVC-010): CSV formula injection (CWE-1236). A cell whose first
# character is one of = + - @ (or a leading tab / carriage return) is executed as
# a formula by Excel / Google Sheets when the file is opened. The CSV is written
# utf-8-sig specifically so Excel opens it, so neutralize before writing: prefix
# any such string value with a single quote so it renders as literal text.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> Any:
    """Neutralize CSV formula injection by prefixing a leading formula trigger
    with a single quote. Non-string values (ints/floats/etc.) pass through
    unchanged so genuine numbers are not mangled."""
    if isinstance(value, str) and value and value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


# public alias so other export surfaces (dashboard_builder's
# export_dashboard) reuse THIS neutralizer instead of growing a local copy —
# one escaper, one behavior (same rule as the shared escapeHtml lesson).
csv_safe = _csv_safe


def export_csv(report_name: str, sections: list[dict[str, Any]]) -> io.BytesIO:
    """Generate a CSV from saved report sections.

    A single CSV can't carry multiple sheets, so sections are stacked: a title
    row per section, the narrative (if any), the table header + rows, then a
    blank separator. Encoded utf-8-sig so Excel opens it with the right encoding.

    Args:
        report_name: report title (first line of the file).
        sections: list of {query, narrative, table:{headers, rows}} (the same
                  shape export_excel consumes).

    Returns:
        BytesIO buffer containing the .csv file.
    """
    text_buf = io.StringIO()
    writer = csv.writer(text_buf)
    writer.writerow([_csv_safe(report_name)])
    writer.writerow([])

    for section in sections:
        title = section.get("query", "")
        if title:
            writer.writerow([_csv_safe(title)])
        narrative = (section.get("narrative") or "").replace("**", "")
        if narrative:
            writer.writerow([_csv_safe(narrative)])
        table = section.get("table") or {}
        headers = table.get("headers") or []
        rows = table.get("rows") or []
        if headers:
            writer.writerow([_csv_safe(h) for h in headers])
        for row in rows:
            writer.writerow([_csv_safe("" if v is None else v) for v in row])
        writer.writerow([])  # blank separator between sections

    buf = io.BytesIO(text_buf.getvalue().encode("utf-8-sig"))
    buf.seek(0)
    return buf


def export_pdf(report_name: str, sections: list[dict[str, Any]]) -> io.BytesIO:
    """Generate a PDF report from saved report sections.

    Each section includes:
    - Query heading
    - Narrative paragraph
    - Stat highlights
    - Table data

    Args:
        report_name: Title for the PDF document.
        sections: List of dicts with keys: query, narrative, stats, table {headers, rows}.

    Returns:
        BytesIO buffer containing the .pdf file.
    """
    if FPDF is None:
        raise ImportError("fpdf2 is required for PDF export. Install with: pip install fpdf2")

    sections = hydrate_full_rows(sections)  # full rows, not the LLM's 10
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)

    # Title page
    report_name = _sanitize_for_pdf(report_name)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 20, report_name, new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(127, 137, 152)
    pdf.cell(0, 8, "Generated by Ask Kavanah", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(10)

    for i, section in enumerate(sections):
        if i > 0:
            pdf.add_page()

        query = _sanitize_for_pdf(section.get("query", f"Section {i + 1}"))
        narrative = _sanitize_for_pdf(section.get("narrative", ""))
        narrative = narrative.replace("**", "")
        stats = section.get("stats", [])
        table = section.get("table", {})

        # Section heading
        pdf.set_text_color(0, 7, 22)
        pdf.set_font("Helvetica", "B", 14)
        pdf.multi_cell(0, 7, query, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

        # Narrative
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(30, 41, 59)
        pdf.multi_cell(0, 5, narrative, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        # Stats row
        if stats:
            pdf.set_font("Helvetica", "B", 9)
            col_width = (pdf.w - 20) / min(len(stats), 4)
            x_start = pdf.get_x()
            y_start = pdf.get_y()

            for j, stat in enumerate(stats[:4]):
                x = x_start + j * col_width
                pdf.set_xy(x, y_start)

                # Stat label
                pdf.set_text_color(127, 137, 152)
                pdf.set_font("Helvetica", "", 8)
                pdf.cell(col_width, 4, _sanitize_for_pdf(str(stat.get("label", ""))), new_x="LMARGIN", new_y="NEXT")

                pdf.set_xy(x, y_start + 4)
                pdf.set_text_color(0, 7, 22)
                pdf.set_font("Helvetica", "B", 12)
                pdf.cell(col_width, 6, _sanitize_for_pdf(str(stat.get("value", ""))), new_x="LMARGIN", new_y="NEXT")

                pdf.set_xy(x, y_start + 10)
                direction = stat.get("direction", "neutral")
                if direction == "up":
                    pdf.set_text_color(3, 127, 12)
                elif direction == "down":
                    pdf.set_text_color(217, 21, 21)
                else:
                    pdf.set_text_color(127, 137, 152)
                pdf.set_font("Helvetica", "", 8)
                pdf.cell(col_width, 4, _sanitize_for_pdf(str(stat.get("change", ""))), new_x="LMARGIN", new_y="NEXT")

            pdf.set_y(y_start + 18)
            pdf.ln(3)

        # Table
        headers = table.get("headers", [])
        rows = table.get("rows", [])

        if headers:
            n_cols = len(headers)
            available_width = pdf.w - 20
            col_w = available_width / n_cols

            # Header row
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_fill_color(232, 244, 253)
            pdf.set_text_color(95, 107, 122)
            for header in headers:
                pdf.cell(col_w, 6, _sanitize_for_pdf(str(header))[:20], border=1, fill=True)
            pdf.ln()

            # Data rows
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(0, 7, 22)
            for row in rows:
                for cell in row:
                    pdf.cell(col_w, 5, _sanitize_for_pdf(str(cell))[:20], border=1)
                pdf.ln()

    buf = io.BytesIO()
    pdf.output(buf)
    buf.seek(0)
    return buf

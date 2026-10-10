"""Summary Report DT scorecards on the by-team pull index +
builder exists-aggregation visibility.

After the push→pull cutover, the Summary Report's DT scorecards read
0 on prod: the metric definitions queried `post_type.keyword`, but the pull
schema (unified_schema_disciple_tools.json) maps top-level `post_type` as a
BARE keyword with no `.keyword` subfield — `term post_type.keyword=contacts`
matched nothing while `term post_type=contacts` matched 305. Confirmed live
on prod 2026-06-12; a hand-corrected prod config validated the diagnosis.

Pins (Part 1 — field paths, all three Summary Report surfaces):
  1. create_summary_report.py Jesus-journey columns use bare `post_type`;
     meta.* paths keep `.keyword` (text+keyword on the pull schema, so those
     never broke — Baptisms worked while New Contacts read 0, which masked
     the pattern).
  2. charts.js summary_report metric defaults — same rule.
  3. summary_report.html standalone-page metric defaults — same rule.

Pins (Part 2 — builder UX):
  4. Both aggregation dropdowns (standard table editor + grouped_table column
     editor) list 'exists'. Without it, a stored exists column ("New
     Churches") displayed as "sum" and any touch of the dropdown silently
     overwrote the aggregation.
  5. The summary_report chart-type metrics editor (builder.js
     _renderCanvaMetricsEditor over _CANVA_SECTIONS) has an Equals Value
     input persisted to metric_overrides, charts.js _m consumes the
     override, and the editor's Jesus-journey DEFAULTS use bare post_type —
     the defaults pre-fill the form, so a stale .keyword there re-broke a
     metric the moment an author touched its card (found by a teammate on staging,
     2026-06-12).
"""

import os
import re
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

import create_summary_report

_CHARTS_JS = os.path.join(_PYTHON_DIR, "asset_management", "web", "static", "dashboard_builder", "charts.js")
_BUILDER_JS = os.path.join(_PYTHON_DIR, "asset_management", "web", "static", "dashboard_builder", "builder.js")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _journey_columns():
    groups = create_summary_report.chart_config["groups"]
    journey = next(g for g in groups if "Jesus journey" in g["label"])
    return {c["label"]: c for c in journey["columns"]}


# ===========================================================================
# 1. Report-seeder Jesus-journey columns query fields the pull schema maps
# ===========================================================================

class TestReportSeederDtColumns:
    def test_post_type_columns_use_bare_field(self):
        """should query bare post_type — the pull schema has no .keyword
        subfield on it, so the .keyword path silently counted 0."""
        cols = _journey_columns()

        assert cols["New Contacts"]["data_sources"][0]["field"] == "post_type"
        assert cols["New Groups"]["data_sources"][0]["field"] == "post_type"
        assert cols["New Contacts"]["equals_value"] == "contacts"
        assert cols["New Groups"]["equals_value"] == "groups"

    def test_meta_columns_keep_keyword_paths(self):
        """meta.* fields are text+keyword on the pull schema — their .keyword
        paths are correct and must not be 'fixed' to bare text fields."""
        cols = _journey_columns()

        assert cols["1st Meetings"]["data_sources"][0]["field"] == "meta.seeker_path.keyword"
        assert cols["Baptisms"]["data_sources"][0]["field"] == "meta.milestones.keyword"
        assert cols["New Churches"]["data_sources"][0]["field"] == "meta.church_start_date"
        assert cols["New Churches"]["aggregation"] == "exists"

    def test_dt_data_view_is_team_keyed(self):
        cols = _journey_columns()
        assert cols["New Contacts"]["data_sources"][0]["data_view"] == "*_disciple_tools"


# ===========================================================================
# 6. Seeder dedup lookup survives a bare-keyword title mapping
# ===========================================================================

class TestSeederDedupQuery:

    def test_existing_report_lookup_matches_both_title_spellings(self):
        """should find the existing dashboard whether `title` is mapped as a
        bare keyword (prod — title.keyword does not exist) or text+keyword.
        With the .keyword-only term, the update-not-duplicate check silently
        found nothing on prod and re-running would CREATE a duplicate."""
        q = create_summary_report.EXISTING_REPORT_QUERY

        should = q["bool"]["should"]
        assert {"term": {"title": "Summary Report"}} in should
        assert {"term": {"title.keyword": "Summary Report"}} in should
        assert q["bool"]["minimum_should_match"] == 1


# ===========================================================================
# 2 + 3. JS/template metric defaults — no post_type.keyword anywhere
# ===========================================================================

class TestJsSurfacesUseBarePostType:
    def test_charts_js_summary_metrics(self):
        src = _read(_CHARTS_JS)

        assert "post_type.keyword" not in src
        # Pin the two repointed metric definitions specifically. (
        # normalized the dv to the canonical '*_disciple_tools'; this pin is
        # about the bare post_type field, so it accepts either spelling.)
        assert re.search(r"_m\('new-groups',\s*'\*_?disciple_tools',\s*'post_type',", src)
        assert re.search(r"_m\('new-contacts',\s*'\*_?disciple_tools',\s*'post_type',", src)


# ===========================================================================
# 4. Builder aggregation dropdowns list 'exists'
# ===========================================================================

class TestBuilderAggDropdownsListExists:
    def test_both_agg_option_lists_include_exists(self):
        """should offer 'exists' in the standard-table and grouped_table
        aggregation dropdowns so a stored exists column displays correctly
        and is editable (it previously rendered as 'sum')."""
        src = _read(_BUILDER_JS)

        # Standard table column editor (const aggOptions = [...]).
        std_list = re.search(r"const aggOptions = \[([^\]]*)\]", src)
        assert std_list, "standard-table aggOptions list not found"
        assert "'exists'" in std_list.group(1)

        # Grouped-table column editor — anchor on its 'gt-agg-select' class so
        # this can't match the Canva/Report metric editors' agg lists.
        gt_list = re.search(r"gt-agg-select[\s\S]*?\[([^\]]*)\]\.forEach\(agg =>", src)
        assert gt_list, "grouped_table aggregation option list not found"
        assert "'exists'" in gt_list.group(1)


# ===========================================================================
# 5. summary_report chart-type metrics editor — equals value + bare defaults
# ===========================================================================

class TestSummaryReportMetricsEditorEqualsValue:

    def test_editor_persists_equals_value_and_charts_consume_it(self):
        """should let an author set the equals match text in the
        summary_report metrics editor and have the chart query honor it —
        previously the editor offered 'equals' with no value input, and the
        server silently fell back to a sum when equals_value was missing."""
        builder_src = _read(_BUILDER_JS)
        charts_src = _read(_CHARTS_JS)

        # Editor (builder.js): the metric-override save path writes equals_value.
        save_block = re.search(r"const saveOverride = \(\) => \{([\s\S]*?)_markDirty\(\);", builder_src)
        assert save_block, "summary_report metrics editor saveOverride not found"
        assert ".equals_value = eqInput.value" in save_block.group(1)

        # Consumer (charts.js): _m applies a non-empty equals_value override.
        assert re.search(r"o\?\.equals_value \? \{ equals_value: o\.equals_value \}", charts_src)

    def test_editor_journey_defaults_use_bare_post_type(self):
        """the editor defaults pre-fill the override form — a stale
        post_type.keyword default re-breaks the metric the moment an author
        touches the card and saves."""
        builder_src = _read(_BUILDER_JS)

        assert "post_type.keyword" not in builder_src
        assert re.search(r"id: 'new-groups',.*field: 'post_type',.*equals_value: 'groups'", builder_src)
        assert re.search(r"id: 'new-contacts',.*field: 'post_type',.*equals_value: 'contacts'", builder_src)

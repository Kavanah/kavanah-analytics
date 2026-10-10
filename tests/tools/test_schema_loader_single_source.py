"""every import source loads its ES mapping from its schema file.

 installs one mapping-carrying composable template per ``SCHEMA_MAP``
source (priority 260) so an auto-created ``*_<source>`` index is born correctly
typed. That guard only covers sources registered in ``SCHEMA_MAP``.
closes the gap for the four sources that previously carried INLINE mapping dicts
in their importers and (for three of them) had no schema file at all:
``google_ads``, ``chatwoot_conversations``, ``chatwoot_insights`` (no file), and
``ga_metrics_window`` (loaded ga_metrics's file; ``*_ga_metrics`` does NOT match
``*_ga_metrics_window``). ``youtube_analytics`` had a file but the importer still
used an inline copy.

The refactor: author/register each source's ``unified_schema_<source>.json`` and
repoint the importer at ``schema_loader`` so the template and the importer's
``create_index_if_not_exists`` share ONE source of truth — they can never drift.

These pins are shape/behaviour only (no live ES). The "fresh index is born
keyword + aggregation succeeds" proof is run on staging via
``simulate_index_template``.

Mutation-verified locally:
  - Remove a newly-covered source from SCHEMA_MAP → test_newly_covered fails.
  - Re-introduce an inline ``mapping``/``INDEX_MAPPING`` dict in an importer
    (instead of get_schema_*) → test_importers_have_no_inline_mapping fails.
  - Point SCHEMA_MAP['ga_metrics_window'] back at 'unified_schema_ga_metrics' →
    test_ga_window_has_dedicated_file fails.
"""

from __future__ import annotations

import ast

import pytest

# The four sources brought under the per-source template guard.
NEWLY_COVERED = (
    "google_ads",
    "chatwoot_conversations",
    "chatwoot_insights",
    "ga_metrics_window",
)

# Importers that previously held an inline mapping dict and now must load from
# the schema file: (module path under bin/python, the inline var name they used).
REPOINTED_IMPORTERS = (
    ("import_scripts/google_ads/send_json_to_elastic.py", "mapping"),
    ("import_scripts/chatwoot/send_conversations_to_elastic.py", "INDEX_MAPPING"),
    ("import_scripts/chatwoot/send_json_to_elastic.py", "INDEX_MAPPING"),
    ("import_scripts/youtube/send_json_to_elastic.py", "mapping"),
)

# Bare fields the dashboards aggregate/filter on across these sources — a text
# type here is exactly the FLKCH fielddata-400 bug.
AGG_FIELDS = ("google_ads_customer_id", "conversation_id", "team_esid", "property_id")
NON_TEXT = {"keyword", "integer", "long", "float", "double", "date", "boolean"}


def _bin_python_dir():
    import os

    # tests/tools/this_file -> repo root -> bin/python
    here = os.path.dirname(__file__)
    return os.path.abspath(os.path.join(here, "..", "..", "bin", "python"))


def test_newly_covered_sources_registered_and_loadable():
    """Each newly-covered source is in SCHEMA_MAP and resolves a populated,
    non-text aggregatable mapping (so the template births a typed
    index instead of dynamic text)."""
    import sys

    sys.path.insert(0, _bin_python_dir())
    from migration.schema_loader import SCHEMA_MAP, get_schema_for_index_type

    for source in NEWLY_COVERED:
        assert source in SCHEMA_MAP, f"{source} must be registered in SCHEMA_MAP"
        props = get_schema_for_index_type(source)["properties"]
        assert props, f"{source} schema must be non-empty"
        for f in AGG_FIELDS:
            if f in props:
                t = props[f].get("type")
                assert t in NON_TEXT, (
                    f"{source}.{f} must be aggregatable ({NON_TEXT}), got {t!r} — "
                    f"a text type here throws the dashboard fielddata 400 (the FLKCH bug)."
                )


def test_ga_window_has_dedicated_schema_file():
    """ga_metrics_window must point at its OWN file, not reuse ga_metrics —
    so a future ga_metrics-only field change can't silently alter the window
    mapping, and the *_ga_metrics_window template pattern is covered."""
    import sys

    sys.path.insert(0, _bin_python_dir())
    from migration.schema_loader import SCHEMA_MAP

    assert SCHEMA_MAP["ga_metrics_window"] == "unified_schema_ga_metrics_window", (
        "ga_metrics_window must resolve a dedicated schema file "
        "(*_ga_metrics does NOT match *_ga_metrics_window)."
    )


def test_importers_have_no_inline_mapping_2():
    """The repointed importers must NOT define an inline mapping dict — the
    schema file is the single source of truth. A literal ``mapping = {...}`` /
    ``INDEX_MAPPING = {...}`` assignment with a ``properties`` key is the dual
    source-of-truth this ticket removes."""
    import os

    base = _bin_python_dir()
    for rel, varname in REPOINTED_IMPORTERS:
        with open(os.path.join(base, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == varname:
                        # An inline mapping is a dict literal carrying "properties".
                        is_inline_dict = isinstance(node.value, ast.Dict) and any(
                            isinstance(k, ast.Constant) and k.value == "properties" for k in node.value.keys
                        )
                        assert not is_inline_dict, (
                            f"{rel} still defines an inline mapping dict "
                            f"`{varname}` — it must load from the schema file via "
                            f"get_schema_for_index_type / get_schema_cached."
                        )

"""Read-only audit: does every live ``*_echo_user_stats`` index carry the schema?

Background
==========
``send_user_stats_to_elastic.py`` created its per-tenant indices with
``schema.get("mappings", {})`` — but ``get_schema_for_index_type`` returns the
mappings body itself (``{"properties": ...}``), so every echo user-stats index
was created with an EMPTY explicit mapping. Field types then came from the
 ``*_echo_user_stats`` template when it was installed, or from ES
dynamic mapping when it was not (strings as ``text`` + ``.keyword``, dates as
``date`` with the default format, ``hours_online_decimal`` as ``float`` only
by luck of the first value). The importer fix passes the schema verbatim; this
tool answers the ticket's follow-up question — which prod indices were born
under the bug and drifted?

The sweep discovers drift by ``team_esid`` / ``team_title`` type, and
``unified_schema_echo_user_stats.json`` has neither field, so that sweep can
never see this drift. This tool compares EVERY schema field instead.

Per concrete ``*_echo_user_stats`` index (system and ``__remap_3`` temp
names skipped) each schema field is classed:

* ``ok``       — live ``type`` matches the schema (and ``format`` too, for dates).
* ``drifted``  — live type/format differs (e.g. ``keyword`` mapped as ``text``).
* ``missing``  — field absent from the live mapping. Under a correct explicit
  mapping every schema field is present from creation, so a missing field
  means the index was created without the schema (docs may simply never have
  carried it yet, which is why it is reported separately from ``drifted``).

An index is ``DRIFTED`` when any field is drifted, ``BARE`` when nothing is
drifted but fields are missing, ``OK`` otherwise. Exit status 0 when every
index is OK, 1 when any is DRIFTED or BARE (so it can gate a runbook step),
2 when Elasticsearch is unreachable.

Usage::

    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_echo_user_stats_mapping # human table
    docker exec -w /app/python imports_container python3 \\
        -m tools.audit_echo_user_stats_mapping --json # machine-readable

This tool never writes. It has no ``--apply``. Rebuilding a drifted index is a
reindex (ES cannot change a field type in place) and belongs to the /
 sweep tooling, not here.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent if globals().get("__file__", "<stdin>") != "<stdin>" else Path("/app/python/tools")
_parents = list(_HERE.parents)
for _candidate in (
    *([_parents[1]] if len(_parents) > 1 else []),
    *([_parents[2] / "bin" / "python"] if len(_parents) > 2 else []),
    Path("/app/python"),
):
    if (_candidate / "asset_management").exists():
        sys.path.insert(0, str(_candidate))
        break

from lib.es_helpers import get_elasticsearch_client  # noqa: E402
from migration.schema_loader import get_schema_for_index_type  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE = "echo_user_stats"
INDEX_PATTERN = f"*_{SOURCE}"

STATE_OK = "OK"
STATE_BARE = "BARE"
STATE_DRIFTED = "DRIFTED"


def concrete_indices(es) -> list[str]:
    """Sorted concrete ``*_echo_user_stats`` index names, minus system/temp names.

    Temp rebuild indices (``{index}__remap_3`` and the like) do not END
    in ``_echo_user_stats`` even though the wildcard matches them, so the
    suffix check is what excludes them.
    """
    live = es.indices.get(index=INDEX_PATTERN, ignore_unavailable=True) or {}
    return sorted(name for name in live.keys() if not name.startswith(".") and name.endswith(f"_{SOURCE}"))


def live_properties(es, index: str) -> dict:
    m = es.indices.get_mapping(index=index)
    return list(m.values())[0].get("mappings", {}).get("properties", {}) or {}


def compare_fields(schema_props: dict, live_props: dict) -> dict[str, dict]:
    """``{field: {"state": ok|drifted|missing, "want": ..., "got": ...}}`` per schema field.

    ``want``/``got`` are the ``type`` plus the ``format`` when the schema sets
    one, so a date field that lost its ``yyyy-MM-dd`` format reads as drift.
    Sub-fields (``.keyword`` on a dynamic-mapped string) are reported in
    ``got`` so the operator can tell dynamic mapping from a hand edit.
    """
    out: dict[str, dict] = {}
    for field, spec in schema_props.items():
        want = {"type": spec.get("type")}
        if "format" in spec:
            want["format"] = spec["format"]
        live = live_props.get(field)
        if live is None:
            out[field] = {"state": "missing", "want": want, "got": None}
            continue
        got = {"type": live.get("type")}
        if "format" in spec:
            got["format"] = live.get("format")
        if "fields" in live:
            got["fields"] = sorted(live["fields"].keys())
        # An ``object`` field that ES stores as an implicit object carries no
        # ``type`` in the live mapping — that is the same thing, not drift.
        if want["type"] == "object" and live.get("type") in (None, "object"):
            out[field] = {"state": "ok", "want": want, "got": got}
            continue
        drifted = got["type"] != want["type"] or got.get("format") != want.get("format")
        out[field] = {"state": "drifted" if drifted else "ok", "want": want, "got": got}
    return out


def classify(fields: dict[str, dict]) -> str:
    states = {f["state"] for f in fields.values()}
    if "drifted" in states:
        return STATE_DRIFTED
    if "missing" in states:
        return STATE_BARE
    return STATE_OK


def audit(es, schema_props: dict) -> list[dict]:
    rows = []
    for index in concrete_indices(es):
        fields = compare_fields(schema_props, live_properties(es, index))
        rows.append(
            {
                "index": index,
                "state": classify(fields),
                "drifted": sorted(f for f, r in fields.items() if r["state"] == "drifted"),
                "missing": sorted(f for f, r in fields.items() if r["state"] == "missing"),
                "fields": fields,
            }
        )
    return rows


def _render_table(rows: list[dict]) -> str:
    if not rows:
        return "no *_echo_user_stats indices found"
    width = max(len(r["index"]) for r in rows)
    lines = [f"{'index'.ljust(width)}  state    detail"]
    for r in rows:
        detail = []
        for f in r["drifted"]:
            spec = r["fields"][f]
            detail.append(f"{f}: want {spec['want']} got {spec['got']}")
        if r["missing"]:
            detail.append(f"missing: {', '.join(r['missing'])}")
        lines.append(f"{r['index'].ljust(width)}  {r['state'].ljust(8)} {'; '.join(detail) or '-'}")
    counts = {s: sum(1 for r in rows if r["state"] == s) for s in (STATE_OK, STATE_BARE, STATE_DRIFTED)}
    lines.append("")
    lines.append(f"{len(rows)} index(es): {counts[STATE_OK]} OK, {counts[STATE_BARE]} BARE, {counts[STATE_DRIFTED]} DRIFTED")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    es = get_elasticsearch_client()
    if es is None:
        logger.error("Failed to connect to Elasticsearch.")
        return 2

    schema_props = get_schema_for_index_type(SOURCE).get("properties", {})
    rows = audit(es, schema_props)
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
    else:
        print(_render_table(rows))
    return 0 if all(r["state"] == STATE_OK for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())

"""One-shot scrub of per-person fields already stored in Elasticsearch.

 stops the Echo, Chatwoot and Disciple.Tools importers from writing the
per-seeker / per-convert fields nothing in the dashboards reads (decisions:
the private docs, a teammate 2026-09-08). This removes
the same fields from the documents that already exist:

* ``*__echo_conversations`` — ``client_name``, ``topic``, ``client_religion``,
  ``client_country``, ``client_language``, ``disposition_notes``, ``notes``,
  ``client_contact``, ``utm_url`` (+ dead ``name`` / ``user`` / ``link`` / ``pdf``)
* ``*__chatwoot_conversations`` — ``sender_name``, ``sender_email``,
  ``sender_phone``, ``sender_instagram_username``, ``dt_conversation_url``,
  ``assignee_email``
* ``*__disciple_tools`` — ``meta.age``, ``meta.gender``, ``meta.faith_status``;
  ``title`` blanked for ``post_type=contacts`` (groups keep theirs); and
  ``meta.contact_address`` **geocoded to** ``meta.contact_place`` /
  ``meta.contact_country`` (option B) before it is removed, so the DT map keeps
  its points at city grain
* stored dashboards — any table column whose ``field`` is a dropped field is
  removed (today: one dead ``client_contact`` column on the ``Summary`` dashboard)

Dry-run by default: reports how many documents still carry each dropped field
and writes nothing. ``--apply`` performs the scrub. Idempotent — a second
``--apply`` reports zero remaining. Never prints a field VALUE, only counts.

The geocoding pass calls Nominatim once per distinct uncached address at ≤1
request/s, so a first prod run over ~2.6k addresses can take up to ~45 minutes;
re-runs are cache hits.

Usage::

    docker exec -w /app/python imports_container python3 -m tools.scrub_person_fields
    docker exec -w /app/python imports_container python3 -m tools.scrub_person_fields --apply
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Callable

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

ECHO_PATTERN = "*__echo_conversations"
CHATWOOT_PATTERN = "*__chatwoot_conversations"
DT_PATTERN = "*__disciple_tools"
DASHBOARDS_INDEX = "de_asset_echarts_dashboards"

ECHO_FIELDS = (
    "client_name",
    "topic",
    "client_religion",
    "client_country",
    "client_language",
    "disposition_notes",
    "notes",
    "client_contact",
    "utm_url",
    "name",
    "user",
    "link",
    "pdf",
)
CHATWOOT_FIELDS = (
    "sender_name",
    "sender_email",
    "sender_phone",
    "sender_instagram_username",
    "dt_conversation_url",
    "assignee_email",
)
DT_META_FIELDS = ("age", "gender", "faith_status")
DT_ADDRESS_FIELD = "meta.contact_address"

ALL_DROPPED_FOR_DASHBOARDS = (
    set(ECHO_FIELDS) | set(CHATWOOT_FIELDS) | {f"meta.{f}" for f in DT_META_FIELDS} | {DT_ADDRESS_FIELD}
)


# update_by_query over ~120k docs runs well past the client's default 10 s read
# timeout; the server-side task keeps going but the client raises and the tool
# aborts mid-way. Give those calls a long per-request timeout (elasticsearch-py 8
# ``.options()``; older clients fall through unchanged).
UBQ_TIMEOUT_SECONDS = 1800


def _long(es):
    opts = getattr(es, "options", None)
    if callable(opts):
        try:
            return opts(request_timeout=UBQ_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — fall back to the plain client
            return es
    return es


def _count_any(es, pattern: str, fields: list[str]) -> int:
    body = {"query": {"bool": {"should": [{"exists": {"field": f}} for f in fields], "minimum_should_match": 1}}}
    return int(es.count(index=pattern, body=body, ignore_unavailable=True).get("count", 0))


def _remove_fields_by_query(es, pattern: str, fields: list[str], *, query: dict | None = None, extra_source: str = "") -> int:
    """update_by_query removing top-level or ``meta.x`` fields; returns updated count."""
    top = [f for f in fields if "." not in f]
    meta = [f.split(".", 1)[1] for f in fields if f.startswith("meta.")]
    script = "".join(f"ctx._source.remove('{f}');" for f in top)
    if meta:
        script += "if (ctx._source.meta != null) {" + "".join(f"ctx._source.meta.remove('{f}');" for f in meta) + "}"
    script += extra_source
    body = {
        "query": query or {"bool": {"should": [{"exists": {"field": f}} for f in fields], "minimum_should_match": 1}},
        "script": {"source": script, "lang": "painless"},
    }
    resp = _long(es).update_by_query(index=pattern, body=body, conflicts="proceed", refresh=True, ignore_unavailable=True)
    return int((resp or {}).get("updated", 0))


def scrub_echo(es, *, apply: bool) -> dict[str, int]:
    fields = list(ECHO_FIELDS)
    before = _count_any(es, ECHO_PATTERN, fields)
    logger.info("%-9s echo_conversations: %d doc(s) carry a dropped field", "would_fix" if not apply else "fixing", before)
    updated = _remove_fields_by_query(es, ECHO_PATTERN, fields) if (apply and before) else 0
    return {"before": before, "updated": updated, "after": _count_any(es, ECHO_PATTERN, fields) if apply else before}


def scrub_chatwoot(es, *, apply: bool) -> dict[str, int]:
    fields = list(CHATWOOT_FIELDS)
    before = _count_any(es, CHATWOOT_PATTERN, fields)
    logger.info("%-9s chatwoot_conversations: %d doc(s) carry a dropped field", "would_fix" if not apply else "fixing", before)
    updated = _remove_fields_by_query(es, CHATWOOT_PATTERN, fields) if (apply and before) else 0
    return {"before": before, "updated": updated, "after": _count_any(es, CHATWOOT_PATTERN, fields) if apply else before}


def scrub_dt(es, *, apply: bool, geocode: Callable[[str], dict | None] | None = None) -> dict[str, int]:
    meta_fields = [f"meta.{f}" for f in DT_META_FIELDS]
    before_meta = _count_any(es, DT_PATTERN, meta_fields)
    titled = int(
        es.count(
            index=DT_PATTERN,
            body={
                "query": {
                    "bool": {
                        "filter": [{"term": {"post_type": "contacts"}}],
                        "must_not": [{"term": {"title.keyword": ""}}],
                        "must": [{"exists": {"field": "title"}}],
                    }
                }
            },
            ignore_unavailable=True,
        ).get("count", 0)
    )
    addressed = int(
        es.count(index=DT_PATTERN, body={"query": {"exists": {"field": DT_ADDRESS_FIELD}}}, ignore_unavailable=True).get(
            "count", 0
        )
    )
    logger.info(
        "%-9s disciple_tools: %d doc(s) with age/gender/faith_status, %d contact(s) still named, %d doc(s) with a street address",
        "would_fix" if not apply else "fixing",
        before_meta,
        titled,
        addressed,
    )
    out = {
        "meta_before": before_meta,
        "titled_before": titled,
        "addressed_before": addressed,
        "geocoded": 0,
        "address_removed": 0,
    }
    if not apply:
        return out

    if before_meta:
        _remove_fields_by_query(es, DT_PATTERN, meta_fields)
    if titled:
        _long(es).update_by_query(
            index=DT_PATTERN,
            body={
                "query": {
                    "bool": {
                        "filter": [{"term": {"post_type": "contacts"}}],
                        "must": [{"exists": {"field": "title"}}],
                        "must_not": [{"term": {"title.keyword": ""}}],
                    }
                },
                "script": {"source": "ctx._source.title = '';", "lang": "painless"},
            },
            conflicts="proceed",
            refresh=True,
            ignore_unavailable=True,
        )

    # Addresses: geocode each distinct non-empty street to a coarse place, write
    # contact_place / contact_country, then remove the street. Empty strings are
    # just removed. Per-doc updates (not update_by_query) because the coarse
    # value comes from the geocoder, not from the document.
    if addressed:
        if geocode is None:
            from lib.geocoding import coarse_location_for_address as geocode  # type: ignore[no-redef]
        resp = es.search(
            index=DT_PATTERN,
            body={"size": 5000, "query": {"exists": {"field": DT_ADDRESS_FIELD}}, "_source": ["meta.contact_address"]},
            ignore_unavailable=True,
        )
        coarse_cache: dict[str, dict | None] = {}
        for hit in (resp.get("hits") or {}).get("hits") or []:
            street = ((hit.get("_source") or {}).get("meta") or {}).get("contact_address") or ""
            street = str(street).strip()
            doc: dict[str, Any] = {}
            if street:
                if street not in coarse_cache:
                    try:
                        coarse_cache[street] = geocode(street)
                    except Exception as exc:  # noqa: BLE001 — one bad address must not stop the scrub
                        logger.warning("geocode failed for one address (len=%d): %s", len(street), type(exc).__name__)
                        coarse_cache[street] = None
                coarse = coarse_cache[street]
                if coarse and coarse.get("place"):
                    doc["contact_place"] = coarse["place"]
                    doc["contact_country"] = coarse.get("country") or ""
                    out["geocoded"] += 1
            try:
                es.update(
                    index=hit["_index"],
                    id=hit["_id"],
                    body={
                        "script": {
                            "source": "for (k in params.doc.keySet()) { ctx._source.meta[k] = params.doc[k]; } ctx._source.meta.remove('contact_address');",
                            "lang": "painless",
                            "params": {"doc": doc},
                        }
                    },
                    refresh=False,
                )
                out["address_removed"] += 1
            except Exception as exc:  # noqa: BLE001 — keep going; the dry-run shows what is left
                logger.warning("update failed for %s/%s: %s", hit.get("_index"), hit.get("_id"), type(exc).__name__)
        try:
            es.indices.refresh(index=DT_PATTERN, ignore_unavailable=True)
        except Exception:  # noqa: BLE001
            pass
    out["meta_after"] = _count_any(es, DT_PATTERN, meta_fields)
    out["addressed_after"] = int(
        es.count(index=DT_PATTERN, body={"query": {"exists": {"field": DT_ADDRESS_FIELD}}}, ignore_unavailable=True).get(
            "count", 0
        )
    )
    return out


def _strip_dropped_columns(node: Any) -> int:
    """Recursively remove list entries under any ``columns`` key whose ``field``
    is a dropped field. Returns the number removed."""
    removed = 0
    if isinstance(node, dict):
        for key, val in list(node.items()):
            if key == "columns" and isinstance(val, list):
                keep = [
                    c for c in val if not (isinstance(c, dict) and str(c.get("field") or "") in ALL_DROPPED_FOR_DASHBOARDS)
                ]
                removed += len(val) - len(keep)
                node[key] = keep
            else:
                removed += _strip_dropped_columns(val)
    elif isinstance(node, list):
        for item in node:
            removed += _strip_dropped_columns(item)
    return removed


def scrub_dashboards(es, *, apply: bool) -> dict[str, int]:
    resp = es.search(index=DASHBOARDS_INDEX, body={"size": 500, "query": {"match_all": {}}}, ignore_unavailable=True)
    touched = columns = 0
    for hit in (resp.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        n = _strip_dropped_columns(src)
        if n:
            touched += 1
            columns += n
            logger.info(
                "%-9s dashboard %r: %d dead column(s) referencing dropped fields",
                "would_fix" if not apply else "fixing",
                (src.get("title") or hit.get("_id")),
                n,
            )
            if apply:
                es.index(index=DASHBOARDS_INDEX, id=hit["_id"], document=src, refresh="wait_for")
    return {"dashboards": touched, "columns": columns}


def run(*, apply: bool, geocode: Callable[[str], dict | None] | None = None) -> dict[str, Any]:
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        logger.error("Could not connect to Elasticsearch")
        return {}
    result = {
        "echo": scrub_echo(es, apply=apply),
        "chatwoot": scrub_chatwoot(es, apply=apply),
        "disciple_tools": scrub_dt(es, apply=apply, geocode=geocode),
        "dashboards": scrub_dashboards(es, apply=apply),
    }
    logger.info("done%s — %s", "" if apply else " (dry-run, nothing written)", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="perform the scrub (default: dry-run)")
    args = parser.parse_args(argv)
    run(apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())

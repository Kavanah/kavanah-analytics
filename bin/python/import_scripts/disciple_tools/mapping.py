"""Disciple.Tools REST JSON → dashboard document shape.

Pure functions, no I/O. This is the contract the legacy push-plugin produced
(see ``promote_staging.py`` and the dashboards in
``asset_management/web/blueprints/dashboard_builder``), reproduced from DT's
native ``dt-posts/v2`` REST JSON. Field shapes verified against a live instance
on 2026-06-05 — see ``the private docs``.

Two REST conventions drive every transform:
  * Dates are objects ``{"timestamp": <unix SECONDS>, "formatted": "YYYY-MM-DD"}``.
    The legacy doc uses epoch MILLIS (``*_ms``) → multiply ``timestamp`` by 1000.
  * Enum fields are objects ``{"key": ..., "label": ...}`` → keep ``.key``.
    Multi-selects (milestones, sources, health_metrics) are arrays of keys.

Baptism is read from ``milestones`` containing ``milestone_baptized`` — the live
instance does NOT populate ``baptism_date`` even for baptized contacts, and the
dashboard query keys on ``meta.milestones``. ``baptism_date`` is mapped only when
present (forward-compatible with instances that do fill it).
"""

from __future__ import annotations

from datetime import datetime, timezone

CONTACT_POST_TYPE = "contacts"
GROUP_POST_TYPE = "groups"


def _enum_key(value):
    """A DT enum field is ``{"key","label"}``; return the key (or the raw
    value if already a scalar, or None)."""
    if isinstance(value, dict):
        return value.get("key")
    if isinstance(value, str):
        return value
    return None


def _key_list(value):
    """A DT multi-select is a list of string keys. Coerce defensively:
    a list passes through; a scalar is wrapped; None/empty → []."""
    if isinstance(value, list):
        return [v for v in value if v not in (None, "")]
    if value in (None, ""):
        return []
    return [value]


def _date_parts(value):
    """Return ``(formatted, epoch_ms)`` for a DT date object.

    DT date objects carry ``timestamp`` in unix SECONDS. The legacy contract
    stores millis, so ms = timestamp * 1000. Missing/absent → ("", 0), which
    mirrors the legacy empty-date convention.
    """
    if not isinstance(value, dict):
        return "", 0
    ts = value.get("timestamp")
    formatted = value.get("formatted") or ""
    if ts in (None, ""):
        return formatted, 0
    try:
        return formatted, int(ts) * 1000
    except (TypeError, ValueError):
        return formatted, 0


def _iso_timestamp(value, fallback=None):
    """ISO-8601 ``@timestamp`` (ES requirement) from a DT date object's unix
    seconds. Falls back to ``fallback`` date object, else current UTC."""
    for candidate in (value, fallback):
        if isinstance(candidate, dict) and candidate.get("timestamp") not in (None, ""):
            try:
                return datetime.fromtimestamp(int(candidate["timestamp"]), tz=timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                continue
    return datetime.now(timezone.utc).isoformat()


def _first_grid_id(location_grid):
    """DT ``location_grid`` is ``[{"id","label"}, ...]``. Return the first grid
    id as a STRING (the geocache prewarm aggregates on a keyword field and looks
    the id up in a string-keyed grid-name map). None/empty → ""."""
    if isinstance(location_grid, list) and location_grid:
        first = location_grid[0]
        if isinstance(first, dict) and first.get("id") not in (None, ""):
            return str(first["id"])
    return ""


def _first_address_value(contact_address):
    """DT ``contact_address`` is ``[{"value","key","verified"}, ...]``. Return
    the first address string (the geocode input). None/empty → ""."""
    if isinstance(contact_address, list) and contact_address:
        first = contact_address[0]
        if isinstance(first, dict):
            return first.get("value") or ""
    return ""


def _title(post):
    return post.get("post_title") or post.get("name") or ""


def map_contact(post: dict, team_esid: str, team_title: str = "", organization_title: str = "", geocode=None) -> dict:
    """Map a DT ``contacts`` REST object to the dashboard document shape.

     (data minimisation, a teammate 2026-09-08): a contact document carries NO
    name (``title`` is blank for contacts — ``ID`` is the identity; groups keep
    their title), no ``age`` / ``gender`` / ``faith_status``, and no street
    address. The first ``contact_address`` entry is reduced through ``geocode``
    (``lib.geocoding.coarse_location_for_address``) to ``meta.contact_place``
    ("City, Country") + ``meta.contact_country``, which is what the DT map
    aggregates on. With ``geocode=None`` (unit tests) the address is simply
    dropped. Nothing in the dashboards reads the dropped fields
    (the private docs).
    """
    created_fmt, created_ms = _date_parts(post.get("post_date"))
    modified_fmt, modified_ms = _date_parts(post.get("last_modified"))

    meta = {
        "type": _enum_key(post.get("type")),
        "overall_status": _enum_key(post.get("overall_status")),
        "seeker_path": _enum_key(post.get("seeker_path")),
        "sources": _key_list(post.get("sources")),
        "milestones": _key_list(post.get("milestones")),
        "location_grid": _first_grid_id(post.get("location_grid")),
    }
    # coarse location only. The raw street address is never stored.
    street = _first_address_value(post.get("contact_address"))
    if street and geocode is not None:
        try:
            coarse = geocode(street)
        except Exception:  # noqa: BLE001 — a geocoder hiccup must not fail the import
            coarse = None
        if coarse and coarse.get("place"):
            meta["contact_place"] = coarse["place"]
            meta["contact_country"] = coarse.get("country") or ""
    # baptism_date is optional in DT — only emit when the source actually carries it.
    if isinstance(post.get("baptism_date"), dict):
        b_fmt, b_ms = _date_parts(post["baptism_date"])
        meta["baptism_date"] = b_fmt
        meta["baptism_date_ms"] = b_ms

    # the two fields the builder's multiplier_contacts /
    # spiritual_conversations map stats aggregate. DT returns number fields as
    # int or numeric string depending on version — coerce to int. OMIT when
    # absent, unparseable, or 0: the map stats are `exists`-type queries, so an
    # emitted 0 (or "") would count contacts with no generations / no
    # quick-button clicks (live-site survey 2026-06-12 treated 0 as unset).
    for rest_field in ("baptism_generation", "quick_button_contact_established"):
        raw = post.get(rest_field)
        if raw in (None, ""):
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value != 0:
            meta[rest_field] = value

    return {
        "@timestamp": _iso_timestamp(post.get("last_modified"), post.get("post_date")),
        "ID": str(post.get("ID", "")),
        "post_type": CONTACT_POST_TYPE,
        "title": "",  # contacts carry no name; ID is the identity
        "date_created": created_fmt,
        "date_created_ms": created_ms,
        "date_modified": modified_fmt,
        "date_modified_ms": modified_ms,
        "team_esid": team_esid,
        "team_title": team_title,
        "organization_title": organization_title,
        "meta": meta,
    }


def map_group(post: dict, team_esid: str, team_title: str = "", organization_title: str = "") -> dict:
    """Map a DT ``groups`` REST object to the dashboard document shape."""
    created_fmt, created_ms = _date_parts(post.get("post_date"))
    modified_fmt, modified_ms = _date_parts(post.get("last_modified"))

    meta = {
        "group_status": _enum_key(post.get("group_status")),
        "group_type": _enum_key(post.get("group_type")),
        "health_metrics": _key_list(post.get("health_metrics")),
        "member_count": post.get("member_count"),
        "leader_count": post.get("leader_count"),
        "location_grid": _first_grid_id(post.get("location_grid")),
    }
    start_fmt, start_ms = _date_parts(post.get("start_date"))
    meta["start_date"] = start_fmt
    meta["start_date_ms"] = start_ms
    # church_start_date is present only for churches — emit only when carried.
    if isinstance(post.get("church_start_date"), dict):
        c_fmt, c_ms = _date_parts(post["church_start_date"])
        meta["church_start_date"] = c_fmt
        meta["church_start_date_ms"] = c_ms

    return {
        "@timestamp": _iso_timestamp(post.get("last_modified"), post.get("post_date")),
        "ID": str(post.get("ID", "")),
        "post_type": GROUP_POST_TYPE,
        "title": _title(post),
        "date_created": created_fmt,
        "date_created_ms": created_ms,
        "date_modified": modified_fmt,
        "date_modified_ms": modified_ms,
        "team_esid": team_esid,
        "team_title": team_title,
        "organization_title": organization_title,
        "meta": meta,
    }


def map_post(post: dict, team_esid: str, team_title: str = "", organization_title: str = "", geocode=None) -> dict | None:
    """Dispatch on ``post_type``. Returns None for unsupported types."""
    post_type = post.get("post_type")
    if post_type == CONTACT_POST_TYPE:
        return map_contact(post, team_esid, team_title, organization_title, geocode=geocode)
    if post_type == GROUP_POST_TYPE:
        return map_group(post, team_esid, team_title, organization_title)
    return None

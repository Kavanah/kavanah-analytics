"""Publish to Global.Church — the dialog that sends one organisation's release product.

The owner of the workspace an organisation belongs to opens this page for
that organisation, chooses which teams and which countries to include,
whether the organisation is named on the shared map, reads and accepts the
data-sharing agreement, and publishes. The page shows what leaves (country
x month counts and the manifest) and what stays (everything else); the
preview is computed by the export job's own counting code and shows counts
only — never a document value.

Publishing does four things, in order, and stops at the first that fails:

1. records the acceptance on the organisation record (agreement id,
   version, the SHA-256 of the agreement file computed at request time,
   the time, the display-name choice);
2. builds the release with ``jobs.export_release_product.build_release``
   (the same job the CLI runs — it refuses rather than producing an invalid
   file) and writes it to a temporary path, as ``--apply`` would;
3. POSTs the file as JSON to ``<gc_gateway_url>/v0/partners/releases`` with
   ``Authorization: Bearer <gc_partner_api_key>``;
4. on 202 records the receipt (release id, manifest hash, time) on the
   organisation record. A refusal from the gateway records nothing.

Entitlement: workspace-owner only. The gate is
``PermissionResolver.has_permission_in_workspace(user, <the organisation's
workspace>, WORKSPACE_DELETE)`` — the one permission ``workspace_owner``
holds that ``workspace_admin`` does not, checked on the organisation's own
workspace so an owner elsewhere cannot publish a foreign organisation's
data. A superuser passes. The manifest's ``acceptedByRole`` is
``workspace-owner`` for the same reason.

The gateway URL is an administrator-set organisation setting; it must be
``https`` and carry no credentials, or the publish is refused before any
request is made. The partner key is stored encrypted by the organisation
form and decrypted only to build the request header; it is never rendered
and never logged.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from flask import abort, render_template, request, session, url_for

from asset_management.jobs import export_release_product as job
from lib.encryption import decrypt, looks_like_fernet_token
from lib.es_helpers import build_data_index_name, get_elasticsearch_client

from . import publish_global_church_bp

try:
    from web.services.asset_service import AssetService
    from web.services.permission_resolver import PermissionResolver
    from web.services.permissions_enum import Permission
except ImportError:
    from ...services.asset_service import AssetService
    from ...services.permission_resolver import PermissionResolver
    from ...services.permissions_enum import Permission


RELEASES_PATH = "/v0/partners/releases"
REQUEST_TIMEOUT_SECONDS = 60

SETTING_GATEWAY_URL = "gc_gateway_url"
SETTING_API_KEY = "gc_partner_api_key"
SETTING_LAST_RELEASE_ID = "gc_last_release_id"
SETTING_LAST_RELEASE_HASH = "gc_last_release_hash"
SETTING_LAST_PUBLISHED_AT = "gc_last_published_at"

AGREEMENT_ID = "digital-aggregates"
AGREEMENT_VERSION = "0.1"
AGREEMENT_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "agreements", f"{AGREEMENT_ID}-v{AGREEMENT_VERSION}.md")
)

DISPLAY_NAME_MAX = 120
_ISO2_RE = re.compile(r"^[A-Z]{2}$")
_ESID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class PublishError(Exception):
    """A refusal the page shows to the owner; nothing was sent."""


# ── Agreement ─────────────────────────────────────────────────────────────────


def load_agreement() -> tuple[str, str]:
    """The agreement text and the SHA-256 of its exact bytes, read at request time."""
    with open(AGREEMENT_FILE, "rb") as fh:
        raw = fh.read()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def render_agreement_html(text: str) -> str:
    """A small, dependency-free rendering of the agreement's Markdown (headings,
    paragraphs, bullet lists, pipe tables, rules, bold and code). Everything is
    escaped first; the file is ours, the rendering is still defensive."""
    blocks: list[str] = []
    para: list[str] = []
    bullets: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if para:
            blocks.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()
        if bullets:
            blocks.append("<ul>" + "".join(f"<li>{_inline(b)}</li>" for b in bullets) + "</ul>")
            bullets.clear()
        if table:
            rows = [r.strip().strip("|").split("|") for r in table if not re.match(r"^\s*\|?\s*-{3,}", r)]
            if rows:
                head, body = rows[0], rows[1:]
                thead = "".join(f"<th>{_inline(c.strip())}</th>" for c in head)
                tbody = "".join("<tr>" + "".join(f"<td>{_inline(c.strip())}</td>" for c in r) + "</tr>" for r in body)
                blocks.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            table.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            flush()
        elif stripped.startswith("|"):
            if para or bullets:
                flush()
            table.append(stripped)
        elif stripped.startswith("#"):
            flush()
            level = min(len(stripped) - len(stripped.lstrip("#")), 4)
            blocks.append(f"<h{level + 1}>{_inline(stripped.lstrip('#').strip())}</h{level + 1}>")
        elif stripped == "---":
            flush()
            blocks.append("<hr>")
        elif stripped.startswith("- "):
            if para or table:
                flush()
            bullets.append(stripped[2:])
        else:
            if bullets or table:
                flush()
            para.append(stripped)
    flush()
    return "\n".join(blocks)


# ── Entitlement ───────────────────────────────────────────────────────────────


def _caller_is_owner(org: dict) -> bool:
    roles = session.get("roles") or []
    if "superuser" in roles:
        return True
    username = session.get("username") or ""
    workspace_esid = str(org.get("workspace_esid") or "")
    return PermissionResolver.has_permission_in_workspace(username, workspace_esid, Permission.WORKSPACE_DELETE)


def _load_org_for_owner(org_esid: str):
    """``(org, None)`` when the caller owns the organisation's workspace;
    ``(None, response)`` otherwise. The 403 is returned directly (not raised)
    so it stays a 403 instead of the page-level redirect."""
    if not _ESID_RE.match(org_esid or ""):
        abort(404)
    org = AssetService.get_by_id(job.ORG_INDEX, org_esid)
    if not org:
        abort(404)
    if not _caller_is_owner(org):
        return None, ("This page requires workspace-owner access for this organisation.", 403)
    return org, None


# ── Reading the organisation's data (counts only) ────────────────────────────


def _hits(resp) -> list[dict]:
    body = resp.body if hasattr(resp, "body") else resp
    return (((body or {}).get("hits") or {}).get("hits")) or []


def _teams_of(es, org_esid: str) -> list[tuple[str, str]]:
    resp = es.search(
        index=job.TEAMS_INDEX,
        body={"size": 500, "query": {"term": {"organization_esid": org_esid}}, "_source": ["team_title"]},
    )
    teams = []
    for hit in _hits(resp):
        title = str((hit.get("_source") or {}).get("team_title") or "").strip()
        if title:
            teams.append((hit["_id"], title))
    return sorted(teams, key=lambda t: t[1].lower())


_COUNTRY_FIELDS = (
    ("echo_conversations", "contact_country_iso2", None),
    ("google_ads", "geo_country_code", {"term": {"record_type": "geo"}}),
    ("youtube_analytics", "country", {"term": {"record_type": "country_breakdown"}}),
)


def _countries_in_data(es, team_esids: list[str], first: str, last: str) -> set[str]:
    """ISO-2 codes present in the selected teams' country-keyed documents in the window."""
    found: set[str] = set()
    for esid in team_esids:
        for source, field_name, extra in _COUNTRY_FIELDS:
            index = build_data_index_name(esid, source)
            if not es.indices.exists(index=index):
                continue
            filters = [job.window_range(first, last)] + ([extra] if extra else [])
            body = {
                "size": 0,
                "query": {"bool": {"filter": filters}},
                "aggs": {"countries": {"terms": {"field": field_name, "size": 300}}},
            }
            resp = es.search(index=index, body=body)
            resp = resp.body if hasattr(resp, "body") else resp
            for bucket in (((resp or {}).get("aggregations") or {}).get("countries") or {}).get("buckets") or []:
                code = str(bucket.get("key") or "").strip().upper()
                if _ISO2_RE.match(code):
                    found.add(code)
    return found


def _country_names(es, codes: list[str]) -> dict[str, str]:
    names: dict[str, str] = {}
    if es.indices.exists(index=job.COUNTRIES_INDEX):
        resp = es.search(index=job.COUNTRIES_INDEX, body={"size": 1000, "query": {"match_all": {}}})
        for hit in _hits(resp):
            src = hit.get("_source") or {}
            title = str(src.get("country_title") or "").strip()
            for key in ("country_iso3_code", "country_iso"):
                code = str(src.get(key) or "").strip().upper()
                if title and _ISO2_RE.match(code):
                    names.setdefault(code, title)
    for code in codes:
        if code in names:
            continue
        try:
            import pycountry

            rec = pycountry.countries.get(alpha_2=code)
            names[code] = rec.name if rec else code
        except Exception:  # noqa: BLE001 — the name is cosmetic; the code is the key
            names[code] = code
    return names


def _preview(es, org_esid: str, team_esids: list[str], countries: list[str], now: datetime) -> dict:
    """The dry run: the job's own counting over the selection, summarised as counts."""
    first, last = job.month_window(now)
    teams = job.load_teams(es, org_esid, team_esids)
    counts = job.Counts()
    for esid, title in teams:
        job.count_team(es, counts, esid, title, countries, first, last)
    per_country: dict[str, dict[str, int]] = {}
    for (_team, iso2, _month), cell in counts.rows.items():
        acc = per_country.setdefault(iso2, {"exposed": 0, "interactions": 0, "touchpoints": 0, "active": 0})
        for k in acc:
            acc[k] += cell[k]
    return {
        "first_month": first,
        "last_month": last,
        "cells": len(counts.rows),
        "sources": sorted(counts.sources_used),
        "per_country": per_country,
        "unattributed_exposed": counts.unattributed_exposed,
        "unattributed_interactions": counts.unattributed_interactions,
    }


# ── Settings and the gateway ─────────────────────────────────────────────────


def gateway_url_ok(url: str) -> bool:
    """Only an https origin with no credentials, query or fragment is accepted."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
    )


def _partner_key(org: dict) -> str:
    stored = str(org.get(SETTING_API_KEY) or "").strip()
    if not stored:
        return ""
    if looks_like_fernet_token(stored):
        return decrypt(stored)
    return stored


def check_settings(org: dict) -> list[str]:
    """Human-readable reasons the organisation cannot publish yet (empty = ready)."""
    problems = []
    url = str(org.get(SETTING_GATEWAY_URL) or "").strip()
    if not url:
        problems.append("the gateway URL is not set")
    elif not gateway_url_ok(url):
        problems.append("the gateway URL must be an https address with no credentials")
    if not str(org.get(SETTING_API_KEY) or "").strip():
        problems.append("the partner API key is not set")
    if not str(org.get(job.SETTING_ORG_URI) or "").strip():
        problems.append("the directory URI is not set")
    return problems


def post_release(gateway_url: str, api_key: str, release: dict) -> tuple[int, dict]:
    """The one network call. Returns ``(status, body)``; raises ``PublishError`` when unreachable."""
    url = gateway_url.strip().rstrip("/") + RELEASES_PATH
    try:
        resp = requests.post(
            url,
            json=release,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise PublishError(f"could not reach the gateway ({type(exc).__name__})") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    return resp.status_code, body if isinstance(body, dict) else {}


def _write_org(org_esid: str, updates: dict) -> dict:
    existing = AssetService.get_by_id(job.ORG_INDEX, org_esid) or {}
    doc = {**existing, **updates}
    if not AssetService.update(job.ORG_INDEX, org_esid, doc):
        raise PublishError("the organisation record could not be updated")
    return doc


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── Request parsing ──────────────────────────────────────────────────────────


def _selected(values, allowed: list[str], *, upper: bool = False) -> list[str]:
    """The submitted values that are actually on offer, in the offered order."""
    wanted = {str(v).strip().upper() if upper else str(v).strip() for v in values}
    return [a for a in allowed if a in wanted]


def _page_state(es, org_esid: str, org: dict, args, *, default_all: bool) -> dict:
    """Everything the template needs, for the given selection. ``default_all``
    (the GET) preselects every team and country when nothing was asked for;
    the POST never defaults — only what the owner ticked is published."""
    now = datetime.now(timezone.utc)
    first, last = job.month_window(now)
    teams = _teams_of(es, org_esid)
    team_ids = [esid for esid, _ in teams]
    selected_teams = _selected(args.getlist("teams"), team_ids) if ("teams" in args or not default_all) else team_ids
    data_countries = sorted(_countries_in_data(es, selected_teams, first, last))
    selected_countries = (
        _selected(args.getlist("countries"), data_countries, upper=True)
        if ("countries" in args or not default_all)
        else data_countries
    )
    names = _country_names(es, data_countries)
    preview = None
    preview_error = None
    if selected_teams and selected_countries:
        try:
            preview = _preview(es, org_esid, selected_teams, selected_countries, now)
        except job.ExportRefused as exc:
            preview_error = str(exc)
    named_default = job._bool(org.get(job.SETTING_DISPLAY_NAMED))
    named = (args.get("named") == "yes") if "named" in args else named_default
    display_name = args.get("display_name") or org.get(job.SETTING_DISPLAY_NAME) or org.get("organization_title") or ""
    text, sha = load_agreement()
    return {
        "org_esid": org_esid,
        "org_title": org.get("organization_title") or org_esid,
        "teams": [{"esid": esid, "title": title, "selected": esid in selected_teams} for esid, title in teams],
        "countries": [
            {"iso2": c, "name": names.get(c, c), "selected": c in selected_countries, "totals": None} for c in data_countries
        ],
        "selected_countries": selected_countries,
        "preview": preview,
        "preview_error": preview_error,
        "named": named,
        "display_name": str(display_name)[:DISPLAY_NAME_MAX],
        "test_data": args.get("test_data") == "yes",
        "agreement_html": render_agreement_html(text),
        "agreement_sha256": sha,
        "agreement_id": AGREEMENT_ID,
        "agreement_version": AGREEMENT_VERSION,
        "settings_problems": check_settings(org),
        "last_release": {
            "id": org.get(SETTING_LAST_RELEASE_ID),
            "hash": org.get(SETTING_LAST_RELEASE_HASH),
            "at": org.get(SETTING_LAST_PUBLISHED_AT),
        },
        "cancel_url": url_for("edit_asset", asset_type="organizations", item_id=org_esid),
    }


def _render(state: dict, status: int = 200, **extra):
    if state.get("preview"):
        per_country = state["preview"]["per_country"]
        for c in state["countries"]:
            c["totals"] = per_country.get(c["iso2"])
    return render_template("publish_global_church.html", **state, **extra), status


# ── Routes ───────────────────────────────────────────────────────────────────


@publish_global_church_bp.route("/<org_esid>", methods=["GET"])
def dialog(org_esid: str):
    org, denied = _load_org_for_owner(org_esid)
    if denied:
        return denied
    es = get_elasticsearch_client()
    if es is None:
        return "Elasticsearch is not available.", 503
    return _render(_page_state(es, org_esid, org, request.args, default_all=True))


@publish_global_church_bp.route("/<org_esid>", methods=["POST"])
def publish(org_esid: str):
    org, denied = _load_org_for_owner(org_esid)
    if denied:
        return denied
    es = get_elasticsearch_client()
    if es is None:
        return "Elasticsearch is not available.", 503
    form = request.form
    state = _page_state(es, org_esid, org, form, default_all=False)
    try:
        if form.get("accept") != "yes":
            raise PublishError("Tick the acceptance box to publish; nothing was sent.")
        problems = check_settings(org)
        if problems:
            raise PublishError("The organisation is not set up to publish: " + "; ".join(problems) + ".")
        team_esids = [t["esid"] for t in state["teams"] if t["selected"]]
        countries = state["selected_countries"]
        if not team_esids or not countries:
            raise PublishError("Select at least one team and one country.")
        named = state["named"]
        display_name = state["display_name"].strip()
        if named and not display_name:
            raise PublishError("Enter the name to show on the map, or choose to contribute unnamed.")

        # 1. the acceptance, hashed server-side from the file at this moment
        _text, sha = load_agreement()
        accepted_at = _now_iso()
        _write_org(
            org_esid,
            {
                job.SETTING_AGREEMENT_ID: AGREEMENT_ID,
                job.SETTING_AGREEMENT_VERSION: AGREEMENT_VERSION,
                job.SETTING_AGREEMENT_SHA256: sha,
                job.SETTING_AGREEMENT_ACCEPTED_AT: accepted_at,
                job.SETTING_DISPLAY_NAMED: named,
                job.SETTING_DISPLAY_NAME: display_name if named else "",
            },
        )

        # 2. the release, built by the job and written as --apply would
        try:
            release, counts = job.build_release(
                es, org_esid=org_esid, team_esids=team_esids, countries=countries, test_data=state["test_data"]
            )
        except job.ExportRefused as exc:
            raise PublishError(f"The export refused: {exc}") from exc
        tmp_dir = tempfile.mkdtemp(prefix="gc-release-")
        try:
            out_path = job.default_out_path(org_esid, release["manifest"]["period"]["lastMonth"], out_dir=tmp_dir)
            job.write_release(release, out_path)
            # 3. the one network call
            status, body = post_release(str(org[SETTING_GATEWAY_URL]), _partner_key(org), release)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if status != 202:
            detail = str(body.get("error") or body.get("message") or "").strip()
            raise PublishError(f"The gateway refused the release (HTTP {status}{': ' + detail if detail else ''}).")

        # 4. the receipt
        receipt = {
            SETTING_LAST_RELEASE_ID: str(body.get("releaseId") or ""),
            SETTING_LAST_RELEASE_HASH: str(body.get("manifestHash") or ""),
            SETTING_LAST_PUBLISHED_AT: _now_iso(),
        }
        org = _write_org(org_esid, receipt)
        logging.info(
            "publish_global_church: organisation %s published %s rows (release %s)",
            org_esid,
            len(release["rows"]),
            receipt[SETTING_LAST_RELEASE_ID],
        )
    except PublishError as exc:
        return _render(state, 400, error=str(exc))
    state = _page_state(es, org_esid, org, form, default_all=False)
    summary = job.summarize(release, counts)
    return _render(state, 200, published={"receipt": receipt, "summary": summary})

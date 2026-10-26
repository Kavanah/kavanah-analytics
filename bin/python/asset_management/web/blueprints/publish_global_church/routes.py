"""Publish to Global.Church — the dialog that sends one organisation's release product.

The owner of the workspace an organisation belongs to opens this page for
that organisation, chooses which teams and which countries to include,
whether the organisation is named on the shared map, reads and accepts the
data-sharing agreement, and publishes. The page shows what leaves (country
x month counts and the manifest) and what stays (everything else); the
preview is computed by the export job's own counting code and shows counts
only — never a document value.

Publishing does three things, in order, and stops at the first that fails
(moved the acceptance write to the end: nothing is recorded on
the organisation until the gateway has accepted the file):

1. builds the release with ``jobs.export_release_product.build_release``
   (the same job the CLI runs — it refuses rather than producing an invalid
   file) from the organisation record *as it will be* once the owner's
   acceptance is written (agreement id, version, the SHA-256 of the
   agreement file computed at request time, the time, the display-name
   choice); nothing is written to disk or to Elasticsearch;
2. POSTs the release as JSON to ``<gc_gateway_url>/v0/partners/releases``
   with ``Authorization: Bearer <gc_partner_api_key>``, never following a
   redirect;
3. on 202 writes the acceptance and the receipt (release id, manifest hash,
   time) onto the organisation record in one update. A refusal from the
   gateway records nothing, so the record never claims an acceptance that
   produced no release.

The whole page is hidden (404) while the ``GLOBAL_CHURCH_PUBLISH`` feature
flag is off, the same gate the export job honours.

Entitlement: workspace-owner only. The gate is
``PermissionResolver.has_permission_in_workspace(user, <the organisation's
workspace>, WORKSPACE_DELETE)`` — the one permission ``workspace_owner``
holds that ``workspace_admin`` does not, checked on the organisation's own
workspace so an owner elsewhere cannot publish a foreign organisation's
data. A superuser may open the page but may not publish unless they are
also the owner: the manifest records ``acceptedByRole: workspace-owner``,
and that must be true of whoever accepted.

The gateway URL, the partner key and the directory URI are superuser-only
organisation settings. The URL must be ``https`` on port 443 (explicit or
implied), an origin with no path beyond ``/``, carry no credentials, and
name a host on the allowlist ``GC_GATEWAY_HOSTS`` (environment,
comma-separated; default ``api.global.church``) — otherwise the publish is
refused before any request is made, so nobody who can edit an organisation
can redirect the release and its bearer key elsewhere. The partner key must be stored
encrypted (the form encrypts it); a value that is not a Fernet token, or one
that no longer decrypts, is refused and the owner is asked to re-enter it.
The key is decrypted only to build the request header; it is never rendered
and never logged.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from flask import abort, render_template, request, session, url_for

from cryptography.fernet import InvalidToken

from asset_management.jobs import export_release_product as job
from lib.encryption import decrypt, looks_like_fernet_token
from lib.es_helpers import build_data_index_name, get_elasticsearch_client
from lib.user_errors import UserFacingError

from asset_management.web.services.global_church_sharing_service import (
    AGREEMENT_FILE,
    AGREEMENT_ID,
    AGREEMENT_VERSION,
    OFF_NOTE,
    WHAT_LEAVES,
    WHAT_STAYS,
    GlobalChurchSharingService,
    SharingError,
    load_agreement,
    render_agreement_html,
)

from . import publish_global_church_bp

try:
    from web.services.asset_service import AssetService
    from web.services.audit_log_service import AuditLogService
    from web.services.permission_resolver import PermissionResolver
    from web.services.permissions_enum import Permission
except ImportError:
    from ...services.asset_service import AssetService
    from ...services.audit_log_service import AuditLogService
    from ...services.permission_resolver import PermissionResolver
    from ...services.permissions_enum import Permission

# Re-exported for the tests and for anyone reading the dialog: the agreement
# text, version and rendering live in the sharing service.
__all__ = ["AGREEMENT_FILE", "AGREEMENT_ID", "AGREEMENT_VERSION", "load_agreement", "render_agreement_html"]


RELEASES_PATH = "/v0/partners/releases"
REQUEST_TIMEOUT_SECONDS = 60
GATEWAY_HOSTS_ENV = "GC_GATEWAY_HOSTS"
DEFAULT_GATEWAY_HOSTS = "api.global.church"
ERROR_DETAIL_MAX = 200

# Asset indexes read here (module-level constants so the index guard
# can see they are not request-derived).
TEAMS_INDEX = job.TEAMS_INDEX
COUNTRIES_INDEX = job.COUNTRIES_INDEX

SETTING_GATEWAY_URL = "gc_gateway_url"
SETTING_API_KEY = "gc_partner_api_key"
SETTING_LAST_RELEASE_ID = "gc_last_release_id"
SETTING_LAST_RELEASE_HASH = "gc_last_release_hash"
SETTING_LAST_PUBLISHED_AT = "gc_last_published_at"

DISPLAY_NAME_MAX = 120
_ISO2_RE = re.compile(r"^[A-Z]{2}$")
_ESID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
# a receipt value the gateway returns is third-party text; it is
# stored only when it is a plain identifier of bounded length.
RECEIPT_VALUE_MAX = 128
_RECEIPT_RE = re.compile(r"^[A-Za-z0-9:._-]{1,128}$")
GATEWAY_PORT = 443


class PublishError(UserFacingError):
    """A refusal the page shows to the owner, in our own words (:
    routes render ``public_message``, never ``str(exc)``); nothing was sent."""


# ── Entitlement ───────────────────────────────────────────────────────────────


def _caller_is_owner(org: dict) -> bool:
    username = session.get("username") or ""
    workspace_esid = str(org.get("workspace_esid") or "")
    return PermissionResolver.has_permission_in_workspace(username, workspace_esid, Permission.WORKSPACE_DELETE)


def _caller_is_superuser() -> bool:
    return "superuser" in (session.get("roles") or [])


def _require_org_owner(org_esid: str, *, allow_superuser: bool, require_sharing: bool = True):
    """``(org, None)`` when the caller owns the organisation's workspace (or,
    for the read-only page, is a superuser); ``(None, response)`` otherwise.
    The 403 is returned directly (not raised) so it stays a 403 instead of
    the page-level redirect. While the feature flag is off the page does not
    exist (404), whoever asks; nor does the publish dialog while the
    organisation's sharing switch is off — only the sharing page
    itself (``require_sharing=False``) is reachable then."""
    if not job.publish_enabled():
        abort(404)
    if not _ESID_RE.match(org_esid or ""):
        abort(404)
    org = AssetService.get_by_id(job.ORG_INDEX, org_esid)
    if not org:
        abort(404)
    if require_sharing and not GlobalChurchSharingService.is_enabled(org):
        abort(404)
    if _caller_is_owner(org) or (allow_superuser and _caller_is_superuser()):
        return org, None
    if allow_superuser:
        return None, ("This page requires workspace-owner access for this organisation.", 403)
    return None, ("Publishing requires the workspace owner of this organisation; a superuser may view but not publish.", 403)


# ── Reading the organisation's data (counts only) ────────────────────────────


def _hits(resp) -> list[dict]:
    body = resp.body if hasattr(resp, "body") else resp
    return (((body or {}).get("hits") or {}).get("hits")) or []


def _teams_of(es, org_esid: str) -> list[tuple[str, str]]:
    resp = es.search(
        index=TEAMS_INDEX,
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
    if es.indices.exists(index=COUNTRIES_INDEX):
        resp = es.search(index=COUNTRIES_INDEX, body={"size": 1000, "query": {"match_all": {}}})
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
    # The preview only needs a per-team key to keep cells apart before they
    # are summed per country; the esid serves, and nothing here leaves.
    for esid in teams:
        job.count_team(es, counts, esid, esid, countries, first, last)
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


def allowed_gateway_hosts() -> frozenset[str]:
    """Hosts a release may be posted to: ``GC_GATEWAY_HOSTS`` (comma-separated),
    default ``api.global.church``. Read at call time so a deployment can set it
    without a restart of anything but the request."""
    raw = os.environ.get(GATEWAY_HOSTS_ENV) or DEFAULT_GATEWAY_HOSTS
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def gateway_url_ok(url: str) -> bool:
    """Only an https origin on an allowed host and port 443, with no path
    beyond ``/``, no credentials, query or fragment, is accepted.
    The organisation setting cannot point the release (and its bearer key)
    at an arbitrary server, port or path."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port  # raises ValueError on a non-numeric port
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.hostname.lower() in allowed_gateway_hosts()
        and port in (None, GATEWAY_PORT)
        and parts.path in ("", "/")
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
    )


def _partner_key(org: dict) -> str:
    """The decrypted partner key. A stored value that is not a Fernet token was
    not written by the organisation form and is refused rather than sent; a
    token that no longer decrypts (rotated or foreign key) is refused too."""
    stored = str(org.get(SETTING_API_KEY) or "").strip()
    if not stored:
        raise PublishError("The partner API key is not set.")
    if not looks_like_fernet_token(stored):
        raise PublishError("The partner API key is not stored encrypted; re-enter it on the organisation's settings.")
    try:
        return decrypt(stored)
    except InvalidToken as exc:
        raise PublishError(
            "The stored partner API key cannot be decrypted; re-enter it on the organisation's settings."
        ) from exc


def check_settings(org: dict) -> list[str]:
    """Human-readable reasons the organisation cannot publish yet (empty = ready)."""
    problems = []
    url = str(org.get(SETTING_GATEWAY_URL) or "").strip()
    if not url:
        problems.append("the gateway URL is not set")
    elif not gateway_url_ok(url):
        problems.append(
            "the gateway URL must be an https address on an allowed gateway host, port 443, with no path or credentials"
        )
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
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise PublishError(f"could not reach the gateway ({type(exc).__name__})") from exc
    if 300 <= resp.status_code < 400:
        raise PublishError(f"the gateway answered with a redirect (HTTP {resp.status_code}); refusing to follow it")
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


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _error_detail(body: dict) -> str:
    """The gateway's own message, bounded and stripped of control characters
    before it is shown (it is third-party text)."""
    detail = str(body.get("error") or body.get("message") or "").strip()
    return _CONTROL_RE.sub(" ", detail)[:ERROR_DETAIL_MAX]


def receipt_value(value) -> str:
    """A receipt field as it may be stored: a plain identifier of at most
    ``RECEIPT_VALUE_MAX`` characters, else empty. The gateway's answer is
    third-party text and must not land in the organisation record as-is."""
    text = str(value or "").strip()
    return text if _RECEIPT_RE.match(text) else ""


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
        except (job.ExportRefused, ValueError) as exc:
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
        "sharing_url": url_for("publish_global_church.sharing", org_esid=org_esid),
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
    org, denied = _require_org_owner(org_esid, allow_superuser=True)
    if denied:
        return denied
    es = get_elasticsearch_client()
    if es is None:
        return "Elasticsearch is not available.", 503
    return _render(_page_state(es, org_esid, org, request.args, default_all=True))


@publish_global_church_bp.route("/<org_esid>", methods=["POST"])
def publish(org_esid: str):
    org, denied = _require_org_owner(org_esid, allow_superuser=False)
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

        # The acceptance, hashed server-side from the file at this moment. It
        # is NOT written yet: the release is built from the record
        # as it will be, and the record changes only once the gateway accepts.
        _text, sha = load_agreement()
        acceptance = {
            job.SETTING_AGREEMENT_ID: AGREEMENT_ID,
            job.SETTING_AGREEMENT_VERSION: AGREEMENT_VERSION,
            job.SETTING_AGREEMENT_SHA256: sha,
            job.SETTING_AGREEMENT_ACCEPTED_AT: _now_iso(),
            job.SETTING_DISPLAY_NAMED: named,
            job.SETTING_DISPLAY_NAME: display_name if named else "",
        }

        # 1. the release, built by the job; nothing touches the disk or ES
        try:
            release, counts = job.build_release(
                es,
                org_esid=org_esid,
                team_esids=team_esids,
                countries=countries,
                test_data=state["test_data"],
                org={**org, **acceptance},
            )
        except (job.ExportRefused, ValueError) as exc:
            raise PublishError(f"The export refused: {exc}") from exc
        # 2. the one network call (the key is decrypted here and used only for the header)
        status, body = post_release(str(org[SETTING_GATEWAY_URL]), _partner_key(org), release)
        if status != 202:
            detail = _error_detail(body)
            raise PublishError(f"The gateway refused the release (HTTP {status}{': ' + detail if detail else ''}).")

        # 3. the acceptance and the receipt, together, in one write
        receipt = {
            SETTING_LAST_RELEASE_ID: receipt_value(body.get("releaseId")),
            SETTING_LAST_RELEASE_HASH: receipt_value(body.get("manifestHash")),
            SETTING_LAST_PUBLISHED_AT: _now_iso(),
        }
        org = _write_org(org_esid, {**acceptance, **receipt})
        logging.info(
            "publish_global_church: organisation %s published %s rows (release %s)",
            org_esid,
            len(release["rows"]),
            receipt[SETTING_LAST_RELEASE_ID],
        )
    except PublishError as exc:
        return _render(state, 400, error=exc.public_message)
    state = _page_state(es, org_esid, org, form, default_all=False)
    summary = job.summarize(release, counts)
    return _render(state, 200, published={"receipt": receipt, "summary": summary})


# ── Sharing switch ─────────────────────────────────────────────────


def _render_sharing(org_esid: str, org: dict, status: int = 200, **extra):
    text, sha = load_agreement()
    enabled = GlobalChurchSharingService.is_enabled(org)
    state = {
        "org_esid": org_esid,
        "org_title": org.get("organization_title") or org_esid,
        "enabled": enabled,
        "changed_at": org.get("gc_sharing_changed_at"),
        "changed_by": org.get("gc_sharing_changed_by"),
        "accepted_agreement": org.get("gc_sharing_agreement_version"),
        "accepted_sha256": org.get("gc_sharing_agreement_sha256"),
        "agreement_html": render_agreement_html(text),
        "agreement_sha256": sha,
        "agreement_id": AGREEMENT_ID,
        "agreement_version": AGREEMENT_VERSION,
        "what_leaves": WHAT_LEAVES,
        "what_stays": WHAT_STAYS,
        "off_note": OFF_NOTE,
        "is_owner": _caller_is_owner(org),
        "last_release": {
            "id": org.get(SETTING_LAST_RELEASE_ID),
            "at": org.get(SETTING_LAST_PUBLISHED_AT),
        },
        "publish_url": url_for("publish_global_church.dialog", org_esid=org_esid) if enabled else None,
        "cancel_url": url_for("edit_asset", asset_type="organizations", item_id=org_esid),
    }
    return render_template("publish_global_church_sharing.html", **state, **extra), status


def _audit_sharing(username: str, org_esid: str, action: str, ok: bool, detail: str = "") -> None:
    AuditLogService.log_event(
        username,
        "global_church_sharing",
        ok,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", ""),
        detail=f"org={org_esid} action={action} source=settings agreement={AGREEMENT_ID} v{AGREEMENT_VERSION}"
        + (f" error={detail}" if detail else ""),
    )


@publish_global_church_bp.route("/<org_esid>/sharing", methods=["GET"])
def sharing(org_esid: str):
    """The organisation's sharing page: the switch, what leaves, what stays, the agreement."""
    org, denied = _require_org_owner(org_esid, allow_superuser=True, require_sharing=False)
    if denied:
        return denied
    return _render_sharing(org_esid, org)


@publish_global_church_bp.route("/<org_esid>/sharing", methods=["POST"])
def sharing_update(org_esid: str):
    """Turn sharing on (with the acceptance box ticked) or off. Owner only."""
    org, denied = _require_org_owner(org_esid, allow_superuser=False, require_sharing=False)
    if denied:
        return denied
    action = str(request.form.get("action") or "")
    username = session.get("username") or ""
    try:
        if action == "enable":
            if request.form.get("accept") != "yes":
                raise SharingError("Tick the acceptance box to turn sharing on; nothing changed.")
            org = GlobalChurchSharingService.enable(org_esid, username)
            notice = "Sharing is on. You can publish from the Publish to Global.Church page."
        elif action == "disable":
            org = GlobalChurchSharingService.disable(org_esid, username)
            notice = "Sharing is off. No further releases will be published. " + OFF_NOTE
        else:
            raise SharingError("Unknown action; nothing changed.")
    except SharingError as exc:
        _audit_sharing(username, org_esid, action, False, exc.public_message)
        return _render_sharing(org_esid, org, 400, error=exc.public_message)
    _audit_sharing(username, org_esid, action, True)
    return _render_sharing(org_esid, org, notice=notice)


@publish_global_church_bp.app_context_processor
def _feature_flag_template_global() -> dict:
    """``feature_enabled('FLAG')`` for templates that show a link only while a
    flag is on (the organisation form's Global.Church section). A callable, so
    nothing is read unless a template asks."""
    from asset_management.web.services.feature_flag_service import FeatureFlagService

    return {"feature_enabled": FeatureFlagService.is_enabled}

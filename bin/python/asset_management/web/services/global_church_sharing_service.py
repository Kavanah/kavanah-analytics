"""Global.Church sharing switch — the per-organisation opt-in.

Sharing with the Global.Church shared map is **off** for every organisation
until its workspace owner turns it on, at signup or later from the
organisation's sharing page. Turning it on records who did it, when, and
which version of the data-sharing agreement (id, version, SHA-256 of the
text) they accepted, and mints the organisation's per-team label salt
 if it has none. Turning it off stops every future publish —
the dialog becomes unreachable and the export job refuses — but
recalls nothing already published; withdrawal of past releases is a
request to Global.Church under §7 of the agreement. The salt is kept
across off/on so labels stay stable.

The organisation document is the only store. Before the first write the
service heals the live ``de_asset_organization`` mapping with every
``gc_*`` field the schema declares (``ensure_mapping_fields``): the index
on prod predates these fields and ``create_index_if_not_exists`` never
alters an existing index.

This module also owns the agreement text and its rendering, so the
signup step, the sharing page and the publish dialog show the same
words and the same hash.
"""

from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from asset_management.jobs import export_release_product as job
from lib.es_helpers import ensure_mapping_fields, get_elasticsearch_client
from lib.user_errors import UserFacingError
from migration.schema_loader import get_schema_for_asset_index

try:
    from web.services.asset_service import AssetService
except ImportError:
    from .asset_service import AssetService

# ── Agreement ─────────────────────────────────────────────────────────────────

AGREEMENT_ID = "digital-aggregates"
AGREEMENT_VERSION = "0.2"
AGREEMENT_FILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "agreements", f"{AGREEMENT_ID}-v{AGREEMENT_VERSION}.md")
)


def agreement_sha256(text: str) -> str:
    """The hash of record: SHA-256 of the text with leading and trailing
    whitespace stripped. Global.Church stores the agreement trimmed and its
    receiver checks this value (Paul, issue #2, 2026-10-31), so a trailing
    newline in our copy of the file does not change the hash."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def load_agreement() -> tuple[str, str]:
    """The agreement text and its hash of record (``agreement_sha256``), read at request time."""
    with open(AGREEMENT_FILE, "rb") as fh:
        text = fh.read().decode("utf-8")
    return text, agreement_sha256(text)


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


# ── The switch ────────────────────────────────────────────────────────────────

SETTING_SHARING_ENABLED = job.SETTING_SHARING_ENABLED
SETTING_SHARING_CHANGED_AT = "gc_sharing_changed_at"
SETTING_SHARING_CHANGED_BY = "gc_sharing_changed_by"
SETTING_SHARING_AGREEMENT_VERSION = "gc_sharing_agreement_version"
SETTING_SHARING_AGREEMENT_SHA256 = "gc_sharing_agreement_sha256"
SETTING_TEAM_LABEL_SALT = job.SETTING_TEAM_LABEL_SALT
SALT_BYTES = 16  # 32 hex chars

# The plain-words explanation every surface shows beside the switch.
WHAT_LEAVES: tuple[str, ...] = (
    "per country, per month: people exposed (ad impressions and video views), interactions (clicks and sessions), "
    "touchpoints (conversations started, by the seeker's country) and active conversations",
    "which countries you chose to include, and which sources the counts came from",
    "an opaque per-team identifier with each row, never a team name",
    "whether your organisation is named on the map, and the agreement version you accepted",
)
WHAT_STAYS: tuple[str, ...] = (
    "every conversation, contact, group and seeker identifier",
    "responder names, campaigns, ad sets, creatives, budgets and spend",
    "platform account identifiers, regions, cities, and anything below country grain",
)
OFF_NOTE = (
    "Turning sharing off stops every future release. It does not recall releases already published; "
    "to withdraw those, write to Global.Church at the contact in the agreement (section 7)."
)


class SharingError(UserFacingError):
    """A refusal shown to the owner in our own words; nothing changed."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class GlobalChurchSharingService:
    """Read and flip one organisation's Global.Church sharing switch."""

    @staticmethod
    def is_enabled(org: dict | None) -> bool:
        return job._bool((org or {}).get(SETTING_SHARING_ENABLED))

    @staticmethod
    def gc_mapping() -> dict:
        """``{"properties": {<every gc_* field the organisation schema declares>}}``."""
        schema = get_schema_for_asset_index(job.ORG_INDEX)
        props = (schema.get("mappings") or schema).get("properties") or {}
        return {"properties": {k: v for k, v in props.items() if k.startswith("gc_")}}

    @staticmethod
    def ensure_mapping(es=None) -> set[str]:
        """Add-only heal of the live organisation index with the gc_* fields (fail-soft)."""
        es = es if es is not None else get_elasticsearch_client()
        if es is None:
            return set()
        return ensure_mapping_fields(es, job.ORG_INDEX, GlobalChurchSharingService.gc_mapping())

    @staticmethod
    def _write(org_esid: str, updates: dict) -> dict:
        existing = AssetService.get_by_id(job.ORG_INDEX, org_esid)
        if not existing:
            raise SharingError("The organisation could not be found.")
        GlobalChurchSharingService.ensure_mapping()
        doc = {**existing, **updates}
        if not AssetService.update(job.ORG_INDEX, org_esid, doc):
            raise SharingError("The organisation record could not be updated; nothing changed.")
        return doc

    @staticmethod
    def enable(org_esid: str, username: str) -> dict:
        """Turn sharing on for ``org_esid``: record who, when and which agreement
        text (id, version, hash) was accepted, and mint the team-label salt if
        the organisation has none yet. Returns the updated document."""
        _text, sha = load_agreement()
        existing = AssetService.get_by_id(job.ORG_INDEX, org_esid) or {}
        salt = str(existing.get(SETTING_TEAM_LABEL_SALT) or "").strip()
        if len(salt) < job.TEAM_LABEL_SALT_MIN_CHARS:
            salt = secrets.token_hex(SALT_BYTES)
        doc = GlobalChurchSharingService._write(
            org_esid,
            {
                SETTING_SHARING_ENABLED: True,
                SETTING_SHARING_CHANGED_AT: _now_iso(),
                SETTING_SHARING_CHANGED_BY: username or "",
                SETTING_SHARING_AGREEMENT_VERSION: f"{AGREEMENT_ID} {AGREEMENT_VERSION}",
                SETTING_SHARING_AGREEMENT_SHA256: sha,
                SETTING_TEAM_LABEL_SALT: salt,
            },
        )
        logging.info("GlobalChurchSharingService: sharing ON for organisation %s by %s", org_esid, username)
        return doc

    @staticmethod
    def disable(org_esid: str, username: str) -> dict:
        """Turn sharing off. The salt and the last-release receipt are kept: a
        later re-enable keeps the labels stable, and nothing published is recalled."""
        doc = GlobalChurchSharingService._write(
            org_esid,
            {
                SETTING_SHARING_ENABLED: False,
                SETTING_SHARING_CHANGED_AT: _now_iso(),
                SETTING_SHARING_CHANGED_BY: username or "",
            },
        )
        logging.info("GlobalChurchSharingService: sharing OFF for organisation %s by %s", org_esid, username)
        return doc

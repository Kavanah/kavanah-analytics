"""Per-credential asset lists for the credential-health sweep.

 wired asset-level probing into the sweep but never fed it a list — every
``de_credential_health`` row was written with ``assets: []``, so anything built on
those per-asset probes could never fire.
This module is the source→asset join the sweep comment deferred.

Two families, two joins, one shape (the prober's ``{"asset_type", "asset_id",
"asset_name", "parent_page_id"?}``):

* **Legacy ``de_asset_fb_app_users``** — a system-user token is tried by the
  importers for every team whose page chain reaches its app
  (team → fb_pages → fb_account → fb_app, ``resolve_team_filter_scope``). So the
  token's asset list is the union of those teams' configured pages, ad accounts
  and IG accounts (``team_assets_for_coverage`` — the same statement of "what
  this team needs" uses for a brand-new system-user connection),
  narrowed by the user's ROLE: ``Pages`` tokens get pages + IGs, ``Ads`` tokens
  get the ad accounts pinned to them, an unknown role gets everything.
* **Wizard ``de_connection``** — the connection's own ``de_connection_asset``
  rows, plus the configured assets of every team
  that points at it via ``connection_esid``, plus every ``de_asset_ig_accounts``
  doc stamped with its ``wizard_connection_esid`` — because THAT stamp, on the IG
  doc, is what makes the IG importer try this connection first
  (``get_instagram_insights._resolve_wizard_ig_credentials``).

IG accounts carry ``parent_page_id`` whenever their asset doc links a page, so
the prober can report a downstream IG failure as caused by its page instead of
as a second independent fault. The page id is read through ``fb_page_esid``, not
off the doc's own ``fb_page_id`` — that copy holds a page NAME on most docs
 and silently matched nothing. Wizard-created docs carry neither and
link the page in ``linked_page_id`` instead.

Everything here is read-only and best-effort: a join that fails logs and
contributes nothing, so one bad row cannot take the sweep down (CLAUDE.md #45).
An empty list means "token-level health only" — exactly today's behaviour.
"""

from __future__ import annotations

import logging
from typing import Any

# The FB-continuity aggregation shape, keyed here by asset id.
# data_quality_service imports only lib — no Flask app comes with it.
from asset_management.web.services.data_quality_service import _latest_date_by_index

# DEAM's de_connection_asset vocabulary -> the prober's. Same table as
# analytics.routes._ASSET_TYPE_TO_PROBE; duplicated rather than
# imported because pulling a Flask blueprint into a cron job drags the app in.
ASSET_TYPE_TO_PROBE = {
    "fb_page": "page",
    "fb_ad_account": "ad_account",
    "ig_account": "ig",
}


# where each asset type's imports land, per team (esid-form index
# ``{team_esid.lower}__{suffix}`` canon). Every one of these
# importers stamps ``kibana_date`` on each doc at the send choke point.
IMPORT_INDEX_SUFFIX = {
    "page": "fb_page_insights",
    "ig": "ig_insights",
    "ad_account": "fb_insights",
}
# the asset-id field each family's importer stamps on every doc
# (mapped keyword in the unified schemas). Recency is aggregated per asset id,
# not per team index: a team can hold several ad accounts, and one of them
# being deleted at Meta must not be masked by its siblings still importing.
IMPORT_ID_FIELD = {
    "page": "fb_page_id",
    "ig": "ig_user_id",
    "ad_account": "ad_account_id",
}
_IMPORT_DATE_FIELD = "kibana_date"


def _bare_ad_account_id(value: Any) -> str:
    """``act_123`` and ``123`` name the same account; compare them bare."""
    v = str(value or "").strip()
    return v[4:] if v.startswith("act_") else v


class AssetScope:
    """Resolve every join once per sweep, then answer per credential.

    Built lazily: nothing is read until the first ``for_*`` call, so a sweep with
    zero credentials costs zero extra queries.
    """

    def __init__(self, es) -> None:
        self._es = es
        self._built = False
        self._team_assets: dict[str, list[dict[str, Any]]] = {}
        self._app_to_teams: dict[str, set[str]] = {}
        self._connection_to_teams: dict[str, set[str]] = {}
        self._connection_to_igs: dict[str, list[dict[str, Any]]] = {}
        self._ig_parent_page: dict[str, str] = {}
        self._page_id_by_esid: dict[str, str] = {}
        # bare ad-account id -> the de_asset_fb_app_users esid pinned on its doc
        self._ad_account_pin: dict[str, str] = {}
        # numeric page id -> its fb_account esid, and fb_account esid ->
        # the apps (with at least one app user) linked to it
        self._page_account: dict[str, str] = {}
        self._account_apps: dict[str, set[str]] = {}

    # ── public ──────────────────────────────────────────────────────────────

    def for_fb_app_user(self, fb_app_esid: str, *, role: str = "", user_esid: str = "") -> list[dict[str, Any]]:
        """Assets a legacy app-user token is expected to reach.

        Role-aware, because the importers are: the page and IG importers walk
        every user under the app but the ``Pages``-role token is the one meant
        to read pages/IGs (``get_fb_app_user_for_pages`` requires it), while the
        ad importer uses only the user PINNED on each ad account doc
        (``de_asset_fb_ad_accounts.fb_app_user_esid``, ``get_insights``). Probing
        an ``Ads`` token against pages just reports the ``pages_read_engagement``
        it was never granted — a permission_lost banner for a failure that is by
        design. So:

        * ``Pages`` -> pages + IG accounts;
        * ``Ads``   -> ad accounts, and only those pinned to THIS user (an
          unpinned ad account is not imported through the legacy chain at all);
        * unknown  -> everything (fail towards probing, not silence).

        Pages are further narrowed to the app the page importer would use
        (``_page_belongs_to``): a team can hold pages from several
        Business accounts (partner projects), and the importer reads each page
        only with the app linked to that page's own account.
        """
        self._build()
        role_l = str(role or "").strip().lower()
        if role_l == "pages":
            want = {"page", "ig"}
        elif role_l == "ads":
            want = {"ad_account"}
        else:
            want = {"page", "ig", "ad_account"}
        out: list[dict[str, Any]] = []
        for team_esid in sorted(self._app_to_teams.get(str(fb_app_esid or ""), ())):
            for a in self._assets_for_team(team_esid):
                if a.get("asset_type") not in want:
                    continue
                if a.get("asset_type") == "ad_account" and not self._ad_account_belongs_to(a, user_esid):
                    continue
                if a.get("asset_type") == "page" and not self._page_belongs_to(a, fb_app_esid):
                    continue
                out.append(a)
        return self._dedupe(out)

    def for_connection(self, connection_esid: str) -> list[dict[str, Any]]:
        """Assets a wizard connection is expected to reach."""
        self._build()
        cid = str(connection_esid or "")
        out: list[dict[str, Any]] = list(self._connection_asset_rows(cid))
        for team_esid in sorted(self._connection_to_teams.get(cid, ())):
            out.extend(self._assets_for_team(team_esid))
        out.extend(self._connection_to_igs.get(cid, ()))
        return self._dedupe(out)

    def import_recency(self) -> dict[tuple[str, str], str]:
        """newest imported doc date per ``(asset_type, asset_id)``.

        Answered for every asset of every team this scope has resolved so far
        (call it AFTER the ``for_*`` pass). One aggregation per family over
        ``*__{suffix}``: terms on the family's asset-id field, max
        ``kibana_date`` — so the value is the asset's OWN newest doc across every
        team's index, never a sibling's. ``yyyy-MM-dd``, or ``""`` when the
        family was searched and no doc carries this asset's id (never imported).

        A family whose aggregation failed contributes NOTHING: its assets are
        absent from the map, which the classifier reads as unknown. It must not
        degrade to ``""`` — one unavailable shard would otherwise stamp every
        code-100 asset of that family as "never imported" and persist it.
        Ad-account ids are compared bare (``act_`` stripped), matching
        ``_bare_ad_account_id``.
        """
        self._build()
        latest_by_family: dict[str, dict[str, str]] = {}
        for a_type, suffix in IMPORT_INDEX_SUFFIX.items():
            id_field = IMPORT_ID_FIELD[a_type]
            try:
                latest_by_family[a_type] = _latest_date_by_index(
                    self._es, f"*__{suffix}", _IMPORT_DATE_FIELD, key_field=id_field
                )
            except Exception as exc:  # noqa: BLE001 — unknown recency is safe; "" would not be
                logging.warning(
                    "credential_asset_scope: import recency for *__%s (%s) failed: %s — recency unknown for %s assets",
                    suffix,
                    id_field,
                    type(exc).__name__,
                    a_type,
                )

        out: dict[tuple[str, str], str] = {}
        for assets in self._team_assets.values():
            for a in assets:
                a_type = str(a.get("asset_type") or "")
                if a_type not in latest_by_family:
                    continue
                a_id = str(a.get("asset_id") or "")
                by_id = latest_by_family[a_type]
                if a_type == "ad_account":
                    bare = _bare_ad_account_id(a_id)
                    date = by_id.get(bare) or by_id.get(f"act_{bare}") or ""
                else:
                    date = by_id.get(a_id) or ""
                out[(a_type, a_id)] = date
        return out

    # ── joins ───────────────────────────────────────────────────────────────

    def _build(self) -> None:
        if self._built:
            return
        self._built = True
        self._index_teams()
        self._index_page_ids()
        self._index_ig_docs()
        self._index_ad_account_pins()
        self._index_account_apps()

    def _index_teams(self) -> None:
        """team -> (apps reachable through its page chain, connection it points at)."""
        try:
            from import_scripts.facebook.lib.assets import get_fb_teams, resolve_team_filter_scope
        except Exception as exc:  # noqa: BLE001 — import boundary
            logging.warning("credential_asset_scope: importer asset helpers unavailable: %s", exc)
            return
        try:
            teams = get_fb_teams(filters={"ds_facebook": "true"}) or []
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: cannot list ds_facebook teams: %s", exc)
            return
        for t in teams:
            team_esid = str(t.get("_id") or "")
            src = t.get("_source") or {}
            if not team_esid:
                continue
            # The populated link is connection_esid (37/38 teams on prod);
            # wizard_connection_esid is present on ZERO team docs but is what
            # get_page_insights reads, so both are honoured (see
            # credential_health.health_for_fb_team for the same reasoning).
            for field in ("connection_esid", "wizard_connection_esid"):
                cid = str(src.get(field) or "").strip()
                if cid:
                    self._connection_to_teams.setdefault(cid, set()).add(team_esid)
            try:
                scope = resolve_team_filter_scope(team_esid) or {}
            except Exception as exc:  # noqa: BLE001 — one team must not sink the sweep
                logging.warning("credential_asset_scope: scope for team %s failed: %s", team_esid, exc)
                continue
            for app_esid in scope.get("fb_app_esids") or ():
                self._app_to_teams.setdefault(str(app_esid), set()).add(team_esid)

    def _index_page_ids(self) -> None:
        """page esid -> Meta's own numeric page id — the authority."""
        try:
            res = self._es.search(
                index="de_asset_fb_pages",
                body={"size": 5000, "query": {"match_all": {}}, "_source": ["fb_page_id", "fb_account_esid"]},
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: cannot read de_asset_fb_pages: %s", exc)
            return
        for h in res.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            pid = str(src.get("fb_page_id") or "").strip()
            esid = str(h.get("_id") or "")
            if pid and esid:
                self._page_id_by_esid[esid] = pid
            account = str(src.get("fb_account_esid") or "").strip()
            if pid and account:
                self._page_account[pid] = account

    def _index_account_apps(self) -> None:
        """fb_account -> the apps linked to it that have at least one app user.

        Mirrors the page importer's ``fb_account_to_app`` (``get_page_insights``):
        an app with no users has no token, and the importer skips it.
        """
        try:
            apps = self._es.search(
                index="de_asset_fb_apps",
                body={"size": 2000, "query": {"match_all": {}}, "_source": ["fb_account_esid"]},
            )
            users = self._es.search(
                index="de_asset_fb_app_users",
                body={"size": 5000, "query": {"match_all": {}}, "_source": ["fb_app_esid"]},
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: cannot read fb apps / app users: %s", exc)
            return
        apps_with_users = {
            str((h.get("_source") or {}).get("fb_app_esid") or "") for h in users.get("hits", {}).get("hits", [])
        }
        for h in apps.get("hits", {}).get("hits", []):
            app_esid = str(h.get("_id") or "")
            account = str((h.get("_source") or {}).get("fb_account_esid") or "").strip()
            if app_esid and account and app_esid in apps_with_users:
                self._account_apps.setdefault(account, set()).add(app_esid)

    def _parent_page_id(self, src: dict[str, Any]) -> str:
        """Meta's numeric page id for one IG asset doc, or "".

        ``fb_page_id`` on the IG doc is a denormalised copy, and the CSV
        importer stored the operator's "Facebook Page" cell there verbatim — a
        page NAME on 19 of 34 staging docs. A name can never equal a
        page probe's ``asset_id``, so ``probe_assets``' ``caused_by`` shield was
        inert for all of them and every dependent IG failure was reported as a
        second independent fault, with nothing raised anywhere.

        ``fb_page_esid`` is the real link and resolved correctly even on the
        affected docs, so read through it. The copy is accepted only when it is
        self-evidently an id; a leftover name is dropped rather than carried,
        because Graph resolves page vanity names and a name that happens to
        round-trip would make the shield fire on a page we never probed.

        wizard-created docs carry neither field. Both wizard IG writers
        store the page Graph returned in ``linked_page_id``, so it is the last
        fallback, under the same numeric-only rule as the copy.
        """
        esid = str(src.get("fb_page_esid") or "").strip()
        via_esid = self._page_id_by_esid.get(esid, "")
        if via_esid:
            return via_esid
        raw = str(src.get("fb_page_id") or "").strip()
        if raw.isdigit():
            return raw
        linked = str(src.get("linked_page_id") or "").strip()
        return linked if linked.isdigit() else ""

    def _index_ig_docs(self) -> None:
        """IG asset docs: parent page for causation, and the wizard-connection stamp."""
        try:
            res = self._es.search(
                index="de_asset_ig_accounts",
                body={
                    "size": 2000,
                    "query": {"match_all": {}},
                    "_source": [
                        "ig_account_id",
                        "ig_account_username",
                        "ig_account_title",
                        "fb_page_id",
                        "fb_page_esid",
                        "linked_page_id",
                        "wizard_connection_esid",
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: cannot read de_asset_ig_accounts: %s", exc)
            return
        for h in res.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            ig_id = str(src.get("ig_account_id") or "").strip()
            if not ig_id:
                continue
            page = self._parent_page_id(src)
            if page:
                self._ig_parent_page[ig_id] = page
            cid = str(src.get("wizard_connection_esid") or "").strip()
            if cid:
                row: dict[str, Any] = {
                    "asset_type": "ig",
                    "asset_id": ig_id,
                    "asset_name": str(src.get("ig_account_username") or src.get("ig_account_title") or ""),
                }
                if page:
                    row["parent_page_id"] = page
                self._connection_to_igs.setdefault(cid, []).append(row)

    def _index_ad_account_pins(self) -> None:
        """ad account -> the app user the ad importer authenticates it with."""
        try:
            res = self._es.search(
                index="de_asset_fb_ad_accounts",
                body={
                    "size": 2000,
                    "query": {"exists": {"field": "fb_app_user_esid"}},
                    "_source": ["fb_ad_account_id", "fb_app_user_esid"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: cannot read de_asset_fb_ad_accounts: %s", exc)
            return
        for h in res.get("hits", {}).get("hits", []):
            src = h.get("_source") or {}
            aid = _bare_ad_account_id(src.get("fb_ad_account_id"))
            pin = str(src.get("fb_app_user_esid") or "").strip()
            if aid and pin:
                self._ad_account_pin[aid] = pin

    def _ad_account_belongs_to(self, asset: dict[str, Any], user_esid: str) -> bool:
        """Is this ad account imported through ``user_esid``?

        Pinned to someone else -> no. Unpinned -> no as well: ``get_insights``
        resolves the token from the pin, so an unpinned account is never imported
        by the legacy chain and probing it says nothing about imports. With no
        ``user_esid`` to compare against (unknown caller) fall back to probing.
        """
        if not user_esid:
            return True
        pin = self._ad_account_pin.get(_bare_ad_account_id(asset.get("asset_id")))
        return pin == str(user_esid)

    def _page_belongs_to(self, asset: dict[str, Any], fb_app_esid: str) -> bool:
        """Would the page importer read this page with ``fb_app_esid``'s token?

        . The importer uses only the app linked to the page's own
        fb_account, and tries every app when that account has no app with a
        user (``get_page_insights``). Probing a partner's page with a token the
        importer never uses for it reported a permanent, false permission loss
        (seen on prod: one partner's Pages token against the other partner's
        page on a shared team, code 10). Unknown ownership keeps probing.
        """
        owners = self._account_apps.get(self._page_account.get(str(asset.get("asset_id") or ""), ""))
        return not owners or str(fb_app_esid) in owners

    def _assets_for_team(self, team_esid: str) -> list[dict[str, Any]]:
        if team_esid in self._team_assets:
            return self._team_assets[team_esid]
        assets: list[dict[str, Any]] = []
        try:
            from asset_management.web.services.system_user_token_service import team_assets_for_coverage

            assets = list(team_assets_for_coverage(team_esid) or [])
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: team assets for %s failed: %s", team_esid, exc)
        # team_assets_for_coverage only claims an IG's parent page when the
        # pairing is unambiguous; the IG doc's page link is authoritative
        # (resolved through fb_page_esid — see _parent_page_id).
        for a in assets:
            if a.get("asset_type") == "ig" and not a.get("parent_page_id"):
                parent = self._ig_parent_page.get(str(a.get("asset_id") or ""))
                if parent:
                    a["parent_page_id"] = parent
        self._team_assets[team_esid] = assets
        return assets

    def _connection_asset_rows(self, connection_esid: str) -> list[dict[str, Any]]:
        if not connection_esid:
            return []
        try:
            from asset_management.web.services.connection_service import ConnectionService

            hits = ConnectionService.list_assets_for_connection(connection_esid) or []
        except Exception as exc:  # noqa: BLE001
            logging.warning("credential_asset_scope: connection assets for %s failed: %s", connection_esid, exc)
            return []
        out: list[dict[str, Any]] = []
        for hit in hits:
            src = hit.get("_source") or {}
            probe_type = ASSET_TYPE_TO_PROBE.get(str(src.get("asset_type") or ""))
            if not probe_type:
                continue  # not a Meta asset we know how to touch
            aid = str(src.get("provider_asset_id") or "").strip()
            if not aid:
                continue
            row: dict[str, Any] = {"asset_type": probe_type, "asset_id": aid, "asset_name": src.get("label") or ""}
            if probe_type == "ig" and self._ig_parent_page.get(aid):
                row["parent_page_id"] = self._ig_parent_page[aid]
            out.append(row)
        return out

    @staticmethod
    def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """First occurrence wins; pages first so the prober's dependency order holds."""
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for r in rows:
            key = (str(r.get("asset_type") or ""), str(r.get("asset_id") or ""))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            out.append(dict(r))
        out.sort(key=lambda r: 0 if r["asset_type"] == "page" else 1)
        return out

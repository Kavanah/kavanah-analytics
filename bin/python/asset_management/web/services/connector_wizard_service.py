"""
Connector Wizard Service

Orchestrates the multi-step setup wizards exposed by the Data Connectors page.
Each wizard creates or links the connector-specific records (Echo tenant,
Chatwoot account, FB account/app/app_user/pages) and updates the team to
flag the data source enabled.

Calls AssetService directly (not the form-based /asset/<type>/add endpoint)
so the wizard can be JSON-driven and return the new ESIDs synchronously.
Encryption is handled by reusing FormService.process_form_data — keeping
the encryption logic in one place across the form and wizard write paths.
"""

import logging
import sys
from typing import Any

sys.path.insert(0, "/app/python")

try:
    from web.services.asset_service import AssetService
    from web.services.form_service import FormService
    from web.asset_configs import ASSET_CONFIGS
except ImportError:
    from .asset_service import AssetService
    from .form_service import FormService
    from ..asset_configs import ASSET_CONFIGS

from lib.chatwoot_base_url import ChatwootBaseUrlError, normalize_chatwoot_base_url
from lib.echo_base_url import EchoSubdomainError, normalize_echo_subdomain
from lib.fb_constants import FB_GRAPH_URL
from lib.errors import UserFacingError

TEAMS_INDEX = "de_asset_teams"


class WizardError(UserFacingError):
    """Raised when a wizard step cannot complete.

    an optional ``needs_authorization`` signal. When True, the wizard
    step did not fail outright; it discovered that the chosen account must be
    OAuth-authorized before it can be linked. The route turns this into a typed
    ``error_kind=needs_authorization`` response carrying ``customer_id`` /
    ``customer_title`` so the frontend can open the Authorize (OAuth) step
    instead of surfacing a hard error. Plain ``WizardError`` (the default)
    keeps its prior hard-failure semantics.
    """

    def __init__(
        self,
        message: str = "",
        *,
        needs_authorization: bool = False,
        customer_id: str = "",
        customer_title: str = "",
    ) -> None:
        super().__init__(message)
        self.needs_authorization = needs_authorization
        self.customer_id = customer_id
        self.customer_title = customer_title


def _prune_dangling_esids(esids: list[str], asset_index: str) -> tuple[list[str], list[str]]:
    """drop ESIDs that no longer resolve to a doc in ``asset_index``.

    The GA4 and YouTube wizards keep a team's linked assets in an array field
    (``team_ga4_property_esids`` / ``team_youtube_channel_handle_esids``) and
    APPEND on each re-connect. earlier a stale ESID — left behind when its
    asset was deleted out from under the team — survived in the array forever,
    so reconnecting could never clear the "Needs attention" state it caused.
    Pruning at re-connect time means a successful re-link also tidies the array.

    Order-preserving. On any lookup error the ESID is KEPT (fail-open): a
    transient ES blip must not silently unlink a live asset. Only an ESID that
    resolved cleanly to a missing doc is dropped.

    Returns ``(kept, dropped)`` — callers audit-log each dropped
    esid via :func:`_audit_pruned_references` AFTER the team write lands, so
    the trail only records prunes that actually persisted.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for esid in esids:
        if not esid:
            continue
        try:
            doc = AssetService.get_by_id(asset_index, esid)
        except Exception as e:  # noqa: BLE001 — fail-open: keep on lookup error
            logging.warning("_prune_dangling_esids: lookup failed for %s/%s: %s", asset_index, esid, e)
            kept.append(esid)
            continue
        if doc:
            kept.append(esid)
        else:
            logging.info("_prune_dangling_esids: dropping dangling %s/%s", asset_index, esid)
            dropped.append(esid)
    return kept, dropped


def _audit_pruned_references(
    dropped: list[str],
    asset_index: str,
    field: str,
    team_esid: str,
    team_doc: dict[str, Any],
) -> None:
    """audit-log each dangling esid the configure-time prune
    removed from a team's link array. Called after the team write succeeds.
    Fail-soft — audit emission never fails the wizard.
    """
    if not dropped:
        return
    try:
        try:
            from web.services.audit_log_service import AuditLogService
        except ImportError:
            from .audit_log_service import AuditLogService

        for esid in dropped:
            AuditLogService.log_reference_removed(
                path="configure_prune",
                child_index=TEAMS_INDEX,
                child_id=team_esid,
                child_title=str(team_doc.get("team_title") or ""),
                field=field,
                removed_esid=esid,
                source_index=asset_index,
            )
    except Exception as e:  # noqa: BLE001 — attribution only; wizard must proceed
        logging.warning("_audit_pruned_references: audit emission failed: %s", e)


def _youtube_picker_label(title: str, source: dict[str, Any], max_channels: int = 3) -> str:
    """disambiguate same-titled YouTube account docs by the channels
    the doc's authorized identity OWNS.

    One Google login can front many Brand Account identities, and the data model
    correctly stores one doc per identity (one doc = one token = one identity,
    ). But title + email are identical across those docs, so the picker
    and the Edit-channels dialog show exact duplicates with no way to tell which
    channel each serves.

    ``youtube_account_owned_channel_titles`` is stamped by the OAuth callback
     and, until now, read by nothing in the web layer. It is
    the only field that actually distinguishes these docs, so it becomes the
    suffix: ``"Example Initiative — TEAM_NORTH"``.

    Deliberately additive: a doc with no stamped titles (never authorized, or a
    legacy grant whose owned set was never probed) keeps its bare title rather
    than gaining a misleading marker. Truncated at ``max_channels`` so a doc
    owning many channels doesn't produce an unreadable option.

    ``authorized_as`` framing matters. The suffix names what the doc's token
    OWNS, which is not necessarily what is LINKED to it — on prod,
    jVj4LpwBIm2AdJKusRc6 owns only one team while 12 channels point at it
    . Wording the suffix as bare channel names would imply "this group
    serves these channels" and read as a mislabel wherever the two diverge, so
    it is explicitly phrased as the authorizing identity instead.
    """
    raw = source.get("youtube_account_owned_channel_titles") or []
    names = [str(t).strip() for t in raw if str(t).strip()]
    if not names:
        return title
    shown = names[:max_channels]
    suffix = ", ".join(shown)
    if len(names) > max_channels:
        suffix += f", +{len(names) - max_channels} more"
    return f"{title} — authorized as: {suffix}"


def list_assets_for_picker(
    asset_type: str,
    allowed_workspaces: list[str] | set[str] | None = None,
) -> list[dict[str, str]]:
    """Return [{"esid": ..., "title": ...}, ...] for the wizard's
    "pick existing" dropdowns.

    Uses the asset's ``title_field`` from ASSET_CONFIGS as the display label.

     (2026-05-27): narrow the option list to the operator's
    workspace scope. ``allowed_workspaces`` is the resolved
    ``RoleFilterService.get_allowed_workspaces()`` value, threaded down
    from the route (the service has no Flask-session access by design):

    * ``None`` — no workspace filter applies. Superusers / asset_admins
      and any other ``get_allowed_workspaces() is None`` caller see every
      asset, preserving the admin-override semantic.
    * a list/set of ``workspace_esid`` strings — keep only docs whose
      ``workspace_esid`` is in the set. **Fail-closed**: a doc with no
      ``workspace_esid`` value is dropped. The
      ``backfill_asset_workspace_esid`` tool stamps every one of the 8
      picker indices (echo_tenants, chatwoot_accounts, fb_accounts /
      apps / app_users / pages, google_ads_customers, ga_properties), and
      organic creates carry the field, so a missing field post-backfill is
      operator error — not a soft gate to silently widen scope through.
      This mirrors the Asset Explorer list view (``app.py`` ``list_assets``)
      rather than the softer keep-on-missing posture in
      ``FormService.get_select_options``; the ticket calls for the
      stricter boundary on this pick-existing surface.
    * ``[]`` (empty) — deny-all: every doc is dropped (no workspace_esid
      can be in an empty set). A scoped operator with no resolvable
      workspace sees an empty picker rather than the cluster-wide list.
    """
    config = ASSET_CONFIGS.get(asset_type)
    if not config:
        # don't echo asset_type into the client-visible error
        # (CodeQL py/reflective-xss flags request-input → response).
        logging.warning("connector_wizard: unknown asset_type=%r", asset_type)
        raise WizardError("Unknown asset type")

    title_field = config.get("title_field")
    docs = AssetService.get_all(config["index"]) or []

    allowed_ws_set = set(allowed_workspaces) if allowed_workspaces is not None else None

    # for the Google Ads picker, tell the client whether each customer
    # already has a usable OAuth token, so the wizard can route a tokenless pick
    # straight to the Authorize step at Next (instead of discovering it at
    # submit and bouncing the user back from Confirm). Only this asset type
    # carries the extra fields; every other picker keeps its {esid,title} shape.
    is_google_ads = asset_type == "google_ads_customers"

    # YouTube account docs are keyed on the authorized Brand Account
    # IDENTITY, but the picker labels them with title + email. One Google login
    # can front many Brand Accounts, so N identities under one login render as N
    # visually IDENTICAL rows and the user cannot tell which channel each serves
    # (reported this; it was closed Won't-do before the case
    # made it concrete). The callback already stamps the owning identity's
    # channel titles on every doc — surface them as a suffix so the rows differ.
    is_youtube_accounts = asset_type == "youtube_accounts"

    options: list[dict[str, str]] = []
    for d in docs:
        source = d.get("_source") or {}
        # fail-closed workspace narrowing. None set = no filter.
        if allowed_ws_set is not None and source.get("workspace_esid") not in allowed_ws_set:
            continue
        esid = d.get("_id") or ""
        title_val = source.get(title_field) if title_field else None
        if isinstance(title_val, list):
            title_val = ", ".join(str(v) for v in title_val if v)
        option: dict[str, Any] = {"esid": esid, "title": (title_val or esid) or ""}
        if is_youtube_accounts:
            option["title"] = _youtube_picker_label(option["title"], source)
        if is_google_ads:
            # The wizard needs the customer_id (the de_connection natural key) to
            # start OAuth, and the authorized flag to decide whether to. Fail-soft
            # to False (treat unknown as "needs auth" — the server gate still
            # protects the link either way).
            customer_id = str(source.get("google_ads_customer_id") or "").strip()
            option["customer_id"] = customer_id
            option["authorized"] = _has_google_ads_oauth_token(customer_id)
        options.append(option)

    options.sort(key=lambda o: o["title"].lower())
    return options


def _create_asset(asset_type: str, fields: dict[str, Any], doc_id: str | None = None) -> str:
    """Create a new asset record, handling encryption + ESID storage by
    routing through FormService. Returns the new ESID.

    When ``doc_id`` is provided the write goes through ``op_type=create`` (see
    ``AssetService.create_with_error``), so a concurrent / repeated create of the
    SAME deterministic id surfaces as a 409 rather than a duplicate doc.
    uses this for Google Ads customers (id derived from the natural key) to make
    creation idempotent and race-proof; callers that want to REUSE the existing
    doc on a 409 must catch :class:`WizardError` and fall back to the same id.
    """
    config = ASSET_CONFIGS.get(asset_type)
    if not config:
        # don't echo asset_type into the client-visible error
        # (CodeQL py/reflective-xss flags request-input → response).
        logging.warning("connector_wizard: unknown asset_type=%r", asset_type)
        raise WizardError("Unknown asset type")

    is_valid, error = FormService.validate_required_fields(fields, config, mode="add")
    if not is_valid:
        raise WizardError(error)

    processed = FormService.process_form_data(fields, config, mode="add")
    success, error_msg, new_esid = AssetService.create_with_error(config["index"], processed, doc_id=doc_id)
    if not success:
        raise WizardError(error_msg or "Could not create asset")
    return new_esid


def _deterministic_asset_id(*parts: str) -> str:
    """a stable ES ``_id`` derived from an asset's natural key.

    A random per-create ``_id`` (the default) makes the create path race-prone:
    the read-then-create dedup (``_find_*_by_id``) can miss a not-yet-visible
    prior write and mint a duplicate (a 4-doc incident for one org, same read-after-
    write class as). Keying ``_id`` on the natural key instead makes the
    create an idempotent upsert at the ES layer — a repeated create of the same
    key 409s on ``op_type=create`` and the caller reuses the existing doc.

    The id is a short, URL/index-safe hex digest of the joined parts (lowercased,
    stripped). Collisions across DISTINCT keys are negligible; identical keys
    deliberately map to the same id (that IS the dedup). SHA-256 (not SHA-1) —
    the digest is a key, not a security primitive, but there's no reason to use a
    weak hash, and the 20-char truncation is identical either way.
    """
    import hashlib

    norm = "\x1f".join(str(p or "").strip().lower() for p in parts)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:20]


def _stamp_asset_workspace(index: str, asset_esid: str | None, team_workspace_esid: Any) -> None:
    """Stamp ``workspace_esid`` onto a just-created data-source asset doc.

    every connector's create path mints the asset row via
    ``_create_asset`` BEFORE the team (and thus its workspace) is resolved, so
    the row carries no ``workspace_esid``. Asset Explorer's ``list_assets`` and
    the wizard's ``list_assets_for_picker`` both fail CLOSED on a missing
    ``workspace_esid`` (a doc without it is dropped for a workspace-scoped
    operator) — so an un-stamped asset exists in ES, links to the team, and
    imports fine, but is INVISIBLE in Asset Explorer. The (GA4) and
     (YouTube) sweeps stamped those two connectors inline; Google Ads,
    Echo, and Chatwoot were missed. This shared helper closes all three.

    Fail-soft by contract: the caller's team link is already (or about to be)
    persisted, so a stamp failure must not raise — it only means the asset stays
    hidden until the backfill tool tags it. No-ops when the team has no
    workspace_esid or the asset id is empty.

    stamps only a workspace-less doc. Re-stamping an owned row moved
    it (and its data) into the caller's workspace on a pick-existing link.
    """
    team_ws = (str(team_workspace_esid or "").strip()) or None
    if not team_ws or not asset_esid:
        return
    try:
        doc = AssetService.get_by_id(index, asset_esid)
        if doc is not None and not str(doc.get("workspace_esid") or "").strip():
            AssetService.update(index, asset_esid, {**doc, "workspace_esid": team_ws})
    except Exception as e:  # noqa: BLE001 — fail-soft: team link already persisted
        logging.warning(
            "connector_wizard: workspace_esid stamp failed index=%s asset=%s ws=%s err=%s",
            index,
            asset_esid,
            team_ws,
            e,
        )


def _stamp_asset_owning_team(index: str, asset_esid: str | None, team_esid: Any) -> None:
    """stamp ``owning_team_esid`` onto a just-created data-source asset.

    The org-scoped billing rollup (``canonical_connections_for_org``) attributes a
    SHARED asset — one esid linked by teams across multiple orgs — to exactly one
    org: the PRIMARY org of this owning team. Stamping the creating team at mint
    time records that owner so a later cross-link onto another org's team can't
    move the asset into that org's billing. Mirrors ``_stamp_asset_workspace``'s
    pattern + fail-soft contract (the team link is already / about to be persisted,
    so a stamp failure must not raise — the source just keeps its earlier
    behaviour until the backfill tool tags it). No-ops on empty asset id / team.
    """
    team = (str(team_esid or "").strip()) or None
    if not team or not asset_esid:
        return
    try:
        doc = AssetService.get_by_id(index, asset_esid)
        if doc is not None:
            AssetService.update(index, asset_esid, {**doc, "owning_team_esid": team})
    except Exception as e:  # noqa: BLE001 — fail-soft: team link already persisted
        logging.warning(
            "connector_wizard: owning_team_esid stamp failed index=%s asset=%s team=%s err=%s",
            index,
            asset_esid,
            team,
            e,
        )


_TERM_FIELD_CACHE: dict[tuple[str, str], str] = {}


def _termable_field(index: str, base_field: str) -> str:
    """Return the field a ``term`` query must target for ``base_field`` on
    ``index``: ``<base_field>.keyword`` when the field is ``text`` (so the raw
    value survives un-analyzed under the sub-field), else the bare
    ``<base_field>`` when the field is already mapped pure ``keyword``.

    the original dedup hard-coded ``.keyword``. That is correct on
    PROD (``de_asset_*`` string fields are text+keyword after the
    reindex) but WRONG on STAGING, where these fields are explicit bare
    ``keyword`` with no sub-field: ``<field>.keyword`` then does not exist, the
    ``term`` throws "No mapping found", the dedup lookup fail-softs to None, and
    every re-add mints a duplicate (reproduced: two "John 2" youtube_accounts
    docs from one back/forward wizard pass on staging). This is the same
    env-divergent mapping trap hit for sort fields; resolve it
    the same way — probe the live mapping ONCE per (index, field) and cache.

    Fail-soft: on any probe error, fall back to the bare field. A ``term`` on a
    bare keyword always works; the only field where bare is wrong is a pure
    ``text`` field with a keyword sub-field, and that path is covered by the
    explicit text check below.
    """
    cache_key = (index, base_field)
    cached = _TERM_FIELD_CACHE.get(cache_key)
    if cached is not None:
        return cached

    resolved = base_field
    try:
        es = AssetService.get_elasticsearch_client()
        if es is not None:
            mapping = es.indices.get_mapping(index=index)
            index_mappings = next(iter(mapping.values()), {})
            props = index_mappings.get("mappings", {}).get("properties", {})
            field_def = props.get(base_field, {}) or {}
            if field_def.get("type") == "text" and "keyword" in (field_def.get("fields", {}) or {}):
                resolved = f"{base_field}.keyword"
    except Exception as e:  # noqa: BLE001 — fail-soft: bare field is the safe default
        logging.info(
            "connector_wizard: term-field mapping probe for %r on %r failed (%s); using bare field",
            base_field,
            index,
            type(e).__name__,
        )

    _TERM_FIELD_CACHE[cache_key] = resolved
    return resolved


def _find_asset_by_natural_key(
    asset_type: str,
    field: str,
    value: str,
    *,
    case_insensitive: bool = False,
    exclude_token_field: str | None = None,
    with_docs: bool = False,
    size: int = 1,
) -> str | None | list[dict[str, Any]]:
    """Return the ESID of an existing ``de_asset_<asset_type>`` row whose
    ``field`` matches ``value`` (natural-key dedup), or None.

    ``exclude_token_field`` — when set, only a doc WITHOUT that field
    (a tokenless placeholder) matches. One Google email can own several Brand
    Account identities, and each identity needs its own account doc + token
    slot; matching an already-tokened doc on the email would reuse (and later
    overwrite) another identity's token — the re-auth ping-pong that broke the
    that connector's imports. A tokenless placeholder is still reused, which keeps
    the orphan-accumulation fix for abandoned auth attempts.

    the wizard create-path mints a fresh random ES ``_id`` per add, so
    re-adding the same natural key (a Google Ads customer id, a GA4 property id)
    silently produces a duplicate orphan row. Callers look up the existing row
    first and reuse its ESID instead of creating a second one.

    The ``term`` targets whichever field ``_termable_field`` resolves —
    ``<field>.keyword`` on a text+keyword mapping (prod), the bare field on a
    pure-keyword mapping (staging). Hard-coding ``.keyword`` silently returned 0
    hits on the bare-keyword env and defeated the whole dedup (; the
     mapping-trap class).

    ``case_insensitive`` — for keys that are case-insensitive in the
    real world (an email address; ``Jane.Doe@Example.com`` and the lowercase form
    are one mailbox), match with the ES ``term`` ``case_insensitive`` flag (ES
    7.10+, cluster is 9.x). This lets the lookup find an EXISTING doc stored with
    a different case WITHOUT a mapping normalizer / reindex. Leave False for
    genuinely case-sensitive keys (customer / property ids).

    ``with_docs`` / ``size`` — when ``with_docs`` is True, return up
    to ``size`` full hits (``{"_id", "_source"}``) instead of the single ESID,
    so a caller can rank candidates (e.g. prefer the caller's own workspace-less
    doc over another user's). Default keeps the original single-ESID contract.

    Fail-soft: any lookup error returns None (or ``[]`` with ``with_docs``) so
    the caller falls back to create (the pre-dedup behavior) rather than
    blocking configuration.
    """
    val = str(value or "").strip()
    if not val:
        return None
    config = ASSET_CONFIGS.get(asset_type)
    if not config:
        return None
    term_field = _termable_field(config["index"], field)
    term_body: dict[str, Any] = {"value": val}
    if case_insensitive:
        term_body["case_insensitive"] = True
    # NB: pass the BARE query clause. get_all_documents wraps it as
    # {"query": <arg>} itself — passing {"query": {...}} here double-wraps to
    # {"query": {"query": {...}}} and ES 400s ("unknown field [query]"), which
    # the except below swallowed → dedup silently no-op'd and every re-add made
    # a duplicate. This was the real bug
    # behind the whole dedup never firing against live ES; the mocked tests
    # patched get_all and never saw the double-wrap.
    query: dict[str, Any] = {"term": {term_field: term_body}}
    if exclude_token_field:
        # restrict the match to tokenless placeholders (see docstring).
        query = {
            "bool": {
                "filter": [query],
                "must_not": [{"exists": {"field": exclude_token_field}}],
            }
        }
    try:
        results = AssetService.get_all(config["index"], query=query, size=size) or []
    except Exception as e:  # noqa: BLE001 — fail-soft: fall back to create
        logging.warning("connector_wizard: %s dedup lookup failed: %s", asset_type, e)
        return [] if with_docs else None
    if with_docs:
        return results
    if not results:
        return None
    return results[0].get("_id") or None


def _find_google_ads_customer_by_id(customer_id: str) -> str | None:
    """ESID of an existing de_asset_google_ads_customers row
    for this customer id, or None. Thin wrapper over the natural-key lookup."""
    return _find_asset_by_natural_key("google_ads_customers", "google_ads_customer_id", customer_id)


def _reuse_or_create_oauth_account(
    asset_type: str,
    new_account: dict[str, Any],
    key_field: str,
    *,
    key_is_email: bool = False,
    created_by: str = "",
    reuse_only_tokenless_field: str | None = None,
    allowed_workspaces: list[str] | tuple[str, ...] | None = (),
) -> str:
    """dedup an OAuth-account create on its natural key.

    The YouTube / GA4 / Facebook wizards mint the account doc in their step-1
    "add new account" path BEFORE OAuth completes, keyed on a random ES ``_id``.
    A user whose auth fails, is abandoned, or lands on a non-owning identity
    just retries — and each retry created a FRESH orphan doc (the prod incident:
    one partner account became FOUR non-working docs, none removable via the UI
    because of the ownership gate).

    Reuse-on-create closes the accumulation: if an account doc with the same
    natural key (``key_field`` — the Google account email for YouTube/GA4, the
    numeric account id for Facebook) already exists, return its ESID instead of
    minting a second one. A re-auth attempt then lands back on the SAME
    placeholder doc, and the OAuth callback populates its token in place —
    self-healing rather than piling up. Removing docs already left dead by a
    failed auth is out of scope here.

    Unlike the Google Ads dedup (, deterministic id keyed on
    ``(customer_id, workspace)``), this step has no team/workspace context —
    the team isn't chosen until the final ``configure_*`` step — so the key is
    the natural field alone. Fail-soft via ``_find_asset_by_natural_key``: any
    lookup error falls back to create (the earlier behavior), never blocks.

    ``key_is_email`` — the Google account email (YouTube/GA4) is case-insensitive
    in the real world, so a differently-cased re-add is the SAME account. We (1)
    lowercase the email in the create payload so new docs are stored canonical,
    and (2) match case-insensitively so the lookup also finds EXISTING docs saved
    with a different case (earlier or hand-created) — otherwise the raw
    exact-case ``.keyword`` term would dedup-miss and mint a duplicate, which is
    the very accumulation this fix exists to stop. Facebook's key is a numeric
    ``fb_account_id`` (case-irrelevant), so it stays exact-match.

    ``reuse_only_tokenless_field`` — name of the doc's token field
    (e.g. ``youtube_account_token``). When set, dedup reuses ONLY a doc that has
    no token yet: an email is not an identity (one Gmail can front N Brand
    Account tiles at Google's OAuth chooser, each minting a token that serves
    only its own channels), so reusing a TOKENED doc for a same-email add would
    overwrite a working identity's token — the re-auth ping-pong. With
    this set, the same email can hold one doc per authorized identity; the
    post-callback identity dedup in ``fb_oauth_server`` keeps retries for an
    identity we already hold from minting duplicates.

    ``created_by`` — the creating user's ``username``, stamped onto a
    NEWLY created account as ``wizard_created_by``. A wizard account is minted
    workspace-less (the team isn't chosen until ``configure_*``), and the
    Authorize step's ``oauth-status`` IDOR gate allows a workspace-less doc only
    to its creator (see ``_check_account_asset_for_wizard``). Without this stamp
    the gate would either 404 the real creator's own poll, or (the earlier,
    over-broad carve-out) expose every unclaimed doc's OAuth status to any
    connectors user.

    Reuse is creator-aware. The original rule — "an existing doc
    already carries its original creator" — failed two ways: docs minted before
    the stamp existed carry NO creator, and a natural-key match can hand user B
    a workspace-less doc created by user A. Either way the reused doc is
    unreachable to the caller at the ``oauth-status`` gate: the wizard hangs on
    "Account not found" for the very user who just authorized. Candidates are
    therefore ranked: (1) a workspace-less doc the CALLER created, (2) a
    workspace-stamped doc (workspace authz governs it, earlier behavior),
    (3) a legacy workspace-less doc with NO creator stamp — reused and
    repair-stamped with the caller (it was reachable to nobody, so claiming it
    narrows nothing). A workspace-less doc carrying a DIFFERENT creator is never
    reused — the caller gets a fresh doc instead, and the IDOR boundary
    stays exactly where it was.

    ``allowed_workspaces`` — rank (2) reused a workspace-stamped doc
    from ANY workspace, so naming another tenant's email / account id handed
    back that tenant's doc (its auth slug, and an authorize step that plants the
    caller's token on it). A stamped doc is now reused only when its workspace
    is in this set; ``None`` means the caller sees every workspace, and the
    default ``()`` reuses no stamped doc at all (a fresh doc is created).
    """
    payload = dict(new_account or {})
    key_value = str(payload.get(key_field) or "").strip()
    caller = str(created_by or "").strip()
    if key_is_email:
        # Canonicalize at write time so the stored keyword and the lookup value
        # agree going forward (the lookup already lowercases its input).
        key_value = key_value.lower()
        if key_value:
            payload[key_field] = key_value
    if key_value:
        candidates = _find_asset_by_natural_key(
            asset_type,
            key_field,
            key_value,
            case_insensitive=key_is_email,
            exclude_token_field=reuse_only_tokenless_field,
            with_docs=True,
            size=5,
        )
        chosen_esid, needs_creator_repair = None, False
        best_rank = None
        for hit in candidates or []:
            src = hit.get("_source") or {}
            esid = hit.get("_id")
            if not esid:
                continue
            has_ws = bool(str(src.get("workspace_esid") or "").strip())
            creator = str(src.get("wizard_created_by") or "").strip()
            if not has_ws and creator and caller and creator == caller:
                rank, repair = 1, False
            elif has_ws:
                ws = str(src.get("workspace_esid") or "").strip()
                if allowed_workspaces is not None and ws not in set(allowed_workspaces):
                    # another workspace's doc: never hand it back.
                    continue
                rank, repair = 2, False
            elif not creator:
                rank, repair = 3, True
            else:
                # Workspace-less doc created by someone else: reusing it would
                # 404 the caller's own oauth-status poll. Skip.
                continue
            if best_rank is None or rank < best_rank:
                best_rank, chosen_esid, needs_creator_repair = rank, esid, repair
        if chosen_esid:
            logging.info(
                "reusing existing %s doc for %s=%r (rank=%s repair=%s, no duplicate created)",
                asset_type,
                key_field,
                key_value,
                best_rank,
                needs_creator_repair,
            )
            if needs_creator_repair and caller:
                # claim the pre-stamp legacy husk for the caller so
                # the oauth-status gate recognizes them. Same fail-soft update
                # pattern as the create-path stamp below.
                try:
                    config = ASSET_CONFIGS.get(asset_type) or {}
                    index = config.get("index")
                    if index:
                        doc = AssetService.get_by_id(index, chosen_esid)
                        if doc is not None:
                            AssetService.update(index, chosen_esid, {**doc, "wizard_created_by": caller})
                except Exception as e:  # noqa: BLE001 — fail-soft: reuse still returns
                    logging.warning(
                        "wizard_created_by repair failed asset=%s esid=%s: %s",
                        asset_type,
                        chosen_esid,
                        e,
                    )
            return chosen_esid
    new_esid = _create_asset(asset_type, payload)
    # Stamp the creator AFTER create: wizard_created_by is not in the account
    # ASSET_CONFIGS schema, so FormService.process_form_data would drop it from
    # the create payload — mirror the _stamp_asset_* post-create pattern instead.
    creator = str(created_by or "").strip()
    if new_esid and creator:
        try:
            config = ASSET_CONFIGS.get(asset_type) or {}
            index = config.get("index")
            if index:
                doc = AssetService.get_by_id(index, new_esid)
                if doc is not None:
                    AssetService.update(index, new_esid, {**doc, "wizard_created_by": creator})
        except Exception as e:  # noqa: BLE001 — fail-soft: the account row already exists
            logging.warning("wizard_created_by stamp failed asset=%s esid=%s: %s", asset_type, new_esid, e)
    return new_esid


def _has_google_ads_oauth_token(customer_id: str) -> bool:
    """True when the customer has an ACTIVE tokened ``google_ads`` connection.

    used by ``configure_google_ads_customer`` to decide whether the
    Confirm step should skip the SA/MCC existence probe (it should, when an
    OAuth token exists — that account is reachable via the user's token, not our
    manager account). Reuses the importer's exact lookup so "has a usable OAuth
    token" is defined identically on both sides. Fail-soft: any error returns
    False, so the SA probe still runs (the earlier behavior) rather than
    silently skipping validation.
    """
    cid = str(customer_id or "").strip()
    if not cid:
        return False
    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService
    try:
        return bool(ConnectionService.get_google_ads_oauth_connection_esid(cid))
    except Exception as e:  # noqa: BLE001 — fail-soft: fall back to the SA probe
        logging.warning("connector_wizard: google_ads OAuth-token check failed for %s: %s", cid, e)
        return False


def _update_team_data_source(
    team_esid: str,
    flag_field: str,
    esid_field: str,
    esid_value: str | list[str],
    title_field: str | None = None,
    title_value: str | None = None,
) -> None:
    """Flip a data-source flag on the team and write the linked ESID(s).

    Reads the existing team doc, merges the changes, and writes via
    AssetService.update so other team fields are preserved.

    ``title_field``/``title_value`` optionally stamp the connector's
    DISPLAY title alongside the esid. The team-edit form's connector chips
    render the *value* field (e.g. ``echo_tenant_title``), not the esid, so a
    wizard that wrote only the esid left the chip blank even though the link
    was correct. Only written when both are provided and non-empty.
    """
    existing = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing:
        # don't echo team_esid into client error (CodeQL py/reflective-xss).
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")

    updated = dict(existing)
    updated[flag_field] = "true"
    updated[esid_field] = esid_value
    if title_field and title_value:
        updated[title_field] = title_value

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")


# ── Echo wizard ──────────────────────────────────────────────────────────────


def _require_pick_in_team_workspace(index: str, asset_esid: str, team_esid: str, not_found: str) -> None:
    """a pick-existing esid arrives in the request body. The asset
    must belong to the team's own workspace: linking another workspace's Echo
    tenant or Chatwoot account would import that tenant's data with its stored
    credential into this team. A missing, unstamped or foreign doc reads as
    missing (no enumeration); the pickers already hide all three.
    """
    team_ws = str((AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}).get("workspace_esid") or "").strip()
    doc_ws = str((AssetService.get_by_id(index, asset_esid) or {}).get("workspace_esid") or "").strip()
    if not team_ws or doc_ws != team_ws:
        logging.warning(
            "connector_wizard:  refused %s esid %r for team %s (asset ws %r, team ws %r)",
            index,
            asset_esid,
            team_esid,
            doc_ws,
            team_ws,
        )
        raise WizardError(not_found)


def configure_echo(
    team_esid: str,
    tenant_esid: str | None,
    new_tenant: dict[str, Any] | None,
) -> dict[str, Any]:
    """Configure Echo for a team.

    Exactly one of ``tenant_esid`` (pick existing) or ``new_tenant`` (create
    new) must be provided. Returns ``{"tenant_esid": ..., "created": bool}``.
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if bool(tenant_esid) == bool(new_tenant):
        raise WizardError("Provide exactly one of tenant_esid or new_tenant")

    created = False
    if tenant_esid:
        _require_pick_in_team_workspace("de_asset_echo_tenants", tenant_esid, team_esid, "Echo tenant not found")
    if new_tenant:
        # the subdomain becomes the importer's request host; vet it
        # before anything is written.
        new_tenant = dict(new_tenant)
        try:
            new_tenant["echo_tenant_subdomain"] = normalize_echo_subdomain(new_tenant.get("echo_tenant_subdomain"))
        except EchoSubdomainError as e:
            raise WizardError(str(e)) from e
        tenant_esid = _create_asset("echo_tenants", new_tenant)
        created = True
        # stamp the team's workspace_esid onto the new tenant doc so
        # it isn't dropped fail-closed by Asset Explorer / the picker. Same gap
        # the (GA4) / (YouTube) sweeps fixed for those
        # connectors but missed for Echo. Only meaningful on the create path.
        _team_doc = AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}
        _stamp_asset_workspace("de_asset_echo_tenants", tenant_esid, _team_doc.get("workspace_esid"))

    # resolve the tenant's display title so the team-form Echo chip
    # (which renders echo_tenant_title) isn't blank. From new_tenant on create,
    # else the linked tenant doc on pick-existing.
    tenant_title = ""
    if new_tenant:
        tenant_title = str((new_tenant or {}).get("echo_tenant_title") or "").strip()
    if not tenant_title:
        tenant_doc = AssetService.get_by_id("de_asset_echo_tenants", tenant_esid) or {}
        tenant_title = str(tenant_doc.get("echo_tenant_title") or "").strip()

    _update_team_data_source(
        team_esid=team_esid,
        flag_field="ds_echo",
        esid_field="echo_tenant_esid",
        esid_value=tenant_esid,
        title_field="echo_tenant_title",
        title_value=tenant_title or None,
    )
    logging.info(
        "WizardService.configure_echo: team=%s tenant=%s created=%s",
        team_esid,
        tenant_esid,
        created,
    )
    return {"tenant_esid": tenant_esid, "created": created}


# ── Chatwoot wizard ──────────────────────────────────────────────────────────


def configure_chatwoot(
    team_esid: str,
    account_esid: str | None,
    new_account: dict[str, Any] | None,
) -> dict[str, Any]:
    """Configure Chatwoot for a team. See configure_echo for arg semantics."""
    if not team_esid:
        raise WizardError("team_esid is required")
    if bool(account_esid) == bool(new_account):
        raise WizardError("Provide exactly one of account_esid or new_account")

    created = False
    if account_esid:
        _require_pick_in_team_workspace("de_asset_chatwoot_accounts", account_esid, team_esid, "Chatwoot account not found")
    if new_account:
        # vet the server URL (blank = Chatwoot Cloud) before anything
        # is written; the importers send the account's token to this host.
        new_account = dict(new_account)
        try:
            new_account["chatwoot_base_url"] = normalize_chatwoot_base_url(new_account.get("chatwoot_base_url"))
        except ChatwootBaseUrlError as e:
            raise WizardError(str(e)) from e
        # prove the server, account id and token work before writing:
        # a wrong token or a bot token used to save fine and only fail at import.
        from import_scripts.chatwoot.api import ChatwootCredentialError, verify_account_access

        try:
            verify_account_access(
                new_account["chatwoot_base_url"],
                new_account.get("chatwoot_account_id"),
                new_account.get("chatwoot_api_token"),
            )
        except ChatwootCredentialError as e:
            raise WizardError(str(e)) from e
        account_esid = _create_asset("chatwoot_accounts", new_account)
        created = True
        # stamp the team's workspace_esid onto the new account doc so
        # it isn't dropped fail-closed by Asset Explorer / the picker (same gap
        # as Echo/Google Ads; the sweeps missed Chatwoot).
        _team_doc = AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}
        _stamp_asset_workspace("de_asset_chatwoot_accounts", account_esid, _team_doc.get("workspace_esid"))
        # record the creating team as the asset's owner so a later
        # cross-link onto another org's team can't move it into that org's billing.
        _stamp_asset_owning_team("de_asset_chatwoot_accounts", account_esid, team_esid)

    # resolve the account's display title so the team-form Chatwoot
    # chip (chatwoot_account_title) isn't blank (same gap as Echo/GAds/FB).
    account_title = ""
    if new_account:
        account_title = str((new_account or {}).get("chatwoot_account_title") or "").strip()
    if not account_title:
        acct_doc = AssetService.get_by_id("de_asset_chatwoot_accounts", account_esid) or {}
        account_title = str(acct_doc.get("chatwoot_account_title") or "").strip()

    _update_team_data_source(
        team_esid=team_esid,
        flag_field="ds_chatwoot",
        esid_field="chatwoot_account_esid",
        esid_value=account_esid,
        title_field="chatwoot_account_title",
        title_value=account_title or None,
    )
    logging.info(
        "WizardService.configure_chatwoot: team=%s account=%s created=%s",
        team_esid,
        account_esid,
        created,
    )
    return {"account_esid": account_esid, "created": created}


# ── Chatwoot inbox picker ─────────────────────────────────────────
#
# A team picks which inboxes of its Chatwoot account it owns
# (de_asset_teams.chatwoot_inbox_ids); the importer then attributes data per
# inbox (import_scripts/chatwoot/attribution.py). One inbox belongs to at most
# one team — a pick another team already holds is refused here, and a clash
# that slips past (concurrent saves) is declined by the importer.

CHATWOOT_ACCOUNTS_INDEX = "de_asset_chatwoot_accounts"
_MAX_CHATWOOT_INBOXES = 200
_CHATWOOT_CHANNEL_LABELS = {
    "Channel::FacebookPage": "Facebook",
    "Channel::Instagram": "Instagram",
    "Channel::Line": "LINE",
    "Channel::WebWidget": "Website",
    "Channel::Whatsapp": "WhatsApp",
    "Channel::Telegram": "Telegram",
    "Channel::Email": "Email",
    "Channel::Api": "API",
    "Channel::Sms": "SMS",
    "Channel::TwilioSms": "SMS",
    "Channel::TwitterProfile": "X (Twitter)",
}


def _chatwoot_team_and_account(team_esid: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
    if not team_esid:
        raise WizardError("team_esid is required")
    team = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not team:
        raise WizardError("Team not found")
    account_esid = str(team.get("chatwoot_account_esid") or "")
    if str(team.get("ds_chatwoot") or "").lower() != "true" or not account_esid:
        raise WizardError("Set up Chatwoot for this team first, then choose its inboxes.")
    account = AssetService.get_by_id(CHATWOOT_ACCOUNTS_INDEX, account_esid)
    if not account:
        raise WizardError("This team's Chatwoot account no longer exists. Set up Chatwoot again.")
    return team, account_esid, account


def _live_chatwoot_inboxes(account: dict[str, Any]) -> list[dict[str, Any]]:
    """The account's inboxes, read live with its stored token. Fixed-string
    errors only — the token and server address are never echoed."""
    import requests

    from import_scripts.chatwoot.get_account_reports import list_inboxes
    from lib.encryption import decrypt

    try:
        base_url = normalize_chatwoot_base_url(account.get("chatwoot_base_url"))
    except ChatwootBaseUrlError as e:
        raise WizardError(str(e)) from e
    token = decrypt(account.get("chatwoot_api_token") or "") if account.get("chatwoot_api_token") else ""
    if not token or not account.get("chatwoot_account_id"):
        raise WizardError("This Chatwoot account has no account ID or API token saved.")
    try:
        return list_inboxes(account["chatwoot_account_id"], token, base_url=base_url)
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        logging.warning("connector_wizard: chatwoot inbox list HTTP %s", status)
        if status in (401, 403):
            raise WizardError(
                "Chatwoot refused the saved API token. Use a user Access Token (Profile settings) from an "
                "administrator of this account, not a bot token."
            ) from e
        raise WizardError("Could not read the inboxes from the Chatwoot server.") from e
    except Exception as e:  # noqa: BLE001 — network / parse errors → operator-facing message
        logging.warning("connector_wizard: chatwoot inbox list failed: %s", e)
        raise WizardError("Could not reach the Chatwoot server. Check its address and that it is online.") from e


_OTHER_TEAM_LABEL = "another team"


def _chatwoot_inbox_claims(account_esid: str, team: dict[str, Any], exclude_team_esid: str) -> dict[str, str | None]:
    """inbox id -> how to name the OTHER team that already picked it.

    A shared Chatwoot account can serve teams in different workspaces (tenants).
    The other team's title is revealed only when it shares the caller team's
    workspace; otherwise the value is None ("another team", name withheld), so
    the picker never discloses a team — or its inbox names — across tenants
    (same posture as _check_team_for_wizard's 404 collapse).
    """
    from import_scripts.chatwoot.attribution import get_teams_for_account, team_inbox_ids
    from lib.es_helpers import get_elasticsearch_client

    es = get_elasticsearch_client()
    if es is None:
        raise WizardError("Could not check which inboxes other teams use. Try again.")
    own_workspace = str(team.get("workspace_esid") or "")
    claims: dict[str, str | None] = {}
    for esid, doc in get_teams_for_account(es, account_esid):
        if esid == exclude_team_esid:
            continue
        same_workspace = bool(own_workspace) and str(doc.get("workspace_esid") or "") == own_workspace
        for inbox_id in team_inbox_ids(doc):
            claims.setdefault(inbox_id, str(doc.get("team_title") or esid) if same_workspace else None)
    return claims


def list_chatwoot_inboxes(team_esid: str) -> dict[str, Any]:
    """The team's Chatwoot inboxes for the picker: each with its channel label,
    whether this team picked it, and which other team owns it (if any)."""
    team, account_esid, account = _chatwoot_team_and_account(team_esid)
    from import_scripts.chatwoot.attribution import team_inbox_ids

    inboxes = _live_chatwoot_inboxes(account)
    claims = _chatwoot_inbox_claims(account_esid, team, team_esid)
    selected = set(team_inbox_ids(team))
    rows = []
    for inbox in inboxes:
        inbox_id = str(inbox["id"])
        taken = inbox_id in claims
        owner = claims.get(inbox_id)
        # Another workspace's inbox: neither its name nor its team is shown.
        hidden = taken and owner is None
        rows.append(
            {
                "id": inbox_id,
                "name": "Inbox in use" if hidden else (inbox.get("name") or f"Inbox {inbox_id}"),
                "channel_label": _CHATWOOT_CHANNEL_LABELS.get(inbox.get("channel_type") or "", "Other"),
                "selected": inbox_id in selected,
                "taken_by": (owner or _OTHER_TEAM_LABEL) if taken else None,
            }
        )
    known = {r["id"] for r in rows}
    return {
        "account_title": account.get("chatwoot_account_title") or "",
        "inboxes": rows,
        # Picks that no longer exist on the account (deleted in Chatwoot).
        "missing_selected": sorted(selected - known),
    }


def set_chatwoot_inboxes(team_esid: str, inbox_ids: list[Any]) -> dict[str, Any]:
    """Save which inboxes of its Chatwoot account the team owns. Every id must
    be an inbox of the account that no other team has picked. An empty list
    clears the picks (the importer then treats the team as unsplit)."""
    if not isinstance(inbox_ids, list):
        raise WizardError("inbox_ids must be a list")
    ids: list[str] = []
    for raw in inbox_ids:
        value = str(raw).strip()
        if not value.isdigit():
            raise WizardError("Inbox ids must be numbers.")
        if value not in ids:
            ids.append(value)
    if len(ids) > _MAX_CHATWOOT_INBOXES:
        raise WizardError("Too many inboxes selected.")

    team, account_esid, account = _chatwoot_team_and_account(team_esid)
    if ids:
        names = {str(i["id"]): i.get("name") or f"Inbox {i['id']}" for i in _live_chatwoot_inboxes(account)}
        if any(i not in names for i in ids):
            raise WizardError("One or more selected inboxes are not on this Chatwoot account. Reload and try again.")
        claims = _chatwoot_inbox_claims(account_esid, team, team_esid)
        taken = [i for i in ids if i in claims]
        if taken:
            first = taken[0]
            if claims[first] is None:
                raise WizardError("One of the selected inboxes already belongs to another team.")
            raise WizardError(f"Inbox '{names[first]}' already belongs to team '{claims[first]}'.")

    from lib.es_helpers import ensure_mapping_fields, get_elasticsearch_client

    # map the field as keyword on the live teams index before the
    # first write (no update_vN step to forget); a no-op once it exists.
    es = get_elasticsearch_client()
    if es is not None:
        ensure_mapping_fields(es, TEAMS_INDEX, {"properties": {"chatwoot_inbox_ids": {"type": "keyword"}}})
    updated = dict(team)
    updated["chatwoot_inbox_ids"] = ids
    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")
    logging.info("WizardService.set_chatwoot_inboxes: team=%s account=%s inboxes=%s", team_esid, account_esid, ids)
    return {"inbox_ids": ids}


# ── Google Ads campaign rules ─────────────────────────────────────
#
# A team claims campaigns of a Google Ads account it SHARES with other teams by
# campaign labels (exact) and name prefixes (case-insensitive), stored on the
# team (import_scripts/google_ads/attribution.py applies them at import). The
# preview reads campaigns from what has already been imported for the account.
# Another workspace's team — and the names of its campaigns — are never shown
# (same posture as the Chatwoot picker / _check_team_for_wizard).

GADS_LABELS_FIELD = "team_google_ads_campaign_labels"
GADS_PREFIXES_FIELD = "team_google_ads_campaign_prefixes"
_MAX_GADS_RULES = 25
_MAX_GADS_RULE_LEN = 100
_GADS_OTHER_TEAM = "another team"


def _clean_gads_rules(values: Any, kind: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise WizardError(f"{kind} must be a list")
    out: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if not value:
            continue
        if len(value) > _MAX_GADS_RULE_LEN or any(ord(c) < 32 for c in value):
            raise WizardError(f"Each {kind[:-1]} must be at most {_MAX_GADS_RULE_LEN} printable characters.")
        if value.lower() not in {v.lower() for v in out}:
            out.append(value)
    if len(out) > _MAX_GADS_RULES:
        raise WizardError(f"At most {_MAX_GADS_RULES} {kind}.")
    return out


def _gads_team_customer_esids(team: dict[str, Any]) -> list[str]:
    ids = list(team.get("team_google_ads_customer_esids") or [])
    if team.get("google_ads_customer_esid") and team["google_ads_customer_esid"] not in ids:
        ids.append(team["google_ads_customer_esid"])
    return [str(i) for i in ids if i]


def _gads_campaign_catalog(es: Any, customer_esid: str, team_esids: list[str]) -> list[dict[str, Any]]:
    """Campaigns already imported for this customer, from every linked team's
    index: {id, name (latest), labels (union), last_date}."""
    from lib.es_helpers import build_data_index_name

    indices = ",".join(build_data_index_name(e, "google_ads") for e in team_esids)
    result = es.search(
        index=indices,
        ignore_unavailable=True,
        size=0,
        query={"term": {"google_ads_customer_esid": customer_esid}},
        aggs={
            "c": {
                "terms": {"field": "campaign_id", "size": 500},
                "aggs": {
                    "labels": {"terms": {"field": "campaign_labels", "size": 20}},
                    "latest": {"top_hits": {"size": 1, "sort": [{"@timestamp": "desc"}], "_source": ["campaign_name"]}},
                    "last_date": {"max": {"field": "date"}},
                },
            }
        },
    )
    catalog = []
    for b in result.get("aggregations", {}).get("c", {}).get("buckets", []):
        hits = b["latest"]["hits"]["hits"]
        catalog.append(
            {
                "id": str(b["key"]),
                "name": (hits[0]["_source"].get("campaign_name") if hits else None) or str(b["key"]),
                "labels": [x["key"] for x in b["labels"]["buckets"]],
                "last_date": (b["last_date"].get("value_as_string") or "")[:10],
            }
        )
    return sorted(catalog, key=lambda c: c["name"].lower())


def _gads_rules_overview(team_esid: str, draft: dict[str, list[str]] | None) -> dict[str, Any]:
    """The team's rules and, per Google Ads account it shares, which team each
    imported campaign goes to — under the saved rules, or ``draft`` for this
    team (other teams always use their saved rules)."""
    from import_scripts.google_ads.attribution import get_linked_teams, plan_customer, team_rules
    from lib.es_helpers import get_elasticsearch_client

    team = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not team:
        raise WizardError("Team not found")
    labels = list(team.get(GADS_LABELS_FIELD) or []) if draft is None else draft["labels"]
    prefixes = list(team.get(GADS_PREFIXES_FIELD) or []) if draft is None else draft["prefixes"]
    es = get_elasticsearch_client()
    if es is None:
        raise WizardError("Could not read the Google Ads accounts. Try again.")
    own_ws = str(team.get("workspace_esid") or "")
    accounts = []
    for customer_esid in _gads_team_customer_esids(team):
        linked = get_linked_teams(customer_esid)
        if len(linked) < 2 or team_esid not in {e for e, _ in linked}:
            continue  # rules only matter on an account this team shares
        linked = [
            (e, {**d, GADS_LABELS_FIELD: labels, GADS_PREFIXES_FIELD: prefixes} if e == team_esid else d) for e, d in linked
        ]
        visible = {e: bool(own_ws) and str(d.get("workspace_esid") or "") == own_ws for e, d in linked}
        names = {e: (d.get("team_title") or e) if visible[e] else _GADS_OTHER_TEAM for e, d in linked}
        rules = {e: team_rules(d) for e, d in linked}
        plan = plan_customer("", linked)
        customer = AssetService.get_by_id("de_asset_google_ads_customers", customer_esid) or {}
        campaigns = []
        suggestions: set[str] = set()
        for c in _gads_campaign_catalog(es, customer_esid, [e for e, _ in linked]):
            suggestions.update(c["labels"])
            matches = [e for e, r in rules.items() if r and r.matches(c["name"], c["labels"])]
            if plan.rules_mode:
                owner, reason = plan.owner_of(c["name"], c["labels"])
                status = "this_team" if owner == team_esid else ("other_team" if owner else reason)
            else:
                owner = linked[0][0]  # legacy: the whole account goes to the lowest team_esid
                status = "this_team" if owner == team_esid else "other_team"
            # Never name another workspace's campaigns: hide the name when every
            # team that decides this campaign is another workspace's team.
            deciders = matches or ([owner] if owner else [])
            hidden = bool(deciders) and team_esid not in deciders and not any(visible[e] for e in deciders)
            campaigns.append(
                {
                    "name": "Campaign used by another team" if hidden else c["name"],
                    "labels": [] if hidden else c["labels"],
                    "last_date": c["last_date"],
                    "status": status,
                    "owner": names.get(owner) if owner and owner != team_esid else None,
                }
            )
        accounts.append(
            {
                "esid": customer_esid,
                "title": customer.get("google_ads_customer_title") or customer_esid,
                "customer_id": customer.get("google_ads_customer_id") or "",
                "split": plan.rules_mode,
                "teams": [{"name": names[e], "has_rules": bool(rules[e]), "is_this_team": e == team_esid} for e, _ in linked],
                "campaigns": campaigns,
                "label_suggestions": sorted(suggestions),
            }
        )
    return {"labels": labels, "prefixes": prefixes, "shared_accounts": accounts}


def google_ads_campaign_rules(team_esid: str) -> dict[str, Any]:
    """The team's saved campaign rules and the per-account preview."""
    return _gads_rules_overview(team_esid, None)


def preview_google_ads_campaign_rules(team_esid: str, labels: Any, prefixes: Any) -> dict[str, Any]:
    """The per-account preview for unsaved rules (nothing is written)."""
    draft = {"labels": _clean_gads_rules(labels, "labels"), "prefixes": _clean_gads_rules(prefixes, "prefixes")}
    return _gads_rules_overview(team_esid, draft)


def set_google_ads_campaign_rules(team_esid: str, labels: Any, prefixes: Any) -> dict[str, Any]:
    """Save the team's campaign rules. A label another team on a shared account
    already claims, or a prefix overlapping one of its prefixes, is refused —
    two teams claiming the same campaigns would leave them unassigned."""
    from import_scripts.google_ads.attribution import get_linked_teams, team_rules
    from lib.es_helpers import ensure_mapping_fields, get_elasticsearch_client

    labels = _clean_gads_rules(labels, "labels")
    prefixes = _clean_gads_rules(prefixes, "prefixes")
    team = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not team:
        raise WizardError("Team not found")
    own_ws = str(team.get("workspace_esid") or "")
    mine = team_rules({GADS_LABELS_FIELD: labels, GADS_PREFIXES_FIELD: prefixes})
    for customer_esid in _gads_team_customer_esids(team):
        for esid, doc in get_linked_teams(customer_esid):
            if esid == team_esid:
                continue
            theirs = team_rules(doc)
            who = (
                (doc.get("team_title") or esid)
                if own_ws and str(doc.get("workspace_esid") or "") == own_ws
                else _GADS_OTHER_TEAM
            )
            clash = sorted(mine.labels & theirs.labels)
            if clash:
                raise WizardError(f"The label '{clash[0]}' is already used by {who} on a shared Google Ads account.")
            for p in mine.prefixes:
                for q in theirs.prefixes:
                    if p.startswith(q) or q.startswith(p):
                        raise WizardError(f"The prefix '{p}' overlaps a prefix used by {who} on a shared Google Ads account.")

    es = get_elasticsearch_client()
    if es is not None:
        ensure_mapping_fields(
            es, TEAMS_INDEX, {"properties": {GADS_LABELS_FIELD: {"type": "keyword"}, GADS_PREFIXES_FIELD: {"type": "keyword"}}}
        )
    updated = dict(team)
    updated[GADS_LABELS_FIELD] = labels
    updated[GADS_PREFIXES_FIELD] = prefixes
    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")
    logging.info("WizardService.set_google_ads_campaign_rules: team=%s labels=%s prefixes=%s", team_esid, labels, prefixes)
    return {"labels": labels, "prefixes": prefixes}


# ── Disciple Tools wizard ──────────────────────────────────────────


def _verify_dt_site_link(dt_domain: str, our_domain: str, token: str) -> None:
    """Probe the DT site with the Site-to-Site Link credential; raise on failure.

    Issues one authenticated ``GET dt-posts/v2/contacts?limit=1`` via the
    importer's own client so "the wizard accepted it" and "the nightly pull can
    read it" are the same check. Hard-fail by design: a typo'd token that's
    only validated at import time surfaces as a silent nightly skip
    (connections.py logs a warning and moves on), which the operator never sees.

    Error messages are fixed strings — the user-typed domain/token are never
    echoed back.
    """
    from import_scripts.disciple_tools.client import DTAuthError, DTDomainError, DTSiteClient

    client = DTSiteClient(dt_domain, our_domain, token)
    try:
        next(client.fetch_posts("contacts", page_size=1), None)
    except DTDomainError as e:
        # a non-public site domain is refused before any request.
        raise WizardError(e.public_message) from e
    except DTAuthError:
        raise WizardError(
            "The Disciple.Tools site rejected the Site Link token. Check the token "
            "and that both domains exactly match the Site Link entry in wp-admin."
        )
    except Exception as e:  # noqa: BLE001 — network/HTTP errors → operator-facing message
        logging.warning("connector_wizard: DT site-link probe failed: %s", e)
        raise WizardError(
            "Could not reach the Disciple.Tools site. Check the site domain and " "that the site is online (HTTPS required)."
        )


def _norm_dt_domain(domain: str) -> str:
    """Normalize a DT domain the same way create_disciple_tools_site / the
    importer client do (strip scheme + all slashes), so a submitted domain can
    be compared to the one stored on the dt_site asset."""
    d = (domain or "").replace("https://", "").replace("http://", "").replace("//", "")
    return d.replace("/", "").strip()


def _existing_dt_site(team_esid: str) -> tuple[str, str] | None:
    """follow-up — the team's saved DT Site Link credential, DECRYPTED,
    with the domain it was issued for.

    Returns ``(token_plaintext, normalized_dt_domain)`` or None. Backs the
    wizard's keep-if-unchanged path (token field left showing the saved
    ciphertext → POST sends token=""). The domain is returned so the caller can
    PIN reuse to the same site: a saved token must never be replayed against a
    caller-supplied different domain (that would send the credential's bearer to
    an arbitrary host — an exfiltration/SSRF vector). Fail-soft: None when the
    team has no dt_site asset / no decryptable token.
    """
    try:
        from web.services.connection_service import ASSET_TYPE_DT_SITE, ConnectionService
    except ImportError:
        from .connection_service import ASSET_TYPE_DT_SITE, ConnectionService
    try:
        for hit in ConnectionService.list_connection_assets_by_type(ASSET_TYPE_DT_SITE):
            src = hit.get("_source") or {}
            if (src.get("provider_asset_id") or "") != team_esid:
                continue
            connection_esid = src.get("connection_esid") or ""
            if not connection_esid:
                continue
            token = ConnectionService.get_decrypted_token(connection_esid)
            if not token:
                continue
            saved_domain = _norm_dt_domain((src.get("metadata") or {}).get("dt_domain") or "")
            return token, saved_domain
    except Exception as e:  # noqa: BLE001 — fail-soft: caller raises "token required"
        logging.warning("connector_wizard: DT saved-site lookup failed for %s: %s", team_esid, e)
    return None


def configure_disciple_tools(
    team_esid: str,
    dt_domain: str,
    our_domain: str,
    token: str,
    username: str,
) -> dict[str, Any]:
    """configure a Disciple.Tools site for a team.

    Live-verifies the Site-to-Site Link credential against the DT site, then
    writes the ``de_connection`` + ``dt_site`` rows the pull importer reads
    (``ConnectionService.create_disciple_tools_site``, idempotent) and stamps
    ``team_disciple_tools_instance`` on the team — the field the connector
    status entry keys "configured" off. Nothing is written when verification
    fails.

    ``username`` is the authenticated session principal (route-supplied, never
    from the request body) and becomes the connection's owner on the Manage
    surface, mirroring the YouTube posture.
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    dt_domain = (dt_domain or "").strip()
    our_domain = (our_domain or "").strip()
    token = (token or "").strip()
    if not dt_domain or not our_domain:
        raise WizardError("Site domain and dashboard domain are both required.")
    if not username:
        raise WizardError("Not authenticated")
    # follow-up — keep-if-unchanged: the wizard shows the saved
    # token's CIPHERTEXT (Asset Explorer "Encrypted value shown" pattern) and
    # sends token="" when the user leaves it untouched. Resolve the stored
    # plaintext so verification and the re-upsert use the real credential.
    #
    # SECURITY: a saved token is PINNED to the domain it was
    # issued for. The submitted dt_domain is caller-controlled; if we reused
    # the saved token against a *different* domain we'd send that credential's
    # Site-Link bearer to an arbitrary host (exfiltration / SSRF). So reusing
    # the saved token is allowed only when dt_domain matches the stored one —
    # changing the site requires re-entering the token.
    if not token:
        saved = _existing_dt_site(team_esid)
        if not saved:
            raise WizardError("Site Link token is required.")
        saved_token, saved_domain = saved
        if _norm_dt_domain(dt_domain) != saved_domain:
            raise WizardError(
                "To point this team at a different Disciple.Tools site, re-enter the " "Site Link token for that site."
            )
        token = saved_token

    # Fail before the external probe if the team doesn't exist / isn't ours.
    existing_team = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing_team:
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")

    _verify_dt_site_link(dt_domain, our_domain, token)

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    result = ConnectionService.create_disciple_tools_site(
        principal=username,
        dt_domain=dt_domain,
        our_domain=our_domain,
        token=token,
        team_esid=team_esid,
    )
    if not result.get("success"):
        raise WizardError(result.get("error") or "Could not save the Disciple.Tools connection")

    # Stamp the status-driving URL field (connector_status_service keys the
    # "configured" state for disciple_tools off team_disciple_tools_instance,
    # not a ds_* flag). Merge-write so other team fields are preserved.
    norm_dt = dt_domain.replace("https://", "").replace("http://", "").replace("//", "").replace("/", "").strip()
    updated = dict(existing_team)
    updated["team_disciple_tools_instance"] = f"https://{norm_dt}"
    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")

    logging.info(
        "WizardService.configure_disciple_tools: team=%s dt_domain=%s connection=%s",
        team_esid,
        norm_dt,
        result.get("connection_esid"),
    )
    return {
        "connection_esid": result.get("connection_esid"),
        "asset_esid": result.get("asset_esid"),
        "dt_domain": norm_dt,
    }


# ── Bitly wizard ──────────────────────────────────────────


_BITLY_API_BASE = "https://api-ssl.bitly.com/v4"
_BITLY_VERIFY_TIMEOUT = 15  # seconds


def _verify_bitly_token(api_token: str) -> None:
    """Probe the Bitly v4 API with the token; raise WizardError on failure.

    Issues one authenticated ``GET /v4/user`` — the cheapest call that proves
    the token is live — so "the wizard accepted it" and "the nightly pull can
    read it" are the same check (DT `_verify_dt_site_link` posture). Hard-fail
    by design: a bad token validated only at import time surfaces as a silent
    nightly skip the operator never sees.

    Error messages are fixed strings — the user-typed token is never echoed
    back or logged (reflective-xss / credential-disclosure posture).
    """
    import requests

    try:
        response = requests.get(
            f"{_BITLY_API_BASE}/user",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=_BITLY_VERIFY_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 — network errors → operator-facing message
        logging.warning("connector_wizard: Bitly token probe failed: %s", type(e).__name__)
        raise WizardError("Could not reach the Bitly API. Check your network and try again.")
    if response.status_code in (401, 403):
        raise WizardError(
            "Bitly rejected the API token. Check the token in your Bitly account "
            "(Settings → Developer settings → API) and try again."
        )
    if response.status_code >= 400:
        logging.warning("connector_wizard: Bitly token probe returned HTTP %s", response.status_code)
        raise WizardError("Bitly returned an unexpected error while verifying the token. Try again later.")


def _existing_bitly_connection(team_esid: str) -> tuple[str, str, list[str]] | None:
    """The team's saved Bitly credential, DECRYPTED, with its stored tags.

    Returns ``(token_plaintext, connection_esid, tags)`` or None. Backs the
    wizard's keep-if-unchanged path (token field left showing dots → POST
    sends api_token="") — same shape as ``_existing_dt_site``. Fail-soft:
    None when the team has no bitly connection / no decryptable token.
    """
    try:
        from web.services.connection_service import ASSET_TYPE_BITLY_ACCOUNT, ConnectionService
    except ImportError:
        from .connection_service import ASSET_TYPE_BITLY_ACCOUNT, ConnectionService
    try:
        for hit in ConnectionService.list_connection_assets_by_type(ASSET_TYPE_BITLY_ACCOUNT):
            src = hit.get("_source") or {}
            if (src.get("provider_asset_id") or "") != team_esid:
                continue
            connection_esid = src.get("connection_esid") or ""
            if not connection_esid:
                continue
            token = ConnectionService.get_decrypted_token(connection_esid)
            if not token:
                continue
            tags = [str(t) for t in ((src.get("metadata") or {}).get("tags") or []) if str(t).strip()]
            return token, connection_esid, tags
    except Exception as e:  # noqa: BLE001 — fail-soft: caller raises "token required"
        logging.warning("connector_wizard: Bitly saved-connection lookup failed for %s: %s", team_esid, e)
    return None


def _normalize_bitly_tags(tags: Any) -> list[str]:
    """Accept a comma-separated string or a list; return sorted unique
    non-empty tag names. Pure (unit-testable)."""
    if isinstance(tags, str):
        parts = tags.split(",")
    else:
        parts = list(tags or [])
    return sorted({str(t).strip() for t in parts if str(t).strip()})


def configure_bitly(
    team_esid: str,
    api_token: str,
    tags: Any,
    username: str,
) -> dict[str, Any]:
    """Phase 3 — configure the Bitly connector for a team.

    Live-verifies the API token against Bitly's ``/v4/user``, then writes the
    ``de_connection`` (token encrypted) + ``bitly_account`` asset rows the
    importer reads (``ConnectionService.create_bitly_connection``, idempotent —
    same team reuses/refreshes its existing connection, never duplicates) and
    stamps ``ds_bitly`` + ``bitly_connection_esid`` on the team. Nothing is
    written when verification fails.

    ``tags`` is the tag filter (comma-separated string or list): only links
    carrying at least one of these Bitly tags are imported and attributed to
    this team. ``username`` is the authenticated session principal
    (route-supplied, never from the request body — posture).
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if not username:
        raise WizardError("Not authenticated")
    api_token = (api_token or "").strip()
    clean_tags = _normalize_bitly_tags(tags)
    if not clean_tags:
        raise WizardError("At least one Bitly tag is required.")

    # Keep-if-unchanged: the wizard shows the saved token as dots and sends
    # api_token="" when the user leaves it untouched (DT pattern). Resolve
    # the stored plaintext so verification and the re-upsert use the real
    # credential. Unlike DT there is no domain to pin — the token is only
    # ever replayed against the fixed Bitly API host.
    if not api_token:
        saved = _existing_bitly_connection(team_esid)
        if not saved:
            raise WizardError("Bitly API token is required.")
        api_token = saved[0]

    # Fail before the external probe if the team doesn't exist / isn't ours.
    existing_team = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing_team:
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")

    _verify_bitly_token(api_token)

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    team_title = str(existing_team.get("team_title") or "").strip()
    result = ConnectionService.create_bitly_connection(
        principal=username,
        team_esid=team_esid,
        api_token=api_token,
        tags=clean_tags,
        label=f"Bitly — {team_title}" if team_title else "Bitly",
    )
    if not result.get("success"):
        raise WizardError(result.get("error") or "Could not save the Bitly connection")

    _update_team_data_source(
        team_esid=team_esid,
        flag_field="ds_bitly",
        esid_field="bitly_connection_esid",
        esid_value=result["connection_esid"],
    )

    logging.info(
        "WizardService.configure_bitly: team=%s connection=%s tags=%s",
        team_esid,
        result.get("connection_esid"),
        clean_tags,
    )
    return {
        "connection_esid": result.get("connection_esid"),
        "asset_esid": result.get("asset_esid"),
        "tags": clean_tags,
    }


# ── Google Ads wizard ───────────────────────────────────────────


def google_ads_oauth_start(customer_id: str, username: str) -> dict[str, Any]:
    """begin the Google Ads per-user OAuth grant for a customer.

    Creates (or reuses, natural-key-safe) a pending ``de_connection``
    (provider=google_ads, provider_account_id=customer_id) owned by the
    authenticated ``username``, seeds a single-use OAuth state on it, and
    returns the popup path segment the frontend opens as
    ``https://{fboauth_hostname}/google_ads/{auth_url_path}``.

    SECURITY: ``principal`` is set from the
    authenticated session ``username`` passed by the route — NEVER from the
    request body. The customer_id is user-supplied; the principal is not. The
    fb_oauth-host callback binds the token commit to this principal.

    Returns ``{connection_esid, auth_url_path, has_token}``. ``has_token`` is
    almost always False here (the grant hasn't happened yet) but is returned
    for symmetry with the status poll and to short-circuit a re-auth of an
    already-connected customer.
    """
    cid = str(customer_id or "").strip()
    if not cid:
        raise WizardError("customer_id is required")
    if not username:
        raise WizardError("Not authenticated")

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    pending = ConnectionService.create_pending_connection(
        principal=username,
        provider="google_ads",
        provider_account_id=cid,
        label=f"Google Ads {cid}",
    )
    if not pending.get("success"):
        raise WizardError(pending.get("error") or "Could not create connection")
    connection_esid = pending["esid"]

    # Always seed a fresh OAuth state + auth_url, even when a token already
    # exists. has_token lets the frontend AUTO-SKIP the Authorize step for an
    # already-connected customer, but it must still be able to RE-AUTHORIZE if
    # the post-grant account-access validation fails (e.g. the granted Google
    # login can't reach the customer) — which needs a working popup path. Seeding
    # is a partial update (it never touches oauth_token_enc or status), so it's
    # safe on an active+tokened connection. (Previously this returned an empty
    # auth_url_path when tokened, which stranded the user on a button-less
    # Authorize step on a validate failure — the round-3 follow-up bug.)
    existing = ConnectionService.get_connection(connection_esid) or {}
    has_token = bool(existing.get("oauth_token_enc"))

    seeded = ConnectionService.seed_oauth_state(connection_esid)
    if not seeded.get("success"):
        raise WizardError(seeded.get("error") or "Could not start authorization")

    return {
        "connection_esid": connection_esid,
        "auth_url_path": seeded["auth_url"],
        "has_token": has_token,
    }


def google_ads_connection_status(connection_esid: str) -> dict[str, Any]:
    """poll a Google Ads connection for OAuth completion.

    Returns ``{connection_esid, has_token, status}``. The frontend polls this
    after opening the consent popup; ``has_token`` flips True once the
    fb_oauth-host callback has stored the refresh token and flipped the
    connection active.
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")
    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        raise WizardError("Connection not found")
    return {
        "connection_esid": connection_esid,
        "has_token": bool(doc.get("oauth_token_enc")),
        "status": doc.get("status") or "",
        # a seeded OAuth state is still pending; the callback's
        # upsert replaces the doc without it, so has_token and not authorizing
        # means a fresh grant landed (Reconnect waits for exactly that).
        "authorizing": bool(doc.get("oauth_state")),
    }


def google_ads_validate_oauth_customer(connection_esid: str, customer_id: str, username: str = "") -> dict[str, Any]:
    """verify the customer id is reachable with the connection's OWN
    OAuth token, called by the wizard right after the grant lands (end of the
    Authorize step) so a bogus-but-valid-format id is caught BEFORE Confirm.

    Probes via the user's just-granted refresh token (NOT our MCC service
    account), so the rejection message is OAuth-appropriate — it never tells
    the user to "link it under our MCC account" (that is the service-account
    model's constraint, irrelevant to a per-user grant).

    Returns ``{"ok": True}`` when the token can read the customer OR when the
    probe is non-definitive (infra/transient — fail-open, never block the
    wizard on our own flakiness). Raises ``WizardError`` only on a DEFINITIVE
    auth/not-found result (a real user error).
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")
    cid = str(customer_id or "").strip()
    if not cid:
        raise WizardError("customer_id is required")

    # IDOR gate: the connection holds an OAuth token, so the session
    # ``username`` MUST own it before we probe with that token. Without this, a
    # caller holding mc_data_connectors could pass another principal's
    # connection_esid and probe customers via the victim's token. Mirrors the
    # discovery-list / discovery-confirm ownership check. Empty username
    # (legacy/non-request caller) skips the gate, preserving the old contract for
    # internal callers that don't carry a session.
    if username:
        _require_google_ads_connection_owner(connection_esid, username)

    try:
        from import_scripts.google_ads.get_campaign_data import (
            probe_google_ads_customer_with_connection,
        )
    except ImportError:
        # Importer/Google libs unavailable in this env — fail-open (the
        # authoritative check still runs at Confirm via the configure path).
        return {"ok": True}

    ok, definitive, message = probe_google_ads_customer_with_connection(connection_esid, cid)
    if ok:
        return {"ok": True}
    if definitive:
        # the user-facing copy is deliberately generic, but the
        # raw API error (e.g. USER_PERMISSION_DENIED vs CUSTOMER_NOT_FOUND vs a
        # manager-account / login_customer_id mismatch) is the only thing that
        # tells us WHICH definitive cause fired. Log it so a failing wizard run
        # is diagnosable from /app/data/logs without re-instrumenting.
        logging.warning(
            "Google Ads OAuth probe DEFINITIVE reject for customer=%s connection=%s: %s",
            cid,
            connection_esid,
            message,
        )
        raise WizardError(
            "We can't access that Google Ads account with the Google login you "
            "authorized. Check the Customer ID, or re-authorize with a Google "
            "account that has access to it."
        )
    logging.warning(
        "Google Ads OAuth customer probe non-definitive for %s; allowing (fail-open): %s",
        cid,
        message,
    )
    return {"ok": True}


# ── Google Ads "Discover from a Google account" mode ──
#
# Unlike the customer-keyed path above (which knows the customer id BEFORE the
# popup), discovery authorizes FIRST then lists what the token can see. Option C
# (token-copy): mint a DISCOVERY connection keyed by a placeholder, authorize,
# list customers, then at confirm copy that token onto each chosen customer-keyed
# connection and link via the existing configure-append path.

# Placeholder natural-key prefix for the customer-id-less discovery connection.
_GADS_DISCOVERY_PREFIX = "discovery:"


def _require_google_ads_connection_owner(connection_esid: str, username: str) -> dict[str, Any]:
    """fetch a connection and assert the session ``username`` OWNS it.

    The discovery connection holds a Google OAuth refresh token. Reading/copying
    that token (discovery_list / discovery_confirm) must be gated on ownership, or
    one user could pass another user's ``connection_esid`` and enumerate THEIR
    accessible accounts or copy THEIR token onto their own customers (IDOR /
    token theft). ``principal`` is the connection's owner, set from the session at
    create time (never the request body), so it is the authoritative check.

    Returns the connection doc on success. Raises ``WizardError`` with a generic
    "not found" (does NOT distinguish missing-vs-not-yours) on any failure, so the
    endpoint can't be used to probe which connection esids exist.
    """
    if not username:
        raise WizardError("Not authenticated")
    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    doc = ConnectionService.get_connection(connection_esid)
    if not doc or doc.get("principal") != username:
        raise WizardError("Connection not found")
    return doc


def google_ads_discovery_start(username: str) -> dict[str, Any]:
    """Step 1 — begin a customer-id-less Google Ads OAuth grant.

    Mints a pending ``de_connection`` keyed by a placeholder
    ``provider_account_id`` (``discovery:<uuid>``) owned by ``username`` and seeds
    an OAuth state. The user authorizes ONE Google login; the granted token lands
    on this discovery connection. ``google_ads_discovery_list`` then enumerates the
    customers the token can see, and ``google_ads_discovery_confirm`` copies the
    token onto the chosen customer-keyed connections.

    SECURITY: principal is the authenticated session ``username`` (route-supplied),
    never the request body. Returns ``{connection_esid, auth_url_path, has_token}``.
    """
    if not username:
        raise WizardError("Not authenticated")

    import uuid

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    # a discovery connection is per-user scratch identity, not
    # per-attempt: reuse the caller's existing one (any status) instead of
    # minting a fresh uuid-keyed row per click. Before this, every retry and
    # abandoned consent screen left another orphaned de_connection behind
    # (four in 131 seconds on dev, 2026-07-30), none of them reaped — the
    # discovery: prefix never matched the wizard-pending sweeper.
    reusable = ConnectionService.find_connection_by_account_prefix(
        principal=username,
        provider="google_ads",
        account_id_prefix=_GADS_DISCOVERY_PREFIX,
    )
    if reusable:
        connection_esid = reusable["_id"]
        logging.info(
            "reusing discovery connection %s for %s (no duplicate created)",
            connection_esid,
            username,
        )
    else:
        placeholder = f"{_GADS_DISCOVERY_PREFIX}{uuid.uuid4()}"
        pending = ConnectionService.create_pending_connection(
            principal=username,
            provider="google_ads",
            provider_account_id=placeholder,
            label="Google Ads (discover accounts)",
        )
        if not pending.get("success"):
            raise WizardError(pending.get("error") or "Could not start discovery")
        connection_esid = pending["esid"]

    seeded = ConnectionService.seed_oauth_state(connection_esid)
    if not seeded.get("success"):
        raise WizardError(seeded.get("error") or "Could not start authorization")

    existing = ConnectionService.get_connection(connection_esid) or {}
    return {
        "connection_esid": connection_esid,
        "auth_url_path": seeded["auth_url"],
        "has_token": bool(existing.get("oauth_token_enc")),
    }


def google_ads_discovery_list(connection_esid: str, username: str) -> dict[str, Any]:
    """Step 2 — list the Google Ads customers the discovery connection's
    token can add to a team (own non-manager accounts + leaf clients under any
    accessible MCC).

    SECURITY: the connection holds a Google OAuth token, so the session
    ``username`` MUST own it — otherwise one user could enumerate another user's
    accessible accounts via their token. Ownership is verified before any token use.

    Returns ``{"customers": [{"id", "name", "manager": False}, ...]}`` — only
    linkable accounts (never a bare MCC). Empty list when the token sees nothing
    addable. Raises ``WizardError`` if the connection has no token yet (the user
    hasn't finished the grant).
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")

    doc = _require_google_ads_connection_owner(connection_esid, username)
    if not doc.get("oauth_token_enc"):
        raise WizardError("Authorization not complete yet")

    try:
        from import_scripts.google_ads.get_campaign_data import (
            list_accessible_customers_for_connection,
        )
    except ImportError:
        # Google libs unavailable in this env — surface as empty rather than 500.
        return {"customers": []}

    customers = list_accessible_customers_for_connection(connection_esid)
    return {"customers": customers}


def google_ads_discovery_confirm(
    connection_esid: str,
    team_esid: str,
    customers: list[dict[str, Any]],
    username: str,
) -> dict[str, Any]:
    """Step 3 — link the chosen discovered customers to the team.

    For each chosen customer, COPIES the discovery connection's token onto a
    customer-keyed ``de_connection`` (``upsert_connection``) so the per-customer
    OAuth path is satisfied WITHOUT a second popup, then links it to the team via
    the existing ``configure_google_ads_customer`` create-new (append) path —
    which dedupes the asset on customer id, stamps the workspace, and appends to
    ``team_google_ads_customer_esids``. ``customers`` is a list of
    ``{"id", "name"}`` from ``google_ads_discovery_list``.

    Returns ``{"linked": [<customer_id>...], "errors": [{"id", "error"}...]}``.
    A per-customer failure is collected (not fatal) so one bad id doesn't drop
    the rest. Raises ``WizardError`` only on a wholly invalid request.
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")
    if not team_esid:
        raise WizardError("team_esid is required")
    if not username:
        raise WizardError("Not authenticated")
    chosen = [c for c in (customers or []) if str((c or {}).get("id") or "").strip()]
    if not chosen:
        raise WizardError("Select at least one account to add")

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    # SECURITY: verify the session user OWNS the discovery connection
    # before reading/copying its OAuth token — else one user could copy another
    # user's Google token onto their own customers (IDOR / token theft).
    _require_google_ads_connection_owner(connection_esid, username)

    # Read the discovery token ONCE; copy it onto each customer-keyed connection.
    token = ConnectionService.get_decrypted_token(connection_esid)
    if not token:
        raise WizardError("Authorization not complete yet")

    # the chosen ids arrive in the request body. Link only customers
    # this grant can list (the same set google_ads_discovery_list offered);
    # copying the token onto an id it cannot reach would attach another
    # tenant's customer to this team. The lister returns [] on any failure,
    # which refuses every id.
    try:
        from import_scripts.google_ads.get_campaign_data import (
            list_accessible_customers_for_connection,
        )
    except ImportError as e:
        raise WizardError("Google Ads support is not available on this server. Contact support.") from e
    listable = {str(c.get("id") or "").replace("-", "") for c in list_accessible_customers_for_connection(connection_esid)}

    linked: list[str] = []
    errors: list[dict[str, str]] = []
    for cust in chosen:
        cid = str(cust.get("id") or "").strip()
        title = str(cust.get("name") or "").strip() or f"Google Ads {cid}"
        if cid.replace("-", "") not in listable:
            logging.warning(
                "connector_wizard:  refused unlisted Google Ads customer %s for connection %s", cid, connection_esid
            )
            errors.append({"id": cid, "error": "This account is not available to the connected Google login."})
            continue
        try:
            # 1) Token-copy: mint/refresh the customer-keyed connection with the
            #    discovery grant's token, so _has_google_ads_oauth_token(cid) is
            # True and configure skips the standalone probe + the gate.
            up = ConnectionService.upsert_connection(
                principal=username,
                provider="google_ads",
                provider_account_id=cid,
                oauth_token=token,
                label=title,
            )
            if not up.get("success"):
                errors.append({"id": cid, "error": up.get("error") or "could not store connection"})
                continue
            # 2) Link to the team (create-new path: dedupes asset on id, appends).
            configure_google_ads_customer(
                team_esid=team_esid,
                customer_esid=None,
                new_customer={
                    "google_ads_customer_id": cid,
                    "google_ads_customer_title": title,
                },
            )
            linked.append(cid)
        except WizardError as e:
            errors.append({"id": cid, "error": str(e)})
        except Exception as e:  # noqa: BLE001 — one bad id must not drop the rest
            logging.warning("discovery_confirm: linking customer %s failed: %s", cid, e)
            # the raw exception is in the log above, not the response.
            errors.append({"id": cid, "error": "Could not link this customer. Try again."})

    return {"linked": linked, "errors": errors}


def _viewer_may_use_connection(connection_esid: str) -> bool:
    """may the current request's user attach ``connection_esid``?

    Platform admins (superuser / asset_admin) may — staff set connectors up
    for partner teams. Anyone else must pass the connections blueprint's
    ``_viewer_can_see_connection`` (their own connection, an org-shared one
    in their workspace, or one already serving a workspace they own). Any
    lookup error refuses. Outside a request there is no viewer to check; this
    function is only reached from the wizard routes, so that path exists for
    direct unit tests of the service alone.
    """
    from flask import has_request_context, session

    if not has_request_context():
        return True
    roles = session.get("roles", []) or []
    if "superuser" in roles or "asset_admin" in roles:
        return True
    username = session.get("username") or ""
    if not username:
        return False
    try:
        try:
            from web.blueprints.connections.routes import _viewer_can_see_connection
            from web.services.connection_service import ConnectionService
        except ImportError:
            from ..blueprints.connections.routes import _viewer_can_see_connection
            from .connection_service import ConnectionService
        source = ConnectionService.get_connection(connection_esid)
        return bool(
            source
            and _viewer_can_see_connection(
                source, viewer_username=username, connection_esid=connection_esid, viewer_roles=roles
            )
        )
    except Exception as e:  # noqa: BLE001 — an authz-resolution error must refuse, never silently allow
        logging.warning("connector_wizard:  connection visibility check failed for %s: %s", connection_esid, e)
        return False


def _connection_backs_customer(connection_esid: str, customer_id: str) -> bool:
    """True iff ``connection_esid`` is a live, tokened google_ads
    connection for ``customer_id`` that the caller may use."""
    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    doc = ConnectionService.get_connection(connection_esid) or {}
    same_customer = str(doc.get("provider_account_id") or "").replace("-", "") == str(customer_id or "").replace("-", "")
    return bool(
        doc.get("provider") == "google_ads"
        and doc.get("status") == "active"
        and doc.get("oauth_token_enc")
        and same_customer
        and _viewer_may_use_connection(connection_esid)
    )


def configure_google_ads_customer(
    team_esid: str,
    customer_esid: str | None,
    new_customer: dict[str, Any] | None = None,
    prefer_connection_esid: str | None = None,
) -> dict[str, Any]:
    """Configure Google Ads for a team.

    Two modes, mutually exclusive:

    * ``customer_esid`` (pick existing) — link an existing
      de_asset_google_ads_customers row to the team.
    * ``new_customer`` (create new) — create a new
      de_asset_google_ads_customers row with the provided fields
      (``google_ads_customer_title`` + ``google_ads_customer_id``)
      then link it. Validation runs through
      ``FormService.validate_required_fields`` against the
      ASSET_CONFIGS["google_ads_customers"] schema, so the
      "Customer ID is required" / "Title is required" gates are
      shared with the Asset Explorer add form.

    Either path writes ``google_ads_customer_esid`` +
    ``ds_google_ads="true"`` +
    ``team_google_ads_integration_status="Active"`` to the team doc.
    The integration-status field is load-bearing — the team-edit form
    gates the customer picker on it, so omitting it would leave the
    team in a state where the customer is linked but the edit form
    would hide it.
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if bool(customer_esid) == bool(new_customer):
        raise WizardError("Provide exactly one of customer_esid or new_customer")

    # validate the customer ID (format + live existence probe under
    # our MCC) BEFORE creating/linking any asset. Resolve the id being linked:
    # from new_customer for create, or the existing asset doc for pick-existing.
    if new_customer:
        google_ads_customer_id = str((new_customer or {}).get("google_ads_customer_id") or "").strip()
    else:
        existing_asset = AssetService.get_by_id("de_asset_google_ads_customers", customer_esid) or {}
        google_ads_customer_id = str(existing_asset.get("google_ads_customer_id") or "").strip()
    try:
        from web.services.connector_validation_service import validate_google_ads_customer
    except ImportError:
        from .connector_validation_service import validate_google_ads_customer

    # Resolve the team FIRST — the guard below needs its
    # google_ads_connection_esid stamp, the create-new path needs its
    # workspace_esid, and a missing team should fail fast before
    # any asset write.
    existing = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing:
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")
    team_workspace_esid = (str(existing.get("workspace_esid") or "").strip()) or None

    # pick-existing takes an esid from the request body. A customer
    # row owned by another workspace reads as missing (no enumeration); rows
    # are per-workspace, so a legitimate pick is always local.
    if not new_customer:
        asset_ws = str(existing_asset.get("workspace_esid") or "").strip()
        if not existing_asset or (asset_ws and asset_ws != (team_workspace_esid or "")):
            logging.warning(
                "connector_wizard:  refused Google Ads customer esid %r for team %s", customer_esid, team_esid
            )
            raise WizardError("Customer not found")

    # resolve the connection that will BACK this link, team-first.
    # The old guard (_has_google_ads_oauth_token) was team-blind: ANY tokened
    # connection for the customer — including another team's — returned True,
    # which suppressed the Authorize step while the team's own
    # google_ads_connection_esid stamp stayed stale, so the pill (which reads
    # the stamp) showed Disconnected with no way to fix it from the UI.
    # Resolution order: the team's own stamp if it is live for this customer
    #, else any live tokened match
    # (sharing one connection across teams is the working norm — one team and
    # Data Connector Test both ride one connection) — and whatever resolves is
    # STAMPED below, so guard, stamp and pill can never disagree again.
    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService

    team_stamp = str(existing.get("google_ads_connection_esid") or "").strip()
    stamp_doc = ConnectionService.get_connection(team_stamp) if team_stamp else None
    stamp_live = bool(
        stamp_doc
        and stamp_doc.get("provider") == "google_ads"
        and stamp_doc.get("status") == "active"
        and stamp_doc.get("oauth_token_enc")
    )
    backing_connection_esid = None
    try:
        if stamp_live:
            backing_connection_esid = ConnectionService.get_google_ads_oauth_connection_esid(
                google_ads_customer_id, prefer_connection_esid=team_stamp
            )
        if not backing_connection_esid:
            backing_connection_esid = ConnectionService.get_google_ads_oauth_connection_esid(google_ads_customer_id)
    except Exception as e:  # noqa: BLE001 — fail-soft, mirrors _has_google_ads_oauth_token
        logging.warning("connector_wizard:  backing-connection resolve failed for %s: %s", google_ads_customer_id, e)
        backing_connection_esid = None

    # the fallback above matches ANY tenant's live connection for
    # this customer id, and whatever resolves is stamped onto this team, so a
    # caller who merely knew another tenant's customer id got that tenant's
    # data imported with that tenant's refresh token. The team's own live
    # stamp stays trusted; any other connection must be one the caller may use.
    if backing_connection_esid and not (stamp_live and backing_connection_esid == team_stamp):
        if not _viewer_may_use_connection(backing_connection_esid):
            logging.warning(
                "connector_wizard:  refused connection %s for customer %s on team %s " "(not visible to the caller)",
                backing_connection_esid,
                google_ads_customer_id,
                team_esid,
            )
            backing_connection_esid = None

    # the connection the wizard just authorized (Reconnect / a fresh
    # grant) backs the link when the team has no live stamp of its own. Without
    # this the fallback above could pick any other live connection for the same
    # customer and the user's new sign-in went unused. A live team stamp is
    # still kept (it may serve the team's other customers).
    if (
        prefer_connection_esid
        and not stamp_live
        and _connection_backs_customer(prefer_connection_esid, google_ads_customer_id)
    ):
        backing_connection_esid = prefer_connection_esid

    # when the customer already has an active tokened google_ads
    # connection, the wizard's Authorize step already validated the id with the
    # user's own token (google_ads_validate_oauth_customer), so skip the
    # standalone probe here. the standalone validate_google_ads_customer
    # now fails open (the shared service account is retired; there is no MCC
    # identity to probe a bare id with), so a no-token customer is allowed
    # through on format alone and surfaces a "not connected" import error at
    # import time rather than being rejected here.
    has_oauth_token = bool(backing_connection_esid)
    if not has_oauth_token:
        validate_google_ads_customer(google_ads_customer_id)

    # the "use existing" wizard path linked a picked customer here
    # without ever performing the per-user OAuth grant. earlier (service
    # account retired) a tokenless customer is un-importable, so linking one
    # produced a success-looking configure that the card downgrade then
    # showed as "disconnected". Gate it: when a PICKED-EXISTING customer has no
    # usable OAuth token, do NOT link — signal needs_authorization so the wizard
    # opens the Authorize (oauth-start) step for this customer instead. The
    # create-new path is exempt: its OAuth grant lands on the de_connection
    # BEFORE this call, so has_oauth_token is already true there; a
    # genuinely tokenless create-new would simply fall through and import-time
    # surfaces the not-connected error (unchanged earlier behavior).
    if not new_customer and not has_oauth_token:
        existing_title = str((existing_asset or {}).get("google_ads_customer_title") or "").strip()
        raise WizardError(
            "This Google Ads customer hasn't been authorized yet. Authorize it with Google to connect it.",
            needs_authorization=True,
            customer_id=google_ads_customer_id,
            customer_title=existing_title,
        )

    created = False
    if new_customer:
        # idempotent, race-proof create keyed on the natural key
        # (customer_id, workspace_esid). The Google Ads OAuth wizard is type-only
        # (no pick-existing step), so a re-run / Reconnect — or a read-after-write
        # race where the read-then-create lookup missed a not-yet-visible
        # prior write — would otherwise mint a duplicate row (a 4-doc
        # incident). We derive a DETERMINISTIC _id from the key and create with
        # op_type=create: a 409 means the doc already exists, so we reuse that id
        # instead of creating a second one. The key includes workspace_esid
        # because the same customer in two workspaces is intentional (one per
        # workspace — e.g. the Google app-review youtube-workspace copy).
        det_id = _deterministic_asset_id("google_ads_customer", google_ads_customer_id, team_workspace_esid or "")
        # Stamp workspace_esid AT create time (not after) so a newly created
        # customer is never a blank-workspace orphan dropped fail-closed by Asset
        # Explorer / the picker (the gap that _stamp_asset_workspace
        # patched after the fact). No-ops to absent when the team has no workspace.
        customer_fields = dict(new_customer)
        if team_workspace_esid:
            customer_fields["workspace_esid"] = team_workspace_esid
        try:
            customer_esid = _create_asset("google_ads_customers", customer_fields, doc_id=det_id)
            created = True
        except WizardError as e:
            # op_type=create 409 → the deterministic-id doc already exists; reuse
            # it. Any other WizardError (validation, ES down) still propagates.
            if "already exists" in str(e).lower():
                customer_esid = det_id
            else:
                raise

    # Safety net for the pick-existing path and earlier random-id docs whose
    # workspace_esid was never stamped: a no-op when the doc already carries it.
    _stamp_asset_workspace("de_asset_google_ads_customers", customer_esid, team_workspace_esid)

    # resolve the customer's display title and stamp it on the team
    # alongside the esid. The team-edit form's "Google Ads Customer" chip
    # displays ``google_ads_customer_title`` (its value field); writing only the
    # esid left the chip blank even though the link was correct. Prefer the
    # new_customer payload's title (create path), else read the linked doc.
    google_ads_customer_title = ""
    if new_customer:
        google_ads_customer_title = str((new_customer or {}).get("google_ads_customer_title") or "").strip()
    if not google_ads_customer_title:
        cust_doc = AssetService.get_by_id("de_asset_google_ads_customers", customer_esid) or {}
        google_ads_customer_title = str(cust_doc.get("google_ads_customer_title") or "").strip()

    # Phase 1 — a team can hold MULTIPLE Google Ads customers. Append
    # the esid to the list field team_google_ads_customer_esids (mirrors
    # configure_youtube's append posture on team_youtube_channel_handle_esids),
    # deduped and with dangling esids pruned so a re-link doesn't accumulate
    # stale references. During the scalar→list transition we KEEP writing the
    # scalar google_ads_customer_esid = list[0] (and its title) so the ~35
    # existing scalar readers stay unbroken until they're migrated; the scalar
    # is dropped in a later cleanup ticket.
    current = existing.get("team_google_ads_customer_esids") or []
    if isinstance(current, str):
        current = [current] if current else []
    elif not isinstance(current, list):
        current = []
    # Back-compat seed: a pre-migration team carries only the scalar. Fold it in
    # so the first multi-account configure on an un-migrated team doesn't drop
    # the existing single customer.
    legacy_scalar = (existing.get("google_ads_customer_esid") or "").strip()
    if legacy_scalar and legacy_scalar not in current:
        current.append(legacy_scalar)
    current, pruned = _prune_dangling_esids(current, "de_asset_google_ads_customers")
    if customer_esid not in current:
        current.append(customer_esid)

    updated = dict(existing)
    updated["ds_google_ads"] = "true"
    updated["team_google_ads_integration_status"] = "Active"
    updated["team_google_ads_customer_esids"] = current
    # defect 2 — configure never wrote google_ads_connection_esid (the
    # tokenless-finalize path was its only writer), so a stale pointer survived
    # every reconfigure and the status pill (which reads it) stayed
    # Disconnected. Stamp the connection that backed this link. A LIVE existing
    # stamp is kept even when this customer resolved elsewhere — it may be
    # serving the team's other customers, and
    # flipping it would break their imports; a dead/stale pointer is replaced,
    # or cleared when nothing resolves (mirrors the dangling-esid
    # pruning, which had no equivalent for the connection pointer).
    if backing_connection_esid and not stamp_live:
        updated["google_ads_connection_esid"] = backing_connection_esid
    elif not backing_connection_esid and team_stamp and not stamp_live:
        updated["google_ads_connection_esid"] = ""
    elif backing_connection_esid and stamp_live and backing_connection_esid != team_stamp:
        logging.info(
            "connector_wizard:  keeping live stamp %s on team %s; customer %s "
            "resolved to %s (may serve the team's other customers)",
            team_stamp,
            team_esid,
            google_ads_customer_id,
            backing_connection_esid,
        )
    # Transition scalar = the team's "primary" customer. Keep an already-set
    # primary STABLE when a second customer is added (the 35 scalar readers
    # treat it as the team's main account — adding another shouldn't flip it);
    # on a first link the just-added customer is the primary.
    primary_esid = legacy_scalar if (legacy_scalar and legacy_scalar in current) else customer_esid
    updated["google_ads_customer_esid"] = primary_esid
    if primary_esid == customer_esid and google_ads_customer_title:
        updated["google_ads_customer_title"] = google_ads_customer_title
    elif primary_esid != customer_esid:
        # Primary is the pre-existing scalar; resolve its title for the chip.
        primary_doc = AssetService.get_by_id("de_asset_google_ads_customers", primary_esid) or {}
        primary_title = str(primary_doc.get("google_ads_customer_title") or "").strip()
        if primary_title:
            updated["google_ads_customer_title"] = primary_title

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")

    _audit_pruned_references(pruned, "de_asset_google_ads_customers", "team_google_ads_customer_esids", team_esid, existing)

    logging.info(
        "WizardService.configure_google_ads_customer: team=%s customer=%s created=%s",
        team_esid,
        customer_esid,
        created,
    )
    # report the connection now stamped on the team (empty when none
    # resolved) so the route reuses it instead of minting a tokenless
    # placeholder that would re-point the team at "Pending Token".
    return {
        "customer_esid": customer_esid,
        "created": created,
        "customer_id": google_ads_customer_id,
        "connection_esid": str(updated.get("google_ads_connection_esid") or ""),
    }


# ── GA4 wizard ──────────────────────────────────────────────────


_GA_PROPERTY_ELSEWHERE_MESSAGE = (
    "This Google Analytics property is already connected in another workspace. " "Contact support if it should be shared."
)


def _ga_property_rows_for_team(ga_property_id: str, team_esid: str, team_ws: str | None) -> list[str]:
    """the existing de_asset_ga_properties rows for ``ga_property_id``
    that ``team_esid`` may reuse, or ``[]`` when none exist yet (create one).

    The dedup reused the first row cluster-wide then
    re-stamped its ``workspace_esid`` to the caller's team, so naming another
    tenant's property id moved that tenant's row (and its data) into the
    caller's workspace. A row already owned by another workspace is now refused
    rather than reused or created alongside. A workspace-less row is owned by
    whichever team links it. Any lookup failure declines.
    """
    from lib.es_helpers import get_elasticsearch_client

    try:
        es = get_elasticsearch_client()
        resp = es.search(
            index=GA_PROPERTIES_INDEX,
            size=50,
            _source=["workspace_esid"],
            query={"term": {"ga_property_id": ga_property_id}},
        )
    except Exception:  # noqa: BLE001 — an authz-relevant lookup must fail closed
        logging.exception("connector_wizard:  GA4 property lookup failed for team %s", team_esid)
        raise WizardError("Could not verify the Google Analytics property. Try again in a moment.")
    body = resp.body if hasattr(resp, "body") else resp
    reusable: list[str] = []
    for hit in (body.get("hits") or {}).get("hits") or []:
        row_ws = str((hit.get("_source") or {}).get("workspace_esid") or "").strip()
        if row_ws and row_ws != (team_ws or ""):
            logging.warning(
                "connector_wizard:  refused GA4 property %s for team %s: owned by workspace %s",
                ga_property_id,
                team_esid,
                row_ws,
            )
            raise WizardError(_GA_PROPERTY_ELSEWHERE_MESSAGE)
        if not row_ws:
            _refuse_ga_property_linked_elsewhere(hit.get("_id") or "", team_esid, team_ws)
        reusable.append(hit.get("_id") or "")
    return [esid for esid in reusable if esid]


def _refuse_ga_property_linked_elsewhere(property_esid: str, team_esid: str, team_ws: str | None) -> None:
    """refuse a workspace-less property row that a team outside
    ``team_ws`` already links (``team_ga4_property_esids``). Fails closed."""
    from lib.es_helpers import get_elasticsearch_client

    try:
        es = get_elasticsearch_client()
        resp = es.search(
            index=TEAMS_INDEX,
            size=50,
            _source=["workspace_esid"],
            query={
                "bool": {
                    "filter": [{"term": {"team_ga4_property_esids": property_esid}}],
                    "must_not": [{"ids": {"values": [team_esid]}}],
                }
            },
        )
    except Exception:  # noqa: BLE001 — an authz-relevant lookup must fail closed
        logging.exception("connector_wizard:  GA4 property link lookup failed for team %s", team_esid)
        raise WizardError("Could not verify the Google Analytics property. Try again in a moment.")
    body = resp.body if hasattr(resp, "body") else resp
    for hit in (body.get("hits") or {}).get("hits") or []:
        other_ws = str((hit.get("_source") or {}).get("workspace_esid") or "").strip()
        if not other_ws or other_ws != (team_ws or ""):
            logging.warning(
                "connector_wizard:  refused GA4 property row %s for team %s: linked by team %s",
                property_esid,
                team_esid,
                hit.get("_id"),
            )
            raise WizardError(_GA_PROPERTY_ELSEWHERE_MESSAGE)


def configure_ga4_property(
    team_esid: str,
    property_esid: str | None,
    new_property: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Configure a GA4 property for a team — append-style.

    Two modes:

    * ``property_esid`` (pick existing) — append an existing
      de_asset_ga_properties row to the team's
      ``team_ga4_property_esids``.
    * ``new_property`` (create new) — create a new
      de_asset_ga_properties row (``ga_property_title`` +
      ``ga_property_id`` + ``ga_property_display_name``) then append.

    Always append rather than overwrite — a team can hold multiple
    properties and the user shouldn't lose existing links by adding
    a new one. To unlink, the user uses the team-edit page's multi-
    select picker.

    Also flips ``ds_google_analytics="true"`` so the team-edit page's
    gating checkbox is satisfied and the GA4 importer's stage-1
    filter picks the team up.
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if bool(property_esid) == bool(new_property):
        raise WizardError("Provide exactly one of property_esid or new_property")

    existing = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing:
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")
    team_ws = (existing.get("workspace_esid") or "").strip() or None

    # validate the GA4 property ID (format + live existence probe with
    # the service account) BEFORE creating/linking any asset, so a bad ID never
    # produces a false "Configured" card or an orphan asset row. Resolve the id
    # being linked: from new_property for create, or the existing asset doc for
    # pick-existing.
    if new_property:
        ga_property_id = str((new_property or {}).get("ga_property_id") or "").strip()
    else:
        existing_asset = AssetService.get_by_id("de_asset_ga_properties", property_esid) or {}
        # pick-existing takes an esid from the request body. A row
        # owned by another workspace reads as missing (no enumeration).
        asset_ws = str(existing_asset.get("workspace_esid") or "").strip()
        if not existing_asset or (asset_ws and asset_ws != (team_ws or "")):
            logging.warning("connector_wizard:  refused GA4 property esid %r for team %s", property_esid, team_esid)
            raise WizardError("Property not found")
        if not asset_ws:
            _refuse_ga_property_linked_elsewhere(property_esid, team_esid, team_ws)
        ga_property_id = str(existing_asset.get("ga_property_id") or "").strip()
    try:
        from web.services.connector_validation_service import validate_ga4_property
    except ImportError:
        from .connector_validation_service import validate_ga4_property
    validate_ga4_property(ga_property_id)

    created = False
    if new_property:
        # dedupe on ga_property_id (same random-_id trap as the Google
        # Ads sibling): re-adding a property that already exists must reuse its
        # row, not mint a duplicate de_asset_ga_properties orphan. Matched on
        # .keyword (text+keyword field). Found → link the existing ESID.
        # reuse only a row this team's workspace owns; a row owned
        # elsewhere refuses inside the lookup.
        reusable = _ga_property_rows_for_team(ga_property_id, team_esid, team_ws)
        if reusable:
            property_esid = reusable[0]
        else:
            property_esid = _create_asset("ga_properties", new_property)
            created = True

    # stamp the team's workspace_esid onto the GA4 property doc.
    # _create_asset above writes the row before the team (and thus workspace)
    # is resolved, so it carried no workspace_esid and was dropped fail-closed
    # by the picker + Asset Explorer for workspace-scoped operators. Mirrors the
    # YouTube stamp. Fail-soft: the team link below already landed.
    # stamp only a workspace-less row; never re-point an owned one.
    if team_ws and property_esid:
        try:
            prop_doc = AssetService.get_by_id(GA_PROPERTIES_INDEX, property_esid)
            if prop_doc is not None and not str(prop_doc.get("workspace_esid") or "").strip():
                AssetService.update(
                    GA_PROPERTIES_INDEX,
                    property_esid,
                    {**prop_doc, "workspace_esid": team_ws},
                )
        except Exception as e:  # noqa: BLE001 — fail-soft: team link already persisted
            logging.warning(
                "connector_wizard: GA4 property workspace_esid stamp failed team=%s property=%s ws=%s err=%s",
                team_esid,
                property_esid,
                team_ws,
                e,
            )

    current = existing.get("team_ga4_property_esids") or []
    # Defensive: the field can legitimately be a single string in some
    # old docs (earlier schema). Normalise to a list before
    # appending so the resulting field is uniformly multi-shaped.
    if isinstance(current, str):
        current = [current] if current else []
    elif not isinstance(current, list):
        current = []
    # prune ESIDs that no longer resolve before appending, so a
    # re-connect also clears a dangling reference instead of accumulating it.
    current, pruned = _prune_dangling_esids(current, "de_asset_ga_properties")
    if property_esid not in current:
        current.append(property_esid)

    updated = dict(existing)
    updated["ds_google_analytics"] = "true"
    updated["team_ga4_property_esids"] = current

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")

    _audit_pruned_references(pruned, "de_asset_ga_properties", "team_ga4_property_esids", team_esid, existing)

    logging.info(
        "WizardService.configure_ga4_property: team=%s property=%s count=%d created=%s",
        team_esid,
        property_esid,
        len(current),
        created,
    )
    return {"property_esid": property_esid, "property_count": len(current), "created": created}


# ── GA4 OAuth wizard ───────────────────────────────────────────────
#
# Flow mirrors YouTube: ga4_account (OAuth) → list properties (Admin API
# list_account_summaries) → link properties to the team. One ga4_account doc
# carries the OAuth slug + state; the fb_oauth_server /ga4/auth_callback leg
# writes the encrypted refresh token onto it (ga4_account_token). Properties are
# enumerated live from the granted account and persisted as de_asset_ga_properties
# rows (the existing importer + team link already read them).


def ga4_oauth_pick_or_create_account(
    account_esid: str | None,
    new_account: dict[str, Any] | None,
    username: str = "",
    allowed_workspaces: list[str] | tuple[str, ...] | None = (),
) -> dict[str, Any]:
    """Step 1: pick an existing ga4_account or create one, then return its
    OAuth status. Mirrors :func:`youtube_oauth_pick_or_create_account`.

    Creating an account routes through ``_create_asset`` so the
    ``auto_generate`` config stamps ``ga4_account_auth_url`` (15-char, unique)
    + ``ga4_account_auth_state`` (25-char). The frontend builds the popup URL
    as ``https://{fboauth_hostname}/ga4/{auth_url_path}``.

    Returns ``{"account_esid", "auth_url_path", "has_token", "account_title"}``.
    """
    if bool(account_esid) == bool(new_account):
        raise WizardError("Provide exactly one of account_esid or new_account")
    if not account_esid:
        email = str((new_account or {}).get("ga4_account_email") or "").strip()
        try:
            from web.services.connector_validation_service import is_valid_email_format
        except ImportError:
            from .connector_validation_service import is_valid_email_format
        if email and not is_valid_email_format(email):
            raise WizardError("Enter a valid Google account email address (e.g. name@example.com).")
        # dedup on the Google email (see youtube_oauth_pick_or_create_account).
        account_esid = _reuse_or_create_oauth_account(
            "ga4_accounts",
            dict(new_account),
            "ga4_account_email",
            key_is_email=True,
            created_by=username,
            # parity with YouTube: one Google email can
            # front several identities; reusing a TOKENED doc on a same-email
            # re-add would let the callback overwrite a working identity's
            # token in place. Only tokenless placeholders are reused.
            reuse_only_tokenless_field="ga4_account_token",
            allowed_workspaces=allowed_workspaces,
        )
    return _read_ga4_account_status(account_esid)


def ga4_account_status(account_esid: str) -> dict[str, Any]:
    """Step 2 polling: re-read a ga4_account to check whether the OAuth callback
    has populated the refresh token. Same return shape as
    :func:`ga4_oauth_pick_or_create_account`.
    """
    return _read_ga4_account_status(account_esid)


def _read_ga4_account_status(account_esid: str) -> dict[str, Any]:
    if not account_esid:
        raise WizardError("account_esid is required")
    source = AssetService.get_by_id(GA4_ACCOUNTS_INDEX, account_esid)
    if not source:
        logging.warning("connector_wizard: ga4_account not found account_esid=%r", account_esid)
        raise WizardError("Google Analytics account not found")
    return {
        "account_esid": account_esid,
        "auth_url_path": source.get("ga4_account_auth_url") or "",
        "account_title": source.get("ga4_account_title") or "",
        "has_token": bool(source.get("ga4_account_token")),
    }


def ga4_list_properties(account_esid: str) -> list[dict[str, str]]:
    """Step 3: list the GA4 properties the authorized Google account can access,
    via the Admin API ``list_account_summaries()``. Mirrors
    :func:`youtube_list_channels`.

    Builds OAuth2 credentials from the account's stored refresh token using the
    same importer helper the daily import uses
    (``ga_config.load_ga4_oauth_credentials``), so the wizard and the importer
    authorize identically. Returns ``[{"id", "title"}]`` sorted by title, where
    ``id`` is the numeric GA4 property id.
    """
    if not account_esid:
        raise WizardError("account_esid is required")
    source = AssetService.get_by_id(GA4_ACCOUNTS_INDEX, account_esid)
    if not source:
        logging.warning("connector_wizard: ga4_account not found account_esid=%r", account_esid)
        raise WizardError("Google Analytics account not found")
    if not source.get("ga4_account_token"):
        raise WizardError("This account has no access token yet. Authorize via OAuth first.")

    # Reuse the importer's credential builder + Admin API client so the wizard
    # and the daily import share one auth path. Lazy import — the google client
    # libs are heavy and only this step needs them.
    try:
        from import_scripts.google_analytics.lib.ga_config import load_ga4_oauth_credentials
        from google.analytics.admin import AnalyticsAdminServiceClient
        from google.api_core.exceptions import GoogleAPIError
    except ImportError as e:  # pragma: no cover - import-env specific
        logging.exception("connector_wizard: ga4 client libraries unavailable")
        raise WizardError("Google Analytics support is not available on this server. Contact support.") from e

    try:
        credentials = load_ga4_oauth_credentials(source)
    except ValueError as e:
        # the ValueError can come from decoding the stored OAuth
        # client, so its text is not ours to show. Log it; return a fixed message.
        logging.warning("connector_wizard: ga4 credentials for account_esid=%r failed: %s", account_esid, e)
        raise WizardError("Could not load the Google credentials for this account. Reconnect Google and try again.") from e

    try:
        admin = AnalyticsAdminServiceClient(credentials=credentials)
        summaries = admin.list_account_summaries()
    except GoogleAPIError as e:
        logging.warning("connector_wizard: ga4 list_account_summaries error: %s", e)
        raise WizardError("Google Analytics API rejected the request. Re-authorize and try again.") from e
    except Exception as e:  # noqa: BLE001 — operator-facing wizard boundary
        logging.warning("connector_wizard: ga4 list_account_summaries failed: %s", e)
        raise WizardError("Could not list properties for this account.") from e

    properties: list[dict[str, str]] = []
    for summary in summaries:
        for prop in getattr(summary, "property_summaries", None) or []:
            # property resource name is "properties/123456789"
            resource = getattr(prop, "property", "") or ""
            pid = resource.split("/")[-1]
            if not pid:
                continue
            title = getattr(prop, "display_name", "") or pid
            properties.append({"id": pid, "title": title})
    properties.sort(key=lambda p: (p["title"] or "").lower())
    return properties


def configure_ga4_oauth(
    team_esid: str,
    account_esid: str,
    properties: list[dict[str, Any]],
    username: str = "",
) -> dict[str, Any]:
    """Step 4 (OAuth path): ensure de_asset_ga_properties rows exist for each
    chosen property (linked to the team), flag the team
    ``ds_google_analytics=true`` and append the property ESIDs, then — when a
    username is supplied and the account holds a token — write a real-principal
    ``de_connection`` (provider=ga4) for the Manage surface.

    Mirrors :func:`configure_youtube`. ``properties`` items are ``{"id", "title"}``
    from :func:`ga4_list_properties`. A property whose ``ga_property_id`` already
    exists is reused (idempotent re-run); otherwise a new row is created.

    Returns ``{"property_esids", "created_count", "connection_esid"}``.
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if not account_esid:
        raise WizardError("account_esid is required")
    if not properties:
        raise WizardError("At least one property is required")

    existing = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing:
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")
    team_ws = (existing.get("workspace_esid") or "").strip() or None

    # the property ids arrive in the request body. Accept only ids
    # the authorized Google account can list, so a caller cannot name another
    # tenant's property and have it imported under this team.
    listable = {p.get("id") for p in ga4_list_properties(account_esid)}
    unlisted = sorted({str(p.get("id") or "").strip() for p in properties} - listable - {""})
    if unlisted:
        logging.warning(
            "connector_wizard:  refused unlisted GA4 propert(ies) %s for account %s", unlisted, account_esid
        )
        raise WizardError(
            "These Google Analytics properties are not available to the authorized Google account: "
            + ", ".join(unlisted)
            + "."
        )

    property_esids: list[str] = []
    created = 0
    for prop in properties:
        pid = str(prop.get("id") or "").strip()
        if not pid:
            raise WizardError("Each property needs an id")
        # Existing rows for this property id so a re-run links the same row.
        # scoped to the team's workspace; a row owned elsewhere refuses.
        reusable = _ga_property_rows_for_team(pid, team_esid, team_ws)
        if reusable:
            property_esids.append(reusable[0])
            continue
        title = prop.get("title") or pid
        new_property = {
            "ga_property_title": title,
            "ga_property_id": pid,
            "ga_property_display_name": title,
        }
        property_esids.append(_create_asset("ga_properties", new_property))
        created += 1

    # stamp the team's workspace_esid onto the GA4 account + property
    # docs. The account is created at OAuth-callback time (fb_oauth_server) and
    # the property rows by _create_asset above, both before a team (and thus a
    # workspace) is resolved, so neither carried a workspace_esid. The picker +
    # Asset Explorer narrow fail-closed, so every wizard-created GA4
    # asset was invisible to workspace-scoped operators. Mirrors the
    # YouTube stamp. Fail-soft: the property/team link below already landed; a
    # stamping failure must not fail the wizard.
    # stamp only workspace-less docs; never re-point an owned one.
    if team_ws:
        try:
            account_doc = AssetService.get_by_id(GA4_ACCOUNTS_INDEX, account_esid)
            if account_doc is not None and not str(account_doc.get("workspace_esid") or "").strip():
                AssetService.update(
                    GA4_ACCOUNTS_INDEX,
                    account_esid,
                    {**account_doc, "workspace_esid": team_ws},
                )
            for prop_esid in property_esids:
                prop_doc = AssetService.get_by_id(GA_PROPERTIES_INDEX, prop_esid)
                if prop_doc is not None and not str(prop_doc.get("workspace_esid") or "").strip():
                    AssetService.update(
                        GA_PROPERTIES_INDEX,
                        prop_esid,
                        {**prop_doc, "workspace_esid": team_ws},
                    )
        except Exception as e:  # noqa: BLE001 — fail-soft: property/team link already persisted
            logging.warning(
                "connector_wizard: GA4 workspace_esid stamp failed team=%s account=%s ws=%s err=%s",
                team_esid,
                account_esid,
                team_ws,
                e,
            )

    current = existing.get("team_ga4_property_esids") or []
    if isinstance(current, str):
        current = [current] if current else []
    elif not isinstance(current, list):
        current = []
    for esid in property_esids:
        if esid not in current:
            current.append(esid)

    updated = dict(existing)
    updated["ds_google_analytics"] = "true"
    updated["team_ga4_property_esids"] = current

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")

    logging.info(
        "WizardService.configure_ga4_oauth: team=%s account=%s properties=%s created=%d",
        team_esid,
        account_esid,
        property_esids,
        created,
    )

    # write the de_connection so the connector card + Manage page
    # resolve.
    #
    # like the YouTube path, this write is part of the wizard's
    # success contract, NOT fail-soft. GA4 shares YouTube's architecture (token
    # on the de_asset_ga4_accounts doc first, de_connection written here at
    # configure time) and the same card downgrade
    # (connector_status_service._connector_status covers "google_analytics"),
    # so it had the same latent defect: a missing username or upsert failure
    # left connection_esid=None while the wizard reported success, and the card
    # then read "disconnected". We require a username, and — when the account
    # carries a token — require the connection write to succeed. A None return
    # with no token yet stays soft (not-yet-authorized).
    if not username:
        raise WizardError("A signed-in user is required to configure this connector.")

    account_source = AssetService.get_by_id(GA4_ACCOUNTS_INDEX, account_esid)
    connection_esid = _write_ga4_connection(
        username=username,
        team_esid=team_esid,
        account_esid=account_esid,
        account_source=account_source,
        properties=properties,
        property_esids=property_esids,
    )
    if connection_esid is None and (account_source or {}).get("ga4_account_token"):
        raise WizardError(
            "The Google Analytics account is authorized but its connection could not be saved. " "Please try again."
        )

    return {
        "property_esids": property_esids,
        "created_count": created,
        "connection_esid": connection_esid,
    }


# ── YouTube wizard ─────────────────────────────────────────────────
#
# Flow: youtube_account (OAuth) → list channels (Data API v3) → link channels
# to the team. Structurally closest to the FB app_user step, but YouTube has
# no app/app-user chain — one account doc carries the OAuth slug + state, and
# the fb_oauth_server callback writes the encrypted refresh token straight onto
# it (youtube_account_token). Channels are then enumerated from the account's
# token via channels.list(mine=True) and persisted as de_asset_youtube_channels
# rows linked back to the account via youtube_account_esid (so the existing
# get_youtube_analytics importer picks them up on its next run).
#
# Superuser-gated at the route layer (Google OAuth verification)
# lands — see routes.index() wireable_connector_ids.

YOUTUBE_ACCOUNTS_INDEX = "de_asset_youtube_accounts"
YOUTUBE_CHANNELS_INDEX = "de_asset_youtube_channels"

# GA4 OAuth indices. ga4_accounts is the OAuth-grant carrier
# (mirrors youtube_accounts); ga_properties is the existing child asset.
GA4_ACCOUNTS_INDEX = "de_asset_ga4_accounts"
GA_PROPERTIES_INDEX = "de_asset_ga_properties"


def youtube_oauth_pick_or_create_account(
    account_esid: str | None,
    new_account: dict[str, Any] | None,
    username: str = "",
    allowed_workspaces: list[str] | tuple[str, ...] | None = (),
) -> dict[str, Any]:
    """Step 1: pick an existing youtube_account or create one, then return
    its OAuth status.

    Creating an account routes through ``_create_asset`` so the
    ``auto_generate`` config stamps ``youtube_account_auth_url`` (15-char,
    unique) + ``youtube_account_auth_state`` (25-char). The frontend builds
    the popup URL from ``auth_url_path`` the same way the FB wizard does:
    ``https://{fboauth_hostname}/youtube/{auth_url_path}``.

    Returns ``{"account_esid", "auth_url_path", "has_token",
    "account_title"}`` — mirrors ``fb_pick_or_create_app_user``'s return
    shape so the route + JS can share the polling pattern.
    """
    if bool(account_esid) == bool(new_account):
        raise WizardError("Provide exactly one of account_esid or new_account")
    if not account_esid:
        # validate the Google account email format before creating the
        # account doc, so a malformed login_hint never gets persisted. (The
        # OAuth round-trip is still the real gate on whether the account is
        # usable; this just catches obvious typos client- and server-side.)
        email = str((new_account or {}).get("youtube_account_email") or "").strip()
        try:
            from web.services.connector_validation_service import is_valid_email_format
        except ImportError:
            from .connector_validation_service import is_valid_email_format
        if email and not is_valid_email_format(email):
            raise WizardError("Enter a valid Google account email address (e.g. name@example.com).")
        # reuse an existing account doc for this Google email instead
        # of minting a duplicate on every "Add account" / failed re-auth attempt.
        # but only a TOKENLESS placeholder: the same Gmail can front
        # several Brand Account identities, each needing its own doc + token
        # slot, so a tokened doc must never be reused (and its token later
        # overwritten) just because the email matches.
        account_esid = _reuse_or_create_oauth_account(
            "youtube_accounts",
            dict(new_account),
            "youtube_account_email",
            key_is_email=True,
            created_by=username,
            reuse_only_tokenless_field="youtube_account_token",
            allowed_workspaces=allowed_workspaces,
        )
    return _read_youtube_account_status(account_esid)


def youtube_account_status(account_esid: str) -> dict[str, Any]:
    """Step 2 polling: re-read a youtube_account to check whether the OAuth
    callback has populated the refresh token. Same return shape as
    ``youtube_oauth_pick_or_create_account``.
    """
    return _read_youtube_account_status(account_esid)


def _read_youtube_account_status(account_esid: str) -> dict[str, Any]:
    if not account_esid:
        raise WizardError("account_esid is required")
    source = AssetService.get_by_id(YOUTUBE_ACCOUNTS_INDEX, account_esid)
    if not source:
        # don't echo account_esid into the client-visible error.
        logging.warning("connector_wizard: youtube_account not found account_esid=%r", account_esid)
        raise WizardError("YouTube account not found")
    # post-callback identity dedup: when the OAuth callback found
    # that the authorized identity already lives on another account doc, it
    # saved the token THERE and stamped this placeholder with a redirect
    # pointer. Follow it (one hop) so the wizard adopts the canonical doc —
    # the poll response's account_esid is what the frontend continues with.
    redirect_esid = str(source.get("youtube_account_redirect_esid") or "").strip()
    if redirect_esid and redirect_esid != account_esid:
        canonical = AssetService.get_by_id(YOUTUBE_ACCOUNTS_INDEX, redirect_esid)
        if canonical:
            logging.info(
                "youtube oauth-status following identity-dedup redirect %s -> %s",
                account_esid,
                redirect_esid,
            )
            account_esid, source = redirect_esid, canonical
    return {
        "account_esid": account_esid,
        "auth_url_path": source.get("youtube_account_auth_url") or "",
        "account_title": source.get("youtube_account_title") or "",
        "has_token": bool(source.get("youtube_account_token")),
    }


def youtube_list_channels(account_esid: str) -> list[dict[str, str]]:
    """Step 3: list the channels the authorized Google account owns, via the
    YouTube Data API v3 (``channels.list(mine=True)``).

    Builds OAuth2 credentials from the account's stored refresh token using
    the same importer helper that the daily import uses
    (``get_youtube_analytics.load_account_credentials``), so the wizard and
    the importer authorize identically. Returns
    ``[{"id", "title", "handle"}]`` sorted by title.
    """
    if not account_esid:
        raise WizardError("account_esid is required")
    source = AssetService.get_by_id(YOUTUBE_ACCOUNTS_INDEX, account_esid)
    if not source:
        logging.warning("connector_wizard: youtube_account not found account_esid=%r", account_esid)
        raise WizardError("YouTube account not found")
    if not source.get("youtube_account_token"):
        raise WizardError("This account has no access token yet. Authorize via OAuth first.")

    # Reuse the importer's credential builder + Data API service so the wizard
    # and the daily import share one auth path. Lazy import — the google
    # client libs are heavy and only this step needs them.
    try:
        from import_scripts.youtube.get_youtube_analytics import (
            load_account_credentials,
            get_youtube_data_service,
        )
        from googleapiclient.errors import HttpError
    except ImportError as e:  # pragma: no cover - import-env specific
        logging.exception("connector_wizard: youtube client libraries unavailable")
        raise WizardError("YouTube support is not available on this server. Contact support.") from e

    try:
        credentials = load_account_credentials(source, account_esid=account_esid)
    except ValueError as e:
        # load_account_credentials raises ValueError when the token is absent;
        # we already guarded above. any other ValueError (decoding
        # the stored OAuth client) is not ours to show: log it, return fixed text.
        logging.warning("connector_wizard: youtube credentials for account_esid=%r failed: %s", account_esid, e)
        raise WizardError("Could not load the Google credentials for this account. Reconnect Google and try again.") from e

    try:
        youtube_data = get_youtube_data_service(credentials)
        # mine=True returns channels owned by the authorized account. snippet
        # carries title + customUrl (the @handle); id is the UCxxxx channel id.
        resp = youtube_data.channels().list(part="snippet", mine=True, maxResults=50).execute()
    except HttpError as e:
        # surface a clean message, not the raw Google error body.
        logging.warning("connector_wizard: youtube channels.list HttpError: %s", e)
        raise WizardError("YouTube API rejected the channel list request. Re-authorize and try again.") from e
    except Exception as e:  # noqa: BLE001 — operator-facing wizard boundary
        logging.warning("connector_wizard: youtube channels.list failed: %s", e)
        raise WizardError("Could not list channels for this account.") from e

    channels: list[dict[str, str]] = []
    for item in resp.get("items") or []:
        cid = item.get("id")
        if not cid:
            continue
        snippet = item.get("snippet") or {}
        channels.append(
            {
                "id": str(cid),
                "title": snippet.get("title") or str(cid),
                # customUrl is the @handle when the channel has one (may be absent).
                "handle": snippet.get("customUrl") or "",
            }
        )
    channels.sort(key=lambda c: (c["title"] or "").lower())
    return channels


def configure_youtube(
    team_esid: str,
    account_esid: str,
    channels: list[dict[str, Any]],
    username: str = "",
) -> dict[str, Any]:
    """Step 4: ensure de_asset_youtube_channels rows exist for each chosen
    channel (linked to the account), then flag the team ``ds_youtube=true``
    and append the channel ESIDs to ``team_youtube_channel_handle_esids``.

    ``channels`` items are ``{"id", "title", "handle"}`` from
    :func:`youtube_list_channels`. A channel whose ``youtube_channel_id``
    already exists for this account is reused (idempotent re-run); a row for
    it on another account is moved here when :class:`_YoutubeChannelAdopter`
    allows it; otherwise a new row is created. Append-style team
    link (preserves channels already linked), matching the GA4 wizard's posture — a team can hold multiple
    channels and a re-run shouldn't unlink the others.

    when ``username`` is supplied and the account already holds an
    OAuth token, also write a real-principal ``de_connection``
    (provider=youtube) + a ``de_connection_asset`` per channel and stamp
    ``connection_esid`` on the team doc. This is what lets the YouTube
    connector card resolve a connection (mirroring the Meta card) and the
    ``/connections/<esid>`` Manage page show this user's YouTube connection.
    The write is fail-soft: any failure leaves the channel/team link intact
    (the importer reads team-linked channels, not the connection), so the
    existing flow never regresses. See :func:`_write_youtube_connection`.

    Returns ``{"channel_esids", "created_count", "moved_count", "connection_esid"}``
    (``connection_esid`` is ``None`` when no connection was written).
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if not account_esid:
        raise WizardError("account_esid is required")
    if not channels:
        raise WizardError("At least one channel is required")

    # Existing channels for this account, keyed by youtube_channel_id, so a
    # re-run links the same row instead of duplicating it. rows for
    # the same channel on OTHER accounts are collected too: reconnecting under a
    # new Google login moves one of those here instead of minting a copy.
    existing_by_cid: dict[str, str] = {}
    elsewhere_by_cid: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for doc in AssetService.get_all(YOUTUBE_CHANNELS_INDEX) or []:
        src = doc.get("_source") or {}
        cid = src.get("youtube_channel_id")
        if not cid:
            continue
        if src.get("youtube_account_esid") == account_esid:
            existing_by_cid[str(cid)] = doc.get("_id") or ""
        elif doc.get("_id"):
            elsewhere_by_cid.setdefault(str(cid), []).append((doc["_id"], src))

    adopter = _YoutubeChannelAdopter(team_esid=team_esid, account_esid=account_esid)
    channel_esids: list[str] = []
    created = 0
    moved = 0
    for ch in channels:
        cid = str(ch.get("id") or "").strip()
        if not cid:
            raise WizardError("Each channel needs an id")
        if cid in existing_by_cid and existing_by_cid[cid]:
            channel_esids.append(existing_by_cid[cid])
            continue
        moved_esid = adopter.adopt(cid, elsewhere_by_cid.get(cid) or [])
        if moved_esid:
            channel_esids.append(moved_esid)
            moved += 1
            continue
        title = ch.get("title") or cid
        handle = ch.get("handle") or ""
        new_channel = {
            "youtube_channel_title": title,
            "youtube_channel_id": cid,
            # youtube_channel_handle is the title_field; fall back to the
            # title when the channel has no @handle so the picker label is
            # never blank.
            "youtube_channel_handle": handle or title,
            # Inject the parent account under the select-field name so
            # FormService routes it through store_esid (writes both
            # youtube_account_esid and youtube_account_title). Same
            # convention as fb_pick_or_create_app.
            "youtube_account_title": account_esid,
        }
        channel_esids.append(_create_asset("youtube_channels", new_channel))
        created += 1

    existing = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not existing:
        logging.warning("connector_wizard: team not found team_esid=%r", team_esid)
        raise WizardError("Team not found")

    # stamp the team's workspace_esid onto the account + channels.
    # Step 1 creates the account before a team (and thus workspace) is known,
    # so neither the account nor the channels carried a workspace_esid. The
    # picker + Asset Explorer narrow fail-closed (a doc with no
    # workspace_esid is DROPPED for a workspace-scoped operator), so every
    # wizard-created YouTube asset was invisible after creation. Stamp it here,
    # where the team — and its workspace — is finally resolved. Fail-soft: the
    # channel/team link above already landed; a stamping failure must not fail
    # the wizard.
    team_ws = (existing.get("workspace_esid") or "").strip() or None
    if team_ws:
        try:
            AssetService.update(
                YOUTUBE_ACCOUNTS_INDEX,
                account_esid,
                {**(AssetService.get_by_id(YOUTUBE_ACCOUNTS_INDEX, account_esid) or {}), "workspace_esid": team_ws},
            )
            for ch_esid in channel_esids:
                ch_doc = AssetService.get_by_id(YOUTUBE_CHANNELS_INDEX, ch_esid)
                if ch_doc is not None:
                    AssetService.update(YOUTUBE_CHANNELS_INDEX, ch_esid, {**ch_doc, "workspace_esid": team_ws})
        except Exception as e:  # noqa: BLE001 — fail-soft: team link already persisted
            logging.warning(
                "connector_wizard: workspace_esid stamp failed team=%s account=%s ws=%s err=%s",
                team_esid,
                account_esid,
                team_ws,
                e,
            )

    current = existing.get("team_youtube_channel_handle_esids") or []
    if isinstance(current, str):
        current = [current] if current else []
    elif not isinstance(current, list):
        current = []
    # prune ESIDs that no longer resolve before appending, so a
    # re-connect also clears a dangling reference instead of accumulating it.
    current, pruned = _prune_dangling_esids(current, "de_asset_youtube_channels")
    for esid in channel_esids:
        if esid not in current:
            current.append(esid)

    updated = dict(existing)
    updated["ds_youtube"] = "true"
    updated["team_youtube_channel_handle_esids"] = current

    if not AssetService.update(TEAMS_INDEX, team_esid, updated):
        raise WizardError("Could not update team")

    _audit_pruned_references(pruned, "de_asset_youtube_channels", "team_youtube_channel_handle_esids", team_esid, existing)

    logging.info(
        "WizardService.configure_youtube: team=%s account=%s channels=%s created=%d moved=%d",
        team_esid,
        account_esid,
        channel_esids,
        created,
        moved,
    )

    # write the de_connection so the connector card + Manage page
    # resolve.
    #
    # this write is part of the wizard's success contract, NOT a
    # fail-soft afterthought. The connector-status card downgrades a YouTube
    # connector to "disconnected" whenever no live de_connection resolves
    # (connector_status_service._connector_status), so an account that got a
    # token on its de_asset_youtube_accounts doc but never a de_connection
    # reads "configured" to the user who ran the wizard yet "disconnected" on
    # the card. The prior fail-soft envelope swallowed exactly that: a missing
    # username, or an upsert failure, left connection_esid=None while the
    # wizard still reported success (prod example: one page-filtered team, 2026-07-07).
    #
    # We require a username, and — when the account carries a token — require
    # the connection write to succeed. A None return when there is genuinely
    # no token yet stays soft (a legitimate not-yet-authorized state); the
    # account_source truthiness check below distinguishes the two.
    if not username:
        raise WizardError("A signed-in user is required to configure this connector.")

    account_source = AssetService.get_by_id(YOUTUBE_ACCOUNTS_INDEX, account_esid)
    connection_esid = _write_youtube_connection(
        username=username,
        team_esid=team_esid,
        account_esid=account_esid,
        account_source=account_source,
        channels=channels,
        channel_esids=channel_esids,
    )
    # A token on the account doc but no connection means the write silently
    # produced nothing (missing account doc) — treat as failure, not success.
    if connection_esid is None and (account_source or {}).get("youtube_account_token"):
        raise WizardError("The YouTube account is authorized but its connection could not be saved. " "Please try again.")

    return {
        "channel_esids": channel_esids,
        "created_count": created,
        "moved_count": moved,
        "connection_esid": connection_esid,
    }


def _as_esid_set(value: Any) -> set[str]:
    """A team ESID-list field (list, legacy scalar string, or absent) as a set."""
    if isinstance(value, list):
        return {str(v) for v in value if v}
    return {str(value)} if value else set()


class _YoutubeChannelAdopter:
    """move an existing channel record onto the account being
    configured instead of creating a duplicate.

    Reconnecting a channel under a different Google login (the path the
    error page sends users down) used to create a second
    ``de_asset_youtube_channels`` row. The old row stayed on an account whose
    token can no longer read the channel, so every team still linked to it got
    "analytics forbidden" on each run and spent a plan slot on the copy. Moving
    the row keeps its ESID, so every team link stays valid.

    A row is moved only when all of these hold; otherwise the caller creates a
    new row, as before:

    * the account's own ``channels.list(mine=True)`` returns the channel. The
      channel list reaches ``configure_youtube`` from the browser, so a forged
      id must never pull another team's row onto this account. The Google call
      is made once, and only when a move is on the table.
    * every team linked to the row shares an org with the target team (the
      cross-org case keeps its own row, per the ticket). A row no team links
      moves only when it carries no workspace or the target team's workspace.
    * the target team resolves and has an org. Ambiguous means decline.
    """

    def __init__(self, *, team_esid: str, account_esid: str) -> None:
        self.team_esid = team_esid
        self.account_esid = account_esid
        self._owned_ids: set[str] | None = None
        self._teams: list[dict[str, Any]] | None = None

    def adopt(self, cid: str, candidates: list[tuple[str, dict[str, Any]]]) -> str | None:
        """Move one candidate row for ``cid`` onto the account; return its ESID,
        or ``None`` when nothing may be moved."""
        if not candidates or cid not in self._account_owned_ids():
            return None
        teams = self._all_teams()
        target = next((t for t in teams if t.get("_id") == self.team_esid), None)
        target_src = (target or {}).get("_source") or {}
        target_orgs = _as_esid_set(target_src.get("organization_esid"))
        if not target_orgs:
            logging.warning("team %s has no org; not moving channel %s", self.team_esid, cid)
            return None
        target_ws = str(target_src.get("workspace_esid") or "").strip()

        eligible: list[tuple[bool, str]] = []
        for esid, src in candidates:
            linked = [t for t in teams if self._links(t, esid)]
            if linked:
                if any(not (_as_esid_set((t.get("_source") or {}).get("organization_esid")) & target_orgs) for t in linked):
                    continue
            elif str(src.get("workspace_esid") or "").strip() not in ("", target_ws):
                continue
            eligible.append((not any(t.get("_id") == self.team_esid for t in linked), esid))
        if not eligible:
            logging.info("channel %s has rows on other accounts, none movable; creating a new row", cid)
            return None

        # Prefer the row the target team already links, then a stable order.
        esid = sorted(eligible)[0][1]
        doc = AssetService.get_by_id(YOUTUBE_CHANNELS_INDEX, esid)
        if doc is None:
            return None
        account = AssetService.get_by_id(YOUTUBE_ACCOUNTS_INDEX, self.account_esid) or {}
        previous = doc.get("youtube_account_esid")
        updated = {
            **doc,
            "youtube_account_esid": self.account_esid,
            "youtube_account_title": account.get("youtube_account_title") or "",
        }
        if not AssetService.update(YOUTUBE_CHANNELS_INDEX, esid, updated):
            raise WizardError("Could not update the existing channel record")
        logging.info(
            "moved channel record %s (channel %s) from account %s to %s",
            esid,
            cid,
            previous,
            self.account_esid,
        )
        return esid

    def _account_owned_ids(self) -> set[str]:
        if self._owned_ids is None:
            try:
                self._owned_ids = {c["id"] for c in youtube_list_channels(self.account_esid)}
            except WizardError as e:
                # No proof of ownership, so no move; the caller creates a row as
                # it did.
                logging.warning("could not list channels for account %s: %s", self.account_esid, e)
                self._owned_ids = set()
        return self._owned_ids

    def _all_teams(self) -> list[dict[str, Any]]:
        if self._teams is None:
            self._teams = list(AssetService.get_all(TEAMS_INDEX) or [])
        return self._teams

    @staticmethod
    def _links(team: dict[str, Any], channel_esid: str) -> bool:
        src = team.get("_source") or {}
        return channel_esid in _as_esid_set(src.get("team_youtube_channel_handle_esids")) or (
            src.get("youtube_channel_esid") == channel_esid
        )


def _write_youtube_connection(
    *,
    username: str,
    team_esid: str,
    account_esid: str,
    account_source: dict[str, Any] | None,
    channels: list[dict[str, Any]],
    channel_esids: list[str],
) -> str | None:
    """mirror the chosen YouTube account into a real-principal
    ``de_connection`` + one ``de_connection_asset`` per channel, then stamp
    ``youtube_connection_esid`` on the team doc so ``connector_status_service``
    resolves the YouTube card (previously the generic
    ``connection_esid``, which collided with the Meta importer's token
    pointer — that field is Meta-owned now).

    Natural key is ``(principal=username, provider=youtube,
    provider_account_id)`` where ``provider_account_id`` is the account
    email (falling back to title, then esid) — matching
    ``connection_backfill.backfill_youtube`` so the shapes line up. Note the
    backfill writes ``principal="legacy:<esid>"``; this real-principal row is
    a SEPARATE doc (different natural key) by design — backfilled rows stay
    superuser-only, a wizard re-run mints the user-visible one (see the
     plan).

    Returns the connection esid, or ``None`` when there's no token to mirror
    (account not yet authorized) or the account doc is missing. Raises on ES
    write failure so the caller's fail-soft envelope logs it.
    """
    if not account_source:
        logging.warning("youtube account %s not found; skipping connection write", account_esid)
        return None

    encrypted_token = account_source.get("youtube_account_token")
    if not encrypted_token:
        # Not yet authorized — nothing to mirror. The card stays in its
        # pre-connection state; a later authorized re-run writes it.
        logging.info("youtube account %s has no token yet; skipping connection write", account_esid)
        return None

    try:
        from lib.encryption import decrypt
    except ImportError:  # pragma: no cover - import boundary
        from ...lib.encryption import decrypt  # type: ignore[no-redef]

    try:
        from web.services.connection_service import (
            ASSET_TYPE_YOUTUBE_CHANNEL,
            PROVIDER_YOUTUBE,
            ConnectionService,
        )
    except ImportError:
        from .connection_service import (  # type: ignore[no-redef]
            ASSET_TYPE_YOUTUBE_CHANNEL,
            PROVIDER_YOUTUBE,
            ConnectionService,
        )

    plaintext_token = decrypt(encrypted_token)

    provider_account_id = str(
        account_source.get("youtube_account_email") or account_source.get("youtube_account_title") or account_esid
    )
    label = account_source.get("youtube_account_title") or ""

    conn_result = ConnectionService.upsert_connection(
        principal=username,
        provider=PROVIDER_YOUTUBE,
        provider_account_id=provider_account_id,
        oauth_token=plaintext_token,
        label=label,
        legacy_asset_index=YOUTUBE_ACCOUNTS_INDEX,
        legacy_asset_esid=account_esid,
    )
    if not conn_result.get("success"):
        raise WizardError(f"upsert_connection failed: {conn_result.get('error')}")
    connection_esid = conn_result["esid"]

    # One de_connection_asset per channel, keyed by the YouTube channel id
    # (provider_asset_id), matching the backfill's asset shape. ``channels``
    # and ``channel_esids`` are positionally aligned by construction in
    # configure_youtube (one esid appended per channel, in order).
    for ch, channel_esid in zip(channels, channel_esids):
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        ConnectionService.upsert_connection_asset(
            connection_esid=connection_esid,
            asset_type=ASSET_TYPE_YOUTUBE_CHANNEL,
            provider_asset_id=cid,
            label=ch.get("title") or ch.get("handle") or cid,
            legacy_asset_index=YOUTUBE_CHANNELS_INDEX,
            legacy_asset_esid=channel_esid or None,
        )

    # Stamp the PROVIDER-SPECIFIC youtube_connection_esid on the team doc —
    # the field connector_status_service._resolve_connection_esid_fallback
    # reads first for the YouTube card. this used to stamp the
    # generic ``connection_esid``, which the Meta account_wide importer reads
    # for its token — so a YouTube wizard run hijacked the team's Meta import
    # credentials. The generic field is Meta-owned now;
    # existing teams that predate this fix still resolve via the fallback's
    # legacy read. Use the ES native partial-update API (doc= merge), NOT
    # AssetService.update, which is a full-document es.index replace and
    # would wipe the rest of the team doc.
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is not None:
            es.update(
                index=TEAMS_INDEX,
                id=team_esid,
                doc={"youtube_connection_esid": connection_esid},
                refresh="wait_for",
            )
        else:
            logging.warning("no ES client; team-doc connection_esid stamp skipped for team=%s", team_esid)
    except Exception as e:  # noqa: BLE001 — connection + assets already written; stamp is the last step
        logging.warning("team-doc connection_esid stamp failed for team=%s: %s", team_esid, e)

    logging.info(
        "wrote youtube de_connection esid=%s principal=%s team=%s channels=%d",
        connection_esid,
        username,
        team_esid,
        len(channel_esids),
    )
    return connection_esid


def _write_ga4_connection(
    *,
    username: str,
    team_esid: str,
    account_esid: str,
    account_source: dict[str, Any] | None,
    properties: list[dict[str, Any]],
    property_esids: list[str],
) -> str | None:
    """mirror the chosen GA4 account into a real-principal
    ``de_connection`` (provider=ga4) + one ``de_connection_asset`` per
    property, then stamp ``connection_esid`` on the team doc so
    ``connector_status_service`` resolves the GA4 card. Direct analogue of
    :func:`_write_youtube_connection` — the GA4 account doc carries the
    encrypted OAuth refresh token (written by the fb_oauth_server /ga4/auth_callback
    leg) and ``de_asset_ga_properties`` is the child asset.

    Natural key is ``(principal=username, provider=ga4, provider_account_id)``
    where ``provider_account_id`` is the account email (falling back to title,
    then esid).

    ``properties`` items are ``{"id", "title"}`` where ``id`` is the GA4
    property ID; ``properties`` and ``property_esids`` are positionally aligned
    by the caller. Returns the connection esid, or ``None`` when there's no
    token to mirror (account not yet authorized) or the account doc is missing.
    Raises on ES write failure so the caller's fail-soft envelope logs it.
    """
    if not account_source:
        logging.warning("ga4 account %s not found; skipping connection write", account_esid)
        return None

    encrypted_token = account_source.get("ga4_account_token")
    if not encrypted_token:
        # Not yet authorized — nothing to mirror. The card stays in its
        # pre-connection state; a later authorized re-run writes it.
        logging.info("ga4 account %s has no token yet; skipping connection write", account_esid)
        return None

    try:
        from lib.encryption import decrypt
    except ImportError:  # pragma: no cover - import boundary
        from ...lib.encryption import decrypt  # type: ignore[no-redef]

    try:
        from web.services.connection_service import (
            ASSET_TYPE_GA4_PROPERTY,
            PROVIDER_GA4,
            ConnectionService,
        )
    except ImportError:
        from .connection_service import (  # type: ignore[no-redef]
            ASSET_TYPE_GA4_PROPERTY,
            PROVIDER_GA4,
            ConnectionService,
        )

    plaintext_token = decrypt(encrypted_token)

    provider_account_id = str(
        account_source.get("ga4_account_email") or account_source.get("ga4_account_title") or account_esid
    )
    label = account_source.get("ga4_account_title") or ""

    conn_result = ConnectionService.upsert_connection(
        principal=username,
        provider=PROVIDER_GA4,
        provider_account_id=provider_account_id,
        oauth_token=plaintext_token,
        label=label,
        legacy_asset_index=GA4_ACCOUNTS_INDEX,
        legacy_asset_esid=account_esid,
    )
    if not conn_result.get("success"):
        raise WizardError(f"upsert_connection failed: {conn_result.get('error')}")
    connection_esid = conn_result["esid"]

    # One de_connection_asset per property, keyed by the GA4 property id
    # (provider_asset_id). ``properties`` and ``property_esids`` are
    # positionally aligned by construction in the caller.
    for prop, property_esid in zip(properties, property_esids):
        pid = str(prop.get("id") or "").strip()
        if not pid:
            continue
        ConnectionService.upsert_connection_asset(
            connection_esid=connection_esid,
            asset_type=ASSET_TYPE_GA4_PROPERTY,
            provider_asset_id=pid,
            label=prop.get("title") or pid,
            legacy_asset_index=GA_PROPERTIES_INDEX,
            legacy_asset_esid=property_esid or None,
        )

    # Stamp the PROVIDER-SPECIFIC ga4_connection_esid on the team doc — the
    # field connector_status_service._resolve_connection_esid_fallback reads
    # first for the GA4 card. this used to stamp the generic
    # ``connection_esid``, which the Meta account_wide importer reads for its
    # token — so a GA4 wizard run hijacked the team's Meta import credentials.
    # The generic field is Meta-owned now; existing teams
    # that predate this fix still resolve via the fallback's legacy read.
    # Mirrors the YouTube stamp (ES partial update, not a full replace).
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is not None:
            es.update(
                index=TEAMS_INDEX,
                id=team_esid,
                doc={"ga4_connection_esid": connection_esid},
                refresh="wait_for",
            )
        else:
            logging.warning("no ES client; team-doc connection_esid stamp skipped for team=%s", team_esid)
    except Exception as e:  # noqa: BLE001 — connection + assets already written; stamp is the last step
        logging.warning("team-doc connection_esid stamp failed for team=%s: %s", team_esid, e)

    logging.info(
        "wrote ga4 de_connection esid=%s principal=%s team=%s properties=%d",
        connection_esid,
        username,
        team_esid,
        len(property_esids),
    )
    return connection_esid


# ── Facebook wizard ──────────────────────────────────────────────────────────
#
# The FB chain is: fb_account → fb_app → fb_app_user (OAuth) → fb_pages.
# Each step accepts either an existing ESID or a new-record dict. The frontend
# walks the user through the chain step-by-step, calling these helpers as it
# goes. Page assignment is the final step and links the team.


def fb_pick_or_create_account(
    account_esid: str | None,
    new_account: dict[str, Any] | None,
    allowed_workspaces: list[str] | tuple[str, ...] | None = (),
) -> str:
    """Step 1: pick an existing fb_account or create one. Returns the ESID."""
    if bool(account_esid) == bool(new_account):
        raise WizardError("Provide exactly one of account_esid or new_account")
    if account_esid:
        return account_esid
    # reuse an existing fb_account with the same fb_account_id rather
    # than minting a duplicate each time the wizard's "add new account" step runs.
    # (The FB OAuth carrier is the app_user step, not this one, so FB's orphan
    # risk is lower than YouTube/GA4 — but the dedup is still correct and cheap.)
    # reuse only a doc in the caller's workspaces.
    return _reuse_or_create_oauth_account(
        "facebook_accounts", dict(new_account or {}), "fb_account_id", allowed_workspaces=allowed_workspaces
    )


def fb_app_selection_mode(
    allowed_workspaces: list[str] | set[str] | None,
) -> dict[str, Any]:
    """Decide whether the FB wizard's App step should be shown or skipped.

    workspaces with no existing fb_apps of their own should skip
    the App step and use the environment's unified Meta app
    (``WIZARD_META_APP_ID``). Legacy workspaces that own at least one
    fb_app keep the App step's "pick existing / create new" picker so
    they can keep using their pre-unification setup.

    Returns one of two shapes:

    * ``{"mode": "skip", "fb_app_esid": "<esid>"}`` — frontend should
      pre-set ``state.appEsid`` and drop the App step from the stepper.
    * ``{"mode": "pick"}`` — frontend renders the App step as today.

    Degrades to ``"pick"`` (the safe legacy path) when:

    - the workspace owns one or more fb_apps (legacy posture), OR
    - ``WIZARD_META_APP_ID`` isn't configured or doesn't resolve to an
      existing fb_app doc (the unified app isn't available, so we can't
      auto-skip).

    ``allowed_workspaces`` mirrors the ``list_assets_for_picker``
    signature so this helper sees exactly what the picker would see —
    no chance of UX drift between "the picker says 'No existing FB
    apps'" and "the mode decision".
    """
    workspace_apps = list_assets_for_picker("facebook_apps", allowed_workspaces=allowed_workspaces)
    if workspace_apps:
        return {"mode": "pick"}

    try:
        from web.services.wizard_oauth_service import WizardOAuthService
    except ImportError:
        from .wizard_oauth_service import WizardOAuthService

    meta_app = WizardOAuthService.get_meta_app()
    if not meta_app or not meta_app.esid:
        return {"mode": "pick"}

    return {"mode": "skip", "fb_app_esid": meta_app.esid}


# ── Path A: de_connection-backed FB wizard ──────────────────────────
#
# Phase 1 (this section) — backend service functions for the migrated data-
# connectors FB wizard. The legacy fb_pick_or_create_app / fb_pick_or_create_app_user
# / fb_list_pages_via_graph / fb_link_pages_to_team chain stays in place so the
# wizard frontend can land Phase 2 (UI migration) atomically without breaking
# the live legacy path. Phase 3 removes the legacy chain.
#
# The three functions here mirror onboarding's de_connection write path:
#   1. fb_create_pending_connection — write a pending_token de_connection row
#      keyed by (principal=username, provider="facebook", provider_account_id=
#      "<wizard-pending>:<unique>"). The callback later replaces the placeholder
#      with the real Meta /me user_id via set_token_for_pending_connection.
#   2. fb_list_pages_for_connection — call /me/accounts using the decrypted
#      token from de_connection.oauth_token_enc.
# 3. fb_finish_data_connectors_wizard — invoke the existing
# / bridges keyed off connection_esid (not app_user_esid), then
#      kick the wizard-style import via run_daily_imports --connection_esid X.


def fb_create_pending_connection(
    username: str,
    workspace_esid: str | None = None,
) -> dict[str, Any]:
    """Path A Phase 1 — write a pending_token de_connection row.

    Called by the wizard's Authorize step BEFORE the OAuth popup opens, so
    the callback handler has a stable connection_esid to upsert the real
    token onto. The provider_account_id placeholder is replaced with the
    Meta /me user_id when the callback runs
    ConnectionService.set_token_for_pending_connection on the same row.

    Scope list comes from WizardOAuthService.default_scopes_for("meta_ads")
    — the same source-of-truth onboarding's step-5 uses. Hardcoding here
    would risk drift between Path A and onboarding when scopes change.

    Returns ``{"connection_esid": "...", "scopes": [...]}``.
    """
    if not username:
        raise WizardError("username is required")

    try:
        from web.services.connection_service import (
            ConnectionService,
            PROVIDER_FACEBOOK,
        )
        from web.services.wizard_oauth_service import WizardOAuthService
    except ImportError:
        from .connection_service import (  # type: ignore[no-redef]
            ConnectionService,
            PROVIDER_FACEBOOK,
        )
        from .wizard_oauth_service import WizardOAuthService  # type: ignore[no-redef]

    scopes = list(WizardOAuthService.default_scopes_for("meta_ads"))

    # fix (2026-06-05): re-use the user's OPEN pending placeholder
    # row instead of minting a new one on every Authorize click. Each click
    # used to mint a fresh ``wizard-pending-<random>`` provider_account_id,
    # which (being a unique natural key) created a brand-new row — so an
    # abandoned authorize left an orphan pending_token row behind every time,
    # and a completed re-run produced duplicate active rows. That accumulation
    # is what showed up as "7 connections" on the Current Plan page
    #. Reusing the open placeholder keeps it to at most one
    # pending row per (principal, provider): the callback still updates the
    # SAME esid in place via set_token_for_pending_connection, so the wizard
    # frontend's "waiting for OAuth..." poll loop is unaffected.
    existing = ConnectionService.find_reusable_pending_connection(username, PROVIDER_FACEBOOK)
    if existing:
        connection_esid = existing.get("_id") or ""
        if connection_esid:
            logging.info(
                "WizardService.fb_create_pending_connection: reusing open pending " "row username=%s connection_esid=%s",
                username,
                connection_esid,
            )
            return {"connection_esid": connection_esid, "scopes": scopes}
        # Defensive: a hit with no _id is malformed — fall through to mint.

    # No reusable placeholder — mint a fresh one. The provider_account_id is
    # the load-bearing disambiguator for the (principal, provider,
    # provider_account_id) natural key; we don't know the real Meta /me user
    # id yet (it arrives at callback time), so the placeholder stands in until
    # set_token_for_pending_connection replaces it.
    import secrets

    try:
        from web.services.connection_service import PENDING_ACCOUNT_ID_PREFIX
    except ImportError:
        from .connection_service import PENDING_ACCOUNT_ID_PREFIX  # type: ignore[no-redef]

    placeholder_account_id = f"{PENDING_ACCOUNT_ID_PREFIX}{secrets.token_urlsafe(16)}"

    result = ConnectionService.create_pending_connection(
        principal=username,
        provider=PROVIDER_FACEBOOK,
        provider_account_id=placeholder_account_id,
        label=f"meta_ads — {username} (data-connectors wizard)",
        scopes=scopes,
        org_shared=False,
    )
    if not result.get("success"):
        raise WizardError(result.get("error") or "Could not create pending connection")

    connection_esid = result.get("esid") or ""
    if not connection_esid:
        raise WizardError("Pending connection write returned no esid")

    logging.info(
        "WizardService.fb_create_pending_connection: minted new pending row " "username=%s connection_esid=%s scopes=%s",
        username,
        connection_esid,
        scopes,
    )
    return {"connection_esid": connection_esid, "scopes": scopes}


def fb_connection_status(connection_esid: str) -> dict[str, Any]:
    """Path A Phase 1 — poll a de_connection for callback completion.

    Mirrors fb_app_user_status's shape so the wizard JS can keep its
    "poll-until-has-token" structure. Returns:

        {"connection_esid": "...",
         "status": "pending_token" | "active" | ...,
         "has_token": bool,
         "token_expires_at": "...?"}

    "active" means the callback successfully ran
    set_token_for_pending_connection — the wizard can advance to the
    Pages step.
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")

    try:
        from web.services.connection_service import STATUS_SUPERSEDED, ConnectionService
    except ImportError:
        from .connection_service import (  # type: ignore[no-redef]
            STATUS_SUPERSEDED,
            ConnectionService,
        )

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        # don't echo connection_esid into client-visible error.
        logging.warning(
            "connector_wizard: de_connection not found connection_esid=%r",
            connection_esid,
        )
        raise WizardError("Connection not found")

    # the OAuth completion merged this placeholder into an existing
    # connection for the same (principal, provider, account). Report the
    # CANONICAL row's state under the canonical esid; the frontend poll adopts
    # it so the remaining wizard steps (accounts/pages/configure) target the
    # one real connection instead of a duplicate. Safe to expose: the merge
    # only ever matches a row with the SAME principal as this placeholder.
    superseded_by = (doc.get("superseded_by") or "").strip()
    if (doc.get("status") or "") == STATUS_SUPERSEDED and superseded_by:
        canonical = ConnectionService.get_connection(superseded_by)
        if canonical:
            return {
                "connection_esid": superseded_by,
                "status": canonical.get("status") or "",
                "has_token": bool(canonical.get("oauth_token_enc")),
                "token_expires_at": canonical.get("token_expires_at"),
            }

    return {
        "connection_esid": connection_esid,
        "status": doc.get("status") or "",
        "has_token": bool(doc.get("oauth_token_enc")),
        "token_expires_at": doc.get("token_expires_at"),
    }


# ── TikTok wizard ─────────────────────────────────────────────────


def tiktok_oauth_start(username: str) -> dict[str, Any]:
    """begin a TikTok Login Kit grant for the session user.

    TikTok's account id (open_id) is unknown until the callback, so this mints a
    ``wizard-pending-*`` placeholder row (Meta's shape) — reusing the
    user's open one when there is one (one pending row per user, not one
    per click) — and seeds a fresh single-use OAuth state on it. The frontend
    opens ``https://{fboauth_hostname}/tiktok/{auth_url_path}`` and polls
    :func:`tiktok_connection_status`.

    SECURITY: ``username`` comes from the authenticated session (the route), never
    the request body. The fb_oauth-host callback binds the token to it.

    Returns ``{connection_esid, auth_url_path}``.
    """
    if not username:
        raise WizardError("Not authenticated")

    try:
        from web.services.connection_service import (
            PENDING_ACCOUNT_ID_PREFIX,
            PROVIDER_TIKTOK,
            ConnectionService,
        )
    except ImportError:
        from .connection_service import (  # type: ignore[no-redef]
            PENDING_ACCOUNT_ID_PREFIX,
            PROVIDER_TIKTOK,
            ConnectionService,
        )
    from lib.tiktok_constants import SCOPES as TIKTOK_SCOPES

    existing = ConnectionService.find_reusable_pending_connection(username, PROVIDER_TIKTOK)
    connection_esid = (existing or {}).get("_id") or ""
    if not connection_esid:
        import secrets

        pending = ConnectionService.create_pending_connection(
            principal=username,
            provider=PROVIDER_TIKTOK,
            provider_account_id=f"{PENDING_ACCOUNT_ID_PREFIX}{secrets.token_urlsafe(16)}",
            label=f"TikTok — {username} (data-connectors wizard)",
            scopes=list(TIKTOK_SCOPES),
        )
        if not pending.get("success"):
            raise WizardError(pending.get("error") or "Could not create connection")
        connection_esid = pending.get("esid") or ""
        if not connection_esid:
            raise WizardError("Pending connection write returned no esid")

    seeded = ConnectionService.seed_oauth_state(connection_esid)
    if not seeded.get("success"):
        raise WizardError(seeded.get("error") or "Could not start authorization")

    logging.info("WizardService.tiktok_oauth_start: username=%s connection_esid=%s", username, connection_esid)
    return {"connection_esid": connection_esid, "auth_url_path": seeded["auth_url"]}


def tiktok_connection_status(connection_esid: str) -> dict[str, Any]:
    """poll a TikTok connection for callback completion.

    Returns ``{connection_esid, status, has_token, label}``. When the callback
    merged this placeholder into the user's existing connection for the same
    TikTok account, reports the canonical row under its esid so the
    frontend adopts it — the same redirect :func:`fb_connection_status` does.
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")

    try:
        from web.services.connection_service import STATUS_SUPERSEDED, ConnectionService
    except ImportError:
        from .connection_service import STATUS_SUPERSEDED, ConnectionService  # type: ignore[no-redef]

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        raise WizardError("Connection not found")

    superseded_by = (doc.get("superseded_by") or "").strip()
    if (doc.get("status") or "") == STATUS_SUPERSEDED and superseded_by:
        canonical = ConnectionService.get_connection(superseded_by)
        if canonical:
            connection_esid, doc = superseded_by, canonical

    return {
        "connection_esid": connection_esid,
        "status": doc.get("status") or "",
        "has_token": bool(doc.get("oauth_token_enc")),
        "label": doc.get("label") or "",
    }


def configure_tiktok(team_esid: str, connection_esid: str) -> dict[str, Any]:
    """link an authorized TikTok connection to a team.

    Appends the connection to the team's ``team_tiktok_connection_esids`` (a
    team may hold several TikTok accounts; re-linking one is a no-op) and turns
    ``ds_tiktok`` on. The caller has already run the team and connection access
    gates; this re-checks only what the gates can't know — that the row is a
    TikTok connection that finished authorizing.

    Returns ``{connection_esid, label, open_id, linked}`` (``linked`` = the
    team's connection list after the write).
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if not connection_esid:
        raise WizardError("connection_esid is required")

    try:
        from web.services.connection_service import PROVIDER_TIKTOK, STATUS_ACTIVE, ConnectionService
    except ImportError:
        from .connection_service import PROVIDER_TIKTOK, STATUS_ACTIVE, ConnectionService  # type: ignore[no-redef]
    from lib.tiktok_constants import TEAM_CONNECTIONS_FIELD, TEAM_FLAG_FIELD

    conn = ConnectionService.get_connection(connection_esid)
    if not conn or conn.get("provider") != PROVIDER_TIKTOK:
        raise WizardError("Connection not found")
    if (conn.get("status") or "") != STATUS_ACTIVE or not conn.get("oauth_token_enc"):
        raise WizardError("This TikTok account hasn't finished authorizing. Authorize it again.")

    team = AssetService.get_by_id(TEAMS_INDEX, team_esid)
    if not team:
        raise WizardError("Team not found")
    current = team.get(TEAM_CONNECTIONS_FIELD) or []
    if isinstance(current, str):
        current = [current] if current else []
    linked = list(dict.fromkeys([str(c) for c in current if c] + [connection_esid]))

    _update_team_data_source(
        team_esid=team_esid,
        flag_field=TEAM_FLAG_FIELD,
        esid_field=TEAM_CONNECTIONS_FIELD,
        esid_value=linked,
    )
    try:
        from web.services import tiktok_account_projection
    except ImportError:
        from . import tiktok_account_projection  # type: ignore[no-redef]
    # The Explorer row is workspace-scoped; the linking team's workspace is it.
    tiktok_account_projection.sync(connection_esid, workspace_esid=str(team.get("workspace_esid") or "") or None)
    logging.info("WizardService.configure_tiktok: team=%s connection=%s linked=%d", team_esid, connection_esid, len(linked))
    return {
        "connection_esid": connection_esid,
        "label": conn.get("label") or "",
        "open_id": conn.get("provider_account_id") or "",
        "linked": linked,
    }


# Meta Graph error codes the wizard explains in its own words. Meta's
# raw error text is logged, never returned to the browser.
_FB_TOKEN_ERROR_CODES = frozenset({102, 190})
_FB_RATE_LIMIT_ERROR_CODES = frozenset({4, 17, 32, 613})


def _fb_graph_error_message(resp: Any, what: str) -> str:  # Any: requests.Response, imported lazily
    """Log a non-200 Graph response and return a fixed, user-readable message.

    Branches on Meta's error ``code``, never on its message text. ``what`` is
    our own noun ("pages", "ad accounts"), never request or API text.
    """
    try:
        err = (resp.json() or {}).get("error") or {}
    except ValueError:
        err = {}
    if not isinstance(err, dict):
        err = {}
    code = err.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    logging.warning(
        "connector_wizard: FB Graph list %s failed status=%s code=%s subcode=%s type=%s fbtrace_id=%s message=%r",
        what,
        resp.status_code,
        code,
        err.get("error_subcode"),
        err.get("type"),
        err.get("fbtrace_id"),
        str(err.get("message") or "")[:300],
    )
    if code in _FB_TOKEN_ERROR_CODES:
        return "Meta access token expired or was revoked. Reconnect Facebook and try again."
    if code == 10 or (isinstance(code, int) and 200 <= code <= 299):
        return (
            f"Meta refused to list {what}: this login is missing a permission. "
            "Reconnect Facebook and grant the requested permissions."
        )
    if code in _FB_RATE_LIMIT_ERROR_CODES:
        return "Meta is limiting requests right now. Wait a few minutes and try again."
    return f"Meta returned an error while listing {what} (HTTP {resp.status_code}). Reconnect Facebook and try again."


def _fb_graph_unreachable(e: Exception, what: str) -> WizardError:
    """a fixed message for a failed Graph request.

    Logs the exception CLASS only: a requests error's text carries the request
    URL, and the access token rides in its query string.
    """
    logging.warning("connector_wizard: FB Graph list %s request failed: %s", what, type(e).__name__)
    return WizardError("Could not reach Meta. Check the connection and try again in a moment.")


def _fb_graph_non_json(e: Exception, what: str) -> WizardError:
    """a fixed message for a 200 Graph response that is not JSON."""
    logging.warning("connector_wizard: FB Graph list %s returned non-JSON: %s", what, e)
    return WizardError(f"Meta returned an unreadable response while listing {what}. Try again in a moment.")


def fb_list_pages_for_connection(connection_esid: str) -> list[dict[str, str]]:
    """Path A Phase 1 — list pages for a de_connection-backed wizard.

    Pulls the decrypted token via ConnectionService.get_decrypted_token
    (the dedicated importer-facing path) rather than reaching into
    de_connection.oauth_token_enc + lib.encryption.decrypt directly, so
    the connection-token-read surface stays narrow.

    Same /me/accounts shape as the legacy fb_list_pages_via_graph; the
    return type matches so the route's dual-mode branch can hand either
    output to the frontend without further reshaping. Returns
    ``[{"id", "name", "username"}]`` sorted by name.
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService  # type: ignore[no-redef]

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        logging.warning(
            "connector_wizard: de_connection not found connection_esid=%r",
            connection_esid,
        )
        raise WizardError("Connection not found")
    if not doc.get("oauth_token_enc"):
        raise WizardError("This connection has no access token yet. Authorize via OAuth first.")

    access_token = ConnectionService.get_decrypted_token(connection_esid)
    if not access_token:
        raise WizardError("Could not decrypt access token for this connection.")

    import requests

    try:
        resp = requests.get(
            f"{FB_GRAPH_URL}/me/accounts",
            params={
                "access_token": access_token,
                "fields": "id,name,username",
                "limit": 200,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        raise _fb_graph_unreachable(e, "pages") from e

    if resp.status_code != 200:
        raise WizardError(_fb_graph_error_message(resp, "pages"))

    try:
        data = resp.json()
    except ValueError as e:
        raise _fb_graph_non_json(e, "pages") from e

    pages: list[dict[str, str]] = []
    for item in data.get("data") or []:
        pid = item.get("id")
        if not pid:
            continue
        pages.append(
            {
                "id": str(pid),
                "name": item.get("name") or "",
                "username": item.get("username") or "",
            }
        )
    pages.sort(key=lambda p: (p["name"] or "").lower())
    return pages


def fb_list_ad_accounts_for_connection(connection_esid: str) -> list[dict[str, str]]:
    """Path A Phase 1 addendum — list ad accounts the connection can see.

    Drives the Accounts step's pick-from-list UX (revised Phase 2 design,
    2026-05-28). Mirrors :func:`fb_list_pages_for_connection` but hits
    ``/me/adaccounts`` instead of ``/me/accounts``. Reads the token via
    ConnectionService.get_decrypted_token (narrow surface).

    Returns ``[{"id", "name", "account_status"}]`` sorted by name.
    ``id`` is the Meta ad-account id (``act_NNN``), which is what the
     bridge expects as ``accepted_ids``. ``account_status`` is
    Meta's integer status code (1=active, 2=disabled, 3=unsettled, etc.)
    surfaced to the UI so a disabled-account row can show a hint.
    """
    if not connection_esid:
        raise WizardError("connection_esid is required")

    try:
        from web.services.connection_service import ConnectionService
    except ImportError:
        from .connection_service import ConnectionService  # type: ignore[no-redef]

    doc = ConnectionService.get_connection(connection_esid)
    if not doc:
        logging.warning(
            "connector_wizard: de_connection not found connection_esid=%r",
            connection_esid,
        )
        raise WizardError("Connection not found")
    if not doc.get("oauth_token_enc"):
        raise WizardError("This connection has no access token yet. Authorize via OAuth first.")

    access_token = ConnectionService.get_decrypted_token(connection_esid)
    if not access_token:
        raise WizardError("Could not decrypt access token for this connection.")

    import requests

    try:
        resp = requests.get(
            f"{FB_GRAPH_URL}/me/adaccounts",
            params={
                "access_token": access_token,
                "fields": "id,name,account_status",
                "limit": 200,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        raise _fb_graph_unreachable(e, "ad accounts") from e

    if resp.status_code != 200:
        raise WizardError(_fb_graph_error_message(resp, "ad accounts"))

    try:
        data = resp.json()
    except ValueError as e:
        raise _fb_graph_non_json(e, "ad accounts") from e

    accounts: list[dict[str, str]] = []
    for item in data.get("data") or []:
        aid = item.get("id")
        if not aid:
            continue
        accounts.append(
            {
                "id": str(aid),
                "name": item.get("name") or "",
                "account_status": str(item.get("account_status") or ""),
            }
        )
    accounts.sort(key=lambda a: (a["name"] or "").lower())
    return accounts


OTHER_WORKSPACE_TEAM_LABEL = "a team in another workspace"


def annotate_ad_accounts_with_team_links(
    accounts: list[dict],
    team_esid: str,
) -> list[dict]:
    """stamp each listed ad account with team-link intent signals.

    Adds two keys to every account dict (mutating in place, also returned):

    * ``linked_to_team`` — this team holds an OPEN ``fb_ad_account_links``
      entry for the account. The wizard pre-checks exactly these, so a
      re-auth starts with what the team already uses instead of everything
      the authing login can see (the 2026-07-14 team A / team B mechanism —
      every visible account arrived pre-checked and a click-through claimed
      siblings' accounts).
    * ``used_by_teams`` — titles of OTHER teams holding an open link. Not a
      block (sharing an Ad Account is the expected Meta topology, rules
      6/7/10) — a label, so checking a shared account is an informed choice.

    Only OPEN links (no ``end_date``) count: ended links are history, not
    current use. The lookup is one nested query over ``de_asset_teams`` —
    ``fb_ad_account_links`` is mapped ``type: nested`` (asset_schema_teams),
    so a plain terms query would silently match nothing.

    Fail-soft: any ES failure logs and returns the accounts unannotated
    (``linked_to_team`` false, ``used_by_teams`` empty) — enrichment must
    never break the Accounts step's listing.
    """
    for a in accounts:
        a.setdefault("linked_to_team", False)
        a.setdefault("used_by_teams", [])
    if not accounts or not team_esid:
        return accounts

    def _bare(provider_id: str) -> str:
        pid = str(provider_id or "").strip()
        return pid[4:] if pid.startswith("act_") else pid

    bare_ids = [b for b in (_bare(a.get("id")) for a in accounts) if b]
    if not bare_ids:
        return accounts

    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return accounts
        resp = es.search(
            index=TEAMS_INDEX,
            body={
                "query": {
                    "nested": {
                        "path": "fb_ad_account_links",
                        "query": {
                            "bool": {
                                "filter": [{"terms": {"fb_ad_account_links.fb_ad_account_id": bare_ids}}],
                                "must_not": [{"exists": {"field": "fb_ad_account_links.end_date"}}],
                            }
                        },
                    }
                },
                "_source": ["team_title", "workspace_esid", "fb_ad_account_links"],
                "size": 1000,
            },
        )
        own_workspace = str((AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}).get("workspace_esid") or "").strip()
    except Exception as e:  # noqa: BLE001 — enrichment boundary; listing must survive
        logging.warning(
            "team-link annotation failed for team=%s (accounts render unannotated): %s",
            team_esid,
            e,
        )
        return accounts

    linked_here: set[str] = set()
    used_by: dict[str, list[str]] = {}
    for hit in (resp.get("hits", {}) or {}).get("hits", []):
        src = hit.get("_source") or {}
        title = str(src.get("team_title") or hit.get("_id") or "").strip()
        # a shared ad account can serve teams in other workspaces
        # (tenants); their names are not ours to show (same rule as
        # _chatwoot_inbox_claims).
        if not own_workspace or str(src.get("workspace_esid") or "").strip() != own_workspace:
            title = OTHER_WORKSPACE_TEAM_LABEL
        is_this_team = hit.get("_id") == team_esid
        for link in src.get("fb_ad_account_links") or []:
            if link.get("end_date"):
                continue
            bare = _bare(link.get("fb_ad_account_id"))
            if bare not in bare_ids:
                continue
            if is_this_team:
                linked_here.add(bare)
            elif title and title not in used_by.setdefault(bare, []):
                used_by[bare].append(title)

    for a in accounts:
        bare = _bare(a.get("id"))
        a["linked_to_team"] = bare in linked_here
        a["used_by_teams"] = sorted(used_by.get(bare) or [])
    return accounts


def _normalize_accounts_arg(
    accounts: list[dict[str, Any]] | None,
    fb_account_provider_id: str | None,
) -> list[dict[str, str]]:
    """coerce the Confirm-step account selection into a uniform
    ``[{"provider_id": "act_NNN", "name": "..."}]`` list.

    Two shapes are accepted so the dual-mode ``/configure`` endpoint can
    keep its prod-fallback posture (Risk Register R8):

    * Path A multi-account (current frontend) — ``accounts`` is a list of
      ``{"provider_id", "name"}`` dicts. ``name`` is optional; a blank
      name is dropped so the bridge falls back to ``act_<bare>``.
    * Legacy single-account — ``fb_account_provider_id`` is a bare
      ``act_NNN`` string with no name. Wrapped into a one-element list.

    Order is preserved: the FIRST entry becomes the team's legacy scalar
    ``fb_ad_account_esid`` (and so the ``is_active_import``-flagged row).
    Since Phase 2 / that is NOT a limit on what imports —
    every entry becomes a link and every link imports. Duplicate
    provider_ids are de-duplicated, keeping the first occurrence's name.
    """
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    if accounts:
        for entry in accounts:
            if not isinstance(entry, dict):
                continue
            pid = str(entry.get("provider_id") or "").strip()
            if not pid or pid in seen:
                continue
            seen.add(pid)
            normalized.append({"provider_id": pid, "name": str(entry.get("name") or "").strip()})
    elif fb_account_provider_id:
        pid = str(fb_account_provider_id).strip()
        if pid:
            normalized.append({"provider_id": pid, "name": ""})
    return normalized


def pages_linked_in_other_workspaces(page_ids: list[str], team_esid: str, workspace_esid: str | None) -> set[str]:
    """the subset of ``page_ids`` already linked to a team outside
    ``workspace_esid`` (a claiming team with no workspace counts as outside).
    A Facebook Page belongs to exactly ONE team (Meta data-model rule 1) and the
    importer's page->team map is last-writer-wins, so a second claim would
    steal the other tenant's data. Raises ``WizardError`` when the lookup fails
    so every caller declines rather than guesses.
    """
    ids = sorted({str(p).strip() for p in page_ids if str(p).strip()})
    if not ids:
        return set()
    from lib.es_helpers import get_elasticsearch_client

    try:
        es = get_elasticsearch_client()
        resp = es.search(
            index=TEAMS_INDEX,
            size=100,
            _source=["workspace_esid", "team_facebook_page_id"],
            query={
                "bool": {"filter": [{"terms": {"team_facebook_page_id": ids}}], "must_not": [{"ids": {"values": [team_esid]}}]}
            },
        )
    except Exception:  # noqa: BLE001 — an authz-relevant lookup must fail closed
        logging.exception("connector_wizard:  page-ownership lookup failed for team %s", team_esid)
        raise WizardError("Could not verify the selected Facebook Pages. Try again in a moment.")
    body = resp.body if hasattr(resp, "body") else resp
    claimed: set[str] = set()
    for hit in (body.get("hits") or {}).get("hits") or []:
        src = hit.get("_source") or {}
        other_ws = (src.get("workspace_esid") or "").strip()
        if not other_ws or other_ws != (workspace_esid or ""):
            linked = src.get("team_facebook_page_id") or []
            claimed |= {pid for pid in (linked if isinstance(linked, list) else [linked]) if pid in ids}
    return claimed


def _refuse_pages_linked_in_other_workspaces(page_ids: list[str], team_esid: str, workspace_esid: str | None) -> None:
    """refuse an explicit Page choice that another workspace's team
    already owns (see :func:`pages_linked_in_other_workspaces`). Linking within
    the same workspace keeps its existing behaviour."""
    claimed = pages_linked_in_other_workspaces(page_ids, team_esid, workspace_esid)
    if claimed:
        logging.warning(
            "connector_wizard:  refused page(s) %s for team %s: linked to a team in another workspace",
            sorted(claimed),
            team_esid,
        )
        raise WizardError(
            "Some selected Facebook Pages are already linked to a team in another workspace: "
            + ", ".join(sorted(claimed))
            + ". A Page can belong to only one team; unlink it there first."
        )


def _require_pages_listable_by_connection(connection_esid: str, page_ids: list[str]) -> None:
    """explicit Page selections arrive in the request body. Accept
    only Pages the connection's Facebook login can list (/me/accounts); that is
    necessary, not sufficient (Meta rule 5: access is not ownership), which is
    why ``_refuse_pages_linked_in_other_workspaces`` runs as well.
    """
    listable = {p.get("id") for p in fb_list_pages_for_connection(connection_esid)}
    unlisted = sorted({pid for pid in page_ids if pid not in listable})
    if unlisted:
        logging.warning("connector_wizard:  refused unlisted page(s) %s for connection %s", unlisted, connection_esid)
        raise WizardError(
            "These Facebook Pages are not available to the connected Facebook login, so they can't be linked: "
            + ", ".join(unlisted)
            + "."
        )


def fb_finish_data_connectors_wizard(
    *,
    username: str,
    team_esid: str,
    connection_esid: str,
    accounts: list[dict[str, Any]] | None = None,
    fb_account_provider_id: str | None = None,
    selected_page_ids: list[str],
    selected_pages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Path A — Confirm-step finisher for the migrated wizard.

    Runs the three onboarding bridges keyed off ``connection_esid``
    (not ``app_user_esid``), then kicks the wizard-style on-demand
    import via :func:`_kickoff_on_demand_backfill`. The bridges populate
    ``de_asset_fb_ad_accounts`` + ``de_asset_fb_pages`` +
    ``de_asset_ig_accounts`` and the team back-links; the importer
    kickoff writes the first ``de_import_progress`` doc and spawns the
    subprocess.

    the Accounts step is now multi-select with per-account
    friendly names. ``accounts`` is a list of
    ``{"provider_id": "act_NNN", "name": "..."}`` dicts. We pass every
    provider_id through to the bridges as ``accepted_ids`` (they already
    iterate the list) and the name map as ``friendly_names`` so the
     bridge titles each ``de_asset_fb_ad_accounts`` doc.

    **Multi-account import works.** ALL
    selected accounts are persisted as ``de_connection_asset`` rows (named)
    AND merged into the team's ``fb_ad_account_links``
    (``_merge_team_ad_account_links``); the importer emits one entry
    per link (``get_insights.py`` ``_build_account_wide_entries``), each with
    the link's own ``connection_esid`` so the token matches the account.

    ``metadata.is_active_import`` is still stamped, but it is now a
    NARROW historical marker — "the account the legacy scalar
    ``fb_ad_account_esid`` points at" — NOT "the only account that
    imports". Nothing renders it today (it is served by
    ``/api/.../connection-selections`` and unused by the frontend). Do not
    reintroduce it as an import-status signal: the team's links are the
    authority. See.

    ``fb_account_provider_id`` (legacy single string) is still accepted
    for the dual-mode ``/configure`` prod-fallback path (Risk Register
    R8); :func:`_normalize_accounts_arg` collapses either shape.

    ``selected_pages`` is the user's EXPLICIT page choice from the
    wizard — ``[{"fb_page_id": ..., "fb_page_name": ...}]``. When provided it
    is passed to the page bridge, which links EXACTLY those pages
    (writes their de_asset_fb_pages docs + ``team_facebook_page_esids``) rather
    than walking the ad account. This fixes, where the wizard discarded
    the selection and ad-account-walk discovery returned nothing for a Page not
    reachable via the chosen ad account. ``fb_import_mode`` is left as the
     bridge set it (account_wide) — the page importer keys on
    ``ds_facebook`` + page ESIDs independently, so the team imports BOTH its ad
    account (account-wide) AND the linked pages. The legacy ``selected_page_ids``
    arg is kept for the onboarding callers that still pass bare ids (no names);
    it is recorded for analytics/audit and used as a fallback page-id source.

    Fail-soft envelope around each bridge call matches the onboarding
    step-6 finish posture (a single bridge failure must not roll back
    the connection or the other bridges' writes). Returns a result dict
    with per-bridge status flags so the route can surface partial
    outcomes to the wizard UI.
    """
    if not username:
        raise WizardError("username is required")
    if not team_esid:
        raise WizardError("team_esid is required")
    if not connection_esid:
        raise WizardError("connection_esid is required")

    normalized_accounts = _normalize_accounts_arg(accounts, fb_account_provider_id)
    if not normalized_accounts:
        raise WizardError("At least one ad account is required")

    try:
        from web.blueprints.analytics.routes import (
            _bridge_meta_assets,
            _bridge_instagram_assets,
            _bridge_fb_pages,
            _kickoff_on_demand_backfill,
        )
    except ImportError:
        from ..blueprints.analytics.routes import (  # type: ignore[no-redef]
            _bridge_meta_assets,
            _bridge_instagram_assets,
            _bridge_fb_pages,
            _kickoff_on_demand_backfill,
        )

    # Resolve the team's workspace_esid for the bridges that need it
    #. Mirrors onboarding's step-6 finish
    # which reads primary_workspace_esid from the user profile; here
    # the team doc itself is authoritative since data-connectors picks
    # a specific team rather than relying on the user's primary.
    team_doc = AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}
    workspace_esid = (team_doc.get("workspace_esid") or "").strip() or None

    accepted_ids = [a["provider_id"] for a in normalized_accounts]
    friendly_names = {a["provider_id"]: a["name"] for a in normalized_accounts if a["name"]}

    # normalize the explicit page selection. Prefer ``selected_pages``
    # (objects with names, from the Data Connectors wizard); fall back to bare
    # ``selected_page_ids`` (legacy onboarding callers) so those pages are still
    # linked, just without a friendly name (the id doubles as the name).
    effective_selected_pages: list[dict[str, Any]] = []
    for p in selected_pages or []:
        pid = str((p or {}).get("fb_page_id") or "").strip()
        if pid:
            effective_selected_pages.append({"fb_page_id": pid, "fb_page_name": (p.get("fb_page_name") or pid)})
    if not effective_selected_pages:
        for pid in selected_page_ids or []:
            pid = str(pid or "").strip()
            if pid:
                effective_selected_pages.append({"fb_page_id": pid, "fb_page_name": pid})

    # the selection came from the request body: bind it to what the
    # connection can list, and never to a Page another workspace's team owns.
    if effective_selected_pages:
        chosen_ids = [p["fb_page_id"] for p in effective_selected_pages]
        _require_pages_listable_by_connection(connection_esid, chosen_ids)
        _refuse_pages_linked_in_other_workspaces(chosen_ids, team_esid, workspace_esid)

    result: dict[str, Any] = {
        "connection_esid": connection_esid,
        "team_esid": team_esid,
        "fb_bridge_ok": False,
        "ig_bridge_ok": False,
        "page_bridge_ok": False,
        "connection_assets_written": 0,
        "accounts_selected": len(accepted_ids),
        "active_import_provider_id": accepted_ids[0] if accepted_ids else "",
        "pages_selected": len(effective_selected_pages),
        "pages_linked": 0,  # set after the page bridge from the team back-link
        "kickoff_ok": False,
        # user-actionable reasons a selection produced no wiring.
        "fb_shared_accounts": [],
        "fb_probe_denied": [],
        "warning": "",
        # Instagram discovery report. `ig_notice` is a NON-blocking
        # message for the wizard's done pane ("N of M pages had no Instagram"),
        # unlike `warning`, which blocks the done pane and means the connector
        # was not configured.
        "ig_accounts_linked": 0,
        "ig_pages_without_ig": [],
        "ig_notice": "",
    }

    # ── FB ad-account bridge ────────────────────────────────────
    try:
        fb_outcome = (
            _bridge_meta_assets(
                username=username,
                team_esid=team_esid,
                connection_esid=connection_esid,
                accepted_ids=accepted_ids,
                friendly_names=friendly_names,
                workspace_esid=workspace_esid,
            )
            or {}
        )
        # fb_bridge_ok used to mean only "did not raise", so a run where
        # the ownership guard refused EVERY selected ad account reported
        # success while ds_facebook was never written and the card stayed
        # unconfigured. Report what the bridge actually wired, and carry the
        # skip reasons so the route can tell the user what to do.
        result["fb_bridge_ok"] = bool(fb_outcome.get("wrote_any"))
        result["fb_shared_accounts"] = fb_outcome.get("shared_accounts") or []
        result["fb_probe_denied"] = fb_outcome.get("probe_denied") or []
    except Exception as e:  # noqa: BLE001 — fail-soft envelope per onboarding posture
        logging.warning(
            "Path A:  bridge raised for user=%r team=%s: %s",
            username,
            team_esid,
            e,
        )

    # ── FB pages bridge ─────────────────────────────────────────
    try:
        _bridge_fb_pages(
            username=username,
            team_esid=team_esid,
            connection_esid=connection_esid,
            accepted_ids=accepted_ids,
            workspace_esid=workspace_esid,
            selected_pages=effective_selected_pages or None,
        )
        result["page_bridge_ok"] = True
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "Path A:  page bridge raised for user=%r team=%s: %s",
            username,
            team_esid,
            e,
        )

    # ── IG bridge ───────────────────────────────────────────────
    try:
        ig_outcome = _bridge_instagram_assets(
            username=username,
            team_esid=team_esid,
            connection_esid=connection_esid,
            accepted_ids=accepted_ids,
            workspace_esid=workspace_esid,
            # Instagram profiles are derived from the user's
            # SELECTED pages, not from the pages promotable through the accepted
            # ad accounts. Mirrors the page bridge above, which has always
            # received this. Omitting it was the bug: a shared ad account's
            # promotable pages reach other teams' profiles (too wide), while a
            # selected page that is not promotable on the accepted account was
            # never looked up at all (too narrow).
            selected_pages=effective_selected_pages or None,
        )
        result["ig_bridge_ok"] = True
        # surface the discovery report so "pulled some but not all"
        # is answerable from the wizard itself, not just the server log. The
        # notice is informational (the FB connector IS configured), so it must
        # not go through `warning`, which suppresses the done pane. Pages with
        # nothing connected are common and expected — the notice only names
        # them and says what to do; it never claims a failure.
        ig_outcome = ig_outcome or {}
        result["ig_accounts_linked"] = int(ig_outcome.get("ig_accounts_linked") or 0)
        result["ig_pages_without_ig"] = list(ig_outcome.get("pages_without_ig") or [])
        if result["ig_pages_without_ig"] and effective_selected_pages:
            names = ", ".join(result["ig_pages_without_ig"])
            n = len(result["ig_pages_without_ig"])
            total = int(ig_outcome.get("pages_walked") or 0) or len(effective_selected_pages)
            result["ig_notice"] = (
                f"{n} of {total} selected page(s) had no Instagram account connected: {names}. "
                "To import Instagram data for a page, connect an Instagram Business or "
                "Creator account to that page in Meta Business Suite — an account that "
                "only exists in the Business Portfolio is not connected to any page."
            )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "Path A:  IG bridge raised for user=%r team=%s: %s",
            username,
            team_esid,
            e,
        )

    # ── Bug 2 — de_connection_asset upserts ─────────────────────
    # The bridges above write the legacy de_asset_fb_* / de_asset_ig_*
    # docs and stamp ``wizard_connection_esid`` on each one, but none of
    # them write the parallel ``de_connection_asset`` row that the
    # "Manage connection" deep-link reads to resolve a
    # connection_esid from a team's linked legacy assets
    # (find_connection_esid_by_legacy_assets). Without these rows the
    # Configured Connectors card has no path back to the OAuth-bearing
    # connection — the Manage button is hidden and the user-visible
    # post-wizard state misses the connector entirely. Mirror onboarding
    # step-6's posture which writes upsert_connection_asset
    # immediately after the bridges in its own finish handler.
    # resolve the ad-account esid the bridge stamped onto
    # the team's legacy scalar (the FIRST written asset doc); we re-read the
    # team doc here so the connection-asset upsert can flag that row.
    # this is NOT "the only account that imports" — every accepted
    # account is also a link, and every link imports.
    active_import_asset_esid = ""
    try:
        refreshed_team = AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}
        active_import_asset_esid = (refreshed_team.get("fb_ad_account_esid") or "").strip()
        # report how many pages the bridge actually linked, so the
        # wizard's success message is honest (pages vs account_wide-only).
        linked_pages = refreshed_team.get("team_facebook_page_esids") or []
        if isinstance(linked_pages, str):
            linked_pages = [linked_pages] if linked_pages else []
        result["pages_linked"] = len(linked_pages)
    except Exception as e:  # noqa: BLE001 — non-fatal; flag just won't be set
        logging.warning(
            "could not re-read team %s for active-import esid: %s",
            team_esid,
            e,
        )

    try:
        n_written = _upsert_connection_assets(
            connection_esid=connection_esid,
            username=username,
            team_esid=team_esid,
            active_import_asset_esid=active_import_asset_esid or None,
        )
        result["connection_assets_written"] = n_written
    except Exception as e:  # noqa: BLE001 — fail-soft envelope per onboarding posture
        logging.warning(
            "Path A / connection_asset upsert raised for user=%r team=%s: %s",
            username,
            team_esid,
            e,
        )

    # ── starter dashboards ─────────────────────────────────────
    # The wizard's step-8 installs the connector's template pack; this path
    # never did, so a user who skipped step 6 and added Meta here landed in an
    # empty Dashboard Builder. Same call as the wizard (ad-account provider ids
    # as the source ids, the team's workspace for the menu), fail-soft, and
    # idempotent on (team, template) so re-configuring never duplicates.
    result["first_look_status"] = "skipped"
    result["first_look_id"] = None
    try:
        from asset_management.web.services.template_service import TemplateService

        pack_outcome = TemplateService.install_starter_pack(
            connector_id="meta_ads",
            team_esid=team_esid,
            source_account_ids=list(accepted_ids),
            workspace_esid=workspace_esid,
            origin="data_connectors_fb",
        )
        result["first_look_status"] = pack_outcome.get("first_look_status") or "skipped"
        result["first_look_id"] = pack_outcome.get("first_look_id")
    except Exception as e:  # noqa: BLE001 — belt and braces; the wrapper is already fail-soft
        logging.warning("starter pack wrapper raised for team=%s: %s", team_esid, e)
        result["first_look_status"] = "exception"

    # ── Importer kickoff ────────────────────────────────────────────────
    try:
        kickoff_result = _kickoff_on_demand_backfill(
            username=username,
            connection_esid=connection_esid,
            connector_id="meta_ads",
            asset_count=len(accepted_ids),
        )
        result["kickoff_ok"] = bool(kickoff_result.get("ok"))
        if not result["kickoff_ok"]:
            logging.warning(
                "Path A: importer kickoff returned not-ok for user=%r team=%s: %s",
                username,
                team_esid,
                kickoff_result,
            )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "Path A: importer kickoff raised for user=%r team=%s: %s",
            username,
            team_esid,
            e,
        )

    logging.info(
        "WizardService.fb_finish_data_connectors_wizard: user=%s team=%s "
        "connection=%s fb=%s page=%s ig=%s conn_assets=%d kickoff=%s",
        username,
        team_esid,
        connection_esid,
        result["fb_bridge_ok"],
        result["page_bridge_ok"],
        result["ig_bridge_ok"],
        result["connection_assets_written"],
        result["kickoff_ok"],
    )

    # the wizard must not report success for a run that wired nothing.
    # When the ad-account bridge wrote no account, the Facebook connector is NOT
    # configured (ds_facebook unwritten, no links) and the card will stay
    # unconfigured, so the user needs a reason rather than a silent success.
    #
    # NOTE: a SHARED ad account is no longer a reason to fail. Sharing one Ad
    # Account across teams is the expected Meta topology (rules 6/7/9/10: a
    # Business Portfolio holds many Ad Accounts, and Ads for the same Page can run
    # from different ones), so those accounts now link normally — they are only
    # barred from becoming the legacy scalar back-link. ``fb_shared_accounts`` is
    # retained as INFORMATIONAL only and must never drive this warning.
    if not result["fb_bridge_ok"]:
        denied = result["fb_probe_denied"]
        if denied:
            ids = ", ".join(d.get("fb_ad_account_id") or "?" for d in denied)
            result["warning"] = (
                f"Facebook was not connected: this login does not have ads_read access "
                f"to the selected ad account(s) ({ids}). Grant the login access in "
                "Meta Business Manager, then re-run this wizard."
            )
        else:
            result["warning"] = (
                "Facebook was not connected: none of the selected ad accounts could be "
                "linked. Re-run the wizard, and contact an administrator if it recurs."
            )
        logging.warning(
            "reporting a NON-configured Facebook outcome to user=%s team=%s " "(shared=%d probe_denied=%d) — %s",
            username,
            team_esid,
            len(result["fb_shared_accounts"]),
            len(denied),
            result["warning"],
        )

    return result


def _upsert_connection_assets(
    *,
    connection_esid: str,
    username: str,
    team_esid: str,
    active_import_asset_esid: str | None = None,
) -> int:
    """Bug 2 — write a ``de_connection_asset`` row per legacy asset
    the bridges just wrote, keyed back to the wizard connection.

    The three bridges each stamp
    ``wizard_connection_esid`` on every legacy ``de_asset_fb_*`` /
    ``de_asset_ig_*`` doc they write. We re-query each index by that
    field to enumerate the freshly-written esids, then call
    ``ConnectionService.upsert_connection_asset`` for each one with
    ``legacy_asset_index`` + ``legacy_asset_esid`` stamped so the
     ``find_connection_esid_by_legacy_assets`` lookup can walk
    from the team's connector card back to this connection.

    ``active_import_asset_esid`` is the
    ``de_asset_fb_ad_accounts`` doc id the team's legacy scalar
    ``fb_ad_account_esid`` points at (see
    ``fb_finish_data_connectors_wizard``). The matching fb_ad_account
    ``de_connection_asset`` row gets ``metadata.is_active_import=True``;
    every other fb_ad_account row gets ``False``.

    **the flag no longer means "the only account that imports".**
    Every accepted account becomes a link and every link imports; the flag
    is now just a marker for which account the legacy scalar points at.
    Nothing renders it. Do not surface it as import status.
    Page / IG rows don't carry the flag (it's meaningless for them).
    When ``active_import_asset_esid`` is None (legacy single-account
    path, or the team re-read failed) no flag is written — the UI
    falls back to treating the first row as active.

    Returns the number of ``de_connection_asset`` rows successfully
    upserted across all three asset types. Per-asset failures log at
    WARNING and continue — same fail-soft envelope as the bridges
    themselves; a partial write is still better than zero (the rows we
    did land make their corresponding connector cards Manage-able).

    Idempotent: ``upsert_connection_asset`` keys on (connection_esid,
    asset_type, provider_asset_id), so a wizard re-run refreshes rather
    than duplicates.
    """
    try:
        from lib.es_helpers import get_elasticsearch_client, dual_term
    except ImportError:
        from ...lib.es_helpers import get_elasticsearch_client, dual_term  # type: ignore[no-redef]

    try:
        from web.services.connection_service import (
            ConnectionService,
            ASSET_TYPE_FB_AD_ACCOUNT,
            ASSET_TYPE_FB_PAGE,
            ASSET_TYPE_IG_ACCOUNT,
        )
    except ImportError:
        from .connection_service import (  # type: ignore[no-redef]
            ConnectionService,
            ASSET_TYPE_FB_AD_ACCOUNT,
            ASSET_TYPE_FB_PAGE,
            ASSET_TYPE_IG_ACCOUNT,
        )

    es = get_elasticsearch_client()
    if es is None:
        logging.warning(
            "ES client unavailable; cannot upsert connection_assets for connection=%s",
            connection_esid,
        )
        return 0

    # (legacy_index, asset_type, provider_id_field, label_field) — the
    # field names match what each bridge writes onto the legacy doc.
    legacy_specs: list[tuple[str, str, str, str]] = [
        ("de_asset_fb_ad_accounts", ASSET_TYPE_FB_AD_ACCOUNT, "fb_ad_account_id", "fb_ad_account_title"),
        ("de_asset_fb_pages", ASSET_TYPE_FB_PAGE, "fb_page_id", "fb_page_name"),
        ("de_asset_ig_accounts", ASSET_TYPE_IG_ACCOUNT, "ig_account_id", "ig_account_title"),
    ]

    n_written = 0
    for legacy_index, asset_type, provider_id_field, label_field in legacy_specs:
        try:
            hits = es.search(
                index=legacy_index,
                body={
                    # dual-term — a.keyword-only match dropped every
                    # legacy asset on bare-keyword envs, so the bridge
                    # silently carried nothing forward to connection_assets.
                    "query": dual_term("wizard_connection_esid", connection_esid),
                    "size": 1000,
                },
            )
        except Exception as e:  # noqa: BLE001 — boundary, fail-soft per-index
            logging.warning(
                "search on %s by wizard_connection_esid=%s raised: %s",
                legacy_index,
                connection_esid,
                e,
            )
            continue

        for hit in (hits.get("hits") or {}).get("hits") or []:
            legacy_esid = hit.get("_id") or ""
            source = hit.get("_source") or {}
            provider_asset_id = str(source.get(provider_id_field) or "").strip()
            label = str(source.get(label_field) or "").strip() or provider_asset_id
            if not provider_asset_id:
                logging.warning(
                    "%s/%s missing %s — skipping connection_asset upsert",
                    legacy_index,
                    legacy_esid,
                    provider_id_field,
                )
                continue

            # stamp is_active_import on the fb_ad_account row the
            # team's single fb_ad_account_esid points at. Only fb_ad_account
            # rows carry the flag; page/IG rows leave metadata empty.
            metadata: dict[str, Any] | None = None
            if asset_type == ASSET_TYPE_FB_AD_ACCOUNT and active_import_asset_esid:
                metadata = {"is_active_import": legacy_esid == active_import_asset_esid}

            try:
                upsert_result = ConnectionService.upsert_connection_asset(
                    connection_esid=connection_esid,
                    asset_type=asset_type,
                    provider_asset_id=provider_asset_id,
                    label=label,
                    metadata=metadata,
                    legacy_asset_index=legacy_index,
                    legacy_asset_esid=legacy_esid,
                )
            except Exception as e:  # noqa: BLE001 — boundary, fail-soft per-row
                logging.warning(
                    "upsert_connection_asset raised for connection=%s asset=%s (%s/%s): %s",
                    connection_esid,
                    provider_asset_id,
                    legacy_index,
                    legacy_esid,
                    e,
                )
                continue

            if upsert_result.get("success"):
                n_written += 1
            else:
                logging.warning(
                    "upsert_connection_asset returned not-ok for connection=%s asset=%s (%s/%s): %s",
                    connection_esid,
                    provider_asset_id,
                    legacy_index,
                    legacy_esid,
                    upsert_result.get("error"),
                )

    logging.info(
        "upserted %d de_connection_asset rows for user=%s team=%s connection=%s",
        n_written,
        username,
        team_esid,
        connection_esid,
    )
    return n_written


# Path A Phase 3 (2026-05-28) removed the legacy
# fb_pick_or_create_app_user / fb_app_user_status / _read_fb_app_user_status
# trio that drove the pre-Path-A wizard's User step. The Path A wizard
# doesn't have a User step — the OAuth roundtrip mints a de_connection row
# directly via WizardOAuthService. fb_link_pages_to_team and
# fb_list_pages_via_graph stay (release-plan test calls the former
# and prod-fallback safety per the plan's Risk Register R8 keeps both).


def fb_list_pages_via_graph(app_user_esid: str) -> list[dict[str, str]]:
    """Step 4: call FB Graph /me/accounts with the user's access token to
    list the pages they manage. Returns ``[{"id", "name"}]``.

    Imported lazily — the requests library is already in the container for
    other import scripts, but we want service-import to stay cheap.
    """
    source = AssetService.get_by_id("de_asset_fb_app_users", app_user_esid)
    if not source:
        # don't echo app_user_esid into client error.
        logging.warning("connector_wizard: fb_app_user not found app_user_esid=%r", app_user_esid)
        raise WizardError("FB app user not found")

    encrypted_token = source.get("fb_app_user_token")
    if not encrypted_token:
        raise WizardError("This user has no access token yet. Authorize via OAuth first.")

    try:
        from lib.encryption import decrypt
    except ImportError:
        from ...lib.encryption import decrypt  # pragma: no cover

    try:
        access_token = decrypt(encrypted_token)
    except Exception as e:  # noqa: BLE001 — decrypt raises InvalidToken and friends
        logging.warning("connector_wizard: decrypt fb_app_user token app_user_esid=%r failed: %s", app_user_esid, e)
        raise WizardError("Could not decrypt access token for this user. Reconnect Facebook and try again.") from e

    import requests

    try:
        resp = requests.get(
            f"{FB_GRAPH_URL}/me/accounts",
            params={
                "access_token": access_token,
                "fields": "id,name,username",
                "limit": 200,
            },
            timeout=15,
        )
    except requests.RequestException as e:
        raise _fb_graph_unreachable(e, "pages") from e

    if resp.status_code != 200:
        raise WizardError(_fb_graph_error_message(resp, "pages"))

    try:
        data = resp.json()
    except ValueError as e:
        raise _fb_graph_non_json(e, "pages") from e

    pages = []
    for item in data.get("data") or []:
        pid = item.get("id")
        if not pid:
            continue
        pages.append(
            {
                "id": str(pid),
                "name": item.get("name") or "",
                "username": item.get("username") or "",
            }
        )
    pages.sort(key=lambda p: (p["name"] or "").lower())
    return pages


def fb_link_pages_to_team(
    team_esid: str,
    fb_account_esid: str,
    pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Step 5: ensure fb_pages records exist for each chosen page, then
    update the team to ds_facebook=true and write team_facebook_page_esids.

     (2026-05-26) — after the FB chain lands, walks each Page to
    its linked Instagram Business account and writes the IG asset chain
    (``de_asset_ig_accounts`` + ``team_instagram_profile_esids`` +
    ``ds_instagram=true``). Mirrors the wizard step-7 bridge so
    a single Meta OAuth grant on /assets/data-connectors/ provisions
    both surfaces. Fail-soft: an IG bridge failure does not roll back
    the FB chain (the user can re-run later by re-clicking Set up Meta
    Ads, which is idempotent on the FB side).

    ``pages`` items may be either:
      - ``{"esid": "..."}`` — already-existing fb_pages doc
      - ``{"fb_page_id": "...", "fb_page_name": "...", "fb_page_slug": "..."}`` — create

    Returns ``{"page_esids": [...], "created_count": N,
              "ig_account_esids": [...], "ig_discovered_count": M}``.
    """
    if not team_esid:
        raise WizardError("team_esid is required")
    if not fb_account_esid:
        raise WizardError("fb_account_esid is required")
    if not pages:
        raise WizardError("At least one page is required")
    # never claim a Page that a team in another workspace owns.
    team_ws = ((AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}).get("workspace_esid") or "").strip() or None
    _refuse_pages_linked_in_other_workspaces(
        [str(p.get("fb_page_id") or "") for p in pages if isinstance(p, dict) and not p.get("esid")], team_esid, team_ws
    )

    page_esids: list[str] = []
    created = 0
    # track the page graph IDs + names so we can hand them to
    # the IG bridge below. For existing-esid entries we read the graph
    # id back from the fb_pages doc; for new entries we already have it.
    page_info_for_ig: list[dict[str, str]] = []
    for p in pages:
        if p.get("esid"):
            page_esids.append(p["esid"])
            existing_page = AssetService.get_by_id("de_asset_fb_pages", p["esid"]) or {}
            graph_id = existing_page.get("fb_page_id") or ""
            name = existing_page.get("fb_page_name") or graph_id
            if graph_id:
                page_info_for_ig.append({"fb_page_id": graph_id, "fb_page_name": name})
            continue
        # Validate minimum new-page fields
        if not (p.get("fb_page_id") and p.get("fb_page_name")):
            raise WizardError("Each new page needs at least fb_page_id and fb_page_name")
        new_page = {
            "fb_page_slug": p.get("fb_page_slug") or p["fb_page_name"],
            "fb_page_id": p["fb_page_id"],
            "fb_page_name": p["fb_page_name"],
            # Inject parent account under the select-field name (see
            # fb_pick_or_create_app for the convention).
            "fb_account_title": fb_account_esid,
        }
        page_esid = _create_asset("facebook_pages", new_page)
        page_esids.append(page_esid)
        page_info_for_ig.append({"fb_page_id": p["fb_page_id"], "fb_page_name": p["fb_page_name"]})
        created += 1

    _update_team_data_source(
        team_esid=team_esid,
        flag_field="ds_facebook",
        esid_field="team_facebook_page_esids",
        esid_value=page_esids,
    )

    logging.info(
        "WizardService.fb_link_pages_to_team: team=%s account=%s pages=%s created=%s",
        team_esid,
        fb_account_esid,
        page_esids,
        created,
    )

    # bridge the IG asset chain. Failures here are demoted
    # to a warning so the FB-side commit isn't lost. Returned counts
    # let the route surface the IG outcome to the wizard UI.
    ig_result = _bridge_ig_for_data_connectors_wizard(
        team_esid=team_esid,
        fb_account_esid=fb_account_esid,
        page_info=page_info_for_ig,
    )
    return {
        "page_esids": page_esids,
        "created_count": created,
        "ig_account_esids": ig_result.get("ig_account_esids", []),
        "ig_discovered_count": ig_result.get("discovered_count", 0),
    }


def _bridge_ig_for_data_connectors_wizard(
    *,
    team_esid: str,
    fb_account_esid: str,
    page_info: list[dict[str, str]],
) -> dict[str, Any]:
    """bridge IG provisioning from the data-connectors FB wizard.

    Resolves a usable access token from the fb_account → fb_apps →
    fb_app_users chain (latest-expiry user wins, mirroring
    ``connector_status_service._resolve_fb_token_health``), then hands
    the page-info list to :func:`meta_ig_bridge_service.bridge_ig_chain_for_pages`.

    When at least one IG account lands, stamps ``ds_instagram=true``
    and appends to ``team_instagram_profile_esids`` so the IG importer's
    two-stage filter picks up the team on its next run.

    Fail-soft envelope around every step — a missing token, a Graph
    error, or an ES write failure logs WARNING and returns
    ``{"ig_account_esids": [], "discovered_count": 0}``. The FB chain
    already committed in the caller, so users with FB-only outcomes
    (partial-grant cohort or Pages with no linked IG) still see a
    successful wizard.
    """
    if not page_info:
        return {"ig_account_esids": [], "discovered_count": 0}

    try:
        from .meta_ig_bridge_service import bridge_ig_chain_for_pages
    except ImportError:  # pragma: no cover
        from asset_management.web.services.meta_ig_bridge_service import (
            bridge_ig_chain_for_pages,
        )

    access_token = _resolve_token_for_fb_account(fb_account_esid)
    if not access_token:
        logging.info(
            "IG bridge: no usable fb_app_user token for fb_account=%s; "
            "IG provisioning skipped (FB chain unaffected)",
            fb_account_esid,
        )
        return {"ig_account_esids": [], "discovered_count": 0}

    try:
        result = bridge_ig_chain_for_pages(
            access_token=access_token,
            pages=page_info,
            team_esid=team_esid,
        )
    except Exception as e:  # noqa: BLE001 — fail-soft envelope
        logging.warning("IG bridge: bridge_ig_chain_for_pages raised: %s", e)
        return {"ig_account_esids": [], "discovered_count": 0}

    discovered = result.get("ig_account_esids") or []
    if not discovered:
        logging.info(
            "IG bridge: no Instagram Business accounts discovered for "
            "team=%s (partial-grant cohort or Pages without linked IG)",
            team_esid,
        )
        return result

    # Stamp ds_instagram + team_instagram_profile_esids on the team doc.
    # Read-modify-write to preserve fb_import_mode and any
    # other fields the route owns.
    try:
        existing = AssetService.get_by_id(TEAMS_INDEX, team_esid) or {}
        merged = dict(existing)
        merged["ds_instagram"] = "true"
        existing_ig = existing.get("team_instagram_profile_esids") or []
        if isinstance(existing_ig, str):
            existing_ig = [existing_ig] if existing_ig else []
        deduped: list[str] = list(existing_ig)
        seen = set(deduped)
        for esid in discovered:
            if esid not in seen:
                deduped.append(esid)
                seen.add(esid)
        merged["team_instagram_profile_esids"] = deduped
        if not AssetService.update(TEAMS_INDEX, team_esid, merged):
            logging.warning(
                "IG bridge: team update returned False for team=%s "
                "(IG asset docs landed but importer won't see them)",
                team_esid,
            )
    except Exception as e:  # noqa: BLE001
        logging.warning(
            "IG bridge: team back-link update failed team=%s: %s "
            "(IG asset docs landed but importer won't see them)",
            team_esid,
            e,
        )

    return result


def _resolve_token_for_fb_account(fb_account_esid: str) -> str | None:
    """Resolve a decrypted Meta access token from an fb_account's app-user chain.

    Walks fb_account → fb_apps → fb_app_users, picks the user with the
    latest token expiry, and decrypts their stored token. Returns
    ``None`` when any link is missing — the IG bridge then short-circuits
    cleanly without touching Graph.
    """
    if not fb_account_esid:
        return None

    try:
        from lib.es_helpers import get_elasticsearch_client
        from lib.encryption import decrypt
    except ImportError:  # pragma: no cover
        from ....lib.es_helpers import get_elasticsearch_client  # type: ignore[no-redef]
        from ....lib.encryption import decrypt  # type: ignore[no-redef]

    es = get_elasticsearch_client()
    if es is None:
        return None

    try:
        apps_resp = es.search(
            index="de_asset_fb_apps",
            body={"query": {"term": {"fb_account_esid": fb_account_esid}}, "size": 50},
        )
        app_esids = [h["_id"] for h in apps_resp.get("hits", {}).get("hits", [])]
    except Exception as e:  # noqa: BLE001
        logging.warning("IG bridge: fb_apps lookup failed: %s", e)
        return None
    if not app_esids:
        return None

    try:
        users_resp = es.search(
            index="de_asset_fb_app_users",
            body={"query": {"terms": {"fb_app_esid": app_esids}}, "size": 100},
        )
        user_hits = users_resp.get("hits", {}).get("hits", [])
    except Exception as e:  # noqa: BLE001
        logging.warning("IG bridge: fb_app_users lookup failed: %s", e)
        return None

    candidates: list[tuple[int, str]] = []
    for hit in user_hits:
        src = hit.get("_source") or {}
        enc = src.get("fb_app_user_token")
        if not enc:
            continue
        candidates.append((int(src.get("fb_app_user_token_exp") or 0), enc))
    if not candidates:
        return None

    candidates.sort(key=lambda t: t[0], reverse=True)
    encrypted = candidates[0][1]
    try:
        return decrypt(encrypted)
    except Exception as e:  # noqa: BLE001
        logging.warning("IG bridge: token decrypt failed: %s", e)
        return None

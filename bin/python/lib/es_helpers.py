import os
import re
import secrets
import ssl
import threading
from elasticsearch import Elasticsearch, NotFoundError
import logging
from migration.schema_loader import get_schema_for_index_type


# mapping-drift-safe exact-match clauses.
#
# de_asset_* / de_connection* string fields drift across environments: some
# are mapped as bare ``keyword``, others as ``text`` + a ``.keyword`` sub-field
# (reindexes land at different times per env). An exact-match ``term``/
# ``terms`` silently returns ZERO hits when it targets the wrong shape — a
# ``term`` on ``field.keyword`` matches nothing if the field is bare keyword,
# and a ``term`` on bare ``field`` matches nothing if the field is text-only.
# No error is raised; the query just comes back empty. This has caused repeated
# production incidents, each the same bug at a
# different query site.
#
# These helpers OR both shapes in a ``bool/should`` so the same clause is
# correct regardless of the live mapping — no per-environment probe needed. A
# doc stores the value under exactly one shape, so the union matches exactly
# the docs that should match (it never over-matches). Pass the BARE field name;
# do not append ``.keyword`` yourself.
def dual_term(field, value):
    """Exact-match ``field == value`` that works whether ``field`` is mapped
    bare ``keyword`` or ``text`` + ``.keyword``. Pass the bare field name."""
    field = field[:-8] if field.endswith(".keyword") else field
    return {
        "bool": {
            "should": [
                {"term": {field: value}},
                {"term": {f"{field}.keyword": value}},
            ],
            "minimum_should_match": 1,
        }
    }


def dual_terms(field, values):
    """``terms`` (value-in-list) twin of :func:`dual_term`. Pass the bare
    field name; matches on both the bare field and its ``.keyword`` sub-field."""
    field = field[:-8] if field.endswith(".keyword") else field
    return {
        "bool": {
            "should": [
                {"terms": {field: values}},
                {"terms": {f"{field}.keyword": values}},
            ],
            "minimum_should_match": 1,
        }
    }


class IncompleteLookupError(RuntimeError):
    """ES matched more documents than it returned, so an id lookup cannot be trusted."""


EXISTING_IDS_BATCH = 500
EXISTING_IDS_MAX_HITS = 10000  # ES index.max_result_window default


def existing_doc_ids(es, index_pattern: str, ids: list[str], batch: int = EXISTING_IDS_BATCH) -> set[str]:
    """The subset of ``ids`` that some index matching ``index_pattern`` holds.

    exact. A document id can live in several indices at once (a team
    index and the demo clone), so a search sized to the batch let duplicate hits
    crowd other ids out and reported them missing. Each batch asks for up to
    ``EXISTING_IDS_MAX_HITS`` hits with an exact total; if ES matched more than
    it returned, :class:`IncompleteLookupError` is raised instead of guessing.
    Callers that delete or move files on the answer must treat that as "unknown".
    """
    found: set[str] = set()
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        resp = es.search(
            index=index_pattern,
            body={
                "size": EXISTING_IDS_MAX_HITS,
                "_source": False,
                "track_total_hits": True,
                "query": {"ids": {"values": chunk}},
            },
        )
        resp = getattr(resp, "body", resp)
        hits = (resp.get("hits") or {}).get("hits") or []
        total = ((resp.get("hits") or {}).get("total") or {}).get("value", len(hits))
        if total > len(hits):
            raise IncompleteLookupError(f"{index_pattern}: {total} matches, {len(hits)} returned")
        found.update(str(h.get("_id")) for h in hits)
    return found


# cache the Elasticsearch client at module level and reuse it.
#
# This function was building a NEW client (fresh connection pool + SSL
# context) AND calling es.ping() (a full network round-trip) on every
# call. A single dashboard web request calls it ~15× via the authz
# helpers, so the per-call rebuild+ping was ~900ms/request of pure
# overhead (ES query cost itself is ~3ms). The ES python client is
# designed to be long-lived and is thread-safe; its
# connection pool reconnects transparently, so one cached client is
# correct for both the web workers and the long-running importers.
#
# Cache key = the connection config (URL + auth + ssl), so a config
# change (e.g. ES_USE_SSL flip in a test) rebuilds rather than serving
# a stale client. The lock makes the build atomic under gthread workers.
_es_client = None
_es_client_key = None
_es_client_lock = threading.Lock()


def _resolve_es_auth():
    """Resolve the credential the shared app ES client authenticates with.

     (de-superuser): the app stack historically authenticated to ES
    as the built-in ``elastic`` superuser, whose blast radius is the entire
    cluster. This resolver lets a scoped service account / API key take over
    when its env vars are set, falling back to ``elastic`` so current deploys
    are byte-identical until the scoped credential is provisioned (Phase 2,
    km-infra owns ES security config).

    Precedence (first set wins):
      1. ``ES_APP_API_KEY``                         → ("api_key", <key>)
      2. ``ES_APP_USER`` + ``ES_APP_PASSWORD``      → ("basic", (user, password))
      3. ``elastic`` / ``ES01_PASSWORD`` (fallback) → ("basic", ("elastic", password))

    Returns an ``(auth_mode, auth_secret)`` pair. ``auth_mode`` is ``"api_key"``
    (secret is the key string) or ``"basic"`` (secret is a ``(user, password)``
    tuple). The pair is folded into the client cache key, so swapping the
    credential at runtime rebuilds the client rather than serving a stale one.
    """
    api_key = os.environ.get("ES_APP_API_KEY")
    if api_key:
        return ("api_key", api_key)
    es_user = os.environ.get("ES_APP_USER")
    es_password = os.environ.get("ES_APP_PASSWORD")
    if es_user and es_password:
        return ("basic", (es_user, es_password))
    return ("basic", ("elastic", os.environ.get("ES01_PASSWORD")))


def _es_connection_config():
    """Resolve the ES connection parameters from the environment.

    Returned tuple doubles as the cache key — if any value changes (incl. the
    resolved auth credential) the cached client is rebuilt.
    """
    es_ip = os.environ.get("ES01_INTERNAL_IP")
    use_ssl = os.environ.get("ES_USE_SSL", "true").lower() != "false"
    auth_mode, auth_secret = _resolve_es_auth()
    return (es_ip, use_ssl, auth_mode, auth_secret)


def _client_is_live(es):
    """True when ``es`` can reach the cluster with the credential it carries.

    ``ping`` alone is the wrong question. It is ``HEAD /``, i.e. the
    ES action ``cluster:monitor/main``, which ``manage_security`` does NOT
    grant — so a correctly least-privileged key (``de_es_admin``: cluster
    ``manage_security``, no indices) pings False and the builder used to report
    it "Unable to connect". A valid, correctly-scoped credential looked broken,
    while an over-privileged one worked: the check punished correct
    provisioning. Role B (``de_es_app``, the shared client) would hit the same
    wall, and there a ``None`` degrades quietly through the app's many
    ``if es is None`` guards.

    So: keep ``ping()`` as the fast path — unchanged for every principal that
    already passes it — and fall back to ``security.authenticate()``, which any
    authenticated principal may call and which answers the question actually
    being asked ("is this credential live?"). The two outcomes are logged
    distinctly; ``ping()`` collapses unreachable / bad-credential / forbidden
    into one ``False``, which is what hid this for so long.
    """
    try:
        if es.ping():
            return True
    except Exception as e:  # noqa: BLE001 — transport-level; the fallback decides
        logging.debug("ES ping raised (%s); trying security.authenticate()", e)

    try:
        who = es.security.authenticate()
    except Exception as e:  # noqa: BLE001 — boundary: genuinely unusable
        logging.error("Unable to connect to Elasticsearch (ping failed; authenticate: %s)", e)
        return False

    logging.info(
        "ES credential authenticated as %r but may not ping (no cluster:monitor/main) — "
        "treating the client as live",
        (who or {}).get("username", "?"),
    )
    return True


def _build_elasticsearch_client(es_ip, use_ssl, auth_mode, auth_secret):
    """Construct (but do not cache) an Elasticsearch client, verifying once
    at build time that the credential can reach the cluster (see
    :func:`_client_is_live`). Returns the client, or None
    on failure — preserving the long-standing None-on-failure contract
    every ``if es is None:`` caller relies on.

    ``auth_mode``/``auth_secret`` come from :func:`_resolve_es_auth`. :
    an API key authenticates via the client's ``api_key=`` kwarg; a basic
    credential via ``basic_auth=(user, password)``."""
    ca_cert_path = "/app/data/certs/es01_http_ca.crt"

    if auth_mode == "api_key":
        auth_kwargs = {"api_key": auth_secret}
    else:
        auth_kwargs = {"basic_auth": auth_secret}

    if use_ssl:
        # Create a default SSL context with relaxed X509 strict verification
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

        es_url = f"https://{es_ip}:9200"

        # Check if CA certificate exists
        if not os.path.exists(ca_cert_path):
            logging.error(f"CA certificate not found at: {ca_cert_path}")
            return None

        ctx.load_verify_locations(cafile=ca_cert_path)

        try:
            es = Elasticsearch(
                [es_url],
                ssl_show_warn=False,
                verify_certs=True,
                ssl_context=ctx,
                **auth_kwargs,
            )
            if not _client_is_live(es):
                return None
            return es
        except Exception as e:
            logging.error(f"Error connecting to Elasticsearch: {e}")
            return None
    else:
        es_url = f"http://{es_ip}:9200"
        try:
            es = Elasticsearch([es_url], **auth_kwargs)
            if not _client_is_live(es):
                return None
            return es
        except Exception as e:
            logging.error(f"Error connecting to Elasticsearch: {e}")
            return None


def get_elasticsearch_client():
    """Return a shared, cached Elasticsearch client.

    Builds once per process (per connection config) and reuses the
    client object thereafter — the client's connection pool handles
    reconnects, so this is safe across both short web requests and
    long-running import jobs. Returns None when the client cannot be
    built (ES unreachable at build time, missing CA cert), so existing
    ``if es is None:`` guards keep working. A failed build is NOT
    cached, so the next call retries rather than serving None forever.
    """
    global _es_client, _es_client_key
    key = _es_connection_config()
    # Fast path: healthy cached client for the current config.
    if _es_client is not None and _es_client_key == key:
        return _es_client
    with _es_client_lock:
        # Re-check under the lock — another thread may have built it.
        if _es_client is not None and _es_client_key == key:
            return _es_client
        client = _build_elasticsearch_client(*key)
        if client is not None:
            _es_client = client
            _es_client_key = key
        return client


def reset_elasticsearch_client():
    """Drop the cached client (tests / forced reconnect). The next
    get_elasticsearch_client() call rebuilds from the current env.
    Also drops the admin client so one reset clears both."""
    global _es_client, _es_client_key
    with _es_client_lock:
        _es_client = None
        _es_client_key = None
    reset_es_admin_client()


# ── ES Security-API write client ──────────────────────────────────
#
# Role B (``de_es_app``) is the shared client's identity
# and must NOT hold ``manage_security``: it authenticates every web request and
# every importer, so a compromise of it must not be able to mint users or
# roles. The few code paths that legitimately create / alter / delete ES
# Security users and roles — signup, email verification, SSO linking, role
# edits, admin provisioning, the subscription / source-limit metadata stamps —
# route the WRITE through this client instead, backed by its own credential:
# ``ES_ADMIN_API_KEY``, an API key whose role descriptor carries
# ``manage_security`` and nothing else the app needs (``de_es_admin``).
#
# Safe by absence: with ``ES_ADMIN_API_KEY`` unset the caller gets the shared
# client back, so a deploy without the key behaves exactly as before. With the
# key set but the client failing to build, the caller gets ``None`` — it does
# NOT fall back to the shared client. Before Role B loses ``manage_security``
# that fallback would silently keep running writes as the wrong principal;
# after it, the fallback would only turn a clear "admin client unavailable"
# into a 403 deeper in the call.
#
# Reads (``es.security.get_user`` and friends) stay on the shared client on
# purpose: they need at most ``read_security`` and are far more frequent.
ES_ADMIN_API_KEY_ENV = "ES_ADMIN_API_KEY"  # SSM /km/<env>/elasticsearch/de-es-admin-api-key

_es_admin_client = None
_es_admin_client_key = None
_es_admin_client_lock = threading.Lock()


def _es_admin_connection_config():
    """Connection tuple for the admin client — same host / TLS settings as the
    shared client, always API-key auth. Doubles as the cache key, so rotating
    ``ES_ADMIN_API_KEY`` at runtime rebuilds the client."""
    es_ip = os.environ.get("ES01_INTERNAL_IP")
    use_ssl = os.environ.get("ES_USE_SSL", "true").lower() != "false"
    return (es_ip, use_ssl, "api_key", os.environ.get(ES_ADMIN_API_KEY_ENV))


def get_es_admin_client(shared=None):
    """Return the client that ES Security-API WRITES go through.

    ``shared`` is the caller's already-resolved shared client — the one it
    just used for the preceding ``get_user`` read. When ``ES_ADMIN_API_KEY``
    is unset this returns exactly that object (or
    :func:`get_elasticsearch_client` when none was passed), so call sites and
    the tests that patch ``get_elasticsearch_client`` on a module see no
    change until the key is provisioned.

    When the key is set, returns a separate cached client authenticated with
    it. Returns ``None`` if that client cannot be built — see the section
    comment above for why it deliberately does not fall back.
    """
    global _es_admin_client, _es_admin_client_key
    key = _es_admin_connection_config()
    if not key[3]:
        return shared if shared is not None else get_elasticsearch_client()
    if _es_admin_client is not None and _es_admin_client_key == key:
        return _es_admin_client
    with _es_admin_client_lock:
        if _es_admin_client is not None and _es_admin_client_key == key:
            return _es_admin_client
        client = _build_elasticsearch_client(*key)
        if client is not None:
            _es_admin_client = client
            _es_admin_client_key = key
        else:
            logging.error(
                "ES admin client could not be built although ES_ADMIN_API_KEY is set — "
                "the Security-API write will fail; not falling back to the shared client"
            )
        return client


def reset_es_admin_client():
    """Drop the cached admin client (tests / forced reconnect)."""
    global _es_admin_client, _es_admin_client_key
    with _es_admin_client_lock:
        _es_admin_client = None
        _es_admin_client_key = None


# Helper functions for common Elasticsearch operations
def add_document(index_name, document):
    from datetime import datetime

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return False

    try:
        # Add @timestamp field if not present (required for data streams)
        if "@timestamp" not in document:
            document["@timestamp"] = datetime.utcnow().isoformat() + "Z"

        es.index(index=index_name, document=document, refresh="wait_for")
        return True
    except Exception as e:
        logging.error(f"Error indexing document in {index_name}: {e}")
        return False


def search_index(index_name, query=None, size=1000):
    """
    Search an Elasticsearch index and return the full response.

    Args:
        index_name: Name of the index to search
        query: Elasticsearch query DSL (defaults to match_all)
        size: Maximum number of results to return

    Returns:
        dict: Full Elasticsearch response object with hits, took, timed_out, etc.
    """
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return {"hits": {"hits": []}}

    # If no query is provided, default to match_all
    if query is None:
        query = {"match_all": {}}

    try:
        result = es.search(index=index_name, size=size, body={"query": query})
        _warn_if_size_cap_hit("search_index", index_name, result.get("hits", {}).get("hits", []), size)
        return result
    except Exception as e:
        logging.error(f"Error searching index {index_name}: {e}")
        return {"hits": {"hits": []}}


def _warn_if_size_cap_hit(caller, index_name, hits, size):
    """surface a probable silent truncation.

    ``search_index`` / ``get_all_documents`` issue ONE ``es.search`` with a
    fixed ``size``; anything beyond it is silently dropped in arbitrary ES
    order. A result that comes back exactly ``size`` long almost certainly
    hit that cap. Callers that must see every doc should use
    :func:`get_all_documents_paginated` instead.
    """
    try:
        if size and hits is not None and len(hits) >= size:
            logging.warning(
                "%s(%s): returned exactly size=%d hits — the result set is "
                "probably truncated. Use get_all_documents_paginated "
                "for unbounded reads.",
                caller,
                index_name,
                size,
            )
    except Exception:  # noqa: BLE001 — advisory logging must never raise
        pass


def get_all_documents_paginated(index_name, query=None, page_size=1000, es=None):
    """fetch EVERY document matching ``query``, with no size cap.

    The legacy single-shot readers (``search_index`` / ``get_all_documents``)
    silently drop everything beyond their fixed ``size`` once an index
    outgrows it — the shared root cause of the importer asset-fetch data loss
    . This helper pages through the full result set instead.

    Fast path: one plain ``es.search`` of ``page_size`` docs. Only when that
    first page comes back full (the legacy cap would have truncated) does it
    restart with a scroll context (the pattern the migration scripts already
    use) and page to exhaustion.

    Args:
        index_name: Index to search.
        query: ES query DSL (defaults to match_all).
        page_size: Docs per page; also the fast-path size.
        es: Optional client — importers that hold their own pass it so their
            existing client mocking/caching keeps working. Defaults to the
            shared cached client.

    Returns:
        list: ALL matching hit dicts (with ``_id`` / ``_source``). Empty list
        on connection failure or any search/scroll error — an error never
        returns a silently truncated subset (that is the exact failure this
        helper exists to prevent).
    """
    if es is None:
        es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return []

    if query is None:
        query = {"match_all": {}}

    try:
        first = es.search(index=index_name, size=page_size, body={"query": query})
        first_hits = first.get("hits", {}).get("hits", [])
        if len(first_hits) < page_size:
            return first_hits
    except Exception as e:
        logging.error(f"Error fetching documents from {index_name}: {e}")
        return []

    # First page came back full — the result set may exceed page_size.
    # Restart from zero with a scroll context and drain it.
    hits = []
    scroll_id = None
    try:
        resp = es.search(index=index_name, size=page_size, scroll="2m", body={"query": query})
        scroll_id = resp.get("_scroll_id")
        batch = resp.get("hits", {}).get("hits", [])
        while batch:
            hits.extend(batch)
            if not scroll_id:
                break
            resp = es.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id") or scroll_id
            batch = resp.get("hits", {}).get("hits", [])
        logging.info(
            "get_all_documents_paginated(%s): %d docs — beyond the single-page size %d",
            index_name,
            len(hits),
            page_size,
        )
        return hits
    except Exception as e:
        logging.error(f"Error scrolling documents from {index_name}: {e}")
        return []
    finally:
        if scroll_id:
            try:
                es.clear_scroll(scroll_id=scroll_id)
            except Exception:  # noqa: BLE001 — cleanup is best-effort
                pass


# Cache of the resolved sortable title field per (index, base_field). A title
# column is either ``text`` (sort on the ``.keyword`` sub-field; prod)
# or pure ``keyword`` (sort on the bare field; staging). The mapping doesn't
# change within a process, so probe once..
_SORTABLE_TITLE_FIELD_CACHE: dict[tuple[str, str], str] = {}


def _sortable_title_field(es, index_name, base_field):
    """Return the field ``get_all_documents`` should sort ``index_name`` on:
    ``<base_field>.keyword`` when the field is ``text`` (has a keyword
    sub-field), else the bare ``<base_field>`` when it is mapped pure
    ``keyword``. Probes the live mapping once per (index, field) and caches it.

    On any probe failure, fall back to the bare field — sorting on a bare
    keyword field always works; the only risk is an unsorted-but-correct result
    if the field were secretly text-only, which no asset title is.
    """
    cache_key = (index_name, base_field)
    cached = _SORTABLE_TITLE_FIELD_CACHE.get(cache_key)
    if cached is not None:
        return cached

    resolved = base_field
    try:
        mapping = es.indices.get_mapping(index=index_name)
        # ``index_name`` may resolve to a concrete index via alias; take the
        # first (and only) returned index's mappings.
        index_mappings = next(iter(mapping.values()), {})
        props = index_mappings.get("mappings", {}).get("properties", {})
        field_def = props.get(base_field, {}) or {}
        if field_def.get("type") == "text" and "keyword" in (field_def.get("fields", {}) or {}):
            resolved = f"{base_field}.keyword"
    except Exception as e:
        logging.info(
            "get_all_documents: mapping probe for %r on %r failed (%s); sorting on bare %r",
            base_field,
            index_name,
            type(e).__name__,
            base_field,
        )

    _SORTABLE_TITLE_FIELD_CACHE[cache_key] = resolved
    return resolved


def get_all_documents(index_name, query=None, size=1000):
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return []

    # If no query is provided, default to match_all
    if query is None:
        query = {"match_all": {}}

    # Define the sortable title field per index. History: sorted on the
    # ``.keyword`` sub-field (prod title columns were text+keyword);
    # added a try-``.keyword``-then-retry-bare fallback for the mixed
    # envs; replaced that runtime fallback with a cached mapping probe
    # (``_sortable_title_field``) that resolves the right field ONCE per index.
    # reindexed every de_asset_* index to explicit keyword, so today the
    # probe resolves the bare field on both envs — but it is KEPT deliberately:
    # if any of these three title columns is ever recreated as text+keyword (a
    # fresh auto-mapped index, an old-snapshot restore), the probe adapts and a
    # bare-field sort can't silently error into an empty result. The
    # fallback the ticket references is already gone; nothing to remove here.
    title_field = {
        "de_asset_teams": "team_title",
        "de_asset_countries": "country_title",
        "de_asset_cities": "city_title",
    }.get(index_name)
    sort_field = _sortable_title_field(es, index_name, title_field) if title_field else None

    def _search(field):
        body = {"query": query}
        if field:
            body["sort"] = [{field: {"order": "asc"}}]
        return es.search(index=index_name, size=size, body=body)

    try:
        result = _search(sort_field)
        hits = result.get("hits", {}).get("hits", [])
        _warn_if_size_cap_hit("get_all_documents", index_name, hits, size)
        return hits
    except Exception as e:
        logging.error(f"Error fetching documents from {index_name}: {e}")
        return []


def get_document_by_id(index_name, doc_id):
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return None

    try:
        return es.get(index=index_name, id=doc_id)["_source"]
    except NotFoundError:
        # A missing document is a normal, handled outcome — callers cope with
        # None. Logging it at ERROR (under whatever importer prefix is active)
        # turned every dangling/optional id reference into log noise across all
        # importers via the shared billing pre-check. DEBUG keeps it
        # available for diagnosis without masquerading as a failure.
        logging.debug(f"Document not found in {index_name} with id {doc_id}")
        return None
    except Exception as e:
        logging.error(f"Error fetching document from {index_name} with id {doc_id}: {e}")
        return None


def delete_document_by_id(index_name, doc_id):
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return False

    try:
        es.delete(index=index_name, id=doc_id, refresh="wait_for")
        return True
    except Exception as e:
        logging.error(f"Error deleting document from {index_name} with id {doc_id}: {e}")
        return False


def edit_document(index_name, doc_id, updated_document):
    from datetime import datetime

    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return False

    try:
        # Add @timestamp field if not present (required for data streams)
        if "@timestamp" not in updated_document:
            updated_document["@timestamp"] = datetime.utcnow().isoformat() + "Z"

        es.index(index=index_name, id=doc_id, document=updated_document, refresh="wait_for")
        return True
    except Exception as e:
        logging.error(f"Error updating document in {index_name} with id {doc_id}: {e}")
        return False


def put_regular_index_template(es, index_name, mapping, priority=500):
    """
    Create or update an index template so the named index is created as a regular
    index (not a data stream), overriding any lower-priority templates that specify
    data_stream (e.g. default_replicas_0_for_single_node).

    Call this before create_index_if_not_exists for any index whose name would
    otherwise match a data-stream-only template.
    """

    template_name = f"template_{index_name}"
    try:
        es.indices.put_index_template(
            name=template_name,
            body={
                "index_patterns": [index_name],
                "priority": priority,
                "template": {
                    "settings": {"number_of_replicas": 0},
                    "mappings": mapping,
                },
                # Omitting "data_stream" ensures this creates a regular index.
            },
        )
        logging.info(f"Put index template '{template_name}' for '{index_name}' (priority {priority})")
        return True
    except Exception as e:
        logging.error(f"Error putting index template for {index_name}: {e}")
        return False


def missing_mapping_fields(live_properties, mapping):
    """The ``properties`` sub-tree of ``mapping`` that ``live_properties`` lacks.

    One level of object/nested nesting is walked: a field that exists live but
    is missing some of its own ``properties`` yields just the missing children
    (with the parent's ``type`` repeated, which ES requires on put_mapping and
    accepts as a no-op when it matches). Live fields are never redefined.
    """
    live = live_properties or {}
    out = {}
    for name, spec in ((mapping or {}).get("properties") or {}).items():
        if name not in live:
            out[name] = spec
            continue
        children = (spec or {}).get("properties")
        if not children:
            continue
        live_children = (live.get(name) or {}).get("properties") or {}
        missing = {c: cs for c, cs in children.items() if c not in live_children}
        if missing:
            entry = {"properties": missing}
            if spec.get("type"):
                entry["type"] = spec["type"]
            out[name] = entry
    return out


def ensure_mapping_fields(es, index_name, mapping):
    """add-only ``put_mapping`` of the fields ``mapping`` declares
    that the live ``index_name`` does not (the sender pattern).

    :func:`create_index_if_not_exists` only ever CREATES; a field added to a
    module's MAPPING after the index exists on prod is otherwise dynamically
    mapped on the first doc that carries it — keyword as text+keyword, a
    ``yyyy-MM-dd`` string as ``date`` (after which a later "" on the same field
    is a ``mapper_parsing_exception``). Idempotent: a no-op when nothing is
    missing. Fail-soft: a mapping failure is logged and the caller decides
    whether writing under dynamic mapping is acceptable. Returns the set of
    top-level field names touched.
    """
    try:
        live = es.indices.get_mapping(index=index_name)
        props = {}
        for _idx, body in (live or {}).items():
            props.update(((body or {}).get("mappings") or {}).get("properties") or {})
        to_add = missing_mapping_fields(props, mapping)
        if not to_add:
            return set()
        es.indices.put_mapping(index=index_name, body={"properties": to_add})
        logging.info("es_helpers: mapped %d missing field(s) on %s: %s", len(to_add), index_name, sorted(to_add))
        return set(to_add)
    except Exception as exc:  # noqa: BLE001 — boundary: a mapping heal must not take the writer down
        logging.warning("es_helpers: put_mapping on %s failed: %s — continuing.", index_name, exc)
        return set()


def create_index_if_not_exists(es, index_name, mapping):
    """Create ``index_name`` with the explicit ``mapping`` if it's missing.

    Returns True when the index exists with a creation attempt honored,
    False on failure. A concurrent creator winning the race
    (``resource_already_exists_exception``) counts as success ONLY after the
    live mapping is verified against the schema — the race winner can be a
    concurrent BULK write auto-creating the index under DYNAMIC mapping (the
    exact drift), and blessing it would convert the failure mode
    into a green result. callers that go on to bulk-write MUST
    treat False as fatal — writing into a missing index auto-creates it
    under ES dynamic mapping (text + .keyword instead of the schema's bare
    keyword), which is exactly the drift that broke team_esid aggregations
    on prod.
    """
    if not es.indices.exists(index=index_name):
        try:
            es.indices.create(index=index_name, body={"mappings": mapping})
        except Exception as e:
            if "resource_already_exists_exception" in str(e).lower():
                # Lost a creation race — verify the live index carries the
                # schema's field types before blessing it. Fail closed if
                # the verification itself fails.
                return _live_mapping_matches(es, index_name, mapping)
            logging.error(f"Error creating index {index_name}: {e}")
            return False
    return True


def _live_mapping_matches(es, index_name, mapping):
    """True when the live index's top-level field types match ``mapping``.

    Only fields the schema declares with an explicit ``type`` are compared;
    extra live fields are fine. A field missing from the live mapping counts
    as a mismatch (a bulk-auto-created index starts empty and dynamic-maps
    strings as text — either way it is not the schema).
    """
    try:
        live = dict(es.indices.get_mapping(index=index_name))
        live_props = next(iter(live.values()))["mappings"].get("properties", {})
    except Exception as e:
        logging.error(f"could not verify mapping of raced index {index_name}: {e}")
        return False
    for field, spec in (mapping.get("properties") or {}).items():
        want = spec.get("type")
        if want is None:
            continue
        got = live_props.get(field, {}).get("type")
        if got != want:
            logging.error(
                f"raced index {index_name} maps {field} as {got!r} but the schema wants "
                f"{want!r} — dynamic-mapping drift; refusing to treat creation as successful"
            )
            return False
    return True


def document_exists(index_name, doc_id):
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return False

    try:
        return es.exists(index=index_name, id=doc_id)
    except Exception as e:
        logging.error(f"Error checking document existence in {index_name} with id {doc_id}: {e}")
        return False


def indexed_doc_present(index_name, doc_id=None, match_fields=None):
    """True only when an importer's data is actually in ES.

    The importers move a fetched JSON file from the active dir to the
    ``*_imported/`` archive AFTER a send step that is NOT gated on the send
    succeeding (``bulk(raise_on_error=False)`` reports success even on a
    partial/zero index). An interrupted or partial send can therefore leave a
    file archived-but-unindexed. The fetch steps then treat "file is in the
    archive" as proof "doc is in ES" and skip the re-fetch forever — silent,
    permanent data loss with no recovery on retry.

    Callers gate their skip-if-archived check on this helper so a stranded
    archived file re-imports on the next run. Two modes:

    * ``doc_id`` — for families whose ES ``_id`` is deterministic and cheap to
      reconstruct at fetch time (FB ad/page, Instagram). Direct ``exists``.
    * ``match_fields`` — a ``{field: value}`` dict, for families whose ``_id``
      is an MD5 of a composite string built only at send time (Google Ads,
      YouTube). Reconstructing that hash here would couple the fetch site to
      the sender's exact string format and break silently on drift; a
      term-match ``count`` query on the identifying fields is robust instead.

    FAILS SAFE toward re-fetching: a missing index, missing doc, empty key, or
    ANY ES error returns False, so the caller re-fetches rather than skipping.
    A spurious re-fetch is cheap and idempotent (senders upsert by _id); a
    spurious skip is permanent loss — so False is always the safe default.
    """
    if not doc_id and not match_fields:
        # Neither key supplied — cannot verify; re-fetch is the safe default.
        return False

    # Wrap the client build too: get_elasticsearch_client can raise (missing CA
    # cert / ES unreachable at build time), and a raise here would ABORT the
    # importer instead of degrading to a re-fetch — the opposite of the
    # fail-safe this helper exists to provide.
    try:
        es = get_elasticsearch_client()
        if es is None:
            return False

        if doc_id:
            return bool(es.exists(index=index_name, id=doc_id))

        must = [{"term": {field: value}} for field, value in match_fields.items()]
        resp = es.count(
            index=index_name,
            body={"query": {"bool": {"must": must}}},
            ignore_unavailable=True,
            allow_no_indices=True,
        )
        return (resp or {}).get("count", 0) > 0
    except Exception as e:  # noqa: BLE001 — boundary; fail safe to re-fetch
        logging.warning(
            "indexed_doc_present(%s, id=%s, fields=%s): %s",
            index_name,
            doc_id,
            match_fields,
            e,
        )
        return False


def is_alias(es, name):
    """Check if a name is an ES alias (vs a concrete index or nonexistent)."""
    try:
        return es.indices.exists_alias(name=name)
    except Exception:
        return False


def get_alias_backing_index(es, alias_name):
    """Return the concrete index behind an alias, or None if not an alias."""
    try:
        if not es.indices.exists_alias(name=alias_name):
            return None
        alias_info = es.indices.get_alias(name=alias_name)
        return list(alias_info.keys())[0]
    except Exception:
        return None


def merge_index_mapping_properties(mapping: dict) -> dict:
    """deep-union the `properties` of every index in a
    get_mapping response into one tree.

    The naive `if k not in merged: merged[k] = v` (first-wins on the TOP-LEVEL
    key only) donates the entire subtree of whichever index is iterated first
    and silently drops every other index's sub-fields. FB `adset_targeting`
    (and its `geo_locations` child) is a `dynamic: true` object whose shape
    differs per index — ~13 distinct `geo_locations` shapes across the ad
    accounts — so no single index carries all children. First-wins therefore
    hid `countries`, `places`, `regions`, `zips`, … from the Data Inventory
    , and made the dashboard-builder field picker a per-request coin
    flip for dynamic-object sub-fields like `adset_promoted_object.page_id` —
    get_mapping's index order is not stable, so the same /fields call offered
    the sub-fields on one run and hid them on the next.

    Recurse into nested object `properties` and union multi-field `fields`
    (so a `text` leaf that gains a `.keyword` sub-field in a later index is not
    left keyword-less). Leaf type conflicts keep the first-seen type (the same
    resolution the shallow merge already used); we only ever ADD, never
    overwrite, so this cannot regress an already-listed field.

    Note: a field that is a bare object (`"type": "object"`, never populated)
    in one index and a populated object (`properties`, no `type`) in another
    merges to a node carrying BOTH keys — flatteners must skip emitting the
    `object`-typed parent itself (the guard) while still recursing
    into its `properties`."""
    import copy

    merged: dict = {}

    def _union(dst: dict, src: dict) -> None:
        for key, meta in src.items():
            if not isinstance(meta, dict):
                continue
            existing = dst.get(key)
            if not isinstance(existing, dict):
                # first time we see this field/object — take a deep copy so a
                # later _union into its nested properties/fields never mutates
                # the ES-returned mapping we were handed.
                dst[key] = copy.deepcopy(meta)
                continue
            # already present: union nested object properties and multi-fields.
            if "properties" in meta or "properties" in existing:
                child = existing.setdefault("properties", {})
                _union(child, meta.get("properties", {}))
            if "fields" in meta:
                subs = existing.setdefault("fields", {})
                for sub_name, sub_meta in meta["fields"].items():
                    subs.setdefault(sub_name, sub_meta)

    for _idx, idx_data in mapping.items():
        _union(merged, idx_data.get("mappings", {}).get("properties", {}))
    return merged


def create_ga_metrics_index_for_team(team_esid):
    """
    Create GA metrics index for a team.
    Called when a team is assigned to a GA property.
    Uses centralized schema from schemas/ directory.

    Args:
        team_esid: The team's Elasticsearch ID

    Returns:
        bool: True if index exists or was created successfully
    """
    es = get_elasticsearch_client()
    if es is None:
        logging.error("Failed to connect to Elasticsearch.")
        return False

    index_name = f"{team_esid}_ga_metrics".lower()

    # Check if index already exists
    if es.indices.exists(index=index_name):
        logging.info(f"GA metrics index {index_name} already exists")
        return True

    # Load schema from JSON file (replaces inline schema definition)
    mapping = get_schema_for_index_type("ga_metrics")

    try:
        es.indices.create(
            index=index_name,
            body={"settings": {"number_of_replicas": 0}, "mappings": mapping},
        )
        logging.info(f"Created GA metrics index: {index_name}")
        return True
    except Exception as e:
        logging.error(f"Error creating GA metrics index {index_name}: {e}")
        return False


def sanitize_team_title(team_title):
    """Sanitize team_title for human-readable alias naming.

    Lowercase, replace spaces/hyphens with underscores, strip special chars,
    collapse consecutive underscores, trim leading/trailing underscores.

    This is the CANONICAL implementation. All importers / scripts must import
    from here. Historical duplicate copies in echo / chatwoot / migration /
    no_data_markers were retired.

    this function is now only used for *alias* generation. The
    underlying index name uses `build_data_index_name(team_esid, source)`,
    which is ESID-keyed and doesn't risk slug collisions. A non-Latin title
    that previously collapsed to ``"unknown"`` here still does — but the
    underlying index is keyed by ESID, so two non-Latin teams no longer
    silently share an index. Alias slug collisions inside an org are caught
     at team-create / team-edit time.

    Args:
        team_title: Raw team title string

    Returns:
        Sanitized lowercase string safe for ES index / alias names.
    """
    if not team_title:
        return "unknown"

    sanitized = team_title.lower().replace(" ", "_").replace("-", "_")
    sanitized = re.sub(r"[^a-z0-9_]", "_", sanitized)
    while "__" in sanitized:
        sanitized = sanitized.replace("__", "_")
    sanitized = sanitized.strip("_")
    return sanitized if sanitized else "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# ESID-keyed data index naming.
#
# Tenant data indices are keyed by `{team_esid}__{source}` (e.g.
# `qLm7B__fb_insights`). Team ESIDs are stable, unique cluster-wide, and
# survive title renames. The team is the unit of data ownership; orgs are
# viewers.
#
# Re-scope (2026-06-01, Decision A): there are NO forward aliases. Index
# names are ESID-only. Human-readable titles surface via (1) the DEAM UI,
# which reads `de_asset_*` docs directly, and (2) the `list-tenant-indices.sh`
# ops CLI. The only aliases that exist are REVERSE aliases for back-compat
# (old `kavanah_media_*` name → new ESID index), created exclusively by the
# migration script (`migrate_to_esid_indices.py`) so existing dashboard
# wildcards keep matching. Reverse-alias cleanup is tracked.
# ─────────────────────────────────────────────────────────────────────────────


# Phase B: structural index-name-level tenant isolation.
#
# The canonical set of importer sources. Every `{team_esid}__{source}` data
# index ends in one of these slugs. Mirrors the data-view list in
# deployment/create-data-views.sh — keep both in sync when a new importer
# lands. The startup-time assertion in scoped_index_pattern would surface
# drift, but the cheap audit is: this constant is the single source of truth
# and every importer's SOURCE_SLUG must appear here.
#
# Why a constant rather than introspection: the live importer registry is
# loaded lazily and per-process; a Flask request that needs to compute an
# index allowlist can't pay that import cost. The constant is read at module
# import time and the assertion runs on the first scoped_index_pattern call.
KNOWN_SOURCES = (
    "fb_insights",
    # deduplicated window-level IG reach ({team_esid}__ig_insights_window).
    "ig_insights_window",
    # deduplicated window-level FB metrics ({team_esid}__fb_insights_window).
    # Holds non-additive metrics (reach, frequency, cpp, estimated_ad_recallers, ...)
    # that FB only computes correctly over a whole window in one call — a per-day
    # sum over-counts them. Produced by the (repurposed) windowed FB importer.
    "fb_insights_window",
    "fb_page_insights",
    "fb_page_posts",
    "ga_metrics",
    # deduplicated window-level GA4 metrics ({team_esid}__ga_metrics_window).
    # Property-level (no breakdown dims) so activeUsers/totalUsers are GA4's true
    # deduplicated unique-user counts for the window — a per-day × per-dimension sum
    # over-counts them ~3-10x. Produced by get_ga_metrics_window.py. Mirrors the FB
    # fb_insights_window pattern.
    "ga_metrics_window",
    "ig_insights",
    "google_ads",
    "youtube_analytics",
    "echo_conversations",
    # follow-up (2026-05-30): echo_user_stats is a per-tenant index
    # ({subdomain}_echo_user_stats, send_user_stats_to_elastic.py) with a
    # schema + a *_echo_user_stats template pattern, but was missing here —
    # so a scoped user's Layer-1 allowlist rewrote it to the empty sentinel
    # and Echo user-stats panels returned 0 hits. Surfaced by the
    # SCHEMA_MAP⊆KNOWN_SOURCES guard added with the fb_page_posts fix.
    "echo_user_stats",
    "chatwoot_conversations",
    "chatwoot_insights",
    # Bitly link-clicks importer ({team_esid}__bitly_clicks).
    "bitly_clicks",
    # TikTok organic daily snapshots ({team_esid}__tiktok_organic) —
    # account_daily + video_daily record types in one index.
    "tiktok_organic",
    # monthly per-team conversation rollups
    # ({team_esid}__conversation_rollups), written by the retention sweep
    # before it deletes raw conversations older than 24 months, so the Summary
    # Report's all-time tiles keep their history. Registered here so the
    # Layer-1 allowlist scopes it like any other per-team source.
    "conversation_rollups",
    # team-keyed Disciple Tools indices (`{team}_disciple_tools`).
    # Distinct from the cross-tenant `disciple-tools` alias
    # — this slug lets per-team DT indices participate in the Layer-1 allowlist
    # like any other source, so a team-scoped user can read their own DT slice.
    "disciple_tools",
)


# A wildcard pattern that matches no ES index. Returned by
# `scoped_index_pattern` when the user's allowed_indices is empty OR the
# intersection with a request's generic pattern is empty. Wildcard rather
# than a concrete name (e.g. `__none__`) so ES returns 0 hits silently
# instead of raising index_not_found_exception — call sites don't need
# ignore_unavailable=True.
_EMPTY_SCOPE_PATTERN = "*__empty_scope__"


def scoped_index_pattern(generic_pattern, allowed_indices):
    """Intersect a wildcard / comma-list index pattern with a user's allowed
    index allowlist. Returns the explicit comma-list of indices the caller
    may query.

    This is Layer 1 of the Phase B two-layer tenant-isolation model:
    structural enforcement at the index-name level. Layer 2
    (RoleFilterService.intersect_body_filters / append_scope_filter) remains
    unchanged — a leak now requires BOTH layers to fail simultaneously.

    Args:
        generic_pattern: A wildcard pattern (``"*google_ads"``), a wildcard
            with exclusions (``"*echo_conversations,-unknown_echo_conversations"``),
            a single concrete index name (``"qLm7B__fb_insights"``), or a
            comma-list of either form. Exclusion entries (leading ``-``) pass
            through unchanged so the request's negative filters survive.
        allowed_indices: A set / frozenset / iterable of concrete index names
            the current user may read. Empty iterable means "deny everything"
            and produces the no-match sentinel.

    Returns:
        A comma-separated list of explicit index names (and any exclusion
        entries from the input). Returns the no-match wildcard sentinel when
        the intersection is empty.

    Examples:
        >>> scoped_index_pattern("*google_ads", {"a__google_ads", "b__fb_insights"})
        'a__google_ads'
        >>> scoped_index_pattern("*fb_insights", set())
        '*__empty_scope__'
        >>> scoped_index_pattern("a__fb_insights", {"a__fb_insights"})
        'a__fb_insights'
        >>> scoped_index_pattern("a__fb_insights", {"b__fb_insights"})
        '*__empty_scope__'
    """
    allowed = set(allowed_indices) if allowed_indices else set()
    if not allowed:
        return _EMPTY_SCOPE_PATTERN

    # Split the input into entries (handle the comma-list shape used by
    # echo_conversations queries that exclude `-unknown_echo_conversations`).
    entries = [e.strip() for e in (generic_pattern or "").split(",") if e.strip()]
    if not entries:
        return _EMPTY_SCOPE_PATTERN

    keep = []
    matched_any_positive = False
    for entry in entries:
        if entry.startswith("-"):
            # Exclusion clause — pass through unchanged. ES applies it after
            # the positive includes; intersecting it against the allowlist
            # would weaken the user's filter, not strengthen it.
            keep.append(entry)
            continue
        if entry.startswith("*"):
            suffix = entry[1:]
            matches = [idx for idx in allowed if idx.endswith(suffix)]
            if matches:
                matched_any_positive = True
                keep.extend(sorted(matches))
            continue
        # Concrete index name — keep only if the user is allowed to read it.
        if entry in allowed:
            matched_any_positive = True
            keep.append(entry)

    if not matched_any_positive:
        return _EMPTY_SCOPE_PATTERN
    return ",".join(keep)


def build_data_index_name(team_esid, source):
    """Return the canonical ES index name for a (team, source) pair.

    Format: ``{team_esid}__{source}`` (lowercase). Double underscore is the
    separator so a team_esid never collides with the source token even if
    the source contains a single underscore.

    Args:
        team_esid: The team's ES document ID. MUST be a non-empty string.
        source: Source slug (e.g. ``"fb_insights"``, ``"ga_metrics"``).
            MUST be a non-empty string.

    Returns:
        Lowercase index name.

    Raises:
        ValueError: if either argument is empty / None / whitespace-only, OR
            if ``team_esid`` starts with ``_``/``-``/``+``. No silent
            ``"unknown_*"`` fallback — a missing or malformed ESID is a bug,
            not a data condition.
    """
    if not team_esid or not str(team_esid).strip():
        raise ValueError("build_data_index_name: team_esid is required")
    if not source or not str(source).strip():
        raise ValueError("build_data_index_name: source is required")
    # ES rejects index names starting with _, -, or +. An auto-generated
    # team _id can land one of these as its leading char (ES base64 _id alphabet),
    # which produces an invalid index name and a cryptic 400 at query time. Fail
    # loudly HERE, naming the offending esid so the operator can locate + fix the
    # team doc, instead of letting ES surface "must not start with '_'" downstream.
    esid = str(team_esid).strip()
    if esid[0] in ("_", "-", "+"):
        raise ValueError(
            f"build_data_index_name: team_esid {esid!r} starts with an illegal "
            f"leading char ({esid[0]!r}); ES index names cannot begin with _/-/+. "
            "This team doc needs a new ES-safe _id."
        )
    return f"{esid}__{str(source).strip()}".lower()


def team_data_index_names(team_esids):
    """every ``{team_esid}__{source}`` index name (lowercased, as
    ``build_data_index_name`` writes them) for the given teams across
    ``KNOWN_SOURCES`` — the Layer-1 allowlist for a scope.

    Built from team ESIDs only. Team TITLES repeat across tenants (two
    signups can both name a team "Team 1"), so no allowlist may ever be
    derived from a title. Blank ESIDs are skipped; no teams → an empty
    frozenset, which every caller treats as deny-all.
    """
    esids = {str(e).strip() for e in (team_esids or []) if e and str(e).strip()}
    return frozenset(f"{esid}__{source}".lower() for esid in esids for source in KNOWN_SOURCES)


def generate_es_safe_doc_id(nbytes=12):
    """Return a random URL-safe doc id whose LEADING char is alphanumeric.

    any ES doc whose ``_id`` is later concatenated into an index name
    (e.g. ``de_asset_teams`` ids -> ``{team_esid}__{source}`` via
    ``build_data_index_name``) must not start with ``_``/``-``/``+`` — ES rejects
    such index names. ES's own auto-generated ids use a base64 alphabet that
    includes those chars and can place one first, so callers that create such a
    doc MUST pass an explicit id from this helper instead of relying on ES
    auto-id.

    ``secrets.token_urlsafe`` draws from ``[A-Za-z0-9_-]``; we reroll until the
    first character is alphanumeric so the resulting id is always a legal ES
    index-name prefix. The body may still contain ``_``/``-`` (legal mid-name).
    """
    while True:
        candidate = secrets.token_urlsafe(nbytes)
        if candidate and candidate[0].isalnum():
            return candidate


def ensure_data_index(es, team_esid, source, mapping):
    """Create the ``{team_esid}__{source}`` index if it doesn't exist.

    This is the importer-facing single entry point. Callers pass the
    team ESID, source slug, and ES mapping; the function builds the
    canonical index name and creates the index with the supplied mapping
    if it's missing.

     (re-scope 2026-06-01, Decision A): forward aliases are gone.
    This used to be ``ensure_data_index_with_aliases`` and refreshed a
    per-(org, team, source) forward-alias set on every call. That whole
    path is removed — importers create the ESID-keyed index and nothing
    else. The only aliases that exist are reverse aliases created by the
    migration script for dashboard back-compat.

    Returns the index name. Raises ValueError on bad inputs (no silent
    fallback).
    """
    index_name = build_data_index_name(team_esid, source)
    # fail LOUDLY when creation fails. Every caller of this
    # function bulk-writes next; a bulk into a missing index auto-creates it
    # under ES dynamic mapping (team_esid as text + .keyword instead of the
    # schema's bare keyword), permanently breaking aggregations on that
    # index. A raised run retries on the next import cycle; a drifted index
    # needs a manual reindex to repair.
    if not create_index_if_not_exists(es, index_name, mapping):
        raise RuntimeError(
            f"failed to create data index {index_name} with its schema mapping; "
            "refusing to continue — a bulk write now would dynamic-map the index."
        )
    return index_name


# Sentinel placed in ``by_title`` when two or more teams share the same
# sanitized title. ``resolve_team_esid`` treats this as "ambiguous, drop
# the doc" rather than mis-routing to whichever team happened to win the
# dict-write race..
_TITLE_COLLISION = object()


def build_team_lookup_cache(es, team_filter=None):
    """Build a one-shot per-import-run lookup cache from ``de_asset_teams``.

    Importers historically key off ``team_title`` (Echo, Chatwoot) or
    pre-resolved ``team_esid`` written into the JSON doc (FB, IG, GA, YT,
    Google Ads). After, every importer needs both ``team_esid``
    (for the index name) and the team's org list (for alias creation).
    A single ES round-trip at the start of the run is much cheaper than
    one per doc.

    when two teams in different workspaces share the same
    sanitized ``team_title``, the title-keyed lookup is *ambiguous* and
    silently mis-routes docs to whichever team happened to win the
    dict-write race. Workspaces are designed to allow duplicate
    ``team_title`` across tenants, so this is a structural cross-workspace
    data-visibility risk. The cache now detects collisions during build,
    logs a WARNING listing every colliding team's (esid, workspace_esid),
    and replaces the ``by_title`` entry with the ``_TITLE_COLLISION``
    sentinel so ``resolve_team_esid`` returns ``None`` for that slug
    (caller drops the doc, same path as the existing unresolved-team
    handling). The ``by_esid`` view is unaffected — ESID-first callers
    (FB daily) never hit the title fallback.

    Args:
        es: Elasticsearch client.
        team_filter: Optional dict of filters for ``de_asset_teams`` query.
            Mirrors the convention used by ``import_scripts/facebook``'s
            ``get_fb_teams(filters=...)`` (e.g. ``{"ds_facebook": "true"}``).
            None = all teams.

    Returns:
        Dict with two views::

            {
                "by_esid": {team_esid: {"team_title": ..., "org_esids": [...]}, ...},
                "by_title": {sanitized_title: team_esid | _TITLE_COLLISION, ...},
            }

        ``by_title`` keys by *sanitized* title. Values are either a
        team_esid (unambiguous resolution) or ``_TITLE_COLLISION`` when
        two or more teams share the same slug.
    """
    by_esid = {}
    by_title = {}
    # Track collisions for the post-pass WARNING. Keyed by sanitized slug;
    # each value is the list of (team_esid, workspace_esid) tuples that
    # share it. workspace_esid is kept local rather than added to the
    # ``by_esid`` view so the doc-shape contract stays intact.
    title_collisions = {}
    workspace_by_esid = {}

    query = {"match_all": {}} if not team_filter else {"bool": {"must": [{"term": {k: v}} for k, v in team_filter.items()]}}

    try:
        resp = es.search(
            index="de_asset_teams",
            size=10000,
            body={"query": query},
        )
    except Exception as e:
        logging.warning("build_team_lookup_cache: de_asset_teams query failed: %s", e)
        return {"by_esid": {}, "by_title": {}}

    for hit in resp.get("hits", {}).get("hits", []):
        team_esid = hit.get("_id")
        team = hit.get("_source", {}) or {}
        team_title = team.get("team_title", "")
        org_esids = team.get("organization_esid", [])
        if isinstance(org_esids, str):
            org_esids = [org_esids]
        workspace_esid = team.get("workspace_esid", "")
        workspace_by_esid[team_esid] = workspace_esid
        by_esid[team_esid] = {
            "team_title": team_title,
            "org_esids": list(org_esids),
        }
        if not team_title:
            continue
        slug = sanitize_team_title(team_title)
        existing = by_title.get(slug)
        if existing is None:
            by_title[slug] = team_esid
        else:
            # Already-known slug. Two cases:
            #   1. existing is a real esid (first collision)  → seed the
            #      tracker with both entries, replace value with sentinel.
            #   2. existing is the sentinel (third or later collision) →
            #      append to the tracker.
            if existing is not _TITLE_COLLISION:
                title_collisions[slug] = [(existing, workspace_by_esid.get(existing, ""))]
                by_title[slug] = _TITLE_COLLISION
            title_collisions[slug].append((team_esid, workspace_esid))

    for slug, members in title_collisions.items():
        logging.warning(
            "build_team_lookup_cache: ambiguous team_title slug %r resolves to "
            "%d teams across different workspaces — title-fallback lookups for "
            "this slug will return None. Members: %s",
            slug,
            len(members),
            ", ".join(f"(team_esid={e}, workspace_esid={w!r})" for e, w in members),
        )

    return {"by_esid": by_esid, "by_title": by_title}


class SlugCollisionError(Exception):
    """Raised when a proposed title would collide on the sanitized
    alias slug with another team / org in the same scope.

    For TEAM collisions: ``colliding_org_esids`` is the list of org
    ESIDs where another team with the same sanitized slug exists, and
    ``existing_team_esid`` names the team that already owns the slug.

    For ORG collisions: ``colliding_org_esids`` is empty and
    ``existing_org_esid`` names the org that already owns the slug.

    Carries ``slug`` so the caller (web route / CLI handler /
    onboarding service) can surface a precise error message.
    """

    def __init__(self, slug, *, colliding_org_esids=None, existing_team_esid=None, existing_org_esid=None):
        self.slug = slug
        self.colliding_org_esids = list(colliding_org_esids or [])
        self.existing_team_esid = existing_team_esid
        self.existing_org_esid = existing_org_esid
        if self.colliding_org_esids:
            detail = f"in org(s) {self.colliding_org_esids!r}"
        elif existing_org_esid:
            detail = f"by org {existing_org_esid!r}"
        else:
            detail = "(scope unknown)"
        super().__init__(f"slug {slug!r} already used {detail}")


# Backwards-compatible alias — earlier commit (8dedef28995) used
# this name. Kept so external imports keep working through the rename.
TeamSlugCollisionError = SlugCollisionError


def check_team_slug_unique(es, *, team_title, org_esids, exclude_team_esid=None):
    """Raise ``SlugCollisionError`` if creating/editing a team with
    ``team_title`` in any of ``org_esids`` would collide on the sanitized
    alias slug with another team in that org.

    alias slugs (`{org_slug}__{team_slug}__{source}`) must be
    unique within each org the team belongs to. Two teams with titles
    "General Marketing" and "general_marketing" produce the same slug
    and would collide on alias creation.

    Args:
        es: Elasticsearch client.
        team_title: Proposed team title.
        org_esids: List of org ESIDs the team would belong to. Empty
            list = no check (the team has no orgs, so no aliases, so no
            collision possible).
        exclude_team_esid: When editing, exclude this team from the
            collision check (so editing a team without changing its
            title doesn't reject the no-op).

    Raises:
        SlugCollisionError: with the colliding slug + org_esids.
    """
    if not team_title:
        return  # empty title is caught upstream; no slug to compare
    if not org_esids:
        return  # orphan team — no aliases, no collisions

    proposed_slug = sanitize_team_title(team_title)
    if not proposed_slug or proposed_slug == "unknown":
        return  # unresolvable sanitization, caller decides whether to reject

    colliding_org_esids = []
    existing_team_esid = None

    for org_esid in org_esids:
        try:
            resp = es.search(
                index="de_asset_teams",
                size=1000,
                body={
                    # dual-term — a.keyword-only match found no
                    # existing teams on bare-keyword prod, so the slug-collision
                    # guard passed teams that actually collide, minting dup slugs.
                    "query": dual_term("organization_esid", org_esid),
                    "_source": ["team_title"],
                },
            )
        except Exception as e:
            logging.warning("check_team_slug_unique: team scan for org %s failed: %s", org_esid, e)
            continue

        for hit in resp.get("hits", {}).get("hits", []):
            other_esid = hit.get("_id")
            if exclude_team_esid and other_esid == exclude_team_esid:
                continue
            other_title = hit.get("_source", {}).get("team_title", "")
            if sanitize_team_title(other_title) == proposed_slug:
                colliding_org_esids.append(org_esid)
                existing_team_esid = other_esid
                break  # one collision per org is enough

    if colliding_org_esids:
        raise SlugCollisionError(
            proposed_slug,
            colliding_org_esids=colliding_org_esids,
            existing_team_esid=existing_team_esid,
        )


def check_org_slug_unique(es, *, organization_title, workspace_esid=None, exclude_org_esid=None):
    """Raise ``SlugCollisionError`` if creating/editing an org with
    ``organization_title`` would collide on the sanitized alias slug
    with another org in the same scope.

    per-tier scoping.

    * When ``workspace_esid`` is supplied (the F5+ canonical call path),
      the collision check is scoped to orgs in that workspace. Two
      unrelated workspaces may both contain an org titled "Acme" — the
      cross-tenant ARA failure case the spike was triggered by.
    * When ``workspace_esid`` is None (back-compat for callers not yet
      retargeted by F5), the check falls back to a cluster-wide scan.
      This branch is the source of the cross-tenant disclosure problem;
      kept only so the rescope can roll out incrementally.

    Args:
        es: Elasticsearch client.
        organization_title: Proposed organization title.
        workspace_esid: Parent workspace ESID. Scopes the collision
            check to orgs whose ``workspace_esid`` field equals this
            value. F5 web routes pass the current user's workspace.
        exclude_org_esid: When editing, exclude this org from the
            collision check (so editing without changing the title /
            slug is a no-op).

    Raises:
        SlugCollisionError: with the colliding slug + existing_org_esid.
    """
    if not organization_title:
        return  # empty title is caught upstream

    proposed_slug = sanitize_team_title(organization_title)
    if not proposed_slug or proposed_slug == "unknown":
        return  # unresolvable sanitization, caller decides whether to reject

    # Build the query: workspace-scoped term filter when workspace_esid
    # is supplied, else global match_all (back-compat path).
    if workspace_esid:
        query = {"term": {"workspace_esid": workspace_esid}}
    else:
        query = {"match_all": {}}

    try:
        resp = es.search(
            index="de_asset_organization",
            size=10000,
            body={"query": query, "_source": ["organization_title"]},
        )
    except Exception as e:
        logging.warning("check_org_slug_unique: org scan failed: %s", e)
        return  # degrade-to-no-check: an admin will see the warning in logs

    for hit in resp.get("hits", {}).get("hits", []):
        other_esid = hit.get("_id")
        if exclude_org_esid and other_esid == exclude_org_esid:
            continue
        other_title = hit.get("_source", {}).get("organization_title", "")
        if sanitize_team_title(other_title) == proposed_slug:
            raise SlugCollisionError(proposed_slug, existing_org_esid=other_esid)


def resolve_creator_workspace_esid(es, username):
    """return the workspace ESID a NEW asset doc should be tagged with.

    Reads the creator's primary workspace via
    ``WorkspaceMembershipService.get_primary_workspace(username)`` so that
    when a user in workspace B creates an org / Chatwoot account / FB
    account / etc., the new doc lands in workspace B — NOT in the
    singleton workspace (which was the earlier default and caused
    cross-tenant leak symptoms during the 2026-05-18 smoke-test).

    Fallback chain (in order):

    1. ``WorkspaceMembershipService.resolve_curation_workspace(username)``
        — the shared resolver: is_primary workspace role →
       workspace-scope data grant → ORG-OWNER org-walk →
       positional ``get_primary_workspace``. This is the right answer for
       any user who completed onboarding under F7, had a
       membership doc written auto-membership branch, OR is
       an org owner whose org points at a workspace but who holds no
       workspace role/grant (the case the bare ``get_primary_workspace``
       missed — it returned None and dropped to the singleton, stamping
       new assets into an arbitrary first workspace).
    2. ``resolve_default_workspace_esid(es)`` — the singleton helper.
       Only fires when the creator has no membership doc at all (a
       user mid-onboarding, or a pre-F7 account that hasn't been
       backfilled). Logged at WARNING level so an operator can spot
       the gap.
    3. ``None`` — both lookups failed. Caller treats this the same way
       it treated ``resolve_default_workspace_esid``'s ``None`` return:
       the new doc is created without ``workspace_esid``, and a later
       backfill or admin tool stamps it.

    Returns ``None`` only when both lookups fail. ES outage in either
    leg falls through to the next fallback.
    """
    if username:
        try:
            try:
                from web.services.workspace_membership_service import (
                    WorkspaceMembershipService,
                )
            except ImportError:
                from asset_management.web.services.workspace_membership_service import (
                    WorkspaceMembershipService,
                )
            # resolve via the shared ``resolve_curation_workspace``
            # chain (is_primary role → workspace data grant → ORG-OWNER
            # org-walk → positional ``get_primary_workspace`` fallback), NOT
            # bare ``get_primary_workspace``. The positional leg preserves the
            # single-workspace owner/member case exactly; the org-owner walk
            # leg is what this fixes: an org owner holds an org role
            # but no workspace role/grant, so bare ``get_primary_workspace``
            # returned None and we fell through to the singleton — stamping a
            # new team/org/asset into an arbitrary first workspace (Control
            # Workspace on prod), where the read-side ``get_allowed_workspaces``
            # inferred-walk then filtered it out (created-but-invisible, plus a
            # false org-scoped slug collision on retry). Same gap
            # fixed for Data Inventory for template install;
            # this is the third caller of the broken resolver.
            primary = WorkspaceMembershipService.resolve_curation_workspace(username)
            if primary:
                return primary
        except Exception as e:
            logging.warning(
                "resolve_creator_workspace_esid(%s): primary lookup failed; falling back to singleton: %s",
                username,
                e,
            )

    logging.warning(
        "resolve_creator_workspace_esid(%s): no primary workspace; falling back to singleton (asset will land in the default workspace)",
        username or "<empty>",
    )
    return resolve_default_workspace_esid(es)


def resolve_default_workspace_esid(es):
    """Return the ESID of the singleton workspace, or ``None`` if missing.

     / F5 transitional helper. Pre-F8 the codebase has no
    WorkspaceMembershipService, so org-create/edit needs a way to learn
    which workspace a new org belongs to. During the beta state every
    tenant owned by Kavanah belongs to the singleton workspace seeded
    by F4 (tools/bootstrap_workspaces.py).

    Implementation: search ``de_asset_workspace`` for any doc and return
    the first hit's ESID. Beta state has exactly one workspace; the
    search returns it deterministically. Post-F8 this helper is
    replaced by a per-user workspace resolver that reads
    ``de_authz_workspace_membership``.

    Returns ``None`` on ES error or empty index — callers degrade to
    the back-compat "no workspace scoping" path so org create still
    succeeds while the F5 backfill is being applied.
    """
    try:
        resp = es.search(
            index="de_asset_workspace",
            body={"query": {"match_all": {}}, "size": 2, "_source": False},
        )
    except Exception as e:
        logging.warning("resolve_default_workspace_esid: workspace lookup failed: %s", e)
        return None

    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        return None
    if len(hits) > 1:
        # Beta state should have exactly one workspace. More than one
        # means F8 has shipped multi-workspace-per-user but this helper
        # hasn't been retired. Log a clear marker so an operator can
        # find this in the access log when they triage the wrong-
        # workspace symptom.
        logging.warning(
            "resolve_default_workspace_esid: %d workspaces in de_asset_workspace; "
            "retiring this helper is overdue",
            len(hits),
        )
    return hits[0]["_id"]


def resolve_team_esid(lookup_cache, team_esid=None, team_title=None):
    """Resolve a team ESID from whichever identifier the importer has.

    Importers that already write ``team_esid`` into their JSON pass it
    through directly. Importers that only have ``team_title`` (Echo,
    Chatwoot) join through the cache.

    Returns ``None`` if neither identifier resolves — caller decides
    whether to raise or skip the doc. We don't raise here because import
    runs commonly have a mix of resolvable and orphan docs (test data,
    legacy team_titles that no longer exist), and a bulk run shouldn't
    abort on the first orphan.

    when ``team_title`` is provided and resolves to a sanitized
    slug shared by teams in different workspaces, the lookup is ambiguous
    and we return ``None`` (caller drops doc). The build-time WARNING in
    ``build_team_lookup_cache`` names the colliding ESIDs so an operator
    can disambiguate.
    """
    if team_esid and team_esid in lookup_cache.get("by_esid", {}):
        return team_esid
    if team_title:
        sanitized = sanitize_team_title(team_title)
        resolved = lookup_cache.get("by_title", {}).get(sanitized)
        if resolved is _TITLE_COLLISION:
            return None
        return resolved
    return None

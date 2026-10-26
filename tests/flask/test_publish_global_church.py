"""Publish to Global.Church dialog (``/publish-global-church/<org_esid>``).

Every test runs the real blueprint against the synthetic Riverside Media
workspace from ``tests/jobs/support.py`` through the fake Elasticsearch, with
``AssetService`` reads and writes backed by the same fake, so the dialog's
preview and the published file are computed by the export job's own code.
The gateway call is the one thing mocked.
"""

from __future__ import annotations

import hashlib
import time
from unittest import mock

import pytest

from asset_management.jobs import export_release_product as job
from asset_management.web.blueprints.publish_global_church import routes
from asset_management.web.services.permissions_enum import Permission
from tests.conftest import CSRF_TEST_TOKEN, CsrfTestClient
from tests.jobs import support as s

R = "asset_management.web.blueprints.publish_global_church.routes"
URL = f"/publish-global-church/{s.ORG_ESID}"
API_KEY = "kv_test_partner_key"
# the shape of a Fernet token; ``decrypt`` is patched to return API_KEY
API_KEY_TOKEN = "gAAAAA" + "B" * 100
GATEWAY = "https://api.example.org"


@pytest.fixture(autouse=True)
def _test_gateway_host_is_allowed(monkeypatch):
    """The fixture's gateway is on the allowlist; anything else is refused."""
    monkeypatch.setenv(routes.GATEWAY_HOSTS_ENV, "api.example.org")


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """The dialog and the job sit behind GLOBAL_CHURCH_PUBLISH (off by default;)."""
    monkeypatch.setattr(job, "publish_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _scoped_admin_passes_the_global_route_gate():
    """``check_route_authorization`` lets a scoped admin through to a route's own
    gate when they hold any workspace-tier permission anywhere (resolved from
    ES, absent here). Grant WORKSPACE_MANAGE — what a workspace_admin and a
    workspace_owner both hold — so the dialog's own owner gate is what decides."""
    with mock.patch(
        "asset_management.web.services.permission_resolver.PermissionResolver.effective_permissions_anywhere",
        return_value=frozenset({Permission.WORKSPACE_MANAGE}),
    ):
        yield


@pytest.fixture
def es():
    return s.riverside_media_workspace()


@pytest.fixture
def store(es):
    """The blueprint's Elasticsearch and AssetService backed by the fixture."""

    def get_by_id(index, doc_id):
        try:
            return dict(es.get(index=index, id=doc_id)["_source"])
        except s.NotFound:
            return None

    def update(index, doc_id, doc):
        docs = es.docs[index]
        for i, (did, _) in enumerate(docs):
            if did == doc_id:
                docs[i] = (did, dict(doc))
                return True
        return False

    update(job.ORG_INDEX, s.ORG_ESID, {**get_by_id(job.ORG_INDEX, s.ORG_ESID), "gc_partner_api_key": API_KEY_TOKEN})
    with (
        mock.patch(f"{R}.get_elasticsearch_client", return_value=es),
        mock.patch(f"{R}.AssetService.get_by_id", side_effect=get_by_id),
        mock.patch(f"{R}.AssetService.update", side_effect=update) as upd,
        mock.patch(f"{R}.decrypt", return_value=API_KEY),
    ):
        yield es, upd


def _org(es):
    return es.get(index=job.ORG_INDEX, id=s.ORG_ESID)["_source"]


def _client(app, roles, username="owneruser"):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["authenticated"] = True
        sess["username"] = username
        sess["roles"] = roles
        sess["is_admin"] = False
        sess["full_name"] = "Owner User"
        sess["email"] = "owner@example.com"
        sess["auth_timestamp"] = time.time()
        sess["csrf_token"] = CSRF_TEST_TOKEN
    return CsrfTestClient(c)


@pytest.fixture
def owner(app):
    with mock.patch(f"{R}.PermissionResolver.has_permission_in_workspace", return_value=True) as gate:
        yield _client(app, ["workspace_owner"]), gate


def _gateway(status=202, body=None):
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = {"releaseId": "rel-0001", "manifestHash": "sha256:" + "ab" * 32} if body is None else body
    return mock.patch(f"{R}.requests.post", return_value=resp)


def _publish_form(**overrides):
    form = {
        "teams": [s.TEAM_PASHTO, s.TEAM_URDU],
        "countries": s.PUBLISHED_COUNTRIES,
        "named": "no",
        "display_name": "",
        "accept": "yes",
    }
    form.update(overrides)
    return form


# ── Entitlement ───────────────────────────────────────────────────────────────


def test_non_owner_gets_403_and_the_gate_is_checked_on_the_organisations_workspace(app, store):
    es, upd = store
    with mock.patch(f"{R}.PermissionResolver.has_permission_in_workspace", return_value=False) as gate:
        c = _client(app, ["workspace_admin"], username="adminuser")
        assert c.get(URL).status_code == 403
        assert c.post(URL, data=_publish_form()).status_code == 403
    gate.assert_called_with("adminuser", "riverside-media-ws", Permission.WORKSPACE_DELETE)
    upd.assert_not_called()
    assert "gc_last_release_id" not in _org(es)
    # an unknown organisation is a 404, not a 403, even for a superuser
    assert _client(app, ["superuser"]).get("/publish-global-church/no-such-org").status_code == 404


# ── GET ───────────────────────────────────────────────────────────────────────


def test_get_renders_teams_countries_preview_counts_and_the_agreement_hash(app, store, owner):
    es, _ = store
    client, gate = owner
    resp = client.get(URL)
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    gate.assert_called_with("owneruser", "riverside-media-ws", Permission.WORKSPACE_DELETE)
    for title in s.TEAMS.values():
        assert title in page
    for name in s.COUNTRY_NAMES.values():  # every country present in the data, withheld ones included
        assert name in page
    assert "Elsewhere" not in page  # the other organisation's team
    # the preview is the job's own counting: per-country totals equal the release rows summed
    release, _ = job.build_release(
        es, org_esid=s.ORG_ESID, team_esids=list(s.TEAMS), countries=s.ALL_COUNTRIES, now=s.NOW, test_data=True
    )
    preview = routes._preview(es, s.ORG_ESID, list(s.TEAMS), s.ALL_COUNTRIES, s.NOW)
    totals: dict[str, dict[str, int]] = {}
    for row in release["rows"]:
        acc = totals.setdefault(row["countryIso2"], {"exposed": 0, "interactions": 0, "touchpoints": 0, "active": 0})
        for k in acc:
            acc[k] += row[k]
    assert preview["per_country"] == totals
    assert preview["cells"] == len(release["rows"])
    assert preview["unattributed_exposed"] == release["manifest"]["unattributed"]["exposed"]
    assert f"{totals['PK']['touchpoints']:,}" in page
    assert "could not be placed in a country" in page
    # the agreement: hashed from the file at request time, rendered, shown
    with open(routes.AGREEMENT_FILE, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    assert sha == s.AGREEMENT_SHA256
    assert sha in page
    assert "Data Sharing Agreement" in page
    assert "thin cover" in page
    assert 'name="accept"' in page


# ── POST ──────────────────────────────────────────────────────────────────────


def test_post_without_acceptance_is_400_and_writes_nothing(app, store, owner):
    es, upd = store
    client, _ = owner
    before = dict(_org(es))
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form(accept=""))
    assert resp.status_code == 400
    assert "Tick the acceptance box" in resp.get_data(as_text=True)
    post.assert_not_called()
    upd.assert_not_called()
    assert _org(es) == before


def test_post_with_acceptance_records_it_sends_a_valid_release_with_the_bearer_key_and_keeps_the_receipt(app, store, owner):
    es, _ = store
    client, _ = owner
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form(named="yes", display_name="Riverside Media (test data)", test_data="yes"))
    assert resp.status_code == 200, resp.get_data(as_text=True)
    page = resp.get_data(as_text=True)
    assert "Published" in page and "rel-0001" in page

    # the one network call: the gateway's releases route, the key as a bearer token, the file as JSON
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == GATEWAY + "/v0/partners/releases"
    assert kwargs["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert kwargs["timeout"] > 0
    body = kwargs["json"]
    assert job.validate_release(body) == [] and job._semantic_check(body) == []
    assert body["manifest"]["countriesIncluded"] == sorted(s.PUBLISHED_COUNTRIES)
    assert body["manifest"]["displayName"] == {"named": True, "name": "Riverside Media (test data)"}
    assert body["manifest"]["testData"] is True
    assert {r["countryIso2"] for r in body["rows"]} <= set(s.PUBLISHED_COUNTRIES)
    assert API_KEY not in page

    # the acceptance on the organisation: computed server-side, not taken from the form
    org = _org(es)
    assert org["gc_agreement_id"] == "digital-aggregates"
    assert org["gc_agreement_version"] == "0.2"
    assert org["gc_agreement_sha256"] == s.AGREEMENT_SHA256
    assert org["gc_agreement_accepted_at"] != s.AGREEMENT_ACCEPTED_AT
    assert org["gc_agreement_accepted_at"].endswith("Z")
    assert body["manifest"]["agreement"]["acceptedAt"] == org["gc_agreement_accepted_at"]
    assert body["manifest"]["agreement"]["sha256"] == org["gc_agreement_sha256"]
    assert org["gc_display_named"] is True
    # the receipt
    assert org["gc_last_release_id"] == "rel-0001"
    assert org["gc_last_release_hash"] == "sha256:" + "ab" * 32
    assert org["gc_last_published_at"].endswith("Z")


def test_gateway_refusal_leaves_no_receipt_and_shows_the_error(app, store, owner):
    es, _ = store
    client, _ = owner
    with _gateway(status=422, body={"error": "manifest.grain must be country x month"}):
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "HTTP 422" in page and "manifest.grain" in page
    org = _org(es)
    assert not any(k.startswith("gc_last_release") for k in org)
    assert "gc_last_published_at" not in org
    # the acceptance is written only after a 202, so a refusal leaves
    # the record exactly as it was (still the fixture's earlier acceptance).
    assert org["gc_agreement_accepted_at"] == s.AGREEMENT_ACCEPTED_AT
    assert org["gc_display_named"] is True
    # an unreachable gateway reads the same way
    with mock.patch(f"{R}.requests.post", side_effect=routes.requests.ConnectionError("boom")):
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400
    assert "could not reach the gateway (ConnectionError)" in resp.get_data(as_text=True)
    assert "gc_last_release_id" not in _org(es)


def test_non_https_gateway_url_is_refused_before_any_request(app, store, owner):
    es, _ = store
    client, _ = owner
    es.docs[job.ORG_INDEX] = [
        (i, {**d, "gc_gateway_url": "http://api.example.org"} if i == s.ORG_ESID else d) for i, d in es.docs[job.ORG_INDEX]
    ]
    page = client.get(URL).get_data(as_text=True)
    assert "must be an https address" in page
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400
    assert "must be an https address" in resp.get_data(as_text=True)
    post.assert_not_called()
    assert "gc_last_release_id" not in _org(es)
    assert routes.gateway_url_ok("https://api.example.org")
    assert routes.gateway_url_ok("https://api.example.org/")
    assert routes.gateway_url_ok("https://API.example.org")
    for bad in ("http://api.example.org", "https://user:pw@api.example.org", "https://api.example.org/?x=1", "", "ftp://x"):
        assert not routes.gateway_url_ok(bad), bad


def test_gateway_host_must_be_on_the_allowlist_and_redirects_are_never_followed(app, store, owner, monkeypatch):
    """Whoever can edit the organisation record must not be able to point the
    release and its bearer key at a server of their choosing."""
    es, _ = store
    client, _ = owner
    # the default allowlist is the production gateway alone
    monkeypatch.delenv(routes.GATEWAY_HOSTS_ENV, raising=False)
    assert routes.allowed_gateway_hosts() == frozenset({"api.global.church"})
    assert routes.gateway_url_ok("https://api.global.church")
    assert not routes.gateway_url_ok("https://api.example.org")
    monkeypatch.setenv(routes.GATEWAY_HOSTS_ENV, " api.example.org , Staging.Example.org ")
    assert routes.allowed_gateway_hosts() == frozenset({"api.example.org", "staging.example.org"})
    # an https URL on a host outside the allowlist is refused before any request
    es.docs[job.ORG_INDEX] = [
        (i, {**d, "gc_gateway_url": "https://attacker.example.net"} if i == s.ORG_ESID else d)
        for i, d in es.docs[job.ORG_INDEX]
    ]
    assert "allowed gateway host" in client.get(URL).get_data(as_text=True)
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400 and "allowed gateway host" in resp.get_data(as_text=True)
    post.assert_not_called()
    assert "gc_last_release_id" not in _org(es)
    # a redirect from an allowed host is not followed: no receipt, the error names it
    es.docs[job.ORG_INDEX] = [
        (i, {**d, "gc_gateway_url": GATEWAY} if i == s.ORG_ESID else d) for i, d in es.docs[job.ORG_INDEX]
    ]
    with _gateway(status=302, body={}) as post:
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400 and "redirect (HTTP 302)" in resp.get_data(as_text=True)
    assert post.call_args.kwargs["allow_redirects"] is False
    assert "gc_last_release_id" not in _org(es)


def test_a_key_that_is_not_stored_encrypted_or_no_longer_decrypts_is_refused_and_nothing_is_sent(app, store, owner):
    es, _ = store
    client, _ = owner
    es.docs[job.ORG_INDEX] = [
        (i, {**d, "gc_partner_api_key": "plain-text-key"} if i == s.ORG_ESID else d) for i, d in es.docs[job.ORG_INDEX]
    ]
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400
    page = resp.get_data(as_text=True)
    assert "not stored encrypted" in page and "plain-text-key" not in page
    post.assert_not_called()
    es.docs[job.ORG_INDEX] = [
        (i, {**d, "gc_partner_api_key": API_KEY_TOKEN} if i == s.ORG_ESID else d) for i, d in es.docs[job.ORG_INDEX]
    ]
    with _gateway() as post, mock.patch(f"{R}.decrypt", side_effect=routes.InvalidToken()):
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 400 and "cannot be decrypted" in resp.get_data(as_text=True)
    post.assert_not_called()
    assert "gc_last_release_id" not in _org(es)
    # the gateway's own message is bounded and stripped of control characters before it is shown
    long = "x" * 500 + "\x07bell"
    assert routes._error_detail({"error": long}) == "x" * routes.ERROR_DETAIL_MAX
    assert routes._error_detail({"message": "a\x00b\nc"}) == "a b c"
    assert routes._error_detail({}) == ""


def test_the_page_says_an_opaque_identifier_not_the_title_leaves_with_each_row(app, store, owner):
    client, _ = owner
    page = client.get(URL).get_data(as_text=True)
    assert "opaque per-team identifier" in page and "rename the team before publishing" not in page


def test_a_superuser_may_view_but_not_publish(app, store):
    """The manifest records ``acceptedByRole: workspace-owner``; a superuser who
    is not the owner cannot make that true, so the POST is refused."""
    es, upd = store
    with mock.patch(f"{R}.PermissionResolver.has_permission_in_workspace", return_value=False):
        c = _client(app, ["superuser"], username="rootuser")
        assert c.get(URL).status_code == 200
        with _gateway() as post:
            resp = c.post(URL, data=_publish_form())
    assert resp.status_code == 403
    assert "a superuser may view but not publish" in resp.get_data(as_text=True)
    post.assert_not_called()
    upd.assert_not_called()
    assert "gc_last_release_id" not in _org(es)


def test_preview_never_contains_a_document_value(app, store, owner):
    es, _ = store
    client, _ = owner
    page = client.get(URL).get_data(as_text=True)
    echo_docs = list(s.docs_of(es, f"{s.TEAM_PASHTO}__echo_conversations"))
    assert echo_docs
    for d in echo_docs[:50]:
        for key in ("id", "client_id", "created_at", "referrer", "team_city", "team_site_url", "echo_source"):
            assert str(d[key]) not in page, key
    fb_docs = list(s.docs_of(es, f"{s.TEAM_PASHTO}__fb_insights"))
    assert fb_docs
    for d in fb_docs[:50]:
        for key in d:
            if key.startswith("adset") or key.startswith("campaign") or key.startswith("ad_"):
                assert str(d[key]) not in page, key


def test_selection_and_name_validation(app, store, owner):
    es, _ = store
    client, _ = owner
    # a team outside the organisation is dropped from the selection, never read
    page = client.get(URL, query_string={"teams": ["team-elsewhere", s.TEAM_URDU], "countries": ["pk"]}).get_data(as_text=True)
    assert 'value="team-urdu-outreach" checked' in page
    assert 'value="team-pashto-outreach" checked' not in page
    assert 'value="PK" checked' in page and 'value="IN" checked' not in page
    # named without a name is refused before anything is written
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form(named="yes", display_name="   "))
    assert resp.status_code == 400 and "Enter the name" in resp.get_data(as_text=True)
    post.assert_not_called()
    # no teams selected: refused
    with _gateway() as post:
        resp = client.post(URL, data=_publish_form(teams=[]))
    assert resp.status_code == 400 and "at least one team" in resp.get_data(as_text=True)
    post.assert_not_called()
    assert "gc_last_release_id" not in _org(es)


def test_agreement_renderer_escapes_and_a_stored_key_is_decrypted_only_for_the_header(app, store, owner):
    html = routes.render_agreement_html(
        "# T <b>\n\nA **bold** `code` <script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two\n\n---\n"
    )
    assert "<h2>T &lt;b&gt;</h2>" in html
    assert "<strong>bold</strong>" in html and "<code>code</code>" in html and "&lt;script&gt;" in html
    assert "<table>" in html and "<td>2</td>" in html and "<li>two</li>" in html and "<hr>" in html
    es, _ = store
    client, _ = owner
    token = "gAAAAA" + "A" * 100
    es.docs[job.ORG_INDEX] = [
        (i, {**d, "gc_partner_api_key": token} if i == s.ORG_ESID else d) for i, d in es.docs[job.ORG_INDEX]
    ]
    with _gateway() as post, mock.patch(f"{R}.decrypt", return_value="plain-key") as dec:
        resp = client.post(URL, data=_publish_form())
    assert resp.status_code == 200
    dec.assert_called_once_with(token)
    assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer plain-key"
    assert "plain-key" not in resp.get_data(as_text=True)

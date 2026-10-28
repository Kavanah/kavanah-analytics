"""Release-product export job: counting rules, selection, manifest, schema, dry-run/apply.

Every test runs the real job (``asset_management.jobs.export_release_product``)
against the synthetic Riverside Media workspace in ``support.py`` through a
fake Elasticsearch that evaluates the job's own queries. Expected figures
are recomputed here straight from the fixture documents, by the rule under
test, so a change in the rule and a change in the data both show.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone

import pytest

from asset_management.jobs import export_release_product as job
from scripts import export_release_product as cli
from tests.jobs import support as s

SELECTED_TEAMS = [s.TEAM_PASHTO, s.TEAM_URDU]


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    """The whole job sits behind GLOBAL_CHURCH_PUBLISH (off by default); these tests exercise the counting rules with it on."""
    monkeypatch.setattr(job, "publish_enabled", lambda: True)


@pytest.fixture
def es():
    return s.riverside_media_workspace()


@pytest.fixture
def release(es):
    rel, counts = job.build_release(
        es, org_esid=s.ORG_ESID, team_esids=SELECTED_TEAMS, countries=s.PUBLISHED_COUNTRIES, now=s.NOW, test_data=True
    )
    return rel, counts


def _rows_by_cell(rel):
    return {(r["team"], r["countryIso2"], r["month"]): r for r in rel["rows"]}


def _in_window(doc):
    return s.FIRST_MONTH <= doc["kibana_date"][:7] <= s.LAST_MONTH


# ── Counting ──────────────────────────────────────────────────────────────────


def test_touchpoints_are_echo_conversations_by_seeker_country_and_activity_month(es, release):
    rel, _ = release
    cells = _rows_by_cell(rel)
    expected: dict[tuple, int] = {}
    for esid, label in s.LABELS.items():
        for d in s.docs_of(es, f"{esid}__echo_conversations"):
            if d["contact_country_iso2"] in s.PUBLISHED_COUNTRIES and _in_window(d):
                key = (label, d["contact_country_iso2"], d["kibana_date"][:7])
                expected[key] = expected.get(key, 0) + 1
    assert expected, "fixture must hold Echo conversations"
    for key, n in expected.items():
        assert cells[key]["touchpoints"] == n, key
    for key, row in cells.items():
        assert row["touchpoints"] == expected.get(key, 0), key


def test_month_is_the_activity_month_not_the_import_month(es, release):
    """Every Echo doc in the fixture was imported the month after it happened; counting by @timestamp would shift everything."""
    rel, _ = release
    cells = _rows_by_cell(rel)
    shifted = 0
    for esid, label in s.LABELS.items():
        for d in s.docs_of(es, f"{esid}__echo_conversations"):
            if d["contact_country_iso2"] in s.PUBLISHED_COUNTRIES and _in_window(d) and d["kibana_date"][:7] < s.LAST_MONTH:
                assert d["@timestamp"][:7] != d["kibana_date"][:7]
                shifted += 1
    assert shifted > 0
    # a conversation dated before the window (import-stamped inside it) is not counted
    assert not any(m < s.FIRST_MONTH for (_, _, m) in cells)
    assert all(s.FIRST_MONTH <= r["month"] <= s.LAST_MONTH for r in rel["rows"])


def test_active_counts_spiritual_or_multiplier_flags_and_never_exceeds_touchpoints(es, release):
    rel, _ = release
    cells = _rows_by_cell(rel)
    expected: dict[tuple, int] = {}
    for esid, label in s.LABELS.items():
        for d in s.docs_of(es, f"{esid}__echo_conversations"):
            if d["contact_country_iso2"] in s.PUBLISHED_COUNTRIES and _in_window(d):
                if d["spiritual_conversation"] >= 1 or d["multiplier"] >= 1:
                    key = (label, d["contact_country_iso2"], d["kibana_date"][:7])
                    expected[key] = expected.get(key, 0) + 1
    assert expected
    for key, row in cells.items():
        assert row["active"] == expected.get(key, 0), key
        assert row["active"] <= row["touchpoints"]


def test_touchpoints_come_from_echo_only_chatwoot_is_never_read(es):
    """A Chatwoot index beside Echo changes nothing and is not listed as a source."""
    cw = f"{s.TEAM_PASHTO}__chatwoot_conversations"
    for i in range(50):
        es.add(cw, f"cw-{i}", {"kibana_date": "2026-03-10", "team_esid": s.TEAM_PASHTO, "contact_country_iso2": "PK"})
    rel, _ = job.build_release(
        es, org_esid=s.ORG_ESID, team_esids=SELECTED_TEAMS, countries=s.PUBLISHED_COUNTRIES, now=s.NOW, test_data=True
    )
    assert "chatwoot" not in " ".join(rel["manifest"]["sources"])
    assert cw not in {idx for idx, _ in es.searches}
    base = s.riverside_media_workspace()
    rel0, _ = job.build_release(
        base, org_esid=s.ORG_ESID, team_esids=SELECTED_TEAMS, countries=s.PUBLISHED_COUNTRIES, now=s.NOW, test_data=True
    )
    assert rel["rows"] == rel0["rows"]


def test_exposed_and_interactions_sum_the_named_fields_from_the_named_record_types(es, release):
    rel, _ = release
    cells = _rows_by_cell(rel)
    from lib.country_codes import resolve_country_to_iso2

    exp_exposed: dict[tuple, int] = {}
    exp_inter: dict[tuple, int] = {}

    def add(store, key, n):
        store[key] = store.get(key, 0) + n

    for esid, label in s.LABELS.items():
        for d in s.docs_of(es, f"{esid}__google_ads"):
            if d.get("record_type") == "geo" and d["geo_country_code"] in s.PUBLISHED_COUNTRIES and _in_window(d):
                key = (label, d["geo_country_code"], d["kibana_date"][:7])
                add(exp_exposed, key, d["impressions"])
                add(exp_inter, key, d.get("clicks", 0))
        for d in s.docs_of(es, f"{esid}__youtube_analytics"):
            if d["record_type"] == "country_breakdown" and d["country"] in s.PUBLISHED_COUNTRIES and _in_window(d):
                add(exp_exposed, (label, d["country"], d["kibana_date"][:7]), d["views"])
        for d in s.docs_of(es, f"{esid}__ga_metrics"):
            iso2 = resolve_country_to_iso2(d["country"])
            if d["report_type"] == "geo_device" and iso2 in s.PUBLISHED_COUNTRIES and _in_window(d):
                add(exp_inter, (label, iso2, d["kibana_date"][:7]), d["sessions"])
        for d in s.docs_of(es, f"{esid}__fb_insights"):
            iso2 = job.single_target_country(d["adset_targeting"])
            if iso2 in s.PUBLISHED_COUNTRIES and _in_window(d):
                key = (label, iso2, d["kibana_date"][:7])
                add(exp_exposed, key, d["insight_impressions"])
                add(exp_inter, key, d["insight_clicks"])
    assert exp_exposed and exp_inter
    for key, row in cells.items():
        assert row["exposed"] == exp_exposed.get(key, 0), key
        assert row["interactions"] == exp_inter.get(key, 0), key
    # the daily campaign docs (clicks, no country) and the pages / video_retention docs never contribute
    assert any(d.get("record_type") is None and d.get("clicks") for d in s.docs_of(es, f"{s.TEAM_PASHTO}__google_ads"))


# ── Facebook attribution ──────────────────────────────────────────────────────


def test_facebook_single_country_ad_sets_attribute_multi_country_ones_go_unattributed(es, release):
    rel, counts = release
    exp_un_exposed = exp_un_inter = 0
    single_pk = 0
    for esid in s.TEAMS:
        for d in s.docs_of(es, f"{esid}__fb_insights"):
            if not _in_window(d):
                continue
            iso2 = job.single_target_country(d["adset_targeting"])
            if iso2 is None:
                exp_un_exposed += d["insight_impressions"]
                exp_un_inter += d["insight_clicks"]
            elif iso2 == "PK":
                single_pk += d["insight_impressions"]
    assert exp_un_exposed > 0 and single_pk > 0
    assert rel["manifest"]["unattributed"] == {"exposed": exp_un_exposed, "interactions": exp_un_inter}
    # multi-country impressions were not split into any country's row
    pk_total = sum(r["exposed"] for r in rel["rows"] if r["countryIso2"] == "PK")
    assert pk_total < pk_total + exp_un_exposed
    # the targeting reducer itself
    assert job.single_target_country(s.fb_targeting(["PK"])) == "PK"
    assert job.single_target_country(s.fb_targeting([], regions_in="PK")) == "PK"
    assert job.single_target_country(s.fb_targeting(["PK"], regions_in="PK")) == "PK"
    assert job.single_target_country(s.fb_targeting(["PK", "IN"])) is None
    assert job.single_target_country(s.fb_targeting(["PK"], regions_in="IN")) is None
    assert job.single_target_country(s.fb_targeting([], country_groups=["worldwide"])) is None
    assert job.single_target_country({"geo_locations": {"countries": [{"country_code": "np"}]}}) == "NP"
    assert job.single_target_country(None) is None


# ── Selection ─────────────────────────────────────────────────────────────────


def test_only_selected_countries_and_teams_leave_and_unselected_ones_are_simply_absent(es):
    rel, _ = job.build_release(
        es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["PK", "IN"], now=s.NOW, test_data=True
    )
    assert rel["manifest"]["countriesIncluded"] == ["IN", "PK"]
    assert {r["countryIso2"] for r in rel["rows"]} == {"IN", "PK"}
    assert {r["team"] for r in rel["rows"]} == {s.LABELS[s.TEAM_PASHTO]}
    text = json.dumps(rel)
    assert "Pashto outreach" not in text and s.TEAM_PASHTO not in text  # neither title nor esid leaves
    for withheld in s.WITHHELD_COUNTRIES:
        assert f'"{withheld}"' not in text
        assert s.ISO3[withheld] not in text
    assert "Urdu outreach" not in text
    assert "withheld" not in text.lower()
    # the Echo query carried the country filter: unselected countries never left ES
    echo_bodies = [b for idx, b in es.searches if idx.endswith("__echo_conversations")]
    assert echo_bodies and all(
        {"terms": {"contact_country_iso2": ["IN", "PK"]}} in b["query"]["bool"]["filter"] for b in echo_bodies
    )


def test_organisation_membership_is_read_from_a_list_or_a_bare_string(es):
    """Production team documents carry ``organization_esid`` as a list (the Teams
    form is a multi-select); older documents carry a string. Both must match,
    and a list that names several organisations matches each of them."""
    assert job.organization_refs([s.ORG_ESID]) == [s.ORG_ESID]
    assert job.organization_refs(s.ORG_ESID) == [s.ORG_ESID]
    assert job.organization_refs(["other-org", s.ORG_ESID]) == ["other-org", s.ORG_ESID]
    assert job.organization_refs(None) == [] and job.organization_refs("") == [] and job.organization_refs([None]) == []
    es.add(job.TEAMS_INDEX, "team-string-shape", {**s.team_doc(s.TEAM_URDU), "organization_esid": s.ORG_ESID})
    es.add(job.TEAMS_INDEX, "team-two-orgs", {**s.team_doc(s.TEAM_URDU), "organization_esid": ["other-org", s.ORG_ESID]})
    teams = job.load_teams(es, s.ORG_ESID, [s.TEAM_PASHTO, "team-string-shape", "team-two-orgs"])
    assert teams == [s.TEAM_PASHTO, "team-string-shape", "team-two-orgs"]
    with pytest.raises(job.ExportRefused, match="does not belong"):
        job.load_teams(es, "other-org", [s.TEAM_PASHTO])


def test_a_team_outside_the_organisation_is_refused(es):
    with pytest.raises(job.ExportRefused, match="does not belong"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO, "team-elsewhere"], countries=["PK"], now=s.NOW)
    with pytest.raises(job.ExportRefused, match="not found"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=["no-such-team"], countries=["PK"], now=s.NOW)
    with pytest.raises(job.ExportRefused, match="alpha-2"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["PAK"], now=s.NOW)


# ── ISO-3 ─────────────────────────────────────────────────────────────────────


def test_alpha3_is_written_beside_alpha2_from_catalog_then_static_table(es, release):
    rel, _ = release
    for r in rel["rows"]:
        assert r["countryIso3"] == s.ISO3[r["countryIso2"]]
    catalog = job.load_catalog_iso3(es)
    # the seeded catalog's country_iso3_code holds alpha-2 — it must not be mistaken for alpha-3
    assert catalog == {"KZ": "KAZ"}
    assert job.resolve_iso3("PK", {}) == "PAK"  # static table
    assert job.resolve_iso3("KZ", catalog) == "KAZ"  # catalog


def test_a_country_whose_alpha3_cannot_be_resolved_refuses_the_run(es, monkeypatch):
    monkeypatch.setattr(job, "ISO2_TO_ISO3_STATIC", {})
    monkeypatch.setitem(__import__("sys").modules, "pycountry", None)
    with pytest.raises(job.ExportRefused, match="refusing to guess"):
        job.resolve_iso3("PK", {})
    d = s._echo_doc(s.random.Random(1), s.TEAM_PASHTO, "PK", "2026-05", "2026-06")
    d["contact_country_iso2"] = "XZ"
    es.add(f"{s.TEAM_PASHTO}__echo_conversations", d["id"], d)
    with pytest.raises(job.ExportRefused, match="XZ"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["XZ"], now=s.NOW)


# ── Manifest + schema ─────────────────────────────────────────────────────────


def test_manifest_carries_the_organisation_settings_and_the_window(release):
    rel, _ = release
    m = rel["manifest"]
    assert m["schemaVersion"] == "0.2.0"
    assert m["grain"] == "country x month"
    assert m["suppression"] == "none"
    assert m["sources"] == ["echo", "facebook_ads", "ga4", "google_ads", "youtube"]
    assert m["countriesIncluded"] == sorted(s.PUBLISHED_COUNTRIES)
    assert m["organization"] == {"orgUri": s.ORG_URI, "partnerOrgId": s.ORG_ESID}
    assert m["displayName"] == {"named": True, "name": s.DISPLAY_NAME}
    assert m["period"] == {"firstMonth": "2024-11", "lastMonth": "2026-10"}
    assert m["agreement"] == {
        "id": s.AGREEMENT_ID,
        "version": s.AGREEMENT_VERSION,
        "sha256": s.AGREEMENT_SHA256,
        "acceptedAt": s.AGREEMENT_ACCEPTED_AT,
        "acceptedByRole": "workspace-owner",
    }
    assert m["generatedAt"] == "2026-10-19T12:00:00Z"
    assert m["generatedBy"] == "kavanah-analytics export_release_product 0.1.0"
    assert m["testData"] is True
    assert job.month_window(datetime(2026, 1, 15, tzinfo=timezone.utc)) == ("2024-02", "2026-01")


def test_unnamed_organisation_and_missing_settings(es):
    org = s.organization_doc(gc_display_named=False, gc_display_name="should not appear")
    es.docs["de_asset_organization"] = [(s.ORG_ESID, org)]
    rel, _ = job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["PK"], now=s.NOW)
    assert rel["manifest"]["displayName"] == {"named": False}
    assert "testData" not in rel["manifest"]
    es.docs["de_asset_organization"] = [(s.ORG_ESID, s.organization_doc(gc_agreement_sha256=""))]
    with pytest.raises(job.ExportRefused, match="gc_agreement_sha256"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["PK"], now=s.NOW)
    es.docs["de_asset_organization"] = [(s.ORG_ESID, s.organization_doc(gc_org_uri="https://example.org/x"))]
    with pytest.raises(job.ExportRefused, match="gc_org_uri"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["PK"], now=s.NOW)
    es.docs["de_asset_organization"] = [(s.ORG_ESID, s.organization_doc(gc_display_name=""))]
    with pytest.raises(job.ExportRefused, match="gc_display_name"):
        job.build_release(es, org_esid=s.ORG_ESID, team_esids=[s.TEAM_PASHTO], countries=["PK"], now=s.NOW)


def test_output_validates_against_the_release_product_schema(release):
    """The built-in check and, when installed, jsonschema both accept the output and reject a broken one."""
    rel, _ = release
    assert job.validate_release(rel) == []
    assert job._builtin_validate(rel) == []
    broken = json.loads(json.dumps(rel))
    broken["rows"][0]["active"] = broken["rows"][0]["touchpoints"] + 1
    broken["manifest"]["displayName"] = {"named": False, "name": "x"}
    broken["manifest"]["agreement"]["sha256"] = "nothex"
    broken["rows"].append(dict(broken["rows"][1]))
    problems = job._builtin_validate(broken) + job._semantic_check(broken)
    assert any("active > touchpoints" in p for p in problems)
    assert any("displayName/name" in p for p in problems)
    assert any("sha256" in p for p in problems)
    assert any("duplicate" in p for p in problems)
    jsonschema = pytest.importorskip("jsonschema")
    with open(job.schema_path(), encoding="utf-8") as fh:
        schema = json.load(fh)
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(rel)
    assert job.validate_release(broken)


# ── Dry-run / apply ───────────────────────────────────────────────────────────


def _printed(es, tmp_path, apply):
    lines: list[str] = []
    out_path = str(tmp_path / "out" / "release.json")
    rc = job.run(
        es,
        org_esid=s.ORG_ESID,
        team_esids=SELECTED_TEAMS,
        countries=s.PUBLISHED_COUNTRIES,
        apply=apply,
        out_path=out_path,
        now=s.NOW,
        test_data=True,
        out=lines.append,
    )
    return rc, "\n".join(lines), out_path


def test_dry_run_prints_counts_only_and_writes_nothing(es, tmp_path):
    rc, text, out_path = _printed(es, tmp_path, apply=False)
    assert rc == 0
    assert not os.path.exists(out_path) and not os.path.exists(os.path.dirname(out_path))
    assert "DRY-RUN" in text and "would write" in text and out_path in text
    assert '"rows": 238' in text and '"countries": 5' in text
    assert '"first_month": "2024-11"' in text and '"last_month": "2026-10"' in text
    assert "unattributed_exposed" in text
    # no document value, team figure, name or id leaks
    for forbidden in ("Pashto outreach", "Urdu outreach", "client-", "echo-", "Riverside", "PAK", '"PK"', 'exposed": 3961'):
        assert forbidden not in text, forbidden
    assert "countryIso2" not in text


def test_apply_writes_a_valid_file(es, tmp_path):
    rc, text, out_path = _printed(es, tmp_path, apply=True)
    assert rc == 0 and "wrote" in text
    with open(out_path, encoding="utf-8") as fh:
        written = json.load(fh)
    assert job.validate_release(written) == []
    assert written["manifest"]["testData"] is True
    assert len(written["rows"]) == 238
    assert not os.path.exists(out_path + ".tmp")


def test_refusal_exits_nonzero_and_writes_nothing(es, tmp_path):
    lines: list[str] = []
    out_path = str(tmp_path / "x.json")
    rc = job.run(
        es,
        org_esid="no-such-org",
        team_esids=SELECTED_TEAMS,
        countries=["PK"],
        apply=True,
        out_path=out_path,
        out=lines.append,
    )
    assert rc == 2 and lines[0].startswith("REFUSED:") and not os.path.exists(out_path)


def test_cli_parses_lists_and_passes_them_to_the_job(es, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "get_elasticsearch_client", lambda: es)
    seen = {}

    def fake_run(es_arg, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(cli.job, "run", fake_run)
    rc = cli.main(
        [
            "--org",
            s.ORG_ESID,
            "--teams",
            f"{s.TEAM_PASHTO}, {s.TEAM_URDU}",
            "--countries",
            "pk,IN",
            "--apply",
            "--out",
            "/tmp/x",
        ]
    )
    assert rc == 0
    assert seen["team_esids"] == [s.TEAM_PASHTO, s.TEAM_URDU]
    assert seen["countries"] == ["pk", "IN"] and seen["apply"] is True and seen["out_path"] == "/tmp/x"
    monkeypatch.setattr(cli, "get_elasticsearch_client", lambda: None)
    assert cli.main(["--org", "o", "--teams", "t", "--countries", "PK"]) == 1


# ── Agreement text ────────────────────────────────────────────────────────────


def test_agreement_text_hash_matches_the_accepted_sha256():
    path = os.path.join(os.path.dirname(job.__file__), "..", "agreements", f"{s.AGREEMENT_ID}-v{s.AGREEMENT_VERSION}.md")
    with open(path, "rb") as fh:
        assert hashlib.sha256(fh.read()).hexdigest() == s.AGREEMENT_SHA256

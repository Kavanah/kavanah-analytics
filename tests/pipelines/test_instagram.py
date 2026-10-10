"""Tests for the Instagram pipeline sender.

 migrated ``send_instagram_json_to_elastic`` to
ESID-keyed indexing scheme. Pre-fix the sender wrote to
``{sanitize(team_title)}_ig_insights`` via inline slug logic +
``create_index_if_not_exists`` + per-doc ``es.index``; the rewrite mirrors
``send_json_to_elastic`` (FB-daily) exactly — ``build_team_lookup_cache``
+ ``resolve_team_esid`` + ``ensure_data_index`` + ``bulk``.
This file's structure mirrors ``TestFbGenerateBulkActions`` + ``TestFacebookSendMain``
in ``test_facebook.py``.
"""

import io
import json
from unittest import mock

import import_scripts.instagram.send_instagram_json_to_elastic as ig_send


_IG_SEND = "import_scripts.instagram.send_instagram_json_to_elastic"


# ---------------------------------------------------------------------------
# read_json_files — contract unchanged
# ---------------------------------------------------------------------------


class TestReadJsonFiles:

    def test_yields_parsed_json_from_directory(self):
        payload = {"team_title": "Alpha", "followers_count": 500}
        fake_walk = [("/data/instagram", [], ["alpha.json"])]
        fake_open = mock.mock_open(read_data=json.dumps(payload))

        with mock.patch(f"{_IG_SEND}.os.walk", return_value=fake_walk), \
             mock.patch("builtins.open", fake_open):
            results = list(ig_send.read_json_files("/data/instagram"))

        assert len(results) == 1
        assert results[0]["team_title"] == "Alpha"

    def test_skips_non_json_files(self):
        fake_walk = [("/data/instagram", [], ["notes.txt", "report.json"])]
        fake_open = mock.mock_open(read_data=json.dumps({"team_title": "Beta"}))

        with mock.patch(f"{_IG_SEND}.os.walk", return_value=fake_walk), \
             mock.patch("builtins.open", fake_open):
            results = list(ig_send.read_json_files("/data/instagram"))

        assert len(results) == 1

    def test_yields_nothing_for_empty_directory(self):
        with mock.patch(f"{_IG_SEND}.os.walk", return_value=[("/data/instagram", [], [])]):
            assert list(ig_send.read_json_files("/data/instagram")) == []

    def test_handles_multiple_files(self):
        fake_walk = [("/data", [], ["a.json", "b.json", "c.json"])]
        payloads = iter([{"id": i} for i in range(3)])

        def fake_open_fn(path, *args, **kwargs):
            return io.StringIO(json.dumps(next(payloads)))

        with mock.patch(f"{_IG_SEND}.os.walk", return_value=fake_walk), \
             mock.patch("builtins.open", side_effect=fake_open_fn):
            results = list(ig_send.read_json_files("/data"))

        assert len(results) == 3


# ---------------------------------------------------------------------------
# generate_bulk_actions — ESID-keyed + orphan-drop contract
# ---------------------------------------------------------------------------


class TestIgGenerateBulkActions:
    """mirrors ``TestFbGenerateBulkActions`` from test_facebook.py.

    Sender now takes a team_cache built from de_asset_teams and routes
    data to ``{team_esid}__ig_insights`` (not ``{slug}_ig_insights``).
    Orphan docs (no team_esid AND no matching team_title) are dropped.
    """

    def _make_doc(self, team="Alpha", team_esid=None, **kwargs):
        base = {"team_title": team, "kibana_date": "2024-06-01", "followers_count": 100}
        if team_esid is not None:
            base["team_esid"] = team_esid
        base.update(kwargs)
        return base

    def _cache(self, *teams):
        """Build a team_cache shape matching build_team_lookup_cache output."""
        by_esid = {}
        by_title = {}
        for team_esid, team_title in teams:
            by_esid[team_esid] = {"team_title": team_title, "org_esids": []}
            slug = team_title.lower().replace(" ", "_").replace("-", "_")
            slug = "".join(c if c.isalnum() or c == "_" else "_" for c in slug)
            while "__" in slug:
                slug = slug.replace("__", "_")
            by_title[slug] = team_esid
        return {"by_esid": by_esid, "by_title": by_title}

    def test_yields_index_action(self):
        es = mock.MagicMock()
        doc = self._make_doc(team_esid="alpha_id")
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(f"{_IG_SEND}.ensure_data_index",
                        return_value="alpha_id__ig_insights"):
            actions = list(ig_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert len(actions) == 1
        assert actions[0]["_op_type"] == "index"
        assert actions[0]["_index"] == "alpha_id__ig_insights"
        assert actions[0]["_source"] is doc

    def test_uses_esid_keyed_index_name(self):
        es = mock.MagicMock()
        doc = self._make_doc(team="My Team", team_esid="my_team_id")
        cache = self._cache(("my_team_id", "My Team"))
        with mock.patch(f"{_IG_SEND}.ensure_data_index",
                        return_value="my_team_id__ig_insights"):
            actions = list(ig_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert actions[0]["_index"] == "my_team_id__ig_insights"

    def test_creates_index_once_per_team(self):
        es = mock.MagicMock()
        docs = [
            self._make_doc("Alpha", team_esid="alpha_id"),
            self._make_doc("Alpha", team_esid="alpha_id"),
            self._make_doc("Beta", team_esid="beta_id"),
        ]
        cache = self._cache(("alpha_id", "Alpha"), ("beta_id", "Beta"))
        with mock.patch(f"{_IG_SEND}.ensure_data_index",
                        side_effect=lambda es_, tid, src, sch: f"{tid}__{src}") as mock_ensure:
            list(ig_send.generate_bulk_actions(docs, {}, es, {}, cache))
        assert mock_ensure.call_count == 2

    def test_drops_orphan_docs(self):
        """docs whose team can't be resolved are dropped, not routed to unknown_ig_insights."""
        es = mock.MagicMock()
        docs = [
            self._make_doc("Stale Team"),  # no team_esid, no matching by_title
            self._make_doc("Alpha", team_esid="alpha_id"),
        ]
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(f"{_IG_SEND}.ensure_data_index",
                        return_value="alpha_id__ig_insights"):
            actions = list(ig_send.generate_bulk_actions(docs, {}, es, {}, cache))
        assert len(actions) == 1
        assert actions[0]["_source"]["team_title"] == "Alpha"

    def test_resolves_via_team_title_fallback_when_team_esid_missing(self):
        # The fallback path: doc lacks team_esid but team_title matches a
        # cache entry. Cache resolves to esid, indexing uses the ESID-keyed name.
        es = mock.MagicMock()
        doc = self._make_doc("Alpha")  # no team_esid
        cache = self._cache(("alpha_id", "Alpha"))
        with mock.patch(f"{_IG_SEND}.ensure_data_index",
                        return_value="alpha_id__ig_insights"):
            actions = list(ig_send.generate_bulk_actions([doc], {}, es, {}, cache))
        assert len(actions) == 1
        assert actions[0]["_index"] == "alpha_id__ig_insights"


# ---------------------------------------------------------------------------
# main — orchestration
# ---------------------------------------------------------------------------


class TestInstagramSendMain:

    def test_main_calls_bulk(self):
        mock_es = mock.MagicMock()
        data = {"team_title": "Alpha", "team_esid": "alpha_id", "kibana_date": "2024-06-01"}
        cache = {
            "by_esid": {"alpha_id": {"team_title": "Alpha", "org_esids": []}},
            "by_title": {"alpha": "alpha_id"},
        }

        with mock.patch(f"{_IG_SEND}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_IG_SEND}.get_schema_for_index_type", return_value={"mappings": {}}), \
             mock.patch(f"{_IG_SEND}.read_json_files", return_value=[data]), \
             mock.patch(f"{_IG_SEND}.build_team_lookup_cache", return_value=cache), \
             mock.patch(f"{_IG_SEND}.ensure_data_index",
                        return_value="alpha_id__ig_insights"), \
             mock.patch(f"{_IG_SEND}.bulk", return_value=(1, [])) as mock_bulk:
            ig_send.main({})

        mock_bulk.assert_called_once()

    def test_main_logs_bulk_errors(self):
        mock_es = mock.MagicMock()
        data = {"team_title": "Beta", "team_esid": "beta_id", "kibana_date": "2024-06-01"}
        cache = {
            "by_esid": {"beta_id": {"team_title": "Beta", "org_esids": []}},
            "by_title": {"beta": "beta_id"},
        }

        with mock.patch(f"{_IG_SEND}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_IG_SEND}.get_schema_for_index_type", return_value={}), \
             mock.patch(f"{_IG_SEND}.read_json_files", return_value=[data]), \
             mock.patch(f"{_IG_SEND}.build_team_lookup_cache", return_value=cache), \
             mock.patch(f"{_IG_SEND}.ensure_data_index",
                        return_value="beta_id__ig_insights"), \
             mock.patch(f"{_IG_SEND}.bulk", return_value=(0, [{"error": "mapping"}])):
            ig_send.main({})  # should not raise

    def test_main_handles_empty_directory(self):
        mock_es = mock.MagicMock()
        with mock.patch(f"{_IG_SEND}.get_elasticsearch_client", return_value=mock_es), \
             mock.patch(f"{_IG_SEND}.get_schema_for_index_type", return_value={}), \
             mock.patch(f"{_IG_SEND}.read_json_files", return_value=[]), \
             mock.patch(f"{_IG_SEND}.build_team_lookup_cache",
                        return_value={"by_esid": {}, "by_title": {}}), \
             mock.patch(f"{_IG_SEND}.bulk", return_value=(0, [])) as mock_bulk:
            ig_send.main({})

        mock_bulk.assert_called_once()  # bulk gets called with an empty generator

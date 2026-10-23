"""
Tests for the Bitly link-clicks pipeline scripts.

Covers:
  import_scripts.bitly.connections:
    list_bitly_connection_configs — de_connection enumeration + skip semantics

  import_scripts.bitly.get_link_clicks:
    match_link_to_teams  — tag → team attribution (single / zero / multi match)
    build_click_docs     — API payload → ES doc transform
    resolve_days         — --days default / clamp
    main                 — skip semantics + no-connections propagation (HTTP mocked)

  import_scripts.bitly.send_json_to_elastic:
    build_doc_id         — deterministic _id with slash sanitization
    read_json_files      — list-per-file unrolling
    main                 — ESID-keyed {team_esid}__bitly_clicks routing

  import_scripts.bitly.run_daily_imports:
    main — exit contract (False on no active connections)
                           + import lock + 3-step progress wiring

HTTP and ES are mocked throughout; the real Bitly API is never called.
"""

import json
import logging
import pytest
from unittest import mock

import import_scripts.bitly.connections as bitly_conn
import import_scripts.bitly.get_link_clicks as bitly_get
import import_scripts.bitly.send_json_to_elastic as bitly_send
import import_scripts.bitly.run_daily_imports as bitly_run
from import_scripts.bitly.connections import BitlyConnectionConfig


def _configs():
    return [
        BitlyConnectionConfig(
            team_esid="esid-tr",
            team_title="FLK North",
            tags={"FLKVT"},
            token="tok-tr",
            connection_esid="conn-tr",
        ),
        BitlyConnectionConfig(
            team_esid="esid-vn",
            team_title="FLK East",
            tags={"FLKXP"},
            token="tok-vn",
            connection_esid="conn-vn",
        ),
    ]


# ---------------------------------------------------------------------------
# match_link_to_teams
# ---------------------------------------------------------------------------


class TestMatchLinkToTeams:

    def test_single_match_returns_that_team(self):
        assert bitly_get.match_link_to_teams(["FLKVT", "campaign-x"], _configs()) == ["esid-tr"]

    def test_zero_match_returns_empty(self):
        assert bitly_get.match_link_to_teams(["campaign-x"], _configs()) == []

    def test_multi_match_returns_all_matched_teams(self):
        assert bitly_get.match_link_to_teams(["FLKXP", "FLKVT"], _configs()) == ["esid-tr", "esid-vn"]

    def test_no_tags_returns_empty(self):
        assert bitly_get.match_link_to_teams(None, _configs()) == []
        assert bitly_get.match_link_to_teams([], _configs()) == []

    def test_two_connections_for_same_team_dedupe_to_one_match(self):
        # Two principals can each hold a connection for the SAME team — that
        # is not an attribution ambiguity.
        configs = _configs() + [
            BitlyConnectionConfig(team_esid="esid-tr", team_title="FLK North", tags={"turkey-alt"}, token="t2")
        ]
        assert bitly_get.match_link_to_teams(["FLKVT", "turkey-alt"], configs) == ["esid-tr"]


# ---------------------------------------------------------------------------
# build_click_docs — API payload → ES docs transform
# ---------------------------------------------------------------------------


_TEAM_TR = {"team_title": "FLK North", "team_esid": "esid-tr"}


class TestBuildClickDocs:

    _LINK = {
        "id": "bit.ly/3abcDEF",
        "title": "Launch page",
        "long_url": "https://example.org/launch",
        "tags": ["FLKVT", "campaign-x"],
    }

    def test_transforms_daily_points_to_docs(self):
        points = [
            {"date": "2026-08-03T00:00:00+0000", "clicks": 5},
            {"date": "2026-08-04T00:00:00+0000", "clicks": 0},
        ]
        docs = bitly_get.build_click_docs(self._LINK, points, _TEAM_TR)

        assert len(docs) == 2
        assert docs[0]["date"] == "2026-08-03"
        assert docs[0]["@timestamp"] == "2026-08-03"
        assert docs[0]["kibana_date"] == "2026-08-03"
        assert docs[0]["clicks"] == 5
        assert docs[0]["link_id"] == "bit.ly/3abcDEF"
        assert docs[0]["link_title"] == "Launch page"
        assert docs[0]["long_url"] == "https://example.org/launch"
        assert docs[0]["tags"] == ["FLKVT", "campaign-x"]
        assert docs[0]["team_title"] == "FLK North"
        assert docs[0]["team_esid"] == "esid-tr"
        assert docs[0]["record_type"] == "link_clicks"

    def test_writes_zero_click_days_too(self):
        # All returned daily points are written — the deterministic _id makes
        # re-runs safe, and zero days keep the series continuous.
        points = [{"date": "2026-08-04T00:00:00+0000", "clicks": 0}]
        docs = bitly_get.build_click_docs(self._LINK, points, _TEAM_TR)
        assert len(docs) == 1
        assert docs[0]["clicks"] == 0

    def test_empty_points_yield_no_docs(self):
        assert bitly_get.build_click_docs(self._LINK, [], _TEAM_TR) == []

    def test_skips_point_without_date(self):
        docs = bitly_get.build_click_docs(self._LINK, [{"clicks": 3}], _TEAM_TR)
        assert docs == []


# ---------------------------------------------------------------------------
# resolve_days
# ---------------------------------------------------------------------------


class TestResolveDays:

    def test_defaults_to_three(self):
        assert bitly_get.resolve_days(None) == 3

    def test_standalone_flag_defaults(self):
        # import.py hands a bare `--days` through as True.
        assert bitly_get.resolve_days(True) == 3

    def test_clamps_to_max_thirty(self):
        assert bitly_get.resolve_days("90") == 30

    def test_clamps_to_min_one(self):
        assert bitly_get.resolve_days("0") == 1

    def test_accepts_valid_value(self):
        assert bitly_get.resolve_days("7") == 7

    def test_raises_on_non_numeric(self):
        with pytest.raises(ValueError):
            bitly_get.resolve_days("soon")


# ---------------------------------------------------------------------------
# connections.list_bitly_connection_configs
# ---------------------------------------------------------------------------

_CONN = "import_scripts.bitly.connections"


def _asset_hit(hit_id, team_esid, connection_esid, tags):
    return {
        "_id": hit_id,
        "_source": {
            "provider_asset_id": team_esid,
            "connection_esid": connection_esid,
            "metadata": {"tags": tags},
        },
    }


class TestListBitlyConnectionConfigs:

    def _run(self, hits, connections, tokens):
        with (
            mock.patch(f"{_CONN}.ConnectionService.list_connection_assets_by_type", return_value=hits),
            mock.patch(
                f"{_CONN}.ConnectionService.get_connection",
                side_effect=lambda esid: connections.get(esid),
            ),
            mock.patch(
                f"{_CONN}.ConnectionService.get_decrypted_token",
                side_effect=lambda esid: tokens.get(esid),
            ),
            mock.patch(f"{_CONN}._resolve_team_title", return_value="FLK North"),
        ):
            return bitly_conn.list_bitly_connection_configs()

    def test_returns_config_with_decrypted_token_and_tags(self):
        hits = [_asset_hit("a1", "esid-tr", "conn-tr", ["FLKVT", "spring"])]
        configs = self._run(hits, {"conn-tr": {"status": "active"}}, {"conn-tr": "plain-token"})
        assert len(configs) == 1
        assert configs[0].team_esid == "esid-tr"
        assert configs[0].team_title == "FLK North"
        assert configs[0].tags == {"FLKVT", "spring"}
        assert configs[0].token == "plain-token"
        assert configs[0].connection_esid == "conn-tr"

    def test_skips_revoked_connection(self):
        # Disconnect flips status=revoked — the import must stop cleanly.
        hits = [_asset_hit("a1", "esid-tr", "conn-tr", ["FLKVT"])]
        assert self._run(hits, {"conn-tr": {"status": "revoked"}}, {"conn-tr": "tok"}) == []

    def test_skips_missing_connection_and_missing_token(self):
        hits = [
            _asset_hit("a1", "esid-tr", "conn-gone", ["FLKVT"]),
            _asset_hit("a2", "esid-vn", "conn-vn", ["FLKXP"]),
        ]
        configs = self._run(hits, {"conn-vn": {"status": "active"}}, {})
        assert configs == []

    def test_skips_asset_without_tags_or_team(self):
        hits = [
            _asset_hit("a1", "esid-tr", "conn-tr", []),
            _asset_hit("a2", "", "conn-x", ["T"]),
        ]
        assert self._run(hits, {"conn-tr": {"status": "active"}}, {"conn-tr": "tok"}) == []


# ---------------------------------------------------------------------------
# get_link_clicks.main — attribution skip semantics (HTTP mocked)
# ---------------------------------------------------------------------------

_GET = "import_scripts.bitly.get_link_clicks"


class TestGetLinkClicksMain:

    def _run(self, links, click_points, configs=None, links_by_token=None):
        saved = {}

        def _capture(docs, output_dir=None):
            saved["docs"] = docs
            return "/tmp/fake.json"

        def _bitlinks(token, _guid):
            if links_by_token is not None:
                return links_by_token.get(token, [])
            return links

        with (
            mock.patch(
                f"{_GET}.list_bitly_connection_configs",
                return_value=configs if configs is not None else _configs(),
                create=True,
            ),
            mock.patch(
                "import_scripts.bitly.connections.list_bitly_connection_configs",
                return_value=configs if configs is not None else _configs(),
            ),
            mock.patch(f"{_GET}.get_groups", return_value=[{"guid": "g1"}]),
            mock.patch(f"{_GET}.get_group_bitlinks", side_effect=_bitlinks),
            mock.patch(f"{_GET}.get_link_clicks", return_value=click_points),
            mock.patch(f"{_GET}.save_click_docs", side_effect=_capture),
        ):
            bitly_get.main({})
        return saved.get("docs", [])

    def test_single_match_link_is_written_with_its_team(self):
        links = [{"id": "bit.ly/one", "title": "t", "long_url": "u", "tags": ["FLKVT"]}]
        points = [{"date": "2026-08-04T00:00:00+0000", "clicks": 2}]
        docs = self._run(links, points)
        assert len(docs) == 1
        assert docs[0]["team_esid"] == "esid-tr"
        assert docs[0]["team_title"] == "FLK North"

    def test_zero_match_link_is_skipped_with_warning(self, caplog):
        links = [{"id": "bit.ly/none", "title": "t", "long_url": "u", "tags": ["other"]}]
        points = [{"date": "2026-08-04T00:00:00+0000", "clicks": 2}]
        with caplog.at_level(logging.WARNING):
            docs = self._run(links, points)
        assert docs == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("bit.ly/none" in r.getMessage() for r in warnings)

    def test_multi_match_link_is_skipped_with_error_never_guessed(self, caplog):
        links = [{"id": "bit.ly/ambig", "title": "t", "long_url": "u", "tags": ["FLKVT", "FLKXP"]}]
        points = [{"date": "2026-08-04T00:00:00+0000", "clicks": 2}]
        with caplog.at_level(logging.ERROR):
            docs = self._run(links, points)
        assert docs == []
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert any("bit.ly/ambig" in r.getMessage() for r in errors)

    def test_shared_account_link_is_written_once_for_the_right_team(self):
        # Two teams share one Bitly account (same token) — each connection can
        # see the other team's links, but a link tagged for exactly one team
        # is written once, attributed to that team, under its own connection.
        shared = [
            BitlyConnectionConfig(team_esid="esid-tr", team_title="FLK North", tags={"FLKVT"}, token="tok"),
            BitlyConnectionConfig(team_esid="esid-vn", team_title="FLK East", tags={"FLKXP"}, token="tok"),
        ]
        links = [
            {"id": "bit.ly/tr", "title": "t", "long_url": "u", "tags": ["FLKVT"]},
            {"id": "bit.ly/vn", "title": "t", "long_url": "u", "tags": ["FLKXP"]},
        ]
        points = [{"date": "2026-08-04T00:00:00+0000", "clicks": 1}]
        docs = self._run(links, points, configs=shared)
        assert sorted(d["link_id"] for d in docs) == ["bit.ly/tr", "bit.ly/vn"]
        by_link = {d["link_id"]: d["team_esid"] for d in docs}
        assert by_link == {"bit.ly/tr": "esid-tr", "bit.ly/vn": "esid-vn"}

    def test_no_active_connections_error_propagates(self):
        # a missing scope must FAIL the run, not exit as a
        # successful zero-row import.
        with mock.patch(
            "import_scripts.bitly.connections.list_bitly_connection_configs",
            return_value=[],
        ):
            with pytest.raises(ValueError, match="no active Bitly connections"):
                bitly_get.main({})


# ---------------------------------------------------------------------------
# send_json_to_elastic — build_doc_id
# ---------------------------------------------------------------------------


class TestBuildDocId:

    def test_deterministic(self):
        assert bitly_send.build_doc_id("bit.ly/abc", "2026-08-04") == bitly_send.build_doc_id("bit.ly/abc", "2026-08-04")

    def test_sanitizes_slashes(self):
        doc_id = bitly_send.build_doc_id("bit.ly/abc", "2026-08-04")
        assert doc_id == "bit.ly_abc_2026-08-04"
        assert "/" not in doc_id

    def test_distinct_per_day_and_link(self):
        assert bitly_send.build_doc_id("bit.ly/a", "2026-08-04") != bitly_send.build_doc_id("bit.ly/a", "2026-08-05")
        assert bitly_send.build_doc_id("bit.ly/a", "2026-08-04") != bitly_send.build_doc_id("bit.ly/b", "2026-08-04")


# ---------------------------------------------------------------------------
# send_json_to_elastic — read_json_files
# ---------------------------------------------------------------------------

_SEND = "import_scripts.bitly.send_json_to_elastic"


class TestReadJsonFiles:

    def test_unrolls_list_payload(self):
        payload = [{"link_id": "bit.ly/a"}, {"link_id": "bit.ly/b"}]
        fake_walk = [("/data/bitly", [], ["run.json"])]
        with (
            mock.patch(f"{_SEND}.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", mock.mock_open(read_data=json.dumps(payload))),
        ):
            results = list(bitly_send.read_json_files("/data/bitly"))
        assert [d["link_id"] for d in results] == ["bit.ly/a", "bit.ly/b"]

    def test_skips_malformed_json_without_raising(self):
        fake_walk = [("/data", [], ["bad.json"])]
        with (
            mock.patch(f"{_SEND}.os.walk", return_value=fake_walk),
            mock.patch("builtins.open", mock.mock_open(read_data="not-valid-json")),
        ):
            assert list(bitly_send.read_json_files("/data")) == []

    def test_skips_non_json_files(self):
        fake_walk = [("/data", [], ["notes.txt"])]
        with mock.patch(f"{_SEND}.os.walk", return_value=fake_walk):
            assert list(bitly_send.read_json_files("/data")) == []


# ---------------------------------------------------------------------------
# send_json_to_elastic — main
# ---------------------------------------------------------------------------


class TestBitlySendMain:

    def _doc(self):
        return {
            "team_title": "FLK North",
            "team_esid": "esid-tr",
            "date": "2026-08-04",
            "record_type": "link_clicks",
            "link_id": "bit.ly/abc",
            "clicks": 5,
        }

    def _team_cache(self):
        return {
            "by_esid": {"esid-tr": {"team_title": "FLK North", "org_esids": []}},
            "by_title": {"flk north": "esid-tr"},
        }

    def test_routes_to_esid_keyed_bitly_clicks_index(self):
        mock_es = mock.MagicMock()
        with (
            mock.patch(f"{_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_SEND}.build_team_lookup_cache", return_value=self._team_cache()),
            mock.patch(f"{_SEND}.read_json_files", return_value=[self._doc()]),
            mock.patch(f"{_SEND}.create_bitly_clicks_index"),
            mock.patch(f"{_SEND}.send_to_elasticsearch") as mock_send,
        ):
            bitly_send.main({})

        mock_send.assert_called_once()
        assert mock_send.call_args[0][1] == "esid-tr__bitly_clicks"
        # Deterministic _id: sanitized link_id + date.
        assert mock_send.call_args[0][3] == "bit.ly_abc_2026-08-04"

    def test_drops_doc_with_unresolvable_team(self):
        mock_es = mock.MagicMock()
        doc = self._doc()
        doc["team_esid"] = "unknown-esid"
        doc["team_title"] = "Nobody"
        with (
            mock.patch(f"{_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_SEND}.build_team_lookup_cache", return_value=self._team_cache()),
            mock.patch(f"{_SEND}.read_json_files", return_value=[doc]),
            mock.patch(f"{_SEND}.send_to_elasticsearch") as mock_send,
        ):
            bitly_send.main({})
        mock_send.assert_not_called()

    def test_sets_kibana_date_when_missing(self):
        mock_es = mock.MagicMock()
        doc = self._doc()
        with (
            mock.patch(f"{_SEND}.get_elasticsearch_client", return_value=mock_es),
            mock.patch(f"{_SEND}.build_team_lookup_cache", return_value=self._team_cache()),
            mock.patch(f"{_SEND}.read_json_files", return_value=[doc]),
            mock.patch(f"{_SEND}.create_bitly_clicks_index"),
            mock.patch(f"{_SEND}.send_to_elasticsearch"),
        ):
            bitly_send.main({})
        assert doc["kibana_date"] == "2026-08-04"

    def test_create_index_uses_unwrapped_schema_mapping(self):
        # the helper passes the mappings BODY (properties
        # directly); create_index_if_not_exists adds the outer wrapper.
        es = mock.MagicMock()
        with mock.patch(f"{_SEND}.create_index_if_not_exists") as mock_create:
            bitly_send.create_bitly_clicks_index(es, "esid-tr__bitly_clicks")
        args = mock_create.call_args[0]
        assert args[0] is es
        assert args[1] == "esid-tr__bitly_clicks"
        assert "properties" in args[2]
        assert "mappings" not in args[2]
        assert args[2]["properties"]["link_id"] == {"type": "keyword"}
        assert args[2]["properties"]["clicks"] == {"type": "long"}


# ---------------------------------------------------------------------------
# run_daily_imports — exit contract + lock/progress (Phase 3)
# ---------------------------------------------------------------------------

_RUN = "import_scripts.bitly.run_daily_imports"
_CONN_LISTER = "import_scripts.bitly.connections.list_bitly_connection_configs"


class _FakeLockService:
    """Records acquire/release; configurable acquire result."""

    def __init__(self, acquire_result="lock-1"):
        self.acquire_result = acquire_result
        self.acquire_calls = []
        self.release_calls = []

    def acquire(self, connector, scope_id="", **_kwargs):  # acquire also takes progress_run_id
        self.acquire_calls.append((connector, scope_id))
        return self.acquire_result

    def release(self, connector, lock_id, scope_id=""):
        self.release_calls.append((connector, lock_id, scope_id))


class TestRunDailyImportsMain:

    def _lock(self, monkeypatch, acquire_result="lock-1"):
        fake = _FakeLockService(acquire_result)
        import lib.import_lock_service as lock_mod

        monkeypatch.setattr(lock_mod, "ImportLockService", fake)
        return fake

    def test_returns_false_when_no_active_connections(self, monkeypatch):
        # main must report failure via its return value so
        # import.py exits non-zero (the Scheduler path). Phase 3: the scope
        # is the set of active bitly de_connection rows.
        self._lock(monkeypatch)
        with mock.patch(_CONN_LISTER, return_value=[]):
            assert bitly_run.main({}) is False

    def test_early_refusal_stamps_the_progress_run_failed(self, monkeypatch):
        # Staging 2026-08-05: a Run-now dispatch whose guard refused (no
        # active connections) left its de_import_progress doc "pending"
        # forever, so the UI bar hung. Every early return must resolve the
        # progress run to a terminal failed state.
        self._lock(monkeypatch)
        stamps = []
        monkeypatch.setattr(
            bitly_run, "_bitly_complete", lambda run_id, status, error=None: stamps.append((run_id, status, error))
        )
        with mock.patch(_CONN_LISTER, return_value=[]):
            assert bitly_run.main({"progress_run_id": "run-1"}) is False
        with mock.patch(_CONN_LISTER, side_effect=RuntimeError("es down")):
            assert bitly_run.main({"progress_run_id": "run-2"}) is False
        with mock.patch(_CONN_LISTER, return_value=_configs()):
            assert bitly_run.main({"progress_run_id": "run-3", "days": "soon"}) is False
        assert [(r, s) for r, s, _ in stamps] == [("run-1", "failed"), ("run-2", "failed"), ("run-3", "failed")]

    def test_returns_false_when_enumeration_fails(self, monkeypatch):
        self._lock(monkeypatch)
        with mock.patch(_CONN_LISTER, side_effect=RuntimeError("es down")):
            assert bitly_run.main({}) is False

    def test_returns_false_on_invalid_days(self, monkeypatch):
        self._lock(monkeypatch)
        with (
            mock.patch(_CONN_LISTER, return_value=_configs()),
            mock.patch(f"{_RUN}.run_subprocess") as mock_run,
        ):
            assert bitly_run.main({"days": "soon"}) is False
        mock_run.assert_not_called()

    def test_returns_true_when_all_steps_succeed(self, monkeypatch):
        fake_lock = self._lock(monkeypatch)
        with (
            mock.patch(_CONN_LISTER, return_value=_configs()),
            mock.patch(f"{_RUN}.run_subprocess", return_value=True) as mock_run,
        ):
            assert bitly_run.main({"days": "7"}) is True
        assert mock_run.call_count == 3
        # Fetch step carries the --days window.
        fetch_cmd = mock_run.call_args_list[0][0][0]
        assert "--days=7" in fetch_cmd
        # Connector-global lock taken and released (scope_id "" matches the
        # scheduler's reported lock_scope for bitly).
        assert fake_lock.acquire_calls == [("bitly", "")]
        assert fake_lock.release_calls == [("bitly", "lock-1", "")]

    def test_returns_false_and_stops_on_step_failure(self, monkeypatch):
        fake_lock = self._lock(monkeypatch)
        with (
            mock.patch(_CONN_LISTER, return_value=_configs()),
            mock.patch(f"{_RUN}.run_subprocess", side_effect=[True, False]) as mock_run,
        ):
            assert bitly_run.main({}) is False
        # Third step (archive) is never reached; the lock is still released.
        assert mock_run.call_count == 2
        assert fake_lock.release_calls == [("bitly", "lock-1", "")]

    def test_lock_held_exits_cleanly_without_running_steps(self, monkeypatch):
        # Another bitly importer holds the lock — abort without dispatching
        # any step and WITHOUT reporting failure (echo pattern: return None,
        # import.py only exits non-zero on `result is False`).
        fake_lock = self._lock(monkeypatch, acquire_result=None)
        with (
            mock.patch(_CONN_LISTER, return_value=_configs()),
            mock.patch(f"{_RUN}.run_subprocess") as mock_run,
        ):
            assert bitly_run.main({}) is None
        mock_run.assert_not_called()
        assert fake_lock.release_calls == []

    def test_reports_three_step_progress_and_terminal_stamp(self, monkeypatch):
        self._lock(monkeypatch)
        phases = []

        class _FakeProgress:
            @staticmethod
            def set_phase(run_id, phase):
                phases.append(("phase", run_id, phase))

            @staticmethod
            def update_progress(run_id, days_fetched):
                phases.append(("step", run_id, days_fetched))

            @staticmethod
            def complete_run(run_id, status, error=None):
                phases.append(("done", run_id, status))

        import lib.import_progress_service as prog_mod

        monkeypatch.setattr(prog_mod, "ImportProgressService", _FakeProgress)
        with (
            mock.patch(_CONN_LISTER, return_value=_configs()),
            mock.patch(f"{_RUN}.run_subprocess", return_value=True),
        ):
            assert bitly_run.main({"progress_run_id": "run-1"}) is True
        assert ("phase", "run-1", "bitly_fetch") in phases
        assert ("phase", "run-1", "bitly_index") in phases
        assert ("phase", "run-1", "bitly_archive") in phases
        assert ("step", "run-1", 3) in phases
        assert phases[-1] == ("done", "run-1", "succeeded")

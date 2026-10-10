"""Tests for import_load_service (Phase 3 — concurrent-import capacity mode).

The guards are the point: a capacity test that silently serializes (duplicate
lock groups), hits prod, or force-refetches real APIs is worse than none.
"""

from unittest import mock

import pytest

from asset_management.web.services import import_load_service as ils


def _item(source="echo", team="team-1", start="2026-07-01", end="2026-07-03"):
    return {"source": source, "team_ids": team, "start_date": start, "end_date": end}


class TestLockGroup:
    def test_meta_family_shares_the_facebook_lock(self):
        assert ils.lock_group("facebook_ads", "t1") == "facebook"
        assert ils.lock_group("facebook_pages", "t2") == "facebook"
        assert ils.lock_group("instagram", "t3") == "facebook"

    def test_unlocked_sources_never_conflict(self):
        assert ils.lock_group("youtube", "t1") == ""
        assert ils.lock_group("chatwoot", "t1") == ""
        assert ils.lock_group("disciple_tools", "t1") == ""

    def test_google_ads_uses_resolved_per_scope_key(self):
        with mock.patch(
            "asset_management.web.services.import_service.ImportService._resolve_scope_arg",
            return_value="--customer_ids=222,111",
        ):
            assert ils.lock_group("google_ads", "t1") == "google_ads::111,222"

    def test_google_ads_falls_back_to_connector_global_when_unresolved(self):
        with mock.patch(
            "asset_management.web.services.import_service.ImportService._resolve_scope_arg",
            return_value=None,
        ):
            assert ils.lock_group("google_ads", "t1") == "google_ads"

    def test_echo_uses_tenant_scope_key(self):
        with mock.patch(
            "lib.import_lock_service.ImportLockService.echo_tenant_scope_key",
            return_value="tenant-a",
        ):
            assert ils.lock_group("echo", "t1") == "echo::tenant-a"


class TestValidateImports:
    def test_should_reject_empty_list(self):
        with pytest.raises(ils.ImportLoadError, match="non-empty"):
            ils.validate_imports([])

    def test_should_reject_over_cap(self):
        items = [_item(team=f"t{i}") for i in range(ils.MAX_CONCURRENT_IMPORTS + 1)]
        with mock.patch.object(ils, "lock_group", side_effect=lambda s, t, es=None: f"echo::{t}"):
            with pytest.raises(ils.ImportLoadError, match="at most"):
                ils.validate_imports(items)

    def test_should_reject_unknown_source(self):
        with pytest.raises(ils.ImportLoadError, match="unknown source"):
            ils.validate_imports([_item(source="carrier_pigeon")])

    def test_should_require_team_scope(self):
        with pytest.raises(ils.ImportLoadError, match="team_ids"):
            ils.validate_imports([_item(team="")])

    @pytest.mark.parametrize("start,end", [("2026-7-1", "2026-07-03"), ("nope", "2026-07-03"), ("2026-07-01", "")])
    def test_should_reject_malformed_dates(self, start, end):
        with pytest.raises(ils.ImportLoadError, match="YYYY-MM-DD"):
            ils.validate_imports([_item(start=start, end=end)])

    def test_should_reject_inverted_range(self):
        with pytest.raises(ils.ImportLoadError, match="after"):
            ils.validate_imports([_item(start="2026-07-05", end="2026-07-01")])

    def test_should_reject_over_long_range(self):
        with pytest.raises(ils.ImportLoadError, match="range longer"):
            ils.validate_imports([_item(start="2026-06-01", end="2026-07-01")])

    def test_should_reject_duplicate_lock_groups(self):
        """The lock loser exits as a silent no-op — two selections on the same
        lock group would fake a concurrency result, so they are rejected."""
        items = [_item(source="facebook_ads", team="t1"), _item(source="instagram", team="t2")]
        with pytest.raises(ils.ImportLoadError, match="lock"):
            ils.validate_imports(items)

    def test_should_allow_multiple_unlocked_sources(self):
        items = [_item(source="youtube", team="t1"), _item(source="chatwoot", team="t2")]
        enriched = ils.validate_imports(items)
        assert [e["lock_group"] for e in enriched] == ["", ""]

    def test_should_allow_duplicate_sources_with_distinct_lock_groups(self):
        """the status plumbing keys completion records per batch
        item now (import_runner --item), so same-source imports no longer
        collapse; only lock-group conflicts still reject. Each enriched item
        carries the item key the panel joins status markers on."""
        items = [_item(source="youtube", team="t1"), _item(source="youtube", team="t2")]
        enriched = ils.validate_imports(items)
        assert [e["item"] for e in enriched] == ["youtube:0", "youtube:1"]

    def test_should_allow_distinct_scoped_groups(self):
        with mock.patch(
            "lib.import_lock_service.ImportLockService.echo_tenant_scope_key",
            return_value="tenant-a",
        ), mock.patch(
            "asset_management.web.services.import_service.ImportService._resolve_scope_arg",
            return_value="--customer_ids=123",
        ):
            enriched = ils.validate_imports([_item(source="echo", team="t1"), _item(source="google_ads", team="t2")])
        assert enriched[0]["lock_group"] != enriched[1]["lock_group"]

    def test_should_reject_unknown_team_esids(self):
        """A typo'd (e.g. wrong-case) esid silently no-ops AND derails
        lock-group collision detection — observed live on staging. Reject."""
        es = mock.MagicMock()
        es.search.return_value = {"hits": {"hits": [{"_id": "Team-OK"}]}}
        with pytest.raises(ils.ImportLoadError, match="unknown team id"):
            ils.validate_imports([_item(team="Team-OK,team-typo")], es)

    def test_team_check_is_failure_soft(self):
        """An ES hiccup must not block the run (the check is best-effort)."""
        es = mock.MagicMock()
        es.search.side_effect = Exception("boom")
        with mock.patch.object(ils, "lock_group", return_value=""):
            enriched = ils.validate_imports([_item(source="youtube")], es)
        assert enriched[0]["source"] == "youtube"


class TestSelectWorstCase:
    """greedy worst-case picker for the capacity panel."""

    def _cand(self, source, group, date="2026-07-01", light=False):
        return {"source": source, "lock_group": group, "last_data_date": date, "light": light}

    def test_should_pick_heaviest_first_with_pairwise_distinct_groups(self):
        candidates = [
            self._cand("facebook_ads", "facebook::c1", light=True),  # weight 0
            self._cand("youtube", ""),                                # weight 3
            self._cand("echo", "echo::t1"),                           # weight 4
            self._cand("echo", "echo::t1", date="2026-06-01"),        # conflicts with [2]
            self._cand("google_ads", "google_ads::111"),              # weight 3
        ]
        picked = ils.select_worst_case(candidates, 3)
        assert picked[0] == 2  # echo first (heaviest)
        assert set(picked) == {2, 1, 4} or set(picked) == {2, 4, 1}
        assert 3 not in picked  # same echo tenant — would silently serialize

    def test_should_respect_global_vs_scoped_exclusion(self):
        """rule holds inside the picker: a connector-global group
        excludes every scoped group of the same connector."""
        candidates = [
            self._cand("echo", "echo"),           # connector-global
            self._cand("echo", "echo::t2"),       # scoped — conflicts with global
            self._cand("youtube", ""),
        ]
        picked = ils.select_worst_case(candidates, 3)
        assert not ({0, 1} <= set(picked))

    def test_should_prefer_freshest_data_within_a_weight_tier(self):
        candidates = [
            self._cand("youtube", "", date="2026-06-01"),
            self._cand("youtube", "", date="2026-07-05"),
        ]
        assert ils.select_worst_case(candidates, 1) == [1]

    def test_light_rows_only_pad_out_the_pick(self):
        candidates = [
            self._cand("facebook_ads", "facebook::c1", light=True),
            self._cand("chatwoot", ""),
        ]
        picked = ils.select_worst_case(candidates, 2)
        assert picked[0] == 1  # real load before a nearly-free FB re-import
        assert picked == [1, 0]

    def test_should_clamp_n_to_the_cap(self):
        candidates = [self._cand("youtube", "") for _ in range(ils.MAX_CONCURRENT_IMPORTS + 10)]
        assert len(ils.select_worst_case(candidates, 999)) == ils.MAX_CONCURRENT_IMPORTS

    def test_unlocked_duplicates_are_all_pickable(self):
        """same-source rows without a lock group never conflict."""
        candidates = [self._cand("youtube", "") for _ in range(3)]
        assert sorted(ils.select_worst_case(candidates, 3)) == [0, 1, 2]


class TestRefuseProd:
    def test_should_refuse_prod_hostname(self, monkeypatch):
        monkeypatch.delenv("LOAD_TEST_ALLOW_PROD", raising=False)
        with pytest.raises(ils.ImportLoadError, match="production"):
            ils.refuse_prod("kavanahanalytics.com")

    def test_should_allow_prod_with_env_opt_in(self, monkeypatch):
        monkeypatch.setenv("LOAD_TEST_ALLOW_PROD", "1")
        ils.refuse_prod("kavanahanalytics.com")  # no raise

    def test_should_allow_staging(self):
        ils.refuse_prod("staging.example.com")  # no raise


class TestRunImportLoad:
    def test_should_delegate_without_force_refresh_and_record_latest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ils, "LATEST_RUN_PATH", str(tmp_path / "latest.json"))
        run_imports = mock.MagicMock(return_value={"import_id": "abc12345", "started": ["echo"]})
        with mock.patch(
            "asset_management.web.services.import_service.ImportService.run_imports", run_imports
        ), mock.patch.object(ils, "lock_group", return_value="echo::t1"):
            result = ils.run_import_load([_item()], "staging.example.com")

        configs = run_imports.call_args.args[0]
        assert configs == [{"source": "echo", "start_date": "2026-07-01", "end_date": "2026-07-03", "team_ids": "team-1"}]
        assert "force_refresh" not in configs[0]
        assert result["imports"][0]["lock_group"] == "echo::t1"
        assert (tmp_path / "latest.json").exists()

    def test_should_refuse_prod_before_spawning(self, monkeypatch):
        monkeypatch.delenv("LOAD_TEST_ALLOW_PROD", raising=False)
        with mock.patch("asset_management.web.services.import_service.ImportService.run_imports") as run_imports:
            with pytest.raises(ils.ImportLoadError, match="production"):
                ils.run_import_load([_item()], "kavanahanalytics.com")
        run_imports.assert_not_called()


class TestListCandidates:
    def _es(self):
        es = mock.MagicMock()
        # Index names are forcibly lowercase; team doc _ids are mixed-case.
        es.indices.get.return_value = {
            "team-a__fb_insights": {},
            "team-a__echo_conversations": {},
            "team-b__youtube_analytics": {},
            "team-b__ga_metrics_window": {},  # derived index — must be ignored
            "gone-team__fb_insights": {},  # deleted team — must be skipped
            "de_asset_teams": {},  # no "__" → ignored
        }

        def search(index=None, body=None, **kwargs):
            if index == "de_asset_teams":
                return {
                    "hits": {
                        "hits": [
                            {"_id": "Team-A", "_source": {"team_title": "Team A"}},
                            {"_id": "Team-B", "_source": {"team_title": "Team B"}},
                        ]
                    }
                }
            # max-kibana_date agg per candidate index set
            latest = {
                "team-a__fb_insights": "2026-07-01T00:00:00.000Z",
                "team-a__echo_conversations": "2026-07-03T00:00:00.000Z",
                "team-b__youtube_analytics": None,  # empty index → skipped
            }.get(index)
            return {"aggregations": {"latest": {"value": 1, "value_as_string": latest} if latest else {"value": None}}}

        es.search.side_effect = search
        return es

    def test_should_shape_candidates_from_team_indices(self):
        with mock.patch.object(ils, "lock_group", side_effect=lambda s, t, es=None: {"facebook_ads": "facebook"}.get(s, s)):
            candidates = ils.list_candidates(self._es())
        # team_esid must be the REAL mixed-case doc id (usable as team_ids),
        # not the lowercased index-name form.
        assert [(c["source"], c["team_esid"]) for c in candidates] == [
            ("echo", "Team-A"),  # newest data first
            ("facebook_ads", "Team-A"),
        ]
        fb = candidates[1]
        assert fb["team_title"] == "Team A"
        assert fb["light"] is True
        assert fb["lock_group"] == "facebook"
        assert fb["last_data_date"] == "2026-07-01"
        assert fb["suggested_end"] == "2026-07-01"
        assert fb["suggested_start"] == "2026-06-29"

    def test_should_return_empty_on_es_failure(self):
        es = mock.MagicMock()
        es.indices.get.side_effect = Exception("boom")
        assert ils.list_candidates(es) == []


class TestImportLoadStatus:
    def test_should_build_markers_with_durations(self):
        raw = {
            "started_at": "2026-07-05T10:00:00",
            "results": [
                {"source": "echo", "success": True, "completed_at": "2026-07-05T10:02:30"},
                {"source": "youtube", "success": True, "completed_at": "2026-07-05T10:00:02"},
                {"source": "google_ads", "success": True, "skipped": True, "message": "no account"},
            ],
        }
        with mock.patch(
            "asset_management.web.services.import_service.ImportService.get_import_status",
            return_value={"found": True, "completed": True},
        ), mock.patch(
            "asset_management.web.services.import_service.ImportService._read_status",
            return_value=raw,
        ):
            status = ils.import_load_status("abc12345")

        by_source = {m["source"]: m for m in status["markers"]}
        assert by_source["echo"]["duration_s"] == 150.0
        assert by_source["echo"]["suspiciously_fast"] is False
        # 2s for a multi-day range = de facto no-op (lock loss / skip) — flagged.
        assert by_source["youtube"]["duration_s"] == 2.0
        assert by_source["youtube"]["suspiciously_fast"] is True
        # A deliberate connector skip is not "suspicious"; it is explained.
        assert by_source["google_ads"]["skipped"] is True
        assert by_source["google_ads"]["suspiciously_fast"] is False

    def test_markers_join_team_ids_by_item(self):
        """per-item results join back to their pid entry so two
        same-source markers are labeled by team."""
        raw = {
            "started_at": "2026-07-05T10:00:00",
            "pids": [
                {"pid": 1, "source": "youtube", "item": "youtube:0", "team_ids": "Team-A"},
                {"pid": 2, "source": "youtube", "item": "youtube:1", "team_ids": "Team-B"},
            ],
            "results": [
                {"source": "youtube", "item": "youtube:0", "success": True, "completed_at": "2026-07-05T10:03:00"},
                {"source": "youtube", "item": "youtube:1", "success": True, "completed_at": "2026-07-05T10:04:00"},
            ],
        }
        with mock.patch(
            "asset_management.web.services.import_service.ImportService.get_import_status",
            return_value={"found": True, "completed": True},
        ), mock.patch(
            "asset_management.web.services.import_service.ImportService._read_status",
            return_value=raw,
        ):
            status = ils.import_load_status("abc12345")

        markers = status["markers"]
        assert [(m["item"], m["team_ids"]) for m in markers] == [
            ("youtube:0", "Team-A"),
            ("youtube:1", "Team-B"),
        ]
        assert markers[0]["duration_s"] == 180.0
        assert markers[1]["duration_s"] == 240.0

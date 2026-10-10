"""Tests for load_tests.identity_pool.

Pure loader + round-robin logic — no locust needed.
"""

from load_tests.identity_pool import Identity, IdentityPool, load_pool


def _write_csv(path, text: str) -> str:
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestLoadPool:
    def test_should_load_identities_from_csv(self, tmp_path):
        csv_path = _write_csv(
            tmp_path / "identities.csv",
            "username,password,role,workspace\n"
            "loadtest-01@example.com,pw1,workspace_viewer,workspace-a\n"
            "loadtest-02@example.com,pw2,team_viewer,\n",
        )
        pool = load_pool(csv_path)
        assert len(pool) == 2
        assert pool[0] == Identity(
            username="loadtest-01@example.com",
            password="pw1",
            role="workspace_viewer",
            workspace="workspace-a",
        )
        assert pool[1].workspace == ""

    def test_should_return_empty_list_when_file_missing(self, tmp_path):
        """Failure-soft: the locustfile falls back to single-identity mode
        rather than aborting the run."""
        assert load_pool(str(tmp_path / "nope.csv")) == []

    def test_should_skip_rows_missing_username_or_password(self, tmp_path):
        csv_path = _write_csv(
            tmp_path / "identities.csv",
            "username,password,role,workspace\n"
            ",pw-no-user,team_viewer,\n"
            "no-password@example.com,,team_viewer,\n"
            "good@example.com,pw,team_viewer,\n",
        )
        pool = load_pool(csv_path)
        assert [i.username for i in pool] == ["good@example.com"]

    def test_should_return_empty_list_for_header_only_file(self, tmp_path):
        csv_path = _write_csv(tmp_path / "identities.csv", "username,password,role,workspace\n")
        assert load_pool(csv_path) == []

    def test_should_exclude_probe_row(self, tmp_path):
        """The probe row is a superuser injected as LOAD_TEST_USERNAME by
        run-locust — background users must never draw it."""
        csv_path = _write_csv(
            tmp_path / "identities.csv",
            "username,password,role,workspace\n"
            "loadtest-01@example.com,pw1,workspace_viewer,workspace-a\n"
            "loadtest-probe@example.com,pw-probe,probe,\n",
        )
        pool = load_pool(csv_path)
        assert [i.username for i in pool] == ["loadtest-01@example.com"]

    def test_should_exclude_unscoped_by_default(self, tmp_path):
        """Unscoped identities can't see any dashboards (403 on the list
        gate) — at 50 users they were 28% of the pool and silently diluted
        the capacity numbers. Out by default (operator decision 2026-07-05)."""
        csv_path = _write_csv(
            tmp_path / "identities.csv",
            "username,password,role,workspace\n"
            "loadtest-01@example.com,pw1,unscoped,\n"
            "loadtest-02@example.com,pw2,workspace_viewer,workspace-a\n",
        )
        pool = load_pool(csv_path)
        assert [i.username for i in pool] == ["loadtest-02@example.com"]

    def test_should_include_unscoped_when_opted_in(self, tmp_path):
        """The 'Include unscoped users' checkbox → include_unscoped=True."""
        csv_path = _write_csv(
            tmp_path / "identities.csv",
            "username,password,role,workspace\n"
            "loadtest-01@example.com,pw1,unscoped,\n"
            "loadtest-02@example.com,pw2,workspace_viewer,workspace-a\n",
        )
        pool = load_pool(csv_path, include_unscoped=True)
        assert len(pool) == 2
        assert pool[0].role == "unscoped"


class TestIdentityPoolRoundRobin:
    def test_should_hand_out_identities_in_order(self):
        pool = IdentityPool([Identity("a", "pw"), Identity("b", "pw"), Identity("c", "pw")])
        assert [pool.next().username for _ in range(3)] == ["a", "b", "c"]

    def test_should_wrap_around_when_users_exceed_identities(self):
        """More locust users than identities → index modulo pool size."""
        pool = IdentityPool([Identity("a", "pw"), Identity("b", "pw")])
        assert [pool.next().username for _ in range(5)] == ["a", "b", "a", "b", "a"]

    def test_should_return_none_when_pool_empty(self):
        assert IdentityPool([]).next() is None

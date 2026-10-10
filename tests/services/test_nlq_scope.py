"""Data Explorer NLQ tenant-scope enforcement.

The /data-explorer/api/ask and /api/chat endpoints accept client-supplied
indices / organizations / teams, but those only reached the model as advisory
prompt text. The fix enforces the caller's org/team scope server-side: every ES
query the tool loop executes is AND-ed with RoleFilterService's allowed-team
filter, resolved once per request, and callers with no resolvable scope fail
closed to a deny-all sentinel.
"""

from unittest import mock

from asset_management.web.services import nlq_service as nlq

_RFS = "asset_management.web.services.role_filter_service.RoleFilterService"
_SCOPE = {"term": {"team_title": "AllowedTeam"}}
# added an index allowlist arg to _execute_tool; these tests
# exercise the scope-injection path, so the index they use must be allow-listed.
_ALLOWED = {"victim_team__fb_insights"}


def _es_returning_empty():
    es = mock.MagicMock()
    es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}
    es.count.return_value = {"count": 0}
    return es


class TestApplyScopeFilter:
    def test_wraps_model_query_as_must_with_mandatory_filter(self):
        body = nlq._apply_scope_filter(
            {"query": {"term": {"organization_title": "VictimOrg"}}}, _SCOPE
        )
        bool_q = body["query"]["bool"]
        # The model's query survives as a must, the scope clause is non-negotiable.
        assert {"term": {"organization_title": "VictimOrg"}} in bool_q["must"]
        assert bool_q["filter"] == [_SCOPE]

    def test_missing_query_defaults_to_match_all_under_scope(self):
        body = nlq._apply_scope_filter({}, _SCOPE)
        assert body["query"]["bool"]["must"] == [{"match_all": {}}]
        assert body["query"]["bool"]["filter"] == [_SCOPE]


class TestExecuteToolEnforcesScope:
    def test_search_injects_scope_regardless_of_model_query(self):
        es = _es_returning_empty()
        with mock.patch.object(nlq, "get_elasticsearch_client", return_value=es):
            nlq._execute_tool(
                "search_elasticsearch",
                {"index": "victim_team__fb_insights",
                 "query_body": {"query": {"match_all": {}}}},
                _SCOPE,
                _ALLOWED,
            )
        # Mutation: drop _apply_scope_filter at the search site -> body carries no
        # bool.filter and this assert fails (cross-tenant query runs unscoped).
        body = es.search.call_args.kwargs["body"]
        assert body["query"]["bool"]["filter"] == [_SCOPE]

    def test_count_injects_scope(self):
        es = _es_returning_empty()
        with mock.patch.object(nlq, "get_elasticsearch_client", return_value=es):
            nlq._execute_tool(
                "count_documents",
                {"index": "victim_team__fb_insights", "query": {"match_all": {}}},
                _SCOPE,
                _ALLOWED,
            )
        body = es.count.call_args.kwargs["body"]
        assert body["query"]["bool"]["filter"] == [_SCOPE]

    def test_render_chart_injects_scope(self):
        es = _es_returning_empty()
        es.search.return_value = {"aggregations": {}, "hits": {"hits": []}}
        with mock.patch.object(nlq, "get_elasticsearch_client", return_value=es), \
                mock.patch.object(nlq, "build_query", return_value={"query": {"match_all": {}}}), \
                mock.patch.object(nlq, "echarts_transform", return_value={"title": {}}):
            nlq._execute_tool(
                "render_chart",
                {"data_view": "victim_team__fb_insights", "y_fields": []},
                _SCOPE,
                _ALLOWED,
            )
        body = es.search.call_args.kwargs["body"]
        assert body["query"]["bool"]["filter"] == [_SCOPE]


class TestResolveScopeFilter:
    def test_deny_all_floor_when_no_resolvable_scope(self):
        # Mutation: drop the has_any_scope deny-all floor -> a no-scope caller
        # would fall through to None/unrestricted and this assert fails.
        with mock.patch(_RFS + ".current_user_has_any_scope", return_value=False):
            assert nlq._resolve_scope_filter() == nlq._NLQ_DENY_ALL_FILTER

    def test_returns_user_clause_when_scoped(self):
        clause = {"terms": {"team_title": ["T1", "T2"]}}
        with mock.patch(_RFS + ".current_user_has_any_scope", return_value=True), \
                mock.patch(_RFS + ".get_filter_for_current_user", return_value=clause):
            assert nlq._resolve_scope_filter() == clause

    def test_fails_closed_when_clause_is_none(self):
        with mock.patch(_RFS + ".current_user_has_any_scope", return_value=True), \
                mock.patch(_RFS + ".get_filter_for_current_user", return_value=None):
            assert nlq._resolve_scope_filter() == nlq._NLQ_DENY_ALL_FILTER

    def test_fails_closed_on_resolution_error(self):
        with mock.patch(_RFS + ".current_user_has_any_scope", side_effect=RuntimeError("boom")):
            assert nlq._resolve_scope_filter() == nlq._NLQ_DENY_ALL_FILTER

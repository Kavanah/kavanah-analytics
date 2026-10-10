"""
Locust load test for the DEAM Dashboard.

Simulates realistic user behaviour: logs in, fetches the dashboard list,
loads each dashboard's config, then fires the actual chart queries that
the frontend would send.  This exercises the same code paths a real user
triggers when opening and navigating dashboards.

Usage (headless):
  locust --headless \
    --host https://<your-host> \
    --users 10 --spawn-rate 2 --run-time 60s \
    -f bin/python/load_tests/locustfile.py

Environment variables:
  LOAD_TEST_USERNAME       Elasticsearch username  (default: elastic)
  LOAD_TEST_PASSWORD       Elasticsearch password  (required for auth)
  LOAD_TEST_PROFILE "classic" (default) or "steps". "steps"
                           enables the stepped baseline→ramp shape plus a
                           dedicated single ProbeUser whose request names are
                           prefixed "probe:" — its stats are the one-person
                           baseline the ramp is compared against. With a shape,
                           --users/--spawn-rate/--run-time are ignored.
  LOAD_TEST_STEP_USERS     Steps mode: comma-separated total-user steps
                           (default "1,5,10,25,50"; first must be 1).
  LOAD_TEST_STEP_SECONDS   Steps mode: dwell per step in seconds (default 120).
  LOAD_TEST_IDENTITY_MODE  "single" (default): every user logs in with
                           LOAD_TEST_USERNAME/PASSWORD. "pool": background
                           users draw round-robin from the identity CSV
                           (multi-identity; the probe always stays on
                           the env identity so baselines stay comparable).
  LOAD_TEST_IDENTITY_FILE  Pool mode CSV path (default
                           /app/data/load_tests/identities.csv), emitted by
                           tools/seed_load_test_identities.py.
"""

import os
import random
import re
import sys

from locust import HttpUser, LoadTestShape, between, task

# Sibling modules (identity_pool, step_profile) — locust loads this file by
# path, so make sure our own directory is importable regardless of cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import identity_pool  # noqa: E402
import step_profile  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_USERNAME = os.environ.get("LOAD_TEST_USERNAME", "elastic")
_PASSWORD = os.environ.get("LOAD_TEST_PASSWORD", "")
_PROFILE = os.environ.get("LOAD_TEST_PROFILE", "classic")
_IDENTITY_MODE = os.environ.get("LOAD_TEST_IDENTITY_MODE", "single")
_PREFIX = "/assets"


def _extract_csrf(html: str) -> str:
    """Extract CSRF token value from an HTML login page (form hidden input)."""
    m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)["\']', html)
    return m.group(1) if m else ""


def _extract_meta_csrf(html: str) -> str:
    """Extract CSRF token from <meta name="csrf-token" content="...">."""
    m = re.search(r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']', html)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# User class
# ---------------------------------------------------------------------------


class DashboardUser(HttpUser):
    """
    Simulates a dashboard viewer who:
      1. Logs in
      2. Fetches the dashboard list
      3. Loads random dashboard configs
      4. Fires the actual chart queries from each dashboard

    This produces realistic load because it exercises the exact queries
    that real dashboards trigger.
    """

    wait_time = between(2, 5)

    # Prepended to every request name. The ProbeUser subclass sets "probe:" so
    # its rows are separable in the stats-history CSV (degradation report).
    name_prefix = ""

    def on_start(self):
        self._dashboards: list[dict] = []
        self._dashboard_configs: dict[str, dict] = {}
        self._username, self._password = self._select_identity()
        self._login()
        self._load_dashboard_list()

    def _n(self, name: str) -> str:
        """Apply the per-class stat-name prefix."""
        return f"{self.name_prefix}{name}"

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _select_identity(self) -> tuple[str, str]:
        """Pick this simulated user's login. Pool mode draws round-robin from
        the seeded identity CSV; missing/empty pool falls back to the single
        env identity (failure-soft — the run still measures something)."""
        if _IDENTITY_MODE == "pool":
            identity = identity_pool.default_pool().next()
            if identity is not None:
                return identity.username, identity.password
        return _USERNAME, _PASSWORD

    def _login(self):
        if not self._password:
            return
        resp = self.client.get(
            "/login",
            name=self._n("/login [GET]"),
        )
        if resp.status_code != 200:
            return
        csrf = _extract_csrf(resp.text)
        # The POST redirects to /dashboard on success (allow_redirects=True),
        # so the final response body contains the CSRF meta tag — extract it
        # here instead of making a separate request.
        resp = self.client.post(
            "/login",
            data={"username": self._username, "password": self._password, "csrf_token": csrf},
            name=self._n("/login [POST]"),
            allow_redirects=True,
        )
        self._set_csrf_from_response(resp)

    def _set_csrf_from_response(self, resp):
        """Extract the CSRF token from any HTML response's <meta> tag and set
        it as the default X-CSRF-Token header for all subsequent requests."""
        if resp.status_code == 200:
            token = _extract_meta_csrf(resp.text)
            if token:
                self.client.headers["X-CSRF-Token"] = token

    # ------------------------------------------------------------------
    # Dashboard discovery
    # ------------------------------------------------------------------

    def _load_dashboard_list(self):
        resp = self.client.get(
            f"{_PREFIX}/dashboard-builder/api/dashboards",
            name=self._n("api/dashboards [list]"),
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                self._dashboards = [d for d in data.get("dashboards", []) if d.get("id")]
            except Exception:
                pass

    def _get_dashboard_config(self, dashboard_id: str) -> dict | None:
        """Fetch and cache a dashboard config."""
        if dashboard_id in self._dashboard_configs:
            return self._dashboard_configs[dashboard_id]
        resp = self.client.get(
            f"{_PREFIX}/dashboard-builder/api/dashboards/{dashboard_id}",
            name=self._n("api/dashboards/[id]"),
        )
        if resp.status_code != 200:
            return None
        try:
            cfg = resp.json()
            self._dashboard_configs[dashboard_id] = cfg
            return cfg
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Page navigation
    # ------------------------------------------------------------------

    @task(3)
    def view_dashboards_page(self):
        """Load the main dashboard HTML page."""
        resp = self.client.get("/dashboard", name=self._n("/dashboard"))
        # Opportunistically refresh the CSRF token on every page load, so
        # even if the initial login redirect failed we eventually pick it up.
        if "X-CSRF-Token" not in self.client.headers:
            self._set_csrf_from_response(resp)

    @task(1)
    def get_filter_options(self):
        """Fetch org/team filter options."""
        self.client.get(
            f"{_PREFIX}/api/dashboards/filter-options",
            name=self._n("api/filter-options"),
        )

    # ------------------------------------------------------------------
    # Realistic dashboard load — pick a random dashboard and fire its queries
    # ------------------------------------------------------------------

    @task(8)
    def load_random_dashboard(self):
        """Pick a random dashboard, load its config, and fire all its chart queries."""
        if not self._dashboards:
            self._load_dashboard_list()
            if not self._dashboards:
                return

        dash = random.choice(self._dashboards)
        config = self._get_dashboard_config(dash["id"])
        if not config:
            return

        charts = config.get("charts", [])
        if isinstance(charts, dict):
            charts = list(charts.values())

        title = config.get("title", "unknown")[:30]

        # Chart types that are rendered server-side or use dedicated endpoints —
        # sending them to /api/query causes 500 errors.
        _skip_types = {
            "report",
            "google_report",
            "combined_report",
            "canva",
            "grouped_table",
        }

        for chart in charts:
            chart_type = chart.get("chart_type", "")
            if chart_type in _skip_types:
                continue
            if chart_type in ("map", "leaflet_map"):
                self._query_leaflet_map_chart(chart, title)
            elif chart_type == "trend_line":
                self._query_trend_line(chart, title)
            else:
                self._query_chart(chart, title)

    # ------------------------------------------------------------------
    # Chart query helpers
    # ------------------------------------------------------------------

    def _query_chart(self, chart: dict, dash_title: str):
        """Fire a standard chart query (bar, metric, pie, table, etc.)."""
        chart_type = chart.get("chart_type", "unknown")
        payload = {
            "chart_type": chart_type,
            "data_view": chart.get("data_view", ""),
            "x_field": chart.get("x_field"),
            "x_field_type": chart.get("x_field_type"),
            "y_fields": chart.get("y_fields", []),
            "y_field_types": chart.get("y_field_types", {}),
            "aggregation": chart.get("aggregation", "sum"),
            "date_range": chart.get("date_range") or _default_date_range(),
            "filters": chart.get("filters", []),
            "limit": chart.get("limit", 20),
        }
        # Include table_columns for table charts
        if chart_type == "table" and chart.get("table_columns"):
            payload["table_columns"] = chart["table_columns"]

        self.client.post(
            f"{_PREFIX}/dashboard-builder/api/query",
            json=payload,
            name=self._n(f"query [{dash_title}:{chart_type}]"),
        )

    def _query_trend_line(self, chart: dict, dash_title: str):
        """Fire a trend line query."""
        sources = chart.get("sources") or chart.get("trend_sources", [])
        if not sources:
            # Fall back to building a single source from chart fields
            sources = [
                {
                    "data_view": chart.get("data_view", ""),
                    "field": (chart.get("y_fields") or [None])[0],
                    "date_field": chart.get("date_field", "kibana_date"),
                    "label": chart.get("title", ""),
                }
            ]

        payload = {
            "sources": sources,
            "date_range": chart.get("date_range") or _default_date_range(),
            "date_interval": chart.get("date_interval", "month"),
            "filters": chart.get("filters", []),
        }
        self.client.post(
            f"{_PREFIX}/dashboard-builder/api/trend-line-data",
            json=payload,
            name=self._n(f"trend [{dash_title}]"),
        )

    def _query_leaflet_map_chart(self, chart: dict, dash_title: str):
        """Fire map-data queries for a leaflet_map chart's statistics."""
        stats = chart.get("map_statistics", [])
        if not stats:
            stat_key = chart.get("map_statistic", "reach")
            stats = [{"key": stat_key}]

        dr = chart.get("date_range") or _default_date_range()
        date_from = dr.get("from", "now-30d")
        date_to = dr.get("to", "now")

        for stat in stats:
            key = stat.get("key") or stat.get("statistic", "reach")
            self.client.get(
                f"{_PREFIX}/dashboard-builder/api/map-data" f"?statistic={key}&date_from={date_from}&date_to={date_to}",
                name=self._n(f"map [{dash_title}:{key}]"),
            )


def _default_date_range():
    """Fallback date range when the chart config doesn't specify one."""
    return {"field": "kibana_date", "from": "now-30d", "to": "now"}


# ---------------------------------------------------------------------------
# Stepped baseline→ramp profile — only when LOAD_TEST_PROFILE=steps
# ---------------------------------------------------------------------------
# Both classes are defined conditionally so classic mode is byte-for-byte the
# earlier behavior: locust auto-discovers any LoadTestShape subclass at
# import (which would silently override --users/--spawn-rate/--run-time), and
# an always-present fixed_count=1 ProbeUser would steal one slot from every
# classic run.

if _PROFILE == "steps":
    _STEP_USERS = step_profile.parse_step_users(os.environ.get("LOAD_TEST_STEP_USERS", step_profile.DEFAULT_STEP_USERS))
    _STEP_SECONDS = step_profile.parse_step_seconds(
        os.environ.get("LOAD_TEST_STEP_SECONDS", str(step_profile.DEFAULT_STEP_SECONDS))
    )

    class ProbeUser(DashboardUser):
        """The one-person baseline. Exactly one probe exists (fixed_count) —
        it is the sole user during step 1 and keeps browsing unchanged while
        background DashboardUsers pile on, so its "probe:"-prefixed stats are
        "what a real person experiences" at every load level.

        Verified (smoke, locust 2.44.4): with a LoadTestShape, the fixed_count
        user is spawned first, so shape step 1 (user count 1) is the probe
        alone, and later steps add only DashboardUsers.
        """

        fixed_count = 1
        # Slower than the background users: the probe is a measurement
        # instrument, not a load generator.
        wait_time = between(5, 10)
        name_prefix = "probe:"

        def _select_identity(self) -> tuple[str, str]:
            # Always the env identity, even in pool mode — a probe whose scope
            # varied run-to-run would make baselines incomparable.
            return _USERNAME, _PASSWORD

    class StagesShape(LoadTestShape):
        """Steps the TOTAL user count through LOAD_TEST_STEP_USERS, dwelling
        LOAD_TEST_STEP_SECONDS on each; returning None ends the run."""

        def tick(self):
            return step_profile.tick(self.get_run_time(), _STEP_USERS, _STEP_SECONDS)

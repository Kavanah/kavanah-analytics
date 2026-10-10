"""
Kibana Service
Handles Kibana API operations for dashboard management.
"""

import json
import os
import re
import requests
import logging

from asset_management.web.config import DEFAULT_KIBANA_URL


# Kibana space IDs are documented as lowercase alphanumeric +
# hyphens, max 13 characters. Validate before any space_id flows into a
# URL path so a value like "../../api/security/users" can't redirect the
# request (CodeQL py/partial-ssrf).
_SPACE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _validated_space_id(space_id):
    """Return space_id if it's safe to interpolate into a Kibana URL,
    else 'default'. Treats None/empty as 'default' so callers don't have
    to pre-check."""
    if not space_id or space_id == "default":
        return "default"
    if isinstance(space_id, str) and _SPACE_ID_RE.match(space_id):
        return space_id
    logging.warning("KibanaService: rejecting invalid space_id=%r, using 'default'", space_id)
    return "default"


def kibana_auth(headers):
    """Resolve the credential for an internal Kibana API request.

    The app stack historically authenticated to Kibana as the built-in
    ``elastic`` superuser (HTTP Basic, ``ES01_PASSWORD``). De-superuser-ing
    lets a scoped service account / API key take over when its env vars are
    set, falling back to ``elastic`` so current deploys are byte-identical
    until the scoped credential is provisioned (Phase 2). Shared by every
    requests-based Kibana caller (this service, dashboard_builder, web/app.py)
    so they can't drift.

    Precedence (first set wins):
      1. ``KIBANA_APP_API_KEY``                       → adds ``Authorization: ApiKey <key>``
                                                        to ``headers``; auth value is None
      2. ``KIBANA_APP_USER`` + ``KIBANA_APP_PASSWORD`` → HTTP Basic (user, password)
      3. ``elastic`` / ``ES01_PASSWORD`` (fallback)    → HTTP Basic (elastic, password)

    Pass the already-built request ``headers`` dict — the API-key branch
    mutates it in place. Returns ``(auth, configured)`` where ``auth`` is the
    value to pass as ``requests(..., auth=...)`` (None when an Authorization
    header was set instead) and ``configured`` is False only when NO credential
    resolves at all (no API key, no scoped basic, no ``ES01_PASSWORD``) — the
    signal for callers to bail exactly as the old ``if not es_password`` guard
    did, rather than fire an unauthenticated request.
    """
    api_key = os.environ.get("KIBANA_APP_API_KEY")
    if api_key:
        headers["Authorization"] = f"ApiKey {api_key}"
        return (None, True)
    user = os.environ.get("KIBANA_APP_USER")
    password = os.environ.get("KIBANA_APP_PASSWORD")
    if user and password:
        return ((user, password), True)
    es_password = os.environ.get("ES01_PASSWORD")
    if not es_password:
        return (None, False)
    return (("elastic", es_password), True)


# Default CA bundle mounted into the container (same path the Elasticsearch client
# verifies against in lib.es_helpers.get_elasticsearch_client).
_KIBANA_CA_CERT_PATH = "/app/data/certs/es01_http_ca.crt"


def kibana_tls_verify():
    """Resolve the ``verify`` argument for outbound Kibana requests.

     (DBUILD-001) / SVC-003: these calls carry the elastic superuser
    password, so TLS verification must not be silently disabled (was hardcoded
    ``verify=False``).

    Resolution order:
      1. ``KIBANA_CA_CERT`` env var, if set and the file exists -> verify against it.
      2. The standard container CA bundle, if present -> verify against it.
      3. Otherwise -> ``True`` (system trust store; works for the public
         dev-reporting host's CA-issued cert).

    The deployed KIBANA_URL is plain ``http://kib01:5601`` (docker-compose), so
    ``verify`` is a no-op there; it matters only when KIBANA_URL is https://.

    Escape hatch: ``KIBANA_TLS_INSECURE=1`` restores skip-verification for an
    environment that genuinely needs it, logged loudly so it is never silent.

    Shared so the cluster's Kibana callers (dashboard_builder, this service's own
    SVC-003 calls, and eventually analytics.routes) don't drift apart.
    """
    if os.environ.get("KIBANA_TLS_INSECURE") == "1":
        logging.warning("KIBANA_TLS_INSECURE=1 — Kibana TLS verification is DISABLED")
        return False
    ca_path = os.environ.get("KIBANA_CA_CERT") or _KIBANA_CA_CERT_PATH
    if ca_path and os.path.exists(ca_path):
        return ca_path
    return True


class KibanaService:
    """Service for Kibana API operations."""

    @staticmethod
    def get_dashboards_in_space(space_id="default"):
        """Get list of dashboards in a Kibana space.

        Args:
            space_id: Kibana space ID (default: 'default')

        Returns:
            list: List of dashboard dictionaries with 'id', 'title', 'description'
        """
        space_id = _validated_space_id(space_id)
        # Get Kibana URL from environment
        kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

        # Build API endpoint for saved objects
        if space_id and space_id != "default":
            api_url = f"{kibana_url}/s/{space_id}/api/saved_objects/_find?type=dashboard&per_page=100"
        else:
            api_url = f"{kibana_url}/api/saved_objects/_find?type=dashboard&per_page=100"

        headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
        # scoped service account / API key, falling back to elastic.
        auth, configured = kibana_auth(headers)
        if not configured:
            logging.error("No Kibana credential configured (KIBANA_APP_* / ES01_PASSWORD)")
            return []

        try:
            response = requests.get(api_url, headers=headers, auth=auth, verify=kibana_tls_verify())

            if response.status_code == 200:
                data = response.json()
                dashboards = []
                for obj in data.get("saved_objects", []):
                    attrs = obj.get("attributes", {})
                    title = attrs.get("title", "Untitled")
                    # No filtering here - all filtering handled by DashboardService
                    dashboards.append(
                        {
                            "id": obj.get("id"),
                            "title": title,
                            "description": attrs.get("description", ""),
                        }
                    )
                # Sort alphabetically by title
                dashboards.sort(key=lambda x: x["title"].lower())
                return dashboards
            else:
                logging.error(f"Failed to fetch dashboards: {response.status_code} - {response.text}")
                return []

        except Exception as e:
            logging.error(f"Error fetching dashboards from space {space_id}: {str(e)}")
            return []

    @staticmethod
    def get_dashboard_panels(dashboard_id, space_id="default"):
        """Fetch dashboard panel information from Kibana API.

        Args:
            dashboard_id: Kibana dashboard ID
            space_id: Kibana space ID (default: 'default')

        Returns:
            dict: Panel information with 'panel_count' and 'first_panel_id' keys,
                  or None if error
        """
        space_id = _validated_space_id(space_id)
        try:
            kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

            # Build URL for space
            if space_id and space_id != "default":
                url = f"{kibana_url}/s/{space_id}/api/saved_objects/dashboard/{dashboard_id}"
            else:
                url = f"{kibana_url}/api/saved_objects/dashboard/{dashboard_id}"

            headers = {"kbn-xsrf": "true"}
            # scoped service account / API key, falling back to elastic.
            auth, configured = kibana_auth(headers)
            if not configured:
                logging.error("No Kibana credential configured (KIBANA_APP_* / ES01_PASSWORD)")
                return None
            response = requests.get(url, auth=auth, headers=headers, verify=kibana_tls_verify())

            if response.status_code == 200:
                data = response.json()
                attrs = data.get("attributes", {})
                panels_json = attrs.get("panelsJSON", "[]")

                # Parse panels
                import json

                panels = json.loads(panels_json)

                panel_count = len(panels)
                first_panel_id = None

                if panel_count > 0:
                    first_panel_id = panels[0].get("panelIndex", panels[0].get("id"))

                return {
                    "panel_count": panel_count,
                    "first_panel_id": first_panel_id,
                    "panels": panels,
                }
            else:
                logging.error(f"Failed to fetch dashboard panels: {response.status_code} - {response.text}")
                return None

        except Exception as e:
            logging.error(f"Error fetching dashboard panels for {dashboard_id}: {str(e)}")
            return None

    @staticmethod
    def resolve_data_view_id(data_view_id, space_id="default"):
        """Resolve a Kibana data view UUID to its ES index pattern title.

        Args:
            data_view_id: Kibana data view UUID (e.g., '6b2a9f4b-...')
            space_id: Kibana space ID

        Returns:
            str: ES index pattern title (e.g., 'google_ads*'), or the original
                 UUID unchanged if the lookup fails.
        """
        space_id = _validated_space_id(space_id)
        try:
            kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

            if space_id and space_id != "default":
                url = f"{kibana_url}/s/{space_id}/api/data_views/data_view/{data_view_id}"
            else:
                url = f"{kibana_url}/api/data_views/data_view/{data_view_id}"

            headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
            # scoped service account / API key, falling back to elastic.
            auth, configured = kibana_auth(headers)
            if not configured:
                return data_view_id
            response = requests.get(url, headers=headers, auth=auth, verify=kibana_tls_verify())

            if response.status_code == 200:
                title = response.json().get("data_view", {}).get("title")
                if title:
                    logging.info(f"Resolved data_view {data_view_id} → {title}")
                    return title

            logging.warning(f"Could not resolve data_view {data_view_id}: {response.status_code}")
            return data_view_id

        except Exception as e:
            logging.error(f"Error resolving data_view {data_view_id}: {e}")
            return data_view_id

    @staticmethod
    def get_full_dashboard_with_visualizations(dashboard_id, space_id="default"):
        """Fetch a Kibana dashboard's panels and each referenced visualization's full saved object.

        Args:
            dashboard_id: Kibana dashboard UUID
            space_id: Kibana space ID (default: 'default')

        Returns:
            dict: {
                'title': str,
                'panels': [{'type': str, 'title': str, 'viz': saved_object_dict}]
            }
            or None on error.
        """
        space_id = _validated_space_id(space_id)
        try:
            kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

            headers = {"kbn-xsrf": "true", "Content-Type": "application/json"}
            # scoped service account / API key, falling back to elastic.
            # Resolved once; reused for the dashboard fetch and every viz fetch.
            auth, configured = kibana_auth(headers)
            if not configured:
                logging.error("No Kibana credential configured (KIBANA_APP_* / ES01_PASSWORD)")
                return None

            # Fetch the dashboard saved object
            if space_id and space_id != "default":
                dash_url = f"{kibana_url}/s/{space_id}/api/saved_objects/dashboard/{dashboard_id}"
            else:
                dash_url = f"{kibana_url}/api/saved_objects/dashboard/{dashboard_id}"

            resp = requests.get(dash_url, headers=headers, auth=auth, verify=kibana_tls_verify())
            if resp.status_code != 200:
                logging.error(f"Failed to fetch dashboard {dashboard_id}: {resp.status_code} {resp.text}")
                return None

            dash_data = resp.json()
            attrs = dash_data.get("attributes", {})
            dash_title = attrs.get("title", "Untitled Dashboard")
            panels_json = attrs.get("panelsJSON", "[]")
            panels_raw = json.loads(panels_json)

            # Build reference map from the top-level 'references' array (newer format)
            # References map panelRefName -> {id, type}
            ref_map = {}
            for ref in dash_data.get("references", []):
                ref_map[ref.get("name", "")] = {
                    "id": ref.get("id"),
                    "type": ref.get("type"),
                }

            logging.info(f"Dashboard {dashboard_id}: found {len(panels_raw)} panels in panelsJSON")

            result_panels = []
            for panel in panels_raw:
                panel_type = panel.get("type", "")
                panel_title = panel.get("title", "")

                # Get the embeddable config if present
                embeddable_config = panel.get("embeddableConfig", {})
                if not panel_title:
                    panel_title = embeddable_config.get("title", "")

                # ── By-value panel (Kibana 8.x inline format) ──────────────────
                # The full Lens config is embedded in embeddableConfig.attributes.
                # There is no separate saved object to fetch.
                embedded_attrs = embeddable_config.get("attributes", {})
                if embedded_attrs.get("visualizationType") or embedded_attrs.get("type") == "lens":
                    if not panel_title:
                        panel_title = embedded_attrs.get("title", "")
                    logging.info(
                        f"Panel '{panel_title}': by-value, visualizationType={embedded_attrs.get('visualizationType')}"
                    )
                    result_panels.append(
                        {
                            "type": panel_type or "lens",
                            "title": panel_title,
                            "viz": {"attributes": embedded_attrs},
                        }
                    )
                    continue

                # ── By-reference panel ─────────────────────────────────────────
                # Resolve viz ID via panelRefName (newer format) or direct id field.
                viz_id = panel.get("id")
                viz_type = panel_type

                panel_ref_name = panel.get("panelRefName")
                if panel_ref_name and panel_ref_name in ref_map:
                    ref = ref_map[panel_ref_name]
                    viz_id = ref["id"]
                    viz_type = ref["type"]

                if not viz_id or not viz_type:
                    logging.info(f"Skipping panel (no viz reference and no embedded config): {json.dumps(panel)[:200]}")
                    continue

                # Fetch the full visualization saved object
                if space_id and space_id != "default":
                    viz_url = f"{kibana_url}/s/{space_id}/api/saved_objects/{viz_type}/{viz_id}"
                else:
                    viz_url = f"{kibana_url}/api/saved_objects/{viz_type}/{viz_id}"

                viz_resp = requests.get(viz_url, headers=headers, auth=auth, verify=kibana_tls_verify())
                if viz_resp.status_code != 200:
                    logging.warning(f"Could not fetch viz {viz_type}/{viz_id}: {viz_resp.status_code}")
                    continue

                viz_obj = viz_resp.json()
                if not panel_title:
                    panel_title = viz_obj.get("attributes", {}).get("title", "")

                logging.info(
                    f"Panel '{panel_title}': by-reference {viz_type}/{viz_id}, visualizationType={viz_obj.get('attributes', {}).get('visualizationType')}"
                )
                result_panels.append(
                    {
                        "type": viz_type,
                        "title": panel_title,
                        "viz": viz_obj,
                    }
                )

            return {"title": dash_title, "panels": result_panels}

        except Exception as e:
            logging.error(f"Error fetching full dashboard with visualizations for {dashboard_id}: {e}")
            return None

    @staticmethod
    def delete_dashboard(dashboard_id, space_id="default"):
        """Delete a dashboard from Kibana by its saved object ID.

        Args:
            dashboard_id: Kibana dashboard UUID
            space_id: Kibana space ID (default: 'default')

        Returns:
            bool: True if deleted successfully, False otherwise
        """
        space_id = _validated_space_id(space_id)
        kibana_url = os.environ.get("KIBANA_URL", DEFAULT_KIBANA_URL)

        if space_id and space_id != "default":
            url = f"{kibana_url}/s/{space_id}/api/saved_objects/dashboard/{dashboard_id}"
        else:
            url = f"{kibana_url}/api/saved_objects/dashboard/{dashboard_id}"

        headers = {"kbn-xsrf": "true"}
        # scoped service account / API key, falling back to elastic.
        auth, configured = kibana_auth(headers)
        if not configured:
            logging.error("No Kibana credential configured (KIBANA_APP_* / ES01_PASSWORD)")
            return False

        try:
            response = requests.delete(url, headers=headers, auth=auth, verify=kibana_tls_verify())
            if response.status_code in (200, 204):
                logging.info(f"Deleted Kibana dashboard {dashboard_id} from space {space_id}")
                return True
            else:
                logging.error(f"Failed to delete Kibana dashboard {dashboard_id}: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logging.error(f"Error deleting Kibana dashboard {dashboard_id}: {e}")
            return False

"""
Role Management Service
Lists non-system Elasticsearch security roles for the Roles tab.

earlier this module is a thin wrapper around ``es.security.get_role``
plus the system-role allowlist used by ``RolePermissionsService`` to
validate custom role names. The legacy ``create_org_role`` /
``create_team_role`` / ``sync_all_roles`` / ``_put_role`` methods that
auto-created ES Security roles for each org/team were retired with the
new authz model — scope membership now lives in ``de_authz_user_*_role``.
"""

import logging
import sys

sys.path.insert(0, "/app/python")
from lib.es_helpers import get_elasticsearch_client

# ES built-in / system role names to exclude when listing roles
_SYSTEM_ROLE_PREFIXES = (
    ".",
    "kibana_",
    "logstash_",
    "beats_",
    "apm_",
    "ml_",
    "monitoring_",
    "reporting_",
    "snapshot_",
    "enrich_",
    "inference_",
    "fleet_",
    "remote_monitoring_",
    "data_frame_transforms_",
    "watcher_",
    # km-infra platform roles (SIEM/security tooling on the ES
    # cluster, e.g. km_siem_security_reader) are created directly on the
    # cluster by the platform layer, not by this app. Exclude them so they
    # don't leak into the Edit Roles picker. The app never creates a role
    # with a km_ prefix (app roles are org_*, team_*, or custom names).
    "km_",
)
_SYSTEM_ROLE_NAMES = {
    "elastic",
    "superuser",
    "transport_client",
    "kibana_admin",
    "kibana_system",
    "logstash_system",
    "beats_system",
    "apm_system",
    "remote_monitoring_agent",
    "cross_cluster_search",
    "cross_cluster_replication",
    "viewer",
    "editor",
    "asset_admin",
    "ingest_admin",
    "machine_learning_admin",
    "machine_learning_user",
    "rollup_admin",
    "rollup_user",
    "transform_admin",
    "transform_user",
}


class RoleManagementService:
    """Lists non-system ES security role names for the Roles tab."""

    # -------------------------------------------------------------------------
    # ES role listing
    # -------------------------------------------------------------------------

    @staticmethod
    def get_all_role_names() -> set:
        """
        Return the set of all non-system Elasticsearch role names.
        Includes org_*, team_*, legacy named roles, etc.
        """
        es = get_elasticsearch_client()
        if es is None:
            logging.error("RoleManagementService: could not connect to ES")
            return set()
        try:
            response = es.security.get_role()
            names = set()
            for name in response:
                if name in _SYSTEM_ROLE_NAMES:
                    continue
                if any(name.startswith(p) for p in _SYSTEM_ROLE_PREFIXES):
                    continue
                names.add(name)
            return names
        except Exception as e:
            logging.error(f"RoleManagementService: could not list roles: {e}")
            return set()

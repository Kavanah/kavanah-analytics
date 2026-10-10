"""
Index pattern resolution for role-scoped access control.

Resolves the current user's roles into ES index patterns. After the
org_admin role was retired, the only live paths are the unrestricted
admin case and the wildcard case for regular org/team users (data is
already filtered at query level by ``RoleFilterService``). The
team-prefix branch is retained as scaffolding in case index-level
isolation is reintroduced.

Role → Index Pattern:
    superuser / asset_admin  →  "*"  (all indices)
    other                    →  "*"  (filtered at query level)
"""

import fnmatch
import logging
import sys

from flask import session

sys.path.insert(0, "/app/python")
from lib.es_helpers import _EMPTY_SCOPE_PATTERN, get_elasticsearch_client, sanitize_team_title

# Roles that get unrestricted index access
_UNRESTRICTED_ROLES = {"superuser", "asset_admin"}

# Shared data indices that all authenticated users may query.
# These don't follow team-prefix naming but are safe because the dashboard
# viewer applies org/team filters at query level.
_SHARED_INDEX_PATTERNS = {
    "disciple-tools",
    "disciple-tools-*",
}


def get_index_pattern():
    """Return the ES index pattern the current user is allowed to query.

    Results are cached in the Flask session under ``_index_pattern`` to avoid
    repeated ES lookups within the same session.

    Returns:
        str: Comma-separated index pattern (e.g. ``"teamA_*,teamB_*"``),
             ``"*"`` for unrestricted users, or ``""`` for deny-all.
    """
    cached = session.get("_index_pattern")
    if cached is not None:
        return cached

    roles = session.get("roles", [])
    pattern = _resolve_pattern(roles)
    session["_index_pattern"] = pattern
    return pattern


def clear_cached_pattern():
    """Invalidate the cached index pattern (call after role changes)."""
    session.pop("_index_pattern", None)


def validate_index_access(requested_index):
    """Check whether the current user may read *requested_index*.

     (2026-09-08): this is now a real check. The original body resolved
    roles to team-title prefixes for the ``org_admin`` role; once that role was
    retired every other user resolved to ``"*"`` and the function returned True
    for everyone — the four call sites that "validated" index access were
    validating nothing. The authoritative tenant boundary is the
    Layer-1 scope (``UserScope.allowed_indices``, the concrete
    ``{team_esid}__{source}`` names the user may read), so this asks that
    resolver: the request is allowed iff its scoped rewrite is non-empty.

    Semantics:
      * a concrete index name — allowed iff it is in ``allowed_indices``;
      * a wildcard / comma-list pattern (``"*__fb_insights"``) — allowed iff at
        least one allowed index matches, so a team member's generic data view
        passes and the caller then queries the SCOPED rewrite, never the raw
        pattern;
      * no authenticated user (anonymous, or outside a request) — denied.
        Public report rendering does not call this; it reads the index from
        the stored publish, which was validated when it was saved;
      * the legacy shared Disciple.Tools patterns keep their historical
        allow (query-level filters scope them).

    Args:
        requested_index: An ES index name or pattern from the request.

    Returns:
        bool: ``True`` if access is permitted, ``False`` otherwise.
    """
    if not requested_index or not isinstance(requested_index, str):
        return False
    requested = requested_index.strip()
    if not requested:
        return False

    # Shared indices are accessible to all authenticated users (legacy DT).
    if any(fnmatch.fnmatch(requested, sp) for sp in _SHARED_INDEX_PATTERNS):
        return True

    try:
        username = session.get("username", "")
    except RuntimeError:
        # Outside a request context there is no user to authorise.
        return False
    if not username:
        return False

    from .role_filter_service import RoleFilterService

    scoped = RoleFilterService.scoped_index_pattern(requested)
    return scoped != _EMPTY_SCOPE_PATTERN


def get_allowed_team_prefixes():
    """Return the list of sanitized team-title prefixes for an org_admin.

    Returns:
        list[str] | None: List of prefixes, or ``None`` for unrestricted users.
    """
    roles = session.get("roles", [])
    if _UNRESTRICTED_ROLES.intersection(roles):
        return None

    if "org_admin" not in roles:
        return []

    return _resolve_team_prefixes(roles)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_pattern(roles):
    """Build the index pattern string from a list of ES roles.

    Index-level isolation only applies to org_admin users.  Regular org/team
    users are already filtered at query level by RoleFilterService, so they
    get unrestricted index access (``"*"``).
    """
    if _UNRESTRICTED_ROLES.intersection(roles):
        return "*"

    if "org_admin" not in roles:
        # Regular org/team users — data filtering handled at query level
        return "*"

    prefixes = _resolve_team_prefixes(roles)
    if not prefixes:
        logging.warning("org_admin user has no resolvable team prefixes")
        return ""

    patterns = [f"{prefix}_*" for prefix in prefixes]
    return ",".join(patterns)


def _resolve_team_prefixes(roles):
    """Resolve org roles → team titles → sanitized prefixes."""
    from .role_filter_service import RoleFilterService

    org_ids = RoleFilterService.extract_org_ids_from_roles(roles)
    if not org_ids:
        logging.warning("org_admin user has no org_* roles — cannot resolve teams")
        return []

    org_titles = RoleFilterService.get_org_titles_by_ids(org_ids)
    if not org_titles:
        logging.warning(f"No ES organizations found for org ids: {org_ids}")
        return []

    team_titles = RoleFilterService.get_team_titles_by_organization(org_titles)
    if not team_titles:
        logging.warning(f"No teams found for organizations: {org_titles}")
        return []

    prefixes = sorted({sanitize_team_title(t) for t in team_titles})
    logging.info(f"Resolved org_admin index prefixes: {prefixes} " f"(orgs={org_titles}, teams={team_titles})")
    return prefixes

"""Unit tests for tools/cleanup_legacy_org_team_roles.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Resolve bin/python so `tools.*` and `lib.*` are importable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "bin" / "python"))

from tools.cleanup_legacy_org_team_roles import (  # noqa: E402
    _is_legacy_role,
    cleanup_de_role_permissions,
    cleanup_es_roles,
    cleanup_user_roles,
)


# ---------------------------------------------------------------------------
# Regex / preserve-list behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        # Legacy roles — should match.
        ("org_VOGZ", True),
        ("team_NFCY", True),
        ("org_NFCY_user", True),
        ("team_some_team_with_underscores", True),
        # New-model roles that happen to share the prefix — MUST NOT match.
        ("org_admin", False),
        ("team_admin", False),
        # Other new-model roles — don't match anyway.
        ("owner", False),
        ("editor", False),
        ("viewer", False),
        # ES system / built-in roles.
        ("superuser", False),
        ("asset_admin", False),
        # Boundary: trailing nothing after the underscore — not a legacy role.
        ("org_", False),
        ("team_", False),
        # Words that start with the same letters but aren't the prefix.
        ("organization_owner", False),
        ("teaming_role", False),
    ],
)
def test_is_legacy_role(name: str, expected: bool) -> None:
    assert _is_legacy_role(name) is expected


# ---------------------------------------------------------------------------
# Phase 1 — user records
# ---------------------------------------------------------------------------


def test_cleanup_user_roles_dry_run_produces_zero_writes() -> None:
    es = MagicMock()
    es.security.get_user.return_value = {
        "alice": {"roles": ["owner", "org_VOGZ", "team_NFCY"], "enabled": True},
        "bob": {"roles": ["org_admin", "viewer"], "enabled": True},  # all preserved
        "carol": {"roles": ["editor", "team_NFCY"], "enabled": True},
    }

    summary = cleanup_user_roles(es, apply=False, limit=None)

    es.security.put_user.assert_not_called()
    assert summary["users_with_legacy"] == 2  # alice + carol
    assert summary["legacy_assignments"] == 3  # org_VOGZ, team_NFCY (alice), team_NFCY (carol)
    assert summary["would_update"] == 2
    assert summary["updated"] == 0
    assert summary["errors"] == 0


def test_cleanup_user_roles_apply_strips_legacy_keeps_others() -> None:
    es = MagicMock()
    es.security.get_user.return_value = {
        "alice": {
            "roles": ["owner", "org_VOGZ", "team_NFCY"],
            "enabled": True,
            "full_name": "Alice",
            "email": "a@example.com",
            "metadata": {"_reserved": False},
        },
    }

    summary = cleanup_user_roles(es, apply=True, limit=None)

    es.security.put_user.assert_called_once()
    kwargs = es.security.put_user.call_args.kwargs
    assert kwargs["username"] == "alice"
    assert kwargs["roles"] == ["owner"]
    # Carry-through fields must be preserved.
    assert kwargs["enabled"] is True
    assert kwargs["full_name"] == "Alice"
    assert kwargs["email"] == "a@example.com"

    assert summary["users_with_legacy"] == 1
    assert summary["legacy_assignments"] == 2
    assert summary["updated"] == 1
    assert summary["errors"] == 0


def test_cleanup_user_roles_idempotent_on_clean_users() -> None:
    es = MagicMock()
    es.security.get_user.return_value = {
        "alice": {"roles": ["owner", "org_admin"], "enabled": True},
        "bob": {"roles": ["viewer"], "enabled": True},
    }

    summary = cleanup_user_roles(es, apply=True, limit=None)

    es.security.put_user.assert_not_called()
    assert summary["users_with_legacy"] == 0
    assert summary["updated"] == 0


# ---------------------------------------------------------------------------
# Phase 2 — ES Security role definitions
# ---------------------------------------------------------------------------


def test_cleanup_es_roles_dry_run() -> None:
    es = MagicMock()
    es.security.get_role.return_value = {
        "owner": {},
        "org_admin": {},  # preserved
        "team_admin": {},  # preserved
        "org_VOGZ": {},  # legacy
        "team_NFCY": {},  # legacy
        "superuser": {},
    }

    summary = cleanup_es_roles(es, apply=False, limit=None)

    es.security.delete_role.assert_not_called()
    assert summary["legacy_found"] == 2
    assert summary["would_delete"] == 2
    assert summary["deleted"] == 0


def test_cleanup_es_roles_apply_deletes_only_legacy() -> None:
    es = MagicMock()
    es.security.get_role.return_value = {
        "owner": {},
        "org_admin": {},
        "team_admin": {},
        "org_VOGZ": {},
        "team_NFCY": {},
    }

    summary = cleanup_es_roles(es, apply=True, limit=None)

    deleted_names = sorted(
        c.kwargs["name"] for c in es.security.delete_role.call_args_list
    )
    assert deleted_names == ["org_VOGZ", "team_NFCY"]
    assert summary["deleted"] == 2
    assert summary["errors"] == 0


# ---------------------------------------------------------------------------
# Phase 3 — de_role_permissions
# ---------------------------------------------------------------------------


def test_cleanup_de_role_permissions_skips_when_index_missing() -> None:
    es = MagicMock()
    es.indices.exists.return_value = False

    summary = cleanup_de_role_permissions(es, apply=False, limit=None)

    es.search.assert_not_called()
    assert summary == {
        "scanned": 0,
        "legacy_found": 0,
        "would_delete": 0,
        "deleted": 0,
        "errors": 0,
    }


def test_cleanup_de_role_permissions_sorts_on_role_name_not_id() -> None:
    # Regression: sorting on _id requires fielddata on _id, which ES 8.x
    # disallows by default. The first dev dry-run crashed with HTTP 400 on
    # Phase 3 because of this. Pin the sort field to role_name (keyword).
    es = MagicMock()
    es.indices.exists.return_value = True
    es.search.return_value = {"hits": {"hits": []}}

    cleanup_de_role_permissions(es, apply=False, limit=None)

    body = es.search.call_args.kwargs["body"]
    assert body["sort"] == [{"role_name": "asc"}]


def test_cleanup_de_role_permissions_apply_deletes_only_legacy() -> None:
    es = MagicMock()
    es.indices.exists.return_value = True
    es.search.return_value = {
        "hits": {
            "hits": [
                {"_id": "owner", "_source": {"role_name": "owner"}, "sort": ["owner"]},
                {
                    "_id": "org_VOGZ",
                    "_source": {"role_name": "org_VOGZ"},
                    "sort": ["org_VOGZ"],
                },
                {
                    "_id": "team_NFCY",
                    "_source": {"role_name": "team_NFCY"},
                    "sort": ["team_NFCY"],
                },
                {
                    "_id": "team_admin",
                    "_source": {"role_name": "team_admin"},
                    "sort": ["team_admin"],
                },
            ]
        }
    }

    summary = cleanup_de_role_permissions(es, apply=True, limit=None)

    deleted_ids = sorted(c.kwargs["id"] for c in es.delete.call_args_list)
    assert deleted_ids == ["org_VOGZ", "team_NFCY"]
    assert summary["legacy_found"] == 2
    assert summary["deleted"] == 2
    assert summary["errors"] == 0

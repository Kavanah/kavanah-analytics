"""Entra-enforced MFA reports as mfa_method="entra_sso", derived
from metadata.microsoft_sub, never as a bare bypass.

Covers the three acceptance criteria:
  * An Entra-linked user is distinguishable from a break-glass bypass
    (method="entra_sso", not the bypass path).
  * A genuine bypass_mfa with no SSO linkage keeps its existing treatment.
  * No SSO user is pushed into app-side MFA enrollment (setup_complete=True,
    required=False — the rule-5 unknown-method fall-through is never reached).

All Elasticsearch calls are mocked, matching test_mfa_service.py conventions.
"""

import sys
import os
import pytest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PYTHON_DIR = os.path.join(_REPO_ROOT, "bin", "python")
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

from asset_management.web.services.mfa_service import MFAService

_ENFORCE_MFA_PATH = "asset_management.web.config.Config.ENFORCE_MFA"


def _security_user(metadata):
    """A mock ES client whose security.get_user returns one user record."""
    es = mock.MagicMock()
    es.security.get_user.return_value = {"alice": {"metadata": metadata, "roles": []}}
    return es


class TestIsEntraEnforced:
    def test_true_when_microsoft_sub_present(self):
        es = _security_user({"microsoft_sub": "AAbbCC123"})
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            assert MFAService.is_entra_enforced("alice") is True

    def test_false_when_no_sub(self):
        es = _security_user({"account_type": "human"})
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            assert MFAService.is_entra_enforced("alice") is False

    def test_fails_closed_on_es_error(self):
        es = mock.MagicMock()
        es.security.get_user.side_effect = Exception("boom")
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            assert MFAService.is_entra_enforced("alice") is False

    def test_fails_closed_when_es_unavailable(self):
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=None):
            assert MFAService.is_entra_enforced("alice") is False


class TestIsMfaRequiredEntraRule:
    def test_entra_linked_user_not_required_and_not_enrollable(self):
        """Acceptance: SSO user is never pushed into app-side enrollment."""
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "is_entra_enforced", return_value=True), \
             mock.patch.object(MFAService, "get_mfa_record") as get_record:
            result = MFAService.is_mfa_required("alice")
        assert result == {"required": False, "method": "entra_sso", "setup_complete": True}
        # Short-circuits before the MFA record is even consulted.
        get_record.assert_not_called()

    def test_entra_supersedes_legacy_bypass_flag(self):
        """A linked user with a leftover bypass reads as Entra."""
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "is_entra_enforced", return_value=True), \
             mock.patch.object(MFAService, "get_mfa_record", return_value={"bypass_mfa": True}):
            result = MFAService.is_mfa_required("alice")
        assert result["method"] == "entra_sso"

    def test_genuine_bypass_without_linkage_unchanged(self):
        """Acceptance: a real break-glass bypass keeps its existing shape."""
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "is_entra_enforced", return_value=False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value={"bypass_mfa": True}):
            result = MFAService.is_mfa_required("alice")
        assert result == {"required": False, "method": None, "setup_complete": False}

    def test_unlinked_user_still_forced_to_enroll(self):
        with mock.patch(_ENFORCE_MFA_PATH, True), \
             mock.patch.object(MFAService, "is_entra_enforced", return_value=False), \
             mock.patch.object(MFAService, "get_mfa_record", return_value=None):
            result = MFAService.is_mfa_required("alice")
        assert result["required"] is True
        assert result["setup_complete"] is False


class TestListAllUsersDerivation:
    def _run(self, metadata, mfa_hits):
        es = mock.MagicMock()
        es.security.get_user.return_value = {"alice": {"metadata": metadata, "roles": [], "enabled": True}}
        es.search.return_value = {"hits": {"hits": mfa_hits}}
        with mock.patch("lib.es_helpers.get_elasticsearch_client", return_value=es):
            users = MFAService.list_all_users_with_mfa()
        assert len(users) == 1
        return users[0]

    def test_linked_user_reports_entra_sso(self):
        row = self._run({"microsoft_sub": "AAbbCC123"}, [])
        assert row["mfa_method"] == "entra_sso"
        assert row["setup_complete"] is True

    def test_linked_user_with_bypass_record_reports_entra_sso(self):
        hits = [{"_id": "alice", "_source": {"username": "alice", "bypass_mfa": True}}]
        row = self._run({"microsoft_sub": "AAbbCC123"}, hits)
        assert row["mfa_method"] == "entra_sso"

    def test_unlinked_totp_user_unchanged(self):
        hits = [{"_id": "alice", "_source": {
            "username": "alice", "mfa_enabled": True,
            "mfa_method": "totp", "totp_confirmed": True,
        }}]
        row = self._run({}, hits)
        assert row["mfa_method"] == "totp"
        assert row["setup_complete"] is True

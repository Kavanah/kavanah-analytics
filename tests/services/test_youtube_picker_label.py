"""Unit tests for the YouTube account picker label disambiguator.

One Google login can front many Brand Account identities. The data model
correctly stores one doc per identity (one doc = one token = one identity,
), but the picker labelled them with title + email only — so N
identities under one login rendered as N visually IDENTICAL rows, in both the
setup picker and the Edit-channels flow, with no way to tell which channel each
doc serves.

``youtube_account_owned_channel_titles`` is stamped by the OAuth callback and was
read by nothing in the web layer; these tests pin it as the disambiguator.
"""

from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from asset_management.web.services.connector_wizard_service import (  # noqa: E402
    _youtube_picker_label,
)

INFO = "Info Sample Jesus Initiative"


def test_should_append_the_owned_channel_so_same_titled_docs_differ():
    """The prod case: two docs, same title, same login, different Brand Account
    identities owning different channels. The labels must not be equal."""
    left = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": ["ঈসা মসীহের জীবনী"]})
    right = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": ["प्रभु येशु के साथ"]})
    assert left != right
    assert "ঈসা মসীহের জীবনী" in left
    assert "प्रभु येशु के साथ" in right


def test_should_keep_the_bare_title_when_no_channels_are_stamped():
    """A never-authorized doc, or a legacy grant whose owned set was never
    probed, must not gain a misleading marker."""
    assert _youtube_picker_label(INFO, {}) == INFO
    assert _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": []}) == INFO


def test_should_ignore_blank_entries_in_the_stamped_list():
    """A stamped list of only empty strings carries no information, so it must
    fall back to the bare title rather than rendering a dangling separator."""
    assert _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": ["", "  "]}) == INFO


def test_should_truncate_a_doc_owning_many_channels():
    """A doc owning 12 channels must not produce an unreadable option."""
    names = [f"Channel {i}" for i in range(12)]
    label = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": names})
    assert "Channel 0" in label
    assert "+9 more" in label
    # The 4th onward are summarized, not listed.
    assert "Channel 5" not in label


def test_should_list_all_channels_when_at_or_under_the_cap():
    label = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": ["A", "B", "C"]}, max_channels=3)
    assert "A" in label and "B" in label and "C" in label
    assert "more" not in label


def test_should_preserve_the_original_title_as_the_prefix():
    """The title still leads — the suffix disambiguates, it does not replace."""
    label = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": ["FLKDF"]})
    assert label.startswith(INFO)


def test_should_coerce_non_string_stamped_values():
    """The stamped list comes from the YouTube API via ES; be defensive rather
    than raising inside a picker that would blank the whole dropdown."""
    label = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": [123, None, "Real"]})
    assert "Real" in label
    assert "123" in label


# ── the call site ────────────────────────────────────────────────────────────
# Testing the helper alone is not enough: the disambiguator can be correct and
# still not be WIRED into the picker. These drive list_assets_for_picker so a
# disconnected call site fails.


def test_should_disambiguate_through_the_picker_itself(monkeypatch):
    """The prod shape end-to-end: two same-titled YouTube docs must come back
    from the picker with DIFFERENT labels."""
    from asset_management.web.services import connector_wizard_service as svc

    docs = [
        {
            "_id": "jVj4LpwBIm2AdJKusRc6",
            "_source": {
                "youtube_account_title": INFO,
                "youtube_account_email": "info@example.com",
                "workspace_esid": "ws1",
                "youtube_account_owned_channel_titles": ["ঈসা মসীহের জীবনী"],
            },
        },
        {
            "_id": "Bq8RXJ8BTJnYq0K40LDs",
            "_source": {
                "youtube_account_title": INFO,
                "youtube_account_email": "info@example.com",
                "workspace_esid": "ws1",
                "youtube_account_owned_channel_titles": ["प्रभु येशु के साथ"],
            },
        },
    ]
    monkeypatch.setattr(svc.AssetService, "get_all", staticmethod(lambda index: docs))

    options = svc.list_assets_for_picker("youtube_accounts", allowed_workspaces=None)
    titles = [o["title"] for o in options]

    assert len(titles) == 2
    assert titles[0] != titles[1], "same-titled YouTube docs still render identically"
    assert any("ঈসা মসীহের জীবনী" in t for t in titles)
    assert any("प्रभु येशु के साथ" in t for t in titles)


def test_should_not_change_labels_for_other_asset_types(monkeypatch):
    """The suffix is YouTube-only — every other picker keeps its bare title, so
    this cannot regress the seven other pick-existing surfaces."""
    from asset_management.web.services import connector_wizard_service as svc

    docs = [
        {
            "_id": "a1",
            "_source": {
                "echo_tenant_title": "Acme Tenant",
                "workspace_esid": "ws1",
                # Even if this stray field were present, it must be ignored here.
                "youtube_account_owned_channel_titles": ["Should Not Appear"],
            },
        }
    ]
    monkeypatch.setattr(svc.AssetService, "get_all", staticmethod(lambda index: docs))

    options = svc.list_assets_for_picker("echo_tenants", allowed_workspaces=None)
    assert options and options[0]["title"] == "Acme Tenant"


def test_should_expose_owned_channels_as_a_readonly_preserved_asset_explorer_column():
    """Asset Explorer's list view is a THIRD surface, separate from the picker
    and the connector dialog: it renders whatever ``fields`` declares. Without
    this column it shows only Title + Email, which are identical across
    same-login docs, so its rows stay indistinguishable.

    Two flags are load-bearing and must not be dropped:

    * ``readonly`` — the value is API-derived truth about who authorized.
      Hand-editing it would make the dedup and ownership audits lie.
    * ``preserve_on_empty`` — a readonly field is not submitted by the form, and
      ``process_form_data`` marks unsubmitted optional fields for DELETION.
      Without this, any unrelated edit to a YouTube account would silently wipe
      the stamp (the regression on the team ds_* checkboxes).
    """
    from asset_management.web.asset_configs import ASSET_CONFIGS

    fields = {f["name"]: f for f in ASSET_CONFIGS["youtube_accounts"]["fields"] if "name" in f}
    col = fields.get("youtube_account_owned_channel_titles")

    assert col is not None, "Asset Explorer would show indistinguishable YouTube rows"
    assert col.get("readonly") is True
    assert col.get("preserve_on_empty") is True
    assert not col.get("required")


def test_should_disambiguate_the_edit_channel_form_account_select(monkeypatch):
    """A FOURTH surface: the Edit YouTube Channel form's "YouTube Account"
    select is a store_esid dropdown sourced straight from
    de_asset_youtube_accounts on youtube_account_title — so same-login docs
    listed identical options, bypassing both the picker and the summary fixes.

    The suffix must land on ``display`` ONLY. ``title`` is what
    process_form_data writes back to youtube_account_title, so suffixing it
    would corrupt the stored field.
    """
    from asset_management.web.asset_configs import ASSET_CONFIGS
    from asset_management.web.services import form_service as fs

    docs = [
        {
            "_id": "acctA",
            "_source": {
                "youtube_account_title": INFO,
                "workspace_esid": "ws1",
                "youtube_account_owned_channel_titles": ["FLKDF"],
            },
        },
        {
            "_id": "acctB",
            "_source": {
                "youtube_account_title": INFO,
                "workspace_esid": "ws1",
                "youtube_account_owned_channel_titles": ["Prabhu Yeshu Ke Saath"],
            },
        },
    ]
    monkeypatch.setattr(fs.AssetService, "get_all", staticmethod(lambda index, _=None: docs))

    opts = fs.FormService.get_select_options(ASSET_CONFIGS["youtube_channels"])["youtube_account_title"]
    by_id = {o["id"]: o for o in opts}

    # Labels differ...
    assert by_id["acctA"]["display"] != by_id["acctB"]["display"]
    assert "FLKDF" in by_id["acctA"]["display"]
    # ...but the stored title is untouched on BOTH.
    assert by_id["acctA"]["title"] == INFO
    assert by_id["acctB"]["title"] == INFO


def test_should_not_set_a_display_label_when_no_channels_are_stamped(monkeypatch):
    """No stamped titles means nothing to disambiguate with — leave ``display``
    unset so the option renders its plain title rather than a bare separator."""
    from asset_management.web.asset_configs import ASSET_CONFIGS
    from asset_management.web.services import form_service as fs

    docs = [{"_id": "acctA", "_source": {"youtube_account_title": INFO, "workspace_esid": "ws1"}}]
    monkeypatch.setattr(fs.AssetService, "get_all", staticmethod(lambda index, _=None: docs))

    opts = fs.FormService.get_select_options(ASSET_CONFIGS["youtube_channels"])["youtube_account_title"]
    assert opts[0].get("display") in (None, INFO)
    assert opts[0]["title"] == INFO


def test_should_frame_the_suffix_as_the_authorizing_identity_not_as_served_channels():
    """Owned is not the same as LINKED. On prod jVj4LpwBIm2AdJKusRc6 owns only
    FLKDF while 12 channels point at it, so a bare channel-name
    suffix would read as "this group serves FLKDF" directly above a list of
    Bada Jeevan and kamaargmoksh — a mislabel. The wording must describe the
    authorizing identity so the divergence reads as information, not as a bug.
    """
    label = _youtube_picker_label(INFO, {"youtube_account_owned_channel_titles": ["FLKDF"]})
    assert "authorized as" in label
    assert label == f"{INFO} — authorized as: FLKDF"

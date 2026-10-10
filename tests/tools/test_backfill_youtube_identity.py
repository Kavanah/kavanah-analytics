"""Unit tests for the YouTube identity-stamp backfill tool.

Covers the pure selection + stamping logic (``needs_backfill``,
``extract_sub_from_id_token``, ``stamp_identity``). The ES / Google HTTP I/O
(_probe_identity, backfill loop) is integration-only and not exercised here.
"""

from __future__ import annotations

import base64
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PY_ROOT = os.path.join(_REPO_ROOT, "bin", "python")
if _PY_ROOT not in sys.path:
    sys.path.insert(0, _PY_ROOT)

from tools.backfill_youtube_identity import (  # noqa: E402
    extract_sub_from_id_token,
    needs_backfill,
    stamp_identity,
)


def _id_token(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.signature"


# ── needs_backfill ────────────────────────────────────────────────────────────


def test_should_select_tokened_doc_without_identity_stamp():
    """The target population: a legacy doc holding a token but never stamped."""
    assert needs_backfill({"youtube_account_token": "enc", "youtube_account_title": "Legacy"})


def test_should_skip_doc_when_identity_already_stamped():
    assert not needs_backfill({"youtube_account_token": "enc", "youtube_account_identity": "103401661513291815428"})


def test_should_skip_tokenless_doc():
    """No token -> nothing to probe; the wizard placeholder path owns these."""
    assert not needs_backfill({"youtube_account_title": "Placeholder"})


def test_should_skip_redirect_placeholder_even_when_tokened():
    """redirect placeholders deliberately stay unstamped — the
    canonical doc carries the identity."""
    assert not needs_backfill({"youtube_account_token": "enc", "youtube_account_redirect_esid": "canonical123"})


# ── extract_sub_from_id_token ─────────────────────────────────────────────────


def test_should_decode_sub_when_id_token_well_formed():
    token = _id_token({"sub": "103401661513291815428", "email": "x@example.com"})
    assert extract_sub_from_id_token(token) == "103401661513291815428"


def test_should_return_empty_when_id_token_absent_or_malformed():
    assert extract_sub_from_id_token("") == ""
    assert extract_sub_from_id_token("not-a-jwt") == ""
    assert extract_sub_from_id_token("a.!!!not-base64!!!.c") == ""


# ── stamp_identity ────────────────────────────────────────────────────────────


def test_should_stamp_identity_and_owned_set_when_sub_recovered():
    """Mirrors fb_oauth_server._stamp_youtube_identity field-for-field."""
    source = {"youtube_account_title": "Legacy", "youtube_account_token": "enc"}
    owned = [{"id": "UCabc", "title": "Chan A"}, {"id": "UCdef", "title": "Chan B"}]

    stamped = stamp_identity(source, "103401", owned, "2026-07-13T00:00:00Z")

    assert stamped["youtube_account_identity"] == "103401"
    assert stamped["youtube_account_owned_channel_ids"] == ["UCabc", "UCdef"]
    assert stamped["youtube_account_owned_channel_titles"] == ["Chan A", "Chan B"]
    assert stamped["youtube_account_identity_checked"] == "2026-07-13T00:00:00Z"


def test_should_omit_identity_field_when_sub_unrecoverable():
    """A legacy grant without openid yields no sub — owned set + checked
    timestamp still land, but no fabricated identity."""
    stamped = stamp_identity({"youtube_account_token": "enc"}, "", [{"id": "UCabc", "title": "A"}], "2026-07-13T00:00:00Z")

    assert "youtube_account_identity" not in stamped
    assert stamped["youtube_account_owned_channel_ids"] == ["UCabc"]


def test_should_preserve_token_and_existing_fields_when_stamping():
    """The stamp is additive: token, email, workspace scoping are untouched."""
    source = {
        "youtube_account_token": "enc",
        "youtube_account_email": "x@example.com",
        "workspace_esid": "kavanah-media",
    }

    stamped = stamp_identity(source, "103401", [], "2026-07-13T00:00:00Z")

    assert stamped["youtube_account_token"] == "enc"
    assert stamped["youtube_account_email"] == "x@example.com"
    assert stamped["workspace_esid"] == "kavanah-media"
    assert source == {  # input not mutated
        "youtube_account_token": "enc",
        "youtube_account_email": "x@example.com",
        "workspace_esid": "kavanah-media",
    }

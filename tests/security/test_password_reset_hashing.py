"""Password hashing in the standalone password-reset Flask app.

Covers the SHA256 fallback for storing the admin password is
removed. Only bcrypt-hashed values verify; legacy SHA256 hex digests
must be rejected so an attacker who reads `settings.json` can't exploit
the weak hash.
"""

import hashlib
import importlib
import os
import sys

import pytest


@pytest.fixture(scope="module")
def pw_reset_app():
    pw_reset_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "bin", "python",
    )
    if pw_reset_dir not in sys.path:
        sys.path.insert(0, pw_reset_dir)
    os.environ.setdefault("APP_SECRET_KEY", "test-pw-reset-secret-key-do-not-use-in-prod")
    if "password_reset.app" in sys.modules:
        importlib.reload(sys.modules["password_reset.app"])
    return importlib.import_module("password_reset.app")


def test_hash_password_returns_bcrypt_format(pw_reset_app):
    """hash_password must return a bcrypt hash (starts with $2)."""
    hashed = pw_reset_app.hash_password("hunter2")
    assert hashed.startswith("$2")


def test_verify_password_accepts_correct_bcrypt(pw_reset_app):
    """A correct password verifies against a bcrypt hash from hash_password."""
    hashed = pw_reset_app.hash_password("hunter2")
    assert pw_reset_app.verify_password("hunter2", hashed) is True


def test_verify_password_rejects_wrong_bcrypt(pw_reset_app):
    """A wrong password fails to verify against a bcrypt hash."""
    hashed = pw_reset_app.hash_password("hunter2")
    assert pw_reset_app.verify_password("not-the-password", hashed) is False


def test_verify_password_rejects_legacy_sha256_hash(pw_reset_app):
    """Legacy SHA256 hex digests (the earlier fallback format) must be
    rejected outright, even when supplied with the correct password."""
    legacy = hashlib.sha256(b"hunter2").hexdigest()
    assert pw_reset_app.verify_password("hunter2", legacy) is False


def test_verify_password_rejects_empty_hash(pw_reset_app):
    """An empty stored hash must not authenticate any password."""
    assert pw_reset_app.verify_password("hunter2", "") is False


def test_verify_password_rejects_garbage_hash(pw_reset_app):
    """A non-bcrypt, non-empty stored hash must not crash and must not
    authenticate (defends against config corruption)."""
    assert pw_reset_app.verify_password("hunter2", "not-a-real-hash") is False

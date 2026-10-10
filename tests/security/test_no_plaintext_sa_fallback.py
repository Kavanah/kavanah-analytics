"""LIB-001 regression: no silent plaintext service-account fallback.

The security scan (LIB-001) flagged ``load_encrypted_service_account`` for
silently falling back to loading an UNENCRYPTED service-account JSON (Google SA
private keys) from disk when the ``.enc`` file was absent, with only a WARNING.

Investigation (2026-07-14) established the helper was dead code: YouTube, Google
Ads, and GA4 are all OAuth-only now, no
``*-sa.json`` files exist on any environment, and nothing in the codebase called
it. It was removed rather than guarded. This test guards against reintroduction
of any plaintext-credential fallback path in ``lib.encryption``.
"""

import inspect

import lib.encryption as encryption


def test_should_not_expose_service_account_fallback_helper() -> None:
    # Arrange / Act / Assert: the dead plaintext-fallback helper must stay gone.
    assert not hasattr(encryption, "load_encrypted_service_account"), (
        "LIB-001: load_encrypted_service_account was removed as dead code because "
        "it silently loaded UNENCRYPTED service-account keys. Do not reintroduce a "
        "plaintext-credential fallback."
    )


def test_encryption_module_has_no_plaintext_credential_fallback() -> None:
    # Arrange: scan the module source for the retired fallback's tell-tale WARNING.
    source = inspect.getsource(encryption)

    # Assert: no code path loads an "UNENCRYPTED" credential file.
    assert "UNENCRYPTED" not in source, (
        "LIB-001: a plaintext-credential fallback path appears to have returned to "
        "lib/encryption.py. Encrypted-at-rest credentials must not silently fall "
        "back to plaintext."
    )

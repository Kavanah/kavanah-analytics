"""Validation for Flask SECRET_KEY and other secrets sourced from
master-config.yaml or the environment.

The 2026-04-23 prod incident was caused by Flask running
with ``SECRET_KEY="null"`` — yq returned the literal string ``"null"`` for a
missing yaml key, and nothing along the path refused it. This module
centralises the rules so both the app boot path and the deploy scripts
(via ``deployment/lib/validate-secret.sh``) agree on what counts as valid.
"""

from __future__ import annotations

import os
import sys


class InvalidSecretError(Exception):
    """Raised when a secret value is missing, weak, or a known sentinel."""


_FORBIDDEN: frozenset[str] = frozenset(
    {
        "",
        "null",
        "None",
        "dev-secret-key-change-in-production",
    }
)
_MIN_LENGTH = 32


def validate_secret_key(value: object, *, name: str = "SECRET_KEY") -> None:
    """Raise :class:`InvalidSecretError` if *value* is not a valid secret.

    Rejects ``None``, non-strings, the forbidden sentinel set
    (``""``, ``"null"``, ``"None"``, the historical default), and any string
    shorter than 32 characters.

    The *name* is interpolated into the error message so callers can identify
    which secret failed (e.g. ``SECRET_KEY``, ``ES01_PASSWORD``).
    """
    if value is None:
        raise InvalidSecretError(
            f"{name} is missing. Set asset_web.flask_secret_key in " f"master-config.yaml or the {name} environment variable."
        )
    if not isinstance(value, str):
        raise InvalidSecretError(f"{name} must be a string, got {type(value).__name__}.")
    if value in _FORBIDDEN:
        raise InvalidSecretError(
            f"{name} is set to a forbidden sentinel value "
            f"({value!r}). This usually means the yaml key is missing — "
            f"set asset_web.flask_secret_key in master-config.yaml to a "
            f"strong random value (openssl rand -hex 32)."
        )
    if len(value) < _MIN_LENGTH:
        raise InvalidSecretError(
            f"{name} is too short ({len(value)} chars; minimum is "
            f"{_MIN_LENGTH}). Generate a strong value with "
            f"`openssl rand -hex 32`."
        )


def is_test_mode() -> bool:
    """Return True if the current process is running under pytest.

    Used by ``app.py`` to skip the hard refusal at module import — tests
    exercise the validator directly via :func:`validate_secret_key` rather
    than via the app's import-time check.
    """
    return "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules


# environment markers that positively identify a production deploy.
# ``DEPLOY_ENV`` is the explicit marker step3-app-stack.sh already keys its own
# TEST_AUTH_SECRET decision on; ``APP_PUBLIC_HOSTNAME`` is the public hostname
# the deploy writes into the compose .env (from reporting.hostname). Either one
# naming prod is enough to refuse.
_PROD_DEPLOY_ENVS: frozenset[str] = frozenset({"prod", "production"})


def validate_test_auth_secret(
    test_auth_secret: object,
    *,
    deploy_env: str = "",
    public_hostname: str = "",
) -> None:
    """Refuse a live ``TEST_AUTH_SECRET`` on anything that identifies as prod.

    The ``X-Test-Persona`` header path (``verify_auth``) grants a full session
    for any persona and skips MFA whenever ``TEST_AUTH_SECRET`` is non-empty.
    It is *safe by absence* — prod's deploy never sets the secret — but until
     nothing checked that promise at runtime: one misconfigured deploy
    or one leaked staging env-file would have turned it into a master key.

    This is a **refuse-only** gate, deliberately compatible with the
    rule against environment-name branches: a prod marker can only *remove*
    the capability, never enable it. If neither marker is present the secret
    behaves exactly as before, so a missing marker degrades to today's posture
    rather than opening anything new.

    Raises :class:`InvalidSecretError` when the secret is non-empty AND either
    ``deploy_env`` is ``prod`` / ``production`` or ``public_hostname`` names a
    production host per :func:`lib.prod_hosts.is_prod_host` (built-in
    ``kavanahanalytics.com`` plus ``PROD_HOSTNAMES``). An empty secret always
    passes.
    """
    if not test_auth_secret or not isinstance(test_auth_secret, str):
        return

    from lib.prod_hosts import is_prod_host  # deferred: keeps this module import-light

    env_marker = (deploy_env or "").strip().lower()
    if env_marker in _PROD_DEPLOY_ENVS:
        raise InvalidSecretError(
            f"TEST_AUTH_SECRET is set while DEPLOY_ENV={env_marker!r}. The X-Test-Persona "
            "header-auth path grants an MFA-bypass session and must never be live on "
            "production. Remove TEST_AUTH_SECRET from this host's compose .env "
            "(deployment/step3-app-stack.sh leaves it empty for prod) and restart."
        )
    if public_hostname and is_prod_host(public_hostname):
        raise InvalidSecretError(
            f"TEST_AUTH_SECRET is set while APP_PUBLIC_HOSTNAME={public_hostname!r} is a "
            "production host. The X-Test-Persona header-auth path grants an MFA-bypass "
            "session and must never be live on production. Remove TEST_AUTH_SECRET from "
            "this host's compose .env and restart."
        )


def validate_test_auth_secret_from_environ(environ: "os._Environ[str] | dict[str, str] | None" = None) -> None:
    """:func:`validate_test_auth_secret` fed from the process environment.

    ``app.py`` calls this at import, inside the same ``not is_test_mode()``
    block as the SECRET_KEY gate, so a prod container with the secret set
    fails to start instead of serving an MFA bypass.
    """
    env = os.environ if environ is None else environ
    validate_test_auth_secret(
        env.get("TEST_AUTH_SECRET", ""),
        deploy_env=env.get("DEPLOY_ENV", ""),
        public_hostname=env.get("APP_PUBLIC_HOSTNAME", ""),
    )

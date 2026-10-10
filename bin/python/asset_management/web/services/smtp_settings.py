"""Shared, env-aware SMTP settings loader.

Every asset-manager mailer historically opened the password-reset app's
``settings.json`` directly and read the SMTP credentials out of it.
moved those credentials to environment variables (``PW_RESET_SMTP_*``,
injected from SSM via deployment/.env.aws by start-imports-container.sh),
and the password-reset app's ``save_settings()`` now STRIPS the env-managed
fields from the file — so a bare ``json.load`` of settings.json no longer
yields credentials. Every mailer must load through this module instead:
file first, env overlay wins. This mirrors ``password_reset/app.py``'s
``get_settings()`` — keep the two override maps in sync.
"""

from __future__ import annotations

import json
import logging
import os

PASSWORD_RESET_SETTINGS = "/app/python/password_reset/config/settings.json"

_SMTP_ENV_OVERRIDES = {
    "smtp_host": "PW_RESET_SMTP_HOST",
    "smtp_port": "PW_RESET_SMTP_PORT",
    "smtp_username": "PW_RESET_SMTP_USERNAME",
    "smtp_password": "PW_RESET_SMTP_PASSWORD",
}


def load_smtp_settings(service_name: str = "smtp_settings") -> dict:
    """Return the shared SMTP settings with env-managed credentials applied.

    ``service_name`` only prefixes log lines so each mailer's failures stay
    attributable. A missing/unreadable file returns ``{}`` overlaid with the
    env values — mail keeps working without the file, since the credentials
    live in the environment.
    """
    try:
        with open(PASSWORD_RESET_SETTINGS, "r") as f:
            settings = json.load(f)
    except FileNotFoundError:
        logging.warning("%s: SMTP config not found at %s", service_name, PASSWORD_RESET_SETTINGS)
        settings = {}
    except Exception as e:  # noqa: BLE001 — top-level boundary
        logging.error("%s: could not load SMTP settings: %s", service_name, e)
        settings = {}

    for field, var in _SMTP_ENV_OVERRIDES.items():
        val = os.environ.get(var)
        if not val:
            continue
        if field == "smtp_port":
            try:
                settings[field] = int(val)
            except ValueError:
                logging.warning("%s: ignoring non-numeric %s", service_name, var)
                continue
        else:
            settings[field] = val

    return settings

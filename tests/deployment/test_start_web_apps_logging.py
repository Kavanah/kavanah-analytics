"""every gunicorn in start_web_apps.sh must pass --capture-output.

Background
----------
Without ``--capture-output`` gunicorn writes only its own records to
``--error-logfile``; the Flask apps' stdout/stderr (``logging.*`` via the
default stderr handler, ``print``, tracebacks raised outside the logging
module) are discarded. On staging that meant ``asset_web_error.log`` sat
frozen at container start while the app logged the correct diagnosis of the
 SSO failure on the first attempt — the only way to see it was
``docker attach`` while reproducing live, which cost about an hour.

This is the same shape of guard as the logrotate symmetry test: the
flag is trivially easy to forget when a fourth web app is added, and its
absence is invisible until the next incident needs the logs.

Mutation-verified locally:
  - Remove ``--capture-output`` from any one gunicorn invocation -> the
    per-invocation assert names the app that lost it.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "python" / "start_web_apps.sh"


def _gunicorn_invocations(text: str) -> list[str]:
    """Return each gunicorn command as one logical line (continuations joined)."""
    joined = text.replace("\\\n", " ")
    return [line for line in joined.splitlines() if re.search(r"^\s*gunicorn\b", line)]


def test_every_gunicorn_captures_app_output():
    text = SCRIPT.read_text()
    invocations = _gunicorn_invocations(text)

    # The script currently starts three web apps; a disappearing invocation
    # would silently shrink coverage, so pin the floor.
    assert len(invocations) >= 3, (
        f"expected >=3 gunicorn invocations in {SCRIPT.name}, found {len(invocations)}"
    )

    missing = [line.split("&")[0].strip()[:80] for line in invocations if "--capture-output" not in line]
    assert not missing, (
        "gunicorn invocation(s) missing --capture-output (app stdout/stderr "
        f"will be discarded — ): {missing}"
    )


def test_every_gunicorn_has_an_error_logfile():
    """--capture-output is meaningless without an --error-logfile to land in."""
    text = SCRIPT.read_text()
    for line in _gunicorn_invocations(text):
        assert "--error-logfile" in line, f"gunicorn invocation without --error-logfile: {line[:80]}"

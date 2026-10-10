"""logging hygiene: volume, durability, and healthcheck noise.

Measured on prod 2026-08-06 during the the web app
wrote ~23 MB/day of error log, of which 82.5% was the elasticsearch client
logging every call at INFO and 11.3% was a "Missing authentication from
127.0.0.1" WARNING emitted by the 15-second Docker healthcheck (11,941 of them
in two days). The AUDIT: lines actually used to investigate the incident were
0.1%. Removing the two noise sources takes the same period from 47.8 MB to
3.0 MB.

These are cheap structural pins — they assert the fixes are still in place, so
a future edit that reintroduces 23 MB/day of chatter fails here rather than in
production six months later.

Mutation-checked: reverting each fix fails its corresponding test.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_elasticsearch_client_logger_is_not_info():
    """The ES client must not log every call at INFO (82.5% of log volume)."""
    src = (REPO / "bin/python/asset_management/web/app.py").read_text()
    # elasticsearch-py 9.x emits the per-request line from
    # elastic_transport.transport — damping "elasticsearch" alone is a no-op,
    # which is exactly the bug this pins (staging, 2026-08-07).
    assert re.search(
        r'getLogger\(\s*["\']elastic_transport["\']\s*\)\.setLevel\(\s*logging\.(WARNING|ERROR)\s*\)',
        src,
    ), "app.py must damp the 'elastic_transport' logger — that is what logs every ES call"


def test_gunicorn_logs_are_not_written_to_container_tmp():
    """Logs must land on a host-mounted path so they survive a container
    recreate. /tmp is container-local and is wiped every rebuild — that is why
    the evidence only went back to the last container start."""
    src = (REPO / "bin/python/start_web_apps.sh").read_text()
    stray = re.findall(r"--(?:access|error)-logfile\s+(/tmp/\S+)", src)
    assert not stray, f"gunicorn log paths still under container /tmp: {stray}"
    assert re.search(r"--error-logfile\s+/app/data/logs/", src), \
        "expected gunicorn error logs under the host-mounted /app/data/logs"


def test_log_directory_is_created_before_gunicorn_starts():
    """gunicorn exits if it cannot open --error-logfile, so a missing log
    directory would fail container start outright."""
    src = (REPO / "bin/python/start_web_apps.sh").read_text()
    mkdir_at = src.find("mkdir -p /app/data/logs")
    first_gunicorn = src.find("\ngunicorn ")
    assert mkdir_at != -1, "start_web_apps.sh must create /app/data/logs"
    assert first_gunicorn == -1 or mkdir_at < first_gunicorn, \
        "mkdir must run BEFORE the first gunicorn launch"


def test_healthcheck_does_not_probe_an_authenticated_route():
    """Probing '/' (auth-required) every 15s logged a WARNING each time."""
    src = (REPO / "deployment/docker-compose.yml").read_text()
    assert 'urlopen(\\"http://localhost:8084/\\"' not in src, \
        "healthcheck must not probe '/', which requires auth and logs a warning per probe"
    assert "localhost:8084/login" in src, \
        "expected the app healthcheck to probe an unauthenticated route"

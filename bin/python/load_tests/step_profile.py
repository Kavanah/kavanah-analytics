"""Step-profile math for the stepped baseline→ramp load test.

The stepped profile runs one probe user alone for the first step (the
single-user baseline), then raises the total user count step by step so the
probe's response times can be compared against its own baseline as load grows.

Deliberately free of locust imports: the locustfile's ``StagesShape`` and the
``/api/testing/run-locust`` endpoint both delegate here, and keeping the math
pure means it is unit-testable without locust installed.
"""

from __future__ import annotations

import re
from lib.user_errors import InvalidRequestError

# Same shape as the duration guard: values flow into a subprocess env
# var, so reject anything that isn't a plain comma-separated integer list
# before it leaves the endpoint.
_STEP_USERS_RE = re.compile(r"^\d{1,3}(,\d{1,3})*$")

MAX_USERS_PER_STEP = 100
MAX_STEPS = 20
MIN_STEP_SECONDS = 10
MAX_STEP_SECONDS = 3600
DEFAULT_STEP_USERS = "1,5,10,25,50"
DEFAULT_STEP_SECONDS = 120
# Spawn rate used for every stage transition. Modest on purpose: ramping is
# not the thing being measured, and a burst of simultaneous logins would
# pollute the first samples of each step.
SPAWN_RATE = 4


def parse_step_users(raw: str) -> list[int]:
    """Parse and validate a comma-separated step-user list like ``1,5,10,25,50``.

    The first step MUST be 1 — it is the single-user baseline window every
    later step is normalized against; without it the degradation report has
    no denominator.
    """
    raw = (raw or "").strip()
    if not _STEP_USERS_RE.fullmatch(raw):
        raise InvalidRequestError("step_users must be comma-separated integers, e.g. '1,5,10,25,50'")
    steps = [int(part) for part in raw.split(",")]
    if len(steps) > MAX_STEPS:
        raise InvalidRequestError(f"step_users supports at most {MAX_STEPS} steps")
    if steps[0] != 1:
        raise InvalidRequestError("the first step must be 1 (the single-user baseline)")
    for value in steps:
        if not 1 <= value <= MAX_USERS_PER_STEP:
            raise InvalidRequestError(f"each step must be between 1 and {MAX_USERS_PER_STEP} users")
    return steps


def parse_step_seconds(raw: str | int) -> int:
    """Parse and validate the per-step dwell time in seconds."""
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise InvalidRequestError("step_seconds must be an integer number of seconds") from None
    if not MIN_STEP_SECONDS <= seconds <= MAX_STEP_SECONDS:
        raise InvalidRequestError(f"step_seconds must be between {MIN_STEP_SECONDS} and {MAX_STEP_SECONDS}")
    return seconds


def total_run_seconds(step_users: list[int], step_seconds: int) -> int:
    """Total wall-clock length of a stepped run (drives the UI countdown)."""
    return len(step_users) * step_seconds


def tick(run_time: float, step_users: list[int], step_seconds: int) -> tuple[int, int] | None:
    """LoadTestShape.tick() math: (target_users, spawn_rate) for ``run_time``.

    Returns None once every step has elapsed, which tells locust to stop
    the run.
    """
    index = int(run_time // step_seconds)
    if index >= len(step_users):
        return None
    return (step_users[index], SPAWN_RATE)

"""load_report_service.py — degradation-vs-baseline report for load tests.

Turns the two CSVs a run leaves behind (locust ``_stats_history.csv`` written
with --csv-full-history, and the resource sampler's ``locust_resources.csv``)
into one JSON report the Load Testing tab charts:

  - the probe user's p50/p95 over time (its request names are ``probe:``-
    prefixed), NORMALIZED to its own single-user baseline (the window where
    the total user count is 1), because the operator's question is "how much
    worse does one person's experience get as load rises" — not raw
    milliseconds;
  - host/container/ES CPU + memory on the same clock;
  - the user-count steps (derived from the data itself, so classic runs work
    too), and import-run markers (merged in by the Phase 3 endpoints).

All math lives here, server-side, so it is unit-testable against golden CSVs
(Definition of Done) — the template only draws.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from statistics import median

logger = logging.getLogger(__name__)

PROBE_PREFIX = "probe:"
# The probe logs in exactly once, inside the baseline window — its ~120ms+
# login rows would inflate the baseline denominator and make later steps
# read as faster-than-baseline (observed on the first staging run). The
# baseline must measure steady-state browsing, so auth requests are excluded
# from the probe series entirely.
_PROBE_EXCLUDED_PREFIX = PROBE_PREFIX + "/login"
BASELINE_USER_COUNT = 1


def _to_float(value: str | None) -> float | None:
    """CSV cell → float; locust writes 'N/A' before a name has samples."""
    if value is None:
        return None
    value = value.strip()
    if not value or value == "N/A":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def parse_stats_history(csv_text: str) -> list[dict]:
    """Locust stats_history rows (full history: one row per name per tick).

    Returns [{"t": epoch_s, "users": int, "name": str, "p50": float|None,
    "p95": float|None}] — the sliding-window percentile columns, which is
    what "current experience at time t" means.
    """
    rows: list[dict] = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        try:
            t = int(raw["Timestamp"])
            users = int(raw["User Count"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(
            {
                "t": t,
                "users": users,
                "name": (raw.get("Name") or "").strip(),
                "p50": _to_float(raw.get("50%")),
                "p95": _to_float(raw.get("95%")),
            }
        )
    return rows


def _series(rows: list[dict], keep) -> list[dict]:
    """Collapse per-name rows into one point per timestamp (median across the
    kept names' non-null percentiles)."""
    by_t: dict[int, dict] = {}
    for row in rows:
        if not keep(row["name"]):
            continue
        bucket = by_t.setdefault(row["t"], {"users": row["users"], "p50": [], "p95": []})
        bucket["users"] = max(bucket["users"], row["users"])
        if row["p50"] is not None:
            bucket["p50"].append(row["p50"])
        if row["p95"] is not None:
            bucket["p95"].append(row["p95"])
    out: list[dict] = []
    for t in sorted(by_t):
        bucket = by_t[t]
        out.append(
            {
                "t": t,
                "users": bucket["users"],
                "p50": round(median(bucket["p50"]), 1) if bucket["p50"] else None,
                "p95": round(median(bucket["p95"]), 1) if bucket["p95"] else None,
            }
        )
    return out


def _is_probe_name(name: str) -> bool:
    return name.startswith(PROBE_PREFIX) and not name.startswith(_PROBE_EXCLUDED_PREFIX)


def probe_series(rows: list[dict]) -> list[dict]:
    """The probe user's experience over time (median across its request
    names), excluding its one-time login rows (see _PROBE_EXCLUDED_PREFIX).

    Raw-ms reference line only. Degradation does NOT divide this series by a
    scalar baseline: the mix of request names differs between the baseline
    window (one-time config/list fetches, later cached) and steady state, so
    cross-name medians compare different baskets — the first staging run
    read 5-user load as 0.4x baseline because of exactly that. Degradation
    is computed per name instead (each name against its own baseline)."""
    return _series(rows, _is_probe_name)


def aggregate_series(rows: list[dict]) -> list[dict]:
    """Locust's own all-requests aggregate over time."""
    return _series(rows, lambda name: name == "Aggregated")


def compute_baseline(rows: list[dict]) -> dict | None:
    """PER-NAME baseline: each probe request name's median p50/p95 across the
    single-user window (users == 1).

    That window exists because the stepped profile forces step 1 to exactly
    one user (the probe alone). The baseline is a per-name dict, not one
    scalar: the probe's request mix changes after its caches warm, so a
    single cross-name number would compare different baskets (verified on
    staging — it read load as *faster* than baseline). The display summary
    (p50_ms/p95_ms) is the median of the per-name medians.

    Classic runs, or a stepped run that died before producing probe samples,
    return None — the report then carries raw series only.
    """
    per_name: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        if not _is_probe_name(row["name"]) or row["users"] != BASELINE_USER_COUNT:
            continue
        bucket = per_name.setdefault(row["name"], {"p50": [], "p95": []})
        if row["p50"]:
            bucket["p50"].append(row["p50"])
        if row["p95"]:
            bucket["p95"].append(row["p95"])
    names = {
        name: {"p50_ms": round(median(vals["p50"]), 1), "p95_ms": round(median(vals["p95"]), 1)}
        for name, vals in per_name.items()
        if vals["p50"] and vals["p95"]
    }
    if not names:
        return None
    return {
        "p50_ms": round(median(v["p50_ms"] for v in names.values()), 1),
        "p95_ms": round(median(v["p95_ms"] for v in names.values()), 1),
        "samples": sum(len(v["p50"]) for v in per_name.values()),
        "per_name": names,
    }


def degradation_series(rows: list[dict], baseline: dict | None) -> list[dict]:
    """Probe experience as a multiple of baseline (1.0 = baseline), computed
    PER NAME then medianed per tick: each request name's percentile is
    divided by that same name's baseline median, so a shifting request mix
    cannot masquerade as a speed change. Names with no baseline sample are
    skipped; ticks without samples are dropped rather than zero-filled — a
    gap is honest; a 0x is a lie."""
    per_name = (baseline or {}).get("per_name") or {}
    if not per_name:
        return []
    by_t: dict[int, dict] = {}
    for row in rows:
        base = per_name.get(row["name"])
        if base is None:
            continue
        bucket = by_t.setdefault(row["t"], {"users": row["users"], "x50": [], "x95": []})
        bucket["users"] = max(bucket["users"], row["users"])
        if row["p50"] and base["p50_ms"]:
            bucket["x50"].append(row["p50"] / base["p50_ms"])
        if row["p95"] and base["p95_ms"]:
            bucket["x95"].append(row["p95"] / base["p95_ms"])
    out: list[dict] = []
    for t in sorted(by_t):
        bucket = by_t[t]
        x50 = round(median(bucket["x50"]), 2) if bucket["x50"] else None
        x95 = round(median(bucket["x95"]), 2) if bucket["x95"] else None
        if x50 is None and x95 is None:
            continue
        out.append({"t": t, "users": bucket["users"], "x50": x50, "x95": x95})
    return out


def derive_steps(aggregate_points: list[dict]) -> list[dict]:
    """User-count bands [{users, from_t, to_t}] straight from the data (no
    run metadata needed, so classic runs and partial runs band correctly).
    The pre-spawn users==0 tick is folded into the first real band."""
    steps: list[dict] = []
    for point in aggregate_points:
        users = point["users"]
        if users == 0 and not steps:
            continue
        if steps and steps[-1]["users"] == users:
            steps[-1]["to_t"] = point["t"]
        else:
            steps.append({"users": users, "from_t": point["t"], "to_t": point["t"]})
    return steps


def parse_resources(csv_text: str) -> list[dict]:
    """Sampler CSV → [{"t": epoch_s, <numeric columns...>}] with None for
    blank (failure-soft) cells."""
    out: list[dict] = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        stamp = (raw.get("timestamp") or "").strip()
        try:
            t = int(datetime.fromisoformat(stamp).timestamp())
        except ValueError:
            continue
        point: dict = {"t": t}
        for key, value in raw.items():
            if key == "timestamp":
                continue
            point[key] = _to_float(value)
        out.append(point)
    return out


def build_report(stats_history_csv: str, resources_csv: str, meta: dict | None = None) -> dict:
    """Assemble the full report JSON from the run's CSVs.

    ``meta`` is the run-metadata blob run-locust wrote at launch (profile,
    step config, identity mode) — display-only; every computed value derives
    from the CSVs themselves.
    """
    rows = parse_stats_history(stats_history_csv)
    aggregate = aggregate_series(rows)
    baseline = compute_baseline(rows)
    return {
        "meta": meta or {},
        "baseline": baseline,
        "probe": probe_series(rows),
        "aggregate": aggregate,
        "degradation": degradation_series(rows, baseline),
        "steps": derive_steps(aggregate),
        "resources": parse_resources(resources_csv),
        # Import-run markers are merged in by the Phase 3 import-load status
        # endpoint; empty until then.
        "imports": [],
    }

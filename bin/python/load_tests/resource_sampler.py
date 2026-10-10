#!/usr/bin/env python3
"""resource_sampler.py — CPU/memory sampler that runs alongside a Locust run.

Launched by ``/api/testing/run-locust`` right after the Locust subprocess and
writes one CSV row per interval so the Load Testing UI can chart server
resources on the same timeline as the probe user's response times.

What it samples (every --interval seconds, default 5):
  host      /proc/stat CPU%, /proc/meminfo used/total, /proc/loadavg 1-min.
            Inside a standard Docker container /proc is the HOST's view (no
            lxcfs in these images), which is exactly what we want — but note
            it therefore includes es01/kib01/nginx activity too.
  container this process's own cgroup (the imports_container, where the web
            app, Locust, AND spawned imports all live): cgroup v2
            cpu.stat/memory.current/memory.max with v1 fallbacks. mem limit is
            meaningful because compose sets mem_limit on the container.
  es        es01 via the nodes-stats API (process CPU%, JVM heap used%),
            using the same lib.es_helpers client the web app uses. The only
            per-container view we can get of ES — no docker socket is mounted
            in imports_container, so `docker stats` is not an option.
  locust    the watched Locust process's own CPU (from /proc/<pid>/stat), so
            the load generator's overhead is visually subtractable.

All CPU percentages are normalized to the whole machine (0-100 across all
cores) so every series shares one axis.

Every column is failure-soft: a source that can't be read produces a blank
cell and one warning, never a crash — a half-monitored load test is far more
useful than an aborted one.

Lifecycle: --watch-pid makes the sampler exit within one interval of the
Locust process dying (verified by /proc/<pid>/cmdline containing "locust" —
same PID-reuse hardening as), so it cannot outlive the run even if
stop-locust is never called. --max-seconds (default 7200) is the backstop.

Usage:
  python3 /app/python/load_tests/resource_sampler.py \
      --out /app/data/logs/locust_resources.csv --interval 5 --watch-pid 1234
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# lib.es_helpers lives under bin/python — same sibling-path trick as the
# locustfile, since this script is launched by path, not as a module.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PYTHON_DIR = os.path.dirname(_HERE)
if _PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PYTHON_DIR)

logger = logging.getLogger("resource_sampler")

CSV_COLUMNS = [
    "timestamp",
    "host_cpu_pct",
    "host_mem_used_pct",
    "host_mem_used_mb",
    "host_mem_total_mb",
    "loadavg_1m",
    "container_cpu_pct",
    "container_mem_used_mb",
    "container_mem_limit_mb",
    "container_mem_pct",
    "es_process_cpu_pct",
    "es_heap_used_pct",
    "locust_cpu_pct",
]

_CGROUP_ROOT = "/sys/fs/cgroup"


# ---------------------------------------------------------------------------
# Pure parsers (string in, value out) — unit-tested with fixture text.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CpuTimes:
    """Aggregate CPU jiffies from /proc/stat's first line."""

    busy: int
    total: int


def parse_proc_stat(text: str) -> CpuTimes | None:
    """First ``cpu`` line of /proc/stat → busy/total jiffies. idle+iowait
    count as idle; everything else is busy."""
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != "cpu":
            continue
        try:
            values = [int(v) for v in parts[1:]]
        except ValueError:
            return None
        if len(values) < 5:
            return None
        idle = values[3] + values[4]  # idle + iowait
        total = sum(values)
        return CpuTimes(busy=total - idle, total=total)
    return None


def parse_meminfo(text: str) -> tuple[int, int] | None:
    """/proc/meminfo → (used_kb, total_kb). used = MemTotal - MemAvailable
    (the kernel's own 'how much could be reclaimed' estimate — matches
    ``free``'s available column, unlike MemFree)."""
    fields: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and key in ("MemTotal", "MemAvailable"):
            try:
                fields[key] = int(parts[0])
            except ValueError:
                return None
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    return (fields["MemTotal"] - fields["MemAvailable"], fields["MemTotal"])


def parse_loadavg(text: str) -> float | None:
    """/proc/loadavg → 1-minute load average."""
    parts = text.split()
    if not parts:
        return None
    try:
        return float(parts[0])
    except ValueError:
        return None


def parse_proc_pid_stat(text: str) -> int | None:
    """/proc/<pid>/stat → utime+stime jiffies. The comm field (2nd) may
    contain spaces and parentheses, so split after the LAST ')'."""
    _, _, after_comm = text.rpartition(")")
    parts = after_comm.split()
    # after_comm starts at field 3 ("state"); utime/stime are fields 14/15
    # of the full line → indices 11/12 here.
    if len(parts) < 13:
        return None
    try:
        return int(parts[11]) + int(parts[12])
    except ValueError:
        return None


def parse_cgroup_cpu_stat(text: str) -> int | None:
    """cgroup v2 cpu.stat → usage_usec."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "usage_usec":
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def parse_int(text: str) -> int | None:
    """Single-integer cgroup file (memory.current, cpuacct.usage, ...).
    Returns None for cgroup v2's literal ``max`` (no limit)."""
    stripped = text.strip()
    if stripped == "max":
        return None
    try:
        return int(stripped)
    except ValueError:
        return None


def cpu_pct(prev: CpuTimes | None, cur: CpuTimes | None) -> float | None:
    """Busy share of the delta between two /proc/stat samples, 0-100."""
    if prev is None or cur is None:
        return None
    dtotal = cur.total - prev.total
    if dtotal <= 0:
        return None
    return round(100.0 * (cur.busy - prev.busy) / dtotal, 2)


def usec_delta_pct(prev_usec: int | None, cur_usec: int | None, elapsed_s: float, ncpus: int) -> float | None:
    """CPU-time delta (microseconds) as a % of the whole machine over the
    elapsed wall-clock — comparable to the host CPU% series."""
    if prev_usec is None or cur_usec is None or elapsed_s <= 0 or ncpus <= 0:
        return None
    return round(100.0 * (cur_usec - prev_usec) / (elapsed_s * 1_000_000 * ncpus), 2)


def jiffies_delta_pct(prev: int | None, cur: int | None, elapsed_s: float, ncpus: int, hz: int) -> float | None:
    """Process jiffies delta as a % of the whole machine."""
    if prev is None or cur is None or elapsed_s <= 0 or ncpus <= 0 or hz <= 0:
        return None
    return round(100.0 * (cur - prev) / hz / (elapsed_s * ncpus), 2)


# ---------------------------------------------------------------------------
# Readers (filesystem/network edges) — injected into Sampler for tests.
# ---------------------------------------------------------------------------


def read_file(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def read_cgroup_cpu_usec(root: str = _CGROUP_ROOT) -> int | None:
    """Own-cgroup CPU usage in microseconds: v2 cpu.stat, else v1 cpuacct
    (nanoseconds, converted)."""
    text = read_file(os.path.join(root, "cpu.stat"))
    if text is not None:
        usec = parse_cgroup_cpu_stat(text)
        if usec is not None:
            return usec
    text = read_file(os.path.join(root, "cpu", "cpuacct.usage"))
    if text is not None:
        nsec = parse_int(text)
        if nsec is not None:
            return nsec // 1000
    return None


def read_cgroup_mem(root: str = _CGROUP_ROOT) -> tuple[int, int | None] | None:
    """Own-cgroup memory → (current_bytes, limit_bytes|None): v2
    memory.current/memory.max, else v1 usage_in_bytes/limit_in_bytes."""
    current_text = read_file(os.path.join(root, "memory.current"))
    if current_text is not None:
        current = parse_int(current_text)
        if current is not None:
            limit_text = read_file(os.path.join(root, "memory.max"))
            limit = parse_int(limit_text) if limit_text is not None else None
            return (current, limit)
    current_text = read_file(os.path.join(root, "memory", "memory.usage_in_bytes"))
    if current_text is not None:
        current = parse_int(current_text)
        if current is not None:
            limit_text = read_file(os.path.join(root, "memory", "memory.limit_in_bytes"))
            limit = parse_int(limit_text) if limit_text is not None else None
            # v1 reports "no limit" as a huge number (PAGE_COUNTER_MAX);
            # treat anything over 1 PiB as unlimited.
            if limit is not None and limit > 1 << 50:
                limit = None
            return (current, limit)
    return None


def read_es_stats() -> tuple[float | None, float | None]:
    """(process_cpu_pct, heap_used_pct) from the es01 nodes-stats API.
    Lazy client; any failure returns (None, None) — ES being busy or down is
    exactly the kind of signal the rest of the row still captures."""
    try:
        from lib.es_helpers import get_elasticsearch_client

        es = get_elasticsearch_client()
        if es is None:
            return (None, None)
        stats = es.nodes.stats(metric="process,jvm")
        nodes = dict(stats).get("nodes", {})
        for node in nodes.values():
            cpu = node.get("process", {}).get("cpu", {}).get("percent")
            heap = node.get("jvm", {}).get("mem", {}).get("heap_used_percent")
            return (
                float(cpu) if cpu is not None else None,
                float(heap) if heap is not None else None,
            )
    except Exception as exc:  # boundary: network/ES errors must not kill the sampler
        logger.warning("es stats unavailable: %s", exc)
    return (None, None)


def watched_pid_alive(pid: int) -> bool:
    """True while <pid> exists AND is still a locust process (earlier
    cmdline identity check, so PID reuse can't keep the sampler alive)."""
    cmdline = read_file(f"/proc/{pid}/cmdline")
    return cmdline is not None and "locust" in cmdline


# ---------------------------------------------------------------------------
# Sampler
# ---------------------------------------------------------------------------


class Sampler:
    """Holds previous-sample state and produces one CSV row per call."""

    def __init__(self, watch_pid: int | None, ncpus: int | None = None, hz: int | None = None) -> None:
        self.watch_pid = watch_pid
        self.ncpus = ncpus or os.cpu_count() or 1
        # SC_CLK_TCK is effectively always 100 on Linux; guarded for tests.
        try:
            self.hz = hz or int(os.sysconf("SC_CLK_TCK"))
        except (ValueError, OSError, AttributeError):
            self.hz = hz or 100
        self._prev_host: CpuTimes | None = None
        self._prev_cgroup_usec: int | None = None
        self._prev_locust_jiffies: int | None = None
        self._prev_time: float | None = None

    def row(self, now: float | None = None) -> dict[str, str]:
        """Sample everything once and return a CSV row dict. Blank cells for
        unavailable sources; deltas are blank on the first call."""
        now = now if now is not None else time.time()
        elapsed = (now - self._prev_time) if self._prev_time is not None else 0.0

        host_cpu = parse_proc_stat(read_file("/proc/stat") or "")
        meminfo = parse_meminfo(read_file("/proc/meminfo") or "")
        loadavg = parse_loadavg(read_file("/proc/loadavg") or "")
        cgroup_usec = read_cgroup_cpu_usec()
        cgroup_mem = read_cgroup_mem()
        es_cpu, es_heap = read_es_stats()
        locust_jiffies = parse_proc_pid_stat(read_file(f"/proc/{self.watch_pid}/stat") or "") if self.watch_pid else None

        row: dict[str, str] = {c: "" for c in CSV_COLUMNS}
        row["timestamp"] = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

        def put(column: str, value: float | int | None) -> None:
            if value is not None:
                row[column] = str(value)

        put("host_cpu_pct", cpu_pct(self._prev_host, host_cpu))
        if meminfo is not None:
            used_kb, total_kb = meminfo
            put("host_mem_used_pct", round(100.0 * used_kb / total_kb, 2) if total_kb else None)
            put("host_mem_used_mb", round(used_kb / 1024, 1))
            put("host_mem_total_mb", round(total_kb / 1024, 1))
        put("loadavg_1m", loadavg)
        put("container_cpu_pct", usec_delta_pct(self._prev_cgroup_usec, cgroup_usec, elapsed, self.ncpus))
        if cgroup_mem is not None:
            current_bytes, limit_bytes = cgroup_mem
            put("container_mem_used_mb", round(current_bytes / (1024 * 1024), 1))
            if limit_bytes:
                put("container_mem_limit_mb", round(limit_bytes / (1024 * 1024), 1))
                put("container_mem_pct", round(100.0 * current_bytes / limit_bytes, 2))
        put("es_process_cpu_pct", es_cpu)
        put("es_heap_used_pct", es_heap)
        put("locust_cpu_pct", jiffies_delta_pct(self._prev_locust_jiffies, locust_jiffies, elapsed, self.ncpus, self.hz))

        self._prev_host = host_cpu
        self._prev_cgroup_usec = cgroup_usec
        self._prev_locust_jiffies = locust_jiffies
        self._prev_time = now
        return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CPU/memory sampler for load-test runs.")
    parser.add_argument("--out", required=True, help="CSV output path.")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between samples (default 5).")
    parser.add_argument(
        "--watch-pid",
        type=int,
        default=None,
        help="Exit within one interval of this locust PID dying (cmdline-verified).",
    )
    parser.add_argument(
        "--max-seconds",
        type=int,
        default=7200,
        help="Hard runtime backstop (default 7200) in case --watch-pid is absent or wedged.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.interval < 1:
        logger.error("--interval must be >= 1 second")
        return 2

    sampler = Sampler(watch_pid=args.watch_pid)
    started = time.time()
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        fh.flush()
        while True:
            writer.writerow(sampler.row())
            fh.flush()
            if time.time() - started > args.max_seconds:
                logger.info("max-seconds reached; exiting")
                return 0
            if args.watch_pid is not None and not watched_pid_alive(args.watch_pid):
                logger.info("watched locust pid %s gone; exiting", args.watch_pid)
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())

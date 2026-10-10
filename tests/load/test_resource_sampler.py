"""Tests for load_tests.resource_sampler.

Parsers and delta math are pure (string fixtures — no /proc access, so these
run on any OS); Sampler.row is exercised with the reader edges monkeypatched.
"""

import pytest

from load_tests import resource_sampler as rs


class TestParseProcStat:
    def test_should_compute_busy_and_total_from_cpu_line(self):
        # user nice system idle iowait irq softirq steal guest guest_nice
        text = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 50 0 25 400 25 0 0 0 0 0\n"
        sample = rs.parse_proc_stat(text)
        assert sample == rs.CpuTimes(busy=150, total=1000)

    def test_should_return_none_for_garbage(self):
        assert rs.parse_proc_stat("not a stat file") is None
        assert rs.parse_proc_stat("cpu a b c d e") is None


class TestCpuPct:
    def test_should_compute_busy_share_of_delta(self):
        prev = rs.CpuTimes(busy=150, total=1000)
        cur = rs.CpuTimes(busy=450, total=2000)
        # 300 busy jiffies over a 1000-jiffy window = 30%
        assert rs.cpu_pct(prev, cur) == 30.0

    def test_should_return_none_without_previous_sample(self):
        assert rs.cpu_pct(None, rs.CpuTimes(1, 2)) is None

    def test_should_return_none_when_counter_does_not_advance(self):
        sample = rs.CpuTimes(busy=150, total=1000)
        assert rs.cpu_pct(sample, sample) is None


class TestParseMeminfo:
    def test_should_use_memavailable_not_memfree(self):
        text = "MemTotal:       8000000 kB\nMemFree:         500000 kB\nMemAvailable:   3000000 kB\n"
        assert rs.parse_meminfo(text) == (5000000, 8000000)

    def test_should_return_none_when_fields_missing(self):
        assert rs.parse_meminfo("MemTotal: 8000000 kB\n") is None


class TestParseProcPidStat:
    def test_should_sum_utime_and_stime(self):
        # comm contains spaces AND parens — split must use the LAST ')'.
        text = "1234 (python3 (weird)) S 1 1 1 0 -1 4194560 500 0 0 0 700 300 0 0 20 0 5 0 100 0 0"
        assert rs.parse_proc_pid_stat(text) == 1000

    def test_should_return_none_for_truncated_line(self):
        assert rs.parse_proc_pid_stat("1234 (x) S 1 1") is None


class TestCgroupReaders:
    def test_should_prefer_v2_cpu_stat(self, tmp_path):
        (tmp_path / "cpu.stat").write_text("usage_usec 123456\nuser_usec 100000\n")
        assert rs.read_cgroup_cpu_usec(str(tmp_path)) == 123456

    def test_should_fall_back_to_v1_cpuacct_nanoseconds(self, tmp_path):
        (tmp_path / "cpu").mkdir()
        (tmp_path / "cpu" / "cpuacct.usage").write_text("9876543210\n")
        assert rs.read_cgroup_cpu_usec(str(tmp_path)) == 9876543

    def test_should_read_v2_memory_with_max_as_no_limit(self, tmp_path):
        (tmp_path / "memory.current").write_text("1048576\n")
        (tmp_path / "memory.max").write_text("max\n")
        assert rs.read_cgroup_mem(str(tmp_path)) == (1048576, None)

    def test_should_treat_v1_huge_limit_as_unlimited(self, tmp_path):
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "memory.usage_in_bytes").write_text("2097152\n")
        (mem / "memory.limit_in_bytes").write_text(str(1 << 60))
        assert rs.read_cgroup_mem(str(tmp_path)) == (2097152, None)

    def test_should_return_none_when_no_cgroup_files(self, tmp_path):
        assert rs.read_cgroup_cpu_usec(str(tmp_path)) is None
        assert rs.read_cgroup_mem(str(tmp_path)) is None


class TestDeltaPcts:
    def test_usec_delta_normalized_across_cpus(self):
        # 2 CPUs, 5s elapsed: 5_000_000 usec of CPU time = 50% of the machine
        assert rs.usec_delta_pct(0, 5_000_000, 5.0, 2) == 50.0

    def test_jiffies_delta_normalized_across_cpus(self):
        # hz=100, 2 CPUs, 5s: 500 jiffies = 5s of CPU = 50% of the machine
        assert rs.jiffies_delta_pct(0, 500, 5.0, 2, 100) == 50.0

    def test_none_inputs_yield_none(self):
        assert rs.usec_delta_pct(None, 1, 5.0, 2) is None
        assert rs.jiffies_delta_pct(1, None, 5.0, 2, 100) is None


class TestSamplerRow:
    """Sampler.row with all reader edges faked — verifies the failure-soft
    contract (blank cells, never a crash) and the delta wiring."""

    @pytest.fixture
    def fake_readers(self, monkeypatch):
        files = {
            "/proc/stat": "cpu  100 0 50 800 50 0 0 0 0 0\n",
            "/proc/meminfo": "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n",
            "/proc/loadavg": "1.50 1.00 0.50 2/300 12345\n",
            "/proc/999/stat": "999 (locust) S 1 1 1 0 -1 0 0 0 0 0 100 100 0 0 20 0 5 0 100 0 0",
        }
        monkeypatch.setattr(rs, "read_file", lambda path: files.get(path))
        monkeypatch.setattr(rs, "read_cgroup_cpu_usec", lambda root=None: 1_000_000)
        monkeypatch.setattr(rs, "read_cgroup_mem", lambda root=None: (512 * 1024 * 1024, 1024 * 1024 * 1024))
        monkeypatch.setattr(rs, "read_es_stats", lambda: (12.0, 66.0))
        return files

    def test_first_row_has_absolutes_but_blank_deltas(self, fake_readers):
        sampler = rs.Sampler(watch_pid=999, ncpus=2, hz=100)
        row = sampler.row(now=1000.0)
        assert row["host_mem_used_pct"] == "50.0"
        assert row["host_mem_total_mb"] == "7812.5"
        assert row["loadavg_1m"] == "1.5"
        assert row["container_mem_used_mb"] == "512.0"
        assert row["container_mem_pct"] == "50.0"
        assert row["es_process_cpu_pct"] == "12.0"
        assert row["es_heap_used_pct"] == "66.0"
        # Deltas need a previous sample.
        assert row["host_cpu_pct"] == ""
        assert row["container_cpu_pct"] == ""
        assert row["locust_cpu_pct"] == ""

    def test_second_row_computes_deltas(self, fake_readers, monkeypatch):
        sampler = rs.Sampler(watch_pid=999, ncpus=2, hz=100)
        sampler.row(now=1000.0)
        # Advance every counter: +300 busy / +1000 total jiffies host;
        # +1s of cgroup CPU; +200 locust jiffies (2s CPU over 5s on 2 cpus).
        fake_readers["/proc/stat"] = "cpu  300 0 150 1350 200 0 0 0 0 0\n"
        fake_readers["/proc/999/stat"] = "999 (locust) S 1 1 1 0 -1 0 0 0 0 0 200 200 0 0 20 0 5 0 100 0 0"
        monkeypatch.setattr(rs, "read_cgroup_cpu_usec", lambda root=None: 2_000_000)
        row = sampler.row(now=1005.0)
        assert row["host_cpu_pct"] == "30.0"
        assert row["container_cpu_pct"] == "10.0"  # 1s CPU / (5s * 2 cpus)
        assert row["locust_cpu_pct"] == "20.0"  # 2s CPU / (5s * 2 cpus)

    def test_all_sources_failing_still_yields_a_row(self, monkeypatch):
        monkeypatch.setattr(rs, "read_file", lambda path: None)
        monkeypatch.setattr(rs, "read_cgroup_cpu_usec", lambda root=None: None)
        monkeypatch.setattr(rs, "read_cgroup_mem", lambda root=None: None)
        monkeypatch.setattr(rs, "read_es_stats", lambda: (None, None))
        row = rs.Sampler(watch_pid=None, ncpus=2, hz=100).row(now=1000.0)
        assert row["timestamp"]  # always present
        assert all(row[c] == "" for c in rs.CSV_COLUMNS if c != "timestamp")

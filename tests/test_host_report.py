#!/usr/bin/env python3
"""Formatting tests for mcp/host_report.py.

No database is used. The module is a pure formatter, carried over from the old
``tools/host-report`` service, so ordering, the stale boundary, sustained-signal
thresholds and the truncation notice are testable with plain dictionaries.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "mcp" / "host_report.py"
README = (ROOT / "README.md").read_text(encoding="utf-8")
# Prose assertions ignore line wrapping; the rendered example stays verbatim.
FLAT_README = " ".join(README.split())

spec = importlib.util.spec_from_file_location("host_report", MODULE)
host_report = importlib.util.module_from_spec(spec)
sys.modules["host_report"] = host_report
spec.loader.exec_module(host_report)

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)
GB = 1024 ** 3


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def row(
    host: str,
    *,
    samples: int = 10,
    age: int = 4,
    cpu_cores: int | None = 2,
    cpu_avg: float | None = 20.0,
    cpu_peak: float | None = 40.0,
    memory_avg: float | None = 30.0,
    memory_used_bytes: float | None = None,
    memory_total_bytes: float | None = 8 * GB,
    swap_avg: float | None = None,
    swap_peak: float | None = None,
    process_count: int | None = None,
    network_rx_mb: float | None = None,
    network_tx_mb: float | None = None,
    host_count: int | None = None,
    updated_at: str | None = None,
    load_avg: float | None = 1.0,
    load_peak: float | None = 2.0,
    disk: float | None = 50.0,
) -> dict:
    return {
        "host_label": host,
        "sample_count": samples,
        "last_seen": at(age) if samples else None,
        "cpu_cores": cpu_cores,
        "cpu_avg": cpu_avg,
        "cpu_peak": cpu_peak,
        "memory_avg": memory_avg,
        "memory_used_bytes_latest": memory_used_bytes,
        "memory_total_bytes": memory_total_bytes,
        "swap_avg": swap_avg,
        "swap_peak": swap_peak,
        "process_count_latest": process_count,
        "network_rx_mb_latest": network_rx_mb,
        "network_tx_mb_latest": network_tx_mb,
        "host_count": host_count,
        "updated_at_latest": updated_at,
        "load_avg": load_avg,
        "load_peak": load_peak,
        "disk": disk,
    }


class ShapeTests(unittest.TestCase):
    def test_line_order_matches_the_report_shape(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com")],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn(
            "- host1.example.com: 10 samples, last-seen 08:56; 2 cores; "
            "CPU 20% avg / 40% peak; mem 2.40 GB avg / 8.00 GB; "
            "load 1.00 avg / 2.00 peak; disk 50%",
            text,
        )

    def test_core_count_reads_singular_and_unknown(self) -> None:
        text = host_report.build_report(
            [
                row("host1.example.com", cpu_cores=1, load_peak=1.5),
                row("host2.example.com", cpu_cores=None),
            ],
            now=NOW,
            known_hosts=("host1.example.com", "host2.example.com"),
        )
        self.assertIn("last-seen 08:56; 1 core; CPU 20% avg", text)
        self.assertIn("last-seen 08:56; cores n/a; CPU 20% avg", text)

    def test_header_and_summary(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com"), row("host2.example.com", age=30)],
            now=NOW,
            known_hosts=("host1.example.com", "host2.example.com", "silent.example"),
        )
        self.assertTrue(
            text.startswith("**Host report** \u2014 31/10/2026 09:00 UTC\n")
        )
        self.assertIn("2/3 hosts reported, 2 stale, 2 flagged", text)

    def test_zero_samples_says_so_and_is_stale(self) -> None:
        text = host_report.build_report(
            [], now=NOW, known_hosts=("silent.example",)
        )
        self.assertIn(
            "- [!] silent.example: no samples in the last 24h; last-seen unknown",
            text,
        )
        self.assertIn("0/1 hosts reported, 1 stale, 1 flagged", text)

    def test_last_seen_uses_date_and_time_when_not_today(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com", age=20 * 60)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn("last-seen 30/10/2026 13:00", text)

    def test_rows_from_oracle_payload(self) -> None:
        rows = host_report.rows_from_oracle(
            {
                "columns": ["HOST_LABEL", "SAMPLE_COUNT", "LAST_SEEN", "CPU_CORES"],
                "rows": [["host1.example.com", 3, at(5), 4]],
            }
        )
        self.assertEqual(rows[0]["host_label"], "host1.example.com")
        self.assertEqual(rows[0]["sample_count"], 3)
        self.assertEqual(rows[0]["cpu_cores"], 4)


class SnapshotFigureTests(unittest.TestCase):
    """The view figures are carried on the summary; the digest reports memory in
    gigabytes when byte amounts are available."""

    def summary(self, **values):
        return host_report._summarise(
            "host1.example.com",
            row("host1.example.com", **values),
            now=NOW,
            stale_minutes=20,
        )

    def test_full_row_carries_every_snapshot_figure(self) -> None:
        summary = self.summary(
            memory_used_bytes=1395864371,
            memory_total_bytes=8375186227,
            swap_avg=2.3,
            swap_peak=6.1,
            process_count=165,
            network_rx_mb=75.0,
            network_tx_mb=31.0,
            host_count=3,
            updated_at="2026-10-31T08:59:00+00:00",
        )
        self.assertEqual(summary.memory_used_bytes, 1395864371)
        self.assertEqual(summary.memory_total_bytes, 8375186227)
        self.assertEqual(summary.swap_avg, 2.3)
        self.assertEqual(summary.swap_peak, 6.1)
        self.assertEqual(summary.process_count, 165)
        self.assertEqual(summary.network_rx_mb, 75.0)
        self.assertEqual(summary.network_tx_mb, 31.0)
        self.assertEqual(summary.host_count, 3)
        self.assertEqual(
            summary.updated_at, datetime(2026, 10, 31, 8, 59, tzinfo=timezone.utc)
        )

    def test_an_older_sample_carries_none(self) -> None:
        summary = self.summary(memory_total_bytes=None)
        self.assertIsNone(summary.memory_used_bytes)
        self.assertIsNone(summary.memory_total_bytes)
        self.assertIsNone(summary.swap_avg)
        self.assertIsNone(summary.swap_peak)
        self.assertIsNone(summary.process_count)
        self.assertIsNone(summary.network_rx_mb)
        self.assertIsNone(summary.network_tx_mb)
        self.assertIsNone(summary.host_count)
        self.assertIsNone(summary.updated_at)

    def test_the_digest_line_reports_memory_in_gigabytes(self) -> None:
        text = host_report.build_report(
            [
                row(
                    "host1.example.com",
                    memory_avg=30.0,
                    memory_used_bytes=1395864371,
                    memory_total_bytes=8 * GB,
                    swap_avg=2.3,
                    swap_peak=6.1,
                    process_count=165,
                    network_rx_mb=75.0,
                    network_tx_mb=31.0,
                    host_count=3,
                )
            ],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn(
            "- host1.example.com: 10 samples, last-seen 08:56; 2 cores; "
            "CPU 20% avg / 40% peak; mem 2.40 GB avg / 8.00 GB; "
            "load 1.00 avg / 2.00 peak; disk 50%",
            text,
        )
        for fragment in ("swap", "net ", "procs", "165", "fleet", "None"):
            self.assertNotIn(fragment, text)
        self.assertNotIn("mem 30%", text)

    def test_memory_falls_back_to_whole_percent_without_totals(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com", memory_avg=30.4, memory_total_bytes=None)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn("mem 30% avg", text)
        self.assertNotIn("GB", text)

    def test_rows_from_oracle_carries_the_new_view_columns(self) -> None:
        rows = host_report.rows_from_oracle(
            {
                "columns": [
                    "HOST_LABEL",
                    "SWAP_AVG",
                    "SWAP_PEAK",
                    "MEMORY_USED_BYTES_LATEST",
                    "MEMORY_TOTAL_BYTES",
                    "PROCESS_COUNT_LATEST",
                    "NETWORK_RX_MB_LATEST",
                    "NETWORK_TX_MB_LATEST",
                    "HOST_COUNT",
                    "UPDATED_AT_LATEST",
                ],
                "rows": [
                    [
                        "host1.example.com",
                        2.3,
                        6.1,
                        1395864371,
                        8375186227,
                        165,
                        75.0,
                        31.0,
                        3,
                        "2026-10-31T08:59:00+00:00",
                    ]
                ],
            }
        )
        record = rows[0]
        self.assertEqual(record["swap_avg"], 2.3)
        self.assertEqual(record["swap_peak"], 6.1)
        self.assertEqual(record["memory_used_bytes_latest"], 1395864371)
        self.assertEqual(record["memory_total_bytes"], 8375186227)
        self.assertEqual(record["process_count_latest"], 165)
        self.assertEqual(record["network_rx_mb_latest"], 75.0)
        self.assertEqual(record["network_tx_mb_latest"], 31.0)
        self.assertEqual(record["host_count"], 3)
        self.assertEqual(record["updated_at_latest"], "2026-10-31T08:59:00+00:00")


class OrderingTests(unittest.TestCase):
    def test_known_hosts_first_then_extras_in_name_order(self) -> None:
        text = host_report.build_report(
            [
                row("zulu.example"),
                row("alpha.example"),
                row("host2.example.com"),
            ],
            now=NOW,
            known_hosts=("host1.example.com", "host2.example.com"),
        )
        order = [
            line.split(":")[0].removeprefix("- ").removeprefix("[!] ")
            for line in text.splitlines()
            if line.startswith("- ")
        ]
        self.assertEqual(
            order,
            [
                "host1.example.com",
                "host2.example.com",
                "alpha.example",
                "zulu.example",
            ],
        )


class StaleTests(unittest.TestCase):
    def test_exactly_twenty_minutes_is_still_fresh(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com", age=20)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertNotIn("STALE", text)
        self.assertIn("0 stale", text)

    def test_older_than_twenty_minutes_is_stale(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com", age=21)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn("STALE: no sample in the last 20m", text)
        self.assertIn("1 stale", text)


class ThresholdTests(unittest.TestCase):
    """Only sustained signals flag a host. CPU and load peaks are reported as
    context but never flag by themselves; load is flagged only when the load
    peak is at least twice the host's core count."""

    def assert_flagged(self, **values) -> str:
        return host_report.build_report(
            [row("host1.example.com", **values)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )

    def test_cpu_average_boundary(self) -> None:
        self.assertIn("CPU avg 60% >= 60%", self.assert_flagged(cpu_avg=60.0))
        text = self.assert_flagged(cpu_avg=59.9)
        self.assertNotIn("CPU avg 60% >=", text)
        self.assertNotIn("[!]", text)

    def test_cpu_peak_alone_never_flags(self) -> None:
        text = self.assert_flagged(cpu_avg=20.0, cpu_peak=117.0)
        self.assertNotIn("[!]", text)
        self.assertIn("0 flagged", text)
        self.assertNotIn("CPU peak 117", text.split(" (", 1)[-1] if " (" in text else "")
        self.assertIn("CPU 20% avg / 117% peak", text)

    def test_memory_average_boundary(self) -> None:
        self.assertIn(
            "memory 6.80 GB avg / 8.00 GB, 85% >= 85%",
            self.assert_flagged(memory_avg=85.0),
        )
        text = self.assert_flagged(memory_avg=84.9)
        self.assertNotIn("85% >= 85%", text)
        self.assertNotIn("[!]", text)

    def test_disk_boundary(self) -> None:
        self.assertIn("disk 95% >= 95%", self.assert_flagged(disk=95.0))
        text = self.assert_flagged(disk=94.9)
        self.assertNotIn("disk 95% >=", text)
        self.assertNotIn("[!]", text)
        # photos sat at 84.8%; a host there must not be flagged.
        text = self.assert_flagged(disk=84.8)
        self.assertNotIn("disk 85% >=", text)
        self.assertNotIn("disk 95% >=", text)
        self.assertNotIn("[!]", text)

    def test_load_peak_boundary_is_twice_the_core_count(self) -> None:
        text = self.assert_flagged(cpu_cores=2, load_avg=1.0, load_peak=4.0)
        self.assertIn("load peak 4.00 >= 4 (2x2 cores)", text)
        below = self.assert_flagged(cpu_cores=2, load_peak=3.99)
        self.assertNotIn("load peak 3.99 >=", below)
        self.assertNotIn("[!]", below)

    def test_a_bigger_host_gets_a_bigger_load_threshold(self) -> None:
        # The same load peak is a flag on 2 cores and fine on 4.
        self.assertIn(
            "load peak 7.00 >= 4 (2x2 cores)",
            self.assert_flagged(cpu_cores=2, load_peak=7.0),
        )
        self.assertNotIn("[!]", self.assert_flagged(cpu_cores=4, load_peak=7.0))
        self.assertIn(
            "load peak 8.00 >= 8 (2x4 cores)",
            self.assert_flagged(cpu_cores=4, load_peak=8.0),
        )

    def test_load_peak_never_flags_without_a_core_count(self) -> None:
        text = self.assert_flagged(cpu_cores=None, load_peak=99.0)
        self.assertNotIn("load peak 99.00 >=", text)
        self.assertNotIn("[!]", text)
        self.assertIn("load 1.00 avg / 99.00 peak", text)

    def test_reason_text_uses_whole_percentages_gigabytes_and_cores(self) -> None:
        for values, reason in (
            ({"cpu_avg": 76.7}, "(CPU avg 77% >= 60%)"),
            (
                {"memory_avg": 88.0},
                "(memory 7.04 GB avg / 8.00 GB, 88% >= 85%)",
            ),
            ({"disk": 95.4}, "(disk 95% >= 95%)"),
            (
                {"cpu_cores": 2, "load_peak": 8.74},
                "(load peak 8.74 >= 4 (2x2 cores))",
            ),
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, self.assert_flagged(**values))

    def test_flags_are_inline_and_marker_is_present(self) -> None:
        text = self.assert_flagged(
            cpu_avg=61.0,
            cpu_peak=117.0,
            memory_avg=86.0,
            load_peak=4.5,
            disk=96.0,
        )
        self.assertIn("- [!] host1.example.com:", text)
        self.assertIn(
            "(CPU avg 61% >= 60%; memory 6.88 GB avg / 8.00 GB, 86% >= 85%; "
            "load peak 4.50 >= 4 (2x2 cores); disk 96% >= 95%)",
            text,
        )
        self.assertNotIn("CPU peak 117% >=", text)

    def test_stale_still_comes_first(self) -> None:
        text = self.assert_flagged(age=30, cpu_avg=99.0)
        self.assertIn(
            "(STALE: no sample in the last 20m; CPU avg 99% >= 60%)", text
        )


class TruncationTests(unittest.TestCase):
    def test_drops_whole_lines_and_reports_the_count(self) -> None:
        known = tuple(f"host-{index:02d}.example" for index in range(30))
        rows = [row(host) for host in known]
        text = host_report.build_report(
            rows, now=NOW, known_hosts=known, limit=400
        )
        self.assertLessEqual(len(text), 400)
        self.assertRegex(
            text,
            r"\n\u2026 truncated: \d+ host line\(s\) not shown$",
        )
        shown = sum(1 for line in text.splitlines() if line.startswith("- "))
        dropped = int(
            text.rsplit("truncated: ", 1)[1].split(" host line", 1)[0]
        )
        self.assertEqual(shown + dropped, len(known))
        # Every retained line is complete, never a partial line from _fit.
        for line in text.splitlines():
            if line.startswith("- "):
                self.assertIn("; disk ", line)


class DigestTests(unittest.TestCase):
    def test_ssh_success_is_folded_into_the_host_line(self) -> None:
        text = host_report.build_digest(
            [row("host1.example.com")],
            ssh_results=[
                {"host": "host1.example.com", "status": "ok", "latency_ms": 42}
            ],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn("disk 50%; SSH ok (42 ms)", text)

    def test_ssh_failures_keep_their_class_and_reason(self) -> None:
        text = host_report.build_digest(
            [row("host1.example.com")],
            ssh_results=[
                {
                    "host": "host1.example.com",
                    "status": "auth_refused",
                    "message": "authentication refused",
                }
            ],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn(
            "SSH auth_refused (authentication refused)", text
        )

    def test_refused_statuses_are_never_rendered(self) -> None:
        text = host_report.build_digest(
            [row("host1.example.com")],
            ssh_results=[
                {
                    "host": "host2.example.com",
                    "status": "out_of_scope",
                    "message": "host is outside this run's delivery scope",
                }
            ],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertNotIn("host2.example.com", text)
        self.assertNotIn("out_of_scope", text)

    def test_not_configured_is_said_once_without_implying_health(self) -> None:
        text = host_report.build_digest(
            [row("host1.example.com")],
            ssh_configured=False,
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertEqual(
            text.count("SSH not configured; no SSH health was checked."), 1
        )
        self.assertNotIn("SSH ok", text)


class ReadmeTests(unittest.TestCase):
    """The README example and threshold paragraph are part of the contract."""

    def test_the_readme_example_is_what_the_formatter_renders(self) -> None:
        rows = [
            row(
                "host1.example.com",
                samples=53,
                age=4,
                cpu_cores=2,
                cpu_avg=44.8,
                cpu_peak=117.0,
                memory_avg=22.6,
                memory_total_bytes=8 * GB,
                load_avg=8.74,
                load_peak=9.50,
                disk=30.9,
            ),
            row(
                "host2.example.com",
                samples=46,
                age=3,
                cpu_cores=2,
                cpu_avg=22.8,
                cpu_peak=28.0,
                memory_avg=30.6,
                memory_total_bytes=8 * GB,
                load_avg=0.23,
                load_peak=0.28,
                disk=80.3,
            ),
            row(
                "host3.example.com",
                samples=49,
                age=3,
                cpu_cores=4,
                cpu_avg=17.9,
                cpu_peak=25.2,
                memory_avg=75.7,
                memory_total_bytes=16 * GB,
                load_avg=3.57,
                load_peak=5.04,
                disk=71.2,
            ),
        ]
        text = host_report.build_digest(
            rows,
            ssh_results=[
                {"host": "host1.example.com", "status": "ok", "latency_ms": 42},
                {
                    "host": "host2.example.com",
                    "status": "auth_refused",
                    "message": "authentication refused",
                },
                {"host": "host3.example.com", "status": "ok", "latency_ms": 61},
            ],
            now=NOW,
            known_hosts=(
                "host1.example.com",
                "host2.example.com",
                "host3.example.com",
            ),
        )
        self.assertIn(text, README)

    def test_the_readme_states_the_sustained_rule(self) -> None:
        self.assertIn("average CPU at or above 60 %", FLAT_README)
        self.assertIn("average memory at or above 85 %", FLAT_README)
        self.assertIn("disk from the latest sample at or above 95 %", FLAT_README)
        self.assertIn(
            "load peak at or above twice the host's core count", FLAT_README
        )
        self.assertIn("CPU peak is never a flag", FLAT_README)
        self.assertIn("memory in gigabytes", FLAT_README.lower())
        self.assertIn("percentages without decimals", FLAT_README.lower())
        self.assertIn("d/m/Y H:i", FLAT_README)


if __name__ == "__main__":
    unittest.main()

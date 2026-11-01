#!/usr/bin/env python3
"""Formatting tests for mcp/host_report.py.

No database is used. The module is a pure formatter, carried over from the old
``tools/host-report`` service, so ordering, the stale boundary, every threshold
and the truncation notice are testable with plain dictionaries.

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

spec = importlib.util.spec_from_file_location("host_report", MODULE)
host_report = importlib.util.module_from_spec(spec)
sys.modules["host_report"] = host_report
spec.loader.exec_module(host_report)

NOW = datetime(2026, 10, 31, 9, 0, tzinfo=timezone.utc)


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def row(
    host: str,
    *,
    samples: int = 10,
    age: int = 4,
    cpu_avg: float | None = 20.0,
    cpu_peak: float | None = 40.0,
    memory_avg: float | None = 30.0,
    load_avg: float | None = 1.0,
    load_peak: float | None = 2.0,
    disk: float | None = 50.0,
) -> dict:
    return {
        "host_label": host,
        "sample_count": samples,
        "last_seen": at(age) if samples else None,
        "cpu_avg": cpu_avg,
        "cpu_peak": cpu_peak,
        "memory_avg": memory_avg,
        "load_avg": load_avg,
        "load_peak": load_peak,
        "disk": disk,
    }


class ShapeTests(unittest.TestCase):
    def test_line_order_matches_the_old_report(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com")],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        self.assertIn(
            "- host1.example.com: 10 samples, last-seen 4m ago; "
            "CPU 20.0% avg / 40.0% peak; mem 30.0% avg; "
            "load 1.00 avg / 2.00 peak; disk 50.0%",
            text,
        )

    def test_header_and_summary(self) -> None:
        text = host_report.build_report(
            [row("host1.example.com"), row("host2.example.com", age=30)],
            now=NOW,
            known_hosts=("host1.example.com", "host2.example.com", "silent.example"),
        )
        self.assertTrue(
            text.startswith("**Host report** \u2014 2026-10-31 09:00 UTC\n")
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

    def test_rows_from_oracle_payload(self) -> None:
        rows = host_report.rows_from_oracle(
            {
                "columns": ["HOST_LABEL", "SAMPLE_COUNT", "LAST_SEEN"],
                "rows": [["host1.example.com", 3, at(5)]],
            }
        )
        self.assertEqual(rows[0]["host_label"], "host1.example.com")
        self.assertEqual(rows[0]["sample_count"], 3)


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
    def assert_flagged(self, **values) -> str:
        text = host_report.build_report(
            [row("host1.example.com", **values)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        return text

    def test_cpu_peak_boundary(self) -> None:
        self.assertIn("CPU peak 90.0% >= 90%", self.assert_flagged(cpu_peak=90.0))
        self.assertNotIn("CPU peak", self.assert_flagged(cpu_peak=89.9))

    def test_memory_average_boundary(self) -> None:
        self.assertIn("memory 90.0% >= 90%", self.assert_flagged(memory_avg=90.0))
        self.assertNotIn("memory 90", self.assert_flagged(memory_avg=89.9))

    def test_load_peak_boundary(self) -> None:
        self.assertIn("load peak 8.00 >= 8", self.assert_flagged(load_peak=8.0))
        self.assertNotIn("load peak", self.assert_flagged(load_peak=7.99))

    def test_disk_boundary(self) -> None:
        self.assertIn("disk 90.0% >= 90%", self.assert_flagged(disk=90.0))
        self.assertNotIn("disk 90.0% >= 90%", self.assert_flagged(disk=89.9))

    def test_flags_are_inline_and_marker_is_present(self) -> None:
        text = self.assert_flagged(
            cpu_peak=91.0, memory_avg=92.0, load_peak=9.0, disk=93.0
        )
        self.assertIn("- [!] host1.example.com:", text)
        self.assertIn(
            "(CPU peak 91.0% >= 90%; memory 92.0% >= 90%; "
            "load peak 9.00 >= 8; disk 93.0% >= 90%)",
            text,
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
        self.assertIn("disk 50.0%; SSH ok (42 ms)", text)

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


if __name__ == "__main__":
    unittest.main()

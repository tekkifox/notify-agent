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
README = (ROOT / "README.md").read_text(encoding="utf-8")
# Prose assertions ignore line wrapping; the rendered example stays verbatim.
FLAT_README = " ".join(README.split())

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
    cpu_cores: int | None = 2,
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
        "cpu_cores": cpu_cores,
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
            "- host1.example.com: 10 samples, last-seen 4m ago; 2 cores; "
            "CPU 20.0% avg / 40.0% peak; mem 30.0% avg; "
            "load 1.00 avg / 2.00 peak; disk 50.0%",
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
        self.assertIn("last-seen 4m ago; 1 core; CPU 20.0% avg", text)
        self.assertIn("last-seen 4m ago; cores n/a; CPU 20.0% avg", text)

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
    """Only sustained signals flag a host; a peak never does, including the
    load peak, which is compared as an average against the host's own cores."""

    def assert_flagged(self, **values) -> str:
        text = host_report.build_report(
            [row("host1.example.com", **values)],
            now=NOW,
            known_hosts=("host1.example.com",),
        )
        return text

    def test_cpu_average_boundary(self) -> None:
        self.assertIn(
            "CPU avg 60.0% >= 60%", self.assert_flagged(cpu_avg=60.0)
        )
        text = self.assert_flagged(cpu_avg=59.9)
        self.assertNotIn("CPU avg 59.9% >=", text)
        self.assertNotIn("[!]", text)

    def test_memory_average_boundary(self) -> None:
        self.assertIn(
            "memory 85.0% >= 85%", self.assert_flagged(memory_avg=85.0)
        )
        text = self.assert_flagged(memory_avg=84.9)
        self.assertNotIn("memory 84.9% >=", text)
        self.assertNotIn("[!]", text)

    def test_disk_boundary(self) -> None:
        self.assertIn("disk 85.0% >= 85%", self.assert_flagged(disk=85.0))
        text = self.assert_flagged(disk=84.9)
        self.assertNotIn("disk 84.9% >=", text)
        self.assertNotIn("[!]", text)

    def test_load_average_boundary_is_twice_the_core_count(self) -> None:
        text = self.assert_flagged(cpu_cores=2, load_avg=4.0)
        self.assertIn("load avg 4.00 >= 2x2 cores", text)
        below = self.assert_flagged(cpu_cores=2, load_avg=3.99)
        self.assertNotIn("load avg 3.99 >=", below)
        self.assertNotIn("[!]", below)

    def test_a_bigger_host_gets_a_bigger_load_threshold(self) -> None:
        # The same average load is a flag on 2 cores and fine on 4.
        self.assertIn(
            "load avg 7.00 >= 2x2 cores",
            self.assert_flagged(cpu_cores=2, load_avg=7.0),
        )
        self.assertNotIn("[!]", self.assert_flagged(cpu_cores=4, load_avg=7.0))
        self.assertIn(
            "load avg 8.00 >= 2x4 cores",
            self.assert_flagged(cpu_cores=4, load_avg=8.0),
        )

    def test_a_huge_load_peak_with_a_modest_average_never_flags(self) -> None:
        # The reboot burst that started this rule: 27.18 on 8 cores was a peak.
        text = self.assert_flagged(
            cpu_cores=8, load_avg=1.5, load_peak=27.18
        )
        self.assertNotIn("[!]", text)
        self.assertNotIn("load avg 1.50 >=", text)
        self.assertIn("load 1.50 avg / 27.18 peak", text)

    def test_load_never_flags_without_a_core_count(self) -> None:
        text = self.assert_flagged(cpu_cores=None, load_avg=99.0)
        self.assertNotIn("load avg 99.00 >=", text)
        self.assertNotIn("[!]", text)

    def test_cpu_peak_is_context_and_never_flags(self) -> None:
        text = self.assert_flagged(cpu_avg=10.0, cpu_peak=437.0)
        self.assertNotIn("[!]", text)
        self.assertNotIn("CPU peak 437.0% >=", text)
        self.assertIn("CPU 10.0% avg / 437.0% peak", text)

    def test_reason_text_matches_the_prompt_examples(self) -> None:
        # prompts/notify.md rule 29 names these exact strings.
        for values, reason in (
            ({"cpu_avg": 76.7}, "(CPU avg 76.7% >= 60%)"),
            ({"memory_avg": 88.0}, "(memory 88.0% >= 85%)"),
            ({"disk": 85.4}, "(disk 85.4% >= 85%)"),
            ({"cpu_cores": 2, "load_avg": 8.74}, "(load avg 8.74 >= 2x2 cores)"),
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, self.assert_flagged(**values))

    def test_flags_are_inline_and_marker_is_present(self) -> None:
        text = self.assert_flagged(
            cpu_avg=61.0,
            memory_avg=86.0,
            load_avg=4.5,
            disk=86.0,
        )
        self.assertIn("- [!] host1.example.com:", text)
        self.assertIn(
            "(CPU avg 61.0% >= 60%; memory 86.0% >= 85%; "
            "disk 86.0% >= 85%; load avg 4.50 >= 2x2 cores)",
            text,
        )

    def test_stale_still_comes_first(self) -> None:
        text = self.assert_flagged(age=30, cpu_avg=99.0)
        self.assertIn(
            "(STALE: no sample in the last 20m; CPU avg 99.0% >= 60%)", text
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
        self.assertIn("disk from the latest sample at or above 85 %", FLAT_README)
        self.assertIn("average load at or above twice that host's own core count", FLAT_README)
        self.assertIn("CPU peak and load peak are context and never flag a host", FLAT_README)
        # The old rule must not creep back into the prose.
        self.assertNotIn("The inclusive thresholds are CPU peak 90", FLAT_README)
        self.assertNotIn("(CPU peak 117.0% >= 90%)", FLAT_README)
        self.assertNotRegex(
            FLAT_README, r"load peak [\d.]+ >= [\d.]+(?![x\d])"
        )
        self.assertNotIn("(load peak", FLAT_README)


if __name__ == "__main__":
    unittest.main()

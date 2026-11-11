#!/usr/bin/env python3
"""Tests for the Oracle series query behind the on-demand charts.

No database is used. The query builder, row parser, bucket choice and host
validation are pure, and the one integration test stubs ``fetch``.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import host_series  # noqa: E402

try:
    import PIL  # noqa: F401
except ImportError:  # pragma: no cover - the Docker image installs Pillow
    HAS_PIL = False
else:
    HAS_PIL = True


class BucketTests(unittest.TestCase):
    def test_bucket_keeps_the_point_count_sane(self) -> None:
        cases = {
            5: 1,
            60: 1,
            70: 2,
            90: 2,
            1440: 30,
            10080: 180,
            43200: 720,
        }
        for window, expected in cases.items():
            with self.subTest(window=window):
                self.assertEqual(
                    host_series.bucket_minutes_for_window(window), expected
                )

    def test_a_chart_never_has_more_than_sixty_points(self) -> None:
        for window in (5, 60, 70, 360, 1440, 2880, 10080, 20160, 43200):
            with self.subTest(window=window):
                bucket = host_series.bucket_minutes_for_window(window)
                points = window / bucket
                self.assertLessEqual(points, 60)
                self.assertGreaterEqual(points, 5)

    def test_a_day_uses_half_hour_buckets(self) -> None:
        expression = host_series.bucket_expression(30)
        self.assertIn("TRUNC(sampled_at, 'MI')", expression)

    def test_a_whole_day_aligns_to_the_day(self) -> None:
        self.assertIn("TRUNC(CAST(sampled_at AS DATE))", host_series.bucket_expression(1440))

    def test_an_hour_uses_the_hour_truncation(self) -> None:
        self.assertEqual(
            host_series.bucket_expression(60), "TRUNC(sampled_at, 'HH24')"
        )


class QueryTests(unittest.TestCase):
    def test_the_query_is_cpu_and_memory_only(self) -> None:
        sql, binds = host_series.build_sql(["media.example.com"], 70)
        self.assertIn("AVG(cpu_percent) AS cpu", sql)
        self.assertIn("AVG(memory_percent) AS memory", sql)
        self.assertNotIn("load_avg", sql)
        self.assertNotIn("disk_percent", sql)
        self.assertIn("NOTIFY_RO.HOST_STATS_V", sql)
        self.assertIn("NUMTODSINTERVAL(:window, 'MINUTE')", sql)
        self.assertIn(":host0", sql)
        self.assertEqual(binds, {"window": 70, "host0": "media.example.com"})

    def test_hosts_are_bind_values_not_sql(self) -> None:
        hosts = ["a.example.com", "b.example.com"]
        sql, binds = host_series.build_sql(hosts, 60)
        self.assertNotIn("a.example.com", sql)
        self.assertNotIn("b.example.com", sql)
        self.assertEqual(binds["host0"], "a.example.com")
        self.assertEqual(binds["host1"], "b.example.com")

    def test_host_validation_rejects_injection_and_extra_hosts(self) -> None:
        with self.assertRaises(host_series.SeriesError):
            host_series._validated_hosts(["x'; DROP TABLE"])
        with self.assertRaises(host_series.SeriesError):
            host_series._validated_hosts(["a..b"])
        many = [f"host{index}.example.com" for index in range(12)]
        self.assertEqual(len(host_series._validated_hosts(many)), 10)

    def test_rows_are_grouped_and_sorted(self) -> None:
        columns = ["HOST_LABEL", "BUCKET", "CPU", "MEMORY"]
        rows = [
            ["b.example.com", "2026-11-11T09:02:00+00:00", 20, 40],
            ["a.example.com", "2026-11-11T09:01:00+00:00", 10, 30],
            ["a.example.com", "2026-11-11T09:00:00+00:00", 5, 25],
        ]
        document = host_series.parse_rows(columns, rows, 70)
        self.assertEqual(document["window_minutes"], 70)
        self.assertEqual(
            [host["host_label"] for host in document["hosts"]],
            ["b.example.com", "a.example.com"],
        )
        self.assertEqual(
            [point["cpu"] for point in document["hosts"][1]["points"]],
            [5.0, 10.0],
        )

    def test_a_malformed_row_is_an_error(self) -> None:
        with self.assertRaises(host_series.SeriesError):
            host_series.parse_rows(["host_label", "bucket"], [["only-one"]], 60)


@unittest.skipUnless(HAS_PIL, "Pillow is required to render charts")
class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)

    def test_main_fetches_and_renders_one_png_per_host(self) -> None:
        columns = ["host_label", "bucket", "cpu", "memory"]
        rows = [
            ["a.example.com", "2026-11-11T09:00:00+00:00", 10, 30],
            ["a.example.com", "2026-11-11T09:01:00+00:00", 12, 32],
            ["b.example.com", "2026-11-11T09:00:00+00:00", 20, 40],
        ]
        with mock.patch.object(
            host_series, "fetch", return_value=(columns, rows)
        ) as fetch:
            result = host_series.main(
                [
                    "--out", str(self.dir),
                    "--window", "60",
                    "--host", "a.example.com",
                    "--host", "b.example.com",
                ]
            )
        self.assertEqual(result, 0)
        fetch.assert_called_once()
        self.assertEqual(
            sorted(path.name for path in self.dir.iterdir()),
            ["a.example.com.png", "b.example.com.png"],
        )

    def test_an_empty_series_is_not_a_failure_and_writes_nothing(self) -> None:
        with mock.patch.object(
            host_series, "fetch", return_value=(
                ["host_label", "bucket", "cpu", "memory"], []
            )
        ):
            result = host_series.main(
                ["--out", str(self.dir), "--window", "60", "--host", "a.example.com"]
            )
        self.assertEqual(result, 0)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_query_failure_returns_nonzero(self) -> None:
        with mock.patch.object(
            host_series, "fetch", side_effect=host_series.SeriesError("oracle error ORA-00942")
        ):
            result = host_series.main(
                ["--out", str(self.dir), "--window", "60", "--host", "a.example.com"]
            )
        self.assertEqual(result, 1)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_window_outside_the_bounds_is_refused(self) -> None:
        for window in ("4", "43201"):
            with self.subTest(window=window):
                result = host_series.main(
                    ["--out", str(self.dir), "--window", window, "--host", "a.example.com"]
                )
                self.assertEqual(result, 1)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_a_failed_render_leaves_no_partial_png(self) -> None:
        columns = ["host_label", "bucket", "cpu", "memory"]
        rows = [["a.example.com", "2026-11-11T09:00:00+00:00", 10, 30]]
        with mock.patch.object(host_series, "fetch", return_value=(columns, rows)):
            with mock.patch.object(
                host_series.host_chart,
                "render_payload",
                side_effect=host_series.host_chart.ChartError("boom"),
            ):
                result = host_series.main(
                    ["--out", str(self.dir), "--window", "60", "--host", "a.example.com"]
                )
        self.assertEqual(result, 1)
        self.assertEqual(list(self.dir.glob("*.png")), [])


if __name__ == "__main__":
    unittest.main()

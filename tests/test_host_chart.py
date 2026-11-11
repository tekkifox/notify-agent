#!/usr/bin/env python3
"""Tests for the host chart renderer.

The renderer is pure: JSON in, PNG out. No database, Discord or network is
used. Pillow is the only third-party dependency; the tests that need it skip
with a named reason when it is unavailable.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "host_chart.py"

try:
    import PIL  # noqa: F401
except ImportError:  # pragma: no cover - the Docker image installs Pillow
    HAS_PIL = False
else:
    HAS_PIL = True

spec = importlib.util.spec_from_file_location("host_chart", MODULE)
host_chart = importlib.util.module_from_spec(spec)
sys.modules["host_chart"] = host_chart
spec.loader.exec_module(host_chart)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def payload(host: str = "media.example.com", **overrides: object) -> dict:
    points = [
        {
            "at": f"2026-11-11T{9 + index:02d}:00:00+00:00",
            "cpu": 10.0 + index * 4,
            "memory": 40.0 + index * 3,
        }
        for index in range(6)
    ]
    document = {"window_minutes": 70, "hosts": [{"host_label": host, "points": points}]}
    document.update(overrides)
    return document


def ihdr(data: bytes) -> tuple[int, int]:
    assert data[:8] == PNG_SIGNATURE
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


class HelperTests(unittest.TestCase):
    def test_slug_keeps_the_host_name_readable(self) -> None:
        self.assertEqual(
            host_chart.slug_for("media.example.com"), "media.example.com"
        )
        self.assertEqual(
            host_chart.slug_for(" Media.Example.COM. "), "media.example.com"
        )
        self.assertEqual(host_chart.slug_for("///"), "host")

    def test_window_labels(self) -> None:
        self.assertEqual(host_chart.format_window(70), "70 min")
        self.assertEqual(host_chart.format_window(1440), "24 h")
        self.assertEqual(host_chart.format_window(90), "90 min")
        self.assertEqual(host_chart.format_window(150), "2 h 30 min")

    def test_a_constant_series_still_has_a_visible_band(self) -> None:
        for value in (0.0, 42.0, 100.0):
            with self.subTest(value=value):
                low, high = host_chart._range_for([value, value])
                self.assertLess(low, high)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 100.0)

    def test_percent_ranges_never_leave_zero_to_one_hundred(self) -> None:
        for values in (
            [-50.0, 150.0, None],
            [99.9, 100.0],
            [0.0, 0.1],
            [None, None],
        ):
            with self.subTest(values=values):
                low, high = host_chart._range_for(values)
                self.assertGreaterEqual(low, 0.0)
                self.assertLessEqual(high, 100.0)
                self.assertLess(low, high)

    def test_a_null_breaks_each_series(self) -> None:
        points = [
            (host_chart._parse_time("2026-11-11T09:00:00+00:00"), 10.0, 20.0),
            (host_chart._parse_time("2026-11-11T09:01:00+00:00"), None, 22.0),
            (host_chart._parse_time("2026-11-11T09:02:00+00:00"), 14.0, None),
            (host_chart._parse_time("2026-11-11T09:03:00+00:00"), 16.0, 26.0),
        ]
        cpu = host_chart.segments(points, 1)
        memory = host_chart.segments(points, 2)
        self.assertEqual([len(run) for run in cpu], [1, 2])
        self.assertEqual([len(run) for run in memory], [2, 1])

    def test_a_z_timestamp_is_accepted(self) -> None:
        moment = host_chart._parse_time("2026-11-11T09:00:00Z")
        self.assertEqual(moment.hour, 9)

    def test_bad_input_is_a_chart_error(self) -> None:
        for document in (
            [],
            {"window_minutes": 0, "hosts": []},
            {"window_minutes": 70, "hosts": "no"},
            {"window_minutes": 70, "hosts": [{"host_label": ""}]},
            {
                "window_minutes": 70,
                "hosts": [
                    {
                        "host_label": "x",
                        "points": [{"at": "not-a-time", "cpu": 1}],
                    }
                ],
            },
            {
                "window_minutes": 70,
                "hosts": [
                    {
                        "host_label": "x",
                        "points": [{"at": "2026-11-11T09:00:00Z", "cpu": "1"}],
                    }
                ],
            },
        ):
            with self.subTest(document=document):
                with self.assertRaises(host_chart.ChartError):
                    host_chart.render_payload(document, tempfile.mkdtemp())


@unittest.skipUnless(HAS_PIL, "Pillow is required to render charts")
class RenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = pathlib.Path(self.temp.name)

    def test_a_png_is_a_valid_image_with_the_expected_size(self) -> None:
        written = host_chart.render_payload(payload(), self.dir)
        self.assertEqual(len(written), 1)
        data = written[0].read_bytes()
        self.assertEqual(data[:8], PNG_SIGNATURE)
        self.assertEqual(ihdr(data), (900, 520))
        self.assertEqual(written[0].name, "media.example.com.png")
        self.assertLess(len(data), 150_000)

    def test_a_different_series_produces_a_different_png(self) -> None:
        first = host_chart.render_payload(payload(), self.dir)
        before = first[0].read_bytes()
        changed = payload()
        changed["hosts"][0]["points"][0]["cpu"] = 99.0
        second = host_chart.render_payload(changed, self.dir)
        self.assertNotEqual(before, second[0].read_bytes())

    def test_one_file_per_host(self) -> None:
        document = {
            "window_minutes": 60,
            "hosts": [
                {"host_label": "a.example.com", "points": payload()["hosts"][0]["points"]},
                {"host_label": "b.example.com", "points": payload()["hosts"][0]["points"]},
            ],
        }
        written = host_chart.render_payload(document, self.dir)
        self.assertEqual(
            sorted(path.name for path in written),
            ["a.example.com.png", "b.example.com.png"],
        )

    def test_a_host_with_no_points_produces_no_file(self) -> None:
        document = {
            "window_minutes": 60,
            "hosts": [{"host_label": "empty.example.com", "points": []}],
        }
        written = host_chart.render_payload(document, self.dir)
        self.assertEqual(written, [])
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_out_of_range_percentages_are_clamped_not_drawn_outside(self) -> None:
        document = payload()
        document["hosts"][0]["points"][0]["cpu"] = 150
        document["hosts"][0]["points"][1]["memory"] = -20
        written = host_chart.render_payload(document, self.dir)
        self.assertEqual(len(written), 1)
        self.assertEqual(ihdr(written[0].read_bytes()), (900, 520))

    def test_bad_input_writes_no_partial_png(self) -> None:
        document = payload()
        document["hosts"][0]["points"][2]["at"] = "not-a-time"
        with self.assertRaises(host_chart.ChartError):
            host_chart.render_payload(document, self.dir)
        self.assertEqual(list(self.dir.glob("*.png")), [])

    def test_cli_reads_stdin_and_a_file(self) -> None:
        document = payload()
        result = subprocess.run(
            [sys.executable, str(MODULE), str(self.dir)],
            input=json.dumps(document),
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.dir / "media.example.com.png").is_file())

        source = self.dir / "input.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        second = self.dir / "second"
        result = subprocess.run(
            [sys.executable, str(MODULE), str(second), str(source)],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((second / "media.example.com.png").is_file())

    def test_cli_fails_loudly_on_bad_input(self) -> None:
        result = subprocess.run(
            [sys.executable, str(MODULE), str(self.dir)],
            input="{not json",
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("host_chart:", result.stderr)
        self.assertEqual(list(self.dir.glob("*.png")), [])


if __name__ == "__main__":
    unittest.main()

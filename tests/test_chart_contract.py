#!/usr/bin/env python3
"""Text-level contract tests for the chart feature.

These keep the schedule, image build and delivery wiring in step without
running a model, a database or Discord.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CRONTABS = {
    name: (ROOT / name).read_text(encoding="utf-8")
    for name in ("crontab", "crontab.live")
}
RUN_PROMPT = (ROOT / "scripts" / "run-prompt.sh").read_text(encoding="utf-8")
NOTIFY = (ROOT / "scripts" / "notify.sh").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def active_lines(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class ScheduleTests(unittest.TestCase):
    def test_the_cron_carries_no_chart_settings(self) -> None:
        for name, text in CRONTABS.items():
            with self.subTest(crontab=name):
                self.assertNotIn("NOTIFY_CHARTS", text)
                self.assertNotIn("NOTIFY_CHART_HOSTS", text)

    def test_the_crontabs_stay_in_step(self) -> None:
        self.assertEqual(CRONTABS["crontab"], CRONTABS["crontab.live"])

    def test_the_admin_chat_digest_keeps_its_window(self) -> None:
        for name, text in CRONTABS.items():
            with self.subTest(crontab=name):
                line = next(
                    line for line in active_lines(text)
                    if line.startswith("5 * * * * ") and "-t admin-chat" in line
                )
                self.assertNotIn("NOTIFY_PREFETCH_WINDOW_MINUTES", line)


class WiringTests(unittest.TestCase):
    def test_no_host_name_is_hardcoded_in_the_chart_path(self) -> None:
        for name in (
            "scripts/run-prompt.sh",
            "scripts/host_chart.py",
            "scripts/host_series.py",
            "scripts/discord_inbound.py",
            "scripts/bot_config.py",
        ):
            with self.subTest(script=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertNotIn("vortexservers", text)

    def test_run_prompt_exports_and_cleans_the_attach_directory(self) -> None:
        self.assertIn("NOTIFY_ATTACH_DIR=$chart_dir", RUN_PROMPT)
        self.assertIn("export NOTIFY_ATTACH_DIR", RUN_PROMPT)
        self.assertIn("cleanup_charts", RUN_PROMPT)
        self.assertIn("trap cleanup_charts EXIT", RUN_PROMPT)

    def test_run_prompt_takes_chart_hosts_from_the_panel(self) -> None:
        self.assertIn("NOTIFY_CHART_HOSTS", RUN_PROMPT)
        self.assertIn("chart_hosts_json", RUN_PROMPT)
        self.assertIn("chart_scope_json", RUN_PROMPT)
        self.assertIn("target_host_scope_json", RUN_PROMPT)

    def test_run_prompt_uses_the_shared_series_helper(self) -> None:
        self.assertIn('"$script_dir/host_series.py"', RUN_PROMPT)
        self.assertIn("--window", RUN_PROMPT)
        self.assertIn("--host", RUN_PROMPT)
        series = (ROOT / "scripts" / "host_series.py").read_text(encoding="utf-8")
        self.assertIn("AVG(cpu_percent) AS cpu", series)
        self.assertIn("AVG(memory_percent) AS memory", series)
        self.assertNotIn("load_avg", series)
        self.assertNotIn("disk_percent", series)

    def test_notify_uploads_multipart_with_a_json_fallback(self) -> None:
        self.assertIn("payload_json=", NOTIFY)
        self.assertIn("files[", NOTIFY)
        self.assertIn("sending without images", NOTIFY)
        self.assertIn("discord_post_json", NOTIFY)
        # curl owns the multipart boundary; a manual header would break it.
        self.assertNotIn("Content-Type: multipart/form-data", NOTIFY)

    def test_the_chart_helper_never_queries_load_or_disk(self) -> None:
        for name in ("host_chart.py", "host_series.py"):
            with self.subTest(script=name):
                text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertNotIn("load_avg", text)
                self.assertNotIn("disk_percent", text)


class DockerfileTests(unittest.TestCase):
    def test_pillow_is_pinned_and_installed_with_the_mcp_dependencies(self) -> None:
        self.assertRegex(DOCKERFILE, r"ARG PILLOW_VERSION=\d+\.\d+\.\d+")
        self.assertIn('"Pillow==${PILLOW_VERSION}"', DOCKERFILE)
        self.assertIn("--target /opt/mcp-python", DOCKERFILE)
        self.assertIn('"oracledb==${ORACLEDB_VERSION}"', DOCKERFILE)

    def test_pillow_has_no_platform_split(self) -> None:
        # One pip invocation serves both supported architectures; there is no
        # amd64/arm64 branch around Pillow.
        self.assertNotRegex(DOCKERFILE, r"(amd64|arm64).*Pillow")


if __name__ == "__main__":
    unittest.main()

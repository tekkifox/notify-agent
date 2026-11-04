#!/usr/bin/env python3
"""Delivery-boundary tests for the ``NOTIFY_REQUIRE_FLAG`` guard.

A scheduled digest must send nothing when nothing is flagged, and that cannot
rest on the prompt alone: the model can still call ``notify.sh``. When
``NOTIFY_REQUIRE_FLAG=1`` the script refuses a message with no ``[!]`` marker
before any request, prints one short line to stderr and exits 0, so the
scheduler sees a skip rather than a failure.

No network is used. A fake ``curl`` writes the payload it would have sent to a
capture file, so "nothing was delivered" is proved by the absence of that file,
not by inferring it from output.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "scripts" / "notify.sh"

REFUSAL = "notify: nothing was flagged, so nothing was sent"


@unittest.skipUnless(shutil.which("jq"), "jq is required to exercise notify.sh")
class NotifyFlagGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.capture = self.dir / "request.json"
        self.marker = self.dir / "sent.marker"

        fake_curl = self.bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            'cat > "$NOTIFY_TEST_CAPTURE"\n'
            "printf '200'\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_notify(
        self,
        message: str,
        *,
        require_flag: str | None = None,
        target: str | None = None,
        webhook: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "COMPOSIO_API_KEY": "test-key",
            "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
            "COMPOSIO_API_BASE": "https://api.example.test",
            "COMPOSIO_ARGS": '{"channel_id":"000000000000000000"}',
            "NOTIFY_TEST_CAPTURE": str(self.capture),
            "NOTIFY_SEND_MARKER": str(self.marker),
        }
        if require_flag is not None:
            env["NOTIFY_REQUIRE_FLAG"] = require_flag
        if webhook is not None:
            env["DISCORD_WEBHOOK_URL"] = webhook
        if target is not None:
            env["NOTIFY_TARGETS"] = json.dumps(
                {
                    "automations": {
                        "channel_id": "000000000000000000",
                        "host_scope": ["host1.example.com"],
                    }
                }
            )

        command = [str(NOTIFY)]
        if target is not None:
            command += ["-t", target]
        command += ["-m", message]
        return subprocess.run(
            command, env=env, text=True, capture_output=True, check=False
        )

    def sent_content(self) -> str:
        payload = json.loads(self.capture.read_text(encoding="utf-8"))
        return payload["arguments"]["content"]

    def test_a_message_without_a_flag_is_not_sent_and_exits_zero(self) -> None:
        result = self.run_notify(
            "Host report: all three hosts are healthy.", require_flag="1"
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(REFUSAL, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.marker.exists())

    def test_a_message_with_the_flag_marker_is_sent(self) -> None:
        message = "- [!] host1.example.com: CPU 61.0% avg >= 60%"
        result = self.run_notify(message, require_flag="1")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr, "")
        self.assertTrue(self.marker.exists())
        self.assertEqual(self.sent_content(), message)

    def test_the_guard_runs_before_any_request(self) -> None:
        # A webhook is a real delivery target; the fake curl proves it was never
        # called because the capture file was not written.
        result = self.run_notify(
            "Host report: nothing to flag.",
            require_flag="1",
            webhook="https://discord.example.test/webhook",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(REFUSAL, result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse(self.marker.exists())

    def test_unset_variable_behaves_exactly_as_today(self) -> None:
        unflagged = self.run_notify("Host report: all three hosts are healthy.")
        self.assertEqual(unflagged.returncode, 0)
        self.assertEqual(self.sent_content(), "Host report: all three hosts are healthy.")
        self.capture.unlink()
        self.marker.unlink()

        flagged = self.run_notify("- [!] host1.example.com: mem 88.0% avg >= 85%")
        self.assertEqual(flagged.returncode, 0)
        self.assertTrue(self.marker.exists())
        self.assertEqual(
            self.sent_content(), "- [!] host1.example.com: mem 88.0% avg >= 85%"
        )

    def test_a_value_other_than_one_keeps_the_old_behaviour(self) -> None:
        for value in ("0", "true", ""):
            with self.subTest(value=value):
                self.capture.unlink(missing_ok=True)
                self.marker.unlink(missing_ok=True)
                result = self.run_notify(
                    "Host report: all three hosts are healthy.",
                    require_flag=value,
                )
                self.assertEqual(result.returncode, 0)
                self.assertNotIn(REFUSAL, result.stderr)
                self.assertTrue(self.capture.exists())

    def test_a_marker_beyond_the_truncation_limit_does_not_count(self) -> None:
        # The guard inspects the message that would be sent, after truncation.
        message = ("a" * 2100) + " [!]"
        result = self.run_notify(message, require_flag="1")
        self.assertEqual(result.returncode, 0)
        self.assertIn(REFUSAL, result.stderr)
        self.assertFalse(self.capture.exists())


if __name__ == "__main__":
    unittest.main()

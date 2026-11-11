#!/usr/bin/env python3
"""Attachment tests for scripts/notify.sh.

No network is used. A fake curl records each invocation and its arguments, so
"plain JSON" and "multipart with these files" are proved from the request the
script would have made. A failed multipart request is simulated with an HTTP
status or a curl exit code.

The per-target webhook tests use the same fake: a chart reaches Discord only
through a webhook, and a named target takes its own NOTIFY_WEBHOOK_* variable.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOTIFY = ROOT / "scripts" / "notify.sh"
WEBHOOK = "https://discord.example.test/webhook"
TARGET_WEBHOOK = "https://discord.com/api/webhooks/000000000000000000/EXAMPLE-TOKEN"
FOREIGN_WEBHOOK = "https://example.test/webhook"
FILE_PART = re.compile(r"^files\[(\d+)\]=@(.+)$")


@unittest.skipUnless(shutil.which("jq"), "jq is required to exercise notify.sh")
class NotifyAttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "requests.log"

        fake_curl = self.bin / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            "{\n"
            "  printf 'CALL\\n'\n"
            "  for arg do printf 'ARG|%s\\n' \"$arg\"; done\n"
            "} >> \"$NOTIFY_TEST_LOG\"\n"
            "multipart=0\n"
            "for arg do\n"
            "  [ \"$arg\" = '-F' ] && multipart=1\n"
            "done\n"
            "if [ \"$multipart\" -eq 1 ]; then\n"
            "  if [ -n \"${NOTIFY_TEST_MULTIPART_EXIT:-}\" ]; then\n"
            "    exit \"$NOTIFY_TEST_MULTIPART_EXIT\"\n"
            "  fi\n"
            "  printf '%s' \"${NOTIFY_TEST_MULTIPART_STATUS:-200}\"\n"
            "  exit 0\n"
            "fi\n"
            "cat > \"${NOTIFY_TEST_LOG}.body\" 2>/dev/null || true\n"
            "printf '200'\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_notify(
        self,
        message: str = "hello",
        attach: pathlib.Path | None = None,
        *,
        composio: bool = False,
        target: str | None = None,
        extra_env: dict[str, str] | None = None,
        multipart_status: str | None = None,
        multipart_exit: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "NOTIFY_TEST_LOG": str(self.log),
        }
        if composio:
            env.update(
                {
                    "COMPOSIO_API_KEY": "test-key",
                    "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
                    "COMPOSIO_API_BASE": "https://api.example.test",
                    "COMPOSIO_ARGS": '{"channel_id":"000000000000000000"}',
                }
            )
        elif target is None:
            env["DISCORD_WEBHOOK_URL"] = WEBHOOK
        if target is not None:
            env["NOTIFY_TARGETS"] = json.dumps(
                {target: {"channel_id": "222222222222222222"}}
            )
        if attach is not None:
            env["NOTIFY_ATTACH_DIR"] = str(attach)
        if multipart_status is not None:
            env["NOTIFY_TEST_MULTIPART_STATUS"] = multipart_status
        if multipart_exit is not None:
            env["NOTIFY_TEST_MULTIPART_EXIT"] = multipart_exit
        if extra_env:
            env.update(extra_env)
        command = [str(NOTIFY)]
        if target is not None:
            command += ["-t", target]
        command += ["-m", message]
        return subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        calls: list[list[str]] = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            if line == "CALL":
                calls.append([])
            elif line.startswith("ARG|") and calls:
                calls[-1].append(line[4:])
        return calls

    def png(self, directory: pathlib.Path, name: str) -> pathlib.Path:
        path = directory / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("utf-8"))
        return path

    def payload_from(self, args: list[str]) -> dict:
        for arg in args:
            if arg.startswith("payload_json="):
                return json.loads(arg[len("payload_json="):])
        return {}

    def files_from(self, args: list[str]) -> list[tuple[int, str]]:
        found = []
        for arg in args:
            match = FILE_PART.match(arg)
            if match:
                found.append((int(match.group(1)), pathlib.Path(match.group(2)).name))
        return found

    def test_no_attachment_directory_posts_plain_json(self) -> None:
        result = self.run_notify()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("--data-binary", calls[0])
        self.assertNotIn("-F", calls[0])
        self.assertIn("Content-Type: application/json", calls[0])

    def test_an_empty_or_non_png_directory_posts_plain_json(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        (attach / "notes.txt").write_text("ignore me", encoding="utf-8")
        result = self.run_notify(attach=attach)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-F", calls[0])

    def test_pngs_are_posted_as_sorted_multipart_parts(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        self.png(attach, "b.example.com.png")
        self.png(attach, "a.example.com.png")
        result = self.run_notify("the digest", attach=attach)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertIn("-F", args)
        self.assertEqual(self.payload_from(args)["content"], "the digest")
        self.assertEqual(
            self.files_from(args),
            [(0, "a.example.com.png"), (1, "b.example.com.png")],
        )

    def test_at_most_ten_direct_pngs_are_attached(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        nested = attach / "nested"
        nested.mkdir()
        for index in range(12):
            self.png(attach, f"{index:02d}.png")
        self.png(nested, "hidden.png")
        (attach / "notes.txt").write_text("ignore me", encoding="utf-8")

        result = self.run_notify(attach=attach)
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.files_from(self.calls()[0])
        self.assertEqual(len(files), 10)
        self.assertEqual([index for index, _ in files], list(range(10)))
        self.assertEqual(
            [name for _, name in files],
            [f"{index:02d}.png" for index in range(10)],
        )

    def test_an_http_failure_falls_back_to_plain_json(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        self.png(attach, "a.example.com.png")
        result = self.run_notify("the digest", attach=attach, multipart_status="500")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sending without images", result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn("-F", calls[0])
        self.assertNotIn("-F", calls[1])
        self.assertIn("--data-binary", calls[1])
        body = json.loads(
            pathlib.Path(str(self.log) + ".body").read_text(encoding="utf-8")
        )
        self.assertEqual(body["content"], "the digest")

    def test_a_curl_failure_falls_back_to_plain_json(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        self.png(attach, "a.example.com.png")
        result = self.run_notify("the digest", attach=attach, multipart_exit="7")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("curl exit 7", result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertNotIn("-F", calls[1])

    def test_the_composio_path_ignores_attachments_and_still_sends(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        self.png(attach, "a.example.com.png")
        result = self.run_notify("the digest", attach=attach, composio=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("only supported on the Discord webhook path", result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-F", calls[0])
        body = json.loads(
            pathlib.Path(str(self.log) + ".body").read_text(encoding="utf-8")
        )
        self.assertEqual(body["arguments"]["content"], "the digest")

    # --- Per-target webhooks: the only route that carries a chart ----------

    def charts(self, names: tuple[str, ...] = ("a.example.com.png",)) -> pathlib.Path:
        attach = self.dir / "charts"
        attach.mkdir(exist_ok=True)
        for name in names:
            self.png(attach, name)
        return attach

    def test_a_target_webhook_posts_multipart_and_marks_sent(self) -> None:
        attach = self.charts(("b.example.com.png", "a.example.com.png"))
        marker = self.dir / "marker"
        result = self.run_notify(
            "the digest",
            attach=attach,
            target="admin-chat",
            extra_env={
                "NOTIFY_WEBHOOK_ADMIN_CHAT": TARGET_WEBHOOK,
                "NOTIFY_SEND_MARKER": str(marker),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists(), "a delivered send must write the marker")
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertIn("-F", args)
        self.assertEqual(args[-1], TARGET_WEBHOOK)
        self.assertEqual(
            self.files_from(args),
            [(0, "a.example.com.png"), (1, "b.example.com.png")],
        )
        payload = self.payload_from(args)
        self.assertEqual(payload["content"], "the digest")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn(TARGET_WEBHOOK, result.stdout)
        self.assertNotIn(TARGET_WEBHOOK, result.stderr)
        self.assertIn("with 2 image(s)", result.stdout)

    def test_a_target_without_its_webhook_keeps_the_composio_text_path(self) -> None:
        attach = self.charts()
        result = self.run_notify(
            "the digest", attach=attach, composio=True, target="admin-chat"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("only supported on the Discord webhook path", result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-F", calls[0])
        self.assertIn(
            "https://api.example.test/api/v3.1/tools/execute/DISCORDBOT_CREATE_MESSAGE",
            calls[0],
        )
        body = json.loads(
            pathlib.Path(str(self.log) + ".body").read_text(encoding="utf-8")
        )
        self.assertEqual(body["arguments"]["content"], "the digest")
        self.assertEqual(body["arguments"]["channel_id"], "222222222222222222")

    def test_a_non_discord_webhook_is_refused_and_falls_back_to_text(self) -> None:
        for url in (
            FOREIGN_WEBHOOK,
            "http://discord.com/api/webhooks/000000000000000000/EXAMPLE-TOKEN",
            "https://discord.com.example.test/api/webhooks/000000000000000000/x",
            "https://discord.com/api/webhooks/",
        ):
            with self.subTest(url=url):
                self.log.unlink(missing_ok=True)
                pathlib.Path(str(self.log) + ".body").unlink(missing_ok=True)
                attach = self.charts()
                result = self.run_notify(
                    "the digest",
                    attach=attach,
                    composio=True,
                    target="admin-chat",
                    extra_env={"NOTIFY_WEBHOOK_ADMIN_CHAT": url},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('target "admin-chat"', result.stderr)
                self.assertIn("not a Discord webhook URL", result.stderr)
                self.assertNotIn(url, result.stderr)
                self.assertNotIn(url, result.stdout)
                calls = self.calls()
                self.assertEqual(len(calls), 1)
                self.assertNotIn("-F", calls[0])
                body = json.loads(
                    pathlib.Path(str(self.log) + ".body").read_text(encoding="utf-8")
                )
                self.assertEqual(body["arguments"]["content"], "the digest")

    def test_a_target_webhook_attaches_at_most_ten_images(self) -> None:
        attach = self.charts(tuple(f"{index:02d}.png" for index in range(12)))
        result = self.run_notify(
            "the digest",
            attach=attach,
            target="admin-chat",
            extra_env={"NOTIFY_WEBHOOK_ADMIN_CHAT": TARGET_WEBHOOK},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.files_from(self.calls()[0])
        self.assertEqual(len(files), 10)
        self.assertEqual([index for index, _ in files], list(range(10)))
        self.assertEqual(
            [name for _, name in files],
            [f"{index:02d}.png" for index in range(10)],
        )

    def test_a_failed_target_webhook_post_falls_back_to_text(self) -> None:
        for kwargs, expected in (
            ({"multipart_status": "500"}, "HTTP 500"),
            ({"multipart_exit": "7"}, "curl exit 7"),
        ):
            with self.subTest(**kwargs):
                self.log.unlink(missing_ok=True)
                pathlib.Path(str(self.log) + ".body").unlink(missing_ok=True)
                attach = self.charts()
                result = self.run_notify(
                    "the digest",
                    attach=attach,
                    target="admin-chat",
                    extra_env={"NOTIFY_WEBHOOK_ADMIN_CHAT": TARGET_WEBHOOK},
                    **kwargs,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("sending without images", result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertNotIn(TARGET_WEBHOOK, result.stderr)
                calls = self.calls()
                self.assertEqual(len(calls), 2)
                self.assertIn("-F", calls[0])
                self.assertEqual(calls[0][-1], TARGET_WEBHOOK)
                self.assertNotIn("-F", calls[1])
                self.assertEqual(calls[1][-1], TARGET_WEBHOOK)
                body = json.loads(
                    pathlib.Path(str(self.log) + ".body").read_text(encoding="utf-8")
                )
                self.assertEqual(body["content"], "the digest")

    def test_the_webhook_variable_name_is_derived_from_the_target(self) -> None:
        cases = (
            ("admin-chat", "NOTIFY_WEBHOOK_ADMIN_CHAT"),
            ("automations", "NOTIFY_WEBHOOK_AUTOMATIONS"),
            ("ops.eu", "NOTIFY_WEBHOOK_OPS_EU"),
            ("Ops EU.v2", "NOTIFY_WEBHOOK_OPS_EU_V2"),
        )
        for target, variable in cases:
            with self.subTest(target=target):
                self.log.unlink(missing_ok=True)
                pathlib.Path(str(self.log) + ".body").unlink(missing_ok=True)
                attach = self.charts()
                result = self.run_notify(
                    "the digest",
                    attach=attach,
                    target=target,
                    extra_env={variable: TARGET_WEBHOOK},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls()
                self.assertEqual(len(calls), 1)
                self.assertIn("-F", calls[0])
                self.assertEqual(calls[0][-1], TARGET_WEBHOOK)

    def test_a_discordapp_webhook_is_accepted(self) -> None:
        attach = self.charts()
        url = "https://discordapp.com/api/webhooks/000000000000000000/EXAMPLE-TOKEN"
        result = self.run_notify(
            "the digest",
            attach=attach,
            target="admin-chat",
            extra_env={"NOTIFY_WEBHOOK_ADMIN_CHAT": url},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertIn("-F", calls[0])
        self.assertEqual(calls[0][-1], url)

    def test_a_global_webhook_is_still_refused_for_a_named_target(self) -> None:
        attach = self.charts()
        result = self.run_notify(
            "the digest",
            attach=attach,
            target="admin-chat",
            extra_env={
                "DISCORD_WEBHOOK_URL": WEBHOOK,
                "NOTIFY_WEBHOOK_ADMIN_CHAT": TARGET_WEBHOOK,
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("a webhook has no targets", result.stderr)
        self.assertNotIn(TARGET_WEBHOOK, result.stderr)
        self.assertNotIn(WEBHOOK, result.stderr)

    def test_only_the_exact_derived_variable_is_read(self) -> None:
        attach = self.charts()
        result = self.run_notify(
            "the digest",
            attach=attach,
            composio=True,
            target="admin-chat",
            extra_env={"NOTIFY_WEBHOOK_ADMINCHAT": TARGET_WEBHOOK},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-F", calls[0])

    def test_a_set_target_webhook_does_not_change_a_message_without_images(self) -> None:
        attach = self.dir / "charts"
        attach.mkdir()
        (attach / "notes.txt").write_text("ignore me", encoding="utf-8")
        result = self.run_notify(
            "the digest",
            attach=attach,
            composio=True,
            target="admin-chat",
            extra_env={"NOTIFY_WEBHOOK_ADMIN_CHAT": TARGET_WEBHOOK},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("-F", calls[0])
        self.assertNotIn(TARGET_WEBHOOK, calls[0])
        body = json.loads(
            pathlib.Path(str(self.log) + ".body").read_text(encoding="utf-8")
        )
        self.assertEqual(body["arguments"]["content"], "the digest")


if __name__ == "__main__":
    unittest.main()

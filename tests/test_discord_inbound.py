#!/usr/bin/env python3
"""Tests for the inbound Discord listener.

No network, no Discord channel, no Composio account and no model are used.
The agent process is stubbed at the ``subprocess.Popen`` boundary.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "discord_inbound.py"

spec = importlib.util.spec_from_file_location("discord_inbound", SCRIPT)
discord_inbound = importlib.util.module_from_spec(spec)
sys.modules["discord_inbound"] = discord_inbound
spec.loader.exec_module(discord_inbound)

CHANNEL = "111111111111111111"
ALLOWED = "222222222222222222"
OTHER_ALLOWED = "333333333333333333"
DISALLOWED = "444444444444444444"
ACCOUNT = "ca_test_account"


def base_env(tmpdir: str) -> dict[str, str]:
    return {
        "COMPOSIO_API_KEY": "test-key-not-a-real-secret",
        "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
        "COMPOSIO_CONNECTED_ACCOUNT_ID": ACCOUNT,
        "COMPOSIO_USER_ID": "default",
        "NOTIFY_INBOUND_CHANNEL_ID": CHANNEL,
        "NOTIFY_INBOUND_ALLOWED_AUTHORS": f"{ALLOWED},{OTHER_ALLOWED}",
        "NOTIFY_INBOUND_TARGET": "admin-chat",
        "NOTIFY_TARGETS": json.dumps(
            {
                "admin-chat": {
                    "channel_id": CHANNEL,
                    "connected_account_id": ACCOUNT,
                    "host_scope": "vortexservers.co.uk",
                    "allow_content": False,
                }
            }
        ),
        "OPENCODE_MODEL": "test/model",
        "OPENCODE_BIN": "/bin/true",
        "NOTIFY_INBOUND_STATE_FILE": str(pathlib.Path(tmpdir) / "state.json"),
        "NOTIFY_INBOUND_POLL_SECONDS": "5",
        "NOTIFY_INBOUND_MAX_INSTRUCTION_CHARS": "1000",
        "NOTIFY_INBOUND_QUEUE_SIZE": "4",
        "NOTIFY_INBOUND_RUN_TIMEOUT_SECONDS": "30",
    }


def raw_message(**overrides: object) -> dict[str, object]:
    message: dict[str, object] = {
        "id": "900000000000000001",
        "channel_id": CHANNEL,
        "author": {"id": ALLOWED, "bot": False},
        "content": "what is the disk usage?",
        "type": 19,
    }
    message.update(overrides)
    return message


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = base_env(self.temp.name)

    def load(self, **changes: str | None) -> discord_inbound.Config:
        env = dict(self.env)
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return discord_inbound.load_config(env)

    def test_valid_config(self) -> None:
        config = self.load()
        self.assertEqual(config.channel_id, CHANNEL)
        self.assertEqual(config.allowed_authors, {ALLOWED, OTHER_ALLOWED})
        self.assertEqual(config.delivery, "composio-target")
        self.assertEqual(config.target.name, "admin-chat")
        self.assertEqual(config.target.connected_account_id, ACCOUNT)
        self.assertFalse(config.target.allow_content)
        self.assertIn("vortexservers.co.uk", config.target.host_scope)

    def test_empty_allowlist_refuses_to_start(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_ALLOWED_AUTHORS="")

    def test_missing_allowlist_refuses_to_start(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_ALLOWED_AUTHORS=None)

    def test_allowlist_empty_entry_refuses(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_ALLOWED_AUTHORS=f"{ALLOWED},,{OTHER_ALLOWED}")

    def test_allowlist_non_numeric_refuses(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_ALLOWED_AUTHORS="not-an-id")

    def test_allowlist_dedupes(self) -> None:
        config = self.load(
            NOTIFY_INBOUND_ALLOWED_AUTHORS=f"{ALLOWED}, {ALLOWED},{OTHER_ALLOWED}"
        )
        self.assertEqual(config.allowed_authors, {ALLOWED, OTHER_ALLOWED})

    def test_channel_is_required(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_CHANNEL_ID=None)

    def test_channel_must_be_numeric(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_CHANNEL_ID="admin-chat")

    def test_target_must_exist(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_TARGET="missing")

    def test_target_channel_must_match_inbound_channel(self) -> None:
        targets = json.loads(self.env["NOTIFY_TARGETS"])
        targets["admin-chat"]["channel_id"] = "555555555555555555"
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_TARGETS=json.dumps(targets))

    def test_target_host_scope_must_be_valid(self) -> None:
        targets = json.loads(self.env["NOTIFY_TARGETS"])
        targets["admin-chat"]["host_scope"] = ["ok", ""]
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_TARGETS=json.dumps(targets))

    def test_target_allow_content_must_be_boolean(self) -> None:
        targets = json.loads(self.env["NOTIFY_TARGETS"])
        targets["admin-chat"]["allow_content"] = "false"
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_TARGETS=json.dumps(targets))

    def test_webhook_mode_rejects_named_target(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(DISCORD_WEBHOOK_URL="https://example.invalid/hook")

    def test_webhook_mode_rejects_default_target(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(
                DISCORD_WEBHOOK_URL="https://example.invalid/hook",
                NOTIFY_INBOUND_TARGET=None,
                NOTIFY_DEFAULT_TARGET="admin-chat",
            )

    def test_webhook_mode_is_valid_without_a_target(self) -> None:
        config = self.load(
            DISCORD_WEBHOOK_URL="https://example.invalid/hook",
            NOTIFY_INBOUND_TARGET=None,
            NOTIFY_TARGETS=None,
            COMPOSIO_TOOL=None,
        )
        self.assertTrue(config.webhook_mode)
        self.assertEqual(config.delivery, "discord-webhook")
        self.assertIsNone(config.target)
        self.assertNotIn("/app/scripts/notify.sh -t", discord_inbound.build_instruction(config, "hi"))

    def test_no_delivery_refuses(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(COMPOSIO_TOOL=None)

    def test_model_is_required(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(OPENCODE_MODEL=None)

    def test_state_file_must_be_absolute(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_STATE_FILE="state.json")

    def test_state_parent_must_exist(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(
                NOTIFY_INBOUND_STATE_FILE=str(
                    pathlib.Path(self.temp.name) / "missing" / "state.json"
                )
            )

    def test_poll_interval_has_a_floor(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_POLL_SECONDS="1")

    def test_read_account_must_match_target(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_CONNECTED_ACCOUNT_ID="ca_other")

    def test_legacy_composio_delivery_is_allowed(self) -> None:
        config = self.load(
            NOTIFY_INBOUND_TARGET=None,
            NOTIFY_TARGETS=None,
        )
        self.assertIsNone(config.target)
        self.assertIn("/app/scripts/notify.sh -m", discord_inbound.build_instruction(config, "hi"))
        self.assertNotIn("/app/scripts/notify.sh -t", discord_inbound.build_instruction(config, "hi"))


class MainTests(unittest.TestCase):
    def test_empty_allowlist_makes_the_listener_exit_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = base_env(tmp)
            env["NOTIFY_INBOUND_ALLOWED_AUTHORS"] = ""
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(discord_inbound.main([]), 2)


class MentionTests(unittest.TestCase):
    def test_strips_user_role_and_channel_mentions(self) -> None:
        self.assertEqual(
            discord_inbound.strip_mentions("<@123> <@!456> <@&789> <#101> status?"),
            "status?",
        )

    def test_strips_everyone_and_here(self) -> None:
        self.assertEqual(
            discord_inbound.strip_mentions("@everyone @here @Here status?"),
            "status?",
        )

    def test_does_not_strip_email_like_text(self) -> None:
        self.assertEqual(
            discord_inbound.strip_mentions("mail me at a@here.example"),
            "mail me at a@here.example",
        )

    def test_removes_control_characters(self) -> None:
        self.assertEqual(
            discord_inbound.strip_mentions("a\x00b\x07c\td"),
            "abc d",
        )

    def test_only_mentions_becomes_empty(self) -> None:
        self.assertEqual(discord_inbound.strip_mentions("<@123> @here"), "")


class EvaluateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(base_env(self.temp.name))

    def evaluate(self, message: object, seen: set[str] | None = None):
        seen = seen or set()
        return discord_inbound.evaluate_message(
            self.config, message, lambda message_id: message_id in seen
        )

    def test_accepts_allowed_reply(self) -> None:
        decision = self.evaluate(raw_message())
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "accepted")
        self.assertEqual(decision.text, "what is the disk usage?")

    def test_accepts_plain_message_type(self) -> None:
        self.assertTrue(self.evaluate(raw_message(type=0)).accepted)

    def test_disallowed_author_is_refused(self) -> None:
        decision = self.evaluate(raw_message(author={"id": DISALLOWED, "bot": False}))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "author_not_allowed")

    def test_bot_author_is_refused(self) -> None:
        decision = self.evaluate(raw_message(author={"id": ALLOWED, "bot": True}))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "bot_author")

    def test_webhook_author_is_refused(self) -> None:
        decision = self.evaluate(raw_message(webhook_id="777"))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "bot_author")

    def test_wrong_channel_is_refused(self) -> None:
        decision = self.evaluate(raw_message(channel_id="555555555555555555"))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "wrong_channel")

    def test_duplicate_is_refused(self) -> None:
        decision = self.evaluate(raw_message(), seen={"900000000000000001"})
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "duplicate")

    def test_system_message_type_is_refused(self) -> None:
        decision = self.evaluate(raw_message(type=7))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unsupported_type")

    def test_too_long_is_refused(self) -> None:
        decision = self.evaluate(raw_message(content="x" * 1001))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "too_long")

    def test_empty_after_mentions_is_refused(self) -> None:
        decision = self.evaluate(raw_message(content="<@123> @here"))
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "empty")

    def test_malformed_messages_are_refused(self) -> None:
        for message in (
            None,
            {},
            {"id": "not-an-id"},
            {"id": "900000000000000002", "author": "missing"},
            {
                "id": "900000000000000003",
                "author": {"id": "bad", "bot": False},
                "channel_id": CHANNEL,
            },
        ):
            with self.subTest(message=message):
                self.assertFalse(self.evaluate(message).accepted)


class SeenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = pathlib.Path(self.temp.name) / "state.json"

    def test_new_store_is_not_initialized(self) -> None:
        store = discord_inbound.SeenStore(self.path)
        self.assertFalse(store.initialized)
        self.assertEqual(store.cursor, "")

    def test_dedupe_survives_a_restart(self) -> None:
        first = discord_inbound.SeenStore(self.path)
        first.record("900000000000000010")
        self.assertTrue(first.is_seen("900000000000000010"))

        restarted = discord_inbound.SeenStore(self.path)
        self.assertTrue(restarted.is_seen("900000000000000010"))
        self.assertEqual(restarted.cursor, "900000000000000010")

    def test_baseline_survives_a_restart(self) -> None:
        first = discord_inbound.SeenStore(self.path)
        first.initialize(["900000000000000020"], "900000000000000020")
        self.assertTrue(first.initialized)

        restarted = discord_inbound.SeenStore(self.path)
        self.assertTrue(restarted.initialized)
        self.assertTrue(restarted.is_seen("900000000000000020"))
        self.assertEqual(restarted.cursor, "900000000000000020")

    def test_state_file_is_mode_0600(self) -> None:
        store = discord_inbound.SeenStore(self.path)
        store.record("900000000000000030")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_state_refuses_to_start(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(discord_inbound.StateError):
            discord_inbound.SeenStore(self.path)

    def test_seen_list_is_bounded(self) -> None:
        store = discord_inbound.SeenStore(self.path, limit=3)
        for number in range(5):
            store.record(f"90000000000000000{number}")
        self.assertFalse(store.is_seen("900000000000000000"))
        self.assertTrue(store.is_seen("900000000000000004"))

    def test_invalid_message_id_is_refused(self) -> None:
        store = discord_inbound.SeenStore(self.path)
        with self.assertRaises(discord_inbound.StateError):
            store.record("not-a-snowflake")


class FetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(base_env(self.temp.name))
        self.store = discord_inbound.SeenStore(
            pathlib.Path(self.temp.name) / "state.json"
        )

    def test_first_poll_establishes_baseline_without_actioning(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "fetch_messages",
            return_value=[{"id": "900000000000000100"}],
        ) as fetch:
            result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(result, [])
        self.assertTrue(self.store.initialized)
        self.assertEqual(self.store.cursor, "900000000000000100")
        self.assertTrue(self.store.is_seen("900000000000000100"))
        fetch.assert_called_once_with(self.config, limit=1)

    def test_empty_channel_baseline_uses_a_time_based_cursor(self) -> None:
        with mock.patch.object(discord_inbound, "fetch_messages", return_value=[]):
            with mock.patch.object(
                discord_inbound, "fetch_channel_last_message_id", return_value=""
            ):
                result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(result, [])
        self.assertTrue(self.store.initialized)
        self.assertRegex(self.store.cursor, r"^[0-9]+$")
        self.assertNotEqual(self.store.cursor, "0")

    def test_missing_permission_baseline_uses_last_message_id(self) -> None:
        # Discord returns [] when the bot lacks Read Message History.  The
        # channel's last_message_id is still visible, so it is the baseline and
        # old history does not become new if the permission is granted later.
        with self.assertLogs("discord-inbound", level="WARNING") as logs:
            with mock.patch.object(
                discord_inbound, "fetch_messages", return_value=[]
            ):
                with mock.patch.object(
                    discord_inbound,
                    "fetch_channel_last_message_id",
                    return_value="900000000000000100",
                ):
                    result = discord_inbound.fetch_new_messages(
                        self.config, self.store
                    )
        self.assertEqual(result, [])
        self.assertEqual(self.store.cursor, "900000000000000100")
        self.assertIn("Grant Read Message History", "\n".join(logs.output))

        with mock.patch.object(
            discord_inbound,
            "fetch_messages",
            return_value=[
                {"id": "900000000000000100"},
                {"id": "900000000000000099"},
            ],
        ):
            result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(result, [])

    def test_new_messages_come_back_oldest_first(self) -> None:
        self.store.initialize(["900000000000000100"], "900000000000000100")
        with mock.patch.object(
            discord_inbound,
            "fetch_messages",
            return_value=[
                {"id": "900000000000000103"},
                {"id": "900000000000000102"},
                {"id": "900000000000000101"},
            ],
        ):
            result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(
            [item["id"] for item in result],
            [
                "900000000000000101",
                "900000000000000102",
                "900000000000000103",
            ],
        )

    def test_paginates_backwards_to_the_cursor(self) -> None:
        self.store.initialize(["900000000000000100"], "900000000000000100")
        pages = [
            [{"id": "900000000000000200"}, {"id": "900000000000000199"}],
            [{"id": "900000000000000198"}, {"id": "900000000000000101"}],
            [{"id": "900000000000000100"}, {"id": "900000000000000099"}],
        ]
        with mock.patch.object(discord_inbound, "MESSAGE_PAGE_LIMIT", 2):
            with mock.patch.object(
                discord_inbound, "fetch_messages", side_effect=pages
            ) as fetch:
                result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(
            [item["id"] for item in result],
            [
                "900000000000000101",
                "900000000000000198",
                "900000000000000199",
                "900000000000000200",
            ],
        )
        self.assertEqual(fetch.call_count, 3)

    def test_no_new_messages_returns_empty(self) -> None:
        self.store.initialize(["900000000000000100"], "900000000000000100")
        with mock.patch.object(
            discord_inbound,
            "fetch_messages",
            return_value=[
                {"id": "900000000000000100"},
                {"id": "900000000000000099"},
            ],
        ):
            result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(result, [])


class FakeResponse:
    def __init__(self, payload: object, *, raw: bytes | None = None) -> None:
        self.raw = raw if raw is not None else json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def read(self, size: int) -> bytes:
        return self.raw[:size]


class ProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(base_env(self.temp.name))

    def test_proxy_request_shape(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(
                {"status": 200, "data": [{"id": "900000000000000001"}]}
            ),
        ) as opener:
            messages = discord_inbound.fetch_messages(self.config, limit=1)
        self.assertEqual(messages, [{"id": "900000000000000001"}])
        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://backend.composio.dev/api/v3.1/tools/execute/proxy",
        )
        self.assertEqual(request.get_header("X-api-key"), self.config.api_key)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["endpoint"], f"/channels/{CHANNEL}/messages")
        self.assertEqual(body["method"], "GET")
        self.assertEqual(body["connected_account_id"], ACCOUNT)
        self.assertEqual(body["user_id"], "default")
        self.assertEqual(
            body["parameters"], [{"name": "limit", "value": "1", "type": "query"}]
        )

    def test_redirects_are_refused(self) -> None:
        handler = discord_inbound._NoRedirect()
        self.assertIsNone(
            handler.redirect_request(
                mock.Mock(),
                None,
                302,
                "Found",
                {},
                "https://example.invalid/elsewhere",
            )
        )

    def test_proxy_http_error_is_redacted(self) -> None:
        error = urllib.error.HTTPError(
            "https://backend.composio.dev/x",
            403,
            "forbidden",
            {},
            None,
        )
        with mock.patch.object(
            discord_inbound, "_open_request", side_effect=error
        ):
            with self.assertRaises(discord_inbound.FetchError) as caught:
                discord_inbound.fetch_messages(self.config, limit=1)
        self.assertEqual(str(caught.exception), "proxy returned HTTP 403")

    def test_proxy_non_200_body_status_is_an_error(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse({"status": 429, "data": []}),
        ):
            with self.assertRaises(discord_inbound.FetchError) as caught:
                discord_inbound.fetch_messages(self.config, limit=1)
        self.assertEqual(str(caught.exception), "proxy returned HTTP 429")

    def test_proxy_invalid_json_is_an_error(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(None, raw=b"not json"),
        ):
            with self.assertRaises(discord_inbound.FetchError):
                discord_inbound.fetch_messages(self.config, limit=1)

    def test_proxy_rejects_a_message_without_an_id(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse({"status": 200, "data": [{}]}),
        ):
            with self.assertRaises(discord_inbound.FetchError):
                discord_inbound.fetch_messages(self.config, limit=1)

    def test_channel_last_message_id(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(
                {"status": 200, "data": {"last_message_id": "900000000000000001"}}
            ),
        ) as opener:
            last = discord_inbound.fetch_channel_last_message_id(self.config)
        self.assertEqual(last, "900000000000000001")
        body = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(body["endpoint"], f"/channels/{CHANNEL}")
        self.assertEqual(body["parameters"], [])

    def test_channel_with_no_last_message_id(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(
                {"status": 200, "data": {"last_message_id": None}}
            ),
        ):
            self.assertEqual(
                discord_inbound.fetch_channel_last_message_id(self.config), ""
            )


class FakeProcess:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.pid = 12345

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class RunAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = base_env(self.temp.name)
        self.config = discord_inbound.load_config(self.env)

    def test_command_shape_and_content_disabled(self) -> None:
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", return_value=FakeProcess(0)
        ) as popen:
            result = discord_inbound.run_agent(self.config, "instruction text")
        self.assertTrue(result)
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                "/bin/true",
                "run",
                "--standalone",
                "--agent",
                "notify",
                "--model",
                "test/model",
                "--",
                "instruction text",
            ],
        )
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(environment["NOTIFY_CONTENT_DISABLED"], "1")
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_content_flag_is_not_exported_for_an_allowing_target(self) -> None:
        env = dict(self.env)
        targets = json.loads(env["NOTIFY_TARGETS"])
        targets["admin-chat"]["allow_content"] = True
        env["NOTIFY_TARGETS"] = json.dumps(targets)
        config = discord_inbound.load_config(env)
        with mock.patch.dict(os.environ, {"NOTIFY_CONTENT_DISABLED": "1"}):
            with mock.patch.object(
                discord_inbound.subprocess, "Popen", return_value=FakeProcess(0)
            ) as popen:
                discord_inbound.run_agent(config, "instruction")
        self.assertNotIn("NOTIFY_CONTENT_DISABLED", popen.call_args.kwargs["env"])

    def test_nonzero_exit_is_reported(self) -> None:
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", return_value=FakeProcess(2)
        ):
            self.assertFalse(discord_inbound.run_agent(self.config, "instruction"))


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(base_env(self.temp.name))
        self.store = discord_inbound.SeenStore(
            pathlib.Path(self.temp.name) / "state.json"
        )

    def test_disallowed_author_never_enqueues_and_logs_no_body(self) -> None:
        enqueued: list[str] = []
        with self.assertLogs("discord-inbound", level="WARNING") as logs:
            decisions = discord_inbound.ingest_messages(
                self.config,
                [
                    raw_message(
                        author={"id": DISALLOWED, "bot": False},
                        content="SECRET BODY THAT MUST NOT BE LOGGED",
                    )
                ],
                self.store,
                lambda text: enqueued.append(text) or True,
            )
        self.assertEqual([decision.reason for decision in decisions], ["author_not_allowed"])
        self.assertEqual(enqueued, [])
        output = "\n".join(logs.output)
        self.assertIn("reason=author_not_allowed", output)
        self.assertNotIn("SECRET BODY THAT MUST NOT BE LOGGED", output)
        self.assertNotIn(DISALLOWED, output)

    def test_disallowed_author_never_reaches_the_process_boundary(self) -> None:
        with mock.patch.object(discord_inbound.subprocess, "Popen") as popen:
            discord_inbound.ingest_messages(
                self.config,
                [raw_message(author={"id": DISALLOWED, "bot": False})],
                self.store,
                lambda _text: True,
            )
        popen.assert_not_called()

    def test_accepted_message_is_recorded_and_enqueued_once(self) -> None:
        enqueued: list[str] = []
        decisions = discord_inbound.ingest_messages(
            self.config,
            [raw_message()],
            self.store,
            lambda text: enqueued.append(text) or True,
        )
        self.assertTrue(decisions[0].accepted)
        self.assertEqual(len(enqueued), 1)
        self.assertIn("what is the disk usage?", enqueued[0])
        self.assertIn("/app/scripts/notify.sh -t admin-chat", enqueued[0])
        self.assertIn("vortexservers.co.uk", enqueued[0])
        self.assertIn("Content search is disabled", enqueued[0])
        self.assertTrue(self.store.is_seen("900000000000000001"))

        second = discord_inbound.ingest_messages(
            self.config,
            [raw_message()],
            self.store,
            lambda text: enqueued.append(text) or True,
        )
        self.assertEqual([decision.reason for decision in second], ["duplicate"])
        self.assertEqual(len(enqueued), 1)

    def test_queue_full_drops_instead_of_racing(self) -> None:
        with self.assertLogs("discord-inbound", level="WARNING") as logs:
            decisions = discord_inbound.ingest_messages(
                self.config,
                [raw_message()],
                self.store,
                lambda _text: False,
            )
        self.assertTrue(decisions[0].accepted)
        self.assertIn("reason=queue_full", "\n".join(logs.output))
        self.assertTrue(self.store.is_seen("900000000000000001"))


class WorkerTests(unittest.TestCase):
    def test_queue_full_drops(self) -> None:
        stop = threading.Event()
        worker = discord_inbound.InstructionWorker(lambda _text: None, 1, stop)
        self.assertTrue(worker.offer("one"))
        self.assertFalse(worker.offer("two"))
        worker.stop()

    def test_worker_runs_one_agent_at_a_time(self) -> None:
        stop = threading.Event()
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0, "done": 0}
        all_done = threading.Event()

        def run(_instruction: str) -> None:
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
                state["done"] += 1
                if state["done"] == 3:
                    all_done.set()

        worker = discord_inbound.InstructionWorker(run, 4, stop)
        worker.start()
        for instruction in ("one", "two", "three"):
            self.assertTrue(worker.offer(instruction))
        self.assertTrue(all_done.wait(2), "worker did not finish in time")
        worker.stop()
        self.assertEqual(state["done"], 3)
        self.assertEqual(state["max_active"], 1)


if __name__ == "__main__":
    unittest.main()

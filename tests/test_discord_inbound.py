#!/usr/bin/env python3
"""Tests for the inbound Discord listener.

No network, no Discord channel, no Composio account and no model are used.
The agent process is stubbed at the ``subprocess.Popen`` boundary.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import shutil
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
# /bin/true exists on the Linux image but not on every development host.
TRUE_BIN = shutil.which("true") or "/bin/true"


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
                    "host_scope": "host3.example.com",
                    "allow_content": False,
                }
            }
        ),
        "OPENCODE_MODEL": "test/model",
        "OPENCODE_BIN": TRUE_BIN,
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
        self.assertIn("host3.example.com", config.target.host_scope)

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


CHANNEL2 = "1314326014318481459"
VORTEX_ACCOUNT = "ca_vortex_test"
AUTOMATIONS_ACCOUNT = "ca_automations_test"


def sources_env(
    tmpdir: str,
    *,
    sources: list[dict[str, object]] | None = None,
    global_authors: str | None = None,
    targets: dict[str, object] | None = None,
) -> dict[str, str]:
    env = base_env(tmpdir)
    env.pop("NOTIFY_INBOUND_CHANNEL_ID", None)
    env.pop("NOTIFY_INBOUND_TARGET", None)
    env.pop("NOTIFY_TARGETS", None)
    if global_authors is not None:
        env["NOTIFY_INBOUND_ALLOWED_AUTHORS"] = global_authors
    if targets is not None:
        env["NOTIFY_TARGETS"] = json.dumps(targets)
    if sources is not None:
        env["NOTIFY_INBOUND_SOURCES"] = json.dumps(sources)
    return env


def default_sources() -> list[dict[str, object]]:
    return [
        {
            "name": "vortex",
            "connected_account_id": VORTEX_ACCOUNT,
            "channel_id": CHANNEL,
            "allowed_authors": ALLOWED,
            "target": "admin-chat",
        },
        {
            "name": "automations",
            "connected_account_id": AUTOMATIONS_ACCOUNT,
            "channel_id": CHANNEL2,
        },
    ]


def default_targets() -> dict[str, object]:
    return {
        "admin-chat": {
            "channel_id": CHANNEL,
            "connected_account_id": VORTEX_ACCOUNT,
        }
    }


class SourceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = sources_env(
            self.temp.name,
            sources=default_sources(),
            targets=default_targets(),
        )

    def load(self, **changes: str | None) -> discord_inbound.Config:
        env = dict(self.env)
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return discord_inbound.load_config(env)

    def test_two_sources_have_own_accounts_channels_and_reply_modes(self) -> None:
        config = self.load()
        self.assertEqual([source.name for source in config.sources], [
            "vortex",
            "automations",
        ])
        vortex, automations = config.sources
        self.assertEqual(vortex.connected_account_id, VORTEX_ACCOUNT)
        self.assertEqual(vortex.channel_id, CHANNEL)
        self.assertEqual(vortex.reply_mode, "target")
        self.assertEqual(vortex.target.name, "admin-chat")
        self.assertEqual(automations.connected_account_id, AUTOMATIONS_ACCOUNT)
        self.assertEqual(automations.channel_id, CHANNEL2)
        self.assertEqual(automations.reply_mode, "bot")
        self.assertIsNone(automations.target)
        self.assertEqual(config.delivery, "multi-source")

    def test_state_files_are_per_source(self) -> None:
        config = self.load()
        vortex, automations = config.sources
        self.assertNotEqual(vortex.state_file, automations.state_file)
        self.assertIn(".vortex.", vortex.state_file.name)
        self.assertIn(".automations.", automations.state_file.name)

    def test_per_source_allowlist_overrides_the_global_one(self) -> None:
        config = self.load()
        vortex, automations = config.sources
        vortex_message = raw_message(
            channel_id=CHANNEL, author={"id": OTHER_ALLOWED, "bot": False}
        )
        automations_message = raw_message(
            channel_id=CHANNEL2, author={"id": OTHER_ALLOWED, "bot": False}
        )
        self.assertEqual(
            discord_inbound.evaluate_message(
                vortex, vortex_message, lambda _message_id: False
            ).reason,
            "author_not_allowed",
        )
        self.assertTrue(
            discord_inbound.evaluate_message(
                automations, automations_message, lambda _message_id: False
            ).accepted
        )

    def test_source_without_authors_and_empty_global_refuses(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError) as caught:
            self.load(NOTIFY_INBOUND_ALLOWED_AUTHORS="")
        self.assertIn("automations", str(caught.exception))

    def test_source_with_own_authors_works_without_a_global_list(self) -> None:
        sources = [
            {
                "name": "vortex",
                "connected_account_id": VORTEX_ACCOUNT,
                "channel_id": CHANNEL,
                "allowed_authors": ALLOWED,
                "target": "admin-chat",
            }
        ]
        env = sources_env(
            self.temp.name,
            sources=sources,
            global_authors="",
            targets=default_targets(),
        )
        config = discord_inbound.load_config(env)
        self.assertEqual(config.sources[0].allowed_authors, {ALLOWED})

    def test_target_account_must_match_the_source(self) -> None:
        targets = default_targets()
        targets["admin-chat"]["connected_account_id"] = AUTOMATIONS_ACCOUNT
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_TARGETS=json.dumps(targets))

    def test_target_channel_must_match_the_source(self) -> None:
        sources = default_sources()
        sources[0]["target"] = "automations-target"
        targets = default_targets()
        targets["automations-target"] = {
            "channel_id": CHANNEL2,
            "connected_account_id": VORTEX_ACCOUNT,
        }
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(
                NOTIFY_INBOUND_SOURCES=json.dumps(sources),
                NOTIFY_TARGETS=json.dumps(targets),
            )

    def test_sources_cannot_combine_with_a_webhook(self) -> None:
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(DISCORD_WEBHOOK_URL="https://example.invalid/hook")

    def test_source_names_must_be_unique_and_safe(self) -> None:
        for bad in ("", "../escape", "has space", "."):
            sources = default_sources()
            sources[1]["name"] = bad
            with self.subTest(name=bad):
                with self.assertRaises(discord_inbound.ConfigError):
                    self.load(NOTIFY_INBOUND_SOURCES=json.dumps(sources))

        sources = default_sources()
        sources[1]["name"] = sources[0]["name"]
        with self.assertRaises(discord_inbound.ConfigError):
            self.load(NOTIFY_INBOUND_SOURCES=json.dumps(sources))


class MultiSourceFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(
            sources_env(
                self.temp.name,
                sources=default_sources(),
                targets=default_targets(),
            )
        )
        self.vortex, self.automations = self.config.sources
        self.vortex_store = discord_inbound.SeenStore(self.vortex.state_file)
        self.automations_store = discord_inbound.SeenStore(
            self.automations.state_file
        )

    def test_each_source_reads_its_own_channel_and_account(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_execute(config, tool, arguments, account):
            calls.append((arguments["channel_id"], account))
            return {"messages": []}

        with mock.patch.object(discord_inbound, "_execute_tool", fake_execute):
            with self.assertLogs("discord-inbound", level="WARNING"):
                discord_inbound.fetch_new_messages(
                    self.config, self.vortex_store, self.vortex
                )
            with self.assertLogs("discord-inbound", level="WARNING"):
                discord_inbound.fetch_new_messages(
                    self.config, self.automations_store, self.automations
                )
        self.assertEqual(
            calls,
            [
                (CHANNEL, VORTEX_ACCOUNT),
                (CHANNEL2, AUTOMATIONS_ACCOUNT),
            ],
        )

    def test_sources_have_independent_cursors(self) -> None:
        pages = {
            CHANNEL: [
                raw_message(id="900000000000001001", channel_id=CHANNEL)
            ],
            CHANNEL2: [
                raw_message(id="900000000000002001", channel_id=CHANNEL2)
            ],
        }

        def fake_execute(config, tool, arguments, account):
            return {"messages": pages[arguments["channel_id"]]}

        with mock.patch.object(discord_inbound, "_execute_tool", fake_execute):
            discord_inbound.fetch_new_messages(
                self.config, self.vortex_store, self.vortex
            )
            discord_inbound.fetch_new_messages(
                self.config, self.automations_store, self.automations
            )
        self.assertEqual(self.vortex_store.cursor, "900000000000001001")
        self.assertEqual(self.automations_store.cursor, "900000000000002001")

        pages[CHANNEL] = [
            raw_message(id="900000000000001003", channel_id=CHANNEL),
            raw_message(id="900000000000001002", channel_id=CHANNEL),
            raw_message(id="900000000000001001", channel_id=CHANNEL),
        ]
        pages[CHANNEL2] = [
            raw_message(id="900000000000002002", channel_id=CHANNEL2),
            raw_message(id="900000000000002001", channel_id=CHANNEL2),
        ]
        with mock.patch.object(discord_inbound, "_execute_tool", fake_execute):
            vortex_messages = discord_inbound.fetch_new_messages(
                self.config, self.vortex_store, self.vortex
            )
            automations_messages = discord_inbound.fetch_new_messages(
                self.config, self.automations_store, self.automations
            )

        self.assertEqual(
            [message["id"] for message in vortex_messages],
            ["900000000000001002", "900000000000001003"],
        )
        self.assertEqual(
            [message["id"] for message in automations_messages],
            ["900000000000002002"],
        )

        enqueued: list[str] = []
        discord_inbound.ingest_messages(
            self.vortex,
            vortex_messages,
            self.vortex_store,
            lambda text: enqueued.append(text) or True,
        )
        self.assertEqual(self.vortex_store.cursor, "900000000000001003")
        self.assertEqual(self.automations_store.cursor, "900000000000002001")

    def test_dedupe_is_per_source(self) -> None:
        message_id = "900000000000003001"
        vortex_message = raw_message(id=message_id, channel_id=CHANNEL)
        automations_message = raw_message(id=message_id, channel_id=CHANNEL2)

        first = discord_inbound.ingest_messages(
            self.vortex, [vortex_message], self.vortex_store, lambda _text: True
        )
        second = discord_inbound.ingest_messages(
            self.automations,
            [automations_message],
            self.automations_store,
            lambda _text: True,
        )
        self.assertTrue(first[0].accepted)
        self.assertTrue(second[0].accepted)
        self.assertTrue(self.vortex_store.is_seen(message_id))
        self.assertTrue(self.automations_store.is_seen(message_id))

        repeat = discord_inbound.ingest_messages(
            self.vortex, [vortex_message], self.vortex_store, lambda _text: True
        )
        self.assertEqual([decision.reason for decision in repeat], ["duplicate"])


class ReplyRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(
            sources_env(
                self.temp.name,
                sources=default_sources(),
                targets=default_targets(),
            )
        )
        self.vortex, self.automations = self.config.sources

    def test_target_source_tells_the_agent_to_use_notify_sh_target(self) -> None:
        instruction = discord_inbound.build_instruction(self.vortex, "hi")
        self.assertIn("/app/scripts/notify.sh -t admin-chat", instruction)

    def test_bot_source_tells_the_agent_to_use_plain_notify_sh(self) -> None:
        instruction = discord_inbound.build_instruction(self.automations, "hi")
        self.assertIn('/app/scripts/notify.sh -m "<message>"', instruction)
        self.assertNotIn("/app/scripts/notify.sh -t", instruction)

    def test_bot_reply_environment_is_pinned_to_the_source(self) -> None:
        base = {
            "COMPOSIO_TOOL": "SOME_OTHER_TOOL",
            "COMPOSIO_ARGS": '{"channel_id":"999999999999999999"}',
            "COMPOSIO_CONNECTED_ACCOUNT_ID": "ca_wrong",
            "DISCORD_WEBHOOK_URL": "https://example.invalid/hook",
            "NOTIFY_DEFAULT_TARGET": "admin-chat",
            "NOTIFY_CONTENT_DISABLED": "1",
        }
        with mock.patch.dict(os.environ, base, clear=False):
            def fake(command: object, **kwargs: object) -> FakeProcess:
                marker = kwargs["env"]["NOTIFY_SEND_MARKER"]  # type: ignore[index]
                pathlib.Path(marker).write_text("", encoding="utf-8")
                return FakeProcess(0)

            with mock.patch.object(
                discord_inbound.subprocess,
                "Popen",
                side_effect=fake,
            ) as popen:
                self.assertTrue(
                    discord_inbound.run_agent(
                        self.config, "instruction", source=self.automations
                    )
                )
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            environment["COMPOSIO_TOOL"],
            discord_inbound.REPLY_TOOL,
        )
        self.assertEqual(
            environment["COMPOSIO_CONNECTED_ACCOUNT_ID"],
            AUTOMATIONS_ACCOUNT,
        )
        self.assertEqual(
            json.loads(environment["COMPOSIO_ARGS"]),
            {"channel_id": CHANNEL2},
        )
        self.assertNotIn("DISCORD_WEBHOOK_URL", environment)
        self.assertNotIn("NOTIFY_DEFAULT_TARGET", environment)
        self.assertNotIn("NOTIFY_CONTENT_DISABLED", environment)

    def test_target_reply_keeps_the_normal_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
                "COMPOSIO_ARGS": '{"channel_id":"999999999999999999"}',
            },
            clear=False,
        ):
            with mock.patch.object(
                discord_inbound.subprocess,
                "Popen",
                return_value=FakeProcess(0),
            ) as popen:
                discord_inbound.run_agent(
                    self.config, "instruction", source=self.vortex
                )
        environment = popen.call_args.kwargs["env"]
        self.assertEqual(
            environment["COMPOSIO_TOOL"], "DISCORDBOT_CREATE_MESSAGE"
        )
        self.assertEqual(
            environment["COMPOSIO_ARGS"], '{"channel_id":"999999999999999999"}'
        )


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
            result = discord_inbound.fetch_new_messages(self.config, self.store)
        self.assertEqual(result, [])
        self.assertTrue(self.store.initialized)
        self.assertRegex(self.store.cursor, r"^[0-9]+$")
        self.assertNotEqual(self.store.cursor, "0")

    def test_unreadable_history_baseline_does_not_action_old_messages(self) -> None:
        # An empty result can mean the bot lacks Read Message History.  The
        # time-based baseline is still newer than all existing messages, so a
        # later permission grant cannot make old history look new.
        with self.assertLogs("discord-inbound", level="WARNING") as logs:
            with mock.patch.object(
                discord_inbound, "fetch_messages", return_value=[]
            ):
                result = discord_inbound.fetch_new_messages(
                    self.config, self.store
                )
        self.assertEqual(result, [])
        self.assertIn("time-based baseline", "\n".join(logs.output))

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


class ToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = discord_inbound.load_config(base_env(self.temp.name))

    def fetch(self, payload: object, **kwargs: object):
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(payload),
        ) as opener:
            messages = discord_inbound.fetch_messages(self.config, **kwargs)
        return messages, opener

    def test_request_shape(self) -> None:
        messages, opener = self.fetch(
            {
                "successful": True,
                "data": {"messages": [{"id": "900000000000000001"}]},
            },
            limit=1,
        )
        self.assertEqual(messages, [{"id": "900000000000000001"}])
        request = opener.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://backend.composio.dev/api/v3.1/tools/execute/"
            "DISCORDBOT_LIST_MESSAGES",
        )
        self.assertEqual(request.get_header("X-api-key"), self.config.api_key)
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["connected_account_id"], ACCOUNT)
        self.assertEqual(body["user_id"], "default")
        self.assertEqual(
            body["arguments"],
            {"channel_id": CHANNEL, "limit": 1},
        )
        self.assertNotIn("endpoint", body)
        self.assertNotIn("method", body)

    def test_before_is_sent_for_backward_paging(self) -> None:
        _, opener = self.fetch(
            {"successful": True, "data": {"messages": []}},
            before="900000000000000100",
            limit=2,
        )
        body = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(
            body["arguments"],
            {
                "channel_id": CHANNEL,
                "limit": 2,
                "before": "900000000000000100",
            },
        )

    def test_empty_message_list_is_success(self) -> None:
        messages, _ = self.fetch({"successful": True, "data": {"messages": []}})
        self.assertEqual(messages, [])

    def test_http_error_is_redacted(self) -> None:
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
        self.assertEqual(
            str(caught.exception), "tool execution returned HTTP 403"
        )

    def test_api_failure_is_an_error_without_echoing_the_body(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(
                {
                    "successful": False,
                    "error": "SECRET ERROR THAT MUST NOT BE LOGGED",
                }
            ),
        ):
            with self.assertRaises(discord_inbound.FetchError) as caught:
                discord_inbound.fetch_messages(self.config, limit=1)
        self.assertEqual(
            str(caught.exception), "tool execution was not successful"
        )
        self.assertNotIn("SECRET", str(caught.exception))

    def test_missing_success_flag_is_an_error(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse({"data": {"messages": []}}),
        ):
            with self.assertRaises(discord_inbound.FetchError):
                discord_inbound.fetch_messages(self.config, limit=1)

    def test_invalid_json_is_an_error(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(None, raw=b"not json"),
        ):
            with self.assertRaises(discord_inbound.FetchError):
                discord_inbound.fetch_messages(self.config, limit=1)

    def test_missing_data_is_an_error(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse({"successful": True}),
        ):
            with self.assertRaises(discord_inbound.FetchError):
                discord_inbound.fetch_messages(self.config, limit=1)

    def test_malformed_payload_shapes_are_errors(self) -> None:
        for payload in (
            {"successful": True, "data": []},
            {"successful": True, "data": {}},
            {"successful": True, "data": {"messages": {}}},
            {"successful": True, "data": {"messages": ["not-a-dict"]}},
        ):
            with self.subTest(payload=payload):
                with mock.patch.object(
                    discord_inbound,
                    "_open_request",
                    return_value=FakeResponse(payload),
                ):
                    with self.assertRaises(discord_inbound.FetchError):
                        discord_inbound.fetch_messages(self.config, limit=1)

    def test_rejects_a_message_without_an_id(self) -> None:
        with mock.patch.object(
            discord_inbound,
            "_open_request",
            return_value=FakeResponse(
                {"successful": True, "data": {"messages": [{}]}}
            ),
        ):
            with self.assertRaises(discord_inbound.FetchError):
                discord_inbound.fetch_messages(self.config, limit=1)

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


class FakeProcess:
    def __init__(
        self, returncode: int = 0, stdout: object | None = None
    ) -> None:
        self.returncode = returncode
        self.pid = 12345
        self.stdout = stdout

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
        def fake(command: object, **kwargs: object) -> FakeProcess:
            marker = kwargs["env"]["NOTIFY_SEND_MARKER"]  # type: ignore[index]
            pathlib.Path(marker).write_text("", encoding="utf-8")
            return FakeProcess(0)

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_DEBUG_LLM", None)
            with mock.patch.object(
                discord_inbound.subprocess, "Popen", side_effect=fake
            ) as popen:
                result = discord_inbound.run_agent(
                    self.config, "instruction text"
                )
        self.assertTrue(result)
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [
                TRUE_BIN,
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
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
        self.assertIn("NOTIFY_SEND_MARKER", environment)

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

    def _popen_side_effect(
        self, attempts: list[tuple[int, bool]]
    ) -> tuple[object, list[dict[str, object]]]:
        """Simulate attempts; the second tuple item touches the marker."""

        pending = list(attempts)
        calls: list[dict[str, object]] = []

        def fake(command: object, **kwargs: object) -> FakeProcess:
            returncode, send = pending.pop(0)
            calls.append({"command": command, "env": kwargs["env"]})
            marker = kwargs["env"]["NOTIFY_SEND_MARKER"]  # type: ignore[index]
            if send:
                pathlib.Path(marker).write_text("", encoding="utf-8")
            return FakeProcess(returncode)

        return fake, calls

    def test_nonzero_exit_is_reported(self) -> None:
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", return_value=FakeProcess(2)
        ):
            self.assertFalse(discord_inbound.run_agent(self.config, "instruction"))

    def test_exit_zero_without_a_marker_is_logged_and_retried_once(self) -> None:
        side_effect, calls = self._popen_side_effect([(0, False), (0, True)])
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=side_effect
        ):
            with self.assertLogs("discord-inbound", level="INFO") as logs:
                result = discord_inbound.run_agent(
                    self.config, "instruction"
                )
        self.assertTrue(result)
        self.assertEqual(len(calls), 2)
        output = "\n".join(logs.output)
        self.assertIn("agent finished without sending", output)
        self.assertIn(discord_inbound.AGENT_LOG_NAME, output)

        markers = [
            pathlib.Path(call["env"]["NOTIFY_SEND_MARKER"])  # type: ignore[index]
            for call in calls
        ]
        self.assertNotEqual(markers[0], markers[1])
        for marker in markers:
            self.assertFalse(marker.exists())

    def test_exit_zero_with_a_marker_is_not_retried(self) -> None:
        side_effect, calls = self._popen_side_effect([(0, True)])
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=side_effect
        ):
            result = discord_inbound.run_agent(self.config, "instruction")
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)

    def test_second_missing_marker_stops_after_two_attempts(self) -> None:
        side_effect, calls = self._popen_side_effect([(0, False), (0, False)])
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=side_effect
        ):
            with self.assertLogs("discord-inbound", level="INFO") as logs:
                result = discord_inbound.run_agent(
                    self.config, "instruction"
                )
        self.assertFalse(result)
        self.assertEqual(len(calls), 2)
        output = "\n".join(logs.output)
        self.assertIn("agent finished without sending", output)
        self.assertIn("agent retry finished without sending", output)

    def test_nonzero_exit_is_not_retried_even_without_a_marker(self) -> None:
        side_effect, calls = self._popen_side_effect([(2, False)])
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=side_effect
        ):
            result = discord_inbound.run_agent(self.config, "instruction")
        self.assertFalse(result)
        self.assertEqual(len(calls), 1)

    def test_agent_output_is_captured_privately_and_only_the_path_is_logged(
        self,
    ) -> None:
        stream = io.BytesIO(b"model output that must not reach the log\n")

        def fake(command: object, **kwargs: object) -> FakeProcess:
            marker = kwargs["env"]["NOTIFY_SEND_MARKER"]  # type: ignore[index]
            pathlib.Path(marker).write_text("", encoding="utf-8")
            return FakeProcess(0, stdout=stream)

        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=fake
        ):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                self.assertTrue(
                    discord_inbound.run_agent(self.config, "instruction")
                )

        log_path = discord_inbound.agent_log_path(self.config)
        self.assertTrue(log_path.exists())
        self.assertEqual(log_path.stat().st_mode & 0o777, 0o600)
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("path: inbound:default", text)
        self.assertIn("model: test/model", text)
        self.assertIn("instruction:\ninstruction", text)
        self.assertIn("--- agent output ---", text)
        self.assertIn("model output", text)
        self.assertIn("--- run end: exit=0", text)
        self.assertIn("sent=yes", text)
        self.assertLess(
            text.index("instruction:\ninstruction"),
            text.index("model output"),
        )
        self.assertLess(
            text.index("model output"), text.index("--- run end:")
        )
        summary = stderr.getvalue()
        self.assertIn("run=inbound:default", summary)
        self.assertNotIn("model output", summary)
        self.assertNotIn(str(log_path), summary)

    def test_agent_output_log_is_capped_and_notes_the_truncation(self) -> None:
        limit = 1024
        payload = b"x" * 2000

        def fake(command: object, **kwargs: object) -> FakeProcess:
            marker = kwargs["env"]["NOTIFY_SEND_MARKER"]  # type: ignore[index]
            pathlib.Path(marker).write_text("", encoding="utf-8")
            return FakeProcess(0, stdout=io.BytesIO(payload))

        with mock.patch.object(
            discord_inbound, "AGENT_LOG_MAX_BYTES", limit
        ):
            with mock.patch.object(
                discord_inbound.subprocess, "Popen", side_effect=fake
            ):
                self.assertTrue(
                    discord_inbound.run_agent(self.config, "instruction")
                )

        log_path = discord_inbound.agent_log_path(self.config)
        raw = log_path.read_bytes()
        self.assertLessEqual(len(raw), limit)
        self.assertIn(b"path: inbound:default", raw)
        self.assertIn(b"truncated", raw)
        self.assertIn(b"--- run end: exit=0", raw)

    def test_debug_llm_is_off_by_default(self) -> None:
        side_effect, calls = self._popen_side_effect([(0, True)])
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_DEBUG_LLM", None)
            with mock.patch.object(
                discord_inbound.subprocess, "Popen", side_effect=side_effect
            ):
                discord_inbound.run_agent(self.config, "instruction")
        self.assertNotIn("--print-logs", calls[0]["command"])

    def test_debug_llm_adds_provider_logging_when_requested(self) -> None:
        side_effect, calls = self._popen_side_effect([(0, True)])
        with mock.patch.dict(os.environ, {"NOTIFY_DEBUG_LLM": "1"}):
            with mock.patch.object(
                discord_inbound.subprocess, "Popen", side_effect=side_effect
            ):
                discord_inbound.run_agent(self.config, "instruction")
        command = calls[0]["command"]
        self.assertIn("--print-logs", command)
        self.assertIn("--log-level", command)
        self.assertIn("debug", command)


    def test_run_emits_exactly_one_summary_line(self) -> None:
        side_effect, _ = self._popen_side_effect([(0, True)])
        stderr = io.StringIO()
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=side_effect
        ):
            with mock.patch("sys.stderr", stderr):
                self.assertTrue(
                    discord_inbound.run_agent(self.config, "instruction")
                )
        lines = [line for line in stderr.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 1)
        self.assertIn("run=inbound:default", lines[0])
        self.assertIn("model=test/model", lines[0])
        self.assertIn("exit=0", lines[0])
        self.assertIn("sent=yes", lines[0])

    def test_each_retry_attempt_gets_its_own_summary_line(self) -> None:
        side_effect, _ = self._popen_side_effect([(0, False), (0, True)])
        stderr = io.StringIO()
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", side_effect=side_effect
        ):
            with mock.patch("sys.stderr", stderr):
                with self.assertLogs("discord-inbound", level="INFO"):
                    self.assertTrue(
                        discord_inbound.run_agent(self.config, "instruction")
                    )
        lines = [line for line in stderr.getvalue().splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertIn("sent=no", lines[0])
        self.assertIn("sent=yes", lines[1])


class SummaryTests(unittest.TestCase):
    def test_summary_is_one_flattened_truncated_line(self) -> None:
        instruction = "first line\n\nsecond\t\tline " + "x" * 200
        line = discord_inbound.agent_capture.summary_line(
            "inbound:vortex-admin", "test/model", 0, 31.04, True, instruction
        )
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\t", line)
        self.assertLessEqual(len(line), 200)
        self.assertIn("run=inbound:vortex-admin", line)
        self.assertIn("model=test/model", line)
        self.assertIn("exit=0", line)
        self.assertIn("31.0s", line)
        self.assertIn("sent=yes", line)
        self.assertIn("first line second line", line)
        self.assertIn("...", line)
        self.assertTrue(line.endswith('..."'))

    def test_long_source_name_cannot_exceed_the_cap(self) -> None:
        line = discord_inbound.agent_capture.summary_line(
            "inbound:" + "s" * 64,
            "m" * 120,
            1,
            2.0,
            False,
            "y" * 500,
        )
        self.assertNotIn("\n", line)
        self.assertLessEqual(len(line), 200)
        self.assertIn("sent=no", line)

    def test_summary_has_utc_timestamp_and_schedule_path(self) -> None:
        line = discord_inbound.agent_capture.summary_line(
            "schedule", "test/model", 0, 1.0, True, "do the thing"
        )
        self.assertRegex(
            line, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z "
        )
        self.assertIn("run=schedule", line)
        self.assertTrue(line.endswith('"do the thing"'))


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
        self.assertIn("host3.example.com", enqueued[0])
        self.assertIn("Report host data only", enqueued[0])
        self.assertTrue(self.store.is_seen("900000000000000001"))

        second = discord_inbound.ingest_messages(
            self.config,
            [raw_message()],
            self.store,
            lambda text: enqueued.append(text) or True,
        )
        self.assertEqual([decision.reason for decision in second], ["duplicate"])
        self.assertEqual(len(enqueued), 1)

    def test_inbound_instruction_never_names_withheld_capabilities(self) -> None:
        instruction = discord_inbound.build_instruction(
            self.config, "what is on the site?"
        )
        lowered = instruction.lower()
        for phrase in (
            "disabled",
            "content search",
            "browse",
            "read files",
        ):
            self.assertNotIn(phrase, lowered)
        self.assertIn("Report host data only", instruction)
        self.assertIn("capabilities", lowered)

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

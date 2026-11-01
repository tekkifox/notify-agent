#!/usr/bin/env python3
"""Tests for the CMS-first bot configuration resolver.

Every HTTP call goes to a loopback stub.  No live CMS, no real API key and no
network egress are used.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from types import TracebackType
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bot_config.py"
INBOUND_SCRIPT = ROOT / "scripts" / "discord_inbound.py"

spec = importlib.util.spec_from_file_location("bot_config", SCRIPT)
bot_config = importlib.util.module_from_spec(spec)
sys.modules["bot_config"] = bot_config
spec.loader.exec_module(bot_config)

inbound_spec = importlib.util.spec_from_file_location(
    "discord_inbound", INBOUND_SCRIPT
)
discord_inbound = importlib.util.module_from_spec(inbound_spec)
sys.modules["discord_inbound"] = discord_inbound
inbound_spec.loader.exec_module(discord_inbound)

CHANNEL = "111111111111111111"
OTHER_CHANNEL = "222222222222222222"
ACCOUNT = "ca_test_account"
AUTHORS = "333333333333333333"
API_KEY = "test-payload-key-not-a-real-secret"

ENV_TARGETS = (
    '{"env-target":{"channel_id":"999999999999999999",'
    '"connected_account_id":"ca_env"}}'
)
ENV_SOURCES = ""

CMS_TARGETS = {
    "panel-target": {
        "channel_id": CHANNEL,
        "connected_account_id": ACCOUNT,
        "host_scope": "panel.example",
    }
}
CMS_SOURCES = [
    {
        "name": "panel-source",
        "connected_account_id": ACCOUNT,
        "channel_id": CHANNEL,
        "allowed_authors": AUTHORS,
        "target": "panel-target",
    }
]


def document(
    *,
    targets: object | None = None,
    sources: object | None = None,
    version: object = 3,
    omit: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "version": version,
        "targets": CMS_TARGETS if targets is None else targets,
        "sources": CMS_SOURCES if sources is None else sources,
    }
    if omit is not None:
        body.pop(omit, None)
    return body


class _StubHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.server.requests.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        body = self.server.body  # type: ignore[attr-defined]
        status = self.server.status  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


class StubServer:
    """A small loopback CMS stub."""

    def __init__(self, status: int = 200, body: bytes = b"{}") -> None:
        self.status = status
        self.body = body
        self.requests: list[dict[str, object]] = []
        self._server = http.server.HTTPServer(("127.0.0.1", 0), _StubHandler)
        self._server.requests = self.requests  # type: ignore[attr-defined]
        self._server.body = body  # type: ignore[attr-defined]
        self._server.status = status  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/api/bot-settings"

    def set_response(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body
        self._server.status = status  # type: ignore[attr-defined]
        self._server.body = body  # type: ignore[attr-defined]

    def __enter__(self) -> "StubServer":
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._thread.join(timeout=5)
        self._server.server_close()


def encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class ResolverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_home = pathlib.Path(self.temp.name) / "state"
        self.env = {
            "XDG_STATE_HOME": str(self.state_home),
            "BOT_SETTINGS_API_KEY": API_KEY,
            "NOTIFY_TARGETS": ENV_TARGETS,
            "NOTIFY_INBOUND_SOURCES": ENV_SOURCES,
            "NOTIFY_INBOUND_ALLOWED_AUTHORS": AUTHORS,
            "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
        }

    @property
    def cache(self) -> pathlib.Path:
        return (
            self.state_home
            / bot_config.CACHE_DIR_NAME
            / bot_config.CACHE_FILE_NAME
        )

    def resolve(self, url: str, **changes: str) -> bot_config.Resolution:
        env = dict(self.env)
        env["BOT_SETTINGS_URL"] = url
        env.update(changes)
        return bot_config.resolve(env)

    def test_unset_url_is_byte_for_byte_the_environment(self) -> None:
        spaced = '{ "env-target" : { "channel_id" : "999999999999999999" } }'
        env = dict(self.env)
        env["NOTIFY_TARGETS"] = spaced
        env["NOTIFY_INBOUND_SOURCES"] = '[{"name":"env-source"}]'

        resolution = bot_config.resolve(env)

        self.assertEqual(resolution.source, "env")
        self.assertEqual(resolution.reason, "")
        self.assertFalse(resolution.log_required)
        self.assertEqual(resolution.targets_json, env["NOTIFY_TARGETS"])
        self.assertEqual(resolution.sources_json, env["NOTIFY_INBOUND_SOURCES"])
        self.assertFalse(self.cache.exists())

    def test_fetch_success_uses_cms_and_writes_cache(self) -> None:
        body = encode(document())
        with StubServer(200, body) as server:
            resolution = self.resolve(server.url)

            self.assertEqual(resolution.source, "cms")
            self.assertEqual(resolution.reason, "")
            self.assertEqual(resolution.log_line, "config source=cms")
            self.assertTrue(resolution.log_required)
            self.assertEqual(json.loads(resolution.targets_json), CMS_TARGETS)
            self.assertEqual(json.loads(resolution.sources_json), CMS_SOURCES)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(server.requests[0]["path"], "/api/bot-settings")
            self.assertEqual(
                server.requests[0]["authorization"],
                f"users API-Key {API_KEY}",
            )

        self.assertTrue(self.cache.is_file())
        self.assertEqual(self.cache.read_bytes(), body)
        self.assertEqual(stat.S_IMODE(self.cache.stat().st_mode), 0o600)
        leftovers = list(self.cache.parent.glob(".bot-settings-*.tmp"))
        self.assertEqual(leftovers, [])

    def test_401_falls_back_to_the_cache(self) -> None:
        good = encode(document())
        with StubServer(200, good) as server:
            self.assertEqual(self.resolve(server.url).source, "cms")

        with StubServer(401, b"denied") as server:
            resolution = self.resolve(server.url)

        self.assertEqual(resolution.source, "cache")
        self.assertEqual(resolution.reason, "cms 401")
        self.assertEqual(
            resolution.log_line, "config source=cache reason=cms 401"
        )
        self.assertEqual(json.loads(resolution.targets_json), CMS_TARGETS)
        self.assertNotIn(API_KEY, resolution.log_line)

    def test_no_cache_and_401_uses_the_environment(self) -> None:
        with StubServer(401, b"denied") as server:
            resolution = self.resolve(server.url)

        self.assertEqual(resolution.source, "env")
        self.assertEqual(resolution.reason, "cms 401")
        self.assertEqual(
            resolution.log_line, "config source=env reason=cms 401"
        )
        self.assertEqual(resolution.targets_json, ENV_TARGETS)

    def test_timeout_uses_the_cache(self) -> None:
        with StubServer(200, encode(document())) as server:
            self.assertEqual(self.resolve(server.url).source, "cms")

        timeout_env = {"BOT_SETTINGS_TIMEOUT_SECONDS": "0.25"}
        with StubServer(200, b"{}") as server:
            with mock.patch.object(
                bot_config._OPENER,
                "open",
                side_effect=socket.timeout("timed out"),
            ):
                resolution = self.resolve(server.url, **timeout_env)

        self.assertEqual(resolution.source, "cache")
        self.assertEqual(resolution.reason, "cms timeout 0.25s")
        self.assertEqual(
            resolution.log_line,
            "config source=cache reason=cms timeout 0.25s",
        )

    def test_invalid_url_falls_back_without_raising(self) -> None:
        resolution = self.resolve("not a url")
        self.assertEqual(resolution.source, "env")
        self.assertEqual(resolution.reason, "cms invalid URL")
        self.assertEqual(resolution.targets_json, ENV_TARGETS)

    def test_timeout_defaults_and_caps(self) -> None:
        self.assertEqual(bot_config.timeout_seconds({}), 5.0)
        self.assertEqual(
            bot_config.timeout_seconds({"BOT_SETTINGS_TIMEOUT_SECONDS": "99"}),
            30.0,
        )
        self.assertEqual(
            bot_config.timeout_seconds(
                {"BOT_SETTINGS_TIMEOUT_SECONDS": "not-a-number"}
            ),
            5.0,
        )

    def test_malformed_json_falls_back_to_the_cache(self) -> None:
        with StubServer(200, encode(document())) as server:
            self.assertEqual(self.resolve(server.url).source, "cms")

        with StubServer(200, b"not json") as server:
            resolution = self.resolve(server.url)

        self.assertEqual(resolution.source, "cache")
        self.assertEqual(resolution.reason, "cms invalid JSON")
        self.assertEqual(json.loads(resolution.targets_json), CMS_TARGETS)

    def test_document_must_have_version_targets_and_sources(self) -> None:
        for missing in ("version", "targets", "sources"):
            with self.subTest(missing=missing):
                with StubServer(
                    200, encode(document(omit=missing))
                ) as server:
                    resolution = self.resolve(server.url)
                self.assertEqual(resolution.source, "env")
                self.assertEqual(
                    resolution.reason, f"cms document is missing {missing}"
                )

    def test_invalid_targets_are_refused_and_fall_through(self) -> None:
        bad_targets = {
            "panel-target": {
                "channel_id": CHANNEL,
                "host_scope": ["panel.example", ""],
            }
        }
        with StubServer(200, encode(document())) as server:
            self.assertEqual(self.resolve(server.url).source, "cms")

        with StubServer(
            200, encode(document(targets=bad_targets, sources=[]))
        ) as server:
            resolution = self.resolve(server.url)

        self.assertEqual(resolution.source, "cache")
        self.assertIn("cms invalid host_scope for target(s) panel-target", resolution.reason)
        self.assertNotIn(CHANNEL, resolution.reason)
        self.assertEqual(json.loads(resolution.targets_json), CMS_TARGETS)

    def test_invalid_targets_without_cache_use_the_environment(self) -> None:
        bad_targets = {"panel-target": {"channel_id": ""}}
        with StubServer(
            200, encode(document(targets=bad_targets, sources=[]))
        ) as server:
            resolution = self.resolve(server.url)

        self.assertEqual(resolution.source, "env")
        self.assertEqual(
            resolution.reason,
            "cms NOTIFY_TARGETS must be a JSON object mapping names to "
            "objects with a channel_id",
        )
        self.assertEqual(resolution.targets_json, ENV_TARGETS)

    def test_invalid_sources_are_refused_by_the_listener_parser(self) -> None:
        bad_sources = [{"name": "panel-source"}]
        with StubServer(
            200, encode(document(sources=bad_sources))
        ) as server:
            resolution = self.resolve(server.url)

        self.assertEqual(resolution.source, "env")
        self.assertIn("cms NOTIFY_INBOUND_SOURCES entry 1", resolution.reason)
        self.assertIn("connected_account_id", resolution.reason)
        self.assertNotIn(CHANNEL, resolution.reason)

    def test_cache_write_is_atomic_and_keeps_the_old_copy(self) -> None:
        first = encode(document())
        second = encode(
            document(
                targets={"new-target": {"channel_id": OTHER_CHANNEL}},
                sources=[],
            )
        )
        with StubServer(200, first) as server:
            self.assertEqual(self.resolve(server.url).source, "cms")
        self.assertEqual(self.cache.read_bytes(), first)

        with StubServer(200, second) as server:
            with mock.patch.object(
                bot_config.os, "replace", side_effect=OSError("boom")
            ):
                resolution = self.resolve(server.url)

        # The fetched document was valid, so it is used even though the cache
        # could not be updated; the previous good copy is intact and no
        # partial temp file is left behind.
        self.assertEqual(resolution.source, "cms")
        self.assertEqual(json.loads(resolution.targets_json), {"new-target": {"channel_id": OTHER_CHANNEL}})
        self.assertEqual(self.cache.read_bytes(), first)
        self.assertEqual(
            list(self.cache.parent.glob(".bot-settings-*.tmp")), []
        )

    def test_api_key_never_appears_in_a_failure(self) -> None:
        with StubServer(401, b"denied") as server:
            resolution = self.resolve(server.url)
        self.assertNotIn(API_KEY, resolution.reason)
        self.assertNotIn(API_KEY, resolution.log_line)

        # A transport failure returns a fixed token, not the exception text.
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        env = dict(self.env)
        env["BOT_SETTINGS_URL"] = f"http://127.0.0.1:{port}/api/bot-settings"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--targets"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, ENV_TARGETS)
        self.assertNotIn(API_KEY, result.stderr)
        self.assertNotIn(API_KEY, result.stdout)
        self.assertIn("config source=env reason=cms network error", result.stderr)


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {
            "XDG_STATE_HOME": str(pathlib.Path(self.temp.name) / "state"),
            "BOT_SETTINGS_API_KEY": API_KEY,
            "NOTIFY_TARGETS": ENV_TARGETS,
            "NOTIFY_INBOUND_SOURCES": ENV_SOURCES,
            "NOTIFY_INBOUND_ALLOWED_AUTHORS": AUTHORS,
            "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
        }

    def run_cli(
        self, url: str | None, *args: str, **changes: str
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.pop("BOT_SETTINGS_URL", None)
        if url is not None:
            env["BOT_SETTINGS_URL"] = url
        env.update(changes)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_prints_json_and_logs_once(self) -> None:
        with StubServer(200, encode(document())) as server:
            result = self.run_cli(server.url, "--targets")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), CMS_TARGETS)
        self.assertEqual(result.stderr, "config source=cms\n")

    def test_cli_unset_url_is_silent_and_exact(self) -> None:
        raw = '{ "env-target" : {"channel_id":"999999999999999999"} }'
        result = self.run_cli(None, "--targets", NOTIFY_TARGETS=raw)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, raw)
        self.assertEqual(result.stderr, "")


class InboundWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_home = pathlib.Path(self.temp.name) / "state"
        self.env = {
            "XDG_STATE_HOME": str(self.state_home),
            "BOT_SETTINGS_API_KEY": API_KEY,
            "COMPOSIO_API_KEY": "composio-test-key",
            "COMPOSIO_TOOL": "DISCORDBOT_CREATE_MESSAGE",
            "COMPOSIO_CONNECTED_ACCOUNT_ID": ACCOUNT,
            "OPENCODE_MODEL": "test/model",
            "OPENCODE_BIN": sys.executable,
            "NOTIFY_INBOUND_ALLOWED_AUTHORS": AUTHORS,
            "NOTIFY_INBOUND_STATE_FILE": str(
                pathlib.Path(self.temp.name) / "inbound-state.json"
            ),
            "NOTIFY_TARGETS": ENV_TARGETS,
            "NOTIFY_INBOUND_SOURCES": json.dumps(
                [
                    {
                        "name": "env-source",
                        "connected_account_id": ACCOUNT,
                        "channel_id": OTHER_CHANNEL,
                        "allowed_authors": AUTHORS,
                    }
                ]
            ),
        }

    def test_listener_uses_the_cms_targets_and_sources(self) -> None:
        with StubServer(200, encode(document())) as server:
            env = dict(self.env)
            env["BOT_SETTINGS_URL"] = server.url
            with self.assertLogs("discord-inbound", level="INFO") as logs:
                resolved = discord_inbound.resolve_environment(env)
            config = discord_inbound.load_config(resolved)

        self.assertEqual([source.name for source in config.sources], ["panel-source"])
        self.assertEqual(config.sources[0].channel_id, CHANNEL)
        self.assertEqual(config.target.name, "panel-target")
        self.assertTrue(
            any("config source=cms" in line for line in logs.output)
        )

    def test_agent_child_receives_the_cms_targets(self) -> None:
        with StubServer(200, encode(document())) as server:
            env = dict(self.env)
            env["BOT_SETTINGS_URL"] = server.url
            config = discord_inbound.load_config(
                discord_inbound.resolve_environment(env)
            )
        process = mock.Mock()
        process.poll.return_value = 0
        with mock.patch.object(
            discord_inbound.subprocess, "Popen", return_value=process
        ) as popen:
            # The send marker is not produced by this stub, so the return
            # value is irrelevant here; the child environment is the subject.
            discord_inbound.run_agent(config, "instruction text")

        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(
            json.loads(child_env["NOTIFY_TARGETS"]), CMS_TARGETS
        )
        self.assertTrue(child_env["NOTIFY_INBOUND_SOURCES"])

    def test_listener_falls_back_to_env_without_a_cache(self) -> None:
        with StubServer(401, b"denied") as server:
            env = dict(self.env)
            env["BOT_SETTINGS_URL"] = server.url
            with self.assertLogs("discord-inbound", level="INFO") as logs:
                resolved = discord_inbound.resolve_environment(env)
            config = discord_inbound.load_config(resolved)

        self.assertEqual([source.name for source in config.sources], ["env-source"])
        self.assertTrue(
            any(
                "config source=env reason=cms 401" in line
                for line in logs.output
            )
        )

    def test_listener_unset_url_is_unchanged_and_silent(self) -> None:
        with self.assertNoLogs("discord-inbound", level="INFO"):
            resolved = discord_inbound.resolve_environment(self.env)

        self.assertEqual(resolved["NOTIFY_TARGETS"], ENV_TARGETS)
        self.assertEqual(
            resolved["NOTIFY_INBOUND_SOURCES"],
            self.env["NOTIFY_INBOUND_SOURCES"],
        )


if __name__ == "__main__":
    unittest.main()

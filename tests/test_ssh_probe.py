#!/usr/bin/env python3
"""Policy, classification and protocol tests for mcp/ssh_probe.py.

No SSH connection is made: every test injects a connector, and the one test
that exercises the CLI selftest asserts it opens nothing. The module imports
paramiko lazily, so this suite runs without it installed.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import socket
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "mcp" / "ssh_probe.py"

spec = importlib.util.spec_from_file_location("ssh_probe", MODULE)
ssh_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssh_probe)

SETTINGS = ssh_probe.Settings(
    user="agent",
    key_path="/run/secrets/id_ed25519",
    timeout=5.0,
    total_timeout=20.0,
    port=22,
    allowed_hosts=(
        "rossmoney.me",
        "photos.rossmoney.me",
        "vortexservers.co.uk",
    ),
)


class Connector:
    """Record calls and return a scripted outcome per host."""

    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = outcomes or {}

    def __call__(self, host, settings, timeout):
        self.calls.append((host, timeout))
        outcome = self.outcomes.get(host)
        if outcome is None:
            return ssh_probe.Outcome("ok", 12, "connected")
        return outcome


class PolicyTests(unittest.TestCase):
    def test_allowlisted_host_ok(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["rossmoney.me"], SETTINGS, connector=connector
        )
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(payload["results"][0]["latency_ms"], 12)
        self.assertEqual(connector.calls, [("rossmoney.me", 5.0)])

    def test_vortex_host_is_probed_when_in_scope(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["vortexservers.co.uk"],
            SETTINGS,
            scope=["vortexservers.co.uk"],
            connector=connector,
        )
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(len(connector.calls), 1)

    def test_rossmoney_is_refused_in_a_vortex_scope_without_connecting(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["rossmoney.me", "photos.rossmoney.me"],
            SETTINGS,
            scope=["vortexservers.co.uk"],
            connector=connector,
        )
        self.assertEqual(connector.calls, [])
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["out_of_scope", "out_of_scope"],
        )
        for result in payload["results"]:
            self.assertFalse(result["attempted"])
            self.assertIn(result["host"], result["message"])

    def test_host_outside_the_global_allowlist_is_refused_without_connecting(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["evil.example"], SETTINGS, connector=connector
        )
        self.assertEqual(connector.calls, [])
        result = payload["results"][0]
        self.assertEqual(result["status"], "not_allowed")
        self.assertIn("evil.example", result["message"])
        self.assertFalse(result["attempted"])

    def test_scope_covers_subdomains_but_not_lookalikes(self) -> None:
        self.assertTrue(
            ssh_probe._covered(
                "vortex1.vortexservers.co.uk", ("vortexservers.co.uk",)
            )
        )
        self.assertFalse(
            ssh_probe._covered(
                "notvortexservers.co.uk", ("vortexservers.co.uk",)
            )
        )
        self.assertFalse(
            ssh_probe._covered(
                "vortexservers.co.uk.example", ("vortexservers.co.uk",)
            )
        )

    def test_invalid_host_is_rejected_before_any_policy_or_connection(self) -> None:
        with self.assertRaises(ssh_probe.InputError):
            ssh_probe.prepare_hosts({"hosts": ["good.example\nvortex"]})


class FailureTests(unittest.TestCase):
    def test_each_failure_class_survives_the_tool(self) -> None:
        for status, message in (
            ("timeout", "connection timed out"),
            ("refused", "connection refused"),
            ("unreachable", "host could not be reached"),
            ("auth_refused", "authentication refused"),
            ("error", "ssh check failed"),
        ):
            with self.subTest(status=status):
                connector = Connector(
                    {"rossmoney.me": ssh_probe.Outcome(status, None, message)}
                )
                payload = ssh_probe.probe(
                    ["rossmoney.me"], SETTINGS, connector=connector
                )
                result = payload["results"][0]
                self.assertEqual(result["status"], status)
                self.assertEqual(result["message"], message)
                self.assertTrue(result["attempted"])

    def test_one_failing_host_does_not_poison_the_others(self) -> None:
        connector = Connector(
            {
                "rossmoney.me": ssh_probe.Outcome(
                    "auth_refused", None, "authentication refused"
                ),
                "photos.rossmoney.me": ssh_probe.Outcome("ok", 30, "connected"),
            }
        )
        payload = ssh_probe.probe(
            ["rossmoney.me", "photos.rossmoney.me"],
            SETTINGS,
            connector=connector,
        )
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["auth_refused", "ok"],
        )
        self.assertEqual(payload["results"][1]["latency_ms"], 30)

    def test_classification_of_builtin_exceptions(self) -> None:
        self.assertEqual(
            ssh_probe._classify_exception(socket.timeout()).status, "timeout"
        )
        self.assertEqual(
            ssh_probe._classify_exception(ConnectionRefusedError()).status,
            "refused",
        )
        self.assertEqual(
            ssh_probe._classify_exception(socket.gaierror()).status,
            "unreachable",
        )

        class AuthenticationException(Exception):
            pass

        self.assertEqual(
            ssh_probe._classify_exception(AuthenticationException()).status,
            "auth_refused",
        )

    def test_total_time_cap_stops_later_hosts(self) -> None:
        settings = SETTINGS._replace(timeout=5.0, total_timeout=10.0)
        clock = [0.0]

        def now():
            return clock[0]

        calls = []

        def connector(host, settings, timeout):
            calls.append((host, timeout))
            clock[0] += 6.0
            return ssh_probe.Outcome("ok", 6000, "connected")

        payload = ssh_probe.probe(
            ["a.rossmoney.me", "b.rossmoney.me", "c.rossmoney.me"],
            settings,
            connector=connector,
            now=now,
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual([call[1] for call in calls], [5.0, 4.0])
        last = payload["results"][2]
        self.assertEqual(last["status"], "timeout")
        self.assertFalse(last["attempted"])
        self.assertIn("total time cap", last["message"])

    def test_secrets_never_appear_in_output(self) -> None:
        payload = ssh_probe.probe(
            ["rossmoney.me"], SETTINGS, connector=Connector()
        )
        text = json.dumps(payload)
        self.assertNotIn("agent", text)
        self.assertNotIn("/run/secrets", text)
        self.assertNotIn("id_ed25519", text)


class ConfigurationTests(unittest.TestCase):
    def test_not_configured_is_explicit_and_not_an_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            payload = ssh_probe.probe(["rossmoney.me"], connector=Connector())
        self.assertFalse(payload["configured"])
        self.assertIn("SSH is not configured", payload["message"])
        self.assertEqual(payload["results"][0]["status"], "not_configured")
        self.assertFalse(payload["results"][0]["attempted"])

    def test_policy_refusal_still_wins_when_ssh_is_unconfigured(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            payload = ssh_probe.probe(
                ["evil.example"], connector=Connector()
            )
        self.assertEqual(payload["results"][0]["status"], "not_allowed")

    def test_call_probe_cannot_widen_the_run_scope(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_ALLOWED_HOSTS": (
                    "rossmoney.me,photos.rossmoney.me,vortexservers.co.uk"
                ),
                "NOTIFY_HOST_SCOPE": '["vortexservers.co.uk"]',
            }
            connector = Connector()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ssh_probe, "_connector", connector
            ):
                text, is_error = ssh_probe.call_probe(
                    {
                        "hosts": ["rossmoney.me", "vortexservers.co.uk"],
                        "scope": ["rossmoney.me", "vortexservers.co.uk"],
                    }
                )
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["out_of_scope", "ok"],
        )
        self.assertEqual(connector.calls, [("vortexservers.co.uk", 5.0)])

    def test_selftest_reports_configuration_without_connecting(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_TIMEOUT_SECONDS": "5",
                "SSH_TOTAL_TIMEOUT_SECONDS": "20",
                "SSH_ALLOWED_HOSTS": "rossmoney.me,vortexservers.co.uk",
            }
            boom = mock.Mock(side_effect=AssertionError("must not connect"))
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ssh_probe, "_connector", boom
            ), contextlib.redirect_stdout(stdout):
                rc = ssh_probe.selftest()
        output = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("ssh configured", output)
        self.assertIn("SSH_KEY_PATH readable", output)
        self.assertNotIn(key.name, output)
        self.assertNotIn("agent", output)
        boom.assert_not_called()

    def test_selftest_without_configuration_is_successful_and_plain(self) -> None:
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(
            stdout
        ):
            rc = ssh_probe.selftest()
        self.assertEqual(rc, 0)
        self.assertIn("SSH is not configured", stdout.getvalue())

    def test_selftest_rejects_a_bad_timeout(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_TIMEOUT_SECONDS": "not-a-number",
            }
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(
                stdout
            ), contextlib.redirect_stderr(stderr):
                rc = ssh_probe.selftest()
        self.assertEqual(rc, 1)
        self.assertIn("SSH_TIMEOUT_SECONDS must be a number", stderr.getvalue())


class ProtocolTests(unittest.TestCase):
    def test_tools_list_exposes_probe(self) -> None:
        responses = io.StringIO()
        requests = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n'
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        )
        with mock.patch.object(ssh_probe.sys, "stdin", io.StringIO(requests)), \
                contextlib.redirect_stdout(responses):
            ssh_probe._serve()
        messages = [json.loads(line) for line in responses.getvalue().splitlines()]
        tools = [
            tool["name"]
            for message in messages
            for tool in message.get("result", {}).get("tools", [])
        ]
        self.assertEqual(tools, ["probe"])
        # OpenCode names the external tool <server>_<tool>, hence ssh_probe.

    def test_unknown_tool_is_a_protocol_error(self) -> None:
        responses = io.StringIO()
        request = (
            '{"jsonrpc":"2.0","id":3,"method":"tools/call",'
            '"params":{"name":"other","arguments":{}}}\n'
        )
        with mock.patch.object(ssh_probe.sys, "stdin", io.StringIO(request)), \
                contextlib.redirect_stdout(responses):
            ssh_probe._serve()
        message = json.loads(responses.getvalue())
        self.assertEqual(message["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()

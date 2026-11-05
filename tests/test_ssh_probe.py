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
import sys
import tempfile
import types
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
        "host1.example.com",
        "host2.example.com",
        "host3.example.com",
    ),
)


class Connector:
    """Record calls and return a scripted outcome per host."""

    def __init__(self, outcomes=None):
        self.calls = []
        self.ports = []
        self.outcomes = outcomes or {}

    def __call__(self, host, settings, timeout):
        self.calls.append((host, timeout))
        self.ports.append(settings.port)
        outcome = self.outcomes.get(host)
        if outcome is None:
            return ssh_probe.Outcome("ok", 12, "connected", 0)
        return outcome


class _FakeStatusEvent:
    """The part of paramiko's threading.Event the probe waits on."""

    def __init__(self, ready=True):
        self.ready = ready
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.ready


class _FakeChannel:
    def __init__(self, exit_status=0, ready=True):
        self.exit_status = exit_status
        self.status_event = _FakeStatusEvent(ready)
        self.timeouts = []

    def settimeout(self, value):
        self.timeouts.append(value)


class _FakeStream:
    def __init__(self, payload, channel, error=None):
        self.payload = payload
        self.channel = channel
        self.error = error

    def read(self, size):
        if self.error is not None:
            raise self.error
        return self.payload[:size]


class JournalClient:
    """A fake SSH client whose only command is the fixed journal read."""

    def __init__(self, payload=b"", exit_status=0, ready=True, read_error=None):
        self.channel = _FakeChannel(exit_status, ready)
        self.stream = _FakeStream(payload, self.channel, read_error)
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append((command, timeout))
        return io.BytesIO(), self.stream, io.BytesIO()


class ParamikoClient(JournalClient):
    """A fake paramiko SSHClient for the full _probe_with_paramiko path."""

    def __init__(self, connect_error=None, **kwargs):
        super().__init__(**kwargs)
        self.connect_error = connect_error
        self.connect_kwargs = None
        self.policy = None
        self.closed = False

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        if self.connect_error is not None:
            raise self.connect_error

    def close(self):
        self.closed = True


def _fake_paramiko(client):
    module = types.ModuleType("paramiko")
    module.SSHClient = lambda: client
    module.AutoAddPolicy = lambda: object()
    return module


class PolicyTests(unittest.TestCase):
    def test_allowlisted_host_ok(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["host1.example.com"], SETTINGS, connector=connector
        )
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(payload["results"][0]["latency_ms"], 12)
        self.assertEqual(connector.calls, [("host1.example.com", 5.0)])

    def test_vortex_host_is_probed_when_in_scope(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["host3.example.com"],
            SETTINGS,
            scope=["host3.example.com"],
            connector=connector,
        )
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(len(connector.calls), 1)

    def test_rossmoney_is_refused_in_a_vortex_scope_without_connecting(self) -> None:
        connector = Connector()
        payload = ssh_probe.probe(
            ["host1.example.com", "host2.example.com"],
            SETTINGS,
            scope=["host3.example.com"],
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
                "vortex1.example.com", ("example.com",)
            )
        )
        self.assertFalse(
            ssh_probe._covered(
                "notexample.com", ("example.com",)
            )
        )
        self.assertFalse(
            ssh_probe._covered(
                "example.com.example", ("example.com",)
            )
        )

    def test_invalid_host_is_rejected_before_any_policy_or_connection(self) -> None:
        with self.assertRaises(ssh_probe.InputError):
            ssh_probe.prepare_hosts({"hosts": ["good.example\nvortex"]})


class HostPortTests(unittest.TestCase):
    def test_host_in_the_map_is_dialled_on_its_own_port(self) -> None:
        settings = SETTINGS._replace(
            host_ports=(("host1.example.com", 2222),)
        )
        connector = Connector()
        payload = ssh_probe.probe(
            ["host1.example.com"], settings, connector=connector
        )
        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(connector.ports, [2222])

    def test_host_absent_from_the_map_uses_ssh_port(self) -> None:
        settings = SETTINGS._replace(
            port=2022, host_ports=(("host1.example.com", 2222),)
        )
        connector = Connector()
        ssh_probe.probe(["host2.example.com"], settings, connector=connector)
        self.assertEqual(connector.ports, [2022])

    def test_map_keys_are_matched_after_normalisation(self) -> None:
        settings = SETTINGS._replace(
            host_ports=(("HOST1.Example.COM.", 2222),)
        )
        connector = Connector()
        ssh_probe.probe(["host1.example.com"], settings, connector=connector)
        self.assertEqual(connector.ports, [2222])

    def test_refused_hosts_are_still_refused_before_any_port_is_used(self) -> None:
        settings = SETTINGS._replace(
            host_ports=(("evil.example", 2222), ("host2.example.com", 2200))
        )
        connector = Connector()
        payload = ssh_probe.probe(
            ["evil.example", "host2.example.com"],
            settings,
            scope=["host1.example.com"],
            connector=connector,
        )
        self.assertEqual(connector.calls, [])
        self.assertEqual(connector.ports, [])
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["not_allowed", "out_of_scope"],
        )

    def test_load_settings_reads_and_normalises_the_map(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_PORT": "2022",
                "NOTIFY_HOST_PORTS": (
                    '{"HOST1.Example.COM.":2222,"host2.example.com":2200}'
                ),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                settings = ssh_probe.load_settings()
        self.assertEqual(settings.port, 2022)
        self.assertEqual(
            settings.host_ports,
            (("host1.example.com", 2222), ("host2.example.com", 2200)),
        )

    def test_malformed_map_fails_configuration_naming_the_variable(self) -> None:
        cases = (
            "not json",
            "[]",
            '"host1.example.com"',
            '{"host1.example.com":"22"}',
            '{"host1.example.com":0}',
            '{"host1.example.com":65536}',
            '{"host1.example.com":true}',
            '{"host1.example.com":22.5}',
            '{"host1.example.com":null}',
            '{"bad host":22}',
            '{"host1.example.com":22,"bad host":2222}',
        )
        with tempfile.NamedTemporaryFile() as key:
            for raw in cases:
                with self.subTest(raw=raw):
                    env = {
                        "SSH_USER": "agent",
                        "SSH_KEY_PATH": key.name,
                        "NOTIFY_HOST_PORTS": raw,
                    }
                    with mock.patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(ssh_probe.ConfigError) as raised:
                            ssh_probe.load_settings()
                    message = str(raised.exception)
                    self.assertIn("NOTIFY_HOST_PORTS", message)
                    self.assertNotIn("bad host", message)
                    self.assertNotIn(raw, message)


class TailnetTests(unittest.TestCase):
    def test_host_with_a_tailnet_address_is_dialled_there(self) -> None:
        settings = SETTINGS._replace(
            host_ports=(("host1.example.com", 2222),),
            host_tailnets=(("host1.example.com", "203.0.113.10"),),
        )
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            payload = ssh_probe.probe(["host1.example.com"], settings)

        self.assertEqual(payload["results"][0]["status"], "ok")
        # The panel name is still what the result reports ...
        self.assertEqual(payload["results"][0]["host"], "host1.example.com")
        # ... but the socket target is the tailnet address, on its own port.
        self.assertEqual(client.connect_kwargs["hostname"], "203.0.113.10")
        self.assertEqual(client.connect_kwargs["port"], 2222)

    def test_host_without_a_tailnet_address_is_dialled_by_name(self) -> None:
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            payload = ssh_probe.probe(["host1.example.com"], SETTINGS)

        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(client.connect_kwargs["hostname"], "host1.example.com")

    def test_tailnet_keys_are_matched_after_normalisation(self) -> None:
        settings = SETTINGS._replace(
            host_tailnets=(("HOST1.Example.COM.", "203.0.113.10"),)
        )
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            ssh_probe.probe(["host1.example.com"], settings)

        self.assertEqual(client.connect_kwargs["hostname"], "203.0.113.10")

    def test_refused_host_with_an_address_is_still_refused_before_dialling(self) -> None:
        settings = SETTINGS._replace(
            host_tailnets=(
                ("evil.example", "203.0.113.10"),
                ("host2.example.com", "203.0.113.11"),
            )
        )
        connector = Connector()
        payload = ssh_probe.probe(
            ["evil.example", "host2.example.com"],
            settings,
            scope=["host1.example.com"],
            connector=connector,
        )
        self.assertEqual(connector.calls, [])
        self.assertEqual(connector.ports, [])
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["not_allowed", "out_of_scope"],
        )

    def test_load_settings_reads_and_normalises_the_tailnet_map(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "NOTIFY_HOST_TAILNET": (
                    '{"HOST1.Example.COM.":"203.0.113.10",'
                    '"host2.example.com":" 203.0.113.11 "}'
                ),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                settings = ssh_probe.load_settings()
        self.assertEqual(
            settings.host_tailnets,
            (
                ("host1.example.com", "203.0.113.10"),
                ("host2.example.com", "203.0.113.11"),
            ),
        )

    def test_malformed_tailnet_map_fails_naming_the_variable_only(self) -> None:
        cases = (
            "not json",
            "[]",
            '"host1.example.com"',
            '{"host1.example.com":"not-an-ip"}',
            '{"host1.example.com":"999.1.1.1"}',
            '{"host1.example.com":"203.0.113"}',
            '{"host1.example.com":"203.0.113.10.1"}',
            '{"host1.example.com":"01.2.3.4"}',
            '{"host1.example.com":112}',
            '{"host1.example.com":null}',
            '{"bad host":"203.0.113.10"}',
            '{"host1.example.com":"203.0.113.10","bad host":"203.0.113.11"}',
        )
        with tempfile.NamedTemporaryFile() as key:
            for raw in cases:
                with self.subTest(raw=raw):
                    env = {
                        "SSH_USER": "agent",
                        "SSH_KEY_PATH": key.name,
                        "NOTIFY_HOST_TAILNET": raw,
                    }
                    with mock.patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(ssh_probe.ConfigError) as raised:
                            ssh_probe.load_settings()
                    message = str(raised.exception)
                    self.assertIn("NOTIFY_HOST_TAILNET", message)
                    self.assertNotIn("bad host", message)
                    self.assertNotIn(raw, message)
                    self.assertNotIn("203.0.113.10", message)


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
                    {"host1.example.com": ssh_probe.Outcome(status, None, message)}
                )
                payload = ssh_probe.probe(
                    ["host1.example.com"], SETTINGS, connector=connector
                )
                result = payload["results"][0]
                self.assertEqual(result["status"], status)
                self.assertEqual(result["message"], message)
                self.assertTrue(result["attempted"])

    def test_one_failing_host_does_not_poison_the_others(self) -> None:
        connector = Connector(
            {
                "host1.example.com": ssh_probe.Outcome(
                    "auth_refused", None, "authentication refused"
                ),
                "host2.example.com": ssh_probe.Outcome("ok", 30, "connected", 4),
            }
        )
        payload = ssh_probe.probe(
            ["host1.example.com", "host2.example.com"],
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
            ["a.host1.example.com", "b.host1.example.com", "c.host1.example.com"],
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
            ["host1.example.com"], SETTINGS, connector=Connector()
        )
        text = json.dumps(payload)
        self.assertNotIn("agent", text)
        self.assertNotIn("/run/secrets", text)
        self.assertNotIn("id_ed25519", text)


class FailedLoginTests(unittest.TestCase):
    def test_count_is_reported_alongside_the_existing_success_fields(self) -> None:
        journal = (
            b"Accepted publickey for agent from 203.0.113.10 port 51234 ssh2\n"
            b"Failed password for invalid user admin from 203.0.113.10 port 51235 ssh2\n"
            b"Failed password for root from 203.0.113.11 port 51236 ssh2\n"
            b"Failed password for root from 203.0.113.12 port 51237 ssh2\n"
        )
        client = JournalClient(payload=journal)
        count, reason = ssh_probe._read_failed_logins(client, 2.5)
        self.assertEqual((count, reason), (3, None))
        self.assertEqual(
            client.commands, [(ssh_probe.FAILED_LOGIN_COMMAND, 2.5)]
        )
        self.assertEqual(client.channel.timeouts, [2.5])
        self.assertEqual(client.channel.status_event.waits, [2.5])

        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("ok", 12, "connected", count, reason),
            0.0,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["latency_ms"], 12)
        self.assertEqual(result["message"], "connected")
        self.assertEqual(result["failed_logins"], 3)
        self.assertNotIn("failed_logins_reason", result)

    def test_a_readable_journal_with_no_failures_is_zero(self) -> None:
        client = JournalClient(
            payload=b"Accepted publickey for agent from 203.0.113.10 ssh2\n"
        )
        self.assertEqual(
            ssh_probe._read_failed_logins(client, 2.0), (0, None)
        )

    def test_unreadable_journal_is_null_with_a_reason(self) -> None:
        client = JournalClient(payload=b"", exit_status=1)
        self.assertEqual(
            ssh_probe._read_failed_logins(client, 2.0),
            (None, "unreadable"),
        )
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("ok", 12, "connected", None, "unreadable"),
            0.0,
        )
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["failed_logins"])
        self.assertEqual(result["failed_logins_reason"], "unreadable")

    def test_command_timeout_is_null_with_a_reason(self) -> None:
        client = JournalClient(read_error=socket.timeout())
        self.assertEqual(
            ssh_probe._read_failed_logins(client, 2.0), (None, "timeout")
        )

    def test_command_that_never_finishes_is_a_timeout(self) -> None:
        client = JournalClient(payload=b"", ready=False)
        self.assertEqual(
            ssh_probe._read_failed_logins(client, 2.0), (None, "timeout")
        )

    def test_missing_journalctl_is_unavailable_not_zero(self) -> None:
        client = JournalClient(exit_status=127)
        self.assertEqual(
            ssh_probe._read_failed_logins(client, 2.0),
            (None, "unavailable"),
        )

    def test_a_truncated_journal_is_null_not_an_undercount(self) -> None:
        client = JournalClient(
            payload=b"Failed password for root from 203.0.113.10 ssh2\n"
        )
        with mock.patch.object(ssh_probe, "FAILED_LOGIN_MAX_BYTES", 16):
            count, reason = ssh_probe._read_failed_logins(client, 2.0)
        self.assertIsNone(count)
        self.assertEqual(reason, "unavailable")

    def test_the_count_never_leaks_journal_text_or_the_command(self) -> None:
        journal = (
            b"Failed password for root from 203.0.113.10 port 22 ssh2\n"
            b"Failed password for admin from 203.0.113.11 port 22 ssh2\n"
            b"Accepted key /run/secrets/id_ed25519 for agent\n"
        )
        client = JournalClient(payload=journal)
        count, reason = ssh_probe._read_failed_logins(client, 2.0)
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("ok", 12, "connected", count, reason),
            0.0,
        )
        self.assertEqual(result["failed_logins"], 2)
        text = json.dumps(result)
        for secret in (
            "Failed password",
            "203.0.113.10",
            "203.0.113.11",
            "id_ed25519",
            "journalctl",
            "--unit=ssh",
            ssh_probe.FAILED_LOGIN_COMMAND,
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)

    def test_a_connector_cannot_smuggle_text_into_the_count_fields(self) -> None:
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome(
                "ok",
                12,
                "connected",
                "Failed password for root from 203.0.113.10",
                "journalctl --unit=ssh",
            ),
            0.0,
        )
        self.assertIsNone(result["failed_logins"])
        self.assertEqual(result["failed_logins_reason"], "unavailable")
        text = json.dumps(result)
        self.assertNotIn("Failed password", text)
        self.assertNotIn("journalctl", text)

    def test_a_failed_connection_has_no_count_field(self) -> None:
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("timeout", None, "connection timed out", 3, None),
            1.0,
        )
        self.assertNotIn("failed_logins", result)
        self.assertNotIn("failed_logins_reason", result)

    def test_the_probe_runs_the_fixed_command_on_the_session_it_opened(self) -> None:
        client = ParamikoClient(
            payload=(
                b"Failed password for root from 203.0.113.10 ssh2\n"
                b"Failed password for admin from 203.0.113.11 ssh2\n"
            )
        )
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            outcome = ssh_probe._probe_with_paramiko(
                "host1.example.com", SETTINGS, 5.0
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.failed_logins, 2)
        self.assertIsNone(outcome.failed_logins_reason)
        self.assertEqual(client.connect_kwargs["hostname"], "host1.example.com")
        self.assertEqual(
            [command for command, _timeout in client.commands],
            [ssh_probe.FAILED_LOGIN_COMMAND],
        )
        self.assertEqual(
            client.commands[0][1], ssh_probe.FAILED_LOGIN_TIMEOUT_SECONDS
        )
        self.assertTrue(client.closed)

    def test_unreadable_journal_keeps_the_probe_ok_but_null(self) -> None:
        client = ParamikoClient(exit_status=1)
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            outcome = ssh_probe._probe_with_paramiko(
                "host1.example.com", SETTINGS, 5.0
            )
        self.assertEqual(outcome.status, "ok")
        self.assertIsNone(outcome.failed_logins)
        self.assertEqual(outcome.failed_logins_reason, "unreadable")
        self.assertTrue(client.closed)

    def test_a_failed_connect_runs_no_command_and_closes(self) -> None:
        client = ParamikoClient(connect_error=socket.timeout())
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            outcome = ssh_probe._probe_with_paramiko(
                "host1.example.com", SETTINGS, 5.0
            )
        self.assertEqual(outcome.status, "timeout")
        self.assertEqual(client.commands, [])
        self.assertTrue(client.closed)


class ConfigurationTests(unittest.TestCase):
    def test_not_configured_is_explicit_and_not_an_error(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            payload = ssh_probe.probe(["host1.example.com"], connector=Connector())
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
                    "host1.example.com,host2.example.com,host3.example.com"
                ),
                "NOTIFY_HOST_SCOPE": '["host3.example.com"]',
            }
            connector = Connector()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ssh_probe, "_connector", connector
            ):
                text, is_error = ssh_probe.call_probe(
                    {
                        "hosts": ["host1.example.com", "host3.example.com"],
                        "scope": ["host1.example.com", "host3.example.com"],
                    }
                )
        self.assertFalse(is_error)
        payload = json.loads(text)
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["out_of_scope", "ok"],
        )
        self.assertEqual(connector.calls, [("host3.example.com", 5.0)])

    def test_selftest_reports_configuration_without_connecting(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_TIMEOUT_SECONDS": "5",
                "SSH_TOTAL_TIMEOUT_SECONDS": "20",
                "SSH_ALLOWED_HOSTS": "host1.example.com,host3.example.com",
                "NOTIFY_HOST_PORTS": '{"host1.example.com":2222}',
                "NOTIFY_HOST_TAILNET": (
                    '{"host1.example.com":"203.0.113.10"}'
                ),
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
        self.assertIn("per-host ports 1", output)
        self.assertIn("per-host tailnets 1", output)
        self.assertNotIn(key.name, output)
        self.assertNotIn("agent", output)
        self.assertNotIn("2222", output)
        self.assertNotIn("203.0.113.10", output)
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

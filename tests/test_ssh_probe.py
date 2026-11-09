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
import stat
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


class StatusClient:
    """A fake SSH client whose only command is the fixed status read."""

    def __init__(self, payload=b"", exit_status=0, ready=True, read_error=None):
        self.channel = _FakeChannel(exit_status, ready)
        self.stream = _FakeStream(payload, self.channel, read_error)
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append((command, timeout))
        return io.BytesIO(), self.stream, io.BytesIO()


class ParamikoClient(StatusClient):
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

    def test_host_with_a_tailnet_hostname_is_dialled_there(self) -> None:
        settings = SETTINGS._replace(
            host_tailnets=(("host1.example.com", "host.example.ts.net"),),
        )
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            payload = ssh_probe.probe(["host1.example.com"], settings)

        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(payload["results"][0]["host"], "host1.example.com")
        self.assertEqual(
            client.connect_kwargs["hostname"], "host.example.ts.net"
        )

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
                    '"host2.example.com":" 203.0.113.11 ",'
                    '"host3.example.com":"HOST.example.ts.net"}'
                ),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                settings = ssh_probe.load_settings()
        self.assertEqual(
            settings.host_tailnets,
            (
                ("host1.example.com", "203.0.113.10"),
                ("host2.example.com", "203.0.113.11"),
                ("host3.example.com", "HOST.example.ts.net"),
            ),
        )

    def test_malformed_tailnet_map_fails_naming_the_variable_only(self) -> None:
        long_label = "a" * 64
        long_name = ".".join(["a" * 63] * 4)  # 255 characters
        cases = (
            "not json",
            "[]",
            '"host1.example.com"',
            '{"host1.example.com":"999.1.1.1"}',
            '{"host1.example.com":"203.0.113"}',
            '{"host1.example.com":"203.0.113.10.1"}',
            '{"host1.example.com":"01.2.3.4"}',
            '{"host1.example.com":112}',
            '{"host1.example.com":null}',
            '{"bad host":"203.0.113.10"}',
            '{"host1.example.com":"203.0.113.10","bad host":"203.0.113.11"}',
            '{"host1.example.com":"-bad.example.ts.net"}',
            '{"host1.example.com":"bad-.example.ts.net"}',
            '{"host1.example.com":"bad..example.ts.net"}',
            '{"host1.example.com":"bad_host.example.ts.net"}',
            '{"host1.example.com":"host.example.ts.net."}',
            '{"host1.example.com":"host.example.ts.net:22"}',
            '{"host1.example.com":"https://host.example.ts.net"}',
            f'{{"host1.example.com":"{long_label}.example.ts.net"}}',
            f'{{"host1.example.com":"{long_name}"}}',
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


class LocalHostTests(unittest.TestCase):
    def test_local_host_is_dialled_at_the_gateway_and_reports_its_own_name(self) -> None:
        settings = SETTINGS._replace(
            host_ports=(("host1.example.com", 2222),),
            host_tailnets=(("host1.example.com", "203.0.113.10"),),
            local_host="host1.example.com",
            local_address="172.27.0.1",
        )
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            payload = ssh_probe.probe(["host1.example.com"], settings)

        result = payload["results"][0]
        self.assertEqual(result["status"], "ok")
        # The result still reports the panel name, not the gateway address.
        self.assertEqual(result["host"], "host1.example.com")
        # The destination is the default gateway, on the host's own port.
        self.assertEqual(client.connect_kwargs["hostname"], "172.27.0.1")
        self.assertEqual(client.connect_kwargs["port"], 2222)

    def test_remote_host_still_uses_its_tailnet_target(self) -> None:
        settings = SETTINGS._replace(
            host_tailnets=(("host2.example.com", "203.0.113.11"),),
            local_host="host1.example.com",
            local_address="172.27.0.1",
        )
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            payload = ssh_probe.probe(["host2.example.com"], settings)

        self.assertEqual(payload["results"][0]["host"], "host2.example.com")
        self.assertEqual(client.connect_kwargs["hostname"], "203.0.113.11")
        self.assertNotEqual(client.connect_kwargs["hostname"], "172.27.0.1")

    def test_unset_local_marker_changes_nothing(self) -> None:
        settings = SETTINGS._replace(
            host_tailnets=(("host1.example.com", "203.0.113.10"),)
        )
        client = ParamikoClient()
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            payload = ssh_probe.probe(["host1.example.com"], settings)

        self.assertEqual(payload["results"][0]["status"], "ok")
        self.assertEqual(client.connect_kwargs["hostname"], "203.0.113.10")

    def test_default_gateway_is_derived_from_the_route_table(self) -> None:
        route = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\t"
            "Mask\t\tMTU\tWindow\tIRTT\n"
            "eth0\t00000000\t01001BAC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
            "eth0\t00001BAC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"
        )
        self.assertEqual(ssh_probe._default_gateway(route), "172.27.0.1")

    def test_route_table_without_a_gateway_fails_naming_the_variable_only(self) -> None:
        route = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\t"
            "Mask\t\tMTU\tWindow\tIRTT\n"
            "eth0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n"
        )
        with self.assertRaises(ssh_probe.ConfigError) as raised:
            ssh_probe._default_gateway(route)
        message = str(raised.exception)
        self.assertIn("NOTIFY_LOCAL_HOST", message)
        self.assertNotIn("172.27.0.1", message)

    def test_malformed_local_marker_fails_naming_the_variable_only(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "NOTIFY_LOCAL_HOST": "bad host",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(ssh_probe.ConfigError) as raised:
                    ssh_probe.load_settings()
        message = str(raised.exception)
        self.assertIn("NOTIFY_LOCAL_HOST", message)
        self.assertNotIn("bad host", message)

    def test_load_settings_reads_the_marker_and_derives_the_gateway(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "NOTIFY_LOCAL_HOST": "HOST1.Example.COM.",
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ssh_probe, "_default_gateway", return_value="172.27.0.1"
            ) as gateway:
                settings = ssh_probe.load_settings()
        self.assertEqual(settings.local_host, "host1.example.com")
        self.assertEqual(settings.local_address, "172.27.0.1")
        gateway.assert_called_once_with()

    def test_load_settings_does_not_read_the_route_table_when_marker_absent(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
            }
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ssh_probe,
                "_default_gateway",
                side_effect=AssertionError("must not read the route table"),
            ):
                settings = ssh_probe.load_settings()
        self.assertEqual(settings.local_host, "")
        self.assertEqual(settings.local_address, "")


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


GOOD_LINE = (
    "2|media(71),mgmt(24),notify-check(27),photos(73),portfolio(70),"
    "root(10),ross(48),vortexservers(66)|59ba804d0837|no|"
    "2 gluetun storage-watchtower"
)


class StateHomeMixin:
    """Keep the keyhash state file inside a temporary XDG state home."""

    def setUp(self) -> None:
        super().setUp()
        self.state_home = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_home.cleanup)
        patcher = mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": self.state_home.name}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def state_path(self) -> str:
        return ssh_probe._state_file_path()


class StatusLineTests(StateHomeMixin, unittest.TestCase):
    def test_a_good_line_is_parsed_into_every_field(self) -> None:
        line = ssh_probe.parse_status_line(GOOD_LINE + "\n")
        self.assertIsNotNone(line)
        self.assertEqual(line.failed_logins, 2)
        self.assertEqual(
            line.logins,
            "media(71),mgmt(24),notify-check(27),photos(73),portfolio(70),"
            "root(10),ross(48),vortexservers(66)",
        )
        self.assertEqual(line.keyhash, "59ba804d0837")
        self.assertIs(line.reboot_required, False)
        self.assertEqual(line.outdated_images, "2 gluetun storage-watchtower")

    def test_not_checked_images_and_yes_reboot_are_kept(self) -> None:
        line = ssh_probe.parse_status_line(
            "0|notify-check(18),root(2),ross(79)|ccf7b3ebdad2|yes|not-checked"
        )
        self.assertIsNotNone(line)
        self.assertEqual(line.failed_logins, 0)
        self.assertIs(line.reboot_required, True)
        self.assertEqual(line.outdated_images, "not-checked")

    def test_a_short_line_is_null_not_invented(self) -> None:
        for text in (
            "",
            "\n\n",
            "0|notify-check(22),root(18)",
            "0|a|59ba804d0837|no",
            "0",
        ):
            with self.subTest(text=text):
                self.assertIsNone(ssh_probe.parse_status_line(text))

    def test_a_malformed_line_is_refused_as_a_whole(self) -> None:
        for text in (
            "x|logins|59ba804d0837|no|not-checked",
            "-1|logins|59ba804d0837|no|not-checked",
            "1.5|logins|59ba804d0837|no|not-checked",
            "0|logins|59ba804d0837|no|1 a|b",
            "0|logins|59ba804d0837|no|1 a|b|c",
        ):
            with self.subTest(text=text):
                self.assertIsNone(ssh_probe.parse_status_line(text))

    def test_only_the_last_non_empty_line_is_used(self) -> None:
        line = ssh_probe.parse_status_line(
            "banner text\n" + GOOD_LINE + "\n\n"
        )
        self.assertIsNotNone(line)
        self.assertEqual(line.failed_logins, 2)

    def test_absent_optional_fields_are_null(self) -> None:
        line = ssh_probe.parse_status_line("0|||maybe|")
        self.assertIsNotNone(line)
        self.assertEqual(line.failed_logins, 0)
        self.assertIsNone(line.logins)
        self.assertIsNone(line.keyhash)
        self.assertIsNone(line.reboot_required)
        self.assertIsNone(line.outdated_images)

    def test_an_invalid_keyhash_is_not_a_comparison_baseline(self) -> None:
        line = ssh_probe.parse_status_line(
            "0|root(1)|NOT-A-HASH|no|not-checked"
        )
        self.assertIsNotNone(line)
        self.assertEqual(line.failed_logins, 0)
        self.assertIsNone(line.keyhash)

    def test_control_characters_cannot_reach_the_result(self) -> None:
        line = ssh_probe.parse_status_line(
            "0|root(1)\x1b[31m,ross(2)\x07|59ba804d0837|no|"
            "1 bad\x00image\n"
        )
        self.assertIsNotNone(line)
        self.assertEqual(line.logins, "root(1) [31m,ross(2)")
        self.assertEqual(line.outdated_images, "1 bad image")

    def test_an_overlong_status_field_is_refused(self) -> None:
        line = ssh_probe.parse_status_line(
            "0|root(1)|59ba804d0837|no|" + "x" * 600
        )
        self.assertIsNotNone(line)
        self.assertIsNone(line.outdated_images)

    def test_a_result_carries_the_whole_line_and_the_existing_fields(self) -> None:
        line = ssh_probe.parse_status_line(GOOD_LINE)
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome(
                "ok",
                12,
                "connected",
                line.failed_logins,
                None,
                line.logins,
                line.keyhash,
                line.reboot_required,
                line.outdated_images,
            ),
            0.0,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["latency_ms"], 12)
        self.assertEqual(result["message"], "connected")
        self.assertEqual(result["failed_logins"], 2)
        self.assertNotIn("failed_logins_reason", result)
        self.assertEqual(result["logins"], line.logins)
        self.assertEqual(result["keyhash"], "59ba804d0837")
        self.assertIs(result["keys_changed"], False)
        self.assertIs(result["reboot_required"], False)
        self.assertEqual(
            result["outdated_images"], "2 gluetun storage-watchtower"
        )

    def test_a_malformed_line_leaves_the_count_null_with_a_reason(self) -> None:
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("ok", 12, "connected", None, "malformed"),
            0.0,
        )
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["failed_logins"])
        self.assertEqual(result["failed_logins_reason"], "malformed")
        self.assertIsNone(result["logins"])
        self.assertIsNone(result["keyhash"])
        self.assertIsNone(result["keys_changed"])
        self.assertIsNone(result["reboot_required"])
        self.assertIsNone(result["outdated_images"])

    def test_a_failed_connection_has_no_status_fields(self) -> None:
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("timeout", None, "connection timed out", 3, None),
            1.0,
        )
        for field in (
            "failed_logins",
            "failed_logins_reason",
            "logins",
            "keyhash",
            "keys_changed",
            "reboot_required",
            "outdated_images",
        ):
            with self.subTest(field=field):
                self.assertNotIn(field, result)


class KeyHashStateTests(StateHomeMixin, unittest.TestCase):
    def _result(self, host: str, keyhash: str) -> dict:
        return ssh_probe._outcome_result(
            host,
            ssh_probe.Outcome(
                "ok", 12, "connected", 0, None, "root(1)", keyhash, False,
                "not-checked",
            ),
            0.0,
        )

    def test_first_sighting_is_false_and_a_new_hash_is_true(self) -> None:
        first = self._result("host1.example.com", "59ba804d0837")
        self.assertIs(first["keys_changed"], False)
        # The same hash on the next probe is not a change.
        self.assertIs(
            self._result("host1.example.com", "59ba804d0837")["keys_changed"],
            False,
        )
        # A different hash is.
        self.assertIs(
            self._result("host1.example.com", "0e6fb55b3872")["keys_changed"],
            True,
        )
        # ...and it becomes the new baseline.
        self.assertIs(
            self._result("host1.example.com", "0e6fb55b3872")["keys_changed"],
            False,
        )

    def test_each_host_has_its_own_baseline(self) -> None:
        self.assertIs(
            self._result("host1.example.com", "59ba804d0837")["keys_changed"],
            False,
        )
        # A different host is a first sighting even with a different hash.
        self.assertIs(
            self._result("host2.example.com", "0e6fb55b3872")["keys_changed"],
            False,
        )
        self.assertIs(
            self._result("host1.example.com", "59ba804d0837")["keys_changed"],
            False,
        )

    def test_the_state_file_is_private_and_under_xdg_state_home(self) -> None:
        self._result("host1.example.com", "59ba804d0837")
        self.assertTrue(os.path.isfile(self.state_path))
        self.assertTrue(
            self.state_path.startswith(os.path.join(self.state_home.name, ""))
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.state_path).st_mode), 0o600
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(os.path.dirname(self.state_path)).st_mode),
            0o700,
        )
        saved = json.loads(pathlib.Path(self.state_path).read_text("utf-8"))
        self.assertEqual(saved, {"host1.example.com": "59ba804d0837"})

    def test_a_corrupt_state_file_is_a_first_sighting(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        pathlib.Path(self.state_path).write_text("{not json", encoding="utf-8")
        self.assertIs(
            self._result("host1.example.com", "59ba804d0837")["keys_changed"],
            False,
        )
        # The corrupt file was replaced, so the next sighting compares again.
        self.assertIs(
            self._result("host1.example.com", "0e6fb55b3872")["keys_changed"],
            True,
        )

    def test_a_state_file_with_bad_entries_is_cleaned_not_trusted(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        pathlib.Path(self.state_path).write_text(
            json.dumps(
                {
                    "host1.example.com": "not-a-hash",
                    "host2.example.com": 7,
                    "host3.example.com": "59ba804d0837",
                }
            ),
            encoding="utf-8",
        )
        result = self._result("host1.example.com", "59ba804d0837")
        self.assertIs(result["keys_changed"], False)
        saved = json.loads(pathlib.Path(self.state_path).read_text("utf-8"))
        self.assertEqual(saved["host1.example.com"], "59ba804d0837")
        self.assertNotIn("host2.example.com", saved)

    def test_a_missing_keyhash_has_no_keys_changed_answer(self) -> None:
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome("ok", 12, "connected", 0, None),
            0.0,
        )
        self.assertIsNone(result["keyhash"])
        self.assertIsNone(result["keys_changed"])

    def test_an_unwritable_state_directory_never_fails_the_probe(self) -> None:
        blocker = pathlib.Path(self.state_home.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        with mock.patch.dict(
            os.environ, {"XDG_STATE_HOME": str(blocker)}, clear=False
        ):
            result = self._result("host1.example.com", "59ba804d0837")
        self.assertEqual(result["status"], "ok")
        self.assertIs(result["keys_changed"], False)


class StatusReadTests(StateHomeMixin, unittest.TestCase):
    def test_the_fixed_command_is_the_only_one_and_uses_its_own_timeout(self) -> None:
        client = StatusClient(payload=(GOOD_LINE + "\n").encode())
        line, reason = ssh_probe._read_status_line(client, 2.5)
        self.assertIsNone(reason)
        self.assertIsNotNone(line)
        self.assertEqual(line.failed_logins, 2)
        self.assertEqual(client.commands, [(ssh_probe.STATUS_COMMAND, 2.5)])
        self.assertEqual(client.channel.timeouts, [2.5])
        self.assertEqual(client.channel.status_event.waits, [2.5])

    def test_a_readable_line_with_no_failures_is_zero(self) -> None:
        client = StatusClient(payload=b"0|root(1)|59ba804d0837|no|not-checked\n")
        line, reason = ssh_probe._read_status_line(client, 2.0)
        self.assertIsNone(reason)
        self.assertEqual(line.failed_logins, 0)

    def test_unreadable_line_is_null_with_a_reason(self) -> None:
        client = StatusClient(payload=b"", exit_status=1)
        self.assertEqual(
            ssh_probe._read_status_line(client, 2.0),
            (None, "unreadable"),
        )

    def test_a_read_timeout_is_null_with_a_reason(self) -> None:
        client = StatusClient(read_error=socket.timeout())
        self.assertEqual(
            ssh_probe._read_status_line(client, 2.0), (None, "timeout")
        )

    def test_a_command_that_never_finishes_is_a_timeout(self) -> None:
        client = StatusClient(payload=b"", ready=False)
        self.assertEqual(
            ssh_probe._read_status_line(client, 2.0), (None, "timeout")
        )

    def test_a_missing_status_command_is_unavailable_not_zero(self) -> None:
        client = StatusClient(exit_status=127)
        self.assertEqual(
            ssh_probe._read_status_line(client, 2.0),
            (None, "unavailable"),
        )

    def test_oversized_output_is_unavailable_not_truncated(self) -> None:
        client = StatusClient(
            payload=b"0|root(1)|59ba804d0837|no|not-checked\n"
        )
        with mock.patch.object(ssh_probe, "STATUS_MAX_BYTES", 16):
            self.assertEqual(
                ssh_probe._read_status_line(client, 2.0),
                (None, "unavailable"),
            )

    def test_output_that_is_not_the_status_line_is_malformed(self) -> None:
        for payload in (b"hello\n", b"0|root(1)\n", b"\n"):
            with self.subTest(payload=payload):
                client = StatusClient(payload=payload)
                self.assertEqual(
                    ssh_probe._read_status_line(client, 2.0),
                    (None, "malformed"),
                )

    def test_no_secret_or_command_text_reaches_the_result(self) -> None:
        client = StatusClient(payload=(GOOD_LINE + "\n").encode())
        line, reason = ssh_probe._read_status_line(client, 2.0)
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome(
                "ok",
                12,
                "connected",
                line.failed_logins,
                reason,
                line.logins,
                line.keyhash,
                line.reboot_required,
                line.outdated_images,
            ),
            0.0,
        )
        text = json.dumps(result)
        for secret in (
            "id_ed25519",
            "/run/secrets",
            "agent",
            "journalctl",
            ssh_probe.STATUS_COMMAND,
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)

    def test_a_connector_cannot_smuggle_text_into_the_status_fields(self) -> None:
        result = ssh_probe._outcome_result(
            "host1.example.com",
            ssh_probe.Outcome(
                "ok",
                12,
                "connected",
                "Failed password for root from 203.0.113.10",
                "journalctl --unit=ssh",
                "root(1)\nsecond line",
                "NOT-A-HASH",
                "yes",
                "x" * 4000,
            ),
            0.0,
        )
        self.assertIsNone(result["failed_logins"])
        self.assertEqual(result["failed_logins_reason"], "unavailable")
        self.assertEqual(result["logins"], "root(1) second line")
        self.assertIsNone(result["keyhash"])
        self.assertIsNone(result["keys_changed"])
        self.assertIsNone(result["reboot_required"])
        self.assertIsNone(result["outdated_images"])
        text = json.dumps(result)
        self.assertNotIn("Failed password", text)
        self.assertNotIn("journalctl", text)

    def test_the_probe_runs_the_fixed_command_on_the_session_it_opened(self) -> None:
        client = ParamikoClient(payload=(GOOD_LINE + "\n").encode())
        with mock.patch.dict(sys.modules, {"paramiko": _fake_paramiko(client)}):
            outcome = ssh_probe._probe_with_paramiko(
                "host1.example.com", SETTINGS, 5.0
            )
        self.assertEqual(outcome.status, "ok")
        self.assertEqual(outcome.failed_logins, 2)
        self.assertIsNone(outcome.failed_logins_reason)
        self.assertEqual(outcome.keyhash, "59ba804d0837")
        self.assertIs(outcome.reboot_required, False)
        self.assertEqual(
            outcome.outdated_images, "2 gluetun storage-watchtower"
        )
        self.assertEqual(client.connect_kwargs["hostname"], "host1.example.com")
        self.assertEqual(
            [command for command, _timeout in client.commands],
            [ssh_probe.STATUS_COMMAND],
        )
        self.assertEqual(
            client.commands[0][1], ssh_probe.STATUS_TIMEOUT_SECONDS
        )
        self.assertTrue(client.closed)

    def test_an_unreadable_line_keeps_the_probe_ok_but_null(self) -> None:
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
                "NOTIFY_LOCAL_HOST": "host1.example.com",
            }
            boom = mock.Mock(side_effect=AssertionError("must not connect"))
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
                ssh_probe, "_connector", boom
            ), mock.patch.object(
                ssh_probe, "_default_gateway", return_value="172.27.0.1"
            ), contextlib.redirect_stdout(stdout):
                rc = ssh_probe.selftest()
        output = stdout.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("ssh configured", output)
        self.assertIn("SSH_KEY_PATH readable", output)
        self.assertIn("per-host ports 1", output)
        self.assertIn("per-host tailnets 1", output)
        self.assertIn("local host set", output)
        self.assertNotIn(key.name, output)
        self.assertNotIn("agent", output)
        self.assertNotIn("2222", output)
        self.assertNotIn("203.0.113.10", output)
        self.assertNotIn("host1.example.com", output)
        self.assertNotIn("172.27.0.1", output)
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


class CmsAllowlistTests(unittest.TestCase):
    def test_cms_allowlist_replaces_ssh_allowed_hosts(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_ALLOWED_HOSTS": "legacy.example",
                "NOTIFY_ALLOWED_HOSTS": '["cms.example.com"]',
            }
            connector = Connector()
            with mock.patch.dict(os.environ, env, clear=True):
                payload = ssh_probe.probe(
                    ["cms.example.com", "legacy.example"],
                    connector=connector,
                )
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["ok", "not_allowed"],
        )
        self.assertEqual(connector.calls, [("cms.example.com", 5.0)])

    def test_an_empty_cms_allowlist_permits_nothing(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_ALLOWED_HOSTS": "host1.example.com",
                "NOTIFY_ALLOWED_HOSTS": "[]",
            }
            connector = Connector()
            with mock.patch.dict(os.environ, env, clear=True):
                payload = ssh_probe.probe(
                    ["host1.example.com"], connector=connector
                )
        self.assertEqual(payload["results"][0]["status"], "not_allowed")
        self.assertEqual(connector.calls, [])

    def test_cms_allowlist_still_uses_the_run_scope(self) -> None:
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "NOTIFY_ALLOWED_HOSTS": (
                    '["host1.example.com","host3.example.com"]'
                ),
                "NOTIFY_HOST_SCOPE": '["host3.example.com"]',
            }
            connector = Connector()
            with mock.patch.dict(os.environ, env, clear=True):
                payload = ssh_probe.probe(
                    ["host1.example.com", "host3.example.com"],
                    run_scope=ssh_probe.parse_run_scope(),
                    connector=connector,
                )
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["out_of_scope", "ok"],
        )
        self.assertEqual(connector.calls, [("host3.example.com", 5.0)])

    def test_a_malformed_cms_allowlist_fails_naming_only_the_variable(
        self,
    ) -> None:
        for value in ("not json", '{"hosts":[]}', '["ok.example",7]', '["bad host"]'):
            with self.subTest(value=value), tempfile.NamedTemporaryFile() as key:
                env = {
                    "SSH_USER": "agent",
                    "SSH_KEY_PATH": key.name,
                    "NOTIFY_ALLOWED_HOSTS": value,
                }
                with mock.patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(ssh_probe.ConfigError) as caught:
                        ssh_probe.load_settings()
                self.assertIn("NOTIFY_ALLOWED_HOSTS", str(caught.exception))


class ScopeArgumentTests(unittest.TestCase):
    def _call(self, run_scope, requested, hosts):
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_ALLOWED_HOSTS": (
                    "host1.example.com,host2.example.com,host3.example.com"
                ),
                "NOTIFY_HOST_SCOPE": json.dumps(run_scope),
            }
            connector = Connector()
            with mock.patch.dict(
                os.environ, env, clear=True
            ), mock.patch.object(ssh_probe, "_connector", connector):
                text, is_error = ssh_probe.call_probe(
                    {"hosts": hosts, "scope": requested}
                )
        return json.loads(text), is_error, connector

    def test_a_json_array_string_equal_to_the_run_scope_is_harmless(self) -> None:
        payload, is_error, connector = self._call(
            ["host1.example.com", "host3.example.com"],
            '["host1.example.com","host3.example.com"]',
            ["host1.example.com", "host3.example.com"],
        )
        self.assertFalse(is_error)
        self.assertEqual(
            [result["status"] for result in payload["results"]], ["ok", "ok"]
        )
        self.assertEqual(len(connector.calls), 2)

    def test_a_json_array_string_scope_cannot_widen_the_run_scope(self) -> None:
        payload, is_error, connector = self._call(
            ["host3.example.com"],
            '["host1.example.com","host3.example.com"]',
            ["host1.example.com", "host3.example.com"],
        )
        self.assertFalse(is_error)
        self.assertEqual(
            [result["status"] for result in payload["results"]],
            ["out_of_scope", "ok"],
        )
        self.assertEqual(connector.calls, [("host3.example.com", 5.0)])

    def test_a_malformed_json_array_string_is_still_refused(self) -> None:
        env = {
            "SSH_USER": "agent",
            "SSH_KEY_PATH": "/run/secrets/id_ed25519",
            "SSH_ALLOWED_HOSTS": "host3.example.com",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            ssh_probe, "_connector", Connector()
        ):
            text, is_error = ssh_probe.call_probe(
                {"hosts": ["host3.example.com"], "scope": '["host3.example.com"'}
            )
        self.assertTrue(is_error)
        self.assertIn("scope", json.loads(text)["error"])


class DefaultScopeTests(unittest.TestCase):
    """An omitted host list falls back to the run scope, never to everything."""

    def _call(self, run_scope, arguments, allowed=None):
        with tempfile.NamedTemporaryFile() as key:
            env = {
                "SSH_USER": "agent",
                "SSH_KEY_PATH": key.name,
                "SSH_ALLOWED_HOSTS": (
                    allowed
                    if allowed is not None
                    else "host1.example.com,host2.example.com,host3.example.com"
                ),
            }
            if run_scope is not None:
                env["NOTIFY_HOST_SCOPE"] = json.dumps(run_scope)
            connector = Connector()
            with mock.patch.dict(
                os.environ, env, clear=True
            ), mock.patch.object(ssh_probe, "_connector", connector):
                text, is_error = ssh_probe.call_probe(arguments)
        return json.loads(text), is_error, connector

    def test_omitted_hosts_probe_exactly_the_run_scope(self) -> None:
        payload, is_error, connector = self._call(
            ["host1.example.com", "host3.example.com"], {}
        )
        self.assertFalse(is_error)
        self.assertEqual(
            [result["status"] for result in payload["results"]], ["ok", "ok"]
        )
        self.assertEqual(
            connector.calls,
            [("host1.example.com", 5.0), ("host3.example.com", 5.0)],
        )

    def test_omitted_hosts_without_a_scope_are_refused(self) -> None:
        payload, is_error, connector = self._call(None, {})
        self.assertTrue(is_error)
        self.assertIn("no host scope", payload["error"])
        self.assertEqual(connector.calls, [])

    def test_an_empty_scope_is_not_a_fallback_to_everything(self) -> None:
        payload, is_error, connector = self._call([], {})
        self.assertTrue(is_error)
        self.assertIn("no host scope", payload["error"])
        self.assertEqual(connector.calls, [])

    def test_an_explicit_list_still_narrows_the_scope(self) -> None:
        payload, is_error, connector = self._call(
            ["host1.example.com", "host3.example.com"],
            {"hosts": ["host1.example.com"]},
        )
        self.assertFalse(is_error)
        self.assertEqual(
            [result["status"] for result in payload["results"]], ["ok"]
        )
        self.assertEqual(connector.calls, [("host1.example.com", 5.0)])

    def test_a_scoped_host_outside_the_allowlist_is_refused(self) -> None:
        payload, is_error, connector = self._call(
            ["host2.example.com"],
            {},
            allowed="host1.example.com",
        )
        self.assertFalse(is_error)
        result = payload["results"][0]
        self.assertEqual(result["status"], "not_allowed")
        self.assertFalse(result["attempted"])
        self.assertEqual(connector.calls, [])


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

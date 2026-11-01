#!/usr/bin/env python3
"""Tests for scripts/host-stats.py.

No database and no Oracle driver are needed: the script delegates the
connection to mcp/oracle_query.py, and these tests replace that module with a
stub. They cover the three outcomes the wrapper must distinguish:

* samples found        -> result=ok, exit 0;
* no samples in window -> result=empty, exit 0;
* query failed         -> secret-free reason on stderr, exit non-zero.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import importlib.util
import io
import os
import pathlib
import unittest
from typing import Any, Sequence
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "host-stats.py"

spec = importlib.util.spec_from_file_location("host_stats", SCRIPT)
host_stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host_stats)

UTC = datetime.timezone.utc


class FakeCursor:
    def __init__(self, row: Sequence[Any] | None) -> None:
        self.row = row
        self.statements: list[str] = []
        self.bind_sets: list[object] = []

    def execute(self, statement: str, binds: object = None) -> None:
        self.statements.append(statement)
        self.bind_sets.append(binds)

    def fetchone(self) -> Sequence[Any] | None:
        return self.row

    def close(self) -> None:
        pass


class FakeOracle:
    """A stub for the two helpers host-stats.py borrows from the MCP module."""

    class ConfigError(RuntimeError):
        pass

    def __init__(
        self,
        row: Sequence[Any] | None = None,
        connection_error: BaseException | None = None,
        config_error: BaseException | None = None,
    ) -> None:
        self.cursor = FakeCursor(row)
        self.connection_error = connection_error
        self.config_error = config_error
        self.config_calls = 0
        self.connection_configs: list[object] = []

    # The script only uses these attributes; it never checks the module type.
    def _config(self) -> object:
        self.config_calls += 1
        if self.config_error is not None:
            raise self.config_error
        return object()

    @contextlib.contextmanager
    def _connection(self, config: object):
        self.connection_configs.append(config)
        if self.connection_error is not None:
            raise self.connection_error
        yield self.cursor


def run_main(
    oracle: FakeOracle, argv: Sequence[str] | None = None
) -> tuple[int, str, str]:
    argv = list(argv or ["--host", "vortexservers.co.uk"])
    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(host_stats, "_ORACLE", oracle), contextlib.redirect_stdout(
        stdout
    ), contextlib.redirect_stderr(stderr):
        code = host_stats.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class SuccessTests(unittest.TestCase):
    ROW = (
        23,
        datetime.datetime(2026, 11, 1, 10, 1, tzinfo=UTC),
        datetime.datetime(2026, 11, 1, 11, 8, tzinfo=UTC),
        decimal.Decimal("91.25"),
        datetime.datetime(2026, 11, 1, 10, 44, tzinfo=UTC),
        63.5,
        datetime.datetime(2026, 11, 1, 10, 44, tzinfo=UTC),
        44,
        datetime.datetime(2026, 11, 1, 11, 8, tzinfo=UTC),
    )

    def test_success_block_shape(self) -> None:
        oracle = FakeOracle(row=self.ROW)
        code, stdout, stderr = run_main(oracle)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "result=ok\n"
            "host=vortexservers.co.uk\n"
            "requested_minutes=70\n"
            "samples=23\n"
            "first_sampled_at=2026-11-01T10:01:00+00:00\n"
            "last_sampled_at=2026-11-01T11:08:00+00:00\n"
            "max_cpu_percent=91.25\n"
            "max_cpu_at=2026-11-01T10:44:00+00:00\n"
            "max_memory_percent=63.5\n"
            "max_memory_at=2026-11-01T10:44:00+00:00\n"
            "max_disk_percent=44\n"
            "max_disk_at=2026-11-01T11:08:00+00:00\n",
        )

    def test_host_and_window_are_binds_not_sql(self) -> None:
        oracle = FakeOracle(row=self.ROW)
        code, stdout, _ = run_main(
            oracle, ["--host", "vortexservers.co.uk", "--minutes", "15"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(oracle.config_calls, 1)
        self.assertEqual(len(oracle.connection_configs), 1)
        cursor = oracle.cursor
        # The script runs its own query only. SET TRANSACTION READ ONLY lives
        # inside the shared _connection helper, which the stub replaces.
        self.assertEqual(cursor.statements, [host_stats.HOST_STATS_SQL])
        self.assertEqual(
            cursor.bind_sets[0],
            {"host": "vortexservers.co.uk", "minutes": 15},
        )
        self.assertNotIn("vortexservers.co.uk", cursor.statements[0])
        self.assertIn("NOTIFY_RO.HOST_STATS_V", cursor.statements[0])
        self.assertIn("result=ok", stdout)

    def test_minutes_defaults_to_seventy(self) -> None:
        oracle = FakeOracle(row=self.ROW)
        code, stdout, _ = run_main(oracle)
        self.assertEqual(code, 0)
        self.assertEqual(oracle.cursor.bind_sets[0]["minutes"], 70)
        self.assertIn("requested_minutes=70\n", stdout)

    def test_null_peaks_are_still_labelled(self) -> None:
        row = (
            1,
            datetime.datetime(2026, 11, 1, 10, 1, tzinfo=UTC),
            datetime.datetime(2026, 11, 1, 10, 1, tzinfo=UTC),
            None,
            None,
            None,
            None,
            None,
            None,
        )
        code, stdout, _ = run_main(FakeOracle(row=row))
        self.assertEqual(code, 0)
        self.assertIn("max_cpu_percent=null\n", stdout)
        self.assertIn("max_cpu_at=null\n", stdout)


class EmptyWindowTests(unittest.TestCase):
    ROW = (0, None, None, None, None, None, None, None, None)

    def test_empty_window_is_success_and_marked(self) -> None:
        oracle = FakeOracle(row=self.ROW)
        code, stdout, stderr = run_main(oracle)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            stdout,
            "result=empty\n"
            "host=vortexservers.co.uk\n"
            "requested_minutes=70\n"
            "samples=0\n",
        )

    def test_aggregate_row_with_no_table_wide_match_is_not_a_failure(self) -> None:
        # COUNT(*) still returns one row with zero when the filter matches
        # nothing; that is the empty outcome, not an error.
        code, stdout, _ = run_main(FakeOracle(row=self.ROW))
        self.assertEqual(code, 0)
        self.assertIn("result=empty", stdout)
        self.assertNotIn("result=ok", stdout)


class FailureTests(unittest.TestCase):
    def test_ora_error_is_reduced_to_the_code_and_exit_is_nonzero(self) -> None:
        error = Exception(
            "ORA-00942: table or view does not exist "
            "password=hunter2 dsn=secret-db.example user=notify "
            "wallet=/run/secrets/wallet"
        )
        code, stdout, stderr = run_main(FakeOracle(connection_error=error))

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "host-stats: oracle error ORA-00942\n")

    def test_dpy_error_is_reduced_to_the_code(self) -> None:
        error = Exception(
            "DPY-4011: the database or network closed the connection "
            "password=hunter2 dsn=secret-db.example"
        )
        code, _, stderr = run_main(FakeOracle(connection_error=error))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "host-stats: oracle error DPY-4011\n")

    def test_unknown_error_is_generic(self) -> None:
        error = Exception(
            "could not connect: password=hunter2 dsn=secret-db.example "
            "user=notify wallet=/wallet"
        )
        code, _, stderr = run_main(FakeOracle(connection_error=error))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "host-stats: oracle query failed\n")

    def test_config_error_is_reported_without_values(self) -> None:
        error = FakeOracle.ConfigError("Oracle connection is not configured")
        code, stdout, stderr = run_main(FakeOracle(config_error=error))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr, "host-stats: Oracle connection is not configured\n"
        )

    def test_config_error_text_is_redacted(self) -> None:
        env = {
            "ORACLE_USER": "notify_secret_user",
            "ORACLE_PASSWORD": "hunter2",
            "ORACLE_DSN": "secret-db.example",
            "ORACLE_WALLET_DIR": "/run/secrets/wallet",
        }
        error = FakeOracle.ConfigError(
            "connect failed for notify_secret_user with hunter2 at "
            "secret-db.example using /run/secrets/wallet"
        )
        with mock.patch.dict(os.environ, env, clear=False):
            code, stdout, stderr = run_main(FakeOracle(config_error=error))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        for secret in env.values():
            self.assertNotIn(secret, stderr)
        self.assertIn("[redacted]", stderr)

    def test_no_secret_reaches_stdout_or_stderr_on_failure(self) -> None:
        secrets = (
            "hunter2",
            "notify",
            "secret-db.example",
            "mydb_high",
            "/run/secrets/wallet",
            "/wallet",
        )
        error = Exception(
            "ORA-01017: invalid credential password=hunter2 "
            "dsn=secret-db.example user=notify alias=mydb_high "
            "wallet=/run/secrets/wallet wallet=/wallet"
        )
        _, stdout, stderr = run_main(FakeOracle(connection_error=error))
        combined = stdout + stderr
        for secret in secrets:
            self.assertNotIn(secret, combined)

    def test_unexpected_result_shape_is_a_failure(self) -> None:
        code, stdout, stderr = run_main(FakeOracle(row=None))
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "host-stats: unexpected query result\n")


class ArgumentTests(unittest.TestCase):
    def test_help_exits_zero_and_names_the_options(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(
            SystemExit
        ) as caught:
            host_stats.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = stdout.getvalue()
        self.assertIn("--host", text)
        self.assertIn("--minutes", text)

    def test_missing_host_is_a_usage_error(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(
            SystemExit
        ) as caught:
            host_stats.main([])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--host", stderr.getvalue())

    def test_control_characters_in_host_are_rejected(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(
            SystemExit
        ) as caught:
            host_stats.main(["--host", "host\nresult=ok"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("control characters", stderr.getvalue())

    def test_minutes_must_be_positive(self) -> None:
        for value in ("0", "-5", "10081"):
            with self.subTest(value=value):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr), self.assertRaises(
                    SystemExit
                ) as caught:
                    host_stats.main(
                        ["--host", "vortexservers.co.uk", "--minutes", value]
                    )
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("--minutes", stderr.getvalue())


class SharedConnectionTests(unittest.TestCase):
    def test_connection_helpers_come_from_the_mcp_server(self) -> None:
        with mock.patch.object(host_stats, "_ORACLE", None):
            module = host_stats.oracle_module()
        self.assertEqual(
            pathlib.Path(module.__file__).resolve(),
            ROOT / "mcp" / "oracle_query.py",
        )
        self.assertTrue(callable(module._config))
        self.assertTrue(callable(module._connection))

    def test_query_is_a_single_read_only_aggregate(self) -> None:
        sql = host_stats.HOST_STATS_SQL.upper()
        self.assertTrue(sql.lstrip().startswith("SELECT"))
        self.assertIn("NOTIFY_RO.HOST_STATS_V", sql)
        self.assertIn(":HOST", sql)
        self.assertIn(":MINUTES", sql)
        for keyword in ("INSERT", "UPDATE", "DELETE", "MERGE", "COMMIT"):
            self.assertNotIn(keyword, sql)


if __name__ == "__main__":
    unittest.main()

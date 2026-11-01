#!/usr/bin/env python3
"""SQL-guard tests for mcp/oracle_query.py.

No database is used. python-oracledb is imported lazily inside the query
function, so these tests can run on a machine without it.

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
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "oracle_query.py"

spec = importlib.util.spec_from_file_location("oracle_query", SERVER)
oracle_query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle_query)


class PoolTestBase(unittest.TestCase):
    """Start each test with no cached pool from an earlier test."""

    def setUp(self) -> None:
        oracle_query._close_pool()

    def tearDown(self) -> None:
        oracle_query._close_pool()


class GuardTests(unittest.TestCase):
    def assert_rejected(self, sql: object) -> None:
        with self.assertRaises(oracle_query.SqlGuardError):
            oracle_query.prepare_sql(sql)

    # --- allowed ---------------------------------------------------------

    def test_select_passes(self) -> None:
        statement = oracle_query.prepare_sql(
            "SELECT host_label, cpu_avg FROM ROSSMONEY.\"host-samples\""
        )
        self.assertTrue(statement.startswith("SELECT"))
        self.assertTrue(statement.endswith("FETCH FIRST 101 ROWS ONLY"))

    def test_with_passes(self) -> None:
        statement = oracle_query.prepare_sql(
            "WITH recent AS (SELECT * FROM t) SELECT * FROM recent"
        )
        self.assertTrue(statement.startswith("WITH"))
        self.assertIn("FETCH FIRST 101 ROWS ONLY", statement)

    def test_trailing_semicolon_is_stripped(self) -> None:
        statement = oracle_query.prepare_sql("SELECT 1 FROM dual;")
        self.assertNotIn(";", statement)
        self.assertTrue(statement.endswith("FETCH FIRST 101 ROWS ONLY"))

    def test_keyword_inside_string_is_data(self) -> None:
        statement = oracle_query.prepare_sql(
            "SELECT 'drop table x; delete from y' FROM dual"
        )
        self.assertIn("FETCH FIRST", statement)

    def test_keyword_inside_quoted_identifier_is_data(self) -> None:
        statement = oracle_query.prepare_sql('SELECT "delete" FROM t')
        self.assertIn("FETCH FIRST", statement)

    def test_comment_is_ignored(self) -> None:
        statement = oracle_query.prepare_sql(
            "SELECT 1 /* ; DROP TABLE x; */ FROM dual -- trailing"
        )
        self.assertIn("FETCH FIRST", statement)

    def test_row_cap_survives_trailing_line_comment(self) -> None:
        statement = oracle_query.prepare_sql("SELECT 1 FROM dual -- trailing")
        self.assertIn("\nFETCH FIRST 101 ROWS ONLY", statement)

    # --- rejected --------------------------------------------------------

    def test_insert_rejected(self) -> None:
        self.assert_rejected("INSERT INTO t (a) VALUES (1)")

    def test_update_rejected(self) -> None:
        self.assert_rejected("UPDATE t SET a = 1")

    def test_delete_rejected(self) -> None:
        self.assert_rejected("DELETE FROM t")

    def test_drop_rejected(self) -> None:
        self.assert_rejected("DROP TABLE t")

    def test_begin_rejected(self) -> None:
        self.assert_rejected("BEGIN NULL; END;")

    def test_with_delete_rejected(self) -> None:
        self.assert_rejected("WITH x AS (SELECT 1 FROM dual) DELETE FROM t")

    def test_select_for_update_rejected(self) -> None:
        self.assert_rejected("SELECT * FROM t FOR UPDATE")

    def test_plsql_package_rejected(self) -> None:
        self.assert_rejected("SELECT DBMS_CLOUD.GET_OBJECT() FROM dual")

    def test_two_statements_rejected(self) -> None:
        self.assert_rejected("SELECT 1 FROM dual; SELECT 2 FROM dual")

    def test_extra_semicolon_rejected(self) -> None:
        self.assert_rejected("SELECT 1 FROM dual;;")

    def test_trailing_junk_rejected(self) -> None:
        self.assert_rejected("SELECT 1 FROM dual; nonsense")

    def test_only_junk_rejected(self) -> None:
        self.assert_rejected("nonsense")

    def test_empty_rejected(self) -> None:
        self.assert_rejected("")
        self.assert_rejected("   ")
        self.assert_rejected(";")

    def test_non_string_rejected(self) -> None:
        self.assert_rejected(None)
        self.assert_rejected(42)
        self.assert_rejected(["SELECT 1 FROM dual"])

    def test_oversized_sql_rejected(self) -> None:
        self.assert_rejected("SELECT 1 FROM dual WHERE a = '"
                             + ("x" * (oracle_query.MAX_SQL_CHARS + 1))
                             + "'")

    # --- row cap ---------------------------------------------------------

    def test_row_cap_applied(self) -> None:
        self.assertEqual(oracle_query.MAX_ROWS, 100)
        statement = oracle_query.prepare_sql("SELECT * FROM t")
        self.assertTrue(statement.endswith("FETCH FIRST 101 ROWS ONLY"))

    def test_existing_fetch_not_duplicated(self) -> None:
        statement = oracle_query.prepare_sql(
            "SELECT * FROM t FETCH FIRST 5 ROWS ONLY"
        )
        self.assertEqual(statement.upper().count("FETCH FIRST"), 1)

    def test_existing_rownum_not_duplicated(self) -> None:
        statement = oracle_query.prepare_sql(
            "SELECT * FROM t WHERE ROWNUM <= 5"
        )
        self.assertNotIn("FETCH FIRST", statement)

    # --- response cap ----------------------------------------------------

    def test_response_size_cap_shrinks_rows(self) -> None:
        columns = ["payload"]
        rows = [["x" * 4096] for _ in range(oracle_query.MAX_ROWS)]
        text = oracle_query._fit_response(columns, rows, False)
        payload = json.loads(text)
        self.assertLessEqual(
            len(text.encode("utf-8")), oracle_query.MAX_RESPONSE_BYTES
        )
        self.assertTrue(payload["truncated"])
        self.assertLess(payload["row_count"], oracle_query.MAX_ROWS)
        self.assertEqual(len(rows), oracle_query.MAX_ROWS)


class McpProtocolTests(unittest.TestCase):
    """Small checks that do not require a database or a subprocess."""

    def test_tool_schema(self) -> None:
        self.assertEqual(oracle_query.QUERY_TOOL["name"], "query")
        self.assertEqual(
            oracle_query.QUERY_TOOL["inputSchema"]["required"], ["sql"]
        )

    def test_guard_error_is_a_tool_error(self) -> None:
        text, is_error = oracle_query.call_query("DROP TABLE t")
        self.assertTrue(is_error)
        self.assertIn('"error"', text)
        self.assertNotIn("DROP", text)

    def test_missing_config_has_no_secret(self) -> None:
        # With no environment at all, the query fails before connecting and the
        # error names no value.
        text, is_error = oracle_query.call_query("SELECT 1 FROM dual")
        self.assertTrue(is_error)
        self.assertIn("not configured", text)

    def test_empty_wallet_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as wallet:
            env = {
                "ORACLE_USER": "user",
                "ORACLE_PASSWORD": "password",
                "ORACLE_DSN": "dsn",
                "ORACLE_WALLET_DIR": wallet,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(oracle_query.ConfigError) as caught:
                    oracle_query._config()
        self.assertIn("ewallet.pem", str(caught.exception))
        self.assertIn("cwallet.sso", str(caught.exception))

    def test_pem_only_wallet_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as wallet:
            pathlib.Path(wallet, "ewallet.pem").write_bytes(b"dummy")
            env = {
                "ORACLE_USER": "user",
                "ORACLE_PASSWORD": "password",
                "ORACLE_DSN": "dsn",
                "ORACLE_WALLET_DIR": wallet,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = oracle_query._config()
            self.assertEqual(config.wallet, wallet)

    def test_tnsnames_only_wallet_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as wallet:
            pathlib.Path(wallet, "tnsnames.ora").write_text(
                "mydb = (DESCRIPTION=)\n"
            )
            env = {
                "ORACLE_USER": "user",
                "ORACLE_PASSWORD": "password",
                "ORACLE_DSN": "dsn",
                "ORACLE_WALLET_DIR": wallet,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(oracle_query.ConfigError) as caught:
                    oracle_query._config()
        self.assertIn("ewallet.pem", str(caught.exception))

    def test_wallet_directory_with_cwallet_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as wallet:
            pathlib.Path(wallet, "cwallet.sso").write_bytes(b"dummy")
            env = {
                "ORACLE_USER": "user",
                "ORACLE_PASSWORD": "password",
                "ORACLE_DSN": "dsn",
                "ORACLE_WALLET_DIR": wallet,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                config = oracle_query._config()
            self.assertEqual(config.wallet, wallet)

    def test_pem_only_wallet_is_passed_through(self) -> None:
        with tempfile.TemporaryDirectory() as wallet:
            pathlib.Path(wallet, "ewallet.pem").write_bytes(b"dummy")
            with oracle_query._wallet_for_connection(wallet) as driver_dir:
                self.assertEqual(driver_dir, wallet)

    def test_java_wallet_files_beside_pem_are_not_passed_to_the_driver(self) -> None:
        # cwallet.sso / ewallet.p12 next to ewallet.pem can make the thin
        # driver prompt for a PEM pass phrase. Only the thin-mode files should
        # reach the driver in that case.
        with tempfile.TemporaryDirectory() as wallet:
            for name in (
                "tnsnames.ora",
                "sqlnet.ora",
                "ewallet.pem",
                "cwallet.sso",
                "ewallet.p12",
            ):
                pathlib.Path(wallet, name).write_bytes(b"dummy")
            with oracle_query._wallet_for_connection(wallet) as driver_dir:
                self.assertNotEqual(driver_dir, wallet)
                self.assertEqual(
                    set(os.listdir(driver_dir)),
                    {"tnsnames.ora", "sqlnet.ora", "ewallet.pem"},
                )
                self.assertTrue(
                    pathlib.Path(driver_dir, "ewallet.pem").is_symlink()
                )


class QueryExecutionTests(PoolTestBase):
    """Exercise the hard row cap and JSON shape with a fake driver."""

    def test_hard_cap_and_column_names(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.description = [("HOST",), ("CPU",)]
                self.statements: list[str] = []
                self.fetch_size = 0

            def execute(self, statement: str) -> None:
                self.statements.append(statement)

            def fetchmany(self, size: int) -> list[tuple[str, int]]:
                self.fetch_size = size
                return [(f"host-{i}", i) for i in range(size)]

            def close(self) -> None:
                pass

        class FakeConnection:
            def __init__(self) -> None:
                self.call_timeout = None
                self.rolled_back = False
                self.closed = False
                self.cursor_instance = FakeCursor()

            def cursor(self) -> FakeCursor:
                return self.cursor_instance

            def rollback(self) -> None:
                self.rolled_back = True

            def close(self) -> None:
                self.closed = True

        class FakePool:
            def __init__(self) -> None:
                self.connection = FakeConnection()
                self.dropped = []
                self.closed = False

            def acquire(self) -> FakeConnection:
                return self.connection

            def drop(self, connection: FakeConnection) -> None:
                self.dropped.append(connection)

            def close(self, force: bool = False) -> None:
                self.closed = True

        fake = types.ModuleType("oracledb")
        fake.defaults = types.SimpleNamespace(fetch_lobs=True)
        fake.POOL_GETMODE_TIMEDWAIT = object()
        pool = FakePool()

        def create_pool(**kwargs: object) -> FakePool:
            fake.pool_kwargs = kwargs
            return pool

        fake.create_pool = create_pool

        config = oracle_query._Config("user", "password", "dsn", "/wallet")
        with mock.patch.dict(sys.modules, {"oracledb": fake}), mock.patch.object(
            oracle_query, "_config", return_value=config
        ):
            text = oracle_query._run_query("SELECT host, cpu FROM t")

        payload = json.loads(text)
        self.assertEqual(payload["columns"], ["HOST", "CPU"])
        self.assertEqual(payload["row_count"], oracle_query.MAX_ROWS)
        self.assertEqual(len(payload["rows"]), oracle_query.MAX_ROWS)
        self.assertTrue(payload["truncated"])
        connection = pool.connection
        self.assertEqual(connection.call_timeout, oracle_query.CALL_TIMEOUT_MS)
        self.assertEqual(
            connection.cursor_instance.statements[0], "SET TRANSACTION READ ONLY"
        )
        self.assertEqual(
            connection.cursor_instance.fetch_size, oracle_query.MAX_ROWS + 1
        )
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        # The password is used for the pool but never returned.
        self.assertEqual(fake.pool_kwargs["password"], "password")
        self.assertNotIn("password", text)
        self.assertNotIn("dsn", text)
        self.assertNotIn("/wallet", text)


class WalletConnectTests(PoolTestBase):
    """The thin-mode connect must never ask for a wallet passphrase."""

    def _connect(self, wallet: str) -> tuple[dict[str, object], str]:
        class FakeCursor:
            description = [("N",)]

            def execute(self, statement: str) -> None:
                pass

            def fetchmany(self, size: int) -> list[tuple[int]]:
                return [(1,)]

            def close(self) -> None:
                pass

        class FakeConnection:
            call_timeout = None

            def __init__(self) -> None:
                self.cursor_instance = FakeCursor()

            def cursor(self) -> FakeCursor:
                return self.cursor_instance

            def rollback(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakePool:
            def __init__(self) -> None:
                self.connection = FakeConnection()

            def acquire(self) -> FakeConnection:
                # A passphrase prompt would read fd 0. It must be EOF, never
                # the JSON-RPC stream that is the server's stdin.
                captured["stdin_read"] = os.read(0, 1)
                return self.connection

            def drop(self, connection: FakeConnection) -> None:
                pass

            def close(self, force: bool = False) -> None:
                pass

        fake = types.ModuleType("oracledb")
        fake.defaults = types.SimpleNamespace(fetch_lobs=True)
        fake.POOL_GETMODE_TIMEDWAIT = object()
        captured: dict[str, object] = {}
        pool = FakePool()

        def create_pool(**kwargs: object) -> FakePool:
            captured.update(kwargs)
            return pool

        fake.create_pool = create_pool

        config = oracle_query._Config("user", "password", "dsn", wallet)
        with mock.patch.dict(sys.modules, {"oracledb": fake}), mock.patch.object(
            oracle_query, "_config", return_value=config
        ):
            text = oracle_query._run_query("SELECT 1 FROM dual")
        return captured, text

    def test_wallet_location_is_never_passed(self) -> None:
        # Passing wallet_location without a password makes the thin driver
        # prompt for a PEM pass phrase on stdin. config_dir alone is enough.
        captured, _ = self._connect("/wallet")
        self.assertNotIn("wallet_location", captured)
        self.assertNotIn("wallet_password", captured)
        self.assertEqual(captured["config_dir"], "/wallet")

    def test_wallet_password_env_does_not_enable_the_prompt_path(self) -> None:
        with mock.patch.dict(
            os.environ, {"ORACLE_WALLET_PASSWORD": "secret"}, clear=False
        ):
            captured, _ = self._connect("/wallet")
        self.assertNotIn("wallet_location", captured)
        self.assertNotIn("wallet_password", captured)

    def test_stdin_is_detached_while_the_driver_connects(self) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"x")
        saved = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            captured, _ = self._connect("/wallet")
            self.assertEqual(captured["stdin_read"], b"")
            # The unread protocol byte survives the connection.
            self.assertEqual(os.read(0, 1), b"x")
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(read_fd)
            os.close(write_fd)

    def test_stdin_guard_hides_and_restores_the_stream(self) -> None:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"x")
        saved = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            with oracle_query._stdin_guarded():
                self.assertEqual(os.read(0, 1), b"")
                self.assertEqual(sys.stdin.read(), "")
            self.assertEqual(os.read(0, 1), b"x")
        finally:
            os.dup2(saved, 0)
            os.close(saved)
            os.close(read_fd)
            os.close(write_fd)


class SearchInputTests(unittest.TestCase):
    """Input handling for search_content, without a database."""

    def test_limit_defaults_to_eight(self) -> None:
        self.assertEqual(oracle_query.DEFAULT_SEARCH_LIMIT, 8)
        self.assertEqual(oracle_query.clamp_limit(None), 8)

    def test_limit_is_clamped_to_the_hard_ceiling(self) -> None:
        self.assertEqual(oracle_query.MAX_SEARCH_LIMIT, 20)
        self.assertEqual(oracle_query.clamp_limit(20), 20)
        self.assertEqual(oracle_query.clamp_limit(21), 20)
        self.assertEqual(oracle_query.clamp_limit(1000), 20)

    def test_limit_has_a_floor_of_one(self) -> None:
        self.assertEqual(oracle_query.clamp_limit(0), 1)
        self.assertEqual(oracle_query.clamp_limit(-5), 1)

    def test_limit_accepts_integers_and_numeric_strings(self) -> None:
        self.assertEqual(oracle_query.clamp_limit(5), 5)
        self.assertEqual(oracle_query.clamp_limit("12"), 12)

    def test_unusable_limit_falls_back_to_the_default(self) -> None:
        self.assertEqual(oracle_query.clamp_limit("many"), 8)
        self.assertEqual(oracle_query.clamp_limit(object()), 8)
        self.assertEqual(oracle_query.clamp_limit(True), 8)

    def test_query_is_trimmed(self) -> None:
        self.assertEqual(
            oracle_query.prepare_search_query("  storage servers  "),
            "storage servers",
        )

    def test_empty_or_non_string_query_is_rejected(self) -> None:
        for query in (None, 42, ["storage"], "", "   "):
            with self.subTest(query=query):
                with self.assertRaises(oracle_query.SearchInputError):
                    oracle_query.prepare_search_query(query)

    def test_oversized_query_is_rejected_without_echoing_it(self) -> None:
        query = "x" * (oracle_query.MAX_SEARCH_QUERY_CHARS + 1)
        with self.assertRaises(oracle_query.SearchInputError) as caught:
            oracle_query.prepare_search_query(query)
        self.assertNotIn(query, str(caught.exception))

    def test_tool_schema(self) -> None:
        tool = oracle_query.SEARCH_TOOL
        self.assertEqual(tool["name"], "search_content")
        self.assertEqual(tool["inputSchema"]["required"], ["query"])
        limit = tool["inputSchema"]["properties"]["limit"]
        self.assertEqual(limit["default"], oracle_query.DEFAULT_SEARCH_LIMIT)
        self.assertEqual(limit["minimum"], 1)
        self.assertEqual(limit["maximum"], oracle_query.MAX_SEARCH_LIMIT)

    def test_tools_list_exposes_both_tools(self) -> None:
        with mock.patch.object(oracle_query, "_write") as write:
            oracle_query._handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
        message = write.call_args.args[0]
        names = [tool["name"] for tool in message["result"]["tools"]]
        self.assertEqual(names, ["query", "search_content"])

    def test_query_text_is_never_the_sql(self) -> None:
        # The search statement is fixed and uses :query as a bind. The text the
        # model supplies must not be able to change the statement.
        dangerous = "storage'); DROP TABLE ROSSMONEY.CONTENT_EMBEDDINGS; --"
        self.assertNotIn(dangerous, oracle_query.SEARCH_SQL)
        self.assertIn("ADMIN.EMBED_TEXT(:query)", oracle_query.SEARCH_SQL)
        self.assertIn("FETCH FIRST :limit ROWS ONLY", oracle_query.SEARCH_SQL)
        self.assertIn("VECTOR_DISTANCE", oracle_query.SEARCH_SQL)
        self.assertIn("COSINE", oracle_query.SEARCH_SQL)


class SearchExecutionTests(PoolTestBase):
    """Exercise search_content with a fake driver: binds, caps and errors."""

    COLUMNS = [
        ("KIND",),
        ("SLUG",),
        ("TITLE",),
        ("SECTION",),
        ("CHUNK",),
        ("SOURCE_UPDATED",),
        ("DISTANCE",),
    ]

    def _run(
        self,
        query: object = "storage servers",
        limit: object = None,
        rows: list[tuple[object, ...]] | None = None,
        connect_error: BaseException | None = None,
    ) -> tuple[str, bool, object]:
        # Each call uses its own stub driver, so start from a clean pool.
        oracle_query._close_pool()
        if rows is None:
            rows = [
                (
                    "experience",
                    "cv",
                    "Platform support",
                    "summary",
                    "short chunk",
                    "2026-01-01T00:00:00",
                    0.666,
                )
            ]

        class FakeCursor:
            description = self.COLUMNS

            def __init__(self) -> None:
                self.statements: list[str] = []
                self.bind_sets: list[object] = []
                self.fetch_size = 0

            def execute(self, statement: str, binds: object = None) -> None:
                self.statements.append(statement)
                self.bind_sets.append(binds)

            def fetchmany(self, size: int) -> list[tuple[object, ...]]:
                self.fetch_size = size
                return rows[:size]

            def close(self) -> None:
                pass

        class FakeConnection:
            def __init__(self) -> None:
                self.call_timeout = None
                self.rolled_back = False
                self.closed = False
                self.cursor_instance = FakeCursor()

            def cursor(self) -> FakeCursor:
                return self.cursor_instance

            def rollback(self) -> None:
                self.rolled_back = True

            def close(self) -> None:
                self.closed = True

        class FakePool:
            def __init__(self) -> None:
                self.connection = FakeConnection()
                self.dropped = []

            def acquire(self) -> FakeConnection:
                if connect_error is not None:
                    raise connect_error
                return self.connection

            def drop(self, connection: FakeConnection) -> None:
                self.dropped.append(connection)

            def close(self, force: bool = False) -> None:
                pass

        fake = types.ModuleType("oracledb")
        fake.defaults = types.SimpleNamespace(fetch_lobs=True)
        fake.POOL_GETMODE_TIMEDWAIT = object()
        pool = FakePool()

        def create_pool(**kwargs: object) -> FakePool:
            fake.pool_kwargs = kwargs
            return pool

        fake.create_pool = create_pool

        config = oracle_query._Config("user", "password", "dsn", "/wallet")
        with mock.patch.dict(sys.modules, {"oracledb": fake}), mock.patch.object(
            oracle_query, "_config", return_value=config
        ):
            text, is_error = oracle_query.call_search_content(query, limit)
        return text, is_error, pool.connection

    def test_query_and_limit_are_bind_variables(self) -> None:
        query = "storage'); DROP TABLE ROSSMONEY.CONTENT_EMBEDDINGS; --"
        text, is_error, connection = self._run(query=query, limit=5)
        self.assertFalse(is_error, text)
        cursor = connection.cursor_instance
        self.assertEqual(
            cursor.statements[0], "SET TRANSACTION READ ONLY"
        )
        self.assertEqual(cursor.statements[1], oracle_query.SEARCH_SQL)
        self.assertNotIn(query, cursor.statements[1])
        self.assertEqual(cursor.bind_sets[1], {"query": query, "limit": 5})
        self.assertEqual(cursor.fetch_size, 5)
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)

    def test_limit_is_clamped_before_it_reaches_the_database(self) -> None:
        _, _, connection = self._run(limit=500)
        self.assertEqual(
            connection.cursor_instance.bind_sets[1]["limit"],
            oracle_query.MAX_SEARCH_LIMIT,
        )
        _, _, connection = self._run(limit=0)
        self.assertEqual(connection.cursor_instance.bind_sets[1]["limit"], 1)
        _, _, connection = self._run(limit="many")
        self.assertEqual(
            connection.cursor_instance.bind_sets[1]["limit"],
            oracle_query.DEFAULT_SEARCH_LIMIT,
        )

    def test_chunks_are_truncated(self) -> None:
        rows = [
            (
                "experience",
                "cv",
                "Platform support",
                "summary",
                "x" * 4000,
                "2026-01-01T00:00:00",
                0.666,
            ),
            (
                "page",
                "building-dependable-systems",
                "Building dependable systems",
                "focus",
                "short chunk",
                "2026-01-01T00:00:00",
                0.747,
            ),
        ]
        text, is_error, _ = self._run(rows=rows)
        self.assertFalse(is_error, text)
        payload = json.loads(text)
        self.assertEqual(payload["count"], 2)
        self.assertEqual(
            len(payload["results"][0]["chunk"]),
            oracle_query.MAX_CHUNK_CHARS,
        )
        self.assertTrue(payload["results"][0]["chunk_truncated"])
        self.assertEqual(payload["results"][1]["chunk"], "short chunk")
        self.assertFalse(payload["results"][1]["chunk_truncated"])
        self.assertIn("truncated", payload["note"])
        self.assertEqual(payload["results"][0]["title"], "Platform support")
        self.assertEqual(payload["results"][0]["distance"], 0.666)

    def test_response_size_cap_applies_to_search_results(self) -> None:
        results = [
            {
                "kind": "experience",
                "title": f"title-{index}",
                "slug": "cv",
                "section": "summary",
                "chunk": "x" * 8192,
                "chunk_truncated": True,
                "source_updated": "2026-01-01T00:00:00",
                "distance": 0.5,
            }
            for index in range(oracle_query.MAX_SEARCH_LIMIT)
        ]
        text = oracle_query._fit_search_response(results, 20)
        payload = json.loads(text)
        self.assertLessEqual(
            len(text.encode("utf-8")), oracle_query.MAX_RESPONSE_BYTES
        )
        self.assertTrue(payload["truncated"])
        self.assertLess(payload["count"], len(results))
        self.assertEqual(len(results), oracle_query.MAX_SEARCH_LIMIT)

    def test_errors_are_sanitised(self) -> None:
        text, is_error, _ = self._run(
            connect_error=Exception(
                "ORA-01017: invalid credential password=hunter2 "
                "dsn=secret-db.example wallet=/wallet"
            )
        )
        self.assertTrue(is_error)
        self.assertEqual(
            json.loads(text),
            {
                "error": "oracle error ORA-01017",
                "code": "ORA-01017",
                "retryable": False,
            },
        )
        for secret in ("hunter2", "secret-db", "dsn", "/wallet"):
            self.assertNotIn(secret, text)

    def test_unknown_driver_error_is_generic(self) -> None:
        text, is_error, _ = self._run(
            connect_error=Exception("password=hunter2 at secret-db")
        )
        self.assertTrue(is_error)
        self.assertEqual(
            json.loads(text),
            {
                "error": "oracle query failed",
                "code": None,
                "retryable": False,
            },
        )
        self.assertNotIn("hunter2", text)
        self.assertNotIn("secret-db", text)

    def test_missing_config_does_not_echo_the_query(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            text, is_error = oracle_query.call_search_content(
                "my private query"
            )
        self.assertTrue(is_error)
        self.assertIn("not configured", text)
        self.assertNotIn("my private query", text)

    def test_invalid_input_is_a_tool_error(self) -> None:
        text, is_error = oracle_query.call_search_content("   ")
        self.assertTrue(is_error)
        self.assertEqual(
            json.loads(text),
            {"error": "query is empty", "code": None, "retryable": False},
        )

        text, is_error = oracle_query.call_search_content(None)
        self.assertTrue(is_error)
        self.assertEqual(
            json.loads(text),
            {
                "error": "query must be a string",
                "code": None,
                "retryable": False,
            },
        )


class ContentDisabledTests(unittest.TestCase):
    """A target with allow_content false must block search_content outright."""

    def test_search_content_refuses_when_the_environment_disables_it(self) -> None:
        with mock.patch.dict(
            os.environ, {oracle_query.CONTENT_DISABLED_ENV: "1"}, clear=True
        ):
            text, is_error = oracle_query.call_search_content("storage servers")
        self.assertTrue(is_error)
        self.assertEqual(
            json.loads(text),
            {
                "error": oracle_query.CONTENT_DISABLED_ERROR,
                "code": None,
                "retryable": False,
            },
        )
        self.assertNotIn("storage servers", text)

    def test_tools_call_reports_the_block(self) -> None:
        with mock.patch.dict(
            os.environ, {oracle_query.CONTENT_DISABLED_ENV: "1"}, clear=True
        ), mock.patch.object(oracle_query, "_write") as write:
            oracle_query._handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "search_content",
                        "arguments": {"query": "the CV"},
                    },
                }
            )
        message = write.call_args.args[0]
        self.assertTrue(message["result"]["isError"])
        self.assertIn(
            oracle_query.CONTENT_DISABLED_ERROR,
            message["result"]["content"][0]["text"],
        )

    def test_search_content_is_unchanged_without_the_flag(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            text, is_error = oracle_query.call_search_content("storage servers")
        # It gets past the content block and fails on the missing Oracle config,
        # exactly as it did before this guard existed.
        self.assertTrue(is_error)
        self.assertIn("not configured", text)


class ErrorClassificationTests(unittest.TestCase):
    """The agent must be able to tell a blip from a permanent failure."""

    def test_transient_codes_are_retryable(self) -> None:
        for code in sorted(oracle_query.TRANSIENT_ERROR_CODES):
            with self.subTest(code=code):
                exc = Exception(f"{code}: connection lost")
                self.assertEqual(oracle_query.error_code(exc), code)
                self.assertTrue(oracle_query.is_retryable(exc))
                message, parsed_code, retryable = oracle_query._safe_error(exc)
                self.assertEqual(message, f"oracle error {code}")
                self.assertEqual(parsed_code, code)
                self.assertTrue(retryable)

    def test_permanent_codes_are_not_retryable(self) -> None:
        for code in ("ORA-00933", "ORA-00942", "ORA-01031", "ORA-01017"):
            with self.subTest(code=code):
                exc = Exception(f"{code}: permanent failure")
                self.assertEqual(oracle_query.error_code(exc), code)
                self.assertFalse(oracle_query.is_retryable(exc))
                payload = json.loads(
                    oracle_query._error_payload(
                        *oracle_query._safe_error(exc)
                    )
                )
                self.assertEqual(payload["code"], code)
                self.assertFalse(payload["retryable"])

    def test_unknown_error_has_no_code_and_is_not_retryable(self) -> None:
        exc = Exception("password=hunter2 at secret-db")
        self.assertIsNone(oracle_query.error_code(exc))
        self.assertFalse(oracle_query.is_retryable(exc))
        message, code, retryable = oracle_query._safe_error(exc)
        self.assertEqual(message, "oracle query failed")
        self.assertIsNone(code)
        self.assertFalse(retryable)


class FailingDriver:
    """A tiny oracledb stub that fails on pool acquire or execute."""

    def __init__(self, failures: list[str], fail_on: str) -> None:
        self.failures = list(failures)
        self.fail_on = fail_on
        self.connects = 0
        self.acquires = 0
        self.executes = 0
        self.create_pool_calls = 0
        self.pool_kwargs: dict[str, object] = {}
        self.pool: FakePool | None = None
        self.connection_class = FakeConnection
        self.getmode = object()

    def module(self) -> types.ModuleType:
        fake = types.ModuleType("oracledb")
        fake.defaults = types.SimpleNamespace(fetch_lobs=True)
        fake.POOL_GETMODE_TIMEDWAIT = self.getmode
        fake.create_pool = self.create_pool
        return fake

    def create_pool(self, **kwargs: object) -> "FakePool":
        self.create_pool_calls += 1
        self.pool_kwargs = kwargs
        self.pool = FakePool(self)
        return self.pool


class FakeCursor:
    description = [("N",)]

    def __init__(self, driver: FailingDriver) -> None:
        self.driver = driver

    def execute(self, statement: str, binds: object = None) -> None:
        if statement == "SET TRANSACTION READ ONLY":
            return
        self.driver.executes += 1
        if self.driver.fail_on == "execute" and self.driver.failures:
            raise Exception(self.driver.failures.pop(0))

    def fetchmany(self, size: int) -> list[tuple[int]]:
        return [(1,)]

    def fetchone(self) -> tuple[int]:
        return (1,)

    def close(self) -> None:
        pass


class FakePool:
    """A pool that remembers what it handed out and what was dropped."""

    def __init__(self, driver: FailingDriver) -> None:
        self.driver = driver
        self.created = 0
        self.dropped: list[FakeConnection] = []
        self.closed = False
        self.available: list[FakeConnection] = []

    def acquire(self) -> "FakeConnection":
        self.driver.connects += 1
        if self.driver.fail_on == "connect" and self.driver.failures:
            raise Exception(self.driver.failures.pop(0))
        self.driver.acquires += 1
        if self.available:
            return self.available.pop()
        self.created += 1
        return self.driver.connection_class(self.driver, self)

    def release(self, connection: "FakeConnection") -> None:
        self.available.append(connection)

    def drop(self, connection: "FakeConnection") -> None:
        connection.dropped = True
        self.dropped.append(connection)

    def close(self, force: bool = False) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, driver: FailingDriver, pool: FakePool) -> None:
        self.driver = driver
        self.pool = pool
        self.call_timeout = None
        self.closed = False
        self.dropped = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.driver)

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.pool.release(self)


class RetryTests(PoolTestBase):
    """A transient failure is retried once; a permanent one is not."""

    CONFIG = oracle_query._Config("user", "password", "dsn", "/wallet")

    def _call_query(
        self, failures: list[str], fail_on: str
    ) -> tuple[str, bool, FailingDriver, mock.Mock]:
        driver = FailingDriver(failures, fail_on)
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=self.CONFIG
        ), mock.patch.object(oracle_query.time, "sleep") as sleep:
            text, is_error = oracle_query.call_query("SELECT 1 FROM dual")
        return text, is_error, driver, sleep

    def assert_bounded_delay(self, sleep: mock.Mock) -> None:
        sleep.assert_called_once()
        delay = sleep.call_args.args[0]
        self.assertGreaterEqual(delay, oracle_query.RETRY_DELAY_MIN_SECONDS)
        self.assertLessEqual(delay, oracle_query.RETRY_DELAY_MAX_SECONDS)

    def test_transient_connect_failure_is_retried_once(self) -> None:
        text, is_error, driver, sleep = self._call_query(
            ["ORA-03113: end-of-file on communication channel"], "connect"
        )
        self.assertFalse(is_error, text)
        self.assertEqual(driver.connects, 2)
        self.assert_bounded_delay(sleep)

    def test_transient_execute_failure_is_retried_once(self) -> None:
        text, is_error, driver, sleep = self._call_query(
            ["ORA-03114: not connected to ORACLE"], "execute"
        )
        self.assertFalse(is_error, text)
        self.assertEqual(driver.connects, 2)
        self.assert_bounded_delay(sleep)

    def test_permanent_failure_is_not_retried(self) -> None:
        for code in ("ORA-00933", "ORA-00942", "ORA-01031"):
            with self.subTest(code=code):
                oracle_query._close_pool()
                text, is_error, driver, sleep = self._call_query(
                    [f"{code}: permanent"], "connect"
                )
                payload = json.loads(text)
                self.assertTrue(is_error)
                self.assertEqual(payload["code"], code)
                self.assertFalse(payload["retryable"])
                self.assertEqual(driver.connects, 1)
                sleep.assert_not_called()

    def test_two_transient_failures_are_reported_as_retryable(self) -> None:
        text, is_error, driver, sleep = self._call_query(
            ["ORA-03113: first blip", "DPY-6005: second blip"], "connect"
        )
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertEqual(payload["code"], "DPY-6005")
        self.assertTrue(payload["retryable"])
        self.assertEqual(driver.connects, oracle_query.RETRY_ATTEMPTS)
        self.assert_bounded_delay(sleep)

    def test_search_uses_the_same_retry_policy(self) -> None:
        driver = FailingDriver(["ORA-12541: no listener"], "connect")
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=self.CONFIG
        ), mock.patch.object(oracle_query.time, "sleep") as sleep:
            text, is_error = oracle_query.call_search_content("storage")
        self.assertFalse(is_error, text)
        self.assertEqual(driver.connects, 2)
        self.assert_bounded_delay(sleep)


class PoolTests(PoolTestBase):
    """Connection reuse, dead-session recovery and clean shutdown."""

    CONFIG = RetryTests.CONFIG

    def _run_query(
        self, driver: FailingDriver
    ) -> tuple[str, bool, mock.Mock]:
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=self.CONFIG
        ), mock.patch.object(oracle_query.time, "sleep") as sleep:
            text, is_error = oracle_query.call_query("SELECT 1 FROM dual")
        return text, is_error, sleep

    def test_pool_is_not_created_at_import_and_is_reused(self) -> None:
        self.assertIsNone(oracle_query._pool_state)
        driver = FailingDriver([], "connect")
        text, is_error, _ = self._run_query(driver)
        self.assertFalse(is_error, text)
        text, is_error, _ = self._run_query(driver)
        self.assertFalse(is_error, text)
        self.assertEqual(driver.create_pool_calls, 1)
        self.assertEqual(driver.pool.created, 1)
        self.assertEqual(driver.acquires, 2)

    def test_pool_settings_suit_a_short_run(self) -> None:
        driver = FailingDriver([], "connect")
        self._run_query(driver)
        kwargs = driver.pool_kwargs
        self.assertEqual(kwargs["min"], 0)
        self.assertEqual(kwargs["max"], 2)
        self.assertEqual(kwargs["increment"], 1)
        self.assertEqual(kwargs["getmode"], driver.getmode)
        self.assertEqual(kwargs["wait_timeout"], oracle_query.POOL_WAIT_TIMEOUT_MS)
        self.assertEqual(
            kwargs["ping_interval"], oracle_query.POOL_PING_INTERVAL_SECONDS
        )
        self.assertEqual(kwargs["timeout"], oracle_query.POOL_IDLE_TIMEOUT_SECONDS)
        self.assertEqual(
            kwargs["max_lifetime_session"],
            oracle_query.POOL_MAX_LIFETIME_SECONDS,
        )
        self.assertEqual(
            kwargs["tcp_connect_timeout"], oracle_query.CONNECT_TIMEOUT_SECONDS
        )
        self.assertNotIn("wallet_location", kwargs)

    def test_dead_session_is_dropped_and_retried_on_a_fresh_connection(
        self,
    ) -> None:
        driver = FailingDriver(["ORA-03114: not connected to ORACLE"], "execute")
        text, is_error, sleep = self._run_query(driver)
        self.assertFalse(is_error, text)
        self.assertEqual(driver.connects, 2)
        self.assertEqual(driver.pool.created, 2)
        self.assertEqual(len(driver.pool.dropped), 1)
        self.assertTrue(driver.pool.dropped[0].dropped)
        sleep.assert_called_once()
        # The retry ran on the newly created connection, not the dead one.
        self.assertNotIn(driver.pool.dropped[0], driver.pool.available)

    def test_dead_session_codes_are_the_transient_ones(self) -> None:
        self.assertEqual(
            oracle_query.DEAD_SESSION_CODES, frozenset({"ORA-03113", "ORA-03114"})
        )
        for code in oracle_query.DEAD_SESSION_CODES:
            with self.subTest(code=code):
                self.assertTrue(
                    oracle_query.is_dead_session(Exception(f"{code}: gone"))
                )
                self.assertTrue(
                    oracle_query.is_retryable(Exception(f"{code}: gone"))
                )

    def test_retry_delay_is_bounded_and_randomised(self) -> None:
        driver = FailingDriver(
            ["ORA-03113: first blip", "ORA-03113: second blip"], "connect"
        )
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=self.CONFIG
        ), mock.patch.object(
            oracle_query.random, "uniform", return_value=0.37
        ) as uniform, mock.patch.object(
            oracle_query.time, "sleep"
        ) as sleep:
            text, is_error = oracle_query.call_query("SELECT 1 FROM dual")
        payload = json.loads(text)
        self.assertTrue(is_error)
        self.assertTrue(payload["retryable"])
        self.assertEqual(driver.connects, oracle_query.RETRY_ATTEMPTS)
        uniform.assert_called_once_with(
            oracle_query.RETRY_DELAY_MIN_SECONDS,
            oracle_query.RETRY_DELAY_MAX_SECONDS,
        )
        sleep.assert_called_once_with(0.37)

    def test_permanent_failure_releases_the_connection_for_reuse(self) -> None:
        driver = FailingDriver(
            ["ORA-00942: table or view does not exist"], "execute"
        )
        text, is_error, sleep = self._run_query(driver)
        self.assertTrue(is_error)
        sleep.assert_not_called()
        self.assertEqual(driver.pool.created, 1)
        self.assertEqual(driver.pool.dropped, [])
        # The session was returned to the pool, so the next query reuses it.
        text, is_error, _ = self._run_query(driver)
        self.assertFalse(is_error, text)
        self.assertEqual(driver.pool.created, 1)

    def test_selftest_closes_the_pool(self) -> None:
        driver = FailingDriver([], "connect")
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=self.CONFIG
        ), contextlib.redirect_stdout(io.StringIO()):
            rc = oracle_query.selftest()
        self.assertEqual(rc, 0)
        self.assertIsNotNone(driver.pool)
        self.assertTrue(driver.pool.closed)
        self.assertIsNone(oracle_query._pool_state)

    def test_server_exit_closes_the_pool(self) -> None:
        driver = FailingDriver([], "connect")
        self._run_query(driver)
        with mock.patch.object(oracle_query.sys, "stdin", io.StringIO("")):
            self.assertEqual(oracle_query.main(), 0)
        self.assertIsNotNone(driver.pool)
        self.assertTrue(driver.pool.closed)
        self.assertIsNone(oracle_query._pool_state)


class SelftestTests(PoolTestBase):
    """--selftest must fail safely when Oracle is not configured."""

    def test_selftest_fails_cleanly_without_configuration(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(
            out
        ), contextlib.redirect_stderr(err):
            rc = oracle_query.selftest()
        self.assertEqual(rc, 1)
        self.assertIn("selftest failed", err.getvalue())
        self.assertIn("not configured", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_selftest_succeeds_with_a_stub_driver(self) -> None:
        driver = FailingDriver([], "connect")
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=RetryTests.CONFIG
        ), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = oracle_query.selftest()
        self.assertEqual(rc, 0)
        self.assertIn("selftest: ok", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_selftest_rejects_an_unexpected_probe_result(self) -> None:
        class OddCursor(FakeCursor):
            def fetchone(self) -> tuple[int]:
                return (2,)

        class OddConnection(FakeConnection):
            def cursor(self) -> OddCursor:
                return OddCursor(self.driver)

        driver = FailingDriver([], "connect")
        driver.connection_class = OddConnection
        err = io.StringIO()
        with mock.patch.dict(
            sys.modules, {"oracledb": driver.module()}
        ), mock.patch.object(
            oracle_query, "_config", return_value=RetryTests.CONFIG
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            err
        ):
            rc = oracle_query.selftest()
        self.assertEqual(rc, 1)
        self.assertIn("unexpected result", err.getvalue())
        self.assertTrue(driver.pool is None or driver.pool.closed)

    def test_cli_selftest_exits_nonzero_without_configuration(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SERVER), "--selftest"],
            env={},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selftest failed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class ClosedPipeTests(unittest.TestCase):
    """A client that closes the pipe must not produce a traceback."""

    class BrokenStdout:
        def write(self, value: str) -> int:
            raise BrokenPipeError()

        def flush(self) -> None:
            raise BrokenPipeError()

        def fileno(self) -> int:
            raise OSError("no fileno")

    class ClosedStdin:
        def readline(self) -> str:
            raise BrokenPipeError()

    def test_write_exits_quietly_on_a_broken_pipe(self) -> None:
        with mock.patch.object(
            oracle_query.sys, "stdout", self.BrokenStdout()
        ):
            with self.assertRaises(SystemExit) as caught:
                oracle_query._write({"jsonrpc": "2.0", "id": 1})
        self.assertEqual(caught.exception.code, 0)

    def test_main_returns_when_stdin_is_closed(self) -> None:
        with mock.patch.object(
            oracle_query.sys, "stdin", self.ClosedStdin()
        ):
            self.assertEqual(oracle_query.main(), 0)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""SQL-guard tests for mcp/oracle_query.py.

No database is used. python-oracledb is imported lazily inside the query
function, so these tests can run on a machine without it.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "oracle_query.py"

spec = importlib.util.spec_from_file_location("oracle_query", SERVER)
oracle_query = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle_query)


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


class QueryExecutionTests(unittest.TestCase):
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

        fake = types.ModuleType("oracledb")
        fake.defaults = types.SimpleNamespace(fetch_lobs=True)
        connection = FakeConnection()

        def connect(**kwargs: object) -> FakeConnection:
            fake.connect_kwargs = kwargs
            return connection

        fake.connect = connect

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
        self.assertEqual(connection.call_timeout, oracle_query.CALL_TIMEOUT_MS)
        self.assertEqual(
            connection.cursor_instance.statements[0], "SET TRANSACTION READ ONLY"
        )
        self.assertEqual(
            connection.cursor_instance.fetch_size, oracle_query.MAX_ROWS + 1
        )
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connection.closed)
        # The password is used for the connection but never returned.
        self.assertEqual(fake.connect_kwargs["password"], "password")
        self.assertNotIn("password", text)
        self.assertNotIn("dsn", text)
        self.assertNotIn("/wallet", text)


if __name__ == "__main__":
    unittest.main()

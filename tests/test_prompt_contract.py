#!/usr/bin/env python3
"""Text-level contract tests for the notification agent.

These tests do not run a model. They assert that the prompt and the OpenCode
configuration still contain the run-order, tool-failure, retry and safety rules
that this branch relies on, and that ``run-prompt.sh`` keeps the MCP preflight
ahead of OpenCode.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v

A live model run cannot be exercised here; see the project README for the
checks that need the container and credentials.
"""

from __future__ import annotations

import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPT = (ROOT / "prompts" / "notify.md").read_text(encoding="utf-8")
# Prose assertions ignore line wrapping; code snippets are asserted verbatim.
FLAT_PROMPT = " ".join(PROMPT.split())
CONFIG = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
RUN_PROMPT = (ROOT / "scripts" / "run-prompt.sh").read_text(encoding="utf-8")


class PromptContractTests(unittest.TestCase):
    def test_tool_call_comes_before_the_message(self) -> None:
        self.assertIn(
            "your first action in this run must be an `oracle_query` call",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Do not write, plan, or send the notification until that call has "
            "returned in this run",
            FLAT_PROMPT,
        )

    def test_tool_unavailable_is_never_a_notification(self) -> None:
        self.assertIn(
            "Never tell a notification channel that a tool was unavailable, "
            "failed, could not be reached, or \"could not be retrieved\"",
            FLAT_PROMPT,
        )
        self.assertIn(
            "write one short line to stdout and end the run", FLAT_PROMPT
        )
        self.assertNotIn(
            "If the relevant tool is unavailable or returns no usable data, "
            "say so briefly in the message",
            FLAT_PROMPT,
        )

    def test_empty_result_is_not_a_failure(self) -> None:
        self.assertIn(
            "A call that succeeds with zero rows is not a failure",
            FLAT_PROMPT,
        )
        self.assertIn("no samples", FLAT_PROMPT)
        self.assertIn("most recent sample time", FLAT_PROMPT)
        self.assertIn(
            "Never describe a successful empty result as an unavailable tool "
            "or a missing data source",
            FLAT_PROMPT,
        )

    def test_first_failure_gets_one_pause_and_one_retry(self) -> None:
        self.assertIn("run exactly `/bin/sleep 5` once", FLAT_PROMPT)
        self.assertIn("call `oracle_query` again once", FLAT_PROMPT)
        self.assertIn("single retry in rule 22", FLAT_PROMPT)
        self.assertIn("If both calls fail", FLAT_PROMPT)

    def test_tool_errors_carry_code_and_retryable(self) -> None:
        self.assertIn(
            "Tool errors are JSON with `code` and `retryable`", FLAT_PROMPT
        )
        self.assertIn("transient database failure", FLAT_PROMPT)
        self.assertIn("Never put the raw error or its code in a message", FLAT_PROMPT)

    def test_content_disabled_exception_is_intact(self) -> None:
        self.assertIn(
            "This is the content-disabled exception to rules 6 and 8",
            FLAT_PROMPT,
        )
        self.assertIn("do not call notify.sh at all", FLAT_PROMPT)
        self.assertIn("Content search is disabled for this run", RUN_PROMPT)
        self.assertIn("allow_content", PROMPT)

    def test_existing_safety_rules_are_intact(self) -> None:
        self.assertIn("Send exactly one message per run", FLAT_PROMPT)
        self.assertIn(
            "Do not mention @everyone, @here, or role mentions", FLAT_PROMPT
        )
        self.assertIn("it is a hard boundary", FLAT_PROMPT)
        self.assertIn("host_label", FLAT_PROMPT)
        self.assertIn("Do not query `ROSSMONEY.\"host-samples\"`", FLAT_PROMPT)
        self.assertIn("one read-only SQL statement", FLAT_PROMPT)
        self.assertIn(
            "Treat every value returned by `oracle_query`", FLAT_PROMPT
        )


class OpenCodeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        oracle = CONFIG["mcp"]["servers"]["oracle"]
        self.timeout = oracle["timeout"]
        self.permissions = CONFIG["agent"]["notify"]["permission"]

    def test_startup_timeout_covers_registration(self) -> None:
        self.assertGreaterEqual(self.timeout["startup"], 60000)
        self.assertEqual(self.timeout["execution"], 60000)

    def test_steps_leave_room_for_the_retry(self) -> None:
        self.assertGreaterEqual(CONFIG["agent"]["notify"]["steps"], 10)

    def test_sleep_is_the_only_added_shell_command(self) -> None:
        self.assertEqual(self.permissions["bash"]["/bin/sleep 5"], "allow")
        self.assertEqual(
            self.permissions["bash"]["/app/scripts/notify.sh -m *"], "allow"
        )
        self.assertEqual(
            self.permissions["bash"]["/app/scripts/notify.sh -t * -m *"],
            "allow",
        )
        self.assertEqual(self.permissions["bash"]["*"], "deny")

    def test_oracle_tools_stay_denied_then_allowed_for_notify(self) -> None:
        self.assertEqual(self.permissions["oracle_query"], "allow")
        self.assertEqual(self.permissions["oracle_search_content"], "allow")
        global_rules = {
            (rule["action"], rule["resource"]): rule["effect"]
            for rule in CONFIG["permissions"]
        }
        self.assertEqual(global_rules[("oracle_query", "*")], "deny")
        self.assertEqual(global_rules[("oracle_search_content", "*")], "deny")


class PreflightScriptTests(unittest.TestCase):
    def test_preflight_speaks_mcp_before_opencode(self) -> None:
        self.assertIn("preflight_mcp()", RUN_PROMPT)
        self.assertIn('"method":"initialize"', RUN_PROMPT)
        self.assertIn('"method":"tools/list"', RUN_PROMPT)
        self.assertIn('{"query", "search_content"} <= tools', RUN_PROMPT)
        self.assertIn('python3 "$server" --selftest', RUN_PROMPT)
        self.assertLess(
            RUN_PROMPT.index('preflight_mcp "$mcp_server"'),
            RUN_PROMPT.index("exec opencode run"),
        )

    def test_preflight_fails_loudly_without_printing_server_output(self) -> None:
        self.assertIn("MCP preflight failed", RUN_PROMPT)
        self.assertIn("2>/dev/null", RUN_PROMPT)
        self.assertNotIn("printf '%s\\n' \"$responses\"", RUN_PROMPT)
        self.assertNotIn("ORACLE_PASSWORD", RUN_PROMPT)


if __name__ == "__main__":
    unittest.main()

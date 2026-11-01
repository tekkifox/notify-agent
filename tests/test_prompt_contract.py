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

    def test_restricted_runs_do_not_name_withheld_capabilities(self) -> None:
        self.assertIn("what you can do", FLAT_PROMPT)
        self.assertIn("available in this run", FLAT_PROMPT)
        self.assertNotIn("say briefly that you can't help with that here", FLAT_PROMPT)
        self.assertIn("Report host data only", RUN_PROMPT)
        self.assertNotIn("Content search is disabled", RUN_PROMPT)
        self.assertNotIn("site, CV, projects or pages", FLAT_PROMPT)

    def test_rule_37_is_conditional_on_instruction_scope(self) -> None:
        self.assertIn("only if this run permits it", FLAT_PROMPT)
        self.assertIn("When the run is host-data-only", FLAT_PROMPT)
        self.assertIn("do not mention site content", FLAT_PROMPT)
        self.assertNotIn("published site content (pages and projects) only where this run permits it", FLAT_PROMPT)

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

    def test_digest_calls_oracle_then_ssh(self) -> None:
        self.assertIn(
            "call `oracle_query` first and `ssh_probe` second, in that order",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Do not write, plan, or send the notification until that call has "
            "returned in this run",
            FLAT_PROMPT,
        )

    def test_ssh_failures_must_read_differently(self) -> None:
        self.assertIn("SSH auth_refused (authentication refused)", FLAT_PROMPT)
        self.assertIn("SSH timeout (connection timed out)", FLAT_PROMPT)
        self.assertIn(
            "\"Could not connect\" and \"authentication refused\" are different "
            "problems",
            FLAT_PROMPT,
        )

    def test_delivery_scope_is_the_only_host_set(self) -> None:
        self.assertIn(
            "the only hosts that exist in this run", FLAT_PROMPT
        )
        self.assertIn(
            "Never mention, probe, or hint at a host outside this run's "
            "delivery scope",
            FLAT_PROMPT,
        )
        self.assertIn(
            "In a run scoped to `admin-chat`, no rossmoney host may appear at "
            "all",
            FLAT_PROMPT,
        )
        self.assertIn(
            "If a result is refused as `not_allowed` or `out_of_scope`, omit "
            "it entirely",
            FLAT_PROMPT,
        )

    def test_ssh_failure_omits_the_ssh_part(self) -> None:
        self.assertIn(
            "If `ssh_probe` cannot run, omit the SSH part of the digest "
            "entirely",
            FLAT_PROMPT,
        )
        self.assertIn("Do not imply any host is healthy", FLAT_PROMPT)
        self.assertNotIn("SSH is not configured", FLAT_PROMPT)

    def test_old_report_format_is_pinned(self) -> None:
        self.assertIn(
            "sample count, last-seen age, CPU average and peak, memory "
            "average, load average and peak",
            FLAT_PROMPT,
        )
        self.assertIn("memory is average only, disk is latest only", FLAT_PROMPT)
        self.assertIn("STALE means no sample in the last 20 minutes, not 30", FLAT_PROMPT)
        self.assertIn(
            "CPU peak 90%, memory average 90%, load peak 8, disk latest 90%",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Order known hosts first, in the order the instruction lists them",
            FLAT_PROMPT,
        )
        self.assertIn(
            "\u2026 truncated: N host line(s) not shown", FLAT_PROMPT
        )


class OpenCodeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        oracle = CONFIG["mcp"]["servers"]["oracle"]
        self.timeout = oracle["timeout"]
        self.ssh = CONFIG["mcp"]["servers"]["ssh"]
        self.permissions = CONFIG["agent"]["notify"]["permission"]

    def test_startup_timeout_covers_registration(self) -> None:
        self.assertGreaterEqual(self.timeout["startup"], 60000)
        self.assertEqual(self.timeout["execution"], 60000)
        self.assertEqual(self.ssh["timeout"], self.timeout)

    def test_ssh_server_matches_the_oracle_shape(self) -> None:
        self.assertEqual(self.ssh["type"], "local")
        self.assertEqual(
            self.ssh["command"], ["python3", "/app/mcp/ssh_probe.py"]
        )
        self.assertFalse(self.ssh["codemode"])
        self.assertIn("NOTIFY_HOST_SCOPE", self.ssh["environment"])
        self.assertEqual(self.ssh["environment"]["PYTHONPATH"], "/opt/mcp-python")

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
        self.assertEqual(self.permissions["ssh_probe"], "allow")
        global_rules = {
            (rule["action"], rule["resource"]): rule["effect"]
            for rule in CONFIG["permissions"]
        }
        self.assertEqual(global_rules[("oracle_query", "*")], "deny")
        self.assertEqual(global_rules[("oracle_search_content", "*")], "deny")
        self.assertEqual(global_rules[("ssh_probe", "*")], "deny")

    def test_host_scope_is_exported_to_the_mcp_child(self) -> None:
        self.assertIn("NOTIFY_HOST_SCOPE=$target_host_scope_json", RUN_PROMPT)
        self.assertIn("export NOTIFY_HOST_SCOPE", RUN_PROMPT)
        self.assertIn("HOST_REPORT_HOSTS", RUN_PROMPT)

    def test_runbooks_are_default_deny_for_the_mcp_child(self) -> None:
        self.assertIn("NOTIFY_RUNBOOKS_ALLOWED=1", RUN_PROMPT)
        self.assertIn("export NOTIFY_RUNBOOKS_ALLOWED", RUN_PROMPT)
        self.assertIn("unset NOTIFY_RUNBOOKS_ALLOWED", RUN_PROMPT)
        self.assertIn("NOTIFY_BOOKSTACK_URL=$target_bookstack_url", RUN_PROMPT)
        self.assertIn("export NOTIFY_BOOKSTACK_URL", RUN_PROMPT)
        self.assertIn("NOTIFY_BOOKSTACK_BOOKS=$target_bookstack_books", RUN_PROMPT)
        self.assertIn("export NOTIFY_BOOKSTACK_BOOKS", RUN_PROMPT)
        bookstack = CONFIG["mcp"]["servers"]["bookstack"]
        for variable in (
            "NOTIFY_RUNBOOKS_ALLOWED",
            "NOTIFY_BOOKSTACK_URL",
            "NOTIFY_BOOKSTACK_BOOKS",
        ):
            self.assertEqual(
                bookstack["environment"][variable],
                "{env:" + variable + "}",
            )
        self.assertNotIn("BOOKSTACK_URL", bookstack["environment"])
        self.assertNotIn("BOOKSTACK_ALLOWED_BOOKS", bookstack["environment"])


class PreflightScriptTests(unittest.TestCase):
    def test_preflight_speaks_mcp_before_opencode(self) -> None:
        self.assertIn("preflight_mcp()", RUN_PROMPT)
        self.assertIn('"method":"initialize"', RUN_PROMPT)
        self.assertIn('"method":"tools/list"', RUN_PROMPT)
        self.assertIn('{"query", "search_content"} <= tools', RUN_PROMPT)
        self.assertIn('python3 "$server" --selftest', RUN_PROMPT)
        self.assertLess(
            RUN_PROMPT.index('preflight_mcp "$mcp_server"'),
            RUN_PROMPT.index("set -- opencode run"),
        )

    def test_preflight_fails_loudly_without_printing_server_output(self) -> None:
        self.assertIn("MCP preflight failed", RUN_PROMPT)
        self.assertIn("2>/dev/null", RUN_PROMPT)
        self.assertNotIn("printf '%s\\n' \"$responses\"", RUN_PROMPT)
        self.assertNotIn("ORACLE_PASSWORD", RUN_PROMPT)


if __name__ == "__main__":
    unittest.main()

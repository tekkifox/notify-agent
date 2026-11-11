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

import ast
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPT = (ROOT / "prompts" / "notify.md").read_text(encoding="utf-8")
# Prose assertions ignore line wrapping; code snippets are asserted verbatim.
FLAT_PROMPT = " ".join(PROMPT.split())
CONFIG = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
RUN_PROMPT = (ROOT / "scripts" / "run-prompt.sh").read_text(encoding="utf-8")
SSH_SERVER = ROOT / "mcp" / "ssh_probe.py"
CRONTABS = {
    name: (ROOT / name).read_text(encoding="utf-8")
    for name in ("crontab", "crontab.live")
}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]):
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current
        current = parents.get(current)
    return None


def _env_vars_read_by_ssh_server() -> set[str]:
    """Return every environment variable name ``ssh_probe.py`` reads.

    Derived from the server's own source so the passthrough contract cannot
    go stale. ``_env`` is the only reader of ``os.environ``; a helper that
    forwards its first parameter to an env accessor (such as
    ``_positive_float``) is followed as if it were an accessor too.
    """

    tree = ast.parse(SSH_SERVER.read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }

    def resolve(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        return None

    accessors = {"_env"}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            parameters = node.args.posonlyargs + node.args.args
            if not parameters:
                continue
            first = parameters[0].arg
            forwards = any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in accessors
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == first
                for call in ast.walk(node)
            )
            if forwards and node.name not in accessors:
                accessors.add(node.name)
                changed = True

    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    names: set[str] = set()
    unresolved: list[str] = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id in accessors
            and call.args
        ):
            continue
        name = resolve(call.args[0])
        if name is not None:
            names.add(name)
            continue
        # A bare parameter is resolved at the call site that passes it in.
        function = _enclosing_function(call, parents)
        parameters = function.args.posonlyargs + function.args.args if function else []
        if not (
            isinstance(call.args[0], ast.Name)
            and call.args[0].id in {parameter.arg for parameter in parameters}
        ):
            unresolved.append(ast.unparse(call))

    if unresolved:
        raise AssertionError(
            "cannot derive environment names from ssh_probe.py: "
            + ", ".join(sorted(unresolved))
        )
    return names


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

    def test_first_failure_gets_up_to_three_retries(self) -> None:
        self.assertIn(
            "the Oracle/SSH MCP tools register a moment after the run starts, "
            "so the first call in a run can fail before the tool exists",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Run `/bin/sleep 5`, then call `oracle_query` again, up to three "
            "times, 5 seconds apart",
            FLAT_PROMPT,
        )
        self.assertIn("Do not conclude anything from the first failure", FLAT_PROMPT)
        self.assertIn("If all calls fail", FLAT_PROMPT)

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

    def test_silent_scheduled_runs_never_reply(self) -> None:
        # The crontab asks for silence; the prompt has to make that binding on
        # the model's final response too, or a quiet run still posts text.
        self.assertIn(
            "On a scheduled run, `notify.sh` is the only way you ever say "
            "anything",
            FLAT_PROMPT,
        )
        self.assertIn(
            "do not call `notify.sh`, do not write a chat reply, and end the "
            "run with no output at all",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Never write a reply instead of, or in addition to, a notification",
            FLAT_PROMPT,
        )
        self.assertIn(
            "the notification itself carries the finding; there is still no "
            "separate reply",
            FLAT_PROMPT,
        )
        self.assertIn(
            "unless the instruction says to stay silent when nothing is "
            "flagged",
            FLAT_PROMPT,
        )
        self.assertIn(
            "A run that ends silently under rule 43 has no final response at "
            "all",
            FLAT_PROMPT,
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
        self.assertIn(
            "with neither, it probes exactly this run's delivery scope",
            FLAT_PROMPT,
        )
        self.assertIn(
            "or every host in the run's delivery scope when it lists none",
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
            "sample count, last-seen age, the CPU core count, CPU average and "
            "peak, memory average, load average and peak",
            FLAT_PROMPT,
        )
        self.assertIn("memory is average only, disk is latest only", FLAT_PROMPT)
        self.assertIn("STALE means no sample in the last 20 minutes, not 30", FLAT_PROMPT)
        self.assertIn(
            "Order known hosts first, in the order the instruction lists them",
            FLAT_PROMPT,
        )
        self.assertIn(
            "\u2026 truncated: N host line(s) not shown", FLAT_PROMPT
        )

    def test_view_columns_include_cpu_cores_and_failed_logins(self) -> None:
        # The column list must carry all three, each with what it means, so the
        # model knows it can read them rather than reporting them as missing.
        self.assertIn(
            "`cpu_percent`, `cpu_cores` (the cores the host reported with the "
            "sample; stable per host but may be null on an old sample)",
            FLAT_PROMPT,
        )
        self.assertIn(
            "`failed_logins` (failed SSH password attempts in the hour before "
            "the sample, an integer or null)",
            FLAT_PROMPT,
        )
        self.assertIn(
            "`failed_login_users` (the per-user breakdown behind those "
            "failures, as a JSON array of `{\"user\", \"count\"}` or null)",
            FLAT_PROMPT,
        )

    def test_render_order_names_the_core_count(self) -> None:
        self.assertIn(
            "Render one line per host in this order: sample count, last-seen "
            "age, the CPU core count, CPU average and peak",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Write the core count as `2 cores` or `1 core`, taken from the "
            "newest sample that has a value",
            FLAT_PROMPT,
        )
        self.assertIn(
            "when the count is null write `cores n/a`, and never invent or "
            "round a number",
            FLAT_PROMPT,
        )

    def test_view_columns_include_accepted_logins(self) -> None:
        # The five accepted-login columns must each say what they mean, so the
        # model can render them instead of reporting them as missing.
        self.assertIn(
            "`ssh_logins` (accepted SSH logins in the hour before the sample, "
            "an integer or null)",
            FLAT_PROMPT,
        )
        self.assertIn(
            "`ssh_login_users` (the per-user breakdown behind those accepted "
            "logins",
            FLAT_PROMPT,
        )
        self.assertIn(
            "`ssh_logins_today` (accepted SSH logins since the start of the "
            "current UTC day, an integer or null)",
            FLAT_PROMPT,
        )
        self.assertIn(
            "`ssh_login_users_today` (the per-user breakdown behind those "
            "logins, the same JSON shape or null)",
            FLAT_PROMPT,
        )
        self.assertIn(
            "`ssh_logins_today_since` (the start of that UTC day, as ISO text "
            "or null)",
            FLAT_PROMPT,
        )

    def test_render_order_names_the_login_fragment(self) -> None:
        self.assertIn(
            "then disk from the latest sample, then failed logins, then "
            "accepted logins",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Write the login fragment as `logins: 12 last hour (root 9, ross "
            "3), 497 today`",
            FLAT_PROMPT,
        )
        self.assertIn(
            "the top three users by count in that last-hour list, then the "
            "running UTC-day total",
            FLAT_PROMPT,
        )
        self.assertIn(
            "When a last-hour figure is null write `logins: n/a`; when the "
            "day's total is null write `today: n/a`",
            FLAT_PROMPT,
        )
        self.assertIn(
            "Never invent a number or a username that is not in the data",
            FLAT_PROMPT,
        )

    def test_worked_example_shows_the_core_count(self) -> None:
        self.assertIn("last-seen 4m ago; 2 cores; CPU 44.8% avg", FLAT_PROMPT)

    def test_aggregate_query_selects_the_core_count(self) -> None:
        self.assertIn(
            "`last_seen`, `cpu_cores`, `cpu_avg`, `cpu_peak`", FLAT_PROMPT
        )
        self.assertIn("MAX(cpu_cores) AS cpu_cores,", PROMPT)


class SustainedSignalRuleTests(unittest.TestCase):
    """Rule 29 flags a host only on a sustained signal, never on a peak alone.

    The old rule flagged a host on a 90 % CPU peak, a 90 % memory average, an
    absolute load peak of 8 and a 90 % disk reading. Those thresholds fired on
    every host that spiked, so the rule now matches the crontab prompts:
    average CPU, average memory, the latest disk reading, and average load
    measured against the host's own core count.
    """

    #: Every parenthesised threshold in the prompt must be one of the four
    #: sustained signals. A peak may appear as context, never as a reason.
    FLAG_REASON = re.compile(r"\(([^()]*>= [^()]*)\)")
    ALLOWED_REASON = re.compile(
        r"^(?:"
        r"CPU avg \d+(?:\.\d+)?% >= 60%"
        r"|memory \d+(?:\.\d+)?% >= 85%"
        r"|disk \d+(?:\.\d+)?% >= 95%"
        r"|load avg \d+(?:\.\d+)? >= 2x\d+ cores"
        r")$"
    )

    def test_the_four_sustained_signals_are_named_with_their_numbers(self) -> None:
        self.assertIn("average CPU >= 60 %", FLAT_PROMPT)
        self.assertIn("average memory >= 85 %", FLAT_PROMPT)
        self.assertIn("disk from the latest sample >= 95 %", FLAT_PROMPT)
        self.assertIn(
            "average load >= twice that host's own core count", FLAT_PROMPT
        )

    def test_no_old_cpu_peak_threshold_survives(self) -> None:
        self.assertNotRegex(FLAT_PROMPT, r"CPU peak[^)]{0,40}>=")
        self.assertNotIn("CPU peak 90", FLAT_PROMPT)
        self.assertNotIn("CPU peak 117.0% >= 90%", FLAT_PROMPT)

    def test_no_absolute_load_peak_threshold_survives(self) -> None:
        # `load avg 8.74 >= 2x2 cores` is allowed; a bare `>= 8` is not.
        self.assertNotRegex(
            FLAT_PROMPT, r"load peak [\d.]+ >= [\d.]+(?![x\d])"
        )
        self.assertNotRegex(
            FLAT_PROMPT, r"load avg [\d.]+ >= [\d.]+(?![x\d])"
        )

    def test_no_flag_reason_is_a_peak_alone(self) -> None:
        reasons = self.FLAG_REASON.findall(PROMPT)
        self.assertTrue(reasons)
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertRegex(reason, self.ALLOWED_REASON)
        self.assertNotIn("(CPU peak", FLAT_PROMPT)
        self.assertNotIn("(load peak", FLAT_PROMPT)

    def test_flag_reasons_are_the_documented_examples(self) -> None:
        for reason in (
            "(CPU avg 76.7% >= 60%)",
            "(memory 88.0% >= 85%)",
            "(disk 95.4% >= 95%)",
            "(load avg 8.74 >= 2x2 cores)",
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, FLAT_PROMPT)

    def test_the_load_test_uses_the_host_core_count(self) -> None:
        self.assertIn("average load >= twice that host's own core count", FLAT_PROMPT)
        self.assertIn(
            "compares the average load with twice the core count from `cpu_cores`",
            FLAT_PROMPT,
        )
        self.assertIn(
            "when the core count is null the load rule cannot apply", FLAT_PROMPT
        )
        self.assertIn(
            "never flagged on load \u2014 do not guess or invent a core count",
            FLAT_PROMPT,
        )

    def test_peaks_are_context_only(self) -> None:
        self.assertIn(
            "CPU peak and load peak are context, never a flag on their own",
            FLAT_PROMPT,
        )
        self.assertIn("a CPU peak never flags a host", FLAT_PROMPT)
        self.assertIn("a load peak never flags one either", FLAT_PROMPT)

    def test_reason_order_is_stale_cpu_memory_disk_load(self) -> None:
        self.assertIn(
            "STALE, then average CPU, memory, disk, load", FLAT_PROMPT
        )
        self.assertNotIn("STALE, then CPU, memory, load, disk", FLAT_PROMPT)

    def test_worked_example_flags_the_average_not_the_peak(self) -> None:
        self.assertIn("(load avg 8.74 >= 2x2 cores)", FLAT_PROMPT)
        self.assertIn("load 8.74 avg / 9.50 peak", FLAT_PROMPT)
        self.assertNotIn("(load peak", FLAT_PROMPT)
        self.assertNotIn("(CPU peak", FLAT_PROMPT)


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

    def test_every_ssh_env_var_is_passed_through(self) -> None:
        environment = self.ssh["environment"]
        expected = _env_vars_read_by_ssh_server()
        # Guard against a derivation that silently stops finding anything.
        self.assertTrue(expected)
        for name in sorted(expected):
            with self.subTest(variable=name):
                self.assertIn(name, environment)
                self.assertEqual(environment[name], f"{{env:{name}}}")

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
        self.assertIn("NOTIFY_BOOKSTACK_TOKEN_ID=$target_bookstack_token_id", RUN_PROMPT)
        self.assertIn("export NOTIFY_BOOKSTACK_TOKEN_ID", RUN_PROMPT)
        self.assertIn(
            "NOTIFY_BOOKSTACK_TOKEN_SECRET=$target_bookstack_token_secret",
            RUN_PROMPT,
        )
        self.assertIn("export NOTIFY_BOOKSTACK_TOKEN_SECRET", RUN_PROMPT)
        self.assertIn('BOOKSTACK_TOKEN_ID_${token_suffix}', RUN_PROMPT)
        self.assertIn('BOOKSTACK_TOKEN_SECRET_${token_suffix}', RUN_PROMPT)
        bookstack = CONFIG["mcp"]["servers"]["bookstack"]
        for variable in (
            "NOTIFY_RUNBOOKS_ALLOWED",
            "NOTIFY_BOOKSTACK_URL",
            "NOTIFY_BOOKSTACK_BOOKS",
            "NOTIFY_BOOKSTACK_TOKEN_ID",
            "NOTIFY_BOOKSTACK_TOKEN_SECRET",
        ):
            self.assertEqual(
                bookstack["environment"][variable],
                "{env:" + variable + "}",
            )
        self.assertNotIn("BOOKSTACK_URL", bookstack["environment"])
        self.assertNotIn("BOOKSTACK_ALLOWED_BOOKS", bookstack["environment"])
        self.assertNotIn("BOOKSTACK_TOKEN_ID", bookstack["environment"])
        self.assertNotIn("BOOKSTACK_TOKEN_SECRET", bookstack["environment"])


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

    def test_preflight_retries_only_the_self_test_once(self) -> None:
        self.assertIn(
            "preflight_retry_delay=${NOTIFY_PREFLIGHT_RETRY_DELAY:-60}",
            RUN_PROMPT,
        )
        self.assertIn('if [ "$preflight_rc" -eq 2 ]', RUN_PROMPT)
        self.assertIn('sleep "$preflight_retry_delay"', RUN_PROMPT)
        self.assertIn('if [ "$preflight_retried" -eq 1 ]', RUN_PROMPT)

    def test_failed_preflight_notifies_the_same_target(self) -> None:
        self.assertIn("MCP preflight Oracle self-test reason", RUN_PROMPT)
        self.assertIn(
            "could not run because the database was unreachable", RUN_PROMPT
        )
        self.assertIn(
            '"$script_dir/notify.sh" -t "$target" -m "$message"', RUN_PROMPT
        )


class FleetDigestScheduleTests(unittest.TestCase):
    """The two fleet-wide digest prompts must ask for per-host detail.

    That is failed logins per user and the host's own CPU core count, because
    the load threshold is stated in terms of that core count.
    """

    def _schedule_line(self, name: str, schedule: str, target: str) -> str:
        for line in CRONTABS[name].splitlines():
            stripped = line.lstrip("# ")
            if stripped.startswith(schedule) and f"-t {target} " in stripped:
                return stripped
        self.fail(f"{name}: no {schedule!r} line for target {target!r}")

    def _digest_lines(self) -> list[tuple[str, str]]:
        return [
            (
                f"{name} {label}",
                self._schedule_line(name, schedule, "automations"),
            )
            for name in CRONTABS
            for label, schedule in (
                ("morning", "0 9 * * * "),
                ("hourly", "15 * * * * "),
            )
        ]

    def test_failed_logins_are_requested_per_user(self) -> None:
        for label, line in self._digest_lines():
            with self.subTest(schedule=label):
                self.assertIn("failed logins", line)
                self.assertIn("top three usernames", line)
                self.assertIn("failed logins: n/a", line)
                self.assertIn("'failed logins: 0'", line)
                self.assertIn("Never invent a username or a number", line)

    def test_ssh_probe_is_not_the_failed_login_source(self) -> None:
        for label, line in self._digest_lines():
            with self.subTest(schedule=label):
                self.assertIn(
                    "ssh_probe answers reachability and key authentication only",
                    line,
                )
                self.assertIn(
                    "must not be used as the source of failed-login figures",
                    line,
                )
                self.assertIn("those figures come from the supplied statistics", line)

    def test_core_count_is_asked_for_on_every_host_line(self) -> None:
        for label, line in self._digest_lines():
            with self.subTest(schedule=label):
                self.assertIn("CPU core count", line)
                self.assertIn("'2 cores'", line)
                self.assertIn("'1 core'", line)
                self.assertIn("'cores n/a'", line)
                self.assertIn("Never invent a username or a number", line)

    def test_accepted_logins_are_requested_on_every_host_line(self) -> None:
        for label, line in self._digest_lines():
            with self.subTest(schedule=label):
                self.assertIn("accepted logins from the latest sample", line)
                self.assertIn(
                    "'logins: 12 last hour (root 9, ross 3), 497 today'", line
                )
                self.assertIn("top three usernames by count", line)
                self.assertIn("'logins: n/a'", line)
                self.assertIn("'today: n/a'", line)
                self.assertIn(
                    "Never invent a number or a username that is not in the data",
                    line,
                )

    def test_sustained_signal_rule_is_averages_only(self) -> None:
        for label, line in self._digest_lines():
            with self.subTest(schedule=label):
                self.assertIn("average CPU >= 60", line)
                self.assertIn("average memory >= 85", line)
                self.assertIn("disk >= 95", line)
                self.assertIn(
                    "average load >= twice that host's own core count", line
                )
                self.assertIn("Decide every flag from the averages", line)
                self.assertIn(
                    "never flag a host because of a peak", line
                )
                self.assertNotIn("load peak >=", line)

    def test_no_crontab_rule_flags_on_a_peak(self) -> None:
        """A peak may be context, never a flag condition or an old rule."""
        for name, text in CRONTABS.items():
            for number, line in enumerate(text.splitlines(), 1):
                with self.subTest(crontab=name, line=number):
                    self.assertNotIn("peak >=", line)
                    self.assertNotIn("never flag on a peak alone", line)

    def test_morning_digest_keeps_its_character_limit(self) -> None:
        line = self._schedule_line("crontab", "0 9 * * * ", "automations")
        self.assertIn("2,000 characters", line)

    def test_daily_ssh_report_takes_its_hosts_from_the_scope(self) -> None:
        """The CMS target's host_scope is the only host list the report needs."""
        for name in CRONTABS:
            with self.subTest(crontab=name):
                line = self._schedule_line(name, "0 8 * * * ", "automations")
                self.assertIn("Call ssh_probe without a host list", line)
                self.assertIn(
                    "it probes every host in the scope of this target", line
                )
                self.assertIn("outside the scope of this run", line)
                self.assertNotIn("outside the seven", line)
                # No enumerated hosts, so the CMS list stays authoritative.
                for host in (
                    "rossmoney.me",
                    "photos.rossmoney.me",
                    "vortexservers.co.uk",
                    "storage.rossmoney.me",
                    "paseo.rossmoney.me",
                    "mgmt.rossmoney.me",
                    "media.rossmoney.me",
                ):
                    with self.subTest(host=host):
                        self.assertNotIn(host, line)

    def test_live_morning_digest_matches_the_template(self) -> None:
        self.assertEqual(
            self._schedule_line("crontab", "0 9 * * * ", "automations"),
            self._schedule_line("crontab.live", "0 9 * * * ", "automations"),
        )

    def test_live_hourly_digests_stay_silent_unless_something_is_flagged(
        self,
    ) -> None:
        silence_rule = (
            "Call notify.sh only when at least one host is flagged or the "
            "supplied statistics report failed logins; if neither, do not call "
            "notify.sh and do not reply: end the run silently with no output."
        )
        for schedule, target in (
            ("5 * * * * ", "admin-chat"),
            ("15 * * * * ", "automations"),
        ):
            with self.subTest(schedule=schedule, target=target):
                line = self._schedule_line("crontab.live", schedule, target)
                self.assertIn("/app/scripts/run-prompt.sh", line)
                self.assertIn(f"-t {target} ", line)
                self.assertIn(silence_rule, line)
                self.assertNotIn("If nothing is flagged", line)

    def test_the_admin_chat_hourly_job_reports_failed_logins(self) -> None:
        line = self._schedule_line("crontab.live", "5 * * * * ", "admin-chat")
        self.assertIn("failed logins", line)
        self.assertIn("top three usernames", line)
        self.assertIn("Never invent a username or a number", line)
        self.assertIn(
            "Call notify.sh only when at least one host is flagged or the "
            "supplied statistics report failed logins",
            line,
        )
        self.assertIn("if neither, do not call notify.sh", line)

    def test_no_scheduled_prompt_asks_for_a_reply(self) -> None:
        for name, text in CRONTABS.items():
            with self.subTest(crontab=name):
                self.assertNotIn("reply with one line", text)
                if "say so in one line" in text:
                    self.assertIn(
                        "say so in one line at the top and still send the report",
                        text,
                    )

    def test_skip_capable_prompts_end_the_run_silently(self) -> None:
        """Every schedule that may skip delivery must stop with no output."""
        skip_markers = ("Call notify.sh only when", "do not call notify.sh")
        for name, text in CRONTABS.items():
            for number, line in enumerate(text.splitlines(), 1):
                if not any(marker in line for marker in skip_markers):
                    continue
                with self.subTest(crontab=name, line=number):
                    self.assertIn("do not reply", line)
                    self.assertIn(
                        "end the run silently with no output", line
                    )
                    self.assertIn(
                        "notify.sh is the only way this run may say anything",
                        line,
                    )
                    self.assertIn(
                        "the notification itself carries the finding", line
                    )

    def test_morning_digest_always_sends_even_when_nothing_is_flagged(self) -> None:
        for name in CRONTABS:
            with self.subTest(crontab=name):
                line = self._schedule_line(name, "0 9 * * * ", "automations")
                self.assertIn(
                    "Send the report with notify.sh every day, even when no "
                    "host is flagged",
                    line,
                )
                self.assertIn(
                    "when nothing crossed a threshold, say so in one line at "
                    "the top and still send the report",
                    line,
                )
                self.assertNotIn("Call notify.sh only when", line)


class FlagGuardScheduleTests(unittest.TestCase):
    """The hourly fleet digest sets the hard silence guard; the morning digest
    always sends. The guard is the code-level floor under the prompt's silence
    rule."""

    def _schedule_line(self, name: str, schedule: str, target: str) -> str:
        for line in CRONTABS[name].splitlines():
            stripped = line.lstrip("# ")
            if stripped.startswith(schedule) and f"-t {target} " in stripped:
                return stripped
        self.fail(f"{name}: no {schedule!r} line for target {target!r}")

    def test_the_silent_fleet_wide_line_requires_a_flag(self) -> None:
        for name in CRONTABS:
            with self.subTest(crontab=name):
                line = self._schedule_line(name, "15 * * * * ", "automations")
                self.assertIn("NOTIFY_REQUIRE_FLAG=1", line)
                self.assertIn("NOTIFY_PREFETCH=host_stats", line)

    def test_the_morning_digest_always_sends(self) -> None:
        for name in CRONTABS:
            with self.subTest(crontab=name):
                line = self._schedule_line(name, "0 9 * * * ", "automations")
                self.assertNotIn("NOTIFY_REQUIRE_FLAG", line)
                self.assertIn("NOTIFY_PREFETCH=host_stats,ssh", line)

    def test_the_admin_chat_hourly_job_does_not_require_a_flag(self) -> None:
        for name in CRONTABS:
            with self.subTest(crontab=name):
                line = self._schedule_line(name, "5 * * * * ", "admin-chat")
                self.assertNotIn("NOTIFY_REQUIRE_FLAG", line)

    def test_the_prompt_says_the_boundary_refuses_a_message_with_no_flag(
        self,
    ) -> None:
        self.assertIn(
            "when a run is configured to require a flag, `notify.sh` reads the "
            "message before any delivery and refuses to send one that carries "
            "no `[!]` marker",
            FLAT_PROMPT,
        )
        self.assertIn("A refused send is a silent skip, not a failure", FLAT_PROMPT)
        self.assertIn(
            "`[!]` is the only marker the boundary checks", FLAT_PROMPT
        )

    def test_charts_are_per_host_and_never_the_models_choice(self) -> None:
        self.assertIn(
            "The switch is per host in the bot settings and is never yours to "
            "change",
            FLAT_PROMPT,
        )
        self.assertIn(
            "A host whose entry is ticked always gets its chart", FLAT_PROMPT
        )
        self.assertIn("never name, link or embed an image file", FLAT_PROMPT)


if __name__ == "__main__":
    unittest.main()

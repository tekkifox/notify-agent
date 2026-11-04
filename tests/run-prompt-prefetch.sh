#!/bin/sh
# Host-statistics pre-fetch tests for scripts/run-prompt.sh.
#
# A fake MCP server answers the readiness preflight and the `query` tool, and a
# fake `opencode` captures the instruction instead of starting a model. No
# database, credentials or network are used.
#
# Requires: jq.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
run_prompt="$repo_dir/scripts/run-prompt.sh"

command -v jq >/dev/null 2>&1 || {
    printf 'test: jq is required\n' >&2
    exit 1
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/run-prompt-prefetch.XXXXXX")
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

mkdir -p "$tmp/bin"
cat > "$tmp/bin/opencode" <<'SH'
#!/bin/sh
for arg do last=$arg; done
printf '%s' "$last" > "$CAPTURE"
exit 0
SH
chmod +x "$tmp/bin/opencode"

# Fake MCP server. It answers initialize and tools/list like the real one, and
# returns one aggregate row per host named in the query. Failure modes are
# driven by environment variables so each test stays one process.
cat > "$tmp/mcp-prefetch.py" <<'PY'
import json
import os
import re
import sys
import time

if "--selftest" in sys.argv[1:]:
    sys.exit(0)

tools = [
    {"name": "query", "description": "stub", "inputSchema": {"type": "object"}},
    {"name": "search_content", "description": "stub", "inputSchema": {"type": "object"}},
]


def write(message):
    print(json.dumps(message), flush=True)


for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")

    if method == "initialize":
        write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "stub", "version": "1"},
                },
            }
        )
        continue

    if method == "tools/list":
        write({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        continue

    if method != "tools/call":
        continue

    arguments = request.get("params", {}).get("arguments", {})
    sql = arguments.get("sql", "")
    sql_log = os.environ.get("SQL_LOG")
    if sql_log:
        with open(sql_log, "a", encoding="utf-8") as handle:
            handle.write(sql.replace("\n", " ") + "\n")

    if os.environ.get("PREFETCH_STDOUT"):
        print(os.environ["PREFETCH_STDOUT"], flush=True)

    if os.environ.get("PREFETCH_SLEEP"):
        time.sleep(float(os.environ["PREFETCH_SLEEP"]))

    if os.environ.get("PREFETCH_FAIL"):
        text = json.dumps(
            {"error": "oracle query failed", "code": None, "retryable": False}
        )
        write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": True,
                },
            }
        )
        continue

    if os.environ.get("PREFETCH_CORES_MISSING") and "cpu_cores" in sql:
        text = json.dumps(
            {
                "error": "oracle error ORA-00904",
                "code": "ORA-00904",
                "retryable": False,
            }
        )
        write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": True,
                },
            }
        )
        continue

    match = re.search(r"host_label IN \(([^)]*)\)", sql)
    hosts = []
    if match:
        hosts = [
            part.strip().strip("'")
            for part in match.group(1).split(",")
            if part.strip()
        ]

    columns = [
        "host_label",
        "sample_count",
        "last_seen",
        "cpu_avg",
        "cpu_peak",
        "memory_avg",
        "load_avg",
        "load_peak",
    ]
    if "cpu_cores" in sql:
        columns.append("cpu_cores")
    columns.append("disk_latest")
    columns.append("failed_logins_latest")
    columns.append("failed_login_users_latest")
    columns.append("ssh_logins_latest")
    columns.append("ssh_login_users_latest")
    columns.append("ssh_logins_today_latest")
    columns.append("ssh_login_users_today_latest")
    columns.append("ssh_logins_today_since_latest")
    extra = os.environ.get("PREFETCH_EXTRA_COLUMN")
    if extra:
        columns.append(extra)

    def latest_with_value(column, good):
        """Emulate the aggregate over a two-sample window for one column.

        The window holds an older sample with a value and a newer sample from
        the second writer without one. Oracle sorts NULLS LAST by default, so
        ``KEEP (DENSE_RANK LAST ORDER BY CASE WHEN <column> IS NOT NULL THEN
        sampled_at END)`` would pick the newer null row; only ``NULLS FIRST``
        puts that row first and leaves the latest good sample as the last rank.
        """
        window = os.environ.get("PREFETCH_FAILED_LOGIN_WINDOW", "good")
        if window == "all-null":
            return None
        if window == "null-newer":
            skip_nulls = (
                "CASE WHEN " + column + " IS NOT NULL THEN sampled_at END"
                " NULLS FIRST"
            )
            return good if skip_nulls in sql else None
        return good

    rows = []
    for host in hosts:
        row = [host, 23, "2026-11-03T08:00:00+00:00", 12.5, 40.0, 33.3, 0.5, 1.2]
        if "cpu_cores" in sql:
            row.append(4)
        row.append(55.5)
        row.append(latest_with_value("failed_logins", 12))
        row.append(
            latest_with_value(
                "failed_login_users",
                '[{"user":"root","count":9},{"user":"admin","count":3}]',
            )
        )
        row.append(latest_with_value("ssh_logins", 12))
        row.append(
            latest_with_value(
                "ssh_login_users",
                '[{"user":"root","count":9},{"user":"ross","count":3}]',
            )
        )
        row.append(latest_with_value("ssh_logins_today", 497))
        row.append(
            latest_with_value(
                "ssh_login_users_today",
                '[{"user":"root","count":300},{"user":"ross","count":197}]',
            )
        )
        row.append(
            latest_with_value("ssh_logins_today_since", "2026-11-03T00:00:00+00:00")
        )
        if extra:
            row.append("passthrough-value")
        rows.append(row)

    text = json.dumps(
        {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": False,
        }
    )
    write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            },
        }
    )
PY

targets='{"automations":{"channel_id":"555555555555555555","host_scope":["host1.example.com","host2.example.com"]},"general":{"channel_id":"111111111111111111"}}'

failures=0
pass() { printf 'ok   - %s\n' "$1"; }
no() { printf 'FAIL - %s\n' "$1" >&2; failures=$((failures + 1)); }

contains() {
    case "$1" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

# run_case NAME TARGET [ENV=VALUE ...]
# Leaves rc, the captured instruction, stderr, the SQL log and the transcript
# in $tmp/NAME.*.
run_case() {
    name=$1
    target=$2
    shift 2
    rm -f "$tmp/$name" "$tmp/$name.stderr" "$tmp/$name.sql" "$tmp/$name.stdout"
    rm -rf "$tmp/$name.data"
    set +e
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        NOTIFY_MCP_SERVER="$tmp/mcp-prefetch.py" \
        NOTIFY_PREFLIGHT_RETRY_DELAY=0 \
        NOTIFY_TARGETS="$targets" \
        XDG_DATA_HOME="$tmp/$name.data" \
        CAPTURE="$tmp/$name" \
        SQL_LOG="$tmp/$name.sql" \
        "$@" \
        "$run_prompt" -t "$target" "Compose the status message." \
        >"$tmp/$name.stdout" 2>"$tmp/$name.stderr"
    rc=$?
    set -e
}

# Control instruction with no pre-fetch. Failure and skip cases must match it
# byte for byte.
run_case control automations
run_case control-unscoped general

# 1. A scoped target gets one aggregate row per in-scope host, the instruction
#    tells the model not to call oracle_query, and the run summary records it.
run_case scoped automations NOTIFY_PREFETCH=host_stats
prompt=$(cat "$tmp/scoped" 2>/dev/null || true)
err=$(cat "$tmp/scoped.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "scoped: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "scoped: instruction did not carry the statistics section"
elif ! contains "$prompt" '"window_minutes":70'; then
    no "scoped: instruction did not carry the default window"
elif ! contains "$prompt" '"host_label":"host1.example.com"'; then
    no "scoped: instruction lost the first host"
elif ! contains "$prompt" '"host_label":"host2.example.com"'; then
    no "scoped: instruction lost the second host"
elif ! contains "$prompt" "Do not call oracle_query for host statistics in this run"; then
    no "scoped: instruction did not tell the model the data is complete"
elif ! contains "$prompt" "SSH status is still yours to collect"; then
    no "scoped: instruction did not leave SSH to the model"
elif ! grep -Fq '"failed_logins_latest":12' "$tmp/scoped"; then
    no "scoped: instruction lost the latest failed-login count"
elif ! grep -Fq '"failed_login_users_latest":"[{\"user\":\"root\",\"count\":9},{\"user\":\"admin\",\"count\":3}]"' "$tmp/scoped"; then
    no "scoped: instruction lost the latest failed-login users"
elif ! grep -q "failed_logins).*KEEP (DENSE_RANK LAST ORDER BY CASE WHEN failed_logins IS NOT NULL THEN sampled_at END NULLS FIRST) AS failed_logins_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query does not take the latest failed-login value"
elif ! grep -q "failed_login_users).*KEEP (DENSE_RANK LAST ORDER BY CASE WHEN failed_login_users IS NOT NULL THEN sampled_at END NULLS FIRST) AS failed_login_users_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query does not take the latest failed-login users"
elif ! contains "$err" "run-prompt: prefetch: 2 host(s), window 70m"; then
    no "scoped: success was not logged"
elif ! contains "$err" 'prefetch=ok "'; then
    no "scoped: run summary did not record prefetch=ok before the excerpt"
elif ! grep -q "cpu_cores" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not ask for cpu_cores"
elif ! grep -q "host_label IN ('host1.example.com','host2.example.com')" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not restrict itself to the delivery scope"
elif contains "$prompt" "host3.example.com"; then
    no "scoped: instruction carried a host outside the scope"
else
    pass "a scoped target receives one aggregate row per host and no tool call"
fi

# 2. An unscoped target skips the pre-fetch with a log line and never queries
#    every host.
run_case unscoped general NOTIFY_PREFETCH=host_stats
prompt=$(cat "$tmp/unscoped" 2>/dev/null || true)
err=$(cat "$tmp/unscoped.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "unscoped: run-prompt.sh failed: $err"
elif contains "$prompt" "## Host statistics for this run"; then
    no "unscoped: instruction carried statistics"
elif ! contains "$err" "run-prompt: prefetch skipped: target has no host scope"; then
    no "unscoped: skip was not logged"
elif ! contains "$err" "prefetch=skipped"; then
    no "unscoped: run summary did not record prefetch=skipped"
elif [ -s "$tmp/unscoped.sql" ]; then
    no "unscoped: the pre-fetch queried despite having no scope"
elif [ "$prompt" != "$(cat "$tmp/control-unscoped")" ]; then
    no "unscoped: instruction changed"
else
    pass "an unscoped target skips the pre-fetch and queries nothing"
fi

# 3. A failing fetch logs one line, leaves the instruction byte-identical and
#    still runs OpenCode, so the model can fall back to the tool.
run_case failing automations NOTIFY_PREFETCH=host_stats \
    PREFETCH_FAIL=1 PREFETCH_STDOUT=SECRET_SERVER_STDOUT
prompt=$(cat "$tmp/failing" 2>/dev/null || true)
err=$(cat "$tmp/failing.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "failing: a failed pre-fetch failed the run: $err"
elif contains "$prompt" "## Host statistics for this run"; then
    no "failing: instruction carried a partial section"
elif [ "$prompt" != "$(cat "$tmp/control")" ]; then
    no "failing: instruction was not left untouched"
elif ! contains "$err" "run-prompt: prefetch failed: oracle query failed"; then
    no "failing: failure was not logged with the greppable prefix"
elif ! contains "$err" "prefetch=failed"; then
    no "failing: run summary did not record prefetch=failed"
elif contains "$err" "SECRET_SERVER_STDOUT"; then
    no "failing: the server's stdout reached the log"
else
    pass "a failed fetch leaves the instruction untouched and still runs the agent"
fi

# 4. ORA-00904 retries once without cpu_cores, logs the retry and continues.
run_case cores automations NOTIFY_PREFETCH=host_stats PREFETCH_CORES_MISSING=1
prompt=$(cat "$tmp/cores" 2>/dev/null || true)
err=$(cat "$tmp/cores.stderr" 2>/dev/null || true)
queries=$(wc -l < "$tmp/cores.sql" 2>/dev/null | tr -d ' ' || printf '0')
if [ "$rc" -ne 0 ]; then
    no "ORA-00904: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "ORA-00904: the retry did not produce statistics"
elif ! contains "$err" "run-prompt: prefetch: cpu_cores not exposed yet"; then
    no "ORA-00904: the retry was not logged"
elif ! contains "$err" "prefetch=ok"; then
    no "ORA-00904: run summary did not record prefetch=ok"
elif [ "$queries" != "2" ]; then
    no "ORA-00904: expected two queries, got $queries"
elif ! sed -n '1p' "$tmp/cores.sql" | grep -q "cpu_cores"; then
    no "ORA-00904: the first query did not ask for cpu_cores"
elif sed -n '2p' "$tmp/cores.sql" | grep -q "cpu_cores"; then
    no "ORA-00904: the retry still asked for cpu_cores"
else
    pass "ORA-00904 retries without cpu_cores and keeps the digest"
fi

# 5. Unset NOTIFY_PREFETCH changes nothing except the summary field.
run_case off automations
prompt=$(cat "$tmp/off" 2>/dev/null || true)
err=$(cat "$tmp/off.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "unset: run-prompt.sh failed: $err"
elif [ "$prompt" != "$(cat "$tmp/control")" ]; then
    no "unset: instruction changed"
elif contains "$err" "run-prompt: prefetch"; then
    no "unset: a pre-fetch line was logged"
elif ! contains "$err" "prefetch=skipped"; then
    no "unset: run summary did not record prefetch=skipped"
elif [ -s "$tmp/off.sql" ]; then
    no "unset: the pre-fetch queried anyway"
else
    pass "an unset NOTIFY_PREFETCH changes nothing"
fi

# 6. The window override reaches both the SQL and the instruction.
run_case window automations NOTIFY_PREFETCH=host_stats \
    NOTIFY_PREFETCH_WINDOW_MINUTES=1440
prompt=$(cat "$tmp/window" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "window: run-prompt.sh failed: $(cat "$tmp/window.stderr")"
elif ! contains "$prompt" '"window_minutes":1440'; then
    no "window: instruction did not carry the override"
elif ! grep -q "NUMTODSINTERVAL(1440, 'MINUTE')" "$tmp/window.sql" 2>/dev/null; then
    no "window: SQL did not carry the override"
else
    pass "NOTIFY_PREFETCH_WINDOW_MINUTES reaches the SQL and the instruction"
fi

# 7. A hung server is bounded by NOTIFY_PREFETCH_TIMEOUT_SECONDS and the run
#    falls back to the tool.
run_case timeout automations NOTIFY_PREFETCH=host_stats \
    NOTIFY_PREFETCH_TIMEOUT_SECONDS=1 PREFETCH_SLEEP=5
prompt=$(cat "$tmp/timeout" 2>/dev/null || true)
err=$(cat "$tmp/timeout.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "timeout: a hung pre-fetch failed the run: $err"
elif contains "$prompt" "## Host statistics for this run"; then
    no "timeout: instruction carried statistics"
elif ! contains "$err" "run-prompt: prefetch failed: query timed out after 1 seconds"; then
    no "timeout: the deadline was not reported"
elif ! contains "$err" "prefetch=failed"; then
    no "timeout: run summary did not record prefetch=failed"
else
    pass "a hung pre-fetch is bounded and leaves the run on the tool"
fi

# 8. The parser is a generic column-to-key pass-through: a column the query did
#    not name still reaches the per-host object, so adding a view column needs
#    no parser change.
run_case passthrough automations NOTIFY_PREFETCH=host_stats \
    PREFETCH_EXTRA_COLUMN=unexpected_column
prompt=$(cat "$tmp/passthrough" 2>/dev/null || true)
err=$(cat "$tmp/passthrough.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "passthrough: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "passthrough: instruction did not carry the statistics section"
elif ! grep -Fq '"unexpected_column":"passthrough-value"' "$tmp/passthrough"; then
    no "passthrough: an extra column did not reach the per-host object"
else
    pass "the parser passes unknown columns through unchanged"
fi

# 9. The accepted-login columns use the same null-skipping idiom as the
#    failed-login columns, and the parser passes every value through unchanged.
run_case logins automations NOTIFY_PREFETCH=host_stats
prompt=$(cat "$tmp/logins" 2>/dev/null || true)
err=$(cat "$tmp/logins.stderr" 2>/dev/null || true)
missing=
for column in ssh_logins ssh_login_users ssh_logins_today \
    ssh_login_users_today ssh_logins_today_since; do
    grep -q "MAX($column) KEEP (DENSE_RANK LAST ORDER BY CASE WHEN $column IS NOT NULL THEN sampled_at END NULLS FIRST) AS ${column}_latest" \
        "$tmp/logins.sql" 2>/dev/null || missing="$missing $column"
done
if [ "$rc" -ne 0 ]; then
    no "logins: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "logins: instruction did not carry the statistics section"
elif [ -n "$missing" ]; then
    no "logins: the query does not take the latest value of$missing with the null-skipping idiom"
elif ! grep -Fq '"ssh_logins_latest":12' "$tmp/logins"; then
    no "logins: instruction lost the last-hour login count"
elif ! grep -Fq '"ssh_login_users_latest":"[{\"user\":\"root\",\"count\":9},{\"user\":\"ross\",\"count\":3}]"' "$tmp/logins"; then
    no "logins: instruction lost the last-hour login users"
elif ! grep -Fq '"ssh_logins_today_latest":497' "$tmp/logins"; then
    no "logins: instruction lost the day's login total"
elif ! grep -Fq '"ssh_login_users_today_latest":"[{\"user\":\"root\",\"count\":300},{\"user\":\"ross\",\"count\":197}]"' "$tmp/logins"; then
    no "logins: instruction lost the day's login users"
elif ! grep -Fq '"ssh_logins_today_since_latest":"2026-11-03T00:00:00+00:00"' "$tmp/logins"; then
    no "logins: instruction lost the day's start time"
else
    pass "accepted logins are pre-fetched for every host with the null-skipping idiom"
fi

# 10. A second, older writer inserts samples without the failed-login or
#     accepted-login columns, interleaved with the good three-minute samples. A
#     newer null sample must not hide the latest value, and a window of all
#     nulls must stay null.
run_case null_newer automations NOTIFY_PREFETCH=host_stats \
    PREFETCH_FAILED_LOGIN_WINDOW=null-newer
prompt=$(cat "$tmp/null_newer" 2>/dev/null || true)
err=$(cat "$tmp/null_newer.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "null newer: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "null newer: instruction did not carry the statistics section"
elif ! grep -Fq '"failed_logins_latest":12' "$tmp/null_newer"; then
    no "null newer: a newer null sample hid the latest failed-login count"
elif ! grep -Fq '"failed_login_users_latest":"[{\"user\":\"root\",\"count\":9},{\"user\":\"admin\",\"count\":3}]"' "$tmp/null_newer"; then
    no "null newer: a newer null sample hid the latest failed-login users"
elif ! grep -q "CASE WHEN failed_logins IS NOT NULL THEN sampled_at END NULLS FIRST" "$tmp/null_newer.sql" 2>/dev/null; then
    no "null newer: the query does not order null samples before the good ones"
elif ! grep -q "CASE WHEN failed_login_users IS NOT NULL THEN sampled_at END NULLS FIRST" "$tmp/null_newer.sql" 2>/dev/null; then
    no "null newer: the users column does not order null samples first"
elif ! grep -Fq '"ssh_logins_latest":12' "$tmp/null_newer"; then
    no "null newer: a newer null sample hid the last-hour login count"
elif ! grep -Fq '"ssh_logins_today_latest":497' "$tmp/null_newer"; then
    no "null newer: a newer null sample hid the day's login total"
else
    pass "a null sample newer than the latest good one does not hide it"
fi

run_case all_null automations NOTIFY_PREFETCH=host_stats \
    PREFETCH_FAILED_LOGIN_WINDOW=all-null
prompt=$(cat "$tmp/all_null" 2>/dev/null || true)
err=$(cat "$tmp/all_null.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "all null: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "all null: instruction did not carry the statistics section"
elif ! grep -Fq '"failed_logins_latest":null' "$tmp/all_null"; then
    no "all null: the failed-login count was not null"
elif ! grep -Fq '"failed_login_users_latest":null' "$tmp/all_null"; then
    no "all null: the failed-login users were not null"
elif ! grep -Fq '"ssh_logins_latest":null' "$tmp/all_null"; then
    no "all null: the last-hour login count was not null"
elif ! grep -Fq '"ssh_login_users_latest":null' "$tmp/all_null"; then
    no "all null: the last-hour login users were not null"
elif ! grep -Fq '"ssh_logins_today_latest":null' "$tmp/all_null"; then
    no "all null: the day's login total was not null"
elif ! grep -Fq '"ssh_login_users_today_latest":null' "$tmp/all_null"; then
    no "all null: the day's login users were not null"
elif ! grep -Fq '"ssh_logins_today_since_latest":null' "$tmp/all_null"; then
    no "all null: the day's start time was not null"
else
    pass "a window of all nulls stays null"
fi

if [ "$failures" -ne 0 ]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi

printf 'all run-prompt prefetch tests passed\n'

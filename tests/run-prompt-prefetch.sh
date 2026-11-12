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
cms_pid=
cleanup() {
    if [ -n "$cms_pid" ]; then
        kill "$cms_pid" 2>/dev/null || true
    fi
    rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmp/bin"
cat > "$tmp/bin/opencode" <<'SH'
#!/bin/sh
for arg do last=$arg; done
printf '%s' "$last" > "$CAPTURE"
printf '%s' "${NOTIFY_ALLOWED_HOSTS-}" > "$CAPTURE.allowlist"
printf '%s' "${NOTIFY_ATTACH_DIR-}" > "$CAPTURE.attach"
if [ -n "${NOTIFY_ATTACH_DIR-}" ] && [ -d "${NOTIFY_ATTACH_DIR}" ]; then
    find "$NOTIFY_ATTACH_DIR" -maxdepth 1 -type f -name '*.png' -printf '%f\n' 2>/dev/null \
        | sort > "$CAPTURE.files"
    first=$(find "$NOTIFY_ATTACH_DIR" -maxdepth 1 -type f -name '*.png' 2>/dev/null \
        | sort | head -n 1)
    if [ -n "$first" ]; then
        od -An -tx1 -N8 "$first" 2>/dev/null | tr -d ' \n' > "$CAPTURE.signature"
    fi
fi
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

    if os.environ.get("PREFETCH_EXTENDED_MISSING") and "memory_total_bytes" in sql:
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
    extended = "memory_total_bytes" in sql
    if extended:
        columns.extend(
            [
                "swap_avg",
                "swap_peak",
                "memory_total_bytes",
                "memory_used_bytes_latest",
                "process_count_latest",
                "network_rx_mb_latest",
                "network_tx_mb_latest",
                "host_count",
                "updated_at_latest",
            ]
        )
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

    def extended_value(good):
        """Emulate a sample that predates the newer view columns."""
        if os.environ.get("PREFETCH_EXTENDED_WINDOW") == "null":
            return None
        return good

    rows = []
    for host in hosts:
        row = [host, 23, "2026-11-03T08:00:00+00:00", 12.5, 40.0, 33.3, 0.5, 1.2]
        if "cpu_cores" in sql:
            row.append(4)
        if extended:
            row.extend(
                [
                    extended_value(2.3),
                    extended_value(6.1),
                    extended_value(8589934592),
                    extended_value(1395864371),
                    extended_value(165),
                    extended_value(75.0),
                    extended_value(31.0),
                    extended_value(3),
                    extended_value("2026-11-03T08:00:00+00:00"),
                ]
            )
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

# Fake ssh_probe MCP server. It answers initialize and tools/list, and a
# tools/call for `probe` returns one ok result per requested host. No SSH
# connection is made.
cat > "$tmp/mcp-ssh.py" <<'PY'
import json
import os
import sys
import time

if "--selftest" in sys.argv[1:]:
    sys.exit(0)


def write(message):
    print(json.dumps(message), flush=True)


tools = [
    {
        "name": "probe",
        "description": "stub",
        "inputSchema": {"type": "object"},
    }
]

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
                    "serverInfo": {"name": "stub-ssh", "version": "1"},
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
    hosts = arguments.get("hosts", [])
    if not isinstance(hosts, list):
        hosts = []
    log = os.environ.get("SSH_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(",".join(hosts) + "\n")

    if os.environ.get("SSH_PREFETCH_SLEEP"):
        time.sleep(float(os.environ["SSH_PREFETCH_SLEEP"]))

    if os.environ.get("SSH_PREFETCH_FAIL"):
        text = json.dumps({"error": "probe unavailable"})
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

    results = []
    for host in hosts:
        results.append(
            {
                "host": host,
                "status": "ok",
                "message": "connected",
                "attempted": True,
                "latency_ms": 7,
                "failed_logins": 0,
                "logins": "root:1,ross:1",
                "keyhash": "0123456789ab",
                "keys_changed": False,
                "reboot_required": False,
                "outdated_images": "not-checked",
            }
        )
    payload = {
        "configured": True,
        "count": len(results),
        "elapsed_ms": 7,
        "results": results,
    }
    write(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(payload)}
                ],
                "isError": False,
            },
        }
    )
PY

# Fake oracledb for the scheduled chart helper. host_series.py speaks the same
# small surface the real driver uses: defaults, connect(), a cursor with a
# description and rows. CHART_EMPTY and CHART_FAIL drive the failure paths.
mkdir -p "$tmp/pydeps"
cat > "$tmp/pydeps/oracledb.py" <<'PY'
import json
import os


class _Defaults:
    fetch_lobs = False


defaults = _Defaults()


class _Cursor:
    description = []

    def execute(self, sql, binds=None):
        self.binds = binds or {}
        log = os.environ.get("HOST_SERIES_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(self.binds) + "\n")
        if os.environ.get("CHART_FAIL"):
            raise RuntimeError("ORA-00942: table or view does not exist")
        hosts = [
            value
            for key, value in sorted(self.binds.items())
            if key.startswith("host")
        ]
        self.description = [
            ("HOST_LABEL",),
            ("BUCKET",),
            ("CPU",),
            ("MEMORY",),
        ]
        self.rows = []
        if not os.environ.get("CHART_EMPTY"):
            for host in hosts:
                for index in range(5):
                    self.rows.append(
                        [
                            host,
                            f"2026-11-11T{8 + index:02d}:00:00+00:00",
                            10.0 + index * 5,
                            30.0 + index * 4,
                        ]
                    )

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Connection:
    call_timeout = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return _Cursor()

    def close(self):
        pass


def connect(**kwargs):
    return _Connection()
PY

# Loopback CMS stub for the "all hosts" case.
cat > "$tmp/cms.py" <<'PY'
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

body = open(sys.argv[1], "rb").read()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Handler)
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    handle.write(str(server.server_address[1]))
server.serve_forever()
PY

targets='{"automations":{"channel_id":"555555555555555555","host_scope":["host1.example.com","host2.example.com"]},"admin-chat":{"channel_id":"777777777777777777","host_scope":["host1.example.com","host2.example.com"]},"general":{"channel_id":"111111111111111111"}}'

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
    rm -f "$tmp/$name.attach" "$tmp/$name.files" "$tmp/$name.signature"
    rm -f "$tmp/$name.series"
    rm -rf "$tmp/$name.data"
    set +e
    env -i \
        PATH="$tmp/bin:$PATH" \
        PYTHONPATH="$tmp/pydeps" \
        ORACLE_USER="test-user" \
        ORACLE_PASSWORD="test-password" \
        ORACLE_DSN="test-dsn" \
        ORACLE_WALLET_DIR="${TMPDIR:-/tmp}" \
        OPENCODE_MODEL="test/model" \
        NOTIFY_MCP_SERVER="$tmp/mcp-prefetch.py" \
        NOTIFY_PREFLIGHT_RETRY_DELAY=0 \
        NOTIFY_TARGETS="$targets" \
        XDG_DATA_HOME="$tmp/$name.data" \
        CAPTURE="$tmp/$name" \
        SQL_LOG="$tmp/$name.sql" \
        HOST_SERIES_LOG="$tmp/$name.series" \
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
elif ! grep -q "AVG(swap_percent) AS swap_avg" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not ask for the swap average"
elif ! grep -q "MAX(memory_total_bytes) AS memory_total_bytes" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not ask for total memory"
elif ! grep -q "MAX(memory_used_bytes) KEEP (DENSE_RANK LAST ORDER BY sampled_at) AS memory_used_bytes_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not take the latest memory used"
elif ! grep -q "MAX(process_count) KEEP (DENSE_RANK LAST ORDER BY sampled_at) AS process_count_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not take the latest process count"
elif ! grep -q "MAX(network_rx_mb) KEEP (DENSE_RANK LAST ORDER BY sampled_at) AS network_rx_mb_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not take the latest network receive figure"
elif ! grep -q "MAX(network_tx_mb) KEEP (DENSE_RANK LAST ORDER BY sampled_at) AS network_tx_mb_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not take the latest network transmit figure"
elif ! grep -q "MAX(host_count) AS host_count" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not ask for the host count"
elif ! grep -q "MAX(updated_at) KEEP (DENSE_RANK LAST ORDER BY sampled_at) AS updated_at_latest" "$tmp/scoped.sql" 2>/dev/null; then
    no "scoped: the query did not take the latest update time"
elif ! grep -Fq '"swap_avg":2.3' "$tmp/scoped"; then
    no "scoped: instruction lost the swap average"
elif ! grep -Fq '"swap_peak":6.1' "$tmp/scoped"; then
    no "scoped: instruction lost the swap peak"
elif ! grep -Fq '"memory_total_bytes":8589934592' "$tmp/scoped"; then
    no "scoped: instruction lost total memory"
elif ! grep -Fq '"memory_used_bytes_latest":1395864371' "$tmp/scoped"; then
    no "scoped: instruction lost memory used"
elif ! grep -Fq '"process_count_latest":165' "$tmp/scoped"; then
    no "scoped: instruction lost the process count"
elif ! grep -Fq '"network_rx_mb_latest":75.0' "$tmp/scoped"; then
    no "scoped: instruction lost network receive"
elif ! grep -Fq '"network_tx_mb_latest":31.0' "$tmp/scoped"; then
    no "scoped: instruction lost network transmit"
elif ! grep -Fq '"host_count":3' "$tmp/scoped"; then
    no "scoped: instruction lost the host count"
elif ! grep -Fq '"updated_at_latest":"2026-11-03T08:00:00+00:00"' "$tmp/scoped"; then
    no "scoped: instruction lost the update time"
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
elif ! sed -n '2p' "$tmp/cores.sql" | grep -q "memory_total_bytes"; then
    no "ORA-00904: the retry dropped the new view columns as well"
else
    pass "ORA-00904 retries without cpu_cores and keeps the digest"
fi

# 4b. A database whose view lacks both cpu_cores and the newer columns still
#     produces a payload: drop cores first, then the extended group.
run_case legacy_view automations NOTIFY_PREFETCH=host_stats \
    PREFETCH_CORES_MISSING=1 PREFETCH_EXTENDED_MISSING=1
prompt=$(cat "$tmp/legacy_view" 2>/dev/null || true)
err=$(cat "$tmp/legacy_view.stderr" 2>/dev/null || true)
queries=$(wc -l < "$tmp/legacy_view.sql" 2>/dev/null | tr -d ' ' || printf '0')
if [ "$rc" -ne 0 ]; then
    no "legacy view: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "legacy view: the fallback did not produce statistics"
elif ! contains "$err" "run-prompt: prefetch: cpu_cores not exposed yet"; then
    no "legacy view: the cpu_cores retry was not logged"
elif ! contains "$err" "run-prompt: prefetch: new host-statistics columns not exposed yet"; then
    no "legacy view: the extended-column retry was not logged"
elif ! contains "$err" "prefetch=ok"; then
    no "legacy view: run summary did not record prefetch=ok"
elif [ "$queries" != "3" ]; then
    no "legacy view: expected three queries, got $queries"
elif sed -n '3p' "$tmp/legacy_view.sql" | grep -qE "cpu_cores|memory_total_bytes"; then
    no "legacy view: the last retry still asked for a column the view lacks"
elif ! grep -Fq '"memory_avg":33.3' "$tmp/legacy_view"; then
    no "legacy view: the fallback lost the existing figures"
else
    pass "an older view still produces a payload without the optional columns"
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

# 10b. A sample older than the new view columns carries nulls for every new
#      figure while the existing figures stay intact.
run_case extended_null automations NOTIFY_PREFETCH=host_stats \
    PREFETCH_EXTENDED_WINDOW=null
prompt=$(cat "$tmp/extended_null" 2>/dev/null || true)
err=$(cat "$tmp/extended_null.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "extended null: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "extended null: instruction did not carry the statistics section"
elif ! grep -Fq '"swap_avg":null' "$tmp/extended_null"; then
    no "extended null: the swap average was not null"
elif ! grep -Fq '"swap_peak":null' "$tmp/extended_null"; then
    no "extended null: the swap peak was not null"
elif ! grep -Fq '"memory_total_bytes":null' "$tmp/extended_null"; then
    no "extended null: total memory was not null"
elif ! grep -Fq '"memory_used_bytes_latest":null' "$tmp/extended_null"; then
    no "extended null: memory used was not null"
elif ! grep -Fq '"process_count_latest":null' "$tmp/extended_null"; then
    no "extended null: the process count was not null"
elif ! grep -Fq '"network_rx_mb_latest":null' "$tmp/extended_null"; then
    no "extended null: network receive was not null"
elif ! grep -Fq '"network_tx_mb_latest":null' "$tmp/extended_null"; then
    no "extended null: network transmit was not null"
elif ! grep -Fq '"host_count":null' "$tmp/extended_null"; then
    no "extended null: the host count was not null"
elif ! grep -Fq '"updated_at_latest":null' "$tmp/extended_null"; then
    no "extended null: the update time was not null"
elif ! grep -Fq '"cpu_avg":12.5' "$tmp/extended_null"; then
    no "extended null: the existing figures were lost"
else
    pass "an older sample keeps the new figures null instead of inventing them"
fi

# 11. NOTIFY_PREFETCH=ssh supplies the probe results before the model starts,
#     so a digest needs no ssh_probe turn. The same scope and server are used.
rm -f "$tmp/ssh.ssh"
run_case ssh automations NOTIFY_PREFETCH=ssh \
    NOTIFY_SSH_MCP_SERVER="$tmp/mcp-ssh.py" SSH_LOG="$tmp/ssh.ssh"
prompt=$(cat "$tmp/ssh" 2>/dev/null || true)
err=$(cat "$tmp/ssh.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "ssh prefetch: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## SSH status for this run"; then
    no "ssh prefetch: instruction did not carry the SSH section"
elif ! contains "$prompt" '"host":"host1.example.com"'; then
    no "ssh prefetch: instruction lost the first host"
elif ! contains "$prompt" '"host":"host2.example.com"'; then
    no "ssh prefetch: instruction lost the second host"
elif ! contains "$prompt" "Do not call ssh_probe in this run"; then
    no "ssh prefetch: instruction did not remove the tool turn"
elif contains "$prompt" "## Host statistics for this run"; then
    no "ssh prefetch: instruction carried statistics that were not requested"
elif ! contains "$err" "run-prompt: ssh prefetch: 2 host(s)"; then
    no "ssh prefetch: success was not logged"
elif ! grep -Fq "host1.example.com,host2.example.com" "$tmp/ssh.ssh" 2>/dev/null; then
    no "ssh prefetch: the probe did not receive the delivery scope"
else
    pass "NOTIFY_PREFETCH=ssh supplies the probe results and skips the tool"
fi

# 12. Both modes in one value supply both sections, and the statistics note
#     stops sending the model to ssh_probe.
rm -f "$tmp/both.ssh"
run_case both automations NOTIFY_PREFETCH=host_stats,ssh \
    NOTIFY_SSH_MCP_SERVER="$tmp/mcp-ssh.py" SSH_LOG="$tmp/both.ssh"
prompt=$(cat "$tmp/both" 2>/dev/null || true)
err=$(cat "$tmp/both.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "both prefetch: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "both prefetch: instruction did not carry the statistics section"
elif ! contains "$prompt" "## SSH status for this run"; then
    no "both prefetch: instruction did not carry the SSH section"
elif ! contains "$prompt" "SSH status is supplied below; do not call ssh_probe for it."; then
    no "both prefetch: the statistics note still sent the model to ssh_probe"
elif ! grep -q "host_label IN ('host1.example.com','host2.example.com')" "$tmp/both.sql" 2>/dev/null; then
    no "both prefetch: the statistics query did not use the delivery scope"
elif ! grep -Fq "host1.example.com,host2.example.com" "$tmp/both.ssh" 2>/dev/null; then
    no "both prefetch: the probe did not receive the delivery scope"
elif ! contains "$err" "run-prompt: ssh prefetch: 2 host(s)"; then
    no "both prefetch: the SSH success was not logged"
else
    pass "NOTIFY_PREFETCH=host_stats,ssh supplies both sections"
fi

# 13. A failed SSH prefetch is logged and never fails the run; the instruction
#     stays byte-identical so the model can still use the tool.
run_case ssh-fail automations NOTIFY_PREFETCH=ssh \
    NOTIFY_SSH_MCP_SERVER="$tmp/mcp-ssh.py" SSH_PREFETCH_FAIL=1
prompt=$(cat "$tmp/ssh-fail" 2>/dev/null || true)
err=$(cat "$tmp/ssh-fail.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "ssh prefetch failure: a failed prefetch failed the run: $err"
elif contains "$prompt" "## SSH status for this run"; then
    no "ssh prefetch failure: instruction carried a partial section"
elif [ "$prompt" != "$(cat "$tmp/control")" ]; then
    no "ssh prefetch failure: instruction was not left untouched"
elif ! contains "$err" "run-prompt: ssh prefetch failed: probe unavailable"; then
    no "ssh prefetch failure: the safe reason was not logged"
else
    pass "a failed SSH prefetch leaves the instruction on the tool"
fi

# 14. With no delivery scope the SSH prefetch skips, exactly like statistics.
run_case ssh-unscoped general NOTIFY_PREFETCH=ssh \
    NOTIFY_SSH_MCP_SERVER="$tmp/mcp-ssh.py" SSH_LOG="$tmp/ssh-unscoped.ssh"
prompt=$(cat "$tmp/ssh-unscoped" 2>/dev/null || true)
err=$(cat "$tmp/ssh-unscoped.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "ssh unscoped: run-prompt.sh failed: $err"
elif contains "$prompt" "## SSH status for this run"; then
    no "ssh unscoped: instruction carried SSH data"
elif ! contains "$err" "run-prompt: ssh prefetch skipped: target has no host scope"; then
    no "ssh unscoped: skip was not logged"
elif [ -s "$tmp/ssh-unscoped.ssh" ]; then
    no "ssh unscoped: the probe was called despite having no scope"
elif [ "$prompt" != "$(cat "$tmp/control-unscoped")" ]; then
    no "ssh unscoped: instruction changed"
else
    pass "an unscoped target skips the SSH prefetch and calls no probe"
fi

# 15. A CMS target with no host_scope resolves to every panel host, which feeds
#     both prefetches and becomes the SSH allowlist.
cms_doc='{"version":1,"targets":{"automations":{"channel_id":"555555555555555555"}},"sources":[],"hosts":[{"name":"host1.example.com","sshPort":2222},{"name":"host2.example.com","sshPort":2200}]}'
printf '%s' "$cms_doc" > "$tmp/cms-body.json"
rm -f "$tmp/cms-port"
python3 "$tmp/cms.py" "$tmp/cms-body.json" "$tmp/cms-port" &
cms_pid=$!
tries=0
while [ ! -s "$tmp/cms-port" ]; do
    tries=$((tries + 1))
    if [ "$tries" -ge 100 ]; then
        break
    fi
    sleep 0.05
done
if [ ! -s "$tmp/cms-port" ]; then
    no "CMS all hosts: loopback stub did not start"
else
    cms_url="http://127.0.0.1:$(cat "$tmp/cms-port")/api/bot-settings"
    rm -f "$tmp/cms-all.ssh"
    run_case cms-all automations NOTIFY_PREFETCH=host_stats,ssh \
        BOT_SETTINGS_URL="$cms_url" \
        BOT_SETTINGS_API_KEY="cms-test-key" \
        XDG_STATE_HOME="$tmp/state-cms" \
        NOTIFY_SSH_MCP_SERVER="$tmp/mcp-ssh.py" \
        SSH_LOG="$tmp/cms-all.ssh"
    prompt=$(cat "$tmp/cms-all" 2>/dev/null || true)
    err=$(cat "$tmp/cms-all.stderr" 2>/dev/null || true)
    if [ "$rc" -ne 0 ]; then
        no "CMS all hosts: run-prompt.sh failed: $err"
    elif ! contains "$err" "config source=cms hosts=2 ssh_ports=2"; then
        no "CMS all hosts: the CMS was not the source"
    elif ! grep -q "host_label IN ('host1.example.com','host2.example.com')" "$tmp/cms-all.sql" 2>/dev/null; then
        no "CMS all hosts: absent host_scope did not reach the statistics query"
    elif ! contains "$prompt" "## SSH status for this run"; then
        no "CMS all hosts: SSH results were not supplied"
    elif ! contains "$prompt" '"host":"host2.example.com"'; then
        no "CMS all hosts: the second panel host was not probed"
    elif [ "$(cat "$tmp/cms-all.allowlist" 2>/dev/null)" != '["host1.example.com","host2.example.com"]' ]; then
        no "CMS all hosts: the panel host list was not exported as the allowlist"
    else
        pass "an absent host_scope resolves to every CMS host for both prefetches"
    fi
fi

# 15b. The CMS hosts array's per-host charts flag reaches the scheduled chart.
if [ -n "$cms_pid" ]; then
    kill "$cms_pid" 2>/dev/null || true
    wait "$cms_pid" 2>/dev/null || true
    cms_pid=
fi
cms_charts='{"version":1,"targets":{"automations":{"channel_id":"555555555555555555"}},"sources":[],"hosts":[{"name":"host1.example.com","sshPort":2222,"charts":true},{"name":"host2.example.com","sshPort":2200}]}'
printf '%s' "$cms_charts" > "$tmp/cms-charts-body.json"
rm -f "$tmp/cms-charts-port"
python3 "$tmp/cms.py" "$tmp/cms-charts-body.json" "$tmp/cms-charts-port" &
cms_pid=$!
tries=0
while [ ! -s "$tmp/cms-charts-port" ]; do
    tries=$((tries + 1))
    if [ "$tries" -ge 100 ]; then
        break
    fi
    sleep 0.05
done
if [ ! -s "$tmp/cms-charts-port" ]; then
    no "CMS charts: loopback stub did not start"
else
    cms_charts_url="http://127.0.0.1:$(cat "$tmp/cms-charts-port")/api/bot-settings"
    run_case cms-charts automations NOTIFY_PREFETCH=host_stats \
        BOT_SETTINGS_URL="$cms_charts_url" \
        BOT_SETTINGS_API_KEY="cms-test-key" \
        XDG_STATE_HOME="$tmp/state-cms-charts"
    prompt=$(cat "$tmp/cms-charts" 2>/dev/null || true)
    err=$(cat "$tmp/cms-charts.stderr" 2>/dev/null || true)
    files=$(cat "$tmp/cms-charts.files" 2>/dev/null || true)
    series_args=$(cat "$tmp/cms-charts.series" 2>/dev/null || true)
    if [ "$rc" -ne 0 ]; then
        no "CMS charts: run-prompt.sh failed: $err"
    elif ! contains "$err" "config source=cms hosts=2 ssh_ports=2"; then
        no "CMS charts: the CMS was not the source"
    elif ! contains "$prompt" "## Host statistics for this run"; then
        no "CMS charts: the digest was not supplied"
    elif ! contains "$files" "host1.example.com.png"; then
        no "CMS charts: the ticked host was not charted"
    elif contains "$files" "host2.example.com.png"; then
        no "CMS charts: an unticked host was charted"
    elif ! contains "$series_args" '"host0": "host1.example.com"'; then
        no "CMS charts: the query did not use the panel's ticked host"
    elif contains "$series_args" "host2.example.com"; then
        no "CMS charts: an unticked panel host reached the query"
    else
        pass "the CMS hosts array's charts flag reaches the scheduled chart"
    fi
fi

# 16. A ticked host is charted with no flag raised, and an unticked host in the
#     same scope is not.
run_case charts admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHART_HOSTS='["host1.example.com"]'
prompt=$(cat "$tmp/charts" 2>/dev/null || true)
err=$(cat "$tmp/charts.stderr" 2>/dev/null || true)
attach=$(cat "$tmp/charts.attach" 2>/dev/null || true)
files=$(cat "$tmp/charts.files" 2>/dev/null || true)
series_args=$(cat "$tmp/charts.series" 2>/dev/null || true)
signature=$(cat "$tmp/charts.signature" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "charts: run-prompt.sh failed: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "charts: instruction lost the statistics section"
elif contains "$prompt" "[!]"; then
    no "charts: the digest was unexpectedly flagged"
elif ! contains "$prompt" "CPU and memory charts are attached to this notification for: \`host1.example.com\`"; then
    no "charts: instruction did not name the charted host"
elif contains "$prompt" "Do not mention, link or name an image file"; then
    no "charts: instruction still told the model not to mention the chart"
elif ! contains "$prompt" "Never name an image file and never add an image URL"; then
    no "charts: instruction stopped forbidding image file names and URLs"
elif ! contains "$files" "host1.example.com.png"; then
    no "charts: the ticked host was not charted"
elif contains "$files" "host2.example.com.png"; then
    no "charts: an unticked host was charted automatically"
elif [ "$signature" != "89504e470d0a1a0a" ]; then
    no "charts: the file is not a PNG"
elif ! contains "$series_args" '"window": 70'; then
    no "charts: the series query did not use the digest window"
elif ! contains "$series_args" '"host0": "host1.example.com"'; then
    no "charts: the chart query did not use the ticked host in scope"
elif contains "$series_args" "host2.example.com"; then
    no "charts: an unticked host reached the series query"
elif ! contains "$err" "run-prompt: charts: 1 image(s) for 70m"; then
    no "charts: success was not logged"
elif [ -e "$attach" ]; then
    no "charts: the chart directory was not cleaned up"
else
    pass "a ticked host is charted with no flag raised and an unticked host is not"
fi

# 16a. Every charted host is named, sorted, in one comma-separated list.
run_case charts_two admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHART_HOSTS='["host2.example.com","host1.example.com"]'
prompt=$(cat "$tmp/charts_two" 2>/dev/null || true)
err=$(cat "$tmp/charts_two.stderr" 2>/dev/null || true)
files=$(cat "$tmp/charts_two.files" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "charts two: run-prompt.sh failed: $err"
elif ! contains "$prompt" "for: \`host1.example.com\`, \`host2.example.com\`."; then
    no "charts two: the notice did not name both charted hosts in order"
elif contains "$prompt" "for: \`host2.example.com\`, \`host1.example.com\`"; then
    no "charts two: the host list was not sorted"
elif ! contains "$files" "host1.example.com.png"; then
    no "charts two: the first host was not charted"
elif ! contains "$files" "host2.example.com.png"; then
    no "charts two: the second host was not charted"
else
    pass "the notice names every charted host in sorted order"
fi

# 16b. A day-long window uses hourly buckets.
run_case charts_window admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHART_HOSTS='["host1.example.com"]' \
    NOTIFY_PREFETCH_WINDOW_MINUTES=1440
err=$(cat "$tmp/charts_window.stderr" 2>/dev/null || true)
files=$(cat "$tmp/charts_window.files" 2>/dev/null || true)
series_args=$(cat "$tmp/charts_window.series" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "charts window: run-prompt.sh failed: $err"
elif ! contains "$series_args" '"window": 1440'; then
    no "charts window: the series query lost the override window"
elif ! contains "$files" "host1.example.com.png"; then
    no "charts window: no chart was written"
else
    pass "a 24-hour chart window reaches the series helper"
fi

# 16c. An empty series is logged and the run still delivers the text.
run_case charts_empty admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHART_HOSTS='["host1.example.com"]' CHART_EMPTY=1
prompt=$(cat "$tmp/charts_empty" 2>/dev/null || true)
err=$(cat "$tmp/charts_empty.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "charts empty: an empty series failed the run: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "charts empty: the digest was lost"
elif contains "$prompt" "CPU and memory charts are attached"; then
    no "charts empty: a chart notice survived an empty series"
elif [ -s "$tmp/charts_empty.attach" ]; then
    no "charts empty: an attach directory was exported with no image"
elif ! contains "$err" "run-prompt: charts: no samples for the requested window"; then
    no "charts empty: the empty series was not logged"
else
    pass "an empty series leaves the run successful and text only"
fi

# 16d. A failed series is logged and the run still delivers the text.
run_case charts_fail admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHART_HOSTS='["host1.example.com"]' CHART_FAIL=1
prompt=$(cat "$tmp/charts_fail" 2>/dev/null || true)
err=$(cat "$tmp/charts_fail.stderr" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "charts fail: a failed series failed the run: $err"
elif ! contains "$prompt" "## Host statistics for this run"; then
    no "charts fail: the digest was lost"
elif contains "$prompt" "CPU and memory charts are attached"; then
    no "charts fail: a chart notice survived a failed render"
elif [ -s "$tmp/charts_fail.attach" ]; then
    no "charts fail: an attach directory was exported after a failure"
elif ! contains "$err" "run-prompt: charts failed: host_series: oracle error ORA-00942"; then
    no "charts fail: the failure was not logged"
else
    pass "a failed series leaves the run successful and text only"
fi

# 16e. An absent host chart list is off, even after a successful pre-fetch.
run_case charts_absent admin-chat NOTIFY_PREFETCH=host_stats
prompt=$(cat "$tmp/charts_absent" 2>/dev/null || true)
err=$(cat "$tmp/charts_absent.stderr" 2>/dev/null || true)
chart_queries=$(wc -l < "$tmp/charts_absent.sql" 2>/dev/null | tr -d ' ' || printf '0')
if [ "$rc" -ne 0 ]; then
    no "charts absent: run-prompt.sh failed: $err"
elif [ "$chart_queries" != "1" ]; then
    no "charts absent: a chart query ran without a ticked host"
elif contains "$prompt" "CPU and memory charts are attached"; then
    no "charts absent: a chart notice appeared with charts disabled"
elif [ -s "$tmp/charts_absent.attach" ]; then
    no "charts absent: an attach directory was exported"
else
    pass "an absent per-host charts flag is off"
fi

# 16f. A ticked host outside the chat scope is not charted.
run_case charts_outside admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHART_HOSTS='["host9.example.com"]'
err=$(cat "$tmp/charts_outside.stderr" 2>/dev/null || true)
chart_queries=$(wc -l < "$tmp/charts_outside.sql" 2>/dev/null | tr -d ' ' || printf '0')
if [ "$rc" -ne 0 ]; then
    no "charts outside: run-prompt.sh failed: $err"
elif [ "$chart_queries" != "1" ]; then
    no "charts outside: a chart query ran for an out-of-scope host"
elif [ -s "$tmp/charts_outside.attach" ]; then
    no "charts outside: an attach directory was exported"
else
    pass "a ticked host outside the chat scope is not charted"
fi

# 16g. NOTIFY_CHARTS=1 is a testing override: chart the whole chat scope.
run_case charts_override_on admin-chat NOTIFY_PREFETCH=host_stats NOTIFY_CHARTS=1
err=$(cat "$tmp/charts_override_on.stderr" 2>/dev/null || true)
files=$(cat "$tmp/charts_override_on.files" 2>/dev/null || true)
series_args=$(cat "$tmp/charts_override_on.series" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "charts override on: run-prompt.sh failed: $err"
elif ! contains "$files" "host1.example.com.png"; then
    no "charts override on: the first scoped host was not charted"
elif ! contains "$files" "host2.example.com.png"; then
    no "charts override on: the second scoped host was not charted"
elif ! contains "$series_args" '"host0": "host1.example.com"'; then
    no "charts override on: the first scoped host was not queried"
elif ! contains "$series_args" '"host1": "host2.example.com"'; then
    no "charts override on: the second scoped host was not queried"
elif ! contains "$err" "run-prompt: charts: 2 image(s) for 70m"; then
    no "charts override on: success was not logged"
else
    pass "NOTIFY_CHARTS=1 charts every host in the chat scope"
fi

# 16h. NOTIFY_CHARTS=0 is a testing override: no chart even for a ticked host.
run_case charts_override_off admin-chat NOTIFY_PREFETCH=host_stats \
    NOTIFY_CHARTS=0 NOTIFY_CHART_HOSTS='["host1.example.com"]'
chart_queries=$(wc -l < "$tmp/charts_override_off.sql" 2>/dev/null | tr -d ' ' || printf '0')
if [ "$rc" -ne 0 ]; then
    no "charts override off: run-prompt.sh failed: $(cat "$tmp/charts_override_off.stderr")"
elif [ "$chart_queries" != "1" ]; then
    no "charts override off: a chart query ran despite NOTIFY_CHARTS=0"
elif [ -s "$tmp/charts_override_off.attach" ]; then
    no "charts override off: an attach directory was exported"
else
    pass "NOTIFY_CHARTS=0 disables charts even for a ticked host"
fi

# 16i. Without NOTIFY_CHARTS nothing is attached, even when a pre-fetch runs.
if [ -s "$tmp/scoped.attach" ]; then
    no "charts off: a chart was attached without NOTIFY_CHARTS"
else
    pass "NOTIFY_CHARTS unset attaches nothing"
fi
if [ "$failures" -ne 0 ]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi

printf 'all run-prompt prefetch tests passed\n'

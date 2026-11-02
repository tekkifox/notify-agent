#!/bin/sh
# Preflight retry and failure-notification tests for scripts/run-prompt.sh.
#
# A fake MCP server fails its self-test a configurable number of times. The
# real notify.sh is copied next to run-prompt.sh and its curl is stubbed, so
# the target allowlist and host_scope guard are exercised without a network.
#
# Requires: jq.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
run_prompt_src="$repo_dir/scripts/run-prompt.sh"
notify_src="$repo_dir/scripts/notify.sh"

command -v jq >/dev/null 2>&1 || {
    printf 'test: jq is required\n' >&2
    exit 1
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/run-prompt-preflight.XXXXXX")
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

mkdir -p "$tmp/bin" "$tmp/app/scripts"
cp "$run_prompt_src" "$tmp/app/scripts/run-prompt.sh"
cp "$notify_src" "$tmp/app/scripts/notify.sh"
chmod +x "$tmp/app/scripts/run-prompt.sh" "$tmp/app/scripts/notify.sh"

# Fake opencode: record that it ran and succeed without calling a model.
cat > "$tmp/bin/opencode" <<'SH'
#!/bin/sh
if [ -n "${OPENCODE_CALLS:-}" ]; then
    : > "$OPENCODE_CALLS"
fi
exit 0
SH
chmod +x "$tmp/bin/opencode"

# Fake curl: capture the Composio payload on stdin and report HTTP 200. The
# real notify.sh builds the request, so its target and host_scope checks run.
cat > "$tmp/bin/curl" <<'SH'
#!/bin/sh
if [ -n "${CURL_COUNT:-}" ]; then
    n=0
    if [ -f "$CURL_COUNT" ]; then n=$(cat "$CURL_COUNT"); fi
    n=$((n + 1))
    printf '%s' "$n" > "$CURL_COUNT"
fi
if [ -n "${CURL_BODY:-}" ]; then
    cat > "$CURL_BODY"
else
    cat > /dev/null
fi
printf '200'
SH
chmod +x "$tmp/bin/curl"

# Minimal MCP stub. Every invocation is counted, including the handshake. The
# self-test fails the first $SELFTEST_FAILS times and prints a safe reason.
cat > "$tmp/mcp-flaky.py" <<'PY'
import json
import os
import sys

starts = os.environ.get("SERVER_STARTS")
if starts:
    n = 0
    try:
        with open(starts, encoding="utf-8") as handle:
            n = int(handle.read() or "0")
    except OSError:
        n = 0
    with open(starts, "w", encoding="utf-8") as handle:
        handle.write(str(n + 1))

if "--selftest" in sys.argv[1:]:
    counter = os.environ["SELFTEST_COUNTER"]
    n = 0
    try:
        with open(counter, encoding="utf-8") as handle:
            n = int(handle.read() or "0")
    except OSError:
        n = 0
    n += 1
    with open(counter, "w", encoding="utf-8") as handle:
        handle.write(str(n))
    if n <= int(os.environ.get("SELFTEST_FAILS", "0")):
        print(
            os.environ.get(
                "SELFTEST_REASON", "selftest failed: oracle error DPY-6005"
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    sys.exit(0)

tools = [
    {"name": "query", "description": "stub", "inputSchema": {"type": "object"}},
    {"name": "search_content", "description": "stub", "inputSchema": {"type": "object"}},
]
for line in sys.stdin:
    if not line.strip():
        continue
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": tools}
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
PY

targets='{"automations":{"channel_id":"555555555555555555","host_scope":["host1.example.com"]}}'

failures=0
pass() { printf 'ok   - %s\n' "$1"; }
no() { printf 'FAIL - %s\n' "$1" >&2; failures=$((failures + 1)); }

# run_case FAILS REASON [ENV=VALUE ...]
# Leaves rc, stderr, stdout, counters and the captured payload in $tmp.
run_case() {
    fails=$1
    reason=$2
    shift 2
    rm -f "$tmp/counter" "$tmp/calls" "$tmp/curl-count" "$tmp/curl-body" \
        "$tmp/starts" "$tmp/stdout" "$tmp/stderr"
    rm -rf "$tmp/state"
    mkdir -p "$tmp/state"
    set +e
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        NOTIFY_MCP_SERVER="$tmp/mcp-flaky.py" \
        NOTIFY_PREFLIGHT_RETRY_DELAY=0 \
        NOTIFY_TARGETS="$targets" \
        COMPOSIO_API_KEY="test-key" \
        COMPOSIO_TOOL="SEND_MESSAGE" \
        COMPOSIO_MESSAGE_FIELD="content" \
        SELFTEST_COUNTER="$tmp/counter" \
        SELFTEST_FAILS="$fails" \
        SELFTEST_REASON="$reason" \
        SERVER_STARTS="$tmp/starts" \
        OPENCODE_CALLS="$tmp/calls" \
        CURL_COUNT="$tmp/curl-count" \
        CURL_BODY="$tmp/curl-body" \
        XDG_STATE_HOME="$tmp/state" \
        "$@" \
        "$tmp/app/scripts/run-prompt.sh" -t automations \
        "Compose the morning digest." >"$tmp/stdout" 2>"$tmp/stderr"
    rc=$?
    set -e
    selftests=$(cat "$tmp/counter" 2>/dev/null || printf '0')
    starts=$(cat "$tmp/starts" 2>/dev/null || printf '0')
    requests=$(cat "$tmp/curl-count" 2>/dev/null || printf '0')
}

contains() {
    case "$1" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

# 1. A transient self-test failure is logged, retried once and then the run
#    continues to OpenCode. There is no failure notification.
run_case 1 "selftest failed: oracle error DPY-6005" \
    ORACLE_PASSWORD="preflight-secret" ORACLE_DSN="preflight-dsn"
err=$(cat "$tmp/stderr")
if [ "$rc" -ne 0 ]; then
    no "transient: expected rc 0 got $rc: $err"
elif [ "$selftests" != "2" ]; then
    no "transient: expected 2 self-tests got $selftests"
elif [ "$starts" != "4" ]; then
    no "transient: expected 2 handshakes and 2 self-tests got $starts starts"
elif [ ! -e "$tmp/calls" ]; then
    no "transient: opencode did not run after the retry succeeded"
elif [ "$requests" != "0" ]; then
    no "transient: a failure notification was sent ($requests requests)"
elif ! contains "$err" "retrying once"; then
    no "transient: the retry was not logged"
elif ! contains "$err" "MCP preflight Oracle self-test reason: selftest failed: oracle error DPY-6005"; then
    no "transient: the safe self-test reason was not logged"
elif contains "$err" "preflight-secret" || contains "$err" "preflight-dsn"; then
    no "transient: the log leaked the environment"
else
    pass "a transient self-test failure is logged and retried once"
fi

# 2. A persistent self-test failure stops before OpenCode and sends one short
#    message through notify.sh to the same target: no statistics, no secrets.
run_case 99 "selftest failed: oracle error DPY-6005" \
    ORACLE_PASSWORD="preflight-secret" ORACLE_DSN="preflight-dsn"
err=$(cat "$tmp/stderr")
out=$(cat "$tmp/stdout")
body=$(cat "$tmp/curl-body" 2>/dev/null || true)
if [ "$rc" -eq 0 ]; then
    no "persistent: expected a non-zero exit"
elif [ "$selftests" != "2" ]; then
    no "persistent: expected exactly 2 self-tests got $selftests"
elif [ "$starts" != "4" ]; then
    no "persistent: expected exactly 2 preflights got $starts starts"
elif [ -e "$tmp/calls" ]; then
    no "persistent: opencode ran despite the failed retry"
elif [ "$requests" != "1" ]; then
    no "persistent: expected one failure notification got $requests"
elif [ -z "$body" ]; then
    no "persistent: notify.sh did not build a request"
elif [ "$(printf '%s' "$body" | jq -r '.arguments.channel_id // empty')" != "555555555555555555" ]; then
    no "persistent: notification was not sent to the configured target"
elif ! printf '%s' "$body" | jq -er '.arguments.content | contains("database was unreachable")' >/dev/null 2>&1; then
    no "persistent: notification did not say the database was unreachable"
elif ! printf '%s' "$body" | jq -er '.arguments.content | contains("DPY-6005")' >/dev/null 2>&1; then
    no "persistent: notification did not carry the safe reason"
elif printf '%s' "$body" | jq -er '.arguments.content | test("[0-9]+%")' >/dev/null 2>&1; then
    no "persistent: notification invented a statistic"
elif contains "$err" "preflight-secret" || contains "$err" "preflight-dsn" \
    || contains "$out" "preflight-secret" || contains "$out" "preflight-dsn" \
    || contains "$body" "preflight-secret" || contains "$body" "preflight-dsn"; then
    no "persistent: the failure path leaked the environment"
elif ! contains "$err" "sent failure notification to target automations"; then
    no "persistent: the send was not logged"
else
    pass "a persistent failure sends one reason-only message to the same target"
fi

# 3. A wallet filename in the reason looks like a hostname. It is masked in the
#    notification so the target's host_scope guard still lets the failure
#    message through, while the container log keeps the full safe reason.
run_case 99 "selftest failed: Oracle wallet directory is empty; expected ewallet.pem (thin mode), cwallet.sso, or ewallet.p12"
err=$(cat "$tmp/stderr")
body=$(cat "$tmp/curl-body" 2>/dev/null || true)
message=$(printf '%s' "$body" | jq -r '.arguments.content // empty' 2>/dev/null || true)
if [ "$rc" -eq 0 ]; then
    no "scrub: expected a non-zero exit"
elif [ "$requests" != "1" ]; then
    no "scrub: the host_scope guard refused the failure notification"
elif ! contains "$message" "[redacted]"; then
    no "scrub: host-like tokens were not masked in the notification"
elif contains "$message" "ewallet.pem" || contains "$message" "cwallet.sso"; then
    no "scrub: a host-like wallet filename reached the notification"
elif ! contains "$err" "MCP preflight Oracle self-test reason: selftest failed: Oracle wallet directory is empty"; then
    no "scrub: the container log lost the full safe reason"
else
    pass "host-like tokens are masked for the scope guard but kept in the log"
fi

# 4. The happy path still runs one handshake, one self-test and one OpenCode
#    attempt, with no retry line and no failure notification.
run_case 0 "unused"
err=$(cat "$tmp/stderr")
if [ "$rc" -ne 0 ]; then
    no "healthy: expected rc 0 got $rc: $err"
elif [ "$selftests" != "1" ]; then
    no "healthy: expected 1 self-test got $selftests"
elif [ "$starts" != "2" ]; then
    no "healthy: expected one handshake and one self-test got $starts starts"
elif [ ! -e "$tmp/calls" ]; then
    no "healthy: opencode did not run"
elif [ "$requests" != "0" ]; then
    no "healthy: a failure notification was sent"
elif contains "$err" "retrying once" || contains "$err" "Oracle self-test reason"; then
    no "healthy: the preflight logged a retry or a reason"
else
    pass "the healthy path is unchanged: one handshake, one self-test"
fi

if [ "$failures" -ne 0 ]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi

printf 'all run-prompt preflight tests passed\n'

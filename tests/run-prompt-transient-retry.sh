#!/bin/sh
# Transient transport/model-run retry tests for scripts/run-prompt.sh.
#
# A transient failure (for example ECONNRESET or the socket closing
# unexpectedly) is retried a small, bounded number of times while nothing has
# been sent, never after a confirmed send, and never in a way that masks a
# non-transient failure.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
run_prompt="$repo_dir/scripts/run-prompt.sh"

command -v python3 >/dev/null 2>&1 || { printf 'test: python3 required\n' >&2; exit 1; }
command -v jq >/dev/null 2>&1 || { printf 'test: jq required\n' >&2; exit 1; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/run-prompt-transient-retry.XXXXXX")
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

mkdir -p "$tmp/bin"

cat > "$tmp/mcp-good.py" <<'PY'
import json, sys
if "--selftest" in sys.argv[1:]:
    sys.exit(0)
tools=[{"name":"query","description":"stub","inputSchema":{"type":"object"}},
       {"name":"search_content","description":"stub","inputSchema":{"type":"object"}}]
for line in sys.stdin:
    if not line.strip(): continue
    r=json.loads(line); m=r.get("method"); rid=r.get("id")
    if m=="initialize": res={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"stub","version":"1"}}
    elif m=="tools/list": res={"tools":tools}
    else: continue
    print(json.dumps({"jsonrpc":"2.0","id":rid,"result":res}),flush=True)
PY

# Fake opencode driven by SCENARIO and a counter file.
cat > "$tmp/bin/opencode" <<'SH'
#!/bin/sh
n=0
if [ -f "${COUNTER_FILE:?}" ]; then n=$(cat "$COUNTER_FILE"); fi
n=$((n+1))
printf '%s' "$n" > "$COUNTER_FILE"
model=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--model" ]; then model=$arg; fi
    prev=$arg
done
printf '%s\n' "$model" >> "${MODELS_FILE:?}"

case "${SCENARIO:?}" in
    transient-then-ok)
        if [ "$n" -eq 1 ]; then
            printf 'Transport: ECONNRESET: The socket connection was closed unexpectedly\n' >&2
            exit 1
        fi
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'ok\n'
        exit 0
        ;;
    transient-socket-then-ok)
        if [ "$n" -eq 1 ]; then
            printf 'The socket connection was closed unexpectedly\n' >&2
            exit 1
        fi
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'ok\n'
        exit 0
        ;;
    transient-exhausted)
        printf 'Transport: ECONNRESET: The socket connection was closed unexpectedly\n' >&2
        exit 1
        ;;
    transient-sent-no-retry)
        # A transient-looking failure after a confirmed send must never be
        # retried (non-duplication).
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'Transport: ECONNRESET: The socket connection was closed unexpectedly\n' >&2
        exit 1
        ;;
    ordinary-fail)
        printf 'something broke\n' >&2
        exit 1
        ;;
    quota-no-transient-retry)
        printf 'Free usage limit reached\n' >&2
        exit 1
        ;;
    transient-then-quota-fallback)
        if [ "$n" -eq 1 ]; then
            printf 'Transport: ECONNRESET: The socket connection was closed unexpectedly\n' >&2
            exit 1
        fi
        if [ "$n" -eq 2 ]; then
            printf 'Free usage limit reached\n' >&2
            exit 1
        fi
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'ok\n'
        exit 0
        ;;
    fallback-transient-then-ok)
        if [ "$n" -eq 1 ]; then
            printf 'Free usage limit reached\n' >&2
            exit 1
        fi
        if [ "$n" -eq 2 ]; then
            printf 'Transport: ECONNRESET: The socket connection was closed unexpectedly\n' >&2
            exit 1
        fi
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'ok\n'
        exit 0
        ;;
esac
exit 1
SH
chmod +x "$tmp/bin/opencode"

failures=0
pass(){ printf 'ok   - %s\n' "$1"; }
no(){ printf 'FAIL - %s\n' "$1" >&2; failures=$((failures+1)); }

run_once() {
    scenario=$1; fallback=$2; name=$3
    shift 3
    rm -f "$tmp/counter" "$tmp/models"
    : > "$tmp/counter"; : > "$tmp/models"
    state="$tmp/state-$name"
    data="$tmp/data-$name"
    rm -rf "$state" "$data"; mkdir -p "$state" "$data"
    set +e
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        OPENCODE_MODEL_FALLBACK="$fallback" \
        NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
        NOTIFY_RUN_LOCK="$tmp/run-$name.lock" \
        XDG_STATE_HOME="$state" \
        XDG_DATA_HOME="$data" \
        NOTIFY_TRANSIENT_RETRY_DELAY=0 \
        SCENARIO="$scenario" \
        COUNTER_FILE="$tmp/counter" \
        MODELS_FILE="$tmp/models" \
        "$@" \
        "$run_prompt" "Compose a status message." >"$tmp/out-$name" 2>"$tmp/err-$name"
    rc=$?
    set -e
    calls=$(cat "$tmp/counter")
    [ -n "$calls" ] || calls=0
    models=$(cat "$tmp/models")
    err=$(cat "$tmp/err-$name")
}

# 1. A transient transport failure (ECONNRESET) with no send is retried and can
#    succeed, without duplicating the send.
run_once transient-then-ok "fallback/model" t1
if [ "$rc" -ne 0 ]; then no "transient-then-ok: expected exit 0 got $rc ($err)";
elif [ "$calls" -ne 2 ]; then no "transient-then-ok: expected 2 attempts got $calls";
elif printf '%s\n' "$models" | grep -qx 'fallback/model'; then no "transient-then-ok: fallback model was used";
elif ! printf '%s' "$err" | grep -q 'run-prompt: transient transport failure'; then no "transient-then-ok: retry was not logged";
elif ! printf '%s' "$err" | grep -q 'ECONNRESET'; then no "transient-then-ok: reason was not logged";
elif ! printf '%s' "$err" | grep -q '(1/2)'; then no "transient-then-ok: attempt count was not logged";
else pass "a transient ECONNRESET is retried and the run succeeds"; fi

# 2. "socket connection was closed unexpectedly" is also retried.
run_once transient-socket-then-ok "fallback/model" t2
if [ "$rc" -ne 0 ]; then no "socket-closed: expected exit 0 got $rc ($err)";
elif [ "$calls" -ne 2 ]; then no "socket-closed: expected 2 attempts got $calls";
elif ! printf '%s' "$err" | grep -q 'run-prompt: transient transport failure'; then no "socket-closed: retry was not logged";
elif ! printf '%s' "$err" | grep -qi 'socket connection was closed unexpectedly'; then no "socket-closed: reason was not logged";
else pass "a closed socket transport failure is retried"; fi

# 3. Exhausted transient retries fail with the last exit code and log each retry.
run_once transient-exhausted "fallback/model" t3 NOTIFY_TRANSIENT_RETRY_MAX=2
if [ "$rc" -eq 0 ]; then no "exhausted: expected a failure";
elif [ "$calls" -ne 3 ]; then no "exhausted: expected 3 attempts (1 + 2 retries) got $calls";
elif [ "$(printf '%s\n' "$err" | grep -c 'run-prompt: transient transport failure')" -ne 2 ]; then
    no "exhausted: expected 2 retry log lines"
else pass "exhausted transient retries fail without unsafe delivery"; fi

# 4. A confirmed send must never be retried, even if the attempt also looks
#    transient (non-duplication guarantee).
run_once transient-sent-no-retry "fallback/model" t4
if [ "$calls" -ne 1 ]; then no "sent-no-retry: expected 1 attempt got $calls";
elif printf '%s' "$err" | grep -q 'transient transport failure'; then no "sent-no-retry: retried despite a confirmed send";
else pass "a transient failure after a confirmed send is not retried"; fi

# 5. A non-transient failure is not retried as transient.
run_once ordinary-fail "fallback/model" t5
if [ "$calls" -ne 1 ]; then no "ordinary: expected 1 attempt got $calls";
elif printf '%s' "$err" | grep -q 'transient transport failure'; then no "ordinary: ordinary failure was retried as transient";
else pass "a non-transient failure is not retried as transient"; fi

# 6. A quota/model-unavailable failure keeps the existing fallback behavior and
#    is not retried as a transient transport failure.
run_once quota-no-transient-retry "fallback/model" t6
if [ "$calls" -ne 2 ]; then no "quota-fallback: expected 2 attempts got $calls";
elif ! printf '%s\n' "$models" | grep -qx 'fallback/model'; then no "quota-fallback: fallback model was not used";
elif printf '%s' "$err" | grep -q 'transient transport failure'; then no "quota-fallback: quota failure was retried as transient";
else pass "quota/model-unavailable fallback behavior is preserved"; fi

# 7. A transient failure on the preferred model can be retried and, if the
#    retried attempt becomes model-unavailable, the existing fallback still
#    fires.
run_once transient-then-quota-fallback "fallback/model" t7
if [ "$rc" -ne 0 ]; then no "transient-then-quota-fallback: expected exit 0 got $rc ($err)";
elif [ "$calls" -ne 3 ]; then no "transient-then-quota-fallback: expected 3 attempts got $calls";
elif ! printf '%s' "$err" | grep -q 'transient transport failure'; then no "transient-then-quota-fallback: transient retry not logged";
elif ! printf '%s' "$err" | grep -q 'preferred model unavailable'; then no "transient-then-quota-fallback: fallback not logged";
elif ! printf '%s\n' "$models" | tail -n 1 | grep -qx 'fallback/model'; then no "transient-then-quota-fallback: final attempt was not fallback";
else pass "transient retry composes with model-unavailable fallback"; fi

# 8. A transient failure on the fallback attempt is retried.
run_once fallback-transient-then-ok "fallback/model" t8
if [ "$rc" -ne 0 ]; then no "fallback-transient: expected exit 0 got $rc ($err)";
elif [ "$calls" -ne 3 ]; then no "fallback-transient: expected 3 attempts got $calls";
elif ! printf '%s\n' "$models" | grep -qx 'fallback/model'; then no "fallback-transient: fallback model was not used";
elif ! printf '%s' "$err" | grep -q 'transient transport failure'; then no "fallback-transient: transient retry not logged";
else pass "a transient failure on the fallback attempt is retried"; fi

# 9. Quota-memo skip path: a transient failure on the direct fallback attempt
#    is retried.
state="$tmp/state-t9"; data="$tmp/data-t9"; mkdir -p "$state/notify-agent" "$data"
printf '{"model":"test/model","expires":9999999999}' > "$state/notify-agent/quota-skip.json"
mkdir -p "$tmp/bin-t9"
cat > "$tmp/bin-t9/opencode" <<'SH9'
#!/bin/sh
n=0
if [ -f "${COUNTER_FILE:?}" ]; then n=$(cat "$COUNTER_FILE"); fi
n=$((n+1))
printf '%s' "$n" > "$COUNTER_FILE"
model=""; prev=""
for arg in "$@"; do if [ "$prev" = "--model" ]; then model=$arg; fi; prev=$arg; done
printf '%s\n' "$model" >> "${MODELS_FILE:?}"
if [ "$n" -eq 1 ]; then
    printf 'socket connection was closed unexpectedly\n' >&2
    exit 1
fi
if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
printf 'ok\n'; exit 0
SH9
chmod +x "$tmp/bin-t9/opencode"
rm -f "$tmp/counter" "$tmp/models"; : > "$tmp/counter"; : > "$tmp/models"
set +e
env -i \
    PATH="$tmp/bin-t9:$tmp/bin:$PATH" \
    OPENCODE_MODEL="test/model" \
    OPENCODE_MODEL_FALLBACK="fallback/model" \
    NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
    NOTIFY_RUN_LOCK="$tmp/run-t9.lock" \
    XDG_STATE_HOME="$state" \
    XDG_DATA_HOME="$data" \
    NOTIFY_TRANSIENT_RETRY_DELAY=0 \
    COUNTER_FILE="$tmp/counter" \
    MODELS_FILE="$tmp/models" \
    "$run_prompt" "Compose a status message." >"$tmp/out-t9" 2>"$tmp/err-t9"
rc=$?
set -e
calls=$(cat "$tmp/counter"); models=$(cat "$tmp/models"); err=$(cat "$tmp/err-t9")
if [ "$rc" -ne 0 ]; then no "memo-skip: expected exit 0 got $rc ($err)";
elif [ "$calls" -ne 2 ]; then no "memo-skip: expected 2 attempts got $calls";
elif printf '%s\n' "$models" | grep -qx 'test/model'; then no "memo-skip: preferred model was attempted";
elif ! printf '%s\n' "$models" | grep -qx 'fallback/model'; then no "memo-skip: fallback model was not used";
elif ! printf '%s' "$err" | grep -q 'transient transport failure'; then no "memo-skip: transient retry not logged";
else pass "quota-memo skip path retries transient failures on the fallback"; fi

# 10. NOTIFY_TRANSIENT_RETRY_MAX=0 disables transient retries.
run_once transient-then-ok "fallback/model" t10 NOTIFY_TRANSIENT_RETRY_MAX=0
if [ "$rc" -eq 0 ]; then no "disabled: expected failure with retries disabled";
elif [ "$calls" -ne 1 ]; then no "disabled: expected 1 attempt got $calls";
elif printf '%s' "$err" | grep -q 'transient transport failure'; then no "disabled: retry logged despite being disabled";
else pass "NOTIFY_TRANSIENT_RETRY_MAX=0 disables transient retries"; fi

# 11. A malformed transient retry setting fails by name.
set +e
env -i \
    PATH="$tmp/bin:$PATH" \
    OPENCODE_MODEL="test/model" \
    NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
    NOTIFY_TRANSIENT_RETRY_MAX=lots \
    "$run_prompt" "Compose a status message." >"$tmp/out-bad" 2>"$tmp/err-bad"
rc=$?
set -e
err=$(cat "$tmp/err-bad")
if [ "$rc" -ne 1 ]; then no "bad max: expected exit 1 got $rc";
elif ! printf '%s' "$err" | grep -q 'NOTIFY_TRANSIENT_RETRY_MAX'; then no "bad max: did not name the variable";
elif printf '%s' "$err" | grep -q 'lots'; then no "bad max: echoed the value";
else pass "a malformed NOTIFY_TRANSIENT_RETRY_MAX fails by name"; fi

if [ "$failures" -ne 0 ]; then printf '%d failed\n' "$failures" >&2; exit 1; fi
printf 'all run-prompt transient retry tests passed\n'

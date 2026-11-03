#!/bin/sh
# Single-flight, per-attempt timeout and quota-memo tests for
# scripts/run-prompt.sh.
#
# The fake `opencode` on PATH is the seam: scenario "ok" sends, "quota-then-ok"
# exhausts the free tier once, "hang" never returns and "fail" is an ordinary
# failure. A fake `flock` is added only when the host has no util-linux flock
# (macOS development); it uses the same fcntl.flock call, so the script's
# `flock -w N 9` contract is exercised either way. The image always has
# /usr/bin/flock.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
run_prompt="$repo_dir/scripts/run-prompt.sh"

command -v python3 >/dev/null 2>&1 || { printf 'test: python3 required\n' >&2; exit 1; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/run-prompt-single-flight.XXXXXX")
holder_pids=
cleanup() {
    for pid in $holder_pids; do
        kill "$pid" 2>/dev/null || true
    done
    rm -rf "$tmp"
}
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

# fake opencode: behaviour driven by SCENARIO and a counter file.
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
    ok)
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'ok\n'
        exit 0
        ;;
    quota-then-ok)
        if [ "$n" -eq 1 ]; then
            printf 'Free usage limit reached\n' >&2
            exit 1
        fi
        if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
        printf 'ok\n'
        exit 0
        ;;
    hang)
        # exec so the timeout signal reaches the sleeping process itself.
        exec sleep 60
        ;;
    fail)
        printf 'something broke\n' >&2
        exit 1
        ;;
esac
exit 1
SH
chmod +x "$tmp/bin/opencode"

# util-linux flock is not on every development host; the shim implements the
# same non-blocking poll on the inherited descriptor.
if ! command -v flock >/dev/null 2>&1; then
    cat > "$tmp/bin/flock" <<'PY'
#!/usr/bin/env python3
import fcntl, os, sys, time

args = sys.argv[1:]
wait = 0.0
while len(args) > 1 and args[0].startswith("-"):
    option = args.pop(0)
    if option in ("-w", "--wait", "--timeout"):
        wait = float(args.pop(0))
    elif option.startswith("-w") and len(option) > 2:
        wait = float(option[2:])
fd = int(args[0])
deadline = time.monotonic() + wait
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except OSError:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.05)
PY
    chmod +x "$tmp/bin/flock"
fi

failures=0
pass(){ printf 'ok   - %s\n' "$1"; }
no(){ printf 'FAIL - %s\n' "$1" >&2; failures=$((failures+1)); }

# hold_lock LOCK SECONDS READY: hold an exclusive flock from another process
# until READY exists, then keep holding for SECONDS.
hold_lock() {
    python3 - "$1" "$2" "$3" <<'PY' &
import fcntl, os, sys, time

path, seconds, ready = sys.argv[1], float(sys.argv[2]), sys.argv[3]
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
open(ready, "w").close()
time.sleep(seconds)
os.close(fd)
PY
    holder_pids="$holder_pids $!"
    attempts=0
    while [ ! -f "$3" ]; do
        attempts=$((attempts+1))
        [ "$attempts" -lt 200 ] || { printf 'test: lock holder did not start\n' >&2; exit 1; }
        sleep 0.05
    done
}

# lock_is_free LOCK: true when no other process holds it.
lock_is_free() {
    python3 - "$1" <<'PY'
import fcntl, os, sys

fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)
sys.exit(0)
PY
}

# run_once SCENARIO FALLBACK STATE LOCK [VAR=VALUE ...]
run_once() {
    scenario=$1; fallback=$2; state=$3; lock=$4
    shift 4
    rm -f "$tmp/counter" "$tmp/models"
    : > "$tmp/counter"; : > "$tmp/models"
    data="$tmp/data-$scenario"
    mkdir -p "$data"
    started=$(date +%s)
    set +e
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        OPENCODE_MODEL_FALLBACK="$fallback" \
        NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
        NOTIFY_RUN_LOCK="$lock" \
        XDG_STATE_HOME="$state" \
        XDG_DATA_HOME="$data" \
        SCENARIO="$scenario" \
        COUNTER_FILE="$tmp/counter" \
        MODELS_FILE="$tmp/models" \
        "$@" \
        "$run_prompt" "Compose a status message." >"$tmp/out" 2>"$tmp/err"
    rc=$?
    set -e
    elapsed=$(( $(date +%s) - started ))
    calls=$(cat "$tmp/counter")
    [ -n "$calls" ] || calls=0
    models=$(cat "$tmp/models")
    err=$(cat "$tmp/err")
}

# ---------------------------------------------------------------------------
# Single flight
# ---------------------------------------------------------------------------

# 1. the lock is held by someone else and the wait expires -> a skip, exit 0.
lock="$tmp/lock-busy"
hold_lock "$lock" 10 "$tmp/busy.ready"
run_once ok "fallback/model" "$tmp/state-busy" "$lock" NOTIFY_RUN_LOCK_WAIT=0
if [ "$rc" -ne 0 ]; then no "busy: expected exit 0 got $rc";
elif [ "$calls" -ne 0 ]; then no "busy: expected no opencode call got $calls";
elif ! printf '%s' "$err" | grep -qx 'run-prompt: skipped: another run is in progress'; then
    no "busy: missing the one skip line"
else pass "a held lock prints one skip line and exits 0"; fi

# 2. the lock frees inside the wait -> the run proceeds.
lock="$tmp/lock-wait"
hold_lock "$lock" 1 "$tmp/wait.ready"
run_once ok "fallback/model" "$tmp/state-wait" "$lock" NOTIFY_RUN_LOCK_WAIT=15
if [ "$rc" -ne 0 ]; then no "wait: expected exit 0 got $rc";
elif [ "$calls" -ne 1 ]; then no "wait: expected 1 opencode call got $calls";
elif [ "$elapsed" -lt 1 ]; then no "wait: did not wait for the lock";
elif printf '%s' "$err" | grep -q 'skipped: another run'; then no "wait: skipped instead of waiting";
else pass "a scheduled run waits for the lock, then runs"; fi

# 3. the lock is released when the run exits.
if lock_is_free "$tmp/lock-wait"; then pass "the lock is free after a run"; else no "lock still held after a run"; fi

# ---------------------------------------------------------------------------
# Hard timeout
# ---------------------------------------------------------------------------

if command -v timeout >/dev/null 2>&1; then
    # 4. a hanging attempt is killed and the run fails quickly.
    lock="$tmp/lock-timeout"
    run_once hang "" "$tmp/state-timeout" "$lock" NOTIFY_RUN_TIMEOUT=1
    if [ "$rc" -eq 0 ]; then no "timeout: expected a failure got 0";
    elif [ "$elapsed" -gt 20 ]; then no "timeout: the attempt was not bounded (${elapsed}s)";
    elif [ "$calls" -ne 1 ]; then no "timeout: expected 1 attempt got $calls";
    else pass "a hanging attempt times out and the run fails"; fi

    # 5. and the lock is not held afterwards.
    if lock_is_free "$lock"; then pass "a timed-out attempt releases the lock"; else no "timed-out run kept the lock"; fi
else
    printf 'skip - timeout not installed; timeout cases not run\n'
fi

# ---------------------------------------------------------------------------
# Quota memo
# ---------------------------------------------------------------------------

# 6. an exhausted free tier is written down after the fallback fires.
state="$tmp/state-quota"
lock="$tmp/lock-quota"
run_once quota-then-ok "fallback/model" "$state" "$lock"
memo="$state/notify-agent/quota-skip.json"
if [ "$rc" -ne 0 ]; then no "memo write: expected exit 0 got $rc";
elif [ ! -f "$memo" ]; then no "memo write: no stamp at $memo";
elif ! grep -q 'test/model' "$memo"; then no "memo write: stamp does not name the preferred model";
elif ! printf '%s' "$models" | grep -q 'fallback/model'; then no "memo write: fallback was not used";
else pass "an exhausted free tier writes a quota stamp"; fi

mode=$(python3 -c 'import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777))' "$state/notify-agent")
if [ "$mode" != "0o700" ]; then no "memo dir: expected 0o700 got $mode"; else pass "the quota state directory is 0700"; fi

# 7. a valid stamp skips the preferred model entirely.
run_once ok "fallback/model" "$state" "$lock"
if [ "$rc" -ne 0 ]; then no "memo skip: expected exit 0 got $rc";
elif [ "$calls" -ne 1 ]; then no "memo skip: expected 1 attempt got $calls";
elif printf '%s' "$models" | grep -q '^test/model$'; then no "memo skip: preferred model was attempted";
elif ! printf '%s' "$models" | grep -q '^fallback/model$'; then no "memo skip: fallback model was not used";
elif ! printf '%s' "$err" | grep -Eq 'run-prompt: preferred model test/model skipped until [0-9][0-9]:[0-9][0-9]Z \(quota\)'; then
    no "memo skip: missing the skip log line"
else pass "a valid stamp goes straight to the fallback and logs the expiry"; fi

# 8. an expired stamp is ignored.
state="$tmp/state-expired"
mkdir -p "$state/notify-agent"
printf '{"model":"test/model","expires":1}' > "$state/notify-agent/quota-skip.json"
run_once ok "fallback/model" "$state" "$tmp/lock-expired"
if [ "$rc" -ne 0 ]; then no "memo expired: expected exit 0 got $rc";
elif ! printf '%s' "$models" | grep -q '^test/model$'; then no "memo expired: preferred model was skipped";
else pass "an expired stamp does not block the preferred model"; fi

# 9. a corrupt stamp is ignored.
state="$tmp/state-corrupt"
mkdir -p "$state/notify-agent"
printf 'not json at all' > "$state/notify-agent/quota-skip.json"
run_once ok "fallback/model" "$state" "$tmp/lock-corrupt"
if [ "$rc" -ne 0 ]; then no "memo corrupt: expected exit 0 got $rc";
elif ! printf '%s' "$models" | grep -q '^test/model$'; then no "memo corrupt: preferred model was skipped";
else pass "an unreadable stamp does not block the preferred model"; fi

# 10. a stamp for another model is ignored.
state="$tmp/state-other"
mkdir -p "$state/notify-agent"
printf '{"model":"other/model","expires":9999999999}' > "$state/notify-agent/quota-skip.json"
run_once ok "fallback/model" "$state" "$tmp/lock-other"
if ! printf '%s' "$models" | grep -q '^test/model$'; then no "memo other model: preferred model was skipped";
else pass "a stamp for another model does not block the preferred model"; fi

# 11. a successful preferred run clears the stamp.
state="$tmp/state-clear"
mkdir -p "$state/notify-agent"
printf '{"model":"test/model","expires":9999999999}' > "$state/notify-agent/quota-skip.json"
run_once ok "" "$state" "$tmp/lock-clear"
if [ "$rc" -ne 0 ]; then no "memo clear: expected exit 0 got $rc";
elif [ -f "$state/notify-agent/quota-skip.json" ]; then no "memo clear: stamp survived a successful run";
else pass "a successful preferred run clears the stamp"; fi

# 12. the memo is not consulted when it is disabled.
state="$tmp/state-disabled"
mkdir -p "$state/notify-agent"
printf '{"model":"test/model","expires":9999999999}' > "$state/notify-agent/quota-skip.json"
run_once ok "fallback/model" "$state" "$tmp/lock-disabled" NOTIFY_QUOTA_MEMO_SECONDS=0
if ! printf '%s' "$models" | grep -q '^test/model$'; then no "memo disabled: preferred model was skipped";
else pass "NOTIFY_QUOTA_MEMO_SECONDS=0 ignores the stamp"; fi

# 13. a bad number is a configuration error that names the variable only.
run_once ok "fallback/model" "$tmp/state-bad" "$tmp/lock-bad" NOTIFY_RUN_TIMEOUT=soon
if [ "$rc" -ne 1 ]; then no "bad timeout: expected exit 1 got $rc";
elif ! printf '%s' "$err" | grep -q 'NOTIFY_RUN_TIMEOUT'; then no "bad timeout: did not name the variable";
elif printf '%s' "$err" | grep -q 'soon'; then no "bad timeout: echoed the value";
else pass "a malformed NOTIFY_RUN_TIMEOUT fails by name"; fi

if [ "$failures" -ne 0 ]; then printf '%d failed\n' "$failures" >&2; exit 1; fi
printf 'all run-prompt single-flight tests passed\n'

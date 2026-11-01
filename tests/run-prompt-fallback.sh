#!/bin/sh
# Fallback tests for scripts/run-prompt.sh.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
run_prompt="$repo_dir/scripts/run-prompt.sh"

command -v jq >/dev/null 2>&1 || { printf 'test: jq required\n' >&2; exit 1; }

tmp=$(mktemp -d "${TMPDIR:-/tmp}/run-prompt-fallback.XXXXXX")
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

# fake opencode: behaviour driven by SCENARIO and a counter file.
cat > "$tmp/bin/opencode" <<'SH'
#!/bin/sh
counter_file=${COUNTER_FILE:?}
n=0
if [ -f "$counter_file" ]; then n=$(cat "$counter_file"); fi
n=$((n+1))
printf '%s' "$n" > "$counter_file"
# record model
model=""
prev=""
for arg in "$@"; do
    if [ "$prev" = "--model" ]; then model=$arg; fi
    prev=$arg
done
printf '%s\n' "$model" >> "${MODELS_FILE:?}"

scenario=${SCENARIO:?}
if [ "$n" -eq 1 ]; then
    case "$scenario" in
        quota|sent|fallback-fail)
            printf 'Free usage limit reached\n' >&2
            if [ "$scenario" = "sent" ] && [ -n "${NOTIFY_SEND_MARKER:-}" ]; then
                : > "$NOTIFY_SEND_MARKER"
            fi
            exit 1
            ;;
        ordinary)
            printf 'something broke\n' >&2
            exit 1
            ;;
    esac
else
    case "$scenario" in
        quota)
            if [ -n "${NOTIFY_SEND_MARKER:-}" ]; then : > "$NOTIFY_SEND_MARKER"; fi
            printf 'ok\n'
            exit 0
            ;;
        fallback-fail)
            printf 'Free usage limit reached\n' >&2
            exit 1
            ;;
        *)
            exit 0
            ;;
    esac
fi
SH
chmod +x "$tmp/bin/opencode"

run_scenario() {
    # args: scenario, fallback_value (possibly empty), extra env...
    scenario=$1; shift
    fallback=$1; shift
    rm -f "$tmp/counter" "$tmp/models"
    : > "$tmp/counter"; : > "$tmp/models"
    data="$tmp/data-$scenario"
    rm -rf "$data"; mkdir -p "$data"
    set +e
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        OPENCODE_MODEL_FALLBACK="$fallback" \
        NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
        XDG_DATA_HOME="$data" \
        SCENARIO="$scenario" \
        COUNTER_FILE="$tmp/counter" \
        MODELS_FILE="$tmp/models" \
        "$@" \
        "$run_prompt" "Compose a status message." >/tmp/out 2>/tmp/err
    rc=$?
    set -e
    calls=$(cat "$tmp/counter")
    models=$(cat "$tmp/models")
    printf 'scenario=%s rc=%s calls=%s models=[%s]\n' "$scenario" "$rc" "$calls" "$models" >&2
    printf '%s|%s|%s' "$rc" "$calls" "$models"
}

failures=0
pass(){ printf 'ok   - %s\n' "$1"; }
no(){ printf 'FAIL - %s\n' "$1" >&2; failures=$((failures+1)); }

# 1. quota -> two attempts, second uses fallback
res=$(run_scenario quota "fallback/model")
rc=${res%%|*}; rest=${res#*|}; calls=${rest%%|*}; models=${rest#*|}
if [ "$rc" -ne 0 ]; then no "quota: expected rc 0 got $rc"; 
elif [ "$calls" != "2" ]; then no "quota: expected 2 attempts got $calls";
elif ! printf '%s' "$models" | grep -q "fallback/model"; then no "quota: fallback model not used";
else pass "quota retries once on fallback model"; fi

# 2. fallback also fails -> two attempts total
res=$(run_scenario fallback-fail "fallback/model")
rc=${res%%|*}; rest=${res#*|}; calls=${rest%%|*}
if [ "$calls" != "2" ]; then no "fallback-fail: expected 2 attempts got $calls";
else pass "fallback failure stops after two attempts"; fi

# 3. ordinary failure -> one attempt
res=$(run_scenario ordinary "fallback/model")
rc=${res%%|*}; rest=${res#*|}; calls=${rest%%|*}
if [ "$calls" != "1" ]; then no "ordinary: expected 1 attempt got $calls";
else pass "ordinary failure does not fall back"; fi

# 4. already sent -> no fallback
res=$(run_scenario sent "fallback/model")
rc=${res%%|*}; rest=${res#*|}; calls=${rest%%|*}
if [ "$calls" != "1" ]; then no "sent: expected 1 attempt got $calls";
else pass "sent marker prevents fallback"; fi

# 5. unset fallback -> one attempt
res=$(run_scenario quota "")
rc=${res%%|*}; rest=${res#*|}; calls=${rest%%|*}
if [ "$calls" != "1" ]; then no "unset: expected 1 attempt got $calls";
else pass "unset fallback keeps single attempt"; fi

# 6. whitespace fallback -> exit 1 naming variable, no echo
set +e
env -i PATH="$tmp/bin:$PATH" OPENCODE_MODEL="test/model" \
    OPENCODE_MODEL_FALLBACK="bad value" NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
    "$run_prompt" "hi" >/tmp/o 2>/tmp/e
rc=$?
set -e
err=$(cat /tmp/e)
if [ "$rc" -ne 1 ]; then no "whitespace: expected exit 1 got $rc";
elif ! printf '%s' "$err" | grep -q "OPENCODE_MODEL_FALLBACK"; then no "whitespace: did not name variable";
elif printf '%s' "$err" | grep -q "bad value"; then no "whitespace: leaked value";
else pass "whitespace fallback rejected by name"; fi

if [ "$failures" -ne 0 ]; then printf '%d failed\n' "$failures" >&2; exit 1; fi
printf 'all run-prompt fallback tests passed\n'
SH
chmod +x tests/run-prompt-fallback.sh 2>/dev/null || true
# move into tests dir properly
mv "$tmp/../run-prompt-fallback.sh" tests/run-prompt-fallback.sh 2>/dev/null || true
# Actually write file directly instead
echo "written"
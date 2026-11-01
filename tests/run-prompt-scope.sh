#!/bin/sh
# Instruction-scope tests for scripts/run-prompt.sh.
#
# A fake `opencode` captures the prompt instead of starting a model. No
# credentials, network, or live delivery is used.
#
# Requires: jq.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
run_prompt="$repo_dir/scripts/run-prompt.sh"

command -v jq >/dev/null 2>&1 || {
    printf 'test: jq is required\n' >&2
    exit 1
}

tmp=$(mktemp -d "${TMPDIR:-/tmp}/run-prompt-scope.XXXXXX")
cleanup() {
    rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmp/bin"
cat > "$tmp/bin/opencode" <<'SH'
#!/bin/sh
for arg do last=$arg; done
printf '%s' "$last" > "$CAPTURE"
if [ -n "${CAPTURE_ARGS:-}" ]; then
    printf '%s\n' "$@" > "$CAPTURE_ARGS"
fi
if [ -n "${CAPTURE_ENV:-}" ]; then
    printf '%s' "${NOTIFY_CONTENT_DISABLED-}" > "$CAPTURE_ENV"
fi
if [ -n "${CAPTURE_RUNBOOKS:-}" ]; then
    printf '%s' "${NOTIFY_RUNBOOKS_ALLOWED-}" > "$CAPTURE_RUNBOOKS"
fi
if [ -n "${CAPTURE_BOOKSTACK_URL:-}" ]; then
    printf '%s' "${NOTIFY_BOOKSTACK_URL-}" > "$CAPTURE_BOOKSTACK_URL"
fi
if [ -n "${CAPTURE_BOOKSTACK_BOOKS:-}" ]; then
    printf '%s' "${NOTIFY_BOOKSTACK_BOOKS-}" > "$CAPTURE_BOOKSTACK_BOOKS"
fi
if [ -n "${CAPTURE_BOOKSTACK_TOKEN_ID:-}" ]; then
    printf '%s' "${NOTIFY_BOOKSTACK_TOKEN_ID-}" > "$CAPTURE_BOOKSTACK_TOKEN_ID"
fi
if [ -n "${CAPTURE_BOOKSTACK_TOKEN_SECRET:-}" ]; then
    printf '%s' "${NOTIFY_BOOKSTACK_TOKEN_SECRET-}" > "$CAPTURE_BOOKSTACK_TOKEN_SECRET"
fi
if [ -n "${CAPTURE_SCOPE:-}" ]; then
    printf '%s' "${NOTIFY_HOST_SCOPE-}" > "$CAPTURE_SCOPE"
fi
if [ -n "${CAPTURE_OUTPUT:-}" ]; then
    printf 'agent stdout line\n'
    printf 'agent stderr line\n' >&2
fi
if [ -n "${CAPTURE_BYTES:-}" ]; then
    dd if=/dev/zero bs=1024 count="$CAPTURE_BYTES" 2>/dev/null
fi
if [ -n "${CAPTURE_MARKER:-}" ] && [ -n "${NOTIFY_SEND_MARKER:-}" ]; then
    : > "$NOTIFY_SEND_MARKER"
fi
if [ -n "${CAPTURE_EXIT:-}" ]; then
    exit "$CAPTURE_EXIT"
fi
SH
chmod +x "$tmp/bin/opencode"

# Minimal MCP stubs for the readiness preflight. They answer only initialize
# and tools/list, so the tests never need a database or credentials.
cat > "$tmp/mcp-good.py" <<'PY'
import json
import sys

if "--selftest" in sys.argv[1:]:
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

cat > "$tmp/mcp-missing-tool.py" <<'PY'
import json
import sys

if "--selftest" in sys.argv[1:]:
    sys.exit(0)

tools = [{"name": "query", "description": "stub", "inputSchema": {"type": "object"}}]
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

cat > "$tmp/mcp-bad-selftest.py" <<'PY'
import json
import sys

if "--selftest" in sys.argv[1:]:
    sys.exit(1)

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

targets='{"general":{"channel_id":"111111111111111111","connected_account_id":"aaaaaaaa-1111-1111-1111-111111111111"},"admin-chat":{"channel_id":"222222222222222222","connected_account_id":"bbbbbbbb-2222-2222-2222-222222222222","host_scope":"host3.example.com","allow_content":false,"allow_runbooks":true,"bookstack":{"url":"https://vortex.example.test","books":"networking,operations-getting-started,organization,bookstack,v-rising,scripts","token":"vortex"}},"multi":{"channel_id":"333333333333333333","host_scope":["a.example","b.example"]},"content-off":{"channel_id":"444444444444444444","allow_content":false},"automations":{"channel_id":"555555555555555555","host_scope":["host1.example.com","host2.example.com","host3.example.com"],"allow_runbooks":true,"bookstack":{"url":"https://operator.example.test","books":"runbooks","token":"operator"}}}'

failures=0
pass() { printf 'ok   - %s\n' "$1"; }
no() { printf 'FAIL - %s\n' "$1" >&2; failures=$((failures + 1)); }

# capture OUT [ENV=VALUE ...] -- run-prompt.sh with the fake opencode.
capture() {
    out=$1
    shift
    rm -f "$out" "$out.env" "$out.scope" "$out.runbooks" \
        "$out.bookstack-url" "$out.bookstack-books" \
        "$out.bookstack-token-id" "$out.bookstack-token-secret"
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
        BOOKSTACK_TOKEN_ID_OPERATOR="operator-token-id" \
        BOOKSTACK_TOKEN_SECRET_OPERATOR="operator-token-secret" \
        BOOKSTACK_TOKEN_ID_VORTEX="vortex-token-id" \
        BOOKSTACK_TOKEN_SECRET_VORTEX="vortex-token-secret" \
        CAPTURE="$out" \
        CAPTURE_ENV="$out.env" \
        CAPTURE_SCOPE="$out.scope" \
        CAPTURE_RUNBOOKS="$out.runbooks" \
        CAPTURE_BOOKSTACK_URL="$out.bookstack-url" \
        CAPTURE_BOOKSTACK_BOOKS="$out.bookstack-books" \
        CAPTURE_BOOKSTACK_TOKEN_ID="$out.bookstack-token-id" \
        CAPTURE_BOOKSTACK_TOKEN_SECRET="$out.bookstack-token-secret" \
        "$@" >"$out.stdout" 2>"$out.stderr"
}

contains() {
    case "$1" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

expected_unscoped=$(cat <<'EOF'
Compose a status message.

The target for this run is `general`. Send with:
/app/scripts/notify.sh -t general -m "<message>"

The known hosts for this run are `host1.example.com`, `host2.example.com`, `host3.example.com`. Include one line for each, in this order, even if it has no samples. Do not add a known host outside the permitted hosts.
EOF
)

# 1. A scoped target carries the scope and its content block into the
#    instruction, and exports NOTIFY_CONTENT_DISABLED for the MCP child.
set +e
capture "$tmp/scoped" NOTIFY_TARGETS="$targets" \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "scoped target: run-prompt.sh failed: $(cat "$tmp/scoped.stderr")"
else
    prompt=$(cat "$tmp/scoped")
    if ! contains "$prompt" "The delivery scope for this run is \`host3.example.com\`."; then
        no "scoped target: instruction did not state the scope"
    elif ! contains "$prompt" "Nothing about any other host may appear in the message"; then
        no "scoped target: instruction did not state the host boundary"
    elif ! contains "$prompt" "Report host data and runbook procedures only"; then
        no "scoped target: instruction did not state the host-data and runbook boundary"
    elif ! contains "$prompt" "Runbooks are available for this run"; then
        no "scoped target: instruction did not mention its granted runbooks"
    elif contains "$prompt" "disabled" \
        || contains "$prompt" "content search" \
        || contains "$prompt" "site, CV, projects or pages"; then
        no "scoped target: instruction named a withheld capability"
    elif ! contains "$prompt" '/app/scripts/notify.sh -t admin-chat -m'; then
        no "scoped target: instruction lost the target command"
    elif [ "$(cat "$tmp/scoped.env")" != "1" ]; then
        no "scoped target: NOTIFY_CONTENT_DISABLED was not exported"
    elif [ "$(cat "$tmp/scoped.scope")" != '["host3.example.com"]' ]; then
        no "scoped target: NOTIFY_HOST_SCOPE was not exported as JSON"
    elif [ "$(cat "$tmp/scoped.runbooks")" != "1" ]; then
        no "scoped target: NOTIFY_RUNBOOKS_ALLOWED was not exported"
    elif [ "$(cat "$tmp/scoped.bookstack-url")" != "https://vortex.example.test" ]; then
        no "scoped target: NOTIFY_BOOKSTACK_URL was not the Vortex instance"
    elif [ "$(cat "$tmp/scoped.bookstack-books")" != "networking,operations-getting-started,organization,bookstack,v-rising,scripts" ]; then
        no "scoped target: NOTIFY_BOOKSTACK_BOOKS was not the Vortex book list"
    elif [ "$(cat "$tmp/scoped.bookstack-token-id")" != "vortex-token-id" ]; then
        no "scoped target: NOTIFY_BOOKSTACK_TOKEN_ID was not the Vortex credential"
    elif [ "$(cat "$tmp/scoped.bookstack-token-secret")" != "vortex-token-secret" ]; then
        no "scoped target: NOTIFY_BOOKSTACK_TOKEN_SECRET was not the Vortex credential"
    elif contains "$(cat "$tmp/scoped.bookstack-url")" "operator.example.test"; then
        no "scoped target: admin-chat could reach the operator instance"
    else
        pass "scoped target carries its scope and its own Vortex runbooks and token"
    fi
fi

# 2. An unscoped target keeps the instruction byte-identical to before.
set +e
capture "$tmp/unscoped" NOTIFY_TARGETS="$targets" \
    "$run_prompt" -t general "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "unscoped target: run-prompt.sh failed: $(cat "$tmp/unscoped.stderr")"
elif [ "$(cat "$tmp/unscoped")" != "$expected_unscoped" ]; then
    no "unscoped target: instruction changed"
elif contains "$(cat "$tmp/unscoped")" "delivery scope"; then
    no "unscoped target: instruction mentioned a scope"
elif contains "$(cat "$tmp/unscoped")" "Report host data only"; then
    no "unscoped target: instruction mentioned a host-data restriction"
elif [ -s "$tmp/unscoped.env" ]; then
    no "unscoped target: NOTIFY_CONTENT_DISABLED was exported"
elif [ -s "$tmp/unscoped.scope" ]; then
    no "unscoped target: NOTIFY_HOST_SCOPE was exported"
else
    pass "unscoped target keeps the old instruction exactly"
fi

# 3. No target keeps the bare instruction.
set +e
capture "$tmp/no-target" "$run_prompt" "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "no target: run-prompt.sh failed: $(cat "$tmp/no-target.stderr")"
elif [ "$(cat "$tmp/no-target")" != "Compose a status message." ]; then
    no "no target: instruction changed"
elif contains "$(cat "$tmp/no-target")" "delivery scope"; then
    no "no target: instruction mentioned a scope"
elif [ -s "$tmp/no-target.env" ]; then
    no "no target: NOTIFY_CONTENT_DISABLED was exported"
else
    pass "no target keeps the bare instruction exactly"
fi

# 4. An array scope is rendered as a list, with no content block.
set +e
capture "$tmp/multi" NOTIFY_TARGETS="$targets" \
    "$run_prompt" -t multi "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "array scope: run-prompt.sh failed: $(cat "$tmp/multi.stderr")"
elif ! contains "$(cat "$tmp/multi")" "\`a.example\`, \`b.example\`"; then
    no "array scope: instruction did not list both hosts"
elif contains "$(cat "$tmp/multi")" "Report host data only"; then
    no "array scope: instruction mentioned a host-data restriction"
elif [ -s "$tmp/multi.env" ]; then
    no "array scope: NOTIFY_CONTENT_DISABLED was exported"
elif [ "$(cat "$tmp/multi.scope")" != '["a.example","b.example"]' ]; then
    no "array scope: NOTIFY_HOST_SCOPE did not carry both hosts"
else
    pass "array scope is rendered as a list"
fi

# 5. allow_content false without a host_scope blocks content only.
set +e
capture "$tmp/content-off" NOTIFY_TARGETS="$targets" \
    "$run_prompt" -t content-off "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "content-off target: run-prompt.sh failed: $(cat "$tmp/content-off.stderr")"
elif ! contains "$(cat "$tmp/content-off")" "Report host data only"; then
    no "content-off target: instruction did not state the host-data boundary"
elif contains "$(cat "$tmp/content-off")" "disabled" \
    || contains "$(cat "$tmp/content-off")" "content search"; then
    no "content-off target: instruction named a withheld capability"
elif contains "$(cat "$tmp/content-off")" "delivery scope"; then
    no "content-off target: instruction mentioned a host scope"
elif [ "$(cat "$tmp/content-off.env")" != "1" ]; then
    no "content-off target: NOTIFY_CONTENT_DISABLED was not exported"
else
    pass "allow_content false blocks content without a host scope"
fi

# 6. A malformed host_scope fails before opencode runs, names the target, and
#    never echoes its id.
set +e
capture "$tmp/bad" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","host_scope":123}}' \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/bad.stderr")
if [ "$rc" -eq 0 ]; then
    no "malformed host_scope: expected a non-zero exit"
elif [ -e "$tmp/bad" ]; then
    no "malformed host_scope: opencode ran despite the failure"
elif ! contains "$err" "admin-chat"; then
    no "malformed host_scope: message did not name the target"
elif contains "$err" "222222222222222222" || contains "$err" "bbbbbbbb-2222-2222-2222-222222222222"; then
    no "malformed host_scope: message leaked an id"
else
    pass "malformed host_scope fails by name and sends nothing"
fi

# 7. A malformed allow_content fails before opencode runs, names the target,
#    and never echoes its id.
set +e
capture "$tmp/bad-content" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","allow_content":"false"}}' \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/bad-content.stderr")
if [ "$rc" -eq 0 ]; then
    no "malformed allow_content: expected a non-zero exit"
elif [ -e "$tmp/bad-content" ]; then
    no "malformed allow_content: opencode ran despite the failure"
elif ! contains "$err" "admin-chat"; then
    no "malformed allow_content: message did not name the target"
elif contains "$err" "222222222222222222"; then
    no "malformed allow_content: message leaked an id"
else
    pass "malformed allow_content fails by name and sends nothing"
fi

# 7b. allow_runbooks true is the only thing that mentions runbooks in the
#     instruction and exports NOTIFY_RUNBOOKS_ALLOWED=1.
set +e
capture "$tmp/runbooks" NOTIFY_TARGETS="$targets" \
    "$run_prompt" -t automations "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "allow_runbooks target: run-prompt.sh failed: $(cat "$tmp/runbooks.stderr")"
elif ! contains "$(cat "$tmp/runbooks")" "Runbooks are available for this run"; then
    no "allow_runbooks target: instruction did not mention runbooks"
elif [ "$(cat "$tmp/runbooks.runbooks")" != "1" ]; then
    no "allow_runbooks target: NOTIFY_RUNBOOKS_ALLOWED was not exported"
elif [ "$(cat "$tmp/runbooks.bookstack-url")" != "https://operator.example.test" ]; then
    no "allow_runbooks target: NOTIFY_BOOKSTACK_URL was not the operator instance"
elif [ "$(cat "$tmp/runbooks.bookstack-books")" != "runbooks" ]; then
    no "allow_runbooks target: NOTIFY_BOOKSTACK_BOOKS was not the operator book"
elif [ "$(cat "$tmp/runbooks.bookstack-token-id")" != "operator-token-id" ]; then
    no "allow_runbooks target: NOTIFY_BOOKSTACK_TOKEN_ID was not the operator credential"
elif [ "$(cat "$tmp/runbooks.bookstack-token-secret")" != "operator-token-secret" ]; then
    no "allow_runbooks target: NOTIFY_BOOKSTACK_TOKEN_SECRET was not the operator credential"
elif contains "$(cat "$tmp/runbooks.bookstack-url")" "vortex.example.test"; then
    no "allow_runbooks target: automations could reach the Vortex instance"
elif [ -s "$tmp/runbooks.env" ]; then
    no "allow_runbooks target: NOTIFY_CONTENT_DISABLED was exported"
else
    pass "allow_runbooks true mentions runbooks and exports its own instance and token"
fi

# 7c. admin-chat with allow_content flipped true but no allow_runbooks is still
#     refused: no mention and no exported grant, even if the parent environment
#     already has one.
set +e
capture "$tmp/content-on" \
    NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","host_scope":"host3.example.com","allow_content":true}}' \
    NOTIFY_RUNBOOKS_ALLOWED=1 \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
prompt=$(cat "$tmp/content-on" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "content-enabled admin-chat: run-prompt.sh failed: $(cat "$tmp/content-on.stderr")"
elif contains "$prompt" "Runbooks" || contains "$prompt" "runbook"; then
    no "content-enabled admin-chat: instruction mentioned runbooks"
elif [ -s "$tmp/content-on.runbooks" ]; then
    no "content-enabled admin-chat: NOTIFY_RUNBOOKS_ALLOWED was not cleared"
elif [ -s "$tmp/content-on.bookstack-url" ] || [ -s "$tmp/content-on.bookstack-books" ]; then
    no "content-enabled admin-chat: NOTIFY_BOOKSTACK_* was not cleared"
elif contains "$prompt" "Report host data only"; then
    no "content-enabled admin-chat: instruction added the host-data block"
else
    pass "content-enabled admin-chat is still refused runbooks"
fi

# 7d. A malformed allow_runbooks fails before opencode runs, names the target,
#     and never echoes its id or value.
set +e
capture "$tmp/bad-runbooks" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","allow_runbooks":"true"}}' \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/bad-runbooks.stderr")
if [ "$rc" -eq 0 ]; then
    no "malformed allow_runbooks: expected a non-zero exit"
elif [ -e "$tmp/bad-runbooks" ]; then
    no "malformed allow_runbooks: opencode ran despite the failure"
elif ! contains "$err" "admin-chat"; then
    no "malformed allow_runbooks: message did not name the target"
elif contains "$err" "222222222222222222" || contains "$err" '"true"' \
    || contains "$err" "allow_runbooks\":\"true"; then
    no "malformed allow_runbooks: message leaked an id or value"
else
    pass "malformed allow_runbooks fails by name and sends nothing"
fi

# 7e. A malformed bookstack fails before opencode runs, names the target, and
#     never echoes the URL or books.
set +e
capture "$tmp/bad-bookstack" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","allow_runbooks":true,"bookstack":{"url":"http://bad.example.test","books":"runbooks"}}}' \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/bad-bookstack.stderr")
if [ "$rc" -eq 0 ]; then
    no "malformed bookstack: expected a non-zero exit"
elif [ -e "$tmp/bad-bookstack" ]; then
    no "malformed bookstack: opencode ran despite the failure"
elif ! contains "$err" "admin-chat"; then
    no "malformed bookstack: message did not name the target"
elif contains "$err" "bad.example.test" || contains "$err" "222222222222222222"; then
    no "malformed bookstack: message leaked a value or id"
else
    pass "malformed bookstack fails by name and sends nothing"
fi

# 7f. A grant without a bookstack instance exports nothing and mentions
#     nothing, even if the parent environment carries a grant and an instance.
set +e
capture "$tmp/grant-no-instance" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","allow_runbooks":true}}' \
    NOTIFY_RUNBOOKS_ALLOWED=1 \
    NOTIFY_BOOKSTACK_URL=https://operator.example.test \
    NOTIFY_BOOKSTACK_BOOKS=runbooks \
    NOTIFY_BOOKSTACK_TOKEN_ID=operator-token-id \
    NOTIFY_BOOKSTACK_TOKEN_SECRET=operator-token-secret \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
prompt=$(cat "$tmp/grant-no-instance" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "grant without instance: run-prompt.sh failed: $(cat "$tmp/grant-no-instance.stderr")"
elif contains "$prompt" "Runbooks" || contains "$prompt" "runbook"; then
    no "grant without instance: instruction mentioned runbooks"
elif [ -s "$tmp/grant-no-instance.runbooks" ] \
    || [ -s "$tmp/grant-no-instance.bookstack-url" ] \
    || [ -s "$tmp/grant-no-instance.bookstack-books" ] \
    || [ -s "$tmp/grant-no-instance.bookstack-token-id" ] \
    || [ -s "$tmp/grant-no-instance.bookstack-token-secret" ]; then
    no "grant without instance: runbook variables were not cleared"
else
    pass "a grant without an instance exports nothing"
fi

# 7g. A bookstack instance without the grant exports nothing and mentions
#     nothing, even if the parent environment carries a grant and an instance.
set +e
capture "$tmp/instance-no-grant" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","bookstack":{"url":"https://vortex.example.test","books":"networking","token":"vortex"}}}' \
    NOTIFY_RUNBOOKS_ALLOWED=1 \
    NOTIFY_BOOKSTACK_URL=https://operator.example.test \
    NOTIFY_BOOKSTACK_BOOKS=runbooks \
    NOTIFY_BOOKSTACK_TOKEN_ID=operator-token-id \
    NOTIFY_BOOKSTACK_TOKEN_SECRET=operator-token-secret \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
prompt=$(cat "$tmp/instance-no-grant" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "instance without grant: run-prompt.sh failed: $(cat "$tmp/instance-no-grant.stderr")"
elif contains "$prompt" "Runbooks" || contains "$prompt" "runbook"; then
    no "instance without grant: instruction mentioned runbooks"
elif [ -s "$tmp/instance-no-grant.runbooks" ] \
    || [ -s "$tmp/instance-no-grant.bookstack-url" ] \
    || [ -s "$tmp/instance-no-grant.bookstack-books" ] \
    || [ -s "$tmp/instance-no-grant.bookstack-token-id" ] \
    || [ -s "$tmp/instance-no-grant.bookstack-token-secret" ]; then
    no "instance without grant: runbook variables were not cleared"
else
    pass "an instance without the grant exports nothing"
fi

# 7h. A malformed bookstack.token suffix fails before opencode runs, names the
#     target, and never echoes the value.
set +e
capture "$tmp/bad-token" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","allow_runbooks":true,"bookstack":{"url":"https://vortex.example.test","books":"runbooks","token":"vor-tex"}}}' \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/bad-token.stderr")
if [ "$rc" -eq 0 ]; then
    no "malformed bookstack token: expected a non-zero exit"
elif [ -e "$tmp/bad-token" ]; then
    no "malformed bookstack token: opencode ran despite the failure"
elif ! contains "$err" "admin-chat"; then
    no "malformed bookstack token: message did not name the target"
elif contains "$err" "vor-tex" || contains "$err" "222222222222222222"; then
    no "malformed bookstack token: message leaked a value or id"
else
    pass "malformed bookstack token fails by name and sends nothing"
fi

# 7i. A named token suffix with no configured pair gets no runbooks and no
#     mention, even if a global BOOKSTACK_TOKEN_* pair is present.
set +e
capture "$tmp/token-missing" NOTIFY_TARGETS='{"admin-chat":{"channel_id":"222222222222222222","allow_runbooks":true,"bookstack":{"url":"https://vortex.example.test","books":"networking","token":"unconfigured"}}}' \
    BOOKSTACK_TOKEN_ID=global-token-id \
    BOOKSTACK_TOKEN_SECRET=global-token-secret \
    "$run_prompt" -t admin-chat "Compose a status message." 2>/dev/null
rc=$?
set -e
prompt=$(cat "$tmp/token-missing" 2>/dev/null || true)
if [ "$rc" -ne 0 ]; then
    no "unconfigured token: run-prompt.sh failed: $(cat "$tmp/token-missing.stderr")"
elif contains "$prompt" "Runbooks" || contains "$prompt" "runbook"; then
    no "unconfigured token: instruction mentioned runbooks"
elif [ -s "$tmp/token-missing.runbooks" ] \
    || [ -s "$tmp/token-missing.bookstack-url" ] \
    || [ -s "$tmp/token-missing.bookstack-books" ] \
    || [ -s "$tmp/token-missing.bookstack-token-id" ] \
    || [ -s "$tmp/token-missing.bookstack-token-secret" ]; then
    no "unconfigured token: runbook variables were not cleared"
else
    pass "an unconfigured token suffix gets no runbooks"
fi

# 8. A server that does not advertise both tools fails the run before
#    opencode starts, without echoing the environment.
set +e
capture "$tmp/mcp-bad" NOTIFY_MCP_SERVER="$tmp/mcp-missing-tool.py" \
    ORACLE_PASSWORD="preflight-secret" ORACLE_DSN="preflight-dsn" \
    "$run_prompt" "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/mcp-bad.stderr")
if [ "$rc" -eq 0 ]; then
    no "MCP preflight: expected a non-zero exit for a missing tool"
elif [ -e "$tmp/mcp-bad" ]; then
    no "MCP preflight: opencode ran despite the missing tool"
elif ! contains "$err" "MCP preflight failed"; then
    no "MCP preflight: failure did not identify the preflight"
elif contains "$err" "preflight-secret" || contains "$err" "preflight-dsn"; then
    no "MCP preflight: failure leaked the environment"
else
    pass "MCP preflight fails before opencode and leaks no environment"
fi

# 9. A missing server file fails the same way.
set +e
capture "$tmp/mcp-absent" NOTIFY_MCP_SERVER="$tmp/does-not-exist.py" \
    "$run_prompt" "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/mcp-absent.stderr")
if [ "$rc" -eq 0 ]; then
    no "MCP preflight: expected a non-zero exit for a missing server"
elif [ -e "$tmp/mcp-absent" ]; then
    no "MCP preflight: opencode ran despite the missing server"
elif ! contains "$err" "server not found"; then
    no "MCP preflight: missing-server failure was not explained"
else
    pass "MCP preflight rejects a missing server before opencode"
fi

# 10. A healthy preflight lets the run continue and does not alter the prompt.
set +e
capture "$tmp/mcp-good" NOTIFY_MCP_SERVER="$tmp/mcp-good.py" \
    "$run_prompt" "Compose a status message." 2>/dev/null
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
    no "MCP preflight: healthy server failed the run: $(cat "$tmp/mcp-good.stderr")"
elif [ "$(cat "$tmp/mcp-good")" != "Compose a status message." ]; then
    no "MCP preflight: healthy server changed the instruction"
else
    pass "MCP preflight passes a healthy server and keeps the instruction"
fi

# 11. A tool listing without a reachable database fails before opencode, so a
#     run cannot message the channel and only then discover Oracle is down.
set +e
capture "$tmp/mcp-bad-selftest" NOTIFY_MCP_SERVER="$tmp/mcp-bad-selftest.py" \
    ORACLE_PASSWORD="selftest-secret" "$run_prompt" "Compose a status message." 2>/dev/null
rc=$?
set -e
err=$(cat "$tmp/mcp-bad-selftest.stderr")
if [ "$rc" -eq 0 ]; then
    no "MCP preflight: expected a non-zero exit for a failed Oracle self-test"
elif [ -e "$tmp/mcp-bad-selftest" ]; then
    no "MCP preflight: opencode ran despite a failed Oracle self-test"
elif ! contains "$err" "Oracle self-test failed"; then
    no "MCP preflight: self-test failure was not explained"
elif contains "$err" "selftest-secret"; then
    no "MCP preflight: self-test failure leaked the environment"
else
    pass "MCP preflight fails before opencode when Oracle is unreachable"
fi

# 12. The shared transcript frames the run: path, model, exact prompt and
#     output, then a footer with exit code, duration and the send marker.
data="$tmp/data"
set +e
capture "$tmp/transcript" \
    XDG_DATA_HOME="$data" \
    CAPTURE_ARGS="$tmp/transcript.args" \
    CAPTURE_OUTPUT=1 \
    CAPTURE_MARKER=1 \
    "$run_prompt" -t general "Compose a status message."
rc=$?
set -e
log="$data/opencode/notify-debug.log"
if [ "$rc" -ne 0 ]; then
    no "transcript: run-prompt.sh failed: $(cat "$tmp/transcript.stderr")"
elif [ ! -f "$log" ]; then
    no "transcript: no debug log was written"
else
    output=$(cat "$log")
    summary=$(cat "$tmp/transcript.stderr")
    if ! contains "$output" "path: schedule"; then
        no "transcript: header did not name the path"
    elif ! contains "$output" "model: test/model"; then
        no "transcript: header did not name the model"
    elif ! contains "$output" "instruction:"; then
        no "transcript: header did not introduce the instruction"
    elif ! contains "$output" "Compose a status message."; then
        no "transcript: header lost the prompt"
    elif ! contains "$output" "The target for this run is \`general\`"; then
        no "transcript: header did not carry the full prompt"
    elif ! contains "$output" "agent stdout line"; then
        no "transcript: stdout was not captured"
    elif ! contains "$output" "agent stderr line"; then
        no "transcript: stderr was not captured"
    elif ! contains "$output" "--- run end: exit=0"; then
        no "transcript: footer did not record the exit code"
    elif ! contains "$output" "duration="; then
        no "transcript: footer did not record the duration"
    elif ! contains "$output" "sent=yes"; then
        no "transcript: footer did not record the send"
    elif ! contains "$summary" "run=schedule"; then
        no "summary: container log line did not name the path"
    elif ! contains "$summary" "model=test/model"; then
        no "summary: container log line did not name the model"
    elif ! contains "$summary" "exit=0"; then
        no "summary: container log line did not record the exit code"
    elif ! contains "$summary" "sent=yes"; then
        no "summary: container log line did not record the send"
    elif ! contains "$summary" "Compose a status message."; then
        no "summary: container log line lost the instruction excerpt"
    elif [ "$(grep -c ' run=schedule ' "$tmp/transcript.stderr")" -ne 1 ]; then
        no "summary: expected exactly one run summary line"
    else
        pass "the transcript has a header, output and a send-aware footer"
    fi
fi

# 13. Deep provider logging is off by default.
if contains "$(cat "$tmp/transcript.args")" "--print-logs" \
    || contains "$(cat "$tmp/transcript.args")" "--log-level"; then
    no "deep mode: provider logging was enabled by default"
else
    pass "deep provider logging is off by default"
fi

# 14. NOTIFY_DEBUG_LLM=1 adds --print-logs and --log-level debug.
set +e
capture "$tmp/deep" \
    XDG_DATA_HOME="$tmp/data-deep" \
    CAPTURE_ARGS="$tmp/deep.args" \
    NOTIFY_DEBUG_LLM=1 \
    "$run_prompt" -t general "Compose a status message."
rc=$?
set -e
args=$(cat "$tmp/deep.args")
if [ "$rc" -ne 0 ]; then
    no "deep mode: run-prompt.sh failed: $(cat "$tmp/deep.stderr")"
elif ! contains "$args" "--print-logs"; then
    no "deep mode: --print-logs was not passed"
elif ! contains "$args" "--log-level"; then
    no "deep mode: --log-level was not passed"
elif ! contains "$args" "debug"; then
    no "deep mode: the debug level was not passed"
else
    pass "NOTIFY_DEBUG_LLM=1 adds provider logging"
fi

# 15. A failed run still gets a footer with its exit code and sent=no.
set +e
capture "$tmp/failed" \
    XDG_DATA_HOME="$tmp/data-failed" \
    CAPTURE_EXIT=3 \
    CAPTURE_OUTPUT=1 \
    "$run_prompt" "Compose a status message."
rc=$?
set -e
output=$(cat "$tmp/data-failed/opencode/notify-debug.log" 2>/dev/null || true)
summary=$(cat "$tmp/failed.stderr" 2>/dev/null || true)
if [ "$rc" -eq 0 ]; then
    no "transcript failure: run-prompt.sh exited 0"
elif ! contains "$output" "--- run end: exit=3"; then
    no "transcript failure: footer did not record the exit code"
elif ! contains "$output" "sent=no"; then
    no "transcript failure: footer did not record the missing send"
elif ! contains "$summary" "exit=3"; then
    no "transcript failure: summary did not record the exit code"
elif ! contains "$summary" "sent=no"; then
    no "transcript failure: summary did not record the missing send"
else
    pass "a failed run records its exit code and sent=no"
fi

# 16. Output past the cap is drained, and the transcript stays bounded.
set +e
capture "$tmp/bounded" \
    XDG_DATA_HOME="$tmp/data-bounded" \
    CAPTURE_BYTES=300 \
    "$run_prompt" "Compose a status message."
rc=$?
set -e
log="$tmp/data-bounded/opencode/notify-debug.log"
size=$(wc -c < "$log" 2>/dev/null | tr -d ' ' || true)
if [ "$rc" -ne 0 ]; then
    no "transcript cap: run-prompt.sh failed: $(cat "$tmp/bounded.stderr")"
elif [ -z "$size" ] || [ "$size" -gt 262144 ]; then
    no "transcript cap: log grew past 256 KiB (size=${size:-unknown})"
else
    pass "the transcript stays bounded when output is large"
fi

if [ "$failures" -ne 0 ]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi

printf 'all run-prompt scope tests passed\n'

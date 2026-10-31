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
if [ -n "${CAPTURE_ENV:-}" ]; then
    printf '%s' "${NOTIFY_CONTENT_DISABLED-}" > "$CAPTURE_ENV"
fi
SH
chmod +x "$tmp/bin/opencode"

targets='{"general":{"channel_id":"111111111111111111","connected_account_id":"aaaaaaaa-1111-1111-1111-111111111111"},"admin-chat":{"channel_id":"222222222222222222","connected_account_id":"bbbbbbbb-2222-2222-2222-222222222222","host_scope":"vortexservers.co.uk","allow_content":false},"multi":{"channel_id":"333333333333333333","host_scope":["a.example","b.example"]},"content-off":{"channel_id":"444444444444444444","allow_content":false}}'

failures=0
pass() { printf 'ok   - %s\n' "$1"; }
no() { printf 'FAIL - %s\n' "$1" >&2; failures=$((failures + 1)); }

# capture OUT [ENV=VALUE ...] -- run-prompt.sh with the fake opencode.
capture() {
    out=$1
    shift
    rm -f "$out" "$out.env"
    env -i \
        PATH="$tmp/bin:$PATH" \
        OPENCODE_MODEL="test/model" \
        CAPTURE="$out" \
        CAPTURE_ENV="$out.env" \
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
    if ! contains "$prompt" "The delivery scope for this run is \`vortexservers.co.uk\`."; then
        no "scoped target: instruction did not state the scope"
    elif ! contains "$prompt" "Nothing about any other host may appear in the message"; then
        no "scoped target: instruction did not state the host boundary"
    elif ! contains "$prompt" "Content search is disabled for this run"; then
        no "scoped target: instruction did not state the content block"
    elif ! contains "$prompt" "do not include anything about the operator's site, CV, projects or pages"; then
        no "scoped target: instruction did not state the content boundary"
    elif ! contains "$prompt" '/app/scripts/notify.sh -t admin-chat -m'; then
        no "scoped target: instruction lost the target command"
    elif [ "$(cat "$tmp/scoped.env")" != "1" ]; then
        no "scoped target: NOTIFY_CONTENT_DISABLED was not exported"
    else
        pass "scoped target carries the delivery scope and content block"
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
elif contains "$(cat "$tmp/unscoped")" "Content search is disabled"; then
    no "unscoped target: instruction mentioned content"
elif [ -s "$tmp/unscoped.env" ]; then
    no "unscoped target: NOTIFY_CONTENT_DISABLED was exported"
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
elif contains "$(cat "$tmp/multi")" "Content search is disabled"; then
    no "array scope: instruction mentioned content"
elif [ -s "$tmp/multi.env" ]; then
    no "array scope: NOTIFY_CONTENT_DISABLED was exported"
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
elif ! contains "$(cat "$tmp/content-off")" "Content search is disabled for this run"; then
    no "content-off target: instruction did not state the content block"
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

if [ "$failures" -ne 0 ]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi

printf 'all run-prompt scope tests passed\n'

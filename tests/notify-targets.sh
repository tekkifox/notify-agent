#!/bin/sh
# Target-resolution tests for scripts/notify.sh.
#
# Everything runs against a local TLS stub. No live Discord or Composio request
# is made, and every id below is an obvious placeholder.
#
# Requires: python3, openssl, curl, jq.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
notify="$repo_dir/scripts/notify.sh"

for bin in python3 openssl curl jq; do
    command -v "$bin" >/dev/null 2>&1 || {
        printf 'test: %s is required\n' "$bin" >&2
        exit 1
    }
done

tmp=$(mktemp -d "${TMPDIR:-/tmp}/notify-targets.XXXXXX")
stub_pid=
cleanup() {
    if [ -n "$stub_pid" ]; then
        kill "$stub_pid" 2>/dev/null || true
        wait "$stub_pid" 2>/dev/null || true
    fi
    rm -rf "$tmp"
}
trap cleanup EXIT INT TERM

mkdir -p "$tmp/captures"

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$tmp/key.pem" -out "$tmp/cert.pem" -days 1 \
    -subj '/CN=localhost' \
    -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' >/dev/null 2>&1

cat > "$tmp/stub.py" <<'PY'
import http.server
import os
import ssl
import sys

captures, cert, key, port_file = sys.argv[1:5]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        index = len([n for n in os.listdir(captures) if n.endswith(".json")])
        with open(os.path.join(captures, "req-%d.json" % index), "wb") as fh:
            fh.write(body)
        with open(os.path.join(captures, "req-%d.path" % index), "w") as fh:
            fh.write(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(cert, key)
server.socket = context.wrap_socket(server.socket, server_side=True)
with open(port_file, "w") as fh:
    fh.write(str(server.server_address[1]))
server.serve_forever()
PY

python3 "$tmp/stub.py" "$tmp/captures" "$tmp/cert.pem" "$tmp/key.pem" "$tmp/port" &
stub_pid=$!

i=0
while [ ! -s "$tmp/port" ]; do
    i=$((i + 1))
    [ "$i" -lt 100 ] || {
        printf 'test: stub did not start\n' >&2
        exit 1
    }
    sleep 0.1
done
api_base="https://127.0.0.1:$(cat "$tmp/port")"

targets='{"general":{"channel_id":"111111111111111111","connected_account_id":"aaaaaaaa-1111-1111-1111-111111111111"},"admin-chat":{"channel_id":"222222222222222222","connected_account_id":"bbbbbbbb-2222-2222-2222-222222222222"}}'

failures=0
pass() { printf 'ok   - %s\n' "$1"; }
no() { printf 'FAIL - %s\n' "$1" >&2; failures=$((failures + 1)); }

count_requests() {
    find "$tmp/captures" -name '*.json' | wc -l | tr -d ' '
}

body_of() {
    cat "$tmp/captures/req-$1.json"
}

path_of() {
    cat "$tmp/captures/req-$1.path"
}

# send [ENV=VALUE ...] -- runs notify.sh with a stubbed Composio endpoint.
send() {
    env -i \
        PATH="$PATH" \
        COMPOSIO_API_KEY="test-key" \
        COMPOSIO_TOOL="DISCORDBOT_CREATE_MESSAGE" \
        COMPOSIO_API_BASE="$api_base" \
        SSL_CERT_FILE="$tmp/cert.pem" \
        CURL_CA_BUNDLE="$tmp/cert.pem" \
        "$@" \
        "$notify" -m "hello"
}

# send_target TARGET [ENV=VALUE ...]
send_target() {
    target=$1
    shift
    env -i \
        PATH="$PATH" \
        COMPOSIO_API_KEY="test-key" \
        COMPOSIO_TOOL="DISCORDBOT_CREATE_MESSAGE" \
        COMPOSIO_API_BASE="$api_base" \
        SSL_CERT_FILE="$tmp/cert.pem" \
        CURL_CA_BUNDLE="$tmp/cert.pem" \
        "$@" \
        "$notify" -t "$target" -m "hello"
}

expect_fail() {
    label=$1
    shift
    before=$(count_requests)
    set +e
    "$@" >/dev/null 2>&1
    rc=$?
    set -e
    after=$(count_requests)
    if [ "$rc" -eq 0 ]; then
        no "$label: expected a non-zero exit"
    elif [ "$after" -ne "$before" ]; then
        no "$label: made a request despite failing"
    else
        pass "$label"
    fi
}

contains() {
    case "$1" in
        *"$2"*) return 0 ;;
        *) return 1 ;;
    esac
}

# 1. A named target resolves into the Composio payload and beats COMPOSIO_ARGS
#    and COMPOSIO_CONNECTED_ACCOUNT_ID.
before=$(count_requests)
set +e
out=$(send_target admin-chat \
    NOTIFY_TARGETS="$targets" \
    COMPOSIO_ARGS='{"channel_id":"999999999999999999","connected_account_id":"cccccccc-9999-9999-9999-999999999999","extra":"kept"}' \
    COMPOSIO_CONNECTED_ACCOUNT_ID="dddddddd-0000-0000-0000-000000000000" 2>&1)
rc=$?
set -e
after=$(count_requests)
if [ "$rc" -ne 0 ]; then
    no "target resolution: notify.sh failed: $out"
else
    body=$(body_of "$((after - 1))")
    path=$(path_of "$((after - 1))")
    if [ "$after" -ne $((before + 1)) ]; then
        no "target resolution: expected one request"
    elif [ "$path" != "/api/v3.1/tools/execute/DISCORDBOT_CREATE_MESSAGE" ]; then
        no "target resolution: unexpected path $path"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.channel_id')" != "222222222222222222" ]; then
        no "target resolution: channel_id did not come from the target"
    elif [ "$(printf '%s' "$body" | jq -r '.connected_account_id')" != "bbbbbbbb-2222-2222-2222-222222222222" ]; then
        no "target resolution: top-level connected_account_id did not come from the target"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.connected_account_id')" != "bbbbbbbb-2222-2222-2222-222222222222" ]; then
        no "target resolution: arguments.connected_account_id did not come from the target"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.extra')" != "kept" ]; then
        no "target resolution: COMPOSIO_ARGS extra key was lost"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.content')" != "hello" ]; then
        no "target resolution: message missing from arguments"
    elif contains "$out" "222222222222222222" || contains "$out" "bbbbbbbb-2222-2222-2222-222222222222"; then
        no "target resolution: output leaked an id"
    else
        pass "target resolution merges the named target and beats COMPOSIO_ARGS"
    fi
fi

# 2. Unknown target fails and names the configured targets, not their ids.
before=$(count_requests)
set +e
out=$(send_target does-not-exist NOTIFY_TARGETS="$targets" 2>&1)
rc=$?
set -e
after=$(count_requests)
if [ "$rc" -eq 0 ]; then
    no "unknown target: expected a non-zero exit"
elif [ "$after" -ne "$before" ]; then
    no "unknown target: made a request despite failing"
elif ! contains "$out" "unknown target"; then
    no "unknown target: message did not say the target is unknown"
elif ! contains "$out" "general" || ! contains "$out" "admin-chat"; then
    no "unknown target: message did not list configured names"
elif contains "$out" "111111111111111111" || contains "$out" "222222222222222222"; then
    no "unknown target: message leaked an id"
else
    pass "unknown target fails, lists names only, and sends nothing"
fi

# 3. Malformed NOTIFY_TARGETS fails before any request and never echoes it.
bad_case=0
for bad in 'not-json' '[]' '{"general":{"connected_account_id":"x"}}' '{"general":{"channel_id":123}}'; do
    bad_case=$((bad_case + 1))
    expect_fail "malformed NOTIFY_TARGETS case $bad_case fails without a request" \
        send NOTIFY_TARGETS="$bad"
done
set +e
out=$(send NOTIFY_TARGETS='{"general":{"connected_account_id":"x"}}' 2>&1)
rc=$?
set -e
if [ "$rc" -eq 0 ]; then
    no "malformed NOTIFY_TARGETS: expected failure"
elif contains "$out" 'connected_account_id":"x'; then
    no "malformed NOTIFY_TARGETS: echoed the value"
else
    pass "malformed NOTIFY_TARGETS message does not echo the value"
fi

# 4. -t is rejected when a webhook is configured.
expect_fail "-t rejected when DISCORD_WEBHOOK_URL is set" \
    send_target general NOTIFY_TARGETS="$targets" \
    DISCORD_WEBHOOK_URL="$api_base/webhook"
set +e
out=$(send_target general NOTIFY_TARGETS="$targets" \
    DISCORD_WEBHOOK_URL="$api_base/webhook" 2>&1)
rc=$?
set -e
if [ "$rc" -eq 0 ] || ! contains "$out" "webhook"; then
    no "webhook: failure did not explain that a webhook has no targets"
else
    pass "webhook failure names the reason"
fi

# 5. No -t and no default keeps the legacy shape, ignoring NOTIFY_TARGETS.
before=$(count_requests)
set +e
out=$(send NOTIFY_TARGETS="$targets" \
    COMPOSIO_ARGS='{"channel_id":"legacy-channel","extra":"kept"}' \
    COMPOSIO_CONNECTED_ACCOUNT_ID="eeeeeeee-0000-0000-0000-000000000000" 2>&1)
rc=$?
set -e
after=$(count_requests)
if [ "$rc" -ne 0 ]; then
    no "legacy path: notify.sh failed: $out"
else
    body=$(body_of "$((after - 1))")
    if [ "$after" -ne $((before + 1)) ]; then
        no "legacy path: expected one request"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.channel_id')" != "legacy-channel" ]; then
        no "legacy path: COMPOSIO_ARGS channel_id was not used"
    elif [ "$(printf '%s' "$body" | jq -r '.connected_account_id')" != "eeeeeeee-0000-0000-0000-000000000000" ]; then
        no "legacy path: COMPOSIO_CONNECTED_ACCOUNT_ID was not used"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.extra')" != "kept" ]; then
        no "legacy path: COMPOSIO_ARGS extra key was lost"
    else
        pass "no -t and no default keeps the legacy behaviour"
    fi
fi

# 6. NOTIFY_DEFAULT_TARGET applies when -t is omitted.
before=$(count_requests)
set +e
out=$(send NOTIFY_TARGETS="$targets" NOTIFY_DEFAULT_TARGET="general" 2>&1)
rc=$?
set -e
after=$(count_requests)
if [ "$rc" -ne 0 ]; then
    no "default target: notify.sh failed: $out"
else
    body=$(body_of "$((after - 1))")
    if [ "$after" -ne $((before + 1)) ]; then
        no "default target: expected one request"
    elif [ "$(printf '%s' "$body" | jq -r '.arguments.channel_id')" != "111111111111111111" ]; then
        no "default target: default channel_id was not used"
    elif [ "$(printf '%s' "$body" | jq -r '.connected_account_id')" != "aaaaaaaa-1111-1111-1111-111111111111" ]; then
        no "default target: default connected_account_id was not used"
    else
        pass "NOTIFY_DEFAULT_TARGET applies when -t is omitted"
    fi
fi

# 7. -t wins over NOTIFY_DEFAULT_TARGET.
before=$(count_requests)
set +e
out=$(send_target admin-chat NOTIFY_TARGETS="$targets" NOTIFY_DEFAULT_TARGET="general" 2>&1)
rc=$?
set -e
after=$(count_requests)
if [ "$rc" -ne 0 ]; then
    no "target over default: notify.sh failed: $out"
else
    body=$(body_of "$((after - 1))")
    if [ "$(printf '%s' "$body" | jq -r '.arguments.channel_id')" != "222222222222222222" ]; then
        no "target over default: -t did not win"
    else
        pass "-t wins over NOTIFY_DEFAULT_TARGET"
    fi
fi

if [ "$failures" -ne 0 ]; then
    printf '%d test(s) failed\n' "$failures" >&2
    exit 1
fi

printf 'all notify target tests passed\n'

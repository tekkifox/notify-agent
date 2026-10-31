#!/bin/sh
# Deterministic notification delivery. No LLM, no retries, no secrets in logs.
#
# Discord is the default path. Composio is a fallback for a future server-side
# tool. Both paths read configuration from the environment only.
#
# Usage:
#   echo "hello" | notify.sh
#   notify.sh -m "hello"
#
# Exit codes:
#   0  delivered
#   1  delivery failed (configuration, network, or remote rejection)
#   64 usage error
set -eu

PROG=notify
MAX_CHARS=2000
MAX_STDIN_BYTES=65536

usage() {
    cat <<'EOF'
Usage: notify.sh [-m MESSAGE]

Reads the message from stdin when -m/--message is not given.

Delivery selection:
  DISCORD_WEBHOOK_URL set                  -> Discord webhook
  else COMPOSIO_API_KEY and COMPOSIO_TOOL  -> Composio tool execution
  else                                     -> error

Optional Composio settings:
  COMPOSIO_API_BASE               default https://backend.composio.dev
  COMPOSIO_USER_ID                default "default"
  COMPOSIO_MESSAGE_FIELD          default "content"
  COMPOSIO_ARGS                   JSON object merged into the tool arguments
  COMPOSIO_CONNECTED_ACCOUNT_ID   connected account to execute against

The message is truncated to 2000 characters. Discord mentions are suppressed.
The webhook URL and API keys are never printed.
EOF
}

fail() {
    printf '%s: %s\n' "$PROG" "$*" >&2
    exit 1
}

usage_fail() {
    printf '%s: %s\n' "$PROG" "$*" >&2
    usage >&2
    exit 64
}

message=
have_message=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        -m|--message)
            [ "$#" -ge 2 ] || usage_fail "-m requires a message argument"
            message=$2
            have_message=1
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            [ "$#" -eq 0 ] || usage_fail "unexpected argument"
            break
            ;;
        *)
            usage_fail "unexpected argument"
            ;;
    esac
done

command -v jq >/dev/null 2>&1 || fail "jq is required but not installed"
command -v curl >/dev/null 2>&1 || fail "curl is required but not installed"

if [ "$have_message" -eq 0 ]; then
    if [ -t 0 ]; then
        usage_fail "no message: pass -m or pipe stdin"
    fi
    message=$(head -c "$MAX_STDIN_BYTES")
fi

case "$message" in
    *[![:space:]]*) ;;
    *) fail "message is empty" ;;
esac

# Count Unicode code points, not bytes, then clamp to Discord's limit.
original_chars=$(printf '%s' "$message" | jq -Rs 'length' 2>/dev/null) \
    || fail "could not encode message"
if [ "$original_chars" -gt "$MAX_CHARS" ]; then
    message=$(printf '%s' "$message" \
        | jq -Rs --argjson max "$MAX_CHARS" '.[0:$max]' -r 2>/dev/null) \
        || fail "could not truncate message"
    printf '%s: message truncated to %s characters\n' "$PROG" "$MAX_CHARS" >&2
fi

send_discord() {
    webhook_url=${DISCORD_WEBHOOK_URL:-}
    case "$webhook_url" in
        https://*) ;;
        *) fail "DISCORD_WEBHOOK_URL must be an https:// URL" ;;
    esac

    payload=$(jq -cn --arg content "$message" \
        '{content: $content, allowed_mentions: {parse: []}}') \
        || fail "could not build Discord payload"

    status=$(printf '%s' "$payload" \
        | curl -sS --proto '=https' --tlsv1.2 --max-time 20 \
            -o /dev/null -w '%{http_code}' \
            -H 'Content-Type: application/json' \
            --data-binary @- \
            "$webhook_url" 2>/dev/null) || {
        rc=$?
        fail "discord request failed (curl exit ${rc})"
    }

    case "$status" in
        2??)
            printf '%s: delivered via discord (HTTP %s)\n' "$PROG" "$status"
            ;;
        429)
            fail "discord rate limited the message (HTTP 429)"
            ;;
        *)
            fail "discord rejected the message (HTTP ${status:-unknown})"
            ;;
    esac
}

send_composio() {
    api_key=${COMPOSIO_API_KEY:-}
    tool=${COMPOSIO_TOOL:-}
    api_base=${COMPOSIO_API_BASE:-https://backend.composio.dev}
    user_id=${COMPOSIO_USER_ID:-default}
    message_field=${COMPOSIO_MESSAGE_FIELD:-content}
    connected_account_id=${COMPOSIO_CONNECTED_ACCOUNT_ID:-}

    case "$api_base" in
        https://*) ;;
        *) fail "COMPOSIO_API_BASE must be an https:// URL" ;;
    esac
    case "$tool" in
        *[!A-Za-z0-9_.-]*) fail "COMPOSIO_TOOL contains invalid characters" ;;
    esac
    case "$message_field" in
        ''|*[!A-Za-z0-9_]*) fail "COMPOSIO_MESSAGE_FIELD contains invalid characters" ;;
    esac

    # COMPOSIO_ARGS is an optional JSON object of extra tool arguments. Validate
    # it without echoing it: parse errors can quote the input.
    args_json='{}'
    if [ -n "${COMPOSIO_ARGS:-}" ]; then
        args_json=$(printf '%s' "$COMPOSIO_ARGS" \
            | jq -ce 'if type == "object" then . else error("not an object") end' 2>/dev/null) \
            || fail "COMPOSIO_ARGS must be a JSON object"
    fi

    # The message field is merged last so it always wins over COMPOSIO_ARGS.
    payload=$(jq -cn \
        --arg user_id "$user_id" \
        --arg field "$message_field" \
        --arg message "$message" \
        --argjson args "$args_json" \
        --arg connected_account_id "$connected_account_id" \
        '{user_id: $user_id}
         + (if $connected_account_id == "" then {} else {connected_account_id: $connected_account_id} end)
         + {arguments: ($args + {($field): $message})}') \
        || fail "could not build Composio payload"

    endpoint="${api_base%/}/api/v3.1/tools/execute/${tool}"

    status=$(printf '%s' "$payload" \
        | curl -sS --proto '=https' --tlsv1.2 --max-time 30 \
            -o /dev/null -w '%{http_code}' \
            -H 'Content-Type: application/json' \
            -H "x-api-key: ${api_key}" \
            --data-binary @- \
            "$endpoint" 2>/dev/null) || {
        rc=$?
        fail "composio request failed (curl exit ${rc})"
    }

    case "$status" in
        2??)
            printf '%s: delivered via composio (HTTP %s)\n' "$PROG" "$status"
            ;;
        *)
            fail "composio rejected the message (HTTP ${status:-unknown})"
            ;;
    esac
}

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    send_discord
elif [ -n "${COMPOSIO_API_KEY:-}" ] && [ -n "${COMPOSIO_TOOL:-}" ]; then
    send_composio
else
    fail "no delivery configured: set DISCORD_WEBHOOK_URL, or COMPOSIO_API_KEY and COMPOSIO_TOOL"
fi

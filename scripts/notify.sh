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
#   0  delivered, or skipped because NOTIFY_REQUIRE_FLAG found no flag
#   1  delivery failed (configuration, network, or remote rejection)
#   64 usage error
set -eu

PROG=notify
MAX_CHARS=2000
MAX_STDIN_BYTES=65536
# The one webhook this agent reuses or creates per channel. Matching is exact.
WEBHOOK_NAME='Server Notification Agent'

usage() {
    cat <<'EOF'
Usage: notify.sh [-t TARGET] [-m MESSAGE]

Reads the message from stdin when -m/--message is not given.

Delivery selection:
  DISCORD_WEBHOOK_URL set                  -> Discord webhook
  else COMPOSIO_API_KEY and COMPOSIO_TOOL  -> Composio tool execution
  else                                     -> error

Named targets (Composio only):
  -t, --target NAME        send to a name defined in NOTIFY_TARGETS
  NOTIFY_TARGETS           JSON object: name -> {channel_id, connected_account_id,
                           host_scope, allow_content, allow_runbooks, bookstack}
                           host_scope, allow_content, allow_runbooks and
                           bookstack are optional target guards. A host_scope is
                           enforced here: if the message names a host outside the
                           permitted list (or its subdomains), notify.sh exits
                           non-zero and sends nothing. bookstack is an object
                           with an https url and a comma-separated books list.
  NOTIFY_DEFAULT_TARGET    target name used when -t is omitted
  The name must exist in NOTIFY_TARGETS; unknown names fail without sending.
  A target cannot be combined with DISCORD_WEBHOOK_URL.

Optional Composio settings:
  COMPOSIO_API_BASE               default https://backend.composio.dev
  COMPOSIO_USER_ID                default "default"
  COMPOSIO_MESSAGE_FIELD          default "content"
  COMPOSIO_ARGS                   JSON object merged into the tool arguments
  COMPOSIO_CONNECTED_ACCOUNT_ID   connected account to execute against

Optional send record:
  NOTIFY_SEND_MARKER   when set, a successful send touches this path. The
                       inbound listener uses it to detect a run that delivered
                       nothing. Unset is a no-op, and a marker that cannot be
                       written never changes the exit status.

Optional flag guard:
  NOTIFY_REQUIRE_FLAG  when set to exactly 1, the message must contain the
                       `[!]` flag marker. Without it the send is refused before
                       any request, one line is printed to stderr, and the
                       script exits 0: a silent skip, not a failure. Unset or
                       any other value keeps the normal behaviour. Scheduled
                       digests set it so a run with nothing flagged cannot post.

Optional Discord attachments:
  NOTIFY_ATTACH_DIR    a directory of chart PNGs to upload with the message.
                       At most ten regular .png files directly inside it are
                       sent, sorted by name. Only a Discord webhook supports
                       attachments; the Composio path sends text and logs one
                       warning. When the directory is unset or has no PNG the
                       request is exactly the plain JSON post. A failed
                       multipart post is retried once without images. When
                       PNGs exist but cannot be delivered, one plain line is
                       appended to the message: no file name, URL, token or
                       error body.

Per-target webhook for attachments:
  NOTIFY_WEBHOOK_<TARGET>  optional Discord webhook URL override for one named
                       target. The name is the target uppercased with every
                       character that is not A-Z0-9 replaced by "_": admin-chat
                       -> NOTIFY_WEBHOOK_ADMIN_CHAT, operations.eu ->
                       NOTIFY_WEBHOOK_OPERATIONS_EU. It is a secret and is
                       never printed.

                       When it is unset and NOTIFY_ATTACH_DIR holds a PNG,
                       notify.sh asks the target's bot connection through
                       Composio for a webhook in the target channel: it reuses
                       the webhook named "Server Notification Agent", or
                       creates one. The target's connected_account_id is used
                       for both calls. A lookup or creation failure sends text
                       only and logs one line naming the target, never a
                       response body or a token.

                       Unset with no usable bot connection, or set to a URL that
                       is not https://discord.com/api/webhooks/... or
                       https://discordapp.com/api/webhooks/..., also means text
                       only. A target never uses DISCORD_WEBHOOK_URL.

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

# Optional send record for the inbound listener. The marker path is set by the
# listener, never by the agent. Only a confirmed 2xx send calls this. Unset is a
# no-op, and a marker that cannot be written is reported but does not turn a
# delivered message into a failed one (the exit status must not change).
mark_sent() {
    [ -n "${NOTIFY_SEND_MARKER:-}" ] || return 0
    # A redirection error on a POSIX special builtin aborts a non-interactive
    # shell, and dash (/bin/sh here and in CI) does exactly that: a bare
    # `: > path` would take the whole script down after a delivered send. Put
    # the special builtin in its own subshell, where a failed redirection can
    # only fail that subshell. The subshell's stderr is /dev/null, so the
    # shell's own "cannot create" line is suppressed too.
    if ! ( : > "$NOTIFY_SEND_MARKER" ) 2>/dev/null; then
        printf '%s: warning: could not record the send marker\n' "$PROG" >&2
    fi
    return 0
}

message=
have_message=0
target=

while [ "$#" -gt 0 ]; do
    case "$1" in
        -m|--message)
            [ "$#" -ge 2 ] || usage_fail "-m requires a message argument"
            message=$2
            have_message=1
            shift 2
            ;;
        -t|--target)
            [ "$#" -ge 2 ] || usage_fail "-t requires a target name"
            [ -n "$2" ] || usage_fail "-t requires a target name"
            target=$2
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

# Hard silence guard. A scheduled digest must send nothing when nothing is
# flagged, and that cannot rest on the prompt alone: the model can still call
# this script. When NOTIFY_REQUIRE_FLAG=1, the message must carry the `[!]`
# marker that every flagged host line starts with. The check runs on the final
# message and before any target lookup or request; a refusal is a silent skip
# (exit 0) so the scheduler does not report a broken job. `[!]` is the only
# marker checked, and the prompt forbids adding it when nothing is flagged.
if [ "${NOTIFY_REQUIRE_FLAG:-}" = "1" ]; then
    case "$message" in
        *"[!]"*) ;;
        *)
            printf '%s: nothing was flagged, so nothing was sent\n' "$PROG" >&2
            exit 0
            ;;
    esac
fi

# Named targets are trusted operator configuration. Validate the allowlist
# before any request and never echo its value: jq errors can quote their input.
targets_json=
if [ -n "${NOTIFY_TARGETS:-}" ]; then
    targets_json=$(printf '%s' "$NOTIFY_TARGETS" | jq -ce '
        if type != "object" then error("not an object") else . end
        | if all(.[];
              type == "object"
              and (.channel_id | type == "string")
              and (.channel_id | length > 0)
              and ((has("connected_account_id") | not)
                   or (.connected_account_id | type == "string")))
          then . else error("invalid entry") end
    ' 2>/dev/null) \
        || fail "NOTIFY_TARGETS must be a JSON object mapping names to objects with a channel_id"

    # host_scope is optional: a non-empty string, or a non-empty array of
    # non-empty strings. Report bad entries by name only, never their ids.
    bad_scope_targets=$(printf '%s' "$targets_json" | jq -r '
        def valid_scope:
            if type == "string" then length > 0
            elif type == "array" then
                length > 0 and all(.[]; type == "string" and length > 0)
            else false end;
        [ to_entries[]
          | select((.value | type) == "object")
          | select(.value | has("host_scope"))
          | select((.value.host_scope | valid_scope) | not)
          | .key
        ] | join(", ")
    ' 2>/dev/null) \
        || fail "could not validate NOTIFY_TARGETS host_scope"
    if [ -n "$bad_scope_targets" ]; then
        fail "invalid host_scope for target(s) ${bad_scope_targets}: expected a non-empty string or an array of non-empty strings"
    fi

    # allow_content is optional and must be a JSON boolean when present.
    # Report bad entries by name only, never their ids.
    bad_content_targets=$(printf '%s' "$targets_json" | jq -r '
        [ to_entries[]
          | select((.value | type) == "object")
          | select(.value | has("allow_content"))
          | select((.value.allow_content | type) != "boolean")
          | .key
        ] | join(", ")
    ' 2>/dev/null) \
        || fail "could not validate NOTIFY_TARGETS allow_content"
    if [ -n "$bad_content_targets" ]; then
        fail "invalid allow_content for target(s) ${bad_content_targets}: expected a boolean"
    fi

    # allow_runbooks is optional and must be a JSON boolean when present.
    # Report bad entries by name only, never their ids.
    bad_runbooks_targets=$(printf '%s' "$targets_json" | jq -r '
        [ to_entries[]
          | select((.value | type) == "object")
          | select(.value | has("allow_runbooks"))
          | select((.value.allow_runbooks | type) != "boolean")
          | .key
        ] | join(", ")
    ' 2>/dev/null) \
        || fail "could not validate NOTIFY_TARGETS allow_runbooks"
    if [ -n "$bad_runbooks_targets" ]; then
        fail "invalid allow_runbooks for target(s) ${bad_runbooks_targets}: expected a boolean"
    fi

    # bookstack is optional per target. When present it must be an object with
    # an https URL and a non-empty comma-separated book list. Report bad
    # entries by name only, never their ids or values.
    bad_bookstack_targets=$(printf '%s' "$targets_json" | jq -r '
        def valid_entry:
            ascii_downcase as $e
            | if ($e | test("^[0-9]+$")) then ($e | tonumber) > 0
              else ($e | test("^[a-z0-9][a-z0-9._-]{0,99}$")) end;
        def valid_books:
            type == "string"
            and (split(",")
                 | map(gsub("^\\s+|\\s+$"; ""))
                 | map(select(length > 0)) as $entries
                 | ($entries | length) > 0
                   and ($entries | length) <= 16
                   and all($entries[]; valid_entry));
        def valid_url:
            type == "string"
            and length > 8
            and test("^https://[^/?#@]+(/[^?#]*)?$");
        def valid_token:
            type == "string"
            and test("^[A-Za-z0-9_]{1,64}$");
        def valid_bookstack:
            type == "object"
            and (.url | valid_url)
            and (.books | valid_books)
            and (.token | valid_token);
        [ to_entries[]
          | select((.value | type) == "object")
          | select(.value | has("bookstack"))
          | select((.value.bookstack | valid_bookstack) | not)
          | .key
        ] | join(", ")
    ' 2>/dev/null) \
        || fail "could not validate NOTIFY_TARGETS bookstack"
    if [ -n "$bad_bookstack_targets" ]; then
        fail "invalid bookstack for target(s) ${bad_bookstack_targets}: expected an object with an https url, a comma-separated books list and a token suffix"
    fi
fi

# -t wins over NOTIFY_DEFAULT_TARGET. With neither, delivery keeps its legacy
# shape and ignores NOTIFY_TARGETS entirely.
target_name=${target:-}
if [ -z "$target_name" ]; then
    target_name=${NOTIFY_DEFAULT_TARGET:-}
fi

target_channel_id=
target_connected_account_id=
if [ -n "$target_name" ]; then
    if [ -z "$targets_json" ]; then
        fail "unknown target \"${target_name}\"; configured targets: none"
    fi
    if ! printf '%s' "$targets_json" \
        | jq -e --arg name "$target_name" 'has($name)' >/dev/null 2>&1; then
        available=$(printf '%s' "$targets_json" | jq -r 'keys | join(", ")') \
            || available=
        [ -n "$available" ] || available=none
        fail "unknown target \"${target_name}\"; configured targets: ${available}"
    fi
    target_channel_id=$(printf '%s' "$targets_json" \
        | jq -r --arg name "$target_name" '.[$name].channel_id') \
        || fail "could not resolve target \"${target_name}\""
    target_connected_account_id=$(printf '%s' "$targets_json" \
        | jq -r --arg name "$target_name" '.[$name].connected_account_id // ""') \
        || fail "could not resolve target \"${target_name}\""
    target_host_scope_json=$(printf '%s' "$targets_json" \
        | jq -c --arg name "$target_name" '
            if has($name) then
                (.[$name].host_scope // [] | if type == "string" then [.] else . end)
            else [] end
        ') \
        || fail "could not resolve host_scope for target \"${target_name}\""
else
    target_host_scope_json='[]'
fi

# Delivery-boundary guard. This is the last line of defence for a scoped
# target: even if the model ignores the prompt or a prompt injection writes a
# host it should not, the message never leaves the container. A host-like
# token is any dotted name with a letters-only suffix. It is accepted when it
# equals a host_scope entry or is a subdomain of one; anything else refuses the
# send. This intentionally catches more than the operator's own hostnames:
# a scoped message that mentions github.com is refused too, because the safe
# failure is to send nothing. It does not catch bare labels, IP addresses,
# spaced or obfuscated names, or a figure with no hostname; the prompt is the
# layer that handles those, and the README says so.
enforce_host_scope() {
    [ -n "$target_name" ] || return 0
    [ -n "$target_host_scope_json" ] || return 0
    [ "$target_host_scope_json" != "[]" ] || return 0

    leaked=$(printf '%s' "$message" | jq -Rrs --argjson scope "$target_host_scope_json" '
        def norm: ascii_downcase | gsub("\\.+$"; "");
        def covered($h; $list):
            any($list[]; . as $e | ($h == $e) or ($h | endswith("." + $e)));
        ($scope | map(norm)) as $allowed
        | [ scan("[A-Za-z0-9][A-Za-z0-9._-]*\\.[A-Za-z]{2,}") ]
        | map(norm)
        | map(select(covered(.; $allowed) | not))
        | unique
        | join(", ")
    ') || fail "could not check the message against the delivery scope"
    if [ -n "$leaked" ]; then
        fail "message for target \"${target_name}\" names host(s) outside its host_scope: ${leaked}"
    fi
}

enforce_host_scope

send_discord() {
    case "${DISCORD_WEBHOOK_URL:-}" in
        https://*) ;;
        *) fail "DISCORD_WEBHOOK_URL must be an https:// URL" ;;
    esac

    post_discord_webhook "$DISCORD_WEBHOOK_URL"
}

# Resolve the per-target webhook variable name: uppercase the target name and
# replace every character that is not A-Z0-9 with an underscore. The result is
# always a safe shell variable name, so the indirect read cannot interpolate
# anything but the operator's own target name.
target_webhook_var() {
    printf 'NOTIFY_WEBHOOK_%s' "$1" \
        | jq -Rr 'ascii_upcase | gsub("[^A-Z0-9]"; "_")'
}

# True when NOTIFY_ATTACH_DIR holds at least one regular .png directly inside
# it. With no image every path keeps its exact previous behaviour.
has_attachments() {
    [ -n "${NOTIFY_ATTACH_DIR:-}" ] || return 1
    [ -d "$NOTIFY_ATTACH_DIR" ] || return 1
    for attach_file in "$NOTIFY_ATTACH_DIR"/*.png; do
        [ -f "$attach_file" ] && return 0
    done
    return 1
}

# Tell the channel, in one plain line, that charts exist but could not be
# posted. It is added at most once per run, only when there are PNGs to lose,
# and it never names a file, a URL, a token or an error body. The line is kept
# inside Discord's limit: a long message is shortened to leave room for it.
attach_note_added=0
attach_note() {
    has_attachments || return 0
    [ "$attach_note_added" -eq 0 ] || return 0
    attach_note_added=1
    note="Charts can be displayed, but this channel has no Discord webhook I can post them to — please give me permission to create webhooks."
    message=$(printf '%s' "$message" \
        | jq -Rs --argjson max "$MAX_CHARS" --arg note "$note" '
            ($note | length) as $n
            | if (length + $n + 2) > $max
              then .[0:($max - $n - 2)] + "\n\n" + $note
              else . + "\n\n" + $note
              end
        ' -r 2>/dev/null) \
        || fail "could not add the chart notice"
}

# The text-only delivery path. This is what every named target used before a
# per-target webhook existed, so it stays byte for byte the same.
send_text() {
    if [ -n "${COMPOSIO_API_KEY:-}" ] && [ -n "${COMPOSIO_TOOL:-}" ]; then
        send_composio
    else
        fail "no delivery configured: set DISCORD_WEBHOOK_URL, or COMPOSIO_API_KEY and COMPOSIO_TOOL"
    fi
}

# A named target may carry its own Discord webhook, the only route that can
# carry a file. When the operator sets NOTIFY_WEBHOOK_<TARGET> it wins. When it
# is unset, the bot resolves a webhook in the target channel through Composio:
# it reuses the one named WEBHOOK_NAME, or creates it once. A webhook URL is a
# credential: it is never printed and never taken from DISCORD_WEBHOOK_URL.
send_target_webhook() {
    webhook_var=$(target_webhook_var "$target_name") \
        || fail "could not derive the webhook variable for target \"${target_name}\""
    eval "target_webhook_url=\${${webhook_var}:-}"

    if [ -n "$target_webhook_url" ]; then
        case "$target_webhook_url" in
            https://discord.com/api/webhooks/?*|https://discordapp.com/api/webhooks/?*)
                ;;
            *)
                printf '%s: warning: target "%s" has a webhook that is not a Discord webhook URL; sending text only\n' \
                    "$PROG" "$target_name" >&2
                attach_note
                send_text
                return 0
                ;;
        esac

        post_discord_webhook "$target_webhook_url"
        return 0
    fi

    # No override: ask the target's own bot connection for a webhook in its
    # channel. A failure names the target only: a response body can carry a
    # token, so no part of it is repeated here.
    if ! resolve_target_webhook; then
        printf '%s: warning: could not resolve a Discord webhook for target "%s"; sending text only\n' \
            "$PROG" "$target_name" >&2
        attach_note
        send_text
        return 0
    fi

    post_discord_webhook "$resolved_webhook_url"
}

# Execute one Composio tool and print its `data` field as JSON. The response
# can carry a webhook token, so nothing about it is logged: on any failure this
# returns non-zero with no output and prints no message.
composio_tool_data() {
    cw_tool=$1
    cw_args=$2
    cw_api_key=${COMPOSIO_API_KEY:-}
    cw_api_base=${COMPOSIO_API_BASE:-https://backend.composio.dev}
    cw_user_id=${COMPOSIO_USER_ID:-default}
    cw_connected_account_id=${target_connected_account_id:-${COMPOSIO_CONNECTED_ACCOUNT_ID:-}}

    [ -n "$cw_api_key" ] || return 1
    case "$cw_api_base" in
        https://*) ;;
        *) return 1 ;;
    esac

    cw_payload=$(jq -cn \
        --arg user_id "$cw_user_id" \
        --argjson args "$cw_args" \
        --arg connected_account_id "$cw_connected_account_id" \
        '{user_id: $user_id}
         + (if $connected_account_id == "" then {} else {connected_account_id: $connected_account_id} end)
         + {arguments: $args}') || return 1

    cw_endpoint="${cw_api_base%/}/api/v3.1/tools/execute/${cw_tool}"

    cw_response=$(printf '%s' "$cw_payload" \
        | curl -sS -f --proto '=https' --tlsv1.2 --max-time 30 \
            -H 'Content-Type: application/json' \
            -H "x-api-key: ${cw_api_key}" \
            --data-binary @- \
            "$cw_endpoint" 2>/dev/null) || return 1

    printf '%s' "$cw_response" | jq -ce '
        if type == "object" and .successful == true then
            (if has("data") then .data else error("no data") end)
        else error("tool execution failed") end
    ' 2>/dev/null
}

# Resolve this target's webhook through its bot connection: list the channel's
# webhooks, reuse the one named WEBHOOK_NAME, or create it if the list has none.
# Sets resolved_webhook_url only on success. A webhook the list returns without
# a token cannot be reused, and creating a duplicate every run is worse than a
# text-only message, so that case fails.
resolve_target_webhook() {
    resolved_webhook_url=
    [ -n "$target_channel_id" ] || return 1

    cw_list_args=$(jq -cn --arg channel_id "$target_channel_id" \
        '{channel_id: $channel_id}') || return 1
    cw_list_data=$(composio_tool_data \
        DISCORDBOT_LIST_CHANNEL_WEBHOOKS "$cw_list_args") || return 1

    # Accept a bare Discord array or a {"webhooks": [...]} wrapper, then pick
    # the entry with the exact name.
    cw_webhook=$(printf '%s' "$cw_list_data" | jq -c --arg name "$WEBHOOK_NAME" '
        if type == "array" then .
        elif type == "object" and (.webhooks | type) == "array" then .webhooks
        else [] end
        | map(select(type == "object" and .name == $name))
        | .[0] // empty
    ' 2>/dev/null) || return 1

    if [ -n "$cw_webhook" ]; then
        cw_id=$(printf '%s' "$cw_webhook" | jq -r '.id // ""' 2>/dev/null) || return 1
        cw_token=$(printf '%s' "$cw_webhook" | jq -r '.token // ""' 2>/dev/null) || return 1
        [ -n "$cw_token" ] || return 1
    else
        cw_create_args=$(jq -cn \
            --arg channel_id "$target_channel_id" \
            --arg name "$WEBHOOK_NAME" \
            '{channel_id: $channel_id, name: $name}') || return 1
        cw_created=$(composio_tool_data \
            DISCORDBOT_CREATE_WEBHOOK "$cw_create_args") || return 1
        cw_webhook=$(printf '%s' "$cw_created" | jq -c '
            if type == "object" and (.webhook | type) == "object" then .webhook else . end
        ' 2>/dev/null) || return 1
        cw_id=$(printf '%s' "$cw_webhook" | jq -r '.id // ""' 2>/dev/null) || return 1
        cw_token=$(printf '%s' "$cw_webhook" | jq -r '.token // ""' 2>/dev/null) || return 1
    fi

    # Build the URL only from a snowflake id and a Discord token, so a malformed
    # response can never redirect the post to another host or path.
    case "$cw_id" in
        ''|*[!0-9]*) return 1 ;;
    esac
    case "$cw_token" in
        ''|*[!A-Za-z0-9._-]*) return 1 ;;
    esac

    resolved_webhook_url="https://discord.com/api/webhooks/${cw_id}/${cw_token}"
    return 0
}

# Post the message to one Discord webhook. With no eligible PNG this is the
# plain JSON request; with PNGs it is multipart, retried once without images
# when it fails.
post_discord_webhook() {
    post_url=$1

    payload=$(jq -cn --arg content "$message" \
        '{content: $content, allowed_mentions: {parse: []}}') \
        || fail "could not build Discord payload"

    # Collect at most ten PNG files directly inside NOTIFY_ATTACH_DIR, sorted
    # by name. Anything else in the directory is ignored. With no eligible
    # file the request is exactly today's plain JSON request.
    attach_count=0
    # --form-string, not -F: curl reads ';' inside a -F value as a form
    # modifier, which truncates a message that contains a semicolon and makes
    # Discord reject payload_json as invalid JSON. The file parts keep -F.
    set -- --form-string "payload_json=$payload"
    if [ -n "${NOTIFY_ATTACH_DIR:-}" ] && [ -d "$NOTIFY_ATTACH_DIR" ]; then
        for attach_file in "$NOTIFY_ATTACH_DIR"/*.png; do
            [ -f "$attach_file" ] || continue
            attach_count=$((attach_count + 1))
            set -- "$@" -F "files[$((attach_count - 1))]=@$attach_file"
            if [ "$attach_count" -ge 10 ]; then
                break
            fi
        done
    fi

    if [ "$attach_count" -eq 0 ]; then
        discord_post_json "$payload" "$post_url"
        return 0
    fi

    # curl sets its own multipart boundary; do not pass a Content-Type header.
    rc=0
    status=$(curl -sS --proto '=https' --tlsv1.2 --max-time 30 \
        -o /dev/null -w '%{http_code}' \
        "$@" \
        "$post_url" 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ]; then
        printf '%s: warning: discord attachment post failed (curl exit %s); sending without images\n' \
            "$PROG" "$rc" >&2
        attach_note
        payload=$(jq -cn --arg content "$message" \
            '{content: $content, allowed_mentions: {parse: []}}') \
            || fail "could not build Discord payload"
        discord_post_json "$payload" "$post_url"
        return 0
    fi

    case "$status" in
        2??)
            printf '%s: delivered via discord (HTTP %s) with %s image(s)\n' \
                "$PROG" "$status" "$attach_count"
            mark_sent
            ;;
        *)
            printf '%s: warning: discord attachment post failed (HTTP %s); sending without images\n' \
                "$PROG" "${status:-unknown}" >&2
            attach_note
            payload=$(jq -cn --arg content "$message" \
                '{content: $content, allowed_mentions: {parse: []}}') \
                || fail "could not build Discord payload"
            discord_post_json "$payload" "$post_url"
            ;;
    esac
}

# The plain JSON post. This is the original request, unchanged, and still the
# hard failure path when no attachment could be delivered.
discord_post_json() {
    post_payload=$1
    post_url=$2

    status=$(printf '%s' "$post_payload" \
        | curl -sS --proto '=https' --tlsv1.2 --max-time 20 \
            -o /dev/null -w '%{http_code}' \
            -H 'Content-Type: application/json' \
            --data-binary @- \
            "$post_url" 2>/dev/null) || {
        rc=$?
        fail "discord request failed (curl exit ${rc})"
    }

    case "$status" in
        2??)
            printf '%s: delivered via discord (HTTP %s)\n' "$PROG" "$status"
            mark_sent
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
    # A named target overrides the configured default connected account.
    connected_account_id=${target_connected_account_id:-${COMPOSIO_CONNECTED_ACCOUNT_ID:-}}

    if [ -n "${NOTIFY_ATTACH_DIR:-}" ] && [ -d "$NOTIFY_ATTACH_DIR" ]; then
        printf '%s: warning: attachments are only supported on the Discord webhook path; sending text only\n' "$PROG" >&2
        attach_note
    fi

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

    # A named target overrides the same keys in COMPOSIO_ARGS. The channel and
    # connection come from the operator's allowlist, never from the model.
    if [ -n "$target_channel_id" ]; then
        target_args=$(jq -cn \
            --arg channel_id "$target_channel_id" \
            --arg connected_account_id "$target_connected_account_id" \
            '{channel_id: $channel_id}
             + (if $connected_account_id == "" then {} else {connected_account_id: $connected_account_id} end)') \
            || fail "could not build target arguments"
        args_json=$(printf '%s' "$args_json" \
            | jq -c --argjson extra "$target_args" '. + $extra') \
            || fail "could not merge target arguments"
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
            mark_sent
            ;;
        *)
            fail "composio rejected the message (HTTP ${status:-unknown})"
            ;;
    esac
}

if [ -n "${DISCORD_WEBHOOK_URL:-}" ]; then
    if [ -n "$target_name" ]; then
        fail "a target was requested but DISCORD_WEBHOOK_URL is set; a webhook has no targets"
    fi
    send_discord
elif [ -n "$target_name" ] && has_attachments; then
    # A chart only leaves the container through a Discord webhook. A named
    # target uses its own NOTIFY_WEBHOOK_* variable when the operator set one;
    # otherwise the text-only path below is unchanged.
    send_target_webhook
else
    send_text
fi

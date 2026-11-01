#!/bin/sh
# Run one unattended OpenCode prompt with the locked-down `notify` agent.
#
# The agent can compose text and call /app/scripts/notify.sh, nothing else.
# See prompts/notify.md and opencode.json.
#
# Usage:
#   run-prompt.sh "Compose and send a short status message"
#   run-prompt.sh -t general "Compose and send a short status message"
#
# The target name is trusted operator configuration: it is carried into the
# instruction, and notify.sh resolves it against the NOTIFY_TARGETS allowlist.
set -eu

# Resolve the repository root so the MCP preflight works both in the image
# (/app/scripts -> /app/mcp) and from a checkout. NOTIFY_MCP_SERVER exists for
# the shell tests; it is not needed in normal operation.
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo_dir=$(CDPATH='' cd -- "${script_dir}/.." && pwd)
mcp_server=${NOTIFY_MCP_SERVER:-${repo_dir}/mcp/oracle_query.py}

# Prove that the MCP server starts and advertises both tools before OpenCode
# runs. The handshake does not touch the database, so this is cheap. Server
# output is captured, never printed: it must not reach the log, and the
# environment may contain credentials.
preflight_mcp() {
    server=$1

    if [ ! -f "$server" ]; then
        printf 'run-prompt: MCP preflight failed: server not found: %s\n' "$server" >&2
        exit 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        printf 'run-prompt: MCP preflight failed: python3 is not installed or not on PATH\n' >&2
        exit 1
    fi

    responses=$(
        printf '%s\n' \
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"run-prompt-preflight","version":"1.0"}}}' \
            '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
            | python3 "$server" 2>/dev/null
    ) || {
        printf 'run-prompt: MCP preflight failed: %s did not start\n' "$server" >&2
        exit 1
    }

    if ! printf '%s' "$responses" | python3 -c '
import json
import sys

initialized = False
tools = set()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if not isinstance(message, dict):
        continue
    if message.get("id") == 1 and isinstance(message.get("result"), dict):
        initialized = True
    if message.get("id") == 2:
        result = message.get("result")
        if isinstance(result, dict):
            for tool in result.get("tools", []):
                name = tool.get("name") if isinstance(tool, dict) else None
                if isinstance(name, str):
                    tools.add(name)
sys.exit(0 if initialized and {"query", "search_content"} <= tools else 1)
' 2>/dev/null; then
        printf 'run-prompt: MCP preflight failed: %s did not expose query and search_content\n' "$server" >&2
        exit 1
    fi

    # The tool listing is not enough: prove the database is reachable too, so a
    # run cannot message the channel and only then discover Oracle is down.
    # The selftest's own reason stays suppressed; it is available by running
    # `python3 /app/mcp/oracle_query.py --selftest` by hand.
    if ! python3 "$server" --selftest >/dev/null 2>&1; then
        printf 'run-prompt: MCP preflight failed: %s Oracle self-test failed\n' "$server" >&2
        exit 1
    fi
}

usage() {
    printf 'usage: run-prompt.sh [-t TARGET] "instruction"\n' >&2
    exit 64
}

target=
have_instruction=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        -t|--target)
            [ "$#" -ge 2 ] || usage
            [ -n "$2" ] || usage
            target=$2
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        --)
            shift
            [ "$#" -eq 1 ] || usage
            instruction=$1
            have_instruction=1
            break
            ;;
        -*)
            usage
            ;;
        *)
            [ "$#" -eq 1 ] || usage
            instruction=$1
            have_instruction=1
            break
            ;;
    esac
done

[ "$have_instruction" -eq 1 ] || usage
[ -n "$instruction" ] || usage

# -t wins over NOTIFY_DEFAULT_TARGET so a schedule can be explicit while a
# deployment-wide default still applies to schedules that omit it.
if [ -z "$target" ]; then
    target=${NOTIFY_DEFAULT_TARGET:-}
fi

# A target may declare a delivery host_scope and an allow_content flag.
# Resolve them here so the instruction can state the boundaries; notify.sh
# validates the same fields again before sending. A malformed value fails by
# target name only, never by id.
target_host_scope=
target_host_scope_json='[]'
target_content_disabled=0
target_runbooks_allowed=0
target_bookstack_url=
target_bookstack_books=
target_bookstack_token=
target_bookstack_token_id=
target_bookstack_token_secret=
target_runbooks_enabled=0
if [ -n "$target" ] && [ -n "${NOTIFY_TARGETS:-}" ]; then
    command -v jq >/dev/null 2>&1 || {
        printf 'run-prompt: jq is required to resolve target host_scope\n' >&2
        exit 1
    }
    targets_json=$(printf '%s' "$NOTIFY_TARGETS" | jq -ce '
        if type != "object" then error("not an object") else . end
    ' 2>/dev/null) || {
        printf 'run-prompt: NOTIFY_TARGETS must be a JSON object mapping names to targets\n' >&2
        exit 1
    }
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
    ' 2>/dev/null) || {
        printf 'run-prompt: could not validate NOTIFY_TARGETS host_scope\n' >&2
        exit 1
    }
    if [ -n "$bad_scope_targets" ]; then
        printf 'run-prompt: invalid host_scope for target(s) %s: expected a non-empty string or an array of non-empty strings\n' "$bad_scope_targets" >&2
        exit 1
    fi
    target_host_scope=$(printf '%s' "$targets_json" | jq -r --arg name "$target" '
        if has($name) then
            .[$name].host_scope // empty
            | if type == "array" then map("`" + . + "`") | join(", ") else "`" + . + "`" end
        else empty end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve target "%s"\n' "$target" >&2
        exit 1
    }
    target_host_scope_json=$(printf '%s' "$targets_json" | jq -c --arg name "$target" '
        if has($name) then
            (.[$name].host_scope // [] | if type == "string" then [.] else . end)
        else [] end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve host_scope for target "%s"\n' "$target" >&2
        exit 1
    }
    bad_content_targets=$(printf '%s' "$targets_json" | jq -r '
        [ to_entries[]
          | select((.value | type) == "object")
          | select(.value | has("allow_content"))
          | select((.value.allow_content | type) != "boolean")
          | .key
        ] | join(", ")
    ' 2>/dev/null) || {
        printf 'run-prompt: could not validate NOTIFY_TARGETS allow_content\n' >&2
        exit 1
    }
    if [ -n "$bad_content_targets" ]; then
        printf 'run-prompt: invalid allow_content for target(s) %s: expected a boolean\n' "$bad_content_targets" >&2
        exit 1
    fi
    target_content_disabled=$(printf '%s' "$targets_json" | jq -r --arg name "$target" '
        if has($name) and (.[$name].allow_content == false) then "1" else "0" end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve target "%s"\n' "$target" >&2
        exit 1
    }
    bad_runbooks_targets=$(printf '%s' "$targets_json" | jq -r '
        [ to_entries[]
          | select((.value | type) == "object")
          | select(.value | has("allow_runbooks"))
          | select((.value.allow_runbooks | type) != "boolean")
          | .key
        ] | join(", ")
    ' 2>/dev/null) || {
        printf 'run-prompt: could not validate NOTIFY_TARGETS allow_runbooks\n' >&2
        exit 1
    }
    if [ -n "$bad_runbooks_targets" ]; then
        printf 'run-prompt: invalid allow_runbooks for target(s) %s: expected a boolean\n' "$bad_runbooks_targets" >&2
        exit 1
    fi
    target_runbooks_allowed=$(printf '%s' "$targets_json" | jq -r --arg name "$target" '
        if has($name) and (.[$name].allow_runbooks == true) then "1" else "0" end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve target "%s"\n' "$target" >&2
        exit 1
    }
    # bookstack is optional per target. When present it must be an object with
    # an https URL and a non-empty comma-separated book list. Report bad
    # entries by target name only, never their ids or values.
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
    ' 2>/dev/null) || {
        printf 'run-prompt: could not validate NOTIFY_TARGETS bookstack\n' >&2
        exit 1
    }
    if [ -n "$bad_bookstack_targets" ]; then
        printf 'run-prompt: invalid bookstack for target(s) %s: expected an object with an https url, a comma-separated books list and a token suffix\n' "$bad_bookstack_targets" >&2
        exit 1
    fi
    target_bookstack_url=$(printf '%s' "$targets_json" | jq -r --arg name "$target" '
        if has($name) and (.[$name].bookstack | type) == "object"
        then .[$name].bookstack.url // empty else empty end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve target "%s"\n' "$target" >&2
        exit 1
    }
    target_bookstack_books=$(printf '%s' "$targets_json" | jq -r --arg name "$target" '
        if has($name) and (.[$name].bookstack | type) == "object"
        then .[$name].bookstack.books // empty else empty end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve target "%s"\n' "$target" >&2
        exit 1
    }
    target_bookstack_token=$(printf '%s' "$targets_json" | jq -r --arg name "$target" '
        if has($name) and (.[$name].bookstack | type) == "object"
        then .[$name].bookstack.token // empty else empty end
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve target "%s"\n' "$target" >&2
        exit 1
    }
    if [ "$target_runbooks_allowed" -eq 1 ] \
        && [ -n "$target_bookstack_url" ] \
        && [ -n "$target_bookstack_books" ] \
        && [ -n "$target_bookstack_token" ]; then
        # The suffix is validated above (letters, digits, underscore only), so
        # it is safe to use as part of an environment variable name.
        token_suffix=$(printf '%s' "$target_bookstack_token" | tr '[:lower:]' '[:upper:]')
        target_bookstack_token_id=$(printenv "BOOKSTACK_TOKEN_ID_${token_suffix}" 2>/dev/null || true)
        target_bookstack_token_secret=$(printenv "BOOKSTACK_TOKEN_SECRET_${token_suffix}" 2>/dev/null || true)
        if [ -n "$target_bookstack_token_id" ] && [ -n "$target_bookstack_token_secret" ]; then
            target_runbooks_enabled=1
        fi
    fi
fi

# The old host report always listed a configured set of hosts, even when one
# sent no samples. Mirror that here: HOST_REPORT_HOSTS is the ordered known-host
# list, filtered to the target's delivery scope when there is one. The agent
# cannot read the environment, so the list is carried into the instruction.
host_report_hosts_raw=${HOST_REPORT_HOSTS:-host1.example.com,host2.example.com,host3.example.com}
known_hosts_rendered=
if command -v jq >/dev/null 2>&1; then
    known_hosts_list=$(printf '%s' "$host_report_hosts_raw" | jq -Rr --argjson scope "$target_host_scope_json" '
        def norm: ascii_downcase | gsub("\\.+$"; "");
        ($scope | map(norm)) as $allowed
        | split(",")
        | map(gsub("^\\s+|\\s+$"; "") | norm)
        | map(select(length > 0))
        | map(. as $h
              | select(($allowed | length) == 0
                       or any($allowed[];
                              . as $e
                              | ($h == $e) or ($h | endswith("." + $e)))))
        | join(",")
    ' 2>/dev/null) || {
        printf 'run-prompt: could not resolve HOST_REPORT_HOSTS\n' >&2
        exit 1
    }
    known_hosts_rendered=$(printf '%s' "$known_hosts_list" | jq -Rr '
        split(",") | map(select(length > 0) | "`" + . + "`") | join(", ")
    ') || {
        printf 'run-prompt: could not render HOST_REPORT_HOSTS\n' >&2
        exit 1
    }
fi

command -v opencode >/dev/null 2>&1 || {
    printf 'run-prompt: opencode is not installed or not on PATH\n' >&2
    exit 1
}

if [ -z "${OPENCODE_MODEL:-}" ]; then
    printf 'run-prompt: OPENCODE_MODEL is not set (expected provider/model)\n' >&2
    exit 1
fi

# Validate whitespace the same way the inbound listener does. The fallback is
# optional; an unset value keeps today's single-attempt behaviour.
case "$OPENCODE_MODEL" in
    *[[:space:]]*)
        printf 'run-prompt: OPENCODE_MODEL must not contain whitespace\n' >&2
        exit 1
        ;;
esac

if [ -n "${OPENCODE_MODEL_FALLBACK:-}" ]; then
    case "$OPENCODE_MODEL_FALLBACK" in
        *[[:space:]]*)
            printf 'run-prompt: OPENCODE_MODEL_FALLBACK must not contain whitespace\n' >&2
            exit 1
            ;;
    esac
fi

if [ -n "$target" ]; then
    prompt="${instruction}

The target for this run is \`${target}\`. Send with:
/app/scripts/notify.sh -t ${target} -m \"<message>\""
    if [ -n "$target_host_scope" ]; then
        prompt="${prompt}

The delivery scope for this run is ${target_host_scope}. Nothing about any other host may appear in the message: report only on the scoped host, and if there is no data for it, say that plainly instead of using another host's numbers."
    fi
    if [ -n "$known_hosts_rendered" ]; then
        prompt="${prompt}

The known hosts for this run are ${known_hosts_rendered}. Include one line for each, in this order, even if it has no samples. Do not add a known host outside the permitted hosts."
    fi
    if [ "$target_content_disabled" -eq 1 ]; then
        if [ "$target_runbooks_enabled" -eq 1 ]; then
            prompt="${prompt}

Report host data and runbook procedures only: do not include any other material."
        else
            prompt="${prompt}

Report host data only: do not include any other material."
        fi
    fi
    if [ "$target_runbooks_enabled" -eq 1 ]; then
        prompt="${prompt}

Runbooks are available for this run: when the instruction asks how to carry out an operational task, or what a runbook flag or alert means, you may use the runbook tools."
    fi
else
    prompt=$instruction
fi

# The MCP server is spawned by opencode as a child process. Export the
# content-search block so oracle_query.py can refuse the tool even if the
# instruction is ignored.
if [ "$target_content_disabled" -eq 1 ]; then
    NOTIFY_CONTENT_DISABLED=1
    export NOTIFY_CONTENT_DISABLED
fi

# Runbooks are default-deny and per target: export the explicit grant and that
# target's own BookStack URL, books and resolved credential pair only when the
# grant, the instance settings and the named credential are all present, and
# clear any inherited values otherwise. bookstack.py refuses both tools unless
# it sees the grant and a complete instance, and it requires an https URL.
if [ "$target_runbooks_enabled" -eq 1 ]; then
    NOTIFY_RUNBOOKS_ALLOWED=1
    export NOTIFY_RUNBOOKS_ALLOWED
    NOTIFY_BOOKSTACK_URL=$target_bookstack_url
    export NOTIFY_BOOKSTACK_URL
    NOTIFY_BOOKSTACK_BOOKS=$target_bookstack_books
    export NOTIFY_BOOKSTACK_BOOKS
    NOTIFY_BOOKSTACK_TOKEN_ID=$target_bookstack_token_id
    export NOTIFY_BOOKSTACK_TOKEN_ID
    NOTIFY_BOOKSTACK_TOKEN_SECRET=$target_bookstack_token_secret
    export NOTIFY_BOOKSTACK_TOKEN_SECRET
else
    unset NOTIFY_RUNBOOKS_ALLOWED
    unset NOTIFY_BOOKSTACK_URL
    unset NOTIFY_BOOKSTACK_BOOKS
    unset NOTIFY_BOOKSTACK_TOKEN_ID
    unset NOTIFY_BOOKSTACK_TOKEN_SECRET
fi

# Export the effective host scope so ssh_probe.py refuses a host outside it
# even if the model ignores the instruction. The MCP server is a child of the
# opencode process, so it inherits the value.
if [ -n "$target_host_scope_json" ] && [ "$target_host_scope_json" != "[]" ]; then
    NOTIFY_HOST_SCOPE=$target_host_scope_json
    export NOTIFY_HOST_SCOPE
fi

# Fail before OpenCode starts if the MCP server cannot answer initialize and
# tools/list under the same environment the agent will use.
preflight_mcp "$mcp_server"

# OpenCode 2 removed --pure. --standalone runs a private server for this one
# invocation instead of attaching to (or leaving behind) a shared background
# service, which is what an unattended cron job wants. No plugins are installed
# in the image, so the run stays reproducible. There is no --auto: anything not
# explicitly allowed by opencode.json is denied.
#
# Provider-level logs are opt-in for a run someone is actively debugging.
# Determine the transcript location first so both attempts share it.
debug_log=
if [ -n "${XDG_DATA_HOME:-}" ]; then
    debug_dir=$XDG_DATA_HOME/opencode
elif [ -n "${HOME:-}" ]; then
    debug_dir=$HOME/.local/share/opencode
else
    debug_dir=
fi
if [ -n "$debug_dir" ]; then
    if mkdir -p "$debug_dir" 2>/dev/null && [ -w "$debug_dir" ]; then
        debug_log=$debug_dir/notify-debug.log
    else
        printf 'run-prompt: warning: debug log directory is not writable; transcription disabled\n' >&2
    fi
fi

# Helper: run one attempt with the given model. Echoes the exit code on stdout
# of the function via return status. Captured combined output goes to the file
# named by $attempt_output (set by caller) so the caller can inspect it.
run_attempt() {
    attempt_model=$1
    attempt_output=$2

    set -- opencode run
    if [ "${NOTIFY_DEBUG_LLM:-}" = "1" ]; then
        set -- "$@" --print-logs --log-level debug
    fi
    set -- "$@" \
        --standalone \
        --agent notify \
        --model "$attempt_model" \
        -- "$prompt"

    if [ -n "$debug_log" ]; then
        attempt_marker=
        if attempt_marker=$(mktemp "${TMPDIR:-/tmp}/notify-send.XXXXXX" 2>/dev/null); then
            rm -f "$attempt_marker"
        else
            attempt_marker=
        fi
        if [ -n "$attempt_marker" ]; then
            export NOTIFY_SEND_MARKER="$attempt_marker"
        else
            unset NOTIFY_SEND_MARKER
        fi
        before_size=0
        if [ -f "$debug_log" ]; then
            before_size=$(wc -c < "$debug_log" | tr -d ' ')
        fi
        # run through agent_capture, capturing its exit code
        set +e
        python3 "$script_dir/agent_capture.py" \
            --log "$debug_log" \
            --origin "schedule" \
            --model "$attempt_model" \
            --instruction "$prompt" \
            --marker "${attempt_marker:-}" \
            -- "$@"
        ATTEMPT_RC=$?
        set -e
        # extract this attempt's slice of the transcript for inspection
        if [ -f "$debug_log" ]; then
            tail -c +$((before_size + 1)) "$debug_log" > "$attempt_output" 2>/dev/null || true
        fi
        if [ -n "$attempt_marker" ]; then
            if grep -q 'sent=yes' "$attempt_output" 2>/dev/null; then
                : > "$attempt_output.sent"
            else
                rm -f "$attempt_output.sent"
            fi
            rm -f "$attempt_marker"
        else
            rm -f "$attempt_output.sent"
        fi
        return 0
    fi

    # No transcript: run directly, capturing output for inspection.
    set +e
    "$@" >"$attempt_output" 2>&1
    ATTEMPT_RC=$?
    set -e
    if [ -n "${NOTIFY_SEND_MARKER:-}" ] && [ -e "${NOTIFY_SEND_MARKER}" ]; then
        : > "$attempt_output.sent"
    else
        rm -f "$attempt_output.sent"
    fi
    return 0
}

model_unavailable() {
    # $1: output file
    if grep -Eiq 'free usage limit reached' "$1" 2>/dev/null; then
        return 0
    fi
    if grep -Eiq 'model not found|model unavailable|model is unavailable|unknown model|no such model|does not exist' "$1" 2>/dev/null; then
        return 0
    fi
    return 1
}

send_marker_present() {
    [ -f "$1.sent" ]
}

fallback="${OPENCODE_MODEL_FALLBACK:-}"

attempt_output=$(mktemp "${TMPDIR:-/tmp}/notify-attempt.XXXXXX" 2>/dev/null) || attempt_output=/tmp/notify-attempt.$$
: > "$attempt_output"

# First attempt on the preferred model.
set +e
run_attempt "$OPENCODE_MODEL" "$attempt_output"
set -e
rc=$ATTEMPT_RC

sent=0
if send_marker_present "$attempt_output"; then
    sent=1
fi

if [ "$rc" -ne 0 ] && [ "$sent" -eq 0 ] && [ -n "$fallback" ] && model_unavailable "$attempt_output"; then
    # Extract a short reason line for the transcript.
    reason=$(grep -Eim1 'free usage limit reached|model not found|model unavailable|model is unavailable|unknown model|no such model|does not exist' "$attempt_output" 2>/dev/null | head -n1 | tr -d '\r')
    [ -n "$reason" ] || reason="model unavailable"
    if [ -n "$debug_log" ]; then
        printf 'fallback: preferred model %s unavailable (%s); retrying with %s\n' \
            "$OPENCODE_MODEL" "$reason" "$fallback" >> "$debug_log"
    fi
    printf 'run-prompt: preferred model unavailable; retrying with %s\n' "$fallback" >&2

    set +e
    run_attempt "$fallback" "$attempt_output"
    set -e
    rc=$ATTEMPT_RC
fi

rm -f "$attempt_output" "$attempt_output.sent"
exit $rc

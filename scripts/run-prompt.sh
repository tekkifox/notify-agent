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
fi

# The old host report always listed a configured set of hosts, even when one
# sent no samples. Mirror that here: HOST_REPORT_HOSTS is the ordered known-host
# list, filtered to the target's delivery scope when there is one. The agent
# cannot read the environment, so the list is carried into the instruction.
host_report_hosts_raw=${HOST_REPORT_HOSTS:-rossmoney.me,photos.rossmoney.me,vortexservers.co.uk}
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
        prompt="${prompt}

Content search is disabled for this run: do not use the content search tool, and do not include anything about the operator's site, CV, projects or pages. Report host data only."
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
set -- opencode run
if [ "${NOTIFY_DEBUG_LLM:-}" = "1" ]; then
    set -- "$@" --print-logs --log-level debug
fi
set -- "$@" \
    --standalone \
    --agent notify \
    --model "$OPENCODE_MODEL" \
    -- "$prompt"

# Capture the complete run to the same transcript the inbound listener uses.
# Both derive the directory from XDG_DATA_HOME, so there is one location. If
# neither XDG_DATA_HOME nor HOME is usable, keep today's inherited output
# rather than failing a schedule.
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

if [ -n "$debug_log" ]; then
    send_marker=
    if send_marker=$(mktemp "${TMPDIR:-/tmp}/notify-send.XXXXXX" 2>/dev/null); then
        rm -f "$send_marker"
        export NOTIFY_SEND_MARKER="$send_marker"
    else
        send_marker=
        printf 'run-prompt: warning: could not create a send marker; the transcript will report sent=unknown\n' >&2
    fi
    set -- python3 "$script_dir/agent_capture.py" \
        --log "$debug_log" \
        --origin "schedule:${target:-default}" \
        --model "$OPENCODE_MODEL" \
        --instruction "$prompt" \
        --marker "${send_marker:-}" \
        -- "$@"
fi

exec "$@"

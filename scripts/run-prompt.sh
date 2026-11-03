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

# A scheduled run gets one more chance when only the Oracle self-test fails, so
# a transient database blip does not lose the whole digest. The delay is short
# enough to retry inside the same cron minute. Tests override it to 0.
preflight_retry_delay=${NOTIFY_PREFLIGHT_RETRY_DELAY:-60}
case "$preflight_retry_delay" in
    ''|*[!0-9]*)
        printf 'run-prompt: NOTIFY_PREFLIGHT_RETRY_DELAY must be a whole number of seconds\n' >&2
        exit 1
        ;;
esac

# Set when the self-test fails so the caller can log and report the reason.
preflight_reason=

# Single flight: the scheduler and the inbound listener share one lock, so two
# OpenCode processes never touch the same session database at once. A wait that
# expires is a skip, not a failure: exit 0 so supercronic stays quiet and the
# next schedule can still run.
run_lock=${NOTIFY_RUN_LOCK:-${TMPDIR:-/tmp}/notify-agent-run.lock}
run_lock_wait=${NOTIFY_RUN_LOCK_WAIT:-600}
case "$run_lock_wait" in
    ''|*[!0-9]*)
        printf 'run-prompt: NOTIFY_RUN_LOCK_WAIT must be a whole number of seconds\n' >&2
        exit 1
        ;;
esac

# Every model attempt is bounded, so a call that never returns cannot hold the
# lock (and the following digest) for ever. A timed-out attempt is a failure
# and follows the existing retry and fallback rules.
run_timeout=${NOTIFY_RUN_TIMEOUT:-300}
case "$run_timeout" in
    ''|*[!0-9]*)
        printf 'run-prompt: NOTIFY_RUN_TIMEOUT must be a whole number of seconds\n' >&2
        exit 1
        ;;
esac

# An exhausted free tier is remembered for this long, so the next run goes
# straight to the fallback instead of probing a dead model again. Zero disables
# the memo.
quota_memo_seconds=${NOTIFY_QUOTA_MEMO_SECONDS:-900}
case "$quota_memo_seconds" in
    ''|*[!0-9]*)
        printf 'run-prompt: NOTIFY_QUOTA_MEMO_SECONDS must be a whole number of seconds\n' >&2
        exit 1
        ;;
esac
if [ -n "${XDG_STATE_HOME:-}" ]; then
    state_home=$XDG_STATE_HOME
elif [ -n "${HOME:-}" ]; then
    state_home=$HOME/.local/state
else
    state_home=${TMPDIR:-/tmp}
fi
quota_memo_file=$state_home/notify-agent/quota-skip.json

# `timeout` bounds every attempt. --kill-after is used when this build supports
# it, so a wedged process that ignores TERM is killed shortly afterwards. A
# missing `timeout` only costs the bound, never the run.
timeout_available=0
timeout_kill_after=0
if command -v timeout >/dev/null 2>&1; then
    timeout_available=1
    if timeout --kill-after=10 1 true >/dev/null 2>&1; then
        timeout_kill_after=1
    fi
else
    printf 'run-prompt: warning: timeout is not installed; attempts are not bounded\n' >&2
fi

# Remember or forget an exhausted free tier. The stamp is one small JSON object
# under the state directory; an unreadable or expired stamp is ignored, so it
# can never block a run. `check` prints the expiry as HH:MM in UTC when a stamp
# for the given model is still valid.
quota_memo() {
    quota_command=$1
    shift
    python3 - "$quota_memo_file" "$quota_command" "$@" <<'PY'
import json
import os
import sys
import time

path = sys.argv[1]
command = sys.argv[2] if len(sys.argv) > 2 else ""
model = sys.argv[3] if len(sys.argv) > 3 else ""

if command == "check":
    try:
        with open(path, "r", encoding="utf-8") as handle:
            stamp = json.load(handle)
    except (OSError, ValueError):
        sys.exit(1)
    if not isinstance(stamp, dict) or stamp.get("model") != model:
        sys.exit(1)
    expires = stamp.get("expires")
    if not isinstance(expires, (int, float)):
        sys.exit(1)
    now = time.time()
    if expires <= now:
        sys.exit(1)
    sys.stdout.write(time.strftime("%H:%M", time.gmtime(expires)))
    sys.exit(0)

if command == "write":
    if len(sys.argv) <= 4:
        sys.exit(1)
    try:
        seconds = int(sys.argv[4])
    except ValueError:
        sys.exit(1)
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        sys.exit(1)
    temporary = path + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump({"model": model, "expires": int(time.time()) + seconds}, handle)
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        sys.exit(1)
    sys.exit(0)

if command == "clear":
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        sys.exit(1)
    sys.exit(0)

sys.exit(2)
PY
}

# True only for the "free usage limit reached" failure, which is the one the
# memo is for; a missing model or a network error is not a quota problem.
quota_exhausted() {
    grep -Eiq 'free usage limit reached' "$1" 2>/dev/null
}

# The self-test already returns a short, secret-free reason. Log it with the
# run-prompt prefix so an incident is answerable from the container log alone.
# Never print the MCP server's stdout: the environment may contain credentials.
log_preflight_reason() {
    reason=$1
    [ -n "$reason" ] || return 0
    printf '%s\n' "$reason" | while IFS= read -r line; do
        [ -n "$line" ] || continue
        printf 'run-prompt: MCP preflight Oracle self-test reason: %s\n' "$line" >&2
    done
}

# Prove that the MCP server starts and advertises both tools before OpenCode
# runs. The handshake does not touch the database, so this is cheap. Server
# output is captured, never printed: it must not reach the log, and the
# environment may contain credentials. Return codes: 0 healthy, 1 a
# configuration/handshake failure, 2 the Oracle self-test failed.
preflight_mcp() {
    server=$1

    if [ ! -f "$server" ]; then
        printf 'run-prompt: MCP preflight failed: server not found: %s\n' "$server" >&2
        return 1
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        printf 'run-prompt: MCP preflight failed: python3 is not installed or not on PATH\n' >&2
        return 1
    fi

    responses=$(
        printf '%s\n' \
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"run-prompt-preflight","version":"1.0"}}}' \
            '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
            '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
            | python3 "$server" 2>/dev/null
    ) || {
        printf 'run-prompt: MCP preflight failed: %s did not start\n' "$server" >&2
        return 1
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
        return 1
    fi

    # The tool listing is not enough: prove the database is reachable too, so a
    # run cannot message the channel and only then discover Oracle is down.
    # Capture stderr (the self-test's safe reason) and discard stdout. The
    # reason is logged by log_preflight_reason and never mixed with the
    # server's protocol output.
    if selftest_reason=$(python3 "$server" --selftest 2>&1 >/dev/null); then
        return 0
    fi
    preflight_reason=$selftest_reason
    log_preflight_reason "$selftest_reason"
    printf 'run-prompt: MCP preflight failed: %s Oracle self-test failed\n' "$server" >&2
    return 2
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

# Read one escaped string field from bot_config.py --json without needing jq.
json_value() {
    python3 -c '
import json
import sys

print(json.load(sys.stdin).get(sys.argv[1], ""), end="")
' "$1"
}

# Prefer the CMS configuration when the operator enabled it. bot_config.py
# validates the panel's document with the same rules notify.sh applies, caches
# the last good copy and falls back to this environment on any failure. When
# BOT_SETTINGS_URL is unset nothing is called and NOTIFY_TARGETS is untouched.
# One resolve returns both values, so the panel is fetched once.
if [ -n "${BOT_SETTINGS_URL:-}" ]; then
    if command -v python3 >/dev/null 2>&1; then
        resolved_config=$(python3 "$script_dir/bot_config.py" --json) || {
            printf 'run-prompt: could not resolve bot configuration; using NOTIFY_TARGETS from the environment\n' >&2
            resolved_config=
        }
        if [ -n "$resolved_config" ]; then
            resolved_targets=$(printf '%s' "$resolved_config" | json_value targets) || {
                printf 'run-prompt: could not read resolved targets; using NOTIFY_TARGETS from the environment\n' >&2
                resolved_targets=${NOTIFY_TARGETS:-}
            }
            resolved_host_ports=$(printf '%s' "$resolved_config" | json_value host_ports) || {
                printf 'run-prompt: could not read resolved SSH ports; using the environment\n' >&2
                resolved_host_ports=${NOTIFY_HOST_PORTS:-}
            }
        else
            resolved_targets=${NOTIFY_TARGETS:-}
            resolved_host_ports=${NOTIFY_HOST_PORTS:-}
        fi
        NOTIFY_TARGETS=$resolved_targets
        export NOTIFY_TARGETS
        # Per-host SSH ports are optional. Export the resolved map when there
        # is one, and clear an inherited value when the panel has none so the
        # global SSH_PORT governs; ssh_probe.py does the same for a hand-run.
        if [ -n "$resolved_host_ports" ]; then
            NOTIFY_HOST_PORTS=$resolved_host_ports
            export NOTIFY_HOST_PORTS
        else
            unset NOTIFY_HOST_PORTS
        fi
    else
        printf 'run-prompt: python3 is not installed; using NOTIFY_TARGETS from the environment\n' >&2
    fi
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

# Report a persistent preflight failure without starting OpenCode. The message
# is fixed text plus the self-test's safe reason: it never contains statistics
# and is delivered through notify.sh, so the target allowlist and host_scope
# still decide whether it may be sent.
send_preflight_failure() {
    reason=$(printf '%s\n' "$preflight_reason" | head -n 1)
    [ -n "$reason" ] || reason="Oracle self-test failed"
    # notify.sh refuses a message whose dotted tokens are outside the target's
    # host_scope. A wallet filename can look like a hostname, so mask dotted
    # tokens here; the full secret-free reason is already in the log above.
    if command -v jq >/dev/null 2>&1; then
        if scrubbed=$(printf '%s' "$reason" | jq -Rr \
            'gsub("[A-Za-z0-9][A-Za-z0-9._-]*\\.[A-Za-z]{2,}"; "[redacted]")' 2>/dev/null); then
            reason=$scrubbed
        fi
    fi
    message="The scheduled task could not run because the database was unreachable, so no digest or report was sent. Reason: ${reason}"
    if [ -n "$target" ]; then
        if "$script_dir/notify.sh" -t "$target" -m "$message" >&2; then
            printf 'run-prompt: sent failure notification to target %s\n' "$target" >&2
        else
            printf 'run-prompt: could not send failure notification to target %s\n' "$target" >&2
        fi
    else
        if "$script_dir/notify.sh" -m "$message" >&2; then
            printf 'run-prompt: sent failure notification\n' >&2
        else
            printf 'run-prompt: could not send failure notification\n' >&2
        fi
    fi
}

# Fail before OpenCode starts if the MCP server cannot answer initialize and
# tools/list under the same environment the agent will use. Only a failed
# Oracle self-test is retried: a missing server or a missing tool is a
# configuration error, and a second database failure still fails the run.
preflight_rc=0
preflight_retried=0
preflight_mcp "$mcp_server" || preflight_rc=$?

if [ "$preflight_rc" -eq 2 ]; then
    preflight_retried=1
    printf 'run-prompt: MCP preflight Oracle self-test failed; retrying once in %s seconds\n' \
        "$preflight_retry_delay" >&2
    sleep "$preflight_retry_delay"
    preflight_rc=0
    preflight_mcp "$mcp_server" || preflight_rc=$?
fi

if [ "$preflight_rc" -ne 0 ]; then
    if [ "$preflight_retried" -eq 1 ]; then
        send_preflight_failure
    fi
    exit 1
fi

# OpenCode 2 removed --pure. The run attaches to the container's shared
# background service rather than passing --standalone to start a private server
# for this one invocation: the service registers the MCP servers once, so
# oracle_query and ssh_probe are available from the first turn, and no second
# server contends with the service for the session database. No plugins are
# installed in the image, so the run stays reproducible. There is no --auto:
# anything not explicitly allowed by opencode.json is denied.
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
    # Hard bound for this attempt, around the agent and (in the transcript
    # path) the capture wrapper. A timeout exits non-zero, so it is handled by
    # the same failure rules as any other failed attempt.
    if [ "$timeout_available" -eq 1 ]; then
        if [ "$timeout_kill_after" -eq 1 ]; then
            set -- timeout --signal=TERM --kill-after=10 "$run_timeout" "$@"
        else
            set -- timeout --signal=TERM "$run_timeout" "$@"
        fi
    fi
    if [ "${NOTIFY_DEBUG_LLM:-}" = "1" ]; then
        set -- "$@" --print-logs --log-level debug
    fi
    set -- "$@" \
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
            -- "$@" 9>&-
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

    # No transcript: run directly, capturing output for inspection. The lock fd
    # is closed for the child, so a lingering MCP process cannot keep holding
    # the single-flight lock after this run finishes.
    set +e
    "$@" >"$attempt_output" 2>&1 9>&-
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

# Take the shared single-flight lock before the first model attempt and hold it
# for every attempt of this run. The kernel releases it when this script exits,
# including on an error or a timeout, so there is no pid file to go stale. A
# wait that expires is a skip: exit 0, so it is not a failed job.
if command -v flock >/dev/null 2>&1; then
    if : 2>/dev/null >>"$run_lock"; then
        exec 9>>"$run_lock"
        if ! flock -w "$run_lock_wait" 9 2>/dev/null; then
            printf 'run-prompt: skipped: another run is in progress\n' >&2
            rm -f "$attempt_output"
            exit 0
        fi
    else
        printf 'run-prompt: warning: run lock %s is not writable; running without the single-flight lock\n' "$run_lock" >&2
    fi
else
    printf 'run-prompt: warning: flock is not installed; running without the single-flight lock\n' >&2
fi

# A dead free tier is remembered: while a valid stamp names the preferred
# model, go straight to the fallback instead of probing it again. Without a
# fallback the memo is pointless, so the preferred model is still attempted.
skip_until=
if [ "$quota_memo_seconds" -gt 0 ] && [ -n "$fallback" ]; then
    skip_until=$(quota_memo check "$OPENCODE_MODEL") || skip_until=
fi

sent=0
if [ -n "$skip_until" ]; then
    printf 'run-prompt: preferred model %s skipped until %sZ (quota)\n' \
        "$OPENCODE_MODEL" "$skip_until" >&2
    set +e
    run_attempt "$fallback" "$attempt_output"
    set -e
    rc=$ATTEMPT_RC
else
    # First attempt on the preferred model.
    set +e
    run_attempt "$OPENCODE_MODEL" "$attempt_output"
    set -e
    rc=$ATTEMPT_RC

    if send_marker_present "$attempt_output"; then
        sent=1
    fi

    if [ "$rc" -eq 0 ] && [ "$quota_memo_seconds" -gt 0 ]; then
        # The preferred model works again: forget the exhausted tier.
        quota_memo clear || printf 'run-prompt: warning: could not clear the quota memo\n' >&2
    fi

    if [ "$rc" -ne 0 ] && [ "$sent" -eq 0 ] && [ -n "$fallback" ] && model_unavailable "$attempt_output"; then
        # Remember an exhausted free tier so the next run does not spend time
        # (and risk a hang) discovering it again.
        if quota_exhausted "$attempt_output" && [ "$quota_memo_seconds" -gt 0 ]; then
            quota_memo write "$OPENCODE_MODEL" "$quota_memo_seconds" \
                || printf 'run-prompt: warning: could not record the quota memo\n' >&2
        fi
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
fi

rm -f "$attempt_output" "$attempt_output.sent"
exit $rc

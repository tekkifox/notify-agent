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
else
    prompt=$instruction
fi

# --pure disables external plugins for a reproducible run. There is no --auto:
# anything not explicitly allowed by opencode.json is denied.
exec opencode run \
    --pure \
    --agent notify \
    --model "$OPENCODE_MODEL" \
    -- "$prompt"

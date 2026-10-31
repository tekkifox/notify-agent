#!/bin/sh
# Run one unattended OpenCode prompt with the locked-down `notify` agent.
#
# The agent can compose text and call /app/scripts/notify.sh, nothing else.
# See prompts/notify.md and opencode.json.
#
# Usage:
#   run-prompt.sh "Compose and send a short status message"
set -eu

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    printf 'usage: run-prompt.sh "instruction"\n' >&2
    exit 64
fi

command -v opencode >/dev/null 2>&1 || {
    printf 'run-prompt: opencode is not installed or not on PATH\n' >&2
    exit 1
}

if [ -z "${OPENCODE_MODEL:-}" ]; then
    printf 'run-prompt: OPENCODE_MODEL is not set (expected provider/model)\n' >&2
    exit 1
fi

# --pure disables external plugins for a reproducible run. There is no --auto:
# anything not explicitly allowed by opencode.json is denied.
exec opencode run \
    --pure \
    --agent notify \
    --model "$OPENCODE_MODEL" \
    -- "$1"

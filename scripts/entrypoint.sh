#!/bin/sh
# Compose entrypoint dispatcher.
#
# With no arguments it behaves exactly like the image ENTRYPOINT:
#   supercronic /app/crontab
# With arguments it runs them directly, which keeps the documented
# `docker compose run ...` commands simple:
#   docker compose run --rm agent opencode auth login "OpenCode Console" --method device
#   echo hi | docker compose run --rm -T agent /app/scripts/notify.sh
set -eu

if [ "$#" -eq 0 ]; then
    exec supercronic /app/crontab
fi

exec "$@"

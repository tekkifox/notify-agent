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

CRONTAB=/app/crontab

# Docker creates an empty directory at a bind-mount path whose host source
# does not exist. supercronic would start on that and silently schedule
# nothing, so refuse to start unless the schedule file is really there.
check_crontab() {
    if [ ! -f "$CRONTAB" ] || [ ! -r "$CRONTAB" ]; then
        cat >&2 <<EOF
FATAL: schedule file $CRONTAB is missing or unreadable.

supercronic needs a readable regular file at $CRONTAB. If the host file
does not exist, Docker mounts an empty directory there and the scheduler
would start with no schedules.

Create the file on the host and point SCHEDULES_FILE at it, for example:
    install -m 0644 ./crontab /srv/notify-agent/crontab
    printf 'SCHEDULES_FILE=/srv/notify-agent/crontab\\n' >> .env
    docker compose up -d

See README "Add a schedule".
EOF
        exit 1
    fi
}

if [ "$#" -eq 0 ]; then
    check_crontab
    exec supercronic "$CRONTAB"
fi

exec "$@"

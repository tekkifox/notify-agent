#!/bin/sh
# Compose entrypoint dispatcher and supervisor.
#
# With no arguments it runs both long-lived processes:
#   supercronic /app/crontab          scheduled prompts
#   python3 /app/scripts/discord_inbound.py
#                                     inbound Discord replies
#
# The two are independent children. If one exits, the supervisor reaps it and
# restarts it without touching the other, so a listener crash cannot take the
# schedule down and a bad schedule cannot silence the listener. SIGTERM and
# SIGINT are forwarded to both, and both are reaped before the container exits.
#
# With arguments it runs them directly, which keeps the documented
# `docker compose run ...` commands simple:
#   docker compose run --rm agent opencode auth login "OpenCode Console" --method device
#   echo hi | docker compose run --rm -T agent /app/scripts/notify.sh
set -eu

CRONTAB=/app/crontab
LISTENER=/app/scripts/discord_inbound.py
RESTART_DELAY=5

STOP=0
CRON_PID=
LISTENER_PID=

log() {
    printf 'entrypoint: %s\n' "$*" >&2
}

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

start_cron() {
    supercronic "$CRONTAB" &
    CRON_PID=$!
    log "started supercronic (pid $CRON_PID)"
}

inbound_enabled() {
    [ -n "${NOTIFY_INBOUND_CHANNEL_ID:-}" ] \
        || [ -n "${NOTIFY_INBOUND_SOURCES:-}" ]
}

start_listener() {
    if ! inbound_enabled; then
        log "inbound disabled: neither NOTIFY_INBOUND_CHANNEL_ID nor NOTIFY_INBOUND_SOURCES is set"
        LISTENER_PID=
        return 0
    fi
    python3 "$LISTENER" &
    LISTENER_PID=$!
    log "started inbound listener (pid $LISTENER_PID)"
}

stop_children() {
    STOP=1
    for pid in "$LISTENER_PID" "$CRON_PID"; do
        [ -n "$pid" ] || continue
        kill -TERM "$pid" 2>/dev/null || true
    done

    # Wait for both together so the compose stop grace period is not spent
    # twice. Then force-kill anything that ignored SIGTERM and reap it.
    seconds=0
    while [ "$seconds" -lt 20 ]; do
        alive=0
        for pid in "$LISTENER_PID" "$CRON_PID"; do
            [ -n "$pid" ] || continue
            if kill -0 "$pid" 2>/dev/null; then
                alive=1
            fi
        done
        if [ "$alive" -eq 0 ]; then
            break
        fi
        sleep 1
        seconds=$((seconds + 1))
    done

    for pid in "$LISTENER_PID" "$CRON_PID"; do
        [ -n "$pid" ] || continue
        kill -KILL "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    LISTENER_PID=
    CRON_PID=
}

trap 'stop_children; exit 0' TERM INT

if [ "$#" -eq 0 ]; then
    check_crontab
    start_cron
    start_listener

    while [ "$STOP" -eq 0 ]; do
        sleep 2

        if [ -n "$CRON_PID" ] && ! kill -0 "$CRON_PID" 2>/dev/null; then
            wait "$CRON_PID" 2>/dev/null || true
            CRON_PID=
            log "supercronic exited; restarting in ${RESTART_DELAY}s"
            sleep "$RESTART_DELAY"
            if [ "$STOP" -eq 0 ]; then
                start_cron
            fi
        fi

        if inbound_enabled && [ -z "$LISTENER_PID" ]; then
            start_listener
        elif [ -n "$LISTENER_PID" ] && ! kill -0 "$LISTENER_PID" 2>/dev/null; then
            wait "$LISTENER_PID" 2>/dev/null || true
            LISTENER_PID=
            log "inbound listener exited; restarting in ${RESTART_DELAY}s"
            sleep "$RESTART_DELAY"
            if [ "$STOP" -eq 0 ]; then
                start_listener
            fi
        fi
    done

    stop_children
    exit 0
fi

exec "$@"

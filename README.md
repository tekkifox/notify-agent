# notify-agent

A hardened, single-container notification agent. It runs the [OpenCode](https://opencode.ai) CLI on a schedule, turns a plain-English instruction into a message, and sends that message to Discord. Delivery itself is deterministic and does not involve the LLM.

Designed for an unattended small VPS: Ubuntu 24.04, x86_64, 2 vCPU, 1.9 GB RAM, Docker 29 with Compose 2.40. Everything except Docker lives inside the container.

- **Today:** Discord incoming webhook.
- **Later:** Composio, switchable through environment variables without rebuilding.
- **Schedules:** [supercronic](https://github.com/aptible/supercronic), a static non-root cron.
- **State:** one named volume for OpenCode auth and sessions.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Dockerfile` | Pinned OpenCode `1.19.5`, supercronic `0.2.51`, non-root uid `10001`. |
| `compose.yaml` | One `agent` service with the hardened runtime posture. |
| `crontab` | Supercronic schedules. One line per schedule. |
| `scripts/notify.sh` | Deterministic delivery. No LLM. |
| `scripts/run-prompt.sh` | Runs `opencode run` with the locked-down `notify` agent. |
| `scripts/entrypoint.sh` | Compose dispatcher: no args runs supercronic, args run the command. |
| `opencode.json` | `notify` agent definition: only `notify.sh` may be executed. |
| `prompts/notify.md` | System prompt for the `notify` agent. |
| `stack.env.example` | Every environment variable, documented, no secrets. |

## Quick start

```sh
cp stack.env.example stack.env
chmod 600 stack.env
$EDITOR stack.env          # set DISCORD_WEBHOOK_URL and OPENCODE_MODEL

docker compose build
docker compose up -d
```

`stack.env` is gitignored and excluded by `.dockerignore`. It is the only place secrets enter the container. Never put a real webhook or API key in the image, `compose.yaml`, or this repository.

`docker compose config` validates on a fresh checkout because `stack.env` is declared optional. If it is missing, delivery fails loudly rather than silently doing nothing.

## One-time authentication

OpenCode needs a provider login once. The device flow prints a URL and code; complete it in a browser on any machine:

```sh
docker compose run --rm agent opencode auth login
```

Choose the provider and model you set in `OPENCODE_MODEL` (for example `provider/model`). Credentials are written to the `opencode-data` volume at `/home/agent/.local/share/opencode`, so they survive container recreation and reboots. They do not survive `docker compose down -v`.

The compose entrypoint dispatcher exists so this command works without an `--entrypoint` override: with arguments it execs the command, with no arguments it execs `supercronic /app/crontab`.

## Test delivery (sends a real message)

```sh
echo hi | docker compose run --rm -T agent /app/scripts/notify.sh
```

This bypasses the LLM and posts directly. It is the fastest way to prove the webhook works. To test the full prompt path as well:

```sh
docker compose run --rm -T agent /app/scripts/run-prompt.sh \
  "Compose a one-line test message saying the notification agent is working. Send it now with notify.sh."
```

Both commands send to the real channel. There is no dry-run mode, because a fake success would be worse than a loud failure. `notify.sh --help` prints usage without sending.

## Add a schedule

Edit `crontab`, then rebuild because the file is baked read-only into the image:

```sh
$EDITOR crontab
docker compose up -d --build
```

Format: one line per schedule, five cron fields, then the prompt passed to `run-prompt.sh`:

```cron
0 9 * * * /app/scripts/run-prompt.sh "Compose and send the 09:00 digest."
```

The fields are minute, hour, day-of-month, month, day-of-week. Times use `TZ` from `stack.env` (default `UTC`). The file already contains a working 09:00 daily digest plus commented examples for weekdays, weekly, monthly, and hourly schedules.

Schedules are trusted operator configuration, not user input. Anyone who can edit `crontab` can make the agent post anything.

## Read logs

```sh
docker compose logs -f --tail=100 agent
```

Supercronic logs each job start and exit to the container log, and OpenCode's final response goes to stdout. Delivery results and short failure reasons also appear there. Logs contain message text and model output; they do **not** contain the webhook URL or API keys. Treat logs as sensitive. Compose caps them at 3 × 10 MB.

A failed job exits non-zero and is visible in the log, but there is no automatic retry. If you need retries, add them at the schedule level (for example a second prompt) rather than hiding failures.

## Delivery behaviour

`scripts/notify.sh`:

- Reads the message from stdin or from `-m MESSAGE`.
- Uses Discord when `DISCORD_WEBHOOK_URL` is set.
- Otherwise uses Composio when both `COMPOSIO_API_KEY` and `COMPOSIO_TOOL` are set.
- Otherwise exits `1` with a short reason.
- Truncates to 2,000 Unicode characters before sending, and says so on stderr.
- Sends `allowed_mentions: {parse: []}`, so `@everyone`, `@here`, and role pings are suppressed.
- Exits non-zero on network errors, rate limits, and non-2xx responses. It never prints the webhook URL or API key.
- Does not follow redirects and requires `https://`.

### Composio limitation

The Composio path is behind `COMPOSIO_API_KEY` + `COMPOSIO_TOOL` and can be enabled without rebuilding:

```sh
COMPOSIO_API_KEY=...
COMPOSIO_TOOL=...
COMPOSIO_USER_ID=...
COMPOSIO_MESSAGE_FIELD=content
```

**Known limitation:** the Captain's Composio Discord connection is user-OAuth based and cannot post to a channel. That path is therefore scaffolded for a future server-side Composio tool, not a working Discord replacement. The arguments sent are `{"user_id": ..., "arguments": {"<COMPOSIO_MESSAGE_FIELD>": "<message>"}}`; adapt `COMPOSIO_TOOL` and the message field to the chosen tool. It has not been tested against a live Composio account.

## Security posture

These are enforced in `compose.yaml`, not conventions:

| Control | Setting |
| --- | --- |
| User | `user: "10001:10001"` (fixed uid/gid) |
| Root filesystem | `read_only: true` |
| Writable state | named volume `opencode-data:/home/agent/.local/share/opencode` plus tmpfs only |
| tmpfs | `/tmp`, `/home/agent/.opencode`, `/home/agent/.config`, `/home/agent/.cache`, `/home/agent/.local/state` |
| Capabilities | `cap_drop: [ALL]` |
| Privilege escalation | `security_opt: [no-new-privileges:true]` |
| Network | no `ports:` at all; outbound only |
| Memory | `mem_limit: 512m` |
| Processes | `pids_limit: 200` |
| Restart | `restart: unless-stopped` |
| Docker socket | not mounted |
| Host mounts | none beyond the named volume |
| Privileged mode | never used |
| Secrets | `env_file: stack.env` only, mode 0600 on the host, gitignored |
| Agent tools | deny-by-default; only `/app/scripts/notify.sh -m ...` is allowed |
| OpenCode sharing | disabled; snapshots and autoupdate disabled |
| Logs | `json-file`, 10 MB × 3 |

The image installs OpenCode from the official installer pinned to `1.19.5`, verifies supercronic by SHA-256, and pins the base image by digest. OpenCode itself lives under `/opt/opencode`, so the runtime home directory can stay read-only.

## Threat model

This is a small, single-tenant agent, not a sandbox for untrusted users.

**Assets inside the container**

- The Discord webhook secret, which can post to one channel.
- The provider OAuth token or API key in the `opencode-data` volume.
- The ability to send messages as the bot/user the credentials represent.
- Message content and model prompts, which leave the host for the model provider.

**What the agent can do**

- Reach the internet outbound.
- Read its environment (including the webhook and provider credentials).
- Call exactly one shell command, `/app/scripts/notify.sh -m ...`, through the OpenCode bash tool.
- Write only to the named volume and tmpfs.

**What it cannot do**

- Accept inbound connections (no published ports).
- Access the Docker socket, the host filesystem, `/etc`, `/home`, or other containers.
- Gain capabilities or new privileges.
- Browse the web, read repository files, spawn subagents, or run arbitrary shell commands: the `notify` agent denies every tool except the one allowed `notify.sh` command.

**Scenarios and residual risk**

- **Prompt injection.** The operator prompt is trusted, but text pasted into it may not be. A manipulated model can still send a misleading message and can echo content present in its prompt. It cannot read files, browse, or run arbitrary commands. Do not put secrets in schedule prompts.
- **Malicious or compromised model provider.** The provider sees the prompt and the composed message. It can influence message content but not the container's shell surface.
- **Container compromise.** An attacker who compromises the OpenCode process gets the webhook, provider token, and outbound internet. There is no egress allowlist, so data can be exfiltrated. They still need a container or kernel escape to reach the host.
- **Docker-host compromise.** Anyone with access to the Docker daemon can read the volume and change the container. Docker is not a security boundary against a hostile host.
- **Supply chain.** OpenCode and supercronic are pinned, and supercronic is checksum-verified; the base image is digest-pinned. Rebuilds still trust the upstream registries and installer.
- **Logs.** Container logs contain message text and model output. Restrict access to `docker compose logs`.
- **No rate limiting.** A misconfigured schedule can post frequently; Discord will rate-limit and `notify.sh` will fail loudly.

**Explicit non-goals**

- Running untrusted prompts for untrusted users.
- Multi-tenant isolation.
- Protecting secrets from someone who already has the Docker socket or the host.
- Guaranteeing delivery. Failures are reported, not hidden.

## Verification

Verified while building this branch:

- `docker compose config` validates.
- The image builds natively and with `--platform linux/amd64` (the target host architecture).
- `docker run --rm --platform linux/amd64 --entrypoint sh <image> -c 'id; supercronic -version'` prints:
  `uid=10001(agent) gid=10001(agent) groups=10001(agent)` and `v0.2.51`.
- The same image reports OpenCode `1.19.5`.
- Running the compose service shows a read-only root filesystem, `CapEff: 0`, `NoNewPrivs: 1`, memory limit `536870912`, pids limit `200`, one writable named volume, and no published ports.
- `shellcheck` (container image `koalaman/shellcheck:stable`) and `sh -n` pass on all three scripts.
- A local TLS stub received a Discord-shaped POST with exactly 2,000 characters and `allowed_mentions: {"parse":[]}`; a second stub received the Composio path and payload, and the API key did not appear in output.
- `opencode debug agent notify` resolves with `bash` as the only enabled tool and a deny-all rule except `/app/scripts/notify.sh`.

Not verified, because this repository contains no credentials:

- `opencode auth login` (no provider account was used).
- A live Discord send (no real webhook was used).
- A live Composio send (no real API key was used; the Composio path was only exercised against a local stub).

Do those three checks on the target host after copying `stack.env` and completing the device flow.

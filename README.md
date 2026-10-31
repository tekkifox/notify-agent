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
| `Dockerfile` | Pinned OpenCode `2.2.1`, supercronic `0.2.51`, non-root uid `10001`. |
| `.github/workflows/publish-image.yml` | Builds and publishes `ghcr.io/tekkifox/notify-agent` on `main`, then triggers the Portainer redeploy webhook. |
| `compose.yaml` | One `agent` service with the hardened runtime posture. |
| `crontab` | Supercronic schedules. Ships with no active schedule; every example is commented out. |
| `scripts/notify.sh` | Deterministic delivery. No LLM. |
| `scripts/run-prompt.sh` | Runs `opencode run` with the locked-down `notify` agent. |
| `scripts/entrypoint.sh` | Compose dispatcher: no args runs supercronic, args run the command. |
| `opencode.json` | `notify` agent definition: only `notify.sh` and the read-only Oracle MCP tool may be used. |
| `prompts/notify.md` | System prompt for the `notify` agent. |
| `mcp/oracle_query.py` | Single-file read-only Oracle MCP server (Python 3, `oracledb` thin mode). |
| `tests/test_oracle_guard.py` | SQL-guard tests; no database or driver required. |
| `stack.env.example` | Every environment variable, documented, no secrets. |

## Quick start

```sh
cp stack.env.example stack.env
chmod 600 stack.env
$EDITOR stack.env          # set DISCORD_WEBHOOK_URL, OPENCODE_MODEL, ORACLE_*

docker compose build
docker compose up -d
```

`stack.env` is gitignored and excluded by `.dockerignore`. It is the only place secrets enter the container. Never put a real webhook or API key in the image, `compose.yaml`, or this repository.

`docker compose config` validates on a fresh checkout because `stack.env` is declared optional. If it is missing, delivery fails loudly rather than silently doing nothing.

## Deploy

Production runs the image published from GitHub, not a local build. A push to `main` runs `.github/workflows/publish-image.yml`, which builds `linux/amd64`, pushes `ghcr.io/tekkifox/notify-agent` to GitHub Container Registry tagged `latest` plus a short commit-SHA tag, and then calls the Portainer stack webhook. `workflow_dispatch` runs the same job by hand.

- The Portainer stack's compose must reference `ghcr.io/tekkifox/notify-agent:latest`, for example `image: ghcr.io/tekkifox/notify-agent:latest`.
- When the webhook fires, Portainer pulls the new image and recreates the `agent` container.
- `PORTAINER_WEBHOOK_URL` is a repository secret. Copy it from **Stacks → notify-agent → Webhooks** in Portainer. If the secret is empty, the workflow still succeeds and prints a warning: the image was published, but no redeploy was triggered.
- Recreating the container keeps the named `opencode-data` volume, so the OpenCode login survives a redeploy. Only `docker compose down -v` removes it.

## One-time authentication

OpenCode 2 uses a device-code login. It prints a code to approve in a browser on any machine; nothing is typed back into the terminal.

```sh
docker compose run --rm agent opencode auth login "OpenCode Console" --method device
```

Use the integration name that matches the provider in `OPENCODE_MODEL` (the example is OpenCode's own integration). Approve the code in the browser. The credential is written to the `opencode-data` volume at `/home/agent/.local/share/opencode`, so it survives container recreation and redeploys. It does not survive `docker compose down -v`.

An API key remains available as a second method:

```sh
docker compose run --rm agent opencode auth login "OpenCode Console" --method key
```

`opencode auth login --help` lists the `--method` flag, and an unknown method reports the valid choices: `device, key`.

The compose entrypoint dispatcher exists so these commands work without an `--entrypoint` override: with arguments it execs the command, with no arguments it execs `supercronic /app/crontab`.

### Upgrading from OpenCode 1.x

The image moved from the pinned `1.19.5` CLI to `2.2.1`. OpenCode 2 still reads the V1 configuration shape used by `opencode.json`, so the `notify` agent and its permission allowlist are unchanged. Four details differ:

- OpenCode 2 installs from `https://opencode.ai/v2/install`; the old installer refuses 2.x releases. `opencode --version` now prints `opencode v2.2.1`.
- `opencode run --pure` was removed. `scripts/run-prompt.sh` now passes `--standalone`, which runs a private server for that one invocation instead of attaching to the shared background service. No plugins are installed in the image, so the run is still reproducible.
- OpenCode 2 also discovers project configuration from the working directory. The image sets `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so the pinned `/app/opencode.json` is the only config source and its rules are not layered twice.
- `opencode debug agent` became `opencode debug agents`; the agent definition is inspectable with `opencode debug agents`.

## Oracle host statistics (MCP)

The `notify` agent can fetch real host statistics instead of inventing them. It
calls a local MCP server, `mcp/oracle_query.py`, which exposes one tool:
`query(sql)`. The server is Python 3 with `python-oracledb` in **thin mode**, so
the image contains no Oracle client libraries and no JDK.

The database side is read-only. The Oracle user has `SELECT` only on:

- `ROSSMONEY."host-samples"` — one JSON document per sample in a `DATA` column,
  roughly 937 rows and growing every three minutes. Fields: `hostLabel`,
  `sampledAt`, `cpuAvg`, `memoryPercent`, `diskPercent`, `loadAvg`,
  `processCount`, `networkRxMB`, `networkTxMB`, `swapPercent`.
- `ROSSMONEY."archview-sources"` — host/source metadata, 3 rows.

A query against `ROSSMONEY.pages` fails with an Oracle privilege error. That
boundary is intentional and is not configurable from the agent. The MCP server
adds its own guard: one statement only, `SELECT` or `WITH` only, no DML/DDL,
PL/SQL, `EXEC` or second statement; `FETCH FIRST 101 ROWS ONLY` is appended when
a query has no limit; results are hard-capped at 100 rows and 128 KiB; each
call has a 15-second timeout. The session also runs `SET TRANSACTION READ ONLY`
before the query, so a guard bypass still cannot write.

Only the `notify` agent may use the tool. `opencode.json` puts a global
`permissions` deny on `oracle_query`; the `notify` agent appends an allow after
it. Every other agent therefore resolves to deny, while the notify agent's
resolved list ends with `oracle_query: allow`.

### Configuration

Add these to `stack.env` for a plain compose run, or as stack environment
variables in Portainer:

| Variable | Purpose |
| --- | --- |
| `ORACLE_USER` | The read-only database user. |
| `ORACLE_PASSWORD` | That user's password. |
| `ORACLE_DSN` | Easy Connect string or TNS alias from the wallet, for example `mydb_high`. |
| `ORACLE_WALLET_DIR` | In-container wallet directory. `compose.yaml` sets `/wallet`. |

`opencode.json` passes all four through to the MCP process with `{env:...}`
substitution, so they are never written into the config file. No real values
belong anywhere in this repository.

### Wallet: mount it, never bake it

The image is public; the wallet is not. Keep the wallet on the host, outside the
repository, readable by the container's uid `10001`, and mount it read-only at
`/wallet`:

```sh
sudo install -d -o 10001 -g 10001 -m 0750 /opt/notify-agent/oracle-wallet
sudo cp tnsnames.ora ewallet.pem sqlnet.ora /opt/notify-agent/oracle-wallet/
sudo chown -R 10001:10001 /opt/notify-agent/oracle-wallet
sudo chmod 0400 /opt/notify-agent/oracle-wallet/*
```

The container needs exactly three files:

| File | Purpose |
| --- | --- |
| `tnsnames.ora` | Maps `ORACLE_DSN`, for example `mydb_high`, to the database service. |
| `ewallet.pem` | The thin-mode TLS wallet. This is the wallet file python-oracledb uses. |
| `sqlnet.ora` | Oracle Net settings supplied with the wallet. |

Mount the directory read-only with `:ro`. The container never writes to it.

The Java-format files from the same wallet are **not** needed and should not be
copied: `cwallet.sso`, `ewallet.p12`, `keystore.jks`, `truststore.jks`, and
`ojdbc.properties`. They are for thick-mode or JDBC clients, not
python-oracledb thin mode. Leaving `cwallet.sso` or `ewallet.p12` next to
`ewallet.pem` can make the driver prompt for a PEM pass phrase, which hangs a
cron container with no terminal. If they are present, the MCP server stages only
the three thin-mode files for the connection so `ewallet.pem` is preferred, but
the simplest and safest fix is not to copy them. There is deliberately no
wallet-password variable: the PEM wallet used here needs no passphrase, and a
password-protected wallet would put another secret in the container
environment.

`compose.yaml` binds `${ORACLE_WALLET_HOST_DIR:-./oracle-wallet}` to
`/wallet` with `:ro`. For a plain compose run, set the host path in `.env`
(compose interpolation, **not** `stack.env`):

```sh
ORACLE_WALLET_HOST_DIR=/opt/notify-agent/oracle-wallet
```

For the Portainer stack, open **Stacks → notify-agent → Editor** and either add
a stack environment variable `ORACLE_WALLET_HOST_DIR` with the absolute host
path, or replace `${ORACLE_WALLET_HOST_DIR:-./oracle-wallet}` with the absolute
path directly. Keep the `:/wallet:ro` suffix, and keep
`ORACLE_WALLET_DIR=/wallet` in the stack environment. Portainer substitutes
stack environment variables while rendering the compose file; `stack.env` only
supplies container environment variables and cannot choose the bind-mount
source.

`docker compose config` still validates on a fresh checkout because the mount
falls back to `./oracle-wallet`. That default directory is gitignored. The
container refuses to query when `ORACLE_WALLET_DIR` is unset or the directory
is not mounted.

### Tool guard tests

The guard is unit-tested without a database or the Oracle driver:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

The tests assert that a `SELECT` and a `WITH` query pass, and that `INSERT`,
`UPDATE`, `DELETE`, `DROP`, `BEGIN`, `WITH ... DELETE`, `FOR UPDATE`, two
statements, trailing junk and an oversized statement are rejected. They also
assert that the row cap is appended exactly once when a query has no limit and
that an existing `FETCH FIRST` or `ROWNUM` is respected.

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

The crontab ships with **no active schedule**. A fresh container runs supercronic and waits, but nothing is posted until an operator activates a line. The file contains commented examples to copy: a daily digest at 09:00, a weekday end-of-day wrap-up, a weekly planning reminder, a monthly maintenance note, and an hourly heartbeat.

Uncomment exactly one example (or write your own line in the same shape), then rebuild because the file is baked read-only into the image:

```sh
$EDITOR crontab
docker compose up -d --build
```

Format: one line per schedule, five cron fields, then the prompt passed to `run-prompt.sh`:

```cron
0 9 * * * /app/scripts/run-prompt.sh "Compose and send the 09:00 digest."
```

The fields are minute, hour, day-of-month, month, day-of-week. Times use `TZ` from `stack.env` (default `UTC`). The block above shows the shape only; it is not active until you put it in `crontab` yourself.

A schedule can also name its destination with `-t`:

```cron
0 9 * * * /app/scripts/run-prompt.sh -t general "Compose and send the 09:00 digest."
```

The name must exist in `NOTIFY_TARGETS` (see [Named targets](#named-targets)). `run-prompt.sh` puts that name into the instruction, the agent must use exactly that `-t` value, and `notify.sh` resolves it against the operator's allowlist. The model never chooses the destination: it can only repeat the name it was given, and an unknown name fails before anything is sent. With no `-t`, the schedule uses `NOTIFY_DEFAULT_TARGET` when set, and otherwise keeps the legacy `COMPOSIO_ARGS` shape.

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
- Accepts `-t NAME` / `--target NAME` to select a named Composio target.
- Uses Discord when `DISCORD_WEBHOOK_URL` is set.
- Otherwise uses Composio when both `COMPOSIO_API_KEY` and `COMPOSIO_TOOL` are set.
- Otherwise exits `1` with a short reason.
- Truncates to 2,000 Unicode characters before sending, and says so on stderr.
- Sends `allowed_mentions: {parse: []}`, so `@everyone`, `@here`, and role pings are suppressed.
- Exits non-zero on network errors, rate limits, and non-2xx responses. It never prints the webhook URL, target ids, or API key.
- Does not follow redirects and requires `https://`.

### Composio path

The Composio path is behind `COMPOSIO_API_KEY` + `COMPOSIO_TOOL`, uses the
`/api/v3.1` execute endpoint, and can be enabled without rebuilding:

```sh
COMPOSIO_API_KEY=...
COMPOSIO_TOOL=DISCORDBOT_CREATE_MESSAGE
COMPOSIO_ARGS='{"channel_id":"123456789012345678"}'
COMPOSIO_CONNECTED_ACCOUNT_ID=...
COMPOSIO_USER_ID=...
COMPOSIO_MESSAGE_FIELD=content
```

It targets the Discord **Bot** toolkit, which posts to a channel normally. The
bot needs `DISCORDBOT_CREATE_MESSAGE` and the tool needs its own `channel_id`,
so `COMPOSIO_ARGS` supplies extra arguments as a JSON object. They are merged
into `arguments`, and the message field (`COMPOSIO_MESSAGE_FIELD`, default
`content`) wins if a key collides. Anything that is not a JSON object is
rejected before the request is sent, without echoing the value.

A Composio user id can hold more than one connection for the same toolkit (for
example two Discord bots, one per server). Executing by `user_id` alone is then
ambiguous and can fail with `403` for a channel on the other server, so set
`COMPOSIO_CONNECTED_ACCOUNT_ID` to name the connection explicitly. When it is
empty the key is omitted from the payload.

The payload sent is `{"user_id": ..., "connected_account_id": ...,
"arguments": {...}}`; `connected_account_id` is absent when unset. The webhook
path still takes precedence whenever `DISCORD_WEBHOOK_URL` is set.

### Named targets

There are real destinations on different Discord servers, behind different
bot connections. A schedule names one instead of carrying a channel id:

```cron
0 9 * * * /app/scripts/run-prompt.sh -t admin-chat "Compose and send the daily digest."
```

`NOTIFY_TARGETS` is the operator-defined allowlist. It is a JSON object whose
keys are target names and whose values need a `channel_id` and may pin a
`connected_account_id` for that bot connection:

```sh
NOTIFY_TARGETS='{"general":{"channel_id":"000000000000000000","connected_account_id":"00000000-0000-0000-0000-000000000000"},"admin-chat":{"channel_id":"111111111111111111","connected_account_id":"11111111-1111-1111-1111-111111111111"}}'
NOTIFY_DEFAULT_TARGET=general
```

Both names above are placeholders. Put real ids only in `stack.env`, which is
gitignored and mode `0600`.

Resolution works like this:

- `-t NAME` selects a target. When `-t` is omitted, `NOTIFY_DEFAULT_TARGET`
  applies. When neither is set, delivery keeps the legacy shape exactly:
  `COMPOSIO_ARGS` plus `COMPOSIO_CONNECTED_ACCOUNT_ID`, with no target lookup.
- The target's `channel_id` and `connected_account_id` are merged into the tool
  arguments and win over the same keys in `COMPOSIO_ARGS`; the target's
  connected account also wins over `COMPOSIO_CONNECTED_ACCOUNT_ID`.
- An unknown name fails non-zero before any request and lists the configured
  target names, never their ids.
- A malformed `NOTIFY_TARGETS` (not an object, or an entry with no `channel_id`)
  fails before any request without echoing the value.
- `-t` with `DISCORD_WEBHOOK_URL` set is a failure, not a silent fallback: a
  webhook has no targets. Remove the webhook to use named targets.

The model cannot choose a destination. `run-prompt.sh` puts the schedule's
target name into the instruction and the agent's prompt tells it to repeat that
name verbatim; the actual channel and connected account come only from
`NOTIFY_TARGETS`. Even a manipulated model cannot invent a destination that is
not in the operator's allowlist.

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
| Host mounts | named `opencode-data` volume plus the Oracle wallet bind-mounted read-only at `/wallet`; no host `/etc`, `/home` or Docker socket |
| Privileged mode | never used |
| Secrets | `env_file: stack.env` only, mode 0600 on the host, gitignored; wallet contents live on the host and are mounted `:ro` |
| Agent tools | deny-by-default; only `/app/scripts/notify.sh -m ...`, `/app/scripts/notify.sh -t ... -m ...` and the MCP tool `oracle_query` are allowed |
| Oracle access | read-only database user; MCP guard allows one `SELECT`/`WITH`, caps rows and response size, applies a 15-second timeout |
| OpenCode sharing | disabled; snapshots and autoupdate disabled |
| Logs | `json-file`, 10 MB × 3 |

The image installs OpenCode from the official v2 installer pinned to `2.2.1`, verifies supercronic by SHA-256, and pins the base image by digest. OpenCode itself lives under `/opt/opencode`, so the runtime home directory can stay read-only.

## Threat model

This is a small, single-tenant agent, not a sandbox for untrusted users.

**Assets inside the container**

- The Discord webhook secret, which can post to one channel.
- The provider OAuth token or API key in the `opencode-data` volume.
- The Oracle password and wallet material.
- The ability to send messages as the bot/user the credentials represent.
- Message content and model prompts, which leave the host for the model provider.

**What the agent can do**

- Reach the internet outbound.
- Read its environment (including the webhook, provider credentials, and Oracle password).
- Call the shell only through the two allowed forms `/app/scripts/notify.sh -m ...` and `/app/scripts/notify.sh -t ... -m ...`.
- Call `oracle_query` with a single read-only `SELECT`/`WITH` statement against the two host-statistics objects.
- Write only to the named volume and tmpfs.

**What it cannot do**

- Accept inbound connections (no published ports).
- Access the Docker socket, the host filesystem, `/etc`, `/home`, or other containers.
- Gain capabilities or new privileges.
- Browse the web, read repository files, spawn subagents, or run arbitrary shell commands: the `notify` agent denies every tool except the two `notify.sh` forms and `oracle_query`.
- Read the CMS content: the Oracle user has no privilege on it, and the MCP guard refuses anything that is not a single read-only query.

**Scenarios and residual risk**

- **Prompt injection.** The operator prompt is trusted, but text pasted into it may not be. A manipulated model can still send a misleading message and can echo content present in its prompt. It cannot read files, browse, or run arbitrary commands. Do not put secrets in schedule prompts. The same applies to every value returned by `oracle_query`: the prompt tells the model to treat those values as data, never as instructions.
- **Database access.** The Oracle user is read-only and limited to two host-statistics objects. A compromised model can read those statistics but not the CMS content, and cannot write. The wallet is mounted read-only, so the process cannot modify it.
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
- The image builds with `--platform linux/amd64` (the target host architecture).
- `docker run --rm --platform linux/amd64 --entrypoint sh <image> -c 'id; supercronic -version'` prints:
  `uid=10001(agent) gid=10001(agent) groups=10001(agent)` and `v0.2.51`.
- The same image reports `opencode v2.2.1`.
- `opencode auth login --help` in the image lists `--method`, and
  `opencode auth login "OpenCode Console" --method __bogus__` reports
  `Authentication method not found: __bogus__. Available: device, key`.
- Running the compose service shows a read-only root filesystem, `CapEff: 0`, `NoNewPrivs: 1`, memory limit `536870912`, pids limit `200`, one writable named volume, and no published ports.
- A hardened `docker run` with `--read-only`, the same tmpfs mounts, and the data volume resolves the model and reaches model selection under `opencode run --standalone`, so the cron path does not need a writable root filesystem.
- `shellcheck` (container image `koalaman/shellcheck:stable`) and `sh -n` pass on every shell script, including `tests/notify-targets.sh`.
- A local TLS stub received a Discord-shaped POST with exactly 2,000 characters and `allowed_mentions: {"parse":[]}`; a second stub received the Composio path and payload, and the API key did not appear in output.
- The Composio stub asserted the `/api/v3.1` execute path, `COMPOSIO_ARGS` merged into `arguments` with the message field winning a collision, `connected_account_id` present only when set, and invalid `COMPOSIO_ARGS` rejected without echoing its contents.
- `opencode debug agents` resolves `notify` as a primary agent; its effective permission list denies everything, allows `oracle_query` last (over the global deny), and allows only `shell` for `/app/scripts/notify.sh -m *` and `/app/scripts/notify.sh -t * -m *`. The `build` agent resolves `oracle_query` to `deny`.
- The SQL-guard suite in `tests/test_oracle_guard.py` passes without a database or the Oracle driver: 36 tests covering allowed `SELECT`/`WITH`, rejected `INSERT`/`UPDATE`/`DELETE`/`DROP`/`BEGIN`/`WITH ... DELETE`/`FOR UPDATE`/multiple statements/trailing junk/oversized SQL, the row cap, the response-size cap, PEM-only and `cwallet.sso` wallet acceptance, empty and `tnsnames.ora`-only wallet rejection, Java wallet files not being passed to the thin driver, and (with a fake driver) the hard cap, column names and `SET TRANSACTION READ ONLY`.
- The MCP stdio handshake in the image returns one tool, `query`, and its guard errors are tool errors rather than protocol failures. A tool call with no configuration returns `Oracle connection is not configured` and never echoes a user, DSN, wallet path, or password.
- OpenCode's own debug log reports `mcp connected server=oracle tools=1` when the server is configured under `mcp.servers`, confirming the V2 shape and `codemode: false` expose the tool directly.
- Image size, measured with `docker save` and `du -sh /` inside each image: before `205 MB` compressed / `502 MB` root filesystem; after `232 MB` compressed / `593 MB` root filesystem. The increase is Python 3, pip, `python-oracledb` and its wheels. The 512 MB `mem_limit` is memory, not image size; the MCP server is only started when a session calls it.
- The image still runs `opencode debug agents` as uid `10001` with `--read-only` and the compose tmpfs/volume layout, and a write to `/` fails with `Read-only file system`.
- The target stub tests in `tests/notify-targets.sh` pass: a target resolves into the Composio payload, unknown and malformed targets fail without requests, `-t` is rejected with a webhook, and the legacy and default-target paths both work.

Not verified, because this repository contains no credentials:

- `opencode auth login "OpenCode Console" --method device` (no provider account was used; the probe above only confirms the `device` and `key` methods exist).
- A live Discord send (no real webhook was used).
- A live Composio send (no real API key was used; the Composio path was only exercised against a local stub).
- A live Oracle query (no credentials or wallet were used; the first mate runs the live query). The guard, protocol, and failure paths were exercised without a database.

Do those checks on the target host after copying `stack.env`, mounting the wallet, and completing the device flow.

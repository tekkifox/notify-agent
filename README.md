# notify-agent

A hardened, single-container notification agent. It runs the [OpenCode](https://opencode.ai) CLI on a schedule, turns a plain-English instruction into a message, and sends that message to Discord. Delivery itself is deterministic and does not involve the LLM.

Designed for an unattended small VPS: Ubuntu 24.04, x86_64, 2 vCPU, 1.9 GB RAM, Docker 29 with Compose 2.40. Everything except Docker lives inside the container.

- **Today:** Discord incoming webhook.
- **Later:** Composio, switchable through environment variables without rebuilding.
- **Schedules:** [supercronic](https://github.com/aptible/supercronic), a static non-root cron, reading a crontab bind-mounted from the host.
- **State:** one named volume for OpenCode auth and sessions.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Dockerfile` | Pinned OpenCode `2.2.1`, supercronic `0.2.51`, non-root uid `10001`. |
| `.github/workflows/publish-image.yml` | Builds and publishes `ghcr.io/tekkifox/notify-agent` on `main`, then triggers the Portainer redeploy webhook. |
| `compose.yaml` | One `agent` service with the hardened runtime posture, including the read-only schedule-file mount. |
| `crontab` | Default supercronic schedules, all commented out. Also the default host source for the mount; one line per schedule. |
| `scripts/notify.sh` | Deterministic delivery. No LLM. |
| `scripts/run-prompt.sh` | Runs `opencode run` with the locked-down `notify` agent. |
| `scripts/host-stats.py` | Scheduled-path prefetch: fetches one host's window before the model runs. |
| `scripts/entrypoint.sh` | Compose dispatcher: no args runs supercronic, args run the command. |
| `opencode.json` | `notify` agent definition: only `notify.sh` and the read-only Oracle MCP tools may be used. |
| `prompts/notify.md` | System prompt for the `notify` agent. |
| `mcp/oracle_query.py` | Single-file read-only Oracle MCP server (Python 3, `oracledb` thin mode) exposing `query` and `search_content`. |
| `tests/test_oracle_guard.py` | SQL-guard and input-handling tests; no database or driver required. |
| `tests/test_host_stats.py` | Prefetch tests: success, empty window, sanitised failure; no database or driver required. |
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

Schedules default to `./crontab`, so a fresh checkout starts as-is. To keep the schedule file somewhere else on the host, set `SCHEDULES_FILE` in `.env` beside `compose.yaml` or in the shell; see [Add a schedule](#add-a-schedule).

## Deploy

Production runs the image published from GitHub, not a local build. A push to `main` runs `.github/workflows/publish-image.yml`, which builds `linux/amd64`, pushes `ghcr.io/tekkifox/notify-agent` to GitHub Container Registry tagged `latest` plus a short commit-SHA tag, and then calls the Portainer stack webhook. `workflow_dispatch` runs the same job by hand.

- The Portainer stack's compose must reference `ghcr.io/tekkifox/notify-agent:latest`, for example `image: ghcr.io/tekkifox/notify-agent:latest`.
- When the webhook fires, Portainer pulls the new image and recreates the `agent` container.
- `PORTAINER_WEBHOOK_URL` is a repository secret. Copy it from **Stacks → notify-agent → Webhooks** in Portainer. If the secret is empty, the workflow still succeeds and prints a warning: the image was published, but no redeploy was triggered.
- Recreating the container keeps the named `opencode-data` volume, so the OpenCode login survives a redeploy. Only `docker compose down -v` removes it.
- The stack directory owns the schedule file, and the stack's compose sets `SCHEDULES_FILE` to it (as an interpolation variable, not in `stack.env`). A redeploy re-reads whatever the host file contains, so schedule changes do not need an image build or a push.

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

The compose entrypoint dispatcher exists so these commands work without an `--entrypoint` override: with arguments it execs the command, with no arguments it checks that `/app/crontab` is a readable regular file and then execs `supercronic /app/crontab`.

### Upgrading from OpenCode 1.x

The image moved from the pinned `1.19.5` CLI to `2.2.1`. OpenCode 2 still reads the V1 configuration shape used by `opencode.json`, so the `notify` agent and its permission allowlist are unchanged. Four details differ:

- OpenCode 2 installs from `https://opencode.ai/v2/install`; the old installer refuses 2.x releases. `opencode --version` now prints `opencode v2.2.1`.
- `opencode run --pure` was removed. `scripts/run-prompt.sh` now passes `--standalone`, which runs a private server for that one invocation instead of attaching to the shared background service. No plugins are installed in the image, so the run is still reproducible.
- OpenCode 2 also discovers project configuration from the working directory. The image sets `OPENCODE_DISABLE_PROJECT_CONFIG=1`, so the pinned `/app/opencode.json` is the only config source and its rules are not layered twice.
- `opencode debug agent` became `opencode debug agents`; the agent definition is inspectable with `opencode debug agents`.

## Oracle access (MCP)

The `notify` agent can fetch real host statistics and search the captain's
published content instead of inventing either. It calls a local MCP server,
`mcp/oracle_query.py`, which exposes two tools:

- `query(sql)` — one read-only `SELECT`/`WITH` statement over host statistics.
- `search_content(query, limit)` — semantic search over the published-content
  index (pages, projects and experience).

The server is Python 3 with `python-oracledb` in **thin mode**, so the image
contains no Oracle client libraries and no JDK. Both tools are read-only, and
the database user itself only has `SELECT` and one `EXECUTE`.

The database side is read-only. For host statistics, query the view
`NOTIFY_RO.HOST_STATS_V`. It has one row per sample, roughly every three minutes:

| Column | Meaning |
| --- | --- |
| `host_label` | Host name, for example `photos.rossmoney.me`. |
| `sampled_at` | The time column (`TIMESTAMP WITH TIME ZONE`). |
| `cpu_percent` | CPU usage percentage. |
| `memory_percent` | Memory usage percentage. |
| `disk_percent` | Disk usage percentage. |
| `load_avg` | Load average. |
| `process_count` | Number of processes. |
| `swap_percent` | Swap usage percentage; may be null. |
| `network_rx_mb` | Network received, in MB. |
| `network_tx_mb` | Network transmitted, in MB. |

Use aggregate functions for anything beyond a single reading. For example, peak
disk and CPU per host over the last hour:

```sql
SELECT host_label,
       MAX(disk_percent) AS max_disk_percent,
       MAX(cpu_percent) AS max_cpu_percent,
       COUNT(*) AS samples
FROM NOTIFY_RO.HOST_STATS_V
WHERE sampled_at >= SYSTIMESTAMP - INTERVAL '1' HOUR
GROUP BY host_label
ORDER BY max_disk_percent DESC;
```

The raw table `ROSSMONEY."host-samples"` stores each sample as a JSON document
in a `DATA` column, so its rows are not the statistics shape. Do not query it
for statistics; the view is the shape to query. The Oracle user has:

- `SELECT` on `NOTIFY_RO.HOST_STATS_V` — one row per sample, roughly every
  three minutes.
- `SELECT` on `ROSSMONEY."host-samples"` — the backing table; one JSON document
  per sample in a `DATA` column, roughly 937 rows and growing every three
  minutes.
- `SELECT` on `ROSSMONEY."archview-sources"` — host/source metadata, 3 rows.
- `SELECT` on `ROSSMONEY.CONTENT_EMBEDDINGS` — the semantic index of published
  content.
- `EXECUTE` on `ADMIN.EMBED_TEXT` — used by `search_content` to embed the query
  inside SQL.

A query against `ROSSMONEY.pages` or any other CMS table still fails with an
Oracle privilege error. That boundary is intentional: published content is
reachable only through the pre-built index and the `search_content` tool, not
through arbitrary SQL. The MCP server adds its own guard to `query`: one
statement only, `SELECT` or `WITH` only, no DML/DDL, PL/SQL, `EXEC` or second
statement; `FETCH FIRST 101 ROWS ONLY` is appended when a query has no limit;
results are hard-capped at 100 rows and 128 KiB; each call has a 15-second
timeout. The session also runs `SET TRANSACTION READ ONLY` before the query, so
a guard bypass still cannot write.

Only the `notify` agent may use either tool. `opencode.json` puts a global
`permissions` deny on `oracle_query` and `oracle_search_content`; the `notify`
agent appends an allow after each. Every other agent therefore resolves both to
deny, while the notify agent's resolved list ends with `oracle_query: allow`
and `oracle_search_content: allow`. OpenCode names an MCP tool
`<server>_<tool>`, so the server's `search_content` tool is reached as
`oracle_search_content`.

### Scheduled path: prefetch, not MCP

OpenCode registers MCP tools after the first model turn. A scheduled job can
therefore start before `oracle_query` exists, and a model that cannot see the
tool reports "statistics query tool unavailable" even though the database is
fine. The scheduled path removes that ordering dependency: the wrapper fetches
the numbers **before** `opencode` runs and puts them in the instruction. The
model only composes the message.

Ad-hoc jobs keep the MCP tools. The admin-chat job genuinely needs arbitrary
SQL, and an operator asking a one-off question is already in a session where
the tools have been registered.

`scripts/host-stats.py` is the scheduled-path fetcher:

```sh
python3 /app/scripts/host-stats.py --host vortexservers.co.uk --minutes 70
```

It prints a compact, prompt-ready block of `key=value` lines on stdout, and
nothing else. A successful window looks like this:

```text
result=ok
host=vortexservers.co.uk
requested_minutes=70
samples=23
first_sampled_at=2026-11-01T10:01:00+00:00
last_sampled_at=2026-11-01T11:08:00+00:00
max_cpu_percent=91.25
max_cpu_at=2026-11-01T10:44:00+00:00
max_memory_percent=63.5
max_memory_at=2026-11-01T10:44:00+00:00
max_disk_percent=44
max_disk_at=2026-11-01T11:08:00+00:00
```

`first_sampled_at` and `last_sampled_at` are the window actually covered, not
the window requested; that is how the model can say "over the last 67 minutes"
without inventing it. `requested_minutes` is what was asked for. Peak
timestamps come from the same aggregate (`MAX ... KEEP (DENSE_RANK FIRST ...)`),
so the whole block is one query and one connection.

The three outcomes are deliberately distinct:

| Outcome | Output | Exit |
| --- | --- | --- |
| Samples found | `result=ok` block on stdout | `0` |
| Query succeeded, no samples in the window | `result=empty`, `samples=0` on stdout | `0` |
| Query failed | nothing on stdout; one reason on stderr | `1` |

An empty window is **not** a failure. It gets exit `0` and a clearly marked
block, so the wrapper can tell "no data for this host" apart from "could not
ask". A failure prints only a short reason on stderr, for example
`host-stats: oracle error ORA-00942` or `host-stats: oracle query failed`.
When an Oracle/DPY code is present only the code survives; driver text,
`ORACLE_DSN`, `ORACLE_USER`, `ORACLE_PASSWORD` and `ORACLE_WALLET_DIR` never
reach stdout or stderr. Configuration errors use the same constant messages the
MCP server already returns.

The fetcher does not invent a second connection path. It loads
`mcp/oracle_query.py` and uses that module's `_config` and `_connection`
helpers, so it inherits the thin-mode wallet staging (PEM only, so no
passphrase prompt), stdin detachment, `SET TRANSACTION READ ONLY`, connect and
call timeouts, and connection cleanup. It adds one fixed aggregate over
`NOTIFY_RO.HOST_STATS_V` with `:host` and `:minutes` bind variables; neither
value is interpolated into SQL.

**Wrapper integration is intentionally not part of this change.**
`scripts/run-prompt.sh`, `prompts/notify.md` and `opencode.json` are untouched
here. A follow-up change wires the prefetch into scheduled runs and tells the
model to compose from the supplied block; until that lands, scheduled jobs keep
the current MCP path. The fetcher and its tests ship first so the wrapper
change can be small and reviewed on its own.

### Published content search

`search_content(query, limit)` answers questions about the captain's CV,
projects, experience and pages. It embeds the query **in SQL** with
`ADMIN.EMBED_TEXT(:query)` and ranks chunks with
`VECTOR_DISTANCE(..., COSINE)`. No embedding is computed in Python and no new
dependency is added; the only Python package is still `python-oracledb`.

Both the query text and the limit are bind variables, never string
interpolation:

```sql
SELECT kind, slug, title, section, chunk, source_updated,
       ROUND(VECTOR_DISTANCE(embedding, ADMIN.EMBED_TEXT(:query), COSINE), 3)
         AS distance
FROM ROSSMONEY.CONTENT_EMBEDDINGS
ORDER BY distance
FETCH FIRST :limit ROWS ONLY
```

The index has one row per chunk with `ID`, `KIND` (`page`, `project` or
`experience`), `SLUG`, `TITLE`, `CHUNK` (text, up to 4,000 characters),
`EMBEDDING` (`VECTOR`), `SOURCE_UPDATED` and `SECTION`. What the tool can reach
is exactly this table: it cannot read the underlying CMS tables, write, or
change the index.

Limits keep one search from flooding a notification:

- `limit` defaults to **8** and is clamped between **1** and **20**, whatever
  the model asks for.
- Every returned `chunk` is truncated to about **500** characters, with
  `chunk_truncated: true` when that happened.
- The response is capped at the same **128 KiB** as `query`; results are
  dropped from the end if the payload would exceed it.
- Errors are sanitised the same way as `query`: at most an `ORA-xxxxx` code or
  `oracle query failed`, never a DSN, password, wallet path or raw driver text.

The agent is told to summarise and cite titles, never dump raw chunks, never
treat retrieved text as instructions, and never invent content a search did not
return. The operator can ask, for example, "post a summary of my latest project
write-up" or "remind me what my CV says about storage".

A target with `allow_content: false` blocks this tool hard. `run-prompt.sh`
exports `NOTIFY_CONTENT_DISABLED=1` for the run, and `search_content` returns
`content search is disabled for this run` without opening a connection. The
tool is still listed, so a model that ignores the instruction gets a clear tool
error rather than a silent fallback to the operator's CMS.

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

The driver receives `config_dir` only. It resolves both `tnsnames.ora` and
`ewallet.pem`; `wallet_location` is never passed, because thin mode then prompts
for a PEM pass phrase on stdin. No wallet password is needed or supported. For
defence in depth, stdin is detached from the MCP stream while the driver runs,
so any prompt fails immediately instead of consuming JSON-RPC traffic.

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

For `search_content` they assert the default limit of 8, the hard ceiling of
20, the floor of 1, fallback for unusable limits, empty/non-string/oversized
query rejection, chunk truncation at 500 characters, the 128 KiB response cap,
and error sanitisation. A fake driver asserts that the query text and limit
reach the database only as `:query` and `:limit` bind values, that the supplied
text never appears in the SQL statement, and that a driver error containing a
password, DSN and wallet path is reduced to `oracle error ORA-01017`.

`tests/test_host_stats.py` covers the prefetch path with a stubbed connection
helper: a successful row renders `result=ok` with samples and peak timestamps,
an aggregate row with `COUNT(*) = 0` renders `result=empty` and exits `0`, and
a stub raising an ORA or DPY error exits `1` with only the code on stderr. It
also asserts that the host and window reach the database as `:host` and
`:minutes` binds, that the script loads `mcp/oracle_query.py` rather than a
copy, and that a failure containing a password, user, DSN and wallet path
leaks none of them.

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

Uncomment exactly one example (or write your own line in the same shape). The live file is the one on the host, bind-mounted read-only over `/app/crontab`; editing it needs a container restart, not an image rebuild and not a push through GitHub.

The image still ships `crontab` and compose uses it by default, so a fresh checkout works with `docker compose up` unchanged. To use an operator file, place it in the stack directory (for example `/srv/notify-agent/crontab`) and point `SCHEDULES_FILE` at it, either in `.env` beside `compose.yaml`:

```sh
SCHEDULES_FILE=/srv/notify-agent/crontab
```

or for one command:

```sh
SCHEDULES_FILE=/srv/notify-agent/crontab docker compose up -d
```

Relative values resolve against the directory containing `compose.yaml`, so
`SCHEDULES_FILE=crontab.local` keeps the file in the stack directory. Confirm
the resolved mount with `docker compose config`.

`SCHEDULES_FILE` is a Compose interpolation variable, not a container
environment variable. Setting it in `stack.env` does nothing: compose resolves
the mount before `env_file` is read. Keep it in the shell or `.env` (both are
gitignored).

The file must be readable by uid `10001`, the container's user; mode `0644`
is fine:

```sh
sudo install -m 0644 ./crontab /srv/notify-agent/crontab
```

Edit it, then restart the container and check the log:

```sh
$EDITOR /srv/notify-agent/crontab
docker compose restart agent
docker compose logs --tail=20 agent
```

Supercronic logs `read crontab: /app/crontab` on startup. A restart
re-reads the file and re-resolves the bind mount, so it also works when the
editor saves by replacing the file (vim, `sed -i`, `mv`) rather than editing
it in place.

Do **not** use `docker kill -s HUP`: supercronic `0.2.51` does not reload on
`SIGHUP`, it dies (exit 129), and only the restart policy brings the container
back. Restart the container to apply an edit.

If the host file is missing, the container refuses to start rather than
scheduling nothing. Docker 29 fails the bind mount itself, because it creates
a directory at the missing source and cannot mount that over the image's
`/app/crontab` file. On a Docker version that lets the mount through, an empty
directory appears at `/app/crontab`; the entrypoint detects it and exits with
a message naming the path and telling you to create the file. Either way,
check `docker compose logs agent` after changing `SCHEDULES_FILE`.

Format is unchanged: one line per schedule, five cron fields, then the prompt
passed to `run-prompt.sh`:

```cron
0 9 * * * /app/scripts/run-prompt.sh "Compose and send the 09:00 digest."
```

The fields are minute, hour, day-of-month, month, day-of-week. Times use `TZ` from `stack.env` (default `UTC`). The block above shows the shape only; it is not active until you put it in the schedule file yourself.

A schedule can also name its destination with `-t`:

```cron
0 9 * * * /app/scripts/run-prompt.sh -t general "Compose and send the 09:00 digest."
```

The name must exist in `NOTIFY_TARGETS` (see [Named targets](#named-targets)). `run-prompt.sh` puts that name into the instruction, the agent must use exactly that `-t` value, and `notify.sh` resolves it against the operator's allowlist. The model never chooses the destination: it can only repeat the name it was given, and an unknown name fails before anything is sent. With no `-t`, the schedule uses `NOTIFY_DEFAULT_TARGET` when set, and otherwise keeps the legacy `COMPOSIO_ARGS` shape.

Schedules are **trusted operator configuration**, not user input. The agent runs whatever command a schedule line names, with the container's full runtime access. Anyone who can edit the schedule file can make the agent post anything, to any configured target, at any time. Treat write access to it like write access to `stack.env`.

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
- Validates `NOTIFY_TARGETS` before any request, including an optional
  `host_scope`; malformed values fail by target name only, never by id.
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
keys are target names and whose values need a `channel_id`, may pin a
`connected_account_id` for that bot connection, and may declare the optional
`host_scope` and `allow_content` guards:

```sh
NOTIFY_TARGETS='{"general":{"channel_id":"000000000000000000","connected_account_id":"00000000-0000-0000-0000-000000000000"},"admin-chat":{"channel_id":"111111111111111111","connected_account_id":"11111111-1111-1111-1111-111111111111","host_scope":"vortexservers.co.uk","allow_content":false}}'
NOTIFY_DEFAULT_TARGET=general
```

Both names above are placeholders. Put real ids only in `stack.env`, which is
gitignored and mode `0600`.

`host_scope` is the host (or list of hosts) a message to that target may talk
about. `allow_content` is a boolean, default `true`; when it is `false` the
target may not use the content-search tool at all. `general` is unscoped and
may report on anything. `admin-chat` is scoped to `vortexservers.co.uk` with
`allow_content: false`, so it gets host data only from
`NOTIFY_RO.HOST_STATS_V` and nothing about the captain's site, CV, projects or
pages. An admin channel in the game-hosting server must not leak the operator's
other infrastructure or CMS content. A new channel on the same server gets the
same protection by giving its target the same guard values, without changing
code.

Resolution works like this:

- `-t NAME` selects a target. When `-t` is omitted, `NOTIFY_DEFAULT_TARGET`
  applies. When neither is set, delivery keeps the legacy shape exactly:
  `COMPOSIO_ARGS` plus `COMPOSIO_CONNECTED_ACCOUNT_ID`, with no target lookup.
- The target's `channel_id` and `connected_account_id` are merged into the tool
  arguments and win over the same keys in `COMPOSIO_ARGS`; the target's
  connected account also wins over `COMPOSIO_CONNECTED_ACCOUNT_ID`.
- An optional `host_scope` is a non-empty string or a non-empty array of
  non-empty strings. It names the only host(s) a message to that target may
  talk about; absent means no restriction. A malformed value fails before any
  request and names the offending targets, never their ids.
- An optional `allow_content` must be a JSON boolean. When a target sets it
  `false`, `run-prompt.sh` adds a plain-words line to the instruction: do not
  use the content-search tool, and do not include anything about the operator's
  site, CV, projects or pages. A malformed value fails before any request and
  names the offending targets, never their ids.
- `run-prompt.sh` resolves the effective target's `host_scope` and adds a
  plain-words line to the instruction: the delivery scope for the run, and that
  nothing about any other host may appear in the message. `prompts/notify.md`
  turns that into a hard rule: filter `host_label` to the scoped host, report
  only that host, never mention another host even in passing, and say plainly
  when the scoped host has no data instead of falling back to another host's
  numbers. With no scope, the instruction is byte-identical to before.
- `allow_content: false` is also a hard guard, not only an instruction:
  `run-prompt.sh` exports `NOTIFY_CONTENT_DISABLED=1` before it execs
  `opencode`, and `mcp/oracle_query.py` refuses every `search_content` call with
  `content search is disabled for this run` while that variable is non-empty.
  The MCP server is a child of the opencode process, so it inherits the value.
  A target without the flag, or with `true`, behaves exactly as before and the
  variable is not exported.
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
not in the operator's allowlist. The destination cannot choose the subject
either: a scoped target carries its host boundary into the instruction and the
agent's rules, and a target with `allow_content: false` also carries a
server-side block that the model cannot talk its way around.

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
| Host mounts | named `opencode-data` volume, the Oracle wallet bind-mounted read-only at `/wallet`, and the schedule file bind-mounted read-only at `/app/crontab` (`SCHEDULES_FILE`, default `./crontab`); no host `/etc`, `/home` or Docker socket |
| Privileged mode | never used |
| Secrets | `env_file: stack.env` only, mode 0600 on the host, gitignored; wallet contents live on the host and are mounted `:ro` |
| Agent tools | deny-by-default; only `/app/scripts/notify.sh -m ...`, `/app/scripts/notify.sh -t ... -m ...` and the MCP tools `oracle_query` and `oracle_search_content` are allowed |
| Oracle access | read-only database user; `query` allows one `SELECT`/`WITH` and `search_content` runs one fixed indexed query; both cap rows/chunks and response size, and apply a 15-second timeout |
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
- Call `oracle_query` with a single read-only `SELECT`/`WITH` statement against the host-statistics objects.
- Call `search_content` to read short chunks from the published-content index.
- Write only to the named volume and tmpfs.

**What it cannot do**

- Accept inbound connections (no published ports).
- Access the Docker socket, the host filesystem, `/etc`, `/home`, or other containers.
- Write the schedule file: it is bind-mounted read-only.
- Gain capabilities or new privileges.
- Browse the web, read repository files, spawn subagents, or run arbitrary shell commands: the `notify` agent denies every tool except the two `notify.sh` forms and the two Oracle tools.
- Read published content when the selected target sets `allow_content: false`: `run-prompt.sh` exports `NOTIFY_CONTENT_DISABLED=1` and the MCP server refuses `search_content` before it connects.
- Read arbitrary CMS tables or write to the content index. It can only run a fixed, read-only semantic search over `ROSSMONEY.CONTENT_EMBEDDINGS`; a `query` against `ROSSMONEY.pages` or any other CMS table fails with a privilege error.

**Scenarios and residual risk**

- **Prompt injection.** The operator prompt is trusted, but text pasted into it may not be. A manipulated model can still send a misleading message and can echo content present in its prompt. It cannot read files, browse, or run arbitrary commands. Do not put secrets in schedule prompts. The same applies to every value returned by `oracle_query` or `oracle_search_content`: the prompt tells the model to treat those values as data, never as instructions.
- **Database access.** The Oracle user is read-only and limited to host statistics plus the published-content index. A compromised model can read those and cannot write. It cannot read arbitrary CMS tables. The wallet is mounted read-only, so the process cannot modify it.
- **Content retrieval.** The index contains published material and short chunks of it leave the host for the model provider during a content question, exactly as host statistics do for a metrics question. Retrieved chunks are untrusted data and are truncated, but they are still readable by the provider.
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
- `docker compose config` resolves the schedule mount to `./crontab` by default and to the `SCHEDULES_FILE` path when that variable is set.
- With `/app/crontab` missing, or present as a directory (the shape a missing bind-mount source can produce), the entrypoint exits `1` before supercronic starts, printing `FATAL: schedule file /app/crontab is missing or unreadable` and naming `/app/crontab`.
- On Docker `29.8.0`, a `SCHEDULES_FILE` path that does not exist fails even earlier, at container creation: the daemon creates a host directory and `runc` refuses to mount it over the image's `/app/crontab` file. The container does not start, so there is no silent empty schedule either.
- With a mounted crontab whose schedules are commented out, supercronic logs `read crontab: /app/crontab`; after replacing the host file and restarting, it logs the same line and a newly added schedule runs.
- `SIGHUP` was tested against supercronic `0.2.51` in this image: sending it terminated the process (exit `129`) without re-reading the file. A container restart is the documented way to apply an edit.
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
- `opencode debug agents` resolves `notify` as a primary agent; its effective permission list denies everything, allows `oracle_query` and `oracle_search_content` last (over the global denies), and allows only `shell` for `/app/scripts/notify.sh -m *` and `/app/scripts/notify.sh -t * -m *`. The `build` agent resolves both Oracle tools to `deny`.
- The SQL-guard and input-handling suite in `tests/test_oracle_guard.py` passes without a database or the Oracle driver: 62 tests covering allowed `SELECT`/`WITH`, rejected `INSERT`/`UPDATE`/`DELETE`/`DROP`/`BEGIN`/`WITH ... DELETE`/`FOR UPDATE`/multiple statements/trailing junk/oversized SQL, the row cap, the response-size cap, PEM-only and `cwallet.sso` wallet acceptance, empty and `tnsnames.ora`-only wallet rejection, Java wallet files not being passed to the thin driver, and (with a fake driver) the hard cap, column names and `SET TRANSACTION READ ONLY`. They also assert that `oracledb.connect` receives `config_dir` with no `wallet_location` or `wallet_password`, that a wallet password in the environment cannot enable that prompt path, and that stdin is EOF for the driver while the JSON-RPC stream survives. They also cover the search limit default/ceiling/floor, query validation, chunk truncation at 500 characters, the search response cap, bind-only query text and limit, and sanitised errors, plus `NOTIFY_CONTENT_DISABLED=1` refusing `search_content` with a clear tool error and behaving unchanged when unset.
- `scripts/host-stats.py` compiles with `python3 -m py_compile`, and its 19 tests pass in the same discovery run as the 62 guard tests (81 total). With no Oracle configuration the script exits `1`, writes nothing to stdout, and prints `host-stats: Oracle connection is not configured`; with a deliberately broken wallet mount it prints only the constant wallet message. A stubbed thin-mode driver produced the `result=ok` block (exit `0`), the `result=empty` block (exit `0`), and an `ORA-01017` failure (exit `1`, stderr `host-stats: oracle error ORA-01017`). Grepping the failing stdout and stderr found none of the supplied user, password, DSN or wallet path.
- The MCP stdio handshake in the image returns two tools, `query` and `search_content`, and its guard/input errors are tool errors rather than protocol failures. A `search_content` call with no configuration returns `Oracle connection is not configured` and never echoes a user, DSN, wallet path, password, or the query text.
- OpenCode names an MCP tool `<server>_<tool>`, so the server's `query` and `search_content` tools resolve to `oracle_query` and `oracle_search_content`. `opencode debug agents` shows both names denied globally and allowed for `notify`, with `codemode: false` exposing them directly under the V2 `mcp.servers` shape.
- Image size on this branch, measured with `docker save | gzip -9` and `du -sh /` inside each image: `main` without content search is `242,637,972` bytes compressed / `593 MB` root filesystem; this branch is `242,638,242` bytes compressed / `593 MB` root filesystem. That is `+270` bytes compressed and `+2,245` bytes uncompressed: only the MCP Python file, prompt and OpenCode config changed, with no new package or dependency. The earlier Python 3, pip, `python-oracledb` and wheel cost (roughly `205 MB` to `232 MB` by the previous measurement) is unchanged. The 512 MB `mem_limit` is memory, not image size; the MCP server is only started when a session calls it.
- The image still runs `opencode debug agents` as uid `10001` with `--read-only` and the compose tmpfs/volume layout, and a write to `/` fails with `Read-only file system`.
- The target stub tests in `tests/notify-targets.sh` pass: a target resolves into the Composio payload, unknown and malformed targets fail without requests, malformed `host_scope` and `allow_content` values fail by target name only, `-t` is rejected with a webhook, and the legacy and default-target paths both work.
- The instruction tests in `tests/run-prompt-scope.sh` pass against a fake `opencode`: a scoped target adds the delivery-scope line and the content block, an array scope is listed, an unscoped target keeps the old instruction byte-for-byte, no target keeps the bare instruction, `allow_content: false` without a host scope adds only the content block, `NOTIFY_CONTENT_DISABLED=1` is exported only for a false value, and malformed `host_scope` or `allow_content` fails before the agent runs.
- The environment inheritance assumption was checked in the image built from this branch (`notify-agent:target-scope`), not just in the unit tests. A temporary local MCP server was spawned by `opencode run --standalone` with `NOTIFY_CONTENT_DISABLED=1` in the parent environment; the child's `/probe/env.txt` contained `NOTIFY_CONTENT_DISABLED=1` and `PYTHONUNBUFFERED=1`, so OpenCode merges the parent environment into the configured MCP environment. Running the installed `/app/mcp/oracle_query.py` over stdio in the same image returned `{"error":"content search is disabled for this run"}` with `isError: true` when the variable was set, and `Oracle connection is not configured` without it.

Not verified, because this repository contains no credentials:

- `opencode auth login "OpenCode Console" --method device` (no provider account was used; the probe above only confirms the `device` and `key` methods exist).
- A live Discord send (no real webhook was used).
- A live Composio send (no real API key was used; the Composio path was only exercised against a local stub).
- A live Oracle query, a live `search_content` call, or a live `scripts/host-stats.py` run against the real view (no credentials or wallet were used; the first mate runs the live checks). The guard, protocol, bind, cap, prefetch, and failure paths were exercised without a database.

Do those checks on the target host after copying `stack.env`, mounting the wallet, and completing the device flow.

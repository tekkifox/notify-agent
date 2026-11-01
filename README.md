# notify-agent

A hardened, single-container notification agent. It runs the [OpenCode](https://opencode.ai) CLI on a schedule, turns a plain-English instruction into a message, and sends that message to Discord. Delivery itself is deterministic and does not involve the LLM.

Designed for an unattended small VPS: Ubuntu 24.04, x86_64, 2 vCPU, 1.9 GB RAM, Docker 29 with Compose 2.40. Everything except Docker lives inside the container.

- **Delivery:** Discord incoming webhook, or Composio, switchable through environment variables without rebuilding.
- **Schedules:** [supercronic](https://github.com/aptible/supercronic), a static non-root cron, reading a crontab bind-mounted from the host.
- **Inbound replies:** an optional Python listener polls one or more Discord channels through Composio tool execution and runs the same agent for messages from an explicit per-source author allowlist.
- **State:** one named volume for OpenCode auth, sessions and the inbound baseline/dedupe file.

## Repository layout

| Path | Purpose |
| --- | --- |
| `Dockerfile` | Pinned OpenCode `2.2.1`, supercronic `0.2.51`, non-root uid `10001`. |
| `.github/workflows/publish-image.yml` | Builds and publishes `ghcr.io/tekkifox/notify-agent` on `main`, then triggers the Portainer redeploy webhook. |
| `compose.yaml` | One `agent` service with the hardened runtime posture, including the read-only schedule-file mount. |
| `crontab` | Default supercronic schedules, all commented out. Also the default host source for the mount; one line per schedule. |
| `scripts/notify.sh` | Deterministic delivery. No LLM. |
| `scripts/run-prompt.sh` | Runs `opencode run` with the locked-down `notify` agent and captures the debug transcript. |
| `scripts/discord_inbound.py` | Optional inbound listener: polls one or more Discord sources, authorises replies and runs the same agent once each. |
| `scripts/agent_capture.py` | Shared bounded debug transcript: header, agent output, send-aware footer; used by both run paths. |
| `scripts/entrypoint.sh` | Compose dispatcher and supervisor: without args runs supercronic and the listener independently, with args runs the command. |
| `opencode.json` | `notify` agent definition: only `notify.sh` and the read-only Oracle, SSH and BookStack MCP tools may be used. |
| `prompts/notify.md` | System prompt for the `notify` agent. |
| `mcp/oracle_query.py` | Single-file read-only Oracle MCP server (Python 3, `oracledb` thin mode) exposing `query` and `search_content`. |
| `mcp/ssh_probe.py` | Single-file read-only SSH MCP server (Python 3, `paramiko`) exposing `probe` as `ssh_probe`, with host-scope refusal before any connection. |
| `mcp/host_report.py` | Pure digest formatter: deterministic ordering, stale rule, thresholds, SSH lines and 2,000-character truncation. |
| `mcp/bookstack.py` | Single-file read-only BookStack MCP server (Python 3, standard library) exposing `search` and `read` as `bookstack_search` and `bookstack_read`, restricted to an allowlist of books. |
| `tests/test_oracle_guard.py` | SQL-guard and input-handling tests; no database or driver required. |
| `tests/test_ssh_probe.py` | SSH policy, scope, failure-class, timeout, selftest and protocol tests; no live connection. |
| `tests/test_bookstack.py` | BookStack allowlist, scope-gate, token-leak, configuration, timeout and protocol tests against a loopback stub; no live wiki. |
| `tests/test_host_report.py` | Digest formatting tests: ordering, the 20-minute stale boundary, every threshold, SSH lines and truncation. |
| `tests/test_discord_inbound.py` | Inbound transport, per-source allowlist, cursor/dedupe isolation, reply routing, transcript framing, unsent retry, debug-flag, length, queue and refusal-path tests; no network or model required. |
| `stack.env.example` | Every environment variable, documented, no secrets. |

## Quick start

```sh
cp stack.env.example stack.env
chmod 600 stack.env
$EDITOR stack.env          # set DISCORD_WEBHOOK_URL, OPENCODE_MODEL, ORACLE_*, SSH_*, BOOKSTACK_*

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

The preferred model is `OPENCODE_MODEL` (Beam Free); the optional `OPENCODE_MODEL_FALLBACK` (DeepSeek Flash 4.1) is used only when the preferred model cannot serve the request, such as Beam Free's "Free usage limit reached" or a model-not-found/unavailable error. The fallback fires at most once, never after a message was already sent, and never on an ordinary failure. A fallback run is visible in the transcript and the container summary shows the model that did the work.

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

`scripts/run-prompt.sh` runs an MCP readiness preflight before it starts
OpenCode: it sends `initialize` and `tools/list` to the same server with the
same environment, fails the run loudly if `query` and `search_content` do not
appear, and then runs `python3 /app/mcp/oracle_query.py --selftest` so an
unreachable database also fails the run before anything is sent. The handshake
does not touch the database, and the preflight captures server output rather
than printing it. `opencode.json` gives the server a 60-second startup timeout
so a slow first registration does not turn into a missing tool. The prompt then
requires the tool call before any message is composed, retries a registration
failure once after `/bin/sleep 5`, and writes a tool failure to stdout instead
of sending it to the notification channel.

The server is Python 3 with `python-oracledb` in **thin mode**, so the image
contains no Oracle client libraries and no JDK. Both tools are read-only, and
the database user itself only has `SELECT` and one `EXECUTE`.

The database side is read-only. For host statistics, query the view
`NOTIFY_RO.HOST_STATS_V`. It has one row per sample, roughly every three minutes:

| Column | Meaning |
| --- | --- |
| `host_label` | Host name, for example `host2.example.com`. |
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

A transient connection failure is retried once after a one-second pause. The
retry set is deliberately narrow: `ORA-03113`, `ORA-03114`, `ORA-03135`,
`ORA-12170`, `ORA-12537`, `ORA-12541`, `DPY-6005` and `DPY-4011`. SQL,
permission and audience errors (`ORA-009xx`, `ORA-01031`, `ORA-00942`) are
returned immediately. Every tool error is JSON with the message, the safe
`code` (or `null`) and a `retryable` boolean, so the agent can tell a database
blip from bad SQL from an empty result. `python3 /app/mcp/oracle_query.py
--selftest` opens a connection and runs `SELECT 1 FROM dual`; it exits 0 on
success or 1 with a short, secret-free reason. A closed stdout pipe ends the
process quietly instead of printing a traceback into the cron log.

Only the `notify` agent may use either tool. `opencode.json` puts a global
`permissions` deny on `oracle_query` and `oracle_search_content`; the `notify`
agent appends an allow after each. Every other agent therefore resolves both to
deny, while the notify agent's resolved list ends with `oracle_query: allow`
and `oracle_search_content: allow`. OpenCode names an MCP tool
`<server>_<tool>`, so the server's `search_content` tool is reached as
`oracle_search_content`.

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

## SSH health (MCP)

The `notify` agent can check SSH reachability and key authentication per host.
It calls a second local MCP server, `mcp/ssh_probe.py`. OpenCode names an MCP
tool `<server>_<tool>`, so the server's `probe` tool is reached as
`ssh_probe`.

The tool takes `{"hosts": ["a.example", "b.example"]}` or a single
`{"host": "a.example"}`. Every requested host gets one result:

- `status: "ok"` with `latency_ms` and `message: "connected"`;
- `status: "timeout"`, `"refused"`, `"unreachable"` (including DNS),
  `"auth_refused"` or `"error"`, with a short secret-free message;
- `status: "not_allowed"` or `"out_of_scope"` when policy refuses the host
  before any socket is opened;
- `status: "not_configured"` when SSH is not configured.

A failure for one host never fails the call or hides the others. Each host has
a timeout (`SSH_TIMEOUT_SECONDS`, default 5) and the whole call has a deadline
(`SSH_TOTAL_TIMEOUT_SECONDS`, default 20), so a list of dead hosts cannot stall
a scheduled run. Hosts that would start after the deadline are returned as
`timeout` with `attempted: false` and `not attempted: total time cap reached`.

### Two host boundaries, enforced in code

Before any connection is attempted, the tool applies two checks:

1. **Global allowlist.** The host must be covered by `SSH_ALLOWED_HOSTS`
   (default `host1.example.com,host2.example.com,host3.example.com`). An entry
   covers itself and its subdomains, so `example.com` covers
   `vortex1.example.com`, but not `notexample.com` or
   `example.com.example`.
2. **Run scope.** `run-prompt.sh` exports the selected target's `host_scope` as
   `NOTIFY_HOST_SCOPE`; the tool refuses a host outside it. The optional
   `scope` argument can narrow that list, never widen it. A run scoped to
   `admin-chat` therefore cannot even receive a rossmoney result, even if the
   model asks for one.

Both refusals name the host and return `attempted: false`; the connector is
never called. That is the defence-in-depth layer beneath the prompt.

`python3 /app/mcp/ssh_probe.py --selftest` reports whether SSH is configured and
which settings are present, without opening any connection. It exits 0 when SSH
is unconfigured (that is a valid state the digest reports plainly) and 1 only
for a malformed setting such as a non-numeric timeout. The output names no
user, key path or key material.

The image has no `openssh-client`. The probe uses **paramiko**, pinned with its
direct dependencies in the `Dockerfile`, so SSH stays in Python and no shell or
subprocess is involved.

### SSH environment

| Variable | Required | Meaning |
| --- | --- | --- |
| `SSH_USER` | yes | Unprivileged account used for the key-authenticated probe |
| `SSH_KEY_PATH` | yes | In-container path of the private key; compose sets `/ssh/id_ed25519` |
| `SSH_ALLOWED_HOSTS` | no | Comma-separated global allowlist; default `host1.example.com,host2.example.com,host3.example.com` |
| `SSH_TIMEOUT_SECONDS` | no | Per-host timeout, default 5; capped at 30 |
| `SSH_TOTAL_TIMEOUT_SECONDS` | no | Whole-call deadline, default 20; capped at 60 |
| `SSH_PORT` | no | SSH port for every host, default 22 |
| `SSH_KEY_HOST_FILE` | compose only | Host path bind-mounted read-only at `/ssh/id_ed25519`; set in `.env` or the shell, not `stack.env` |
| `HOST_REPORT_HOSTS` | no | Ordered known-host list for the digest; default `host1.example.com,host2.example.com,host3.example.com` |

Mount the key read-only, exactly like the Oracle wallet:

```yaml
volumes:
  - ${SSH_KEY_HOST_FILE:-./ssh/id_ed25519}:/ssh/id_ed25519:ro
```

Put the key outside the repository (for example
`/srv/notify-agent/ssh/id_ed25519`), make it readable by uid `10001`, and point
`SSH_KEY_HOST_FILE` at it. `ssh/` is gitignored. If the host file is missing,
Docker may create an empty directory at the mount point; the tool checks for a
readable regular file and reports `configured: false` rather than pretending a
check happened. Never commit the key or bake it into the image.

### The destination is the boundary

The rule is about where a host may be discussed, not which host may be probed:

- `admin-chat` is the Vortex server's channel. Its `host_scope` is
  `["host3.example.com"]`, so only Vortex hosts may appear there.
- `automations` is TekkiFox's Automations. Its `host_scope` includes
  `host1.example.com`, `host2.example.com` and `host3.example.com`.
- A rossmoney hostname, or a figure attributable to one, must never reach
  `admin-chat`.

`notify.sh` enforces `host_scope` at the send boundary, not only in the prompt
or the tool. For a scoped target it extracts host-like tokens (dotted names with
a letters-only suffix) from the message and accepts a token only when it equals
a scope entry or is a subdomain of one. Anything else makes the script exit
non-zero before any request; the delivery call never happens. The same report
aimed at `automations` succeeds. The check covers URLs and code spans, because
it scans the whole message.

What can evade it: bare host labels with no dot, IP addresses, spaced or
obfuscated names, Unicode lookalikes, and a figure that is not accompanied by a
hostname. It is an exact-name guard, which is the minimum the captain asked for;
the prompt is the layer that handles semantic attribution. The guard is also
deliberately stricter than "known hosts": a scoped message that mentions an
unrelated domain such as `github.com` is refused too, because sending nothing is
the safe failure. Keep the prompt's host set and the target's `host_scope` in
sync.

## Runbooks (MCP)

The `notify` agent can answer "how do I restart backrest?" or "what does a
load-peak flag mean?" from a BookStack wiki. It calls a third local MCP server,
`mcp/bookstack.py`, which exposes two read-only tools:

- `search(query, limit)` — search the allowed books; returns page name, book,
  slug and a short excerpt.
- `read(page)` — one page's text, by page id or slug.

OpenCode names an MCP tool `<server>_<tool>`, so the agent reaches these as
`bookstack_search` and `bookstack_read`.

### One instance per target, enforced in code

Runbooks are per target, and each target has its own BookStack instance. A
target entry may carry a `bookstack` object:

```json
"bookstack": {"url": "https://bookstack.example.com", "books": "runbooks"}
```

`url` must be `https`; `books` is a comma-separated list of book ids or slugs.
The target's `allow_runbooks` flag is the explicit grant, default **false**. A
target needs both the grant and a complete `bookstack` object; without either,
both tools refuse and the generated instruction does not mention runbooks.

In the example configuration, `automations` points at the operator's wiki with
the single book `runbooks`, and `admin-chat` points at the Vortex wiki with all
six of its books: `networking`, `operations-getting-started`, `organization`,
`bookstack`, `v-rising` and `scripts`. Each run exports only its own target's
URL and books, so an `admin-chat` run cannot reach the operator's instance and
an `automations` run cannot reach the Vortex one. That isolation is the point of
the change and is asserted in the tests.

A wiki also holds deployment inventory (real hostnames, Composio connections,
channel ids, the Auth0 wiring), so this is deliberately not a general wiki
client. The target's `books` list names the only books that may be read.

- Search is issued **per allowed book** (`/api/search/book/{id}`), so a page
  from another book is never fetched, let alone returned. Each result is
  checked against the allowlist again before it leaves the process, and a
  book outside the allowlist never contributes to a count.
- `read` checks the page's `book_id` against the allowlist before returning
  any text.
- A page in another book, a missing page and an ambiguous slug all return the
  same `page is not available` error. The tool therefore cannot be used to
  discover what else exists, and the error names neither the page nor its
  book.
- A slug in the target's `books` list is resolved to its id through
  `/api/books` on first use and cached for the life of the process.

The token is sent only as `Authorization: Token <id>:<secret>`; it is never a
URL parameter, never printed, never returned and never included in an error.
HTTP redirects are refused rather than followed, so a redirect cannot carry the
header to another host. Errors carry the HTTP status and a fixed short reason;
a response body is never echoed. Each request times out after
`BOOKSTACK_TIMEOUT_SECONDS` (default 10, capped at 60), so a slow wiki cannot
stall a scheduled run. A page is capped at 20,000 characters with
`truncated: true`; a search excerpt at 300 characters.

Page text is untrusted data. The tool descriptions and the prompt say so: it is
documentation to read, never an instruction to follow.

### Access gate: default-deny, per target

`allow_runbooks` is a per-target boolean, default **false**, and the target's
`bookstack` object supplies the instance. Adding a target does not grant
runbooks, and a target that permits CMS content still gets no runbooks unless
it has both the grant and an instance.

`run-prompt.sh` and `scripts/discord_inbound.py` export
`NOTIFY_RUNBOOKS_ALLOWED=1`, `NOTIFY_BOOKSTACK_URL` and
`NOTIFY_BOOKSTACK_BOOKS` for the run only when the effective target has
`allow_runbooks: true` and a complete `bookstack` object; every other run
clears all three variables. `mcp/bookstack.py` refuses both tools unless the
grant is exactly `1` and the URL and books are present, before it reads
configuration or opens a socket. It also refuses a URL that is absent or not
`https`. The refusal names neither the wiki nor the book.

The CMS content gate is separate. `allow_content: false` still exports
`NOTIFY_CONTENT_DISABLED=1` for `oracle_search_content`; it does not gate the
target's own runbooks. That is what lets `admin-chat` stay host-data-only for
the operator's CMS while reading its own Vortex wiki. The generated instruction
for that run says "host data and runbook procedures only", so the static prompt
does not treat it as a host-data-only run. A content-disabled target without a
runbook grant keeps the stricter "host data only" instruction and never
mentions runbooks.

The tools are still listed in a refused run, as `search_content` is, so a model
that ignores the instruction gets a clear tool error instead of a silent
fallback to another source.

### BookStack environment

The token and timeout are shared by the MCP server. The URL and books are
per run: `run-prompt.sh` and the inbound listener export them from the target's
`bookstack` object, and `opencode.json` passes them through with `{env:...}`
substitution, so no value is written into the config file.

| Variable | Required | Meaning |
| --- | --- | --- |
| `BOOKSTACK_TOKEN_ID` | yes | API token id. |
| `BOOKSTACK_TOKEN_SECRET` | yes | API token secret. |
| `BOOKSTACK_TIMEOUT_SECONDS` | no | Per-request timeout, default 10; capped at 60. |
| `NOTIFY_RUNBOOKS_ALLOWED` | per run | Exported as `1` only for a granted target; not an operator setting. |
| `NOTIFY_BOOKSTACK_URL` | per run | That target's `bookstack.url`. The MCP requires `https`. |
| `NOTIFY_BOOKSTACK_BOOKS` | per run | That target's `bookstack.books`; the per-book allowlist. |

An entry in `books` may be a slug or an id. If an entry does not exist on the
instance, that search is skipped rather than failing the whole run; a search
with no resolvable books returns an empty result and a read is refused.

A missing or malformed value fails the server at startup, names the variable
and never echoes the value. A run that was not granted runbooks validates
nothing, because both tools refuse anyway. `python3 /app/mcp/bookstack.py
--selftest` reports whether the configuration is present, without making a
request and without printing the URL or token.

Create the token for a **dedicated BookStack user** whose role can see only the
books in the target's `books` list — for example a role with "Access System
API" and content permission on those books alone. The allowlist is the second
line of defence; that user's role is the first. A token belonging to an
administrator would defeat both. The token is shared by the MCP server, so it
must be valid for the instance selected by the run; per-instance tokens are not
part of this change.

Only the `notify` agent may use either tool. `opencode.json` puts a global
`permissions` deny on `bookstack_search` and `bookstack_read`; the `notify`
agent appends an allow after each, exactly as it does for the Oracle and SSH
tools.

### Tests

`tests/test_bookstack.py` drives a stub BookStack on loopback; there is no live
wiki and no real token in the repository. It covers allowlist enforcement in
both tools, a forbidden and a missing page producing the same refusal, the
disabled-mode gate making no request, no token or response body in any output
on success or failure, redirect refusal, configuration errors that name the
variable without echoing the value, the startup exit path, the configured
timeout against a server that never answers, slug resolution and the MCP
protocol surface.

## Morning digest

The digest keeps the old host report's shape, plus SSH status folded into each
host line. A rendered example (produced by `mcp/host_report.py`):

```text
**Host report** — 2026-10-31 09:00 UTC
3/3 hosts reported, 0 stale, 1 flagged
- [!] host1.example.com: 53 samples, last-seen 4m ago; CPU 44.8% avg / 117.0% peak; mem 22.6% avg; load 0.44 avg / 1.17 peak; disk 30.9%; SSH ok (42 ms) (CPU peak 117.0% >= 90%)
- host2.example.com: 46 samples, last-seen 3m ago; CPU 22.8% avg / 28.0% peak; mem 30.6% avg; load 0.23 avg / 0.28 peak; disk 80.3%; SSH auth_refused (authentication refused)
- host3.example.com: 49 samples, last-seen 3m ago; CPU 17.9% avg / 25.2% peak; mem 75.7% avg; load 3.57 avg / 5.04 peak; disk 71.2%; SSH ok (61 ms)
```

Per host, the order is: sample count, last-seen age, CPU average and peak,
memory average, load average and peak, disk from the latest sample, then SSH.
Memory is average only and disk is latest only; load has both average and peak.
STALE means no sample in the last **20** minutes (a sample exactly 20 minutes old
is still fresh), and a host with no samples in the 24-hour window is stale too.
The inclusive thresholds are CPU peak 90, memory average 90, load peak 8 and
disk latest 90; a flagged line starts with `[!]` and ends with the inline reason,
for example `(CPU peak 117.0% >= 90%)`.

Known hosts keep the order of `HOST_REPORT_HOSTS`; other hosts found in the data
follow in name order. A known host with no samples still gets a line. When the
message would exceed 2,000 characters, whole host lines are dropped from the end
and the message ends with `… truncated: N host line(s) not shown`.

`mcp/host_report.py` is the pure reference formatter and is unit-tested. The
prompt asks the model to render the same shape from one aggregate query, then
append the SSH result per host. `run-prompt.sh` filters `HOST_REPORT_HOSTS` to
the selected target's `host_scope` and carries the ordered list into the
instruction, so a scoped run only sees its own hosts.

### Ready-to-paste schedule lines

The schedule is a host file (`SCHEDULES_FILE`, default `./crontab`), not
anything this change can make live. Add one of these lines to the host file,
then restart the container. The first is the full morning digest for the
automations channel; the second is a Vortex-only digest for `admin-chat`.

```cron
0 9 * * * /app/scripts/run-prompt.sh -t automations "Compose the morning digest. Call oracle_query first for the 24-hour host statistics, then ssh_probe for SSH status. Use the old host report shape: one line per host with sample count, last-seen age, CPU average and peak, memory average, load average and peak, and disk from the latest sample, plus SSH success with latency or the classified failure. Include every known host in this run, in order. Flag stale hosts (no sample in 20 minutes) and CPU peak >= 90, memory average >= 90, load peak >= 8, disk >= 90. Keep it under 2,000 characters; drop whole host lines from the end and say how many were not shown. Never mention a host outside this run's scope. If SSH status cannot be checked, omit the SSH part. Send it now with notify.sh."
```

```cron
0 9 * * * /app/scripts/run-prompt.sh -t admin-chat "Compose the morning Vortex digest. Call oracle_query first for the 24-hour host statistics, then ssh_probe for SSH status, for the Vortex hosts only. Use the old host report shape: one line per host with sample count, last-seen age, CPU average and peak, memory average, load average and peak, and disk from the latest sample, plus SSH success with latency or the classified failure. Include every known host in this run, in order. Flag stale hosts (no sample in 20 minutes) and the thresholds. Keep it under 2,000 characters; drop whole host lines from the end and say how many were not shown. Never mention a rossmoney host or anything about it. If SSH status cannot be checked, omit the SSH part. Send it now with notify.sh."
```

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

A morning digest that calls both MCP tools and keeps the old host-report shape,
aimed at the automations channel:

```cron
0 9 * * * /app/scripts/run-prompt.sh -t automations "Compose the morning digest. Call oracle_query first for the 24-hour host statistics, then ssh_probe for SSH status. Use the old host report shape: one line per host with sample count, last-seen age, CPU average and peak, memory average, load average and peak, and disk from the latest sample, plus SSH success with latency or the classified failure. Include every known host in this run, in order. Flag stale hosts (no sample in 20 minutes) and CPU peak >= 90, memory average >= 90, load peak >= 8, disk >= 90. Keep it under 2,000 characters; drop whole host lines from the end and say how many were not shown. Never mention a host outside this run's scope. If SSH status cannot be checked, omit the SSH part. Send it now with notify.sh."
```

This repository cannot make that schedule live: the schedule is the operator's
host file (`SCHEDULES_FILE`, default `./crontab`). Add the line to that file and
restart the container. See [Morning digest](#morning-digest) for the
Vortex-only `admin-chat` variant.

The name must exist in `NOTIFY_TARGETS` (see [Named targets](#named-targets)). `run-prompt.sh` puts that name into the instruction, the agent must use exactly that `-t` value, and `notify.sh` resolves it against the operator's allowlist. The model never chooses the destination: it can only repeat the name it was given, and an unknown name fails before anything is sent. With no `-t`, the schedule uses `NOTIFY_DEFAULT_TARGET` when set, and otherwise keeps the legacy `COMPOSIO_ARGS` shape.

Schedules are **trusted operator configuration**, not user input. The agent runs whatever command a schedule line names, with the container's full runtime access. Anyone who can edit the schedule file can make the agent post anything, to any configured target, at any time. Treat write access to it like write access to `stack.env`.

## Read logs

```sh
docker compose logs -f --tail=100 agent
```

Supercronic logs each job start and exit to the container log, and delivery results and short failure reasons also appear there. The container log also receives exactly one one-line summary per agent run:

```text
2026-11-01T14:12:09Z run=inbound:vortex-admin model=provider/model exit=0 31.0s sent=yes "please report on the host stats?"
```

The summary carries the UTC time, path (`inbound:<source>` or `schedule`), model, exit code, duration and send state, plus the request flattened to one line and capped at about 200 characters with an ellipsis. For inbound runs it quotes the operator's own message rather than the generated instruction. The whole line stays under 320 characters, even with a long source name or model. It contains no tool output, no provider detail and no transcript file paths, and it replaces the old per-run `agent finished` line rather than adding a duplicate. The agent's complete stdout and stderr go to the bounded `notify-debug.log` transcript instead (see [Agent output and the debug transcript](#agent-output-and-the-debug-transcript)). A failed host-statistics query is reported in that transcript only; it is never sent to the notification channel. The MCP readiness preflight fails the job before OpenCode starts and prints only the server path, never its output or environment.

Because the one-line summary includes a short excerpt of the operator's instruction, treat container logs as internal too. Neither the container log nor the transcript contains the webhook URL or API keys. Compose caps the container log at 3 × 10 MB.

A failed job exits non-zero and is visible in the log, but there is no automatic retry. If you need retries, add them at the schedule level (for example a second prompt) rather than hiding failures.

## Delivery behaviour

`scripts/notify.sh`:

- Reads the message from stdin or from `-m MESSAGE`.
- Accepts `-t NAME` / `--target NAME` to select a named Composio target.
- Validates `NOTIFY_TARGETS` before any request, including an optional
  `host_scope`; malformed values fail by target name only, never by id.
- Enforces `host_scope` at the send boundary: for a scoped target, a message
  that names a host outside the permitted list (or its subdomains) exits
  non-zero before any request. URLs and code spans are scanned; see
  [The destination is the boundary](#the-destination-is-the-boundary).
- Uses Discord when `DISCORD_WEBHOOK_URL` is set.
- Otherwise uses Composio when both `COMPOSIO_API_KEY` and `COMPOSIO_TOOL` are set.
- Otherwise exits `1` with a short reason.
- Truncates to 2,000 Unicode characters before sending, and says so on stderr.
- Honours optional `NOTIFY_SEND_MARKER`: when set, a confirmed 2xx send touches
  that path. The inbound listener uses it to detect a run that delivered
  nothing. Unset is a no-op, a failed send never writes it, and an unwritable
  marker only warns without changing the exit status.
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
`host_scope`, `allow_content`, `allow_runbooks` and `bookstack` guards:

```sh
NOTIFY_TARGETS='{"automations":{"channel_id":"000000000000000000","connected_account_id":"00000000-0000-0000-0000-000000000000","host_scope":["host1.example.com","host2.example.com","host3.example.com"],"allow_runbooks":true,"bookstack":{"url":"https://bookstack.example.com","books":"runbooks"}},"admin-chat":{"channel_id":"111111111111111111","connected_account_id":"11111111-1111-1111-1111-111111111111","host_scope":["host3.example.com"],"allow_content":false,"allow_runbooks":true,"bookstack":{"url":"https://vortex-bookstack.example.com","books":"networking,operations-getting-started,organization,bookstack,v-rising,scripts"}}}'
NOTIFY_DEFAULT_TARGET=automations
```

Both names above are placeholders. Put real ids and real per-target BookStack
URLs only in `stack.env`, which is gitignored and mode `0600`.

`host_scope` is the host (or list of hosts) a message to that target may talk
about. Entries cover themselves and their subdomains. `allow_content` is a
boolean, default `true`; when it is `false` the target may not use the
content-search tool at all. `allow_runbooks` is a boolean, default `false`, and
`bookstack` names that target's own instance. A target needs both the grant and
a complete `bookstack` object; the gate is default-deny, and adding a target
does not grant runbooks. `automations` is TekkiFox's Automations and may report
rossmoney and Vortex hosts, and it points at the operator's wiki. `admin-chat`
is the Vortex server's admin channel and is scoped to `host3.example.com` only,
with `allow_content: false` for the operator's CMS and its own `bookstack`
pointing at the Vortex wiki. That keeps the operator's other infrastructure and
CMS content out of the game-hosting server, and keeps each target on its own
runbook instance. A new channel on the same server gets the same protection by
giving its target the same guard values, without changing code — and still gets
no runbooks unless it is explicitly granted an instance.

Resolution works like this:

- `-t NAME` selects a target. When `-t` is omitted, `NOTIFY_DEFAULT_TARGET`
  applies. When neither is set, delivery keeps the legacy shape exactly:
  `COMPOSIO_ARGS` plus `COMPOSIO_CONNECTED_ACCOUNT_ID`, with no target lookup.
- The target's `channel_id` and `connected_account_id` are merged into the tool
  arguments and win over the same keys in `COMPOSIO_ARGS`; the target's
  connected account also wins over `COMPOSIO_CONNECTED_ACCOUNT_ID`.
- An optional `host_scope` is a non-empty string or a non-empty array of
  non-empty strings. It names the only host(s) a message to that target may
  talk about; absent means no restriction. An entry covers itself and its
  subdomains. The guard is enforced twice: `run-prompt.sh` adds the scope to
  the instruction and exports it to `ssh_probe`, and `notify.sh` refuses to
  send a message whose body names a host outside the list. The delivery refusal
  is non-zero and happens before any request. A malformed value fails before
  any request and names the offending targets, never their ids.
- The delivery guard extracts host-like tokens (dotted names with a
  letters-only suffix) from the message and accepts a token only when it equals
  a scope entry or is a subdomain of one. URLs and code spans are scanned. It
  does not catch bare labels, IP addresses, spaced or obfuscated names,
  Unicode lookalikes, or a figure with no hostname; the prompt handles semantic
  attribution, and the guard is the hard floor.
- An optional `allow_content` must be a JSON boolean. When a target sets it
  `false`, `run-prompt.sh` adds a plain-words line to the instruction: do not
  use the content-search tool, and do not include anything about the operator's
  site, CV, projects or pages. A malformed value fails before any request and
  names the offending targets, never their ids.
- An optional `allow_runbooks` must be a JSON boolean and defaults to `false`.
  An optional `bookstack` must be an object with an `https://` `url` and a
  non-empty comma-separated `books` list. A target needs both the grant and the
  instance; only then are `NOTIFY_RUNBOOKS_ALLOWED=1`,
  `NOTIFY_BOOKSTACK_URL` and `NOTIFY_BOOKSTACK_BOOKS` exported and runbooks
  mentioned in its instruction. Every other target clears all three, so
  `mcp/bookstack.py` refuses both tools. A malformed value fails before any
  request and names the offending targets, never their ids or values.
- `run-prompt.sh` resolves the effective target's `host_scope` and adds a
  plain-words line to the instruction: the delivery scope for the run, and that
  nothing about any other host may appear in the message. It also filters
  `HOST_REPORT_HOSTS` to that scope and lists the ordered known hosts for the
  digest. `prompts/notify.md` turns that into a hard rule: filter `host_label`
  to the scoped hosts, report only them, never mention another host even in
  passing, and say plainly when a scoped host has no data instead of falling
  back to another host's numbers. With no scope, the instruction keeps the
  old target line and adds only the known-host list.
- `allow_content: false` is also a hard guard, not only an instruction:
  `run-prompt.sh` exports `NOTIFY_CONTENT_DISABLED=1` before it execs
  `opencode`, and `mcp/oracle_query.py` refuses every `search_content` call with
  `content search is disabled for this run` while that variable is non-empty.
  The MCP server is a child of the opencode process, so it inherits the value.
  A target without the flag, or with `true`, behaves exactly as before and the
  variable is not exported.
- `allow_runbooks` plus the target's `bookstack` object is the hard guard for
  the BookStack tools: only a target with both exports the grant, URL and books,
  and `mcp/bookstack.py` refuses both tools for anything else before it reads
  configuration. The URL must be `https`. The CMS content gate is separate and
  still applies to `oracle_search_content`.
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
agent's rules, a target with `allow_content: false` also carries a server-side
CMS block, and a runbook grant is bound to that target's own BookStack URL and
books, so one target cannot read another's wiki.

## Inbound Discord replies

By default the bot is output-only. Set `NOTIFY_INBOUND_CHANNEL_ID` (one
channel) or `NOTIFY_INBOUND_SOURCES` (several channels on one or more servers)
and the container also runs `scripts/discord_inbound.py`, a long-lived Python
process alongside supercronic. A reply from an allowlisted human is turned
into an instruction for the same `notify` agent; the agent answers with
`scripts/notify.sh`, so the answer lands back in the channel it came from.

This is not a general chat endpoint. It is a bounded, at-most-once command
path into a read-only agent, with a channel and an author allowlist for every
source. Leave both variables unset to keep the container output-only; the
entrypoint then logs
`inbound disabled: neither NOTIFY_INBOUND_CHANNEL_ID nor NOTIFY_INBOUND_SOURCES is set`
and never starts the listener.

### Transport: Composio tool execution

**Proxy does not work for the `discordbot` toolkit.** Composio returns
`Proxy execute is not supported for toolkit "discordbot"`. The listener
therefore reads with normal Composio tool execution, not the proxy, not a
Discord bot token in the container, and not `DISCORD_NEW_MESSAGE_TRIGGER`:

```text
POST https://backend.composio.dev/api/v3.1/tools/execute/DISCORDBOT_LIST_MESSAGES
  connected_account_id: that source's Discord Bot connection
  arguments: {channel_id, limit, before?}
```

- It reuses `COMPOSIO_API_KEY` and a Discord **Bot** connected account. No new
  credential and no bot token in `stack.env`.
- The trigger is delivered by webhook or a realtime Pusher subscription. This
  container publishes no inbound port, and the trigger API has no documented
  "poll my trigger events" REST method. Adding a webhook receiver or the
  Composio SDK would be a larger change and a new runtime dependency.
- Tool execution is a plain authenticated REST call, so `urllib` is enough.
  No new Python package is installed. Redirects are refused, so the API key is
  never forwarded to another host.
- Discord returns messages newest-first. The listener walks backwards with
  `before` until it reaches its saved cursor, then handles the new messages
  oldest-first. This avoids the `after` pitfall where a backlog larger than
  one page would skip messages between the cursor and the newest page.
- Only the HTTP status and a fixed failure token are logged. A Composio error
  body can echo arguments, so it is never printed.

Each bot connection needs **View Channel** and **Read Message History** in the
channel it reads. The reply path needs Send Messages. If the connection lacks
Read Message History, the read tool fails or returns an empty page and the
listener logs only a redacted `poll failed source=<name>: ...` line; grant the
permission on the bot's role and restart. An empty first poll still establishes
a time-based baseline, so granting the permission later cannot make old history
look new.

The listener reads each configured channel only. A reply inside a thread has
the thread's channel id, so it is not followed; answer in the channel itself.

### Configuration

Single-channel mode is unchanged; it is still the right shape for one server:

```sh
# Required for single-channel inbound.
NOTIFY_INBOUND_CHANNEL_ID=111111111111111111
NOTIFY_INBOUND_ALLOWED_AUTHORS=222222222222222222,333333333333333333
NOTIFY_INBOUND_TARGET=admin-chat

# Optional.
NOTIFY_INBOUND_CONNECTED_ACCOUNT_ID=
NOTIFY_INBOUND_POLL_SECONDS=30
NOTIFY_INBOUND_MAX_INSTRUCTION_CHARS=1000
NOTIFY_INBOUND_QUEUE_SIZE=4
NOTIFY_INBOUND_RUN_TIMEOUT_SECONDS=300
NOTIFY_INBOUND_STATE_FILE=/home/agent/.local/share/opencode/discord-inbound-state.json
```

`NOTIFY_INBOUND_CHANNEL_ID` is the only channel read in this mode. The listener
refuses to start without it. `NOTIFY_INBOUND_ALLOWED_AUTHORS` is a
comma-separated list of Discord **user ids**, not usernames; usernames can
change, ids cannot. The listener refuses to start if it is empty,
whitespace-only, contains an empty entry, or contains anything that is not a
numeric id. That is the authorisation boundary, so a typo is a hard failure
rather than a silent wildcard.

`NOTIFY_INBOUND_TARGET` names the target used for the answer. It must exist in
`NOTIFY_TARGETS`, and its `channel_id` must equal `NOTIFY_INBOUND_CHANNEL_ID`;
the listener refuses to start otherwise, so an answer cannot be routed to a
different channel. Its `host_scope`, `allow_content`, `allow_runbooks` and
`bookstack` guards are applied to the inbound run exactly as they are for a
scheduled prompt, including the server-side `NOTIFY_CONTENT_DISABLED=1` CMS
block and the default-deny per-target `NOTIFY_RUNBOOKS_ALLOWED` grant. If neither
`NOTIFY_INBOUND_TARGET` nor `NOTIFY_DEFAULT_TARGET` is set, the listener falls
back to the legacy Composio delivery shape (`COMPOSIO_ARGS` plus
`COMPOSIO_CONNECTED_ACCOUNT_ID`) and logs a warning that it cannot verify the
answer goes to the inbound channel. With `DISCORD_WEBHOOK_URL` set, both
target variables must be empty: a webhook has no named targets, and
`notify.sh` rejects the combination.

`NOTIFY_INBOUND_CONNECTED_ACCOUNT_ID` pins the connection used to read. It is
optional; when set it must match the target's `connected_account_id`, so the
same bot reads and sends. Otherwise the target's account is used, then
`COMPOSIO_CONNECTED_ACCOUNT_ID`, then the Composio project default.

#### Multiple servers: `NOTIFY_INBOUND_SOURCES`

`NOTIFY_INBOUND_SOURCES` is a JSON array. Use it instead of the single-channel
variables when the bot serves more than one server. Each entry needs a unique
`name`, its own `connected_account_id` and `channel_id`, and may set
`allowed_authors` and a reply `target`:

```sh
NOTIFY_TARGETS='{"admin-chat":{"channel_id":"000000000000000000","connected_account_id":"ca_EXAMPLECONNECTION","host_scope":"host3.example.com","allow_content":false}}'

NOTIFY_INBOUND_SOURCES='[
  {"name":"vortex-admin","connected_account_id":"ca_EXAMPLECONNECTION","channel_id":"000000000000000000","allowed_authors":"222222222222222222","target":"admin-chat"},
  {"name":"automations-general","connected_account_id":"ca_EXAMPLECONNECTION","channel_id":"000000000000000000","allowed_authors":"222222222222222222"}
]'
```

The ids above are the documented examples: `ca_EXAMPLECONNECTION` is the Vortex
Game Servers bot connection and `000000000000000000` is its `#admin-chat`;
`ca_EXAMPLECONNECTION` is the TekkiFox's Automations bot connection and
`000000000000000000` is its `#general`. The other three Automations channels
are added the same way, by channel id: `#sonarr` `000000000000000000`,
`#seerr` `000000000000000000`, `#radarr` `000000000000000000`. Give each its
own source name, for example `automations-sonarr`.

Every source has an independent cursor and dedupe file derived from
`NOTIFY_INBOUND_STATE_FILE`: with the default path, source `vortex-admin` uses
`discord-inbound-state.vortex-admin.json`. One server's traffic cannot advance
another's cursor or suppress its messages. Switching a live deployment from
single-channel mode to sources mode starts each source with a fresh baseline
(the first poll actions nothing) rather than replaying old messages.

Authorisation is per source and default-deny. A source with no
`allowed_authors` inherits `NOTIFY_INBOUND_ALLOWED_AUTHORS`; a source with
neither refuses to start, naming the source. A source's own list overrides the
global list for its channel only.

Reply routing is per source, and the listener logs `reply=` for each one:

- With `target`, the answer uses `notify.sh -t <target>` exactly as a scheduled
  prompt does. The target must exist in `NOTIFY_TARGETS`, its `channel_id` must
  equal the source's channel, and its `connected_account_id` must match the
  source's account. `COMPOSIO_TOOL` is required for this path.
- Without `target`, the listener replies through the bot with
  `DISCORDBOT_CREATE_MESSAGE`: for that run it sets `COMPOSIO_TOOL`,
  `COMPOSIO_CONNECTED_ACCOUNT_ID` and `COMPOSIO_ARGS` (the source's
  `channel_id`), and removes `DISCORD_WEBHOOK_URL` and
  `NOTIFY_DEFAULT_TARGET` from the child environment. The agent still calls
  `notify.sh -m`, but it cannot reach another channel or server. This is what
  makes an Automations channel work without a `NOTIFY_TARGETS` entry.

`NOTIFY_INBOUND_SOURCES` cannot be combined with `DISCORD_WEBHOOK_URL`: a
webhook is bound to one channel, while sources choose their own reply path.

`NOTIFY_INBOUND_STATE_FILE` must be on a writable persistent path. The default
is inside the `opencode-data` named volume. Do not point it at the tmpfs
`/home/agent/.local/state`, or a restart would lose the baseline and could
replay messages.

### Authorisation, default-deny

Every polled message is checked in this order:

1. **Well formed.** A missing or non-numeric message id, author or type is
   refused as `malformed_message`.
2. **Not a bot.** `author.bot` and any message with `webhook_id` are refused as
   `bot_author`. This includes the notification bot itself, which is what
   prevents an infinite loop. Only message types `0` (default) and `19`
   (reply) are considered; system messages are refused as `unsupported_type`.
3. **Author allowlist.** The author id must be in that source's allowlist
   (its own `allowed_authors`, or the inherited
   `NOTIFY_INBOUND_ALLOWED_AUTHORS`); otherwise `author_not_allowed`.
4. **Channel.** `channel_id` must equal that source's `channel_id`; otherwise
   `wrong_channel`.
5. **Not a duplicate.** That source's state file records the message id
   before the instruction is queued; a restart or an overlapping poll sees
   `duplicate`. Another source's state is never consulted.
6. **Has usable text.** Discord mention syntax (`<@id>`, `<@!id>`, `<@&id>`,
   `<#id>`) and literal `@everyone` / `@here` are stripped; if nothing
   remains, the message is `empty`.
7. **Length.** The stripped instruction must be at most
   `NOTIFY_INBOUND_MAX_INSTRUCTION_CHARS`; longer messages are refused as
   `too_long`, never silently truncated.

Refusals are logged as `refused source=<name> reason=<token> msg=<short hash>`.
The message body, author id, channel id, API key and webhook URL are never
logged. The short hash is derived from the message id, so repeated polls of
the same refused message are recognisable without exposing the id.

The first poll with no state file is a **baseline**: it records the newest
existing message and actions nothing. The notification bot's earlier messages
and anything else that predates the listener are therefore ignored. This is
done independently for every source. If the first poll returns no messages
(an empty channel, or a bot without Read Message History), the baseline is a
synthetic snowflake for the current time. Every pre-existing message is older,
so a later permission grant cannot make old history look new. Each state file
is written atomically with mode `0600`; a corrupt file makes the listener
refuse to start rather than guess which messages were handled.

### Rate limit and shutdown

One worker thread runs the agent. `NOTIFY_INBOUND_QUEUE_SIZE` bounds the
waiting instructions; when the queue is full the message is recorded, dropped
and logged as `dropped reason=queue_full`. Two agents never run at once.
`NOTIFY_INBOUND_RUN_TIMEOUT_SECONDS` is a hard limit; the agent runs in its own
process group so a timeout or container shutdown terminates the agent and its
MCP children together.

Recording before queuing gives **at-most-once** semantics: a crash can lose a
queued instruction, but it cannot replay one. That is the deliberate trade for
a command path into an agent with database access. A backlog larger than
`20 * 100` messages is truncated with a `backlog truncated` warning; the
listener does not buffer unbounded history.

### Agent output and the debug transcript

Both run paths write the agent's complete stdout and stderr to one shared
transcript, `notify-debug.log`, under the OpenCode data directory:
`$XDG_DATA_HOME/opencode/notify-debug.log`, defaulting to
`/home/agent/.local/share/opencode/notify-debug.log`. The inbound listener
derives the same directory from `NOTIFY_INBOUND_STATE_FILE`, which defaults to
that path; `run-prompt.sh` derives it from `XDG_DATA_HOME` (falling back to
`$HOME/.local/share`). That is the writable `opencode-data` volume.

Each attempt is self-describing:

```text
===== notify run 2026-11-01T14:05:06Z =====
path: inbound:vortex-admin
model: provider/model
instruction:
<the exact instruction or prompt sent to the agent>
--- agent output ---
<the agent's stdout and stderr>
--- run end: exit=0 duration=29.5s sent=no ---
```

The footer records the exit code, duration and whether `notify.sh` confirmed a
send through `NOTIFY_SEND_MARKER` (`sent=yes`, `sent=no`, or `sent=unknown`
when no marker path could be created). Inbound retries each get their own
header and footer in the same file. Read it with, for example:

```sh
docker compose exec agent tail -n 200 \
  /home/agent/.local/share/opencode/notify-debug.log
```

The file is mode `0600` and capped at 256 KiB. When it is nearly full it is
reset before the next run, and output past the cap is drained and discarded
with a truncation note, so it never grows without limit. The container log gets
the transcript path but never its contents; the separate one-line summary
carries only the short instruction excerpt. **The transcript contains the
operator's instructions, model output and host data, so treat it as
internal.** It does not add anything the agent itself did not print.

The separate one-line container-log summary (see [Read logs](#read-logs))
contains only a flattened instruction excerpt; the full instruction is in this
transcript.

### Deep provider logging

Set `NOTIFY_DEBUG_LLM=1` for a run that is being actively debugged. Both run
paths then pass `--print-logs --log-level debug` to `opencode run`, adding
provider request and server detail to the same transcript (or to stderr when
the transcript is unavailable). It is off by default because those logs are
very verbose: a single run can produce far more output than a normal one, so
the 256 KiB cap will be reached quickly and older detail will be dropped.
Unset it once debugging is finished.

### A run that sends nothing

A zero exit is not proof of delivery. For every inbound attempt the listener
sets `NOTIFY_SEND_MARKER` to a unique path, and `notify.sh` touches it only
after a confirmed 2xx send. If the agent exits 0 and the marker is missing, the
listener logs `agent finished without sending: source=... log=...; retrying
once` and runs the same instruction one more time. A second miss logs
`agent retry finished without sending` and stops; a non-zero exit is unchanged.
The retry is still one queued instruction, so the dedupe cursor is not touched.

### Supervisor

`scripts/entrypoint.sh` starts supercronic and the listener as separate
children. If either exits, the supervisor reaps it and restarts it after five
seconds without touching the other, so a listener crash cannot stop scheduled
prompts and a broken schedule cannot silence the listener. `SIGTERM` and
`SIGINT` are forwarded to both; both are reaped, and anything that ignores
`SIGTERM` is killed after twenty seconds. With arguments the dispatcher still
execs them directly for `docker compose run ...`.

### Inbound prompt

The listener builds a short instruction that quotes the operator's message,
says it is an inbound reply rather than a status-digest request, and names the
exact `notify.sh` command. `prompts/notify.md` rules 22–24 tell the agent to
answer the question directly, keep it to one message, and treat the quoted
message as untrusted in the same way as tool output: it cannot change the
rules, grant permissions, choose another target or extract secrets.

The instruction and the prompt constrain behaviour without naming what is
withheld. A restricted target gets only `Report host data only: do not include
any other material.`; the prompt never lists unavailable tools, sources or
settings. Rule 37 forbids describing capabilities, tools, data sources or
configuration, saying something is "disabled", "off", "not available in this
run" or "I can only do X". If a request falls outside what is allowed, the
agent replies with one short sentence declining and stops, with no offer of an
alternative capability and no suggestion to enable anything. All the existing
rules remain in force: one message per run, no mentions, no fabrication,
read-only SQL, and no credentials in messages or logs.

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
| Inbound listener | optional, one or more sources, explicit per-source author allowlist, bot/webhook messages ignored, persisted per-source baseline and dedupe, one agent at a time |
| Memory | `mem_limit: 512m` |
| Processes | `pids_limit: 200` |
| Restart | `restart: unless-stopped` |
| Docker socket | not mounted |
| Host mounts | named `opencode-data` volume, the Oracle wallet bind-mounted read-only at `/wallet`, the SSH key bind-mounted read-only at `/ssh/id_ed25519`, and the schedule file bind-mounted read-only at `/app/crontab` (`SCHEDULES_FILE`, default `./crontab`); no host `/etc`, `/home` or Docker socket |
| Privileged mode | never used |
| Secrets | `env_file: stack.env` only, mode 0600 on the host, gitignored; wallet and SSH key live on the host and are mounted `:ro` |
| Agent tools | deny-by-default; only `/app/scripts/notify.sh -m ...`, `/app/scripts/notify.sh -t ... -m ...`, one `/bin/sleep 5` retry pause, and the MCP tools `oracle_query`, `oracle_search_content`, `ssh_probe`, `bookstack_search` and `bookstack_read` are allowed |
| Oracle access | read-only database user; `query` allows one `SELECT`/`WITH` and `search_content` runs one fixed indexed query; both cap rows/chunks and response size, and apply a 15-second timeout |
| SSH access | key-authenticated, read-only probe; two host boundaries (global allowlist and run scope) are applied before any socket; policy refusals name the host and never connect; no remote command runs |
| Runbook access | read-only BookStack API, one dedicated token; per-target instances: a target needs `allow_runbooks: true` and a `bookstack` object (`https` url plus books), and only that target's URL/books are exported for the run; `admin-chat` points at the Vortex instance with all six of its books, `automations` at the operator's instance with `runbooks`, and neither can reach the other's instance; a target without a grant or an instance refuses both tools before any request; exactly the books in that target's list can be searched or read, and per-book search means another book is never even fetched; a missing slug is skipped, not fatal; a forbidden page and a missing page share one refusal; redirects refused; token never in a URL, log or error; `allow_content: false` still gates the operator's CMS, not the target's own runbooks |
| Delivery guard | `notify.sh` refuses a scoped target's message when it names a host outside that target's `host_scope`, before any request |
| OpenCode sharing | disabled; snapshots and autoupdate disabled |
| Logs | `json-file`, 10 MB × 3 |

The image installs OpenCode from the official v2 installer pinned to `2.2.1`, verifies supercronic by SHA-256, and pins the base image by digest. OpenCode itself lives under `/opt/opencode`, so the runtime home directory can stay read-only.

## Threat model

This is a small, single-tenant agent, not a sandbox for untrusted users.

**Assets inside the container**

- The Discord webhook secret, which can post to one channel.
- The provider OAuth token or API key in the `opencode-data` volume.
- The Oracle password and wallet material.
- The SSH private key and the host allowlist it can reach.
- The ability to send messages as the bot/user the credentials represent.
- Message content and model prompts, which leave the host for the model provider.

**What the agent can do**

- Reach the internet outbound.
- Read its environment (including the webhook, provider credentials, and Oracle password).
- Call the shell only through the two allowed forms `/app/scripts/notify.sh -m ...` and `/app/scripts/notify.sh -t ... -m ...`, plus one `/bin/sleep 5` between a failed `oracle_query` call and its retry.
- Call `oracle_query` with a single read-only `SELECT`/`WITH` statement against the host-statistics objects.
- Call `search_content` to read short chunks from the published-content index.
- Call `ssh_probe` with hosts that are both in `SSH_ALLOWED_HOSTS` and inside the run's delivery scope. The probe opens a key-authenticated SSH session and closes it; it runs no remote command. A host outside either boundary is refused before a socket is opened.
- Call `bookstack_search` and `bookstack_read` against exactly the books in the target's `bookstack.books` list, and only when the run was explicitly granted that target's instance. A page outside that list, a missing page and an ambiguous slug are refused with the same message. Every other run refuses both tools before any request.
- Write only to the named volume and tmpfs.
- When the inbound listener is enabled, read the configured channels through Composio tool execution (`DISCORDBOT_LIST_MESSAGES`) using each source's bot connection. The listener itself never gives the agent an arbitrary command: it only invokes the fixed `opencode run --agent notify` command with a filtered instruction.
- For a source without a named target, send its answer through that source's bot connection (`DISCORDBOT_CREATE_MESSAGE`) to that source's channel. The listener sets the channel and account in the child environment; the model cannot choose them.

**What it cannot do**

- Accept inbound connections (no published ports). The inbound listener makes outbound poll requests; it does not listen.
- Read any channel other than a configured source's `channel_id`, or action a message from a bot, webhook, or author outside that source's allowlist.
- Send a source's answer to another source's channel: the per-run environment pins the bot account and channel, and a named target must match the source.
- Access the Docker socket, the host filesystem, `/etc`, `/home`, or other containers.
- Write the schedule file: it is bind-mounted read-only.
- Gain capabilities or new privileges.
- Browse the web, read repository files, spawn subagents, or run arbitrary shell commands: the `notify` agent denies every tool except the two `notify.sh` forms, the two Oracle tools and `ssh_probe`.
- Use the SSH key outside the probe: the key is invoked only by `ssh_probe`, with a fixed user and port, and the probe runs no remote command. It cannot probe a host outside the global allowlist or the selected target's scope.
- Send a message to a scoped target if it names a host outside that target's `host_scope`: `notify.sh` refuses before any request. The guard is exact-name based, so figures without a hostname are a prompt-level risk, not a code-level one.
- Read published content when the selected target sets `allow_content: false`: `run-prompt.sh` exports `NOTIFY_CONTENT_DISABLED=1` and the MCP server refuses `search_content` before it connects.
- Read a wiki page outside the target's `bookstack.books` list, including the deployment inventory: the server checks the page's book before returning text, searches per allowed book, and answers a forbidden page and a missing page identically. It cannot discover that another book or page exists. A run cannot reach another target's instance at all: only that target's URL is exported.
- Reach a wiki at all unless the target has `allow_runbooks: true` and its own `bookstack` instance: both BookStack tools refuse before reading configuration or opening a socket. A missing URL, a URL that is not `https`, a missing books list or a missing grant all refuse. The CMS content gate is separate, so a content-disabled target can still read its own runbooks. The token is never in a URL and redirects are refused.
- Read arbitrary CMS tables or write to the content index. It can only run a fixed, read-only semantic search over `ROSSMONEY.CONTENT_EMBEDDINGS`; a `query` against `ROSSMONEY.pages` or any other CMS table fails with a privilege error.

**Scenarios and residual risk**

- **Prompt injection.** The operator prompt is trusted, but text pasted into it may not be. A manipulated model can still send a misleading message and can echo content present in its prompt. It cannot read files, browse, or run arbitrary commands. Do not put secrets in schedule prompts. The same applies to every value returned by `oracle_query`, `oracle_search_content` or `ssh_probe`: the prompt tells the model to treat those values as data, never as instructions.
- **Destination leak.** A rossmoney host must not reach the Vortex `admin-chat`. The hard guard is in `notify.sh`: a scoped target refuses a message that names a host outside its `host_scope`, before any request. `ssh_probe` also refuses out-of-scope hosts, so the model cannot receive a rossmoney result in an admin-chat run. The exact-name guard does not catch a figure without a hostname, an IP address, a bare label, or an obfuscated name; those remain prompt-level risks. The guard is intentionally strict and also refuses unrelated domains in a scoped message.
- **SSH access.** The probe uses one unprivileged key, mounted read-only, and authenticates only to allowlisted hosts within the run scope. It runs no remote command, so a compromised model cannot use it as a shell. Host keys are not pinned (`AutoAddPolicy`), so the probe reports reachability, not host identity; an on-path attacker could answer the handshake, but cannot recover the private key.
- **Inbound message injection.** An allowlisted human's reply becomes an instruction to an agent with database access. The allowlist is the boundary: a non-allowlisted author, a bot, a webhook, a different channel, a duplicate, an over-long message or a mentions-only message never reaches the agent. An allowlisted human can still phrase a request that produces a bad answer or a wide read, exactly as they could by editing a schedule. The agent remains read-only and can still only send through `notify.sh`. The listener never passes the raw Discord payload: it strips mentions, caps length, and wraps the text in a fixed instruction. If the allowlist is too broad, the exposure is too broad.
- **Database access.** The Oracle user is read-only and limited to host statistics plus the published-content index. A compromised model can read those and cannot write. It cannot read arbitrary CMS tables. The wallet is mounted read-only, so the process cannot modify it.
- **Content retrieval.** The index contains published material and short chunks of it leave the host for the model provider during a content question, exactly as host statistics do for a metrics question. Retrieved chunks are untrusted data and are truncated, but they are still readable by the provider.
- **Runbook retrieval.** Runbook text leaves the host for the model provider when the agent answers a procedure question, just as host statistics do. The exposure is limited to the books in the target's `bookstack.books` list by code, and to whatever the token's BookStack role can see by permissions. A token for a broad role would defeat the allowlist, so use a dedicated user that can see only the books in that target's list. Each run is bound to one instance, so an `admin-chat` run cannot read the operator's wiki and an `automations` run cannot read the Vortex one. The token itself is shared by the MCP server; per-instance tokens are a follow-up. Runbook text is untrusted data: it can be wrong, stale, or contain injected instructions, and the prompt tells the model not to follow it. A target with no grant or no instance cannot reach a wiki at all.
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
- `opencode debug agents` resolves `notify` as a primary agent; its effective permission list denies everything, allows `oracle_query`, `oracle_search_content` and `ssh_probe` last (over the global denies), and allows only `shell` for `/app/scripts/notify.sh -m *` and `/app/scripts/notify.sh -t * -m *`. The `build` agent resolves the MCP tools to `deny`.
- The SQL-guard and input-handling suite in `tests/test_oracle_guard.py` passes without a database or the Oracle driver: 89 tests covering allowed `SELECT`/`WITH`, rejected `INSERT`/`UPDATE`/`DELETE`/`DROP`/`BEGIN`/`WITH ... DELETE`/`FOR UPDATE`/multiple statements/trailing junk/oversized SQL, the row cap, the response-size cap, PEM-only and `cwallet.sso` wallet acceptance, empty and `tnsnames.ora`-only wallet rejection, Java wallet files not being passed to the thin driver, and (with a fake driver) the hard cap, column names and `SET TRANSACTION READ ONLY`. They also assert that `oracledb.connect` receives `config_dir` with no `wallet_location` or `wallet_password`, that a wallet password in the environment cannot enable that prompt path, and that stdin is EOF for the driver while the JSON-RPC stream survives. They also cover the search limit default/ceiling/floor, query validation, chunk truncation at 500 characters, the search response cap, bind-only query text and limit, and sanitised errors, plus `NOTIFY_CONTENT_DISABLED=1` refusing `search_content` with a clear tool error and behaving unchanged when unset. With a stub driver they also cover one retry for each transient code on connect and execute, no retry for SQL/permission errors, the `code`/`retryable` error payload, `--selftest` success and a clean non-zero failure with no configuration (including the CLI exit path), and a broken stdout/stdin pipe ending quietly with no traceback.
- The MCP stdio handshake in the image returns two tools, `query` and `search_content`, and its guard/input errors are tool errors rather than protocol failures. A `search_content` call with no configuration returns `Oracle connection is not configured` and never echoes a user, DSN, wallet path, password, or the query text.
- OpenCode names an MCP tool `<server>_<tool>`, so the server's `query` and `search_content` tools resolve to `oracle_query` and `oracle_search_content`. `opencode debug agents` shows both names denied globally and allowed for `notify`, with `codemode: false` exposing them directly under the V2 `mcp.servers` shape.
- Image size on this branch, measured with `docker save | gzip -9` and `du -sh /` inside each image: `main` without content search is `242,637,972` bytes compressed / `593 MB` root filesystem; this branch is `242,638,242` bytes compressed / `593 MB` root filesystem. That is `+270` bytes compressed and `+2,245` bytes uncompressed: only the MCP Python file, prompt and OpenCode config changed, with no new package or dependency. The earlier Python 3, pip, `python-oracledb` and wheel cost (roughly `205 MB` to `232 MB` by the previous measurement) is unchanged. The 512 MB `mem_limit` is memory, not image size; the MCP server is only started when a session calls it.
- The image still runs `opencode debug agents` as uid `10001` with `--read-only` and the compose tmpfs/volume layout, and a write to `/` fails with `Read-only file system`.
- The target stub tests in `tests/notify-targets.sh` cover: a target resolves into the Composio payload, unknown and malformed targets fail without requests, malformed `host_scope`, `allow_content`, `allow_runbooks` and `bookstack` values fail by target name only (never the id or value), `-t` is rejected with a webhook, the legacy and default-target paths both work, and the bot-source reply path posts `DISCORDBOT_CREATE_MESSAGE` to the pinned source channel and account. The delivery guard is covered explicitly: a report naming `host1.example.com` aimed at `admin-chat` exits non-zero and makes no request; the same report aimed at `automations` is delivered; a Vortex host is accepted on `admin-chat`; `other.example` is refused on both; a rossmoney subdomain and a rossmoney URL are refused on `admin-chat`; and an unscoped target is not guarded. The send marker is covered too: a 2xx Composio or webhook send writes it, a rejected send does not, an unset marker changes nothing, and an unwritable marker warns without changing the exit status.
- The whole Python suite passes on the rebased tree: `python3 -m unittest discover -s tests -p 'test_*.py'` reports **338 tests, OK**. That includes the Oracle guard/search suite, the inbound listener suite, the prompt/config contract tests, `tests/test_ssh_probe.py`, `tests/test_host_report.py` and `tests/test_bookstack.py`. The inbound suite covers the transcript header/footer and cap, the unsent retry (exactly once, marker present means no retry, two misses stop, non-zero exits unchanged), `NOTIFY_DEBUG_LLM` being off by default and opt-in, the one-line container-log summary (flattened, truncated, capped even with a long source name), and the generated instruction never naming a withheld capability.
- `tests/test_ssh_probe.py` covers: an allowlisted success with latency; every failure class; a Vortex host probed when it is in scope; a rossmoney host refused in a Vortex scope with the connector asserted never called; a host outside `SSH_ALLOWED_HOSTS` refused without a connection; SSH not configured reported as `configured: false`; one failing host not poisoning another; the total-time deadline skipping later hosts; the `scope` argument unable to widen `NOTIFY_HOST_SCOPE`; subdomain matching and lookalike rejection; a bound/unbound selftest; and no key path, user or key material in output. No live SSH connection was made.
- `tests/test_host_report.py` covers the old report shape: per-host field order, the summary counts, known-host-first ordering then extras by name, zero-sample hosts, the stale boundary at exactly 20 minutes (fresh) and 21 minutes (stale), each threshold at the boundary and one below, the inline flag text, the `[!]` marker, SSH success/failure/refusal handling, the not-configured line, and truncation dropping whole lines with the exact count. It runs without a database.
- `tests/run-prompt-scope.sh` passes against the fake `opencode`: a scoped target adds the scope line and exports `NOTIFY_HOST_SCOPE` as JSON; an unscoped target keeps its instruction plus the known-host line; an array scope lists both hosts; `allow_content: false` still exports only `NOTIFY_CONTENT_DISABLED` when no runbook grant is present; `allow_runbooks: true` with a `bookstack` object adds the runbook line and exports the grant plus that target's URL and books; `admin-chat` exports the Vortex URL and its six books while `automations` exports the operator URL and `runbooks`, and neither carries the other's URL; a grant without an instance and an instance without a grant export nothing; a content-enabled target without the flag mentions no runbooks and clears inherited values; malformed values fail before the agent runs. The MCP preflight cases still pass. The transcript cases assert the header (path, model, exact prompt), captured stdout and stderr, the exit/duration/send footer, `sent=yes` when the marker is touched, `sent=no` and the real exit code on failure, the 256 KiB cap, and that `NOTIFY_DEBUG_LLM=1` (and only then) adds `--print-logs --log-level debug`.
- `python3 -m py_compile` passes on `mcp/bookstack.py`, `mcp/ssh_probe.py`, `mcp/host_report.py`, `scripts/agent_capture.py`, `scripts/discord_inbound.py`, `tests/test_bookstack.py`, `tests/test_ssh_probe.py`, `tests/test_host_report.py` and `tests/test_discord_inbound.py`; `sh -n` passes on `scripts/notify.sh`, `scripts/run-prompt.sh`, `tests/notify-targets.sh` and `tests/run-prompt-scope.sh`.
- `opencode.json` statically registers three MCP servers (`oracle`, `ssh` and `bookstack`), gives all three the same 60-second startup/execution budget, keeps the global denies on `oracle_query`, `oracle_search_content`, `ssh_probe`, `bookstack_search` and `bookstack_read`, and allows all five for `notify`.
- `tests/test_bookstack.py` makes real HTTP requests to loopback stubs (with a test-only opener that maps a fake `https://` host to each stub) and asserts what each stub received: search asks only the resolved book on the selected instance, a result whose `book.id` is outside the allowlist is dropped and never contributes to `count`, a page with `book_id: 99` is refused in both id and slug form without fetching its content, and a forbidden page, a missing page and an unknown slug return the identical `page is not available` message with no book or page name. It also covers default-deny: with `NOTIFY_RUNBOOKS_ALLOWED` missing, empty or any value other than `1`, both tools refuse with zero requests, even with a complete BookStack configuration; a grant without a URL or books refuses; a URL that is absent or not `https` fails configuration; the token appearing only in the `Authorization` header (never in a URL parameter) and never in output on a 401 or 500 whose body echoes it; redirect refusal; the allowlist/URL/timeout configuration errors; the startup exit path naming the variable without echoing the value; a startup without the grant serving without configuration; and a server that accepts a connection and never answers failing inside `BOOKSTACK_TIMEOUT_SECONDS`. The isolation tests run an `admin-chat` (Vortex) environment against a Vortex stub and an `automations` (operator) environment against an operator stub: each contacts only its own stub, the other stub receives zero requests, and the exported URLs differ. The allowlist tests also prove that a slug which does not exist on the instance is skipped, that a search with no resolvable book returns an empty result, and that a read is then refused, rather than failing the run.
- `python3 /app/mcp/bookstack.py --selftest` is exercised in the suite: with configuration it prints names only (no URL, token or book slug), and with a grant but missing tokens it exits `1` naming `BOOKSTACK_TOKEN_ID`. No live BookStack instance was contacted at any point.

Not verified here (no credentials, no live hosts, no Docker daemon use):

- No live SSH connection to any host, including Vortex or rossmoney. Every connector call in the suite is a stub.
- No live Oracle query, no live `search_content`, and no live model run of the digest. The formatting and prompt contracts are tested without a model.
- No live BookStack request and no real BookStack token. `tests/test_bookstack.py` talks only to a stub on `127.0.0.1`; the real `Runbooks` book was not fetched. The dedicated BookStack user and its role are operator setup, not something this branch can create.
- No `docker build`/image-size measurement for the paramiko addition. Paramiko and its direct dependencies are pinned in the Dockerfile; the base image has no `openssh-client`, which is why it is added.
- The schedule lines are documentation only. The live schedule is the host `SCHEDULES_FILE`; this branch cannot activate it.
- Host-key identity is not pinned (`AutoAddPolicy`); the probe measures reachability and key authentication, not the remote host's identity.
- The instruction tests in `tests/run-prompt-scope.sh` pass against a fake `opencode`: a scoped target adds the delivery-scope line and the content block, an array scope is listed, an unscoped target keeps the old instruction byte-for-byte, no target keeps the bare instruction, `allow_content: false` without a host scope adds only the content block, `NOTIFY_CONTENT_DISABLED=1` is exported only for a false value, `allow_runbooks: true` is the only thing that adds a runbook line and exports `NOTIFY_RUNBOOKS_ALLOWED=1`, a content-enabled target without the flag mentions no runbooks and clears any inherited grant, and malformed `host_scope`, `allow_content` or `allow_runbooks` fails before the agent runs. The same suite now drives the MCP preflight with stub servers: a server that omits `search_content`, a missing server and a server whose Oracle self-test fails all fail before `opencode` starts without echoing `ORACLE_PASSWORD` or `ORACLE_DSN`, and a healthy server leaves the instruction byte-for-byte unchanged.
- The text-level contract tests in `tests/test_prompt_contract.py` assert that the prompt keeps the query-first run order, the "never send a tool-unavailable message" rule, the zero-rows-is-not-a-failure wording, the one-pause/one-retry rule, the capability-disclosure ban, the positive-only restricted-run wording, and the existing one-message/no-mentions/host-scope/read-only-SQL rules. They also assert that `opencode.json` keeps the 60-second startup timeout, at least 10 steps, the exact `/bin/sleep 5` allowance, and the denied-then-allowed Oracle tool permissions. These are text assertions, not a live model run.
- The environment inheritance assumption was checked in the image built from this branch (`notify-agent:target-scope`), not just in the unit tests. A temporary local MCP server was spawned by `opencode run --standalone` with `NOTIFY_CONTENT_DISABLED=1` in the parent environment; the child's `/probe/env.txt` contained `NOTIFY_CONTENT_DISABLED=1` and `PYTHONUNBUFFERED=1`, so OpenCode merges the parent environment into the configured MCP environment. Running the installed `/app/mcp/oracle_query.py` over stdio in the same image returned `{"error":"content search is disabled for this run"}` with `isError: true` when the variable was set, and `Oracle connection is not configured` without it.

Inbound listener checks on this branch:

- `python3 -m py_compile scripts/discord_inbound.py tests/test_discord_inbound.py` passes.
- `python3 -m unittest discover -s tests -p 'test_*.py' -v` passes: the existing SQL-guard suite plus the inbound suite. No network, Discord channel, Composio account, database or model is used.
- The inbound suite covers: an empty, missing, whitespace-only or non-numeric allowlist refusing to start; the channel filter; bot and webhook authors being ignored; reply type 19 accepted and system types refused; the length cap; mention stripping including `@everyone`, `@here`, user, role and channel forms; dedupe across a simulated restart; a corrupt state file refusing to start; the bounded queue; the worker's `max_active` staying at 1; baseline-without-actioning, including the time-based fallback when history is unreadable; backwards pagination to the cursor; the `DISCORDBOT_LIST_MESSAGES` tool request shape (`connected_account_id`, `user_id`, `channel_id`, `limit`, `before`, `x-api-key`) against a stub; success, empty, HTTP error, API failure, malformed payload, missing id, invalid JSON and redirect-refusal cases; and the exact `opencode run --standalone --agent notify --model ... -- <instruction>` argv with `NOTIFY_CONTENT_DISABLED` set only for a target whose `allow_content` is false, and `NOTIFY_RUNBOOKS_ALLOWED`/`NOTIFY_BOOKSTACK_URL`/`NOTIFY_BOOKSTACK_BOOKS` set only when the target has both `allow_runbooks: true` and a `bookstack` object. It also asserts that an `admin-chat` run exports only the Vortex URL and its six books, and an `automations` run exports only the operator URL and `runbooks`; a grant without an instance and an instance without a grant export nothing.
- The multi-source suite covers: two sources with their own accounts, channels and reply modes; per-source state file names; per-source allowlists overriding the global list; a source with neither list refusing to start and naming itself; duplicate source names and unsafe names refused; target channel/account mismatches refused; sources plus a webhook refused; independent baselines and cursors; per-source dedupe for the same message id; and both reply paths (named target instruction, and bot environment pinned to the source's `DISCORDBOT_CREATE_MESSAGE` account and channel with webhook/default-target removed).
- The refusal path is exercised at the real process boundary: with `subprocess.Popen` patched, a message from a disallowed author produces no `Popen` call, no enqueue, and this log line (message id hashed, body absent):
  `refused source=default reason=author_not_allowed msg=d1a35b8e`
- `composio tools info DISCORDBOT_LIST_MESSAGES` was inspected locally: the tool slug, `channel_id` requirement, `limit`, and `before`/`after`/`around` parameters match the implementation. `python3 -m py_compile` and the unit tests stub the HTTP call; no live listener poll was performed.
- `sh -n scripts/entrypoint.sh` passes. The supervisor was not exercised against a real container.

Not verified, because this repository contains no credentials or live services, and no live write is allowed:

- The listener running end to end against either live channel. The Composio HTTP call is stubbed in every test; the bot connections' View Channel and Read Message History permissions were not exercised by this code.
- A live inbound reply end to end (no real channel, message or author id was used).
- A live Discord or Composio send. The reply path is tested by inspecting the child environment and stubbed `notify.sh`/`Popen` behaviour; nothing was posted to either server.
- `opencode auth login "OpenCode Console" --method device` (no provider account was used; the probe above only confirms the `device` and `key` methods exist).
- A live Oracle query or a live `search_content` call (no credentials or wallet were used; the first mate runs the live checks). The guard, protocol, bind, cap, and failure paths were exercised without a database.
- `docker compose config`, an image build, and `shellcheck` (Docker and shellcheck are not installed in the build environment used for this branch).

Do those checks on the target host after copying `stack.env`, mounting the wallet, granting the bot Read Message History in the admin channel, and completing the device flow.

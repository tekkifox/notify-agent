#!/usr/bin/env python3
"""Inbound Discord replies for the notify agent.

The bot is normally output-only: ``scripts/notify.sh`` posts a message and
nothing reads the channel.  This listener closes that loop.  It polls one or
more configured Discord channels through Composio tool execution, applies a
default-deny authorisation model, and for each accepted message runs the
existing ``notify`` OpenCode agent non-interactively.  The agent answers with
``scripts/notify.sh``, so the answer comes back to the same channel.

Why Composio tool execution and not the proxy or a trigger
----------------------------------------------------------

The deployment already has a Composio Discord Bot connected account and
``COMPOSIO_API_KEY``; using it adds no credential.  Composio's proxy endpoint
(``/api/v3.1/tools/execute/proxy``) works for the ``discord`` user toolkit but
is **not supported for ``discordbot``** -- it returns
``Proxy execute is not supported for toolkit "discordbot"``.  The bot toolkit
must be executed as a tool instead:
``POST /api/v3.1/tools/execute/DISCORDBOT_LIST_MESSAGES`` with
``channel_id``, ``limit`` and, when paging, ``before``.  Composio's trigger is
delivered by webhook or a realtime Pusher subscription; this container
publishes no inbound port, and the trigger API has no documented "poll my
trigger events" REST method.  Tool execution is a plain, bounded, idempotent
poll with no new dependency (``urllib`` is enough).

Discord returns messages newest-first, so the listener walks backwards with
``before`` until it reaches its saved cursor, then handles the new messages
oldest-first.

Sources
-------

``NOTIFY_INBOUND_SOURCES`` is an optional JSON array.  Each entry has a
``name``, its own ``connected_account_id`` and ``channel_id``, and optionally
``allowed_authors`` and a reply ``target``.  Every source has its own cursor
and dedupe file, so one server can never advance another's cursor or suppress
its messages.  When ``NOTIFY_INBOUND_SOURCES`` is absent, the single-channel
variables behave exactly as before.

Default-deny rules
------------------

* ``NOTIFY_INBOUND_ALLOWED_AUTHORS`` is required in single-channel mode.  A
  source without its own ``allowed_authors`` inherits it, and a source with
  neither refuses to start.  An empty or missing allowlist never means "allow
  everyone".
* Only the configured channel of each source is read, and every returned
  message must carry that same ``channel_id``.
* Bot and webhook messages are ignored, including the notification bot's own
  messages.  Only user messages and replies (types 0 and 19) are considered.
* Message IDs are recorded in a small state file before an instruction is
  queued, so a restart or an overlapping poll cannot replay it.  Each source
  has its own state file.  The saved cursor also acts as the first-poll
  baseline: pre-existing messages, including old notifications, are never
  actioned.
* Instructions are capped after mention removal; over-long messages are
  refused rather than truncated.
* One worker thread runs the agent.  A bounded queue absorbs bursts; messages
  beyond it are dropped and logged.  Two agents never race.

Everything that could carry a secret is redacted from logs: the API key,
webhook URL, channel id, author ids and the operator's message body are never
printed.  Message ids are logged only as a short hash.  The agent's own output
is not printed either: it goes to a private, size-capped capture file under the
state directory, and only that path is logged.  The capture is treated as
sensitive (it can echo model and tool output) and is never shipped to the
container log.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

# The capture helpers live beside this script.  They are imported directly so
# the listener and run-prompt.sh share one transcript format.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import agent_capture  # noqa: E402  (path set up above)

LOG = logging.getLogger("discord-inbound")

COMPOSIO_EXECUTE_PATH = "/api/v3.1/tools/execute"
READ_MESSAGES_TOOL = "DISCORDBOT_LIST_MESSAGES"
REPLY_TOOL = "DISCORDBOT_CREATE_MESSAGE"
DEFAULT_API_BASE = "https://backend.composio.dev"
DEFAULT_POLL_SECONDS = 30.0
MIN_POLL_SECONDS = 5.0
DEFAULT_MAX_INSTRUCTION_CHARS = 1_000
MAX_INSTRUCTION_CHARS = 2_000
DEFAULT_QUEUE_SIZE = 4
MAX_QUEUE_SIZE = 100
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0
MIN_RUN_TIMEOUT_SECONDS = 30.0
MAX_RUN_TIMEOUT_SECONDS = 3_600.0
DEFAULT_STATE_FILE = "/home/agent/.local/share/opencode/discord-inbound-state.json"
AGENT_LOG_NAME = agent_capture.DEBUG_LOG_NAME
AGENT_LOG_MAX_BYTES = agent_capture.MAX_BYTES
SEND_MARKER_PREFIX = ".discord-inbound-send-"
STATE_VERSION = 1
SEEN_ID_LIMIT = 500
MESSAGE_PAGE_LIMIT = 100
MAX_PAGES_PER_POLL = 20
HTTP_TIMEOUT_SECONDS = 30
MAX_TOOL_RESPONSE_BYTES = 2 * 1024 * 1024
DISCORD_EPOCH_MS = 1_420_070_400_000
SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$")

# Discord message types that can carry a human instruction.  Type 19 is a
# reply, which is exactly how the captain answers a notification.
ACCEPTED_MESSAGE_TYPES = frozenset({0, 19})

TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BOOK_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
TOKEN_SUFFIX_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
SNOWFLAKE_RE = re.compile(r"^[0-9]{1,32}$")
MENTION_RE = re.compile(r"<@[!&]?[0-9]+>|<#[0-9]+>")
EVERYONE_RE = re.compile(r"(?<![\w])@(everyone|here)\b", re.IGNORECASE)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SPACE_RE = re.compile(r"[ \t]+")
NEWLINE_RE = re.compile(r"\n{3,}")
SCOPE_FORBIDDEN_RE = re.compile(r"[`\r\n]")
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")


class ConfigError(ValueError):
    """The listener cannot start safely with this configuration."""


class StateError(RuntimeError):
    """The dedupe/baseline state could not be read or written."""


class FetchError(RuntimeError):
    """A poll failed.  The cursor is deliberately left unchanged."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the API key is never sent to another host."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open_request(request: urllib.request.Request) -> Any:
    return _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class InboundTarget:
    """The named Composio target used to send the answer back."""

    name: str
    channel_id: str
    connected_account_id: str
    host_scope: str
    allow_content: bool
    allow_runbooks: bool
    bookstack_url: str
    bookstack_books: str
    bookstack_token: str


@dataclasses.dataclass(frozen=True)
class InboundSource:
    """One polled channel with its own account, allowlist and state."""

    name: str
    channel_id: str
    connected_account_id: str
    allowed_authors: frozenset[str]
    target: InboundTarget | None
    reply_mode: str  # "target" | "bot" | "legacy" | "webhook"
    state_file: Path
    max_instruction_chars: int


@dataclasses.dataclass(frozen=True)
class Config:
    api_base: str
    api_key: str = dataclasses.field(repr=False)
    sources: tuple[InboundSource, ...]
    # Aliases for the first source, kept so single-channel callers and the
    # existing tests keep their shape.  Multi-source code uses ``sources``.
    channel_id: str
    allowed_authors: frozenset[str]
    connected_account_id: str
    user_id: str
    target: InboundTarget | None
    reply_mode: str
    poll_seconds: float
    max_instruction_chars: int
    queue_size: int
    run_timeout_seconds: float
    state_file: Path
    opencode_model: str
    opencode_bin: str
    delivery: str
    webhook_mode: bool
    opencode_model_fallback: str | None


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _positive_float(
    env: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number") from None
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _positive_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw, 10)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not (minimum <= value <= maximum):
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def parse_allowlist(raw: str) -> frozenset[str]:
    """Parse the author allowlist.

    The allowlist is the authorisation boundary, so anything ambiguous is a
    hard failure rather than a skip.  An empty value, an empty entry (for
    example ``1,,2``), or a non-numeric Discord id refuses to start.
    """

    value = (raw or "").strip()
    if not value:
        raise ConfigError(
            "NOTIFY_INBOUND_ALLOWED_AUTHORS is required; an empty allowlist "
            "would accept every author"
        )
    authors: set[str] = set()
    for index, part in enumerate(value.split(","), start=1):
        author = part.strip()
        if not author:
            raise ConfigError(
                f"NOTIFY_INBOUND_ALLOWED_AUTHORS entry {index} is empty"
            )
        if not SNOWFLAKE_RE.match(author):
            raise ConfigError(
                f"NOTIFY_INBOUND_ALLOWED_AUTHORS entry {index} is not a "
                "numeric Discord id"
            )
        authors.add(author)
    if not authors:
        raise ConfigError("NOTIFY_INBOUND_ALLOWED_AUTHORS is empty")
    return frozenset(authors)


def _format_host_scope(scope: object) -> str:
    if isinstance(scope, str):
        parts = [scope]
    elif isinstance(scope, list):
        parts = scope
    else:
        raise ConfigError(
            "target host_scope must be a non-empty string or an array of "
            "non-empty strings"
        )
    if not parts:
        raise ConfigError("target host_scope must not be empty")
    formatted: list[str] = []
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            raise ConfigError(
                "target host_scope must contain only non-empty strings"
            )
        if SCOPE_FORBIDDEN_RE.search(part):
            raise ConfigError(
                "target host_scope must not contain backticks or newlines"
            )
        formatted.append(f"`{part.strip()}`")
    return ", ".join(formatted)


def _format_bookstack(
    entry: Mapping[str, Any], name: str
) -> tuple[str, str, str]:
    """Validate a target's optional BookStack instance settings.

    The URL must be https, the books value a non-empty comma-separated list of
    ids or slugs, and the token a suffix naming the credential pair in
    ``BOOKSTACK_TOKEN_ID_<SUFFIX>`` / ``BOOKSTACK_TOKEN_SECRET_<SUFFIX>``.
    Errors name the target only, never the value.
    """

    raw = entry.get("bookstack")
    if raw is None:
        return "", "", ""
    if not isinstance(raw, dict):
        raise ConfigError(f"target {name!r} bookstack must be an object")

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ConfigError(f"target {name!r} bookstack.url must be an https URL")
    url = url.strip()
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.netloc
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise ConfigError(f"target {name!r} bookstack.url must be an https URL")

    books = raw.get("books")
    if not isinstance(books, str) or not books.strip():
        raise ConfigError(
            f"target {name!r} bookstack.books must be a comma-separated list"
        )
    entries = [part.strip() for part in books.split(",") if part.strip()]
    if not entries or len(entries) > 16:
        raise ConfigError(
            f"target {name!r} bookstack.books must name between 1 and 16 books"
        )
    normalised: list[str] = []
    for part in entries:
        lowered = part.lower()
        if lowered.isdigit():
            if int(lowered, 10) <= 0:
                raise ConfigError(
                    f"target {name!r} bookstack.books contains an invalid entry"
                )
        elif not BOOK_SLUG_RE.match(lowered):
            raise ConfigError(
                f"target {name!r} bookstack.books contains an invalid entry"
            )
        normalised.append(lowered)

    token = raw.get("token")
    if not isinstance(token, str) or not TOKEN_SUFFIX_RE.match(token):
        raise ConfigError(
            f"target {name!r} bookstack.token must be a suffix of letters, "
            "digits and underscore"
        )
    return url, ",".join(normalised), token


def _load_target_by_name(
    env: Mapping[str, str],
    name: str,
    channel_id: str,
    *,
    expected_account: str = "",
    label: str = "NOTIFY_INBOUND_TARGET",
) -> InboundTarget:
    """Resolve one named target and tie it to the inbound channel."""

    if (env.get("DISCORD_WEBHOOK_URL") or "").strip():
        # notify.sh rejects -t (and even NOTIFY_DEFAULT_TARGET) when a webhook
        # is set, so a named target here could only fail at reply time.
        raise ConfigError(
            f"{label} {name!r} cannot be used with DISCORD_WEBHOOK_URL; "
            "a webhook has no named targets"
        )

    if not TARGET_NAME_RE.match(name):
        raise ConfigError(
            f"{label} may contain only letters, digits, dot, underscore and "
            "hyphen"
        )

    raw_targets = (env.get("NOTIFY_TARGETS") or "").strip()
    if not raw_targets:
        raise ConfigError(
            f"{label} names target {name!r}, but NOTIFY_TARGETS is not set"
        )
    try:
        targets = json.loads(raw_targets)
    except json.JSONDecodeError:
        raise ConfigError("NOTIFY_TARGETS must be a JSON object") from None
    if not isinstance(targets, dict):
        raise ConfigError("NOTIFY_TARGETS must be a JSON object")
    entry = targets.get(name)
    if not isinstance(entry, dict):
        raise ConfigError(
            f"{label} {name!r} is not defined in NOTIFY_TARGETS"
        )

    target_channel = entry.get("channel_id")
    if not isinstance(target_channel, str) or not SNOWFLAKE_RE.match(
        target_channel.strip()
    ):
        raise ConfigError(f"target {name!r} needs a numeric channel_id")
    if int(target_channel.strip()) != int(channel_id):
        raise ConfigError(
            f"target {name!r} channel_id does not match the inbound channel; "
            "refusing to send replies to a different channel"
        )

    target_account = entry.get("connected_account_id", "")
    if target_account is None:
        target_account = ""
    if not isinstance(target_account, str):
        raise ConfigError(
            f"target {name!r} connected_account_id must be a string"
        )
    target_account = target_account.strip()
    if target_account and not ACCOUNT_ID_RE.match(target_account):
        raise ConfigError(
            f"target {name!r} connected_account_id contains invalid characters"
        )
    if expected_account and target_account and target_account != expected_account:
        raise ConfigError(
            f"target {name!r} connected_account_id does not match the "
            "inbound source's connected_account_id; the same bot must read "
            "and send"
        )
    if not target_account:
        # The read connection is authoritative: fill it in so notify.sh
        # cannot fall back to an unrelated COMPOSIO_CONNECTED_ACCOUNT_ID.
        target_account = expected_account

    host_scope = ""
    if "host_scope" in entry:
        host_scope = _format_host_scope(entry["host_scope"])

    allow_content = True
    if "allow_content" in entry:
        if not isinstance(entry["allow_content"], bool):
            raise ConfigError(
                f"target {name!r} allow_content must be a boolean"
            )
        allow_content = entry["allow_content"]

    # Runbooks are default-deny and independent of allow_content: only a
    # target that names this flag gets them.
    allow_runbooks = False
    if "allow_runbooks" in entry:
        if not isinstance(entry["allow_runbooks"], bool):
            raise ConfigError(
                f"target {name!r} allow_runbooks must be a boolean"
            )
        allow_runbooks = entry["allow_runbooks"]

    bookstack_url, bookstack_books, bookstack_token = _format_bookstack(entry, name)

    return InboundTarget(
        name=name,
        channel_id=target_channel.strip(),
        connected_account_id=target_account,
        host_scope=host_scope,
        allow_content=allow_content,
        allow_runbooks=allow_runbooks,
        bookstack_url=bookstack_url,
        bookstack_books=bookstack_books,
        bookstack_token=bookstack_token,
    )


def _load_target(
    env: Mapping[str, str],
    channel_id: str,
) -> InboundTarget | None:
    """Resolve the named target used for the single-channel reply, if any."""

    name = (env.get("NOTIFY_INBOUND_TARGET") or "").strip()
    if not name:
        name = (env.get("NOTIFY_DEFAULT_TARGET") or "").strip()
    if not name:
        return None
    return _load_target_by_name(env, name, channel_id, label="NOTIFY_INBOUND_TARGET")


def _resolve_read_account(
    env: Mapping[str, str],
    target: InboundTarget | None,
) -> str:
    explicit = (env.get("NOTIFY_INBOUND_CONNECTED_ACCOUNT_ID") or "").strip()
    target_account = target.connected_account_id if target is not None else ""
    default_account = (env.get("COMPOSIO_CONNECTED_ACCOUNT_ID") or "").strip()

    if explicit and target_account and explicit != target_account:
        raise ConfigError(
            "NOTIFY_INBOUND_CONNECTED_ACCOUNT_ID does not match the inbound "
            "target's connected_account_id; the same bot must read and send"
        )
    if explicit and not ACCOUNT_ID_RE.match(explicit):
        raise ConfigError(
            "NOTIFY_INBOUND_CONNECTED_ACCOUNT_ID contains invalid characters"
        )
    return explicit or target_account or default_account


def _which_opencode(env: Mapping[str, str]) -> str:
    requested = (env.get("OPENCODE_BIN") or "opencode").strip()
    if not requested:
        requested = "opencode"
    found = shutil.which(requested)
    if not found:
        raise ConfigError(
            "opencode is not installed or not on PATH (set OPENCODE_BIN to "
            "the binary if it is elsewhere)"
        )
    return found


def _source_state_path(base: Path, name: str) -> Path:
    """Return the per-source state file derived from the configured path."""

    return base.with_name(f"{base.stem}.{name}{base.suffix}")


def _load_sources(
    env: Mapping[str, str],
    *,
    webhook_mode: bool,
    global_authors: frozenset[str],
    max_chars: int,
    state_path: Path,
) -> tuple[tuple[InboundSource, ...], str]:
    """Resolve the configured inbound sources and their reply modes."""

    raw_sources = (env.get("NOTIFY_INBOUND_SOURCES") or "").strip()
    if not raw_sources:
        if not global_authors:
            raise ConfigError(
                "NOTIFY_INBOUND_ALLOWED_AUTHORS is required; an empty "
                "allowlist would accept every author"
            )
        channel_id = _required(env, "NOTIFY_INBOUND_CHANNEL_ID")
        if not SNOWFLAKE_RE.match(channel_id):
            raise ConfigError(
                "NOTIFY_INBOUND_CHANNEL_ID must be a numeric Discord id"
            )

        target = _load_target(env, channel_id)
        account = _resolve_read_account(env, target)
        if not webhook_mode and not (env.get("COMPOSIO_TOOL") or "").strip():
            raise ConfigError(
                "no delivery configured: set DISCORD_WEBHOOK_URL, or "
                "COMPOSIO_TOOL for the Composio path"
            )
        if target is not None:
            reply_mode = "target"
            delivery = "composio-target"
        elif webhook_mode:
            reply_mode = "webhook"
            delivery = "discord-webhook"
        else:
            reply_mode = "legacy"
            delivery = "composio"

        source = InboundSource(
            name="default",
            channel_id=channel_id,
            connected_account_id=account,
            allowed_authors=global_authors,
            target=target,
            reply_mode=reply_mode,
            state_file=state_path,
            max_instruction_chars=max_chars,
        )
        return (source,), delivery

    if webhook_mode:
        raise ConfigError(
            "NOTIFY_INBOUND_SOURCES cannot be combined with "
            "DISCORD_WEBHOOK_URL; each source replies through its own target "
            "or through the bot"
        )

    try:
        entries = json.loads(raw_sources)
    except json.JSONDecodeError:
        raise ConfigError("NOTIFY_INBOUND_SOURCES must be a JSON array") from None
    if not isinstance(entries, list) or not entries:
        raise ConfigError("NOTIFY_INBOUND_SOURCES must be a non-empty JSON array")

    sources: list[InboundSource] = []
    names: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        label = f"NOTIFY_INBOUND_SOURCES entry {index}"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be an object")

        name = entry.get("name")
        if not isinstance(name, str) or not SOURCE_NAME_RE.match(name.strip()):
            raise ConfigError(
                f"{label} needs a name of 1-64 letters, digits, dot, "
                "underscore or hyphen"
            )
        name = name.strip()
        if name in names:
            raise ConfigError(f"{label} repeats source name {name!r}")
        names.add(name)

        account = entry.get("connected_account_id")
        if not isinstance(account, str) or not ACCOUNT_ID_RE.match(
            account.strip()
        ):
            raise ConfigError(
                f"{label} ({name!r}) needs a connected_account_id"
            )
        account = account.strip()

        channel_id = entry.get("channel_id")
        if not isinstance(channel_id, str) or not SNOWFLAKE_RE.match(
            channel_id.strip()
        ):
            raise ConfigError(f"{label} ({name!r}) needs a numeric channel_id")
        channel_id = channel_id.strip()

        if "allowed_authors" in entry:
            raw_authors = entry["allowed_authors"]
            if not isinstance(raw_authors, str):
                raise ConfigError(
                    f"{label} ({name!r}) allowed_authors must be a "
                    "comma-separated string of Discord user ids"
                )
            try:
                authors = parse_allowlist(raw_authors)
            except ConfigError as error:
                raise ConfigError(f"{label} ({name!r}): {error}") from None
        else:
            authors = global_authors
        if not authors:
            raise ConfigError(
                f"{label} ({name!r}) has no allowed_authors and "
                "NOTIFY_INBOUND_ALLOWED_AUTHORS is empty"
            )

        target_name = entry.get("target")
        if target_name is None:
            target = None
            reply_mode = "bot"
        else:
            if not isinstance(target_name, str) or not target_name.strip():
                raise ConfigError(
                    f"{label} ({name!r}) target must be a non-empty string"
                )
            target = _load_target_by_name(
                env,
                target_name.strip(),
                channel_id,
                expected_account=account,
                label=f"{label} ({name!r}) target",
            )
            reply_mode = "target"

        sources.append(
            InboundSource(
                name=name,
                channel_id=channel_id,
                connected_account_id=account,
                allowed_authors=authors,
                target=target,
                reply_mode=reply_mode,
                state_file=_source_state_path(state_path, name),
                max_instruction_chars=max_chars,
            )
        )

    if any(source.reply_mode == "target" for source in sources) and not (
        env.get("COMPOSIO_TOOL") or ""
    ).strip():
        raise ConfigError(
            "a source names a target, but COMPOSIO_TOOL is not set; notify.sh "
            "needs the tool slug for the target reply path (for example "
            "DISCORDBOT_CREATE_MESSAGE)"
        )

    if len(sources) == 1:
        delivery = (
            "composio-target"
            if sources[0].reply_mode == "target"
            else "composio-bot"
        )
    else:
        delivery = "multi-source"
    return tuple(sources), delivery


def load_config(environ: Mapping[str, str]) -> Config:
    """Validate environment configuration and return a frozen Config.

    This is deliberately strict.  Every caller of this function is a process
    start, so a missing or ambiguous value should stop the listener before it
    reads a single message, not fail open later.
    """

    env = {key: value for key, value in environ.items()}

    api_key = _required(env, "COMPOSIO_API_KEY")
    api_base = (env.get("COMPOSIO_API_BASE") or DEFAULT_API_BASE).strip()
    if not api_base.startswith("https://"):
        raise ConfigError("COMPOSIO_API_BASE must be an https:// URL")
    api_base = api_base.rstrip("/")

    webhook_mode = bool((env.get("DISCORD_WEBHOOK_URL") or "").strip())
    if webhook_mode:
        if not (env.get("DISCORD_WEBHOOK_URL") or "").strip().startswith("https://"):
            raise ConfigError("DISCORD_WEBHOOK_URL must be an https:// URL")

    raw_global_authors = (env.get("NOTIFY_INBOUND_ALLOWED_AUTHORS") or "").strip()
    if raw_global_authors:
        global_authors = parse_allowlist(raw_global_authors)
    else:
        global_authors = frozenset()

    user_id = (env.get("COMPOSIO_USER_ID") or "").strip()

    model = _required(env, "OPENCODE_MODEL")
    if any(character.isspace() for character in model):
        raise ConfigError("OPENCODE_MODEL must not contain whitespace")

    fallback_raw = (env.get("OPENCODE_MODEL_FALLBACK") or "").strip()
    if fallback_raw and any(character.isspace() for character in fallback_raw):
        raise ConfigError(
            "OPENCODE_MODEL_FALLBACK must not contain whitespace"
        )
    opencode_model_fallback = fallback_raw or None

    opencode_bin = _which_opencode(env)

    state_raw = (env.get("NOTIFY_INBOUND_STATE_FILE") or DEFAULT_STATE_FILE).strip()
    if not state_raw:
        state_raw = DEFAULT_STATE_FILE
    state_path = Path(state_raw).expanduser()
    if not state_path.is_absolute():
        raise ConfigError("NOTIFY_INBOUND_STATE_FILE must be an absolute path")
    _check_state_path(state_path)

    poll_seconds = _positive_float(
        env,
        "NOTIFY_INBOUND_POLL_SECONDS",
        DEFAULT_POLL_SECONDS,
        MIN_POLL_SECONDS,
        3_600.0,
    )
    max_chars = _positive_int(
        env,
        "NOTIFY_INBOUND_MAX_INSTRUCTION_CHARS",
        DEFAULT_MAX_INSTRUCTION_CHARS,
        1,
        MAX_INSTRUCTION_CHARS,
    )
    queue_size = _positive_int(
        env,
        "NOTIFY_INBOUND_QUEUE_SIZE",
        DEFAULT_QUEUE_SIZE,
        1,
        MAX_QUEUE_SIZE,
    )
    run_timeout = _positive_float(
        env,
        "NOTIFY_INBOUND_RUN_TIMEOUT_SECONDS",
        DEFAULT_RUN_TIMEOUT_SECONDS,
        MIN_RUN_TIMEOUT_SECONDS,
        MAX_RUN_TIMEOUT_SECONDS,
    )

    sources, delivery = _load_sources(
        env,
        webhook_mode=webhook_mode,
        global_authors=global_authors,
        max_chars=max_chars,
        state_path=state_path,
    )
    first = sources[0]

    return Config(
        api_base=api_base,
        api_key=api_key,
        sources=sources,
        channel_id=first.channel_id,
        allowed_authors=first.allowed_authors,
        connected_account_id=first.connected_account_id,
        user_id=user_id,
        target=first.target,
        reply_mode=first.reply_mode,
        poll_seconds=poll_seconds,
        max_instruction_chars=max_chars,
        queue_size=queue_size,
        run_timeout_seconds=run_timeout,
        state_file=state_path,
        opencode_model=model,
        opencode_bin=opencode_bin,
        delivery=delivery,
        webhook_mode=webhook_mode,
        opencode_model_fallback=opencode_model_fallback,
    )


def _check_state_path(path: Path) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise ConfigError(
            "NOTIFY_INBOUND_STATE_FILE parent directory does not exist"
        )
    probe = None
    try:
        fd, probe = tempfile.mkstemp(
            dir=str(parent), prefix=".discord-inbound-probe-"
        )
        os.close(fd)
    except OSError:
        raise ConfigError(
            "NOTIFY_INBOUND_STATE_FILE parent directory is not writable"
        ) from None
    finally:
        if probe is not None:
            try:
                os.unlink(probe)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Message filtering
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Decision:
    accepted: bool
    reason: str
    text: str
    message_id: str


def strip_mentions(text: str) -> str:
    """Remove Discord mention syntax and control characters.

    Mentions are not part of an instruction and must never reach the agent or
    its outgoing message.  ``notify.sh`` also sends ``allowed_mentions: []``,
    but stripping here means a mention cannot be used to steer the agent or
    ping someone through a second path.
    """

    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    normalised = CONTROL_RE.sub("", normalised)
    normalised = MENTION_RE.sub(" ", normalised)
    normalised = EVERYONE_RE.sub(" ", normalised)
    normalised = SPACE_RE.sub(" ", normalised)
    normalised = NEWLINE_RE.sub("\n\n", normalised)
    return normalised.strip()


def evaluate_message(
    config: Config,
    message: object,
    is_seen: Callable[[str], bool],
) -> Decision:
    """Decide whether one raw Discord message may become an instruction.

    The checks are ordered so a bot or a wrong-channel message is refused
    before its text is inspected, and a duplicate is refused before any work.
    The returned reason is a fixed token; it is safe to log.
    """

    if not isinstance(message, dict):
        return Decision(False, "malformed_message", "", "")

    message_id = message.get("id")
    if not isinstance(message_id, str) or not SNOWFLAKE_RE.match(message_id):
        return Decision(False, "malformed_message", "", "")

    if is_seen(message_id):
        return Decision(False, "duplicate", "", message_id)

    author = message.get("author")
    if not isinstance(author, dict):
        return Decision(False, "malformed_message", "", message_id)

    if author.get("bot") is True or message.get("webhook_id"):
        return Decision(False, "bot_author", "", message_id)

    author_id = author.get("id")
    if not isinstance(author_id, str) or not SNOWFLAKE_RE.match(author_id):
        return Decision(False, "malformed_message", "", message_id)

    if author_id not in config.allowed_authors:
        return Decision(False, "author_not_allowed", "", message_id)

    if message.get("channel_id") != config.channel_id:
        return Decision(False, "wrong_channel", "", message_id)

    message_type = message.get("type", 0)
    if isinstance(message_type, bool) or not isinstance(message_type, int):
        return Decision(False, "malformed_message", "", message_id)
    if message_type not in ACCEPTED_MESSAGE_TYPES:
        return Decision(False, "unsupported_type", "", message_id)

    content = message.get("content")
    if not isinstance(content, str):
        return Decision(False, "empty", "", message_id)

    text = strip_mentions(content)
    if not text:
        return Decision(False, "empty", "", message_id)
    if len(text) > config.max_instruction_chars:
        return Decision(False, "too_long", "", message_id)

    return Decision(True, "accepted", text, message_id)


def short_id(message_id: str) -> str:
    if not message_id:
        return "-"
    return hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:8]


def snowflake_at(epoch_seconds: float) -> str:
    """Build a Discord-shaped snowflake for a point in time.

    Discord paginates by snowflake position, not by message existence, so a
    synthetic snowflake is a safe baseline when the channel looks empty.  It
    also protects the missing-permission case: Discord returns an empty list
    when the bot lacks Read Message History, and a later grant must not make
    the listener action messages that predate the baseline.
    """

    milliseconds = max(0, int(epoch_seconds * 1000) - DISCORD_EPOCH_MS)
    return str(milliseconds << 22)


# ---------------------------------------------------------------------------
# Durable baseline and dedupe
# ---------------------------------------------------------------------------


class SeenStore:
    """Persist the cursor and recently seen message ids.

    The file is written atomically and mode 0600.  A message is recorded
    before it is queued, which gives at-most-once semantics: a crash can lose
    a queued instruction, but it cannot replay one.
    """

    def __init__(self, path: Path, limit: int = SEEN_ID_LIMIT) -> None:
        self.path = Path(path)
        self.limit = limit
        self._order: collections.deque[str] = collections.deque()
        self._seen: set[str] = set()
        self.cursor = ""
        self.initialized = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            raise StateError("could not read the inbound state file") from None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise StateError(
                "the inbound state file is not valid JSON; refusing to guess "
                "which messages were already handled"
            ) from None
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise StateError(
                "the inbound state file has an unsupported format; refusing "
                "to guess which messages were already handled"
            )
        cursor = data.get("cursor", "")
        initialized = data.get("initialized", False)
        seen = data.get("seen", [])
        if not isinstance(cursor, str):
            raise StateError("the inbound state file has an invalid cursor")
        if cursor and not SNOWFLAKE_RE.match(cursor):
            raise StateError("the inbound state file has an invalid cursor")
        if not isinstance(initialized, bool):
            raise StateError("the inbound state file has an invalid baseline flag")
        if not isinstance(seen, list):
            raise StateError("the inbound state file has an invalid seen list")
        for item in seen:
            if not isinstance(item, str) or not SNOWFLAKE_RE.match(item):
                raise StateError("the inbound state file has an invalid message id")
        self.cursor = cursor
        self.initialized = initialized
        for item in seen[-self.limit :]:
            self._remember(item)

    def _remember(self, message_id: str) -> None:
        if message_id in self._seen:
            return
        self._seen.add(message_id)
        self._order.append(message_id)
        while len(self._order) > self.limit:
            old = self._order.popleft()
            self._seen.discard(old)

    def is_seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def _snapshot(self) -> tuple[list[str], str, bool]:
        return list(self._order), self.cursor, self.initialized

    def _restore(self, snapshot: tuple[list[str], str, bool]) -> None:
        order, cursor, initialized = snapshot
        self._order = collections.deque(order)
        self._seen = set(order)
        self.cursor = cursor
        self.initialized = initialized

    def _save(self) -> None:
        parent = self.path.parent
        payload = {
            "version": STATE_VERSION,
            "initialized": self.initialized,
            "cursor": self.cursor,
            "seen": list(self._order),
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        fd = -1
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(
                dir=str(parent), prefix=".discord-inbound-state-", suffix=".tmp"
            )
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except OSError:
            raise StateError("could not write the inbound state file") from None
        finally:
            if fd != -1:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _advance_cursor(self, message_id: str) -> bool:
        if not message_id:
            return False
        if not self.cursor or int(message_id) > int(self.cursor):
            self.cursor = message_id
            return True
        return False

    def record(self, message_id: str) -> None:
        """Remember one message id and advance the cursor, atomically."""

        if not SNOWFLAKE_RE.match(message_id):
            raise StateError("refusing to record an invalid message id")
        snapshot = self._snapshot()
        changed = message_id not in self._seen
        self._remember(message_id)
        changed = self._advance_cursor(message_id) or changed
        if not changed:
            return
        try:
            self._save()
        except StateError:
            self._restore(snapshot)
            raise

    def initialize(self, message_ids: Iterable[str], cursor: str) -> None:
        """Set the first-poll baseline without actioning any pre-existing message."""

        ids = list(message_ids)
        for message_id in ids:
            if not SNOWFLAKE_RE.match(message_id):
                raise StateError("refusing to baseline an invalid message id")
        if cursor and not SNOWFLAKE_RE.match(cursor):
            raise StateError("refusing to baseline an invalid cursor")
        snapshot = self._snapshot()
        self.initialized = True
        self.cursor = cursor
        for message_id in ids:
            self._remember(message_id)
        try:
            self._save()
        except StateError:
            self._restore(snapshot)
            raise


# ---------------------------------------------------------------------------
# Composio tool execution polling
# ---------------------------------------------------------------------------


def _execute_tool(
    config: Config,
    tool: str,
    arguments: Mapping[str, Any],
    connected_account_id: str,
) -> object:
    """Execute one Composio tool and return its ``data`` field.

    The bot toolkit does not support the proxy endpoint, so reads go through
    normal tool execution.  Only the HTTP status and a fixed failure token are
    reported: a response body can echo arguments, message content or ids.
    """

    body: dict[str, Any] = {"arguments": dict(arguments)}
    if connected_account_id:
        body["connected_account_id"] = connected_account_id
    if config.user_id:
        body["user_id"] = config.user_id

    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{config.api_base}{COMPOSIO_EXECUTE_PATH}/{tool}",
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": config.api_key,
            "User-Agent": "notify-agent-discord-inbound/2",
        },
    )

    try:
        with _open_request(request) as response:
            raw = response.read(MAX_TOOL_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        # Never read or log the response body: it can echo request data.
        raise FetchError(f"tool execution returned HTTP {error.code}") from None
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, str) and reason:
            raise FetchError(f"tool request failed: {reason}") from None
        raise FetchError("tool request failed") from None
    except (TimeoutError, OSError):
        raise FetchError("tool request failed or timed out") from None

    if len(raw) > MAX_TOOL_RESPONSE_BYTES:
        raise FetchError("tool response was too large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FetchError("tool response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise FetchError("tool response had an unexpected shape")

    if payload.get("successful") is not True:
        raise FetchError("tool execution was not successful")
    if "data" not in payload:
        raise FetchError("tool response had no data")
    return payload.get("data")


def _resolve_source(
    config: Config, source: InboundSource | None
) -> InboundSource:
    return source if source is not None else config.sources[0]


def fetch_messages(
    config: Config,
    *,
    before: str | None = None,
    limit: int = MESSAGE_PAGE_LIMIT,
    source: InboundSource | None = None,
) -> list[dict[str, Any]]:
    chosen = _resolve_source(config, source)
    arguments: dict[str, Any] = {
        "channel_id": chosen.channel_id,
        "limit": limit,
    }
    if before:
        arguments["before"] = before
    data = _execute_tool(
        config,
        READ_MESSAGES_TOOL,
        arguments,
        chosen.connected_account_id,
    )
    if not isinstance(data, dict):
        raise FetchError("tool response did not contain a message list")
    raw_messages = data.get("messages")
    if not isinstance(raw_messages, list):
        raise FetchError("tool response did not contain a message list")

    messages: list[dict[str, Any]] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            raise FetchError("tool response contained a malformed message")
        message_id = item.get("id")
        if not isinstance(message_id, str) or not SNOWFLAKE_RE.match(message_id):
            raise FetchError("tool response contained a message without an id")
        messages.append(item)
    return messages


def fetch_new_messages(
    config: Config,
    store: SeenStore,
    source: InboundSource | None = None,
) -> list[dict[str, Any]]:
    """Return new messages oldest-first, or establish the first-poll baseline.

    Discord returns messages newest-first.  ``after`` would return the newest
    page after the cursor, silently skipping older messages in a backlog, so
    the listener walks backwards with ``before`` until it reaches the cursor,
    then reverses the collected pages.  The page cap bounds memory on a very
    long outage; when it is hit the gap is logged and the cursor moves on.
    """

    chosen = _resolve_source(config, source)

    if not store.initialized:
        # A single newest message is enough: the baseline is "ignore anything
        # that already exists", including the bot's own past notifications.
        page = (
            fetch_messages(config, limit=1)
            if source is None
            else fetch_messages(config, limit=1, source=source)
        )
        ids = [item["id"] for item in page]
        if ids:
            cursor = max(ids, key=int)
        else:
            # An empty list can mean an empty channel or a bot without Read
            # Message History.  A synthetic now-snowflake is a safe baseline
            # either way: every pre-existing message is older, so a later
            # permission grant cannot make old history look new.
            cursor = snowflake_at(time.time())
            LOG.warning(
                "source=%s returned no readable messages; using a time-based "
                "baseline. Grant View Channel and Read Message History if the "
                "channel is not empty.",
                chosen.name,
            )
        store.initialize(ids, cursor)
        LOG.info(
            "baseline established: source=%s ignored=%d cursor=%s",
            chosen.name,
            len(ids),
            short_id(cursor),
        )
        return []

    cursor = store.cursor
    collected: list[dict[str, Any]] = []
    before: str | None = None
    pages = 0
    truncated = False

    while True:
        if pages >= MAX_PAGES_PER_POLL:
            truncated = True
            break
        if source is None:
            page = fetch_messages(
                config, before=before, limit=MESSAGE_PAGE_LIMIT
            )
        else:
            page = fetch_messages(
                config,
                before=before,
                limit=MESSAGE_PAGE_LIMIT,
                source=source,
            )
        pages += 1
        if not page:
            break
        collected.extend(page)
        page_ids = [item["id"] for item in page]
        oldest = min(page_ids, key=int)
        if cursor and int(oldest) <= int(cursor):
            break
        if len(page) < MESSAGE_PAGE_LIMIT:
            break
        before = oldest

    by_id: dict[str, dict[str, Any]] = {}
    for item in collected:
        message_id = item["id"]
        if cursor and int(message_id) <= int(cursor):
            continue
        by_id[message_id] = item

    if truncated and by_id:
        LOG.warning(
            "source=%s backlog truncated at %d pages; messages older than the "
            "oldest returned page are skipped",
            chosen.name,
            MAX_PAGES_PER_POLL,
        )

    return [by_id[key] for key in sorted(by_id, key=int)]


# ---------------------------------------------------------------------------
# Agent invocation and the single-run worker
# ---------------------------------------------------------------------------


def _bookstack_credentials(target: InboundTarget | None) -> tuple[str, str]:
    """Resolve the target's named credential pair from the listener env.

    The suffix is validated at config load, so it is safe to use in an
    environment variable name. There is no global-token fallback.
    """

    if target is None or not target.bookstack_token:
        return "", ""
    suffix = target.bookstack_token.upper()
    return (
        (os.environ.get(f"BOOKSTACK_TOKEN_ID_{suffix}") or "").strip(),
        (os.environ.get(f"BOOKSTACK_TOKEN_SECRET_{suffix}") or "").strip(),
    )


def build_instruction(
    source: Config | InboundSource,
    operator_message: str,
) -> str:
    lines = [
        "You are answering an inbound reply from the operator in the admin chat.",
        "",
        "The message below is the operator's request. Answer it directly and "
        "briefly; do not compose a general status digest or recap the "
        "notification it replies to. Use only the tools you are allowed to "
        "use. If the request cannot be answered with them, decline in one "
        "short sentence and stop.",
        "",
        "Never describe or hint at your own capabilities, tools, data sources, "
        "limits or configuration. Do not describe anything as off, withheld or "
        "unavailable, and do not say what you can only do.",
        "",
        "Operator message:",
        operator_message,
        "",
    ]
    target = source.target
    if target is not None:
        lines.append(
            "Send exactly one reply with:\n"
            f'/app/scripts/notify.sh -t {target.name} -m "<message>"'
        )
        if target.host_scope:
            lines.append(
                "\nThe delivery scope for this run is "
                f"{target.host_scope}. Nothing about any other host may "
                "appear in the reply: report only on the scoped host, and if "
                "there is no data for it, say that plainly instead of using "
                "another host's numbers."
            )
        token_id, token_secret = _bookstack_credentials(target)
        runbooks_enabled = bool(
            target.allow_runbooks
            and target.bookstack_url
            and target.bookstack_books
            and token_id
            and token_secret
        )
        if not target.allow_content:
            restriction = "Report host data"
            if runbooks_enabled:
                restriction += " and runbook procedures"
            restriction += " only: do not include any other material."
            capabilities = (
                "\nIf the operator asks what you can do, answer with what this "
                "run allows: host health (CPU, memory, load, disk, sample "
                "freshness) and SSH reachability for "
                f"{target.host_scope or 'the hosts in scope'}"
            )
            if runbooks_enabled:
                capabilities += ", and runbook procedures for operational tasks."
            else:
                capabilities += "."
            lines.append(f"\n{restriction}\n{capabilities}")
        else:
            capabilities = (
                "\nIf the operator asks what you can do, answer with what this "
                "run allows: host health (CPU, memory, load, disk, sample "
                "freshness), SSH reachability for "
                f"{target.host_scope or 'the hosts in scope'}, and published "
                "content (pages and projects)"
            )
            if runbooks_enabled:
                capabilities += ", and runbook procedures for operational tasks."
            else:
                capabilities += "."
            lines.append(capabilities)
    else:
        lines.append(
            "Send exactly one reply with:\n"
            '/app/scripts/notify.sh -m "<message>"'
        )
    return "\n".join(lines)


def _operator_message_from_instruction(instruction: str) -> str | None:
    marker = "Operator message:\n"
    start = instruction.find(marker)
    if start < 0:
        return None
    start += len(marker)
    # The operator message runs until the blank line before "Send exactly".
    end = instruction.find("\n\nSend exactly", start)
    if end < 0:
        end = instruction.find("\nSend exactly", start)
    if end < 0:
        end = len(instruction)
    return instruction[start:end].strip() or None


def _terminate(process: subprocess.Popen[Any]) -> None:
    """Terminate the agent and its MCP children, then reap it."""

    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def agent_log_path(config: Config) -> Path:
    """The shared debug transcript, under the state directory."""

    return agent_capture.log_path(config.state_file.parent)


def _finish_capture(thread: threading.Thread | None) -> None:
    """Wait briefly for the log drain to see EOF after the agent exits."""

    if thread is not None:
        thread.join(timeout=5.0)


def _new_send_marker(config: Config) -> Path:
    """Reserve a unique, listener-owned send-record path for one attempt."""

    return config.state_file.parent / (
        f"{SEND_MARKER_PREFIX}{uuid.uuid4().hex}.marker"
    )


def _discard_send_marker(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _run_agent_once(
    config: Config,
    chosen: InboundSource,
    instruction: str,
    stop_event: threading.Event | None,
    summary_text: str | None = None,
    model: str | None = None,
) -> tuple[bool, bool]:
    """Run one attempt; return ``(clean_exit, send_marker_seen)``."""

    effective_model = model if model is not None else config.opencode_model

    command = [config.opencode_bin, "run"]
    if os.environ.get("NOTIFY_DEBUG_LLM") == "1":
        # Explicitly requested for one debugging run: provider-level detail.
        # It is far too verbose to enable by default.
        command.extend(["--print-logs", "--log-level", "debug"])
    command.extend(
        [
            "--standalone",
            "--agent",
            "notify",
            "--model",
            effective_model,
            "--",
            instruction,
        ]
    )

    environment = os.environ.copy()
    environment.pop("NOTIFY_CONTENT_DISABLED", None)
    if chosen.target is not None and not chosen.target.allow_content:
        environment["NOTIFY_CONTENT_DISABLED"] = "1"
    # Runbooks are default-deny and per target: export the explicit grant and
    # that target's own BookStack URL, books and resolved credential pair only
    # when all are present, and clear any inherited values otherwise.
    environment.pop("NOTIFY_RUNBOOKS_ALLOWED", None)
    environment.pop("NOTIFY_BOOKSTACK_URL", None)
    environment.pop("NOTIFY_BOOKSTACK_BOOKS", None)
    environment.pop("NOTIFY_BOOKSTACK_TOKEN_ID", None)
    environment.pop("NOTIFY_BOOKSTACK_TOKEN_SECRET", None)
    token_id, token_secret = _bookstack_credentials(chosen.target)
    if (
        chosen.target is not None
        and chosen.target.allow_runbooks
        and chosen.target.bookstack_url
        and chosen.target.bookstack_books
        and token_id
        and token_secret
    ):
        environment["NOTIFY_RUNBOOKS_ALLOWED"] = "1"
        environment["NOTIFY_BOOKSTACK_URL"] = chosen.target.bookstack_url
        environment["NOTIFY_BOOKSTACK_BOOKS"] = chosen.target.bookstack_books
        environment["NOTIFY_BOOKSTACK_TOKEN_ID"] = token_id
        environment["NOTIFY_BOOKSTACK_TOKEN_SECRET"] = token_secret
    if chosen.reply_mode == "bot":
        # notify.sh treats DISCORD_WEBHOOK_URL and NOTIFY_DEFAULT_TARGET as
        # stronger than COMPOSIO_ARGS.  Remove both so this run can only reach
        # the source's own channel through the source's own bot connection.
        environment.pop("DISCORD_WEBHOOK_URL", None)
        environment.pop("NOTIFY_DEFAULT_TARGET", None)
        environment["COMPOSIO_TOOL"] = REPLY_TOOL
        environment["COMPOSIO_CONNECTED_ACCOUNT_ID"] = (
            chosen.connected_account_id
        )
        environment["COMPOSIO_ARGS"] = json.dumps(
            {"channel_id": chosen.channel_id}, separators=(",", ":")
        )

    marker = _new_send_marker(config)
    environment["NOTIFY_SEND_MARKER"] = str(marker)

    log_path = agent_log_path(config)
    log_fd, budget = agent_capture.record_start(
        log_path,
        f"inbound:{chosen.name}",
        effective_model,
        instruction,
        max_bytes=AGENT_LOG_MAX_BYTES,
    )
    capture = log_fd is not None

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        if log_fd is not None:
            try:
                os.close(log_fd)
            except OSError:
                pass
        agent_capture.record_end(
            log_path,
            "start-failed",
            0.0,
            False,
            max_bytes=AGENT_LOG_MAX_BYTES,
        )
        _discard_send_marker(marker)
        LOG.error("agent could not start: %s", type(error).__name__)
        agent_capture.print_summary(
            f"inbound:{chosen.name}",
            effective_model,
            "start-failed",
            0.0,
            False,
            summary_text if summary_text is not None else instruction,
        )
        return False, False

    drain: threading.Thread | None = None
    if capture:
        stream = getattr(process, "stdout", None)
        if stream is None:
            try:
                os.close(log_fd)
            except OSError:
                pass
        else:
            drain = threading.Thread(
                target=agent_capture.drain_output,
                args=(stream, log_fd, budget),
                kwargs={"max_bytes": AGENT_LOG_MAX_BYTES},
                name="discord-inbound-agent-log",
                daemon=True,
            )
            drain.start()

    def _finish(exit_code: int | str) -> tuple[bool, float]:
        """Close the capture and emit the one-line run summary exactly once."""

        _finish_capture(drain)
        elapsed = time.monotonic() - started
        sent = marker.exists()
        agent_capture.record_end(
            log_path,
            exit_code,
            elapsed,
            sent,
            max_bytes=AGENT_LOG_MAX_BYTES,
        )
        agent_capture.print_summary(
            f"inbound:{chosen.name}",
            effective_model,
            exit_code,
            elapsed,
            sent,
            summary_text if summary_text is not None else instruction,
        )
        return sent, elapsed

    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                sent, _ = _finish(return_code)
                return return_code == 0, sent
            if stop_event is not None and stop_event.is_set():
                LOG.info("agent stopping: shutdown requested")
                _terminate(process)
                _finish("stopped")
                return False, False
            if time.monotonic() - started > config.run_timeout_seconds:
                LOG.warning(
                    "agent timed out after %.0f seconds",
                    config.run_timeout_seconds,
                )
                _terminate(process)
                _finish("timeout")
                return False, False
            time.sleep(0.5)
    except BaseException:
        _terminate(process)
        _finish("interrupted")
        raise
    finally:
        _discard_send_marker(marker)


def run_agent(
    config: Config,
    instruction: str,
    stop_event: threading.Event | None = None,
    source: InboundSource | None = None,
    summary_text: str | None = None,
) -> bool:
    """Run the notify agent, retrying once when it exits without sending.

    Every attempt's complete stdout and stderr go to the shared, size-capped
    ``notify-debug.log`` under the state directory, framed by a header (time,
    path, model and exact instruction) and a footer (exit code, duration and
    whether a send was recorded).  Only the path is logged in the container
    log, never the contents.  The file is private (mode 0600) and never
    exceeds ``AGENT_LOG_MAX_BYTES``; excess output is drained and discarded.

    A clean exit does not prove the agent delivered an answer.  ``notify.sh``
    touches the per-attempt ``NOTIFY_SEND_MARKER`` path only on a confirmed
    2xx send, so a missing marker after exit 0 means the run sent nothing.
    That is logged as ``agent finished without sending`` and the same
    instruction is retried exactly once; a second miss stops there.  A
    non-zero exit keeps the previous behaviour and is not retried.  The retry
    is still one queued instruction: the dedupe cursor is not involved, so no
    message is replayed.

    Set ``NOTIFY_DEBUG_LLM=1`` to pass ``--print-logs --log-level debug`` to
    ``opencode run`` for one run being actively debugged.  It is off by default
    because provider-level logs are very verbose.

    A source without a named target replies through the bot: notify.sh is
    pinned to that source's channel and connected account for this run, so the
    answer cannot go to another server.  The environment is rebuilt for every
    attempt; the worker is single-threaded, so these values never race.
    """

    chosen = _resolve_source(config, source)
    log_path = agent_log_path(config)
    preferred = config.opencode_model
    fallback = config.opencode_model_fallback

    def _log_size() -> int:
        try:
            return log_path.stat().st_size
        except OSError:
            return 0

    def _read_slice(start: int) -> str:
        try:
            with log_path.open("rb") as handle:
                handle.seek(start)
                data = handle.read()
        except OSError:
            return ""
        return data.decode("utf-8", "replace")

    def _model_unavailable(text: str) -> bool:
        low = text.lower()
        if "free usage limit reached" in low:
            return True
        needles = (
            "model not found",
            "model unavailable",
            "model is unavailable",
            "unknown model",
            "no such model",
            "does not exist",
        )
        return any(needle in low for needle in needles)

    def _failure_reason(text: str) -> str:
        for line in text.splitlines():
            low = line.lower()
            if (
                "free usage limit" in low
                or "not found" in low
                or "unavailable" in low
                or "unknown model" in low
                or "does not exist" in low
            ):
                stripped = line.strip()
                if stripped:
                    return stripped
        return "model unavailable"

    before = _log_size()
    clean_exit, sent = _run_agent_once(
        config, chosen, instruction, stop_event,
        summary_text=summary_text, model=preferred,
    )
    after_text = _read_slice(before)
    if not clean_exit and not sent and fallback and _model_unavailable(after_text):
        reason = _failure_reason(after_text)
        # Make the fallback visible in the transcript.
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"fallback: preferred model {preferred} failed "
                    f"({reason}); retrying with {fallback}\\n"
                )
        except OSError:
            pass
        LOG.warning(
            "agent model unavailable: source=%s preferred=%s fallback=%s; "
            "retrying once",
            chosen.name,
            preferred,
            fallback,
        )
        if stop_event is not None and stop_event.is_set():
            LOG.info("agent retry skipped: shutdown requested")
            return False
        before2 = _log_size()
        clean_exit2, sent2 = _run_agent_once(
            config, chosen, instruction, stop_event,
            summary_text=summary_text, model=fallback,
        )
        if not clean_exit2:
            LOG.warning(
                "agent fallback failed: source=%s log=%s; not retrying again",
                chosen.name,
                log_path,
            )
            return False
        if not sent2:
            LOG.error(
                "agent fallback finished without sending: source=%s log=%s; "
                "not retrying again",
                chosen.name,
                log_path,
            )
            return False
        return True

    if not clean_exit:
        return False
    if sent:
        return True

    LOG.warning(
        "agent finished without sending: source=%s log=%s; retrying once",
        chosen.name,
        log_path,
    )
    if stop_event is not None and stop_event.is_set():
        LOG.info("agent retry skipped: shutdown requested")
        return False

    clean_exit, sent = _run_agent_once(
        config, chosen, instruction, stop_event,
        summary_text=summary_text, model=preferred,
    )
    if not clean_exit:
        LOG.warning(
            "agent retry failed: source=%s log=%s; not retrying again",
            chosen.name,
            log_path,
        )
        return False
    if not sent:
        LOG.error(
            "agent retry finished without sending: source=%s log=%s; "
            "not retrying again",
            chosen.name,
            log_path,
        )
        return False
    return True


class InstructionWorker:
    """One worker thread, one agent at a time, with a bounded queue."""

    def __init__(
        self,
        run: Callable[[Any], object],
        maxsize: int,
        stop_event: threading.Event,
    ) -> None:
        self._run = run
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=maxsize)
        self._stop = stop_event
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._active = 0
        self._max_active = 0
        self._processed = 0
        self._failed = 0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="discord-inbound-worker", daemon=True
        )
        self._thread.start()

    def offer(self, item: Any) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    self._active += 1
                    self._max_active = max(self._max_active, self._active)
                self._run(item)
            except Exception as error:
                with self._lock:
                    self._failed += 1
                # Log only the type: an exception message can echo the
                # instruction it was given.
                LOG.error("agent invocation raised: %s", type(error).__name__)
            finally:
                with self._lock:
                    self._active -= 1
                    self._processed += 1
                self._queue.task_done()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "processed": self._processed,
                "failed": self._failed,
                "max_active": self._max_active,
                "queued": self._queue.qsize(),
            }


def _source_name(source: Config | InboundSource) -> str:
    return source.name if isinstance(source, InboundSource) else "default"


def ingest_messages(
    source: Config | InboundSource,
    messages: Iterable[object],
    store: SeenStore,
    enqueue: Callable[[str], bool],
) -> list[Decision]:
    """Filter, record and enqueue a poll's messages.

    ``enqueue`` is the only route to an agent run.  Tests inject a stub; the
    listener passes a closure over ``InstructionWorker.offer``.
    """

    name = _source_name(source)
    decisions: list[Decision] = []
    for message in messages:
        decision = evaluate_message(source, message, store.is_seen)
        decisions.append(decision)
        token = short_id(decision.message_id)

        if not decision.accepted:
            LOG.warning(
                "refused source=%s reason=%s msg=%s",
                name,
                decision.reason,
                token,
            )
            if decision.message_id:
                store.record(decision.message_id)
            continue

        LOG.info(
            "accepted source=%s msg=%s chars=%d",
            name,
            token,
            len(decision.text),
        )
        # Record before queuing: at-most-once, never replay after a restart.
        store.record(decision.message_id)
        instruction = build_instruction(source, decision.text)
        if not enqueue(instruction):
            LOG.warning(
                "dropped source=%s reason=queue_full msg=%s", name, token
            )
    return decisions


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handle(signum: int, _frame: object) -> None:
        LOG.info("received signal %s; stopping", signum)
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _handle)
        except (ValueError, OSError):
            # Not the main thread (for example in a test); callers can still
            # set the event directly.
            pass


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # no command-line arguments: configuration is environment only
    _configure_logging()
    try:
        config = load_config(os.environ)
    except ConfigError as error:
        LOG.error("configuration error: %s", error)
        return 2

    stores: dict[str, SeenStore] = {}
    for source in config.sources:
        try:
            stores[source.name] = SeenStore(source.state_file)
        except StateError as error:
            LOG.error("state error: source=%s: %s", source.name, error)
            return 3

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    worker = InstructionWorker(
        run=lambda item: run_agent(
            config, item[1], stop_event, source=item[0],
            summary_text=_operator_message_from_instruction(item[1]),
        ),
        maxsize=config.queue_size,
        stop_event=stop_event,
    )
    worker.start()

    LOG.info(
        "starting: sources=%d delivery=%s poll=%.0fs queue=%d "
        "max_instruction_chars=%d",
        len(config.sources),
        config.delivery,
        config.poll_seconds,
        config.queue_size,
        config.max_instruction_chars,
    )
    LOG.info(
        "agent transcript: %s",
        agent_capture.log_path(config.state_file.parent),
    )
    for source in config.sources:
        reply = source.target.name if source.target is not None else source.reply_mode
        LOG.info(
            "source=%s reply=%s authors=%d",
            source.name,
            reply,
            len(source.allowed_authors),
        )
        if source.reply_mode == "legacy":
            LOG.warning(
                "source=%s has no NOTIFY_INBOUND_TARGET or "
                "NOTIFY_DEFAULT_TARGET; replies use the legacy Composio "
                "delivery configuration and this listener cannot verify that "
                "it is the inbound channel",
                source.name,
            )

    exit_code = 0
    try:
        while not stop_event.is_set():
            for source in config.sources:
                store = stores[source.name]
                try:
                    messages = fetch_new_messages(config, store, source)
                    if messages:
                        ingest_messages(
                            source,
                            messages,
                            store,
                            lambda text, chosen=source: worker.offer(
                                (chosen, text)
                            ),
                        )
                except FetchError as error:
                    LOG.warning(
                        "poll failed source=%s: %s", source.name, error
                    )
                except StateError as error:
                    LOG.error(
                        "state error source=%s: %s", source.name, error
                    )
                    exit_code = 3
                    break
            if exit_code:
                break
            stop_event.wait(config.poll_seconds)
    finally:
        worker.stop()
        stats = worker.stats()
        LOG.info(
            "stopped: processed=%d failed=%d max_active=%d queued=%d",
            stats["processed"],
            stats["failed"],
            stats["max_active"],
            stats["queued"],
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

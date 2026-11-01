#!/usr/bin/env python3
"""Inbound Discord replies for the notify agent.

The bot is normally output-only: ``scripts/notify.sh`` posts a message and
nothing reads the channel.  This listener closes that loop.  It polls one
admin-chat channel through Composio's proxy endpoint, applies a default-deny
authorisation model, and for each accepted message runs the existing
``notify`` OpenCode agent non-interactively.  The agent answers with
``scripts/notify.sh``, so the answer comes back to the same channel.

Why Composio's proxy and not ``DISCORD_NEW_MESSAGE_TRIGGER``
-------------------------------------------------------------

The deployment already has a Composio Discord connected account and
``COMPOSIO_API_KEY``; using it adds no credential.  Composio's trigger is
delivered by webhook or a realtime Pusher subscription.  This container
publishes no inbound port, and the trigger API does not expose a documented
"poll my trigger events" REST method.  The proxy endpoint
(``POST /api/v3.1/tools/execute/proxy``) does let us make an authenticated
Discord ``GET /channels/{id}/messages`` call with the same connected account.
It returns messages newest-first, so the listener walks backwards with the
``before`` parameter until it reaches its saved cursor, then handles the new
messages oldest-first.  That is a plain, bounded, idempotent poll with no new
dependency (``urllib`` is enough).

Default-deny rules
------------------

* ``NOTIFY_INBOUND_ALLOWED_AUTHORS`` is required.  An empty or missing
  allowlist makes the listener refuse to start.
* Only ``NOTIFY_INBOUND_CHANNEL_ID`` is read, and every returned message must
  carry that same ``channel_id``.
* Bot and webhook messages are ignored, including the notification bot's own
  messages.  Only user messages and replies (types 0 and 19) are considered.
* Message IDs are recorded in a small state file before an instruction is
  queued, so a restart or an overlapping poll cannot replay it.  The saved
  cursor also acts as the first-poll baseline: pre-existing messages,
  including old notifications, are never actioned.
* Instructions are capped after mention removal; over-long messages are
  refused rather than truncated.
* One worker thread runs the agent.  A bounded queue absorbs bursts; messages
  beyond it are dropped and logged.  Two agents never race.

Everything that could carry a secret is redacted from logs: the API key,
webhook URL, channel id, author ids and the operator's message body are never
printed.  Message ids are logged only as a short hash.
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
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

LOG = logging.getLogger("discord-inbound")

COMPOSIO_PROXY_PATH = "/api/v3.1/tools/execute/proxy"
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
STATE_VERSION = 1
SEEN_ID_LIMIT = 500
MESSAGE_PAGE_LIMIT = 100
MAX_PAGES_PER_POLL = 20
HTTP_TIMEOUT_SECONDS = 30
MAX_PROXY_RESPONSE_BYTES = 2 * 1024 * 1024
DISCORD_EPOCH_MS = 1_420_070_400_000

# Discord message types that can carry a human instruction.  Type 19 is a
# reply, which is exactly how the captain answers a notification.
ACCEPTED_MESSAGE_TYPES = frozenset({0, 19})

TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
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


@dataclasses.dataclass(frozen=True)
class Config:
    api_base: str
    api_key: str = dataclasses.field(repr=False)
    channel_id: str
    allowed_authors: frozenset[str]
    connected_account_id: str
    user_id: str
    target: InboundTarget | None
    poll_seconds: float
    max_instruction_chars: int
    queue_size: int
    run_timeout_seconds: float
    state_file: Path
    opencode_model: str
    opencode_bin: str
    delivery: str
    webhook_mode: bool


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


def _load_target(
    env: Mapping[str, str],
    channel_id: str,
) -> InboundTarget | None:
    """Resolve the named target used for the reply, if one is configured."""

    if (env.get("DISCORD_WEBHOOK_URL") or "").strip():
        # notify.sh rejects -t (and even NOTIFY_DEFAULT_TARGET) when a webhook
        # is set, so a named target here could only fail at reply time.
        for name in ("NOTIFY_INBOUND_TARGET", "NOTIFY_DEFAULT_TARGET"):
            if (env.get(name) or "").strip():
                raise ConfigError(
                    f"{name} cannot be used with DISCORD_WEBHOOK_URL; "
                    "a webhook has no named targets"
                )
        return None

    name = (env.get("NOTIFY_INBOUND_TARGET") or "").strip()
    if not name:
        name = (env.get("NOTIFY_DEFAULT_TARGET") or "").strip()
    if not name:
        # Legacy Composio delivery is still allowed, but it cannot be tied to
        # the inbound channel by this listener.  The operator's existing
        # COMPOSIO_ARGS/COMPOSIO_CONNECTED_ACCOUNT_ID configuration applies.
        return None
    if not TARGET_NAME_RE.match(name):
        raise ConfigError(
            "NOTIFY_INBOUND_TARGET may contain only letters, digits, dot, "
            "underscore and hyphen"
        )

    raw_targets = (env.get("NOTIFY_TARGETS") or "").strip()
    if not raw_targets:
        raise ConfigError(
            f"NOTIFY_INBOUND_TARGET names target {name!r}, but NOTIFY_TARGETS "
            "is not set"
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
            f"NOTIFY_INBOUND_TARGET {name!r} is not defined in NOTIFY_TARGETS"
        )

    target_channel = entry.get("channel_id")
    if not isinstance(target_channel, str) or not SNOWFLAKE_RE.match(
        target_channel.strip()
    ):
        raise ConfigError(f"target {name!r} needs a numeric channel_id")
    if int(target_channel.strip()) != int(channel_id):
        raise ConfigError(
            f"target {name!r} channel_id does not match "
            "NOTIFY_INBOUND_CHANNEL_ID; refusing to send replies to a "
            "different channel"
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

    return InboundTarget(
        name=name,
        channel_id=target_channel.strip(),
        connected_account_id=target_account,
        host_scope=host_scope,
        allow_content=allow_content,
    )


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

    channel_id = _required(env, "NOTIFY_INBOUND_CHANNEL_ID")
    if not SNOWFLAKE_RE.match(channel_id):
        raise ConfigError("NOTIFY_INBOUND_CHANNEL_ID must be a numeric Discord id")

    allowed_authors = parse_allowlist(env.get("NOTIFY_INBOUND_ALLOWED_AUTHORS", ""))

    webhook_mode = bool((env.get("DISCORD_WEBHOOK_URL") or "").strip())
    if webhook_mode:
        if not (env.get("DISCORD_WEBHOOK_URL") or "").strip().startswith("https://"):
            raise ConfigError("DISCORD_WEBHOOK_URL must be an https:// URL")
        delivery = "discord-webhook"
    else:
        if not (env.get("COMPOSIO_TOOL") or "").strip():
            raise ConfigError(
                "no delivery configured: set DISCORD_WEBHOOK_URL, or "
                "COMPOSIO_TOOL for the Composio path"
            )
        delivery = "composio"

    target = _load_target(env, channel_id)
    if target is not None:
        delivery = "composio-target"

    connected_account_id = _resolve_read_account(env, target)

    user_id = (env.get("COMPOSIO_USER_ID") or "").strip()

    model = _required(env, "OPENCODE_MODEL")
    if any(character.isspace() for character in model):
        raise ConfigError("OPENCODE_MODEL must not contain whitespace")

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

    return Config(
        api_base=api_base,
        api_key=api_key,
        channel_id=channel_id,
        allowed_authors=allowed_authors,
        connected_account_id=connected_account_id,
        user_id=user_id,
        target=target,
        poll_seconds=poll_seconds,
        max_instruction_chars=max_chars,
        queue_size=queue_size,
        run_timeout_seconds=run_timeout,
        state_file=state_path,
        opencode_model=model,
        opencode_bin=opencode_bin,
        delivery=delivery,
        webhook_mode=webhook_mode,
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
# Composio proxy polling
# ---------------------------------------------------------------------------


def _proxy_request(
    config: Config,
    endpoint: str,
    parameters: Sequence[Mapping[str, str]],
) -> object:
    body: dict[str, Any] = {
        "endpoint": endpoint,
        "method": "GET",
        "parameters": list(parameters),
    }
    if config.connected_account_id:
        body["connected_account_id"] = config.connected_account_id
    if config.user_id:
        body["user_id"] = config.user_id

    encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        f"{config.api_base}{COMPOSIO_PROXY_PATH}",
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-api-key": config.api_key,
            "User-Agent": "notify-agent-discord-inbound/1",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_PROXY_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        # Never read or log the response body: it can echo request data.
        raise FetchError(f"proxy returned HTTP {error.code}") from None
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, str) and reason:
            raise FetchError(f"proxy request failed: {reason}") from None
        raise FetchError("proxy request failed") from None
    except (TimeoutError, OSError):
        raise FetchError("proxy request failed or timed out") from None

    if len(raw) > MAX_PROXY_RESPONSE_BYTES:
        raise FetchError("proxy response was too large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FetchError("proxy response was not valid JSON") from None
    if not isinstance(payload, dict):
        raise FetchError("proxy response had an unexpected shape")

    status = payload.get("status")
    if status != 200:
        if isinstance(status, int) and not isinstance(status, bool):
            raise FetchError(f"proxy returned HTTP {status}")
        raise FetchError("proxy response had no valid status")

    return payload.get("data")


def fetch_messages(
    config: Config,
    *,
    before: str | None = None,
    limit: int = MESSAGE_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    parameters: list[dict[str, str]] = [
        {"name": "limit", "value": str(limit), "type": "query"}
    ]
    if before:
        parameters.append({"name": "before", "value": before, "type": "query"})
    data = _proxy_request(
        config,
        f"/channels/{config.channel_id}/messages",
        parameters,
    )
    if not isinstance(data, list):
        raise FetchError("proxy response did not contain a message list")

    messages: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            raise FetchError("proxy response contained a malformed message")
        message_id = item.get("id")
        if not isinstance(message_id, str) or not SNOWFLAKE_RE.match(message_id):
            raise FetchError("proxy response contained a message without an id")
        messages.append(item)
    return messages


def fetch_channel_last_message_id(config: Config) -> str:
    """Return the channel's newest message id, even without Read History.

    ``last_message_id`` is visible with View Channel alone.  It gives the
    baseline a server-side position when Discord returns an empty message list
    because the bot lacks Read Message History, so a later permission grant
    cannot make old history look new.
    """

    data = _proxy_request(config, f"/channels/{config.channel_id}", [])
    if not isinstance(data, dict):
        raise FetchError("proxy response did not contain a channel object")
    last = data.get("last_message_id")
    if last is None:
        return ""
    if not isinstance(last, str) or not SNOWFLAKE_RE.match(last):
        raise FetchError("proxy response contained an invalid channel object")
    return last


def fetch_new_messages(config: Config, store: SeenStore) -> list[dict[str, Any]]:
    """Return new messages oldest-first, or establish the first-poll baseline.

    Discord returns messages newest-first.  ``after`` would return the newest
    page after the cursor, silently skipping older messages in a backlog, so
    the listener walks backwards with ``before`` until it reaches the cursor,
    then reverses the collected pages.  The page cap bounds memory on a very
    long outage; when it is hit the gap is logged and the cursor moves on.
    """

    if not store.initialized:
        # A single newest message is enough: the baseline is "ignore anything
        # that already exists", including the bot's own past notifications.
        page = fetch_messages(config, limit=1)
        ids = [item["id"] for item in page]
        if ids:
            cursor = max(ids, key=int)
        else:
            # No visible message can mean an empty channel or a bot without
            # Read Message History.  last_message_id is visible with View
            # Channel alone, so it is the better server-side baseline.  A
            # synthetic now-snowflake is the fallback for a truly empty
            # channel; both prevent a later permission grant from making old
            # history look new.
            cursor = fetch_channel_last_message_id(config)
            if cursor:
                LOG.warning(
                    "channel has messages but none are readable; using its "
                    "last_message_id as the baseline. Grant Read Message "
                    "History to read replies."
                )
            else:
                cursor = snowflake_at(time.time())
        store.initialize(ids, cursor)
        LOG.info(
            "baseline established: ignored=%d cursor=%s",
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
        page = fetch_messages(config, before=before, limit=MESSAGE_PAGE_LIMIT)
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
            "backlog truncated at %d pages; messages older than the oldest "
            "returned page are skipped",
            MAX_PAGES_PER_POLL,
        )

    return [by_id[key] for key in sorted(by_id, key=int)]


# ---------------------------------------------------------------------------
# Agent invocation and the single-run worker
# ---------------------------------------------------------------------------


def build_instruction(config: Config, operator_message: str) -> str:
    lines = [
        "You are answering an inbound reply from the operator in the admin chat.",
        "",
        "The message below is the operator's request. Answer it directly and "
        "briefly; do not compose a general status digest or recap the "
        "notification it replies to. Use only the read-only Oracle tools and "
        "the notify.sh send command. If the available tools cannot answer it, "
        "say so plainly instead of guessing.",
        "",
        "Operator message:",
        operator_message,
        "",
    ]
    if config.target is not None:
        lines.append(
            "Send exactly one reply with:\n"
            f'/app/scripts/notify.sh -t {config.target.name} -m "<message>"'
        )
        if config.target.host_scope:
            lines.append(
                "\nThe delivery scope for this run is "
                f"{config.target.host_scope}. Nothing about any other host may "
                "appear in the reply: report only on the scoped host, and if "
                "there is no data for it, say that plainly instead of using "
                "another host's numbers."
            )
        if not config.target.allow_content:
            lines.append(
                "\nContent search is disabled for this run: do not use the "
                "content search tool, and do not include anything about the "
                "operator's site, CV, projects or pages. Report host data only."
            )
    else:
        lines.append(
            "Send exactly one reply with:\n"
            '/app/scripts/notify.sh -m "<message>"'
        )
    return "\n".join(lines)


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


def run_agent(
    config: Config,
    instruction: str,
    stop_event: threading.Event | None = None,
) -> bool:
    """Run the notify agent once, non-interactively.

    stdout and stderr are discarded: the agent already sends its answer to
    Discord, and discarding keeps model output (which could contain a mistake)
    out of the listener's logs.  Only the exit status and duration are logged.
    """

    command = [
        config.opencode_bin,
        "run",
        "--standalone",
        "--agent",
        "notify",
        "--model",
        config.opencode_model,
        "--",
        instruction,
    ]

    environment = os.environ.copy()
    environment.pop("NOTIFY_CONTENT_DISABLED", None)
    if config.target is not None and not config.target.allow_content:
        environment["NOTIFY_CONTENT_DISABLED"] = "1"

    started = time.monotonic()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        LOG.error("agent could not start: %s", type(error).__name__)
        return False

    try:
        while True:
            return_code = process.poll()
            if return_code is not None:
                elapsed = time.monotonic() - started
                if return_code == 0:
                    LOG.info("agent finished: exit=0 seconds=%.1f", elapsed)
                else:
                    LOG.warning(
                        "agent failed: exit=%s seconds=%.1f",
                        return_code,
                        elapsed,
                    )
                return return_code == 0
            if stop_event is not None and stop_event.is_set():
                LOG.info("agent stopping: shutdown requested")
                _terminate(process)
                return False
            if time.monotonic() - started > config.run_timeout_seconds:
                LOG.warning(
                    "agent timed out after %.0f seconds",
                    config.run_timeout_seconds,
                )
                _terminate(process)
                return False
            time.sleep(0.5)
    except BaseException:
        _terminate(process)
        raise


class InstructionWorker:
    """One worker thread, one agent at a time, with a bounded queue."""

    def __init__(
        self,
        run: Callable[[str], object],
        maxsize: int,
        stop_event: threading.Event,
    ) -> None:
        self._run = run
        self._queue: queue.Queue[str] = queue.Queue(maxsize=maxsize)
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

    def offer(self, instruction: str) -> bool:
        try:
            self._queue.put_nowait(instruction)
            return True
        except queue.Full:
            return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                instruction = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    self._active += 1
                    self._max_active = max(self._max_active, self._active)
                self._run(instruction)
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


def ingest_messages(
    config: Config,
    messages: Iterable[object],
    store: SeenStore,
    enqueue: Callable[[str], bool],
) -> list[Decision]:
    """Filter, record and enqueue a poll's messages.

    ``enqueue`` is the only route to an agent run.  Tests inject a stub; the
    listener passes ``InstructionWorker.offer``.
    """

    decisions: list[Decision] = []
    for message in messages:
        decision = evaluate_message(config, message, store.is_seen)
        decisions.append(decision)
        token = short_id(decision.message_id)

        if not decision.accepted:
            LOG.warning("refused reason=%s msg=%s", decision.reason, token)
            if decision.message_id:
                store.record(decision.message_id)
            continue

        LOG.info(
            "accepted msg=%s chars=%d",
            token,
            len(decision.text),
        )
        # Record before queuing: at-most-once, never replay after a restart.
        store.record(decision.message_id)
        instruction = build_instruction(config, decision.text)
        if not enqueue(instruction):
            LOG.warning("dropped reason=queue_full msg=%s", token)
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

    try:
        store = SeenStore(config.state_file)
    except StateError as error:
        LOG.error("state error: %s", error)
        return 3

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    worker = InstructionWorker(
        run=lambda instruction: run_agent(config, instruction, stop_event),
        maxsize=config.queue_size,
        stop_event=stop_event,
    )
    worker.start()

    if config.target is not None:
        target_name = config.target.name
    elif config.webhook_mode:
        target_name = "none"
    else:
        target_name = "legacy"
    LOG.info(
        "starting: delivery=%s target=%s poll=%.0fs queue=%d "
        "max_instruction_chars=%d authors=%d",
        config.delivery,
        target_name,
        config.poll_seconds,
        config.queue_size,
        config.max_instruction_chars,
        len(config.allowed_authors),
    )
    if config.target is None and not config.webhook_mode:
        LOG.warning(
            "no NOTIFY_INBOUND_TARGET or NOTIFY_DEFAULT_TARGET is set; replies "
            "use the legacy Composio delivery configuration and this listener "
            "cannot verify that it is the inbound channel"
        )

    exit_code = 0
    try:
        while not stop_event.is_set():
            try:
                messages = fetch_new_messages(config, store)
                if messages:
                    ingest_messages(config, messages, store, worker.offer)
            except FetchError as error:
                LOG.warning("poll failed: %s", error)
            except StateError as error:
                LOG.error("state error: %s", error)
                exit_code = 3
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

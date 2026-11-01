#!/usr/bin/env python3
"""Resolve the notify agent's targets and sources.

The operator can edit the bot configuration in the CMS admin panel.  This
module is the single resolver both delivery paths use:

* ``scripts/run-prompt.sh`` needs ``NOTIFY_TARGETS`` for a scheduled prompt.
* ``scripts/discord_inbound.py`` needs ``NOTIFY_TARGETS`` and
  ``NOTIFY_INBOUND_SOURCES`` for inbound replies.

Precedence is CMS, then the last good cached copy, then the environment.  The
resolver never raises for a CMS problem: a website outage must not silence the
09:00 digest.  When ``BOT_SETTINGS_URL`` is unset, it returns the environment
values unchanged, so the feature is enabled and disabled by one variable.

Security boundaries:

* ``BOT_SETTINGS_API_KEY`` is a Payload API key.  It is sent only in the
  ``Authorization`` header, never placed in a URL, log line or error message.
  Redirects are refused so it cannot be forwarded to another host.
* A fetched document is validated with the same rules the agent applies to the
  environment: targets with the parser used at the notify.sh send boundary,
  sources with ``discord_inbound``'s own ``_load_sources``.  A document the
  agent would refuse from the environment is refused here too, and the run
  falls through to the cache or the environment.
* The cache holds internal configuration (host names and channel ids), not
  secrets.  It is written atomically with mode 0600 under ``$XDG_STATE_HOME``
  (the same state directory the agent already uses).

Command line, for ``run-prompt.sh``::

    python3 scripts/bot_config.py --targets
    python3 scripts/bot_config.py --sources

The resolved JSON is printed to stdout; the single source log line goes to
stderr.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_DOCUMENT_BYTES = 256 * 1024
CACHE_DIR_NAME = "notify-agent"
CACHE_FILE_NAME = "bot-settings-cache.json"
LOG_PREFIX = "config"

TARGETS_SHAPE_ERROR = (
    "NOTIFY_TARGETS must be a JSON object mapping names to objects with a "
    "channel_id"
)


class DocumentProblem(ValueError):
    """The fetched or cached document is not usable."""

    @property
    def reason(self) -> str:
        return str(self)


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


_OPENER = urllib.request.build_opener(_NoRedirect())


@dataclasses.dataclass(frozen=True)
class Resolution:
    """The effective targets/sources and where they came from."""

    targets_json: str
    sources_json: str
    source: str
    reason: str = ""

    @property
    def log_line(self) -> str:
        line = f"{LOG_PREFIX} source={self.source}"
        if self.reason:
            line = f"{line} reason={self.reason}"
        return line

    @property
    def log_required(self) -> bool:
        """An unset URL keeps today's silent behaviour."""

        return self.source != "env" or bool(self.reason)


def timeout_seconds(environ: Mapping[str, str]) -> float:
    """Read BOT_SETTINGS_TIMEOUT_SECONDS, default 5, capped at 30.

    A malformed value is not allowed to fail a run: it falls back to the
    default, because this setting exists to bound a website call, not to gate
    delivery.
    """

    raw = (environ.get("BOT_SETTINGS_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return min(value, MAX_TIMEOUT_SECONDS)


def cache_path(environ: Mapping[str, str]) -> Path:
    """Return the cache path under the agent's existing state directory."""

    state_home = (environ.get("XDG_STATE_HOME") or "").strip()
    if state_home:
        base = Path(state_home).expanduser()
    else:
        base = Path.home() / ".local" / "state"
    return base / CACHE_DIR_NAME / CACHE_FILE_NAME


def _format_seconds(value: float) -> str:
    if value == int(value):
        return f"{int(value)}s"
    return f"{value:g}s"


def _env_resolution(
    environ: Mapping[str, str], reason: str = ""
) -> Resolution:
    return Resolution(
        targets_json=environ.get("NOTIFY_TARGETS") or "",
        sources_json=environ.get("NOTIFY_INBOUND_SOURCES") or "",
        source="env",
        reason=reason,
    )


def _safe_reason(text: str) -> str:
    """Keep a log reason on one line without control characters."""

    cleaned = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
    return cleaned[:300]


def _validate_targets(targets: object) -> None:
    """Apply the notify.sh send-boundary target rules.

    This mirrors the jq validation in ``notify.sh`` so the CMS cannot supply a
    target the delivery script would refuse.  Errors name targets only, never
    their channel or account ids.
    """

    if not isinstance(targets, dict):
        raise DocumentProblem(TARGETS_SHAPE_ERROR)
    for entry in targets.values():
        if not isinstance(entry, dict):
            raise DocumentProblem(TARGETS_SHAPE_ERROR)
        channel_id = entry.get("channel_id")
        if not isinstance(channel_id, str) or not channel_id:
            raise DocumentProblem(TARGETS_SHAPE_ERROR)
        if "connected_account_id" in entry and not isinstance(
            entry["connected_account_id"], str
        ):
            raise DocumentProblem(TARGETS_SHAPE_ERROR)

    bad_scope: list[str] = []
    bad_content: list[str] = []
    for name, entry in targets.items():
        if "host_scope" in entry:
            scope = entry["host_scope"]
            valid = (
                isinstance(scope, str)
                and bool(scope)
                or isinstance(scope, list)
                and bool(scope)
                and all(isinstance(item, str) and bool(item) for item in scope)
            )
            if not valid:
                bad_scope.append(name)
        if "allow_content" in entry and not isinstance(
            entry["allow_content"], bool
        ):
            bad_content.append(name)

    if bad_scope:
        raise DocumentProblem(
            "invalid host_scope for target(s) "
            f"{', '.join(bad_scope)}: expected a non-empty string or an array "
            "of non-empty strings"
        )
    if bad_content:
        raise DocumentProblem(
            "invalid allow_content for target(s) "
            f"{', '.join(bad_content)}: expected a boolean"
        )


def _validate_sources(
    environ: Mapping[str, str], targets: object, sources: object
) -> None:
    """Apply the inbound listener's own source rules.

    ``_load_sources`` is the exact parser ``load_config`` uses for
    ``NOTIFY_INBOUND_SOURCES``.  Importing it lazily avoids a module cycle
    (``discord_inbound`` imports this module to resolve its configuration).
    """

    if not sources:
        return
    import discord_inbound  # noqa: PLC0415 - deliberate lazy import

    env = dict(environ)
    env["NOTIFY_TARGETS"] = _encode(targets)
    env["NOTIFY_INBOUND_SOURCES"] = _encode(sources)
    try:
        raw_authors = (env.get("NOTIFY_INBOUND_ALLOWED_AUTHORS") or "").strip()
        global_authors = (
            discord_inbound.parse_allowlist(raw_authors)
            if raw_authors
            else frozenset()
        )
        webhook_mode = bool((env.get("DISCORD_WEBHOOK_URL") or "").strip())
        discord_inbound._load_sources(
            env,
            webhook_mode=webhook_mode,
            global_authors=global_authors,
            max_chars=discord_inbound.DEFAULT_MAX_INSTRUCTION_CHARS,
            state_path=Path(tempfile.gettempdir())
            / "bot-config-validation.json",
        )
    except Exception as error:  # the listener's parser uses ConfigError
        message = str(error) or "source configuration was refused"
        raise DocumentProblem(message) from None


def validate_document(
    document: object, environ: Mapping[str, str]
) -> tuple[object, object]:
    """Validate a CMS document and return its targets and sources."""

    if not isinstance(document, dict):
        raise DocumentProblem("document is not a JSON object")
    if "version" not in document:
        raise DocumentProblem("document is missing version")
    version = document["version"]
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        raise DocumentProblem("document version must be a number or string")
    if "targets" not in document:
        raise DocumentProblem("document is missing targets")
    if "sources" not in document:
        raise DocumentProblem("document is missing sources")
    targets = document["targets"]
    sources = document["sources"]
    if not isinstance(sources, list):
        raise DocumentProblem("document sources must be a JSON array")
    _validate_targets(targets)
    _validate_sources(environ, targets, sources)
    return targets, sources


def _encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _parse_document(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise DocumentProblem("invalid JSON") from None


def _fetch(url: str, api_key: str, timeout: float) -> bytes:
    headers = {
        "Accept": "application/json",
        "User-Agent": "notify-agent-bot-config/1",
    }
    if api_key:
        headers["Authorization"] = f"users API-Key {api_key}"
    try:
        request = urllib.request.Request(url, headers=headers, method="GET")
    except ValueError:
        raise DocumentProblem("invalid URL") from None
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status = response.status
            if status != 200:
                raise DocumentProblem(str(status))
            raw = response.read(MAX_DOCUMENT_BYTES + 1)
    except urllib.error.HTTPError as error:
        # Never read or log the body: it can echo request data.
        code = error.code
        error.close()
        raise DocumentProblem(str(code)) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise DocumentProblem(
                f"timeout {_format_seconds(timeout)}"
            ) from None
        raise DocumentProblem("network error") from None
    except (TimeoutError, socket.timeout):
        raise DocumentProblem(f"timeout {_format_seconds(timeout)}") from None
    except ValueError:
        raise DocumentProblem("invalid URL") from None
    except OSError:
        raise DocumentProblem("network error") from None
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentProblem("response too large")
    return raw


def _write_cache(path: Path, raw: bytes) -> None:
    """Write the cache atomically with mode 0600."""

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = -1
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(
            dir=str(parent), prefix=".bot-settings-", suffix=".tmp"
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
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


def _read_cache(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise DocumentProblem("cache unreadable") from None


def resolve(environ: Mapping[str, str] | None = None) -> Resolution:
    """Resolve configuration with CMS -> cache -> environment precedence."""

    env = os.environ if environ is None else environ
    url = (env.get("BOT_SETTINGS_URL") or "").strip()
    if not url:
        # Byte-for-byte the old behaviour: no request, no cache, no log line.
        return _env_resolution(env)

    api_key = (env.get("BOT_SETTINGS_API_KEY") or "").strip()
    timeout = timeout_seconds(env)
    cache = cache_path(env)

    try:
        raw = _fetch(url, api_key, timeout)
        targets, sources = validate_document(_parse_document(raw), env)
    except DocumentProblem as error:
        reason = f"cms {_safe_reason(error.reason)}"
        try:
            cached_raw = _read_cache(cache)
        except DocumentProblem as cache_error:
            return _env_resolution(
                env, f"{reason} {_safe_reason(cache_error.reason)}"
            )
        if cached_raw is None:
            return _env_resolution(env, reason)
        try:
            targets, sources = validate_document(
                _parse_document(cached_raw), env
            )
        except DocumentProblem as cache_error:
            return _env_resolution(
                env, f"{reason} cache {_safe_reason(cache_error.reason)}"
            )
        return Resolution(
            targets_json=_encode(targets),
            sources_json="" if not sources else _encode(sources),
            source="cache",
            reason=reason,
        )

    try:
        _write_cache(cache, raw)
    except OSError:
        # The fetched config is valid and usable; a cache write failure must
        # not fail the run.  There is deliberately no extra log line.
        pass

    return Resolution(
        targets_json=_encode(targets),
        sources_json="" if not sources else _encode(sources),
        source="cms",
    )


def _main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--targets"
    if mode not in ("--targets", "--sources", "--log"):
        sys.stderr.write(
            "usage: bot_config.py [--targets|--sources|--log]\n"
        )
        return 64
    resolution = resolve()
    if mode == "--targets":
        sys.stdout.write(resolution.targets_json)
    elif mode == "--sources":
        sys.stdout.write(resolution.sources_json)
    else:
        sys.stdout.write(resolution.log_line)
        return 0
    # The source line belongs on stderr so stdout stays parseable JSON.
    if resolution.log_required:
        sys.stderr.write(f"{resolution.log_line}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))

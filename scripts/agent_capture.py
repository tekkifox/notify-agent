#!/usr/bin/env python3
"""Shared, bounded debug transcript for notify agent runs.

Every inbound reply and every scheduled prompt writes the agent's complete
stdout and stderr to one file, ``notify-debug.log``, under the agent data
directory.  Each attempt is self-describing: a header records the UTC time,
the path (inbound source or schedule), the model and the exact instruction,
the agent's output follows, and a footer records the exit code, duration and
whether ``notify.sh`` confirmed a send.

The file is private (mode 0600) and capped at ``MAX_BYTES``.  A record that
would overflow is clipped, output past the cap is drained and discarded with a
truncation note, and the footer always fits because space is reserved for it.

This module is used in two ways: ``discord_inbound.py`` imports the helpers,
and ``run-prompt.sh`` runs it as a command to wrap ``opencode run``.

The transcript can contain the operator's instruction, host data and anything
the model or its tools printed.  It is never copied into the container log,
and operators should treat it as internal.
"""

from __future__ import annotations

import argparse
import datetime
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Sequence

DEBUG_LOG_NAME = "notify-debug.log"
MAX_BYTES = 256 * 1024
# Start a fresh file when the remaining room is too small to hold a header.
MIN_ROOM_BYTES = 16 * 1024
# Keep enough room for the footer after the agent output.
FOOTER_RESERVE = 512
# One-line container-log summary.  The instruction excerpt is short and the
# whole line is hard-capped so a long source name or model cannot balloon it.
SUMMARY_MAX_CHARS = 320
SUMMARY_INSTRUCTION_CHARS = 200


def log_path(state_dir: Path) -> Path:
    """Return the shared transcript path under ``state_dir``."""

    return Path(state_dir) / DEBUG_LOG_NAME


def _reset_if_full(path: Path, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    except OSError:
        return
    if size >= max_bytes - MIN_ROOM_BYTES:
        try:
            path.unlink()
        except OSError:
            pass


def _open_append(path: Path) -> int | None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError as error:
        print(
            f"agent-capture: could not open the debug log: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        return None
    try:
        os.fchmod(fd, 0o600)
    except OSError as error:
        print(
            f"agent-capture: could not secure the debug log: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        os.close(fd)
        return None
    return fd


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def flatten_instruction(
    text: str, limit: int = SUMMARY_INSTRUCTION_CHARS
) -> str:
    """Collapse all whitespace to single spaces, then hard-truncate."""

    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    if limit <= 3:
        return collapsed[: max(0, limit)]
    return collapsed[: limit - 3].rstrip() + "..."


def _shorten(value: str, room: int) -> str:
    """Fit ``value`` into ``room`` characters, marking a cut with an ellipsis."""

    if room <= 0:
        return ""
    if len(value) <= room:
        return value
    if room <= 3:
        return value[:room]
    return value[: room - 3].rstrip() + "..."


def _llm_text(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    return text if text else "unknown"


def summary_line(
    origin: str,
    model: str,
    exit_code: int | str,
    duration: float,
    sent: bool | None,
    instruction: str,
    *,
    max_chars: int = SUMMARY_MAX_CHARS,
    llm_calls: object = None,
    llm: object = None,
    llm_types: object = None,
    types: object = None,
    tools: object = None,
) -> str:
    """Build the single-line container-log summary for one run.

    ``origin`` is ``inbound:<source>`` or ``schedule``.  The instruction is
    flattened to one line, capped at about 80 characters with an ellipsis, and
    the whole line is at most ``max_chars`` even when the source name or model
    is long.  The exit/duration/sent fields are never dropped: a long source or
    model is shortened first.
    """

    if sent is None:
        sent_text = "unknown"
    else:
        sent_text = "yes" if sent else "no"
    origin_text = " ".join(str(origin).split())
    model_text = " ".join(str(model).split()) or "unknown"
    stamp = _stamp()
    llm_value = _llm_text(llm if llm is not None else llm_calls)
    types_value = types if types is not None else llm_types
    types_text = "" if types_value is None else " ".join(str(types_value).split())
    tail = f"exit={exit_code} {duration:.1f}s sent={sent_text} llm={llm_value}"
    if types_text:
        tail += f" types={types_text}"
    def build(run_field: str, model_field: str, excerpt: str) -> str:
        return (
            f'{stamp} run={run_field} model={model_field} {tail} '
            f'"{excerpt}"'
        )

    empty = build(origin_text, model_text, "")
    if len(empty) > max_chars:
        model_room = max_chars - len(build(origin_text, "", ""))
        model_text = _shorten(model_text, model_room)
        empty = build(origin_text, model_text, "")
    if len(empty) > max_chars:
        origin_room = max_chars - len(build("", model_text, ""))
        origin_text = _shorten(origin_text, origin_room)
        empty = build(origin_text, model_text, "")

    excerpt_room = max_chars - (len(empty) - 3) - 3
    limit = min(SUMMARY_INSTRUCTION_CHARS, max(0, excerpt_room))
    excerpt = flatten_instruction(instruction, limit) if limit > 0 else ""
    line = build(origin_text, model_text, excerpt)
    return line.replace("\r", " ").replace("\n", " ")


def print_summary(
    origin: str,
    model: str,
    exit_code: int | str,
    duration: float,
    sent: bool | None,
    instruction: str,
    *,
    stream: Any = None,
    max_chars: int = SUMMARY_MAX_CHARS,
) -> None:
    """Write exactly one summary line to the stream that reaches docker logs."""

    target = sys.stderr if stream is None else stream
    line = summary_line(
        origin,
        model,
        exit_code,
        duration,
        sent,
        instruction,
        max_chars=max_chars,
    )
    target.write(line + "\n")
    target.flush()


def record_start(
    path: Path,
    origin: str,
    model: str,
    instruction: str,
    *,
    max_bytes: int = MAX_BYTES,
) -> tuple[int | None, int]:
    """Write the header and return ``(fd, output_budget)``.

    ``fd`` is None when the log cannot be opened; callers then discard output
    exactly as before.  ``output_budget`` is the number of agent bytes that
    fit after the header, leaving room for the footer.
    """

    _reset_if_full(path, max_bytes)
    fd = _open_append(path)
    if fd is None:
        return None, 0

    try:
        size = os.fstat(fd).st_size
        available = max(0, max_bytes - size - FOOTER_RESERVE)
        header = (
            f"===== notify run {_stamp()} =====\n"
            f"path: {origin}\n"
            f"model: {model or 'unknown'}\n"
            "instruction:\n"
            f"{instruction}\n"
            "--- agent output ---\n"
        ).encode("utf-8", "replace")
        if len(header) > available:
            note = b"\n[header truncated at the log size cap]\n"
            header = header[: max(0, available - len(note))] + note
        _write_all(fd, header)
        budget = max(0, max_bytes - os.fstat(fd).st_size - FOOTER_RESERVE)
        return fd, budget
    except OSError:
        os.close(fd)
        return None, 0


def drain_output(
    stream: Any,
    fd: int,
    budget: int,
    *,
    max_bytes: int = MAX_BYTES,
) -> None:
    """Copy at most ``budget`` bytes to the log, discard the rest, and close.

    The pipe is always drained, so a chatty agent can never block on a full
    pipe, and the file can never grow past its cap.
    """

    truncated = False
    remaining = budget
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            if remaining <= 0:
                truncated = True
                continue
            piece = chunk[:remaining]
            _write_all(fd, piece)
            remaining -= len(piece)
            if len(piece) < len(chunk):
                truncated = True
    except (OSError, ValueError):
        pass
    finally:
        if truncated:
            note = (
                f"\n[agent output truncated at {max_bytes} bytes]\n"
            ).encode("utf-8")
            try:
                os.write(fd, note)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass


def record_end(
    path: Path,
    exit_code: int | str,
    duration: float,
    sent: bool | None,
    *,
    max_bytes: int = MAX_BYTES,
    llm_calls: object = None,
    llm: object = None,
    llm_types: object = None,
    types: object = None,
    tools: object = None,
) -> None:
    """Append the footer, trimming the tail only if it would overflow."""

    if sent is None:
        sent_text = "unknown"
    else:
        sent_text = "yes" if sent else "no"
    llm_value = _llm_text(llm if llm is not None else llm_calls)
    types_value = types if types is not None else llm_types
    types_text = "" if types_value is None else " ".join(str(types_value).split())
    tools_value = tools
    tools_text = "" if tools_value is None else " ".join(str(tools_value).split())
    footer_parts = [
        f"--- run end: exit={exit_code} duration={duration:.1f}s",
        f"sent={sent_text}",
        f"llm={llm_value}",
    ]
    if types_text:
        footer_parts.append(f"types={types_text}")
    if tools_text:
        footer_parts.append(f"tools={tools_text}")
    footer = (" ".join(footer_parts) + " ---\n").encode("utf-8", "replace")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    except OSError:
        return
    try:
        os.fchmod(fd, 0o600)
        size = os.fstat(fd).st_size
        if size + len(footer) > max_bytes:
            os.ftruncate(fd, max(0, max_bytes - len(footer)))
        _write_all(fd, footer)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _finish_capture(thread: threading.Thread | None) -> None:
    if thread is not None:
        thread.join(timeout=5.0)


def _discard_marker(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _run(command: Sequence[str], args: argparse.Namespace) -> int:
    log = Path(args.log)
    marker = Path(args.marker) if args.marker else None
    fd, budget = record_start(
        log, args.origin, args.model, args.instruction
    )
    capture = fd is not None
    started = time.monotonic()

    environment = os.environ.copy()
    if marker is not None:
        environment["NOTIFY_SEND_MARKER"] = str(marker)

    try:
        process = subprocess.Popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture else subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        record_end(log, "start-failed", 0.0, False)
        print(
            f"run-prompt: agent could not start: {type(error).__name__}",
            file=sys.stderr,
        )
        print_summary(
            args.origin, args.model, "start-failed", 0.0, False,
            args.instruction,
        )
        return 127

    drain: threading.Thread | None = None
    if capture:
        stream = getattr(process, "stdout", None)
        if stream is None:
            try:
                os.close(fd)
            except OSError:
                pass
        else:
            drain = threading.Thread(
                target=drain_output,
                args=(stream, fd, budget),
                name="notify-agent-log",
                daemon=True,
            )
            drain.start()

    def _stop(signum: int, _frame: object) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            pass
        _discard_marker(marker)
        elapsed = time.monotonic() - started
        record_end(
            log,
            f"signal-{signum}",
            elapsed,
            False,
        )
        print_summary(
            args.origin,
            args.model,
            f"signal-{signum}",
            elapsed,
            False,
            args.instruction,
        )
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            pass

    returncode = process.wait()
    _finish_capture(drain)
    elapsed = time.monotonic() - started
    sent = marker.exists() if marker is not None else None
    record_end(log, returncode, elapsed, sent)
    print_summary(
        args.origin, args.model, returncode, elapsed, sent, args.instruction
    )
    _discard_marker(marker)
    return returncode


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an agent command with a bounded debug transcript."
    )
    parser.add_argument("--log", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--instruction", required=True)
    parser.add_argument("--marker", default="")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("a command to run is required")
    return _run(command, args)


if __name__ == "__main__":
    raise SystemExit(main())

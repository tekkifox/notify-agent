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
) -> None:
    """Append the footer, trimming the tail only if it would overflow."""

    if sent is None:
        sent_text = "unknown"
    else:
        sent_text = "yes" if sent else "no"
    footer = (
        f"--- run end: exit={exit_code} duration={duration:.1f}s "
        f"sent={sent_text} ---\n"
    ).encode("utf-8", "replace")
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
        record_end(
            log,
            f"signal-{signum}",
            time.monotonic() - started,
            False,
        )
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, _stop)
        except (ValueError, OSError):
            pass

    returncode = process.wait()
    _finish_capture(drain)
    sent = marker.exists() if marker is not None else None
    record_end(log, returncode, time.monotonic() - started, sent)
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

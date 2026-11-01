#!/usr/bin/env python3
"""Fetch one host's recent statistics before the model runs.

Scheduled jobs must not depend on an MCP tool being registered before the
first model turn. This script opens the database directly, runs one fixed
aggregate, and prints a compact block the wrapper can paste into the
instruction. Ad-hoc jobs keep using the MCP tools.

Usage:

    host-stats.py --host HOST [--minutes 70]

Three outcomes, deliberately distinct:

* samples found        -> ``result=ok`` block on stdout, exit 0;
* query succeeded, no samples in the window
                       -> ``result=empty`` block on stdout, exit 0;
* anything failed      -> a short secret-free reason on stderr, exit 1.

Stdout is key=value lines only: no prose, no SQL, no instructions. Connection
settings are never printed.

The connection path is not reimplemented here. ``mcp/oracle_query.py`` already
owns the thin-mode configuration, the PEM-only wallet staging (so the driver
cannot prompt for a passphrase), stdin detachment, ``SET TRANSACTION READ
ONLY`` and connection cleanup. This script loads that same file and uses its
``_config`` and ``_connection`` helpers, so the scheduled path cannot drift
into a weaker copy. The module is loaded by path because the image installs
``/app/scripts`` and ``/app/mcp`` as siblings; it is imported lazily so
``--help`` works without the Oracle driver.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import math
import os
import pathlib
import re
import sys
from types import ModuleType
from typing import Any, Sequence

DEFAULT_MINUTES = 70
MAX_MINUTES = 10_080

ORACLE_MODULE_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "mcp" / "oracle_query.py"
)

# One aggregate for the whole window. KEEP ... DENSE_RANK gives the timestamp
# of each peak without a second query or a second connection. The host and the
# window are bind variables; neither is interpolated into the statement.
HOST_STATS_SQL = """
SELECT COUNT(*) AS samples,
       MIN(sampled_at) AS first_sampled_at,
       MAX(sampled_at) AS last_sampled_at,
       MAX(cpu_percent) AS max_cpu_percent,
       MAX(sampled_at) KEEP (
           DENSE_RANK FIRST ORDER BY cpu_percent DESC NULLS LAST
       ) AS max_cpu_at,
       MAX(memory_percent) AS max_memory_percent,
       MAX(sampled_at) KEEP (
           DENSE_RANK FIRST ORDER BY memory_percent DESC NULLS LAST
       ) AS max_memory_at,
       MAX(disk_percent) AS max_disk_percent,
       MAX(sampled_at) KEEP (
           DENSE_RANK FIRST ORDER BY disk_percent DESC NULLS LAST
       ) AS max_disk_at
FROM NOTIFY_RO.HOST_STATS_V
WHERE host_label = :host
  AND sampled_at >= SYSTIMESTAMP - NUMTODSINTERVAL(:minutes, 'MINUTE')
"""

# Only a driver code may reach stderr. Everything else becomes a generic
# reason, so a DSN, user, password or wallet path cannot leak through an
# exception message.
_ERROR_CODE = re.compile(r"\b(ORA-\d{5}|DPY-\d{4,5})\b", re.IGNORECASE)

# Belt and braces for the constant ConfigError messages from the MCP module:
# if one ever gains a value, that value still cannot reach stderr.
_SECRET_ENV = (
    "ORACLE_USER",
    "ORACLE_PASSWORD",
    "ORACLE_DSN",
    "ORACLE_WALLET_DIR",
)

_ORACLE: ModuleType | None = None


class FetchError(RuntimeError):
    """A fetch failed and the message is safe to print."""


def _redact(text: str) -> str:
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if value:
            text = text.replace(value, "[redacted]")
    return text


def _reason(exc: BaseException) -> str:
    match = _ERROR_CODE.search(str(exc))
    if match:
        return f"oracle error {match.group(1).upper()}"
    return "oracle query failed"


def oracle_module() -> ModuleType:
    """Load and cache the sibling MCP module that owns the connection path."""

    global _ORACLE
    if _ORACLE is not None:
        return _ORACLE

    if not ORACLE_MODULE_PATH.is_file():
        raise FetchError("oracle helper is unavailable")

    spec = importlib.util.spec_from_file_location(
        "notify_oracle_query", ORACLE_MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise FetchError("oracle helper is unavailable")

    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise FetchError(_reason(exc)) from None

    _ORACLE = module
    return module


def _format_number(value: Any) -> str:
    if value is None:
        return "null"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "null"
    if not math.isfinite(number):
        return "null"
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_timestamp(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def _row_value(row: Sequence[Any], index: int) -> Any:
    return row[index] if index < len(row) else None


def render(host: str, minutes: int, row: Sequence[Any] | None) -> str:
    """Turn one aggregate row into the prompt-ready block."""

    if row is None:
        raise FetchError("unexpected query result")

    try:
        samples = int(_row_value(row, 0) or 0)
    except (TypeError, ValueError):
        raise FetchError("unexpected query result") from None

    header = [
        f"host={host}",
        f"requested_minutes={minutes}",
    ]

    if samples <= 0:
        return "\n".join(["result=empty", *header, "samples=0"])

    return "\n".join(
        [
            "result=ok",
            *header,
            f"samples={samples}",
            f"first_sampled_at={_format_timestamp(_row_value(row, 1))}",
            f"last_sampled_at={_format_timestamp(_row_value(row, 2))}",
            f"max_cpu_percent={_format_number(_row_value(row, 3))}",
            f"max_cpu_at={_format_timestamp(_row_value(row, 4))}",
            f"max_memory_percent={_format_number(_row_value(row, 5))}",
            f"max_memory_at={_format_timestamp(_row_value(row, 6))}",
            f"max_disk_percent={_format_number(_row_value(row, 7))}",
            f"max_disk_at={_format_timestamp(_row_value(row, 8))}",
        ]
    )


def fetch(host: str, minutes: int) -> str:
    """Query the view through the MCP server's read-only connection helpers."""

    oracle = oracle_module()
    config_error = getattr(oracle, "ConfigError", ())

    try:
        config = oracle._config()
        with oracle._connection(config) as cursor:
            # Binds only: the host label never becomes SQL text.
            cursor.execute(HOST_STATS_SQL, {"host": host, "minutes": minutes})
            row = cursor.fetchone()
    except config_error as exc:
        # The MCP module builds these messages from constants only.
        raise FetchError(_redact(str(exc))) from None
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(_reason(exc)) from None

    return render(host, minutes, row)


def _clean_host(raw: str) -> str:
    host = raw.strip()
    if not host:
        raise ValueError("--host must not be empty")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in host):
        raise ValueError("--host must not contain control characters")
    return host


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="host-stats.py",
        description=(
            "Print one host's recent statistics as key=value lines. "
            "Exit 0 with result=ok or result=empty; exit non-zero on failure."
        ),
    )
    parser.add_argument(
        "--host",
        required=True,
        help="host_label to report, for example vortexservers.co.uk",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=DEFAULT_MINUTES,
        help=(
            f"window size in minutes, 1-{MAX_MINUTES} "
            f"(default {DEFAULT_MINUTES})"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        host = _clean_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))

    if not 1 <= args.minutes <= MAX_MINUTES:
        parser.error(f"--minutes must be between 1 and {MAX_MINUTES}")

    try:
        block = fetch(host, args.minutes)
    except FetchError as exc:
        print(f"host-stats: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(block + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

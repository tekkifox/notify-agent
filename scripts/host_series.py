#!/usr/bin/env python3
"""Fetch a bucketed CPU/memory series from Oracle and render host charts.

The scheduled digest (``run-prompt.sh``) and the inbound Discord listener both
run this before the model starts, so the notification the model sends can carry
the charts as attachments. It is deliberately a separate process: a database or
Pillow failure must never take the caller down, and the caller can bound it with
a timeout.

The query is read-only and bounded by the caller:

* ``NOTIFY_RO.HOST_STATS_V`` only,
* the requested host labels (validated hostnames, never interpolated),
* a window between 5 minutes and 30 days.

Buckets are chosen so a chart has roughly 40-60 points: the window divided by
about 50, snapped to a whole number of minutes, hours or days.

Usage:
    host_series.py --out DIR --window MINUTES --host HOST [--host HOST ...]

Exit codes: 0 rendered (possibly zero charts if the window is empty), 1 query
or render failure, 2 usage error.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Sequence

import host_chart

MIN_WINDOW_MINUTES = 5
MAX_WINDOW_MINUTES = 30 * 24 * 60
MAX_HOSTS = 10
MAX_POINTS_PER_HOST = 60

# Whole minutes, hours and days. The smallest bucket that keeps at most 60
# points is chosen, so 70 minutes lands on 2-minute buckets (35 points) and a
# day on 30-minute buckets (48 points).
BUCKET_CANDIDATES = (
    1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 45,
    60, 90, 120, 180, 240, 360, 480, 720, 960,
    1440, 2160, 2880, 4320, 7200, 10080, 14400, 20160, 30240, 43200,
)

HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)
ERROR_CODE_RE = re.compile(r"(?:ORA-\d{5}|DPY-\d{4})")
THIN_WALLET_FILES = ("tnsnames.ora", "ewallet.pem", "sqlnet.ora")
JAVA_WALLET_FILES = frozenset({"cwallet.sso", "ewallet.p12"})


class SeriesError(RuntimeError):
    """The series could not be fetched or rendered."""


def bucket_minutes_for_window(window_minutes: int) -> int:
    """Pick the smallest bucket that yields at most 60 points."""

    for candidate in BUCKET_CANDIDATES:
        if window_minutes / candidate <= MAX_POINTS_PER_HOST:
            return candidate
    return BUCKET_CANDIDATES[-1]


def bucket_expression(minutes: int) -> str:
    """Return the Oracle expression for one bucket, aligned to UTC midnight."""

    if minutes >= 1440:
        days = max(1, minutes // 1440)
        return (
            "TRUNC(CAST(sampled_at AS DATE))"
            f" - MOD(TO_NUMBER(TO_CHAR(sampled_at, 'J')), {days})"
        )
    if minutes == 60:
        return "TRUNC(sampled_at, 'HH24')"
    return (
        "TRUNC(sampled_at, 'MI')"
        " - MOD(TO_NUMBER(TO_CHAR(sampled_at, 'HH24')) * 60"
        " + TO_NUMBER(TO_CHAR(sampled_at, 'MI')), "
        f"{minutes}) / 1440"
    )


def build_sql(hosts: Sequence[str], window_minutes: int) -> tuple[str, dict[str, Any]]:
    """Build the bounded, bucketed series query and its bind values."""

    bucket = bucket_expression(bucket_minutes_for_window(window_minutes))
    placeholders = ", ".join(f":host{index}" for index in range(len(hosts)))
    sql = (
        "SELECT host_label,\n"
        f"       {bucket} AS bucket,\n"
        "       AVG(cpu_percent) AS cpu,\n"
        "       AVG(memory_percent) AS memory\n"
        "FROM NOTIFY_RO.HOST_STATS_V\n"
        "WHERE sampled_at >= SYSTIMESTAMP - NUMTODSINTERVAL(:window, 'MINUTE')\n"
        f"  AND host_label IN ({placeholders})\n"
        f"GROUP BY host_label, {bucket}\n"
        "ORDER BY host_label, bucket"
    )
    binds: dict[str, Any] = {"window": window_minutes}
    for index, host in enumerate(hosts):
        binds[f"host{index}"] = host
    return sql, binds


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SeriesError("the series contained a non-numeric value")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SeriesError("the series contained a non-numeric value") from None


def parse_rows(
    columns: Sequence[str], rows: Sequence[Sequence[Any]], window_minutes: int
) -> dict[str, Any]:
    """Turn column/row output into the renderer's JSON payload."""

    names = [str(name).lower() for name in columns]
    try:
        host_index = names.index("host_label")
        bucket_index = names.index("bucket")
    except ValueError:
        raise SeriesError("the series response is missing a column") from None
    cpu_index = names.index("cpu") if "cpu" in names else None
    memory_index = names.index("memory") if "memory" in names else None

    points: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < len(names):
            raise SeriesError("the series response had a malformed row")
        host = row[host_index]
        if not isinstance(host, str) or not host:
            continue
        bucket = row[bucket_index]
        if bucket is None:
            continue
        points.setdefault(host, []).append(
            {
                "at": _iso(bucket),
                "cpu": _number(row[cpu_index]) if cpu_index is not None else None,
                "memory": (
                    _number(row[memory_index]) if memory_index is not None else None
                ),
            }
        )
    for host_points in points.values():
        host_points.sort(key=lambda point: point["at"])
    return {
        "window_minutes": window_minutes,
        "hosts": [
            {"host_label": host, "points": host_points}
            for host, host_points in points.items()
        ],
    }


@contextlib.contextmanager
def _wallet_directory(wallet: str) -> Iterator[str]:
    """Use only the thin-mode wallet files, as the MCP server does.

    A wallet that also carries Java-format files can make the thin driver ask
    for a PEM pass phrase, which would hang a listener with no terminal.
    """

    try:
        entries = set(os.listdir(wallet))
    except OSError:
        yield wallet
        return
    if "ewallet.pem" not in entries or not entries.intersection(JAVA_WALLET_FILES):
        yield wallet
        return
    with tempfile.TemporaryDirectory(prefix="notify-chart-wallet-") as staged:
        for name in THIN_WALLET_FILES:
            if name in entries:
                os.symlink(os.path.join(wallet, name), os.path.join(staged, name))
        yield staged


def fetch(hosts: Sequence[str], window_minutes: int) -> tuple[list[str], list[list[Any]]]:
    """Run the query directly, without the MCP row cap."""

    try:
        import oracledb
    except ImportError:
        raise SeriesError("the Oracle driver is not installed") from None

    user = os.environ.get("ORACLE_USER", "")
    password = os.environ.get("ORACLE_PASSWORD", "")
    dsn = os.environ.get("ORACLE_DSN", "")
    wallet = os.environ.get("ORACLE_WALLET_DIR", "")
    if not user or not password or not dsn:
        raise SeriesError("Oracle connection is not configured")
    if not wallet:
        raise SeriesError("ORACLE_WALLET_DIR is not set")

    oracledb.defaults.fetch_lobs = False
    sql, binds = build_sql(hosts, window_minutes)
    try:
        with _wallet_directory(wallet) as config_dir:
            with oracledb.connect(
                user=user,
                password=password,
                dsn=dsn,
                config_dir=config_dir,
                tcp_connect_timeout=10,
            ) as connection:
                connection.call_timeout = 15_000
                cursor = connection.cursor()
                try:
                    cursor.execute(sql, binds)
                    columns = [str(column[0]) for column in (cursor.description or [])]
                    rows = [list(row) for row in cursor.fetchall()]
                finally:
                    cursor.close()
    except SeriesError:
        raise
    except Exception as error:
        match = ERROR_CODE_RE.search(str(error))
        reason = f"oracle error {match.group(0)}" if match else "oracle query failed"
        raise SeriesError(reason) from None
    return columns, rows


def _validated_hosts(values: Sequence[str]) -> list[str]:
    hosts: list[str] = []
    for value in values:
        host = (value or "").strip().lower().rstrip(".")
        if not host or len(host) > 253 or not HOST_RE.match(host):
            raise SeriesError("a host label is not a valid hostname")
        if host not in hosts:
            hosts.append(host)
    if len(hosts) > MAX_HOSTS:
        hosts = hosts[:MAX_HOSTS]
    return hosts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--window", required=True, type=int)
    parser.add_argument("--host", action="append", default=[])
    arguments = parser.parse_args(argv)

    try:
        if not (MIN_WINDOW_MINUTES <= arguments.window <= MAX_WINDOW_MINUTES):
            raise SeriesError(
                f"the window must be between {MIN_WINDOW_MINUTES} minutes "
                f"and {MAX_WINDOW_MINUTES // 1440} days"
            )
        hosts = _validated_hosts(arguments.host)
        if not hosts:
            raise SeriesError("at least one host is required")
        columns, rows = fetch(hosts, arguments.window)
        payload = parse_rows(columns, rows, arguments.window)
        if not any(host["points"] for host in payload["hosts"]):
            print(
                "host_series: no samples for the requested window; no chart written",
                file=sys.stderr,
            )
            return 0
        written = host_chart.render_payload(payload, arguments.out)
        print(f"host_series: wrote {len(written)} chart(s)", file=sys.stderr)
        return 0
    except (SeriesError, host_chart.ChartError) as error:
        print(f"host_series: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("host_series: could not write the chart directory", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

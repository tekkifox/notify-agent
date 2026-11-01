#!/usr/bin/env python3
"""Deterministic digest formatting for notify-agent.

This is the reference shape for the morning digest, carried over from the old
``tools/host-report`` service (``report.py``) so the captain sees the same
per-host line every morning. It is a pure module: callers pass rows and ``now``
in, get a string out, and every formatting rule can be tested without a
database or network.

The rows are aggregate rows from ``NOTIFY_RO.HOST_STATS_V`` rather than the old
JSON sample documents. That changes where the numbers come from, not how they
are rendered:

* ``sample_count`` — number of samples in the 24-hour window
* ``last_seen`` — the newest ``sampled_at`` in that window
* ``cpu_avg`` / ``cpu_peak`` — average and maximum ``cpu_percent``
* ``memory_avg`` — average ``memory_percent`` (average only, deliberately)
* ``load_avg`` / ``load_peak`` — average and maximum ``load_avg``
* ``disk`` — ``disk_percent`` from the newest sample (latest only, deliberately)

The prompt asks the agent to run one aggregate SQL query and then render the
result with these rules. ``build_report`` and ``build_digest`` are the
executable specification; ``tests/test_host_report.py`` pins the ordering,
stale boundary, thresholds and truncation.

SSH results are optional and are folded into the same per-host line, so
dropping a line from the end drops both its statistics and its SSH status.
Policy refusals (``not_allowed`` / ``out_of_scope``) are never rendered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

#: Discord/notify.sh rejects messages longer than this many characters.
DISCORD_LIMIT = 2000

#: A host with no sample within this window is stale. The old service used
#: ``age > 20m``, so a sample exactly 20 minutes old is still fresh.
STALE_MINUTES = 20

#: How much history the report covers.
WINDOW_HOURS = 24

#: Inclusive thresholds. Exactly 90% is a problem, just like 90.1%.
CPU_PEAK_PERCENT = 90.0
MEMORY_PERCENT = 90.0
LOAD_PEAK = 8.0
DISK_PERCENT = 90.0

#: Mirrors the old ``HOST_REPORT_HOSTS`` default. Override in the prompt
#: instruction / environment; this is only the fallback.
KNOWN_HOSTS: tuple[str, ...] = (
    "rossmoney.me",
    "photos.rossmoney.me",
    "vortexservers.co.uk",
)

TRUNCATION_NOTICE = "\u2026 truncated: {count} host line(s) not shown"

# Statuses that mean "a policy refused this host", not "the host is healthy".
REFUSED_STATUSES = frozenset({"not_allowed", "out_of_scope", "denied"})


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    read = getattr(value, "read", None)
    if callable(read):
        return _as_text(read())
    return str(value)


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    text = _as_text(value).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return as_utc(parsed)


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _record(row: Mapping[str, Any] | Sequence[Any], columns: Sequence[str] | None) -> dict[str, Any]:
    """Normalise one row to lower-case keys.

    Accepts the mapping form used by the formatting tests and the
    ``{"columns": [...], "rows": [...]}`` shape returned by ``oracle_query``.
    """

    if isinstance(row, Mapping):
        return {str(key).lower(): value for key, value in row.items()}
    if columns is None:
        raise ValueError("columns are required for sequence rows")
    return {
        str(column).lower(): value
        for column, value in zip(columns, row)
    }


def rows_from_oracle(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Convert an ``oracle_query`` result payload to mapping rows."""

    columns = payload.get("columns")
    rows = payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("expected columns and rows")
    return [_record(row, columns) for row in rows if isinstance(row, (list, tuple))]


@dataclass
class HostSummary:
    host: str
    samples: int
    last_seen: datetime | None
    cpu_avg: float | None = None
    cpu_peak: float | None = None
    memory_avg: float | None = None
    load_avg: float | None = None
    load_peak: float | None = None
    disk: float | None = None
    stale: bool = False
    flags: list[str] = field(default_factory=list)


def _summarise(host: str, record: Mapping[str, Any] | None, *, now: datetime, stale_minutes: int) -> HostSummary:
    if record is None:
        summary = HostSummary(host=host, samples=0, last_seen=None)
    else:
        summary = HostSummary(
            host=host,
            samples=_as_int(record.get("sample_count", record.get("samples"))),
            last_seen=parse_timestamp(
                record.get("last_seen", record.get("sampled_at"))
            ),
            cpu_avg=_as_float(record.get("cpu_avg")),
            cpu_peak=_as_float(record.get("cpu_peak")),
            memory_avg=_as_float(record.get("memory_avg")),
            load_avg=_as_float(record.get("load_avg")),
            load_peak=_as_float(record.get("load_peak")),
            disk=_as_float(record.get("disk")),
        )

    age = None if summary.last_seen is None else now - summary.last_seen
    summary.stale = age is None or age > timedelta(minutes=stale_minutes)

    if summary.stale:
        summary.flags.append(f"STALE: no sample in the last {stale_minutes}m")
    if summary.cpu_peak is not None and summary.cpu_peak >= CPU_PEAK_PERCENT:
        summary.flags.append(
            f"CPU peak {summary.cpu_peak:.1f}% >= {CPU_PEAK_PERCENT:.0f}%"
        )
    if summary.memory_avg is not None and summary.memory_avg >= MEMORY_PERCENT:
        summary.flags.append(
            f"memory {summary.memory_avg:.1f}% >= {MEMORY_PERCENT:.0f}%"
        )
    if summary.load_peak is not None and summary.load_peak >= LOAD_PEAK:
        summary.flags.append(
            f"load peak {summary.load_peak:.2f} >= {LOAD_PEAK:.0f}"
        )
    if summary.disk is not None and summary.disk >= DISK_PERCENT:
        summary.flags.append(
            f"disk {summary.disk:.1f}% >= {DISK_PERCENT:.0f}%"
        )
    return summary


def _age(value: datetime | None, now: datetime) -> str:
    if value is None:
        return "unknown"
    seconds = int((now - value).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes}m ago"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h ago"


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}%"


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _ssh_text(result: Mapping[str, Any] | None) -> str:
    if result is None:
        return ""
    status = str(result.get("status", "")).strip().lower()
    if not status or status in REFUSED_STATUSES:
        return ""
    if status == "not_configured":
        return ""
    if status == "ok":
        latency = result.get("latency_ms")
        try:
            latency_text = f"{int(latency)} ms"
        except (TypeError, ValueError):
            latency_text = "latency unknown"
        return f"; SSH ok ({latency_text})"
    message = " ".join(str(result.get("message", "")).split())
    if message:
        return f"; SSH {status} ({message})"
    return f"; SSH {status}"


def _host_line(summary: HostSummary, now: datetime, ssh: Mapping[str, Any] | None = None) -> str:
    marker = "[!] " if summary.flags else ""
    if summary.samples == 0:
        body = f"no samples in the last {WINDOW_HOURS}h; last-seen unknown"
    else:
        noun = "sample" if summary.samples == 1 else "samples"
        body = (
            f"{summary.samples} {noun}, last-seen {_age(summary.last_seen, now)}"
            f"; CPU {_percent(summary.cpu_avg)} avg / {_percent(summary.cpu_peak)} peak"
            f"; mem {_percent(summary.memory_avg)} avg"
            f"; load {_number(summary.load_avg)} avg / {_number(summary.load_peak)} peak"
            f"; disk {_percent(summary.disk)}"
        )
    body += _ssh_text(ssh)
    if summary.flags:
        body += " (" + "; ".join(summary.flags) + ")"
    return f"- {marker}{summary.host}: {body}"


def _ordered_hosts(
    records: Iterable[Mapping[str, Any]],
    known_hosts: Sequence[str],
) -> tuple[list[str], dict[str, Mapping[str, Any]]]:
    by_host: dict[str, Mapping[str, Any]] = {}
    extras: set[str] = set()
    for record in records:
        host = _as_text(record.get("host_label", record.get("host"))).strip()
        if not host:
            continue
        by_host[host] = record
        extras.add(host)

    ordered: list[str] = []
    for host in known_hosts:
        if host not in ordered:
            ordered.append(host)
    for host in sorted(extras):
        if host not in ordered:
            ordered.append(host)
    return ordered, by_host


def _fit(header: str, lines: Sequence[str], limit: int) -> str:
    joined = header + "\n" + "\n".join(lines)
    if len(joined) <= limit:
        return joined

    for keep in range(len(lines), -1, -1):
        notice = "\n" + TRUNCATION_NOTICE.format(count=len(lines) - keep)
        candidate = header + "\n" + "\n".join(lines[:keep]) + notice
        if len(candidate) <= limit:
            return candidate

    fallback = header + "\n" + TRUNCATION_NOTICE.format(count=len(lines))
    return fallback[: max(limit - 1, 0)] + "\u2026"


def _prepare(
    rows: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    now: datetime | None,
    known_hosts: Iterable[str] | None,
    columns: Sequence[str] | None,
    stale_minutes: int,
) -> tuple[datetime, list[str], list[HostSummary], dict[str, Mapping[str, Any]]]:
    current = as_utc(now or datetime.now(timezone.utc))
    since = current - timedelta(hours=WINDOW_HOURS)
    known = tuple(known_hosts) if known_hosts is not None else KNOWN_HOSTS

    records = [_record(row, columns) for row in rows]
    filtered = [
        record
        for record in records
        if (parse_timestamp(record.get("last_seen", record.get("sampled_at"))) is None
            or parse_timestamp(record.get("last_seen", record.get("sampled_at"))) >= since)
    ]
    ordered, by_host = _ordered_hosts(filtered, known)
    summaries = [
        _summarise(
            host,
            by_host.get(host),
            now=current,
            stale_minutes=stale_minutes,
        )
        for host in ordered
    ]
    return current, ordered, summaries, by_host


def build_report(
    rows: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    now: datetime | None = None,
    known_hosts: Iterable[str] | None = None,
    columns: Sequence[str] | None = None,
    limit: int = DISCORD_LIMIT,
    stale_minutes: int = STALE_MINUTES,
) -> str:
    """Render the host-statistics part of the digest.

    Every known host gets a line even with no rows; any other host in the data
    is appended in name order, so the result is deterministic. Host lines are
    dropped from the end when the report would exceed ``limit``, and the
    truncation notice names how many were omitted.
    """

    current, _ordered, summaries, _by_host = _prepare(
        rows,
        now=now,
        known_hosts=known_hosts,
        columns=columns,
        stale_minutes=stale_minutes,
    )

    reported = sum(1 for summary in summaries if summary.samples)
    stale = sum(1 for summary in summaries if summary.stale)
    flagged = sum(1 for summary in summaries if summary.flags)

    header = (
        f"**Host report** \u2014 {current.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"{reported}/{len(summaries)} hosts reported, {stale} stale, "
        f"{flagged} flagged"
    )
    lines = [_host_line(summary, current) for summary in summaries]
    return _fit(header, lines, limit)


def build_digest(
    rows: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    ssh_results: Iterable[Mapping[str, Any]] | None = None,
    ssh_configured: bool = True,
    now: datetime | None = None,
    known_hosts: Iterable[str] | None = None,
    columns: Sequence[str] | None = None,
    limit: int = DISCORD_LIMIT,
    stale_minutes: int = STALE_MINUTES,
) -> str:
    """Render host statistics plus one SSH status per host.

    The SSH result for a host is folded into that host's line, so truncation
    never separates a host's statistics from its SSH status. Refused statuses
    (``not_allowed`` / ``out_of_scope`` / ``denied``) are omitted. Hosts absent
    from ``ssh_results`` simply have no SSH text, which is what the prompt asks
    for when a host was not probed.
    """

    current, ordered, summaries, _by_host = _prepare(
        rows,
        now=now,
        known_hosts=known_hosts,
        columns=columns,
        stale_minutes=stale_minutes,
    )

    ssh_by_host: dict[str, Mapping[str, Any]] = {}
    for result in ssh_results or ():
        host = _as_text(result.get("host")).strip().lower()
        if host:
            ssh_by_host[host] = result

    reported = sum(1 for summary in summaries if summary.samples)
    stale = sum(1 for summary in summaries if summary.stale)
    flagged = sum(1 for summary in summaries if summary.flags)

    summary_line = (
        f"{reported}/{len(summaries)} hosts reported, {stale} stale, "
        f"{flagged} flagged"
    )
    header = (
        f"**Host report** \u2014 {current.strftime('%Y-%m-%d %H:%M')} UTC\n"
        f"{summary_line}"
    )
    if not ssh_configured:
        header += "\nSSH not configured; no SSH health was checked."

    lines = [
        _host_line(
            summary,
            current,
            ssh_by_host.get(summary.host.lower()),
        )
        for summary in summaries
    ]
    return _fit(header, lines, limit)

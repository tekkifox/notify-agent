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
* ``cpu_cores`` — the cores the host reported; the load threshold is relative
  to this, so a null count means the load rule cannot apply
* ``cpu_avg`` / ``cpu_peak`` — average and maximum ``cpu_percent``
* ``memory_avg`` — average ``memory_percent`` (the sustained memory signal)
* ``swap_avg`` / ``swap_peak`` — average and maximum ``swap_percent``
* ``memory_used_bytes_latest`` / ``memory_total_bytes`` — memory used in the
  newest sample and the host's total
* ``process_count_latest`` — processes in the newest sample
* ``network_rx_mb_latest`` / ``network_tx_mb_latest`` — network in and out in
  the newest sample
* ``host_count`` — hosts covered by the snapshot
* ``updated_at_latest`` — when the snapshot was rolled up
* ``load_avg`` / ``load_peak`` — average and maximum ``load_avg``
* ``disk`` — ``disk_percent`` from the newest sample (latest only, deliberately)

Memory figures in user-facing output are reported in gigabytes. When byte
amounts are available the report shows memory use and totals in GB; otherwise it
falls back to the memory percentage (a whole percent). CPU and disk are
percentages (whole percent, no decimals). Timestamps are shown as the time when
the date is today, or as the date and time when it differs from today, in
``d/m/Y H:i`` format.

Flagging follows sustained signals only. CPU and load peaks are reported as
context but never cause a flag by themselves: a host is flagged for stale data
(no sample in 20 minutes), average CPU >= 60%, average memory >= 85%, disk >=
95%, or load peak >= twice the host's core count when core count is known.

The prompt asks the agent to run one aggregate SQL query and then render the
result with these rules. ``build_report`` and ``build_digest`` are the
executable specification; ``tests/test_host_report.py`` pins the ordering,
stale boundary, sustained thresholds and truncation.

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

#: Inclusive sustained-signal thresholds. Exactly at the boundary is a problem.
#: CPU and load peaks are reported as context and are deliberately not
#: thresholds on their own.
CPU_AVG_PERCENT = 60.0
MEMORY_PERCENT = 85.0
DISK_PERCENT = 95.0

#: The load peak flags a host only at or above this multiple of its own core
#: count, as a sustained-capacity signal. A CPU or load peak is still reported
#: as context and never flags by itself. A null core count makes the rule
#: inapplicable — the formatter never guesses one.
LOAD_CORES_MULTIPLIER = 2

BYTES_PER_GB = 1024 ** 3

#: Mirrors the old ``HOST_REPORT_HOSTS`` default. Override in the prompt
#: instruction / environment; this is only the fallback.
KNOWN_HOSTS: tuple[str, ...] = (
    "host1.example.com",
    "host2.example.com",
    "host3.example.com",
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


def _as_cores(value: Any) -> int | None:
    """Return a positive core count, or ``None`` when it is unknown."""

    number = _as_float(value)
    if number is None:
        return None
    cores = int(number)
    return cores if cores >= 1 else None


def _as_optional_int(value: Any) -> int | None:
    """Return a non-negative count, or ``None`` when the host did not report it."""

    number = _as_float(value)
    if number is None:
        return None
    count = int(number)
    return count if count >= 0 else None


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
    cpu_cores: int | None = None
    cpu_avg: float | None = None
    cpu_peak: float | None = None
    memory_avg: float | None = None
    memory_used_bytes: float | None = None
    memory_total_bytes: float | None = None
    swap_avg: float | None = None
    swap_peak: float | None = None
    process_count: int | None = None
    network_rx_mb: float | None = None
    network_tx_mb: float | None = None
    host_count: int | None = None
    updated_at: datetime | None = None
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
            cpu_cores=_as_cores(record.get("cpu_cores")),
            cpu_avg=_as_float(record.get("cpu_avg")),
            cpu_peak=_as_float(record.get("cpu_peak")),
            memory_avg=_as_float(record.get("memory_avg")),
            memory_used_bytes=_as_float(
                record.get("memory_used_bytes_latest", record.get("memory_used_bytes"))
            ),
            memory_total_bytes=_as_float(record.get("memory_total_bytes")),
            swap_avg=_as_float(record.get("swap_avg")),
            swap_peak=_as_float(record.get("swap_peak")),
            process_count=_as_optional_int(
                record.get("process_count_latest", record.get("process_count"))
            ),
            network_rx_mb=_as_float(
                record.get("network_rx_mb_latest", record.get("network_rx_mb"))
            ),
            network_tx_mb=_as_float(
                record.get("network_tx_mb_latest", record.get("network_tx_mb"))
            ),
            host_count=_as_optional_int(record.get("host_count")),
            updated_at=parse_timestamp(
                record.get("updated_at_latest", record.get("updated_at"))
            ),
            load_avg=_as_float(record.get("load_avg")),
            load_peak=_as_float(record.get("load_peak")),
            disk=_as_float(record.get("disk", record.get("disk_latest"))),
        )

    age = None if summary.last_seen is None else now - summary.last_seen
    summary.stale = age is None or age > timedelta(minutes=stale_minutes)

    if summary.stale:
        summary.flags.append(f"STALE: no sample in the last {stale_minutes}m")
    if summary.cpu_avg is not None and summary.cpu_avg >= CPU_AVG_PERCENT:
        summary.flags.append(
            f"CPU avg {_percent(summary.cpu_avg)} >= {CPU_AVG_PERCENT:.0f}%"
        )
    if summary.memory_avg is not None and summary.memory_avg >= MEMORY_PERCENT:
        avg_used = _average_memory_used_bytes(summary)
        if avg_used is not None and summary.memory_total_bytes is not None:
            summary.flags.append(
                "memory "
                f"{_gigabytes(avg_used)} avg / {_gigabytes(summary.memory_total_bytes)}, "
                f"{_percent(summary.memory_avg)} >= {MEMORY_PERCENT:.0f}%"
            )
        elif avg_used is not None:
            summary.flags.append(
                "memory "
                f"{_gigabytes(avg_used)} avg, "
                f"{_percent(summary.memory_avg)} >= {MEMORY_PERCENT:.0f}%"
            )
        else:
            summary.flags.append(
                f"memory {_percent(summary.memory_avg)} >= {MEMORY_PERCENT:.0f}%"
            )
    if summary.cpu_cores is not None and summary.load_peak is not None:
        cores = summary.cpu_cores
        threshold = LOAD_CORES_MULTIPLIER * cores
        if summary.load_peak >= threshold:
            summary.flags.append(
                f"load peak {summary.load_peak:.2f} >= "
                f"{threshold:g} ({LOAD_CORES_MULTIPLIER}x{cores} {_cores_noun(cores)})"
            )
    if summary.disk is not None and summary.disk >= DISK_PERCENT:
        summary.flags.append(
            f"disk {_percent(summary.disk)} >= {DISK_PERCENT:.0f}%"
        )
    return summary


def _age(value: datetime | None, now: datetime) -> str:
    """Return a recency timestamp for ``value`` relative to ``now``.

    The time alone is shown when ``value`` falls on the same UTC day as ``now``;
    otherwise the date and time are shown. Both use ``d/m/Y H:i``.
    """

    if value is None:
        return "unknown"
    if value.date() == now.date():
        return value.strftime("%H:%M")
    return value.strftime("%d/%m/%Y %H:%M")


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def _gigabytes(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value / BYTES_PER_GB:.2f} GB"


def _average_memory_used_bytes(summary: HostSummary) -> float | None:
    """Derive average memory used from the average percent and total bytes."""

    if summary.memory_avg is None or summary.memory_total_bytes is None:
        return None
    return summary.memory_total_bytes * (summary.memory_avg / 100.0)


def _memory_text(summary: HostSummary) -> str:
    avg_used = _average_memory_used_bytes(summary)
    if avg_used is not None and summary.memory_total_bytes is not None:
        return (
            f"mem {_gigabytes(avg_used)} avg / "
            f"{_gigabytes(summary.memory_total_bytes)}"
        )
    if avg_used is not None:
        return f"mem {_gigabytes(avg_used)} avg"
    if (
        summary.memory_used_bytes is not None
        and summary.memory_total_bytes is not None
    ):
        return (
            f"mem {_gigabytes(summary.memory_used_bytes)} / "
            f"{_gigabytes(summary.memory_total_bytes)}"
        )
    if summary.memory_used_bytes is not None:
        return f"mem {_gigabytes(summary.memory_used_bytes)}"
    if summary.memory_avg is not None:
        return f"mem {_percent(summary.memory_avg)} avg"
    return "mem n/a"


def _number(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _cores_noun(cores: int) -> str:
    return "core" if cores == 1 else "cores"


def _cores_text(cores: int | None) -> str:
    return "cores n/a" if cores is None else f"{cores} {_cores_noun(cores)}"


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


def _summary_line(summaries: Sequence[HostSummary]) -> str:
    reported = sum(1 for summary in summaries if summary.samples)
    stale = sum(1 for summary in summaries if summary.stale)
    flagged = sum(1 for summary in summaries if summary.flags)
    return (
        f"{reported}/{len(summaries)} hosts reported, {stale} stale, "
        f"{flagged} flagged"
    )


def _host_line(summary: HostSummary, now: datetime, ssh: Mapping[str, Any] | None = None) -> str:
    marker = "[!] " if summary.flags else ""
    if summary.samples == 0:
        body = f"no samples in the last {WINDOW_HOURS}h; last-seen unknown"
    else:
        noun = "sample" if summary.samples == 1 else "samples"
        body = (
            f"{summary.samples} {noun}, last-seen {_age(summary.last_seen, now)}"
            f"; {_cores_text(summary.cpu_cores)}"
            f"; CPU {_percent(summary.cpu_avg)} avg / {_percent(summary.cpu_peak)} peak"
            f"; {_memory_text(summary)}"
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

    header = (
        f"**Host report** \u2014 {current.strftime('%d/%m/%Y %H:%M')} UTC\n"
        f"{_summary_line(summaries)}"
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

    header = (
        f"**Host report** \u2014 {current.strftime('%d/%m/%Y %H:%M')} UTC\n"
        f"{_summary_line(summaries)}"
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

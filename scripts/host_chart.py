#!/usr/bin/env python3
"""Render CPU and memory line charts for the host digests.

Input is JSON (a path on the command line, or stdin when the path is ``-`` or
omitted)::

    {"window_minutes": 70,
     "hosts": [{"host_label": "media.example.com",
                "points": [{"at": "2026-11-11T09:00:00+00:00",
                             "cpu": 12.3, "memory": 40.1}, ...]}]}

One PNG is written per host into the directory given on the command line.  A
host with no points produces no file.  A null cpu or memory breaks the line
instead of drawing through zero.

Only Pillow and the standard library are used.  The image is drawn at twice the
target size and downsampled, so the lines stay smooth at the size Discord
shows inline and the file stays small.

Usage:
    host_chart.py OUTPUT_DIR [INPUT]

Exit codes: 0 rendered (possibly zero charts), 1 bad input or write failure,
2 usage error.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - handled loudly at the entry point
    Image = ImageDraw = ImageFont = None  # type: ignore[assignment]

# Target image size. The drawing happens at SCALE times this and is resized
# down, which is the cheapest way to get anti-aliased lines with Pillow.
WIDTH = 900
HEIGHT = 520
SCALE = 2

BACKGROUND = (15, 23, 42)
PANEL_BACKGROUND = (17, 24, 39)
GRID = (51, 65, 85)
AXIS = (100, 116, 139)
TEXT = (203, 213, 225)
MUTED = (148, 163, 184)
TITLE = (241, 245, 249)
CPU_COLOUR = (34, 197, 94)
MEMORY_COLOUR = (245, 158, 11)

LEFT = 78
RIGHT = 22
TOP = 66
BOTTOM = 44
GAP = 30
PANEL_HEIGHT = (HEIGHT - TOP - BOTTOM - GAP) // 2

PERCENT_MIN = 0.0
PERCENT_MAX = 100.0
Y_TICKS = 4
X_TICKS = 5

SLUG_RE = re.compile(r"[^a-z0-9.-]+")


class ChartError(ValueError):
    """The input cannot be rendered."""


def _font(size: int, *, bold: bool = False):
    """Return a scalable font when one is available, else Pillow's default."""

    if ImageFont is None:
        raise ChartError("Pillow is not installed")
    names = ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",)
    directories = (
        "/usr/share/fonts/truetype/dejavu",
        "/usr/share/fonts/dejavu",
        "/usr/local/share/fonts",
    )
    for directory in directories:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                try:
                    return ImageFont.truetype(path, size)
                except OSError:
                    continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Pillow before 10.1 has no sized default font.
        return ImageFont.load_default()


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ChartError("a point is missing its timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise ChartError("a point has an invalid timestamp") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _number(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartError(f"{name} must be a number or null")
    number = float(value)
    if not math.isfinite(number):
        raise ChartError(f"{name} must be finite")
    # Percent series must never leave 0-100; clamp instead of drawing outside.
    return min(PERCENT_MAX, max(PERCENT_MIN, number))


def _parse_host(entry: object) -> tuple[str, list[tuple[datetime, float | None, float | None]]]:
    if not isinstance(entry, dict):
        raise ChartError("each host must be an object")
    label = entry.get("host_label")
    if not isinstance(label, str) or not label.strip():
        raise ChartError("each host needs a host_label")
    label = label.strip()
    raw_points = entry.get("points", [])
    if raw_points is None:
        raw_points = []
    if not isinstance(raw_points, list):
        raise ChartError(f"points for {label} must be a list")

    points: list[tuple[datetime, float | None, float | None]] = []
    for item in raw_points:
        if not isinstance(item, dict):
            raise ChartError(f"a point for {label} is not an object")
        moment = _parse_time(item.get("at"))
        cpu = _number(item.get("cpu"), "cpu")
        memory = _number(item.get("memory"), "memory")
        points.append((moment, cpu, memory))
    points.sort(key=lambda point: point[0])
    return label, points


def parse_payload(payload: object) -> tuple[int, list[tuple[str, list[tuple[datetime, float | None, float | None]]]]]:
    """Validate the JSON payload and return ``(window_minutes, hosts)``."""

    if not isinstance(payload, dict):
        raise ChartError("input must be a JSON object")
    window = payload.get("window_minutes")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ChartError("window_minutes must be a positive whole number")
    raw_hosts = payload.get("hosts", [])
    if raw_hosts is None:
        raw_hosts = []
    if not isinstance(raw_hosts, list):
        raise ChartError("hosts must be a list")
    return window, [_parse_host(entry) for entry in raw_hosts]


def slug_for(label: str) -> str:
    """A path-safe file stem for a host label."""

    slug = SLUG_RE.sub("-", label.strip().lower()).strip(".-")
    return slug or "host"


def format_window(minutes: int) -> str:
    if minutes <= 120:
        return f"{minutes} min"
    if minutes % 60 == 0:
        return f"{minutes // 60} h"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min"


def _range_for(values: list[float | None]) -> tuple[float, float]:
    """A padded 0-100 range that still shows a constant series as a band."""

    present = [value for value in values if value is not None]
    if not present:
        return PERCENT_MIN, PERCENT_MAX
    low, high = min(present), max(present)
    if high - low < 0.5:
        low -= 2.5
        high += 2.5
    else:
        padding = max((high - low) * 0.12, 0.5)
        low -= padding
        high += padding
    low = max(PERCENT_MIN, low)
    high = min(PERCENT_MAX, high)
    if high - low < 1.0:
        middle = (low + high) / 2
        low = max(PERCENT_MIN, middle - 0.5)
        high = min(PERCENT_MAX, middle + 0.5)
        if high - low < 1.0:
            if low == PERCENT_MIN:
                high = min(PERCENT_MAX, low + 1.0)
            elif high == PERCENT_MAX:
                low = max(PERCENT_MIN, high - 1.0)
    return low, high


def segments(
    points: list[tuple[datetime, float | None, float | None]], column: int
) -> list[list[tuple[datetime, float]]]:
    """Split one series into runs of consecutive non-null values.

    A null breaks the line.  ``column`` is 1 for CPU and 2 for memory.
    """

    runs: list[list[tuple[datetime, float]]] = []
    current: list[tuple[datetime, float]] = []
    for point in points:
        value = point[column]
        if value is None:
            if current:
                runs.append(current)
                current = []
            continue
        current.append((point[0], value))
    if current:
        runs.append(current)
    return runs


def _tick_label(value: float) -> str:
    if abs(value) < 0.05:
        value = 0.0
    return f"{value:.0f}%"


def _time_label(moment: datetime, span_seconds: float) -> str:
    if span_seconds > 36 * 3600:
        return moment.strftime("%d %b %H:%M")
    return moment.strftime("%H:%M")


def _draw_chart(
    label: str,
    points: list[tuple[datetime, float | None, float | None]],
    window: int,
) -> "Image.Image":
    scale = SCALE
    image = Image.new("RGB", (WIDTH * scale, HEIGHT * scale), BACKGROUND)
    draw = ImageDraw.Draw(image)

    title_font = _font(20 * scale, bold=True)
    panel_font = _font(13 * scale, bold=True)
    tick_font = _font(12 * scale)

    def px(value: float) -> float:
        return value * scale

    draw.text(
        (px(22), px(18)),
        f"{label} \u2014 last {format_window(window)}",
        font=title_font,
        fill=TITLE,
    )

    moments = [point[0] for point in points]
    if moments:
        start = min(moments)
        end = max(moments)
    else:
        end = datetime.now(timezone.utc)
        start = end
    span_seconds = (end - start).total_seconds()
    if span_seconds <= 0:
        span_seconds = float(window * 60)
        start = end - timedelta(seconds=span_seconds)

    panels = (
        ("CPU %", CPU_COLOUR, 1),
        ("Memory %", MEMORY_COLOUR, 2),
    )

    for index, (name, colour, column) in enumerate(panels):
        top = TOP + index * (PANEL_HEIGHT + GAP)
        bottom = top + PANEL_HEIGHT
        left = LEFT
        right = WIDTH - RIGHT

        draw.rectangle(
            (px(left), px(top), px(right), px(bottom)),
            fill=PANEL_BACKGROUND,
            outline=GRID,
            width=max(1, scale // 2),
        )
        draw.text(
            (px(left + 8), px(top + 6)),
            name,
            font=panel_font,
            fill=colour,
        )

        values = [point[column] for point in points]
        low, high = _range_for(values)

        # Horizontal grid and the percent scale.
        for tick in range(Y_TICKS):
            fraction = tick / (Y_TICKS - 1)
            value = low + (high - low) * fraction
            y = bottom - fraction * PANEL_HEIGHT
            draw.line(
                (px(left), px(y), px(right), px(y)),
                fill=GRID,
                width=max(1, scale // 2),
            )
            text = _tick_label(value)
            width = draw.textlength(text, font=tick_font)
            draw.text(
                (px(left - 8) - width, px(y - 7)),
                text,
                font=tick_font,
                fill=MUTED,
            )

        # Vertical grid, shared by both panels.
        for tick in range(X_TICKS):
            fraction = tick / (X_TICKS - 1)
            x = left + fraction * (right - left)
            draw.line(
                (px(x), px(top), px(x), px(bottom)),
                fill=GRID,
                width=max(1, scale // 3),
            )

        for run in segments(points, column):
            coordinates = []
            for moment, value in run:
                if span_seconds > 0:
                    fraction = (moment - start).total_seconds() / span_seconds
                else:
                    fraction = 0.5
                fraction = min(1.0, max(0.0, fraction))
                x = left + fraction * (right - left)
                y = bottom - ((value - low) / (high - low)) * PANEL_HEIGHT
                coordinates.append((px(x), px(y)))
            if len(coordinates) == 1:
                x, y = coordinates[0]
                radius = max(2, 2 * scale)
                draw.ellipse(
                    (x - radius, y - radius, x + radius, y + radius),
                    fill=colour,
                )
            else:
                draw.line(
                    coordinates,
                    fill=colour,
                    width=max(2, 2 * scale),
                    joint="curve",
                )

    # Time ticks under the bottom panel only.
    axis_y = HEIGHT - BOTTOM + 6
    for tick in range(X_TICKS):
        fraction = tick / (X_TICKS - 1)
        x = LEFT + fraction * ((WIDTH - RIGHT) - LEFT)
        moment = start + (end - start) * fraction
        text = _time_label(moment, span_seconds)
        width = draw.textlength(text, font=tick_font)
        draw.text(
            (px(x) - width / 2, px(axis_y)),
            text,
            font=tick_font,
            fill=MUTED,
        )

    return image.resize((WIDTH, HEIGHT), Image.LANCZOS)


def render_payload(payload: object, out_dir: str | os.PathLike[str]) -> list[Path]:
    """Render every host in the payload. Returns the written paths."""

    if Image is None:
        raise ChartError("Pillow is not installed")
    window, hosts = parse_payload(payload)
    directory = Path(out_dir)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ChartError("could not create the output directory") from None
    if not directory.is_dir():
        raise ChartError("the output path is not a directory")

    written: list[Path] = []
    used: set[str] = set()
    for label, points in hosts:
        if not points:
            continue
        stem = slug_for(label)
        candidate = stem
        suffix = 2
        while candidate in used:
            candidate = f"{stem}-{suffix}"
            suffix += 1
        used.add(candidate)

        target = directory / f"{candidate}.png"
        image = _draw_chart(label, points, window)
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=str(directory), prefix=".chart-", suffix=".png.tmp"
            )
            os.close(descriptor)
            image.save(temporary, format="PNG", optimize=True)
            os.replace(temporary, target)
            temporary = None
        except OSError:
            raise ChartError("could not write a chart image") from None
        finally:
            image.close()
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        written.append(target)
    return written


def _read_input(path: str | None) -> object:
    try:
        if path is None or path == "-":
            return json.load(sys.stdin)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError:
        raise ChartError("could not read the input") from None
    except json.JSONDecodeError:
        raise ChartError("input is not valid JSON") from None


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print("usage: host_chart.py OUTPUT_DIR [INPUT]", file=sys.stderr)
        return 2
    out_dir = argv[1]
    source = argv[2] if len(argv) == 3 else None
    try:
        written = render_payload(_read_input(source), out_dir)
    except ChartError as error:
        print(f"host_chart: {error}", file=sys.stderr)
        return 1
    if written:
        print(f"host_chart: wrote {len(written)} chart(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

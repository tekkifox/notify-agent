#!/usr/bin/env python3
"""Parse every active schedule line with ``sh -n``.

supercronic hands each schedule to ``/bin/sh``. A prompt whose quoting is
broken parses as an unterminated string, so the job fails on every run with
``Syntax error: Unterminated quoted string`` and never delivers. Text-level
contract tests cannot see that, because the prompt still reads correctly; this
test asks the shell that actually runs it.

Run with:
    python3 -m unittest discover -s tests -p 'test_*.py' -v
"""

from __future__ import annotations

import pathlib
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CRONTABS = ("crontab", "crontab.live")


def active_lines(text: str) -> list[tuple[int, str]]:
    """Return ``(line number, line)`` for every non-blank, non-comment line."""

    lines = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        lines.append((number, line))
    return lines


class CrontabSyntaxTests(unittest.TestCase):
    def test_every_active_schedule_parses(self) -> None:
        for name in CRONTABS:
            with self.subTest(crontab=name):
                path = ROOT / name
                lines = active_lines(path.read_text(encoding="utf-8"))
                self.assertTrue(lines, f"{name}: no active schedule lines")
                for number, line in lines:
                    with self.subTest(crontab=name, line=number):
                        result = subprocess.run(
                            ["sh", "-n"],
                            input=line + "\n",
                            text=True,
                            capture_output=True,
                        )
                        self.assertEqual(
                            result.returncode,
                            0,
                            f"{name} line {number} does not parse: "
                            f"{result.stderr.strip()}",
                        )


if __name__ == "__main__":
    unittest.main()

"""Source positions.

Lines and columns are 1-based; columns count Unicode code points (SPEC §15.1).
Ranges are half-open: ``end_col`` is one past the last character.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Span:
    start: int  # code-point offset into the (normalized) source
    end: int
    line: int
    col: int
    end_line: int
    end_col: int

    def to_json(self) -> dict:
        return {
            "line": self.line,
            "col": self.col,
            "endLine": self.end_line,
            "endCol": self.end_col,
        }


class LineMap:
    """Maps code-point offsets to (line, col)."""

    def __init__(self, text: str) -> None:
        self._starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                self._starts.append(i + 1)

    def position(self, offset: int) -> tuple[int, int]:
        line = bisect_right(self._starts, offset) - 1
        return line + 1, offset - self._starts[line] + 1

    def span(self, start: int, end: int) -> Span:
        line, col = self.position(start)
        end_line, end_col = self.position(end)
        return Span(start, end, line, col, end_line, end_col)


def normalize(text: str) -> str:
    """Strip a UTF-8 BOM and convert CRLF / CR line endings to LF."""
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")

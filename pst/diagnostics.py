"""Diagnostics with stable error codes and machine-applicable fixes (SPEC §15.1)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .source import Span


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Edit:
    span: Span
    text: str

    def to_json(self) -> dict:
        return {**self.span.to_json(), "text": self.text}


@dataclass(frozen=True, slots=True)
class Fix:
    description: str
    edits: tuple[Edit, ...]

    def to_json(self) -> dict:
        return {
            "description": self.description,
            "edits": [e.to_json() for e in self.edits],
        }


@dataclass(frozen=True, slots=True)
class Diagnostic:
    file: str
    span: Span
    severity: Severity
    code: str
    message: str
    fixes: tuple[Fix, ...] = ()

    def to_json(self) -> dict:
        return {
            "file": self.file,
            **self.span.to_json(),
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
            "fixes": [f.to_json() for f in self.fixes],
        }

    def human(self) -> str:
        return (
            f"{self.file}:{self.span.line}:{self.span.col}: "
            f"{self.severity.value} {self.code}: {self.message}"
        )


# Stable error codes. Never renumber; only add. `pst explain` reads this table.
ERROR_CODES: dict[str, str] = {
    "E0000": "The source file is not valid UTF-8.",
    "E0001": "Tab character outside a string literal. Indent with 4 spaces.",
    "E0002": "Indentation is not a multiple of 4 spaces.",
    "E0003": "Indentation increases by more than one level (4 spaces) at once.",
    "E0004": "Unterminated string literal: missing closing '\"' before end of line.",
    "E0005": "Invalid escape sequence. Valid: \\n \\t \\r \\0 \\\\ \\\" \\' \\{ \\u{XXXX}.",
    "E0006": "Unexpected character outside a string or comment.",
    "E0007": "Malformed number literal.",
    "E0008": "Invalid char literal: must contain exactly one character, e.g. 'a'.",
    "E0009": "Unmatched or unclosed bracket.",
    "E0030": "Invalid string placeholder: '{' not closed on the same line, or empty '{}'. "
    "Write '\\{' for a literal brace.",
}

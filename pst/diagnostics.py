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
    "E0010": "End marker doesn't match the block it closes, or follows no block it can close.",
    "E0040": "Syntax error: unexpected token.",
    "E0041": "Unexpected indentation: this line is indented without opening a block.",
    "E0042": "Expected an indented block (bodies can't be empty).",
    "E0043": "Comparisons can't be chained. Write 'a < b and b < c'.",
    "E0044": "'uses' must come directly after the header, at most once.",
    "E0045": "A file must start with exactly one 'program Name' or 'unit Name' header.",
    "E0046": "Invalid assignment target. Assign to a variable, field, index, or deref(r).",
    "E0047": "Expression is not a statement. Only calls and assignments can stand alone.",
    "E0048": "Declaration not allowed here (types, routines, externs, tests: top level only).",
    "E0049": "A block must start on a new line, indented by 4 spaces.",
    "E0101": "A unit can't contain top-level statements.",
    "E0104": "A discard ('_' or '_name') can't be read.",
    "E0201": "Variable declared without an initializer.",
    "E0202": "'const' is only allowed at top level. Use 'let' inside routines.",
    "E0801": "'if' expression without 'else'.",
    "E0803": "'if' expression used as an operand must be parenthesized.",
    "E0804": "'case' expression must be the whole right side of ':=', 'var', 'let', or 'return'.",
}

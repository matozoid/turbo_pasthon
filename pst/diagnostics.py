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
    "E0020": "Wrong casing. Types, traits, enum members, constants, units and type "
    "parameters are UpperCamel; routines, variables, parameters and fields are lowerCamel.",
    "E0100": "Unknown name, or the unit or enum has no such member.",
    "E0101": "A unit can't contain top-level statements.",
    "E0102": "Unit not found (searched: standard units, then the using file's directory), "
    "or the file is a program.",
    "E0103": "Top-level variables belong to the program body and aren't visible in routines. "
    "Pass the value as a parameter ('var' to change it).",
    "E0104": "A discard ('_' or '_name') can't be read.",
    "E0105": "The program or unit name must match the file name (Name.pst).",
    "E0106": "'extern \"pst\"' is reserved for the standard units.",
    "E0107": "Cyclic 'uses' between units.",
    "E0108": "Duplicate declaration. Only routines may share a name (overloads).",
    "E0109": "The name exists in that unit but isn't exported.",
    "E0110": "Ambiguous name: several used units export it. Qualify it: 'Unit.name'.",
    "E0111": "'This' is only valid inside a trait.",
    "E0201": "Variable declared without an initializer.",
    "E0202": "'const' is only allowed at top level. Use 'let' inside routines.",
    "E0205": "Variable declared again in the same routine. Choose another name.",
    "E0206": "Variable never used. Remove it, or discard the value with '_ := expr'.",
    "E0300": "Type mismatch.",
    "E0301": "The type of an empty literal ('[]', '{}') isn't known here. Declare it.",
    "E0302": "Can't infer a type argument, or an overloaded or generic routine is used as a "
    "value without saying which one.",
    "E0303": "Wrong number of type arguments.",
    "E0304": "Not a type.",
    "E0305": "Operator not defined for these operand types.",
    "E0306": "Not a value (a type, a unit, or the missing result of a procedure call).",
    "E0307": "Not callable.",
    "E0310": "Strings can't be indexed. Iterate, use bytes(s)[i], or slice(s, start, stop).",
    "E0311": "This type can't be indexed.",
    "E0320": "No such field.",
    "E0321": "The field is private to its unit.",
    "E0330": "A record with private fields can only be constructed in its own unit.",
    "E0340": "Can't assign: not a mutable variable or place.",
    "E0341": "A 'var' argument must be a mutable variable or place.",
    "E0350": "Not iterable.",
    "E0420": "A trait can't be the type of a value. Use a bounded type parameter or a union.",
    "E0421": "Interpolated value isn't Printable.",
    "E0501": "Function doesn't return a value on every path.",
    "E0502": "Function result ignored. Use it, or discard it with '_ := ...'.",
    "E0503": "Wrong 'return': missing value in a function, or a value outside one.",
    "E0504": "'break' or 'continue' outside a loop.",
    "E0505": "'try' needs an Option or Result operand, inside a function returning the same "
    "kind (and, for Result, the same error type).",
    "E0510": "Ambiguous call: several overloads match equally well.",
    "E0511": "No overload matches the arguments (count, names, or types).",
    "E0520": "A routine whose first parameter is 'var' can't be called with a dot.",
    "E0521": "Argument for a 'var' parameter must be marked 'var'.",
    "E0522": "'var' on an argument whose parameter isn't 'var'.",
    "E0530": "Argument has the same type as an earlier parameter group; pass it by name.",
    "E0601": "Case label overlaps an earlier one.",
    "E0602": "Case isn't exhaustive. Cover every variant or member, or add 'else'.",
    "E0603": "Invalid case label for the subject's type.",
    "E0700": "Type argument doesn't satisfy its bound.",
    "E0710": "Operation not provided by the type parameter's bounds.",
    "E0801": "'if' expression without 'else'.",
    "E0802": "Branches of an 'if' or 'case' expression have different types.",
    "E0803": "'if' expression used as an operand must be parenthesized.",
    "E0804": "'case' expression must be the whole right side of ':=', 'var', 'let', or 'return'.",
    "E0990": "Not supported by the code generator yet (the program checks, but can't be built).",
}

"""Symbols and the symbol index (SPEC §15.6).

Every name the checker resolves is recorded as a ``Ref``: definitions and uses, with the
symbols they point to. The index answers "what is at this position" and "where is this
symbol used", which is what `pst query`, the language server and renames need.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .source import Span


class Kind(str, Enum):
    UNIT = "unit"
    TYPE = "type"
    TRAIT = "trait"
    BUILTIN_TYPE = "builtin type"
    BUILTIN_TRAIT = "builtin trait"
    ENUM_MEMBER = "enum member"
    FIELD = "field"
    CONST = "const"
    FUNCTION = "function"
    PROCEDURE = "procedure"
    TYPE_PARAM = "type parameter"
    PARAM = "parameter"
    VAR = "var"
    LET = "let"
    LOOP_VAR = "loop variable"
    BINDING = "case binding"
    RESULT = "result"


ROUTINES = {Kind.FUNCTION, Kind.PROCEDURE}
TYPES = {Kind.TYPE, Kind.TRAIT, Kind.BUILTIN_TYPE, Kind.BUILTIN_TRAIT, Kind.TYPE_PARAM}
LOCALS = {Kind.PARAM, Kind.VAR, Kind.LET, Kind.LOOP_VAR, Kind.BINDING, Kind.RESULT}

# SPEC §2.5: decided by the first letter.
UPPER_CAMEL = {Kind.UNIT, Kind.TYPE, Kind.TRAIT, Kind.ENUM_MEMBER, Kind.CONST, Kind.TYPE_PARAM}
LOWER_CAMEL = {
    Kind.FUNCTION, Kind.PROCEDURE, Kind.PARAM, Kind.VAR, Kind.LET, Kind.LOOP_VAR,
    Kind.BINDING, Kind.FIELD,
}  # fmt: skip


@dataclass(eq=False)
class Symbol:
    kind: Kind
    name: str
    span: Span | None  # the defining name; None for compiler-provided symbols
    file: str | None
    unit: str
    exported: bool = False
    node: object = None  # the declaring AST node
    members: dict[str, "Symbol"] = field(default_factory=dict)  # enum members, fields, trait routines
    is_extern: bool = False
    # Locals only:
    reads: int = 0
    unused_fix: Span | None = None  # replaced by `_` when the variable is never read

    @property
    def is_routine(self) -> bool:
        return self.kind in ROUTINES

    def describe(self) -> str:
        return f"{self.kind.value} '{self.name}'"

    def location(self) -> str:
        if self.span is None:
            return f"built into unit {self.unit}"
        return f"{self.file}:{self.span.line}:{self.span.col}"

    def to_json(self) -> dict:
        out = {"kind": self.kind.value, "name": self.name, "unit": self.unit}
        if self.span is not None:
            out["file"] = self.file
            out.update(self.span.to_json())
        return out


@dataclass(frozen=True, eq=False)
class Ref:
    """A name at ``span`` in ``file`` that defines or uses ``targets``.

    A use of an overloaded routine targets every overload until types pick one.
    """

    file: str
    span: Span
    targets: tuple[Symbol, ...]
    is_def: bool = False

    def to_json(self) -> dict:
        return {
            "file": self.file,
            **self.span.to_json(),
            "def": self.is_def,
            "targets": [t.to_json() for t in self.targets],
        }


class Index:
    def __init__(self) -> None:
        self.refs: list[Ref] = []

    def add(self, file: str, span: Span, targets, is_def: bool = False) -> None:
        self.refs.append(Ref(file, span, tuple(targets), is_def))

    def at(self, file: str, line: int, col: int) -> Ref | None:
        """The reference covering a position, if any."""
        for r in self.refs:
            s = r.span
            if r.file == file and (s.line, s.col) <= (line, col) < (s.end_line, s.end_col):
                return r
        return None

    def references(self, sym: Symbol) -> list[Ref]:
        """Every definition and use of ``sym``."""
        return [r for r in self.refs if any(t is sym for t in r.targets)]

"""Abstract syntax tree for Turbo Pasthon (SPEC §16).

Every node has a ``span`` covering its source text (SPEC §15.6). Declarations carry their
``##`` doc comment in ``doc``; nodes that can be closed by an end marker (SPEC §2.4) record
its span in ``end_marker``.

The parser never stops at an error. Broken statements and expressions become
``ErrorStmt`` / ``ErrorExpr`` nodes, so the tree is always complete.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from typing import Union

from .source import Span


@dataclass(eq=False)
class Node:
    span: Span


# ------------------------------------------------------------------ names, types


@dataclass(eq=False)
class Ident(Node):
    name: str


@dataclass(eq=False)
class TypeName(Node):
    """`Integer`, `List[T]`, `Geometry.Shape`, `Array[T, N * 2]`.

    ``args`` holds type expressions, or plain expressions for constant arguments. A bare
    name may also turn out to be a constant (`N` in `Array[T, N]`); the checker decides.
    """

    parts: list[str]
    args: list[Union["TypeExpr", "Expr"]] = field(default_factory=list)
    part_spans: list[Span] = field(default_factory=list)  # one per part


@dataclass(eq=False)
class RefType(Node):
    target: "TypeExpr"


@dataclass(eq=False)
class FuncType(Node):
    is_function: bool
    params: list["TypeExpr"]
    result: "TypeExpr | None"


TypeExpr = Union[TypeName, RefType, FuncType]


@dataclass(eq=False)
class TypeParam(Node):
    name: Ident
    bounds: list[TypeExpr]  # traits, or the type of a constant parameter (`N: Integer`)


# ----------------------------------------------------------------- expressions


@dataclass(eq=False)
class Name(Node):
    name: str


@dataclass(eq=False)
class IntLit(Node):
    value: int


@dataclass(eq=False)
class RealLit(Node):
    value: float


@dataclass(eq=False)
class CharLit(Node):
    value: str


@dataclass(eq=False)
class BoolLit(Node):
    value: bool


@dataclass(eq=False)
class StringLit(Node):
    value: str


@dataclass(eq=False)
class InterpString(Node):
    """A string with placeholders: text parts and expressions, in order."""

    parts: list[Union[str, "Expr"]]


@dataclass(eq=False)
class ListLit(Node):
    items: list["Expr"]


@dataclass(eq=False)
class MapEntry(Node):
    key: "Expr"
    value: "Expr"


@dataclass(eq=False)
class MapLit(Node):
    entries: list[MapEntry]


@dataclass(eq=False)
class SetLit(Node):
    items: list["Expr"]


@dataclass(eq=False)
class EmptyBraces(Node):
    """`{}`: an empty map or set, decided by the expected type."""


@dataclass(eq=False)
class Paren(Node):
    inner: "Expr"


@dataclass(eq=False)
class Unary(Node):
    op: str  # "-", "not", "try"
    operand: "Expr"


@dataclass(eq=False)
class Binary(Node):
    op: str  # "+", "div", "and", "<=", "in", ...
    left: "Expr"
    right: "Expr"
    op_span: Span


@dataclass(eq=False)
class Arg(Node):
    name: Ident | None  # named argument
    is_var: bool  # `var x` at the call site
    value: "Expr"


@dataclass(eq=False)
class Call(Node):
    callee: "Expr"
    args: list[Arg]


@dataclass(eq=False)
class Index(Node):
    """`a[i]` or `f[Integer]`: indexing and explicit instantiation look the same, so
    the checker decides (SPEC §16). Items may be type expressions."""

    target: "Expr"
    items: list[Union["Expr", TypeExpr]]


@dataclass(eq=False)
class FieldAccess(Node):
    target: "Expr"
    name: Ident


@dataclass(eq=False)
class Deref(Node):
    target: "Expr"


@dataclass(eq=False)
class CondExpr(Node):
    cond: "Expr"
    value: "Expr"


@dataclass(eq=False)
class IfExpr(Node):
    branches: list[CondExpr]
    else_value: "Expr"


@dataclass(eq=False)
class ErrorExpr(Node):
    pass


# ---------------------------------------------------------------- case labels


@dataclass(eq=False)
class ValueLabel(Node):
    """A constant, or a bare name: an enum member or a variant without binding."""

    value: "Expr"


@dataclass(eq=False)
class RangeLabel(Node):
    low: "Expr"
    high: "Expr"


@dataclass(eq=False)
class VariantLabel(Node):
    """`Circle(c)` / `Circle(_)`: a union variant with a binding."""

    variant: TypeName
    binding: Ident  # name starts with `_` for a discard


Label = Union[ValueLabel, RangeLabel, VariantLabel]


@dataclass(eq=False)
class CaseExprArm(Node):
    labels: list[Label]  # empty for `else`
    value: "Expr"


@dataclass(eq=False)
class CaseExpr(Node):
    subject: "Expr"
    arms: list[CaseExprArm]


Expr = Union[
    Name, IntLit, RealLit, CharLit, BoolLit, StringLit, InterpString, ListLit, MapLit,
    SetLit, EmptyBraces, Paren, Unary, Binary, Call, Index, FieldAccess, Deref, IfExpr,
    CaseExpr, ErrorExpr,
]  # fmt: skip


# ------------------------------------------------------------------ statements


@dataclass(eq=False)
class Block(Node):
    stmts: list["Stmt"]


@dataclass(eq=False)
class VarDecl(Node):
    mutable: bool  # var / let
    name: Ident
    type: TypeExpr | None
    value: Expr


@dataclass(eq=False)
class Assign(Node):
    target: Expr
    value: Expr


@dataclass(eq=False)
class Discard(Node):
    name: str  # "_" or "_name"
    value: Expr


@dataclass(eq=False)
class ExprStmt(Node):
    expr: Expr


@dataclass(eq=False)
class CondBlock(Node):
    cond: Expr
    body: Block


@dataclass(eq=False)
class If(Node):
    branches: list[CondBlock]  # `if` and every `else if`
    else_body: Block | None
    end_marker: Span | None = None


@dataclass(eq=False)
class While(Node):
    cond: Expr
    body: Block
    end_marker: Span | None = None


@dataclass(eq=False)
class Repeat(Node):
    body: Block
    cond: Expr


@dataclass(eq=False)
class ForRange(Node):
    var: Ident
    start: Expr
    stop: Expr
    down: bool
    body: Block
    end_marker: Span | None = None


@dataclass(eq=False)
class ForIn(Node):
    vars: list[Ident]  # one, or key and value for maps; names may be discards
    iterable: Expr
    body: Block
    end_marker: Span | None = None


@dataclass(eq=False)
class CaseArm(Node):
    labels: list[Label]  # empty for `else`
    body: Union[Block, "Stmt"]  # indented block, or one statement on the arm's line


@dataclass(eq=False)
class Case(Node):
    subject: Expr
    arms: list[CaseArm]
    end_marker: Span | None = None


@dataclass(eq=False)
class Defer(Node):
    body: Union[Block, "Stmt"]
    end_marker: Span | None = None


@dataclass(eq=False)
class Break(Node):
    pass


@dataclass(eq=False)
class Continue(Node):
    pass


@dataclass(eq=False)
class Return(Node):
    value: Expr | None


@dataclass(eq=False)
class Assert(Node):
    cond: Expr
    message: Expr | None


@dataclass(eq=False)
class ErrorStmt(Node):
    pass


Stmt = Union[
    VarDecl, Assign, Discard, ExprStmt, If, While, Repeat, ForRange, ForIn, Case, Defer,
    Break, Continue, Return, Assert, ErrorStmt,
]  # fmt: skip


# ---------------------------------------------------------------- declarations


@dataclass(eq=False)
class FieldDecl(Node):
    exported: bool
    name: Ident
    type: TypeExpr
    default: Expr | None
    doc: str | None = None


@dataclass(eq=False)
class Param(Node):
    """One parameter group: `a, b: T`, `var x: T := d`, `this`, `var this`, or `...`."""

    is_var: bool
    names: list[Ident]  # several for a group; ["this"] for a `this` parameter
    type: TypeExpr | None  # None for `this` and `...`
    default: Expr | None
    is_this: bool = False
    is_varargs: bool = False


@dataclass(eq=False)
class Signature(Node):
    is_function: bool
    name: Ident
    type_params: list[TypeParam]
    params: list[Param]
    result: TypeExpr | None
    doc: str | None = None


@dataclass(eq=False)
class EnumType(Node):
    members: list[Ident]


@dataclass(eq=False)
class SubrangeType(Node):
    low: Expr
    high: Expr


@dataclass(eq=False)
class UnionType(Node):
    variants: list[TypeExpr]


@dataclass(eq=False)
class RecordType(Node):
    fields: list[FieldDecl]


@dataclass(eq=False)
class TraitType(Node):
    fields: list[FieldDecl]
    routines: list[Signature]


TypeBody = Union[TypeExpr, EnumType, SubrangeType, UnionType, RecordType, TraitType]


@dataclass(eq=False)
class TypeDecl(Node):
    exported: bool
    name: Ident
    params: list[TypeParam]
    body: TypeBody
    doc: str | None = None
    end_marker: Span | None = None


@dataclass(eq=False)
class TypeSection(Node):
    decls: list[TypeDecl]


@dataclass(eq=False)
class ConstDecl(Node):
    exported: bool
    name: Ident
    value: Expr
    doc: str | None = None


@dataclass(eq=False)
class ConstSection(Node):
    decls: list[ConstDecl]


@dataclass(eq=False)
class Routine(Node):
    exported: bool
    unsafe: bool
    sig: Signature
    requires: list[Expr]
    ensures: list[Expr]
    body: Block
    doc: str | None = None
    end_marker: Span | None = None


@dataclass(eq=False)
class ExternDecl(Node):
    exported: bool
    sig: Signature
    symbol: str | None  # `as "symbol"`
    doc: str | None = None


@dataclass(eq=False)
class ExternBlock(Node):
    library: str
    decls: list[ExternDecl]
    end_marker: Span | None = None


@dataclass(eq=False)
class TestBlock(Node):
    name: str
    body: Block
    end_marker: Span | None = None


Decl = Union[TypeSection, ConstSection, Routine, ExternBlock, TestBlock]


@dataclass(eq=False)
class File(Node):
    kind: str  # "program" or "unit"
    name: Ident
    uses: list[Ident]
    items: list[Union[Decl, Stmt]]  # in source order; statements only in programs


# --------------------------------------------------------------------- output

_SKIP_IN_DUMP = {"span", "op_span", "doc", "end_marker", "part_spans"}
_BARE = re.compile(r'[^\s()\[\]"]+')


def dump(node) -> str:
    """Compact s-expression form, for tests and debugging.

    `(Assign (Name x) (Binary + (IntLit 1) (IntLit 2)))`. Boolean fields appear as
    `:name` when true; None, false and empty lists are left out. `BoolLit` values print
    as `true` / `false`.
    """
    if isinstance(node, Node):
        out = [type(node).__name__]
        for f in fields(node):
            if f.name in _SKIP_IN_DUMP:
                continue
            v = getattr(node, f.name)
            if isinstance(v, bool) and f.name == "value":  # BoolLit: data, not a flag
                out.append("true" if v else "false")
            elif v is None or v is False or v == []:
                continue
            else:
                out.append(f":{f.name}" if v is True else dump(v))
        return "(" + " ".join(out) + ")"
    if isinstance(node, list):
        return "[" + " ".join(dump(x) for x in node) + "]"
    if isinstance(node, str):
        return node if _BARE.fullmatch(node) else json.dumps(node)
    return str(node)


def to_json(node):
    """Full JSON form, including spans, for `pst ast --json`."""
    if isinstance(node, Node):
        out = {"node": type(node).__name__}
        for f in fields(node):
            v = getattr(node, f.name)
            out[f.name] = v.to_json() if isinstance(v, Span) else to_json(v)
        return out
    if isinstance(node, list):
        return [to_json(x) for x in node]
    return node

"""Semantic types (SPEC §4).

Types are immutable values compared structurally. Declared types (records, enums,
unions, subranges, traits) are identified by their declaring symbol plus type arguments.
Aliases are expanded when a type expression is evaluated, so `Integer` *is* `Int64`.

Type parameters appear as ``Param``. During inference, the parameters of the generic
being called act as variables: ``unify`` binds them in a substitution dict.
"""

from __future__ import annotations

from dataclasses import dataclass

from .symbols import Symbol

INTEGERS = ("Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64")
REALS = ("Real32", "Real64")
_ALIAS_NAMES = {"Int64": "Integer", "Real64": "Real", "UInt8": "Byte"}


class Type:
    __slots__ = ()

    def __str__(self) -> str:
        return show(self)


@dataclass(frozen=True, eq=True)
class Prim(Type):
    name: str  # Int8..UInt64, Real32, Real64, Boolean, Char, String, CString, Pointer


@dataclass(frozen=True, eq=True)
class Builtin(Type):
    """`List[T]`, `Map[K, V]`, `Set[T]`, `Array[T, N]`, `Weak[T]`, `Ptr[T]`."""

    name: str
    args: tuple


@dataclass(frozen=True, eq=True)
class RefT(Type):
    target: Type


@dataclass(frozen=True, eq=True)
class FuncT(Type):
    is_function: bool
    params: tuple
    result: Type  # VOID for procedures


@dataclass(frozen=True, eq=True)
class Named(Type):
    """A declared record, enum, union or subrange type, with its type arguments."""

    sym: Symbol
    args: tuple = ()


@dataclass(frozen=True, eq=True)
class TraitT(Type):
    """A trait, as used in a bound. Never the type of a value (E0420)."""

    sym: Symbol
    args: tuple = ()


@dataclass(frozen=True, eq=True)
class Param(Type):
    """A type parameter, or a constant parameter used as a type argument."""

    sym: Symbol


@dataclass(frozen=True, eq=True)
class ConstArg(Type):
    """A constant type argument: `5` or an expression like `N * 2` (kept as text)."""

    value: object  # int, or str for an expression


@dataclass(frozen=True, eq=True)
class Special(Type):
    name: str


@dataclass(frozen=True, eq=True)
class TypeValue(Type):
    """A type name used in an expression: a constructor, conversion, or enum."""

    type: Type


@dataclass(frozen=True, eq=True)
class Overloads(Type):
    """A routine name used as a value before a single routine is chosen."""

    syms: tuple


ERROR = Special("<error>")  # already reported; compatible with everything
VOID = Special("<no value>")  # the result of a procedure
THIS = Special("This")  # inside trait signatures
EMPTY_LIST = Special("[]")  # `[]` before its type is known
EMPTY_BRACES = Special("{}")
INT_LIT = Special("<integer literal>")
REAL_LIT = Special("<real literal>")

INTEGER = Prim("Int64")
REAL = Prim("Real64")
BOOLEAN = Prim("Boolean")
CHAR = Prim("Char")
STRING = Prim("String")


def is_integer(t: Type) -> bool:
    return isinstance(t, Prim) and t.name in INTEGERS


def is_real(t: Type) -> bool:
    return isinstance(t, Prim) and t.name in REALS


def is_numeric(t: Type) -> bool:
    return is_integer(t) or is_real(t)


def has_error(t) -> bool:
    if t is ERROR or t == ERROR:
        return True
    if isinstance(t, (Builtin, Named, TraitT)):
        return any(has_error(a) for a in t.args)
    if isinstance(t, RefT):
        return has_error(t.target)
    if isinstance(t, FuncT):
        return any(has_error(p) for p in t.params) or has_error(t.result)
    return False


def show(t) -> str:
    if isinstance(t, Prim):
        return _ALIAS_NAMES.get(t.name, t.name)
    if isinstance(t, (Builtin, Named, TraitT)):
        name = t.name if isinstance(t, Builtin) else t.sym.name
        return f"{name}[{', '.join(show(a) for a in t.args)}]" if t.args else name
    if isinstance(t, RefT):
        return f"ref {show(t.target)}"
    if isinstance(t, FuncT):
        kw = "function" if t.is_function else "procedure"
        params = ", ".join(show(p) for p in t.params)
        return f"{kw}({params})" + (f": {show(t.result)}" if t.is_function else "")
    if isinstance(t, Param):
        return t.sym.name
    if isinstance(t, ConstArg):
        return str(t.value)
    if isinstance(t, Special):
        return t.name
    if isinstance(t, TypeValue):
        return f"type {show(t.type)}"
    if isinstance(t, Overloads):
        return f"routine '{t.syms[0].name}'"
    return repr(t)


def subst(t, s: dict):
    """Replaces parameters bound in ``s`` (one pass: bindings are not re-substituted)."""
    if not s:
        return t
    if isinstance(t, Param):
        return s.get(t.sym, t)
    if isinstance(t, Builtin):
        return Builtin(t.name, tuple(subst(a, s) for a in t.args))
    if isinstance(t, Named):
        return Named(t.sym, tuple(subst(a, s) for a in t.args))
    if isinstance(t, TraitT):
        return TraitT(t.sym, tuple(subst(a, s) for a in t.args))
    if isinstance(t, RefT):
        return RefT(subst(t.target, s))
    if isinstance(t, FuncT):
        return FuncT(t.is_function, tuple(subst(p, s) for p in t.params), subst(t.result, s))
    if t is THIS and THIS in s:
        return s[THIS]
    return t


def params_in(t, out: set | None = None) -> set:
    out = set() if out is None else out
    if isinstance(t, Param):
        out.add(t.sym)
    elif isinstance(t, (Builtin, Named, TraitT)):
        for a in t.args:
            params_in(a, out)
    elif isinstance(t, RefT):
        params_in(t.target, out)
    elif isinstance(t, FuncT):
        for p in t.params:
            params_in(p, out)
        params_in(t.result, out)
    return out


def unify(pattern, actual, s: dict, vars: set) -> bool:
    """Matches ``pattern`` against ``actual``, binding parameters in ``vars`` into ``s``.

    Only exact structure matches; implicit conversions are the caller's business.
    """
    if actual == ERROR or pattern == ERROR:
        return True
    if isinstance(pattern, Param) and pattern.sym in vars:
        bound = s.get(pattern.sym)
        if bound is None:
            s[pattern.sym] = actual
            return True
        return bound == actual or bound == ERROR or actual == ERROR
    if type(pattern) is not type(actual):
        return False
    if isinstance(pattern, (Builtin, Named, TraitT)):
        same = (pattern.name == actual.name) if isinstance(pattern, Builtin) else pattern.sym is actual.sym
        return (
            same
            and len(pattern.args) == len(actual.args)
            and all(unify(p, a, s, vars) for p, a in zip(pattern.args, actual.args))
        )
    if isinstance(pattern, RefT):
        return unify(pattern.target, actual.target, s, vars)
    if isinstance(pattern, FuncT):
        return (
            pattern.is_function == actual.is_function
            and len(pattern.params) == len(actual.params)
            and all(unify(p, a, s, vars) for p, a in zip(pattern.params, actual.params))
            and unify(pattern.result, actual.result, s, vars)
        )
    return pattern == actual

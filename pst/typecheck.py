"""Type checking: checker stage 2 (SPEC §4, §6–§9).

Runs after name resolution, over every unit (standard units included). Names were already
resolved into the symbol index, so this pass finds the symbol behind a name by its span.
What it adds:

- the type of every expression and variable (``Symbol.type`` for locals),
- overload resolution: the index entry of an overloaded call is narrowed to the routine
  chosen (SPEC §6.7),
- field access and dot calls (UFCS, SPEC §6.6), named arguments, and bare names in `case`
  labels, which stage 1 leaves alone because they need types,
- trait satisfaction for bounds (SPEC §4.10), checked structurally where the generic is
  used.

Inside a generic, a type parameter is an opaque type: only what its bounds provide works
on it (SPEC §8). A type error produces ``ERROR``, which is compatible with everything, so
one mistake gives one diagnostic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from . import ast as A
from .diagnostics import Edit, Fix
from .source import Span
from .symbols import LOCALS, Kind, Symbol
from .types import (
    BOOLEAN, CHAR, EMPTY_BRACES, EMPTY_LIST, ERROR, INT_LIT, INTEGER, REAL, REAL_LIT, STRING,
    THIS, VOID, Builtin, ConstArg, FuncT, Named, Overloads, Param, Prim, RefT, TraitT,
    TypeValue, has_error, is_integer, is_numeric, is_real, params_in, show, subst, unify,
)  # fmt: skip

_ARITY = {"List": 1, "Weak": 1, "Ptr": 1, "Array": 2}
_INT_RANGE = {
    "Int8": (-(2**7), 2**7 - 1), "Int16": (-(2**15), 2**15 - 1),
    "Int32": (-(2**31), 2**31 - 1), "Int64": (-(2**63), 2**63 - 1),
    "UInt8": (0, 2**8 - 1), "UInt16": (0, 2**16 - 1), "UInt32": (0, 2**32 - 1),
    "UInt64": (0, 2**64 - 1),
}  # fmt: skip
_ARITH = {"+", "-", "*", "/", "div", "mod"}
_DECLS = (A.TypeSection, A.ConstSection, A.Routine, A.ExternBlock, A.TestBlock)
_TYPE_EXPRS = (A.TypeName, A.RefType, A.FuncType)


def _point(span: Span) -> Span:
    return Span(span.start, span.start, span.line, span.col, span.line, span.col)


def _expr_to_type(e):
    """An expression that names a type (`List[Integer]` in `f[List[Integer]]`), as a
    TypeName. The resolver recorded its names at the same spans."""
    if isinstance(e, _TYPE_EXPRS):
        return e
    if isinstance(e, A.Name):
        return A.TypeName(e.span, [e.name], [], [e.span])
    if isinstance(e, A.FieldAccess):
        inner = _expr_to_type(e.target)
        if isinstance(inner, A.TypeName) and not inner.args:
            return A.TypeName(e.span, inner.parts + [e.name.name], [], inner.part_spans + [e.name.span])
        return None
    if isinstance(e, A.Index):
        inner = _expr_to_type(e.target)
        if isinstance(inner, A.TypeName) and not inner.args:
            args = [_expr_to_type(x) or x for x in e.items]
            return A.TypeName(e.span, inner.parts, args, inner.part_spans)
    return None


def _literal_value(e):
    """The value of an integer or real literal, possibly negated; None otherwise."""
    if isinstance(e, A.Paren):
        return _literal_value(e.inner)
    if isinstance(e, (A.IntLit, A.RealLit)):
        return e.value
    if isinstance(e, A.Unary) and e.op == "-" and isinstance(e.operand, (A.IntLit, A.RealLit)):
        return -e.operand.value
    return None


def _unparen(e):
    return _unparen(e.inner) if isinstance(e, A.Paren) else e


def _castable(t) -> bool:
    return is_integer(t) or t in (Prim("Pointer"), Prim("CString")) or (isinstance(t, Builtin) and t.name == "Ptr")


def _children(node):
    """The direct sub-nodes of an AST node."""
    for value in vars(node).values():
        if isinstance(value, A.Node):
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, A.Node))


def _is_literal(e) -> bool:
    return _literal_value(e) is not None


# ------------------------------------------------------------------ signatures


@dataclass
class PInfo:
    """One parameter (or record field, for construction)."""

    name: str
    type: object
    is_var: bool = False
    has_default: bool = False
    group: int = 0  # parameters declared together (`a, b: T`) share a group
    varargs: bool = False
    sym: Symbol | None = None  # the parameter or field, for named-argument references
    default: object = None


@dataclass
class Sig:
    sym: Symbol | None
    name: str
    is_function: bool
    tparams: list[Symbol]
    params: list[PInfo]
    result: object  # VOID for procedures
    kind: str = "routine"  # "routine", "record" (construction), "value" (function value)
    positional: bool = True  # records: only with a single field

    def functype(self) -> FuncT:
        return FuncT(self.is_function, tuple(p.type for p in self.params), self.result)

    def describe(self) -> str:
        params = ", ".join(("var " if p.is_var else "") + show(p.type) for p in self.params)
        where = f" ({self.sym.location()})" if self.sym is not None else ""
        return f"{self.name}({params}){where}"


@dataclass
class FieldInfo:
    sym: Symbol | None
    name: str
    type: object
    has_default: bool
    default: object
    exported: bool


@dataclass
class Decl:
    """What a declared type is: record, enum, union, subrange, alias, or trait."""

    kind: str
    params: list[Symbol]
    fields: list[FieldInfo] = field(default_factory=list)
    variants: list = field(default_factory=list)
    base: object = ERROR  # subranges
    alias: object = ERROR
    routines: list[Symbol] = field(default_factory=list)  # traits
    members: list[str] = field(default_factory=list)  # enums
    c: bool = False  # a C record (SPEC §10.5)

    def field(self, name: str) -> FieldInfo | None:
        return next((f for f in self.fields if f.name == name), None)


@dataclass
class Cand:
    """An overload candidate. ``pre`` binds `This` and the trait's parameters for a bound
    member, or a generic record's parameters for an alias."""

    sig: Sig
    pre: dict = field(default_factory=dict)


@dataclass
class CArg:
    span: Span
    name: A.Ident | None
    is_var: bool
    expr: object
    type: object  # None while flexible
    flex: object = None  # INT_LIT, REAL_LIT, EMPTY_LIST, EMPTY_BRACES, or Overloads
    receiver: bool = False  # the `a` of `a.f(...)`


@dataclass
class Fail:
    code: str
    span: Span
    message: str
    bound: bool  # the arguments fit the parameters; a type was wrong


@dataclass
class Match:
    sig: Sig
    params: list[PInfo]
    result: object
    s: dict
    tier: int  # 0 exact, 1 union injection, 2 literal of a non-default type
    slots: list
    extra: list
    pre: dict = field(default_factory=dict)  # the candidate's `pre`


@dataclass
class _CaseInfo:
    type: object
    kind: str  # union, enum, bool, value, error
    keys: list = field(default_factory=list)  # union variants / enum members, for coverage
    covered: set = field(default_factory=set)
    ranges: list = field(default_factory=list)  # (low, high) of constant labels


# ------------------------------------------------------------------ environment


class Env:
    """Everything shared between the units: declared types, signatures, constants."""

    def __init__(self, checker, units) -> None:
        self.c = checker
        self.index = checker.index
        self.units = {u.name: u for u in reversed(units)}
        self.by_file = {u.file: u for u in units}
        self._sigs: dict[Symbol, Sig] = {}
        self._decls: dict[Symbol, Decl] = {}
        self._consts: dict[Symbol, object] = {}
        self._bounds: dict[Symbol, list] = {}
        self._checkers: dict[str, TypeChecker] = {}
        self._const_checkers: dict[str, TypeChecker] = {}
        self._busy: set = set()
        self.sat_cache: dict = {}
        self.owner: dict[Symbol, Symbol] = {}  # enum members, fields, trait routines
        # Generic types written in type expressions, (file, span) -> type: their bounds
        # are checked once every declaration is known (check_bounds).
        self.written: dict[tuple, Named] = {}
        self._hooks: dict | None = None
        for u in units:
            for item in u.tree.items:
                if isinstance(item, A.TypeSection):
                    for d in item.decls:
                        sym = self.def_sym(u.file, d.name.span)
                        if sym is not None:
                            for m in sym.members.values():
                                self.owner[m] = sym
        # A variable for "the element type", when asking whether a type is an iterator.
        self.elem_var = Symbol(Kind.TYPE_PARAM, "T", None, None, "System")

    def checker(self, unit) -> "TypeChecker":
        if unit.file not in self._checkers:
            self._checkers[unit.file] = TypeChecker(self, unit)
        return self._checkers[unit.file]

    def const_checker(self, file: str) -> "TypeChecker":
        """A separate checker for constants and subrange bounds, evaluated on demand."""
        if file not in self._const_checkers:
            self._const_checkers[file] = TypeChecker(self, self.by_file[file])
        return self._const_checkers[file]

    def error(self, file, code, span, message, fixes=()) -> None:
        self.c.error(file, code, span, message, fixes)

    def def_sym(self, file, span) -> Symbol | None:
        r = self.index.lookup(file, span)
        return r.targets[0] if r is not None and len(r.targets) == 1 else None

    def sys(self, name: str) -> Symbol | None:
        u = self.units.get("System")
        syms = u.top.get(name) if u is not None and u.std else None
        return syms[0] if syms else None

    def hooks(self) -> dict:
        """The destroy hooks (SPEC §11.1): resource record symbol -> its `destroy`
        procedure, a non-generic `procedure destroy(var r: R)` in the record's unit."""
        if self._hooks is None:
            self._hooks = {}
            for u in self.units.values():
                for item in u.tree.items:
                    if not (isinstance(item, A.Routine) and item.sig.name.name == "destroy"):
                        continue
                    sym = self.def_sym(u.file, item.sig.name.span)
                    if sym is None:
                        continue
                    sig = self.sig(sym)
                    if sig.is_function or sig.tparams or len(sig.params) != 1 or not sig.params[0].is_var:
                        continue
                    t = sig.params[0].type
                    if not (isinstance(t, Named) and self.kind_of(t) == "record" and not t.args):
                        continue
                    if t.sym.unit != u.name:
                        self.error(
                            u.file, "E1102", item.sig.name.span,
                            f"a destroy hook for '{t.sym.name}' must be declared in its unit, "
                            f"'{t.sym.unit}'",
                        )  # fmt: skip
                    elif t.sym not in self._hooks:
                        self._hooks[t.sym] = sym
        return self._hooks

    def is_resource(self, t) -> bool:
        """Whether ``t`` is a resource record, which exists only behind `ref`."""
        return isinstance(t, Named) and t.sym in self.hooks()

    def check_bounds(self) -> None:
        """`Map[Real, V]`: each type argument of a written generic type must satisfy
        its parameter's bounds (E0700), and a `Set`'s elements must fit one."""
        for (file, span), t in self.written.items():
            if self.is_set(t):
                self.check_element(file, span, t.args[0])
            d = self.decl(t.sym)
            s = dict(zip(d.params, t.args))
            chk = self.checker(self.by_file[file])
            for p in d.params:
                for b in self.bounds(p):
                    if isinstance(b, TraitT) and not chk.satisfies(s[p], subst(b, s)):
                        msg = f"{show(s[p])} doesn't satisfy {show(subst(b, s))}, required by '{t.sym.name}'"
                        self.error(file, "E0700", span, msg)
                        break

    def is_set(self, t) -> bool:
        """Whether ``t`` is a `Set[T]`: a record of System's, with literals of its own."""
        return isinstance(t, Named) and t.sym is self.sys("Set") and t.sym is not None

    def check_element(self, file, span, t) -> None:
        """A `Set` element type: ordinal, with at most 256 values (SPEC §4.5)."""
        if isinstance(t, Param) or has_error(t):
            return
        r = self.ordinal_range(t)
        if r is None or r[1] - r[0] >= 256:
            msg = f"{show(t)} can't be a Set element: it needs an ordinal type with at most 256 values (Boolean, Byte, an enum, or a subrange)"
            self.error(file, "E0700", span, msg)

    def ordinal_range(self, t) -> tuple | None:
        """The first and last `ord` of an ordinal type. None for other types, and for a
        subrange whose bounds aren't literals or enum members."""
        if t == BOOLEAN:
            return 0, 1
        if t == CHAR:
            return 0, 0x10FFFF
        if isinstance(t, Prim) and t.name in _INT_RANGE:
            return _INT_RANGE[t.name]
        if not isinstance(t, Named):
            return None
        d = self.decl(t.sym)
        if d.kind == "enum":
            return 0, len(d.members) - 1
        if d.kind != "subrange":
            return None
        bounds = [self.ordinal_of(b, d.base) for b in (t.sym.node.body.low, t.sym.node.body.high)]
        return None if None in bounds else tuple(bounds)

    def ordinal_of(self, e, t) -> int | None:
        """The `ord` of a subrange bound of type ``t``: a literal or an enum member."""
        e = _unparen(e)
        v = _literal_value(e)
        if isinstance(v, int):
            return v
        if isinstance(e, A.CharLit):
            return ord(e.value)
        if isinstance(e, A.FieldAccess) and self.kind_of(t) == "enum" and e.name.name in self.decl(t.sym).members:
            return self.decl(t.sym).members.index(e.name.name)
        return None

    def is_map(self, t) -> bool:
        """Whether ``t`` is a `Map[K, V]`: a record of System's, with literals of its own."""
        return isinstance(t, Named) and t.sym is self.sys("Map") and t.sym is not None

    # ----------------------------------------------------------- type expressions

    def type_of(self, t, file: str, this_ok: bool = False, value: bool = True):
        """The type a type expression denotes. Traits are only allowed when ``value`` is
        false (bounds)."""
        if t is None:
            return ERROR
        if isinstance(t, A.RefType):
            return RefT(self.type_of(t.target, file, this_ok))
        if isinstance(t, A.FuncType):
            return FuncT(
                t.is_function,
                tuple(self.type_of(p, file, this_ok) for p in t.params),
                self.type_of(t.result, file, this_ok) if t.result is not None else VOID,
            )
        if not isinstance(t, A.TypeName):
            return ERROR
        if t.parts == ["This"]:
            return THIS if this_ok else ERROR  # outside a trait: E0111 from stage 1
        span = (t.part_spans or [t.span])[-1]
        r = self.index.lookup(file, span)
        if r is None or len(r.targets) != 1:
            return ERROR  # unknown or ambiguous: already reported
        sym = r.targets[0]
        name = ".".join(t.parts)
        args = tuple(self.type_arg(a, file, this_ok) for a in t.args)
        if sym.kind is Kind.BUILTIN_TYPE:
            want = _ARITY.get(sym.name, 0)
            if len(args) != want:
                return self.arity(file, t.span, name, want, len(args))
            return Builtin(sym.name, args) if want else Prim(sym.name)
        if sym.kind in (Kind.TYPE, Kind.TRAIT):
            d = self.decl(sym)
            if len(args) != len(d.params):
                return self.arity(file, t.span, name, len(d.params), len(args))
            if d.kind == "trait":
                return self.trait_type(TraitT(sym, args), file, t.span, value)
            if d.kind == "alias":
                return subst(d.alias, dict(zip(d.params, args)))
            if args:
                self.written[(file, t.span)] = Named(sym, args)
            return Named(sym, args)
        if sym.kind is Kind.BUILTIN_TRAIT:
            return self.trait_type(TraitT(sym, args), file, t.span, value)
        if sym.kind is Kind.TYPE_PARAM:
            if args:
                return self.arity(file, t.span, name, 0, len(args))
            if self.is_const_param(sym):
                self.error(file, "E0304", t.span, f"'{name}' is a constant parameter, not a type")
                return ERROR
            return Param(sym)
        self.error(file, "E0304", t.span, f"'{name}' is a {sym.kind.value}, not a type")
        return ERROR

    def trait_type(self, t: TraitT, file, span, value: bool):
        if value:
            self.error(
                file, "E0420", span,
                f"trait '{t.sym.name}' can't be the type of a value; use a type parameter "
                f"bounded by it ('[T: {t.sym.name}]'), or a union",
            )  # fmt: skip
            return ERROR
        return t

    def arity(self, file, span, name, want, got):
        if want == 0:
            msg = f"'{name}' takes no type arguments"
        else:
            msg = f"'{name}' takes {want} type argument{'s' if want > 1 else ''}, not {got}"
        self.error(file, "E0303", span, msg)
        return ERROR

    def type_arg(self, a, file: str, this_ok: bool):
        if isinstance(a, A.TypeName) and len(a.parts) == 1 and not a.args and a.parts != ["This"]:
            r = self.index.lookup(file, (a.part_spans or [a.span])[0])
            if r is not None and len(r.targets) == 1:
                sym = r.targets[0]
                if sym.kind is Kind.CONST:
                    v = self.const_int(sym)
                    return ConstArg(v if v is not None else sym.name)
                if sym.kind is Kind.TYPE_PARAM and self.is_const_param(sym):
                    return Param(sym)
        if isinstance(a, _TYPE_EXPRS):
            return self.type_of(a, file, this_ok)
        if isinstance(a, A.IntLit):
            return ConstArg(a.value)
        return ConstArg(A.dump(a))

    # --------------------------------------------------------------- declarations

    def decl(self, sym: Symbol) -> Decl:
        d = self._decls.get(sym)
        if d is not None:
            return d
        node = sym.node
        file = sym.file
        if not isinstance(node, A.TypeDecl):
            d = Decl("builtin", [])
            self._decls[sym] = d
            return d
        params = [self.def_sym(file, p.name.span) for p in node.params]
        params = [p if p is not None else Symbol(Kind.TYPE_PARAM, "?", None, None, "") for p in params]
        body = node.body
        if isinstance(body, A.RecordType):
            kind = "record"
        elif isinstance(body, A.TraitType):
            kind = "trait"
        elif isinstance(body, A.EnumType):
            kind = "enum"
        elif isinstance(body, A.UnionType):
            kind = "union"
        elif isinstance(body, A.SubrangeType):
            kind = "subrange"
        else:
            kind = "alias"
        d = Decl(kind, params, c=kind == "record" and body.c)
        self._decls[sym] = d  # before the parts, which may refer back to the type
        if kind in ("record", "trait"):
            for f in body.fields:
                fsym = self.def_sym(file, f.name.span)
                d.fields.append(FieldInfo(
                    fsym, f.name.name, self.type_of(f.type, file, this_ok=kind == "trait"),
                    f.default is not None, f.default, fsym.exported if fsym else False,
                ))  # fmt: skip
            if kind == "trait":
                for sig in body.routines:
                    rsym = self.def_sym(file, sig.name.span)
                    if rsym is not None:
                        d.routines.append(rsym)
        elif kind == "enum":
            d.members = [m.name for m in body.members]
        elif kind == "union":
            d.variants = [self.type_of(v, file) for v in body.variants]
        elif kind == "subrange":
            d.base = self.const_checker(file).value(body.low)
        else:
            if sym in self._busy:
                self.error(file, "E0304", node.name.span, f"alias '{sym.name}' refers to itself")
                return d
            self._busy.add(sym)
            d.alias = self.type_of(body, file)
            self._busy.discard(sym)
        return d

    def variants(self, t) -> list:
        if isinstance(t, Named):
            d = self.decl(t.sym)
            if d.kind == "union":
                s = dict(zip(d.params, t.args))
                return [subst(v, s) for v in d.variants]
        return []

    def base(self, t):
        """A subrange's base type; any other type itself."""
        if isinstance(t, Named):
            d = self.decl(t.sym)
            if d.kind == "subrange":
                return d.base
        return t

    def kind_of(self, t) -> str | None:
        return self.decl(t.sym).kind if isinstance(t, Named) else None

    def bounds(self, tp: Symbol) -> list:
        if tp not in self._bounds:
            self._bounds[tp] = []
            node = tp.node
            if isinstance(node, A.TypeParam):
                self._bounds[tp] = [self.type_of(b, tp.file, value=False) for b in node.bounds]
        return self._bounds[tp]

    def is_const_param(self, tp: Symbol) -> bool:
        return any(not isinstance(b, TraitT) and b != ERROR for b in self.bounds(tp))

    def const_param_type(self, tp: Symbol):
        return next((b for b in self.bounds(tp) if not isinstance(b, TraitT)), ERROR)

    def const_type(self, sym: Symbol):
        if sym in self._consts:
            return self._consts[sym]
        if sym in self._busy:
            self.error(sym.file, "E0300", sym.span, f"constant '{sym.name}' depends on itself")
            return ERROR
        self._busy.add(sym)
        t = self.const_checker(sym.file).value(sym.node.value)
        self._busy.discard(sym)
        self._consts[sym] = t
        return t

    def const_int(self, sym: Symbol) -> int | None:
        node = getattr(sym, "node", None)
        v = _literal_value(node.value) if isinstance(node, A.ConstDecl) else None
        return v if isinstance(v, int) else None

    # ----------------------------------------------------------------- signatures

    def sig(self, sym: Symbol) -> Sig:
        s = self._sigs.get(sym)
        if s is not None:
            return s
        node = sym.node
        sig = node.sig if isinstance(node, (A.Routine, A.ExternDecl)) else node
        file = sym.file
        in_trait = isinstance(node, A.Signature)
        tparams = [self.def_sym(file, tp.name.span) for tp in sig.type_params]
        tparams = [t for t in tparams if t is not None]
        params: list[PInfo] = []
        for g, p in enumerate(sig.params):
            if p.is_varargs:
                params.append(PInfo("...", ERROR, group=g, varargs=True))
                continue
            if p.is_this:
                params.append(PInfo("this", THIS, p.is_var, group=g))
                continue
            t = self.type_of(p.type, file, this_ok=in_trait)
            for ident in p.names:
                psym = self.def_sym(file, ident.span) if isinstance(node, A.Routine) else None
                params.append(PInfo(ident.name, t, p.is_var, p.default is not None, g, sym=psym, default=p.default))
        result = self.type_of(sig.result, file, this_ok=in_trait) if sig.result is not None else VOID
        s = Sig(sym, sym.name, sig.is_function, tparams, params, result)
        self._sigs[sym] = s
        return s

    def record_sig(self, sym: Symbol) -> Sig:
        d = self.decl(sym)
        params = [
            PInfo(f.name, f.type, False, f.has_default, i, sym=f.sym, default=f.default)
            for i, f in enumerate(d.fields)
        ]
        result = Named(sym, tuple(Param(p) for p in d.params))
        return Sig(sym, sym.name, True, d.params, params, result, "record", len(params) == 1)


# ------------------------------------------------------------------ the checker


class TypeChecker:
    def __init__(self, env: Env, unit) -> None:
        self.env = env
        self.c = env.c
        self.index = env.index
        self.unit = unit
        self.file = unit.file
        self._resolver = None
        self.types: dict[int, object] = {}  # id(expr) -> type
        self.calls: dict[int, Match] = {}  # id(call) -> the overload chosen, for the backend
        self.why = ""  # why the last trait check failed
        self._in_builtin: set = set()
        # The routine being checked:
        self.result = VOID
        self.is_function = False
        self.in_routine = False
        self.in_unsafe = False
        self.tparams: list[Symbol] = []
        self.loops: list[dict] = []
        self.in_defer = False
        self.in_ensures = False
        self._new_args: set[int] = set()  # id(argument of a `new` call), for resources()

    # ------------------------------------------------------------------ helpers

    def error(self, code: str, span: Span, message: str, fixes=()) -> None:
        self.c.error(self.file, code, span, message, fixes)

    def ref_at(self, span: Span):
        return self.index.lookup(self.file, span)

    def sym_at(self, span: Span) -> Symbol | None:
        return self.env.def_sym(self.file, span)

    def text(self, span: Span) -> str:
        return self.unit.text[span.start : span.end]

    def lookup(self, name: str) -> list[Symbol]:
        """Unit-level lookup, as in a declaration of this unit."""
        if self._resolver is None:
            from .resolve import Resolver

            self._resolver = Resolver(self.c, self.unit)
        return self._resolver.lookup(name).symbols

    def lookup_routines(self, name: str) -> list[Symbol]:
        return [s for s in self.lookup(name) if s.is_routine]

    def type_of(self, t, value: bool = True):
        return self.env.type_of(t, self.file, value=value)

    def base(self, t):
        return self.env.base(t)

    def assignable(self, actual, expected) -> bool:
        if has_error(actual) or has_error(expected) or actual == expected:
            return True
        if self.base(actual) == self.base(expected):
            return True  # subrange and its base: checked at runtime
        return any(v == actual for v in self.env.variants(expected))

    def mismatch(self, span: Span, actual, expected) -> None:
        msg = f"expected {show(expected)}, found {show(actual)}"
        a, e = self.base(actual), self.base(expected)
        if is_integer(a) and is_real(e):
            msg += "; convert with toReal(...)"
        elif is_real(a) and is_integer(e):
            msg += "; convert with round(...) or toInt(...)"
        elif is_integer(a) and is_integer(e):
            msg += f"; convert with {show(e)}(...)"
        self.error("E0300", span, msg)

    # --------------------------------------------------------------------- units

    def run(self) -> None:
        items = self.unit.tree.items
        for item in items:
            if isinstance(item, A.TypeSection):
                for d in item.decls:
                    self.type_decl(d)
            elif isinstance(item, A.ConstSection):
                for d in item.decls:
                    sym = self.sym_at(d.name.span)
                    if sym is not None:
                        self.env.const_type(sym)
            elif isinstance(item, A.Routine):
                self.routine(item)
            elif isinstance(item, A.ExternBlock):
                for d in item.decls:
                    if isinstance(d, A.TypeSection):
                        for td in d.decls:
                            self.type_decl(td)
                        continue
                    sym = self.sym_at(d.sig.name.span)
                    if sym is not None:
                        self.signature(sym)
                        if item.library != "pst":
                            self.c_signature(sym, d.sig)
            elif isinstance(item, A.TestBlock):
                self.body(item.body.stmts)
        self.body([i for i in items if not isinstance(i, _DECLS)])
        if self.env.hooks():
            self.resources(self.unit.tree, None)

    def resources(self, node, parent) -> None:
        """Resource records exist only behind `ref`, and their destroy hook runs by itself
        (SPEC §11.1): E1103 for a value of one (other than `r.field`, and `R(...)` as the
        argument of `new`), E1101 for naming the hook."""
        hooks = self.env.hooks()
        if isinstance(node, A.TypeName) and not isinstance(parent, A.RefType) and not self.is_weak(parent):
            sym = self.type_sym(node)
            hook = hooks.get(sym)
            own = isinstance(parent, A.Param) and hook is not None and parent is hook.node.sig.params[0]
            if hook is not None and not own:
                self.error(
                    "E1103", node.span,
                    f"'{sym.name}' is a resource record (it has a destroy hook), so its values "
                    f"exist only behind 'ref': write 'ref {sym.name}'",
                    [Fix(f"write 'ref {sym.name}'", (Edit(_point(node.span), "ref "),))],
                )  # fmt: skip
        elif isinstance(node, A.Call) and id(node) in self.calls:
            m = self.calls[id(node)]
            if m.sig.sym is not None and m.sig.sym in hooks.values():
                self.error("E1101", node.span, "a destroy hook can't be called; it runs when the last reference goes away")
                return
            if m.sig.sym is not None and m.sig.sym is self.env.sys("new") and node.args:
                self._new_args.add(id(node.args[0]))
        elif isinstance(node, A.Name):
            r = self.ref_at(node.span)
            if r is not None and r.targets and all(s in hooks.values() for s in r.targets):
                self.error("E1101", node.span, "a destroy hook can't be used as a value; it runs by itself")
        t = self.types.get(id(node))
        if self.env.is_resource(t) and not self.resource_ok(node, parent):
            name = t.sym.name
            if isinstance(node, A.Deref):
                msg = f"'deref' of a 'ref {name}' would copy a resource record; use its fields"
            elif isinstance(node, A.Call):
                msg = f"a resource record can only be made inside 'new': 'new({self.text(node.span)})'"
            else:
                msg = f"a value of resource record '{name}' would be copied here; only 'ref {name}' can be"
            self.error("E1103", node.span, msg)
            return
        for child in _children(node):
            self.resources(child, node)

    def type_sym(self, t: A.TypeName) -> Symbol | None:
        r = self.ref_at((t.part_spans or [t.span])[-1])
        return r.targets[0] if r is not None and len(r.targets) == 1 else None

    def is_weak(self, t) -> bool:
        """Whether ``t`` is the type expression `Weak[...]`, whose argument is the type of
        what it refers to, as with `ref`."""
        if not isinstance(t, A.TypeName):
            return False
        sym = self.type_sym(t)
        return sym is not None and sym.kind is Kind.BUILTIN_TYPE and sym.name == "Weak"

    def resource_ok(self, node, parent) -> bool:
        """Whether a value of a resource record may appear as ``node``: as `r.field`, or
        `R(...)` directly inside `new`."""
        if isinstance(node, A.Deref):
            return False
        if isinstance(parent, A.FieldAccess) and parent.target is node:
            return True
        return isinstance(node, A.Call) and id(parent) in self._new_args

    def body(self, stmts: list) -> None:
        """The program body or a test block."""
        self.result, self.is_function, self.in_routine, self.tparams = VOID, False, False, []
        self.stmts(stmts)

    def type_decl(self, d: A.TypeDecl) -> None:
        sym = self.sym_at(d.name.span)
        if sym is None:
            return
        for p in d.params:
            tp = self.sym_at(p.name.span)
            if tp is not None:
                self.env.bounds(tp)
        info = self.env.decl(sym)
        if info.c:
            for f, node in zip(info.fields, d.body.fields):
                self.c_compatible(f.type, node.type.span, in_record=True)
        if info.kind == "record":
            for f in info.fields:
                if f.default is not None:
                    self.check(f.default, f.type)
        elif info.kind == "trait":
            for r in info.routines:
                self.env.sig(r)
        elif info.kind == "subrange":
            b = info.base
            if not has_error(b) and not (is_integer(b) or b == CHAR or self.env.kind_of(b) == "enum"):
                self.error("E0300", d.body.span, f"a subrange needs integer, Char or enum bounds, not {show(b)}")
            self.check(d.body.high, b)
        elif info.kind == "union":
            seen = []
            for v, node in zip(info.variants, d.body.variants):
                if not has_error(v) and v in seen:
                    self.error("E0300", node.span, f"variant {show(v)} appears twice in the union")
                seen.append(v)

    def c_signature(self, sym: Symbol, node: A.Signature) -> None:
        """An extern routine's parameters and result must be C-compatible (SPEC §10.1)."""
        sig = self.env.sig(sym)
        for p in sig.params:
            if not p.varargs:
                self.c_compatible(p.type, node.params[p.group].type.span)
        if sig.is_function:
            self.c_compatible(sig.result, node.result.span)

    def c_compatible(self, t, span, in_record: bool = False) -> bool:
        """Whether C can take or give values of type ``t`` (SPEC §10.1); E0901 if not."""
        if self.is_c_type(t, in_record):
            return True
        self.error(
            "E0901", span,
            f"{show(t)} isn't C-compatible; use numbers, Boolean, C types (CInt, CString, "
            "Ptr[T], Pointer), C records, or function types of those",
        )  # fmt: skip
        return False

    def is_c_type(self, t, in_record: bool = False) -> bool:
        if has_error(t) or t == VOID:
            return True
        if isinstance(t, Prim):
            return t not in (CHAR, STRING)
        if isinstance(t, Builtin):
            if t.name == "Ptr":
                return self.is_c_type(t.args[0], True)
            return t.name == "Array" and in_record and self.is_c_type(t.args[0], True)
        if isinstance(t, FuncT):
            return all(self.is_c_type(p) for p in t.params) and self.is_c_type(t.result)
        return isinstance(t, Named) and self.env.decl(t.sym).c

    def unsafe(self, span, what: str) -> None:
        """E0902 unless inside an unsafe routine (SPEC §10.4)."""
        if not self.in_unsafe:
            self.error("E0902", span, f"{what} is only allowed inside an 'unsafe' function or procedure")

    def signature(self, sym: Symbol) -> Sig:
        sig = self.env.sig(sym)
        for tp in sig.tparams:
            self.env.bounds(tp)
        done = set()
        for p in sig.params:
            if p.default is not None and id(p.default) not in done:
                done.add(id(p.default))
                self.check(p.default, p.type)
        return sig

    def routine(self, r: A.Routine) -> None:
        sym = self.sym_at(r.sig.name.span)
        if sym is None:
            return
        self.tparams = list(self.env.sig(sym).tparams)
        sig = self.signature(sym)
        for p in sig.params:
            if p.sym is not None:
                p.sym.type = p.type
        self.result = sig.result if sig.is_function else VOID
        self.is_function, self.in_routine, self.loops = sig.is_function, True, []
        self.in_unsafe = r.unsafe
        for e in r.requires:
            self.check(e, BOOLEAN)
        self.in_ensures = True
        for e in r.ensures:
            self.check(e, BOOLEAN)
        self.in_ensures = False
        if not self.stmts(r.body.stmts) and sig.is_function:
            self.error(
                "E0501", r.sig.name.span,
                f"function '{sym.name}' doesn't return a value on every path; "
                "end it with a 'return'",
            )  # fmt: skip
        self.tparams, self.in_routine, self.in_unsafe = [], False, False

    # ---------------------------------------------------------------- statements

    def stmts(self, stmts: list) -> bool:
        """Checks statements; true if they never complete normally (always return)."""
        ends = False
        for s in stmts:
            if self.stmt(s):
                ends = True
        return ends

    def block(self, body) -> bool:
        return self.stmts(body.stmts if isinstance(body, A.Block) else [body])

    def loop(self, body: A.Block) -> dict:
        self.loops.append({"broke": False})
        self.stmts(body.stmts)
        return self.loops.pop()

    def stmt(self, s) -> bool:
        if isinstance(s, A.VarDecl):
            sym = self.sym_at(s.name.span)
            if s.type is not None:
                t = self.type_of(s.type)
                self.check(s.value, t)
            else:
                t = self.value(s.value)
            if sym is not None:
                sym.type = t
        elif isinstance(s, A.Assign):
            t = self.value(s.target)
            why = self.immutable(s.target)
            if why:
                self.error("E0340", s.target.span, f"can't assign to {why}")
            self.check(s.value, t)
        elif isinstance(s, A.Discard):
            self.value(s.value)
        elif isinstance(s, A.ExprStmt):
            self.expr_stmt(s)
        elif isinstance(s, A.If):
            ends = True
            for b in s.branches:
                self.check(b.cond, BOOLEAN)
                ends = self.block(b.body) and ends
            if s.else_body is None:
                return False
            return self.block(s.else_body) and ends
        elif isinstance(s, A.While):
            self.check(s.cond, BOOLEAN)
            info = self.loop(s.body)
            return isinstance(s.cond, A.BoolLit) and s.cond.value and not info["broke"]
        elif isinstance(s, A.Repeat):
            self.loops.append({"broke": False, "repeat": True})
            ends = self.stmts(s.body.stmts)
            info = self.loops.pop()
            self.check(s.cond, BOOLEAN)
            forever = isinstance(s.cond, A.BoolLit) and not s.cond.value
            return (ends or forever) and not info["broke"]
        elif isinstance(s, A.ForRange):
            self.for_range(s)
        elif isinstance(s, A.ForIn):
            self.for_in(s)
        elif isinstance(s, A.Case):
            return self.case_stmt(s)
        elif isinstance(s, A.Defer):  # runs as its block is left, so can't leave itself
            outer = self.loops, self.in_defer
            self.loops, self.in_defer = [], True
            self.block(s.body)
            self.loops, self.in_defer = outer
        elif isinstance(s, (A.Break, A.Continue)):
            word = "break" if isinstance(s, A.Break) else "continue"
            if not self.loops and self.in_defer:
                self.error("E0507", s.span, f"'{word}' can't leave a deferred statement")
            elif not self.loops:
                self.error("E0504", s.span, f"'{word}' outside a loop")
            elif isinstance(s, A.Break):
                self.loops[-1]["broke"] = True
            elif self.loops[-1].get("repeat"):
                self.error("E0506", s.span, "'continue' isn't allowed in a 'repeat' loop")
        elif isinstance(s, A.Return):
            self.return_stmt(s)
            return True
        elif isinstance(s, A.Assert):
            self.check(s.cond, BOOLEAN)
            if s.message is not None:
                self.check(s.message, STRING)
        return False

    def expr_stmt(self, s: A.ExprStmt) -> None:
        e = s.expr
        if isinstance(e, A.Unary) and e.op == "try":
            self.expr(e)  # `try f()` alone: propagate the error, drop the value
            return
        t = self.expr(e)
        if isinstance(e, A.Call) and t != VOID and not has_error(t) and not isinstance(t, TypeValue):
            name = self.text(e.callee.span)
            self.error(
                "E0502", e.span,
                f"result of '{name}' is ignored; use it, or discard it with '_ := ...'",
                [Fix("discard the result", (Edit(_point(s.span), "_ := "),))],
            )  # fmt: skip

    def return_stmt(self, s: A.Return) -> None:
        if self.in_defer:
            self.error("E0507", s.span, "'return' can't leave a deferred statement")
        if s.value is None:
            if self.is_function:
                self.error("E0503", s.span, f"missing return value of type {show(self.result)}")
            return
        if not self.is_function:
            where = "a procedure" if self.in_routine else "the program body or a test"
            self.error("E0503", s.value.span, f"{where} can't return a value")
            self.value(s.value)
            return
        self.check(s.value, self.result)

    def for_range(self, s: A.ForRange) -> None:
        t = self.value(s.start)
        b = self.base(t)
        if not has_error(b) and not (is_integer(b) or b == CHAR or self.env.kind_of(b) == "enum"):
            self.error("E0300", s.start.span, f"'for ... to' needs integer, Char or enum bounds, not {show(t)}")
            t = ERROR
        self.check(s.stop, t)
        sym = self.sym_at(s.var.span)
        if sym is not None:
            sym.type = t
        self.loop(s.body)

    def for_in(self, s: A.ForIn) -> None:
        t = self.expr(s.iterable)
        elems = self.elements(t, len(s.vars), s.iterable)
        for v, et in zip(s.vars, elems):
            sym = self.sym_at(v.span)
            if sym is not None:
                sym.type = et
        self.loop(s.body)

    def elements(self, t, n: int, e) -> list:
        """The loop variable types of `for ... in e` (SPEC §4.14)."""
        bad = [ERROR] * n
        if isinstance(t, TypeValue):
            if self.env.kind_of(t.type) == "enum" and n == 1:
                return [t.type]
            t = self.as_value(t, e)
        elif t == VOID or isinstance(t, Overloads):
            t = self.as_value(t, e)
        if has_error(t):
            return bad
        if self.env.is_map(t):
            return list(t.args) if n == 2 else [t.args[0]]
        if n == 2:
            self.error("E0350", e.span, f"two loop variables ('for k, v in m') need a Map, not {show(t)}")
            return bad
        if isinstance(t, Builtin) and t.name in ("List", "Array"):
            return [t.args[0]]
        if t == STRING:
            return [CHAR]
        x = self.iterator_item(t)
        if x is not None:
            return [x]
        for f in self.lookup_routines("iter"):
            fs = self.env.sig(f)
            if len(fs.params) != 1 or isinstance(f.node, A.Signature):
                continue
            sf: dict = {}
            if unify(fs.params[0].type, t, sf, set(fs.tparams)):
                x = self.iterator_item(subst(fs.result, sf))
                if x is not None:
                    return [x]
        self.error(
            "E0350", e.span,
            f"can't iterate over {show(t)}: it isn't a List, Array, Set, Map, String, enum, "
            "or iterator, and no 'iter' function takes it",
        )  # fmt: skip
        return bad

    def iterator_item(self, t):
        it = self.env.sys("Iterator")
        if it is None:
            return None
        x = self.env.elem_var
        s: dict = {}
        if self.satisfies(t, TraitT(it, (Param(x),)), s, {x}):
            return s.get(x, ERROR)
        return None

    # ---------------------------------------------------------------------- case

    def case_stmt(self, s: A.Case) -> bool:
        info = self.case_begin(self.value(s.subject))
        ends = bool(s.arms)
        has_else = False
        for arm in s.arms:
            has_else = has_else or not arm.labels
            self.case_labels(info, arm.labels)
            ends = self.block(arm.body) and ends
        complete = self.case_end(info, has_else, s.span)
        return ends and complete

    def case_begin(self, t) -> _CaseInfo:
        if has_error(t):
            return _CaseInfo(t, "error")
        if t == BOOLEAN:
            return _CaseInfo(t, "bool", [True, False])
        kind = self.env.kind_of(t)
        if kind == "union":
            return _CaseInfo(t, "union", self.env.variants(t))
        if kind == "enum":
            return _CaseInfo(t, "enum", self.env.decl(t.sym).members)
        return _CaseInfo(t, "value")

    def case_end(self, info: _CaseInfo, has_else: bool, span: Span) -> bool:
        if info.kind == "error" or has_else:
            return True
        if info.kind == "value":
            self.error("E0602", span, f"'case' over {show(info.type)} needs an 'else' arm")
            return False
        missing = [k for k in info.keys if self.key(info, k) not in info.covered]
        if missing:
            names = ", ".join(self.key_name(info, k) for k in missing)
            self.error("E0602", span, f"'case' doesn't cover {names}; add arms for them, or 'else'")
            return False
        return True

    def key(self, info: _CaseInfo, k):
        return info.keys.index(k) if info.kind == "union" else k

    @staticmethod
    def key_name(info: _CaseInfo, k) -> str:
        if info.kind == "union":
            return show(k)
        if info.kind == "bool":
            return "true" if k else "false"
        return str(k)

    def cover(self, info: _CaseInfo, key, span: Span, name: str) -> None:
        if key in info.covered:
            self.error("E0601", span, f"'{name}' is already covered by an earlier label")
        info.covered.add(key)

    def case_labels(self, info: _CaseInfo, labels: list) -> None:
        for label in labels:
            if isinstance(label, A.VariantLabel):
                self.variant_label(info, label)
            elif isinstance(label, A.ValueLabel):
                if info.kind in ("union", "enum"):
                    self.named_label(info, label.value)
                else:
                    v = self.const_label(info, label.value)
                    self.add_range(info, v, v, label.span)
            elif isinstance(label, A.RangeLabel):
                if info.kind == "enum":
                    lo, hi = self.named_label(info, label.low, cover=False), self.named_label(info, label.high, cover=False)
                    if lo is not None and hi is not None:
                        members = info.keys[info.keys.index(lo) : info.keys.index(hi) + 1]
                        if not members:
                            self.error("E0603", label.span, f"empty range: '{lo}' comes after '{hi}'")
                        for m in members:
                            self.cover(info, m, label.span, m)
                elif info.kind in ("union", "bool"):
                    self.error("E0603", label.span, f"a range label needs integer, Char or enum values, not {show(info.type)}")
                else:
                    lo, hi = self.const_label(info, label.low), self.const_label(info, label.high)
                    if lo is not None and hi is not None and not isinstance(lo, str) and lo > hi:
                        self.error("E0603", label.span, "empty range: the low end is above the high end")
                    self.add_range(info, lo, hi, label.span)

    def add_range(self, info: _CaseInfo, lo, hi, span: Span) -> None:
        if lo is None or hi is None:
            return
        if info.kind == "bool":
            self.cover(info, lo, span, "true" if lo else "false")
            return
        for a, b in info.ranges:
            if type(a) is type(lo) and not (hi < a or lo > b):
                self.error("E0601", span, "this label overlaps an earlier one")
                break
        info.ranges.append((lo, hi))

    def label_head(self, sym: Symbol):
        """What a variant label names: a declared type, or a primitive/builtin name."""
        if sym.kind is Kind.TYPE:
            d = self.env.decl(sym)
            if d.kind == "alias" and not d.params:
                return self.head(d.alias)
            return sym
        if sym.kind is Kind.BUILTIN_TYPE:
            return sym.name
        return None

    @staticmethod
    def head(t):
        if isinstance(t, Named):
            return t.sym
        if isinstance(t, (Prim, Builtin)):
            return t.name
        return None

    def variant_of(self, info: _CaseInfo, sym: Symbol, span: Span, name: str):
        h = self.label_head(sym)
        for v in info.keys:
            if h is not None and self.head(v) is h or (isinstance(h, str) and self.head(v) == h):
                return v
        variants = ", ".join(show(v) for v in info.keys)
        self.error("E0603", span, f"'{name}' is not a variant of {show(info.type)} (variants: {variants})")
        return None

    def variant_label(self, info: _CaseInfo, label: A.VariantLabel) -> None:
        tn = label.variant
        binding = self.sym_at(label.binding.span)
        vt = ERROR
        name = ".".join(tn.parts)
        r = self.ref_at((tn.part_spans or [tn.span])[-1])
        if info.kind == "union":
            if r is not None and len(r.targets) == 1:
                v = self.variant_of(info, r.targets[0], tn.span, name)
                if v is not None:
                    vt = v
                    self.cover(info, info.keys.index(v), label.span, name)
        elif info.kind != "error":
            self.error("E0603", label.span, f"'{name}(...)' needs a union to match, not {show(info.type)}")
        if binding is not None:
            binding.type = vt

    def named_label(self, info: _CaseInfo, e, cover: bool = True):
        """A bare enum member or variant name (`Red:`, `None:`). Returns the enum member."""
        if isinstance(e, A.FieldAccess):  # `Color.Red`, `System.None`
            r = self.ref_at(e.name.span)
            if r is None or len(r.targets) != 1:
                return None
            sym, span, name = r.targets[0], e.span, self.text(e.span)
        elif isinstance(e, A.Name):
            sym, span, name = None, e.span, e.name
        else:
            self.error("E0603", e.span, f"labels over {show(info.type)} must be names")
            self.value(e)
            return None
        if info.kind == "enum":
            member = e.name.name if isinstance(e, A.FieldAccess) else e.name
            if member not in info.keys or (sym is not None and self.env.owner.get(sym) is not info.type.sym):
                self.error("E0603", span, f"'{name}' is not a member of {show(info.type)}")
                return None
            if sym is None:
                self.index.add(self.file, span, [info.type.sym.members[member]])
            if cover:
                self.cover(info, member, span, member)
            return member
        if sym is None:
            found = [s for s in self.lookup(name) if s.kind in (Kind.TYPE, Kind.BUILTIN_TYPE)]
            if len(found) != 1:
                self.error("E0603", span, f"'{name}' is not a variant of {show(info.type)}")
                return None
            sym = found[0]
            self.index.add(self.file, span, [sym])
        v = self.variant_of(info, sym, span, name)
        if v is not None:
            if self.env.kind_of(v) != "record" or self.env.decl(v.sym).fields:
                self.error("E0603", span, f"variant {show(v)} has fields; bind it: '{name}(x)' or '{name}(_)'")
            if cover:
                self.cover(info, info.keys.index(v), span, name)
        return None

    def const_label(self, info: _CaseInfo, e):
        """A constant label over a non-enum, non-union type. Returns its value, if known."""
        if isinstance(e, A.Name) and self.ref_at(e.span) is None:
            found = self.lookup(e.name)
            if not found:
                self.error("E0100", e.span, f"unknown name '{e.name}'")
                return None
            sym = found[0]
            if sym.kind is not Kind.CONST:
                self.error("E0603", e.span, f"case labels must be constants; '{e.name}' is a {sym.kind.value}")
                return None
            self.index.add(self.file, e.span, [sym])
            t = self.env.const_type(sym)
            if info.kind != "error" and not self.assignable(t, info.type):
                self.mismatch(e.span, t, info.type)
            return self.env.const_int(sym)
        if info.kind == "error":
            self.value(e)
            return None
        self.check(e, info.type)
        if isinstance(e, A.BoolLit):
            return e.value
        if isinstance(e, A.CharLit):
            return ord(e.value)
        if isinstance(e, A.StringLit):
            return e.value
        v = _literal_value(e)
        if isinstance(v, int):
            return v
        if not isinstance(e, (A.Name, A.FieldAccess)):
            self.error("E0603", e.span, "case labels must be constants")
        return None

    # --------------------------------------------------------------- expressions

    def expr(self, e, expected=None):
        """The type of ``e``. ``expected`` is a hint: literals and empty collections take
        it, and calls use it to infer type arguments. It is not checked here."""
        m = getattr(self, "e_" + type(e).__name__, None)
        t = m(e, expected) if m is not None else ERROR
        self.types[id(e)] = t
        return t

    def value(self, e, expected=None):
        """Like ``expr``, but the result must be a value."""
        return self.as_value(self.expr(e, expected), e)

    def as_value(self, t, e):
        if isinstance(t, TypeValue):
            self.error("E0306", e.span, f"'{self.text(e.span)}' is a type, not a value")
            return ERROR
        if t == VOID:
            self.error("E0306", e.span, f"'{self.text(e.span)}' is a procedure call and has no value")
            return ERROR
        if isinstance(t, Overloads):
            self.error(
                "E0302", e.span,
                f"'{t.syms[0].name}' is overloaded; the expected type must say which one is meant",
            )  # fmt: skip
            return ERROR
        return t

    def check(self, e, expected):
        """Checks that ``e`` is a value of type ``expected``."""
        t = self.value(e, expected)
        if expected is not None and not self.assignable(t, expected):
            self.mismatch(e.span, t, expected)
        return t

    # literals

    def int_lit(self, v: int, span: Span, expected):
        b = self.base(expected) if expected is not None else None
        t = expected if is_integer(b) else INTEGER
        lo, hi = _INT_RANGE[(b if is_integer(b) else INTEGER).name]
        if not lo <= v <= hi:
            self.error("E0300", span, f"{v} doesn't fit in {show(t)}")
        return t

    def e_IntLit(self, e, expected):
        return self.int_lit(e.value, e.span, expected)

    def e_RealLit(self, e, expected):
        b = self.base(expected) if expected is not None else None
        return expected if is_real(b) else REAL

    def e_CharLit(self, e, expected):
        return expected if expected is not None and self.base(expected) == CHAR else CHAR

    def e_BoolLit(self, e, expected):
        return BOOLEAN

    def e_StringLit(self, e, expected):
        return STRING

    def e_InterpString(self, e, expected):
        printable = self.env.sys("Printable")
        for p in e.parts:
            if isinstance(p, str):
                continue
            t = self.value(p)
            if printable is not None and not self.satisfies(t, TraitT(printable, ())):
                self.error(
                    "E0421", p.span,
                    f"{show(t)} can't be shown in a string: it isn't Printable "
                    f"(declare 'function toString(x: {show(t)}): String')",
                )  # fmt: skip
        return STRING

    def e_ErrorExpr(self, e, expected):
        return ERROR

    def e_Paren(self, e, expected):
        return self.expr(e.inner, expected)

    def e_ListLit(self, e, expected):
        if isinstance(expected, Builtin) and expected.name in ("List", "Array"):
            for x in e.items:
                self.check(x, expected.args[0])
            n = expected.args[1] if expected.name == "Array" else None
            if isinstance(n, ConstArg) and isinstance(n.value, int) and n.value != len(e.items):
                self.error("E0300", e.span, f"{show(expected)} needs {n.value} elements, found {len(e.items)}")
            return expected
        if not e.items:
            self.error("E0301", e.span, "the type of '[]' isn't known here; declare it: 'var xs: List[T] := []'")
            return ERROR
        first = self.value(e.items[0])
        for x in e.items[1:]:
            self.check(x, first)
        return Builtin("List", (first,))

    def e_SetLit(self, e, expected):
        if self.env.is_set(expected):
            for x in e.items:
                self.check(x, expected.args[0])
            return expected
        first = self.value(e.items[0])
        for x in e.items[1:]:
            self.check(x, first)
        self.env.check_element(self.file, e.span, first)
        return Named(self.env.sys("Set"), (first,))

    def e_MapLit(self, e, expected):
        if self.env.is_map(expected):
            k, v = expected.args
        else:
            first = e.entries[0]
            k, v = self.value(first.key), self.value(first.value)
            for x in (first.key, first.value):
                self.types[id(x)] = self.types.get(id(x))
            rest = e.entries[1:]
            for entry in rest:
                self.check(entry.key, k)
                self.check(entry.value, v)
            return Named(self.env.sys("Map"), (k, v))
        for entry in e.entries:
            self.check(entry.key, k)
            self.check(entry.value, v)
        return expected

    def e_EmptyBraces(self, e, expected):
        if self.env.is_map(expected) or self.env.is_set(expected):
            return expected
        self.error("E0301", e.span, "the type of '{}' isn't known here; declare it: 'var m: Map[K, V] := {}'")
        return ERROR

    # names

    def e_Name(self, e, expected):
        r = self.ref_at(e.span)
        if r is None or not r.targets:
            return ERROR  # reported by stage 1
        return self.sym_value(list(r.targets), e.span, expected)

    def sym_value(self, syms: list, span: Span, expected):
        s = syms[0]
        if s.is_routine:
            return self.routine_value(syms, span, expected)
        k = s.kind
        if k in LOCALS and k is not Kind.RESULT:
            return s.type if s.type is not None else ERROR
        if k is Kind.RESULT:
            return self.result
        if k is Kind.CONST:
            return self.env.const_type(s)
        if k is Kind.ENUM_MEMBER:
            owner = self.env.owner.get(s)
            return Named(owner) if owner is not None else ERROR
        if k is Kind.TYPE:
            d = self.env.decl(s)
            if d.kind == "record" and not d.fields and not d.params:
                return Named(s)  # `None`: a record without fields is a value
            if d.kind == "alias":
                return TypeValue(d.alias)
            return TypeValue(Named(s, tuple(Param(p) for p in d.params)))
        if k is Kind.BUILTIN_TYPE:
            return TypeValue(Prim(s.name) if s.name not in _ARITY else Builtin(s.name, ()))
        if k is Kind.TYPE_PARAM:
            if self.env.is_const_param(s):
                return self.env.const_param_type(s)
            return TypeValue(Param(s))
        self.error("E0306", span, f"{s.describe()} is not a value")
        return ERROR

    def routine_value(self, syms: list, span: Span, expected, targs=None):
        sigs = [self.env.sig(s) for s in syms if not isinstance(s.node, A.Signature)]
        if targs is not None:
            sigs = [g for g in sigs if len(g.tparams) == len(targs)]
            sigs = [replace(g, tparams=[], params=[replace(p, type=subst(p.type, dict(zip(g.tparams, targs)))) for p in g.params],
                            result=subst(g.result, dict(zip(g.tparams, targs)))) for g in sigs]  # fmt: skip
        else:
            sigs = [g for g in sigs if not g.tparams] or sigs
        if isinstance(expected, FuncT):
            fit = [g for g in sigs if not g.tparams and g.functype() == expected]
            if len(fit) == 1:
                if fit[0].sym is not None:
                    self.index.narrow(self.file, span, [fit[0].sym])
                return expected
        if len(sigs) == 1:
            g = sigs[0]
            if g.tparams:
                self.error(
                    "E0302", span,
                    f"generic '{g.name}' must be instantiated to be used as a value: "
                    f"'{g.name}[{', '.join(t.name for t in g.tparams)}]' with the types filled in",
                )  # fmt: skip
                return ERROR
            if g.sym is not None:
                self.index.narrow(self.file, span, [g.sym])
            return g.functype()
        if not sigs:
            self.error("E0302", span, f"'{syms[0].name}' can't be used as a value here")
            return ERROR
        return Overloads(tuple(g.sym for g in sigs))

    def static_owner(self, e) -> Symbol | None:
        """The unit or enum that ``e`` names, if it names one (the left of `Unit.x`)."""
        if isinstance(e, A.Name):
            span = e.span
        elif isinstance(e, A.FieldAccess):
            if self.static_owner(e.target) is None:
                return None
            span = e.name.span
        else:
            return None
        r = self.ref_at(span)
        if r is None or len(r.targets) != 1:
            return None
        sym = r.targets[0]
        if sym.kind is Kind.UNIT or (sym.kind is Kind.TYPE and isinstance(getattr(sym.node, "body", None), A.EnumType)):
            return sym
        return None

    def e_FieldAccess(self, e, expected):
        if self.static_owner(e.target) is not None:
            r = self.ref_at(e.name.span)
            if r is None or not r.targets:
                return ERROR
            return self.sym_value(list(r.targets), e.name.span, expected)
        t = self.value(e.target)
        return self.field(t, e.name)

    def find_field(self, t, name: str):
        """(FieldInfo, owner Decl, substitution) for a field of ``t``, or None."""
        b = t.target if isinstance(t, RefT) else t
        if isinstance(b, Named):
            d = self.env.decl(b.sym)
            if d.kind == "record":
                f = d.field(name)
                if f is not None:
                    return f, b.sym, dict(zip(d.params, b.args))
        elif isinstance(b, Param):
            for bound in self.env.bounds(b.sym):
                if isinstance(bound, TraitT) and bound.sym.kind is Kind.TRAIT:
                    d = self.env.decl(bound.sym)
                    f = d.field(name)
                    if f is not None:
                        s = dict(zip(d.params, bound.args))
                        s[THIS] = b
                        return f, None, s
        return None

    def field(self, t, ident: A.Ident):
        if has_error(t):
            return ERROR
        found = self.find_field(t, ident.name)
        if found is not None:
            f, owner, s = found
            if owner is not None and not f.exported and owner.unit != self.unit.name:
                self.error("E0321", ident.span, f"field '{f.name}' of {owner.name} is private to unit {owner.unit}")
            if f.sym is not None:
                self.index.add(self.file, ident.span, [f.sym])
            return subst(f.type, s)
        b = t.target if isinstance(t, RefT) else t
        if isinstance(b, Param):
            self.error("E0710", ident.span, f"'{b.sym.name}' has no field '{ident.name}': no bound of '{b.sym.name}' provides one")
            return ERROR
        msg = f"{show(t)} has no field '{ident.name}'"
        if self.lookup_routines(ident.name):
            msg += f"; to call function '{ident.name}', add parentheses: '.{ident.name}()'"
        self.error("E0320", ident.span, msg)
        return ERROR

    def e_Deref(self, e, expected):
        t = self.value(e.target)
        if isinstance(t, RefT):
            return t.target
        if isinstance(t, Builtin) and t.name == "Ptr":
            self.unsafe(e.span, "dereferencing a pointer")
            return t.args[0]
        if not has_error(t):
            self.error("E0305", e.span, f"'deref' needs a ref, not {show(t)}")
        return ERROR

    # operators

    def e_Unary(self, e, expected):
        if e.op == "try":
            return self.try_expr(e)
        if e.op == "not":
            self.check(e.operand, BOOLEAN)
            return BOOLEAN
        v = _literal_value(e)
        if isinstance(v, int):
            return self.int_lit(v, e.span, expected)
        if isinstance(v, float):
            return self.e_RealLit(e, expected)
        t = self.value(e.operand, expected)
        b = self.base(t)
        if has_error(b) or is_numeric(b):
            return b
        if isinstance(b, Param):
            self.error("E0710", e.span, f"'-' isn't available on '{b.sym.name}': its bounds don't provide arithmetic")
        else:
            self.error("E0305", e.span, f"'-' needs a number, not {show(t)}")
        return ERROR

    def try_expr(self, e):
        t = self.value(e.operand)
        if has_error(t):
            return ERROR
        opt, res = self.env.sys("Option"), self.env.sys("Result")
        if not isinstance(t, Named) or t.sym not in (opt, res):
            self.error("E0505", e.span, f"'try' needs an Option or a Result, not {show(t)}")
            return ERROR
        if self.in_defer:
            self.error("E0507", e.span, "'try' can't leave a deferred statement")
            return t.args[0]
        r = self.result if self.is_function else None
        want = "an Option" if t.sym is opt else f"a Result[..., {show(t.args[1])}]"
        if r is None:
            self.error("E0505", e.span, f"'try' can only be used in a function that returns {want}")
        elif not has_error(r):
            if not isinstance(r, Named) or r.sym is not t.sym:
                self.error("E0505", e.span, f"'try' here needs the function to return {want}, not {show(r)}")
            elif t.sym is res and not has_error(t.args[1]) and r.args[1] != t.args[1]:
                self.error(
                    "E0505", e.span,
                    f"'try' would return an error of type {show(t.args[1])}, but the function "
                    f"returns errors of type {show(r.args[1])}",
                )  # fmt: skip
        return t.args[0]

    def operands(self, e, hint):
        if _is_literal(e.left) and not _is_literal(e.right):
            rt = self.value(e.right, hint)
            lt = self.value(e.left, rt)
        else:
            lt = self.value(e.left, hint)
            rt = self.value(e.right, lt)
        return lt, rt

    def e_Binary(self, e, expected):
        op = e.op
        if op in ("and", "or"):
            self.check(e.left, BOOLEAN)
            self.check(e.right, BOOLEAN)
            return BOOLEAN
        if op == "in":
            return self.in_op(e)
        lt, rt = self.operands(e, expected if op in _ARITH else None)
        if op in _ARITH:
            return self.arith(e, lt, rt)
        if has_error(lt) or has_error(rt):
            return BOOLEAN
        if not (self.assignable(lt, rt) or self.assignable(rt, lt)):
            self.error("E0305", e.op_span, f"can't compare {show(lt)} with {show(rt)}")
            return BOOLEAN
        t = rt if lt in self.env.variants(rt) else lt
        trait = "Equatable" if op in ("=", "<>") else "Ordered"
        if not self.builtin_trait(t, trait):
            if isinstance(t, Param):
                self.error("E0710", e.op_span, f"'{op}' needs '{t.sym.name}: {trait}'; add the bound")
            else:
                self.error("E0305", e.op_span, f"'{op}' doesn't work on {show(t)}: it isn't {trait}")
        return BOOLEAN

    def arith(self, e, lt, rt):
        op = e.op
        if has_error(lt) or has_error(rt):
            return ERROR
        lb, rb = self.base(lt), self.base(rt)
        if isinstance(lb, Builtin) and lb.name == "Ptr" and is_integer(rb) and op in ("+", "-"):
            self.unsafe(e.op_span, "pointer arithmetic")
            return lt
        for b in (lb, rb):
            if isinstance(b, Param):
                self.error("E0710", e.op_span, f"'{op}' isn't available on '{b.sym.name}': its bounds don't provide arithmetic")
                return ERROR
        if lb != rb or not is_numeric(lb):
            msg = f"'{op}' can't combine {show(lt)} and {show(rt)}"
            if STRING in (lb, rb):
                msg += "; build strings with interpolation: \"{a}{b}\""
            elif any(isinstance(b, Builtin) and b.name == "List" for b in (lb, rb)):
                msg += "; join lists with concat(a, b)"
            elif is_numeric(lb) and is_numeric(rb):
                msg += "; mixed numeric types: convert one side (toReal, round, Int32(...))"
            self.error("E0305", e.op_span, msg)
            return ERROR
        if op == "/" and not is_real(lb):
            self.error("E0305", e.op_span, "'/' works on Real only; use 'div' for integer division")
            return ERROR
        if op in ("div", "mod") and not is_integer(lb):
            self.error("E0305", e.op_span, f"'{op}' works on integers only; use '/' for Real")
            return ERROR
        return lb

    def in_op(self, e):
        rt = self.value(e.right)
        if isinstance(rt, Builtin) and rt.name == "List" or self.env.is_map(rt) or self.env.is_set(rt):
            self.check(e.left, rt.args[0])
        else:
            self.value(e.left)
            if not has_error(rt):
                self.error("E0305", e.op_span, f"'in' needs a Set, List or Map on the right, not {show(rt)}")
        return BOOLEAN

    def e_IfExpr(self, e, expected):
        for b in e.branches:
            self.check(b.cond, BOOLEAN)
        return self.branches([b.value for b in e.branches] + [e.else_value], expected)

    def e_CaseExpr(self, e, expected):
        info = self.case_begin(self.value(e.subject))
        has_else = False
        for arm in e.arms:
            has_else = has_else or not arm.labels
            self.case_labels(info, arm.labels)
        self.case_end(info, has_else, e.span)
        return self.branches([arm.value for arm in e.arms], expected)

    def branches(self, values: list, expected):
        if expected is not None and not has_error(expected):
            for v in values:
                self.check(v, expected)
            return expected
        first = None
        for v in values:
            t = self.value(v, first)
            if first is None or has_error(first):
                first = t
            elif not has_error(t) and t != first:
                self.error(
                    "E0802", v.span,
                    f"this branch is {show(t)}, but the first is {show(first)}; "
                    "all branches must have the same type",
                )  # fmt: skip
        return first if first is not None else ERROR

    # indexing

    def type_args(self, e: A.Index):
        """The type arguments of `f[T]` / `Stack[T]`, if ``e`` is an instantiation."""
        target = e.target
        if isinstance(target, A.Name):
            r = self.ref_at(target.span)
        elif isinstance(target, A.FieldAccess) and self.static_owner(target.target) is not None:
            r = self.ref_at(target.name.span)
        else:
            return None
        if r is None or not r.targets:
            return None
        s = r.targets[0]
        if not (s.is_routine or s.kind is Kind.TYPE):
            return None
        out = []
        for x in e.items:
            if isinstance(x, A.IntLit):
                out.append(ConstArg(x.value))
                continue
            tn = _expr_to_type(x)
            out.append(self.env.type_arg(tn, self.file, False) if tn is not None else ConstArg(A.dump(x)))
        return out

    def e_Index(self, e, expected):
        targs = self.type_args(e)
        if targs is not None:
            target = e.target
            span = target.span if isinstance(target, A.Name) else target.name.span
            syms = list(self.ref_at(span).targets)
            if syms[0].is_routine:
                return self.routine_value(syms, span, expected, targs)
            d = self.env.decl(syms[0])
            if len(targs) != len(d.params):
                return self.env.arity(self.file, e.span, syms[0].name, len(d.params), len(targs))
            return TypeValue(Named(syms[0], tuple(targs)))
        t = self.value(e.target)
        b = t.target if isinstance(t, RefT) else t
        if len(e.items) != 1:
            for x in e.items:
                self.value(x)
            if not has_error(b):
                self.error("E0311", e.span, "index with one value: 'a[i]'")
            return ERROR
        item = e.items[0]
        if isinstance(b, Builtin) and b.name in ("List", "Array"):
            it = self.value(item, INTEGER)
            if not has_error(it) and not is_integer(self.base(it)):
                self.error("E0300", item.span, f"an index must be an integer, not {show(it)}")
            return b.args[0]
        if self.env.is_map(b):
            self.check(item, b.args[0])
            return b.args[1]
        self.value(item)
        if b == STRING:
            self.error(
                "E0310", e.span,
                "strings can't be indexed; iterate with 'for c in s', index the bytes with "
                "'bytes(s)[i]', or cut with 'slice(s, start, stop)'",
            )  # fmt: skip
        elif not has_error(b):
            self.error("E0311", e.span, f"{show(t)} can't be indexed")
        return ERROR

    # ------------------------------------------------------------------- calls

    def e_Call(self, e, expected):
        callee = e.callee
        explicit = None
        if isinstance(callee, A.Index):
            explicit = self.type_args(callee)
            if explicit is not None:
                callee = callee.target
        if (
            self.in_ensures and isinstance(callee, A.Name) and callee.name == "old"
            and self.ref_at(callee.span) is None and len(e.args) == 1
        ):  # fmt: skip
            return self.value(e.args[0].value, expected)
        receiver = None
        if isinstance(callee, A.Name):
            name, span = callee.name, callee.span
        elif isinstance(callee, A.FieldAccess) and self.static_owner(callee.target) is not None:
            name, span = callee.name.name, callee.name.span
        elif isinstance(callee, A.FieldAccess):
            name, span = callee.name.name, None
            rt = self.value(callee.target)
            if has_error(rt):
                self.prepare(e.args)
                return ERROR
            if self.find_field(rt, name) is not None:
                return self.call_value(e, self.field(rt, callee.name), name)
            receiver = CArg(callee.target.span, None, False, callee.target, rt, receiver=True)
            cands = self.ufcs_candidates(name)
            if not cands:
                self.prepare(e.args)
                self.error("E0320", callee.name.span, f"{show(rt)} has no field '{name}', and no function '{name}' is visible")
                return ERROR
            return self.call(e, name, cands, receiver, explicit, expected, None)
        else:
            return self.call_value(e, self.value(callee), self.text(callee.span))
        r = self.ref_at(span)
        if r is None or not r.targets:
            self.prepare(e.args)
            return ERROR
        syms = list(r.targets)
        s0 = syms[0]
        if s0.kind in (Kind.TYPE, Kind.BUILTIN_TYPE):
            return self.construct(e, s0, explicit, expected)
        if s0.is_extern and s0.unit == "C" and s0.name == "cast":
            return self.cast(e, explicit)
        if s0.is_extern and s0.unit == "C" and s0.name == "addr":
            t = self.call(e, name, self.candidates(syms), None, explicit, expected, span)
            self.addr(e, t)
            return t
        if s0.is_routine:
            return self.call(e, name, self.candidates(syms), None, explicit, expected, span)
        return self.call_value(e, self.as_value(self.sym_value(syms, span, None), callee), name)

    def cast(self, e, explicit):
        """`cast[T](x)`: reinterprets a pointer or integer as another (SPEC §10.4)."""
        if explicit is None or len(explicit) != 1 or len(e.args) != 1 or e.args[0].name or e.args[0].is_var:
            self.prepare(e.args)
            self.error("E0511", e.span, "write 'cast[T](x)': one type argument and one value")
            return ERROR
        target = explicit[0]
        t = self.value(e.args[0].value)
        self.unsafe(e.span, "'cast'")
        if not (has_error(t) or has_error(target) or (_castable(self.base(t)) and _castable(target))):
            self.error("E0904", e.span, f"can't cast {show(t)} to {show(target)}: 'cast' converts between pointers (Ptr[T], Pointer, CString) and integers")
        return target

    def addr(self, e, t) -> None:
        """`addr(var x)`: the address of a variable, or of a field of a C record in one."""
        self.unsafe(e.span, "'addr'")
        if len(e.args) != 1 or has_error(t):
            return
        arg = e.args[0].value
        if not self.c_compatible(t.args[0], arg.span):
            return
        place = arg
        while True:
            place = _unparen(place)
            if isinstance(place, A.FieldAccess) and self.env.kind_of(self.types.get(id(place.target))) == "record":
                if not self.env.decl(self.types[id(place.target)].sym).c:
                    break
                place = place.target
                continue
            break
        if not isinstance(place, A.Name):
            self.error("E0905", arg.span, "'addr' needs a variable, or a field of a C record held in one")

    def bound_candidates(self, member_of) -> list[Cand]:
        """Candidates from the bounds of the type parameters in scope. ``member_of(d)``
        picks the trait routines wanted from a trait declaration."""
        out = []
        for tp in self.tparams:
            for b in self.env.bounds(tp):
                if isinstance(b, TraitT) and b.sym.kind is Kind.TRAIT:
                    d = self.env.decl(b.sym)
                    for m in member_of(b.sym, d):
                        pre = dict(zip(d.params, b.args))
                        pre[THIS] = Param(tp)
                        out.append(Cand(self.env.sig(m), pre))
        return out

    def candidates(self, syms: list) -> list[Cand]:
        out = []
        for s in syms:
            if isinstance(s.node, A.Signature):  # a bound member (SPEC §8.2)
                out += self.bound_candidates(lambda tr, d: [s] if self.env.owner.get(s) is tr else [])
            else:
                out.append(Cand(self.env.sig(s)))
        return out

    def ufcs_candidates(self, name: str) -> list[Cand]:
        bound = self.bound_candidates(lambda tr, d: [m for m in d.routines if m.name == name])
        if bound:
            return bound
        return [Cand(self.env.sig(s)) for s in self.lookup_routines(name) if not isinstance(s.node, A.Signature)]

    def construct(self, e, sym: Symbol, explicit, expected):
        """`Point(x: 1.0)`, `Some(5)`, or a conversion like `Int32(x)`."""
        if sym.kind is Kind.BUILTIN_TYPE:
            if sym.name in _ARITY:
                self.prepare(e.args)
                self.error("E0306", e.callee.span, f"'{sym.name}' values are written as literals, not constructed")
                return ERROR
            return self.convert(e, Prim(sym.name))
        d = self.env.decl(sym)
        pre = {}
        target = sym
        if d.kind == "alias":
            a = d.alias
            if isinstance(a, Named) and self.env.kind_of(a) == "record":
                target, d = a.sym, self.env.decl(a.sym)
                pre = dict(zip(d.params, a.args))
            elif is_numeric(a) or a == CHAR or self.env.kind_of(a) == "subrange":
                return self.convert(e, a)
            else:
                self.prepare(e.args)
                if not has_error(a):
                    self.error("E0306", e.callee.span, f"{show(a)} can't be constructed like this")
                return ERROR
        if d.kind == "subrange":
            return self.convert(e, Named(sym))
        if d.kind != "record":
            self.prepare(e.args)
            what = {"union": "construct one of its variants", "enum": f"write a member: '{sym.name}.X'"}
            self.error("E0306", e.callee.span, f"'{sym.name}' is {'an' if d.kind in ('enum', 'alias') else 'a'} {d.kind}; {what.get(d.kind, 'it has no values')}")
            return ERROR
        private = [f.name for f in d.fields if not f.exported]
        if target in (self.env.sys("Map"), self.env.sys("Set")) and target.unit != self.unit.name:
            self.prepare(e.args)
            lit = "{k: v}" if target.name == "Map" else "{a, b}"
            self.error("E0306", e.callee.span, f"'{target.name}' values are written as literals: '{lit}', or '{{}}' where the type is known")
            return ERROR
        if target.unit != self.unit.name and private:
            self.prepare(e.args)
            self.error(
                "E0330", e.span,
                f"{target.name} has private fields ({', '.join(private)}), so only unit "
                f"{target.unit} can construct it; use a function it exports",
            )  # fmt: skip
            return ERROR
        cand = Cand(self.env.record_sig(target), pre)
        return self.call(e, target.name, [cand], None, explicit, expected, None)

    def convert(self, e, target):
        args = e.args
        if len(args) != 1 or args[0].name is not None or args[0].is_var:
            self.prepare(args)
            self.error("E0511", e.span, f"conversion to {show(target)} takes one value: '{show(target)}(x)'")
            return target
        t = self.value(args[0].value, target if _is_literal(args[0].value) else None)
        a, b = self.base(t), self.base(target)
        ok = (
            has_error(t) or a == b or (is_integer(a) and is_integer(b)) or (is_real(a) and is_real(b))
        )
        if not ok:
            msg = f"can't convert {show(t)} to {show(target)}"
            if is_integer(a) and is_real(b):
                msg += "; use toReal(x)"
            elif is_real(a) and is_integer(b):
                msg += "; use round(x) or toInt(x)"
            self.error("E0300", args[0].value.span, msg)
        return target

    def call_value(self, e, t, name: str):
        if has_error(t):
            self.prepare(e.args)
            return ERROR
        if not isinstance(t, FuncT):
            self.prepare(e.args)
            self.error("E0307", e.callee.span, f"'{name}' is {show(t)}, not a function")
            return ERROR
        params = [PInfo(f"#{i + 1}", p, group=i) for i, p in enumerate(t.params)]
        sig = Sig(None, name, t.is_function, [], params, t.result, kind="value")
        return self.call(e, name, [Cand(sig)], None, None, None, None)

    def flex(self, v):
        """Arguments whose type depends on the parameter: literals, `[]`, `{}`, and
        overloaded function names."""
        if isinstance(v, A.Paren):
            return self.flex(v.inner)
        lit = _literal_value(v)
        if isinstance(lit, int):
            return INT_LIT
        if isinstance(lit, float):
            return REAL_LIT
        if isinstance(v, A.ListLit) and not v.items:
            return EMPTY_LIST
        if isinstance(v, A.EmptyBraces):
            return EMPTY_BRACES
        span = None
        if isinstance(v, A.Name):
            span = v.span
        elif isinstance(v, A.FieldAccess) and self.static_owner(v.target) is not None:
            span = v.name.span
        if span is not None:
            r = self.ref_at(span)
            if r is not None and len(r.targets) > 1 and all(s.is_routine for s in r.targets):
                return Overloads(r.targets)
        return None

    def prepare(self, args: list) -> list[CArg]:
        out = []
        for a in args:
            f = self.flex(a.value)
            t = None if f is not None else self.value(a.value)
            out.append(CArg(a.span, a.name, a.is_var, a.value, t, f))
        return out

    @staticmethod
    def arg_desc(a: CArg) -> str:
        if a.flex is None:
            return show(a.type)
        return {INT_LIT: "integer literal", REAL_LIT: "real literal", EMPTY_LIST: "[]", EMPTY_BRACES: "{}"}.get(
            a.flex, "overloaded function"
        )

    def call(self, e, name: str, cands: list[Cand], receiver, explicit, expected, ref_span):
        args = ([receiver] if receiver is not None else []) + self.prepare(e.args)
        matches, fails = [], []
        for c in cands:
            m = self.match(c, args, explicit, expected, e.span)
            (matches if isinstance(m, Match) else fails).append(m)
        if not matches:
            bound = [f for f in fails if f.bound]
            f = fails[0] if len(fails) == 1 else bound[0] if len(bound) == 1 else None
            if f is not None:
                self.error(f.code, f.span, f.message)
            elif cands:
                options = "; ".join(c.sig.describe() for c in cands[:6])
                self.error(
                    "E0511", e.span,
                    f"no overload of '{name}' takes ({', '.join(self.arg_desc(a) for a in args)}); "
                    f"candidates: {options}",
                )  # fmt: skip
            return ERROR
        best = min((m.tier, bool(m.sig.tparams)) for m in matches)
        top = [m for m in matches if (m.tier, bool(m.sig.tparams)) == best]
        if len(top) > 1:
            options = "; ".join(m.sig.describe() for m in top)
            self.error("E0510", e.span, f"call to '{name}' is ambiguous: {options}")
            return ERROR
        m = top[0]
        self.finish(e, name, m, receiver, ref_span)
        self.calls[id(e)] = m
        return subst(m.result, m.s)

    def match(self, c: Cand, args: list[CArg], explicit, expected, span: Span):
        sig = c.sig
        pre = c.pre
        params = [replace(p, type=subst(p.type, pre)) for p in sig.params] if pre else sig.params
        result = subst(sig.result, pre)
        tparams = [t for t in sig.tparams if t not in pre]
        vars = set(tparams)
        what = f"field '{{}}' of {sig.name}" if sig.kind == "record" else f"parameter '{{}}' of '{sig.name}'"
        s: dict = {}
        if explicit is not None:
            if len(explicit) != len(tparams):
                n = len(tparams)
                return Fail("E0303", span, f"'{sig.name}' takes {n} type argument{'s' if n != 1 else ''}, not {len(explicit)}", False)
            s = dict(zip(tparams, explicit))
            vars = set()
        # Bind arguments to parameters.
        slots: list = [None] * len(params)
        extra: list = []
        rest = next((i for i, p in enumerate(params) if p.varargs), None)
        pos = 0
        for a in args:
            if a.name is None:
                if not sig.positional:
                    return Fail("E0511", a.span, f"name the fields: '{sig.name}({params[0].name if params else 'x'}: ...)'", False)
                if pos < len(params) and not params[pos].varargs:
                    slots[pos] = a
                    pos += 1
                elif rest is not None:
                    extra.append(a)
                else:
                    n = len(params)
                    return Fail("E0511", a.span, f"too many arguments: '{sig.name}' takes {n}", False)
            else:
                n = a.name.name
                i = next((i for i, p in enumerate(params) if p.name == n and not p.varargs), None)
                if i is None:
                    kind = "field" if sig.kind == "record" else "parameter"
                    return Fail("E0511", a.name.span, f"'{sig.name}' has no {kind} '{n}'", False)
                if slots[i] is not None:
                    return Fail("E0511", a.span, f"'{n}' is passed twice", False)
                slots[i] = a
        missing = [p.name for i, p in enumerate(params) if slots[i] is None and not p.has_default and not p.varargs]
        if missing:
            names = ", ".join(f"'{n}'" for n in missing)
            return Fail("E0511", span, f"missing argument{'s' if len(missing) > 1 else ''} {names} for '{sig.name}'", False)
        # Types.
        tier = 0
        flex = []
        for p, a in zip(params, slots):
            if a is None:
                continue
            if a.flex is not None:
                flex.append((p, a))
                continue
            s2 = dict(s)
            if unify(p.type, a.type, s2, vars):
                s = s2
                continue
            s2 = dict(s)
            if self.inject(p.type, a.type, s2, vars):
                s, tier = s2, max(tier, 1)
                continue
            want = subst(p.type, s)
            if not params_in(want) & vars and self.assignable(a.type, want):
                tier = max(tier, 1)
                continue
            return Fail("E0300", a.span, f"{what.format(p.name)}: expected {show(want)}, found {show(a.type)}", True)
        if expected is not None and not has_error(expected) and params_in(result) & (vars - s.keys()):
            for target in [expected] + self.env.variants(expected):
                s2 = dict(s)
                if unify(result, target, s2, vars):
                    s = s2
                    break
        for p, a in flex:
            want = subst(p.type, s)
            t = self.flex_fit(a, want, s, vars)
            if t is None:
                return Fail("E0300", a.span, f"{what.format(p.name)}: expected {show(want)}, found {self.arg_desc(a)}", True)
            tier = max(tier, t)
        # Bounds, which may also infer parameters used only in bounds (`T` of `I: Iterator[T]`).
        for _ in range(2):
            for tp in tparams:
                if tp not in s:
                    continue
                for b in self.env.bounds(tp):
                    if not isinstance(b, TraitT):
                        continue
                    if not self.satisfies(s[tp], subst(b, s), s, vars):
                        msg = f"{show(s[tp])} doesn't satisfy {show(subst(b, s))}, required by '{sig.name}'"
                        return Fail("E0700", span, msg + (f": {self.why}" if self.why else ""), True)
        used = set()
        for p in params:
            params_in(p.type, used)
        params_in(result, used)
        errors = any(a.type is not None and has_error(a.type) for a in slots + extra if a is not None)
        for tp in tparams:
            if tp in vars and tp not in s:
                if errors:  # an argument already failed: don't pile on
                    s[tp] = ERROR
                    continue
                return Fail(
                    "E0302", span,
                    f"can't infer '{tp.name}' for '{sig.name}'; give it explicitly: "
                    f"'{sig.name}[{', '.join(t.name for t in tparams)}](...)'",
                    True,
                )  # fmt: skip
        return Match(sig, params, result, s, tier, slots, extra, pre)

    def inject(self, pattern, actual, s: dict, vars) -> bool:
        """Matches a variant against a union parameter (`Some[T]` against `Option[T]`)."""
        for v in self.env.variants(pattern):
            s2 = dict(s)
            if unify(v, actual, s2, vars):
                s.update(s2)
                return True
        return False

    def flex_fit(self, a: CArg, want, s: dict, vars):
        """Whether a flexible argument fits ``want``: 0 exact, 2 non-default literal type."""
        k = a.flex
        if has_error(want):
            return 0
        if isinstance(want, Param) and want.sym in vars:
            if k == INT_LIT:
                s[want.sym] = INTEGER
                return 0
            if k == REAL_LIT:
                s[want.sym] = REAL
                return 0
            return None
        b = self.base(want)
        if k == INT_LIT:
            if not is_integer(b):
                return None
            lo, hi = _INT_RANGE[b.name]
            return (0 if want == INTEGER else 2) if lo <= _literal_value(a.expr) <= hi else None
        if k == REAL_LIT:
            return (0 if want == REAL else 2) if is_real(b) else None
        if k == EMPTY_LIST:
            return 0 if isinstance(want, Builtin) and want.name == "List" else None
        if k == EMPTY_BRACES:
            return 0 if self.env.is_map(want) or self.env.is_set(want) else None
        if isinstance(k, Overloads) and isinstance(want, FuncT):
            fits = []
            for sym in k.syms:
                g = self.env.sig(sym)
                s2 = dict(s)
                if not g.tparams and unify(want, g.functype(), s2, vars):
                    fits.append(s2)
            if len(fits) == 1:
                s.update(fits[0])
                return 0
        return None

    def finish(self, e, name: str, m: Match, receiver, ref_span) -> None:
        sig = m.sig
        if ref_span is not None and sig.sym is not None and sig.kind == "routine":
            self.index.narrow(self.file, ref_span, [sig.sym])
        for p, a in zip(m.params, m.slots):
            if a is None:
                continue
            want = subst(p.type, m.s)
            if a.flex is not None:
                self.check(a.expr, want)
            if a.name is not None and p.sym is not None:
                self.index.add(self.file, a.name.span, [p.sym])
            if p.is_var:
                if a.receiver:
                    rest = ", ".join(self.text(x.span) for x in e.args)
                    new = f"{name}(var {self.text(a.span)}{', ' + rest if rest else ''})"
                    self.error(
                        "E0520", e.span,
                        f"'{name}' changes its first argument, so it can't be called with a dot; "
                        f"write '{new}'",
                        [Fix(f"rewrite as '{new}'", (Edit(e.span, new),))],
                    )  # fmt: skip
                elif not a.is_var:
                    self.error(
                        "E0521", a.expr.span,
                        f"'{p.name}' is a var parameter of '{name}': mark the argument 'var'",
                        [Fix("add 'var'", (Edit(_point(a.expr.span), "var "),))],
                    )  # fmt: skip
                else:
                    why = self.immutable(a.expr)
                    if why:
                        self.error("E0341", a.expr.span, f"can't pass {why} as 'var'")
            elif a.is_var:
                self.error("E0522", a.span, f"'{p.name}' is not a var parameter of '{name}'; remove 'var'")
        for a in m.extra:
            t = self.value(a.expr) if a.flex is not None else a.type
            if t is not None:
                self.c_compatible(t, a.expr.span)
            if a.is_var:
                self.error("E0522", a.span, f"variadic arguments of '{name}' can't be 'var'")
        self.var_overlap([a for p, a in zip(m.params, m.slots) if a is not None and p.is_var and a.is_var])
        if sig.kind != "routine":
            return
        decl = sig.params
        for i, a in enumerate(m.slots):
            if a is None or a.name is not None or has_error(decl[i].type):
                continue
            p = decl[i]
            q = next((q for q in decl[:i] if q.group != p.group and not q.varargs and q.type == p.type), None)
            if q is not None:
                self.error(
                    "E0530", a.span,
                    f"'{p.name}' has the same type as '{q.name}'; pass it by name: '{p.name}: ...'",
                    [Fix(f"name the argument '{p.name}'", (Edit(_point(a.span), f"{p.name}: "),))],
                )  # fmt: skip

    # ------------------------------------------------------------- mutability

    def var_overlap(self, args: list) -> None:
        """`var` arguments of one call must be separate places (SPEC §6.3): not the same
        variable, field or element, and not one inside another."""
        seen = []
        for a in args:
            path = self.place_path(a.expr)
            if path is None:
                continue
            for other, opath in seen:
                n = min(len(path), len(opath))
                if path[:n] == opath[:n]:
                    self.error(
                        "E0523", a.expr.span,
                        f"'{self.text(a.expr.span)}' overlaps '{self.text(other.span)}', which is "
                        "also passed as 'var'; pass separate places",
                    )  # fmt: skip
                    break
            seen.append((a.expr, path))

    def place_path(self, e) -> tuple | None:
        """A place as a path from where it is stored, or None if unknown: `p.a[i]` is
        (p, "a", "[]"). The index is left out, since `xs[i]` and `xs[j]` may be the same
        element. A place reached through a ref starts at the pointee's type, since any two
        refs to a `T` may point to the same one."""
        if isinstance(e, A.Paren):
            return self.place_path(e.inner)
        if isinstance(e, A.Name):
            r = self.ref_at(e.span)
            return (r.targets[0],) if r is not None and len(r.targets) == 1 else None
        if isinstance(e, A.Deref):
            return self.pointee(e.target)
        if isinstance(e, (A.FieldAccess, A.Index)):
            if isinstance(e, A.FieldAccess) and self.static_owner(e.target) is not None:
                return None
            base = self.pointee(e.target) or self.place_path(e.target)
            step = e.name.name if isinstance(e, A.FieldAccess) else "[]"
            return None if base is None else (*base, step)
        return None

    def pointee(self, e) -> tuple | None:
        """The start of a path through ``e`` if it is a ref or pointer, else None."""
        t = self.types.get(id(e))
        if isinstance(t, RefT):
            return (("ref", t.target),)
        if isinstance(t, Builtin) and t.name == "Ptr":
            return (("ref", t.args[0]),)
        return None

    def immutable(self, e) -> str | None:
        """None if ``e`` is a place that may be changed; otherwise what it is."""
        if isinstance(e, A.Paren):
            return self.immutable(e.inner)
        if isinstance(e, A.Name):
            r = self.ref_at(e.span)
            if r is None or len(r.targets) != 1:
                return None  # already reported
            s = r.targets[0]
            if s.kind is Kind.VAR:
                return None
            if s.kind is Kind.PARAM:
                return None if getattr(s.node, "is_var", False) else f"parameter '{s.name}' (it isn't 'var')"
            if s.kind is Kind.LET:
                return f"let binding '{s.name}' (declare it with 'var')"
            if s.kind is Kind.LOOP_VAR:
                return f"loop variable '{s.name}'"
            if s.kind is Kind.BINDING:
                return f"case binding '{s.name}'"
            return f"{s.describe()}"
        if isinstance(e, A.FieldAccess):
            if self.static_owner(e.target) is not None:
                return f"'{self.text(e.span)}'"
            if isinstance(self.types.get(id(e.target)), RefT):
                return None  # through a ref
            return self.immutable(e.target)
        if isinstance(e, A.Index):
            if isinstance(self.types.get(id(e.target)), RefT):
                return None
            return self.immutable(e.target)
        if isinstance(e, A.Deref):
            return None
        return f"'{self.text(e.span)}', which isn't a variable"

    # ------------------------------------------------------------------ traits

    def satisfies(self, t, trait: TraitT, s: dict | None = None, vars=frozenset(), depth: int = 0) -> bool:
        """Whether ``t`` satisfies ``trait``. Unbound parameters (``vars``) in the trait's
        arguments are inferred into ``s``."""
        s = {} if s is None else s
        self.why = ""
        if has_error(t):
            return True
        sym = trait.sym
        if sym.kind is Kind.BUILTIN_TRAIT:
            if self.builtin_trait(t, sym.name):
                return True
            self.why = f"{show(t)} isn't {sym.name}"
            return False
        if isinstance(t, Param):
            for b in self.env.bounds(t.sym):
                if isinstance(b, TraitT) and b.sym is sym:
                    s2 = dict(s)
                    if all(unify(p, a, s2, vars) for p, a in zip(trait.args, b.args)):
                        s.update(s2)
                        return True
            self.why = f"'{t.sym.name}' has no bound {show(trait)}"
            return False
        if sym is self.env.sys("Printable") and self.printable(t):
            return True
        if depth > 16:
            return False
        key = None
        if not params_in(trait) & set(vars):
            key = (self.unit.name, t, trait)
            if key in self.env.sat_cache:
                return self.env.sat_cache[key]
            self.env.sat_cache[key] = True  # assume while checking (recursive types)
        ok = self.structural(t, trait, s, vars, depth)
        if key is not None:
            self.env.sat_cache[key] = ok
        return ok

    def structural(self, t, trait: TraitT, s: dict, vars, depth: int) -> bool:
        d = self.env.decl(trait.sym)
        sub = dict(zip(d.params, trait.args))
        sub[THIS] = t
        for f in d.fields:
            need = subst(f.type, sub)
            found = self.find_field(t, f.name)
            have = subst(found[0].type, found[2]) if found is not None else None
            s2 = dict(s)
            if have is None or not unify(need, have, s2, vars):
                self.why = f"it has no field '{f.name}: {show(need)}'"
                return False
            s.update(s2)
        for m in d.routines:
            if not self.provides(t, m, sub, s, vars, depth):
                self.why = f"no visible function '{m.name}' takes {show(t)} as required"
                return False
        return True

    def provides(self, t, m: Symbol, sub: dict, s: dict, vars, depth: int) -> bool:
        return self.provider(t, m, sub, s, vars, depth) is not None

    def provider(self, t, m: Symbol, sub: dict, s: dict, vars=frozenset(), depth: int = 0):
        """The visible routine that provides trait routine ``m`` for type ``t``, with the
        bindings of its own type parameters, or None."""
        msig = self.env.sig(m)
        need = [subst(p.type, sub) for p in msig.params]
        need_result = subst(msig.result, sub)
        var_this = bool(msig.params) and msig.params[0].is_var
        for f in self.lookup_routines(m.name):
            if isinstance(f.node, A.Signature):
                continue
            fs = self.env.sig(f)
            if (
                len(fs.params) != len(need) or not fs.params or fs.params[0].is_var != var_this
                or fs.is_function != msig.is_function
            ):  # fmt: skip
                continue
            fv = set(fs.tparams)
            sf: dict = {}
            if not unify(fs.params[0].type, t, sf, fv):
                continue
            ok = True
            for tp in fs.tparams:
                for b in self.env.bounds(tp):
                    if ok and isinstance(b, TraitT) and tp in sf:
                        ok = self.satisfies(sf[tp], subst(b, sf), sf, fv, depth + 1)
            s2 = dict(s)
            for fp, np in zip(fs.params[1:], need[1:]):
                ok = ok and self.unify2(np, fp.type, s2, vars, sf, fv)
            ok = ok and self.unify2(need_result, fs.result, s2, vars, sf, fv)
            if ok:
                s.update(s2)
                return f, sf
        return None

    @staticmethod
    def unify2(need, have, s: dict, vars, sf: dict, fv) -> bool:
        """Unifies a trait's requirement with a candidate's declared type, when both
        sides may have unbound parameters."""
        s2 = dict(s)
        if unify(need, subst(have, sf), s2, vars):
            s.update(s2)
            return True
        sf2 = dict(sf)
        if unify(have, subst(need, s), sf2, fv):
            sf.update(sf2)
            return True
        return False

    def printable(self, t) -> bool:
        """Built in: primitives, enums, and subranges are Printable (SPEC §4.10)."""
        if isinstance(t, Prim):
            return t.name not in ("CString", "Pointer")
        return self.env.kind_of(t) in ("enum", "subrange")

    def builtin_trait(self, t, name: str) -> bool:
        if has_error(t):
            return True
        if isinstance(t, Param):
            implied = {"Equatable": ("Equatable", "Ordered", "Hashable")}.get(name, (name,))
            return any(
                isinstance(b, TraitT) and b.sym.kind is Kind.BUILTIN_TRAIT and b.sym.name in implied
                for b in self.env.bounds(t.sym)
            )
        key = (t, name)
        if key in self._in_builtin:
            return True  # a recursive type: assume, the other parts decide
        self._in_builtin.add(key)
        try:
            return self._builtin_trait(t, name)
        finally:
            self._in_builtin.discard(key)

    def _builtin_trait(self, t, name: str) -> bool:
        kind = self.env.kind_of(t)
        parts = []
        if kind == "record":
            d = self.env.decl(t.sym)
            s = dict(zip(d.params, t.args))
            parts = [subst(f.type, s) for f in d.fields]
        elif kind == "union":
            parts = self.env.variants(t)
        elif isinstance(t, Builtin):
            parts = [a for a in t.args if not isinstance(a, ConstArg)]
        if name == "Equatable":
            if isinstance(t, FuncT):
                return False
            if isinstance(t, RefT):
                return True
            return all(self.builtin_trait(p, name) for p in parts)
        if name == "Ordered":
            return is_numeric(t) or t in (CHAR, STRING) or kind in ("enum", "subrange")
        if name == "Hashable":
            if isinstance(t, Prim):
                return is_integer(t) or t in (CHAR, STRING, BOOLEAN)
            if kind in ("enum", "subrange"):
                return True
            return kind == "record" and all(self.builtin_trait(p, name) for p in parts)
        if name == "Sendable":
            if isinstance(t, RefT) or (isinstance(t, Builtin) and t.name in ("Weak", "Ptr")):
                return False
            if isinstance(t, Prim) and t.name == "Pointer":
                return False
            return all(self.builtin_trait(p, name) for p in parts)
        return False

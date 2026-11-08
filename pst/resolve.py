"""Unit loading and name resolution: checker stage 1 (SPEC §3, §5.2, §15.2).

Stage 2, the type checker, lives in ``typecheck.py`` and runs after every unit is resolved.

``check(path)`` parses a file and every unit it uses, collects the top-level declarations
of each unit (the declaration pass), then resolves every name and builds the symbol index.

What is resolved here: plain names, type names, qualified names (`Strings.split`,
`Color.Red`), type parameters, `this`, and `result` / `old` in `ensures`. Names that need
types are left for the type checker without an error: field access and dot calls
(`p.x`, `xs.len()`), named arguments, and bare names in `case` labels.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field

from . import ast as A
from .diagnostics import Diagnostic, Edit, Fix, Severity
from .parser import ParseResult, parse
from .source import Span, normalize
from .symbols import LOCALS, LOWER_CAMEL, ROUTINES, UPPER_CAMEL, Index, Kind, Symbol

STD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "units")

_BUILTIN_TYPES = {
    "System": [
        "Int8", "Int16", "Int32", "Int64", "UInt8", "UInt16", "UInt32", "UInt64",
        "Real32", "Real64", "Boolean", "Char", "String", "List", "Map", "Set", "Array",
        "Weak",
    ],
    "C": ["CString", "Ptr", "Pointer"],
}  # fmt: skip
_BUILTIN_TRAITS = {"System": ["Equatable", "Ordered", "Hashable", "Sendable"]}

_TYPE_EXPRS = (A.TypeName, A.RefType, A.FuncType)


def _is_discard(name: str) -> bool:
    return name.startswith("_")


# ------------------------------------------------------------------------ units


@dataclass(eq=False)
class Unit:
    name: str
    path: str
    std: bool
    parsed: ParseResult
    symbol: Symbol
    top: dict[str, list[Symbol]] = field(default_factory=dict)
    used: list["Unit"] = field(default_factory=list)  # explicit `uses`, then System
    text: str = ""  # normalized source, for fixes that rewrite code

    @property
    def tree(self) -> A.File:
        return self.parsed.file

    @property
    def file(self) -> str:
        return self.path

    def exports(self, name: str) -> list[Symbol]:
        return [s for s in self.top.get(name, []) if s.exported]


@dataclass
class CheckResult:
    main: Unit | None
    units: dict[str, Unit]
    index: Index
    diagnostics: list[Diagnostic]

    @property
    def has_errors(self) -> bool:
        return any(d.severity is Severity.ERROR for d in self.diagnostics)


def _read(path: str) -> tuple[str | None, str | None]:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return None, str(e.strerror or e)
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as e:
        return None, f"invalid UTF-8 byte 0x{data[e.start]:02X}"


def _point(span: Span) -> Span:
    return Span(span.start, span.start, span.line, span.col, span.line, span.col)


class Checker:
    def __init__(self, std_dir: str = STD_DIR) -> None:
        self.std_dir = std_dir
        self.units: dict[str, Unit] = {}  # by unit name; programs are not entered
        self.missing: set[str] = set()
        self.programs: set[str] = set()  # found, but not units
        self.index = Index()
        self.diagnostics: list[Diagnostic] = []

    def error(self, file: str, code: str, span: Span, message: str, fixes=()) -> None:
        d = Diagnostic(file, span, Severity.ERROR, code, message, tuple(fixes))
        if d not in self.diagnostics:
            self.diagnostics.append(d)

    # ---------------------------------------------------------------- loading

    def run(self, path: str) -> CheckResult:
        std = os.path.dirname(os.path.abspath(path)) == os.path.abspath(self.std_dir)
        main = self.load(path, std=std)
        if main is not None:
            self.check_cycles(main)
            others = [u for u in self.units.values() if u is not main]
            for unit in [main] + others:
                Resolver(self, unit).run()
            from .typecheck import Env

            env = Env(self, [main] + others)
            for unit in [main] + others:
                env.checker(unit).run()
        self.diagnostics.sort(key=lambda d: (d.file != (main.file if main else ""), d.file, d.span.start))
        return CheckResult(main, self.units, self.index, self.diagnostics)

    def load(self, path: str, std: bool, expected: str | None = None) -> Unit | None:
        text, err = _read(path)
        if text is None:
            span = Span(0, 0, 1, 1, 1, 1)
            self.diagnostics.append(Diagnostic(path, span, Severity.ERROR, "E0000", err))
            return None
        parsed = parse(text, path)
        self.diagnostics.extend(parsed.diagnostics)
        tree = parsed.file
        stem = os.path.splitext(os.path.basename(path))[0]
        name = tree.name.name or stem
        symbol = Symbol(Kind.UNIT, name, tree.name.span if tree.name.name else None, path, name, True, tree)
        unit = Unit(name, path, std, parsed, symbol, text=normalize(text))
        if tree.name.name and tree.name.name != stem:
            fixes = []
            if stem[:1].isalpha() and stem.replace("_", "a").isalnum() and stem.isascii():
                fixes.append(Fix(f"rename to '{stem}'", (Edit(tree.name.span, stem),)))
            self.error(
                path, "E0105", tree.name.span,
                f"{tree.kind} '{tree.name.name}' must be in a file named '{tree.name.name}.pst', "
                f"not '{os.path.basename(path)}'",
                fixes,
            )  # fmt: skip
        if tree.name.name:
            self.index.add(path, tree.name.span, [symbol], is_def=True)
        if tree.kind == "unit":
            self.units.setdefault(name, unit)
            if expected is not None:
                self.units.setdefault(expected, unit)
        Declarations(self, unit).collect()

        seen: set[str] = set()
        for ident in tree.uses:
            if ident.name in seen:
                continue
            seen.add(ident.name)
            used = self.find_unit(ident.name, unit, ident.span)
            if used is not None:
                unit.used.append(used)
                self.index.add(path, ident.span, [used.symbol])
        if name != "System" or not std:
            system = self.units.get("System") or self.find_unit("System", unit, None)
            if system is not None and system not in unit.used:
                unit.used.append(system)
        return unit

    def find_unit(self, name: str, user: Unit, span: Span | None) -> Unit | None:
        if name == user.name:
            if span is not None:
                self.error(user.file, "E0107", span, f"'{name}' can't use itself")
            return None
        if name in self.units:
            return self.units[name]
        if name in self.programs:
            if span is not None:
                self.error(user.file, "E0102", span, f"'{name}' is a program, not a unit")
            return None
        if name in self.missing:
            if span is not None:
                self.error(user.file, "E0102", span, f"unit '{name}' not found")
            return None
        candidates = [(os.path.join(self.std_dir, name + ".pst"), True)]
        if not user.std:
            candidates.append((os.path.join(os.path.dirname(user.path), name + ".pst"), False))
        for path, std in candidates:
            if os.path.isfile(path):
                unit = self.load(path, std, expected=name)
                if unit is None:
                    return None
                if unit.tree.kind != "unit":
                    self.programs.add(name)
                    if span is not None:
                        self.error(user.file, "E0102", span, f"'{name}' is a program, not a unit")
                    return None
                return unit
        self.missing.add(name)
        if span is not None:
            where = os.path.dirname(user.path) or "."
            self.error(
                user.file, "E0102", span,
                f"unit '{name}' not found: no '{name}.pst' in {where} or the standard units",
            )  # fmt: skip
        return None

    def check_cycles(self, main: Unit) -> None:
        state: dict[int, int] = {}  # id(unit) -> 1 visiting, 2 done
        stack: list[Unit] = []

        def visit(u: Unit) -> None:
            state[id(u)] = 1
            stack.append(u)
            for ident in u.tree.uses:
                v = self.units.get(ident.name)
                if v is None:
                    continue
                if state.get(id(v)) == 1:
                    path = [w.name for w in stack[stack.index(v):]] + [v.name]
                    self.error(
                        u.file, "E0107", ident.span, f"cyclic uses: {' -> '.join(path)}"
                    )
                elif id(v) not in state:
                    visit(v)
            stack.pop()
            state[id(u)] = 2

        visit(main)


def check(path: str, std_dir: str = STD_DIR) -> CheckResult:
    return Checker(std_dir).run(path)


# ------------------------------------------------------------ declaration pass


class Declarations:
    """Collects the top-level names of one unit (SPEC §15.2 step 2)."""

    def __init__(self, checker: Checker, unit: Unit) -> None:
        self.c = checker
        self.unit = unit

    def collect(self) -> None:
        u = self.unit
        for name in _BUILTIN_TYPES.get(u.name, []) if u.std else []:
            u.top[name] = [Symbol(Kind.BUILTIN_TYPE, name, None, None, u.name, True)]
        for name in _BUILTIN_TRAITS.get(u.name, []) if u.std else []:
            u.top[name] = [Symbol(Kind.BUILTIN_TRAIT, name, None, None, u.name, True)]
        for item in u.tree.items:
            if isinstance(item, A.TypeSection):
                for d in item.decls:
                    self.type_decl(d)
            elif isinstance(item, A.ConstSection):
                for d in item.decls:
                    self.add(self.new(Kind.CONST, d.name, d.exported, d))
            elif isinstance(item, A.Routine):
                kind = Kind.FUNCTION if item.sig.is_function else Kind.PROCEDURE
                self.add(self.new(kind, item.sig.name, item.exported, item))
            elif isinstance(item, A.ExternBlock):
                if item.library == "pst" and not u.std:
                    self.c.error(
                        u.file, "E0106", item.span,
                        "'extern \"pst\"' is reserved for the standard units",
                    )  # fmt: skip
                for d in item.decls:
                    kind = Kind.FUNCTION if d.sig.is_function else Kind.PROCEDURE
                    sym = self.new(kind, d.sig.name, d.exported, d)
                    sym.is_extern = True
                    self.add(sym)

    def new(self, kind: Kind, ident: A.Ident, exported: bool, node) -> Symbol:
        sym = Symbol(kind, ident.name, ident.span, self.unit.file, self.unit.name, exported, node)
        self.c.index.add(self.unit.file, ident.span, [sym], is_def=True)
        return sym

    def add(self, sym: Symbol) -> None:
        existing = self.unit.top.setdefault(sym.name, [])
        clash = next((s for s in existing if not (s.is_routine and sym.is_routine)), None)
        if clash is not None:
            self.c.error(
                self.unit.file, "E0108", sym.span,
                f"'{sym.name}' is already declared ({clash.describe()}, {clash.location()})",
            )  # fmt: skip
            return
        existing.append(sym)

    def member(self, owner: Symbol, kind: Kind, ident: A.Ident, exported: bool, node) -> None:
        sym = self.new(kind, ident, exported, node)
        if ident.name in owner.members:
            prev = owner.members[ident.name]
            self.c.error(
                self.unit.file, "E0108", ident.span,
                f"'{ident.name}' is already declared in '{owner.name}' ({prev.location()})",
            )  # fmt: skip
            return
        owner.members[ident.name] = sym

    def type_decl(self, d: A.TypeDecl) -> None:
        body = d.body
        kind = Kind.TRAIT if isinstance(body, A.TraitType) else Kind.TYPE
        sym = self.new(kind, d.name, d.exported, d)
        if isinstance(body, A.EnumType):
            for m in body.members:
                self.member(sym, Kind.ENUM_MEMBER, m, d.exported, m)
        elif isinstance(body, (A.RecordType, A.TraitType)):
            for f in body.fields:
                self.member(sym, Kind.FIELD, f.name, d.exported and (f.exported or kind is Kind.TRAIT), f)
            if isinstance(body, A.TraitType):
                for sig in body.routines:
                    k = Kind.FUNCTION if sig.is_function else Kind.PROCEDURE
                    self.member(sym, k, sig.name, d.exported, sig)
        self.add(sym)


# ----------------------------------------------------------------- resolution


class _Ctx:
    """One routine body, test block, or the program's main body."""

    def __init__(self, is_main: bool = False) -> None:
        self.is_main = is_main
        self.scopes: list[dict[str, Symbol]] = [{}]
        self.locals: list[Symbol] = []
        self.in_ensures = False


@dataclass
class _Lookup:
    symbols: list[Symbol]
    ambiguous: list[Unit] = field(default_factory=list)  # units that export clashing names


class Resolver:
    def __init__(self, checker: Checker, unit: Unit) -> None:
        self.c = checker
        self.unit = unit
        self.file = unit.file
        self.ctx: _Ctx | None = None
        self.type_scopes: list[dict[str, Symbol]] = []  # type params and bound members
        self.in_trait = False
        self.main_locals: dict[str, Symbol] = {}
        self.unit_names = {u.name: u for u in unit.used}

    def error(self, code: str, span: Span, message: str, fixes=()) -> None:
        self.c.error(self.file, code, span, message, fixes)

    def ref(self, span: Span, targets, is_def: bool = False) -> None:
        self.c.index.add(self.file, span, targets, is_def)

    def run(self) -> None:
        items = self.unit.tree.items
        # Main first, so E0103 knows every top-level local.
        self.ctx = _Ctx(is_main=True)
        self.stmts([i for i in items if not isinstance(i, (A.TypeSection, A.ConstSection,
                                                          A.Routine, A.ExternBlock, A.TestBlock))])
        self.finish_ctx()
        self.main_locals = {s.name: s for s in self.ctx.locals}
        self.ctx = None
        for item in items:
            if isinstance(item, A.TypeSection):
                for d in item.decls:
                    self.type_decl(d)
            elif isinstance(item, A.ConstSection):
                for d in item.decls:
                    self.expr(d.value)
            elif isinstance(item, A.Routine):
                self.routine(item)
            elif isinstance(item, A.ExternBlock):
                for d in item.decls:
                    self.signature(d.sig)
            elif isinstance(item, A.TestBlock):
                self.ctx = _Ctx()
                self.stmts(item.body.stmts)
                self.finish_ctx()
                self.ctx = None
        self.check_casing()

    # ----------------------------------------------------------------- lookup

    def lookup(self, name: str) -> _Lookup:
        if self.ctx is not None:
            for scope in reversed(self.ctx.scopes):
                if name in scope:
                    return _Lookup([scope[name]])
        for scope in reversed(self.type_scopes):
            if name in scope:
                return _Lookup([scope[name]])
        own = self.unit.top.get(name, [])
        if own and not own[0].is_routine:
            return _Lookup(own)
        if not own and name in self.unit_names:
            return _Lookup([self.unit_names[name].symbol])
        routines = list(own)
        others: list[tuple[Symbol, Unit]] = []
        for u in self.unit.used:
            for s in u.exports(name):
                if s.is_routine:
                    if s not in routines:
                        routines.append(s)
                elif all(s is not o for o, _ in others):
                    others.append((s, u))
        if own or not others:
            return _Lookup(routines)
        if len(others) == 1 and not routines:
            return _Lookup([others[0][0]])
        units = [u for _, u in others]
        units += [u for u in self.unit.used if any(s in routines for s in u.exports(name))]
        return _Lookup([], units)

    def visible_names(self) -> set[str]:
        names: set[str] = set(self.unit.top) | set(self.unit_names)
        if self.ctx is not None:
            for scope in self.ctx.scopes:
                names |= scope.keys()
        for scope in self.type_scopes:
            names |= scope.keys()
        for u in self.unit.used:
            names |= {n for n, syms in u.top.items() if any(s.exported for s in syms)}
        return names

    def unknown(self, name: str, span: Span, what: str = "name") -> None:
        if self.ctx is not None and not self.ctx.is_main and name in self.main_locals:
            self.error(
                "E0103", span,
                f"'{name}' is a top-level variable of the program, which functions can't see; "
                "pass it as a parameter ('var' to change it)",
            )  # fmt: skip
            return
        if self.ctx is None and name in self.main_locals:
            self.error("E0103", span, f"'{name}' is a top-level variable and isn't visible here")
            return
        matches = difflib.get_close_matches(name, sorted(self.visible_names()), n=3, cutoff=0.75)
        if matches:
            # Probably a typo for this: don't also call the intended variable unused.
            for sym in self.lookup(matches[0]).symbols:
                sym.reads += 1
        fixes = [Fix(f"replace with '{m}'", (Edit(span, m),)) for m in matches]
        self.error("E0100", span, f"unknown {what} '{name}'", fixes)

    def resolve_name(self, name: str, span: Span, read: bool = True) -> list[Symbol]:
        found = self.lookup(name)
        if found.ambiguous:
            fixes = [Fix(f"qualify as '{u.name}.{name}'", (Edit(span, f"{u.name}.{name}"),)) for u in found.ambiguous]
            units = ", ".join(u.name for u in found.ambiguous)
            self.error("E0110", span, f"'{name}' is ambiguous: declared in units {units}; qualify it", fixes)
            return []
        if not found.symbols:
            self.unknown(name, span)
            return []
        if read and len(found.symbols) == 1:
            found.symbols[0].reads += 1
        self.ref(span, found.symbols)
        return found.symbols

    def unit_member(self, unit_sym: Symbol, ident: A.Ident) -> list[Symbol]:
        unit = self.unit_names.get(unit_sym.name)
        if unit is None:
            return []
        syms = unit.exports(ident.name)
        if not syms:
            if ident.name in unit.top:
                self.error("E0109", ident.span, f"'{ident.name}' is not exported by unit {unit.name}")
            else:
                self.error("E0100", ident.span, f"unit {unit.name} has no '{ident.name}'")
            return []
        self.ref(ident.span, syms)
        return syms

    def enum_member(self, enum: Symbol, ident: A.Ident) -> None:
        member = enum.members.get(ident.name)
        if member is None:
            names = sorted(enum.members)
            fixes = [
                Fix(f"replace with '{m}'", (Edit(ident.span, m),))
                for m in difflib.get_close_matches(ident.name, names, n=3, cutoff=0.6)
            ]
            self.error("E0100", ident.span, f"enum '{enum.name}' has no member '{ident.name}'", fixes)
            return
        if enum.unit != self.unit.name and not member.exported:
            self.error("E0109", ident.span, f"'{enum.name}' is not exported by unit {enum.unit}")
        self.ref(ident.span, [member])

    @staticmethod
    def _is_enum(sym: Symbol) -> bool:
        return sym.kind is Kind.TYPE and isinstance(getattr(sym.node, "body", None), A.EnumType)

    def static_target(self, e) -> Symbol | None:
        """Resolves ``e`` as the left side of a dot. Returns the unit or enum it names, if
        it names one; otherwise resolves it as an ordinary expression and returns None."""
        if isinstance(e, A.Name) and e.name not in ("this", "This"):
            found = self.lookup(e.name)
            if len(found.symbols) == 1 and (
                found.symbols[0].kind is Kind.UNIT or self._is_enum(found.symbols[0])
            ):
                self.ref(e.span, found.symbols)
                return found.symbols[0]
            self.expr(e)
            return None
        if isinstance(e, A.FieldAccess):
            owner = self.static_target(e.target)
            if owner is None:
                return None
            if owner.kind is Kind.UNIT:
                syms = self.unit_member(owner, e.name)
                if len(syms) == 1 and (syms[0].kind is Kind.UNIT or self._is_enum(syms[0])):
                    return syms[0]
                return None
            self.enum_member(owner, e.name)
            return None
        self.expr(e)
        return None

    # ------------------------------------------------------------------ scopes

    def push(self) -> None:
        self.ctx.scopes.append({})

    def pop(self) -> None:
        self.ctx.scopes.pop()

    def define(self, kind: Kind, ident: A.Ident, node, unused_fix: Span | None = None) -> Symbol | None:
        if _is_discard(ident.name):
            return None
        sym = Symbol(kind, ident.name, ident.span, self.file, self.unit.name, node=node, unused_fix=unused_fix)
        for scope in self.ctx.scopes:
            prev = scope.get(ident.name)
            if prev is not None and prev.kind is not Kind.RESULT:
                self.error(
                    "E0205", ident.span,
                    f"'{ident.name}' is already declared in this "
                    f"{'program body' if self.ctx.is_main else 'routine'} "
                    f"({prev.kind.value}, line {prev.span.line}); choose another name",
                )  # fmt: skip
                break
        self.ctx.scopes[-1][ident.name] = sym
        self.ctx.locals.append(sym)
        self.ref(ident.span, [sym], is_def=True)
        return sym

    def finish_ctx(self) -> None:
        for sym in self.ctx.locals:
            if sym.reads or sym.unused_fix is None:
                continue
            fix = Fix(f"discard: replace with '_'", (Edit(sym.unused_fix, "_"),))
            self.error("E0206", sym.span, f"{sym.kind.value} '{sym.name}' is never used", [fix])

    def push_type_params(self, params: list[A.TypeParam]) -> None:
        scope: dict[str, Symbol] = {}
        self.type_scopes.append(scope)
        for p in params:
            sym = Symbol(Kind.TYPE_PARAM, p.name.name, p.name.span, self.file, self.unit.name, node=p)
            if p.name.name in scope:
                self.error("E0108", p.name.span, f"type parameter '{p.name.name}' is declared twice")
            else:
                scope[p.name.name] = sym
            self.ref(p.name.span, [sym], is_def=True)
        # Bound members (SPEC §8.2): routines required by a bound resolve to the trait.
        members: dict[str, Symbol] = {}
        for p in params:
            for b in p.bounds:
                for sym in self.type_expr(b):
                    if sym.kind is Kind.TRAIT:
                        for m in sym.members.values():
                            if m.is_routine:
                                members.setdefault(m.name, m)
        if members:
            scope.update({k: v for k, v in members.items() if k not in scope})

    # ------------------------------------------------------------------- types

    def type_expr(self, t) -> list[Symbol]:
        if isinstance(t, A.TypeName):
            return self.type_name(t)
        if isinstance(t, A.RefType):
            self.type_expr(t.target)
        elif isinstance(t, A.FuncType):
            for p in t.params:
                self.type_expr(p)
            if t.result is not None:
                self.type_expr(t.result)
        return []

    def type_name(self, t: A.TypeName) -> list[Symbol]:
        for a in t.args:
            if isinstance(a, _TYPE_EXPRS):
                self.type_expr(a)
            else:
                self.expr(a)
        spans = t.part_spans or [t.span]
        if t.parts == ["This"]:
            if not self.in_trait:
                self.error("E0111", t.span, "'This' is only valid inside a trait")
            return []
        syms = self.resolve_name(t.parts[0], spans[0])
        for part, span in zip(t.parts[1:], spans[1:]):
            if len(syms) != 1 or syms[0].kind is not Kind.UNIT:
                return []  # `Enum.Member` and the like: the type checker reports it
            syms = self.unit_member(syms[0], A.Ident(span, part))
        return syms

    def type_decl(self, d: A.TypeDecl) -> None:
        self.push_type_params(d.params)
        body = d.body
        if isinstance(body, _TYPE_EXPRS):
            self.type_expr(body)
        elif isinstance(body, A.SubrangeType):
            self.expr(body.low)
            self.expr(body.high)
        elif isinstance(body, A.UnionType):
            for v in body.variants:
                self.type_expr(v)
        elif isinstance(body, (A.RecordType, A.TraitType)):
            self.in_trait = isinstance(body, A.TraitType)
            for f in body.fields:
                self.type_expr(f.type)
                if f.default is not None:
                    self.expr(f.default)
            if isinstance(body, A.TraitType):
                for sig in body.routines:
                    self.signature(sig)
            self.in_trait = False
        self.type_scopes.pop()

    # ---------------------------------------------------------------- routines

    def signature(self, sig: A.Signature) -> None:
        """Resolves a signature without a body (extern, trait member)."""
        self.push_type_params(sig.type_params)
        for p in sig.params:
            if p.type is not None:
                self.type_expr(p.type)
            if p.default is not None:
                self.expr(p.default)
        if sig.result is not None:
            self.type_expr(sig.result)
        self.type_scopes.pop()

    def routine(self, r: A.Routine) -> None:
        sig = r.sig
        self.push_type_params(sig.type_params)
        self.ctx = _Ctx()
        for p in sig.params:
            if p.type is not None:
                self.type_expr(p.type)
            if p.default is not None:
                self.expr(p.default)
            for ident in p.names:
                self.define(Kind.PARAM, ident, p)
        if sig.result is not None:
            self.type_expr(sig.result)
        for e in r.requires:
            self.expr(e)
        if r.ensures:
            self.push()
            if sig.is_function:
                self.ctx.scopes[-1]["result"] = Symbol(
                    Kind.RESULT, "result", sig.name.span, self.file, self.unit.name, node=r
                )
            self.ctx.in_ensures = True
            for e in r.ensures:
                self.expr(e)
            self.ctx.in_ensures = False
            self.pop()
        self.push()
        self.stmts(r.body.stmts)
        self.pop()
        self.finish_ctx()
        self.ctx = None
        self.type_scopes.pop()

    # -------------------------------------------------------------- statements

    def stmts(self, stmts: list) -> None:
        for s in stmts:
            self.stmt(s)

    def block(self, body) -> None:
        self.push()
        if isinstance(body, A.Block):
            self.stmts(body.stmts)
        else:
            self.stmt(body)
        self.pop()

    def stmt(self, s) -> None:
        if isinstance(s, A.VarDecl):
            if s.type is not None:
                self.type_expr(s.type)
            self.expr(s.value)
            kind = Kind.VAR if s.mutable else Kind.LET
            self.define(kind, s.name, s, unused_fix=s.span.join(s.type.span if s.type else s.name.span))
        elif isinstance(s, A.Assign):
            if isinstance(s.target, A.Name):
                self.name(s.target, read=False)
            else:
                self.expr(s.target)
            self.expr(s.value)
        elif isinstance(s, A.Discard):
            self.expr(s.value)
        elif isinstance(s, A.ExprStmt):
            self.expr(s.expr)
        elif isinstance(s, A.If):
            for b in s.branches:
                self.expr(b.cond)
                self.block(b.body)
            if s.else_body is not None:
                self.block(s.else_body)
        elif isinstance(s, A.While):
            self.expr(s.cond)
            self.block(s.body)
        elif isinstance(s, A.Repeat):
            self.push()  # the condition sees the body's variables
            self.stmts(s.body.stmts)
            self.expr(s.cond)
            self.pop()
        elif isinstance(s, A.ForRange):
            self.expr(s.start)
            self.expr(s.stop)
            self.push()
            self.define(Kind.LOOP_VAR, s.var, s)
            self.stmts(s.body.stmts)
            self.pop()
        elif isinstance(s, A.ForIn):
            self.expr(s.iterable)
            self.push()
            for v in s.vars:
                self.define(Kind.LOOP_VAR, v, s, unused_fix=v.span)
            self.stmts(s.body.stmts)
            self.pop()
        elif isinstance(s, A.Case):
            self.expr(s.subject)
            for arm in s.arms:
                self.push()
                self.labels(arm.labels)
                self.block(arm.body)
                self.pop()
        elif isinstance(s, A.Defer):
            self.block(s.body)
        elif isinstance(s, A.Return):
            if s.value is not None:
                self.expr(s.value)
        elif isinstance(s, A.Assert):
            self.expr(s.cond)
            if s.message is not None:
                self.expr(s.message)

    def labels(self, labels: list) -> None:
        for label in labels:
            if isinstance(label, A.ValueLabel):
                if not isinstance(label.value, A.Name):  # bare names need the subject's type
                    self.expr(label.value)
            elif isinstance(label, A.RangeLabel):
                for e in (label.low, label.high):
                    if not isinstance(e, A.Name):
                        self.expr(e)
            elif isinstance(label, A.VariantLabel):
                self.type_name(label.variant)
                self.define(Kind.BINDING, label.binding, label, unused_fix=label.binding.span)

    # ------------------------------------------------------------- expressions

    def name(self, e: A.Name, read: bool = True) -> None:
        if e.name == "This":
            if not self.in_trait:
                self.error("E0111", e.span, "'This' is only valid inside a trait")
            return
        if e.name == "this" and self.lookup("this").symbols == []:
            self.error("E0100", e.span, "'this' is only valid in a trait's routine signatures")
            return
        self.resolve_name(e.name, e.span, read)

    def expr(self, e) -> None:
        if e is None:
            return
        if isinstance(e, A.Name):
            self.name(e)
        elif isinstance(e, A.InterpString):
            for p in e.parts:
                if not isinstance(p, str):
                    self.expr(p)
        elif isinstance(e, (A.ListLit, A.SetLit)):
            for x in e.items:
                self.expr(x)
        elif isinstance(e, A.MapLit):
            for entry in e.entries:
                self.expr(entry.key)
                self.expr(entry.value)
        elif isinstance(e, A.Paren):
            self.expr(e.inner)
        elif isinstance(e, A.Unary):
            self.expr(e.operand)
        elif isinstance(e, A.Binary):
            self.expr(e.left)
            self.expr(e.right)
        elif isinstance(e, A.Call):
            in_ensures = self.ctx is not None and self.ctx.in_ensures
            if not (in_ensures and isinstance(e.callee, A.Name) and e.callee.name == "old"):
                self.expr(e.callee)
            for a in e.args:
                self.expr(a.value)
        elif isinstance(e, A.Index):
            self.expr(e.target)
            for x in e.items:
                if isinstance(x, _TYPE_EXPRS):
                    self.type_expr(x)
                else:
                    self.expr(x)
        elif isinstance(e, A.FieldAccess):
            self.static_target(e)
        elif isinstance(e, A.Deref):
            self.expr(e.target)
        elif isinstance(e, A.IfExpr):
            for b in e.branches:
                self.expr(b.cond)
                self.expr(b.value)
            self.expr(e.else_value)
        elif isinstance(e, A.CaseExpr):
            self.expr(e.subject)
            for arm in e.arms:
                if self.ctx is not None:
                    self.push()
                self.labels(arm.labels)
                self.expr(arm.value)
                if self.ctx is not None:
                    self.pop()

    # ------------------------------------------------------------------ naming

    def check_casing(self) -> None:
        """E0020 (SPEC §2.5), with a fix that renames the definition and its uses here."""
        defs = [r for r in self.c.index.refs if r.is_def and r.file == self.file]
        for d in defs:
            sym = d.targets[0]
            if sym.kind is Kind.RESULT or sym.name == "this" or not sym.name[:1].isalpha():
                continue
            upper = sym.name[0].isupper()
            if sym.kind in UPPER_CAMEL and not upper:
                want, style = sym.name[0].upper() + sym.name[1:], "UpperCamel"
            elif sym.kind in LOWER_CAMEL and upper:
                want, style = sym.name[0].lower() + sym.name[1:], "lowerCamel"
            else:
                continue
            edits = tuple(
                Edit(r.span, want)
                for r in self.c.index.references(sym)
                if r.file == self.file and len(r.targets) == 1
            )
            self.error(
                "E0020", d.span,
                f"{sym.kind.value} names are {style}: '{want}', not '{sym.name}'",
                [Fix(f"rename to '{want}'", edits)],
            )  # fmt: skip

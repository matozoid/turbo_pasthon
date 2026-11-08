"""The emit pass: typed AST to an x86-64 executable (SPEC §15.2 steps 4–5).

The code generator grows construct by construct. Anything it can't compile yet is
reported as `E0990` at the construct, so a program either builds correctly or not at all.

Supported so far:
- values of type Integer (and the sized integers), Boolean, Char, String, enums, and
  non-generic or concretely instantiated records and unions (so `Option[Integer]`,
  `Result[String, Integer]`): literals, constants, locals, parameters (`var` ones too),
  fields, construction, arithmetic with overflow checks, integer conversions, comparisons,
  `and`/`or`/`not`, `if` and `case` expressions, string interpolation;
- C interop (SPEC §10): calls into shared libraries, callbacks, `Ptr[T]`/`Pointer`/
  `CString`, `deref`, pointer arithmetic, `cast`, `addr`, C records,
  `toCString`/`fromCString`, `isNull`;
- statements: `var`/`let`, assignment (to fields too), `if`, `case`, `while`, `repeat`,
  `for ... to/downto`, `break`, `continue`, `return`, `assert`, `_ := e`, calls;
- routines (up to 6 parameters), with defaults, named arguments, dot calls, and
  contracts; a generic routine is compiled once per instance (SPEC §8), with calls to
  bound members resolved where its type arguments became concrete;
- built-ins: `write`/`writeln`, `toString`, `ord`, `halt`, `inc`, `dec`, and
  `byteLen`, `charCount`, `slice`, `startsWith` from `Strings`.

Scheme (SPEC §15.2: no IR, no optimizer): every expression leaves its value in rax;
binary operators push the left operand while the right one is computed. Each routine has
an rbp frame with one 8-byte slot per local. Calls follow System V: arguments in rdi, rsi,
rdx, rcx, r8, r9, result in rax. A `var` argument is passed as the address of its place.

Strings, records and unions (SPEC §4.6, §11) are pointers to reference-counted heap
blocks. Every block starts with a refcount and a descriptor:

    string: refcount | 0          | byte length | char count | UTF-8 bytes | NUL
    record: refcount | descriptor | field 0 | field 1 | ...     (8 bytes per field)
    union:  refcount | descriptor | the variant's value

A descriptor (read-only data) gives the number of fields, the block size, a number that
makes it unique, and per field whether it holds a reference. A union value has its own
block, whose descriptor says which variant it holds; `case` compares descriptors. So one
release, one copy, and one equality routine serve every type.

String literals live in read-only data with refcount -1, which retain and release ignore.
Reference counting follows one rule: every expression produces an *owned* reference.
Reading a variable retains; whoever consumes a value releases it or stores it. Callees
own their arguments and release them on exit. Locals are released when their block ends
(or a `return`, `break` or `continue` leaves it).

Records are values: a copy shares the block, and writing a field first makes the block
unique (copying it if it is shared). A `var` argument that is a whole variable is passed
as the variable's address. Any other place (`var p.x`) is copied into a temporary, whose
address is passed, and copied back after the call. So no pointer into a block ever
outlives the statement that made it, except one made by `addr` in unsafe code.

Sized integers are kept sign- or zero-extended to 64 bits; arithmetic is done in 64 bits
and then checked against the type's range. A C record's block holds the C struct's bytes,
zero-padded to whole words that its descriptor marks as plain data. C places (`deref(p)`,
fields of C records) are read and written at their C width.

Panics (SPEC §12) print `panic P0002: integer overflow` and the source position to
stderr, then exit with status 70. Each panic site has its own out-of-line stub.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field

from . import ast as A
from . import elf
from .diagnostics import Diagnostic, Severity
from .resolve import CheckResult
from .symbols import Kind, Symbol
from .types import (
    BOOLEAN, CHAR, INTEGER, STRING, THIS, VOID, Builtin, FuncT, Named, Prim, TypeValue,
    has_error, params_in, show, subst, unify,
)  # fmt: skip
from .x86 import (
    R8, R9, R10, R11, R12, R13, R14, R15, RAX, RBP, RBX, RCX, RDI, RDX, RSI, RSP, Assembler,
)  # fmt: skip

_PANIC_STATUS = 70
_MAX_DEPTH = 64  # nesting of generic instances (SPEC §8, E0720)
_ARG_REGS = (RDI, RSI, RDX, RCX, R8, R9)
_CALLER_SAVED = (RAX, RCX, RDX, RSI, RDI, R8, R9, R10, R11)
_DECLS = (A.TypeSection, A.ConstSection, A.Routine, A.ExternBlock, A.TestBlock)
_VALUES = (INTEGER, BOOLEAN, CHAR, STRING)  # the types the code generator handles so far
_TO_STRING = {BOOLEAN: "rt_bool_to_str", CHAR: "rt_char_to_str"}
_COMPARE = {"=": "e", "<>": "ne", "<": "l", "<=": "le", ">": "g", ">=": "ge"}
_UNSIGNED = {"=": "e", "<>": "ne", "<": "b", "<=": "be", ">": "a", ">=": "ae"}
# Integer types: (bits, signed). A value is kept sign- or zero-extended to 64 bits.
_INTS = {
    "Int8": (8, True), "Int16": (16, True), "Int32": (32, True), "Int64": (64, True),
    "UInt8": (8, False), "UInt16": (16, False), "UInt32": (32, False), "UInt64": (64, False),
}  # fmt: skip
_UINT64 = Prim("UInt64")
# Heap block layout: the header, then a string's parts or a record's fields.
_RC, _DESC = 0, 8
_LEN, _CHARS, _BYTES = 16, 24, 32
_FIELDS = 16
# A list block: the header, the element count, the capacity, then the elements.
_COUNT, _CAP, _ELEMS = 16, 24, 32
# Descriptor layout: then one word per field, 1 if it holds a reference. A list's
# descriptor has count _LIST and one kind word, for all its elements.
_D_COUNT, _D_SIZE, _D_ID, _D_KINDS = 0, 8, 16, 24
_LIST = -1


@dataclass
class BuildResult:
    executable: bytes | None
    diagnostics: list[Diagnostic] = field(default_factory=list)


def build(result: CheckResult, checked: bool = True) -> BuildResult:
    """Compiles a checked program. ``result`` must have no errors. ``checked`` keeps the
    runtime checks that `--unchecked` drops (overflow, bounds, contracts)."""
    if result.main is None or result.has_errors:
        raise ValueError("build needs a program that checked without errors")
    return Emitter(result, checked).run()


class _Unsupported(Exception):
    """Raised to abandon the construct being compiled after reporting E0990."""


@dataclass
class _Loop:
    brk: str
    cont: str
    depth: int  # scopes outside the loop's body


@dataclass
class _Frame:
    """The routine (or program body) being compiled."""

    file: str
    sym: Symbol | None
    exit: str  # label of the epilogue
    slots: dict = field(default_factory=dict)  # Symbol or key -> rbp offset
    size: int = 0
    # Reference-counted slots per open block, innermost last. The first holds the
    # parameters and `old` values, released in the epilogue.
    scopes: list = field(default_factory=lambda: [[]])
    loops: list = field(default_factory=list)
    result_slot: int | None = None
    ensures: list = field(default_factory=list)
    old: dict = field(default_factory=dict)  # id(old call) -> slot
    # A generic routine's instance: its type parameters' arguments, and per parameter
    # the file whose visibility resolves bound members (SPEC §8.2).
    s: dict = field(default_factory=dict)
    sites: dict = field(default_factory=dict)
    depth: int = 0  # instances that led to this one
    keep: dict = field(default_factory=dict)  # id(toCString call) -> slot keeping its string

    def alloc(self, key) -> int:
        self.size += 8
        self.slots[key] = -self.size
        return -self.size


class Emitter:
    def __init__(self, result: CheckResult, checked: bool = True) -> None:
        self.r = result
        self.checked = checked
        self.env = result.env
        self.main = result.main
        self.asm = Assembler()
        self.diagnostics: list[Diagnostic] = []
        self._runtime: dict[str, object] = {}  # runtime routines used: label -> emitter
        self._panics: list[tuple[str, bytes]] = []  # (stub label, message)
        self._labels: dict[tuple, str] = {}  # (routine, type arguments) -> label
        self._queue: list[tuple] = []  # (routine, type arguments, sites, depth) to emit
        self._descs: dict[object, str] = {}  # record type, or (union, variant) -> label
        self._thunks: list[tuple] = []  # (label, routine label, parameter types) of callbacks
        self.needed: list[bytes] = [elf.LIBC]  # shared libraries, in DT_NEEDED order
        self.library: dict[int, str] = {}  # id(extern declaration) -> its library
        self.units = {u.path: u for u in [self.main, *result.units.values()]}
        # Expression types and resolved calls, from every unit's checker.
        self.types: dict[int, object] = {}
        self.calls: dict[int, object] = {}
        for c in [*self.env._checkers.values(), *self.env._const_checkers.values()]:
            self.types.update(c.types)
            self.calls.update(c.calls)
        self.f: _Frame | None = None
        for u in self.units.values():
            for item in u.tree.items:
                if isinstance(item, A.ExternBlock):
                    for d in item.decls:
                        self.library[id(d)] = item.library

    # ------------------------------------------------------------------ helpers

    def unsupported(self, node, what: str):
        d = Diagnostic(
            self.f.file if self.f else self.main.path, node.span, Severity.ERROR, "E0990",
            f"{what} is not supported by the code generator yet",
        )  # fmt: skip
        if d not in self.diagnostics:
            self.diagnostics.append(d)
        raise _Unsupported()

    def type(self, e):
        return self.sub(self.types.get(id(e)))

    def sub(self, t):
        """``t`` in the instance being compiled: its type parameters replaced."""
        return subst(t, self.f.s) if self.f is not None and t is not None else t

    def kind(self, t) -> str | None:
        """"record", "union", "enum", ... for a declared type."""
        return self.env.kind_of(t)

    @staticmethod
    def ints(t) -> tuple | None:
        """(bits, signed) for an integer type."""
        return _INTS.get(t.name) if isinstance(t, Prim) else None

    def is_c_record(self, t) -> bool:
        return self.kind(t) == "record" and self.env.decl(t.sym).c

    def normalize(self, t, reg: int = RAX) -> None:
        """Extends the low bits of a value of type ``t`` that C (or a cast) left in ``reg``."""
        info = self.ints(t)
        if info is not None:
            self.asm.extend(reg, *info)
        elif t == BOOLEAN:
            self.asm.movzx8(reg, reg)

    def fit(self, t, span, cc: str = "o") -> None:
        """After 64-bit arithmetic in rax: wraps the result to integer type ``t``. A checked
        build panics if it didn't fit (for 64-bit types: if flag ``cc`` is set)."""
        a = self.asm
        bits, signed = self.ints(t) or (64, True)
        if bits == 64:
            if self.checked:
                a.jcc(cc, self.panic("P0002", "integer overflow", span))
            return
        if self.checked:
            a.mov(RDX, RAX)
        a.extend(RAX, bits, signed)
        if self.checked:
            a.cmp(RAX, RDX)
            a.jcc("ne", self.panic("P0002", "integer overflow", span))

    def managed(self, t) -> bool:
        """Whether values of type ``t`` are reference counted."""
        return t == STRING or self.kind(t) in ("record", "union") or _is_list(t)

    def supported(self, t, seen: frozenset = frozenset()) -> bool:
        if t in _VALUES or has_error(t) or t in seen or self.ints(t) or _is_pointer(t):
            return True
        if _is_list(t):
            return self.supported(t.args[0], seen)
        if isinstance(t, FuncT):  # a named top-level routine: its address
            return all(self.supported(p, seen) for p in (*t.params, t.result) if p != VOID)
        kind = self.kind(t)
        if kind == "enum":
            return True
        if kind not in ("record", "union"):
            return False
        parts = [ft for _, ft in self.fields(t)] if kind == "record" else self.env.variants(t)
        return all(self.supported(p, seen | {t}) for p in parts)

    def value_type(self, t, node, what: str = "a value"):
        """Checks that the code generator handles values of type ``t``."""
        if not self.supported(t):
            self.unsupported(node, f"{what} of type {show(t)}")

    def fields(self, t) -> list:
        """(name, type) of each field of record type ``t``."""
        d = self.env.decl(t.sym)
        s = dict(zip(d.params, t.args))
        return [(f.name, subst(f.type, s)) for f in d.fields]

    def descriptor(self, key, parts: list) -> str:
        """The data label of the descriptor for blocks of ``key`` holding ``parts``."""
        if key not in self._descs:
            words = [len(parts), _FIELDS + 8 * len(parts), len(self._descs)]
            words += [int(self.managed(p)) for p in parts]
            self._descs[key] = self.asm.data(struct.pack(f"<{len(words)}q", *words), align=8)
        return self._descs[key]

    def list_desc(self, t) -> str:
        """The descriptor of lists of type ``t``."""
        key = ("list", t.args[0])
        if key not in self._descs:
            words = [_LIST, _ELEMS, len(self._descs), int(self.managed(t.args[0]))]
            self._descs[key] = self.asm.data(struct.pack("<4q", *words), align=8)
        return self._descs[key]

    def new_block(self, desc: str) -> None:
        """rax := a new block with descriptor ``desc``; its fields are the caller's."""
        self.asm.lea_data(RDI, desc)
        self.asm.call(self.runtime("rt_new"))

    def coerce(self, e, want) -> None:
        """Computes ``e`` into rax as a value of type ``want``: a variant is put into its
        union (SPEC §4.7, the only implicit conversion)."""
        self.expr(e)
        have = self.type(e)
        if want is None or have == want or self.kind(want) != "union":
            return
        variants = self.env.variants(want)
        if have not in variants:
            self.unsupported(e, f"converting {show(have)} to {show(want)}")
        self.asm.push(RAX)
        self.new_block(self.descriptor((want, variants.index(have)), [have]))
        self.asm.pop(RCX)
        self.asm.store(RAX, _FIELDS, RCX)

    def text(self, node) -> str:
        u = self.units.get(self.f.file)
        return u.text[node.span.start : node.span.end] if u is not None else "?"

    def where(self, span) -> str:
        u = self.units.get(self.f.file)
        if u is not None and u.std:
            path = os.path.basename(self.f.file)
        else:
            path = os.path.relpath(self.f.file, os.path.dirname(self.main.path))
        return f"{path}:{span.line}:{span.col}"

    def ref(self, span) -> Symbol | None:
        r = self.r.index.lookup(self.f.file, span)
        return r.targets[0] if r is not None and len(r.targets) == 1 else None

    def panic(self, code: str, message: str, span) -> str:
        """A label that panics with ``code`` at ``span``."""
        label = self.asm.new_label("panic")
        text = f"panic {code}: {message}\n    at {self.where(span)}\n"
        self._panics.append((label, text.encode("utf-8")))
        return label

    def literal(self, text: str) -> str:
        """The data label of an immortal string block holding ``text``."""
        data = text.encode("utf-8")
        block = struct.pack("<qQQQ", -1, 0, len(data), len(text)) + data + b"\0"
        return self.asm.data(block, align=8)

    def retain(self) -> None:
        """Adds a reference to the block in rax."""
        self.asm.call(self.runtime("rt_retain"))

    def release(self, reg: int = RAX) -> None:
        """Drops a reference to the block in ``reg``. Preserves every register."""
        if reg != RDI:
            self.asm.mov(RDI, reg)
        self.asm.call(self.runtime("rt_release"))

    def drop(self, n: int) -> None:
        """Pops ``n`` owned references off the stack and releases them; keeps rax."""
        for _ in range(n):
            self.asm.pop(RDI)
            self.release(RDI)

    def release_scopes(self, depth: int) -> None:
        """Releases the variables of the blocks above ``depth``, innermost first."""
        keep = set(self.f.keep.values())
        for scope in reversed(self.f.scopes[depth:]):
            for slot in reversed(scope):
                self.asm.load(RDI, RBP, slot)
                self.release(RDI)
                if slot in keep:  # the scope may be entered again without reaching toCString
                    self.asm.store_imm(RBP, slot, 0)

    # --------------------------------------------------------------------- driver

    def run(self) -> BuildResult:
        if self.main.tree.kind != "program":
            self.diagnostics.append(Diagnostic(
                self.main.path, self.main.tree.name.span, Severity.ERROR, "E0990",
                "only a program can be built; a unit is compiled with the program that uses it",
            ))  # fmt: skip
            return BuildResult(None, self.diagnostics)
        a = self.asm
        self.f = _Frame(self.main.path, None, a.new_label("exit"))
        a.label("_start")
        a.mov(RBP, RSP)
        size_at = a.sub_imm32_fixed(RSP)
        body = [i for i in self.main.tree.items if not isinstance(i, _DECLS)]
        self.keep_alive(body)
        self.block(A.Block(self.main.tree.span, body))
        a.label(self.f.exit)
        a.xor32(RDI, RDI)
        self.exit()
        a.patch32(size_at, elf.align(self.f.size, 16))
        while self._queue:
            self.routine(*self._queue.pop())
        self.f = None
        for thunk in self._thunks:
            self.thunk(*thunk)
        for label, message in self._panics:
            a.label(label)
            a.lea_data(RSI, a.data(message))
            a.mov_imm(RDX, len(message))
            a.jmp(self.runtime("rt_panic"))
        self.emit_runtime()
        if self.diagnostics:
            return BuildResult(None, self.diagnostics)
        needed = tuple(self.needed)
        got = elf.got_offset(len(a.code), len(a.rodata), a.imports, needed)
        code = a.finish(elf.rodata_offset(len(a.code)), got)
        exe = elf.executable(code, bytes(a.rodata), a.labels["_start"], a.imports, needed)
        return BuildResult(exe)

    def label_of(self, e, sym: Symbol, targs: tuple = (), sites: dict | None = None) -> str:
        """The label of a routine, or of a generic routine's instance for ``targs``. An
        instance is compiled once, however many places use it (SPEC §8.1)."""
        key = (sym, targs)
        if key not in self._labels:
            depth = self.f.depth + 1 if targs else self.f.depth
            if depth > _MAX_DEPTH:
                self.diagnostics.append(Diagnostic(
                    self.f.file, e.span, Severity.ERROR, "E0720",
                    f"generic instances nest more than {_MAX_DEPTH} deep here: "
                    f"'{sym.name}' keeps instantiating itself with a bigger type",
                ))  # fmt: skip
                raise _Unsupported()
            name = f"{sym.name}[{', '.join(show(t) for t in targs)}]" if targs else sym.name
            self._labels[key] = f"{sym.unit}.{name}#{len(self._labels)}"
            self._queue.append((sym, targs, sites or {}, depth))
        return self._labels[key]

    def routine(self, sym: Symbol, targs: tuple, sites: dict, depth: int) -> None:
        a = self.asm
        node: A.Routine = sym.node
        sig = self.env.sig(sym)
        s = dict(zip(sig.tparams, targs))
        self.f = f = _Frame(sym.file, sym, a.new_label("ret"), s=s, sites=sites, depth=depth)
        a.label(self._labels[(sym, targs)])
        a.push(RBP)
        a.mov(RBP, RSP)
        size_at = a.sub_imm32_fixed(RSP)
        try:
            self.keep_alive([*node.requires, *node.ensures, node.body])
            for i, p in enumerate(sig.params):
                if p.sym is None:
                    self.unsupported(node.sig, "this kind of parameter")
                self.value_type(self.sub(p.type), node.sig.name, f"parameter '{p.name}'")
                slot = f.alloc(p.sym)
                a.store(RBP, slot, _ARG_REGS[i])
                if self.managed(self.sub(p.type)) and not p.is_var:
                    f.scopes[0].append(slot)
            if sig.is_function:
                self.value_type(self.sub(sig.result), node.sig.result, "a result")
            if self.checked:
                for e in node.requires:
                    self.expr(e)
                    a.test(RAX, RAX)
                    a.jcc("e", self.panic("P0010", f"requires violated: {self.text(e)}", e.span))
                if node.ensures:
                    f.ensures = node.ensures
                    f.result_slot = f.alloc("result")
                    for e in node.ensures:
                        self.save_old(e)
            self.block(node.body)
            a.label(f.exit)
            if f.ensures:
                a.store(RBP, f.result_slot, RAX)
                for e in f.ensures:
                    self.expr(e)
                    a.test(RAX, RAX)
                    a.jcc("e", self.panic("P0011", f"ensures violated: {self.text(e)}", e.span))
                a.load(RAX, RBP, f.result_slot)
            self.release_scopes(0)
        except _Unsupported:
            if f.exit not in a.labels:
                a.label(f.exit)
        a.leave()
        a.ret()
        a.patch32(size_at, elf.align(f.size, 16))

    def keep_alive(self, nodes: list) -> None:
        """Gives each `toCString` call in ``nodes`` a slot, cleared on entry, that keeps
        its string alive until the end of the enclosing block (SPEC §10.2)."""
        for n in nodes:
            if isinstance(n, A.Call) and self.is_c(n, "toCString"):
                slot = self.f.alloc(("keep", id(n)))
                self.asm.store_imm(RBP, slot, 0)
                self.f.keep[id(n)] = slot
            self.keep_alive(list(_children(n)))

    def is_c(self, e: A.Call, name: str) -> bool:
        """Whether ``e`` calls the routine ``name`` of unit C."""
        m = self.calls.get(id(e))
        sym = m.sig.sym if m is not None else self.ref(_callee_span(e.callee))
        return sym is not None and sym.unit == "C" and sym.name == name and sym.is_extern

    def save_old(self, e) -> None:
        """Evaluates the `old(x)` calls of an `ensures` on entry."""
        if isinstance(e, A.Call) and self.is_old(e):
            self.expr(e.args[0].value)
            slot = self.f.alloc(("old", id(e)))
            self.asm.store(RBP, slot, RAX)
            self.f.old[id(e)] = slot
            if self.managed(self.type(e)):
                self.f.scopes[0].append(slot)
            return
        for child in _children(e):
            self.save_old(child)

    def is_old(self, e: A.Call) -> bool:
        c = e.callee
        return (
            isinstance(c, A.Name) and c.name == "old" and id(e) not in self.calls
            and self.r.index.lookup(self.f.file, c.span) is None
        )  # fmt: skip

    # ---------------------------------------------------------------- statements

    def stmts(self, stmts: list) -> None:
        for s in stmts:
            try:
                self.stmt(s)
            except _Unsupported:
                pass

    def block(self, body) -> None:
        """Compiles a block; its variables are released at its end."""
        self.f.scopes.append([])
        self.stmts(body.stmts if isinstance(body, A.Block) else [body])
        self.release_scopes(len(self.f.scopes) - 1)
        self.f.scopes.pop()

    def local(self, span) -> int:
        sym = self.ref(span)
        return self.f.alloc(sym if sym is not None else object())

    def stmt(self, s) -> None:
        a = self.asm
        if isinstance(s, A.VarDecl):
            sym = self.ref(s.name.span)
            t = self.sub(sym.type) if sym else None
            self.value_type(t, s.name, f"variable '{s.name.name}'")
            self.coerce(s.value, t)
            slot = self.local(s.name.span)
            a.store(RBP, slot, RAX)
            if self.managed(t):
                self.f.scopes[-1].append(slot)
        elif isinstance(s, A.Assign):
            t = self.type(s.target)
            self.coerce(s.value, t)
            a.push(RAX)
            self.address(s.target)
            a.pop(RCX)
            self.store_at(s.target, t)
        elif isinstance(s, A.Discard):
            self.expr(s.value)
            if self.managed(self.type(s.value)):
                self.release()
        elif isinstance(s, A.ExprStmt):
            if not isinstance(s.expr, A.Call):
                self.unsupported(s, "this statement")
            self.call(s.expr)
        elif isinstance(s, A.If):
            end = a.new_label("fi")
            for b in s.branches:
                nxt = a.new_label("elif")
                self.expr(b.cond)
                a.test(RAX, RAX)
                a.jcc("e", nxt)
                self.block(b.body)
                a.jmp(end)
                a.label(nxt)
            if s.else_body is not None:
                self.block(s.else_body)
            a.label(end)
        elif isinstance(s, A.While):
            top, end = a.new_label("while"), a.new_label("wend")
            a.label(top)
            self.expr(s.cond)
            a.test(RAX, RAX)
            a.jcc("e", end)
            self.loop(s.body, end, top)
            a.jmp(top)
            a.label(end)
        elif isinstance(s, A.Repeat):
            self.repeat(s)
        elif isinstance(s, A.ForRange):
            self.for_range(s)
        elif isinstance(s, A.ForIn):
            self.for_in(s)
        elif isinstance(s, A.Case):
            self.case(s.subject, s.arms, None)
        elif isinstance(s, (A.Break, A.Continue)):
            loop = self.f.loops[-1]
            self.release_scopes(loop.depth)
            a.jmp(loop.brk if isinstance(s, A.Break) else loop.cont)
        elif isinstance(s, A.Return):
            if s.value is not None:
                self.coerce(s.value, self.sub(self.env.sig(self.f.sym).result))
            self.release_scopes(1)  # the parameters are released after `ensures`
            a.jmp(self.f.exit)
        elif isinstance(s, A.Assert):
            message = f"assertion failed: {self.text(s.cond)}"
            if s.message is not None:
                if not isinstance(s.message, A.StringLit):
                    self.unsupported(s.message, "an assert message other than a string literal")
                message += f": {s.message.value}"
            self.expr(s.cond)
            a.test(RAX, RAX)
            a.jcc("e", self.panic("P0020", message, s.span))
        else:
            self.unsupported(s, f"the '{type(s).__name__}' statement")

    def loop(self, body, brk: str, cont: str) -> None:
        self.f.loops.append(_Loop(brk, cont, len(self.f.scopes)))
        self.block(body)
        self.f.loops.pop()

    def repeat(self, s: A.Repeat) -> None:
        """The `until` condition sees the body's variables, so the body's block is
        released after it. (`continue` isn't allowed in `repeat`, SPEC §7.4.)"""
        a = self.asm
        top, end = a.new_label("repeat"), a.new_label("rend")
        depth = len(self.f.scopes)
        a.label(top)
        self.f.scopes.append([])
        self.f.loops.append(_Loop(end, end, depth))
        self.stmts(s.body.stmts)
        self.f.loops.pop()
        try:
            self.expr(s.cond)
        finally:
            self.release_scopes(depth)  # keeps rax
            self.f.scopes.pop()
        a.test(RAX, RAX)
        a.jcc("e", top)
        a.label(end)

    def for_range(self, s: A.ForRange) -> None:
        a = self.asm
        sym = self.ref(s.var.span)
        t = self.sub(sym.type) if sym is not None else None
        if t is not None and t != CHAR and not self.ints(t) and self.kind(t) != "enum":
            self.unsupported(s.var, f"a loop over {show(t)}")
        unsigned = t is not None and self.ints(t) is not None and not self.ints(t)[1]
        var = self.local(s.var.span)
        stop = self.f.alloc(object())
        top, cont, end = a.new_label("for"), a.new_label("next"), a.new_label("rof")
        self.expr(s.start)
        a.store(RBP, var, RAX)
        self.expr(s.stop)
        a.store(RBP, stop, RAX)
        a.mov(RCX, RAX)
        a.load(RAX, RBP, var)
        a.cmp(RAX, RCX)
        a.jcc(("b" if s.down else "a") if unsigned else ("l" if s.down else "g"), end)
        a.label(top)
        self.loop(s.body, end, cont)
        a.label(cont)
        a.load(RAX, RBP, var)  # the counter can't overflow: it stops at `stop`
        a.load(RCX, RBP, stop)
        a.cmp(RAX, RCX)
        a.jcc("e", end)
        a.dec(RAX) if s.down else a.inc(RAX)
        a.store(RBP, var, RAX)
        a.jmp(top)
        a.label(end)

    def for_in(self, s: A.ForIn) -> None:
        """`for x in xs` over a list: the loop runs over the list as it was when the
        loop started (a copy, SPEC §4.14), kept in a slot of its own block."""
        a = self.asm
        t = self.type(s.iterable)
        if len(s.vars) != 1:
            self.unsupported(s.iterable, f"a loop over {show(t)}")
        if isinstance(t, TypeValue):
            return self.for_enum(s, t.type)
        if not _is_list(t):
            return self.for_iterator(s, t)
        managed = self.managed(t.args[0])
        depth = len(self.f.scopes)
        self.f.scopes.append([])
        self.expr(s.iterable)
        items, pos = self.f.alloc(object()), self.f.alloc(object())
        a.store(RBP, items, RAX)
        self.f.scopes[-1].append(items)
        var = self.local(s.vars[0].span)
        a.store_imm(RBP, var, 0)
        if managed:
            self.f.scopes[-1].append(var)
        a.store_imm(RBP, pos, 0)
        top, cont, end = a.new_label("for"), a.new_label("next"), a.new_label("rof")
        a.label(top)
        a.load(RCX, RBP, pos)
        a.load(RAX, RBP, items)
        a.cmp_mem(RCX, RAX, _COUNT)
        a.jcc("ge", end)
        a.shl_imm(RCX, 3)
        a.add(RCX, RAX)
        a.load(RAX, RCX, _ELEMS)
        if managed:
            self.retain()
            a.load(RDI, RBP, var)
            self.release(RDI)  # the previous element
        a.store(RBP, var, RAX)
        self.loop(s.body, end, cont)
        a.label(cont)
        a.load(RAX, RBP, pos)
        a.inc(RAX)
        a.store(RBP, pos, RAX)
        a.jmp(top)
        a.label(end)
        self.release_scopes(depth)
        self.f.scopes.pop()

    def for_enum(self, s: A.ForIn, t) -> None:
        """`for c in Color`: every member, in order."""
        a = self.asm
        if self.kind(t) != "enum":
            self.unsupported(s.iterable, f"a loop over {show(t)}")
        var = self.local(s.vars[0].span)
        top, cont, end = a.new_label("for"), a.new_label("next"), a.new_label("rof")
        a.store_imm(RBP, var, 0)
        a.label(top)
        a.load(RAX, RBP, var)
        a.cmp_imm(RAX, len(self.env.decl(t.sym).members))
        a.jcc("ge", end)
        self.loop(s.body, end, cont)
        a.label(cont)
        a.load(RAX, RBP, var)
        a.inc(RAX)
        a.store(RBP, var, RAX)
        a.jmp(top)
        a.label(end)

    def for_iterator(self, s: A.ForIn, t) -> None:
        """`for x in e` by the iterator protocol (SPEC §4.14): `e` itself if it is an
        Iterator, else `iter(e)`, then `next` until it returns `None`. `iter` and `next`
        are found where the iterable's type became concrete, as bound members are."""
        a = self.asm
        generic = self.types.get(id(s.iterable))
        outer = [p for p in params_in(generic) if p in self.f.sites]
        site = self.f.sites[outer[0]] if outer else self.f.file
        chk = self.env._checkers[site]
        it_t, make = t, None
        item = chk.iterator_item(t)
        if item is None:
            for f in chk.lookup_routines("iter"):
                fs = self.env.sig(f)
                sf: dict = {}
                if len(fs.params) == 1 and isinstance(f.node, A.Routine) and unify(fs.params[0].type, t, sf, set(fs.tparams)):
                    item = chk.iterator_item(subst(fs.result, sf))
                    if item is not None:
                        it_t = subst(fs.result, sf)
                        make = self.label_of(s, f, tuple(sf[tp] for tp in fs.tparams), {tp: site for tp in fs.tparams})
                        break
        trait = self.env.decl(self.env.sys("Iterator"))
        member = next(r for r in trait.routines if r.name == "next")
        found = chk.provider(it_t, member, {trait.params[0]: item, THIS: it_t}, {}) if item is not None else None
        if found is None:
            self.unsupported(s.iterable, f"a loop over {show(t)}")
        f, sf = found
        tparams = self.env.sig(f).tparams
        step = self.label_of(s, f, tuple(sf.get(tp) for tp in tparams), {tp: site for tp in tparams})
        option = subst(self.env.sig(f).result, sf)
        variants = self.env.variants(option)
        none = next(i for i, v in enumerate(variants) if _head(v) is self.env.sys("None"))
        managed = self.managed(item)
        depth = len(self.f.scopes)
        self.f.scopes.append([])
        self.expr(s.iterable)
        if make is not None:
            a.mov(RDI, RAX)
            a.call(make)
        it = self.f.alloc(object())
        a.store(RBP, it, RAX)
        if self.managed(it_t):
            self.f.scopes[-1].append(it)
        var = self.local(s.vars[0].span)
        a.store_imm(RBP, var, 0)
        if managed:
            self.f.scopes[-1].append(var)
        top, done, end = a.new_label("for"), a.new_label("none"), a.new_label("rof")
        a.label(top)
        a.lea(RDI, RBP, it)
        a.call(step)
        a.lea_data(RCX, self.descriptor((option, none), [variants[none]]))
        a.cmp_mem(RCX, RAX, _DESC)
        a.jcc("e", done)
        a.push(RAX)
        a.load(RAX, RAX, _FIELDS)  # the Some
        a.load(RAX, RAX, _FIELDS)  # its value
        if managed:
            self.retain()
            a.load(RDI, RBP, var)
            self.release(RDI)  # the previous value
        a.store(RBP, var, RAX)
        self.drop(1)
        self.loop(s.body, end, top)
        a.jmp(top)
        a.label(done)
        self.release()
        a.label(end)
        self.release_scopes(depth)
        self.f.scopes.pop()

    def case(self, subject, arms: list, result) -> None:
        """A `case` statement, or a `case` expression of type ``result`` (each arm's value
        then ends up in rax). The subject is kept in a slot of its own block."""
        a = self.asm
        t = self.type(subject)
        depth = len(self.f.scopes)
        self.f.scopes.append([])
        self.expr(subject)
        slot = self.f.alloc(object())
        a.store(RBP, slot, RAX)
        if self.managed(t):
            self.f.scopes[-1].append(slot)
        end = a.new_label("esac")
        for arm in arms:
            body, nxt = a.new_label("arm"), a.new_label("arm")
            for label in arm.labels:
                self.case_label(t, slot, label, body)
            if arm.labels:
                a.jmp(nxt)
            a.label(body)
            self.f.scopes.append([])
            bound = [x for x in arm.labels if isinstance(x, A.VariantLabel) and self.ref(x.binding.span)]
            if bound:
                if len(arm.labels) > 1:
                    self.unsupported(bound[0], "a binding in an arm with several labels")
                a.load(RAX, RBP, slot)
                a.load(RAX, RAX, _FIELDS)
                bt = self.sub(self.ref(bound[0].binding.span).type)
                if self.managed(bt):
                    self.retain()
                b = self.local(bound[0].binding.span)
                a.store(RBP, b, RAX)
                if self.managed(bt):
                    self.f.scopes[-1].append(b)
            if result is not None:
                self.coerce(arm.value, result)
            else:
                self.stmts(arm.body.stmts if isinstance(arm.body, A.Block) else [arm.body])
            self.release_scopes(len(self.f.scopes) - 1)  # keeps rax
            self.f.scopes.pop()
            a.jmp(end)
            a.label(nxt)
        a.label(end)
        self.release_scopes(depth)
        self.f.scopes.pop()

    def case_label(self, t, slot: int, label, target: str) -> None:
        """Jumps to ``target`` if the subject in ``slot`` (of type ``t``) matches."""
        a = self.asm
        if self.kind(t) == "union":
            e = label.variant if isinstance(label, A.VariantLabel) else label.value
            span = e.part_spans[-1] if isinstance(e, A.TypeName) and e.part_spans else e.span
            if isinstance(e, A.FieldAccess):
                span = e.name.span
            sym = self.ref(span)
            if sym is not None and sym.kind is Kind.TYPE and self.env.decl(sym).kind == "alias":
                sym = _head(self.env.decl(sym).alias)
            elif sym is not None and sym.kind is Kind.BUILTIN_TYPE:
                sym = sym.name
            variants = self.env.variants(t)
            i = next((i for i, v in enumerate(variants) if _head(v) == sym), None)
            if sym is None or i is None:
                self.unsupported(label, "this label")
            a.load(RAX, RBP, slot)
            a.load(RAX, RAX, _DESC)
            a.lea_data(RCX, self.descriptor((t, i), [variants[i]]))
            a.cmp(RAX, RCX)
            a.jcc("e", target)
        elif isinstance(label, A.RangeLabel):
            skip = a.new_label("range")
            unsigned = self.ints(t) is not None and not self.ints(t)[1]
            self.label_compare(t, slot, label.low)
            a.jcc("b" if unsigned else "l", skip)
            self.label_compare(t, slot, label.high)
            a.jcc("be" if unsigned else "le", target)
            a.label(skip)
        else:
            self.label_compare(t, slot, label.value)
            a.jcc("e", target)

    def label_compare(self, t, slot: int, e) -> None:
        """Compares the subject in ``slot`` with the label value ``e``, setting the flags."""
        a = self.asm
        self.expr(e)
        if t == STRING:
            a.push(RAX)
            a.mov(RSI, RAX)
            a.load(RDI, RBP, slot)
            a.call(self.runtime("rt_str_cmp"))
            self.drop(1)
            a.cmp_imm(RAX, 0)
        else:
            a.load(RCX, RBP, slot)
            a.cmp(RCX, RAX)

    def address(self, e) -> None:
        """Puts the address of the place ``e`` in rax. A field's record is made unique
        first, so the write doesn't show through other copies."""
        if isinstance(e, A.Paren):
            return self.address(e.inner)
        if self.is_cplace(e):
            return self.cplace(e)
        if isinstance(e, A.FieldAccess) and self.kind(self.type(e.target)) == "record":
            names = [n for n, _ in self.fields(self.type(e.target))]
            self.address(e.target)
            self.asm.mov(RDI, RAX)
            self.asm.call(self.runtime("rt_unique"))
            self.asm.add_imm(RAX, _FIELDS + 8 * names.index(e.name.name))
            return
        if isinstance(e, A.Index) and _is_list(self.type(e.target)):
            self.expr(e.items[0])
            self.asm.push(RAX)
            self.address(e.target)
            self.asm.mov(RDI, RAX)
            self.asm.call(self.runtime("rt_unique"))
            self.asm.pop(RCX)
            self.element(RAX, RCX, e)
            return
        if isinstance(e, A.Name):
            sym = self.ref(e.span)
            if sym is not None and sym in self.f.slots:
                if self.is_var_param(sym):
                    self.asm.load(RAX, RBP, self.f.slots[sym])
                else:
                    self.asm.lea(RAX, RBP, self.f.slots[sym])
                return
        self.unsupported(e, "changing this kind of place")

    def is_cplace(self, e) -> bool:
        """Whether the place ``e`` is in C memory: `deref(p)`, or a field of a C record."""
        e = _unparen(e)
        if isinstance(e, A.Deref):
            return True
        return isinstance(e, A.FieldAccess) and self.is_c_record(self.type(e.target))

    def cplace(self, e) -> None:
        """rax := the address of the C bytes of place ``e``: `deref(p)`, a field of a C
        record, or a C record held in a variable (made unique first)."""
        e = _unparen(e)
        if isinstance(e, A.Deref):
            return self.expr(e.target)
        if isinstance(e, A.FieldAccess) and self.is_c_record(self.type(e.target)):
            self.cplace(e.target)
            self.asm.add_imm(RAX, self.c_offset(self.type(e.target), e.name.name, e))
            return
        self.address(e)
        self.asm.mov(RDI, RAX)
        self.asm.call(self.runtime("rt_unique"))
        self.asm.add_imm(RAX, _FIELDS)

    def store_at(self, place, t) -> None:
        """Stores the owned value rcx of type ``t`` at address rax of ``place``, releasing
        what it replaces."""
        if self.is_cplace(place):
            return self.store_c(t, place)
        if self.managed(t):
            self.asm.load(RDI, RAX)
            self.release(RDI)
        self.asm.store(RAX, 0, RCX)

    def load_at(self, place, t) -> None:
        """rax := the value of type ``t`` at address rax of ``place`` (owned)."""
        if self.is_cplace(place):
            return self.load_c(t, place)
        self.asm.load(RAX, RAX)
        if self.managed(t):
            self.retain()

    # ------------------------------------------------------------------ C memory
    # C records (SPEC §10.5) are blocks like other records, but their payload has the C
    # struct's layout. Its padding and tail are zeroed, so `=` compares it word by word.

    def c_layout(self, t, node) -> tuple[int, int]:
        """(size, alignment) of type ``t`` in C memory."""
        info = self.ints(t)
        if info is not None:
            return info[0] // 8, info[0] // 8
        if t == BOOLEAN:
            return 1, 1
        if _is_pointer(t) or isinstance(t, FuncT):
            return 8, 8
        if self.is_c_record(t):
            _, size, align = self.c_struct(t, node)
            return size, align
        self.unsupported(node, f"{show(t)} in C memory")

    def c_struct(self, t, node) -> tuple[list, int, int]:
        """The fields of C record ``t`` as (name, type, offset), its size and alignment."""
        out, offset, align = [], 0, 1
        for name, ft in self.fields(t):
            size, al = self.c_layout(ft, node)
            offset = elf.align(offset, al)
            out.append((name, ft, offset))
            offset += size
            align = max(align, al)
        return out, elf.align(offset, align), align

    def c_offset(self, t, name: str, node) -> int:
        return next(off for n, _, off in self.c_struct(t, node)[0] if n == name)

    def new_c_block(self, t, node) -> None:
        """rax := a new zeroed block for C record ``t``."""
        a = self.asm
        words = (self.c_struct(t, node)[1] + 7) // 8
        self.new_block(self.descriptor(t, [INTEGER] * words))
        a.push(RAX)
        a.lea(RDI, RAX, _FIELDS)
        a.xor32(RSI, RSI)
        a.mov_imm(RDX, 8 * words)
        self.ccall("memset")
        a.pop(RAX)

    def load_c(self, t, node) -> None:
        """rax := the value of type ``t`` in C memory at address rax (owned)."""
        a = self.asm
        info = self.ints(t)
        if info is not None:
            a.load_w(RAX, RAX, 0, *info)
        elif t == BOOLEAN:
            a.load8(RAX, RAX)
        elif _is_pointer(t):
            a.load(RAX, RAX)
        elif self.is_c_record(t):
            a.push(RAX)
            self.new_c_block(t, node)
            a.push(RAX)
            a.lea(RDI, RAX, _FIELDS)
            a.load(RSI, RSP, 8)
            a.mov_imm(RDX, self.c_layout(t, node)[0])
            self.ccall("memcpy")
            a.pop(RAX)
            a.add_imm(RSP, 8)
        else:
            self.unsupported(node, f"reading {show(t)} from C memory")

    def store_c(self, t, node) -> None:
        """Stores the owned value rcx of type ``t`` in C memory at address rax."""
        a = self.asm
        info = self.ints(t)
        if info is not None:
            a.store_w(RAX, 0, RCX, info[0])
        elif t == BOOLEAN:
            a.store8(RAX, 0, RCX)
        elif _is_pointer(t):
            a.store(RAX, 0, RCX)
        elif self.is_c_record(t):
            a.push(RCX)
            a.mov(RDI, RAX)
            a.lea(RSI, RCX, _FIELDS)
            a.mov_imm(RDX, self.c_layout(t, node)[0])
            self.ccall("memcpy")
            self.drop(1)
        else:
            self.unsupported(node, f"writing {show(t)} to C memory")

    @staticmethod
    def is_var_param(sym: Symbol) -> bool:
        return sym.kind is Kind.PARAM and getattr(sym.node, "is_var", False)

    # --------------------------------------------------------------- expressions

    def expr(self, e) -> None:
        """Computes ``e`` into rax (an owned reference, for a string)."""
        t = self.type(e)
        if t is not None and t != VOID:
            self.value_type(t, e)
        m = getattr(self, "e_" + type(e).__name__, None)
        if m is None:
            self.unsupported(e, "this expression")
        m(e)

    def e_IntLit(self, e) -> None:
        self.asm.mov_imm(RAX, e.value)

    def e_BoolLit(self, e) -> None:
        self.asm.mov_imm(RAX, 1 if e.value else 0)

    def e_CharLit(self, e) -> None:
        self.asm.mov_imm(RAX, ord(e.value))

    def e_StringLit(self, e) -> None:
        self.asm.lea_data(RAX, self.literal(e.value))

    def e_InterpString(self, e) -> None:
        self.string_of(e.parts)

    def e_Paren(self, e) -> None:
        self.expr(e.inner)

    def e_Name(self, e) -> None:
        sym = self.ref(e.span)
        if sym is None:
            self.unsupported(e, "this name")
        self.load_sym(sym, e)

    def e_FieldAccess(self, e) -> None:
        t = self.type(e.target)
        if t is None or isinstance(t, TypeValue):  # `Color.Red`, `Unit.name`
            sym = self.ref(e.name.span)
            if sym is None:
                self.unsupported(e, "this name")
            return self.load_sym(sym, e)
        if self.kind(t) != "record":
            self.unsupported(e, f"a field of {show(t)}")
        if self.is_c_record(t):
            self.expr(e.target)
            self.asm.push(RAX)
            self.asm.add_imm(RAX, _FIELDS + self.c_offset(t, e.name.name, e))
            self.load_c(self.type(e), e)
            self.drop(1)
            return
        names = [n for n, _ in self.fields(t)]
        self.expr(e.target)
        self.asm.push(RAX)
        self.asm.load(RAX, RAX, _FIELDS + 8 * names.index(e.name.name))
        if self.managed(self.type(e)):
            self.retain()
        self.drop(1)

    def e_ListLit(self, e) -> None:
        a = self.asm
        t = self.type(e)
        if not _is_list(t):
            self.unsupported(e, f"a literal of {show(t)}")
        for x in e.items:
            self.coerce(x, t.args[0])
            a.push(RAX)
        a.lea_data(RDI, self.list_desc(t))
        a.mov_imm(RSI, len(e.items))
        a.call(self.runtime("rt_list_new"))
        for i in reversed(range(len(e.items))):
            a.pop(RCX)
            a.store(RAX, _ELEMS + 8 * i, RCX)

    def e_Index(self, e) -> None:
        a = self.asm
        t = self.type(e.target)
        if not _is_list(t):
            self.unsupported(e, f"indexing {show(t)}")
        self.expr(e.target)
        a.push(RAX)
        self.expr(e.items[0])
        a.mov(RCX, RAX)
        a.load(RAX, RSP)
        self.element(RAX, RCX, e)
        a.load(RAX, RAX)
        if self.managed(t.args[0]):
            self.retain()
        self.drop(1)

    def element(self, block: int, index: int, e) -> None:
        """rax := the address of element ``index`` of the list ``block``. A checked build
        panics (P0001) if there is no such element."""
        a = self.asm
        if self.checked:
            a.cmp_mem(index, block, _COUNT)
            a.jcc("ae", self.panic("P0001", "index out of bounds", e.span))  # also < 0
        a.shl_imm(index, 3)
        a.add(index, block)
        a.lea(RAX, index, _ELEMS)

    def load_sym(self, sym: Symbol, e) -> None:
        a = self.asm
        if sym.kind is Kind.CONST:
            outer = self.f.file
            self.f.file = sym.file
            try:
                self.expr(sym.node.value)
            finally:
                self.f.file = outer
            return
        t = self.type(e)
        if sym.kind is Kind.ENUM_MEMBER:
            a.mov_imm(RAX, self.env.decl(self.env.owner[sym]).members.index(sym.name))
            return
        if sym.kind is Kind.TYPE and self.kind(t) == "record":  # `None`
            self.new_block(self.descriptor(t, []))
            return
        if sym.is_routine:
            if not isinstance(sym.node, A.Routine) or self.env.sig(sym).tparams:
                self.unsupported(e, f"{sym.describe()} as a value")
            a.lea_code(RAX, self.label_of(e, sym))
            return
        if sym.kind is Kind.RESULT and self.f.result_slot is not None:
            a.load(RAX, RBP, self.f.result_slot)
        elif sym in self.f.slots:
            a.load(RAX, RBP, self.f.slots[sym])
            if self.is_var_param(sym):
                a.load(RAX, RAX)
            if self.ints(t) is not None:  # C may have written only the low bits (`addr`)
                self.normalize(t)
        else:
            self.unsupported(e, f"using {sym.describe()} as a value")
        if self.managed(t):
            self.retain()

    def e_Deref(self, e) -> None:
        t = self.type(e.target)
        if not _is_ptr(t):
            self.unsupported(e, f"'deref' of {show(t)}")
        self.expr(e.target)
        self.load_c(self.type(e), e)

    def e_Unary(self, e) -> None:
        a = self.asm
        if e.op == "not":
            self.expr(e.operand)
            a.xor_imm32(RAX, 1)
        elif e.op == "-":
            if isinstance(e.operand, A.IntLit):
                a.mov_imm(RAX, -e.operand.value)
                return
            self.expr(e.operand)
            a.neg(RAX)  # sets CF unless the operand is 0
            t = self.type(e)
            self.fit(t, e.span, "b" if t == _UINT64 else "o")
        else:
            self.unsupported(e, f"'{e.op}'")

    def overflow(self, span) -> None:
        if self.checked:
            self.asm.jcc("o", self.panic("P0002", "integer overflow", span))

    def e_Binary(self, e) -> None:
        a = self.asm
        op = e.op
        if op in ("and", "or"):
            end = a.new_label(op)
            self.expr(e.left)
            a.test(RAX, RAX)
            a.jcc("e" if op == "and" else "ne", end)
            self.expr(e.right)
            a.label(end)
            return
        if op == "in":
            t = self.type(e.right)
            if not _is_list(t):
                self.unsupported(e, f"'in' on {show(t)}")
            return self.sys_call(e, "contains", [(e.left, t.args[0]), (e.right, t)], (t.args[0],))
        if _is_ptr(self.type(e.left)) and op in ("+", "-"):
            return self.pointer_arith(e)
        t = self.type(e.left)
        if self.kind(self.type(e.right)) == "union":
            t = self.type(e.right)  # comparing a variant with a union
        self.coerce(e.left, t)
        a.push(RAX)
        self.coerce(e.right, t)
        if self.managed(t) and t != STRING:  # `=` or `<>`
            a.push(RAX)
            a.load(RDI, RSP, 8)
            a.mov(RSI, RAX)
            a.call(self.runtime("rt_eq"))
            self.drop(2)
            if op == "<>":
                a.xor_imm32(RAX, 1)
            return
        if t == STRING and op in _COMPARE:
            a.push(RAX)
            a.load(RDI, RSP, 8)
            a.mov(RSI, RAX)
            a.call(self.runtime("rt_str_cmp"))
            self.drop(2)
            a.cmp_imm(RAX, 0)
            a.setcc(_COMPARE[op], RAX)
            a.movzx8(RAX, RAX)
            return
        a.mov(RCX, RAX)
        a.pop(RAX)
        if op in _COMPARE:
            a.cmp(RAX, RCX)
            unsigned = _UINT64 in (self.type(e.left), self.type(e.right))
            a.setcc((_UNSIGNED if unsigned else _COMPARE)[op], RAX)
            a.movzx8(RAX, RAX)
        elif op in ("+", "-", "*", "div", "mod"):
            self.arith(op, self.type(e), e.op_span)
        else:
            self.unsupported(e, f"'{op}'")

    def arith(self, op: str, t, span) -> None:
        """rax := rax op rcx for integers of type ``t``, checked for overflow."""
        a = self.asm
        if op in ("div", "mod"):
            return self.divide(op, t, span)
        if t == _UINT64:
            if op == "*":
                a.mul(RCX)
            else:
                (a.add if op == "+" else a.sub)(RAX, RCX)
            return self.fit(t, span, "b")
        {"+": a.add, "-": a.sub, "*": a.imul}[op](RAX, RCX)
        self.fit(t, span)

    def pointer_arith(self, e) -> None:
        """`p + n`, `p - n`: moves a `Ptr[T]` by ``n`` values of type T (SPEC §10.4)."""
        a = self.asm
        size, _ = self.c_layout(self.type(e.left).args[0], e)
        self.expr(e.left)
        a.push(RAX)
        self.expr(e.right)
        a.mov_imm(RCX, size)
        a.imul(RCX, RAX)
        a.pop(RAX)
        (a.add if e.op == "+" else a.sub)(RAX, RCX)

    def divide(self, op: str, t, span) -> None:
        """rax div/mod rcx, truncating toward zero (the remainder takes the sign of the
        dividend). Division by zero panics even in unchecked builds."""
        a = self.asm
        normal, done = a.new_label("div"), a.new_label("div")
        a.test(RCX, RCX)
        a.jcc("e", self.panic("P0004", "division by zero", span))
        if not (self.ints(t) or (64, True))[1]:
            a.xor32(RDX, RDX)
            a.div(RCX)
            if op == "mod":
                a.mov(RAX, RDX)
            return
        a.cmp_imm(RCX, -1)  # idiv traps on MIN div -1
        a.jcc("ne", normal)
        if op == "div":
            a.neg(RAX)
            self.overflow(span)
        else:
            a.xor32(RAX, RAX)
        a.jmp(done)
        a.label(normal)
        a.cqo()
        a.idiv(RCX)
        if op == "mod":
            a.mov(RAX, RDX)
        a.label(done)
        if (self.ints(t) or (64, True))[0] < 64:
            self.fit(t, span)  # MIN div -1

    def e_IfExpr(self, e) -> None:
        a = self.asm
        end = a.new_label("fi")
        for b in e.branches:
            nxt = a.new_label("elif")
            self.expr(b.cond)
            a.test(RAX, RAX)
            a.jcc("e", nxt)
            self.coerce(b.value, self.type(e))
            a.jmp(end)
            a.label(nxt)
        self.coerce(e.else_value, self.type(e))
        a.label(end)

    def e_CaseExpr(self, e) -> None:
        self.case(e.subject, e.arms, self.type(e))

    def e_Call(self, e) -> None:
        if id(e) in self.f.old:
            self.asm.load(RAX, RBP, self.f.old[id(e)])
            if self.managed(self.type(e)):
                self.retain()
            return
        self.call(e)

    # ------------------------------------------------------------------- strings

    def string_of(self, parts: list) -> None:
        """Builds a new string from text and Printable expressions, into rax."""
        a = self.asm
        for p in parts:
            if isinstance(p, str):
                a.lea_data(RAX, self.literal(p))
            else:
                self.to_string(p)
            a.push(RAX)
        a.mov(RDI, RSP)
        a.lea(RSI, RSP, 8 * len(parts))
        a.call(self.runtime("rt_concat"))
        a.add_imm(RSP, 8 * len(parts))

    def to_string(self, e) -> None:
        """Computes ``e`` and converts it to a String (`toString`). An enum member's
        string is its name."""
        t = self.type(e)
        enum = self.kind(t) == "enum"
        info = self.ints(t)
        if t not in (STRING, *_TO_STRING) and not enum and info is None:
            self.unsupported(e, f"converting {show(t)} to a string")
        self.expr(e)
        if info is not None:
            self.asm.call(self.runtime("rt_int_to_str" if info[1] else "rt_uint_to_str"))
        elif t in _TO_STRING:
            self.asm.call(self.runtime(_TO_STRING[t]))
        elif enum:
            names = "".join(m + "\0" for m in self.env.decl(t.sym).members)
            self.asm.lea_data(RSI, self.asm.data(names.encode("utf-8")))
            self.asm.call(self.runtime("rt_enum_name"))

    # --------------------------------------------------------------------- calls

    def call(self, e: A.Call) -> None:
        """Calls a routine; a function's result is left in rax."""
        a = self.asm
        m = self.calls.get(id(e))
        if m is not None and m.sig.kind == "record":
            return self.construct(e, m)
        if m is None and self.is_c(e, "cast"):
            self.expr(e.args[0].value)
            return self.normalize(self.type(e))  # truncates to a smaller integer type
        if m is None and len(e.args) == 1:
            sym = self.ref(_callee_span(e.callee))
            if sym is not None and sym.kind in (Kind.TYPE, Kind.BUILTIN_TYPE):
                return self.convert(e)
        if m is not None and m.sig.kind == "value":
            return self.call_value(e, m)
        if m is None or m.sig.kind != "routine" or m.sig.sym is None:
            self.unsupported(e, "this kind of call")
        sym = m.sig.sym
        if isinstance(sym.node, A.ExternDecl):
            if self.library.get(id(sym.node)) == "pst":
                return self.builtin(e, sym, m)
            return self.c_call(e, sym, m)
        if isinstance(sym.node, A.Signature):
            target, targs, sites = self.bound_member(e, m)
        else:
            target = sym
            targs, sites = self.instance(m.sig.tparams, m.s)
        if not isinstance(target.node, A.Routine):
            self.unsupported(e, f"calling {target.describe()}")
        if m.extra or len(m.params) > len(_ARG_REGS):
            self.unsupported(e, f"a call with more than {len(_ARG_REGS)} arguments")
        copies = []  # (place, temporary) of `var` arguments that aren't whole variables
        for p, slot in zip(m.params, m.slots):
            place = _unparen(slot.expr) if slot is not None and p.is_var else None
            if isinstance(place, A.Name):
                self.address(place)
            elif place is not None:
                if self.movable(place, [x.expr for x in m.slots if x is not None and x is not slot]):
                    self.address(place)  # moved out, so the callee's copy isn't shared
                    a.mov(RCX, RAX)
                    a.load(RAX, RCX)
                    a.store_imm(RCX, 0, 0)
                else:
                    self.expr(place)
                tmp = self.f.alloc(object())
                a.store(RBP, tmp, RAX)
                a.lea(RAX, RBP, tmp)
                copies.append((place, tmp))
            else:
                self.argument(p, slot, m, sym.file)
            a.push(RAX)
        for i in reversed(range(len(m.params))):
            a.pop(_ARG_REGS[i])
        a.call(self.label_of(e, target, targs, sites))
        if copies:
            a.push(RAX)
            for place, tmp in copies:
                self.address(place)
                a.load(RCX, RBP, tmp)
                self.store_at(place, self.type(place))
            a.pop(RAX)

    def call_value(self, e, m) -> None:
        """Calls a function value: `f(x)` with `f: function(T): U`."""
        a = self.asm
        if m.extra or len(m.params) > len(_ARG_REGS):
            self.unsupported(e, f"a call with more than {len(_ARG_REGS)} arguments")
        self.expr(e.callee)
        a.push(RAX)
        for p, slot in zip(m.params, m.slots):
            self.argument(p, slot, m, self.f.file)
            a.push(RAX)
        for i in reversed(range(len(m.params))):
            a.pop(_ARG_REGS[i])
        a.pop(R11)
        a.call_reg(R11)

    def movable(self, place, others: list) -> bool:
        """Whether a `var` argument that isn't a whole variable can be moved out of its
        place for the call, instead of copied: when it is managed, so a copy would be
        shared and the callee's changes would copy it again, and its variable isn't used
        anywhere else in the call (SPEC §6.3), so nothing sees the place empty."""
        if not self.managed(self.type(place)) or self.is_cplace(place):
            return False
        root = place
        while isinstance(root, (A.Paren, A.FieldAccess, A.Index)):
            root = root.inner if isinstance(root, A.Paren) else root.target
        sym = self.ref(root.span) if isinstance(root, A.Name) else None
        if sym is None:
            return False
        uses = 0
        todo = [place, *others]
        while todo:
            n = todo.pop()
            uses += isinstance(n, A.Name) and self.ref(n.span) is sym
            todo.extend(_children(n))
        return uses == 1

    def convert(self, e) -> None:
        """`Int32(x)`: converts between integer types, panicking (in checked builds) if
        the value doesn't fit."""
        a = self.asm
        src, dst = self.type(e.args[0].value), self.type(e)
        if not (self.ints(src) and self.ints(dst)):
            self.unsupported(e, f"converting {show(src)} to {show(dst)}")
        self.expr(e.args[0].value)
        if src == dst:
            return
        if self.checked and (src == _UINT64) != (dst == _UINT64) and (src == _UINT64 or not self.ints(dst)[1]):
            a.test(RAX, RAX)  # UInt64 above Int64's range, or a negative value to UInt64
            a.jcc("s", self.panic("P0002", "integer overflow", e.span))
        self.fit(dst, e.span)

    def c_call(self, e, sym: Symbol, m) -> None:
        """Calls a C function (SPEC §10.1) through the GOT, with the System V ABI. C records
        of up to 16 bytes are passed and returned in registers."""
        a = self.asm
        lib = self.library[id(sym.node)].encode()
        if lib not in self.needed:
            self.needed.append(lib)
        args = []  # (type, whether it is a C record passed by value)
        for p, slot in zip(m.params, m.slots):
            if p.varargs:
                continue
            t = self.sub(subst(p.type, m.s))
            place = _unparen(slot.expr) if slot is not None else None
            if p.is_var:
                self.cplace(place) if self.is_c_record(t) or self.is_cplace(place) else self.address(place)
            elif isinstance(t, FuncT):
                self.callback(place, t)
            else:
                self.argument(p, slot, m, sym.file)
            a.push(RAX)
            args.append((t, self.is_c_record(t) and not p.is_var))
        for x in m.extra:
            self.expr(x.expr)
            a.push(RAX)
            args.append((self.type(x.expr), False))
        regs, n = 0, len(args)
        for i, (t, by_value) in enumerate(args):
            words = (self.c_layout(t, e)[0] + 7) // 8 if by_value else 1
            if words > 2:
                self.unsupported(e, f"passing a C record of more than 16 bytes")
            if regs + words > len(_ARG_REGS):
                self.unsupported(e, f"a C call needing more than {len(_ARG_REGS)} registers")
            a.load(_ARG_REGS[regs], RSP, 8 * (n - 1 - i))
            if by_value:
                a.mov(R11, _ARG_REGS[regs])
                for w in range(words):
                    a.load(_ARG_REGS[regs + w], R11, _FIELDS + 8 * w)
            regs += words
        self.ccall(sym.node.symbol or sym.name)
        for i, (t, by_value) in enumerate(args):
            if by_value:
                a.load(RDI, RSP, 8 * (n - 1 - i))
                self.release(RDI)
        if n:
            a.add_imm(RSP, 8 * n)
        result = self.sub(subst(m.result, m.s))
        if self.is_c_record(result):
            size = self.c_layout(result, e)[0]
            if size > 16:
                self.unsupported(e, "returning a C record of more than 16 bytes")
            a.push(RDX)
            a.push(RAX)
            self.new_c_block(result, e)
            a.push(RAX)
            a.lea(RDI, RAX, _FIELDS)
            a.lea(RSI, RSP, 8)
            a.mov_imm(RDX, size)
            self.ccall("memcpy")
            a.pop(RAX)
            a.add_imm(RSP, 16)
        elif m.sig.is_function:
            self.normalize(result)  # C leaves the upper bits of small results undefined

    def callback(self, e, t: FuncT) -> None:
        """rax := the address of a thunk through which C calls the routine named by ``e``
        (SPEC §10.3)."""
        sym = self.ref(_callee_span(e)) if isinstance(e, (A.Name, A.FieldAccess)) else None
        if sym is None or not isinstance(sym.node, A.Routine) or self.env.sig(sym).tparams:
            self.unsupported(e, "a callback other than the name of a top-level routine")
        key = ("thunk", sym)
        if key not in self._labels:
            for p in t.params:
                if self.managed(p):
                    self.unsupported(e, f"a callback taking {show(p)}")
            self._labels[key] = self.asm.new_label("thunk")
            self._thunks.append((self._labels[key], self.label_of(e, sym), t.params))
        self.asm.lea_code(RAX, self._labels[key])

    def thunk(self, label: str, target: str, params: tuple) -> None:
        """C passes small integers with undefined upper bits: extend them, then go on to
        the routine."""
        self.asm.label(label)
        for reg, t in zip(_ARG_REGS, params):
            self.normalize(t, reg)
        self.asm.jmp(target)

    def instance(self, tparams: list, s: dict) -> tuple[tuple, dict]:
        """The type arguments of a call to a generic routine, and for each the file whose
        visibility resolves its bound members: where it became concrete (SPEC §8.2)."""
        targs, sites = [], {}
        for tp in tparams:
            outer = [p for p in params_in(s.get(tp)) if p in self.f.sites]
            sites[tp] = self.f.sites[outer[0]] if outer else self.f.file
            targs.append(self.sub(s.get(tp)))
        return tuple(targs), sites

    def bound_member(self, e, m) -> tuple:
        """The routine called by `x.area()` for `x: T` with `T: Measurable`, in this
        instance: found where `T` became concrete (SPEC §8.2). Returns it with its own
        type arguments and sites."""
        member = m.sig.sym
        this = m.pre.get(THIS)
        sub = {p: self.sub(t) for p, t in m.pre.items()}
        site = self.f.sites.get(this.sym, self.f.file) if this is not None else self.f.file
        found = self.env._checkers[site].provider(sub.get(THIS), member, sub, {})
        if found is None:
            self.unsupported(e, f"calling '{member.name}' here")
        f, sf = found
        tparams = self.env.sig(f).tparams
        return f, tuple(sf[tp] for tp in tparams), {tp: site for tp in tparams}

    def argument(self, p, slot, m, file: str) -> None:
        """Computes an argument (or a record field) into rax: the one given, or the
        parameter's default, which belongs to the declaring unit."""
        want = self.sub(subst(p.type, m.s))
        if slot is not None:
            return self.coerce(slot.expr, want)
        outer = self.f.file
        self.f.file = file
        try:
            self.coerce(p.default, want)
        finally:
            self.f.file = outer

    def sys_call(self, e, name: str, args: list, targs: tuple) -> None:
        """Calls the System routine ``name`` (not overloaded), instantiated for ``targs``,
        with ``args``: (expression, parameter type) in parameter order."""
        sym = self.env.sys(name)
        for x, t in args:
            self.coerce(x, t)
            self.asm.push(RAX)
        for i in reversed(range(len(args))):
            self.asm.pop(_ARG_REGS[i])
        sites = {tp: self.f.file for tp in self.env.sig(sym).tparams}
        self.asm.call(self.label_of(e, sym, targs, sites))

    def construct(self, e, m) -> None:
        """`Point(x: 1, y: 2)`: the field values are pushed, then moved into a new block."""
        a = self.asm
        t = self.sub(subst(m.result, m.s))
        self.value_type(t, e)
        for p, slot in zip(m.params, m.slots):
            self.argument(p, slot, m, m.sig.sym.file)
            a.push(RAX)
        if self.is_c_record(t):
            self.new_c_block(t, e)
            for name, ft, offset in reversed(self.c_struct(t, e)[0]):
                a.pop(RCX)
                a.push(RAX)
                a.add_imm(RAX, _FIELDS + offset)
                self.store_c(ft, e)
                a.pop(RAX)
            return
        self.new_block(self.descriptor(t, [ft for _, ft in self.fields(t)]))
        for i in reversed(range(len(m.params))):
            a.pop(RCX)
            a.store(RAX, _FIELDS + 8 * i, RCX)

    def builtin(self, e, sym: Symbol, m) -> None:
        """Routines declared `extern "pst"`: implemented by the compiler."""
        a = self.asm
        args = [s.expr if s is not None else None for s in m.slots]
        name = (sym.unit, sym.name)
        if name in (("IO", "write"), ("IO", "writeln")):
            parts = [] if not args else _parts(args[0])
            self.string_of(parts + (["\n"] if sym.name == "writeln" else []))
            a.mov(RDI, RAX)
            a.call(self.runtime("rt_write_str"))
        elif name == ("System", "toString"):
            self.to_string(args[0])
        elif name == ("System", "ord") and (self.type(args[0]) == CHAR or self.kind(self.type(args[0])) == "enum"):
            self.expr(args[0])
        elif name == ("System", "halt"):
            self.expr(args[0])
            a.mov(RDI, RAX)
            self.exit()
        elif name in (("System", "inc"), ("System", "dec")):
            t = self.sub(m.s.get(m.sig.tparams[0])) if m.sig.tparams else None
            if not self.ints(t):
                self.unsupported(e, f"'{sym.name}' of {show(t)}")
            if len(args) > 1 and args[1] is not None:
                self.expr(args[1])
            else:
                a.mov_imm(RAX, 1)
            a.push(RAX)
            self.address(args[0])
            a.pop(RCX)
            a.push(RAX)
            a.push(RCX)
            self.load_at(args[0], t)
            a.pop(RCX)
            self.arith("+" if sym.name == "inc" else "-", t, e.span)
            a.mov(RCX, RAX)
            a.pop(RAX)
            self.store_at(args[0], t)
        elif name == ("System", "len") and _is_list(self.type(args[0])):
            self.expr(args[0])
            a.push(RAX)
            a.load(RAX, RAX, _COUNT)
            self.drop(1)
        elif name == ("System", "decodeUtf8At"):
            self.expr(args[0])
            a.push(RAX)
            self.expr(args[1])
            a.load(RDI, RSP)
            a.mov(RSI, RAX)
            a.call(self.runtime("rt_decode_utf8"))
            self.drop(1)
        elif name == ("System", "utf8Width"):
            self.expr(args[0])
            a.mov(RCX, RAX)
            a.mov_imm(RAX, 1)
            for top in (0x80, 0x800, 0x10000):
                done = a.new_label("w")
                a.cmp_imm(RCX, top)
                a.jcc("b", done)
                a.inc(RAX)
                a.label(done)
        elif name == ("System", "getFailed"):
            self.string_of(["panic P0005: ", args[0], f"\n    at {self.where(e.span)}\n"])
            a.lea(RSI, RAX, _BYTES)
            a.load(RDX, RAX, _LEN)
            a.mov_imm(RDI, 2)
            a.call(self.runtime("rt_write"))
            a.mov_imm(RDI, _PANIC_STATUS)
            self.exit()
        elif name == ("System", "resize"):
            self.expr(args[1])
            a.push(RAX)
            self.address(args[0])
            a.mov(RDI, RAX)
            a.pop(RSI)
            a.call(self.runtime("rt_list_resize"))
        elif name == ("C", "toCString"):
            if id(e) not in self.f.keep:
                self.unsupported(e, "'toCString' here")
            slot = self.f.keep[id(e)]
            self.expr(args[0])
            a.load(RDI, RBP, slot)
            self.release(RDI)  # the string of the previous time round a loop
            a.store(RBP, slot, RAX)
            if slot not in self.f.scopes[-1]:
                self.f.scopes[-1].append(slot)
            a.add_imm(RAX, _BYTES)
        elif name == ("C", "isNull"):
            self.expr(args[0])
            a.test(RAX, RAX)
            a.setcc("e", RAX)
            a.movzx8(RAX, RAX)
        elif name == ("C", "fromCString"):
            self.expr(args[0])
            a.test(RAX, RAX)
            a.jcc("e", self.panic("P0007", "null C string", e.span))
            a.mov(RDI, RAX)
            a.call(self.runtime("rt_from_cstring"))
        elif name == ("C", "addr"):
            place = _unparen(args[0])
            if self.is_c_record(self.type(place)) or self.is_cplace(place):
                self.cplace(place)
            else:
                self.address(place)
        elif sym.name in ("byteLen", "charCount") and sym.unit in ("System", "Strings"):
            self.expr(args[0])
            a.push(RAX)
            a.load(RAX, RAX, _LEN if sym.name == "byteLen" else _CHARS)
            self.drop(1)
        elif name == ("Strings", "startsWith"):
            self.expr(args[0])
            a.push(RAX)
            self.expr(args[1])
            a.push(RAX)
            a.load(RDI, RSP, 8)
            a.mov(RSI, RAX)
            a.call(self.runtime("rt_starts_with"))
            self.drop(2)
        elif name == ("Strings", "slice"):
            self.slice(e, args)
        else:
            self.unsupported(e, f"'{sym.name}'")

    def slice(self, e, args) -> None:
        """`slice(s, start, stop)`: offsets out of range panic with `P0001` (in checked
        builds), offsets inside a character with `P0006`."""
        a = self.asm
        for arg in args:
            self.expr(arg)
            a.push(RAX)
        a.pop(RDX)  # stop
        a.pop(RSI)  # start
        a.load(RDI, RSP)  # the string stays on the stack until released
        if self.checked:
            bounds = self.panic("P0001", "slice offsets out of bounds", e.span)
            a.cmp(RSI, RDX)  # unsigned: a negative start is out of range too
            a.jcc("a", bounds)
            a.load(RCX, RDI, _LEN)
            a.cmp(RDX, RCX)
            a.jcc("a", bounds)
        inside = self.panic("P0006", "slice offset is not on a character boundary", e.span)
        for reg in (RSI, RDX):
            ok = a.new_label("ok")
            a.load(RCX, RDI, _LEN)
            a.cmp(reg, RCX)
            a.jcc("ae", ok)  # the end, or out of range in an unchecked build
            a.mov(RCX, RDI)
            a.add(RCX, reg)
            a.load8(RCX, RCX, _BYTES)
            a.and32_imm(RCX, 0xC0)
            a.cmp_imm(RCX, 0x80)  # a continuation byte
            a.jcc("e", inside)
            a.label(ok)
        a.call(self.runtime("rt_slice"))
        self.drop(1)

    # ----------------------------------------------------------------- runtime
    # Runtime routines are emitted once, after the program, if something uses them.
    # They call libc for memory, copying, comparing, number formatting and I/O.

    def runtime(self, label: str) -> str:
        """Marks the runtime routine ``label`` (a method of this class) as used."""
        self._runtime.setdefault(label, getattr(self, label))
        return label

    def emit_runtime(self) -> None:
        done = set()
        while len(done) < len(self._runtime):  # a routine may use another
            for label, emitter in list(self._runtime.items()):
                if label not in done:
                    done.add(label)
                    self.asm.label(label)
                    emitter()

    def ccall(self, name: str) -> None:
        """Calls a libc function, with the stack aligned as the C ABI requires (and al = 0
        for variadic ones)."""
        a = self.asm
        a.push(RBP)
        a.mov(RBP, RSP)
        a.and_imm(RSP, -16)
        a.xor32(RAX, RAX)
        a.call_import(name)
        a.leave()

    def exit(self) -> None:
        """Ends the program with status rdi. `exit`, not `_exit`: C code may have output
        waiting in stdio buffers."""
        self.ccall("exit")

    def rt_write(self) -> None:
        """Writes rdx bytes at rsi to file descriptor rdi, retrying partial writes. On an
        error (a closed pipe), exits with status 1."""
        a = self.asm
        loop, done, failed = a.new_label("w"), a.new_label("w"), a.new_label("w")
        for r in (RBX, R12, R13):
            a.push(r)
        a.mov(RBX, RDI)
        a.mov(R12, RSI)
        a.mov(R13, RDX)
        a.label(loop)
        a.test(R13, R13)
        a.jcc("e", done)
        a.mov(RDI, RBX)
        a.mov(RSI, R12)
        a.mov(RDX, R13)
        self.ccall("write")
        a.test(RAX, RAX)
        a.jcc("le", failed)
        a.add(R12, RAX)
        a.sub(R13, RAX)
        a.jmp(loop)
        a.label(failed)
        a.mov_imm(RDI, 1)
        self.exit()
        a.label(done)
        for r in (R13, R12, RBX):
            a.pop(r)
        a.ret()

    def rt_write_str(self) -> None:
        """Writes the string rdi to stdout and releases it."""
        a = self.asm
        a.push(RDI)
        a.lea(RSI, RDI, _BYTES)
        a.load(RDX, RDI, _LEN)
        a.mov_imm(RDI, 1)
        a.call(self.runtime("rt_write"))
        self.drop(1)
        a.ret()

    def rt_panic(self) -> None:
        """Writes the message (rsi, rdx) to stderr and exits with status 70."""
        a = self.asm
        a.mov_imm(RDI, 2)
        a.call(self.runtime("rt_write"))
        a.mov_imm(RDI, _PANIC_STATUS)
        self.exit()

    def rt_retain(self) -> None:
        """Adds a reference to the block rax (not to literals). Preserves registers."""
        a = self.asm
        done = a.new_label("r")
        a.cmp_mem_imm(RAX, _RC, 0)
        a.jcc("l", done)
        a.inc_mem(RAX, _RC)
        a.label(done)
        a.ret()

    def walk(self, block: int, desc: int, count: int, vals: int, step: int) -> None:
        """Sets up a loop over the slots of ``block``, whose descriptor is in ``desc``:
        ``count`` := how many, ``vals`` := the first, ``desc`` := its kind word (the next
        is ``step`` bytes on: 8 for a record's fields, 0 for a list's elements)."""
        a = self.asm
        go = a.new_label("k")
        a.load(count, desc, _D_COUNT)
        a.add_imm(desc, _D_KINDS)
        a.lea(vals, block, _FIELDS)
        a.mov_imm(step, 8)
        a.cmp_imm(count, _LIST)
        a.jcc("ne", go)
        a.load(count, block, _COUNT)
        a.lea(vals, block, _ELEMS)
        a.xor32(step, step)
        a.label(go)

    def rt_release(self) -> None:
        """Drops a reference to the block rdi (if not 0). With the last one, releases the
        references its fields (or elements) hold and frees it. Preserves registers."""
        a = self.asm
        loop, skip, free, done = (a.new_label("r") for _ in range(4))
        a.test(RDI, RDI)  # an empty slot (toCString's, or a moved-out value)
        a.jcc("e", done)
        a.cmp_mem_imm(RDI, _RC, 0)
        a.jcc("l", done)
        a.dec_mem(RDI, _RC)
        a.jcc("ne", done)
        for r in _CALLER_SAVED:
            a.push(r)
        a.mov(RSI, RDI)  # the block
        a.load(RDX, RSI, _DESC)
        a.test(RDX, RDX)
        a.jcc("e", free)  # a string
        self.walk(RSI, RDX, RCX, R8, R9)
        a.label(loop)  # rcx slots left; rdx their kinds, r8 their values
        a.test(RCX, RCX)
        a.jcc("e", free)
        a.load(RAX, RDX)
        a.test(RAX, RAX)
        a.jcc("e", skip)
        a.load(RDI, R8)
        a.call(self.runtime("rt_release"))
        a.label(skip)
        a.add(RDX, R9)
        a.add_imm(R8, 8)
        a.dec(RCX)
        a.jmp(loop)
        a.label(free)
        a.mov(RDI, RSI)
        self.ccall("free")
        for r in reversed(_CALLER_SAVED):
            a.pop(r)
        a.label(done)
        a.ret()

    def rt_new(self) -> None:
        """rax := a new block with descriptor rdi and refcount 1. Its fields are the
        caller's to fill in."""
        a = self.asm
        a.push(RDI)
        a.load(RDI, RDI, _D_SIZE)
        a.call(self.runtime("rt_alloc"))
        a.pop(RCX)
        a.store_imm(RAX, _RC, 1)
        a.store(RAX, _DESC, RCX)
        a.ret()

    def rt_list_new(self) -> None:
        """rax := a new list block with descriptor rdi and room for rsi elements, which
        it holds: the caller fills them in."""
        a = self.asm
        a.push(RDI)
        a.push(RSI)
        a.mov(RDI, RSI)
        a.shl_imm(RDI, 3)
        a.add_imm(RDI, _ELEMS)
        a.call(self.runtime("rt_alloc"))
        a.pop(RSI)
        a.pop(RCX)
        a.store_imm(RAX, _RC, 1)
        a.store(RAX, _DESC, RCX)
        a.store(RAX, _COUNT, RSI)
        a.store(RAX, _CAP, RSI)
        a.ret()

    def rt_list_resize(self) -> None:
        """rdi is the address of a variable holding a list; rsi a length (not negative).
        Makes the list unique and gives it rsi elements: dropped ones are released, new
        ones are 0, for the caller to fill in. Growing at least doubles the capacity."""
        a = self.asm
        big, fits, shrink, loop, done = (a.new_label("z") for _ in range(5))
        saved = (RBX, R12, R13, R14)
        for r in saved:
            a.push(r)
        a.mov(RBX, RDI)  # the variable
        a.mov(R12, RSI)  # the new length
        a.call(self.runtime("rt_unique"))
        a.mov(R13, RAX)  # the list
        a.load(RCX, R13, _CAP)
        a.cmp(R12, RCX)
        a.jcc("le", fits)
        a.mov(R14, RCX)
        a.add(R14, RCX)
        a.cmp(R14, R12)
        a.jcc("ge", big)
        a.mov(R14, R12)
        a.label(big)  # r14 := max(2 * cap, length)
        a.mov(RDI, R13)
        a.mov(RSI, R14)
        a.shl_imm(RSI, 3)
        a.add_imm(RSI, _ELEMS)
        self.ccall("realloc")
        a.test(RAX, RAX)
        a.jcc("e", self.runtime("rt_oom"))
        a.mov(R13, RAX)
        a.store(R13, _CAP, R14)
        a.store(RBX, 0, R13)
        a.label(fits)
        a.load(RCX, R13, _COUNT)
        a.cmp(R12, RCX)
        a.jcc("l", shrink)
        a.mov(RDI, RCX)  # zero the new elements
        a.shl_imm(RDI, 3)
        a.add(RDI, R13)
        a.add_imm(RDI, _ELEMS)
        a.mov(RDX, R12)
        a.sub(RDX, RCX)
        a.shl_imm(RDX, 3)
        a.xor32(RSI, RSI)
        self.ccall("memset")
        a.jmp(done)
        a.label(shrink)
        a.load(RDX, R13, _DESC)
        a.load(RDX, RDX, _D_KINDS)
        a.test(RDX, RDX)
        a.jcc("e", done)
        a.mov(R14, R12)
        a.label(loop)  # release elements r14 up to the old count
        a.cmp(R14, RCX)
        a.jcc("ge", done)
        a.mov(RAX, R14)
        a.shl_imm(RAX, 3)
        a.add(RAX, R13)
        a.load(RDI, RAX, _ELEMS)
        self.release(RDI)
        a.inc(R14)
        a.jmp(loop)
        a.label(done)
        a.store(R13, _COUNT, R12)
        for r in reversed(saved):
            a.pop(r)
        a.ret()

    def rt_unique(self) -> None:
        """rdi is the address of a variable holding a record or list. If it is shared,
        the variable gets a copy of its own. rax := the block, now unshared."""
        a = self.asm
        copy, lst, copied, loop, skip, done = (a.new_label("u") for _ in range(6))
        a.load(RAX, RDI)
        a.cmp_mem_imm(RAX, _RC, 1)
        a.jcc("ne", copy)
        a.ret()
        a.label(copy)
        for r in (RBX, R12, R13):
            a.push(r)
        a.mov(RBX, RDI)  # the variable
        a.mov(R12, RAX)  # the shared block
        a.load(RDI, RAX, _DESC)
        a.cmp_mem_imm(RDI, _D_COUNT, _LIST)
        a.jcc("e", lst)
        a.call(self.runtime("rt_new"))
        a.mov(R13, RAX)  # the copy
        a.lea(RDI, R13, _FIELDS)
        a.lea(RSI, R12, _FIELDS)
        a.load(RDX, R12, _DESC)
        a.load(RDX, RDX, _D_SIZE)
        a.sub_imm(RDX, _FIELDS)
        self.ccall("memcpy")
        a.jmp(copied)
        a.label(lst)  # the same capacity; the count and the elements
        a.load(RSI, R12, _CAP)
        a.call(self.runtime("rt_list_new"))
        a.mov(R13, RAX)
        a.lea(RDI, R13, _COUNT)
        a.lea(RSI, R12, _COUNT)
        a.load(RDX, R12, _COUNT)
        a.shl_imm(RDX, 3)
        a.add_imm(RDX, _ELEMS - _COUNT)
        self.ccall("memcpy")
        a.label(copied)
        a.load(RDX, R13, _DESC)
        self.walk(R13, RDX, RCX, R8, R9)
        a.label(loop)  # the copy holds new references to what the slots refer to
        a.test(RCX, RCX)
        a.jcc("e", done)
        a.load(RAX, RDX)
        a.test(RAX, RAX)
        a.jcc("e", skip)
        a.load(RAX, R8)
        self.retain()
        a.label(skip)
        a.add(RDX, R9)
        a.add_imm(R8, 8)
        a.dec(RCX)
        a.jmp(loop)
        a.label(done)
        self.release(R12)  # not the last reference: the block was shared
        a.store(RBX, 0, R13)
        a.mov(RAX, R13)
        for r in (R13, R12, RBX):
            a.pop(r)
        a.ret()

    def rt_eq(self) -> None:
        """rax := whether the blocks rdi and rsi hold equal values: the same string, or
        records (union values, lists) of the same descriptor whose slots are equal."""
        a = self.asm
        record, same, loop, plain, nxt, yes, no, done = (a.new_label("q") for _ in range(8))
        saved = (RBX, R12, R13, R14, R15)
        for r in saved:
            a.push(r)
        a.load(RAX, RDI, _DESC)
        a.load(RCX, RSI, _DESC)
        a.cmp(RAX, RCX)
        a.jcc("ne", no)
        a.test(RAX, RAX)
        a.jcc("ne", record)
        a.call(self.runtime("rt_str_cmp"))
        a.test(RAX, RAX)
        a.jcc("ne", no)
        a.jmp(yes)
        a.label(record)
        a.mov(R13, RAX)
        self.walk(RDI, R13, R14, RBX, R15)
        a.load(RCX, RAX, _D_COUNT)
        a.cmp_imm(RCX, _LIST)
        a.jcc("ne", same)
        a.load(RCX, RSI, _COUNT)  # lists: the same length
        a.cmp(R14, RCX)
        a.jcc("ne", no)
        a.label(same)
        a.mov(R12, RBX)  # the same slot of the other block
        a.sub(R12, RDI)
        a.add(R12, RSI)
        a.label(loop)  # r14 slots left; r13 their kinds; rbx, r12 the two values
        a.test(R14, R14)
        a.jcc("e", yes)
        a.load(RDI, RBX)
        a.load(RSI, R12)
        a.load(RAX, R13)
        a.test(RAX, RAX)
        a.jcc("e", plain)
        a.call(self.runtime("rt_eq"))
        a.test(RAX, RAX)
        a.jcc("e", no)
        a.jmp(nxt)
        a.label(plain)
        a.cmp(RDI, RSI)
        a.jcc("ne", no)
        a.label(nxt)
        a.add_imm(RBX, 8)
        a.add_imm(R12, 8)
        a.add(R13, R15)
        a.dec(R14)
        a.jmp(loop)
        a.label(yes)
        a.mov_imm(RAX, 1)
        a.jmp(done)
        a.label(no)
        a.xor32(RAX, RAX)
        a.label(done)
        for r in reversed(saved):
            a.pop(r)
        a.ret()

    def rt_enum_name(self) -> None:
        """rax := the name of enum member rax, from the NUL-separated names at rsi."""
        a = self.asm
        loop, found = a.new_label("n"), a.new_label("n")
        a.push(RBX)
        a.push(R12)
        a.mov(RBX, RAX)
        a.mov(R12, RSI)
        a.label(loop)
        a.mov(RDI, R12)
        self.ccall("strlen")
        a.test(RBX, RBX)
        a.jcc("e", found)
        a.add(R12, RAX)
        a.inc(R12)
        a.dec(RBX)
        a.jmp(loop)
        a.label(found)
        a.mov(RDI, R12)
        a.mov(RSI, RAX)
        a.call(self.runtime("rt_str_from"))
        a.pop(R12)
        a.pop(RBX)
        a.ret()

    def rt_alloc(self) -> None:
        """rax := malloc(rdi). Running out of memory is fatal."""
        a = self.asm
        self.ccall("malloc")
        a.test(RAX, RAX)
        a.jcc("e", self.runtime("rt_oom"))
        a.ret()

    def rt_oom(self) -> None:
        a = self.asm
        message = b"panic: out of memory\n"
        a.lea_data(RSI, a.data(message))
        a.mov_imm(RDX, len(message))
        a.jmp(self.runtime("rt_panic"))

    def rt_str_new(self) -> None:
        """rax := a new string block of rdi bytes, refcount 1, NUL-terminated. The bytes
        and the char count are the caller's to fill in."""
        a = self.asm
        a.push(RBX)
        a.mov(RBX, RDI)
        a.add_imm(RDI, _BYTES + 1)
        a.call(self.runtime("rt_alloc"))
        a.store_imm(RAX, _RC, 1)
        a.store_imm(RAX, _DESC, 0)
        a.store(RAX, _LEN, RBX)
        a.lea(RCX, RAX, _BYTES)
        a.add(RCX, RBX)
        a.store8_imm(RCX, 0, 0)
        a.pop(RBX)
        a.ret()

    def rt_str_from(self) -> None:
        """rax := a new string holding the rsi UTF-8 bytes at rdi."""
        a = self.asm
        loop, done = a.new_label("f"), a.new_label("f")
        for r in (RBX, R12, R13):
            a.push(r)
        a.mov(RBX, RDI)
        a.mov(R12, RSI)
        a.mov(RDI, RSI)
        a.call(self.runtime("rt_str_new"))
        a.mov(R13, RAX)
        a.lea(RDI, RAX, _BYTES)
        a.mov(RSI, RBX)
        a.mov(RDX, R12)
        self.ccall("memcpy")
        a.xor32(RAX, RAX)  # chars: the bytes that don't continue a character
        a.label(loop)
        a.test(R12, R12)
        a.jcc("e", done)
        a.load8(RCX, RBX)
        a.inc(RBX)
        a.dec(R12)
        a.and32_imm(RCX, 0xC0)
        a.cmp_imm(RCX, 0x80)
        a.jcc("e", loop)
        a.inc(RAX)
        a.jmp(loop)
        a.label(done)
        a.store(R13, _CHARS, RAX)
        a.mov(RAX, R13)
        for r in (R13, R12, RBX):
            a.pop(r)
        a.ret()

    def rt_utf8_len(self) -> None:
        """rax := the length of the valid UTF-8 sequence at rdi, or 0 if there is none (no
        overlong forms or surrogates). A NUL never continues a sequence, so this doesn't
        read past the end of a C string. Uses rcx, rdx, r8-r11."""
        a = self.asm
        one, three, four, check, cont, bad = (a.new_label("u8") for _ in range(6))
        a.load8(RCX, RDI)
        a.cmp_imm(RCX, 0x80)
        a.jcc("b", one)
        a.mov_imm(R9, 0x80)  # the range of the second byte
        a.mov_imm(R10, 0xBF)
        a.mov_imm(R8, 2)
        a.cmp_imm(RCX, 0xC2)
        a.jcc("b", bad)
        a.cmp_imm(RCX, 0xE0)
        a.jcc("b", check)
        a.mov_imm(R8, 3)
        a.cmp_imm(RCX, 0xF0)
        a.jcc("b", three)
        a.mov_imm(R8, 4)
        a.cmp_imm(RCX, 0xF5)
        a.jcc("b", four)
        a.jmp(bad)
        for label, lead, low, high in ((three, 0xE0, 0xA0, 0x9F), (four, 0xF0, 0x90, 0x8F)):
            not_low = a.new_label("u8")
            a.label(label)
            a.cmp_imm(RCX, lead)
            a.jcc("ne", not_low)
            a.mov_imm(R9, low)  # overlong
            a.label(not_low)
            a.cmp_imm(RCX, lead + 0x0D if lead == 0xE0 else lead + 4)  # ED: surrogates; F4: > U+10FFFF
            a.jcc("ne", check)
            a.mov_imm(R10, high)
            a.jmp(check)
        a.label(check)
        a.load8(RDX, RDI, 1)
        a.cmp(RDX, R9)
        a.jcc("b", bad)
        a.cmp(RDX, R10)
        a.jcc("a", bad)
        a.mov_imm(R11, 2)
        a.label(cont)  # r11: the next byte
        a.cmp(R11, R8)
        a.jcc("e", one)
        a.mov(RDX, RDI)
        a.add(RDX, R11)
        a.load8(RDX, RDX)
        a.and32_imm(RDX, 0xC0)
        a.cmp_imm(RDX, 0x80)
        a.jcc("ne", bad)
        a.inc(R11)
        a.jmp(cont)
        a.label(one)  # r8 = 1 isn't set for ASCII: return it here
        a.cmp_imm(RCX, 0x80)
        a.mov_imm(RAX, 1)
        a.jcc("b", done := a.new_label("u8"))
        a.mov(RAX, R8)
        a.label(done)
        a.ret()
        a.label(bad)
        a.xor32(RAX, RAX)
        a.ret()

    def rt_decode_utf8(self) -> None:
        """rax := the code point at byte offset rsi of the string rdi (valid UTF-8, and
        rsi on a character boundary)."""
        a = self.asm
        two, three, more, done = (a.new_label("d") for _ in range(4))
        a.add(RSI, RDI)
        a.load8(RAX, RSI, _BYTES)
        a.cmp_imm(RAX, 0x80)
        a.jcc("b", done)
        a.mov_imm(RDX, 1)  # continuation bytes
        a.cmp_imm(RAX, 0xE0)
        a.jcc("b", two)
        a.inc(RDX)
        a.cmp_imm(RAX, 0xF0)
        a.jcc("b", three)
        a.inc(RDX)
        a.and32_imm(RAX, 0x07)
        a.jmp(more)
        a.label(three)
        a.and32_imm(RAX, 0x0F)
        a.jmp(more)
        a.label(two)
        a.and32_imm(RAX, 0x1F)
        a.label(more)
        a.shl_imm(RAX, 6)
        a.inc(RSI)
        a.load8(RCX, RSI, _BYTES)
        a.and32_imm(RCX, 0x3F)
        a.or_(RAX, RCX)
        a.dec(RDX)
        a.jcc("ne", more)
        a.label(done)
        a.ret()

    def rt_from_cstring(self) -> None:
        """rax := a new string copied from the C string rdi. Each byte that isn't part of
        valid UTF-8 becomes U+FFFD."""
        a = self.asm
        saved = (RBX, R12, R13, R14, R15)
        for r in saved:
            a.push(r)
        a.mov(RBX, RDI)
        a.xor32(R12, R12)  # bytes
        a.xor32(R13, R13)  # chars
        a.mov(R14, RDI)
        count, invalid, alloc = (a.new_label("fc") for _ in range(3))
        a.label(count)
        a.load8(RCX, R14)
        a.test(RCX, RCX)
        a.jcc("e", alloc)
        a.inc(R13)
        a.mov(RDI, R14)
        a.call(self.runtime("rt_utf8_len"))
        a.test(RAX, RAX)
        a.jcc("e", invalid)
        a.add(R12, RAX)
        a.add(R14, RAX)
        a.jmp(count)
        a.label(invalid)
        a.add_imm(R12, 3)
        a.inc(R14)
        a.jmp(count)
        a.label(alloc)
        a.mov(RDI, R12)
        a.call(self.runtime("rt_str_new"))
        a.store(RAX, _CHARS, R13)
        a.mov(R15, RAX)
        a.lea(R13, RAX, _BYTES)  # where the next byte goes
        a.mov(R14, RBX)
        copy, byte, replace, done = (a.new_label("fc") for _ in range(4))
        a.label(copy)
        a.load8(RCX, R14)
        a.test(RCX, RCX)
        a.jcc("e", done)
        a.mov(RDI, R14)
        a.call(self.runtime("rt_utf8_len"))
        a.test(RAX, RAX)
        a.jcc("e", replace)
        a.label(byte)
        a.load8(RDX, R14)
        a.store8(R13, 0, RDX)
        a.inc(R14)
        a.inc(R13)
        a.dec(RAX)
        a.jcc("ne", byte)
        a.jmp(copy)
        a.label(replace)
        for i, b in enumerate(b"\xef\xbf\xbd"):
            a.store8_imm(R13, i, b)
        a.add_imm(R13, 3)
        a.inc(R14)
        a.jmp(copy)
        a.label(done)
        a.mov(RAX, R15)
        for r in reversed(saved):
            a.pop(r)
        a.ret()

    def rt_concat(self) -> None:
        """rax := the concatenation of the strings in [rdi, rsi), last to first (the stack
        order of pushed parts). Releases the parts."""
        a = self.asm
        saved = (RBX, R12, R13, R14, R15)
        for r in saved:
            a.push(r)
        a.mov(R12, RDI)  # first slot (the last part)
        a.mov(R13, RSI)  # end (just past the first part)
        a.xor32(R14, R14)  # bytes
        a.xor32(R15, R15)  # chars
        total, alloc = a.new_label("c"), a.new_label("c")
        a.mov(RBX, R13)
        a.label(total)
        a.cmp(RBX, R12)
        a.jcc("e", alloc)
        a.sub_imm(RBX, 8)
        a.load(RAX, RBX)
        a.load(RCX, RAX, _LEN)
        a.add(R14, RCX)
        a.load(RCX, RAX, _CHARS)
        a.add(R15, RCX)
        a.jmp(total)
        a.label(alloc)
        a.mov(RDI, R14)
        a.call(self.runtime("rt_str_new"))
        a.store(RAX, _CHARS, R15)
        a.mov(R14, RAX)  # the result
        a.lea(R15, RAX, _BYTES)  # where the next part goes
        copy, done = a.new_label("c"), a.new_label("c")
        a.mov(RBX, R13)
        a.label(copy)
        a.cmp(RBX, R12)
        a.jcc("e", done)
        a.sub_imm(RBX, 8)
        a.load(RSI, RBX)
        a.load(RDX, RSI, _LEN)
        a.add_imm(RSI, _BYTES)
        a.mov(RDI, R15)
        a.add(R15, RDX)
        self.ccall("memcpy")
        a.load(RDI, RBX)
        self.release(RDI)
        a.jmp(copy)
        a.label(done)
        a.mov(RAX, R14)
        for r in reversed(saved):
            a.pop(r)
        a.ret()

    def rt_int_to_str(self, fmt: bytes = b"%ld\0") -> None:
        """rax := the decimal string of the integer rax."""
        a = self.asm
        a.sub_imm(RSP, 32)
        a.mov(RCX, RAX)
        a.mov(RDI, RSP)
        a.mov_imm(RSI, 32)
        a.lea_data(RDX, a.data(fmt))
        self.ccall("snprintf")
        a.mov(RDI, RSP)
        a.mov(RSI, RAX)
        a.call(self.runtime("rt_str_from"))
        a.add_imm(RSP, 32)
        a.ret()

    def rt_uint_to_str(self) -> None:
        """rax := the decimal string of the unsigned integer rax."""
        self.rt_int_to_str(b"%lu\0")

    def rt_bool_to_str(self) -> None:
        """rax := "true" or "false"."""
        a = self.asm
        done = a.new_label("b")
        a.test(RAX, RAX)
        a.lea_data(RAX, self.literal("false"))  # lea keeps the flags
        a.jcc("e", done)
        a.lea_data(RAX, self.literal("true"))
        a.label(done)
        a.ret()

    def rt_char_to_str(self) -> None:
        """rax := the one-character string of the code point rax (UTF-8 encoded)."""
        a = self.asm
        sized, loop, lead = a.new_label("u"), a.new_label("u"), a.new_label("u")
        for n, prefix, limit in ((1, 0, 0x80), (2, 0xC0, 0x800), (3, 0xE0, 0x10000)):
            a.mov_imm(RSI, n)  # bytes
            a.mov_imm(RDX, prefix)  # bits of the first byte
            a.cmp_imm(RAX, limit)
            a.jcc("b", sized)
        a.mov_imm(RSI, 4)
        a.mov_imm(RDX, 0xF0)
        a.label(sized)
        a.sub_imm(RSP, 8)  # the bytes
        a.lea(RCX, RSP, -1)
        a.add(RCX, RSI)  # the last byte; continuation bytes are written backwards
        a.mov(RDI, RSI)
        a.label(loop)
        a.dec(RDI)
        a.jcc("e", lead)
        a.mov(R8, RAX)
        a.and32_imm(R8, 0x3F)
        a.or32_imm(R8, 0x80)
        a.store8(RCX, 0, R8)
        a.shr_imm(RAX, 6)
        a.dec(RCX)
        a.jmp(loop)
        a.label(lead)
        a.add(RDX, RAX)  # the prefix bits and the remaining value don't overlap
        a.store8(RCX, 0, RDX)
        a.mov(RDI, RSP)
        a.call(self.runtime("rt_str_from"))
        a.add_imm(RSP, 8)
        a.ret()

    def rt_str_cmp(self) -> None:
        """rax := -1, 0 or 1 as string rdi is before, equal to or after string rsi
        (bytewise, which is code point order for UTF-8)."""
        a = self.asm
        short, less, more, done = (a.new_label("s") for _ in range(4))
        a.push(RBX)
        a.push(R12)
        a.load(RBX, RDI, _LEN)
        a.load(R12, RSI, _LEN)
        a.mov(RDX, RBX)
        a.cmp(RDX, R12)
        a.jcc("be", short)
        a.mov(RDX, R12)
        a.label(short)  # rdx: the shorter length
        a.add_imm(RDI, _BYTES)
        a.add_imm(RSI, _BYTES)
        self.ccall("memcmp")
        a.test32(RAX, RAX)
        a.jcc("s", less)
        a.jcc("ne", more)
        a.cmp(RBX, R12)  # one is a prefix of the other
        a.jcc("b", less)
        a.jcc("a", more)
        a.xor32(RAX, RAX)
        a.jmp(done)
        a.label(less)
        a.mov_imm(RAX, -1)
        a.jmp(done)
        a.label(more)
        a.mov_imm(RAX, 1)
        a.label(done)
        a.pop(R12)
        a.pop(RBX)
        a.ret()

    def rt_starts_with(self) -> None:
        """rax := whether string rdi starts with string rsi."""
        a = self.asm
        no = a.new_label("p")
        a.load(RDX, RSI, _LEN)
        a.load(RCX, RDI, _LEN)
        a.cmp(RDX, RCX)
        a.jcc("a", no)
        a.add_imm(RDI, _BYTES)
        a.add_imm(RSI, _BYTES)
        self.ccall("memcmp")
        a.test32(RAX, RAX)
        a.jcc("ne", no)
        a.mov_imm(RAX, 1)
        a.ret()
        a.label(no)
        a.xor32(RAX, RAX)
        a.ret()

    def rt_slice(self) -> None:
        """rax := the bytes [rsi, rdx) of string rdi, as a new string. The offsets are
        checked by the caller."""
        a = self.asm
        a.sub(RDX, RSI)
        a.add_imm(RDI, _BYTES)
        a.add(RDI, RSI)
        a.mov(RSI, RDX)
        a.jmp(self.runtime("rt_str_from"))

def _is_pointer(t) -> bool:
    """`Ptr[T]`, `Pointer` or `CString`: an unmanaged address (SPEC §4.13)."""
    return t in (Prim("Pointer"), Prim("CString")) or (isinstance(t, Builtin) and t.name == "Ptr")


def _is_list(t) -> bool:
    return isinstance(t, Builtin) and t.name == "List"


def _is_ptr(t) -> bool:
    return isinstance(t, Builtin) and t.name == "Ptr"


def _head(t):
    """What a variant label names: a declared type's symbol, or a primitive's name."""
    return t.sym if isinstance(t, Named) else t.name if isinstance(t, Prim) else None


def _unparen(e):
    return _unparen(e.inner) if isinstance(e, A.Paren) else e


def _callee_span(c):
    """The span naming what a callee refers to: `f`, or the `f` of `Unit.f` or `f[T]`."""
    if isinstance(c, A.Index):
        return _callee_span(c.target)
    return c.name.span if isinstance(c, A.FieldAccess) else c.span


def _parts(arg) -> list:
    """The pieces of a `write` argument: a literal's text, an interpolation's parts, or
    the value."""
    if isinstance(arg, A.StringLit):
        return [arg.value]
    if isinstance(arg, A.InterpString):
        return list(arg.parts)
    return [arg]


def _children(node):
    """The direct sub-nodes of an AST node."""
    for value in vars(node).values():
        if isinstance(value, A.Node):
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, A.Node))

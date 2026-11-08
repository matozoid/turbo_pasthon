"""The emit pass: typed AST to an x86-64 executable (SPEC §15.2 steps 4–5).

The code generator grows construct by construct. Anything it can't compile yet is
reported as `E0990` at the construct, so a program either builds correctly or not at all.

Supported so far:
- values of type Integer, Boolean, Char, String, enums, and non-generic or concretely
  instantiated records and unions (so `Option[Integer]`, `Result[String, Integer]`):
  literals, constants, locals, parameters (`var` ones too), fields, construction,
  arithmetic with overflow checks, comparisons, `and`/`or`/`not`, `if` and `case`
  expressions, string interpolation;
- statements: `var`/`let`, assignment (to fields too), `if`, `case`, `while`, `repeat`,
  `for ... to/downto`, `break`, `continue`, `return`, `assert`, `_ := e`, calls;
- non-generic routines (up to 6 parameters), with defaults, named arguments, dot calls,
  and contracts;
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
outlives the statement that made it.

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
    BOOLEAN, CHAR, INTEGER, STRING, VOID, Named, Prim, TypeValue, has_error, show, subst,
)  # fmt: skip
from .x86 import (
    R8, R9, R10, R11, R12, R13, R14, R15, RAX, RBP, RBX, RCX, RDI, RDX, RSI, RSP, Assembler,
)  # fmt: skip

_PANIC_STATUS = 70
_ARG_REGS = (RDI, RSI, RDX, RCX, R8, R9)
_CALLER_SAVED = (RAX, RCX, RDX, RSI, RDI, R8, R9, R10, R11)
_DECLS = (A.TypeSection, A.ConstSection, A.Routine, A.ExternBlock, A.TestBlock)
_VALUES = (INTEGER, BOOLEAN, CHAR, STRING)  # the types the code generator handles so far
_TO_STRING = {INTEGER: "rt_int_to_str", BOOLEAN: "rt_bool_to_str", CHAR: "rt_char_to_str"}
_COMPARE = {"=": "e", "<>": "ne", "<": "l", "<=": "le", ">": "g", ">=": "ge"}
# Heap block layout: the header, then a string's parts or a record's fields.
_RC, _DESC = 0, 8
_LEN, _CHARS, _BYTES = 16, 24, 32
_FIELDS = 16
# Descriptor layout: then one word per field, 1 if it holds a reference.
_D_COUNT, _D_SIZE, _D_ID, _D_KINDS = 0, 8, 16, 24


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
        self._labels: dict[Symbol, str] = {}  # routines -> label
        self._queue: list[Symbol] = []  # routines referenced but not emitted yet
        self._descs: dict[object, str] = {}  # record type, or (union, variant) -> label
        self.units = {u.path: u for u in [self.main, *result.units.values()]}
        # Expression types and resolved calls, from every unit's checker.
        self.types: dict[int, object] = {}
        self.calls: dict[int, object] = {}
        for c in [*self.env._checkers.values(), *self.env._const_checkers.values()]:
            self.types.update(c.types)
            self.calls.update(c.calls)
        self.f: _Frame | None = None

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
        return self.types.get(id(e))

    def kind(self, t) -> str | None:
        """"record", "union", "enum", ... for a declared type."""
        return self.env.kind_of(t)

    def managed(self, t) -> bool:
        """Whether values of type ``t`` are reference counted."""
        return t == STRING or self.kind(t) in ("record", "union")

    def supported(self, t, seen: frozenset = frozenset()) -> bool:
        if t in _VALUES or has_error(t) or t in seen:
            return True
        kind = self.kind(t)
        if kind == "enum":
            return True
        if kind not in ("record", "union") or any(not self.supported(a) for a in t.args):
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
        for scope in reversed(self.f.scopes[depth:]):
            for slot in reversed(scope):
                self.asm.load(RDI, RBP, slot)
                self.release(RDI)

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
        self.block(A.Block(self.main.tree.span, [
            i for i in self.main.tree.items if not isinstance(i, _DECLS)
        ]))  # fmt: skip
        a.label(self.f.exit)
        a.xor32(RDI, RDI)
        self.exit()
        a.patch32(size_at, elf.align(self.f.size, 16))
        while self._queue:
            self.routine(self._queue.pop())
        self.f = None
        for label, message in self._panics:
            a.label(label)
            a.lea_data(RSI, a.data(message))
            a.mov_imm(RDX, len(message))
            a.jmp(self.runtime("rt_panic"))
        self.emit_runtime()
        if self.diagnostics:
            return BuildResult(None, self.diagnostics)
        got = elf.got_offset(len(a.code), len(a.rodata), a.imports)
        code = a.finish(elf.rodata_offset(len(a.code)), got)
        exe = elf.executable(code, bytes(a.rodata), a.labels["_start"], a.imports)
        return BuildResult(exe)

    def label_of(self, sym: Symbol) -> str:
        if sym not in self._labels:
            self._labels[sym] = f"{sym.unit}.{sym.name}#{len(self._labels)}"
            self._queue.append(sym)
        return self._labels[sym]

    def routine(self, sym: Symbol) -> None:
        a = self.asm
        node: A.Routine = sym.node
        sig = self.env.sig(sym)
        self.f = f = _Frame(sym.file, sym, a.new_label("ret"))
        a.label(self._labels[sym])
        a.push(RBP)
        a.mov(RBP, RSP)
        size_at = a.sub_imm32_fixed(RSP)
        try:
            for i, p in enumerate(sig.params):
                if p.sym is None:
                    self.unsupported(node.sig, "this kind of parameter")
                self.value_type(p.type, node.sig.name, f"parameter '{p.name}'")
                slot = f.alloc(p.sym)
                a.store(RBP, slot, _ARG_REGS[i])
                if self.managed(p.type) and not p.is_var:
                    f.scopes[0].append(slot)
            if sig.is_function:
                self.value_type(sig.result, node.sig.result, "a result")
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
            self.value_type(sym.type if sym else None, s.name, f"variable '{s.name.name}'")
            self.coerce(s.value, sym.type)
            slot = self.local(s.name.span)
            a.store(RBP, slot, RAX)
            if self.managed(sym.type):
                self.f.scopes[-1].append(slot)
        elif isinstance(s, A.Assign):
            t = self.type(s.target)
            self.coerce(s.value, t)
            a.push(RAX)
            self.address(s.target)
            a.pop(RCX)
            if self.managed(t):
                a.load(RDI, RAX)
                self.release(RDI)
            a.store(RAX, 0, RCX)
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
        elif isinstance(s, A.Case):
            self.case(s.subject, s.arms, None)
        elif isinstance(s, (A.Break, A.Continue)):
            loop = self.f.loops[-1]
            self.release_scopes(loop.depth)
            a.jmp(loop.brk if isinstance(s, A.Break) else loop.cont)
        elif isinstance(s, A.Return):
            if s.value is not None:
                self.coerce(s.value, self.env.sig(self.f.sym).result)
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
        if sym is not None and sym.type not in (INTEGER, CHAR) and self.kind(sym.type) != "enum":
            self.unsupported(s.var, f"a loop over {show(sym.type)}")
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
        a.jcc("l" if s.down else "g", end)
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
                binding = self.ref(bound[0].binding.span)
                if self.managed(binding.type):
                    self.retain()
                b = self.local(bound[0].binding.span)
                a.store(RBP, b, RAX)
                if self.managed(binding.type):
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
            self.label_compare(t, slot, label.low)
            a.jcc("l", skip)
            self.label_compare(t, slot, label.high)
            a.jcc("le", target)
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
        if isinstance(e, A.FieldAccess) and self.kind(self.type(e.target)) == "record":
            names = [n for n, _ in self.fields(self.type(e.target))]
            self.address(e.target)
            self.asm.mov(RDI, RAX)
            self.asm.call(self.runtime("rt_unique"))
            self.asm.add_imm(RAX, _FIELDS + 8 * names.index(e.name.name))
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
        names = [n for n, _ in self.fields(t)]
        self.expr(e.target)
        self.asm.push(RAX)
        self.asm.load(RAX, RAX, _FIELDS + 8 * names.index(e.name.name))
        if self.managed(self.type(e)):
            self.retain()
        self.drop(1)

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
        if sym.kind is Kind.RESULT and self.f.result_slot is not None:
            a.load(RAX, RBP, self.f.result_slot)
        elif sym in self.f.slots:
            a.load(RAX, RBP, self.f.slots[sym])
            if self.is_var_param(sym):
                a.load(RAX, RAX)
        else:
            self.unsupported(e, f"using {sym.describe()} as a value")
        if self.managed(t):
            self.retain()

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
            a.neg(RAX)
            self.overflow(e.span)
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
            self.unsupported(e, "'in'")
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
            a.setcc(_COMPARE[op], RAX)
            a.movzx8(RAX, RAX)
        elif op in ("+", "-", "*"):
            {"+": a.add, "-": a.sub, "*": a.imul}[op](RAX, RCX)
            self.overflow(e.op_span)
        elif op in ("div", "mod"):
            self.divide(op, e.op_span)
        else:
            self.unsupported(e, f"'{op}'")

    def divide(self, op: str, span) -> None:
        """rax div/mod rcx, truncating toward zero (the remainder takes the sign of the
        dividend). Division by zero panics even in unchecked builds."""
        a = self.asm
        normal, done = a.new_label("div"), a.new_label("div")
        a.test(RCX, RCX)
        a.jcc("e", self.panic("P0004", "division by zero", span))
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
        if t not in (STRING, *_TO_STRING) and not enum:
            self.unsupported(e, f"converting {show(t)} to a string")
        self.expr(e)
        if t in _TO_STRING:
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
        if m is None or m.sig.kind != "routine" or m.sig.sym is None:
            self.unsupported(e, "this kind of call")
        sym = m.sig.sym
        if isinstance(sym.node, A.ExternDecl):
            return self.builtin(e, sym, m)
        if sym.node is None or not isinstance(sym.node, A.Routine):
            self.unsupported(e, f"calling {sym.describe()}")
        if m.sig.tparams:
            self.unsupported(e, f"calling generic '{sym.name}'")
        if m.extra or len(m.params) > len(_ARG_REGS):
            self.unsupported(e, f"a call with more than {len(_ARG_REGS)} arguments")
        copies = []  # (place, temporary) of `var` arguments that aren't whole variables
        for p, slot in zip(m.params, m.slots):
            place = _unparen(slot.expr) if slot is not None and p.is_var else None
            if isinstance(place, A.Name):
                self.address(place)
            elif place is not None:
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
        a.call(self.label_of(sym))
        if copies:
            a.push(RAX)
            for place, tmp in copies:
                self.address(place)
                if self.managed(self.type(place)):
                    a.load(RDI, RAX)
                    self.release(RDI)
                a.load(RCX, RBP, tmp)
                a.store(RAX, 0, RCX)
            a.pop(RAX)

    def argument(self, p, slot, m, file: str) -> None:
        """Computes an argument (or a record field) into rax: the one given, or the
        parameter's default, which belongs to the declaring unit."""
        want = subst(p.type, m.s)
        if slot is not None:
            return self.coerce(slot.expr, want)
        outer = self.f.file
        self.f.file = file
        try:
            self.coerce(p.default, want)
        finally:
            self.f.file = outer

    def construct(self, e, m) -> None:
        """`Point(x: 1, y: 2)`: the field values are pushed, then moved into a new block."""
        a = self.asm
        t = subst(m.sig.result, m.s)
        self.value_type(t, e)
        for p, slot in zip(m.params, m.slots):
            self.argument(p, slot, m, m.sig.sym.file)
            a.push(RAX)
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
            t = m.s.get(m.sig.tparams[0]) if m.sig.tparams else None
            if t != INTEGER:
                self.unsupported(e, f"'{sym.name}' of {show(t)}")
            if len(args) > 1 and args[1] is not None:
                self.expr(args[1])
            else:
                a.mov_imm(RAX, 1)
            a.push(RAX)
            self.address(args[0])
            a.pop(RCX)
            a.load(RDX, RAX)
            (a.add if sym.name == "inc" else a.sub)(RDX, RCX)
            self.overflow(e.span)
            a.store(RAX, 0, RDX)
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
        """Ends the program with status rdi."""
        self.ccall("_exit")

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

    def rt_release(self) -> None:
        """Drops a reference to the block rdi. With the last one, releases the references
        its fields hold and frees it. Preserves registers."""
        a = self.asm
        loop, skip, free, done = (a.new_label("r") for _ in range(4))
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
        a.load(RCX, RDX, _D_COUNT)
        a.add_imm(RDX, _D_KINDS)
        a.lea(R8, RSI, _FIELDS)
        a.label(loop)  # rcx fields left; rdx their kinds, r8 their values
        a.test(RCX, RCX)
        a.jcc("e", free)
        a.load(RAX, RDX)
        a.test(RAX, RAX)
        a.jcc("e", skip)
        a.load(RDI, R8)
        a.call(self.runtime("rt_release"))
        a.label(skip)
        a.add_imm(RDX, 8)
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

    def rt_unique(self) -> None:
        """rdi is the address of a variable holding a record. If the record is shared,
        the variable gets a copy of its own. rax := the record, now unshared."""
        a = self.asm
        copy, loop, skip, done = (a.new_label("u") for _ in range(4))
        a.load(RAX, RDI)
        a.cmp_mem_imm(RAX, _RC, 1)
        a.jcc("ne", copy)
        a.ret()
        a.label(copy)
        for r in (RBX, R12, R13):
            a.push(r)
        a.mov(RBX, RDI)  # the variable
        a.mov(R12, RAX)  # the shared record
        a.load(RDI, RAX, _DESC)
        a.call(self.runtime("rt_new"))
        a.mov(R13, RAX)  # the copy
        a.lea(RDI, R13, _FIELDS)
        a.lea(RSI, R12, _FIELDS)
        a.load(RDX, R12, _DESC)
        a.load(RDX, RDX, _D_SIZE)
        a.sub_imm(RDX, _FIELDS)
        self.ccall("memcpy")
        a.load(RDX, R13, _DESC)
        a.load(RCX, RDX, _D_COUNT)
        a.add_imm(RDX, _D_KINDS)
        a.lea(R8, R13, _FIELDS)
        a.label(loop)  # the copy holds new references to what the fields refer to
        a.test(RCX, RCX)
        a.jcc("e", done)
        a.load(RAX, RDX)
        a.test(RAX, RAX)
        a.jcc("e", skip)
        a.load(RAX, R8)
        self.retain()
        a.label(skip)
        a.add_imm(RDX, 8)
        a.add_imm(R8, 8)
        a.dec(RCX)
        a.jmp(loop)
        a.label(done)
        self.release(R12)  # not the last reference: the record was shared
        a.store(RBX, 0, R13)
        a.mov(RAX, R13)
        for r in (R13, R12, RBX):
            a.pop(r)
        a.ret()

    def rt_eq(self) -> None:
        """rax := whether the blocks rdi and rsi hold equal values: the same string, or
        records (or union values) of the same descriptor whose fields are equal."""
        a = self.asm
        record, loop, plain, nxt, yes, no, done = (a.new_label("q") for _ in range(7))
        saved = (RBX, R12, R13, R14)
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
        a.load(R14, RAX, _D_COUNT)
        a.lea(R13, RAX, _D_KINDS)
        a.lea(RBX, RDI, _FIELDS)
        a.lea(R12, RSI, _FIELDS)
        a.label(loop)  # r14 fields left; r13 their kinds; rbx, r12 the two values
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
        a.add_imm(R13, 8)
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
        oom = a.new_label("oom")
        self.ccall("malloc")
        a.test(RAX, RAX)
        a.jcc("e", oom)
        a.ret()
        a.label(oom)
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

    def rt_int_to_str(self) -> None:
        """rax := the decimal string of the integer rax."""
        a = self.asm
        a.sub_imm(RSP, 32)
        a.mov(RCX, RAX)
        a.mov(RDI, RSP)
        a.mov_imm(RSI, 32)
        a.lea_data(RDX, a.data(b"%ld\0"))
        self.ccall("snprintf")
        a.mov(RDI, RSP)
        a.mov(RSI, RAX)
        a.call(self.runtime("rt_str_from"))
        a.add_imm(RSP, 32)
        a.ret()

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

def _head(t):
    """What a variant label names: a declared type's symbol, or a primitive's name."""
    return t.sym if isinstance(t, Named) else t.name if isinstance(t, Prim) else None


def _unparen(e):
    return _unparen(e.inner) if isinstance(e, A.Paren) else e


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

"""The emit pass: typed AST to an x86-64 executable (SPEC §15.2 steps 4–5).

The code generator grows construct by construct. Anything it can't compile yet is
reported as `E0990` at the construct, so a program either builds correctly or not at all.

Supported so far:
- values of type Integer and Boolean: literals, constants, locals, parameters (`var` ones
  too), arithmetic with overflow checks, comparisons, `and`/`or`/`not`, `if` expressions;
- statements: `var`/`let`, assignment, `if`, `while`, `repeat`, `for ... to/downto`,
  `break`, `continue`, `return`, `assert`, `_ := e`, calls;
- non-generic routines (up to 6 parameters), with defaults, named arguments, dot calls,
  and contracts;
- built-ins: `write`/`writeln` (string literals, interpolation, Integer, Boolean),
  `halt`, `inc`, `dec`.

Scheme (SPEC §15.2: no IR, no optimizer): every expression leaves its value in rax;
binary operators push the left operand while the right one is computed. Each routine has
an rbp frame with one 8-byte slot per local. Calls follow System V: arguments in rdi, rsi,
rdx, rcx, r8, r9, result in rax. A `var` argument is passed as the address of its place.

Panics (SPEC §12) print `panic P0002: integer overflow` and the source position to
stderr, then exit with status 70. Each panic site has its own out-of-line stub.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ast as A
from . import elf
from .diagnostics import Diagnostic, Severity
from .resolve import CheckResult
from .symbols import Kind, Symbol
from .types import BOOLEAN, INTEGER, VOID, has_error, show
from .x86 import R8, R9, RAX, RBP, RCX, RDI, RDX, RSI, RSP, Assembler

_SYS_WRITE = 1
_SYS_EXIT_GROUP = 231
_EINTR = 4
_PANIC_STATUS = 70
_ARG_REGS = (RDI, RSI, RDX, RCX, R8, R9)
_DECLS = (A.TypeSection, A.ConstSection, A.Routine, A.ExternBlock, A.TestBlock)
_VALUES = (INTEGER, BOOLEAN)  # the types the code generator handles so far
_COMPARE = {"=": "e", "<>": "ne", "<": "l", "<=": "le", ">": "g", ">=": "ge"}
_SWAPPED = {"l": "g", "le": "ge", "g": "l", "ge": "le", "e": "e", "ne": "ne"}


@dataclass
class BuildResult:
    executable: bytes | None
    diagnostics: list[Diagnostic] = field(default_factory=list)


def build(result: CheckResult, checked: bool = True) -> BuildResult:
    """Compiles a checked program. ``result`` must have no errors. ``checked`` keeps the
    runtime checks that `--unchecked` drops (overflow, contracts)."""
    if result.main is None or result.has_errors:
        raise ValueError("build needs a program that checked without errors")
    return Emitter(result, checked).run()


class _Unsupported(Exception):
    """Raised to abandon the construct being compiled after reporting E0990."""


@dataclass
class _Frame:
    """The routine (or program body) being compiled."""

    file: str
    sym: Symbol | None
    exit: str  # label of the epilogue
    slots: dict = field(default_factory=dict)  # Symbol or key -> rbp offset
    size: int = 0
    loops: list = field(default_factory=list)  # (break label, continue label)
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

    def value_type(self, t, node, what: str = "a value"):
        """Checks that the code generator handles values of type ``t``."""
        if t not in _VALUES and not has_error(t):
            self.unsupported(node, f"{what} of type {show(t)}")

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
        self.stmts([i for i in self.main.tree.items if not isinstance(i, _DECLS)])
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
            a.jmp(self.runtime("rt_panic", self.rt_panic))
        self.emit_runtime()
        if self.diagnostics:
            return BuildResult(None, self.diagnostics)
        code = a.finish(elf.rodata_offset(len(a.code)))
        return BuildResult(elf.executable(code, bytes(a.rodata), a.labels["_start"]))

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
                a.store(RBP, f.alloc(p.sym), _ARG_REGS[i])
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
            self.stmts(node.body.stmts)
            a.label(f.exit)
            if f.ensures:
                a.store(RBP, f.result_slot, RAX)
                for e in f.ensures:
                    self.expr(e)
                    a.test(RAX, RAX)
                    a.jcc("e", self.panic("P0011", f"ensures violated: {self.text(e)}", e.span))
                a.load(RAX, RBP, f.result_slot)
        except _Unsupported:
            if f.exit not in a.labels:
                a.label(f.exit)
        a.leave()
        a.ret()
        a.patch32(size_at, elf.align(f.size, 16))

    def save_old(self, e) -> None:
        """Evaluates the `old(x)` calls of an `ensures` on entry."""
        if isinstance(e, A.Call) and self.is_old(e):
            arg = e.args[0].value
            self.expr(arg)
            self.asm.store(RBP, self.f.alloc(("old", id(e))), RAX)
            self.f.old[id(e)] = self.f.slots[("old", id(e))]
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
        self.stmts(body.stmts if isinstance(body, A.Block) else [body])

    def local(self, span) -> int:
        sym = self.ref(span)
        return self.f.alloc(sym if sym is not None else object())

    def stmt(self, s) -> None:
        a = self.asm
        if isinstance(s, A.VarDecl):
            sym = self.ref(s.name.span)
            self.value_type(sym.type if sym else None, s.name, f"variable '{s.name.name}'")
            self.expr(s.value)
            a.store(RBP, self.local(s.name.span), RAX)
        elif isinstance(s, A.Assign):
            self.expr(s.value)
            a.push(RAX)
            self.address(s.target)
            a.pop(RCX)
            a.store(RAX, 0, RCX)
        elif isinstance(s, A.Discard):
            self.expr(s.value)
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
            top, cond, end = a.new_label("repeat"), a.new_label("until"), a.new_label("rend")
            a.label(top)
            self.loop(s.body, end, cond)
            a.label(cond)
            self.expr(s.cond)
            a.test(RAX, RAX)
            a.jcc("e", top)
            a.label(end)
        elif isinstance(s, A.ForRange):
            self.for_range(s)
        elif isinstance(s, (A.Break, A.Continue)):
            brk, cont = self.f.loops[-1]
            a.jmp(brk if isinstance(s, A.Break) else cont)
        elif isinstance(s, A.Return):
            if s.value is not None:
                self.expr(s.value)
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
        self.f.loops.append((brk, cont))
        self.block(body)
        self.f.loops.pop()

    def for_range(self, s: A.ForRange) -> None:
        a = self.asm
        sym = self.ref(s.var.span)
        self.value_type(sym.type if sym else None, s.var, "a loop variable")
        if sym is not None and sym.type != INTEGER:
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

    def address(self, e) -> None:
        """Puts the address of the place ``e`` in rax."""
        if isinstance(e, A.Paren):
            return self.address(e.inner)
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
        """Computes ``e`` into rax."""
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

    def e_Paren(self, e) -> None:
        self.expr(e.inner)

    def e_Name(self, e) -> None:
        sym = self.ref(e.span)
        if sym is None:
            self.unsupported(e, "this name")
        self.load_sym(sym, e)

    def e_FieldAccess(self, e) -> None:
        sym = self.ref(e.name.span)
        if sym is None or sym.kind is not Kind.CONST:
            self.unsupported(e, "field access")
        self.load_sym(sym, e)

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
        if sym.kind is Kind.RESULT and self.f.result_slot is not None:
            a.load(RAX, RBP, self.f.result_slot)
            return
        if sym in self.f.slots:
            a.load(RAX, RBP, self.f.slots[sym])
            if self.is_var_param(sym):
                a.load(RAX, RAX)
            return
        self.unsupported(e, f"using {sym.describe()} as a value")

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
        self.expr(e.left)
        a.push(RAX)
        self.expr(e.right)
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
            self.expr(b.value)
            a.jmp(end)
            a.label(nxt)
        self.expr(e.else_value)
        a.label(end)

    def e_Call(self, e) -> None:
        if id(e) in self.f.old:
            self.asm.load(RAX, RBP, self.f.old[id(e)])
            return
        self.call(e)

    # --------------------------------------------------------------------- calls

    def call(self, e: A.Call) -> None:
        """Calls a routine; a function's result is left in rax."""
        a = self.asm
        m = self.calls.get(id(e))
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
        for p, slot in zip(m.params, m.slots):
            if slot is not None and p.is_var:
                self.address(slot.expr)
            elif slot is not None:
                self.expr(slot.expr)
            else:  # the default, which belongs to the callee's unit
                outer = self.f.file
                self.f.file = sym.file
                try:
                    self.expr(p.default)
                finally:
                    self.f.file = outer
            a.push(RAX)
        for i in reversed(range(len(m.params))):
            a.pop(_ARG_REGS[i])
        a.call(self.label_of(sym))

    def builtin(self, e, sym: Symbol, m) -> None:
        """Routines declared `extern "pst"`: implemented by the compiler."""
        a = self.asm
        args = [s.expr if s is not None else None for s in m.slots]
        if sym.unit == "IO" and sym.name in ("write", "writeln"):
            self.write(args[0] if args else None, newline=sym.name == "writeln")
        elif sym.unit == "System" and sym.name == "halt":
            self.expr(args[0])
            a.mov(RDI, RAX)
            self.exit()
        elif sym.unit == "System" and sym.name in ("inc", "dec"):
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
        else:
            self.unsupported(e, f"'{sym.name}'")

    def write(self, arg, newline: bool) -> None:
        """`write` / `writeln`: text pieces are written as they're computed."""
        pending = ""
        parts = arg.parts if isinstance(arg, A.InterpString) else [arg] if arg is not None else []
        for p in parts:
            if isinstance(p, A.StringLit):
                p = p.value
            if isinstance(p, str):
                pending += p
                continue
            self.write_text(pending)
            pending = ""
            self.write_value(p)
        self.write_text(pending + ("\n" if newline else ""))

    def write_text(self, text: str) -> None:
        if not text:
            return
        a = self.asm
        data = text.encode("utf-8")
        a.mov_imm(RDI, 1)
        a.lea_data(RSI, a.data(data))
        a.mov_imm(RDX, len(data))
        a.call(self.runtime("rt_write", self.rt_write))

    def write_value(self, e) -> None:
        a = self.asm
        t = self.type(e)
        self.expr(e)
        if t == INTEGER:
            a.call(self.runtime("rt_write_int", self.rt_write_int))
        elif t == BOOLEAN:
            false = a.new_label("false")
            a.mov_imm(RDI, 1)
            a.lea_data(RSI, a.data(b"true"))
            a.mov_imm(RDX, 4)
            a.test(RAX, RAX)
            a.jcc("ne", false)
            a.lea_data(RSI, a.data(b"false"))
            a.mov_imm(RDX, 5)
            a.label(false)
            a.call(self.runtime("rt_write", self.rt_write))
        else:
            self.unsupported(e, f"writing a value of type {show(t)}")

    # ----------------------------------------------------------------- runtime

    def runtime(self, label: str, emitter) -> str:
        self._runtime.setdefault(label, emitter)
        return label

    def emit_runtime(self) -> None:
        done = set()
        while len(done) < len(self._runtime):  # a routine may use another
            for label, emitter in list(self._runtime.items()):
                if label not in done:
                    done.add(label)
                    self.asm.label(label)
                    emitter()

    def exit(self) -> None:
        """exit_group(rdi)."""
        self.asm.mov_imm(RAX, _SYS_EXIT_GROUP)
        self.asm.syscall()

    def rt_write(self) -> None:
        """Writes rdx bytes at rsi to file descriptor rdi, retrying partial writes and
        EINTR. On any other error (a closed pipe), exits with status 1."""
        a = self.asm
        loop, done, failed = a.new_label("w"), a.new_label("w"), a.new_label("w")
        a.label(loop)
        a.test(RDX, RDX)
        a.jcc("e", done)
        a.mov_imm(RAX, _SYS_WRITE)
        a.syscall()  # clobbers rcx, r11
        a.test(RAX, RAX)
        a.jcc("s", failed)
        a.add(RSI, RAX)
        a.sub(RDX, RAX)
        a.jmp(loop)
        a.label(failed)
        a.cmp_imm(RAX, -_EINTR)
        a.jcc("e", loop)
        a.mov_imm(RDI, 1)
        self.exit()
        a.label(done)
        a.ret()

    def rt_write_int(self) -> None:
        """Writes the signed integer in rax to stdout, in decimal."""
        a = self.asm
        positive, digit, write = a.new_label("i"), a.new_label("i"), a.new_label("i")
        a.sub_imm(RSP, 32)
        a.lea(RSI, RSP, 32)  # digits are stored backwards from the end of the buffer
        a.mov(R8, RAX)
        a.test(RAX, RAX)
        a.jcc("ns", positive)
        a.neg(RAX)  # the minimum stays itself, which is right when read unsigned
        a.label(positive)
        a.mov_imm(RCX, 10)
        a.label(digit)
        a.xor32(RDX, RDX)
        a.div(RCX)
        a.add_imm(RDX, ord("0"))
        a.dec(RSI)
        a.store8(RSI, 0, RDX)
        a.test(RAX, RAX)
        a.jcc("ne", digit)
        a.test(R8, R8)
        a.jcc("ns", write)
        a.dec(RSI)
        a.store8_imm(RSI, 0, ord("-"))
        a.label(write)
        a.lea(RDX, RSP, 32)
        a.sub(RDX, RSI)
        a.mov_imm(RDI, 1)
        a.call(self.runtime("rt_write", self.rt_write))
        a.add_imm(RSP, 32)
        a.ret()

    def rt_panic(self) -> None:
        """Writes the message (rsi, rdx) to stderr and exits with status 70."""
        a = self.asm
        a.mov_imm(RDI, 2)
        a.call(self.runtime("rt_write", self.rt_write))
        a.mov_imm(RDI, _PANIC_STATUS)
        self.exit()


def _children(node):
    """The direct sub-nodes of an AST node."""
    for value in vars(node).values():
        if isinstance(value, A.Node):
            yield value
        elif isinstance(value, list):
            yield from (v for v in value if isinstance(v, A.Node))

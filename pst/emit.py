"""The emit pass: typed AST to an x86-64 executable (SPEC §15.2 steps 4–5).

The code generator grows construct by construct. Anything it can't compile yet is
reported as `E0990` at the construct, so a program either builds correctly or not at all.

Supported so far: the program body, with `write` / `writeln` of string literals.

Calling convention: System V (arguments in rdi, rsi, rdx, ...). Runtime routines are
emitted only when used.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ast as A
from . import elf
from .diagnostics import Diagnostic, Severity
from .resolve import CheckResult
from .x86 import RAX, RDI, RDX, RSI, Assembler

_SYS_WRITE = 1
_SYS_EXIT_GROUP = 231
_EINTR = 4
_DECLS = (A.TypeSection, A.ConstSection, A.Routine, A.ExternBlock, A.TestBlock)


@dataclass
class BuildResult:
    executable: bytes | None
    diagnostics: list[Diagnostic] = field(default_factory=list)


def build(result: CheckResult) -> BuildResult:
    """Compiles a checked program. ``result`` must have no errors."""
    if result.main is None or result.has_errors:
        raise ValueError("build needs a program that checked without errors")
    return Emitter(result).run()


class Emitter:
    def __init__(self, result: CheckResult) -> None:
        self.r = result
        self.unit = result.main
        self.file = self.unit.file
        self.asm = Assembler()
        self.diagnostics: list[Diagnostic] = []
        self._runtime: dict[str, object] = {}  # runtime routines used: label -> emitter

    def unsupported(self, node, what: str) -> None:
        self.diagnostics.append(Diagnostic(
            self.file, node.span, Severity.ERROR, "E0990",
            f"{what} is not supported by the code generator yet",
        ))  # fmt: skip

    def run(self) -> BuildResult:
        if self.unit.tree.kind != "program":
            self.diagnostics.append(Diagnostic(
                self.file, self.unit.tree.name.span, Severity.ERROR, "E0990",
                "only a program can be built; a unit is compiled with the program that uses it",
            ))  # fmt: skip
            return BuildResult(None, self.diagnostics)
        a = self.asm
        a.label("_start")
        for item in self.unit.tree.items:
            if not isinstance(item, _DECLS):
                self.stmt(item)
        a.xor32(RDI, RDI)
        self.exit()
        self.emit_runtime()
        if self.diagnostics:
            return BuildResult(None, self.diagnostics)
        code = a.finish(elf.rodata_offset(len(a.code)))
        return BuildResult(elf.executable(code, bytes(a.rodata), a.labels["_start"]))

    # -------------------------------------------------------------- statements

    def stmt(self, s) -> None:
        if isinstance(s, A.ExprStmt) and isinstance(s.expr, A.Call):
            self.call_stmt(s.expr)
        else:
            self.unsupported(s, f"'{type(s).__name__}' statement")

    def callee(self, call: A.Call):
        """The routine a call resolved to (after overload resolution), or None."""
        c = call.callee
        span = c.span if isinstance(c, A.Name) else c.name.span if isinstance(c, A.FieldAccess) else None
        ref = self.r.index.lookup(self.file, span) if span is not None else None
        if ref is None or len(ref.targets) != 1:
            return None
        return ref.targets[0]

    def call_stmt(self, call: A.Call) -> None:
        sym = self.callee(call)
        if sym is not None and sym.unit == "IO" and sym.name in ("write", "writeln"):
            self.write(call, newline=sym.name == "writeln")
            return
        self.unsupported(call, "this call")

    def write(self, call: A.Call, newline: bool) -> None:
        text = ""
        if call.args:
            arg = call.args[0].value
            if not isinstance(arg, A.StringLit):
                self.unsupported(arg, "writing a value other than a string literal")
                return
            text = arg.value
        data = (text + ("\n" if newline else "")).encode("utf-8")
        if not data:
            return
        a = self.asm
        a.lea_data(RSI, a.data(data))
        a.mov_imm(RDX, len(data))
        a.call(self.runtime("rt_write", self.rt_write))

    # ----------------------------------------------------------------- runtime

    def runtime(self, label: str, emitter) -> str:
        self._runtime.setdefault(label, emitter)
        return label

    def emit_runtime(self) -> None:
        for label, emitter in self._runtime.items():
            self.asm.label(label)
            emitter()

    def exit(self) -> None:
        """exit_group(rdi)."""
        self.asm.mov_imm(RAX, _SYS_EXIT_GROUP)
        self.asm.syscall()

    def rt_write(self) -> None:
        """Writes rdx bytes at rsi to stdout, retrying partial writes and EINTR.
        On any other error (a closed pipe), exits with status 1."""
        a = self.asm
        loop, done, failed = a.new_label("w"), a.new_label("w"), a.new_label("w")
        a.label(loop)
        a.test(RDX, RDX)
        a.jcc("e", done)
        a.mov_imm(RAX, _SYS_WRITE)
        a.mov_imm(RDI, 1)
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

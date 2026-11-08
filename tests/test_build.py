import os
import platform
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from pst.emit import build
from pst.resolve import check
from pst.x86 import R8, R9, R10, R11, R12, R15, RAX, RBX, RCX, RDX, RSI, RSP, Assembler

_NATIVE = platform.system() == "Linux" and platform.machine() == "x86_64"
_TMP = tempfile.TemporaryDirectory()


def tearDownModule():
    _TMP.cleanup()


def compile_program(body: str):
    """Checks and builds a program; returns (BuildResult, temp dir)."""
    tmp = tempfile.mkdtemp(dir=_TMP.name)
    path = Path(tmp, "Main.pst")
    path.write_text("program Main\n\nuses IO\n\n" + textwrap.dedent(body).lstrip("\n"))
    result = check(str(path))
    assert not result.has_errors, [d.human() for d in result.diagnostics]
    return build(result), tmp


def run_program(body: str, *args: str) -> subprocess.CompletedProcess:
    built, tmp = compile_program(body)
    assert built.executable is not None, [d.human() for d in built.diagnostics]
    exe = os.path.join(tmp, "main")
    Path(exe).write_bytes(built.executable)
    os.chmod(exe, 0o755)
    return subprocess.run([exe, *args], capture_output=True, timeout=10)


class Encoding(unittest.TestCase):
    def test_instructions(self):
        a = Assembler()
        a.mov_imm(RAX, 1)
        a.mov_imm(R9, 2**40)
        a.mov_imm(RCX, -1)
        a.mov(R8, RAX)
        a.push(R12)
        a.pop(RBX)
        a.add_imm(RSP, 8)
        a.sub_imm(R15, 1000)
        a.cmp_imm(RAX, -4)
        a.sub(RAX, RDX)
        a.test(R10, R10)
        a.xor32(R11, R11)
        a.lea_data(RSI, a.data(b"x"))
        a.label("l")
        a.jcc("ne", "l")
        a.jmp("l")
        a.call("l")
        a.syscall()
        a.ret()
        expected = (  # checked with objdump
            "b801000000 49b90000000000010000 48c7c1ffffffff 4989c0 4154 5b 4883c408"
            " 4981efe8030000 4883f8fc 4829d0 4d85d2 4531db 488d35c5000000"
            " 0f85faffffff e9f5ffffff e8f0ffffff 0f05 c3"
        )
        self.assertEqual(a.finish(0x100).hex(), expected.replace(" ", ""))

    def test_data_is_shared(self):
        a = Assembler()
        self.assertEqual(a.data(b"hi"), a.data(b"hi"))
        self.assertEqual(bytes(a.rodata), b"hi")


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Run(unittest.TestCase):
    def test_hello(self):
        p = run_program("""
            writeln("Hello, world!")
            write("ünï ")
            IO.writeln("ok")
            writeln()
            write("")
            """)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.decode(), "Hello, world!\nünï ok\n\n")

    def test_empty_program(self):
        p = run_program("")
        self.assertEqual((p.returncode, p.stdout), (0, b""))

    def test_closed_stdout_exits_with_failure(self):
        built, tmp = compile_program('writeln("x")')
        exe = os.path.join(tmp, "main")
        Path(exe).write_bytes(built.executable)
        os.chmod(exe, 0o755)
        p = subprocess.run(f"{exe} >&-", shell=True)  # stdout closed: EBADF
        self.assertEqual(p.returncode, 1)


class Unsupported(unittest.TestCase):
    def test_reported_at_the_construct(self):
        built, _ = compile_program("""
            let n := 1
            writeln(n)
            """)
        self.assertIsNone(built.executable)
        self.assertEqual(
            [(d.code, d.span.line, d.span.col) for d in built.diagnostics],
            [("E0990", 5, 1), ("E0990", 6, 9)],
        )


if __name__ == "__main__":
    unittest.main()

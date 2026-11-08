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


def compile_program(body: str, checked: bool = True):
    """Checks and builds a program; returns (BuildResult, temp dir)."""
    tmp = tempfile.mkdtemp(dir=_TMP.name)
    path = Path(tmp, "Main.pst")
    path.write_text("program Main\n\nuses IO\n\n" + textwrap.dedent(body).lstrip("\n"))
    result = check(str(path))
    assert not result.has_errors, [d.human() for d in result.diagnostics]
    return build(result, checked), tmp


def run_program(body: str, *args: str, checked: bool = True) -> subprocess.CompletedProcess:
    built, tmp = compile_program(body, checked)
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


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Integers(unittest.TestCase):
    def output(self, body: str, **kw) -> str:
        p = run_program(body, **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode()

    def test_arithmetic(self):
        out = self.output("""
            const
                Big = 1000000 * 1000000
            let a := 7
            writeln("{a + 3} {a - 10} {a * -6} {Big} {-a}")
            writeln("{-7 div 2} {-7 mod 2} {7 div -2} {7 mod -2} {(2 + 3) * 4}")
            writeln(9223372036854775807)
            writeln(-9223372036854775807 - 1)
            writeln(0)
            """)
        self.assertEqual(
            out,
            "10 -3 -42 1000000000000 -7\n-3 -1 -3 1 20\n"
            "9223372036854775807\n-9223372036854775808\n0\n",
        )

    def test_booleans(self):
        out = self.output("""
            let a := 3
            let t := a > 2 and a < 5
            writeln("{t} {not t} {a = 3 or a div 0 = 1} {a <> 3 and a div 0 = 1}")
            writeln(if a >= 3 then 10 else 20)
            """)
        self.assertEqual(out, "true false true false\n10\n")

    def test_control_flow(self):
        out = self.output("""
            var total := 0
            for i := 1 to 10 do
                if i mod 2 = 0 then
                    continue
                total := total + i
            for i := 3 downto 1 do
                write(i)
            for i := 5 to 4 do
                write("never")
            for i := 9223372036854775806 to 9223372036854775807 do
                write(".")
            writeln(" {total}")
            var n := 0
            while true do
                inc(var n)
                if n = 4 then
                    break
            repeat
                dec(var n, 2)
            until n < 0
            if n = 0 then
                writeln("zero")
            else if n = -2 then
                writeln("minus two")
            else
                writeln("other")
            """)
        self.assertEqual(out, "321.. 25\nminus two\n")

    def test_routines(self):
        out = self.output("""
            function fact(n: Integer): Integer
                requires n >= 0
                if n <= 1 then
                    return 1
                return n * fact(n - 1)

            procedure bump(var x: Integer, by: Integer := 10)
                ensures x = old(x) + by
                x := x + by

            procedure twice(var x: Integer)
                bump(var x)
                bump(x: var x, by: 1)

            function sum6(a, b, c, d, e, f: Integer): Integer
                return a + 2 * b + 3 * c + 4 * d + 5 * e + 6 * f

            writeln(fact(20))
            var b := 5
            twice(var b)
            writeln(b)
            writeln(sum6(1, 2, 3, fact(3), 5, 6))
            writeln(3.fact())
            """)
        self.assertEqual(out, "2432902008176640000\n16\n99\n6\n")

    def test_halt(self):
        p = run_program("""
            writeln("a")
            halt(3)
            writeln("b")
            """)
        self.assertEqual((p.returncode, p.stdout), (3, b"a\n"))


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Panics(unittest.TestCase):
    def panic(self, body: str, **kw) -> str:
        p = run_program(body, **kw)
        self.assertEqual(p.returncode, 70, p.stdout)
        return p.stderr.decode()

    def test_overflow(self):
        for expr, col in [("x + 1", 8), ("x * 2", 8), ("-(-x - 1)", 6), ("(-x - 1) div -1", 15)]:
            with self.subTest(expr):
                err = self.panic(f"var x := 9223372036854775807\nx := {expr}")
                self.assertEqual(err, f"panic P0002: integer overflow\n    at Main.pst:6:{col}\n")
        err = self.panic("var x := 9223372036854775807\ninc(var x)")
        self.assertIn("P0002", err)

    def test_unchecked_wraps(self):
        p = run_program(
            "var x := 9223372036854775807\nx := x + 1\nwriteln(x)", checked=False
        )
        self.assertEqual((p.returncode, p.stdout), (0, b"-9223372036854775808\n"))

    def test_division_by_zero(self):
        for checked in (True, False):
            err = self.panic("var z := 0\nwriteln(1 mod z)", checked=checked)
            self.assertEqual(err, "panic P0004: division by zero\n    at Main.pst:6:11\n")

    def test_contracts(self):
        err = self.panic("""
            function f(n: Integer): Integer
                requires n > 0
                return n
            writeln(f(0))
            """)
        self.assertEqual(err, "panic P0010: requires violated: n > 0\n    at Main.pst:6:14\n")
        err = self.panic("""
            function f(n: Integer): Integer
                ensures result > n
                return n
            writeln(f(3))
            """)
        self.assertIn("P0011: ensures violated: result > n", err)
        p = run_program("""
            function f(n: Integer): Integer
                requires n > 0
                return n
            writeln(f(0))
            """, checked=False)  # fmt: skip
        self.assertEqual((p.returncode, p.stdout), (0, b"0\n"))

    def test_assert_stays_in_unchecked_builds(self):
        err = self.panic('assert 1 + 1 = 3, "math"', checked=False)
        self.assertEqual(err, "panic P0020: assertion failed: 1 + 1 = 3: math\n    at Main.pst:5:1\n")


class Unsupported(unittest.TestCase):
    def test_reported_at_the_construct(self):
        built, _ = compile_program("""
            let s := "text"
            writeln(s)
            _ := 1.5
            """)
        self.assertIsNone(built.executable)
        self.assertEqual(
            [(d.code, d.span.line, d.span.col) for d in built.diagnostics],
            [("E0990", 5, 5), ("E0990", 6, 9), ("E0990", 7, 6)],
        )

    def test_only_programs(self):
        tmp = tempfile.mkdtemp(dir=_TMP.name)
        path = Path(tmp, "Lib.pst")
        path.write_text("unit Lib\n\nexport function one(): Integer\n    return 1\n")
        built = build(check(str(path)))
        self.assertEqual([d.code for d in built.diagnostics], ["E0990"])


if __name__ == "__main__":
    unittest.main()

import os
import platform
import shutil
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


def compile_program(body: str, checked: bool = True, uses: str = "IO", units: dict | None = None):
    """Checks and builds a program, with ``units`` (file name -> source) next to it;
    returns (BuildResult, temp dir)."""
    tmp = tempfile.mkdtemp(dir=_TMP.name)
    for name, text in (units or {}).items():
        Path(tmp, name).write_text(textwrap.dedent(text).lstrip("\n"))
    path = Path(tmp, "Main.pst")
    path.write_text(f"program Main\n\nuses {uses}\n\n" + textwrap.dedent(body).lstrip("\n"))
    result = check(str(path))
    assert not result.has_errors, [d.human() for d in result.diagnostics]
    return build(result, checked), tmp


def run_program(
    body: str, *args: str, checked: bool = True, uses: str = "IO", under: tuple = (),
    units: dict | None = None,
) -> subprocess.CompletedProcess:
    """Builds and runs a program, optionally under a tool such as valgrind."""
    built, tmp = compile_program(body, checked, uses, units)
    assert built.executable is not None, [d.human() for d in built.diagnostics]
    exe = os.path.join(tmp, "main")
    Path(exe).write_bytes(built.executable)
    os.chmod(exe, 0o755)
    return subprocess.run([*under, exe, *args], capture_output=True, timeout=60)


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
        a.test32(RAX, R8)
        a.ret()
        expected = (  # checked with objdump
            "b801000000 49b90000000000010000 48c7c1ffffffff 4989c0 4154 5b 4883c408"
            " 4981efe8030000 4883f8fc 4829d0 4d85d2 4531db 488d35c5000000"
            " 0f85faffffff e9f5ffffff e8f0ffffff 4485c0 c3"
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

    def test_slice_offsets(self):
        for args, code in [("0, 2", "P0006"), ("2, 9", "P0001"), ("3, 1", "P0001"), ("-1, 1", "P0001")]:
            with self.subTest(args):
                err = self.panic(f'writeln(slice("h\u00e9llo", {args}))', uses="IO, Strings")
                self.assertTrue(err.startswith(f"panic {code}: "), err)
                self.assertTrue(err.endswith("at Main.pst:5:9\n"), err)
        err = self.panic('writeln(slice("h\u00e9llo", 2, 3))', checked=False, uses="IO, Strings")
        self.assertIn("P0006: slice offset is not on a character boundary", err)

    def test_assert_stays_in_unchecked_builds(self):
        err = self.panic('assert 1 + 1 = 3, "math"', checked=False)
        self.assertEqual(err, "panic P0020: assertion failed: 1 + 1 = 3: math\n    at Main.pst:5:1\n")


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Strings(unittest.TestCase):
    def output(self, body: str, **kw) -> str:
        p = run_program(body, uses="IO, Strings", **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode()

    def test_values(self):
        out = self.output("""
            function greet(name: String, punct: Char := '!'): String
                let g := "Hello, {name}{punct}"
                return g

            procedure append(var s: String, tail: String)
                s := "{s}{tail}"

            var s := "ab"
            let t := s
            s := "{s}c"
            writeln("{s} {t}")
            writeln(greet("w\u00f6rld"))
            writeln(greet("x", punct: '\u00e9'))
            var acc := ""
            for i := 1 to 4 do
                append(var acc, tail: toString(i))
            writeln(acc)
            writeln("{toString(-12)} {toString(true)} {toString('\u20ac')} {'\U0001F600'} {ord('A')}")
            writeln(if acc = "1234" then "same" else acc)
            """)
        self.assertEqual(
            out, "abc ab\nHello, w\u00f6rld!\nHello, x\u00e9\n1234\n-12 true \u20ac \U0001F600 65\nsame\n"
        )

    def test_comparisons(self):
        out = self.output("""
            let e := ""
            writeln("{'a' < 'b'} {"abc" < "abd"} {"ab" < "abc"} {"b" > "abc"} {"x" = "x"}")
            writeln("{e = ""} {"a" <> "a"} {"\u00e9" > "z"} {e < "a"} {"b" >= "b"}")
            """)
        self.assertEqual(out, "true true true true true\ntrue false true true true\n")

    def test_strings_unit(self):
        out = self.output("""
            let s := "h\u00e9llo"
            writeln("{s.byteLen()} {s.charCount()} {slice(s, 0, 3)}|{slice(s, 3, 6)}|{slice(s, 6, 6)}|")
            writeln("{slice(s, 1, 3).charCount()} {startsWith(s, prefix: "h\u00e9")}")
            writeln("{startsWith("he", prefix: "hello")} {startsWith("x", prefix: "")}")
            """)
        self.assertEqual(out, "6 5 h\u00e9|llo||\n1 true\nfalse true\n")

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_reference_counting(self):
        """Every string is freed exactly once, however its block is left."""
        out = self.output("""
            const
                Name = "pst"
                Banner = "<{Name}>"

            function find(s: String): Integer
                for i := 0 to s.byteLen() - 1 do
                    let rest := slice(s, i, s.byteLen())
                    var j := 0
                    while true do
                        let tmp := "{rest}{j}"
                        if startsWith(tmp, prefix: "x") then
                            return i
                        j := j + 1
                        if j > 2 then
                            break
                return -1

            procedure shout(var s: String)
                ensures s.byteLen() = old(s).byteLen() + 1
                ensures startsWith(s, prefix: old(s))
                s := "{s}!"

            function pick(b: Boolean): String
                return if b then "yes {Banner}" else Name

            writeln("{find("abxd")} {find("ab")}")
            var s := "hey"
            shout(var s)
            shout(var s)
            s := s
            writeln(s)
            writeln("{pick(true)} {pick(false)} {Banner}")
            var k := 0
            repeat
                let a := "a{k}"
                k := k + 1
                for i := 1 to 3 do
                    let w := "{a}{i}"
                    if w = "a12" then
                        break
                    if i = 1 then
                        continue
                    _ := w
                let b := "{a}b"
                if b = "a3b" then
                    break
            until a = "a5"
            writeln(k)
            _ := pick(true)
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual(out, "2 -1\nhey!!\nyes <pst> pst <pst>\n4\n")

    def test_many_strings(self):
        out = self.output("""
            var n := 0
            var y := ""
            while n < 1000000 do
                let x := "n = {n}"
                y := "{x}!"
                n := n + 1
            writeln(y)
            """)
        self.assertEqual(out, "n = 999999!\n")


class Records(unittest.TestCase):
    def output(self, body: str, **kw) -> str:
        p = run_program(body, **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode()

    def test_records_enums_and_unions(self):
        out = self.output("""
            type
                Color = (Red, Green, Blue)
                Point = record
                    x: Integer
                    y: Integer
                    label: String := "p"
                Circle = record
                    r: Integer
                Rect = record
                    w: Integer
                    h: Integer
                Shape = Circle | Rect

            function area(s: Shape): Integer
                return case s of
                    Circle(c): 3 * c.r * c.r
                    Rect(r): r.w * r.h

            procedure move(var p: Point, dx: Integer)
                p.x := p.x + dx

            procedure bump(var n: Integer)
                n := n + 1

            function find(n: Integer): Option[Integer]
                if n > 0 then
                    return Some(n * 2)
                return None

            var p := Point(x: 1, y: 2)
            let q := p
            move(var p, dx: 10)
            p.label := "moved"
            writeln("{p.x} {p.y} {p.label} / {q.x} {q.label}")
            bump(var p.y)
            writeln(p.y)
            writeln(p = q)
            writeln(Point(x: 1, y: 2) = q)
            let c := Color.Green
            writeln("{c} {ord(c)} {Color.Blue}")
            for k := Color.Red to Color.Blue do
                write("{k} ")
            writeln("")
            case c of
                Red: writeln("red")
                Green, Blue: writeln("not red")
            var shapes: Shape := Circle(r: 2)
            writeln(area(shapes))
            shapes := Rect(w: 3, h: 4)
            writeln(area(shapes))
            writeln(area(Circle(r: 1)))
            case find(4) of
                Some(v): writeln("some {v.value}")
                None: writeln("none")
            case find(0) of
                Some(v): writeln("some {v.value}")
                None: writeln("none")
            let o: Option[Integer] := Some(3)
            writeln(o = Some(3))
            writeln(o = None)
            let label := case 7 of
                1..5: "low"
                else: "high"
            writeln(label)
            """)
        self.assertEqual(
            out,
            "11 2 moved / 1 p\n3\nfalse\ntrue\nGreen 1 Blue\nRed Green Blue \nnot red\n12\n12\n3\n"
            "some 8\nnone\ntrue\nfalse\nhigh\n",
        )

    def test_case_on_values(self):
        out = self.output("""
            type
                Circle = record
                    r: Integer
                Rect = record
                    w: Integer
                    h: Integer
                Shape = Circle | Rect

            function kind(s: String): Integer
                return case s of
                    "a", "b": 1
                    "c": 2
                    else: 0

            function cls(c: Char): String
                return case c of
                    'a'..'z': "lower"
                    '0'..'9': "digit"
                    else: "other"

            for n := 1 to 3 do
                let s: Shape := case n of
                    1: Circle(r: n)
                    else: Rect(w: n, h: 1)
                case s of
                    Circle(c): writeln("circle {c.r}")
                    Rect(_): writeln("rect")
            writeln("{kind("b")} {kind("c")} {kind("zz")}")
            writeln("{cls('q')} {cls('5')} {cls('#')}")
            let o: Option[String] := System.None
            case o of
                Some(v): writeln(v.value)
                None: writeln("nothing")
            let flag := true
            case flag of
                true: writeln("yes")
                false: writeln("no")
            """)
        self.assertEqual(out, "circle 1\nrect\nrect\n1 2 0\nlower digit other\nnothing\nyes\n")

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_copy_on_write(self):
        """Copies share a block until one is changed; every block is freed once."""
        out = self.output("""
            type
                Inner = record
                    name: String
                    n: Integer
                Outer = record
                    inner: Inner
                    tag: String
                Value = Integer | String | Inner
                Tree = Leaf | Node
                Leaf = record
                Node = record
                    left: Tree
                    value: Integer
                    right: Tree

            procedure rename(var s: String, text: String)
                s := text

            function make(n: Integer): Outer
                return Outer(inner: Inner(name: "in{n}", n: n), tag: "t{n}")

            function describe(v: Value): String
                return case v of
                    Integer(i): "int {i}"
                    String(s): "str {s}"
                    Inner(r): "inner {r.name}"

            function sum(t: Tree): Integer
                case t of
                    Leaf: return 0
                    Node(n): return sum(n.left) + n.value + sum(n.right)

            function parse(s: String): Result[Integer, String]
                if s = "one" then
                    return Ok(1)
                return Err("bad: {s}")

            var a := make(1)
            var b := a
            a.inner.name := "changed"
            rename(var a.inner.name, text: "renamed {a.tag}")
            writeln("{a.inner.name} {b.inner.name} {make(5).inner.name}")
            b.inner.n := 7
            writeln("{a.inner.n} {b.inner.n}")
            writeln(a = b)
            b := a
            writeln(a = b)
            writeln(describe(a.inner.n))
            writeln(describe("x"))
            writeln(describe(a.inner))
            var t: Tree := Node(left: Node(left: Leaf, value: 1, right: Leaf), value: 2, right: Node(left: Leaf, value: 3, right: Leaf))
            writeln(sum(t))
            var i := 0
            while true do
                i := i + 1
                case parse(if i = 1 then "one" else "two") of
                    Ok(v): writeln("ok {v.value}")
                    Err(e):
                        writeln(e.error)
                        break
            var total := 0
            for k := 1 to 10000 do
                let o := make(k)
                var c := o
                c.inner.n := k
                total := total + c.inner.n - o.inner.n
            writeln(total)
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual(
            out,
            "renamed t1 in1 in5\n1 7\nfalse\ntrue\nint 1\nstr x\ninner renamed t1\n6\nok 1\nbad: two\n0\n",
        )


class Generics(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_instances(self):
        """Generic routines over records, unions and strings; bound members are found
        where the type argument became concrete (`area` lives in Main, not Geo)."""
        p = run_program("""
            type
                Circle = record
                    r: Integer
                Square = record
                    side: Integer
                Pair[A, B] = record
                    first: A
                    second: B
                Tree[T] = Leaf | Node[T]
                Leaf = record
                Node[T] = record
                    left: Tree[T]
                    value: T
                    right: Tree[T]

            function area(c: Circle): Integer
                return 3 * c.r * c.r

            function area(s: Square): Integer
                return s.side * s.side

            function biggest[T: Ordered](a, b: T): T
                return if a > b then a else b

            function swapped[A, B](p: Pair[A, B]): Pair[B, A]
                return Pair(first: p.second, second: p.first)

            function first[T](xs: Option[T], fallback: T): T
                return case xs of
                    Some(s): s.value
                    None: fallback

            function insert[T: Ordered](t: Tree[T], v: T): Tree[T]
                case t of
                    Leaf: return Node(left: Leaf, value: v, right: Leaf)
                    Node(n):
                        if v < n.value then
                            return Node(left: insert(n.left, v), value: n.value, right: n.right)
                        return Node(left: n.left, value: n.value, right: insert(n.right, v))

            function show[T: Printable](t: Tree[T]): String
                return case t of
                    Leaf: ""
                    Node(n): "{show(n.left)}{n.value} {show(n.right)}"

            procedure swap[T](var a, b: T)
                let t := a
                a := b
                b := t

            writeln(biggest(3, 7))
            writeln(biggest("pear", "apple"))
            writeln(biggest('x', 'y'))
            let p := swapped(Pair(first: 1, second: "one"))
            writeln("{p.first} {p.second}")
            writeln(first(Some(5), fallback: 0))
            writeln(first(System.None, fallback: "none"))
            var t: Tree[Integer] := Leaf
            var k := 5
            for i := 1 to 6 do
                k := k * 7 mod 11
                t := insert(t, k)
            writeln(show(t))
            var s: Tree[String] := Leaf
            s := insert(s, "m")
            s := insert(s, "c")
            s := insert(s, "x")
            writeln(show(s))
            var x := "left"
            var y := "right"
            swap(var x, var y)
            writeln("{x} {y}")
            var pc := Pair(first: 1, second: 2)
            swap(var pc.first, var pc.second)
            writeln("{pc.first} {pc.second}")
            writeln(total(Circle(r: 1), Circle(r: 2)))
            writeln(total(Square(side: 3), Square(side: 4)))
            writeln(describe(Square(side: 2)))
            """, uses="IO, Geo", units={"Geo.pst": """
                unit Geo

                uses IO

                type
                    export Measurable = trait
                        function area(this): Integer

                export function total[T: Measurable](a, b: T): Integer
                    return twice(a) + b.area()

                export function describe[T: Measurable](x: T): String
                    return "area {total(x, x) div 3}"

                function twice[T: Measurable](x: T): Integer
                    return double(x.area())

                function double(n: Integer): Integer
                    return n * 2
                """}, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(
            p.stdout.decode(),
            "7\npear\ny\none 1\n5\nnone\n2 3 4 6 9 10 \nc m x \nright left\n2 1\n18\n34\narea 4\n",
        )

    def test_explicit_type_arguments(self):
        p = run_program("""
            function nothing[T](): Option[T]
                return None

            writeln(nothing[Integer]() = None)
            """)
        self.assertEqual(p.stdout, b"true\n")

    def test_endless_instantiation(self):
        built, _ = compile_program("""
            type
                Box[T] = record
                    item: T

            function deep[T](x: T, n: Integer): Integer
                if n = 0 then
                    return 0
                return deep(Box(item: x), n - 1) + 1

            writeln(deep(1, 3))
            """)
        self.assertIsNone(built.executable)
        self.assertEqual([(d.code, d.span.line, d.span.col) for d in built.diagnostics], [("E0720", 12, 12)])


class Unsupported(unittest.TestCase):
    def test_reported_at_the_construct(self):
        built, _ = compile_program("""
            let r := 1.5
            writeln(r)
            _ := 2.5
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

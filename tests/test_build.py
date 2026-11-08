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
    units: dict | None = None, env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Builds and runs a program, optionally under a tool such as valgrind."""
    built, tmp = compile_program(body, checked, uses, units)
    assert built.executable is not None, [d.human() for d in built.diagnostics]
    exe = os.path.join(tmp, "main")
    Path(exe).write_bytes(built.executable)
    os.chmod(exe, 0o755)
    environ = {**os.environb, **{k.encode(): v for k, v in (env or {}).items()}}
    return subprocess.run([*under, exe, *args], capture_output=True, timeout=60, env=environ)


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


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class SizedIntegers(unittest.TestCase):
    def output(self, body: str, **kw) -> str:
        p = run_program(body, **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode()

    def test_values(self):
        out = self.output("""
            var a: Int8 := 127
            var u: UInt8 := 250
            var w: UInt64 := 18446744073709551615
            let i: Int32 := -2147483648
            writeln("{a} {u} {w} {i}")
            inc(var u, 5)
            dec(var w)
            writeln("{u} {w} {UInt64(5) < w} {w div 3} {w mod 10}")
            writeln("{Int64(i) - 1} {UInt16(65535)} {Int8(a - 100)} {Int16(a) * 3}")
            for x := UInt8(254) to 255 do
                write(x)
            writeln()
            """)
        self.assertEqual(
            out,
            "127 250 18446744073709551615 -2147483648\n"
            "255 18446744073709551614 true 6148914691236517204 4\n"
            "-2147483649 65535 27 381\n254255\n",
        )

    def test_overflow_and_conversions(self):
        for body in [
            "var a: Int8 := 127\ninc(var a)",
            "var a: Int32 := 2000000000\na := a * 2",
            "var u: UInt8 := 0\ndec(var u)",
            "var w: UInt64 := 18446744073709551615\nw := w + 1",
            "let x := 300\nwriteln(UInt8(x))",
            "let x := -1\nwriteln(UInt64(x))",
            "let w: UInt64 := 18446744073709551615\nwriteln(Int64(w))",
        ]:
            with self.subTest(body):
                p = run_program(body)
                self.assertEqual(p.returncode, 70, p.stdout)
                self.assertIn(b"P0002", p.stderr)
        p = run_program("var a: Int8 := 127\ninc(var a)\nlet x := 300\nwriteln(\"{a} {UInt8(x)}\")", checked=False)
        self.assertEqual(p.stdout, b"-128 44\n")


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class CInterop(unittest.TestCase):
    LIBC = """
        extern "libc.so.6"
            type
                export Div = record
                    quot: CInt
                    rem: CInt
                export Mixed = record
                    a: Int8
                    b: Int64
                    c: Int16
            function abs(x: CInt): CInt
            function strlen(s: CString): CSize
            function printf(fmt: CString, ...): CInt
            function malloc(n: CSize): Pointer
            procedure free(p: Pointer)
            procedure qsort(base: Pointer, n: CSize, size: CSize, cmp: function(Pointer, Pointer): CInt)
            function cdiv(a: CInt, b: CInt): Div as "div"
            function getenv(name: CString): CString
            function setenv(name: CString, value: CString, overwrite: CInt): CInt

        extern "libm.so.6"
            function labs(x: CLong): CLong
        """

    def run_c(self, body: str, **kw) -> subprocess.CompletedProcess:
        return run_program(textwrap.dedent(self.LIBC) + textwrap.dedent(body), uses="IO, C", **kw)

    def output(self, body: str, **kw) -> str:
        p = self.run_c(body, **kw)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout.decode()

    def test_calls_and_strings(self):
        out = self.output("""
            let s := "h\u00e9llo"
            writeln("{abs(-5)} {strlen(toCString(s))} {labs(-7)}")
            for i := 1 to 3 do
                let t := "{s}{i}"
                write(strlen(toCString(t)))
            writeln()
            _ := setenv(toCString("PST_TEST"), value: toCString("ok\u20ac"), overwrite: 1)
            writeln(fromCString(getenv(toCString("PST_TEST"))))
            _ := printf(toCString("%d-%s|"), 7, toCString("x"))
            """)
        self.assertEqual(out, "5 6 7\n777\nok\u20ac\n7-x|")

    def test_invalid_utf8_and_null(self):
        p = self.run_c(
            'writeln(fromCString(getenv(toCString("PST_TEST"))))',
            env={"PST_TEST": b"a\xff\xc3(\xed\xa0\x80\xf0\x9f\x98\x80\xe2\x82"},
        )
        r = "\ufffd"
        self.assertEqual(p.stdout.decode(), f"a{r}{r}({r}{r}{r}\U0001F600{r}{r}\n")
        out = self.output("""
            function env(name: String): Option[String]
                let p := getenv(toCString(name))
                if p.isNull() then
                    return None
                return Some(fromCString(p))

            unsafe procedure pointers()
                let m := malloc(8)
                writeln("{isNull(m)} {cast[Ptr[CInt]](m).isNull()} {isNull(cast[Ptr[Div]](0))}")
                free(m)

            writeln("{env("PST_TEST").isSome()} {env("PST_NOT_SET_ANYWHERE").isSome()}")
            pointers()
            """, env={"PST_TEST": b"x"})
        self.assertEqual(out, "true false\nfalse false true\n")
        p = self.run_c('writeln(fromCString(getenv(toCString("PST_NOT_SET_ANYWHERE"))))')
        self.assertEqual(p.returncode, 70)
        self.assertIn(b"panic P0007: null C string", p.stderr)

    def test_pointers_and_callbacks(self):
        out = self.output("""
            function compare(a: Pointer, b: Pointer): CInt
                return compareInts(a, b: b)

            unsafe function compareInts(a: Pointer, b: Pointer): CInt
                let x := deref(cast[Ptr[CInt]](a))
                let y := deref(cast[Ptr[CInt]](b))
                return if x < y then -1 else if x > y then 1 else 0

            unsafe procedure sortDemo()
                let p := cast[Ptr[CInt]](malloc(20))
                for i := 0 to 4 do
                    deref(p + i) := CInt(10 - 3 * i)
                qsort(cast[Pointer](p), 5, size: 4, cmp: compare)
                for i := 0 to 4 do
                    write("{deref(p + i)} ")
                writeln()
                free(cast[Pointer](p))

            sortDemo()
            """)
        self.assertEqual(out, "-2 1 4 7 10 \n")

    def test_c_records(self):
        out = self.output("""
            unsafe procedure demo()
                var d := Div(quot: 1, rem: 2)
                let q := addr(var d.quot)
                deref(q) := 42
                inc(var d.rem)
                writeln("{d.quot} {d.rem}")
                var e := d
                e.rem := 7
                writeln("{d.rem} {e.rem} {d = e} {d = Div(quot: 42, rem: 3)}")
                let m := Mixed(a: -3, b: 100, c: 300)
                writeln("{m.a} {m.b} {m.c}")

            demo()
            let r := cdiv(17, b: 5)
            writeln("{r.quot} {r.rem}")
            """)
        self.assertEqual(out, "42 3\n3 7 false true\n-3 100 300\n3 2\n")

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_no_leaks(self):
        p = self.run_c("""
            procedure demo()
                for i := 1 to 3 do
                    let t := "x{i}"
                    _ := strlen(toCString(t))
                    if i = 2 then
                        return
            demo()
            var d := Div(quot: 1, rem: 2)
            let e := d
            d := cdiv(9, b: 2)
            writeln(fromCString(getenv(toCString("PST_TEST"))))
            writeln(e.rem + d.rem)
            """, under=("valgrind", "--leak-check=full", "--error-exitcode=9", "-q"), env={"PST_TEST": b"v\xff"})
        self.assertEqual((p.returncode, p.stdout.decode()), (0, "v\ufffd\n3\n"), p.stderr)


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Lists(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_operations(self):
        """Literals, indexing, `add`/`remove`/`concat` (written in Pasthon), `in`, `=`,
        loops, copy-on-write and nested lists; every block is freed once."""
        p = run_program("""
            type
                Item = record
                    name: String
                    tags: List[String]

            procedure bump(var x: Integer)
                x := x + 1

            var xs: List[Integer] := [1, 2, 3]
            add(var xs, 4)
            writeln("{len(xs)} {xs[0]} {xs[3]}")
            var ys := xs
            ys[0] := 10
            bump(var ys[1])
            writeln("{xs[0]} {xs[1]} {ys[0]} {ys[1]}")
            remove(var ys, 1)
            for y in ys do
                write("{y} ")
            writeln()
            let zs := concat(xs, b: ys)
            writeln("{len(zs)} {3 in zs} {7 in zs} {xs = [1, 2, 3, 4]} {xs = ys}")
            var names: List[String] := []
            for i := 1 to 20 do
                add(var names, "n{i}")
            writeln("{names[19]} {"n7" in names} {"b" in names}")
            var it := Item(name: "a", tags: [])
            add(var it.tags, "x")
            let copy := it
            add(var it.tags, "y")
            writeln("{len(it.tags)} {len(copy.tags)}")
            var grid: List[List[Integer]] := [[1, 2], [3]]
            add(var grid[1], 4)
            grid[0][1] := 9
            for row in grid do
                for x in row do
                    write("{x} ")
            writeln()
            var os: List[Option[String]] := [Some("a"), None]
            add(var os, Some("c"))
            writeln("{os = [Some("a"), None, Some("c")]} {os = [Some("a"), None, Some("d")]}")
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(
            p.stdout.decode(),
            "4 1 4\n1 2 10 3\n10 3 4 \n7 true false true false\nn20 true false\n2 1\n1 9 3 4 \n"
            "true false\n",
        )

    def test_bounds(self):
        for body in ["let xs := [1]\nwriteln(xs[1])", "var xs := [1]\nxs[-1] := 2",
                     "var xs: List[Integer] := []\nremove(var xs, 0)"]:
            with self.subTest(body):
                p = run_program(body)
                self.assertEqual(p.returncode, 70, p.stdout)
                self.assertIn(b"P0001", p.stderr)

    def test_growing_a_field(self):
        """`add(var b.items, x)` moves the list out of the field for the call, so it isn't
        copied each time."""
        p = run_program("""
            type
                Bag = record
                    items: List[Integer]
            procedure addLen(var xs: List[Integer], b: Bag)
                add(var xs, len(b.items))
            var b := Bag(items: [])
            for i := 1 to 100000 do
                add(var b.items, i)
            addLen(var b.items, b)  # `b` is used again, so `b.items` is copied, not moved
            writeln("{b.items[99999]} {b.items[100000]}")
            """)
        self.assertEqual(p.stdout, b"100000 100000\n")


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Maps(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_operations(self):
        """Literals, `m[k]` (reading, assigning, as a `var` place), `in`, `remove`,
        insertion order, keys of several types, growth and removal over many entries,
        copy-on-write, `=` in any order (nested too), `get`/`getOr`; every block is
        freed once."""
        p = run_program("""
            type
                Color = (Red, Green, Blue)
                Point = record
                    x: Integer
                    y: Integer
                Box = record
                    name: String
                    tags: Map[String, Integer]

            function total(m: Map[String, Integer]): Integer
                var t := 0
                for _, v in m do
                    t := t + v
                return t

            procedure bump(var n: Integer)
                inc(var n, 10)

            var m := {"one": 1, "two": 2, "three": 3}
            m["two"] := 22
            m["four"] := 4
            remove(var m, "one")
            remove(var m, "missing")
            for k, v in m do
                write("{k}={v} ")
            writeln("{len(m)} {total(m)}")
            let copy := m
            m["five"] := 5
            inc(var m["two"])
            bump(var m["three"])
            writeln("{len(copy)} {len(m)} {copy["two"]} {m["two"]} {m["three"]}")
            for k in m do
                write("{k},")
            writeln()
            var colors: Map[Color, String] := {}
            colors[Color.Blue] := "sky"
            colors[Color.Red] := "rose"
            var points: Map[Point, Char] := {Point(x: 1, y: 2): 'a'}
            points[Point(x: 3, y: 4)] := 'b'
            write("{colors[Color.Red]} {colors[Color.Blue]} {Color.Green in colors} ")
            writeln("{points[Point(x: 1, y: 2)]} {points[Point(x: 3, y: 4)]} {Point(x: 2, y: 1) in points}")
            var big: Map[Integer, Integer] := {}
            for i := 0 to 9999 do
                big[i * 1024] := i
            for i := 0 to 9999 do
                if i mod 3 <> 0 then
                    remove(var big, i * 1024)
            var sum := 0
            for _, v in big do
                sum := sum + v
            writeln("{len(big)} {sum} {big[9999 * 1024]}")
            let a := {"x": 1, "y": 2}
            let b := {"y": 2, "x": 1}
            let c := {"x": 1, "y": 3}
            var nested := {"p": a, "q": c}
            let nested2 := {"q": c, "p": b}
            writeln("{a = b} {a = c} {a <> c} {nested = nested2}")
            nested["p"]["x"] := 100
            writeln("{nested["p"]["x"]} {a["x"]} {nested = nested2}")
            var box := Box(name: "b", tags: {"k": 1})
            box.tags["j"] := 2
            writeln("{box = Box(name: "b", tags: {"j": 2, "k": 1})} {len(box.tags)}")
            var lists: Map[String, List[Integer]] := {"a": [1]}
            add(var lists["a"], 2)
            lists["a"][0] := 7
            writeln("{lists["a"][0]} {lists["a"][1]} {len(lists["a"])}")
            var words: Map[String, Integer] := {}
            for w in ["a", "b", "a", "c", "a", "b"] do
                words[w] := getOr(words, w, 0) + 1
            for w, n in words do
                write("{w}:{n} ")
            let e: Map[String, String] := {}
            writeln("{get(get(m, "two"))} {isNone(get(m, "zzz"))} {len(e)} {e = {}} {"" in e}")
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(
            p.stdout.decode(),
            "two=22 three=3 four=4 3 29\n3 4 22 23 13\ntwo,three,four,five,\n"
            "rose sky false a b false\n3334 16668333 9999\ntrue false true true\n"
            "100 1 false\ntrue 2\n7 2 2\na:3 b:2 c:1 23 true 0 true false\n",
        )

    def test_missing_key(self):
        for body in ['let m := {"a": 1}\nwriteln(m["b"])', 'var m := {"a": 1}\ninc(var m["b"])']:
            with self.subTest(body):
                p = run_program(body)
                self.assertEqual(p.returncode, 70, p.stdout)
                self.assertIn(b"panic P0008: missing map key\n    at ", p.stderr)
                self.assertIn(b".pst:6:", p.stderr)  # where the program indexed the map


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Sets(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_operations(self):
        """Literals, `in`, `add`/`remove`, `len`, iteration in `ord` order, union,
        intersection and difference, `=`, and elements with a negative first `ord`."""
        p = run_program("""
            type
                Color = (Red, Green, Blue)

            var s: Set[Color] := {Color.Red}
            add(var s, Color.Blue)
            add(var s, Color.Blue)
            writeln("{len(s)} {Color.Red in s} {Color.Green in s} {Color.Blue in s}")
            for c in s do
                write("{c} ")
            let before := s
            remove(var s, Color.Red)
            remove(var s, Color.Green)
            let t := {Color.Green, Color.Blue}
            write("{len(before)} {len(union(s, t))} {len(intersection(s, t))} ")
            writeln("{len(difference(t, s))} {s = {Color.Blue}} {s = t}")
            var bs: Set[Byte] := {255, 0, 64, 63}
            for b in bs do
                write("{b} ")
            var ns: Set[Int8] := {127, -128, 0}
            for n in ns do
                write("{n} ")
            var none: Set[Boolean] := {}
            writeln("{len(bs)} {len(none)} {true in none} {true in {true}}")
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(
            p.stdout.decode(),
            "2 true false true\nRed Blue 2 2 1 1 true false\n0 63 64 255 -128 0 127 4 0 false true\n",
        )

    def test_bits(self):
        """Results wrap to the type's width, `shr` shifts in zeros, shifting by the width
        or more (or a negative count) gives 0."""
        p = run_program("""
            let x: Int8 := -1
            writeln("{band(12, 10)} {bor(12, 10)} {bxor(12, 10)} {bnot(0)} {shl(1, 63)} {shr(-1, 60)}")
            writeln("{shl(1, 64)} {shr(1, -1)} {popCount(-1)} {shr(x, 4)} {popCount(x)} {shl(x, 7)} {bnot(Byte(5))}")
            """, uses="IO, Bits")  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(p.stdout.decode(), "8 14 6 -1 -9223372036854775808 15\n0 0 64 15 8 -128 250\n")


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Refs(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_sharing(self):
        """`new`, fields read and written through refs (one box, shared), `deref` copies
        and replaces, `=` is identity, refs in records, lists and maps."""
        p = run_program("""
            type
                Node = record
                    value: Integer
                    next: Option[ref Node]

                Pair = record
                    a: ref Node
                    n: Integer

            function sum(n: ref Node): Integer
                var total := n.value
                case n.next of
                    Some(m): total := total + sum(m.value)
                    None: total := total + 0
                return total

            procedure bump(var x: Integer)
                x := x + 100

            let a := new(Node(value: 1, next: None))
            let b := new(Node(value: 2, next: Some(a)))
            write("{sum(b)} ")
            a.value := 10
            writeln(sum(b))
            let c := deref(a)
            a.value := 20
            write("{c.value} {a.value} {sum(b)} ")
            deref(a) := Node(value: 5, next: None)
            writeln("{c.value} {a.value} {sum(b)}")
            bump(var a.value)
            inc(var a.value)
            let a2 := a
            writeln("{a.value} {a2 = a} {a = b} {deref(a2) = deref(a)}")
            write("{Pair(a: a, n: 1) = Pair(a: a2, n: 1)} {Pair(a: a, n: 1) = Pair(a: b, n: 1)} ")
            var xs := [a, b]
            xs[1].value := 7
            let m := {"k": a}
            m["k"].value := 8
            write("{b.value} {a.value} ")
            let n := new(41)
            deref(n) := deref(n) + 1
            let s := new("hi")
            deref(s) := "{deref(s)}!"
            writeln("{deref(n)} {deref(s)} {len(xs)} {len(m)}")
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(
            p.stdout.decode(),
            "3 12\n10 20 22 10 5 7\n106 true false true\ntrue false 7 8 42 hi! 2 1\n",
        )

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_destroy_and_weak(self):
        """A resource record's destroy hook runs once, when the last strong reference
        goes, and may change the value; then weak references get None."""
        p = run_program("""
            type
                Res = record
                    id: Integer
                    log: List[String]

                Tree = record
                    kids: List[ref Tree]
                    parent: Option[Weak[Tree]]
                    name: String

            procedure destroy(var r: Res)
                r.id := r.id * 10
                add(var r.log, "bye")
                writeln("destroy {r.id} {len(r.log)}")

            function make(id: Integer): ref Res
                return new(Res(id: id, log: []))

            function alive(w: Weak[Res]): Boolean
                return case get(w) of
                    Some(_): true
                    None: false

            let x := make(1)
            var w := weak(x)
            if true then
                let y := make(2)
                w := weak(y)
                let y2 := y
                writeln("{alive(w)} {y2.id}")
            writeln("{alive(w)} {weak(x) = weak(x)} {weak(x) = w}")
            writeln(make(3).id)
            let root := new(Tree(kids: [], parent: None, name: "root"))
            let kid := new(Tree(kids: [], parent: Some(weak(root)), name: "kid"))
            add(var root.kids, kid)
            case kid.parent of
                Some(p):
                    case get(p.value) of
                        Some(r): writeln("{r.value.name} {len(r.value.kids)}")
                        None: writeln("orphan")
                None: writeln("no parent")
            writeln("end")
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(
            p.stdout.decode(),
            "true 2\ndestroy 20 1\nfalse true false\ndestroy 30 1\n3\nroot 1\nend\ndestroy 10 1\n",
        )


class DeferAndTry(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_defer_and_try(self):
        """Deferred statements run in reverse order as their block is left: at its end,
        by `return`, `break`, `continue` and a failing `try`. A failing `try` releases
        the temporaries of the expressions around it (in a string, a list, a map) and
        returns the `Err` or `None` in the function's own result type."""
        p = run_program("""

            function parse(s: String): Result[Integer, String]
                if s = "bad" then
                    return Err("not a number: {s}")
                return Ok(byteLen(s))

            function half(n: Integer): Option[Integer]
                if n mod 2 = 1 then
                    return None
                return Some(n div 2)

            function both(a: String, b: String): Result[String, String]
                defer writeln("  both done")
                let label := "{a}+{b}"
                defer
                    writeln("  leaving {label}")
                let parts := [label, "{try parse(a)}", "{try parse(b)}"]
                return Ok("{parts[0]}={len(parts)}")

            function pick(xs: List[String], n: Integer): Option[String]
                defer writeln("  pick done")
                let m := {"x": xs[0], "y": "{try half(n)}"}
                return Some("{m["x"]}{m["y"]}")

            function sum(xs: List[String]): Result[Integer, String]
                var total := 0
                for x in xs do
                    defer write("[{x}]")
                    if x = "skip" then
                        continue
                    if x = "stop" then
                        break
                    total := total + try parse(x)
                writeln("")
                return Ok(total)

            function check(s: String): Result[Integer, String]
                defer writeln("  check done")
                try parse(s)
                return Ok(1)

            procedure show(r: Result[Integer, String])
                case r of
                    Ok(v): writeln("ok {v.value}")
                    Err(x): writeln("err {x.error}")

            procedure showS(r: Result[String, String])
                case r of
                    Ok(v): writeln("ok {v.value}")
                    Err(x): writeln("err {x.error}")

            procedure showO(r: Option[String])
                case r of
                    Some(v): writeln("some {v.value}")
                    None: writeln("none")

            procedure order()
                defer writeln("  first")
                defer writeln("  second")
                if true then
                    defer writeln("  inner")
                    writeln("  body")
                writeln("  end")

            order()
            showS(both("ab", b: "cde"))
            showS(both("ab", b: "bad"))
            showS(both("bad", b: "x"))
            showO(pick(["p"], 4))
            showO(pick(["p"], 3))
            show(sum(["a", "skip", "bc", "stop", "zzz"]))
            show(sum(["a", "bad", "bc"]))
            show(check("bad"))
            show(check("ok"))
            """, uses="IO, Strings", under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(p.stdout.decode(), (
            "  body\n  inner\n  end\n  second\n  first\n"
            "  leaving ab+cde\n  both done\nok ab+cde=3\n"
            "  leaving ab+bad\n  both done\nerr not a number: bad\n"
            "  leaving bad+x\n  both done\nerr not a number: bad\n"
            "  pick done\nsome p2\n  pick done\nnone\n"
            "[a][skip][bc][stop]\nok 3\n[a][bad]err not a number: bad\n"
            "  check done\nerr not a number: bad\n  check done\nok 1\n"
        ))


class Iterators(unittest.TestCase):
    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_loops_and_adapters(self):
        """`for` over enums, strings, user iterators and System adapters; `next` and
        `iter` are found where the iterator type became concrete (`count` in System
        calls this program's `next`). Function values are named routines."""
        p = run_program("""
            type
                Color = (Red, Green, Blue)
                Countdown = record
                    n: Integer

            function next(var c: Countdown): Option[Integer]
                if c.n = 0 then
                    return None
                c.n := c.n - 1
                return Some(c.n + 1)

            function double(x: Integer): Integer
                return x * 2

            function isOdd(x: Integer): Boolean
                return x mod 2 = 1

            function shout(s: String): String
                return "{s}!"

            for c in Color do
                write("{c} ")
            for ch in "hé€𝄞" do
                write("[{ch}]")
            for x in Countdown(n: 3) do
                write(" {x}")
            writeln()
            let xs := [1, 2, 3, 4, 5]
            let words := ["a", "b", "c"]
            for x in xs.iter().filter(isOdd).map(double).skip(1).take(5) do
                write("{x} ")
            for w in words.iter().map(shout) do
                write(w)
            for p in zip(words.iter(), b: xs.iter()) do
                write(" {p.first}{p.second}")
            for i in enumerate(words.iter()) do
                write(" {i.index}={i.value}")
            writeln()
            var it := xs.iter()
            for _ in it do
                break
            writeln("{count(it)} {count(Countdown(n: 7))} {sum(xs.iter())} {count(chars("𝄞x"))}")
            writeln("{collect(xs.iter().map(double)) = [2, 4, 6, 8, 10]} {any(xs.iter(), isOdd)} {all(xs.iter(), isOdd)}")
            let f := double
            writeln("{get(find(xs.iter(), isOdd))} {f(21)}")
            """, under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))  # fmt: skip
        self.assertEqual((p.returncode, p.stderr), (0, b""))
        self.assertEqual(
            p.stdout.decode(),
            "Red Green Blue [h][é][€][𝄞] 3 2 1\n6 10 a!b!c! a1 b2 c3 0=a 1=b 2=c\n"
            "5 7 15 2\ntrue true false\n1 42\n",
        )

    def test_get_on_none(self):
        p = run_program("""
            function big(x: Integer): Boolean
                return x > 10
            writeln(get(find([1, 2].iter(), big)))
            """)
        self.assertEqual(p.returncode, 70)
        self.assertIn(b"panic P0005: get on None", p.stderr)


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

    def test_try_after_moved_var_argument(self):
        """A failing `try` would leave the moved-out place empty."""
        built, _ = compile_program("""
            type
                Box = record
                    items: List[String]

            procedure put(var xs: List[String], n: Integer)
                add(var xs, "{n}")

            function f(n: Option[Integer]): Option[Integer]
                var b := Box(items: [])
                put(var b.items, try n)
                return Some(len(b.items))

            writeln(isSome(f(Some(1))))
            """)
        self.assertEqual(
            [(d.code, d.span.line, d.span.col) for d in built.diagnostics], [("E0990", 14, 22)]
        )

    def test_only_programs(self):
        tmp = tempfile.mkdtemp(dir=_TMP.name)
        path = Path(tmp, "Lib.pst")
        path.write_text("unit Lib\n\nexport function one(): Integer\n    return 1\n")
        built = build(check(str(path)))
        self.assertEqual([d.code for d in built.diagnostics], ["E0990"])


if __name__ == "__main__":
    unittest.main()

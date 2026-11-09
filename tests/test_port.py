"""Tests of the compiler's Pasthon port.

Each front end stage has a dump (a command of the port's tool, or a program in
``compiler/``) that prints its result in a canonical form, which must match a golden file in
``tests/golden/``. With ``PST_UPDATE_GOLDEN=1`` the tests write the golden files instead.
The command line tool's runs must match golden files too, and the compiler must build
itself.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.tool import COMPILER, ROOT, STD_DIR, build_tool, install, pst_tool

_NATIVE = platform.system() == "Linux" and platform.machine() == "x86_64"
_TMP = tempfile.TemporaryDirectory()


def tearDownModule():
    _TMP.cleanup()


GOLDEN = ROOT / "tests" / "golden"
_UPDATE = os.environ.get("PST_UPDATE_GOLDEN") == "1"
_ERROR = re.compile(r"^(?:\S+ )?error E\d{4} ", re.M)


def normalize(text: str, case_dir: str, keep_std: bool = True) -> str:
    """A dump with the paths of the standard units and of the files in ``case_dir`` made
    relative; without ``keep_std``, also without the index entries in the standard units."""
    case_dir = case_dir.rstrip("/")
    text = text.replace(STD_DIR + "/", "std/").replace(case_dir + "/", "").replace(case_dir, ".")
    if keep_std:
        return text
    lines, keep = [], True
    for line in text.splitlines(keepends=True):
        if not line.startswith(" "):
            keep = not line.startswith(("def std/", "use std/"))
        if keep:
            lines.append(line)
    return "".join(lines)


def assert_golden(test: unittest.TestCase, golden: Path, actual: str):
    """Compares ``actual`` with the golden file, or writes it with PST_UPDATE_GOLDEN=1."""
    if _UPDATE:
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_bytes(actual.encode())
        return
    test.assertTrue(golden.exists(), f"{golden} is missing: run with PST_UPDATE_GOLDEN=1")
    expected = golden.read_bytes().decode()
    if actual != expected:
        got, want = actual.splitlines(), expected.splitlines()
        for i, (a, b) in enumerate(zip(got, want)):
            if a != b:
                test.fail(f"{golden.name}: line {i + 1}:\n  port:   {a}\n  golden: {b}")
        test.fail(f"{golden.name}: {len(got)} lines from the port, {len(want)} in the golden file")


def source_files() -> list[Path]:
    return sorted(
        p
        for d in ("std", "examples", "compiler")
        for p in (ROOT / d).glob("*.pst")
    )


class _Differential(unittest.TestCase):
    """Runs a dump of the port, a command of its tool or a dump program, and compares it
    with the golden files in ``tests/golden/<kind>/``."""

    kind = ""
    tool = ""
    command = ""  # a command of the port's tool, instead of a dump program
    snippets: dict[str, str] = {}

    @classmethod
    def setUpClass(cls):
        cls.exe = pst_tool() if cls.command else build_tool(cls.tool)

    def args(self, path: Path) -> list[str]:
        return [self.command, str(path)] if self.command else [str(path)]

    def dump(self, path: Path, under: tuple = (), keep_std: bool = True) -> str:
        r = subprocess.run([*under, self.exe, *self.args(path)], capture_output=True, timeout=120)
        out = r.stdout.decode()
        if self.command:  # status 1 if there are errors, as for `pst check`
            self.assertIn(r.returncode, (0, 1), r.stderr.decode())
            self.assertEqual(r.returncode == 1, _ERROR.search(out) is not None, path.name)
            self.assertEqual(r.stderr, b"")
        else:
            self.assertEqual(r.returncode, 0, r.stderr.decode())
        return normalize(out, str(path.parent), keep_std)

    def compare(self, path: Path, name: str, keep_std: bool = True):
        assert_golden(self, GOLDEN / self.kind / f"{name}.out", self.dump(path, keep_std=keep_std))

    def test_source_files(self):
        paths = source_files()
        self.assertGreater(len(paths), 10)
        for path in paths:
            with self.subTest(path.name):
                self.assertIsNone(_ERROR.search(self.dump(path)))

    def test_snippets(self):
        for name, text in self.snippets.items():
            with self.subTest(name):
                path = Path(_TMP.name, f"{self.kind}-{name}.pst")
                path.write_bytes(text.encode())
                self.compare(path, name)

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        self.dump(COMPILER / "Lexer.pst",
                  under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))


# Inputs that exercise the lexer's errors and corners.
SNIPPETS = {
    "tabs": "program P\n\tx := 1\nif a then\n  \tb\n",
    "indent": "if a then\n      b\n  c\nd\n",
    "dedent": "if a then\n    if b then\n        c\n  d\ne\n",
    "strings": 'let s := "a{b}c{d + "x{y}z"}e"\nlet t := "{ }"\nlet u := "\\{x}"\n',
    "unterminated": 'let s := "abc\nlet t := "a{b\nlet c := \'x\n',
    "placeholder-brackets": 'f("{g(x}")\nlet z := "{[1, 2}"\n',
    "escapes": 'let s := "\\n\\t\\r\\0\\\\\\"\\\'\\q\\u{41}\\u{1F600}\\u{D800}\\u{110000}\\u{}\\u{1234567}\\uz"\nlet e := "\\\n',
    "chars": "let a := 'x'\nlet b := ''\nlet c := 'ab'\nlet d := '\\n'\nlet e := '\\u{E9}'\nlet f := '\\\n",
    "numbers": (
        "let a := 0x1F + 0b1010 + 0o777 + 1_000 + 1.5 + 2.5e10 + 3.0E-2 + 1e5 + 1__0 + 1_\n"
        "let b := 0xG + 18446744073709551615 + 18446744073709551616 + 0x1_0000_0000_0000_0000\n"
        "let c := 1.e5 + 1..2 + 0x1.5 + 12abc + 0b + 9.9.9\n"
    ),
    "brackets": "f(a, [b, c)\nx := (1 +\n    2)\ny := ]\nz := (\nvar w := 3\nlet q := {a: (b}\n",
    "unclosed-end": "f(a, [b\n",
    "operators": "a := b <> c <= d >= e => f .. g ... h | i\nx ^ y & z ! w % v ; u @ \u201cq\u201d \u2018r\u2019 \u00e9\n",
    "markers": (
        "function f()\n    return 1\n# end function f\nrecord\n# end record\n# end if  \n"
        "# end while x\n# end functionx\n# end trait T extra\n# endif\n    # end for\n"
    ),
    "comments": "## doc\n# plain\nx := 1  # trailing\n    # indented comment\n",
    "line-endings": "\ufeffprogram P\r\nx := 1\rif a then\r\n    b\r\n",
    "underscore": "_ := f()\n_x := 2\nfor _ in xs do\n    a_b := 1\n",
    "unicode": "let s := \"h\u00e9llo \u4e16\u754c \U0001F600\"\nlet c := '\u00e9'\n# \u00e9\u00e9\n\u00e9 := 1\n",
    "empty": "",
    "only-comments": "# a\n\n## b\n",
    "no-final-newline": "x := 1",
    "statement-start": "f(a,\n  b)\ng(c,\nx.y := 1\nh(d,\n# comment\n\nreturn 2\nk(e,\n    let := 3)\n",
}


# Inputs that exercise the parser's errors, recovery and corners.
PARSER_SNIPPETS = {
    "header-missing": "x := 1\n",
    "header-twice": "program P\nprogram Q\nunit U\n",
    "uses-late": "program P\nx := 1\nuses IO\nuses A, B\n",
    "uses-broken": "program P\nuses IO,\nx := 1\n",
    "unit-statements": "unit U\nx := 1\nfunction f(): Integer\n    return 1\n",
    "end-markers": (
        "program P\n"
        "if a then\n    b()\n# end if\n"
        "while a do\n    b()\n# end for\n"
        "function f(x: Integer): Integer\n    return x\n# end function g\n"
        "procedure q()\n    b()\n# end procedure\n# end procedure\n"
        "repeat\n    b()\nuntil a\n# end repeat\n"
        "x := 1\n# end if\n"
        "# end while\n"
        "type\n    R = record\n        a: Integer\n    # end record R\n    T = trait\n        b: Integer\n    # end trait X\n"
        "defer\n    b()\n# end defer\n"
        "defer b()\n# end defer\n"
        "case x of\n    1: b()\n# end case\n"
        "for i := 1 to 2 do\n    b()\n# end for\n"
        "for v in xs do\n    b()\n# end for\n"
        "test \"t\"\n    b()\n# end test\n"
        'extern "c"\n    function f(): Integer\n# end extern\n'
        "if a then\n    b(\n# end if\n"
    ),
    "indentation": "program P\nx := 1\n    y := 2\n        z := 3\nif a then\nb()\nwhile a do\n",
    "types": (
        "program P\n"
        "type\n"
        "    export E = (A, B, C,)\n"
        "    F = (A B)\n"
        "    G = (A,\n"
        "    S = 1..10\n"
        "    U = Integer | String | ref Node | function(Integer): String\n"
        "    V[T, N: Integer] = List[T]\n"
        "    W[T: Show + Eq] = Map[String, List[T]]\n"
        "    X = Geometry.Shape\n"
        "    Y = ref T\n"
        "    Z = procedure(Integer, String)\n"
        "    A = 1 +\n"
        "    B = Array[Integer, N * 2]\n"
        "    C = 3\n"
        "    D = record\n"
        "    T = trait\n"
        "        export x: Integer\n"
        "        function f(this): Integer\n"
        "        procedure g(var this, y: Integer)\n"
        "    R = record\n"
        "        ## A field.\n"
        "        export x: Integer := 3\n"
        "        y\n"
        "        z: Integer\n"
        "type\n"
        "export type\n    Q = Integer\n"
        "type Q = Integer\n"
        "type\n"
    ),
    "consts": "program P\nconst\n    A = 1\n    export B := 2\n    C\n    D = 1 + \n    E = \"x\"\nconst\n",
    "routines": (
        "program P\n"
        "## Doc one.\n##\n##   Doc two.\n"
        "export function f[T: Eq](a, b: T, var c: List[T] := [], ...): Integer\n"
        "    requires a = b\n    ensures result > 0\n    requires c\n    return 1\n"
        "function g()\n    return\n"
        "procedure h(): Integer\n    x()\n"
        "function k\n    x()\n"
        "function m(this, var this, x: Integer = 3): ref T\n    x()\n"
        "export unsafe procedure n()\n    x()\n"
        "unsafe export procedure o()\n    x()\n"
        "export x := 1\n"
        "procedure p()\n"
        "procedure q()\n    requires a\n"
        "procedure r() x()\n"
    ),
    "externs": (
        "program P\n"
        'extern "libc.so.6"\n'
        "    ## Writes.\n"
        '    export function write(fd: Integer, buf: Pointer, n: Integer): Integer as "write"\n'
        "    procedure exit(code: Integer)\n"
        "    x := 1\n"
        "    type\n"
        "        Tm = record\n            sec: Integer\n            min: Integer := 3\n"
        "        G[T] = record\n            a: T\n"
        "        E = (A, B)\n"
        '    function f(): Integer as "a{b}"\n'
        'extern "{x}"\n    procedure g()\n'
        "extern\n    procedure g()\n"
        'extern "c"\n'
    ),
    "tests": 'program P\ntest "adds"\n    assert 1 + 1 = 2, "math"\ntest x\n    assert true\ntest "e"\n',
    "statements": (
        "program P\n"
        "var x: Integer := 1\nlet y := 2\nvar z\nlet w: Integer\nvar _ := 3\n_ := f()\n_n := g()\n"
        "x := if a then 1 else if b then 2 else 3\n"
        "x := case y of\n    1, 2: 3\n    4..5: 6\n    Circle(c): c\n    Square(_): 0\n    else: 7\n"
        "case y of\n    1: a()\n    2:\n        b()\n    Some(v): c(v)\n    else: d()\n"
        "case y of\n    1:\n"
        "for i := 10 downto 1 do\n    a()\nfor k, v in m do\n    a()\nfor _ in xs do\n    a()\nfor _ := 1 to 2 do\n    a()\n"
        "for i = 1 to 2 do\n    a()\nfor i := 1 do\n    a()\n"
        "while a do\n    break\n    continue\n"
        "repeat\n    a()\nuntil b\nrepeat\n    a()\n"
        "if a then\n    b()\nelse if c then\n    d()\nelse\n    e()\n"
        "if a then b()\n"
        "defer a()\ndefer\n    a()\n"
        "return\nreturn 1\nreturn case x of\n    1: 2\n"
        "assert a\nassert a, \"m\"\n"
        "requires a\nelse\nuntil a\n"
        "const C = 1\n"
        "function f()\n    procedure g()\n        x()\n    type T = Integer\n    return 1\n"
        "x = 1\nx + 1\nf() := 2\nderef(p) := 3\na.b[c] := 4\ntry f()\n1 < 2 < 3\n"
        "x := 1 2\n"
        "x := (1 +\n"
    ),
    "expressions": (
        "program P\n"
        "x := -a + b * c div d mod e - f / g\n"
        "x := not a and b or not c = d\n"
        "x := a <> b\nx := a in b\nx := a <= b >= c\n"
        "x := try f(a, b: c, var d)\n"
        "x := f(a: 1, b)\n"
        "x := g[Integer](1) + h[ref T, function(): T, This]\n"
        "x := a.b.c[1, 2].d(3)\n"
        "x := [1, 2, 3] + [] + [x,]\n"
        "x := {} + {1, 2} + {\"a\": 1, \"b\": 2,} + {1,}\n"
        "x := {1: 2, 3}\n"
        "x := deref(p).x + deref p\n"
        "x := 1 + if a then 2 else 3\n"
        "x := 1 + case y of\n    1: 2\n"
        "x := if a then 1\n"
        "x := _ + 1\n"
        "x := 0x1F + 1_000 + 1.5e3 + 1_0.2_5 + 'a' + '\\n' + true + false + This + this\n"
        "x := \"a{b}c{d + 1}\" + \"{e}\" + \"{f}{g}\"\n"
        "x := \"a{b c}\"\n"
        "x := ()\nx := (1\nx := f(1\nx := a.\nx := a.1\n"
        "x := case\n"
    ),
    "case-expr-arms": "program P\nx := case y of\n    1:\n        2\n    3 4\n    5: 6\n",
    "lex-errors": "program P\nx := 'ab' +\ny := \"abc\nz := 1 $ 2\nw := (1,\n    $)\nv := 1 +\n",
    "docs": (
        "program P\n## Orphan.\n\n## The type.\ntype\n    ## T doc.\n    T = Integer\n"
        "## The const.\nconst\n    ## C doc.\n    C = 1  ## not a doc\n"
        "##No space.\n##  Two spaces.\nfunction f(): Integer\n    return 1\n"
    ),
    "empty": "",
    "only-header": "unit U",
    "header-errors": "program\nx := 1\n",
    "header-extra": "program P Q\nx := 1\n",
}

class _FrontEnd(_Differential):
    """A dump that also runs on the ``.pst`` files in its golden directory, which are the
    inputs of the former Python compiler's lexer and parser tests."""

    def test_golden_inputs(self):
        paths = sorted((GOLDEN / self.kind).glob("*.pst"))
        self.assertGreater(len(paths), 50)
        for path in paths:
            with self.subTest(path.stem):
                self.compare(path, path.stem)


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Lexer(_FrontEnd):
    kind = command = "tokens"
    snippets = SNIPPETS


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Parser(_FrontEnd):
    kind = command = "ast"
    snippets = PARSER_SNIPPETS

    def test_never_crashes(self):
        """Every prefix of a real program yields a tree."""
        src = (ROOT / "examples" / "Inventory.pst").read_bytes()
        path = Path(_TMP.name, "prefix.pst")
        for n in range(0, len(src), 7):
            with self.subTest(n):
                path.write_bytes(src[:n])
                self.dump(path)


# Programs, each a set of files (the first is checked), that exercise the resolver's errors
# and corners.
RESOLVE_SNIPPETS = {
    "names": {
        "Main.pst": (
            "program Main\nuses IO\n"
            "let count := 1\nvar total := 2\nlet unused := 3\n"
            "writeln(cuont)\nwriteln(totl + count)\nwriteln(nothingLikeIt)\n"
            "function f(x: Integer): Integer\n    return x + total\n"
            "type\n    R = record\n        a: Integer := count\n"
            "procedure p(a: Integer, b: Integer)\n    let a := 2\n    var b2 := a\n    writeln(b)\n"
            "for i := 1 to 3 do\n    let unusedToo := i\nfor x in [1] do\n    writeln(1)\n"
            "for _ in [1] do\n    writeln(1)\n"
        ),
    },
    "declarations": {
        "Main.pst": (
            "program Main\n"
            "type\n    E = (A, B, A)\n    R = record\n        x: Integer\n        x: String\n"
            "    E = Integer\n    T[X, X] = List[X]\n"
            "    Tr = trait\n        f: Integer\n        procedure go(this)\n        function me(): This\n"
            "    Bad = This\n"
            "const\n    C = 1\n    C = 2\n"
            "function C(): Integer\n    return 1\n"
            "function g(): Integer\n    return 1\nfunction g(x: Integer): Integer\n    return x\n"
            'extern "pst"\n    function h(): Integer\n'
            "let e := E.C\nlet e2 := E.B\nlet e3 := E.Aa\nwriteln(e, e2, e3)\n"
            "procedure use[T: Tr](t: T)\n    go(t)\n    writeln(this)\n    let q: This := t\n"
        ),
    },
    "casing": {
        "Main.pst": (
            "program Main\ntype\n    point = record\n        X: Integer\n    Color = (red, Green)\n"
            "const\n    max = 3\n"
            "function Area(p: point): Integer\n    let Big := p.X\n    return Big + max\n"
            "let q := Area(point(X: 1))\nwriteln(q)\n"
            "procedure _hidden()\n    writeln(1)\n"
        ),
    },
    "contracts": {
        "Main.pst": (
            "program Main\n"
            "function f(x: Integer): Integer\n    requires x > 0\n    ensures result > old(x)\n    return x + 1\n"
            "procedure p(var x: Integer)\n    ensures x = old(x) + result\n    x := x + 1\n"
            "function old(): Integer\n    return 1\n"
            "test \"t\"\n    let y := f(1)\n    assert y > 0, \"positive\"\n"
        ),
    },
    "statements": {
        "Main.pst": (
            "program Main\nuses IO\n"
            "type\n    Shape = Circle | Square\n    Circle = record\n        r: Integer\n    Square = record\n        s: Integer\n"
            "let s: Shape := Circle(r: 1)\n"
            "case s of\n    Circle(c):\n        writeln(c.r)\n    Square(sq):\n        writeln(1)\n    Missing(m): writeln(m)\n"
            "let n := case 3 of\n    1, 2: 10\n    3..5: 20\n    else: 0\n"
            "repeat\n    let k := n\nuntil k > 0\n"
            "var z := 0\nz := z + 1\nzz := 2\n"
            "defer writeln(z)\n"
            "while z < 3 do\n    z := z + 1\n"
            "if z > 1 then\n    writeln(1)\nelse if z < 0 then\n    writeln(2)\nelse\n    writeln(3)\n"
            "let w := if z > 1 then 1 else 2\nwriteln(\"{w} {undefinedHere}\")\n"
            "let m := {1: [2], 3: [4]}\nlet st := {1, 2}\nwriteln(m[1], st, -z, not true, IO.writeln, IO.nope, Strings.x)\n"
            "_ := deref(new(1))\nlet xs: List[Integer] := []\nwriteln(xs[0], len[Integer](xs))\n"
        ),
    },
    "units": {
        "Main.pst": (
            "program Main\nuses IO, A, B, A, Main, Missing, Prog, Missing, Alias, Bad, Raw\n"
            "writeln(shared, Shared)\nwriteln(onlyA)\nwriteln(A.hidden)\nwriteln(A.Kind.One)\nwriteln(A.Kind.Three)\n"
            "writeln(fromB(), common())\nlet k: A.Kind := A.Kind.Two\nlet j: A.Nope := k\nwriteln(j)\n"
        ),
        "A.pst": (
            "unit A\nuses B\n"
            "type\n    export Kind = (One, Two)\n"
            "const\n    export Shared = 1\n"
            "export function shared(): Integer\n    return 1\n"
            "export function onlyA(): Integer\n    return 2\n"
            "function hidden(): Integer\n    return 3\n"
            "export function common(): Integer\n    return 1\n"
        ),
        "B.pst": (
            "unit B\nuses A\n"
            "const\n    export Shared = 2\n"
            "export function shared(): Integer\n    return 1\n"
            "export function fromB(): Integer\n    return 1\n"
            "export function common(x: Integer := 0): Integer\n    return x\n"
            "const\n    export S = Shared\n"
        ),
        "Prog.pst": "program Prog\n",
        "Alias.pst": "unit Other\nexport function aliased(): Integer\n    return 1\n",
        "Bad.pst": "unit Bad\nuses Bad\nconst\n    export X = 1\n",
    },
    "raw": {
        "Main.pst": "program Main\nuses Raw, Cut\n",
        "Raw.pst": b"unit Raw\n# caf\xc3\xa9 \xed\xa0\x80\n",
        "Cut.pst": b"unit Cut\n\xe2\x82",
    },
    "wrong-name": {
        "main-1.pst": "program Main\nlet x := 1\nwriteln(x)\n",
    },
    "wrong-name-fixable": {
        "Prog_2.pst": "unit Main\n",
    },
    "invalid-main": {
        "Main.pst": b"program Main\n\xf5\n",
    },
    "unit-no-header": {
        "Main.pst": "uses IO\nwriteln(1)\n",
    },
}

@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Resolve(_Differential):
    """Resolution without type checking. The golden files leave out the index entries in
    the standard units, except for a standard unit itself."""

    kind = "resolve"
    tool = "ResolveDump"

    def args(self, path: Path) -> list[str]:
        return [STD_DIR, str(path)]

    def test_snippets(self):
        for name, files in RESOLVE_SNIPPETS.items():
            with self.subTest(name):
                d = Path(tempfile.mkdtemp(dir=_TMP.name))
                for f, text in files.items():
                    Path(d, f).write_bytes(text if isinstance(text, bytes) else text.encode())
                self.compare(d / next(iter(files)), name, keep_std=False)

    def test_missing_file(self):
        self.compare(Path(_TMP.name, "does-not-exist.pst"), "missing-file")

    def test_std_unit(self):
        self.compare(Path(STD_DIR, "Strings.pst"), "std-strings")


def main_file(d: Path) -> Path:
    """The file to check in a directory of ``tests/golden/check``: ``Main.pst``, or its
    only file."""
    if (d / "Main.pst").exists():
        return d / "Main.pst"
    [path] = d.glob("*.pst")
    return path


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Check(_Differential):
    """Resolution and type checking of the programs in the directories of
    ``tests/golden/check``, taken from the former Python compiler's tests."""

    kind = "check"
    tool = "CheckDump"

    def args(self, path: Path) -> list[str]:
        return [STD_DIR, str(path)]

    def test_checker_tests(self):
        dirs = sorted(d for d in (GOLDEN / "check").iterdir() if d.is_dir())
        self.assertGreater(len(dirs), 100)
        for d in dirs:
            with self.subTest(d.name):
                self.compare(main_file(d), d.name, keep_std=False)

    def test_missing_file(self):
        self.compare(Path(_TMP.name, "does-not-exist.pst"), "missing-file")

    def test_std_unit(self):
        self.compare(Path(STD_DIR, "Strings.pst"), "std-strings")

    def test_example(self):
        self.compare(ROOT / "examples" / "Inventory.pst", "inventory", keep_std=False)

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        self.dump(ROOT / "examples" / "Inventory.pst",
                  under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"))


def asm_script() -> str:
    """A script that calls every assembler instruction with registers, displacements
    and immediates at the edges of each encoding."""
    regs = range(16)
    bases = (0, 3, 4, 5, 7, 8, 12, 13, 15)
    disps = (0, 8, -8, 127, 128, -128, -129, 4096, -100000)
    imms = (0, 1, -1, 127, 128, -128, -129, 2**31 - 1, -(2**31))
    lines = ["new_label top L", "label top", "data msg 1 104,105,0", "data msg2 1 104,105,0",
             "data tbl 8 0,0,0,0,0,0,0,0,1", "data empty 1 -", "new_label fwd X"]
    for d in regs:
        for s in regs:
            lines += [f"{op} {d} {s}" for op in ("mov", "add", "xor", "or_", "and_", "sub", "cmp", "test",
                                                  "test32", "imul", "popcnt", "movzx8", "xor32")]
        for op in ("neg", "not_", "shl_cl", "shr_cl", "mul", "div", "idiv", "inc", "dec", "push", "pop",
                   "call_reg"):
            lines.append(f"{op} {d}")
        for v in (0, 1, 255, 2**32 - 1, 2**32, -1, -(2**31), -(2**31) - 1, 2**63 - 1, -(2**63)):
            lines.append(f"mov_imm {d} {v}")
        for v in imms:
            lines += [f"{op} {d} {v}" for op in ("add_imm", "sub_imm", "cmp_imm", "and_imm")]
        lines += [f"xor_imm32 {d} 1", f"or32_imm {d} 65535", f"and32_imm {d} -1", f"shr_imm {d} 3",
                  f"shl_imm {d} 63", f"sub_imm32_fixed {d}", f"lea_data {d} msg", f"lea_code {d} top",
                  f"load_global {d} argv", f"store_global argc {d}"]
        for bits in (8, 16, 32, 64):
            lines += [f"extend {d} {bits} true", f"extend {d} {bits} false"]
        for cc in ("o", "no", "b", "ae", "e", "ne", "be", "a", "s", "ns", "l", "ge", "le", "g"):
            lines.append(f"setcc {cc} {d}")
        for b in bases:
            for disp in disps:
                lines += [f"load {d} {b} {disp}", f"store {b} {disp} {d}", f"store8 {b} {disp} {d}",
                          f"load8 {d} {b} {disp}", f"lea {d} {b} {disp}", f"cmp_mem {d} {b} {disp}"]
                for bits in (8, 16, 32, 64):
                    lines += [f"load_w {d} {b} {disp} {bits} true", f"load_w {d} {b} {disp} {bits} false",
                              f"store_w {b} {disp} {d} {bits}"]
    for b in bases:
        for disp in disps:
            lines += [f"store8_imm {b} {disp} 200", f"store_imm {b} {disp} -5", f"inc_mem {b} {disp}",
                      f"dec_mem {b} {disp}", f"cmp_mem_imm {b} {disp} -1"]
    for cc in ("o", "e", "ne", "l", "g", "ae"):
        lines += [f"jcc {cc} top", f"jcc {cc} fwd"]
    lines += ["jmp top", "jmp fwd", "call top", "call fwd", "call_import malloc", "call_import free",
              "call_import malloc", "emit 144 15 11", "cqo", "leave", "ret", "patch32 3 -2",
              "code_ref tbl 0 fwd", "label fwd"]
    lines += ["track", "push 0", "own", "push 8", "push 3", "own", "pop 1", "add_imm 4 8",
              "push 2", "push 2", "add_imm 4 16", "sub_imm 4 8", "untrack", "push 0", "pop 0", "ret"]
    return "".join(line + "\n" for line in lines)


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Asm(unittest.TestCase):
    """The assembler and ELF writer, run on an instruction script."""

    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("AsmDump")

    def compare(self, script: str, name: str):
        path = Path(_TMP.name, "asm.txt")
        path.write_text(script)
        r = subprocess.run([self.exe, str(path)], capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        assert_golden(self, GOLDEN / "asm" / f"{name}.out", r.stdout.decode())

    def test_instructions(self):
        self.compare(asm_script(), "instructions")

    def test_empty(self):
        self.compare("", "empty")


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Cli(unittest.TestCase):
    """The command line tool (compiler/Pst.pst): its exit status and output, which must
    match the golden files in ``tests/golden/cli``, one per run."""

    PROGRAMS = {
        "Args.pst": "program Args\nuses IO, OS\nfor a in args() do\n    writeln(a)\n",
        "Exit.pst": (
            "program Exit\nuses IO, OS\nwriteln(len(args()))\nlet xs: List[Integer] := []\n"
            "if len(args()) > 0 then\n    writeln(xs[0])\nexit(3)\n"
        ),
        "Bad.pst": (
            "program Bad\nuses IO\nfunction f(): Integer\n    return 1\nf()\n"
            "writeln(na\u00efve)\nwriteln(x\U0001d11e)\nlet s := 1 \x01 \x7f \x0c\n"
        ),
        "Lib.pst": "unit Lib\nexport function one(): Integer\n    return 1\n",
        "Shop.pst": (
            "unit Shop\nuses IO\nfunction double(x: Integer): Integer\n    return 2 * x\n"
            'test "double doubles"\n    assert double(2) = 4\n'
            'test "half \u00e9 wrong"\n    writeln("about to fail")\n    assert double(1) = 3\n'
            'test "bounds"\n    let xs: List[Integer] := []\n    writeln(xs[1])\n'
            'test "halts"\n    halt(4)\n'
        ),
    }

    @classmethod
    def setUpClass(cls):
        cls.exe = pst_tool()
        cls.work = Path(tempfile.mkdtemp(dir=_TMP.name))
        for name, text in cls.PROGRAMS.items():
            (cls.work / name).write_text(text)

    def setUp(self):
        self.runs = 0

    def compare(self, *args: str, outputs: tuple = ()):
        """Runs ``pst args`` and compares the result with the next golden file of the test;
        each file in ``outputs`` must be written, as an executable."""
        for o in outputs:
            Path(self.work, o).unlink(missing_ok=True)
        r = subprocess.run([self.exe, *args], cwd=self.work, capture_output=True, timeout=600)
        self.runs += 1
        name = f"{self.id().rsplit('.', 1)[1]}.{self.runs}.out"
        stdout, stderr = r.stdout.decode(), r.stderr.decode()
        assert_golden(self, GOLDEN / "cli" / name,
                      f"$ pst {' '.join(args)}\nexit {r.returncode}\n--- stdout\n{stdout}"
                      f"--- stderr\n{stderr}")
        for o in outputs:
            self.assertEqual(Path(self.work, o).stat().st_mode & 0o777, 0o755)
        return r.returncode, stdout, stderr

    def test_check(self):
        self.assertEqual(self.compare("check", "Args.pst")[0], 0)
        self.assertEqual(self.compare("check", "Lib.pst", "--json")[0], 0)
        self.assertEqual(self.compare("check", "Bad.pst")[0], 1)
        self.assertIn("\\ud834\\udd1e", self.compare("check", "--json", "Bad.pst")[1])
        self.compare("check", "missing.pst", "--json")

    def test_build(self):
        self.compare("build", "Args.pst", outputs=("Args",))
        self.compare("build", "Args.pst", "-o", "a.out", outputs=("a.out",))
        self.compare("build", "--unchecked", "--output", "e", "Exit.pst", outputs=("e",))
        self.assertEqual(self.compare("build", "Bad.pst", "--json")[0], 1)
        self.assertEqual(self.compare("build", "Lib.pst")[0], 1)

    def test_run(self):
        self.assertEqual(self.compare("run", "Args.pst", "x", "-- y", "--json")[1],
                         "x\n-- y\n--json\n")
        self.assertEqual(self.compare("run", "Args.pst", "--", "a", "b c")[1], "a\nb c\n")
        self.assertEqual(self.compare("run", "Exit.pst")[0], 3)
        self.assertEqual(self.compare("run", "Exit.pst", "--", "x")[0], 70)
        self.assertEqual(self.compare("run", "--unchecked", "Bad.pst")[0], 1)

    def test_test(self):
        self.assertEqual(self.compare("test", "Shop.pst")[0], 1)
        self.assertEqual(self.compare("test", "Shop.pst", "--json", "--unchecked")[0], 1)
        self.assertEqual(self.compare("test", "Shop.pst", "--filter", "double")[0], 0)
        self.assertEqual(self.compare("test", "Lib.pst")[1], "0 passed, 0 failed\n")
        self.assertEqual(self.compare("test", ".", "--filter", "o")[0], 1)
        self.compare("test", "Bad.pst", "--json")

    def test_dumps(self):
        for command in ("tokens", "ast"):
            with self.subTest(command):
                r = subprocess.run([self.exe, command, "Args.pst"], cwd=self.work,
                                   capture_output=True)
                self.assertEqual((r.returncode, r.stderr), (0, b""))
                r = subprocess.run([self.exe, command, "missing.pst"], cwd=self.work,
                                   capture_output=True)
                self.assertEqual(r.returncode, 1)
                self.assertIn(b"pst: error:", r.stderr)

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        out = Path(_TMP.name, "inventory")
        r = subprocess.run(["valgrind", "-q", "--leak-check=full", "--error-exitcode=9", self.exe,
                            "build", str(ROOT / "examples" / "Inventory.pst"), "-o", str(out)],
                           capture_output=True, timeout=600)
        self.assertEqual((r.returncode, r.stderr.decode()), (0, ""))

    def test_usage(self):
        for args in ([], ["frob"], ["build"], ["build", "Args.pst", "-o"],
                     ["tokens", "Args.pst", "--json"], ["ast", "--unchecked", "Args.pst"]):
            with self.subTest(args):
                r = subprocess.run([self.exe, *args], capture_output=True)
                self.assertEqual(r.returncode, 2)


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Bootstrap(unittest.TestCase):
    """The compiler compiles itself: it and the compilers it builds are the same
    executable, byte for byte."""

    def test_fixpoint(self):
        compiler = pst_tool()
        source = str(COMPILER / "Pst.pst")
        previous = Path(compiler).read_bytes()
        for stage in (1, 2):
            where = Path(_TMP.name, f"stage{stage}")
            out = os.path.join(_TMP.name, f"pst{stage}")
            r = subprocess.run([compiler, "build", source, "-o", out], capture_output=True,
                               timeout=600)
            self.assertEqual((r.returncode, r.stderr.decode()), (0, ""))
            got = Path(out).read_bytes()
            self.assertTrue(got == previous, f"stage {stage} differs from stage {stage - 1}")
            previous, compiler = got, install(out, where)


del _Differential, _FrontEnd

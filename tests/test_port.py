"""Differential tests: the compiler's Pasthon port against the Python compiler.

Each port stage has a dump program in ``compiler/`` that prints its result in a canonical
form; the same form is printed here from the Python stage, and the two must match.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from pst.emit import build
from pst import ast
from pst.lexer import lex
from pst.parser import parse
from pst.source import Span
from pst.resolve import Checker, Resolver as PyResolver, STD_DIR, check
from pst.symbols import LOCALS
from pst.tokens import TokenKind
from pst.types import show

ROOT = Path(__file__).resolve().parent.parent
COMPILER = ROOT / "compiler"
_NATIVE = platform.system() == "Linux" and platform.machine() == "x86_64"
_TMP = tempfile.TemporaryDirectory()


def tearDownModule():
    _TMP.cleanup()


def build_tool(name: str) -> str:
    """Builds ``compiler/<name>.pst`` and returns the executable's path."""
    result = check(str(COMPILER / f"{name}.pst"))
    assert not result.has_errors, [d.human() for d in result.diagnostics]
    built = build(result, True)
    assert built.executable is not None, [d.human() for d in built.diagnostics]
    exe = os.path.join(_TMP.name, name)
    Path(exe).write_bytes(built.executable)
    os.chmod(exe, 0o755)
    return exe


def quote(s: str) -> str:
    out = ['"']
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\n":
            out.append("\\n")
        elif ord(c) < 32 or ord(c) == 127:
            out.append(f"\\u{{{ord(c):X}}}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


def where(s) -> str:
    return f"{s.line}:{s.col}-{s.end_line}:{s.end_col}"


def kind_names() -> dict[TokenKind, str]:
    """The Pasthon name of each token kind: the enums list the kinds in the same order."""
    text = (COMPILER / "Tokens.pst").read_text()
    body = re.search(r"export TokenKind = \((.*?)\)", text, re.S).group(1)
    names = [n.strip() for n in body.split(",") if n.strip()]
    assert len(names) == len(TokenKind), (len(names), len(TokenKind))
    return dict(zip(TokenKind, names))


def value_of(t) -> str:
    v = t.value
    if t.kind is TokenKind.REAL:
        return f"real {t.text.replace('_', '')}"
    if t.kind is TokenKind.INT:
        return f"int {v}"
    if t.kind is TokenKind.CHAR:
        return f"char {ord(v) if v else 0}"
    if t.kind is TokenKind.END_MARKER:
        return f"marker {v[0]} {v[1] or '-'}"
    if isinstance(v, str):
        return f"text {quote(v)}"
    return "-"


def lex_dump(text: str, file: str) -> str:
    names = kind_names()
    r = lex(text, file)
    lines = [f"{names[t.kind]} {where(t.span)} {quote(t.text)} {value_of(t)}" for t in r.tokens]
    for c in r.comments:
        lines.append(f"comment {where(c.span)} {quote(c.text)} {'true' if c.is_doc else 'false'}")
    for d in r.diagnostics:
        lines.append(f"{d.severity.value} {d.code} {where(d.span)} {quote(d.message)}")
        for f in d.fixes:
            lines.append(f"  fix {quote(f.description)}")
            for e in f.edits:
                lines.append(f"    edit {where(e.span)} {quote(e.text)}")
    return "".join(line + "\n" for line in lines)


def diagnostic_lines(diagnostics) -> list[str]:
    lines = []
    for d in diagnostics:
        lines.append(f"{d.severity.value} {d.code} {where(d.span)} {quote(d.message)}")
        for f in d.fixes:
            lines.append(f"  fix {quote(f.description)}")
            for e in f.edits:
                lines.append(f"    edit {where(e.span)} {quote(e.text)}")
    return lines


def ast_dump(text: str, file: str) -> str:
    """The tree as ``compiler/AstDump.pst`` prints it: one node per line, indented by
    depth, with scalar fields on the node's line and child nodes below it."""
    r = parse(text, file)
    lines = []

    def scalar(node, name, v) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(node, ast.RealLit):
            return text[node.span.start : node.span.end].replace("_", "")
        if isinstance(node, ast.CharLit):
            return str(ord(v) if v else 0)
        if isinstance(v, int):
            return str(v)
        if isinstance(v, str):
            return quote(v)
        if isinstance(v, Span):
            return where(v)
        if name == "part_spans":
            return "[" + ",".join(where(s) for s in v) + "]"
        return "[" + ",".join(quote(x) for x in v) + "]"

    def walk(node, depth: int, label: str):
        pad = "  " * depth
        scalars, children = [], []
        for f in fields(node):
            v = getattr(node, f.name)
            if f.name == "span" or v is None or (isinstance(v, list) and not v):
                continue
            if isinstance(v, ast.Node):
                children.append((f"{f.name}: ", v))
            elif isinstance(v, list) and not isinstance(v[0], Span) and f.name != "parts" or (
                f.name == "parts" and isinstance(node, ast.InterpString)
            ):
                children.extend((f"{f.name}[{i}]: ", x) for i, x in enumerate(v))
            else:
                scalars.append(f" {f.name}={scalar(node, f.name, v)}")
        kind = type(node).__name__
        lines.append(f"{pad}{label}{kind} {where(node.span)}{''.join(scalars)}")
        for lab, child in children:
            if isinstance(child, str):
                lines.append(f"{pad}  {lab}Text {quote(child)}")
            else:
                walk(child, depth + 1, lab)

    walk(r.file, 0, "")
    lines.extend(diagnostic_lines(r.diagnostics))
    return "".join(line + "\n" for line in lines)


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


def resolve_dump(path: str, std_dir: str = STD_DIR) -> str:
    """Resolution without type checking, as ``compiler/ResolveDump.pst`` prints it: the
    symbol index, then the diagnostics of every file."""
    c = Checker(std_dir)
    std = os.path.dirname(os.path.abspath(path)) == os.path.abspath(std_dir)
    main = c.load(path, std=std)
    if main is not None:
        c.check_cycles(main)
        others = [u for u in c.units.values() if u is not main]
        for unit in [main] + others:
            PyResolver(c, unit).run()
    c.diagnostics.sort(key=lambda d: (d.file != (main.file if main else ""), d.file, d.span.start))
    lines = []
    for r in c.index.refs:
        lines.append(f"{'def' if r.is_def else 'use'} {r.file} {where(r.span)}")
        for t in r.targets:
            lines.append(f"  {t.describe()} @ {t.location()}")
    for d in c.diagnostics:
        lines.append(f"{d.file} {d.severity.value} {d.code} {where(d.span)} {quote(d.message)}")
        for f in d.fixes:
            lines.append(f"  fix {quote(f.description)}")
            for e in f.edits:
                lines.append(f"    edit {where(e.span)} {quote(e.text)}")
    return "".join(line + "\n" for line in lines)


def check_dump(path: str, std_dir: str = STD_DIR) -> str:
    """Resolution and type checking, as ``compiler/CheckDump.pst`` prints it: the symbol
    index with the type of each local variable at its definition, then the diagnostics."""
    result = check(path, std_dir)
    lines = []
    for r in result.index.refs:
        lines.append(f"{'def' if r.is_def else 'use'} {r.file} {where(r.span)}")
        for t in r.targets:
            lines.append(f"  {t.describe()} @ {t.location()}")
            if r.is_def and t.kind in LOCALS:
                lines.append(f"    : {'?' if t.type is None else show(t.type)}")
    for d in result.diagnostics:
        lines.append(f"{d.file} {d.severity.value} {d.code} {where(d.span)} {quote(d.message)}")
        for f in d.fixes:
            lines.append(f"  fix {quote(f.description)}")
            for e in f.edits:
                lines.append(f"    edit {where(e.span)} {quote(e.text)}")
    return "".join(line + "\n" for line in lines)


def source_files() -> list[Path]:
    return sorted(
        p
        for d in ("pst/units", "examples", "compiler")
        for p in (ROOT / d).glob("*.pst")
    )


class _Differential(unittest.TestCase):
    """Runs a dump program and the matching Python dump on the same files."""

    tool = ""
    snippets: dict[str, str] = {}

    @staticmethod
    def expected(text: str, file: str) -> str:
        raise NotImplementedError

    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool(cls.tool)

    def args(self, path: Path) -> list[str]:
        return [str(path)]

    def expected_for(self, path: Path) -> str:
        text = path.read_bytes().decode("utf-8", errors="replace")
        return type(self).expected(text, str(path))

    def compare(self, path: Path, under: tuple = ()):
        expected = self.expected_for(path)
        r = subprocess.run([*under, self.exe, *self.args(path)], capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        actual = r.stdout.decode()
        if actual != expected:
            got, want = actual.splitlines(), expected.splitlines()
            for i, (a, b) in enumerate(zip(got, want)):
                if a != b:
                    self.fail(f"{path.name}: line {i + 1}:\n  port:   {a}\n  python: {b}")
            self.fail(f"{path.name}: {len(got)} lines from the port, {len(want)} from Python")

    def test_source_files(self):
        paths = source_files()
        self.assertGreater(len(paths), 10)
        for path in paths:
            with self.subTest(path.name):
                self.compare(path)

    def test_snippets(self):
        for name, text in self.snippets.items():
            with self.subTest(name):
                path = Path(_TMP.name, f"{self.tool}-{name}.pst")
                path.write_bytes(text.encode())
                self.compare(path)

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        self.compare(
            COMPILER / "Lexer.pst",
            under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"),
        )


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


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Lexer(_Differential):
    tool = "LexDump"
    snippets = SNIPPETS
    expected = staticmethod(lex_dump)


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Parser(_Differential):
    tool = "AstDump"
    snippets = PARSER_SNIPPETS
    expected = staticmethod(ast_dump)


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
    tool = "ResolveDump"

    def args(self, path: Path) -> list[str]:
        return [STD_DIR, str(path)]

    def expected_for(self, path: Path) -> str:
        return resolve_dump(str(path))

    def test_snippets(self):
        for name, files in RESOLVE_SNIPPETS.items():
            with self.subTest(name):
                d = Path(tempfile.mkdtemp(dir=_TMP.name))
                for f, text in files.items():
                    Path(d, f).write_bytes(text if isinstance(text, bytes) else text.encode())
                self.compare(d / next(iter(files)))

    def test_missing_file(self):
        self.compare(Path(_TMP.name, "does-not-exist.pst"))

    def test_std_unit(self):
        self.compare(Path(STD_DIR, "Strings.pst"))


def checker_inputs(out: str) -> list[Path]:
    """Copies the programs that the resolver, type checker and build tests check into
    ``out``, one directory each, and returns their main files. The tests run with
    ``check`` recording its input and building skipped."""
    import io
    import tests.test_build as tb
    import tests.test_resolve as tr
    import tests.test_typecheck as tt

    paths = []
    real = tr.check

    def record(path, std_dir=STD_DIR):
        src = os.path.dirname(os.path.abspath(path))
        if not src.startswith(str(ROOT)):  # not the std units or examples
            d = Path(out, str(len(paths)))
            shutil.copytree(src, d)
            paths.append(d / os.path.basename(path))
        return real(path, std_dir)

    def skip(*args, **kwargs):
        raise unittest.SkipTest("only checking")

    os.makedirs(tb._TMP.name, exist_ok=True)  # removed if test_build already ran
    saved = tr.check, tb.check, tb.build
    tr.check, tb.check, tb.build = record, record, skip
    try:
        suite = unittest.TestSuite()
        for m in (tr, tt, tb):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(m))
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    finally:
        tr.check, tb.check, tb.build = saved
    return paths


class Check(_Differential):
    tool = "CheckDump"

    def args(self, path: Path) -> list[str]:
        return [STD_DIR, str(path)]

    def expected_for(self, path: Path) -> str:
        return check_dump(str(path))

    def test_checker_tests(self):
        paths = checker_inputs(tempfile.mkdtemp(dir=_TMP.name))
        self.assertGreater(len(paths), 100)
        for path in paths:
            with self.subTest(str(path)):
                self.compare(path)

    def test_missing_file(self):
        self.compare(Path(_TMP.name, "does-not-exist.pst"))

    def test_std_unit(self):
        self.compare(Path(STD_DIR, "Strings.pst"))

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        self.compare(
            ROOT / "examples" / "Inventory.pst",
            under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"),
        )


def asm_dump(script: str) -> str:
    """Runs an assembler script (see ``compiler/AsmDump.pst``) and prints what AsmDump prints."""
    from pst import elf, x86

    a = x86.Assembler()
    names: dict[str, str] = {}
    out: list[str] = []

    def arg(w: str):
        w = names.get(w, w)
        if w in ("true", "false"):
            return w == "true"
        return int(w) if re.fullmatch(r"-?\d+", w) else w

    for line in script.splitlines():
        if not line:
            continue
        op, *ws = line.split(" ")
        if op == "data":
            value = bytes(int(b) & 0xFF for b in ws[2].split(",")) if ws[2] != "-" else b""
            names[ws[0]] = a.data(value, int(ws[1]))
        elif op == "new_label":
            names[ws[0]] = a.new_label(ws[1])
        elif op == "track":
            a.stack = []
        elif op == "untrack":
            a.stack = None
        elif op == "own":
            a.stack[-1] = True
        elif op == "sub_imm32_fixed":
            out.append(f"fixed {a.sub_imm32_fixed(*map(arg, ws))}")
        else:
            getattr(a, op)(*map(arg, ws))
        if a.stack is not None:
            out.append(f"stack [{''.join('1' if o else '0' for o in a.stack)}]")
    needed = (elf.LIBC,)
    code = a.finish(elf.rodata_offset(len(a.code)), elf.got_offset(len(a.code), len(a.rodata), a.imports, needed))

    def dump(title: str, bs: bytes) -> None:
        out.append(f"{title} {len(bs)}")
        out.extend(bs[i : i + 32].hex() for i in range(0, len(bs), 32))

    dump("code", code)
    dump("rodata", bytes(a.rodata))
    out.append(f"imports {' '.join(a.imports)}")
    out.append(f"globals {' '.join(a.globals)}")
    dump("executable", elf.executable(code, bytes(a.rodata), 0, a.imports, needed, len(a.globals)))
    return "".join(line + "\n" for line in out)


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


class Asm(unittest.TestCase):
    """The assembler and ELF writer, run on the same instruction script."""

    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("AsmDump")

    def compare(self, script: str):
        path = Path(_TMP.name, "asm.txt")
        path.write_text(script)
        r = subprocess.run([self.exe, str(path)], capture_output=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        got, want = r.stdout.decode().splitlines(), asm_dump(script).splitlines()
        for i, (a, b) in enumerate(zip(got, want)):
            if a != b:
                self.fail(f"line {i + 1}:\n  port:   {a}\n  python: {b}")
        self.assertEqual(len(got), len(want))

    def test_instructions(self):
        self.compare(asm_script())

    def test_empty(self):
        self.compare("")


def build_inputs(out: str) -> list[tuple[Path, bool]]:
    """Copies the programs that the build tests compile into ``out``, one directory each,
    and returns their main files, each with whether it is built checked. The tests run
    with building skipped."""
    import io
    import tests.test_build as tb

    inputs = []
    last = []

    def record(path, std_dir=STD_DIR):
        last[:] = [path]
        return check(path, std_dir)

    def skip(result, checked=True):
        d = Path(out, str(len(inputs)))
        shutil.copytree(os.path.dirname(os.path.abspath(last[0])), d)
        inputs.append((d / os.path.basename(last[0]), checked))
        raise unittest.SkipTest("only collecting")

    os.makedirs(tb._TMP.name, exist_ok=True)  # removed if test_build already ran
    saved = tb.check, tb.build
    tb.check, tb.build = record, skip
    try:
        suite = unittest.defaultTestLoader.loadTestsFromModule(tb)
        unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    finally:
        tb.check, tb.build = saved
    return inputs


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Build(unittest.TestCase):
    """The code generator: the port's executable and diagnostics must be the Python
    compiler's, byte for byte."""

    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("Build")

    def compare(self, path: Path, checked: bool = True, under: tuple = ()):
        result = check(str(path))
        diagnostics = list(result.diagnostics)
        want = None
        if not result.has_errors:
            built = build(result, checked)
            diagnostics += built.diagnostics
            want = built.executable
        out = Path(_TMP.name, "built")
        out.unlink(missing_ok=True)
        args = [self.exe, STD_DIR, str(path), str(out)] + ([] if checked else ["--unchecked"])
        r = subprocess.run([*under, *args], capture_output=True, timeout=600)
        self.assertEqual(r.stderr.decode(), "".join(d.human() + "\n" for d in diagnostics))
        self.assertEqual(r.returncode, 0 if want is not None else 1)
        if want is not None:
            self.assertTrue(out.read_bytes() == want, f"{path.name}: the executables differ")
            self.assertEqual(out.stat().st_mode & 0o777, 0o755)

    def test_build_tests(self):
        inputs = build_inputs(tempfile.mkdtemp(dir=_TMP.name))
        self.assertGreater(len(inputs), 50)
        for path, checked in inputs:
            with self.subTest(str(path)):
                self.compare(path, checked)

    def test_programs(self):
        for path in (ROOT / "examples" / "Inventory.pst", COMPILER / "LexDump.pst",
                     COMPILER / "AsmDump.pst"):
            with self.subTest(path.name):
                self.compare(path)

    def test_unchecked(self):
        self.compare(ROOT / "examples" / "Inventory.pst", checked=False)

    def test_errors(self):
        path = Path(_TMP.name, "Broken.pst")
        path.write_text("program Broken\n\nlet x: Integer := true\n")
        self.compare(path)

    def test_missing_file(self):
        self.compare(Path(_TMP.name, "does-not-exist.pst"))

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        self.compare(
            ROOT / "examples" / "Inventory.pst",
            under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"),
        )


del _Differential

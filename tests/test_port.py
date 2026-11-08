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
from pst.resolve import check
from pst.tokens import TokenKind

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

    def compare(self, path: Path, under: tuple = ()):
        text = path.read_bytes().decode("utf-8", errors="replace")
        expected = type(self).expected(text, str(path))
        r = subprocess.run([*under, self.exe, str(path)], capture_output=True, timeout=120)
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


del _Differential

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
from pathlib import Path

from pst.emit import build
from pst.lexer import lex
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


@unittest.skipUnless(_NATIVE, "needs Linux x86-64")
class Lexer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exe = build_tool("LexDump")

    def compare(self, path: Path, under: tuple = ()):
        text = path.read_bytes().decode("utf-8", errors="replace")
        expected = lex_dump(text, str(path))
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
        paths = sorted(
            p
            for d in ("pst/units", "examples", "compiler")
            for p in (ROOT / d).glob("*.pst")
        )
        self.assertGreater(len(paths), 10)
        for path in paths:
            with self.subTest(path.name):
                self.compare(path)

    def test_snippets(self):
        for name, text in SNIPPETS.items():
            with self.subTest(name):
                path = Path(_TMP.name, f"{name}.pst")
                path.write_bytes(text.encode())
                self.compare(path)

    @unittest.skipUnless(shutil.which("valgrind"), "needs valgrind")
    def test_valgrind(self):
        self.compare(
            COMPILER / "Lexer.pst",
            under=("valgrind", "-q", "--leak-check=full", "--error-exitcode=9"),
        )

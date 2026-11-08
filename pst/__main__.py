"""Command line entry point: ``python -m pst <command> ...``.

``tokens`` and ``ast`` are debugging aids for the lexer and parser.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

from .diagnostics import Diagnostic, Severity
from . import ast
from .lexer import lex
from .parser import parse
from .resolve import check
from .emit import build
from .source import Span


def _read(path: str) -> tuple[str | None, Diagnostic | None]:
    data = open(path, "rb").read()
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError as e:
        prefix = data[: e.start].decode("utf-8")
        line = prefix.count("\n") + 1
        col = len(prefix) - (prefix.rfind("\n") + 1) + 1
        span = Span(len(prefix), len(prefix) + 1, line, col, line, col + 1)
        return None, Diagnostic(
            path, span, Severity.ERROR, "E0000", f"invalid UTF-8 byte 0x{data[e.start]:02X}"
        )


def cmd_tokens(args: argparse.Namespace) -> int:
    text, err = _read(args.file)
    if err is not None:
        diags, tokens = [err], []
    else:
        result = lex(text, args.file)
        diags, tokens = result.diagnostics, result.tokens

    if args.json:
        json.dump(
            {
                "tokens": [t.to_json() for t in tokens],
                "diagnostics": [d.to_json() for d in diags],
            },
            sys.stdout,
            indent=1,
        )
        print()
    else:
        for t in tokens:
            s = t.span
            value = "" if t.value is None else f"  {t.value!r}"
            print(f"{s.line}:{s.col}-{s.end_line}:{s.end_col}\t{t.kind.name}\t{t.text!r}{value}")
        for d in diags:
            print(d.human(), file=sys.stderr)
    return 1 if any(d.severity is Severity.ERROR for d in diags) else 0


def cmd_ast(args: argparse.Namespace) -> int:
    text, err = _read(args.file)
    if err is not None:
        diags, tree = [err], None
    else:
        result = parse(text, args.file)
        diags, tree = result.diagnostics, result.file

    if args.json:
        json.dump(
            {
                "ast": None if tree is None else ast.to_json(tree),
                "diagnostics": [d.to_json() for d in diags],
            },
            sys.stdout,
            indent=1,
        )
        print()
    else:
        if tree is not None:
            print(ast.dump(tree))
        for d in diags:
            print(d.human(), file=sys.stderr)
    return 1 if any(d.severity is Severity.ERROR for d in diags) else 0


def cmd_check(args: argparse.Namespace) -> int:
    result = check(args.file)
    for d in result.diagnostics:
        if args.json:
            print(json.dumps(d.to_json()))
        else:
            print(d.human(), file=sys.stderr)
    return 1 if result.has_errors else 0


def _build(path: str, out: str, as_json: bool = False) -> bool:
    """Checks and compiles ``path`` into the executable ``out``; prints diagnostics."""
    result = check(path)
    diags = list(result.diagnostics)
    exe = None
    if not result.has_errors:
        built = build(result)
        diags += built.diagnostics
        exe = built.executable
    for d in diags:
        if as_json:
            print(json.dumps(d.to_json()))
        else:
            print(d.human(), file=sys.stderr)
    if exe is None:
        return False
    with open(out, "wb") as f:
        f.write(exe)
    os.chmod(out, 0o755)
    return True


def cmd_build(args: argparse.Namespace) -> int:
    out = args.output or os.path.splitext(os.path.basename(args.file))[0]
    return 0 if _build(args.file, out, args.json) else 1


def cmd_run(args: argparse.Namespace) -> int:
    rest = args.args[1:] if args.args[:1] == ["--"] else args.args
    with tempfile.TemporaryDirectory() as tmp:
        exe = os.path.join(tmp, os.path.splitext(os.path.basename(args.file))[0])
        if not _build(args.file, exe):
            return 1
        return subprocess.run([exe, *rest]).returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pst")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tokens", help="print the token stream of a file")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tokens)
    p = sub.add_parser("ast", help="print the syntax tree of a file")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ast)
    p = sub.add_parser("check", help="check a program or unit and the units it uses")
    p.add_argument("file")
    p.add_argument("--json", action="store_true", help="one JSON diagnostic per line")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("build", help="compile a program to an executable")
    p.add_argument("file")
    p.add_argument("-o", "--output", help="executable path (default: the program name)")
    p.add_argument("--unchecked", action="store_true", help="drop runtime checks except assert")
    p.add_argument("--json", action="store_true", help="one JSON diagnostic per line")
    p.set_defaults(func=cmd_build)
    p = sub.add_parser("run", help="build a program to a temporary directory and run it")
    p.add_argument("file")
    p.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the program, after --")
    p.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

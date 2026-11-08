"""Command line entry point: ``python -m pst <command> ...``.

``tokens`` and ``ast`` are debugging aids for the lexer and parser.
"""

from __future__ import annotations

import argparse
import json
import sys

from .diagnostics import Diagnostic, Severity
from . import ast
from .lexer import lex
from .parser import parse
from .resolve import check
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
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

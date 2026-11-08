"""Recursive-descent parser for Turbo Pasthon (SPEC §16).

The parser never stops at the first error (SPEC §15.6). A syntax error inside a line
raises ``_ParseError``; the enclosing line list catches it, skips the rest of the line and
any block indented under it, and inserts an ``ErrorStmt``. Indentation makes these
recovery points reliable, so one mistake produces one diagnostic.

Errors that don't make the structure ambiguous (a chained comparison, an `if` expression
without `else`, a missing initializer that is still followed by a value...) are reported
without unwinding, and parsing continues normally.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from . import ast as A
from .diagnostics import Diagnostic, Edit, Fix, Severity
from .lexer import LexResult, lex
from .source import Span
from .tokens import LAYOUT_KINDS, Comment, Token, TokenKind as K

T = TypeVar("T")

_COMPARISONS = {K.EQ, K.NE, K.LT, K.LE, K.GT, K.GE, K.IN}
_ADDITIVE = {K.PLUS, K.MINUS}
_MULTIPLICATIVE = {K.STAR, K.SLASH, K.DIV, K.MOD}
_DECL_START = {K.TYPE, K.CONST, K.FUNCTION, K.PROCEDURE, K.EXTERN, K.TEST, K.EXPORT, K.UNSAFE}
_TYPE_ONLY_START = {K.REF, K.FUNCTION, K.PROCEDURE, K.THIS_TYPE}


class _ParseError(Exception):
    """Unwinds to the nearest line list, which then resynchronizes."""


@dataclass
class ParseResult:
    file: A.File
    tokens: list[Token]
    comments: list[Comment]
    diagnostics: list[Diagnostic]

    @property
    def has_errors(self) -> bool:
        return any(d.severity is Severity.ERROR for d in self.diagnostics)


def parse(text: str, file: str = "<input>") -> ParseResult:
    lexed = lex(text, file)
    parser = _Parser(lexed, file)
    tree = parser.parse_file()
    diags = sorted(lexed.diagnostics + parser.diags, key=lambda d: (d.span.start, d.span.end))
    return ParseResult(tree, lexed.tokens, lexed.comments, diags)


def _point(span: Span, at_end: bool = False) -> Span:
    if at_end:
        return Span(span.end, span.end, span.end_line, span.end_col, span.end_line, span.end_col)
    return Span(span.start, span.start, span.line, span.col, span.line, span.col)


def _describe(tok: Token) -> str:
    k = tok.kind
    if k is K.NEWLINE:
        return "end of line"
    if k is K.INDENT:
        return "an indented line"
    if k is K.DEDENT:
        return "end of block"
    if k is K.EOF:
        return "end of file"
    if k is K.IDENT:
        return f"identifier '{tok.text}'"
    if k is K.END_MARKER:
        return "an end marker"
    if k in (K.INT, K.REAL, K.CHAR, K.STRING, K.STRING_BEGIN):
        return f"{k.value} {tok.text}"
    return f"'{tok.text}'"


class _Parser:
    def __init__(self, lexed: LexResult, file: str) -> None:
        self.toks = lexed.tokens
        self.i = 0
        self.file = file
        self.diags: list[Diagnostic] = []
        self._reported: set[tuple[str, int]] = set()
        self.last = self.toks[0]  # last consumed non-layout token
        self.kind = "program"
        # Parser errors on a logical line that already has a lexical error are almost
        # always follow-on errors, so they are not reported. A logical line can span
        # several physical lines inside brackets.
        self._lex_error_lines = {d.span.line for d in lexed.diagnostics}
        ends = [t.span.end for t in self.toks if t.kind in (K.NEWLINE, K.EOF)]
        self._lex_error_ranges = [
            (d.span.start, next((e for e in ends if e >= d.span.end), d.span.end))
            for d in lexed.diagnostics
        ]
        code_lines = {t.span.line for t in self.toks if t.kind not in LAYOUT_KINDS}
        self._docs = {
            c.span.line: c.text
            for c in lexed.comments
            if c.is_doc and c.span.line not in code_lines
        }

    # ------------------------------------------------------------ token access

    def peek(self, ahead: int = 0) -> Token:
        return self.toks[min(self.i + ahead, len(self.toks) - 1)]

    def at(self, *kinds: K) -> bool:
        return self.toks[self.i].kind in kinds

    def advance(self) -> Token:
        tok = self.toks[self.i]
        if tok.kind is not K.EOF:
            self.i += 1
        if tok.kind not in LAYOUT_KINDS:
            self.last = tok
        return tok

    def accept(self, kind: K) -> Token | None:
        return self.advance() if self.at(kind) else None

    def expect(self, kind: K, what: str) -> Token:
        if self.at(kind):
            return self.advance()
        self.fail("E0040", self.peek().span, f"expected {what}, found {_describe(self.peek())}")

    def finish(self, start: Span) -> Span:
        """Span from ``start`` to the end of the last consumed token."""
        if self.last.span.end >= start.start:
            return start.join(self.last.span)
        return _point(start)

    def prev_was_dedent(self) -> bool:
        return self.i > 0 and self.toks[self.i - 1].kind is K.DEDENT

    def end_line(self) -> None:
        """End of a simple line: NEWLINE, or nothing right after an indented block."""
        if self.at(K.NEWLINE):
            self.advance()
        elif not self.prev_was_dedent():
            self.fail(
                "E0040", self.peek().span, f"expected end of line, found {_describe(self.peek())}"
            )

    # ------------------------------------------------------------- diagnostics

    def report(self, code: str, span: Span, message: str, fixes: tuple[Fix, ...] = ()) -> None:
        if (
            span.line in self._lex_error_lines
            or any(a <= span.start <= b for a, b in self._lex_error_ranges)
            or (code, span.start) in self._reported
        ):
            return
        self._reported.add((code, span.start))
        self.diags.append(Diagnostic(self.file, span, Severity.ERROR, code, message, fixes))

    def fail(self, code: str, span: Span, message: str, fixes: tuple[Fix, ...] = ()):
        self.report(code, span, message, fixes)
        raise _ParseError

    def sync(self) -> None:
        """Skip the rest of the current line and any block indented under it."""
        depth = 0
        while True:
            k = self.peek().kind
            if k is K.EOF:
                return
            if depth == 0:
                if k is K.DEDENT:
                    return
                if k is K.NEWLINE:
                    self.advance()
                    if not self.at(K.INDENT):
                        return
                    depth = 1
            elif k is K.INDENT:
                depth += 1
            elif k is K.DEDENT:
                depth -= 1
                if depth == 0:
                    self.advance()
                    return
            self.advance()

    # -------------------------------------------------------------- line lists

    def line_list(
        self, parse_one: Callable[[], T | None], on_error: Callable[[Span], T] | None = None
    ) -> list[T]:
        """Parse lines until the end of the current block (DEDENT or EOF)."""
        out: list = []
        while not self.at(K.DEDENT, K.EOF):
            if self.at(K.END_MARKER):
                self.end_marker(out[-1] if out else None)
                continue
            if self.at(K.INDENT):
                self.report("E0041", self.peek().span, "unexpected indentation")
                self.advance()
                out.extend(self.line_list(parse_one, on_error))
                self.accept(K.DEDENT)
                continue
            pos, start = self.i, self.peek().span
            try:
                item = parse_one()
            except _ParseError:
                self.sync()
                item = on_error(self.finish(start)) if on_error else None
            if item is not None:
                out.append(item)
            if self.i == pos:
                self.advance()
        return out

    def indented(
        self,
        parse_one: Callable[[], T | None],
        what: str,
        on_error: Callable[[Span], T] | None = None,
        optional: bool = False,
    ) -> list[T]:
        """The end of a header line, then an indented list of lines."""
        self.end_line()
        if not self.at(K.INDENT):
            if not optional:
                self.report("E0042", _point(self.peek().span), f"expected an indented {what}")
            return []
        indent = self.advance()
        out = self.line_list(parse_one, on_error)
        self.accept(K.DEDENT)
        if not out:
            self.report("E0042", indent.span, f"expected an indented {what}")
        return out

    def end_marker(self, prev) -> None:
        tok = self.advance()
        kind, name = tok.value
        text = tok.text.rstrip()
        expected = self.marker_for(prev)
        if isinstance(prev, A.ErrorStmt):
            pass  # the block itself was broken; don't pile on
        elif isinstance(prev, A.Repeat):
            self.report(
                "E0010", tok.span, "a 'repeat' block is closed by 'until' and takes no end marker"
            )
        elif expected is None:
            self.report(
                "E0010", tok.span, f"end marker '{text}' does not directly follow a block it can close"
            )
        else:
            want_kind, want_name = expected
            if kind != want_kind or (name is not None and name != want_name):
                correct = f"# end {want_kind}" + (f" {want_name}" if want_name and name else "")
                what = f"{want_kind} '{want_name}'" if want_name else f"'{want_kind}' block"
                self.report(
                    "E0010",
                    tok.span,
                    f"end marker '{text}' does not match the {what} it closes; "
                    f"expected '{correct}'",
                    (Fix("correct the end marker", (Edit(tok.span, correct),)),),
                )
            elif prev.end_marker is not None:
                self.report("E0010", tok.span, "the block already has an end marker")
            else:
                prev.end_marker = tok.span
        self.accept(K.NEWLINE)

    @staticmethod
    def marker_for(node) -> tuple[str, str | None] | None:
        if isinstance(node, A.If):
            return "if", None
        if isinstance(node, A.While):
            return "while", None
        if isinstance(node, (A.ForRange, A.ForIn)):
            return "for", None
        if isinstance(node, A.Case):
            return "case", None
        if isinstance(node, A.Defer) and isinstance(node.body, A.Block):
            return "defer", None
        if isinstance(node, A.TestBlock):
            return "test", None
        if isinstance(node, A.ExternBlock):
            return "extern", None
        if isinstance(node, A.Routine):
            return ("function" if node.sig.is_function else "procedure"), node.sig.name.name
        if isinstance(node, A.TypeDecl):
            if isinstance(node.body, A.RecordType):
                return "record", node.name.name
            if isinstance(node.body, A.TraitType):
                return "trait", node.name.name
        return None

    def doc(self, start: Span) -> str | None:
        lines = []
        line = start.line - 1
        while line in self._docs:
            text = self._docs[line][2:]
            lines.append(text[1:] if text.startswith(" ") else text)
            line -= 1
        return "\n".join(reversed(lines)) if lines else None

    # ------------------------------------------------------------------- names

    def ident(self, what: str) -> A.Ident:
        tok = self.peek()
        if tok.kind is K.IDENT:
            self.advance()
            return A.Ident(tok.span, tok.text)
        if tok.kind is K.UNDERSCORE:
            self.fail(
                "E0040", tok.span, f"expected {what}; names starting with '_' are discards"
            )
        self.fail("E0040", tok.span, f"expected {what}, found {_describe(tok)}")

    def string_const(self, what: str) -> str:
        """A string literal without placeholders (library, symbol, and test names)."""
        if self.at(K.STRING):
            return self.advance().value
        if self.at(K.STRING_BEGIN):
            e = self.primary()
            self.report("E0040", e.span, f"{what} may not contain placeholders; write '\\{{'")
            return ""
        self.fail("E0040", self.peek().span, f"expected {what} (a string), found {_describe(self.peek())}")

    # -------------------------------------------------------------------- file

    def parse_file(self) -> A.File:
        start = self.peek().span
        name = A.Ident(_point(start), "")
        try:
            if self.at(K.PROGRAM, K.UNIT):
                self.kind = self.advance().text
                name = self.ident(f"{self.kind} name")
                self.end_line()
            else:
                self.report(
                    "E0045", _point(start), "a file must start with 'program Name' or 'unit Name'"
                )
        except _ParseError:
            self.sync()
        uses: list[A.Ident] = []
        if self.at(K.USES):
            self.uses(uses)
        items = self.line_list(lambda: self.top_item(uses), A.ErrorStmt)
        return A.File(self.finish(start), self.kind, name, uses, items)

    def uses(self, into: list[A.Ident]) -> None:
        try:
            self.advance()
            into.append(self.ident("unit name"))
            while self.accept(K.COMMA):
                into.append(self.ident("unit name"))
            self.end_line()
        except _ParseError:
            self.sync()

    def top_item(self, uses: list[A.Ident]):
        tok = self.peek()
        if tok.kind is K.USES:
            self.report("E0044", tok.span, "'uses' must come directly after the header, at most once")
            self.uses(uses)
            return None
        if tok.kind in (K.PROGRAM, K.UNIT):
            self.fail("E0045", tok.span, f"unexpected '{tok.text}': a file has exactly one header")
        if tok.kind in _DECL_START:
            return self.decl()
        stmt = self.stmt()
        if self.kind == "unit":
            self.report(
                "E0101", stmt.span, "a unit can't contain top-level statements; move it into a function"
            )
        return stmt

    def decl(self):
        start = self.peek().span
        if self.at(K.TYPE):
            return self.type_section()
        if self.at(K.CONST):
            return self.const_section()
        if self.at(K.EXTERN):
            return self.extern_block()
        if self.at(K.TEST):
            return self.test_block()
        exported = self.accept(K.EXPORT) is not None
        if exported and self.at(K.TYPE, K.CONST):
            self.report(
                "E0048",
                self.peek().span,
                f"'export' goes on each declaration inside a '{self.peek().text}' section",
            )
            return self.decl()
        if self.at(K.UNSAFE, K.FUNCTION, K.PROCEDURE):
            return self.routine(exported, start)
        self.fail(
            "E0040",
            self.peek().span,
            f"expected 'function' or 'procedure' after 'export', found {_describe(self.peek())}",
        )

    # ------------------------------------------------------------------- types

    def type_section(self) -> A.TypeSection:
        start = self.advance().span
        decls = self.indented(self.type_decl, "list of type declarations")
        return A.TypeSection(self.finish(start), decls)

    def type_decl(self) -> A.TypeDecl:
        start = self.peek().span
        exported = self.accept(K.EXPORT) is not None
        name = self.ident("type name")
        params = self.type_params() if self.at(K.LBRACKET) else []
        self.expect(K.EQ, "'=' after the type name")
        body = self.type_body()
        return A.TypeDecl(self.finish(start), exported, name, params, body, self.doc(start))

    def type_body(self):
        start = self.peek().span
        if self.accept(K.RECORD):
            fields = self.indented(lambda: self.member(False), "list of fields", optional=True)
            return A.RecordType(self.finish(start), fields)
        if self.accept(K.TRAIT):
            members = self.indented(lambda: self.member(True), "list of trait members", optional=True)
            return A.TraitType(
                self.finish(start),
                [m for m in members if isinstance(m, A.FieldDecl)],
                [m for m in members if isinstance(m, A.Signature)],
            )
        if self.accept(K.LPAREN):
            members = [self.ident("enum member")]
            while self.accept(K.COMMA):
                if self.at(K.RPAREN):
                    break
                members.append(self.ident("enum member"))
            self.expect(K.RPAREN, "')' to close the enum")
            self.end_line()
            return A.EnumType(self.finish(start), members)

        if self.at(*_TYPE_ONLY_START):
            first = self.type_expr()
        else:
            low = self.expr()
            if self.accept(K.DOTDOT):
                high = self.expr()
                self.end_line()
                return A.SubrangeType(self.finish(start), low, high)
            first = self.expr_to_type(low)
            if first is None:
                self.fail("E0040", low.span, "expected a type, a subrange 'a..b', or 'record'/'trait'")
        variants = [first]
        while self.accept(K.PIPE):
            variants.append(self.type_expr())
        self.end_line()
        if len(variants) > 1:
            return A.UnionType(self.finish(start), variants)
        return first

    def member(self, in_trait: bool):
        start = self.peek().span
        if in_trait and self.at(K.FUNCTION, K.PROCEDURE):
            sig = self.signature()
            self.end_line()
            sig.doc = self.doc(start)
            return sig
        exported = self.accept(K.EXPORT) is not None
        if exported and in_trait:
            self.report("E0048", start, "trait members are exported with the trait")
            exported = False
        name = self.ident("field name")
        self.expect(K.COLON, "':' and the field type")
        type_ = self.type_expr()
        default = self.expr() if self.accept(K.ASSIGN) else None
        self.end_line()
        return A.FieldDecl(self.finish(start), exported, name, type_, default, self.doc(start))

    def type_params(self) -> list[A.TypeParam]:
        self.advance()
        params = []
        while True:
            start = self.peek().span
            name = self.ident("type parameter")
            bounds = []
            if self.accept(K.COLON):
                bounds.append(self.type_expr())
                while self.accept(K.PLUS):
                    bounds.append(self.type_expr())
            params.append(A.TypeParam(self.finish(start), name, bounds))
            if not self.accept(K.COMMA) or self.at(K.RBRACKET):
                break
        self.expect(K.RBRACKET, "']' to close the type parameters")
        return params

    def type_expr(self):
        start = self.peek().span
        if self.accept(K.REF):
            return A.RefType(self.finish(start), self.type_expr())
        if self.at(K.FUNCTION, K.PROCEDURE):
            is_function = self.advance().kind is K.FUNCTION
            self.expect(K.LPAREN, "'(' and the parameter types")
            params = []
            while not self.at(K.RPAREN):
                params.append(self.type_expr())
                if not self.accept(K.COMMA):
                    break
            self.expect(K.RPAREN, "')'")
            result = None
            if is_function:
                self.expect(K.COLON, "':' and the result type")
                result = self.type_expr()
            return A.FuncType(self.finish(start), is_function, params, result)
        if self.accept(K.THIS_TYPE):
            return A.TypeName(start, ["This"])
        if not self.at(K.IDENT):
            self.fail("E0040", start, f"expected a type, found {_describe(self.peek())}")
        parts = [self.advance().text]
        while self.at(K.DOT) and self.peek(1).kind is K.IDENT:
            self.advance()
            parts.append(self.advance().text)
        args = self.bracket_items(True) if self.at(K.LBRACKET) else []
        return A.TypeName(self.finish(start), parts, args)

    def bracket_items(self, as_types: bool) -> list:
        """`[a, b]` after a name: type arguments, or (in expressions) index items.

        In an expression `f[Integer]` and `a[i]` look the same, so names stay
        expressions there and the checker decides. Items that can only be types
        (`ref T`, `function(...)`, `This`) are parsed as types either way.
        """
        self.advance()
        items = []
        while not self.at(K.RBRACKET):
            if self.at(*_TYPE_ONLY_START):
                items.append(self.type_expr())
            else:
                e = self.expr()
                items.append((self.expr_to_type(e) or e) if as_types else e)
            if not self.accept(K.COMMA):
                break
        self.expect(K.RBRACKET, "']'")
        return items

    def expr_to_type(self, e):
        """Reinterpret a name expression (`List[T]`, `Geometry.Shape`) as a type."""
        if isinstance(e, A.Name):
            return A.TypeName(e.span, [e.name])
        if isinstance(e, A.FieldAccess):
            inner = self.expr_to_type(e.target)
            if isinstance(inner, A.TypeName) and not inner.args:
                return A.TypeName(e.span, inner.parts + [e.name.name])
            return None
        if isinstance(e, A.Index):
            inner = self.expr_to_type(e.target)
            if isinstance(inner, A.TypeName) and not inner.args:
                args = [
                    x if isinstance(x, (A.TypeName, A.RefType, A.FuncType))
                    else self.expr_to_type(x) or x
                    for x in e.items
                ]
                return A.TypeName(e.span, inner.parts, args)
        return None

    # ---------------------------------------------------- constants, routines

    def const_section(self) -> A.ConstSection:
        start = self.advance().span
        decls = self.indented(self.const_decl, "list of constant declarations")
        return A.ConstSection(self.finish(start), decls)

    def const_decl(self) -> A.ConstDecl:
        start = self.peek().span
        exported = self.accept(K.EXPORT) is not None
        name = self.ident("constant name")
        if self.at(K.ASSIGN):
            tok = self.advance()
            self.report(
                "E0040",
                tok.span,
                "constants are declared with '=', not ':='",
                (Fix("use '='", (Edit(tok.span, "="),)),),
            )
        else:
            self.expect(K.EQ, "'=' after the constant name")
        value = self.expr()
        self.end_line()
        return A.ConstDecl(self.finish(start), exported, name, value, self.doc(start))

    def routine(self, exported: bool, start: Span) -> A.Routine:
        unsafe = self.accept(K.UNSAFE) is not None
        if self.at(K.EXPORT):
            self.fail("E0040", self.peek().span, "write 'export unsafe', not 'unsafe export'")
        sig = self.signature()
        requires: list = []
        ensures: list = []
        stmts: list = []
        self.end_line()
        if not self.at(K.INDENT):
            self.report("E0042", _point(self.peek().span), f"expected the body of '{sig.name.name}'")
        else:
            indent = self.advance()
            while self.at(K.REQUIRES, K.ENSURES):
                tok = self.advance()
                try:
                    if tok.kind is K.REQUIRES and ensures:
                        self.report("E0040", tok.span, "'requires' must come before 'ensures'")
                    (requires if tok.kind is K.REQUIRES else ensures).append(self.expr())
                    self.end_line()
                except _ParseError:
                    self.sync()
            stmts = self.line_list(self.stmt, A.ErrorStmt)
            self.accept(K.DEDENT)
            if not stmts:
                self.report("E0042", indent.span, f"expected statements in the body of '{sig.name.name}'")
        body = A.Block(self._block_span(stmts, sig.span), stmts)
        return A.Routine(self.finish(start), exported, unsafe, sig, requires, ensures, body, self.doc(start))

    def signature(self) -> A.Signature:
        start = self.peek().span
        is_function = self.advance().kind is K.FUNCTION
        kw = "function" if is_function else "procedure"
        name = self.ident(f"{kw} name")
        tparams = self.type_params() if self.at(K.LBRACKET) else []
        self.expect(K.LPAREN, "'(' and the parameter list")
        params = []
        while not self.at(K.RPAREN):
            params.append(self.param())
            if not self.accept(K.COMMA):
                break
        self.expect(K.RPAREN, "')' to close the parameter list")
        result = self.type_expr() if self.accept(K.COLON) else None
        if is_function and result is None:
            self.report(
                "E0040",
                _point(self.last.span, at_end=True),
                f"function '{name.name}' needs a result type ': T'; "
                "a routine without a result is a 'procedure'",
            )
        elif not is_function and result is not None:
            self.report(
                "E0040",
                result.span,
                f"procedure '{name.name}' can't have a result type; use 'function'",
            )
        return A.Signature(self.finish(start), is_function, name, tparams, params, result)

    def param(self) -> A.Param:
        start = self.peek().span
        if self.accept(K.ELLIPSIS):
            return A.Param(start, False, [], None, None, is_varargs=True)
        is_var = self.accept(K.VAR) is not None
        if self.at(K.THIS):
            tok = self.advance()
            return A.Param(self.finish(start), is_var, [A.Ident(tok.span, "this")], None, None, is_this=True)
        names = [self.ident("parameter name")]
        while (
            self.at(K.COMMA)
            and self.peek(1).kind is K.IDENT
            and self.peek(2).kind in (K.COMMA, K.COLON)
        ):
            self.advance()
            names.append(self.ident("parameter name"))
        self.expect(K.COLON, "':' and the parameter type")
        type_ = self.type_expr()
        default = self.expr() if self.accept(K.ASSIGN) else None
        return A.Param(self.finish(start), is_var, names, type_, default)

    def extern_block(self) -> A.ExternBlock:
        start = self.advance().span
        library = self.string_const("library name")
        decls = self.indented(self.extern_decl, "list of extern declarations")
        return A.ExternBlock(self.finish(start), library, decls)

    def extern_decl(self) -> A.ExternDecl:
        start = self.peek().span
        if not self.at(K.FUNCTION, K.PROCEDURE):
            self.fail(
                "E0040", start, f"expected 'function' or 'procedure', found {_describe(self.peek())}"
            )
        sig = self.signature()
        symbol = self.string_const("C symbol name") if self.accept(K.AS) else None
        self.end_line()
        return A.ExternDecl(self.finish(start), sig, symbol, self.doc(start))

    def test_block(self) -> A.TestBlock:
        start = self.advance().span
        name = self.string_const("test name")
        body = self.block()
        return A.TestBlock(self.finish(start), name, body)

    # -------------------------------------------------------------- statements

    def _block_span(self, stmts: list, fallback: Span) -> Span:
        return stmts[0].span.join(stmts[-1].span) if stmts else _point(fallback, at_end=True)

    def block(self) -> A.Block:
        start = self.peek().span
        if not self.at(K.NEWLINE) and not self.prev_was_dedent():
            self.report(
                "E0049",
                start,
                "a block must start on a new line, indented by 4 spaces",
            )
            stmt = self.stmt()
            return A.Block(stmt.span, [stmt])
        stmts = self.indented(self.stmt, "block", A.ErrorStmt)
        return A.Block(self._block_span(stmts, start), stmts)

    def stmt(self):
        tok = self.peek()
        k = tok.kind
        start = tok.span

        if k in _DECL_START:
            # The line list skips the rest of the declaration, body included.
            if k is K.CONST:
                self.fail("E0202", start, "'const' is only allowed at top level; use 'let' here")
            self.fail("E0048", start, f"'{tok.text}' declarations are only allowed at top level")
        if k in (K.VAR, K.LET):
            return self.var_decl()
        if k is K.IF:
            return self.if_stmt()
        if k is K.WHILE:
            self.advance()
            cond = self.expr()
            self.expect(K.DO, "'do'")
            body = self.block()
            return A.While(self.finish(start), cond, body)
        if k is K.REPEAT:
            self.advance()
            body = self.block()
            self.expect(K.UNTIL, "'until' to close the 'repeat' block")
            cond = self.expr()
            self.end_line()
            return A.Repeat(self.finish(start), body, cond)
        if k is K.FOR:
            return self.for_stmt()
        if k is K.CASE:
            self.advance()
            subject = self.expr()
            self.expect(K.OF, "'of'")
            arms = self.indented(self.case_arm, "list of case arms")
            return A.Case(self.finish(start), subject, arms)
        if k is K.DEFER:
            self.advance()
            body = self.block() if self.at(K.NEWLINE) else self.stmt()
            return A.Defer(self.finish(start), body)
        if k in (K.BREAK, K.CONTINUE):
            self.advance()
            self.end_line()
            return (A.Break if k is K.BREAK else A.Continue)(start)
        if k is K.RETURN:
            self.advance()
            if self.at(K.NEWLINE):
                self.advance()
                return A.Return(start, None)
            value = self.rhs()
            return A.Return(self.finish(start), value)
        if k is K.ASSERT:
            self.advance()
            cond = self.expr()
            message = self.expr() if self.accept(K.COMMA) else None
            self.end_line()
            return A.Assert(self.finish(start), cond, message)
        if k is K.UNDERSCORE and self.peek(1).kind is K.ASSIGN:
            self.advance()
            self.advance()
            value = self.rhs()
            return A.Discard(self.finish(start), tok.text, value)
        if k in (K.REQUIRES, K.ENSURES):
            self.fail("E0040", start, f"'{tok.text}' must be at the start of a function body")
        if k is K.ELSE:
            self.fail("E0040", start, "'else' without a matching 'if'")
        if k is K.UNTIL:
            self.fail("E0040", start, "'until' without a matching 'repeat'")

        e = self.expr()
        if self.at(K.ASSIGN):
            if not isinstance(e, (A.Name, A.FieldAccess, A.Index, A.Deref, A.ErrorExpr)):
                self.report("E0046", e.span, "can't assign to this expression")
            self.advance()
            value = self.rhs()
            return A.Assign(self.finish(start), e, value)
        call = e.operand if isinstance(e, A.Unary) and e.op == "try" else e
        if not isinstance(call, (A.Call, A.ErrorExpr)):
            fixes: tuple[Fix, ...] = ()
            msg = "this expression is not a statement; only calls and assignments are"
            if isinstance(e, A.Binary) and e.op == "=":
                msg += "; did you mean ':='?"
                fixes = (Fix("assign with ':='", (Edit(e.op_span, ":="),)),)
            self.report("E0047", e.span, msg, fixes)
        self.end_line()
        return A.ExprStmt(self.finish(start), e)

    def rhs(self):
        """Right side of `:=` / `var` / `let`, or the operand of `return`. Ends the line."""
        if self.at(K.CASE):
            return self.case_expr()
        e = self.expr()
        self.end_line()
        return e

    def var_decl(self) -> A.VarDecl:
        start = self.peek().span
        mutable = self.advance().kind is K.VAR
        if self.at(K.UNDERSCORE):
            self.fail("E0040", self.peek().span, "to discard a value, write '_ := expr' without 'var'")
        name = self.ident("variable name")
        type_ = self.type_expr() if self.accept(K.COLON) else None
        if not self.at(K.ASSIGN):
            self.fail(
                "E0201",
                _point(self.peek().span),
                f"'{name.name}' must be initialized: '{'var' if mutable else 'let'} "
                f"{name.name} := value'",
            )
        self.advance()
        value = self.rhs()
        return A.VarDecl(self.finish(start), mutable, name, type_, value)

    def if_stmt(self) -> A.If:
        start = self.advance().span
        cond = self.expr()
        self.expect(K.THEN, "'then'")
        body = self.block()
        branches = [A.CondBlock(cond.span.join(body.span), cond, body)]
        else_body = None
        while self.accept(K.ELSE):
            if self.accept(K.IF):
                cond = self.expr()
                self.expect(K.THEN, "'then'")
                body = self.block()
                branches.append(A.CondBlock(cond.span.join(body.span), cond, body))
            else:
                else_body = self.block()
                break
        return A.If(self.finish(start), branches, else_body)

    def loop_var(self) -> A.Ident:
        tok = self.peek()
        if tok.kind in (K.IDENT, K.UNDERSCORE):
            self.advance()
            return A.Ident(tok.span, tok.text)
        self.fail("E0040", tok.span, f"expected a loop variable, found {_describe(tok)}")

    def for_stmt(self):
        start = self.advance().span
        first = self.loop_var()
        if self.accept(K.ASSIGN):
            if first.name.startswith("_"):
                self.report("E0040", first.span, "a counting loop needs a named variable")
            lo = self.expr()
            if not self.at(K.TO, K.DOWNTO):
                self.fail("E0040", self.peek().span, f"expected 'to' or 'downto', found {_describe(self.peek())}")
            down = self.advance().kind is K.DOWNTO
            hi = self.expr()
            self.expect(K.DO, "'do'")
            body = self.block()
            return A.ForRange(self.finish(start), first, lo, hi, down, body)
        vars_ = [first]
        if self.accept(K.COMMA):
            vars_.append(self.loop_var())
        self.expect(K.IN, "'in' or ':='")
        iterable = self.expr()
        self.expect(K.DO, "'do'")
        body = self.block()
        return A.ForIn(self.finish(start), vars_, iterable, body)

    # -------------------------------------------------------------------- case

    def labels(self) -> list:
        if self.accept(K.ELSE):
            return []
        out = [self.label()]
        while self.accept(K.COMMA):
            out.append(self.label())
        return out

    def label(self):
        start = self.peek().span
        if self.at(K.IDENT) and self.peek(1).kind is K.LPAREN:
            name = self.advance()
            self.advance()
            binding = self.loop_var() if self.at(K.IDENT, K.UNDERSCORE) else None
            if binding is None:
                self.fail("E0040", self.peek().span, "expected a name for the variant value, or '_'")
            self.expect(K.RPAREN, "')'")
            return A.VariantLabel(self.finish(start), A.TypeName(name.span, [name.text]), binding)
        low = self.expr()
        if self.accept(K.DOTDOT):
            high = self.expr()
            return A.RangeLabel(self.finish(start), low, high)
        return A.ValueLabel(low.span, low)

    def case_arm(self) -> A.CaseArm:
        start = self.peek().span
        labels = self.labels()
        self.expect(K.COLON, "':' after the case label")
        body = self.block() if self.at(K.NEWLINE) else self.stmt()
        return A.CaseArm(self.finish(start), labels, body)

    def case_expr(self) -> A.CaseExpr:
        start = self.advance().span
        subject = self.expr()
        self.expect(K.OF, "'of'")
        arms = self.indented(self.case_expr_arm, "list of case arms")
        return A.CaseExpr(self.finish(start), subject, arms)

    def case_expr_arm(self) -> A.CaseExprArm:
        start = self.peek().span
        labels = self.labels()
        self.expect(K.COLON, "':' after the case label")
        if self.at(K.NEWLINE):
            self.fail(
                "E0040",
                self.peek().span,
                "an arm of a case expression takes one expression on the same line",
            )
        value = self.expr()
        self.end_line()
        return A.CaseExprArm(self.finish(start), labels, value)

    # ------------------------------------------------------------- expressions

    def expr(self):
        if self.at(K.IF):
            return self.if_expr()
        return self.or_expr()

    def if_expr(self) -> A.IfExpr:
        start = self.advance().span
        branches = []
        while True:
            cstart = self.peek().span
            cond = self.expr()
            self.expect(K.THEN, "'then'")
            value = self.expr()
            branches.append(A.CondExpr(self.finish(cstart), cond, value))
            if self.at(K.ELSE) and self.peek(1).kind is K.IF:
                self.advance()
                self.advance()
                continue
            break
        if self.accept(K.ELSE):
            else_value = self.expr()
        else:
            here = _point(self.last.span, at_end=True)
            self.report("E0801", here, "an 'if' expression needs an 'else' branch")
            else_value = A.ErrorExpr(here)
        return A.IfExpr(self.finish(start), branches, else_value)

    def _binary(self, operand: Callable, ops: set[K]):
        left = operand()
        while self.peek().kind in ops:
            op = self.advance()
            right = operand()
            left = A.Binary(left.span.join(right.span), op.text, left, right, op.span)
        return left

    def or_expr(self):
        return self._binary(self.and_expr, {K.OR})

    def and_expr(self):
        return self._binary(self.not_expr, {K.AND})

    def not_expr(self):
        if self.at(K.NOT):
            start = self.advance().span
            operand = self.not_expr()
            return A.Unary(start.join(operand.span), "not", operand)
        return self.comparison()

    def comparison(self):
        left = self.additive()
        chained = False
        while self.peek().kind in _COMPARISONS:
            op = self.advance()
            if chained:
                self.report(
                    "E0043",
                    op.span,
                    "comparisons can't be chained; write 'a < b and b < c'",
                )
            right = self.additive()
            left = A.Binary(left.span.join(right.span), op.text, left, right, op.span)
            chained = True
        return left

    def additive(self):
        return self._binary(self.multiplicative, _ADDITIVE)

    def multiplicative(self):
        return self._binary(self.unary, _MULTIPLICATIVE)

    def unary(self):
        if self.at(K.MINUS, K.TRY, K.NOT):
            # `not` belongs one level above comparisons; it's accepted here too, since an
            # operand position (`x = not y`) leaves no ambiguity.
            op = self.advance()
            operand = self.unary()
            return A.Unary(op.span.join(operand.span), op.text, operand)
        return self.postfix(self.primary())

    def postfix(self, e):
        while True:
            if self.at(K.LPAREN):
                args = self.call_args()
                e = A.Call(e.span.join(self.last.span), e, args)
            elif self.at(K.LBRACKET):
                items = self.bracket_items(False)
                e = A.Index(e.span.join(self.last.span), e, items)
            elif self.at(K.DOT):
                self.advance()
                name = self.ident("field or function name after '.'")
                e = A.FieldAccess(e.span.join(name.span), e, name)
            else:
                return e

    def call_args(self) -> list[A.Arg]:
        self.advance()
        args: list[A.Arg] = []
        named_seen = False
        while not self.at(K.RPAREN):
            start = self.peek().span
            name = None
            if self.at(K.IDENT) and self.peek(1).kind is K.COLON:
                name = self.ident("argument name")
                self.advance()
                named_seen = True
            elif named_seen:
                self.report(
                    "E0040", start, "positional arguments must come before named arguments"
                )
            is_var = self.accept(K.VAR) is not None
            value = self.expr()
            args.append(A.Arg(self.finish(start), name, is_var, value))
            if not self.accept(K.COMMA):
                break
        self.expect(K.RPAREN, "')' to close the argument list")
        return args

    def primary(self):
        tok = self.peek()
        k = tok.kind
        if k is K.INT:
            self.advance()
            return A.IntLit(tok.span, tok.value)
        if k is K.REAL:
            self.advance()
            return A.RealLit(tok.span, tok.value)
        if k is K.CHAR:
            self.advance()
            return A.CharLit(tok.span, tok.value)
        if k is K.STRING:
            self.advance()
            return A.StringLit(tok.span, tok.value)
        if k is K.STRING_BEGIN:
            return self.interpolation()
        if k in (K.TRUE, K.FALSE):
            self.advance()
            return A.BoolLit(tok.span, k is K.TRUE)
        if k in (K.IDENT, K.THIS, K.THIS_TYPE):
            self.advance()
            return A.Name(tok.span, tok.text)
        if k is K.UNDERSCORE:
            self.advance()
            self.report("E0104", tok.span, f"'{tok.text}' is a discard and can't be read")
            return A.ErrorExpr(tok.span)
        if k is K.LPAREN:
            self.advance()
            inner = self.expr()
            self.expect(K.RPAREN, "')'")
            return A.Paren(self.finish(tok.span), inner)
        if k is K.LBRACKET:
            self.advance()
            items = []
            while not self.at(K.RBRACKET):
                items.append(self.expr())
                if not self.accept(K.COMMA):
                    break
            self.expect(K.RBRACKET, "']' to close the list")
            return A.ListLit(self.finish(tok.span), items)
        if k is K.LBRACE:
            return self.braces()
        if k is K.DEREF:
            self.advance()
            self.expect(K.LPAREN, "'(' after 'deref'")
            target = self.expr()
            self.expect(K.RPAREN, "')'")
            return A.Deref(self.finish(tok.span), target)
        if k is K.IF:
            e = self.if_expr()
            self.report(
                "E0803",
                e.span,
                "an 'if' expression used as an operand must be in parentheses",
                (
                    Fix(
                        "add parentheses",
                        (Edit(_point(e.span), "("), Edit(_point(e.span, at_end=True), ")")),
                    ),
                ),
            )
            return e
        if k is K.CASE:
            self.report(
                "E0804",
                tok.span,
                "a 'case' expression must be the whole right side of ':=', 'var', 'let', "
                "or 'return'",
            )
            return self.case_expr()
        self.fail("E0040", tok.span, f"expected an expression, found {_describe(tok)}")

    def interpolation(self) -> A.InterpString:
        first = self.advance()
        parts: list = [first.value] if first.value else []
        while True:
            parts.append(self.expr())
            if self.at(K.STRING_MID, K.STRING_END):
                tok = self.advance()
                if tok.value:
                    parts.append(tok.value)
                if tok.kind is K.STRING_END:
                    break
            else:
                self.fail(
                    "E0040",
                    self.peek().span,
                    f"expected '}}' to close the placeholder, found {_describe(self.peek())}",
                )
        return A.InterpString(self.finish(first.span), parts)

    def braces(self):
        start = self.advance().span
        if self.accept(K.RBRACE):
            return A.EmptyBraces(self.finish(start))
        first = self.expr()
        if self.accept(K.COLON):
            value = self.expr()
            entries = [A.MapEntry(first.span.join(value.span), first, value)]
            while self.accept(K.COMMA):
                if self.at(K.RBRACE):
                    break
                key = self.expr()
                self.expect(K.COLON, "':' between key and value")
                value = self.expr()
                entries.append(A.MapEntry(key.span.join(value.span), key, value))
            self.expect(K.RBRACE, "'}' to close the map")
            return A.MapLit(self.finish(start), entries)
        items = [first]
        while self.accept(K.COMMA):
            if self.at(K.RBRACE):
                break
            items.append(self.expr())
        self.expect(K.RBRACE, "'}' to close the set")
        return A.SetLit(self.finish(start), items)

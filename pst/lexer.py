"""Lexer for Turbo Pasthon (SPEC §2).

Turns source text into tokens, including the layout tokens NEWLINE / INDENT / DEDENT.
It never stops at the first error: every problem becomes a diagnostic and lexing
continues, so later stages always receive a complete token stream (SPEC §15.6).

String literals always interpolate (SPEC §2.8). A string with placeholders is emitted as

    STRING_BEGIN  <expression tokens>  STRING_MID  <expression tokens>  ...  STRING_END

and a string without placeholders as a single STRING token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .diagnostics import Diagnostic, Edit, Fix, Severity
from .source import LineMap, normalize
from .tokens import KEYWORDS, LAYOUT_KINDS, OPERATORS, Comment, Token, TokenKind as K

INDENT_WIDTH = 4

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEC = r"[0-9](?:_?[0-9])*"
_NUMBER_RE = re.compile(
    rf"(?P<hex>0[xX][0-9a-fA-F](?:_?[0-9a-fA-F])*)"
    rf"|(?P<bin>0[bB][01](?:_?[01])*)"
    rf"|(?P<oct>0[oO][0-7](?:_?[0-7])*)"
    rf"|(?P<real>{_DEC}\.{_DEC}(?:[eE][+-]?{_DEC})?)"
    rf"|(?P<dec>{_DEC})"
)
_WORD_TAIL_RE = re.compile(r"[A-Za-z0-9_]*")
_UNICODE_ESCAPE_RE = re.compile(r"u\{([0-9a-fA-F]{1,6})\}")
_OPENER = {")": "(", "]": "[", "}": "{"}

# `# end <keyword> [<name>]` (SPEC §2.4). Names only after keywords that declare one.
_MARKER_RE = re.compile(
    r"# end (?:(?P<plain>if|while|for|case|test|defer|extern)"
    r"|(?P<named>function|procedure|record|trait)(?: (?P<name>[A-Za-z][A-Za-z0-9_]*))?) *$"
)

_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "{": "{",
}

_CLOSER = {"(": ")", "[": "]", "{": "}"}

# Inside open brackets, a line that starts like this, at or left of the indentation of
# the line that opened them, can't be a continuation: the brackets were never closed.
_STATEMENT_START_RE = re.compile(
    r"(?:(?:var|let|if|while|for|repeat|case|defer|break|continue|pass|return|assert|function"
    r"|procedure|type|const|extern|test|export|unsafe|uses|program|unit)\b"
    r"|[A-Za-z_][A-Za-z0-9_.]*\s*:=(?!=)"
    r"|# end )"
)
_MAX_INT = 2**64 - 1


@dataclass
class LexResult:
    tokens: list[Token]
    comments: list[Comment]
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(d.severity is Severity.ERROR for d in self.diagnostics)


def lex(text: str, file: str = "<input>") -> LexResult:
    return _Lexer(normalize(text), file).run()


class _Lexer:
    def __init__(self, src: str, file: str) -> None:
        self.src = src
        self.n = len(src)
        self.file = file
        self.lines = LineMap(src)
        self.pos = 0
        self.tokens: list[Token] = []
        self.comments: list[Comment] = []
        self.diags: list[Diagnostic] = []
        self.indents = [0]
        # Open brackets: (expected closer, offset of opener).
        self.brackets: list[tuple[str, int]] = []
        # One entry per open string placeholder: bracket depth when it was opened.
        self.placeholders: list[int] = []
        self.line_has_tokens = False

    # ------------------------------------------------------------------ driver

    def run(self) -> LexResult:
        at_line_start = True
        while True:
            if at_line_start:
                at_line_start = False
                if not self._line_start():
                    break
            if self.pos >= self.n:
                break
            c = self.src[self.pos]

            if c == "\n":
                if self.placeholders:
                    self._abort_placeholders()
                if self.brackets and self._next_line_starts_statement(self.pos + 1):
                    self._close_unclosed()
                if not self.brackets:
                    if self.line_has_tokens:
                        self._emit(K.NEWLINE, self.pos, self.pos + 1)
                    self.line_has_tokens = False
                    at_line_start = True
                self.pos += 1
            elif c == " ":
                self.pos += 1
            elif c == "\t":
                self._tab(self.pos)
                self.pos += 1
            elif c == "#":
                self._comment()
            elif c == '"':
                start = self.pos
                self.pos += 1
                self._string_body(start, first=True)
            elif c == "'":
                self._char()
            elif c.isascii() and c.isdigit():
                self._number()
            elif c == "_" or (c.isascii() and c.isalpha()):
                self._word()
            elif c in "([{":
                self.brackets.append((_CLOSER[c], self.pos))
                self._emit_single(c)
            elif c in ")]}":
                if c == "}" and self.placeholders and len(self.brackets) == self.placeholders[-1]:
                    self.placeholders.pop()
                    start = self.pos
                    self.pos += 1
                    self._string_body(start, first=False)
                else:
                    self._close_bracket(c)
            else:
                self._operator_or_error()

        return self._finish()

    def _finish(self) -> LexResult:
        if self.placeholders:
            self._abort_placeholders()
        self._close_unclosed()
        if self.line_has_tokens:
            self._emit(K.NEWLINE, self.n, self.n)
        while len(self.indents) > 1:
            self.indents.pop()
            self._emit(K.DEDENT, self.n, self.n)
        self._emit(K.EOF, self.n, self.n)
        return LexResult(self.tokens, self.comments, self.diags)

    def _close_unclosed(self) -> None:
        """Report every open bracket, offering to close them all after the last token."""
        if not self.brackets:
            return
        closers = "".join(closer for closer, _ in reversed(self.brackets))
        end = self.tokens[-1].span.end if self.tokens else self.pos
        at = self.lines.span(end, end)
        fix = Fix(f"insert '{closers}'", (Edit(at, closers),))
        for closer, offset in self.brackets:
            self._error("E0009", offset, offset + 1, f"unclosed '{_OPENER[closer]}'", (fix,))
        self.brackets.clear()

    def _next_line_starts_statement(self, pos: int) -> bool:
        """Whether the next non-blank, non-comment line (from ``pos``) clearly starts a
        new statement rather than continuing the bracketed expression."""
        src = self.src
        while pos < self.n:
            width = 0
            while pos < self.n and src[pos] in " \t":
                width += INDENT_WIDTH if src[pos] == "\t" else 1
                pos += 1
            if pos >= self.n:
                return False
            if src[pos] == "\n" or (src[pos] == "#" and not src.startswith("# end ", pos)):
                nl = src.find("\n", pos)
                if nl < 0:
                    return False
                pos = nl + 1
                continue
            return width <= self.indents[-1] and _STATEMENT_START_RE.match(src, pos) is not None
        return False

    # ------------------------------------------------------------- indentation

    def _line_start(self) -> bool:
        """Handle indentation at the start of a logical line.

        Skips blank and comment-only lines. End-marker lines count as content lines,
        because a marker sits at the indentation of the block it closes. Returns False
        at end of input.
        """
        src = self.src
        while True:
            line_begin = self.pos
            width = 0
            while self.pos < self.n and src[self.pos] in " \t":
                if src[self.pos] == "\t":
                    self._tab(self.pos)
                    width += INDENT_WIDTH
                else:
                    width += 1
                self.pos += 1
            if self.pos >= self.n:
                return False
            c = src[self.pos]
            if c == "\n":
                self.pos += 1
                continue
            if c == "#":
                m = _MARKER_RE.match(src, self.pos, self._line_end())
                if m is None:
                    self._comment()
                    if self.pos < self.n:
                        self.pos += 1  # the newline
                    continue
                self._indent(width, line_begin)
                self._marker(m)
                return True
            self._indent(width, line_begin)
            return True

    def _indent(self, width: int, line_begin: int) -> None:
        if width % INDENT_WIDTH:
            self._error(
                "E0002",
                line_begin,
                self.pos,
                f"indentation of {width} spaces is not a multiple of {INDENT_WIDTH}",
            )
        top = self.indents[-1]
        if width > top:
            if width - top > INDENT_WIDTH:
                self._error(
                    "E0003",
                    line_begin,
                    self.pos,
                    f"indentation increases by {width - top} spaces; "
                    f"one level is {INDENT_WIDTH}",
                )
            self.indents.append(width)
            self._emit(K.INDENT, self.pos, self.pos)
        elif width < top:
            while len(self.indents) > 1 and self.indents[-1] > width:
                self.indents.pop()
                self._emit(K.DEDENT, self.pos, self.pos)
            # A width between two levels was already reported as E0002; continue at
            # the enclosing level.

    # ---------------------------------------------------------------- comments

    def _line_end(self) -> int:
        end = self.src.find("\n", self.pos)
        return self.n if end < 0 else end

    def _comment(self) -> None:
        start = self.pos
        self.pos = self._line_end()
        text = self.src[start : self.pos]
        self.comments.append(
            Comment(text, self.lines.span(start, self.pos), text.startswith("##"))
        )

    def _marker(self, m: re.Match) -> None:
        start = self.pos
        self.pos = m.end()
        keyword = m.group("plain") or m.group("named")
        self._emit(K.END_MARKER, start, self.pos, (keyword, m.group("name")))

    # ------------------------------------------------------------- whitespace

    def _tab(self, offset: int) -> None:
        span = self.lines.span(offset, offset + 1)
        self.diags.append(
            Diagnostic(
                self.file,
                span,
                Severity.ERROR,
                "E0001",
                "tab character; indent with 4 spaces",
                (Fix("replace tab with 4 spaces", (Edit(span, " " * INDENT_WIDTH),)),),
            )
        )

    # ------------------------------------------------------------------ words

    def _word(self) -> None:
        start = self.pos
        m = _IDENT_RE.match(self.src, self.pos)
        assert m is not None
        self.pos = m.end()
        text = m.group()
        if text.startswith("_"):
            # `_` or `_name`: a discard (SPEC §7.9). The text keeps the name.
            self._emit(K.UNDERSCORE, start, self.pos)
            return
        self._emit(KEYWORDS.get(text, K.IDENT), start, self.pos)

    # ---------------------------------------------------------------- numbers

    def _number(self) -> None:
        start = self.pos
        m = _NUMBER_RE.match(self.src, self.pos)
        assert m is not None  # starts with a digit, so `dec` always matches
        self.pos = m.end()

        tail = _WORD_TAIL_RE.match(self.src, self.pos)
        if tail is not None and tail.end() > self.pos:
            self.pos = tail.end()
            text = self.src[start : self.pos]
            hint = ""
            if m.lastgroup == "dec" and re.fullmatch(r"[0-9_]+[eE][+-]?[0-9_]*", text):
                hint = "; a real literal must contain '.', e.g. 1.0e5"
            elif "__" in text or text.endswith("_"):
                hint = "; '_' may only appear between two digits"
            self._error("E0007", start, self.pos, f"malformed number literal '{text}'{hint}")
            self._emit(K.INT, start, self.pos, 0)
            return

        text = m.group()
        digits = text.replace("_", "")
        kind = m.lastgroup
        if kind == "real":
            self._emit(K.REAL, start, self.pos, float(digits))
            return
        base = {"hex": 16, "bin": 2, "oct": 8, "dec": 10}[kind]
        value = int(digits[2:] if base != 10 else digits, base)
        if value > _MAX_INT:
            self._error(
                "E0007", start, self.pos, f"integer literal '{text}' does not fit in 64 bits"
            )
            value = 0
        self._emit(K.INT, start, self.pos, value)

    # ---------------------------------------------------------------- strings

    def _string_body(self, start: int, first: bool) -> None:
        """Scan string text up to the closing quote or the next placeholder.

        ``start`` is the offset of the opening '"' (first segment) or of the '}' that
        closed the previous placeholder. ``self.pos`` is just past it.
        """
        src = self.src
        buf: list[str] = []
        while True:
            if self.pos >= self.n or src[self.pos] == "\n":
                self._error("E0004", start, self.pos, "unterminated string literal")
                self._emit(K.STRING if first else K.STRING_END, start, self.pos, "".join(buf))
                return
            c = src[self.pos]
            if c == '"':
                self.pos += 1
                self._emit(K.STRING if first else K.STRING_END, start, self.pos, "".join(buf))
                return
            if c == "\\":
                buf.append(self._escape())
                continue
            if c == "{":
                j = self.pos + 1
                while j < self.n and src[j] == " ":
                    j += 1
                if j < self.n and src[j] == "}":
                    brace = self.lines.span(self.pos, self.pos + 1)
                    self._error(
                        "E0030",
                        self.pos,
                        j + 1,
                        "empty placeholder '{}'; write '\\{' for a literal brace",
                        (Fix("escape the brace", (Edit(brace, "\\{"),)),),
                    )
                    buf.append(src[self.pos : j + 1])
                    self.pos = j + 1
                    continue
                self.pos += 1
                self._emit(
                    K.STRING_BEGIN if first else K.STRING_MID, start, self.pos, "".join(buf)
                )
                self.placeholders.append(len(self.brackets))
                return
            buf.append(c)
            self.pos += 1

    def _abort_placeholders(self) -> None:
        """A line ended inside a placeholder: close every open string on this line."""
        self._error(
            "E0030",
            self.pos,
            self.pos,
            "string placeholder '{' is not closed on this line; "
            "write '\\{' for a literal brace",
        )
        del self.brackets[self.placeholders[0] :]
        for _ in self.placeholders:
            self._emit(K.STRING_END, self.pos, self.pos, "")
        self.placeholders.clear()

    def _char(self) -> None:
        start = self.pos
        self.pos += 1
        chars: list[str] = []
        while self.pos < self.n and self.src[self.pos] not in "'\n":
            if self.src[self.pos] == "\\":
                chars.append(self._escape())
            else:
                chars.append(self.src[self.pos])
                self.pos += 1
        if self.pos < self.n and self.src[self.pos] == "'":
            self.pos += 1
        else:
            self._error("E0008", start, self.pos, "unterminated char literal")
        if len(chars) != 1:
            self._error(
                "E0008",
                start,
                self.pos,
                f"char literal must contain exactly one character, found {len(chars)}",
            )
        self._emit(K.CHAR, start, self.pos, chars[0] if len(chars) == 1 else "\0")

    def _escape(self) -> str:
        """Decode the escape at self.pos (a backslash) and advance past it."""
        start = self.pos
        self.pos += 1
        if self.pos >= self.n or self.src[self.pos] == "\n":
            self._error("E0005", start, self.pos, "incomplete escape sequence")
            return ""
        c = self.src[self.pos]
        if c in _ESCAPES:
            self.pos += 1
            return _ESCAPES[c]
        if c == "u":
            m = _UNICODE_ESCAPE_RE.match(self.src, self.pos)
            if m is not None:
                self.pos = m.end()
                cp = int(m.group(1), 16)
                if cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF:
                    return chr(cp)
                self._error(
                    "E0005", start, self.pos, f"'\\u{{{m.group(1)}}}' is not a Unicode scalar value"
                )
                return "�"
            self.pos += 1
            # Swallow a malformed `{...}` so it is not mistaken for a placeholder.
            if self.pos < self.n and self.src[self.pos] == "{":
                close = self.src.find("}", self.pos, self._line_end())
                if close >= 0:
                    self.pos = close + 1
            self._error("E0005", start, self.pos, "expected '\\u{XXXX}' with 1 to 6 hex digits")
            return "�"
        self.pos += 1
        self._error("E0005", start, self.pos, f"invalid escape sequence '\\{c}'")
        return c

    # ------------------------------------------------------- operators, brackets

    def _emit_single(self, c: str) -> None:
        start = self.pos
        self.pos += 1
        self._emit(K(c), start, self.pos)

    def _close_bracket(self, c: str) -> None:
        start = self.pos
        self.pos += 1
        self._emit(K(c), start, self.pos)
        floor = self.placeholders[-1] if self.placeholders else 0
        if len(self.brackets) > floor and self.brackets[-1][0] == c:
            self.brackets.pop()
            return
        expected = self.brackets[-1][0] if len(self.brackets) > floor else None
        msg = f"unmatched '{c}'" + (f"; expected '{expected}'" if expected else "")
        self._error("E0009", start, self.pos, msg)
        # Recover: if this closer matches an outer opener, close everything up to it.
        for i in range(len(self.brackets) - 1, floor - 1, -1):
            if self.brackets[i][0] == c:
                del self.brackets[i:]
                break

    def _operator_or_error(self) -> None:
        for text, kind in OPERATORS:
            if self.src.startswith(text, self.pos):
                start = self.pos
                self.pos += len(text)
                self._emit(kind, start, self.pos)
                return
        c = self.src[self.pos]
        start = self.pos
        self.pos += 1
        hint = ""
        if c in "“”":
            hint = "; use a plain '\"'"
        elif c in "‘’":
            hint = "; use a plain \"'\""
        elif c == "^":
            hint = "; dereference with deref(r)"
        elif c in "&!%":
            hint = {"&": "; use 'and'", "!": "; use 'not'", "%": "; use 'mod'"}[c]
        elif c == ";":
            hint = "; statements end at the end of the line"
        self._error("E0006", start, self.pos, f"unexpected character '{c}' (U+{ord(c):04X}){hint}")

    # ---------------------------------------------------------------- helpers

    def _emit(self, kind: K, start: int, end: int, value=None) -> None:
        self.tokens.append(Token(kind, self.src[start:end], self.lines.span(start, end), value))
        if kind not in LAYOUT_KINDS:
            self.line_has_tokens = True

    def _error(self, code: str, start: int, end: int, message: str, fixes=()) -> None:
        self.diags.append(
            Diagnostic(
                self.file, self.lines.span(start, end), Severity.ERROR, code, message, tuple(fixes)
            )
        )

"""Token kinds for Turbo Pasthon (SPEC §2)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from .source import Span


class TokenKind(Enum):
    # Names and literals
    IDENT = "identifier"
    INT = "integer literal"
    REAL = "real literal"
    CHAR = "char literal"
    STRING = "string literal"  # no placeholders
    STRING_BEGIN = "string start"  # text before the first placeholder
    STRING_MID = "string middle"  # text between two placeholders
    STRING_END = "string end"  # text after the last placeholder

    # Layout
    NEWLINE = "newline"
    INDENT = "indent"
    DEDENT = "dedent"
    END_MARKER = "end marker"  # `# end <keyword> [<name>]`, value = (keyword, name)
    EOF = "end of file"

    # Keywords (SPEC §2.6)
    AND = "and"
    ASSERT = "assert"
    BREAK = "break"
    CASE = "case"
    CONST = "const"
    CONTINUE = "continue"
    DEFER = "defer"
    DEREF = "deref"
    DIV = "div"
    DO = "do"
    DOWNTO = "downto"
    ELSE = "else"
    ENSURES = "ensures"
    EXPORT = "export"
    EXTERN = "extern"
    FALSE = "false"
    FOR = "for"
    FUNCTION = "function"
    IF = "if"
    IN = "in"
    LET = "let"
    MOD = "mod"
    NOT = "not"
    OF = "of"
    OR = "or"
    PROCEDURE = "procedure"
    PROGRAM = "program"
    RECORD = "record"
    REF = "ref"
    REPEAT = "repeat"
    REQUIRES = "requires"
    RETURN = "return"
    TEST = "test"
    THEN = "then"
    THIS = "this"
    THIS_TYPE = "This"
    TO = "to"
    TRAIT = "trait"
    TRUE = "true"
    TRY = "try"
    TYPE = "type"
    UNIT = "unit"
    UNSAFE = "unsafe"
    UNTIL = "until"
    USES = "uses"
    VAR = "var"
    WHILE = "while"
    UNDERSCORE = "_"

    # Operators and punctuation
    ASSIGN = ":="
    EQ = "="
    NE = "<>"
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    PLUS = "+"
    MINUS = "-"
    STAR = "*"
    SLASH = "/"
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    LBRACE = "{"
    RBRACE = "}"
    COMMA = ","
    COLON = ":"
    DOT = "."
    DOTDOT = ".."
    ELLIPSIS = "..."
    PIPE = "|"
    ARROW = "=>"  # reserved for v0.2 lambdas


_FIRST_KEYWORD = list(TokenKind).index(TokenKind.AND)
_FIRST_OPERATOR = list(TokenKind).index(TokenKind.ASSIGN)

KEYWORDS: dict[str, TokenKind] = {
    k.value: k for k in list(TokenKind)[_FIRST_KEYWORD:_FIRST_OPERATOR]
}

# Longest first, so that "..." wins over ".." over ".".
OPERATORS: list[tuple[str, TokenKind]] = sorted(
    ((k.value, k) for k in list(TokenKind)[_FIRST_OPERATOR:]),
    key=lambda pair: -len(pair[0]),
)

LAYOUT_KINDS = frozenset(
    {TokenKind.NEWLINE, TokenKind.INDENT, TokenKind.DEDENT, TokenKind.EOF}
)


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str  # exact source text
    span: Span
    value: Any = None  # decoded literal value, or (keyword, name) for END_MARKER

    def to_json(self) -> dict:
        out = {"kind": self.kind.name, "text": self.text, **self.span.to_json()}
        if self.value is not None:
            out["value"] = list(self.value) if isinstance(self.value, tuple) else self.value
        return out


@dataclass(frozen=True, slots=True)
class Comment:
    text: str  # including the leading '#'
    span: Span
    is_doc: bool  # starts with '##'

import unittest

from pst.lexer import lex
from pst.tokens import TokenKind as K


def kinds(src: str) -> list[K]:
    return [t.kind for t in lex(src).tokens]


def codes(src: str) -> list[str]:
    return [d.code for d in lex(src).diagnostics]


class Basics(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(kinds(""), [K.EOF])
        self.assertEqual(kinds("\n\n   \n# comment\n"), [K.EOF])

    def test_assignment(self):
        self.assertEqual(
            kinds("x := 1 + 2\n"),
            [K.IDENT, K.ASSIGN, K.INT, K.PLUS, K.INT, K.NEWLINE, K.EOF],
        )

    def test_no_trailing_newline(self):
        self.assertEqual(kinds("x"), [K.IDENT, K.NEWLINE, K.EOF])

    def test_keywords_case_sensitive(self):
        self.assertEqual(kinds("this This If")[:3], [K.THIS, K.THIS_TYPE, K.IDENT])
        self.assertEqual(kinds('f() as "_exit"')[3:5], [K.AS, K.STRING])

    def test_underscore(self):
        self.assertEqual(kinds("_ := f()")[:2], [K.UNDERSCORE, K.ASSIGN])
        toks = lex("_status := close(fd)").tokens
        self.assertEqual((toks[0].kind, toks[0].text), (K.UNDERSCORE, "_status"))
        self.assertEqual(kinds("x_y")[0], K.IDENT)

    def test_operators_longest_match(self):
        self.assertEqual(
            kinds("a..b ... <= <> >= := =>")[:-2],
            [K.IDENT, K.DOTDOT, K.IDENT, K.ELLIPSIS, K.LE, K.NE, K.GE, K.ASSIGN, K.ARROW],
        )

    def test_positions(self):
        toks = lex("x := 1\n  \ny\n").tokens
        x, y = toks[0], toks[4]
        self.assertEqual((x.span.line, x.span.col, x.span.end_col), (1, 1, 2))
        self.assertEqual((y.span.line, y.span.col), (3, 1))

    def test_columns_count_code_points(self):
        toks = lex('s := "héllo" x').tokens
        self.assertEqual(toks[3].span.col, 14)

    def test_crlf_and_bom(self):
        self.assertEqual(kinds("﻿a\r\nb\r\n"), kinds("a\nb\n"))


class Layout(unittest.TestCase):
    SRC = "if a then\n    b\n    if c then\n        d\ne\n"

    def test_indent_dedent(self):
        self.assertEqual(
            kinds(self.SRC),
            [
                K.IF, K.IDENT, K.THEN, K.NEWLINE,
                K.INDENT, K.IDENT, K.NEWLINE,
                K.IF, K.IDENT, K.THEN, K.NEWLINE,
                K.INDENT, K.IDENT, K.NEWLINE,
                K.DEDENT, K.DEDENT, K.IDENT, K.NEWLINE, K.EOF,
            ],
        )  # fmt: skip

    def test_dedent_at_eof(self):
        self.assertEqual(kinds("if a then\n    b")[-3:], [K.NEWLINE, K.DEDENT, K.EOF])

    def test_blank_and_comment_lines_ignored(self):
        src = "if a then\n\n# top comment\n    b\n        # deep comment\n    c\n"
        self.assertEqual(kinds(src).count(K.INDENT), 1)
        self.assertEqual(codes(src), [])

    def test_newline_suppressed_in_brackets(self):
        src = "f(a,\n  b,\n        c)\nx\n"
        self.assertEqual(
            kinds(src),
            [K.IDENT, K.LPAREN, K.IDENT, K.COMMA, K.IDENT, K.COMMA, K.IDENT, K.RPAREN,
             K.NEWLINE, K.IDENT, K.NEWLINE, K.EOF],
        )  # fmt: skip
        self.assertEqual(codes(src), [])

    def test_bad_width(self):
        self.assertEqual(codes("if a then\n   b\n"), ["E0002"])

    def test_double_indent(self):
        self.assertEqual(codes("if a then\n        b\n"), ["E0003"])

    def test_tab(self):
        r = lex("if a then\n\tb\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0001"])
        self.assertEqual(r.diagnostics[0].fixes[0].edits[0].text, "    ")
        self.assertIn(K.INDENT, [t.kind for t in r.tokens])  # tab counted as 4

    def test_end_marker(self):
        src = "function f(): Int\n    return 1\n# end function f\n"
        toks = lex(src).tokens
        marker = [t for t in toks if t.kind is K.END_MARKER]
        self.assertEqual(len(marker), 1)
        self.assertEqual(marker[0].value, ("function", "f"))
        # The marker line dedents like any statement at that level.
        i = toks.index(marker[0])
        self.assertEqual(toks[i - 1].kind, K.DEDENT)
        self.assertEqual(toks[i + 1].kind, K.NEWLINE)

    def test_marker_lookalikes_are_comments(self):
        for src in ("# end iff\n", "# end if x\n", "# ends here\n", "#end if\n"):
            r = lex(src)
            self.assertEqual([t.kind for t in r.tokens], [K.EOF], src)
            self.assertEqual(len(r.comments), 1)

    def test_trailing_comment(self):
        r = lex("x := 1 # one\n")
        self.assertEqual(kinds("x := 1 # one\n"), kinds("x := 1\n"))
        self.assertEqual(r.comments[0].text, "# one")

    def test_doc_comment(self):
        r = lex("## Adds.\nfunction f()\n")
        self.assertTrue(r.comments[0].is_doc)


class Numbers(unittest.TestCase):
    def value(self, src):
        t = lex(src).tokens[0]
        return t.kind, t.value

    def test_ints(self):
        self.assertEqual(self.value("1_000_000"), (K.INT, 1_000_000))
        self.assertEqual(self.value("0xFF"), (K.INT, 255))
        self.assertEqual(self.value("0b1010"), (K.INT, 10))
        self.assertEqual(self.value("0o17"), (K.INT, 15))

    def test_reals(self):
        self.assertEqual(self.value("3.25"), (K.REAL, 3.25))
        self.assertEqual(self.value("1.5e3"), (K.REAL, 1500.0))
        self.assertEqual(self.value("2.0E-2"), (K.REAL, 0.02))

    def test_range_is_not_real(self):
        self.assertEqual(kinds("0..9")[:3], [K.INT, K.DOTDOT, K.INT])

    def test_method_on_int(self):
        self.assertEqual(kinds("1.abs")[:3], [K.INT, K.DOT, K.IDENT])

    def test_malformed(self):
        for src in ("12abc", "1e5", "1__0", "1_", "0xZZ"):
            self.assertEqual(codes(src), ["E0007"], src)

    def test_too_big(self):
        self.assertEqual(codes("18446744073709551615"), [])
        self.assertEqual(codes("18446744073709551616"), ["E0007"])


class Strings(unittest.TestCase):
    def test_plain(self):
        t = lex('"hi\\n\\"x\\" \\{y}"').tokens[0]
        self.assertEqual(t.kind, K.STRING)
        self.assertEqual(t.value, 'hi\n"x" {y}')

    def test_unicode_escape(self):
        self.assertEqual(lex('"\\u{1F600}"').tokens[0].value, "\U0001F600")
        self.assertEqual(codes('"\\u{D800}"'), ["E0005"])
        self.assertEqual(codes('"\\u{}"'), ["E0005"])

    def test_bad_escape(self):
        self.assertEqual(codes('"\\q"'), ["E0005"])

    def test_unterminated(self):
        r = lex('x := "abc\ny\n')
        self.assertEqual([d.code for d in r.diagnostics], ["E0004"])
        self.assertEqual(
            [t.kind for t in r.tokens],
            [K.IDENT, K.ASSIGN, K.STRING, K.NEWLINE, K.IDENT, K.NEWLINE, K.EOF],
        )

    def test_interpolation(self):
        toks = lex('"a{x}b{f(y)}c"').tokens
        self.assertEqual(
            [t.kind for t in toks][:-2],
            [K.STRING_BEGIN, K.IDENT, K.STRING_MID, K.IDENT, K.LPAREN, K.IDENT,
             K.RPAREN, K.STRING_END],
        )  # fmt: skip
        self.assertEqual([toks[0].value, toks[2].value, toks[7].value], ["a", "b", "c"])

    def test_nested_string_in_placeholder(self):
        toks = lex('"x{g("in{y}")}z"').tokens
        self.assertEqual(
            [t.kind for t in toks][:-2],
            [K.STRING_BEGIN, K.IDENT, K.LPAREN, K.STRING_BEGIN, K.IDENT, K.STRING_END,
             K.RPAREN, K.STRING_END],
        )  # fmt: skip
        self.assertEqual(codes('"x{g("in{y}")}z"'), [])

    def test_braces_inside_placeholder(self):
        # Inner `{...}` is a bracket pair, not the end of the placeholder.
        self.assertEqual(kinds('"{ {1} }"')[:-2],
                         [K.STRING_BEGIN, K.LBRACE, K.INT, K.RBRACE, K.STRING_END])  # fmt: skip

    def test_empty_placeholder(self):
        r = lex('"a{}b"')
        self.assertEqual([d.code for d in r.diagnostics], ["E0030"])
        self.assertEqual(r.tokens[0].kind, K.STRING)
        self.assertEqual(r.diagnostics[0].fixes[0].edits[0].text, "\\{")

    def test_unclosed_placeholder(self):
        r = lex('s := "a{x\ny\n')
        self.assertEqual([d.code for d in r.diagnostics], ["E0030"])
        self.assertEqual(
            [t.kind for t in r.tokens],
            [K.IDENT, K.ASSIGN, K.STRING_BEGIN, K.IDENT, K.STRING_END, K.NEWLINE,
             K.IDENT, K.NEWLINE, K.EOF],
        )  # fmt: skip

    def test_unclosed_bracket_in_placeholder(self):
        r = lex('"{f(x}"\ny\n')
        self.assertIn("E0009", [d.code for d in r.diagnostics])
        self.assertEqual([t.kind for t in r.tokens][-3:], [K.IDENT, K.NEWLINE, K.EOF])


class Chars(unittest.TestCase):
    def test_char(self):
        self.assertEqual(lex("'a'").tokens[0].value, "a")
        self.assertEqual(lex("'\\n'").tokens[0].value, "\n")
        self.assertEqual(lex("'é'").tokens[0].value, "é")

    def test_bad_char(self):
        self.assertEqual(codes("''"), ["E0008"])
        self.assertEqual(codes("'ab'"), ["E0008"])
        self.assertEqual(codes("'a"), ["E0008"])


class Brackets(unittest.TestCase):
    def test_unclosed(self):
        self.assertEqual(codes("f(a"), ["E0009"])

    def test_unmatched_closer(self):
        self.assertEqual(codes("a)"), ["E0009"])

    def test_mismatch_recovers(self):
        r = lex("f([a)\nx\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0009"])
        # Recovery closed both brackets, so the newline ends the statement.
        self.assertEqual([t.kind for t in r.tokens][-4:], [K.NEWLINE, K.IDENT, K.NEWLINE, K.EOF])


class Errors(unittest.TestCase):
    def test_unexpected_char(self):
        r = lex("a ^ b; c\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0006", "E0006"])
        self.assertIn("deref", r.diagnostics[0].message)
        self.assertEqual(kinds("a ^ b; c\n").count(K.IDENT), 3)

    def test_non_ascii_identifier(self):
        self.assertEqual(codes("é := 1"), ["E0006"])

    def test_keeps_going(self):
        src = "x := 1abc\ny := 'zz'\nz := \"q\\k\"\n\tw\n"
        self.assertEqual(codes(src), ["E0007", "E0008", "E0005", "E0001"])

    def test_json(self):
        d = lex("a )").diagnostics[0].to_json()
        self.assertEqual(
            {k: d[k] for k in ("line", "col", "endLine", "endCol", "code", "severity")},
            {"line": 1, "col": 3, "endLine": 1, "endCol": 4, "code": "E0009",
             "severity": "error"},
        )  # fmt: skip


if __name__ == "__main__":
    unittest.main()

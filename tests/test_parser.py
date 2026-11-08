import unittest
from pathlib import Path

from pst import ast as A
from pst.parser import parse

ROOT = Path(__file__).resolve().parent.parent


def tree(src: str, header: str = "program P\n") -> str:
    """Dump of the file's items, asserting there are no diagnostics."""
    r = parse(header + src)
    assert not r.diagnostics, [d.human() for d in r.diagnostics]
    return " ".join(A.dump(item) for item in r.file.items)


def expr(src: str) -> str:
    return tree(f"x := {src}\n").removeprefix("(Assign (Name x) ").removesuffix(")")


def codes(src: str, header: str = "program P\n") -> list[str]:
    return [d.code for d in parse(header + src).diagnostics]


class Expressions(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(
            expr("1 + 2 * 3"),
            "(Binary + (IntLit 1) (Binary * (IntLit 2) (IntLit 3)))",
        )
        self.assertEqual(
            expr("a or b and not c = d"),
            "(Binary or (Name a) (Binary and (Name b) (Unary not (Binary = (Name c) (Name d)))))",
        )
        self.assertEqual(
            expr("-a.b(c)[d]"),
            "(Unary - (Index (Call (FieldAccess (Name a) (Ident b)) [(Arg (Name c))]) [(Name d)]))",
        )

    def test_left_associative(self):
        self.assertEqual(
            expr("a - b - c"), "(Binary - (Binary - (Name a) (Name b)) (Name c))"
        )

    def test_try(self):
        self.assertEqual(
            expr("total + try f(x)"),
            "(Binary + (Name total) (Unary try (Call (Name f) [(Arg (Name x))])))",
        )

    def test_literals(self):
        self.assertEqual(expr("[1, 2,]"), "(ListLit [(IntLit 1) (IntLit 2)])")
        self.assertEqual(expr("{1: 'a'}"), "(MapLit [(MapEntry (IntLit 1) (CharLit a))])")
        self.assertEqual(expr("{1, 2}"), "(SetLit [(IntLit 1) (IntLit 2)])")
        self.assertEqual(expr("{}"), "(EmptyBraces)")
        self.assertEqual(expr("true"), "(BoolLit true)")
        self.assertEqual(expr("false"), "(BoolLit false)")
        self.assertEqual(expr("2.5"), "(RealLit 2.5)")

    def test_interpolation(self):
        self.assertEqual(
            expr('"a{x}b{f(y)}"'),
            '(InterpString [a (Name x) b (Call (Name f) [(Arg (Name y))])])',
        )

    def test_call_args(self):
        self.assertEqual(
            expr("f(var a, b: 1, c: var d)"),
            "(Call (Name f) [(Arg :is_var (Name a)) (Arg (Ident b) (IntLit 1)) "
            "(Arg (Ident c) :is_var (Name d))])",
        )

    def test_index_stays_expression(self):
        self.assertEqual(expr("a[i]"), "(Index (Name a) [(Name i)])")
        self.assertEqual(
            expr("f[ref T](p)"),
            "(Call (Index (Name f) [(RefType (TypeName [T]))]) [(Arg (Name p))])",
        )

    def test_deref(self):
        self.assertEqual(expr("deref(p).x"), "(FieldAccess (Deref (Name p)) (Ident x))")

    def test_if_expr(self):
        self.assertEqual(
            expr("if a then 1 else if b then 2 else 3"),
            "(IfExpr [(CondExpr (Name a) (IntLit 1)) (CondExpr (Name b) (IntLit 2))] (IntLit 3))",
        )
        self.assertEqual(
            expr("1 + (if a then 2 else 3)"),
            "(Binary + (IntLit 1) (Paren (IfExpr [(CondExpr (Name a) (IntLit 2))] (IntLit 3))))",
        )

    def test_case_expr(self):
        self.assertEqual(
            tree('s := case k of\n    1, 2: "low"\n    3..9: "mid"\n    else: "high"\n'),
            '(Assign (Name s) (CaseExpr (Name k) [(CaseExprArm [(ValueLabel (IntLit 1)) '
            '(ValueLabel (IntLit 2))] (StringLit low)) (CaseExprArm [(RangeLabel (IntLit 3) '
            '(IntLit 9))] (StringLit mid)) (CaseExprArm (StringLit high))]))',
        )


class Statements(unittest.TestCase):
    def test_var_let(self):
        self.assertEqual(
            tree("var a: List[Int] := []\nlet b := 1\n"),
            "(VarDecl :mutable (Ident a) (TypeName [List] [(TypeName [Int])]) (ListLit)) "
            "(VarDecl (Ident b) (IntLit 1))",
        )

    def test_assign_targets(self):
        self.assertEqual(
            tree("a.b[c] := 1\nderef(r) := 2\n"),
            "(Assign (Index (FieldAccess (Name a) (Ident b)) [(Name c)]) (IntLit 1)) "
            "(Assign (Deref (Name r)) (IntLit 2))",
        )

    def test_discard(self):
        self.assertEqual(
            tree("_ := f()\n_status := g()\n"),
            "(Discard _ (Call (Name f))) (Discard _status (Call (Name g)))",
        )

    def test_if_else_if(self):
        self.assertEqual(
            tree("if a then\n    f()\nelse if b then\n    g()\nelse\n    h()\n"),
            "(If [(CondBlock (Name a) (Block [(ExprStmt (Call (Name f)))])) "
            "(CondBlock (Name b) (Block [(ExprStmt (Call (Name g)))]))] "
            "(Block [(ExprStmt (Call (Name h)))]))",
        )

    def test_loops(self):
        self.assertEqual(
            tree("while a do\n    f()\nrepeat\n    g()\nuntil b\n"),
            "(While (Name a) (Block [(ExprStmt (Call (Name f)))])) "
            "(Repeat (Block [(ExprStmt (Call (Name g)))]) (Name b))",
        )
        self.assertEqual(
            tree("for i := 9 downto 0 do\n    break\nfor _, v in m do\n    continue\n"),
            "(ForRange (Ident i) (IntLit 9) (IntLit 0) :down (Block [(Break)])) "
            "(ForIn [(Ident _) (Ident v)] (Name m) (Block [(Continue)]))",
        )

    def test_case_stmt(self):
        self.assertEqual(
            tree("case s of\n    Circle(c): f(c)\n    Empty, Square(_):\n        g()\n    else: h()\n"),
            "(Case (Name s) [(CaseArm [(VariantLabel (TypeName [Circle]) (Ident c))] "
            "(ExprStmt (Call (Name f) [(Arg (Name c))]))) (CaseArm [(ValueLabel (Name Empty)) "
            "(VariantLabel (TypeName [Square]) (Ident _))] (Block [(ExprStmt (Call (Name g)))])) "
            "(CaseArm (ExprStmt (Call (Name h))))])",
        )

    def test_defer_return_assert(self):
        self.assertEqual(
            tree("function f(): Int\n    defer close(h)\n    assert x > 0, \"bad\"\n    return 1\n"),
            "(Routine (Signature :is_function (Ident f) (TypeName [Int])) (Block ["
            "(Defer (ExprStmt (Call (Name close) [(Arg (Name h))]))) "
            "(Assert (Binary > (Name x) (IntLit 0)) (StringLit bad)) (Return (IntLit 1))]))",
        )

    def test_try_statement(self):
        self.assertEqual(tree("try f()\n"), "(ExprStmt (Unary try (Call (Name f))))")


class Declarations(unittest.TestCase):
    def test_types(self):
        src = (
            "type\n"
            "    Color = (Red, Green,)\n"
            "    Digit = 0..9\n"
            "    Shape = Circle | Geo.Square | ref Node\n"
            "    Fn = function(Int, String): Bool\n"
            "    Pair[K, V: Hash + Eq] = record\n"
            "        export key: K\n"
            "        value: V := 0\n"
            "    Empty = record\n"
            "    Named = trait\n"
            "        name: String\n"
            "        function greet(this): String\n"
        )
        self.assertEqual(
            tree(src),
            "(TypeSection [(TypeDecl (Ident Color) (EnumType [(Ident Red) (Ident Green)])) "
            "(TypeDecl (Ident Digit) (SubrangeType (IntLit 0) (IntLit 9))) "
            "(TypeDecl (Ident Shape) (UnionType [(TypeName [Circle]) (TypeName [Geo Square]) "
            "(RefType (TypeName [Node]))])) "
            "(TypeDecl (Ident Fn) (FuncType :is_function [(TypeName [Int]) (TypeName [String])] "
            "(TypeName [Bool]))) "
            "(TypeDecl (Ident Pair) [(TypeParam (Ident K)) (TypeParam (Ident V) "
            "[(TypeName [Hash]) (TypeName [Eq])])] (RecordType [(FieldDecl :exported (Ident key) "
            "(TypeName [K])) (FieldDecl (Ident value) (TypeName [V]) (IntLit 0))])) "
            "(TypeDecl (Ident Empty) (RecordType)) "
            "(TypeDecl (Ident Named) (TraitType [(FieldDecl (Ident name) (TypeName [String]))] "
            "[(Signature :is_function (Ident greet) [(Param [(Ident this)] :is_this)] "
            "(TypeName [String]))]))])",
        )

    def test_const(self):
        self.assertEqual(
            tree("const\n    export Max = 10\n    Min = -Max\n"),
            "(ConstSection [(ConstDecl :exported (Ident Max) (IntLit 10)) "
            "(ConstDecl (Ident Min) (Unary - (Name Max)))])",
        )

    def test_routine(self):
        src = (
            "export unsafe function f[T](var this, a, b: T := 1, ...): ref T\n"
            "    requires a > 0\n"
            "    ensures b > 0\n"
            "    return 1\n"
        )
        self.assertEqual(
            tree(src),
            "(Routine :exported :unsafe (Signature :is_function (Ident f) [(TypeParam (Ident T))] "
            "[(Param :is_var [(Ident this)] :is_this) (Param [(Ident a) (Ident b)] "
            "(TypeName [T]) (IntLit 1)) (Param :is_varargs)] (RefType (TypeName [T]))) "
            "[(Binary > (Name a) (IntLit 0))] [(Binary > (Name b) (IntLit 0))] "
            "(Block [(Return (IntLit 1))]))",
        )

    def test_extern(self):
        src = 'extern "libc"\n    procedure exit(code: Int32) as "_exit"\n'
        self.assertEqual(
            tree(src),
            "(ExternBlock libc [(ExternDecl (Signature (Ident exit) [(Param [(Ident code)] "
            "(TypeName [Int32]))]) _exit)])",
        )

    def test_test_block(self):
        self.assertEqual(
            tree('test "it works"\n    assert true\n'),
            '(TestBlock "it works" (Block [(Assert (BoolLit true))]))',
        )

    def test_uses_and_unit(self):
        r = parse("unit Geo\nuses IO, Math\nfunction f(): Int\n    return 1\n")
        self.assertEqual(r.diagnostics, [])
        self.assertEqual((r.file.kind, r.file.name.name), ("unit", "Geo"))
        self.assertEqual([u.name for u in r.file.uses], ["IO", "Math"])

    def test_doc_comments(self):
        r = parse("program P\n## Adds.\n## Really.\nfunction f(): Int\n    return 1\n")
        self.assertEqual(r.file.items[0].doc, "Adds.\nReally.")
        r = parse("program P\n## Not attached.\n\nfunction f(): Int\n    return 1\n")
        self.assertIsNone(r.file.items[0].doc)

    def test_spec_example(self):
        r = parse((ROOT / "examples" / "inventory.pst").read_text(), "inventory.pst")
        self.assertEqual([d.human() for d in r.diagnostics], [])
        kinds = [type(i).__name__ for i in r.file.items]
        self.assertEqual(kinds.count("Routine"), 4)
        self.assertEqual(kinds[-4:], ["VarDecl", "Case", "ForIn", "ExprStmt"])


class EndMarkers(unittest.TestCase):
    def test_attached(self):
        r = parse("program P\nfunction f(): Int\n    return 1\n# end function f\n"
                  "if a then\n    g()\n# end if\n")  # fmt: skip
        self.assertEqual(r.diagnostics, [])
        self.assertIsNotNone(r.file.items[0].end_marker)
        self.assertIsNotNone(r.file.items[1].end_marker)

    def test_nested(self):
        src = "while a do\n    if b then\n        f()\n    # end if\n# end while\n"
        self.assertEqual(codes(src), [])

    def test_record_marker(self):
        src = "type\n    Box = record\n        v: Int\n    # end record Box\n"
        self.assertEqual(codes(src), [])

    def test_mismatch_has_fix(self):
        r = parse("program P\nfunction f(): Int\n    return 1\n# end function g\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0010"])
        self.assertEqual(r.diagnostics[0].fixes[0].edits[0].text, "# end function f")
        self.assertEqual(
            codes("if a then\n    f()\n# end while\n"), ["E0010"]
        )

    def test_stray(self):
        self.assertEqual(codes("x := 1\n# end if\n"), ["E0010"])
        self.assertEqual(codes("# end if\n"), ["E0010"])


class Errors(unittest.TestCase):
    def test_missing_header(self):
        r = parse("x := 1\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0045"])
        self.assertEqual(r.file.kind, "program")

    def test_statement_in_unit(self):
        self.assertEqual(codes("x := 1\n", "unit U\n"), ["E0101"])

    def test_uses_misplaced(self):
        self.assertEqual(codes("x := 1\nuses IO\n"), ["E0044"])

    def test_equals_as_assignment(self):
        r = parse("program P\nx = 5\n")
        d = r.diagnostics[0]
        self.assertEqual(d.code, "E0047")
        self.assertEqual(d.fixes[0].edits[0].text, ":=")
        self.assertEqual(d.fixes[0].edits[0].span.col, 3)

    def test_not_a_statement(self):
        self.assertEqual(codes("a + b\n"), ["E0047"])
        self.assertEqual(codes("f\n"), ["E0047"])

    def test_bad_target(self):
        self.assertEqual(codes("f() := 1\n"), ["E0046"])

    def test_uninitialized(self):
        self.assertEqual(codes("var x: Int\ny := 1\n"), ["E0201"])

    def test_chained_comparison(self):
        self.assertEqual(codes("x := a < b < c\n"), ["E0043"])

    def test_if_expr_errors(self):
        self.assertEqual(codes("x := if a then 1\n"), ["E0801"])
        r = parse("program P\nx := 1 + if a then 2 else 3\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0803"])
        self.assertEqual([e.text for e in r.diagnostics[0].fixes[0].edits], ["(", ")"])

    def test_case_expr_in_operand(self):
        self.assertIn("E0804", codes("x := 1 + case k of\n    else: 2\n"))

    def test_discard_read(self):
        self.assertEqual(codes("x := _y + 1\n"), ["E0104"])

    def test_decl_in_block(self):
        src = "function f(): Int\n    const C = 1\n    procedure g()\n        h()\n    return 1\n"
        r = parse("program P\n" + src)
        self.assertEqual([d.code for d in r.diagnostics], ["E0202", "E0048"])
        body = r.file.items[0].body.stmts
        self.assertEqual([type(s).__name__ for s in body], ["ErrorStmt", "ErrorStmt", "Return"])

    def test_indentation(self):
        self.assertEqual(codes("x := 1\n    y := 2\n"), ["E0041"])
        self.assertEqual(codes("if a then\nf()\n"), ["E0042"])
        self.assertEqual(codes("if a then f()\n"), ["E0049"])

    def test_recovery_one_error_per_line(self):
        src = "x := (1 +)\ny := 2\nif then\n    z := 3\nw := 4\n"
        r = parse("program P\n" + src)
        self.assertEqual([d.span.line for d in r.diagnostics], [2, 4])
        names = [i.target.name for i in r.file.items if isinstance(i, A.Assign)]
        self.assertEqual(names, ["y", "w"])

    def test_lexer_errors_suppress_parser_errors(self):
        r = parse("program P\nx := 1abc +\ny := 2\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0007"])
        r = parse("program P\ny := (1 +\nz := 3\n")
        self.assertEqual([d.code for d in r.diagnostics], ["E0009"])

    def test_never_crashes(self):
        # Every prefix of a real program yields a tree.
        src = (ROOT / "examples" / "inventory.pst").read_text()
        for n in range(0, len(src), 7):
            r = parse(src[:n])
            self.assertIsInstance(r.file, A.File)

    def test_sorted_diagnostics(self):
        r = parse("x = 1\ny := 1abc\n")
        offsets = [d.span.start for d in r.diagnostics]
        self.assertEqual(offsets, sorted(offsets))


if __name__ == "__main__":
    unittest.main()

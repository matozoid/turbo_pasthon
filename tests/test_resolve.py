import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from pst.resolve import STD_DIR, check
from pst.symbols import Kind

ROOT = Path(__file__).resolve().parent.parent


_TMP = tempfile.TemporaryDirectory()


def tearDownModule():
    _TMP.cleanup()


def run(files: dict[str, str], main: str | None = None):
    """Writes ``files`` to a fresh directory and checks the first (or ``main``)."""
    os.makedirs(_TMP.name, exist_ok=True)  # other test modules share it
    root = tempfile.mkdtemp(dir=_TMP.name)
    for name, text in files.items():
        Path(root, name).write_text(textwrap.dedent(text).lstrip("\n"))
    return check(os.path.join(root, main or next(iter(files))))


def codes(result) -> list[tuple[str, str, int, int]]:
    return [
        (os.path.basename(d.file), d.code, d.span.line, d.span.col) for d in result.diagnostics
    ]


def prog(body: str) -> dict[str, str]:
    return {"Main.pst": "program Main\n\nuses IO\n\n" + textwrap.dedent(body).lstrip("\n")}


class Clean(unittest.TestCase):
    def test_example(self):
        r = check(str(ROOT / "examples" / "Inventory.pst"))
        self.assertEqual(r.diagnostics, [])

    def test_standard_units(self):
        for name in sorted(os.listdir(STD_DIR)):
            with self.subTest(name):
                r = check(os.path.join(STD_DIR, name))
                self.assertEqual(r.diagnostics, [])

    def test_hoisting_and_mutual_recursion(self):
        r = run(prog("""
            writeln(isEven(4))

            function isEven(n: Integer): Boolean
                return if n = 0 then true else isOdd(n - 1)

            function isOdd(n: Integer): Boolean
                return if n = 0 then false else isEven(n - 1)
            """))
        self.assertEqual(codes(r), [])

    def test_generic_bound_members(self):
        r = run(prog("""
            type
                Measurable = trait
                    function area(this): Real

            function total[T: Measurable](xs: List[T]): Real
                var sum := 0.0
                for x in xs do
                    sum := sum + area(x)
                return sum
            """))
        self.assertEqual(codes(r), [])
        area_refs = [x for x in r.index.refs if x.targets and x.targets[0].name == "area"]
        self.assertEqual(len(area_refs), 2)  # the trait member and its use
        self.assertTrue(all(x.targets[0].kind is Kind.FUNCTION for x in area_refs))

    def test_contracts(self):
        r = run(prog("""
            procedure push(var xs: List[Integer], x: Integer)
                ensures len(xs) = len(old(xs)) + 1
                add(var xs, x)

            function double(x: Integer): Integer
                ensures result = 2 * x
                return x + x
            """))
        self.assertEqual(codes(r), [])

    def test_qualified_names(self):
        r = run({
            "Main.pst": """
                program Main

                uses IO, Strings

                type
                    Color = (Red, Green)

                writeln(Strings.join(["a"], sep: ""))
                writeln(Color.Green)
                var c: Color := Color.Red
                case c of
                    Red: writeln(1)
                    Green: writeln(2)
                """,
        })
        self.assertEqual(codes(r), [])

    def test_repeat_condition_sees_body(self):
        r = run(prog("""
            repeat
                let line := readln()
            until isNone(line)
            """))
        self.assertEqual(codes(r), [])


class Errors(unittest.TestCase):
    def test_unknown_name_with_fix(self):
        r = run(prog("""
            var total := 1
            writeln(totl)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0100", 6, 9)])
        fix = r.diagnostics[0].fixes[0]
        self.assertEqual(fix.edits[0].text, "total")

    def test_main_local_in_function(self):
        r = run(prog("""
            var total := 1
            writeln(total)

            procedure show()
                writeln(total)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0103", 9, 13)])

    def test_main_local_used_before_declaration(self):
        r = run(prog("""
            writeln(total)
            var total := 1
            writeln(total)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0100", 5, 9)])

    def test_shadowing(self):
        r = run(prog("""
            procedure p(n: Integer)
                if n > 0 then
                    let n := 1
                    writeln(n)
                for i := 0 to n do
                    for i := 0 to n do
                        writeln(i)
            """))
        self.assertEqual(
            codes(r), [("Main.pst", "E0205", 7, 13), ("Main.pst", "E0205", 10, 13)]
        )

    def test_unused_variables(self):
        r = run(prog("""
            var a := 1
            var b: Integer := 2
            b := 3
            for x in [1] do
                writeln(1)
            for i := 0 to 1 do
                writeln(1)
            case readln() of
                Some(v): writeln(1)
                None: writeln(2)
            """))
        self.assertEqual(
            codes(r),
            [
                ("Main.pst", "E0206", 5, 5),
                ("Main.pst", "E0206", 6, 5),
                ("Main.pst", "E0206", 8, 5),
                ("Main.pst", "E0206", 13, 10),
            ],
        )
        edits = [d.fixes[0].edits[0] for d in r.diagnostics]
        self.assertEqual([(e.span.col, e.span.end_col, e.text) for e in edits], [
            (1, 6, "_"),  # `var a` -> `_`
            (1, 15, "_"),  # `var b: Integer` -> `_`
            (5, 6, "_"),
            (10, 11, "_"),
        ])  # fmt: skip

    def test_duplicates_and_overloads(self):
        r = run(prog("""
            type
                Color = (Red, Red)
                Color = record
                    x: Integer
                    x: Real

            function f(x: Integer): Integer
                return x

            function f(x: Real): Real
                return x
            """))
        self.assertEqual(
            codes(r),
            [
                ("Main.pst", "E0108", 6, 19),
                ("Main.pst", "E0108", 7, 5),
                ("Main.pst", "E0108", 9, 9),
            ],
        )

    def test_casing_fix_renames_uses(self):
        r = run(prog("""
            function Twice(x: Integer): Integer
                return x * 2

            writeln(Twice(2))
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0020", 5, 10)])
        edits = r.diagnostics[0].fixes[0].edits
        self.assertEqual([(e.span.line, e.text) for e in edits], [(5, "twice"), (8, "twice")])

    def test_enum_member(self):
        r = run(prog("""
            type
                Color = (Red, Green)

            writeln(Color.Rde)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0100", 8, 15)])
        self.assertEqual(r.diagnostics[0].fixes[0].edits[0].text, "Red")

    def test_this_type_outside_trait(self):
        r = run(prog("""
            function f(x: This): Integer
                return 1
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0111", 5, 15)])

    def test_extern_pst_reserved(self):
        r = run(prog("""
            extern "pst"
                function magic(): Integer
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0106", 5, 1)])


class Units(unittest.TestCase):
    def test_exports(self):
        r = run({
            "Main.pst": """
                program Main

                uses IO, Geo

                writeln(area(Shape.Circle))
                writeln(Geo.area(Geo.Shape.Square))
                writeln(Geo.hidden())
                writeln(hidden())
                """,
            "Geo.pst": """
                unit Geo

                type
                    export Shape = (Circle, Square)

                export function area(s: Shape): Integer
                    return hidden()

                function hidden(): Integer
                    return 2
                """,
        })
        self.assertEqual(
            codes(r), [("Main.pst", "E0109", 7, 13), ("Main.pst", "E0100", 8, 9)]
        )

    def test_unit_not_found_and_program(self):
        r = run({
            "Main.pst": """
                program Main

                uses Nope, Other
                """,
            "Other.pst": """
                program Other
                """,
        })
        self.assertEqual(
            codes(r), [("Main.pst", "E0102", 3, 6), ("Main.pst", "E0102", 3, 12)]
        )

    def test_header_must_match_file(self):
        r = run({"Main.pst": "program Mian\n"})
        self.assertEqual(codes(r), [("Main.pst", "E0105", 1, 9)])
        self.assertEqual(r.diagnostics[0].fixes[0].edits[0].text, "Main")

    def test_cycle(self):
        r = run({
            "Main.pst": "program Main\n\nuses A\n",
            "A.pst": "unit A\n\nuses B\n",
            "B.pst": "unit B\n\nuses A\n",
        })
        self.assertEqual(codes(r), [("B.pst", "E0107", 3, 6)])
        self.assertIn("A -> B -> A", r.diagnostics[0].message)

    def test_ambiguous(self):
        r = run({
            "Main.pst": """
                program Main

                uses A, B

                let p := Point(x: 1)
                let q := A.Point(x: 1)
                _ := combine(p, q)
                """,
            "A.pst": """
                unit A

                type
                    export Point = record
                        export x: Integer

                export function combine(a, b: Point): Integer
                    return 1
                """,
            "B.pst": """
                unit B

                type
                    export Point = record
                        export x: Integer

                export function combine(a, b: Point): Integer
                    return 2
                """,
        })
        self.assertEqual(codes(r), [("Main.pst", "E0110", 5, 10)])
        fixes = [f.edits[0].text for f in r.diagnostics[0].fixes]
        self.assertEqual(fixes, ["A.Point", "B.Point"])
        combine = [x for x in r.index.refs if not x.is_def and x.targets and x.targets[0].name == "combine"]
        self.assertEqual(len(combine[0].targets), 1)  # narrowed by the type of q
        self.assertEqual(combine[0].targets[0].unit, "A")


class SymbolIndex(unittest.TestCase):
    def test_definition_and_references(self):
        r = check(str(ROOT / "examples" / "Inventory.pst"))
        file = str(ROOT / "examples" / "Inventory.pst")
        ref = r.index.at(file, 42, 30)  # `restock` in restockAll
        self.assertIsNotNone(ref)
        (sym,) = ref.targets
        self.assertEqual((sym.kind, sym.name, sym.span.line), (Kind.FUNCTION, "restock", 19))
        uses = [x.span.line for x in r.index.references(sym) if not x.is_def]
        self.assertEqual(uses, [42, 47])


if __name__ == "__main__":
    unittest.main()

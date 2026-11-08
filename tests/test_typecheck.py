import textwrap
import unittest

from tests.test_resolve import codes, prog, run


def errs(body: str) -> list[tuple[str, int, int]]:
    return [(c, line - 4, col) for _, c, line, col in codes(run(prog(body)))]


class Clean(unittest.TestCase):
    def test_inference_and_literals(self):
        self.assertEqual(errs("""
            let a := 1
            let b: Int32 := 2
            let c: Real32 := 1.5
            let xs: List[Int32] := [1, 2, 3]
            var m: Map[String, Integer] := {}
            m["a"] := a
            writeln("{a} {b} {c} {len(xs)} {m["a"]}")
            let r := toReal(a) / 2.0
            writeln(r)
            writeln(7 div 2 + 7 mod 2)
            """), [])

    def test_generics_and_iterators(self):
        self.assertEqual(errs("""
            type
                Shape = trait
                    function area(this): Real

                Square = record
                    side: Real

            function area(s: Square): Real
                return s.side * s.side

            function total[T: Shape](xs: List[T]): Real
                var sum := 0.0
                for x in xs do
                    sum := sum + x.area()
                return sum

            let shapes := [Square(side: 2.0)]
            writeln(total(shapes))
            for c in "abc" do
                writeln(c)
            let doubled := map(iter([1, 2]), double)
            for d in doubled do
                writeln(d)

            function double(x: Integer): Integer
                return x * 2
            """), [])

    def test_option_case_and_try(self):
        self.assertEqual(errs("""
            function half(x: Integer): Option[Integer]
                if x mod 2 = 0 then
                    return Some(x div 2)
                return None

            function quarter(x: Integer): Option[Integer]
                let h := try half(x)
                return half(h)

            case quarter(8) of
                Some(v): writeln(v.value)
                None: writeln("odd")
            """), [])

    def test_enum_case_and_loop(self):
        self.assertEqual(errs("""
            type
                Color = (Red, Green, Blue)

            function name(c: Color): String
                case c of
                    Red: return "r"
                    Green..Blue: return "gb"

            for c in Color do
                writeln(name(c))
            """), [])


class Errors(unittest.TestCase):
    def test_mismatch_with_hint(self):
        r = run(prog("""
            let x: Real := 1
            writeln(x)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0300", 5, 16)])
        self.assertIn("toReal", r.diagnostics[0].message)

    def test_operators(self):
        self.assertEqual(errs("""
            let a := 1 / 2
            let b := 1.0 div 2.0
            let c := "a" + "b"
            writeln("{a}{b}{c}")
            """), [("E0305", 1, 12), ("E0305", 2, 14), ("E0305", 3, 14)])

    def test_missing_return_and_ignored_result(self):
        r = run(prog("""
            function f(x: Integer): Integer
                if x > 0 then
                    return 1

            f(1)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0501", 5, 10), ("Main.pst", "E0502", 9, 1)])
        self.assertEqual(r.diagnostics[1].fixes[0].edits[0].text, "_ := ")

    def test_var_arguments(self):
        r = run(prog("""
            var xs: List[Integer] := []
            add(xs, 1)
            xs.add(2)
            let ys: List[Integer] := []
            add(var ys, 3)
            """))
        self.assertEqual(
            codes(r),
            [("Main.pst", "E0521", 6, 5), ("Main.pst", "E0520", 7, 1), ("Main.pst", "E0341", 9, 9)],
        )
        self.assertEqual(r.diagnostics[1].fixes[0].edits[0].text, "add(var xs, 2)")

    def test_same_typed_groups_need_names(self):
        r = run(prog("""
            procedure move(x: Integer, y: Integer)
                writeln(x + y)

            procedure span(a, b: Integer)
                writeln(a + b)

            move(1, 2)
            move(1, y: 2)
            span(1, 2)
            """))
        self.assertEqual(codes(r), [("Main.pst", "E0530", 11, 9)])
        self.assertEqual(r.diagnostics[0].fixes[0].edits[0].text, "y: ")

    def test_case_coverage(self):
        self.assertEqual(errs("""
            type
                Color = (Red, Green, Blue)

            let c := Color.Red
            case c of
                Red: writeln(1)
                Red: writeln(2)
            case 3 of
                1..5: writeln(1)
                4: writeln(2)
            """), [("E0602", 5, 1), ("E0601", 7, 5), ("E0602", 8, 1), ("E0601", 10, 5)])

    def test_bounds(self):
        self.assertEqual(errs("""
            type
                Point = record
                    x: Real

            function biggest[T: Ordered](a, b: T): T
                return if a > b then a else b

            function sum[T](a, b: T): T
                return a + b

            _ := biggest(Point(x: 1.0), Point(x: 2.0))
            """), [("E0710", 9, 14), ("E0700", 11, 6)])

    def test_map_keys(self):
        """A Map's key type must be Hashable, and maps are written, not constructed."""
        self.assertEqual(errs("""
            function size[K, V](m: Map[K, V]): Integer
                return 0

            var m: Map[Real, Integer] := {}
            m[1.5] := 1
            let n: Map[String, Integer] := Map(keys: [])
            writeln("{len(n)} {size(n)}")
            """), [("E0700", 1, 24), ("E0700", 4, 8), ("E0306", 6, 32)])

    def test_set_elements(self):
        """Set elements are an ordinal type with at most 256 values."""
        self.assertEqual(errs("""
            type
                Digit = '0'..'9'
                Small = -5..5
                Big = 0..300

            let a: Set[Digit] := {}
            let b: Set[Small] := {}
            let c: Set[Real] := {}
            let d: Set[Char] := {}
            let e: Set[Big] := {}
            let f := {1, 2}
            let g: Set[Boolean] := Set(words: [])
            writeln("{len(a)} {len(b)} {len(c)} {len(d)} {len(e)} {len(f)} {len(g)}")
            """), [("E0700", 8, 8), ("E0700", 9, 8), ("E0700", 10, 8), ("E0700", 11, 10), ("E0306", 12, 24)])

    def test_overloads(self):
        self.assertEqual(errs("""
            function f(x: Int32): Integer
                return 1

            function f(x: Int16): Integer
                return 2

            function f(x: Real): Integer
                return 3

            _ := f(1.0)
            _ := f(1)
            _ := f("a")
            """), [("E0510", 11, 6), ("E0511", 12, 6)])

    def test_fields_and_construction(self):
        r = run({
            "Main.pst": """
                program Main

                uses IO, Geo

                let p := Point(x: 1)
                _ := Secret(code: 1)
                writeln(p.y)
                writeln(p.x)
                """,
            "Geo.pst": """
                unit Geo

                type
                    export Point = record
                        export x: Integer

                    export Secret = record
                        code: Integer
                """,
        })
        self.assertEqual(codes(r), [("Main.pst", "E0330", 6, 6), ("Main.pst", "E0320", 7, 11)])

    def test_strings_and_traits(self):
        self.assertEqual(errs("""
            type
                Point = record
                    x: Real

            let s := "abc"
            writeln(s[0])
            writeln("{Point(x: 1.0)}")
            var shown: Printable := 1
            writeln(shown)
            """), [("E0310", 6, 9), ("E0421", 7, 11), ("E0420", 8, 12)])

    def test_try_needs_matching_result(self):
        self.assertEqual(errs("""
            function half(x: Integer): Option[Integer]
                return Some(x)

            function f(x: Integer): Integer
                return try half(x)
            """), [("E0505", 5, 12)])

    def test_no_continue_in_repeat(self):
        self.assertEqual(errs("""
            var n := 0
            repeat
                n := n + 1
                while n < 3 do
                    n := n + 1
                    continue
                if n = 5 then
                    continue
                let done := n > 7
            until done
            """), [("E0506", 8, 9)])

    def test_var_arguments_must_not_overlap(self):
        self.assertEqual(errs("""
            type
                Pair = record
                    a: Integer
                    b: Integer

            procedure both(var x, y: Integer)
                x := y

            procedure mix(var p: Pair, var x: Integer)
                p.a := x

            var p := Pair(a: 1, b: 2)
            var n := 0
            both(var p.a, var p.b)
            both(var n, var p.a)
            both(var n, var n)
            both(var p.a, var (p.a))
            mix(var p, var p.b)
            var xs := [1, 2]
            both(var xs[0], var xs[1])
            let r := new(Pair(a: 1, b: 2))
            let s := new(Pair(a: 3, b: 4))
            both(var r.a, var s.a)
            both(var r.a, var s.b)
            mix(var deref(r), var s.a)
            both(var r.a, var p.a)
            """), [
                ("E0523", 16, 17), ("E0523", 17, 19), ("E0523", 18, 16),
                ("E0523", 20, 21), ("E0523", 23, 19), ("E0523", 25, 23),
            ])  # fmt: skip

    def test_assign_immutable_and_branches(self):
        self.assertEqual(errs("""
            let a := 1
            a := 2
            let b := if a > 0 then 1 else "x"
            writeln("{b}")
            """), [("E0340", 2, 1), ("E0802", 3, 31)])



def c_errs(body: str) -> list[tuple[str, int, int]]:
    files = {"Main.pst": "program Main\n\nuses IO, C\n\n" + textwrap.dedent(body).lstrip("\n")}
    return [(c, line - 4, col) for _, c, line, col in codes(run(files))]


class CInterop(unittest.TestCase):
    maxDiff = None

    def test_c_compatible(self):
        self.assertEqual(c_errs("""
            extern "libc.so.6"
                type
                    export Good = record
                        p: Ptr[Good]
                        n: CInt
                    export Bad = record
                        s: String
                function f(s: String, g: Good): CInt
                function g(cb: function(String): CInt): Pointer
                function h(fmt: CString, ...): CInt
            _ := h(toCString("%s"), "x")
            _ := h(toCString("%d"), 1, Good(p: cast[Ptr[Good]](0), n: 1))
            """), [
                ("E0901", 7, 16), ("E0901", 8, 19), ("E0901", 9, 20),
                ("E0901", 11, 25), ("E0902", 12, 36),
            ])  # fmt: skip

    def test_unsafe_operations(self):
        self.assertEqual(c_errs("""
            procedure f(p: Ptr[CInt])
                var x: CInt := 1
                writeln(deref(p))
                writeln(deref(p + 1))
                let q := addr(var x)
                let n := cast[Integer](p)
                writeln("{n} {deref(q)}")

            unsafe procedure g(p: Ptr[CInt], s: String)
                var x: CInt := 1
                let q := addr(var x)
                let n := cast[Integer](p)
                let t := cast[String](p)
                let u := cast[Ptr[CInt]](n, s)
                writeln("{n} {deref(q)} {deref(p + 1)} {t} {deref(u)}")
                var xs: List[CInt] := [1]
                let v := addr(var xs[0])
                writeln(deref(v))
            """), [
                ("E0902", 3, 13), ("E0902", 4, 13), ("E0902", 4, 21), ("E0902", 5, 14),
                ("E0902", 6, 14), ("E0902", 7, 19), ("E0904", 13, 14), ("E0511", 14, 14),
                ("E0905", 17, 23),
            ])  # fmt: skip


if __name__ == "__main__":
    unittest.main()

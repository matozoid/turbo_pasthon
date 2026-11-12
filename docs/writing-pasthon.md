# Writing Pasthon: common mistakes

A checklist for people (and AI assistants) who know other languages and are new to Turbo
Pasthon. Pasthon is Pascal with indentation instead of `begin`/`end`; when unsure, write it
the way Pascal would, not the way Python, C or JavaScript would. SPEC.md is the full
reference; `pst query explain CODE` explains an error code.

## Files and units

- A file starts with `program Name` or `unit Name`, and `Name` is the file name without
  `.pst` (`E0105`).
- `writeln`, `write` and `readln` come from `IO`: write `uses IO`. Text helpers (`split`,
  `join`, `trim`, `pos`, `parseInt`) come from `Strings`; `min`, `max` and `abs` from `Math`;
  `args()` and files from `OS`. Only `System` is used implicitly.
- `args()` holds the arguments without the program's name: the first is `args()[0]`.
- Declarations go in indented `type` and `const` blocks, one record field per line:

  ```pascal
  type
      Color = (Red, Green, Blue)
      Point = record
          x: Integer
          y: Integer
      Shape = Point | Circle
  ```

- A program's top-level statements are its main code; there is no `main` routine.
- A unit from a git repository: `uses Yaml from "github.com/someone/pasthon-yaml@v1.0"`,
  always with a tag or commit after `@`. Its constants unit (the `Deps` of `uses Deps,
  Yaml from Deps.Yaml`) needs a name of its own: unit names are global in a program.

## Statements

- `if c then`, `while c do`, `for x in xs do`, `for i := 1 to n do` (`downto` to count
  down). Not `elif`: write `else if`. An empty body is `pass`.
- `:=` assigns, `=` compares, `<>` is "not equal". `x = 1` on its own line is `E0047`.
- No `+=` or `++`: `inc(var x)`, `inc(var x, n)`, `dec(var x)`. They work on any ordinal
  type, so `inc(var color)` moves an enum to its next member.
- Comments are `{ ... }` only. `#` and `//` are errors.
- `let` can't be assigned again; `var` can. `var x := 0` infers the type.
- An unused `let`, `var` or loop variable is an error (`E0206`): name it `_`, as in
  `for _ in xs do`. A counting loop is the exception: it always names its variable,
  used or not (`for i := 1 to 3 do`). An ignored function result is an error too (`E0502`):
  `_ := f()`.
- A `function` has a result type and returns on every path (`E0501`); a routine without a
  result is a `procedure`. `if` and `case` used as values need an `else` (`E0602`).
- `case x of` takes labels like `Red:`, `1..3:`, `'a'..'z':`, `"one":`, and union variants
  with a binding: `Some(v):` (then `v.value`), `Err(e):` (then `e.error`).

## Calls

- A parameter with the same type as an earlier, separately declared one must be passed by
  name (`E0530`): `copyFile("a", dst: "b")`, `join(parts, sep: ", ")`.
- `var` parameters are marked at the call too: `add(var xs, 3)`, `sort(var xs)`.
- Routines can be called with a dot: `xs.length()` is `length(xs)`.

## Values

- Strings don't join with `+`. Use interpolation: `"{name}: {count}"` (write `\{` for a
  literal brace). `length` doesn't take a String: use `charCount` (characters) or `byteLen`.
  To search, use `pos(s, sub: "x")` from `Strings`, not `in`.
- Enum members are qualified, `Color.Red`, except as `case` labels.
- Records are built with named fields: `Point(x: 1, y: 2)`.
- `=` on records compares every field. When that's wrong (a cache field, a circular buffer),
  give the record its own `equals(a, b: R): Boolean` in its unit, and `compare(a, b: R):
  Integer` for `<`. There is no other operator overloading.
- Conversions: `Integer(x)` and the like between integer types, `toInt(r)` or `round(r)` from
  `Real` to `Integer`, `toReal(i)` from `Integer`, `Char(i)` from a code point, `ord(c)` to
  one. There's no `Color(1)`.
- An `Option` or `Result` isn't its value. Unwrap it with `try f()` (in a routine that returns
  an `Option` or `Result`), `getOr(r, default)`, `case`, or `.get()` (which panics on
  `None`/`Err`).

## Collections

- `List[T]` is `[1, 2]`, `Map[K, V]` is `["a": 1]` (empty: `[:]`), `Set[T]` is `[1, 2]`
  (empty: `[]`). A Set's order is unspecified.
- Change them through `var`: `add(var xs, x)`, `remove(var m, key)`, `xs[0] := 5`.
- `x in c` works on a `List`, `Set`, `BitSet` or `Map` (its keys), not on an `Array` or a
  `String`.
- `BitSet[T]` is the Pascal set: an enum or other ordinal type with at most 256 values, and
  the only set that takes ranges (`[Color.Red..Color.Blue]`).
- Several bounds on one type parameter are joined with `+`: `[T: Hashable + Ordered]`.
- `+`, `*` and `-` on two sets are union, intersection and difference. `<=` and `>=` test
  subset and superset.

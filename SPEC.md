# Turbo Pasthon — Language Specification

Version: 0.1 (draft) · 2026-11-07

Turbo Pasthon ("Pasthon") is a statically typed, compiled language with Pascal's
vocabulary and Python's indentation-based block structure. It is designed to be
easy for AI tools (and humans) to read, write, and verify.

- File extension: `.pst`
- Compiler/toolchain: `pst`
- Target: Linux x86-64, ELF executables, emitted directly (no assembler, no external linker)

---

## 1. Design principles

The main users are AI coding models; the language should be readable for humans.

1. **One obvious way.** Each construct has a single syntax. The formatter is canonical and unconfigurable.
2. **Nothing hidden.** No implicit conversions (one exception: injection into a union, §4.7), no exceptions, no null, no operator overloading, no macros. Errors are values.
3. **Contracts visible.** Function signatures are always fully typed. Mutation is marked with `var`.
4. **No surprises at runtime.** Overflow, bounds and contract failures panic in checked builds (`--unchecked` wraps overflow and drops contracts). No undefined behaviour outside `unsafe`.
5. **Machine-checkable.** Built-in contracts (`requires`/`ensures`), built-in tests, JSON diagnostics with stable error codes, and `pst query`, LSP and MCP over one front end (§15).
6. **Good error messages.** A diagnostic says what is wrong and how to fix it, and offers a fix where it can.
7. **Familiar syntax.** Pascal's vocabulary with Python's significant indentation.
8. **Memory safety.** No null, no manual free: automatic reference counting and value semantics (§11). Raw pointers only inside `unsafe`.
9. **Good C interop.** Pasthon calls C libraries and C calls back into Pasthon. C records have exact C layout and are declared in external blocks. Strings go to C without copying, but explicitly (`toCString`). Bindings are written by hand; headers aren't imported (§10).
10. **Turbo.** The compiler is fast and simple, inspired by Turbo Pascal: few passes, no forward declarations, parse, check, emit machine code. It is written in Turbo Pasthon itself.
11. **Platforms.** Linux x86-64 first, other modern hosted platforms later. Relying on libc is fine.

Non-goals:

- Fast generated code. There are no optimization passes; code won't be the fastest, but not slow.
- C calling Pasthon libraries, and static linking of C libraries.

---

## 2. Lexical structure

### 2.1 Source text
- UTF-8. Identifiers are ASCII; strings and comments may contain any Unicode.
- Language is **case-sensitive**.

### 2.2 Indentation
- Blocks are delimited by indentation, exactly **4 spaces** per level.
- Tab characters anywhere outside string literals are an error.
- An indentation change not a multiple of 4, or an indent of more than one level, is an error.
- The lexer emits `NEWLINE`, `INDENT`, `DEDENT` tokens (as in Python).
- Blank lines and comment-only lines do not affect indentation. End markers (§2.4) are the
  exception: a marker line counts as a line at its own indentation, so it closes the block
  it follows.
- **Line continuation:** only inside open `(` and `[`. Inside brackets, newlines and indentation are ignored. There is no backslash continuation.

### 2.3 Comments
- `{ ... }`, which may span lines and nest: `{ a { b } c }` is one comment. An unclosed
  `{` is error `E0011`.
- There is no other comment style: `#` is `E0006`, as is any character outside the
  language.
- Doc comments: a `{ }` comment directly above a declaration; attached to it
  by tooling. The text of a `{ }` doc comment drops the braces, and each line after the
  first drops up to two leading spaces, so lines can be aligned under the first:

```pascal
{ Adds one.
  Never fails. }
function inc(x: Integer): Integer
```

### 2.4 Block end markers (optional, verified)
A comment of the exact form

```
{ end <keyword> [<name>] }
```

placed on its own line, at the indentation of a block header, immediately after
that block's last line, is an **end marker**. The compiler verifies that it
matches the block just closed:

| Block | Marker |
|---|---|
| `if` / `else if` / `else` chain | `{ end if }` |
| `while` | `{ end while }` |
| `for` | `{ end for }` |
| `repeat ... until` | not allowed (`until` already closes it) |
| `case` | `{ end case }` |
| `function f` / `procedure p` | `{ end function f }` / `{ end procedure p }` (name optional) |
| `record` / `trait` type `T` | `{ end record T }` / `{ end trait T }` (name optional) |
| `test "..."` | `{ end test }` |
| `defer` block | `{ end defer }` |
| `external` block | `{ end external }` |

A mismatch is error `E0010`. Markers are never required.

### 2.5 Identifiers and naming
`[A-Za-z][A-Za-z0-9_]*`. An identifier starts with a letter.

Underscores inside names are allowed but not recommended: idiomatic Pasthon uses camelCase,
and examples, the standard units and generated code never use `_` in names. They exist so
that names from other systems can be kept unchanged. A C symbol that is not a valid
Pasthon name (`_exit`, `SDL_Init`) is imported under a new name with `name` (§10.1).

A word starting with `_` is never an identifier. It is a **discard** (§7.9): `_` alone, or
`_name` to document what is being dropped. Discards can't be read.

Casing is enforced (error `E0020`), decided by the first letter:

| Kind | Style | Example |
|---|---|---|
| Types, traits, enum members, constants, units | UpperCamel | `Point`, `MaxQty` |
| Functions, procedures, variables, parameters, fields | lowerCamel | `restock`, `itemCount` |
| Generic type parameters | UpperCamel (usually one letter) | `T`, `K`, `V` |

### 2.6 Keywords

```
and       assert    break     case      const     continue  defer
div       do        downto    else      ensures   export    external
false     for       function  if        in        let
mod       not       of        or        pass      procedure program
record    ref       repeat    requires  return    shl       shr
test      then      this      This      to        trait     true
try       type      unit      unsafe    until     uses      var
while     xor       deref     _
```

### 2.7 Literals

| Kind | Examples | Notes |
|---|---|---|
| Integer | `42`, `1_000_000`, `$FF` (hexadecimal), `%1010` (binary) | `_` as digit separator only; no octal. `0xFF` and `0b1010` are errors (`E0007`) with the `$` or `%` form as a hint |
| Real | `3.14`, `1.0e-9`, `2.0` | must contain `.`; `2` is never Real |
| Boolean | `true`, `false` | |
| Char | `'a'`, `'\n'`, `'\u{1F600}'` | one Unicode scalar value |
| String | `"x = {x}, sum = {a + b}\n"` | always interpolates (§2.8); escapes: `\n \t \r \0 \\ \" \' \{ \u{...}` |
| List | `[1, 2, 3]` | |
| Map | `["a": 1, "b": 2]`, `[:]` (empty) | |
| Set | `[Color.Red, Color.Blue]` | a list literal where a `Set` is expected |

`[a, b]` is a list, an array or a set, by the expected type; with no expected type it is a
`List`. Empty `[]` and `[:]` require the expected type to be known from context (error
`E0301` otherwise). Braces are comments (§2.3), not sets or maps.

### 2.8 String interpolation
Every string literal is a template. There is no separate interpolated-string syntax and no
string concatenation operator: building a string has exactly one form.

```pascal
let msg := "{item.name}: {item.qty} left"
let json := "\{\"name\": \"{name}\"}"     { a backslash makes a literal brace }
```

- `{expr}` inserts any expression whose type satisfies `Printable` (§4.10).
- `{expr:width}` pads the text with spaces to `width` characters, as Pascal's
  `write(x:width)`: in front when `width` is positive, after it when negative; longer text
  stays whole. `{expr:width:decimals}` writes a real (`Real` or `Real32`) with exactly
  `decimals` digits after the point (rounded half to even, as C's `%.*f`; no point for 0;
  clamped to 0..100); `{x:0:2}` is just the decimals. `width` and `decimals` are `Integer`
  expressions; the padding is `System.justify(text, width)`. A `:` in a placeholder that
  is not inside brackets starts the format; the formatter writes it without spaces.
  Decimals on anything but a real are an error (`E0422`).
- `\{` is a literal `{`. A `}` with no open placeholder is literal and needs no escape.
- An unterminated `{`, or an empty `{}`, is an error (`E0030`).
- A string with no placeholders is a plain constant, with no runtime cost.
- A string literal, including its placeholders, must fit on one line. Use `\n` for newlines.
- Strings in `external` library names, `name` symbol names and `test` names may not contain placeholders.

### 2.9 Lexical errors

| Code | Meaning |
|---|---|
| `E0000` | source file is not valid UTF-8 |
| `E0001` | tab character outside a string (fix: replace with 4 spaces) |
| `E0002` | indentation not a multiple of 4 |
| `E0003` | indentation increases by more than one level |
| `E0004` | unterminated string literal |
| `E0005` | invalid escape sequence |
| `E0006` | unexpected character (including non-ASCII outside strings and comments) |
| `E0007` | malformed number literal, or integer above 2^64 - 1 |
| `E0008` | invalid char literal |
| `E0009` | unmatched or unclosed bracket |
| `E0011` | unclosed `{` comment |
| `E0030` | unclosed or empty string placeholder (fix: `\{`) |

The lexer reports every error and continues; it always produces a complete token stream.

An unclosed bracket doesn't swallow the rest of the file. Inside open brackets, a line
that is not indented deeper than the line that opened them, and that starts like a new
statement (a statement or declaration keyword, `name :=`, or an end marker), closes them:
`E0009` is reported for each, with a fix inserting the missing closers at the end of the
previous line.

---

## 3. Program structure

### 3.1 Files
Each file is exactly one of:

```pascal
program Name          { produces an executable; may contain top-level statements }
unit Name             { library; declarations only }
```

The name must match the file name (`Name.pst`).

### 3.2 Top-level layout
After the header, in any order (and, in programs, interleaved with statements, §3.3):

- `uses A, B, C`: at most once, directly after the header
- `type` sections
- `const` sections
- `function` / `procedure` declarations
- `external` blocks
- `test` blocks

### 3.3 Program body
In a `program` file, **all top-level statements** together form the program body
(the implicit `main`). They run top to bottom, unindented, like a script. There is no
`begin` keyword and no extra indentation level.

- Statements and declarations may be **freely interleaved**. Declarations are hoisted
  (§3.4), so their position has no effect on meaning, compile time, or the executable.
- Top-level `var`/`let` are **locals of main**, visible to later top-level statements only.
  They are **never visible inside functions**, wherever the function is declared
  (`E0103`, with a suggested fix: pass the value as a parameter, or as a `var` parameter
  to mutate it). There are no mutable global variables.
- `unit` files can't contain top-level statements (`E0101`).
- Exit status is 0 when the body ends, or set with `halt(code)`.

```pascal
program Hello

uses IO

const
    Greeting = "hello"

var count := 0

procedure bump(var n: Integer)
    inc(var n)

bump(var count)
writeln("{Greeting}, count = {count}")
```

### 3.4 Declaration order
All top-level declarations in a file (and in used units) are visible everywhere
in the file. No `forward` declarations are needed. Mutual recursion works.

### 3.5 Exports and imports
- A declaration prefixed with `export` is visible to units/programs that `uses` this unit.
- Exporting a record type exports only the fields marked `export` (§4.4). Exporting a
  trait exports all of its members.
- Enum members are exported with their enum.
- `uses Strings` makes exported names available both qualified (`Strings.split`) and unqualified (`split`).
- If an unqualified name is ambiguous between units, using it is an error (`E0110`); qualify it.
- The `System` unit (core types, `Option`, `Result`, `length`, etc.) is implicitly used.
- Lookup order for an unqualified name: local variables and parameters (innermost first),
  type parameters and the members their bounds require (§8.2), the file's own top-level
  declarations, then the names exported by the used units. Own declarations hide imported
  ones, except routines: overloads with the same name from the own unit and every used
  unit form one overload set (§6.7).
- Two top-level declarations with the same name in one unit are an error (`E0108`), except
  routines (overloads).
- `uses X` finds `X.pst` among the standard units first, then in the directory of the file
  that names it. A missing unit, or one that is a `program`, is `E0102`. Cyclic `uses` is an
  error (`E0107`).
- Using a name that `X` declares but doesn't export is `E0109`.

### 3.6 Standard units
The standard units (§14) are ordinary `.pst` files shipped with the compiler (`std/` in the repository; installed as `units/` next to the `pst` executable).
Primitive types, the built-in generic types (`List`, `Map`, `Set`, `Array`, `Weak`), the
traits `Equatable`, `Ordered`, `Hashable`, `Sendable`, and the C types `CString`, `Ptr`,
`Pointer` are provided by the compiler as names in `System` (`C` for the C types). Routines
the compiler implements are declared like C functions, in `external "pst"` blocks, which are
allowed only in standard units (`E0106`):

```pascal
external "pst"
    { Number of elements. }
    export function length[T](xs: List[T]): Integer
```

`export` on a declaration inside an `external` block exports it, as for routines.

---

## 4. Types

All type declarations live in `type` sections and have the form `Name = <type-expr>`.

### 4.1 Primitive types

| Type | Meaning |
|---|---|
| `Integer` | signed 64-bit (alias of `Int64`) |
| `Int8`, `Int16`, `Int32`, `Int64` | signed integers |
| `UInt8` (`Byte`), `UInt16`, `UInt32`, `UInt64` | unsigned integers |
| `Real` | IEEE-754 64-bit (alias of `Real64`) |
| `Real32` | IEEE-754 32-bit |
| `Boolean` | `true` / `false`, 1 byte |
| `Char` | Unicode scalar value, 32-bit |
| `String` | immutable UTF-8 text, refcounted; iterated, not indexed (§4.5.1) |

An integer literal's type is `Integer` unless the expected type is another integer type
and the value fits.

### 4.2 Aliases and subranges

```pascal
type
    Meters = Real                 { alias: fully interchangeable with Real }
    Percent = 0..100              { subrange of Integer }
    Digit = '0'..'9'              { subrange of Char }
```

Subrange assignments are range-checked at runtime in checked builds (panic `P0003`).

### 4.3 Enums

```pascal
type
    Color = (Red, Green, Blue)
```

- Members are always written qualified (`Color.Red`), **except** as `case` labels where the type is known (`Red:`).
- Enums are ordinal: `ord(Color.Green) = 1`, comparable with `<`, usable in `for` and as `Set` elements.
- Enums are `Printable`: a member prints as its name.

### 4.4 Records

```pascal
type
    Point = record
        x: Real
        y: Real
        label: String := ""      { field default }
```

- **Value type.** Assignment copies.
- Construction names every field without a default: `Point(x: 1.0, y: 2.0)`.
- A record with exactly one field may be constructed positionally: `Some(5)`.
- A record with zero fields is used as a value by its bare name: `None`.
- Records are stored like strings (§11), so C can't use them. Records declared in an
  `external` block are C structs instead (§10.5).
- Records cannot contain functions. Behavior is provided by free functions (§6.6).

**Field visibility.** Fields are private to the declaring unit unless marked `export`:

```pascal
type
    export SortedList = record
        export count: Integer        { readable and writable from other units }
        items: List[Integer]         { only this unit's functions can touch it }
```

- Outside the unit, private fields can't be read, written, or named.
- A record with any private field can only be **constructed** inside its unit. Other
  units go through an exported function (e.g. `newSortedList()`), so the unit can protect
  invariants.
- `=` still compares all fields, private ones included.

**Resource records.** A record whose unit declares `procedure destroy(var r: R)` for it is a
*resource record*. It exists only behind `ref` (§11.1).

### 4.5 Arrays, lists, maps, sets

| Type | Semantics |
|---|---|
| `Array[T, N]` | fixed length `N` (compile-time constant), inline value, C-compatible |
| `List[T]` | growable, 0-based |
| `Map[K, V]` | hash map in insertion order; `K` must satisfy `Hashable` |
| `Set[T]` | `T` must be an ordinal type with at most 256 values (`E0700`); stored as a bitset |

All indexing is 0-based and bounds-checked in checked builds (panic `P0001`).

#### 4.5.1 Strings are iterated, not indexed
Indexing syntax promises O(1) access, which UTF-8 text can't provide, so `s[i]` doesn't
exist on `String` (`E0310`, with a fix suggesting `chars`, `bytes`, or `slice`).

- `for c in s do` / `chars(s)` iterate over `Char`s (§4.14).
- `charCount(s)` is the number of `Char`s, computed once when the string is created (O(1)).
  `byteLen(s)` is the UTF-8 size. There is no `length(s)` for strings. `length` is only
  for collections with O(1) indexing.
- `bytes(s)` returns a copy of the UTF-8 bytes as a `List[Byte]`, with real O(1) indexing
  for parsers.
- **Positions are byte offsets.** `pos(s, sub, start := 0): Option[Integer]` returns one, and
  `slice(s, from, to)` takes them. An offset that isn't on a `Char` boundary panics (`P0006`).

### 4.6 Value semantics everywhere (except `ref`)
`String`, records, unions, `List`, `Map` and `Set` behave as **values**: assignment and parameter
passing produce logically independent copies. They are implemented as refcounted buffers
with **copy-on-write**, so copies are O(1) until mutated.

A `List` is one block holding its length, its capacity and its elements; the compiler
provides only that block, indexing, `length` and an internal `resize`. `add`, `insert`,
`remove`, `removeLast`, `concat`, `sort` and `in` are ordinary Pasthon routines in System,
built on those. Growing at least doubles the capacity, so `add` is amortized O(1).

- `insert(var xs, index, x)` puts `x` at `index` (`0 <= index <= length(xs)`, else `P0001`),
  moving the elements from there up.
- `sort(var xs)` sorts a list of an `Ordered` type ascending; `sort(var xs, less: f)` sorts
  by a function `f(a, b): Boolean` that says whether `a` goes before `b`;
  `sort(var xs, key: f)` sorts by an `Ordered` key `f(x)`, computed once per element. All
  are stable merge sorts, O(n log n).

A `Map` is a record in System holding lists of keys, values and hashes, in insertion
order, plus an open-addressing slot table that points into them. The compiler provides
only the `[k: v]` and `[:]` literals, `m[k]`, `k in m`, and an internal `hash`; everything
else is Pasthon in System. `Map(...)` can't be written outside System (`E0306`).

- `m[k]` reads a value and panics `P0008` if the key is missing; `m[k] := v` inserts or
  overwrites; `m[k]` is also a `var` argument (`inc(var m[k])`), which panics `P0008`
  for a missing key.
- `k in m`, `length(m)`, `remove(var m, k)` (no-op for a missing key), `get(m, k): Option[V]`,
  `getOr(m, k, default)`. All are O(1) on average.
- Iterating gives keys (`for k in m`) or entries (`for k, v in m`) in insertion order;
  overwriting keeps a key's place, removing and re-adding moves it to the end.
- `=` compares contents regardless of order.
- `remove` leaves a tombstone; the table is rebuilt (and compacted) when it is 3/4 full.

A `Set` is a record in System holding 256 bits, one per possible element: Boolean, Byte,
Int8, an enum, or a subrange with at most 256 values (`Set[Char]` and `Set[Integer]` are
`E0700`; use a subrange like `'a'..'z'`, or a `Map`). Element `x` is bit `ord(x)` minus
the `ord` of the type's first value. The compiler provides the set literals, `x in s`, the
operators below, and the element-to-bit mapping; the rest is Pasthon in System.

- A literal lists elements and ranges, as in Pascal: `[]`, `[Color.Red, Color.Blue]`,
  `['0'..'9', '_']`; `[5..3]` is empty. Where a `Set[Color]` is expected, a bare name that
  isn't otherwise visible is a member of `Color`: `[Red, Green..Blue]`. A literal with a
  range is a set even with nothing expected: `d in ['0'..'4']` is `Set[Digit]` for a `d:
  Digit`; elsewhere, `a..b` is `E0305`.
- `a + b` (union), `a - b` (in `a`, not in `b`), `a * b` (intersection), `a <= b` (`a` is a
  subset of `b`), `a >= b` (superset), `=`, `<>`; both sides of one set type. `<`, `>`,
  `/`, `div` and `mod` on sets are `E0305`.
- `add(var s, x)`, `remove(var s, x)` (no-op if absent), `length(s)`, `x in s`.
- `union(a, b)`, `intersection(a, b)`, `difference(a, b)`: the operators as functions.
- Iterating gives the elements in `ord` order. `Set(...)` can't be written outside System
  (`E0306`).

The only aliasing in the language comes from `ref T` (§4.9).

### 4.7 Unions (tagged)

```pascal
type
    Circle = record
        r: Real
    Rect = record
        w: Real
        h: Real
    Shape = Circle | Rect
```

- Each variant is an existing type. Variant types in one union must be distinct.
- A value of variant type converts implicitly to the union (`var s: Shape := Circle(r: 1.0)`).
  This is the only implicit conversion in the language.
- The conversion reaches into list literals wherever a `List[Shape]` is expected, including
  arguments: `total([Circle(r: 1.0), Rect(w: 1.0, h: 2.0)])`.
- The only way to get a variant out is `case ... of` (§7.6), which must be exhaustive.

### 4.8 Option and Result
Defined in `System` as ordinary unions:

```pascal
type
    Some[T] = record
        value: T
    None = record
    Option[T] = Some[T] | None

    Ok[T] = record
        value: T
    Err[E] = record
        error: E
    Result[T, E] = Ok[T] | Err[E]
```

Helper functions in `System`: `isSome`, `isNone`, `isOk`, `isErr`, `get` (panics on `None`/`Err`), `getOr(default)`.

### 4.9 References

```pascal
type
    Node = record
        value: Integer
        next: Option[ref Node]
```

- `ref T` is a non-null, refcounted pointer to a heap-allocated `T`.
- Create with `new(value)`: `let n := new(Node(value: 1, next: None))`.
- Field access auto-dereferences: `n.value`.
- Whole-value access uses the built-in `deref`: `deref(n)` reads (copies the pointee out),
  `deref(n) := other` replaces the pointee. `deref` is a keyword, and the only call form
  allowed on the left of `:=`.
- `=` on two refs compares **identity**. Compare contents with `deref(a) = deref(b)`.
- A `let` binding of a ref can't be rebound, but the pointee can still be mutated.
- `Weak[T]` holds a non-owning reference: `weak(r)` creates it, `w.get(): Option[ref T]`.
  Use it to break cycles; strong reference cycles leak. Once the last strong reference
  goes away, `get` gives `None` (the pointee is freed even while weak references remain).
  `=` on two weak references is true if they refer to the same pointee, or both to none.

### 4.10 Traits

Traits are **compile-time constraints only**. They have no runtime representation
and cannot be used as the type of a value, field, or list element (error `E0420`).
Use a union for runtime polymorphism.

```pascal
type
    Measurable = trait
        function area(this): Real

    Scalable = trait
        procedure scale(var this, k: Real)

    Named = trait
        name: String

    Mergeable = trait
        function merged(this, other: This): This
```

- `this` stands for the first parameter of the required function. `var this` means the implementation must take its first parameter as `var`.
- `This` is the type being checked.
- A trait can require **fields** and **functions**.
- **Structural satisfaction:** type `X` satisfies trait `Tr` if `X` has every required field
  (same name and type) and, for every required function, a visible function with that name
  exists whose first parameter is `X` (or `var X`) and whose remaining signature matches
  after substituting `This := X`. No `implements` declaration exists.
- Combined bounds: `[T: Measurable + Named]`.
- **Generic traits** take type parameters: `Iterator[T]`. In a bound they are applied like
  types: `[T, I: Iterator[T]]`. A type argument used only in a bound (here `T`) is inferred
  from the signatures of the satisfying type (here, from `next`'s return type).

Built-in traits in `System`: `Iterator[T]` (§4.14), `Sendable` (§11.2), `Equatable` (`=`), `Ordered` (`<` etc.), `Hashable`,
`Printable` (`toString(this): String`).

`Equatable`, `Ordered`, `Hashable` and `Sendable` are decided by the compiler from the
type's structure (e.g. a record is `Equatable` if all its fields are). `Printable` is
satisfied built-in by primitives (except C types), enums, and subranges, and structurally
(a visible `toString`) by everything else.
System declares `toString` for `List`, `Map` and `Set` with Printable items, so they print
as `[1, 2]`, `[a: 1, b: 2]` (insertion order; `[:]` when empty) and `[Red, Blue]` (`ord`
order); items show by their own `toString`, so strings appear without quotes.
A `Real` prints with the fewest significant digits that read back as the same value,
always as a real literal would be written: `2.0`, `0.1`, `1500.0`, in full below 10^16
and with an exponent from there or below 0.0001 (`1.0e+16`, `1.5e-07`); and `inf`, `-inf`
or `nan`. A `Real32` prints the same way with up to 9 digits, in full below 10^7
(`0.1`, `9999999.0`, `1.0e+07`).

### 4.11 Function types

```pascal
type
    BinOp = function(Integer, Integer): Integer
    Callback = procedure(String)
```

A value of function type is a named top-level routine or a lambda. A generic function must
be instantiated to be used as a value: `apply(max[Integer])`.

A **lambda** writes a function value inline: its parameters, `=>`, and one expression.

```pascal
sort(var people, key: p => p.name)
let found := find(iter(people), p => p.name = wanted)
let add: function(Integer, Integer): Integer := (a, b) => a + b
let tick: function(): Integer := () => count + 1
```

Lambdas are restricted so that the common, safe cases work and the classic bugs can't happen:

- The body is a single expression, never a block. For a procedure type, it's a call with
  no value. A lambda that is the whole right side of `:=` / `var` / `let` or `return` may
  have a `case` expression as its body:
  ```pascal
  let area: function(Shape): Real := s => case s of
      Circle(c): pi * c.r * c.r
      Rect(r): r.w * r.h
  ```
- Parameter types come from the expected function type, so a lambda can only appear where
  one is expected (`E0806`), with as many parameters (`E0807`). When that type's result
  is a type parameter still to infer, as `K` of `sort(var xs, key: f)`, the type of the
  body gives it.
- Captures are **by value**: the lambda keeps a copy of each variable it uses, taken when it
  is created. There is no shared loop-variable bug, and captured variables can't be changed
  through it (`E0341`). A lambda that captures nothing is as cheap as a named routine.
- A lambda is `Sendable` if all its captures are.

### 4.12 Generic types

```pascal
type
    Stack[T] = record
        items: List[T]

    Matrix[T, Rows: Integer, Cols: Integer] = record
        cells: Array[T, Rows * Cols]
```

See §8 for instantiation rules.

### 4.13 C types (interop only)
Available from the `C` unit:

| Pasthon | C |
|---|---|
| `CInt` = `Int32`, `CUInt` = `UInt32` | `int`, `unsigned` |
| `CLong` = `Int64`, `CSize` = `UInt64` | `long`, `size_t` |
| `CDouble` = `Real`, `CFloat` = `Real32` | `double`, `float` |
| `CChar` = `Int8` | `char` |
| `CString` | `const char*`, NUL-terminated |
| `Ptr[T]` | `T*`, unmanaged, nullable |
| `Pointer` | `void*` |

`Ptr`/`Pointer` are unmanaged and can be dereferenced (`deref(p)`, same form as for `ref`) only inside functions marked `unsafe` (§10.4).
`isNull(p)` tests a `Ptr[T]`, `Pointer` or `CString` for null, outside `unsafe` too (§10.2).
`addr(var x): Ptr[T]` gives the address of a variable, or of a field of a C record in one
(§10.5). It is allowed only inside `unsafe` functions, and only for C-compatible types
(§10.1). The pointer is valid while the variable is in scope and not reassigned (assigning
a C record variable may move its bytes).

Sized integers (`Int8` … `UInt64`) are checked like `Integer`: arithmetic that leaves the
type's range panics (`P0002`) in checked builds and wraps in `--unchecked` ones. Conversions
between integer types (`Int32(x)`) are checked the same way; `cast[T](x)` truncates without
checking.

### 4.14 Iterators
```pascal
type
    Iterator[T] = trait                       { in System }
        function next(var this): Option[T]
```

- An iterator is an ordinary record holding its state, plus a `next` function. Satisfaction
  is structural, as for every trait.
- A type is **iterable** if a visible function `iter(c: C): I` exists where `I` satisfies
  `Iterator[T]`. Every iterator is iterable as itself.
- `for x in e do <body>` means:
  ```pascal
  var it := iter(e)          { or a copy of e, if e is already an Iterator }
  while true do
      case next(var it) of
          Some(s):
              let x := s.value   { x is a fresh let binding }
              <body>
          None:    break
  ```
  Each loop is instantiated for its concrete iterator type, so `next` is a direct call.
- `for _ in e do` iterates without binding.
- `List`, `Array`, `String` (chars) and enum types (`for c in Color do`, all members in
  order) are iterated directly by the compiler, without `iter`. `Map` and `Set` have an
  `iter` in System: a map's yields its keys, a set's its elements.
- `for k, v in m do` works on `Map`: it iterates `entries(m)`, which yields
  `Entry[K, V]` (fields `key`, `value`) and binds the two fields.
- **Snapshot semantics.** An iterator holds its own copy of the collection. With
  copy-on-write (§4.6) this is O(1). Mutating the collection inside the loop copies the
  buffer, and the loop keeps iterating the original contents. Iterators never dangle and
  never observe concurrent modification.
- The same holds for an iterator: `for x in it` advances a copy, and `it` is unchanged
  after the loop. An adapter that consumes the iterator it wraps calls
  `next(var it.source)` instead of looping over it.
- Generic code accepts iterators, not collections: `count(iter(xs))`.

```pascal
type
    CharIter = record
        text: String        { refcounted copy, O(1) }
        pos: Integer        { byte offset }

function iter(s: String): CharIter
    return CharIter(text: s, pos: 0)

function next(var it: CharIter): Option[Char]
    if it.pos >= byteLen(it.text) then
        return None
    let c := decodeUtf8At(it.text, it.pos)
    it.pos := it.pos + utf8Width(c)
    return Some(c)

function count[T, I: Iterator[T]](it: I): Integer
    var n := 0
    for _ in it do
        inc(var n)
    return n
```

**Built-in iterables:** `List`, `Array`, `Set`, `Map` (keys), `String` (Chars), enum types
(all members in order). Integer ranges use `for i := a to b do` (§7.5), the only way to
loop over integers.

**Adapters in `System`.** Each one is a generic iterator record plus `next`, so loops over
them compile to direct calls with no allocation:

| Adapter | Yields |
|---|---|
| `take(it, n)` / `skip(it, n)` | first `n` / all but first `n` |
| `zip(a, b)` | `Pair[A, B]` (fields `first`, `second`) until either ends |
| `enumerate(it)` | `Indexed[T]` (fields `index`, `value`) |
| `chars(s)` | `Char`s of a string |

**Consumers:** `collect(it): List[T]`, `count`, `sum`, `any(it, pred)`, `all(it, pred)`,
`find(it, pred): Option[T]`.

There is no `map` or `filter`: a `for` expression (§9.4) is the one way to map and filter,
`for x in xs do if x > 0 then double(x) else continue`. A `pred` is a named function
or a lambda (§4.11): `any(iter(xs), x => x < 0)`.

---

## 5. Declarations

### 5.1 Constants
```pascal
const
    MaxQty = 1000
    Pi2 = 2.0 * Pi
    Origin = Point(x: 0.0, y: 0.0)
```
The value must be computable at compile time. The type is inferred.
`const` sections are only allowed at top level, never inside functions (`E0202`). Use `let` there.

### 5.2 Local variables
```pascal
var count := 0               { mutable, type inferred }
var total: Real := 0.0       { mutable, explicit type }
let name := "bob"            { immutable binding }
```
- Every variable **must** be initialized at declaration (`E0201`).
- `let` bindings can't be reassigned or passed as `var` arguments.
- Shadowing a variable from an enclosing scope in the same function is an error (`E0205`).
- Declaring a variable and never using it is an error (`E0206`). Remove it, or discard the
  value (fix: `var x := f()` becomes `_ := f()`). Assigning to a variable doesn't use it;
  reading it, or passing it as `var`, does. This covers `for ... in` variables and `case`
  bindings (fix: `_`), but not the counter of `for i := a to b`.

---

## 6. Functions and procedures

### 6.1 Syntax
```pascal
function name[TypeParams](params): ReturnType
    requires <bool-expr>          { zero or more, first lines of body }
    ensures <bool-expr>           { zero or more, after requires }
    <statements>

procedure name[TypeParams](params)
    <statements>
```

- `function` must return a value on every path (`E0501`). `procedure` returns nothing.
- Calling a function and ignoring its result is an error (`E0502`); assign it to `_` (§7.9).

### 6.2 Parameters
- `x: T`: by value, immutable inside the body.
- `var x: T`: by reference, mutable; the caller sees changes.
- Default values: `sep: String := ", "`. Only trailing parameters may have defaults.
- **Parameter groups** (Pascal style): `a, b: T` declares several parameters with one type.
  Grouping declares them *interchangeable in role*, which affects named arguments (§6.4):
  `function max[T: Ordered](a, b: T): T`, `procedure swap[T](var a, b: T)`.

### 6.3 Call-site `var`
An argument passed to a `var` parameter must be marked `var` at the call site:

```pascal
swap(var a, var b)
```

There are no exceptions to this rule. Because dot syntax can't carry a `var` marker,
functions whose first parameter is `var` can't be called with dot syntax (§6.6).
Every call that may mutate an argument shows `var` at that argument.

- Arguments are evaluated in two rounds: first the value arguments, left to right; then the
  `var` places, left to right. So in `add(var p.items, length(p.items))` the length is read
  before `p.items` is bound.
- A `var` argument that is a whole variable is passed by reference. Any other place (a
  field `p.x`, an element `xs[i]`) is copied in before the call and copied back after it.
  Where no program can tell the difference, the compiler *moves* the value out instead
  of copying it. That is unless another `var` place of the call uses its variable, or
  the place is in a box (`r.items` with `r: ref T`) that the callee may reach through
  an argument: one whose type can hold a `ref` or `Weak` to that box's type, or a function
  value, since a lambda may have captured one. Without mutable globals, arguments are the
  only way in. So `add(var p.items, x)` and
  `add(var p.items, Item(at: length(p.items)))` are O(1), not a copy of the list.
- A `var` argument can't contain a `try` (`E0524`): returning from inside the call would
  leave the places already bound emptied. Put the value in a variable first:
  `let k := try find(xs)` then `inc(var counts[k])`.
- The `var` arguments of one call must be separate places: not the same variable, field
  or element, and not one inside the other (`E0523`): `swap(var p, var p.x)` is an error.
  The check is at compile time, so it assumes the worst where it can't know:
  - Any two elements of one collection may be the same: `swap(var xs[i], var xs[j])` is an
    error. Pass the collection and the indexes instead: `swap(var xs, i, j)`.
  - Any two refs to a `T` may point to the same `T`: `swap(var deref(a), var deref(b))`
    and `move(var a.pos, var b.pos)` are errors when `a` and `b` are both `ref T`.

### 6.4 Named arguments
- Any argument may be passed by name: `restock(item, amount: 5)`.
- Positional arguments must come before named ones.
- Each parameter is bound exactly once.
- **Required names for same-typed parameters.** If several parameters have the same type
  but are declared **separately** (not in one group), every one of them except the first
  must be passed by name (`E0530`, with a fix inserting the names):

  ```pascal
  procedure copyFile(src: String, dst: String)
  copyFile("a.txt", dst: "b.txt")     { ok }
  copyFile("a.txt", "b.txt")          { E0530: 'dst' has the same type as 'src'; name it }

  function max[T: Ordered](a, b: T): T
  max(x, y)                           { ok: a and b are grouped, so interchangeable }
  ```
- "Same type" is decided on the declared parameter types, before generic substitution.
  The first argument of a dot call (§6.6) counts as positional.
- A named argument to a `var` parameter is written `name: var expr`.

### 6.5 Contracts
- `requires` checks run on entry; `ensures` checks run on every return.
- In `ensures`, `result` is the return value, and `old(expr)` is `expr` evaluated on entry.
- A violation panics with `P0010` / `P0011` in checked builds. Contracts are removed with `--unchecked`.

### 6.6 Uniform function call syntax (UFCS)
`a.f(b, c)` is exactly `f(a, b, c)`. Resolution for `a.f`:
1. If `a` has a field named `f`, it is field access (calling it if `f` has a function type).
2. Otherwise it is a call to function `f` with `a` as the first argument.

Dot syntax is **read-only**: if `f`'s first parameter is `var`, `a.f(...)` is an error
(`E0520`, with a fix rewriting it to `f(var a, ...)`). So `p.length()` never changes `p`,
and mutation is always written `shift(var p, 1.0)`.

Mutating through a `ref` doesn't need `var`: `n.relink(other)` with `relink(n: ref Node, ...)`
is allowed, since the reference itself isn't changed. The type `ref` in the signature shows
that the pointee is shared.

### 6.7 Overloading
Functions may share a name if their parameter types differ. Calls resolve by
**exact** type match only (there are no implicit conversions, so ranking is never needed).
Two candidates that match exactly is an error (`E0510`).

---

## 7. Statements

### 7.1 Assignment
```pascal
x := expr
p.x := expr
list[i] := expr
deref(r) := expr
```
There are no compound operators (`+=`). Use `inc(var x)`, `inc(var x, n)`, `dec(var x)`, `dec(var x, n)` for counters.

### 7.2 if
```pascal
if x > 0 then
    writeln("pos")
else if x < 0 then
    writeln("neg")
else
    writeln("zero")
```
- Bodies always start on a new line.
- `else if` on one line continues the same chain. It does not open a nested `if` block,
  so the chain stays flat and a single `{ end if }` closes it. Writing `else` followed by an
  indented `if` on the next line is a nested `if`, and the formatter rewrites it to `else if`.

### 7.3 while
```pascal
while i < n do
    inc(var i)
```

### 7.4 repeat ... until
```pascal
repeat
    line := readln()
until line = "quit"
```
`until` sits at the same indentation as `repeat` and closes the block. The condition can
use variables declared in the body. So `continue` isn't allowed in a `repeat` body
(`E0506`): it would reach the condition past declarations that didn't run.

### 7.5 for
```pascal
for i := 0 to n - 1 do        { inclusive bounds, step +1 }
    ...
for i := n - 1 downto 0 do    { inclusive bounds, step -1 }
    ...
for item in items do          { any iterable (§4.14) }
    ...
for key, value in map do      { Map only }
    ...
```
- The loop variable is a fresh `let` binding scoped to the body. `for ... in` is defined by
  the iterator protocol (§4.14).
- The bounds of `to`/`downto` are evaluated once.
- Where a value is expected, `for` is an expression that builds a list (§9.4).

### 7.6 case
```pascal
case expr of
    1, 2:
        writeln("low")
    3..9: writeln("mid")
    else: writeln("high")
```
- Labels: constants, ranges, comma lists, enum members (bare), union variants with binding.
- An arm body is a single statement on the same line, or an indented block.
- Labels must not overlap (`E0601`).
- Over an **enum or union**, `case` must be exhaustive or have `else` (`E0602`).
  Over other types, `else` is required.
- Union arms bind the variant value:
  ```pascal
  case shape of
      Circle(c): return Pi * c.r * c.r
      Rect(r):   return r.w * r.h
  ```
  The binding is optional: `None: return 0`. A label that binds needs an arm of its own
  (`E0603`): in `X(x), Y(y):` one of the two would hold nothing.

Expression forms of `if` and `case` are described in §9.3.

### 7.7 break / continue / return / pass
As usual. `break`/`continue` apply to the innermost loop (`continue` not in `repeat`, §7.4).
`pass` does nothing; it fills a block or `case` arm that has nothing to do:
```pascal
case shape of
    Circle(c): draw(c)
    else: pass
```

### 7.8 defer
```pascal
let f := try open(path)
defer close(var f)
```
Runs at scope exit in reverse order, on every path, including `return`, `break`,
`continue` and `try` propagation (not on panic). Takes a single statement or an indented
block. A deferred statement runs at the scope's exit, so it can't leave itself: no
`return` or `try` in it, and no `break` or `continue` of a loop outside it (`E0507`).

### 7.9 Discarding results
`_ := expr` evaluates and drops a value. Required for ignored function results.

Every word starting with `_` is a discard: `_` alone, or `_name` when a name helps the reader.
A discard can't be read (`E0104`), only assigned. Discards appear in exactly three places:

- left of `:=`: `_ := f()`, `_status := close(fd)`
- as a `case` binding: `Circle(_): ...`, `Circle(_radius): ...`
- as a `for` loop variable: `for _ in items do`

### 7.10 assert
`assert cond` or `assert cond, "message"`. Panics with `P0020`. Always active (also in `--unchecked`).

---

## 8. Generics

- Type parameters: `[T]`, bounded `[T: Trait]`, multiple bounds `[T: A + B]`, constants `[N: Integer]`.
- **Instantiation is monomorphization by copying** (C++-template style): each distinct
  combination of type and constant arguments produces a full copy of the function or type,
  with the arguments substituted, compiled like ordinary code. The copy isn't checked
  again: checking the body once against its bounds makes every instance valid.
- **Bounds are checked up front.** The body of a generic is type-checked once against its
  bounds, so it may only use operations the bounds guarantee (`E0710`). Errors therefore
  point at the generic's definition or the call site, never deep inside an instantiation.
- Type arguments are inferred from value arguments where possible. Otherwise write them
  explicitly: `parse[Integer](s)`.
- Where the expected type is known (a declared variable, a field, a `return`), type
  arguments are first inferred from it, so a variant argument is injected into the union:
  `return Ok(Circle(r: 1.0))` in a function returning `Result[Shape, String]` gives an
  `Ok[Shape]`. If that doesn't fit the arguments, inference falls back to the arguments
  alone.
- Constant parameters may be used in type expressions and as values: `Array[T, N * 2]`, `for i := 0 to N - 1 do`.
- Recursive instantiation depth is limited to 64 (`E0720`, reported when the program is
  built): `function f[T](x: T)` calling `f(Box(item: x))` would never end.
- Resource records can't be type arguments (use `ref R`, §11.1). An unbounded `T` supports only copying, assignment, passing to parameters of type `T`,
  and storing in generic containers. Everything else (`=`, `<`, `toString`, calls,
  fields) needs a bound that provides it.

### 8.1 Instances
- An instance is identified by (generic declaration, list of type and constant arguments).
  It is created once per program and shared by every unit that uses it.
- Instances are created on demand from a work queue during the check and emit pass.
  A cache lookup happens before every instantiation, so mutual and self recursion terminate.

### 8.2 Name resolution inside generics
A name used in a generic body is resolved in one of two ways:

1. **Bound members are resolved at the instantiation site.** Fields and functions required
   by the bounds of `T` (e.g. `area` from `T: Measurable`) are looked up where the trait is
   checked, that is where `T` is substituted, with the visibility of that place. This lets a
   generic in unit `Geometry` call `area` for a `Circle` whose `area` lives in the program
   that uses `Geometry`.
2. **Everything else is resolved at the definition site**, once, when the generic body is
   checked against its bounds. That covers other functions, types, constants, and units
   visible in the generic's own unit. Instantiation never changes their meaning.

A name that matches a bound member, and also something visible at the definition site,
always resolves to the bound member. There is no further lookup and no overload ranking
between the two sources.

```pascal
unit Geometry

type
    export Measurable = trait
        function area(this): Real

export function totalArea[T: Measurable](xs: List[T]): Real
    var sum := 0.0
    for x in xs do
        sum := sum + x.area()     { bound member: resolved where totalArea[T] is instantiated }
    return round2(sum)            { not a bound member: resolved here, in Geometry }

function round2(x: Real): Real
    return toReal(round(x * 100.0)) / 100.0
```

```pascal
program Shapes

uses Geometry, Math

type
    Circle = record
        r: Real

function area(c: Circle): Real
    return Pi * c.r * c.r         { visible here, so Circle satisfies Measurable here }

writeln("{totalArea([Circle(r: 1.0), Circle(r: 2.0)])}")
```

`round2` is not exported from `Geometry` and is not visible in `Shapes`, but the
instance `totalArea[Circle]` still uses it, because it was resolved at the definition site.

```pascal
function largest[T: Ordered](xs: List[T]): Option[T]
    if length(xs) = 0 then
        return None
    var best := xs[0]
    for x in xs do
        if x > best then
            best := x
    return Some(best)
```

---

## 9. Expressions

### 9.1 Operator precedence (lowest to highest)

| Level | Operators | Operand types |
|---|---|---|
| 1 | `or` | Boolean, or integers (bitwise) |
| 1a | `xor` | Boolean, or integers (bitwise) |
| 2 | `and` | Boolean, or integers (bitwise) |
| 3 | `not` | Boolean, or an integer (bitwise) |
| 4 | `=` `<>` `<` `<=` `>` `>=` `in` | non-associative: `a < b < c` is an error |
| 5 | `+` `-` | numeric, or sets (§4.6) (strings: §2.8; lists: `add`, `concat`) |
| 6 | `*` `/` `div` `mod` `shl` `shr` | |
| 7 | unary `-`, `try` | |
| 8 | postfix: call `()`, index `[]`, field `.` | |

- `/` works on Real only. `div` and `mod` work on integers only. `div` truncates toward
  zero and `a mod b` takes the sign of `a`, so `a = (a div b) * b + a mod b`:
  `-7 div 2 = -3`, `-7 mod 2 = -1`. `x mod -1` is `0`; `Integer.min div -1` overflows.
- Integer division by zero panics (`P0004`), in `--unchecked` builds too.
- `Real` arithmetic is IEEE-754 double precision, rounding to nearest: `1.0 / 0.0` is
  infinity and `0.0 / 0.0` a NaN, without a panic. A comparison with a NaN is false,
  except `<>`, which is true; `0.0 = -0.0`. Structural `=` compares reals the same way,
  so a record holding a NaN is unequal to itself. A literal is the double nearest its
  decimal value (ties to even); one too large is infinity.
- `Real32` arithmetic is IEEE-754 single precision: each operation is exact in double
  and then rounded to the nearest single, so it gives the single-precision result. A
  `Real32` literal is the single nearest its decimal value. `Real(x)` widens a `Real32`
  exactly and `Real32(x)` rounds a `Real` to the nearest single.
- `toInt(r)` truncates toward zero and `round(r)` rounds half away from zero
  (`round(2.5) = 3`, `round(-2.5) = -3`). A result outside `Integer`, or a NaN, panics
  (`P0002`) in checked builds.
- `and`, `or`, `xor` and `not` are logical on Booleans (`and` and `or` short-circuit) and
  bitwise on integers, as in Pascal: both operands of one integer type, and the result
  wraps to its width (`not b` of a `Byte` is `255 - b`). A comparison binds tighter, so
  write `(x and $FF) = 0`. `x shl n` and `x shr n` shift an integer by an `Integer` count:
  `shr` shifts in zeros, and a count outside 0 to width - 1 gives 0.
- Mixed numeric types are an error. Convert explicitly: `toReal(i)`, `toInt(r)` (truncates), `round(r)`, `Int32(x)` (checked narrowing). `ord(c)` is a `Char`'s code point and
  `Char(i)` the `Char` of code point `i`; it panics (`P0009`) if `i` is negative, above
  U+10FFFF or a surrogate, in `--unchecked` builds too.
- Integer overflow panics in checked builds (`P0002`) and wraps in `--unchecked` builds.
- `=` is structural for records, unions, lists, maps, sets, and strings, and identity for `ref`.

### 9.2 try
`try e`:
- If `e: Result[T, E]`: on `Ok(v)` the value is `v`. On `Err(x)` the enclosing function immediately returns `Err(x)`, so the enclosing function must return `Result[_, E]` with the same `E`.
- If `e: Option[T]`: on `Some(v)` the value is `v`. On `None` the function returns `None`, so it must return an `Option`.

Deferred statements run before the early return.

### 9.3 if and case expressions
`if` and `case` also exist as **expressions**. The form is chosen by position: in
statement position they are statements (§7.2, §7.6) and produce no value; where a value
is expected they are expressions. There is no "last expression of a block is its value" rule.

```pascal
let sign := if x > 0 then 1 else if x < 0 then -1 else 0

let label := case item.status of
    Active:       "on"
    Discontinued: "gone"
    Backorder:    "wait"

return if isOk(r) then "ok" else "failed"
```

Rules:
- Each branch is a **single expression**, never a statement block.
- `if` expressions require `else` (`E0801`). `case` expressions must be exhaustive, or
  over non-enum, non-union types have `else` (`E0602`, as for statements).
- All branches must have the same type (`E0802`). No implicit conversion, except union
  injection when the expected type is a union.
- Union arms bind values as in statements: `Circle(c): Pi * c.r * c.r`.
- Only the chosen branch is evaluated.
- **`if` expression layout:** a single line, or wrapped in parentheses (newlines inside
  brackets are free, §2.2). It has the lowest precedence, so as an operand it must be
  parenthesized: `1 + (if a then 2 else 3)` (`E0803` otherwise).
- **`case` expression layout:** as the statement, with an expression after each arm's
  colon. Because its arms form an indented block, a `case` expression may only appear as
  the whole right-hand side of `:=` / `var` / `let`, or as the whole operand of `return`,
  or as the body of a lambda that is (§4.11; `E0804`).
- **Prefer the expression.** A statement `if`/`case` whose every branch only assigns the
  same variable gets warning `W0801` from `pst check`, with a fix that rewrites it to the
  expression form.

### 9.4 for expressions
`for` is an expression too, like `if` and `case` (§9.3), and by the same rule: the
form is chosen by position. In statement position it is the loop of §7.5, with its body on
the following lines. Where a value is expected, `do` is followed by a single expression,
and the value is a list of its results:

```pascal
let names := for p in people do p.name
let squares := for i := 1 to 10 do i * i
let labels := for key, value in stock do "{key}: {value}"
let adults := for p in people do if p.age >= 18 then p.name else continue
let pairs := (
    for a in xs
    for b in neighbours(a)
    do if a < b then Pair(first: a, second: b) else continue
)
let total := sum(iter(for item in items do item.qty * item.price))
```

- A `for` expression is one or more `for` clauses, then `do e`. A clause is any loop head
  of §7.5 (`x in e`, `k, v in m`, `i := a to b`, `i := a downto b`). Several clauses nest:
  a later clause runs once for each item of the earlier ones and sees their names. Only the
  expression form takes more than one clause.
- The value is a new `List[T]`, where `T` is the type of `e`, in loop order. It is built as
  the statement loops would build it:
  ```pascal
  var r: List[T] := []
  for a in xs do
      for b in neighbours(a) do
          add(var r, e)
  ```
  So loop variables are fresh `let` bindings and sources are snapshots (§4.14).
- **Skipping and stopping.** When `e` is an `if` expression, a branch may be `continue` or
  `break` instead of a value. `continue` adds nothing for this item and goes on with the
  next one. `break` adds nothing and ends the whole `for` expression, whatever the number
  of clauses. At least one branch must be a value, and the branches that are values give
  `T` (`E0805` otherwise; `E0802` if they differ). Nowhere else are `continue` and `break`
  expressions, and an `if` expression still requires `else` (`E0801`).
- No function values are involved. `try` in a clause or in `e` returns from the enclosing
  function, as in a loop: `for s in parts do try parseInt(s)`.
- **Eager.** The loops run to the end before the value exists. An endless iterator needs a
  `break`, or is cut off first:
  ```pascal
  let small := for n in naturals() do if n * n < 1000 then n * n else break
  let first := for n in take(naturals(), 10) do n * n
  ```
  A loop that stops on what it has collected so far ("the first ten primes") is written as
  a statement.
- **Layout and precedence** are those of the `if` expression: a single line, or wrapped in
  parentheses, and parenthesized when it is an operand (`E0803`). `e` extends as far as it
  can, so an `if` expression there needs no parentheses.

---

## 10. C interoperability

### 10.1 external blocks
```pascal
external "libc.so.6"
    function strlen(s: CString): CSize
    function printf(fmt: CString, ...): CInt
    procedure free(p: Pointer)

external "libm.so.6"
    function cos(x: CDouble): CDouble
```
- Every library becomes a `DT_NEEDED` entry in the output ELF. Symbols are bound at load time through the GOT (no PLT, no lazy binding).
- `name "symbol"` binds a declaration to a C symbol with a different name. Without `name`, the
  Pasthon name is the symbol name. Use it for symbols that aren't valid Pasthon names
  (leading `_`, or casing that breaks `E0020`):

  ```pascal
  external "libc.so.6"
      procedure exitNow(code: CInt) name "_exit"
      function errnoLocation(): Ptr[CInt] name "__errno_location"

  external "libSDL2-2.0.so.0"
      function sdlInit(flags: CUInt): CInt name "SDL_Init"
  ```
  The symbol string may not contain placeholders.
- `name` is a keyword only there; elsewhere it's an ordinary identifier.
- `...` (C varargs) is allowed only in external declarations.
- Calls use the System V AMD64 ABI.
- Parameter and return types must be C-compatible: primitive numerics, `Boolean`, C types,
  `Ptr[T]`, `Pointer`, C records (§10.5), `Array[T, N]` (inside C records), and non-generic
  function types (as callbacks).
- Bindings are written by hand (or by an AI). The compiler doesn't read C headers.
- Only Pasthon calling C, and C calling back (§10.3), are supported. pst doesn't build
  libraries for C to call, and doesn't link static C libraries (`.a`, `.o`).

### 10.2 Strings
- A `String` is never converted to a `CString` implicitly: write `strlen(toCString(s))`.
- `toCString(s: String): CString` doesn't copy: it returns a pointer to the string's bytes,
  which always end with a NUL, and keeps the string alive until the end of the enclosing
  block. A NUL inside the string ends it early for C.
- `fromCString(p: CString): String` copies the C string. Bytes that aren't valid UTF-8 each
  become U+FFFD. A null pointer panics (`P0007`), in `--unchecked` builds too: where C may
  return null, check with `isNull` first.
- `isNull(p)` takes a `CString`, `Pointer` or `Ptr[T]`. It is safe (no `unsafe` needed),
  since it doesn't read through the pointer:

  ```pascal
  function env(name: String): Option[String]
      let p := getenv(toCString(name))
      if p.isNull() then
          return None
      return Some(fromCString(p))
  ```
- `fromCBytes(p: Pointer, count: Integer): String` copies `count` bytes, which may include
  NULs, decoding them the same way.
  Planned, not in v0.1: decoding other encodings (Latin-1 and other 8-bit code pages,
  where bytes 0x80–0xFF are characters), as a parameter or a separate function.
- A program ends through libc's `exit`, so output C buffered with `stdio` is written. It is
  buffered separately from `IO`'s: their order is only kept when C flushes (`fflush`).

### 10.3 Callbacks
A top-level, non-generic Pasthon function whose signature is C-compatible can be passed
where C expects a function pointer, by name or as any value of its function type (§4.11).
C may call it any number of times while the program runs. So can a lambda that captures no
variables. A lambda that captures some can't be: C calls it without them. Passed directly
to an external routine it is an error (`E0808`); passed as a value it panics (`P0012`).

### 10.4 unsafe
Dereferencing `Ptr[T]`/`Pointer`, pointer arithmetic (`p + n`, in units of `T`), `cast[T](x)`
and `addr(var x)` are allowed only inside functions declared `unsafe function` /
`unsafe procedure`. Calling an unsafe
function requires no marker. Unsafe code is greppable, not contagious.


### 10.5 C records
A record declared in a `type` section inside an `external` block is a **C record**: it has
exactly the layout of the C struct (fields in declaration order, C alignment rules). A
C record value holds those bytes; like other records, it is a value: assignment copies it
(copy-on-write, §4.6).

```pascal
external "libSDL2-2.0.so.0"
    type
        export SdlRect = record
            x: CInt
            y: CInt
            w: CInt
            h: CInt
    function sdlFillRect(s: Ptr[SdlSurface], r: Ptr[SdlRect], color: CUInt): CInt name "SDL_FillRect"

unsafe procedure fill(s: Ptr[SdlSurface])
    var r := SdlRect(x: 0, y: 0, w: 8, h: 8)
    r.w := 16
    _ := sdlFillRect(s, addr(var r), color: 0)
```

- Fields must be C-compatible (§10.1): no `String`, unions, `ref`, `List`, or Pasthon
  records. Fields have no defaults and are all exported.
- Outside `unsafe`, a C record is used like any record: construction, field access, `=`.
  Assignment copies the bytes.
- Only C records can be passed to C by value or through `Ptr[T]`.
- C calls and callbacks (§10.3) follow System V: integer arguments go in 6 registers and
  reals in 8 xmm ones, the rest on the stack (a `Real32` passed to `...` is promoted to
  `double`, as in C). A C record of up to 16 bytes passed or returned by value goes word
  by word: a word holding only reals in the next xmm register, any other in the next
  integer one, or the whole record on the stack when the registers run out. A bigger one
  goes on the stack, and one returned is written to memory the caller passes.

---

## 11. Memory model

- **Automatic reference counting.** `String`, records, unions, `List`, `Map`, `Set` and `ref T` are refcounted.
- Increments and decrements are inserted by the compiler. Destruction is deterministic: memory is freed as soon as the last owner goes away.
- **No cycle collector.** Strong cycles leak, so use `Weak[T]`. A checked build keeps track
  of the heap blocks it allocates and frees, and of the file and line of the code that
  allocated each. If any are still alive when the program (or a test block) reaches its
  end, it prints `leak: N blocks still alive at exit` to stderr, then a line per type and
  place, most blocks first, and exits with status 70:

  ```
  leak: 8 blocks still alive at exit
    2 × ref Node allocated at Main.pst:9
    2 × Option[ref Node] allocated at Main.pst:11
  ```

  A union value counts as two blocks, the union's and the variant's (`Some[ref Node]`). A
  block made outside any code, by the runtime itself, has no place. `halt`, `OS.exit` and
  panics end the program without this check.
- Primitives, enums, arrays and C records (§10.5) live inline (stack or embedded). Strings, records and unions
  are pointers to refcounted heap blocks, shared on copy and copied before they're changed
  (§4.6). A union value's block records which variant it holds.
- Heap allocation goes through libc `malloc`/`free`. If `malloc` fails, the program prints
  `panic: out of memory` to stderr and exits with status 70.
- Refcounts are **not atomic**. Every heap object belongs to exactly one thread (§11.2).

### 11.1 Destroy hooks and resource records
```pascal
type
    export File = record
        fd: CInt

procedure destroy(var f: File)
    _ := close(f.fd)

export function open(path: String): Result[ref File, String]
    let fd := try sysOpen(path)
    return Ok(new(File(fd: fd)))
```

- A record whose unit declares `procedure destroy(var r: R)` for it is a **resource record**.
  `destroy` must be in the same unit as the record, with exactly this signature (one
  declared in another unit is `E1102`). It is called automatically and can never be
  called explicitly, nor used as a value (`E1101`).
- **Resource records exist only behind `ref`.** A value of type `R` itself can't be stored
  in a variable, parameter, field, list, or return value (`E1103`). Only `ref R` can.
  - The constructor `R(...)` may appear only as the direct argument of `new(...)`.
  - `deref(r)` on a `ref R` is an error (it would copy). Field access `r.fd` works.
  - `R` can't be a generic type argument, but `ref R` can: `List[ref File]`. `Weak[R]` is
    allowed, as its argument is the type of what it refers to, like `ref`'s.
- `ref R` copies freely (refcount +1). Cleanup is deterministic: `destroy` runs exactly once,
  when the last strong reference goes away. Weak references to it already get `None` while
  it runs. Then what the record holds is released and the memory is freed.
- If a record holds `ref R` fields, releasing the record releases them, which may trigger
  their `destroy`.
- There are **no moves** and no move tracking. Ownership is always the refcount.
- Destroy does not run on panic or `halt`. The process exits and the OS reclaims resources.

### 11.2 Concurrency (reserved design, not implemented in v0.1)
v0.1 is single-threaded, but the design is fixed now so v0.1 doesn't rule it out:

- **Threads share nothing.** Each thread has its own heap. Refcounts stay non-atomic and fast.
- There are no mutable globals (§3.3), so no shared state can exist by accident.
- Communication is by **message passing** only, through the `Threads` unit:
  `spawn(worker, arg): Task[T]`, `join(t): T`, `Channel[T]`, `send(var ch, value)`,
  `receive(var ch): Option[T]`.
- Only `Sendable` values cross threads: types containing no `ref`, `Weak`, `Ptr`, or
  `Pointer`. Sending transfers the value (a runtime operation, not compile-time move tracking). Buffers with refcount 1 are handed over in O(1),
  shared buffers are deep-copied, so the receiver always gets an object it owns alone.
- `spawn` takes a named top-level function (or a lambda that only captures `Sendable`
  values).
- v0.1 implementation constraint: the runtime must not rely on any global mutable state
  of its own (allocator caches, string tables) without being made per-thread.

---

## 12. Errors and panics

- **Expected failures** (I/O, parsing, validation) are values: `Result[T, E]`.
- **Bugs** panic. A panic prints the code, message, file:line:col, and a stack trace to stderr, then exits with status 70:
  ```
  panic P0002: integer overflow
      at Main.pst:12:15
  ```
  Paths are relative to the program's directory; standard units show their file name.
  `requires`, `ensures` and `assert` panics quote the failed condition (and the assert's message).

| Code | Cause |
|---|---|
| `P0001` | index out of bounds |
| `P0002` | integer overflow |
| `P0003` | subrange violation |
| `P0004` | division by zero |
| `P0005` | `get` on `None` / `Err` |
| `P0006` | string byte offset not on a `Char` boundary |
| `P0007` | `fromCString` of a null pointer |
| `P0008` | missing map key (check with `k in m`, or use `get`/`getOr`) |
| `P0009` | `Char(i)` of a value that isn't a Unicode scalar value |
| `P0010` | `requires` violated |
| `P0011` | `ensures` violated |
| `P0012` | function value passed to C captures variables (§10.3) |
| `P0020` | `assert` failed |

`halt(code)` exits immediately (deferred statements do not run).

A checked build also ends with status 70 if heap blocks leaked (§11).

---

## 13. Tests

```pascal
test "largest of empty list is None"
    assert largest[Integer]([]).isNone()
```
- `test` blocks are top-level, in any file. They are checked with the file, but compiled only
  by `pst test`.
- Tests can call non-exported functions of their own unit.
- `pst test PATH [--filter text] [--timeout seconds] [--unchecked] [--json]` runs the tests
  of a file (a program or a unit), or of each `.pst` file directly in a directory, in name
  order. Each file is checked and built on its own, with the units it uses; their tests
  don't run. A program's body doesn't run either.
- Each test runs in its own process: test block number *i* of the file (counting from 0) is
  run by the test executable with argument *i*. A test passes if its process exits with
  status 0, so a panic or a `halt` with another status fails it. `--filter` runs only the
  tests whose name contains the text. `--timeout` ends a test still running after that many
  seconds, with the signal SIGALRM, and fails it with the line `timed out after N s`.
- Output, on standard output: one line per test, `ok   Shop.pst:10: double doubles` or
  `FAIL Shop.pst:13: half is wrong`; after a failure, what the test wrote to standard output
  and standard error, indented by 4 spaces, and `exit status N` unless it panicked (status
  70) or timed out; then `P passed, F failed`. Diagnostics go to standard error. The exit status is 1 if a
  test failed or a file didn't build, else 0.
- With `--json`, diagnostics and tests are printed as one JSON object per line, without the
  summary. A test's object is its block's position (§15.1), `"test"` (its name), `"passed"`,
  `"timedOut"` and `"output"`: `{"file": "Shop.pst", "line": 13, "col": 1, "endLine": 15,
  "endCol": 23, "test": "half is wrong", "passed": false, "timedOut": false, "output":
  "panic P0020: ..."}`.

---

## 14. Standard units (v0.1 scope)

| Unit | Contents |
|---|---|
| `System` (implicit) | core types, `Option`, `Result`, `Iterator`, `iter`, adapters (including `chars`) and consumers (§4.14), `length`, `add`, `insert`, `concat`, `remove`, `removeLast`, `sort`, `inc`, `dec`, `ord`, `new`, `weak`, `toString`, `justify(s, width)` (pads with spaces to `width` characters, in front, or after for a negative width; §2.8), conversions, `halt`, `assert` support |
| `IO` | `write`, `writeln` (standard output), `writeErr`, `writelnErr` (standard error), `readln(): Option[String]`, `read(count): Option[String]` (exactly `count` bytes, `None` if the input ends first); files: `open` (reading), `create(path, mode := $1A4)` (octal 644) (writing; creates or empties), `close`, `readAll`, `readBytes`, `writeAll`, `writeBytes` returning `Result[_, String]` with C's error message. A `File` closes when its last reference goes away. Written in Pasthon on libc's `open`, `read`, `write`, `close` |
| `Strings` | `bytes`, `fromBytes` (invalid UTF-8 becomes U+FFFD), `charCount`, `byteLen`, `slice`, `pos` (byte offsets), `split`, `join`, `trim`, `replace`, `startsWith`, `endsWith`, `parseInt`, `parseReal` (`[+-]digits[.digits][(e|E)[+-]digits]`), `repeated(s, count)`, `upperCase`/`lowerCase` (ASCII letters only), `lines` (split at `\n` or `\r\n`; a break at the end starts no line), `hexStr(n, digits := 1)` and `binStr(n, digits := 1)` (upper case, zero-padded, `-` for negatives), `globMatch(text, pattern)` (the whole text; `*`, `?`, `[a-z]`, `[!...]`). `split` and `replace` require a non-empty separator. Written in Pasthon on libc's `memmem`, `memcpy`, `strtod` and `snprintf`, except `charCount`, `byteLen`, `slice` and `startsWith` (compiler intrinsics) |
| `Math` | `Pi`, `sqrt`, `sin`, `cos`, `abs`, `min`, `max`, `floor`, `ceil`. Written in Pasthon on libm |
| `Bits` | `popCount` on all integer types (a compiler intrinsic); the bit operations are operators (§9.1) |
| `OS` | `args(): List[String]` (without the program name), `env(name): Option[String]`, `exit(code)`, `currentDir(): String`, `realPath(path): Result[String, String]` (absolute, with symbolic links, `.` and `..` resolved), `isFile(path): Boolean` (a regular file, following symbolic links), `isDir(path): Boolean`, `listDir(path): Result[List[String], String]` (the entry names, without `.` and `..`, in no particular order), `programPath(): String` (the running executable, from `/proc/self/exe`), `tempDir(): Result[String, String]` (a new private directory under `TMPDIR`, or `/tmp`), `makeDir(path, mode := $1ED)` (octal 755), `removeFile(path)` and `removeDir(path)` (an empty directory; all three `Result[Integer, String]`), `run(path, args, output := "", errors := "", input := "", dir := ""): Result[Integer, String]` (runs an executable and waits: its exit status, or minus the signal that ended it; 127 if it can't be started; with `output`, its standard output goes to that file, and its standard error too unless `errors` names another file; with `input`, it reads that file; with `dir`, it runs in that directory, while the file paths stay relative to the caller's). Written in Pasthon on libc (`getcwd`, `realpath`, `stat`, `readlink`, `mkdtemp`, `mkdir`, `unlink`, `rmdir`, `opendir`, `readdir`, `fork`, `dup2`, `chdir`, `execv`, `waitpid`); `args` reads `argv`, which the program's entry point keeps in a writable word after the GOT (§15.2) |
| `Paths` | file paths as text, POSIX style and lexical only (the file system is never consulted; `OS.realPath` follows links): `isAbsolute`, `joinPath(base, rest)` (`rest` if it's absolute), `baseName` and `dirName` (trailing slashes ignored; `c` and `a/b` for `a/b/c`, `.` as the directory of a bare name), `extension` (with its dot; leading dots don't start one, so `.profile` has none), `stem`, `withExtension(path, ext)`, `components` (`/` first for an absolute path; `.` and empty parts left out), `normalize` (repeated slashes, `.`, and each `..` with the part before it removed; `.` if nothing is left), `relativeTo(path, base): Option[String]` (both absolute or both relative; `None` when the way out of `base` passes a `..`). Written in Pasthon on `Strings` |
| `Json` | `JValue = JNull \| JBool \| JNumber \| JString \| JArray \| JObject` (an object keeps its `Member`s in order; a number keeps its text), `parse(s): Result[JValue, String]` (RFC 8259; the error gives the character offset), `toText(j)` (one line, no spaces), `quote(s)` (a JSON string literal), and readers that never fail: `field(j, key)` (`JNull` if missing), `isNull`, `asString`, `asBoolean`, `asInteger` (`Option`s), `items` (empty if not an array). Written in Pasthon |
| `Threads` | reserved for §11.2; not in v0.1 |
| `C` | C interop types, `toCString`, `fromCString`, `fromCBytes`, `isNull`, `addr`, `cast` |
| `Time` | dates and times modeled on Joda-Time, with nanosecond precision and the proleptic Gregorian calendar (years -999_999 to 999_999). Immutable values: `Instant` (seconds and nanoseconds since 1970-01-01T00:00:00Z), `Duration` (seconds and 0 to 999_999_999 nanoseconds), `LocalDate`, `LocalTime`, `LocalDateTime`, and the enum `Weekday`. Made with `now()`, `ofEpochSecond(s, nanos := 0)`, `ofEpochMilli`, `ofSeconds(s, nanos := 0)`, `ofDays`, `ofHours`, `ofMinutes`, `ofMillis`, `ofNanos`, `date(y, m, d)`, `time(h, m, second := 0, nano := 0)` (both panic `P0010` on invalid parts; `isValidDate`, `isValidTime` check first), `dateTime(date, time)`, `ofEpochDay`, `ofNanoOfDay`. Read with accessors (`year`, `month`, `day`, `weekday`, `dayOfYear`, `hour`, `minute`, `second`, `nano`, `epochSecond`, `toEpochMilli`, `toEpochDay`, `toNanoOfDay`, `seconds`, `nanos`, `toMillis`, `toNanos`). Arithmetic: `plus`/`minus` with a `Duration` (a `LocalTime` wraps around midnight), `plusDays`, `plusMonths` (a day past the new month's end becomes its last day), `plusYears`, `between(a, b)`, `daysBetween`, `negated`, `abs`, `times`; `compare`, `isBefore`, `isAfter` (`isLonger`, `isShorter` for durations). `toLocalDateTime(i, offsetSeconds := 0)` and `toInstant(dt, offsetSeconds := 0)` convert at a fixed UTC offset. `toString` writes ISO-8601 (`2026-11-10T09:30:00.250Z`, `PT8H6M12.5S` as `PT8H6M12.500S`: fractions in 3, 6 or 9 digits, years past 9999 with `+`); `parseDate`, `parseTime`, `parseDateTime`, `parseInstant` (`Z` or `±hh:mm`), `parseDuration` (`[±]PnDTnHnMn.nS`, a day being 24 hours) return `Result[_, String]` with the character offset of the error. Time zones: a `Zone` comes from `zone(name)` (an IANA name, read from the TZif file (RFC 8536, versions 1 to 4, leap seconds ignored) under `TZDIR` or `/usr/share/zoneinfo`, with the POSIX TZ rule in its footer for instants after its last transition), `systemZone()` (`TZ`: a name, a file, or a POSIX TZ rule; else `/etc/localtime`), `utc()` or `fixedZone(offsetSeconds)`; `offsetAt(z, i)`, `isDstAt`, `abbreviationAt`, `name`. A `DateTime` is an `Instant` in a `Zone`: `inZone(i, z)`, `now(z)`, `atZone(localDateTime, z)` (in a gap the time moves forward by the gap's length, in an overlap the earlier instant is taken), `toInstant`, `zone`, `offset`, `toLocalDateTime`, `date`, `time`; `plus`/`minus` add exact durations, `plusDays`/`plusMonths`/`plusYears` move the local date-time and resolve it again; `toString` gives `2026-11-10T09:30:00+01:00[Europe/Amsterdam]` (no brackets for a fixed offset, `Z` for offset 0), `parseZoned` reads it back (the offset gives the instant). A `Period` is calendar years, months and days, each may be negative: `period(years := 0, months := 0, days := 0)`, `years`, `months`, `days`, `plus`, `negated`, `normalized` (whole years out of the months), `between(a, b)` for dates (adding it to `a` gives `b` when `b` isn't before `a`), and `plus`/`minus` on a `LocalDate`, `LocalDateTime` or `DateTime` (years and months first, clamping the day, then days; a `DateTime` is resolved again with `atZone`); `toString` gives `P1Y2M3D` (`P0D` for zero), `parsePeriod` reads `[±]PnYnMnWnD` (a week is 7 days, a part may have its own sign). An `Interval` is the instants from a start up to, not including, an end: `interval(start, end)`, `interval(start, d)`, `start`, `end`, `duration`, `contains`, `overlaps`, `abuts`, `overlap` and `gap` (`Option`s); `toString` gives `start/end`, `parseInterval` reads `start/end`, `start/duration` or `duration/end`. Written in Pasthon on libc's `clock_gettime` |

---

## 15. Toolchain

| Command | Purpose |
|---|---|
| `pst build Main.pst [-o out] [--unchecked]` | compile to an ELF executable |
| `pst run Main.pst [-- args]` | build to a temp dir and run |
| `pst test PATH [--filter text] [--timeout seconds]` | run `test` blocks (§13) |
| `pst check [path] --json` | type-check only, emit diagnostics |
| `pst fmt PATH [--check]` | canonical formatter (§15.3) |
| `pst lsp` | language server over stdio, for human editors (§15.4) |
| `pst mcp` | MCP server over stdio, for AI agents (§15.7) |
| `pst query <request> --json` | one-shot semantic queries for scripts and AI agents (§15.5) |
| `pst index [path] --json` | dump the whole project's symbol index (§15.5) |
| `pst tokens FILE` / `pst ast FILE` | debugging dumps of the lexer and parser |

`compiler/Pst.pst` is the tool, written in Pasthon. It has `check`, `build`, `run`, `test`,
`fmt`, `lsp`, `mcp`, `query`, `index`, `tokens` and `ast` so far, and finds the standard units in `units` next to its executable.
`tokens` and `ast` print their own text format, one token or node per line, then the
diagnostics; they exit with status 1 if there are errors.

`scripts/bootstrap.sh` builds the tool into `build/pst` (with `build/units`). The seed is
`$PST_SEED` if set, else the latest GitHub release. The seed builds stage 1, stage 1 builds
stage 2 and stage 2 builds stage 3; stages 2 and 3 must be identical (stage 1 differs when
the code generation changed since the seed). A tag `v*` makes the release workflow publish
`pst-linux-x86_64.tar.gz` (`pst` and `units/`). The compiler may only use the features
that the latest release compiles.

`build/pst run tests/Run.pst` runs the cases in `tests/cases`: shell command lines whose
transcript (exit status, standard output and standard error) must match the case's
`expected.out`; `-- --update` writes the transcripts instead.
The standard units and the compiler's units have `test` blocks (§13), which the case
`unit-tests` runs with `pst test`.
The code in `std/`, `compiler/`, `examples/` and `tests/Run.pst` is formatted with `pst fmt`;
the case `fmt.repo` checks it.

### 15.1 Diagnostics
Human format: `Main.pst:12:5: error E0502: result of 'restock' is ignored`.

JSON format (`--json`, one object per line):
```json
{
  "file": "Main.pst",
  "line": 12, "col": 5, "endLine": 12, "endCol": 24,
  "severity": "error",
  "code": "E0502",
  "message": "result of 'restock' is ignored",
  "fixes": [
    {"description": "discard the result",
     "edits": [{"line": 12, "col": 5, "endLine": 12, "endCol": 5, "text": "_ := "}]}
  ]
}
```
Error codes are stable forever. Each has a doc page (`pst explain E0502`).

**Positions** in all `pst` JSON output: `line` and `col` are 1-based, and `col` counts
Unicode code points. Ranges are half-open (`endCol` is one past the last character).
Paths are relative to the project root. The language server converts to whatever
encoding the client negotiates (§15.4).

### 15.2 Compilation model
1. **Lex + parse** every file into an AST. Indentation becomes tokens.
2. **Declaration pass**: collect all top-level names per unit (this is why `forward` isn't needed).
3. **Check pass**: type-check each function and instantiate generics on demand. Produces a
   typed AST and the **symbol index** (§15.6). This is where the front end ends, and
   `check`, `lsp`, `query`, `index` stop here.
4. **Emit pass**: walk the typed AST and emit x86-64 machine code directly with a simple
   stack-based expression scheme. No IR, no optimizer.
5. **Link**: the compiler writes the ELF itself (`PT_INTERP` = `/lib64/ld-linux-x86-64.so.2`,
   dynamic section, a GOT for external symbols, writable words for the runtime after it, the
   runtime emitted along with the program).

Checked build (default): bounds, overflow, subrange, contracts. `--unchecked` drops all of
them except `assert`.

### 15.3 Formatter rules
`pst fmt PATH` formats a file, or the `.pst` files of a directory (not its subdirectories),
in place, and prints the path of each file it changed. With `--check` it writes nothing and
exits with status 1 if a file would change. A file with lexer errors is left alone: its
diagnostics go to standard error and the status is 1. The formatter works on the tokens and
comments, so it formats a file that doesn't parse; it changes only layout, never tokens,
except for adding a trailing comma to a broken group and rewriting `else` + `if` (below).

- 4-space indent, max line length 100, one statement per line, no semicolons.
- Spaces: none before `,` `:` `.` `..` `)` `]` `}`, none after `.` `..` `(` `[` `{`, none
  after a unary `-` or `+`, and none before `(` or `[` after a name, a string literal, `)`,
  `]`, `deref`, `function`, `procedure` or `This`. One space everywhere else, so around
  binary operators and `:=`, after `:` and `,`, and between `not` and `(`.
- The expressions in string placeholders are formatted the same way.
- Blank lines: a run of blank lines becomes one; none at the start of a block, the start or
  the end of the file.
- An `else` whose block is a single `if` chain (and nothing else, not even an end marker)
  becomes `else if`.
- Comments stay on their line. A trailing comment gets two spaces before it. A `{ }`
  comment followed by code on its line stays in front of that code. A comment on
  its own line is indented like the next line of code, or deeper (up to the previous line's
  depth) if it was written deeper, so a comment closing a block stays in it.
- Existing end markers are preserved. The formatter never adds or removes them.
- A line longer than 100 columns breaks inside brackets: the widest group, preferring a
  `(` group, is printed one item per line, one level deeper, each item followed by a comma,
  and the closer on its own line. If its last item is itself a group taking more than half of
  it, that group breaks instead and hugs the outer brackets (`writeln(join(` ... `))`),
  except in a parenthesized expression with an `and` or `or` of its own.
  This repeats until the line fits or nothing more can break. A group only breaks if the
  text before and after it on the line each fit and are no wider than its contents, and a
  group of one item with no groups or `and`/`or` inside only if that item fits on its own
  line; otherwise the line stays long.
- A parenthesized expression gets no trailing comma; without commas, it breaks before each
  `and` and `or` at its own level instead, and a `for` expression also before each `for`
  clause after the first and before `do`.
- A group stays broken if it ends with a trailing comma, holds a comment, or holds a broken
  group that isn't its last item (or any broken group, in a parenthesized expression with
  its own `and` or `or`). Formatting formatted code changes nothing. To keep a short list one item per line, end it with a
  comma; to join it, remove the comma.

### 15.4 Language server (`pst lsp`)
Intended for human editors. (AI agents should use `pst mcp` (§15.7) or `pst query` (§15.5).)
Built into the `pst` executable, so it is always the same version as the compiler and uses
the same front end. JSON-RPC over stdio, LSP 3.17. It has no dependencies beyond the compiler.
Editor setup: `docs/lsp4ij.md` (IntelliJ IDEA and other JetBrains IDEs) and `docs/vscode.md`
(VS Code, with the extension in `editors/vscode`).

So far it synchronizes whole documents (`textDocumentSync` 1), pushes diagnostics, formats,
and answers completion, hover, signature help, definition, type definition, implementation, references, rename, code actions, document
symbols, workspace symbols, call hierarchy, semantic tokens, inlay hints and folding ranges. After each change it checks
every open document, with the open documents' texts read in place of their files (§15.6),
and pushes the diagnostics that are in that document; closing a document clears its
diagnostics. Columns are converted to UTF-16 code units. A request about a position checks
its document again and looks up the name at the position, or the name just before it:

- Completion after `x.` (also triggered by typing `.`) offers a unit's exports, an enum's
  members, or the fields of a value's record type followed by the routines whose first
  parameter takes it (UFCS, §6.6). Elsewhere it offers the locals declared earlier in the
  same routine (or main body), the document's top-level names, the units it uses and their
  exports, and keywords. Only names starting with the word before the position are
  offered, each with its hover's first line as detail. When the text doesn't parse, the
  receiver is looked up in the text without the `.` and the word, and then in the text
  with the receiver alone on its line.
- Hover shows a routine's signature, the first line of a type, constant or field
  declaration, a local's type (`let count: Integer`), or the kind and name of anything
  else, followed by the doc comment.
- Signature help finds the innermost unclosed `(` before the position whose callee is a
  name, from the document's tokens, so it works while the call is unfinished. It shows each
  routine (or record, for construction) the name may mean, with parameter types, `var`
  markers and defaults, and marks the argument at the position: by name if it is named,
  else by position, counting the receiver of `a.f(...)` as the first.
- Definition gives the defining name, one per overload the name may still mean.
- Type definition gives the declarations of the types in the type of the name at the
  position: of a local, constant, field or enum member, or what a function returns. The
  type's arguments and `ref` targets count too, so `List[ref Item]` leads to `Item`.
- Implementation goes from a trait to the record, enum, union and subrange types that
  satisfy it (§4.10), and from a trait routine to the routines that provide it for those
  types. It looks at the types declared in the open documents and the units they use,
  apart from standard units, with the routines visible from each open document.
- References searches every open document and the units it uses; documents that aren't
  open are only searched as units used by an open one.
- Rename edits the same places references finds, in files that aren't open too. It
  refuses (error `-32803`) symbols of standard units, units themselves (rename the file),
  names that break the naming rules for the symbol's kind (§2.5), and renames after which
  the open documents would have more errors than before.
- Code actions are the fixes of the document's diagnostics on the requested lines.
- Document symbols are the document's types, constants and routines, with enum members,
  fields and trait routines as children.
- Workspace symbols are the types, constants, routines, fields and enum members declared
  in the open documents and the units they use, apart from standard units, whose names
  contain the letters of the query in order, ignoring case.
- Call hierarchy starts from the routine at the position. Incoming calls are the uses of
  the routine in the open documents and the units they use, grouped by the routine they
  are in, or by the document for uses outside any routine. Outgoing calls are the
  routines named in the routine's declaration.
- Semantic tokens (whole documents only) give each name the type of what it means:
  namespace (units), type, enum, interface (traits), struct (records), typeParameter,
  parameter, variable, property (fields), enumMember or function; with the modifiers
  declaration, readonly (constants, `let`, enum members) and defaultLibrary (standard
  units and builtins). Keywords, numbers, strings (each part of an interpolated string)
  and comments are tokens too.
- Inlay hints show the inferred type after the name of each `let`, `var` and loop
  variable declared without a type in the requested lines, and the inferred type arguments
  of each generic routine called there without them, after its name: `concat[Integer]`.
- Folding ranges fold each line followed by more deeply indented lines, up to the last of
  them; blank lines don't end a fold.

Other requests get the error `-32601`. `exit` ends with status 0 after `shutdown`, else 1,
as does the end of the input. The table below is the full plan.

| LSP feature | Pasthon behavior |
|---|---|
| Diagnostics (push) | Same codes, messages, and fixes as `pst check` |
| Code actions | Every diagnostic `fix` becomes a quick fix |
| Hover | Declared or inferred type, doc comment, contracts (`requires`/`ensures`); for generics, the instance at that position |
| Go to definition | Variables, functions, types, fields, enum members, units; trait-bound calls in a generic jump to the trait member |
| Go to type definition | From any expression to its type declaration |
| Go to implementation | From a trait (or trait member) to every type and function that satisfies it in the workspace |
| Find references | All uses, including UFCS calls (`p.length()` is a reference to `length`) |
| Document / workspace symbols | Units, types, functions, constants, tests, with export status |
| Completion | Locals, parameters, unit members, keywords, snippets for block headers. After `x.`: fields of `x`, plus every visible function whose first parameter accepts `x`'s type by value (UFCS), sorted fields first |
| Signature help | Parameter names, types, `var` markers, defaults |
| Rename | Safe across units, respects naming rules (`E0020`) and refuses on conflicts |
| Formatting | `pst fmt` (whole document only, since the format is canonical) |
| Inlay hints | Inferred types for `var x :=` / `let x :=`, and inferred generic arguments |
| Semantic tokens | Distinguishes types, traits, enum members, functions, `var` parameters, constants |
| Folding ranges | From indentation, and from end markers |
| Call hierarchy | Incoming and outgoing calls |

### 15.5 Query CLI for agents (`pst query`, `pst index`)
AI agents and scripts often can't hold an LSP session open. The same data is available as
one-shot commands with JSON output. Each command takes a position as `File.pst:line:col`
(same convention as §15.1), or, apart from `complete`, a symbol's name: `name`,
`Unit.name` or `Type.name`.

| Request | Returns |
|---|---|
| `pst query def File.pst:12:5` | definition location(s) |
| `pst query refs File.pst:12:5` | all reference locations |
| `pst query type File.pst:12:5` | type of the expression, and its declaration location |
| `pst query hover File.pst:12:5` | everything hover shows, structured |
| `pst query complete File.pst:12:9` | completion items with kinds and signatures |
| `pst query symbols [File.pst]` | symbols of one file, or the project |
| `pst query impls Measurable` | types and functions satisfying a trait |
| `pst query callers restock` / `callees restock` | call graph edges |
| `pst query accepts Item` | what `x.` can call on a value of that type (§6.6) |
| `pst query instances largest` | every generic instance and where each is created |
| `pst query explain E0502` | error code documentation |

`pst index [PATH] --json` writes the complete symbol index (§15.6) in one document, so an
agent can load the whole project's structure in one call instead of opening files. It
checks the file, or the `.pst` files of the directory (by default the current one), and
prints `"symbols"` and `"references"`:

- each symbol is like hover's, with an `"id"`: every symbol declared or used in the
  files, including those of the standard units and built-ins;
- each reference is a location with the `"symbol"` id it means and `"isDef"`: every name
  in the files.

All query output has a stable, versioned schema (`"schema": "pst-query/1"`).

So far `pst query` has `def`, `refs`, `type`, `hover`, `complete`, `impls`, `callers`,
`callees`, `accepts`, `instances`, `explain` and `symbols`. Each prints one JSON
object with `"schema"` and one list:

- `def` gives `"locations"`: the defining names, one per overload the name may mean.
- `refs` gives `"locations"` with `"isDef"`: the definition and uses of the symbol in the
  `.pst` files of the file's directory and the units they use.
- `type` gives `"types"`: for each meaning of the name, the type of its value (or what a
  function returns) as `"type"`, and the `"declarations"` of the types it is made of.
- `hover` gives `"symbols"`, each with `"name"`, `"kind"`, the type or routine it belongs
  to as `"container"`, the `"type"` of a value, the `"declaration"` in short (a routine's
  signature, a declaration's first line), the `"doc"` comment and the `"location"`.
- `complete` gives `"items"`: what the editor's completion offers at the position, each
  with `"label"`, `"kind"` (a symbol kind, or `"keyword"`) and, for a symbol, its
  `"detail"` (hover's declaration).
- `impls` on a trait gives `"symbols"`, each with `"name"`, `"kind"` and `"location"`: the
  types that satisfy it. On a trait routine, it gives the routines that provide it. Both
  search the `.pst` files of the file's directory and the units they use, apart from the
  standard units.
- `callers` on a routine gives `"callers"`: one entry per routine that calls it, as
  `"caller"` (name, kind, location), or per unit level as `"file"`, each with the
  `"calls"` locations. It searches like `refs`.
- `callees` on a routine gives `"callees"`: one entry per routine declared in source that
  it calls, as `"callee"`, with the `"calls"` locations in its declaration.
- `accepts` on a type, or on a value, gives `"symbols"`, each with `"name"`, `"kind"`,
  `"location"` and `"declaration"`: the routines whose first parameter takes a value of
  that type, so `x.f()` can call them (§6.6), with generic routines whose bounds the
  type satisfies. A generic type stands for any of its instances. The routines are those
  the file of the position (or of the name's declaration) sees: its own, and the exports
  of the units it uses.
- `instances` on a generic routine or type gives `"instances"`: one per list of type
  arguments, as `"instance"` (such as `largest[Integer]`) and `"args"`, with the
  `"locations"` of the calls, constructions and type names that create it, in the `.pst`
  files of the file's directory and the units they use. A use inside a generic is given in
  that generic's own parameters (`largest[T]`); the instances it leads to once that
  generic is itself instantiated aren't followed.
- `explain CODE` gives `"explanations"`: the error or panic code's `"code"`, its
  `"meaning"` as the tables of §2.9, §12 and §16 give it, and the `"section"` and its
  `"title"` that list it. An unknown code is an error.
- `symbols [PATH]` gives `"symbols"` like hover's: the types, constants, routines, fields
  and enum members declared in a file, or in the `.pst` files of a directory (by default
  the current one).

A name is looked up among the types, traits, constants, routines, fields, enum members
and units declared in the `.pst` files of the current directory and the units they use,
qualified by the unit, or by the type or trait it belongs to. The query is then answered at
its definition. The overloads of one routine are answered at each definition, and the
answers' lists joined without repeats; renaming one (`pst_rename`) needs its position. A
name that means nothing, or more than one other thing, is an error that lists what it may
mean, with their positions.

A location is an object with `"file"`, `"line"`, `"col"`, `"endLine"` and `"endCol"`
(§15.1). Files inside the current directory are relative to it; others, such as the
standard units, are absolute. A position that names nothing gives an empty list. An
unreadable file or a malformed position is an error (status 1). `--json` is accepted;
the output is always JSON.

### 15.6 Front-end requirements
These follow from serving editors and agents, and constrain the compiler implementation:

- **Error tolerance.** The parser recovers at line and block boundaries (indentation makes
  this reliable) and always produces a full AST, with error nodes where needed. The checker
  continues past errors, typing broken expressions as an error type that suppresses
  follow-on diagnostics. Unlike classic Turbo Pascal, the compiler never stops at the
  first error.
- **Full source positions.** Every token and AST node keeps its range. Comments, including
  doc comments and end markers, are retained for hover and formatting.
- **Symbol index.** The check pass records, for every identifier occurrence: the symbol it
  resolves to, its kind, its type, and whether it is a definition or a reference. The
  index also records trait satisfaction and generic instances. LSP and query requests are
  lookups into this index, not re-analysis.
- **Incremental by unit.** On an edit, only the changed file is re-parsed. Its unit and the
  units that `uses` it are re-checked, provided its exported signatures changed (otherwise
  only the unit itself). The emit pass never runs in the server.
- **Unsaved buffers.** The front end reads source from an overlay (editor buffers) before
  the file system, so diagnostics reflect what is on screen.

### 15.7 MCP server (`pst mcp`)
The primary interface for AI agents. It is a Model Context Protocol server over stdio,
built into the `pst` executable. Every tool is a thin wrapper over the same front end and
symbol index as `pst query`, and returns the same JSON schemas (§15.1, §15.5).

Configure it in an agent with the command `pst mcp --root <project-dir>`.

**Tools**

| Tool | Input | Returns |
|---|---|---|
| `pst_check` | `paths?` | diagnostics with fixes (§15.1) |
| `pst_apply_fix` | `file`, `line`, `col`, `code` | applies a diagnostic's fix to disk; returns the edits made and new diagnostics |
| `pst_test` | `paths?`, `filter?`, `timeoutMs?` | per-test pass/fail, with assertion messages and positions |
| `pst_run` | `file`, `args?`, `stdin?`, `timeoutMs?` | exit code, stdout, stderr, panic code and position if any |
| `pst_fmt` | `paths?`, `check?` | formatted files, or the list of files that differ |
| `pst_index` | `paths?` | complete symbol index |
| `pst_symbols` | `file?`, `query?` | matching declarations with signatures and doc comments |
| `pst_def` | `file`, `line`, `col` or `symbol` | definition location(s) |
| `pst_refs` | `file`, `line`, `col` or `symbol` | all references (UFCS calls included) |
| `pst_type` | `file`, `line`, `col` | expression type and its declaration |
| `pst_impls` | `trait` | types and functions satisfying it |
| `pst_callers` / `pst_callees` | `symbol` | call graph edges |
| `pst_instances` | `symbol` | generic instances and where they are created |
| `pst_accepts` | `file`, `line`, `col` or `type` | visible functions whose first parameter accepts that type, i.e. what `x.` can call (§6.6) |
| `pst_rename` | `symbol`, `newName`, `dryRun?` | edits across units, refused on conflicts |
| `pst_explain` | `code` | documentation for an error code |

**Rules**
- Tools take either a position or a qualified symbol name (`Geometry.totalArea`), because
  agents usually know names, not columns.
- Results are compact JSON. Large results are paged with `cursor`.
- Only `pst_apply_fix`, `pst_fmt` (without `check`), and `pst_rename` (without `dryRun`)
  write files, and only inside the project root. `pst_run` executes the program and is
  marked as such in its tool description, so agents can ask for approval.
- The server watches the project directory, so files edited by the agent through other
  tools are re-checked automatically before the next tool call.

So far `pst mcp [--root DIR]` has `pst_check`, `pst_symbols`, `pst_index`, `pst_def`,
`pst_refs`, `pst_type`, `pst_hover`, `pst_impls`, `pst_callers`, `pst_callees`,
`pst_accepts`, `pst_instances`, `pst_explain`, `pst_fmt`,
`pst_apply_fix`, `pst_rename`, `pst_run` and `pst_test`. It reads
one JSON-RPC message per line and answers `initialize`, `ping`, `tools/list` and
`tools/call`; notifications are ignored. It runs in the root (by default the current
directory), so paths are relative to it, and it reads the files from disk on every call,
so it needs no watching.

- Each tool's result is one text item holding the JSON that `pst query` or `pst index`
  prints for it (§15.5). `pst_hover` is `pst query hover`; `pst_symbols` takes a file or
  directory as `file`, and its `query` keeps the names that contain its letters in order.
- A result's lists hold at most 200 items each. When one holds more, the result says
  where the next page starts as `"nextCursor"`; the call made again with that as its
  `cursor` gives the next 200 items of each list. Results of the tools that write files
  (`pst_fmt`, `pst_apply_fix` and `pst_rename`) aren't paged.
- `pst_check` gives `"errors"`, the number of errors, and `"diagnostics"` as `pst check
  --json` prints them (§15.1), each once, for the `.pst` files of `paths`.
- `pst_fmt` gives the `"changed"` files, whether they were `"written"`, and the
  `"diagnostics"` of files that don't parse.
- `pst_apply_fix` applies the fix numbered `fix` (by default 0, the first) of the
  diagnostic with `code` whose range holds the position. It gives the `"file"`, the
  `"edits"` made (as in §15.1) and the file's new `"errors"` and `"diagnostics"`.
- `pst_rename` takes `symbol`, or `file`, `line` and `col`. It gives the `"files"` it
  changes, the `"edits"` (the locations of the old name) and whether they were
  `"written"`. It searches like `pst query refs`, and is refused like the language
  server's rename (§15.4), or when a file to change is outside the root.
- `pst_run` builds the program with `pst build --json`. If that fails it gives
  `"built": false` and the `"diagnostics"`. Else it runs the program with `args`, with
  `stdin` (by default empty) as its standard input, and ends it after `timeoutMs`
  (by default 10000, rounded up to whole seconds). It gives `"built": true`, the
  `"diagnostics"`, the `"exitCode"` (or `"signal"`, or `"timedOut": true`), `"stdout"`,
  `"stderr"` and, if it panicked, the `"panic"` with `"code"`, `"message"`, `"file"`,
  `"line"` and `"col"`.
- `pst_test` runs `pst test --json` on the `.pst` files of `paths`, with `filter`, and
  `--timeout` from `timeoutMs` (by default 10000, rounded up to whole seconds). It
  gives the numbers `"passed"` and `"failed"`, the `"tests"` as `pst test --json` prints
  them, and the other `"diagnostics"`.
- A tool that fails, such as on an unreadable file or a name that means nothing, gives
  its message with `"isError": true`.

### 15.8 Implementation priority
All tooling layers share one front end, so each layer after the first is small:

1. `pst check --json`: diagnostics, the core of the agent feedback loop
2. `pst index` / `pst query`: symbol index and one-shot queries
3. `pst mcp`: the agent interface
4. `pst lsp`: the human editor interface

---

## 16. Grammar (EBNF sketch)

`NEWLINE`, `INDENT`, `DEDENT` come from the lexer. `block = NEWLINE INDENT stmt+ DEDENT`.
`DISCARD` is `_` or any word starting with `_` (§7.9).

```ebnf
file        = header [uses] { topDecl | stmt } ;   (* stmt: program only *)
header      = ("program" | "unit") IDENT NEWLINE ;
uses        = "uses" IDENT { "," IDENT } NEWLINE ;
topDecl     = typeSect | constSect | routine | externBlk | testBlk ;

typeSect    = "type" NEWLINE INDENT { typeDecl } DEDENT ;
typeDecl    = ["export"] IDENT [typeParams] "=" typeBody ;
typeBody    = typeExpr NEWLINE
            | "(" IDENT { "," IDENT } ")" NEWLINE                      (* enum *)
            | constExpr ".." constExpr NEWLINE                         (* subrange *)
            | typeExpr "|" typeExpr { "|" typeExpr } NEWLINE           (* union *)
            | "record" NEWLINE [INDENT { field } DEDENT]
            | "trait" NEWLINE INDENT { field | signature NEWLINE } DEDENT ;
field       = ["export"] IDENT ":" typeExpr [":=" constExpr] NEWLINE ;  (* no export in traits *)
typeExpr    = IDENT { "." IDENT } [typeArgs] | "This" | "ref" typeExpr
            | ("function" | "procedure") "(" [typeExpr {"," typeExpr}] ")" [":" typeExpr] ;
typeParams  = "[" typeParam { "," typeParam } "]" ;
typeParam   = IDENT [":" bound { "+" bound }] ;
bound       = IDENT [typeArgs] ;
typeArgs    = "[" (typeExpr | constExpr) { "," (typeExpr | constExpr) } "]" ;

constSect   = "const" NEWLINE INDENT { ["export"] IDENT "=" expr NEWLINE } DEDENT ;

routine     = ["export"] ["unsafe"] signature routineBody ;
signature   = ("function" | "procedure") IDENT [typeParams]
              "(" [param { "," param }] ")" [":" typeExpr] ;
param       = ["var"] ( "this" | IDENT { "," IDENT } ":" typeExpr [":=" expr] ) | "..." ;
routineBody = NEWLINE INDENT { "requires" expr NEWLINE } { "ensures" expr NEWLINE }
              stmt { stmt } DEDENT ;

externBlk   = "external" STRING NEWLINE INDENT { externDecl NEWLINE | typeSect } DEDENT ;
externDecl  = ["export"] signature ["name" STRING] ;
testBlk     = "test" STRING block ;

stmt        = varDecl | assign | callStmt | ifStmt | whileStmt | repeatStmt
            | forStmt | caseStmt | deferStmt | "break" NEWLINE | "continue" NEWLINE | "pass" NEWLINE
            | "return" (rhs | NEWLINE) | DISCARD ":=" rhs
            | "assert" expr ["," expr] NEWLINE ;
varDecl     = ("var" | "let") IDENT [":" typeExpr] ":=" rhs ;
assign      = lvalue ":=" rhs ;               (* lvalue: name, field, index, deref(r) *)
callStmt    = ["try"] call NEWLINE ;
ifStmt      = "if" expr "then" block { "else" "if" expr "then" block } ["else" block] ;
whileStmt   = "while" expr "do" block ;
repeatStmt  = "repeat" block "until" expr NEWLINE ;
forStmt     = "for" IDENT ":=" expr ("to" | "downto") expr "do" block
            | "for" (IDENT | DISCARD) ["," (IDENT | DISCARD)] "in" expr "do" block ;
caseStmt    = "case" expr "of" NEWLINE INDENT { caseArm } DEDENT ;
caseArm     = (label { "," label } | "else") ":" (simpleStmt | block) ;
label       = constExpr [".." constExpr] | IDENT ["(" (IDENT | DISCARD) ")"] ;
deferStmt   = "defer" (simpleStmt | block) ;

expr        = lambda | ifExpr | orExpr ;     (* orExpr: precedence levels of §9.1 *)
lambda      = (lambdaParam | "(" [lambdaParam { "," lambdaParam }] ")") "=>" expr ;
lambdaParam = IDENT | DISCARD ;
ifExpr      = "if" expr "then" expr { "else" "if" expr "then" expr } "else" expr ;
caseExpr    = "case" expr "of" NEWLINE INDENT { exprArm } DEDENT ;
exprArm     = (label { "," label } | "else") ":" expr NEWLINE ;
rhs         = caseExpr | rhsLambda | expr NEWLINE ;  (* right side of := / var / let, and return operand *)
rhsLambda   = (lambdaParam | "(" [lambdaParam { "," lambdaParam }] ")") "=>" (caseExpr | rhsLambda) ;
```

The parser can't tell `name[...]` (index) from `name[...]` (explicit instantiation)
syntactically. It produces one `Index` node, resolved in the check pass.

A block may end with an end marker (§2.2) on its own line; the parser checks it against
the block it closes.

### 16.1 Syntax errors

| Code | Meaning |
|---|---|
| `E0010` | end marker doesn't match its block, or follows no block it can close (fix: correct marker) |
| `E0040` | syntax error: expected X, found Y |
| `E0041` | unexpected indentation |
| `E0042` | expected an indented block; bodies can't be empty |
| `E0043` | chained comparison (`a < b < c`) |
| `E0044` | `uses` not directly after the header, or repeated |
| `E0045` | missing or repeated `program` / `unit` header |
| `E0046` | invalid assignment target |
| `E0047` | expression is not a statement (fix for `x = 1`: `:=`) |
| `E0048` | declaration not allowed here (types, routines, externs, tests are top level only) |
| `E0049` | block doesn't start on a new line (`if a then f()`) |

The parser also reports `E0101`, `E0104`, `E0201`, `E0202`, `E0801`, `E0803`, `E0804`,
`E0805`, and `E0903` where they are purely syntactic.

Recovery: an error skips the rest of its line and any block indented under it, leaving an
error node, so one mistake gives one diagnostic. Syntax errors on a logical line that
already has a lexical error are not reported.

### 16.2 Name errors

| Code | Meaning |
|---|---|
| `E0020` | wrong casing for the kind of name (fix: rename the definition and its uses) |
| `E0100` | unknown name, or no such member in a unit or enum (fix: similar visible names) |
| `E0101` | top-level statement in a `unit` (move it into a routine) |
| `E0102` | unit not found, or not a unit |
| `E0103` | top-level variable of the program used inside a routine or `const` |
| `E0104` | a discard `_` read as a value |
| `E0105` | `program` / `unit` name doesn't match the file name (fix: rename) |
| `E0106` | `external "pst"` outside the standard units |
| `E0107` | cyclic `uses` |
| `E0108` | duplicate declaration in one unit, record, trait, enum, or type parameter list |
| `E0109` | name not exported by its unit |
| `E0110` | unqualified name ambiguous between used units (fix: qualify) |
| `E0111` | `This` outside a trait |
| `E0201` | variable declared without a value (`let x := value`) |
| `E0202` | `const` inside a routine (use `let`) |
| `E0205` | variable declared again in the same routine |
| `E0206` | variable never used (fix: discard with `_`) |

### 16.3 Type errors

| Code | Meaning |
|---|---|
| `E0300` | type mismatch (hints: `toReal`, `round`, `Int32(...)`) |
| `E0301` | type of an empty `[]` / `[:]` unknown (declare the variable's type) |
| `E0302` | type argument can't be inferred, or an overloaded/generic routine used as a value |
| `E0303` | wrong number of type arguments |
| `E0304` | not a type |
| `E0305` | operator not defined for the operand types (`/` is Real only; `div`/`mod` integers only) |
| `E0306` | not a value: a type, a unit, or a procedure call |
| `E0307` | calling something that isn't a function |
| `E0310` | indexing a string |
| `E0311` | indexing a type that can't be indexed |
| `E0320` | no such field |
| `E0321` | field private to its unit |
| `E0330` | constructing a record with private fields outside its unit |
| `E0340` | assignment to a `let`, a non-`var` parameter, a loop variable, a binding, or a constant |
| `E0341` | `var` argument that isn't a mutable place |
| `E0350` | `for ... in` over something not iterable |
| `E0420` | trait used as the type of a value |
| `E0421` | interpolated value isn't `Printable` |
| `E0422` | decimals in a placeholder (`{x:0:2}`) on a value that isn't a real |
| `E0501` | function doesn't return on every path |
| `E0502` | function result ignored (fix: `_ := `) |
| `E0503` | `return` without a value in a function, or with one elsewhere |
| `E0504` | `break` / `continue` outside a loop |
| `E0505` | `try` on a non-Option/Result, or in a function not returning a matching Option/Result |
| `E0506` | `continue` in a `repeat` loop (§7.4) |
| `E0507` | `return`, `try`, or a `break`/`continue` of an outer loop in a deferred statement (§7.8) |
| `E0510` | ambiguous overloaded call |
| `E0511` | no overload matches (argument count, names, or types) |
| `E0520` | dot call of a routine whose first parameter is `var` (fix: `f(var a, ...)`) |
| `E0521` | argument for a `var` parameter not marked `var` (fix: add `var`) |
| `E0522` | `var` on an argument whose parameter isn't `var` |
| `E0523` | two `var` arguments of one call overlap (§6.3) |
| `E0524` | `try` inside a `var` argument (§6.3) |
| `E0530` | positional argument with the same type as an earlier parameter group (fix: name it) |
| `E0601` | overlapping `case` labels |
| `E0602` | non-exhaustive `case` (enum or union), or missing `else` (other types) |
| `E0603` | invalid `case` label for the subject's type |
| `E0700` | type argument doesn't satisfy a bound, or isn't a valid `Set` element |
| `E0710` | operation on a type parameter that its bounds don't provide |
| `E0720` | generic instances nest more than 64 deep (§8) |
| `E0801` | `if` expression without `else` |
| `E0802` | `if` / `case` expression branches of different types |
| `E0803` | `if` or `for` expression used as an operand without parentheses |
| `E0804` | `case` expression that isn't the whole right side of `:=`, `var`, `let`, or `return`, or the body of a lambda that is |
| `E0805` | `for` expression whose `if` gives no value in any branch, only `continue` or `break` |
| `E0806` | lambda where no function type is expected |
| `E0807` | lambda with a different number of parameters than the function type expected |
| `E0808` | lambda that captures variables passed to C (§10.3) |
| `E0901` | type isn't C-compatible (external signature, C record field, C varargs argument; §10.1) |
| `E0902` | unsafe operation outside an `unsafe` routine (§10.4) |
| `E0903` | invalid declaration in an external block's `type` section (§10.5) |
| `E0904` | `cast` between types other than pointers and integers (§10.4) |
| `E0905` | `addr` of something other than a variable or a C record field in one (§4.13) |
| `E1101` | calling a destroy hook, or using it as a value (§11.1) |
| `E1102` | destroy hook declared outside its record's unit |
| `E1103` | a value of a resource record outside `ref`: its type written elsewhere than in `ref R` or `Weak[R]`, `R(...)` not directly inside `new`, `deref` of a `ref R`, or a copy of one |

### 16.4 Build errors

| Code | Meaning |
|---|---|
| `E0990` | the program checks, but uses a construct the code generator doesn't support yet |

---

## 17. Deliberately excluded

Classes, inheritance, exceptions, null, implicit numeric conversions, operator overloading,
macros, mutable globals, `goto`, `with`, compound assignment, chained comparisons,
semicolon-separated statements, tuples (use records), closures that share
variables (lambdas copy them), runtime trait objects (use unions).

---

## 18. Open questions

None at the moment.

## 18a. Planned

Pascal, not C, is the model. These changes come in steps; each new form is accepted next to
the old one until a seed release knows it, then std and the compiler move over and the old
form goes. The `$`/`%` literals (§2.7) went through these steps and are finished: v0.3
knew them, std and the compiler moved over, and `0x`/`0b` are gone.

1. Done (§9.1): `and`, `or`, `xor`, `not` bitwise on integers, `shl`/`shr` operators.
   Finished: std and the compiler use them, `Bits` keeps only `popCount`, and `xor`,
   `shl`, `shr` are keywords.
2. Done (§2.8): format specifiers in placeholders, as Pascal's `write(x:width:decimals)`:
   `"{n:5}"` right-aligns in 5 characters, `"{s:-8}"` left-aligns, `"{x:0:2}"` writes 2
   decimals. They replaced `padLeft`, `padRight` and `toFixed`.
3. Done (§14): Pascal names in `Strings`: `upperCase`, `lowerCase`, `hexStr(n, digits)`
   (now upper case), `binStr`, and `pos` for `find` (still an `Option`).
4. Done (§10.1): `external "lib"` and `name "symbol"` instead of `extern` and `as`.
   Finished: std uses them, and `extern` and `as` are gone.
5. Done (§14): `length` instead of `len`.
   Finished: std and the compiler use `length`, and `len` is gone.
6. Comments are `{ ... }`, the only comment style: `#` goes, and a `{ }` comment directly
   above a declaration is its doc comment; end markers read `{ end if }`. Sets and maps
   are written with brackets, by the expected type: `[Color.Red, Color.Blue]`,
   `["a": 1, "b": 2]`, and `[:]` for an empty map. Two releases: first the bracket forms,
   then `{ }` comments. Done (§2.3, §2.4, §2.7): the bracket forms, which std and the
   compiler use, and `{ }` comments and `{ end if }` markers. The brace literals are gone.
   Finished: std and the compiler use `{ }` comments and markers, and `#` is gone.

---

## 19. Complete example

```pascal
program Inventory

uses IO, Strings

type
    Status = (Active, Discontinued, Backorder)

    Item = record
        name: String
        qty: Integer
        status: Status

    Named = trait
        name: String

const
    MaxQty = 1000

function restock(var item: Item, amount: Integer): Result[Integer, String]
    requires amount > 0
    ensures item.qty <= MaxQty
    if item.qty + amount > MaxQty then
        return Err("{item.name}: over capacity")
    item.qty := item.qty + amount
    return Ok(item.qty)

function describe(item: Item): String
    return case item.status of
        Active:       "{item.name}: {item.qty}"
        Discontinued: "{item.name}: gone"
        Backorder:    "{item.name}: waiting"

function names[T: Named](xs: List[T]): String
    var parts: List[String] := []
    for x in xs do
        add(var parts, x.name)
    return join(parts, sep: ", ")

function restockAll(var items: List[Item], amount: Integer): Result[Integer, String]
    var total := 0
    for i := 0 to length(items) - 1 do
        total := total + try restock(var items[i], amount)
    return Ok(total)

test "restock refuses over capacity"
    var it := Item(name: "bolt", qty: 999, status: Status.Active)
    assert restock(var it, amount: 5).isErr()
    assert it.qty = 999

var items := [
    Item(name: "bolt", qty: 10, status: Status.Active),
    Item(name: "nut", qty: 0, status: Status.Backorder),
]
case restockAll(var items, amount: 5) of
    Ok(n):  writeln("total stock: {n.value}")
    Err(e): writeln("error: {e.error}")
for item in items do
    writeln(item.describe())
writeln(names(items))
```

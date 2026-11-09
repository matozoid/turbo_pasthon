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

1. **One obvious way.** Each construct has a single syntax. The formatter is canonical and unconfigurable.
2. **Nothing hidden.** No implicit conversions (one exception: injection into a union, §4.7), no exceptions, no null, no operator overloading, no macros.
3. **Contracts visible.** Function signatures are always fully typed. Mutation is marked with `var`. Errors are values.
4. **Machine-checkable.** Built-in contracts (`requires`/`ensures`), built-in tests, JSON diagnostics with stable error codes.
5. **Turbo.** The compiler is fast and simple: parse, check, emit machine code. No optimization passes.

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
- **Line continuation:** only inside open `(`, `[`, `{`. Inside brackets, newlines and indentation are ignored. There is no backslash continuation.

### 2.3 Comments
- `#` to end of line. No block comments.
- Doc comments: `##` directly above a declaration; attached to it by tooling.

### 2.4 Block end markers (optional, verified)
A comment of the exact form

```
# end <keyword> [<name>]
```

placed on its own line, at the indentation of a block header, immediately after
that block's last line, is an **end marker**. The compiler verifies that it
matches the block just closed:

| Block | Marker |
|---|---|
| `if` / `else if` / `else` chain | `# end if` |
| `while` | `# end while` |
| `for` | `# end for` |
| `repeat ... until` | not allowed (`until` already closes it) |
| `case` | `# end case` |
| `function f` / `procedure p` | `# end function f` / `# end procedure p` (name optional) |
| `record` / `trait` type `T` | `# end record T` / `# end trait T` (name optional) |
| `test "..."` | `# end test` |
| `defer` block | `# end defer` |
| `extern` block | `# end extern` |

A mismatch is error `E0010`. Markers are never required.

### 2.5 Identifiers and naming
`[A-Za-z][A-Za-z0-9_]*`. An identifier starts with a letter.

Underscores inside names are allowed but not recommended: idiomatic Pasthon uses camelCase,
and examples, the standard units and generated code never use `_` in names. They exist so
that names from other systems can be kept unchanged. A C symbol that is not a valid
Pasthon name (`_exit`, `SDL_Init`) is imported under a new name with `as` (§10.1).

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
and       as        assert    break     case      const     continue
defer     div       do        downto    else      ensures   export
extern    false     for       function  if        in        let
mod       not       of        or        pass      procedure program
record    ref       repeat    requires  return    test      then
this      This      to        trait     true      try       type
unit      unsafe    until     uses      var       while     deref
_
```

### 2.7 Literals

| Kind | Examples | Notes |
|---|---|---|
| Integer | `42`, `1_000_000`, `0xFF`, `0b1010`, `0o755` | `_` as digit separator only |
| Real | `3.14`, `1.0e-9`, `2.0` | must contain `.`; `2` is never Real |
| Boolean | `true`, `false` | |
| Char | `'a'`, `'\n'`, `'\u{1F600}'` | one Unicode scalar value |
| String | `"x = {x}, sum = {a + b}\n"` | always interpolates (§2.8); escapes: `\n \t \r \0 \\ \" \' \{ \u{...}` |
| List | `[1, 2, 3]` | |
| Map | `{"a": 1, "b": 2}` | |
| Set | `{Color.Red, Color.Blue}` | |

Empty `[]` and `{}` require the expected type to be known from context (error `E0301` otherwise).

### 2.8 String interpolation
Every string literal is a template. There is no separate interpolated-string syntax and no
string concatenation operator: building a string has exactly one form.

```pascal
let msg := "{item.name}: {item.qty} left"
let json := "\{\"name\": \"{name}\"}"     # \{ is a literal brace
```

- `{expr}` inserts any expression whose type satisfies `Printable` (§4.10).
- `\{` is a literal `{`. A `}` with no open placeholder is literal and needs no escape.
- An unterminated `{`, or an empty `{}`, is an error (`E0030`).
- A string with no placeholders is a plain constant, with no runtime cost.
- A string literal, including its placeholders, must fit on one line. Use `\n` for newlines.
- Strings in `extern` library names, `as` symbol names and `test` names may not contain placeholders.

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
program Name          # produces an executable; may contain top-level statements
unit Name             # library; declarations only
```

The name must match the file name (`Name.pst`).

### 3.2 Top-level layout
After the header, in any order (and, in programs, interleaved with statements, §3.3):

- `uses A, B, C`: at most once, directly after the header
- `type` sections
- `const` sections
- `function` / `procedure` declarations
- `extern` blocks
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
- The `System` unit (core types, `Option`, `Result`, `len`, etc.) is implicitly used.
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
the compiler implements are declared like C functions, in `extern "pst"` blocks, which are
allowed only in standard units (`E0106`):

```pascal
extern "pst"
    ## Number of elements.
    export function len[T](xs: List[T]): Integer
```

`export` on a declaration inside an `extern` block exports it, as for routines.

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
    Meters = Real                 # alias: fully interchangeable with Real
    Percent = 0..100              # subrange of Integer
    Digit = '0'..'9'              # subrange of Char
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
        label: String := ""      # field default
```

- **Value type.** Assignment copies.
- Construction names every field without a default: `Point(x: 1.0, y: 2.0)`.
- A record with exactly one field may be constructed positionally: `Some(5)`.
- A record with zero fields is used as a value by its bare name: `None`.
- Records are stored like strings (§11), so C can't use them. Records declared in an
  `extern` block are C structs instead (§10.5).
- Records cannot contain functions. Behavior is provided by free functions (§6.6).

**Field visibility.** Fields are private to the declaring unit unless marked `export`:

```pascal
type
    export SortedList = record
        export count: Integer        # readable and writable from other units
        items: List[Integer]         # only this unit's functions can touch it
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
  `byteLen(s)` is the UTF-8 size. There is no `len(s)`. `len` is only for collections
  with O(1) indexing.
- `bytes(s)` returns a copy of the UTF-8 bytes as a `List[Byte]`, with real O(1) indexing
  for parsers.
- **Positions are byte offsets.** `find(s, sub, start := 0): Option[Integer]` returns one, and
  `slice(s, from, to)` takes them. An offset that isn't on a `Char` boundary panics (`P0006`).

### 4.6 Value semantics everywhere (except `ref`)
`String`, records, unions, `List`, `Map` and `Set` behave as **values**: assignment and parameter
passing produce logically independent copies. They are implemented as refcounted buffers
with **copy-on-write**, so copies are O(1) until mutated.

A `List` is one block holding its length, its capacity and its elements; the compiler
provides only that block, indexing, `len` and an internal `resize`. `add`, `insert`,
`remove`, `removeLast`, `concat`, `sort` and `in` are ordinary Pasthon routines in System,
built on those. Growing at least doubles the capacity, so `add` is amortized O(1).

- `insert(var xs, index, x)` puts `x` at `index` (`0 <= index <= len(xs)`, else `P0001`),
  moving the elements from there up.
- `sort(var xs)` sorts a list of an `Ordered` type ascending; `sort(var xs, less: f)` sorts
  by a function `f(a, b): Boolean` that says whether `a` goes before `b`. Both are stable
  merge sorts, O(n log n).

A `Map` is a record in System holding lists of keys, values and hashes, in insertion
order, plus an open-addressing slot table that points into them. The compiler provides
only the `{k: v}` and `{}` literals, `m[k]`, `k in m`, and an internal `hash`; everything
else is Pasthon in System. `Map(...)` can't be written outside System (`E0306`).

- `m[k]` reads a value and panics `P0008` if the key is missing; `m[k] := v` inserts or
  overwrites; `m[k]` is also a `var` argument (`inc(var m[k])`), which panics `P0008`
  for a missing key.
- `k in m`, `len(m)`, `remove(var m, k)` (no-op for a missing key), `get(m, k): Option[V]`,
  `getOr(m, k, default)`. All are O(1) on average.
- Iterating gives keys (`for k in m`) or entries (`for k, v in m`) in insertion order;
  overwriting keeps a key's place, removing and re-adding moves it to the end.
- `=` compares contents regardless of order.
- `remove` leaves a tombstone; the table is rebuilt (and compacted) when it is 3/4 full.

A `Set` is a record in System holding 256 bits, one per possible element: Boolean, Byte,
Int8, an enum, or a subrange with at most 256 values (`Set[Char]` and `Set[Integer]` are
`E0700`; use a subrange like `'a'..'z'`, or a `Map`). Element `x` is bit `ord(x)` minus
the `ord` of the type's first value. The compiler provides the `{a, b}` and `{}` literals,
`x in s`, and the element-to-bit mapping; the rest is Pasthon in System, on `Bits`.

- `add(var s, x)`, `remove(var s, x)` (no-op if absent), `len(s)`, `x in s`.
- `union(a, b)`, `intersection(a, b)`, `difference(a, b)` (in `a`, not in `b`).
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

### 4.11 Function types

```pascal
type
    BinOp = function(Integer, Integer): Integer
    Callback = procedure(String)
```

Values of function type are **named top-level functions** only (no closures/lambdas in v0.1).
A generic function must be instantiated to be used as a value: `apply(max[Integer])`.

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
    Iterator[T] = trait                       # in System
        function next(var this): Option[T]
```

- An iterator is an ordinary record holding its state, plus a `next` function. Satisfaction
  is structural, as for every trait.
- A type is **iterable** if a visible function `iter(c: C): I` exists where `I` satisfies
  `Iterator[T]`. Every iterator is iterable as itself.
- `for x in e do <body>` means:
  ```pascal
  var it := iter(e)          # or a copy of e, if e is already an Iterator
  while true do
      case next(var it) of
          Some(s):
              let x := s.value   # x is a fresh let binding
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
        text: String        # refcounted copy, O(1)
        pos: Integer        # byte offset

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
| `map(it, f)` | `f(x)` for each `x` |
| `filter(it, pred)` | `x` where `pred(x)` |
| `take(it, n)` / `skip(it, n)` | first `n` / all but first `n` |
| `zip(a, b)` | `Pair[A, B]` (fields `first`, `second`) until either ends |
| `enumerate(it)` | `Indexed[T]` (fields `index`, `value`) |
| `chars(s)` | `Char`s of a string |

**Consumers:** `collect(it): List[T]`, `count`, `sum`, `any(it, pred)`, `all(it, pred)`,
`find(it, pred): Option[T]`.

In v0.1, `f` and `pred` are named functions (`map(xs, double)`). v0.2 lambdas (§18a) make
them inline: `map(xs, x => x * 2)`.

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
var count := 0               # mutable, type inferred
var total: Real := 0.0       # mutable, explicit type
let name := "bob"            # immutable binding
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
    requires <bool-expr>          # zero or more, first lines of body
    ensures <bool-expr>           # zero or more, after requires
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
  `var` places, left to right. So in `add(var p.items, len(p.items))` the length is read
  before `p.items` is bound.
- A `var` argument that is a whole variable is passed by reference. Any other place (a
  field `p.x`, an element `xs[i]`) is copied in before the call and copied back after it.
  Where no program can tell the difference, the compiler *moves* the value out instead
  of copying it. That is unless another `var` place of the call uses its variable, or
  the place is in a box (`r.items` with `r: ref T`) that the callee may reach through
  an argument: one whose type can hold a `ref` or `Weak` to that box's type. Without
  mutable globals, arguments are the only way in. So `add(var p.items, x)` and
  `add(var p.items, Item(at: len(p.items)))` are O(1), not a copy of the list.
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
  copyFile("a.txt", dst: "b.txt")     # ok
  copyFile("a.txt", "b.txt")          # E0530: 'dst' has the same type as 'src'; name it

  function max[T: Ordered](a, b: T): T
  max(x, y)                           # ok: a and b are grouped, so interchangeable
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
  so the chain stays flat and a single `# end if` closes it. Writing `else` followed by an
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
for i := 0 to n - 1 do        # inclusive bounds, step +1
    ...
for i := n - 1 downto 0 do    # inclusive bounds, step -1
    ...
for item in items do          # any iterable (§4.14)
    ...
for key, value in map do      # Map only
    ...
```
- The loop variable is a fresh `let` binding scoped to the body. `for ... in` is defined by
  the iterator protocol (§4.14).
- The bounds of `to`/`downto` are evaluated once.

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
  The binding is optional: `None: return 0`.

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
        sum := sum + x.area()     # bound member: resolved where totalArea[T] is instantiated
    return round2(sum)            # not a bound member: resolved here, in Geometry

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
    return Pi * c.r * c.r         # visible here, so Circle satisfies Measurable here

writeln("{totalArea([Circle(r: 1.0), Circle(r: 2.0)])}")
```

`round2` is not exported from `Geometry` and is not visible in `Shapes`, but the
instance `totalArea[Circle]` still uses it, because it was resolved at the definition site.

```pascal
function largest[T: Ordered](xs: List[T]): Option[T]
    if len(xs) = 0 then
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
| 1 | `or` | Boolean |
| 2 | `and` | Boolean |
| 3 | `not` | Boolean |
| 4 | `=` `<>` `<` `<=` `>` `>=` `in` | non-associative: `a < b < c` is an error |
| 5 | `+` `-` | numeric only (strings: §2.8; lists: `add`, `concat`) |
| 6 | `*` `/` `div` `mod` | |
| 7 | unary `-`, `try` | |
| 8 | postfix: call `()`, index `[]`, field `.` | |

- `/` works on Real only. `div` and `mod` work on integers only. `div` truncates toward
  zero and `a mod b` takes the sign of `a`, so `a = (a div b) * b + a mod b`:
  `-7 div 2 = -3`, `-7 mod 2 = -1`. `x mod -1` is `0`; `Integer.min div -1` overflows.
- Division by zero panics (`P0004`), in `--unchecked` builds too.
- `and`, `or`, `not` work on Boolean only (short-circuit). Bitwise ops are library functions in the `Bits` unit (§14).
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
  the whole right-hand side of `:=` / `var` / `let`, or as the whole operand of `return`
  (`E0804`).
- **Prefer the expression.** A statement `if`/`case` whose every branch only assigns the
  same variable gets warning `W0801` from `pst check`, with a fix that rewrites it to the
  expression form.

---

## 10. C interoperability

### 10.1 extern blocks
```pascal
extern "libc.so.6"
    function strlen(s: CString): CSize
    function printf(fmt: CString, ...): CInt
    procedure free(p: Pointer)

extern "libm.so.6"
    function cos(x: CDouble): CDouble
```
- Every library becomes a `DT_NEEDED` entry in the output ELF. Symbols are bound at load time through the GOT (no PLT, no lazy binding).
- `as "symbol"` binds a declaration to a C symbol with a different name. Without `as`, the
  Pasthon name is the symbol name. Use it for symbols that aren't valid Pasthon names
  (leading `_`, or casing that breaks `E0020`):

  ```pascal
  extern "libc.so.6"
      procedure exitNow(code: CInt) as "_exit"
      function errnoLocation(): Ptr[CInt] as "__errno_location"

  extern "libSDL2-2.0.so.0"
      function sdlInit(flags: CUInt): CInt as "SDL_Init"
  ```
  The symbol string may not contain placeholders.
- `...` (C varargs) is allowed only in extern declarations.
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
where C expects a function pointer. C may call it any number of times while the program
runs.

### 10.4 unsafe
Dereferencing `Ptr[T]`/`Pointer`, pointer arithmetic (`p + n`, in units of `T`), `cast[T](x)`
and `addr(var x)` are allowed only inside functions declared `unsafe function` /
`unsafe procedure`. Calling an unsafe
function requires no marker. Unsafe code is greppable, not contagious.


### 10.5 C records
A record declared in a `type` section inside an `extern` block is a **C record**: it has
exactly the layout of the C struct (fields in declaration order, C alignment rules). A
C record value holds those bytes; like other records, it is a value: assignment copies it
(copy-on-write, §4.6).

```pascal
extern "libSDL2-2.0.so.0"
    type
        export SdlRect = record
            x: CInt
            y: CInt
            w: CInt
            h: CInt
    function sdlFillRect(s: Ptr[SdlSurface], r: Ptr[SdlRect], color: CUInt): CInt as "SDL_FillRect"

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
- v0.1 code generator limits (`E0990`): C records are passed and returned by value only up
  to 16 bytes; a C call takes at most 6 register arguments; callbacks can't take C records
  by value; `Real`/`CDouble`/`CFloat` aren't supported in C calls.

---

## 11. Memory model

- **Automatic reference counting.** `String`, records, unions, `List`, `Map`, `Set` and `ref T` are refcounted.
- Increments and decrements are inserted by the compiler. Destruction is deterministic: memory is freed as soon as the last owner goes away.
- **No cycle collector.** Strong cycles leak, so use `Weak[T]`. A checked build counts the
  heap blocks it allocates and frees; if any are still alive when the program (or a test
  block) reaches its end, it prints `leak: N blocks still alive at exit` to stderr and exits
  with status 70. `halt`, `OS.exit` and panics end the program without this check.
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
- `spawn` takes a named top-level function (or a v0.2 lambda that only captures `Sendable`
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
- `pst test PATH [--filter text] [--unchecked] [--json]` runs the tests of a file (a program or
  a unit), or of each `.pst` file directly in a directory, in name order. Each file is checked
  and built on its own, with the units it uses; their tests don't run. A program's body
  doesn't run either.
- Each test runs in its own process: test block number *i* of the file (counting from 0) is
  run by the test executable with argument *i*. A test passes if its process exits with
  status 0, so a panic or a `halt` with another status fails it. `--filter` runs only the
  tests whose name contains the text.
- Output, on standard output: one line per test, `ok   Shop.pst:10: double doubles` or
  `FAIL Shop.pst:13: half is wrong`; after a failure, what the test wrote to standard output
  and standard error, indented by 4 spaces, and `exit status N` unless it panicked (status
  70); then `P passed, F failed`. Diagnostics go to standard error. The exit status is 1 if a
  test failed or a file didn't build, else 0.
- With `--json`, diagnostics and tests are printed as one JSON object per line, without the
  summary. A test's object is its block's position (§15.1), `"test"` (its name), `"passed"`
  and `"output"`: `{"file": "Shop.pst", "line": 13, "col": 1, "endLine": 15, "endCol": 23,
  "test": "half is wrong", "passed": false, "output": "panic P0020: ..."}`.

---

## 14. Standard units (v0.1 scope)

| Unit | Contents |
|---|---|
| `System` (implicit) | core types, `Option`, `Result`, `Iterator`, `iter`, adapters (including `chars`) and consumers (§4.14), `len`, `add`, `insert`, `concat`, `remove`, `removeLast`, `sort`, `inc`, `dec`, `ord`, `new`, `weak`, `toString`, conversions, `halt`, `assert` support |
| `IO` | `write`, `writeln` (standard output), `writeErr`, `writelnErr` (standard error), `readln(): Option[String]`, `read(count): Option[String]` (exactly `count` bytes, `None` if the input ends first); files: `open` (reading), `create(path, mode := 0o644)` (writing; creates or empties), `close`, `readAll`, `readBytes`, `writeAll`, `writeBytes` returning `Result[_, String]` with C's error message. A `File` closes when its last reference goes away. Written in Pasthon on libc's `open`, `read`, `write`, `close` |
| `Strings` | `bytes`, `fromBytes` (invalid UTF-8 becomes U+FFFD), `charCount`, `byteLen`, `slice`, `find` (byte offsets), `split`, `join`, `trim`, `replace`, `startsWith`, `endsWith`, `parseInt`, `parseReal`. `split` and `replace` require a non-empty separator. Written in Pasthon on libc's `memmem` and `memcpy`, except `charCount`, `byteLen`, `slice` and `startsWith` (compiler intrinsics) |
| `Math` | `Pi`, `sqrt`, `sin`, `cos`, `abs`, `min`, `max`, `floor`, `ceil` |
| `Bits` | `band`, `bor`, `bxor`, `bnot`, `shl`, `shr`, `popCount` on all integer types (compiler intrinsics); results wrap to the type's width, `shr` shifts in zeros, a shift count outside 0 to width - 1 gives 0 |
| `OS` | `args(): List[String]` (without the program name), `env(name): Option[String]`, `exit(code)`, `currentDir(): String`, `realPath(path): Result[String, String]` (absolute, with symbolic links, `.` and `..` resolved), `isFile(path): Boolean` (a regular file, following symbolic links), `isDir(path): Boolean`, `listDir(path): Result[List[String], String]` (the entry names, without `.` and `..`, in no particular order), `programPath(): String` (the running executable, from `/proc/self/exe`), `tempDir(): Result[String, String]` (a new private directory under `TMPDIR`, or `/tmp`), `makeDir(path, mode := 0o755)`, `removeFile(path)` and `removeDir(path)` (an empty directory; all three `Result[Integer, String]`), `run(path, args, output := "", errors := "", input := "", dir := ""): Result[Integer, String]` (runs an executable and waits: its exit status, or minus the signal that ended it; 127 if it can't be started; with `output`, its standard output goes to that file, and its standard error too unless `errors` names another file; with `input`, it reads that file; with `dir`, it runs in that directory, while the file paths stay relative to the caller's). Written in Pasthon on libc (`getcwd`, `realpath`, `stat`, `readlink`, `mkdtemp`, `mkdir`, `unlink`, `rmdir`, `opendir`, `readdir`, `fork`, `dup2`, `chdir`, `execv`, `waitpid`); `args` reads `argv`, which the program's entry point keeps in a writable word after the GOT (§15.2) |
| `Json` | `JValue = JNull \| JBool \| JNumber \| JString \| JArray \| JObject` (an object keeps its `Member`s in order; a number keeps its text), `parse(s): Result[JValue, String]` (RFC 8259; the error gives the character offset), `toText(j)` (one line, no spaces), `quote(s)` (a JSON string literal), and readers that never fail: `field(j, key)` (`JNull` if missing), `isNull`, `asString`, `asBoolean`, `asInteger` (`Option`s), `items` (empty if not an array). Written in Pasthon |
| `Threads` | reserved for §11.2; not in v0.1 |
| `C` | C interop types, `toCString`, `fromCString`, `fromCBytes`, `isNull`, `addr`, `cast` |
| `DateTime` | planned, not in v0.1: dates and times modeled on Joda-Time. Immutable values `Instant`, `LocalDate`, `LocalTime`, `LocalDateTime`, `DateTime` (with a time zone), `Duration`, `Period`, `Interval`. Written in Pasthon on libc's time functions |

---

## 15. Toolchain

| Command | Purpose |
|---|---|
| `pst build Main.pst [-o out] [--unchecked]` | compile to an ELF executable |
| `pst run Main.pst [-- args]` | build to a temp dir and run |
| `pst test PATH [--filter text]` | run `test` blocks (§13) |
| `pst check [path] --json` | type-check only, emit diagnostics |
| `pst fmt PATH [--check]` | canonical formatter (§15.3) |
| `pst lsp` | language server over stdio, for human editors (§15.4) |
| `pst mcp` | MCP server over stdio, for AI agents (§15.7) |
| `pst query <request> --json` | one-shot semantic queries for scripts and AI agents (§15.5) |
| `pst index [path] --json` | dump the whole project's symbol index (§15.5) |
| `pst tokens FILE` / `pst ast FILE` | debugging dumps of the lexer and parser |

`compiler/Pst.pst` is the tool, written in Pasthon. It has `check`, `build`, `run`, `test`,
`fmt`, `lsp`, `tokens` and `ast` so far, and finds the standard units in `units` next to its executable.
`tokens` and `ast` print their own text format, one token or node per line, then the
diagnostics; they exit with status 1 if there are errors.

`scripts/bootstrap.sh` builds the tool into `build/pst` (with `build/units`). The seed is
`$PST_SEED` if set, else the latest GitHub release. The seed builds stage 1, stage 1 builds
stage 2, and the two must be identical. A tag `v*` makes the release workflow publish
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
   dynamic section, a GOT for extern symbols, writable words for the runtime after it, the
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
- Comments stay on their line. A trailing comment gets two spaces before `#`. A comment on
  its own line is indented like the next line of code, or deeper (up to the previous line's
  depth) if it was written deeper, so a comment closing a block stays in it.
- Existing end markers are preserved. The formatter never adds or removes them.
- A line longer than 100 columns breaks inside brackets: the widest group, preferring a
  `(` group, is printed one item per line, one level deeper, each item followed by a comma,
  and the closer on its own line. If its last item is itself a group taking more than half of
  it, that group breaks instead and hugs the outer brackets (`writeln(join(` ... `))`).
  This repeats until the line fits or nothing more can break. A group only breaks if the
  text before and after it on the line each fit and are no wider than its contents, and a
  group of one item with no groups or `and`/`or` inside only if that item fits on its own
  line; otherwise the line stays long.
- A parenthesized expression gets no trailing comma; without commas, it breaks before each
  `and` and `or` at its own level instead.
- A group stays broken if it ends with a trailing comma, holds a comment, or holds a broken
  group that isn't its last item. To keep a short list one item per line, end it with a
  comma; to join it, remove the comma.

### 15.4 Language server (`pst lsp`)
Intended for human editors. (AI agents should use `pst mcp` (§15.7) or `pst query` (§15.5).)
Built into the `pst` executable, so it is always the same version as the compiler and uses
the same front end. JSON-RPC over stdio, LSP 3.17. It has no dependencies beyond the compiler.

So far it synchronizes whole documents (`textDocumentSync` 1), pushes diagnostics, formats,
and answers completion, hover, signature help, definition, type definition, references, rename, code actions, document
symbols, workspace symbols and folding ranges. After each change it checks
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
  else, followed by the `##` doc comment.
- Signature help finds the innermost unclosed `(` before the position whose callee is a
  name, from the document's tokens, so it works while the call is unfinished. It shows each
  routine (or record, for construction) the name may mean, with parameter types, `var`
  markers and defaults, and marks the argument at the position: by name if it is named,
  else by position, counting the receiver of `a.f(...)` as the first.
- Definition gives the defining name, one per overload the name may still mean.
- Type definition gives the declarations of the types in the type of the name at the
  position: of a local, constant, field or enum member, or what a function returns. The
  type's arguments and `ref` targets count too, so `List[ref Item]` leads to `Item`.
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
- Folding ranges fold each line followed by more deeply indented lines, up to the last of
  them; blank lines don't end a fold.

Other requests get the error `-32601`. `exit` ends with status 0 after `shutdown`, else 1,
as does the end of the input. The table below is the full plan.

| LSP feature | Pasthon behavior |
|---|---|
| Diagnostics (push) | Same codes, messages, and fixes as `pst check` |
| Code actions | Every diagnostic `fix` becomes a quick fix |
| Hover | Declared or inferred type, `##` doc comment, contracts (`requires`/`ensures`); for generics, the instance at that position |
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
(same convention as §15.1).

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
| `pst query instances largest` | every generic instance and where each is created |
| `pst query explain E0502` | error code documentation |

`pst index --json` writes the complete symbol index (§15.6) in one document, so an agent
can load the whole project's structure in one call instead of opening files.

All query output has a stable, versioned schema (`"schema": "pst-query/1"`).

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
| `pst_test` | `paths?`, `filter?` | per-test pass/fail, with assertion messages and positions |
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
| `pst_accepts` | `type` | visible functions whose first parameter accepts that type, i.e. what `x.` can call (§6.6) |
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

externBlk   = "extern" STRING NEWLINE INDENT { externDecl NEWLINE | typeSect } DEDENT ;
externDecl  = ["export"] signature ["as" STRING] ;
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

expr        = ifExpr | orExpr ;              (* orExpr: precedence levels of §9.1 *)
ifExpr      = "if" expr "then" expr { "else" "if" expr "then" expr } "else" expr ;
caseExpr    = "case" expr "of" NEWLINE INDENT { exprArm } DEDENT ;
exprArm     = (label { "," label } | "else") ":" expr NEWLINE ;
rhs         = caseExpr | expr NEWLINE ;      (* right side of := / var / let, and return operand *)
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

The parser also reports `E0101`, `E0104`, `E0201`, `E0202`, `E0801`, `E0803`, `E0804`, and
`E0903` where they are purely syntactic.

Recovery: an error skips the rest of its line and any block indented under it, leaving an
error node, so one mistake gives one diagnostic. Syntax errors on a logical line that
already has a lexical error are not reported.

### 16.2 Name errors

| Code | Meaning |
|---|---|
| `E0020` | wrong casing for the kind of name (fix: rename the definition and its uses) |
| `E0100` | unknown name, or no such member in a unit or enum (fix: similar visible names) |
| `E0102` | unit not found, or not a unit |
| `E0103` | top-level variable of the program used inside a routine or `const` |
| `E0105` | `program` / `unit` name doesn't match the file name (fix: rename) |
| `E0106` | `extern "pst"` outside the standard units |
| `E0107` | cyclic `uses` |
| `E0108` | duplicate declaration in one unit, record, trait, enum, or type parameter list |
| `E0109` | name not exported by its unit |
| `E0110` | unqualified name ambiguous between used units (fix: qualify) |
| `E0111` | `This` outside a trait |
| `E0205` | variable declared again in the same routine |
| `E0206` | variable never used (fix: discard with `_`) |

### 16.3 Type errors

| Code | Meaning |
|---|---|
| `E0300` | type mismatch (hints: `toReal`, `round`, `Int32(...)`) |
| `E0301` | type of an empty `[]` / `{}` unknown (declare the variable's type) |
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
| `E0530` | positional argument with the same type as an earlier parameter group (fix: name it) |
| `E0601` | overlapping `case` labels |
| `E0602` | non-exhaustive `case` (enum or union), or missing `else` (other types) |
| `E0603` | invalid `case` label for the subject's type |
| `E0700` | type argument doesn't satisfy a bound, or isn't a valid `Set` element |
| `E0710` | operation on a type parameter that its bounds don't provide |
| `E0720` | generic instances nest more than 64 deep (§8) |
| `E0802` | `if` / `case` expression branches of different types |
| `E0901` | type isn't C-compatible (extern signature, C record field, C varargs argument; §10.1) |
| `E0902` | unsafe operation outside an `unsafe` routine (§10.4) |
| `E0903` | invalid declaration in an extern block's `type` section (§10.5) |
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
semicolon-separated statements, tuples (use records), closures/lambdas (v0.1),
runtime trait objects (use unions).

---

## 18. Open questions

None at the moment.

## 18a. Planned for v0.2

**Lambdas**, restricted so that the common, safe cases work and the classic bugs can't happen:

```pascal
let doubled := map(xs, x => x * 2)
let adults := filter(people, p => p.age >= 18)
sort(var people, key: p => p.name)
```

- The body is a single expression, never a block.
- Captures are **by value**: a frozen copy taken when the lambda is created. There is no
  shared loop-variable bug, and captured variables can't be mutated.
- Parameter types are inferred from the expected function type. Lambdas can't appear where
  no function type is expected.
- A lambda is `Sendable` if all its captures are.

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
    for i := 0 to len(items) - 1 do
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

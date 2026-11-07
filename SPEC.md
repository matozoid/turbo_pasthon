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
- Blank lines and comment-only lines do not affect indentation.
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
| `if` / `elif` / `else` chain | `# end if` |
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
`[A-Za-z][A-Za-z0-9]*`. Underscores are not allowed in Pasthon identifiers
(exception: names declared in `extern` blocks may contain `_`).

Naming is enforced (error `E0020`):

| Kind | Style | Example |
|---|---|---|
| Types, traits, enum members, constants, units | UpperCamel | `Point`, `MaxQty` |
| Functions, procedures, variables, parameters, fields | lowerCamel | `restock`, `itemCount` |
| Generic type parameters | UpperCamel (usually one letter) | `T`, `K`, `V` |

### 2.6 Keywords

```
and       assert    break     case      const     continue  defer
div       do        downto    elif      else      ensures   export
extern    false     for       function  if        in        let
mod       not       of        or        procedure program   record
ref       repeat    requires  return    test      then      this
This      to        trait     true      try       type      unit
unsafe    until     uses      var       while     _
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
- Strings in `extern` library names and `test` names may not contain placeholders.

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
    inc(n)

bump(var count)
writeln("{Greeting}, count = {count}")
```

### 3.4 Declaration order
All top-level declarations in a file (and in used units) are visible everywhere
in the file. No `forward` declarations are needed. Mutual recursion works.

### 3.5 Exports and imports
- A declaration prefixed with `export` is visible to units/programs that `uses` this unit.
- Exporting a record or trait type exports all of its fields/members.
- Enum members are exported with their enum.
- `uses Strings` makes exported names available both qualified (`Strings.split`) and unqualified (`split`).
- If an unqualified name is ambiguous between units, using it is an error (`E0110`); qualify it.
- The `System` unit (core types, `Option`, `Result`, `writeln`, `len`, etc.) is implicitly used.

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
| `String` | immutable UTF-8 text, refcounted |

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
- Fields are laid out in declaration order with C alignment rules, so a record whose
  fields are all C-compatible is layout-identical to the C struct.
- Records cannot contain functions. Behavior is provided by free functions (§6.6).

### 4.5 Arrays, lists, maps, sets

| Type | Semantics |
|---|---|
| `Array[T, N]` | fixed length `N` (compile-time constant), inline value, C-compatible |
| `List[T]` | growable, 0-based |
| `Map[K, V]` | hash map; `K` must satisfy `Hashable` |
| `Set[T]` | `T` must be an ordinal type with at most 256 values; stored as a bitset |

All indexing is 0-based and bounds-checked in checked builds (panic `P0001`).

### 4.6 Value semantics everywhere (except `ref`)
`String`, `List`, `Map` behave as **values**: assignment and parameter passing
produce logically independent copies. They are implemented as refcounted buffers
with **copy-on-write**, so copies are O(1) until mutated.

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
- Layout: tag (UInt32) followed by storage for the largest variant.

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
- Whole-value access uses `^`: `n^` (read), `n^ := other` (write).
- `=` on two refs compares **identity**. Compare contents with `a^ = b^`.
- A `let` binding of a ref can't be rebound, but the pointee can still be mutated.
- `Weak[T]` holds a non-owning reference: `weak(r)` creates it, `w.get(): Option[ref T]`.
  Use it to break cycles; strong reference cycles leak.

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

Built-in traits in `System`: `Equatable` (`=`), `Ordered` (`<` etc.), `Hashable`,
`Printable` (`toString(this): String`), `Copyable`.

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

`Ptr`/`Pointer` are unmanaged and can be dereferenced only inside functions marked `unsafe` (§10.4).

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

### 5.2 Local variables
```pascal
var count := 0               # mutable, type inferred
var total: Real := 0.0       # mutable, explicit type
let name := "bob"            # immutable binding
```
- Every variable **must** be initialized at declaration (`E0201`).
- `let` bindings can't be reassigned or passed as `var` arguments.
- Shadowing a variable from an enclosing scope in the same function is an error (`E0205`).
- Declaring a variable and never using it is an error (`E0206`). Remove it.

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

### 6.3 Call-site `var`
An argument passed to a `var` parameter must be marked `var` at the call site:

```pascal
swap(var a, var b)
```

The receiver in UFCS method-call syntax is exempt: `p.shift(1.0)`.

### 6.4 Named arguments
- Any argument may be passed by name: `restock(item, amount: 5)`.
- Positional arguments must come before named ones.
- Each parameter is bound exactly once.

### 6.5 Contracts
- `requires` checks run on entry; `ensures` checks run on every return.
- In `ensures`, `result` is the return value, and `old(expr)` is `expr` evaluated on entry.
- A violation panics with `P0010` / `P0011` in checked builds. Contracts are removed with `--unchecked`.

### 6.6 Uniform function call syntax (UFCS)
`a.f(b, c)` is exactly `f(a, b, c)`. Resolution for `a.f`:
1. If `a` has a field named `f`, it is field access (calling it if `f` has a function type).
2. Otherwise it is a call to function `f` with `a` as the first argument.

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
r^ := expr
```
There are no compound operators (`+=`). Use `inc(x)`, `inc(x, n)`, `dec(x)`, `dec(x, n)` for counters.

### 7.2 if
```pascal
if x > 0 then
    writeln("pos")
elif x < 0 then
    writeln("neg")
else
    writeln("zero")
```
Bodies always start on a new line.

### 7.3 while
```pascal
while i < n do
    inc(i)
```

### 7.4 repeat ... until
```pascal
repeat
    line := readln()
until line = "quit"
```
`until` sits at the same indentation as `repeat` and closes the block.

### 7.5 for
```pascal
for i := 0 to n - 1 do        # inclusive bounds, step +1
    ...
for i := n - 1 downto 0 do    # inclusive bounds, step -1
    ...
for item in items do          # List, Array, Set, Map (yields keys), String (yields Char), enum type
    ...
for key, value in map do      # Map only
    ...
```
- The loop variable is a fresh `let` binding scoped to the body.
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

### 7.7 break / continue / return
As usual. `break`/`continue` apply to the innermost loop.

### 7.8 defer
```pascal
let f := try open(path)
defer close(var f)
```
Runs at scope exit in reverse order, on every path, including `return` and `try`
propagation (not on panic). Takes a single statement or an indented block.

### 7.9 Discarding results
`_ := expr` evaluates and drops a value. Required for ignored function results.
`_` is the only identifier containing an underscore. It can't be read, only assigned,
and can also appear as a `case` binding: `Circle(_): ...`.

### 7.10 assert
`assert cond` or `assert cond, "message"`. Panics with `P0020`. Always active (also in `--unchecked`).

---

## 8. Generics

- Type parameters: `[T]`, bounded `[T: Trait]`, multiple bounds `[T: A + B]`, constants `[N: Integer]`.
- **Instantiation is monomorphization by copying** (C++-template style): each distinct
  combination of type and constant arguments produces a full copy of the function or type,
  with the arguments substituted, which is then type-checked and compiled like ordinary code.
- **Bounds are checked up front.** The body of a generic is type-checked once against its
  bounds, so it may only use operations the bounds guarantee (`E0710`). Errors therefore
  point at the generic's definition or the call site, never deep inside an instantiation.
- Type arguments are inferred from value arguments where possible. Otherwise write them
  explicitly: `parse[Integer](s)`.
- Constant parameters may be used in type expressions and as values: `Array[T, N * 2]`, `for i := 0 to N - 1 do`.
- Recursive instantiation depth is limited to 64 (`E0720`).

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
| 8 | postfix: call `()`, index `[]`, field `.`, deref `^` | |

- `/` works on Real only. `div` and `mod` work on integers only.
- `and`, `or`, `not` work on Boolean only (short-circuit). Bitwise ops are library functions in the `Bits` unit (§14).
- Mixed numeric types are an error. Convert explicitly: `toReal(i)`, `toInt(r)` (truncates), `round(r)`, `Int32(x)` (checked narrowing).
- Integer overflow panics in checked builds (`P0002`) and wraps in `--unchecked` builds.
- `=` is structural for records, unions, lists, maps, sets, and strings, and identity for `ref`.

### 9.2 try
`try e`:
- If `e: Result[T, E]`: on `Ok(v)` the value is `v`. On `Err(x)` the enclosing function immediately returns `Err(x)`, so the enclosing function must return `Result[_, E]` with the same `E`.
- If `e: Option[T]`: on `Some(v)` the value is `v`. On `None` the function returns `None`, so it must return an `Option`.

Deferred statements run before the early return.

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
- Every library becomes a `DT_NEEDED` entry in the output ELF. Symbols are bound via PLT/GOT.
- `...` (C varargs) is allowed only in extern declarations.
- Calls use the System V AMD64 ABI.
- Parameter and return types must be C-compatible: primitive numerics, `Boolean`, C types,
  `Ptr[T]`, `Pointer`, records with only C-compatible fields, `Array[T, N]` (inside records),
  and non-generic function types (as callbacks).

### 10.2 Strings
- `toCString(s: String): CString` returns a NUL-terminated copy owned by the runtime and freed at the end of the calling scope.
- `fromCString(p: CString): String` copies the C string.

### 10.3 Callbacks
A top-level, non-generic Pasthon function whose signature is C-compatible can be passed
where C expects a function pointer.

### 10.4 unsafe
Dereferencing `Ptr[T]`/`Pointer`, pointer arithmetic (`p + n`), and `cast[T](x)` are allowed
only inside functions declared `unsafe function` / `unsafe procedure`. Calling an unsafe
function requires no marker. Unsafe code is greppable, not contagious.

---

## 11. Memory model

- **Automatic reference counting.** `String`, `List`, `Map`, `ref T`, and generic values containing them are refcounted.
- Increments and decrements are inserted by the compiler. Destruction is deterministic: memory is freed as soon as the last owner goes away.
- **No cycle collector.** Strong cycles leak, so use `Weak[T]`.
- Records, arrays, enums, unions, and primitives live inline (stack or embedded).
- Heap allocation goes through libc `malloc`/`free`.

---

## 12. Errors and panics

- **Expected failures** (I/O, parsing, validation) are values: `Result[T, E]`.
- **Bugs** panic. A panic prints the code, message, file:line:col, and a stack trace to stderr, then exits with status 70.

| Code | Cause |
|---|---|
| `P0001` | index out of bounds |
| `P0002` | integer overflow |
| `P0003` | subrange violation |
| `P0004` | division by zero |
| `P0005` | `get` on `None` / `Err` |
| `P0010` | `requires` violated |
| `P0011` | `ensures` violated |
| `P0020` | `assert` failed |

`halt(code)` exits immediately (deferred statements do not run).

---

## 13. Tests

```pascal
test "largest of empty list is None"
    assert largest[Integer]([]).isNone()
```
- `test` blocks are top-level, in any file. They are compiled only by `pst test`.
- `pst test [file|dir] [--filter text]` runs tests and reports pass/fail, with JSON output available.
- Tests can call non-exported functions of their own unit.

---

## 14. Standard units (v0.1 scope)

| Unit | Contents |
|---|---|
| `System` (implicit) | core types, `Option`, `Result`, `len`, `add`, `concat`, `remove`, `inc`, `dec`, `ord`, `new`, `weak`, `toString`, conversions, `halt`, `assert` support |
| `IO` | `write`, `writeln`, `readln`, files: `open`, `close`, `readAll`, `writeAll` returning `Result` |
| `Strings` | `split`, `join`, `trim`, `find`, `replace`, `startsWith`, `parseInt`, `parseReal` |
| `Math` | `Pi`, `sqrt`, `sin`, `cos`, `abs`, `min`, `max`, `floor`, `ceil` |
| `Bits` | `band`, `bor`, `bxor`, `bnot`, `shl`, `shr`, `popCount` on all integer types (compiler intrinsics) |
| `OS` | `args(): List[String]`, `env(name): Option[String]`, `exit` |
| `C` | C interop types, `toCString`, `fromCString` |

---

## 15. Toolchain

| Command | Purpose |
|---|---|
| `pst build Main.pst [-o out] [--unchecked]` | compile to an ELF executable |
| `pst run Main.pst [-- args]` | build to a temp dir and run |
| `pst test [path]` | run `test` blocks |
| `pst check [path] --json` | type-check only, emit diagnostics |
| `pst fmt [path] [--check]` | canonical formatter |

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

### 15.2 Compilation model
1. **Lex + parse** every file into an AST. Indentation becomes tokens.
2. **Declaration pass**: collect all top-level names per unit (this is why `forward` isn't needed).
3. **Check + emit pass**: type-check each function, instantiate generics on demand, and emit
   x86-64 machine code directly with a simple stack-based expression scheme. No IR, no optimizer.
4. **Link**: the compiler writes the ELF itself (`PT_INTERP` = `/lib64/ld-linux-x86-64.so.2`,
   dynamic section, PLT/GOT for extern symbols, the runtime embedded as precompiled code).

Checked build (default): bounds, overflow, subrange, contracts. `--unchecked` drops all of
them except `assert`.

### 15.3 Formatter rules
- 4-space indent, max line length 100, one statement per line, no semicolons.
- Single spaces around binary operators and `:=`. No space before `:` in declarations, one after.
- Long calls and literals break inside brackets, one item per line, with a trailing comma.
- Existing end markers are preserved. The formatter never adds or removes them.

---

## 16. Grammar (EBNF sketch)

`NEWLINE`, `INDENT`, `DEDENT` come from the lexer. `block = NEWLINE INDENT stmt+ DEDENT`.

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
field       = IDENT ":" typeExpr [":=" constExpr] NEWLINE ;
typeExpr    = IDENT [typeArgs] | "ref" typeExpr
            | ("function" | "procedure") "(" [typeExpr {"," typeExpr}] ")" [":" typeExpr] ;
typeParams  = "[" typeParam { "," typeParam } "]" ;
typeParam   = IDENT [":" IDENT { "+" IDENT }] ;
typeArgs    = "[" (typeExpr | constExpr) { "," (typeExpr | constExpr) } "]" ;

constSect   = "const" NEWLINE INDENT { ["export"] IDENT "=" expr NEWLINE } DEDENT ;

routine     = ["export"] ["unsafe"] signature routineBody ;
signature   = ("function" | "procedure") IDENT [typeParams]
              "(" [param { "," param }] ")" [":" typeExpr] ;
param       = ["var"] ( "this" | IDENT ":" typeExpr [":=" expr] ) | "..." ;
routineBody = NEWLINE INDENT { "requires" expr NEWLINE } { "ensures" expr NEWLINE }
              stmt { stmt } DEDENT ;

externBlk   = "extern" STRING NEWLINE INDENT { signature NEWLINE } DEDENT ;
testBlk     = "test" STRING block ;

stmt        = varDecl | assign | callStmt | ifStmt | whileStmt | repeatStmt
            | forStmt | caseStmt | deferStmt | "break" NEWLINE | "continue" NEWLINE
            | "return" [expr] NEWLINE | "_" ":=" expr NEWLINE
            | "assert" expr ["," expr] NEWLINE ;
varDecl     = ("var" | "let") IDENT [":" typeExpr] ":=" expr NEWLINE ;
assign      = lvalue ":=" expr NEWLINE ;
ifStmt      = "if" expr "then" block { "elif" expr "then" block } ["else" block] ;
whileStmt   = "while" expr "do" block ;
repeatStmt  = "repeat" block "until" expr NEWLINE ;
forStmt     = "for" IDENT ":=" expr ("to" | "downto") expr "do" block
            | "for" IDENT ["," IDENT] "in" expr "do" block ;
caseStmt    = "case" expr "of" NEWLINE INDENT { caseArm } DEDENT ;
caseArm     = (label { "," label } | "else") ":" (simpleStmt | block) ;
label       = constExpr [".." constExpr] | IDENT ["(" IDENT ")"] ;
deferStmt   = "defer" (simpleStmt | block) ;
```

The parser can't tell `name[...]` (index) from `name[...]` (explicit instantiation)
syntactically. It produces one `IndexOrInstantiate` node, resolved in the check pass.

---

## 17. Deliberately excluded

Classes, inheritance, exceptions, null, implicit numeric conversions, operator overloading,
macros, mutable globals, `goto`, `with`, compound assignment, chained comparisons,
semicolon-separated statements, tuples (use records), closures/lambdas (v0.1),
runtime trait objects (use unions).

---

## 18. Open questions

1. **Closures/lambdas**: would make `map`/`filter` practical. They need captured environments, which are refcounted, so this fits the memory model. Defer to v0.2?
2. **Field privacy**: currently exported types export all fields. Add per-field `export`?
3. **Destructors**: should records holding C resources get a `destroy` hook called at last release, or is `defer` enough?
4. **Concurrency**: out of scope for v0.1.
5. **Named-argument enforcement**: require names when several parameters share a type (e.g. `copy(src, dst)`)? Right now this is optional.
6. **Strings**: `s[i]` returns `Byte` (UTF-8 unit) while `for c in s` yields `Char`. Is that acceptable, or should indexing be removed in favor of explicit `bytes(s)[i]`?

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
    case item.status of
        Active:       return "{item.name}: {item.qty}"
        Discontinued: return "{item.name}: gone"
        Backorder:    return "{item.name}: waiting"

function names[T: Named](xs: List[T]): String
    var parts: List[String] := []
    for x in xs do
        parts.add(x.name)
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

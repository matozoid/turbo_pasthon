Goals:

 - simple to reason about
   - one obvious way: one syntax per construct, canonical unconfigurable formatter
   - nothing hidden: no implicit conversions (except injection into a union), no exceptions,
     no operator overloading, no macros; errors are values
 - no surprises at runtime
   - overflow, bounds and contract failures panic in checked builds
     (`--unchecked` wraps overflow and drops contracts)
   - no undefined behaviour outside unsafe
 - well typed
 - good error messages
   - say what is wrong and how to fix it; offer fixes where possible
 - machine-checkable
   - contracts
   - built-in tests
   - JSON diagnostics with stable error codes
   - pst query, LSP, MCP
 - Pascal syntax, but with Pythons significant indentation
 - main target is AI coding models
 - simple implementation
 - good C interop
   - Pasthon calls C libraries, and C calls back into Pasthon
   - C records: structs with exact C layout, declared in extern blocks
   - strings are passed to C without copying, but explicitly (toCString)
   - bindings written by hand; no header import
 - fast compiler inspired by Turbo Pascal
   - few passes; no forward declarations needed
   - no optimization
     - code won't be the fastest, but not slow
   - implemented in Turbo Pasthon itself
 - memory safety
   - no null
   - no manual free
   - automatic reference counting
   - value semantics
   - raw pointers only inside unsafe
 - platforms
   - Linux x86-64 first, other modern hosted platforms later; relying on libc is fine

Non-goals:

 - fast generated code: no optimization passes
 - C calling Pasthon libraries, static linking of C libraries

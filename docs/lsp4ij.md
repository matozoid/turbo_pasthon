# Using `pst lsp` in IntelliJ IDEA with LSP4IJ

[LSP4IJ](https://plugins.jetbrains.com/plugin/23257-lsp4ij) is a JetBrains plugin
that connects any language server to IntelliJ IDEA and the other JetBrains IDEs. The
Pasthon language server is part of the `pst` executable (SPEC §15.4), so you don't need
to install anything else.

## 1. Get a `pst` executable

`pst` only runs on Linux x86_64. It finds the standard units in a `units` directory next
to the executable, so keep the two together. Either:

- download `pst-linux-x86_64.tar.gz` from the latest GitHub release and unpack it. You
  get `pst` and `units/`; or
- build it from this repository with `scripts/bootstrap.sh`. You get `build/pst` and
  `build/units`, which links to `std/`.

Check that it works:

```sh
/path/to/pst check examples/Inventory.pst
```

## 2. Install LSP4IJ

Open **Settings | Plugins | Marketplace**, search for **LSP4IJ**, install it, and restart
the IDE.

## 3. Add the language server

1. Open **Settings | Languages & Frameworks | Language Servers** and click **+**.
2. On the **Server** tab:
   - **Name**: `Pasthon`
   - **Command**: `/path/to/pst lsp`. Use the absolute path, because the IDE doesn't
     start the server from your shell or with your `PATH`.
3. On the **Mappings** tab, open **File name patterns** and add a row:
   - **File name patterns**: `*.pst`
   - **Language Id**: `pasthon`
4. Leave the **Configuration** tab empty. The server has no settings.
5. Click the apply button.

If the IDE asks which file type to use when you first open a `.pst` file, choose
**Text**. The server's semantic tokens then colour the code.

## 4. Check that it runs

Open a `.pst` file. The server starts and an icon for it appears in the
**Language Servers** tool window (**View | Tool Windows | Language Servers**). Errors and
warnings from `pst check` are underlined as you type.

If nothing happens, select the server in that tool window and set **Debug | Trace** to
**verbose**. The window then logs every message between the IDE and `pst`. A server that
fails to start usually has the wrong command path, or has no `units` directory next to
the executable.

## What you get

| IDE action | LSP feature |
|---|---|
| Underlined errors and warnings, as you type | diagnostics |
| **Alt+Enter** on a diagnostic | code actions (the diagnostic's fixes) |
| **Code \| Reformat Code** | formatting (the same rules as `pst fmt`) |
| **Ctrl+Space**, or typing `.` | completion |
| Hovering, or **Ctrl+Q** | hover |
| **Ctrl+P** inside a call | signature help |
| **Ctrl+B** / **Ctrl+Click** | go to definition |
| **Ctrl+Shift+B** | go to type definition |
| **Ctrl+Alt+B** on a trait or trait routine | go to implementation |
| **Alt+F7** | find references |
| **Shift+F6** | rename |
| **Ctrl+F12**, **Structure** tool window | document symbols |
| **Navigate \| Symbol** (**Ctrl+Alt+Shift+N**) | workspace symbols |
| **Ctrl+Alt+H** | call hierarchy |
| Grey type after `let x :=`, `var x :=` and loop variables | inlay hints |
| Colours by meaning (types, fields, parameters, …) | semantic tokens |
| Fold markers in the gutter | folding ranges |

The keys are the default Windows/Linux keymap. Each feature's behaviour is described in
SPEC §15.4.

## Limits

- Before LSP4IJ 0.30.0, **Ctrl+hover** underlines the whole file in a `.pst` file
  ([lsp4ij#1854](https://github.com/redhat-developer/lsp4ij/pull/1854)). The file has no
  IntelliJ language, so it is one element as far as the IDE knows. Navigation still goes
  to the right place. From 0.30.0, the underline follows the server's semantic tokens, so
  it covers just the name.

- The server syncs whole documents and checks every open document after each change.
- References, rename, workspace symbols and implementations only see the open documents
  and the units they use. Open the files you want them to search.
- Rename refuses symbols of the standard units, units themselves (rename the file
  instead), and renames that would cause new errors.

# Using `pst lsp` in Visual Studio Code

VS Code needs an extension to start a language server. A small one is in
`editors/vscode`. It registers `.pst` files as Turbo Pasthon and starts `pst lsp` for
them. Everything else comes from the server (SPEC §15.4).

## 1. Get a `pst` executable

`pst` only runs on Linux x86_64. It finds the standard units in a `units` directory next
to the executable, so keep the two together. Either:

- download `pst-linux-x86_64.tar.gz` from the latest GitHub release and unpack it. You
  get `pst` and `units/`; or
- build it from this repository with `scripts/bootstrap.sh`. You get `build/pst` and
  `build/units`, which links to `std/`.

On Windows or macOS, run VS Code with the Remote - WSL or Remote - SSH extension, and
install the extension and `pst` on the Linux side.

## 2. Install the extension

You need Node.js and npm. In `editors/vscode`, fetch the extension's one dependency,
`vscode-languageclient`:

```sh
cd editors/vscode
npm install
```

Then do one of the following:

- **Link it** (best while you work on this repository; picks up changes after a reload):

  ```sh
  ln -s "$PWD" ~/.vscode/extensions/matozoid.pasthon-0.1.0
  ```

- **Package and install it**:

  ```sh
  npx @vscode/vsce package --allow-missing-repository
  code --install-extension pasthon-0.1.0.vsix
  ```

Restart VS Code, or run **Developer: Reload Window**.

## 3. Point it at `pst`

Open **Settings** and search for **pasthon**, or add this to `settings.json`:

```json
"pasthon.path": "/path/to/pst"
```

The default, `pst`, only works if `pst` is on the `PATH` that VS Code was started with.

## 4. Check that it runs

Open a `.pst` file. The language indicator in the status bar shows **Turbo Pasthon**, and
errors and warnings from `pst check` are underlined as you type.

If nothing happens, set `"pasthon.trace.server": "verbose"` and open
**View | Output**, then choose **Pasthon** in the list. The channel logs every message
between VS Code and `pst`, and why the server didn't start. A server that fails to start
usually has the wrong `pasthon.path`, or has no `units` directory next to the executable.

## What you get

| VS Code action | LSP feature |
|---|---|
| Squiggles and the **Problems** panel, as you type | diagnostics |
| **Ctrl+.** on a diagnostic | code actions (the diagnostic's fixes) |
| **Format Document** (**Ctrl+Shift+I**) | formatting (the same rules as `pst fmt`) |
| **Ctrl+Space**, or typing `.` | completion |
| Hovering | hover |
| Typing `(` or `,` in a call, or **Ctrl+Shift+Space** | signature help |
| **F12** / **Ctrl+Click** | go to definition |
| **Go to Type Definition** | type definition |
| **Ctrl+F12** on a trait or trait routine | go to implementation |
| **Shift+F12**, **Shift+Alt+F12** | find references |
| **F2** | rename |
| **Ctrl+Shift+O**, **Outline** view, breadcrumbs | document symbols |
| **Ctrl+T** | workspace symbols |
| **Shift+Alt+H** | call hierarchy |
| Grey type after `let x :=`, `var x :=` and loop variables | inlay hints |
| Colours by meaning (types, fields, parameters, …) | semantic tokens |
| Fold markers in the gutter | folding ranges |

The keys are the default Linux keymap. There is no TextMate grammar, so the colours come
only from semantic tokens: the editor's theme must have semantic highlighting enabled,
which is the default (`"editor.semanticHighlighting.enabled": "configuredByTheme"`).
Each feature's behaviour is described in SPEC §15.4.

## Limits

- The server syncs whole documents and checks every open document after each change.
- References, rename, workspace symbols and implementations only see the open documents
  and the units they use. Open the files you want them to search.
- Rename refuses symbols of the standard units, units themselves (rename the file
  instead), and renames that would cause new errors.

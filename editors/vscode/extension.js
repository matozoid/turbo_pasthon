// Starts `pst lsp` for .pst files.

const vscode = require("vscode");
const { LanguageClient } = require("vscode-languageclient/node");

let client;

function activate(context) {
    const path = vscode.workspace.getConfiguration("pasthon").get("path", "pst");
    const server = { command: path, args: ["lsp"] };
    client = new LanguageClient(
        "pasthon",
        "Pasthon",
        { run: server, debug: server },
        { documentSelector: [{ scheme: "file", language: "pasthon" }] }
    );
    context.subscriptions.push(client);
    client.start();
}

function deactivate() {
    return client ? client.stop() : undefined;
}

module.exports = { activate, deactivate };

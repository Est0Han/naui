"""Disposable stdio server for protocol tests, never used by the application."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from naui.lsp import encode_message, read_message


def emit(message):
    sys.stdout.buffer.write(encode_message({"jsonrpc": "2.0", **message}))
    sys.stdout.buffer.flush()


documents = {}
while True:
    message = read_message(sys.stdin.buffer)
    if message is None:
        break
    method, params = message.get("method"), message.get("params") or {}
    result = None
    if method == "initialize":
        result = {"capabilities": {"textDocumentSync": 1, "definitionProvider": True, "hoverProvider": True,
                                   "referencesProvider": True, "renameProvider": True}}
    elif method == "textDocument/didOpen":
        document = params["textDocument"]
        documents[document["uri"]] = document
    elif method == "textDocument/didChange":
        document = params["textDocument"]
        documents[document["uri"]].update(version=document["version"], text=params["contentChanges"][0]["text"])
    elif method == "textDocument/didClose":
        documents.pop(params["textDocument"]["uri"], None)
    elif method == "naui/capture":
        result = documents
    elif method == "naui/hang":
        continue
    elif method == "naui/bad-frame":
        sys.stdout.buffer.write(b"Content-Length: 9999999999\r\n\r\n")
        sys.stdout.buffer.flush()
        continue
    elif method == "exit":
        break
    if "id" in message:
        emit({"id": message["id"], "result": result})

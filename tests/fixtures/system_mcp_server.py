"""Deterministic MCP fixture; never used for real devices or research work."""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def picture():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))
    colors = [(220, 25, 35), (15, 170, 60), (30, 70, 230), (250, 220, 20)]
    rows = b"".join(b"\0" + b"".join(bytes(colors[(y >= 64) * 2 + (x >= 64)]) for x in range(128)) for y in range(128))
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", 128, 128, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")).decode()


def respond(message, tag, log):
    method = message.get("method", "")
    if log:
        with Path(log).open("a") as stream:
            stream.write(json.dumps({"method": method, "tag": tag,
                "internal_token_present": "META_RESEARCH_MCP_TOKEN" in os.environ,
                "credential_present": "FIXTURE_SECRET" in os.environ}) + "\n")
    if "id" not in message:
        return None
    result = {}
    if method == "initialize":
        result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "system-mcp-fixture", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": f"receipt_{tag}", "description": "Return the deterministic fixture receipt. Read-only.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
                            {"name": f"picture_{tag}", "description": "Return a test image for visual inspection. Read-only.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]}
    elif method == "tools/call":
        if message["params"]["name"] == f"picture_{tag}":
            result = {"content": [{"type": "image", "mimeType": "image/png", "data": picture()}]}
        else:
            result = {"content": [{"type": "text", "text": f"MCP_RECEIPT_{tag}_7f392bc1"}], "structuredContent": {"receipt": f"MCP_RECEIPT_{tag}_7f392bc1", "ok": True}}
    elif method in ("resources/list", "resources/templates/list"):
        result = {"resources" if method == "resources/list" else "resourceTemplates": []}
    elif method != "ping":
        return {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--port", type=int, default=18779)
    parser.add_argument("--tag", default="stdio")
    parser.add_argument("--log")
    parser.add_argument("--require-token")
    options = parser.parse_args()
    if options.http:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.send_response(405); self.end_headers()
            def do_DELETE(self):
                self.send_response(200); self.end_headers()
            def do_POST(self):
                if options.require_token and self.headers.get("Authorization") != "Bearer " + options.require_token:
                    self.send_response(401); self.send_header("Content-Length", "0"); self.end_headers(); return
                message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                result = respond(message, options.tag, options.log)
                self.send_response(200 if result else 202)
                self.send_header("Content-Type", "application/json")
                data = json.dumps(result).encode() if result else b""
                self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)
        ThreadingHTTPServer(("127.0.0.1", options.port), Handler).serve_forever()
    else:
        for line in sys.stdin:
            result = respond(json.loads(line), options.tag, options.log)
            if result is not None:
                print(json.dumps(result), flush=True)

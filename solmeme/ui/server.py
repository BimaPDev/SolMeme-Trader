"""Localhost server for the website and for the Bubble Tea client."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}

_FALLBACK = """<!doctype html>
<meta charset="utf-8">
<title>SolMeme</title>
<body style="font:16px/1.4 ui-sans-serif,system-ui;background:#12140f;color:#efe7d2;padding:2rem">
<p>Paper session is running. The website files are not in <code>web/</code> yet.</p>
<p><a style="color:#e6c35c" href="/api/snapshot">/api/snapshot</a></p>
</body>
"""


class Hub:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: dict = {}

    def update(self, snap: dict) -> None:
        with self._lock:
            self._snap = snap

    def get(self) -> dict:
        with self._lock:
            return self._snap


def serve(hub: Hub, directory: Path, host: str = "127.0.0.1", port: int = 8787):
    """Bind on localhost and serve until shutdown(). Returns (httpd, port)."""
    root = directory.resolve()
    handler = _handler(hub, root)
    ports = [0] if port == 0 else range(port, port + 20)
    last: OSError | None = None
    for candidate in ports:
        try:
            httpd = ThreadingHTTPServer((host, candidate), handler)
        except OSError as exc:
            last = exc
            continue
        httpd.allow_reuse_address = True
        threading.Thread(target=httpd.serve_forever, name="solmeme-ui", daemon=True).start()
        return httpd, httpd.server_address[1]
    raise OSError(f"no free localhost port from {port}") from last


def _handler(hub: Hub, root: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot":
                body = json.dumps(hub.get(), separators=(",", ":")).encode()
                self._send(200, "application/json", body)
                return
            if path in ("/", "/index.html"):
                self._file(root / "index.html", fallback=True)
                return
            rel = path.lstrip("/")
            target = (root / rel).resolve()
            if root not in target.parents and target != root:
                self._send(403, "text/plain; charset=utf-8", b"forbidden")
                return
            self._file(target, fallback=False)

        def _file(self, target: Path, *, fallback: bool) -> None:
            if target.is_file() and target.suffix.lower() in _TYPES:
                self._send(200, _TYPES[target.suffix.lower()], target.read_bytes())
                return
            if fallback:
                self._send(200, "text/html; charset=utf-8", _FALLBACK.encode())
                return
            self._send(404, "text/plain; charset=utf-8", b"not found")

        def _send(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler

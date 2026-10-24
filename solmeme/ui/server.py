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
        self.report_dir: Path | None = None    # data/reports/session/<run>: where `solmeme session` writes
        self.history_fn = None                 # () -> dict: wallet and strategy PnL over time
        self.swarm_fn = None                   # () -> dict: the strategy swarm (reads the DB)
        self.agent_fn = None                   # (key) -> dict: one strategy version's full profile
        self.narratives_fn = None              # () -> dict: themes, tweets, AI coin guesses, news
        self.token_fn = None                   # (mint) -> dict: one coin's forensics card
        self.trades_fn = None                  # () -> dict: every agent's fills and the coins they share
        self.council_chat_fn = None            # (sitting, after_id) -> dict: council sittings as chat transcripts
        self.cursor_usage_fn = None            # (fresh) -> dict: Cursor calls/tokens and the plan's real balance
        self.live_fn = None                    # () -> dict: the live dry-run wallet in full (trades, history, readiness)
        self.live_action_fn = None             # (action, why) -> dict: e.g. start a new live attempt
        self.ask_fn = None                     # (mint, note) -> dict: ask the council about one coin (ai/ask.py)
        self.asks_fn = None                    # (ask_id) -> dict: one ask, or the recent ones
        self.my_wallets_fn = None              # (action, address, note, verdict) -> dict: wallets you track
        self.whale_tape_fn = None              # () -> dict: live whale trades and coins several whales are buying
        self.team_fn = None                    # () -> dict: ResearchBot, SniperBot, TraderBot, council, Overseer
        self.research_fn = None                # () -> list: ResearchBot briefs on strategy matches (ui/workflow.py)
        self.setups_fn = None                  # () -> list: SniperBot setups one condition away
        self.permissions_fn = None             # (changes | None) -> dict: TraderBot permissions
        self._swarm: tuple[float, dict] | None = None

    def swarm(self, max_age_s: float = 10.0) -> dict:
        """The swarm view, rebuilt at most every max_age_s (it reads SQLite)."""
        import time

        if self.swarm_fn is None:
            return {}
        with self._lock:
            cached = self._swarm
        if cached and time.time() - cached[0] < max_age_s:
            return cached[1]
        data = self.swarm_fn()
        with self._lock:
            self._swarm = (time.time(), data)
        return data

    def update(self, snap: dict) -> None:
        with self._lock:
            self._snap = snap

    def get(self) -> dict:
        with self._lock:
            return self._snap


def session_report(folder: Path | None) -> dict:
    """The newest `solmeme session` report for this run and the last AI note (most reports
    carry none: the AI is only asked when something changed). Nulls when nothing was written yet."""
    out = {"markdown": None, "updated": None, "note": None, "note_at": None, "next_at": None}
    if folder is None:
        return out
    try:
        out["next_at"] = json.loads((folder.parent / "next.json").read_text(encoding="utf-8")).get("next_at")
    except (OSError, ValueError):
        pass
    try:
        latest = folder / "latest.md"
        out.update(markdown=latest.read_text(encoding="utf-8"), updated=latest.stat().st_mtime)
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        out.update(note=state.get("note"), note_at=state.get("note_at"))
    except (OSError, ValueError):
        pass
    return out


def serve(hub: Hub, directory: Path, host: str = "127.0.0.1", port: int = 8787):
    """Bind on localhost and serve until shutdown(). Returns (httpd, port)."""
    root = directory.resolve()
    handler = _handler(hub, root)
    ports = [0] if port == 0 else range(port, port + 20)
    last: OSError | None = None
    for candidate in ports:
        try:
            httpd = _Server((host, candidate), handler)
        except OSError as exc:
            last = exc
            continue
        threading.Thread(target=httpd.serve_forever, name="solmeme-ui", daemon=True).start()
        return httpd, httpd.server_address[1]
    raise OSError(f"no free localhost port from {port}") from last


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        import sys

        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, TimeoutError)):
            return                    # a phone browser closing the connection: not an error
        super().handle_error(request, client_address)


def _handler(hub: Hub, root: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            """Writes: ask the council about a coin, or start a new live attempt (needs {"confirm": true})."""
            path = self.path.split("?", 1)[0]
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 4096)) or b"{}")
            except ValueError:
                body = {}
            if path == "/api/permissions" and hub.permissions_fn is not None:
                data = hub.permissions_fn(body if isinstance(body, dict) else {})
                self._send(200, "application/json", json.dumps(data).encode())
                return
            if path == "/api/ask" and hub.ask_fn is not None:
                data = hub.ask_fn(str(body.get("mint") or body.get("address") or ""), str(body.get("note") or ""),
                                  str(body.get("kind") or "token"))
                self._send(200 if "error" not in data else 400, "application/json", json.dumps(data, default=str).encode())
                return
            if path == "/api/my-wallets" and hub.my_wallets_fn is not None:
                data = hub.my_wallets_fn(str(body.get("action") or ""), str(body.get("address") or ""),
                                         str(body.get("note") or ""), str(body.get("verdict") or ""))
                self._send(200 if "error" not in data else 400, "application/json", json.dumps(data, default=str).encode())
                return
            if path != "/api/live/new-attempt" or hub.live_action_fn is None:
                self._send(404, "application/json", b'{"error":"not found"}')
                return
            if body.get("confirm") is not True:
                self._send(400, "application/json", b'{"error":"confirm required"}')
                return
            data = hub.live_action_fn("new_attempt", str(body.get("why") or "")[:200])
            self._send(200, "application/json", json.dumps(data).encode())

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/snapshot":
                body = json.dumps(hub.get(), separators=(",", ":")).encode()
                self._send(200, "application/json", body)
                return
            if path == "/api/history":
                data = hub.history_fn() if hub.history_fn else {}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":")).encode())
                return
            if path == "/api/swarm":
                try:
                    data = hub.swarm(max_age_s=0.0 if "fresh=1" in self.path else 10.0)   # Sync button: skip the cache
                except Exception as exc:  # noqa: BLE001 - keep the page alive if the DB is busy
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":")).encode())
                return
            if path == "/api/agent":
                from urllib.parse import parse_qs, urlsplit
                key = (parse_qs(urlsplit(self.path).query).get("key") or [""])[0]
                try:
                    data = hub.agent_fn(key) if hub.agent_fn and key else {"error": "no key"}
                except Exception as exc:  # noqa: BLE001
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path in ("/api/narratives", "/api/token", "/api/trades"):
                from urllib.parse import parse_qs, urlsplit
                mint = (parse_qs(urlsplit(self.path).query).get("mint") or [""])[0]
                try:
                    if path == "/api/narratives":
                        data = hub.narratives_fn() if hub.narratives_fn else {}
                    elif path == "/api/trades":
                        data = hub.trades_fn() if hub.trades_fn else {}
                    else:
                        data = hub.token_fn(mint) if hub.token_fn and mint else {"error": "no mint"}
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/council-chat":
                from urllib.parse import parse_qs, urlsplit
                q = parse_qs(urlsplit(self.path).query)
                try:
                    after = int((q.get("after") or ["0"])[0] or 0)
                    data = (hub.council_chat_fn((q.get("sitting") or [""])[0], after)
                            if hub.council_chat_fn else {"messages": [], "sittings": []})
                except Exception as exc:  # noqa: BLE001 - keep the page alive if the DB is busy
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/live":
                try:
                    data = hub.live_fn() if hub.live_fn else {"enabled": False}
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/cursor-usage":
                try:
                    data = hub.cursor_usage_fn("fresh=1" in self.path) if hub.cursor_usage_fn else {"configured": False}
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path in ("/api/research", "/api/setups", "/api/permissions"):
                fn = {"/api/research": hub.research_fn, "/api/setups": hub.setups_fn,
                      "/api/permissions": (lambda: hub.permissions_fn(None)) if hub.permissions_fn else None}[path]
                try:
                    data = fn() if fn else []
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/team":
                try:
                    data = hub.team_fn() if hub.team_fn else {}
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/whale-tape":
                try:
                    data = hub.whale_tape_fn() if hub.whale_tape_fn else {"trades": [], "clusters": []}
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/my-wallets":
                from urllib.parse import parse_qs, urlsplit
                detail = (parse_qs(urlsplit(self.path).query).get("detail") or [""])[0]
                try:
                    data = (hub.my_wallets_fn("detail" if detail else "list", detail, "", "")
                            if hub.my_wallets_fn else {"wallets": {}})
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, default=str).encode())
                return
            if path == "/api/asks":
                from urllib.parse import parse_qs, urlsplit
                ask_id = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
                try:
                    data = hub.asks_fn(ask_id) if hub.asks_fn else {"asks": []}
                except Exception as exc:  # noqa: BLE001 - keep the page alive
                    data = {"error": str(exc)}
                self._send(200, "application/json", json.dumps(data, separators=(",", ":"), default=str).encode())
                return
            if path == "/api/session-report":
                self._send(200, "application/json", json.dumps(session_report(hub.report_dir)).encode())
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

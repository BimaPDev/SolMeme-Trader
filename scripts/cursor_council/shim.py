"""OpenAI-compatible /v1/chat/completions on top of Cursor's headless CLI (`agent -p`).

Runs in its own LXC (cursor, <cursor-host>). The council treats it like any `kind: openai` judge.
Each request becomes one `agent -p --mode ask --output-format json` run in an empty workspace:
ask mode, no --force, so the agent answers and never edits files or runs commands.
Standard library only. At most SHIM_CONCURRENCY runs at once (each CLI run peaks near 410 MB).

Every run is logged (model, tokens Cursor reports, seconds, ok) in USAGE_DB. GET /v1/usage returns
that meter, plus the account's real plan usage when CURSOR_SESSION_COOKIE is set: the same numbers
Cursor's dashboard shows, read from its internal (undocumented) /api/usage-summary. That cookie is a
full browser login, so it stays in this container and is never passed to the CLI.

Env:
  CURSOR_API_KEY         Cursor dashboard → API Keys (user API key; the CLI reads it)
  SHIM_TOKEN             shared secret; callers send `Authorization: Bearer <token>`
  SHIM_PORT              default 8088
  SHIM_DEFAULT_MODEL     used when the request's model is empty or "default" (default "auto")
  SHIM_TIMEOUT_S         per run (default 300)
  AGENT_BIN              default ~/.local/bin/agent
  SHIM_CONCURRENCY       parallel CLI runs (default 2); extra requests wait their turn
  CURSOR_SESSION_COOKIE  optional: the WorkosCursorSessionToken cookie from a cursor.com browser login
  USAGE_DB               default ~/usage.db
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.getenv("SHIM_PORT", "8088"))
TOKEN = os.getenv("SHIM_TOKEN", "")
DEFAULT_MODEL = os.getenv("SHIM_DEFAULT_MODEL", "auto")
TIMEOUT_S = float(os.getenv("SHIM_TIMEOUT_S", "300"))
AGENT = os.getenv("AGENT_BIN", str(Path.home() / ".local/bin/agent"))
WORKSPACE = Path(os.getenv("SHIM_WORKSPACE", str(Path.home() / "ws")))
USAGE_DB = Path(os.getenv("USAGE_DB", str(Path.home() / "usage.db")))
MAX_PROMPT = 100_000                     # one argv string must stay under Linux's 128 KiB limit
JSON_ONLY = ("\n\nAnswer with exactly one JSON object and nothing else: no prose, no code fences. "
             "Do not use tools, read files or run commands; everything you need is in this message.")
ACCOUNT_URL = "https://cursor.com/api/usage-summary"
ACCOUNT_CACHE_S = 300

CONCURRENCY = max(1, int(os.getenv("SHIM_CONCURRENCY", "2")))
_slots = threading.BoundedSemaphore(CONCURRENCY)
_running = [0]
_count_lock = threading.Lock()
_models: list[str] = []
_account: dict = {"ts": 0.0, "data": None}
_account_lock = threading.Lock()


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ── usage meter ────────────────────────────────────────────────────────────
def _db() -> sqlite3.Connection:
    c = sqlite3.connect(USAGE_DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL, model TEXT, ok INTEGER, seconds REAL, chars_in INTEGER, "
              "chars_out INTEGER, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, "
              "cache_write INTEGER, error TEXT)")
    return c


def record(model: str, ok: bool, seconds: float, chars_in: int, chars_out: int, usage: dict, error: str = "") -> None:
    try:
        with _db() as c:
            c.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), model, int(ok), seconds, chars_in, chars_out, int(usage.get("inputTokens") or 0),
                       int(usage.get("outputTokens") or 0), int(usage.get("cacheReadTokens") or 0),
                       int(usage.get("cacheWriteTokens") or 0), error[:200]))
    except sqlite3.Error as exc:
        log(f"usage log failed: {exc}")


def meter(since: float) -> dict:
    """Totals and per-model rows for calls since `since`."""
    with _db() as c:
        rows = c.execute(
            "SELECT model, COUNT(*), SUM(ok), SUM(seconds), SUM(input_tokens), SUM(output_tokens), SUM(cache_read) "
            "FROM calls WHERE ts>=? GROUP BY model ORDER BY SUM(input_tokens)+SUM(output_tokens) DESC", (since,)).fetchall()
    models = [dict(model=m, calls=n, ok=ok or 0, avg_s=round((s or 0) / n, 1) if n else None, input_tokens=i or 0,
                   output_tokens=o or 0, cache_read=cr or 0) for m, n, ok, s, i, o, cr in rows]
    total = {k: sum(r[k] for r in models) for k in ("calls", "ok", "input_tokens", "output_tokens", "cache_read")}
    return dict(since=since, models=models, **total)


# ── the account's real plan usage (undocumented dashboard endpoint) ────────
def account(force: bool = False) -> dict:
    cookie = os.getenv("CURSOR_SESSION_COOKIE", "").strip()
    if not cookie:
        return {"configured": False}
    with _account_lock:
        if not force and _account["data"] is not None and time.time() - _account["ts"] < ACCOUNT_CACHE_S:
            return _account["data"]
    if "=" not in cookie.split(";")[0]:
        cookie = f"WorkosCursorSessionToken={cookie}"      # just the value was pasted
    req = urllib.request.Request(ACCOUNT_URL, headers={
        "Cookie": cookie, "Accept": "application/json", "Origin": "https://cursor.com",
        "Referer": "https://cursor.com/dashboard", "User-Agent": "Mozilla/5.0 (cursor-shim usage meter)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = {"configured": True, "ok": True, "fetched_at": time.time(), "summary": json.loads(r.read())}
    except urllib.error.HTTPError as exc:
        why = "the cookie expired or was signed out: paste a fresh one" if exc.code in (401, 403) else f"HTTP {exc.code}"
        data = {"configured": True, "ok": False, "fetched_at": time.time(), "error": why}
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        data = {"configured": True, "ok": False, "fetched_at": time.time(), "error": str(exc)[:200]}
    with _account_lock:
        _account.update(ts=time.time(), data=data)
    return data


def usage_report(force: bool = False) -> dict:
    now = time.time()
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    acct = account(force)
    cycle_start = None
    try:
        cycle_start = datetime.fromisoformat(str(acct["summary"]["billingCycleStart"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError):
        pass
    month = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    return dict(now=now, today=meter(midnight), last_24h=meter(now - 86400),
                cycle=meter(cycle_start or month), cycle_from="billing cycle" if cycle_start else "calendar month",
                account=acct)


# ── running the CLI ────────────────────────────────────────────────────────
def run_agent(prompt: str, model: str) -> tuple[str, dict]:
    """The answer text and the token usage Cursor reports for the run."""
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    cmd = [AGENT, "-p", "--output-format", "json", "--mode", "ask", "--trust",
           "--workspace", str(WORKSPACE)]
    if model and model not in ("default",):
        cmd += ["--model", model]
    cmd.append(prompt)
    env = {k: v for k, v in os.environ.items() if k != "CURSOR_SESSION_COOKIE"}   # the CLI never sees the login
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=WORKSPACE,
                       stdin=subprocess.DEVNULL, env=env)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip()[-400:] or f"agent exited {p.returncode}")
    out = p.stdout.strip()
    try:
        data = json.loads(out.splitlines()[-1])
    except (ValueError, IndexError):
        return out, {}                    # text output: hand it back as is
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result") or data)[:400])
    return str(data.get("result") or ""), data.get("usage") or {}


def list_models() -> list[str]:
    global _models
    if not _models:
        try:
            p = subprocess.run([AGENT, "--list-models"], capture_output=True, text=True, timeout=60,
                               stdin=subprocess.DEVNULL)
            _models = [ln.split()[0] for ln in p.stdout.splitlines()
                       if ln.strip() and not ln.lower().startswith(("available", "model", "-", "tip"))]
        except Exception as exc:          # noqa: BLE001
            log(f"list-models failed: {exc}")
    return _models


class Handler(BaseHTTPRequestHandler):
    server_version = "cursor-shim/1"

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        if TOKEN and self.headers.get("Authorization", "") != f"Bearer {TOKEN}":
            self._send(401, {"error": {"message": "bad or missing bearer token"}})
            return False
        return True

    def log_message(self, fmt, *args):   # quiet default access log
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/healthz", "/health"):
            return self._send(200, {"ok": True, "running": _running[0], "slots": CONCURRENCY,
                                    "key": bool(os.getenv("CURSOR_API_KEY")),
                                    "account": bool(os.getenv("CURSOR_SESSION_COOKIE"))})
        if path == "/v1/models":
            if not self._authed():
                return
            return self._send(200, {"object": "list",
                                    "data": [{"id": m, "object": "model"} for m in list_models()]})
        if path == "/v1/usage":
            if not self._authed():
                return
            try:
                return self._send(200, usage_report(force="fresh=1" in self.path))
            except Exception as exc:      # noqa: BLE001
                return self._send(500, {"error": {"message": str(exc)[:300]}})
        self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._send(404, {"error": {"message": "not found"}})
        if not self._authed():
            return
        if not os.getenv("CURSOR_API_KEY"):     # 403: the council does not retry it, the judge just sits out
            return self._send(403, {"error": {"message": "CURSOR_API_KEY is not set in /etc/cursor-shim.env"}})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._send(400, {"error": {"message": "body is not JSON"}})
        msgs = req.get("messages") or []
        system = "\n\n".join(str(m.get("content") or "") for m in msgs if m.get("role") == "system")
        rest = "\n\n".join(str(m.get("content") or "") for m in msgs if m.get("role") != "system")
        prompt = (f"{system}\n\n{rest}" if system else rest).strip()
        if (req.get("response_format") or {}).get("type") == "json_object":
            prompt += JSON_ONLY
        if not prompt:
            return self._send(400, {"error": {"message": "empty prompt"}})
        if len(prompt) > MAX_PROMPT:
            prompt = prompt[:MAX_PROMPT // 2] + "\n…(trimmed)…\n" + prompt[-MAX_PROMPT // 2:]
        model = str(req.get("model") or DEFAULT_MODEL)
        if model == "default":
            model = DEFAULT_MODEL
        t0 = time.time()
        with _slots:
            with _count_lock:
                _running[0] += 1
            t_run = time.time()
            try:
                text, usage = run_agent(prompt, model)
            except subprocess.TimeoutExpired:
                log(f"timeout after {TIMEOUT_S:.0f}s ({len(prompt)} chars, {model})")
                record(model, False, time.time() - t_run, len(prompt), 0, {}, "timeout")
                return self._send(504, {"error": {"message": f"agent timed out after {TIMEOUT_S:.0f}s"}})
            except Exception as exc:      # noqa: BLE001
                log(f"error ({model}): {exc}")
                record(model, False, time.time() - t_run, len(prompt), 0, {}, str(exc))
                return self._send(502, {"error": {"message": str(exc)[:400]}})
            finally:
                with _count_lock:
                    _running[0] -= 1
        record(model, True, time.time() - t_run, len(prompt), len(text), usage)
        log(f"ok {time.time() - t0:.1f}s, {len(prompt)} chars in, {len(text)} out, "
            f"{usage.get('inputTokens', '?')}+{usage.get('outputTokens', '?')} tokens ({model})")
        self._send(200, {
            "id": "chatcmpl-" + uuid.uuid4().hex[:24], "object": "chat.completion", "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": int(usage.get("inputTokens") or 0),
                      "completion_tokens": int(usage.get("outputTokens") or 0),
                      "total_tokens": int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0)},
        })


if __name__ == "__main__":
    if not os.getenv("CURSOR_API_KEY"):
        log("warning: CURSOR_API_KEY is not set; requests will fail until it is")
    _db().close()
    log(f"listening on :{PORT} (agent {AGENT}, default model {DEFAULT_MODEL}, {CONCURRENCY} at a time, "
        f"account usage {'on' if os.getenv('CURSOR_SESSION_COOKIE') else 'off'})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

"""Browser-backed sources for platforms without an official API (Axiom, fomo.family, j7tracker).

Instead of scraping HTML, a real Chromium with a *persistent profile* (you log in once with
`solmeme login <site>`) opens the site and we listen to the network traffic the web app
already receives: websocket frames and JSON API responses. Subclasses turn those payloads
into normalized events.

Contract for a site adapter (solmeme/sources/<site>.py exposes `class Source`):

    class Source(BrowserSource):
        name = "axiom"
        start_url = "https://axiom.trade/pulse"
        login_url = "https://axiom.trade"
        ws_patterns = ["axiom.trade"]          # substrings of websocket URLs to listen to
        http_patterns = ["api.axiom.trade"]    # substrings of response URLs to parse (JSON)

        def on_ws(self, url: str, payload: str | bytes) -> list: ...
        def on_http(self, url: str, data) -> list: ...      # data = parsed JSON
        async def on_page(self, page) -> None: ...          # optional: clicks / periodic actions

Every return value is a list of events (ExternalSignal / WalletTrade / Enrichment). Unknown
payload shapes are sampled to data/browser/<site>/samples.jsonl so parsers can be fixed from
real traffic later. Never put a funded wallet or private key into this browser profile.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path

from solmeme.sources.base import Backoff, Context

log = logging.getLogger("solmeme.browser")
MAX_SAMPLES = 400


def profile_dir(settings, name: str) -> Path:
    d = settings.data_dir / "browser" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


class BrowserSource:
    name = "browser"
    start_url = ""
    login_url = ""
    ws_patterns: list[str] = []
    http_patterns: list[str] = []
    reload_every_s = 1800          # reload the page periodically (sites leak memory / drop sockets)

    def __init__(self, ctx: Context):
        self.ctx = ctx
        cfg = getattr(ctx.settings.sources, self.name, None)
        self.options: dict = dict(getattr(cfg, "model_extra", None) or {})
        self.headless = bool(self.options.get("headless", True))
        self.profile = profile_dir(ctx.settings, self.name)
        self._samples = 0
        self._sample_path = self.profile / "samples.jsonl"

    # ── override in adapters ────────────────────────────────────────────
    def on_ws(self, url: str, payload) -> list:
        return []

    def on_http(self, url: str, data) -> list:
        return []

    async def on_page(self, page) -> None:
        return None

    # ── helpers ─────────────────────────────────────────────────────────
    def sample(self, kind: str, url: str, payload) -> None:
        """Keep a bounded sample of raw payloads for parser development."""
        if self._samples >= MAX_SAMPLES:
            return
        self._samples += 1
        if isinstance(payload, bytes):
            payload = payload[:2000].hex()
        elif not isinstance(payload, str):
            payload = json.dumps(payload, default=str)
        try:
            with self._sample_path.open("a") as fh:
                fh.write(json.dumps({"ts": time.time(), "kind": kind, "url": url, "payload": payload[:20000]}) + "\n")
        except OSError:
            pass

    def _emit(self, events) -> None:
        for ev in events or []:
            ev.ts = time.time()
            self.ctx.push(self.name, ev)
            if self.ctx.watch is not None and getattr(ev, "mint", None):
                self.ctx.watch.touch(ev.mint, "kol" if ev.kind == "wallet_trade" else "signal", ev.ts)

    def _safe(self, fn, *args) -> None:
        try:
            self._emit(fn(*args))
        except Exception as exc:  # noqa: BLE001 — a bad payload must not kill the feed
            st = self.ctx.stat(self.name)
            st.errors += 1
            st.note = f"parse error: {type(exc).__name__}: {str(exc)[:80]}"
            self.sample("error", args[0] if args else "", args[1] if len(args) > 1 else "")

    def _matches(self, url: str, patterns: list[str]) -> bool:
        return any(p in url for p in patterns)

    # ── browser lifecycle ───────────────────────────────────────────────
    async def run(self) -> None:
        from playwright.async_api import async_playwright

        st = self.ctx.stat(self.name)
        bo = Backoff(base_s=15, max_s=900)
        while True:
            try:
                async with async_playwright() as pw:
                    browser_ctx = await pw.chromium.launch_persistent_context(
                        str(self.profile / "profile"), headless=self.headless,
                        viewport={"width": 1400, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"])
                    page = browser_ctx.pages[0] if browser_ctx.pages else await browser_ctx.new_page()
                    self._attach(page)
                    browser_ctx.on("page", self._attach)
                    await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
                    st.status, st.note = "ok", f"browser open: {self.start_url}"
                    bo.ok()
                    started = time.monotonic()
                    while True:
                        await self.on_page(page)
                        await asyncio.sleep(5)
                        if time.monotonic() - started > self.reload_every_s * random.uniform(0.9, 1.1):
                            await page.reload(wait_until="domcontentloaded", timeout=60_000)
                            started = time.monotonic()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                wait = bo.fail(repr(exc))
                st.errors += 1
                st.status, st.note = "restarting", f"{type(exc).__name__}: {str(exc)[:100]}"
                log.warning("%s browser failed: %r (restart in %.0fs)", self.name, exc, wait)
                await asyncio.sleep(wait)

    def _attach(self, page) -> None:
        page.on("websocket", self._on_websocket)
        page.on("response", lambda resp: asyncio.ensure_future(self._on_response(resp)))

    def _on_websocket(self, ws) -> None:
        if not self._matches(ws.url, self.ws_patterns):
            return
        url = ws.url
        ws.on("framereceived", lambda payload: self._safe(self.on_ws, url, payload))

    async def _on_response(self, resp) -> None:
        url = resp.url
        if not self.http_patterns or not self._matches(url, self.http_patterns):
            return
        if "json" not in (resp.headers.get("content-type") or ""):
            return
        try:
            data = await resp.json()
        except Exception:  # noqa: BLE001 — body gone / not JSON
            return
        self._safe(self.on_http, url, data)


async def interactive_login(settings, name: str, url: str) -> None:
    """Open a visible browser on the site's persistent profile so the user can log in once.

    Closing the window does not close a persistent context, so the wait watches
    the pages. When the last page is gone, the context is closed and the profile
    is flushed.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(profile_dir(settings, name) / "profile"), headless=False, viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        closed = asyncio.Event()
        ctx.on("close", lambda *_: closed.set())

        async def until_window_closed() -> None:
            while not closed.is_set():
                if not any(not p.is_closed() for p in ctx.pages):
                    closed.set()
                    return
                await asyncio.sleep(0.4)

        watcher = asyncio.create_task(until_window_closed())
        try:
            await closed.wait()
        finally:
            watcher.cancel()
            try:
                await ctx.close()
            except Exception:  # noqa: BLE001 — already closed when the window went away
                pass

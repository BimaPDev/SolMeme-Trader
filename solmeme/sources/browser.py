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
import re
import time
from pathlib import Path

from solmeme.sources.base import Backoff, Context

log = logging.getLogger("solmeme.browser")
MAX_SAMPLES = 400
_desktop_ua: str | None = None


def profile_dir(settings, name: str) -> Path:
    d = settings.data_dir / "browser" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def desktop_ua(ua: str) -> str:
    """The desktop Chrome user agent matching a headless one."""
    return re.sub(r"HeadlessChrome/(\d+)[\d.]*", r"Chrome/\1.0.0.0", ua)


async def headless_user_agent(pw) -> str:
    """Headless Chromium says "HeadlessChrome" in its user agent, and some sites refuse it
    outright (every fomo API call fails, so the page shows "Couldn't load your account")."""
    global _desktop_ua
    if _desktop_ua is None:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            _desktop_ua = desktop_ua(await page.evaluate("navigator.userAgent"))
        finally:
            await browser.close()
    return _desktop_ua


class BrowserSource:
    name = "browser"
    start_url = ""
    login_url = ""
    ws_patterns: list[str] = []
    http_patterns: list[str] = []
    reload_every_s = 1800          # reload the page periodically (sites drop sockets)
    recycle_every_s = 2700         # restart the whole browser: headless Chrome leaks memory a reload does not free

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
                    ua = {"user_agent": await headless_user_agent(pw)} if self.headless else {}
                    browser_ctx = await pw.chromium.launch_persistent_context(
                        str(self.profile / "profile"), headless=self.headless,
                        viewport={"width": 1400, "height": 900},
                        args=["--disable-blink-features=AutomationControlled"], **ua)
                    await import_session(browser_ctx, self.profile)
                    page = browser_ctx.pages[0] if browser_ctx.pages else await browser_ctx.new_page()
                    self._attach(page)
                    browser_ctx.on("page", self._attach)
                    await page.goto(self.start_url, wait_until="domcontentloaded", timeout=60_000)
                    st.status, st.note = "ok", f"browser open: {self.start_url}"
                    bo.ok()
                    started = opened = time.monotonic()
                    recycle = self.recycle_every_s * random.uniform(0.9, 1.1)
                    while True:
                        await self.on_page(page)
                        await asyncio.sleep(5)
                        if time.monotonic() - opened > recycle:
                            # headless Chrome leaks (one page reached 950 MB in 1.6 h and the 3 GB container ran
                            # out, Oct 10); a reload keeps the leak, a fresh browser frees it
                            log.info("%s: recycling the browser after %.0f min", self.name, recycle / 60)
                            break
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


SESSION_FILE = "session.json"      # portable login: plain cookies + localStorage, written by `solmeme login`

# Imports once per export: the stamp is the session file's mtime, so a fresh `solmeme login` replaces an old
# import (Oct 10: fomo's profile had imported on Oct 1, so its renewed login was skipped and it stayed logged out)
_LOCAL_STORAGE_JS = """(origins, stamp) => {
  const o = origins[location.origin];
  if (!o || localStorage.getItem('__solmeme_imported') === stamp) return;
  for (const [k, v] of Object.entries(o)) localStorage.setItem(k, v);
  localStorage.setItem('__solmeme_imported', stamp);
}"""


async def import_session(browser_ctx, folder: Path) -> bool:
    """Load a session exported on another machine. Chrome's own cookie store is encrypted with the
    OS keychain, so a profile copied from a Mac to Linux arrives logged out; session.json does not."""
    path = Path(folder) / SESSION_FILE
    if not path.is_file():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("cookies"):
            await browser_ctx.add_cookies(state["cookies"])
        origins = {o["origin"]: {i["name"]: i["value"] for i in o.get("localStorage", [])}
                   for o in state.get("origins", []) if o.get("origin")}
        if origins:
            stamp = str(int(path.stat().st_mtime))
            await browser_ctx.add_init_script(f"({_LOCAL_STORAGE_JS})({json.dumps(origins)}, {json.dumps(stamp)})")
        log.info("%s: imported %d cookies and %d origins from %s", folder.name, len(state.get("cookies") or []),
                 len(origins), SESSION_FILE)
        return True
    except Exception as exc:  # noqa: BLE001 - a bad export must not stop the source
        log.warning("%s: could not import %s: %r", folder.name, SESSION_FILE, exc)
        return False


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

        export = profile_dir(settings, name) / SESSION_FILE

        async def until_window_closed() -> None:
            ticks = 0
            while not closed.is_set():
                if not any(not p.is_closed() for p in ctx.pages):
                    closed.set()
                    return
                ticks += 1
                if ticks % 5 == 0:          # keep a portable copy of the login while pages are still open
                    try:
                        await ctx.storage_state(path=str(export))
                    except Exception:  # noqa: BLE001
                        pass
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

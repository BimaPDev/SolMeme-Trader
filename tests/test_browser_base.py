"""BrowserSource captures websocket frames from a real (headless) Chromium page."""

import asyncio
import json

import httpx
import pytest
import websockets
from websockets.datastructures import Headers
from websockets.http11 import Response

from solmeme.core.events import ExternalSignal
from solmeme.sources.base import Context
from solmeme.sources.browser import BrowserSource, desktop_ua


class Echo(BrowserSource):
    name = "testsite"
    ws_patterns = ["127.0.0.1"]

    def on_ws(self, url, payload):
        msg = json.loads(payload)
        return [ExternalSignal(ts=0, source=self.name, mint=msg["mint"], signal="test_new")]


def test_desktop_ua_drops_headless():
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "HeadlessChrome/153.0.8010.12 Safari/537.36")
    assert desktop_ua(ua).endswith("(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")
    assert desktop_ua(desktop_ua(ua)) == desktop_ua(ua)


def test_browser_captures_ws_frames(settings):
    pytest.importorskip("playwright")
    got = []
    agents = []

    async def main():
        page = {}

        async def handler(ws):
            await asyncio.sleep(0.5)
            for i in range(3):
                await ws.send(json.dumps({"mint": f"M{i}"}))
            await asyncio.sleep(5)

        def serve_page(conn, req):   # page must be same-host: Chrome blocks data: pages → localhost
            if req.path == "/page":
                agents.append(req.headers.get("User-Agent", ""))
                return Response(200, "OK", Headers({"Content-Type": "text/html"}), page["html"].encode())

        async with websockets.serve(handler, "127.0.0.1", 0, process_request=serve_page) as server:
            port = server.sockets[0].getsockname()[1]
            page["html"] = f"<script>new WebSocket('ws://127.0.0.1:{port}/ws')</script>"
            async with httpx.AsyncClient() as http:
                ctx = Context(settings, http, got.append)
                src = Echo(ctx)
                src.start_url = f"http://127.0.0.1:{port}/page"
                task = asyncio.create_task(src.run())
                for _ in range(100):
                    if len(got) >= 3:
                        break
                    await asyncio.sleep(0.1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())
    assert [e.mint for e in got] == ["M0", "M1", "M2"]
    assert agents and not any("Headless" in a for a in agents)   # sites like fomo refuse HeadlessChrome


def test_a_session_exported_on_another_machine_is_imported(tmp_path):
    import asyncio
    import json

    from solmeme.sources.browser import SESSION_FILE, import_session

    class Ctx:
        def __init__(self):
            self.cookies, self.scripts = [], []

        async def add_cookies(self, c):
            self.cookies += c

        async def add_init_script(self, js):
            self.scripts.append(js)

    ctx = Ctx()
    assert asyncio.run(import_session(ctx, tmp_path)) is False          # nothing exported: keep the profile as is
    (tmp_path / SESSION_FILE).write_text(json.dumps({
        "cookies": [{"name": "sid", "value": "x", "domain": ".fomo.family", "path": "/"}],
        "origins": [{"origin": "https://fomo.family", "localStorage": [{"name": "token", "value": "abc"}]}]}))
    assert asyncio.run(import_session(ctx, tmp_path)) is True
    assert ctx.cookies[0]["name"] == "sid" and '"https://fomo.family": {"token": "abc"}' in ctx.scripts[0]
    (tmp_path / SESSION_FILE).write_text("{broken")
    assert asyncio.run(import_session(Ctx(), tmp_path)) is False

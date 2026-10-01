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
from solmeme.sources.browser import BrowserSource


class Echo(BrowserSource):
    name = "testsite"
    ws_patterns = ["127.0.0.1"]

    def on_ws(self, url, payload):
        msg = json.loads(payload)
        return [ExternalSignal(ts=0, source=self.name, mint=msg["mint"], signal="test_new")]


def test_browser_captures_ws_frames(settings):
    pytest.importorskip("playwright")
    got = []

    async def main():
        page = {}

        async def handler(ws):
            await asyncio.sleep(0.5)
            for i in range(3):
                await ws.send(json.dumps({"mint": f"M{i}"}))
            await asyncio.sleep(5)

        def serve_page(conn, req):   # page must be same-host: Chrome blocks data: pages → localhost
            if req.path == "/page":
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

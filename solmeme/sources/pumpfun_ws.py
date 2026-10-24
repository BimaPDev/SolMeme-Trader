"""pump.fun's own real-time trade feed: the websocket its website uses (NATS over wss).

Every visitor of pump.fun connects to wss://unified-prod.nats.realtime.pump.fun/ with one shared, read-only
login ("subscriber") and receives `unifiedTradeEvent.lite.<mint>` messages: wallet, buy/sell, SOL and token
amounts, price and market cap, about a second after the trade. We subscribe to all of them
(`unifiedTradeEvent.lite.*`, ~150 trades a second across pump.fun) and keep only the coins the bot cares
about: the top of the watchlist (held coins first), so the event log grows by what we can replay.

The login is not stored in code: it is read from the website once (a headless browser opens pump.fun and
we take the password its page sends) and cached in data/session/pumpfun_ws.json; it is read again if the
server ever refuses it. PUMPFUN_WS_PASS in .env overrides it. Read-only: nothing is ever sent but SUB/PONG.

Trades below the bonding curve's completion price become CurveTrade events with reserves derived from the
price (pump.fun's curve is x*y=k), so fills and features work as with PumpPortal's paid stream; coins that
have traded above it are treated as migrated (PoolTrade).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import datetime

import websockets

from solmeme.config import secret
from solmeme.core.events import CurveTrade, PoolTrade
from solmeme.sim import curve
from solmeme.sources.base import Backoff, Context

log = logging.getLogger("solmeme.pumpfun_ws")

NAME = "pumpfun"
URL = "wss://unified-prod.nats.realtime.pump.fun/"
SUBJECT = "unifiedTradeEvent.lite.*"
PUMP_K = curve.INITIAL_V_SOL * curve.INITIAL_V_TOKENS
_V_TOKENS_DONE = curve.INITIAL_V_TOKENS - curve.INITIAL_REAL_TOKENS
COMPLETE_PRICE = (PUMP_K / _V_TOKENS_DONE) / _V_TOKENS_DONE      # SOL per token when the curve completes


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def parse_trade(payload: str, ts: float, migrated: set[str]) -> CurveTrade | PoolTrade | None:
    """One `unifiedTradeEvent.lite` message (a JSON string holding JSON) → a trade event."""
    try:
        d = json.loads(payload)
        if isinstance(d, str):
            d = json.loads(d)
    except ValueError:
        return None
    mint, side = d.get("mint") or "", d.get("type")
    if not mint.endswith("pump") or side not in ("buy", "sell"):
        return None                                   # pump.fun coins only (the feed also carries other chains)
    sol, tokens, px = _f(d.get("amountSol")), _f(d.get("baseAmount")), _f(d.get("priceSol"))
    if px <= 0 or sol <= 0:
        return None
    common = dict(ts=ts, source=NAME, mint=mint, trader=d.get("userAddress") or "", side=side, sol=sol,
                  tokens=tokens, mcap_sol=px * curve.TOTAL_SUPPLY, sig=d.get("tx") or "")
    if px > COMPLETE_PRICE * 1.02:
        migrated.add(mint)
    if mint in migrated:
        return PoolTrade(pool="pump-amm", **common)
    return CurveTrade(v_sol=math.sqrt(PUMP_K * px), v_tokens=math.sqrt(PUMP_K / px), pool="pump", **common)


def frames(buf: str) -> tuple[list[tuple[str, str]], list[str], str]:
    """Split NATS protocol text into (subject, payload) messages, other control lines, and the unparsed rest."""
    msgs, ctrl = [], []
    while True:
        nl = buf.find("\r\n")
        if nl < 0:
            return msgs, ctrl, buf
        line = buf[:nl]
        if line.startswith("MSG ") or line.startswith("HMSG "):
            parts = line.split()
            size = int(parts[-1])
            start = nl + 2
            if len(buf) < start + size + 2:
                return msgs, ctrl, buf                # wait for the rest of the payload
            body = buf[start:start + size]
            if line.startswith("HMSG "):              # headers come first; the payload is after them
                hdr = int(parts[-2])
                body = body[hdr:]
            msgs.append((parts[1], body))
            buf = buf[start + size + 2:]
        else:
            ctrl.append(line)
            buf = buf[nl + 2:]


async def _login_from_site() -> dict | None:
    """Open pump.fun headless and take the CONNECT the page sends (its shared read-only login)."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    found: dict = {}
    async with async_playwright() as p:
        b = await p.chromium.launch()
        try:
            pg = await b.new_page()

            def on_ws(ws):
                def sent(f):
                    f = f if isinstance(f, str) else f.decode("utf-8", "replace")
                    if f.startswith("CONNECT ") and "unified-prod" in ws.url and not found:
                        try:
                            found.update(json.loads(f[8:].split("\r\n")[0]))
                        except ValueError:
                            pass
                ws.on("framesent", sent)
            pg.on("websocket", on_ws)
            await pg.goto("https://pump.fun/", wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):
                if found:
                    break
                await asyncio.sleep(1)
        finally:
            await b.close()
    return {k: found[k] for k in ("user", "pass") if k in found} or None


class PumpFunWS:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.pumpfun
        self.cache = ctx.settings.data_dir / "session" / "pumpfun_ws.json"
        self.wanted: set[str] = set()
        self.migrated: set[str] = set()
        self.day = ""
        self.today = 0
        self.seen_rate = 0.0

    async def _login(self, refresh: bool = False) -> dict | None:
        if secret("PUMPFUN_WS_PASS"):
            return {"user": "subscriber", "pass": secret("PUMPFUN_WS_PASS")}
        if not refresh:
            try:
                return json.loads(self.cache.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        creds = await _login_from_site()
        if creds:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            self.cache.write_text(json.dumps(creds), encoding="utf-8")
        return creds

    async def _refresh_wanted(self) -> None:
        while True:
            if self.ctx.watch is not None:
                self.wanted = set(self.ctx.watch.priority_list(time.time())[: self.cfg.max_coins])
            await asyncio.sleep(2)

    def _wallets(self) -> set[str]:
        """Wallets whose pump.fun trades are kept on any coin: yours, the polled ones and every whale in the book."""
        return set(self.ctx.settings.sources.tracked_wallets) | set(getattr(self.ctx, "whales", None) or ())

    def _keep(self, mint: str) -> bool:
        """Watched coins until the daily cap; after it, only the ones we hold or asked about (pinned)."""
        if mint not in self.wanted:
            return False
        day = datetime.now().strftime("%Y-%m-%d")
        if day != self.day:
            self.day, self.today = day, 0
        if not self.cfg.daily_events or self.today < self.cfg.daily_events:
            return True
        return self.ctx.watch is not None and mint in self.ctx.watch.pinned

    async def _session(self, creds: dict) -> None:
        stat = self.ctx.stat(NAME)
        async with websockets.connect(URL, max_size=2 ** 22, origin="https://pump.fun", open_timeout=20,
                                      additional_headers={"User-Agent": "Mozilla/5.0"}) as ws:
            await ws.recv()                                    # INFO
            conn = {"verbose": False, "pedantic": False, "tls_required": False, "name": "", "lang": "pumpfun-worker",
                    "version": "1", "protocol": 1, "headers": True, "no_responders": True, **creds}
            await ws.send("CONNECT " + json.dumps(conn) + "\r\nPING\r\n")
            await ws.send(f"SUB {SUBJECT} 1\r\n")
            stat.status, stat.note = "ok", "connected: every pump.fun trade, kept for the watched coins"
            buf, seen, t0 = "", 0, time.time()
            async for raw in ws:
                buf += raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                msgs, ctrl, buf = frames(buf)
                for line in ctrl:
                    if line.startswith("PING"):
                        await ws.send("PONG\r\n")
                    elif line.startswith("-ERR"):
                        raise PermissionError(line)
                now = time.time()
                wallets = self._wallets()
                for subject, body in msgs:
                    seen += 1
                    mint = subject.rsplit(".", 1)[-1]
                    # watched coins, plus any pump.fun trade by a wallet we track (yours and the top whales):
                    # their full pump.fun history, live, on every coin
                    if not self._keep(mint) and not (wallets and any(w in body for w in wallets)):
                        continue
                    ev = parse_trade(body, now, self.migrated)
                    if ev is not None and (ev.sol >= self.cfg.min_sol or ev.trader in wallets):
                        self.today += 1
                        self.ctx.push(NAME, ev)
                if now - t0 >= 60:
                    self.seen_rate = seen / (now - t0)
                    stat.note = (f"{self.seen_rate:.0f} trades/s on pump.fun, kept for {len(self.wanted)} watched coins; "
                                 f"{self.today:,} today")
                    seen, t0 = 0, now
                if len(self.migrated) > 50_000:
                    self.migrated.clear()

    async def run(self) -> None:
        stat = self.ctx.stat(NAME)
        asyncio.get_running_loop().create_task(self._refresh_wanted())
        bo = Backoff(5, 300)
        refresh = False
        while True:
            creds = await self._login(refresh)
            if not creds:
                stat.status, stat.note = "error", "could not read pump.fun's login from its website"
                await asyncio.sleep(bo.fail("no login"))
                continue
            started = time.time()
            try:
                await self._session(creds)
                refresh = False
            except PermissionError as e:
                stat.status, stat.note = "error", f"login refused ({str(e)[:60]}); reading it from the site again"
                refresh = True
            except Exception as e:  # noqa: BLE001 - reconnect on anything
                stat.status, stat.note = "error", f"{type(e).__name__}: {str(e)[:80]}"
            stat.errors += 1
            if time.time() - started > 300:
                bo.ok()                                   # it ran for a while: reconnect quickly
            await asyncio.sleep(bo.fail(stat.note))

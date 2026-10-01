"""Live loop: sources → single consumer (stamp, record, step engine).

The consumer stamps every event with a non-decreasing receive time, appends it
to the event log, then feeds the engine — so the log order IS the processing
order and replaying it reproduces this session exactly. A 250 ms ticker fires
due timers (fills, time stops) between events; ticks are not recorded and
don't need to be (timers execute "as of" their due time either way).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time
from collections import deque

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from solmeme.config import ROOT, Settings
from solmeme.core.eventlog import EventLogWriter, read_events
from solmeme.core.store import Store
from solmeme.lab.metrics import engine_metrics
from solmeme.sim.activity import TokenActivity, age_label, short_mint
from solmeme.sim.engine import Engine, Journal
from solmeme.sim.positions import position_rows
from solmeme.sources.base import Context
from solmeme.sources.dexscreener import DexScreener
from solmeme.sources.gmgn import Gmgn
from solmeme.sources.misc import Rugcheck, SolPriceSource, Unsupported
from solmeme.sources.pumpportal import PumpPortal
from solmeme.sources.watchlist import Watchlist
from solmeme.strategy.spec import StrategySpec

log = logging.getLogger("solmeme.live")


def build_sources(ctx: Context) -> tuple[list, PumpPortal | None]:
    s = ctx.settings.sources
    out: list = []
    pp = None
    if s.pumpportal.enabled:
        pp = PumpPortal(ctx)
        out.append(pp)
    if s.gmgn.enabled:
        out.append(Gmgn(ctx))
    if s.dexscreener.enabled:
        out.append(DexScreener(ctx))
    if s.jupiter.enabled:
        out.append(SolPriceSource(ctx))
    if s.rugcheck.enabled:
        out.append(Rugcheck(ctx))
    for name in PLATFORM_SOURCES:
        if getattr(s, name).enabled:
            out.append(load_platform(name)(ctx))
    return out, pp


PLATFORM_SOURCES = ("axiom", "fomo", "j7tracker")


def _usd(amount: float, *, signed: bool = False) -> str:
    body = f"${abs(amount):,.2f}" if abs(amount) < 100 else f"${abs(amount):,.0f}"
    if amount < 0:
        return "-" + body
    if signed and amount > 0:
        return "+" + body
    return body


def _paper_equity_sol(runner, engine) -> float:
    """Cash plus what open positions would return if sold at the current market."""
    equity = runner.portfolio.cash
    for mint, pos in runner.portfolio.positions.items():
        if pos.tokens <= 0:
            continue
        st = engine.market.get(mint)
        if st is None:
            continue
        got = engine.fills.liquidation_value(st, pos.tokens)
        if got is None and st.price_sol:
            got = pos.tokens * st.price_sol
        if got is not None:
            equity += got
    return equity


def load_platform(name: str):
    """solmeme/sources/<name>.py must expose `class Source` (see sources/browser.py for the contract)."""
    import importlib

    try:
        return importlib.import_module(f"solmeme.sources.{name}").Source
    except (ImportError, AttributeError):
        return lambda ctx: Unsupported(ctx, name)


class LiveSession:
    def __init__(self, settings: Settings, store: Store | None, specs: list[tuple[StrategySpec, str]] | None,
                 console: Console | None = None, warmup_min: float = 45.0, ai: bool = False,
                 ai_every_s: float = 600, ui: str = "normal"):
        self.settings = settings
        self.store = store
        self.console = console or Console()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200_000)
        self.watch = Watchlist(settings.record.watch_ttl_s, settings.record.watch_launch_window_s)
        self.writer = EventLogWriter(settings.events_dir)
        self.activity = TokenActivity()
        self.engine: Engine | None = None
        self.sniper = None
        self.run_id = None
        self.last_ts = 0.0
        if specs:
            self.run_id = store.start_run("live") if store else None
            self.engine = Engine(specs, settings, journal=Journal(store, self.run_id), activity=self.activity)
            if (settings.sniper.enabled and self.engine is not None
                    and any(r.spec.id == "launch_sniper" for r in self.engine.runners)):
                from solmeme.sniper.session import Sniper

                self.sniper = Sniper(
                    self.engine, store,
                    decision_s=settings.sniper.decision_s,
                    horizon_s=settings.sniper.horizon_s,
                    explore=settings.sniper.explore,
                    on_decision=self._on_snipe,
                    resume=True,
                )
                self.sniper.attach()
            if warmup_min > 0:
                self._warmup(warmup_min)
        self.started = time.time()
        self.ai = ai
        self.ai_every_s = ai_every_s
        self._ai_proc = None
        self._last_ai = time.time() - ai_every_s + 120   # first AI cycle ~2 min after start
        self.ctx: Context | None = None
        self.pp: PumpPortal | None = None
        self.ui = ui
        self.hub = None
        self._httpd = None
        self._tui_proc = None
        self.boards_snap = None
        self.people_cards: list = []
        self.feed: deque = deque(maxlen=40)
        self.scan_note = ""
        self._last_publish = 0.0

    def _warmup(self, minutes: float) -> None:
        """Rebuild market + positions by replaying the recent log through the same engine."""
        start = time.time() - minutes * 60
        n = 0
        for ev in read_events(self.settings.events_dir, start, None):
            self.engine.step(ev)
            self.watch.observe(ev)
            self.last_ts = max(self.last_ts, ev.ts)
            n += 1
        if n:
            open_pos = sum(len(r.portfolio.positions) for r in self.engine.runners)
            self.console.print(f"[dim]warm start: replayed {n} events from the last {minutes:.0f} min "
                               f"→ {open_pos} open positions restored[/dim]")

    def emit(self, ev) -> None:
        try:
            self.queue.put_nowait(ev)
        except asyncio.QueueFull:
            log.error("event queue full — dropping %s", ev.kind)

    async def _consume(self) -> None:
        while True:
            ev = await self.queue.get()
            ts = max(time.time(), self.last_ts, self.engine.now if self.engine else 0.0)
            ev.ts = ts
            self.last_ts = ts
            self.writer.write(ev)
            self.watch.observe(ev)
            if self.engine is not None:
                self.engine.step(ev)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self.engine is not None:
                now = max(time.time(), self.last_ts)
                self.engine.advance(now)
                self.last_ts = now
            if self.hub is not None and time.time() - self._last_publish >= 1:
                self.publish()

    async def _manage(self) -> None:
        last_status = time.time()
        while True:
            await asyncio.sleep(5)
            pinned = self.engine.wanted_mints() if self.engine else set()
            self.watch.set_pinned(pinned)
            if self.pp is not None:
                self.pp.want(self.watch.priority_list(time.time()), pinned)
            if time.time() - last_status >= 60:
                last_status = time.time()
                self.print_status()
                self._sync_versions()
            if self.ai and time.time() - self._last_ai >= self.ai_every_s:
                await self._spawn_ai_cycle()

    async def _scans(self) -> None:
        """Boards and wallet search, once up front and again every 10 minutes. Failures stay off the trading loop."""
        await asyncio.sleep(3)
        while True:
            started = time.time()
            try:
                await self._one_scan()
            except Exception as exc:  # noqa: BLE001
                log.warning("scan failed: %s", exc)
                self.scan_note = str(exc)
                self.console.print(f"[yellow]boards/people scan failed[/yellow] {exc}")
            await asyncio.sleep(max(30.0, 600 - (time.time() - started)))

    async def _one_scan(self) -> None:
        from solmeme.cli import _boards_markdown, _print_boards
        from solmeme.lab.boards import PUMP_COINS, snapshot
        from solmeme.lab.people import discover
        from solmeme.sources.gmgn import available

        http = self.ctx.http

        async def fetch_html(url: str):
            r = await http.get(url, timeout=6, headers={"User-Agent": "SolMemeTrader/0.1", "Accept": "text/html"})
            if "html" not in (r.headers.get("content-type") or ""):
                return ""
            return r.text[:20_000]

        async def pump_get(params: dict):
            r = await http.get(PUMP_COINS, params=params,
                               headers={"User-Agent": "SolMemeTrader/0.1", "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []

        req = Gmgn(self.ctx)._req if available() else None
        snap = await snapshot(req, now=time.time(), max_cards=4, fetch_html=fetch_html, pump_get=pump_get)
        self.boards_snap = snap
        self.people_cards = []
        self.scan_note = ""
        self.publish()
        self.console.print("\n[bold]Boards[/bold]")
        _print_boards(snap)
        out = self.settings.data_dir / "boards"
        out.mkdir(parents=True, exist_ok=True)
        path = out / (time.strftime("%Y%m%d-%H%M", time.gmtime()) + ".md")
        path.write_text(_boards_markdown(snap), encoding="utf-8")
        if req is None:
            self.console.print("[yellow]GMGN_API_KEY is not set.[/yellow] Wallet search skipped. Pump.fun still loaded.")
            return
        followed = set(self.settings.sources.tracked_wallets)
        cards = await discover(req, limit=5, followed=followed)
        self.people_cards = cards
        self.publish()
        self.console.print(f"\n[bold]People[/bold]  {len(cards)} scored  ·  paper only")
        for card in cards:
            color = {"copy": "green", "watch": "yellow", "skip": "red"}.get(card["verdict"], "white")
            self.console.print(Panel(card["text"], border_style=color))

    def _sync_versions(self) -> None:
        if self.engine is None or self.store is None:
            return
        from solmeme.ai.promoter import active_specs
        for change in self.engine.sync_runners(active_specs(self.store)):
            self.console.print(f"[bold magenta]strategy set changed:[/bold magenta] {change}")

    async def _spawn_ai_cycle(self) -> None:
        """Reviews replay days of data — CPU heavy — so they run in a separate process
        (a thread would hold the GIL and delay event stamping)."""
        if self._ai_proc is not None and self._ai_proc.returncode is None:
            return
        self._last_ai = time.time()
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        fh = open(logs / "ai-cycle.log", "a")
        self._ai_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "solmeme.cli", "ai-cycle", stdout=fh, stderr=fh, cwd=str(ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.console.print("[dim]AI cycle started (shadow gate + due reviews) → logs/ai-cycle.log[/dim]")

    def print_status(self) -> None:
        up = time.time() - self.started
        t = Table(title=f"SolMeme-Trader · up {up / 60:.0f} min · {self.writer.count} events recorded", expand=False)
        t.add_column("source")
        t.add_column("status")
        t.add_column("events", justify="right")
        t.add_column("note")
        if self.ctx is not None:
            for name, s in sorted(self.ctx.stats.items()):
                t.add_row(name, s.status, str(s.events), s.note[:60])
        self.console.print(t)
        if self.pp is not None and self.pp.trade_streams_unavailable:
            self.console.print("[dim]trade streams: unavailable (set PUMPPORTAL_API_KEY funded with ≥0.02 SOL) — "
                               "prices come from DexScreener/GMGN snapshots[/dim]")
        elif self.pp is not None:
            self.console.print(f"[dim]trade streams: {len(self.pp.subscribed)} tokens · "
                               f"{self.pp.trade_events_today} trade events today"
                               f"{' · BUDGET REACHED' if self.pp.budget_exhausted else ''}[/dim]")
        if self.engine is not None:
            px = self.engine.market.sol_usd
            title = "paper" if not px else f"paper · SOL ${px:,.2f}"
            m = engine_metrics(self.engine)
            st = Table(title=title, expand=False)
            for col in ("strategy", "role", "open", "closed", "win%", "pnl SOL", "made", "cash", "balance"):
                st.add_column(col)
            for r in self.engine.runners:
                mm = m[r.key]
                equity = _paper_equity_sol(r, self.engine)
                st.add_row(
                    r.key, r.label, str(len(r.portfolio.positions)), str(mm.n_trades),
                    f"{mm.win_rate * 100:.0f}", f"{mm.total_pnl_sol:+.3f}",
                    _usd(mm.total_pnl_sol * px, signed=True) if px else "—",
                    f"{r.portfolio.cash:.2f}",
                    _usd(equity * px) if px else "—",
                )
            self.console.print(st)
            self._print_positions()
        if self.sniper:
            sm = self.sniper.learner.summary()
            bwr = sm.get("buy_win_rate")
            bwr_s = "n/a" if bwr is None else f"{bwr * 100:.0f}%"
            self.console.print(
                f"[dim]sniper: {sm.get('labels', 0)} labels · threshold {sm.get('threshold', 0):.2f} · "
                f"buy win {bwr_s}[/dim]"
            )
        self._print_tokens()

    def _print_positions(self) -> None:
        table = Table(title="positions", expand=False)
        for col in ("coin", "strategy", "status", "bought", "entry", "sold", "mark", "made"):
            table.add_column(col)
        rows = position_rows(self.engine)
        if not rows:
            rows = [("—", "—", "no paper buys yet", "—", "—", "—", "—", "—")]
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    def _print_tokens(self) -> None:
        symbols: dict[str, str] = {}
        held: dict[str, str] = {}
        if self.engine is not None:
            for mint, tok in self.engine.market.tokens.items():
                if tok.symbol:
                    symbols[mint] = tok.symbol
            for r in self.engine.runners:
                for mint, pos in r.portfolio.positions.items():
                    if pos.symbol:
                        symbols[mint] = pos.symbol
                    held[mint] = r.spec.id if mint not in held else f"{held[mint]}, {r.spec.id}"
        now = time.time()
        rows = self.activity.board(self.watch, symbols, held, now)
        table = Table(title="tokens", expand=False)
        table.add_column("token")
        table.add_column("mint")
        table.add_column("doing")
        table.add_column("detail")
        table.add_column("age")
        for note in rows:
            table.add_row(
                note.symbol or short_mint(note.mint),
                short_mint(note.mint),
                note.phase,
                note.detail,
                age_label(note.ts, now),
            )
        self.console.print(table)

    def publish(self) -> None:
        if self.hub is None:
            return
        from solmeme.ui.snapshot import build_snapshot

        self.hub.update(build_snapshot(self))
        self._last_publish = time.time()

    async def _watch_tui(self) -> None:
        proc = self._tui_proc
        while proc is not None and proc.poll() is None:
            await asyncio.sleep(0.4)

    def _shutdown_ui(self) -> None:
        proc = self._tui_proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            self._tui_proc = None
        httpd = self._httpd
        if httpd is not None:
            self._httpd = None
            httpd.shutdown()
            httpd.server_close()

    def _on_snipe(self, card, action, score) -> None:
        from rich.markup import escape

        label = action.upper()
        color = {"BUY": "green", "EXPLORE": "yellow", "SKIP": "dim"}.get(label, "white")
        sym = card.symbol or "?"
        self.feed.append(f"snipe {label} {sym} {card.mint[:6]} {score:.2f} {card.note}")
        self.console.print(
            f"snipe [{color}]{label}[/{color}] {escape(sym)} {escape(card.mint[:6])} {score:.2f} {escape(card.note)}"
        )

    async def run(self, minutes: float | None = None) -> None:
        async with httpx.AsyncClient(http2=False, follow_redirects=True) as http:
            self.ctx = Context(self.settings, http, self.emit, self.watch, activity=self.activity)
            sources, self.pp = build_sources(self.ctx)
            tasks = [asyncio.create_task(s.run()) for s in sources]
            tasks += [asyncio.create_task(self._consume()), asyncio.create_task(self._tick()),
                      asyncio.create_task(self._manage()), asyncio.create_task(self._scans())]
            waiters: list[asyncio.Task] = []
            try:
                if minutes:
                    waiters.append(asyncio.create_task(asyncio.sleep(minutes * 60)))
                else:
                    waiters.append(asyncio.create_task(asyncio.gather(*tasks)))
                if self._tui_proc is not None:
                    waiters.append(asyncio.create_task(self._watch_tui()))
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks + waiters:
                    t.cancel()
                # Save before awaiting anything: closing browsers/sockets can take a few
                # seconds, and a second Ctrl+C during that wait must not lose data.
                self._save()
                self.console.print("[dim]saved. closing connections… (Ctrl+C again to force quit)[/dim]")
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, *waiters, return_exceptions=True), 15,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                self.print_status()
                self._shutdown_ui()

    def _save(self) -> None:
        """Flush everything to disk. Synchronous on purpose (no await = can't be interrupted
        by the event loop). Open positions are restored next start by the warm-start replay."""
        self.writer.close()
        if self.sniper:
            self.sniper.flush(self.last_ts or time.time(), force=False)
        if self.engine is not None:
            self.engine.finish(close_open=False)
            if self.store and self.run_id:
                self.store.end_run(self.run_id)

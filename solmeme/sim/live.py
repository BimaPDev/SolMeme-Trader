"""Live loop: sources → single consumer (stamp, record, step engine).

The consumer stamps every event with a non-decreasing receive time, appends it
to the event log, then feeds the engine — so the log order IS the processing
order and replaying it reproduces this session exactly. A 250 ms ticker fires
due timers (fills, time stops) between events; ticks are not recorded and
don't need to be (timers execute "as of" their due time either way).
"""

from __future__ import annotations

import asyncio
import json
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
from solmeme.sim.wallet import WalletHistory, fetch_sol_usd, fund_in_sol, logged_sol_usd, wallet_view
from solmeme.sources.base import Context
from solmeme.sources.dexscreener import DexScreener
from solmeme.sources.bundletrace import BundleTrace
from solmeme.sources.chaincheck import ChainCheck
from solmeme.sources.pumpfun_social import PumpFunSocial
from solmeme.sources.pumpfun_ws import PumpFunWS
from solmeme.sources.gecko import Gecko
from solmeme.sources.gmgn import Gmgn
from solmeme.sources.news import News
from solmeme.sources.misc import Rugcheck, SolPriceSource, Unsupported
from solmeme.sources.pumpportal import PumpPortal
from solmeme.sources.solstream import SolStream
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
    if s.solstream.enabled:
        out.append(SolStream(ctx))
    if s.gecko.enabled:
        out.append(Gecko(ctx))
    if s.chaincheck.enabled:
        out.append(ChainCheck(ctx))
    if s.bundletrace.enabled:
        out.append(BundleTrace(ctx))
    if s.pumpfun.enabled:
        out.append(PumpFunWS(ctx))
    if s.pumpfun_social.enabled:
        out.append(PumpFunSocial(ctx))
    if s.news.enabled:
        out.append(News(ctx))
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
    return runner.portfolio.cash + _held_sol(runner, engine)


def _held_sol(runner, engine) -> float:
    """What a runner's open positions would return if sold at the current market."""
    return engine.held_sol(runner)


def _wallet_line(view: dict | None) -> str:
    """One-line rich-markup wallet summary: the best strategy's wallet, start → now, against the goal."""
    if view is None:
        return "[bold]Wallet[/bold]  —"
    pnl = view["pnl_usd"] if view["pnl_usd"] is not None else view["pnl_sol"]
    color = "green" if pnl > 0 else "red" if pnl < 0 else "dim"
    pct = f" ({view['pnl_pct']:+.2f}%)" if view["pnl_pct"] is not None else ""
    if view["start_usd"] is not None and view["balance_usd"] is not None:
        head = f"${view['start_usd']:,.2f} ({view['start_sol']:.3f} SOL) → ${view['balance_usd']:,.2f}"
        delta = f"{'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}"
    else:
        head = f"{view['start_sol']:.3f} SOL → {view['balance_sol']:.3f} SOL"
        delta = f"{pnl:+.3f} SOL"
    line = f"[bold]Wallet[/bold]  {head}  [{color}]{delta}{pct}[/{color}]"
    if view["best"]:
        line += f"  [dim]best: {view['best']}[/dim]"
    rng = view.get("range")
    if rng:
        fmt = (lambda v: f"${v:,.2f}") if rng["unit"] == "usd" else (lambda v: f"{v:.3f} SOL")
        line += f"  [bold]High[/bold] {fmt(rng['high'])}"
        if rng["from_high_pct"] is not None and rng["from_high_pct"] < 0:
            line += f" [red]({rng['from_high_pct']:.1f}%)[/red]"
        line += f"  [bold]Low[/bold] {fmt(rng['low'])}"
    if view["goal_usd"]:
        line += f"  [bold]Goal[/bold] ${view['goal_usd']:,.2f}"
        if view["goal_pct"] is not None:
            line += f" ({view['goal_pct']:.0f}%)"
        if view["reached"]:
            line += f"  [green]reached by {', '.join(view['reached'])}[/green]"
    return line


def load_platform(name: str):
    """solmeme/sources/<name>.py must expose `class Source` (see sources/browser.py for the contract)."""
    import importlib

    try:
        return importlib.import_module(f"solmeme.sources.{name}").Source
    except (ImportError, AttributeError):
        return lambda ctx: Unsupported(ctx, name)


class LiveSession:
    def __init__(self, settings: Settings, store: Store | None, specs: list[tuple[StrategySpec, str]] | None,
                 console: Console | None = None, warmup_min: float = 45.0, ai: bool = False, fresh: bool = False,
                 new_wallet: bool = False,
                 ai_every_s: float = 600, ui: str = "normal"):
        self.console = console or Console()
        self.wallet_px: float | None = None
        self.wallet_history = WalletHistory()
        from solmeme.ui.history import SeriesBook
        self.series = SeriesBook()
        if specs and settings.portfolio.starting_usd:
            self.wallet_px = fetch_sol_usd() or logged_sol_usd(settings)
            if self.wallet_px:
                settings = fund_in_sol(settings, self.wallet_px)
                self.console.print(f"[dim]wallet: ${settings.portfolio.starting_usd:,.2f} = "
                                   f"{settings.portfolio.starting_sol:.4f} SOL "
                                   f"{'shared by every strategy' if settings.portfolio.shared_wallet else 'per strategy'} "
                                   f"at ${self.wallet_px:,.2f}/SOL[/dim]")
            else:
                self.console.print(f"[yellow]wallet: no SOL price yet, starting with "
                                   f"{settings.portfolio.starting_sol} SOL instead of "
                                   f"${settings.portfolio.starting_usd:,.2f}[/yellow]")
        self.settings = settings
        self.store = store
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200_000)
        self.watch = Watchlist(settings.record.watch_ttl_s, settings.record.watch_launch_window_s)
        self.writer = EventLogWriter(settings.events_dir)
        self.activity = TokenActivity()
        self.engine: Engine | None = None
        self.dryrun = None                        # live dry run: champion fills at real quotes (sim/dryrun.py)
        self.overseer = None                      # sim/overseer.py: the last check before a live buy
        self.sniper = None
        self.run_id = None
        self.last_ts = 0.0
        if specs:
            self.run_id = store.start_run("live") if store else None
            self.engine = Engine(specs, settings, journal=Journal(store, self.run_id), activity=self.activity,
                                 shared_wallet=settings.portfolio.shared_wallet)
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
            self._load_bans()
            if fresh:
                self._restore_swarm_wallets()
            saved = None
            if settings.portfolio.keep_wallet and not new_wallet:
                saved = self._restore_champions_wallet()
            if warmup_min > 0:
                self._warmup(warmup_min, trade=not fresh)
            if saved and saved.get("positions"):
                self._restore_positions(saved)       # after the warm start, so their tokens have market state
            # the warm start rebuilt 45 min of market state: let live prices settle before buying, or
            # every stale "dip" or "breakout" in that state gets bought in the same second
            self.engine.entries_after = time.time() + settings.portfolio.entry_settle_s
            if settings.dryrun.enabled and self.engine.account is not None:
                from solmeme.sim.dryrun import DryRun

                self.dryrun = DryRun(settings, store)
                if self.dryrun.load():
                    self.console.print(f"live dry run continues: {self.dryrun.equity():.4f} SOL "
                                       f"(started at {self.dryrun.start_sol:.4f} SOL), "
                                       f"{len(self.dryrun.positions)} open")
                self.engine.fill_listeners.append(self.dryrun.on_fill)   # queued until its loop runs
                if getattr(settings, "overseer", None) is not None and settings.overseer.enabled:
                    from solmeme.sim.overseer import Overseer

                    def _ages(sess=self):
                        stats = getattr(getattr(sess, "ctx", None), "stats", {}) or {}
                        now = time.time()
                        return {n: now - stats[n].last_event_ts for n in ("pumpfun", "dexscreener")
                                if n in stats and stats[n].last_event_ts}
                    self.overseer = Overseer(settings.data_dir, settings.overseer, health_fn=_ages)
                    self.overseer.load()
                    self.dryrun.overseer = self.overseer
                if getattr(settings.dryrun, "council_review", False):
                    from solmeme.ai.ask import review_buy
                    self.dryrun.review_fn = lambda item: review_buy(self, item, self.dryrun.council_sell)
                eng = self.engine

                def _market():
                    b = eng.shared_breakdown()
                    return eng.market.sol_usd, (b["cash"] + b["held"]) if b else None
                self.dryrun.market_fn = _market

                def _exit_plan(key, eng=eng):
                    r = next((x for x in eng.runners if x.key == key), None)
                    if r is None:
                        return None, None
                    p, ex = r.spec.p, r.spec.exits
                    return (float(p(ex.stop_loss_pct)) if ex.stop_loss_pct is not None else None,
                            float(p(ex.max_hold_s)) if ex.max_hold_s is not None else None)
                self.dryrun.exit_plan_fn = _exit_plan
                self.dryrun.state_fn = lambda mint, eng=eng: eng.market.get(mint)
        if self.engine is not None:
            def _holders(mint, eng=self.engine):
                st = eng.market.get(mint)
                if st is None or not st.has_trade_stream:
                    return None
                return {w: b for w, b in list(st.balances.items()) if b > 0 and w != st.creator}
            self._holders_fn = _holders                     # handed to the sources' Context in run()
        self.started = time.time()
        self.ai = ai
        self.ai_every_s = ai_every_s
        self._ai_proc = None
        self._last_ai = time.time() - ai_every_s + 120   # first AI cycle ~2 min after start
        self._evo_proc = None
        self._last_evo = time.time() - settings.evolution.every_min * 60 + 300   # first generation ~5 min in
        self.ctx: Context | None = None
        self.pp: PumpPortal | None = None
        self.ui = ui
        self.hub = None
        self._httpd = None
        self._tui_proc = None
        self.boards_snap = None
        self.people_cards: list = []
        self.people_note = ""
        self.sources: list = []
        from solmeme.lab.people import TraderBook
        self.traders = TraderBook()
        self.feed: deque = deque(maxlen=40)
        self.asked_mints: dict[str, float] = {}   # coins you asked the council about (ai/ask.py) -> follow until
        self.scan_note = ""
        self._last_publish = 0.0

    def _restore_champions_wallet(self) -> None:
        """Continue the champions' wallet from the last save (positions were valued at market then)."""
        from solmeme.sim.wallet import SHARED, load_champions_wallet

        st = load_champions_wallet(self.settings)
        if not st or self.engine is None or self.engine.account is None or not st.get("equity_sol"):
            return None
        acct = self.engine.account
        # new saves carry the real cash plus the open positions themselves; old ones only a total value
        acct.cash = float(st["cash_sol"]) if "cash_sol" in st else float(st["equity_sol"])
        acct.starting_sol = float(st["start_sol"])
        self.wallet_px = st.get("start_px") or self.wallet_px
        self.wallet_history.marks[SHARED] = {k: tuple(v) for k, v in (st.get("marks") or {}).items()}
        self.console.print(f"[dim]champions' wallet continues: {float(st['equity_sol']):.4f} SOL "
                           f"(started at {acct.starting_sol:.4f} SOL)[/dim]")
        return st

    def _restore_positions(self, saved: dict) -> None:
        """Open positions survive a restart: same trades, sold later by their own exit rules. A position
        whose strategy is gone (replaced champion) is cashed out at its last saved market value."""
        runners = {r.key: r for r in self.engine.runners}
        back, cashed = 0, 0.0
        for key, group in saved["positions"].items():
            r = runners.get(key)
            for d in group["positions"]:
                if r is not None and (group["shared"] == self.engine.in_shared_wallet(r)):
                    if self.engine.restore_position(r, d):
                        back += 1
                        if not group["shared"]:      # swarm wallets were rebuilt from closed trades only
                            r.portfolio.account.cash -= d["cost_sol"] - d["proceeds_sol"]
                elif group["shared"]:
                    self.engine.account.cash += float(d.get("mark_value_sol") or 0.0)
                    cashed += float(d.get("mark_value_sol") or 0.0)
        if back or cashed:
            self.console.print(f"[dim]restored {back} open positions"
                               + (f"; cashed out {cashed:.4f} SOL from strategies no longer running" if cashed else "")
                               + "[/dim]")

    def _restore_swarm_wallets(self) -> None:
        """Swarm agents keep their wallet across restarts: start plus realized PnL since birth, the
        same number the swarm judges them on. Their open positions come back in _restore_positions."""
        if self.engine is None or self.store is None:
            return
        from solmeme.sim.wallet import wallet_reset_ts
        reset = wallet_reset_ts(self.settings)
        for r in self.engine.runners:
            if r.label != "swarm":
                continue
            row = self.store.get_version(r.spec.id, r.spec.version)
            if row is None:
                continue
            since = max(row["status_ts"], reset)
            pnl = sum(float(t["pnl_sol"] or 0) for t in self.store.trades(r.spec.id, r.spec.version, run_mode="live")
                      if (t["entry_ts"] or 0) >= since)
            if pnl:
                r.portfolio.account.cash = max(0.0, r.portfolio.account.cash + pnl)
                r.portfolio.realized += pnl

    def wallet(self) -> dict | None:
        return wallet_view(self.engine, self.wallet_px, self.engine.market.sol_usd if self.engine else None,
                           self.settings.portfolio.goal_usd, self.wallet_history)

    def _warmup(self, minutes: float, trade: bool = True) -> None:
        """Rebuild market + positions by replaying the recent log through the same engine.

        trade=False rebuilds market data only (prices, history, features), so the wallet
        starts untouched with no positions: a fresh run.
        """
        start = time.time() - minutes * 60
        n = 0
        journal = self.engine.journal
        journal.persist_after = float("inf")       # the session that recorded these already journaled them
        for ev in read_events(self.settings.events_dir, start, None):
            if trade:
                self.engine.step(ev)
            else:
                self.engine.market.apply(ev)
                self.engine.now = max(self.engine.now, ev.ts)
            self.watch.observe(ev)
            self.last_ts = max(self.last_ts, ev.ts)
            n += 1
        journal.persist_after = self.last_ts if n else float("-inf")
        if n and not trade:
            self.console.print(f"[dim]fresh start: loaded {n} events of market data from the last "
                               f"{minutes:.0f} min; no positions carried over[/dim]")
        elif n:
            open_pos = sum(len(r.portfolio.positions) for r in self.engine.runners)
            self.console.print(f"[dim]warm start: replayed {n} events from the last {minutes:.0f} min "
                               f"→ {open_pos} open positions restored[/dim]")

    def emit(self, ev) -> None:
        if getattr(ev, "kind", None) == "wallet_trade":
            self.traders.add_trade(ev.wallet, ev.side, ev.usd, getattr(ev, "symbol", "") or "", ev.label)
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
                copied = self.engine.market.narratives.copied
                while copied:                 # a new copycat launched: price-track the original it copies
                    self.watch.touch(copied.popleft(), "signal", ts)

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            if self.engine is not None:
                now = max(time.time(), self.last_ts)
                self.engine.advance(now)
                self.last_ts = now
                self.wallet_history.observe(self.engine, self.engine.market.sol_usd, now)
                self.series.observe(self, now)
            if self.hub is not None and time.time() - self._last_publish >= 0.25:
                try:
                    self.publish()
                except Exception:  # noqa: BLE001
                    log.exception("snapshot failed")

    def _pin_ranks(self, pinned: set[str]) -> dict[str, int]:
        """Who needs a held coin's live trades most, for sources with few slots (pump.fun feed: max_coins):
        0 the live wallet, champions and your asks, 1 trials, 2 swarm agents, 3 the rest (outcome trackers)."""
        ranks = {m: 3 for m in pinned}
        for m in self.asked_mints:
            ranks[m] = 0
        if self.dryrun is not None:
            for m in getattr(self.dryrun, "positions", {}):
                ranks[m] = 0
        for r in (self.engine.runners if self.engine else []):
            k = 0 if r.label == "champion" else 2 if r.label == "swarm" else 1
            for m in (*r.portfolio.positions, *r.pending_buys):
                ranks[m] = min(ranks.get(m, 3), k)
        return ranks

    async def _manage(self) -> None:
        last_status = time.time()
        while True:
            await asyncio.sleep(5)
            pinned = self.engine.wanted_mints() if self.engine else set()
            now = time.time()
            self.asked_mints = {m: t for m, t in self.asked_mints.items() if t > now}
            pinned |= set(self.asked_mints)        # coins you asked about get fetched like held ones
            self.watch.set_pinned(pinned, self._pin_ranks(pinned))
            if self.pp is not None:
                self.pp.want(self.watch.priority_list(time.time()), pinned)
            if time.time() - last_status >= 60:
                last_status = time.time()
                self.print_status()
                self._sync_versions()
                self._load_bans()
                if self.engine is not None and self.settings.fills.calibrate_from_live:
                    from solmeme.sim.calibration import load as load_calibration
                    self.engine.fills.calib = load_calibration(self.settings.data_dir)   # the dry run's latest
                self._dump_state()
            if self.ai and time.time() - self._last_ai >= self.ai_every_s:
                await self._spawn_ai_cycle()
            if (self.engine is not None and self.settings.evolution.enabled
                    and time.time() - self._last_evo >= self.settings.evolution.every_min * 60):
                await self._spawn_evolution()

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
        self.scan_note = ""
        self.publish()
        self.console.print("\n[bold]Boards[/bold]")
        _print_boards(snap)
        out = self.settings.data_dir / "boards"
        out.mkdir(parents=True, exist_ok=True)
        path = out / (time.strftime("%Y%m%d-%H%M", time.gmtime()) + ".md")
        path.write_text(_boards_markdown(snap), encoding="utf-8")
        self.publish()
        if self.people_cards:
            self.console.print(f"\n[bold]People[/bold]  {len(self.people_cards)} from fomo")
            for card in self.people_cards:
                self.console.print(card["text"].split("\n", 1)[0])
        else:
            self.console.print(f"[dim]{self.people_note}[/dim]")

    def _fomo_rows(self) -> list[dict]:
        for src in self.sources:
            if getattr(src, "name", "") == "fomo":
                return list(getattr(src, "leaderboard", None) or [])
        return []

    def _load_bans(self) -> None:
        if self.engine is None:
            return
        from solmeme.lab.banlist import BanList
        from solmeme.lab.whales import WhaleBook
        self.banlist = BanList(self.settings.data_dir)
        self.engine.market.banned = self.banlist.addresses
        self.engine.market.ban_roles = {a: b.role for a, b in self.banlist.bans.items()}
        from solmeme.lab import my_wallets
        self.whalebook = WhaleBook(self.settings.data_dir)
        mine = list(my_wallets.load(self.settings.data_dir))          # wallets you chose on the dashboard
        self.engine.market.whales = set(self.whalebook.whales()) | set(mine)
        self._my_wallets = set(mine)
        self._whale_set = set(self.whalebook.whales())
        if getattr(self, "ctx", None) is not None:
            self.ctx.whales = self._whale_set                          # pump.fun feed keeps their trades on any coin
        if not getattr(self, "_my_hook", False):                       # log their trades, for the dashboard
            self._my_hook = True
            self._my_seen: set[str] = set()

            def hook(ev, st, data_dir=self.settings.data_dir):
                who = getattr(ev, "wallet", None) or getattr(ev, "trader", None)
                if not who or ev.kind not in ("wallet_trade", "curve_trade", "pool_trade"):
                    return
                mine, whale = who in self._my_wallets, who in self._whale_set
                if not mine and not whale:
                    return
                key = getattr(ev, "tx", "") or getattr(ev, "sig", "") or f"{who}:{ev.mint}:{ev.side}:{int(ev.ts)}"
                if key in self._my_seen:
                    return
                self._my_seen.add(key)
                if len(self._my_seen) > 20_000:
                    self._my_seen.clear()
                row = dict(ts=ev.ts, wallet=who, mint=ev.mint, side=ev.side,
                           symbol=getattr(ev, "symbol", "") or (st.symbol if st else ""),
                           sol=getattr(ev, "sol", None), price_sol=st.price_sol if st else None, source=ev.source)
                if mine:
                    my_wallets.record_trade(data_dir, row)
                if whale:
                    my_wallets.record_whale_trade(data_dir, row)
            self.engine.hooks.append(hook)
        # GMGN polls the top whales next to the wallets in settings.yaml and yours
        if not hasattr(self, "_base_tracked"):
            self._base_tracked = list(self.settings.sources.tracked_wallets)
        base = self._base_tracked + [w for w in mine if w not in self._base_tracked]
        tracked = base + [w for w in self.whalebook.top() if w not in base]
        self.settings.sources.tracked_wallets[:] = tracked

    def _sync_versions(self) -> None:
        if self.engine is None or self.store is None:
            return
        from solmeme.ai.promoter import active_specs
        epoch = self.engine.wallet_epoch
        b = self.engine.shared_breakdown()
        paper_before = (b["cash"] + b["held"]) if b else None
        for change in self.engine.sync_runners(active_specs(self.store)):
            self.console.print(f"[bold magenta]strategy set changed:[/bold magenta] {change}")
        if self.engine.wallet_epoch != epoch:     # fresh champions' wallet: its start, high and low start over
            self.wallet_px = self.engine.market.sol_usd or self.wallet_px
            if self.dryrun is not None and self.engine.account is not None:
                self.dryrun.paper_rebase(paper_before, self.engine.account.starting_sol)
            self.wallet_history.marks.pop("wallet", None)

    async def _spawn_evolution(self) -> None:
        """One swarm generation in its own low-priority process (replays are CPU heavy). No LLM."""
        if self._evo_proc is not None and self._evo_proc.returncode is None:
            return
        self._last_evo = time.time()
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        fh = open(logs / "evolve.log", "a")
        self._evo_proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "solmeme.cli", "evolve", stdout=fh, stderr=fh, cwd=str(ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
        self.console.print("[dim]swarm generation started → logs/evolve.log[/dim]")

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
            shared = self.engine.account is not None
            cols = ["strategy", "role", "open", "closed", "win%", "pnl SOL", "made"]
            cols += ["held"] if shared else ["cash", "balance"]
            for col in cols:
                st.add_column(col)
            for r in self.engine.runners:
                mm = m[r.key]
                row = [
                    r.key, r.label, str(len(r.portfolio.positions)), str(mm.n_trades),
                    f"{mm.win_rate * 100:.0f}", f"{mm.total_pnl_sol:+.3f}",
                    _usd(mm.total_pnl_sol * px, signed=True) if px else "—",
                ]
                if shared:
                    held = _held_sol(r, self.engine)
                    row.append(_usd(held * px) if px else f"{held:.3f}")
                else:
                    equity = _paper_equity_sol(r, self.engine)
                    row += [f"{r.portfolio.cash:.2f}", _usd(equity * px) if px else "—"]
                st.add_row(*row)
            self.console.print(st)
            self.console.print(_wallet_line(self.wallet()))
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
        from solmeme.ui.snapshot import _token_maps

        symbols, held, opened = _token_maps(self)
        now = time.time()
        rows = self.activity.board(self.watch, symbols, held, now, opened=opened)
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

    def _refresh_people(self) -> None:
        self.traders.add_leaderboard(self._fomo_rows())
        self.traders.add_tracked(self.settings.sources.tracked_wallets)
        self.people_cards = self.traders.cards(limit=8)
        self.people_note = "" if self.people_cards else (
            "No traders yet. People come from the fomo leaderboard and the trades it records."
        )

    def publish(self) -> None:
        self._refresh_people()
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
            self.loop = asyncio.get_running_loop()       # council picks (ai/ask.py) post events from their thread
            self.ctx = Context(self.settings, http, self.emit, self.watch, activity=self.activity)
            self.ctx.holders_fn = getattr(self, "_holders_fn", None)    # bundletrace: top holders from the trade stream
            self.ctx.whales = getattr(self, "_whale_set", set())
            sources, self.pp = build_sources(self.ctx)
            self.sources = sources
            tasks = [asyncio.create_task(s.run()) for s in sources]
            tasks += [asyncio.create_task(self._consume()), asyncio.create_task(self._tick()),
                      asyncio.create_task(self._manage()), asyncio.create_task(self._scans())]
            if self.dryrun is not None:
                tasks.append(asyncio.create_task(self._run_dryrun(http)))

            async def _run_tasks() -> None:
                await asyncio.gather(*tasks)

            waiters: list[asyncio.Task] = []
            try:
                if minutes:
                    waiters.append(asyncio.create_task(asyncio.sleep(minutes * 60)))
                else:
                    waiters.append(asyncio.create_task(_run_tasks()))
                if self._tui_proc is not None:
                    waiters.append(asyncio.create_task(self._watch_tui()))
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks + waiters:
                    t.cancel()
                # Save before awaiting anything: closing browsers/sockets can take a few
                # seconds, and a second Ctrl+C during that wait must not lose data.
                self._save()
                self._dump_state(ended=True)
                self.console.print("[dim]saved. closing connections… (Ctrl+C again to force quit)[/dim]")
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, *waiters, return_exceptions=True), 15,
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                self.print_status()
                self._shutdown_ui()

    async def _run_dryrun(self, http: httpx.AsyncClient) -> None:
        """Start the dry-run wallet once the SOL price is known (a fresh one only the first time), then run it."""
        d = self.dryrun
        while not d.started_ts:
            px = self.engine.market.sol_usd
            if px:
                b = self.engine.shared_breakdown()
                d.start_fresh(px, (b["cash"] + b["held"]) if b else None)
                self.console.print(f"[bold]live dry run[/bold]: a fresh ${d.cfg.start_usd:.0f} wallet "
                                   f"({d.start_sol:.4f} SOL) copies the champions at real Jupiter prices. Nothing is sent.")
                break
            await asyncio.sleep(2)
        await d.run(http)

    def _dump_state(self, ended: bool = False) -> None:
        """data/session/live.json: the UI snapshot, for `solmeme session` in another terminal."""
        if self.engine is None:
            return
        if self.settings.portfolio.keep_wallet:
            try:
                from solmeme.sim.wallet import save_champions_wallet
                save_champions_wallet(self.settings, self.engine, self.wallet_px, self.wallet_history)
            except Exception:  # noqa: BLE001
                log.exception("champions' wallet save failed")
        from solmeme.ui.snapshot import build_snapshot

        try:
            snap = build_snapshot(self)
            for heavy in ("tokens", "boards", "people"):
                snap.pop(heavy, None)
            snap.update(run_id=self.run_id, event_session=self.writer.session, started=self.started,
                        pid=os.getpid(), ended=ended)
            path = self.settings.data_dir / "session" / "live.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(snap, default=str), encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001 — a status file must never stop trading
            log.exception("session state dump failed")

    def _save(self) -> None:
        """Flush everything to disk. Synchronous on purpose (no await = can't be interrupted
        by the event loop). Open positions are restored next start by the warm-start replay."""
        self.writer.close()
        if self.sniper:
            self.sniper.flush(self.last_ts or time.time(), force=False)
        if self.engine is not None:
            if self.settings.portfolio.keep_wallet:
                try:
                    from solmeme.sim.wallet import save_champions_wallet
                    save_champions_wallet(self.settings, self.engine, self.wallet_px, self.wallet_history)
                except Exception:  # noqa: BLE001
                    log.exception("champions' wallet save failed")
            self.engine.finish(close_open=False)
            if self.store and self.run_id:
                self.store.end_run(self.run_id)

"""Live dry run: would real money have done what the paper wallet did?

A second wallet, started at a real deposit (dryrun.start_usd), mirrors every fill of the champions'
shared wallet at the moment it happens, but prices it from a real executable Jupiter quote instead
of the paper fill model: our own price impact, the real route (pump.fun curve or PumpSwap), and the
real failure cases (no route, price moved past the slippage limit, a coin that cannot be sold).
Sizing follows the same rule as the champions (position_pct of its own equity, capped), so a $50
wallet bets like a $50 wallet. Open positions are re-valued with a real sell quote.

Nothing is signed or sent: no private key exists anywhere in this project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

import httpx

from solmeme.config import Settings

log = logging.getLogger("solmeme.dryrun")

SOL_MINT = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000
BASE_FEE_SOL = 0.000005
PUMP_COIN = "https://advanced-indexer.pump.fun/in-memory-coin/"


async def _no_coin(_mint):
    return None
SOL_QUOTES = {"So11111111111111111111111111111111111111112", "11111111111111111111111111111111"}
FILE = "dryrun_wallet.json"
REQUEST_FILE = "dryrun_request.json"
SERIES_EVERY_S = 60.0
SERIES_MAX = 8000
PATH_MAX = 240                 # points kept per position chart
TAPE_MAX = 500


class QuoteError(Exception):
    """No executable quote: no route, the API refused, or it timed out."""


@dataclass
class Quote:
    out_raw: int
    impact_pct: float
    route: list[str]
    ms: float


QuoteFn = Callable[[str, str, int], Awaitable[Quote]]


@dataclass
class DryPos:
    key: str                  # the champion runner it mirrors (strategy@version)
    mint: str
    symbol: str
    raw: int                  # tokens held, in base units
    decimals: int
    cost_sol: float           # swap SOL + fees + rent paid to open
    fees_sol: float
    entry_ts: float
    entry_price: float        # SOL per token (real)
    paper_entry_price: float
    buy_impact_pct: float
    initial_raw: int = 0
    proceeds_sol: float = 0.0
    value_sol: float = 0.0    # last real sell quote for what is held
    marked_ts: float = 0.0
    sell_fails: int = 0
    route: list[str] = field(default_factory=list)
    path: list = field(default_factory=list)   # [ts, value SOL]: entry, every re-check, sells (the position's chart)
    why: str = ""             # what the strategy saw when it bought
    entry_sol_usd: float = 0.0
    peak_value_sol: float = 0.0


class DryRun:
    def __init__(self, settings: Settings, store=None, quote_fn: QuoteFn | None = None,
                 decimals_fn: Callable[[str], Awaitable[int]] | None = None, clock=time.time,
                 coin_fn: Callable[[str], Awaitable[dict | None]] | None = None):
        self.settings, self.cfg, self.store, self.clock = settings, settings.dryrun, store, clock
        self.http: httpx.AsyncClient | None = None
        self._quote = quote_fn or self._jupiter_quote
        self._coin = coin_fn or (self._pump_coin if quote_fn is None else _no_coin)   # injected quotes: no network
        self._coin_cache: dict[str, dict] = {}
        self.exit_plan_fn = None                          # key -> (stop_pct, max_hold_s) of the strategy that bought
        self._dev_history = None if quote_fn is not None else self._creator_history   # injected quotes: no network
        self.state_fn = None                              # mint -> TokenState (the creator's bag, for the watcher)
        self._decimals = decimals_fn or self._jupiter_decimals
        self._dec_cache: dict[str, int] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        import queue as _q
        self.council_q: _q.Queue = _q.Queue()             # (key, mint, why) from the council's review threads
        self.review_fn = None                             # (item) -> None: starts a council review of a live buy
        self.overseer = None                              # sim/overseer.py: may block a live buy
        self.started_ts = 0.0
        self.start_sol = 0.0
        self.start_px: float | None = None
        self.cash = 0.0
        self.rent_held = 0.0
        self.positions: dict[tuple[str, str], DryPos] = {}
        self.closed: list[dict] = []          # newest last (also stored in the DB)
        self.counts = self._zero_counts()
        self.high = self.low = (0.0, 0.0)     # (equity SOL, ts)
        self.paper_start_sol: float | None = None   # the champions' wallet when the dry run started, for comparison
        self.paper_factor = 1.0                     # paper's return before a champions'-wallet restart (paper_rebase)
        self._retry: dict[tuple[str, str], tuple[float, str, dict]] = {}   # unsellable for now: retried
        self.series: list[tuple[float, float]] = []    # (ts, equity SOL): the wallet over time, for the chart
        self.tape: list[dict] = []                      # every fill and failed attempt, newest last (the trades page)
        self.attempt = 1                                # attempts: each a fresh real-size wallet; old ones are kept
        self.attempts: list[dict] = []
        self.stopped: dict | None = None                # hit the loss limit: no new buys until a new attempt
        self.market_fn = None                           # () -> (sol_usd, champions' wallet SOL), set by the live session

    @staticmethod
    def _zero_counts() -> dict:
        return dict(buys=0, sells=0, failed_buys=0, slippage=0, no_route=0, no_cash=0, stuck=0,
                    sell_retries=0, quote_errors=0, skipped=0, after_stop=0)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start_fresh(self, sol_usd: float, paper_equity_sol: float | None) -> None:
        now = self.clock()
        self.started_ts, self.start_px = now, sol_usd
        self.start_sol = self.cash = self.cfg.start_usd / sol_usd
        self.paper_start_sol = paper_equity_sol
        self.paper_factor = 1.0
        self.high = self.low = (self.start_sol, now)
        self.series = [(now, self.start_sol, self.start_sol)]
        self.save()

    def load(self) -> bool:
        try:
            d = json.loads((self.settings.data_dir / "session" / FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        self.started_ts, self.start_sol, self.start_px = d["started_ts"], d["start_sol"], d.get("start_px")
        self.cash, self.rent_held = d["cash"], d.get("rent_held", 0.0)
        self.positions = {(p["key"], p["mint"]): DryPos(**p) for p in d.get("positions", [])}
        self.closed = d.get("closed", [])[-200:]
        self.counts.update(d.get("counts", {}))
        self.high, self.low = tuple(d.get("high", (self.start_sol, self.started_ts))), tuple(d.get("low", (self.start_sol, self.started_ts)))
        self.paper_start_sol = d.get("paper_start_sol")
        self.paper_factor = float(d.get("paper_factor") or 1.0)
        self.series = [tuple(x) for x in d.get("series", [])]
        self.tape = d.get("tape", [])[-TAPE_MAX:]
        self.attempt, self.attempts, self.stopped = d.get("attempt", 1), d.get("attempts", []), d.get("stopped")
        return True

    def save(self) -> None:
        path = self.settings.data_dir / "session" / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(
            started_ts=self.started_ts, start_sol=self.start_sol, start_px=self.start_px, cash=self.cash,
            rent_held=self.rent_held, positions=[asdict(p) for p in self.positions.values()],
            closed=self.closed[-200:], counts=self.counts, high=self.high, low=self.low,
            paper_start_sol=self.paper_start_sol, paper_factor=self.paper_factor, series=self.series, tape=self.tape[-TAPE_MAX:],
            attempt=self.attempt, attempts=self.attempts, stopped=self.stopped),
            default=float), encoding="utf-8")
        tmp.replace(path)
        if self.settings.fills.calibrate_from_live:     # teach paper what real prices did (sim/calibration.py)
            from solmeme.sim import calibration
            try:
                calibration.save(self.settings.data_dir,
                                 calibration.compute(self.tape, self.settings.fills.calibration_prior_n))
            except OSError:
                log.exception("fill calibration not saved")

    # ── the engine tells us about every fill (sync, on the event loop) ─────
    def paper_rebase(self, paper_equity_before: float | None, new_start: float) -> None:
        """The champions' wallet restarted (a new champion): keep "paper, same period" continuous by carrying the
        return paper had made so far, then measuring from the fresh wallet's start (Oct 10: it read +109% because
        attempt #4 began at the old wallet's $22 and the new one started at $50)."""
        if self.paper_start_sol and paper_equity_before:
            self.paper_factor *= paper_equity_before / self.paper_start_sol
        self.paper_start_sol = new_start
        self.save()

    def council_sell(self, key: str, mint: str, why: str) -> None:
        """Called from the council's review thread: the judges want this live position sold now."""
        self.council_q.put((key, mint, why))

    async def _council_sells(self) -> None:
        while not self.council_q.empty():
            key, mint, why = self.council_q.get_nowait()
            if (key, mint) in self.positions:
                await self.sell(dict(key=key, mint=mint, why=["council"]), 1.0, f"council: {why}"[:160])

    def on_fill(self, item: dict) -> None:
        if item.get("shared") and item.get("side") in ("buy", "sell"):
            self.queue.put_nowait(dict(item))

    async def run(self, http: httpx.AsyncClient) -> None:
        self.http = http
        last_mark = last_retry = 0.0
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                item = None
            try:
                self._handle_request()
                await self._council_sells()
                if item is not None:
                    await (self.buy(item) if item["side"] == "buy" else self.sell(item))
                if self._retry and self.clock() - last_retry >= self.cfg.sell_retry_s:
                    last_retry = self.clock()
                    await self.retry_stuck_sells()
                if self.clock() - last_mark >= self.cfg.mark_every_s:
                    last_mark = self.clock()
                    await self.mark()
            except Exception:  # noqa: BLE001 - the dry run never stops trading
                log.exception("dry run step failed")

    # ── money ──────────────────────────────────────────────────────────────
    def equity(self) -> float:
        return self.cash + self.rent_held + sum(p.value_sol for p in self.positions.values())

    def _track(self, force: bool = False) -> None:
        e, now = self.equity(), self.clock()
        if e > self.high[0]:
            self.high = (e, now)
        if e < self.low[0]:
            self.low = (e, now)
        limit = self.cfg.loss_limit_pct
        if (not self.stopped and limit and self.start_sol and e <= self.start_sol * (1 - limit / 100)):
            self.stopped = dict(ts=now, equity_sol=e, reason=f"hit the {limit:g}% loss limit")
            self.tape.append(dict(ts=now, side="stopped", attempt=self.attempt, key="", mint="", symbol="",
                                  paper_price=None, why=f"wallet down {(1 - e / self.start_sol) * 100:.0f}%: no new buys"))
            log.warning("dry run: attempt %d hit the %g%% loss limit at %.4f SOL", self.attempt, limit, e)
        if force or not self.series or now - self.series[-1][0] >= SERIES_EVERY_S:
            self.series.append((now, e, self.cash))
            if len(self.series) > SERIES_MAX:           # thin the older half, keep the recent detail
                half = len(self.series) // 2
                self.series = self.series[:half:2] + self.series[half:]

    def _size(self, key: str, mult: float = 1.0) -> float:
        """A real-size bet: the champions' rule applied to this wallet, times the champion's whale boost."""
        pf = self.settings.portfolio
        want = pf.position_pct / 100 * self.equity() if pf.position_pct else 0.1
        return min(want * max(1.0, mult), pf.max_position_sol, self.cash - pf.reserve_sol)

    async def _pump_coin(self, mint: str) -> dict | None:
        """pump.fun's own record of a coin (what it is paired with, snipers...), read once per coin."""
        if mint in self._coin_cache:
            return self._coin_cache[mint]
        try:
            if self.http is None:
                self.http = httpx.AsyncClient()
            r = await self.http.get(PUMP_COIN + mint, timeout=6, headers={"User-Agent": "Mozilla/5.0", "Origin": "https://pump.fun"})
            d = r.json() if r.status_code == 200 else None
        except Exception:  # noqa: BLE001 - no answer: the buy is judged without it
            d = None
        if d:
            self._coin_cache[mint] = d
        return d

    async def _creator_history(self, creator: str) -> dict:
        from solmeme.sources.chaincheck import creator_history
        if self.http is None:
            self.http = httpx.AsyncClient()
        return await creator_history(self.http, creator)

    async def _safety(self, mint: str) -> str | None:
        """Last look before a live buy, from pump.fun itself (Oct 11, Zoroark: paired with CARDS, not SOL, so every
        price we had was off; it dumped 2 minutes later)."""
        if not mint.endswith("pump"):
            return None
        self._coin_cache.pop(mint, None)                  # market cap vs high must be read now
        d = await self._coin(mint) or {}
        q = d.get("quoteMint")
        if q and q not in SOL_QUOTES:
            return f"paired with another token ({q[:6]}…), not SOL: our prices for it are wrong"
        if not self.cfg.watcher:
            return None
        st = self.state_fn(mint) if self.state_fn else None
        trace = ((getattr(st, "ext", None) or {}).get("bundletrace")) or {}
        if trace.get("checked"):
            from solmeme.sources.bundletrace import bundled
            why = bundled(trace)
            if why:
                return why
        if d.get("dev") and self._dev_history is not None:
            from solmeme.sources.chaincheck import serial_launcher
            why = serial_launcher(await self._dev_history(d["dev"]))
            if why:
                return why
        mc, ath = float(d.get("marketCapUsd") or 0), float(d.get("athMarketCapUsd") or 0)
        drop = self.cfg.watcher_ath_drop_pct
        if mc > 0 and ath > 0 and mc < ath * (1 - drop / 100):
            return f"already {100 * (1 - mc / ath):.0f}% under its high on pump.fun (${mc:,.0f} vs ${ath:,.0f}): falling, not rising"
        return None

    def _watch(self, pos: "DryPos") -> str | None:
        """While holding: why this live position must go now (None = keep)."""
        if not self.cfg.watcher or pos.raw <= 0 or not pos.initial_raw:
            return None
        stop, max_hold = self.exit_plan_fn(pos.key) if self.exit_plan_fn else (None, None)
        stop = stop if stop is not None else self.cfg.watcher_hard_stop_pct
        basis = (pos.cost_sol - pos.fees_sol - self.settings.fills.ata_rent_sol) * pos.raw / pos.initial_raw
        if basis > 0 and pos.value_sol <= basis * (1 - stop / 100):
            return f"real value {100 * (pos.value_sol / basis - 1):+.0f}% hit the {stop:g}% stop (paper hadn't sold)"
        held = self.clock() - pos.entry_ts
        if max_hold and held > max_hold + self.cfg.watcher_hold_grace_s:
            return f"held {held / 60:.0f} min, past the strategy's {max_hold / 60:.0f} min limit"
        st = self.state_fn(pos.mint) if self.state_fn else None
        peak, bag = getattr(st, "dev_peak_balance", None), getattr(st, "dev_balance", None)
        if peak and bag is not None and (1 - bag / peak) * 100 >= self.cfg.watcher_dev_sold_pct:
            return f"the creator sold {100 * (1 - bag / peak):.0f}% of their bag"
        return None

    def _leg_cost(self, landed: bool) -> float:
        f = self.settings.fills
        return BASE_FEE_SOL + f.priority_fee_sol + (f.jito_tip_sol if landed else 0.0)

    def _log(self, side: str, item: dict, **kw) -> None:
        """One line on the live trades page: what happened, at what real price, against paper's."""
        self.tape.append(dict(ts=self.clock(), side=side, attempt=self.attempt, key=item.get("key"), mint=item.get("mint"),
                              symbol=item.get("symbol") or "", paper_price=float(item.get("price") or 0) or None, **kw))
        if len(self.tape) > TAPE_MAX:
            del self.tape[: len(self.tape) - TAPE_MAX]

    # ── trades ─────────────────────────────────────────────────────────────
    async def buy(self, item: dict) -> None:
        if self.stopped:                                 # past the loss limit: a real wallet would have stopped
            self.counts["after_stop"] += 1
            return
        pkey = (item["key"], item["mint"])
        if pkey in self.positions:
            self.counts["skipped"] += 1
            return
        from solmeme.ui.workflow import allowed, cap_size, load_permissions
        perm = load_permissions(self.settings.data_dir)          # TraderBot: the permissions you set
        px = float((self.market_fn() or (0,))[0] or 0) if self.market_fn else 0.0
        today = time.strftime("%Y-%m-%d")
        trades_today = sum(1 for t in self.tape if t.get("side") == "buy" and time.strftime("%Y-%m-%d", time.localtime(t["ts"])) == today)
        why = allowed(perm, item["key"], 0.0, px or None, len(self.positions), trades_today)
        if why:
            self.counts["not_permitted"] = self.counts.get("not_permitted", 0) + 1
            self._log("blocked", item, why=f"trader: {why}")
            return
        if self.overseer is not None:
            equity = self.equity()
            day = [e for t, e, *_ in self.series if t >= self.clock() - 86400]
            open_cost = sum(p.cost_sol - p.proceeds_sol for p in self.positions.values())
            why = self.overseer.check(item, equity, open_cost, day[0] if day else self.start_sol, self.closed)
            if why:
                self.counts["overseer"] = self.counts.get("overseer", 0) + 1
                self._log("blocked", item, why=f"overseer: {why}")
                return
        why = await self._safety(item["mint"])
        if why:
            self.counts["unsafe"] = self.counts.get("unsafe", 0) + 1
            self._log("blocked", item, why=f"safety: {why}")
            return
        size = cap_size(perm, self._size(item["key"], float(item.get("size_mult") or 1.0)), px or None)
        rent = self.settings.fills.ata_rent_sol
        if size < 0.01 or self.cash < size + rent + self._leg_cost(True) * 2:
            self.counts["no_cash"] += 1
            return
        try:
            q = await self._quote(SOL_MINT, item["mint"], int(size * LAMPORTS))
            dec = await self._decimals(item["mint"])
        except QuoteError as exc:
            self.counts["no_route" if "route" in str(exc).lower() else "quote_errors"] += 1
            self.counts["failed_buys"] += 1
            self.cash -= self._leg_cost(False)
            self._log("missed", item, sol=size, why=str(exc)[:160])
            self._journal_miss(item, size, str(exc))
            return
        tokens = q.out_raw / 10 ** dec
        price = size / tokens if tokens > 0 else float("inf")
        ref = float(item.get("ref_price") or item.get("price") or 0)
        if ref > 0 and 0 < price < ref * (1 - self.cfg.watcher_data_gap_pct / 100):
            # far CHEAPER than the price the strategy decided on: its data for this coin is broken (Oct 11, LARP, a bonk
            # coin: paper 2.5e-7, real 1.38e-7), so paper's exits for it would be noise too. A real wallet would skip it.
            self.counts["bad_data"] = self.counts.get("bad_data", 0) + 1
            self._log("missed", item, sol=size, price=price, why=f"data: real price {100 * (price / ref - 1):+.0f}% vs the "
                      f"decision price; the strategy's prices for this coin are wrong")
            self._journal_miss(item, size, f"bad data: real {price:.3g} vs decision {ref:.3g}")
            return
        if tokens <= 0 or (ref > 0 and price > ref * (1 + self.cfg.slippage_tolerance_pct / 100)):
            self.counts["slippage"] += 1
            self.counts["failed_buys"] += 1
            self.cash -= self._leg_cost(False)          # a real tx with min-out would revert, but still pay the fee
            self._log("missed", item, sol=size, price=price, impact_pct=q.impact_pct,
                      why=f"slippage: real price {100 * (price / ref - 1):+.0f}% vs the decision price" if ref else "slippage")
            self._journal_miss(item, size, f"slippage: real {price:.3g} vs decision {ref:.3g}")
            return
        raw, sandwiched = q.out_raw, False
        f = self.settings.fills
        if f.jito_tip_sol <= 0 and random.Random(f"{item['mint']}{self.clock()}").random() < f.sandwich_prob_unprotected:
            # no MEV protection: a real quote can't show a sandwich, so take it off the tokens, up to the slippage limit
            room = ref * (1 + self.cfg.slippage_tolerance_pct / 100) / price if ref > 0 else 2.0
            worse = max(1.0, min(1 + f.sandwich_extra_unprotected_pct / 100, room))
            raw, sandwiched = int(q.out_raw / worse), worse > 1.0
            tokens = raw / 10 ** dec
            price = size / tokens if tokens > 0 else float("inf")
        fees = self._leg_cost(True) + size * self.cfg.platform_fee_pct / 100
        self.cash -= size + fees + rent
        self.rent_held += rent
        self.positions[pkey] = DryPos(
            key=item["key"], mint=item["mint"], symbol=item.get("symbol") or "", raw=raw, decimals=dec,
            cost_sol=size + fees + rent, fees_sol=fees, entry_ts=self.clock(), entry_price=price,
            paper_entry_price=float(item.get("price") or 0), buy_impact_pct=q.impact_pct, initial_raw=raw,
            value_sol=size, marked_ts=self.clock(), route=q.route, path=[[self.clock(), size]],
            why=",".join(item.get("why") or [])[:240], peak_value_sol=size,
            entry_sol_usd=float((self.market_fn() or (0,))[0] or 0) if self.market_fn else 0.0)
        self.counts["buys"] += 1
        self._log("buy", item, sol=size, price=price, impact_pct=q.impact_pct, route="/".join(q.route), sandwiched=sandwiched,
                  why=",".join(item.get("why") or [])[:160], whales=item.get("whales"),
                  size_mult=float(item.get("size_mult") or 1.0))
        self._track(force=True)
        self.save()
        if self.review_fn is not None:                   # the council looks at every live buy (ai/ask.py review_buy)
            try:
                self.review_fn(dict(item))
            except Exception:  # noqa: BLE001
                log.exception("council review did not start")
        log.info("dry run BUY %s %.4f SOL at %.3g (paper %.3g, impact %.2f%%, %s)", item.get("symbol"),
                 size, price, float(item.get("price") or 0), q.impact_pct, "/".join(q.route))

    async def sell(self, item: dict, frac: float | None = None, reason: str | None = None) -> None:
        pos = self.positions.get((item["key"], item["mint"]))
        if pos is None:
            return
        frac = min(1.0, max(0.0, float(frac if frac is not None else item.get("frac") or 1.0)))
        raw = pos.raw if frac >= 0.999 else int(pos.raw * frac)
        why = reason or ",".join(item.get("why") or []) or "sell"
        if raw <= 0:
            return
        try:
            q = await self._quote(pos.mint, SOL_MINT, raw)
        except QuoteError as exc:
            pos.sell_fails += 1
            self.counts["sell_retries"] += 1
            self.cash -= self._leg_cost(False)
            if pos.sell_fails > self.cfg.sell_retries:
                self._log("stuck", item, sol=0.0, why=f"no buyer after {pos.sell_fails} tries: {str(exc)[:100]}",
                          pnl_sol=-(pos.cost_sol - pos.proceeds_sol))
                self._close(pos, why, stuck=True, paper=item, note=str(exc))
            else:
                self._log("sell_failed", item, frac=frac, why=f"try {pos.sell_fails}: {str(exc)[:120]}")
            if pos.sell_fails <= self.cfg.sell_retries:
                self._retry[(pos.key, pos.mint)] = (frac, why, item)     # try again in sell_retry_s
            self.save()
            return
        sol_out = q.out_raw / LAMPORTS
        fees = self._leg_cost(True) + sol_out * self.cfg.platform_fee_pct / 100
        self.cash += sol_out - fees
        pos.proceeds_sol += sol_out - fees
        pos.fees_sol += fees
        pos.raw -= raw
        pos.sell_fails = 0
        self.counts["sells"] += 1
        exit_price = sol_out / (raw / 10 ** pos.decimals)
        closing = pos.raw <= 0 or frac >= 0.999
        held_after = 0.0 if closing else pos.value_sol * (1 - frac)
        pos.path = (pos.path + [[self.clock(), sol_out + held_after]])[-PATH_MAX:]
        self._log("sell", item, sol=sol_out, price=exit_price, impact_pct=q.impact_pct, frac=frac, partial=not closing,
                  why=why, x=exit_price / pos.entry_price if pos.entry_price else None,
                  pnl_sol=(pos.proceeds_sol + self.settings.fills.ata_rent_sol - pos.cost_sol) if closing else None,
                  paper_pnl_pct=item.get("pnl_pct") if closing else None)
        if closing:
            self._close(pos, why, paper=item, exit_price=exit_price, sell_impact=q.impact_pct)
        else:
            pos.value_sol = pos.value_sol * (1 - frac)
        self._track(force=True)
        self.save()
        log.info("dry run SELL %s %.0f%% for %.4f SOL at %.3g (paper %.3g, impact %.2f%%)", pos.symbol,
                 frac * 100, sol_out, exit_price, float(item.get("price") or 0), q.impact_pct)

    async def retry_stuck_sells(self) -> None:
        """Sells that found no buyer, tried again (the newest paper sell for that coin wins)."""
        for pkey, (frac, why, item) in list(self._retry.items()):
            self._retry.pop(pkey, None)
            if pkey in self.positions:
                await self.sell(item, frac, why)

    def _close(self, pos: DryPos, why: str, stuck: bool = False, paper: dict | None = None,
               exit_price: float | None = None, sell_impact: float | None = None, note: str = "") -> None:
        self.positions.pop((pos.key, pos.mint), None)
        self._retry.pop((pos.key, pos.mint), None)
        rent = self.settings.fills.ata_rent_sol
        self.rent_held = max(0.0, self.rent_held - rent)
        if not stuck:
            self.cash += rent                           # closing the token account refunds its rent
            pos.proceeds_sol += rent
        else:
            self.counts["stuck"] += 1
        pnl = pos.proceeds_sol - pos.cost_sol
        row = dict(
            mint=pos.mint, symbol=pos.symbol, key=pos.key, entry_ts=pos.entry_ts, exit_ts=self.clock(),
            size_sol=pos.cost_sol, proceeds_sol=pos.proceeds_sol, pnl_sol=pnl,
            pnl_pct=pnl / pos.cost_sol * 100 if pos.cost_sol else 0.0, fees_sol=pos.fees_sol,
            entry_price=pos.entry_price, exit_price=exit_price, paper_entry_price=pos.paper_entry_price,
            paper_exit_price=float((paper or {}).get("price") or 0) or None,
            paper_pnl_pct=(paper or {}).get("pnl_pct"), paper_trade_id=(paper or {}).get("trade_id"),
            buy_impact_pct=pos.buy_impact_pct, sell_impact_pct=sell_impact, route="/".join(pos.route),
            exit_reason=why, status="stuck" if stuck else "closed", note=note[:200], attempt=self.attempt)
        self.closed.append(dict(row, path=(pos.path + ([[self.clock(), 0.0]] if stuck else []))[-PATH_MAX:]))
        self.closed = self.closed[-200:]
        if self.store is not None:
            try:
                self.store.add_dryrun_trade(row)
            except Exception:  # noqa: BLE001
                log.exception("dry run trade not stored")

    def new_attempt(self, sol_usd: float, paper_equity_sol: float | None, why: str = "") -> dict:
        """End this attempt (open positions sold at their real value) and start a fresh real-size wallet.
        The summary is kept; the fill tape is kept too, so the paper calibration learns from every attempt."""
        for pos in list(self.positions.values()):
            fees = self._leg_cost(True) + pos.value_sol * self.cfg.platform_fee_pct / 100
            self.cash += pos.value_sol - fees
            pos.proceeds_sol += pos.value_sol - fees
            pos.fees_sol += fees
            self._close(pos, "attempt ended", exit_price=None)
        e = self.equity()
        done = [t for t in self.closed if t["status"] != "missed"]
        summary = dict(
            attempt=self.attempt, started_ts=self.started_ts, ended_ts=self.clock(), start_usd=self.cfg.start_usd,
            start_sol=self.start_sol, end_sol=e, end_usd=e * sol_usd if sol_usd else None,
            pnl_pct=(e / self.start_sol - 1) * 100 if self.start_sol else None,
            paper_pnl_pct=((self.paper_factor * paper_equity_sol / self.paper_start_sol - 1) * 100
                           if paper_equity_sol and self.paper_start_sol else None),
            trades=len(done), won=sum(1 for t in done if t["pnl_sol"] > 0), counts=dict(self.counts),
            ended_by=(self.stopped or {}).get("reason") or why or "a new attempt was started")
        self.attempts.append(summary)
        log.info("dry run: attempt %d ended (%s), %+.1f%%", self.attempt, summary["ended_by"], summary["pnl_pct"] or 0)
        self.attempt += 1
        self.positions, self.closed, self._retry = {}, [], {}
        self.counts, self.stopped, self.rent_held = self._zero_counts(), None, 0.0
        self.start_fresh(sol_usd, paper_equity_sol)
        return summary

    def _handle_request(self) -> None:
        """data/session/dryrun_request.json, written by the dashboard button or `solmeme new-attempt`."""
        path = self.settings.data_dir / "session" / REQUEST_FILE
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        path.unlink(missing_ok=True)
        if req.get("action") == "new_attempt" and self.market_fn is not None:
            px, paper = self.market_fn()
            if px:
                self.new_attempt(px, paper, str(req.get("why") or "")[:200])

    def _journal_miss(self, item: dict, size: float, why: str) -> None:
        self.closed.append(dict(mint=item["mint"], symbol=item.get("symbol") or "", key=item["key"],
                                entry_ts=self.clock(), exit_ts=self.clock(), size_sol=0.0, proceeds_sol=0.0,
                                pnl_sol=-self._leg_cost(False), pnl_pct=None, fees_sol=self._leg_cost(False),
                                entry_price=None, exit_price=None, paper_entry_price=float(item.get("price") or 0),
                                paper_exit_price=None, paper_pnl_pct=None, paper_trade_id=None,
                                buy_impact_pct=None, sell_impact_pct=None, route="", exit_reason="buy failed",
                                status="missed", note=f"{size:.4f} SOL: {why}"[:200]))
        self.closed = self.closed[-200:]
        self.save()

    async def mark(self) -> None:
        """Re-value open positions with what a real sell would return right now."""
        for pos in list(self.positions.values()):
            if pos.raw <= 0:
                continue
            quoted = True
            try:
                q = await self._quote(pos.mint, SOL_MINT, pos.raw)
                pos.value_sol = q.out_raw / LAMPORTS
            except QuoteError:
                pos.value_sol = 0.0                     # nobody would buy it right now
                quoted = False
            pos.marked_ts = self.clock()
            pos.peak_value_sol = max(pos.peak_value_sol or 0.0, pos.value_sol)
            pos.path = (pos.path + [[pos.marked_ts, pos.value_sol]])[-PATH_MAX:]
            # a failed quote (a Jupiter 5xx, no route for a moment) reads as "worth 0": never a stop on that (Oct 11, CRC
            # was sold for "-100%" and filled at 0.85x); the next good quote decides
            why = self._watch(pos) if quoted else None
            if why:
                self.counts["watcher_sells"] = self.counts.get("watcher_sells", 0) + 1
                log.warning("watcher: selling %s: %s", pos.symbol, why)
                await self.sell(dict(key=pos.key, mint=pos.mint, symbol=pos.symbol, why=["watcher"]), 1.0, f"watcher: {why}"[:160])
        self._track()
        self.save()

    # ── Jupiter ────────────────────────────────────────────────────────────
    async def _jupiter_quote(self, in_mint: str, out_mint: str, amount: int) -> Quote:
        assert self.http is not None
        params = dict(inputMint=in_mint, outputMint=out_mint, amount=str(amount),
                      slippageBps=str(int(self.cfg.slippage_tolerance_pct * 100)), restrictIntermediateTokens="true")
        for attempt in range(3):
            t0 = time.time()
            try:
                r = await self.http.get(self.cfg.quote_url, params=params, timeout=self.cfg.timeout_s)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise QuoteError(f"quote request failed: {exc}") from exc
                await asyncio.sleep(1 + attempt)
                continue
            if r.status_code == 429 or (r.status_code >= 500 and attempt < 2):
                await asyncio.sleep(1 + attempt if r.status_code >= 500 else 2 + 2 * attempt)
                continue                                  # a 503 from the quote API is a blip, not "no buy" (BUNKER, Oct 10)
            try:
                d = r.json()
            except ValueError as exc:
                raise QuoteError(f"bad quote reply ({r.status_code})") from exc
            if r.status_code >= 400 or "outAmount" not in d:
                msg = str(d.get("error") or d.get("errorCode") or d)[:160]
                raise QuoteError(("no route: " if "route" in msg.lower() or r.status_code in (400, 404) else "") + msg)
            return Quote(out_raw=int(d["outAmount"]), impact_pct=100 * float(d.get("priceImpactPct") or 0),
                         route=[(x.get("swapInfo") or {}).get("label") or "?" for x in d.get("routePlan") or []],
                         ms=(time.time() - t0) * 1000)
        raise QuoteError("rate limited by the quote API")

    async def _jupiter_decimals(self, mint: str) -> int:
        if mint in self._dec_cache:
            return self._dec_cache[mint]
        dec = 6 if mint.endswith("pump") else None      # every pump.fun token has 6 decimals
        if dec is None and self.http is not None:
            try:
                r = await self.http.get(self.cfg.token_url, params={"query": mint}, timeout=self.cfg.timeout_s)
                hit = next((t for t in r.json() if t.get("id") == mint), None)
                dec = int(hit["decimals"]) if hit and hit.get("decimals") is not None else None
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                dec = None
        self._dec_cache[mint] = dec if dec is not None else 6
        return self._dec_cache[mint]

    # ── the dashboard ──────────────────────────────────────────────────────
    def summary(self, sol_usd: float | None, paper_equity_sol: float | None) -> dict:
        """The few numbers the header and sidebar show (sent every second)."""
        e = self.equity()
        return dict(enabled=True, started_ts=self.started_ts, start_sol=self.start_sol, start_usd=self.cfg.start_usd,
                    equity_sol=e, equity_usd=e * sol_usd if sol_usd else None,
                    pnl_pct=(e / self.start_sol - 1) * 100 if self.start_sol else None,
                    paper_pnl_pct=((self.paper_factor * paper_equity_sol / self.paper_start_sol - 1) * 100
                                   if paper_equity_sol and self.paper_start_sol else None),
                    open=len(self.positions), trades=sum(1 for t in self.closed if t["status"] != "missed"),
                    attempt=self.attempt, stopped=(self.stopped or {}).get("reason"))

    def readiness(self, trades: list[dict]) -> list[dict]:
        """Would real money be a good idea? Plain pass/fail checks on the dry run's own record."""
        done = [t for t in trades if t.get("status") in ("closed", "stuck")]
        pnl = sorted(float(t["pnl_sol"] or 0) for t in done)
        days = (self.clock() - self.started_ts) / 86400 if self.started_ts else 0.0
        peak, dd = -1e18, 0.0
        for pt in self.series:
            e = pt[1]
            peak = max(peak, e)
            dd = max(dd, 1 - e / peak if peak > 0 else 0.0)
        stuck = sum(1 for t in done if t["status"] == "stuck")
        e = self.equity()
        return [
            dict(id="sample", ok=len(done) >= 50 and days >= 3, label="Enough evidence",
                 detail=f"{len(done)} trades over {days:.1f} days (want 50+ trades over 3+ days, including a weekend)"),
            dict(id="profit", ok=e > self.start_sol, label="Profitable after real costs",
                 detail=f"wallet {(e / self.start_sol - 1) * 100 if self.start_sol else 0:+.1f}% since the start, fees and price impact included"),
            dict(id="breadth", ok=len(pnl) > 6 and sum(pnl[:-3]) > 0, label="Not carried by 2-3 lucky coins",
                 detail=(f"without the best 3 trades: {sum(pnl[:-3]):+.3f} SOL" if len(pnl) > 3 else "needs more trades")),
            dict(id="drawdown", ok=dd < 0.4 and len(self.series) > 1, label="A drawdown you could live with",
                 detail=f"worst fall from a high: {dd * 100:.0f}% (want under 40%)"),
            dict(id="stuck", ok=bool(done) and stuck / len(done) < 0.1, label="Coins can actually be sold",
                 detail=f"{stuck} of {len(done)} trades could not be sold (want under 10%)"),
        ]

    def view(self, sol_usd: float | None, paper_equity_sol: float | None) -> dict:
        e = self.equity()
        done = [t for t in self.closed if t["status"] != "missed"]
        won = sum(1 for t in done if t["pnl_sol"] > 0)
        paired = [t for t in done if t.get("paper_pnl_pct") is not None and t.get("pnl_pct") is not None]
        gap = (sum(t["pnl_pct"] - t["paper_pnl_pct"] for t in paired) / len(paired)) if paired else None
        entry_gap = [100 * (t["entry_price"] / t["paper_entry_price"] - 1) for t in done
                     if t.get("entry_price") and t.get("paper_entry_price")]
        impacts = [t["buy_impact_pct"] for t in done if t.get("buy_impact_pct") is not None]
        return dict(
            enabled=True, started_ts=self.started_ts, start_sol=self.start_sol, start_usd=self.cfg.start_usd,
            equity_sol=e, equity_usd=e * sol_usd if sol_usd else None, cash_sol=self.cash,
            pnl_pct=(e / self.start_sol - 1) * 100 if self.start_sol else None,
            paper_pnl_pct=((self.paper_factor * paper_equity_sol / self.paper_start_sol - 1) * 100
                           if paper_equity_sol and self.paper_start_sol else None),
            high_sol=self.high[0], low_sol=self.low[0], counts=self.counts, trades=len(done), won=won,
            avg_gap_pct=gap, avg_entry_gap_pct=sum(entry_gap) / len(entry_gap) if entry_gap else None,
            avg_buy_impact_pct=sum(impacts) / len(impacts) if impacts else None,
            open=[dict(symbol=p.symbol, mint=p.mint, key=p.key, cost_sol=p.cost_sol, value_sol=p.value_sol,
                       entry_ts=p.entry_ts, marked_ts=p.marked_ts, impact_pct=p.buy_impact_pct,
                       entry_gap_pct=100 * (p.entry_price / p.paper_entry_price - 1) if p.paper_entry_price else None,
                       sell_fails=p.sell_fails, route="/".join(p.route), path=p.path, why=p.why,
                       entry_price=p.entry_price, sol_in=p.cost_sol - p.fees_sol - self.settings.fills.ata_rent_sol,
                       proceeds_sol=p.proceeds_sol, sold_pct=100 * (1 - p.raw / p.initial_raw) if p.initial_raw else 0.0,
                       entry_mcap_usd=p.entry_price * 1e9 * p.entry_sol_usd if p.entry_sol_usd else None,
                       peak_value_sol=max(p.peak_value_sol or 0.0, p.value_sol))
                  for p in self.positions.values()],
            recent=list(reversed(self.closed[-30:])),
            tape=list(reversed([t for t in self.tape if t.get("attempt", 1) == self.attempt][-300:])),
            attempt=self.attempt, attempts=list(reversed(self.attempts)), stopped=self.stopped,
            loss_limit_pct=self.cfg.loss_limit_pct,
        )

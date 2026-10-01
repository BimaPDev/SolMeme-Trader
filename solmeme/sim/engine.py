"""Deterministic engine shared by live paper trading and replay.

step(event):
  1. fire every scheduled timer due at or before event.ts (each "as of" its due time)
  2. apply the event to MarketState
  3. for every strategy version: manage open positions on the touched token,
     then evaluate entry rules if the event kind is one of its triggers.

No I/O, no wall clock, per-order seeded RNG. Live and replay call the same
step(), so a replay of a recorded session reproduces the live journal.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field
from typing import Callable

from solmeme.config import Settings
from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState
from solmeme.sim.fills import Fill, FillModel, order_rng
from solmeme.sim.portfolio import Portfolio, Position
from solmeme.strategy.dsl import evaluate, in_universe
from solmeme.strategy.spec import Cond, StrategySpec

CHECK_EVERY_S = 10.0
NEAR_MISS_TRACK_S = 1800.0


@dataclass
class Order:
    side: str
    mint: str
    ref_price: float
    decided_ts: float
    sol: float = 0.0
    tokens: float = 0.0
    reason: str = ""
    attempt: int = 0
    n: int = 0
    features: dict = field(default_factory=dict)


@dataclass
class Tracker:
    """Follows a token's price after an exit or a near-miss (for the journal / AI)."""
    kind: str
    mint: str
    ref_price: float
    start_ts: float
    end_ts: float
    payload: dict
    max_p: float = 0.0
    min_p: float = 0.0
    max_ts: float = 0.0


class Runner:
    def __init__(self, spec: StrategySpec, starting_sol: float, label: str = ""):
        self.spec = spec
        self.key = spec.key
        self.label = label                      # champion | challenger | candidate
        self.portfolio = Portfolio(starting_sol)
        self.entries: dict[str, int] = {}
        self.pending_buys: set[str] = set()
        self.near_missed: set[str] = set()
        self.order_n = itertools.count()
        self.failed_buy_cost = 0.0
        self.failed_buys = 0


class Journal:
    """In-memory sink. Persisting to SQLite is optional (live/backtest runs)."""

    def __init__(self, store=None, run_id: str | None = None):
        self.store, self.run_id = store, run_id
        self.trades: list[dict] = []
        self.orders: list[dict] = []
        self.near_misses: list[dict] = []

    def trade(self, row: dict) -> None:
        self.trades.append(row)
        if self.store:
            self.store.insert_trade({**row, "run_id": self.run_id})

    def update_trade(self, row: dict) -> None:
        # row is the same dict already in self.trades (mutated in place)
        if self.store:
            self.store.insert_trade({**row, "run_id": self.run_id})

    def order(self, row: dict) -> None:
        self.orders.append(row)
        if self.store:
            self.store.insert_order({**row, "run_id": self.run_id})

    def near_miss(self, row: dict) -> None:
        self.near_misses.append(row)
        if self.store:
            self.store.insert_near_miss({**{k: row[k] for k in (
                "strategy_id", "version", "mint", "ts", "failed_rule", "price", "mcap_sol")}, "run_id": self.run_id})


class Engine:
    def __init__(self, specs: list[tuple[StrategySpec, str]], settings: Settings, journal: Journal | None = None,
                 seed: int = 7, record_near_misses: bool = True, snapshot_features: bool = True,
                 activity=None):
        self.settings = settings
        self.market = MarketState(excluded=set(settings.excluded_mints))
        self.fills = FillModel(settings.fills)
        self.journal = journal or Journal()
        self.seed = seed
        self.record_near_misses = record_near_misses
        self.snapshot_features = snapshot_features
        self.activity = activity
        self.runners: list[Runner] = [Runner(s, settings.portfolio.starting_sol, lbl) for s, lbl in specs]
        self._timers: list = []
        self._tseq = itertools.count()
        self.now = 0.0
        self.trackers: dict[str, list[Tracker]] = {}
        self.events = 0
        self.hooks: list = []
        pf = settings.prefilter
        self.prefilter_all = [Cond(**c) for c in pf.all] if pf.enabled else []
        self.prefilter_none = [Cond(**c) for c in pf.none] if pf.enabled else []
        self.prefilter_blocked: dict[str, int] = {}

    def _passes_prefilter(self, fv: FeatureView) -> str | None:
        """Blocking condition id, or None when the token passes."""
        from solmeme.strategy.dsl import eval_cond
        for c in self.prefilter_all:
            if not eval_cond(c, fv.get(c.f), c.v):
                self.prefilter_blocked[c.id] = self.prefilter_blocked.get(c.id, 0) + 1
                return c.id
        for c in self.prefilter_none:
            if eval_cond(c, fv.get(c.f), c.v):
                self.prefilter_blocked[c.id] = self.prefilter_blocked.get(c.id, 0) + 1
                return c.id
        return None

    def _activity_note(self, mint: str, phase: str, detail: str, *, symbol: str = "", ts: float = 0.0) -> None:
        if self.activity is None:
            return
        self.activity.note(mint, phase, detail, symbol=symbol or "", ts=ts)

    # ── scheduling ─────────────────────────────────────────────────────────
    def schedule(self, due: float, fn: Callable[[float], None]) -> None:
        heapq.heappush(self._timers, (due, next(self._tseq), fn))

    def advance(self, ts: float) -> None:
        while self._timers and self._timers[0][0] <= ts:
            due, _, fn = heapq.heappop(self._timers)
            self.now = max(self.now, due)
            fn(due)
        self.now = max(self.now, ts)

    # ── main entry ─────────────────────────────────────────────────────────
    def step(self, ev) -> None:
        self.advance(ev.ts)
        self.events += 1
        st = self.market.apply(ev)
        if st is None:
            return
        now = ev.ts
        self._update_trackers(st, now)
        for hook in list(self.hooks):
            hook(ev, st)
        fv: FeatureView | None = None
        prefilter_block: str | None = None
        prefilter_checked = False
        buy_ids: list[str] = []
        near: tuple[str, str] | None = None
        for r in self.runners:
            pos = r.portfolio.positions.get(st.mint)
            if pos is not None:
                self._manage_position(r, pos, st, now)
            if ev.kind not in r.spec.entry.triggers:
                continue
            if not self._can_enter(r, st.mint, now):
                continue
            if not in_universe(r.spec, st, now):
                continue
            if fv is None:
                fv = FeatureView(st, now, self.market)
            if not prefilter_checked:
                prefilter_block = self._passes_prefilter(fv)
                prefilter_checked = True
            if prefilter_block is not None:
                continue        # still loop: other runners may hold this token and need exit checks
            explain = self.record_near_misses and st.mint not in r.near_missed
            ok, failed = evaluate(r.spec, fv, explain=explain)
            if ok:
                was_pending = st.mint in r.pending_buys
                self._place_buy(r, st, now, fv)
                if not was_pending and st.mint in r.pending_buys:
                    buy_ids.append(r.spec.id)
            elif explain and len(failed) == 1:
                if near is None:
                    near = (r.spec.id, failed[0])
                self._near_miss(r, st, now, failed[0])
        if fv is not None:
            if prefilter_block is not None:
                phase, detail = "skipped", f"prefilter {prefilter_block}"
            elif buy_ids:
                phase, detail = "buying", ", ".join(buy_ids)
            elif near is not None:
                phase, detail = "analyzing", f"near-miss {near[0]} {near[1]}"
            else:
                phase, detail = "analyzing", "no entry"
            self._activity_note(st.mint, phase, detail, symbol=st.symbol or "", ts=now)

    # ── entries ────────────────────────────────────────────────────────────
    def _can_enter(self, r: Runner, mint: str, now: float) -> bool:
        if r.label == "retired":
            return False
        if mint in r.portfolio.positions or mint in r.pending_buys:
            return False
        if r.entries.get(mint, 0) >= r.spec.sizing.max_entries_per_token:
            return False
        if len(r.portfolio.positions) + len(r.pending_buys) >= r.spec.sizing.max_open:
            return False
        if r.portfolio.day_loss(now) >= self.settings.portfolio.daily_loss_limit_sol:
            return False
        return True

    def _size(self, r: Runner) -> float:
        want = float(r.spec.p(r.spec.sizing.sol))
        size = min(want, self.settings.portfolio.max_position_sol, r.portfolio.cash - 0.05)
        return size if size >= 0.01 else 0.0

    def _place_buy(self, r: Runner, st, now: float, fv: FeatureView) -> None:
        size = self._size(r)
        if size <= 0:
            return
        n = next(r.order_n)
        o = Order("buy", st.mint, st.price_sol, now, sol=size, reason="entry", n=n,
                  features=fv.snapshot() if self.snapshot_features else {})
        r.pending_buys.add(st.mint)
        r.entries[st.mint] = r.entries.get(st.mint, 0) + 1
        rng = order_rng(self.seed, r.key, st.mint, n)
        self.schedule(now + self.fills.latency_s(rng), lambda due, r=r, o=o, rng=rng: self._exec_buy(r, o, rng, due))

    def _exec_buy(self, r: Runner, o: Order, rng, due: float) -> None:
        r.pending_buys.discard(o.mint)
        st = self.market.get(o.mint)
        if st is None:
            return
        f = self.fills.buy(st, o.sol, o.ref_price, rng)
        self.journal.order(dict(strategy_id=r.spec.id, version=r.spec.version, mint=o.mint, side="buy", ts=due,
                                status="filled" if f.ok else "failed", reason=f.reason, cost_sol=f.cost_sol))
        if not f.ok:
            r.portfolio.charge(f.cost_sol)
            r.failed_buy_cost += f.cost_sol
            r.failed_buys += 1
            return
        pos = Position(
            trade_id=f"{r.key}:{o.mint}:{o.n}", mint=o.mint, symbol=st.symbol, entry_ts=due,
            entry_price=f.price, tokens=f.tokens, initial_tokens=f.tokens, cost_sol=f.sol + f.cost_sol,
            rent_sol=self.settings.fills.ata_rent_sol, fees_sol=f.cost_sol + f.sol * self._swap_fee(st),
            peak_price=st.price_sol or f.price, trough_price=st.price_sol or f.price,
            entry_features=o.features, kol_sells_at_entry=len({w for _, w, _ in st.wallet_sells}),
            dev_sold_at_entry=st.dev_sold,
            fills=[dict(side="buy", ts=due, sol=f.sol, tokens=f.tokens, price=f.price, venue=f.venue,
                        decided_ts=o.decided_ts, ref_price=o.ref_price, sandwiched=f.meta.get("sandwiched"))],
        )
        r.portfolio.open(pos, f.sol, f.cost_sol)
        self.schedule(due + CHECK_EVERY_S, lambda t, r=r, mint=o.mint, tid=pos.trade_id: self._check(r, mint, tid, t))

    def _swap_fee(self, st) -> float:
        c = self.settings.fills
        return (c.amm_fee_pct(st.mcap_sol) if st.migrated else c.curve_fee_pct) / 100

    # ── exits ──────────────────────────────────────────────────────────────
    def _check(self, r: Runner, mint: str, trade_id: str, now: float) -> None:
        pos = r.portfolio.positions.get(mint)
        if pos is None or pos.trade_id != trade_id:
            return
        st = self.market.get(mint)
        if st is not None:
            self._manage_position(r, pos, st, now)
        if mint in r.portfolio.positions:
            self.schedule(now + CHECK_EVERY_S, lambda t: self._check(r, mint, trade_id, t))

    def _manage_position(self, r: Runner, pos: Position, st, now: float) -> None:
        if pos.selling or st.price_sol is None:
            return
        px = st.price_sol
        pos.peak_price = max(pos.peak_price, px)
        pos.trough_price = min(pos.trough_price, px) if pos.trough_price else px
        reason, pct = self._exit_reason(r.spec, pos, st, now)
        if reason is None:
            return
        if reason == "stale" and st.migrated and st.amm is None:
            # no reserves and no fresh price: assume a haircut dump
            self._stale_exit(r, pos, st, now)
            return
        tokens = pos.tokens if pct >= 100 else min(pos.tokens, pos.initial_tokens * pct / 100)
        if tokens <= 0:
            return
        pos.selling = True
        self._activity_note(st.mint, "selling", f"{r.spec.id} {reason}",
                            symbol=pos.symbol or st.symbol or "", ts=now)
        n = next(r.order_n)
        o = Order("sell", st.mint, px, now, tokens=tokens, reason=reason, n=n)
        rng = order_rng(self.seed, r.key, st.mint, n)
        self.schedule(now + self.fills.latency_s(rng), lambda due, r=r, o=o, rng=rng: self._exec_sell(r, o, rng, due))

    def _exit_reason(self, spec: StrategySpec, pos: Position, st, now: float) -> tuple[str | None, float]:
        ex = spec.exits
        p = spec.p
        px = st.price_sol
        x = px / pos.entry_price if pos.entry_price else 1.0
        if ex.on_rug:
            past = st.price_at_or_before(now - 60)
            if past and px / past <= 0.4:
                return "rug", 100
        if ex.on_dev_sell and st.dev_sold and not pos.dev_sold_at_entry:
            return "dev_sold", 100
        if ex.on_kol_sell and len({w for _, w, _ in st.wallet_sells}) > pos.kol_sells_at_entry:
            return "kol_sold", 100
        if ex.stop_loss_pct is not None and x <= 1 - float(p(ex.stop_loss_pct)) / 100:
            return "stop_loss", 100
        if ex.trailing_pct is not None:
            peak_x = pos.peak_price / pos.entry_price
            if peak_x >= float(p(ex.trailing_activate_x)) and px <= pos.peak_price * (1 - float(p(ex.trailing_pct)) / 100):
                return "trailing_stop", 100
        if pos.tp_done < len(ex.take_profit):
            lvl = ex.take_profit[pos.tp_done]
            if x >= float(p(lvl.at_x)):
                last = pos.tp_done == len(ex.take_profit) - 1
                return f"tp{pos.tp_done + 1}", 100 if last else float(p(lvl.sell_pct))
        if ex.on_migration == "sell" and st.migrated:
            return "migrated", 100
        if ex.max_hold_s is not None and now - pos.entry_ts >= float(p(ex.max_hold_s)):
            return "time_stop", 100
        if ex.stale_after_s is not None and st.price_ts and now - st.price_ts >= float(p(ex.stale_after_s)):
            return "stale", 100
        return None, 0

    def _exec_sell(self, r: Runner, o: Order, rng, due: float) -> None:
        pos = r.portfolio.positions.get(o.mint)
        if pos is None:
            return
        st = self.market.get(o.mint)
        force = o.attempt >= self.settings.fills.sell_max_retries
        f = self.fills.sell(st, min(o.tokens, pos.tokens), o.ref_price, rng, force=force)
        self.journal.order(dict(strategy_id=r.spec.id, version=r.spec.version, mint=o.mint, side="sell", ts=due,
                                status="filled" if f.ok else "failed", reason=f"{o.reason}:{f.reason}", cost_sol=f.cost_sol))
        if not f.ok:
            r.portfolio.charge(f.cost_sol)
            pos.cost_sol += f.cost_sol
            pos.fees_sol += f.cost_sol
            pos.failed_attempts += 1
            retry = Order("sell", o.mint, st.price_sol or o.ref_price, due, tokens=o.tokens, reason=o.reason,
                          attempt=o.attempt + 1, n=next(r.order_n))
            rng2 = order_rng(self.seed, r.key, o.mint, retry.n)
            self.schedule(due + self.fills.latency_s(rng2), lambda t: self._exec_sell(r, retry, rng2, t))
            return
        self._apply_sell(r, pos, st, f, o.reason, due)

    def _stale_exit(self, r: Runner, pos: Position, st, now: float) -> None:
        self._activity_note(st.mint, "selling", f"{r.spec.id} stale",
                            symbol=pos.symbol or st.symbol or "", ts=now)
        f = self.fills.stale_sell(st, pos.tokens)
        self._apply_sell(r, pos, st, f, "stale", now)

    def _apply_sell(self, r: Runner, pos: Position, st, f, reason: str, ts: float) -> None:
        pos.selling = False
        pos.fills.append(dict(side="sell", ts=ts, sol=f.sol, tokens=f.tokens, price=f.price, venue=f.venue, reason=reason))
        r.portfolio.reduce(pos.mint, f.tokens, f.sol, f.cost_sol)
        if reason.startswith("tp") and pos.tokens > 1e-6:
            pos.tp_done += 1
            return
        if pos.tokens > pos.initial_tokens * 1e-6 and not reason.startswith("tp"):
            # partial dust left from rounding: dump it with this exit
            pos.tokens = 0.0
        self._close(r, pos, st, reason, ts)

    def _close(self, r: Runner, pos: Position, st, reason: str, ts: float) -> None:
        pos, pnl = r.portfolio.close(pos.mint, ts)
        exit_price = pos.fills[-1]["price"] if pos.fills else 0.0
        fv = FeatureView(st, ts, self.market) if st is not None else None
        row = dict(
            trade_id=pos.trade_id, strategy_id=r.spec.id, version=r.spec.version, mint=pos.mint,
            symbol=pos.symbol or (st.symbol if st else ""), entry_ts=pos.entry_ts, exit_ts=ts,
            entry_price=pos.entry_price, exit_price=exit_price, size_sol=pos.cost_sol,
            proceeds_sol=pos.proceeds_sol, pnl_sol=pnl, pnl_pct=pnl / pos.cost_sol * 100 if pos.cost_sol else 0.0,
            fees_sol=pos.fees_sol, exit_reason=reason, hold_s=ts - pos.entry_ts,
            mfe_x=pos.peak_price / pos.entry_price if pos.entry_price else None,
            mae_x=pos.trough_price / pos.entry_price if pos.entry_price else None,
            peak_after_exit_x=None, failed_attempts=pos.failed_attempts,
            entry_features=pos.entry_features,
            exit_features=fv.snapshot() if (fv and self.snapshot_features) else {},
            fills=pos.fills,
        )
        self.journal.trade(row)
        tr = Tracker("post_exit", pos.mint, st.price_sol if st and st.price_sol else exit_price, ts,
                     ts + self.settings.record.post_exit_track_s, {"row": row})
        tr.max_p = tr.min_p = tr.ref_price
        self.trackers.setdefault(pos.mint, []).append(tr)
        self.schedule(tr.end_ts, lambda t, tr=tr: self._finish_tracker(tr))

    # ── trackers (post-exit path, near-miss outcomes) ──────────────────────
    def _near_miss(self, r: Runner, st, now: float, failed_rule: str) -> None:
        r.near_missed.add(st.mint)
        row = dict(strategy_id=r.spec.id, version=r.spec.version, mint=st.mint, symbol=st.symbol, ts=now,
                   failed_rule=failed_rule, price=st.price_sol, mcap_sol=st.mcap_sol)
        tr = Tracker("near_miss", st.mint, st.price_sol, now, now + NEAR_MISS_TRACK_S, {"row": row})
        tr.max_p = tr.min_p = st.price_sol
        self.trackers.setdefault(st.mint, []).append(tr)
        self.schedule(tr.end_ts, lambda t, tr=tr: self._finish_tracker(tr))

    def _update_trackers(self, st, now: float) -> None:
        trs = self.trackers.get(st.mint)
        if not trs or st.price_sol is None:
            return
        for tr in trs:
            if st.price_sol > tr.max_p:
                tr.max_p, tr.max_ts = st.price_sol, now
            tr.min_p = min(tr.min_p, st.price_sol)

    def _finish_tracker(self, tr: Tracker) -> None:
        trs = self.trackers.get(tr.mint)
        if trs and tr in trs:
            trs.remove(tr)
            if not trs:
                del self.trackers[tr.mint]
        else:
            return
        row = tr.payload["row"]
        if tr.kind == "post_exit":
            row["peak_after_exit_x"] = tr.max_p / row["exit_price"] if row.get("exit_price") else None
            self.journal.update_trade(row)
        else:
            row["max_x_after"] = tr.max_p / tr.ref_price if tr.ref_price else None
            row["min_x_after"] = tr.min_p / tr.ref_price if tr.ref_price else None
            self.journal.near_miss(row)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def sync_runners(self, specs: list[tuple[StrategySpec, str]]) -> list[str]:
        """Apply champion/challenger changes mid-run: add new versions, relabel promoted ones,
        retire removed ones (no new entries; open positions still exit normally)."""
        changes = []
        want = {s.key: (s, lbl) for s, lbl in specs}
        have = {r.key: r for r in self.runners}
        for key, (spec, lbl) in want.items():
            if key not in have:
                self.runners.append(Runner(spec, self.settings.portfolio.starting_sol, lbl))
                changes.append(f"+{key} ({lbl})")
            elif have[key].label != lbl:
                changes.append(f"{key}: {have[key].label} → {lbl}")
                have[key].label = lbl
        for key, r in have.items():
            if key not in want and r.label != "retired":
                r.label = "retired"
                changes.append(f"-{key} (retired)")
        self.runners = [r for r in self.runners
                        if r.label != "retired" or r.portfolio.positions or r.pending_buys]
        return changes

    def wanted_mints(self) -> set[str]:
        out: set[str] = set(self.trackers)
        for r in self.runners:
            out |= set(r.portfolio.positions) | r.pending_buys
        for hook in self.hooks:
            wm = getattr(hook, "wanted_mints", None)
            if not callable(wm):
                owner = getattr(hook, "__self__", None)
                if owner is not None and owner is not self:
                    wm = getattr(owner, "wanted_mints", None)
            if callable(wm):
                out |= set(wm())
        return out

    def finish(self, now: float | None = None, close_open: bool = True) -> None:
        """End of data: force-close open positions at the current market and flush trackers."""
        now = self.now if now is None else now
        self.advance(now)
        if close_open:
            for r in self.runners:
                for mint, pos in list(r.portfolio.positions.items()):
                    st = self.market.get(mint)
                    if st is None or st.price_sol is None:
                        f = self.fills.stale_sell(st, pos.tokens) if st else None
                    else:
                        q = self.fills.quote_sell(st, pos.tokens)
                        f = None
                        if q:
                            c = self.settings.fills
                            f = Fill(True, "sell", "end_of_data", sol=q[0], tokens=pos.tokens,
                                     price=q[0] / pos.tokens if pos.tokens else 0.0,
                                     cost_sol=c.priority_fee_sol + c.jito_tip_sol + q[0] * c.platform_fee_pct / 100,
                                     venue=q[1])
                    if f is None:
                        continue
                    pos.selling = False
                    self._apply_sell(r, pos, st, f, "end_of_data", now)
        for trs in list(self.trackers.values()):
            for tr in list(trs):
                self._finish_tracker(tr)
        self._timers.clear()

"""Paper launch sniper. One decision per mint, then a 2x-before-50% label.

Entries go through the shared engine fill simulator. This module never builds
a transaction.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass

from solmeme.features.registry import FeatureView
from solmeme.sniper.analyze import analyze
from solmeme.sniper.learn import SniperLearner
from solmeme.sniper.model import STRATEGY_ID

WINDOW_S = 20.0


@dataclass
class _Track:
    mint: str
    ts: float
    symbol: str
    action: str
    score: float
    vetoes: list[str]
    note: str
    vector: dict
    p0: float | None
    max_p: float
    min_p: float
    end: float
    hit: bool | None = None
    labeled: bool = False
    outcome: str | None = None
    pnl: float | None = None
    trade_id: str | None = None
    bought: bool = False


class Sniper:
    """Scores a launch once, paper-buys through the engine, and labels the path."""

    def __init__(self, engine, store=None, *, decision_s=12.0, horizon_s=600.0, explore=True,
                 on_decision=None, resume: bool = False):
        self.engine = engine
        self.store = store
        self.decision_s = float(decision_s)
        self.horizon_s = float(horizon_s)
        self.explore = bool(explore)
        self.on_decision = on_decision
        self.learner = SniperLearner()
        if store is not None:
            blob = store.load_sniper()
            if blob:
                self.learner = SniperLearner.from_dict(blob)
        self.pending: dict[str, _Track] = {}
        self.trackers: dict[str, _Track] = {}
        self._decided: set[str] = set()
        self._learned: set[str] = set()
        self._n_decisions = 0
        self._n_buys = 0
        self._n_skips = 0
        if resume and store is not None:
            self._resume()

    def attach(self) -> None:
        if self.on_event not in self.engine.hooks:
            self.engine.hooks.append(self.on_event)

    def on_event(self, ev, st) -> None:
        now = ev.ts
        self._touch(st, now)
        if st.mint in self._decided:
            self._restore_buy(st, now)
            return
        age = now - st.age_ref_ts
        if self.decision_s <= age <= self.decision_s + WINDOW_S:
            self._decide(st, now)

    def flush(self, now: float, force: bool = False) -> None:
        """Label open trackers. A 2x wins; otherwise a passed horizon or force is a loss."""
        for tr in list(self.trackers.values()):
            if tr.labeled:
                continue
            won = tr.hit is True or (
                tr.hit is not False and tr.p0 is not None and tr.p0 > 0 and tr.max_p >= 2 * tr.p0
            )
            if won:
                self._label(tr, True)
            elif tr.hit is False or now > tr.end or force:
                self._label(tr, False)

    def report(self) -> dict:
        return {
            "summary": self.learner.summary(),
            "working": self.learner.working(),
            "failing": self.learner.failing(),
            "decisions": self._n_decisions,
            "open_labels": sum(1 for tr in self.trackers.values() if not tr.labeled),
            "buys": self._n_buys,
            "skips": self._n_skips,
        }

    def wanted_mints(self) -> set[str]:
        """Mints still inside the decision window, plus any unlabeled path."""
        now = self.engine.now
        out = {mint for mint, tr in self.trackers.items() if not tr.labeled}
        lo, hi = self.decision_s, self.decision_s + WINDOW_S
        for mint, st in self.engine.market.tokens.items():
            if mint in self._decided:
                continue
            age = now - st.age_ref_ts
            if lo <= age <= hi:
                out.add(mint)
        return out

    def _decide(self, st, now: float) -> None:
        self._decided.add(st.mint)
        fv = FeatureView(st, now, self.engine.market)
        card = analyze(fv.snapshot(), mint=st.mint, ts=now, symbol=st.symbol or "", price=st.price_sol)
        block = self.engine._passes_prefilter(fv)
        if block and block not in card.vetoes:
            card.vetoes.append(block)
        p = self.learner.score(card.vector)
        if card.vetoes:
            action = "skip"
        elif p >= self.learner.threshold():
            action = "buy"
        elif self.explore and self._explore(st.mint, now, p):
            action = "explore"
        else:
            action = "skip"
        p0 = st.price_sol
        tr = _Track(
            mint=st.mint, ts=now, symbol=st.symbol or "", action=action, score=p,
            vetoes=list(card.vetoes), note=card.note, vector=dict(card.vector),
            p0=p0, max_p=p0 or 0.0, min_p=p0 if p0 is not None else 0.0,
            end=now + self.horizon_s,
        )
        self.trackers[st.mint] = tr
        if action in ("buy", "explore"):
            self._arm_buy(tr, st, now, fv)
            self._n_buys += 1
        else:
            self._n_skips += 1
        self._n_decisions += 1
        self._write_decision(tr, save_learner=False)
        if self.on_decision is not None:
            try:
                self.on_decision(card, action, p)
            except Exception:
                pass

    def _explore(self, mint: str, now: float, p: float) -> bool:
        if p < self.learner.threshold() - 0.12:
            return False
        rng = random.Random(int(hashlib.sha256(f"{mint}:{int(now)}".encode()).hexdigest()[:8], 16))
        return rng.random() < self.learner.explore_rate()

    def _arm_buy(self, tr: _Track, st, now: float, fv) -> None:
        if tr.bought or tr.action not in ("buy", "explore"):
            return
        runner = next((r for r in self.engine.runners if r.spec.id == STRATEGY_ID), None)
        if runner is None or not self.engine._can_enter(runner, st.mint, now):
            return
        self.engine._place_buy(runner, st, now, fv)
        tr.bought = True
        self.pending[st.mint] = tr

    def _restore_buy(self, st, now: float) -> None:
        """Replay a saved buy when warmup rolls the tape back across the decision window."""
        tr = self.trackers.get(st.mint)
        if tr is None or tr.bought or tr.action not in ("buy", "explore"):
            return
        age = now - st.age_ref_ts
        if not (self.decision_s <= age <= self.decision_s + WINDOW_S):
            return
        self._arm_buy(tr, st, now, FeatureView(st, now, self.engine.market))

    def _resume(self) -> None:
        """Remember decisions already learned so a restart does not train on them again."""
        for row in self.store.sniper_decisions(limit=5000):
            mint = row.get("mint")
            if not mint or mint in self._decided:
                continue
            self._decided.add(mint)
            if row.get("outcome"):
                self._learned.add(mint)
            p0 = row.get("p0")
            try:
                p0 = float(p0) if p0 is not None else None
            except (TypeError, ValueError):
                p0 = None
            ts = float(row.get("ts") or 0)
            tr = _Track(
                mint=mint, ts=ts, symbol=row.get("symbol") or "", action=row.get("action") or "skip",
                score=float(row.get("score") or 0), vetoes=_json_list(row.get("vetoes")),
                note=row.get("note") or "", vector=_json_obj(row.get("features")),
                p0=p0, max_p=(p0 or 0) * float(row.get("max_x") or 1),
                min_p=p0 if p0 is not None else 0.0, end=ts + self.horizon_s,
                labeled=bool(row.get("outcome")), outcome=row.get("outcome"),
                pnl=row.get("pnl_sol"),
            )
            self.trackers[mint] = tr
            if tr.action in ("buy", "explore") and not tr.labeled:
                self.pending[mint] = tr

    def _touch(self, st, now: float) -> None:
        tr = self.trackers.get(st.mint)
        if tr is None:
            return
        if tr.labeled:
            self._catch_pnl(tr)
            return
        self._link_trade(tr)
        price = st.price_sol
        if price is not None and now <= tr.end and tr.p0 and tr.p0 > 0:
            tr.max_p = max(tr.max_p, price)
            tr.min_p = min(tr.min_p, price)
            if tr.hit is None:
                if price >= 2 * tr.p0:
                    tr.hit = True
                elif price <= 0.5 * tr.p0:
                    tr.hit = False
            if tr.hit is not None:
                self._label(tr, bool(tr.hit))
                return
        if now > tr.end and not tr.labeled:
            self._label(tr, False)

    def _link_trade(self, tr: _Track) -> None:
        if tr.mint not in self.pending:
            return
        for r in self.engine.runners:
            if r.spec.id != STRATEGY_ID:
                continue
            pos = r.portfolio.positions.get(tr.mint)
            if pos is not None:
                tr.trade_id = pos.trade_id

    def _pnl(self, tr: _Track) -> float | None:
        for r in self.engine.runners:
            if r.spec.id != STRATEGY_ID:
                continue
            if tr.mint in r.pending_buys or tr.mint in r.portfolio.positions:
                pos = r.portfolio.positions.get(tr.mint)
                if pos is not None:
                    tr.trade_id = pos.trade_id
                return None
        for row in reversed(self.engine.journal.trades):
            if row.get("strategy_id") != STRATEGY_ID or row.get("mint") != tr.mint:
                continue
            if tr.trade_id and row.get("trade_id") != tr.trade_id:
                continue
            if row.get("exit_ts") is None:
                continue
            return row.get("pnl_sol")
        return None

    def _label(self, tr: _Track, win: bool) -> None:
        if tr.labeled:
            return
        tr.labeled = True
        tr.outcome = "win" if win else "loss"
        tr.pnl = self._pnl(tr)
        self.pending.pop(tr.mint, None)
        if tr.mint in self._learned:
            self._write_decision(tr, save_learner=False)
            return
        self._learned.add(tr.mint)
        self.learner.observe(tr.vector, win, action=tr.action, pnl=tr.pnl)
        self._write_decision(tr, save_learner=True)

    def _catch_pnl(self, tr: _Track) -> None:
        if tr.pnl is not None or tr.action not in ("buy", "explore"):
            return
        pnl = self._pnl(tr)
        if pnl is None:
            return
        tr.pnl = pnl
        self.learner.note_pnl(tr.action, pnl)
        self._write_decision(tr, save_learner=True)

    def _max_x(self, tr: _Track) -> float | None:
        if tr.p0 is None or tr.p0 <= 0:
            return None
        return tr.max_p / tr.p0

    def _write_decision(self, tr: _Track, *, save_learner: bool) -> None:
        if self.store is None:
            return
        if save_learner:
            self.store.save_sniper(self.learner.to_dict())
        self.store.insert_sniper_decision({
            "mint": tr.mint, "ts": tr.ts, "symbol": tr.symbol, "action": tr.action,
            "score": tr.score, "vetoes": tr.vetoes, "note": tr.note, "features": tr.vector,
            "outcome": tr.outcome, "max_x": self._max_x(tr), "pnl_sol": tr.pnl, "p0": tr.p0,
        })


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return val if isinstance(val, list) else []


def _json_obj(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return val if isinstance(val, dict) else {}


def run_sniper(settings, events, store=None, *, decision_s=12.0, horizon_s=600.0, explore=True):
    """Replay `events` through an engine that only runs the launch sniper spec."""
    from solmeme.sim.engine import Engine, Journal
    from solmeme.strategy.spec import load_dir

    spec = next((s for s in load_dir(settings.strategies_dir) if s.id == STRATEGY_ID), None)
    if spec is None:
        raise FileNotFoundError(f"{STRATEGY_ID} not found in {settings.strategies_dir}")
    run_id = store.start_run("sniper") if store else None
    journal = Journal(store, run_id) if store else Journal()
    engine = Engine([(spec, "champion")], settings, journal=journal)
    sniper = Sniper(engine, store, decision_s=decision_s, horizon_s=horizon_s, explore=explore)
    sniper.attach()
    for ev in events:
        engine.step(ev)
    engine.finish(close_open=True)
    sniper.flush(engine.now, force=True)
    if store and run_id:
        store.end_run(run_id)
    return engine, sniper

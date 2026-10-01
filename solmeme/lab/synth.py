"""Synthetic pump.fun market for demos and tests (no API keys needed).

Token archetypes with a planted, learnable pattern:
  organic — many distinct buyers, rising price, often graduates; KOLs sometimes buy early
  rug     — bundled snipers + dev dump after 1-3 min
  wash    — a few wallets trading back and forth (high trade count, low unique buyers)
  dud     — a handful of buys, then silence
  fader   — looks organic, but an early whale (big single buy) dumps after 1-4 min → bleed
            (the planted, non-obvious mistake: catchable via max_buy_* / top1_pct)
Events are consistent with real curve math, so fills/features behave realistically.
"""

from __future__ import annotations

import heapq
import random
import string

from solmeme.core.events import (
    CurveTrade, Enrichment, ExternalSignal, Migration, PoolTrade, SolPrice, TokenLaunched, WalletTrade,
)
from solmeme.sim import curve
from solmeme.sim.amm import Pool

SOL_USD = 150.0
GRAD_V_SOL = 115.0
KOLS = [f"KOL{i:02d}" + "x" * 38 for i in range(8)]


def _addr(rng: random.Random, prefix: str = "") -> str:
    return prefix + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(44 - len(prefix)))


class _Tok:
    def __init__(self, rng: random.Random, t0: float, archetype: str):
        self.rng = rng
        self.t0 = t0
        self.arch = archetype
        self.mint = _addr(rng)
        self.creator = _addr(rng)
        self.sym = "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        self.v_sol, self.v_tok = curve.INITIAL_V_SOL, curve.INITIAL_V_TOKENS
        self.bal: dict[str, float] = {}
        self.events: list = []
        self.migrated = False
        self.pool: Pool | None = None
        self.n = 0
        self.tape: list = []   # (ts, is_buy, sol) for DexScreener-style 5-min stats

    def emit(self, ev) -> None:
        self.events.append(ev)
        if ev.kind in ("curve_trade", "pool_trade"):
            self.tape.append((ev.ts, ev.side == "buy", ev.sol))

    def m5(self, now: float) -> tuple[int, int, float]:
        win = [x for x in self.tape if now - 300 <= x[0] <= now]
        return sum(1 for x in win if x[1]), sum(1 for x in win if not x[1]), sum(x[2] for x in win) * SOL_USD

    def chg_m5(self, now: float) -> float | None:
        past = [x for x in self.events if x.kind in ("curve_trade", "pool_trade") and x.ts <= now - 300]
        if not past:
            return None
        e = past[-1]
        p0 = curve.price(e.v_sol, e.v_tokens) if e.kind == "curve_trade" else (e.sol / e.tokens if e.tokens else None)
        return (self.price() / p0 - 1) * 100 if p0 else None

    def buy(self, ts: float, who: str, sol: float) -> None:
        if self.migrated:
            assert self.pool is not None
            tok = self.pool.buy_out(sol, 0.3)
            self.pool.apply_trade("buy", sol, tok)
            self.bal[who] = self.bal.get(who, 0) + tok
            self.emit(PoolTrade(ts=ts, source="synth", mint=self.mint, trader=who, side="buy", sol=sol, tokens=tok))
            return
        tok, self.v_sol, self.v_tok = curve.buy_out(self.v_sol, self.v_tok, sol, 1.25)
        self.bal[who] = self.bal.get(who, 0) + tok
        self.n += 1
        self.emit(CurveTrade(ts=ts, source="synth", mint=self.mint, trader=who, side="buy", sol=sol, tokens=tok,
                             new_balance=self.bal[who], v_sol=self.v_sol, v_tokens=self.v_tok,
                             mcap_sol=curve.price(self.v_sol, self.v_tok) * 1e9, sig=f"s{self.mint[:6]}{self.n}"))
        if self.v_sol >= GRAD_V_SOL:
            self.migrated = True
            self.pool = Pool.seed_from_price(curve.price(self.v_sol, self.v_tok))
            self.emit(Migration(ts=ts + 2, source="synth", mint=self.mint))

    def sell(self, ts: float, who: str, frac: float) -> None:
        tok = self.bal.get(who, 0) * frac
        if tok <= 0:
            return
        self.bal[who] -= tok
        if self.migrated:
            assert self.pool is not None
            sol = self.pool.sell_out(tok, 0.3)
            self.pool.apply_trade("sell", sol, tok)
            self.emit(PoolTrade(ts=ts, source="synth", mint=self.mint, trader=who, side="sell", sol=sol, tokens=tok))
            return
        sol, self.v_sol, self.v_tok = curve.sell_out(self.v_sol, self.v_tok, tok, 1.25)
        self.n += 1
        self.emit(CurveTrade(ts=ts, source="synth", mint=self.mint, trader=who, side="sell", sol=sol, tokens=tok,
                             new_balance=self.bal[who], v_sol=self.v_sol, v_tokens=self.v_tok,
                             mcap_sol=curve.price(self.v_sol, self.v_tok) * 1e9, sig=f"s{self.mint[:6]}{self.n}"))

    def price(self) -> float:
        return self.pool.price if self.migrated and self.pool else curve.price(self.v_sol, self.v_tok)

    def holders(self) -> tuple[int, float]:
        vals = sorted((b for b in self.bal.values() if b > 0), reverse=True)
        return len(vals), sum(vals[:10]) / 1e9 * 100


def _simulate(tok: _Tok, horizon_s: float) -> None:
    rng, t = tok.rng, tok.t0
    dev_buy = rng.uniform(0.3, 2.0)
    tokens0, v_sol, v_tok = curve.buy_out(tok.v_sol, tok.v_tok, dev_buy, 1.25)
    tok.v_sol, tok.v_tok = v_sol, v_tok
    tok.bal[tok.creator] = tokens0
    tok.emit(TokenLaunched(ts=t, source="synth", mint=tok.mint, name=tok.sym.title(), symbol=tok.sym,
                           creator=tok.creator, v_sol=v_sol, v_tokens=v_tok, mcap_sol=curve.price(v_sol, v_tok) * 1e9,
                           initial_buy_sol=dev_buy, initial_buy_tokens=tokens0))
    wallets: list[str] = []
    end = t + horizon_s
    arch = tok.arch
    if arch == "rug":
        for _ in range(rng.randint(3, 6)):  # bundled snipers
            w = _addr(rng, "B")
            wallets.append(w)
            tok.buy(t + rng.uniform(0.2, 1.5), w, rng.uniform(0.5, 2.5))
        dump_at = t + rng.uniform(60, 180)
    if arch == "fader":
        whale = _addr(rng, "W")
        whale_at = t + rng.uniform(8, 30)
        fade_at = t + rng.uniform(60, 240)
        whale_done = False
    kol_at = None
    if (arch == "organic" and rng.random() < 0.5) or (arch == "rug" and rng.random() < 0.15):
        kol_at = t + rng.uniform(20, 120)
    kol_buyers: list[str] = []
    next_snap = t + 20
    peak_px = 0.0
    while t < end:
        age = t - tok.t0
        if arch == "fader":
            t += rng.expovariate(1 / 1.0)
            if not whale_done and t >= whale_at:
                tok.buy(t, whale, rng.uniform(4, 8))
                whale_done = True
            if t >= fade_at:
                tok.sell(t, whale, 1.0)
                arch = "bleed"
                continue
            if rng.random() < 0.72:
                w = _addr(rng)
                wallets.append(w)
                tok.buy(t, w, rng.lognormvariate(-0.6, 0.8))
            elif wallets:
                tok.sell(t, rng.choice(wallets), rng.choice([0.5, 1.0]))
        elif arch == "bleed":
            t += rng.expovariate(1 / 2.0)
            if rng.random() < 0.33:
                w = _addr(rng)
                wallets.append(w)
                tok.buy(t, w, rng.lognormvariate(-1.2, 0.7))
            elif wallets:
                tok.sell(t, rng.choice(wallets), 1.0)
            if age > 1200:
                break
        elif arch == "organic":
            t += rng.expovariate(1 / max(0.5, 0.9 + age / 400))
            p_buy = 0.74 if age < 500 else (0.5 if age < 1200 else 0.42)
            if rng.random() < p_buy:
                w = _addr(rng) if (rng.random() < 0.85 or not wallets) else rng.choice(wallets)
                wallets.append(w)
                tok.buy(t, w, rng.lognormvariate(-0.5, 0.8))
            elif wallets:
                tok.sell(t, rng.choice(wallets), rng.choice([0.5, 1.0]))
        elif arch == "rug":
            t += rng.expovariate(1 / 2.5)
            if t >= dump_at:
                tok.sell(t, tok.creator, 1.0)
                for w in wallets[:6]:
                    tok.sell(t + rng.uniform(0.1, 3), w, 1.0)
                arch = "dead"
                continue
            if rng.random() < 0.65:
                w = _addr(rng)
                wallets.append(w)
                tok.buy(t, w, rng.lognormvariate(-1.2, 0.7))
            elif wallets:
                tok.sell(t, rng.choice(wallets), 1.0)
        elif arch == "wash":
            t += rng.expovariate(1 / 3.0)
            if len(wallets) < 4:
                w = _addr(rng)
                wallets.append(w)
                tok.buy(t, w, rng.uniform(0.3, 1.0))
            else:
                w = rng.choice(wallets)
                if rng.random() < 0.52:
                    tok.buy(t, w, rng.uniform(0.2, 0.8))
                else:
                    tok.sell(t, w, 0.6)
            if age > 900:
                break
        elif arch == "dud":
            t += rng.expovariate(1 / 8.0)
            if age < 60 and rng.random() < 0.7:
                w = _addr(rng)
                wallets.append(w)
                tok.buy(t, w, rng.uniform(0.05, 0.4))
            elif wallets and rng.random() < 0.5:
                tok.sell(t, rng.choice(wallets), 1.0)
            if age > 300:
                break
        else:  # dead after rug
            t += rng.expovariate(1 / 30.0)
            if wallets and rng.random() < 0.3:
                tok.sell(t, rng.choice(wallets), 1.0)
            if age > 900:
                break

        if kol_at is not None and t >= kol_at and len(kol_buyers) < 3:
            k = rng.choice(KOLS)
            if k not in kol_buyers:
                kol_buyers.append(k)
                tok.buy(t, k, rng.uniform(1, 3))
                tok.emit(WalletTrade(ts=t + rng.uniform(1, 4), source="synth", wallet=k, label="kol", mint=tok.mint,
                                     side="buy", sol=2.0, symbol=tok.sym))
            kol_at = t + rng.uniform(10, 60)
        px = tok.price()
        peak_px = max(peak_px, px)
        if kol_buyers and px < peak_px * 0.6:
            for k in kol_buyers:
                tok.sell(t, k, 1.0)
                tok.emit(WalletTrade(ts=t + 1, source="synth", wallet=k, label="kol", mint=tok.mint, side="sell",
                                     symbol=tok.sym))
            kol_buyers = []
            kol_at = None
        if t >= next_snap and age < 1800:
            n_h, top10 = tok.holders()
            data = {
                "symbol": tok.sym, "holders": n_h, "top10_pct": top10,
                "price_usd": px * SOL_USD, "mcap_usd": px * 1e9 * SOL_USD,
                "smart_degen_count": (rng.randint(1, 4) if tok.arch in ("organic", "fader") else rng.randint(0, 1)),
                "rug_ratio": round(rng.uniform(0.35, 0.7) if tok.arch == "rug" else rng.uniform(0.0, 0.25), 3),
                "bundler_rate": round(rng.uniform(0.3, 0.6) if tok.arch == "rug" else rng.uniform(0, 0.15), 3),
                "migrated": tok.migrated,
            }
            if age < 300:
                tok.emit(ExternalSignal(ts=t + 0.5, source="gmgn_trenches", mint=tok.mint, signal="gmgn_new", data={}))
            tok.emit(Enrichment(ts=t + 0.5, source="gmgn_trenches", mint=tok.mint, data=data))
            b5, s5, v5 = tok.m5(t)
            dex = {"dex_id": "pumpswap" if tok.migrated else "pumpfun", "price_sol": px, "price_usd": px * SOL_USD,
                   "buys_m5": b5, "sells_m5": s5, "volume_m5_usd": v5, "price_chg_m5": tok.chg_m5(t)}
            if tok.migrated and tok.pool:
                dex.update(liquidity_usd=tok.pool.sol * 2 * SOL_USD, liquidity_sol=tok.pool.sol * 2)
            if age >= 45:   # DexScreener indexes new pairs with a delay
                tok.emit(Enrichment(ts=t + 0.7 + rng.uniform(0, 4), source="dexscreener", mint=tok.mint,
                                    data={k: v for k, v in dex.items() if v is not None}))
            next_snap = t + 20


def generate(n_tokens: int = 300, duration_s: float = 7200, start_ts: float = 1_780_000_000.0,
             seed: int = 1, mix: dict[str, float] | None = None) -> list:
    rng = random.Random(seed)
    mix = mix or {"organic": 0.14, "fader": 0.12, "rug": 0.22, "wash": 0.17, "dud": 0.35}
    arches, weights = zip(*mix.items())
    streams = []
    for i in range(n_tokens):
        t0 = start_ts + rng.uniform(0, duration_s * 0.8)
        tok = _Tok(random.Random(rng.random()), t0, rng.choices(arches, weights)[0])
        _simulate(tok, horizon_s=2400)
        tok.events.sort(key=lambda e: e.ts)
        streams.append(tok.events)
    sol = [SolPrice(ts=start_ts + k * 60 - 1, source="synth", usd=SOL_USD) for k in range(int(duration_s / 60) + 45)]
    merged = list(heapq.merge(sol, *streams, key=lambda e: e.ts))
    return merged

"""The paper wallet: a dollar starting balance and goal, tracked per strategy.

Every strategy trades its own copy of the same wallet, so each one answers
"what would my $X have become if I had run this strategy?".
"""

from __future__ import annotations

import logging
import time

import httpx

from solmeme.config import Settings, secret

log = logging.getLogger(__name__)

WSOL = "So11111111111111111111111111111111111111112"


def fetch_sol_usd(timeout: float = 5.0) -> float | None:
    """Current SOL price, or None if no price source answers."""
    try:
        key = secret("JUPITER_API_KEY")
        if key:
            r = httpx.get("https://api.jup.ag/price/v3", params={"ids": WSOL},
                          headers={"x-api-key": key}, timeout=timeout)
            r.raise_for_status()
            return float(r.json()[WSOL]["usdPrice"])
        r = httpx.get("https://api.coingecko.com/api/v3/simple/price",
                      params={"ids": "solana", "vs_currencies": "usd"}, timeout=timeout)
        r.raise_for_status()
        return float(r.json()["solana"]["usd"])
    except Exception as exc:
        log.warning("SOL price lookup failed: %s", exc)
        return None


def logged_sol_usd(settings: Settings, minutes: float = 360) -> float | None:
    """Most recent SOL price in the event log, if one was recorded lately."""
    from solmeme.core.eventlog import read_events

    price = None
    for ev in read_events(settings.events_dir, time.time() - minutes * 60, None):
        if ev.kind == "sol_price" and ev.usd:
            price = ev.usd
    return price


def fund_in_sol(settings: Settings, sol_usd: float | None) -> Settings:
    """Copy of settings whose starting_sol is portfolio.starting_usd at this SOL price."""
    usd = settings.portfolio.starting_usd
    if not usd or not sol_usd:
        return settings
    out = settings.model_copy(deep=True)
    out.portfolio.starting_sol = usd / sol_usd
    return out


def _balances(engine) -> list[tuple[str, float, float]]:
    """(key, starting SOL, current SOL) per wallet: one shared wallet, or one per strategy."""
    from solmeme.sim.live import _held_sol, _paper_equity_sol

    if engine.account is not None:
        # challengers trade their own copy of the wallet; their positions are not in this one
        held = sum(_held_sol(r, engine) for r in engine.runners if r.portfolio.account is engine.account)
        return [(SHARED, engine.account.starting_sol, engine.account.cash + held)]
    return [(r.key, r.portfolio.starting_sol, _paper_equity_sol(r, engine)) for r in engine.runners]


SHARED = "wallet"


class WalletHistory:
    """Highest and lowest value each wallet has reached this session."""

    def __init__(self):
        self.marks: dict[str, dict[str, tuple[float, float]]] = {}

    def observe(self, engine, px: float | None, now: float) -> None:
        if engine is None:
            return
        for key, _, balance in _balances(engine):
            marks = self.marks.setdefault(key, {})
            self._mark(marks, "sol", balance, now)
            if px:
                self._mark(marks, "usd", balance * px, now)
    @staticmethod
    def _mark(marks: dict, unit: str, value: float, now: float) -> None:
        high, low = marks.get("high_" + unit), marks.get("low_" + unit)
        if high is None or value > high[0]:
            marks["high_" + unit] = (value, now)
        if low is None or value < low[0]:
            marks["low_" + unit] = (value, now)

    def range_of(self, key: str, balance_sol: float, balance_usd: float | None) -> dict:
        """High/low for one strategy in USD when known, else SOL, plus how far below the high it is now."""
        marks = self.marks.get(key, {})
        unit = "usd" if balance_usd is not None and "high_usd" in marks else "sol"
        now_value = balance_usd if unit == "usd" else balance_sol
        high = marks.get("high_" + unit, (now_value, None))
        low = marks.get("low_" + unit, (now_value, None))
        high = high if high[0] >= now_value else (now_value, None)
        low = low if low[0] <= now_value else (now_value, None)
        return {
            "unit": unit,
            "high": high[0],
            "high_ts": high[1],
            "low": low[0],
            "low_ts": low[1],
            "from_high_pct": (now_value - high[0]) / high[0] * 100 if high[0] else None,
        }


CHAMPIONS_FILE = "champions_wallet.json"
RESET_FILE = "wallet_reset.json"


TRUST_FILE = "trust_trades_after.json"


def trusted_since(settings: Settings) -> float:
    """Paper trades before this were filled by a broken model and must not judge anyone (0 = trust all).
    Oct 11: the live calibration drew random real-vs-paper gaps onto paper buys, and negative ones made fake
    bargains (16% of Oct 10's curve fills, +21.6% avg vs -11.7%), so the swarm's fitness, kills, promotions and
    winners were partly built on wins that live could never get."""
    import json

    try:
        return float(json.loads((settings.data_dir / "session" / TRUST_FILE).read_text(encoding="utf-8"))["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def wallet_reset_ts(settings: Settings) -> float:
    """When every wallet was last restarted at the starting amount (0 = never). Wallets count only
    trades after it; track records (fitness, kills, winners) keep using everything."""
    import json

    try:
        return float(json.loads((settings.data_dir / "session" / RESET_FILE).read_text(encoding="utf-8"))["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def reset_wallets(settings: Settings, now: float | None = None) -> float:
    """Start every wallet over (champions and swarm agents), as if freshly funded. History stays."""
    import json

    now = now or time.time()
    d = settings.data_dir / "session"
    d.mkdir(parents=True, exist_ok=True)
    (d / RESET_FILE).write_text(json.dumps({"ts": now}), encoding="utf-8")
    (d / CHAMPIONS_FILE).unlink(missing_ok=True)
    return now


def save_champions_wallet(settings: Settings, engine, start_px: float | None, history: "WalletHistory") -> None:
    """data/session/champions_wallet.json: what the shared wallet is worth now (open positions at market)."""
    import json

    b = engine.shared_breakdown() if engine is not None else None
    if b is None:
        return
    path = settings.data_dir / "session" / CHAMPIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    marks = {k: list(v) for k, v in history.marks.get(SHARED, {}).items()}
    from dataclasses import asdict

    positions = {}                      # runner key -> open positions (champions and swarm agents keep theirs)
    for r in engine.runners:
        shared = engine.in_shared_wallet(r)
        if not shared and r.label != "swarm":
            continue
        rows = []
        for p in r.portfolio.positions.values():
            if p.tokens <= 0:
                continue
            d = asdict(p)
            d["mark_value_sol"] = engine.held_sol_of(p)
            rows.append(d)
        if rows:
            positions[r.key] = dict(shared=shared, label=r.label, positions=rows)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "equity_sol": b["cash"] + b["held"], "cash_sol": b["cash"],
                               "start_sol": b["start"], "start_px": start_px, "epoch": engine.wallet_epoch,
                               "marks": marks, "positions": positions}, default=float), encoding="utf-8")
    tmp.replace(path)


def load_champions_wallet(settings: Settings) -> dict | None:
    import json

    try:
        return json.loads((settings.data_dir / "session" / CHAMPIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def wallet_view(engine, start_px: float | None, px: float | None, goal_usd: float | None,
                history: WalletHistory | None = None) -> dict | None:
    """The wallet against where it started and the goal.

    With a shared wallet this is the whole wallet (cash plus every open position) and
    best is None; otherwise it is the best strategy's own copy of the wallet.
    start_usd uses the SOL price when the wallet was funded and balance_usd the
    price now, so a SOL move shows up in the result just as it would in a real wallet.
    """
    if engine is None or not engine.runners:
        return None
    start_px = start_px or px
    wallets = _balances(engine)
    key, start, balance = max(wallets, key=lambda w: w[2] / w[1] if w[1] else 0.0)
    start_usd = start * start_px if start_px else None
    balance_usd = balance * px if px else None
    if start_usd and balance_usd is not None:
        pnl_pct = (balance_usd - start_usd) / start_usd * 100
    else:
        pnl_pct = (balance - start) / start * 100 if start else None
    reached = [k for k, _, b in wallets if goal_usd and px and b * px >= goal_usd]
    # locked = profit already taken by closed trades; open = what open positions add at market right now
    sb = engine.shared_breakdown() if engine.account is not None else None
    locked_sol = (sb["cash"] + sb["open_cost"] - sb["start"]) if sb else None
    open_sol = (sb["held"] - sb["open_cost"]) if sb else None
    return {
        "strategies": len(engine.runners),
        "shared": engine.account is not None,
        "best": None if key == SHARED else key,
        "start_sol": start,
        "start_usd": start_usd,
        "balance_sol": balance,
        "balance_usd": balance_usd,
        "pnl_sol": balance - start,
        "pnl_usd": None if start_usd is None or balance_usd is None else balance_usd - start_usd,
        "pnl_pct": pnl_pct,
        "goal_usd": goal_usd,
        "goal_pct": None if not goal_usd or balance_usd is None else balance_usd / goal_usd * 100,
        "reached": reached,
        "range": None if history is None else history.range_of(key, balance, balance_usd),
        "locked_sol": locked_sol,
        "locked_usd": None if locked_sol is None or not px else locked_sol * px,
        "open_sol": open_sol,
        "open_usd": None if open_sol is None or not px else open_sol * px,
    }

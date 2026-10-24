"""Time series for the website charts: the wallet and every strategy's PnL, sampled in memory.

Kept for the life of the session only (a restart starts a fresh wallet anyway). The swarm page
draws agents from their stored trades instead, so those curves survive restarts.
"""

from __future__ import annotations

from collections import deque

SAMPLE_S = 15.0
KEEP_S = 24 * 3600


def runner_pnl_sol(runner, engine) -> float:
    """Realized PnL plus what open positions would add if sold at the current market."""
    from solmeme.sim.live import _held_sol

    open_cost = sum(p.cost_sol - p.proceeds_sol for p in runner.portfolio.positions.values() if p.tokens > 0)
    return runner.portfolio.realized + _held_sol(runner, engine) - open_cost


class SeriesBook:
    def __init__(self, sample_s: float = SAMPLE_S, keep_s: float = KEEP_S):
        self.sample_s = sample_s
        n = int(keep_s / sample_s) + 1
        self.wallet: deque = deque(maxlen=n)              # (ts, usd, sol)
        self.runners: dict[str, deque] = {}               # key -> (ts, pnl_sol)
        self._n = n
        self._last = 0.0

    def observe(self, session, now: float) -> None:
        if now - self._last < self.sample_s or session.engine is None:
            return
        self._last = now
        view = session.wallet()
        if view:
            self.wallet.append((now, view.get("balance_usd"), view.get("balance_sol")))
        live = set()
        for r in session.engine.runners:
            live.add(r.key)
            self.runners.setdefault(r.key, deque(maxlen=self._n)).append((now, runner_pnl_sol(r, session.engine)))
        for key in [k for k in self.runners if k not in live]:
            del self.runners[key]

    def as_dict(self, max_points: int = 480) -> dict:
        return {
            "sample_s": self.sample_s,
            "wallet": [[round(t, 1), _r(u, 4), _r(s, 6)] for t, u, s in _thin(list(self.wallet), max_points)],
            "runners": {k: [[round(t, 1), _r(p, 6)] for t, p in _thin(list(v), max_points)]
                        for k, v in self.runners.items()},
        }


def _thin(rows: list, max_points: int) -> list:
    if len(rows) <= max_points:
        return rows
    step = len(rows) / max_points
    out = [rows[int(i * step)] for i in range(max_points - 1)]
    out.append(rows[-1])
    return out


def _r(x, nd: int):
    return None if x is None else round(float(x), nd)

"""Paper fills, corrected by what real prices did (the live dry run).

The paper fill model prices against market data that is a few seconds old and never moves for our own
buys. The live dry run quotes the same fills at real executable prices, so the gap between the two is a
measurement of how optimistic paper is. This module turns those measurements into a correction the
paper fill model applies, so everything that learns from paper (swarm, lessons, council, promotions)
learns under realistic fills:

  buy   with probability w: no route at all (as often as live found none), else the price is moved by a
        gap drawn from the observed real-vs-paper entry gaps; the usual slippage limit then fails buys
        whose corrected price ran too far, so paper misses entries the way live does
  sell  with probability w: proceeds moved by a gap drawn from the observed exit gaps

w = n / (n + prior_n) shrinks the correction toward "no change" while the sample is small, so a single
bad hour does not rewrite paper. Draws use the order's own RNG, so a replay stays deterministic.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

FILE = "fill_calibration.json"


@dataclass
class Calibration:
    buy_gaps: list[float] = field(default_factory=list)    # real / paper - 1 at entry (positive = live paid more)
    sell_gaps: list[float] = field(default_factory=list)   # real / paper - 1 at exit (negative = live got less)
    no_route: float = 0.0                                   # share of buy attempts with no route at all
    n: int = 0                                              # buy attempts measured
    weight: float = 0.0                                     # n / (n + prior_n)
    updated: float = 0.0

    def buy_gap(self, rng: random.Random) -> float | None:
        """The correction for one paper buy: a price gap (0 = none), or None for "no route"."""
        if self.weight <= 0 or rng.random() >= self.weight:
            return 0.0
        if self.no_route > 0 and rng.random() < self.no_route:
            return None
        # never better than the model's own quote: a negative gap is one trade where paper was wrong the other way;
        # drawn onto a random buy it made a fake bargain (Oct 10: 30 of 115 gaps negative, down to -50%; the positive
        # draws failed on slippage, so paper kept only the lucky ones and 16% of its fills landed 10%+ under the
        # decided price, averaging +21.6% vs -11.7% for the rest)
        return max(0.0, rng.choice(self.buy_gaps)) if self.buy_gaps else 0.0

    def sell_gap(self, rng: random.Random) -> float:
        if self.weight <= 0 or not self.sell_gaps or rng.random() >= self.weight:
            return 0.0
        return min(0.0, rng.choice(self.sell_gaps))      # same for sells: only ever worse

    def summary(self) -> dict:
        def med(xs):
            return sorted(xs)[len(xs) // 2] if xs else None
        return dict(n=self.n, weight=round(self.weight, 3), no_route=round(self.no_route, 3),
                    buy_gap_median=med(self.buy_gaps), sell_gap_median=med(self.sell_gaps), updated=self.updated)


def compute(tape: list[dict], prior_n: int = 30, max_gap: float = 2.0) -> Calibration:
    """From the dry run's fills: every buy attempt that got a real price (bought or missed on slippage),
    attempts with no route, and every sell, each against the paper price of the same fill."""
    buys, sells, attempts, no_route = [], [], 0, 0
    for t in tape:
        side, real, paper = t.get("side"), t.get("price"), t.get("paper_price")
        if side in ("buy", "missed"):
            attempts += 1
            if real and paper:
                buys.append(max(-0.5, min(max_gap, real / paper - 1)))
            elif side == "missed" and "no route" in str(t.get("why") or "").lower():
                no_route += 1
        elif side == "sell" and real and paper:
            sells.append(max(-0.9, min(0.5, real / paper - 1)))
    measured = len(buys) + no_route
    return Calibration(buy_gaps=[round(g, 4) for g in buys[-500:]], sell_gaps=[round(g, 4) for g in sells[-500:]],
                       no_route=no_route / measured if measured else 0.0, n=measured,
                       weight=measured / (measured + prior_n) if measured else 0.0, updated=time.time())


def save(data_dir: Path, cal: Calibration) -> None:
    path = Path(data_dir) / "session" / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(cal)), encoding="utf-8")
    tmp.replace(path)


def load(data_dir: Path) -> Calibration | None:
    try:
        d = json.loads((Path(data_dir) / "session" / FILE).read_text(encoding="utf-8"))
        return Calibration(**{k: d[k] for k in Calibration.__dataclass_fields__ if k in d})
    except (OSError, ValueError, TypeError):
        return None

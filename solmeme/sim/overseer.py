"""The Overseer: the last word before the live wallet buys anything.

The other roles do the work: ResearchBot (the sources, whale tape, callouts, early lane) finds coins, TraderBot (the
champions and the global filter) trades inside your rules, SniperBot (real-price fills, the slippage guard) checks each
entry, and the council decides. The Overseer watches all of it and can stop new live buys when something is wrong:

  data      the live trade feed or the price data went quiet (buying blind is how the fake-price losses happened)
  day_loss  the live wallet lost 20%+ in the last 24 h (separate from the attempt's 50% kill switch)
  exposure  more than 70% of the wallet is already in open positions
  council   the council's own picks lost N times in a row on live: council_pick buys pause

Paper is never stopped (it keeps learning). Every decision is logged, so the dashboard and Hermes can show it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

FILE = "overseer.json"


@dataclass
class Overseer:
    data_dir: Path
    cfg: object
    health_fn: object = None                 # () -> dict(source -> seconds since its last event)
    log: list = field(default_factory=list)
    paused: dict = field(default_factory=dict)   # guard -> reason, while it blocks

    def _note(self, guard: str, reason: str, item: dict | None = None) -> None:
        self.log.append(dict(ts=time.time(), guard=guard, reason=reason[:200],
                             mint=(item or {}).get("mint"), symbol=(item or {}).get("symbol"), key=(item or {}).get("key")))
        self.log = self.log[-200:]
        self.save()

    def check(self, item: dict, equity: float, open_cost: float, day_start_equity: float | None,
              closed: list[dict]) -> str | None:
        """None when the live buy may go ahead, else why not."""
        c = self.cfg
        if self.health_fn is not None:
            ages = self.health_fn() or {}
            stale = [f"{k} quiet {int(v)} s" for k, v in ages.items() if v is not None and v > c.max_data_age_s]
            if stale:
                return self._block("data", "data feed down: " + ", ".join(stale), item)
        if day_start_equity and equity < day_start_equity * (1 - c.day_loss_pct / 100):
            return self._block("day_loss", f"live wallet down {100 * (1 - equity / day_start_equity):.0f}% in 24 h "
                                           f"(stop at {c.day_loss_pct:g}%)", item)
        if equity > 0 and open_cost / equity > c.max_exposure_pct / 100:
            return self._block("exposure", f"{100 * open_cost / equity:.0f}% of the wallet already in positions "
                                           f"(max {c.max_exposure_pct:g}%)", item)
        if str(item.get("key", "")).startswith("council_pick@"):
            mine = [t for t in closed if str(t.get("key", "")).startswith("council_pick@")][-c.council_loss_streak:]
            if len(mine) >= c.council_loss_streak and all((t.get("pnl_sol") or 0) <= 0 for t in mine):
                return self._block("council", f"the council's last {c.council_loss_streak} live picks all lost: "
                                              "council picks paused", item)
        for g in list(self.paused):
            self.paused.pop(g)                   # nothing blocks now: earlier pauses are over
        return None

    def _block(self, guard: str, reason: str, item: dict) -> str:
        if self.paused.get(guard) != reason:
            self.paused[guard] = reason
        self._note(guard, reason, item)
        return reason

    def status(self) -> dict:
        return dict(paused=dict(self.paused), log=list(reversed(self.log[-40:])),
                    guards=dict(max_data_age_s=self.cfg.max_data_age_s, day_loss_pct=self.cfg.day_loss_pct,
                                max_exposure_pct=self.cfg.max_exposure_pct, council_loss_streak=self.cfg.council_loss_streak))

    def save(self) -> None:
        p = Path(self.data_dir) / "session" / FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(paused=self.paused, log=self.log[-200:])), encoding="utf-8")
        tmp.replace(p)

    def load(self) -> None:
        try:
            d = json.loads((Path(self.data_dir) / "session" / FILE).read_text(encoding="utf-8"))
            self.paused, self.log = d.get("paused", {}), d.get("log", [])
        except (OSError, ValueError):
            pass

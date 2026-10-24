"""How the council's coin picks turned out, and what a buy would actually be, for the judges' prompt.

Oct 10: the judges said avoid or wait to ~180 picks in a row (0 buys) without knowing that a quarter of
them hit 2x within 30 minutes, what the trade would cost at our size, or how the bot would exit. Now:

  Follower       every pick (asked, or skipped by the global filter) is followed for FOLLOW_S after the
                 decision: peak, low and final price vs the price at the decision. Finished rows go to
                 <data>/pick_outcomes.jsonl; open ones survive restarts in session/pick_follow.json.
  base_rates     the share that peaked at 1.5x / 2x, ended above the decision price, and the median end,
                 overall, by the council's verdict and by lane (early runner vs whale cluster).
  trade_plan     the bet size the live wallet would use, its round-trip cost at today's fee settings and
                 the council_pick exit plan, so a buy is judged as a trade, not just as a coin.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

FILE = "pick_outcomes.jsonl"
OPEN_FILE = "pick_follow.json"
FOLLOW_S = 1800
KEEP_ROWS = 600
MIN_GROUP = 5


def lane_of(why: str) -> str:
    return "early" if str(why).startswith("early runner") else "whales" if "whale" in str(why) else "other"


def _rows_path(data_dir) -> Path:
    return Path(data_dir) / FILE


def record(data_dir, row: dict) -> None:
    path = _rows_path(data_dir)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = (lines + [json.dumps(row)])[-KEEP_ROWS:]
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def load(data_dir, limit: int = KEEP_ROWS) -> list[dict]:
    try:
        out = []
        for line in _rows_path(data_dir).read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
    except OSError:
        return []


class Follower:
    """Follows each pick's price for FOLLOW_S after the decision."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.open: dict[str, dict] = {}
        try:
            self.open = json.loads((self.data_dir / "session" / OPEN_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.open = {}

    def _save(self) -> None:
        p = self.data_dir / "session" / OPEN_FILE
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(self.open), encoding="utf-8")
        except OSError:
            pass

    def start(self, ask: dict, price: float | None, why: str) -> None:
        if not price or ask.get("id") in self.open:
            return
        self.open[ask["id"]] = dict(id=ask["id"], mint=ask["mint"], symbol=ask.get("symbol") or "", ts=time.time(),
                                    verdict=ask.get("verdict") or "", action=ask.get("action") or "",
                                    lane=lane_of(why), px0=float(price), max_x=1.0, min_x=1.0, last_x=1.0)
        self._save()

    def update(self, market, now: float | None = None) -> int:
        """Mark every open pick at its current price; finish the ones past FOLLOW_S. Returns how many finished."""
        now = now or time.time()
        done = []
        for k, o in list(self.open.items()):
            st = market.get(o["mint"]) if market is not None else None
            px = getattr(st, "price_sol", None) if st is not None else None
            if px:
                x = float(px) / o["px0"]
                o.update(max_x=max(o["max_x"], x), min_x=min(o["min_x"], x), last_x=x)
            if now - o["ts"] >= FOLLOW_S:
                done.append(k)
        for k in done:
            o = self.open.pop(k)
            record(self.data_dir, dict(o, end_x=o["last_x"], followed_s=round(now - o["ts"])))
        if done or self.open:
            self._save()
        return len(done)


def _group(rows: list[dict]) -> str:
    n = len(rows)
    pct = lambda c: f"{100 * sum(1 for r in rows if c(r)) / n:.0f}%"           # noqa: E731
    med = statistics.median(r["end_x"] for r in rows)
    return (f"n={n}: peaked 1.5x+ {pct(lambda r: r['max_x'] >= 1.5)}, 2x+ {pct(lambda r: r['max_x'] >= 2)}; "
            f"dropped 30%+ below it at some point (a 30% stop fires, unless a take-profit came first) "
            f"{pct(lambda r: r['min_x'] <= 0.7)}; above the decision price at 30 min "
            f"{pct(lambda r: r['end_x'] > 1)}; median at 30 min {100 * (med - 1):+.0f}%")


def base_rates(data_dir, lane: str | None = None) -> str:
    rows = [r for r in load(data_dir) if r.get("end_x") and r.get("max_x")]
    if len(rows) < MIN_GROUP:
        return ""
    since = time.strftime("%b %d %H:%M", time.localtime(min(r["ts"] for r in rows)))
    out = [f"Track record: how earlier picks moved in the 30 min after the decision ({len(rows)} picks since {since}). "
           f"Peaks are what a take-profit could catch; the 30-min price is what holding gets."]
    out.append(f"  all picks: {_group(rows)}")
    if lane:
        same = [r for r in rows if r.get("lane") == lane]
        if len(same) >= MIN_GROUP:
            out.append(f"  this lane ({lane}): {_group(same)}")
    for v in ("buy", "wait", "avoid", "skipped"):
        g = [r for r in rows if (r.get("verdict") or "") == v]
        if len(g) >= MIN_GROUP:
            label = "skipped by the global filter" if v == "skipped" else f"the council said {v}"
            out.append(f"  {label}: {_group(g)}")
    return "\n".join(out)


def trade_plan(session) -> str:
    """The trade a buy verdict starts: size, cost at today's fee settings, and the council_pick exits."""
    s = session.settings
    eng, d = session.engine, getattr(session, "dryrun", None)
    f = s.fills
    try:
        equity = d.equity() if d is not None else None
    except Exception:  # noqa: BLE001
        equity = None
    pct = s.portfolio.position_pct or 0
    size = min(pct / 100 * equity, s.portfolio.max_position_sol) if equity and pct else 0.1
    px = eng.market.sol_usd if eng is not None and eng.market.sol_usd else None
    fixed = 2 * (0.000005 + f.priority_fee_sol + f.jito_tip_sol)
    cost_pct = 100 * fixed / size + 2 * f.curve_fee_pct if size else None
    exits = ""
    for r in (eng.runners if eng is not None else []):
        if r.spec.id == "council_pick" and r.label == "champion":
            ex, p = r.spec.exits, r.spec.p
            tps = ", ".join(f"sell {float(p(t.sell_pct)):g}% at {float(p(t.at_x)):g}x" for t in ex.take_profit)
            exits = (f"{tps}; stop at -{float(p(ex.stop_loss_pct)):g}%"
                     + (f"; trailing {float(p(ex.trailing_pct)):g}% after {float(p(ex.trailing_activate_x)):g}x"
                        if ex.trailing_pct else "")
                     + (f"; out after {float(p(ex.max_hold_s)) / 60:.0f} min" if ex.max_hold_s else ""))
    usd = f" (${size * px:.2f})" if px else ""
    line = f"If you say buy: the live wallet buys about {size:.3f} SOL{usd}"
    if cost_pct is not None:
        line += (f"; a round trip costs about {cost_pct:.1f}% (fees {fixed:.4f} SOL + pump.fun's {f.curve_fee_pct:g}% "
                 f"each side), so the coin must rise about {cost_pct:.0f}% just to break even")
    tol = getattr(s.dryrun, "slippage_tolerance_pct", None) or f.slippage_tolerance_pct
    if tol:
        line += f"; the buy fails if the price runs {tol:g}%+ before it lands"
    return line + (f". Exit plan: {exits}." if exits else ".")

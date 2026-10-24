"""Whale book: wallets that keep getting into good tokens early and getting out ahead.

Every swarm generation (no LLM):
  1. good tokens: launches in the window whose market cap ran >= GOOD_X within 30 min
  2. every wallet seen trading (GMGN smart-money / KOL feeds, fomo, tracked wallets) is scored:
     win rate and median exit multiple of its round trips, and how often it bought a good token
     early (below half its eventual peak)
  3. wallets that pass become whales, promising ones with less history go on watch, and whales
     whose record slips are dropped; every score is kept as history
  4. up to MAX_TRACES new whales per generation get a light on-chain check: money from a banned
     wallet means it is flagged instead of trusted
The live bot polls the top whales on GMGN and exposes `whale_buys` (distinct whales that bought
a token in the last 10 minutes) as a feature strategies can evolve rules on.

We only see wallets the feeds report, not every buyer of a token.
"""

from __future__ import annotations

import bisect
import json
import statistics
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable

FILE = "whales.json"
GOOD_X = 3.0
MIN_ROUND_TRIPS = 4
WHALE_WIN, WHALE_EXIT = 0.6, 1.2
TRACK_TOP = 3        # polled through GMGN (other venues too); pump.fun trades of every whale come live from its feed
MAX_TRACES = 3
MAX_REVIEW = 6                # whales the council judges per generation
REVIEW_EVERY_S = 86400
HISTORY = 48


CREATED: dict[str, int] = {}     # wallet -> coins it launched in the last scan window (dev-wallet forensics)


def scan(events: Iterable) -> tuple[list[tuple], dict, dict]:
    """Wallet trades (ts, wallet, mint, side, label), first-seen time per mint, market cap paths.
    Also counts each creator's launches into CREATED: a "whale" that launched dozens of coins is a dev wallet
    that wins on its own launches (Oct 10: the council caught one with 223 created tokens)."""
    trades, first, mcap = [], {}, defaultdict(list)
    created: dict[str, int] = defaultdict(int)
    for ev in events:
        k = ev.kind
        if k == "wallet_trade":
            trades.append((ev.ts, ev.wallet, ev.mint, ev.side, ev.label))
        elif k == "launch":
            first.setdefault(ev.mint, ev.ts)
            if getattr(ev, "creator", ""):
                created[ev.creator] += 1
        elif k == "enrichment":
            m = ev.data.get("mcap_usd")
            if m:
                mcap[ev.mint].append((ev.ts, float(m)))
                first.setdefault(ev.mint, ev.ts)
    for v in mcap.values():
        v.sort()
    CREATED.clear()
    CREATED.update(created)
    return trades, first, mcap


def _at(path: list, t: float) -> float | None:
    i = bisect.bisect_right(path, (t, float("inf"))) - 1
    return path[i][1] if i >= 0 else None


def good_tokens(first: dict, mcap: dict, x: float = GOOD_X, within_s: float = 1800) -> dict[str, float]:
    """mint -> peak market cap, for tokens that ran x-fold from their first seen market cap."""
    out = {}
    for mint, t0 in first.items():
        path = [p for p in mcap.get(mint, []) if t0 <= p[0] <= t0 + within_s]
        if len(path) >= 3 and max(m for _, m in path) >= x * path[0][1]:
            out[mint] = max(m for _, m in path)
    return out


def score_wallets(trades: list[tuple], mcap: dict, good: dict[str, float]) -> dict[str, dict]:
    per: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    labels: dict[str, str] = {}
    for ts, w, m, side, label in sorted(trades):
        per[w][m].append((ts, side))
        labels.setdefault(w, label)
    out = {}
    for w, toks in per.items():
        exits, early, recent = [], 0, []
        for m, rows in toks.items():
            buy = next((t for t, s in rows if s == "buy"), None)
            if buy is None:
                continue
            path = mcap.get(m) or []
            m0 = _at(path, buy)
            sell = next((t for t, s in rows if s == "sell" and t > buy), None)
            if m0 and sell:
                m1 = _at(path, sell)
                if m1:
                    exits.append(m1 / m0)
            if m in good and m0 and m0 <= 0.5 * good[m]:
                early += 1
                recent.append(m)
        win = sum(e > 1.0 for e in exits) / len(exits) if exits else None
        med = statistics.median(exits) if exits else None
        # few round trips are mostly luck: shrink toward 0 below 8
        quality = ((win or 0) * min(med or 0, 3.0) + 0.1 * min(early, 3)) * (min(len(exits), 8) / 8) ** 0.5
        out[w] = dict(label=labels.get(w, ""), tokens=len(toks), round_trips=len(exits), win=win, median_exit=med,
                      early_good=early, good_tokens=recent[-5:], score=round(quality, 3))
    return out


def classify(st: dict) -> str:
    good_exits = st["round_trips"] >= MIN_ROUND_TRIPS and (st["win"] or 0) >= WHALE_WIN and (st["median_exit"] or 0) >= WHALE_EXIT
    if good_exits and st["early_good"] >= 1:
        return "whale"
    if st["early_good"] >= 3 and st["round_trips"] >= 2 and (st["win"] or 0) >= 0.5:
        return "whale"
    if good_exits or (st["early_good"] >= 2 and (st["win"] is None or st["win"] >= 0.5)):
        return "watch"
    return ""


class WhaleBook:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / FILE
        self.wallets: dict[str, dict] = {}
        try:
            self.wallets = json.loads(self.path.read_text(encoding="utf-8")).get("wallets", {})
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"wallets": self.wallets}, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def whales(self) -> list[str]:
        """Whales whose buys count (whale_buys): not flagged by the chain check, not distrusted by the council."""
        return [w for w, e in self.wallets.items()
                if e["status"] == "whale" and not e.get("flag") and e.get("council") != "distrust"]

    def top(self, n: int = TRACK_TOP) -> list[str]:
        """The whales polled live: ones the council trusts first, then by score."""
        return sorted(self.whales(), key=lambda w: (self.wallets[w].get("council") != "trust", -self.wallets[w]["score"]))[:n]

    def to_review(self, now: float, n: int = MAX_REVIEW) -> list[str]:
        """Whales for the council: never reviewed, reviewed over a day ago, or whose score moved a lot since."""
        def due(e: dict) -> bool:
            if not e.get("council_ts"):
                return True
            then = e.get("council_at_score") or 0.0
            return now - e["council_ts"] > REVIEW_EVERY_S or abs(e["score"] - then) > 0.3 * max(then, 0.1)
        ws = [w for w in self.whales() if due(self.wallets[w])]
        return sorted(ws, key=lambda w: (bool(self.wallets[w].get("council_ts")), -self.wallets[w]["score"]))[:n]


def update(settings, events_fn, window: tuple[float, float], log_fn=lambda _: None, now: float | None = None,
           trace=None) -> list[dict]:
    """Re-score every wallet, move them between whale / watch / dropped, trace new whales. Returns changes."""
    from solmeme.lab.banlist import BanList

    now = now or time.time()
    banned = BanList(settings.data_dir).addresses
    book = WhaleBook(settings.data_dir)
    trades, first, mcap = scan(events_fn(*window))
    good = good_tokens(first, mcap)
    scores = score_wallets(trades, mcap, good)
    from solmeme.lab.banlist import find_clusters
    movers = {w for c in find_clusters([(t, w, m) for t, w, m, s, _ in trades if s == "buy"], min_shared=3) for w in c["wallets"]}
    changes, new_whales = [], []
    for w, st in scores.items():
        status = "" if w in banned or w in movers else classify(st)   # lockstep wallets are one operator, not whales
        old = book.wallets.get(w)
        if old is None and not status:
            continue
        entry = old or dict(first_seen=now, status="", history=[])
        prev = entry["status"]
        if prev in ("whale", "watch") and not status:
            status = "dropped"
        elif prev == "dropped" and not status:
            status = "dropped"
        entry.update(st, status=status, last_checked=now, created=CREATED.get(w, 0))
        entry["history"] = (entry.get("history") or [])[-(HISTORY - 1):] + [[round(now), st["score"]]]
        book.wallets[w] = entry
        if status != prev:
            changes.append(dict(wallet=w, was=prev or "new", now=status, score=st["score"], win=st["win"],
                                median_exit=st["median_exit"], early_good=st["early_good"]))
            if status == "whale" and prev != "whale":
                new_whales.append(w)
    for w, entry in book.wallets.items():            # known wallets that went quiet keep their status, marked stale
        if w not in scores:
            entry["stale_since"] = entry.get("stale_since") or now
        else:
            entry.pop("stale_since", None)
    if trace is not None:
        for w in new_whales[:MAX_TRACES]:
            try:
                linked = trace(w, banned)
            except Exception as exc:  # noqa: BLE001 - public RPC can fail; try again next generation
                log_fn(f"whales: trace of {w[:6]}… failed: {exc}")
                continue
            book.wallets[w]["traced"] = now
            if linked:
                book.wallets[w]["flag"] = f"money linked to banned wallet(s) {', '.join(x[:6] + '…' for x in linked)}"
                log_fn(f"whales: {w[:6]}… is linked to banned wallets, not trusted")
    book.save()
    for c in changes:
        log_fn(f"whales: {c['wallet'][:6]}…{c['wallet'][-4:]} {c['was']} → {c['now']} (score {c['score']}, "
               f"win {c['win'] if c['win'] is None else round(c['win'] * 100)}%, early on {c['early_good']} good tokens)")
    return changes


def chain_trace(wallet: str, banned: set[str]) -> list[str]:
    """Banned wallets among this wallet's direct SOL counterparties (public RPC, one hop)."""
    import asyncio

    import httpx

    from solmeme.lab.forensics import load_history, parse_history

    async def go():
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as http:
            src, rows = await load_history(http, wallet, 40)
            edges, _ = parse_history(src, rows, wallet)
            peers = {e["dest"] if e["source"] == wallet else e["source"] for e in edges}
            return sorted(peers & banned)

    return asyncio.run(go())

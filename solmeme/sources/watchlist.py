"""Watchlist: which tokens deserve scarce resources (billed trade streams,
DexScreener batch polls, GMGN security lookups).

Priority: held/pending positions > KOL/smart-money buys > paid Dex boosts >
GMGN trench listings > fresh migrations > fresh launches (only if
watch_launch_window_s > 0). The policy is global — never per strategy — so
every strategy version (including future challengers) sees the same data.
"""

from __future__ import annotations

RANK = {"held": 0, "kol": 1, "boost": 2, "trench": 3, "signal": 3, "migrated": 4, "launch": 5}


class Watchlist:
    def __init__(self, ttl_s: float = 1800, launch_window_s: float = 0, max_size: int = 2000):
        self.ttl_s = ttl_s
        self.launch_window_s = launch_window_s
        self.max_size = max_size
        self.reasons: dict[str, dict[str, float]] = {}
        self.pinned: set[str] = set()
        self.pin_rank: dict[str, int] = {}      # lower first: what the live wallet / champions hold beats swarm holdings

    def touch(self, mint: str, reason: str, ts: float) -> None:
        self.reasons.setdefault(mint, {})[reason] = ts

    def observe(self, ev) -> None:
        k = ev.kind
        if k == "launch" and self.launch_window_s > 0:
            self.touch(ev.mint, "launch", ev.ts)
        elif k == "migration":
            self.touch(ev.mint, "migrated", ev.ts)
        elif k == "wallet_trade" and ev.side == "buy":
            self.touch(ev.mint, "kol", ev.ts)
        elif k == "signal":
            if ev.signal.startswith("dex_"):
                self.touch(ev.mint, "boost", ev.ts)
            elif ev.signal.startswith("gmgn_"):
                self.touch(ev.mint, "trench", ev.ts)
            else:
                self.touch(ev.mint, "signal", ev.ts)

    def set_pinned(self, mints: set[str], ranks: dict[str, int] | None = None) -> None:
        self.pinned = set(mints)
        self.pin_rank = {m: (ranks or {}).get(m, 9) for m in self.pinned}

    def _alive(self, reason: str, ts: float, now: float) -> bool:
        ttl = self.launch_window_s if reason == "launch" else self.ttl_s
        return now - ts <= ttl

    def priority_list(self, now: float) -> list[str]:
        scored = []
        for mint in list(self.reasons):
            live = {r: t for r, t in self.reasons[mint].items() if self._alive(r, t, now)}
            if not live:
                del self.reasons[mint]
                continue
            self.reasons[mint] = live
            best = min(RANK[r] for r in live)
            scored.append((best, -max(live.values()), mint))
        scored.sort()
        ordered = [m for _, _, m in scored if m not in self.pinned]
        pinned = sorted(self.pinned, key=lambda m: (self.pin_rank.get(m, 9), m))
        return [*pinned, *ordered][: self.max_size]

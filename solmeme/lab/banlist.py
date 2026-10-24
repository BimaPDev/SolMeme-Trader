"""Wallet ban list: pump-and-dump operators the bot should never buy alongside.

A token is flagged live when its creator is banned or a banned wallet buys it, and the global
prefilter (`banned_wallets >= 1`) then keeps every strategy out. Entries live in
<data>/banned_wallets.json so they survive restarts and deploys.

Each swarm generation also looks for new suspects in the recorded trades:
  * coordinated clusters: wallets that keep buying the same tokens within seconds of each other
    (one operator spreading buys to fake organic demand)
  * rug creators: a token's creator whose launch pumped and then lost 90%+ within minutes
Candidates go to the council when it is on (judges approve or reject from the evidence);
otherwise they are banned directly. Rejected candidates are not proposed again for a day.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

FILE = "banned_wallets.json"
REJECT_MEMORY_S = 86400


@dataclass
class Ban:
    address: str
    role: str                 # creator | cluster | funder | operation | manual
    reason: str
    source: str = "manual"    # manual | auto | council
    ts: float = field(default_factory=time.time)
    evidence: dict = field(default_factory=dict)


class BanList:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / FILE
        self.bans: dict[str, Ban] = {}
        self.rejected: dict[str, float] = {}      # address -> ts the council said no
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.bans = {b["address"]: Ban(**b) for b in raw.get("bans", [])}
        self.rejected = {k: float(v) for k, v in (raw.get("rejected") or {}).items()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"bans": [asdict(b) for b in self.bans.values()], "rejected": self.rejected},
                                  indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @property
    def addresses(self) -> set[str]:
        return set(self.bans)

    def add(self, address: str, role: str, reason: str, source: str = "manual", evidence: dict | None = None) -> bool:
        if address in self.bans:
            return False
        self.bans[address] = Ban(address, role, reason, source, time.time(), evidence or {})
        self.rejected.pop(address, None)
        return True

    def remove(self, address: str) -> bool:
        return self.bans.pop(address, None) is not None

    def reject(self, address: str, now: float | None = None) -> None:
        self.rejected[address] = now or time.time()

    def recently_rejected(self, address: str, now: float | None = None) -> bool:
        return (now or time.time()) - self.rejected.get(address, 0) < REJECT_MEMORY_S


# ── detection (offline, over recorded events) ─────────────────────────────
def scan(events: Iterable) -> tuple[list[tuple], dict, dict]:
    """One pass: wallet buys (ts, wallet, mint), creators by mint, and market cap paths by mint."""
    buys: list[tuple] = []
    creators: dict[str, tuple[str, float]] = {}
    mcap: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for ev in events:
        k = ev.kind
        if k == "wallet_trade" and ev.side == "buy":
            buys.append((ev.ts, ev.wallet, ev.mint))
        elif k == "launch" and ev.creator:
            creators[ev.mint] = (ev.creator, ev.ts)
        elif k == "enrichment":
            m = ev.data.get("mcap_usd")
            if m:
                mcap[ev.mint].append((ev.ts, float(m)))
    return buys, creators, mcap


MIN_CLUSTER_SHARED = 8          # tokens bought together within 30s before wallets count as one operator
MIN_RUG_PEAK_USD = 10_000       # a rug that never reached this market cap blocked nothing worth buying
MIN_RUG_CRASH_S = 20            # "crashes" faster than this are mostly two data feeds disagreeing


def find_clusters(buys: list[tuple], window_s: float = 30, min_shared: int = MIN_CLUSTER_SHARED,
                  min_overlap: float = 0.8) -> list[dict]:
    """Groups of wallets that bought the same tokens within window_s of each other, again and again.
    min_overlap: the shared tokens must be most of each wallet's tokens (a cluster acts as one)."""
    first: dict[str, dict[str, float]] = defaultdict(dict)
    for ts, w, m in sorted(buys):
        first[w].setdefault(m, ts)
    wallets = [w for w, ms in first.items() if len(ms) >= min_shared]
    links: dict[str, set[str]] = defaultdict(set)
    shared_of: dict[tuple[str, str], int] = {}
    for i, a in enumerate(wallets):
        for b in wallets[i + 1:]:
            n = sum(1 for m, t in first[a].items() if m in first[b] and abs(first[b][m] - t) <= window_s)
            if n >= min_shared and n >= min_overlap * min(len(first[a]), len(first[b])):
                links[a].add(b)
                links[b].add(a)
                shared_of[(a, b)] = n
    seen, out = set(), []
    for w in links:
        if w in seen:
            continue
        group, stack = set(), [w]
        while stack:
            x = stack.pop()
            if x in group:
                continue
            group.add(x)
            stack.extend(links[x] - group)
        seen |= group
        members = sorted(group)
        shared = min(n for (a, b), n in shared_of.items() if a in group and b in group)
        out.append(dict(wallets=members, shared_tokens=shared,
                        tokens_each={w: len(first[w]) for w in members}))
    return out


def find_rug_creators(creators: dict, mcap: dict, pump_x: float = 4.0, crash: float = 0.1,
                      within_s: float = 1800, crash_window_s: float = 300) -> list[dict]:
    """Creators whose token went at least pump_x above its first market cap and then fell to
    `crash` of its peak within crash_window_s, all inside within_s of launch."""
    out = []
    for mint, (creator, launch_ts) in creators.items():
        path = sorted(p for p in mcap.get(mint, []) if launch_ts <= p[0] <= launch_ts + within_s)
        if len(path) < 5:
            continue
        start = path[0][1]
        peak_ts, peak = max(path, key=lambda p: p[1])
        if peak < pump_x * start:
            continue
        after = [m for t, m in path if peak_ts < t <= peak_ts + crash_window_s]
        if peak < MIN_RUG_PEAK_USD:
            continue
        if after and min(after) <= crash * peak:
            crash_s = next(t for t, m in path if t > peak_ts and m <= crash * peak) - peak_ts
            if crash_s < MIN_RUG_CRASH_S:
                continue
            out.append(dict(creator=creator, mint=mint, start_mcap=start, peak_mcap=peak, low_after=min(after),
                            peak_after_s=peak_ts - launch_ts, crash_s=crash_s))
    return out


def candidates(bans: BanList, buys: list[tuple], creators: dict, mcap: dict, now: float) -> list[dict]:
    """New ban candidates with their evidence (already banned or recently rejected ones are skipped)."""
    out = []
    for c in find_clusters(buys):
        fresh = [w for w in c["wallets"] if w not in bans.bans and not bans.recently_rejected(w, now)]
        if fresh:
            out.append(dict(kind="cluster", wallets=fresh, role="cluster",
                            reason=f"{len(c['wallets'])} wallets bought the same {c['shared_tokens']}+ tokens within 30s of each other",
                            evidence=c))
    for r in find_rug_creators(creators, mcap):
        w = r["creator"]
        if w not in bans.bans and not bans.recently_rejected(w, now):
            out.append(dict(kind="rug_creator", wallets=[w], role="creator",
                            reason=f"created {r['mint'][:6]}…: ${r['start_mcap']:,.0f} → peak ${r['peak_mcap']:,.0f} "
                                   f"then ${r['low_after']:,.0f} within {r['crash_s']:.0f}s",
                            evidence=r))
    return out


def prune(bans: BanList) -> list[str]:
    """Drop automatic bans that the current (stricter) rules would not make. Manual bans stay."""
    gone = []
    for addr, b in list(bans.bans.items()):
        if b.source == "manual":
            continue
        e = b.evidence or {}
        if b.role == "creator":
            keep = (e.get("peak_mcap") or 0) >= MIN_RUG_PEAK_USD and (e.get("crash_s") or 0) >= MIN_RUG_CRASH_S
        elif b.role == "cluster":
            keep = (e.get("shared_tokens") or 0) >= MIN_CLUSTER_SHARED
        else:
            keep = True
        if not keep:
            bans.remove(addr)
            gone.append(addr)
    return gone


def update(settings, store, events_fn, window: tuple[float, float], log_fn=lambda _: None, now: float | None = None) -> list[dict]:
    """Scan the window, ask the council about new candidates (when on), and save. Returns decisions."""
    now = now or time.time()
    bans = BanList(settings.data_dir)
    buys, creators, mcap = scan(events_fn(*window))
    cands = candidates(bans, buys, creators, mcap, now)
    if not cands:
        return []
    verdicts = None
    if settings.council.enabled:
        from solmeme.ai.council import review_bans
        verdicts = review_bans(settings, store, cands, log_fn=log_fn)
    decisions = []
    for i, c in enumerate(cands):
        ok = True if verdicts is None else verdicts.get(i, (True, "no judge answered"))[0]
        why = "" if verdicts is None else verdicts.get(i, (True, ""))[1]
        for w in c["wallets"]:
            if ok:
                bans.add(w, c["role"], c["reason"], source="council" if verdicts is not None else "auto", evidence=c["evidence"])
            else:
                bans.reject(w, now)
        decisions.append(dict(c, banned=ok, council=why))
        log_fn(f"ban list: {'BANNED' if ok else 'not banned'} {len(c['wallets'])} wallet(s): {c['reason']}"
               + (f" (council: {why})" if why else ""))
    bans.save()
    return decisions

"""Narratives: which words are in the air (tweets, j7tracker's AI coin guesses, news) and which
launches ride them. No LLM: words from a token's name and symbol are matched against recent
headlines, and launches are indexed by word so a theme's crowding and hit rate can be read.

Everything is keyed by local receive time, like every other event, so replays see the same
matches the live bot saw.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque

TWEET_SOURCES = {"j7tracker"}
AI_SOURCES = {"j7ai"}
NEWS_SOURCES = {"gnews", "gtrends", "reddit"}
TWEET_WINDOW_S = 1800.0
NEWS_WINDOW_S = 6 * 3600.0
THEME_WINDOW_S = 3600.0
KEEP_S = NEWS_WINDOW_S
COPY_WINDOW_S = 7200.0      # same ticker/name within 2h of the first launch = one copycat cluster
MAX_RECENT = 200

_WORD = re.compile(r"[a-z0-9]+")
STOP = set("""
the and for you your are was were with this that from have has had not but all any can our out who what when
where why how its his her she him they them their there here just like get got will would could should about
into over under more most some than then too very now new one two off via amp http https www com status
coin coins token tokens sol solana pump pumpfun fun meme memes official real inu ceo dev launch launched
buy sell hold moon gem ape based the community chain crypto bonk usd usdc wif
""".split())


def words(text: str) -> set[str]:
    """Words worth matching: lowercase, 3+ letters, not stop words or crypto filler."""
    return {w for w in _WORD.findall((text or "").lower()) if len(w) >= 3 and w not in STOP and not w.isdigit()}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def clone_keys(name: str, symbol: str) -> set[str]:
    """What copycats share: the ticker, or the full name (spaces, case and emoji ignored)."""
    keys = set()
    if len(_norm(symbol)) >= 2:
        keys.add("s:" + _norm(symbol))
    if len(_norm(name)) >= 3:
        keys.add("n:" + _norm(name))
    return keys


def token_words(name: str, symbol: str) -> set[str]:
    out = words(name) | words(symbol)
    sym = re.sub(r"[^a-z0-9]", "", (symbol or "").lower())
    if len(sym) >= 3 and sym not in STOP:
        out.add(sym)
    return out


class Narratives:
    def __init__(self):
        self.by_word: dict[str, deque] = defaultdict(deque)       # word -> (ts, source, followers, id)
        self.tickers: dict[str, deque] = defaultdict(deque)       # j7 AI ticker / name word -> ts
        self.launches: dict[str, deque] = defaultdict(deque)      # word -> (ts, mint)
        self.clones: dict[str, deque] = defaultdict(deque)        # ticker / name key -> (ts, mint), first = the OG
        self.copied: deque = deque(maxlen=200)                    # OGs that just got a new copy (live: watch them)
        self.recent: dict[str, deque] = {k: deque(maxlen=MAX_RECENT) for k in ("tweets", "ai", "news")}   # for the dashboard
        self._seen: set[tuple[str, str]] = set()
        self._order: deque = deque()
        self._pruned = 0.0

    # ── ingest ────────────────────────────────────────────────────────────
    def add_headline(self, ev) -> None:
        key = (ev.source, ev.id or ev.url or ev.text[:120])
        if key in self._seen:
            return
        self._seen.add(key)
        self._order.append(key)
        if len(self._order) > 50_000:
            self._seen.discard(self._order.popleft())
        ws = words(ev.text) | words(ev.name)
        for w in ws:
            self.by_word[w].append((ev.ts, ev.source, ev.followers or 0.0, key[1]))
        if ev.source in AI_SOURCES:
            for t in {re.sub(r"[^a-z0-9]", "", ev.ticker.lower())} | words(ev.name):
                if len(t) >= 2:
                    self.tickers[t].append(ev.ts)
        kind = "ai" if ev.source in AI_SOURCES else "news" if ev.source in NEWS_SOURCES else "tweets"
        self.recent[kind].append(dict(ts=ev.ts, source=ev.source, author=ev.author, followers=ev.followers,
                                text=ev.text[:280], url=ev.url, ticker=ev.ticker, name=ev.name, words=sorted(ws)[:12]))
        self._prune(ev.ts)

    def add_launch(self, ts: float, mint: str, name: str, symbol: str) -> None:
        for w in token_words(name, symbol):
            self.launches[w].append((ts, mint))
        for k in clone_keys(name, symbol):
            q = self.clones[k]
            while q and q[0][0] < ts - COPY_WINDOW_S:
                q.popleft()
            if q and q[0][1] != mint and all(m != mint for _, m in q):
                self.copied.append(q[0][1])
            if all(m != mint for _, m in q):
                q.append((ts, mint))

    def _prune(self, now: float) -> None:
        if now - self._pruned < 60:
            return
        self._pruned = now
        for book, keep in ((self.by_word, KEEP_S), (self.launches, THEME_WINDOW_S * 6)):
            for w in list(book):
                q = book[w]
                while q and q[0][0] < now - keep:
                    q.popleft()
                if not q:
                    del book[w]
        for k in list(self.clones):
            q = self.clones[k]
            while q and q[0][0] < now - COPY_WINDOW_S * 2:
                q.popleft()
            if not q:
                del self.clones[k]
        for w in list(self.tickers):
            q = self.tickers[w]
            while q and q[0] < now - TWEET_WINDOW_S:
                q.popleft()
            if not q:
                del self.tickers[w]

    # ── per-token reads (features) ────────────────────────────────────────
    def _hits(self, ws: set[str], now: float, window: float, sources) -> list[tuple]:
        seen, out = set(), []
        for w in ws:
            for ts, src, fol, hid in reversed(self.by_word.get(w, ())):
                if ts > now:
                    continue
                if ts < now - window:
                    break
                if src in sources and hid not in seen:
                    seen.add(hid)
                    out.append((ts, src, fol, hid))
        return out

    def tweet_hits(self, ws: set[str], now: float) -> list[tuple]:
        return self._hits(ws, now, TWEET_WINDOW_S, TWEET_SOURCES)

    def news_hits(self, ws: set[str], now: float) -> list[tuple]:
        return self._hits(ws, now, NEWS_WINDOW_S, NEWS_SOURCES)

    def ai_match(self, name: str, symbol: str, now: float) -> bool:
        sym = re.sub(r"[^a-z0-9]", "", (symbol or "").lower())
        keys = ({sym} if len(sym) >= 2 else set()) | words(name)
        return any(now - TWEET_WINDOW_S <= ts <= now for k in keys for ts in self.tickers.get(k, ()))

    def theme_launches(self, ws: set[str], mint: str, now: float) -> int:
        mints = set()
        for w in ws:
            for ts, m in reversed(self.launches.get(w, ())):
                if ts > now:
                    continue
                if ts < now - THEME_WINDOW_S:
                    break
                if m != mint:
                    mints.add(m)
        return len(mints)

    def og_info(self, mint: str, name: str, symbol: str, now: float) -> tuple[int, int, str] | None:
        """(rank among same-ticker/name launches, 1 = the original; copies launched after it so far; the OG's
        mint), from the cluster with the most copies. None when the token was never seen launching."""
        best = None
        for k in clone_keys(name, symbol):
            q = [x for x in self.clones.get(k, ()) if x[0] <= now]
            mine = next((i for i, (_, m) in enumerate(q) if m == mint), None)
            if mine is None:
                continue
            info = (mine + 1, len(q) - mine - 1, q[0][1])
            if best is None or len(q) > best[1] + best[0]:
                best = info
        return best

    def clusters(self, market, now: float, top: int = 15) -> list[dict]:
        """Live copycat clusters: an OG with 2+ copies in the last 2h, newest activity first."""
        seen, out = set(), []
        for k, q in self.clones.items():
            live = [x for x in q if now - COPY_WINDOW_S <= x[0] <= now]
            if len(live) < 3 or live[0][1] in seen:
                continue
            seen.add(live[0][1])
            og = market.tokens.get(live[0][1])
            x = og.price_sol / og.first_price_sol if og is not None and og.price_sol and og.first_price_sol else None
            out.append(dict(key=k[2:], og=live[0][1], symbol=(og.symbol if og else "") or k[2:], og_ts=live[0][0],
                            copies=len(live) - 1, last_copy_ts=live[-1][0], og_x=x,
                            og_mcap_sol=og.mcap_sol if og is not None else None))
        out.sort(key=lambda c: (-c["last_copy_ts"]))
        return out[:top]

    # ── dashboard ─────────────────────────────────────────────────────────
    def themes(self, market, now: float, top: int = 20) -> list[dict]:
        """Words with the most launches in the last hour, how many of those ran 2x, and the tweets
        and news behind them."""
        rows = []
        for w, q in self.launches.items():
            mints = {m for ts, m in q if now - THEME_WINDOW_S <= ts <= now}
            if len(mints) < 3:
                continue
            ran = 0
            for m in mints:
                st = market.tokens.get(m)
                if st is not None and st.first_price_sol and st.ath_price_sol and st.ath_price_sol >= 2 * st.first_price_sol:
                    ran += 1
            tw = [h for h in self.by_word.get(w, ()) if now - TWEET_WINDOW_S <= h[0] <= now and h[1] not in NEWS_SOURCES]
            nw = [h for h in self.by_word.get(w, ()) if now - NEWS_WINDOW_S <= h[0] <= now and h[1] in NEWS_SOURCES]
            rows.append(dict(word=w, launches=len(mints), ran_2x=ran, hit_pct=round(100 * ran / len(mints)),
                             tweets=len(tw), news=len(nw), max_followers=max((h[2] for h in tw), default=0)))
        rows.sort(key=lambda r: (-r["launches"], -r["ran_2x"]))
        return rows[:top]

    def headlines(self, kind: str, n: int = 40) -> list[dict]:
        return list(self.recent[kind])[-n:][::-1]

    def all_recent(self) -> list[dict]:
        return sorted((h for q in self.recent.values() for h in list(q)), key=lambda h: h["ts"])

    def source_counts(self) -> dict[str, int]:
        return dict(Counter(h["source"] for q in self.recent.values() for h in list(q)))

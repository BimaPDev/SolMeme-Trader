"""Free news and trends, as Headline events for the narrative matcher (features/narrative.py).

  gnews    Google News top stories RSS (US)
  gtrends  Google Trends daily trending searches RSS (US)
  reddit   r/all hot posts (public Atom feed; the JSON API is blocked without a login)

Headlines are untrusted text: they are only split into words and matched, never followed or
fed to a model as instructions.
"""

from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET

from solmeme.core.events import Headline
from solmeme.sources.base import Backoff, Context, poll_loop

UA = {"User-Agent": "Mozilla/5.0 (compatible; SolMemeTrader/0.1; paper research bot)"}
FEEDS = {
    "gnews": "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
    "gtrends": "https://trends.google.com/trending/rss?geo=US",
}
REDDIT = "https://www.reddit.com/r/all/hot/.rss?limit=50"
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_BYTES = 2_000_000


def parse_rss(source: str, body: str) -> list[Headline]:
    try:
        root = ET.fromstring(body[:MAX_BYTES])
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        link = (item.findtext("link") or "").strip()
        extra = ""
        if source == "gtrends":     # the news titles under each trending search say what it is about
            extra = " ".join((n.text or "") for n in item.iter() if n.tag.endswith("news_item_title"))[:300]
        hid = title if source == "gtrends" else (link or title)    # every Trends item links to the same page
        out.append(Headline(ts=0.0, source=source, id=hid, text=f"{title} {extra}".strip()[:400], url=link))
    return out


def parse_reddit(body: str) -> list[Headline]:
    try:
        root = ET.fromstring(body[:MAX_BYTES])
    except ET.ParseError:
        return []
    out = []
    for e in root.iter(ATOM + "entry"):
        title = (e.findtext(ATOM + "title") or "").strip()
        if not title:
            continue
        link = e.find(ATOM + "link")
        cat = e.find(ATOM + "category")
        out.append(Headline(ts=0.0, source="reddit", id=(e.findtext(ATOM + "id") or title),
                            author=(cat.get("label") if cat is not None else "") or "", text=title[:400],
                            url=link.get("href") if link is not None else ""))
    return out


class News:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.cfg = ctx.settings.sources.news
        self.seen: set[tuple[str, str]] = set()

    def _push(self, items: list[Headline]) -> None:
        now = time.time()
        for h in items:
            key = (h.source, h.id)
            if key in self.seen:
                continue
            self.seen.add(key)
            h.ts = now
            self.ctx.push("news", h)
        if len(self.seen) > 20_000:
            self.seen.clear()

    def _feed(self, source: str, url: str):
        async def poll() -> None:
            r = await self.ctx.http.get(url, headers=UA, timeout=20, follow_redirects=True)
            r.raise_for_status()
            self._push(parse_rss(source, r.text))
        return poll

    async def poll_reddit(self) -> None:
        r = await self.ctx.http.get(REDDIT, headers=UA, timeout=20, follow_redirects=True)
        r.raise_for_status()
        self._push(parse_reddit(r.text))

    async def run(self) -> None:
        self.ctx.stat("news")
        loops = [poll_loop(self.ctx, "news", self.cfg.poll_s, self._feed(s, u), Backoff(60, 1800)) for s, u in FEEDS.items()]
        if self.cfg.reddit:
            loops.append(poll_loop(self.ctx, "news", self.cfg.poll_s, self.poll_reddit, Backoff(120, 3600)))
        await asyncio.gather(*loops)

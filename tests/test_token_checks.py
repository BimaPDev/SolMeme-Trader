"""Offline notes when a source checks a mint. No network."""

from __future__ import annotations

import asyncio

from solmeme.sources.base import Context
from solmeme.sources.dexscreener import DexScreener
from solmeme.sources.gmgn import Gmgn
from solmeme.sources.misc import Rugcheck


class Rec:
    def __init__(self):
        self.rows = []

    def note(self, mint, phase, detail, **kw):
        self.rows.append((mint, phase, detail, kw))


class Watch:
    def __init__(self, mints):
        self.mints = list(mints)

    def priority_list(self, now):
        return list(self.mints)


def _ctx(settings, watch, activity, http=None):
    pushed = []
    ctx = Context(settings=settings, http=http, emit=pushed.append, watch=watch)
    ctx.activity = activity
    return ctx, pushed


def test_dexscreener_notes_resolved_mints(settings):
    ctx, pushed = _ctx(settings, Watch(["MintAAA", "MintMissing"]), Rec())
    src = DexScreener(ctx)

    async def _get(path):
        assert "MintAAA" in path and "MintMissing" in path
        return [{
            "chainId": "solana",
            "dexId": "pumpfun",
            "pairAddress": "P",
            "baseToken": {"address": "MintAAA", "symbol": "DOG"},
            "quoteToken": {"symbol": "SOL"},
            "priceUsd": "0.01",
            "liquidity": {"usd": 1000},
        }]

    src._get = _get
    asyncio.run(src.poll_watch())

    assert ctx.activity.rows == [
        ("MintAAA", "checking", "dex price", {"symbol": "DOG", "source": "dex price"}),
    ]
    assert len(pushed) == 1
    assert pushed[0].source == "dexscreener" and pushed[0].mint == "MintAAA"
    assert pushed[0].data["symbol"] == "DOG"


def test_dexscreener_poll_watch_without_activity(settings):
    pushed = []
    ctx = Context(settings=settings, http=None, emit=pushed.append, watch=Watch(["MintAAA"]))
    src = DexScreener(ctx)

    async def _get(path):
        return [{
            "chainId": "solana",
            "baseToken": {"address": "MintAAA", "symbol": "DOG"},
            "quoteToken": {"symbol": "SOL"},
            "priceUsd": "0.01",
        }]

    src._get = _get
    asyncio.run(src.poll_watch())
    assert len(pushed) == 1 and pushed[0].mint == "MintAAA"


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._body


def test_rugcheck_notes_only_found_reports(settings):
    class Http:
        async def get(self, url, timeout=15):
            if url.endswith("/MintMissing/report/summary"):
                return _Resp(404, {})
            return _Resp(200, {"score": 4, "score_normalised": 12, "risks": [{"name": "mint"}]})

    ctx, pushed = _ctx(settings, Watch(["MintMissing", "MintAAA"]), Rec(), http=Http())
    asyncio.run(Rugcheck(ctx).poll())

    assert ctx.activity.rows == [
        ("MintAAA", "checking", "rugcheck", {"symbol": "", "source": "rugcheck"}),
    ]
    assert len(pushed) == 1 and pushed[0].source == "rugcheck" and pushed[0].mint == "MintAAA"


def test_gmgn_security_notes_dict_payloads(settings):
    ctx, pushed = _ctx(settings, Watch(["MintBad", "MintAAA"]), Rec())
    src = Gmgn(ctx)

    async def _req(method, path, params, body=None):
        if params.get("address") == "MintBad":
            return ["not-a-dict"]
        return {"symbol": "SEC", "is_honeypot": False, "rug_ratio": 0.1}

    src._req = _req
    asyncio.run(src.poll_security())

    assert ctx.activity.rows == [
        ("MintAAA", "checking", "gmgn security", {"symbol": "SEC", "source": "gmgn security"}),
    ]
    assert len(pushed) == 1 and pushed[0].source == "gmgn_security" and pushed[0].mint == "MintAAA"

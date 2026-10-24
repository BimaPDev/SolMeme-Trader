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
    settings.sources.gmgn.security_max_per_min = 20          # 3 coins a poll, so both are looked up
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


def test_the_global_filter_wants_the_coins_own_socials(settings):
    """Oct 11, HOMELESS: one link to someone's tweet passed "socials >= 1"; the link must be the coin's own."""
    from solmeme.features.registry import FeatureView
    from solmeme.features.token_state import MarketState

    from solmeme.config import load_settings

    rule = next(r for r in load_settings().prefilter.all if r["id"] == "no_socials")      # config/settings.yaml
    assert rule["f"] == "owned_socials_count" and rule["v"] == 1
    m = MarketState()
    borrowed, own = m._token("B" * 40 + "pump", 0.0), m._token("O" * 40 + "pump", 0.0)
    borrowed.gmgn = {"twitter": "https://x.com/someone/status/1977"}
    own.gmgn = {"twitter": "https://x.com/homeless_coin", "website": "https://pump.fun/coin/x"}
    assert FeatureView(borrowed, 1.0, m).get("owned_socials_count") == 0
    assert FeatureView(own, 1.0, m).get("owned_socials_count") == 1        # the pump.fun page doesn't count


def test_the_global_filter_skips_coins_already_falling_from_their_high():
    from solmeme.config import load_settings

    rule = next(r for r in load_settings().prefilter.none if r["id"] == "falling")
    assert (rule["f"], rule["op"], rule["v"]) == ("dd_from_ath_pct", ">", 10)


def test_the_global_filter_blocks_coins_not_paired_with_sol():
    from solmeme.config import load_settings
    from solmeme.features.registry import FeatureView
    from solmeme.features.token_state import MarketState

    rule = next(r for r in load_settings().prefilter.none if r["id"] == "not_sol_pair")
    assert (rule["f"], rule["op"], rule["v"]) == ("pump_quote_sol", "==", False)
    m = MarketState()
    st = m._token("Z" * 40 + "pump", 0.0)
    assert FeatureView(st, 1.0, m).get("pump_quote_sol") is None         # unknown: no veto
    st.ext["pump_coin"] = {"quote_sol": False}
    assert FeatureView(st, 1.0, m).get("pump_quote_sol") is False


def test_the_global_filter_skips_coins_that_already_ran_3x():
    from solmeme.config import load_settings

    rule = next(r for r in load_settings().prefilter.none if r["id"] == "ran_up")
    assert (rule["f"], rule["op"], rule["v"]) == ("x_from_first_price", ">=", 3)


def test_a_crash_before_we_saw_the_coin_still_counts_as_falling():
    from solmeme.config import load_settings
    from solmeme.features.registry import FeatureView
    from solmeme.features.token_state import MarketState

    assert any(r["id"] == "falling_pump" for r in load_settings().prefilter.none)
    m = MarketState(); m.sol_usd = 100.0
    st = m._token("E" * 40 + "pump", 0.0)
    st.set_price(0.0, 2.333e-7)                                 # 233 SOL = $23,330: our own "high" is right now
    assert FeatureView(st, 1.0, m).get("dd_from_ath_pct") == 0
    st.ext["pump_coin"] = {"ath_mcap_usd": 161026.0}            # pump.fun saw it at $161k
    assert round(FeatureView(st, 1.0, m).get("pump_dd_from_ath_pct")) == 86

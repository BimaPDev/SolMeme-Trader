"""Find wallets to paper-copy from KOL feeds and a name search."""

import asyncio

from solmeme.lab.people import TraderBook, discover, fomo_cards, gather_traders, is_address, judge, lookup, search_wallets

W = "LbUiWL3xVV8hTFYBVdbTNrpDo41NKS6o3LHHuDzjfcY"
MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"


def test_judge_copy_skip_and_dev():
    verdict, reasons = judge({"winrate": 0.6, "pnl": 1.4, "buy_count": 20, "common": {}})
    assert verdict == "copy" and any("win" in r for r in reasons)
    verdict, _ = judge({"winrate": 0.8, "pnl": 3, "buy_count": 400})
    assert verdict == "skip"
    verdict, reasons = judge({"buy_count": 2, "common": {"created_token_count": 8}})
    assert verdict == "skip" and any("dev" in r for r in reasons)


def test_gather_keeps_buys_only():
    rows = [
        {"maker": W, "event_type": "buy", "base_address": MINT, "symbol": "OTTR", "cost_usd": 40},
        {"maker": W, "event_type": "sell", "base_address": MINT, "symbol": "OTTR", "cost_usd": 10},
        {"maker": W, "event_type": "transferIn", "base_address": MINT},
    ]
    got = gather_traders(rows, "kol")
    assert got[W]["buys"] == 1 and got[W]["usd"] == 40 and got[W]["symbols"] == ["OTTR"]


def test_search_wallets_and_address():
    assert is_address(W)
    assert not is_address("otter")
    hits = search_wallets({"wallets": [{"address": W, "twitter_username": "otter", "wallet_tags": ["kol"]}]})
    assert hits[0]["twitter"] == "otter"


def test_discover_ranks_a_profitable_wallet_first():
    calls = []

    async def req(method, path, params, body=None):
        calls.append(path)
        if path.endswith("kol"):
            return {"list": [{"maker": W, "event_type": "buy", "base_address": MINT,
                              "symbol": "OTTR", "cost_usd": 80}]}
        if path.endswith("smartmoney"):
            return {"list": []}
        if path.endswith("wallet_stats"):
            assert params["wallet_address"] == W and params["period"] == "7d"
            return {"wallet_address": W, "winrate": 0.55, "pnl": 1.2, "buy_count": 12,
                    "common": {"name": "Otter", "twitter_username": "otter"}}
        return {}

    cards = asyncio.run(discover(req, limit=5, followed=set()))
    assert cards[0]["verdict"] == "copy"
    assert "Otter" in cards[0]["text"] and "OTTR" in cards[0]["text"]
    assert "paper only" in cards[0]["text"]


def test_trader_book_ranks_active_buyers_and_drops_a_stale_board():
    other = "So11111111111111111111111111111111111111112"
    book = TraderBook()
    book.add_leaderboard([{"address": W, "handle": "otter"}, {"address": other, "handle": "quiet"}])
    book.add_trade(other, "buy", 500, "BONK", "fomo")
    cards = book.cards()
    assert cards[0]["wallet"] == other
    assert "1 buys" in cards[0]["text"] and "BONK" in cards[0]["text"]
    book.add_leaderboard([])
    left = book.cards()
    assert [c["wallet"] for c in left] == [other]


def test_fomo_cards_use_the_leaderboard_handle():
    cards = fomo_cards([
        {"address": W, "handle": "otter"},
        {"handle": "nameless"},
    ], limit=8)
    assert len(cards) == 1
    assert cards[0]["wallet"] == W and cards[0]["verdict"] == "watch"
    assert cards[0]["text"].startswith("otter  " + W)
    assert "fomo leaderboard" in cards[0]["text"]


def test_lookup_by_name_uses_search():
    async def req(method, path, params, body=None):
        if path.endswith("search"):
            assert params["q"] == "otter"
            return {"wallets": [{"address": W, "twitter_username": "otter", "wallet_tags": []}]}
        if path.endswith("wallet_activity"):
            return {"activities": [{"wallet_address": W, "event_type": "buy", "base_address": MINT,
                                    "token": {"symbol": "OTTR"}, "cost_usd": 15}]}
        if path.endswith("wallet_stats"):
            return {"winrate": 0.2, "pnl": 0.4, "buy_count": 30}
        return {}

    cards = asyncio.run(lookup(req, "otter"))
    assert cards[0]["wallet"] == W and cards[0]["verdict"] == "skip"

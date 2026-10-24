"""pump.fun's trade feed: NATS framing and trade messages → events."""

import json
import math

import pytest

from solmeme.core.events import CurveTrade, PoolTrade
from solmeme.sim import curve
from solmeme.sources.pumpfun_ws import COMPLETE_PRICE, frames, parse_trade


def _msg(mint, px, side="buy", sol="0.1"):
    inner = json.dumps({"mint": mint, "tx": "sig1", "userAddress": "W1", "type": side, "baseAmount": "53577.6",
                        "amountSol": sol, "priceSol": str(px), "marketCap": "1"})
    return json.dumps(inner)                                    # the feed double-encodes: a JSON string of JSON


def test_frames_split_messages_and_keep_partial_payloads():
    a, b = _msg("Apump", 1e-7), _msg("Bpump", 2e-7)
    buf = f"PING\r\nMSG unifiedTradeEvent.lite.Apump 1 {len(a)}\r\n{a}\r\nMSG unifiedTradeEvent.lite.Bpump 1 {len(b)}\r\n{b[:10]}"
    msgs, ctrl, rest = frames(buf)
    assert ctrl == ["PING"] and [s for s, _ in msgs] == ["unifiedTradeEvent.lite.Apump"]
    msgs, _, rest = frames(rest + b[10:] + "\r\n")
    assert [s for s, _ in msgs] == ["unifiedTradeEvent.lite.Bpump"] and rest == ""


def test_curve_trades_carry_reserves_that_reproduce_the_price():
    migrated: set[str] = set()
    ev = parse_trade(_msg("Apump", 1.5e-7), 10.0, migrated)
    assert isinstance(ev, CurveTrade) and ev.side == "buy" and ev.sol == 0.1 and ev.trader == "W1"
    assert curve.price(ev.v_sol, ev.v_tokens) == pytest.approx(1.5e-7)
    assert math.isclose(ev.v_sol * ev.v_tokens, curve.INITIAL_V_SOL * curve.INITIAL_V_TOKENS, rel_tol=1e-9)


def test_a_coin_above_the_curve_is_migrated_from_then_on():
    migrated: set[str] = set()
    assert isinstance(parse_trade(_msg("Mpump", COMPLETE_PRICE * 1.5), 1.0, migrated), PoolTrade)
    assert isinstance(parse_trade(_msg("Mpump", COMPLETE_PRICE * 0.5), 2.0, migrated), PoolTrade)   # dumped after
    assert parse_trade(_msg("0xabc", 1e-7), 3.0, migrated) is None                                  # other chains
    assert parse_trade(_msg("Apump", 1e-7, side="create"), 3.0, migrated) is None


def test_a_tracked_wallets_trades_are_kept_on_any_coin():
    import asyncio, json as _json

    from solmeme.sources import pumpfun_ws as P

    body = _msg("Zpump", 1e-7).replace("W1", "Tracked1")
    frame = f"MSG unifiedTradeEvent.lite.Zpump 1 {len(body)}\r\n{body}\r\n"

    class Stat:
        status = note = ""; errors = events = 0

    class Settings:
        class sources:
            tracked_wallets = ["Tracked1"]
            class pumpfun:
                max_coins, daily_events, min_sol = 60, 0, 0.0005
        data_dir = None

    class Ctx:
        settings = Settings; watch = None; pushed = []
        def stat(self, n): return Stat()
        def push(self, n, ev): self.pushed.append(ev)

    ctx = Ctx(); src = P.PumpFunWS.__new__(P.PumpFunWS)
    src.ctx, src.cfg, src.wanted, src.migrated, src.day, src.today = ctx, Settings.sources.pumpfun, set(), set(), "", 0
    msgs, _, _ = P.frames(frame)
    wallets = src._wallets()
    kept = [m for m, b in msgs if src._keep(m.rsplit(".", 1)[-1]) or any(w in b for w in wallets)]
    assert kept == ["unifiedTradeEvent.lite.Zpump"]                 # not a watched coin, kept for the wallet

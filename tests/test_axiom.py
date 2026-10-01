"""Axiom Pulse parser and feature wiring. Fixtures are reconstructed, not live captures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solmeme.core.events import ExternalSignal, WalletTrade
from solmeme.features.registry import FEATURES
from solmeme.features.token_state import MarketState, TokenState
from solmeme.sources.axiom import Source, parse_http, parse_ws_message
from solmeme.sources.base import Context
from solmeme.sources.browser import BrowserSource

MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
MINT2 = "8qbHbw2BbbTHBW1sbeqakYXVKRQM8Ne7pLK7m6CVfeR"
DEV = "CktRuQ2mttgRGkXJtyksdKHjUdc2C4TgDzyB98oEzy8"
PAIR = "GgBaCs3NCBuZN12kCJgAW63ydqohFkHEdfdEXBPzLHq"
WALLET = "LbUiWL3xVV8hTFYBVdbTNrpDo41NKS6o3LHHuDzjfcY"

FIXTURES = Path(__file__).parent / "fixtures" / "axiom"
SOL = 100.0

_AXIOM_FEATURES = (
    "axiom_listed",
    "axiom_final_stretch",
    "axiom_top10_pct",
    "axiom_dev_hold_pct",
    "axiom_sniper_pct",
    "axiom_insider_pct",
    "axiom_bundle_pct",
    "axiom_holders",
    "axiom_pro_traders",
)


def load(name: str):
    doc = json.loads((FIXTURES / name).read_text())
    assert "reconstructed" in doc["_note"].lower()
    url = doc.get("url")
    if "payload" in doc:
        return doc["payload"], url
    return doc, url


def ws(name: str, **kw):
    payload, _url = load(name)
    return parse_ws_message(payload, **kw)


def feat(st, name: str, now: float = 1.0):
    return FEATURES[name].fn(st, now, None)


def _source(settings) -> Source:
    return Source(Context(settings=settings, http=None, emit=lambda _ev: None))


def test_source_identity():
    assert issubclass(Source, BrowserSource)
    assert Source.name == "axiom"
    assert Source.start_url == "https://axiom.trade/pulse"
    assert Source.login_url == "https://axiom.trade/"


def test_new_pairs_object_and_sol_usd():
    events, recognized, sol = ws("new_pairs.json", sol_usd=SOL)
    assert recognized is True and sol == SOL and len(events) == 1
    ev = events[0]
    assert isinstance(ev, ExternalSignal)
    assert ev.source == "axiom" and ev.ts == 0.0 and ev.mint == MINT
    assert ev.signal == "axiom_new_pair"
    assert ev.data["symbol"] == "DEMO"
    assert ev.data["protocol"] == "Pump"
    assert ev.data["dev_hold_pct"] == pytest.approx(2.7)
    assert ev.data["sniper_pct"] == pytest.approx(16.64)
    assert ev.data["top10_pct"] == pytest.approx(22.5)
    assert ev.data["insider_pct"] == pytest.approx(3.0)
    assert ev.data["bundle_pct"] == pytest.approx(4.0)
    assert ev.data["holders"] == pytest.approx(42)
    assert ev.data["pro_traders"] == pytest.approx(6)
    assert ev.data["mcap_usd"] == pytest.approx(25.0 * SOL)
    assert ev.data["volume_usd"] == pytest.approx(3.5 * SOL)
    assert ev.data["liquidity_usd"] == pytest.approx(4.0 * SOL)
    assert ev.data["twitter"] == "https://x.com/demo"
    assert ev.data["telegram"] == "https://t.me/demo"
    assert ev.data["website"] == "https://example.com"


def test_percent_fraction_boundary():
    """Values <= 1 are fractions; values above 1 are already percents."""
    payload = {
        "room": "new_pairs",
        "content": {
            "token_address": MINT,
            "protocol": "Pump",
            "dev_holds_percent": 1,
            "snipers_hold_percent": 1.01,
            "top_10_holders": 0.027,
            "bondingCurvePercent": 10,
        },
    }
    (ev,) = parse_ws_message(payload)[0]
    assert ev.data["dev_hold_pct"] == pytest.approx(100.0)
    assert ev.data["sniper_pct"] == pytest.approx(1.01)
    assert ev.data["top10_pct"] == pytest.approx(2.7)


def test_pulse_rows_stage_and_liquidity_override():
    stretch, recognized, _ = ws("pulse_final_stretch.json", sol_usd=SOL)
    assert recognized is True and len(stretch) == 1
    ev = stretch[0]
    assert ev.signal == "axiom_final_stretch" and ev.mint == MINT
    assert ev.data["top10_pct"] == pytest.approx(16.64)
    assert ev.data["dev_hold_pct"] == pytest.approx(2.7)
    assert ev.data["sniper_pct"] == pytest.approx(8.0)
    assert ev.data["holders"] == pytest.approx(100)
    assert ev.data["pro_traders"] == pytest.approx(7)
    # index 52 (18 SOL) overrides index 21 (5 SOL)
    assert ev.data["liquidity_usd"] == pytest.approx(18.0 * SOL)
    assert ev.data["mcap_usd"] == pytest.approx(40.0 * SOL)
    assert ev.data["volume_usd"] == pytest.approx(2.5 * SOL)

    fresh, recognized, _ = ws("pulse_new_pair.json", sol_usd=SOL)
    assert recognized is True and len(fresh) == 1
    assert fresh[0].signal == "axiom_new_pair" and fresh[0].mint == MINT2
    assert fresh[0].data["dev_hold_pct"] == pytest.approx(2.7)
    assert fresh[0].data["liquidity_usd"] == pytest.approx(5.0 * SOL)


def test_pump_amm_is_migrated():
    events, recognized, _ = ws("pulse_pump_amm.json")
    assert recognized is True and len(events) == 1
    assert events[0].signal == "axiom_migrated"
    assert events[0].mint == MINT


@pytest.mark.parametrize("protocol", ["Raydium", "Meteora", "Orca", "PumpSwap", "plain amm"])
def test_migrated_protocols(protocol):
    payload = {
        "room": "update_pulse_v2",
        "content": {"token_address": MINT, "protocol": protocol, "bondingCurvePercent": 10},
    }
    (ev,) = parse_ws_message(payload)[0]
    assert ev.signal == "axiom_migrated"


def test_other_stage_rules():
    def one(**content):
        payload = {"room": "update_pulse_v2", "content": {"token_address": MINT, "protocol": "Pump", **content}}
        return parse_ws_message(payload)[0][0].signal

    assert one(bondingCurvePercent=99.5) == "axiom_migrated"
    assert one(bondingCurvePercent=99.49) == "axiom_final_stretch"
    assert one(bondingCurvePercent=75) == "axiom_final_stretch"
    assert one(bondingCurvePercent=74.9) == "axiom_new_pair"
    assert one(migratedAt="2026-01-01T00:00:00Z", bondingCurvePercent=10) == "axiom_migrated"
    assert one(isMigrated=True, bondingCurvePercent=10) == "axiom_migrated"
    assert one(numHolders=3) == "axiom_pulse"

    # new_pairs room wins over a high bonding percent (unless already migrated).
    (ev,) = parse_ws_message({
        "room": "new_pairs",
        "content": {"token_address": MINT, "protocol": "Pump", "bondingCurvePercent": 90},
    })[0]
    assert ev.signal == "axiom_new_pair"


def test_camel_pulse_http():
    doc, url = load("pulse_camel.json")
    events, recognized, sol = parse_http(url, doc, sol_usd=SOL)
    assert recognized is True and sol == SOL and len(events) == 1
    ev = events[0]
    assert ev.signal == "axiom_new_pair" and ev.mint == MINT and ev.ts == 0.0
    assert ev.data["dev_hold_pct"] == pytest.approx(2.7)
    assert ev.data["sniper_pct"] == pytest.approx(16.64)
    assert ev.data["top10_pct"] == pytest.approx(22.5)
    assert ev.data["mcap_usd"] == pytest.approx(25.0 * SOL)
    assert ev.data["liquidity_usd"] == pytest.approx(4.0 * SOL)
    assert ev.data["holders"] == pytest.approx(42)
    assert ev.data["pro_traders"] == pytest.approx(6)


def test_trending_positional_row():
    payload, url = load("trending_v2.json")
    events, recognized, _ = parse_http(url, payload, sol_usd=SOL)
    assert recognized is True and len(events) == 1
    ev = events[0]
    assert ev.mint == MINT and ev.signal == "axiom_final_stretch"
    assert ev.data["protocol"] == "Pump"
    assert ev.data["website"] == "https://example.com"
    assert ev.data["twitter"] == "https://x.com/demo"
    assert ev.data["telegram"] == "https://t.me/demo"
    assert ev.data["top10_pct"] == pytest.approx(16.64)
    assert ev.data["insider_pct"] == pytest.approx(2.7)
    assert ev.data["bundle_pct"] == pytest.approx(4.0)
    assert ev.data["dev_hold_pct"] == pytest.approx(5.0)
    assert ev.data["holders"] == pytest.approx(300)
    assert ev.data["mcap_usd"] == pytest.approx(50000)  # already USD; not rescaled
    assert ev.data["liquidity_usd"] == pytest.approx(1900)
    assert ev.data["volume_usd"] == pytest.approx(20 * SOL)
    assert "sniper_pct" not in ev.data  # sniperCount is not mapped on trending rows


def test_wallet_dict_and_positional_row():
    events, recognized, _ = ws("wallet_ws.json")
    assert recognized is True and len(events) == 1
    ev = events[0]
    assert isinstance(ev, WalletTrade)
    assert ev.source == "axiom" and ev.label == "axiom_tracked" and ev.ts == 0.0
    assert ev.side == "buy" and ev.wallet == WALLET and ev.mint == MINT
    assert ev.sol == pytest.approx(1.5) and ev.usd == pytest.approx(210.0)
    assert ev.symbol == "DEMO" and ev.tx == "axiomwallettx1"

    hyphen = {
        "room": f"v-{WALLET}",
        "content": {
            "type": "sell",
            "maker_address": WALLET,
            "tokenAddress": MINT2,
            "total_sol": 0.4,
            "signature": "axiomwallettx2",
        },
    }
    (sold,) = parse_ws_message(hyphen)[0]
    assert sold.side == "sell" and sold.mint == MINT2 and sold.tx == "axiomwallettx2"

    rows, url = load("wallet_http.json")
    http_events, recognized, _ = parse_http(url, rows)
    assert recognized is True and len(http_events) == 1
    row = http_events[0]
    assert isinstance(row, WalletTrade)
    assert row.label == "axiom_tracked" and row.side == "sell"
    assert row.wallet == WALLET and row.mint == MINT2
    assert row.sol == pytest.approx(1.25) and row.usd == pytest.approx(180.0)
    assert row.symbol == "DEMO" and row.tx == "axiomwallettxrow1"


def test_sol_price_cached_noise_quiet():
    events, recognized, sol = ws("sol_price.json", sol_usd=1.0)
    assert events == [] and recognized is True and sol == pytest.approx(142.5)

    events, recognized, sol = ws("noise_btc.json", sol_usd=SOL)
    assert events == [] and recognized is True and sol == SOL


def test_incremental_uses_pairs_from_full_row():
    pairs: dict[str, str] = {}
    full, recognized, _ = ws("pulse_final_stretch.json", pairs=pairs)
    assert recognized is True and full[0].mint == MINT
    assert pairs[PAIR] == MINT

    payload, _url = load("incremental.json")
    events, recognized, _ = parse_ws_message(payload, sol_usd=SOL, pairs=pairs)
    assert recognized is True and len(events) == 1
    ev = events[0]
    assert ev.mint == MINT and ev.signal == "axiom_final_stretch"
    assert ev.data["top10_pct"] == pytest.approx(2.7)
    assert ev.data["mcap_usd"] == pytest.approx(10 * SOL)

    # no prior row → pair cannot be resolved, still recognized, no event
    alone, recognized, _ = parse_ws_message(payload, pairs={})
    assert alone == [] and recognized is True


def test_wallet_dedupe_by_tx(settings):
    src = _source(settings)
    payload, _url = load("wallet_ws.json")
    raw = json.dumps(payload)
    first = src.on_ws("wss://cluster.axiom.trade/", raw)
    second = src.on_ws("wss://cluster.axiom.trade/", raw)
    assert len(first) == 1 and first[0].tx == "axiomwallettx1"
    assert second == []

    changed = json.loads(raw)
    changed["content"]["total_sol"] = 9.0
    assert src.on_ws("wss://cluster.axiom.trade/", json.dumps(changed)) == []

    changed["content"]["signature"] = "axiomwallettx3"
    third = src.on_ws("wss://cluster.axiom.trade/", json.dumps(changed))
    assert len(third) == 1 and third[0].tx == "axiomwallettx3" and third[0].sol == pytest.approx(9.0)


def test_unknown_room_is_sampled(settings):
    src = _source(settings)
    payload, _url = load("garbage.json")
    assert src.on_ws("wss://cluster.axiom.trade/", payload) == []
    path = settings.data_dir / "browser" / "axiom" / "samples.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "ws" and "not_a_known_room" in rec["payload"]


def test_known_rooms_are_not_sampled(settings):
    src = _source(settings)
    for name in ("sol_price.json", "noise_btc.json", "new_pairs.json"):
        payload, _url = load(name)
        src.on_ws("wss://cluster.axiom.trade/", payload)
    assert not (settings.data_dir / "browser" / "axiom" / "samples.jsonl").exists()


def test_garbage_never_raises():
    events, recognized, sol = ws("garbage.json", sol_usd=SOL)
    assert events == [] and recognized is False and sol == SOL

    assert parse_ws_message("this is not json {", sol_usd=SOL) == ([], False, SOL)
    assert parse_ws_message(b"\xff\xfe", sol_usd=3.0) == ([], False, 3.0)
    assert parse_ws_message(None) == ([], False, None)
    assert parse_http("https://api.axiom.trade/nope", {"nope": 1})[0] == []
    assert parse_http("", None) == ([], False, None)


def test_features_after_apply():
    for name in _AXIOM_FEATURES:
        assert FEATURES[name].group == "axiom"

    events, _, _ = ws("new_pairs.json", sol_usd=SOL)
    market = MarketState()
    for ev in events:
        market.apply(ev)
    st = market.get(MINT)
    assert feat(st, "axiom_listed") is True
    assert feat(st, "axiom_final_stretch") is False
    assert feat(st, "axiom_top10_pct") == pytest.approx(22.5)
    assert feat(st, "axiom_dev_hold_pct") == pytest.approx(2.7)
    assert feat(st, "axiom_sniper_pct") == pytest.approx(16.64)
    assert feat(st, "axiom_insider_pct") == pytest.approx(3.0)
    assert feat(st, "axiom_bundle_pct") == pytest.approx(4.0)
    assert feat(st, "axiom_holders") == pytest.approx(42)
    assert feat(st, "axiom_pro_traders") == pytest.approx(6)

    stretch, _, _ = ws("pulse_final_stretch.json", sol_usd=SOL)
    stretched = MarketState()
    stretched.apply(stretch[0])
    st2 = stretched.get(MINT)
    assert feat(st2, "axiom_listed") is True
    assert feat(st2, "axiom_final_stretch") is True
    assert feat(st2, "axiom_dev_hold_pct") == pytest.approx(2.7)

    unseen = TokenState(mint=MINT2, first_seen_ts=0.0)
    for name in _AXIOM_FEATURES:
        assert feat(unseen, name) is None

"""Our own rug check: parsing the chain's answers, and dev history from recorded launches."""

from solmeme.core.events import TokenLaunched
from solmeme.features.registry import FeatureView
from solmeme.features.token_state import MarketState
from solmeme.sources.chaincheck import SYSTEM_PROGRAM, TOKEN_2022, holder_shares, mint_flags, reasons


def test_mint_flags_find_authorities_and_token2022_traps():
    clean = {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "data": {"parsed": {"info": {
        "mintAuthority": None, "freezeAuthority": None, "supply": "1000000000000000", "decimals": 6}}}}
    f = mint_flags(clean)
    assert f == {"mint_authority": False, "freeze_authority": False, "token_2022": False, "traps": [], "supply": 1e9}
    trap = {"owner": TOKEN_2022, "data": {"parsed": {"info": {"mintAuthority": "Dev", "freezeAuthority": "Dev",
        "supply": "1000", "decimals": 0, "extensions": [
            {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500}}},
            {"extension": "transferHook", "state": {}},
            {"extension": "defaultAccountState", "state": {"accountState": "initialized"}},   # harmless
            {"extension": "metadataPointer", "state": {}}]}}}}
    f = mint_flags(trap)
    assert f["freeze_authority"] and f["mint_authority"] and f["token_2022"]
    assert f["traps"] == ["transfer fee", "transfer hook"]
    assert reasons(f) == ["freeze authority", "mint authority", "token-2022 transfer fee", "token-2022 transfer hook"]


def test_holder_shares_leave_out_program_accounts():
    largest = [{"address": "curveAta", "uiAmount": 700e6}, {"address": "devAta", "uiAmount": 120e6},
               {"address": "a3", "uiAmount": 30e6}, {"address": "a4", "uiAmount": 20e6}]
    owners = {"curveAta": "CurvePda", "devAta": "DevWallet", "a3": "W3", "a4": "W4"}
    progs = {"CurvePda": "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P", "DevWallet": SYSTEM_PROGRAM, "W3": SYSTEM_PROGRAM,
             "W4": None}                                   # a wallet with no SOL left still counts as a person
    h = holder_shares(largest, owners, progs, 1e9)
    assert h == {"top1_pct": 12.0, "top10_pct": 17.0, "holders_seen": 3}
    assert reasons({**h, "top1_pct": 60.0}) == ["top1 60%"]


def test_dev_history_counts_a_creators_earlier_rugs():
    m = MarketState()
    for i, mint in enumerate(("old1pump", "old2pump", "newpump")):
        m.apply(TokenLaunched(ts=100.0 * i, source="pumpportal", mint=mint, creator="Dev", v_sol=30.0, v_tokens=1.07e9))
    m.tokens["old1pump"].set_price(10, 1e-7)
    m.tokens["old1pump"].set_price(20, 5e-9)                # down 95%: rugged
    m.tokens["old2pump"].set_price(110, 1e-7)
    m.tokens["old2pump"].set_price(120, 6e-8)               # down 40%: not a rug
    fv = FeatureView(m.tokens["newpump"], 300.0, m)
    assert fv.get("dev_prior_launches") == 2 and fv.get("dev_prior_rugs") == 1
    assert FeatureView(m.tokens["old1pump"], 300.0, m).get("dev_prior_launches") == 0


def test_creator_history_from_pump_fun_curves_and_the_coin_factory_rule():
    import asyncio, base64
    from solmeme.sources import chaincheck as cc

    class R:
        def __init__(self, body): self.body, self.status_code = body, 200
        def raise_for_status(self): pass
        def json(self): return self.body

    class Http:
        async def post(self, url, json, timeout):
            if json["method"] == "getProgramAccounts":
                assert json["params"][1]["filters"][0]["memcmp"] == {"offset": 49, "bytes": "Dev1"}
                curves = [{"account": {"data": [base64.b64encode(b"\x01" if i < 2 else b"\x00").decode(), "base64"]}} for i in range(21)]
                return R({"result": curves})
            return R({"result": [{"blockTime": 1}] * 1000})          # an old, busy wallet
    cc._DEV_CACHE.clear()
    h = asyncio.run(cc.creator_history(Http(), "Dev1"))
    assert h == {"launches": 21, "graduated": 2, "wallet_txs": 1000, "wallet_age_h": None}
    assert "21 coins" in cc.serial_launcher(h)                      # Zoroark's creator: a coin factory
    assert cc.serial_launcher({"launches": 1, "graduated": 0}) is None
    assert cc.serial_launcher({"launches": 12, "graduated": 4}) is None   # a third graduated: not a factory

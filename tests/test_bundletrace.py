"""Our own bundle check (sources/bundletrace.py): holders funded by one wallet are one person."""

import asyncio
from types import SimpleNamespace as NS

from solmeme.sources import bundletrace as bt


def test_the_funder_is_who_sent_the_first_sol_or_paid_the_fee():
    tx = {"transaction": {"message": {"accountKeys": [{"pubkey": "Payer", "signer": True}],
                                      "instructions": [{"program": "system", "parsed": {"type": "transfer",
                                                        "info": {"source": "Boss", "destination": "W1"}}}]}}}
    assert bt.funder_of(tx, "W1") == "Boss"
    no_transfer = {"transaction": {"message": {"accountKeys": [{"pubkey": "Boss", "signer": True}], "instructions": []}}}
    assert bt.funder_of(no_transfer, "W2") == "Boss"            # the fee payer runs it
    assert bt.funder_of(no_transfer, "Boss") is None


def test_holders_sharing_a_funder_are_one_cluster_and_exchanges_link_nobody():
    holders = {"A": 4.0, "B": 3.0, "C": 2.0, "D": 1.0, "E": 1.0}
    funders = {"A": "Boss", "B": "Boss", "C": "Boss", "D": "Binance", "E": "Binance"}
    cl = bt.clusters(holders, funders, exchanges={"Binance"})
    assert cl == [{"wallets": 3, "pct": 9.0, "funder": "Boss"}]
    assert "3 of its top holders" not in (bt.bundled(dict(linked_wallets=3, linked_pct=9.0)) or "")   # under the line
    assert "a bundle" in bt.bundled(dict(linked_wallets=5, linked_pct=6.0))
    assert "a bundle" in bt.bundled(dict(linked_wallets=2, linked_pct=12.0))


def test_a_trace_counts_linked_and_fresh_wallets(settings):
    now = 10_000_000.0

    class R:
        def __init__(self, body): self.body = body
        def raise_for_status(self): pass
        def json(self): return self.body

    funding = {"W1": ("Boss", now - 3600), "W2": ("Boss", now - 7200), "W3": ("Old", now - 30 * 86400)}

    class Http:
        async def post(self, url, json, timeout):
            m, p = json["method"], json["params"]
            if m == "getSignaturesForAddress":
                f = funding.get(p[0])
                return R({"result": [{"signature": "s-" + p[0], "blockTime": f[1]}] if f else []})
            if m == "getTransaction":
                w = p[0][2:]
                return R({"result": {"transaction": {"message": {"accountKeys": [], "instructions": [
                    {"program": "system", "parsed": {"type": "transfer", "info": {"source": funding[w][0], "destination": w}}}]}}}})
            if m == "getBalance":
                return R({"result": {"value": 2 * 10**9}})          # 2 SOL: not an exchange
            raise AssertionError(m)

    settings.sources.bundletrace.max_requests_per_s = 1000.0
    t = bt.BundleTrace(NS(settings=settings, http=Http(), watch=None))
    d = asyncio.run(t.trace({"W1": 5.0, "W2": 4.0, "W3": 1.0}, now))
    assert (d["checked"], d["linked_wallets"], d["linked_pct"], d["fresh_wallets"], d["fresh_pct"]) == (3, 2, 9.0, 2, 9.0)

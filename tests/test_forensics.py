"""Wallet trace: transfers, patterns, and what the other side is."""

import asyncio

from solmeme.lab.forensics import (
    DUST_SOL,
    LAMPORTS,
    identity_from_stats,
    investigate,
    parse_helius_tx,
    parse_history,
    parse_rpc_tx,
    summarize,
)

ROOT = "RootWallet"
FUNDER = "FunderAaa"
EXIT = "ExitWallet"
MINT = "MintToken111"


def _sol(src, dst, sol, ts, sig="s", source="SYSTEM_PROGRAM"):
    return {
        "signature": sig,
        "timestamp": ts,
        "type": "TRANSFER",
        "source": source,
        "nativeTransfers": [{
            "fromUserAccount": src,
            "toUserAccount": dst,
            "amount": int(sol * LAMPORTS),
        }],
    }


def test_helius_groups_sol_and_token_and_skips_swaps_and_dust():
    swap = {"signature": "swap", "timestamp": 1, "type": "SWAP", "source": "JUPITER",
            "nativeTransfers": [{"fromUserAccount": ROOT, "toUserAccount": "Pool", "amount": int(2 * LAMPORTS)}]}
    dust = _sol(FUNDER, ROOT, DUST_SOL / 2, 2, sig="dust")
    token = {
        "signature": "tok", "timestamp": 3, "type": "TRANSFER", "source": "SOLANA_PROGRAM_LIBRARY",
        "tokenTransfers": [{
            "fromUserAccount": ROOT, "toUserAccount": EXIT,
            "tokenAmount": 1_500_000, "mint": MINT,
        }],
    }
    funded = _sol(FUNDER, ROOT, 10, 4, sig="in", source="COINBASE")
    edges, trades = parse_history("helius", [swap, dust, token, funded], ROOT)
    assert len(trades) == 1 and trades[0]["venue"] == "JUPITER"
    summary = summarize(ROOT, edges)
    by = {row["wallet"]: row for row in summary["counterparties"]}
    assert set(by) == {FUNDER, EXIT}
    assert by[FUNDER]["sol_in"] == 10 and by[FUNDER]["chain_label"] == "Coinbase"
    assert by[EXIT]["token_out"] == 1
    assert summary["sol_in"] == 10 and summary["sol_out"] == 0
    assert any("earliest SOL" in note for note in summary["patterns"])


def test_passthrough_and_fanout():
    rows = [
        _sol(FUNDER, ROOT, 5, 1_000, sig="in"),
        _sol(ROOT, EXIT, 4.8, 1_050, sig="out"),
    ]
    for i, dest in enumerate(("A", "B", "C", "D")):
        rows.append(_sol(ROOT, dest, 1.0, 2_000 + i * 10, sig=f"f{i}"))
    edges, _trades = parse_history("helius", rows, ROOT)
    notes = summarize(ROOT, edges)["patterns"]
    assert any("sent 4.80 SOL to ExitWallet" in note for note in notes)
    assert any(note.startswith("paid 4 wallets about 1.00 SOL") for note in notes)


def test_rpc_system_transfer_and_dex_swap():
    sol = {
        "signature": "sig1",
        "blockTime": 50,
        "meta": {"err": None},
        "transaction": {
            "signatures": ["sig1"],
            "message": {"accountKeys": [
                {"pubkey": FUNDER, "signer": True},
                {"pubkey": ROOT, "signer": False},
            ], "instructions": [{
                "program": "system",
                "programId": "11111111111111111111111111111111",
                "parsed": {"type": "transfer", "info": {
                    "source": FUNDER, "destination": ROOT, "lamports": int(1.5 * LAMPORTS),
                }},
            }]},
        },
    }
    edges, trades = parse_rpc_tx(sol, ROOT)
    assert trades == [] and edges[0]["sol"] == 1.5 and edges[0]["source"] == FUNDER
    swap = {
        "signature": "sig2",
        "blockTime": 60,
        "meta": {"err": None, "innerInstructions": []},
        "transaction": {
            "signatures": ["sig2"],
            "message": {"accountKeys": [{"pubkey": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"}],
                        "instructions": [{"programId": "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4", "parsed": None}]},
        },
    }
    edges, trades = parse_rpc_tx(swap, ROOT)
    assert edges == [] and trades and trades[0]["venue"] == "Jupiter"


def test_identity_reads_name_tags_and_launches():
    ident = identity_from_stats({
        "winrate": 0.5, "pnl": 1.2, "buy_count": 9,
        "common": {"name": "Otter", "twitter_username": "otter", "wallet_tags": [], "tags": ["kol"],
                   "created_token_count": 4, "fund_from": "Coinbase Hot Wallet",
                   "fund_from_address": "AxiomRXZ", "fund_amount": "0.02", "twitter_fans_num": 100},
    }, FUNDER)
    assert ident["name"] == "Otter" and ident["twitter"] == "otter"
    assert ident["created"] == 4 and ident["tags"] == ["kol"]
    assert ident["fund_label"] == "Coinbase Hot Wallet" and ident["fund_address"] == "AxiomRXZ"
    assert ident["fans"] == 100


def test_failed_tx_records_the_signer_not_a_sol_transfer():
    tx = {
        "signature": "fail",
        "blockTime": 10,
        "meta": {"err": {"InstructionError": [0, {"Custom": 7}]}},
        "transaction": {"signatures": ["fail"], "message": {"accountKeys": [
            {"pubkey": "SignerAaa", "signer": True},
            {"pubkey": ROOT, "signer": False, "writable": False},
        ], "instructions": []}},
    }
    edges, trades = parse_rpc_tx(tx, ROOT)
    assert trades == [] and edges[0]["kind"] == "mention" and edges[0]["source"] == "SignerAaa"
    notes = summarize(ROOT, edges)["patterns"]
    assert any("without moving its SOL" in note for note in notes)


def test_investigate_names_the_funder_and_follows_one_hop():
    helius_root = [_sol(FUNDER, ROOT, 8, 100, sig="a"), _sol(ROOT, EXIT, 3, 200, sig="b")]
    helius_funder = [_sol("Exchange1", FUNDER, 20, 50, sig="c")]
    helius_exit = [_sol(EXIT, "NextHop", 2.5, 300, sig="d")]

    async def fetch(address, limit):
        if address == ROOT:
            return "helius", helius_root
        if address == FUNDER:
            return "helius", helius_funder
        if address == EXIT:
            return "helius", helius_exit
        return "helius", []

    async def identify(address):
        if address == FUNDER:
            return {"name": "Otter", "twitter": "otter", "tags": ["kol"], "created": 0,
                    "win": 0.6, "pnl": 1.4, "buys": 11, "label": ""}
        if address == "Exchange1":
            return {"name": "Binance", "twitter": "", "tags": ["exchange"], "created": 0,
                    "win": None, "pnl": None, "buys": 0, "label": ""}
        return {}

    report = asyncio.run(investigate(fetch, identify, ROOT, hops=2, follow=5, identify_limit=8))
    text = report["text"]
    assert "Otter" in text and "@otter" in text
    assert "funded by Exchange1" in text
    assert "Binance" in text
    assert "one hop further" in text
    assert report["summary"]["sol_in"] == 8 and report["summary"]["sol_out"] == 3


def test_parse_helius_ignores_failed_and_self():
    failed = {"transactionError": {"error": "x"}, "nativeTransfers": [
        {"fromUserAccount": FUNDER, "toUserAccount": ROOT, "amount": int(LAMPORTS)}]}
    self_send = _sol(ROOT, ROOT, 1, 1, sig="self")
    assert parse_helius_tx(failed, ROOT) == ([], [])
    edges, _trades = parse_helius_tx(self_send, ROOT)
    assert edges == []

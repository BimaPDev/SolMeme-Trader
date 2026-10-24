"""Trace a wallet's SOL and token transfers and say what the other side is.

The graph is built from public chain history (Helius when HELIUS_API_KEY is
set, otherwise a public RPC). Names, X accounts, and tags come from GMGN
wallet_stats when that key is set. Nothing here sends a transaction.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from solmeme.config import secret
from solmeme.sources.gmgn import _f

LAMPORTS = 1_000_000_000
DUST_SOL = 0.005
HELIUS_HISTORY = "https://api.helius.xyz/v0/addresses/{address}/transactions"
RPC_URLS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
)
WSOL = "So11111111111111111111111111111111111111112"

# Programs we can name without an API. A transfer to one of these is a venue, not a person.
PROGRAMS = {
    "11111111111111111111111111111111": "System program",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated token program",
    "ComputeBudget111111111111111111111111111111": "Compute budget",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "PumpSwap",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo program",
}
DEX_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
}
SWAP_SOURCES = {
    "JUPITER", "RAYDIUM", "ORCA", "PUMP_FUN", "PUMP_AMM", "METEORA", "LIFINITY",
    "PHOENIX", "WHIRLPOOL", "METEORA_DLMM", "RAYDIUM_CLMM",
}
EXCHANGE_SOURCES = {
    "COINBASE": "Coinbase",
    "BINANCE": "Binance",
    "BYBIT": "Bybit",
    "OKX": "OKX",
    "KRAKEN": "Kraken",
}
SYSTEM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


def short_addr(addr: str) -> str:
    if len(addr) <= 12:
        return addr
    return f"{addr[:4]}…{addr[-4:]}"


def _when(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _edge(ts, signature, kind, source, dest, sol=0.0, mint="", amount=0.0, venue=""):
    return {
        "ts": float(ts or 0),
        "signature": signature or "",
        "kind": kind,
        "source": source or "",
        "dest": dest or "",
        "sol": float(sol or 0),
        "mint": mint or "",
        "amount": float(amount or 0),
        "venue": venue or "",
    }


def _keep_party(addr: str) -> bool:
    return bool(addr) and addr not in PROGRAMS and addr != WSOL


def parse_helius_tx(tx: dict, root: str) -> tuple[list[dict], list[dict]]:
    if not isinstance(tx, dict) or tx.get("transactionError"):
        return [], []
    kind = str(tx.get("type") or "").upper()
    source = str(tx.get("source") or "").upper()
    ts = float(tx.get("timestamp") or 0)
    sig = tx.get("signature") or ""
    if kind == "SWAP" or source in SWAP_SOURCES:
        return [], [{"ts": ts, "signature": sig, "venue": source or kind or "SWAP"}]
    venue = EXCHANGE_SOURCES.get(source, "")
    edges = []
    for row in tx.get("nativeTransfers") or []:
        if not isinstance(row, dict):
            continue
        sol = float(row.get("amount") or 0) / LAMPORTS
        src, dst = row.get("fromUserAccount") or "", row.get("toUserAccount") or ""
        if sol < DUST_SOL or root not in (src, dst) or src == dst:
            continue
        if not _keep_party(src) or not _keep_party(dst):
            continue
        edges.append(_edge(ts, sig, "sol", src, dst, sol=sol, venue=venue))
    for row in tx.get("tokenTransfers") or []:
        if not isinstance(row, dict):
            continue
        amount = _f(row.get("tokenAmount")) or 0
        src, dst = row.get("fromUserAccount") or "", row.get("toUserAccount") or ""
        mint = row.get("mint") or ""
        if amount <= 0 or root not in (src, dst) or src == dst or mint == WSOL:
            continue
        if not _keep_party(src) or not _keep_party(dst):
            continue
        edges.append(_edge(ts, sig, "token", src, dst, mint=mint, amount=amount, venue=venue))
    return edges, []


def _account_keys(tx: dict) -> list[str]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    keys = []
    for key in msg.get("accountKeys") or []:
        if isinstance(key, str):
            keys.append(key)
        elif isinstance(key, dict):
            keys.append(key.get("pubkey") or "")
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(loaded.get("writable") or [])
    keys.extend(loaded.get("readonly") or [])
    return keys


def _instructions(tx: dict) -> list[dict]:
    msg = (tx.get("transaction") or {}).get("message") or {}
    found = [ix for ix in (msg.get("instructions") or []) if isinstance(ix, dict)]
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        if isinstance(group, dict):
            found.extend(ix for ix in (group.get("instructions") or []) if isinstance(ix, dict))
    return found


def _program_id(ix: dict, keys: list[str]) -> str:
    if ix.get("programId"):
        return ix["programId"]
    idx = ix.get("programIdIndex")
    if isinstance(idx, int) and 0 <= idx < len(keys):
        return keys[idx]
    return ""


def _token_owners(tx: dict, keys: list[str]) -> dict[str, str]:
    owners = {}
    meta = tx.get("meta") or {}
    for bal in (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or []):
        if not isinstance(bal, dict):
            continue
        idx = bal.get("accountIndex")
        owner = bal.get("owner") or ""
        if isinstance(idx, int) and owner and 0 <= idx < len(keys):
            owners[keys[idx]] = owner
    return owners


def _token_ui(info: dict) -> float:
    raw = info.get("tokenAmount")
    if isinstance(raw, dict):
        return _f(raw.get("uiAmount")) or 0
    return _f(raw) or 0


def _fee_payer(tx: dict, keys: list[str]) -> str:
    for key in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []:
        if isinstance(key, dict) and key.get("signer") and key.get("pubkey"):
            return key["pubkey"]
    return keys[0] if keys else ""


def _mention(tx: dict, root: str, keys: list[str], ts: float, sig: str) -> list[dict]:
    """The wallet is listed on the transaction, and the transaction does not move its funds."""
    if root not in keys:
        return []
    payer = _fee_payer(tx, keys)
    if not payer or payer == root or not _keep_party(payer):
        return []
    return [_edge(ts, sig, "mention", payer, root)]


def parse_rpc_tx(tx: dict, root: str) -> tuple[list[dict], list[dict]]:
    if not isinstance(tx, dict):
        return [], []
    meta = tx.get("meta") or {}
    failed = bool(meta.get("err"))
    keys = _account_keys(tx)
    ixs = _instructions(tx)
    programs = {_program_id(ix, keys) for ix in ixs}
    sigs = (tx.get("transaction") or {}).get("signatures") or []
    sig = tx.get("signature") or (sigs[0] if sigs else "")
    ts = float(tx.get("blockTime") or 0)
    if failed:
        return _mention(tx, root, keys, ts, sig), []
    if programs & DEX_PROGRAMS:
        venue = PROGRAMS.get(next(iter(programs & DEX_PROGRAMS)), "DEX")
        return _mention(tx, root, keys, ts, sig), [{"ts": ts, "signature": sig, "venue": venue}]
    owners = _token_owners(tx, keys)
    edges = []
    for ix in ixs:
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") if isinstance(parsed.get("info"), dict) else {}
        program = _program_id(ix, keys)
        ptype = parsed.get("type") or ""
        named = ix.get("program") or ""
        if ptype in ("transfer", "transferWithSeed") and (program == SYSTEM or named == "system"):
            sol = float(info.get("lamports") or 0) / LAMPORTS
            src, dst = info.get("source") or "", info.get("destination") or ""
            if sol < DUST_SOL or root not in (src, dst) or src == dst:
                continue
            if not _keep_party(src) or not _keep_party(dst):
                continue
            edges.append(_edge(ts, sig, "sol", src, dst, sol=sol))
        elif ptype in ("transfer", "transferChecked") and (program in TOKEN_PROGRAMS or named in ("spl-token", "spl-token-2022")):
            amount = _token_ui(info)
            mint = info.get("mint") or ""
            src_acct, dst_acct = info.get("source") or "", info.get("destination") or ""
            src = owners.get(src_acct) or (info.get("authority") if isinstance(info.get("authority"), str) else "")
            dst = owners.get(dst_acct) or ""
            if amount <= 0 or root not in (src, dst) or src == dst or mint == WSOL:
                continue
            if not _keep_party(src) or not _keep_party(dst):
                continue
            edges.append(_edge(ts, sig, "token", src, dst, mint=mint, amount=amount))
    if not edges:
        edges = _mention(tx, root, keys, ts, sig)
    return edges, []


def parse_history(source: str, rows, root: str) -> tuple[list[dict], list[dict]]:
    edges, trades = [], []
    parser = parse_helius_tx if source == "helius" else parse_rpc_tx
    for tx in rows or []:
        found, swaps = parser(tx, root)
        edges.extend(found)
        trades.extend(swaps)
    seen = set()
    unique = []
    for edge in edges:
        key = (
            edge["signature"], edge["kind"], edge["source"], edge["dest"],
            round(edge["sol"], 6), edge["mint"], round(edge["amount"], 4),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(edge)
    return unique, trades


def _flow(sol_in: float, sol_out: float, token_in: int, token_out: int, mentions: int = 0) -> str:
    parts = []
    if sol_in > 0 and sol_out == 0:
        parts.append("funded this wallet")
    elif sol_out > 0 and sol_in == 0:
        parts.append("this wallet paid them")
    elif sol_in > 0 and sol_out > 0:
        parts.append("SOL both ways")
    if token_in and not token_out:
        parts.append("sent tokens here")
    elif token_out and not token_in:
        parts.append("received tokens from this wallet")
    elif token_in and token_out:
        parts.append("tokens both ways")
    if mentions and not parts:
        parts.append("listed this wallet on their transactions, and its SOL did not move")
    return ", ".join(parts)


def summarize(root: str, edges: list[dict]) -> dict:
    slots: dict[str, dict] = {}
    for edge in edges:
        if edge["source"] == root:
            other, inbound = edge["dest"], False
        elif edge["dest"] == root:
            other, inbound = edge["source"], True
        else:
            continue
        if not other or other == root:
            continue
        slot = slots.setdefault(other, {
            "wallet": other, "sol_in": 0.0, "sol_out": 0.0,
            "token_in": 0, "token_out": 0, "mentions": 0, "n": 0, "sigs": set(),
            "first": None, "last": None, "chain_label": "", "tokens": [],
        })
        if edge["kind"] == "mention":
            slot["mentions"] += 1
        elif edge["kind"] == "sol":
            if inbound:
                slot["sol_in"] += edge["sol"]
            else:
                slot["sol_out"] += edge["sol"]
        else:
            if inbound:
                slot["token_in"] += 1
            else:
                slot["token_out"] += 1
            if len(slot["tokens"]) < 8:
                slot["tokens"].append({
                    "dir": "in" if inbound else "out",
                    "mint": edge["mint"],
                    "amount": edge["amount"],
                })
        slot["sigs"].add(edge["signature"])
        ts = edge["ts"]
        if ts:
            slot["first"] = ts if slot["first"] is None else min(slot["first"], ts)
            slot["last"] = ts if slot["last"] is None else max(slot["last"], ts)
        if edge.get("venue") and not slot["chain_label"]:
            slot["chain_label"] = edge["venue"]
    rows = []
    for slot in slots.values():
        slot["n"] = len(slot.pop("sigs"))
        rows.append(slot)
    rows.sort(key=lambda r: (r["sol_in"] + r["sol_out"], r["token_in"] + r["token_out"], r["mentions"]), reverse=True)
    return {
        "sol_in": sum(r["sol_in"] for r in rows),
        "sol_out": sum(r["sol_out"] for r in rows),
        "counterparties": rows,
        "patterns": _patterns(root, edges),
    }


def _patterns(root: str, edges: list[dict]) -> list[str]:
    notes = []
    mentions = [e for e in edges if e["kind"] == "mention"]
    sol_edges = [e for e in edges if e["kind"] == "sol"]
    if mentions and not sol_edges:
        payers = {e["source"] for e in mentions}
        notes.append(
            f"{len(mentions)} transactions list this wallet without moving its SOL, "
            f"signed by {len(payers)} other wallet" + ("s" if len(payers) != 1 else "")
        )
    sol_in = sorted((e for e in sol_edges if e["dest"] == root), key=lambda e: e["ts"])
    if sol_in:
        first = sol_in[0]
        notes.append(f"earliest SOL in was {first['sol']:.2f} from {short_addr(first['source'])}")
    notes.extend(_passthroughs(root, edges))
    notes.extend(_fanouts(root, edges))
    return notes


def _passthroughs(root: str, edges: list[dict]) -> list[str]:
    inbound = [e for e in edges if e["kind"] == "sol" and e["dest"] == root]
    outbound = [e for e in edges if e["kind"] == "sol" and e["source"] == root]
    notes = []
    used = set()
    for inn in sorted(inbound, key=lambda e: e["ts"]):
        best = None
        for i, out in enumerate(outbound):
            if i in used or inn["sol"] <= 0:
                continue
            dt = out["ts"] - inn["ts"]
            if dt < 0 or dt > 600:
                continue
            if abs(out["sol"] - inn["sol"]) / inn["sol"] > 0.2:
                continue
            if best is None or dt < best[0]:
                best = (dt, i, out)
        if best:
            used.add(best[1])
            out = best[2]
            notes.append(
                f"received {inn['sol']:.2f} SOL from {short_addr(inn['source'])} and sent "
                f"{out['sol']:.2f} SOL to {short_addr(out['dest'])} {best[0]:.0f}s later"
            )
    return notes


def _fanouts(root: str, edges: list[dict]) -> list[str]:
    outs = sorted(
        (e for e in edges if e["kind"] == "sol" and e["source"] == root and e["sol"] >= 0.05),
        key=lambda e: e["ts"],
    )
    notes = []
    i = 0
    while i < len(outs):
        group = [i]
        for j in range(i + 1, len(outs)):
            if outs[j]["ts"] - outs[i]["ts"] > 180:
                break
            amounts = sorted(outs[k]["sol"] for k in group)
            med = amounts[len(amounts) // 2]
            if med > 0 and abs(outs[j]["sol"] - med) / med <= 0.25:
                group.append(j)
        dests = {outs[k]["dest"] for k in group}
        if len(dests) >= 4:
            avg = sum(outs[k]["sol"] for k in group) / len(group)
            span = outs[group[-1]]["ts"] - outs[group[0]]["ts"]
            notes.append(f"paid {len(dests)} wallets about {avg:.2f} SOL within {span:.0f}s")
            i = group[-1] + 1
        else:
            i += 1
    return notes


def what_is(wallet: str, identity: dict | None, flow: str) -> str:
    identity = identity or {}
    bits = []
    label = identity.get("label") or ""
    if label:
        bits.append(str(label))
    name = identity.get("name") or ""
    if name and name not in (wallet, label):
        bits.append(str(name))
    twitter = str(identity.get("twitter") or "").lstrip("@")
    if twitter:
        bits.append(f"@{twitter}")
    tags = [str(t) for t in (identity.get("tags") or []) if t]
    if tags:
        bits.append("tags " + ",".join(tags[:8]))
    created = int(identity.get("created") or 0)
    if created >= 1:
        bits.append(f"launched {created} token" + ("s" if created != 1 else ""))
    if identity.get("fans"):
        bits.append(f"{int(identity['fans']):,} on X")
    win, pnl, buys = identity.get("win"), identity.get("pnl"), identity.get("buys")
    stats = []
    if win is not None:
        stats.append(f"win {win * 100:.0f}%")
    if pnl is not None:
        stats.append(f"pnl {pnl:.2f}x")
    if identity.get("realized_usd"):
        stats.append(f"realized ${identity['realized_usd']:,.0f}")
    if buys:
        stats.append(f"{buys:.0f} buys/7d")
    if stats:
        bits.append("7d " + " ".join(stats))
    if not bits:
        bits.append("no public name")
    if flow:
        bits.append(flow)
    return " · ".join(bits)


def _amt(n: float) -> str:
    if n >= 1000:
        return f"{n:,.0f}"
    text = f"{n:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _token_blurb(row: dict) -> str:
    if not row.get("tokens"):
        return ""
    bits = []
    for move in row["tokens"][:3]:
        way = "in" if move["dir"] == "in" else "out"
        bits.append(f"{way} {_amt(move['amount'])} {short_addr(move['mint'])}")
    extra = len(row["tokens"]) - len(bits)
    text = "tokens " + ", ".join(bits)
    if extra > 0:
        text += f" +{extra} more"
    return text


def _row_line(row: dict, identity: dict | None) -> str:
    flow = _flow(row["sol_in"], row["sol_out"], row["token_in"], row["token_out"], row.get("mentions") or 0)
    amounts = []
    if row["sol_in"]:
        amounts.append(f"in {row['sol_in']:.2f} SOL")
    if row["sol_out"]:
        amounts.append(f"out {row['sol_out']:.2f} SOL")
    if row.get("mentions") and not amounts and not row["token_in"] and not row["token_out"]:
        amounts.append(f"mentioned in {row['mentions']} tx")
    if not amounts:
        amounts.append("tokens only")
    head = f"{'  '.join(amounts)}  {row['n']} tx  {short_addr(row['wallet'])}"
    lines = [head, "    " + what_is(row["wallet"], identity, flow)]
    tokens = _token_blurb(row)
    if tokens:
        lines.append("    " + tokens)
    return "\n".join(lines)


def hop_notes(seed: str, hops: dict[str, dict], identities: dict[str, dict]) -> list[str]:
    lines = []
    funder_of: dict[str, list[str]] = {}
    for wallet, summary in hops.items():
        inbound = [c for c in summary["counterparties"] if c["sol_in"] > 0 and c["wallet"] != seed]
        outbound = [c for c in summary["counterparties"] if c["sol_out"] > 0 and c["wallet"] != seed]
        parts = []
        if inbound:
            top = max(inbound, key=lambda c: c["sol_in"])
            funder_of.setdefault(top["wallet"], []).append(wallet)
            parts.append(
                f"funded by {short_addr(top['wallet'])} "
                f"({what_is(top['wallet'], identities.get(top['wallet']), '')}, {top['sol_in']:.2f} SOL)"
            )
        if outbound:
            top = max(outbound, key=lambda c: c["sol_out"])
            parts.append(
                f"sent {top['sol_out']:.2f} SOL on to {short_addr(top['wallet'])} "
                f"({what_is(top['wallet'], identities.get(top['wallet']), '')})"
            )
        if parts:
            lines.append(f"{short_addr(wallet)}: " + "; ".join(parts))
        elif summary["counterparties"]:
            lines.append(f"{short_addr(wallet)}: token links only in this window")
        else:
            lines.append(f"{short_addr(wallet)}: no transfers in the transactions that were read")
    for funder, group in funder_of.items():
        if len(group) >= 2:
            names = ", ".join(short_addr(w) for w in group)
            lines.append(
                f"{len(group)} connected wallets ({names}) were funded by the same address {short_addr(funder)}"
            )
    return lines


def render(wallet: str, identity: dict | None, summary: dict, trades: list[dict], *,
           source: str, tx_count: int, truncated: bool, hop_lines: list[str] | None = None,
           identities: dict[str, dict] | None = None) -> str:
    identities = identities or {}
    ident = identity or {}
    lines = [wallet, what_is(wallet, ident, "")]
    if ident.get("balance_sol") is not None:
        lines.append(f"SOL on hand: {ident['balance_sol']:.2f}")
    if ident.get("fund_address"):
        amount = f"{ident['fund_sol']:.4f} SOL " if ident.get("fund_sol") else ""
        when = _when(ident.get("fund_ts"))
        label = ident.get("fund_label") or "unlabeled"
        lines.append(
            f"first recorded SOL in: {amount}from {label} {ident['fund_address']}"
            + (f" on {when}" if when else "")
        )
    span = [r["first"] for r in summary["counterparties"] if r.get("first")]
    span += [r["last"] for r in summary["counterparties"] if r.get("last")]
    history = f"history: {tx_count} transactions via {source}"
    if span:
        history += f", {_when(min(span))} → {_when(max(span))}"
    if truncated:
        history += " (newest only; older history was not read)"
    lines.append(history)
    lines.append(
        f"SOL in {summary['sol_in']:.2f} from "
        f"{sum(1 for r in summary['counterparties'] if r['sol_in'] > 0)} wallets · "
        f"SOL out {summary['sol_out']:.2f} to "
        f"{sum(1 for r in summary['counterparties'] if r['sol_out'] > 0)} wallets"
    )
    if trades:
        venues = [t["venue"] for t in trades if t.get("venue")]
        top = ", ".join(name for name, _ in Counter(venues).most_common(4))
        lines.append(f"swaps: {len(trades)}" + (f" via {top}" if top else ""))
    if summary["patterns"]:
        lines.append("patterns:")
        lines.extend(f"- {note}" for note in summary["patterns"])
    shown = summary["counterparties"][:25]
    if shown:
        lines.append("connected:")
        for row in shown:
            lines.append(_row_line(row, identities.get(row["wallet"])))
        extra = len(summary["counterparties"]) - len(shown)
        if extra > 0:
            lines.append(f"  +{extra} smaller links")
    elif not trades:
        lines.append("No SOL or token transfers in the transactions that were read.")
    if hop_lines:
        lines.append("one hop further:")
        lines.extend(f"- {line}" for line in hop_lines)
    if source == "rpc":
        lines.append("Public RPC misses token sends that only touch a token account. "
                     "HELIUS_API_KEY reads those too.")
    return "\n".join(lines)


def identity_from_stats(stats: dict, wallet: str) -> dict:
    stats = stats if isinstance(stats, dict) else {}
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    pnl_stat = stats.get("pnl_stat") if isinstance(stats.get("pnl_stat"), dict) else {}
    created = _f(common.get("created_token_count")) or 0
    name = str(common.get("name") or "")
    if name == wallet:
        name = ""
    win = _f(stats.get("winrate"))
    if win is None:
        win = _f(pnl_stat.get("winrate"))
    return {
        "name": name,
        "twitter": common.get("twitter_username") or "",
        "tags": list(common.get("tags") or []),
        "created": int(created),
        "fans": int(_f(common.get("twitter_fans_num") or common.get("followers_count")) or 0),
        "win": win,
        "pnl": _f(stats.get("pnl")),
        "buys": _f(stats.get("buy_count") if stats.get("buy_count") is not None else stats.get("buy")) or 0,
        "realized_usd": _f(stats.get("realized_profit")),
        "balance_sol": _f(stats.get("native_balance")),
        "fund_label": common.get("fund_from") or "",
        "fund_address": common.get("fund_from_address") or "",
        "fund_sol": _f(common.get("fund_amount")),
        "fund_ts": _f(common.get("fund_from_ts")),
        "label": "",
    }


def _blank_identity(label: str = "") -> dict:
    return {"label": label, "name": "", "twitter": "", "tags": [], "created": 0,
            "win": None, "pnl": None, "buys": 0}


async def identify_wallet(req, wallet: str) -> dict:
    if wallet in PROGRAMS:
        return _blank_identity(PROGRAMS[wallet])
    if req is None:
        return {}
    from solmeme.lab.people import _stats
    data = await _stats(req, wallet)
    return identity_from_stats(data if isinstance(data, dict) else {}, wallet)


def _apply_chain_labels(summary: dict, identities: dict[str, dict]) -> None:
    for row in summary["counterparties"]:
        ident = identities.setdefault(row["wallet"], {})
        if row.get("chain_label") and not ident.get("label") and not ident.get("name"):
            ident["label"] = row["chain_label"]


async def investigate(fetch, identify, wallet: str, *, hops: int = 1, tx_limit: int = 100,
                      follow: int = 5, identify_limit: int = 12) -> dict:
    source, rows = await fetch(wallet, tx_limit)
    rows = list(rows or [])
    edges, trades = parse_history(source, rows, wallet)
    summary = summarize(wallet, edges)
    hop_summaries: dict[str, dict] = {}
    if hops >= 2:
        for row in summary["counterparties"]:
            if row["wallet"] in PROGRAMS:
                continue
            if len(hop_summaries) >= follow:
                break
            try:
                src2, raw2 = await fetch(row["wallet"], min(40, tx_limit))
            except Exception as exc:  # noqa: BLE001
                hop_summaries[row["wallet"]] = {"error": str(exc), "counterparties": [], "sol_in": 0, "sol_out": 0, "patterns": []}
                continue
            edges2, _trades2 = parse_history(src2, raw2, row["wallet"])
            hop_summaries[row["wallet"]] = summarize(row["wallet"], edges2)

    queue = [wallet]
    queue.extend(row["wallet"] for row in summary["counterparties"])
    for hop in hop_summaries.values():
        queue.extend(row["wallet"] for row in hop.get("counterparties") or [] if row["sol_in"] or row["sol_out"])
    seen = set()
    identify_ids = []
    for addr in queue:
        if addr in seen:
            continue
        seen.add(addr)
        identify_ids.append(addr)
        if len(identify_ids) >= identify_limit:
            break
    identities: dict[str, dict] = {}
    for addr in identify_ids:
        try:
            identities[addr] = await identify(addr) or {}
        except Exception:  # noqa: BLE001
            identities[addr] = {}
    _apply_chain_labels(summary, identities)
    for hop in hop_summaries.values():
        _apply_chain_labels(hop, identities)
    notes = []
    for addr, hop in hop_summaries.items():
        if hop.get("error"):
            notes.append(f"{short_addr(addr)}: history failed ({hop['error']})")
    notes.extend(hop_notes(wallet, {k: v for k, v in hop_summaries.items() if not v.get("error")}, identities))
    cap = min(tx_limit, 40) if source == "rpc" else tx_limit
    text = render(
        wallet, identities.get(wallet), summary, trades,
        source=source, tx_count=len(rows), truncated=bool(rows) and len(rows) >= cap,
        hop_lines=notes, identities=identities,
    )
    return {
        "wallet": wallet,
        "source": source,
        "text": text,
        "summary": summary,
        "trades": trades,
        "identities": identities,
    }


async def _helius_history(http, address: str, limit: int, key: str) -> list:
    out = []
    before = None
    while len(out) < limit:
        page = min(100, limit - len(out))
        params = {
            "api-key": key,
            "limit": page,
            "token-accounts": "balanceChanged",
            "sort-order": "desc",
        }
        if before:
            params["before-signature"] = before
        resp = await http.get(HELIUS_HISTORY.format(address=address), params=params, timeout=40)
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            break
        out.extend(row for row in rows if isinstance(row, dict))
        sig = rows[-1].get("signature") if isinstance(rows[-1], dict) else None
        if not sig or sig == before or len(rows) < page:
            break
        before = sig
    return out[:limit]


_rpc_url: str | None = None


async def _post_rpc(http, url: str, body, timeout: float = 40):
    import asyncio

    delay = 1.5
    last = None
    for _ in range(6):
        resp = await http.post(url, json=body, timeout=timeout)
        if resp.status_code == 429:
            last = resp
            await asyncio.sleep(delay)
            delay = min(delay * 2, 20)
            continue
        resp.raise_for_status()
        return resp
    code = last.status_code if last is not None else ""
    raise RuntimeError(f"solana rpc rate limited ({code})")


async def _rpc_at(http, url: str, method: str, params: list):
    resp = await _post_rpc(http, url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    payload = resp.json()
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload["error"].get("message") or payload["error"])
    return payload.get("result") if isinstance(payload, dict) else None


async def _pick_rpc(http) -> str:
    global _rpc_url
    if _rpc_url:
        return _rpc_url
    last: Exception | None = None
    for url in RPC_URLS:
        try:
            if await _rpc_at(http, url, "getSlot", []):
                _rpc_url = url
                return url
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise RuntimeError(f"solana rpc unavailable: {last}")


async def _rpc_history(http, address: str, limit: int) -> list:
    import asyncio

    limit = min(limit, 40)
    url = await _pick_rpc(http)
    sigs = await _rpc_at(http, url, "getSignaturesForAddress", [address, {"limit": limit}])
    if not isinstance(sigs, list) or not sigs:
        return []
    signatures = [row.get("signature") for row in sigs if isinstance(row, dict) and row.get("signature")]
    opts = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1, "commitment": "confirmed"}
    by_id: dict = {}
    for start in range(0, len(signatures), 8):
        chunk = signatures[start:start + 8]
        body = [{
            "jsonrpc": "2.0", "id": start + i, "method": "getTransaction", "params": [sig, opts],
        } for i, sig in enumerate(chunk)]
        resp = await _post_rpc(http, url, body, timeout=60)
        payload = resp.json()
        if isinstance(payload, dict):
            payload = [payload]
        for item in payload or []:
            if isinstance(item, dict) and item.get("result"):
                by_id[item.get("id")] = item["result"]
        await asyncio.sleep(0.4)
    txs = []
    for i, sig in enumerate(signatures):
        tx = by_id.get(i)
        if isinstance(tx, dict):
            tx["signature"] = sig
            txs.append(tx)
    return txs


async def load_history(http, address: str, limit: int) -> tuple[str, list]:
    key = secret("HELIUS_API_KEY")
    if key:
        return "helius", await _helius_history(http, address, limit, key)
    return "rpc", await _rpc_history(http, address, limit)

"""Axiom (axiom.trade) Pulse + tracked-wallet adapter.

Axiom has no public market-data API. This listens to the websocket frames and JSON
responses the logged-in web app already receives and turns them into events.

Wire formats below are reconstructed from community clients that log the live
site (not from a capture made by this project). Real frames that don't match are
sampled via ``BrowserSource.sample`` and never raise.

Verified from those clients:

* Cluster sockets ``wss://cluster*.axiom.trade/`` speak JSON text.
  Subscribe (the page sends this itself) is ``{"action":"join","room":...}``.
  Rooms used here: ``new_pairs``, ``update_pulse_v2`` (older name ``update_pulse``),
  ``v:<wallet>`` / ``v-<wallet>`` tracked-wallet trades, ``sol_price``.
* ``new_pairs`` content is an object. Captured field names are snake_case
  (``token_address``, ``dev_holds_percent``, …); some SDK examples use camelCase.
  Both are accepted.
* ``update_pulse_v2`` content is a list of rows, each row a fixed-index array.
  Indexes used here follow christianfm10/axiomfilter ``models.py`` (2025-11) and
  the longer map in christianfm10/ravexutils (2026-07): 1 mint, 7 protocol,
  9–11 socials, 13–17 holder percents, 18 volume SOL, 19 mcap SOL, 21 liquidity
  SOL (index 52 overrides it when the longer row is present), 26 bonding %,
  28 holders, 29 pro traders, 30 migrated-at.
* ``POST /pulse`` (and ``token-info`` / ``pair-info`` / ``new-trending-v2``) repeat
  the same facts as JSON objects or as the positional ``new-trending-v2`` row.
* Tracked-wallet HTTP ``tracked-wallet-transactions-v3`` is a list of positional
  rows (see ``_WALLET_ROW``).
* A second socket ``wss://pulse2.axiom.trade/ws`` carries msgpack, which this
  project does not depend on. Binary frames are sampled, not decoded.

Percents are normalized to 0–100 (a value ≤ 1 is treated as a fraction, same
rule as the GMGN normalizer). SOL notionals become ``*_usd`` only when the
payload already has USD or a ``sol_price`` frame has been seen.
"""

from __future__ import annotations

import json
import re
from typing import Any

from solmeme.core.events import ExternalSignal, WalletTrade
from solmeme.sources.browser import BrowserSource

_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_WALLET_ROOM = re.compile(r"^v[:\-]([1-9A-HJ-NP-Za-km-z]{32,44})$")

# update_pulse_v2 row indexes → (normalized key, kind). kind pct/num/str.
_PULSE_IDX: dict[int, tuple[str, str]] = {
    7: ("protocol", "str"),
    9: ("website", "str"),
    10: ("twitter", "str"),
    11: ("telegram", "str"),
    13: ("top10_pct", "pct"),
    14: ("dev_hold_pct", "pct"),
    15: ("sniper_pct", "pct"),
    16: ("insider_pct", "pct"),
    17: ("bundle_pct", "pct"),
    18: ("volume_sol", "num"),
    19: ("mcap_sol", "num"),
    26: ("bonding_pct", "num"),
    28: ("holders", "num"),
    29: ("pro_traders", "num"),
    30: ("migrated_at", "str"),
}

# new-trending-v2 positional row (AxiomTradeAPI-py TRENDING_V2_FIELDS).
_TREND_IDX: dict[int, tuple[str, str]] = {
    7: ("protocol", "str"),
    10: ("website", "str"),
    11: ("twitter", "str"),
    12: ("telegram", "str"),
    16: ("is_migrated", "bool"),
    19: ("liquidity_sol", "num"),
    20: ("bonding_pct", "num"),
    23: ("volume_sol", "num"),
    24: ("mcap_usd", "num"),
    28: ("liquidity_usd", "num"),
    35: ("top10_pct", "pct"),
    37: ("insider_pct", "pct"),
    38: ("bundle_pct", "pct"),
    39: ("dev_hold_pct", "pct"),
    43: ("holders", "num"),
}

# Object keys seen on /pulse, token-info, pair-info, and SDK examples.
_OBJ_KEYS: dict[str, tuple[str, ...]] = {
    "mint": ("tokenAddress", "token_address", "mint", "tokenMint"),
    "protocol": ("protocol", "displayProtocol", "exchangeName"),
    "website": ("website",),
    "twitter": ("twitter",),
    "telegram": ("telegram",),
    "top10_pct": ("top10HoldersPercent", "top_10_holders_percent", "top10_holders_percent", "top_10_holders", "top10Holders"),
    "dev_hold_pct": ("devHoldsPercent", "dev_holds_percent", "developerHoldingPercent", "devHoldingPercent"),
    "sniper_pct": ("snipersHoldPercent", "snipers_hold_percent"),
    "insider_pct": ("insidersHoldPercent", "insiders_hold_percent", "insiderPercentage"),
    "bundle_pct": ("bundlersHoldPercent", "bundlers_hold_percent", "bundlePercentage"),
    "holders": ("numHolders", "holderCount", "num_holders", "holders"),
    "pro_traders": ("numTradingBotUsers", "numBotUsers", "pro_traders", "proTraders", "num_trading_bot_users"),
    "mcap_usd": ("marketCapUsd", "usd_market_cap"),
    "mcap_sol": ("marketCapSol", "market_cap_sol", "marketCapSolAmount"),
    "volume_usd": ("volumeUsd",),
    "volume_sol": ("volumeSol", "volume_sol"),
    "liquidity_usd": ("liquidityUsd",),
    "liquidity_sol": ("liquiditySol", "liquidity_sol", "initial_liquidity_sol", "initialLiquiditySol"),
    "bonding_pct": ("bondingCurvePercent", "bonding_curve_percent", "completionPercent"),
    "migrated_at": ("migratedDate", "migrated_date", "migratedAt"),
    "symbol": ("tokenTicker", "token_ticker", "symbol"),
    "pair": ("pairAddress", "pair_address"),
}

_PCT_KEYS = {"top10_pct", "dev_hold_pct", "sniper_pct", "insider_pct", "bundle_pct"}
_SOL_KEYS = {"mcap_sol": "mcap_usd", "volume_sol": "volume_usd", "liquidity_sol": "liquidity_usd"}
_OUT_KEYS = (
    "top10_pct", "dev_hold_pct", "sniper_pct", "insider_pct", "bundle_pct",
    "holders", "pro_traders", "mcap_usd", "volume_usd", "liquidity_usd",
    "protocol", "twitter", "telegram", "website",
)

# Rooms that are not token data. sol_price is consumed; the rest are ignored quietly.
_NOISE_ROOMS = {
    "sol_price", "btc_price", "eth_price", "bnb_price", "block_hash",
    "jito-bribe-fee", "sol-priority-fee", "connection_monitor", "lighthouse",
    "announcement", "force-refresh", "online-users", "connection-monitor",
}
_TOKEN_ROOMS = {"new_pairs", "update_pulse_v2", "update_pulse"}
_COLUMN_KEYS = {
    "newPairs": "axiom_new_pair", "new_pairs": "axiom_new_pair",
    "finalStretch": "axiom_final_stretch", "final_stretch": "axiom_final_stretch",
    "migrated": "axiom_migrated",
}

# tracked-wallet-transactions-v3 row, reconstructed from the positional sample
# shipped in AxiomTradeAPI-py (.chipadev_data/tracked_wallet_tx_v3.json):
# 2 signature, 6 sol, 7 usd, 9 side, 11 maker, 16 mint, 18 symbol.
_WALLET_SIDE, _WALLET_SOL, _WALLET_USD = 9, 6, 7
_WALLET_TX, _WALLET_MAKER, _WALLET_MINT, _WALLET_SYMBOL = 2, 11, 16, 18


def _f(x) -> float | None:
    try:
        if x in (None, ""):
            return None
        n = float(x)
    except (TypeError, ValueError):
        return None
    if n != n:  # NaN
        return None
    return n


def _pct(x) -> float | None:
    n = _f(x)
    if n is None:
        return None
    return n * 100.0 if n <= 1.0 else n


def _loads(payload) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return None
    text = payload.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _mintish(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _MINT_RE.match(value) else None


def _clean_str(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _pick(obj: dict, names: tuple[str, ...]):
    for name in names:
        if name in obj and obj[name] not in (None, ""):
            return obj[name]
    return None


def _convert(kind: str, value):
    if kind == "pct":
        return _pct(value)
    if kind == "num":
        return _f(value)
    if kind == "bool":
        return bool(value) if isinstance(value, bool) else None
    return _clean_str(value) if not isinstance(value, (int, float)) else (str(value) if value else None)


def _apply_sol(fields: dict, sol_usd: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _OUT_KEYS:
        if key in fields and fields[key] not in (None, ""):
            out[key] = fields[key]
    if sol_usd and sol_usd > 0:
        for sol_key, usd_key in _SOL_KEYS.items():
            if usd_key not in out and fields.get(sol_key) is not None:
                out[usd_key] = fields[sol_key] * sol_usd
    return out


def _stage(fields: dict, *, room: str | None, threshold: float, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    proto = str(fields.get("protocol") or "").lower()
    bonding = fields.get("bonding_pct")
    migrated = bool(fields.get("migrated_at")) or bool(fields.get("is_migrated"))
    if isinstance(bonding, (int, float)) and bonding >= 99.5:
        migrated = True
    # Bare "amm" would match unrelated words; require it as its own token.
    # Raydium LaunchLab and Meteora DBC are bonding curves, not graduated pools.
    curve_venue = any(tag in proto for tag in ("launchlab", "launch lab", "dbc", "bonding", "launchpad"))
    if not curve_venue and (re.search(r"\bamm\b", proto)
                            or any(tag in proto for tag in ("raydium", "meteora", "orca", "pumpswap"))):
        migrated = True
    if migrated:
        return "axiom_migrated"
    if room == "new_pairs":
        return "axiom_new_pair"
    if isinstance(bonding, (int, float)) and bonding >= threshold:
        return "axiom_final_stretch"
    if bonding is not None:
        return "axiom_new_pair"
    return "axiom_pulse"


def _signal(mint: str, fields: dict, *, room: str | None, sol_usd, threshold: float,
            explicit: str | None = None) -> ExternalSignal | None:
    mint = _mintish(mint) or ""
    if not mint:
        return None
    data = _apply_sol(fields, sol_usd)
    if fields.get("symbol"):
        data["symbol"] = fields["symbol"]
    return ExternalSignal(ts=0.0, source="axiom", mint=mint, signal=_stage(fields, room=room, threshold=threshold, explicit=explicit), data=data)


def _fields_from_row(row: list, idx_map: dict[int, tuple[str, str]]) -> dict:
    fields: dict[str, Any] = {}
    for idx, (key, kind) in idx_map.items():
        if idx >= len(row):
            continue
        val = _convert(kind, row[idx])
        if val is not None:
            fields[key] = val
    return fields


def _fields_from_obj(obj: dict) -> dict:
    fields: dict[str, Any] = {}
    for key, names in _OBJ_KEYS.items():
        if key in {"mint", "pair", "symbol"}:
            continue
        raw = _pick(obj, names)
        if raw is None:
            continue
        kind = "pct" if key in _PCT_KEYS else "str" if key in {"protocol", "website", "twitter", "telegram", "migrated_at"} else "num"
        val = _convert(kind, raw)
        if val is not None:
            fields[key] = val
    if obj.get("isMigrated") is True or obj.get("is_migrated") is True:
        fields["is_migrated"] = True
    sym = _clean_str(_pick(obj, _OBJ_KEYS["symbol"]))
    if sym:
        fields["symbol"] = sym
    return fields


def _pair_of_row(row: list) -> str | None:
    return _mintish(row[0]) if row else None


def _liquidity_from_pulse_row(row: list, fields: dict) -> None:
    """Index 21 is liquidity in the short row and initial liquidity in the long one."""
    long = _f(row[52]) if len(row) > 52 else None
    short = _f(row[21]) if len(row) > 21 else None
    chosen = long if long is not None else short
    if chosen is not None:
        fields["liquidity_sol"] = chosen


def _token_from_row(row: list, *, room: str | None, sol_usd, threshold: float,
                    explicit: str | None, pairs: dict[str, str]) -> ExternalSignal | None:
    if not isinstance(row, list) or len(row) < 2:
        return None
    mint = _mintish(row[1])
    pair = _pair_of_row(row)
    # Pulse rows put the dev wallet at index 3. new-trending-v2 puts the ticker there
    # and is much longer (market-cap USD sits at index 24, holders at 43).
    dev_at_3 = len(row) > 3 and isinstance(row[3], str) and bool(_mintish(row[3]))
    ticker_trend = (not dev_at_3) and len(row) > 35 and isinstance(row[3] if len(row) > 3 else None, str)
    idx_map = _TREND_IDX if ticker_trend else _PULSE_IDX
    fields = _fields_from_row(row, idx_map)
    if idx_map is _PULSE_IDX:
        _liquidity_from_pulse_row(row, fields)
    if pair and mint:
        pairs[pair] = mint
    return _signal(mint or "", fields, room=room, sol_usd=sol_usd, threshold=threshold, explicit=explicit)


def _token_from_obj(obj: dict, *, room: str | None, sol_usd, threshold: float,
                    explicit: str | None, pairs: dict[str, str]) -> ExternalSignal | None:
    mint = _mintish(_pick(obj, _OBJ_KEYS["mint"]) or "")
    pair = _mintish(_pick(obj, _OBJ_KEYS["pair"]) or "")
    fields = _fields_from_obj(obj)
    if pair and mint:
        pairs[pair] = mint
    if not fields and room not in _TOKEN_ROOMS:
        return None
    return _signal(mint or "", fields, room=room, sol_usd=sol_usd, threshold=threshold, explicit=explicit)


def _looks_like_token_obj(obj: dict) -> bool:
    if _mintish(_pick(obj, _OBJ_KEYS["mint"]) or "") is None:
        return False
    hints = ("protocol", "top10HoldersPercent", "devHoldsPercent", "marketCapSol", "marketCapUsd",
             "numHolders", "bondingCurvePercent", "token_address", "dev_holds_percent", "exchangeName",
             "top_10_holders", "initial_liquidity_sol")
    return any(k in obj for k in hints)


def _trade_from_obj(obj: dict, wallet: str | None = None) -> WalletTrade | None:
    side = _side(obj.get("type") or obj.get("txType") or obj.get("side"))
    mint = _mintish(obj.get("tokenAddress") or obj.get("token_address") or obj.get("mint") or "")
    who = _mintish(obj.get("maker_address") or obj.get("makerAddress") or obj.get("wallet") or wallet or "")
    if not side or not mint or not who:
        return None
    return WalletTrade(
        ts=0.0, source="axiom", label="axiom_tracked", wallet=who, mint=mint, side=side,
        sol=_f(obj.get("total_sol") if obj.get("total_sol") is not None else obj.get("totalSol") or obj.get("solAmount")),
        usd=_f(obj.get("total_usd") if obj.get("total_usd") is not None else obj.get("totalUsd")),
        symbol=_clean_str(obj.get("tokenTicker") or obj.get("token_ticker") or obj.get("symbol")) or "",
        tx=_clean_str(obj.get("signature") or obj.get("tx") or obj.get("transactionHash")) or "",
    )


def _side(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"buy", "b"} or text.startswith("buy"):
        return "buy"
    if text in {"sell", "s"} or text.startswith("sell"):
        return "sell"
    return None


def _trade_from_row(row: list) -> WalletTrade | None:
    if not isinstance(row, list) or len(row) <= _WALLET_MINT:
        return None
    side = _side(row[_WALLET_SIDE])
    mint = _mintish(row[_WALLET_MINT] if isinstance(row[_WALLET_MINT], str) else "")
    maker = _mintish(row[_WALLET_MAKER] if len(row) > _WALLET_MAKER and isinstance(row[_WALLET_MAKER], str) else "")
    if not side or not mint or not maker:
        return None
    symbol = row[_WALLET_SYMBOL] if len(row) > _WALLET_SYMBOL else ""
    tx = row[_WALLET_TX] if len(row) > _WALLET_TX else ""
    return WalletTrade(
        ts=0.0, source="axiom", label="axiom_tracked", wallet=maker, mint=mint, side=side,
        sol=_f(row[_WALLET_SOL]) if len(row) > _WALLET_SOL else None,
        usd=_f(row[_WALLET_USD]) if len(row) > _WALLET_USD else None,
        symbol=symbol if isinstance(symbol, str) else "",
        tx=tx if isinstance(tx, str) else "",
    )


def _sol_price(content) -> float | None:
    if isinstance(content, (int, float)):
        return _f(content)
    if isinstance(content, dict):
        for key in ("usd", "price", "priceUsd", "price_usd", "sol", "value"):
            n = _f(content.get(key))
            if n:
                return n
    if isinstance(content, list) and content:
        return _f(content[0])
    return None


def _pulse_incremental(msg: list, *, sol_usd, threshold: float, pairs: dict[str, str]) -> ExternalSignal | None:
    """Type-1 pulse frame: ``[1, pairAddress, [[fieldIndex, value], ...]]``."""
    if len(msg) < 3 or not isinstance(msg[2], list):
        return None
    pair = msg[1] if isinstance(msg[1], str) else ""
    fields: dict[str, Any] = {}
    mint = pairs.get(pair)
    for change in msg[2]:
        if not isinstance(change, (list, tuple)) or len(change) < 2 or not isinstance(change[0], int):
            continue
        idx, raw = change[0], change[1]
        if idx == 1:
            found = _mintish(raw if isinstance(raw, str) else "")
            if found:
                mint = found
                if pair:
                    pairs[pair] = found
            continue
        spec = _PULSE_IDX.get(idx)
        if spec is None and idx in (21, 52):
            n = _f(raw)
            if n is not None and (idx == 52 or "liquidity_sol" not in fields):
                fields["liquidity_sol"] = n
            continue
        if spec is None:
            continue
        key, kind = spec
        val = _convert(kind, raw)
        if val is not None:
            fields[key] = val
    if not mint:
        return None
    return _signal(mint, fields, room="update_pulse_v2", sol_usd=sol_usd, threshold=threshold)


def parse_ws_message(payload, *, sol_usd: float | None = None, final_stretch_pct: float = 75.0,
                     pairs: dict[str, str] | None = None) -> tuple[list, bool, float | None]:
    """Parse one cluster/pulse frame.

    Returns ``(events, recognized, sol_usd)``. ``recognized`` is False for shapes
    the parser doesn't know, so the caller can sample them. Never raises.
    """
    try:
        return _parse_ws(payload, sol_usd=sol_usd, final_stretch_pct=final_stretch_pct,
                         pairs=pairs if pairs is not None else {})
    except Exception:
        return [], False, sol_usd


def _parse_ws(payload, *, sol_usd, final_stretch_pct, pairs: dict[str, str]) -> tuple[list, bool, float | None]:
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return [], False, sol_usd
    data = _loads(payload)
    if data is None:
        return [], False, sol_usd
    threshold = final_stretch_pct
    if isinstance(data, list):
        return _parse_pulse_list(data, sol_usd=sol_usd, threshold=threshold, pairs=pairs)
    if not isinstance(data, dict):
        return [], False, sol_usd
    room = data.get("room") or data.get("channel") or ""
    if not isinstance(room, str):
        room = ""
    if room == "sol_price":
        price = _sol_price(data.get("content"))
        return [], True, price or sol_usd
    if room in _NOISE_ROOMS or room.startswith(("bnb|", "e-", "s:", "t:", "f:", "td:")):
        return [], True, sol_usd
    wallet = _WALLET_ROOM.match(room)
    content = data.get("content", data.get("data"))
    if wallet:
        trade = _trade_from_obj(content, wallet.group(1)) if isinstance(content, dict) else None
        if trade is None and isinstance(content, list):
            trade = _trade_from_row(content)
        return ([trade] if trade else []), True, sol_usd
    if room in _TOKEN_ROOMS or (isinstance(content, list) and room):
        events = _tokens_from_content(content, room=room or None, sol_usd=sol_usd, threshold=threshold, pairs=pairs)
        return events, True, sol_usd
    if isinstance(content, dict) and _looks_like_token_obj(content):
        ev = _token_from_obj(content, room=room or None, sol_usd=sol_usd, threshold=threshold, explicit=None, pairs=pairs)
        return ([ev] if ev else []), True, sol_usd
    return [], False, sol_usd


def _parse_pulse_list(data: list, *, sol_usd, threshold, pairs) -> tuple[list, bool, float | None]:
    if not data or not isinstance(data[0], int):
        events = []
        recognized = False
        for item in data:
            if isinstance(item, list):
                ev = _token_from_row(item, room="update_pulse_v2", sol_usd=sol_usd, threshold=threshold, explicit=None, pairs=pairs)
                recognized = True
                if ev:
                    events.append(ev)
            elif isinstance(item, dict) and _looks_like_token_obj(item):
                ev = _token_from_obj(item, room=None, sol_usd=sol_usd, threshold=threshold, explicit=None, pairs=pairs)
                recognized = True
                if ev:
                    events.append(ev)
        return events, recognized, sol_usd
    kind = data[0]
    if kind == 0:
        return [], True, sol_usd
    if kind == 1:
        ev = _pulse_incremental(data, sol_usd=sol_usd, threshold=threshold, pairs=pairs)
        return ([ev] if ev else []), True, sol_usd
    if kind == 2 and len(data) > 1 and isinstance(data[1], list):
        row = data[1]
        # Some clients nest the row one level deeper: [2, [row]].
        if row and isinstance(row[0], list):
            row = row[0]
        ev = _token_from_row(row, room="update_pulse_v2", sol_usd=sol_usd, threshold=threshold, explicit=None, pairs=pairs)
        return ([ev] if ev else []), True, sol_usd
    if kind == 3:
        return [], True, sol_usd
    return [], False, sol_usd


def _tokens_from_content(content, *, room, sol_usd, threshold, pairs, explicit=None) -> list:
    events = []
    items = content if isinstance(content, list) else [content] if isinstance(content, dict) else []
    for item in items:
        ev = None
        if isinstance(item, list):
            ev = _token_from_row(item, room=room, sol_usd=sol_usd, threshold=threshold, explicit=explicit, pairs=pairs)
        elif isinstance(item, dict):
            ev = _token_from_obj(item, room=room, sol_usd=sol_usd, threshold=threshold, explicit=explicit, pairs=pairs)
        if ev:
            events.append(ev)
    return events


def parse_http(url: str, data, *, sol_usd: float | None = None, final_stretch_pct: float = 75.0,
               pairs: dict[str, str] | None = None) -> tuple[list, bool, float | None]:
    """Parse one JSON response. Returns ``(events, recognized, sol_usd)``. Never raises."""
    try:
        return _parse_http(url or "", data, sol_usd=sol_usd, final_stretch_pct=final_stretch_pct,
                           pairs=pairs if pairs is not None else {})
    except Exception:
        return [], False, sol_usd


def _unwrap(data):
    if isinstance(data, dict):
        for key in ("responseObject", "data", "result"):
            inner = data.get(key)
            if isinstance(inner, (dict, list)):
                return inner
    return data


def _parse_http(url: str, data, *, sol_usd, final_stretch_pct, pairs) -> tuple[list, bool, float | None]:
    data = _unwrap(data)
    low = url.lower()
    if "tracked-wallet" in low:
        return _wallet_payload(data), True, sol_usd
    if isinstance(data, dict) and any(isinstance(data.get(k), list) for k in _COLUMN_KEYS):
        events = []
        for key, signal in _COLUMN_KEYS.items():
            rows = data.get(key)
            if isinstance(rows, list):
                events.extend(_tokens_from_content(rows, room=None, sol_usd=sol_usd, threshold=final_stretch_pct,
                                                   pairs=pairs, explicit=signal))
        return events, True, sol_usd
    if isinstance(data, dict) and isinstance(data.get("tokens"), list):
        events = _tokens_from_content(data["tokens"], room=None, sol_usd=sol_usd, threshold=final_stretch_pct, pairs=pairs)
        return events, True, sol_usd
    if isinstance(data, list):
        if data and isinstance(data[0], list) and len(data[0]) > _WALLET_MINT and _side(data[0][_WALLET_SIDE] if len(data[0]) > _WALLET_SIDE else None):
            return _wallet_payload(data), True, sol_usd
        events = _tokens_from_content(data, room=None, sol_usd=sol_usd, threshold=final_stretch_pct, pairs=pairs)
        return events, bool(events) or _rows_look_known(data), sol_usd
    if isinstance(data, dict) and _looks_like_token_obj(data):
        ev = _token_from_obj(data, room=None, sol_usd=sol_usd, threshold=final_stretch_pct, explicit=None, pairs=pairs)
        return ([ev] if ev else []), True, sol_usd
    if isinstance(data, dict) and (_trade_from_obj(data) is not None):
        trade = _trade_from_obj(data)
        return ([trade] if trade else []), True, sol_usd
    return [], False, sol_usd


def _rows_look_known(data: list) -> bool:
    if not data:
        return False
    first = data[0]
    if isinstance(first, dict):
        return _looks_like_token_obj(first)
    return isinstance(first, list) and len(first) > 7


def _wallet_payload(data) -> list:
    rows = data.get("transactions") or data.get("trades") or data.get("data") if isinstance(data, dict) else data
    if isinstance(data, dict) and not isinstance(rows, list):
        trade = _trade_from_obj(data)
        return [trade] if trade else []
    if not isinstance(rows, list):
        return []
    out = []
    for row in rows:
        trade = _trade_from_obj(row) if isinstance(row, dict) else _trade_from_row(row) if isinstance(row, list) else None
        if trade:
            out.append(trade)
    return out


class Source(BrowserSource):
    name = "axiom"
    start_url = "https://axiom.trade/pulse"
    login_url = "https://axiom.trade/"
    ws_patterns = ["axiom.trade"]
    http_patterns = ["axiom.trade"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self._sol_usd: float | None = None
        self._pairs: dict[str, str] = {}
        self._seen: set[str] = set()

    def _threshold(self) -> float:
        try:
            return float(self.options.get("final_stretch_pct", 75))
        except (TypeError, ValueError):
            return 75.0

    def _dedupe(self, events: list) -> list:
        out = []
        for ev in events:
            if isinstance(ev, WalletTrade):
                key = "tx:" + ev.tx if ev.tx else f"{ev.wallet}|{ev.mint}|{ev.side}|{ev.sol}|{ev.usd}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                if len(self._seen) > 50_000:
                    self._seen.clear()
            out.append(ev)
        return out

    def on_ws(self, url, payload) -> list:
        events, recognized, sol = parse_ws_message(
            payload, sol_usd=self._sol_usd, final_stretch_pct=self._threshold(), pairs=self._pairs)
        if sol:
            self._sol_usd = sol
        if not recognized:
            self.sample("ws", url, payload)
        return self._dedupe(events)

    def on_http(self, url, data) -> list:
        events, recognized, sol = parse_http(
            url, data, sol_usd=self._sol_usd, final_stretch_pct=self._threshold(), pairs=self._pairs)
        if sol:
            self._sol_usd = sol
        if not recognized:
            self.sample("http", url, data)
        return self._dedupe(events)

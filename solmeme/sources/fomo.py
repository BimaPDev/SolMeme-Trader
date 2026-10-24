"""fomo.family leaderboard + live-fill adapter.

fomo has no public API. The private host the web app calls is ``prod-api.fomo.family``
(Cloudflare rejects non-browser clients on authenticated routes), plus a websocket at
``wss://prod-api.fomo.family/ws``. This adapter only parses traffic the logged-in page
already receives.

Shapes are reconstructed from the itsnex1s/fomo-sdk contract (fixtures sanitized from
live responses on 2026-09-28) and its ``docs/API.md`` / ``src/live.ts``. They are not
captures from this project. Unknown payloads are sampled and never raise.

Solana's ``networkId`` is ``1399811149``. Only that chain is emitted. Leaderboard
entries contribute the followed-trader set (top ``top_n`` of the configured window,
default 50 of ``7d``). A fill becomes a ``WalletTrade`` only when its trader is in
that set and its USD size is at least ``min_usd`` (default 20).
"""

from __future__ import annotations

import json
import re
from typing import Any

from solmeme.core.events import ExternalSignal, WalletTrade
from solmeme.sources.browser import BrowserSource

SOLANA = 1399811149
_WINDOWS = ("24h", "7d", "30d", "all-time")
_QUOTE = {
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}
_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_BUY_TYPES = {"swap_buy", "large_buy", "multi_user_buy"}
_SELL_TYPES = {"swap_sell", "large_sell", "single_user_sell", "multi_user_sell"}
_AGGREGATE_TYPES = {"multi_user_buy", "multi_user_sell"}
_WS_NOISE = {"challenge", "challengeAccepted", "subscribed", "unsubscribed", "error", "ping", "pong"}
_ROTATE = ("https://fomo.family/leaderboard", "https://fomo.family/")


def _f(x) -> float | None:
    try:
        if x in (None, ""):
            return None
        n = float(x)
    except (TypeError, ValueError):
        return None
    if n != n:
        return None
    return n


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


def _unwrap(data):
    if isinstance(data, dict):
        for key in ("responseObject", "data", "result"):
            inner = data.get(key)
            if isinstance(inner, (dict, list)) and key != "data":
                return inner
            if key == "data" and isinstance(inner, (dict, list)) and "leaderboard" not in data and "swaps" not in data:
                return inner
    return data


def _addr(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _ADDR.match(value) else None


def _network(value) -> int | None:
    if isinstance(value, str) and value.strip().lower() in {"sol", "solana"}:
        return SOLANA
    n = _f(value)
    return int(n) if n is not None else None


def _is_solana(value, *, default: bool = False) -> bool:
    if value is None:
        return default
    return _network(value) == SOLANA


class Followed:
    """Solana wallets (and their fomo user ids / handles) taken from a leaderboard."""

    def __init__(self):
        self.wallets: set[str] = set()
        self.ids: dict[str, str] = {}
        self.handles: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self.wallets)

    def wallet_for(self, *, wallet=None, user_id=None, handle=None) -> str | None:
        found = _addr(wallet or "")
        if found and found in self.wallets:
            return found
        if user_id and str(user_id) in self.ids:
            return self.ids[str(user_id)]
        if handle and str(handle).lstrip("@").lower() in self.handles:
            return self.handles[str(handle).lstrip("@").lower()]
        return None


def parse_leaderboard(data) -> list[dict]:
    """Return ranked traders ``[{id, address, handle}]`` in board order. Never raises."""
    try:
        body = _unwrap(data)
        rows = body.get("leaderboard") if isinstance(body, dict) else body if isinstance(body, list) else None
        if not isinstance(rows, list):
            return []
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            address = _addr(row.get("address") or "")
            if not address:
                continue
            out.append({
                "id": str(row.get("id") or ""),
                "address": address,
                "handle": str(row.get("userHandle") or row.get("handle") or "").lstrip("@"),
            })
        return out
    except Exception:
        return []


def leaderboard_window(url: str) -> str | None:
    """Window named in a leaderboard URL, ``''`` if the path has no window, else None."""
    match = re.search(r"/leaderboard(?:/(24h|7d|30d|all-time))?(?:[/?#]|$)", url or "")
    if not match:
        return None
    return match.group(1) or ""


def _usd_of(obj: dict, side: str) -> float | None:
    preferred = ("humanUsdAmountIn", "amountInUSD") if side == "buy" else ("humanUsdAmountOut", "amountOutUSD")
    fallback = ("humanUsdAmountOut", "amountOutUSD", "usdAmount", "humanUsdAmount", "usdValue")
    if side == "sell":
        fallback = ("humanUsdAmountIn", "amountInUSD", "usdAmount", "humanUsdAmount", "usdValue")
    for key in preferred + fallback:
        n = _f(obj.get(key))
        if n is not None:
            return n
    body = obj.get("body")
    if isinstance(body, dict):
        for key in ("humanUsdAmount", "usdValue", "usdAmount", "totalVolume"):
            n = _f(body.get(key))
            if n is not None and key != "totalVolume":
                return n
            if n is not None and obj.get("type") not in _AGGREGATE_TYPES:
                return n
    return _f(obj.get("usdAmount"))


def _sol_of_swap(swap: dict, side: str) -> float | None:
    inn, out = swap.get("inTokenAddress"), swap.get("outTokenAddress")
    wsol = "So11111111111111111111111111111111111111112"
    if inn == wsol:
        return _f(swap.get("inHumanAmount"))
    if out == wsol:
        return _f(swap.get("outHumanAmount"))
    if side == "buy":
        return _f(swap.get("inHumanAmount")) if inn == wsol else None
    return None


def _trade(wallet: str, mint: str, side: str, *, usd, sol, symbol, tx) -> WalletTrade | None:
    if side not in {"buy", "sell"} or not wallet or not mint or mint in _QUOTE:
        return None
    return WalletTrade(
        ts=0.0, source="fomo", label="fomo", wallet=wallet, mint=mint, side=side,
        sol=sol, usd=usd, symbol=symbol or "", tx=tx or "")


def parse_swap(swap: dict, followed: Followed, *, min_usd: float = 20.0) -> WalletTrade | None:
    """One ``/v2/users/{id}/swaps`` row → a Solana trade, or None. Never raises."""
    try:
        if not isinstance(swap, dict):
            return None
        inn, out = swap.get("inTokenAddress"), swap.get("outTokenAddress")
        in_net, out_net = _network(swap.get("inNetworkId")), _network(swap.get("outNetworkId"))
        mint, side = None, None
        if in_net == SOLANA and _addr(inn or "") and inn not in _QUOTE:
            mint, side = inn, "sell"
        elif out_net == SOLANA and _addr(out or "") and out not in _QUOTE:
            mint, side = out, "buy"
        if not mint or not side:
            return None
        wallet = followed.wallet_for(
            wallet=swap.get("address") if side == "sell" else swap.get("recipient"),
            user_id=swap.get("userId"))
        if wallet is None:
            wallet = followed.wallet_for(wallet=swap.get("address") or swap.get("recipient"), user_id=swap.get("userId"))
        if wallet is None:
            return None
        usd = _usd_of(swap, side)
        if usd is not None and usd < min_usd:
            return None
        return _trade(wallet, mint, side, usd=usd, sol=_sol_of_swap(swap, side),
                      symbol="", tx=str(swap.get("signature") or swap.get("txHash") or swap.get("id") or ""))
    except Exception:
        return None


def _side_of_type(kind: str) -> str | None:
    if kind in _BUY_TYPES or kind.endswith("_buy"):
        return "buy"
    if kind in _SELL_TYPES or kind.endswith("_sell"):
        return "sell"
    return None


def parse_feed_item(item: dict, followed: Followed, *, min_usd: float = 20.0) -> WalletTrade | None:
    """One trading-activity / token-feed item. Aggregates (multi_user_*) are not one wallet. Never raises."""
    try:
        if not isinstance(item, dict):
            return None
        kind = str(item.get("type") or "")
        if kind in _AGGREGATE_TYPES:
            return None
        side = _side_of_type(kind)
        if not side:
            return None
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        net = item.get("networkId", body.get("networkId"))
        if not _is_solana(net, default=False):
            return None
        mint = _addr(item.get("tokenAddress") or body.get("tokenAddress") or body.get("address") or "")
        if not mint:
            return None
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        wallet = followed.wallet_for(
            wallet=item.get("address") or item.get("userAddress") or body.get("address"),
            user_id=item.get("userId") or body.get("userId") or user.get("id"),
            handle=item.get("userHandle") or body.get("userHandle") or user.get("userHandle"))
        if wallet is None:
            return None
        usd = _usd_of(item, side)
        if usd is not None and usd < min_usd:
            return None
        symbol = item.get("symbol") or body.get("ticker") or body.get("symbol") or ""
        return _trade(wallet, mint, side, usd=usd, sol=None, symbol=str(symbol or ""),
                      tx=str(item.get("signature") or item.get("txHash") or item.get("tradeId") or item.get("id") or ""))
    except Exception:
        return None


def _token_address(row: dict) -> tuple[str | None, int | None]:
    token = row.get("token") if isinstance(row.get("token"), dict) else {}
    address = _addr(token.get("address") or row.get("tokenAddress") or row.get("address") or "")
    net = _network(token.get("networkId") if token.get("networkId") is not None else row.get("networkId"))
    return address, net


def parse_trending(data) -> list[ExternalSignal]:
    """``/proxy/trendingTokens`` or a token-list snapshot. Solana only. Never raises."""
    try:
        return _parse_trending(data)
    except Exception:
        return []


def _trending_rows(data) -> list:
    body = _unwrap(data)
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("tokens", "items", "trending"):
            if isinstance(body.get(key), list):
                return body[key]
    return []


def _parse_trending(data) -> list[ExternalSignal]:
    events = []
    rank = 0
    for row in _trending_rows(data):
        if not isinstance(row, dict):
            continue
        mint, net = _token_address(row)
        if net is not None and net != SOLANA:
            continue
        if not mint or net != SOLANA:
            continue
        rank += 1
        token = row.get("token") if isinstance(row.get("token"), dict) else {}
        info = {
            "fomo_rank": rank,
            "symbol": token.get("symbol") or row.get("symbol"),
            "holders": _f(row.get("holders") or row.get("uniqueTraders")),
            "fomo_buyers": _f(row.get("uniqueTraders") or row.get("holders")),
            "mcap_usd": _f(row.get("marketCap")),
            "volume_usd": _f(row.get("volume24") or row.get("volume")),
            "liquidity_usd": _f(row.get("liquidity")),
        }
        events.append(ExternalSignal(
            ts=0.0, source="fomo", mint=mint, signal="fomo_trending",
            data={k: v for k, v in info.items() if v not in (None, "")}))
    return events


def _trending_update(payload: dict) -> list[ExternalSignal]:
    kind = str(payload.get("kind") or "")
    if kind == "remove":
        return []
    if kind == "snapshot" or "tokens" in payload:
        return parse_trending(payload)
    if kind == "update":
        key = str(payload.get("tokenKey") or "")
        address, _, net = key.rpartition(":")
        if _network(net) != SOLANA:
            return []
        mint = _addr(address)
        update = payload.get("update") if isinstance(payload.get("update"), dict) else {}
        if not mint:
            return []
        data = {k: v for k, v in {
            "mcap_usd": _f(update.get("marketCap")),
            "volume_usd": _f(update.get("volume24")),
            "holders": _f(update.get("holders")),
        }.items() if v is not None}
        return [ExternalSignal(ts=0.0, source="fomo", mint=mint, signal="fomo_trending", data=data)]
    return parse_trending(payload)


def parse_ws_message(payload, followed: Followed, *, min_usd: float = 20.0) -> tuple[list, bool]:
    """One fomo websocket frame. Returns ``(events, recognized)``. Never raises."""
    try:
        data = _loads(payload)
        if not isinstance(data, dict):
            return [], False
        kind = str(data.get("type") or "")
        if kind in _WS_NOISE:
            return [], True
        if kind != "data":
            return [], False
        topic = str(data.get("topicType") or "")
        body = data.get("payload")
        if topic in {"trending_tokens", "graduated_tokens", "pre_graduated_tokens"}:
            if isinstance(body, dict):
                return _trending_update(body), True
            return parse_trending(body), True
        if topic in {"trading_activity", "token_feed", "feed"}:
            items = body if isinstance(body, list) else [body]
            events = [ev for item in items if isinstance(item, dict) for ev in [_one_feed(item, followed, min_usd)] if ev]
            return events, True
        if topic in {"prices", "token_details", "bars"}:
            return [], True
        return [], False
    except Exception:
        return [], False


def _one_feed(item: dict, followed: Followed, min_usd: float):
    trade = parse_feed_item(item, followed, min_usd=min_usd)
    return trade


def parse_http(url: str, data, followed: Followed, *, min_usd: float = 20.0) -> tuple[list, bool, list[dict] | None]:
    """Parse one JSON body. ``traders`` is a list when this was a leaderboard, else None.

    Returns ``(events, recognized, traders)``. Never raises.
    """
    try:
        return _parse_http(url or "", data, followed, min_usd=min_usd)
    except Exception:
        return [], False, None


def _parse_http(url: str, data, followed: Followed, *, min_usd: float) -> tuple[list, bool, list[dict] | None]:
    window = leaderboard_window(url)
    body = _unwrap(data)
    if window is not None or (isinstance(body, dict) and isinstance(body.get("leaderboard"), list)):
        traders = parse_leaderboard(data)
        if window is not None or traders:
            return [], True, traders
    if isinstance(body, dict) and isinstance(body.get("swaps"), list):
        events = [ev for row in body["swaps"] if (ev := parse_swap(row, followed, min_usd=min_usd))]
        return events, True, None
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        events = [ev for row in body["items"] if (ev := parse_feed_item(row, followed, min_usd=min_usd))]
        return events, True, None
    if isinstance(body, dict) and isinstance(body.get("feed"), list):
        events = [ev for row in body["feed"] if (ev := parse_feed_item(row, followed, min_usd=min_usd))]
        return events, True, None
    if "trending" in url or (isinstance(body, list) and body and isinstance(body[0], dict) and "token" in body[0]):
        return parse_trending(data), True, None
    if isinstance(body, dict) and _token_address(body)[0] and "marketCap" in body:
        return parse_trending([body]), True, None
    return [], False, None


class Source(BrowserSource):
    name = "fomo"
    start_url = "https://fomo.family/leaderboard"
    login_url = "https://fomo.family/"
    ws_patterns = ["fomo.family"]
    http_patterns = ["fomo.family"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self.followed = Followed()
        self.leaderboard: list[dict] = []
        self._seen: set[str] = set()
        self._tick = 0
        self._have_period = False      # the configured leaderboard window has been seen

    def _params(self) -> tuple[int, str, float]:
        try:
            top_n = int(self.options.get("top_n", 50))
        except (TypeError, ValueError):
            top_n = 50
        period = str(self.options.get("leaderboard") or "7d")
        if period not in _WINDOWS:
            period = "7d"
        try:
            min_usd = float(self.options.get("min_usd", 20))
        except (TypeError, ValueError):
            min_usd = 20.0
        return max(0, top_n), period, min_usd

    def _apply_traders(self, traders: list[dict]) -> None:
        top_n, _, _ = self._params()
        book = Followed()
        for row in traders[:top_n]:
            book.wallets.add(row["address"])
            if row.get("id"):
                book.ids[row["id"]] = row["address"]
            if row.get("handle"):
                book.handles[row["handle"].lower()] = row["address"]
        self.followed = book
        self.leaderboard = list(traders[:top_n])

    def _dedupe(self, events: list) -> list:
        out = []
        for ev in events:
            if isinstance(ev, WalletTrade):
                key = "tx:" + ev.tx if ev.tx else f"{ev.wallet}|{ev.mint}|{ev.side}|{ev.usd}|{ev.sol}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                if len(self._seen) > 50_000:
                    self._seen.clear()
            out.append(ev)
        return out

    def _period_ok(self, url: str) -> bool:
        window = leaderboard_window(url)
        if window is None or window == "":
            return True
        _, period, _ = self._params()
        return window == period

    def on_ws(self, url, payload) -> list:
        _, _, min_usd = self._params()
        events, recognized = parse_ws_message(payload, self.followed, min_usd=min_usd)
        if not recognized:
            self.sample("ws", url, payload)
        return self._dedupe(events)

    def on_http(self, url, data) -> list:
        _, period, min_usd = self._params()
        events, recognized, traders = parse_http(url, data, self.followed, min_usd=min_usd)
        if traders:                               # empty → not the trader list; keep the current set
            if leaderboard_window(url) == period:
                self._have_period = True
            # The site opens on its 24h board; use whatever window loaded until the configured one does.
            if self._period_ok(url) or not self._have_period:
                self._apply_traders(traders)
        if not recognized:
            self.sample("http", url, data)
        return self._dedupe(events)

    async def on_page(self, page) -> None:
        """Alternate the leaderboard and the home feed so both XHRs keep flowing."""
        self._tick += 1
        if self._tick % 12 != 1:
            return
        try:
            cur = page.url or ""
        except Exception:
            return
        if "fomo.family" not in cur:
            return
        if any(part in cur for part in ("account-recovery", "verify-token", "reauth", "login")):
            return
        nxt = _ROTATE[(self._tick // 12) % 2]
        if nxt.rstrip("/") == cur.split("?", 1)[0].rstrip("/"):
            return
        try:
            await page.goto(nxt, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            return

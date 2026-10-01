"""j7tracker.io tweet-feed adapter.

j7tracker.io streams tweets from tracked X accounts over socket.io. There is no
public read API (docs.j7tracker.io is a deploy/trade API — this adapter never
calls it and never reads a wallet key). Only ``j7tracker.io`` is contacted;
``j7tracker.com`` is a lookalike and is not a pattern here.

What the public web bundle (``/static/js/main.*.js``) actually does, read from
the shipped JS rather than from a captured frame:

* Feed socket is socket.io on ``wss://nyc.j7tracker.io`` or ``wss://dfw.j7tracker.io``
  (``transports: ["websocket"]``, auth token = the session).
* Events: ``tweet``, ``initialTweets`` (array), ``tweet_update``,
  ``tweet.subtweet.update``, ``quoted_tweet``, ``reply_tweet``, ``token_meta``,
  ``vamp_trigger``, ``feed_card`` / ``feed_cards``.
* A tweet object uses ``id``, ``text``, ``author.handle``, ``author.followersCount``,
  ``isReply``, ``isRetweet``, ``isQuote``, plus optional ``quotedTweet`` / ``replyTo``
  / ``card``. The client finds contract addresses by scanning text with a base58
  regex; tweets do not carry a structured CA list.
* ``token_meta`` is ``{id, tokenMeta: {mint, chain, symbol, name, marketCapUsd,
  liquidityUsd, ...}}`` and links a tweet to a mint (validator ``zv`` in the bundle).
* ``vamp_trigger`` is ``{coinData: {mint_address|mint|contractAddress|..., twitter,
  symbol, name}}`` — a tweet-linked coin the app offers to copy.

Those field names are from the bundle, not from a live frame. Payloads that don't
match are sampled. Parsing never raises.

``min_followers`` (default 0) and ``authors`` (default empty = everyone) come from
``sources.j7tracker`` in settings. Per-mint ``tweet_count`` / ``max_followers`` are
accumulated on the source and copied into each signal's ``data``, because
``state.ext`` only keeps the latest dict.
"""

from __future__ import annotations

import json
import re
from typing import Any

from solmeme.core.events import ExternalSignal
from solmeme.sources.browser import BrowserSource

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
# Bounded on both sides: a 32-44 char slice of an 88-char tx signature must not count as a mint.
_B58 = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
_URLS = (
    re.compile(r"pump\.fun/(?:coin|token)/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"dexscreener\.com/solana/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"gmgn\.ai/sol/token/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"axiom\.trade/(?:t|meme)/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"photon-sol\.tinyastro\.io/[a-z]{2}/lp/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"birdeye\.so/(?:token|solana)/([1-9A-HJ-NP-Za-km-z]{32,44})"),
    re.compile(r"solscan\.io/token/([1-9A-HJ-NP-Za-km-z]{32,44})"),
)
# Well-known non-meme mints. System program is 32 '1's (decodes to 32 zero bytes).
_IGNORE = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB",
    "11111111111111111111111111111111",
}
_TWEET_EVENTS = {"tweet", "tweet_update", "tweet.subtweet.update", "quoted_tweet", "reply_tweet", "initialTweets"}
_LAUNCH_EVENTS = {"vamp_trigger", "vamp_update_trigger"}
_META_EVENTS = {"token_meta"}
_CARD_EVENTS = {"feed_card", "feed_cards"}
_NOISE_EVENTS = {
    "connect", "disconnect", "connect_error", "connected_users", "auth_error", "error",
    "debug_response", "buy_success", "buy_error", "pnl_update", "user_connected",
    "hidden_accounts_updated", "admin_alert", "admin_alert_clear", "deploy_success",
    "sell_result", "tracked_fomo", "tracked_pump", "tracked_telegram",
}


def b58decode(text: str) -> bytes | None:
    """Decode a base58 string. Returns None if a character is outside the alphabet."""
    n = 0
    for char in text:
        i = _ALPHABET.find(char)
        if i < 0:
            return None
        n = n * 58 + i
    pad = 0
    for char in text:
        if char != "1":
            break
        pad += 1
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + body


def is_solana_mint(value: str) -> bool:
    """True for a base58 string that decodes to exactly 32 bytes and isn't a quote asset."""
    if not isinstance(value, str) or not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", value):
        return False
    if value in _IGNORE:
        return False
    raw = b58decode(value)
    return raw is not None and len(raw) == 32


def extract_mints(text: str) -> list[str]:
    """Mints mentioned in free text or in known Solana token URLs, in order, de-duplicated."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(mint: str) -> None:
        if mint not in seen and is_solana_mint(mint):
            seen.add(mint)
            found.append(mint)

    for pattern in _URLS:
        for match in pattern.finditer(text):
            add(match.group(1))
    for match in _B58.finditer(text):
        add(match.group(0))
    return found


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
    if not text:
        return None
    if text[0] in "{[":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def iter_socketio(payload) -> list[tuple[str, Any]]:
    """Yield ``(event, data)`` from a socket.io / engine.io frame, or from a raw JSON object.

    A bare dict is treated as a tweet when it has text/author, and as a named event when
    it has ``event`` + ``data``. A bare list of tweets is ``initialTweets``.
    """
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return []
    if isinstance(payload, dict):
        if isinstance(payload.get("event"), str):
            return [(payload["event"], payload.get("data"))]
        if payload.get("text") or payload.get("full_text") or isinstance(payload.get("author"), (dict, str)):
            return [("tweet", payload)]
        return []
    if isinstance(payload, list):
        return [("initialTweets", payload)]
    if not isinstance(payload, str):
        return []
    out = []
    for part in payload.split("\x1e"):
        part = part.strip()
        if not part:
            continue
        if part[0] in "{[":
            data = _loads(part)
            if isinstance(data, dict):
                out.extend(iter_socketio(data))
            elif isinstance(data, list):
                out.append(("initialTweets", data))
            continue
        # engine.io packet type is the first digit; socket.io rides on type "4" (message).
        if part[0] in "0123456" and not part.startswith("4"):
            continue
        if part[0] == "4":
            part = part[1:]
        if not part or part[0] not in "0123456":
            continue
        if part[0] not in "23":  # socket.io event or ack
            continue
        rest = part[1:]
        attach = re.match(r"\d+-", rest)
        if attach:
            rest = rest[attach.end():]
        else:
            ack = re.match(r"(\d+)(?=[\[/])", rest)
            if ack:
                rest = rest[ack.end():]
        if rest.startswith("/"):
            comma = rest.find(",")
            if comma < 0:
                continue
            rest = rest[comma + 1:]
        if not rest.startswith("["):
            continue
        try:
            arr = json.loads(rest)
        except json.JSONDecodeError:
            continue
        if isinstance(arr, list) and arr and isinstance(arr[0], str):
            out.append((arr[0], arr[1] if len(arr) > 1 else None))
    return out


def _handle(author) -> str:
    if isinstance(author, str):
        return author.lstrip("@").strip()
    if not isinstance(author, dict):
        return ""
    handle = author.get("handle") or author.get("username") or author.get("screen_name") or ""
    return str(handle).lstrip("@").strip()


def _followers(author) -> float | None:
    if not isinstance(author, dict):
        return None
    metrics = author.get("metrics") if isinstance(author.get("metrics"), dict) else {}
    public = author.get("public_metrics") if isinstance(author.get("public_metrics"), dict) else {}
    for value in (author.get("followersCount"), author.get("followers_count"), author.get("followers"),
                  metrics.get("followers"), public.get("followers_count")):
        try:
            if value is None or value == "":
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _texts(tweet: dict) -> str:
    chunks = []
    for key in ("text", "full_text", "body"):
        if isinstance(tweet.get(key), str):
            chunks.append(tweet[key])
    for nested_key in ("quotedTweet", "replyTo", "retweetedTweet", "retweet", "quoted_status"):
        nested = tweet.get(nested_key)
        if isinstance(nested, dict):
            chunks.append(_texts(nested))
    card = tweet.get("card")
    if isinstance(card, dict):
        for key in ("url", "title", "description"):
            if isinstance(card.get(key), str):
                chunks.append(card[key])
    for key in ("tweetUrl", "tweet_url", "url"):
        if isinstance(tweet.get(key), str):
            chunks.append(tweet[key])
    return "\n".join(chunks)


def _tweet_id(tweet: dict) -> str:
    for key in ("id", "tweet_id", "tweetId", "rest_id"):
        if tweet.get(key) not in (None, ""):
            return str(tweet[key])
    return ""


def _flag(tweet: dict, *keys) -> bool:
    return any(bool(tweet.get(key)) for key in keys)


def parse_tweet(tweet: dict, *, signal: str = "j7_tweet_ca") -> list[ExternalSignal]:
    """One tweet-like object → one signal per Solana mint found in its text. Never raises."""
    try:
        if not isinstance(tweet, dict):
            return []
        text = _texts(tweet)
        mints = extract_mints(text)
        if not mints:
            return []
        author = _handle(tweet.get("author") or tweet.get("user"))
        followers = _followers(tweet.get("author") if isinstance(tweet.get("author"), dict) else tweet.get("user"))
        preview = (tweet.get("text") or tweet.get("full_text") or text).strip().replace("\n", " ")[:200]
        data = {
            "author": author,
            "tweet_id": _tweet_id(tweet),
            "is_reply": _flag(tweet, "isReply", "is_reply"),
            "is_retweet": _flag(tweet, "isRetweet", "is_retweet", "isQuote", "is_quote"),
            "text": preview,
        }
        if followers is not None:
            data["followers"] = followers
        return [ExternalSignal(ts=0.0, source="j7tracker", mint=mint, signal=signal, data=dict(data)) for mint in mints]
    except Exception:
        return []


def _mint_from_coin(coin: dict) -> str | None:
    if not isinstance(coin, dict):
        return None
    for key in ("mint_address", "mint", "contractAddress", "tokenAddress", "address", "sourceMint"):
        value = coin.get(key)
        if isinstance(value, str) and is_solana_mint(value.strip()):
            return value.strip()
    return None


def parse_token_meta(msg: dict) -> list[ExternalSignal]:
    """``token_meta`` event → ``j7_tweet_ca`` when the linked mint is Solana. Never raises."""
    try:
        if not isinstance(msg, dict):
            return []
        meta = msg.get("tokenMeta") if isinstance(msg.get("tokenMeta"), dict) else msg
        chain = str(meta.get("chain") or "sol").lower()
        if chain not in {"sol", "solana", ""}:
            return []
        mint = meta.get("mint") or meta.get("address")
        if not isinstance(mint, str) or not is_solana_mint(mint.strip()):
            return []
        data: dict[str, Any] = {
            "tweet_id": str(msg.get("id") or msg.get("tweet_id") or ""),
            "author": _handle(msg.get("author")),
            "is_reply": False,
            "is_retweet": False,
            "text": "",
        }
        followers = _followers(msg.get("author"))
        if followers is not None:
            data["followers"] = followers
        for src, dst in (("symbol", "symbol"), ("marketCapUsd", "mcap_usd"), ("liquidityUsd", "liquidity_usd")):
            if meta.get(src) not in (None, ""):
                data[dst] = meta[src]
        return [ExternalSignal(ts=0.0, source="j7tracker", mint=mint.strip(), signal="j7_tweet_ca", data=data)]
    except Exception:
        return []


def parse_launch(msg: dict) -> list[ExternalSignal]:
    """``vamp_trigger`` / a feed card with an embedded Solana mint → ``j7_launch``. Never raises."""
    try:
        if not isinstance(msg, dict):
            return []
        coin = msg.get("coinData") if isinstance(msg.get("coinData"), dict) else msg
        mints = []
        found = _mint_from_coin(coin)
        if found:
            mints.append(found)
        webhook = msg.get("webhook") if isinstance(msg.get("webhook"), dict) else {}
        embeds = webhook.get("embeds") if isinstance(webhook.get("embeds"), list) else []
        for embed in embeds:
            if isinstance(embed, dict) and isinstance(embed.get("address"), str) and is_solana_mint(embed["address"]):
                if embed["address"] not in mints:
                    mints.append(embed["address"])
        text = " ".join(str(coin.get(k) or "") for k in ("twitter", "website", "description", "name", "symbol"))
        for mint in extract_mints(text):
            if mint not in mints:
                mints.append(mint)
        if not mints:
            return []
        handle = ""
        twitter = str(coin.get("twitter") or "")
        match = re.search(r"(?:twitter\.com|x\.com)/([^/?#]+)", twitter)
        if match and match.group(1) not in {"i", "intent", "share"}:
            handle = match.group(1)
        events = []
        for mint in mints:
            data: dict[str, Any] = {
                "author": handle,
                "tweet_id": str(msg.get("id") or ""),
                "is_reply": False,
                "is_retweet": False,
                "text": f"{coin.get('name') or ''} {coin.get('symbol') or ''}".strip()[:200],
            }
            if coin.get("symbol"):
                data["symbol"] = coin["symbol"]
            events.append(ExternalSignal(ts=0.0, source="j7tracker", mint=mint, signal="j7_launch", data=data))
        return events
    except Exception:
        return []


class MintStats:
    """Running per-mint tweet ids and the largest author follower count."""

    def __init__(self):
        self.seen: set[tuple[str, str]] = set()
        self.tweets: dict[str, set[str]] = {}
        self.max_followers: dict[str, float] = {}

    def accept(self, events: list, *, min_followers: float, authors: set[str]) -> list:
        out = []
        for ev in events:
            if not isinstance(ev, ExternalSignal):
                continue
            author = str(ev.data.get("author") or "")
            if authors and author.lower() not in authors:
                continue
            followers = ev.data.get("followers")
            if min_followers > 0 and (followers is None or float(followers) < min_followers):
                continue
            tweet_id = str(ev.data.get("tweet_id") or "")
            # Dedupe only when we have a stable tweet id. Launches without one still dedupe
            # on (signal, mint) once a tweet id is missing and the text matches.
            dedupe_key = (tweet_id, ev.mint, ev.signal) if tweet_id else (ev.signal, ev.mint, ev.data.get("text", ""))
            if dedupe_key in self.seen:
                continue
            self.seen.add(dedupe_key)
            if ev.signal == "j7_tweet_ca":
                bucket = self.tweets.setdefault(ev.mint, set())
                bucket.add(tweet_id or dedupe_key[0])
            if followers is not None:
                self.max_followers[ev.mint] = max(self.max_followers.get(ev.mint, 0.0), float(followers))
            data = dict(ev.data)
            if ev.mint in self.tweets:
                data["tweet_count"] = len(self.tweets[ev.mint])
            if ev.mint in self.max_followers:
                data["max_followers"] = self.max_followers[ev.mint]
            out.append(ExternalSignal(ts=0.0, source=ev.source, mint=ev.mint, signal=ev.signal, data=data))
        if len(self.seen) > 50_000:
            self.seen.clear()
        return out


def events_from(event: str, data, *, signal_for_tweet: str = "j7_tweet_ca") -> tuple[list, bool]:
    """Map one socket.io event to signals. ``recognized`` is False for unknown event names."""
    if event in _NOISE_EVENTS:
        return [], True
    if event in _TWEET_EVENTS:
        items = data if isinstance(data, list) else [data]
        events = []
        for item in items:
            events.extend(parse_tweet(item, signal=signal_for_tweet))
        return events, True
    if event in _META_EVENTS:
        items = data if isinstance(data, list) else [data]
        events = []
        for item in items:
            events.extend(parse_token_meta(item))
        return events, True
    if event in _LAUNCH_EVENTS or event in _CARD_EVENTS:
        items = data if isinstance(data, list) else [data]
        events = []
        for item in items:
            events.extend(parse_launch(item))
        return events, True
    return [], False


def _allow_authors(authors) -> set[str]:
    """Allow-list of handles. A list or a comma-separated string; empty means everyone."""
    if not authors:
        return set()
    parts = authors.split(",") if isinstance(authors, str) else authors
    allow = set()
    for part in parts:
        name = str(part).strip().lstrip("@").strip().lower()
        if name:
            allow.add(name)
    return allow


def parse_ws_message(payload, *, min_followers: float = 0.0, authors: list | str | None = None,
                     stats: MintStats | None = None) -> tuple[list, bool]:
    """Parse one websocket frame. Returns ``(events, recognized)``. Never raises.

    ``recognized`` is False when nothing in the frame was a known event, so the
    caller can sample it. ``stats`` accumulates dedupe + per-mint counters.
    """
    try:
        frames = iter_socketio(payload)
        if not frames:
            # Engine.IO control frames (open / ping / pong / close) are not tweets.
            if isinstance(payload, str) and payload.strip()[:1] in "012356":
                return [], True
            return [], False
        allow = _allow_authors(authors)
        book = stats if stats is not None else MintStats()
        events: list = []
        recognized = False
        for name, data in frames:
            found, known = events_from(name, data)
            recognized = recognized or known
            events.extend(found)
        if not recognized:
            return [], False
        return book.accept(events, min_followers=float(min_followers or 0), authors=allow), True
    except Exception:
        return [], False


class Source(BrowserSource):
    """Leave ``https://j7tracker.io/?popout=social`` open.

    That popout is the feed without the deploy panel. ``login_url`` is the main
    site so ``solmeme login j7tracker`` can complete whatever login the app shows.
    """

    name = "j7tracker"
    start_url = "https://j7tracker.io/?popout=social"
    login_url = "https://j7tracker.io/"
    ws_patterns = ["j7tracker.io"]
    http_patterns = ["j7tracker.io"]

    def __init__(self, ctx):
        super().__init__(ctx)
        self._stats = MintStats()

    def _params(self) -> tuple[float, list]:
        try:
            min_followers = float(self.options.get("min_followers", 0) or 0)
        except (TypeError, ValueError):
            min_followers = 0.0
        authors = self.options.get("authors") or []
        if isinstance(authors, str):
            authors = [part.strip() for part in authors.split(",") if part.strip()]
        return min_followers, list(authors)

    def on_ws(self, url, payload) -> list:
        if "j7tracker.com" in (url or "") and "j7tracker.io" not in (url or ""):
            return []
        min_followers, authors = self._params()
        events, recognized = parse_ws_message(
            payload, min_followers=min_followers, authors=authors, stats=self._stats)
        if not recognized:
            self.sample("ws", url, payload)
        return events

    def on_http(self, url, data) -> list:
        # The feed is socket.io over a websocket. Polling responses, when they
        # are JSON, use the same event shape: {"event": name, "data": ...} or a tweet.
        if "j7tracker.com" in (url or "") and "j7tracker.io" not in (url or ""):
            return []
        min_followers, authors = self._params()
        events, recognized = parse_ws_message(
            data, min_followers=min_followers, authors=authors, stats=self._stats)
        if not recognized:
            self.sample("http", url, data)
        return events

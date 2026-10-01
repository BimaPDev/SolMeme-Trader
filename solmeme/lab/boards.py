"""One-shot look at GMGN boards, pump.fun, and a card for each copied buy.

Trenches, trending, and copy-trade (KOL + smart-money buys) come from the
GMGN OpenAPI. Pump.fun new / almost / graduated coins come from the public
coin list. The card uses token info + security when a GMGN key is set, plus
the title of the token's own https site. Launch text is shown as untrusted
data. This does not search X and does not call an LLM, so a missing post
stays missing.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from solmeme.sim.curve import INITIAL_V_TOKENS, bonding_pct
from solmeme.sources.gmgn import (
    KOL_PATH, RESPONSE_KEY, SIGNAL_FOR_TYPE, SMARTMONEY_PATH, SOL_PLATFORMS,
    SOL_QUOTE_ADDRESS_TYPES, TOKEN_INFO_PATH, TOKEN_SECURITY_PATH, TRENCHES_PATH,
    _f, _pct, _ts, normalize_activity, normalize_trench,
)

RANK_PATH = "/v1/market/rank"
PUMP_COINS = "https://frontend-api-v3.pump.fun/coins"
_INSTRUCT = re.compile(
    r"ignore (all |any |previous |prior )?instructions|system prompt|you must (buy|sell|swap)|"
    r"disregard (the |all )?(above|previous)",
    re.I,
)


class _Title(HTMLParser):
    def __init__(self):
        super().__init__()
        self._on = False
        self.title = ""

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._on = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._on = False

    def handle_data(self, data):
        if self._on and len(self.title) < 180:
            self.title += data


def page_title(html: str) -> str:
    p = _Title()
    try:
        p.feed(html[:20_000])
    except Exception:
        return ""
    return " ".join(p.title.split())[:160]


def looks_like_instruction(text: str) -> bool:
    return bool(text and _INSTRUCT.search(text))


def trending_rows(data, limit: int = 15) -> list[dict]:
    rows = []
    if isinstance(data, dict):
        raw = data.get("rank") or data.get("list") or []
    elif isinstance(data, list):
        raw = data
    else:
        raw = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mint = item.get("address") or ""
        if not mint:
            continue
        rows.append({
            "mint": mint,
            "symbol": item.get("symbol") or "",
            "name": item.get("name") or "",
            "mcap_usd": _f(item.get("market_cap") or item.get("usd_market_cap")),
            "volume_usd": _f(item.get("volume")),
            "holders": _f(item.get("holder_count")),
            "liquidity_usd": _f(item.get("liquidity")),
            "hot_level": _f(item.get("hot_level")),
        })
        if len(rows) >= limit:
            break
    return rows


def trench_columns(data, per_column: int = 8) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not isinstance(data, dict):
        return out
    for req_type, sig in SIGNAL_FOR_TYPE.items():
        col = []
        for item in data.get(RESPONSE_KEY.get(req_type, req_type)) or []:
            if not isinstance(item, dict):
                continue
            norm = normalize_trench(item, req_type)
            if not norm:
                continue
            mint, d = norm
            col.append({"mint": mint, "signal": sig, **d})
            if len(col) >= per_column:
                break
        out[req_type] = col
    return out


def copy_buys(rows, label: str, *, now: float, window_s: float) -> list[dict]:
    buys = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        ev = normalize_activity(item, label)
        if ev is None or ev.side != "buy":
            continue
        t = _ts(item.get("timestamp") or item.get("block_timestamp"))
        if t is not None and now - t > window_s:
            continue
        buys.append({
            "mint": ev.mint, "symbol": ev.symbol, "wallet": ev.wallet, "label": label,
            "usd": ev.usd, "ts": t, "tx": ev.tx,
        })
    buys.sort(key=lambda b: b["ts"] or 0, reverse=True)
    return buys


def score_token(info: dict, security: dict) -> tuple[str, list[str]]:
    """skip / research / watch. Hard stops follow the public GMGN scoring card."""
    info = info if isinstance(info, dict) else {}
    security = security if isinstance(security, dict) else {}
    stat = info.get("stat") if isinstance(info.get("stat"), dict) else {}
    dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
    hard: list[str] = []
    warn: list[str] = []

    hp = security.get("is_honeypot")
    if hp in (True, 1, "1", "yes", "true") or security.get("honeypot") in (1, "1", True):
        hard.append("honeypot")
    rug = _f(security.get("rug_ratio") if security.get("rug_ratio") is not None else stat.get("rug_ratio"))
    if rug is not None and rug > 0.30:
        hard.append(f"rug {rug:.2f}")
    elif rug is not None and rug >= 0.10:
        warn.append(f"rug {rug:.2f}")
    top10 = _pct(security.get("top_10_holder_rate") if security.get("top_10_holder_rate") is not None
                 else stat.get("top_10_holder_rate"))
    if top10 is not None and top10 > 50:
        hard.append(f"top10 {top10:.0f}%")
    elif top10 is not None and top10 >= 20:
        warn.append(f"top10 {top10:.0f}%")
    if security.get("renounced_mint") is False:
        hard.append("mint authority still live")
    if security.get("renounced_freeze_account") is False:
        hard.append("freeze authority still live")
    status = dev.get("creator_token_status") or security.get("creator_token_status")
    if status == "creator_hold":
        hard.append("dev still holding")
    holders = _f(info.get("holder_count"))
    if holders is not None and holders < 20:
        warn.append(f"only {holders:.0f} holders")
    liq = _f(info.get("liquidity"))
    if liq is not None and liq < 5_000:
        warn.append(f"liquidity ${liq:,.0f}")
    if hard:
        return "skip", hard + warn
    if len(warn) >= 3:
        return "research", warn
    return "watch", warn


def narrative_from_info(info: dict) -> dict:
    info = info if isinstance(info, dict) else {}
    link = info.get("link") if isinstance(info.get("link"), dict) else {}
    dev = info.get("dev") if isinstance(info.get("dev"), dict) else {}
    desc = str(link.get("description") or "")
    name = str(info.get("name") or "")
    suspicious = looks_like_instruction(desc) or looks_like_instruction(name)
    return {
        "symbol": info.get("symbol") or "",
        "name": "" if looks_like_instruction(name) else name,
        "story": "" if suspicious else " ".join(desc.split())[:240],
        "suspicious": suspicious,
        "twitter": link.get("twitter_username") or None,
        "website": link.get("website") or None,
        "telegram": link.get("telegram") or None,
        "dev_status": dev.get("creator_token_status"),
        "dev_launches": dev.get("creator_open_count"),
        "cto": dev.get("cto_flag") in (1, "1", True),
        "holders": _f(info.get("holder_count")),
        "liquidity_usd": _f(info.get("liquidity")),
        "mcap_usd": _mcap(info),
    }


def _mcap(info: dict):
    price = info.get("price")
    px = _f(price.get("price") if isinstance(price, dict) else price)
    supply = _f(info.get("circulating_supply") or info.get("total_supply"))
    if px is None or supply is None:
        return None
    return px * supply


def safe_site(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url.startswith("https://") or " " in url:
        return None
    host = url.split("/")[2].lower()
    if host in {"localhost", "127.0.0.1"} or host.endswith(".local"):
        return None
    return url


def normalize_pump(item: dict) -> dict | None:
    """One frontend-api-v3 coin. Reserves are raw (6 decimals); curve math wants whole tokens."""
    if not isinstance(item, dict):
        return None
    mint = item.get("mint") or ""
    if not mint or item.get("nsfw") or item.get("is_banned"):
        return None
    v_tok = _f(item.get("virtual_token_reserves"))
    pct = None
    if v_tok:
        ui = v_tok / 1e6 if v_tok > 1e12 else v_tok
        if 0 < ui <= INITIAL_V_TOKENS:
            pct = round(bonding_pct(ui), 1)
            mcap = _f(item.get("usd_market_cap") or item.get("market_cap_usd")) or 0
            if pct == 0 and mcap > 10_000:
                pct = None
    tw = (item.get("twitter") or "").strip()
    handle = tw
    for host in ("x.com/", "twitter.com/"):
        if host in tw:
            handle = tw.split(host, 1)[1].split("?")[0].strip("/").split("/")[0]
            break
    handle = handle.lstrip("@")
    return {
        "mint": mint,
        "symbol": item.get("symbol") or "",
        "name": item.get("name") or "",
        "mcap_usd": _f(item.get("usd_market_cap") or item.get("market_cap_usd")),
        "bonding_pct": pct,
        "complete": bool(item.get("complete")),
        "replies": _f(item.get("reply_count")),
        "description": item.get("description") or "",
        "twitter": handle or None,
        "website": (item.get("website") or "").strip() or None,
        "telegram": (item.get("telegram") or "").strip() or None,
        "creator": item.get("creator") or "",
    }


def pump_as_info(row: dict) -> dict:
    return {
        "symbol": row.get("symbol") or "",
        "name": row.get("name") or "",
        "link": {
            "description": row.get("description") or "",
            "twitter_username": row.get("twitter"),
            "website": row.get("website"),
            "telegram": row.get("telegram"),
        },
    }


def pump_lists(payloads: dict[str, list], limit: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for name, raw in payloads.items():
        rows = []
        for item in raw or []:
            row = normalize_pump(item)
            if not row:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        out[name] = rows
    return out


def render_card(buy: dict, card: dict, verdict: str, reasons: list[str], site_title: str) -> str:
    sym = card.get("symbol") or buy.get("symbol") or "?"
    if buy.get("label") == "pump":
        bond = buy.get("bonding_pct")
        who = "pump.fun" + (f"  bonding {bond:.0f}%" if isinstance(bond, (int, float)) else "")
        who += f"  creator {str(buy.get('wallet') or '')[:8]}"
    else:
        who = (f"bought by {buy.get('label')} {str(buy.get('wallet') or '')[:8]}  "
               f"usd={buy.get('usd') if buy.get('usd') is not None else '?'}")
    lines = [
        f"{sym}  {buy.get('mint', '')}",
        who,
        f"verdict: {verdict}" + (f"  ({', '.join(reasons)})" if reasons else ""),
        f"stats: mcap={_money(card.get('mcap_usd'))}  liq={_money(card.get('liquidity_usd'))}  "
        f"holders={_n(card.get('holders'))}  dev={card.get('dev_status') or '?'}"
        + ("  cto" if card.get("cto") else "")
        + (f"  prior_launches={card.get('dev_launches')}" if card.get("dev_launches") is not None else ""),
    ]
    if card.get("suspicious"):
        lines.append("story: launch text looks like it is trying to steer a reader. Not used.")
    elif card.get("story"):
        lines.append(f"story (written by the launcher, not checked): {card['story']}")
    else:
        lines.append("story: the token has no description on file.")
    bits = []
    if card.get("twitter"):
        bits.append(f"x=@{card['twitter']}")
    if card.get("website"):
        bits.append(f"site={card['website']}")
    if card.get("telegram"):
        bits.append("telegram=yes")
    lines.append("links: " + (", ".join(bits) if bits else "none on the token"))
    if site_title:
        lines.append(f"site title: {site_title}")
    elif card.get("website"):
        lines.append("site title: not fetched (missing, not https, or the page did not answer)")
    lines.append("web: posts on X were not searched. A story with no page behind it is unverified.")
    return "\n".join(lines)


def _money(x):
    return "?" if x is None else f"${x:,.0f}"


def _n(x):
    return "?" if x is None else f"{x:.0f}"


def _rows(data) -> list:
    if isinstance(data, dict):
        return data.get("list") or data.get("activities") or []
    return data if isinstance(data, list) else []


def _merge_pump(info: dict, row: dict) -> dict:
    """Keep GMGN fields. Fill name, story, and links from the pump.fun coin when GMGN left them blank."""
    extra = pump_as_info(row)
    out = dict(info) if isinstance(info, dict) else {}
    out.setdefault("symbol", extra["symbol"])
    if not out.get("name"):
        out["name"] = extra["name"]
    link = dict(out.get("link") or {}) if isinstance(out.get("link"), dict) else {}
    for key, val in extra["link"].items():
        if val and not link.get(key):
            link[key] = val
    out["link"] = link
    return out


async def _one_card(req, buy: dict, pump_row: dict | None, fetch_html) -> dict:
    info: dict = {}
    sec: dict = {}
    if req is not None:
        got = await req("GET", TOKEN_INFO_PATH, {"chain": "sol", "address": buy["mint"]}) or {}
        sec_got = await req("GET", TOKEN_SECURITY_PATH, {"chain": "sol", "address": buy["mint"]}) or {}
        info = got if isinstance(got, dict) else {}
        sec = sec_got if isinstance(sec_got, dict) else {}
    if pump_row:
        info = _merge_pump(info, pump_row)
    card = narrative_from_info(info)
    if pump_row and not card.get("mcap_usd"):
        card["mcap_usd"] = pump_row.get("mcap_usd")
    verdict, reasons = score_token(info, sec)
    if req is None:
        verdict = "research"
        reasons = ["safety stats not loaded"] + reasons
    title = ""
    url = safe_site(card.get("website"))
    if url and fetch_html is not None:
        try:
            html = await fetch_html(url)
        except Exception:
            html = None
        title = page_title(html or "")
    return {"buy": buy, "text": render_card(buy, card, verdict, reasons, title), "verdict": verdict}


async def snapshot(req=None, *, now: float, per_column: int = 8, trending_limit: int = 15,
                   buy_window_s: float = 1800, max_cards: int = 6, fetch_html=None,
                   pump_get=None) -> dict:
    """req(method, path, params, body=None) is Gmgn._req. pump_get(params)->list. fetch_html(url)->str|None."""
    trenches: dict = {}
    trending: list = []
    buys: list = []
    cards: list = []
    seen: set[str] = set()
    if req is not None:
        trench_data = await req("POST", TRENCHES_PATH, {"chain": "sol"}, trenches_body(per_column))
        rank = await req("GET", RANK_PATH, {
            "chain": "sol", "interval": "1h", "limit": trending_limit,
            "order_by": "volume", "direction": "desc",
        })
        kol = await req("GET", KOL_PATH, {"chain": "sol", "limit": 50})
        smart = await req("GET", SMARTMONEY_PATH, {"chain": "sol", "limit": 50})
        trenches = trench_columns(trench_data or {}, per_column)
        trending = trending_rows(rank or {}, trending_limit)
        buys = copy_buys(_rows(kol), "kol", now=now, window_s=buy_window_s)
        buys += copy_buys(_rows(smart), "smartmoney", now=now, window_s=buy_window_s)
        buys.sort(key=lambda b: b["ts"] or 0, reverse=True)
        for buy in buys:
            if buy["mint"] in seen:
                continue
            seen.add(buy["mint"])
            cards.append(await _one_card(req, buy, None, fetch_html))
            if len(cards) >= max_cards:
                break
    pump: dict[str, list] = {}
    if pump_get is not None:
        payloads = {}
        for name, sort, complete in (
            ("new", "created_timestamp", "false"),
            ("almost", "market_cap", "false"),
            ("graduated", "created_timestamp", "true"),
        ):
            payloads[name] = await pump_get({
                "offset": 0, "limit": per_column, "order": "DESC", "includeNsfw": "false",
                "sort": sort, "complete": complete,
            }) or []
        pump = pump_lists(payloads, per_column)
        n = 0
        for row in pump.get("almost") or []:
            if row["mint"] in seen:
                continue
            seen.add(row["mint"])
            buy = {
                "mint": row["mint"], "symbol": row["symbol"], "label": "pump",
                "wallet": row.get("creator") or "", "usd": None, "bonding_pct": row.get("bonding_pct"),
            }
            cards.append(await _one_card(req, buy, row, fetch_html))
            n += 1
            if n >= max_cards:
                break
    return {"trenches": trenches, "trending": trending, "buys": buys, "cards": cards, "pump": pump}


def trenches_body(limit: int) -> dict:
    section = {
        "filters": ["offchain", "onchain"], "launchpad_platform_v2": True, "limit": limit,
        "launchpad_platform": SOL_PLATFORMS, "quote_address_type": SOL_QUOTE_ADDRESS_TYPES,
    }
    return {"version": "v2", "new_creation": dict(section), "near_completion": dict(section), "completed": dict(section)}

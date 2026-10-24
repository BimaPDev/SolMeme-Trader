"""Find wallets worth paper-copying and read their 7-day record.

Candidates come from the GMGN KOL and smart-money feeds, or from a name /
address search. The verdict uses wallet_stats only: win rate, pnl, how often
they trade, and whether they mostly launch tokens. Nothing here sends a
transaction.
"""

from __future__ import annotations

import re

from solmeme.sources.gmgn import KOL_PATH, SMARTMONEY_PATH, WALLET_ACTIVITY_PATH, _f, normalize_activity

STATS_PATH = "/v1/user/wallet_stats"
SEARCH_PATH = "/v1/market/search"
_B58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def is_address(text: str) -> bool:
    return bool(text and _B58.match(text.strip()))


def _activity_rows(data) -> list:
    if isinstance(data, dict):
        return data.get("list") or data.get("activities") or []
    return data if isinstance(data, list) else []


def gather_traders(rows, label: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        raw = (item.get("event_type") or item.get("type") or item.get("side") or "").lower()
        if raw.replace("_", "").startswith("transfer"):
            continue
        ev = normalize_activity(item, label)
        if ev is None or ev.side != "buy" or not ev.wallet or ev.wallet == "unknown":
            continue
        slot = out.setdefault(ev.wallet, {
            "wallet": ev.wallet, "labels": set(), "buys": 0, "usd": 0.0, "symbols": [],
        })
        slot["labels"].add(label)
        slot["buys"] += 1
        if ev.usd:
            slot["usd"] += ev.usd
        if ev.symbol and ev.symbol not in slot["symbols"]:
            slot["symbols"].append(ev.symbol)
    return out


def merge_traders(*groups: dict[str, dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    for group in groups:
        for wallet, slot in group.items():
            cur = merged.setdefault(wallet, {
                "wallet": wallet, "labels": set(), "buys": 0, "usd": 0.0, "symbols": [],
            })
            cur["labels"] |= set(slot["labels"])
            cur["buys"] += slot["buys"]
            cur["usd"] += slot["usd"]
            for sym in slot["symbols"]:
                if sym not in cur["symbols"]:
                    cur["symbols"].append(sym)
    rows = list(merged.values())
    rows.sort(key=lambda r: (r["usd"], r["buys"]), reverse=True)
    return rows


def parse_stats(data, wallet: str) -> dict:
    if isinstance(data, dict) and isinstance(data.get("list"), list):
        data = data["list"]
    if isinstance(data, list):
        matches = [x for x in data if isinstance(x, dict) and x.get("wallet_address") in (wallet, None, "")]
        exact = [x for x in matches if x.get("wallet_address") == wallet]
        pick = exact or matches or [x for x in data if isinstance(x, dict)]
        return pick[0] if pick else {}
    return data if isinstance(data, dict) else {}


def judge(stats: dict) -> tuple[str, list[str]]:
    """copy / watch / skip from a 7d wallet_stats object."""
    stats = stats if isinstance(stats, dict) else {}
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    win = _f(stats.get("winrate"))
    pnl = _f(stats.get("pnl"))
    buys = _f(stats.get("buy_count")) or 0
    created = _f(common.get("created_token_count")) or 0
    if created >= 3 and created * 2 > buys:
        return "skip", [f"launched {created:.0f} tokens; this looks like a dev wallet"]
    if buys > 200:
        return "skip", [f"{buys:.0f} buys in 7d; too fast to copy"]
    notes: list[str] = []
    if buys < 5:
        notes.append(f"only {buys:.0f} buys in 7d")
    if win is None and pnl is None:
        return "watch", notes + ["7d stats missing"]
    if win is not None and win < 0.35:
        notes.append(f"win rate {win * 100:.0f}%")
    if pnl is not None and pnl <= 0:
        notes.append(f"7d pnl {pnl:.2f}x")
    good = win is not None and pnl is not None and win >= 0.45 and pnl > 0 and buys >= 5
    if good and not notes:
        return "copy", [f"win {win * 100:.0f}%", f"pnl {pnl:.2f}x", f"{buys:.0f} buys / 7d"]
    if pnl is not None and pnl > 0 and (win is None or win >= 0.35):
        return "watch", notes or [f"pnl {pnl:.2f}x"]
    return "skip", notes or ["no profitable 7d record"]


def display_name(stats: dict, fallback: str = "") -> str:
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    name = common.get("name") or common.get("twitter_username") or fallback
    return str(name or "")


def search_wallets(data) -> list[dict]:
    raw = []
    if isinstance(data, dict):
        raw = data.get("wallets") or []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        addr = item.get("address") or ""
        if not addr:
            continue
        out.append({
            "wallet": addr,
            "twitter": item.get("twitter_username") or "",
            "tags": item.get("wallet_tags") or [],
        })
    return out


def render_person(row: dict, stats: dict, verdict: str, reasons: list[str], *, followed: bool) -> str:
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    name = display_name(stats) or row.get("twitter") or ""
    labels = ",".join(sorted(row.get("labels") or []))
    head = row["wallet"]
    if name:
        head = f"{name}  {head}"
    lines = [
        head,
        f"verdict: {verdict}" + (f"  ({', '.join(reasons)})" if reasons else ""),
    ]
    bits = []
    if labels:
        bits.append(labels)
    if row.get("buys"):
        bits.append(f"{row['buys']} recent buys")
    if row.get("usd"):
        bits.append(f"${row['usd']:,.0f}")
    tags = common.get("tags") or row.get("tags") or []
    if tags:
        bits.append("tags " + ",".join(str(t) for t in tags[:4]))
    if common.get("twitter_username"):
        bits.append(f"x=@{common['twitter_username']}")
    if bits:
        lines.append(" · ".join(bits))
    symbols = row.get("symbols") or []
    if symbols:
        lines.append("recent: " + ", ".join(symbols[:6]))
    if followed:
        lines.append("already in sources.tracked_wallets")
    elif verdict == "copy":
        lines.append("paper only: add this address to sources.tracked_wallets, then ./run")
    return "\n".join(lines)


async def _stats(req, wallet: str) -> dict:
    data = await req("GET", STATS_PATH, {"chain": "sol", "wallet_address": wallet, "period": "7d"})
    return parse_stats(data or {}, wallet)


async def _recent_buys(req, wallet: str) -> dict:
    data = await req("GET", WALLET_ACTIVITY_PATH, {
        "chain": "sol", "wallet_address": wallet, "limit": 20, "type": ["buy", "sell"],
    })
    found = gather_traders(_activity_rows(data), "tracked")
    return found.get(wallet) or {"wallet": wallet, "labels": set(), "buys": 0, "usd": 0.0, "symbols": []}


class TraderBook:
    """Traders seen on the fomo leaderboard or in recorded trades. No GMGN calls."""

    def __init__(self):
        self._rows: dict[str, dict] = {}

    def _slot(self, wallet: str, rank: int | None = None) -> dict:
        slot = self._rows.get(wallet)
        if slot is None:
            slot = {
                "wallet": wallet, "handle": "", "labels": set(), "buys": 0, "sells": 0,
                "usd": 0.0, "symbols": [], "rank": 10_000, "on_board": False,
            }
            self._rows[wallet] = slot
        if rank is not None:
            slot["rank"] = rank
        return slot

    def add_leaderboard(self, traders: list[dict] | None) -> None:
        present: set[str] = set()
        for i, row in enumerate(traders or []):
            if not isinstance(row, dict):
                continue
            wallet = str(row.get("address") or row.get("wallet") or "")
            if not wallet:
                continue
            slot = self._slot(wallet, i)
            handle = str(row.get("handle") or "").lstrip("@")
            if handle:
                slot["handle"] = handle
            slot["labels"].add("fomo")
            slot["on_board"] = True
            present.add(wallet)
        for wallet, slot in self._rows.items():
            if wallet not in present:
                slot["on_board"] = False

    def add_tracked(self, wallets: list[str] | None) -> None:
        for wallet in wallets or []:
            wallet = str(wallet or "").strip()
            if wallet:
                self._slot(wallet)["labels"].add("tracked")

    def add_trade(self, wallet: str, side: str, usd: float | None = None, symbol: str = "", label: str = "") -> None:
        if not wallet or wallet == "unknown":
            return
        slot = self._slot(wallet)
        if label:
            slot["labels"].add(label)
        if side == "buy":
            slot["buys"] += 1
            if usd:
                slot["usd"] += float(usd)
            if symbol and symbol not in slot["symbols"]:
                slot["symbols"].append(symbol)
        elif side == "sell":
            slot["sells"] += 1

    def cards(self, *, limit: int = 8) -> list[dict]:
        rows = [
            row for row in self._rows.values()
            if row["on_board"] or row["buys"] or row["sells"] or "tracked" in row["labels"]
        ]
        rows.sort(key=lambda r: (-r["usd"], -r["buys"], r["rank"]))
        out = []
        for row in rows[:limit]:
            handle = row["handle"]
            wallet = row["wallet"]
            head = f"{handle}  {wallet}" if handle else wallet
            lines = [head, "verdict: watch"]
            if row["buys"] or row["usd"] or row["sells"]:
                bits = []
                labels = sorted(row["labels"])
                if labels:
                    bits.append(",".join(labels))
                if row["buys"]:
                    bits.append(f"{row['buys']} buys")
                if row["sells"]:
                    bits.append(f"{row['sells']} sells")
                if row["usd"]:
                    bits.append(f"${row['usd']:,.0f}")
                lines.append(" · ".join(bits))
                if row["symbols"]:
                    lines.append("recent: " + ", ".join(row["symbols"][:6]))
            elif "fomo" in row["labels"]:
                lines.append("fomo leaderboard")
            else:
                lines.append("tracked")
            out.append({"wallet": wallet, "verdict": "watch", "text": "\n".join(lines)})
        return out


def fomo_cards(traders: list[dict] | None, *, limit: int = 8) -> list[dict]:
    book = TraderBook()
    book.add_leaderboard(traders)
    return book.cards(limit=limit)


async def discover(req, *, limit: int = 8, followed: set[str] | None = None) -> list[dict]:
    followed = followed or set()
    kol = await req("GET", KOL_PATH, {"chain": "sol", "limit": 50})
    smart = await req("GET", SMARTMONEY_PATH, {"chain": "sol", "limit": 50})
    ranked = merge_traders(
        gather_traders(_activity_rows(kol), "kol"),
        gather_traders(_activity_rows(smart), "smartmoney"),
    )
    cards = []
    for row in ranked[:limit]:
        stats = await _stats(req, row["wallet"])
        verdict, reasons = judge(stats)
        cards.append({
            "wallet": row["wallet"],
            "verdict": verdict,
            "text": render_person(row, stats, verdict, reasons, followed=row["wallet"] in followed),
        })
    order = {"copy": 0, "watch": 1, "skip": 2}
    cards.sort(key=lambda c: order.get(c["verdict"], 9))
    return cards


async def lookup(req, query: str, *, followed: set[str] | None = None) -> list[dict]:
    followed = followed or set()
    query = query.strip()
    if is_address(query):
        targets = [{"wallet": query, "twitter": "", "tags": []}]
    else:
        data = await req("GET", SEARCH_PATH, {"q": query, "chain": "sol"})
        targets = search_wallets(data or {})
    cards = []
    for hit in targets[:8]:
        row = await _recent_buys(req, hit["wallet"])
        row["twitter"] = hit.get("twitter") or ""
        row["tags"] = hit.get("tags") or []
        stats = await _stats(req, hit["wallet"])
        verdict, reasons = judge(stats)
        cards.append({
            "wallet": hit["wallet"],
            "verdict": verdict,
            "text": render_person(row, stats, verdict, reasons, followed=hit["wallet"] in followed),
        })
    return cards

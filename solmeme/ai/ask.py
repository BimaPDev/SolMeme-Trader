"""Ask the council about one coin you found yourself: should I trade it?

You paste a mint on the dashboard. The bot starts following the coin (it is pinned on the watchlist so
DexScreener, GeckoTerminal and our on-chain check fetch it), waits for data, then builds an evidence pack:
the coin card (holders, dev, red and green flags, buyers, narrative), the global filter's verdict, which
strategies would buy it right now and which rule stops each one, and how live entries have compared with
paper. A few judges answer buy / wait / avoid; their votes are weighted by reputation. Advice only:
nothing is bought or sold, and the paper wallets are untouched.

Each ask is one JSON file in data/session/asks/, written by the worker thread (never SQLite).
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from solmeme.ai.track_record import Follower, base_rates, lane_of, trade_plan

MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
WAIT_FOR_DATA_S = 45
MAX_JUDGES = 3
KEEP = dict(pick=200, review=80, wallet=80, coin=80)    # per kind: a day of auto-picks must not push out reviews or yours

SYSTEM = (
    "You are one judge advising a human trader on a single Solana meme coin they found themselves. "
    "Decide from the evidence whether to buy it now, wait, or avoid it. Be skeptical: most meme coins go "
    "to zero, late entries buy other people's exits, and data can be 10-40 seconds old. Reply with JSON only. "
    "Be terse, like notes; every reason at most 15 words; numbers stay exact."
)
SCHEMA = ('{"verdict": "buy" | "wait" | "avoid", "confidence": 0.0-1.0, "reasons": ["..."], "risks": ["..."], '
          '"plan": "if buy: entry, take profit, stop (short)"}')


def _dir(settings) -> Path:
    d = settings.data_dir / "session" / "asks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(settings, ask: dict) -> None:
    path = _dir(settings) / f"{ask['id']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ask, default=str), encoding="utf-8")
    tmp.replace(path)


def list_asks(settings, ask_id: str = "") -> dict:
    files = sorted(_dir(settings).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    items, kept = [], {}
    for p in files:
        try:
            a = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        kind = a.get("kind") or "coin"
        kept[kind] = kept.get(kind, 0) + 1
        if kept[kind] > KEEP.get(kind, KEEP["coin"]):
            p.unlink(missing_ok=True)
            continue
        items.append(a)
    if ask_id:
        return next((a for a in items if a["id"] == ask_id), {"error": "unknown ask"})
    return {"asks": items}


# ── evidence ──────────────────────────────────────────────────────────────
def _strategies(session, st, now) -> list[dict]:
    """For each champion and the busiest live agents: would it buy now, and if not, which rules stop it."""
    from solmeme.features.registry import FeatureView
    from solmeme.strategy.dsl import evaluate, in_universe

    eng = session.engine
    fv = FeatureView(st, now, eng.market)
    blocked = eng._passes_prefilter(fv)
    rows = []
    for r in list(eng.runners):
        if r.label not in ("champion", "swarm", "challenger"):
            continue
        ok, failed = evaluate(r.spec, fv, explain=True)
        rows.append(dict(key=r.key, role=r.label, universe=in_universe(r.spec, st, now), would_buy=ok and blocked is None,
                         failed=failed[:4]))
    rows.sort(key=lambda x: (x["role"] != "champion", not x["would_buy"]))
    return [dict(prefilter=blocked)] + rows[:10]


def evidence(session, mint: str) -> tuple[str, dict]:
    from solmeme.ui.narrative_view import build_token

    eng = session.engine
    st = eng.market.get(mint)
    card = build_token(session, mint)
    now = eng.now or time.time()
    strat = _strategies(session, st, now) if st is not None else []
    chain = (st.ext.get("chaincheck") if st is not None else None) or _chain_now(session.settings, mint)
    dry = getattr(session, "dryrun", None)
    calib = None
    try:
        from solmeme.sim.calibration import load
        c = load(session.settings.data_dir)
        calib = c.summary() if c else None
    except Exception:  # noqa: BLE001
        pass
    facts = dict(card=card, strategies=strat, chain=chain, calibration=calib,
                 live=dict(attempt=getattr(dry, "attempt", None)) if dry else None)
    f = card.get("features") or {}
    lines = [f"Coin {card.get('symbol') or '?'} ({card.get('name') or ''}) mint {mint}",
             f"pool {card.get('pool')} migrated {card.get('migrated')} price_sol {card.get('price_sol')}",
             "features: " + ", ".join(f"{k}={_fmt(v)}" for k, v in f.items() if v is not None),
             "red flags: " + ("; ".join(card.get("red_flags") or []) or "none"),
             "green flags: " + ("; ".join(card.get("green_flags") or []) or "none"),
             f"creator: {json.dumps(card.get('creator'))}",
             f"tracked buyers (first 15): {json.dumps([dict(label=b['label'], whale=b['whale'], banned=b['banned'], sold=b['sold']) for b in (card.get('buyers') or [])[:15]])}",
             f"our on-chain check: {json.dumps({k: v for k, v in chain.items() if k != 'checked_ts'}) if chain else 'not yet'}"]
    if strat:
        lines.append(f"global filter: {'passes' if strat[0]['prefilter'] is None else 'BLOCKS (' + strat[0]['prefilter'] + ')'}")
        for r in strat[1:]:
            lines.append(f"strategy {r['key']} ({r['role']}): {'WOULD BUY' if r['would_buy'] else 'no; fails ' + ', '.join(r['failed'] or ['universe'])}")
    if calib:
        lines.append(f"live vs paper so far: real entries median {_fmt(calib.get('buy_gap_median'))} above paper's price "
                     f"({calib.get('n')} live buy attempts)")
    tweets = [s.get("text", "")[:120] for s in (card.get("story") or [])[:3]]
    if tweets:
        lines.append("matching tweets: " + " | ".join(tweets))
    return "\n".join(lines), facts


def _chain_now(settings, mint: str) -> dict:
    """The on-chain mint check, read directly when the background checker has not reached this coin yet."""
    import httpx

    from solmeme.sources.chaincheck import mint_flags, reasons

    try:
        r = httpx.post(settings.sources.chaincheck.rpc_urls[0], timeout=10, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [mint, {"encoding": "jsonParsed"}]})
        d = {k: v for k, v in mint_flags(((r.json().get("result") or {}).get("value"))).items() if k != "supply"}
        return {**d, "reasons": reasons(d)} if d else {}
    except Exception:  # noqa: BLE001 - the ask goes on without it
        return {}


def _fmt(v):
    return f"{v:.4g}" if isinstance(v, float) else v


# ── the council's answer ──────────────────────────────────────────────────
def _judges(settings) -> list:
    from solmeme.ai.council import pace_cursor

    from solmeme.ai.council import cursor_out

    members = [m for m in settings.council.members if m.enabled]
    sit, _ = pace_cursor(settings, members)
    main = [m for m in sit if not m.fallback]
    if main:
        return main[:MAX_JUDGES]
    backup = [m for m in sit if m.fallback and m.kind != "ollama"]     # 9router first
    if not backup or cursor_out(settings):                             # local GPU judges only when Cursor is out
        backup += [m for m in sit if m.fallback and m.kind == "ollama"]
    return backup[:MAX_JUDGES]


def judge(settings, text: str, ask_fn=None, system: str = SYSTEM, schema: str = SCHEMA,
          verdicts: tuple[str, ...] = ("buy", "wait", "avoid"), max_judges: int = MAX_JUDGES) -> dict:
    from solmeme.ai.council import ask as council_ask, reputations, weight
    from solmeme.core.store import Store

    ask_fn = ask_fn or council_ask
    members = _judges(settings)[:max_judges]
    if not members:
        return dict(verdict="unknown", error="no judge available", votes=[])
    try:
        reps = reputations(Store(settings.db_path))
    except Exception:  # noqa: BLE001 - a busy DB must not stop the answer
        reps = {}
    user = f"{text}\n\nAnswer with this JSON: {schema}"

    def one(m):
        t = time.time()
        try:
            a = ask_fn(m, system, user) or {}
            v = str(a.get("verdict", "")).lower()
            if v not in verdicts:
                raise ValueError(f"no verdict in {str(a)[:80]}")
            return dict(judge=m.name, verdict=v, confidence=min(1.0, max(0.0, float(a.get("confidence") or 0.5))),
                        reasons=[str(x)[:160] for x in (a.get("reasons") or [])][:4],
                        risks=[str(x)[:160] for x in (a.get("risks") or [])][:4],
                        plan=str(a.get("plan") or "")[:240], seconds=round(time.time() - t, 1))
        except Exception as e:  # noqa: BLE001
            return dict(judge=m.name, error=str(e)[:200], seconds=round(time.time() - t, 1))

    with ThreadPoolExecutor(max_workers=len(members)) as ex:
        votes = list(ex.map(one, members))
    good = [v for v in votes if "verdict" in v]
    if not good:
        return dict(verdict="unknown", error="no judge answered", votes=votes)
    score = {k: 0.0 for k in verdicts}
    for v in good:
        score[v["verdict"]] += weight(reps.get(v["judge"])) * v["confidence"]
    total = sum(score.values()) or 1.0
    verdict = max(score, key=score.get)
    return dict(verdict=verdict, agreement=round(score[verdict] / total, 2),
                score={k: round(x / total, 2) for k, x in score.items()}, votes=votes)


# ── the request ───────────────────────────────────────────────────────────
def start(session, mint: str, note: str = "") -> dict:
    mint = mint.strip()
    if not MINT_RE.match(mint):
        return {"error": "that is not a Solana token address"}
    if session.engine is None:
        return {"error": "bot not running"}
    ask = dict(id=uuid.uuid4().hex[:12], mint=mint, note=note[:200], ts=time.time(), status="gathering",
               symbol="", verdict=None)
    _save(session.settings, ask)
    session.asked_mints[mint] = time.time() + 1800          # follow it for 30 min so the data keeps coming
    threading.Thread(target=_run, args=(session, ask), name=f"ask-{ask['id']}", daemon=True).start()
    return ask


def _run(session, ask: dict) -> None:
    s = session.settings
    try:
        deadline = time.time() + WAIT_FOR_DATA_S
        while time.time() < deadline:
            st = session.engine.market.get(ask["mint"])
            if st is not None and st.price_sol and (st.dex or st.gmgn):
                break
            time.sleep(3)
        st = session.engine.market.get(ask["mint"])
        if st is None:
            ask.update(status="done", verdict="unknown", error="no data on this coin yet (not on DexScreener/GMGN?)")
            _save(s, ask)
            return
        text, facts = evidence(session, ask["mint"])
        ask.update(status="asking", symbol=st.symbol or "", evidence=text,
                   red_flags=facts["card"].get("red_flags"), green_flags=facts["card"].get("green_flags"),
                   strategies=facts["strategies"], price_sol=st.price_sol,
                   mcap_usd=(facts["card"].get("features") or {}).get("mcap_usd"))
        _save(s, ask)
        ask.update(judge(s, text + _context(session) + (f"\nThe trader's note: {ask['note']}" if ask.get("note") else "")))
        ask["status"] = "done"
    except Exception as e:  # noqa: BLE001
        ask.update(status="done", verdict="unknown", error=str(e)[:300])
    ask["done_ts"] = time.time()
    _save(s, ask)


# ── wallets: should the bot follow this wallet? ───────────────────────────
WALLET_SYSTEM = (
    "You are one judge advising a human trader on a single Solana wallet they want to follow. Decide whether the "
    "bot should track it (its buys become a signal and bigger bets), keep watching it, or avoid it. Skilled traders "
    "exit with profit across many coins; insiders, snipers, bundlers and dev wallets look good but only win on "
    "their own launches; a few lucky trades prove nothing. Reply with JSON only. Be terse, like notes; every reason "
    "at most 15 words; numbers stay exact."
)
WALLET_SCHEMA = '{"verdict": "track" | "watch" | "avoid", "confidence": 0.0-1.0, "reasons": ["..."], "risks": ["..."]}'


def _gmgn(settings, path: str, params: dict):
    """One GMGN call from the ask thread (the live session's client is bound to its own event loop)."""
    import httpx

    from solmeme.config import secret

    key = secret("GMGN_API_KEY")
    if not key:
        return None
    for attempt in range(2):                   # the feeds share the key: wait out a short 429, then go on without
        q = {"timestamp": int(time.time()), "client_id": uuid.uuid4().hex, "chain": "sol", **params}
        r = httpx.get("https://openapi.gmgn.ai" + path, params=q, timeout=20,
                      headers={"X-APIKEY": key, "Accept": "application/json", "User-Agent": "SolMemeTrader/0.1"})
        if r.status_code != 429:
            break
        time.sleep(4 * (attempt + 1))
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) else body


def _sightings(session, wallet: str, limit: int = 15) -> list[dict]:
    """Coins the bot saw this wallet buy, and what the price did since its first buy."""
    eng = session.engine
    out = []
    for st in list(eng.market.tokens.values()):
        t = st.first_buy_ts.get(wallet)
        if t is None:
            t = next((ts for ts, w, _ in st.wallet_buys if w == wallet), None)
        if t is None:
            continue
        then = st.price_at_or_before(t) if hasattr(st, "price_at_or_before") else None
        x = st.price_sol / then if then and st.price_sol else None
        sold = any(w == wallet for _, w, _ in st.wallet_sells)
        out.append(dict(symbol=st.symbol or st.mint[:6], mint=st.mint, bought_ago_min=round((eng.now - t) / 60),
                        secs_after_launch=round(t - st.age_ref_ts) if st.age_ref_ts else None,
                        x_since_buy=round(x, 2) if x else None, sold=sold, ath_x=round(st.ath_price_sol / then, 2)
                        if then and st.ath_price_sol else None))
    out.sort(key=lambda r: r["bought_ago_min"])
    return out[:limit]


def wallet_evidence(session, wallet: str) -> tuple[str, dict]:
    from solmeme.lab import my_wallets
    from solmeme.lab.people import STATS_PATH, judge as people_judge, parse_stats
    from solmeme.lab.whales import WhaleBook

    s = session.settings
    book = getattr(session, "whalebook", None) or WhaleBook(s.data_dir)
    whale = {k: v for k, v in (book.wallets.get(wallet) or {}).items() if k != "history"}
    bans = getattr(session, "banlist", None)
    ban = bans.bans.get(wallet) if bans is not None else None
    stats, recent, heur = {}, [], None
    try:
        stats = parse_stats(_gmgn(s, STATS_PATH, {"wallet_address": wallet, "period": "7d"}) or {}, wallet)
        heur = people_judge(stats)
    except Exception as e:  # noqa: BLE001 - one source down must not stop the ask
        stats = {"error": str(e)[:120]}
    try:
        act = _gmgn(s, "/v1/user/wallet_activity", {"wallet_address": wallet, "limit": 20, "type": ["buy", "sell"]}) or {}
        rows = (act.get("activities") or act.get("list") or []) if isinstance(act, dict) else act
        recent = [dict(side=r.get("event_type") or r.get("type"), symbol=(r.get("token") or {}).get("symbol") or r.get("symbol"),
                       usd=r.get("cost_usd") or r.get("amount_usd")) for r in rows[:15] if isinstance(r, dict)]
    except Exception:  # noqa: BLE001
        pass
    linked = []
    try:                                       # one hop of SOL transfers on a public node: capped, it can crawl
        from solmeme.lab.whales import chain_trace
        pool = ThreadPoolExecutor(max_workers=1)
        linked = pool.submit(chain_trace, wallet, set(session.engine.market.banned)).result(timeout=30)
        pool.shutdown(wait=False)
    except Exception:  # noqa: BLE001 - slow, refused or timed out: judged without it
        linked = None
    seen = _sightings(session, wallet)
    coins = coin_forensics(session, wallet, seen)
    common = stats.get("common") if isinstance(stats.get("common"), dict) else {}
    facts = dict(whale=whale, ban=dict(role=ban.role, reason=ban.reason) if ban else None, stats=stats,
                 heuristic=heur, recent=recent, linked_banned=linked, sightings=seen, coins=coins,
                 tracked=wallet in my_wallets.load(s.data_dir),
                 name=common.get("name") or common.get("twitter_username") or "")
    keep = {k: stats.get(k) for k in ("winrate", "pnl", "realized_profit", "buy_count", "sell_count", "token_num",
                                      "pnl_lt_minus_dot5_num", "pnl_gt_5x_num", "avg_holding_peroid") if stats.get(k) is not None}
    lines = [f"Wallet {wallet} {('(' + facts['name'] + ')') if facts['name'] else ''}",
             f"on our ban list: {json.dumps(facts['ban']) if ban else 'no'}",
             f"our whale book: {json.dumps(whale) if whale else 'not scored (never seen trading our coins enough)'}",
             f"GMGN 7d stats: {json.dumps(keep) if keep else stats.get('error', 'none')}",
             f"GMGN tags: {common.get('tags') or []}; created tokens: {common.get('created_token_count')}; "
             f"twitter: {common.get('twitter_username') or '-'}",
             f"simple rule check: {heur[0] + ' (' + '; '.join(heur[1]) + ')' if heur else 'n/a'}",
             f"recent GMGN activity: {json.dumps(recent[:10])}",
             f"money links to banned wallets (one hop): {linked if linked is not None else 'not checked'}",
             f"coins the bot saw it buy (newest first): {json.dumps(seen[:10]) if seen else 'none'}",
             f"forensics on those coins: {coins['summary']}",
             *[f"  {c['symbol']}: {c['line']}" for c in coins["rows"]]]
    return "\n".join(lines), facts


def start_wallet(session, address: str, note: str = "") -> dict:
    address = address.strip()
    if not MINT_RE.match(address):
        return {"error": "that is not a Solana wallet address"}
    if session.engine is None:
        return {"error": "bot not running"}
    ask = dict(id=uuid.uuid4().hex[:12], kind="wallet", mint=address, note=note[:200], ts=time.time(),
               status="gathering", symbol="", verdict=None)
    _save(session.settings, ask)
    threading.Thread(target=_run_wallet, args=(session, ask), name=f"askw-{ask['id']}", daemon=True).start()
    return ask


def _run_wallet(session, ask: dict) -> None:
    s = session.settings
    try:
        text, facts = wallet_evidence(session, ask["mint"])
        ask.update(status="asking", evidence=text, symbol=facts["name"], wallet=facts)
        _save(s, ask)
        ask.update(judge(s, text + (f"\nThe trader's note: {ask['note']}" if ask.get("note") else ""),
                         system=WALLET_SYSTEM, schema=WALLET_SCHEMA, verdicts=("track", "watch", "avoid")))
        ask["status"] = "done"
        if ask.get("verdict") == "track":                 # the council decides: a Track goes straight into your list
            from solmeme.lab import my_wallets
            got = my_wallets.add(s.data_dir, ask["mint"], ask.get("note", ""), "track", by="council", fingerprint=fingerprint(facts))
            ask["auto_added"] = "error" not in got
            if ask["auto_added"]:
                my_wallets.update(s.data_dir, ask["mint"], why=why_of(ask), name=facts.get("name") or "")
            if "error" in got:
                ask["auto_error"] = got["error"]
    except Exception as e:  # noqa: BLE001
        ask.update(status="done", verdict="unknown", error=str(e)[:300])
    ask["done_ts"] = time.time()
    _save(s, ask)


# ── tracked wallets: checked again every 4 hours ─────────────────────────
def fingerprint(facts: dict) -> dict:
    """The parts of a wallet's record that should send it back to the judges when they move."""
    w, st = facts.get("whale") or {}, facts.get("stats") or {}
    return dict(banned=bool(facts.get("ban")), linked=len(facts.get("linked_banned") or []),
                status=w.get("status"), council=w.get("council"), win=w.get("win"), trips=w.get("round_trips"),
                gmgn_win=st.get("winrate"), gmgn_pnl=st.get("pnl"))


def moved(old: dict, new: dict) -> list[str]:
    """What changed enough to ask the judges again (empty: nothing worth a Cursor call)."""
    out = []
    for k in ("status", "council"):
        if old.get(k) != new.get(k):
            out.append(f"whale book {k} {old.get(k)} → {new.get(k)}")
    for k, label, step in (("win", "whale-book win rate", 0.15), ("gmgn_win", "GMGN win rate", 0.15)):
        a, b = old.get(k), new.get(k)
        if a is not None and b is not None and abs(a - b) >= step:
            out.append(f"{label} {a:.0%} → {b:.0%}")
    a, b = old.get("gmgn_pnl"), new.get("gmgn_pnl")
    if a is not None and b is not None and (a > 0) != (b > 0):
        out.append(f"GMGN 7d pnl {a:+.2f} → {b:+.2f}")
    if (new.get("trips") or 0) - (old.get("trips") or 0) >= 5:
        out.append(f"{new.get('trips', 0) - (old.get('trips') or 0)} more round trips")
    return out


def recheck(session, address: str, judge_fn=None, now: float | None = None) -> dict:
    """One tracked wallet's 4-hourly check. Returns what happened (kept / removed, and why)."""
    from solmeme.lab import my_wallets

    s, now = session.settings, now or time.time()
    entry = my_wallets.load(s.data_dir).get(address)
    if entry is None:
        return {"result": "gone"}
    text, facts = wallet_evidence(session, address)
    fp = fingerprint(facts)
    if fp["banned"] or fp["linked"]:
        why = "on the ban list" if fp["banned"] else f"money links to {fp['linked']} banned wallet(s)"
        my_wallets.remove(s.data_dir, address, f"4-hour check: {why}", by="check")
        return {"result": "removed", "why": why}
    changes = moved(entry.get("fingerprint") or {}, fp)
    stale = now - (entry.get("judged_ts") or entry.get("ts") or 0) >= my_wallets.REJUDGE_S
    if not changes and not stale:
        my_wallets.update(s.data_dir, address, checked_ts=now, fingerprint=fp)
        return {"result": "kept", "why": "record unchanged (no Cursor call)"}
    why_ask = "; ".join(changes) or "daily re-judge"
    out = (judge_fn or judge)(s, text + f"\nThis wallet is already tracked. Re-check because: {why_ask}",
                              system=WALLET_SYSTEM, schema=WALLET_SCHEMA, verdicts=("track", "watch", "avoid"), max_judges=2)
    v = out.get("verdict")
    streak = (entry.get("watch_streak") or 0) + 1 if v == "watch" else 0
    rec = dict(id=uuid.uuid4().hex[:12], kind="wallet", recheck=True, mint=address, ts=now, done_ts=time.time(),
               status="done", symbol=facts.get("name") or "", evidence=text, wallet=facts, why_asked=why_ask, **out)
    _save(s, rec)
    if v == "avoid" or (v == "watch" and streak >= 2):
        why = "judges now say Avoid" if v == "avoid" else "judges said Watch twice in a row"
        my_wallets.remove(s.data_dir, address, f"4-hour check: {why} ({why_ask})", by="check")
        return {"result": "removed", "why": why, "verdict": v}
    my_wallets.update(s.data_dir, address, checked_ts=now, judged_ts=now, fingerprint=fp, watch_streak=streak,
                      verdict=v or entry.get("verdict", ""), why=why_of(rec) or entry.get("why") or [])
    return {"result": "kept", "why": f"judges: {v}", "verdict": v}


def start_rechecks(session, every_s: float = 600) -> None:
    """Background thread: every 10 minutes, check the tracked wallets whose 4 hours are up (one at a time)."""
    import logging

    from solmeme.lab import my_wallets

    log = logging.getLogger("solmeme.ask")

    def loop():
        time.sleep(120)
        while True:
            for address in my_wallets.due(session.settings.data_dir):
                try:
                    r = recheck(session, address)
                    log.warning("wallet check %s…: %s (%s)", address[:6], r.get("result"), r.get("why"))
                except Exception:  # noqa: BLE001 - try again next round
                    log.exception("wallet check %s failed", address[:6])
            time.sleep(every_s)

    threading.Thread(target=loop, name="wallet-rechecks", daemon=True).start()


def why_of(ask: dict, n: int = 4) -> list[str]:
    """The reasons of the judges who voted the winning verdict, one or two each."""
    v = ask.get("verdict")
    out = []
    for vote in ask.get("votes") or []:
        if vote.get("verdict") == v:
            out += [f"{vote['judge']}: {r}" for r in (vote.get("reasons") or [])[:2]]
    return out[:n]


def latest_wallet_ask(settings, address: str) -> dict | None:
    return next((a for a in list_asks(settings)["asks"] if a.get("kind") == "wallet" and a.get("mint") == address
                 and a.get("status") == "done"), None)


def wallet_detail(session, address: str) -> dict:
    """One tracked wallet for the dashboard: why it is tracked, and what it bought (with the price since)."""
    from solmeme.lab import my_wallets

    s = session.settings
    entry = my_wallets.load(s.data_dir).get(address) or {}
    last = latest_wallet_ask(s, address)
    why = entry.get("why") or (why_of(last) if last else [])
    m = session.engine.market if session.engine else None
    trades = []
    for t in my_wallets.trades_for(s.data_dir, address):
        st = m.get(t["mint"]) if m else None
        x = st.price_sol / t["price_sol"] if st and st.price_sol and t.get("price_sol") else None
        trades.append({**t, "symbol": t.get("symbol") or (st.symbol if st else "") or t["mint"][:6],
                       "x_since": round(x, 2) if x else None})
    recent = ((last or {}).get("wallet") or {}).get("recent") or []
    seen = _sightings(session, address) if session.engine else []
    return dict(address=address, entry=entry, why=why, verdict=entry.get("verdict") or (last or {}).get("verdict"),
                trades=trades, seen=seen, gmgn_recent=recent[:12], last_ask_ts=(last or {}).get("ts"),
                coins=coin_forensics(session, address, seen) if session.engine else None)


# ── live buys: the council reviews every coin the live wallet just bought ──
REVIEW_SYSTEM = (
    "You are one judge reviewing a meme coin the live wallet bought seconds ago. From the evidence (forensics on "
    "the creator, holders and early buyers, the narrative, how it is trading now) decide: hold it and let the "
    "strategy's exits run, or sell now because it is a trap (rug setup, insider or bundle exit, dead narrative). "
    "Selling costs fees, so only say sell on real red flags. Reply with JSON only. Be terse, like notes; every "
    "reason at most 15 words; numbers stay exact."
)
REVIEW_SCHEMA = '{"verdict": "hold" | "sell", "confidence": 0.0-1.0, "reasons": ["..."], "risks": ["..."]}'


def review_buy(session, item: dict, on_sell) -> None:
    """Background: judge one live buy; call on_sell(key, mint, why) when the judges clearly say sell."""
    s = session.settings
    cfg = s.dryrun
    day = time.strftime("%Y-%m-%d")
    counts = getattr(session, "_review_counts", {})
    if counts.get("day") != day:
        counts = {"day": day, "n": 0}
    if counts["n"] >= getattr(cfg, "council_reviews_per_day", 30):
        return
    counts["n"] += 1
    session._review_counts = counts

    def run():
        ask = dict(id=uuid.uuid4().hex[:12], kind="review", mint=item["mint"], ts=time.time(), status="asking",
                   symbol=item.get("symbol") or "", note=f"live bought it for {item.get('key', '')}", verdict=None)
        try:
            text, facts = evidence(session, item["mint"])
            st = session.engine.market.get(item["mint"])
            entry = float(item.get("price") or 0)
            now_x = st.price_sol / entry if st and st.price_sol and entry else None
            text += (f"\nLive position: bought by {item.get('key')} because {', '.join(item.get('why') or [])}; "
                     f"price now {now_x:.2f}x the entry." if now_x else "")
            ask.update(evidence=text, red_flags=facts["card"].get("red_flags"), green_flags=facts["card"].get("green_flags"),
                       strategies=facts["strategies"], mcap_usd=(facts["card"].get("features") or {}).get("mcap_usd"))
            ask.update(judge(s, text, system=REVIEW_SYSTEM, schema=REVIEW_SCHEMA, verdicts=("hold", "sell")))
            answered = sum(1 for v in ask.get("votes") or [] if "verdict" in v)
            sell = ask.get("verdict") == "sell" and (ask.get("agreement") or 0) >= 0.6 and answered >= 2
            ask["action"] = "sold" if sell else "held"
            if sell:
                on_sell(item["key"], item["mint"], "; ".join(why_of(ask, 2)) or "council: sell")
        except Exception as e:  # noqa: BLE001
            ask.update(verdict="unknown", error=str(e)[:300], action="held")
        ask.update(status="done", done_ts=time.time())
        _save(s, ask)

    threading.Thread(target=run, name=f"review-{item['mint'][:6]}", daemon=True).start()


# ── the council picks coins itself: whale clusters → buy / wait / avoid → the council_pick champion buys ──
PICK_RECHECK_S = 6 * 3600


def _passes_filter(session, mint: str) -> tuple[bool, str]:
    """The free global filter first (bans, rug checks, your socials rule): no Cursor call on a coin we'd never trade."""
    from solmeme.features.registry import FeatureView

    eng = session.engine
    st = eng.market.get(mint) if eng else None
    if st is None or not st.price_sol:
        return False, "no price yet"
    block = eng._passes_prefilter(FeatureView(st, eng.now or time.time(), eng.market))
    return block is None, (f"global filter: {block}" if block else "")


def pick(session, mint: str, why: str, judge_fn=None, emit=None) -> dict:
    """One candidate: free checks, then the council; a clear Buy becomes a council_pick signal."""
    from solmeme.core.events import ExternalSignal

    s = session.settings
    ok, block = _passes_filter(session, mint)
    st = session.engine.market.get(mint) if session.engine else None
    ask = dict(id=uuid.uuid4().hex[:12], kind="pick", mint=mint, ts=time.time(), status="asking",
               symbol=(st.symbol if st else "") or "", note=f"auto: {why}", verdict=None)
    follower = getattr(session, "pick_follower", None)
    if not ok:
        ask.update(status="done", verdict="skipped", action="skipped", error=f"not asked: {block}", done_ts=time.time())
        _save(s, ask)
        if follower is not None:                    # filtered coins are followed too: is the filter right?
            follower.start(ask, st.price_sol if st else None, why)
        return ask
    text, facts = evidence(session, mint)
    ask.update(evidence=text, red_flags=facts["card"].get("red_flags"), green_flags=facts["card"].get("green_flags"),
               strategies=facts["strategies"], mcap_usd=(facts["card"].get("features") or {}).get("mcap_usd"))
    ask.update((judge_fn or judge)(s, text + _context(session, lane_of(why))
                                   + f"\nWhy it came up: {why}. If you say buy, the live wallet buys it now."))
    answered = sum(1 for v in ask.get("votes") or [] if "verdict" in v)
    buy = ask.get("verdict") == "buy" and (ask.get("agreement") or 0) >= 0.6 and answered >= 2
    ask.update(status="done", action="bought" if buy else "passed", done_ts=time.time())
    if buy and emit is not None:
        emit(ExternalSignal(ts=time.time(), source="council_pick", mint=mint, signal="council_pick",
                            data={"verdict": "buy", "agreement": ask.get("agreement"), "why": why[:120], "pick_ts": time.time()}))
    _save(s, ask)
    if follower is not None:
        st = session.engine.market.get(mint) if session.engine else None
        follower.start(ask, st.price_sol if st else None, why)
    return ask


def _context(session, lane: str | None = None) -> str:
    """What the judges need to weigh a buy as a trade: how earlier picks moved, and what this trade would be."""
    try:
        rates, plan = base_rates(session.settings.data_dir, lane), trade_plan(session)
    except Exception:  # noqa: BLE001 - the question goes out without it
        return ""
    return "".join(f"\n\n{x}" for x in (rates, plan) if x)


EARLY_AGE_S = 180


def early_signals(st, now: float, m) -> list[str]:
    """The early-runner fast lane: what a coin under 3 minutes old already shows (SOBDOG, Oct 10: a book whale at
    +26 s, 53 holders at +50 s, a KOL at +47 s, callouts from +90 s; 18x by the time a 3-whale cluster formed)."""
    from solmeme.features.registry import FeatureView

    fv = FeatureView(st, now, m)
    out = []
    if (fv.get("whale_buys") or 0) >= 1:
        out.append(f"{fv.get('whale_buys')} whale(s) from the book bought")
    if (fv.get("kol_buys_600s") or 0) >= 1:
        out.append(f"{fv.get('kol_buys_600s')} KOL(s) bought")
    if (fv.get("pump_callouts") or 0) >= 1:
        out.append(f"{fv.get('pump_callouts')} pump.fun callout(s)")
    if (fv.get("gmgn_holders") or 0) >= 40:
        out.append(f"{int(fv.get('gmgn_holders'))} holders already")
    return out


def early_candidates(session, now: float, need: int = 2) -> list[tuple[str, str]]:
    eng, watch = session.engine, getattr(session, "watch", None)
    if eng is None or watch is None:
        return []
    out = []
    for mint in watch.priority_list(now)[:400]:
        st = eng.market.get(mint)
        if st is None or now - st.age_ref_ts > EARLY_AGE_S:
            continue
        try:
            sig = early_signals(st, now, eng.market)
        except RuntimeError:                       # the trading loop changed the coin meanwhile: next round
            continue
        if len(sig) >= need:
            out.append((mint, f"{int(now - st.age_ref_ts)} s old: " + ", ".join(sig)))
    return out


def start_picks(session, every_s: float = 10) -> None:
    """Background: early runners (the fast lane, first) and whale clusters go to the council (capped per day)."""
    import logging

    from solmeme.ui.launch import _whale_tape

    log = logging.getLogger("solmeme.ask")
    cfg = session.settings.council
    seen: dict[str, float] = {}

    def emit(ev):
        loop, ctx = getattr(session, "loop", None), getattr(session, "ctx", None)
        if loop is not None and ctx is not None:
            loop.call_soon_threadsafe(ctx.push, "council_pick", ev)   # the event queue belongs to the trading loop

    session.pick_follower = Follower(session.settings.data_dir)

    def loop():
        time.sleep(90)
        day, n, n_early = "", 0, 0
        while True:
            try:
                if session.engine is not None:
                    session.pick_follower.update(session.engine.market)
                today = time.strftime("%Y-%m-%d")
                if today != day:
                    day, n, n_early = today, 0, 0
                now = time.time()
                if getattr(cfg, "early_picks", True):
                    for mint, why in early_candidates(session, now):
                        if n_early >= getattr(cfg, "early_picks_per_day", 60) or now - seen.get(mint, 0) < PICK_RECHECK_S:
                            continue
                        seen[mint] = now
                        a = pick(session, mint, "early runner, " + why, emit=emit)
                        if a.get("action") != "skipped":
                            n_early += 1
                        log.warning("council early pick %s: %s (%s) %s", a.get("symbol"), a.get("action"), a.get("verdict"), why)
                        now = time.time()
                for c in _whale_tape(session).get("clusters") or []:
                    if n >= getattr(cfg, "picks_per_day", 40) or now - seen.get(c["mint"], 0) < PICK_RECHECK_S:
                        continue
                    seen[c["mint"]] = now
                    why = f"{c['whales']} whales from the book bought it in 10 min" + (f" ({c['trusted']} trusted)" if c.get("trusted") else "")
                    a = pick(session, c["mint"], why, emit=emit)
                    if a.get("action") != "skipped":
                        n += 1
                    log.warning("council pick %s: %s (%s)", c.get("symbol"), a.get("action"), a.get("verdict"))
            except Exception:  # noqa: BLE001 - next round
                log.exception("council picks failed")
            time.sleep(every_s)

    threading.Thread(target=loop, name="council-picks", daemon=True).start()


def coin_forensics(session, wallet: str, seen: list[dict], limit: int = 8) -> dict:
    """The same coin forensics the council uses on a coin, on each of the wallet's recent coins, plus the
    pattern: an insider's coins are its own launches, banned creators' coins, rugs and coins dead within hours."""
    from solmeme.lab import my_wallets
    from solmeme.ui.narrative_view import build_token

    s, eng = session.settings, session.engine
    mints = list(dict.fromkeys([r["mint"] for r in seen] +
                               [t["mint"] for t in my_wallets.trades_for(s.data_dir, wallet, 40) if t.get("side") == "buy"] +
                               [t["mint"] for t in my_wallets.whale_trades(s.data_dir) if t.get("wallet") == wallet and t.get("side") == "buy"]))
    rows, own, banned_creator, flagged, dead, ran = [], 0, 0, 0, 0, 0
    for mint in mints[:limit]:
        st = eng.market.get(mint) if eng else None
        if st is None:
            continue
        try:
            card = build_token(session, mint)
        except Exception:  # noqa: BLE001 - the trading loop changed the coin meanwhile
            continue
        f = card.get("features") or {}
        mine = bool(st.creator) and st.creator == wallet
        bad_dev = bool((card.get("creator") or {}).get("banned"))
        reds = card.get("red_flags") or []
        x_ath = st.price_sol / st.ath_price_sol if st.price_sol and st.ath_price_sol else None
        is_dead = x_ath is not None and x_ath <= 0.1
        sight = next((r for r in seen if r["mint"] == mint), {})
        own += mine; banned_creator += bad_dev; flagged += bool(reds); dead += is_dead
        ran += (sight.get("ath_x") or 0) >= 2
        bits = (["it created this coin itself"] if mine else []) + (["creator is on the ban list"] if bad_dev else []) + reds[:2]
        if f.get("dev_prior_rugs"):
            bits.append(f"creator's earlier coins: {f['dev_prior_rugs']} of {f.get('dev_prior_launches')} rugged")
        if x_ath is not None:
            bits.append(f"now {x_ath:.0%} of its high" + (" (dead)" if is_dead else ""))
        if sight.get("ath_x"):
            bits.append(f"peaked {sight['ath_x']}x after its buy")
        rows.append(dict(mint=mint, symbol=card.get("symbol") or mint[:6], own=mine, banned_creator=bad_dev,
                         red_flags=reds, dead=is_dead, line="; ".join(bits) or "no red flags"))
    n = len(rows)
    summary = (f"{n} of its recent coins checked: {own} it created itself, {banned_creator} by banned creators, "
               f"{flagged} with red flags, {dead} dead (≤10% of their high), {ran} ran 2x+ after its buy"
               if n else "none of its coins are in the bot's market data")
    return dict(rows=rows, summary=summary, own=own, banned_creator=banned_creator, flagged=flagged, dead=dead, ran=ran)

"""Narratives tab (themes, tweets, news) and the per-coin forensics card.

Both read the live MarketState from the web server's thread while the bot keeps writing to it,
so every read copies first and the caller retries on a "changed size during iteration".
"""

from __future__ import annotations

import time

from solmeme.features.narrative import token_words
from solmeme.features.registry import FEATURES


def retry(fn, tries: int = 4):
    for i in range(tries):
        try:
            return fn()
        except RuntimeError:            # a dict/deque changed while we read it
            if i == tries - 1:
                raise
            time.sleep(0.05)


def build_narratives(session) -> dict:
    eng = session.engine
    if eng is None:
        return {}
    m, now = eng.market, eng.now or time.time()
    nar = m.narratives
    return dict(
        now=now,
        themes=nar.themes(m, now, top=25),
        clusters=nar.clusters(m, now),
        tweets=nar.headlines("tweets"), ai=nar.headlines("ai"), news=nar.headlines("news"),
        counts=nar.source_counts(),
    )


def _feat(st, now, m, name):
    try:
        v = FEATURES[name].fn(st, now, m)
    except Exception:  # noqa: BLE001 - a missing input is just "unknown" here
        return None
    return v


def build_token(session, mint: str) -> dict:
    """Everything we know about one coin: who made it, who bought, red flags, the story behind it."""
    eng = session.engine
    if eng is None:
        return {"error": "bot not running"}
    m, now = eng.market, eng.now or time.time()
    st = m.get(mint)
    if st is None:
        return {"error": "not seen this session", "mint": mint}
    whales = set(m.whales)
    book = getattr(session, "whalebook", None)
    bans = getattr(session, "banlist", None)
    buys = list(st.wallet_buys)
    sells = list(st.wallet_sells)
    buyers = {}
    for t, w, label in buys:
        buyers.setdefault(w, dict(wallet=w, label=label, first_buy=t, whale=w in whales,
                                  banned=w in m.banned, sold=False))
    for _, w, _ in sells:
        if w in buyers:
            buyers[w]["sold"] = True
    ws = token_words(st.name or "", st.symbol or "")
    tweets = [h for h in m.narratives.all_recent() if set(h.get("words") or ()) & ws][-12:][::-1]
    names = ["mcap_usd", "age_s", "gmgn_holders", "gmgn_top10_pct", "gmgn_bundler_rate", "gmgn_insider_rate",
             "gmgn_dev_hold_pct", "gmgn_rug_ratio", "gmgn_sniper_count", "dev_sold_pct", "dex_buy_ratio_m5",
             "vol_mcap_1h", "dd_from_ath_pct", "x_from_first_price", "kol_buys", "whale_buys", "banned_wallets",
             "tweet_match_30m", "tweet_match_followers", "ai_ticker_match", "news_match_6h", "theme_launches_1h",
             "ca_tweeted", "j7_launched", "gecko_trending", "dex_paid", "hard_rug", "og_rank", "copies_after", "is_og",
             # our own forensics: the creator's history, the mint on chain, links the coin really owns
             "dev_prior_launches", "dev_prior_rugs", "dev_launch_pct", "chain_rug_flags", "chain_freeze_authority",
             "chain_top10_pct", "owned_socials_count", "secs_since_trusted_price",
             "pump_trending_rank", "pump_trending_kol", "pump_callouts", "pump_callout_leader", "pump_callout_mcap_x",
             "pump_holder_reward", "pump_cashback", "pump_mayhem", "pump_charity", "pump_live"]
    feats = {n: _feat(st, now, m, n) for n in names}
    flags = []
    if st.banned_hits:
        flags.append(f"{len(st.banned_hits)} banned wallet(s) created or bought it")
    if st.creator and st.creator in m.banned:
        flags.append("the creator is on the ban list")
    if feats.get("hard_rug"):
        flags.append("hard rug check failed (honeypot / extreme concentration / dev hoard)")
    if (feats.get("dev_prior_rugs") or 0) >= 2:
        flags.append(f"its creator's earlier coins: {feats['dev_prior_rugs']} of {feats.get('dev_prior_launches')} now 90%+ down")
    if feats.get("chain_freeze_authority"):
        flags.append("on chain: the creator can freeze holders' tokens (honeypot setup)")
    if feats.get("ca_tweeted"):
        flags.append("its contract address was tweeted: 3% of such trades won so far")
    if (feats.get("vol_mcap_1h") or 0) >= 3:
        flags.append("1h volume is 3x+ its market cap (churn; our worst bucket)")
    mc = feats.get("mcap_usd") or 0
    if 40_000 <= mc <= 70_000:
        flags.append("$40-70k market cap: our weakest zone")
    if (feats.get("og_rank") or 0) >= 2:
        og = m.narratives.og_info(mint, st.name or "", st.symbol or "", now)
        flags.append(f"a copy: launch #{feats['og_rank']} of this ticker/name" + (f" (original {og[2][:6]}…)" if og else ""))
    good = []
    calls = sorted((st.ext.get("pump_callouts") or {}).values(), key=lambda c: c["ts"])
    if calls:
        lead = [c for c in calls if c.get("caller_rank")]
        good.append(f"{len(calls)} pump.fun callout(s)" + (f", {len(lead)} by leaderboard traders" if lead else "")
                    + (f"; first thesis: \"{calls[0].get('thesis')}\"" if calls[0].get("thesis") else ""))
    if feats.get("pump_trending_rank"):
        good.append(f"#{feats['pump_trending_rank']} on pump.fun Trending ({feats.get('pump_trending_kol') or 0} KOLs in)")
    if feats.get("is_og"):
        good.append(f"the original: {feats.get('copies_after')} copycat(s) launched after it")
    if feats.get("whale_buys"):
        good.append(f"{feats['whale_buys']} whale(s) bought in the last 10 min")
    if feats.get("ai_ticker_match"):
        good.append("j7tracker's AI predicted this coin from a tweet")
    if feats.get("tweet_match_30m"):
        good.append(f"matches {feats['tweet_match_30m']} recent tweet(s)")
    if feats.get("news_match_6h"):
        good.append(f"matches {feats['news_match_6h']} news headline(s)")
    creator = None
    if st.creator:
        ban = bans.bans.get(st.creator) if bans is not None else None
        creator = dict(wallet=st.creator, banned=st.creator in m.banned, ban_reason=getattr(ban, "reason", ""),
                       whale=(book.wallets.get(st.creator) or {}).get("status", "") if book is not None else "")
    return dict(
        mint=mint, symbol=st.symbol, name=st.name, pool=st.pool, migrated=st.migrated,
        created_ts=st.created_ts, first_seen_ts=st.first_seen_ts, price_sol=st.price_sol,
        sol_usd=m.sol_usd, features=feats, red_flags=flags, green_flags=good, creator=creator,
        buyers=sorted(buyers.values(), key=lambda b: b["first_buy"])[:40],
        banned_hits=sorted(st.banned_hits), signals=sorted(st.signals), words=sorted(ws), story=tweets,
        links=dict(gmgn=f"https://gmgn.ai/sol/token/{mint}", dexscreener=f"https://dexscreener.com/solana/{mint}",
                   solscan=f"https://solscan.io/token/{mint}"),
    )


def build_trades(session, limit: int = 200) -> dict:
    """The trade tape (every buy, sell and missed buy, newest first) and the coins several agents hold."""
    eng = session.engine
    if eng is None:
        return {}
    tape = list(eng.tape)[-limit:][::-1]
    held: dict[str, dict] = {}
    for r in list(eng.runners):
        for pos in list(r.portfolio.positions.values()):
            if pos.tokens <= 0:
                continue
            st = eng.market.get(pos.mint)
            x = st.price_sol / pos.entry_price if st is not None and st.price_sol and pos.entry_price else None
            c = held.setdefault(pos.mint, dict(mint=pos.mint, symbol=pos.symbol or (st.symbol if st else ""), holders=[]))
            c["holders"].append(dict(key=r.key, role=r.label, x=x, entry_ts=pos.entry_ts))
    crowded = sorted((c for c in held.values() if len(c["holders"]) >= 2),
                     key=lambda c: (-len(c["holders"]), -max(h["entry_ts"] for h in c["holders"])))
    day = [t for t in list(eng.tape) if t["side"] == "sell" and "pnl_sol" in t]
    return dict(now=eng.now, tape=tape, crowded=crowded[:20],
                stats=dict(buys=sum(1 for t in tape if t["side"] == "buy"), closed=len(day),
                           won=sum(1 for t in day if t["pnl_sol"] > 0), pnl_sol=sum(t["pnl_sol"] for t in day)))

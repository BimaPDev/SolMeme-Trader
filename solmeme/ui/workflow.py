"""The three working roles behind the live wallet, built from what the bot already computes:

  ResearchBot  research_feed  every coin a strategy bought or nearly bought gets a brief: wallets (creator history,
                              banned, whales/KOLs in), holders (count, top 10, growth), liquidity (pool, and what a
                              round trip at our size costs) and a green / yellow / red flag. No AI call.
  SniperBot    setups         coins one condition away from a champion's entry: what it is waiting for (value now vs
                              needed) and the trade ticket that fires when it hits (size, slippage limit, exit plan).
  TraderBot    permissions    what the live wallet may do, set by you on the dashboard: which strategies may trade it,
                              max bet, max open positions, trades per day, council picks / early lane on or off.
                              The dry run checks them before every buy (allowed()).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

PERMISSIONS_FILE = "trader_permissions.json"
DEFAULT_PERMISSIONS = dict(strategies={}, max_bet_usd=15.0, max_open=4, max_trades_per_day=40, council_picks=True)


# ── TraderBot: permissions ────────────────────────────────────────────────
def load_permissions(data_dir: Path) -> dict:
    try:
        p = json.loads((Path(data_dir) / "session" / PERMISSIONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        p = {}
    return {**DEFAULT_PERMISSIONS, **p, "strategies": {**DEFAULT_PERMISSIONS["strategies"], **(p.get("strategies") or {})}}


def save_permissions(data_dir: Path, changes: dict) -> dict:
    cur = load_permissions(data_dir)
    for k in ("max_bet_usd", "max_open", "max_trades_per_day"):
        if k in changes and changes[k] is not None:
            cur[k] = max(0.0, float(changes[k])) if k == "max_bet_usd" else max(0, int(changes[k]))
    if "council_picks" in changes:
        cur["council_picks"] = bool(changes["council_picks"])
    for sid, on in (changes.get("strategies") or {}).items():
        cur["strategies"][str(sid)] = bool(on)
    path = Path(data_dir) / "session" / PERMISSIONS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    return cur


def allowed(perm: dict, key: str, size_sol: float, sol_usd: float | None, open_n: int, trades_today: int) -> str | None:
    """None when TraderBot may buy, else why not (your permissions)."""
    sid = key.split("@")[0]
    if perm["strategies"].get(sid) is False:
        return f"{sid} is not allowed to trade the live wallet (your setting)"
    if sid == "council_pick" and not perm.get("council_picks", True):
        return "council picks are switched off (your setting)"
    if open_n >= perm["max_open"]:
        return f"{open_n} positions open (your max {perm['max_open']})"
    if trades_today >= perm["max_trades_per_day"]:
        return f"{trades_today} live trades today (your max {perm['max_trades_per_day']})"
    return None


def cap_size(perm: dict, size_sol: float, sol_usd: float | None) -> float:
    """Your max bet per trade."""
    if sol_usd and perm.get("max_bet_usd"):
        return min(size_sol, perm["max_bet_usd"] / sol_usd)
    return size_sol


# ── ResearchBot: a brief on every strategy match ──────────────────────────
def brief(session, mint: str, now: float, size_sol: float = 0.1) -> dict | None:
    from solmeme.features.registry import FeatureView

    eng = session.engine
    st = eng.market.get(mint) if eng else None
    if st is None or not st.price_sol:
        return None
    m = eng.market
    fv = FeatureView(st, now, m)
    g = lambda k: fv.get(k)                                     # noqa: E731
    holders, top10 = g("gmgn_holders"), g("gmgn_top10_pct")
    if holders is None:                                         # GMGN missing: count from the live trade stream
        holders, top10 = g("holders_est"), top10 if top10 is not None else g("top10_pct")
    liq = st.dex.get("liquidity_usd") or st.gmgn.get("liquidity_usd") or st.gmgn.get("liquidity")
    rt = None                                                   # what a buy-then-sell of our size costs right now
    try:
        q = eng.fills.quote_buy(st, size_sol)
        if q and q[0] > 0:
            back = eng.fills.quote_sell(st, q[0])
            rt = 100 * (1 - back[0] / size_sol) if back else None
    except Exception:  # noqa: BLE001
        pass
    wallets = dict(creator=st.creator[:6] + "…" if st.creator else None, creator_banned=bool(st.creator and st.creator in m.banned),
                   dev_chain_launches=g("dev_chain_launches"), dev_chain_grad_pct=g("dev_chain_grad_rate"), dev_wallet_age_h=g("dev_wallet_age_h"),
                   dev_prior_launches=g("dev_prior_launches"), dev_prior_rugs=g("dev_prior_rugs"), dev_sold_pct=g("dev_sold_pct"),
                   banned_in=g("banned_wallets"), whales_in=g("whale_buys"), whales_selling=g("whale_sells"), kols_in=g("kol_buys_600s"))
    red, yellow, green = [], [], []
    if wallets["creator_banned"] or (wallets["banned_in"] or 0) >= 1:
        red.append("banned wallets involved")
    if g("hard_rug"):
        red.append("hard rug check failed")
    if (wallets["dev_prior_rugs"] or 0) >= 2:
        red.append(f"creator rugged {wallets['dev_prior_rugs']} earlier coins")
    if top10 is not None and top10 > 50:
        red.append(f"top 10 hold {top10:.0f}%")
    elif top10 is not None and top10 > 30:
        yellow.append(f"top 10 hold {top10:.0f}%")
    if rt is not None and rt > 25:
        red.append(f"a round trip at our size costs {rt:.0f}%")
    elif rt is not None and rt > 12:
        yellow.append(f"round trip costs {rt:.0f}%")
    if liq is not None and liq < 5000:
        yellow.append(f"thin liquidity ${liq:,.0f}")
    if (wallets["dev_sold_pct"] or 0) > 50:
        yellow.append(f"dev sold {wallets['dev_sold_pct']:.0f}%")
    if holders is None and top10 is None:                       # unchecked is not the same as clean
        yellow.append("no holder data yet")
    if not st.creator:
        yellow.append("creator unknown: dev history not checked")
    if (wallets["whales_in"] or 0) >= 1:
        green.append(f"{wallets['whales_in']} whale(s) in")
    if (wallets["kols_in"] or 0) >= 1:
        green.append(f"{wallets['kols_in']} KOL(s) in")
    if holders and holders >= 100:
        green.append(f"{int(holders)} holders")
    n_dev, grad = g("dev_chain_launches"), g("dev_chain_grad_rate")      # from the chain (crawl-hive's idea)
    if n_dev is not None and n_dev >= 10 and (grad or 0) < 10:
        red.append(f"coin factory: the creator launched {int(n_dev)} coins, {grad or 0:.0f}% graduated")
    elif n_dev is not None and n_dev >= 3 and (grad or 0) >= 30:
        green.append(f"creator's record: {grad:.0f}% of {int(n_dev)} launches graduated")
    age_h = g("dev_wallet_age_h")
    if age_h is not None and age_h < 24:
        yellow.append(f"creator wallet only {age_h:.0f} h old")
    linked, lpct, fresh = g("linked_wallets"), g("linked_pct"), g("fresh_holder_pct")
    if linked is not None and (linked >= 5 or (lpct or 0) >= 10):
        red.append(f"bundle: {int(linked)} top holders funded by one wallet ({lpct or 0:.1f}% of supply)")
    elif linked is not None and linked >= 3:
        yellow.append(f"{int(linked)} top holders share a funder")
    if fresh is not None and fresh >= 15:
        yellow.append(f"{fresh:.0f}% held by wallets under 3 days old")
    if g("pump_callouts"):
        green.append(f"{g('pump_callouts')} pump.fun callout(s)")
    flag = "red" if red else "yellow" if yellow else "green"
    return dict(mint=mint, symbol=st.symbol or mint[:6], age_s=round(now - st.age_ref_ts),
                mcap_usd=st.mcap_sol * m.sol_usd if st.mcap_sol and m.sol_usd else None,
                wallets=wallets, holders=dict(count=holders, top10_pct=top10, sniper_count=g("gmgn_sniper_count"),
                                              bundler_rate=g("gmgn_bundler_rate"), linked_wallets=linked, linked_pct=lpct,
                                              fresh_pct=fresh),
                liquidity=dict(pool_usd=liq, round_trip_pct=round(rt, 1) if rt is not None else None, migrated=st.migrated),
                flag=flag, red=red, yellow=yellow, green=green)


def research_feed(session, limit: int = 40, window_s: float = 3600) -> list[dict]:
    """Every coin a strategy bought or nearly bought in the last hour, newest first, with its brief."""
    eng = session.engine
    if eng is None:
        return []
    now = eng.now or time.time()
    roles = {r.key: r.label for r in eng.runners}
    seen: dict[str, dict] = {}
    for t in reversed(list(eng.tape)):
        if t.get("side") == "buy" and now - t.get("ts", 0) <= window_s:
            seen.setdefault(t["mint"], dict(ts=t["ts"], match="bought", by=[]))["by"].append(t.get("key", ""))
    for n in reversed([*eng.journal.near_misses[-2000:], *eng.live_near_misses()]):
        if now - n.get("ts", 0) <= window_s:
            key = f"{n['strategy_id']}@{n['version']}"
            seen.setdefault(n["mint"], dict(ts=n["ts"], match="nearly", by=[]))["by"].append(key)
    out = []
    for mint, s in sorted(seen.items(), key=lambda kv: -kv[1]["ts"])[:limit]:
        try:
            b = brief(session, mint, now)
        except RuntimeError:                       # the trading loop changed the coin meanwhile
            continue
        if b:
            by = list(dict.fromkeys(s["by"]))
            out.append(dict(b, ts=s["ts"], match=s["match"], by=[k.split("@")[0] for k in by][:4],
                            champion=any(roles.get(k) == "champion" for k in by)))
    return out


# ── SniperBot: setups one condition away ──────────────────────────────────
def setups(session, window_s: float = 1800, limit: int = 30) -> list[dict]:
    """Champion near-misses still alive: which conditions are missing (value now vs needed), and the ticket."""
    from solmeme.features.registry import FeatureView
    from solmeme.strategy.dsl import evaluate

    eng = session.engine
    if eng is None:
        return []
    now = eng.now or time.time()
    champs = {r.key: r for r in eng.runners if r.label == "champion"}
    perm = load_permissions(session.settings.data_dir)
    px = eng.market.sol_usd
    out, done = [], set()
    for n in reversed(eng.live_near_misses()):        # still being followed: the journal gets them 30 min late
        key = f"{n['strategy_id']}@{n['version']}"
        r = champs.get(key)
        if r is None or now - n.get("ts", 0) > window_s or (key, n["mint"]) in done:
            continue
        done.add((key, n["mint"]))
        st = eng.market.get(n["mint"])
        if st is None or n["mint"] in r.portfolio.positions:
            continue
        try:
            fv = FeatureView(st, now, eng.market)
            ok, failed = evaluate(r.spec, fv, explain=True)
        except RuntimeError:
            continue
        rules = {c.id: c for c in [*r.spec.entry.all, *r.spec.entry.none]}
        waiting = []
        for rid in failed:
            if rid == "score":
                from solmeme.strategy.dsl import score
                waiting.append(dict(rule="score", feature="confidence score", op=">=",
                                    need=r.spec.p(r.spec.entry.min_score), now=round(score(r.spec, fv), 2)))
                continue
            c = rules.get(rid)
            if c is None:
                waiting.append(dict(rule=rid))
                continue
            need = r.spec.p(c.v)
            waiting.append(dict(rule=rid, feature=c.f, op=("not " if c in r.spec.entry.none else "") + c.op,
                                need=need, now=fv.get(c.f)))
        if not failed and not ok:
            continue
        ex, p = r.spec.exits, r.spec.p
        size = min(eng._size(r, 1.0) if hasattr(eng, "_size") else 0.1, (perm["max_bet_usd"] / px) if px else 1e9)
        ticket = dict(size_sol=round(size, 4), size_usd=round(size * px, 2) if px else None,
                      max_slippage_pct=session.settings.dryrun.slippage_tolerance_pct,
                      take_profit=[f"{lv.sell_pct:g}% at {float(p(lv.at_x)):g}x" for lv in ex.take_profit],
                      stop_pct=float(p(ex.stop_loss_pct)) if ex.stop_loss_pct is not None else None,
                      live_allowed=perm["strategies"].get(r.spec.id, True))
        out.append(dict(mint=n["mint"], symbol=st.symbol or n["mint"][:6], strategy=r.spec.id, since=n["ts"],
                        status="ready" if ok else "waiting", waiting=waiting, ticket=ticket,
                        mcap_usd=st.mcap_sol * px if st.mcap_sol and px else None))
        if len(out) >= limit:
            break
    return out

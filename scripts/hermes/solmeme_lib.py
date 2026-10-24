"""SolMeme alerts for Hermes cron (no_agent jobs): read-only checks of the SolMeme dashboard API.

Hermes delivers a job's stdout to Telegram verbatim; empty stdout sends nothing. No AI is involved.
Stdlib only. State lives in ~/.hermes/scripts/.solmeme_alerts_state.json so each alert fires once and
re-arms after it clears (no spam when a number hovers around a line).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.request

BASE = os.environ.get("SOLMEME_URL", "http://192.168.50.211")
STATE = os.environ.get("SOLMEME_ALERT_STATE", os.path.expanduser("~/.hermes/scripts/.solmeme_alerts_state.json"))

DROPS = (-15, -25, -40)            # % from the $50 start
GAINS = (50, 100, 200)
SWING_PCT, SWING_COOLDOWN_S = 10, 3600          # wallet moved this much within 30 minutes
DOWN_AFTER_FAILS, DOWN_REMIND_S = 2, 3 * 3600    # two failed checks (~10 min), then remind every 3 h
IDLE_S, FEED_STALL_S, GMGN_BACKOFF_S, GEN_LONG_S = 5400, 600, 3600, 3600
HIGH_COOLDOWN_S, HIGH_STEP = 3 * 3600, 1.05
CHECKPOINTS = {"2026-10-09": "24-hour", "2026-10-11": "3-day", "2026-10-15": "1-week", "2026-10-22": "2-week"}


def get(path: str, timeout: float = 15.0):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.load(r)


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def usd(x) -> str:
    return "—" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def sgn_usd(x) -> str:
    return "—" if x is None else f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def pct(x, signed=True) -> str:
    return "—" if x is None else f"{'+' if signed and x >= 0 else ''}{x:.0f}%"


def short(key: str) -> str:
    sid, _, v = key.partition("@")
    return f"{sid} {v[:7]}".strip()


def _wallet_line(w: dict) -> str:
    return (f"Wallet {usd(w['balance_usd'])} ({pct(w.get('pnl_pct'))} vs {usd(w['start_usd'])}) · "
            f"locked in {sgn_usd(w.get('locked_usd'))}, open {sgn_usd(w.get('open_usd'))}")


def _champions(snap: dict) -> str:
    rows = [r for r in snap.get("strategies", []) if r["role"] == "champion" and (r["closed"] or r["open"])]
    return "; ".join(f"{short(r['key'])}: {r['closed']} closed, {r['open']} open, {r['pnl_sol']:+.3f} SOL" for r in rows) or "no trades yet"


# ── alerts (every 5 min) ──────────────────────────────────────────────────────
def check_down(st: dict, now: float, ok: bool) -> list[str]:
    msgs = []
    if ok:
        if st.get("down"):
            mins = (now - st.get("first_fail_ts", now)) / 60
            msgs.append(f"🟢 SolMeme is answering again (was unreachable ~{mins:.0f} min).")
        st.update(fails=0, down=False)
        return msgs
    st["fails"] = st.get("fails", 0) + 1
    if st["fails"] == 1:
        st["first_fail_ts"] = now
    if st["fails"] >= DOWN_AFTER_FAILS and not st.get("down"):
        st.update(down=True, last_down_msg=now)
        msgs.append("🔴 SolMeme bot is not responding (~10 min). Open the dashboard http://the bot host or run "
                    "`systemctl status solmeme` on the bot host. The wallet and open positions are saved and carry over a restart.")
    elif st.get("down") and now - st.get("last_down_msg", 0) > DOWN_REMIND_S:
        st["last_down_msg"] = now
        msgs.append(f"🔴 SolMeme still not responding ({(now - st['first_fail_ts']) / 3600:.1f} h).")
    return msgs


def check_snapshot(snap: dict, hist: list | None, st: dict, now: float) -> list[str]:
    msgs: list[str] = []
    w = snap.get("wallet") or {}
    up = float(snap.get("uptime_s") or 0)

    # restarted? uptime should have grown by the time since the last check
    if st.get("last_check_ts") is not None:
        expected = st.get("last_uptime", 0) + (now - st["last_check_ts"])
        if up < expected - 180:
            msgs.append(f"🔄 SolMeme restarted ({up / 60:.0f} min ago). {_wallet_line(w) if w else ''} "
                        f"The wallet and open positions carry over; buying resumes after a 2-minute warm-up.")
    st.update(last_check_ts=now, last_uptime=up)

    if w and w.get("pnl_pct") is not None:
        p, hit = w["pnl_pct"], st.setdefault("hit", {})
        for lvl in DROPS:
            k = f"d{lvl}"
            if p <= lvl and not hit.get(k):
                hit[k] = True
                icon = "🟠" if lvl > -25 else "🔴"
                msgs.append(f"{icon} Wallet is down {abs(lvl)}% from the start. {_wallet_line(w)}. "
                            f"Low {usd(w['range']['low']) if w.get('range') else '—'}. Champions: {_champions(snap)}")
            elif p > lvl + 5:
                hit[k] = False
        for lvl in GAINS:
            k = f"g{lvl}"
            if p >= lvl and not hit.get(k):
                hit[k] = True
                msgs.append(f"🎉 Wallet is up {lvl}% from the start! {_wallet_line(w)}. Champions: {_champions(snap)}")
            elif p < lvl - 10:
                hit[k] = False
        bal = w["balance_usd"]
        high = st.get("high_usd") or max(w["start_usd"] or 0, bal)
        if bal > high * HIGH_STEP and bal > (w["start_usd"] or 0) * 1.2 and now - st.get("last_high_msg", 0) > HIGH_COOLDOWN_S:
            st["last_high_msg"] = now
            msgs.append(f"🏆 New high: {usd(bal)} (previous {usd(high)}). Locked in {sgn_usd(w.get('locked_usd'))}, open {sgn_usd(w.get('open_usd'))}.")
        st["high_usd"] = max(high, bal)

    # sharp 30-minute move
    pts = [r for r in (hist or []) if r[1] is not None and r[0] >= now - 1800]
    if len(pts) >= 5 and now - st.get("last_swing", 0) > SWING_COOLDOWN_S:
        chg = (pts[-1][1] / pts[0][1] - 1) * 100 if pts[0][1] else 0
        if abs(chg) >= SWING_PCT:
            st["last_swing"] = now
            msgs.append(f"{'📈' if chg > 0 else '📉'} Wallet moved {chg:+.0f}% in 30 min ({usd(pts[0][1])} → {usd(pts[-1][1])}). "
                        f"Open positions are valued at market, so part of this may not be locked in.")

    # nothing closing anywhere
    total = sum(r["closed"] for r in snap.get("strategies", []))
    if total != st.get("closed_total"):
        st.update(closed_total=total, closed_ts=now, idle_alerted=False)
    elif now - st.get("closed_ts", now) > IDLE_S and up > IDLE_S and not st.get("idle_alerted"):
        st["idle_alerted"] = True
        msgs.append(f"🟡 No trades have closed for {(now - st['closed_ts']) / 3600:.1f} h. Often a quiet market or GMGN rate limits; "
                    f"check the Sources tab if it lasts.")

    # data feeds
    srcs = {s["name"]: s for s in snap.get("sources", [])}
    feeds = st.setdefault("feeds", {})
    for name in ("dexscreener", "pumpportal"):
        s = srcs.get(name)
        if not s:
            continue
        f = feeds.setdefault(name, {"events": s["events"], "since": now, "alerted": False})
        if s["events"] != f["events"]:
            f.update(events=s["events"], since=now, alerted=False)
        elif now - f["since"] > FEED_STALL_S and not f["alerted"] and up > FEED_STALL_S:
            f["alerted"] = True
            msgs.append(f"🟡 Data feed '{name}' has received nothing for {(now - f['since']) / 60:.0f} min.")
    g = srcs.get("gmgn")
    if g and g["status"] == "backoff":
        st.setdefault("gmgn_since", now)
        if now - st["gmgn_since"] > GMGN_BACKOFF_S and not st.get("gmgn_alerted"):
            st["gmgn_alerted"] = True
            msgs.append(f"🟡 GMGN has been rate-limited for {(now - st['gmgn_since']) / 3600:.1f} h. Holder counts go stale, so "
                        f"holder_dip may stop buying.")
    else:
        st.update(gmgn_since=None, gmgn_alerted=False)
        st.pop("gmgn_since", None)

    # swarm generation taking too long
    sc = snap.get("swarm_clock") or {}
    if sc.get("running") and sc.get("started_ts") and now - sc["started_ts"] > GEN_LONG_S and st.get("gen_alert") != sc["started_ts"]:
        st["gen_alert"] = sc["started_ts"]
        msgs.append(f"🟡 A swarm generation has been running {(now - sc['started_ts']) / 60:.0f} min (phase: {sc.get('phase')}). "
                    + ("The council is slow when Cursor's daily share is spent and the backup judge (9router) "
                       "answers instead. Not urgent." if sc.get("phase") == "council" else "Not urgent."))
    return msgs


# ── digest (twice a day) ──────────────────────────────────────────────────────
def digest(snap: dict, swarm: dict | None, st: dict, now: float, when: dt.datetime) -> str:
    w = snap.get("wallet") or {}
    d = st.get("digest") or {}
    lines = [f"📊 SolMeme · {when.strftime('%a %b %-d, %-I:%M %p')}", ""]
    if w:
        lines.append(_wallet_line(w))
        if w.get("range"):
            lines.append(f"Range {usd(w['range']['low'])} – {usd(w['range']['high'])} · goal {usd(w.get('goal_usd'))}")
        if d.get("wallet") is not None:
            lines.append(f"Since last digest ({(now - d['ts']) / 3600:.0f} h ago): {sgn_usd(w['balance_usd'] - d['wallet'])}")
    lines += ["", "Champions: " + _champions(snap)]
    best = (swarm or {}).get("best") or []
    if best:
        lines.append("Best performing: " + "; ".join(
            f"{short(b['key'])} {b['equity'] * 100:.0f}% ({b['trades']} trades)" for b in best[:3]))
    agents = (swarm or {}).get("agents") or []
    if agents:
        alive = sum(a["status"] == "swarm" for a in agents)
        dead = sum(a["status"] == "dead" for a in agents)
        lines.append(f"Swarm: {alive} alive, {dead} dead, generation {(swarm or {}).get('generation')}")
    bans = snap.get("bans") or {}
    whales = [x for x in (snap.get("whales") or []) if x["status"] == "whale"]
    lines.append(f"Bans {bans.get('count', '—')} · whales {len(whales)} · open positions {sum(r['open'] for r in snap.get('strategies', []))}")
    bad = [f"{s['name']} {s['status']}" for s in snap.get("sources", []) if s["status"] not in ("ok", "disabled") and s["name"] != "gmgn"]
    g = next((s for s in snap.get("sources", []) if s["name"] == "gmgn"), None)
    if g and g["status"] != "ok":
        bad.append(f"gmgn {g['status']}")
    lines.append("Sources: " + ("all ok" if not bad else ", ".join(bad)))
    lines.append(f"Uptime {float(snap.get('uptime_s') or 0) / 3600:.1f} h · SOL {usd(snap.get('sol_usd'))}")
    cp = CHECKPOINTS.get(when.strftime("%Y-%m-%d"))
    if cp and when.hour >= 18 and w:
        closed = sum(r["closed"] for r in snap.get("strategies", []) if r["role"] == "champion")
        lines += ["", f"✅ CHECKPOINT: {cp}. Log row for MONITORING.md:",
                  f"{when.strftime('%b %-d')} | SOL {usd(snap.get('sol_usd'))} | wallet {usd(w['balance_usd'])} | locked {sgn_usd(w.get('locked_usd'))} | "
                  f"open {sgn_usd(w.get('open_usd'))} | champion trades {closed} | worst dip {usd((w.get('range') or {}).get('low'))} | "
                  f"swarm alive {sum(a['status'] == 'swarm' for a in agents)}",
                  "Ask Claude to \"review the test run\" for the full analysis."]
    st["digest"] = {"ts": now, "wallet": w.get("balance_usd")}
    return "\n".join(lines)


# ── generation report (from the 5-min alerts job, once per finished generation) ─────────
def _op(sig: str) -> str:
    """A mutation signature in plain words."""
    kind, _, rest = sig.partition(":")
    feat = rest.split(":")[0]
    return {
        "lesson": f"learned rule on {feat}", "idea": "council idea", "winner_rule": "copied what winners share",
        "crossover": "mixed two parents", "remove_rule": f"dropped rule {feat}", "add_rule": f"new rule on {feat}",
        "rule_value": f"retuned {feat}", "param": f"tuned {feat} {'up' if rest.endswith('+') else 'down'}",
        "exit": "changed exits", "take_profit": "changed take-profit", "adopt_exits": "copied a winner's exits",
        "sizing_pct": f"bet size {'up' if rest.endswith('+') else 'down'}",
        "boost": "whale bet boost" + ("" if rest == "add" else " dropped" if rest == "drop" else " retuned"),
    }.get(kind, sig)


def _rule(i: dict) -> str:
    verb = "buy only when" if i.get("kind") == "require" else "don't buy when"
    v = i.get("v")
    return f"{verb} {i.get('feature')} {i.get('op')} {v:g}" if isinstance(v, (int, float)) else f"{verb} {i.get('feature')}"


def generation_report(gen: int, swarm: dict, snap: dict, st: dict, with_lessons: bool = True) -> str:
    ev = [e for e in swarm.get("events") or [] if e.get("gen") == gen]
    born = [e for e in ev if e["event"] == "born"]
    killed = [e for e in ev if e["event"] == "killed"]
    retired = [e for e in ev if e["event"] == "retired"]
    promoted = [e for e in ev if e["event"] == "promoted"]
    lines = [f"🧬 SolMeme · generation {gen} finished", ""]
    if not ev:
        lines.append("A quiet one: nobody born, killed or retired.")
    if born:
        lines.append(f"Born {len(born)}: " + "; ".join(
            f"{short(e['key'])} ({', '.join(dict.fromkeys(_op(s) for s in e.get('ops') or [])) or 'mutated'})" for e in born[:4])
            + (f" (+{len(born) - 4} more)" if len(born) > 4 else ""))
    for e in killed[:3]:
        lines.append(f"Killed {short(e['key'])}: {(e.get('reason') or '')[:90]}")
    if len(killed) > 3:
        lines.append(f"…and {len(killed) - 3} more killed")
    if retired:
        lines.append(f"Retired {len(retired)} to make room (the weakest of a full swarm): " + ", ".join(short(e["key"]) for e in retired[:4]))
    for e in promoted:
        lines.append(f"👑 Promoted to champion: {short(e['key'])}")
    # the API's lessons are the newest generation's: only shown when that is the one reported
    lessons = sorted(swarm.get("lessons") or [], key=lambda l: -(l.get("saved_sol") or 0))[:3] if with_lessons else []
    if lessons:
        lines += ["", "What it learned (losing trades it would now avoid):"]
        for l in lessons:
            lines.append(f"• {l['strategy_id']}: don't buy when {l['feature']} {'<' if l['op'] == '<' else '>'} {l['v']:.4g} "
                         f"(would have saved {l['saved_sol']:+.2f} SOL, blocking {l['blocked']}/{l['of']})")
    ideas = [i for i in ((swarm.get("council") or {}).get("ideas") or []) if i.get("gen") == gen]
    approved = [i for i in ideas if i.get("approved")]
    if ideas:
        lines += ["", f"Council: {len(approved)} rule(s) approved, {len(ideas) - len(approved)} dropped."]
        for i in approved[:3]:
            lines.append(f"• {i['strategy_id']}: {_rule(i)} (by {i.get('proposed_by')})")
    seen = st.get("whale_verdicts") or {}
    now_v = {w["address"]: w.get("council") for w in snap.get("whales") or [] if w.get("council")}
    changed = {a: v for a, v in now_v.items() if seen.get(a) != v}
    st["whale_verdicts"] = now_v
    if changed:
        n = {v: sum(1 for x in changed.values() if x == v) for v in ("trust", "distrust", "unsure")}
        lines.append(f"Whales judged: {n['trust']} trusted, {n['distrust']} distrusted, {n['unsure']} unsure.")
    best = (swarm.get("best") or [])[:1]
    if best:
        b = best[0]
        lines += ["", f"Best agent: {short(b['key'])} · wallet {b['equity'] * 100:.0f}% · {b['trades']} trades"]
    w, d, last = snap.get("wallet") or {}, snap.get("dryrun") or {}, st.get("gen_wallets") or {}
    if w:
        since = f" ({sgn_usd(w['balance_usd'] - last['paper'])} since last report)" if last.get("paper") is not None else ""
        lines.append(f"Paper wallet {usd(w['balance_usd'])}{since}")
    if d:
        since = f" ({sgn_usd(d['equity_usd'] - last['live'])} since last report)" if last.get("live") is not None and d.get("equity_usd") is not None else ""
        lines.append(f"Live dry run {usd(d.get('equity_usd'))} ({pct(d.get('pnl_pct'))} vs {usd(d.get('start_usd'))}){since}")
    st["gen_wallets"] = {"paper": w.get("balance_usd"), "live": d.get("equity_usd")}
    lines += ["", f"Details: {BASE}/#/swarm"]
    return "\n".join(lines)


def check_generation(snap: dict, st: dict, fetch_swarm) -> list[str]:
    """One report per finished swarm generation. The clock says which generation is running (or was
    last started), so the finished one is that minus one while it runs, or that one when idle. A restart
    re-runs a generation under the same number, so nothing is ever reported twice. When several finished
    between checks, only the latest is reported (no flood)."""
    sc = snap.get("swarm_clock") or {}
    gen_now = sc.get("gen")
    if gen_now is None:
        return []                                        # a bot from before the clock had numbers
    done = int(gen_now) - (1 if sc.get("running") else 0)
    if "gen_reported" not in st:
        st["gen_reported"] = done                        # first run after install: start from here
        return []
    if done <= st["gen_reported"]:
        return []
    try:
        swarm = fetch_swarm()
    except Exception:  # noqa: BLE001 - try again on the next run
        return []
    st["gen_reported"] = done
    # the API's lessons belong to the newest generation it has recorded
    return [generation_report(done, swarm, snap, st, with_lessons=int(swarm.get("generation") or 0) == done)]


def check_live(snap: dict, st: dict) -> list[str]:
    """The live dry run: once when an attempt hits its loss limit, once when a new attempt starts."""
    d = snap.get("dryrun") or {}
    if not d:
        return []
    msgs, att = [], d.get("attempt") or 1
    if d.get("stopped") and st.get("live_stop_alert") != att:
        st["live_stop_alert"] = att
        msgs.append(f"🛑 Live wallet (dry run, attempt #{att}) {d['stopped']}: {usd(d.get('equity_usd'))} "
                    f"({pct(d.get('pnl_pct'))} vs {usd(d.get('start_usd'))}; paper {pct(d.get('paper_pnl_pct'))} over the same time). "
                    f"It stopped buying, as a real wallet with a kill switch would. Start attempt #{att + 1} from the "
                    f"Live page when you're ready. {BASE}/#/live")
    if st.get("live_attempt") not in (None, att) and att > st["live_attempt"]:
        msgs.append(f"🔁 Live wallet: attempt #{att} started with a fresh {usd(d.get('start_usd'))}.")
    st["live_attempt"] = att
    return msgs


def check_live_trades(live: dict, st: dict) -> list[str]:
    """Every live (dry-run) buy and sell since the last run; skipped buys are summed into one line."""
    tape = (live or {}).get("tape") or []
    if not tape:
        return []
    newest = max(t["ts"] for t in tape)
    since = st.get("live_tape_ts")
    st["live_tape_ts"] = max(newest, since or 0)
    if since is None:
        return []                                    # first run: don't replay the whole tape
    px = live.get("sol_usd")
    money = (lambda s: usd(s * px)) if px else (lambda s: f"{s:.4f} SOL")
    msgs, missed, blocked = [], [], []
    for t in sorted((t for t in tape if t["ts"] > since), key=lambda t: t["ts"]):
        sym = f"${t.get('symbol') or t['mint'][:6]}"
        link = f"https://dexscreener.com/solana/{t['mint']}"
        who = short(t.get("key") or "")
        if t["side"] == "buy":
            why = ", ".join(re.split(r",(?=[a-z_])", str(t.get("why") or ""))[:3])   # values can hold "1,221"
            boost = f" · {t['size_mult']:g}x bet ({t.get('whales')} whale)" if (t.get("size_mult") or 1) > 1 else ""
            msgs.append(f"🟢 Live BUY {sym}: {money(t.get('sol') or 0)} by {who}{boost}\nwhy: {why}\n{link}")
        elif t["side"] == "sell":
            pnl = t.get("pnl_sol")
            cost = (t.get("sol") or 0) - (pnl or 0)                       # proceeds minus profit: what it cost
            pct_live = 100 * pnl / cost if pnl is not None and cost > 0 else None   # after fees, like the wallet
            icon = "✅" if (pnl or 0) > 0 else "🔻"
            part = " (part)" if t.get("partial") else ""
            msgs.append(f"{icon} Live SELL{part} {sym}: {pct(pct_live)} ({sgn_usd(pnl * px) if pnl is not None and px else '—'}), "
                        f"{t.get('why') or 'exit'} · paper did {pct(t.get('paper_pnl_pct'))}\n{link}")
        elif t["side"] == "missed":
            missed.append(f"{sym} ({str(t.get('why') or '')[:50]})")
        elif t["side"] == "blocked":
            blocked.append(f"{sym}: {str(t.get('why') or '').replace('overseer: ', '')[:90]}")
        elif t["side"] in ("stuck", "sell_failed"):
            msgs.append(f"⚠️ Live {t['side'].replace('_', ' ')} {sym}: {str(t.get('why') or '')[:80]}\n{link}")
    if missed:
        msgs.append(f"⏭ Live skipped {len(missed)} buy(s) at real prices: " + "; ".join(missed[:4]))
    if blocked:
        msgs.append(f"🛡 The Overseer stopped {len(blocked)} live buy(s): " + "; ".join(blocked[:3]))
    return msgs


BROWSER_SOURCES = ("fomo", "axiom", "j7tracker")
BROWSER_STALL_S = 3600


def check_browsers(snap: dict, st: dict, now: float) -> list[str]:
    """A browser source (a logged-in page the bot listens to) that stops producing events has usually been
    logged out: fomo sat at 0 events from Oct 2 to Oct 10 without a word. Alert once; re-arm when it recovers."""
    seen = st.setdefault("browser_events", {})
    msgs = []
    for s in snap.get("sources") or []:
        name = s.get("name")
        if name not in BROWSER_SOURCES or s.get("status") in ("off", "disabled"):
            continue
        ev = int(s.get("events") or 0)
        last = seen.get(name)
        if last is None or ev != last["events"]:          # moved (a restart resets the count: also a change)
            if last and last.get("alerted") and ev > last["events"]:
                msgs.append(f"✅ {name} is producing events again.")
            seen[name] = {"events": ev, "since": now, "alerted": False}
            continue
        if not last["alerted"] and now - last["since"] >= BROWSER_STALL_S:
            last["alerted"] = True
            msgs.append(f"🔑 {name} has produced no events for {int((now - last['since']) // 3600)}h+: its login has "
                        f"probably expired. Run `.venv/bin/solmeme login {name}` on the Mac and tell Claude to copy "
                        f"the session over. {BASE}/#/sources")
    return msgs


def check_my_wallets(data: dict, st: dict) -> list[str]:
    """Wallets the council added to your tracking, and ones a 4-hour check removed (not your own removals)."""
    wallets, removed = (data or {}).get("wallets") or {}, (data or {}).get("removed") or []
    first = "my_wallets_seen" not in st
    seen = set(st.get("my_wallets_seen") or [])
    since = st.get("my_wallets_removed_ts", 0)
    st["my_wallets_seen"] = sorted(wallets)
    st["my_wallets_removed_ts"] = max([since] + [r.get("ts", 0) for r in removed])
    if first:
        return []
    msgs = []
    for a, e in wallets.items():
        if a not in seen and e.get("by") == "council":
            msgs.append(f"🐋 The council added wallet {a[:6]}…{a[-4:]} to your tracked wallets. Its buys now count as "
                        f"whale buys. Remove it any time: {BASE}/#/ask  https://gmgn.ai/sol/address/{a}")
    for r in removed:
        if r.get("ts", 0) > since and r.get("by") != "you":
            msgs.append(f"🧹 Wallet {r['address'][:6]}…{r['address'][-4:]} was removed from tracking: {r.get('reason')}")
    return msgs


def check_whale_clusters(tape: dict, st: dict, now: float) -> list[str]:
    """Several whales from the book buying the same coin within minutes: once per coin (re-arms after 6 h)."""
    sent = st.setdefault("whale_clusters", {})
    for m in [m for m, t in sent.items() if now - t > 6 * 3600]:
        sent.pop(m)
    msgs = []
    for c in (tape or {}).get("clusters") or []:
        if c["mint"] in sent:
            continue
        sent[c["mint"]] = now
        trusted = f" ({c['trusted']} trusted by the council)" if c.get("trusted") else ""
        mc = f" · mcap {usd(c['mcap_usd'])}" if c.get("mcap_usd") else ""
        msgs.append(f"🐋 {c['whales']} whales{trusted} bought ${c.get('symbol') or c['mint'][:6]} in the last 10 min{mc}\n"
                    f"https://dexscreener.com/solana/{c['mint']}  ·  {BASE}/#/live-whales")
    return msgs

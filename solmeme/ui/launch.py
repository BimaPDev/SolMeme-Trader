"""Start the localhost server, and the Bubble Tea program when asked."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from solmeme.config import ROOT
from solmeme.ui.server import Hub, serve
from solmeme.ui.snapshot import build_snapshot

UI_PORT = 8787
# Set SOLMEME_UI_HOST=0.0.0.0 to serve the site to the LAN (e.g. on a homelab box); default is localhost.
UI_HOST = os.environ.get("SOLMEME_UI_HOST", "127.0.0.1")


class UiError(RuntimeError):
    pass


def attach(session, mode: str) -> str:
    """Publish snapshots on localhost. mode is "web" or "tui". Returns the site URL."""
    if mode not in ("web", "tui"):
        raise UiError(f"unknown ui {mode}")
    hub = Hub()
    hub.report_dir = session.settings.reports_dir / "session" / (session.run_id or "none")
    hub.history_fn = session.series.as_dict
    if session.store is not None:
        hub.swarm_fn = lambda: _swarm_view(session)
        hub.agent_fn = lambda key: _agent_view(session, key)
        hub.council_chat_fn = lambda sitting, after: _council_chat_view(session, sitting, after)
        hub.cursor_usage_fn = lambda fresh: _cursor_usage_view(fresh)
        hub.live_fn = lambda: _live_view(session)
        hub.live_action_fn = lambda action, why: _live_action(session, action, why)
    from solmeme.ui.narrative_view import build_narratives, build_token, build_trades, retry
    hub.narratives_fn = lambda: retry(lambda: build_narratives(session))
    hub.trades_fn = lambda: retry(lambda: build_trades(session))
    hub.token_fn = lambda mint: retry(lambda: build_token(session, mint))
    from solmeme.ai.ask import list_asks, start as start_ask, start_wallet
    hub.ask_fn = lambda mint, note, kind="token": (start_wallet if kind == "wallet" else start_ask)(session, mint, note)
    hub.my_wallets_fn = lambda action, address, note, verdict: _my_wallets(session, action, address, note, verdict)
    hub.whale_tape_fn = lambda: _whale_tape(session)
    hub.team_fn = lambda: _team(session)
    from solmeme.ui import workflow
    hub.research_fn = lambda: retry(lambda: workflow.research_feed(session))
    hub.setups_fn = lambda: retry(lambda: workflow.setups(session))
    hub.permissions_fn = lambda changes: (workflow.save_permissions(session.settings.data_dir, changes) if changes
                                          else workflow.load_permissions(session.settings.data_dir))
    from solmeme.ai.ask import start_picks, start_rechecks
    start_rechecks(session)                     # tracked wallets: checked again every 4 hours
    if getattr(session.settings.council, "auto_picks", False):
        start_picks(session)                    # whale clusters → the council decides → council_pick buys
    hub.asks_fn = lambda ask_id: list_asks(session.settings, ask_id)
    session.hub = hub
    session.publish()
    try:
        httpd, port = serve(hub, ROOT / "web", host=UI_HOST,
                            port=int(os.environ.get("SOLMEME_UI_PORT", UI_PORT)))
    except OSError as exc:
        raise UiError(str(exc)) from exc
    session._httpd = httpd
    url = f"http://127.0.0.1:{port}/"
    if mode == "web":
        session.console.print(f"[bold]website[/bold] {url}")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        return url
    binary = ensure_tui_binary()
    session._tui_proc = subprocess.Popen(
        [str(binary), "--url", f"http://127.0.0.1:{port}/api/snapshot"],
        cwd=str(ROOT),
    )
    return url


def _swarm_view(session) -> dict:
    from solmeme.core.store import Store
    from solmeme.ui.swarm_view import build_swarm

    store = Store(session.settings.db_path)      # SQLite connections stay on the thread that made them
    try:
        sol_usd = session.engine.market.sol_usd if session.engine else None
        # council members and kill rules come from the file as it is now, so edits show without a restart
        from solmeme.config import load_settings
        fresh = load_settings()
        fresh.data_dir, fresh.strategies_dir = session.settings.data_dir, session.settings.strategies_dir
        fresh.portfolio.starting_usd = session.settings.portfolio.starting_usd
        return build_swarm(store, fresh, sol_usd)
    finally:
        store.close()


def _council_chat_view(session, sitting: str, after: int) -> dict:
    from solmeme.config import load_settings
    from solmeme.core.store import Store
    from solmeme.ui.council_chat import build_council_chat

    store = Store(session.settings.db_path)      # SQLite connections stay on the thread that made them
    try:
        fresh = load_settings()                  # the judge list as the file is now
        fresh.data_dir = session.settings.data_dir
        return build_council_chat(store, fresh, sitting, after)
    finally:
        store.close()


def _live_view(session) -> dict:
    from solmeme.core.store import Store
    from solmeme.ui.narrative_view import retry

    d = session.dryrun
    if d is None or not d.started_ts:
        return {"enabled": bool(session.settings.dryrun.enabled), "started": False}
    store = Store(session.settings.db_path)      # SQLite connections stay on the thread that made them
    try:
        trades = store.dryrun_trades(d.started_ts)
    finally:
        store.close()
    sol_usd = session.engine.market.sol_usd if session.engine else None
    b = session.engine.shared_breakdown() if session.engine else None

    def build() -> dict:                         # the trading loop changes the wallet meanwhile: copies, retried
        view = d.view(sol_usd, (b["cash"] + b["held"]) if b else None)
        from solmeme.sim.calibration import load as load_calibration
        cal = load_calibration(session.settings.data_dir) if session.settings.fills.calibrate_from_live else None
        _enrich_open(session, view.get("open") or [], sol_usd)
        return dict(view, started=True, series=list(d.series), readiness=d.readiness(trades),
                    history=list(reversed(trades[-500:])), sol_usd=sol_usd, now=time.time(),
                    calibration=cal.summary() if cal else None)
    return retry(build)


def _live_action(session, action: str, why: str) -> dict:
    """Queue it for the dry run's own loop (it owns the wallet; this is the web server's thread)."""
    import json

    from solmeme.sim.dryrun import REQUEST_FILE

    if action != "new_attempt" or session.dryrun is None:
        return {"ok": False, "error": "no live dry run"}
    path = session.settings.data_dir / "session" / REQUEST_FILE
    path.write_text(json.dumps({"action": action, "why": why or "started from the dashboard"}), encoding="utf-8")
    return {"ok": True, "attempt": session.dryrun.attempt + 1}


def _cursor_usage_view(fresh: bool) -> dict:
    from solmeme.config import load_settings
    from solmeme.ui.cursor_usage import cursor_usage

    return cursor_usage(load_settings(), fresh)       # the judge list as the file is now


def _agent_view(session, key: str) -> dict:
    from solmeme.core.store import Store
    from solmeme.ui.swarm_view import agent_detail

    store = Store(session.settings.db_path)
    try:
        return agent_detail(store, session.settings, key, session.engine.market.sol_usd if session.engine else None)
    finally:
        store.close()


def ensure_tui_binary() -> Path:
    tui = ROOT / "tui"
    binary = tui / "solmeme-tui"
    sources = list(tui.glob("*.go"))
    gomod = tui / "go.mod"
    if not sources or not gomod.is_file():
        raise UiError("tui/ has no Go program yet")
    newest = max(path.stat().st_mtime for path in [*sources, gomod])
    if binary.is_file() and binary.stat().st_mtime >= newest:
        return binary
    go = shutil.which("go")
    if not go:
        raise UiError("Go is not installed, so the Bubble Tea UI cannot be built. Use --ui web or the normal CLI.")
    sys.__stderr__.write("building Bubble Tea UI…\n")
    try:
        subprocess.run([go, "build", "-o", binary.name, "."], cwd=tui, check=True)
    except subprocess.CalledProcessError as exc:
        raise UiError("Bubble Tea UI failed to build") from exc
    return binary


def _my_wallets(session, action: str, address: str, note: str, verdict: str) -> dict:
    """List, add or remove the wallets you track; the session merges them within a minute."""
    from solmeme.ai.ask import MINT_RE
    from solmeme.lab import my_wallets

    d = session.settings.data_dir
    if action == "list":
        return {"wallets": my_wallets.load(d), "max": my_wallets.MAX, "removed": my_wallets.removed(d)[-10:][::-1]}
    if not MINT_RE.match(address.strip()):
        return {"error": "that is not a Solana wallet address"}
    if action == "detail":
        from solmeme.ai.ask import wallet_detail
        return wallet_detail(session, address.strip())
    if action == "add":
        from solmeme.ai.ask import latest_wallet_ask, why_of
        got = my_wallets.add(d, address.strip(), note, verdict)
        last = latest_wallet_ask(session.settings, address.strip())
        if "error" not in got and last:
            my_wallets.update(d, address.strip(), why=why_of(last), name=((last.get("wallet") or {}).get("name") or ""))
        return got
    if action == "remove":
        return my_wallets.remove(d, address.strip(), "removed by you", by="you")
    return {"error": "unknown action"}


def _whale_tape(session, window_s: float = 3600, cluster_s: float = 600, cluster_min: int = 3) -> dict:
    """The live whale tape: recent trades by whales in the book, and coins several whales bought within minutes."""
    import time

    from solmeme.lab import my_wallets

    now = time.time()
    rows = my_wallets.whale_trades(session.settings.data_dir, since=now - window_s)
    book = getattr(session, "whalebook", None)
    m = session.engine.market if session.engine else None
    out, buyers = [], {}
    for r in rows:
        e = (book.wallets.get(r["wallet"]) if book else None) or {}
        st = m.get(r["mint"]) if m else None
        x = st.price_sol / r["price_sol"] if st and st.price_sol and r.get("price_sol") else None
        out.append({**r, "symbol": r.get("symbol") or (st.symbol if st else "") or r["mint"][:6], "x_since": round(x, 2) if x else None,
                    "whale": dict(score=e.get("score"), win=e.get("win"), council=e.get("council"), label=e.get("label"))})
        if r["side"] == "buy" and now - r["ts"] <= cluster_s:
            buyers.setdefault(r["mint"], {}).setdefault(r["wallet"], r["ts"])
    clusters = []
    for mint, ws in buyers.items():
        if len(ws) >= cluster_min:
            st = m.get(mint) if m else None
            trusted = sum(1 for w in ws if book and (book.wallets.get(w) or {}).get("council") == "trust")
            clusters.append(dict(mint=mint, symbol=(st.symbol if st else "") or mint[:6], whales=len(ws), trusted=trusted,
                                 first_ts=min(ws.values()), mcap_usd=st.mcap_sol * m.sol_usd if st and st.mcap_sol and m.sol_usd else None))
    clusters.sort(key=lambda c: (-c["whales"], -c["first_ts"]))
    return dict(now=now, trades=out[:200], clusters=clusters, whales_watched=len(getattr(session, "_whale_set", ()) or ()))


def _enrich_open(session, rows: list[dict], sol_usd: float | None) -> None:
    """Live positions for the Positions page: market cap now, the strategy's exit plan, the council's verdict."""
    from solmeme.ai.ask import list_asks

    eng = session.engine
    specs = {r.key: r.spec for r in (eng.runners if eng else [])}
    reviews = {}
    try:
        for a in list_asks(session.settings)["asks"]:
            if a.get("kind") == "review" and a.get("status") == "done":
                reviews.setdefault(a["mint"], a)
    except Exception:  # noqa: BLE001
        pass
    for r in rows:
        st = eng.market.get(r["mint"]) if eng else None
        if st is not None and st.price_sol and sol_usd:
            r["mcap_usd"] = st.price_sol * 1e9 * sol_usd
        sp = specs.get(r["key"])
        if sp is not None:
            ex, p = sp.exits, sp.p
            plan = [f"sell {lv.sell_pct:g}% at {float(p(lv.at_x)):g}x" for lv in ex.take_profit]
            if ex.stop_loss_pct is not None:
                plan.append(f"stop at -{float(p(ex.stop_loss_pct)):g}%")
            if ex.trailing_pct is not None:
                plan.append(f"trail {float(p(ex.trailing_pct)):g}% after {float(p(ex.trailing_activate_x)):g}x")
            if ex.on_kol_sell:
                plan.append("sell when a KOL sells")
            if ex.on_dev_sell:
                plan.append("sell when the dev sells")
            if ex.max_hold_s:
                plan.append(f"sell after {float(p(ex.max_hold_s)) / 60:g} min")
            r["exit_plan"] = plan
        a = reviews.get(r["mint"])
        if a:
            r["council"] = dict(verdict=a.get("verdict"), agreement=a.get("agreement"), action=a.get("action"),
                                why=[f"{v['judge']}: {(v.get('reasons') or [''])[0]}" for v in a.get("votes") or [] if v.get("reasons")][:3])


def _team(session) -> dict:
    """The team at a glance: ResearchBot, SniperBot, TraderBot, the council and the Overseer, each with what it is doing."""
    import collections
    import time

    from solmeme.ai.ask import list_asks

    now = time.time()
    stats = getattr(getattr(session, "ctx", None), "stats", {}) or {}
    research_src = ("pumpfun", "pumpfun_social", "gmgn", "dexscreener", "fomo", "j7tracker", "chaincheck", "gecko")
    sources = [dict(name=n, status=stats[n].status, events=stats[n].events,
                    quiet_s=round(now - stats[n].last_event_ts) if stats[n].last_event_ts else None)
               for n in research_src if n in stats]
    d = session.dryrun
    tape = [t for t in (d.tape if d else []) if now - t.get("ts", 0) <= 86400]
    sides = collections.Counter(t["side"] for t in tape)
    misses = collections.Counter((t.get("why") or "").split(":")[0] for t in tape if t["side"] == "missed")
    asks = [a for a in list_asks(session.settings)["asks"] if now - a.get("ts", 0) <= 86400]
    picks = collections.Counter(a.get("action") for a in asks if a.get("kind") == "pick")
    reviews = collections.Counter(a.get("action") for a in asks if a.get("kind") == "review")
    eng = session.engine
    champs = [r.key for r in (eng.runners if eng else []) if r.label == "champion"]
    open_live = len(d.positions) if d else 0
    members = [m for m in session.settings.council.members if m.enabled]
    ov = session.overseer.status() if getattr(session, "overseer", None) else None
    return dict(
        now=now,
        research=dict(sources=sources, early_lane=getattr(session.settings.council, "early_picks", False),
                      whales_watched=len(getattr(session, "_whale_set", ()) or ())),
        sniper=dict(buys=sides.get("buy", 0), missed=sides.get("missed", 0), blocked=sides.get("blocked", 0),
                    miss_reasons=dict(misses.most_common(4)), slippage_limit_pct=session.settings.dryrun.slippage_tolerance_pct,
                    reviews=dict(reviews)),
        trader=dict(champions=champs, open_live=open_live, sells=sides.get("sell", 0),
                    loss_limit_pct=session.settings.dryrun.loss_limit_pct,
                    rules=[c.get("id") for c in session.settings.prefilter.all] + [c.get("id") for c in session.settings.prefilter.none]),
        council=dict(judges=[m.name for m in members if not m.fallback], backups=[m.name for m in members if m.fallback],
                     picks=dict(picks), reviews=dict(reviews)),
        overseer=ov,
    )

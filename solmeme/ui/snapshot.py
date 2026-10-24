"""JSON view of a live paper session, shared by the terminal UI and the website."""

from __future__ import annotations

import json
import time

from solmeme.lab.metrics import engine_metrics
from solmeme.sim.activity import age_label, short_mint
from solmeme.sim.live import _held_sol, _paper_equity_sol
from solmeme.sim.positions import position_owners, position_rows, position_values

_POSITION_KEYS = ("coin", "strategy", "status", "bought", "entry", "sold", "mark", "made")


def build_snapshot(session) -> dict:
    now = time.time()
    engine = session.engine
    sol_usd = engine.market.sol_usd if engine is not None else None
    return {
        "paper": True,
        "ui": session.ui,
        "now": now,
        "uptime_s": max(0.0, now - session.started),
        "events": session.writer.count,
        "sol_usd": sol_usd,
        "wallet": session.wallet(),
        "dryrun": _dryrun(session, sol_usd),
        "shared_wallet": engine is not None and engine.account is not None,
        "sources": _sources(session),
        "trade_streams": _trade_streams(session),
        "strategies": _strategies(session, sol_usd),
        "positions": _positions(session),
        "tokens": _tokens(session, now),
        "sniper": _sniper(session),
        "boards": _boards(session.boards_snap, session.engine, now),
        "people": _people(session.people_cards),
        "people_note": getattr(session, "people_note", "") or "",
        "feed": list(session.feed),
        "scan_note": session.scan_note,
        "swarm_clock": _swarm_clock(session, now),
        "bans": _bans(session),
        "whales": _whales(session),
    }


def _whales(session) -> list[dict] | None:
    book = getattr(session, "whalebook", None)
    if book is None:
        return None
    tracked = set(session.settings.sources.tracked_wallets)
    rows = [dict(address=w, status=e["status"], score=e["score"], win=e.get("win"), median_exit=e.get("median_exit"),
                 round_trips=e.get("round_trips", 0), early_good=e.get("early_good", 0), label=e.get("label", ""),
                 flag=e.get("flag", ""), tracked=w in tracked, history=[h[1] for h in (e.get("history") or [])[-12:]],
                 council=e.get("council"), council_score=e.get("council_score"), council_reason=e.get("council_reason", ""),
                 council_ts=e.get("council_ts"))
            for w, e in book.wallets.items() if e["status"] in ("whale", "watch", "dropped")]
    order = {"whale": 0, "watch": 1, "dropped": 2}
    return sorted(rows, key=lambda r: (order[r["status"]], -r["score"]))[:40]


def _bans(session) -> dict | None:
    bans = getattr(session, "banlist", None)       # reloaded by the session every minute
    if bans is None:
        return None
    rows = sorted(bans.bans.values(), key=lambda b: -b.ts)
    flagged = 0
    if session.engine is not None:
        flagged = sum(1 for st in session.engine.market.tokens.values() if st.banned_hits)
    return {"count": len(rows), "tokens_blocked": flagged,
            "recent": [{"address": b.address, "role": b.role, "source": b.source, "ts": b.ts, "reason": b.reason}
                       for b in rows[:25]]}


def _swarm_clock(session, now: float) -> dict | None:
    cfg = session.settings.evolution
    if not cfg.enabled or session.engine is None:
        return None
    proc = getattr(session, "_evo_proc", None)
    last = getattr(session, "_last_evo", None)
    running = proc is not None and proc.returncode is None
    phase = started = gen = None
    try:                               # the generation running now, or the last one started
        st = json.loads((session.settings.data_dir / "session" / "evolve.json").read_text())
        gen = st.get("gen")
        if running:
            phase, started = st.get("phase"), st.get("started")
    except (OSError, ValueError):
        pass
    return {
        "running": running,
        "gen": gen,
        "phase": phase,
        "started_ts": started,
        "next_ts": None if last is None else last + cfg.every_min * 60,
        "every_min": cfg.every_min,
        "agents": sum(1 for r in session.engine.runners if r.label == "swarm"),
    }


def _sources(session) -> list[dict]:
    if session.ctx is None:
        return []
    rows = []
    for name, stat in sorted(session.ctx.stats.items()):
        rows.append({
            "name": name,
            "status": stat.status,
            "events": stat.events,
            "errors": stat.errors,
            "note": (stat.note or "")[:160],
        })
    return rows


def _trade_streams(session) -> dict | None:
    pp = session.pp
    if pp is None:
        return None
    return {
        "unavailable": bool(pp.trade_streams_unavailable),
        "subscribed": len(pp.subscribed),
        "events_today": pp.trade_events_today,
        "budget_exhausted": bool(pp.budget_exhausted),
    }


def _strategies(session, sol_usd) -> list[dict]:
    engine = session.engine
    if engine is None:
        return []
    metrics = engine_metrics(engine)
    rows = []
    for runner in engine.runners:
        stat = metrics[runner.key]
        shared = engine.account is not None
        # shared wallet: cash is the wallet's, so a row's balance is what its open positions are worth
        equity = _held_sol(runner, engine) if shared else _paper_equity_sol(runner, engine)
        rows.append({
            "key": runner.key,
            "role": runner.label,
            "open": len(runner.portfolio.positions),
            "closed": stat.n_trades,
            "win_pct": round(stat.win_rate * 100, 1),
            "pnl_sol": stat.total_pnl_sol,
            "made_usd": None if not sol_usd else stat.total_pnl_sol * sol_usd,
            "cash_sol": None if shared else runner.portfolio.cash,
            "balance_sol": equity,
            "balance_usd": None if not sol_usd else equity * sol_usd,
            "in_wallet": engine.in_shared_wallet(runner) if shared else True,   # challengers: own copy
        })
    return rows


def _positions(session) -> list[dict]:
    if session.engine is None:
        return []
    px = session.engine.market.sol_usd
    rows = []
    for row, (mint, invested, worth), (key, role) in zip(position_rows(session.engine), position_values(session.engine),
                                                          position_owners(session.engine), strict=True):
        d = dict(zip(_POSITION_KEYS, row, strict=True))
        d.update(mint=mint, invested_sol=invested, worth_sol=worth, key=key, role=role,
                 invested_usd=invested * px if px else None, worth_usd=worth * px if px else None)
        rows.append(d)
    return rows


def _tokens(session, now: float) -> list[dict]:
    symbols, held, opened = _token_maps(session)
    rows = []
    for note in session.activity.board(session.watch, symbols, held, now, opened=opened):
        rows.append({
            "symbol": note.symbol or short_mint(note.mint),
            "mint": note.mint,
            "mint_short": short_mint(note.mint),
            "phase": note.phase,
            "detail": note.detail,
            "age": age_label(note.ts, now),
            "ts": note.ts,
            "source": note.source,
        })
    return rows


def _token_maps(session) -> tuple[dict[str, str], dict[str, str], dict[str, float]]:
    symbols: dict[str, str] = {}
    held: dict[str, str] = {}
    opened: dict[str, float] = {}
    engine = session.engine
    if engine is None:
        return symbols, held, opened
    for mint, tok in engine.market.tokens.items():
        if tok.symbol:
            symbols[mint] = tok.symbol
    for runner in engine.runners:
        for mint, pos in runner.portfolio.positions.items():
            if pos.symbol:
                symbols[mint] = pos.symbol
            held[mint] = runner.spec.id if mint not in held else f"{held[mint]}, {runner.spec.id}"
            prev = opened.get(mint)
            if prev is None or pos.entry_ts < prev:
                opened[mint] = pos.entry_ts
    return symbols, held, opened


def _sniper(session) -> dict | None:
    if not session.sniper:
        return None
    out = {}
    for key, value in session.sniper.learner.summary().items():
        if isinstance(value, (int, float, str, bool)) or value is None:
            if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
                value = None
            out[key] = value
    return out


def _boards(snap: dict | None, engine=None, now: float = 0.0) -> dict | None:
    if not snap:
        return None
    trenches = snap.get("trenches") or {}
    pump = snap.get("pump") or {}
    has_trenches = any(trenches.get(k) for k in ("new_creation", "near_completion", "completed"))
    has_pump = any(pump.get(k) for k in ("new", "almost", "graduated"))
    trending = snap.get("trending") or []
    trending_source = "gmgn" if trending else None
    if not trending:
        trending = _live_trending(engine, now)
        trending_source = "dexscreener" if trending else None
    return {
        # without GMGN, the UIs show the pump.fun lanes as Trenches
        "trenches_source": "gmgn" if has_trenches else ("pump.fun" if has_pump else None),
        "trending_source": trending_source,
        "trenches": {
            key: [_board_row(row) for row in trenches.get(key) or []]
            for key in ("new_creation", "near_completion", "completed")
        },
        "trending": [
            {
                "symbol": row.get("symbol") or "",
                "mcap_usd": row.get("mcap_usd"),
                "volume_usd": row.get("volume_usd"),
                "holders": row.get("holders"),
                "mint": row.get("mint") or "",
            }
            for row in trending
        ],
        "pump": {
            key: [_pump_row(row) for row in pump.get(key) or []]
            for key in ("new", "almost", "graduated")
        },
        "cards": [
            {"verdict": card.get("verdict") or "", "text": card.get("text") or ""}
            for card in snap.get("cards") or []
        ],
    }


def _live_trending(engine, now: float, limit: int = 15, fresh_s: float = 600) -> list[dict]:
    """Watched tokens with the most DexScreener 1h volume, for when GMGN trending is unavailable."""
    if engine is None:
        return []
    rows = []
    for mint, st in engine.market.tokens.items():
        vol = st.dex.get("volume_h1_usd")
        if not vol or st.dex_ts is None or now - st.dex_ts > fresh_s:
            continue
        rows.append({"symbol": st.symbol or st.dex.get("symbol") or "", "mcap_usd": st.dex.get("mcap_usd"),
                     "volume_usd": vol, "holders": None, "mint": mint})
    rows.sort(key=lambda r: -r["volume_usd"])
    return rows[:limit]


def _board_row(row: dict) -> dict:
    return {
        "symbol": row.get("symbol") or "",
        "mcap_usd": row.get("mcap_usd"),
        "holders": row.get("holders"),
        "mint": row.get("mint") or "",
    }


def _pump_row(row: dict) -> dict:
    return {
        "symbol": row.get("symbol") or "",
        "mcap_usd": row.get("mcap_usd"),
        "bonding_pct": row.get("bonding_pct"),
        "mint": row.get("mint") or "",
    }


def _people(cards: list) -> list[dict]:
    return [
        {"verdict": card.get("verdict") or "", "text": card.get("text") or ""}
        for card in cards or []
    ]


def _dryrun(session, sol_usd: float | None) -> dict | None:
    d = getattr(session, "dryrun", None)
    if d is None or not d.started_ts:
        return None
    b = session.engine.shared_breakdown() if session.engine is not None else None
    return d.summary(sol_usd, (b["cash"] + b["held"]) if b else None)

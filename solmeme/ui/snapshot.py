"""JSON view of a live paper session, shared by the terminal UI and the website."""

from __future__ import annotations

import time

from solmeme.lab.metrics import engine_metrics
from solmeme.sim.activity import age_label, short_mint
from solmeme.sim.live import _paper_equity_sol
from solmeme.sim.positions import position_rows

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
        "sources": _sources(session),
        "trade_streams": _trade_streams(session),
        "strategies": _strategies(session, sol_usd),
        "positions": _positions(session),
        "tokens": _tokens(session, now),
        "sniper": _sniper(session),
        "boards": _boards(session.boards_snap),
        "people": _people(session.people_cards),
        "feed": list(session.feed),
        "scan_note": session.scan_note,
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
        equity = _paper_equity_sol(runner, engine)
        rows.append({
            "key": runner.key,
            "role": runner.label,
            "open": len(runner.portfolio.positions),
            "closed": stat.n_trades,
            "win_pct": round(stat.win_rate * 100, 1),
            "pnl_sol": stat.total_pnl_sol,
            "made_usd": None if not sol_usd else stat.total_pnl_sol * sol_usd,
            "cash_sol": runner.portfolio.cash,
            "balance_sol": equity,
            "balance_usd": None if not sol_usd else equity * sol_usd,
        })
    return rows


def _positions(session) -> list[dict]:
    if session.engine is None:
        return []
    return [dict(zip(_POSITION_KEYS, row, strict=True)) for row in position_rows(session.engine)]


def _tokens(session, now: float) -> list[dict]:
    symbols: dict[str, str] = {}
    held: dict[str, str] = {}
    engine = session.engine
    if engine is not None:
        for mint, tok in engine.market.tokens.items():
            if tok.symbol:
                symbols[mint] = tok.symbol
        for runner in engine.runners:
            for mint, pos in runner.portfolio.positions.items():
                if pos.symbol:
                    symbols[mint] = pos.symbol
                held[mint] = runner.spec.id if mint not in held else f"{held[mint]}, {runner.spec.id}"
    rows = []
    for note in session.activity.board(session.watch, symbols, held, now):
        rows.append({
            "symbol": note.symbol or short_mint(note.mint),
            "mint": note.mint,
            "mint_short": short_mint(note.mint),
            "phase": note.phase,
            "detail": note.detail,
            "age": age_label(note.ts, now),
            "source": note.source,
        })
    return rows


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


def _boards(snap: dict | None) -> dict | None:
    if not snap:
        return None
    trenches = snap.get("trenches") or {}
    pump = snap.get("pump") or {}
    return {
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
            for row in snap.get("trending") or []
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

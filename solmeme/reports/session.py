"""What the running bot did lately: a free, deterministic session report.

`solmeme session` reads three things the live run already leaves on disk:
data/session/live.json (the UI snapshot, rewritten every minute), the run's trades in
SQLite, and its event log. Problems become flags with stable ids, so a caller can tell
whether anything is new before asking an AI to read the report.
"""

from __future__ import annotations

import gzip
import json
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from solmeme.config import Settings
from solmeme.core.store import Store

STALE_S = 180          # live.json is rewritten every 60 s; older than this, the bot is not running
QUIET_S = 60           # running, but no event for this long: the feed stalled
GAP_S = 90             # a hole this long inside the window
OTHER_WRITER_S = 120   # another session's event file touched this recently: a second bot
STOP_SLIP_PTS = 10     # stop-loss filled this many points past the stop
DRAWDOWN_PCT = (15, 30)
STREAK = 5


@dataclass
class Flag:
    id: str            # stable: the same problem keeps the same id from one report to the next
    text: str


@dataclass
class SessionReport:
    markdown: str
    run_id: str | None = None
    live: bool = False
    closed: int = 0
    flags: list[Flag] = field(default_factory=list)


def _hhmm(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M") if ts else "—"


def _usd(x) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def _k(x) -> str:
    return "—" if x is None else f"{x / 1000:.0f}k"


def load_live(settings: Settings) -> tuple[dict | None, float | None]:
    path = settings.data_dir / "session" / "live.json"
    try:
        snap = json.loads(path.read_text(encoding="utf-8"))
        return snap, time.time() - path.stat().st_mtime
    except (OSError, ValueError):
        return None, None


def session_files(events_dir: Path, session: str) -> list[Path]:
    return sorted(events_dir.glob(f"*/*-{session}.jsonl.gz"))


def event_stats(events_dir: Path, session: str, since: float) -> dict:
    """Counts by source in the window, last event per source over the whole run, and the
    longest hole inside the window."""
    window: Counter = Counter()
    before: Counter = Counter()
    last_by_source: dict[str, float] = {}
    first = last = None
    prev = None
    gap = (0.0, None, None)
    for f in session_files(events_dir, session):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    ts, src = ev.get("ts") or 0.0, ev.get("source") or "?"
                    first = ts if first is None else first
                    last = ts
                    last_by_source[src] = ts
                    if ts >= since:
                        window[src] += 1
                        if prev is not None and prev >= since and ts - prev > gap[0]:
                            gap = (ts - prev, prev, ts)
                    else:
                        before[src] += 1
                    prev = ts
        except (EOFError, OSError, zlib.error):
            continue                # the file being written ends mid-member; keep what was read
    return {"window": window, "before": before, "last_by_source": last_by_source,
            "first": first, "last": last, "gap": gap}


def other_writers(events_dir: Path, session: str, now: float) -> list[tuple[str, float]]:
    """Event files from other sessions written in the last OTHER_WRITER_S: another bot is running."""
    out = {}
    for f in events_dir.glob("*/*.jsonl.gz"):
        sid = f.name.split(".", 1)[0].partition("-")[2]
        if not sid or sid == session:
            continue
        age = now - f.stat().st_mtime
        if age <= OTHER_WRITER_S:
            out[sid] = min(age, out.get(sid, age))
    return sorted(out.items())


def _stop_levels(store: Store, trades: list[dict]) -> dict[tuple[str, str], float]:
    from solmeme.strategy.spec import spec_from_json

    levels = {}
    for sid, ver in {(t["strategy_id"], t["version"]) for t in trades}:
        row = store.get_version(sid, ver)
        if not row:
            continue
        spec = spec_from_json(row["spec_json"])
        if spec.exits.stop_loss_pct is not None:
            levels[(sid, ver)] = float(spec.p(spec.exits.stop_loss_pct))
    return levels


def _features(t: dict) -> dict:
    ef = t.get("entry_features")
    if isinstance(ef, str):
        try:
            return json.loads(ef) or {}
        except ValueError:
            return {}
    return ef or {}


def build(settings: Settings, store: Store, minutes: float, now: float | None = None) -> SessionReport:
    now = now or time.time()
    since = now - minutes * 60
    snap, age = load_live(settings)
    if snap is None:
        return SessionReport("# Session report\n\nNo session yet: `data/session/live.json` is missing. "
                             "Start the bot with `./run`.\n",
                             flags=[Flag("not_running", "No live session file.")])
    run_id = snap.get("run_id")
    live = not snap.get("ended") and age is not None and age <= STALE_S
    flags: list[Flag] = []
    if snap.get("ended"):
        flags.append(Flag("ended", f"Session ended at {_hhmm(now - age)} UTC."))
    elif not live:
        flags.append(Flag("not_running", f"No update from the bot for {age / 60:.0f} min: it is not running "
                                         "(or crashed without saving)."))

    session = str(snap.get("event_session") or "")
    ev = event_stats(settings.events_dir, session, since) if session else None
    trades = store.query("SELECT * FROM trades WHERE run_id=? ORDER BY exit_ts", (run_id,)) if run_id else []
    recent = [t for t in trades if (t["exit_ts"] or 0) >= since]
    buys = store.query("SELECT COUNT(*) AS n FROM orders WHERE run_id=? AND side='buy' AND status='filled' AND ts>=?",
                       (run_id, since))[0]["n"] if run_id else 0
    px = snap.get("sol_usd")

    # ── flags ──────────────────────────────────────────────────────────────
    if live and ev and ev["last"] and now - ev["last"] > QUIET_S:
        flags.append(Flag("stalled", f"No event recorded for {(now - ev['last']) / 60:.1f} min while the bot is up: "
                                     "the feed or the event loop is stuck."))
    if ev and ev["gap"][0] > GAP_S:
        g, a, b = ev["gap"]
        flags.append(Flag(f"gap@{_hhmm(a)}", f"No events for {g / 60:.1f} min ({_hhmm(a)}–{_hhmm(b)} UTC)."))
    if live:
        for sid, wage in other_writers(settings.events_dir, session, now):
            flags.append(Flag(f"other_bot:{sid}", f"Another bot is writing events (session {sid}, {wage:.0f}s ago). "
                                                   "Two bots double the event log and both write trades."))
    for src in snap.get("sources") or []:
        status = src.get("status")
        if status not in ("ok", "disabled", "unsupported", None):
            flags.append(Flag(f"source:{src['name']}:{status}",
                              f"Source {src['name']} is {status}: {src.get('note') or 'no detail'}."))
        if ev and ev["before"].get(src["name"], 0) >= 20 and not ev["window"].get(src["name"]):
            flags.append(Flag(f"quiet:{src['name']}", f"Source {src['name']} went quiet: events earlier, none "
                                                      f"in the last {minutes:.0f} min."))
    stops = [t for t in recent if t["exit_reason"] == "stop_loss"]
    if stops:
        levels = _stop_levels(store, stops)
        slipped = [t for t in stops if (t["strategy_id"], t["version"]) in levels
                   and t["pnl_pct"] < -(levels[(t["strategy_id"], t["version"])] + STOP_SLIP_PTS)]
        if slipped:
            avg = sum(t["pnl_pct"] for t in slipped) / len(slipped)
            flags.append(Flag("stop_gap", f"{len(slipped)} of {len(stops)} stop-losses filled 10+ points past the stop "
                                          f"(avg {avg:.0f}%)."))
    tail = trades[-STREAK:]
    if len(tail) == STREAK and all(t["pnl_sol"] <= 0 for t in tail):
        flags.append(Flag(f"streak@{tail[-1]['trade_id']}", f"Last {STREAK} closed trades all lost money."))
    wallet = snap.get("wallet") or {}
    rng = wallet.get("range") or {}
    from_high = rng.get("from_high_pct")
    for lvl in DRAWDOWN_PCT:
        if from_high is not None and from_high <= -lvl:
            flags.append(Flag(f"drawdown{lvl}", f"Wallet is {abs(from_high):.0f}% below its high."))
            break
    held = sum(s.get("balance_sol") or 0.0 for s in snap.get("strategies") or [] if s.get("in_wallet", True))
    cash = (wallet.get("balance_sol") or 0.0) - held if snap.get("shared_wallet") else None
    if live and cash is not None and cash < 0.1:
        flags.append(Flag("cash_low", f"Wallet cash is {cash:.3f} SOL: new buys are skipped or shrunk."))
    up_min = (snap.get("uptime_s") or 0) / 60
    if live and buys == 0 and up_min >= minutes:
        flags.append(Flag("idle", f"No buys in the last {minutes:.0f} min."))

    # ── markdown ───────────────────────────────────────────────────────────
    lines = [f"# Session report · {_hhmm(now)} UTC", ""]
    n_win = sum(ev["window"].values()) if ev else 0
    lines.append(f"run `{run_id}` · up {up_min:.0f} min · {'running' if live else 'not running'} · "
                 f"last {minutes:.0f} min: {n_win:,} events ({n_win / minutes:,.0f}/min)")
    lines.append("")
    lines.append("## Wallet")
    if wallet:
        unit_usd = wallet.get("balance_usd") is not None
        start = _usd(wallet.get("start_usd")) if unit_usd else f"{wallet.get('start_sol', 0):.3f} SOL"
        bal = _usd(wallet.get("balance_usd")) if unit_usd else f"{wallet.get('balance_sol', 0):.3f} SOL"
        pct = wallet.get("pnl_pct")
        line = f"{start} → {bal}" + (f" ({pct:+.1f}%)" if pct is not None else "")
        if rng.get("high") is not None:
            fmt = _usd if rng.get("unit") == "usd" else (lambda v: f"{v:.3f} SOL")
            line += f" · high {fmt(rng['high'])} ({_hhmm(rng.get('high_ts'))}) · low {fmt(rng['low'])} ({_hhmm(rng.get('low_ts'))})"
        if wallet.get("goal_usd"):
            line += f" · goal {_usd(wallet['goal_usd'])}"
        lines.append(line)
        if cash is not None:
            lines.append(f"cash {cash:.3f} SOL ({_usd(cash * px) if px else '—'}) · in positions {held:.3f} SOL")
    else:
        lines.append("_no wallet yet_")
    lines += ["", "## Problems"]
    lines += [f"- {f.text}" for f in flags] or ["- none"]

    lines += ["", f"## Trades closed in the last {minutes:.0f} min"]
    if recent:
        pnl = sum(t["pnl_sol"] for t in recent)
        wins = sum(t["pnl_sol"] > 0 for t in recent)
        lines.append(f"{len(recent)} closed, {wins} won, {pnl:+.4f} SOL ({_usd(pnl * px) if px else '—'}) · {buys} buys")
        lines += ["", "| coin | strategy | exit | P&L | peak | dip | held | liq / vol 5m / buy ratio at entry |",
                  "|---|---|---|--:|--:|--:|--:|---|"]
        for t in recent:
            f = _features(t)
            ratio = f.get("dex_buy_ratio_m5")
            lines.append(f"| {t['symbol'] or t['mint'][:6]} | {t['strategy_id']}@{t['version'][:4]} | {t['exit_reason']} | "
                         f"{t['pnl_pct']:+.0f}% | {t['mfe_x'] or 0:.2f}x | {t['mae_x'] or 0:.2f}x | {t['hold_s'] or 0:.0f}s | "
                         f"{_k(f.get('dex_liquidity_usd'))} / {_k(f.get('dex_volume_m5_usd'))} / "
                         f"{'—' if ratio is None else f'{ratio:.1f}'} |")
    else:
        lines.append(f"none · {buys} buys")

    lines += ["", "## Since the run started", "",
              "| strategy | role | open | closed | win % | P&L SOL | made |", "|---|---|--:|--:|--:|--:|--:|"]
    for s in snap.get("strategies") or []:
        if not (s.get("open") or s.get("closed")):
            continue
        role = s.get("role", "") + ("" if s.get("in_wallet", True) else " (own wallet)")
        lines.append(f"| {s['key']} | {role} | {s.get('open', 0)} | {s.get('closed', 0)} | "
                     f"{s.get('win_pct', 0):.0f} | {s.get('pnl_sol', 0):+.4f} | {_usd(s.get('made_usd'))} |")
    open_rows = [p for p in snap.get("positions") or [] if str(p.get("status", "")).startswith(("holding", "selling"))]
    lines += ["", "## Open positions"]
    if open_rows:
        lines += ["", "| coin | strategy | status | bought | entry | mark | made |", "|---|---|---|---|---|---|---|"]
        lines += [f"| {p['coin']} | {p['strategy']} | {p['status']} | {p['bought']} | {p['entry']} | {p['mark']} | {p['made']} |"
                  for p in open_rows]
    else:
        lines.append("none")

    lines += ["", f"## Sources (last {minutes:.0f} min)", "", "| source | status | events | last event |", "|---|---|--:|--:|"]
    names = sorted({s["name"] for s in snap.get("sources") or []} | set(ev["window"] if ev else ()))
    status = {s["name"]: s.get("status", "") for s in snap.get("sources") or []}
    for name in names:
        last = ev["last_by_source"].get(name) if ev else None
        lines.append(f"| {name} | {status.get(name, '')} | {ev['window'].get(name, 0) if ev else 0:,} | "
                     f"{'—' if last is None else f'{now - last:.0f}s ago'} |")
    return SessionReport("\n".join(lines) + "\n", run_id=run_id, live=live, closed=len(trades), flags=flags)

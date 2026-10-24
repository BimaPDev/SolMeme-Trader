"""Wallets the council chose to track for you (from "Ask the council" on the dashboard).

A wallet you ask about goes in when the judges vote Track, and you can remove it any time. Every 4 hours each
one is checked again (ai/ask.py: recheck_due): a ban or a money link to a banned wallet removes it at once
without asking anyone; the judges are asked again only when its record moved or a day has passed, and an
Avoid (or two Watch in a row) removes it. Removals are logged so Hermes can tell you.

They live in <data>/my_wallets.json, apart from the whale book (which every swarm generation rebuilds), and
the live session merges them in each minute: their buys count as whale buys (whale_buys feature, the swarm's
bet-boost gene) and GMGN polls them live next to the top whales. Capped, because every tracked wallet is one
more GMGN call every 30 s.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

FILE = "my_wallets.json"
MAX = 12
RECHECK_S = 4 * 3600
REJUDGE_S = 24 * 3600
KEEP_REMOVED = 50


def _read(data_dir: Path) -> dict:
    try:
        d = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
        return {"wallets": d.get("wallets", {}), "removed": d.get("removed", [])}
    except (OSError, ValueError):
        return {"wallets": {}, "removed": []}


def _write(data_dir: Path, d: dict) -> None:
    path = Path(data_dir) / FILE
    tmp = path.with_suffix(".tmp")
    d["removed"] = d.get("removed", [])[-KEEP_REMOVED:]
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(path)


def load(data_dir: Path) -> dict[str, dict]:
    return _read(data_dir)["wallets"]


def removed(data_dir: Path) -> list[dict]:
    return _read(data_dir)["removed"]


def add(data_dir: Path, address: str, note: str = "", verdict: str = "", by: str = "you", fingerprint: dict | None = None) -> dict:
    d = _read(data_dir)
    w = d["wallets"]
    if address not in w and len(w) >= MAX:
        return {"error": f"already tracking {MAX} wallets; remove one first"}
    now = time.time()
    w[address] = {**w.get(address, {}), "ts": w.get(address, {}).get("ts", now), "note": note[:200], "verdict": verdict,
                  "by": by, "checked_ts": now, "judged_ts": now, "watch_streak": 0, "fingerprint": fingerprint or {}}
    _write(data_dir, d)
    return {"ok": True, "wallets": w}


def update(data_dir: Path, address: str, **fields) -> None:
    d = _read(data_dir)
    if address in d["wallets"]:
        d["wallets"][address].update(fields)
        _write(data_dir, d)


def remove(data_dir: Path, address: str, reason: str = "removed by you", by: str = "you") -> dict:
    d = _read(data_dir)
    if d["wallets"].pop(address, None) is not None:
        d["removed"].append(dict(address=address, ts=time.time(), reason=reason[:240], by=by))
    _write(data_dir, d)
    return {"ok": True, "wallets": d["wallets"]}


def due(data_dir: Path, now: float | None = None) -> list[str]:
    now = now or time.time()
    return [a for a, e in load(data_dir).items() if now - (e.get("checked_ts") or e.get("ts") or 0) >= RECHECK_S]


# ── what the tracked wallets traded (survives restarts; the market state only keeps ~45 min after one) ──
TRADES_FILE = "my_wallet_trades.jsonl"
TRADES_MAX_BYTES = 5_000_000


def record_trade(data_dir: Path, row: dict) -> None:
    path = Path(data_dir) / TRADES_FILE
    try:
        if path.exists() and path.stat().st_size > TRADES_MAX_BYTES:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def trades_for(data_dir: Path, wallet: str, limit: int = 40) -> list[dict]:
    """This wallet's recorded trades, newest first."""
    path = Path(data_dir) / TRADES_FILE
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[::-1]:
            if wallet in line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("wallet") == wallet:
                    out.append(r)
                    if len(out) >= limit:
                        break
    except OSError:
        pass
    return out


# ── the whale tape: every pump.fun / GMGN trade by a whale in the book, for the Whales page and alerts ──
WHALE_FILE = "whale_trades.jsonl"


def record_whale_trade(data_dir: Path, row: dict) -> None:
    path = Path(data_dir) / WHALE_FILE
    try:
        if path.exists() and path.stat().st_size > TRADES_MAX_BYTES * 2:
            lines = path.read_text(encoding="utf-8").splitlines()
            path.write_text("\n".join(lines[len(lines) // 2:]) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass


def whale_trades(data_dir: Path, since: float = 0.0, limit: int = 300) -> list[dict]:
    """Newest first."""
    path = Path(data_dir) / WHALE_FILE
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[::-1]:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ts", 0) < since:
                break
            out.append(r)
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out

#!/usr/bin/env python3
"""Hermes cron (--no-agent, every 5 min): print an alert only when something needs attention."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solmeme_lib as lib

now = time.time()
st = lib.load_state()
try:
    snap = lib.get("/api/snapshot")
    ok = True
except Exception:  # noqa: BLE001 - an unreachable bot is the alert
    snap, ok = None, False
msgs = lib.check_down(st, now, ok)
if ok:
    try:
        hist = lib.get("/api/history")["wallet"]
    except Exception:  # noqa: BLE001
        hist = None
    msgs += lib.check_snapshot(snap, hist, st, now)
    msgs += lib.check_generation(snap, st, lambda: lib.get("/api/swarm", timeout=40))
    msgs += lib.check_live(snap, st)
    msgs += lib.check_browsers(snap, st, now)
    try:
        msgs += lib.check_whale_clusters(lib.get("/api/whale-tape", timeout=30), st, now)
    except Exception:  # noqa: BLE001
        pass
    try:
        msgs += lib.check_my_wallets(lib.get("/api/my-wallets"), st)
    except Exception:  # noqa: BLE001
        pass
    try:
        msgs += lib.check_live_trades(lib.get("/api/live", timeout=30), st)
    except Exception:  # noqa: BLE001 - the trade alerts must not stop the rest
        pass
lib.save_state(st)
print("\n\n".join(msgs))

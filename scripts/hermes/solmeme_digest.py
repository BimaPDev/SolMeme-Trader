#!/usr/bin/env python3
"""Hermes cron (--no-agent, morning and evening): a short status digest."""
import datetime as dt, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solmeme_lib as lib

now = time.time()
st = lib.load_state()
try:
    snap = lib.get("/api/snapshot")
except Exception as exc:  # noqa: BLE001
    print(f"📊 SolMeme digest: the bot is not responding ({exc}). Check http://<bot-host>")
    sys.exit(0)
try:
    swarm = lib.get("/api/swarm", timeout=30)
except Exception:  # noqa: BLE001
    swarm = None
msg = lib.digest(snap, swarm, st, now, dt.datetime.now())
lib.save_state(st)
print(msg)

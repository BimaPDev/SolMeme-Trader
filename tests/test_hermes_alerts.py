"""Hermes Telegram alerts: each fires once, re-arms after it clears, and stays silent when all is well."""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "hermes"))
import solmeme_lib as lib  # noqa: E402


def snap(pnl=0.0, bal=None, uptime=7200, closed=10, dex=1000, gmgn="ok", running=False, started=None):
    bal = 50 * (1 + pnl / 100) if bal is None else bal
    return {"uptime_s": uptime, "sol_usd": 110.0,
            "wallet": {"balance_usd": bal, "start_usd": 50.0, "pnl_pct": (bal / 50 - 1) * 100, "locked_usd": bal - 50,
                       "open_usd": 0.0, "goal_usd": 100, "range": {"low": min(bal, 50), "high": max(bal, 50)}},
            "strategies": [{"key": "holder_dip@abcdef1", "role": "champion", "closed": closed, "open": 1, "pnl_sol": 0.1}],
            "sources": [{"name": "dexscreener", "status": "ok", "events": dex}, {"name": "pumpportal", "status": "ok", "events": dex},
                        {"name": "gmgn", "status": gmgn, "events": 5}],
            "swarm_clock": {"running": running, "phase": "council", "started_ts": started}, "bans": {"count": 5}, "whales": []}


T0 = 1_000_000.0                     # epoch-like times: cooldowns compare against "never"


def run(s, st, now, hist=None, uptime=None):
    s["uptime_s"] = (now - T0 + 7200) if uptime is None else uptime       # the bot keeps running while time passes
    return lib.check_snapshot(s, hist, st, now)


def test_quiet_when_all_is_well():
    st = {}
    assert run(snap(), st, T0 + 1000.0) == [] and run(snap(dex=1100), st, T0 + 1300.0) == []


def test_down_alert_needs_two_failures_then_recovers():
    st = {}
    assert lib.check_down(st, 0.0, False) == []                                  # one blip: nothing
    assert "not responding" in lib.check_down(st, 300.0, False)[0]
    assert lib.check_down(st, 600.0, False) == []                                # already told you
    assert "still not responding" in lib.check_down(st, 300.0 + 3 * 3600 + 1, False)[0]
    assert "answering again" in lib.check_down(st, 20000.0, True)[0]
    assert lib.check_down(st, 20300.0, True) == []


def test_restart_is_noticed_from_uptime():
    st = {}
    run(snap(), st, T0 + 1000.0)
    assert run(snap(), st, T0 + 1300.0) == []                              # normal growth
    assert "restarted" in run(snap(), st, T0 + 1600.0, uptime=60)[0]


def test_drop_and_gain_levels_fire_once_and_rearm():
    st = {}
    assert "down 15%" in run(snap(pnl=-18), st, T0 + 1000.0)[0]
    assert run(snap(pnl=-20), st, T0 + 1300.0) == []                                  # same level, no repeat
    assert "down 25%" in run(snap(pnl=-26), st, T0 + 1600.0)[0]
    run(snap(pnl=-5), st, T0 + 1900.0)                                                # recovered: levels re-arm
    assert "down 15%" in run(snap(pnl=-16), st, T0 + 2200.0)[0]
    assert any("up 50%" in m for m in run(snap(pnl=55), st, T0 + 2500.0))
    assert not any("up 50%" in m for m in run(snap(pnl=58), st, T0 + 2800.0))


def test_sharp_30_minute_move_has_a_cooldown():
    st = {}
    hist = [[T0 + 1000.0 + i * 15, 50.0 + i * 0.1] for i in range(100)]               # +~20% in 25 min
    msgs = run(snap(bal=59.9), st, T0 + 2500.0, hist)
    assert any("moved +" in m for m in msgs)
    assert not any("moved" in m for m in run(snap(bal=59.9), st, T0 + 2600.0, hist))


def test_idle_and_stalled_feeds_and_gmgn():
    st = {}
    run(snap(closed=10, dex=1000, gmgn="backoff"), st, T0 + 1000.0)
    msgs = run(snap(closed=10, dex=1000, gmgn="backoff"), st, T0 + 7000.0)
    text = " ".join(msgs)
    assert "No trades have closed" in text and "'dexscreener' has received nothing" in text and "GMGN has been rate-limited" in text
    assert run(snap(closed=10, dex=1000, gmgn="backoff"), st, T0 + 7300.0) == []   # once only
    assert run(snap(closed=11, dex=1200, gmgn="ok"), st, T0 + 7600.0) == []        # healthy again re-arms silently


def test_long_generation_is_reported_once():
    st = {}
    s = snap(running=True, started=T0 + 1000.0)
    assert "generation has been running" in run(s, st, T0 + 5000.0)[0]
    assert run(s, st, T0 + 5300.0) == []


def test_digest_and_checkpoint_row():
    st = {}
    swarm = {"generation": 12, "best": [{"key": "holder_dip@abcdef1", "equity": 3.5, "trades": 40}],
             "agents": [{"status": "swarm"}, {"status": "swarm"}, {"status": "dead"}]}
    text = lib.digest(snap(pnl=20), swarm, st, 5000.0, dt.datetime(2026, 10, 9, 21, 0))
    assert "Wallet $60.00" in text and "Best performing: holder_dip abcdef1 350%" in text and "2 alive, 1 dead" in text
    assert "CHECKPOINT: 24-hour" in text and "Log row" in text
    later = lib.digest(snap(pnl=30), swarm, st, 5000.0 + 12 * 3600, dt.datetime(2026, 10, 10, 9, 0))
    assert "Since last digest (12 h ago): +$5.00" in later and "CHECKPOINT" not in later


def _swarm(gen, events=(), lessons=(), ideas=(), gens=None):
    return {"generation": gen, "events": list(events), "lessons": list(lessons), "council": {"ideas": list(ideas)},
            "generations": gens if gens is not None else [{"gen": g, "ts": 100.0 * g} for g in range(1, gen + 1)],
            "best": [{"key": "holder_dip@70b5254b90", "equity": 25.0, "trades": 160}]}


def test_one_report_per_finished_generation():
    st, calls = {}, []
    sw = _swarm(37, events=[
        {"gen": 37, "event": "born", "key": "holder_dip@abc1234567", "ops": ["boost:add", "lesson:is_pump:<"], "reason": ""},
        {"gen": 37, "event": "killed", "key": "kol_follow@def1234567", "ops": [], "reason": "losing: 5 trades, 0 won"},
        {"gen": 37, "event": "retired", "key": "holder_dip@0001234567", "ops": [], "reason": "retired to make room"},
        {"gen": 36, "event": "born", "key": "holder_dip@old1234567", "ops": [], "reason": ""}],
        lessons=[{"strategy_id": "holder_dip", "feature": "dex_buys_m5", "op": ">", "v": 120.0, "saved_sol": 2.0, "blocked": 41, "of": 533}],
        ideas=[{"gen": 37, "approved": True, "strategy_id": "holder_dip", "kind": "avoid", "feature": "age_s", "op": ">=", "v": 110.0, "proposed_by": "cursor-gpt"},
               {"gen": 37, "approved": False, "strategy_id": "kol_follow", "kind": "require", "feature": "x", "op": ">=", "v": 1.0}])
    fetch = lambda: calls.append(1) or sw                                            # noqa: E731
    s = snap()
    s["swarm_clock"] = {"running": True, "gen": 37}                                  # installed while 37 runs
    assert lib.check_generation(s, st, fetch) == [] and st["gen_reported"] == 36 and not calls
    assert lib.check_generation(s, st, fetch) == [] and not calls                    # nothing due: no API call
    s["swarm_clock"] = {"running": False, "gen": 37}
    msg = lib.check_generation(s, st, fetch)[0]                                      # it finished: one report
    assert "generation 37 finished" in msg and "whale bet boost" in msg and "Killed kol_follow" in msg
    assert "Retired 1" in msg and "dex_buys_m5" in msg and "don't buy when age_s >= 110" in msg and "old1234" not in msg
    assert lib.check_generation(s, st, fetch) == []                                  # never twice
    s["swarm_clock"] = {"running": True, "gen": 38}                                  # 38 starts: 37 already told
    assert lib.check_generation(s, st, fetch) == []

    st2 = {"gen_reported": 36}                                                       # 38 started the moment 37 ended
    assert "generation 37 finished" in lib.check_generation(s, st2, fetch)[0]        # 37, never 36
    sw["generation"] = 38                                                            # 38 recorded and running...
    s["swarm_clock"] = {"running": True, "gen": 39}                                  # ...then done, 39 running unrecorded
    late = lib.check_generation(s, st2, fetch)[0]
    assert "generation 38 finished" in late and "What it learned" in late           # the API's lessons are 38's
    sw["generation"] = 39
    s["swarm_clock"] = {"running": True, "gen": 40}
    late39 = lib.check_generation(s, st2, fetch)[0]
    sw["generation"] = 40
    assert "generation 39 finished" in late39
    s["swarm_clock"] = {"running": True, "gen": 40}                                  # a restart re-runs 40: nothing new
    assert lib.check_generation(s, st2, fetch) == []
    sw["generation"] = 41                                                            # 41 already recorded (judged)...
    s["swarm_clock"] = {"running": True, "gen": 41}
    assert "What it learned" not in lib.check_generation(s, st2, fetch)[0]           # ...so the lessons are 41's: left out of 40
    s["swarm_clock"] = {"gen": None}
    assert lib.check_generation(s, st2, fetch) == []                                 # an older bot: stay quiet


def test_live_loss_limit_and_new_attempt_alerts():
    st = {}
    s = snap()
    s["dryrun"] = {"attempt": 1, "stopped": None, "equity_usd": 40.0, "pnl_pct": -20.0, "start_usd": 50.0, "paper_pnl_pct": -5.0}
    assert lib.check_live(s, st) == []
    s["dryrun"].update(stopped="hit the 50% loss limit", equity_usd=24.0, pnl_pct=-52.0)
    msg = lib.check_live(s, st)
    assert len(msg) == 1 and "loss limit" in msg[0] and "attempt #2" in msg[0]
    assert lib.check_live(s, st) == []                                        # once
    s["dryrun"].update(attempt=2, stopped=None, equity_usd=50.0, pnl_pct=0.0)
    assert "attempt #2 started" in lib.check_live(s, st)[0]
    assert lib.check_live(s, st) == []


def test_live_trade_alerts_report_each_fill_once():
    import solmeme_lib as lib

    tape = [dict(ts=1.0, side="buy", key="kol_follow@e820edf739", mint="Gm" * 20, symbol="GATE", sol=0.05,
                 why="kol_buys_600s 2.00,mcap_sol 1,221,secs_since_first_kol 91.86", size_mult=1.0),
            dict(ts=2.0, side="sell", key="kol_follow@e820edf739", mint="Gm" * 20, symbol="GATE", sol=0.054,
                 pnl_sol=-0.0063, paper_pnl_pct=-22.0, why="kol_sold", x=1.02),
            dict(ts=3.0, side="missed", key="k", mint="Jm" * 20, symbol="JUAN", why="slippage: real price +108%")]
    st = {}
    assert lib.check_live_trades({"tape": tape, "sol_usd": 100.0}, st) == []      # first run: no replay
    st["live_tape_ts"] = 0.5
    msgs = lib.check_live_trades({"tape": tape, "sol_usd": 100.0}, st)
    assert msgs[0].startswith("🟢 Live BUY $GATE: $5.00") and "mcap_sol 1,221" in msgs[0]
    assert msgs[1].startswith("🔻 Live SELL $GATE: -10% (-$0.63), kol_sold")      # after fees, not the price ratio
    assert msgs[2].startswith("⏭ Live skipped 1 buy(s)") and "$JUAN" in msgs[2]
    assert lib.check_live_trades({"tape": tape, "sol_usd": 100.0}, st) == []      # each fill once


def test_a_logged_out_browser_source_is_reported_once():
    import solmeme_lib as lib

    snap = lambda n: {"sources": [{"name": "fomo", "status": "ok", "events": n}, {"name": "gmgn", "status": "ok", "events": 1}]}
    st = {}
    assert lib.check_browsers(snap(0), st, 0) == []
    assert lib.check_browsers(snap(0), st, 1800) == []
    msgs = lib.check_browsers(snap(0), st, 3700)
    assert len(msgs) == 1 and msgs[0].startswith("🔑 fomo") and "solmeme login fomo" in msgs[0]
    assert lib.check_browsers(snap(0), st, 9000) == []                 # once
    assert lib.check_browsers(snap(5), st, 9100)[0].startswith("✅ fomo")


def test_council_wallet_changes_are_reported_but_not_your_own():
    import solmeme_lib as lib

    st = {}
    assert lib.check_my_wallets({"wallets": {}, "removed": []}, st) == []
    W = "Whale" + "1" * 35
    msgs = lib.check_my_wallets({"wallets": {W: {"by": "council"}}, "removed": []}, st)
    assert len(msgs) == 1 and msgs[0].startswith("🐋 The council added wallet Whale1")
    msgs = lib.check_my_wallets({"wallets": {}, "removed": [{"address": W, "ts": 10, "reason": "4-hour check: judges now say Avoid", "by": "check"},
                                                               {"address": "Mine" + "2" * 36, "ts": 11, "reason": "removed by you", "by": "you"}]}, st)
    assert msgs == ["🧹 Wallet Whale1…1111 was removed from tracking: 4-hour check: judges now say Avoid"]


def test_a_whale_cluster_is_reported_once_per_coin():
    import solmeme_lib as lib

    tape = {"clusters": [{"mint": "Hot" + "1" * 37, "symbol": "HOT", "whales": 4, "trusted": 1, "mcap_usd": 42000.0}]}
    st = {}
    msgs = lib.check_whale_clusters(tape, st, 0.0)
    assert len(msgs) == 1 and msgs[0].startswith("🐋 4 whales (1 trusted by the council) bought $HOT") and "$42,000.00" in msgs[0]
    assert lib.check_whale_clusters(tape, st, 60.0) == []                       # once
    assert len(lib.check_whale_clusters(tape, st, 7 * 3600.0)) == 1             # re-arms after 6 h


def test_overseer_blocks_are_reported():
    import solmeme_lib as lib

    st = {"live_tape_ts": 0.0}
    tape = [dict(ts=1.0, side="blocked", key="k", mint="M" * 40, symbol="X", why="overseer: data feed down: pumpfun quiet 300 s")]
    msgs = lib.check_live_trades({"tape": tape, "sol_usd": 100.0}, st)
    assert msgs == ["🛡 The Overseer stopped 1 live buy(s): $X: data feed down: pumpfun quiet 300 s"]

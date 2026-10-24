"""`solmeme session`: the free report on the running bot, and when it asks an AI."""

import json
import subprocess
import time
from io import StringIO

from rich.console import Console

from solmeme.ai import session_review
from solmeme.ai.session_review import ask_claude, review_once, should_ask
from solmeme.core.eventlog import EventLogWriter
from solmeme.core.events import SolPrice
from solmeme.core.store import Store
from solmeme.reports.session import Flag, SessionReport, build
from solmeme.sim.live import LiveSession

MINT = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"


def _trade(run_id, spec, n, *, reason, pnl_pct, exit_ts):
    size = 0.104
    return dict(trade_id=f"{spec.key}:{MINT}:{n}", run_id=run_id, strategy_id=spec.id, version=spec.version,
                mint=MINT, symbol=f"OTTR{n}", entry_ts=exit_ts - 60, exit_ts=exit_ts, entry_price=1e-6,
                exit_price=1e-6, size_sol=size, proceeds_sol=size * (1 + pnl_pct / 100),
                pnl_sol=size * pnl_pct / 100, pnl_pct=pnl_pct, fees_sol=0.007, exit_reason=reason, hold_s=60.0,
                mfe_x=1.0, mae_x=0.6, peak_after_exit_x=None, failed_attempts=0,
                entry_features={"dex_liquidity_usd": 17000, "dex_volume_m5_usd": 40000, "dex_buy_ratio_m5": 1.4},
                exit_features={}, fills=[])


def _live_session_on_disk(settings, specs, now):
    """What a running bot leaves behind: a run, trades, an event log with a hole, live.json,
    and a second bot's event file."""
    store = Store(settings.db_path)
    spec = specs["migration_momentum"]                       # stop loss 25%
    store.upsert_version(spec.id, spec.version, spec.canonical_json(), "champion")
    run_id = store.start_run("live")
    store.insert_trade(_trade(run_id, spec, 1, reason="stop_loss", pnl_pct=-40.0, exit_ts=now - 300))
    store.insert_trade(_trade(run_id, spec, 2, reason="trailing_stop", pnl_pct=30.0, exit_ts=now - 120))
    store.insert_order(dict(run_id=run_id, strategy_id=spec.id, version=spec.version, mint=MINT, side="buy",
                            ts=now - 400, status="filled", reason="filled", cost_sol=0.004))
    ours = EventLogWriter(settings.events_dir)
    for ts in [now - 600 + 10 * i for i in range(20)] + [now - 200 + 10 * i for i in range(19)]:
        ours.write(SolPrice(ts=ts, source="dexscreener", usd=120.0))   # hole from now-410 to now-200
    ours.close()
    other = EventLogWriter(settings.events_dir)
    other.session = "1" + ours.session[1:] if ours.session[0] != "1" else "2" + ours.session[1:]
    other.write(SolPrice(ts=now - 5, source="sol_price", usd=120.0))
    other.close()
    snap = {
        "run_id": run_id, "event_session": ours.session, "started": now - 1200, "ended": False, "uptime_s": 1200,
        "sol_usd": 120.0, "shared_wallet": True,
        "wallet": {"start_usd": 50.0, "balance_usd": 45.0, "pnl_pct": -10.0, "start_sol": 0.41, "balance_sol": 0.37,
                   "goal_usd": 100.0, "range": {"unit": "usd", "high": 61.9, "high_ts": now - 900, "low": 44.0,
                                                "low_ts": now - 60, "from_high_pct": -27.3}},
        "strategies": [{"key": spec.key, "role": "champion", "open": 1, "closed": 2, "win_pct": 50.0,
                        "pnl_sol": 0.0, "made_usd": 0.0, "balance_sol": 0.1}],
        "positions": [{"coin": "WIF", "strategy": spec.id, "status": "holding", "bought": "0.104", "entry": "1e-6",
                       "sold": "—", "mark": "1.1x", "made": "+$1.20"}],
        "sources": [{"name": "dexscreener", "status": "ok"}, {"name": "fomo", "status": "backoff", "note": "timeout"}],
    }
    path = settings.data_dir / "session" / "live.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap))
    return store, other.session


def test_report_flags_problems_from_what_the_bot_left_on_disk(settings, specs):
    now = time.time()
    store, other = _live_session_on_disk(settings, specs, now)
    r = build(settings, store, 15, now)
    ids = {f.id for f in r.flags}
    assert r.live and r.closed == 2
    assert "stop_gap" in ids                                 # -40% on a 25% stop
    assert any(i.startswith("gap@") for i in ids)            # 3.5 min without events
    assert f"other_bot:{other}" in ids
    assert "source:fomo:backoff" in ids
    assert "drawdown15" in ids                               # 27% below the high
    assert "cash_low" not in ids and "idle" not in ids       # 0.27 SOL cash, one buy in the window
    assert "OTTR1" in r.markdown and "| WIF |" in r.markdown and "$61.90" in r.markdown


def test_no_session_file_is_reported_not_raised(settings):
    r = build(settings, Store(settings.db_path), 15)
    assert not r.live and [f.id for f in r.flags] == ["not_running"]


def test_ai_is_asked_only_when_something_changed():
    rep = SessionReport("x", run_id="r", live=True, closed=3, flags=[Flag("stop_gap", "...")])
    assert should_ask({}, rep)[0]
    assert not should_ask({"closed": 3, "flags": ["stop_gap"]}, rep)[0]
    assert should_ask({"closed": 2, "flags": ["stop_gap"]}, rep)[0]
    assert should_ask({"closed": 3, "flags": []}, rep)[0]
    assert not should_ask({}, SessionReport("x", run_id="r", live=False, closed=3))[0]


def test_review_once_with_local_model_calls_once_then_waits_for_news(settings, specs, monkeypatch):
    now = time.time()
    store, _ = _live_session_on_disk(settings, specs, now)
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "Stops are slipping."}}],
                    "usage": {"prompt_tokens": 900, "completion_tokens": 40}}

    monkeypatch.setattr(session_review.httpx, "post", lambda url, **kw: calls.append((url, kw)) or Resp())
    first = review_once(settings, store, 15, "local", now=now)
    assert len(calls) == 1 and calls[0][0].endswith("/chat/completions")
    assert "Stops are slipping." in first.text and first.path.exists()
    assert store.llm_spend_since(0) == 0.0 and store.query("SELECT purpose FROM llm_usage")[0]["purpose"] == "session_review"
    second = review_once(settings, store, 15, "local", now=now + 1)
    assert len(calls) == 1 and second.ai_status.startswith("no AI call")
    assert "Stops are slipping." not in second.text


def test_claude_runs_headless_with_no_tools(settings, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["input"] = cmd, kw.get("input")
        out = {"type": "result", "is_error": False, "result": "All quiet.", "total_cost_usd": 0.02,
               "usage": {"input_tokens": 10, "output_tokens": 5}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(out), stderr="")

    monkeypatch.setattr(session_review.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(session_review.subprocess, "run", fake_run)
    text, usage = ask_claude(settings.ai.session_review, "the report")
    cmd = seen["cmd"]
    assert text == "All quiet." and usage["cost_usd"] == 0.02
    assert "-p" in cmd and cmd[cmd.index("--tools") + 1] == "" and "--no-session-persistence" in cmd
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd and "--bare" not in cmd
    assert seen["input"] == "the report"


def test_live_session_dumps_state_for_the_session_command(settings, specs):
    console = Console(file=StringIO(), force_terminal=False, width=120)
    session = LiveSession(settings, Store(settings.db_path), [(specs["migration_momentum"], "champion")],
                          console=console, warmup_min=0, ai=False)
    session._dump_state()
    snap = json.loads((settings.data_dir / "session" / "live.json").read_text())
    assert snap["run_id"] == session.run_id and snap["event_session"] == session.writer.session
    assert snap["ended"] is False and "tokens" not in snap and "wallet" in snap


def test_web_serves_the_latest_report_and_ai_note(tmp_path):
    import urllib.request

    from solmeme.ui.server import Hub, serve

    hub = Hub()
    httpd, port = serve(hub, tmp_path, port=0)
    try:
        def get():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/session-report") as r:
                return json.loads(r.read())

        assert get() == {"markdown": None, "updated": None, "note": None, "note_at": None, "next_at": None}
        hub.report_dir = tmp_path / "live-1"
        hub.report_dir.mkdir()
        (hub.report_dir / "latest.md").write_text("# Session report · 05:15 UTC\n\n## Problems\n- none\n")
        (hub.report_dir / "state.json").write_text(json.dumps({"note": "All quiet.", "note_at": "05:15", "closed": 0}))
        got = get()
        assert got["markdown"].startswith("# Session report") and got["updated"]
        assert got["note"] == "All quiet." and got["note_at"] == "05:15"
        (tmp_path / "next.json").write_text(json.dumps({"next_at": 123.0, "every_min": 15}))
        assert get()["next_at"] == 123.0
    finally:
        httpd.shutdown()
        httpd.server_close()

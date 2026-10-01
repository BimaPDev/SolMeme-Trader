import time
from types import SimpleNamespace

from solmeme.sim.activity import TokenActivity, age_label, short_mint


def test_note_latest_cap_unknown_and_symbol():
    act = TokenActivity(per_phase=2)
    act.note("a", "checking", "first", symbol="SOL", ts=1)
    act.note("a", "checking", "second", ts=2)
    act.note("a", "not-a-phase", "ignored", ts=3)
    act.note("b", "checking", "b", ts=4)
    act.note("a", "checking", "refreshed", ts=5)
    act.note("c", "checking", "c", ts=6)
    act.note("z", "buying", "still here", symbol="ZED", ts=1)

    kept = act.recent("checking", 10)
    assert [(n.mint, n.detail, n.symbol) for n in kept] == [
        ("c", "c", ""),
        ("a", "refreshed", "SOL"),
    ]
    assert act.recent("not-a-phase", 5) == []
    assert act.recent("buying", 5)[0].symbol == "ZED"


def test_recent_is_newest_first():
    act = TokenActivity()
    act.note("old", "analyzing", "old", ts=1)
    act.note("new", "analyzing", "new", ts=2)
    act.note("old", "analyzing", "refreshed", ts=3)
    assert [n.mint for n in act.recent("analyzing", 10)] == ["old", "new"]
    assert act.recent("analyzing", 1)[0].detail == "refreshed"


def test_note_defaults_ts_to_now():
    act = TokenActivity()
    before = time.time()
    act.note("m", "watching", "x")
    after = time.time()
    assert before <= act.recent("watching", 1)[0].ts <= after


def test_board_sections_dedup_and_symbols():
    act = TokenActivity()
    now = 1_000.0
    act.note("HELD", "selling", "taking profit", ts=900, source="exit")
    act.note("HELD", "analyzing", "should not duplicate", ts=950)
    act.note("HELD", "checking", "also hidden", ts=990)
    act.note("ANA", "analyzing", "curve 40%", ts=800)
    act.note("ANA", "checking", "hidden by analyze", ts=990)
    act.note("CHK", "checking", "security", ts=700)

    symbols = {"OPEN": "OP", "HELD": "HLD", "ANA": "AN", "CHK": "CK", "WATCH": "WT"}
    held = {"OPEN": "trench", "HELD": "kol_follow"}
    watch = SimpleNamespace(
        ttl_s=500,
        launch_window_s=100,
        reasons={"WATCH": {"launch": 950, "kol": 600}},
        pinned=set(),
        priority_list=lambda _now: ["HELD", "ANA", "CHK", "WATCH"],
    )

    rows = act.board(watch, symbols, held, now)
    assert [(r.mint, r.phase, r.detail, r.symbol) for r in rows] == [
        ("OPEN", "holding", "trench open", "OP"),
        ("HELD", "selling", "taking profit", "HLD"),
        ("ANA", "analyzing", "curve 40%", "AN"),
        ("CHK", "checking", "security", "CK"),
        ("WATCH", "watching", "kol", "WT"),
    ]
    assert rows[0].ts == now
    assert rows[1].ts == 900 and rows[1].source == "exit"


def test_short_mint_and_age_label():
    assert short_mint("abcd") == "abcd"
    assert short_mint("123456789012") == "123456789012"
    assert short_mint("1234567890123") == "1234…0123"
    assert age_label(100, 100) == "0s"
    assert age_label(100, 112) == "12s"
    assert age_label(100, 280) == "3m"
    assert age_label(100, 7300) == "2h"
    assert age_label(100, 50) == "0s"

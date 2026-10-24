"""SQLite (WAL) for operational state: strategy versions, journal, proposals,
lessons, LLM usage. Raw market events live in the event log, not here."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,              -- champion | challenger | trial | swarm | retired | rejected | candidate | dead
    parent_version TEXT,
    created_ts REAL NOT NULL,
    status_ts REAL NOT NULL,
    note TEXT DEFAULT '',
    PRIMARY KEY (strategy_id, version)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,                -- live | replay | backtest
    started_ts REAL NOT NULL,
    ended_ts REAL,
    data_from REAL, data_to REAL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS trades (
    trade_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    entry_ts REAL, exit_ts REAL,
    entry_price REAL, exit_price REAL,
    size_sol REAL, proceeds_sol REAL, pnl_sol REAL, pnl_pct REAL, fees_sol REAL,
    exit_reason TEXT,
    hold_s REAL,
    mfe_x REAL, mae_x REAL, peak_after_exit_x REAL,
    failed_attempts INTEGER DEFAULT 0,
    entry_features TEXT, exit_features TEXT, fills TEXT
);
CREATE INDEX IF NOT EXISTS trades_by_strategy ON trades(strategy_id, version, exit_ts);
CREATE TABLE IF NOT EXISTS orders (
    run_id TEXT, strategy_id TEXT, version TEXT, mint TEXT, side TEXT,
    ts REAL, status TEXT, reason TEXT, cost_sol REAL
);
CREATE TABLE IF NOT EXISTS near_misses (
    run_id TEXT, strategy_id TEXT, version TEXT, mint TEXT, ts REAL,
    failed_rule TEXT, price REAL, mcap_sol REAL,
    PRIMARY KEY (run_id, strategy_id, version, mint)
);
CREATE TABLE IF NOT EXISTS token_outcomes (
    mint TEXT PRIMARY KEY, symbol TEXT, first_ts REAL, first_mcap_sol REAL,
    max_mcap_sol REAL, max_mcap_ts REAL, last_mcap_sol REAL, last_ts REAL, migrated INTEGER
);
CREATE TABLE IF NOT EXISTS proposals (
    proposal_id TEXT PRIMARY KEY,
    created_ts REAL, strategy_id TEXT, base_version TEXT, challenger_version TEXT,
    hypothesis TEXT, changes_json TEXT, change_hash TEXT, expected_effect TEXT,
    status TEXT,                      -- pending | backtest_failed | shadow | promoted | rejected | invalid | duplicate
    backtest_json TEXT, shadow_json TEXT, shadow_started_ts REAL, decided_ts REAL, reason TEXT,
    review_id TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY, ts REAL, strategy_id TEXT, version TEXT,
    window_from REAL, window_to REAL, model TEXT, cost_usd REAL, result_json TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
    ts REAL, purpose TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cache_read INTEGER, cache_write INTEGER, cost_usd REAL
);
CREATE TABLE IF NOT EXISTS evo_log (
    ts REAL, gen INTEGER, strategy_id TEXT, version TEXT, parent_version TEXT, mate_version TEXT,
    ops TEXT,                         -- json list of mutation signatures
    event TEXT,                       -- born | screened_out | killed | promoted
    fitness REAL, parent_fitness REAL, reason TEXT
);
CREATE INDEX IF NOT EXISTS evo_log_by_version ON evo_log(version, event);
CREATE TABLE IF NOT EXISTS evo_scores (
    gen INTEGER, ts REAL, key TEXT, role TEXT, replay REAL, replay_n INTEGER,
    live_n INTEGER, live_pnl_sol REAL, equity REAL, fitness REAL
);
CREATE TABLE IF NOT EXISTS evo_lessons (
    gen INTEGER, ts REAL, strategy_id TEXT, feature TEXT, op TEXT, v REAL,
    saved_sol REAL, blocked INTEGER, n INTEGER
);
CREATE TABLE IF NOT EXISTS council_ideas (
    idea_id TEXT PRIMARY KEY, ts REAL, gen INTEGER, strategy_id TEXT,
    feature TEXT, op TEXT, v REAL, kind TEXT,     -- kind: require (entry.all) | avoid (entry.none)
    reason TEXT, proposed_by TEXT, votes_json TEXT, score REAL, approved INTEGER, in_sample_json TEXT,
    status TEXT,                      -- unhelpful | rejected | testing | working | failed
    status_reason TEXT, carriers INTEGER DEFAULT 0, live_trades INTEGER DEFAULT 0,
    live_pnl_sol REAL DEFAULT 0, deaths INTEGER DEFAULT 0, replay_delta REAL
);
CREATE TABLE IF NOT EXISTS council_calls (
    ts REAL, member TEXT, purpose TEXT, seconds REAL, ok INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS council_messages (     -- a sitting as a chat: what each judge said, as it happened
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, sitting TEXT, gen INTEGER,
    topic TEXT,                       -- strategy id, or "bans"
    phase TEXT,                       -- open | explain | check | vote | verdict | bans | close
    speaker TEXT,                     -- a judge's name, or "council" (the chair: data checks, tallies)
    kind TEXT,                        -- asking | answer | error | system
    text TEXT, data_json TEXT
);
CREATE INDEX IF NOT EXISTS council_messages_sitting ON council_messages(sitting, id);
CREATE TABLE IF NOT EXISTS dryrun_trades (        -- the live dry run: champion trades priced from real quotes
    mint TEXT, symbol TEXT, key TEXT, entry_ts REAL, exit_ts REAL,
    size_sol REAL, proceeds_sol REAL, pnl_sol REAL, pnl_pct REAL, fees_sol REAL,
    entry_price REAL, exit_price REAL, paper_entry_price REAL, paper_exit_price REAL,
    paper_pnl_pct REAL, paper_trade_id TEXT, buy_impact_pct REAL, sell_impact_pct REAL,
    route TEXT, exit_reason TEXT, status TEXT, note TEXT,  -- status: closed | stuck
    attempt INTEGER DEFAULT 1                             -- which live attempt (each a fresh real-size wallet)
);
CREATE TABLE IF NOT EXISTS sniper_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    blob TEXT NOT NULL,
    updated_ts REAL
);
CREATE TABLE IF NOT EXISTS sniper_decisions (
    mint TEXT NOT NULL,
    ts REAL NOT NULL,
    symbol TEXT,
    action TEXT,
    score REAL,
    vetoes TEXT,
    note TEXT,
    features TEXT,
    outcome TEXT,
    max_x REAL,
    pnl_sol REAL,
    p0 REAL,
    PRIMARY KEY (mint, ts)
);
"""


def new_id(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex[:12]


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate_sniper()
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(dryrun_trades)")}
        if cols and "attempt" not in cols:               # tables from before attempts existed
            self.conn.execute("ALTER TABLE dryrun_trades ADD COLUMN attempt INTEGER DEFAULT 1")

    def close(self) -> None:
        self.conn.close()

    # ── helpers ────────────────────────────────────────────────────────────
    def _insert(self, table: str, row: dict[str, Any], replace: bool = False) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        self.conn.execute(f"{verb} INTO {table} ({cols}) VALUES ({qs})", list(row.values()))

    def query(self, sql: str, params: Iterable = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, list(params)).fetchall()]

    # ── strategy versions ──────────────────────────────────────────────────
    def upsert_version(self, strategy_id: str, version: str, spec_json: str, status: str,
                       parent_version: str | None = None, note: str = "") -> None:
        now = time.time()
        existing = self.get_version(strategy_id, version)
        if existing:
            self.conn.execute(
                "UPDATE strategy_versions SET status=?, status_ts=?, note=? WHERE strategy_id=? AND version=?",
                (status, now, note or existing["note"], strategy_id, version),
            )
            return
        self._insert("strategy_versions", dict(
            strategy_id=strategy_id, version=version, spec_json=spec_json, status=status,
            parent_version=parent_version, created_ts=now, status_ts=now, note=note,
        ))

    def get_version(self, strategy_id: str, version: str) -> dict | None:
        rows = self.query("SELECT * FROM strategy_versions WHERE strategy_id=? AND version=?",
                          (strategy_id, version))
        return rows[0] if rows else None

    def set_status(self, strategy_id: str, version: str, status: str, note: str = "") -> None:
        self.conn.execute(
            "UPDATE strategy_versions SET status=?, status_ts=?, note=CASE WHEN ?='' THEN note ELSE ? END "
            "WHERE strategy_id=? AND version=?",
            (status, time.time(), note, note, strategy_id, version),
        )

    def champion(self, strategy_id: str) -> dict | None:
        rows = self.query("SELECT * FROM strategy_versions WHERE strategy_id=? AND status='champion' "
                          "ORDER BY status_ts DESC LIMIT 1", (strategy_id,))
        return rows[0] if rows else None

    def active_versions(self) -> list[dict]:
        return self.query("SELECT * FROM strategy_versions WHERE status IN ('champion','challenger','trial','swarm') "
                          "ORDER BY strategy_id, status")

    def versions(self, strategy_id: str | None = None) -> list[dict]:
        if strategy_id:
            return self.query("SELECT * FROM strategy_versions WHERE strategy_id=? ORDER BY created_ts",
                              (strategy_id,))
        return self.query("SELECT * FROM strategy_versions ORDER BY strategy_id, created_ts")

    # ── runs / journal ─────────────────────────────────────────────────────
    def start_run(self, mode: str, data_from: float | None = None, data_to: float | None = None,
                  note: str = "") -> str:
        run_id = new_id(f"{mode}-")
        self._insert("runs", dict(run_id=run_id, mode=mode, started_ts=time.time(),
                                  data_from=data_from, data_to=data_to, note=note))
        return run_id

    def end_run(self, run_id: str) -> None:
        self.conn.execute("UPDATE runs SET ended_ts=? WHERE run_id=?", (time.time(), run_id))

    def insert_trade(self, row: dict) -> None:
        row = dict(row)
        for k in ("entry_features", "exit_features", "fills"):
            if k in row and not isinstance(row[k], str):
                row[k] = json.dumps(row[k], default=float)
        self._insert("trades", row, replace=True)

    def insert_order(self, row: dict) -> None:
        self._insert("orders", row)

    def insert_near_miss(self, row: dict) -> None:
        self._insert("near_misses", row)

    def upsert_outcome(self, row: dict) -> None:
        self._insert("token_outcomes", row, replace=True)

    def trades(self, strategy_id: str | None = None, version: str | None = None,
               run_mode: str | None = None, since: float | None = None) -> list[dict]:
        sql = "SELECT t.* FROM trades t JOIN runs r ON r.run_id=t.run_id WHERE 1=1"
        p: list = []
        if strategy_id:
            sql += " AND t.strategy_id=?"; p.append(strategy_id)
        if version:
            sql += " AND t.version=?"; p.append(version)
        if run_mode:
            sql += " AND r.mode=?"; p.append(run_mode)
        if since is not None:
            sql += " AND t.exit_ts>=?"; p.append(since)
        return self.query(sql + " ORDER BY t.exit_ts", p)

    # ── proposals / reviews / usage ────────────────────────────────────────
    def insert_proposal(self, row: dict) -> None:
        self._insert("proposals", row, replace=True)

    def update_proposal(self, proposal_id: str, **fields) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE proposals SET {sets} WHERE proposal_id=?",
                          [*fields.values(), proposal_id])

    def proposals(self, strategy_id: str | None = None, status: str | None = None,
                  limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM proposals WHERE 1=1"
        p: list = []
        if strategy_id:
            sql += " AND strategy_id=?"; p.append(strategy_id)
        if status:
            sql += " AND status=?"; p.append(status)
        return self.query(sql + " ORDER BY created_ts DESC LIMIT ?", [*p, limit])

    def insert_review(self, row: dict) -> None:
        self._insert("reviews", row, replace=True)

    def log_llm_usage(self, row: dict) -> None:
        self._insert("llm_usage", row)

    def llm_spend_since(self, ts: float) -> float:
        r = self.conn.execute("SELECT COALESCE(SUM(cost_usd),0) FROM llm_usage WHERE ts>=?", (ts,)).fetchone()
        return float(r[0])

    # ── strategy swarm ─────────────────────────────────────────────────────
    def insert_evo(self, row: dict) -> None:
        self._insert("evo_log", row)

    def evo_rows(self, version: str | None = None, event: str | None = None) -> list[dict]:
        sql, p = "SELECT * FROM evo_log WHERE 1=1", []
        if version:
            sql += " AND version=?"; p.append(version)
        if event:
            sql += " AND event=?"; p.append(event)
        return self.query(sql + " ORDER BY ts", p)

    def evo_generation(self) -> int:
        r = self.conn.execute("SELECT COALESCE(MAX(gen),0) FROM evo_log").fetchone()[0]
        s = self.conn.execute("SELECT COALESCE(MAX(gen),0) FROM evo_scores").fetchone()[0]
        return int(max(r, s))

    def insert_evo_lesson(self, row: dict) -> None:
        self._insert("evo_lessons", row)

    def insert_evo_score(self, row: dict) -> None:
        self._insert("evo_scores", row)

    def evo_scores(self, gen: int | None = None) -> list[dict]:
        gen = self.evo_generation() if gen is None else gen
        return self.query("SELECT * FROM evo_scores WHERE gen=? ORDER BY fitness DESC", [gen])

    # ── council ────────────────────────────────────────────────────────────
    def insert_council_idea(self, row: dict) -> None:
        self._insert("council_ideas", row, replace=True)

    def update_council_idea(self, idea_id: str, **fields) -> None:
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE council_ideas SET {sets} WHERE idea_id=?", [*fields.values(), idea_id])

    def council_ideas(self, strategy_id: str | None = None, approved: bool | None = None,
                      limit: int = 500) -> list[dict]:
        sql, p = "SELECT * FROM council_ideas WHERE 1=1", []
        if strategy_id:
            sql += " AND strategy_id=?"; p.append(strategy_id)
        if approved is not None:
            sql += " AND approved=?"; p.append(int(approved))
        return self.query(sql + " ORDER BY ts LIMIT ?", [*p, limit])

    def log_council_call(self, row: dict) -> None:
        self._insert("council_calls", row)

    def council_calls(self, since: float = 0.0) -> list[dict]:
        return self.query("SELECT * FROM council_calls WHERE ts>=? ORDER BY ts", [since])

    def add_council_message(self, row: dict) -> None:
        self._insert("council_messages", row)

    def council_sittings(self, limit: int = 30) -> list[dict]:
        """Newest first: each sitting's gen, first/last message time, size, and whether it was closed."""
        return self.query(
            "SELECT sitting, MIN(gen) AS gen, MIN(ts) AS started, MAX(ts) AS last_ts, COUNT(*) AS n, "
            "MAX(phase='close') AS closed FROM council_messages GROUP BY sitting ORDER BY MIN(id) DESC LIMIT ?",
            [limit])

    def add_dryrun_trade(self, row: dict) -> None:
        self._insert("dryrun_trades", row)

    def dryrun_trades(self, since: float = 0.0) -> list[dict]:
        return self.query("SELECT * FROM dryrun_trades WHERE exit_ts>=? ORDER BY exit_ts", [since])

    def council_messages(self, sitting: str, after_id: int = 0, limit: int = 1000) -> list[dict]:
        return self.query("SELECT * FROM council_messages WHERE sitting=? AND id>? ORDER BY id LIMIT ?",
                          [sitting, after_id, limit])

    # ── launch sniper ──────────────────────────────────────────────────────
    def _migrate_sniper(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(sniper_decisions)")}
        if cols and "p0" not in cols:
            self.conn.execute("ALTER TABLE sniper_decisions ADD COLUMN p0 REAL")

    def save_sniper(self, blob: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO sniper_state (id, blob, updated_ts) VALUES (1, ?, ?)",
            (json.dumps(blob), time.time()),
        )

    def load_sniper(self) -> dict | None:
        rows = self.query("SELECT blob FROM sniper_state WHERE id=1")
        if not rows:
            return None
        return json.loads(rows[0]["blob"])

    def insert_sniper_decision(self, row: dict) -> None:
        row = dict(row)
        for k in ("vetoes", "features"):
            if k in row and row[k] is not None and not isinstance(row[k], str):
                row[k] = json.dumps(row[k])
        self._insert("sniper_decisions", row, replace=True)

    def sniper_decisions(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM sniper_decisions ORDER BY ts DESC LIMIT ?", [limit])

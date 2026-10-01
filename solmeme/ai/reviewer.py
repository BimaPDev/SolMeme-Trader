"""One AI review cycle for a strategy:

  1. split recorded data into an evidence window and a newer holdout window
  2. replay the champion on the evidence window (full journal + near-misses)
  3. mine patterns + scan the whole token universe on the same window
  4. ask Claude for findings and ≤N proposals (structured output)
  5. validate each proposal and run the holdout backtest gate
Survivors become live paper challengers (forward shadow gate decides later).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from solmeme.ai import evidence
from solmeme.ai.llm import complete_json
from solmeme.ai.promoter import Promoter, champion_spec
from solmeme.ai.prompts import system_prompt
from solmeme.ai.schemas import ReviewOutput
from solmeme.ai.triage import triage
from solmeme.config import Settings
from solmeme.core.eventlog import read_events
from solmeme.core.store import Store, new_id
from solmeme.lab import scan
from solmeme.lab.metrics import engine_metrics
from solmeme.sim.replay import run_replay

log = logging.getLogger("solmeme.reviewer")


@dataclass
class Windows:
    evidence: list
    holdout: list

    @property
    def evidence_range(self) -> tuple[float, float]:
        return (self.evidence[0].ts, self.evidence[-1].ts) if self.evidence else (0.0, 0.0)


def split_windows(events: list, holdout_frac: float) -> Windows:
    if not events:
        return Windows([], [])
    t0, t1 = events[0].ts, events[-1].ts
    cut = t1 - (t1 - t0) * holdout_frac
    i = next((k for k, e in enumerate(events) if e.ts >= cut), len(events))
    return Windows(events[:i], events[i:])


def load_windows(settings: Settings, days: float | None = None) -> Windows:
    days = days or settings.promotion.review_window_days
    events = list(read_events(settings.events_dir, time.time() - days * 86400, None))
    return split_windows(events, settings.promotion.holdout_frac)


def review_strategy(settings: Settings, store: Store, strategy_id: str, windows: Windows,
                    dry_run: bool = False, universe_rows: list | None = None, force_ai: bool = False) -> dict:
    spec = champion_spec(store, strategy_id)
    if spec is None:
        raise ValueError(f"no champion for {strategy_id}")
    if not windows.evidence:
        raise ValueError("no recorded events — run `solmeme record` / `solmeme run` first")

    eng = run_replay(settings, [(spec, "champion")], windows.evidence)
    m = engine_metrics(eng)[spec.key]
    trades = [t for t in eng.journal.trades if t["strategy_id"] == spec.id]
    tri = triage(m, trades, eng.journal.near_misses, settings.ai.triage)
    if not tri.call_ai and not force_ai:
        return dict(strategy_id=strategy_id, skipped=f"no AI call: {tri.reason}", flags=tri.flags)
    last = store.query("SELECT result_json FROM reviews WHERE strategy_id=? AND version=? ORDER BY ts DESC LIMIT 1",
                       (strategy_id, spec.version))
    if last and not force_ai and json.loads(last[0]["result_json"] or "{}").get("triage_sig") == tri.signature:
        return dict(strategy_id=strategy_id, skipped="no AI call: same findings as the last review of this version",
                    flags=tri.flags)
    if universe_rows is None:
        universe_rows = scan.scan(windows.evidence, set(settings.excluded_mints))
    lift = scan.lift_table(universe_rows, top=15)
    lessons = store.proposals(strategy_id, limit=25)
    pack = evidence.build(spec, m, trades, eng.journal.near_misses, lessons, lift, windows.evidence_range)
    if tri.flags:
        pack = ("## Deterministic triage flags (start here)\n" + "\n".join(f"- {f}" for f in tri.flags)
                + "\n\n" + pack)
    if dry_run:
        return dict(strategy_id=strategy_id, dry_run=True, prompt_chars=len(system_prompt()) + len(pack),
                    evidence_pack=pack)

    res = complete_json(settings, system_prompt(), pack, ReviewOutput, purpose=f"review:{strategy_id}", store=store)
    out: ReviewOutput = res.parsed
    rid = new_id("r-")
    store.insert_review(dict(review_id=rid, ts=time.time(), strategy_id=strategy_id, version=spec.version,
                             window_from=windows.evidence_range[0], window_to=windows.evidence_range[1],
                             model=res.model, cost_usd=res.cost_usd,
                             result_json=json.dumps({**out.model_dump(), "triage_sig": tri.signature,
                                                     "triage_flags": tri.flags})))
    promoter = Promoter(settings, store)
    results = []
    for prop in out.proposals[: settings.ai.max_proposals_per_review]:
        row = promoter.submit(spec, prop.changes, hypothesis=prop.hypothesis, expected_effect=prop.expected_effect,
                              origin="ai", review_id=rid, is_retest=prop.is_retest)
        if row["status"] == "pending":
            row = promoter.backtest_gate(row, spec, windows.holdout)
        results.append(dict(title=prop.title, status=row["status"], reason=row.get("reason", ""),
                            version=row.get("challenger_version")))
    return dict(strategy_id=strategy_id, review_id=rid, model=res.model, cost_usd=round(res.cost_usd, 4),
                cache_read=res.cache_read, summary=out.summary,
                findings=[f.model_dump() for f in out.findings], proposals=results,
                feature_requests=out.feature_requests)


def due_for_review(settings: Settings, store: Store, strategy_id: str) -> bool:
    last = store.query("SELECT MAX(ts) AS ts FROM reviews WHERE strategy_id=?", (strategy_id,))[0]["ts"] or 0
    champ = store.champion(strategy_id)
    if champ is None:
        return False
    new_trades = len(store.trades(strategy_id, champ["version"], run_mode="live", since=last))
    hours = (time.time() - last) / 3600
    return new_trades >= settings.ai.review_every_trades or (hours >= settings.ai.review_every_hours and new_trades >= 10)


def ai_cycle(settings: Settings, store: Store, force: bool = False, log_fn=print) -> dict:
    """Shadow gate for running challengers, then reviews for every champion that is due."""
    promoter = Promoter(settings, store)
    decisions = promoter.shadow_gate()
    for d in decisions:
        log_fn(f"[shadow] {d['strategy_id']}: {d['verdict']} — {d['reason']}")
    due = [r["strategy_id"] for r in store.active_versions()
           if r["status"] == "champion" and (force or due_for_review(settings, store, r["strategy_id"]))]
    reviews = []
    if due:
        windows = load_windows(settings)
        rows = scan.scan(windows.evidence, set(settings.excluded_mints)) if windows.evidence else []
        for sid in due:
            try:
                r = review_strategy(settings, store, sid, windows, universe_rows=rows)
                reviews.append(r)
                log_fn(f"[review] {sid}: " + json.dumps({k: r.get(k) for k in ("skipped", "proposals", "cost_usd")},
                                                        default=str))
            except Exception as exc:  # noqa: BLE001 — one strategy failing must not stop the others
                log.exception("review %s failed", sid)
                reviews.append(dict(strategy_id=sid, error=repr(exc)))
                log_fn(f"[review] {sid}: failed — {exc!r}")
    return dict(decisions=decisions, reviews=reviews)

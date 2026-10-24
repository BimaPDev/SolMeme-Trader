"""Champion/challenger lifecycle.

  proposal ──validate──▶ invalid | duplicate
     │
     ▼ holdout backtest (data the reviewer never saw)
  backtest_failed | shadow (runs live as a paper challenger) | backtest_passed (queued: challenger cap)
     │
     ▼ forward shadow vs champion (bootstrap CI on per-trade PnL difference)
  promoted | rejected (worse / inconclusive after max_shadow_hours)

Every outcome is kept as a lesson the reviewer sees next time.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from solmeme.config import Settings
from solmeme.core.store import Store, new_id
from solmeme.lab.metrics import bootstrap_diff_ci, compute, engine_metrics
from solmeme.sim.replay import run_replay
from solmeme.strategy.diff import Change, InvalidChange, apply_changes, change_hash, change_signature
from solmeme.strategy.spec import StrategySpec, load_dir, spec_from_json

log = logging.getLogger("solmeme.promoter")
LESSON_MEMORY_S = 14 * 86400


def ensure_seeded(store: Store, settings: Settings) -> list[str]:
    """Register strategies/*.yaml as champions for ids the DB doesn't know yet, and
    strategies/trials/*.yaml as trials: paper-only on their own wallet until promoted."""
    seeded = []
    for spec in load_dir(settings.strategies_dir):
        if not store.versions(spec.id):        # a demoted (trial) or retired id is not re-seeded as champion
            store.upsert_version(spec.id, spec.version, spec.canonical_json(), "champion", note="seed")
            seeded.append(spec.key)
    trials = settings.strategies_dir / "trials"
    for spec in load_dir(trials) if trials.is_dir() else []:
        if store.champion(spec.id) is not None or store.get_version(spec.id, spec.version):
            continue            # promoted already, or this exact version was seen (maybe stopped with `kill`)
        for v in store.versions(spec.id):
            if v["status"] == "trial":   # the file was edited: the new version replaces the running one
                store.set_status(spec.id, v["version"], "retired", note=f"trial file changed to {spec.version}")
        store.upsert_version(spec.id, spec.version, spec.canonical_json(), "trial", note="seed trial")
        seeded.append(f"{spec.key} (trial)")
    return seeded


def active_specs(store: Store) -> list[tuple[StrategySpec, str]]:
    return [(spec_from_json(r["spec_json"]), r["status"]) for r in store.active_versions()]


def champion_spec(store: Store, strategy_id: str) -> StrategySpec | None:
    row = store.champion(strategy_id)
    return spec_from_json(row["spec_json"]) if row else None


class Promoter:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store
        self.cfg = settings.promotion

    # ── submission ──────────────────────────────────────────────────────────
    def submit(self, base: StrategySpec, changes: list[Change], *, hypothesis: str, expected_effect: str = "",
               origin: str = "ai", review_id: str | None = None, is_retest: bool = False,
               max_changes: int = 3) -> dict:
        pid = new_id("p-")
        row = dict(proposal_id=pid, created_ts=time.time(), strategy_id=base.id, base_version=base.version,
                   challenger_version=None, hypothesis=f"[{origin}] {hypothesis}",
                   changes_json=json.dumps([c.model_dump(mode="json", exclude_none=True) for c in changes]),
                   change_hash=change_signature(changes), expected_effect=expected_effect, status="pending",
                   review_id=review_id, reason="")
        try:
            new = apply_changes(base, changes, max_changes=max_changes)
        except InvalidChange as exc:
            row.update(status="invalid", reason=str(exc), decided_ts=time.time())
            self.store.insert_proposal(row)
            return row
        row["challenger_version"] = new.version
        if self.store.get_version(new.id, new.version):
            row.update(status="duplicate", reason="identical strategy version already exists", decided_ts=time.time())
            self.store.insert_proposal(row)
            return row
        if not is_retest:
            since = time.time() - LESSON_MEMORY_S
            for p in self.store.proposals(base.id):
                if (p["change_hash"] == row["change_hash"] and p["status"] in ("rejected", "backtest_failed")
                        and (p["decided_ts"] or 0) >= since):
                    row.update(status="duplicate", reason=f"same idea rejected recently ({p['proposal_id']})",
                               decided_ts=time.time())
                    self.store.insert_proposal(row)
                    return row
        self.store.upsert_version(new.id, new.version, new.canonical_json(), "candidate",
                                  parent_version=base.version, note=f"{origin}:{pid}")
        self.store.insert_proposal(row)
        row["_spec"] = new
        return row

    # ── gate 1: holdout backtest ───────────────────────────────────────────
    def backtest_gate(self, proposal: dict, champion: StrategySpec, holdout_events: list) -> dict:
        challenger: StrategySpec = proposal.get("_spec") or spec_from_json(
            self.store.get_version(proposal["strategy_id"], proposal["challenger_version"])["spec_json"])
        eng = run_replay(self.settings, [(champion, "champion"), (challenger, "challenger")], holdout_events,
                         snapshot_features=False, record_near_misses=False)
        ms = engine_metrics(eng)
        mc, mx = ms[champion.key], ms[challenger.key]
        c = self.cfg
        reasons = []
        if mx.n_trades < c.min_backtest_trades:
            reasons.append(f"only {mx.n_trades} holdout trades (< {c.min_backtest_trades})")
        if not mx.expectancy_sol > mc.expectancy_sol + c.min_expectancy_delta_sol:
            reasons.append(f"expectancy {mx.expectancy_sol:+.4f} ≤ champion {mc.expectancy_sol:+.4f}")
        if mx.total_pnl_sol < mc.total_pnl_sol - abs(mc.total_pnl_sol) * 0.1:
            reasons.append(f"total PnL {mx.total_pnl_sol:+.3f} < champion {mc.total_pnl_sol:+.3f}")
        dd_cap = max(mc.max_dd_sol * c.max_dd_tolerance, mc.max_dd_sol + 0.05)
        if mx.max_dd_sol > dd_cap:
            reasons.append(f"max DD {mx.max_dd_sol:.3f} > {dd_cap:.3f}")
        if mx.pnl_ex_top3_sol < mc.pnl_ex_top3_sol and mx.pnl_ex_top3_sol < 0:
            reasons.append("gains depend on ≤3 outlier trades")
        bt = dict(champion=mc.as_dict(), challenger=mx.as_dict(),
                  window=[holdout_events[0].ts, holdout_events[-1].ts] if holdout_events else None)
        pid = proposal["proposal_id"]
        if reasons:
            self.store.update_proposal(pid, status="backtest_failed", backtest_json=json.dumps(bt),
                                       reason="; ".join(reasons), decided_ts=time.time())
            self.store.set_status(proposal["strategy_id"], proposal["challenger_version"], "rejected",
                                  note="holdout backtest failed")
            return {**proposal, "status": "backtest_failed", "reason": "; ".join(reasons), "backtest": bt}
        n_ch = sum(1 for v in self.store.versions(proposal["strategy_id"]) if v["status"] == "challenger")
        status = "shadow" if n_ch < self.settings.ai.max_challengers_per_strategy else "backtest_passed"
        self.store.update_proposal(pid, status=status, backtest_json=json.dumps(bt),
                                   shadow_started_ts=time.time() if status == "shadow" else None,
                                   reason="passed holdout backtest" + ("" if status == "shadow" else "; queued (challenger cap)"))
        if status == "shadow":
            self.store.set_status(proposal["strategy_id"], proposal["challenger_version"], "challenger",
                                  note=f"shadow since {time.strftime('%Y-%m-%d %H:%M')}")
        return {**proposal, "status": status, "backtest": bt}

    # ── gate 2: forward shadow ─────────────────────────────────────────────
    def shadow_gate(self) -> list[dict]:
        c = self.cfg
        decisions = []
        for p in self.store.proposals(status="shadow"):
            sid = p["strategy_id"]
            champ = self.store.champion(sid)
            if champ is None:
                continue
            from solmeme.sim.wallet import trusted_since
            since = max(p["shadow_started_ts"] or p["created_ts"], trusted_since(self.settings))
            ch_tr = [t for t in self.store.trades(sid, p["challenger_version"], run_mode="live")
                     if (t["entry_ts"] or 0) >= since]
            cp_tr = [t for t in self.store.trades(sid, champ["version"], run_mode="live")
                     if (t["entry_ts"] or 0) >= since]
            mx, mc = compute(ch_tr), compute(cp_tr)
            info = dict(challenger=mx.as_dict(), champion=mc.as_dict(), since=since)
            age_h = (time.time() - since) / 3600
            verdict, reason = None, ""
            if mx.n_trades >= c.min_shadow_trades:
                a = [t["pnl_sol"] for t in ch_tr]
                b = [t["pnl_sol"] for t in cp_tr] if len(cp_tr) >= 5 else [mc.expectancy_sol or 0.0] * 5
                lo, point, hi = bootstrap_diff_ci(a, b, c.bootstrap_iters, c.bootstrap_conf)
                info["ci"] = [lo, point, hi]
                dd_ok = mx.max_dd_sol <= max(mc.max_dd_sol * c.max_dd_tolerance, mc.max_dd_sol + 0.05)
                if lo > 0 and dd_ok:
                    verdict, reason = "promote", f"shadow edge {point:+.4f} SOL/trade (CI {lo:+.4f}..{hi:+.4f})"
                elif hi < 0:
                    verdict, reason = "reject", f"worse than champion (CI {lo:+.4f}..{hi:+.4f})"
            if verdict is None and age_h > c.max_shadow_hours:
                verdict, reason = "reject", f"inconclusive after {age_h:.0f}h ({mx.n_trades} trades)"
            self.store.update_proposal(p["proposal_id"], shadow_json=json.dumps(info))
            if verdict == "promote":
                self.promote(sid, p["challenger_version"], reason)
                self.store.update_proposal(p["proposal_id"], status="promoted", reason=reason, decided_ts=time.time())
            elif verdict == "reject":
                self.store.set_status(sid, p["challenger_version"], "rejected", note=reason)
                self.store.update_proposal(p["proposal_id"], status="rejected", reason=reason, decided_ts=time.time())
            if verdict:
                decisions.append(dict(proposal_id=p["proposal_id"], strategy_id=sid, verdict=verdict, reason=reason))
        self._activate_queued()
        return decisions

    def _activate_queued(self) -> None:
        for p in reversed(self.store.proposals(status="backtest_passed")):
            sid = p["strategy_id"]
            n_ch = sum(1 for v in self.store.versions(sid) if v["status"] == "challenger")
            if n_ch < self.settings.ai.max_challengers_per_strategy:
                self.store.set_status(sid, p["challenger_version"], "challenger", note="activated from queue")
                self.store.update_proposal(p["proposal_id"], status="shadow", shadow_started_ts=time.time())

    # ── manual controls ────────────────────────────────────────────────────
    def promote(self, strategy_id: str, version: str, reason: str = "manual") -> None:
        row = self.store.get_version(strategy_id, version)
        if row is None:
            raise ValueError(f"unknown version {strategy_id}@{version}")
        cur = self.store.champion(strategy_id)
        if cur and cur["version"] != version:
            self.store.set_status(strategy_id, cur["version"], "retired", note=f"replaced by {version}")
        self.store.set_status(strategy_id, version, "champion", note=reason)
        hist = self.settings.strategies_dir / "history"
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"{strategy_id}@{version}.yaml").write_text(
            f"# promoted {time.strftime('%Y-%m-%d %H:%M:%S')} — {reason}\n" + spec_from_json(row["spec_json"]).to_yaml())
        log.info("promoted %s@%s: %s", strategy_id, version, reason)

    def rollback(self, strategy_id: str, to_version: str | None = None) -> str:
        cur = self.store.champion(strategy_id)
        if to_version is None:
            retired = [v for v in self.store.versions(strategy_id) if v["status"] == "retired"]
            if not retired:
                raise ValueError("no earlier champion to roll back to")
            to_version = max(retired, key=lambda v: v["status_ts"])["version"]
        if cur:
            self.store.set_status(strategy_id, cur["version"], "retired", note=f"rolled back to {to_version}")
        self.store.set_status(strategy_id, to_version, "champion", note="rollback")
        return to_version

    def demote(self, strategy_id: str, reason: str = "manual") -> str:
        """Champion → trial: it keeps trading and seeding the swarm on its own paper wallet, but no
        longer spends the shared wallet. Undo with `promote`."""
        cur = self.store.champion(strategy_id)
        if cur is None:
            raise ValueError(f"{strategy_id} has no champion")
        self.store.set_status(strategy_id, cur["version"], "trial", note=f"demoted: {reason}")
        return cur["version"]

    def kill_challenger(self, strategy_id: str, version: str) -> None:
        self.store.set_status(strategy_id, version, "rejected", note="killed manually")
        for p in self.store.proposals(strategy_id, status="shadow"):
            if p["challenger_version"] == version:
                self.store.update_proposal(p["proposal_id"], status="rejected", reason="killed manually",
                                           decided_ts=time.time())

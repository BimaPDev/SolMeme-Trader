"""Council: several LLM judges (free router models, local Ollama) study why coins made money and
turn that into entry rules the swarm then tests live. Never in the buy/sell path.

Each generation, for the busiest strategies:
  1. explain  every judge sees winners vs losers (entry features, best coins, past ideas and how
              they did) and proposes up to N rules: "require f >= v" or "avoid f > v", with a reason
  2. check    each rule is replayed on the same trades: rules that would not have helped are dropped
              before any vote (the data decides, not the model's confidence)
  3. vote     judges vote on the survivors; votes are weighted by each judge's track record
  4. test     approved ideas become a mutation kind: the swarm breeds children carrying them, which
              trade live on their own wallets
  5. judge    every generation, each idea is scored from its carriers' live trades and deaths:
              testing → working | failed. Failed ideas stop being bred, and judges whose ideas fail
              lose voting weight.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
import numpy as np

from solmeme.config import CouncilMember, Settings, secret
from solmeme.core.store import Store, new_id
from solmeme.features.registry import FEATURES
from solmeme.lab.mining import _feat_matrix, feature_lift
from solmeme.strategy.spec import StrategySpec

SYSTEM = (
    "You are one judge on a council that studies Solana meme-coin paper trades. You explain why the "
    "winning coins made money and turn that into simple entry rules over the listed features. Be "
    "concrete and skeptical: small samples lie, and a rule must keep enough trades to matter. "
    "Reply with JSON only. Be terse, like notes: no preamble, no restating the evidence, no hedging; "
    "every reason field at most 15 words. Numbers, feature names and verdicts stay exact."
)


@dataclass
class Rule:
    feature: str
    op: str            # ">=" or "<="
    value: float
    kind: str          # "require" (entry.all) or "avoid" (entry.none)
    reason: str = ""

    def text(self) -> str:
        verb = "buy only when" if self.kind == "require" else "don't buy when"
        return f"{verb} {self.feature} {self.op} {self.value:g}"

    def blocks(self, x: float) -> bool:
        hit = x >= self.value if self.op == ">=" else x <= self.value
        return not hit if self.kind == "require" else hit


# ── talking to judges ──────────────────────────────────────────────────────
def ask(member: CouncilMember, system: str, user: str, retries: int = 2) -> dict:
    """One JSON answer from one judge. A busy or reloading GPU makes Ollama answer 4xx/5xx or time out
    for a while, so failed calls wait and retry before giving up; the error keeps the server's message."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return _ask_once(member, system, user)
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:200].replace("\n", " ")
            last = RuntimeError(f"{exc.response.status_code} from {member.name}: {body}")
            if exc.response.status_code in (401, 403, 404, 410):     # retrying cannot fix these
                break
        except (httpx.TimeoutException, httpx.TransportError, ValueError) as exc:
            last = exc
        if attempt < retries:
            time.sleep(20 * (attempt + 1))
    raise last  # type: ignore[misc]


MAX_LOCAL_PROMPT_CHARS = 9000      # ~2,600 tokens of prompt, leaving room for the answer in a 4,096-token window
BAN_BATCH = 6                      # ban suspects per question: long lists overflow local models and break their JSON


def fit_prompt(member: CouncilMember, system: str, user: str) -> str:
    """Trim the middle of an over-long prompt (evidence), keeping its start and the instructions at the end."""
    limit = member.max_prompt_chars or (MAX_LOCAL_PROMPT_CHARS if member.kind == "ollama" else 0)
    room = limit - len(system)
    if not limit or len(user) <= room:
        return user
    tail = min(2500, room // 2)
    return user[: room - tail - 40] + "\n…(evidence trimmed to fit)…\n" + user[-tail:]


def _ask_once(member: CouncilMember, system: str, user: str) -> dict:
    user = fit_prompt(member, system, user)
    if member.kind == "ollama":
        r = httpx.post(member.base_url.rstrip("/") + "/api/chat", timeout=member.timeout_s, json={
            "model": member.model, "stream": False, "think": False, "format": "json",
            "keep_alive": member.keep_alive, "options": {"temperature": member.temperature},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        text = r.json()["message"]["content"]
    else:
        headers = {}
        key = secret(member.key_env) if member.key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        r = httpx.post(member.base_url.rstrip("/") + "/chat/completions", timeout=member.timeout_s, headers=headers,
                       json={"model": member.model, "temperature": member.temperature, "stream": False,
                             "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
    return _json(text)


def _json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise ValueError(f"no JSON in answer: {text[:120]!r}")
        out = json.loads(m.group(0))
    if not isinstance(out, dict):
        raise ValueError("answer is not a JSON object")
    return out


def available(member: CouncilMember) -> bool:
    if not member.enabled or not member.model:
        return False
    return not member.key_env or bool(secret(member.key_env))


# ── evidence ───────────────────────────────────────────────────────────────
def evidence(spec: StrategySpec, trades: list[dict], past: list[dict], top: int = 14) -> tuple[str, dict]:
    """The prompt body for one strategy, and observed value ranges for validating answers."""
    pnl = [float(t["pnl_sol"]) for t in trades]
    wins = [t for t in trades if float(t["pnl_sol"]) > 0]
    lift = feature_lift(trades, top=top)
    names, X = _feat_matrix(trades)
    ranges = {}
    for j, n in enumerate(names):
        col = X[:, j][~np.isnan(X[:, j])]
        if len(col) >= max(5, len(trades) // 2):
            ranges[n] = (float(np.min(col)), float(np.max(col)))
    rules = "\n".join(f"  {sec}: {c.f} {c.op} {spec.p(c.v)}" for sec, c in spec.entry.rules()) or "  (none)"
    if spec.entry.score:
        rules += f"\n  score (buy at {spec.p(spec.entry.min_score)} points): " + ", ".join(
            f"{t.f} {t.op} {spec.p(t.v)} = {spec.p(t.w)}" for t in spec.entry.score)
    feats = "\n".join(
        f"  {d['feature']} ({FEATURES[d['feature']].unit}; {FEATURES[d['feature']].description[:70]}): "
        f"winners {d['median_winners']} vs losers {d['median_losers']}, AUC {d['auc']}"
        for d in lift if d["feature"] in FEATURES) or "  (not enough trades for feature stats)"
    best = sorted(wins, key=lambda t: -float(t.get("pnl_pct") or 0))[:5]
    cards = []
    for t in best:
        f = t.get("entry_features") or {}
        if isinstance(f, str):
            f = json.loads(f)
        keyv = ", ".join(f"{d['feature']}={_fmt(f.get(d['feature']))}" for d in lift[:6] if d["feature"] in f)
        cards.append(f"  {t.get('symbol') or t['mint'][:6]}: {float(t.get('pnl_pct') or 0):+.0f}%, "
                     f"peak {float(t.get('mfe_x') or 1):.2f}x, exit {t.get('exit_reason')}; {keyv}")
    hist = "\n".join(f"  {p['text']} → {p['status']} ({p['status_reason']})" for p in past[-8:]) or "  (none yet)"
    body = (
        f"Strategy {spec.id}: {spec.description.strip()[:300]}\n"
        f"Current entry rules:\n{rules}\n\n"
        f"Recent trades: {len(trades)} ({len(wins)} winners), total {sum(pnl):+.3f} SOL, "
        f"average {np.mean([float(t['pnl_pct']) for t in trades]):+.1f}% per trade.\n\n"
        f"Features that separate winners from losers (median values; AUC 0.5 = no signal):\n{feats}\n\n"
        f"Best winning coins:\n" + ("\n".join(cards) or "  (none)") + "\n\n"
        f"Earlier council ideas for this strategy and how they did live:\n{hist}\n"
    )
    return body, ranges


def live_evidence(settings: Settings, store: Store, sid: str) -> tuple[str, list[dict]]:
    """What the live dry run saw on this strategy's champion entries at real prices: text for the judges,
    and the entries labelled good (+1: bought and won) or bad (-1: could not be bought, lost or stuck),
    with the paper entry features, so rules can be checked against live outcomes too."""
    try:
        tape = json.loads((settings.data_dir / "session" / "dryrun_wallet.json").read_text(encoding="utf-8"))["tape"]
    except (OSError, ValueError, KeyError):
        return "", []
    mine = [t for t in tape if str(t.get("key", "")).startswith(sid + "@")]
    attempts = [t for t in mine if t.get("side") in ("buy", "missed")]
    if len(attempts) < 5:
        return "", []
    closes = {t["mint"]: t for t in mine if t.get("side") == "sell" and not t.get("partial")}
    stuck = {t["mint"] for t in mine if t.get("side") == "stuck"}
    rows, missed, gaps = [], 0, []
    for t in attempts:
        if t.get("price") and t.get("paper_price"):
            gaps.append(t["price"] / t["paper_price"] - 1)
        if t["side"] == "missed":
            missed += 1
            good = False
        elif t["mint"] in stuck:
            good = False
        elif t["mint"] in closes and closes[t["mint"]].get("pnl_sol") is not None:
            good = float(closes[t["mint"]]["pnl_sol"]) > 0
        else:
            continue                                      # still open: no verdict yet
        ver = str(t["key"]).split("@", 1)[1]
        r = store.query("SELECT entry_features FROM trades WHERE strategy_id=? AND version=? AND mint=? "
                        "ORDER BY ABS(entry_ts - ?) LIMIT 1", [sid, ver, t["mint"], float(t.get("ts") or 0)])
        f = json.loads(r[0]["entry_features"] or "{}") if r and r[0]["entry_features"] else {}
        if f:
            rows.append(dict(pnl_sol=1.0 if good else -1.0, pnl_pct=1.0 if good else -1.0, entry_features=f))
    n_good = sum(r["pnl_sol"] > 0 for r in rows)
    gap = sorted(gaps)[len(gaps) // 2] if gaps else None
    text = (f"\nLive check (the live dry run copied the last {len(attempts)} {sid} champion entries at real prices): "
            f"{missed} could not be bought because the real price had already run past the slippage limit"
            + (f" (median real entry {gap * 100:+.0f}% vs paper)" if gap is not None else "")
            + f"; of the rest that closed, {n_good} won and {len(rows) - n_good - missed} lost or could not be sold.")
    lift = [d for d in feature_lift(rows, top=6) if d["feature"] in FEATURES] if rows else []
    if lift:
        text += " Entries that worked live vs ones that failed live differed most on:\n" + "\n".join(
            f"  {d['feature']}: worked {d['median_winners']} vs failed {d['median_losers']}, AUC {d['auc']}" for d in lift)
    text += ("\nA rule that skips entries live cannot fill, or that lose at real prices, is worth proposing; "
             "it must still pass the paper check.\n")
    return text, rows


def live_check(rule: "Rule", rows: list[dict]) -> str:
    """How a rule would have done on the live dry run's labelled entries."""
    if not rows:
        return ""
    bad = [r for r in rows if r["pnl_sol"] < 0]
    good = [r for r in rows if r["pnl_sol"] > 0]

    def blocked(rs):
        out = 0
        for r in rs:
            x = (r["entry_features"] or {}).get(rule.feature)
            if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x)) and rule.blocks(float(x)):
                out += 1
        return out
    return f"live: it would have skipped {blocked(bad)} of {len(bad)} entries that failed at real prices and {blocked(good)} of {len(good)} that worked"


def _fmt(x) -> str:
    return "?" if x is None else (f"{x:.4g}" if isinstance(x, float) else str(x))


def in_sample(rule: Rule, trades: list[dict]) -> dict:
    """What the rule would have done to these trades: blocked count and SOL saved (positive = better)."""
    blocked, saved, wins_lost, known = 0, 0.0, 0, 0
    for t in trades:
        f = t.get("entry_features") or {}
        if isinstance(f, str):
            f = json.loads(f)
        x = f.get(rule.feature)
        if not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(float(x)):
            continue
        known += 1
        if rule.blocks(float(x)):
            blocked += 1
            p = float(t["pnl_sol"])
            saved -= p
            wins_lost += 1 if p > 0 else 0
    return dict(n=len(trades), known=known, blocked=blocked, kept=len(trades) - blocked,
                saved_sol=round(saved, 4), winners_lost=wins_lost)


def helpful(stats: dict, min_blocked: int = 3, min_keep_frac: float = 0.3) -> tuple[bool, str]:
    if stats["known"] < max(8, stats["n"] // 2):
        return False, f"feature known on only {stats['known']}/{stats['n']} trades"
    if stats["blocked"] < min_blocked:
        return False, f"blocks only {stats['blocked']} trades"
    if stats["kept"] < min_keep_frac * stats["n"]:
        return False, f"keeps only {stats['kept']}/{stats['n']} trades"
    if stats["saved_sol"] <= 0:
        return False, f"would have cost {-stats['saved_sol']:.3f} SOL"
    return True, f"would have saved {stats['saved_sol']:.3f} SOL, blocking {stats['blocked']}/{stats['n']}"


def parse_rules(answer: dict, ranges: dict, limit: int) -> list[Rule]:
    out = []
    for r in (answer.get("rules") or [])[:limit]:
        try:
            f, op, v = str(r["feature"]), str(r["op"]).strip(), float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
        kind = str(r.get("kind", "require")).lower().strip()
        if kind not in ("require", "avoid"):
            continue                       # "boost" and other invented kinds are not rules we can apply
        if f not in FEATURES or f not in ranges or op not in (">=", "<=", ">", "<") or not math.isfinite(v):
            continue
        out.append(Rule(f, ">=" if op in (">=", ">") else "<=", round(v, 6), kind, str(r.get("reason", ""))[:240]))
    return out


# ── track record ───────────────────────────────────────────────────────────
def idea_sig(idea_id: str) -> str:
    return f"idea:{idea_id}"


def judge_ideas(store: Store, settings: Settings, now: float) -> list[dict]:
    """Score every approved idea from its carriers: live trades since birth and deaths."""
    from solmeme.sim.wallet import trusted_since

    cfg = settings.council
    trust_ts = trusted_since(settings)            # trades filled by the broken model don't judge an idea
    born = store.evo_rows(event="born")
    killed = {r["version"] for r in store.evo_rows(event="killed")}
    promoted = {r["version"] for r in store.evo_rows(event="promoted")}
    out = []
    for idea in store.council_ideas(approved=True):
        sig = idea_sig(idea["idea_id"])
        carriers = [r for r in born if sig in json.loads(r["ops"] or "[]")]
        n_tr, pnl, deaths, deltas = 0, 0.0, 0, []
        for c in carriers:
            trades = [t for t in store.trades(c["strategy_id"], c["version"], run_mode="live")
                      if (t["entry_ts"] or 0) >= max(c["ts"], trust_ts)]
            n_tr += len(trades)
            pnl += sum(float(t["pnl_sol"] or 0) for t in trades)
            deaths += c["version"] in killed
            if c["fitness"] is not None and c["parent_fitness"] is not None:
                deltas.append(c["fitness"] - c["parent_fitness"])
        if any(c["version"] in promoted for c in carriers):
            status, why = "working", "a carrier was promoted to champion"
        elif n_tr >= cfg.judge_min_trades and pnl > 0:
            status, why = "working", f"carriers {pnl:+.3f} SOL over {n_tr} live trades"
        elif n_tr >= cfg.judge_min_trades or deaths >= 2:
            status, why = "failed", f"carriers {pnl:+.3f} SOL over {n_tr} live trades, {deaths} died"
        elif not carriers and now - idea["ts"] > 6 * 3600:
            status, why = "failed", "no child carrying it passed replay screening in 6h"
        else:
            status, why = "testing", f"{len(carriers)} carriers, {n_tr} live trades so far"
        store.update_council_idea(idea["idea_id"], status=status, status_reason=why, carriers=len(carriers),
                                  live_trades=n_tr, live_pnl_sol=pnl, deaths=deaths,
                                  replay_delta=float(np.mean(deltas)) if deltas else None)
        out.append({**idea, "status": status, "status_reason": why})
    return out


def reputations(store: Store) -> dict[str, float]:
    """Per judge: +1 for each working idea it proposed or approved, -1 for each failed one; mean in [-1, 1]."""
    scores: dict[str, list[float]] = {}
    for idea in store.council_ideas():
        if idea["status"] not in ("working", "failed"):
            continue
        s = 1.0 if idea["status"] == "working" else -1.0
        votes = json.loads(idea["votes_json"] or "{}")
        backers = {idea["proposed_by"]} | {m for m, v in votes.items() if v.get("approve")}
        for m in backers:
            scores.setdefault(m, []).append(s)
    return {m: float(np.mean(v)) for m, v in scores.items()}


def weight(rep: float | None) -> float:
    return 1.0 if rep is None else max(0.3, min(2.0, 1.0 + rep))


# ── the transcript ─────────────────────────────────────────────────────────
class Chat:
    """A sitting as a chat (council_messages), written the moment each step happens, for the dashboard.
    Only called on the thread that owns the store. A transcript failure never stops a sitting."""

    def __init__(self, store: Store, gen: int | None):
        self.store, self.gen = store, gen
        self.sitting = f"gen{gen}-{os.getpid()}"      # one per generation run: its ban review and its sitting

    def say(self, topic: str, phase: str, speaker: str, kind: str, text: str, data: dict | None = None) -> None:
        try:
            self.store.add_council_message(dict(
                ts=time.time(), sitting=self.sitting, gen=self.gen, topic=topic, phase=phase, speaker=speaker,
                kind=kind, text=text, data_json=json.dumps(data, default=str) if data else None))
        except Exception:  # noqa: BLE001
            pass

    def events(self, phase: str, describe):
        """An on_event callback for _ask_all: who is thinking, what each judge said, who failed or ran late.
        describe(member, key, answer) turns one answer into chat text."""
        def on_event(kind: str, m: CouncilMember | None, key, ans, row) -> None:
            topic = key if isinstance(key, str) else ("bans" if phase == "bans" else "")
            if kind == "ask":
                self.say(topic, phase, m.name, "asking", "is thinking…", {"model": m.model})
            elif kind == "done" and ans is not None:
                self.say(topic, phase, m.name, "answer", describe(m, key, ans),
                         {"model": m.model, "seconds": round(row["seconds"], 1), "raw": ans})
            elif kind == "done":
                self.say(topic, phase, m.name, "error", f"couldn't answer: {row['error'] or 'no answer'}",
                         {"model": m.model, "seconds": round(row["seconds"], 1)})
            elif kind == "late":
                self.say(topic, phase, m.name, "error", "ran out of time this sitting", {"model": m.model})
            elif kind == "backup":
                self.say("", phase, "council", "system",
                         f"{len(ans)} question(s) got no answer, so the backup judges step in: {', '.join(row)}.")
        return on_event


def cursor_out(settings: Settings) -> str:
    """Why no Cursor judge can answer at all ("" when Cursor can). Local GPU judges only sit when this is
    non-empty (your rule, Oct 10): every Cursor judge off or without a key, the shim down, or the plan used up.
    Today's pacing share being spent is not "out": then 9router alone backs up."""
    cursor = [m for m in settings.council.members if m.name.startswith("cursor") and available(m)]
    if not cursor:
        return "no Cursor judge is on"
    base = cursor[0].base_url.rstrip("/").removesuffix("/v1")
    try:
        h = httpx.get(base + "/health", timeout=8.0).json()
        if not h.get("ok") or h.get("account") is False:
            return f"the Cursor shim reports {str(h)[:80]}"
    except Exception as e:  # noqa: BLE001
        return f"the Cursor shim is unreachable ({str(e)[:60]})"
    from solmeme.ui.cursor_usage import cursor_usage

    plan = ((((cursor_usage(settings).get("account") or {}).get("summary")) or {}).get("individualUsage") or {}).get("plan") or {}
    pools = {_cursor_pool(m) for m in cursor}
    if pools and all(float(plan.get(f"{p}PercentUsed") or 0) >= 100 for p in pools):
        return "the Cursor plan is used up"
    return ""


def _local_ok(settings: Settings | None, primary: list[CouncilMember], answers: dict) -> bool:
    """May a local judge answer what is still missing? Only when Cursor is out (see cursor_out), or when
    Cursor judges sat in this round and every one of them failed (offline in practice)."""
    sat = [m for m in primary if m.name.startswith("cursor")]
    if sat:                                          # Cursor sat: local only if every Cursor judge failed everything
        return all(all(v is None for v in answers.get(m.name, {}).values()) for m in sat)
    return settings is None or bool(cursor_out(settings))   # benched by pacing is not out; no settings = no Cursor here


def _cursor_pool(m: CouncilMember) -> str:
    """Cursor Pro meters two pools: Auto + Composer, and named API models (Sonnet, GPT, Gemini...)."""
    return "auto" if m.model == "auto" or m.model.startswith("composer") else "api"


CYCLE_SLACK_PCT = 2.0


def pace_cursor(settings: Settings, members: list[CouncilMember], now: float | None = None) -> tuple[list[CouncilMember], str]:
    """Keep each Cursor pool on an even pace for its billing cycle (with the cycle start known), else within an
    even daily share of what is left of it.
    Uses the percentages Cursor reports per pool (the dollar figure only covers the API pool, so pacing on it
    benched every judge while 90% of the plan was unused); falls back to the dollar balance without them.
    Returns the judges who sit, and a note when some Cursor ones sit out (empty otherwise)."""
    cursor = [m for m in members if m.name.startswith("cursor")]
    if not getattr(settings.council, "cursor_pacing", True) or not cursor:   # tolerate a config from before pacing
        return members, ""
    from datetime import datetime

    from solmeme.ui.cursor_usage import cursor_usage

    now = now or time.time()
    summary = ((cursor_usage(settings, fresh=True).get("account") or {}).get("summary")) or {}
    plan = (summary.get("individualUsage") or {}).get("plan") or {}
    used, remaining = plan.get("used"), plan.get("remaining")
    pct = {k: plan.get(f"{k}PercentUsed") for k in ("auto", "api")}
    try:
        end = datetime.fromisoformat(str(summary["billingCycleEnd"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        end = None
    has_pct = all(v is not None for v in pct.values())
    if end is None or (not has_pct and (used is None or remaining is None)):
        return members, ""                                 # balance unknown: no pacing
    try:
        start = datetime.fromisoformat(str(summary["billingCycleStart"]).replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        start = None
    if has_pct and start and end > start:
        # use the whole plan (your call, Oct 10): a pool sits only while it is ahead of an even pace for the
        # billing cycle, so quiet days are made up later instead of a daily share benching Cursor while most of
        # the plan is unused. CYCLE_SLACK_PCT lets a fresh cycle start at once.
        line = min(100.0, 100.0 * max(0.0, (now - start) / (end - start)) + CYCLE_SLACK_PCT)
        benched = {p for p in ("auto", "api") if float(pct[p]) >= line}
        if not benched:
            return members, ""
        out = [m for m in members if m not in cursor or _cursor_pool(m) not in benched]
        return out, (f"Cursor {' and '.join(sorted(benched))} judges sit out a little: "
                     + "; ".join(f"{p} pool {float(pct[p]):.0f}% used" for p in sorted(benched))
                     + f", ahead of an even pace ({line:.0f}% by now); they are back as the cycle catches up")
    path = settings.data_dir / "session" / "cursor_pacing.json"
    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("day") != day or (has_pct and "pct_start" not in state):
        state = {"day": day, "used_start": used, "pct_start": pct if has_pct else None}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    days_left = max(1.0, (end - now) / 86400)
    benched, notes = set(), []
    if has_pct and state.get("pct_start"):
        for pool in ("auto", "api"):
            spent = max(0.0, float(pct[pool]) - float(state["pct_start"][pool]))
            share = (100.0 - float(pct[pool]) + spent) / days_left
            if spent >= share:
                benched.add(pool)
                notes.append(f"{pool} pool {spent:.1f}% of {share:.1f}% ({100 - float(pct[pool]):.0f}% left)")
    else:
        spent = max(0.0, float(used) - float(state.get("used_start") or used))
        share = (float(remaining) + spent) / days_left
        if spent >= share:
            benched = {"auto", "api"}
            notes.append(f"${spent / 100:.2f} of ${share / 100:.2f}; ${float(remaining) / 100:.2f} left")
    if not benched:
        return members, ""
    out = [m for m in members if m not in cursor or _cursor_pool(m) not in benched]
    return out, (f"Cursor {' and '.join(sorted(benched))} judges sit out until tomorrow: today's share is spent "
                 f"({'; '.join(notes)}; {days_left:.0f} days to the reset)")


def _judges_line(members: list[CouncilMember], reps: dict[str, float]) -> str:
    main = [m for m in members if not m.fallback] or members
    backup = [m for m in members if m.fallback and m not in main]
    line = ", ".join(f"{m.name} (vote ×{weight(reps.get(m.name)):.2f})" for m in main)
    return line + (f"; backup: {', '.join(m.name for m in backup)}" if backup else "")


# ── one sitting ────────────────────────────────────────────────────────────
def sit(settings: Settings, store: Store, gen: int, species: dict[str, StrategySpec],
        trades_by_species: dict[str, list[dict]], log_fn=lambda _: None, now: float | None = None) -> list[dict]:
    """Propose → check → vote for the busiest strategies. Returns the new idea rows."""
    cfg = settings.council
    now = now or time.time()
    members = [m for m in cfg.members if available(m)]
    chat = Chat(store, gen)
    members, paced = pace_cursor(settings, members)
    if paced:
        log_fn(f"council: {paced}")
        chat.say("", "open", "council", "system", paced)
    if not members:
        log_fn("council: no judge available")
        chat.say("", "open", "council", "system", f"Generation {gen}: no judge is available, so the council does not sit.")
        return []
    reps = reputations(store)
    started = time.time()
    busy = sorted(((sid, ts) for sid, ts in trades_by_species.items()
                   if sid in species and len(ts) >= cfg.min_trades), key=lambda x: -len(x[1]))[:cfg.max_species]
    created: list[dict] = []
    # Each judge answers everything in one go (all proposals, then all votes): local models share
    # one GPU, and swapping them per question costs minutes of loading.
    ctx = {}
    live_rows: dict[str, list[dict]] = {}
    for sid, trades in busy:
        spec = species[sid]
        past = [dict(p, text=Rule(p["feature"], p["op"], p["v"], p["kind"]).text())
                for p in store.council_ideas(strategy_id=sid, approved=True)]
        body, ranges = evidence(spec, trades, past)
        live_text, live_rows[sid] = live_evidence(settings, store, sid)
        ctx[sid] = (spec, trades, body + live_text, ranges)
    if not ctx:
        chat.say("", "open", "council", "system",
                 f"Generation {gen}: no strategy has {cfg.min_trades}+ recent trades to study, so the council does not sit.")
        return []
    table = "; ".join(f"{sid} ({len(t)} trades, {sum(float(x['pnl_sol']) > 0 for x in t)} won, "
                      f"{sum(float(x['pnl_sol']) for x in t):+.2f} SOL)" for sid, (_, t, _, _) in ctx.items())
    chat.say("", "open", "council", "system",
             f"Generation {gen}: the council sits. Judges: {_judges_line(members, reps)}. On the table: {table}.")

    # 1. explain → proposals
    proposals: dict[str, list[tuple[str, Rule]]] = {sid: [] for sid in ctx}
    questions = []
    for sid, (spec, trades, body, ranges) in ctx.items():
        ask_text = (body + f"\nWhy did the winners make money? Propose up to {cfg.max_ideas_per_member} entry "
                    "rules that keep winners like these and drop losers. Use only features listed above, with "
                    'thresholds inside the observed range. Answer {"why": "...", "rules": [{"feature": "...", '
                    '"op": ">=" or "<=", "value": number, "kind": "require" or "avoid", "reason": "..."}]}. '
                    '"require" means buy only when the condition holds; "avoid" means never buy when it holds.')
        questions.append((sid, f"explain {sid}", ask_text))
        chat.say(sid, "explain", "council", "system",
                 f"{sid}: why did the winners make money? Each judge proposes up to {cfg.max_ideas_per_member} entry rules.",
                 {"evidence": body})

    def explained(m, sid, ans):
        rules = parse_rules(ans, ctx[sid][3], cfg.max_ideas_per_member)
        lines = [str(ans.get("why") or "").strip()]
        lines += [f"• {r.text()}: {r.reason}" if r.reason else f"• {r.text()}" for r in rules]
        dropped = len((ans.get("rules") or [])[:cfg.max_ideas_per_member]) - len(rules)
        if dropped > 0:
            lines.append(f"({dropped} more rule(s) ignored: unknown feature, bad operator or value outside the data)")
        if not rules:
            lines.append("(no usable rule)")
        return "\n".join(x for x in lines if x)

    answers = _ask_all(store, members, questions, log_fn, started, cfg.max_seconds, chat.events("explain", explained),
                       settings=settings)
    for m in members:
        for sid, (spec, trades, body, ranges) in ctx.items():
            ans = answers[m.name].get(sid)
            if ans is not None:
                proposals[sid] += [(m.name, r) for r in parse_rules(ans, ranges, cfg.max_ideas_per_member)]

    # 2. check against the data, dedupe by (feature, op, kind)
    survivors: dict[str, list] = {}
    for sid, (spec, trades, body, ranges) in ctx.items():
        seen: dict[tuple, str] = {}
        for who, rule in proposals[sid]:
            key = (rule.feature, rule.op, rule.kind)
            if key in seen:
                chat.say(sid, "check", "council", "system", f"{who}'s {rule.text()} merges with {seen[key]}'s rule on the same feature.")
                continue
            if any(c.f == rule.feature for _, c in spec.entry.rules()):
                chat.say(sid, "check", "council", "system", f"{who}'s {rule.text()} is skipped: {sid} already has a rule on {rule.feature}.")
                continue
            seen[key] = who
            stats = in_sample(rule, trades)
            ok, why = helpful(stats)
            row = dict(idea_id=new_id("i-"), ts=now, gen=gen, strategy_id=sid, feature=rule.feature, op=rule.op,
                       v=rule.value, kind=rule.kind, reason=rule.reason, proposed_by=who, votes_json="{}",
                       score=None, approved=0, in_sample_json=json.dumps(stats),
                       status="testing" if ok else "unhelpful", status_reason=why)
            live = live_check(rule, live_rows.get(sid) or [])
            chat.say(sid, "check", "council", "system",
                     f"{'✓' if ok else '✗'} {who}'s {rule.text()}: {'passes the data check, ' if ok else 'dropped, '}{why}."
                     + (f" ({live})" if live else ""), {"stats": stats, "passed": ok, "live": live})
            if ok:
                survivors.setdefault(sid, []).append((row, rule, stats))
            else:
                store.insert_council_idea(row)
                created.append(row)
        if sid not in survivors:
            log_fn(f"council: {sid}: {len(proposals[sid])} proposals, none would have helped")
            chat.say(sid, "check", "council", "system",
                     f"{sid}: {len(proposals[sid])} proposal(s), none survived the data check, so there is nothing to vote on.")

    # 3. vote
    votes: dict[str, dict] = {r["idea_id"]: {} for rows in survivors.values() for r, _, _ in rows}
    questions = []
    for sid, rows in survivors.items():
        listing = "\n".join(f"  {i + 1}. {rule.text()} — {why_ok(stats)}"
                            + (f"; {live_check(rule, live_rows.get(sid) or [])}" if live_rows.get(sid) else "")
                            + f". Proposer's reason: {rule.reason}"
                            for i, (_, rule, stats) in enumerate(rows))
        vote_text = (ctx[sid][2] + f"\nThe council proposed these rules; each was checked on the same trades:\n"
                     f"{listing}\n\nVote on each. Approve only rules with a believable market reason that are "
                     "likely to hold on new coins, not just fit these trades. Answer "
                     '{"votes": [{"n": 1, "approve": true or false, "confidence": 0.0-1.0, "reason": "..."}]}')
        questions.append((sid, f"vote {sid}", vote_text))
        chat.say(sid, "vote", "council", "system", f"{sid}: vote on {len(rows)} rule(s) that passed the data check.",
                 {"rules": [rule.text() for _, rule, _ in rows]})

    def voted(m, sid, ans):
        lines = []
        for v in ans.get("votes") or []:
            try:
                rule = survivors[sid][int(v["n"]) - 1][1]
                conf = min(1.0, max(0.0, float(v.get("confidence") or 0.5)))
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            mark = "✓ approve" if v.get("approve") else "✗ reject"
            lines.append(f"{mark} {rule.text()} ({conf:.0%}): {str(v.get('reason', '')).strip()}")
        return "\n".join(lines) or "(no usable vote)"

    answers = _ask_all(store, members, questions, log_fn, started, cfg.max_seconds,
                       chat.events("vote", voted), settings=settings) if questions else {}
    for m in members:
        for sid, rows in survivors.items():
            ans = answers.get(m.name, {}).get(sid)
            for v in (ans or {}).get("votes") or []:
                try:
                    row = rows[int(v["n"]) - 1][0]
                    votes[row["idea_id"]][m.name] = dict(approve=bool(v.get("approve")),
                                                         confidence=min(1.0, max(0.0, float(v.get("confidence") or 0.5))),
                                                         reason=str(v.get("reason", ""))[:200])
                except (KeyError, ValueError, TypeError, IndexError):
                    continue

    # 4. tally
    for sid, rows in survivors.items():
        for row, rule, stats in rows:
            vs = votes[row["idea_id"]]
            total = sum(weight(reps.get(n)) for n in vs) or 1.0
            yes = sum(weight(reps.get(n)) * v["confidence"] for n, v in vs.items() if v["approve"])
            score = yes / total if vs else 0.0
            approved = bool(vs) and score >= cfg.approve_score
            row.update(votes_json=json.dumps(vs), score=round(score, 3), approved=int(approved),
                       status="testing" if approved else "rejected",
                       status_reason=why_ok(stats) if approved else f"council score {score:.2f} < {cfg.approve_score}")
            store.insert_council_idea(row)
            created.append(row)
            log_fn(f"council: {sid}: {'APPROVED' if approved else 'rejected'} {rule.text()} "
                   f"(score {score:.2f}, {len(vs)} votes; {why_ok(stats)})")
            yes_n = sum(1 for v in vs.values() if v["approve"])
            chat.say(sid, "verdict", "council", "system",
                     (f"APPROVED: {rule.text()}. Score {score:.2f} ({yes_n} of {len(vs)} for). "
                      "The swarm breeds children carrying it from the next generation; their live trades judge it.")
                     if approved else
                     f"Rejected: {rule.text()}. Score {score:.2f} ({yes_n} of {len(vs)} for), below {cfg.approve_score}.",
                     {"idea_id": row["idea_id"], "score": round(score, 3), "approved": approved})
    n_ok = sum(1 for r in created if r["approved"])
    chat.say("", "close", "council", "system",
             f"Sitting closed after {time.time() - started:.0f}s: {n_ok} rule(s) approved, {len(created) - n_ok} dropped.")
    return created


def _ask_timed(m: CouncilMember, purpose: str, text: str) -> tuple[dict | None, dict]:
    """One question to one judge, plus its council_calls row. Touches no store, so it is thread-safe."""
    t0 = time.time()
    try:
        ans, err = ask(m, SYSTEM, text), ""
    except Exception as exc:  # noqa: BLE001 - one judge failing never stops the sitting
        ans, err = None, str(exc)[:200]
    return ans, dict(ts=time.time(), member=m.name, purpose=purpose, seconds=time.time() - t0,
                     ok=int(ans is not None), error=err)


def _ask_all(store: Store, members: list[CouncilMember], questions: list[tuple[object, str, str]], log_fn,
             started: float | None = None, budget_s: float | None = None, on_event=None,
             settings: Settings | None = None) -> dict[str, dict]:
    """Every judge answers every (key, purpose, text) question → {judge: {key: answer or None}}.
    Backup judges (fallback: true) are only asked the questions no other judge answered, with no time
    budget: they are the last line of defense, so a slow sitting still ends with an answer.
    on_event(kind, member, key, answer, call_row) hears each step live: ask | done | late | backup."""
    # with every Cursor judge benched, the remote backups lead; a local judge never leads (it waits for its tier)
    primary = [m for m in members if not m.fallback] or [m for m in members if m.kind != "ollama"]
    backup = [m for m in members if m not in primary]
    answers = _ask_round(store, primary, questions, log_fn, started, budget_s, on_event) if primary else {}
    unanswered = lambda: [q for q in questions if all(answers.get(m.name, {}).get(q[0]) is None  # noqa: E731
                                                      for m in members if m.name in answers)]
    # backups in tiers: remote ones (9router) first; local GPU judges only when Cursor is out (_local_ok)
    for tier in ([m for m in backup if m.kind != "ollama"], [m for m in backup if m.kind == "ollama"]):
        missing = unanswered()
        if not tier or not missing:
            continue
        if tier[0].kind == "ollama" and not _local_ok(settings, primary, answers):
            log_fn(f"council: {len(missing)} question(s) unanswered; local judges stay off while Cursor is up")
            continue
        log_fn(f"council: {len(missing)} question(s) unanswered, asking backup judges {', '.join(m.name for m in tier)}")
        if on_event:
            on_event("backup", None, None, [q[0] for q in missing], [m.name for m in tier])
        answers.update(_ask_round(store, tier, missing, log_fn, on_event=on_event))
    for m in backup:
        answers.setdefault(m.name, {})      # not needed this time: no answers, but every judge has an entry
    return answers


def _ask_round(store: Store, members: list[CouncilMember], questions: list[tuple[object, str, str]], log_fn,
               started: float | None = None, budget_s: float | None = None, on_event=None) -> dict[str, dict]:
    """Ollama judges share one GPU, so they answer one after another in a single thread (one model load
    each); every other judge is a remote API and gets its own thread. Workers only talk to the judges and
    report through a queue; the SQLite store (call log, transcript) is written here, on the calling thread,
    as each answer arrives. A judge that runs past the budget skips its remaining questions."""
    events: queue.Queue = queue.Queue()

    def one(m: CouncilMember):
        out = {}
        for key, purpose, text in questions:
            if started is not None and budget_s and time.time() - started > budget_s:
                events.put(("late", m, key, None, None))
                break
            events.put(("ask", m, key, None, None))
            out[key], row = _ask_timed(m, purpose, text)
            events.put(("done", m, key, out[key], row))
        return out

    local = [m for m in members if m.kind == "ollama"]
    remote = [m for m in members if m.kind != "ollama"]
    done: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=1 + len(remote)) as pool:
        futs = [pool.submit(lambda: {m.name: one(m) for m in local})] if local else []
        futs += [pool.submit(lambda m=m: {m.name: one(m)}) for m in remote]
        while True:
            try:
                kind, m, key, ans, row = events.get(timeout=0.05)
            except queue.Empty:
                if all(f.done() for f in futs) and events.empty():
                    break               # every worker finished and everything it reported is handled
                continue
            if kind == "done":
                store.log_council_call(row)
                if not row["ok"]:
                    log_fn(f"council: {m.name} failed ({row['purpose']}): {row['error']}")
            elif kind == "late":
                log_fn(f"council: {m.name} out of time this generation")
            if on_event:
                on_event(kind, m, key, ans, row)
        for f in futs:
            done.update(f.result())
    return {m.name: done[m.name] for m in members}


def why_ok(stats: dict) -> str:
    return f"would have saved {stats['saved_sol']:.3f} SOL, blocking {stats['blocked']}/{stats['n']} trades"


def _ban_line(c: dict) -> str:
    """One short line per suspect: the facts a judge needs, not the raw evidence dump."""
    e = c.get("evidence") or {}
    if c["kind"] == "cluster":
        each = e.get("tokens_each") or {}
        facts = (f"{len(c['wallets'])} wallets bought the same {e.get('shared_tokens')} tokens within 30s of each other; "
                 f"tokens per wallet {sorted(each.values())[:6]}")
    else:
        facts = (f"creator; token went ${e.get('start_mcap', 0):,.0f} → peak ${e.get('peak_mcap', 0):,.0f} "
                 f"→ ${e.get('low_after', 0):,.0f} within {e.get('crash_s', 0):.0f}s of the peak")
    return f"{c['kind']}: {facts}"


def review_bans(settings: Settings, store: Store, cands: list[dict], log_fn=lambda _: None) -> dict[int, tuple[bool, str]]:
    """Judges vote on wallets the scanner wants to ban. Returns {candidate index: (ban?, reasons)};
    candidates no judge answered on are missing (the caller decides)."""
    cfg = settings.council
    members = [m for m in cfg.members if available(m)]
    if not members or not cands:
        return {}
    members, paced = pace_cursor(settings, members)
    if paced:
        log_fn(f"council: {paced}")
    if not members:
        return {}
    reps = reputations(store)
    chat = Chat(store, store.evo_generation() or None)      # this generation's scores are in by now
    chat.say("bans", "bans", "council", "system",
             f"Ban review: the scanner flagged {len(cands)} suspect(s). Judges: {_judges_line(members, reps)}.\n"
             + "\n".join(f"#{i + 1} {_ban_line(c)}" for i, c in enumerate(cands)))
    intro = ("A scanner flagged these Solana wallets as possible pump-and-dump operators. Banned wallets make the bot "
             "refuse any token they created or buy, so a wrong ban blocks good trades, and a missed one lets us buy "
             "into a rug. Coordinated clusters (several wallets always buying the same tokens within seconds) usually "
             "fake organic demand; a creator whose token pumped then lost 90%+ within minutes usually dumped it. "
             "Ordinary fast traders who just happen to overlap are not operators.\n\n")
    batches = [cands[i:i + BAN_BATCH] for i in range(0, len(cands), BAN_BATCH)]
    votes: dict[int, dict[str, dict]] = {}
    questions = []
    for b, batch in enumerate(batches):
        listing = "\n".join(f"  {k + 1}. {_ban_line(c)}" for k, c in enumerate(batch))
        text = (intro + listing + f"\n\nVote on each of the {len(batch)}. Answer "
                '{"votes": [{"n": 1, "ban": true or false, "confidence": 0.0-1.0, "reason": "short"}]}')
        questions.append((b, f"ban review {b + 1}/{len(batches)}", text))
    def judged(m, b, ans):
        lines = []
        for v in ans.get("votes") or []:
            try:
                k = int(v["n"]) - 1
                conf = min(1.0, max(0.0, float(v.get("confidence") or 0.5)))
            except (KeyError, ValueError, TypeError):
                continue
            if 0 <= k < len(batches[b]):
                lines.append(f"#{b * BAN_BATCH + k + 1} {'ban' if v.get('ban') else 'keep'} ({conf:.0%}): "
                             f"{str(v.get('reason', '')).strip()}")
        return "\n".join(lines) or "(no usable vote)"

    answers = _ask_all(store, members, questions, log_fn,   # each judge answers all its batches in a row
                       on_event=chat.events("bans", judged), settings=settings)
    for m in members:
        for b, batch in enumerate(batches):
            ans = answers[m.name].get(b)
            for v in (ans or {}).get("votes") or []:
                try:
                    k = int(v["n"]) - 1
                    if 0 <= k < len(batch):
                        votes.setdefault(b * BAN_BATCH + k, {})[m.name] = dict(
                            ban=bool(v.get("ban")), confidence=min(1.0, max(0.0, float(v.get("confidence") or 0.5))),
                            reason=str(v.get("reason", ""))[:160])
                except (KeyError, ValueError, TypeError):
                    continue
    out = {}
    for i, vs in votes.items():
        total = sum(weight(reps.get(n)) for n in vs) or 1.0
        yes = sum(weight(reps.get(n)) * v["confidence"] for n, v in vs.items() if v["ban"])
        reasons = "; ".join(f"{n}: {'ban' if v['ban'] else 'keep'} ({v['reason']})" for n, v in vs.items())
        out[i] = (yes / total >= cfg.approve_score, f"score {yes / total:.2f} — {reasons}")
    chat.say("bans", "verdict", "council", "system", "\n".join(
        f"#{i + 1} {'BAN' if out[i][0] else 'keep'}: score {out[i][1].split(' — ')[0].split()[-1]}"
        if i in out else f"#{i + 1} no judge answered" for i in range(len(cands))))
    return out


# ── whale review ───────────────────────────────────────────────────────────
WHALE_DISTRUST = 0.25        # a weighted trust score at or below this: the whale's buys stop counting
WHALE_MIN_VOTES = 2          # fewer judges than this answered: no verdict, the wallet is asked again next time


def _whale_line(w: str, e: dict, now: float) -> str:
    win = "?" if e.get("win") is None else f"{e['win'] * 100:.0f}%"
    med = "?" if e.get("median_exit") is None else f"{e['median_exit']:.2f}x"
    hist = [h[1] for h in (e.get("history") or [])][-6:]
    trend = f"score {e['score']:.2f}" + (f" (was {hist[0]:.2f})" if len(hist) > 1 else "")
    chain = (f"chain check: {e['flag']}" if e.get("flag") else
             "chain check: no money from banned wallets" if e.get("traced") else "chain check: not traced yet")
    created = e.get("created")
    dev = (f"; launched {created} coins itself in the scan window (dev wallet?)" if created else
           "; launched no coins itself" if created == 0 else "")
    return (f"{w[:4]}…{w[-4:]} ({e.get('label') or 'unlabeled'}): {e.get('round_trips', 0)} round trips, win {win}, "
            f"median exit {med}, early on {e.get('early_good', 0)} tokens that ran 3x, {e.get('tokens', 0)} tokens traded; "
            f"{trend}; first seen {max(0.0, (now - e.get('first_seen', now)) / 3600):.0f}h ago; {chain}{dev}")


def review_whales(settings: Settings, store: Store, log_fn=lambda _: None, now: float | None = None) -> list[dict]:
    """Judges decide which whales to trust. The bot counts a whale's buys (whale_buys, which the swarm's
    bet-boost gene uses) and polls it live only while it is not distrusted; trusted ones get the live
    polling slots first. A few calls per generation, never per trade."""
    from solmeme.lab.whales import WhaleBook

    cfg = settings.council
    now = now or time.time()
    book = WhaleBook(settings.data_dir)
    todo = book.to_review(now)
    members = [m for m in cfg.members if available(m)]
    if not todo or not members:
        return []
    members, paced = pace_cursor(settings, members, now)
    if paced:
        log_fn(f"council: {paced}")
    if not members:
        return []
    reps = reputations(store)
    chat = Chat(store, store.evo_generation() or None)
    listing = "\n".join(f"  {i + 1}. {_whale_line(w, book.wallets[w], now)}" for i, w in enumerate(todo))
    chat.say("whales", "whales", "council", "system",
             f"Whale review: should the bot trust these {len(todo)} wallets as a signal? Judges: {_judges_line(members, reps)}.\n"
             + "\n".join(f"#{i + 1} {_whale_line(w, book.wallets[w], now)}" for i, w in enumerate(todo)))
    text = ("These Solana wallets keep buying meme coins early that later ran 3x, and exit well. The bot bets more "
            "on a coin when trusted whales bought it in the last 10 minutes (it still needs its own entry rules). "
            "Trust a wallet only if it looks like a skilled trader whose buys are worth following. Distrust ones "
            "that look like insiders or operators: launch-block snipers and bundlers who get in early because they "
            "are part of the launch, dev or team wallets, coordinated groups, or anyone whose good record comes "
            "from a handful of trades. Followers of those get dumped on.\n\n" + listing +
            f"\n\nVote on each of the {len(todo)}. Answer "
            '{"votes": [{"n": 1, "trust": true or false, "confidence": 0.0-1.0, "reason": "short"}]}')

    def judged(m, _key, ans):
        lines = []
        for v in ans.get("votes") or []:
            try:
                k, conf = int(v["n"]) - 1, min(1.0, max(0.0, float(v.get("confidence") or 0.5)))
            except (KeyError, ValueError, TypeError):
                continue
            if 0 <= k < len(todo):
                lines.append(f"#{k + 1} {'trust' if v.get('trust') else 'distrust'} ({conf:.0%}): {str(v.get('reason', '')).strip()}")
        return "\n".join(lines) or "(no usable vote)"

    answers = _ask_all(store, members, [("whales", "whale review", text)], log_fn,
                       on_event=chat.events("whales", judged), settings=settings)
    out = []
    for i, w in enumerate(todo):
        vs = {}
        for m in members:
            for v in (answers.get(m.name, {}).get("whales") or {}).get("votes") or []:
                try:
                    if int(v["n"]) - 1 == i:
                        vs[m.name] = dict(trust=bool(v.get("trust")),
                                          confidence=min(1.0, max(0.0, float(v.get("confidence") or 0.5))),
                                          reason=str(v.get("reason", ""))[:160])
                except (KeyError, ValueError, TypeError):
                    continue
        if len(vs) < WHALE_MIN_VOTES:     # Oct 9: 9router alone distrusted 6 whales while the others were down
            continue
        total = sum(weight(reps.get(n)) for n in vs) or 1.0
        score = sum(weight(reps.get(n)) * v["confidence"] for n, v in vs.items() if v["trust"]) / total
        verdict = "trust" if score >= cfg.approve_score else "distrust" if score <= WHALE_DISTRUST else "unsure"
        e = book.wallets[w]
        e.update(council=verdict, council_score=round(score, 3), council_ts=now, council_at_score=e["score"],
                 council_reason="; ".join(f"{n}: {'trust' if v['trust'] else 'distrust'} ({v['reason']})" for n, v in vs.items())[:600])
        out.append(dict(wallet=w, verdict=verdict, score=round(score, 3), votes=len(vs)))
        log_fn(f"council: whale {w[:6]}…{w[-4:]} → {verdict} (score {score:.2f}, {len(vs)} votes)")
    book.save()
    chat.say("whales", "verdict", "council", "system", "\n".join(
        f"#{todo.index(o['wallet']) + 1} {o['verdict'].upper() if o['verdict'] != 'unsure' else 'unsure'}: score {o['score']:.2f}"
        for o in out) or "No judge answered; nothing changes.")
    return out

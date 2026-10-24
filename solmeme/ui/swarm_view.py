"""JSON view of the strategy swarm for the website: generations, agents with equity curves from
their stored live trades, lineage, deaths, lessons and what each kind of mutation has done."""

from __future__ import annotations

import json
from collections import defaultdict

from solmeme.config import Settings
from solmeme.core.store import Store
from solmeme.lab.evolve import Credit

TRADE_KEYS = ("symbol", "mint", "entry_ts", "exit_ts", "pnl_sol", "pnl_pct", "exit_reason", "hold_s", "mfe_x")


def build_swarm(store: Store, settings: Settings, sol_usd: float | None) -> dict:
    cfg = settings.evolution
    start_sol = (settings.portfolio.starting_usd / sol_usd) if settings.portfolio.starting_usd and sol_usd \
        else settings.portfolio.starting_sol
    from solmeme.sim.wallet import wallet_reset_ts
    reset = wallet_reset_ts(settings)
    log = store.evo_rows()
    born = {r["version"]: r for r in log if r["event"] == "born"}
    ended = {r["version"]: r for r in log if r["event"] in ("killed", "promoted")}
    gen_now = store.evo_generation()

    latest_score: dict[str, dict] = {}
    gens: dict[int, dict] = {}
    for row in store.query("SELECT * FROM evo_scores ORDER BY gen"):
        latest_score[row["key"]] = row
        g = gens.setdefault(row["gen"], dict(gen=row["gen"], ts=row["ts"], fitness=[], alive=0))
        g["fitness"].append(row["fitness"])
        if row["role"] in ("swarm", "newborn"):
            g["alive"] += 1
    counts: dict[int, dict] = defaultdict(lambda: dict(born=0, killed=0, screened_out=0, promoted=0))
    for r in log:
        counts[r["gen"]][r["event"]] = counts[r["gen"]].get(r["event"], 0) + 1
    generations = []
    for g in sorted(set(gens) | set(counts)):
        fit = sorted(f for f in gens.get(g, {}).get("fitness", []) if f is not None and f > -100)
        generations.append(dict(
            gen=g, ts=gens.get(g, {}).get("ts") or next((r["ts"] for r in log if r["gen"] == g), None),
            alive=gens.get(g, {}).get("alive", 0),
            best=fit[-1] if fit else None, median=fit[len(fit) // 2] if fit else None,
            **{k: counts[g].get(k, 0) for k in ("born", "killed", "screened_out", "promoted")}))

    statuses = {(v["strategy_id"], v["version"]): v for v in store.versions()}
    agents = []
    for version, b in born.items():
        sid = b["strategy_id"]
        v = statuses.get((sid, version))
        if v is None:
            continue
        trades = [t for t in store.trades(sid, version, run_mode="live") if (t["entry_ts"] or 0) >= b["ts"]]
        pnl, wins = 0.0, 0
        wstart = max(b["ts"], reset)
        curve, wpnl = [[wstart, 1.0]], 0.0                 # wallet: from the last wallet reset
        for t in trades:
            pnl += float(t["pnl_sol"] or 0)
            wins += 1 if (t["pnl_sol"] or 0) > 0 else 0
            if (t["entry_ts"] or 0) >= reset:
                wpnl += float(t["pnl_sol"] or 0)
                curve.append([t["exit_ts"], round(1 + wpnl / start_sol, 4) if start_sol else 1.0])
        score = latest_score.get(f"{sid}@{version}") or {}
        sizing = json.loads(v["spec_json"]).get("sizing") or {}
        end = ended.get(version)
        agents.append(dict(
            key=f"{sid}@{version}", species=sid, version=version, status=v["status"],
            gen=b["gen"], born_ts=b["ts"], parent=b["parent_version"], mate=b["mate_version"],
            ops=json.loads(b["ops"] or "[]"),
            replay=score.get("replay", b["fitness"]), replay_n=score.get("replay_n"),
            fitness=score.get("fitness", b["fitness"]), parent_fitness=b["parent_fitness"],
            ended_ts=end["ts"] if end else None, reason=(end or {}).get("reason") or (v["note"] if v["status"] == "dead" else ""),
            live_n=len(trades), wins=wins, pnl_sol=pnl, pnl_usd=pnl * sol_usd if sol_usd else None,
            equity=1 + wpnl / start_sol if start_sol else 1.0,
            bet_pct=sizing.get("pct") if sizing.get("pct") is not None else settings.portfolio.position_pct,
            curve=curve,
            trades=[{k: t.get(k) for k in TRADE_KEYS} for t in trades[-20:]][::-1],
        ))
    agents.sort(key=lambda a: (a["status"] != "swarm", -(a["fitness"] if a["fitness"] is not None else -1e9)))

    species = []
    for sid in sorted({a["species"] for a in agents} | {k.split("@")[0] for k in latest_score}):
        if sid in cfg.species_exclude:
            continue
        champ = store.champion(sid)
        mine = [a for a in agents if a["species"] == sid]
        alive = [a for a in mine if a["status"] == "swarm"]
        best = max(alive, key=lambda a: a["fitness"] if a["fitness"] is not None else -1e9) if alive else None
        ck = f"{sid}@{champ['version']}" if champ else None
        species.append(dict(
            id=sid, champion=ck, champion_fitness=(latest_score.get(ck) or {}).get("fitness") if ck else None,
            alive=len(alive), dead=sum(1 for a in mine if a["status"] == "dead"),
            promoted=sum(1 for a in mine if a["status"] in ("champion", "retired") and a["ended_ts"]),
            best=best["key"] if best else None, best_fitness=best["fitness"] if best else None,
            live_pnl_sol=sum(a["pnl_sol"] for a in alive)))

    lessons = store.query("SELECT * FROM evo_lessons WHERE gen=? ORDER BY saved_sol DESC", [gen_now])
    events = [dict(ts=r["ts"], gen=r["gen"], event=r["event"], key=f"{r['strategy_id']}@{r['version']}",
                   reason=r["reason"] or "", ops=json.loads(r["ops"] or "[]"), fitness=r["fitness"],
                   parent_fitness=r["parent_fitness"])
              for r in log if r["event"] != "screened_out"][-60:][::-1]
    return dict(
        best=build_best(store, settings, sol_usd),
        council=_council(store, settings),
        generation=gen_now,
        last_ts=generations[-1]["ts"] if generations else None,
        config=dict(population=cfg.population, every_min=cfg.every_min, bust_frac=cfg.bust_frac,
                    min_live_trades=cfg.min_live_trades, kill_bottom_frac=cfg.kill_bottom_frac,
                    kill_min_trades=cfg.kill_min_trades, kill_confidence=cfg.kill_confidence,
                    idle_kill_h=cfg.idle_kill_h, enabled=cfg.enabled,
                    position_pct=settings.portfolio.position_pct),
        start_sol=start_sol,
        generations=generations,
        species=species,
        agents=agents,
        lessons=[dict(strategy_id=l["strategy_id"], feature=l["feature"], op=l["op"], v=l["v"],
                      saved_sol=l["saved_sol"], blocked=l["blocked"], of=l["n"]) for l in lessons],
        credit=Credit(log).table()[:40],
        events=events,
    )


def _council(store: Store, settings: Settings) -> dict:
    from solmeme.ai.council import available, reputations, weight

    cfg = settings.council
    reps = reputations(store)
    calls = store.council_calls(since=0)
    ideas = store.council_ideas()
    members = []
    for m in cfg.members:
        mine = [c for c in calls if c["member"] == m.name]
        ok = [c for c in mine if c["ok"]]
        proposed = [i for i in ideas if i["proposed_by"] == m.name]
        members.append(dict(
            name=m.name, model=m.model, kind=m.kind, available=available(m), enabled=m.enabled,
            missing_key=bool(m.key_env) and not available(m) and m.enabled and bool(m.model),
            calls=len(mine), ok=len(ok), avg_s=sum(c["seconds"] for c in ok) / len(ok) if ok else None,
            last_ts=mine[-1]["ts"] if mine else None, last_error=next((c["error"] for c in reversed(mine) if not c["ok"]), ""),
            reputation=reps.get(m.name), weight=weight(reps.get(m.name)),
            proposed=len(proposed), working=sum(1 for i in proposed if i["status"] == "working"),
            failed=sum(1 for i in proposed if i["status"] == "failed")))
    rows = []
    for i in reversed(ideas[-60:]):
        rows.append(dict(
            idea_id=i["idea_id"], ts=i["ts"], gen=i["gen"], strategy_id=i["strategy_id"], feature=i["feature"],
            op=i["op"], v=i["v"], kind=i["kind"], reason=i["reason"] or "", proposed_by=i["proposed_by"],
            votes=json.loads(i["votes_json"] or "{}"), score=i["score"], approved=bool(i["approved"]),
            in_sample=json.loads(i["in_sample_json"] or "{}"), status=i["status"], status_reason=i["status_reason"] or "",
            carriers=i["carriers"] or 0, live_trades=i["live_trades"] or 0, live_pnl_sol=i["live_pnl_sol"] or 0.0,
            deaths=i["deaths"] or 0))
    return dict(enabled=cfg.enabled, members=members, ideas=rows)


# ── best performing + agent profile ───────────────────────────────────────────
LIVE_STATUSES = ("swarm", "champion", "trial", "challenger")


def _start_sol(settings: Settings, sol_usd: float | None) -> float:
    pf = settings.portfolio
    return pf.starting_usd / sol_usd if pf.starting_usd and sol_usd else pf.starting_sol


def _since(store: Store, sid: str, version: str, row: dict, settings: Settings | None = None) -> float:
    """Where this version's wallet starts: its birth, or the last wallet reset if that is later."""
    born = store.evo_rows(version=version, event="born")
    start = born[0]["ts"] if born else row["created_ts"]
    if settings is not None:
        from solmeme.sim.wallet import wallet_reset_ts
        start = max(start, wallet_reset_ts(settings))
    return start


def _live(store: Store, sid: str, version: str, since: float) -> list[dict]:
    return [t for t in store.trades(sid, version, run_mode="live") if (t["entry_ts"] or 0) >= since]


def _by_token(trades: list[dict]) -> list[dict]:
    g: dict[str, dict] = {}
    for t in trades:
        e = g.setdefault(t["mint"], dict(mint=t["mint"], symbol=t.get("symbol") or t["mint"][:6], trades=0, pnl_sol=0.0,
                                         best_pct=None, peak_x=None))
        e["trades"] += 1
        e["pnl_sol"] += float(t["pnl_sol"] or 0)
        e["best_pct"] = max(e["best_pct"] if e["best_pct"] is not None else -1e9, float(t["pnl_pct"] or 0))
        e["peak_x"] = max(e["peak_x"] or 0, float(t.get("mfe_x") or 0))
    total_win = sum(e["pnl_sol"] for e in g.values() if e["pnl_sol"] > 0) or 1.0
    for e in g.values():
        e["share_of_profit"] = e["pnl_sol"] / total_win if e["pnl_sol"] > 0 else 0.0
    return sorted(g.values(), key=lambda e: -e["pnl_sol"])


def build_best(store: Store, settings: Settings, sol_usd: float | None, top: int = 5, min_trades: int = 5) -> list[dict]:
    """Every live strategy version ranked by how far it grew its own wallet since it started."""
    start_sol = _start_sol(settings, sol_usd)
    rows = store.query(f"SELECT * FROM strategy_versions WHERE status IN ({','.join('?' * len(LIVE_STATUSES))})",
                       list(LIVE_STATUSES))
    out = []
    for r in rows:
        sid, v = r["strategy_id"], r["version"]
        trades = _live(store, sid, v, _since(store, sid, v, r, settings))
        if len(trades) < min_trades:
            continue
        pnl = sum(float(t["pnl_sol"] or 0) for t in trades)
        curve, run = [], 0.0
        for t in sorted(trades, key=lambda t: t["exit_ts"] or 0):
            run += float(t["pnl_sol"] or 0)
            curve.append([t["exit_ts"], round(1 + run / start_sol, 4)])
        tokens = _by_token(trades)
        out.append(dict(key=f"{sid}@{v}", status=r["status"], trades=len(trades),
                        win=sum(float(t["pnl_sol"] or 0) > 0 for t in trades) / len(trades),
                        pnl_sol=pnl, pnl_usd=pnl * sol_usd if sol_usd else None, equity=1 + pnl / start_sol,
                        curve=curve, top_token=tokens[0] if tokens and tokens[0]["pnl_sol"] > 0 else None))
    out.sort(key=lambda a: -a["equity"])
    return out[:top]


def _resolve(spec, x):
    try:
        return spec.p(x)
    except Exception:  # noqa: BLE001
        return x


def describe_spec(spec) -> dict:
    rules = [dict(section=sec, id=c.id, feature=c.f, op=c.op, value=_resolve(spec, c.v), param=c.v if isinstance(c.v, str) else None,
                  locked=c.locked) for sec, c in spec.entry.rules()]
    rules += [dict(section=f"score +{_resolve(spec, t.w)}", id=t.id, feature=t.f, op=t.op, value=_resolve(spec, t.v),
                   param=t.v if isinstance(t.v, str) else None, locked=t.locked) for t in spec.entry.score]
    if spec.entry.score:
        rules.append(dict(section="score", id="min_score", feature="buy at", op=">=", value=_resolve(spec, spec.entry.min_score),
                          param=spec.entry.min_score if isinstance(spec.entry.min_score, str) else None, locked=False))
    ex = spec.exits
    exits = dict(take_profit=[[_resolve(spec, t.at_x), _resolve(spec, t.sell_pct)] for t in ex.take_profit],
                 stop_loss_pct=_resolve(spec, ex.stop_loss_pct), trailing_pct=_resolve(spec, ex.trailing_pct),
                 trailing_activate_x=_resolve(spec, ex.trailing_activate_x), max_hold_s=_resolve(spec, ex.max_hold_s),
                 stale_after_s=_resolve(spec, ex.stale_after_s), on_rug=ex.on_rug, on_dev_sell=ex.on_dev_sell,
                 on_kol_sell=ex.on_kol_sell, on_migration=ex.on_migration)
    return dict(description=spec.description.strip(), universe=spec.universe.model_dump(), rules=rules, exits=exits,
                sizing=dict(pct=spec.sizing.pct, sol=_resolve(spec, spec.sizing.sol), max_open=spec.sizing.max_open),
                params={k: dict(value=p.value, lo=p.lo, hi=p.hi) for k, p in spec.params.items()})


def _diff(child: dict, parent: dict) -> list[str]:
    out = []
    pr = {r["id"]: r for r in parent["rules"]}
    cr = {r["id"]: r for r in child["rules"]}
    for rid, r in cr.items():
        if rid not in pr:
            out.append(f"added {r['section']} rule: {r['feature']} {r['op']} {r['value']}")
        elif (r["op"], r["value"]) != (pr[rid]["op"], pr[rid]["value"]):
            out.append(f"{r['feature']}: {pr[rid]['op']} {pr[rid]['value']} → {r['op']} {r['value']}")
    for rid, r in pr.items():
        if rid not in cr:
            out.append(f"removed rule: {r['feature']} {r['op']} {r['value']}")
    for k, v in child["exits"].items():
        if parent["exits"].get(k) != v:
            out.append(f"exit {k}: {parent['exits'].get(k)} → {v}")
    if child["sizing"]["pct"] != parent["sizing"]["pct"]:
        out.append(f"bet size: {parent['sizing']['pct'] or 'default'} → {child['sizing']['pct']}% of wallet")
    return out


def agent_detail(store: Store, settings: Settings, key: str, sol_usd: float | None) -> dict:
    from solmeme.strategy.spec import spec_from_json

    sid, _, version = key.partition("@")
    row = store.get_version(sid, version)
    if row is None:
        return {"error": f"unknown strategy {key}"}
    start_sol = _start_sol(settings, sol_usd)
    spec = spec_from_json(row["spec_json"])
    since = _since(store, sid, version, row, settings)                   # wallet: since the last reset
    trades = sorted(_live(store, sid, version, since), key=lambda t: t["exit_ts"] or 0)
    all_trades = sorted(_live(store, sid, version, _since(store, sid, version, row)), key=lambda t: t["exit_ts"] or 0)
    curve, run = [[since, 1.0]], 0.0
    for t in trades:
        run += float(t["pnl_sol"] or 0)
        curve.append([t["exit_ts"], round(1 + run / start_sol, 4), (t.get("symbol") or t["mint"][:6]), round(float(t["pnl_pct"] or 0), 1)])
    me = describe_spec(spec)

    # family tree: walk parents back to the seed, then list children
    lineage, cur, seen = [], row, set()
    while cur is not None and cur["version"] not in seen and len(lineage) < 15:
        seen.add(cur["version"])
        b = store.evo_rows(version=cur["version"], event="born")
        node_since = b[0]["ts"] if b else cur["created_ts"]
        tr = _live(store, sid, cur["version"], node_since)
        lineage.append(dict(version=cur["version"], status=cur["status"], created_ts=cur["created_ts"], note=cur["note"] or "",
                            gen=b[0]["gen"] if b else None, ops=json.loads(b[0]["ops"]) if b else [],
                            mate=b[0]["mate_version"] if b else None, replay=b[0]["fitness"] if b else None,
                            trades=len(tr), pnl_sol=sum(float(t["pnl_sol"] or 0) for t in tr)))
        parent_v = cur["parent_version"] or (b[0]["parent_version"] if b else None)
        cur = store.get_version(sid, parent_v) if parent_v else None
    parent_spec = None
    if len(lineage) > 1:
        prow = store.get_version(sid, lineage[1]["version"])
        parent_spec = describe_spec(spec_from_json(prow["spec_json"])) if prow else None
    children = [dict(version=v["version"], status=v["status"], created_ts=v["created_ts"])
                for v in store.query("SELECT version, status, created_ts FROM strategy_versions WHERE strategy_id=? AND parent_version=? "
                                     "ORDER BY created_ts", [sid, version])]
    pnl = sum(float(t["pnl_sol"] or 0) for t in trades)
    from solmeme.lab import winners as winlab
    book = winlab.load(settings.data_dir)
    insight = next((w for w in book.get("winners") or [] if w["key"] == key), None)
    return dict(
        insight=insight, stress=(book.get("stress") or {}).get(key),
        key=key, status=row["status"], note=row["note"] or "", since=since, start_sol=start_sol,
        trades_n=len(trades), win=(sum(float(t["pnl_sol"] or 0) > 0 for t in trades) / len(trades)) if trades else None,
        pnl_sol=pnl, pnl_usd=pnl * sol_usd if sol_usd else None, equity=1 + pnl / start_sol if start_sol else 1.0,
        best_trade=max((float(t["pnl_pct"] or 0) for t in trades), default=None),
        worst_trade=min((float(t["pnl_pct"] or 0) for t in trades), default=None),
        curve=curve, tokens=_by_token(trades)[:15],
        trades=[{k: t.get(k) for k in ("symbol", "mint", "entry_ts", "exit_ts", "pnl_sol", "pnl_pct", "exit_reason", "hold_s", "mfe_x", "size_sol")}
                for t in all_trades[::-1][:200]],                          # every trade it ever made stays visible
        lifetime=dict(trades=len(all_trades), pnl_sol=sum(float(t["pnl_sol"] or 0) for t in all_trades)),
        lineage=lineage, children=children, spec=me, changes_vs_parent=_diff(me, parent_spec) if parent_spec else [],
    )

"""Markdown reports (reports/YYYY-MM-DD.md)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from solmeme.config import Settings
from solmeme.core.eventlog import time_range
from solmeme.core.store import Store
from solmeme.lab.metrics import Metrics, by_version, compute


def _iso(ts) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if ts else "—"


def metrics_table(ms: list[Metrics], roles: dict[str, str] | None = None) -> str:
    roles = roles or {}
    head = "| strategy@version | role | trades | win % | PnL SOL | exp % / trade | median % | PF | max DD | ex-top3 | rug % | sold-early % |\n"
    head += "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|\n"
    rows = []
    for m in sorted(ms, key=lambda m: -m.total_pnl_sol):
        pf = "∞" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        rows.append(f"| `{m.key}` | {roles.get(m.key, '')} | {m.n_trades} | {m.win_rate * 100:.0f} | "
                    f"{m.total_pnl_sol:+.3f} | {m.expectancy_pct:+.1f} | {m.median_pct:+.1f} | {pf} | "
                    f"{m.max_dd_sol:.3f} | {m.pnl_ex_top3_sol:+.3f} | {m.rug_hit_rate * 100:.0f} | "
                    f"{m.early_exit_rate * 100:.0f} |")
    return head + "\n".join(rows) if rows else "_no trades yet_"


def daily_report(settings: Settings, store: Store, days: float = 7) -> Path:
    since = time.time() - days * 86400
    versions = store.versions()
    roles = {f"{v['strategy_id']}@{v['version']}": v["status"] for v in versions}
    live = store.trades(run_mode="live", since=since)
    grouped = by_version(live)
    ms = [compute(ts, key) for key, ts in grouped.items()]
    rng = time_range(settings.events_dir)

    lines = [
        f"# SolMeme-Trader report — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        f"Recorded data: {_iso(rng[0]) if rng else '—'} → {_iso(rng[1]) if rng else '—'} UTC · "
        f"LLM spend today ${store.llm_spend_since(time.time() - 86400):.2f}, "
        f"last {days:.0f}d ${store.llm_spend_since(since):.2f}",
        "",
        f"## Live paper performance (last {days:.0f} days)",
        "",
        metrics_table(ms, roles),
        "",
        "## Strategy versions",
        "",
        "| strategy | version | status | since | note |",
        "|---|---|---|---|---|",
    ]
    for v in versions:
        if v["status"] in ("champion", "challenger") or v["status_ts"] >= since:
            lines.append(f"| {v['strategy_id']} | `{v['version']}` | {v['status']} | {_iso(v['status_ts'])} | "
                         f"{(v['note'] or '')[:80]} |")
    lines += ["", "## Proposals", "", "| when | strategy | status | hypothesis | outcome |", "|---|---|---|---|---|"]
    for p in store.proposals(limit=30):
        lines.append(f"| {_iso(p['created_ts'])} | {p['strategy_id']} | {p['status']} | "
                     f"{(p['hypothesis'] or '')[:110].replace('|', '/')} | {(p['reason'] or '')[:110].replace('|', '/')} |")
    lines += ["", "## Latest AI reviews", ""]
    for r in store.query("SELECT * FROM reviews ORDER BY ts DESC LIMIT 5"):
        res = json.loads(r["result_json"] or "{}")
        lines += [f"### {r['strategy_id']}@{r['version']} — {_iso(r['ts'])} ({r['model']}, ${r['cost_usd']:.3f})", "",
                  res.get("summary", ""), ""]
        for f in res.get("findings", [])[:6]:
            lines.append(f"- **{f['category']}** ({f['confidence']}, ~{f['est_pnl_impact_sol']:+.3f} SOL): "
                         f"{f['description']}")
        for fr in res.get("feature_requests", [])[:5]:
            lines.append(f"- _feature request:_ {fr}")
        lines.append("")
    out = settings.reports_dir / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return out


def backtest_report(settings: Settings, ms: list[Metrics], roles: dict[str, str], window: tuple[float, float],
                    extra: str = "") -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    out = settings.reports_dir / f"backtest_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"# Backtest {stamp}\n\nWindow: {_iso(window[0])} → {_iso(window[1])} UTC\n\n"
                   + metrics_table(ms, roles) + "\n\n" + extra + "\n")
    return out

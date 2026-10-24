"""AI notes on the session report, through the Claude Code CLI or a local Ollama model.

The report itself is free and written every time. The AI is asked only when it has something
new to read: trades closed or problems flagged since it was last asked. Claude runs as
`claude -p` with every tool disabled, so it can only read the report it is handed; it uses
whatever Claude login the CLI already has. Every call is priced into llm_usage and counts
against ai.daily_budget_usd like the strategy reviews do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from solmeme.config import SessionReviewCfg, Settings
from solmeme.core.store import Store
from solmeme.reports.session import SessionReport, build

PROVIDERS = ("claude", "local", "none")

SYSTEM = """You review a live paper-trading session of a Solana meme-coin bot. No real money is at risk.
You get a report the bot wrote: wallet, problems it detected, trades closed lately, open positions,
per-strategy totals and data sources. Your previous note, if any, comes first.
Write at most 150 words of plain sentences:
1. What changed since your previous note, citing numbers from the report.
2. The most important problem or pattern, and why it matters.
3. One concrete thing to check or change.
Use only numbers that appear in the report. Do not invent prices, causes, or trades.
If nothing matters, say so in one sentence."""


@dataclass
class ReviewResult:
    report: SessionReport
    text: str               # report markdown plus the AI note, if one was written
    path: Path
    ai_status: str          # what happened with the AI this time, for the console


def _day_start() -> float:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def should_ask(state: dict, report: SessionReport) -> tuple[bool, str]:
    if not report.live and "ended" not in {f.id for f in report.flags}:
        return False, "bot not running"
    seen = set(state.get("flags") or [])
    new_flags = [f.id for f in report.flags if f.id not in seen]
    new_trades = report.closed - int(state.get("closed") or 0)
    if new_trades > 0:
        return True, f"{new_trades} new closed trade{'s' if new_trades != 1 else ''}"
    if new_flags:
        return True, "new problems: " + ", ".join(new_flags)
    return False, "nothing new since the last AI note"


def ask_claude(cfg: SessionReviewCfg, prompt: str) -> tuple[str, dict]:
    exe = shutil.which("claude")
    if exe is None:
        raise RuntimeError("the `claude` CLI is not on PATH (install Claude Code, or use --ai local)")
    # No tools, MCP servers or skills: their definitions can add ~20k tokens to every call.
    # (--bare would strip more but skips the keychain, which breaks a subscription login.)
    cmd = [exe, "-p", "--model", cfg.claude_model, "--output-format", "json", "--tools", "",
           "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
           "--system-prompt", SYSTEM]
    r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=cfg.timeout_s,
                       cwd=tempfile.gettempdir())
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError((r.stderr or r.stdout or f"claude exited {r.returncode}").strip()[:300]) from None
    if r.returncode != 0 or out.get("is_error"):
        raise RuntimeError(str(out.get("result") or r.stderr or f"claude exited {r.returncode}")[:300])
    u = out.get("usage") or {}
    return str(out.get("result") or "").strip(), {
        "model": f"claude-cli:{cfg.claude_model}", "cost_usd": float(out.get("total_cost_usd") or 0.0),
        "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0)}


def ask_local(cfg: SessionReviewCfg, prompt: str) -> tuple[str, dict]:
    r = httpx.post(f"{cfg.local_url.rstrip('/')}/chat/completions", timeout=cfg.timeout_s, json={
        "model": cfg.local_model, "stream": False,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
    r.raise_for_status()
    j = r.json()
    u = j.get("usage") or {}
    return str(j["choices"][0]["message"]["content"]).strip(), {
        "model": f"local:{cfg.local_model}", "cost_usd": 0.0,
        "input_tokens": u.get("prompt_tokens", 0), "output_tokens": u.get("completion_tokens", 0),
        "cache_read": 0, "cache_write": 0}


def write_schedule(settings: Settings, next_at: float, every_min: float) -> None:
    """When the `--every` loop reports next, so the website can say so instead of looking stuck."""
    folder = settings.reports_dir / "session"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "next.json").write_text(json.dumps({"next_at": next_at, "every_min": every_min}), encoding="utf-8")


def review_once(settings: Settings, store: Store, minutes: float, provider: str, *,
                force_ai: bool = False, now: float | None = None) -> ReviewResult:
    now = now or time.time()
    report = build(settings, store, minutes, now)
    folder = settings.reports_dir / "session" / (report.run_id or "none")
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    text = report.markdown
    if provider == "none":
        status = "AI off"
    else:
        ask, why = should_ask(state, report)
        if force_ai and (report.live or report.run_id):
            ask, why = True, "forced"
        if not ask:
            status = f"no AI call: {why}"
        elif store.llm_spend_since(_day_start()) >= settings.ai.daily_budget_usd:
            status = f"no AI call: daily budget ${settings.ai.daily_budget_usd:.2f} reached"
        else:
            prev = state.get("note")
            prompt = (f"Your previous note ({state.get('note_at', '?')} UTC):\n{prev}\n\n" if prev else "") + report.markdown
            cfg = settings.ai.session_review
            try:
                note, usage = (ask_claude if provider == "claude" else ask_local)(cfg, prompt)
            except Exception as exc:  # noqa: BLE001 — the report still gets written
                status = f"AI call failed ({provider}): {exc}"
            else:
                store.log_llm_usage(dict(ts=now, purpose="session_review", **usage))
                stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%H:%M")
                text += f"\n## AI note ({usage['model']}, ${usage['cost_usd']:.3f}) · {why}\n\n{note}\n"
                state.update(closed=report.closed, flags=[f.id for f in report.flags], note=note, note_at=stamp)
                state_path.write_text(json.dumps(state), encoding="utf-8")
                status = f"AI note written ({why}, ${usage['cost_usd']:.3f})"

    path = folder / (datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d-%H%M") + ".md")
    path.write_text(text, encoding="utf-8")
    (folder / "latest.md").write_text(text, encoding="utf-8")
    return ReviewResult(report, text, path, status)

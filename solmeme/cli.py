"""solmeme — command line.

  check                     test every data source + credentials
  boards                    print Trenches, Trending, CopyTrade, pump.fun, and a card per buy
  people [QUERY]            find wallets to paper-copy, or look one up by name or address
  trace ADDRESS            who funded a wallet, who it paid, and what those wallets are
  setup                     ask for API keys once and save them in .env
  record                    ingest + event log only
  run [--ui normal|tui|web] live paper trading. normal is the rich log; tui is Bubble Tea; web is localhost
  snipe                     paper launch sniper (live, or --replay over the event log)
  snipe-report              what the sniper has learned: working bins, failing bins, recent decisions
  backtest                  replay recorded data through every active strategy version
  scan                      strategy-independent pattern discovery over the token universe
  optimize STRATEGY         optuna walk-forward param search → proposal (gated like AI proposals)
  review [STRATEGY]         AI review now (--dry-run prints the evidence pack, no API call)
  ai-cycle                  shadow gate + due reviews (what `run` schedules every 10 min)
  evolve                    one swarm generation: kill losers, learn, breed, screen (what `run` schedules hourly)
  swarm                     the swarm leaderboard and what the last generation did
  council                   LLM judges and their ideas: proposed rules, votes, how carriers did live
  ban list|add|remove       pump-and-dump wallets the bot never buys alongside
  strategies / show         list versions / print a spec
  promote / rollback / kill / demote   manual champion control (demote: champion → trial, off the shared wallet)
  report                    markdown report → <data>/reports/
  session [--every 15]      report on the running bot; --ai claude|local adds a note when something changed
  synth                     write a synthetic market into the event log (demo / tests)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

app = typer.Typer(add_completion=False, help=__doc__, no_args_is_help=True)
console = Console()


@app.callback()
def main(data_dir: Optional[Path] = typer.Option(None, "--data-dir", help="Use a separate data dir (e.g. data-demo)"),
         verbose: bool = typer.Option(False, "-v", "--verbose")):
    if data_dir:
        os.environ["SOLMEME_DATA_DIR"] = str(data_dir.resolve())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _live_lock(s):
    """Hold data/live.lock for this process, or exit if another live session already holds it.

    Two sessions at once each record every event into the log, so replays would see them twice.
    """
    import fcntl

    fh = open(s.data_dir / "live.lock", "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.seek(0)
        pid = fh.read().strip() or "?"
        console.print(f"[red]another session is already running (pid {pid}). Stop it first (Ctrl+C in its "
                      f"terminal, or kill {pid}).[/red]")
        raise typer.Exit(1)
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def _ctx():
    from solmeme.ai.promoter import ensure_seeded
    from solmeme.config import load_settings
    from solmeme.core.store import Store

    s = load_settings()
    store = Store(s.db_path)
    seeded = ensure_seeded(store, s)
    for k in seeded:
        if k.endswith(" (trial)"):
            console.print(f"[green]seeded trial[/green] {k[:-8]} [dim](paper-only, own wallet)[/dim]")
        else:
            console.print(f"[green]seeded champion[/green] {k}")
    return s, store


def _parse_time(x: Optional[str]) -> Optional[float]:
    if not x:
        return None
    try:
        return float(x)
    except ValueError:
        return datetime.fromisoformat(x).replace(tzinfo=timezone.utc).timestamp()


# ── data ──────────────────────────────────────────────────────────────────────
@app.command()
def check():
    """Test every data source and credential (no trading)."""
    import httpx
    import websockets

    from solmeme.config import load_settings, secret
    from solmeme.sources import gmgn
    from solmeme.sources.base import Context
    from solmeme.sources.dexscreener import DexScreener
    from solmeme.sources.misc import SolPriceSource

    s = load_settings()
    t = Table(title="Source check")
    for c in ("source", "result", "detail"):
        t.add_column(c)

    async def go():
        async with httpx.AsyncClient(follow_redirects=True) as http:
            ctx = Context(s, http, lambda ev: None)
            # PumpPortal
            url = s.sources.pumpportal.url
            key = secret("PUMPPORTAL_API_KEY")
            if key:
                url += ("&" if "?" in url else "?") + f"api-key={key}"
            try:
                async with websockets.connect(url, open_timeout=10) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    n, deadline, note = 0, time.time() + 15, ""
                    while time.time() < deadline:
                        try:
                            msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - time.time()))
                        except asyncio.TimeoutError:
                            break
                        if msg.get("txType") == "create":
                            n += 1
                        elif "errors" in msg or "error" in msg:
                            note = str(msg)[:120]
                    t.add_row("pumpportal", "[green]ok[/green]" if n else "[yellow]no launches[/yellow]",
                              f"{n} new tokens in 15s" + (f" · {note}" if note else "")
                              + ("" if key else " · no PUMPPORTAL_API_KEY (trade streams may need one)"))
            except Exception as exc:  # noqa: BLE001
                t.add_row("pumpportal", "[red]fail[/red]", f"{exc!r}"[:140])
            # GMGN
            if not gmgn.available():
                t.add_row("gmgn", "[yellow]skipped[/yellow]", "GMGN_API_KEY not set (free at gmgn.ai/ai)")
            else:
                try:
                    g = gmgn.Gmgn(ctx)
                    data = await g._req("POST", gmgn.TRENCHES_PATH, {"chain": "sol"},
                                        {"version": "v2", "new_creation": {"limit": 5, "launchpad_platform": gmgn.SOL_PLATFORMS,
                                                                           "quote_address_type": gmgn.SOL_QUOTE_ADDRESS_TYPES,
                                                                           "launchpad_platform_v2": True}})
                    n = len((data or {}).get("new_creation") or []) if isinstance(data, dict) else 0
                    t.add_row("gmgn", "[green]ok[/green]", f"trenches returned {n} tokens")
                except Exception as exc:  # noqa: BLE001
                    t.add_row("gmgn", "[red]fail[/red]", f"{exc!r}"[:140])
            # DexScreener
            try:
                rows = await DexScreener(ctx)._get("/token-boosts/latest/v1")
                t.add_row("dexscreener", "[green]ok[/green]", f"{len(rows)} latest boosts")
            except Exception as exc:  # noqa: BLE001
                t.add_row("dexscreener", "[red]fail[/red]", f"{exc!r}"[:140])
            # SOL price
            try:
                px = await SolPriceSource(ctx).fetch()
                t.add_row("sol_price", "[green]ok[/green]", f"SOL = ${px:,.2f}")
            except Exception as exc:  # noqa: BLE001
                t.add_row("sol_price", "[red]fail[/red]", f"{exc!r}"[:140])

    asyncio.run(go())
    has_claude = bool(secret("ANTHROPIC_API_KEY")) or Path.home().joinpath(".config/anthropic").exists()
    t.add_row("claude", "[green]ok[/green]" if has_claude else "[yellow]missing[/yellow]",
              "ANTHROPIC_API_KEY or `ant auth login` profile" if has_claude else "set ANTHROPIC_API_KEY for AI reviews")
    from solmeme.sim.live import PLATFORM_SOURCES, load_platform

    for name in PLATFORM_SOURCES:
        cls = load_platform(name)
        on = getattr(s.sources, name).enabled
        logged = (s.data_dir / "browser" / name / "profile").exists()
        if not isinstance(cls, type):
            t.add_row(name, "[dim]n/a[/dim]", "adapter not installed")
        else:
            t.add_row(name, "[green]enabled[/green]" if on else "[dim]disabled[/dim]",
                      ("profile saved" if logged else f"run `solmeme login {name}` first")
                      + f" · enable with sources.{name}.enabled: true")
    console.print(t)


_SETUP_KEYS = (
    ("GMGN_API_KEY", "GMGN, free at https://gmgn.ai/ai — trenches, trending, copy-trade, wallet scores"),
    ("JUPITER_API_KEY", "optional Jupiter price key"),
    ("PUMPPORTAL_API_KEY", "optional PumpPortal key for per-token trades"),
    ("HELIUS_API_KEY", "optional Helius RPC"),
    ("ANTHROPIC_API_KEY", "optional Claude key; leave empty and ./run stays AI-off"),
    ("DEEPSEEK_KEY", "optional"),
)


@app.command()
def setup():
    """Ask for each API key once and store it in .env. Press Enter to keep a key that is already saved."""
    from solmeme.config import ROOT, secret, upsert_env

    console.print("[bold]Save API keys in .env[/bold]")
    console.print("Input is hidden. Enter skips a key and leaves the saved value in place.\n"
                  "[yellow]Paste API keys only. Never a wallet private key or seed phrase.[/yellow]")
    updates = {}
    for name, hint in _SETUP_KEYS:
        label = f"{name} — {hint}"
        if secret(name):
            label += " (saved)"
        value = typer.prompt(label, default="", hide_input=True, show_default=False, prompt_suffix=": ")
        if value.strip():
            updates[name] = value.strip()
    if not updates:
        console.print("No new keys. .env was left as it is.")
    else:
        try:
            written = upsert_env(ROOT / ".env", updates)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)
        for name in written:
            os.environ[name] = updates[name]
        console.print(f"Saved {len(written)} key(s) in .env: {', '.join(written)}")
    console.print("Axiom, fomo, and j7tracker logins are a browser session in data/browser/, not a line in .env.\n"
                  "Run [bold]./run login fomo[/bold] (or axiom, j7tracker) once if you want those.")


@app.command()
def boards(cards: int = typer.Option(6, help="How many copied buys and pump.fun names to analyze")):
    """Show Trenches, Trending, CopyTrade, and pump.fun, then a stats + narrative card for each."""
    import httpx

    from solmeme.config import load_settings
    from solmeme.lab.boards import PUMP_COINS, snapshot
    from solmeme.sources.base import Context
    from solmeme.sources.gmgn import Gmgn, available

    s = load_settings()
    if not available():
        console.print("[yellow]GMGN_API_KEY is not set.[/yellow] Trenches, Trending, and CopyTrade need it "
                      "(free at https://gmgn.ai/ai). Pump.fun still loads.")

    async def go():
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            gmgn = Gmgn(Context(settings=s, http=http, emit=lambda *a, **k: None)) if available() else None

            async def fetch_html(url: str):
                r = await http.get(url, timeout=6, headers={"User-Agent": "SolMemeTrader/0.1", "Accept": "text/html"})
                if "html" not in (r.headers.get("content-type") or ""):
                    return ""
                return r.text[:20_000]

            async def pump_get(params: dict):
                r = await http.get(PUMP_COINS, params=params, headers={"User-Agent": "SolMemeTrader/0.1", "Accept": "application/json"})
                r.raise_for_status()
                data = r.json()
                return data if isinstance(data, list) else []

            return await snapshot(None if gmgn is None else gmgn._req, now=time.time(), max_cards=cards,
                                  fetch_html=fetch_html, pump_get=pump_get)

    try:
        snap = asyncio.run(go())
    except Exception as e:
        console.print(f"[red]boards failed[/red] {e}")
        raise typer.Exit(1)
    _print_boards(snap)
    out = s.data_dir / "boards"
    out.mkdir(parents=True, exist_ok=True)
    path = out / (datetime.now(timezone.utc).strftime("%Y%m%d-%H%M") + ".md")
    path.write_text(_boards_markdown(snap), encoding="utf-8")
    console.print(f"\nsaved {path}")


def _print_boards(snap: dict) -> None:
    labels = {"new_creation": "Trenches · New", "near_completion": "Trenches · Almost", "completed": "Trenches · Migrated"}
    for key, title in labels.items():
        t = Table(title=title, show_lines=False)
        t.add_column("symbol")
        t.add_column("mcap")
        t.add_column("holders", justify="right")
        t.add_column("mint")
        for row in snap["trenches"].get(key) or []:
            t.add_row(str(row.get("symbol") or ""), _usd(row.get("mcap_usd")), _num(row.get("holders")), row["mint"])
        console.print(t)
    t = Table(title="Trending · 1h by volume")
    t.add_column("symbol")
    t.add_column("mcap")
    t.add_column("volume")
    t.add_column("holders", justify="right")
    t.add_column("mint")
    for row in snap["trending"]:
        t.add_row(row["symbol"], _usd(row.get("mcap_usd")), _usd(row.get("volume_usd")),
                  _num(row.get("holders")), row["mint"])
    console.print(t)
    pump_labels = {"new": "pump.fun · New", "almost": "pump.fun · Almost", "graduated": "pump.fun · Graduated"}
    for key, title in pump_labels.items():
        t = Table(title=title, show_lines=False)
        t.add_column("symbol")
        t.add_column("mcap")
        t.add_column("curve", justify="right")
        t.add_column("mint")
        for row in (snap.get("pump") or {}).get(key) or []:
            bond = row.get("bonding_pct")
            t.add_row(str(row.get("symbol") or ""), _usd(row.get("mcap_usd")),
                      "" if bond is None else f"{bond:.0f}%", row["mint"])
        console.print(t)
    console.print(f"\n[bold]CopyTrade + pump.fun[/bold]  {len(snap['buys'])} wallet buys, {len(snap['cards'])} analyzed")
    for card in snap["cards"]:
        color = {"skip": "red", "research": "yellow", "watch": "green"}.get(card["verdict"], "white")
        console.print(Panel(card["text"], border_style=color))


def _boards_markdown(snap: dict) -> str:
    lines = ["# Boards", ""]
    for key, title in (("new_creation", "New"), ("near_completion", "Almost"), ("completed", "Migrated")):
        lines.append(f"## Trenches · {title}")
        for row in snap["trenches"].get(key) or []:
            lines.append(f"- {row.get('symbol') or '?'} {_usd(row.get('mcap_usd'))} `{row['mint']}`")
        lines.append("")
    lines.append("## Trending")
    for row in snap["trending"]:
        lines.append(f"- {row['symbol']} mcap {_usd(row.get('mcap_usd'))} vol {_usd(row.get('volume_usd'))} `{row['mint']}`")
    lines.append("")
    for key, title in (("new", "New"), ("almost", "Almost"), ("graduated", "Graduated")):
        lines.append(f"## pump.fun · {title}")
        for row in (snap.get("pump") or {}).get(key) or []:
            bond = row.get("bonding_pct")
            curve = "" if bond is None else f" curve {bond:.0f}%"
            lines.append(f"- {row.get('symbol') or '?'} {_usd(row.get('mcap_usd'))}{curve} `{row['mint']}`")
        lines.append("")
    lines.append("## CopyTrade")
    for card in snap["cards"]:
        lines.append("```")
        lines.append(card["text"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


@app.command()
def people(query: Optional[str] = typer.Argument(None, help="Wallet address or name. Omit to rank wallets buying right now."),
           limit: int = typer.Option(8, help="How many wallets to score")):
    """Rank KOL and smart-money wallets, or look one up. Paper copy only: no transactions are sent."""
    import httpx

    from solmeme.config import load_settings
    from solmeme.lab.people import discover, lookup
    from solmeme.sources.base import Context
    from solmeme.sources.gmgn import Gmgn, available

    if not available():
        console.print("[yellow]GMGN_API_KEY is not set.[/yellow] Wallet search uses the GMGN API. "
                      "A free key is at https://gmgn.ai/ai.")
        raise typer.Exit(1)
    s = load_settings()
    followed = set(s.sources.tracked_wallets)

    async def go():
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            gmgn = Gmgn(Context(settings=s, http=http, emit=lambda *a, **k: None))
            if query:
                return await lookup(gmgn._req, query, followed=followed)
            return await discover(gmgn._req, limit=limit, followed=followed)

    try:
        cards = asyncio.run(go())
    except Exception as e:
        console.print(f"[red]people failed[/red] {e}")
        raise typer.Exit(1)
    title = f"Wallets matching {query}" if query else "Wallets buying on the KOL and smart-money feeds"
    console.print(f"[bold]{title}[/bold]  {len(cards)} scored  ·  paper only, nothing is sent")
    if not cards:
        console.print("No wallets came back.")
        return
    for card in cards:
        color = {"copy": "green", "watch": "yellow", "skip": "red"}.get(card["verdict"], "white")
        console.print(Panel(card["text"], border_style=color))


@app.command()
def trace(address: str = typer.Argument(..., help="Solana wallet address"),
          hops: int = typer.Option(2, min=1, max=2, help="2 follows the largest counterparties one step. 1 stops at direct links."),
          limit: int = typer.Option(100, min=1, max=300, help="How many of the wallet's newest transactions to read")):
    """Follow SOL and token transfers, then label the wallets on the other side. No transactions are sent."""
    import httpx

    from solmeme.config import load_settings
    from solmeme.lab.forensics import identify_wallet, investigate, load_history
    from solmeme.lab.people import is_address
    from solmeme.sources.base import Context
    from solmeme.sources.gmgn import Gmgn, available

    address = address.strip()
    if not is_address(address):
        console.print("[red]trace needs a wallet address.[/red] A name search is `solmeme people <name>`.")
        raise typer.Exit(1)
    s = load_settings()
    gmgn_on = available()

    async def go():
        async with httpx.AsyncClient(timeout=40, follow_redirects=True) as http:
            gmgn = Gmgn(Context(settings=s, http=http, emit=lambda *a, **k: None)) if gmgn_on else None
            req = gmgn._req if gmgn else None

            async def fetch(addr, tx_limit):
                return await load_history(http, addr, tx_limit)

            async def identify(addr):
                return await identify_wallet(req, addr)

            return await investigate(fetch, identify, address, hops=hops, tx_limit=limit)

    try:
        report = asyncio.run(go())
    except Exception as e:
        console.print(f"[red]trace failed[/red] {e}")
        raise typer.Exit(1)
    console.print(Panel(report["text"], title="wallet trace", border_style="cyan"))
    console.print("[dim]public chain history only · nothing is sent[/dim]")
    if hops < 2 and report["summary"]["counterparties"]:
        console.print("[dim]Follow the money one step further with --hops 2[/dim]")


def _usd(x) -> str:
    return "" if x is None else f"${x:,.0f}"


def _num(x) -> str:
    return "" if x is None else f"{x:.0f}"


def enable_browser_source(text: str, name: str) -> tuple[str, bool]:
    """Turn sources.<name>.enabled on. Leaves the rest of the settings file alone.

    The second value is true when that source is enabled afterward, including
    when it was already on. Indentation in front of the key is kept.
    """
    import re
    updated, n = re.subn(
        rf"^([ \t]*{re.escape(name)}:\s*\{{enabled:\s*)false\b",
        r"\1true",
        text,
        count=1,
        flags=re.M,
    )
    if n:
        return updated, True
    already = re.search(rf"^[ \t]*{re.escape(name)}:\s*\{{enabled:\s*true\b", text, re.M)
    return text, bool(already)


@app.command()
def login(site: str = typer.Argument(..., help="axiom | fomo | j7tracker")):
    """Open a visible browser for a platform so you can log in once (session is saved locally)."""
    from solmeme.config import load_settings
    from solmeme.sim.live import PLATFORM_SOURCES, load_platform
    from solmeme.sources.browser import interactive_login

    if site not in PLATFORM_SOURCES:
        console.print(f"[red]unknown site {site}[/red] (choose: {', '.join(PLATFORM_SOURCES)})")
        raise typer.Exit(1)
    cls = load_platform(site)
    url = getattr(cls, "login_url", "") or getattr(cls, "start_url", "")
    if not url:
        console.print(f"[red]{site} adapter has no login page[/red]")
        raise typer.Exit(1)
    console.print(f"[bold]A browser window will open for {site}.[/bold]")
    console.print("Log in, then close that window. This command waits until the window is closed.\n"
                  "[yellow]Use a throwaway account. Never connect a funded wallet or paste a private key.[/yellow]")
    try:
        asyncio.run(interactive_login(load_settings(), site, url))
    except KeyboardInterrupt:
        console.print("[yellow]Stopped. Close the browser window instead of Ctrl-C so the session can finish saving.[/yellow]")
        raise typer.Exit(1)
    from solmeme.config import ROOT
    path = ROOT / "config" / "settings.yaml"
    updated, on = enable_browser_source(path.read_text(encoding="utf-8"), site)
    path.write_text(updated, encoding="utf-8")
    if on:
        console.print(f"session saved, and sources.{site}.enabled is on. Next `./run` will use it.")
    else:
        console.print(f"[yellow]session saved, but sources.{site}.enabled was not turned on.[/yellow] "
                      f"Set `{site}: {{enabled: true, ...}}` in config/settings.yaml.")


@app.command()
def record(minutes: Optional[float] = typer.Option(None, help="Stop after N minutes (default: run forever)")):
    """Ingest all sources into the event log (no trading)."""
    from solmeme.config import load_settings
    from solmeme.sim.live import LiveSession

    s = load_settings()
    lock = _live_lock(s)  # noqa: F841 - held until the process exits
    console.print(f"[bold]recording[/bold] → {s.events_dir}  (Ctrl-C to stop)")
    try:
        asyncio.run(LiveSession(s, None, None, console).run(minutes))
    except KeyboardInterrupt:
        pass


@app.command()
def run(minutes: Optional[float] = typer.Option(None, help="Stop after N minutes"),
        ai: bool = typer.Option(True, "--ai/--no-ai", help="Schedule AI cycles (shadow gate + reviews)"),
        warmup_min: float = typer.Option(45, help="Rebuild state by replaying the last N minutes of the log"),
        ui: str = typer.Option("normal", "--ui", help="normal (rich log), tui (Bubble Tea), or web (localhost)"),
        fresh: bool = typer.Option(True, "--fresh/--resume",
                                   help="Fresh (default): a new wallet, the warm start loads market data only. "
                                        "Resume: replay recent trades to restore open positions"),
        usd: Optional[float] = typer.Option(None, "--usd", help="Starting wallet in dollars (overrides settings)"),
        new_wallet: bool = typer.Option(False, "--new-wallet", help="Start the champions' wallet over instead of continuing it"),
        goal: Optional[float] = typer.Option(None, "--goal", help="Wallet goal in dollars (overrides settings)")):
    """Live paper trading: record + run every champion and challenger on the same stream."""
    from solmeme.ai.promoter import active_specs
    from solmeme.sim.live import LiveSession

    ui = ui.lower()
    if ui not in ("normal", "tui", "web"):
        console.print("[red]--ui must be normal, tui, or web[/red]")
        raise typer.Exit(1)
    s, store = _ctx()
    lock = _live_lock(s)  # noqa: F841 - held until the process exits
    if usd is not None:
        if usd <= 0:
            console.print("[red]--usd must be more than 0[/red]")
            raise typer.Exit(1)
        s.portfolio.starting_usd = usd
    if goal is not None:
        s.portfolio.goal_usd = goal
    specs = active_specs(store)
    out = console
    log_file = None
    if ui == "tui":
        from solmeme.config import ROOT
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        log_file = open(logs / "tui-session.log", "a", encoding="utf-8")
        logging.basicConfig(level=logging.WARNING, force=True, stream=log_file,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        out = Console(file=log_file, force_terminal=False, highlight=False)
    summary = (f"[bold]paper trading[/bold] {len(specs)} strategy versions: "
               + ", ".join(f"{sp.key} ({lbl})" for sp, lbl in specs))
    out.print(summary)
    out.print("[dim]also scanning boards and wallets, then again every 10 min[/dim]")
    session = LiveSession(s, store, specs, out, warmup_min=warmup_min, ai=ai, ui=ui, fresh=fresh,
                          new_wallet=new_wallet or usd is not None)
    try:
        if ui != "normal":
            from solmeme.ui.launch import UiError, attach
            try:
                attach(session, ui)
            except UiError as exc:
                console.print(f"[red]{exc}[/red]")
                session._save()
                raise typer.Exit(1)
        asyncio.run(session.run(minutes))
    except KeyboardInterrupt:
        pass
    finally:
        if log_file is not None:
            log_file.close()


@app.command()
def snipe(minutes: Optional[float] = typer.Option(None, help="Stop after N minutes"),
          replay: bool = typer.Option(False, "--replay", help="Learn from the event log instead of going live"),
          days: float = typer.Option(1, help="Replay the last N days")):
    """Paper launch sniper. Scores a token ~12s after creation. Never sends a transaction."""
    from solmeme.lab.metrics import engine_metrics
    from solmeme.sim.live import LiveSession
    from solmeme.sim.replay import load_events
    from solmeme.sniper.model import STRATEGY_ID
    from solmeme.sniper.session import run_sniper
    from solmeme.strategy.spec import load_dir

    s, store = _ctx()
    spec = next((sp for sp in load_dir(s.strategies_dir) if sp.id == STRATEGY_ID), None)
    if spec is None:
        console.print("[red]launch_sniper strategy not found[/red]")
        raise typer.Exit(1)
    if replay:
        events = load_events(s, time.time() - days * 86400, None)
        if not events:
            console.print("[red]no recorded events in that window[/red] — run `solmeme record` or `solmeme synth` first")
            raise typer.Exit(1)
        console.print("[bold]paper sniper replay[/bold] — paper only, no transactions")
        eng, sniper = run_sniper(
            s, events, store,
            decision_s=s.sniper.decision_s, horizon_s=s.sniper.horizon_s, explore=s.sniper.explore,
        )
        rep = sniper.report()
        console.print(f"{rep['decisions']} decisions · {rep['buys']} buys · {rep['skips']} skips · "
                      f"{rep['open_labels']} open labels")
        _print_sniper(rep["summary"], rep["working"], rep["failing"])
        _print_metrics(engine_metrics(eng).values(), {r.key: r.label for r in eng.runners})
        return
    console.print("[bold]paper sniper[/bold] — paper only, no transactions")
    try:
        asyncio.run(LiveSession(s, store, [(spec, "champion")], console, warmup_min=45, ai=False).run(minutes))
    except KeyboardInterrupt:
        pass


@app.command("snipe-report")
def snipe_report():
    """Show which feature bins the sniper has learned, and the latest decisions."""
    from solmeme.sniper.learn import SniperLearner

    _, store = _ctx()
    blob = store.load_sniper()
    if not blob:
        console.print("no sniper learning saved yet — run solmeme snipe")
        return
    learner = SniperLearner.from_dict(blob)
    _print_sniper(learner.summary(), learner.working(), learner.failing())
    _print_sniper_decisions(store.sniper_decisions(15))


@app.command()
def synth(tokens: int = typer.Option(400), hours: float = typer.Option(3.0), seed: int = typer.Option(1),
          end_now: bool = typer.Option(True, help="Place the synthetic window so it ends now")):
    """Write a synthetic pump.fun market into the event log (use with --data-dir data-demo)."""
    from solmeme.config import load_settings
    from solmeme.core.eventlog import EventLogWriter
    from solmeme.lab.synth import generate

    s = load_settings()
    if s.data_dir.name == "data":
        console.print("[yellow]tip: use `solmeme --data-dir data-demo synth` to keep synthetic data separate[/yellow]")
    dur = hours * 3600
    start = time.time() - dur - 2400 if end_now else 1_780_000_000.0
    evs = generate(n_tokens=tokens, duration_s=dur, start_ts=start, seed=seed)
    w = EventLogWriter(s.events_dir)
    for ev in evs:
        w.write(ev)
    w.close()
    console.print(f"wrote {len(evs)} synthetic events → {s.events_dir}")


# ── lab ───────────────────────────────────────────────────────────────────────
@app.command()
def backtest(days: float = typer.Option(3, help="Replay the last N days"),
             start: Optional[str] = typer.Option(None, "--from", help="ISO time or epoch"),
             end: Optional[str] = typer.Option(None, "--to"),
             strategy: list[str] = typer.Option([], "-s", "--strategy", help="Limit to these strategy ids"),
             all_yaml: bool = typer.Option(False, "--yaml", help="Use strategies/*.yaml instead of DB versions")):
    """Replay recorded events through active strategy versions and write a leaderboard."""
    from solmeme.ai.promoter import active_specs
    from solmeme.lab.metrics import engine_metrics
    from solmeme.lab.mining import exit_summary, near_miss_summary
    from solmeme.reports.markdown import backtest_report
    from solmeme.sim.replay import load_events, run_replay
    from solmeme.strategy.spec import load_dir

    s, store = _ctx()
    a = _parse_time(start) or time.time() - days * 86400
    b = _parse_time(end)
    specs = [(sp, "yaml") for sp in load_dir(s.strategies_dir)] if all_yaml else active_specs(store)
    if strategy:
        specs = [(sp, l) for sp, l in specs if sp.id in strategy]
    t0 = time.time()
    events = load_events(s, a, b)
    if not events:
        console.print("[red]no recorded events in that window[/red] — run `solmeme record` or `solmeme synth` first")
        raise typer.Exit(1)
    eng = run_replay(s, specs, events)
    ms = engine_metrics(eng)
    roles = {sp.key: l for sp, l in specs}
    extra = []
    for sp, _ in specs:
        tr = [t for t in eng.journal.trades if t["strategy_id"] == sp.id and t["version"] == sp.version]
        nm = [n for n in eng.journal.near_misses if n["strategy_id"] == sp.id and n["version"] == sp.version]
        extra.append(f"### {sp.key}\n\nExits: `{json.dumps(exit_summary(tr))}`\n\nNear-misses: `{json.dumps(near_miss_summary(nm)[:6])}`\n")
    out = backtest_report(s, list(ms.values()), roles, (events[0].ts, events[-1].ts), "\n".join(extra))
    _print_metrics(ms.values(), roles)
    console.print(f"{len(events):,} events in {time.time() - t0:.1f}s → {out}")


def _fmt_sniper(k: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and ("rate" in k or k in ("threshold", "bias", "recent_buy_pnl")):
        return f"{v:.3f}"
    if isinstance(v, float):
        return f"{v:.4g}"
    if isinstance(v, dict):
        return ""
    return str(v)


def _print_sniper(summary: dict, working: list, failing: list) -> None:
    from solmeme.sniper.model import PRIOR_WEIGHTS

    t = Table(title="sniper")
    t.add_column("metric")
    t.add_column("value")
    for k, v in summary.items():
        if k == "weights" or isinstance(v, dict):
            continue
        t.add_row(k, _fmt_sniper(k, v))
    console.print(t)
    weights = summary.get("weights") or {}
    moved = sorted(weights.items(), key=lambda kv: -abs(kv[1] - PRIOR_WEIGHTS.get(kv[0], 0.0)))
    wt = Table(title="learned weights")
    wt.add_column("feature")
    wt.add_column("weight")
    wt.add_column("prior")
    for name, w in moved[:8]:
        wt.add_row(name, f"{w:+.3f}", f"{PRIOR_WEIGHTS.get(name, 0.0):+.3f}")
    console.print(wt)
    _print_bins("working bins", working[:12])
    _print_bins("failing bins", failing[:12])


def _print_bins(title: str, rows: list) -> None:
    t = Table(title=title)
    for c in ("feature", "bin", "n", "wins", "win rate", "lift"):
        t.add_column(c)
    for d in rows:
        wr, lift = d.get("win_rate"), d.get("lift")
        t.add_row(
            str(d.get("feature", "")), str(d.get("bin", "")), str(d.get("n", "")), str(d.get("wins", "")),
            "" if wr is None else f"{wr:.0%}",
            f"{lift:+.2f}" if isinstance(lift, float) else str(lift if lift is not None else ""),
        )
    console.print(t)


def _print_sniper_decisions(rows: list) -> None:
    t = Table(title="recent decisions")
    for c in ("time", "symbol", "action", "score", "outcome", "max x", "pnl", "note"):
        t.add_column(c)
    for d in rows:
        ts = d.get("ts")
        when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else ""
        px = d.get("max_x")
        pnl = d.get("pnl_sol")
        t.add_row(
            when, str(d.get("symbol") or ""), str(d.get("action") or ""),
            "" if d.get("score") is None else f"{d['score']:.2f}",
            str(d.get("outcome") or ""),
            "" if px is None else f"{px:.2f}",
            "" if pnl is None else f"{pnl:+.4f}",
            (d.get("note") or "")[:48],
        )
    console.print(t)


def _print_metrics(ms, roles):
    t = Table()
    for c in ("strategy@version", "role", "trades", "win%", "PnL SOL", "exp %", "median %", "max DD", "ex-top3", "rug%"):
        t.add_column(c)
    for m in sorted(ms, key=lambda m: -m.total_pnl_sol):
        t.add_row(m.key, roles.get(m.key, ""), str(m.n_trades), f"{m.win_rate * 100:.0f}", f"{m.total_pnl_sol:+.3f}",
                  f"{m.expectancy_pct:+.1f}", f"{m.median_pct:+.1f}", f"{m.max_dd_sol:.3f}",
                  f"{m.pnl_ex_top3_sol:+.3f}", f"{m.rug_hit_rate * 100:.0f}")
    console.print(t)


@app.command()
def scan(days: float = typer.Option(3), checkpoint: Optional[int] = typer.Option(None, help="Only this age checkpoint (s)")):
    """What predicts a runner? Feature lift across ALL tokens (not just traded ones)."""
    from solmeme.lab import scan as sc
    from solmeme.sim.replay import load_events

    s, _ = _ctx()
    events = load_events(s, time.time() - days * 86400, None)
    rows = sc.scan(events, set(s.excluded_mints))
    console.print(f"{len(rows):,} token-moments · base rate (2x before -50%): "
                  f"{sum(r['win'] for r in rows) / max(1, len(rows)):.1%}")
    for cp in ([checkpoint] if checkpoint else sorted({r['checkpoint_s'] for r in rows})):
        t = Table(title=f"age checkpoint {cp}s")
        for c in ("feature", "AUC", "coverage", "best rule", "win rate", "n"):
            t.add_column(c)
        for d in sc.lift_table(rows, cp, top=12):
            t.add_row(d["feature"], f"{d['auc']:.3f}", f"{d['coverage']:.2f}", d["best_rule"] or "",
                      f"{d['rule_win_rate']:.1%}" if d["rule_win_rate"] is not None else "", str(d["rule_n"] or ""))
        console.print(t)


@app.command()
def optimize(strategy: str, trials: int = typer.Option(48), folds: int = typer.Option(3),
             days: float = typer.Option(3), propose: bool = typer.Option(True, "--propose/--no-propose",
                                                                        help="Submit the best params through the gates")):
    """Walk-forward optuna search over a strategy's params (evidence window only)."""
    from solmeme.ai.promoter import Promoter, champion_spec
    from solmeme.ai.reviewer import load_windows
    from solmeme.lab.optimize import optimize as opt
    from solmeme.strategy.diff import Change

    s, store = _ctx()
    spec = champion_spec(store, strategy)
    if spec is None:
        console.print(f"[red]unknown strategy {strategy}[/red]")
        raise typer.Exit(1)
    w = load_windows(s, days)
    if not w.evidence:
        console.print("[red]no recorded events[/red]")
        raise typer.Exit(1)
    console.print(f"optimizing {spec.key} on {len(w.evidence):,} evidence events ({trials} trials × {folds} folds)…")
    best, hist = opt(s, spec, w.evidence, trials=trials, folds=folds)
    t = Table(title="top trials")
    t.add_column("score")
    t.add_column("params")
    for h in hist[:8]:
        t.add_row(f"{h['score']:+.2f}", json.dumps({k: round(v, 3) for k, v in h["params"].items()}))
    console.print(t)
    changes = [Change(op="set_param", param=k, value=best.params[k].value)
               for k in spec.params if best.params[k].value != spec.params[k].value]
    if not changes:
        console.print("current params are already the best found")
        return
    console.print("best: " + ", ".join(f"{c.param}={c.value:g}" for c in changes))
    if propose:
        p = Promoter(s, store)
        row = p.submit(spec, changes, hypothesis="optimizer: best walk-forward params", origin="optimizer",
                       max_changes=len(changes))
        if row["status"] == "pending":
            row = p.backtest_gate(row, spec, w.holdout)
        console.print(f"proposal → [bold]{row['status']}[/bold] {row.get('reason', '')}")


# ── AI ────────────────────────────────────────────────────────────────────────
@app.command()
def review(strategy: Optional[str] = typer.Argument(None), dry_run: bool = typer.Option(False, "--dry-run"),
           days: Optional[float] = typer.Option(None),
           force: bool = typer.Option(False, "--force", help="Call the AI even if triage finds nothing")):
    """Run an AI review now (all champions if no strategy given)."""
    from solmeme.ai.reviewer import load_windows, review_strategy
    from solmeme.lab import scan as sc

    s, store = _ctx()
    w = load_windows(s, days)
    ids = [strategy] if strategy else [r["strategy_id"] for r in store.active_versions() if r["status"] == "champion"]
    rows = sc.scan(w.evidence, set(s.excluded_mints)) if w.evidence else []
    for sid in ids:
        try:
            r = review_strategy(s, store, sid, w, dry_run=dry_run, universe_rows=rows, force_ai=force)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]{sid}: {exc}[/red]")
            continue
        if dry_run and "evidence_pack" in r:
            console.rule(f"{sid} evidence pack ({r['prompt_chars']:,} chars incl. system prompt)")
            console.print(r["evidence_pack"], markup=False, highlight=False)
            continue
        console.rule(sid)
        console.print_json(json.dumps(r, default=str))


@app.command("ai-cycle")
def ai_cycle_cmd(force: bool = typer.Option(False, help="Review every champion now")):
    """Shadow gate for running challengers + reviews for champions that are due."""
    from solmeme.ai.reviewer import ai_cycle

    s, store = _ctx()
    console.print(f"[{datetime.now().strftime('%H:%M:%S')}] AI cycle")
    ai_cycle(s, store, force=force, log_fn=console.print)


@app.command()
def evolve(generations: int = typer.Option(1, help="How many generations to run"),
           every: Optional[float] = typer.Option(None, help="Minutes between generations (default: back to back)")):
    """Strategy swarm: kill agents that bust or lose, learn from their trades, breed and screen new ones."""
    from solmeme.core.eventlog import read_events
    from solmeme.lab.evolve import evolve_once, recorded_folds, report_markdown
    from solmeme.sim.wallet import fetch_sol_usd, logged_sol_usd

    try:
        os.nice(10)                       # the live bot shares this machine; replays must not starve it
    except OSError:
        pass
    s, store = _ctx()
    for i in range(generations):
        if i and every:
            time.sleep(every * 60)
        px = logged_sol_usd(s) or fetch_sol_usd()
        start_sol = s.portfolio.starting_usd / px if s.portfolio.starting_usd and px else None
        console.print(f"[{datetime.now().strftime('%H:%M:%S')}] swarm generation {store.evo_generation() + 1}")
        folds = recorded_folds(s.events_dir, s.evolution.window_h, s.evolution.folds)
        gen = store.evo_generation() + 1
        status = s.data_dir / "session" / "evolve.json"
        status.parent.mkdir(parents=True, exist_ok=True)

        def phase(name: str, gen=gen, started=time.time()) -> None:
            status.write_text(json.dumps({"gen": gen, "phase": name, "started": started, "ts": time.time()}))

        rep = evolve_once(s, store, lambda a, b: read_events(s.events_dir, a, b), start_sol=start_sol,
                          folds=folds, phase_fn=phase, log_fn=console.print)
        md = report_markdown(rep)
        out = s.reports_dir / "evolution"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"gen-{rep.gen:04d}.md").write_text(md, encoding="utf-8")
        (out / "latest.md").write_text(md, encoding="utf-8")
        console.print(f"generation {rep.gen}: {len(rep.born)} born, {len(rep.killed)} killed, "
                      f"{rep.screened_out} screened out, {len(rep.promoted)} promoted in {rep.seconds:.0f}s "
                      f"→ {out / 'latest.md'}")


@app.command()
def swarm():
    """The swarm leaderboard from the last generation."""
    s, store = _ctx()
    rows = store.evo_scores()
    if not rows:
        console.print("no generation yet: run `solmeme evolve` (or `solmeme run`, which starts one every "
                      f"{s.evolution.every_min:.0f} min)")
        return
    t = Table(title=f"swarm generation {rows[0]['gen']}")
    for c in ("#", "strategy", "role", "fitness", "replay", "replay trades", "live trades", "live PnL SOL", "wallet"):
        t.add_column(c)
    for i, r in enumerate(rows, 1):
        t.add_row(str(i), r["key"], r["role"], f"{r['fitness']:+.1f}", f"{r['replay']:+.1f}", str(r["replay_n"]),
                  "" if r["live_n"] is None else str(r["live_n"]),
                  "" if r["live_pnl_sol"] is None else f"{r['live_pnl_sol']:+.3f}",
                  "" if r["equity"] is None else f"{r['equity']:.0%}")
    console.print(t)
    dead = [v for v in store.versions() if v["status"] == "dead"]
    console.print(f"[dim]{len(dead)} dead so far · report: {s.reports_dir / 'evolution' / 'latest.md'}[/dim]")


@app.command("reset-wallets")
def reset_wallets_cmd():
    """Start every wallet over at the starting amount (champions and swarm agents), keeping all history.
    Stop the bot first; it starts fresh on the next start."""
    from solmeme.sim.wallet import reset_wallets

    s, _ = _ctx()
    ts = reset_wallets(s)
    console.print(f"wallets reset at {datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')}: every wallet starts at "
                  f"${s.portfolio.starting_usd or 0:,.0f} on the next start; trades, family trees and stats are kept")


@app.command("council")
def council_cmd():
    """The council's judges and ideas (sittings run inside each swarm generation)."""
    from solmeme.ai.council import available, judge_ideas, reputations

    s, store = _ctx()
    judge_ideas(store, s, time.time())
    reps = reputations(store)
    t = Table(title="judges")
    for c in ("judge", "model", "ready", "reputation"):
        t.add_column(c)
    for m in s.council.members:
        t.add_row(m.name, m.model or "(no model set)", "yes" if available(m) else "no",
                  "" if m.name not in reps else f"{reps[m.name]:+.2f}")
    console.print(t)
    t = Table(title="ideas")
    for c in ("strategy", "rule", "by", "score", "status", "why"):
        t.add_column(c)
    for i in reversed(store.council_ideas()[-30:]):
        verb = "require" if i["kind"] == "require" else "avoid"
        t.add_row(i["strategy_id"], f"{verb} {i['feature']} {i['op']} {i['v']:g}", i["proposed_by"],
                  "" if i["score"] is None else f"{i['score']:.2f}", i["status"], (i["status_reason"] or "")[:60])
    console.print(t)


ban_app = typer.Typer(help="Pump-and-dump wallets: tokens they create or buy are never bought.")
app.add_typer(ban_app, name="ban")


@ban_app.command("list")
def ban_list():
    """Show the ban list."""
    from solmeme.lab.banlist import BanList

    s, _ = _ctx()
    bans = BanList(s.data_dir)
    t = Table(title=f"{len(bans.bans)} banned wallets")
    for c in ("wallet", "role", "source", "added", "reason"):
        t.add_column(c)
    for b in sorted(bans.bans.values(), key=lambda b: -b.ts):
        t.add_row(b.address, b.role, b.source, datetime.fromtimestamp(b.ts).strftime("%m-%d %H:%M"), b.reason[:70])
    console.print(t)


@ban_app.command("add")
def ban_add(address: str, reason: str = typer.Option(..., help="Why (kept with the entry)"),
            role: str = typer.Option("manual", help="creator | cluster | funder | operation | manual")):
    """Ban a wallet. The running bot picks it up within a minute."""
    from solmeme.lab.banlist import BanList
    from solmeme.lab.people import is_address

    if not is_address(address):
        console.print("[red]not a Solana address[/red]")
        raise typer.Exit(1)
    s, _ = _ctx()
    bans = BanList(s.data_dir)
    added = bans.add(address, role, reason)
    bans.save()
    console.print(("banned " if added else "already banned ") + address)


@ban_app.command("remove")
def ban_remove(address: str):
    """Take a wallet off the ban list."""
    from solmeme.lab.banlist import BanList

    s, _ = _ctx()
    bans = BanList(s.data_dir)
    ok = bans.remove(address)
    bans.save()
    console.print(("removed " if ok else "not on the list: ") + address)


# ── strategy control ──────────────────────────────────────────────────────────
@app.command()
def strategies(strategy: Optional[str] = typer.Argument(None)):
    """List strategy versions and their status."""
    _, store = _ctx()
    t = Table()
    for c in ("strategy", "version", "status", "parent", "since", "note"):
        t.add_column(c)
    for v in store.versions(strategy):
        t.add_row(v["strategy_id"], v["version"], v["status"], v["parent_version"] or "",
                  datetime.fromtimestamp(v["status_ts"]).strftime("%m-%d %H:%M"), (v["note"] or "")[:60])
    console.print(t)


@app.command()
def show(strategy: str, version: Optional[str] = typer.Argument(None)):
    """Print a strategy spec (champion by default)."""
    from solmeme.strategy.spec import spec_from_json

    _, store = _ctx()
    row = store.get_version(strategy, version) if version else store.champion(strategy)
    if not row:
        console.print("[red]not found[/red]")
        raise typer.Exit(1)
    console.print(spec_from_json(row["spec_json"]).to_yaml(), markup=False)


@app.command()
def promote(strategy: str, version: str):
    """Manually make a version the champion."""
    from solmeme.ai.promoter import Promoter

    s, store = _ctx()
    Promoter(s, store).promote(strategy, version, "manual")
    console.print(f"{strategy}@{version} is now champion")


@app.command()
def rollback(strategy: str, to: Optional[str] = typer.Option(None, "--to", help="Version (default: previous champion)")):
    """Restore an earlier champion."""
    from solmeme.ai.promoter import Promoter

    s, store = _ctx()
    v = Promoter(s, store).rollback(strategy, to)
    console.print(f"{strategy} champion is now {v}")


@app.command("new-attempt")
def new_attempt(why: str = typer.Option("", help="Why, kept with the attempt's summary")):
    """Live dry run: end this attempt and start a fresh real-size wallet (the running bot picks it up)."""
    import json as _json

    from solmeme.config import load_settings
    from solmeme.sim.dryrun import REQUEST_FILE

    s = load_settings()
    path = s.data_dir / "session" / REQUEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"action": "new_attempt", "why": why}), encoding="utf-8")
    console.print("asked the running bot to start a new live attempt (within a few seconds)")


@app.command()
def demote(strategy: str, reason: str = typer.Option("manual", help="Why, kept in the version note")):
    """Move a champion off the shared wallet: it keeps trading as a trial on its own wallet."""
    from solmeme.ai.promoter import Promoter

    s, store = _ctx()
    v = Promoter(s, store).demote(strategy, reason)
    console.print(f"{strategy}@{v} is now a trial (own wallet). Undo: solmeme promote {strategy} {v}")


@app.command()
def kill(strategy: str, version: str):
    """Stop a running challenger."""
    from solmeme.ai.promoter import Promoter

    s, store = _ctx()
    Promoter(s, store).kill_challenger(strategy, version)
    console.print(f"challenger {strategy}@{version} stopped")


@app.command()
def report(days: float = typer.Option(7)):
    """Write the markdown report (live performance, versions, proposals, AI findings)."""
    from solmeme.reports.markdown import daily_report

    s, store = _ctx()
    out = daily_report(s, store, days)
    console.print(f"report → {out}")


@app.command()
def session(minutes: Optional[float] = typer.Option(None, help="Look back N minutes (default: --every, else ai.session_review.every_min)"),
            every: Optional[float] = typer.Option(None, help="Repeat every N minutes until Ctrl+C"),
            ai: Optional[str] = typer.Option(None, help="claude | local | none (default: ai.session_review.provider)"),
            force_ai: bool = typer.Option(False, "--force-ai", help="Ask the AI even when nothing changed")):
    """Report on the running bot: wallet, trades, problems; an AI note only when something changed."""
    from rich.markdown import Markdown

    from solmeme.ai.session_review import PROVIDERS, review_once, write_schedule
    from solmeme.reports.session import load_live

    s, store = _ctx()
    cfg = s.ai.session_review
    provider = (ai or cfg.provider).lower()
    if provider not in PROVIDERS:
        console.print(f"[red]--ai must be one of {', '.join(PROVIDERS)}[/red]")
        raise typer.Exit(2)
    look = minutes or every or cfg.every_min
    while True:
        started = time.time()
        out = review_once(s, store, look, provider, force_ai=force_ai)
        console.print(Markdown(out.text))
        console.print(f"[dim]→ {out.path} · {out.ai_status}[/dim]")
        if not every:
            return
        nxt = started + every * 60
        seen_run = out.report.run_id
        write_schedule(s, nxt, every)
        console.print(f"[dim]next report at {datetime.fromtimestamp(nxt).strftime('%H:%M')} (Ctrl+C to stop)[/dim]\n")
        while time.time() < nxt:
            time.sleep(min(30.0, max(1.0, nxt - time.time())))
            snap, _ = load_live(s)
            run = (snap or {}).get("run_id")
            if run and run != seen_run and not snap.get("ended"):
                seen_run = run                    # a new bot run: report on it one interval after it started
                first = float(snap.get("started") or time.time()) + every * 60
                if first < nxt:
                    nxt = first
                    write_schedule(s, nxt, every)
                console.print(f"[dim]new run {run}: first report at "
                              f"{datetime.fromtimestamp(nxt).strftime('%H:%M')}[/dim]")


if __name__ == "__main__":
    app()

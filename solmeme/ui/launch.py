"""Start the localhost server, and the Bubble Tea program when asked."""

from __future__ import annotations

import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from solmeme.config import ROOT
from solmeme.ui.server import Hub, serve
from solmeme.ui.snapshot import build_snapshot

UI_PORT = 8787


class UiError(RuntimeError):
    pass


def attach(session, mode: str) -> str:
    """Publish snapshots on localhost. mode is "web" or "tui". Returns the site URL."""
    if mode not in ("web", "tui"):
        raise UiError(f"unknown ui {mode}")
    hub = Hub()
    session.hub = hub
    session.publish()
    try:
        httpd, port = serve(hub, ROOT / "web", port=UI_PORT)
    except OSError as exc:
        raise UiError(str(exc)) from exc
    session._httpd = httpd
    url = f"http://127.0.0.1:{port}/"
    if mode == "web":
        session.console.print(f"[bold]website[/bold] {url}")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
        return url
    binary = ensure_tui_binary()
    session._tui_proc = subprocess.Popen(
        [str(binary), "--url", f"http://127.0.0.1:{port}/api/snapshot"],
        cwd=str(ROOT),
    )
    return url


def ensure_tui_binary() -> Path:
    tui = ROOT / "tui"
    binary = tui / "solmeme-tui"
    sources = list(tui.glob("*.go"))
    gomod = tui / "go.mod"
    if not sources or not gomod.is_file():
        raise UiError("tui/ has no Go program yet")
    newest = max(path.stat().st_mtime for path in [*sources, gomod])
    if binary.is_file() and binary.stat().st_mtime >= newest:
        return binary
    go = shutil.which("go")
    if not go:
        raise UiError("Go is not installed, so the Bubble Tea UI cannot be built. Use --ui web or the normal CLI.")
    sys.__stderr__.write("building Bubble Tea UI…\n")
    try:
        subprocess.run([go, "build", "-o", binary.name, "."], cwd=tui, check=True)
    except subprocess.CalledProcessError as exc:
        raise UiError("Bubble Tea UI failed to build") from exc
    return binary

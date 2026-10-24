"""The council's sittings as chat transcripts (council_messages), for the dashboard's live view."""

from __future__ import annotations

import json
import time

from solmeme.config import Settings
from solmeme.core.store import Store

LIVE_IDLE_S = 30 * 60        # an unclosed sitting with a message this recent is still in session


def build_council_chat(store: Store, settings: Settings, sitting: str = "", after_id: int = 0) -> dict:
    """The newest sittings, and the chosen one's messages (default: the newest) after `after_id`."""
    sittings = store.council_sittings(30)
    ids = [s["sitting"] for s in sittings]
    chosen = sitting if sitting in ids else (ids[0] if ids else "")
    now = time.time()
    msgs = []
    for r in store.council_messages(chosen, after_id) if chosen else []:
        data = json.loads(r["data_json"]) if r["data_json"] else {}
        msgs.append(dict(id=r["id"], ts=r["ts"], topic=r["topic"] or "", phase=r["phase"], speaker=r["speaker"],
                         kind=r["kind"], text=r["text"] or "", data=data))
    try:
        evo = json.loads((settings.data_dir / "session" / "evolve.json").read_text())
    except (OSError, ValueError):
        evo = {}
    return dict(
        now=now,
        sitting=chosen,
        sittings=[dict(s, live=bool(not s["closed"] and now - s["last_ts"] < LIVE_IDLE_S)) for s in sittings],
        messages=msgs,
        judges={m.name: dict(model=m.model, kind=m.kind, fallback=m.fallback, enabled=m.enabled)
                for m in settings.council.members},
        generation=dict(gen=evo.get("gen"), phase=evo.get("phase"), started=evo.get("started")),
    )

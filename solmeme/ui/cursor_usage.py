"""Cursor usage for the dashboard: the bridge's own call meter and, when its session cookie is set, the
account's real plan balance. Both come from the Cursor bridge (scripts/cursor_council/shim.py /v1/usage),
so the login cookie never leaves the Cursor container."""

from __future__ import annotations

import threading
import time

import httpx

from solmeme.config import Settings, secret

CACHE_S = 60.0
_cache: dict = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def cursor_judges(settings: Settings) -> list:
    """Council members served by the Cursor bridge (named cursor-*, kind openai)."""
    return [m for m in settings.council.members if m.kind == "openai" and m.name.startswith("cursor")]


def cursor_usage(settings: Settings, fresh: bool = False) -> dict:
    judges = cursor_judges(settings)
    if not judges:
        return {"configured": False, "error": "no Cursor judge in config/settings.yaml"}
    with _lock:
        if not fresh and _cache["data"] is not None and time.time() - _cache["ts"] < CACHE_S:
            return _cache["data"]
    m = judges[0]
    key = secret(m.key_env) if m.key_env else ""
    try:
        r = httpx.get(m.base_url.rstrip("/") + "/usage" + ("?fresh=1" if fresh else ""), timeout=20.0,
                      headers={"Authorization": f"Bearer {key}"} if key else {})
        r.raise_for_status()
        data = dict(r.json(), configured=True)
    except (httpx.HTTPError, ValueError) as exc:
        data = {"configured": True, "error": f"Cursor bridge not answering: {exc}"[:200]}
    data["judges"] = [dict(name=j.name, model=j.model, enabled=j.enabled) for j in judges]
    with _lock:
        _cache.update(ts=time.time(), data=data)
    return data

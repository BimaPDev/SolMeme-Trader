"""The dashboard's Cursor usage comes from the bridge, is cached, and degrades to a message when the bridge is down."""

import httpx

from solmeme.config import CouncilMember
from solmeme.ui import cursor_usage as cu


def _settings(settings):
    settings.council.members = [
        CouncilMember(name="cursor-sonnet", kind="openai", base_url="http://bridge:8088/v1", model="sonnet", key_env="SHIM_T"),
        CouncilMember(name="cursor-gpt", kind="openai", base_url="http://bridge:8088/v1", model="gpt", key_env="SHIM_T"),
        CouncilMember(name="qwen8b", kind="ollama", base_url="http://gpu", model="qwen3:8b"),
    ]
    return settings


def test_usage_is_read_from_the_bridge_and_cached(settings, monkeypatch):
    calls = []

    def fake_get(url, timeout, headers):
        calls.append((url, headers))
        return httpx.Response(200, json={"today": {"calls": 3}, "account": {"configured": False}},
                              request=httpx.Request("GET", url))

    monkeypatch.setenv("SHIM_T", "tok")
    monkeypatch.setattr(cu.httpx, "get", fake_get)
    cu._cache.update(ts=0.0, data=None)
    out = cu.cursor_usage(_settings(settings))
    assert out["today"]["calls"] == 3 and [j["name"] for j in out["judges"]] == ["cursor-sonnet", "cursor-gpt"]
    assert calls == [("http://bridge:8088/v1/usage", {"Authorization": "Bearer tok"})]
    cu.cursor_usage(settings)                                  # within a minute: served from the cache
    assert len(calls) == 1
    cu.cursor_usage(settings, fresh=True)                      # the Sync button skips it
    assert calls[-1][0].endswith("/usage?fresh=1")


def test_a_bridge_that_is_down_gives_a_message_not_an_error(settings, monkeypatch):
    def down(url, timeout, headers):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cu.httpx, "get", down)
    cu._cache.update(ts=0.0, data=None)
    out = cu.cursor_usage(_settings(settings))
    assert "not answering" in out["error"] and len(out["judges"]) == 2

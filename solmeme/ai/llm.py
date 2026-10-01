"""LLM client — port of PaperTrader llm.py, extended.

Default provider is Claude via the Anthropic SDK:
  * structured output: client.beta.messages.parse(output_format=PydanticModel)
  * the big static system prompt is prompt-cached (cache_control, 1h TTL)
  * explicit effort (Opus 5.5 defaults to "medium"; reviews run at "high")
  * server-side refusal fallback (fallbacks="default")
  * stop_reason checked before reading the parsed output
  * every call is priced into the llm_usage ledger; a daily $ budget is enforced
OpenAI-compatible and Hermes-over-SSH providers are kept from PaperTrader.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, TypeVar

import httpx
from pydantic import BaseModel

from solmeme.config import Settings

log = logging.getLogger("solmeme.llm")
T = TypeVar("T", bound=BaseModel)


class BudgetExceeded(RuntimeError):
    pass


class LLMRefusal(RuntimeError):
    pass


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    model: str
    stop_reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float = 0.0


def _day_start() -> float:
    d = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def complete_json(settings: Settings, system: str, user: str, schema: type[T], *, purpose: str,
                  store=None, effort: str | None = None) -> LLMResult[T]:
    ai = settings.ai
    if store is not None and store.llm_spend_since(_day_start()) >= ai.daily_budget_usd:
        raise BudgetExceeded(f"daily LLM budget ${ai.daily_budget_usd:.2f} reached")
    if ai.provider == "claude":
        res = _claude(settings, system, user, schema, effort or ai.effort)
    elif ai.provider == "hermes_ssh":
        res = _hermes(settings, system, user, schema)
    else:
        res = _openai_compat(settings, system, user, schema)
    if store is not None:
        store.log_llm_usage(dict(ts=time.time(), purpose=purpose, model=res.model, input_tokens=res.input_tokens,
                                 output_tokens=res.output_tokens, cache_read=res.cache_read,
                                 cache_write=res.cache_write, cost_usd=res.cost_usd))
    return res


def _claude(settings: Settings, system: str, user: str, schema: type[T], effort: str) -> LLMResult[T]:
    import anthropic

    ai = settings.ai
    client = anthropic.Anthropic(max_retries=3, timeout=600)
    try:
        resp = client.beta.messages.parse(
            model=ai.model,
            max_tokens=ai.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
            messages=[{"role": "user", "content": user}],
            output_format=schema,
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.RateLimitError as exc:
        raise RuntimeError(f"Claude rate limited: {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise RuntimeError(f"Claude API error {exc.status_code}: {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise RuntimeError("Claude connection error") from exc

    if resp.stop_reason == "refusal":
        cat = getattr(resp.stop_details, "category", None) if getattr(resp, "stop_details", None) else None
        raise LLMRefusal(f"Claude declined the request (category={cat})")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude hit max_tokens before finishing the structured output; raise ai.max_tokens")
    parsed = resp.parsed_output
    if parsed is None:
        raise RuntimeError("Claude returned no parsed output")
    u = resp.usage
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    cost = (u.input_tokens * ai.price_in + u.output_tokens * ai.price_out
            + cr * ai.price_cache_read + cw * ai.price_cache_write) / 1e6
    return LLMResult(parsed, resp.model, resp.stop_reason or "", u.input_tokens, u.output_tokens, cr, cw, cost)


def _schema_hint(schema: type[BaseModel]) -> str:
    return ("\nRespond with JSON only, no other text, matching this JSON Schema:\n"
            + json.dumps(schema.model_json_schema(), separators=(",", ":")))


def _extract_json(text: str) -> str:
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end < start:
        raise ValueError(f"no JSON object in response: {text[:200]!r}")
    return text[start:end + 1]


def _openai_compat(settings: Settings, system: str, user: str, schema: type[T]) -> LLMResult[T]:
    c = settings.ai.openai_compat
    resp = httpx.post(
        f"{c.get('base_url', 'https://api.deepseek.com/v1')}/chat/completions",
        headers={"Authorization": f"Bearer {os.getenv(c.get('key_env', 'DEEPSEEK_KEY'), '')}"},
        json={"model": c.get("model", "deepseek-chat"), "max_tokens": settings.ai.max_tokens,
              "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system + _schema_hint(schema)},
                           {"role": "user", "content": user}]},
        timeout=600,
    )
    resp.raise_for_status()
    j = resp.json()
    u = j.get("usage") or {}
    parsed = schema.model_validate_json(_extract_json(j["choices"][0]["message"]["content"]))
    return LLMResult(parsed, c.get("model", ""), "end_turn", u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def _hermes(settings: Settings, system: str, user: str, schema: type[T]) -> LLMResult[T]:
    c = settings.ai.hermes_ssh
    prompt = f"{system}{_schema_hint(schema)}\n\n{user}"
    q = "'" + prompt.replace("'", "'\\''") + "'"
    remote = f"cd {c.get('workdir', '~')} && {c.get('bin_path', 'hermes')} chat -q {q} -Q --source solmeme --yolo"
    r = subprocess.run(["ssh", "-i", c.get("key_path", ""), "-o", "StrictHostKeyChecking=accept-new",
                        "-o", "ConnectTimeout=15", "-p", str(c.get("port", 22)),
                        f"{c.get('user', 'hermes')}@{c.get('host', '')}", remote],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"ssh/hermes exited {r.returncode}")
    return LLMResult(schema.model_validate_json(_extract_json(r.stdout)), "hermes", "end_turn")

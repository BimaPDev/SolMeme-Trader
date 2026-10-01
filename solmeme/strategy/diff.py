"""Structured strategy changes (what the AI and optimizer may propose) and
their validation. Guardrails live here, not in the prompt:
  * params stay inside their declared [lo, hi] bounds
  * new rules must use registry features
  * locked rules, sizing and universe can never be changed
  * literal exit values stay inside hard sanity bounds
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel

from solmeme.features.registry import FEATURES
from solmeme.strategy.spec import StrategySpec

EXIT_BOUNDS: dict[str, tuple[float, float]] = {
    "stop_loss_pct": (5, 95),
    "trailing_pct": (5, 90),
    "trailing_activate_x": (1.05, 20),
    "max_hold_s": (30, 86_400),
    "stale_after_s": (30, 7_200),
}
EXIT_BOOLS = {"on_dev_sell", "on_kol_sell", "on_rug"}
MAX_RULES_PER_SECTION = 12


class Change(BaseModel):
    op: Literal["set_param", "add_rule", "remove_rule", "set_rule_value", "set_exit", "set_take_profit"]
    param: str | None = None
    value: float | int | bool | str | list[float] | None = None
    rule_id: str | None = None
    section: Literal["all", "any", "none"] | None = None
    feature: str | None = None
    operator: Literal[">", ">=", "<", "<=", "==", "!=", "between", "in"] | None = None
    exit_field: str | None = None
    take_profit: list[list[float]] | None = None   # [[at_x, sell_pct], ...]


class InvalidChange(ValueError):
    pass


def change_signature(changes: list[Change]) -> str:
    """Value-insensitive fingerprint: what was changed and in which direction-ish.
    Used to stop the AI from re-proposing an already rejected idea."""
    parts = []
    for c in changes:
        target = c.param or c.rule_id or c.exit_field or c.feature or ""
        parts.append(f"{c.op}:{target}:{c.section or ''}:{c.feature or ''}:{c.operator or ''}")
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()[:16]


def change_hash(changes: list[Change]) -> str:
    blob = json.dumps([c.model_dump(mode="json") for c in changes], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def apply_changes(spec: StrategySpec, changes: list[Change], max_changes: int = 3) -> StrategySpec:
    if not changes:
        raise InvalidChange("no changes")
    if len(changes) > max_changes:
        raise InvalidChange(f"too many changes ({len(changes)} > {max_changes})")
    d: dict[str, Any] = spec.model_dump(mode="json")
    for c in changes:
        _apply_one(d, c)
    try:
        new = StrategySpec.model_validate(d)
    except Exception as exc:  # pydantic validation error
        raise InvalidChange(f"spec invalid after change: {exc}") from exc
    if new.version == spec.version:
        raise InvalidChange("changes produce an identical strategy")
    return new


def _find_rule(d: dict, rule_id: str) -> tuple[str, int, dict]:
    for sec in ("all", "any", "none"):
        for i, r in enumerate(d["entry"][sec]):
            if r["id"] == rule_id:
                return sec, i, r
    raise InvalidChange(f"unknown rule {rule_id!r}")


def _apply_one(d: dict, c: Change) -> None:
    if c.op == "set_param":
        p = d["params"].get(c.param or "")
        if p is None:
            raise InvalidChange(f"unknown param {c.param!r}")
        v = _num(c.value)
        if not (p["lo"] <= v <= p["hi"]):
            raise InvalidChange(f"param {c.param}={v} outside bounds [{p['lo']}, {p['hi']}]")
        p["value"] = v

    elif c.op == "add_rule":
        if not (c.section and c.feature and c.operator and c.rule_id):
            raise InvalidChange("add_rule needs section, feature, operator, rule_id")
        if c.feature not in FEATURES:
            raise InvalidChange(f"unknown feature {c.feature!r}")
        if any(r["id"] == c.rule_id for s in ("all", "any", "none") for r in d["entry"][s]):
            raise InvalidChange(f"rule id {c.rule_id!r} already exists")
        if len(d["entry"][c.section]) >= MAX_RULES_PER_SECTION:
            raise InvalidChange("too many rules in section")
        d["entry"][c.section].append(
            {"id": c.rule_id, "f": c.feature, "op": c.operator, "v": c.value, "locked": False})

    elif c.op == "remove_rule":
        sec, i, r = _find_rule(d, c.rule_id or "")
        if r.get("locked"):
            raise InvalidChange(f"rule {c.rule_id} is locked")
        d["entry"][sec].pop(i)

    elif c.op == "set_rule_value":
        sec, i, r = _find_rule(d, c.rule_id or "")
        if r.get("locked"):
            raise InvalidChange(f"rule {c.rule_id} is locked")
        if isinstance(r["v"], str) and r["v"].startswith("$"):
            # rule is parameterized: route through the bounded param
            _apply_one(d, Change(op="set_param", param=r["v"][1:], value=c.value))
        else:
            r["v"] = c.value
            if c.operator:
                r["op"] = c.operator

    elif c.op == "set_exit":
        f = c.exit_field or ""
        cur = d["exits"].get(f, None)
        if isinstance(cur, str) and cur.startswith("$"):
            _apply_one(d, Change(op="set_param", param=cur[1:], value=c.value))
        elif f in EXIT_BOUNDS:
            v = _num(c.value) if c.value is not None else None
            if v is not None:
                lo, hi = EXIT_BOUNDS[f]
                if not (lo <= v <= hi):
                    raise InvalidChange(f"{f}={v} outside sane bounds [{lo}, {hi}]")
            d["exits"][f] = v
        elif f in EXIT_BOOLS:
            d["exits"][f] = bool(c.value)
        elif f == "on_migration":
            if c.value not in ("hold", "sell"):
                raise InvalidChange("on_migration must be hold|sell")
            d["exits"][f] = c.value
        else:
            raise InvalidChange(f"exit field {f!r} cannot be changed")

    elif c.op == "set_take_profit":
        ladder = c.take_profit or []
        if not ladder or len(ladder) > 5:
            raise InvalidChange("take_profit ladder needs 1-5 levels")
        out = []
        for lvl in ladder:
            if len(lvl) != 2:
                raise InvalidChange("take_profit level must be [at_x, sell_pct]")
            x, pct = float(lvl[0]), float(lvl[1])
            if not (1.05 <= x <= 100) or not (1 <= pct <= 100):
                raise InvalidChange(f"take_profit level {lvl} out of bounds")
            out.append({"at_x": x, "sell_pct": pct})
        d["exits"]["take_profit"] = sorted(out, key=lambda t: t["at_x"])
    else:
        raise InvalidChange(f"unsupported op {c.op}")


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise InvalidChange(f"expected a number, got {v!r}") from exc

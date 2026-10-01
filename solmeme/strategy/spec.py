"""Declarative strategy spec (YAML) + version hashing + param resolution.

A strategy = universe filter + entry rules (DSL over registry features) +
sizing + exit policy + tunable params with hard bounds. Values may reference
params as "$name". The content hash of the spec is its version.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from solmeme.features.registry import FEATURES

Op = Literal[">", ">=", "<", "<=", "==", "!=", "between", "in"]


class Cond(BaseModel):
    id: str
    f: str
    op: Op
    v: Any = None
    locked: bool = False

    @model_validator(mode="after")
    def _check(self):
        if self.f not in FEATURES:
            raise ValueError(f"rule {self.id!r}: unknown feature {self.f!r}")
        if self.op == "between" and not (isinstance(self.v, list) and len(self.v) == 2):
            raise ValueError(f"rule {self.id!r}: 'between' needs [lo, hi]")
        return self


class Entry(BaseModel):
    all: list[Cond] = []
    any: list[Cond] = []
    none: list[Cond] = []
    # event kinds that trigger an entry evaluation for the touched token
    triggers: list[str] = ["curve_trade", "pool_trade", "enrichment", "wallet_trade", "signal"]

    def rules(self) -> list[tuple[str, Cond]]:
        return [(s, c) for s in ("all", "any", "none") for c in getattr(self, s)]


class TP(BaseModel):
    at_x: float | str
    sell_pct: float | str


class Exits(BaseModel):
    take_profit: list[TP] = []
    stop_loss_pct: float | str | None = 40
    trailing_pct: float | str | None = None
    trailing_activate_x: float | str = 1.5
    max_hold_s: float | str | None = 1800
    stale_after_s: float | str | None = 300
    on_dev_sell: bool = False
    on_kol_sell: bool = False
    on_rug: bool = True
    on_migration: Literal["hold", "sell"] = "hold"


class Param(BaseModel):
    value: float
    lo: float
    hi: float
    type: Literal["int", "float"] = "float"

    @model_validator(mode="after")
    def _bounds(self):
        if not (self.lo <= self.value <= self.hi):
            raise ValueError(f"param value {self.value} outside [{self.lo}, {self.hi}]")
        if self.type == "int":
            self.value = float(round(self.value))
        return self


class Universe(BaseModel):
    pools: list[str] | None = None            # None = any launchpad/pool
    stage: Literal["curve", "migrated", "any"] = "any"
    min_age_s: float = 0
    max_age_s: float | None = None


class Sizing(BaseModel):
    sol: float | str = 0.1
    max_open: int = 5
    max_entries_per_token: int = 1


class StrategySpec(BaseModel):
    id: str
    description: str = ""
    universe: Universe = Universe()
    entry: Entry
    sizing: Sizing = Sizing()
    exits: Exits = Exits()
    params: dict[str, Param] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _refs(self):
        for ref in _param_refs(self.model_dump(exclude={"params"})):
            if ref not in self.params:
                raise ValueError(f"{self.id}: references undefined param ${ref}")
        ids = [c.id for _, c in self.entry.rules()]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.id}: duplicate rule ids")
        return self

    # ── identity ──
    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def version(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:10]

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    # ── param resolution ──
    def p(self, x):
        """Resolve a value that may be a "$param" reference (recursively for lists)."""
        if isinstance(x, str) and x.startswith("$"):
            return self.params[x[1:]].value
        if isinstance(x, list):
            return [self.p(i) for i in x]
        return x

    def with_params(self, values: dict[str, float]) -> "StrategySpec":
        d = self.model_dump(mode="json")
        for k, v in values.items():
            d["params"][k]["value"] = v
        return StrategySpec.model_validate(d)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json", exclude_defaults=False), sort_keys=False)


def _param_refs(obj) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, str) and obj.startswith("$"):
        out.add(obj[1:])
    elif isinstance(obj, dict):
        for v in obj.values():
            out |= _param_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _param_refs(v)
    return out


def load_spec(path: Path) -> StrategySpec:
    return StrategySpec.model_validate(yaml.safe_load(path.read_text()))


def load_dir(d: Path) -> list[StrategySpec]:
    return [load_spec(p) for p in sorted(d.glob("*.yaml"))]


def spec_from_json(s: str) -> StrategySpec:
    return StrategySpec.model_validate_json(s)

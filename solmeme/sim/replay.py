"""Replay recorded events through the exact live engine (backtests, optimizer,
promotion gates)."""

from __future__ import annotations

from typing import Iterable

from solmeme.config import Settings
from solmeme.core.eventlog import read_events
from solmeme.sim.engine import Engine, Journal
from solmeme.strategy.spec import StrategySpec


def load_events(settings: Settings, start: float | None = None, end: float | None = None) -> list:
    return list(read_events(settings.events_dir, start, end))


def run_replay(settings: Settings, specs: list[tuple[StrategySpec, str]], events: Iterable,
               journal: Journal | None = None, seed: int = 7, snapshot_features: bool = True,
               record_near_misses: bool = True) -> Engine:
    eng = Engine(specs, settings, journal=journal, seed=seed, snapshot_features=snapshot_features,
                 record_near_misses=record_near_misses)
    last = None
    for ev in events:
        eng.step(ev)
        last = ev.ts
    eng.finish(last)
    return eng

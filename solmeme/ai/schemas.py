"""Structured output of an AI review. Kept flat (no nested unions) so it fits
structured-output schema limits and works with every provider; semantic
validation happens in strategy/diff.py, not here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from solmeme.strategy.diff import Change

MistakeCategory = Literal[
    "entry_too_late", "entry_too_early", "rug_not_filtered", "dev_dump", "exit_too_early", "exit_too_late",
    "stop_too_tight", "stop_too_loose", "fee_drag", "illiquid_exit", "fill_failure", "filter_too_loose",
    "filter_too_strict", "false_signal", "regime_shift", "sample_noise", "other",
]


class Finding(BaseModel):
    category: MistakeCategory
    description: str
    evidence_trade_ids: list[str]
    affected_trades: int
    est_pnl_impact_sol: float
    confidence: Literal["low", "medium", "high"]


class Proposal(BaseModel):
    title: str
    hypothesis: str
    changes: list[Change]
    expected_effect: str
    expected_trades_change: Literal["fewer", "same", "more"]
    evidence: str
    risk: str
    is_retest: bool


class ReviewOutput(BaseModel):
    summary: str
    findings: list[Finding]
    proposals: list[Proposal]
    feature_requests: list[str]

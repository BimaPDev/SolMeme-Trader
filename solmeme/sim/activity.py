"""In-memory board of what the bot is doing with each token."""

from __future__ import annotations

import time
from dataclasses import dataclass

from solmeme.sources.watchlist import RANK

PHASES = ("holding", "selling", "buying", "analyzing", "skipped", "checking", "watching")
_MERGE = ("buying", "analyzing", "skipped", "selling")
_UNKNOWN_RANK = 1_000_000


@dataclass(frozen=True)
class TokenNote:
    mint: str
    phase: str
    detail: str
    symbol: str = ""
    ts: float = 0.0
    source: str = ""


def short_mint(mint: str) -> str:
    if len(mint) > 12:
        return mint[:4] + "…" + mint[-4:]
    return mint


def age_label(ts: float, now: float) -> str:
    age = max(0.0, now - ts)
    if age < 60:
        return f"{int(age)}s"
    if age < 3600:
        return f"{int(age // 60)}m"
    return f"{int(age // 3600)}h"


class TokenActivity:
    def __init__(self, per_phase: int = 40):
        self.per_phase = per_phase
        self._notes: dict[str, dict[str, TokenNote]] = {phase: {} for phase in PHASES}

    def note(
        self,
        mint: str,
        phase: str,
        detail: str,
        *,
        symbol: str = "",
        ts: float | None = None,
        source: str = "",
    ) -> None:
        bucket = self._notes.get(phase)
        if bucket is None:
            return
        prev = bucket.pop(mint, None)
        if not symbol and prev is not None:
            symbol = prev.symbol
        if ts is None:
            ts = time.time()
        bucket[mint] = TokenNote(mint, phase, detail, symbol, ts, source)
        while len(bucket) > self.per_phase:
            del bucket[next(iter(bucket))]

    def recent(self, phase: str, limit: int) -> list[TokenNote]:
        bucket = self._notes.get(phase)
        if not bucket or limit <= 0:
            return []
        notes = list(bucket.values())
        notes.reverse()
        return notes[:limit]

    def board(
        self,
        watch,
        symbols: dict[str, str],
        held: dict[str, str],
        now: float,
        *,
        hold_limit: int = 6,
        analyze_limit: int = 6,
        check_limit: int = 4,
        watch_limit: int = 4,
        opened: dict[str, float] | None = None,
    ) -> list[TokenNote]:
        out: list[TokenNote] = []
        shown: set[str] = set()

        opened = opened or {}
        for mint, strategy in list(held.items())[:hold_limit]:
            selling = self._notes["selling"].get(mint)
            if selling is not None:
                row = selling
            else:
                row = TokenNote(
                    mint, "holding", f"{strategy} open", symbols.get(mint, ""),
                    opened.get(mint, now),
                )
            self._add(out, shown, row, symbols)

        merged: list[TokenNote] = []
        for phase in _MERGE:
            merged.extend(self._notes[phase].values())
        merged.sort(key=lambda n: n.ts, reverse=True)
        self._take(out, shown, merged, symbols, analyze_limit)
        self._take(out, shown, self.recent("checking", self.per_phase), symbols, check_limit)

        watching: list[TokenNote] = []
        for mint in watch.priority_list(now):
            if mint in shown:
                continue
            detail, since = _watch_choice(watch, mint, now)
            watching.append(TokenNote(mint, "watching", detail, symbols.get(mint, ""), since))
            if len(watching) >= watch_limit:
                break
        self._take(out, shown, watching, symbols, watch_limit)
        return out

    def _add(self, out: list[TokenNote], shown: set[str], note: TokenNote, symbols: dict[str, str]) -> None:
        if note.mint in shown:
            return
        symbol = note.symbol or symbols.get(note.mint, "")
        if symbol != note.symbol:
            note = TokenNote(note.mint, note.phase, note.detail, symbol, note.ts, note.source)
        out.append(note)
        shown.add(note.mint)

    def _take(
        self,
        out: list[TokenNote],
        shown: set[str],
        notes: list[TokenNote],
        symbols: dict[str, str],
        limit: int,
    ) -> None:
        added = 0
        for note in notes:
            if added >= limit:
                return
            if note.mint in shown:
                continue
            self._add(out, shown, note, symbols)
            added += 1


def _watch_choice(watch, mint: str, now: float) -> tuple[str, float]:
    best_key: tuple[int, str] | None = None
    best_ts = now
    for reason, ts in watch.reasons.get(mint, {}).items():
        window = watch.launch_window_s if reason == "launch" else watch.ttl_s
        if now - ts > window:
            continue
        key = (RANK.get(reason, _UNKNOWN_RANK), reason)
        if best_key is None or key < best_key:
            best_key = key
            best_ts = ts
    if best_key is not None:
        return best_key[1], best_ts
    if mint in watch.pinned:
        return "pinned", now
    return "watched", now

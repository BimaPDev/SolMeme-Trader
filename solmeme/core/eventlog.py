"""Append-only event log: data/events/YYYYMMDD/HH-<session>.jsonl.gz (UTC), one event per line.

This is the source of truth for replay/backtests. Files are written in receive
order, so reading them in (hour, session) order yields a time-ordered stream.

Each run writes its own files (the session id is its start time in ms). A run that
is killed mid-write leaves a truncated gzip, and gzip can't read past a truncated
member — so if runs shared a file, one crash would silently hide every later run's
events in that hour. Older single-file hours (HH.jsonl.gz) are still read.
"""

from __future__ import annotations

import gzip
import io
import time
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from solmeme.core.events import dump_event, parse_event


def _hour_path(root: Path, ts: float, session: str) -> Path:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return root / dt.strftime("%Y%m%d") / f"{dt.strftime('%H')}-{session}.jsonl.gz"


def _sort_key(f: Path) -> tuple:
    """(day, hour, session) — legacy HH.jsonl.gz files sort first within their hour."""
    stem = f.name.split(".", 1)[0]
    hour, _, session = stem.partition("-")
    return f.parent.name, hour, int(session) if session.isdigit() else -1


class EventLogWriter:
    def __init__(self, root: Path, flush_every_s: float = 2.0):
        self.root = root
        self.session = f"{int(time.time() * 1000):013d}"
        self.flush_every_s = flush_every_s
        self._path: Path | None = None
        self._fh: io.TextIOWrapper | None = None
        self._last_flush = 0.0
        self.count = 0

    def write(self, ev) -> None:
        path = _hour_path(self.root, ev.ts, self.session)
        if path != self._path:
            self._open(path)
        assert self._fh is not None
        self._fh.write(dump_event(ev))
        self._fh.write("\n")
        self.count += 1
        now = time.monotonic()
        if now - self._last_flush > self.flush_every_s:
            self._fh.flush()
            self._last_flush = now

    def _open(self, path: Path) -> None:
        self.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        # gzip append creates a new member; gzip.open reads multi-member files fine.
        self._fh = io.TextIOWrapper(gzip.open(path, "ab", compresslevel=5), encoding="utf-8")
        self._path = path

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
            self._path = None


def list_files(root: Path, start: float | None = None, end: float | None = None) -> list[Path]:
    if not root.exists():
        return []
    files = sorted(root.glob("*/*.jsonl.gz"), key=_sort_key)
    if start is None and end is None:
        return files
    out = []
    for f in files:
        try:
            dt = datetime.strptime(f.parent.name + f.name[:2], "%Y%m%d%H").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        lo, hi = dt.timestamp(), (dt + timedelta(hours=1)).timestamp()
        if start is not None and hi < start:
            continue
        if end is not None and lo > end:
            continue
        out.append(f)
    return out


def read_events(root: Path, start: float | None = None, end: float | None = None) -> Iterator:
    for f in list_files(root, start, end):
        try:
            with gzip.open(f, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        ev = parse_event(line)
                    except Exception:
                        continue
                    if start is not None and ev.ts < start:
                        continue
                    if end is not None and ev.ts > end:
                        return
                    yield ev
        except (EOFError, OSError, zlib.error):
            # a file still being written (or truncated by a crash) — keep what we read
            continue


def time_range(root: Path) -> tuple[float, float] | None:
    """First and last event timestamps in the log (reads first/last file only)."""
    files = list_files(root)
    if not files:
        return None
    first = next(read_events_file(files[0]), None)
    last = None
    for ev in read_events_file(files[-1]):
        last = ev
    if first is None or last is None:
        return None
    return first.ts, last.ts


def read_events_file(f: Path) -> Iterator:
    try:
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        yield parse_event(line)
                    except Exception:
                        continue
    except (EOFError, OSError, zlib.error):
        return


def prune(root: Path, retention_days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for day in root.glob("*"):
        try:
            d = datetime.strptime(day.name, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if d < cutoff:
            for f in day.glob("*"):
                f.unlink()
            day.rmdir()
            removed += 1
    return removed

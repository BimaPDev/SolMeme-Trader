"""A run killed mid-write must not hide later runs' events (it used to: shared hour file)."""

import gzip
import time

from solmeme.core.eventlog import EventLogWriter, list_files, read_events
from solmeme.core.events import SolPrice

TS = 1790841600.0  # 2026-10-01 08:00 UTC


def _write(root, start, n, usd):
    w = EventLogWriter(root)
    for i in range(n):
        w.write(SolPrice(ts=start + i, usd=usd))
    w.close()
    time.sleep(0.002)          # distinct session ids
    return w


def test_crashed_session_does_not_hide_later_sessions(tmp_path):
    first = _write(tmp_path, TS, 500, 150.0)
    (f,) = [p for p in list_files(tmp_path) if first.session in p.name]
    f.write_bytes(f.read_bytes()[: f.stat().st_size // 2])      # simulate kill -9 mid-write
    _write(tmp_path, TS + 1000, 100, 151.0)
    evs = list(read_events(tmp_path))
    assert sum(e.usd == 151.0 for e in evs) == 100              # second run fully readable
    assert 0 < sum(e.usd == 150.0 for e in evs) < 500           # first run partially recovered
    assert [e.ts for e in evs] == sorted(e.ts for e in evs)     # still time-ordered


def test_legacy_hour_file_still_read_first(tmp_path):
    day = tmp_path / "20261001"
    day.mkdir()
    with gzip.open(day / "08.jsonl.gz", "wt") as fh:
        fh.write(SolPrice(ts=TS, usd=1.0).model_dump_json() + "\n")
    _write(tmp_path, TS + 10, 3, 2.0)
    assert [e.usd for e in read_events(tmp_path)] == [1.0, 2.0, 2.0, 2.0]

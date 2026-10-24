"""Trials: strategies/trials/*.yaml run paper-only on their own wallet until promoted."""

import shutil

import pytest

from solmeme.ai.promoter import Promoter, active_specs, ensure_seeded
from solmeme.core.store import Store
from solmeme.sim.live import LiveSession
from solmeme.strategy.spec import load_spec

from .conftest import ROOT


@pytest.fixture()
def lab(settings, tmp_path):
    d = tmp_path / "strategies"
    (d / "trials").mkdir(parents=True)
    shutil.copy(ROOT / "strategies" / "migration_momentum.yaml", d)
    shutil.copy(ROOT / "strategies" / "trials" / "established_breakout.yaml", d / "trials")
    settings.strategies_dir = d
    return settings, Store(settings.db_path), d / "trials" / "established_breakout.yaml"


def _roles(store):
    return {sp.id: (sp.version, lbl) for sp, lbl in active_specs(store)}


def test_trial_files_are_seeded_as_trials(lab):
    settings, store, path = lab
    ensure_seeded(store, settings)
    roles = _roles(store)
    assert roles["migration_momentum"][1] == "champion"
    assert roles["established_breakout"] == (load_spec(path).version, "trial")
    ensure_seeded(store, settings)                       # idempotent
    assert len(store.versions("established_breakout")) == 1


def test_editing_a_trial_replaces_it_and_a_stopped_one_stays_stopped(lab):
    settings, store, path = lab
    ensure_seeded(store, settings)
    old = load_spec(path).version
    path.write_text(path.read_text().replace("value: 4, lo: 1", "value: 6, lo: 1"))
    ensure_seeded(store, settings)
    new = load_spec(path).version
    assert new != old and _roles(store)["established_breakout"] == (new, "trial")
    assert store.get_version("established_breakout", old)["status"] == "retired"
    Promoter(settings, store).kill_challenger("established_breakout", new)
    ensure_seeded(store, settings)
    assert "established_breakout" not in _roles(store)


def test_a_promoted_trial_ignores_its_file(lab):
    settings, store, path = lab
    ensure_seeded(store, settings)
    version = load_spec(path).version
    Promoter(settings, store).promote("established_breakout", version, "manual")
    path.write_text(path.read_text().replace("value: 4, lo: 1", "value: 6, lo: 1"))
    ensure_seeded(store, settings)
    assert _roles(store)["established_breakout"] == (version, "champion")


def test_trials_trade_their_own_wallet(lab, monkeypatch):
    settings, store, _ = lab
    monkeypatch.setattr("solmeme.sim.live.fetch_sol_usd", lambda: 125.0)
    settings.portfolio.starting_usd = 50.0
    settings.portfolio.shared_wallet = True
    ensure_seeded(store, settings)
    session = LiveSession(settings, None, active_specs(store), warmup_min=0, ai=False)
    by_id = {r.spec.id: r for r in session.engine.runners}
    assert session.engine.in_shared_wallet(by_id["migration_momentum"])
    trial = by_id["established_breakout"]
    assert not session.engine.in_shared_wallet(trial) and trial.portfolio.cash == pytest.approx(0.4)


def test_a_demoted_champion_stays_a_trial_after_reseeding(lab):
    settings, store, _ = lab
    ensure_seeded(store, settings)
    v = Promoter(settings, store).demote("migration_momentum", "losing")
    ensure_seeded(store, settings)                       # a restart must not promote it back
    assert _roles(store)["migration_momentum"] == (v, "trial")
    assert store.champion("migration_momentum") is None

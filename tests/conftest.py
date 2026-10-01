from __future__ import annotations

from pathlib import Path

import pytest

from solmeme.config import Settings
from solmeme.lab.synth import generate
from solmeme.strategy.spec import load_dir

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def events():
    return generate(n_tokens=160, duration_s=3600, seed=5)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings(data_dir=tmp_path / "data", strategies_dir=ROOT / "strategies")
    return s.resolve()


@pytest.fixture(scope="session")
def specs():
    return {s.id: s for s in load_dir(ROOT / "strategies")}

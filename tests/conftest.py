from pathlib import Path

import pytest

from china_stock_sim.config import load_settings

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "isolated_project"
    (root / "configs").mkdir(parents=True)
    # Fixtures start inactive regardless of this workstation's explicit production activation.
    defaults = (REPOSITORY / "configs/default.toml").read_text().replace('enabled = true', 'enabled = false')
    (root / "configs/default.toml").write_text(defaults)
    (root / "configs/execution_rules.toml").write_bytes((REPOSITORY / "configs/execution_rules.toml").read_bytes())
    return root


@pytest.fixture
def settings(project):
    return load_settings(project)

import json
from pathlib import Path

import pytest

from agy_pool import procs
from agy_pool.paths import TOKEN_FILE, get_paths
from agy_pool.store import Store


def write_token(path: Path, email: str, secret: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": secret, "auth_method": "consumer", "id_token_email": email}))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "gemini"
    monkeypatch.setenv("AGY_POOL_GEMINI_ROOT", str(root))
    monkeypatch.setenv("AGY_POOL_HOME", str(root / "agy-pool"))
    monkeypatch.setenv("AGY_POOL_BIN_DIR", str(tmp_path / "bin"))
    fake_agy = tmp_path / "real-bin" / "agy"
    fake_agy.parent.mkdir()
    fake_agy.write_text("#!/bin/sh\n")
    fake_agy.chmod(0o755)
    monkeypatch.setenv("AGY_BIN", str(fake_agy))
    # Never look at the real /proc in tests.
    monkeypatch.setattr(procs, "scan", lambda name, d: ([], []))
    return Store(get_paths())


@pytest.fixture
def live_token(env):
    return env.paths.gemini_root / "antigravity-cli" / TOKEN_FILE

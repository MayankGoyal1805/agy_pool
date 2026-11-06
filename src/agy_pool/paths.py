"""Filesystem locations. Every path can be overridden by env var (used by tests)."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

TOKEN_FILE = "antigravity-oauth-token"
DEFAULT_INSTANCE_DIR_NAME = "antigravity-cli"


@dataclass(frozen=True)
class Paths:
    gemini_root: Path  # ~/.gemini; agy resolves relative --app_data_dir against it
    store: Path  # account store + tool state
    bin_dir: Path  # where launchers are written

    @property
    def accounts(self) -> Path:
        return self.store / "accounts"

    @property
    def backups(self) -> Path:
        return self.store / "backups"

    @property
    def secrets(self) -> Path:
        return self.store / "secrets"

    @property
    def state_file(self) -> Path:
        return self.store / "state.json"

    @property
    def lock_file(self) -> Path:
        return self.store / ".lock"

    def resolve_app_dir(self, app_dir: str) -> Path:
        p = Path(app_dir).expanduser()
        return p if p.is_absolute() else self.gemini_root / p

    def app_dir_arg(self, path: Path) -> str:
        """Value for --app_data_dir: relative to ~/.gemini when possible (the form agy is known to accept)."""
        path = Path(path)
        try:
            return str(path.resolve().relative_to(self.gemini_root.resolve()))
        except ValueError:
            return str(path.resolve())


def get_paths() -> Paths:
    gemini_root = Path(os.environ.get("AGY_POOL_GEMINI_ROOT", Path.home() / ".gemini")).expanduser()
    store = Path(os.environ.get("AGY_POOL_HOME", gemini_root / "agy-pool")).expanduser()
    bin_dir = Path(os.environ.get("AGY_POOL_BIN_DIR", Path.home() / ".local" / "bin")).expanduser()
    return Paths(gemini_root=gemini_root, store=store, bin_dir=bin_dir)


def find_agy_binary() -> str:
    """The real agy binary (never one of our launchers)."""
    env = os.environ.get("AGY_BIN")
    if env:
        return env
    candidates = [Path.home() / ".local" / "bin" / "agy", Path.home() / ".gemini" / "bin" / "agy"]
    found = shutil.which("agy")
    if found:
        candidates.insert(0, Path(found))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    raise FileNotFoundError("agy binary not found; set AGY_BIN")

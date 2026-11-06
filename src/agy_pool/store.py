"""Account store and instance registry.

Layout (under Paths.store, default ~/.gemini/agy-pool):
    accounts/<label>/antigravity-oauth-token   one minimal agy data dir per account
    backups/                                   timestamped copies of tokens we overwrote
    secrets/<instance>.key                     API keys for api-key instances (0600)
    state.json                                 instance registry + active account per instance
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from . import tokens
from .paths import DEFAULT_INSTANCE_DIR_NAME, TOKEN_FILE, Paths

LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULT_INSTANCE = "flash"
AUTH_ACCOUNT = "account"
AUTH_API_KEY = "api-key"


class StoreError(Exception):
    pass


def validate_label(label: str, what: str = "label") -> str:
    label = label.strip().lower()
    if not LABEL_RE.match(label):
        raise StoreError(f"invalid {what} {label!r}: use a-z, 0-9, '-', '_' (max 32 chars)")
    return label


@dataclass
class Account:
    label: str
    dir: Path

    @property
    def token(self) -> Path:
        return self.dir / TOKEN_FILE

    @property
    def email(self) -> Optional[str]:
        return tokens.token_email(self.token)

    @property
    def has_token(self) -> bool:
        return self.token.is_file()


@dataclass
class Instance:
    name: str
    app_dir: str  # as passed to --app_data_dir (relative to ~/.gemini; agy rejects absolute paths)
    auth: str  # AUTH_ACCOUNT | AUTH_API_KEY
    account: Optional[str] = None  # label last switched to (AUTH_ACCOUNT only)
    base_url: Optional[str] = None  # GOOGLE_GEMINI_BASE_URL (AUTH_API_KEY only)

    def to_json(self) -> dict:
        d = {"app_dir": self.app_dir, "auth": self.auth, "account": self.account}
        if self.base_url:
            d["base_url"] = self.base_url
        return d


class Store:
    def __init__(self, paths: Paths):
        self.paths = paths

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            data = json.loads(self.paths.state_file.read_text())
        except (OSError, ValueError):
            data = {}
        insts = data.setdefault("instances", {})
        if DEFAULT_INSTANCE not in insts:
            insts[DEFAULT_INSTANCE] = Instance(DEFAULT_INSTANCE, DEFAULT_INSTANCE_DIR_NAME, AUTH_ACCOUNT).to_json()
        return data

    def _save(self, data: dict) -> None:
        tokens.atomic_write(self.paths.state_file, (json.dumps(data, indent=2) + "\n").encode())

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive lock for mutating operations (switch, import, rm...)."""
        self.paths.store.mkdir(parents=True, exist_ok=True)
        with open(self.paths.lock_file, "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError("another agy-pool operation is in progress")
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------- accounts
    def account(self, label: str) -> Account:
        return Account(validate_label(label), self.paths.accounts / validate_label(label))

    def get_account(self, label: str) -> Account:
        acc = self.account(label)
        if not acc.dir.is_dir():
            raise StoreError(f"no account named {acc.label!r} (see `agy-pool list`)")
        return acc

    def accounts(self) -> list[Account]:
        if not self.paths.accounts.is_dir():
            return []
        return [
            Account(p.name, p)
            for p in sorted(self.paths.accounts.iterdir())
            if p.is_dir() and LABEL_RE.match(p.name)
        ]

    def find_by_email(self, email: Optional[str]) -> Optional[Account]:
        if not email:
            return None
        matches = [a for a in self.accounts() if a.email and a.email.lower() == email.lower()]
        return matches[0] if matches else None

    def import_token(self, label: str, src_token: Path, overwrite: bool = False) -> Account:
        acc = self.account(label)
        src_token = Path(src_token)
        if not src_token.is_file():
            raise StoreError(f"no token at {src_token}")
        email = tokens.token_email(src_token)
        if not email:
            raise StoreError(f"{src_token} is not a readable agy token")
        other = self.find_by_email(email)
        if other and other.label != acc.label:
            raise StoreError(f"{email} is already stored as {other.label!r}")
        if acc.has_token and not overwrite:
            raise StoreError(f"account {acc.label!r} already exists ({acc.email}); use --force to overwrite")
        if acc.has_token:
            tokens.backup(acc.token, self.paths.backups, f"{acc.label}-{acc.email}")
        acc.dir.mkdir(parents=True, exist_ok=True)
        tokens.atomic_copy(src_token, acc.token)
        return acc

    def remove_account(self, label: str) -> Account:
        acc = self.get_account(label)
        for inst in self.instances():
            if inst.account == acc.label:
                raise StoreError(f"{acc.label!r} is active on instance {inst.name!r}; switch away first")
        if acc.has_token:
            tokens.backup(acc.token, self.paths.backups, f"removed-{acc.label}-{acc.email}")
        import shutil

        shutil.rmtree(acc.dir)
        return acc

    def rename_account(self, old: str, new: str) -> Account:
        acc = self.get_account(old)
        dst = self.account(new)
        if dst.dir.exists():
            raise StoreError(f"account {dst.label!r} already exists")
        acc.dir.rename(dst.dir)
        data = self._load()
        for cfg in data["instances"].values():
            if cfg.get("account") == acc.label:
                cfg["account"] = dst.label
        self._save(data)
        return dst

    # ------------------------------------------------------------ instances
    def instances(self) -> list[Instance]:
        data = self._load()
        return [Instance(name, **cfg) for name, cfg in sorted(data["instances"].items())]

    def get_instance(self, name: str) -> Instance:
        name = validate_label(name, "instance name")
        for inst in self.instances():
            if inst.name == name:
                return inst
        raise StoreError(f"no instance named {name!r} (see `agy-pool instance list`)")

    def save_instance(self, inst: Instance) -> None:
        data = self._load()
        data["instances"][inst.name] = inst.to_json()
        self._save(data)

    def delete_instance(self, name: str) -> None:
        data = self._load()
        data["instances"].pop(name, None)
        self._save(data)

    def instance_dir(self, inst: Instance) -> Path:
        return self.paths.resolve_app_dir(inst.app_dir)

    def instance_token(self, inst: Instance) -> Path:
        return self.instance_dir(inst) / TOKEN_FILE

    def api_key_file(self, inst: Instance) -> Path:
        return self.paths.secrets / f"{inst.name}.key"

    def cli_dir(self, inst: Instance) -> Path:
        """Per-instance settings/logs subdir (agy's --cli_dir_name) for api-key instances.

        Chats stay in the shared app dir; only settings.json, logs and cache live here, so
        `modelProvider: "gemini"` doesn't leak into the account-based instance."""
        return self.instance_dir(inst) / inst.name

    def seed_cli_settings(self, inst: Instance) -> Path:
        """Create/patch <app_dir>/<name>/settings.json: start from the shared settings.json
        (permissions, trusted workspaces, model) and force modelProvider=gemini."""
        dst = self.cli_dir(inst) / "settings.json"
        try:
            data = json.loads(dst.read_text())
        except (OSError, ValueError):
            try:
                data = json.loads((self.instance_dir(inst) / "settings.json").read_text())
            except (OSError, ValueError):
                data = {}
        data["modelProvider"] = "gemini"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tokens.atomic_write(dst, (json.dumps(data, indent=2) + "\n").encode())
        os.chmod(dst, 0o644)
        return dst

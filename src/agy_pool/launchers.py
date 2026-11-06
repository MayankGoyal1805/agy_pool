"""Launcher scripts (~/.local/bin/agy-<instance>) that pin an instance and mark its processes."""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .paths import Paths, find_agy_binary
from .procs import MARKER_ENV
from .store import AUTH_API_KEY, Instance, Store

HEADER = "# Managed by agy-pool"
API_KEY_SETTINGS = '{"modelProvider":"gemini"}'


def launcher_path(paths: Paths, inst: Instance) -> Path:
    return paths.bin_dir / f"agy-{inst.name}"


def render(store: Store, inst: Instance, agy_bin: str) -> str:
    q = shlex.quote
    lines = [
        "#!/bin/sh",
        f"{HEADER}: launcher for instance '{inst.name}'. Regenerate with `agy-pool instance launcher {inst.name}`.",
        f"export {MARKER_ENV}={q(inst.name)}",
    ]
    if inst.auth == AUTH_API_KEY:
        key_file = store.api_key_file(inst)
        lines += [
            f"GEMINI_API_KEY=\"$(cat {q(str(key_file))})\" || {{ echo 'agy-{inst.name}: missing API key file {key_file}' >&2; exit 1; }}",
            "export GEMINI_API_KEY",
            "unset GOOGLE_API_KEY  # agy prefers GOOGLE_API_KEY when both are set",
        ]
        if inst.base_url:
            lines.append(f"export GOOGLE_GEMINI_BASE_URL={q(inst.base_url)}")
        # API-key mode needs modelProvider=gemini. `acp` only reads it from settings.json, so it
        # gets its own settings subdir (--cli_dir_name, an acp-only flag); other commands take it
        # per session via --settings. Either way the shared settings.json is untouched.
        lines += [
            'if [ "${1:-}" = acp ]; then',
            "  shift",
            f'  exec {q(agy_bin)} --app_data_dir={q(inst.app_dir)} acp --cli_dir_name={q(inst.name)} "$@"',
            "fi",
            f"exec {q(agy_bin)} --app_data_dir={q(inst.app_dir)} --settings {q(API_KEY_SETTINGS)} \"$@\"",
        ]
        return "\n".join(lines) + "\n"
    lines.append(f'exec {q(agy_bin)} --app_data_dir={q(inst.app_dir)} "$@"')
    return "\n".join(lines) + "\n"


def write(store: Store, inst: Instance, force: bool = False) -> Path:
    path = launcher_path(store.paths, inst)
    if path.exists() and HEADER not in path.read_text(errors="replace") and not force:
        raise FileExistsError(f"{path} exists and was not created by agy-pool (use --force)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(store, inst, find_agy_binary()))
    os.chmod(path, 0o755)
    return path


def remove(store: Store, inst: Instance) -> None:
    path = launcher_path(store.paths, inst)
    if path.exists() and HEADER in path.read_text(errors="replace"):
        path.unlink()

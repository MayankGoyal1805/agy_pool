"""Core account pool manager, persistence, round-robin allocation, and instance management."""

from __future__ import annotations

import datetime
import json
import logging
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger("agy_pool.manager")

AGY_BIN = os.environ.get("AGY_BIN", "/home/ubuntu/.local/bin/agy")
GEMINI_ROOT = Path(os.environ.get("GEMINI_ROOT", Path.home() / ".gemini"))
POOL_ROOT = Path(os.environ.get("AGY_POOL_ROOT", GEMINI_ROOT / "account_pool"))
ACCOUNTS_DIR = POOL_ROOT / "accounts"
STATE_FILE = POOL_ROOT / "state.json"
TMUX_SESSION_NAME = "subagents"

PRIMARY_CLI_DIR = Path(os.environ.get("AGY_CLI_DIR", GEMINI_ROOT / "antigravity-cli"))
INSTANCES_DIR = Path(os.environ.get("AGY_INSTANCES_DIR", GEMINI_ROOT / "instances"))
LOCAL_BIN_DIR = Path.home() / ".local" / "bin"

SHARED_DATA_TARGETS = [
    "mcp",
    "mcp_config.json",
    "scratch",
    "builtin",
]

_pool_lock = threading.Lock()


def ensure_dirs() -> None:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
    INSTANCES_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_BIN_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        with open(STATE_FILE, "w") as f:
            json.dump({"rr_index": 0, "exhausted": {}, "custom_order": []}, f, indent=2)


def load_state() -> dict:
    ensure_dirs()
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"rr_index": 0, "exhausted": {}, "custom_order": []}


def save_state(state: dict) -> None:
    ensure_dirs()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_token_email(token_file: Path) -> str:
    if not token_file.exists():
        return "Not Logged In"
    try:
        with open(token_file, "r") as f:
            data = json.load(f)
            return data.get("id_token_email") or data.get("email") or "Authenticated"
    except Exception:
        return "Unknown"


def _sort_key(name: str):
    """Sort numerically if possible, else alphabetically."""
    if name.isdigit():
        return (0, int(name))
    if name.startswith("acc") and name[3:].isdigit():
        return (0, int(name[3:]))
    return (1, name)


def list_accounts(include_symlinks: bool = False) -> list[dict]:
    """List all accounts in the pool sorted numerically."""
    ensure_dirs()
    accounts = []
    if not ACCOUNTS_DIR.exists():
        return accounts

    entries = sorted(ACCOUNTS_DIR.iterdir(), key=lambda p: _sort_key(p.name))
    for p in entries:
        if not include_symlinks and p.is_symlink():
            continue
        if p.is_dir():
            token_file = p / "antigravity-oauth-token"
            email = get_token_email(token_file)
            has_token = token_file.exists()
            accounts.append({
                "id": p.name,
                "path": str(p.resolve()),
                "rel_dir": f"account_pool/accounts/{p.name}",
                "email": email,
                "authenticated": has_token,
            })
    return accounts


def get_next_available_slot() -> str:
    """Find next integer slot ID (e.g. 1, 2, 3... 6)."""
    accounts = list_accounts(include_symlinks=False)
    numeric_ids = []
    for acc in accounts:
        aid = acc["id"]
        if aid.isdigit():
            numeric_ids.append(int(aid))
        elif aid.startswith("acc") and aid[3:].isdigit():
            numeric_ids.append(int(aid[3:]))

    if not numeric_ids:
        return "1"
    return str(max(numeric_ids) + 1)


def resolve_slot_id(slot_input: Optional[str]) -> str:
    """Normalize input (e.g. 'acc3' -> '3', None -> next available)."""
    if not slot_input or not str(slot_input).strip():
        return get_next_available_slot()
    s = str(slot_input).strip().lower()
    if s.startswith("acc") and s[3:].isdigit():
        return s[3:]
    return s


# =====================================================================
# Programmatic Allocator API (Round-Robin & Quota Failover)
# =====================================================================

class AccountPoolManager:
    """Thread-safe round-robin account allocator with quota failover."""

    @staticmethod
    def get_next_healthy_account() -> Optional[dict]:
        with _pool_lock:
            state = load_state()
            accounts = [a for a in list_accounts() if a["authenticated"]]
            if not accounts:
                return None

            exhausted = state.get("exhausted", {})
            now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

            # Clean expired exhaustion entries
            active_exhausted = {}
            for aid, exp_ts in exhausted.items():
                if exp_ts > now_ts:
                    active_exhausted[aid] = exp_ts
            if len(active_exhausted) != len(exhausted):
                state["exhausted"] = active_exhausted
                save_state(state)

            rr_idx = state.get("rr_index", 0)

            # Round robin across current accounts
            for offset in range(len(accounts)):
                cand_idx = (rr_idx + offset) % len(accounts)
                cand = accounts[cand_idx]
                if cand["id"] not in active_exhausted:
                    # Advance pointer for next subagent
                    state["rr_index"] = (cand_idx + 1) % len(accounts)
                    save_state(state)
                    return cand

            # Fallback if all marked exhausted: return least recently used
            cand = accounts[rr_idx % len(accounts)]
            state["rr_index"] = (rr_idx + 1) % len(accounts)
            save_state(state)
            return cand

    @staticmethod
    def mark_account_exhausted(acc_id: str, cooldown_hours: float = 3.0) -> None:
        with _pool_lock:
            state = load_state()
            exhausted = state.get("exhausted", {})
            expiry = datetime.datetime.now(datetime.timezone.utc).timestamp() + (cooldown_hours * 3600)
            exhausted[str(acc_id)] = expiry
            state["exhausted"] = exhausted
            save_state(state)
            exp_iso = datetime.datetime.fromtimestamp(expiry, datetime.timezone.utc).isoformat()
            logger.warning("Account %s marked exhausted until %s", acc_id, exp_iso)


# =====================================================================
# Instance Management (Separation of Profiles on Disk)
# =====================================================================

def get_instance_dir(instance_name: str) -> Path:
    """Resolve directory path for a named Antigravity instance."""
    name = instance_name.strip().lower()
    if name in ("primary", "default", "cli"):
        return PRIMARY_CLI_DIR
    return INSTANCES_DIR / name


def get_instance_rel_dir(instance_name: str) -> str:
    """Resolve relative path to GEMINI_ROOT for --app_data_dir."""
    name = instance_name.strip().lower()
    if name in ("primary", "default", "cli"):
        return "antigravity-cli"
    return f"instances/{name}"


def _create_wrapper_binary(instance_name: str, rel_dir: str) -> None:
    """Generate executable helper script in ~/.local/bin/agy-<name>."""
    if instance_name in ("primary", "default", "cli"):
        return

    wrapper_path = LOCAL_BIN_DIR / f"agy-{instance_name}"
    script_content = (
        "#!/bin/bash\n"
        f"# Auto-generated launcher for Antigravity instance '{instance_name}'\n"
        f"exec {AGY_BIN} --app_data_dir=\"{rel_dir}\" \"$@\"\n"
    )
    try:
        wrapper_path.write_text(script_content)
        wrapper_path.chmod(0o755)
    except Exception as exc:
        logger.warning("Could not write wrapper script %s: %s", wrapper_path, exc)


def _remove_wrapper_binary(instance_name: str) -> None:
    wrapper_path = LOCAL_BIN_DIR / f"agy-{instance_name}"
    if wrapper_path.exists():
        try:
            wrapper_path.unlink()
        except Exception:
            pass


def list_instances() -> list[dict[str, Any]]:
    """Scan and list all Antigravity instances and profiles on the machine."""
    ensure_dirs()
    instances = []

    # Map slot tokens to slot ids
    accounts = list_accounts(include_symlinks=False)
    email_to_slot = {acc["email"]: acc["id"] for acc in accounts if acc["authenticated"]}

    # 1. Primary interactive CLI instance
    PRIMARY_CLI_DIR.mkdir(parents=True, exist_ok=True)
    prim_token = PRIMARY_CLI_DIR / "antigravity-oauth-token"
    prim_email = get_token_email(prim_token)
    instances.append({
        "name": "primary",
        "is_default": True,
        "path": str(PRIMARY_CLI_DIR),
        "rel_dir": "antigravity-cli",
        "email": prim_email,
        "authenticated": prim_token.exists(),
        "matching_slot": email_to_slot.get(prim_email),
        "shared_data": False,
        "cli_cmd": "agy",
    })

    # 2. Custom instances under ~/.gemini/instances
    if INSTANCES_DIR.exists():
        for p in sorted(INSTANCES_DIR.iterdir()):
            if p.is_dir():
                tok = p / "antigravity-oauth-token"
                em = get_token_email(tok)
                # Check if it shares data via symlinked brain
                is_shared = (p / "brain").is_symlink()
                instances.append({
                    "name": p.name,
                    "is_default": False,
                    "path": str(p),
                    "rel_dir": f"instances/{p.name}",
                    "email": em,
                    "authenticated": tok.exists(),
                    "matching_slot": email_to_slot.get(em),
                    "shared_data": is_shared,
                    "cli_cmd": f"agy-{p.name}",
                })

    return instances


def switch_instance_account(instance_name: str, slot_id: str) -> tuple[bool, str]:
    """Switch an instance's active account to use a slot's credentials."""
    slot_id = resolve_slot_id(slot_id)
    slot_token = ACCOUNTS_DIR / slot_id / "antigravity-oauth-token"
    if not slot_token.exists():
        return False, f"Slot '{slot_id}' does not have an active token ({slot_token} not found)"

    target_dir = get_instance_dir(instance_name)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_token = target_dir / "antigravity-oauth-token"

    # Backup existing token
    if target_token.exists():
        backup_file = target_dir / "antigravity-oauth-token.bak"
        try:
            shutil.copy2(target_token, backup_file)
        except Exception:
            pass

    try:
        shutil.copy2(slot_token, target_token)
        new_email = get_token_email(target_token)
        return True, f"Instance '{instance_name}' switched to Slot {slot_id} ({new_email})"
    except Exception as exc:
        return False, f"Failed to switch token: {exc}"


def create_instance(
    instance_name: str,
    slot_id: Optional[str] = None,
    custom_path: Optional[Path] = None,
    shared_data: bool = True,
    source_instance: str = "primary",
) -> tuple[bool, str]:
    """Create a new Antigravity instance profile on disk.

    Supports:
    - Custom directory path
    - Shared data directory (symlinked brain, conversations, etc. for dual remote views)
    - Automatic wrapper script generation (~/.local/bin/agy-<name>)
    """
    name = instance_name.strip().lower()
    if name in ("primary", "default", "cli"):
        return False, "Cannot recreate primary CLI instance"

    target_dir = Path(custom_path).resolve() if custom_path else (INSTANCES_DIR / name)
    if target_dir.exists() and any(target_dir.iterdir()):
        return False, f"Instance '{name}' already exists at {target_dir}"

    target_dir.mkdir(parents=True, exist_ok=True)

    # 1. Unique device installation_id for Google Remote View
    inst_id_file = target_dir / "installation_id"
    if not inst_id_file.exists():
        inst_id_file.write_text(str(uuid.uuid4()))

    # 2. Shared data directory symlinks (brain, conversations, etc.)
    source_dir = get_instance_dir(source_instance)
    if shared_data and source_dir.exists():
        for item_name in SHARED_DATA_TARGETS:
            src_item = source_dir / item_name
            dst_item = target_dir / item_name
            if src_item.exists() and not dst_item.exists():
                try:
                    dst_item.symlink_to(src_item)
                except Exception as exc:
                    logger.warning("Could not symlink %s: %s", item_name, exc)

    # 3. Account assignment if requested
    if slot_id:
        switch_instance_account(name, slot_id)

    # 4. Generate CLI wrapper binary in ~/.local/bin/agy-<name>
    rel_dir = f"instances/{name}" if not custom_path else str(target_dir)
    _create_wrapper_binary(name, rel_dir)

    shared_msg = " [shared data with primary]" if shared_data else " [isolated data]"
    return True, f"Created instance '{name}' at {target_dir}{shared_msg} (Command: agy-{name})"


def remove_instance(instance_name: str) -> tuple[bool, str]:
    """Delete a custom Antigravity instance profile safely."""
    name = instance_name.strip().lower()
    if name in ("primary", "default", "cli"):
        return False, "Cannot delete primary CLI instance"

    target_dir = INSTANCES_DIR / name
    if not target_dir.exists():
        return False, f"Instance '{name}' does not exist"

    # Remove launcher binary
    _remove_wrapper_binary(name)

    # Safely unlink all symlinks first to guarantee source data is never touched
    try:
        for p in target_dir.iterdir():
            if p.is_symlink():
                p.unlink()
        shutil.rmtree(target_dir)
        return True, f"Deleted instance '{name}' ({target_dir})"
    except Exception as exc:
        return False, f"Failed to remove instance: {exc}"


def import_primary_cli_account(slot_id: Optional[str] = None) -> tuple[bool, str]:
    """Import the current primary CLI account token into a pool slot."""
    src_token = PRIMARY_CLI_DIR / "antigravity-oauth-token"
    if not src_token.exists():
        return False, "Primary CLI has no active token to import"

    if not slot_id:
        slot_id = get_next_available_slot()
    else:
        slot_id = resolve_slot_id(slot_id)

    target_dir = ACCOUNTS_DIR / slot_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target_token = target_dir / "antigravity-oauth-token"

    try:
        shutil.copy2(src_token, target_token)
        email = get_token_email(target_token)
        return True, f"Imported primary CLI token into Slot {slot_id} ({email})"
    except Exception as exc:
        return False, f"Failed to import token: {exc}"

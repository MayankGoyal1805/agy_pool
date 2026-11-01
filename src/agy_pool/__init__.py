"""Antigravity Multi-Account Pool & Live Tmux Grid Manager."""

from .manager import (
    ACCOUNTS_DIR,
    AGY_BIN,
    POOL_ROOT,
    STATE_FILE,
    AccountPoolManager,
    ensure_dirs,
    get_next_available_slot,
    get_token_email,
    list_accounts,
    load_state,
    resolve_slot_id,
    save_state,
)
from .quota import fetch_all_usage, fetch_single_account_usage
from .tmux import TMUX_SESSION_NAME, TmuxSubagentGrid

__version__ = "0.2.0"

__all__ = [
    "AccountPoolManager",
    "TmuxSubagentGrid",
    "list_accounts",
    "fetch_all_usage",
    "fetch_single_account_usage",
    "ensure_dirs",
    "load_state",
    "save_state",
    "get_next_available_slot",
    "resolve_slot_id",
    "get_token_email",
    "ACCOUNTS_DIR",
    "POOL_ROOT",
    "STATE_FILE",
    "AGY_BIN",
    "TMUX_SESSION_NAME",
]

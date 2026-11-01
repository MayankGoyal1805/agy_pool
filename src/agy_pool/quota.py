"""Dynamic live Google quota inspection and usage parsing."""

from __future__ import annotations

import concurrent.futures
import logging
import subprocess
from typing import Any, Optional

from .manager import AGY_BIN, list_accounts

logger = logging.getLogger("agy_pool.quota")


def parse_raw_usage_text(raw_text: str) -> dict[str, dict[str, Any]]:
    """Dynamically parse quota output from `agy -p /usage` without hardcoding model groups."""
    pools: dict[str, dict[str, Any]] = {}
    for line in raw_text.strip().splitlines():
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) >= 3:
            grp = parts[0]
            limit_name = parts[1]
            pct_str = parts[2].rstrip("%")
            try:
                pct_val = int(pct_str)
            except ValueError:
                pct_val = None

            reset_time = parts[3] if len(parts) >= 4 else None

            lower_lim = limit_name.lower()
            if "five" in lower_lim or "5" in lower_lim:
                kind = "5-hr"
            elif "week" in lower_lim:
                kind = "weekly"
            else:
                kind = limit_name

            if grp not in pools:
                pools[grp] = {}
            pools[grp][kind] = {
                "pct": pct_val,
                "reset": reset_time,
                "raw_name": limit_name,
            }
    return pools


def fetch_single_account_usage(acc: dict) -> dict[str, Any]:
    """Fetch live Google quota for a single account via `agy -p /usage`."""
    if not acc.get("authenticated"):
        return {
            "acc": acc,
            "error": "Not authenticated",
            "pools": {},
        }

    cmd = [
        AGY_BIN,
        f"--app_data_dir={acc['rel_dir']}",
        "-p", "/usage",
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout).strip()[:100]
            return {
                "acc": acc,
                "error": err_msg or f"Exited with code {proc.returncode}",
                "pools": {},
            }

        pools = parse_raw_usage_text(proc.stdout)
        return {
            "acc": acc,
            "error": None,
            "pools": pools,
        }
    except subprocess.TimeoutExpired:
        return {
            "acc": acc,
            "error": "Timed out after 25s",
            "pools": {},
        }
    except Exception as exc:
        return {
            "acc": acc,
            "error": str(exc),
            "pools": {},
        }


def fetch_all_usage(accounts: Optional[list[dict]] = None) -> list[dict[str, Any]]:
    """Query live quota for all accounts in parallel."""
    if accounts is None:
        accounts = list_accounts(include_symlinks=False)
    if not accounts:
        return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(accounts), 1)) as executor:
        results = list(executor.map(fetch_single_account_usage, accounts))
    return results

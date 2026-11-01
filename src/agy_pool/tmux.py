"""Tmux multi-pane grid visualizer for parallel Antigravity subagents."""

from __future__ import annotations

import datetime
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("agy_pool.tmux")

TMUX_SESSION_NAME = "subagents"
SUBAGENTS_LOG_DIR = Path("/tmp/hermes_subagents")

_tmux_lock = threading.Lock()


class TmuxSubagentGrid:
    """Manages multi-pane subagent terminal split in tmux session 'subagents'."""

    @staticmethod
    def is_tmux_available() -> bool:
        return shutil.which("tmux") is not None

    @classmethod
    def ensure_grid_session(cls) -> bool:
        if not cls.is_tmux_available():
            return False
        try:
            res = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION_NAME], capture_output=True)
            if res.returncode != 0:
                subprocess.run(
                    ["tmux", "new-session", "-d", "-s", TMUX_SESSION_NAME, "-n", "grid"],
                    check=False,
                )
                subprocess.run(["tmux", "select-pane", "-t", f"{TMUX_SESSION_NAME}:grid.0", "-T", "__placeholder__"], check=False)
                subprocess.run(["tmux", "set-option", "-t", TMUX_SESSION_NAME, "pane-border-status", "top"], check=False)
                subprocess.run(["tmux", "set-option", "-t", TMUX_SESSION_NAME, "pane-border-format", " [#{pane_index}] #{pane_title} "], check=False)
                subprocess.run(["tmux", "set-option", "-t", TMUX_SESSION_NAME, "mouse", "on"], check=False)
            return True
        except Exception as exc:
            logger.warning("Failed to setup tmux session %s: %s", TMUX_SESSION_NAME, exc)
            return False

    @classmethod
    def allocate_pane(
        cls,
        subagent_id: str,
        role: str,
        account_id: str,
        email: str,
        cwd: str,
        goal: str,
    ) -> Tuple[Optional[str], Path]:
        """Creates a dedicated pane in tmux 'subagents:grid' and sets up its log file."""
        SUBAGENTS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = SUBAGENTS_LOG_DIR / f"{subagent_id}.log"

        cls._init_log_file(log_file, subagent_id, role, account_id, email, cwd, goal)

        with _tmux_lock:
            if not cls.ensure_grid_session():
                return None, log_file

            try:
                pane_title = f"{role} ({account_id}) [RUNNING]"

                res = subprocess.run(
                    ["tmux", "list-panes", "-t", f"{TMUX_SESSION_NAME}:grid", "-F", "#{pane_id}:#{pane_title}"],
                    capture_output=True,
                    text=True,
                )
                panes = [line.strip().split(":", 1) for line in res.stdout.strip().splitlines() if line.strip()]

                # Prune if too many panes (> 8)
                if len(panes) >= 8:
                    oldest_pane_id = panes[0][0]
                    subprocess.run(["tmux", "kill-pane", "-t", oldest_pane_id], capture_output=True)

                # Reuse placeholder if sole pane
                if len(panes) == 1 and (panes[0][1] == "__placeholder__" or "instance" in panes[0][1]):
                    target_pane = panes[0][0]
                    subprocess.run(["tmux", "respawn-pane", "-k", "-t", target_pane, f"tail -n +1 -f {log_file}"], check=False)
                    pane_id = target_pane
                else:
                    split_res = subprocess.run(
                        ["tmux", "split-window", "-t", f"{TMUX_SESSION_NAME}:grid", "-d", "-P", "-F", "#{pane_id}", f"tail -n +1 -f {log_file}"],
                        capture_output=True,
                        text=True,
                    )
                    pane_id = split_res.stdout.strip() or None

                if pane_id:
                    subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T", pane_title], capture_output=True)
                    subprocess.run(["tmux", "select-layout", "-t", f"{TMUX_SESSION_NAME}:grid", "tiled"], capture_output=True)

                return pane_id, log_file
            except Exception as exc:
                logger.warning("Failed to allocate tmux pane: %s", exc)
                return None, log_file

    @classmethod
    def update_pane_status(cls, pane_id: Optional[str], role: str, account_id: str, status: str) -> None:
        if not pane_id or not cls.is_tmux_available():
            return
        try:
            title = f"{role} ({account_id}) [{status}]"
            subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T", title], capture_output=True)
        except Exception:
            pass

    @staticmethod
    def _init_log_file(log_file: Path, subagent_id: str, role: str, account_id: str, email: str, cwd: str, goal: str) -> None:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        trunc_goal = goal.strip().replace("\n", " ")
        if len(trunc_goal) > 90:
            trunc_goal = trunc_goal[:87] + "..."

        banner = [
            "\033[1;36m┌─────────────────────────────────────────────────────────────────────────────┐\033[0m",
            f"\033[1;36m│\033[0m \033[1;37mSUBAGENT:\033[0m  \033[1;33m{role:<15}\033[0m \033[90m[{subagent_id}]\033[0m",
            f"\033[1;36m│\033[0m \033[1;37mACCOUNT:\033[0m   \033[1;32m{account_id:<6}\033[0m \033[90m({email})\033[0m",
            f"\033[1;36m│\033[0m \033[1;37mCWD:\033[0m       \033[34m{cwd}\033[0m",
            f"\033[1;36m│\033[0m \033[1;37mSTARTED:\033[0m   \033[90m{now}\033[0m",
            f"\033[1;36m│\033[0m \033[1;37mGOAL:\033[0m      {trunc_goal}",
            "\033[1;36m├─────────────────────────────────────────────────────────────────────────────┤\033[0m",
            "\033[1;33m[*] Spawning Antigravity CLI process (gemini-3.8-flash-high)...\033[0m",
            "",
        ]
        with open(log_file, "w") as f:
            f.write("\n".join(banner) + "\n")

    @staticmethod
    def append_log(log_file: Path, text: str) -> None:
        try:
            with open(log_file, "a") as f:
                f.write(text + "\n")
        except Exception:
            pass

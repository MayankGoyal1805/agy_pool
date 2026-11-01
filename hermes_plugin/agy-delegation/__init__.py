"""Antigravity Delegation Plugin for Hermes.

Routes Hermes subagent delegation (`delegate_task`) directly to the official Google
Antigravity CLI (`agy`) using `gemini-3.8-flash-high`.
Capabilities:
- Standalone: Works seamlessly with single-account `agy` out of the box.
- Multi-Account Enhanced: When `agy-pool` is installed, automatically unlocks multi-account
  round-robin, live tmux multi-pane split grid, and automatic quota failover.
- Shared Workspace: All subagents run with the main Hermes agent's workspace CWD.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Attempt to load agy_pool if available
sys.path.insert(0, "/home/ubuntu/repos/agy_pool/src")
sys.path.insert(0, "/home/ubuntu/.gemini")

try:
    from agy_pool.manager import AccountPoolManager
    from agy_pool.tmux import TmuxSubagentGrid
except ImportError:
    try:
        from account_pool.manager import AccountPoolManager, TmuxSubagentGrid
    except ImportError:
        AccountPoolManager = None
        TmuxSubagentGrid = None

logger = logging.getLogger("hermes.plugin.agy_delegation")

AGY_BINARY_PATH = "/home/ubuntu/.local/bin/agy"


def _resolve_agent_cwd(parent_agent: Any, explicit_cwd: Optional[str] = None) -> str:
    """Resolve the true working directory of the main Hermes agent."""
    if explicit_cwd and os.path.isdir(explicit_cwd):
        return str(Path(explicit_cwd).resolve())

    # 1. Check parent_agent session DB row
    session_id = getattr(parent_agent, "session_id", None)
    if session_id:
        try:
            from hermes_state_registry import acquire
            db = acquire()
            row = db.get_session(session_id)
            if row:
                cwd_val = row.get("cwd") if isinstance(row, dict) else getattr(row, "cwd", None)
                if cwd_val and os.path.isdir(str(cwd_val)):
                    return str(Path(cwd_val).resolve())
        except Exception:
            pass

    # 2. Check Hermes runtime_cwd module
    try:
        from agent.runtime_cwd import resolve_agent_cwd
        p = resolve_agent_cwd()
        if p and p.is_dir() and str(p) != "/home/ubuntu/.hermes":
            return str(p.resolve())
    except Exception:
        pass

    # 3. Check parent_agent instance attributes
    candidates = [
        getattr(parent_agent, "session_cwd", None),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None),
        getattr(parent_agent, "cwd", None),
    ]

    for cand in candidates:
        if cand and isinstance(cand, (str, Path)) and os.path.isdir(str(cand)):
            return str(Path(cand).resolve())

    return os.getcwd()


class AntigravityChildAgent:
    """Subagent child agent that executes turns via the Google Antigravity CLI."""

    def __init__(
        self,
        task_index: int,
        goal: str,
        context: Optional[str] = None,
        model: Optional[str] = None,
        role: str = "leaf",
        parent_agent: Any = None,
        subagent_id: Optional[str] = None,
        tool_progress_callback: Any = None,
        ephemeral_system_prompt: Optional[str] = None,
        cwd: Optional[str] = None,
    ):
        self.task_index = task_index
        self.goal = goal
        self.context = context or ""
        self.model = model or "gemini-3.8-flash-high"
        self.provider = "antigravity"
        self.requested_provider = "antigravity"
        self._delegate_role = role
        self._delegate_output_schema = None
        self._delegate_images = None
        self._delegate_depth = (getattr(parent_agent, "_delegate_depth", 0) + 1) if parent_agent else 1
        self._subagent_id = subagent_id or f"sa-{task_index}-{uuid.uuid4().hex[:8]}"
        self.session_id = self._subagent_id
        self._parent_subagent_id = getattr(parent_agent, "_subagent_id", None) if parent_agent else None
        self._parent_session_id = getattr(parent_agent, "session_id", None) if parent_agent else None
        self.tool_progress_callback = tool_progress_callback
        self.ephemeral_system_prompt = ephemeral_system_prompt or ""
        self._delegate_saved_tool_names = []
        self._delegate_route = {
            "backend": "antigravity",
            "provider": "antigravity",
            "model": self.model,
            "label": f"agy-{role}",
        }

        # Accounting and runtime state
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_estimated_cost_usd = 0.0
        self.session_cost_status = "included"
        self.service_tier = None
        self.reasoning_config = None
        self.request_overrides = {}
        self.base_url = None
        self.tools = []
        self.valid_tool_names = set()
        self.enabled_toolsets = []
        self._fallback_activated = False
        self._interrupt_requested = False
        self._active_proc: Optional[subprocess.Popen] = None
        self.agy_conversation_id: Optional[str] = None
        self.cwd = _resolve_agent_cwd(parent_agent, cwd)

        # Tmux & Account allocation state
        self.allocated_account: Optional[dict] = None
        self.tmux_pane_id: Optional[str] = None
        self.subagent_log_file: Optional[Path] = None

    def close(self) -> None:
        if self._active_proc and self._active_proc.poll() is None:
            try:
                self._active_proc.terminate()
            except Exception:
                pass

    def interrupt(self, reason: str = "parent delegation ended") -> None:
        self._interrupt_requested = True
        self.close()

    def get_activity_summary(self) -> dict[str, Any]:
        acc_label = self.allocated_account.get("id") if self.allocated_account else "default"
        return {
            "last_activity_desc": f"executing Antigravity CLI ({self.model}) on {acc_label}",
            "running": self._active_proc is not None and self._active_proc.poll() is None,
        }

    def _allocate_resources(self):
        """Lease next healthy account from pool and allocate a live tmux pane if available."""
        if AccountPoolManager:
            try:
                self.allocated_account = AccountPoolManager.get_next_healthy_account()
            except Exception:
                self.allocated_account = None

        acc_id = self.allocated_account.get("id", "default") if self.allocated_account else "default"
        email = self.allocated_account.get("email", "primary") if self.allocated_account else "primary"

        if TmuxSubagentGrid:
            try:
                self.tmux_pane_id, self.subagent_log_file = TmuxSubagentGrid.allocate_pane(
                    subagent_id=self._subagent_id,
                    role=self._delegate_role,
                    account_id=acc_id,
                    email=email,
                    cwd=self.cwd,
                    goal=self.goal,
                )
            except Exception:
                pass

    def _is_quota_error(self, err_text: str) -> bool:
        """Detect whether error is due to Google quota or rate limit exhaustion."""
        keywords = [
            "429",
            "RESOURCE_EXHAUSTED",
            "PRIMARY_QUOTA",
            "quota exceeded",
            "rate limit",
            "Rate limit exceeded",
            "Too Many Requests",
        ]
        return any(k.lower() in err_text.lower() for k in keywords)

    def run_conversation(
        self,
        user_message: Any,
        task_id: Optional[str] = None,
        stream_callback: Optional[Any] = None,
        **kwargs,
    ) -> dict[str, Any]:
        """Execute turn using agy CLI binary with auto account rotation and tmux streaming."""
        prompt_text = str(user_message).strip()
        if self.context and self.context not in prompt_text:
            prompt_text = f"Context:\n{self.context}\n\nTask Goal:\n{prompt_text}"

        if not self.allocated_account:
            self._allocate_resources()

        max_failover_attempts = 3 if self.allocated_account else 1
        attempt = 0

        while attempt < max_failover_attempts:
            attempt += 1
            acc_id = self.allocated_account.get("id", "default") if self.allocated_account else "default"
            acc_email = self.allocated_account.get("email", "") if self.allocated_account else ""

            cmd = [
                AGY_BINARY_PATH,
                "--model", self.model,
                "--output-format", "json",
                "--dangerously-skip-permissions",
            ]

            if self.allocated_account and "rel_dir" in self.allocated_account:
                cmd.append(f"--app_data_dir={self.allocated_account['rel_dir']}")

            project_name = Path(self.cwd).name or "hermes-subagents"
            cmd.extend(["--project", project_name, "--add-dir", self.cwd])

            if self.agy_conversation_id:
                cmd.extend(["--conversation", self.agy_conversation_id])

            logger.info(
                "[AntigravitySubagent-%d] Running on account=%s (%s), role=%s, cwd=%s, attempt=%d",
                self.task_index, acc_id, acc_email, self._delegate_role, self.cwd, attempt,
            )

            if self.subagent_log_file and TmuxSubagentGrid:
                TmuxSubagentGrid.append_log(
                    self.subagent_log_file,
                    f"\033[90m[{time.strftime('%H:%M:%S')}]\033[0m Invoking turn (attempt {attempt}) on slot \033[1;32m{acc_id}\033[0m ({acc_email})...",
                )

            start_time = time.monotonic()
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=self.cwd,
            )
            self._active_proc = proc

            stdout, stderr = proc.communicate(input=prompt_text)
            duration = round(time.monotonic() - start_time, 2)
            self._active_proc = None

            # Handle errors & quota exhaustion
            if proc.returncode != 0:
                err_msg = stderr.strip() or f"agy exited with code {proc.returncode}"

                if self._is_quota_error(err_msg) and AccountPoolManager and self.allocated_account and attempt < max_failover_attempts:
                    logger.warning(
                        "[AntigravitySubagent-%d] Quota exhausted on account %s! Rotating to next account...",
                        self.task_index, acc_id,
                    )
                    if self.subagent_log_file and TmuxSubagentGrid:
                        TmuxSubagentGrid.append_log(
                            self.subagent_log_file,
                            f"\033[1;31m[!] Quota exhausted on slot {acc_id}!\033[0m Rotating to next account...",
                        )
                        TmuxSubagentGrid.update_pane_status(self.tmux_pane_id, self._delegate_role, acc_id, "FAILOVER")

                    AccountPoolManager.mark_account_exhausted(acc_id, cooldown_hours=3.0)
                    self.allocated_account = AccountPoolManager.get_next_healthy_account()
                    continue

                # Unrecoverable error
                logger.error("[AntigravitySubagent-%d] agy failed: %s", self.task_index, err_msg)
                if self.subagent_log_file and TmuxSubagentGrid:
                    TmuxSubagentGrid.append_log(self.subagent_log_file, f"\033[1;31m[ERROR]\033[0m {err_msg}")
                    TmuxSubagentGrid.update_pane_status(self.tmux_pane_id, self._delegate_role, acc_id, "✗ FAILED")

                return {
                    "final_response": f"Antigravity CLI execution error: {err_msg}",
                    "completed": False,
                    "failed": True,
                    "error": err_msg,
                    "api_calls": 1,
                    "messages": [
                        {"role": "user", "content": prompt_text},
                        {"role": "assistant", "content": err_msg},
                    ],
                }

            # Parse response
            try:
                data = json.loads(stdout.strip())
                resp_text = data.get("response", "").strip()
                self.agy_conversation_id = data.get("conversation_id")
                if self.agy_conversation_id:
                    self.session_id = self.agy_conversation_id

                usage = data.get("usage", {})
                in_tok = usage.get("input_tokens", 0)
                out_tok = usage.get("output_tokens", 0)
                think_tok = usage.get("thinking_tokens", 0)
                self.session_prompt_tokens += in_tok
                self.session_completion_tokens += out_tok
                self.session_reasoning_tokens += think_tok
                num_turns = data.get("num_turns", 1)
                status_ok = data.get("status") == "SUCCESS"

                logger.info(
                    "[AntigravitySubagent-%d] Success on slot %s in %.1fs (cid=%s, in=%d, out=%d)",
                    self.task_index, acc_id, duration, self.agy_conversation_id, in_tok, out_tok,
                )

                if self.subagent_log_file and TmuxSubagentGrid:
                    summary_box = [
                        "",
                        "\033[1;32m┌─────────────────────────────────────────────────────────────────────────────┐\033[0m",
                        f"\033[1;32m│\033[0m \033[1;37mSTATUS:\033[0m   \033[1;32mCOMPLETED\033[0m in {duration:.1f}s (Turns: {num_turns})",
                        f"\033[1;32m│\033[0m \033[1;37mTOKENS:\033[0m   Input={in_tok:,} | Output={out_tok:,} (Thinking={think_tok:,})",
                        f"\033[1;32m│\033[0m \033[1;37mCONV ID:\033[0m  {self.agy_conversation_id or 'N/A'}",
                        "\033[1;32m├─────────────────────────────────────────────────────────────────────────────┤\033[0m",
                        "\033[1;37mRESPONSE SUMMARY:\033[0m",
                        resp_text[:500] + ("..." if len(resp_text) > 500 else ""),
                        "\033[1;32m└─────────────────────────────────────────────────────────────────────────────┘\033[0m",
                        "\033[90m[Subagent execution completed. Pane remains open for review.]\033[0m",
                    ]
                    TmuxSubagentGrid.append_log(self.subagent_log_file, "\n".join(summary_box))
                    TmuxSubagentGrid.update_pane_status(self.tmux_pane_id, self._delegate_role, acc_id, "✓ DONE")

                if stream_callback and resp_text:
                    try:
                        stream_callback(resp_text)
                    except Exception:
                        pass

                return {
                    "final_response": resp_text,
                    "completed": status_ok,
                    "api_calls": num_turns,
                    "messages": [
                        {"role": "user", "content": prompt_text},
                        {"role": "assistant", "content": resp_text},
                    ],
                    "conversation_id": self.agy_conversation_id,
                }
            except json.JSONDecodeError as exc:
                logger.error("[AntigravitySubagent-%d] Failed to parse agy JSON: %s\nOutput: %s", self.task_index, exc, stdout)
                if self.subagent_log_file and TmuxSubagentGrid:
                    TmuxSubagentGrid.append_log(self.subagent_log_file, f"\033[1;33m[RAW OUTPUT]\033[0m\n{stdout}")
                    TmuxSubagentGrid.update_pane_status(self.tmux_pane_id, self._delegate_role, acc_id, "✓ DONE")

                return {
                    "final_response": stdout.strip(),
                    "completed": True,
                    "api_calls": 1,
                    "messages": [
                        {"role": "user", "content": prompt_text},
                        {"role": "assistant", "content": stdout.strip()},
                    ],
                }

        return {
            "final_response": "Exhausted all account quota failover attempts.",
            "completed": False,
            "failed": True,
            "error": "All accounts quota exhausted",
        }


_orig_resolve_creds = None
_orig_build_child_agent = None


def _patched_resolve_delegation_credentials(cfg: dict, parent_agent: Any) -> dict:
    configured_provider = (cfg.get("provider") or "").strip().lower()
    configured_backend = (cfg.get("backend") or "").strip().lower()

    if configured_provider == "antigravity" or configured_backend == "antigravity":
        model = cfg.get("model") or "gemini-3.8-flash-high"
        return {
            "model": model,
            "provider": "antigravity",
            "base_url": None,
            "api_key": "antigravity-local",
            "api_mode": None,
            "request_overrides": {},
            "command": None,
            "args": [],
        }

    return _orig_resolve_creds(cfg, parent_agent)


def _patched_build_child_agent(*args, **kwargs):
    override_provider = kwargs.get("override_provider") or ""
    if override_provider.strip().lower() == "antigravity":
        task_index = kwargs.get("task_index", 0)
        goal = kwargs.get("goal", "")
        context = kwargs.get("context")
        model = kwargs.get("model") or "gemini-3.8-flash-high"
        role = kwargs.get("role", "leaf")
        parent_agent = kwargs.get("parent_agent")
        subagent_id = f"sa-{task_index}-{uuid.uuid4().hex[:8]}"

        return AntigravityChildAgent(
            task_index=task_index,
            goal=goal,
            context=context,
            model=model,
            role=role,
            parent_agent=parent_agent,
            subagent_id=subagent_id,
            cwd=_resolve_agent_cwd(parent_agent, kwargs.get("cwd")),
        )

    return _orig_build_child_agent(*args, **kwargs)


def _apply() -> None:
    global _orig_resolve_creds, _orig_build_child_agent

    try:
        import tools.delegate_tool_config as dtc
        import tools.delegate_tool as dt

        if _orig_resolve_creds is None:
            _orig_resolve_creds = dtc._resolve_delegation_credentials
            dtc._resolve_delegation_credentials = _patched_resolve_delegation_credentials
            dt._resolve_delegation_credentials = _patched_resolve_delegation_credentials

        if _orig_build_child_agent is None:
            _orig_build_child_agent = dt._build_child_agent
            dt._build_child_agent = _patched_build_child_agent

        logger.info("[agy-delegation] Hooked Hermes delegation to Antigravity CLI")
    except Exception as exc:
        logger.warning("[agy-delegation] Failed to apply delegation hook: %s", exc)


def register(ctx):
    del ctx
    _apply()

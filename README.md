# agy-pool

> **Antigravity Multi-Account Pool & Live Tmux Split Grid Manager**

`agy-pool` manages a dynamic pool of Google Pro accounts for Google Antigravity (`agy`) CLI subagents and manages local Antigravity CLI instances.

---

## Key Features

- **Round-Robin Allocation**: Distributes parallel subagents across multiple accounts, preventing Token-Per-Minute (TPM) and Requests-Per-Minute (RPM) rate limits.
- **Automatic Quota Failover**: Detects `429` / `RESOURCE_EXHAUSTED` and automatically rotates to the next healthy account without failing subagent tasks.
- **Dynamic Multi-Pool Quota Monitor**: Discovers and monitors all active Google quota buckets (Gemini, Claude & GPT, Argon, etc.) dynamically with zero hardcoded model lists. Responsive card layout prevents terminal line wrapping.
- **Live Tmux Multi-Pane Grid**: Dynamically splits and auto-tiles panes in `tmux attach -t subagents` with top border status titles and real-time streaming logs.
- **Instance & Account Switcher**: Easily switch your primary interactive CLI (`agy`) between accounts in the pool with a single command.
- **Hermes Delegation Plugin**: Routes Hermes subagents to Antigravity CLI. Install directly with `hermes plugins install MayankGoyal1805/antigravity_delegate_hermes` or via `agy-pool install-hermes-plugin`.

---

## Installation via `uv`

Install `agy-pool` globally using `uv`:

```bash
uv tool install git+https://github.com/MayankGoyal1805/agy_pool.git
```

Or from a local checkout:

```bash
cd ~/repos/agy_pool
uv tool install . --force
```

---

## CLI Usage

### 1. View Live Quota Across All Pools
```bash
agy-pool usage
```
Displays multi-account quota monitor in IST:
```text
======================================================================================================================
  Antigravity Multi-Account Live Quota & Usage Monitor (IST)
======================================================================================================================
  SLOT  EMAIL                      GEMINI 5HR   RESET (IST)           GEMINI WK    CLAUDE 5HR   CLAUDE WK    ARGON WK   STATUS
----------------------------------------------------------------------------------------------------------------------
  1     dev1@example.com           [████░] 86%  10:35 PM (in 3h 12m)  [██░░░] 50%  [█████]100%  [█████]100%      --     HEALTHY
  2     dev2@example.com           [█████]100%  09:03 PM (in 1h 40m)  [███░░] 53%  [█████]100%  [█████]100%      --     HEALTHY
  3     dev3@example.com           [█████]100%  09:55 PM (in 2h 32m)  [█████]100%  [█████]100%  [█████]100%      --     HEALTHY
  4     dev4@example.com           [█████]100%  10:06 PM (in 2h 42m)  [██░░░] 36%  [█████]100%  [█████]100%      --     HEALTHY
  5     dev5@example.com           [█████]100%  10:26 PM (in 3h 3m)   [█████]100%  [█████]100%  [█████]100%  [████░] 77% HEALTHY
======================================================================================================================
  Note: '--' indicates the model group is not available for that account slot.
======================================================================================================================
```

### 2. View Account Pool & CLI Status
```bash
agy-pool status
```

### 3. Account & Instance Switching
- Check which account is active in your primary `agy` CLI:
  ```bash
  agy-pool current
  ```
- Switch your primary `agy` CLI to use Slot 2's credentials:
  ```bash
  agy-pool switch 2
  ```
- Import the currently active CLI account into the pool:
  ```bash
  agy-pool import-current
  ```
- List all Antigravity instances and profiles on the machine:
  ```bash
  agy-pool instances
  ```

### 4. Add / Login Accounts
- Auto-assign next number:
  ```bash
  agy-pool login
  ```
- Login or re-authenticate a specific slot:
  ```bash
  agy-pool login 6
  ```

### 5. Remove Accounts
```bash
agy-pool rm 3
```

### 6. Attach to Live Subagents Tmux Grid
```bash
agy-pool view
# or
tmux attach -t subagents
```

### 7. Install / Setup Hermes Delegation Plugin
Pull and install the standalone plugin directly from GitHub into Hermes:
```bash
agy-pool install-hermes-plugin
# or directly via Hermes CLI:
hermes plugins install MayankGoyal1805/antigravity_delegate_hermes
```

---

## Python API (for Hermes / Custom Orchestrators)

```python
from agy_pool.manager import AccountPoolManager
from agy_pool.tmux import TmuxSubagentGrid

# 1. Lease next account via thread-safe round-robin
account = AccountPoolManager.get_next_healthy_account()
# Returns: {"id": "1", "rel_dir": "account_pool/accounts/1", "email": "..."}

# 2. Allocate pane in tmux subagent grid
pane_id, log_file = TmuxSubagentGrid.allocate_pane(
    subagent_id="sa-1",
    role="leaf",
    account_id=account["id"],
    email=account["email"],
    cwd="/path/to/repo",
    goal="Analyze repository files",
)

# 3. Mark account exhausted if rate limited (triggers automatic failover)
AccountPoolManager.mark_account_exhausted(account["id"], cooldown_hours=3.0)
```

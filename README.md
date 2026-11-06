# agy-pool

Manually switch the Google account behind an [agy](https://antigravity.google) (Antigravity CLI) instance, without moving your chats.

- **One chat directory.** Instances point at `~/.gemini/antigravity-cli` (agy's default), so the CLI, T3 Code and the VS Code extension all see the same chats.
- **Accounts are swappable tokens.** Each stored account is a tiny agy data dir holding its OAuth token. Switching copies the token into the instance.
- **No background polling.** Quota is only checked when you run `agy-pool usage`.

## Install

```bash
uv tool install --force ~/repos/agy_pool   # from a local checkout
agy-pool instance launcher                 # writes ~/.local/bin/agy-flash
```

### On another machine

Needs [uv](https://docs.astral.sh/uv/) and the `agy` binary (found via `AGY_BIN` or `~/.local/bin/agy`).

```bash
uv tool install git+https://github.com/MayankGoyal1805/agy_pool.git            # install
uv tool install --force git+https://github.com/MayankGoyal1805/agy_pool.git   # upgrade
agy-pool instance launcher                                                     # writes ~/.local/bin/agy-flash
```

Accounts, instances and API keys are per machine and are not copied: run `agy-pool login <label>` and
`agy-pool instance create ... --api-key --base-url ...` again there.

## Concepts

| Thing | What it is |
|---|---|
| **Account** | `~/.gemini/agy-pool/accounts/<label>/antigravity-oauth-token` |
| **Instance** | A name + an app dir + an auth mode. `flash` (default) uses a stored account; others can use a Gemini API key. |
| **Launcher** | `~/.local/bin/agy-<instance>`: runs `agy --app_data_dir=<dir>` and sets `AGY_POOL_INSTANCE=<instance>` so `switch` knows which processes belong to it. |

Point anything that should follow the switchable account (T3 Code ACP provider, your shell) at `agy-flash`:

```bash
alias agy=agy-flash          # optional: make plain `agy` the flash instance
# T3 Code: ACP provider, command /home/ubuntu/.local/bin/agy-flash, args: acp
```

## Commands

```bash
agy-pool login main               # add an account via browser OAuth
agy-pool import alt --from DIR    # import a token from an existing agy data dir
agy-pool import main --current    # adopt the token the instance is using now
agy-pool list                     # accounts and where they're active
agy-pool current                  # live account of an instance + its agy processes
agy-pool switch alt               # switch flash to `alt`
agy-pool switch alt --dry-run     # show what would be stopped
agy-pool usage [label]            # quota, on demand
agy-pool rename alt work
agy-pool rm work

agy-pool instance create argon --api-key --base-url http://HOST:PORT   # API-key instance sharing the same chats
agy-pool instance list
agy-pool instance launcher [name]          # (re)write launchers
agy-pool instance rm argon
```

## How `switch` works

1. Finds agy processes started via the instance's launcher (env marker). Processes not started through it, such as the VS Code extension or a plain `agy`, are never stopped; unmarked ones using the dir are listed as a warning.
2. Asks, then stops them (SIGTERM, SIGKILL after 8s). A running agy keeps its token in memory and refreshes it about hourly, writing it back to the file, which would undo the switch.
3. Copies the live token back to its stored account (it's the freshest copy). An unknown token is saved to `backups/`.
4. Atomically installs the new token (temp file + fsync + rename, mode 0600) and records it.

T3 Code starts a fresh `agy-flash acp` on your next message, which loads the new account. If remote control was running, restart it with `agy-flash remote-control start`.

`agy-pool current` warns about **drift**: the live token no longer matches the last switch, meaning something rewrote it.

## API-key instances

agy only uses `GEMINI_API_KEY` when `modelProvider: "gemini"` is set. Without it, agy ignores the key and signs in with the OAuth token in the dir. Putting that in the shared `settings.json` would flip the account instance too, so the launcher keeps it per instance:

- `agy-argon acp …` → `agy --app_data_dir=<dir> acp --cli_dir_name=argon …`. Settings, logs and cache live in `<dir>/argon/`, seeded from the shared `settings.json` at create time. Chats stay shared.
- any other command → `agy --app_data_dir=<dir> --settings '{"modelProvider":"gemini"}' …`. This is a session-only override; the top-level CLI has no `--cli_dir_name`.

The launcher exports `GEMINI_API_KEY` (from `secrets/<name>.key`), `GOOGLE_GEMINI_BASE_URL` (if `--base-url` was given), and unsets `GOOGLE_API_KEY`. In this mode agy never reads or refreshes the OAuth token, and remote control isn't available.

Note: agy rejects an absolute `--app_data_dir`, so app dirs must live under `~/.gemini`.

## Files

```
~/.gemini/agy-pool/
  state.json            instances + last switched account
  accounts/<label>/     one agy data dir per account
  backups/              every token we overwrite
  secrets/<inst>.key    API keys (0600)
```

Env overrides: `AGY_BIN`, `AGY_POOL_HOME`, `AGY_POOL_GEMINI_ROOT`, `AGY_POOL_BIN_DIR`, `AGY_POOL_TZ`.

## Development

```bash
uv run --group dev pytest
```

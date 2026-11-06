"""agy-pool: manage Google accounts behind agy (Antigravity CLI) instances."""

from __future__ import annotations

import datetime
import os
import subprocess
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from . import launchers, procs, tokens
from .paths import TOKEN_FILE, find_agy_binary, get_paths
from .store import AUTH_ACCOUNT, AUTH_API_KEY, DEFAULT_INSTANCE, Instance, Store, StoreError, validate_label
from .switching import execute_switch, live_status, plan_switch
from .usage import fetch_many

console = Console()
err = Console(stderr=True)

app = typer.Typer(
    name="agy-pool",
    help="Manage Google accounts behind agy (Antigravity CLI) instances.",
    add_completion=False,
    no_args_is_help=True,
)
instance_app = typer.Typer(help="Manage instances (launchers + auth mode).", no_args_is_help=True)
app.add_typer(instance_app, name="instance")

InstanceOpt = typer.Option(DEFAULT_INSTANCE, "--instance", "-i", help="Instance to act on")


def _store() -> Store:
    return Store(get_paths())


def _fail(msg: str) -> None:
    err.print(f"[bold red]✗[/] {msg}")
    raise typer.Exit(1)


def _ago(seconds: Optional[float]) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


def _mtime(path: Path) -> str:
    try:
        return datetime.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "-"


# ---------------------------------------------------------------- accounts
@app.command("list")
def cmd_list() -> None:
    """List stored accounts and which instance uses each."""
    store = _store()
    accounts = store.accounts()
    if not accounts:
        console.print("No accounts yet. Add one with [bold]agy-pool login <label>[/] or [bold]agy-pool import[/].")
        return
    live = {i.name: live_status(store, i) for i in store.instances() if i.auth == AUTH_ACCOUNT}
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
    t.add_column("Label", style="bold")
    t.add_column("Email")
    t.add_column("Active on")
    t.add_column("Token saved", style="dim")
    for a in accounts:
        on = [n for n, s in live.items() if s.matched and s.matched.label == a.label]
        t.add_row(a.label, a.email or "[red]no token[/]", ", ".join(on) or "", _mtime(a.token))
    console.print(t)


@app.command("current")
def cmd_current(instance: str = InstanceOpt) -> None:
    """Show which account an instance is running as, and its agy processes."""
    store = _store()
    try:
        inst = store.get_instance(instance)
    except StoreError as e:
        _fail(str(e))
    console.print(f"Instance [bold cyan]{inst.name}[/]  dir: {store.instance_dir(inst)}")
    if inst.auth == AUTH_API_KEY:
        key = store.api_key_file(inst)
        console.print(f"  auth: API key ({'present' if key.is_file() else '[red]missing[/]'}: {key})")
    else:
        s = live_status(store, inst)
        label = s.matched.label if s.matched else "[yellow]not in store[/] (run `agy-pool import <label> --current`)"
        console.print(f"  account: [bold]{s.email or '[red]logged out[/]'}[/]  →  {label}")
        if s.drift:
            console.print(
                f"  [bold yellow]⚠ drift:[/] last switched to [bold]{s.recorded}[/], but the live token is "
                f"{s.email or 'missing'}. Something rewrote it (an agy process that was running during the switch?)."
            )
    marked, unmarked = procs.scan(inst.name, store.instance_dir(inst))
    for p in marked:
        console.print(f"  [green]●[/] pid {p.pid}  {p.kind}  (up {_ago(p.started_ago)})")
    for p in unmarked:
        console.print(f"  [yellow]●[/] pid {p.pid}  {p.kind}, not started via agy-{inst.name} (up {_ago(p.started_ago)})")
    if not marked and not unmarked:
        console.print("  [dim]no agy processes running[/]")


@app.command("login")
def cmd_login(
    label: str = typer.Argument(..., help="Short name for the account, e.g. main, alt1"),
    force: bool = typer.Option(False, "--force", "-f", help="Re-login an existing account"),
) -> None:
    """Add an account by signing in through the browser."""
    store = _store()
    try:
        acc = store.account(label)
    except StoreError as e:
        _fail(str(e))
    if acc.has_token and not force:
        _fail(f"{acc.label!r} already exists ({acc.email}); use --force to sign in again")
    acc.dir.mkdir(parents=True, exist_ok=True)
    if acc.has_token:
        tokens.backup(acc.token, store.paths.backups, f"relogin-{acc.label}-{acc.email}")
    cmd = [find_agy_binary(), f"--app_data_dir={store.paths.app_dir_arg(acc.dir)}", "login"]
    try:
        rc = subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        rc = 130
    if rc != 0 or not acc.email:
        _fail(f"login did not complete (exit {rc})")
    dup = [a for a in store.accounts() if a.label != acc.label and a.email == acc.email]
    if dup:
        console.print(f"[yellow]⚠ {acc.email} is also stored as {dup[0].label!r}[/]")
    console.print(f"[green]✓[/] {acc.label} → {acc.email}")


@app.command("import")
def cmd_import(
    label: str = typer.Argument(..., help="Label for the imported account"),
    from_dir: Optional[Path] = typer.Option(None, "--from", help="agy data dir (or token file) to import from"),
    current: bool = typer.Option(False, "--current", help="Import the token the instance is using now"),
    instance: str = InstanceOpt,
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing account"),
) -> None:
    """Import an existing token into the store (copy; the source is left untouched)."""
    store = _store()
    if bool(from_dir) == current:
        _fail("pass exactly one of --from DIR or --current")
    try:
        if current:
            inst = store.get_instance(instance)
            src = store.instance_token(inst)
        else:
            src = from_dir.expanduser()
            src = src / TOKEN_FILE if src.is_dir() else src
        with store.locked():
            acc = store.import_token(label, src, overwrite=force)
            if current:
                inst.account = acc.label
                store.save_instance(inst)
    except StoreError as e:
        _fail(str(e))
    console.print(f"[green]✓[/] imported {acc.label} → {acc.email}")


@app.command("rm")
def cmd_rm(
    label: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Remove a stored account (its token is kept in backups)."""
    store = _store()
    try:
        acc = store.get_account(label)
        if not yes and not typer.confirm(f"Remove {acc.label} ({acc.email})?"):
            raise typer.Exit(0)
        with store.locked():
            store.remove_account(label)
    except StoreError as e:
        _fail(str(e))
    console.print(f"[green]✓[/] removed {acc.label}")


@app.command("rename")
def cmd_rename(old: str = typer.Argument(...), new: str = typer.Argument(...)) -> None:
    """Rename a stored account."""
    store = _store()
    try:
        with store.locked():
            acc = store.rename_account(old, new)
    except StoreError as e:
        _fail(str(e))
    console.print(f"[green]✓[/] {old} → {acc.label}")


@app.command("switch")
def cmd_switch(
    label: str = typer.Argument(..., help="Account to switch to"),
    instance: str = InstanceOpt,
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask before stopping processes"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Show what would happen"),
) -> None:
    """Switch an instance to another account.

    Stops the instance's agy processes (only ones started via its launcher), saves the
    current token back to its account, then installs the new token atomically.
    """
    store = _store()
    try:
        plan = plan_switch(store, instance, label)
    except StoreError as e:
        _fail(str(e))
    cur = plan.live
    cur_label = cur.matched.label if cur.matched else "unknown"
    console.print(
        f"[bold cyan]{plan.instance.name}[/]: {cur.email or 'logged out'} ({cur_label})  →  "
        f"[bold]{plan.target.email}[/] ({plan.target.label})"
    )
    if cur.matched and cur.matched.label == plan.target.label and not cur.drift:
        console.print("[green]Already on that account.[/]")
        return
    if plan.to_stop:
        console.print("Will stop:")
        for p in plan.to_stop:
            console.print(f"  pid {p.pid}  {p.kind}  (up {_ago(p.started_ago)})")
    if plan.unmarked:
        console.print("[yellow]Not started via the launcher, so left running (they may write the old token back):[/]")
        for p in plan.unmarked:
            console.print(f"  pid {p.pid}  {p.kind}  {p.cmdline[:80]}")
    if dry_run:
        return
    if plan.to_stop and not yes and not typer.confirm("Stop these and switch?"):
        raise typer.Exit(0)
    try:
        with store.locked():
            res = execute_switch(store, plan)
    except StoreError as e:
        _fail(str(e))
    if res.written_back:
        console.print(f"  saved refreshed token back to {res.written_back}")
    if res.orphan_backup:
        console.print(f"  [yellow]previous token was not in the store; saved to {res.orphan_backup}[/]")
    if res.killed:
        console.print(f"  force-killed {res.killed}")
    console.print(f"[green]✓[/] {plan.instance.name} now uses {plan.target.label} ({plan.target.email})")
    if any(p.kind == "remote-control daemon" for p in plan.to_stop):
        console.print(f"  remote control was stopped; start it again with: agy-{plan.instance.name} remote-control start")


@app.command("usage")
def cmd_usage(
    label: Optional[str] = typer.Argument(None, help="Account (default: all)"),
    tz: str = typer.Option(os.environ.get("AGY_POOL_TZ", "Asia/Kolkata"), "--tz", help="Timezone for reset times"),
) -> None:
    """Show remaining quota (one /usage call per account, only when you run this)."""
    store = _store()
    try:
        accounts = [store.get_account(label)] if label else store.accounts()
        zone = ZoneInfo(tz)
    except StoreError as e:
        _fail(str(e))
    except Exception:
        _fail(f"unknown timezone {tz!r}")
    if not accounts:
        _fail("no accounts")
    active = {s.matched.label for i in store.instances() if i.auth == AUTH_ACCOUNT
              for s in [live_status(store, i)] if s.matched}
    with console.status(f"Querying quota for {len(accounts)} account(s)…"):
        results = fetch_many(store.paths, accounts)
    now = datetime.datetime.now(datetime.timezone.utc)
    for r in results:
        mark = " [green](active)[/]" if r.account.label in active else ""
        t = Table(title=f"[bold]{r.account.label}[/] · {r.account.email}{mark}", title_justify="left",
                  box=box.SIMPLE, header_style="dim", show_edge=False)
        t.add_column("Group")
        t.add_column("Limit")
        t.add_column("Left", justify="right")
        t.add_column("Resets")
        if r.error:
            console.print(f"[bold]{r.account.label}[/] · {r.account.email}{mark}: [red]{r.error}[/]\n")
            continue
        for lim in r.limits:
            color = "green" if (lim.pct or 0) > 40 else "yellow" if (lim.pct or 0) > 15 else "red"
            reset = ""
            if lim.reset:
                secs = max(0, int((lim.reset - now).total_seconds()))
                reset = f"{lim.reset.astimezone(zone):%a %H:%M} (in {_ago(secs)})"
            t.add_row(lim.group, lim.name, f"[{color}]{lim.pct}%[/]", reset)
        console.print(t)
        console.print()


# --------------------------------------------------------------- instances
@instance_app.command("list")
def cmd_instance_list() -> None:
    """List instances, their launchers and auth."""
    store = _store()
    t = Table(box=box.SIMPLE_HEAVY, header_style="bold cyan")
    for col in ("Name", "Launcher", "App dir", "Auth"):
        t.add_column(col)
    for inst in store.instances():
        lp = launchers.launcher_path(store.paths, inst)
        if inst.auth == AUTH_API_KEY:
            auth = "API key" + (f" @ {inst.base_url}" if inst.base_url else "")
        else:
            auth = live_status(store, inst).email or "logged out"
        t.add_row(inst.name, lp.name if lp.exists() else f"[dim]{lp.name} (not written)[/]",
                  str(store.instance_dir(inst)), auth)
    console.print(t)


@instance_app.command("create")
def cmd_instance_create(
    name: str = typer.Argument(..., help="Instance name, e.g. argon"),
    app_dir: str = typer.Option("antigravity-cli", "--app-dir", help="agy data dir, must be under ~/.gemini (e.g. antigravity-cli)"),
    api_key: bool = typer.Option(False, "--api-key", help="Authenticate with a Gemini API key instead of an account"),
    api_key_file: Optional[Path] = typer.Option(None, "--api-key-file", help="Read the API key from this file"),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Gemini API endpoint (GOOGLE_GEMINI_BASE_URL) for --api-key"),
) -> None:
    """Create an instance and its launcher (agy-<name>)."""
    store = _store()
    try:
        name = validate_label(name, "instance name")
        if any(i.name == name for i in store.instances()):
            _fail(f"instance {name!r} already exists")
        use_key = api_key or api_key_file is not None
        if base_url and not use_key:
            _fail("--base-url only applies to --api-key instances")
        app_dir = store.paths.app_dir_arg(store.paths.resolve_app_dir(app_dir))
        if Path(app_dir).is_absolute():
            _fail(f"app dir must be under {store.paths.gemini_root} (agy rejects absolute --app_data_dir)")
        inst = Instance(name, app_dir, AUTH_API_KEY if use_key else AUTH_ACCOUNT,
                        base_url=base_url.rstrip("/") if base_url else None)
        if use_key:
            key = (api_key_file.read_text() if api_key_file else typer.prompt("Gemini API key", hide_input=True)).strip()
            if not key:
                _fail("empty API key")
            tokens.atomic_write(store.api_key_file(inst), key.encode())
            store.seed_cli_settings(inst)
        store.save_instance(inst)
        path = launchers.write(store, inst)
    except (StoreError, FileExistsError, OSError) as e:
        _fail(str(e))
    console.print(f"[green]✓[/] instance {name} → {path}")


@instance_app.command("launcher")
def cmd_instance_launcher(
    name: Optional[str] = typer.Argument(None, help="Instance (default: all)"),
    force: bool = typer.Option(False, "--force", help="Overwrite a file not created by agy-pool"),
) -> None:
    """(Re)write launcher scripts."""
    store = _store()
    try:
        insts = [store.get_instance(name)] if name else store.instances()
        for inst in insts:
            console.print(f"[green]✓[/] {launchers.write(store, inst, force=force)}")
    except (StoreError, FileExistsError) as e:
        _fail(str(e))


@instance_app.command("rm")
def cmd_instance_rm(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Remove an instance's registration, launcher and API key. The app dir and chats are not touched."""
    store = _store()
    try:
        inst = store.get_instance(name)
    except StoreError as e:
        _fail(str(e))
    if inst.name == DEFAULT_INSTANCE:
        _fail(f"{DEFAULT_INSTANCE!r} is the default instance and can't be removed")
    if not yes and not typer.confirm(f"Remove instance {inst.name}? (app dir is left alone)"):
        raise typer.Exit(0)
    launchers.remove(store, inst)
    key = store.api_key_file(inst)
    if key.exists():
        key.unlink()
    store.delete_instance(inst.name)
    console.print(f"[green]✓[/] removed instance {inst.name}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

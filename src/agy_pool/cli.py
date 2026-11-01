"""Antigravity Account Pool & Instance Manager CLI powered by Typer and Rich."""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .manager import (
    ACCOUNTS_DIR,
    AGY_BIN,
    AccountPoolManager,
    create_instance,
    ensure_dirs,
    get_instance_dir,
    get_instance_rel_dir,
    get_next_available_slot,
    import_primary_cli_account,
    list_accounts,
    list_instances,
    load_state,
    remove_instance,
    resolve_slot_id,
    save_state,
    switch_instance_account,
)
from .quota import fetch_all_usage
from .tmux import TMUX_SESSION_NAME

console = Console()
app = typer.Typer(
    name="agy-pool",
    help="Antigravity Multi-Account Pool & Instance Manager",
    add_completion=False,
    no_args_is_help=False,
)

instance_app = typer.Typer(
    name="instance",
    help="Manage separated Antigravity profile instances on this machine",
    add_completion=False,
)
app.add_typer(instance_app, name="instance")


def get_timezone(tz_name: str = "IST") -> datetime.tzinfo:
    """Resolve timezone by name, supporting IST, UTC, and LOCAL."""
    upper = tz_name.strip().upper()
    if upper == "IST":
        return ZoneInfo("Asia/Kolkata")
    if upper == "UTC":
        return datetime.timezone.utc
    if upper == "LOCAL":
        return datetime.datetime.now().astimezone().tzinfo or datetime.timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def format_reset_time_ist(iso_str: Optional[str], tz_name: str = "IST") -> str:
    """Format reset timestamp in requested timezone (default: IST) and relative countdown."""
    if not iso_str:
        return "--"
    try:
        clean = iso_str.replace(" UTC", "+00:00").replace("Z", "+00:00")
        if "T" not in clean and " " in clean:
            clean = clean.replace(" ", "T")
        dt_utc = datetime.datetime.fromisoformat(clean)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        secs = int((dt_utc - now_utc).total_seconds())

        if secs <= 0:
            return "ready"

        target_tz = get_timezone(tz_name)
        dt_target = dt_utc.astimezone(target_tz)
        time_str = dt_target.strftime("%I:%M %p")

        hours, rem = divmod(secs, 3600)
        mins, _ = divmod(rem, 60)
        if hours > 0:
            return f"{time_str} (in {hours}h {mins}m)"
        return f"{time_str} (in {mins}m)"
    except Exception:
        return str(iso_str)[:16]


def format_bar(pct: Optional[int], width: int = 5) -> str:
    if pct is None:
        return "    --    "
    filled = max(0, min(width, int(round((pct / 100.0) * width))))
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}]{pct:>3}%"


# =====================================================================
# Main Commands
# =====================================================================

@app.command(name="usage")
def cmd_usage(
    tz: str = typer.Option(
        os.environ.get("AGY_TIMEZONE", "IST"),
        "--tz",
        "--timezone",
        help="Timezone for reset timestamps (default: IST)",
    ),
    as_cards: bool = typer.Option(False, "--cards", "-c", help="Show vertical card view"),
):
    """View real-time Google quota remaining across all accounts & pools in IST."""
    print("\nQuerying live Google quota for all accounts in parallel...\n")
    results = fetch_all_usage()

    state = load_state()
    exhausted_map = state.get("exhausted", {})
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    if as_cards:
        console.rule(f"[bold cyan]Antigravity Multi-Account Live Quota Monitor ({tz.upper()})[/bold cyan]")
        for item in results:
            acc = item["acc"]
            slot = acc["id"]
            email = acc["email"]
            pools = item.get("pools", {})

            exp_ts = exhausted_map.get(slot, 0)
            if exp_ts > now_ts:
                status_text = f"[bold yellow]COOLDOWN ({int((exp_ts - now_ts) / 60)}m)[/bold yellow]"
                border_color = "yellow"
            elif not acc.get("authenticated"):
                status_text = "[bold red]UNAUTHENTICATED[/bold red]"
                border_color = "red"
            elif item.get("error"):
                status_text = f"[bold red]ERROR: {item['error']}[/bold red]"
                border_color = "red"
            else:
                status_text = "[bold green]HEALTHY[/bold green]"
                border_color = "blue"

            t = Table.grid(padding=(0, 2))
            t.add_column("Group", style="bold yellow", width=26)
            t.add_column("Limits", style="white")

            if not pools:
                t.add_row("(No buckets)", "[dim]Not authenticated or no active quota reported[/dim]")
            else:
                for grp_name, limits in pools.items():
                    display_grp = grp_name.replace("Claude and GPT models", "Claude & GPT")
                    sorted_limits = sorted(limits.items(), key=lambda x: (0 if "5" in x[0] else 1, x[0]))
                    for kind, p in sorted_limits:
                        bar = format_bar(p["pct"])
                        rst = format_reset_time_ist(p["reset"], tz)
                        rst_str = f" · [dim]{rst}[/dim]" if rst else ""
                        row_label = f"{display_grp} ({kind}):"
                        t.add_row(row_label, f"{bar}{rst_str}")

            panel = Panel(
                t,
                title=f"[bold cyan]Slot {slot}[/bold cyan] · [bold white]{email}[/bold white]",
                subtitle=status_text,
                box=box.ROUNDED,
                border_style=border_color,
            )
            console.print(panel)
        console.rule()
        return

    # Default Table View
    line_width = 118
    print("=" * line_width)
    print(f"  Antigravity Multi-Account Live Quota & Usage Monitor ({tz.upper()})")
    print("=" * line_width)
    print(f"  {'SLOT':<5} {'EMAIL':<26} {'GEMINI 5HR':<12} {'RESET (IST)':<21} {'GEMINI WK':<12} {'CLAUDE 5HR':<12} {'CLAUDE WK':<12} {'ARGON WK':<10} {'STATUS'}")
    print("-" * line_width)

    for item in results:
        acc = item["acc"]
        slot = acc["id"]
        email = acc["email"]
        pools = item.get("pools", {})
        if len(email) > 24:
            email = email[:21] + "..."

        if not acc.get("authenticated"):
            print(f"  {slot:<5} {'(not authenticated)':<26} {'--':<12} {'--':<21} {'--':<12} {'--':<12} {'--':<12} {'--':<10} UNAUTHENTICATED")
            continue

        if item.get("error"):
            print(f"  {slot:<5} {email:<26} ERROR: {item['error']}")
            continue

        gem_5hr = format_bar(pools.get("Gemini Models", {}).get("5-hr", {}).get("pct"))
        gem_5hr_rst = format_reset_time_ist(pools.get("Gemini Models", {}).get("5-hr", {}).get("reset"), tz)
        gem_wk = format_bar(pools.get("Gemini Models", {}).get("weekly", {}).get("pct"))
        cld_5hr = format_bar(pools.get("Claude and GPT models", {}).get("5-hr", {}).get("pct"))
        cld_wk = format_bar(pools.get("Claude and GPT models", {}).get("weekly", {}).get("pct"))
        argon_pct = pools.get("Gemini 4 Argon", {}).get("weekly", {}).get("pct")
        argon_wk = format_bar(argon_pct) if argon_pct is not None else "    --    "

        exp_ts = exhausted_map.get(slot, 0)
        if exp_ts > now_ts:
            mins_left = int((exp_ts - now_ts) / 60)
            status_str = f"COOLDOWN ({mins_left}m)"
        elif not acc.get("authenticated"):
            status_str = "UNAUTH"
        elif item.get("error"):
            status_str = "ERROR"
        else:
            status_str = "HEALTHY"

        print(f"  {slot:<5} {email:<26} {gem_5hr:<12} {gem_5hr_rst:<21} {gem_wk:<12} {cld_5hr:<12} {cld_wk:<12} {argon_wk:<10} {status_str}")

    print("=" * line_width)
    print("  Note: '--' indicates the model group is not available for that account slot.")
    print("=" * line_width + "\n")


@app.command(name="status")
def cmd_status():
    """List all accounts in the pool and identify active CLI accounts."""
    ensure_dirs()
    accounts = list_accounts(include_symlinks=False)
    instances = list_instances()
    primary = next((inst for inst in instances if inst["name"] == "primary"), None)
    state = load_state()
    rr_idx = state.get("rr_index", 0)

    table = Table(
        title=f"Antigravity Account Pool (Total: {len(accounts)} | Next Round-Robin: #{rr_idx})",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("Slot", justify="center", style="bold cyan", width=6)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Email", style="bold white")
    table.add_column("CLI Active", justify="center", width=12)

    for acc in accounts:
        auth_str = "[bold green]YES[/bold green]" if acc["authenticated"] else "[bold red]NO[/bold red]"
        is_active = "[bold green]● ACTIVE[/bold green]" if primary and primary.get("matching_slot") == acc["id"] else ""
        table.add_row(acc["id"], auth_str, acc["email"], is_active)

    console.print(table)
    if primary:
        console.print(f"  [dim]Primary Interactive CLI (`agy`):[/dim] [bold white]{primary['email']}[/bold white] (Slot {primary.get('matching_slot') or 'Unmanaged'})\n")


@app.command(name="login")
def cmd_login(
    slot: Optional[str] = typer.Argument(None, help="Slot number (defaults to next available integer)"),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-authentication"),
):
    """Authenticate or add an account slot via browser OAuth."""
    slot_id = resolve_slot_id(slot)
    acc_path = ACCOUNTS_DIR / slot_id
    acc_path.mkdir(parents=True, exist_ok=True)
    rel_dir = f"account_pool/accounts/{slot_id}"

    token_file = acc_path / "antigravity-oauth-token"
    if token_file.exists() and not force:
        console.print(f"[yellow]Slot '{slot_id}' is already authenticated! Use --force to re-login.[/yellow]")
        return

    console.print(f"\nLaunching Antigravity OAuth login for [bold cyan]Slot {slot_id}[/bold cyan]...")
    cmd = [AGY_BIN, f"--app_data_dir={rel_dir}", "login"]
    try:
        subprocess.run(cmd, check=True)
        console.print(f"[bold green]✓ Successfully authenticated Slot {slot_id}![/bold green]\n")
    except subprocess.CalledProcessError as e:
        console.print(f"[bold red]✗ Authentication failed (code {e.returncode})[/bold red]\n")
    except KeyboardInterrupt:
        console.print("\n[yellow]Login cancelled.[/yellow]\n")


@app.command(name="rm")
def cmd_remove(
    slot: str = typer.Argument(..., help="Slot number to delete (e.g. 3)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Remove an account slot from the pool."""
    slot_id = resolve_slot_id(slot)
    acc_path = ACCOUNTS_DIR / slot_id
    alias_path = ACCOUNTS_DIR / f"acc{slot_id}"

    if not acc_path.exists() and not alias_path.exists():
        console.print(f"[bold red]Slot '{slot_id}' does not exist.[/bold red]")
        return

    confirm = yes or typer.confirm(f"Are you sure you want to delete Slot {slot_id}?")
    if confirm:
        if acc_path.exists():
            if acc_path.is_symlink():
                acc_path.unlink()
            elif acc_path.is_dir():
                shutil.rmtree(acc_path)
        if alias_path.exists():
            if alias_path.is_symlink():
                alias_path.unlink()
            elif alias_path.is_dir():
                shutil.rmtree(alias_path)

        state = load_state()
        exhausted = state.get("exhausted", {})
        if slot_id in exhausted:
            del exhausted[slot_id]
            state["exhausted"] = exhausted
            save_state(state)

        console.print(f"[bold green]✓ Removed Slot {slot_id}. Pool dynamically adjusted.[/bold green]")


@app.command(name="switch")
def cmd_switch(
    slot: str = typer.Argument(..., help="Slot number to activate in primary CLI (e.g. 2)"),
):
    """Switch your primary interactive CLI (`agy`) to use a slot's credentials."""
    ok, msg = switch_instance_account("primary", slot)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
        console.print("[dim]You can now run `agy` in your terminal with this account.[/dim]\n")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]\n")


@app.command(name="current")
def cmd_current():
    """Show which account is active in the primary interactive CLI."""
    instances = list_instances()
    primary = next((i for i in instances if i["name"] == "primary"), None)
    if primary:
        console.print(f"\n[bold cyan]Active Antigravity CLI Account:[/bold cyan] [bold white]{primary['email']}[/bold white]")
        console.print(f"  Directory: [dim]{primary['path']}[/dim]")
        slot_str = f"Slot {primary['matching_slot']}" if primary.get("matching_slot") else "None (Unmanaged)"
        console.print(f"  Pool Slot: [bold yellow]{slot_str}[/bold yellow]\n")


@app.command(name="run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def cmd_run(
    ctx: typer.Context,
    instance: str = typer.Argument("primary", help="Instance to run (e.g. primary, subagents)"),
):
    """Launch the interactive Antigravity CLI for a specific instance."""
    rel_dir = get_instance_rel_dir(instance)
    args = ctx.args or []
    console.print(f"[dim]Launching Antigravity CLI for instance '{instance}' (--app_data_dir={rel_dir})...[/dim]\n")
    cmd = [AGY_BIN, f"--app_data_dir={rel_dir}"] + args
    os.execvp(AGY_BIN, cmd)


@app.command(name="test")
def cmd_test():
    """Ping all authenticated accounts with a lightweight 1-word query."""
    accounts = [a for a in list_accounts() if a["authenticated"]]
    if not accounts:
        console.print("[yellow]No authenticated accounts found.[/yellow]")
        return

    console.print(f"\nPinging {len(accounts)} accounts with lightweight query (gemini-3.8-flash)...")
    for acc in accounts:
        cmd = [
            AGY_BIN,
            f"--app_data_dir={acc['rel_dir']}",
            "--model", "gemini-3.8-flash",
            "--output-format", "json",
            "--dangerously-skip-permissions",
        ]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stdout, stderr = proc.communicate(input="say ok in 1 word", timeout=20)
        if proc.returncode == 0:
            console.print(f"  [bold green]✓ Slot {acc['id']:<4}[/bold green] ({acc['email']}): [green]HEALTHY[/green]")
        else:
            err = (stderr or stdout).strip()[:80]
            console.print(f"  [bold red]✗ Slot {acc['id']:<4}[/bold red] ({acc['email']}): [red]FAILED ({err})[/red]")
    console.print()


@app.command(name="view")
def cmd_view():
    """Attach to the live subagents tmux grid."""
    if not shutil.which("tmux"):
        console.print("[bold red]Error: tmux is not installed.[/bold red]")
        return
    res = subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION_NAME], capture_output=True)
    if res.returncode != 0:
        console.print(f"[yellow]No active '{TMUX_SESSION_NAME}' tmux session yet. Run subagents to spawn one.[/yellow]")
        return
    console.print(f"[dim]Attaching to tmux session '{TMUX_SESSION_NAME}' (detach anytime with Ctrl+b d)...[/dim]")
    os.execvp("tmux", ["tmux", "attach", "-t", TMUX_SESSION_NAME])


@app.command(name="install-hermes-plugin")
def cmd_install_plugin():
    """Install or update the Hermes agy-delegation plugin."""
    hermes_plugins_dir = Path.home() / ".hermes" / "plugins"
    target_plugin_dir = hermes_plugins_dir / "agy-delegation"

    repo_plugin_src = Path(__file__).resolve().parent.parent.parent / "hermes_plugin" / "agy-delegation"
    if not repo_plugin_src.exists():
        repo_plugin_src = Path("/home/ubuntu/repos/agy_pool/hermes_plugin/agy-delegation")

    if not repo_plugin_src.exists():
        console.print(f"[bold red]Error: Plugin source not found at {repo_plugin_src}[/bold red]")
        return

    hermes_plugins_dir.mkdir(parents=True, exist_ok=True)
    if target_plugin_dir.exists():
        if target_plugin_dir.is_symlink():
            target_plugin_dir.unlink()
        else:
            shutil.rmtree(target_plugin_dir)

    shutil.copytree(repo_plugin_src, target_plugin_dir)
    console.print(f"[bold green]✓ Installed agy-delegation plugin into {target_plugin_dir}[/bold green]")

    res = subprocess.run(["systemctl", "--user", "is-active", "hermes-gateway.service"], capture_output=True, text=True)
    if res.stdout.strip() == "active":
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway.service"])
        console.print("[bold green]✓ Restarted hermes-gateway.service[/bold green]")


# =====================================================================
# Instance Sub-commands
# =====================================================================

@instance_app.command(name="list")
def cmd_instance_list():
    """List all Antigravity profile instances, their paths, and launcher commands."""
    instances = list_instances()
    table = Table(
        title="Antigravity Profile Instances on this Machine",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("Instance", style="bold cyan")
    table.add_column("CLI Command", style="bold magenta")
    table.add_column("Active Account", style="bold white")
    table.add_column("Pool Slot", justify="center")
    table.add_column("Data Mode", justify="center")
    table.add_column("Storage Path (AppData)", style="dim")

    for inst in instances:
        slot_str = f"Slot {inst['matching_slot']}" if inst.get("matching_slot") else "[dim]--[/dim]"
        data_mode = "[green]Shared[/green]" if inst.get("shared_data") or inst.get("is_default") else "[cyan]Isolated[/cyan]"
        table.add_row(
            inst["name"],
            inst["cli_cmd"],
            inst["email"],
            slot_str,
            data_mode,
            inst["path"],
        )

    console.print(table)


@instance_app.command(name="create")
def cmd_instance_create(
    name: str = typer.Argument(..., help="New instance name (e.g. subagents, coding)"),
    slot: Optional[str] = typer.Option(None, "--slot", "-s", help="Slot number to assign"),
    path: Optional[Path] = typer.Option(None, "--path", "-p", help="Custom directory path (optional)"),
    shared_data: bool = typer.Option(True, "--shared/--isolated", help="Share conversation data with primary instance"),
):
    """Create a new Antigravity instance profile with dedicated launcher."""
    ok, msg = create_instance(name, slot_id=slot, custom_path=path, shared_data=shared_data)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
        console.print(f"[dim]Run with `agy-{name}` or `agy-pool run {name}`.[/dim]\n")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]\n")


@instance_app.command(name="switch")
def cmd_instance_switch(
    instance: str = typer.Argument(..., help="Instance name (e.g. primary, subagents)"),
    slot: str = typer.Argument(..., help="Slot number to assign (e.g. 2)"),
):
    """Switch any instance's active account to a pool slot."""
    ok, msg = switch_instance_account(instance, slot)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]")


@instance_app.command(name="rm")
def cmd_instance_rm(
    name: str = typer.Argument(..., help="Instance name to delete"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete a custom Antigravity instance directory and its launcher."""
    confirm = yes or typer.confirm(f"Are you sure you want to delete instance '{name}'?")
    if confirm:
        ok, msg = remove_instance(name)
        if ok:
            console.print(f"[bold green]✓ {msg}[/bold green]")
        else:
            console.print(f"[bold red]✗ {msg}[/bold red]")


@instance_app.command(name="run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def cmd_instance_run(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Instance name to launch"),
):
    """Launch the interactive Antigravity CLI for a specific instance."""
    rel_dir = get_instance_rel_dir(name)
    args = ctx.args or []
    console.print(f"[dim]Launching Antigravity CLI for instance '{name}' (--app_data_dir={rel_dir})...[/dim]\n")
    cmd = [AGY_BIN, f"--app_data_dir={rel_dir}"] + args
    os.execvp(AGY_BIN, cmd)


def main():
    app()


if __name__ == "__main__":
    main()

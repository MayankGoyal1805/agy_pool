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


def format_reset_time(iso_str: Optional[str], tz_name: str = "IST", compact: bool = False) -> str:
    """Format full reset timestamp and relative countdown in requested timezone."""
    if not iso_str:
        return ""
    try:
        clean = iso_str.replace(" UTC", "+00:00").replace("Z", "+00:00")
        if "T" not in clean and " " in clean:
            clean = clean.replace(" ", "T")
        dt_utc = datetime.datetime.fromisoformat(clean)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        secs = int((dt_utc - now_utc).total_seconds())

        if secs <= 0:
            rel = "now" if compact else "resets now"
        else:
            days, rem = divmod(secs, 86400)
            hours, rem = divmod(rem, 3600)
            mins, _ = divmod(rem, 60)
            if compact:
                if days >= 3:
                    rel = f"{days}d"
                elif days > 0:
                    rel = f"{days}d {hours}h"
                elif hours > 0:
                    rel = f"{hours}h {mins}m"
                else:
                    rel = f"{mins}m"
            else:
                if days > 0:
                    rel = f"in {days}d {hours}h"
                elif hours > 0:
                    rel = f"in {hours}h {mins}m"
                else:
                    rel = f"in {mins}m"

        if compact:
            return rel

        target_tz = get_timezone(tz_name)
        target_dt = dt_utc.astimezone(target_tz)
        tz_label = tz_name.upper()

        if secs > 86400:
            date_str = target_dt.strftime("%b %d, %I:%M %p")
        else:
            date_str = target_dt.strftime("%I:%M %p")

        return f"{date_str} ({rel})"
    except Exception:
        return f"({iso_str[:16]})"


def format_bar_str(pct: Optional[int], width: int = 5) -> str:
    if pct is None:
        return "[dim]--[/dim]"
    filled = max(0, min(width, int(round((pct / 100.0) * width))))
    bar = "█" * filled + "░" * (width - filled)
    color = "green" if pct > 40 else ("yellow" if pct > 15 else "red")
    return f"[{color}][{bar}] {pct:>3}%[/{color}]"


def color_pct_str(pct: Optional[int]) -> str:
    if pct is None:
        return "[dim]--[/dim]"
    color = "green" if pct > 40 else ("yellow" if pct > 15 else "red")
    return f"[{color}]{pct:>3}%[/{color}]"


# =====================================================================
# Main Commands
# =====================================================================

@app.command(name="usage")
def cmd_usage(
    as_cards: bool = typer.Option(False, "--cards", "-c", help="Show vertical card view"),
    wide: bool = typer.Option(False, "--wide", "-w", help="Force full 8-column wide table"),
    tz: str = typer.Option(
        os.environ.get("AGY_TIMEZONE", "IST"),
        "--tz",
        "--timezone",
        help="Timezone for reset timestamps (default: IST)",
    ),
):
    """View real-time Google quota remaining across all accounts & pools in IST."""
    with console.status("[bold cyan]Querying Google quota for all accounts in parallel...[/bold cyan]"):
        results = fetch_all_usage()

    state = load_state()
    exhausted_map = state.get("exhausted", {})
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()

    if as_cards:
        # Pane-resilient Structured Grid Cards
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
                border_color = "cyan"

            grid = Table.grid(padding=(0, 2), expand=True)
            grid.add_column("Pool", style="bold yellow", width=18)
            grid.add_column("Remaining", style="white")
            grid.add_column("Reset Time (IST)", style="dim white", justify="right")

            gem_grp = pools.get("Gemini Models", {})
            cld_grp = pools.get("Claude and GPT models", {})
            argon_grp = pools.get("Gemini 4 Argon", {})

            # 1. Gemini 5-Hr
            g5 = gem_grp.get("5-hr", {})
            g5_bar = format_bar_str(g5.get("pct"))
            g5_rst = format_reset_time(g5.get("reset"), tz) or "--"
            grid.add_row("Gemini 5-Hr", g5_bar, g5_rst)

            # 2. Gemini Weekly
            gw = gem_grp.get("weekly", {})
            gw_bar = format_bar_str(gw.get("pct"))
            gw_rst = format_reset_time(gw.get("reset"), tz) or "--"
            grid.add_row("Gemini Weekly", gw_bar, gw_rst)

            # 3. Claude 5-Hr
            c5 = cld_grp.get("5-hr", {})
            c5_bar = format_bar_str(c5.get("pct"))
            grid.add_row("Claude 5-Hr", c5_bar, "--")

            # 4. Claude Weekly
            cw = cld_grp.get("weekly", {})
            cw_bar = format_bar_str(cw.get("pct"))
            grid.add_row("Claude Weekly", cw_bar, "--")

            # 5. Argon Weekly (if available)
            if argon_grp:
                ar = argon_grp.get("weekly", {})
                ar_bar = format_bar_str(ar.get("pct"))
                ar_rst = format_reset_time(ar.get("reset"), tz) or "--"
                grid.add_row("Gemini 4 Argon", ar_bar, ar_rst)

            panel = Panel(
                grid,
                title=f"[bold cyan]Slot {slot}[/bold cyan] · [bold white]{email}[/bold white]",
                subtitle=status_text,
                box=box.ROUNDED,
                border_style=border_color,
            )
            console.print(panel)
        console.rule()
        return

    term_width = console.width or 100

    # Responsive Adaptive Table (Under 125 cols: compact 5-column layout <= 75 chars)
    if not wide and term_width < 125:
        table = Table(
            title=f"Antigravity Multi-Account Live Quota Monitor ({tz.upper()})",
            box=box.ROUNDED,
            header_style="bold cyan",
            padding=(0, 1),
        )
        table.add_column("Slot", justify="center", style="bold cyan")
        table.add_column("Account", style="bold white", min_width=20, no_wrap=True)
        table.add_column("Gemini (5h / Wk)", justify="left")
        table.add_column("Claude / Argon", justify="left")
        table.add_column("Status", justify="center")

        for item in results:
            acc = item["acc"]
            slot = acc["id"]
            email = acc["email"]
            pools = item.get("pools", {})

            # Status
            exp_ts = exhausted_map.get(slot, 0)
            if exp_ts > now_ts:
                status_str = f"[bold yellow]COOLDOWN ({int((exp_ts - now_ts) / 60)}m)[/bold yellow]"
            elif not acc.get("authenticated"):
                status_str = "[bold red]UNAUTH[/bold red]"
            elif item.get("error"):
                status_str = "[bold red]ERROR[/bold red]"
            else:
                status_str = "[bold green]HEALTHY[/bold green]"

            # Account user + domain
            if "@" in email:
                u, d = email.split("@", 1)
                acc_cell = f"{u}\n[dim]@{d}[/dim]"
            else:
                acc_cell = email

            g5_pct = pools.get("Gemini Models", {}).get("5-hr", {}).get("pct")
            g5_rst = format_reset_time(pools.get("Gemini Models", {}).get("5-hr", {}).get("reset"), tz, compact=True)
            gw_pct = pools.get("Gemini Models", {}).get("weekly", {}).get("pct")
            gw_rst = format_reset_time(pools.get("Gemini Models", {}).get("weekly", {}).get("reset"), tz, compact=True)

            g5_str = f"5h: {color_pct_str(g5_pct)}" + (f" [dim]({g5_rst})[/dim]" if g5_rst else "")
            gw_str = f"Wk: {color_pct_str(gw_pct)}" + (f" [dim]({gw_rst})[/dim]" if gw_rst else "")
            gem_cell = f"{g5_str}\n{gw_str}"

            cld_pct = pools.get("Claude and GPT models", {}).get("5-hr", {}).get("pct")
            ar_pct = pools.get("Gemini 4 Argon", {}).get("weekly", {}).get("pct")
            ar_rst = format_reset_time(pools.get("Gemini 4 Argon", {}).get("weekly", {}).get("reset"), tz, compact=True)

            cld_str = f"Cld: {color_pct_str(cld_pct)}" if cld_pct is not None else "Cld: --"
            ar_str = f"Arg: {color_pct_str(ar_pct)}" + (f" [dim]({ar_rst})[/dim]" if ar_rst else "") if ar_pct is not None else "[dim]Arg: --[/dim]"
            other_cell = f"{cld_str}\n{ar_str}"

            table.add_row(slot, acc_cell, gem_cell, other_cell, status_str)

        console.print(table)
        return

    # Full Widescreen Table View (125+ cols or --wide)
    table = Table(
        title=f"Antigravity Multi-Account Live Quota Monitor ({tz.upper()})",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    table.add_column("Slot", justify="center", style="bold cyan")
    table.add_column("Email", style="bold white")
    table.add_column("Gemini 5-Hr", justify="center")
    table.add_column("Gemini Wk", justify="center")
    table.add_column("Claude 5-Hr", justify="center")
    table.add_column("Claude Wk", justify="center")
    table.add_column("Argon Wk", justify="center")
    table.add_column("Status", justify="center")

    for item in results:
        acc = item["acc"]
        slot = acc["id"]
        email = acc["email"]
        pools = item.get("pools", {})

        # Status
        exp_ts = exhausted_map.get(slot, 0)
        if exp_ts > now_ts:
            status_str = f"[bold yellow]COOLDOWN ({int((exp_ts - now_ts) / 60)}m)[/bold yellow]"
        elif not acc.get("authenticated"):
            status_str = "[bold red]UNAUTH[/bold red]"
        elif item.get("error"):
            status_str = "[bold red]ERROR[/bold red]"
        else:
            status_str = "[bold green]HEALTHY[/bold green]"

        gem_5hr = format_bar_str(pools.get("Gemini Models", {}).get("5-hr", {}).get("pct"))
        gem_5hr_rst = format_reset_time(pools.get("Gemini Models", {}).get("5-hr", {}).get("reset"), tz, compact=True)
        gem_5hr_cell = f"{gem_5hr}\n[dim]{gem_5hr_rst}[/dim]" if gem_5hr_rst else gem_5hr

        gem_wk = format_bar_str(pools.get("Gemini Models", {}).get("weekly", {}).get("pct"))
        gem_wk_rst = format_reset_time(pools.get("Gemini Models", {}).get("weekly", {}).get("reset"), tz, compact=True)
        gem_wk_cell = f"{gem_wk}\n[dim]{gem_wk_rst}[/dim]" if gem_wk_rst else gem_wk

        cld_grp = pools.get("Claude and GPT models", {})
        cld_5hr = format_bar_str(cld_grp.get("5-hr", {}).get("pct"))
        cld_wk = format_bar_str(cld_grp.get("weekly", {}).get("pct"))

        argon_grp = pools.get("Gemini 4 Argon", {})
        argon_wk = format_bar_str(argon_grp.get("weekly", {}).get("pct"))
        argon_wk_rst = format_reset_time(argon_grp.get("weekly", {}).get("reset"), tz, compact=True)
        argon_cell = f"{argon_wk}\n[dim]{argon_wk_rst}[/dim]" if argon_wk_rst else argon_wk

        table.add_row(slot, email, gem_5hr_cell, gem_wk_cell, cld_5hr, cld_wk, argon_cell, status_str)

    console.print(table)


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
    """List all Antigravity profile instances, their paths, launcher commands, and remote daemons."""
    instances = list_instances()
    table = Table(
        title="Antigravity Profile Instances on this Machine",
        box=box.ROUNDED,
        border_style="cyan",
        header_style="bold cyan",
    )
    table.add_column("Instance", style="bold cyan")
    table.add_column("CLI Command", style="bold magenta")
    table.add_column("Active Account", style="bold white")
    table.add_column("Pool Slot", justify="center")
    table.add_column("Remote View", justify="center")
    table.add_column("Data Mode", justify="center")
    table.add_column("Storage Path (AppData)", style="dim")

    for inst in instances:
        slot_str = f"Slot {inst['matching_slot']}" if inst.get("matching_slot") else "[dim]--[/dim]"
        data_mode = "[green]Shared[/green]" if inst.get("shared_data") or inst.get("is_default") else "[cyan]Isolated[/cyan]"
        
        # Check systemd remote daemon status
        svc = "antigravity-cli-daemon.service" if inst["name"] == "primary" else f"antigravity-{inst['name']}-daemon.service"
        res = subprocess.run(["systemctl", "--user", "is-active", svc], capture_output=True, text=True)
        rc_status = "[bold green]● Active[/bold green]" if res.stdout.strip() == "active" else "[dim]Inactive[/dim]"

        table.add_row(
            inst["name"],
            inst["cli_cmd"],
            inst["email"],
            slot_str,
            rc_status,
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
    """Switch any instance's active account to a pool slot and reload remote daemon."""
    ok, msg = switch_instance_account(instance, slot)
    if ok:
        console.print(f"[bold green]✓ {msg}[/bold green]")
        svc = "antigravity-cli-daemon.service" if instance == "primary" else f"antigravity-{instance}-daemon.service"
        res = subprocess.run(["systemctl", "--user", "is-active", svc], capture_output=True, text=True)
        if res.stdout.strip() == "active":
            subprocess.run(["systemctl", "--user", "restart", svc])
            console.print(f"[bold cyan]✓ Restarted {svc} to apply new account credentials[/bold cyan]")
    else:
        console.print(f"[bold red]✗ {msg}[/bold red]")


@instance_app.command(name="remote-control")
def cmd_instance_remote_control(
    action: str = typer.Argument("status", help="Action: status, restart, start, stop"),
    name: str = typer.Option("subagents", "--name", "-n", help="Instance name (e.g. subagents, primary)"),
):
    """Manage remote-control background daemons for instances."""
    service = "antigravity-cli-daemon.service" if name == "primary" else f"antigravity-{name}-daemon.service"
    if action == "status":
        res = subprocess.run(["systemctl", "--user", "status", service], capture_output=True, text=True)
        console.print(res.stdout or res.stderr)
    elif action in ("restart", "start", "stop"):
        subprocess.run(["systemctl", "--user", action, service])
        res = subprocess.run(["systemctl", "--user", "is-active", service], capture_output=True, text=True)
        st = res.stdout.strip()
        console.print(f"[bold cyan]{service}: {st}[/bold cyan]")


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

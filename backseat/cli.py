"""
Backseat CLI — your phone does the work.
"""

import shlex
import sys
from pathlib import Path
from typing import Optional

import click
import httpx
import typer

# Some terminals (notably Windows' default console codepage) can't encode the
# unicode symbols (✓, ●, ○, —) used throughout this CLI's output.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from backseat.config import (
    BackseatConfig,
    BackseatError,
    PhoneConnection,
    SavedCommand,
    get_command,
    get_connection,
    load_config,
    save_config,
)
from backseat import health as agent
from backseat.ssh import SSHClient

app = typer.Typer(
    help="Backseat — your phone does the work.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
tunnel_app = typer.Typer(help="Manage Cloudflare tunnels on your phone.", no_args_is_help=True)
app.add_typer(tunnel_app, name="tunnel")

apps_app = typer.Typer(help="Manage long-running apps on your phone (auto-restart, logs).", no_args_is_help=True)
app.add_typer(apps_app, name="apps")

remote_app = typer.Typer(help="Reach your phone without being on the same WiFi.", no_args_is_help=True)
app.add_typer(remote_app, name="remote")

console = Console()
err_console = Console(stderr=True)


def _exit(msg: str) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    raise typer.Exit(1)


def _ok(msg: str) -> None:
    console.print(f"[bold green]✓[/bold green] {msg}")


def _default_app_name(remote: str) -> str:
    base = remote.rstrip("/").rsplit("/", 1)[-1] or "app"
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in base).strip("-")
    return safe or "app"


# ── init ───────────────────────────────────────────────────────────────────────

@app.command()
def init(
    name: str = typer.Option("phone", "--name", "-n", help="Name for this connection"),
):
    """Pair with your phone."""

    console.print(Panel(
        "[bold]Let's connect to your phone.[/bold]\n\n"
        "[dim]First time? Run this single command in Termux on your phone:[/dim]\n\n"
        "  [bold cyan]pkg install python openssh && pip install \"backseat\\[agent\\]\" && sshd && backseat-agent[/bold cyan]\n\n"
        "[dim]Already set up? Just run:[/dim]  [bold cyan]backseat-agent[/bold cyan]\n\n"
        "Your phone will show a QR code and a pairing code.\n"
        "Come back here when you see it.",
        title="[bold #7c6af7]backseat init[/]",
        border_style="#2a2a2a",
    ))
    console.print()
    typer.confirm("Phone is showing the pairing code — ready to continue?", abort=True)

    ip = typer.prompt("Phone IP address (e.g. 192.168.1.5)")
    port = typer.prompt("Agent port", default=8080)
    pairing_code = typer.prompt("Pairing code (6 characters from phone)")
    ssh_user = typer.prompt("SSH username (same as Termux whoami)")
    ssh_port = typer.prompt("SSH port", default=8022)
    auth_method = typer.prompt("SSH auth method", default="key", show_choices=True,
                               type=click.Choice(["key", "password"]))
    key_path = None
    if auth_method == "key":
        default_key = str(Path.home() / ".ssh" / "id_rsa")
        key_path = typer.prompt("Path to SSH private key", default=default_key)
        if not Path(key_path).expanduser().exists():
            console.print(f"[yellow]Warning:[/yellow] Key file not found at {key_path}. You can update it later.")

    # Check agent is reachable
    console.print(f"\n[dim]Checking agent at {ip}:{port}...[/dim]")
    dummy_conn = PhoneConnection(
        name=name, ip=ip, port=int(port), ssh_port=int(ssh_port),
        user=ssh_user, auth_method=auth_method, key_path=key_path,
    )
    if not agent.ping(dummy_conn):
        _exit(
            f"Cannot reach agent at {ip}:{port}.\n"
            "  Make sure python agent.py is running in Termux and you're on the same WiFi."
        )

    # Pair — exchange pairing code for session token
    console.print("[dim]Pairing...[/dim]")
    try:
        r = httpx.post(
            f"http://{ip}:{port}/pair",
            json={"pairing_token": pairing_code.upper().strip(), "ssh_user": ssh_user},
            timeout=10.0,
        )
        if r.status_code == 403:
            _exit("Invalid pairing code. Check the code shown on your phone.")
        r.raise_for_status()
        session_token = r.json()["session_token"]
    except httpx.ConnectError:
        _exit(f"Lost connection to agent at {ip}:{port}.")
    except httpx.HTTPStatusError as e:
        _exit(f"Pairing failed: {e.response.text}")

    conn = PhoneConnection(
        name=name,
        ip=ip,
        port=int(port),
        ssh_port=int(ssh_port),
        user=ssh_user,
        auth_method=auth_method,
        key_path=key_path,
        agent_token=session_token,
    )

    config = load_config()

    # Overwrite existing connection with same name
    config.connections = [c for c in config.connections if c.name != name]
    config.connections.append(conn)
    if config.default_connection is None:
        config.default_connection = name

    save_config(config)

    _ok(f"Paired and saved as [bold]{name}[/bold]")
    console.print(f"\n  [dim]Run[/dim] [bold]backseat status[/bold] [dim]to see your dashboard.[/dim]")

    if typer.confirm(
        "\nEnable remote access via Cloudflare, so backseat works even when you're "
        "not on the same WiFi as your phone?",
        default=False,
    ):
        try:
            status = agent.enable_remote(conn)
        except BackseatError as e:
            console.print(f"[yellow]Couldn't enable remote access: {e}[/yellow]")
            console.print("[dim]You can try again later with: backseat remote enable[/dim]")
        else:
            if status.url:
                conn.remote_url = status.url
                save_config(config)
                _ok("Remote access enabled")
                console.print(f"  [bold #7c6af7]{status.url}[/bold #7c6af7]")
            else:
                console.print("[yellow]Enabled, but no URL yet — run [bold]backseat remote status[/bold] shortly.[/yellow]")


# ── status ─────────────────────────────────────────────────────────────────────

@app.command()
def status(
    connection: Optional[str] = typer.Option(None, "--connection", "-c", help="Connection name"),
):
    """Live terminal dashboard: CPU, RAM, uptime, apps, tunnel, processes."""
    try:
        conn = get_connection(connection)
    except BackseatError as e:
        _exit(str(e))

    from backseat.dashboard import run_dashboard
    run_dashboard(conn)


# ── deploy ─────────────────────────────────────────────────────────────────────

@app.command()
def deploy(
    local: Path = typer.Argument(..., help="Local file or folder to deploy"),
    remote: str = typer.Argument(..., help="Remote path on phone (e.g. ~/myapp)"),
    start: Optional[str] = typer.Option(None, "--start", "-s", help="Command to run after deploy"),
    name: Optional[str] = typer.Option(None, "--name", help="App name for a managed --start (default: derived from remote path)"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c", help="Connection name"),
    foreground: bool = typer.Option(
        False, "--foreground",
        help="Run --start once over SSH and show its output, instead of registering it as a supervised app",
    ),
):
    """Deploy a file or folder to your phone via SSH.

    With --start, the command is registered as a managed app on the agent:
    it keeps running after this command exits, restarts automatically if it
    crashes, and its output goes to `backseat apps logs`. Use --foreground
    for a one-off command instead (e.g. a build step) that shouldn't persist.
    """
    if not local.exists():
        _exit(f"Local path not found: {local}")

    try:
        conn = get_connection(connection)
    except BackseatError as e:
        _exit(str(e))

    password = None
    if conn.auth_method == "password":
        password = typer.prompt("SSH password", hide_input=True)

    console.print(f"\n[dim]Connecting to[/dim] [bold]{conn.name}[/bold] [dim]({conn.ip})...[/dim]")

    try:
        with SSHClient(conn, password=password) as ssh:
            console.print(f"[dim]Uploading[/dim] [bold]{local}[/bold] [dim]→[/dim] [bold]{remote}[/bold]")

            from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            ) as prog:
                task = prog.add_task("Uploading...", total=None)
                uploaded = ssh.upload(local, remote)
                prog.update(task, description=f"Uploaded {len(uploaded)} file(s)")

            _ok(f"Uploaded {len(uploaded)} file(s) to {remote}")

            if start and foreground:
                console.print(f"[dim]Running once:[/dim] {start}")
                safe_remote = shlex.quote(remote)
                out, err, code = ssh.run(f"cd {safe_remote} && {start}")
                if out.strip():
                    console.print(out.strip())
                if err.strip():
                    console.print(f"[yellow]{err.strip()}[/yellow]")
                if code != 0:
                    _exit(f"Start command exited with code {code}")
                _ok("Start command completed")
            elif start:
                app_name = name or _default_app_name(remote)
                console.print(f"[dim]Registering managed app[/dim] [bold]{app_name}[/bold][dim]:[/dim] {start}")
                agent.create_app(conn, name=app_name, command=start, cwd=remote)
                _ok(f"App [bold]{app_name}[/bold] started — it will auto-restart if it crashes")
                console.print(f"  [dim]Logs:[/dim] backseat apps logs {app_name}")

    except BackseatError as e:
        _exit(str(e))


# ── run ────────────────────────────────────────────────────────────────────────

@app.command()
def run(
    name: str = typer.Argument(..., help="Saved command name"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Run a saved command on the phone."""
    try:
        conn = get_connection(connection)
        cmd = get_command(name)
    except BackseatError as e:
        _exit(str(e))

    console.print(f"[dim]Running:[/dim] [bold]{cmd.command}[/bold]\n")

    # Try HTTP agent first, fall back to SSH
    try:
        result = agent.run_command(conn, cmd.command)
        if result.stdout.strip():
            console.print(result.stdout.strip())
        if result.stderr.strip():
            console.print(f"[yellow]{result.stderr.strip()}[/yellow]")
        if result.returncode != 0:
            _exit(f"Command exited with code {result.returncode}")
        return
    except BackseatError:
        console.print("[dim]Agent unreachable, falling back to SSH...[/dim]")

    # SSH fallback
    password = None
    if conn.auth_method == "password":
        password = typer.prompt("SSH password", hide_input=True)

    try:
        with SSHClient(conn, password=password) as ssh:
            out, err, code = ssh.run(cmd.command)
            if out.strip():
                console.print(out.strip())
            if err.strip():
                console.print(f"[yellow]{err.strip()}[/yellow]")
            if code != 0:
                _exit(f"Command exited with code {code}")
    except BackseatError as e:
        _exit(str(e))


# ── add ────────────────────────────────────────────────────────────────────────

@app.command()
def add(
    name: str = typer.Argument(..., help="Name for the command"),
    command: Optional[str] = typer.Option(None, "--command", "-cmd", help="The shell command"),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
):
    """Save a command for later use with [bold]backseat run[/bold]."""
    if not command:
        command = typer.prompt("Command to save")
    if not description:
        description = typer.prompt("Description (optional)", default="")

    config = load_config()
    existing = next((c for c in config.commands if c.name == name), None)
    if existing:
        overwrite = typer.confirm(f"Command '{name}' already exists. Overwrite?")
        if not overwrite:
            raise typer.Abort()
        config.commands = [c for c in config.commands if c.name != name]

    config.commands.append(SavedCommand(
        name=name,
        command=command,
        description=description or None,
    ))
    save_config(config)
    _ok(f"Saved command [bold]{name}[/bold]")


# ── list ───────────────────────────────────────────────────────────────────────

@app.command(name="list")
def list_commands():
    """List all saved commands."""
    config = load_config()
    if not config.commands:
        console.print("[dim]No saved commands. Use [bold]backseat add <name>[/bold] to create one.[/dim]")
        return

    table = Table(show_header=True, header_style="bold #7c6af7", box=None, padding=(0, 2))
    table.add_column("Name", style="bold white")
    table.add_column("Command", style="cyan")
    table.add_column("Description", style="dim")

    for cmd in config.commands:
        table.add_row(cmd.name, cmd.command, cmd.description or "")

    console.print(table)


# ── connections ────────────────────────────────────────────────────────────────

@app.command()
def connections():
    """List saved phone connections."""
    config = load_config()
    if not config.connections:
        console.print("[dim]No connections. Run [bold]backseat init[/bold] to pair a phone.[/dim]")
        return

    table = Table(show_header=True, header_style="bold #7c6af7", box=None, padding=(0, 2))
    table.add_column("Name", style="bold white")
    table.add_column("IP", style="cyan")
    table.add_column("Port")
    table.add_column("SSH User")
    table.add_column("Auth")
    table.add_column("Default", justify="center")

    for c in config.connections:
        is_default = "●" if c.name == config.default_connection else ""
        table.add_row(c.name, c.ip, str(c.port), c.user, c.auth_method, is_default)

    console.print(table)


# ── remove ─────────────────────────────────────────────────────────────────────

@app.command()
def remove(
    name: str = typer.Argument(..., help="Command name to remove"),
):
    """Remove a saved command."""
    config = load_config()
    before = len(config.commands)
    config.commands = [c for c in config.commands if c.name != name]
    if len(config.commands) == before:
        _exit(f"No command named '{name}'.")
    save_config(config)
    _ok(f"Removed command [bold]{name}[/bold]")


# ── tunnel subcommands ─────────────────────────────────────────────────────────

@tunnel_app.command("start")
def tunnel_start(
    port: int = typer.Argument(..., help="Local port on the phone to expose"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Start a Cloudflare quick tunnel for a port on your phone."""
    try:
        conn = get_connection(connection)
    except BackseatError as e:
        _exit(str(e))

    console.print(f"[dim]Starting tunnel for port {port}...[/dim]")

    from rich.progress import Progress, SpinnerColumn, TextColumn
    try:
        with Progress(SpinnerColumn(), TextColumn("{task.description}"), transient=True, console=console) as prog:
            prog.add_task("Waiting for Cloudflare URL (up to 10s)...")
            tunnel = agent.start_tunnel(conn, port)

        if tunnel.url:
            _ok(f"Tunnel active")
            console.print(f"\n  [bold #7c6af7]{tunnel.url}[/bold #7c6af7]  [dim]→ phone:{port}[/dim]\n")
        else:
            console.print("[yellow]Tunnel started but URL not yet available. Run [bold]backseat tunnel status[/bold].[/yellow]")
    except BackseatError as e:
        _exit(str(e))


@tunnel_app.command("stop")
def tunnel_stop(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Stop the active Cloudflare tunnel."""
    try:
        conn = get_connection(connection)
        agent.stop_tunnel(conn)
        _ok("Tunnel stopped")
    except BackseatError as e:
        _exit(str(e))


@tunnel_app.command("status")
def tunnel_status_cmd(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Show current Cloudflare tunnel status."""
    try:
        conn = get_connection(connection)
        t = agent.get_tunnel_status(conn)
    except BackseatError as e:
        _exit(str(e))

    if t.active and t.mode == "named":
        console.print(f"[bold green]● Active[/bold green]  named tunnel ({len(t.routes or [])} hosts)")
        for route in t.routes or []:
            dot = "[bold green]●[/bold green]" if route.live else "[dim red]○[/dim red]"
            style = "bold #7c6af7" if route.live else "dim"
            port = f"  :{route.port}" if route.port else ""
            console.print(f"  {dot} [{style}]{route.hostname}[/{style}]{port}")
    elif t.active:
        console.print(f"[bold green]● Active[/bold green]  port {t.port}")
        if t.url:
            console.print(f"  [bold #7c6af7]{t.url}[/bold #7c6af7]")
    else:
        console.print("[dim]○ No active tunnel[/dim]")
        console.print(f"  Start one: [bold]backseat tunnel start <port>[/bold]")


# ── remote subcommands ─────────────────────────────────────────────────────────

@remote_app.command("enable")
def remote_enable_cmd(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Expose the agent itself via a Cloudflare quick tunnel, so backseat works
    even when you're not on the same WiFi as your phone."""
    try:
        conn = get_connection(connection)
        console.print("[dim]Enabling remote access (this survives agent restarts)...[/dim]")
        status = agent.enable_remote(conn)
    except BackseatError as e:
        _exit(str(e))

    if not status.url:
        console.print("[yellow]Enabled, but no URL yet. Run [bold]backseat remote status[/bold] shortly.[/yellow]")
        return

    config = load_config()
    for c in config.connections:
        if c.name == conn.name:
            c.remote_url = status.url
    save_config(config)

    _ok("Remote access enabled")
    console.print(f"\n  [bold #7c6af7]{status.url}[/bold #7c6af7]\n")
    console.print("[dim]backseat will now automatically use this whenever the phone isn't reachable on LAN.[/dim]")
    console.print("[dim]Note: quick tunnels get a new URL each time the agent restarts — run [/dim]"
                  "[bold]backseat remote sync[/bold][dim] after a restart to refresh it.[/dim]")


@remote_app.command("disable")
def remote_disable_cmd(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Stop exposing the agent remotely."""
    try:
        conn = get_connection(connection)
        agent.disable_remote(conn)
    except BackseatError as e:
        _exit(str(e))

    config = load_config()
    for c in config.connections:
        if c.name == conn.name:
            c.remote_url = None
    save_config(config)

    _ok("Remote access disabled")


@remote_app.command("status")
def remote_status_cmd(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Show whether remote access is active."""
    try:
        conn = get_connection(connection)
        status = agent.get_remote_status(conn)
    except BackseatError as e:
        _exit(str(e))

    if status.active and status.url:
        console.print(f"[bold green]● Active[/bold green]  {status.url}")
    elif status.active:
        console.print("[yellow]● Starting up — no URL yet. Try again shortly.[/yellow]")
    else:
        console.print("[dim]○ Remote access not enabled[/dim]")
        console.print("  Enable it: [bold]backseat remote enable[/bold]")


@remote_app.command("sync")
def remote_sync_cmd(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Refresh the saved remote URL — quick tunnels get a new one on every
    agent restart, so run this (while on LAN) to pick up the current one."""
    try:
        conn = get_connection(connection)
        status = agent.get_remote_status(conn)
    except BackseatError as e:
        _exit(str(e))

    if not status.active or not status.url:
        _exit("Remote access isn't active. Run: backseat remote enable")

    config = load_config()
    for c in config.connections:
        if c.name == conn.name:
            c.remote_url = status.url
    save_config(config)

    _ok(f"Synced — {status.url}")


# ── apps subcommands ───────────────────────────────────────────────────────────

_STATUS_STYLE = {"running": "bold green", "crashed": "bold red", "stopped": "dim"}


def _fmt_uptime(seconds: Optional[int]) -> str:
    if seconds is None:
        return "—"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


@apps_app.command("list")
def apps_list_cmd(
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """List managed apps and their status."""
    try:
        conn = get_connection(connection)
        apps = agent.list_apps(conn)
    except BackseatError as e:
        _exit(str(e))

    if not apps:
        console.print("[dim]No managed apps. Use [bold]backseat deploy <local> <remote> --start \"<cmd>\"[/bold] to create one.[/dim]")
        return

    table = Table(show_header=True, header_style="bold #7c6af7", box=None, padding=(0, 2))
    table.add_column("Name", style="bold white")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("Uptime")
    table.add_column("Restarts", justify="right")
    table.add_column("Command", style="dim")

    for a in apps:
        style = _STATUS_STYLE.get(a.status, "white")
        table.add_row(
            a.name,
            f"[{style}]{a.status}[/{style}]",
            str(a.pid) if a.pid else "—",
            _fmt_uptime(a.uptime_seconds),
            str(a.restart_count),
            a.command,
        )

    console.print(table)


@apps_app.command("start")
def apps_start_cmd(
    name: str = typer.Argument(..., help="App name"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Start a stopped app (also clears its restart backoff)."""
    try:
        conn = get_connection(connection)
        agent.start_app(conn, name)
        _ok(f"Started [bold]{name}[/bold]")
    except BackseatError as e:
        _exit(str(e))


@apps_app.command("stop")
def apps_stop_cmd(
    name: str = typer.Argument(..., help="App name"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Stop an app. It will not be auto-restarted until you start it again."""
    try:
        conn = get_connection(connection)
        agent.stop_app(conn, name)
        _ok(f"Stopped [bold]{name}[/bold]")
    except BackseatError as e:
        _exit(str(e))


@apps_app.command("restart")
def apps_restart_cmd(
    name: str = typer.Argument(..., help="App name"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Restart an app and reset its crash-backoff counter."""
    try:
        conn = get_connection(connection)
        agent.restart_app(conn, name)
        _ok(f"Restarted [bold]{name}[/bold]")
    except BackseatError as e:
        _exit(str(e))


@apps_app.command("remove")
def apps_remove_cmd(
    name: str = typer.Argument(..., help="App name"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Stop an app and forget it (deletes its logs too)."""
    try:
        conn = get_connection(connection)
        agent.remove_app(conn, name)
        _ok(f"Removed [bold]{name}[/bold]")
    except BackseatError as e:
        _exit(str(e))


@apps_app.command("logs")
def apps_logs_cmd(
    name: str = typer.Argument(..., help="App name"),
    lines: int = typer.Option(200, "--lines", "-n", help="Number of recent lines to show"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new log lines as they're written"),
    connection: Optional[str] = typer.Option(None, "--connection", "-c"),
):
    """Show (or follow) an app's combined stdout/stderr log."""
    try:
        conn = get_connection(connection)
        if not follow:
            for line in agent.get_app_logs(conn, name, lines=lines):
                console.print(line)
            return

        console.print(f"[dim]Following logs for[/dim] [bold]{name}[/bold] [dim](Ctrl+C to stop)[/dim]\n")
        try:
            for line in agent.stream_app_logs(conn, name):
                console.print(line)
        except KeyboardInterrupt:
            pass
    except BackseatError as e:
        _exit(str(e))


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

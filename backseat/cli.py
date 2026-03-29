"""
Backseat CLI — your phone does the work.
"""

import shlex
import sys
from pathlib import Path
from typing import Optional

import httpx
import typer
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

console = Console()
err_console = Console(stderr=True)


def _exit(msg: str) -> None:
    err_console.print(f"[bold red]Error:[/bold red] {msg}")
    raise typer.Exit(1)


def _ok(msg: str) -> None:
    console.print(f"[bold green]✓[/bold green] {msg}")


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
                               type=typer.Choice(["key", "password"]))
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


# ── status ─────────────────────────────────────────────────────────────────────

@app.command()
def status(
    connection: Optional[str] = typer.Option(None, "--connection", "-c", help="Connection name"),
):
    """Live terminal dashboard: CPU, RAM, uptime, tunnel, processes."""
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
    connection: Optional[str] = typer.Option(None, "--connection", "-c", help="Connection name"),
    background: bool = typer.Option(True, "--background/--foreground", help="Run start command in background"),
):
    """Deploy a file or folder to your phone via SSH."""
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

            if start:
                console.print(f"[dim]Running:[/dim] {start}")
                safe_remote = shlex.quote(remote)
                if background:
                    ssh.run_background(f"cd {safe_remote} && {start}")
                    _ok(f"Started in background: {start}")
                else:
                    out, err, code = ssh.run(f"cd {safe_remote} && {start}")
                    if out.strip():
                        console.print(out.strip())
                    if err.strip():
                        console.print(f"[yellow]{err.strip()}[/yellow]")
                    if code != 0:
                        _exit(f"Start command exited with code {code}")
                    else:
                        _ok("Start command completed")

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

    if t.active:
        console.print(f"[bold green]● Active[/bold green]  port {t.port}")
        if t.url:
            console.print(f"  [bold #7c6af7]{t.url}[/bold #7c6af7]")
    else:
        console.print("[dim]○ No active tunnel[/dim]")
        console.print(f"  Start one: [bold]backseat tunnel start <port>[/bold]")


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()

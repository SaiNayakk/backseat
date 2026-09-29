"""
Rich terminal dashboard for backseat status command.
Polls the phone agent every 2 seconds and renders a live view.
"""

import time
from datetime import datetime
from typing import Optional

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from backseat.config import PhoneConnection, BackseatError
from backseat.health import AppInfo, HealthSnapshot, TunnelStatus, get_health, get_tunnel_status, list_apps

console = Console()


def fmt_uptime(seconds: int) -> str:
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def bar_color(pct: float) -> str:
    if pct >= 85:
        return "red"
    if pct >= 65:
        return "yellow"
    return "green"


def _stat_panel(snap: HealthSnapshot) -> Panel:
    p = Progress(TextColumn("{task.description}", style="bold white"), BarColumn(bar_width=20), TextColumn("{task.fields[value]}"))

    cpu_task = p.add_task("CPU    ", total=100, value=f"[{bar_color(snap.cpu_percent)}]{snap.cpu_percent:.0f}%[/]")
    p.update(cpu_task, completed=snap.cpu_percent)

    ram_task = p.add_task("RAM    ", total=100, value=f"[{bar_color(snap.ram_percent)}]{snap.ram_percent:.0f}%[/]  {snap.ram_used_mb}/{snap.ram_total_mb} MB")
    p.update(ram_task, completed=snap.ram_percent)

    disk_task = p.add_task("Storage", total=100, value=f"[{bar_color(snap.storage_percent)}]{snap.storage_percent:.0f}%[/]  {snap.storage_used_gb}/{snap.storage_total_gb} GB")
    p.update(disk_task, completed=snap.storage_percent)

    uptime_text = Text()
    uptime_text.append("\n")
    uptime_text.append("Uptime   ", style="bold white")
    uptime_text.append(fmt_uptime(snap.uptime_seconds), style="cyan")
    uptime_text.append("     ")
    uptime_text.append("Requests ", style="bold white")
    uptime_text.append(str(snap.request_count), style="cyan")

    from rich.console import Group
    return Panel(Group(p, uptime_text), title="[bold #7c6af7]System[/]", border_style="#2a2a2a")


def _process_table(snap: HealthSnapshot) -> Panel:
    table = Table(show_header=True, header_style="bold #888888", box=None, padding=(0, 1))
    table.add_column("PID", style="dim", width=7)
    table.add_column("Name", style="white", no_wrap=True)
    table.add_column("CPU%", justify="right", style="yellow")
    table.add_column("MEM%", justify="right", style="blue")

    for p in snap.processes[:8]:
        cpu_style = "red" if p.cpu_percent > 50 else "yellow" if p.cpu_percent > 20 else "white"
        table.add_row(
            str(p.pid),
            p.name[:24],
            f"[{cpu_style}]{p.cpu_percent:.1f}[/]",
            f"{p.mem_percent:.1f}",
        )

    return Panel(table, title="[bold #7c6af7]Processes[/]", border_style="#2a2a2a")


_APP_STATUS_STYLE = {"running": "green", "crashed": "red", "stopped": "dim"}


def _apps_panel(apps: Optional[list[AppInfo]]) -> Panel:
    if not apps:
        text = Text("No managed apps.", style="dim")
        text.append("\n\nDeploy one with: ", style="dim")
        text.append("backseat deploy <local> <remote> --start \"<cmd>\"", style="bold white")
        return Panel(text, title="[bold #7c6af7]Apps[/]", border_style="#2a2a2a")

    table = Table(show_header=True, header_style="bold #888888", box=None, padding=(0, 1))
    table.add_column("Name", style="white", no_wrap=True)
    table.add_column("Status")
    table.add_column("Uptime", justify="right")
    table.add_column("Restarts", justify="right", style="dim")

    for a in apps:
        style = _APP_STATUS_STYLE.get(a.status, "white")
        table.add_row(
            a.name,
            f"[{style}]{a.status}[/{style}]",
            fmt_uptime(a.uptime_seconds) if a.uptime_seconds is not None else "—",
            str(a.restart_count),
        )

    return Panel(table, title="[bold #7c6af7]Apps[/]", border_style="#2a2a2a")


def _tunnel_panel(tunnel: Optional[TunnelStatus]) -> Panel:
    if tunnel is None or not tunnel.active:
        text = Text("○  No active tunnel", style="dim")
        text.append("\n\nStart one with: ", style="dim")
        text.append("backseat tunnel start <port>", style="bold white")
        return Panel(text, title="[bold #7c6af7]Cloudflare Tunnel[/]", border_style="#2a2a2a")

    if tunnel.mode == "named":
        if not tunnel.routes:
            text = Text("● Active", style="bold green")
            text.append("  (named tunnel, no hostnames configured)", style="dim")
            return Panel(text, title="[bold #7c6af7]Cloudflare Tunnel[/]", border_style="#2a2a2a")

        table = Table(show_header=True, header_style="bold #888888", box=None, padding=(0, 1))
        table.add_column("", width=1)
        table.add_column("Hostname", style="white", no_wrap=True)
        table.add_column("Port", justify="right", style="dim")
        for route in tunnel.routes:
            dot_style = "bold green" if route.live else "dim red"
            table.add_row(
                f"[{dot_style}]●[/{dot_style}]",
                route.hostname if route.live else f"[dim]{route.hostname}[/dim]",
                str(route.port) if route.port else "—",
            )
        return Panel(table, title="[bold #7c6af7]Cloudflare Tunnel[/]", border_style="#2a2a2a")

    text = Text("● Active", style="bold green")
    text.append(f"  →  port {tunnel.port}\n\n", style="white")
    if tunnel.url:
        text.append(tunnel.url, style="bold #7c6af7 underline")
    else:
        text.append("Waiting for URL...", style="dim")
    return Panel(text, title="[bold #7c6af7]Cloudflare Tunnel[/]", border_style="#2a2a2a")


def _header(conn: PhoneConnection, last_updated: str, error: Optional[str]) -> Panel:
    left = Text()
    left.append("⬡ BACKSEAT", style="bold #7c6af7")
    left.append(f"  {conn.name}", style="white")
    left.append(f"  {conn.ip}:{conn.port}", style="dim")

    right = Text(justify="right")
    if error:
        right.append(f"⚠  {error}", style="red")
    else:
        right.append(f"Updated {last_updated}", style="dim")

    from rich.table import Table as RTable
    grid = RTable.grid(expand=True)
    grid.add_column()
    grid.add_column(justify="right")
    grid.add_row(left, right)

    return Panel(grid, border_style="#2a2a2a")


def run_dashboard(conn: PhoneConnection) -> None:
    """Entry point — start the live dashboard. Exits cleanly on Ctrl+C."""
    last_snap: Optional[HealthSnapshot] = None
    last_tunnel: Optional[TunnelStatus] = None
    last_apps: Optional[list[AppInfo]] = None
    last_updated = "—"
    last_error: Optional[str] = None

    def build() -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="footer", size=1),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right", ratio=1),
        )
        layout["left"].split_column(
            Layout(name="stats"),
            Layout(name="tunnel", size=11),
        )
        layout["right"].split_column(
            Layout(name="processes"),
            Layout(name="apps", size=8),
        )

        if last_snap:
            layout["header"].update(_header(conn, last_updated, last_error))
            layout["stats"].update(_stat_panel(last_snap))
            layout["processes"].update(_process_table(last_snap))
            layout["tunnel"].update(_tunnel_panel(last_tunnel))
            layout["apps"].update(_apps_panel(last_apps))
        else:
            msg = last_error or "Connecting..."
            layout["header"].update(Panel(f"[dim]{msg}[/]"))
            layout["stats"].update(Panel(""))
            layout["processes"].update(Panel(""))
            layout["tunnel"].update(Panel(""))
            layout["apps"].update(Panel(""))

        layout["footer"].update(
            Text("  [dim]Ctrl+C[/dim] to exit", justify="left")
        )
        return layout

    try:
        with Live(build(), refresh_per_second=1, screen=True) as live:
            while True:
                try:
                    last_snap = get_health(conn)
                    last_tunnel = get_tunnel_status(conn)
                    last_apps = list_apps(conn)
                    last_updated = datetime.now().strftime("%H:%M:%S")
                    last_error = None
                except BackseatError as e:
                    lines = str(e).splitlines()
                    last_error = lines[0] if lines else "Unknown error"
                except Exception as e:
                    last_error = f"Unexpected error: {e}"

                live.update(build())
                time.sleep(2)

    except KeyboardInterrupt:
        console.print("\n[dim]Dashboard closed.[/dim]")

"""
HTTP client for the Backseat phone agent.
Fetches health stats and runs commands via the agent API.
"""

import socket
from typing import Iterator, Optional

import httpx
from pydantic import BaseModel

from backseat.config import PhoneConnection, BackseatError


# ── Response models (mirror agent.py) ─────────────────────────────────────────

class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    mem_percent: float


class HealthSnapshot(BaseModel):
    cpu_percent: float
    ram_percent: float
    ram_used_mb: int
    ram_total_mb: int
    storage_percent: float
    storage_used_gb: float
    storage_total_gb: float
    uptime_seconds: int
    request_count: int
    processes: list[ProcessInfo]
    timestamp: str


class CommandResult(BaseModel):
    stdout: str
    stderr: str
    returncode: int


class TunnelRoute(BaseModel):
    hostname: str
    port: Optional[int] = None
    live: bool = False


class TunnelStatus(BaseModel):
    active: bool
    url: Optional[str] = None
    port: Optional[int] = None
    mode: Optional[str] = None  # "quick" (backseat tunnel start) or "named" (cloudflared config.yml as a managed app)
    routes: Optional[list[TunnelRoute]] = None


class RemoteStatus(BaseModel):
    active: bool
    url: Optional[str] = None


class AppInfo(BaseModel):
    name: str
    command: str
    cwd: Optional[str] = None
    status: str
    pid: Optional[int] = None
    uptime_seconds: Optional[int] = None
    restart_count: int = 0
    desired_state: str


# ── Client ─────────────────────────────────────────────────────────────────────

def _lan_reachable(conn: PhoneConnection, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((conn.ip, conn.port), timeout=timeout):
            return True
    except OSError:
        return False


def _sync_remote_url(conn: PhoneConnection) -> None:
    """Best-effort: called whenever LAN just succeeded. Quick tunnels get a new
    URL every time the agent restarts, so without this, the saved remote_url
    silently goes stale and remote access breaks the next time you're off
    WiFi. Refreshes and persists it now, while we can still reach the agent."""
    try:
        r = httpx.get(
            f"http://{conn.ip}:{conn.port}/remote/status",
            headers=_headers(conn),
            timeout=2.0,
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError:
        return
    new_url = data.get("url") if data.get("active") else None
    if not new_url or new_url == conn.remote_url:
        return
    from backseat.config import load_config, save_config
    cfg = load_config()
    for c in cfg.connections:
        if c.name == conn.name:
            c.remote_url = new_url
    save_config(cfg)
    conn.remote_url = new_url


def _base_url(conn: PhoneConnection) -> str:
    """LAN address by default; falls back to the agent's own remote quick-tunnel
    URL (see backseat remote enable) when the phone isn't reachable on LAN."""
    if _lan_reachable(conn):
        _sync_remote_url(conn)
        return f"http://{conn.ip}:{conn.port}"
    if conn.remote_url:
        return conn.remote_url.rstrip("/")
    return f"http://{conn.ip}:{conn.port}"


def _headers(conn: PhoneConnection) -> dict:
    return {"x-backseat-token": conn.agent_token or ""}


def _agent_unreachable(conn: PhoneConnection, extra: str = "") -> BackseatError:
    """Builds an accurate unreachable message — whether the LAN address or the
    remote tunnel was actually attempted, not just always the LAN one."""
    if conn.remote_url and not _lan_reachable(conn):
        msg = (
            f"Agent unreachable via remote tunnel ({conn.remote_url}).\n"
            "Check your phone still has internet access, or run [bold]backseat remote sync[/bold] "
            "next time you're on the same WiFi."
        )
    else:
        msg = (
            f"Agent unreachable at {conn.ip}:{conn.port}.\n"
            "Start it in Termux: [bold]backseat-agent[/bold]"
        )
    return BackseatError(msg + (f"\n{extra}" if extra else ""))


def _agent_timeout(conn: PhoneConnection) -> BackseatError:
    if conn.remote_url and not _lan_reachable(conn):
        return BackseatError(f"Agent timed out via remote tunnel ({conn.remote_url}).")
    return BackseatError(f"Agent timed out at {conn.ip}:{conn.port}.")


def ping(conn: PhoneConnection) -> bool:
    """Check if agent is reachable. Returns True/False."""
    try:
        r = httpx.get(f"{_base_url(conn)}/ping", timeout=6.0)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


def get_health(conn: PhoneConnection) -> HealthSnapshot:
    try:
        r = httpx.get(
            f"{_base_url(conn)}/health",
            headers=_headers(conn),
            timeout=10.0,
        )
        r.raise_for_status()
        return HealthSnapshot.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def run_command(conn: PhoneConnection, command: str) -> CommandResult:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/run",
            headers=_headers(conn),
            json={"command": command},
            timeout=60.0,
        )
        r.raise_for_status()
        return CommandResult.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise BackseatError("Command timed out after 60 seconds.")
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def get_tunnel_status(conn: PhoneConnection) -> TunnelStatus:
    try:
        r = httpx.get(
            f"{_base_url(conn)}/tunnel/status",
            headers=_headers(conn),
            timeout=8.0,
        )
        r.raise_for_status()
        return TunnelStatus.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def start_tunnel(conn: PhoneConnection, port: int) -> TunnelStatus:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/tunnel/start",
            headers=_headers(conn),
            json={"port": port},
            timeout=30.0,
        )
        r.raise_for_status()
        return TunnelStatus.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def stop_tunnel(conn: PhoneConnection) -> None:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/tunnel/stop",
            headers=_headers(conn),
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


# ── Remote access ────────────────────────────────────────────────────────────────
# A dedicated quick tunnel exposing the agent itself, so `backseat` can reach it
# without being on the same WiFi. Independent of the per-app tunnel above.

def get_remote_status(conn: PhoneConnection) -> RemoteStatus:
    try:
        r = httpx.get(
            f"{_base_url(conn)}/remote/status",
            headers=_headers(conn),
            timeout=8.0,
        )
        r.raise_for_status()
        return RemoteStatus.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def enable_remote(conn: PhoneConnection) -> RemoteStatus:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/remote/enable",
            headers=_headers(conn),
            timeout=15.0,
        )
        r.raise_for_status()
        return RemoteStatus.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def disable_remote(conn: PhoneConnection) -> None:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/remote/disable",
            headers=_headers(conn),
            timeout=10.0,
        )
        r.raise_for_status()
    except httpx.ConnectError:
        raise _agent_unreachable(conn)
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


# ── Apps ───────────────────────────────────────────────────────────────────────
# Managed long-running processes on the phone: auto-restarted on crash, logged
# to disk, persisted across agent restarts. See backseat/agent.py's /apps routes.

def create_app(conn: PhoneConnection, name: str, command: str, cwd: Optional[str] = None) -> AppInfo:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/apps",
            headers=_headers(conn),
            json={"name": name, "command": command, "cwd": cwd},
            timeout=15.0,
        )
        if r.status_code == 409:
            raise BackseatError(r.json().get("detail", f"App '{name}' already exists."))
        r.raise_for_status()
        return AppInfo.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(
            conn, "Managed apps require the agent to be running — they aren't available over plain SSH."
        )
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def list_apps(conn: PhoneConnection) -> list[AppInfo]:
    try:
        r = httpx.get(f"{_base_url(conn)}/apps", headers=_headers(conn), timeout=8.0)
        r.raise_for_status()
        return [AppInfo.model_validate(a) for a in r.json()]
    except httpx.ConnectError:
        raise _agent_unreachable(
            conn, "Managed apps require the agent to be running — they aren't available over plain SSH."
        )
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def _apps_action(conn: PhoneConnection, name: str, action: str) -> AppInfo:
    try:
        r = httpx.post(
            f"{_base_url(conn)}/apps/{name}/{action}",
            headers=_headers(conn),
            timeout=15.0,
        )
        if r.status_code == 404:
            raise BackseatError(f"No app named '{name}'. Run [bold]backseat apps list[/bold].")
        r.raise_for_status()
        return AppInfo.model_validate(r.json())
    except httpx.ConnectError:
        raise _agent_unreachable(
            conn, "Managed apps require the agent to be running — they aren't available over plain SSH."
        )
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def start_app(conn: PhoneConnection, name: str) -> AppInfo:
    return _apps_action(conn, name, "start")


def stop_app(conn: PhoneConnection, name: str) -> AppInfo:
    return _apps_action(conn, name, "stop")


def restart_app(conn: PhoneConnection, name: str) -> AppInfo:
    return _apps_action(conn, name, "restart")


def remove_app(conn: PhoneConnection, name: str) -> None:
    try:
        r = httpx.delete(f"{_base_url(conn)}/apps/{name}", headers=_headers(conn), timeout=10.0)
        if r.status_code == 404:
            raise BackseatError(f"No app named '{name}'. Run [bold]backseat apps list[/bold].")
        r.raise_for_status()
    except httpx.ConnectError:
        raise _agent_unreachable(
            conn, "Managed apps require the agent to be running — they aren't available over plain SSH."
        )
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def get_app_logs(conn: PhoneConnection, name: str, lines: int = 200) -> list[str]:
    try:
        r = httpx.get(
            f"{_base_url(conn)}/apps/{name}/logs",
            headers=_headers(conn),
            params={"lines": lines},
            timeout=10.0,
        )
        if r.status_code == 404:
            raise BackseatError(f"No app named '{name}'. Run [bold]backseat apps list[/bold].")
        r.raise_for_status()
        return r.json()["lines"]
    except httpx.ConnectError:
        raise _agent_unreachable(
            conn, "Managed apps require the agent to be running — they aren't available over plain SSH."
        )
    except httpx.TimeoutException:
        raise _agent_timeout(conn)
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def stream_app_logs(conn: PhoneConnection, name: str) -> Iterator[str]:
    """Yields new log lines as they're written. Blocks until interrupted."""
    try:
        with httpx.stream(
            "GET",
            f"{_base_url(conn)}/apps/{name}/logs",
            headers=_headers(conn),
            params={"follow": "1"},
            timeout=None,
        ) as r:
            if r.status_code == 404:
                raise BackseatError(f"No app named '{name}'. Run [bold]backseat apps list[/bold].")
            r.raise_for_status()
            for line in r.iter_lines():
                yield line
    except httpx.ConnectError:
        raise _agent_unreachable(
            conn, "Managed apps require the agent to be running — they aren't available over plain SSH."
        )
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")

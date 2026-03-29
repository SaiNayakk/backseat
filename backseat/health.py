"""
HTTP client for the Backseat phone agent.
Fetches health stats and runs commands via the agent API.
"""

from typing import Optional

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


class TunnelStatus(BaseModel):
    active: bool
    url: Optional[str] = None
    port: Optional[int] = None


# ── Client ─────────────────────────────────────────────────────────────────────

def _base_url(conn: PhoneConnection) -> str:
    return f"http://{conn.ip}:{conn.port}"


def _headers(conn: PhoneConnection) -> dict:
    return {"x-backseat-token": conn.agent_token or ""}


def ping(conn: PhoneConnection) -> bool:
    """Check if agent is reachable. Returns True/False."""
    try:
        r = httpx.get(f"{_base_url(conn)}/ping", timeout=3.0)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException):
        return False


def get_health(conn: PhoneConnection) -> HealthSnapshot:
    try:
        r = httpx.get(
            f"{_base_url(conn)}/health",
            headers=_headers(conn),
            timeout=5.0,
        )
        r.raise_for_status()
        return HealthSnapshot.model_validate(r.json())
    except httpx.ConnectError:
        raise BackseatError(
            f"Agent unreachable at {conn.ip}:{conn.port}.\n"
            "Start it in Termux: [bold]backseat-agent[/bold]"
        )
    except httpx.TimeoutException:
        raise BackseatError(f"Agent timed out at {conn.ip}:{conn.port}.")
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
        raise BackseatError(
            f"Agent unreachable at {conn.ip}:{conn.port}.\n"
            "Start it in Termux: [bold]backseat-agent[/bold]"
        )
    except httpx.TimeoutException:
        raise BackseatError("Command timed out after 60 seconds.")
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")


def get_tunnel_status(conn: PhoneConnection) -> TunnelStatus:
    try:
        r = httpx.get(
            f"{_base_url(conn)}/tunnel/status",
            headers=_headers(conn),
            timeout=5.0,
        )
        r.raise_for_status()
        return TunnelStatus.model_validate(r.json())
    except httpx.ConnectError:
        raise BackseatError(
            f"Agent unreachable at {conn.ip}:{conn.port}.\n"
            "Start it in Termux: [bold]backseat-agent[/bold]"
        )
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
        raise BackseatError(
            f"Agent unreachable at {conn.ip}:{conn.port}.\n"
            "Start it in Termux: [bold]backseat-agent[/bold]"
        )
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
        raise BackseatError(
            f"Agent unreachable at {conn.ip}:{conn.port}.\n"
            "Start it in Termux: [bold]backseat-agent[/bold]"
        )
    except httpx.HTTPStatusError as e:
        raise BackseatError(f"Agent returned error {e.response.status_code}: {e.response.text}")

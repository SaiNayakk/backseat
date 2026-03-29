import ipaddress
import json
import os
import stat
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, field_validator

CONFIG_PATH = Path.home() / ".backseat" / "config.json"


class PhoneConnection(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    ip: str
    port: int = Field(default=8080, ge=1, le=65535)
    ssh_port: int = Field(default=8022, ge=1, le=65535)
    user: str = Field(min_length=1, max_length=64)
    auth_method: str
    key_path: Optional[str] = None
    agent_token: Optional[str] = None

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, v: str) -> str:
        try:
            ipaddress.ip_address(v)
        except ValueError:
            raise ValueError(f"Invalid IP address: {v!r}")
        return v

    @field_validator("auth_method")
    @classmethod
    def validate_auth(cls, v: str) -> str:
        if v not in ("key", "password"):
            raise ValueError("auth_method must be 'key' or 'password'")
        return v

    @field_validator("key_path")
    @classmethod
    def expand_key_path(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return str(Path(v).expanduser().resolve())
        return v


class SavedCommand(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1)
    description: Optional[str] = None


class BackseatConfig(BaseModel):
    connections: list[PhoneConnection] = []
    commands: list[SavedCommand] = []
    default_connection: Optional[str] = None


def load_config() -> BackseatConfig:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config = BackseatConfig()
        _write(config)
        return config
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    return BackseatConfig.model_validate(json.loads(raw))


def save_config(config: BackseatConfig) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _write(config)


def _write(config: BackseatConfig) -> None:
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(config.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    # Windows requires removing target before rename
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    tmp.rename(CONFIG_PATH)
    if sys.platform == "win32":
        # Windows does not support Unix permissions.
        # ~/.backseat/config.json is readable by other local users.
        # Move it to a protected location if this is a shared machine.
        pass
    else:
        os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)


def get_connection(name: Optional[str], config: Optional[BackseatConfig] = None) -> PhoneConnection:
    if config is None:
        config = load_config()
    if not config.connections:
        raise BackseatError(
            "No phone connected. Run [bold]backseat init[/bold] first."
        )
    target = name or config.default_connection
    if target:
        match = next((c for c in config.connections if c.name == target), None)
        if match:
            return match
        raise BackseatError(f"No connection named '{target}'. Run [bold]backseat init[/bold].")
    return config.connections[0]


def get_command(name: str, config: Optional[BackseatConfig] = None) -> SavedCommand:
    if config is None:
        config = load_config()
    match = next((c for c in config.commands if c.name == name), None)
    if not match:
        raise BackseatError(
            f"No saved command '{name}'. Use [bold]backseat add {name}[/bold] to create it."
        )
    return match


class BackseatError(Exception):
    """User-facing error with a Rich-formatted message."""
    pass

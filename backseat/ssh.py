"""
SSH + SCP layer using paramiko.
Pure logic — no I/O, no prompts. All credentials passed in.
"""

import os
import socket
from pathlib import Path
from typing import Optional

import paramiko

from backseat.config import PhoneConnection, BackseatError


class SSHClient:
    def __init__(self, connection: PhoneConnection, password: Optional[str] = None):
        self.connection = connection
        self.password = password
        self._client: Optional[paramiko.SSHClient] = None

    def connect(self) -> None:
        client = paramiko.SSHClient()
        # AutoAddPolicy trusts any host key on first connect (TOFU — Trust On First Use).
        # This means a MITM on first connection would go undetected.
        # Acceptable for a personal LAN tool; use known_hosts verification for higher security.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        kwargs = dict(
            hostname=self.connection.ip,
            port=self.connection.ssh_port,
            username=self.connection.user,
            timeout=15,
        )

        if self.connection.auth_method == "key":
            if not self.connection.key_path:
                raise BackseatError(
                    "Auth method is 'key' but no key_path is set. "
                    "Re-run [bold]backseat init[/bold]."
                )
            kwargs["key_filename"] = self.connection.key_path
        else:
            if not self.password:
                raise BackseatError(
                    "Password required but not provided."
                )
            kwargs["password"] = self.password

        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException:
            raise BackseatError("SSH authentication failed. Check your credentials.")
        except paramiko.NoValidConnectionsError:
            raise BackseatError(
                f"Cannot connect to {self.connection.ip}:{self.connection.ssh_port}. "
                "Is the phone on the same network and is sshd running?\n"
                "  Start sshd in Termux: [bold]sshd[/bold]"
            )
        except (TimeoutError, socket.timeout):
            raise BackseatError(
                f"Connection timed out to {self.connection.ip}:{self.connection.ssh_port}."
            )
        except (paramiko.SSHException, socket.error, OSError) as e:
            raise BackseatError(f"SSH connection failed: {e}")

        # keep the session alive
        transport = client.get_transport()
        if transport:
            transport.set_keepalive(30)

        self._client = client

    def run(self, command: str) -> tuple[str, str, int]:
        """Run a command. Returns (stdout, stderr, returncode)."""
        if not self._client:
            raise BackseatError("SSH not connected.")
        _, stdout, stderr = self._client.exec_command(command)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = stdout.channel.recv_exit_status()
        return out, err, code

    def run_background(self, command: str) -> None:
        """Run a command detached (nohup). Fire and forget."""
        self.run(f"nohup {command} > /dev/null 2>&1 &")

    def upload(self, local: Path, remote: str) -> list[str]:
        """
        Upload a file or directory over SFTP.
        Returns list of uploaded remote paths.
        """
        if not self._client:
            raise BackseatError("SSH not connected.")

        sftp = self._client.open_sftp()
        uploaded: list[str] = []

        try:
            if local.is_file():
                _ensure_remote_dir(sftp, os.path.dirname(remote))
                sftp.put(str(local), remote)
                uploaded.append(remote)
            elif local.is_dir():
                uploaded.extend(_upload_dir(sftp, local, remote))
            else:
                raise BackseatError(f"Local path not found: {local}")
        finally:
            sftp.close()

        return uploaded

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SSHClient":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()


# ── SFTP helpers ───────────────────────────────────────────────────────────────

def _ensure_remote_dir(sftp: paramiko.SFTPClient, remote_dir: str) -> None:
    """Create remote directory tree if it doesn't exist."""
    if not remote_dir or remote_dir == "/":
        return
    parts = remote_dir.replace("\\", "/").split("/")
    current = ""
    for part in parts:
        if not part:
            current = "/"
            continue
        current = f"{current}/{part}" if current != "/" else f"/{part}"
        if current in ("", "/"):
            continue
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def _upload_dir(sftp: paramiko.SFTPClient, local_dir: Path, remote_dir: str) -> list[str]:
    """Recursively upload a directory. Returns list of uploaded remote paths."""
    uploaded: list[str] = []
    _ensure_remote_dir(sftp, remote_dir)

    for item in local_dir.iterdir():
        remote_path = f"{remote_dir}/{item.name}"
        if item.is_file():
            sftp.put(str(item), remote_path)
            uploaded.append(remote_path)
        elif item.is_dir():
            uploaded.extend(_upload_dir(sftp, item, remote_path))

    return uploaded

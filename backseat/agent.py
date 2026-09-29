"""
Backseat Agent — runs on Android phone inside Termux

Install (in Termux):
    pkg install python openssh
    pip install "backseat[agent]"
    sshd
    backseat-agent

On startup shows a QR code + pairing code so you can pair
from your laptop with: backseat init
"""

import json
import logging
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil
import qrcode
from flask import Flask, Response, jsonify, request, stream_with_context

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backseat")
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# ── State ──────────────────────────────────────────────────────────────────────

START_TIME = time.time()
PAIRING_TOKEN: str = secrets.token_hex(4).upper()  # 8 hex chars, 32-bit entropy
SESSION_TOKEN: str = secrets.token_urlsafe(32)
request_count: int = 0
paired: bool = False
_paired_lock = threading.Lock()

# Rate limiting for /pair endpoint
_pair_attempts: int = 0
_pair_locked_until: float = 0.0
MAX_PAIR_ATTEMPTS = 5
PAIR_LOCKOUT_SECONDS = 60

# Cloudflare tunnel state
_tunnel_process: Optional[subprocess.Popen] = None
_tunnel_url: Optional[str] = None
_tunnel_port: Optional[int] = None
_tunnel_lock = threading.Lock()

# Remote-access tunnel state — a dedicated quick tunnel exposing the agent's own
# port, separate from _tunnel_process above (which exposes one app port at a
# time). Lets `backseat` reach this agent over the internet, not just LAN.
_remote_process: Optional[subprocess.Popen] = None
_remote_url: Optional[str] = None
_remote_lock = threading.Lock()
AGENT_PORT = 8080  # overwritten in main() from BACKSEAT_PORT

# ── Persisted agent state (survives agent restarts, lives on the phone) ────────

AGENT_HOME = Path.home() / ".backseat" / "agent"
LOGS_DIR = AGENT_HOME / "logs"
APPS_FILE = AGENT_HOME / "apps.json"
TOKEN_FILE = AGENT_HOME / "token.json"
REMOTE_FLAG_FILE = AGENT_HOME / "remote_enabled"
APP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_LOG_BYTES = 5 * 1024 * 1024

# Managed long-running processes ("apps"), keyed by name.
# Persisted fields: name, command, cwd, desired_state ("running"/"stopped").
# Runtime-only fields (never written to disk): _proc, pid, status, restart_count,
# started_at, next_restart_at.
_apps: dict[str, dict] = {}
_apps_lock = threading.RLock()


# ── Helpers ────────────────────────────────────────────────────────────────────

def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def check_dependencies() -> None:
    missing = []
    for pkg in ("flask", "psutil", "qrcode"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[backseat] Missing packages: {', '.join(missing)}")
        print(f"[backseat] Run: pip install {' '.join(missing)}")
        sys.exit(1)


def show_pairing_info(ip: str, port: int, already_paired: bool) -> None:
    print("\n" + "=" * 52)
    print("  BACKSEAT AGENT")
    print("=" * 52)

    if already_paired:
        print("\n  Already paired from a previous run — no need to run")
        print("  backseat init again. Your laptop's saved session still works.")
        print(f"\n  IP Address   :  {ip}")
        print(f"  Port         :  {port}")
        print("=" * 52)
        print(f"  Dashboard    :  http://{ip}:{port}/dashboard  (public, read-only)")
        print("=" * 52 + "\n")
        return

    pairing_string = f"backseat://{ip}:{port}#{PAIRING_TOKEN}"

    qr = qrcode.QRCode(border=1)
    qr.add_data(pairing_string)
    qr.make(fit=True)
    qr.print_ascii(invert=True)

    print(f"\n  IP Address   :  {ip}")
    print(f"  Port         :  {port}")
    print(f"  Pair Code    :  {PAIRING_TOKEN}")
    print(f"  (Code expires after {MAX_PAIR_ATTEMPTS} failed attempts)")
    print(f"\n  On your laptop:")
    print(f"    backseat init")
    print(f"  Enter [  {PAIRING_TOKEN}  ] when prompted.")
    print("=" * 52)
    print(f"  Dashboard    :  http://{ip}:{port}/dashboard  (public, read-only)")
    print("=" * 52 + "\n")


def stop_tunnel_process() -> None:
    global _tunnel_process, _tunnel_url, _tunnel_port
    with _tunnel_lock:
        if _tunnel_process and _tunnel_process.poll() is None:
            _tunnel_process.terminate()
            try:
                _tunnel_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _tunnel_process.kill()
        _tunnel_process = None
        _tunnel_url = None
        _tunnel_port = None


def handle_shutdown(signum, frame):
    log.info("Shutting down — stopping tunnel if active. Managed apps keep running.")
    stop_tunnel_process()
    sys.exit(0)


# ── Persisted token ──────────────────────────────────────────────────────────────

def _load_persisted_token() -> None:
    """Restore session token + paired state from a previous run, if present."""
    global SESSION_TOKEN, paired
    if not TOKEN_FILE.exists():
        return
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
        SESSION_TOKEN = data["session_token"]
        paired = True
        log.info("Restored saved pairing — no need to re-pair after this restart.")
    except (json.JSONDecodeError, KeyError, OSError):
        log.warning("Could not read saved token file, ignoring.")


def _save_token() -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"session_token": SESSION_TOKEN}), encoding="utf-8")


# ── Apps: persistence ──────────────────────────────────────────────────────────

def _persist_apps() -> None:
    """Write the subset of app state that should survive a restart."""
    with _apps_lock:
        data = [
            {"name": a["name"], "command": a["command"], "cwd": a.get("cwd"),
             "desired_state": a["desired_state"]}
            for a in _apps.values()
        ]
    APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = APPS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(APPS_FILE)


def _load_apps_file() -> list[dict]:
    if not APPS_FILE.exists():
        return []
    try:
        return json.loads(APPS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read apps.json, starting with no apps.")
        return []


def _new_app_entry(name: str, command: str, cwd: Optional[str]) -> dict:
    return {
        "name": name,
        "command": command,
        "cwd": cwd,
        "desired_state": "running",
        "_proc": None,
        "pid": None,
        "status": "stopped",
        "restart_count": 0,
        "started_at": None,
        "next_restart_at": 0.0,
    }


# ── Apps: process lifecycle ─────────────────────────────────────────────────────

def _rotate_log_if_needed(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            backup = path.with_suffix(path.suffix + ".1")
            if backup.exists():
                backup.unlink()
            path.rename(backup)
    except OSError:
        pass


def _shell_command(app: dict) -> str:
    cwd = app.get("cwd")
    if cwd:
        expanded = str(Path(cwd).expanduser())
        return f"cd {shlex.quote(expanded)} && ( {app['command']} )"
    return app["command"]


def _spawn_app(app: dict) -> None:
    """Start (or restart) the process for an app. Caller must hold _apps_lock."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"{app['name']}.log"
    _rotate_log_if_needed(log_path)
    log_fh = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")

    popen_kwargs = dict(shell=True, stdout=log_fh, stderr=subprocess.STDOUT)
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True  # survives the agent process dying

    try:
        proc = subprocess.Popen(_shell_command(app), **popen_kwargs)
    finally:
        log_fh.close()

    app["_proc"] = proc
    app["pid"] = proc.pid
    app["status"] = "running"
    app["started_at"] = time.time()
    log.info(f"App '{app['name']}' started (pid {proc.pid})")


def _proc_matches_app(app: dict, cwd: Optional[str], cmdline: str) -> bool:
    """Best-effort match between a live process and a registered app.

    Prefers cwd: apps commonly share a long command prefix (e.g. the same
    PATH export used to fix Termux's minimal SSH PATH), which makes a
    truncated command-substring match ambiguous between them, but their cwd
    is reliably distinct. Falls back to a full-command substring match
    (not truncated — a short prefix has the same ambiguity problem cwd solves)
    for apps with no cwd set.
    """
    app_cwd = app.get("cwd")
    if app_cwd and cwd:
        try:
            if Path(app_cwd).resolve() == Path(cwd).resolve():
                return True
        except OSError:
            pass
        return False
    return bool(app["command"]) and app["command"] in cmdline


def _is_app_alive(app: dict) -> bool:
    """Best-effort liveness check, valid across agent restarts (no _proc handle)."""
    proc = app.get("_proc")
    if proc is not None:
        return proc.poll() is None
    pid = app.get("pid")
    if not pid or not psutil.pid_exists(pid):
        return False
    try:
        candidate = psutil.Process(pid)
        cmdline = " ".join(candidate.cmdline())
        try:
            cwd = candidate.cwd()
        except (psutil.AccessDenied, psutil.ZombieProcess):
            cwd = None
        return _proc_matches_app(app, cwd, cmdline)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _stop_app_process(app: dict) -> None:
    """Best-effort stop. Caller must hold _apps_lock."""
    proc = app.get("_proc")
    pid = app.get("pid")
    try:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        elif pid and psutil.pid_exists(pid):
            psutil.Process(pid).terminate()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        pass
    app["_proc"] = None
    app["pid"] = None
    app["status"] = "stopped"


def _app_public(app: dict) -> dict:
    uptime = int(time.time() - app["started_at"]) if app.get("started_at") and app["status"] == "running" else None
    return {
        "name": app["name"],
        "command": app["command"],
        "cwd": app.get("cwd"),
        "status": app["status"],
        "pid": app.get("pid"),
        "uptime_seconds": uptime,
        "restart_count": app["restart_count"],
        "desired_state": app["desired_state"],
    }


def _load_and_start_apps() -> None:
    """On agent startup: reload persisted apps and re-adopt or (re)start them."""
    with _apps_lock:
        claimed_pids: set[int] = set()
        for entry in _load_apps_file():
            name = entry.get("name")
            if not name or not APP_NAME_RE.match(name):
                continue
            app = _new_app_entry(name, entry["command"], entry.get("cwd"))
            app["desired_state"] = entry.get("desired_state", "running")
            _apps[name] = app
            if app["desired_state"] != "running":
                continue
            # Try to find a still-running process from before the restart by
            # scanning for one whose cwd (or, failing that, full command)
            # matches — pids aren't persisted since they aren't stable across
            # a reboot. claimed_pids stops two apps from adopting the same
            # process when several share a long command prefix.
            adopted = False
            for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
                pid = proc.info["pid"]
                if pid in claimed_pids:
                    continue
                cmdline = " ".join(proc.info.get("cmdline") or [])
                if _proc_matches_app(app, proc.info.get("cwd"), cmdline):
                    app["pid"] = pid
                    app["status"] = "running"
                    app["started_at"] = time.time()
                    adopted = True
                    claimed_pids.add(pid)
                    log.info(f"App '{name}' already running (pid {pid}), adopted.")
                    break
            if not adopted:
                _spawn_app(app)


# ── Watchdog ─────────────────────────────────────────────────────────────────────

WATCHDOG_INTERVAL = 3.0
STABLE_AFTER_SECONDS = 60
MAX_BACKOFF_SECONDS = 60


def _watchdog_loop() -> None:
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        with _apps_lock:
            for app in list(_apps.values()):
                if app["desired_state"] != "running":
                    continue
                if _is_app_alive(app):
                    if app["status"] != "running":
                        app["status"] = "running"
                    if (app.get("started_at") and app["restart_count"] > 0
                            and time.time() - app["started_at"] > STABLE_AFTER_SECONDS):
                        app["restart_count"] = 0
                    continue

                # Process is dead but should be running — restart with backoff.
                app["status"] = "crashed"
                app["pid"] = None
                app["_proc"] = None
                now = time.time()
                if now < app.get("next_restart_at", 0):
                    continue
                app["restart_count"] += 1
                backoff = min(2 ** min(app["restart_count"], 6), MAX_BACKOFF_SECONDS)
                # Won't attempt another restart within `backoff` seconds even if
                # this one also dies immediately — caps restart-loop frequency.
                app["next_restart_at"] = now + backoff
                log.warning(
                    f"App '{app['name']}' is down, restarting "
                    f"(attempt {app['restart_count']}, cooldown {backoff}s)"
                )
                _spawn_app(app)


# ── App ────────────────────────────────────────────────────────────────────────

app = Flask(__name__)


# ── Auth ───────────────────────────────────────────────────────────────────────

def require_auth():
    """Returns an error response tuple, or None if the request carries the session token."""
    if not paired:
        return jsonify({"detail": "Agent not yet paired. Run backseat init."}), 403
    token = request.headers.get("x-backseat-token", "")
    if not (token and secrets.compare_digest(token, SESSION_TOKEN)):
        return jsonify({"detail": "Invalid token."}), 401
    return None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    """Unauthenticated — used by laptop to check if agent is reachable before pairing."""
    return jsonify({"status": "ok", "paired": paired})


@app.post("/pair")
def pair():
    global paired, _pair_attempts, _pair_locked_until
    body = request.get_json(silent=True) or {}
    pairing_token = body.get("pairing_token", "")
    ssh_user = body.get("ssh_user", "")

    with _paired_lock:
        if paired:
            return jsonify({"detail": "Already paired."}), 409

        now = time.time()
        if now < _pair_locked_until:
            remaining = int(_pair_locked_until - now)
            return jsonify({"detail": f"Too many failed attempts. Try again in {remaining}s."}), 429

        if not secrets.compare_digest(pairing_token.upper().strip(), PAIRING_TOKEN):
            _pair_attempts += 1
            log.warning(f"Failed pairing attempt {_pair_attempts}/{MAX_PAIR_ATTEMPTS}")
            if _pair_attempts >= MAX_PAIR_ATTEMPTS:
                _pair_locked_until = now + PAIR_LOCKOUT_SECONDS
                _pair_attempts = 0
                return jsonify({"detail": f"Too many failed attempts. Locked for {PAIR_LOCKOUT_SECONDS}s."}), 429
            return jsonify({"detail": f"Invalid pairing code. {MAX_PAIR_ATTEMPTS - _pair_attempts} attempts remaining."}), 403

        _pair_attempts = 0
        paired = True

    _save_token()
    log.info(f"Paired successfully with user '{ssh_user}'")
    return jsonify({"session_token": SESSION_TOKEN, "message": "Paired successfully"})


def _get_cpu_percent() -> float:
    try:
        return float(psutil.cpu_percent(interval=None))
    except (PermissionError, Exception):
        pass
    # On Android/Termux, /proc/stat is blocked by SELinux for non-root apps.
    # Fallback to parsing top output if available:
    try:
        out = subprocess.check_output(["top", "-n", "1", "-b"], text=True, stderr=subprocess.DEVNULL)
        for line in out.splitlines():
            if "%cpu" in line and "%idle" in line:
                total_m = re.search(r"(\d+)%cpu", line)
                idle_m = re.search(r"(\d+)%idle", line)
                if total_m and idle_m:
                    total = float(total_m.group(1))
                    idle = float(idle_m.group(1))
                    if total > 0:
                        return round(((total - idle) / total) * 100.0, 1)
    except Exception:
        pass
    try:
        return round(sum(p.info.get("cpu_percent") or 0.0 for p in psutil.process_iter(["cpu_percent"])), 1)
    except Exception:
        return 0.0


def _system_snapshot() -> dict:
    """CPU, RAM, storage and the busiest processes."""
    try:
        ram = psutil.virtual_memory()
    except Exception:
        ram = None

    try:
        disk = psutil.disk_usage(os.path.expanduser("~"))
    except Exception:
        try:
            disk = psutil.disk_usage("/")
        except Exception:
            disk = None

    procs = []
    try:
        for p in sorted(
            psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
            key=lambda x: (x.info.get("cpu_percent") or 0) if x.info else 0,
            reverse=True,
        )[:10]:
            if not p.info:
                continue
            procs.append({
                "pid": p.info.get("pid", 0),
                "name": p.info.get("name") or "unknown",
                "cpu_percent": round(p.info.get("cpu_percent") or 0, 1),
                "mem_percent": round(p.info.get("memory_percent") or 0, 1),
            })
    except Exception:
        pass

    return {
        "cpu_percent": _get_cpu_percent(),
        "ram_percent": ram.percent if ram else 0.0,
        "ram_used_mb": (ram.used // (1024 * 1024)) if ram else 0,
        "ram_total_mb": (ram.total // (1024 * 1024)) if ram else 0,
        "storage_percent": disk.percent if disk else 0.0,
        "storage_used_gb": round(disk.used / (1024 ** 3), 1) if disk else 0.0,
        "storage_total_gb": round(disk.total / (1024 ** 3), 1) if disk else 0.0,
        "uptime_seconds": int(time.time() - START_TIME),
        "processes": procs,
    }


@app.get("/health")
def health():
    global request_count
    err = require_auth()
    if err:
        return err
    request_count += 1
    return jsonify({**_system_snapshot(), "request_count": request_count, "timestamp": datetime.now(timezone.utc).isoformat()})


_status_cache: dict = {"at": 0.0, "data": None}


@app.get("/public/status")
def public_status():
    """
    What the public dashboard shows: system load, app names and states, the
    tunnel's public hostnames. Never commands, paths, pids, ports, logs or the
    remote-access address. Cached for a few seconds.
    """
    with _vitals_lock:
        if time.time() - _status_cache["at"] > 5 or _status_cache["data"] is None:
            snap = _system_snapshot()
            snap["processes"] = [{k: p[k] for k in ("name", "cpu_percent", "mem_percent")} for p in snap["processes"][:8]]
            with _apps_lock:
                apps = [{"name": a["name"], "status": a["status"], "restart_count": a["restart_count"],
                         "uptime_seconds": int(time.time() - a["started_at"]) if a.get("started_at") and a["status"] == "running" else None}
                        for a in _apps.values()]
            routes = _find_named_tunnel() or []
            snap.update(
                apps=apps,
                battery=_battery(),
                request_count=request_count,
                tunnel={"active": bool(routes), "hostnames": [{"hostname": r.get("hostname"), "live": bool(r.get("live"))} for r in routes]},
            )
            _status_cache.update(at=time.time(), data=snap)
        data = _status_cache["data"]
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Public vitals ──────────────────────────────────────────────────────────────
# A few harmless numbers for a public status line (e.g. a portfolio saying "served
# by this phone"): no app names, commands, paths or tokens. Cached, because every
# page view of that site calls it. Battery needs the Termux:API Android app.

VITALS_ORIGINS = [o.strip() for o in os.getenv("BACKSEAT_PUBLIC_ORIGINS", "https://saiworks.nncs.in").split(",") if o.strip()]
_vitals_cache: dict = {"at": 0.0, "data": None}
_battery_cache: dict = {"at": 0.0, "data": None}
_vitals_lock = threading.Lock()


def _battery() -> Optional[dict]:
    """termux-battery-status, at most once every 5 minutes; None if Termux:API isn't installed."""
    now = time.time()
    if now - _battery_cache["at"] < 300:
        return _battery_cache["data"]
    data = None
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=4).stdout
        b = json.loads(out)
        data = {"percent": int(b["percentage"]), "charging": b.get("status") in ("CHARGING", "FULL"),
                "temp_c": round(float(b["temperature"]), 1) if b.get("temperature") is not None else None}
    except Exception:
        data = None
    _battery_cache.update(at=now, data=data)
    return data


@app.get("/public/vitals")
def public_vitals():
    with _vitals_lock:
        if time.time() - _vitals_cache["at"] > 30 or _vitals_cache["data"] is None:
            with _apps_lock:
                states = [a.get("status") for a in _apps.values()]
            _vitals_cache.update(at=time.time(), data={
                "apps_up": sum(1 for st in states if st == "running"),
                "apps_total": len(states),
                "cpu_percent": round(_get_cpu_percent()),
                "server_uptime_seconds": int(time.time() - START_TIME),
                "battery": _battery(),
            })
        data = _vitals_cache["data"]
    resp = jsonify(data)
    origin = request.headers.get("Origin", "")
    if origin in VITALS_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Cache-Control"] = "public, max-age=30"
    return resp


@app.post("/run")
def run_command():
    global request_count
    err = require_auth()
    if err:
        return err
    request_count += 1

    body = request.get_json(silent=True) or {}
    command = body.get("command", "")
    if not command:
        return jsonify({"detail": "command is required"}), 400

    log.info(f"Running command: {command!r}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"detail": "Command timed out after 60 seconds."}), 408

    return jsonify({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    })


# ── Apps ───────────────────────────────────────────────────────────────────────
# Managed long-running processes: auto-restarted on crash, logged to disk,
# and persisted so they survive an agent restart or phone reboot.

@app.get("/apps")
def apps_list():
    err = require_auth()
    if err:
        return err
    with _apps_lock:
        return jsonify([_app_public(a) for a in _apps.values()])


@app.post("/apps")
def apps_create():
    err = require_auth()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    command = (body.get("command") or "").strip()
    cwd = body.get("cwd")

    if not APP_NAME_RE.match(name):
        return jsonify({"detail": "name must be 1-64 chars: letters, digits, '-', '_'"}), 400
    if not command:
        return jsonify({"detail": "command is required"}), 400

    with _apps_lock:
        if name in _apps:
            return jsonify({"detail": f"App '{name}' already exists. Remove it first or use /apps/{name}/restart."}), 409
        app_entry = _new_app_entry(name, command, cwd)
        _apps[name] = app_entry
        _spawn_app(app_entry)
        _persist_apps()
        result = _app_public(app_entry)

    log.info(f"App '{name}' created: {command!r}")
    return jsonify(result), 201


@app.post("/apps/<name>/start")
def apps_start(name: str):
    err = require_auth()
    if err:
        return err
    with _apps_lock:
        app_entry = _apps.get(name)
        if not app_entry:
            return jsonify({"detail": f"No app named '{name}'."}), 404
        app_entry["desired_state"] = "running"
        app_entry["restart_count"] = 0
        app_entry["next_restart_at"] = 0.0
        if not _is_app_alive(app_entry):
            _spawn_app(app_entry)
        _persist_apps()
        return jsonify(_app_public(app_entry))


@app.post("/apps/<name>/stop")
def apps_stop(name: str):
    err = require_auth()
    if err:
        return err
    with _apps_lock:
        app_entry = _apps.get(name)
        if not app_entry:
            return jsonify({"detail": f"No app named '{name}'."}), 404
        app_entry["desired_state"] = "stopped"
        _stop_app_process(app_entry)
        _persist_apps()
        return jsonify(_app_public(app_entry))


@app.post("/apps/<name>/restart")
def apps_restart(name: str):
    err = require_auth()
    if err:
        return err
    with _apps_lock:
        app_entry = _apps.get(name)
        if not app_entry:
            return jsonify({"detail": f"No app named '{name}'."}), 404
        _stop_app_process(app_entry)
        app_entry["desired_state"] = "running"
        app_entry["restart_count"] = 0
        app_entry["next_restart_at"] = 0.0
        _spawn_app(app_entry)
        _persist_apps()
        return jsonify(_app_public(app_entry))


@app.delete("/apps/<name>")
def apps_remove(name: str):
    err = require_auth()
    if err:
        return err
    with _apps_lock:
        app_entry = _apps.pop(name, None)
        if not app_entry:
            return jsonify({"detail": f"No app named '{name}'."}), 404
        _stop_app_process(app_entry)
        _persist_apps()
    log_path = LOGS_DIR / f"{name}.log"
    try:
        log_path.unlink(missing_ok=True)
        log_path.with_suffix(".log.1").unlink(missing_ok=True)
    except OSError:
        pass
    return jsonify({"status": "removed"})


@app.get("/apps/<name>/logs")
def apps_logs(name: str):
    err = require_auth()
    if err:
        return err
    with _apps_lock:
        if name not in _apps:
            return jsonify({"detail": f"No app named '{name}'."}), 404

    log_path = LOGS_DIR / f"{name}.log"
    follow = request.args.get("follow") in ("1", "true", "yes")
    try:
        lines_n = int(request.args.get("lines", "200"))
    except ValueError:
        lines_n = 200

    if not follow:
        if not log_path.exists():
            return jsonify({"lines": []})
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-lines_n:]
        return jsonify({"lines": [line.rstrip("\n") for line in tail]})

    def stream():
        if not log_path.exists():
            return
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    yield line
                else:
                    with _apps_lock:
                        still_exists = name in _apps
                    if not still_exists:
                        break
                    time.sleep(1)

    return Response(stream_with_context(stream()), mimetype="text/plain")


# ── Tunnel ─────────────────────────────────────────────────────────────────────

def _read_tunnel_url(proc: subprocess.Popen) -> None:
    global _tunnel_url
    if proc.stderr is None:
        return
    for line in proc.stderr:
        if not _tunnel_url:
            match = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
            if match:
                with _tunnel_lock:
                    _tunnel_url = match.group(0)
                log.info(f"Tunnel URL: {_tunnel_url}")


def _check_port_live(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=0.3):
            return True
    except OSError:
        return False


def _parse_ingress(text: str) -> list[tuple[str, Optional[int]]]:
    """Pair each `- hostname: ...` line with the `service: http://localhost:PORT`
    line that follows it in a cloudflared config.yml ingress block."""
    entries: list[tuple[str, Optional[int]]] = []
    current_host: Optional[str] = None
    for line in text.splitlines():
        host_match = re.match(r"\s*-\s*hostname:\s*(\S+)", line)
        if host_match:
            current_host = host_match.group(1)
            continue
        service_match = re.match(r"\s*service:\s*https?://localhost:(\d+)", line)
        if current_host:
            entries.append((current_host, int(service_match.group(1)) if service_match else None))
            current_host = None
    return entries


def _find_named_tunnel() -> Optional[list[dict]]:
    """Look for a managed app running `cloudflared ... tunnel run` with a local
    config.yml, distinct from the quick tunnel tracked by /tunnel/start above.
    Returns each ingress route (hostname, port, whether the local port is live),
    or None if no such app is alive."""
    with _apps_lock:
        for app in _apps.values():
            command = app.get("command", "")
            if "cloudflared" not in command or not re.search(r"\btunnel\b.*\brun\b", command):  # "tunnel run" or "tunnel --config x.yml run"
                continue
            if not _is_app_alive(app):
                continue
            match = re.search(r"--config\s+(\S+)", command)
            if not match:
                return []
            try:
                text = Path(match.group(1)).read_text(encoding="utf-8")
            except OSError:
                return []
            return [
                {"hostname": host, "port": port, "live": port is not None and _check_port_live(port)}
                for host, port in _parse_ingress(text)
            ]
    return None


@app.get("/tunnel/status")
def tunnel_status():
    err = require_auth()
    if err:
        return err
    with _tunnel_lock:
        active = _tunnel_process is not None and _tunnel_process.poll() is None
        if active:
            return jsonify({
                "active": True,
                "url": _tunnel_url,
                "port": _tunnel_port,
                "mode": "quick",
                "routes": None,
            })

    routes = _find_named_tunnel()
    if routes is not None:
        return jsonify({
            "active": True,
            "url": None,
            "port": None,
            "mode": "named",
            "routes": routes,
        })

    return jsonify({"active": False, "url": None, "port": None, "mode": None, "routes": None})


@app.post("/tunnel/start")
def tunnel_start():
    global _tunnel_process, _tunnel_url, _tunnel_port
    err = require_auth()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    port = body.get("port")
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return jsonify({"detail": "port must be an integer between 1 and 65535"}), 400

    stop_tunnel_process()

    with _tunnel_lock:
        _tunnel_url = None
        _tunnel_port = port

    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--config", "/dev/null", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return jsonify({"detail": "cloudflared not found. Install with: pkg install cloudflared"}), 500
    except (OSError, PermissionError) as e:
        return jsonify({"detail": f"Failed to start cloudflared: {e}"}), 500

    with _tunnel_lock:
        _tunnel_process = proc

    thread = threading.Thread(target=_read_tunnel_url, args=(proc,), daemon=True)
    thread.start()

    # Wait up to 10s for URL, bail early if process dies
    for _ in range(20):
        with _tunnel_lock:
            if _tunnel_url:
                break
        if proc.poll() is not None:
            return jsonify({"detail": f"cloudflared exited unexpectedly (code {proc.returncode}). Check your network."}), 500
        time.sleep(0.5)

    with _tunnel_lock:
        active = _tunnel_process is not None and _tunnel_process.poll() is None
        return jsonify({"active": active, "url": _tunnel_url, "port": _tunnel_port})


@app.post("/tunnel/stop")
def tunnel_stop():
    err = require_auth()
    if err:
        return err
    stop_tunnel_process()
    log.info("Tunnel stopped.")
    return jsonify({"status": "stopped"})


# ── Remote access (self-tunnel) ──────────────────────────────────────────────────
# Lets `backseat` reach this agent over the internet, not just LAN, via a quick
# tunnel exposing the agent's own port. Separate from _tunnel_process above
# (which exposes one managed app's port at a time) so the two don't conflict.

def stop_remote_process() -> None:
    global _remote_process, _remote_url
    with _remote_lock:
        if _remote_process and _remote_process.poll() is None:
            _remote_process.terminate()
            try:
                _remote_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _remote_process.kill()
        _remote_process = None
        _remote_url = None
    try:
        subprocess.run(["pkill", "-9", "-f", f"cloudflared tunnel.*:{AGENT_PORT}"], stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _read_remote_url(proc: subprocess.Popen) -> None:
    global _remote_url
    if proc.stderr is None:
        return
    for line in proc.stderr:
        if not _remote_url:
            match = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
            if match:
                with _remote_lock:
                    _remote_url = match.group(0)
                log.info(f"Remote access URL: {_remote_url}")


def start_remote_tunnel() -> tuple[bool, Optional[str]]:
    """Start (or restart) the quick tunnel exposing this agent's own port.
    Returns (started_ok, error_detail_if_not_ok)."""
    global _remote_process
    stop_remote_process()
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--config", "/dev/null", "--url", f"http://127.0.0.1:{AGENT_PORT}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return False, "cloudflared not found. Install with: pkg install cloudflared"
    except (OSError, PermissionError) as e:
        return False, f"Failed to start cloudflared: {e}"

    with _remote_lock:
        _remote_process = proc

    threading.Thread(target=_read_remote_url, args=(proc,), daemon=True).start()

    for _ in range(40):
        with _remote_lock:
            if _remote_url:
                break
        if proc.poll() is not None:
            return False, f"cloudflared exited unexpectedly (code {proc.returncode})"
        time.sleep(0.5)
    return True, None


@app.get("/remote/status")
def remote_status():
    err = require_auth()
    if err:
        return err
    with _remote_lock:
        active = _remote_process is not None and _remote_process.poll() is None
        return jsonify({"active": active, "url": _remote_url if active else None})


@app.post("/remote/enable")
def remote_enable():
    err = require_auth()
    if err:
        return err
    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    REMOTE_FLAG_FILE.touch()
    ok, detail = start_remote_tunnel()
    if not ok:
        return jsonify({"detail": detail}), 500
    with _remote_lock:
        active = _remote_process is not None and _remote_process.poll() is None
        return jsonify({"active": active, "url": _remote_url})


@app.post("/remote/disable")
def remote_disable():
    err = require_auth()
    if err:
        return err
    if REMOTE_FLAG_FILE.exists():
        REMOTE_FLAG_FILE.unlink()
    stop_remote_process()
    log.info("Remote access disabled.")
    return jsonify({"status": "disabled"})


# ── Web Dashboard ──────────────────────────────────────────────────────────────

_HTML = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"}


@app.get("/dashboard")
def dashboard():
    """Public and read-only: it only shows what /public/status returns."""
    return _DASHBOARD_HTML, 200, _HTML


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backseat Dashboard</title>
<style>
  :root {
    --bg: #0c0c0e;
    --panel-bg: #141418;
    --panel-header-bg: #18181e;
    --border: #2a2a32;
    --border-light: #383844;
    --accent: #7c6af7;
    --accent-hover: #9687ff;
    --text: #e0e0e6;
    --text-dim: #717182;
    --text-muted: #4e4e5d;
    --c-green: #4caf50;
    --c-yellow: #f0a500;
    --c-red: #e05555;
    --c-blue: #5dade2;
    --c-cyan: #38d39f;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'SF Mono', Menlo, Consolas, monospace;
    font-size: 13px;
    line-height: 1.5;
    min-height: 100vh;
    padding: clamp(0.75rem, 2vw, 1.75rem);
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .app-container {
    width: 100%;
    max-width: 1300px;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  /* Header Panel */
  .header-card {
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.75rem 1.1rem;
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .header-left {
    display: flex;
    align-items: center;
    gap: 0.85rem;
    flex-wrap: wrap;
  }
  .logo {
    color: var(--accent);
    font-weight: 700;
    font-size: 0.95rem;
    letter-spacing: 0.06em;
  }
  .conn-info {
    color: var(--text-dim);
    font-size: 0.85rem;
  }
  .header-right {
    font-size: 0.8rem;
    color: var(--text-dim);
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }
  .header-right.err { color: var(--c-red); }

  /* 2-Column Grid Layout */
  .grid-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1.1fr);
    gap: 1rem;
  }
  @media (max-width: 860px) {
    .grid-layout {
      grid-template-columns: 1fr;
    }
  }
  .col {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  /* Panels */
  .panel {
    background: var(--panel-bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  .panel-title {
    color: var(--accent);
    font-weight: 700;
    font-size: 0.85rem;
    padding: 0.5rem 0.9rem;
    background: var(--panel-header-bg);
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .panel-body {
    padding: 0.9rem;
    flex: 1;
  }

  /* Progress Bars */
  .bar-row {
    display: grid;
    grid-template-columns: 80px minmax(100px, 1fr) auto;
    align-items: center;
    gap: 0.8rem;
    margin-bottom: 0.75rem;
  }
  .bar-label {
    font-weight: 600;
    color: #fff;
  }
  .bar-track {
    background: #1e1e26;
    border-radius: 3px;
    height: 10px;
    overflow: hidden;
    position: relative;
    border: 1px solid #282834;
  }
  .bar-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.4s ease, background-color 0.4s ease;
  }
  .bar-val {
    font-size: 0.85rem;
    text-align: right;
    white-space: nowrap;
  }

  /* System Metrics Info */
  .sys-info {
    margin-top: 0.6rem;
    padding-top: 0.6rem;
    border-top: 1px dashed var(--border);
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    font-size: 0.85rem;
  }
  .sys-metric {
    display: flex;
    gap: 0.5rem;
  }
  .sys-metric .label {
    font-weight: 600;
    color: #fff;
  }
  .sys-metric .val {
    color: var(--c-cyan);
    font-weight: 500;
  }

  /* Tables */
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }
  th {
    text-align: left;
    color: var(--text-dim);
    font-weight: 600;
    padding: 0.35rem 0.5rem;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 0.4rem 0.5rem;
    border-bottom: 1px solid #1a1a22;
  }
  tr:last-child td {
    border-bottom: none;
  }

  /* Badges & Dots */
  .dot {
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 4px;
  }
  .dot.active { background: var(--c-green); box-shadow: 0 0 6px rgba(76, 175, 80, 0.4); }
  .dot.inactive { background: var(--text-muted); }
  .dot.crashed { background: var(--c-red); box-shadow: 0 0 6px rgba(224, 85, 85, 0.4); }

  .badge {
    display: inline-block;
    padding: 0.1rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .badge-running { background: rgba(76, 175, 80, 0.15); color: var(--c-green); border: 1px solid rgba(76, 175, 80, 0.3); }
  .badge-crashed { background: rgba(224, 85, 85, 0.15); color: var(--c-red); border: 1px solid rgba(224, 85, 85, 0.3); }
  .badge-stopped { background: #1e1e24; color: var(--text-dim); border: 1px solid var(--border); }

  /* Tunnel info */
  .tunnel-box {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .tunnel-status-line {
    font-size: 0.9rem;
    font-weight: 600;
  }
  .tunnel-url {
    color: var(--accent);
    font-weight: 600;
    text-decoration: underline;
    word-break: break-all;
    font-size: 0.85rem;
    transition: color 0.2s;
  }
  .tunnel-url:hover {
    color: var(--accent-hover);
  }
  .hint-text {
    color: var(--text-dim);
    font-size: 0.8rem;
    margin-top: 0.4rem;
  }
  .hint-code {
    color: #fff;
    background: #1e1e26;
    padding: 0.15rem 0.4rem;
    border-radius: 3px;
    font-size: 0.8rem;
  }

  /* Footer */
  .footer {
    text-align: center;
    color: var(--text-muted);
    font-size: 0.75rem;
    margin-top: 0.5rem;
  }
</style>
</head>
<body>
<div class="app-container">
  <!-- Header -->
  <div class="header-card">
    <div class="header-left">
      <span class="logo">⬡ BACKSEAT</span>
      <span class="conn-info" id="hdr-conn">Phone Agent</span>
    </div>
    <div class="header-right" id="hdr-right">
      <span class="dot active"></span> <span id="hdr-updated">Connecting...</span>
    </div>
  </div>

  <!-- Body Grid -->
  <div class="grid-layout">
    <!-- Left Column: System + Cloudflare Tunnel -->
    <div class="col">
      <!-- System Panel -->
      <div class="panel">
        <div class="panel-title">System</div>
        <div class="panel-body">
          <div class="bar-row">
            <span class="bar-label">CPU</span>
            <div class="bar-track"><div class="bar-fill" id="cpu-fill" style="width: 0%;"></div></div>
            <span class="bar-val" id="cpu-val">—</span>
          </div>
          <div class="bar-row">
            <span class="bar-label">RAM</span>
            <div class="bar-track"><div class="bar-fill" id="ram-fill" style="width: 0%;"></div></div>
            <span class="bar-val" id="ram-val">—</span>
          </div>
          <div class="bar-row">
            <span class="bar-label">Storage</span>
            <div class="bar-track"><div class="bar-fill" id="disk-fill" style="width: 0%;"></div></div>
            <span class="bar-val" id="disk-val">—</span>
          </div>
          <div class="sys-info">
            <div class="sys-metric">
              <span class="label">Uptime</span>
              <span class="val" id="uptime-val">—</span>
            </div>
            <div class="sys-metric">
              <span class="label">Requests</span>
              <span class="val" id="requests-val">—</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Cloudflare Tunnel Panel -->
      <div class="panel">
        <div class="panel-title">Cloudflare Tunnel</div>
        <div class="panel-body">
          <div class="tunnel-box" id="tunnel-content">
            <div style="color: var(--text-dim);">Checking tunnel...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Right Column: Processes + Apps -->
    <div class="col">
      <!-- Processes Panel -->
      <div class="panel">
        <div class="panel-title">Processes</div>
        <div class="panel-body" style="padding: 0.4rem 0.6rem;">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th style="text-align: right; width: 65px; color: var(--c-yellow);">CPU%</th>
                <th style="text-align: right; width: 65px; color: var(--c-blue);">MEM%</th>
              </tr>
            </thead>
            <tbody id="procs-tbody">
              <tr><td colspan="4" style="color: var(--text-dim); text-align: center; padding: 1rem;">Loading...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Apps Panel -->
      <div class="panel">
        <div class="panel-title">Apps</div>
        <div class="panel-body" style="padding: 0.6rem 0.8rem;">
          <div id="apps-container">
            <div style="color: var(--text-dim);">Loading apps...</div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="footer">
    Backseat Terminal View • Polling every 2s
  </div>
</div>

<script>
// Public, read-only: everything comes from /public/status.
function barColor(pct) {
  if (pct >= 85) return 'var(--c-red)';
  if (pct >= 65) return 'var(--c-yellow)';
  return 'var(--c-green)';
}

function fmtUptime(s) {
  if (s == null) return '—';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

async function tick() {
  try {
    const h = await fetch('/public/status').then(r => r.json());
    const apps = h.apps;

    // Header info
    document.getElementById('hdr-conn').textContent = location.host;
    document.getElementById('hdr-right').className = 'header-right';
    document.getElementById('hdr-right').innerHTML = `<span class="dot active"></span> <span>Updated ${new Date().toLocaleTimeString()}</span>`;

    // CPU
    const cpuFill = document.getElementById('cpu-fill');
    const cpuVal = document.getElementById('cpu-val');
    const cpuPct = Math.min(100, Math.max(0, h.cpu_percent || 0));
    cpuFill.style.width = `${cpuPct}%`;
    cpuFill.style.backgroundColor = barColor(cpuPct);
    cpuVal.style.color = barColor(cpuPct);
    cpuVal.textContent = `${cpuPct.toFixed(0)}%`;

    // RAM
    const ramFill = document.getElementById('ram-fill');
    const ramVal = document.getElementById('ram-val');
    const ramPct = Math.min(100, Math.max(0, h.ram_percent || 0));
    ramFill.style.width = `${ramPct}%`;
    ramFill.style.backgroundColor = barColor(ramPct);
    ramVal.innerHTML = `<span style="color:${barColor(ramPct)}">${ramPct.toFixed(0)}%</span>  <span style="color:var(--text-dim);font-size:0.8rem">${h.ram_used_mb || 0}/${h.ram_total_mb || 0} MB</span>`;

    // Storage
    const diskFill = document.getElementById('disk-fill');
    const diskVal = document.getElementById('disk-val');
    const diskPct = Math.min(100, Math.max(0, h.storage_percent || 0));
    diskFill.style.width = `${diskPct}%`;
    diskFill.style.backgroundColor = barColor(diskPct);
    diskVal.innerHTML = `<span style="color:${barColor(diskPct)}">${diskPct.toFixed(0)}%</span>  <span style="color:var(--text-dim);font-size:0.8rem">${h.storage_used_gb || 0}/${h.storage_total_gb || 0} GB</span>`;

    // Uptime & Requests
    document.getElementById('uptime-val').textContent = fmtUptime(h.uptime_seconds);
    document.getElementById('requests-val').textContent = h.request_count || 0;

    // Tunnel Panel
    const tunnelEl = document.getElementById('tunnel-content');
    const t = h.tunnel || {};
    tunnelEl.innerHTML = t.active && t.hostnames.length ? `
      <div><span class="dot active"></span> <strong style="color:var(--c-green)">Active</strong> <span style="color:var(--text-dim);font-size:0.8rem">(Cloudflare named tunnel)</span></div>
      <table style="margin-top:0.4rem;"><tbody>
        ${t.hostnames.map(rt => `<tr><td><span class="dot ${rt.live ? 'active' : 'inactive'}"></span> ${esc(rt.hostname)}</td></tr>`).join('')}
      </tbody></table>` : '<div style="color:var(--text-dim);">○ No active tunnel</div>';

    // Processes
    const procsTbody = document.getElementById('procs-tbody');
    if (Array.isArray(h.processes) && h.processes.length > 0) {
      procsTbody.innerHTML = h.processes.slice(0, 8).map(p => {
        const cpu = p.cpu_percent || 0;
        const cpuColor = cpu > 50 ? 'var(--c-red)' : cpu > 20 ? 'var(--c-yellow)' : '#fff';
        return `<tr>
          <td style="color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px;">${esc(p.name)}</td>
          <td style="text-align:right;color:${cpuColor};font-weight:500;">${cpu.toFixed(1)}</td>
          <td style="text-align:right;color:var(--c-blue);">${(p.mem_percent || 0).toFixed(1)}</td>
        </tr>`;
      }).join('');
    } else {
      procsTbody.innerHTML = '<tr><td colspan="3" style="color:var(--text-dim);text-align:center;">No processes reported</td></tr>';
    }

    // Apps
    const appsEl = document.getElementById('apps-container');
    if (Array.isArray(apps) && apps.length > 0) {
      appsEl.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Name</th>
              <th>Status</th>
              <th style="text-align:right">Uptime</th>
              <th style="text-align:right">Restarts</th>
            </tr>
          </thead>
          <tbody>
            ${apps.map(a => {
              const badgeClass = a.status === 'running' ? 'badge-running' : a.status === 'crashed' ? 'badge-crashed' : 'badge-stopped';
              return `<tr>
                <td style="font-weight:600;color:#fff;">${esc(a.name)}</td>
                <td><span class="badge ${badgeClass}">${esc(a.status)}</span></td>
                <td style="text-align:right;color:var(--text-dim);">${fmtUptime(a.uptime_seconds)}</td>
                <td style="text-align:right;color:var(--text-dim);">${esc(a.restart_count)}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>`;
    } else {
      appsEl.innerHTML = `
        <div style="color:var(--text-dim);">No managed apps.</div>
        <div class="hint-text" style="margin-top:0.5rem;">Deploy one with:<br><span class="hint-code" style="display:inline-block;margin-top:0.25rem;">backseat deploy &lt;local&gt; &lt;remote&gt; --start "&lt;cmd&gt;"</span></div>`;
    }

  } catch (err) {
    const hdrRight = document.getElementById('hdr-right');
    if (hdrRight) {
      hdrRight.className = 'header-right err';
      hdrRight.innerHTML = `⚠ <span>Connection lost — retrying...</span>`;
    }
  }
}

tick();
setInterval(tick, 2000);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

BOOT_SCRIPT_PATH = Path.home() / ".termux" / "boot" / "start-backseat.sh"


def _install_boot_script() -> None:
    """Write a Termux:Boot script so the agent auto-starts after a phone reboot.

    Requires the separate Termux:Boot app (F-Droid) to be installed — Termux
    itself has no boot hook, Termux:Boot provides one via a broadcast receiver.
    """
    BOOT_SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "#!/data/data/com.termux/files/usr/bin/sh\n"
        "termux-wake-lock\n"
        "backseat-agent\n"
    )
    BOOT_SCRIPT_PATH.write_text(script, encoding="utf-8")
    os.chmod(BOOT_SCRIPT_PATH, 0o700)
    print(f"[backseat] Wrote boot script to {BOOT_SCRIPT_PATH}")
    print("[backseat] Install the 'Termux:Boot' app from F-Droid if you haven't —")
    print("[backseat] it's what actually triggers this script on reboot.")
    print("[backseat] Then open Termux:Boot once so Android grants it permission.")


def main() -> None:
    argv = sys.argv[1:]

    # Some terminals (notably Windows' default console codepage) can't encode
    # the block characters the QR code uses. Termux/Android is always UTF-8,
    # so this only matters when running the agent locally for testing.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    if "--install-boot" in argv:
        _install_boot_script()
        return


    check_dependencies()
    AGENT_HOME.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    if "--reset-pairing" in argv and TOKEN_FILE.exists():
        TOKEN_FILE.unlink()
        print("[backseat] Cleared saved pairing — a new pairing code will be required.")

    _load_persisted_token()
    _load_and_start_apps()

    global AGENT_PORT
    port = int(os.getenv("BACKSEAT_PORT", "8080"))
    AGENT_PORT = port
    ip = get_local_ip()
    show_pairing_info(ip, port, already_paired=paired)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    threading.Thread(target=_watchdog_loop, daemon=True).start()
    if REMOTE_FLAG_FILE.exists():
        threading.Thread(target=start_remote_tunnel, daemon=True).start()
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()

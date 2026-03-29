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

import logging
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import psutil
import qrcode
import uvicorn
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backseat")

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
    for pkg in ("fastapi", "uvicorn", "psutil", "qrcode"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"[backseat] Missing packages: {', '.join(missing)}")
        print(f"[backseat] Run: pip install {' '.join(missing)}")
        sys.exit(1)


def show_pairing_info(ip: str, port: int) -> None:
    pairing_string = f"backseat://{ip}:{port}#{PAIRING_TOKEN}"

    print("\n" + "=" * 52)
    print("  BACKSEAT AGENT")
    print("=" * 52)

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
    print(f"  Dashboard    :  http://{ip}:{port}/dashboard?token=<session>")
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
    log.info("Shutting down — stopping tunnel if active...")
    stop_tunnel_process()
    sys.exit(0)


# ── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    ip = get_local_ip()
    port = int(os.getenv("BACKSEAT_PORT", "8080"))
    show_pairing_info(ip, port)
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    yield
    stop_tunnel_process()
    log.info("Agent stopped.")


app = FastAPI(title="Backseat Agent", lifespan=lifespan, docs_url=None, redoc_url=None)


# ── Auth ───────────────────────────────────────────────────────────────────────

def require_auth(token: str) -> None:
    if not paired:
        raise HTTPException(status_code=403, detail="Agent not yet paired. Run backseat init.")
    if not secrets.compare_digest(token, SESSION_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token.")


# ── Models ─────────────────────────────────────────────────────────────────────

class PairRequest(BaseModel):
    pairing_token: str
    ssh_user: str


class PairResponse(BaseModel):
    session_token: str
    message: str


class RunRequest(BaseModel):
    command: str


class RunResponse(BaseModel):
    stdout: str
    stderr: str
    returncode: int


class ProcessInfo(BaseModel):
    pid: int
    name: str
    cpu_percent: float
    mem_percent: float


class HealthResponse(BaseModel):
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


class TunnelStartRequest(BaseModel):
    port: int = Field(ge=1, le=65535)


class TunnelStatusResponse(BaseModel):
    active: bool
    url: Optional[str] = None
    port: Optional[int] = None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/ping")
def ping():
    """Unauthenticated — used by laptop to check if agent is reachable before pairing."""
    return {"status": "ok", "paired": paired}


@app.post("/pair", response_model=PairResponse)
def pair(req: PairRequest):
    global paired, _pair_attempts, _pair_locked_until
    with _paired_lock:
        if paired:
            raise HTTPException(status_code=409, detail="Already paired.")

        now = time.time()
        if now < _pair_locked_until:
            remaining = int(_pair_locked_until - now)
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Try again in {remaining}s.",
            )

        if not secrets.compare_digest(req.pairing_token.upper().strip(), PAIRING_TOKEN):
            _pair_attempts += 1
            log.warning(f"Failed pairing attempt {_pair_attempts}/{MAX_PAIR_ATTEMPTS}")
            if _pair_attempts >= MAX_PAIR_ATTEMPTS:
                _pair_locked_until = now + PAIR_LOCKOUT_SECONDS
                _pair_attempts = 0
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many failed attempts. Locked for {PAIR_LOCKOUT_SECONDS}s.",
                )
            raise HTTPException(
                status_code=403,
                detail=f"Invalid pairing code. {MAX_PAIR_ATTEMPTS - _pair_attempts} attempts remaining.",
            )

        _pair_attempts = 0
        paired = True

    log.info(f"Paired successfully with user '{req.ssh_user}'")
    return PairResponse(session_token=SESSION_TOKEN, message="Paired successfully")


@app.get("/health", response_model=HealthResponse)
def health(x_backseat_token: str = Header(default="")):
    global request_count
    require_auth(x_backseat_token)
    request_count += 1

    ram = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    procs = []
    for p in sorted(
        psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]),
        key=lambda x: x.info["cpu_percent"] or 0,
        reverse=True,
    )[:10]:
        procs.append(ProcessInfo(
            pid=p.info["pid"],
            name=p.info["name"] or "unknown",
            cpu_percent=round(p.info["cpu_percent"] or 0, 1),
            mem_percent=round(p.info["memory_percent"] or 0, 1),
        ))

    return HealthResponse(
        cpu_percent=psutil.cpu_percent(interval=0.5),
        ram_percent=ram.percent,
        ram_used_mb=ram.used // (1024 * 1024),
        ram_total_mb=ram.total // (1024 * 1024),
        storage_percent=disk.percent,
        storage_used_gb=round(disk.used / (1024 ** 3), 1),
        storage_total_gb=round(disk.total / (1024 ** 3), 1),
        uptime_seconds=int(time.time() - START_TIME),
        request_count=request_count,
        processes=procs,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/run", response_model=RunResponse)
def run_command(req: RunRequest, x_backseat_token: str = Header(default="")):
    global request_count
    require_auth(x_backseat_token)
    request_count += 1

    log.info(f"Running command: {req.command!r}")
    try:
        result = subprocess.run(
            req.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=408, detail="Command timed out after 60 seconds.")

    return RunResponse(
        stdout=result.stdout,
        stderr=result.stderr,
        returncode=result.returncode,
    )


# ── Tunnel ─────────────────────────────────────────────────────────────────────

def _read_tunnel_url(proc: subprocess.Popen) -> None:
    global _tunnel_url
    if proc.stderr is None:
        return
    for line in proc.stderr:
        match = re.search(r"https://[a-z0-9\-]+\.trycloudflare\.com", line)
        if match:
            with _tunnel_lock:
                _tunnel_url = match.group(0)
            log.info(f"Tunnel URL: {_tunnel_url}")
            break


@app.get("/tunnel/status", response_model=TunnelStatusResponse)
def tunnel_status(x_backseat_token: str = Header(default="")):
    require_auth(x_backseat_token)
    with _tunnel_lock:
        active = _tunnel_process is not None and _tunnel_process.poll() is None
        return TunnelStatusResponse(
            active=active,
            url=_tunnel_url if active else None,
            port=_tunnel_port if active else None,
        )


@app.post("/tunnel/start", response_model=TunnelStatusResponse)
def tunnel_start(req: TunnelStartRequest, x_backseat_token: str = Header(default="")):
    global _tunnel_process, _tunnel_url, _tunnel_port
    require_auth(x_backseat_token)

    stop_tunnel_process()

    with _tunnel_lock:
        _tunnel_url = None
        _tunnel_port = req.port

    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{req.port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail="cloudflared not found. Install with: pkg install cloudflared",
        )
    except (OSError, PermissionError) as e:
        raise HTTPException(status_code=500, detail=f"Failed to start cloudflared: {e}")

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
            raise HTTPException(
                status_code=500,
                detail=f"cloudflared exited unexpectedly (code {proc.returncode}). Check your network.",
            )
        time.sleep(0.5)

    with _tunnel_lock:
        active = _tunnel_process is not None and _tunnel_process.poll() is None
        return TunnelStatusResponse(active=active, url=_tunnel_url, port=_tunnel_port)


@app.post("/tunnel/stop")
def tunnel_stop(x_backseat_token: str = Header(default="")):
    require_auth(x_backseat_token)
    stop_tunnel_process()
    log.info("Tunnel stopped.")
    return {"status": "stopped"}


# ── Web Dashboard ──────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(token: str = ""):
    if not paired or not secrets.compare_digest(token, SESSION_TOKEN):
        return HTMLResponse(content="<h3>Not authorized. Add ?token=your_session_token to the URL.</h3>", status_code=401)
    return HTMLResponse(content=_DASHBOARD_HTML)


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Backseat</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0f0f0f;color:#e0e0e0;font-family:'Segoe UI',system-ui,sans-serif;padding:1rem;min-height:100vh}
  h1{color:#7c6af7;font-size:1.3rem;margin-bottom:1rem;letter-spacing:.05em}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:.7rem;margin-bottom:1rem}
  .card{background:#1a1a1a;border:1px solid #2a2a2a;border-radius:10px;padding:.9rem}
  .label{font-size:.65rem;color:#888;text-transform:uppercase;letter-spacing:.1em;margin-bottom:.35rem}
  .value{font-size:1.5rem;font-weight:700;color:#fff}
  .sub{font-size:.7rem;color:#666;margin-top:.2rem}
  .bar-bg{background:#2a2a2a;border-radius:4px;height:5px;margin-top:.5rem}
  .bar{height:5px;border-radius:4px;background:#7c6af7;transition:width .5s}
  .bar.warn{background:#f0a500}.bar.danger{background:#e05555}
  .section{font-size:.65rem;color:#666;text-transform:uppercase;letter-spacing:.1em;margin:.9rem 0 .4rem}
  table{width:100%;border-collapse:collapse;font-size:.8rem}
  th{text-align:left;color:#555;font-weight:500;padding:.3rem .5rem;border-bottom:1px solid #222}
  td{padding:.3rem .5rem;border-bottom:1px solid #1a1a1a}
  .pill{display:inline-block;padding:.15rem .55rem;border-radius:20px;font-size:.7rem}
  .on{background:#1a3a1a;color:#4caf50;border:1px solid #2d6b2d}
  .off{background:#222;color:#666;border:1px solid #333}
  .url{font-size:.8rem;color:#7c6af7;word-break:break-all;margin-top:.4rem}
  .footer{font-size:.65rem;color:#333;text-align:right;margin-top:1rem}
</style>
</head>
<body>
<h1>⬡ Backseat</h1>
<div class="grid" id="stats"></div>
<div class="section">Cloudflare Tunnel</div>
<div class="card" id="tunnel"></div>
<div class="section">Processes</div>
<table><thead><tr><th>Name</th><th>CPU%</th><th>MEM%</th></tr></thead>
<tbody id="procs"></tbody></table>
<div class="footer" id="ts"></div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token')||'';
const H = {'x-backseat-token':TOKEN};
function bar(p){const c=p>85?'danger':p>65?'warn':'';return `<div class="bar-bg"><div class="bar ${c}" style="width:${p}%"></div></div>`}
function uptime(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`}
async function tick(){
  try{
    const[h,t]=await Promise.all([
      fetch('/health',{headers:H}).then(r=>r.json()),
      fetch('/tunnel/status',{headers:H}).then(r=>r.json())
    ]);
    document.getElementById('stats').innerHTML=`
      <div class="card"><div class="label">CPU</div><div class="value">${h.cpu_percent.toFixed(0)}%</div>${bar(h.cpu_percent)}</div>
      <div class="card"><div class="label">RAM</div><div class="value">${h.ram_percent.toFixed(0)}%</div><div class="sub">${h.ram_used_mb}/${h.ram_total_mb} MB</div>${bar(h.ram_percent)}</div>
      <div class="card"><div class="label">Storage</div><div class="value">${h.storage_percent.toFixed(0)}%</div><div class="sub">${h.storage_used_gb}/${h.storage_total_gb} GB</div>${bar(h.storage_percent)}</div>
      <div class="card"><div class="label">Uptime</div><div class="value" style="font-size:1.1rem">${uptime(h.uptime_seconds)}</div></div>
      <div class="card"><div class="label">Requests</div><div class="value">${h.request_count}</div></div>`;
    const te=s=>{const d=document.createElement('div');d.textContent=s;return d.innerHTML};
    if(t.active){
      const el=document.getElementById('tunnel');
      el.innerHTML=`<span class="pill on">● Active</span> <span style="color:#888;font-size:.8rem">port ${te(String(t.port))}</span>`;
      if(t.url){const u=document.createElement('div');u.className='url';u.textContent=t.url;el.appendChild(u);}
    }else{
      document.getElementById('tunnel').innerHTML=`<span class="pill off">○ Inactive</span>`;
    }
    const tbody=document.getElementById('procs');tbody.innerHTML='';
    h.processes.slice(0,8).forEach(p=>{const tr=document.createElement('tr');['name','cpu_percent','mem_percent'].forEach(k=>{const td=document.createElement('td');td.textContent=p[k];tr.appendChild(td);});tbody.appendChild(tr);});
    document.getElementById('ts').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('ts').textContent='Connection lost — retrying...'}
}
tick();setInterval(tick,3000);
</script>
</body>
</html>"""


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    check_dependencies()
    port = int(os.getenv("BACKSEAT_PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()

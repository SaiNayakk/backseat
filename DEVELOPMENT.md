# Backseat — Developer Guide

## What is this?
A Python CLI tool that turns your Android phone (running Termux) into a personal deploy server.
Deploy apps, run saved commands, monitor health, and manage Cloudflare tunnels — all from your laptop terminal.

## Architecture

```
[Laptop — backseat CLI]  ──SSH/SCP──▶  [Android Phone / Termux]
          │                                       │
          │◀──── HTTP (health, run, tunnel) ──────│
          │
  [Rich terminal dashboard]          [Web dashboard at :8080/dashboard]
```

### Two sides:
1. **`backseat/`** — Python CLI (Typer + Rich), runs on your laptop
2. **`agent/`** — FastAPI HTTP server, runs inside Termux on your phone

### How they talk:
| Channel | Used for |
|---|---|
| HTTP (httpx → FastAPI) | Health stats, running commands, tunnel management |
| SSH/SCP (paramiko) | Deploying files, SSH fallback when agent is down |

### Auth flow:
1. `python agent.py` — phone prints QR + 8-char pairing code
2. `backseat init` — laptop sends code to `/pair`, gets back a session token
3. All subsequent API calls use `x-backseat-token: <session_token>` header
4. SSH uses key or password (chosen at init, passwords never stored in config)

## Project Structure
```
backseat/
├── backseat/
│   ├── __init__.py
│   ├── __main__.py     # python -m backseat entry point
│   ├── cli.py          # all CLI commands (typer)
│   ├── config.py       # pydantic models + config file (~/.backseat/config.json)
│   ├── ssh.py          # SSH + SCP via paramiko
│   ├── health.py       # HTTP client for agent API
│   └── dashboard.py    # Rich live terminal dashboard
├── agent/
│   ├── agent.py        # FastAPI server — runs ON the phone in Termux
│   └── requirements.txt
├── DEVELOPMENT.md      # this file
├── README.md
├── LICENSE
├── pyproject.toml
└── requirements.txt
```

## CLI Reference
```bash
backseat init                     # pair with phone (QR + 8-char code)
backseat status                   # live dashboard: CPU, RAM, uptime, tunnel
backseat deploy <local> <remote>  # upload files via SCP + optional start cmd
backseat run <name>               # run a saved command (HTTP → SSH fallback)
backseat add <name>               # save a new command
backseat list                     # list all saved commands
backseat remove <name>            # delete a saved command
backseat connections              # list saved phone connections
backseat tunnel start <port>      # start Cloudflare quick tunnel on phone
backseat tunnel stop              # stop tunnel
backseat tunnel status            # show tunnel URL
```

## Agent API
| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | /ping | None | Reachability check (used before pairing) |
| POST | /pair | None | Exchange pairing code for session token |
| GET | /health | Token | CPU, RAM, storage, uptime, request count, processes |
| POST | /run | Token | Run shell command, return stdout/stderr/code |
| GET | /tunnel/status | Token | Tunnel active state + public URL |
| POST | /tunnel/start | Token | Start cloudflared quick tunnel |
| POST | /tunnel/stop | Token | Stop active tunnel |
| GET | /dashboard | Token (query param) | Web dashboard HTML |

## Key Libraries
| Library | Side | Purpose |
|---|---|---|
| `typer` | laptop | CLI framework |
| `rich` | laptop | Terminal dashboard + formatting |
| `paramiko` | laptop | SSH + SCP |
| `httpx` | laptop | HTTP client for agent API |
| `pydantic` | both | Models + config validation |
| `fastapi` + `uvicorn` | phone | HTTP server |
| `psutil` | phone | System metrics |
| `qrcode` | phone | QR code on startup |

## Config
`~/.backseat/config.json` — stores connections and saved commands.
- Permissions: `0600` on Unix (owner read/write only)
- Passwords are **never stored** — prompted at runtime
- Writes are atomic (`.tmp` → rename)

## Phone Setup
```bash
# In Termux
pkg install python openssh cloudflared
pip install -r agent/requirements.txt
sshd                   # SSH server on port 8022
python agent.py        # Backseat agent on port 8080
```

## Dev Notes
- `backseat run` tries HTTP first, falls back to SSH if agent is unreachable
- Directory SCP walks the tree manually — paramiko has no `scp -r`
- Never use `typer.prompt()` or `input()` inside a `rich.live.Live` context — corrupts output
- Agent uses `threading.Lock` for all tunnel state mutations
- Rich Live dashboard polls health + tunnel sequentially (2s interval)
- `check_dependencies()` in agent.py gives a clean error if packages are missing

# Backseat — Developer Guide

## What is this?
A Python CLI tool that turns your Android phone (running Termux) into a personal deploy server
and 24/7 host: deploy apps, keep them running with auto-restart, run saved commands, monitor
health, and manage Cloudflare tunnels — all from your laptop terminal.

## Architecture

```
[Laptop — backseat CLI]  ──SSH/SCP──▶  [Android Phone / Termux]
          │                                       │
          │◀──── HTTP (health, run, apps, tunnel)─│
          │
  [Rich terminal dashboard]          [Web dashboard at :8080/dashboard]
```

### Two sides, one package:
1. **`backseat/`** — Python CLI (Typer + Rich), runs on your laptop
2. **`backseat/agent.py`** — Flask HTTP server, runs inside Termux on your phone

Both ship from the same `backseat` package and `pyproject.toml` — there is no separate `agent/`
directory or FastAPI process; `backseat-agent` is a console-script entry point into
`backseat/agent.py`.

### How they talk:
| Channel | Used for |
|---|---|
| HTTP (httpx → Flask) | Health stats, running commands, managed apps, tunnel management |
| SSH/SCP (paramiko) | Deploying files, SSH fallback when agent is down |

### Auth flow:
1. `backseat-agent` — phone prints QR + 8-char pairing code
2. `backseat init` — laptop sends code to `/pair`, gets back a session token
3. All subsequent API calls use `x-backseat-token: <session_token>` header
4. SSH uses key or password (chosen at init, passwords never stored in config)
5. The agent persists its session token to `~/.backseat/agent/token.json` on the phone, so a
   restart of `backseat-agent` (crash, reboot, manual) doesn't force re-pairing. Use
   `backseat-agent --reset-pairing` to invalidate it deliberately.

### Managed apps (the 24/7 piece):
`backseat deploy <local> <remote> --start "<cmd>"` registers `<cmd>` as a named "app" on the agent
instead of firing a detached SSH `nohup` process. The agent then owns its lifecycle:
- Spawned via `subprocess.Popen(..., start_new_session=True)` (non-Windows) so it survives the
  agent process dying, not just a laptop disconnect.
- A watchdog thread (`_watchdog_loop` in `agent.py`) polls liveness every 3s and restarts crashed
  apps, with exponential backoff capped at 60s between attempts.
- App registration (name, command, cwd, desired running/stopped state) is persisted to
  `~/.backseat/agent/apps.json` on the phone, so it survives an agent restart. On startup the
  agent tries to re-adopt an already-running process by matching cmdline against the recorded
  command (pids aren't persisted — they aren't stable across a reboot).
- stdout/stderr are captured to `~/.backseat/agent/logs/<name>.log` (rotated at 5MB, one backup
  kept), readable via `backseat apps logs <name>` (tail or `--follow` streaming).

`backseat-agent --install-boot` writes a Termux:Boot script (`~/.termux/boot/start-backseat.sh`)
so the agent — and therefore all managed apps — comes back after a phone reboot too. This needs
the separate Termux:Boot app from F-Droid; Termux itself has no boot hook.

## Project Structure
```
backseat/
├── backseat/
│   ├── __init__.py
│   ├── __main__.py     # python -m backseat entry point
│   ├── cli.py          # all CLI commands (typer): deploy, run, apps, tunnel, ...
│   ├── config.py       # pydantic models + laptop config file (~/.backseat/config.json)
│   ├── ssh.py          # SSH + SCP via paramiko
│   ├── health.py       # HTTP client for agent API (health, apps, tunnel)
│   ├── dashboard.py    # Rich live terminal dashboard
│   └── agent.py        # Flask server — runs ON the phone in Termux
├── npm/                 # `npm install -g backseat` wrapper that shells out to pip
├── DEVELOPMENT.md       # this file
├── README.md
├── LICENSE
├── pyproject.toml
└── requirements.txt
```

## CLI Reference
```bash
backseat init                     # pair with phone (QR + 8-char code)
backseat status                   # live dashboard: CPU, RAM, uptime, apps, tunnel
backseat deploy <local> <remote>  # upload files via SCP
backseat deploy <local> <remote> --start "<cmd>"            # ...and register as a managed app
backseat deploy <local> <remote> --start "<cmd>" --foreground  # ...run once over SSH instead
backseat run <name>               # run a saved command (HTTP → SSH fallback)
backseat add <name>               # save a new command
backseat list                     # list all saved commands
backseat remove <name>            # delete a saved command
backseat connections              # list saved phone connections
backseat apps list                # list managed apps: status, pid, uptime, restarts
backseat apps logs <name> [-f]    # tail or follow an app's combined stdout/stderr
backseat apps start/stop/restart/remove <name>
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
| GET | /apps | Token | List managed apps + runtime status |
| POST | /apps | Token | Register + start a managed app `{name, command, cwd}` |
| POST | /apps/\<name\>/start | Token | Start (clears crash-backoff state) |
| POST | /apps/\<name\>/stop | Token | Stop; watchdog won't auto-restart it |
| POST | /apps/\<name\>/restart | Token | Stop then start, resets restart_count |
| DELETE | /apps/\<name\> | Token | Stop, forget, delete its log file |
| GET | /apps/\<name\>/logs | Token | `?lines=N` tail, or `?follow=1` to stream (chunked) |
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
| `httpx` | laptop | HTTP client for agent API (incl. streaming log follow) |
| `pydantic` | both | Models + config validation |
| `flask` | phone | HTTP server |
| `psutil` | phone | System metrics + liveness/adoption checks for managed apps |
| `qrcode` | phone | QR code on startup |

## Config
- `~/.backseat/config.json` (laptop) — connections and saved commands. `0600` on Unix, atomic
  writes (`.tmp` → rename).
- `~/.backseat/agent/` (phone) — `token.json` (persisted session token), `apps.json` (registered
  apps + desired state), `logs/<name>.log` (per-app output).

## Phone Setup
```bash
# In Termux
pkg install python openssh cloudflared
pip install "backseat[agent]"
sshd                   # SSH server on port 8022
backseat-agent         # Backseat agent on port 8080

# Optional: survive phone reboots (needs the Termux:Boot app from F-Droid)
backseat-agent --install-boot
```

## Dev Notes
- `backseat run` tries HTTP first, falls back to SSH if agent is unreachable. `backseat deploy
  --start` (without `--foreground`) requires the agent — managed apps have no SSH fallback since
  they need the watchdog.
- Directory SCP walks the tree manually — paramiko has no `scp -r`
- Never use `typer.prompt()` or `input()` inside a `rich.live.Live` context — corrupts output
- Agent uses `threading.Lock`/`RLock` for tunnel state and app-registry mutations respectively
  (`_tunnel_lock`, `_apps_lock`)
- Rich Live dashboard polls health + tunnel + apps sequentially (2s interval)
- `check_dependencies()` in agent.py gives a clean error if packages are missing
- Both `cli.py` and `agent.py` reconfigure stdout/stderr to UTF-8 on import/startup — Rich's
  checkmarks/bullets and the QR code otherwise crash on Windows' legacy console codepage (harmless
  on Termux, which is always UTF-8, but breaks local testing on Windows)
- App liveness/re-adoption after a restart is a heuristic (cmdline substring match via `psutil`),
  not exact — pids aren't persisted since they aren't stable across a reboot

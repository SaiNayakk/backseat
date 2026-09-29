# Backseat

> Your phone does the work. You take the backseat.

Backseat is an open source Python CLI that turns your Android phone (running [Termux](https://termux.dev)) into a personal deploy server. Deploy apps, run saved command workflows, monitor system health, and expose services publicly via Cloudflare tunnels — all from your laptop terminal.

No VPS needed. No monthly bill. Just your phone.

---

## How it works

```
[Laptop — backseat CLI]  ──SSH/SCP──▶  [Android Phone / Termux]
          │                                       │
          │◀────────── HTTP ───────────────────────│
          │
  [Terminal dashboard]              [Web dashboard — any browser]
```

A lightweight agent runs on your phone inside Termux. Your laptop pairs with it using a QR code + pairing code, then communicates over your local network (or via Cloudflare tunnel when remote).

---

## What it is (and isn't)

**Right now:**
- A **Python CLI** (`backseat`) that runs on your laptop
- A **Python agent** (`backseat-agent`) that runs inside Termux on your phone
- A **web dashboard** served by the agent, viewable in any browser

**Not yet:**
- A native Android app — the phone side requires Termux. A native app is on the roadmap.

---

## Requirements

**Laptop:** Python 3.10+, any OS

**Phone:** Android with [Termux](https://termux.dev) installed
- Install Termux from [F-Droid](https://f-droid.org/packages/com.termux/), not the Play Store
- Same WiFi network as your laptop (or use `backseat tunnel` for remote access)

---

## Installation

### Laptop

Pick whichever package manager you already have:

```bash
# npm (no Python required — auto-installs it)
npm install -g backseat

# pip
pip install "backseat[deploy]"
```

### Phone — one command in Termux
```bash
pkg install python openssh && pip install "backseat[agent]" && sshd && backseat-agent
```

That's it. Your phone will display a QR code and a pairing code.

---

## Quickstart

### 1. On your phone (Termux)
```bash
pkg install python openssh && pip install "backseat[agent]" && sshd && backseat-agent
```

You'll see:
```
====================================================
  BACKSEAT AGENT
====================================================
  [QR CODE]

  IP Address   :  192.168.1.5
  Port         :  8080
  Pair Code    :  A3F9C1B2

  On your laptop:
    backseat init
====================================================
```

### 2. On your laptop
```bash
backseat init
```

Follow the prompts — enter your phone's IP and the pairing code shown on screen.

### 3. You're paired. Try it:
```bash
backseat status                          # live terminal dashboard
backseat deploy ./myapp ~/myapp          # push your app to the phone
backseat tunnel start 3000               # expose port 3000 to the internet
```

---

## Commands

### Setup
```bash
backseat init                     # pair with your phone
backseat connections              # list saved connections
```

### Monitor
```bash
backseat status                   # live terminal dashboard (CPU, RAM, uptime, tunnel, apps)
backseat tunnel status            # show active tunnel URL
```

### Deploy
```bash
backseat deploy ./myapp ~/myapp                          # upload folder
backseat deploy ./myapp ~/myapp --start "python app.py"   # upload + run as a managed app
backseat deploy ./myapp ~/myapp --start "npm run build" --foreground  # one-off command, not persisted
```

`--start` registers the command as a managed app on the agent (see below) — it keeps running
after the CLI exits, restarts itself if it crashes, and survives you closing your laptop. Use
`--foreground` instead for a one-off command (e.g. a build step) that shouldn't persist.

### Apps — keep things running 24/7
```bash
backseat apps list                # name, status, uptime, restart count
backseat apps logs myapp          # last 200 lines
backseat apps logs myapp --follow # stream new lines live
backseat apps stop myapp          # stop it (won't be auto-restarted)
backseat apps start myapp         # start it again
backseat apps restart myapp       # restart + reset its crash-backoff
backseat apps remove myapp        # stop it and forget it (deletes logs)
```

Apps are supervised by the agent, not by your laptop: a crash triggers an automatic restart with
increasing backoff (capped at 60s between attempts), and app state (what's registered, whether it
should be running) is persisted on the phone so it survives an agent restart too. Combine with
`backseat-agent --install-boot` (below) so apps come back after a phone reboot, not just an agent
restart.

### Saved commands
```bash
backseat add restart              # save a command called "restart"
backseat run restart              # run it (tries HTTP, falls back to SSH)
backseat list                     # list all saved commands
backseat remove restart           # delete one
```

Saved commands are for one-off actions (`git pull && restart`, `df -h`, ...), not long-running
processes — use `backseat deploy --start` / `backseat apps` for anything you want to stay up.

### Cloudflare tunnels
```bash
backseat tunnel start 3000        # expose port 3000 publicly via Cloudflare
backseat tunnel status            # show the public URL
backseat tunnel stop              # stop the tunnel
```

---

## Web Dashboard

Open in any browser on the same network:
```
http://<phone-ip>:8080/dashboard?token=<your-session-token>
```

Your session token is shown after `backseat init` and stored in `~/.backseat/config.json`.

Shows: CPU, RAM, storage, uptime, request count, managed apps, Cloudflare tunnel status, and top processes. Auto-refreshes every 3 seconds. Works locally or publicly when a tunnel is active.

---

## Running 24/7 — surviving reboots and crashes

Three things need to be true for a phone to act as always-on hosting: apps restart themselves
after a crash, apps and pairing survive an agent restart, and the agent comes back after a phone
reboot. Backseat handles the first two automatically; the third needs one-time setup:

```bash
# In Termux, after installing the Termux:Boot app from F-Droid:
backseat-agent --install-boot
```

This writes `~/.termux/boot/start-backseat.sh`, which Termux:Boot runs on device boot — it takes a
`termux-wake-lock` (so Android doesn't deep-sleep the process) and starts `backseat-agent`. Open
the Termux:Boot app once after installing it so Android grants it the permission it needs.

Session pairing already survives an agent restart on its own — `backseat-agent` persists its
session token to disk, so you won't need to run `backseat init` again unless you explicitly reset
it with `backseat-agent --reset-pairing`.

---

## Security

Backseat is a **personal, local-network tool**. It is not hardened for direct public internet exposure.

### Known limitations and why they exist

**1. No HTTPS between laptop and agent**

All communication is plain HTTP over your local network. Adding TLS to a local IP requires certificate management and breaks the zero-config setup goal.

*Mitigation:* Use on trusted networks only. For remote access, use `backseat tunnel` — traffic goes through Cloudflare's encrypted edge rather than exposing the agent directly.

**2. Session token has no expiration**

The session token is valid until you explicitly reset pairing. It's persisted on the phone at
`~/.backseat/agent/token.json` specifically so a crash or reboot doesn't force re-pairing — which
means a compromised token stays valid indefinitely, not just until the next restart.

*Mitigation:* Token is stored at `~/.backseat/config.json` (laptop) and `~/.backseat/agent/token.json` (phone) with `0600` permissions (owner read/write only) on Unix. Run `backseat-agent --reset-pairing` to invalidate the current token and require a fresh pairing code.

**3. `/run` and `/apps` execute arbitrary shell commands, unsandboxed**

The agent runs any shell command sent to it, whether a one-off `backseat run` or a long-lived
managed app — with the same permissions as the Termux user, no isolation between apps or from the
rest of your phone's storage. This is the feature — it's how deploys and command execution work.
It's protected behind the session token.

*Mitigation:* Keep your token secure and only deploy code you trust — same trust model as running
it on a shared server. Never expose port 8080 to the public internet directly.

**4. Web dashboard token in URL**

The dashboard authenticates via `?token=` query parameter, which appears in browser history and access logs.

*Mitigation:* Don't use the dashboard on shared computers. Cookie-based auth is on the roadmap.

**5. Auto-restarted apps keep running even if you forget about them**

An app that keeps crashing will keep being restarted indefinitely (with backoff), and a stopped
Termux session doesn't stop apps that were started with `start_new_session` — they outlive the
agent process that spawned them.

*Mitigation:* Use `backseat apps list` / `backseat apps stop` to check what's actually running,
especially before assuming a phone is idle (for battery/data reasons).

**6. Single client only**

Only one laptop can be paired at a time. A second pairing attempt returns 409. This is intentional — Backseat is a personal tool.

---

## Config

`~/.backseat/config.json` (laptop) stores your phone connections and saved commands.
`~/.backseat/agent/` (phone) stores the persisted session token, the registered apps list, and
per-app logs.

- Permissions: `0600` on Unix (owner-only)
- **Passwords are never stored** — prompted at runtime if you use password auth
- Writes are atomic to prevent corruption

---

## Contributing

Contributions are welcome. See [DEVELOPMENT.md](DEVELOPMENT.md) for architecture notes.

1. Fork the repo
2. Create a branch: `git checkout -b feature/your-feature`
3. Make your changes
4. Open a pull request

Please open an issue before starting large changes.

---

## Roadmap

- [x] Persistent pairing across agent restarts
- [x] Managed apps: auto-restart on crash, persisted across agent restarts, log streaming
- [x] Boot auto-start via Termux:Boot (`backseat-agent --install-boot`)
- [ ] Native Android app (no Termux required)
- [ ] HTTPS with self-signed certificate
- [ ] Cookie-based dashboard auth
- [ ] Multiple paired clients
- [ ] Deploy hooks (pre/post commands)
- [ ] Named Cloudflare tunnels for a stable public URL (quick tunnels rotate on every restart)

---

## License

MIT — see [LICENSE](LICENSE).

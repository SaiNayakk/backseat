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
backseat status                   # live terminal dashboard (CPU, RAM, uptime, tunnel)
backseat tunnel status            # show active tunnel URL
```

### Deploy
```bash
backseat deploy ./myapp ~/myapp                          # upload folder
backseat deploy ./myapp ~/myapp --start "python app.py" # upload + start in background
```

### Saved commands
```bash
backseat add restart              # save a command called "restart"
backseat run restart              # run it (tries HTTP, falls back to SSH)
backseat list                     # list all saved commands
backseat remove restart           # delete one
```

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

Shows: CPU, RAM, storage, uptime, request count, Cloudflare tunnel status, and top processes. Auto-refreshes every 3 seconds. Works locally or publicly when a tunnel is active.

---

## Security

Backseat is a **personal, local-network tool**. It is not hardened for direct public internet exposure.

### Known limitations and why they exist

**1. No HTTPS between laptop and agent**

All communication is plain HTTP over your local network. Adding TLS to a local IP requires certificate management and breaks the zero-config setup goal.

*Mitigation:* Use on trusted networks only. For remote access, use `backseat tunnel` — traffic goes through Cloudflare's encrypted edge rather than exposing the agent directly.

**2. Session token has no expiration**

The session token is valid for the lifetime of the agent process. If compromised, access persists until the agent restarts.

*Mitigation:* Token is stored at `~/.backseat/config.json` with `0600` permissions (owner read/write only) on Unix. Restarting `backseat-agent` generates a new token and invalidates the old one.

**3. `/run` executes arbitrary shell commands**

The agent runs any shell command sent to it. This is the feature — it's how `backseat run` works. It is protected behind the session token.

*Mitigation:* Keep your token secure. Never expose port 8080 to the public internet directly.

**4. Web dashboard token in URL**

The dashboard authenticates via `?token=` query parameter, which appears in browser history and access logs.

*Mitigation:* Don't use the dashboard on shared computers. Cookie-based auth is on the roadmap.

**5. Re-pairing required after agent restart**

The pairing code regenerates every time `backseat-agent` starts. After 5 failed attempts the endpoint locks for 60 seconds.

*Mitigation:* Persistent pairing across restarts is planned.

**6. Single client only**

Only one laptop can be paired at a time. A second pairing attempt returns 409. This is intentional — Backseat is a personal tool.

---

## Config

`~/.backseat/config.json` stores your phone connections and saved commands.

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

- [ ] Native Android app (no Termux required)
- [ ] Persistent pairing across agent restarts
- [ ] HTTPS with self-signed certificate
- [ ] Cookie-based dashboard auth
- [ ] Multiple paired clients
- [ ] Deploy hooks (pre/post commands)
- [ ] Live log streaming from running processes

---

## License

MIT — see [LICENSE](LICENSE).

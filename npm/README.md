# backseat

> Your phone does the work. You take the backseat.

Backseat turns an Android phone running Termux into a personal deploy server: pair your laptop
with a QR code, deploy files or whole projects over SSH, run them as managed apps that
auto-restart on crash and survive reboots, monitor CPU/RAM/storage/uptime from a live terminal or
web dashboard, and expose local ports to the internet via Cloudflare tunnels — no VPS or monthly
hosting bill.

This npm package is a thin installer, not the implementation. Backseat itself is a Python CLI;
running `backseat` (or `npx backseat`) after installing this package finds a Python 3.10+
interpreter on your machine, installs the real package via `pip install "backseat[deploy]"`, and
forwards all arguments to it.

## Install

```bash
npm install -g backseat
```

Requires Python 3.10+ already available on your PATH.

## Quickstart

```bash
# On your phone, in Termux:
pkg install python openssh && pip install "backseat[agent]" && sshd && backseat-agent

# On your laptop:
backseat init
backseat status
backseat deploy ./myapp ~/myapp --start "python app.py"
```

## Full documentation

This package intentionally ships no docs of its own beyond this file — see the main project for
the full command reference, architecture notes, and security model:

https://github.com/SaiNayakk/backseat

## License

MIT — see the [repository](https://github.com/SaiNayakk/backseat) for details.

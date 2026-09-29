#!/data/data/com.termux/files/usr/bin/bash
# One-command phone setup for Backseat. Safe to run any number of times.
#   bash termux-setup.sh
# Installs what's missing, starts the agent, and makes it come back after a reboot.
set -e

echo "== packages"
# A full upgrade, not just update: a newer python with an older libexpat breaks pip itself.
pkg update -y
pkg upgrade -y
# clang: psutil has a small C extension that pip compiles on the phone.
pkg install -y python openssh cloudflared git termux-api clang

echo "== backseat agent"
# The agent needs only these three. --no-deps skips the laptop-side libraries
# (pydantic needs Rust to build, which fails on Termux).
pip install --upgrade flask psutil qrcode
pip install --upgrade --no-deps backseat

echo "== start on boot (needs the Termux:Boot app from F-Droid, opened once)"
backseat-agent --install-boot || echo "   skipped: install Termux:Boot, open it once, then rerun"

echo "== start now"
termux-wake-lock || true
pgrep -x sshd >/dev/null || sshd
if pgrep -f backseat-agent >/dev/null; then
  echo "   agent already running"
else
  nohup backseat-agent > ~/backseat-agent.log 2>&1 &
  sleep 2
  echo "   agent started; pairing QR and code are in ~/backseat-agent.log"
fi

echo "Done. Apps you deployed before restart automatically once the agent is up."

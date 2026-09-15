#!/usr/bin/env bash
# Headless Docker on a Mac without admin rights: Colima (Lima VM, Apple Virtualization) + docker CLI,
# installed under ~/.local, started by launchd, then the phonepilot image is built and the backend
# switched to one container per phone session (PHONEPILOT_SANDBOX=docker).
#   bash deploy/mac/docker.sh
set -euo pipefail
APP_DIR="${PHONEPILOT_DIR:-$HOME/phonepilot}"
BIN="$HOME/.local/bin"; mkdir -p "$BIN" "$APP_DIR/logs"
export PATH="$BIN:$PATH"
ARCH=$(uname -m); [ "$ARCH" = "arm64" ] && LARCH=arm64 DARCH=aarch64 || LARCH=x86_64 DARCH=x86_64
say() { printf '\033[1;33m== %s\033[0m\n' "$*"; }

say "lima + colima + docker cli (user-local)"
if ! command -v limactl >/dev/null 2>&1; then
  LV=$(curl -fsSL https://api.github.com/repos/lima-vm/lima/releases/latest | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
  curl -fsSL -o /tmp/lima.tgz "https://github.com/lima-vm/lima/releases/download/${LV}/lima-${LV#v}-Darwin-${LARCH}.tar.gz"
  mkdir -p "$HOME/.local" && tar -xzf /tmp/lima.tgz -C "$HOME/.local"   # provides bin/limactl + share/lima
fi
if ! command -v colima >/dev/null 2>&1; then
  curl -fsSL -o "$BIN/colima" "https://github.com/abiosoft/colima/releases/latest/download/colima-Darwin-${LARCH}"
  chmod +x "$BIN/colima"
fi
if ! command -v docker >/dev/null 2>&1; then
  DV=$(curl -fsSL https://download.docker.com/mac/static/stable/${DARCH}/ | grep -o 'docker-[0-9][0-9.]*\.tgz' | sort -V | tail -1)
  curl -fsSL -o /tmp/docker.tgz "https://download.docker.com/mac/static/stable/${DARCH}/${DV}"
  tar -xzf /tmp/docker.tgz -C /tmp && cp /tmp/docker/docker "$BIN/docker" && chmod +x "$BIN/docker"
fi
limactl --version | head -1; colima version | head -1; docker --version

say "colima start (vz + virtiofs, home mounted writable)"
colima start --vm-type vz --mount-type virtiofs --cpu 4 --memory 8 --disk 60 --mount "$HOME:w" 2>&1 | tail -3 || colima start 2>&1 | tail -3
docker info --format 'engine {{.ServerVersion}} · {{.NCPU}} cpu · {{.MemTotal}} bytes'

say "launchd: start colima at login"
L=com.phonepilot.colima; P="$HOME/Library/LaunchAgents/$L.plist"
cat > "$P" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$L</string>
  <key>ProgramArguments</key><array><string>$BIN/colima</string><string>start</string></array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>$BIN:/usr/local/bin:/usr/bin:/bin</string><key>HOME</key><string>$HOME</string></dict>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/logs/colima.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/logs/colima.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$P" || true

say "build phonepilot image"
docker build -q -t phonepilot:latest "$APP_DIR" | tail -1

say "switch backend to docker"
cd "$APP_DIR"
grep -v '^PHONEPILOT_SANDBOX=' .env > .env.tmp || true; mv .env.tmp .env
echo "PHONEPILOT_SANDBOX=docker" >> .env
# the web tier must see the docker CLI + socket
grep -v '^DOCKER_HOST=' .env > .env.tmp || true; mv .env.tmp .env
echo "DOCKER_HOST=unix://$HOME/.colima/default/docker.sock" >> .env
launchctl kickstart -k "gui/$(id -u)/com.phonepilot.serve"
sleep 3; curl -s http://127.0.0.1:8080/healthz; echo
tail -2 "$APP_DIR/logs/serve.log"

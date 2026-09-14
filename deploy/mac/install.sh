#!/usr/bin/env bash
# Install PhonePilot as an always-on service on a Mac (tested target: Mac Studio, Apple Silicon).
#
#   curl -fsSL https://raw.githubusercontent.com/ShryukGrandhi/phonepilot/main/deploy/mac/install.sh | bash
#   # or from a checkout:  bash deploy/mac/install.sh
#
# What it does: clones/updates ~/phonepilot, creates a venv with uv, installs adb + cloudflared via
# Homebrew, writes ~/phonepilot/.env (master key generated once), and registers a launchd agent that
# runs `phonepilot serve` on 127.0.0.1:8080 and a Cloudflare quick tunnel that gives it a public
# https URL. Re-running is safe: it updates code and restarts the agents.
set -euo pipefail

APP_DIR="$HOME/phonepilot"
REPO="https://github.com/ShryukGrandhi/phonepilot.git"
PORT="${PHONEPILOT_PORT:-8080}"
LABEL="com.phonepilot.serve"
TUNNEL_LABEL="com.phonepilot.tunnel"

say() { printf '\033[1;33m== %s\033[0m\n' "$*"; }

say "prerequisites"
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required: https://brew.sh"; exit 1
fi
command -v uv >/dev/null 2>&1 || brew install uv
command -v adb >/dev/null 2>&1 || brew install --cask android-platform-tools
command -v cloudflared >/dev/null 2>&1 || brew install cloudflared
command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg || true

say "code"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull -q --ff-only
else
  git clone -q "$REPO" "$APP_DIR"
fi
cd "$APP_DIR"
uv venv -q -p 3.13 .venv
uv pip install -q -e "."

say "config"
touch .env
if ! grep -q '^PHONEPILOT_MASTER_KEY=' .env; then
  echo "PHONEPILOT_MASTER_KEY=$(.venv/bin/phonepilot serve --generate-master-key)" >> .env
fi
for var in PHONEPILOT_POOL_PHONE_KEY PHONEPILOT_POOL_GEMINI_KEY PHONEPILOT_POOL_ANTHROPIC_KEY; do
  if [ -n "${!var:-}" ] && ! grep -q "^$var=" .env; then echo "$var=${!var}" >> .env; fi
done
grep -q '^PHONEPILOT_TRANSPORT=' .env || echo "PHONEPILOT_TRANSPORT=${PHONEPILOT_TRANSPORT:-http}" >> .env
grep -q '^PHONEPILOT_SANDBOX=' .env || echo "PHONEPILOT_SANDBOX=${PHONEPILOT_SANDBOX:-process}" >> .env
chmod 600 .env
mkdir -p data logs

say "launchd: $LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$APP_DIR/.venv/bin/phonepilot</string><string>serve</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>$PORT</string>
    <string>--data</string><string>$APP_DIR/data</string><string>--secure-cookies</string><string>--trust-proxy</string>
  </array>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/logs/serve.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/logs/serve.err.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "launchd: $TUNNEL_LABEL (public https via Cloudflare quick tunnel)"
TPLIST="$HOME/Library/LaunchAgents/$TUNNEL_LABEL.plist"
cat > "$TPLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$TUNNEL_LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v cloudflared)</string><string>tunnel</string><string>--no-autoupdate</string>
    <string>--url</string><string>http://127.0.0.1:$PORT</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/logs/tunnel.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/logs/tunnel.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$TUNNEL_LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$TPLIST"

say "waiting for the service and the tunnel URL"
for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
for _ in $(seq 1 40); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' logs/tunnel.log 2>/dev/null | tail -1 || true)
  [ -n "$URL" ] && break; sleep 1
done
echo
echo "PhonePilot is running."
echo "  local:  http://127.0.0.1:$PORT/login"
echo "  public: ${URL:-(tunnel URL not found yet; tail logs/tunnel.log)}"
echo "  first signup becomes admin; mint invites in Settings or: .venv/bin/phonepilot serve --data data --invite"
echo "  logs:   $APP_DIR/logs/"

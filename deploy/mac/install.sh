#!/usr/bin/env bash
# Install PhonePilot as an always-on backend on a Mac (Apple Silicon or Intel). No Homebrew, no sudo:
# uv + Python 3.13 (astral installer), adb (Google platform-tools zip), optional cloudflared, launchd agent.
#
#   bash deploy/mac/install.sh
#   PHONEPILOT_POOL_PHONE_KEY=pck_… PHONEPILOT_POOL_GEMINI_KEY=AIza… bash deploy/mac/install.sh
#
# Re-running updates the code and restarts the agent. Public exposure is a separate step:
#   deploy/mac/expose.sh   (Tailscale Funnel if available, else a Cloudflare quick tunnel)
set -euo pipefail

APP_DIR="${PHONEPILOT_DIR:-$HOME/phonepilot}"
REPO="https://github.com/ShryukGrandhi/phonepilot.git"
PORT="${PHONEPILOT_PORT:-8080}"
LABEL="com.phonepilot.serve"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$HOME/.local/share/uv/bin:$PATH"

say() { printf '\033[1;33m== %s\033[0m\n' "$*"; }

say "uv + python"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" sh >/dev/null
fi
uv --version
uv python install 3.13 >/dev/null 2>&1 || true

say "adb (platform-tools)"
if ! command -v adb >/dev/null 2>&1; then
  TMP=$(mktemp -d)
  curl -fsSL -o "$TMP/pt.zip" https://dl.google.com/android/repository/platform-tools-latest-darwin.zip
  unzip -q -o "$TMP/pt.zip" -d "$HOME/.local"
  ln -sf "$HOME/.local/platform-tools/adb" "$BIN/adb"
  rm -rf "$TMP"
fi
adb --version | head -1
[ -f "$HOME/.android/adbkey.pub" ] || { mkdir -p "$HOME/.android"; adb keygen "$HOME/.android/adbkey" >/dev/null 2>&1 || true; }

say "code -> $APP_DIR"
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
  if [ -n "${!var:-}" ]; then
    grep -v "^$var=" .env > .env.tmp || true; mv .env.tmp .env
    echo "$var=${!var}" >> .env
  fi
done
grep -q '^PHONEPILOT_TRANSPORT=' .env || echo "PHONEPILOT_TRANSPORT=${PHONEPILOT_TRANSPORT:-adb}" >> .env
grep -q '^PHONEPILOT_SANDBOX=' .env || echo "PHONEPILOT_SANDBOX=${PHONEPILOT_SANDBOX:-process}" >> .env
grep -q '^PHONEPILOT_MAX_PHONES_PER_USER=' .env || echo "PHONEPILOT_MAX_PHONES_PER_USER=${PHONEPILOT_MAX_PHONES_PER_USER:-2}" >> .env
grep -q '^PHONEPILOT_ALLOWED_ORIGINS=' .env || echo "PHONEPILOT_ALLOWED_ORIGINS=${PHONEPILOT_ALLOWED_ORIGINS:-}" >> .env
chmod 600 .env
mkdir -p data logs

say "launchd: $LABEL"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents"
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
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$BIN:$HOME/.local/platform-tools:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/logs/serve.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/logs/serve.err.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

say "waiting for the service"
for _ in $(seq 1 30); do curl -fs "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "http://127.0.0.1:$PORT/healthz" && echo
echo "PhonePilot backend is running on http://127.0.0.1:$PORT  (logs: $APP_DIR/logs/)"
echo "next: bash $APP_DIR/deploy/mac/expose.sh   # public https URL"

#!/usr/bin/env bash
# Give the local backend a public https URL.
#   1. Tailscale Funnel (stable URL https://<machine>.<tailnet>.ts.net) if the Tailscale app is installed
#      and Funnel is allowed for this node.
#   2. Otherwise a Cloudflare quick tunnel (random https://*.trycloudflare.com URL, changes on restart),
#      registered as a launchd agent.
set -euo pipefail
PORT="${PHONEPILOT_PORT:-8080}"
APP_DIR="${PHONEPILOT_DIR:-$HOME/phonepilot}"
BIN="$HOME/.local/bin"
TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
mkdir -p "$APP_DIR/logs" "$BIN"

if [ -x "$TS" ]; then
  echo "== trying Tailscale Funnel"
  if "$TS" funnel --bg "$PORT" 2>"$APP_DIR/logs/funnel.err" >"$APP_DIR/logs/funnel.out"; then
    "$TS" funnel status 2>/dev/null || true
    URL=$("$TS" funnel status 2>/dev/null | grep -o 'https://[^ ]*' | head -1 || true)
    if [ -n "$URL" ]; then
      echo "PUBLIC_URL=$URL"
      echo "$URL" > "$APP_DIR/data/public_url"
      exit 0
    fi
  fi
  echo "Funnel not available: $(cat "$APP_DIR/logs/funnel.err" | tail -2)"
fi

echo "== Cloudflare quick tunnel"
if ! command -v cloudflared >/dev/null 2>&1 && [ ! -x "$BIN/cloudflared" ]; then
  ARCH=$(uname -m); [ "$ARCH" = "arm64" ] && CF=cloudflared-darwin-arm64.tgz || CF=cloudflared-darwin-amd64.tgz
  curl -fsSL -o /tmp/cf.tgz "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF"
  tar -xzf /tmp/cf.tgz -C "$BIN" cloudflared && chmod +x "$BIN/cloudflared"
fi
CFBIN=$(command -v cloudflared || echo "$BIN/cloudflared")
LABEL="com.phonepilot.tunnel"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$CFBIN</string><string>tunnel</string><string>--no-autoupdate</string><string>--url</string><string>http://127.0.0.1:$PORT</string>
  </array>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$APP_DIR/logs/tunnel.log</string>
  <key>StandardErrorPath</key><string>$APP_DIR/logs/tunnel.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
: > "$APP_DIR/logs/tunnel.log"
launchctl bootstrap "gui/$(id -u)" "$PLIST"
for _ in $(seq 1 60); do
  URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$APP_DIR/logs/tunnel.log" 2>/dev/null | tail -1 || true)
  [ -n "$URL" ] && break; sleep 1
done
echo "PUBLIC_URL=${URL:-unknown}"
[ -n "$URL" ] && echo "$URL" > "$APP_DIR/data/public_url"

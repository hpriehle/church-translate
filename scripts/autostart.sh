#!/usr/bin/env bash
# Run the translation server in the background of your Mac login (a launchd LaunchAgent):
# it starts when you log in, restarts if it crashes, and keeps access to the keychain that
# Claude Code needs. Use the operator page's "Restart server" button to restart it.
#
# If ~/.cloudflared/church-translate.yml exists, the public-link tunnel runs on this Mac too
# (a second agent), so the public link needs nothing but this Mac and an internet connection.
#
#   scripts/autostart.sh install   install and start the server (and the tunnel, if configured)
#   scripts/autostart.sh tunnel    install/start only the tunnel (leaves the server running)
#   scripts/autostart.sh remove    stop and uninstall both (then use ./start.sh by hand again)
#   scripts/autostart.sh restart   restart the server from Terminal
#   scripts/autostart.sh status
set -euo pipefail
LABEL=com.clntacq.church-translate
TUNNEL_LABEL=$LABEL.tunnel
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TUNNEL_PLIST="$HOME/Library/LaunchAgents/$TUNNEL_LABEL.plist"
TUNNEL_CONFIG="${TUNNEL_CONFIG:-$HOME/.cloudflared/church-translate.yml}"
DOMAIN="gui/$(id -u)"

[[ "$(uname)" == "Darwin" ]] || { echo "macOS only (on Linux use a systemd user unit)"; exit 1; }

load() {  # load <label> <plist>
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  # bootout returns before the old job is gone; bootstrapping too early fails with "5: Input/output error".
  for _ in $(seq 1 20); do launchctl print "$DOMAIN/$1" >/dev/null 2>&1 || break; sleep 0.5; done
  local ok=""
  for _ in 1 2 3 4 5; do
    if launchctl bootstrap "$DOMAIN" "$2" 2>/dev/null; then ok=1; break; fi
    sleep 2
  done
  [[ -n "$ok" ]] || { echo "Could not start $1 — try: launchctl bootstrap $DOMAIN $2"; return 1; }
  launchctl enable "$DOMAIN/$1"
}

install_server() {
  mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
  cat > "$PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/start.sh</string><string>--no-browser</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- Real-time audio: don't let macOS treat it as a low-priority background job. -->
  <key>ProcessType</key><string>Interactive</string>
  <!-- Leave Ollama (started by start.sh) running if the server process exits. -->
  <key>AbandonProcessGroup</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.log</string>
</dict>
</plist>
PLIST_END
  load "$LABEL" "$PLIST"
  echo "Installed: the server now runs at login. Log: $DIR/logs/launchd.log"
}

install_tunnel() {
  local cf
  cf="$(command -v cloudflared || true)"
  [[ -n "$cf" ]] || cf=/opt/homebrew/bin/cloudflared
  if [[ ! -f "$TUNNEL_CONFIG" ]]; then
    echo "No tunnel config at $TUNNEL_CONFIG — the server works on the local network only."
    return 0
  fi
  [[ -x "$cf" ]] || { echo "cloudflared is missing: brew install cloudflared"; return 1; }
  mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
  cat > "$TUNNEL_PLIST" <<PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$TUNNEL_LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$cf</string><string>tunnel</string><string>--config</string><string>$TUNNEL_CONFIG</string><string>--metrics</string><string>127.0.0.1:20241</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/logs/tunnel.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/tunnel.log</string>
</dict>
</plist>
PLIST_END
  load "$TUNNEL_LABEL" "$TUNNEL_PLIST"
  echo "Installed: the public-link tunnel runs on this Mac. Log: $DIR/logs/tunnel.log"
}

case "${1:-status}" in
  install) install_server; install_tunnel ;;
  tunnel)  install_tunnel ;;
  remove)
    for l in "$TUNNEL_LABEL" "$LABEL"; do launchctl bootout "$DOMAIN/$l" 2>/dev/null || true; done
    rm -f "$PLIST" "$TUNNEL_PLIST"
    echo "Removed. Start the server with ./start.sh when you need it."
    ;;
  restart) launchctl kickstart -k "$DOMAIN/$LABEL" ;;
  status)
    for l in "$LABEL" "$TUNNEL_LABEL"; do
      if launchctl print "$DOMAIN/$l" >/dev/null 2>&1; then
        echo "$l:"
        launchctl print "$DOMAIN/$l" | grep -E "^\s*(state|pid|last exit code) =" | head -3 || true
      else
        echo "$l: not installed"
      fi
    done
    ;;
  *) echo "usage: $0 install|tunnel|remove|restart|status"; exit 1 ;;
esac

#!/usr/bin/env bash
# Run the translation server in the background of your Mac login (a launchd LaunchAgent):
# it starts when you log in, restarts if it crashes, and keeps access to the keychain that
# Claude Code needs. Use the operator page's "Restart server" button to restart it.
#
#   scripts/autostart.sh install   install and start now
#   scripts/autostart.sh remove    stop and uninstall (then use ./start.sh by hand again)
#   scripts/autostart.sh restart   restart from Terminal
#   scripts/autostart.sh status
set -euo pipefail
LABEL=com.clntacq.church-translate
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

[[ "$(uname)" == "Darwin" ]] || { echo "macOS only (on Linux use a systemd user unit)"; exit 1; }

case "${1:-status}" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$DIR/start.sh</string><string>--no-browser</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string></dict>
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
EOF
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl enable "$DOMAIN/$LABEL"
    echo "Installed: the server now runs at login. Log: $DIR/logs/launchd.log"
    ;;
  remove)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed. Start the server with ./start.sh when you need it."
    ;;
  restart)
    launchctl kickstart -k "$DOMAIN/$LABEL"
    ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "^\s*(state|pid|last exit code) =" || true
    else
      echo "not installed"
    fi
    ;;
  *) echo "usage: $0 install|remove|restart|status"; exit 1 ;;
esac

#!/bin/bash
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS_DIR="${LEDGER_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LOG_DIR="${LEDGER_LOG_DIR:-$REPO_DIR/data/logs}"
UVICORN="${LEDGER_UVICORN:-$REPO_DIR/.venv/bin/uvicorn}"
PYTHON="${LEDGER_PYTHON:-$REPO_DIR/.venv/bin/python}"
SERVER_PLIST="$AGENTS_DIR/com.ledger.server.plist"
SNAPSHOT_PLIST="$AGENTS_DIR/com.ledger.snapshot.plist"

mkdir -p "$AGENTS_DIR" "$LOG_DIR"

cat > "$SERVER_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ledger.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UVICORN</string>
    <string>app.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/server.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/server.err.log</string>
</dict>
</plist>
EOF

cat > "$SNAPSHOT_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.ledger.snapshot</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>app.snapshot</string>
    <string>--catch-up</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Minute</key><integer>5</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Minute</key><integer>5</integer></dict>
  </array>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/snapshot.out.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/snapshot.err.log</string>
</dict>
</plist>
EOF

if [ "${LEDGER_SKIP_BOOTSTRAP:-0}" != "1" ]; then
  launchctl bootstrap "gui/$(id -u)" "$SERVER_PLIST"
  launchctl bootstrap "gui/$(id -u)" "$SNAPSHOT_PLIST"
fi

printf 'Installed %s and %s\n' "$SERVER_PLIST" "$SNAPSHOT_PLIST"

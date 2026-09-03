#!/bin/bash
set -eu

AGENTS_DIR="${LEDGER_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
SERVER_PLIST="$AGENTS_DIR/com.ledger.server.plist"
SNAPSHOT_PLIST="$AGENTS_DIR/com.ledger.snapshot.plist"

if [ "${LEDGER_SKIP_BOOTSTRAP:-0}" != "1" ] && command -v launchctl >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$SERVER_PLIST" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)" "$SNAPSHOT_PLIST" 2>/dev/null || true
fi

rm -f "$SERVER_PLIST" "$SNAPSHOT_PLIST"
printf 'Removed Ledger launchd agents\n'

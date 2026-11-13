#!/usr/bin/env bash
# Start nethogs monitoring for the per-process network plasmoid
# Usage: ./start-monitoring.sh [interface]
# Requires nethogs with the file capabilities listed below (see README.md);
# it runs as your own user, so nothing here needs root.

set -euo pipefail

INTERFACE="${1:-}"
DATA_FILE="/tmp/nethogs-raw.txt"
CAPS="cap_net_admin,cap_net_raw,cap_dac_read_search,cap_sys_ptrace"

if ! command -v nethogs &> /dev/null; then
    echo "Error: nethogs is not installed. Install it with:"
    echo "  sudo pacman -S nethogs"
    exit 1
fi

if ! getcap "$(command -v nethogs)" 2>/dev/null | grep -q "cap_net_admin"; then
    echo "Error: nethogs is missing its file capabilities, so it cannot capture packets."
    echo "Grant them once (a nethogs upgrade drops them again):"
    echo "  sudo setcap ${CAPS}=ep $(command -v nethogs)"
    exit 1
fi

echo "Starting nethogs monitoring on ${INTERFACE:-default interface}..."
echo "Output: $DATA_FILE"
echo "Stop with: Ctrl+C or kill PID $$"

if [ -n "$INTERFACE" ]; then
    nethogs -t -d 1 -c 0 "$INTERFACE" > "$DATA_FILE" 2>/dev/null &
else
    nethogs -t -d 1 -c 0 > "$DATA_FILE" 2>/dev/null &
fi
child=$!
trap 'kill "$child" 2>/dev/null || true' INT TERM HUP
wait "$child" || true

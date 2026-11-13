#!/usr/bin/env bash
# Install the per-process network monitor plasmoid
# Usage: ./install.sh

set -euo pipefail

PLASMOID_ID="network-perprocess-monitor"
SOURCE_DIR="package"
TARGET_DIR="${HOME}/.local/share/plasma/plasmoids/${PLASMOID_ID}"

echo "Installing ${PLASMOID_ID}..."

# Remove old installation if present
if [ -d "$TARGET_DIR" ]; then
    echo "Removing previous installation..."
    rm -rf "$TARGET_DIR"
fi

# Copy package directory
mkdir -p "$(dirname "$TARGET_DIR")"
cp -r "$SOURCE_DIR" "$TARGET_DIR"
echo "Copied to $TARGET_DIR"

echo ""
echo "Installation complete!"
echo "Add the widget: Right-click panel -> Add Widgets -> Per-Process Network Monitor"
echo "Or test with: plasmawindowed ${PLASMOID_ID}"
echo ""
echo "To start monitoring, run in a terminal:"
echo "  ./start-monitoring.sh [interface]"
echo ""
echo "To restart Plasma shell (if widget doesn't appear):"
echo "  kquitapp6 plasmashell && kstart6 plasmashell"

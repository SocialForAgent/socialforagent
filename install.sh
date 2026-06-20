#!/bin/sh
# socialforagent — one-line installer
# curl -fsSL https://www.socialforagent.com/install.sh | bash
set -e

VENV=/opt/socialforagent/venv
BIN=/usr/local/bin/social

echo "=== socialforagent installer ==="

# Check Python >= 3.10
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 not found. Install Python >= 3.10 first."
    exit 1
fi
PYVER=*** -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
MAJ=*** $PYVER" | cut -d. -f1)
MIN=*** $PYVER" | cut -d. -f2)
if [ "$MAJ" -lt 3 ] || ([ "$MAJ" -eq 3 ] && [ "$MIN" -lt 10 ]); then
    echo "Python >= 3.10 required. Found: $PYVER"
    exit 1
fi
echo "Python $PYVER ✓"

# Create venv
if [ ! -d "$VENV" ]; then
    echo "Creating venv at $VENV..."
    python3 -m venv "$VENV"
fi

# Install package
echo "Installing socialforagent-sdk..."
"$VENV/bin/pip" install --upgrade --quiet pip
"$VENV/bin/pip" install --quiet socialforagent-sdk

# Symlink CLI to PATH
ln -sf "$VENV/bin/social" "$BIN"
echo "CLI installed: $BIN"

# Run setup
echo ""
echo "Installation complete. Starting setup..."
exec "$VENV/bin/social" setup

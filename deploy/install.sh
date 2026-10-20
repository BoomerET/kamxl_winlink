#!/bin/sh
# Install the tncstation systemd units for this clone and its user.
#
#   sudo deploy/install.sh            install (or update) both units
#   sudo deploy/install.sh --dry-run  just print what would be installed
#
# Fills in the user who ran sudo, this folder, and the Python to use
# (.venv/bin/python here if it exists, else python3), then reloads
# systemd. It doesn't start anything -- it prints the commands for that.
# Running it again after moving the folder or changing the venv updates
# the units in place.

set -eu

DIR=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=${SUDO_USER:-$(id -un)}
UNIT_DIR=${TNCSTATION_UNIT_DIR:-/etc/systemd/system}   # override for testing
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

if [ -x "$DIR/.venv/bin/python" ]; then
    PYTHON="$DIR/.venv/bin/python"
else
    PYTHON=$(command -v python3)
fi

if [ "$USER_NAME" = "root" ]; then
    echo "Run this with sudo from your own account, not as root:" >&2
    echo "  sudo $0" >&2
    exit 1
fi

for unit in tncstation-daemon.service tncstation-web.service; do
    text=$(sed -e "s|@USER@|$USER_NAME|g" -e "s|@DIR@|$DIR|g" \
               -e "s|@PYTHON@|$PYTHON|g" "$DIR/deploy/$unit")
    if [ "$DRY_RUN" = 1 ]; then
        echo "### $UNIT_DIR/$unit"
        echo "$text"
        echo
    else
        printf '%s\n' "$text" > "$UNIT_DIR/$unit"
        echo "Installed $UNIT_DIR/$unit"
    fi
done

[ "$DRY_RUN" = 1 ] && exit 0

systemctl daemon-reload

if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx dialout; then
    echo
    echo "Note: $USER_NAME isn't in the dialout group, so running by hand"
    echo "won't reach the serial port (the service adds it itself):"
    echo "  sudo usermod -aG dialout $USER_NAME   (then log out and back in)"
fi

[ -f "$DIR/.env" ] || echo "
No .env yet: cp .env.sample .env and edit it before starting."

cat <<EOF

User: $USER_NAME   Folder: $DIR   Python: $PYTHON

Start now and at every boot:
  sudo systemctl enable --now tncstation-daemon tncstation-web
Check:
  systemctl status tncstation-daemon tncstation-web
  journalctl -u tncstation-daemon -f
EOF

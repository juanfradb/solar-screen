#!/usr/bin/env bash
# Install the GDM greeter half of solar-screen.
#
# The greeter runs as the gdm user, whose home is not readable by the regular
# user, so the controller is deployed to /usr/local/lib. GDM launches its
# greeter with `gnome-session --autostart /usr/share/gdm/greeter/autostart`,
# which gives us the greeter's private D-Bus address (it lives in /tmp, not in
# /run/user/120) without having to discover it from outside.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR=/usr/local/lib/solar-screen
CONFIG_DIR=/etc/solar-screen
CONFIG_FILE="$CONFIG_DIR/config.json"
AUTOSTART_DIR=/usr/share/gdm/greeter/autostart
AUTOSTART_FILE="$AUTOSTART_DIR/solar-screen-greeter.desktop"
OLD_UNIT=/etc/systemd/system/solar-screen-greeter.service

if [[ ${EUID} -eq 0 ]]; then
    SUDO=()
else
    SUDO=(sudo -A)
fi

if ! /usr/bin/python3 -c 'import gi' 2>/dev/null; then
    echo "python3-gi is required for the greeter (sudo apt install python3-gi)" >&2
    exit 1
fi

# Remove the system service used by earlier revisions: the greeter's session
# bus is not reachable from outside the session.
if [[ -e "$OLD_UNIT" ]]; then
    "${SUDO[@]}" systemctl disable --now solar-screen-greeter.service || true
    "${SUDO[@]}" rm -f "$OLD_UNIT"
    "${SUDO[@]}" systemctl daemon-reload
fi

"${SUDO[@]}" install -d -m 0755 "$DEST_DIR" "$CONFIG_DIR" "$AUTOSTART_DIR"
"${SUDO[@]}" install -m 0755 "$APP_DIR/solar_screen.py" "$DEST_DIR/solar_screen.py"
"${SUDO[@]}" install -m 0644 "$APP_DIR/solar_table.py" "$DEST_DIR/solar_table.py"
"${SUDO[@]}" install -m 0755 "$APP_DIR/solar-screen-greeter.sh" "$DEST_DIR/solar-screen-greeter"
"${SUDO[@]}" install -m 0644 "$APP_DIR/solar-screen-greeter.desktop" "$AUTOSTART_FILE"

if [[ ! -e "$CONFIG_FILE" ]]; then
    source_config="${XDG_CONFIG_HOME:-$HOME/.config}/solar-screen/config.json"
    if [[ ! -f "$source_config" ]]; then
        echo "missing $source_config; run 'solar-screen enable' first" >&2
        exit 1
    fi
    temporary="$(mktemp)"
    trap 'rm -f "$temporary"' EXIT
    python3 - "$source_config" "$temporary" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    config = json.load(stream)
# The greeter only lives for a few seconds and mutter's color manager may
# overwrite the LUT right after it starts; poll fast enough to win the race.
config["update_seconds"] = min(float(config.get("update_seconds", 1.0)), 1.0)
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    json.dump(config, stream, indent=2)
    stream.write("\n")
PY
    "${SUDO[@]}" install -m 0644 "$temporary" "$CONFIG_FILE"
fi

echo "greeter filter installed."
echo "It will start with the next GDM greeter (logout or reboot)."
echo "Check afterwards: ps -u gdm -o args= | grep solar-screen"

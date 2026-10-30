#!/bin/sh
# Launched by GDM's greeter session; inherits its private D-Bus address.
set -eu
SOLAR_SCREEN_BACKEND=mutter
SOLAR_SCREEN_CONFIG=/etc/solar-screen/config.json
SOLAR_SCREEN_MANAGE_GNOME=0
SOLAR_SCREEN_BASELINE=identity
export SOLAR_SCREEN_BACKEND SOLAR_SCREEN_CONFIG SOLAR_SCREEN_MANAGE_GNOME SOLAR_SCREEN_BASELINE
exec /usr/bin/python3 /usr/local/lib/solar-screen/solar_screen.py daemon --wait

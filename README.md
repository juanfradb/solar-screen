# Solar Screen

[![Tests](https://github.com/juanfradb/solar-screen/actions/workflows/test.yml/badge.svg)](https://github.com/juanfradb/solar-screen/actions/workflows/test.yml)

Solar-position display white-point controller for GNOME on X11.

Solar Screen estimates clear-sky direct sunlight at the configured location,
maps its chromaticity onto an sRGB display, and applies the result through an
atomic XRandR gamma ramp. It does not change hardware brightness.

At night, the controller transitions from the modeled horizon white point to
red-only output. The default red-only threshold is -6° solar elevation.

## Requirements

- Linux with an X11 session
- GNOME and systemd user services
- Python 3.10+
- `libX11`, `libXrandr`, `gsettings`, and `gdbus`

Wayland is not currently supported.

## Install

```bash
git clone https://github.com/juanfradb/solar-screen.git
cd solar-screen
mkdir -p ~/.local/bin ~/.config/solar-screen
ln -sfn "$PWD/solar_screen.py" ~/.local/bin/solar-screen
cp config.example.json ~/.config/solar-screen/config.json
chmod +x solar_screen.py
```

Edit `~/.config/solar-screen/config.json` with latitude, longitude, and
altitude, then enable the service:

```bash
solar-screen enable
```

## Control

```bash
solar-screen status
solar-screen disable
solar-screen enable
```

`enable` saves and disables GNOME Night Light before taking ownership of the
display LUT. `disable` restores the exact baseline LUT and the previous GNOME
Night Light setting.

## Model

- NOAA apparent solar elevation, calculated in UTC every five seconds.
- Bird SPECTRL2 clear-sky direct-normal spectral irradiance.
- CIE 1931 2° color matching functions and sRGB conversion.
- Exact sRGB EOTF/OETF composition over the existing XRandR baseline.
- Smooth twilight transition to red-only output.

The runtime has no Python package dependencies and works offline. The bundled
lookup table was generated with `pvlib` and `colour-science`; its reproducible
generator is in `tools/generate_solar_table.py`.

A three-primary LCD cannot reproduce a full spectral power distribution. This
project reproduces the closest display chromaticity available from its fixed
RGB primaries. Cloud percentage is intentionally excluded because it does not
determine spectral composition.

## Verify

```bash
python3 -m unittest -v
systemd-analyze --user verify solar-screen.service
```

## License

MIT

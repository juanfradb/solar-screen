#!/usr/bin/env python3
"""Solar-position display white point for GNOME on X11."""

from __future__ import annotations

import argparse
import array
import base64
import ctypes
import ctypes.util
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from solar_table import SOLAR_RGB_TABLE


APP_DIR = Path(__file__).resolve().parent
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
CONFIG_PATH = Path(
    os.environ.get("SOLAR_SCREEN_CONFIG", CONFIG_HOME / "solar-screen/config.json")
)
RUNTIME_DIR = (
    Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "solar-screen"
)
# System services (the GDM greeter) get no session environment, only a uid.
if "XDG_RUNTIME_DIR" not in os.environ:
    os.environ["XDG_RUNTIME_DIR"] = str(RUNTIME_DIR.parent)
if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
    _session_bus = RUNTIME_DIR.parent / "bus"
    if _session_bus.exists():
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={_session_bus}"
RUNTIME_STATE = RUNTIME_DIR / "state.json"
LOCK_PATH = RUNTIME_DIR / "daemon.lock"
PERSISTENT_DIR = Path.home() / ".local/state/solar-screen"
GNOME_BACKUP = PERSISTENT_DIR / "gnome-night-light.json"
UNIT_PATH = Path.home() / ".config/systemd/user/solar-screen.service"
GNOME_SCHEMA = "org.gnome.settings-daemon.plugins.color"
GNOME_KEY = "night-light-enabled"

DEFAULT_CONFIG = {
    "red_only_below_elevation": -6.0,
    "update_seconds": 5.0,
}


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise ValueError(
            f"missing configuration: copy config.example.json to {CONFIG_PATH}"
        )
    config = DEFAULT_CONFIG | read_json(CONFIG_PATH)
    missing = {"latitude", "longitude", "altitude_m"} - config.keys()
    if missing:
        raise ValueError(f"missing configuration keys: {', '.join(sorted(missing))}")
    latitude = float(config["latitude"])
    longitude = float(config["longitude"])
    red_below = float(config["red_only_below_elevation"])
    interval = float(config["update_seconds"])
    altitude = float(config["altitude_m"])
    if not all(
        math.isfinite(value)
        for value in (latitude, longitude, altitude, red_below, interval)
    ):
        raise ValueError("all numeric configuration values must be finite")
    if not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180")
    if not -18 <= red_below < 0:
        raise ValueError("red_only_below_elevation must be between -18 and 0")
    if interval <= 0:
        raise ValueError("update_seconds must be positive")
    config.update(
        latitude=latitude,
        longitude=longitude,
        altitude_m=altitude,
        red_only_below_elevation=red_below,
        update_seconds=interval,
    )
    return config


def solar_elevation(
    instant: datetime, latitude: float, longitude: float, altitude_m: float = 0.0
) -> float:
    """NOAA apparent solar elevation. Longitude is east-positive."""
    if instant.tzinfo is None:
        raise ValueError("instant must include a timezone")
    current = instant.astimezone(timezone.utc)
    julian_day = current.timestamp() / 86400.0 + 2440587.5
    century = (julian_day - 2451545.0) / 36525.0

    mean_longitude = (280.46646 + century * (36000.76983 + century * 0.0003032)) % 360.0
    mean_anomaly = 357.52911 + century * (35999.05029 - 0.0001537 * century)
    eccentricity = 0.016708634 - century * (0.000042037 + 0.0000001267 * century)
    anomaly_rad = math.radians(mean_anomaly)
    equation_of_center = (
        math.sin(anomaly_rad) * (1.914602 - century * (0.004817 + 0.000014 * century))
        + math.sin(2 * anomaly_rad) * (0.019993 - 0.000101 * century)
        + math.sin(3 * anomaly_rad) * 0.000289
    )
    true_longitude = mean_longitude + equation_of_center
    omega = 125.04 - 1934.136 * century
    apparent_longitude = (
        true_longitude - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    )
    obliquity_seconds = 21.448 - century * (
        46.815 + century * (0.00059 - century * 0.001813)
    )
    mean_obliquity = 23.0 + (26.0 + obliquity_seconds / 60.0) / 60.0
    corrected_obliquity = mean_obliquity + 0.00256 * math.cos(math.radians(omega))
    obliquity_rad = math.radians(corrected_obliquity)
    declination = math.asin(
        math.sin(obliquity_rad) * math.sin(math.radians(apparent_longitude))
    )

    y = math.tan(obliquity_rad / 2.0) ** 2
    longitude_rad = math.radians(mean_longitude)
    equation_of_time = 4.0 * math.degrees(
        y * math.sin(2.0 * longitude_rad)
        - 2.0 * eccentricity * math.sin(anomaly_rad)
        + 4.0 * eccentricity * y * math.sin(anomaly_rad) * math.cos(2.0 * longitude_rad)
        - 0.5 * y * y * math.sin(4.0 * longitude_rad)
        - 1.25 * eccentricity * eccentricity * math.sin(2.0 * anomaly_rad)
    )
    utc_minutes = (
        current.hour * 60.0
        + current.minute
        + current.second / 60.0
        + current.microsecond / 60_000_000.0
    )
    true_solar_minutes = (utc_minutes + equation_of_time + 4.0 * longitude) % 1440
    hour_angle = math.radians(true_solar_minutes / 4.0 - 180.0)
    latitude_rad = math.radians(latitude)
    cosine_zenith = math.sin(latitude_rad) * math.sin(declination) + math.cos(
        latitude_rad
    ) * math.cos(declination) * math.cos(hour_angle)
    geometric_elevation = 90.0 - math.degrees(
        math.acos(max(-1.0, min(1.0, cosine_zenith)))
    )

    if geometric_elevation > 85.0:
        refraction = 0.0
    elif geometric_elevation > 5.0:
        tangent = math.tan(math.radians(geometric_elevation))
        refraction = (
            58.1 / tangent - 0.07 / tangent**3 + 0.000086 / tangent**5
        ) / 3600.0
    elif geometric_elevation > -0.575:
        e = geometric_elevation
        refraction = (
            1735.0 + e * (-518.2 + e * (103.4 + e * (-12.79 + e * 0.711)))
        ) / 3600.0
    else:
        tangent = math.tan(math.radians(geometric_elevation))
        refraction = 0.0 if abs(tangent) < 1e-12 else -20.772 / tangent / 3600.0
    pressure_ratio = max(0.0, 1.0 - 2.25577e-5 * altitude_m) ** 5.25588
    return geometric_elevation + refraction * pressure_ratio


@dataclass(frozen=True)
class Target:
    elevation: float
    red: float
    green: float
    blue: float
    mode: str

    @property
    def rgb(self) -> tuple[float, float, float]:
        return self.red, self.green, self.blue


def target_for_elevation(elevation: float, red_below: float = -6.0) -> Target:
    if elevation <= red_below:
        return Target(elevation, 1.0, 0.0, 0.0, "red-only")
    if elevation < 0.0:
        position = (elevation - red_below) / -red_below
        smooth = position * position * (3.0 - 2.0 * position)
        horizon_green, horizon_blue = SOLAR_RGB_TABLE[0]
        return Target(
            elevation,
            1.0,
            horizon_green * smooth,
            horizon_blue * smooth,
            "twilight",
        )

    table_position = min(elevation, 90.0) * 2.0
    lower = min(int(table_position), len(SOLAR_RGB_TABLE) - 1)
    upper = min(lower + 1, len(SOLAR_RGB_TABLE) - 1)
    fraction = table_position - lower
    green = (
        SOLAR_RGB_TABLE[lower][0] * (1.0 - fraction)
        + SOLAR_RGB_TABLE[upper][0] * fraction
    )
    blue = (
        SOLAR_RGB_TABLE[lower][1] * (1.0 - fraction)
        + SOLAR_RGB_TABLE[upper][1] * fraction
    )
    return Target(elevation, 1.0, green, blue, "direct-sun")


class XRRScreenResources(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_ulong),
        ("config_timestamp", ctypes.c_ulong),
        ("ncrtc", ctypes.c_int),
        ("crtcs", ctypes.POINTER(ctypes.c_ulong)),
        ("noutput", ctypes.c_int),
        ("outputs", ctypes.POINTER(ctypes.c_ulong)),
        ("nmode", ctypes.c_int),
        ("modes", ctypes.c_void_p),
    ]


class XRROutputInfo(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_ulong),
        ("crtc", ctypes.c_ulong),
        ("name", ctypes.c_char_p),
        ("name_len", ctypes.c_int),
        ("mm_width", ctypes.c_ulong),
        ("mm_height", ctypes.c_ulong),
        ("connection", ctypes.c_int),
        ("subpixel_order", ctypes.c_int),
        ("ncrtc", ctypes.c_int),
        ("crtcs", ctypes.POINTER(ctypes.c_ulong)),
        ("nclone", ctypes.c_int),
        ("clones", ctypes.POINTER(ctypes.c_ulong)),
        ("nmode", ctypes.c_int),
        ("npreferred", ctypes.c_int),
        ("modes", ctypes.POINTER(ctypes.c_ulong)),
    ]


class XRRCrtcGamma(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_int),
        ("red", ctypes.POINTER(ctypes.c_ushort)),
        ("green", ctypes.POINTER(ctypes.c_ushort)),
        ("blue", ctypes.POINTER(ctypes.c_ushort)),
    ]


@dataclass(frozen=True)
class OutputInfo:
    name: str
    crtc: int
    size: int


Ramp = tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]


class XRandR:
    def __init__(self) -> None:
        x11_name = ctypes.util.find_library("X11")
        randr_name = ctypes.util.find_library("Xrandr")
        if not x11_name or not randr_name:
            raise RuntimeError("libX11/libXrandr not found")
        self.x11 = ctypes.CDLL(x11_name)
        self.randr = ctypes.CDLL(randr_name)
        self._declare_api()
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError(f"cannot open X display {os.environ.get('DISPLAY', '')}")
        self._x_error = False
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._error_handler = handler_type(self._handle_x_error)
        self.x11.XSetErrorHandler(ctypes.cast(self._error_handler, ctypes.c_void_p))
        self.root = self.x11.XDefaultRootWindow(self.display)

    def _handle_x_error(self, _display: int, _event: int) -> int:
        self._x_error = True
        return 0

    def _declare_api(self) -> None:
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.x11.XSync.restype = ctypes.c_int
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.x11.XCloseDisplay.restype = ctypes.c_int
        self.x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        self.x11.XSetErrorHandler.restype = ctypes.c_void_p

        resources = ctypes.POINTER(XRRScreenResources)
        output_info = ctypes.POINTER(XRROutputInfo)
        gamma = ctypes.POINTER(XRRCrtcGamma)
        self.randr.XRRGetScreenResourcesCurrent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]
        self.randr.XRRGetScreenResourcesCurrent.restype = resources
        self.randr.XRRFreeScreenResources.argtypes = [resources]
        self.randr.XRRFreeScreenResources.restype = None
        self.randr.XRRGetOutputInfo.argtypes = [
            ctypes.c_void_p,
            resources,
            ctypes.c_ulong,
        ]
        self.randr.XRRGetOutputInfo.restype = output_info
        self.randr.XRRFreeOutputInfo.argtypes = [output_info]
        self.randr.XRRFreeOutputInfo.restype = None
        self.randr.XRRGetCrtcGammaSize.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.randr.XRRGetCrtcGammaSize.restype = ctypes.c_int
        self.randr.XRRGetCrtcGamma.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.randr.XRRGetCrtcGamma.restype = gamma
        self.randr.XRRAllocGamma.argtypes = [ctypes.c_int]
        self.randr.XRRAllocGamma.restype = gamma
        self.randr.XRRSetCrtcGamma.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            gamma,
        ]
        self.randr.XRRSetCrtcGamma.restype = None
        self.randr.XRRFreeGamma.argtypes = [gamma]
        self.randr.XRRFreeGamma.restype = None

    def close(self) -> None:
        if getattr(self, "display", None):
            self.x11.XCloseDisplay(self.display)
            self.display = None

    def active_outputs(self) -> dict[str, OutputInfo]:
        resources = self.randr.XRRGetScreenResourcesCurrent(self.display, self.root)
        if not resources:
            raise RuntimeError("XRRGetScreenResourcesCurrent failed")
        result: dict[str, OutputInfo] = {}
        seen_crtcs: set[int] = set()
        try:
            for index in range(resources.contents.noutput):
                output_id = resources.contents.outputs[index]
                pointer = self.randr.XRRGetOutputInfo(
                    self.display, resources, output_id
                )
                if not pointer:
                    continue
                try:
                    info = pointer.contents
                    crtc = int(info.crtc)
                    if info.connection != 0 or not crtc or crtc in seen_crtcs:
                        continue
                    size = self.randr.XRRGetCrtcGammaSize(self.display, crtc)
                    if size < 2:
                        continue
                    name = ctypes.string_at(info.name, info.name_len).decode(
                        "utf-8", "replace"
                    )
                    result[name] = OutputInfo(name, crtc, size)
                    seen_crtcs.add(crtc)
                finally:
                    self.randr.XRRFreeOutputInfo(pointer)
        finally:
            self.randr.XRRFreeScreenResources(resources)
        return result

    def get_ramp(self, output: OutputInfo) -> Ramp:
        pointer = self.randr.XRRGetCrtcGamma(self.display, output.crtc)
        if not pointer:
            raise RuntimeError(f"cannot read gamma ramp for {output.name}")
        try:
            size = pointer.contents.size
            if size != output.size:
                raise RuntimeError(
                    f"gamma size changed for {output.name}: {output.size} -> {size}"
                )
            return tuple(
                tuple(int(channel[index]) for index in range(size))
                for channel in (
                    pointer.contents.red,
                    pointer.contents.green,
                    pointer.contents.blue,
                )
            )  # type: ignore[return-value]
        finally:
            self.randr.XRRFreeGamma(pointer)

    def set_ramp(self, output: OutputInfo, ramp: Ramp) -> None:
        if any(len(channel) != output.size for channel in ramp):
            raise ValueError("gamma ramp has wrong size")
        pointer = self.randr.XRRAllocGamma(output.size)
        if not pointer:
            raise RuntimeError("XRRAllocGamma failed")
        try:
            self._x_error = False
            for source, destination in zip(
                ramp,
                (
                    pointer.contents.red,
                    pointer.contents.green,
                    pointer.contents.blue,
                ),
            ):
                for index, value in enumerate(source):
                    destination[index] = value
            self.randr.XRRSetCrtcGamma(self.display, output.crtc, pointer)
            self.x11.XSync(self.display, 0)
            if self._x_error:
                raise RuntimeError(f"X11 rejected gamma write for {output.name}")
        finally:
            self.randr.XRRFreeGamma(pointer)


class MutterDisplayConfig:
    """Gamma backend through mutter's display-config D-Bus API.

    Works on Wayland, where clients cannot touch the CRTC directly, and is
    also valid on X11. Mutter keeps the ramp per CRTC; the compositor's color
    manager may overwrite it when it applies its own LUT, so the daemon keeps
    polling and re-applying.
    """

    BUS_NAME = "org.gnome.Mutter.DisplayConfig"
    OBJECT_PATH = "/org/gnome/Mutter/DisplayConfig"
    INTERFACE = "org.gnome.Mutter.DisplayConfig"

    def __init__(self) -> None:
        try:
            import gi

            from gi.repository import Gio, GLib
        except ImportError as error:
            raise RuntimeError(
                "PyGObject (python3-gi) is required for the mutter backend"
            ) from error
        self._gio = Gio
        self._glib = GLib
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
            session_bus = RUNTIME_DIR.parent / "bus"
            if session_bus.exists():
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={session_bus}"
        try:
            self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as error:
            raise RuntimeError(
                f"cannot connect to the session bus: {error.message}"
            ) from error
        self.proxy = Gio.DBusProxy.new_sync(
            self.connection,
            Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,
            self.BUS_NAME,
            self.OBJECT_PATH,
            self.INTERFACE,
            None,
        )
        if self.proxy.get_name_owner() is None:
            raise RuntimeError(f"{self.BUS_NAME} is not available")

    def close(self) -> None:
        self.proxy = None
        self.connection = None

    def _call(self, method: str, parameters=None):
        return self.proxy.call_sync(
            method,
            parameters,
            self._gio.DBusCallFlags.NONE,
            5000,
            None,
        )

    def _resources(self) -> tuple[int, list]:
        reply = self._call("GetResources")
        serial, crtcs, _outputs, _modes, _width, _height = reply.unpack()
        return int(serial), crtcs

    def _gamma(self, serial: int, crtc: int) -> Ramp:
        reply = self._call(
            "GetCrtcGamma", self._glib.Variant("(uu)", (serial, crtc))
        )
        red, green, blue = reply.unpack()
        return (
            tuple(int(value) for value in red),
            tuple(int(value) for value in green),
            tuple(int(value) for value in blue),
        )

    @staticmethod
    def _crtc_is_active(crtc: tuple) -> bool:
        return int(crtc[4]) > 0 and int(crtc[5]) > 0

    def active_outputs(self) -> dict[str, OutputInfo]:
        serial, crtcs = self._resources()
        result: dict[str, OutputInfo] = {}
        for crtc in crtcs:
            if not self._crtc_is_active(crtc):
                continue
            crtc_id = int(crtc[0])
            ramp = self._gamma(serial, crtc_id)
            size = len(ramp[0])
            if size < 2:
                continue
            name = f"crtc-{crtc_id}"
            result[name] = OutputInfo(name, crtc_id, size)
        return result

    def get_ramp(self, output: OutputInfo) -> Ramp:
        serial, _crtcs = self._resources()
        ramp = self._gamma(serial, output.crtc)
        if any(len(channel) != output.size for channel in ramp):
            raise RuntimeError(
                f"gamma size changed for {output.name}: "
                f"{output.size} -> {len(ramp[0])}"
            )
        return ramp

    def set_ramp(self, output: OutputInfo, ramp: Ramp) -> None:
        if any(len(channel) != output.size for channel in ramp):
            raise ValueError("gamma ramp has wrong size")
        serial, crtcs = self._resources()
        if not any(
            int(crtc[0]) == output.crtc and self._crtc_is_active(crtc)
            for crtc in crtcs
        ):
            raise RuntimeError(f"{output.name}: CRTC is no longer active")
        self._call(
            "SetCrtcGamma",
            self._glib.Variant(
                "(uuaqaqaq)",
                (serial, output.crtc, list(ramp[0]), list(ramp[1]), list(ramp[2])),
            ),
        )


def create_backend() -> XRandR | MutterDisplayConfig:
    preferred = os.environ.get("SOLAR_SCREEN_BACKEND", "auto")
    if preferred == "wayland":
        preferred = "mutter"
    if preferred not in ("auto", "x11", "mutter"):
        raise RuntimeError(f"unknown SOLAR_SCREEN_BACKEND value: {preferred}")
    # XWayland exposes XRandR but its gamma ramps do not reach the CRTC.
    backends = (
        ("mutter", "x11")
        if os.environ.get("XDG_SESSION_TYPE") == "wayland"
        else ("x11", "mutter")
    )
    errors: list[str] = []
    for backend in backends:
        if preferred not in ("auto", backend):
            continue
        if backend == "x11":
            if not os.environ.get("DISPLAY"):
                errors.append("DISPLAY is not set")
                continue
            try:
                return XRandR()
            except RuntimeError as error:
                errors.append(str(error))
        else:
            try:
                return MutterDisplayConfig()
            except RuntimeError as error:
                errors.append(str(error))
    raise RuntimeError("no display backend available: " + "; ".join(errors))


def identity_ramp(size: int) -> Ramp:
    if size < 2:
        raise ValueError("gamma ramp size must be at least 2")
    return tuple(
        tuple(round(65535.0 * index / (size - 1)) for index in range(size))
        for _ in range(3)
    )  # type: ignore[return-value]


def ramp_bytes(ramp: Ramp) -> bytes:
    values = array.array("H")
    for channel in ramp:
        values.extend(channel)
    return values.tobytes()


def ramp_hash(ramp: Ramp) -> str:
    return hashlib.sha256(ramp_bytes(ramp)).hexdigest()


def encode_ramp(ramp: Ramp) -> str:
    return base64.b64encode(ramp_bytes(ramp)).decode("ascii")


def decode_ramp(encoded: str, size: int) -> Ramp:
    values = array.array("H")
    values.frombytes(base64.b64decode(encoded, validate=True))
    if len(values) != size * 3:
        raise ValueError("saved gamma ramp has wrong size")
    return (
        tuple(values[0:size]),
        tuple(values[size : size * 2]),
        tuple(values[size * 2 : size * 3]),
    )


def srgb_decode(value: float) -> float:
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def srgb_encode(value: float) -> float:
    return 12.92 * value if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055


def compose_ramp(baseline: Ramp, gains: tuple[float, float, float]) -> Ramp:
    result = []
    for channel, encoded_gain in zip(baseline, gains):
        if encoded_gain == 1.0:
            result.append(channel)
            continue
        if encoded_gain == 0.0:
            result.append((0,) * len(channel))
            continue
        linear_gain = srgb_decode(encoded_gain)
        result.append(
            tuple(
                round(65535.0 * srgb_encode(srgb_decode(value / 65535.0) * linear_gain))
                for value in channel
            )
        )
    return tuple(result)  # type: ignore[return-value]


@dataclass
class OutputState:
    info: OutputInfo
    baseline: Ramp
    last_hash: str
    owned_hashes: set[str]

    @property
    def baseline_hash(self) -> str:
        return ramp_hash(self.baseline)

    def serialized(self) -> dict:
        owned_hashes = self.owned_hashes | {self.baseline_hash, self.last_hash}
        return {
            "crtc": self.info.crtc,
            "size": self.info.size,
            "baseline": encode_ramp(self.baseline),
            "baseline_hash": self.baseline_hash,
            "last_hash": self.last_hash,
            "owned_hashes": sorted(owned_hashes),
        }


def output_state_from_current(
    info: OutputInfo, current: Ramp, previous: dict | None
) -> OutputState:
    current_hash = ramp_hash(current)
    if previous:
        try:
            if (
                int(previous["crtc"]) == info.crtc
                and int(previous["size"]) == info.size
            ):
                baseline = decode_ramp(previous["baseline"], info.size)
                owned_hashes = {
                    str(value) for value in previous.get("owned_hashes", [])
                }
                owned_hashes.update(
                    {str(previous.get("last_hash", "")), ramp_hash(baseline)}
                )
                if current_hash in owned_hashes:
                    return OutputState(info, baseline, current_hash, owned_hashes)
        except (KeyError, TypeError, ValueError, base64.binascii.Error):
            pass
    return OutputState(info, current, current_hash, {current_hash})


def gsettings_get_bool(schema: str, key: str) -> bool:
    result = subprocess.run(
        ["gsettings", "get", schema, key],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() == "true"


def gsettings_set_bool(schema: str, key: str, value: bool) -> None:
    subprocess.run(
        ["gsettings", "set", schema, key, "true" if value else "false"],
        check=True,
    )


def night_light_active() -> bool:
    result = subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.SettingsDaemon.Color",
            "--object-path",
            "/org/gnome/SettingsDaemon/Color",
            "--method",
            "org.freedesktop.DBus.Properties.Get",
            "org.gnome.SettingsDaemon.Color",
            "NightLightActive",
        ],
        text=True,
        capture_output=True,
    )
    return result.returncode == 0 and "true" in result.stdout.lower()


def night_light_temperature() -> int | None:
    result = subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.gnome.SettingsDaemon.Color",
            "--object-path",
            "/org/gnome/SettingsDaemon/Color",
            "--method",
            "org.freedesktop.DBus.Properties.Get",
            "org.gnome.SettingsDaemon.Color",
            "Temperature",
        ],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    for token in result.stdout.replace(">", " ").replace(",", " ").split():
        if token.isdigit():
            return int(token)
    return None


def disable_gnome_night_light(wait: bool = True) -> bool:
    changed = gsettings_get_bool(GNOME_SCHEMA, GNOME_KEY)
    if changed:
        gsettings_set_bool(GNOME_SCHEMA, GNOME_KEY, False)
    if wait:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            temperature = night_light_temperature()
            if not night_light_active() and (
                temperature is None or temperature >= 6500
            ):
                break
            time.sleep(0.1)
        time.sleep(0.2)
    return changed


def wait_for_stable_ramps(
    backend: XRandR | MutterDisplayConfig, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    stable_since = time.monotonic()
    previous: tuple[tuple[str, int, str], ...] | None = None
    while time.monotonic() < deadline:
        active = backend.active_outputs()
        signature = tuple(
            sorted(
                (name, info.crtc, ramp_hash(backend.get_ramp(info)))
                for name, info in active.items()
            )
        )
        now = time.monotonic()
        if signature and signature == previous:
            if now - stable_since >= 0.5:
                return
        else:
            previous = signature
            stable_since = now
        time.sleep(0.1)
    raise RuntimeError("display gamma ramps did not stabilize")


def save_gnome_setting() -> None:
    if not GNOME_BACKUP.exists():
        atomic_json(
            GNOME_BACKUP,
            {"night_light_enabled": gsettings_get_bool(GNOME_SCHEMA, GNOME_KEY)},
        )


def restore_gnome_setting() -> None:
    backup = read_json(GNOME_BACKUP)
    if "night_light_enabled" not in backup:
        return
    gsettings_set_bool(GNOME_SCHEMA, GNOME_KEY, bool(backup["night_light_enabled"]))
    GNOME_BACKUP.unlink(missing_ok=True)


def persist_runtime_state(
    outputs: dict[str, OutputState], target: Target, running: bool, applied: bool
) -> None:
    atomic_json(
        RUNTIME_STATE,
        {
            "version": 1,
            "owner_pid": os.getpid(),
            "outputs": {name: state.serialized() for name, state in outputs.items()},
            "status": {
                "running": running,
                "applied": applied,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "solar_elevation_deg": round(target.elevation, 6),
                "mode": target.mode,
                "rgb_gains": [round(value, 6) for value in target.rgb],
                "model": "SPECTRL2 clear-sky direct sun -> CIE 1931 -> sRGB",
                "active_outputs": sorted(outputs),
            },
        },
    )


def restore_owned(
    backend: XRandR | MutterDisplayConfig,
    outputs: dict[str, OutputState],
    update_hash: bool = True,
) -> int:
    restored = 0
    active = backend.active_outputs()
    for name, state in outputs.items():
        info = active.get(name)
        if not info or info.crtc != state.info.crtc or info.size != state.info.size:
            continue
        current = backend.get_ramp(info)
        current_hash = ramp_hash(current)
        if current_hash not in state.owned_hashes | {
            state.last_hash,
            state.baseline_hash,
        }:
            log(f"{name}: LUT changed externally; baseline not restored")
            continue
        backend.set_ramp(info, state.baseline)
        if ramp_hash(backend.get_ramp(info)) != state.baseline_hash:
            raise RuntimeError(f"{name}: baseline restore verification failed")
        if update_hash:
            state.last_hash = state.baseline_hash
            state.owned_hashes = {state.baseline_hash}
        restored += 1
    return restored


def restore_from_saved_state() -> int:
    saved = read_json(RUNTIME_STATE)
    previous_outputs = saved.get("outputs", {})
    if not isinstance(previous_outputs, dict):
        return 0
    backend = create_backend()
    states: dict[str, OutputState] = {}
    try:
        active = backend.active_outputs()
        for name, value in previous_outputs.items():
            info = active.get(name)
            if not info or not isinstance(value, dict):
                continue
            try:
                if int(value["crtc"]) != info.crtc or int(value["size"]) != info.size:
                    continue
                baseline = decode_ramp(value["baseline"], info.size)
                states[name] = OutputState(
                    info,
                    baseline,
                    str(value.get("last_hash", "")),
                    {str(item) for item in value.get("owned_hashes", [])},
                )
            except (KeyError, TypeError, ValueError, base64.binascii.Error):
                continue
        return restore_owned(backend, states)
    finally:
        backend.close()


def daemon(stop_event: threading.Event | None = None) -> int:
    config = load_config()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    lock_stream = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError("another solar-screen daemon is running")

    if os.environ.get("SOLAR_SCREEN_MANAGE_GNOME", "1") != "0":
        disable_gnome_night_light(wait=True)
    backend = create_backend()
    wait_for_stable_ramps(backend)
    saved = read_json(RUNTIME_STATE)
    previous = (
        saved.get("outputs", {}) if isinstance(saved.get("outputs"), dict) else {}
    )
    outputs: dict[str, OutputState] = {}
    identity_baseline = os.environ.get("SOLAR_SCREEN_BASELINE") == "identity"
    stopping = stop_event if stop_event is not None else threading.Event()

    if stop_event is None:

        def stop(_signum: int, _frame: object) -> None:
            stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
    last_mode = ""
    next_gnome_check = 0.0
    next_log = 0.0
    target = target_for_elevation(-90.0, config["red_only_below_elevation"])
    try:
        while not stopping.is_set():
            monotonic_now = time.monotonic()
            if monotonic_now >= next_gnome_check:
                if disable_gnome_night_light(wait=True):
                    wait_for_stable_ramps(backend)
                next_gnome_check = monotonic_now + 30.0

            instant = datetime.now(timezone.utc)
            elevation = solar_elevation(
                instant,
                config["latitude"],
                config["longitude"],
                config["altitude_m"],
            )
            target = target_for_elevation(elevation, config["red_only_below_elevation"])
            active = backend.active_outputs()
            planned: dict[str, tuple[OutputInfo, Ramp, str, str]] = {}
            for name, info in active.items():
                current = backend.get_ramp(info)
                current_hash = ramp_hash(current)
                state = outputs.get(name)
                if (
                    state is None
                    or state.info.crtc != info.crtc
                    or state.info.size != info.size
                ):
                    state = output_state_from_current(info, current, previous.get(name))
                    if identity_baseline:
                        # The greeter has no user calibration; never compound
                        # with a LUT left behind by a previous session.
                        baseline = identity_ramp(info.size)
                        state = OutputState(
                            info,
                            baseline,
                            current_hash,
                            {current_hash, ramp_hash(baseline)},
                        )
                    outputs[name] = state
                    log(f"{name}: controlling CRTC {info.crtc}, LUT {info.size}")
                elif current_hash != state.last_hash:
                    if current_hash != state.baseline_hash:
                        log(f"{name}: external LUT change adopted as new baseline")
                        state.baseline = current
                    state.last_hash = current_hash

                desired = compose_ramp(state.baseline, target.rgb)
                desired_hash = ramp_hash(desired)
                state.last_hash = desired_hash
                state.owned_hashes = {
                    state.baseline_hash,
                    current_hash,
                    desired_hash,
                }
                planned[name] = (info, desired, current_hash, desired_hash)

            # Persist baseline plus intended hash before touching a LUT. This
            # makes every crash point recoverable without compounding filters.
            persist_runtime_state(outputs, target, True, False)
            for name, (info, desired, current_hash, desired_hash) in planned.items():
                if current_hash != desired_hash:
                    backend.set_ramp(info, desired)
                    if ramp_hash(backend.get_ramp(info)) != desired_hash:
                        raise RuntimeError(f"{name}: gamma write verification failed")
            persist_runtime_state(outputs, target, True, True)

            if target.mode != last_mode or monotonic_now >= next_log:
                log(
                    f"sun {target.elevation:.3f} deg; "
                    f"RGB {target.red:.4f}/{target.green:.4f}/{target.blue:.4f}; "
                    f"{target.mode}"
                )
                last_mode = target.mode
                next_log = monotonic_now + 300.0
            stopping.wait(config["update_seconds"])
    finally:
        try:
            count = restore_owned(backend, outputs)
            log(f"restored {count} output(s)")
            persist_runtime_state(outputs, target, False, False)
        except Exception as error:
            log(f"solar-screen: could not restore gamma ramps: {error}")
        finally:
            backend.close()
            lock_stream.close()
    return 0


def daemon_supervised() -> int:
    """Run the daemon, waiting for a compositor that comes and goes.

    Used by the GDM greeter service: the session bus and mutter only exist
    while the login screen is up.
    """
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bus_path = RUNTIME_DIR.parent / "bus"
    while not stopping.is_set():
        runtime_ready = RUNTIME_DIR.parent.is_dir()
        session_ready = bool(
            os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        ) or bus_path.exists()
        if runtime_ready and session_ready:
            if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
            try:
                return daemon(stopping)
            except Exception as error:
                log(f"solar-screen: display session unavailable: {error}")
        stopping.wait(1.0)
    return 0


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *arguments],
        check=check,
        text=True,
        capture_output=not check,
    )


def service_active() -> bool:
    return (
        systemctl(
            "is-active", "--quiet", "solar-screen.service", check=False
        ).returncode
        == 0
    )


def service_main_pid() -> int:
    result = systemctl(
        "show", "--property=MainPID", "--value", "solar-screen.service", check=False
    )
    try:
        return int(result.stdout.strip())
    except (AttributeError, ValueError):
        return 0


def service_ready() -> bool:
    if not service_active():
        return False
    saved = read_json(RUNTIME_STATE)
    status = saved.get("status", {})
    return bool(
        isinstance(status, dict)
        and saved.get("owner_pid") == service_main_pid()
        and status.get("running")
        and status.get("applied")
        and status.get("active_outputs")
    )


def install_unit() -> None:
    source = APP_DIR / "solar-screen.service"
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if UNIT_PATH.is_symlink():
        UNIT_PATH.unlink()
    if not UNIT_PATH.exists() or UNIT_PATH.read_bytes() != source.read_bytes():
        shutil.copyfile(source, UNIT_PATH)
    UNIT_PATH.chmod(0o644)


def stop_service() -> bool:
    systemctl("disable", "--now", "solar-screen.service", check=False)
    deadline = time.monotonic() + 10.0
    while service_active() and time.monotonic() < deadline:
        time.sleep(0.1)
    return not service_active()


def enable() -> int:
    if os.environ.get("XDG_SESSION_TYPE") != "x11":
        raise RuntimeError("solar-screen currently requires an X11 session")
    save_gnome_setting()
    try:
        disable_gnome_night_light(wait=True)
        install_unit()
        systemctl("daemon-reload")
        systemctl("enable", "--now", "solar-screen.service")
        deadline = time.monotonic() + 20.0
        while not service_ready() and time.monotonic() < deadline:
            time.sleep(0.1)
        if not service_ready():
            raise RuntimeError("solar-screen.service did not become ready")
    except BaseException as error:
        if not stop_service():
            raise RuntimeError(
                "solar-screen failed readiness and could not stop; GNOME left untouched"
            ) from error
        restore_gnome_setting()
        raise
    return print_status()


def disable() -> int:
    if not stop_service():
        raise RuntimeError("solar-screen.service did not stop; GNOME left untouched")
    try:
        restore_from_saved_state()
    finally:
        restore_gnome_setting()
    print("solar-screen disabled; original GNOME Night Light setting restored")
    return 0


def current_target() -> Target:
    config = load_config()
    elevation = solar_elevation(
        datetime.now(timezone.utc),
        config["latitude"],
        config["longitude"],
        config["altitude_m"],
    )
    return target_for_elevation(elevation, config["red_only_below_elevation"])


def print_status() -> int:
    target = current_target()
    saved = read_json(RUNTIME_STATE)
    status = saved.get("status", {}) if isinstance(saved.get("status"), dict) else {}
    labels = {
        "direct-sun": "sol directo modelado",
        "twilight": "crepusculo",
        "red-only": "solo rojo",
    }
    active = service_active()
    print(f"Servicio: {'activo' if active else 'inactivo'}")
    print(f"Elevacion solar: {target.elevation:.3f} deg")
    print(f"Modo: {labels.get(target.mode, target.mode)}")
    print("Ganancias RGB: " + "/".join(f"{value * 100:.1f}%" for value in target.rgb))
    if status.get("active_outputs"):
        print(f"Pantallas: {', '.join(status['active_outputs'])}")
    print("Modelo: sol directo despejado SPECTRL2 -> CIE 1931 -> sRGB")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Match X11 display primaries to modeled local sunlight"
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=("status", "enable", "disable", "daemon", "reset"),
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="with daemon: wait for the display session to appear",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    command = arguments.command
    if command == "daemon":
        return daemon_supervised() if arguments.wait else daemon()
    if command == "enable":
        return enable()
    if command == "disable":
        return disable()
    if command == "reset":
        restored = restore_from_saved_state()
        log(f"restored {restored} output(s) from saved state")
        return 0
    return print_status()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as error:
        log(f"solar-screen: {error}")
        raise SystemExit(1)

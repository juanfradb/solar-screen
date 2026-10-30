#!/usr/bin/env python3

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from solar_screen import (
    MutterDisplayConfig,
    OutputInfo,
    compose_ramp,
    create_backend,
    decode_ramp,
    encode_ramp,
    identity_ramp,
    output_state_from_current,
    ramp_hash,
    solar_elevation,
    target_for_elevation,
)
from solar_table import SOLAR_RGB_TABLE


class SolarModelTests(unittest.TestCase):
    def test_greenwich_vectors_match_pvlib(self):
        vectors = (
            (datetime(2026, 3, 20, 12, tzinfo=timezone.utc), 38.472784),
            (datetime(2026, 6, 21, 12, tzinfo=timezone.utc), 61.966578),
            (datetime(2026, 12, 21, 12, tzinfo=timezone.utc), 15.142931),
        )
        for instant, expected in vectors:
            actual = solar_elevation(instant, 51.4769, -0.0005, 46)
            self.assertAlmostEqual(actual, expected, delta=0.06)

    def test_same_instant_is_timezone_independent(self):
        utc = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)
        argentina = utc.astimezone(timezone(timedelta(hours=-3)))
        self.assertEqual(
            solar_elevation(utc, 51.4769, -0.0005, 46),
            solar_elevation(argentina, 51.4769, -0.0005, 46),
        )

    def test_rgb_anchors(self):
        self.assertEqual(target_for_elevation(-6).rgb, (1.0, 0.0, 0.0))
        self.assertEqual(target_for_elevation(-30).mode, "red-only")
        horizon = target_for_elevation(0)
        self.assertAlmostEqual(horizon.green, 0.335132)
        self.assertEqual(horizon.blue, 0)
        zenith = target_for_elevation(90)
        self.assertAlmostEqual(zenith.green, 0.940884)
        self.assertAlmostEqual(zenith.blue, 0.872336)

    def test_twilight_is_continuous_and_monotonic(self):
        elevations = [value / 10 for value in range(-60, 901)]
        targets = [target_for_elevation(value) for value in elevations]
        greens = [target.green for target in targets]
        blues = [target.blue for target in targets]
        self.assertTrue(all(a <= b for a, b in zip(greens, greens[1:])))
        self.assertTrue(all(a <= b for a, b in zip(blues, blues[1:])))
        self.assertLess(
            abs(target_for_elevation(-0.0001).green - target_for_elevation(0).green),
            1e-6,
        )

    def test_generated_table_shape_and_monotonicity(self):
        self.assertEqual(len(SOLAR_RGB_TABLE), 181)
        for channel in (0, 1):
            values = [row[channel] for row in SOLAR_RGB_TABLE]
            self.assertTrue(all(a <= b for a, b in zip(values, values[1:])))


class GammaRampTests(unittest.TestCase):
    def test_identity_and_red_only(self):
        baseline = ((0, 32768, 65535),) * 3
        self.assertEqual(compose_ramp(baseline, (1, 1, 1)), baseline)
        red = compose_ramp(baseline, (1, 0, 0))
        self.assertEqual(red[0], baseline[0])
        self.assertEqual(red[1], (0, 0, 0))
        self.assertEqual(red[2], (0, 0, 0))

    def test_round_trip_encoding(self):
        ramp = ((0, 1, 65535), (2, 3, 4), (5, 6, 7))
        self.assertEqual(decode_ramp(encode_ramp(ramp), 3), ramp)

    def test_identity_ramp(self):
        ramp = identity_ramp(1024)
        self.assertEqual(len(ramp), 3)
        self.assertEqual(ramp[0][0], 0)
        self.assertEqual(ramp[0][-1], 65535)
        self.assertEqual(ramp[0], ramp[1])
        self.assertEqual(ramp[1], ramp[2])
        self.assertTrue(all(a <= b for a, b in zip(ramp[0], ramp[0][1:])))

    def test_pre_write_crash_keeps_original_baseline(self):
        baseline = ((0, 32768, 65535),) * 3
        current = compose_ramp(baseline, (1.0, 0.8, 0.6))
        intended = compose_ramp(baseline, (1.0, 0.79, 0.59))
        previous = {
            "crtc": 7,
            "size": 3,
            "baseline": encode_ramp(baseline),
            "last_hash": ramp_hash(intended),
            "owned_hashes": [
                ramp_hash(baseline),
                ramp_hash(current),
                ramp_hash(intended),
            ],
        }
        state = output_state_from_current(OutputInfo("test", 7, 3), current, previous)
        self.assertEqual(state.baseline, baseline)


class MutterBackendTests(unittest.TestCase):
    def test_crtc_is_active_uses_layout_size(self):
        self.assertTrue(
            MutterDisplayConfig._crtc_is_active(
                (0, 78, 0, 0, 1920, 1080, 0, 0, [], {})
            )
        )
        self.assertFalse(
            MutterDisplayConfig._crtc_is_active(
                (1, 79, 0, 0, 0, 0, -1, 0, [], {})
            )
        )

    def test_active_outputs_seeds_empty_ramp(self):
        backend = object.__new__(MutterDisplayConfig)
        writes = {}
        backend._resources = lambda: (
            7,
            [(0, 78, 0, 0, 1920, 1080, 0, 0, [0], {})],
        )
        backend._gamma = lambda serial, crtc: ((), (), ())
        backend.set_ramp = lambda info, ramp: writes.setdefault(
            info.name, (info.size, ramp)
        )
        outputs = backend.active_outputs()
        self.assertEqual(
            outputs["mutter-crtc-0"].size, MutterDisplayConfig.DEFAULT_LUT_SIZE
        )
        self.assertEqual(
            writes["mutter-crtc-0"][1],
            identity_ramp(MutterDisplayConfig.DEFAULT_LUT_SIZE),
        )

    def test_active_outputs_keeps_existing_ramp(self):
        backend = object.__new__(MutterDisplayConfig)
        ramp = identity_ramp(64)
        backend._resources = lambda: (
            1,
            [(2, 80, 0, 0, 1280, 720, 1, 0, [0], {})],
        )
        backend._gamma = lambda serial, crtc: ramp
        backend.set_ramp = lambda info, value: self.fail("must not write")
        outputs = backend.active_outputs()
        self.assertEqual(outputs["mutter-crtc-2"].size, 64)

    def test_active_outputs_skips_inactive_crtc(self):
        backend = object.__new__(MutterDisplayConfig)
        backend._resources = lambda: (
            1,
            [(1, 79, 0, 0, 0, 0, -1, 0, [], {})],
        )
        backend._gamma = lambda serial, crtc: self.fail("must not read")
        self.assertEqual(backend.active_outputs(), {})


class BackendSelectionTests(unittest.TestCase):
    def test_wayland_prefers_mutter(self):
        calls = []
        environment = {"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}
        with patch.dict(os.environ, environment, clear=False):
            with patch(
                "solar_screen.XRandR", lambda: calls.append("x11") or "x11"
            ), patch(
                "solar_screen.MutterDisplayConfig",
                lambda: calls.append("mutter") or "mutter",
            ):
                self.assertEqual(create_backend(), "mutter")
        self.assertEqual(calls, ["mutter"])

    def test_x11_prefers_xrandr(self):
        calls = []
        environment = {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"}
        with patch.dict(os.environ, environment, clear=False):
            with patch(
                "solar_screen.XRandR", lambda: calls.append("x11") or "x11"
            ), patch(
                "solar_screen.MutterDisplayConfig",
                lambda: calls.append("mutter") or "mutter",
            ):
                self.assertEqual(create_backend(), "x11")
        self.assertEqual(calls, ["x11"])

    def test_falls_back_to_mutter(self):
        def broken_x11():
            raise RuntimeError("cannot open display")

        with patch.dict(
            os.environ,
            {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"},
            clear=False,
        ), patch("solar_screen.XRandR", broken_x11), patch(
            "solar_screen.MutterDisplayConfig", lambda: "mutter"
        ):
            self.assertEqual(create_backend(), "mutter")

    def test_unknown_backend_is_rejected(self):
        with patch.dict(
            os.environ, {"SOLAR_SCREEN_BACKEND": "bogus"}, clear=False
        ):
            with self.assertRaises(RuntimeError):
                create_backend()


if __name__ == "__main__":
    unittest.main()

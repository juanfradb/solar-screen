#!/usr/bin/env python3
"""Generate the runtime elevation-to-sRGB lookup table."""

import argparse

import colour
import numpy as np
from pvlib.atmosphere import alt2pres, get_relative_airmass
from pvlib.spectrum import spectrl2


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--altitude", type=float, default=390.0)
    return parser.parse_args()


def generate(altitude):
    elevation = np.arange(0.0, 90.0001, 0.5)
    zenith = 90.0 - elevation
    spectra = spectrl2(
        apparent_zenith=zenith,
        aoi=zenith,
        surface_tilt=np.zeros_like(zenith),
        ground_albedo=0.18,
        surface_pressure=float(alt2pres(altitude)),
        relative_airmass=get_relative_airmass(zenith, model="kastenyoung1989"),
        precipitable_water=1.5,
        ozone=0.3,
        aerosol_turbidity_500nm=0.1,
        dayofyear=np.full_like(zenith, 199),
    )
    wavelengths = spectra["wavelength"]
    irradiance = spectra["dni"]
    cmfs = colour.MSDS_CMFS["CIE 1931 2 Degree Standard Observer"]
    xyz = np.stack(
        [
            np.trapezoid(
                irradiance
                * np.interp(
                    wavelengths,
                    cmfs.wavelengths,
                    cmfs.values[:, channel],
                    left=0,
                    right=0,
                )[:, None],
                wavelengths,
                axis=0,
            )
            for channel in range(3)
        ],
        axis=1,
    )
    xyz /= xyz[:, 1, None]
    rgb = np.clip(colour.XYZ_to_sRGB(xyz), 0, None)
    rgb /= rgb.max(axis=1)[:, None]
    return tuple((float(row[1]), float(row[2])) for row in rgb)


def main():
    table = generate(parse_args().altitude)
    print("SOLAR_RGB_TABLE = (")
    for green, blue in table:
        print(f"    ({green:.6f}, {blue:.6f}),")
    print(")")


if __name__ == "__main__":
    main()

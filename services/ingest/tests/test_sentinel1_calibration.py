"""Sentinel-1 calibration coordinate regression tests."""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np


def _load_sentinel1_module():
    """Load raster processing code while stubbing the unused COG writer if absent."""
    try:
        return importlib.import_module("app.tasks.sentinel1")
    except ModuleNotFoundError as exc:
        if exc.name != "rio_cogeo":
            raise

    package = types.ModuleType("rio_cogeo")
    package.__path__ = []
    cogeo = types.ModuleType("rio_cogeo.cogeo")
    cogeo.cog_translate = lambda *args, **kwargs: None
    profiles = types.ModuleType("rio_cogeo.profiles")
    profiles.cog_profiles = {}
    with patch.dict(
        sys.modules,
        {
            "rio_cogeo": package,
            "rio_cogeo.cogeo": cogeo,
            "rio_cogeo.profiles": profiles,
        },
    ):
        return importlib.import_module("app.tasks.sentinel1")


class Sentinel1CalibrationCoordinateTests(unittest.TestCase):
    def test_zero_based_pixel_centers_include_lut_edge_pixels(self) -> None:
        sentinel1 = _load_sentinel1_module()
        lut = sentinel1.S1CalibrationLUT(
            lines=np.asarray([0.0, 1.0]),
            pixels=(np.asarray([0.0, 1.0, 2.0]),) * 2,
            sigma_nought=(
                np.asarray([2.0, 3.0, 4.0]),
                np.asarray([4.0, 5.0, 6.0]),
            ),
        )

        source_power = sentinel1._calibrate_source_window_sigma0(
            np.full((2, 3), 4.0, dtype=np.float32), lut, row_offset=0, col_offset=0
        )

        expected = np.asarray(
            [[4.0, 16.0 / 9.0, 1.0], [1.0, 16.0 / 25.0, 4.0 / 9.0]],
            dtype=np.float32,
        )
        np.testing.assert_allclose(source_power, expected, rtol=1e-6, atol=1e-6)
        self.assertTrue(np.isfinite(source_power[:, [0, -1]]).all())


if __name__ == "__main__":
    unittest.main()

"""NDTI registry + agri optical wiring (straw/residue index)."""

from __future__ import annotations

import unittest

import numpy as np

from agric_satellite_analysis_common.lonlat_pixels import (
    _PREFERRED_COLUMN_ORDER,
    to_columnar_pixel_data,
)
from app.tasks.agri_lonlat import (
    AGRI_OPTICAL_INDEX_KEYS,
    INDEX_KEY_TO_PIXEL,
    agri_optical_index_defs,
)
from app.tasks.indices import get_index


class NdtiIndexTests(unittest.TestCase):
    def test_ndti_formula_and_bands(self) -> None:
        idx = get_index("ndti")
        self.assertEqual(idx.key, "ndti")
        self.assertEqual(idx.label, "NDTI")
        self.assertEqual(idx.bands, ("B11", "B12"))
        self.assertIn("B12", idx.stac_asset_map)
        self.assertEqual(idx.stac_asset_map["B12"][0], "swir22")
        # Reflectance: B11=0.3, B12=0.1 → (0.2)/(0.4) = 0.5
        out = idx.formula(
            {
                "B11": np.array([0.3], dtype=np.float32),
                "B12": np.array([0.1], dtype=np.float32),
            }
        )
        self.assertAlmostEqual(float(out[0]), 0.5, places=5)
        # Quantized DN + scale/offset must match reflectance path.
        out_dn = idx.formula(
            {
                "B11": np.array([4000.0], dtype=np.float32),
                "B12": np.array([2000.0], dtype=np.float32),
            },
            band_radiometry={
                "B11": {"scale": 0.0001, "offset": 0.0},
                "B12": {"scale": 0.0001, "offset": 0.0},
            },
        )
        # (0.4-0.2)/(0.4+0.2)=1/3
        self.assertAlmostEqual(float(out_dn[0]), 1.0 / 3.0, places=5)

    def test_ndti_in_agri_optical_emit_set(self) -> None:
        self.assertIn("ndti", AGRI_OPTICAL_INDEX_KEYS)
        self.assertEqual(INDEX_KEY_TO_PIXEL["ndti"], "NDTI")
        keys = [d.key for d in agri_optical_index_defs()]
        self.assertIn("ndti", keys)
        # B12 must be among bands fetched for optical products
        bands = {b for d in agri_optical_index_defs() for b in d.bands}
        self.assertIn("B12", bands)
        self.assertIn("B11", bands)

    def test_preferred_column_order_includes_ndti(self) -> None:
        self.assertIn("NDTI", _PREFERRED_COLUMN_ORDER)
        # After MNDWI, before S1
        mndwi_i = _PREFERRED_COLUMN_ORDER.index("MNDWI")
        ndti_i = _PREFERRED_COLUMN_ORDER.index("NDTI")
        vv_i = _PREFERRED_COLUMN_ORDER.index("VV_db")
        self.assertLess(mndwi_i, ndti_i)
        self.assertLess(ndti_i, vv_i)

    def test_columnar_roundtrip_keeps_ndti(self) -> None:
        pixels = [
            {
                "lon": 122.56,
                "lat": 43.09,
                "clear": 1,
                "NDVI": 0.5,
                "EVI": 0.4,
                "NDMI": 0.1,
                "NDRE": 0.2,
                "CIre": 0.3,
                "MNDWI": -0.2,
                "NDTI": 0.12,
            }
        ]
        payload = to_columnar_pixel_data(pixels)
        self.assertIn("NDTI", payload["columns"])
        self.assertEqual(payload["columns"]["NDTI"][0], 0.12)


if __name__ == "__main__":
    unittest.main()

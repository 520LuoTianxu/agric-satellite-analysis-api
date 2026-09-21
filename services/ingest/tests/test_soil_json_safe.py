"""Regression tests for soil payload JSON normalization."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.tasks.soil import _json_safe  # noqa: E402


class SoilJsonSafeTests(unittest.TestCase):
    def test_normalizes_nested_non_finite_and_numpy_floats(self) -> None:
        payload = {
            "layers": [
                {
                    "missing": float("nan"),
                    "overflow": float("inf"),
                    "finite_numpy": np.float32(1.25),
                    "missing_numpy": np.float64(np.nan),
                }
            ],
            "negative_overflow": (float("-inf"),),
        }

        safe_payload = _json_safe(payload)

        self.assertIsNone(safe_payload["layers"][0]["missing"])
        self.assertIsNone(safe_payload["layers"][0]["overflow"])
        self.assertEqual(safe_payload["layers"][0]["finite_numpy"], 1.25)
        self.assertIsInstance(safe_payload["layers"][0]["finite_numpy"], float)
        self.assertIsNone(safe_payload["layers"][0]["missing_numpy"])
        self.assertEqual(safe_payload["negative_overflow"], [None])


if __name__ == "__main__":
    unittest.main()

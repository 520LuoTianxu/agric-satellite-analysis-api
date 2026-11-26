"""Precision sowing stays inside the key-node row cap."""

from __future__ import annotations

import unittest
from pathlib import Path


class SowingQueryTests(unittest.TestCase):
    def test_precision_sowing_is_ordered_before_the_row_cap(self) -> None:
        source = Path(__file__).resolve().parents[1].joinpath(
            "app/services/biweekly_field_records.py"
        )
        sql = source.read_text(encoding="utf-8")
        self.assertIn("PRECISION_SOWING", sql)
        self.assertLess(sql.index("PRECISION_SOWING"), sql.index("LIMIT 16"))

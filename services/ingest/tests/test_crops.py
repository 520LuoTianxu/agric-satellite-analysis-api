"""Unit tests for crop catalog normalize + season lookup."""

from __future__ import annotations

import unittest

from app.core.crops import (
    get_crop_season,
    list_crops,
    normalize_crop_key,
)


class NormalizeCropKeyTests(unittest.TestCase):
    def test_english_key(self) -> None:
        self.assertEqual(normalize_crop_key("corn"), "corn")
        self.assertEqual(normalize_crop_key("Wheat"), "wheat")

    def test_aliases_spring_summer_corn_map_to_corn(self) -> None:
        self.assertEqual(normalize_crop_key("春玉米"), "corn")
        self.assertEqual(normalize_crop_key("夏玉米"), "corn")
        self.assertEqual(normalize_crop_key("maize"), "corn")
        self.assertEqual(normalize_crop_key("玉米"), "corn")

    def test_unknown(self) -> None:
        self.assertIsNone(normalize_crop_key(None))
        self.assertIsNone(normalize_crop_key(""))
        self.assertIsNone(normalize_crop_key("not_a_crop_xyz"))


class GetCropSeasonTests(unittest.TestCase):
    def test_corn_default_is_jun_sep(self) -> None:
        season = get_crop_season("corn")
        self.assertEqual(set(season.season_months), {6, 7, 8, 9})
        self.assertEqual(set(season.peak_months), {7, 8})
        self.assertIn("玉米", season.label_zh)

    def test_spring_alias_uses_corn_season(self) -> None:
        self.assertEqual(
            get_crop_season("春玉米").season_months,
            get_crop_season("corn").season_months,
        )

    def test_wheat_spring_window(self) -> None:
        season = get_crop_season("wheat")
        self.assertEqual(set(season.season_months), {3, 4, 5, 6})

    def test_list_crops_has_corn_not_spring_summer_keys(self) -> None:
        keys = {c["key"] for c in list_crops()}
        self.assertIn("corn", keys)
        self.assertNotIn("corn_spring", keys)
        self.assertNotIn("corn_summer", keys)


if __name__ == "__main__":
    unittest.main()

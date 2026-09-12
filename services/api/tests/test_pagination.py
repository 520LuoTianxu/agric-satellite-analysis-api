"""Tests for newest-page offset helper used by agri timeseries pagination."""

from __future__ import annotations

import unittest

from app.core.pagination import newest_page_offset


class NewestPageOffsetTests(unittest.TestCase):
    def test_total_within_limit(self) -> None:
        self.assertEqual(newest_page_offset(278, 500), 0)
        self.assertEqual(newest_page_offset(0, 500), 0)
        self.assertEqual(newest_page_offset(500, 500), 0)

    def test_total_exceeds_limit(self) -> None:
        self.assertEqual(newest_page_offset(800, 500), 300)
        self.assertEqual(newest_page_offset(501, 500), 1)

    def test_invalid_limit(self) -> None:
        self.assertEqual(newest_page_offset(100, 0), 0)
        self.assertEqual(newest_page_offset(100, -10), 0)


if __name__ == "__main__":
    unittest.main()

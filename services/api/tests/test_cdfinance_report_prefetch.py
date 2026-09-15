"""Unit tests for report-time cdfinance prefetch helpers."""

from __future__ import annotations

import unittest

from app.services.cdfinance_report_prefetch import (
    normalize_optional_group_id,
    normalize_optional_hr_base_id,
    normalize_optional_token,
    resolve_request_token,
)


class TestNormalizeHelpers(unittest.TestCase):
    def test_normalize_token(self):
        self.assertIsNone(normalize_optional_token(None))
        self.assertIsNone(normalize_optional_token("  "))
        self.assertEqual(normalize_optional_token("abc"), "abc")
        self.assertEqual(normalize_optional_token("Bearer abc"), "abc")
        self.assertEqual(normalize_optional_token("bearer xyz"), "xyz")

    def test_normalize_group_id(self):
        self.assertIsNone(normalize_optional_group_id(None))
        self.assertIsNone(normalize_optional_group_id("  "))
        self.assertEqual(normalize_optional_group_id(3232), "3232")
        self.assertEqual(normalize_optional_group_id(" 99 "), "99")

    def test_normalize_hr_base_id(self):
        self.assertIsNone(normalize_optional_hr_base_id(None))
        self.assertIsNone(normalize_optional_hr_base_id("  "))
        self.assertEqual(normalize_optional_hr_base_id(10), "10")
        self.assertEqual(normalize_optional_hr_base_id(" 10 "), "10")

    def test_resolve_request_token_precedence(self):
        self.assertEqual(
            resolve_request_token(
                cdfinance_token="a",
                token="b",
                authorization="Bearer c",
            ),
            "a",
        )
        self.assertEqual(
            resolve_request_token(token="b", authorization="Bearer c"),
            "b",
        )
        self.assertEqual(
            resolve_request_token(authorization="Bearer c"),
            "c",
        )
        self.assertIsNone(resolve_request_token())


if __name__ == "__main__":
    unittest.main()

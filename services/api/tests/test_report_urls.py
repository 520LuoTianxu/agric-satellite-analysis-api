"""Private OSS report URL response helpers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services.report_urls import report_progress_for_response


class _FakeStorage:
    def presigned_get(self, key, *, expires):
        self.key = key
        self.expires = expires
        return f"https://signed.example/{key}?signature=test"


class ReportUrlTests(unittest.TestCase):
    def test_private_report_progress_gets_signed_url_without_mutating_input(self):
        storage = _FakeStorage()
        progress = {
            "object_key": "reports/default/land-1/assessment.pdf",
            "public_url": "https://agric-dev.oss-cn-beijing.aliyuncs.com/reports/default/land-1/assessment.pdf",
            "score": 82,
        }

        with patch("app.services.report_urls.get_storage", return_value=storage):
            result = report_progress_for_response(progress)

        self.assertEqual(result["public_url"], result["download_url"])
        self.assertIn("signature=test", result["public_url"])
        self.assertEqual(progress["public_url"], "https://agric-dev.oss-cn-beijing.aliyuncs.com/reports/default/land-1/assessment.pdf")
        self.assertEqual(storage.key, "reports/default/land-1/assessment.pdf")

    def test_signing_failure_removes_unusable_private_url(self):
        with patch(
            "app.services.report_urls.get_storage",
            side_effect=RuntimeError("missing OSS credentials"),
        ):
            result = report_progress_for_response(
                {
                    "object_key": "reports/default/land-1/assessment.pdf",
                    "public_url": "https://agric-dev.oss-cn-beijing.aliyuncs.com/private.pdf",
                }
            )

        self.assertNotIn("public_url", result)
        self.assertNotIn("download_url", result)


if __name__ == "__main__":
    unittest.main()

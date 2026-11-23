"""OssStorage.bucket is a str property. Calling it is a TypeError.

Isolated fixture only: oss2.Bucket is a MagicMock, no network and no
production credentials. The bytes below are not the production P18 PDF.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from agric_satellite_analysis_common.biweekly.official_ref import (
    inspect_official_object,
    official_object_key,
    sha256_bytes,
)
from agric_satellite_analysis_common.settings import CommonSettings
from agric_satellite_analysis_common import storage as storage_mod
from agric_satellite_analysis_common.storage import bucket_name


BUCKET = "agric-isolated-fixture"
LAND = "32696"
JOB = "bj-b435c12f9a70f8a9"
MIN_PDF = b"%PDF-1.4\n" + b"isolated-fixture-pdf\n" * 80 + b"%%EOF\n"


def make_oss_storage() -> storage_mod.OssStorage:
    settings = CommonSettings(
        oss_access_key_id="AKTEST",
        oss_access_key_secret="SECRETVALUE",
        oss_bucket=BUCKET,
        oss_endpoint="https://oss-cn-beijing.aliyuncs.com",
        oss_is_cname=False,
        storage_backend="oss",
    )
    with (
        patch.object(storage_mod, "settings", settings),
        patch.object(storage_mod.oss2, "Auth", return_value=MagicMock()),
        patch.object(storage_mod.oss2, "Session", return_value=MagicMock()),
        patch.object(storage_mod.oss2, "Bucket", return_value=MagicMock()),
    ):
        return storage_mod.OssStorage(sleep=lambda _s: None)


class BucketPropertyTests(unittest.TestCase):
    def test_real_oss_storage_bucket_call_raises_typeerror(self) -> None:
        storage = make_oss_storage()
        self.assertIsInstance(storage_mod.OssStorage.bucket, property)
        self.assertEqual(storage.bucket, BUCKET)
        self.assertEqual(bucket_name(storage), BUCKET)
        with self.assertRaises(TypeError) as caught:
            storage.bucket()
        self.assertIn("not callable", str(caught.exception))

    def test_bucket_name_does_not_call_a_method(self) -> None:
        class MethodBucket:
            def __init__(self) -> None:
                self.calls = 0

            def bucket(self) -> str:
                self.calls += 1
                return "called"

        storage = MethodBucket()
        with self.assertRaises(TypeError) as caught:
            bucket_name(storage)
        self.assertEqual(storage.calls, 0)
        self.assertIn("str property", str(caught.exception))

    def test_inspect_uses_property_on_real_oss_storage(self) -> None:
        storage = make_oss_storage()
        key = official_object_key(LAND, JOB)
        body = MIN_PDF

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data
                self._i = 0

            def read(self, n: int = -1) -> bytes:
                if n is None or n < 0:
                    n = len(self._data) - self._i
                chunk = self._data[self._i : self._i + n]
                self._i += len(chunk)
                return chunk

            def close(self) -> None:
                return None

        oss_bucket = MagicMock()
        oss_bucket.object_exists.return_value = True
        meta = MagicMock()
        meta.content_length = len(body)
        meta.content_type = "application/pdf"
        meta.etag = "abc"
        meta.headers = {}
        oss_bucket.head_object.return_value = meta
        oss_bucket.get_object.side_effect = lambda *_a, **_k: _Body(body)
        storage._bucket = oss_bucket
        ref = {
            "bucket": BUCKET,
            "key": key,
            "size_bytes": len(body),
            "sha256": sha256_bytes(body),
            "content_type": "application/pdf",
        }
        out = inspect_official_object(storage, ref, expect_pdf=True)
        self.assertEqual(out["sha256"], sha256_bytes(body))
        self.assertEqual(out["bucket"], BUCKET)
        self.assertTrue(str(out["key"]).endswith(f"biweekly-{JOB}.pdf"))


if __name__ == "__main__":
    unittest.main()

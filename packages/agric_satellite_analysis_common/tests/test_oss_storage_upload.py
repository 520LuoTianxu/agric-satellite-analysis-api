"""Unit tests for OssStorage multipart / retry upload (mocked oss2, no credentials)."""

from __future__ import annotations

import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agric_satellite_analysis_common import storage as storage_mod
from agric_satellite_analysis_common.settings import CommonSettings


class ResolveOssIsCnameTests(unittest.TestCase):
    def test_explicit_true_wins(self) -> None:
        self.assertTrue(
            storage_mod.resolve_oss_is_cname(
                "https://oss-cn-beijing.aliyuncs.com",
                "agric-prod",
                True,
            )
        )

    def test_explicit_false_wins(self) -> None:
        self.assertFalse(
            storage_mod.resolve_oss_is_cname(
                "https://agric-prod.oss-accelerate.aliyuncs.com",
                "agric-prod",
                False,
            )
        )

    def test_auto_detects_bucket_prefixed_accelerate(self) -> None:
        self.assertTrue(
            storage_mod.resolve_oss_is_cname(
                "https://agric-prod.oss-accelerate.aliyuncs.com",
                "agric-prod",
                None,
            )
        )

    def test_auto_false_for_regional_endpoint(self) -> None:
        self.assertFalse(
            storage_mod.resolve_oss_is_cname(
                "https://oss-cn-beijing.aliyuncs.com",
                "agric-prod",
                None,
            )
        )

    def test_auto_false_for_bare_accelerate(self) -> None:
        self.assertFalse(
            storage_mod.resolve_oss_is_cname(
                "https://oss-accelerate.aliyuncs.com",
                "agric-prod",
                None,
            )
        )


class TransientClassificationTests(unittest.TestCase):
    def test_ssl_and_timeout_are_transient(self) -> None:
        self.assertTrue(storage_mod.is_transient_oss_error(ssl.SSLEOFError(8, "eof")))
        self.assertTrue(storage_mod.is_transient_oss_error(TimeoutError("timed out")))
        self.assertTrue(
            storage_mod.is_transient_oss_error(
                RuntimeError("Server disconnected without sending a response.")
            )
        )

    def test_http_4xx_not_transient_5xx_is(self) -> None:
        class HttpExc(Exception):
            def __init__(self, status: int) -> None:
                super().__init__(f"status={status}")
                self.status = status

        self.assertFalse(storage_mod.is_transient_oss_error(HttpExc(403)))
        self.assertTrue(storage_mod.is_transient_oss_error(HttpExc(503)))
        self.assertTrue(storage_mod.is_transient_oss_error(HttpExc(429)))


class RetryOssCallTests(unittest.TestCase):
    def test_retries_transient_then_succeeds(self) -> None:
        n = {"c": 0}

        def flaky() -> str:
            n["c"] += 1
            if n["c"] < 3:
                raise ssl.SSLEOFError(8, "UNEXPECTED_EOF")
            return "ok"

        self.assertEqual(
            storage_mod.retry_oss_call(
                flaky, what="t", attempts=5, base=0.0, sleep=lambda _s: None
            ),
            "ok",
        )
        self.assertEqual(n["c"], 3)

    def test_non_transient_raises_immediately(self) -> None:
        n = {"c": 0}

        def bad() -> None:
            n["c"] += 1
            raise ValueError("nope")

        with self.assertRaises(ValueError):
            storage_mod.retry_oss_call(
                bad, what="t", attempts=5, base=0.0, sleep=lambda _s: None
            )
        self.assertEqual(n["c"], 1)


def _patch_settings(**kwargs):
    base = dict(
        oss_access_key_id="AKTEST",
        oss_access_key_secret="SECRET",
        oss_bucket="agric-prod",
        oss_endpoint="https://oss-accelerate.aliyuncs.com",
        oss_is_cname=None,
        oss_multipart_threshold_mb=1.0,
        oss_part_size_mb=4.0,
        oss_multipart_num_threads=8,
        oss_upload_max_retries=5,
        oss_upload_retry_backoff_s=0.0,
        oss_resumable_store_dir="",
        oss_connect_timeout_s=10.0,
        oss_read_timeout_s=180.0,
        openfarm_scratch_dir="/tmp/openfarm-scratch-test",
    )
    base.update(kwargs)
    return patch.object(
        storage_mod,
        "settings",
        CommonSettings(**base),
    )


class OssStorageUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        storage_mod.clear_storage_cache()

    def tearDown(self) -> None:
        storage_mod.clear_storage_cache()

    def _make_storage(self, **setting_overrides):
        bucket = MagicMock()
        auth = MagicMock()
        session = MagicMock()
        with (
            _patch_settings(**setting_overrides),
            patch.object(storage_mod.oss2, "Auth", return_value=auth) as auth_cls,
            patch.object(storage_mod.oss2, "Session", return_value=session),
            patch.object(storage_mod.oss2, "Bucket", return_value=bucket) as bucket_cls,
        ):
            storage = storage_mod.OssStorage(sleep=lambda _s: None)
        auth_cls.assert_called_once()
        bucket_cls.assert_called_once()
        kwargs = bucket_cls.call_args.kwargs
        self.assertIn("is_cname", kwargs)
        self.assertIn("connect_timeout", kwargs)
        self.assertIn("session", kwargs)
        storage._bucket = bucket
        storage._test_bucket_cls = bucket_cls
        return storage, bucket

    def test_bucket_is_cname_auto_for_bucket_prefixed_endpoint(self) -> None:
        storage, _bucket = self._make_storage(
            oss_endpoint="https://agric-prod.oss-accelerate.aliyuncs.com",
        )
        self.assertTrue(storage._is_cname)
        self.assertTrue(storage._test_bucket_cls.call_args.kwargs["is_cname"])

    def test_small_file_uses_put_with_retry(self) -> None:
        storage, bucket = self._make_storage(oss_multipart_threshold_mb=1.0)
        calls = {"n": 0}

        def put(key, path, headers=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ssl.SSLEOFError(8, "UNEXPECTED_EOF")
            return None

        bucket.put_object_from_file.side_effect = put
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "small.bin"
            path.write_bytes(b"x" * 100)
            with patch.object(storage_mod.oss2, "resumable_upload") as resumable:
                key = storage.upload_file("k/small.bin", str(path), content_type="application/octet-stream")
        self.assertEqual(key, "k/small.bin")
        self.assertEqual(calls["n"], 2)
        resumable.assert_not_called()
        self.assertEqual(bucket.put_object_from_file.call_count, 2)

    def test_large_file_uses_resumable_multipart(self) -> None:
        storage, bucket = self._make_storage(
            oss_multipart_threshold_mb=1.0,
            oss_part_size_mb=4.0,
            oss_multipart_num_threads=6,
            oss_resumable_store_dir="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "resume"
            storage._store_root = str(store_root)
            path = Path(tmp) / "big.bin"
            path.write_bytes(b"y" * (1024 * 1024 + 10))
            with (
                patch.object(storage_mod.oss2, "resumable_upload") as resumable,
                patch.object(
                    storage_mod.oss2,
                    "ResumableStore",
                    return_value=MagicMock(name="store"),
                ) as store_cls,
            ):
                key = storage.upload_file("k/big.bin", str(path))
                self.assertTrue(store_root.is_dir())
                store_cls.assert_called_once()
                self.assertEqual(store_cls.call_args.kwargs["root"], str(store_root))
                resumable.assert_called_once()
                kwargs = resumable.call_args.kwargs
                self.assertEqual(kwargs["multipart_threshold"], 1024 * 1024)
                self.assertEqual(kwargs["part_size"], 4 * 1024 * 1024)
                self.assertEqual(kwargs["num_threads"], 6)
                self.assertIsNotNone(kwargs["store"])
        self.assertEqual(key, "k/big.bin")
        bucket.put_object_from_file.assert_not_called()

    def test_put_bytes_large_writes_temp_and_multipart(self) -> None:
        storage, bucket = self._make_storage(oss_multipart_threshold_mb=1.0)
        with tempfile.TemporaryDirectory() as tmp:
            storage._store_root = tmp
            payload = b"z" * (1024 * 1024 + 3)
            with patch.object(storage, "upload_file", return_value="k/bytes.bin") as up:
                key = storage.put_bytes("k/bytes.bin", payload, content_type="application/octet-stream")
        self.assertEqual(key, "k/bytes.bin")
        up.assert_called_once()
        args, kwargs = up.call_args
        self.assertEqual(args[0], "k/bytes.bin")
        self.assertTrue(args[1].endswith(".bin"))
        self.assertFalse(Path(args[1]).exists())  # temp cleaned up
        self.assertEqual(kwargs.get("content_type"), "application/octet-stream")
        bucket.put_object.assert_not_called()

    def test_put_bytes_small_retries_put_object(self) -> None:
        storage, bucket = self._make_storage(oss_multipart_threshold_mb=1.0)
        calls = {"n": 0}

        def put(key, data, headers=None):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ConnectionResetError("Connection reset")
            return None

        bucket.put_object.side_effect = put
        key = storage.put_bytes("k/tiny.bin", b"abc")
        self.assertEqual(key, "k/tiny.bin")
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()

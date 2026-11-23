"""API registration against a real OssStorage bucket property.

oss2 is mocked. The PDF is a synthetic fixture, not the production P18 file.
No production database session is opened: MemoryOfficialStore stands in for
agric_satellite.biweekly_official_jobs. The production adapter still opens a
fresh sync Session per call, and the complete route still runs inspect via
asyncio.to_thread so that session is not used on the event-loop thread.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from agric_satellite_analysis_common.biweekly.official_ref import (
    official_object_key,
    sha256_bytes,
)
from agric_satellite_analysis_common.biweekly.official_store import MemoryOfficialStore
from agric_satellite_analysis_common.biweekly.trigger import TriggerService
from agric_satellite_analysis_common.settings import CommonSettings
from agric_satellite_analysis_common import storage as storage_mod
from agric_satellite_analysis_common.storage import bucket_name

from app.middleware.biweekly_auth import require_biweekly_access
from app.routers.biweekly import download_job
from app.routers import internal_biweekly as biweekly_mod


BUCKET = "agric-isolated-fixture"
LAND = "32696"
JOB = "bj-b435c12f9a70f8a9"
SEASON = "2025/26"
MIN_PDF = b"%PDF-1.4\n" + b"isolated-fixture-not-production\n" * 40 + b"%%EOF\n"


class _QuietCatalog:
    """Stand-in so TriggerService does not open the local 6417 pack."""


def make_oss_storage(objects: dict[str, bytes]) -> storage_mod.OssStorage:
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
        storage = storage_mod.OssStorage(sleep=lambda _s: None)

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
    oss_bucket.object_exists.side_effect = lambda key: key in objects

    def head(key):
        data = objects[key]
        meta = MagicMock()
        meta.content_length = len(data)
        meta.content_type = "application/pdf"
        meta.etag = "abc"
        meta.headers = {}
        return meta

    oss_bucket.head_object.side_effect = head
    oss_bucket.get_object.side_effect = lambda key, headers=None: _Body(objects[key])
    storage._bucket = oss_bucket
    storage._thread_ids = []
    original_exists = oss_bucket.object_exists.side_effect

    def exists(key):
        storage._thread_ids.append(threading.get_ident())
        return original_exists(key)

    oss_bucket.object_exists.side_effect = exists
    return storage


def seed(service: TriggerService, *, status: str = "failed") -> None:
    service._write(
        {
            "job_id": JOB,
            "kind": "generate",
            "status": status,
            "land_id": LAND,
            "season_id": SEASON,
            "period_id": "18",
            "payload": {},
            "result": {},
            "history": [{"status": status, "at": "2026-11-23T20:37:54+08:00"}],
            "created_at": "2026-11-23T20:34:41+08:00",
            "updated_at": "2026-11-23T20:37:54+08:00",
            "artifact": None,
            "error_code": "GENERATE_FAILED" if status == "failed" else None,
        }
    )


class RealOssPropertyApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        key = official_object_key(LAND, JOB)
        self.objects = {key: MIN_PDF}
        self.storage = make_oss_storage(self.objects)
        self.service = TriggerService(
            Path(self.tmp.name),
            _QuietCatalog(),
            official_store=MemoryOfficialStore(),
            object_storage=self.storage,
            expected_oss_bucket=bucket_name(self.storage),
        )
        self._patch = patch(
            "app.routers.internal_biweekly.build_trigger_service",
            return_value=self.service,
        )
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmp.cleanup()

    def _body(self, *, status: str = "ready"):
        key = official_object_key(LAND, JOB)
        return biweekly_mod.CompleteRequest(
            status=status,
            filename=f"种植双周报_{LAND}_2026-05-02_2026-05-15.pdf",
            mime="application/pdf",
            kind="official_visual_artifact",
            oss_bucket=BUCKET,
            oss_key=key,
            size_bytes=len(MIN_PDF),
            sha256=sha256_bytes(MIN_PDF),
            content_type="application/pdf",
            facts={
                "land_id": LAND,
                "period": {"start": "2026-05-02", "end_inclusive": "2026-05-15"},
            },
            note="fixture registration only",
            report_job_id=None,
        )

    def test_failed_job_recovers_and_ready_stays_idempotent(self) -> None:
        seed(self.service, status="failed")
        loop_thread = threading.get_ident()
        first = asyncio.run(
            biweekly_mod.biweekly_report_complete(JOB, self._body(), None)
        )
        self.assertEqual(first["job"]["status"], "ready")
        self.assertEqual(first["job"]["artifact"]["sha256"], sha256_bytes(MIN_PDF))
        self.assertNotIn("url", first["job"]["artifact"])
        self.assertNotIn("SECRETVALUE", str(first["job"]["artifact"]))
        self.assertTrue(self.storage._thread_ids)
        self.assertNotEqual(self.storage._thread_ids[0], loop_thread)
        second = asyncio.run(
            biweekly_mod.biweekly_report_complete(JOB, self._body(), None)
        )
        self.assertEqual(second["job"]["status"], "ready")
        self.assertEqual(
            second["job"]["artifact"]["sha256"], first["job"]["artifact"]["sha256"]
        )
        failed = asyncio.run(
            biweekly_mod.biweekly_report_complete(
                JOB,
                biweekly_mod.CompleteRequest(
                    status="failed", error_code="GENERATE_FAILED", note="late"
                ),
                None,
            )
        )
        self.assertEqual(failed["job"]["status"], "ready")
        self.assertEqual(
            self.service.get_job(JOB)["artifact"]["key"], official_object_key(LAND, JOB)
        )
        _name, data, mime = self.service.download(JOB)
        self.assertEqual(mime, "application/pdf")
        self.assertEqual(data, MIN_PDF)

    def test_trigger_service_reads_bucket_property_without_calling_it(self) -> None:
        with TemporaryDirectory() as tmp:
            service = TriggerService(
                Path(tmp),
                catalog=_QuietCatalog(),
                object_storage=self.storage,
            )
        self.assertEqual(service.expected_oss_bucket, BUCKET)
        self.assertIs(service.object_storage, self.storage)

    def test_build_trigger_keeps_real_storage(self) -> None:
        with (
            patch(
                "agric_satellite_analysis_common.storage.get_storage",
                return_value=self.storage,
            ),
            patch(
                "app.services.biweekly_official_store.build_official_store",
                return_value=MemoryOfficialStore(),
            ),
        ):
            from app.services.biweekly_trigger import build_trigger_service

            service = build_trigger_service(use_official_pdf=False)
        self.assertIs(service.object_storage, self.storage)
        self.assertEqual(service.expected_oss_bucket, BUCKET)

    def test_download_route_still_requires_biweekly_access(self) -> None:
        from app.routers.biweekly import router

        route = next(
            item
            for item in router.routes
            if getattr(item, "endpoint", None) is download_job
        )
        calls = [dep.call for dep in route.dependant.dependencies]
        self.assertIn(require_biweekly_access, calls)


if __name__ == "__main__":
    unittest.main()

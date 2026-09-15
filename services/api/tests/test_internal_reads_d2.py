"""Unit tests for D2 internal read helpers (resolve tag parse, job patch merge)."""

from __future__ import annotations

import asyncio
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.routers import internal_fields as fields_mod
from app.routers import internal_jobs as jobs_mod


class LandIdFromTagsTests(unittest.TestCase):
    def test_extracts_agri_tag(self) -> None:
        self.assertEqual(
            fields_mod._land_id_from_tags(["crop:wheat", "agri:LAND99"]),
            "LAND99",
        )

    def test_ignores_non_list(self) -> None:
        self.assertIsNone(fields_mod._land_id_from_tags(None))
        self.assertIsNone(fields_mod._land_id_from_tags({"agri": "x"}))


class JobPatchMergeTests(unittest.TestCase):
    def test_merge_progress_steps(self) -> None:
        job = MagicMock()
        job.id = uuid.uuid4()
        job.field_id = None
        job.type = "ndvi"
        job.status = "pending"
        job.progress_json = {
            "current_step": "scene_search",
            "steps": {"scene_search": {"status": "completed"}},
        }
        job.error = None
        job.params_json = None
        job.created_at = datetime.now(timezone.utc)
        job.started_at = None
        job.finished_at = None

        db = AsyncMock()
        db.get = AsyncMock(return_value=job)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        body = jobs_mod.InternalJobPatch(
            status="running",
            merge_progress=True,
            progress_json={
                "current_step": "download",
                "steps": {"download": {"status": "running"}},
            },
        )

        out = asyncio.get_event_loop().run_until_complete(
            jobs_mod.patch_job(job.id, body, None, db)
        )
        self.assertEqual(job.status, "running")
        self.assertIsNotNone(job.started_at)
        steps = job.progress_json["steps"]
        self.assertIn("scene_search", steps)
        self.assertIn("download", steps)
        self.assertEqual(out.status, "running")


class ResolveBothIdsTests(unittest.TestCase):
    def test_both_ids_validates_field(self) -> None:
        field = MagicMock()
        field.id = uuid.uuid4()
        field.tags_json = ["agri:L1"]
        field.name = "f"
        field.deleted_at = None

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=field)
        db.execute = AsyncMock(return_value=result)

        out = asyncio.get_event_loop().run_until_complete(
            fields_mod.resolve_field(
                None,
                db,
                field_id=str(field.id),
                land_id="L1",
                parcel_id=None,
            )
        )
        self.assertEqual(out.field_id, str(field.id))
        self.assertEqual(out.land_id, "L1")



class EnsureAgriLandTagTests(unittest.TestCase):
    def test_replace_and_clear(self) -> None:
        from app.core.agri_tags import ensure_agri_land_tag

        self.assertEqual(
            ensure_agri_land_tag(["crop:wheat", "agri:OLD"], "25107"),
            ["crop:wheat", "agri:25107"],
        )
        self.assertEqual(
            ensure_agri_land_tag(["agri:OLD", "cdfinance_group:1"], ""),
            ["cdfinance_group:1"],
        )
        self.assertEqual(ensure_agri_land_tag(None, "9"), ["agri:9"])


class PatchFieldTagsTests(unittest.TestCase):
    def test_merge_land_and_group(self) -> None:
        field = MagicMock()
        field.id = uuid.uuid4()
        field.tags_json = ["crop:corn"]
        field.name = "郎吕坡村委会4号"
        field.deleted_at = None

        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=field)
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        body = fields_mod.FieldTagsPatch(land_id="25107", group_id="7694")
        out = asyncio.get_event_loop().run_until_complete(
            fields_mod.patch_field_tags(str(field.id), body, None, db)
        )
        self.assertEqual(out.land_id, "25107")
        self.assertIn("agri:25107", out.tags or [])
        self.assertIn("cdfinance_group:7694", out.tags or [])
        self.assertEqual(out.name, "郎吕坡村委会4号")
        db.commit.assert_awaited()


if __name__ == "__main__":
    unittest.main()

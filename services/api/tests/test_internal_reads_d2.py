"""Unit tests for direct internal land reads and job progress patching."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.routers import internal_jobs as jobs_mod
from app.routers import internal_lands as lands_mod


def _land() -> MagicMock:
    land = MagicMock()
    land.land_id = "L1"
    land.source_parcel_id = "L1"
    land.tile_id = "tile-1"
    land.virtual_tile_id = None
    land.project_key = None
    land.land_name = "测试地块"
    land.farm_id = None
    land.group_id = "G1"
    land.group_name = "测试项目"
    land.province_name = "河北省"
    land.city_name = "邢台市"
    land.county_name = "清河县"
    land.town_name = None
    land.village_name = None
    land.boundary_geojson = {"type": "MultiPolygon", "coordinates": []}
    land.crop_type = None
    land.season = None
    land.tags_json = ["crop:wheat"]
    land.deleted_at = None
    return land


def test_resolve_land_returns_the_requested_primary_key() -> None:
    land = _land()
    db = AsyncMock()
    db.get = AsyncMock(return_value=land)

    out = asyncio.run(lands_mod.resolve_land(None, db, land_id="L1"))

    assert out.land_id == "L1"
    assert out.tile_id == "tile-1"
    assert out.group_id == "G1"
    db.get.assert_awaited_once()


def test_patch_land_tags_updates_metadata_on_same_row() -> None:
    land = _land()
    db = AsyncMock()
    db.get = AsyncMock(return_value=land)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    body = lands_mod.LandTagsPatch(tags_json=["crop:corn"])
    out = asyncio.run(lands_mod.patch_land_tags("L1", body, None, db))

    assert land.tags_json == ["crop:corn"]
    assert out.land_id == "L1"
    assert out.tile_id == "tile-1"
    db.commit.assert_awaited_once()


def test_job_patch_merges_progress_steps() -> None:
    job = MagicMock()
    job.id = uuid.uuid4()
    job.land_id = "L1"
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

    out = asyncio.run(jobs_mod.patch_job(job.id, body, None, db))

    assert job.status == "running"
    assert job.started_at is not None
    assert "scene_search" in job.progress_json["steps"]
    assert "download" in job.progress_json["steps"]
    assert out.status == "running"

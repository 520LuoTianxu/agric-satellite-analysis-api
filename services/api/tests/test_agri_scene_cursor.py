"""使用测试 PostgreSQL 验证时序游标分页与完整日期组择景。"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from unittest import IsolatedAsyncioTestCase

from app.routers import agri as agri_mod
from app.routers import internal_agri as internal_agri_mod


API_ROOT = Path(__file__).resolve().parents[3]
TEST_ENV_FILE = API_ROOT / "ABflow" / ".env.test"


def _test_database_url() -> str:
    """只从明确的测试库变量或 .env.test 读取连接，禁止回退到默认库。"""
    url = os.environ.get("AGRI_TEST_DATABASE_URL") or dotenv_values(TEST_ENV_FILE).get(
        "DATABASE_URL"
    )
    if not url:
        raise RuntimeError(
            "需要 AGRI_TEST_DATABASE_URL 或 API 仓库 ABflow/.env.test 中的 DATABASE_URL"
        )
    if not url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("遥感集成测试只允许使用 postgresql+asyncpg 测试库")
    return url


class SceneCursorDatabaseTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine(_test_database_url(), pool_pre_ping=True)
        self.connection = None
        self.transaction = None
        self.session = None
        self.land_id = f"codex-scene-cursor-{uuid4().hex}"
        self.tile_id = f"test-tile-{uuid4().hex}"
        self.addAsyncCleanup(self._rollback_and_verify_cleanup)

        self.connection = await self.engine.connect()
        self.transaction = await self.connection.begin()
        self.session = AsyncSession(
            bind=self.connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        await self._insert_land()

    async def _insert_land(self) -> None:
        boundary = {
            "type": "Polygon",
            "coordinates": [
                [
                    [121.0, 31.0],
                    [121.001, 31.0],
                    [121.001, 31.001],
                    [121.0, 31.001],
                    [121.0, 31.0],
                ]
            ],
        }
        await self.session.execute(
            text(
                """
                INSERT INTO agric_satellite.land_parcels (
                    land_id, tile_id, boundary_geojson,
                    min_lon, min_lat, max_lon, max_lat,
                    source_file, source_feature_index
                ) VALUES (
                    :land_id, :tile_id, CAST(:boundary AS jsonb),
                    121.0, 31.0, 121.001, 31.001,
                    'codex-scene-cursor-integration', 0
                )
                """
            ),
            {
                "land_id": self.land_id,
                "tile_id": self.tile_id,
                "boundary": json.dumps(boundary),
            },
        )

    async def _insert_scene(
        self,
        *,
        scene_date: date,
        scene_id: str,
        pixel_data: dict | None = None,
        cloud_cover: float | None = None,
        cloud_cover_over_30: bool | None = None,
    ) -> None:
        await self.session.execute(
            text(
                """
                INSERT INTO agric_satellite.parcel_scene_products (
                    land_id, tile_id, date, sensor, scene_id,
                    pixel_data_url, pixel_data, cloud_cover, cloud_cover_over_30
                ) VALUES (
                    :land_id, :tile_id, :scene_date, 'S2', :scene_id,
                    '', CAST(:pixel_data AS jsonb), :cloud_cover, :cloud_cover_over_30
                )
                """
            ),
            {
                "land_id": self.land_id,
                "tile_id": self.tile_id,
                "scene_date": scene_date,
                "scene_id": scene_id,
                "pixel_data": json.dumps(
                    pixel_data or {"format": "lonlat_v1", "pixels": []}
                ),
                "cloud_cover": cloud_cover,
                "cloud_cover_over_30": cloud_cover_over_30,
            },
        )

    async def _rollback_and_verify_cleanup(self) -> None:
        """回滚整笔测试事务；若意外提交，只清理本测试 UUID 对应的数据。"""
        try:
            if self.session is not None:
                await self.session.close()
            if self.transaction is not None and self.transaction.is_active:
                await self.transaction.rollback()

            if self.land_id and self.engine is not None:
                async with self.engine.connect() as verify:
                    result = await verify.execute(
                        text(
                            """
                            SELECT
                                (SELECT count(*) FROM agric_satellite.land_parcels WHERE land_id = :land_id)
                                + (SELECT count(*) FROM agric_satellite.parcel_scene_products WHERE land_id = :land_id)
                                AS remaining_rows
                            """
                        ),
                        {"land_id": self.land_id},
                    )
                    remaining = int(result.scalar_one())
                    await verify.rollback()
                if remaining:
                    # 只按本测试随机 land_id 清理，避免触碰其他测试或业务数据。
                    async with self.engine.begin() as cleanup:
                        await cleanup.execute(
                            text(
                                "DELETE FROM agric_satellite.parcel_scene_products "
                                "WHERE land_id = :land_id"
                            ),
                            {"land_id": self.land_id},
                        )
                        await cleanup.execute(
                            text(
                                "DELETE FROM agric_satellite.land_parcels "
                                "WHERE land_id = :land_id"
                            ),
                            {"land_id": self.land_id},
                        )
                    async with self.engine.connect() as verify_after_cleanup:
                        check = await verify_after_cleanup.execute(
                            text(
                                """
                                SELECT
                                    (SELECT count(*) FROM agric_satellite.land_parcels WHERE land_id = :land_id)
                                    + (SELECT count(*) FROM agric_satellite.parcel_scene_products WHERE land_id = :land_id)
                                """
                            ),
                            {"land_id": self.land_id},
                        )
                        remaining = int(check.scalar_one())
                        await verify_after_cleanup.rollback()
                self.assertEqual(remaining, 0, "测试结束后仍存在本测试创建的数据")
        finally:
            if self.connection is not None:
                await self.connection.close()
            await self.engine.dispose()

    async def test_cursor_pages_are_stable_and_total_remains_global(self) -> None:
        for scene_date, scene_id in (
            (date(2026, 9, 27), "scene-a"),
            (date(2026, 9, 27), "scene-b"),
            (date(2026, 9, 27), "scene-c"),
            (date(2026, 9, 26), "scene-older"),
        ):
            await self._insert_scene(scene_date=scene_date, scene_id=scene_id)

        first_page = await agri_mod.list_land_scenes(
            self.land_id,
            ctx=None,
            db=self.session,
            sensor="S2",
            date_from=None,
            date_to=None,
            include_pixels=0,
            order="desc",
            limit=2,
            offset=0,
            before_date=None,
            before_scene_id=None,
        )
        cursor_item = first_page["items"][-1]
        second_page = await agri_mod.list_land_scenes(
            self.land_id,
            ctx=None,
            db=self.session,
            sensor="S2",
            date_from=None,
            date_to=None,
            include_pixels=0,
            order="desc",
            limit=2,
            offset=0,
            before_date=cursor_item["date"],
            before_scene_id=cursor_item["scene_id"],
        )

        first_ids = [item["scene_id"] for item in first_page["items"]]
        second_ids = [item["scene_id"] for item in second_page["items"]]
        self.assertEqual(first_ids, ["scene-a", "scene-b"])
        self.assertEqual(second_ids, ["scene-c", "scene-older"])
        self.assertEqual(first_page["total"], 4)
        self.assertEqual(second_page["total"], 4)
        self.assertEqual(len(set(first_ids + second_ids)), 4)

        with self.assertRaises(HTTPException) as raised:
            await agri_mod.list_land_scenes(
                self.land_id,
                ctx=None,
                db=self.session,
                sensor="S2",
                date_from=None,
                date_to=None,
                include_pixels=0,
                order="desc",
                limit=2,
                offset=0,
                before_date=cursor_item["date"],
                before_scene_id=None,
            )
        self.assertEqual(raised.exception.status_code, 422)

    async def test_day_limit_keeps_all_candidates_for_selected_date(self) -> None:
        selected_date = date(2026, 7, 1)
        await self._insert_scene(
            scene_date=selected_date,
            scene_id="A_fair_decloud",
            pixel_data={
                "format": "lonlat_v1",
                "source": "uncrtaints_decloud",
                "decloud_quality": "fair",
                "pixels": [{"lon": 121.0005, "lat": 31.0005, "clear": 1, "NDVI": None}],
            },
        )
        await self._insert_scene(
            scene_date=selected_date,
            scene_id="B_clear_raw",
            pixel_data={
                "format": "lonlat_v1",
                "source": "stac_direct",
                "pixels": [{"lon": 121.0005, "lat": 31.0005, "clear": 1, "NDVI": 0.75}],
            },
            cloud_cover=5.0,
            cloud_cover_over_30=False,
        )
        await self._insert_scene(
            scene_date=date(2026, 7, 2),
            scene_id="C_next_day",
            pixel_data={
                "format": "lonlat_v1",
                "source": "stac_direct",
                "pixels": [{"lon": 121.0005, "lat": 31.0005, "clear": 1, "NDVI": 0.45}],
            },
            cloud_cover=5.0,
            cloud_cover_over_30=False,
        )

        result = await agri_mod.list_ndvi_day_grade_shares(
            self.land_id,
            ctx=None,
            db=self.session,
            date_from=None,
            date_to=None,
            limit=1,
        )

        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].date, selected_date)
        self.assertEqual(result.items[0].scene_id, "B_clear_raw")
        self.assertEqual(result.items[0].n, 1)
        self.assertEqual(result.items[0].counts["绿"], 1)

    async def test_satellite_batch_existing_dates_respect_job_window(self) -> None:
        for scene_date, scene_id in (
            (date(2026, 8, 31), "before-window"),
            (date(2026, 9, 1), "window-start"),
            (date(2026, 9, 15), "window-middle"),
            (date(2026, 9, 20), "window-decloud_decloud"),
            (date(2026, 9, 30), "window-end"),
            (date(2026, 10, 1), "after-window"),
        ):
            await self._insert_scene(
                scene_date=scene_date,
                scene_id=scene_id,
                pixel_data=(
                    {
                        "format": "lonlat_v1",
                        "source": "uncrtaints_decloud",
                        "pixels": [],
                    }
                    if scene_id.endswith("_decloud")
                    else None
                ),
            )

        with self.assertRaises(ValidationError):
            internal_agri_mod.SatelliteBatchInputsRequest(
                land_ids=[self.land_id], sensor="S2", date_from=date(2026, 9, 1)
            )
        with self.assertRaises(ValidationError):
            internal_agri_mod.SatelliteBatchInputsRequest(
                land_ids=[self.land_id],
                sensor="S2",
                date_from=date(2026, 9, 30),
                date_to=date(2026, 9, 1),
            )

        bounded = internal_agri_mod.SatelliteBatchInputsRequest(
            land_ids=[self.land_id],
            sensor="S2",
            date_from=date(2026, 9, 1),
            date_to=date(2026, 9, 30),
        )
        bounded_result = await internal_agri_mod.satellite_batch_inputs(
            bounded, None, self.session
        )
        self.assertEqual(
            bounded_result.items[0].existing_dates,
            [date(2026, 9, 1), date(2026, 9, 15), date(2026, 9, 30)],
        )

        unbounded = internal_agri_mod.SatelliteBatchInputsRequest(
            land_ids=[self.land_id], sensor="S2"
        )
        unbounded_result = await internal_agri_mod.satellite_batch_inputs(
            unbounded, None, self.session
        )
        self.assertEqual(
            unbounded_result.items[0].existing_dates,
            [
                date(2026, 8, 31),
                date(2026, 9, 1),
                date(2026, 9, 15),
                date(2026, 9, 30),
                date(2026, 10, 1),
            ],
        )

        without_dates = internal_agri_mod.SatelliteBatchInputsRequest(
            land_ids=[self.land_id], sensor="S2", include_existing_dates=False
        )
        skipped_result = await internal_agri_mod.satellite_batch_inputs(
            without_dates, None, self.session
        )
        self.assertEqual(skipped_result.items[0].existing_dates, [])
